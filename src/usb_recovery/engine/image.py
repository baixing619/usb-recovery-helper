"""Bounded, read-only ordinary image files and partition discovery.

A ddrescue map is a saved snapshot, not a promise that an unfinished image is
complete. Every view read is checked against physical map offsets.
"""
from __future__ import annotations

import bisect
import os
from pathlib import Path
import re
import stat
import struct
import threading
import zlib

MAX_READ = 64 * 1024 * 1024
MAX_MAP_BYTES = 64 * 1024 * 1024
MAX_MAP_RANGES = 1_000_000
MAX_OFFSET = (1 << 63) - 1


class Cancelled(Exception):
    """A user-requested stop; callers must retain partial output as partial."""


def check_cancel(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise Cancelled('用户已停止任务；已生成的结果可能不完整')


class MapRanges:
    def __init__(self, text):
        self.parts = []
        for number, line in enumerate(text.lstrip('\ufeff').splitlines(), 1):
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            fields = line.split()
            # The current-position/current-status/current-pass header is not a range.
            if len(fields) == 3 and fields[1] in '+?*/-' and fields[2].isdigit():
                continue
            if len(fields) != 3 or fields[2] not in '+?*/-':
                raise ValueError(f'Invalid ddrescue map line {number}')
            try:
                start, size = (int(value, 16 if value.lower().startswith('0x') else 10) for value in fields[:2])
            except ValueError as error:
                raise ValueError(f'Invalid ddrescue map range on line {number}') from error
            expected = self.parts[-1][0] + self.parts[-1][1] if self.parts else 0
            if start != expected or size <= 0 or start + size > MAX_OFFSET:
                raise ValueError('Map ranges must start at zero, be ordered, positive and continuous')
            self.parts.append((start, size, fields[2]))
            if len(self.parts) > MAX_MAP_RANGES:
                raise ValueError('Map range resource limit exceeded')
        if not self.parts:
            raise ValueError('No complete ranges in saved ddrescue map')
        self.end = self.parts[-1][0] + self.parts[-1][1]
        self._starts = [part[0] for part in self.parts]

    def permits(self, position, length):
        if not isinstance(position, int) or not isinstance(length, int) or position < 0 or length < 0 or position + length > self.end:
            return False
        if length == 0:
            return True
        index = max(0, bisect.bisect_right(self._starts, position) - 1)
        target, needed = position + length, position
        for part_index in range(index, len(self.parts)):
            start, size, status = self.parts[part_index]
            if status != '+' or start > needed:
                return False
            needed = min(target, start + size)
            if needed == target:
                return True
        return False


def no_reparse(path):
    """Reject links/junctions on existing ancestors, including dangling links."""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Path contains a symbolic link, junction or reparse point')


def regular_file(path):
    text = os.fspath(path)
    if text.startswith(('\\\\.\\', '\\\\?\\', '\\??\\')):
        raise ValueError('Only ordinary saved image files are accepted; device paths are forbidden')
    candidate = Path(path).absolute()
    no_reparse(candidate)
    info = candidate.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Input must be an ordinary saved file')
    return candidate


class ImageReader:
    def __init__(self, path, map_path=None, offset=0, length=None, cancel=None):
        if not isinstance(offset, int) or offset < 0:
            raise ValueError('Invalid image view offset')
        self.path = regular_file(path)
        self.base_offset = offset
        self.cancel = cancel
        self.map = None
        self.map_path = None
        self.map_sha256 = None
        self.warnings = []
        if map_path is not None:
            import hashlib
            self.map_path = regular_file(map_path)
            if self.map_path.stat().st_size > MAX_MAP_BYTES:
                raise ValueError('Map resource limit exceeded')
            map_data = self.map_path.read_bytes()
            self.map = MapRanges(map_data.decode('utf-8-sig'))
            self.map_sha256 = hashlib.sha256(map_data).hexdigest()
        else:
            self.warnings.append('Image completeness unknown: no ddrescue map was supplied')
        self._handle = self.path.open('rb', buffering=0)
        self._lock = threading.Lock()
        try:
            info = os.fstat(self._handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Input changed: image is no longer an ordinary file')
            self._identity = (info.st_dev, info.st_ino)
            self._file_size = info.st_size
            self.size = info.st_size - offset if length is None else length
            if not isinstance(self.size, int) or self.size < 0 or offset + self.size > info.st_size or offset + self.size > MAX_OFFSET:
                raise ValueError('Image view exceeds the saved file bounds')
        except BaseException:
            self._handle.close()
            raise

    def permits(self, position, length):
        return (isinstance(position, int) and isinstance(length, int) and position >= 0 and length >= 0
                and position + length <= self.size
                and (self.map is None or self.map.permits(self.base_offset + position, length)))

    def read(self, position, length):
        check_cancel(self.cancel)
        if not self.permits(position, length):
            raise ValueError('Requested bytes exceed image bounds or are not all + in the saved map')
        if length > MAX_READ:
            raise ValueError('Single-read resource limit exceeded; stream the image in bounded chunks')
        with self._lock:
            if self._handle.closed:
                raise ValueError('ImageReader is closed')
            self._handle.seek(self.base_offset + position)
            data = self._handle.read(length)
        if len(data) != length:
            raise ValueError('Image became shorter than the requested range')
        check_cancel(self.cancel)
        return data

    def close(self):
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _filesystem(reader, offset, size):
    if size < 512:
        return 'unknown', ['Partition is shorter than a boot sector']
    try:
        boot = reader.read(offset, 512)
    except ValueError as error:
        return 'unknown', [str(error)]
    return ('exfat' if boot[3:11] == b'EXFAT   ' else 'unknown'), []


def _gpt(reader, sector):
    header = reader.read(sector, sector)
    if header[:8] != b'EFI PART':
        return None
    length = struct.unpack_from('<I', header, 12)[0]
    if not 92 <= length <= sector:
        raise ValueError('GPT header length invalid')
    checked = bytearray(header[:length])
    stored = struct.unpack_from('<I', checked, 16)[0]
    checked[16:20] = bytes(4)
    if zlib.crc32(checked) != stored:
        raise ValueError('GPT header CRC invalid; partition offsets were not trusted')
    current, backup, first, last = struct.unpack_from('<QQQQ', header, 24)
    table, count, item_size, table_crc = struct.unpack_from('<QIII', header, 72)
    sectors = reader.size // sector
    if current != 1 or backup >= sectors or first < 2 or first > last or last >= sectors:
        raise ValueError('GPT usable boundaries invalid')
    if not 1 <= count <= 16384 or not 128 <= item_size <= 4096 or item_size % 128 or count * item_size > 16 * 1024 * 1024:
        raise ValueError('GPT entry resource limit or entry size invalid')
    if table < 2 or table * sector + count * item_size > first * sector:
        raise ValueError('GPT entry table overlaps usable data')
    entries = reader.read(table * sector, count * item_size)
    if zlib.crc32(entries) != table_crc:
        raise ValueError('GPT partition-table CRC invalid')
    results, ranges = [], []
    for index in range(count):
        entry = entries[index * item_size:(index + 1) * item_size]
        if entry[:16] == bytes(16):
            continue
        begin, end = struct.unpack_from('<QQ', entry, 32)
        if begin < first or end < begin or end > last or any(begin <= b and end >= a for a, b in ranges):
            raise ValueError('GPT partition bounds invalid or overlapping')
        ranges.append((begin, end))
        offset, size = begin * sector, (end - begin + 1) * sector
        filesystem, warnings = _filesystem(reader, offset, size)
        label = entry[56:128].decode('utf-16le', 'replace').split('\0', 1)[0]
        results.append(dict(id=f'gpt-{index + 1}', offset=offset, size=size, filesystem=filesystem,
                            label=label or f'GPT {index + 1}', warnings=warnings))
    return results


def detect_volumes(reader):
    """Return bounded views relative to reader, never opening raw devices."""
    if reader.size < 512:
        return [dict(id='image', offset=0, size=reader.size, filesystem='unknown', label='镜像', warnings=['Image is shorter than 512 bytes'])]
    warnings = []
    try:
        first = reader.read(0, 512)
    except (ValueError, OSError) as error:
        # A sparse/partial image may have a valid saved backup boot region.
        # Failed guarded reads must not prevent fixed-site discovery or deep scan.
        first = bytes(512)
        warnings.append('Main boot sector is unreadable/unconfirmed: ' + str(error))
    if first[3:11] == b'EXFAT   ':
        return [dict(id='raw-exfat', offset=0, size=reader.size, filesystem='exfat', label='exFAT 裸卷', warnings=list(getattr(reader, 'warnings', [])))]
    for sector in (512, 4096):
        if reader.size < 2 * sector:
            continue
        try:
            result = _gpt(reader, sector)
            if result is not None:
                for item in result:
                    item['warnings'].extend(warnings)
                return result or [dict(id='image', offset=0, size=reader.size, filesystem='unknown', label='空 GPT 镜像', warnings=warnings)]
        except ValueError as error:
            warnings.append(str(error))
    results, ranges = [], []
    if first[510:512] == b'\x55\xAA':
        for index in range(4):
            entry = first[446 + index * 16:462 + index * 16]
            kind = entry[4]
            if not kind:
                continue
            if kind == 0xEE:
                warnings.append('GPT protective MBR found; no valid GPT table was available')
                continue
            if kind in (0x05, 0x0F, 0x85):
                warnings.append('Extended MBR partitions are not supported in this version')
                continue
            start, sectors = struct.unpack_from('<II', entry, 8)
            offset, size = start * 512, sectors * 512
            if start == 0 or not size or offset + size > reader.size or any(offset < b and offset + size > a for a, b in ranges):
                warnings.append(f'MBR partition {index + 1} bounds invalid or overlapping')
                continue
            ranges.append((offset, offset + size))
            filesystem, issues = _filesystem(reader, offset, size)
            results.append(dict(id=f'mbr-{index + 1}', offset=offset, size=size, filesystem=filesystem,
                                label=f'MBR {index + 1}', warnings=issues))
    if results:
        for item in results:
            item['warnings'].extend(warnings)
        return results
    # Try backup discovery only after bounded partition tables. An exFAT
    # partition may itself begin at sector 12 and is not a raw-volume backup.
    for shift in range(9, 13):
        offset = 12 * (1 << shift)
        if offset + 512 > reader.size:
            continue
        try:
            backup = reader.read(offset, 512)
            if backup[3:11] == b'EXFAT   ' and backup[108] == shift and backup[510:512] == b'\x55\xAA':
                return [dict(id='raw-exfat-backup', offset=0, size=reader.size, filesystem='exfat', label='exFAT 裸卷（备份引导线索）',
                             warnings=warnings + ['Main boot is damaged; backup geometry requires independent inventory validation'])]
        except ValueError:
            continue
    return [dict(id='image', offset=0, size=reader.size, filesystem='unknown', label='镜像（文件系统未确认）', warnings=warnings)]
