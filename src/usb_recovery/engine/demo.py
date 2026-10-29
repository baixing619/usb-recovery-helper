"""A deterministic, wholly fictional exFAT image for offline self-checks."""
from __future__ import annotations

import io
from pathlib import Path
import struct
import zipfile

from .exfat import _checksum
from .image import no_reparse


def _spreadsheet():
    content = {
        '[Content_Types].xml': '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="虚构示例" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml': '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>虚构演示资料</t></is></c><c r="B1" t="inlineStr"><is><t>数量</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>示例项目</t></is></c><c r="B2"><v>12</v></c></row></sheetData></worksheet>',
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in content.items():
            item = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, text.encode('utf-8'))
    return stream.getvalue()


def _entry(name, first, size, *, directory=False, active=True):
    encoded = name.encode('utf-16le')
    count = (len(encoded) + 29) // 30
    entry = bytearray(64 + count * 32)
    entry[:2] = bytes((0x85, count + 1))
    struct.pack_into('<H', entry, 4, 0x10 if directory else 0x20)
    entry[32:34], entry[35] = b'\xC0\x03', len(encoded) // 2
    # Names use BMP codepoints; one-to-one uppercase mirrors the demo upcase table.
    upper = ''.join(char.upper() if len(char.upper()) == 1 else char for char in name)
    struct.pack_into('<H', entry, 36, _checksum(upper.encode('utf-16le'), 16, ()))
    struct.pack_into('<Q', entry, 40, size)
    struct.pack_into('<I', entry, 52, first)
    struct.pack_into('<Q', entry, 56, size)
    for index in range(count):
        entry[64 + index * 32] = 0xC1
        chunk = encoded[index * 30:(index + 1) * 30]
        entry[66 + index * 32:66 + index * 32 + len(chunk)] = chunk
    struct.pack_into('<H', entry, 2, _checksum(entry))
    if not active:
        for index in range(0, len(entry), 32):
            entry[index] &= 0x7F
    return entry


def create_demo_image(path):
    """CreateNew a small image; never reads devices or includes user material."""
    path = Path(path).absolute()
    no_reparse(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    no_reparse(path.parent)
    sector, cluster_count, fat_sector, fat_sectors, heap_sector = 512, 512, 24, 5, 29
    data = bytearray((heap_sector + cluster_count) * sector)
    heap = heap_sector * sector
    def fat(cluster, following):
        struct.pack_into('<I', data, fat_sector * sector + cluster * 4, following)
    def put(cluster, payload):
        offset = heap + (cluster - 2) * sector
        data[offset:offset + len(payload)] = payload
    fat(0, 0xFFFFFFF8); fat(1, 0xFFFFFFFF)
    fat(2, 0xFFFFFFFF); fat(3, 0xFFFFFFFF)
    upcase = bytearray()
    for code in range(65536):
        char = chr(code)
        upper = char.upper()
        mapped = ord(upper) if len(upper) == 1 and ord(upper) <= 65535 else code
        upcase.extend(struct.pack('<H', mapped))
    upcase_first, upcase_clusters = 4, len(upcase) // sector
    put(upcase_first, upcase)
    for cluster in range(upcase_first, upcase_first + upcase_clusters):
        fat(cluster, cluster + 1 if cluster + 1 < upcase_first + upcase_clusters else 0xFFFFFFFF)
    next_cluster = upcase_first + upcase_clusters
    readme = ('这是完全虚构的恢复演示资料。\r\n所有文件仅用于软件自检，不代表任何真实业务。\r\n').encode('utf-8')
    xlsx = _spreadsheet()
    readme_cluster = next_cluster
    put(readme_cluster, readme); next_cluster += (len(readme) + sector - 1) // sector
    xlsx_cluster = next_cluster
    put(xlsx_cluster, xlsx); next_cluster += (len(xlsx) + sector - 1) // sector
    history_cluster = next_cluster; next_cluster += 1
    old_cluster = next_cluster; next_cluster += 1
    old = '历史候选演示：身份与内容可能已被复用。'.encode('utf-8')
    put(old_cluster, old)
    put(history_cluster, _entry('旧说明.txt', old_cluster, len(old), active=False))
    # Every occupied cluster is allocated except historical candidates.
    bitmap = bytearray((cluster_count + 7) // 8)
    for cluster in range(2, history_cluster):
        bit = cluster - 2
        bitmap[bit // 8] |= 1 << (bit % 8)
    put(3, bitmap)
    bitmap_entry = bytearray(32); bitmap_entry[0] = 0x81
    struct.pack_into('<I', bitmap_entry, 20, 3)
    struct.pack_into('<Q', bitmap_entry, 24, len(bitmap))
    upcase_entry = bytearray(32); upcase_entry[0] = 0x82
    struct.pack_into('<I', upcase_entry, 4, _checksum(upcase, 32, ()))
    struct.pack_into('<I', upcase_entry, 20, upcase_first)
    struct.pack_into('<Q', upcase_entry, 24, len(upcase))
    label = bytearray(32); label[0:2] = bytes((0x83, 4)); label[2:10] = '恢复演示'.encode('utf-16le')
    root = (bitmap_entry + upcase_entry + label + _entry('使用说明.txt', readme_cluster, len(readme))
            + _entry('虚构示例.xlsx', xlsx_cluster, len(xlsx)) + bytes(32)
            + _entry('历史目录', history_cluster, sector, directory=True, active=False))
    put(2, root)
    boot = bytearray(12 * sector)
    boot[0:3], boot[3:11] = b'\xEB\x76\x90', b'EXFAT   '
    struct.pack_into('<Q', boot, 72, len(data) // sector)
    struct.pack_into('<IIII', boot, 80, fat_sector, fat_sectors, heap_sector, cluster_count)
    struct.pack_into('<IIH', boot, 96, 2, 0x1234ABCD, 0x0100)
    boot[108:113] = bytes((9, 0, 1, 0x80, (history_cluster - 2) * 100 // cluster_count))
    boot[510:512] = b'\x55\xAA'
    for index in range(1, 11):
        boot[(index + 1) * sector - 4:(index + 1) * sector] = b'\x00\x00\x55\xAA'
    boot[11 * sector:] = struct.pack('<I', _checksum(boot[:11 * sector], 32, (106, 107, 112))) * (sector // 4)
    data[:12 * sector], data[12 * sector:24 * sector] = boot, boot
    with path.open('xb') as output:
        output.write(data)
    return path
