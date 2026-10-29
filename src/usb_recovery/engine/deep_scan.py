"""Bounded sequential image scan for complete contiguous OOXML candidates.

Ranges derive from a checked EOCD/central directory, not guessed filenames.
All image bytes are read for coverage, including zero/FF regions. ZIP64 and
resource-limited candidates are explicitly reported as unexamined.
"""
from __future__ import annotations

import hashlib
import struct

from .office import (_Cancelled, _OutputLimited, _cancelled, _check_cancel, _checked_path,
                     _inspect_ooxml_bytes, _write_new, MAX_MEMBERS)

CHUNK_SIZE = 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_SIGNATURES = 200000
MAX_VERIFICATIONS = 1024
MAX_VERIFICATION_BYTES = 2 * 1024 * 1024 * 1024
EOCD = b'PK\x05\x06'


def _scan_ranges(reader, size):
    """Partition a reader view at saved-map boundaries without filling holes.

    Generic readers retain the size/read-only contract. ImageReader's map uses
    physical offsets, so a partition view must be translated by base_offset.
    """
    mapping = getattr(reader, 'map', None)
    parts = getattr(mapping, 'parts', None)
    if parts is None:
        if size:
            yield 0, size, True
        return
    base = int(getattr(reader, 'base_offset', 0))
    end = base + size
    cursor = 0
    for start, length, state in parts:
        if start + length <= base:
            continue
        if start >= end:
            break
        first, last = max(start, base) - base, min(start + length, end) - base
        if first > cursor:
            yield cursor, first, False
        if last > first:
            yield first, last, state == '+'
        cursor = max(cursor, last)
    if cursor < size:
        yield cursor, size, False


def _read_exact(reader, position, length):
    if position < 0 or length < 0 or position + length > reader.size:
        raise ValueError('候选字节范围超出镜像')
    data = reader.read(position, length)
    if len(data) != length:
        raise OSError(f'镜像短读：位置 {position}，需要 {length}，实读 {len(data)}')
    return data


def _read_candidate(reader, position, length, cancel):
    chunks = []
    while length:
        _check_cancel(cancel)
        amount = min(CHUNK_SIZE, length)
        chunks.append(_read_exact(reader, position, amount))
        length -= amount
        position += amount
    return b''.join(chunks)


def _bounds(reader, position):
    header = _read_exact(reader, position, 22)
    signature, disk, cd_disk, disk_entries, entries, cd_size, cd_offset, comment = struct.unpack('<4s4H2IH', header)
    if signature != EOCD or disk != 0 or cd_disk != 0 or entries != disk_entries or entries == 0:
        return None
    if entries == 65535 or cd_size == 0xffffffff or cd_offset == 0xffffffff:
        return {'limited': 'ZIP64 候选暂不支持，未完成该候选检查。'}
    end = position + 22 + comment
    start = position - cd_size - cd_offset
    if start < 0 or end > reader.size or cd_size == 0:
        return None
    if entries > MAX_MEMBERS or end - start > MAX_ARCHIVE_BYTES:
        return {'limited': 'Office 候选超过成员数量或字节资源上限，未验证。'}
    # The computed directory must really start with a central record.
    central = _read_exact(reader, position - cd_size, min(cd_size, 4))
    if central != b'PK\x01\x02':
        return None
    return {'offset': start, 'length': end - start, 'end': end}


