"""Read-only exFAT inventory and conservative candidate extraction.

Layout/checksums follow Microsoft's exFAT specification:
https://learn.microsoft.com/windows/win32/fileio/exfat-specification
Inactive entries and data after an end marker are evidence of historical
metadata only. Their cluster contents may have been reused.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import struct

from .image import Cancelled, check_cancel, no_reparse

MAX_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_TOTAL_DIRECTORY_BYTES = 256 * 1024 * 1024
MAX_DIRECTORIES = 4096
MAX_ENTRIES = 100_000
MAX_CHAIN_CLUSTERS = 1_000_000
MAX_TOTAL_EXTENTS = 131072
MAX_DEPTH = 128
MAX_BITMAP_BYTES = 64 * 1024 * 1024
CHUNK = 1024 * 1024
EOC = 0xFFFFFFF8


class _ExtentLimit(ValueError):
    """A cumulative inventory metadata budget, separate from corrupt chains."""


def _u16(data, offset):
    return struct.unpack_from('<H', data, offset)[0]


def _u32(data, offset):
    return struct.unpack_from('<I', data, offset)[0]


def _u64(data, offset):
    return struct.unpack_from('<Q', data, offset)[0]


def _checksum(data, bits=16, skip=(2, 3)):
    value = 0
    for index, byte in enumerate(data):
        if index not in skip:
            value = (((value >> 1) | ((value & 1) << (bits - 1))) + byte) & ((1 << bits) - 1)
    return value


def _permitted(reader, position, length):
    return reader.permits(position, length) if hasattr(reader, 'permits') else 0 <= position <= position + length <= reader.size


class _Exfat:
    def __init__(self, reader):
        self.reader = reader
        self.warnings = list(getattr(reader, 'warnings', []))
        self._fat_pages = OrderedDict()
        self._extent_records = 0
        self.resource_limited = False
        self.bitmap = None
        candidates = []
        # A bad/unread main sector must not prevent trying the fixed backup sites.
        sites = [(0, None)] + [(12 * (1 << shift), 1 << shift) for shift in range(9, 13)]
        errors = []
        for offset, expected_sector in sites:
            try:
                first = reader.read(offset, 512)
                if first[3:11] != b'EXFAT   ' or not 9 <= first[108] <= 12:
                    continue
                sector = 1 << first[108]
                if expected_sector is not None and sector != expected_sector:
                    continue
                geometry = self._geometry(first, sector)
                boot = reader.read(offset, sector * 12)
                value = _checksum(boot[:sector * 11], 32, (106, 107, 112))
                valid = all(_u32(boot, i) == value for i in range(sector * 11, sector * 12, 4))
                geometry['boot_checksum_valid'] = valid
                geometry['boot_source'] = 'main' if offset == 0 else 'backup'
                candidates.append((valid, offset, geometry, first))
            except (ValueError, OSError) as error:
                errors.append(str(error))
        if not candidates:
            raise ValueError('No bounded exFAT main/backup boot geometry could be read: ' + '; '.join(errors[:2]))
        candidates.sort(key=lambda item: (not item[0], item[1]))
        _, _, self.geometry, first = candidates[0]
        geometry_keys = ('sector_bytes', 'cluster_bytes', 'volume_bytes', 'fat_offset', 'fat_bytes', 'heap_offset', 'cluster_count', 'root_cluster')
        agrees = all(all(candidate[2][key] == self.geometry[key] for key in geometry_keys) for candidate in candidates)
        sources = {item[2]['boot_source'] for item in candidates}
        assumptions = []
        if not self.geometry['boot_checksum_valid']:
            assumptions.append('Both available boot checksums are invalid; bounded geometry is a salvage hypothesis')
        if not agrees:
            assumptions.append('Main and backup boot geometry disagree; the selected geometry is a salvage hypothesis')
        if sources != {'main', 'backup'}:
            assumptions.append('Only one boot region was usable; independent main/backup agreement is unverified')
        self.geometry.update(verified=self.geometry['boot_checksum_valid'] and agrees and len(sources) == 2,
                             assumptions=assumptions, boot_regions=[dict(source=item[2]['boot_source'], checksum_valid=item[0]) for item in candidates])
        self.warnings.extend(assumptions)
        if self.geometry['truncated_image']:
            self.warnings.append('Saved image is truncated: logical volume extends beyond the saved file; only fully saved, map-confirmed ranges may be recovered')
        self.sector = self.geometry['sector_bytes']
        self.cluster_bytes = self.geometry['cluster_bytes']
        self.cluster_count = self.geometry['cluster_count']
        self.heap = self.geometry['heap_offset']
        self.root = self.geometry['root_cluster']
        self.fat_offset = self.geometry['fat_offset']
        self.fat_bytes = self.geometry['fat_bytes']

    def _geometry(self, first, sector):
        if first[510:512] != b'\x55\xAA' or first[109] > 25 - first[108] or first[110] not in (1, 2):
            raise ValueError('Unsupported exFAT sector/cluster/FAT geometry')
        cluster_bytes = sector << first[109]
        volume_bytes = _u64(first, 72) * sector
        fat_offset, fat_bytes = _u32(first, 80) * sector, _u32(first, 84) * sector
        heap, clusters, root = _u32(first, 88) * sector, _u32(first, 92), _u32(first, 96)
        if not 1 <= clusters <= 0xFFFFFFF5 or not 2 <= root <= clusters + 1:
            raise ValueError('Invalid exFAT cluster count/root')
        truncated = False
        if volume_bytes > self.reader.size:
            mapping = getattr(self.reader, 'map', None)
            saved_size = getattr(self.reader, '_file_size', None)
            base = getattr(self.reader, 'base_offset', None)
            map_end = getattr(mapping, 'end', None)
            # This exception is supported only by a saved acquisition domain
            # larger than its actually written file. A view of a complete disk
            # image must never borrow the next partition's bytes as volume space.
            truncated = (isinstance(saved_size, int) and isinstance(base, int) and isinstance(map_end, int)
                         and map_end > saved_size and base + self.reader.size == saved_size
                         and volume_bytes <= map_end - base)
        if not volume_bytes or (volume_bytes > self.reader.size and not truncated) or fat_offset < 24 * sector:
            raise ValueError('exFAT volume/FAT exceeds image view')
        if fat_bytes < (clusters + 2) * 4 or fat_offset + fat_bytes * first[110] > heap or heap + clusters * cluster_bytes > volume_bytes:
            raise ValueError('Invalid exFAT FAT/heap bounds')
        active = (_u16(first, 106) & 1) if first[110] == 2 else 0
        return dict(sector_bytes=sector, cluster_bytes=cluster_bytes, volume_bytes=volume_bytes,
                    fat_offset=fat_offset + active * fat_bytes, fat_bytes=fat_bytes, heap_offset=heap,
                    cluster_count=clusters, root_cluster=root, fat_count=first[110], active_fat=active,
                    truncated_image=truncated, saved_view_bytes=self.reader.size)

    def next_cluster(self, cluster):
        offset = cluster * 4
        page_offset = (offset // 65536) * 65536
        page = self._fat_pages.get(page_offset)
        if page is None:
            take = min(65536, self.fat_bytes - page_offset)
            if _permitted(self.reader, self.fat_offset + page_offset, take):
                page = self.reader.read(self.fat_offset + page_offset, take)
                self._fat_pages[page_offset] = page
                if len(self._fat_pages) > 16:
                    self._fat_pages.popitem(last=False)
            else:
                return _u32(self.reader.read(self.fat_offset + offset, 4), 0)
        self._fat_pages.move_to_end(page_offset)
        return _u32(page, offset - page_offset)

    def extents(self, first, length, contiguous):
        count = (length + self.cluster_bytes - 1) // self.cluster_bytes
        if not count:
            if first != 0:
                raise ValueError('Empty stream has a nonzero first cluster')
            return []
        if count > self.cluster_count or not 2 <= first <= self.cluster_count + 1:
            raise ValueError('Stream length/first cluster exceeds heap')
        if contiguous:
            if first + count > self.cluster_count + 2:
                raise ValueError('Contiguous stream exceeds heap')
            self.check_extent_budget(1)
            self._extent_records += 1
            return [dict(offset=self.heap + (first - 2) * self.cluster_bytes, length=length, cluster=first)]
        if count > MAX_CHAIN_CLUSTERS:
            raise ValueError('Fragmented-chain resource limit exceeded')
        # Check the full prospective allocation before constructing any dictionaries.
        # Multiple aliases/cross-links must not multiply a chain without a total cap.
        self.check_extent_budget(count)
        output, seen, cluster, remaining = [], set(), first, length
        for index in range(count):
            check_cancel(getattr(self.reader, 'cancel', None))
            if cluster in seen or not 2 <= cluster <= self.cluster_count + 1:
                raise ValueError('FAT cycle or invalid cluster')
            seen.add(cluster)
            take = min(remaining, self.cluster_bytes)
            output.append(dict(offset=self.heap + (cluster - 2) * self.cluster_bytes, length=take, cluster=cluster))
            remaining -= take
            cluster = self.next_cluster(cluster)
            if index < count - 1 and cluster >= EOC:
                raise ValueError('FAT chain ends before declared stream length')
        if cluster < EOC:
            raise ValueError('FAT chain continues beyond declared stream length or cycles')
        self._extent_records += len(output)
        return output

    def check_extent_budget(self, count):
        if self._extent_records + count > MAX_TOTAL_EXTENTS:
            self.resource_limited = True
            raise _ExtentLimit('Cumulative extent metadata resource budget reached; inventory is partial')

    def root_extents(self):
        output, seen, cluster = [], set(), self.root
        while cluster < EOC:
            if cluster in seen or not 2 <= cluster <= self.cluster_count + 1 or (len(output) + 1) * self.cluster_bytes > MAX_DIRECTORY_BYTES:
                raise ValueError('Root directory chain cycles, is invalid or exceeds resource limit')
            seen.add(cluster)
            self.check_extent_budget(1)
            output.append(dict(offset=self.heap + (cluster - 2) * self.cluster_bytes, length=self.cluster_bytes, cluster=cluster))
            self._extent_records += 1
            cluster = self.next_cluster(cluster)
        return output

    def read_extents(self, extents, maximum):
        if sum(item['length'] for item in extents) > maximum:
            raise ValueError('Metadata stream resource limit exceeded')
        result = bytearray()
        for extent in extents:
            offset, remaining = extent['offset'], extent['length']
            while remaining:
                take = min(remaining, CHUNK)
                result.extend(self.reader.read(offset, take))
                offset += take
                remaining -= take
        return bytes(result)

    def allocation_state(self, extents):
        if self.bitmap is None:
            return None
        for extent in extents:
            start = extent['cluster'] - 2
            count = (extent['length'] + self.cluster_bytes - 1) // self.cluster_bytes
            end = start + count
            if end > self.cluster_count:
                return False
            # At most seven edge bits; check whole-byte spans without per-cluster loops.
            while start < end and start % 8:
                if not self.bitmap[start // 8] & (1 << (start % 8)):
                    return False
                start += 1
            whole_end = end - end % 8
            if start < whole_end and any(byte != 255 for byte in self.bitmap[start // 8:whole_end // 8]):
                return False
            start = max(start, whole_end)
            while start < end:
                if not self.bitmap[start // 8] & (1 << (start % 8)):
                    return False
                start += 1
        return True

    def entry_sets(self, data, include_history, inherited_history):
        entries, anomalies, marker, position = [], [], None, 0
        while position + 32 <= len(data):
            if position % 4096 == 0:
                check_cancel(getattr(self.reader, 'cancel', None))
            kind = data[position]
            if kind == 0:
                if marker is None:
                    marker = position
                if not include_history:
                    break
            history = inherited_history or marker is not None or kind == 0x05
            if kind in (0x85, 0x05) and (kind == 0x85 or include_history):
                secondary = data[position + 1]
                length = (secondary + 1) * 32
                raw = data[position:position + length]
                valid = 2 <= secondary <= 18 and len(raw) == length
                if valid:
                    expected = 0xC0 if kind == 0x85 else 0x40
                    valid = raw[32] == expected
                if valid:
                    name_length = raw[35]
                    names = [raw[i:i + 32] for i in range(64, length, 32)]
                    valid = 1 <= name_length <= 255 and len(names) == math.ceil(name_length / 15)
                    valid = valid and all(part[0] == (0xC1 if kind == 0x85 else 0x41) for part in names)
                if valid:
                    normalized = bytearray(raw)
                    if kind == 0x05:
                        for i in range(0, len(normalized), 32):
                            normalized[i] |= 0x80
                    valid = _checksum(normalized) == _u16(raw, 2)
                if valid:
                    try:
                        name = b''.join(part[2:32] for part in names)[:name_length * 2].decode('utf-16le', 'strict')
                    except UnicodeError:
                        valid = False
                if valid:
                    entries.append(dict(name=name, directory=bool(_u16(raw, 4) & 16), flags=raw[33],
                                        size=_u64(raw, 56), valid_size=_u64(raw, 40), first_cluster=_u32(raw, 52),
                                        entry_offset=position, origin='historical' if history else 'current',
                                        active=kind == 0x85, after_end_marker=marker is not None,
                                        checksum_mode='restored_inuse_bits' if kind == 0x05 else 'stored'))
                    position += length
                    continue
                if len(anomalies) < 32:
                    anomalies.append(dict(offset=position, reason='Invalid file entry set/checksum; ignored'))
            position += 32
        return entries, anomalies, marker

    def inventory(self, include_deleted):
        root_bytes = self.read_extents(self.root_extents(), MAX_DIRECTORY_BYTES)
        bitmaps = []
        for offset in range(0, len(root_bytes), 32):
            if root_bytes[offset] == 0:
                break
            if root_bytes[offset] == 0x81 and (root_bytes[offset + 1] & 1) == self.geometry['active_fat']:
                bitmaps.append((_u32(root_bytes, offset + 20), _u64(root_bytes, offset + 24)))
        if len(bitmaps) == 1 and bitmaps[0][1] == (self.cluster_count + 7) // 8:
            try:
                self.bitmap = self.read_extents(self.extents(*bitmaps[0], False), MAX_BITMAP_BYTES)
            except (ValueError, OSError) as error:
                self.warnings.append('Allocation bitmap unavailable: ' + str(error))
        else:
            self.warnings.append('Allocation bitmap is missing or ambiguous; allocation state unverified')
        files, directories = [], []
        queue = deque([((), root_bytes, False, self.root)])
        visited = {self.root}
        total_bytes, total_entries = len(root_bytes), 0
        while queue:
            check_cancel(getattr(self.reader, 'cancel', None))
            components, data, inherited_history, directory_cluster = queue.popleft()
            entries, anomalies, marker = self.entry_sets(data, include_deleted, inherited_history)
            directories.append(dict(path='/'.join(components) or '/', components=list(components),
                                    origin='historical' if inherited_history else 'current', bytes=len(data),
                                    end_marker_offset=marker, anomalies=anomalies, warnings=[]))
            if anomalies:
                self.warnings.append(f'Directory {directories[-1]["path"]}: invalid entry sets were ignored')
            for entry in entries:
                total_entries += 1
                if total_entries > MAX_ENTRIES:
                    self.resource_limited = True
                    self.warnings.append('Entry count resource limit reached; inventory is partial')
                    return self.result(files, directories)
                entry['components'] = list(components) + [entry['name']]
                entry['path'] = '/'.join(entry['components'])
                entry['id'] = f'{directory_cluster}:{entry["entry_offset"]}:{entry["origin"]}'
                warnings, blockers = [], []
                if entry['origin'] == 'historical':
                    warnings.append('Historical metadata candidate: original identity/content may have been reused; not a current-file fact')
                if not self.geometry['verified']:
                    warnings.append('Boot geometry is unverified; independently check recovered contents')
                if entry['valid_size'] != entry['size']:
                    blockers.append('ValidDataLength differs from DataLength; refusing uninitialized/stale bytes')
                if not entry['flags'] & 1 or entry['flags'] & ~3:
                    blockers.append('Unsupported stream flags')
                try:
                    extents = self.extents(entry['first_cluster'], entry['size'], bool(entry['flags'] & 2))
                    if not all(_permitted(self.reader, item['offset'], item['length']) for item in extents):
                        blockers.append('File ranges are not fully + in the saved map')
                except _ExtentLimit as error:
                    self.warnings.append(str(error))
                    return self.result(files, directories)
                except (ValueError, OSError) as error:
                    extents = []
                    blockers.append(str(error))
                entry['extents'] = extents
                allocation = self.allocation_state(extents) if not blockers else None
                entry['allocation_state'] = allocation
                if allocation is not True:
                    warnings.append('Cluster allocation is unverified or no longer allocated; bytes may be stale/reused')
                entry['warnings'] = warnings + blockers
                entry['readable'] = not blockers
                if entry['directory']:
                    if not entry['size'] or entry['size'] % self.cluster_bytes or entry['size'] > MAX_DIRECTORY_BYTES:
                        entry['readable'] = False
                        entry['warnings'].append('Invalid or oversized directory length')
                    if len(entry['components']) > MAX_DEPTH or len(visited) >= MAX_DIRECTORIES or total_bytes + entry['size'] > MAX_TOTAL_DIRECTORY_BYTES:
                        self.resource_limited = True
                        entry['readable'] = False
                        entry['warnings'].append('Directory traversal resource limit reached')
                    if entry['first_cluster'] in visited:
                        entry['readable'] = False
                        entry['warnings'].append('Directory cluster cycle/alias ignored')
                    if entry['readable']:
                        try:
                            contents = self.read_extents(extents, MAX_DIRECTORY_BYTES)
                            visited.add(entry['first_cluster'])
                            total_bytes += len(contents)
                            queue.append((tuple(entry['components']), contents, entry['origin'] == 'historical', entry['first_cluster']))
                        except (ValueError, OSError) as error:
                            entry['readable'] = False
                            entry['warnings'].append(str(error))
                    if not entry['readable']:
                        directories.append(entry)
                        self.warnings.append(f'Directory {entry["path"]} was not traversed: ' + '; '.join(entry['warnings']))
                else:
                    files.append(entry)
        return self.result(files, directories)

    def result(self, files, directories):
        return dict(filesystem='exfat', files=files, directories=directories, warnings=self.warnings, geometry=self.geometry,
                    complete=not self.resource_limited and not self.geometry['truncated_image'], resource_limited=self.resource_limited,
                    extent_records=self._extent_records, extent_limit=MAX_TOTAL_EXTENTS)


def inventory(reader, include_deleted=False):
    return _Exfat(reader).inventory(include_deleted)


_RESERVED = {'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$', 'CONIN$', 'CONOUT$', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10)), *('COM' + i for i in '¹²³'), *('LPT' + i for i in '¹²³')}


def _safe_component(name):
    name = str(name)
    cleaned = ''.join('_' if ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or char in '<>:"/\\|?*' else char for char in name)
    cleaned = cleaned.rstrip(' .')
    if not cleaned or cleaned in ('.', '..'):
        cleaned = '_'
    if cleaned.split('.', 1)[0].rstrip(' .').upper() in _RESERVED:
        cleaned = '_' + cleaned
    if len(cleaned.encode('utf-16le')) > 360:
        suffix = Path(cleaned).suffix[:24]
        prefix = cleaned
        while len(prefix.encode('utf-16le')) > 260:
            prefix = prefix[:-1]
        cleaned = prefix + '~' + hashlib.sha256(name.encode('utf-8', 'surrogatepass')).hexdigest()[:10] + suffix
    return cleaned


def _prepare_directory(path):
    if os.fspath(path).startswith(('\\\\.\\', '\\\\?\\', '\\??\\')):
        raise ValueError('Output must be an ordinary directory, not a device namespace')
    path = Path(path).absolute()
    no_reparse(path)
    path.mkdir(exist_ok=True)
    no_reparse(path)
    if not path.is_dir():
        raise ValueError('Output root must be an ordinary directory')
    return path


@contextmanager
def _output_tree(output_root, components):
    """Pin every Windows directory against rename/reparse mutation while writing.

    FILE_READ_ATTRIBUTES alone does not stop directory renames on Windows;
    GENERIC_READ without delete sharing prevents directory replacement. Setup
    holds write sharing closed until every ancestor has a pinned child and the
    leaf has a non-deletable guard. The fixed nonempty chain then permits write
    sharing, so ordinary sibling files remain usable during a long operation.
    Child components must already be safe single names. Cleanup restores the
    leaf's read-only sharing while its guard is still open, before removing it.
    POSIX checks are suitable for tests; the supported product target is Windows.
    """
    if os.fspath(output_root).startswith(('\\\\.\\', '\\\\?\\', '\\??\\')):
        raise ValueError('Output must be an ordinary directory, not a device namespace')
    if any(not isinstance(component, str) or _safe_component(component) != component for component in components):
        raise ValueError('Output child components must be safe single directory names')
    root = Path(os.path.abspath(output_root))
    handles = []
    pin_paths = []
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        def pin(path, share=1):
            handle = kernel.CreateFileW(str(path), 0x80000000, share, None, 3, 0x02200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise OSError(ctypes.get_last_error(), 'Cannot protect output directory against replacement', str(path))
            handles.append(handle)
            pin_paths.append(path)
            no_reparse(path)
        def replace_pin(index, share):
            old = handles[index]
            pin(pin_paths[index], share)
            replacement = handles.pop()
            pin_paths.pop()
            handles[index] = replacement
            kernel.CloseHandle(old)
    else:
        def pin(path):
            handle = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
            handles.append(handle)
            no_reparse(path)
    try:
        # A volume/share anchor cannot be replaced as a child directory.
        for directory in [*reversed(root.parents), root]:
            if directory == Path(directory.anchor):
                no_reparse(directory)
                # Selecting a volume/share root is valid. Pin it read-only as
                # well, so the leaf guard always has a real handle to replace.
                # Never mkdir an anchor or open a raw volume/device namespace.
                if directory == root:
                    pin(directory)
                continue
            _prepare_directory(directory)
            pin(directory)
        parent = root
        for component in components:
            parent = _prepare_directory(parent / component)
            pin(parent)
        if os.name == 'nt':
            # Keep a non-deletable guard file open before allowing metadata
            # writes in the leaf directory. The directory still cannot move,
            # and Windows refuses reparse points on nonempty directories:
            # https://learn.microsoft.com/windows-hardware/drivers/ifs/fsctl-set-reparse-point
            # Each ancestor also stays nonempty because its child is pinned
            # without delete sharing. Write sharing can therefore be enabled
            # throughout the chain without blocking unrelated sibling files.
            import uuid
            guard = parent / ('.recovery-output-guard-' + uuid.uuid4().hex + '.tmp')
            guard_output = guard.open('xb')
            cleanup_safe, restore_error = False, None
            try:
                with guard_output:
                    try:
                        for index in range(len(handles)):
                            replace_pin(index, 3)
                        yield root, parent
                    finally:
                        try:
                            # Keep the guard open until mutation is blocked;
                            # otherwise an empty leaf could become a junction.
                            replace_pin(len(handles) - 1, 1)
                            cleanup_safe = True
                        except OSError as error:
                            restore_error = error
            finally:
                if cleanup_safe:
                    guard.unlink(missing_ok=True)
            if restore_error is not None:
                raise OSError('Could not safely restore output directory protection; owned guard file was retained', str(guard)) from restore_error
        else:
            yield root, parent
    finally:
        for handle in reversed(handles):
            if os.name == 'nt':
                kernel.CloseHandle(handle)
            else:
                os.close(handle)


def extract_file(reader, entry, output_root):
    """Create a new candidate file; preserve original name and never overwrite.

    On cancellation/read error an explicitly .partial named file is retained.
    A completed filename is published only after the full stream is flushed.
    """
    if not entry.get('readable') or entry.get('directory'):
        raise ValueError('Entry is not a readable file candidate')
    size = entry.get('size')
    valid_size = entry.get('valid_size', size)
    extents = entry.get('extents', [])
    if not isinstance(size, int) or size < 0 or valid_size != size or sum(item['length'] for item in extents) != size:
        raise ValueError('File size/extents are inconsistent')
    if any(not isinstance(item.get('offset'), int) or not isinstance(item.get('length'), int) or item['length'] < 0 or not _permitted(reader, item['offset'], item['length']) for item in extents):
        raise ValueError('File extents are outside confirmed image ranges')
    original = str(entry.get('path', entry.get('name', 'file')))
    components = entry.get('components')
    if components is None:
        components = re.split(r'[/\\]', original)
    if not isinstance(components, (tuple, list)) or not 1 <= len(components) <= MAX_DEPTH:
        raise ValueError('Invalid output path component count')
    cleaned = [_safe_component(component) for component in components]
    warnings = list(entry.get('warnings', []))
    if cleaned != list(components):
        warnings.append('Output name was sanitized; original_path preserves the original metadata name')
    with _output_tree(output_root, cleaned[:-1]) as (root, parent):
        return _write_candidate(reader, entry, extents, original, cleaned, warnings, root, parent)


def _write_candidate(reader, entry, extents, original, cleaned, warnings, root, parent):
    filename = cleaned[-1]
    stem, suffix = Path(filename).stem, Path(filename).suffix
    source = Path(reader.path).resolve() if hasattr(reader, 'path') else None
    check_cancel(getattr(reader, 'cancel', None))
    import uuid
    partial = parent / (filename + '.' + uuid.uuid4().hex[:12] + '.partial')
    output = partial.open('xb', buffering=0)
    digest, written = hashlib.sha256(), 0
    try:
        no_reparse(parent)
        if not parent.resolve().is_relative_to(root.resolve()):
            raise ValueError('Output parent changed during creation')
        with output:
            info = os.fstat(output.fileno())
            if not stat.S_ISREG(info.st_mode) or (hasattr(reader, '_identity') and (info.st_dev, info.st_ino) == reader._identity):
                raise ValueError('Output must be a new ordinary file distinct from the image')
            for extent in extents:
                position, remaining = extent['offset'], extent['length']
                while remaining:
                    check_cancel(getattr(reader, 'cancel', None))
                    take = min(remaining, CHUNK)
                    data = reader.read(position, take)
                    output.write(data)
                    digest.update(data)
                    written += len(data)
                    position += take
                    remaining -= take
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        output.close()
        raise
    for index in range(10000):
        target = parent / (filename if index == 0 else f'{stem}~{index}{suffix}')
        no_reparse(parent)
        no_reparse(target)
        if source is not None and target.resolve() == source:
            continue
        try:
            if os.name == 'nt':
                # Windows rename is CreateNew-like: it refuses an existing target.
                os.rename(partial, target)
            else:
                # POSIX rename may overwrite; an exclusive hardlink does not.
                os.link(partial, target, follow_symlinks=False)
                partial.unlink()
            if index:
                warnings.append('Output name collision: a suffix was added; no existing file was overwritten')
            break
        except FileExistsError:
            continue
    else:
        raise ValueError('Output filename collision limit reached; .partial retained')
    return dict(path=str(target), original_path=original, sha256=digest.hexdigest(), bytes=written,
                origin=entry.get('origin', 'historical'), warnings=warnings)
