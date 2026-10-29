"""Read-only, bounded Office checks and evidence-preserving recovery candidates.

No Office automation, macro execution, formula evaluation, or network access.
Passing these checks proves container/XML/BIFF structure, not business accuracy.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import struct
import urllib.parse
import zipfile
import zlib
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import iterparse as safe_iterparse

MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_CELLS = 500000
MAX_MERGE_AREA = 2000000
MAX_BIFF_RECORDS = 1000000
MAX_XML_NODES = 2000000
MAX_XML_DEPTH = 256
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
C = 'http://schemas.openxmlformats.org/package/2006/content-types'
CT = '[Content_Types].xml'
OLE_MAGIC = bytes.fromhex('D0CF11E0A1B11AE1')
SCOPE = '结构检查；未进行原生 WPS/Excel 验收、公式重算、宏执行或业务完整性核对。'
MAIN_TYPES = {
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml': '.xlsx',
    'application/vnd.ms-excel.sheet.macroEnabled.main+xml': '.xlsm',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml': '.xltx',
    'application/vnd.ms-excel.template.macroEnabled.main+xml': '.xltm',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml': '.docx',
    'application/vnd.ms-word.document.macroEnabled.main+xml': '.docm',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml': '.dotx',
    'application/vnd.ms-word.template.macroEnabledTemplate.main+xml': '.dotm',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml': '.pptx',
    'application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml': '.pptm',
    'application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml': '.ppsx',
    'application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml': '.ppsm',
    'application/vnd.openxmlformats-officedocument.presentationml.template.main+xml': '.potx',
    'application/vnd.ms-powerpoint.template.macroEnabled.main+xml': '.potm',
}


class _Cancelled(Exception):
    pass


class _OutputLimited(OSError):
    pass


def _cancelled(cancel):
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    return bool(cancel.is_set())


def _check_cancel(cancel):
    if _cancelled(cancel):
        raise _Cancelled('已取消')


def _checked_path(path, *, file=False):
    """Reject device/UNC paths and existing reparse points before file I/O."""
    raw = os.fspath(path)
    if raw.startswith(('\\\\', '//')) or '\x00' in raw:
        raise ValueError('只接受本地普通文件路径')
    target = Path(raw).absolute()
    for item in (target, *target.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('不允许符号链接或重解析路径')
    if file and not stat.S_ISREG(target.stat().st_mode):
        raise ValueError('输入必须为普通文件')
    return target


def _write_new(path, data, output_guard=None):
    # Share the engine's Windows ancestor handles, including its nonempty-leaf
    # guard, so a directory cannot become a junction between check and create.
    from .exfat import _output_tree
    path = _checked_path(path)
    parent = _checked_path(path.parent)
    if output_guard is not None:
        try:
            output_guard(path, len(data))
        except Exception as exc:
            raise _OutputLimited(f'输出限制：{exc}') from exc
    with _output_tree(parent, []):
        _checked_path(parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        if _checked_path(path, file=True).read_bytes() != data:
            raise OSError('新建文件读回不一致')
    return str(path)


def _source(path):
    path = _checked_path(path, file=True)
    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ValueError('文件超过 Office 检查资源上限')
    with path.open('rb') as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) != size or len(raw) > MAX_SOURCE_BYTES:
        raise ValueError('输入大小变化或超过资源上限')
    return path, raw


def _safe_member(name):
    return bool(name and '\\' not in name and '\x00' not in name and ':' not in name
                and not name.startswith('/')
                and all(piece not in ('', '.', '..') for piece in name.rstrip('/').split('/')))


def _xml(data):
    # DefusedXML rejects entities/DTDs instead of fetching or expanding them.
    parser = safe_iterparse(io.BytesIO(data), events=('start', 'end'), forbid_dtd=True, forbid_entities=True, forbid_external=True)
    depth, nodes = 0, 0
    for event, _ in parser:
        if event == 'start':
            depth += 1
            nodes += 1
            if depth > MAX_XML_DEPTH or nodes > MAX_XML_NODES:
                raise ValueError('XML 节点数量或嵌套深度超过资源上限')
        else:
            depth -= 1
    return parser.root


def _check_directory(raw, infos):
    """ZipFile tolerates inaccurate EOCD counts; a complete candidate does not."""
    end = raw.rfind(b'PK\x05\x06', max(0, len(raw) - 65557))
    if end < 0 or end + 22 > len(raw):
        raise ValueError('ZIP EOCD 截断')
    _, disk, cd_disk, disk_count, count, cd_size, cd_offset, comment = struct.unpack_from('<4s4H2IH', raw, end)
    if (disk or cd_disk or disk_count != count or count != len(infos)
            or end + 22 + comment != len(raw) or count == 65535
            or cd_offset == 0xffffffff or cd_size == 0xffffffff):
        raise ValueError('ZIP EOCD 数量、范围或单卷结构不一致/ZIP64 不受支持')
    start = end - cd_size
    if start < 0 or start - cd_offset < 0:
        raise ValueError('ZIP 中央目录范围无效')
    cursor = start
    for info in infos:
        if cursor + 46 > end or raw[cursor:cursor + 4] != b'PK\x01\x02':
            raise ValueError('ZIP 中央目录记录截断')
        nlen, xlen, clen = struct.unpack_from('<HHH', raw, cursor + 28)
        cursor += 46 + nlen + xlen + clen
    if cursor != end:
        raise ValueError('ZIP 中央目录数量与实际字节不一致')


def _central_payload(raw, info, cancel):
    """Header damage can be bypassed only when original central CRC/size prove the payload."""
    offset = info.header_offset
    encoded = info.filename.encode('utf-8' if info.flag_bits & 0x800 else 'cp437')
    starts = [offset + 30 + len(encoded)]
    if 0 <= offset and offset + 30 <= len(raw):
        nlen, xlen = struct.unpack_from('<HH', raw, offset + 26)
        starts.insert(0, offset + 30 + nlen + xlen)
    for start in dict.fromkeys(starts):
        _check_cancel(cancel)
        if start < 0 or start + info.compress_size > len(raw):
            continue
        compressed = raw[start:start + info.compress_size]
        try:
            if info.compress_type == 8:
                inflater = zlib.decompressobj(-15)
                data = inflater.decompress(compressed, MAX_PART_BYTES + 1)
                if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                    continue
            else:
                data = compressed
            if len(data) == info.file_size and zlib.crc32(data) == info.CRC:
                return data, start
        except zlib.error:
            continue
    return None


def _zip_parts(raw, cancel=None, tolerant=False):
    parts, evidence, warnings = {}, [], []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise ValueError('ZIP 成员数量或解压总量超过资源上限')
        if not tolerant:
            _check_directory(raw, infos)
        seen = set()
        for info in infos:
            _check_cancel(cancel)
            name = info.filename
            folded = name.casefold()
            if not _safe_member(name) or folded in seen:
                raise ValueError('ZIP 含重复、大小写冲突或不安全成员路径')
            seen.add(folded)
            if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
                raise ValueError('ZIP 不允许链接成员')
            if info.file_size > MAX_PART_BYTES or info.flag_bits & 1 or info.compress_type not in (0, 8):
                raise ValueError('ZIP 部件超限、加密或压缩算法不受支持')
            if info.is_dir():
                continue
            try:
                chunks, length = [], 0
                with archive.open(info) as stream:
                    while True:
                        _check_cancel(cancel)
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        length += len(block)
                        if length > MAX_PART_BYTES:
                            raise ValueError('ZIP 部件解压超过资源上限')
                        chunks.append(block)
                data = b''.join(chunks)
                if len(data) != info.file_size or zlib.crc32(data) != info.CRC:
                    raise ValueError('ZIP CRC/尺寸不一致')
                # Independently bind each original payload to its local header.
                off = info.header_offset
                if off < 0 or raw[off:off + 4] != b'PK\x03\x04' or off + 30 > len(raw):
                    raise ValueError('ZIP 本地头缺失')
                nlen, xlen = struct.unpack_from('<HH', raw, off + 26)
                start = off + 30 + nlen + xlen
                if start + info.compress_size > len(raw):
                    raise ValueError('ZIP 部件范围越界')
                if info.compress_type == 8:
                    inflater = zlib.decompressobj(-15)
                    inflated = inflater.decompress(raw[start:start + info.compress_size], MAX_PART_BYTES + 1)
                    if (not inflater.eof or inflater.unused_data or inflater.unconsumed_tail or inflated != data):
                        raise ValueError('ZIP 原压缩流终点/尺寸不匹配')
                parts[name] = data
                evidence.append({'part': name, 'header_offset': off, 'data_offset': start,
                                 'compressed_size': info.compress_size, 'uncompressed_size': len(data),
                                 'crc32': info.CRC, 'sha256': hashlib.sha256(data).hexdigest(),
                                 'original_crc_valid': True})
            except _Cancelled:
                raise
            except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
                if not tolerant:
                    raise
                warnings.append(f'{name}: {exc}')
                recovered = _central_payload(raw, info, cancel)
                if recovered is not None:
                    data, start = recovered
                    parts[name] = data
                    evidence.append({'part': name, 'header_offset': info.header_offset, 'data_offset': start,
                                     'compressed_size': info.compress_size, 'uncompressed_size': len(data),
                                     'crc32': info.CRC, 'sha256': hashlib.sha256(data).hexdigest(),
                                     'original_crc_valid': True, 'recovery_basis': 'original_central_CRC_size_and_stream_EOF'})
    return parts, evidence, warnings


def _resolve_relationship(rels_name, target):
    split = urllib.parse.urlsplit(target)
    if split.scheme or split.netloc or split.query:
        raise ValueError('内部关系包含外部 URI')
    target = urllib.parse.unquote(split.path)
    owner = '' if rels_name == '_rels/.rels' else rels_name.replace('/_rels/', '/')[:-5]
    resolved = posixpath.normpath(target.lstrip('/') if target.startswith('/')
                                 else posixpath.join(posixpath.dirname(owner), target or posixpath.basename(owner)))
    if not _safe_member(resolved):
        raise ValueError('内部关系越出包边界')
    return resolved


def _coord(value):
    match = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', value or '')
    if not match:
        raise ValueError('原单元格坐标无效')
    column = 0
    for letter in match[1]:
        column = column * 26 + ord(letter) - 64
    row = int(match[2])
    if row > 1048576 or column > 16384:
        raise ValueError('原单元格坐标超出工作表范围')
    return row, column


def _text_runs(element):
    if element is None:
        return None
    return ''.join((node.text or '') for child in element
                   for node in ([child] if child.tag == f'{{{S}}}t' else child.findall(f'{{{S}}}t') if child.tag == f'{{{S}}}r' else []))


def _analyse_parts(parts, cancel=None):
    trees, warnings = {}, []
    for name, data in parts.items():
        _check_cancel(cancel)
        if name.lower().endswith(('.xml', '.rels', '.vml')):
            trees[name] = _xml(data)
    if CT not in trees or trees[CT].tag != f'{{{C}}}Types':
        raise ValueError('缺少有效 Content Types')
    missing_ct = []
    defaults, overrides = {}, {}
    for node in trees[CT]:
        if node.tag == f'{{{C}}}Override':
            part = urllib.parse.unquote(node.get('PartName', '').lstrip('/'))
            if not _safe_member(part):
                raise ValueError('Content Types 部件路径无效')
            if part not in parts:
                missing_ct.append(part)
            if part in overrides or not node.get('ContentType'):
                raise ValueError('Content Types 声明重复或无类型')
            overrides[part] = node.get('ContentType')
        elif node.tag == f'{{{C}}}Default':
            extension = node.get('Extension', '').casefold()
            if not extension or extension in defaults or not node.get('ContentType'):
                raise ValueError('Content Types 默认声明重复或无效')
            defaults[extension] = node.get('ContentType')
        else:
            raise ValueError('Content Types 包含未知节点')
    if missing_ct:
        raise ValueError('Content Types 指向缺失部件：' + ', '.join(missing_ct[:10]))
    def content_type(name):
        return overrides.get(name, defaults.get(PurePosixPath(name).name.rsplit('.', 1)[-1].casefold()))
    if any(not content_type(name) for name in parts if name != CT):
        raise ValueError('原部件缺少 Content Types 声明')
    relationships = {}
    external = 0
    for name, root in trees.items():
        if not name.endswith('.rels'):
            continue
        if root.tag != f'{{{P}}}Relationships':
            raise ValueError('关系 XML 根节点无效')
        mapping = {}
        for node in root:
            ident = node.get('Id')
            if node.tag != f'{{{P}}}Relationship' or not ident or ident in mapping:
                raise ValueError('关系 ID 缺失或重复')
            if node.get('TargetMode') == 'External':
                external += 1
                mapping[ident] = None
                continue
            target = _resolve_relationship(name, node.get('Target', ''))
            if target not in parts:
                raise ValueError(f'关系指向缺失部件：{name} -> {target}')
            mapping[ident] = (target, node.get('Type', ''))
        relationships[name] = mapping
    roots = relationships.get('_rels/.rels', {})
    main = [value[0] for value in roots.values() if value and value[1].endswith('/officeDocument')]
    if len(main) != 1:
        raise ValueError('缺少唯一原始 Office 主部件关系')
    main = main[0]
    kind, extension = None, None
    extension = MAIN_TYPES.get(content_type(main))
    if extension is None:
        raise ValueError('主部件缺少正确的 Office Content Type')
    if trees.get(main) is not None and trees[main].tag == f'{{{S}}}workbook':
        kind = 'OOXML spreadsheet' if extension in ('.xlsx', '.xlsm', '.xltx', '.xltm') else None
    elif main in trees and trees[main].tag.endswith('}document') and '/wordprocessingml/' in trees[main].tag:
        kind = 'OOXML word document' if extension in ('.docx', '.docm', '.dotx', '.dotm') else None
    elif main in trees and trees[main].tag.endswith('}presentation') and '/presentationml/' in trees[main].tag:
        kind = 'OOXML presentation' if extension in ('.pptx', '.pptm', '.ppsx', '.ppsm', '.potx', '.potm') else None
    if kind is None:
        raise ValueError('不支持或无法确认 Office 主部件类型')
    # Check every r:id against its original owner relationship, without fetching external links.
    for name, root in trees.items():
        if name.endswith('.rels'):
            continue
        relation_name = posixpath.join(posixpath.dirname(name), '_rels', posixpath.basename(name) + '.rels')
        ids = relationships.get(relation_name, {})
        for node in root.iter():
            for attr, value in node.attrib.items():
                if attr.startswith('{' + R + '}') and attr.rsplit('}', 1)[-1] in ('id', 'embed', 'link') and value not in ids:
                    raise ValueError(f'原部件关系缺失：{name}, {value}')
    actual_cells, merge_area, worksheets = 0, 0, []
    shared = None
    if 'xl/sharedStrings.xml' in trees:
        shared = [_text_runs(node) for node in trees['xl/sharedStrings.xml'].findall(f'{{{S}}}si')]
        if len(shared) > MAX_CELLS:
            raise ValueError('共享文本数量超过资源上限')
    for name, root in trees.items():
        _check_cancel(cancel)
        if root.tag != f'{{{S}}}worksheet':
            continue
        seen = set()
        count = 0
        for cell in root.findall(f'{{{S}}}sheetData/{{{S}}}row/{{{S}}}c'):
            count += 1
            actual_cells += 1
            if actual_cells > MAX_CELLS:
                raise ValueError('实际 XML 单元格数量超过资源上限')
            coordinate = cell.get('r')
            if coordinate is not None:
                _coord(coordinate)
                if coordinate in seen:
                    raise ValueError('原单元格坐标重复')
                seen.add(coordinate)
            if cell.get('t') == 's':
                value = cell.find(f'{{{S}}}v')
                index = value.text if value is not None else None
                if index is None or not re.fullmatch(r'[0-9]{1,10}', index) or shared is None or int(index) >= len(shared):
                    raise ValueError('缺失共享文本或原共享文本索引无效')
        for node in root.findall(f'{{{S}}}mergeCells/{{{S}}}mergeCell'):
            pair = node.get('ref', '').split(':')
            first, last = _coord(pair[0]), _coord(pair[-1])
            if len(pair) > 2 or last[0] < first[0] or last[1] < first[1]:
                raise ValueError('合并范围无效')
            merge_area += (last[0] - first[0] + 1) * (last[1] - first[1] + 1)
            if merge_area > MAX_MERGE_AREA:
                raise ValueError('合并区域累计面积超过资源上限')
        worksheets.append({'part': name, 'actual_cells': count})
    if kind == 'OOXML spreadsheet':
        book_sheets = trees[main].findall(f'{{{S}}}sheets/{{{S}}}sheet')
        if not book_sheets:
            raise ValueError('原工作簿没有工作表清单')
        relation_name = posixpath.join(posixpath.dirname(main), '_rels', posixpath.basename(main) + '.rels')
        mapping = relationships.get(relation_name, {})
        for sheet in book_sheets:
            relation = mapping.get(sheet.get(f'{{{R}}}id'))
            if not relation or not relation[1].endswith(('/worksheet', '/chartsheet', '/dialogsheet', '/macrosheet')):
                raise ValueError('原工作表关系类型缺失或无效')
            sheet_kind = relation[1].rsplit('/', 1)[-1]
            if relation[0] not in trees or trees[relation[0]].tag != f'{{{S}}}{sheet_kind}':
                raise ValueError('原工作表关系与目标 XML 类型不一致')
            if sheet_kind == 'worksheet' and content_type(relation[0]) != 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml':
                raise ValueError('原工作表 Content Type 无效')
    if external:
        warnings.append(f'包内含 {external} 条外部关系；仅检查原文字，未连接外部资源。')
    return {'format': kind, 'extension': extension, 'zip_crc_valid': True,
            'xml_parts_parsed': len(trees), 'package_parts': len(parts), 'actual_cells': actual_cells,
            'merge_area_checked': merge_area, 'worksheets': worksheets, 'warnings': warnings}


def _inspect_ooxml_bytes(raw, cancel=None):
    parts, _, warnings = _zip_parts(raw, cancel)
    result = _analyse_parts(parts, cancel)
    result['status'] = 'structure_checked'
    result['warnings'] = warnings + result['warnings']
    return result


def _biff_structure(data):
    counter = 0
    def records(start, expected_kind):
        nonlocal counter
        first = True
        while True:
            counter += 1
            if counter > MAX_BIFF_RECORDS or start + 4 > len(data):
                raise ValueError('BIFF 记录截断或超过资源上限')
            tag, length = struct.unpack_from('<HH', data, start)
            end = start + 4 + length
            if end > len(data):
                raise ValueError('BIFF 记录负载截断')
            payload = data[start + 4:end]
            if first:
                if tag not in (0x809, 0x409, 0x209, 9) or length < 4:
                    raise ValueError('BIFF BOF 无效')
                if struct.unpack_from('<H', payload, 2)[0] != expected_kind:
                    raise ValueError('BIFF 子流类型无效')
                first = False
            yield tag, payload
            if tag == 10:
                if length:
                    raise ValueError('BIFF EOF 长度无效')
                return
            start = end
    sheets = []
    for tag, payload in records(0, 5):
        if tag == 0x2f:
            return {'password_protected': True, 'status': 'unverified', 'warnings': ['BIFF 已加密，未验证工作表内容。']}
        if tag == 0x85:
            if len(payload) < 6:
                raise ValueError('BIFF BOUNDSHEET 截断')
            sheets.append((struct.unpack_from('<I', payload)[0], payload[5]))
    if not sheets or len({x[0] for x in sheets}) != len(sheets):
        raise ValueError('BIFF 工作表偏移缺失或重复')
    for offset, kind in sheets:
        if offset >= len(data):
            raise ValueError('BIFF 工作表偏移越界')
        # BIFF sheet kind: worksheet/dialog=0, macro=1, chart=2, VB module=6.
        expected = {0: 0x10, 1: 0x40, 2: 0x20, 6: 6}.get(kind)
        if expected is None:
            raise ValueError('BIFF 工作表种类无法确认')
        list(records(offset, expected))
    return {'biff_record_structure_valid': True, 'sheet_streams_checked': len(sheets)}


def _inspect_ole(raw):
    import olefile
    if len(raw) < 512 or struct.unpack_from('<HH', raw, 28) not in ((0xfffe, 9), (0xfffe, 12)):
        raise ValueError('OLE 头部无效')
    with olefile.OleFileIO(io.BytesIO(raw), raise_defects=olefile.DEFECT_INCORRECT) as archive:
        streams = archive.listdir(streams=True, storages=False)
        if len(streams) > MAX_MEMBERS:
            raise ValueError('OLE 流数量超过资源上限')
        total = 0
        names = {'/'.join(x): x for x in streams}
        for name in streams:
            size = archive.get_size(name)
            total += size
            if size > MAX_PART_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError('OLE 流大小超过资源上限')
            if len(archive.openstream(name).read(MAX_PART_BYTES + 1)) != size:
                raise ValueError('OLE 流截断')
        result = {'format': 'OLE compound document', 'ole_streams_checked': len(streams), 'warnings': []}
        if {'EncryptionInfo', 'EncryptedPackage'}.issubset(names):
            result.update(status='unverified', password_protected=True, warnings=['加密 Office 包，需要密码后才能验证内容。'])
        elif 'Workbook' in names or 'Book' in names:
            name = names.get('Workbook', names.get('Book'))
            result.update(_biff_structure(archive.openstream(name).read()))
            result['extension'] = '.xls'
        elif 'WordDocument' in names:
            result['extension'] = '.doc'
            result['status'] = 'unverified'
            result['warnings'].append('仅验证 OLE 流，未完整解析旧版 Word 文档语义。')
        elif 'PowerPoint Document' in names:
            result['extension'] = '.ppt'
            result['status'] = 'unverified'
            result['warnings'].append('仅验证 OLE 流，未完整解析旧版 PowerPoint 文档语义。')
        else:
            raise ValueError('OLE 内未找到可确认的 Office 主流')
        result.setdefault('status', 'structure_checked')
        return result


def inspect_document(path):
    result = {'path': str(path), 'status': 'unverified', 'warnings': [], 'scope': SCOPE}
    try:
        source, raw = _source(path)
        result.update(path=str(source), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        if source.name.startswith('~$'):
            result.update(status='temporary', warnings=['Office 临时锁文件，不作为正文完整文件。'])
            return result
        if raw.startswith(OLE_MAGIC):
            result.update(_inspect_ole(raw))
        else:
            try:
                result.update(_inspect_ooxml_bytes(raw))
            except Exception:
                result['status'] = 'partial' if (zipfile.is_zipfile(io.BytesIO(raw)) or b'PK\x03\x04' in raw) else 'unverified'
                raise
        result['extension_mismatch'] = bool(result.get('extension') and source.suffix.lower() != result['extension'])
    except Exception as exc:
        result['warnings'].append(f'{type(exc).__name__}: {exc}')
    return result


def _local_parts(raw, cancel=None):
    """Recover only complete, bounded local records whose original CRC matches."""
    parts, evidence, warnings, seen = {}, [], [], set()
    offset, total, signatures = 0, 0, 0
    while True:
        _check_cancel(cancel)
        offset = raw.find(b'PK\x03\x04', offset)
        if offset < 0:
            break
        start_header = offset
        offset += 4
        signatures += 1
        if signatures > MAX_MEMBERS:
            warnings.append('本地头搜索达到资源上限；剩余部分未检查。')
            break
        try:
            if start_header + 30 > len(raw):
                raise ValueError('本地头截断')
            version, flags, method, _, _, crc, cs, us, nlen, xlen = struct.unpack_from('<5H3I2H', raw, start_header + 4)
            if flags & 9 or method not in (0, 8) or version > 63 or not 0 < nlen <= 4096 or xlen > 65535:
                raise ValueError('本地头加密/数据描述符或参数不受支持')
            if us > MAX_PART_BYTES or cs > MAX_SOURCE_BYTES or total + us > MAX_TOTAL_BYTES:
                raise ValueError('本地头大小超过资源上限')
            name = raw[start_header + 30:start_header + 30 + nlen].decode('utf-8' if flags & 0x800 else 'cp437')
            if not _safe_member(name) or name.casefold() in seen:
                raise ValueError('不安全或重复的本地成员名')
            seen.add(name.casefold())
            begin = start_header + 30 + nlen + xlen
            end = begin + cs
            if end > len(raw):
                raise ValueError('原压缩部件截断')
            compressed = raw[begin:end]
            if method == 8:
                inflater = zlib.decompressobj(-15)
                data = inflater.decompress(compressed, MAX_PART_BYTES + 1)
                if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                    raise ValueError('原压缩流 EOF/范围不匹配')
            else:
                data = compressed
            if len(data) != us or zlib.crc32(data) != crc:
                raise ValueError('原部件 CRC/尺寸不匹配')
            if name.lower().endswith(('.xml', '.rels', '.vml')):
                _xml(data)
            if not name.endswith('/'):
                parts[name] = data
                total += us
                evidence.append({'part': name, 'header_offset': start_header, 'data_offset': begin,
                                 'compressed_size': cs, 'uncompressed_size': us, 'crc32': crc,
                                 'sha256': hashlib.sha256(data).hexdigest(), 'original_crc_valid': True})
            offset = end
        except (ValueError, UnicodeError, struct.error, zlib.error, ET.ParseError) as exc:
            warnings.append(f'本地头 {start_header}: {exc}')
    return parts, evidence, warnings


def _relationships(items):
    root = ET.Element('Relationships', xmlns=P)
    for ident, kind, target in items:
        ET.SubElement(root, 'Relationship', Id=ident, Type=R + '/' + kind, Target=target)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _repair_parts(parts):
    result, changes, inferred = dict(parts), [], False
    if CT in result:
        root = _xml(result[CT])
        missing = [n for n in root if n.get('PartName') and n.get('PartName').lstrip('/') not in parts]
        has_shared_cells = any(cell.get('t') == 's' for name, data in parts.items() if name.startswith('xl/worksheets/') and name.endswith('.xml')
                               for cell in _xml(data).iter(f'{{{S}}}c'))
        shared_rel = any(node.get('Type', '').endswith('/sharedStrings') for name, data in parts.items() if name.endswith('.rels') for node in _xml(data))
        if (len(missing) == 1 and missing[0].get('PartName') == '/xl/sharedStrings.xml'
                and missing[0].get('ContentType', '').endswith('.sharedStrings+xml')
                and not has_shared_cells and not shared_rel
                and not any(name.casefold() == 'xl/sharedstrings.xml' for name in parts)):
            root.remove(missing[0])
            result[CT] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            changes.append({'part': CT, 'operation': 'remove_unused_dangling_sharedStrings_declaration', 'inferred': False})
    main = next((name for name in ('xl/workbook.xml', 'word/document.xml', 'ppt/presentation.xml') if name in parts), None)
    if main is None:
        return result, changes, inferred
    if '_rels/.rels' not in result:
        result['_rels/.rels'] = _relationships([('recoveredRoot', 'officeDocument', main)])
        changes.append({'part': '_rels/.rels', 'operation': 'infer_root_from_original_main_part', 'inferred': True})
        inferred = True
    rel_name = 'xl/_rels/workbook.xml.rels'
    if main == 'xl/workbook.xml' and rel_name not in result:
        sheets = _xml(parts[main]).findall(f'{{{S}}}sheets/{{{S}}}sheet')
        sheet_parts = sorted(name for name in parts if re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml', name))
        # One worksheet has a unique possible mapping. Multi-sheet ordering is unknown.
        if len(sheets) == len(sheet_parts) == 1 and sheets[0].get(f'{{{R}}}id'):
            entries = [(sheets[0].get(f'{{{R}}}id'), 'worksheet', sheet_parts[0][3:])]
            for name, kind in [('xl/sharedStrings.xml', 'sharedStrings'), ('xl/styles.xml', 'styles')]:
                if name in parts:
                    entries.append(('recovered' + kind, kind, name[3:]))
            result[rel_name] = _relationships(entries)
            changes.append({'part': rel_name, 'operation': 'infer_unique_single_worksheet_mapping', 'inferred': True})
            inferred = True
    if CT not in result:
        root = ET.Element('Types', xmlns=C)
        ET.SubElement(root, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
        ET.SubElement(root, 'Default', Extension='xml', ContentType='application/xml')
        types = {
            'xl/workbook.xml': 'application/vnd.ms-excel.sheet.macroEnabled.main+xml' if 'xl/vbaProject.bin' in parts else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',
            'word/document.xml': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml',
            'ppt/presentation.xml': 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml',
            'xl/sharedStrings.xml': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml',
            'xl/styles.xml': 'application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml',
            'xl/vbaProject.bin': 'application/vnd.ms-office.vbaProject',
        }
        for name in sorted(result):
            kind = types.get(name)
            if re.fullmatch(r'xl/worksheets/sheet[0-9]+\.xml', name):
                kind = 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'
            if kind:
                ET.SubElement(root, 'Override', PartName='/' + name, ContentType=kind)
        result[CT] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        changes.append({'part': CT, 'operation': 'derive_standard_content_types_from_original_part_names', 'inferred': True})
    return result, changes, inferred


def _pack(parts):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            archive.writestr(name, parts[name])
    return stream.getvalue()


def _partial_records(parts, cancel=None):
    shared, labels, sheets = None, {}, []
    if 'xl/sharedStrings.xml' in parts:
        shared = [_text_runs(node) for node in _xml(parts['xl/sharedStrings.xml']).findall(f'{{{S}}}si')]
    if 'xl/workbook.xml' in parts and 'xl/_rels/workbook.xml.rels' in parts:
        rels = {node.get('Id'): _resolve_relationship('xl/_rels/workbook.xml.rels', node.get('Target', ''))
                for node in _xml(parts['xl/_rels/workbook.xml.rels']) if node.get('TargetMode') != 'External'}
        for sheet in _xml(parts['xl/workbook.xml']).findall(f'{{{S}}}sheets/{{{S}}}sheet'):
            if sheet.get(f'{{{R}}}id') in rels:
                labels[rels[sheet.get(f'{{{R}}}id')]] = sheet.get('name')
    count = 0
    for name, data in sorted(parts.items()):
        _check_cancel(cancel)
        if not name.lower().endswith('.xml'):
            continue
        root = _xml(data)
        if root.tag != f'{{{S}}}worksheet':
            paragraph_ns = ('http://schemas.openxmlformats.org/wordprocessingml/2006/main' if name.startswith('word/')
                            else 'http://schemas.openxmlformats.org/drawingml/2006/main' if name.startswith(('ppt/slides/', 'ppt/notesSlides/')) else None)
            if paragraph_ns:
                paragraphs = []
                for node in root.iter(f'{{{paragraph_ns}}}p'):
                    count += 1
                    if count > MAX_CELLS:
                        raise ValueError('残缺视图文字段落超过资源上限')
                    text = ''.join((child.text or '') if child.tag == f'{{{paragraph_ns}}}t' else '\t' if child.tag == f'{{{paragraph_ns}}}tab' else '\n' if child.tag in (f'{{{paragraph_ns}}}br', f'{{{paragraph_ns}}}cr') else '' for child in node.iter())
                    paragraphs.append({'original_paragraph_ordinal': len(paragraphs) + 1, 'text': text})
                if paragraphs:
                    sheets.append({'part': name, 'original_sheet_name': None, 'cells': [],
                                   'paragraphs': paragraphs, 'dimension_as_recorded': None, 'merges_as_recorded': []})
            continue
        cells = []
        for node in root.findall(f'{{{S}}}sheetData/{{{S}}}row/{{{S}}}c'):
            count += 1
            if count > MAX_CELLS:
                raise ValueError('残缺视图实际单元格超过资源上限')
            value, formula = node.find(f'{{{S}}}v'), node.find(f'{{{S}}}f')
            raw = None if value is None else value.text
            typ, status = node.get('t', 'n'), 'original_stored_value'
            display = raw
            if typ == 's':
                if raw is not None and re.fullmatch(r'[0-9]{1,10}', raw) and shared is not None and int(raw) < len(shared):
                    display = shared[int(raw)]
                    status = 'original_shared_text'
                else:
                    display, status = f'【缺失共享文本：原索引 {raw}】', 'missing_shared_strings'
            elif typ == 'inlineStr':
                display = _text_runs(node.find(f'{{{S}}}is'))
                status = 'original_inline_text' if display is not None else 'missing_inline_text'
            elif value is None:
                display, status = '【原 XML 单元格无值节点；不是补成 0】', 'original_no_value_element'
            elif raw is None:
                display, status = '【原值节点为空；不是补成 0】', 'original_empty_value_element'
            cells.append({'coordinate': node.get('r'), 'attributes': dict(node.attrib), 'type': typ,
                          'raw_value': raw, 'value_element_present': value is not None,
                          'display_text': display, 'status': status,
                          'formula': None if formula is None else {'original_text': formula.text,
                                                                    'attributes': dict(formula.attrib), 'original_cache': raw}})
        dimension = root.find(f'{{{S}}}dimension')
        sheets.append({'part': name, 'original_sheet_name': labels.get(name),
                       'dimension_as_recorded': None if dimension is None else dimension.get('ref'),
                       'merges_as_recorded': [node.get('ref') for node in root.findall(f'{{{S}}}mergeCells/{{{S}}}mergeCell')],
                       'cells': cells})
    return sheets


def _partial_html(sheets):
    escape = lambda value: html.escape(str(value) if value is not None else '【原文字缺失】', quote=True)
    content = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
               '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
               '<title>残缺文档可读核对件</title><style>body{font:15px sans-serif;margin:24px}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:6px;white-space:pre-wrap;overflow-wrap:anywhere}aside{background:#fff1ce;padding:14px}code{white-space:pre-wrap}</style></head><body>',
               '<h1>残缺文档可读核对件</h1><aside>仅展示本文件原 CRC 有效且 XML 完整的部件。缺失的工作表、文字、坐标和缓存不猜补。',
               '只列实际 XML 记录；未列出的坐标为【XML 未记录单元格】，不等于 0 或已证明损坏。',
               '公式原文和原缓存可能陈旧；不重算、不执行宏，不加载网络资源。合并、样式、打印版式未还原。',
               '原表名关系缺失时使用部件名；本页不是完整业务凭证。</aside>']
    for sheet in sheets:
        if 'paragraphs' in sheet:
            content.extend(['<h2>', escape(sheet['part']), '</h2><p>以下仅保留原部件中的段落文字及顺序，不还原图像、链接目标和页面版式。</p><table><tr><th>原段落序号</th><th>原文字</th></tr>'])
            for paragraph in sheet['paragraphs']:
                content.extend(['<tr><td>', str(paragraph['original_paragraph_ordinal']), '</td><td>', escape(paragraph['text']), '</td></tr>'])
            content.append('</table>')
            continue
        content.extend(['<h2>', escape(sheet['original_sheet_name'] or sheet['part'] + '（原表名关系缺失）'), '</h2><p>原部件：', escape(sheet['part']),
                        '；原 dimension：', escape(sheet['dimension_as_recorded']), '</p><p>原合并范围（未展开）：', escape(', '.join(str(x) for x in sheet['merges_as_recorded'])), '</p>',
                        '<table><tr><th>原坐标</th><th>原值 / 缺失标记</th><th>公式原文</th><th>原缓存</th><th>原属性</th></tr>'])
        for cell in sheet['cells']:
            formula = cell['formula']
            content.extend(['<tr data-source-cell="', escape(cell['coordinate'] or ''), '"><td>', escape(cell['coordinate']), '</td><td>', escape(cell['display_text']),
                            '</td><td><code>', escape(formula['original_text']) if formula else '【无原公式节点】', '</code></td><td>',
                            escape(formula['original_cache']) if formula else '【非公式】', '</td><td><code>',
                            escape(json.dumps({'cell': cell['attributes'], 'formula': None if formula is None else formula['attributes']}, ensure_ascii=False)), '</code></td></tr>'])
        content.append('</table>')
    content.append('</body></html>')
    return ''.join(content).encode('utf-8')


def salvage_document(path, output_dir, cancel=None, output_guard=None):
    result = {'source': str(path), 'status': 'unverified', 'candidates': [], 'partial_view': None,
              'warnings': [], 'cancelled': False, 'output_limited': False, 'scope': SCOPE}
    try:
        _check_cancel(cancel)
        source, raw = _source(path)
        digest = hashlib.sha256(raw).hexdigest()
        result.update(source=str(source), source_sha256=digest)
        inspection = inspect_document(source)
        result['status'] = inspection['status']
        result['warnings'].extend(inspection['warnings'])
        if inspection['status'] in ('temporary', 'structure_checked') or raw.startswith(OLE_MAGIC):
            return result
        output = _checked_path(output_dir)
        try:
            parts, evidence, warnings = _zip_parts(raw, cancel, tolerant=True)
        except zipfile.BadZipFile:
            parts, evidence, warnings = _local_parts(raw, cancel)
        result['warnings'].extend(warnings)
        # XML whose original CRC passes but whose syntax fails remains unusable.
        for name in list(parts):
            if name.lower().endswith(('.xml', '.rels', '.vml')):
                try:
                    _xml(parts[name])
                except Exception as exc:
                    result['warnings'].append(f'{name}: XML 不完整/不安全，未用于候选：{exc}')
                    del parts[name]
        repaired, changes, inferred = _repair_parts(parts)
        _check_cancel(cancel)
        try:
            candidate = _pack(repaired)
            validation = _inspect_ooxml_bytes(candidate, cancel)
            # Every unchanged original part remains byte-identical in the new container.
            changed_names = {change['part'] for change in changes}
            if any(repaired.get(name) != data for name, data in parts.items() if name not in changed_names):
                raise ValueError('原部件内容发生非元数据变化')
            extension = validation['extension']
            destination = output / f'recovered_{digest[:20]}_candidate{extension}'
            _check_cancel(cancel)
            target = _write_new(destination, candidate, output_guard)
            result['candidates'].append({'path': target, 'status': 'repaired_candidate', 'source': str(source),
                                         'source_sha256': digest, 'sha256': hashlib.sha256(candidate).hexdigest(),
                                         'inferred_relationships': inferred, 'changes': changes,
                                         'original_parts_preserved': sorted(name for name in parts if name not in changed_names),
                                         'validation': validation})
            result['status'] = 'repaired_candidate'
        except (_Cancelled, _OutputLimited):
            raise
        except Exception as exc:
            result['warnings'].append(f'未生成完整容器候选：{exc}')
        _check_cancel(cancel)
        sheets = _partial_records(parts, cancel)
        if sheets:
            records = {'source': str(source), 'source_sha256': digest, 'sheets': sheets,
                       'original_part_evidence': evidence, 'scope': SCOPE,
                       'missing_content_not_invented': True}
            try:
                _check_cancel(cancel)
                json_path = _write_new(output / f'partial_{digest[:20]}.json', json.dumps(records, ensure_ascii=False, indent=2).encode('utf-8'), output_guard)
                result['partial_records'] = json_path
                _check_cancel(cancel)
                html_path = _write_new(output / f'partial_{digest[:20]}.html', _partial_html(sheets), output_guard)
                result.update(partial_view=html_path, partial_records=json_path)
                if not result['candidates']:
                    result['status'] = 'partial'
            except (_Cancelled, _OutputLimited):
                raise
            except Exception as exc:
                result['warnings'].append(f'残缺核对件未全部写出：{exc}')
        _, after = _source(source)
        result['source_unchanged'] = hashlib.sha256(after).hexdigest() == digest
        if not result['source_unchanged']:
            result['warnings'].append('处理期间输入副本改变；结果需要复核。')
    except _Cancelled:
        result.update(cancelled=True)
        result['warnings'].append('已取消；已写出的结果保留，不能视为完整恢复。')
    except _OutputLimited as exc:
        result['output_limited'] = True
        result['warnings'].append(str(exc))
    except Exception as exc:
        result['warnings'].append(f'{type(exc).__name__}: {exc}')
    return result