def scan_office(reader, output_dir, known_hashes=None, progress=None, cancel=None, output_guard=None):
    result = {'files': [], 'warnings': [], 'cancelled': False, 'scanned_bytes': 0,
              'complete': False, 'duplicate_hashes': 0, 'output_limited': False}
    hashes = set(known_hashes or ())
    seen_offsets, seen_ranges = set(), set()
    position, tail, signatures, verification_count, verification_bytes = 0, b'', 0, 0, 0
    limited, failures = False, False
    size = int(reader.size)
    if size < 0:
        result['warnings'].append('镜像大小无效。')
        return result
    try:
        output = _checked_path(output_dir)
        ranges = iter(_scan_ranges(reader, size))
        range_end, range_readable = 0, True
        while position < size:
            _check_cancel(cancel)
            if position >= range_end:
                range_start, range_end, range_readable = next(ranges)
                position = range_start
            if not range_readable:
                failures = True
                if len(result['warnings']) < 200:
                    result['warnings'].append(f'镜像范围 {position}..{range_end} 未在保存的 map 中确认；已跳过，未填零。')
                position = range_end
                tail = b''
                if progress:
                    progress({'message': '部分镜像范围未读取，继续检查已确认范围', 'percent': position * 100 / size,
                              'bytes_done': result['scanned_bytes'], 'bytes_total': size})
                continue
            length = min(CHUNK_SIZE, range_end - position)
            try:
                block = _read_exact(reader, position, length)
            except Exception as exc:
                if _cancelled(cancel):
                    raise _Cancelled()
                failures = True
                result['warnings'].append(f'镜像范围 {position}..{position + length} 未读：{exc}')
                position += length
                tail = b''
                if progress:
                    progress({'message': '部分镜像范围不可读，继续检查其他范围', 'percent': position * 100 / size,
                              'bytes_done': result['scanned_bytes'], 'bytes_total': size})
                continue
            result['scanned_bytes'] += length
            data = tail + block
            base = position - len(tail)
            # Skip signature work for empty blocks, never the physical read.
            search = 0
            while signatures <= MAX_SIGNATURES:
                _check_cancel(cancel)
                index = data.find(EOCD, search)
                if index < 0:
                    break
                search = index + 4
                absolute = base + index
                if absolute in seen_offsets:
                    continue
                seen_offsets.add(absolute)
                signatures += 1
                if signatures > MAX_SIGNATURES:
                    limited = True
                    result['warnings'].append('EOCD 签名数量超过资源上限；剩余候选未检查。')
                    break
                try:
                    bounds = _bounds(reader, absolute)
                    if not bounds:
                        continue
                    if 'limited' in bounds:
                        limited = True
                        result['warnings'].append(f'位置 {absolute}: {bounds["limited"]}')
                        continue
                    key = (bounds['offset'], bounds['length'])
                    if key in seen_ranges:
                        continue
                    seen_ranges.add(key)
                    if verification_count >= MAX_VERIFICATIONS or verification_bytes + bounds['length'] > MAX_VERIFICATION_BYTES:
                        limited = True
                        result['warnings'].append('候选验证数量或总字节超过资源上限；其余候选未验证。')
                        continue
                    verification_count += 1
                    verification_bytes += bounds['length']
                    _check_cancel(cancel)
                    raw = _read_candidate(reader, bounds['offset'], bounds['length'], cancel)
                    _check_cancel(cancel)
                    validation = _inspect_ooxml_bytes(raw, cancel)
                    digest = hashlib.sha256(raw).hexdigest()
                    if digest in hashes:
                        result['duplicate_hashes'] += 1
                        continue
                    _check_cancel(cancel)
                    path = _write_new(output / f'office_offset_{bounds["offset"]:016x}_{digest[:16]}{validation["extension"]}', raw, output_guard)
                    hashes.add(digest)
                    result['files'].append({'path': path, 'sha256': digest, 'bytes': len(raw),
                                            'offset': bounds['offset'], 'length': bounds['length'],
                                            'end_offset': bounds['end'], 'status': 'structure_checked',
                                            'origin': 'historical_candidate', 'original_name_known': False,
                                            'original_version_known': False, 'validation': validation,
                                            'warnings': ['连续完整 OOXML 候选；原文件名、版本及是否仍有效未知。']})
                except (_Cancelled, _OutputLimited):
                    raise
                except FileExistsError as exc:
                    limited = True
                    result['warnings'].append(f'结果已存在，未覆盖：{exc}')
                except OSError as exc:
                    failures = True
                    result['warnings'].append(f'位置 {absolute} 候选读取/输出失败：{exc}')
                except Exception as exc:
                    # Rejected signatures are expected. Keep a bounded diagnostic list.
                    if len(result['warnings']) < 200:
                        result['warnings'].append(f'位置 {absolute} 未成为完整 Office 候选：{exc}')
            if signatures > MAX_SIGNATURES:
                # The outer loop keeps range-aware reading coverage while no
                # longer indexing signatures. It must not bridge unknown gaps.
                tail = b''
                position += length
                if progress:
                    progress({'message': '已达候选资源上限，继续核对读取覆盖', 'percent': position * 100 / size,
                              'bytes_done': result['scanned_bytes'], 'bytes_total': size})
                continue
            tail = data[-3:]
            position += length
            if progress:
                progress({'message': '正在搜索完整 Office 候选', 'percent': position * 100 / size,
                          'bytes_done': result['scanned_bytes'], 'bytes_total': size})
        _check_cancel(cancel)
        result['complete'] = not limited and not failures and result['scanned_bytes'] == size
    except _Cancelled:
        result['cancelled'] = True
        result['warnings'].append('已取消；已导出的候选保留，未完成全镜像搜索。')
    except _OutputLimited as exc:
        result['output_limited'] = True
        result['warnings'].append(str(exc))
    except Exception as exc:
        result['warnings'].append(f'{type(exc).__name__}: {exc}')
    return result
