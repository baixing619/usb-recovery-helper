"""Synthetic documents only; scratch data stays in the project verification tree."""
import hashlib
import io
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from usb_recovery.engine import office

TEMP = Path(__file__).resolve().parents[1] / '验证' / '临时'
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
P = 'http://schemas.openxmlformats.org/package/2006/relationships'
C = 'http://schemas.openxmlformats.org/package/2006/content-types'


def package_parts(cells='<c r="A1"><v>7</v></c>', dimension='A1', merge=''):
    return {
        '[Content_Types].xml': f'<Types xmlns="{C}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'.encode(),
        '_rels/.rels': f'<Relationships xmlns="{P}"><Relationship Id="root" Type="{R}/officeDocument" Target="xl/workbook.xml"/></Relationships>'.encode(),
        'xl/workbook.xml': f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="Example" sheetId="1" r:id="s1"/></sheets></workbook>'.encode(),
        'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="{P}"><Relationship Id="s1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'.encode(),
        'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{S}"><dimension ref="{dimension}"/><sheetData><row r="1">{cells}</row></sheetData>{merge}</worksheet>'.encode(),
    }


def zip_bytes(parts=None, prefix=b'', compression=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO(prefix)
    stream.seek(0, 2)
    with zipfile.ZipFile(stream, 'w', compression=compression) as archive:
        for name, data in (package_parts() if parts is None else parts).items():
            archive.writestr(name, data)
    return stream.getvalue()


def ole_workbook():
    """Minimal CFB v3 with a genuine global/worksheet BIFF8 record sequence."""
    def record(tag, data=b''):
        return struct.pack('<HH', tag, len(data)) + data
    bof = lambda kind: record(0x809, struct.pack('<HHHHII', 0x600, kind, 0, 0, 0, 0))
    bound = struct.pack('<IBBBB', 42, 0, 0, 6, 0) + b'Sheet1'
    workbook = bof(5) + record(0x85, bound) + record(10) + bof(0x10) + record(10)
    workbook = workbook.ljust(4096, b'\0')
    head = bytearray(512)
    head[:8] = bytes.fromhex('D0CF11E0A1B11AE1')
    struct.pack_into('<HHHH', head, 24, 0x3e, 3, 0xfffe, 9)
    struct.pack_into('<H', head, 32, 6)
    struct.pack_into('<IIIIIIIII', head, 40, 0, 1, 1, 0, 4096, 0xfffffffe, 0, 0xfffffffe, 0)
    struct.pack_into('<109I', head, 76, 0, *([0xffffffff] * 108))
    fat = [0xfffffffd, 0xfffffffe] + list(range(3, 10)) + [0xfffffffe] + [0xffffffff] * 118
    directory = bytearray(512)
    for offset, name, kind, child, sector, length in [(0, 'Root Entry', 5, 1, 0xfffffffe, 0), (128, 'Workbook', 2, 0xffffffff, 2, 4096)]:
        encoded = (name + '\0').encode('utf-16le')
        directory[offset:offset + len(encoded)] = encoded
        struct.pack_into('<HBBIII', directory, offset + 64, len(encoded), kind, 1, 0xffffffff, 0xffffffff, child)
        struct.pack_into('<IQ', directory, offset + 116, sector, length)
    return bytes(head) + struct.pack('<128I', *fat) + bytes(directory) + workbook


class OfficeTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='office-', dir=TEMP)
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def source(self, data=None, name='example.xlsx'):
        source = self.root / name
        source.write_bytes(zip_bytes() if data is None else data)
        return source

    def test_sparse_format_dimension_checks_actual_cells(self):
        path = self.source(zip_bytes(package_parts(dimension='A1:XFD1048576')))
        result = office.inspect_document(path)
        self.assertEqual(result['status'], 'structure_checked')
        self.assertEqual(result['actual_cells'], 1)

    def test_wrong_extension_and_zip_preamble_are_detected(self):
        result = office.inspect_document(self.source(zip_bytes(prefix=b'generic preamble'), 'example.xls'))
        self.assertEqual(result['status'], 'structure_checked')
        self.assertEqual(result['extension'], '.xlsx')
        self.assertTrue(result['extension_mismatch'])

    def test_bad_crc_is_never_structure_checked(self):
        raw = bytearray(zip_bytes(compression=zipfile.ZIP_STORED))
        offset = raw.index(b'<worksheet')
        raw[offset + 2] ^= 1
        result = office.inspect_document(self.source(raw))
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any('CRC' in x for x in result['warnings']))

    def test_missing_shared_string_is_not_invented_and_formula_cache_is_preserved(self):
        parts = package_parts('<c r="A1" t="s"><v>17</v></c><c r="B1"><f>SUM(C1:C2)</f><v>123.50</v></c><c r="C1"/>')
        source = self.source(zip_bytes(parts))
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        result = office.salvage_document(source, self.root / 'out')
        self.assertFalse(result['candidates'])
        view = Path(result['partial_view'])
        html = view.read_text('utf-8')
        self.assertIn('缺失共享文本', html)
        self.assertIn('SUM(C1:C2)', html)
        self.assertIn('123.50', html)
        self.assertIn('A1', html)
        self.assertIn('无值节点', html)
        self.assertNotIn('<script', html.lower())
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_unused_dangling_content_type_only_changes_metadata(self):
        parts = package_parts()
        parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace(b'</Types>', b'<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
        source = self.source(zip_bytes(parts))
        self.assertEqual(office.inspect_document(source)['status'], 'partial')
        result = office.salvage_document(source, self.root / 'out')
        self.assertEqual(len(result['candidates']), 1)
        candidate = result['candidates'][0]
        self.assertEqual(candidate['status'], 'repaired_candidate')
        with zipfile.ZipFile(candidate['path']) as archive:
            for name in parts:
                if name != '[Content_Types].xml':
                    self.assertEqual(archive.read(name), parts[name])
        self.assertEqual(office.inspect_document(candidate['path'])['status'], 'structure_checked')

    def test_missing_central_directory_rebuilds_from_original_crc_parts(self):
        raw = zip_bytes()
        truncated = raw[:raw.index(b'PK\x01\x02')]
        result = office.salvage_document(self.source(truncated), self.root / 'out')
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['inferred_relationships'], False)

    def test_damaged_local_header_recovers_only_with_original_central_crc(self):
        raw = bytearray(zip_bytes())
        raw[:4] = b'BAD!'
        result = office.salvage_document(self.source(raw), self.root / 'out')
        self.assertEqual(len(result['candidates']), 1, result)
        with zipfile.ZipFile(result['candidates'][0]['path']) as archive:
            self.assertEqual(archive.read('[Content_Types].xml'), package_parts()['[Content_Types].xml'])

    def test_missing_sheet_relationship_is_explicitly_an_inferred_candidate(self):
        parts = package_parts()
        del parts['xl/_rels/workbook.xml.rels']
        result = office.salvage_document(self.source(zip_bytes(parts)), self.root / 'out')
        self.assertEqual(len(result['candidates']), 1)
        self.assertTrue(result['candidates'][0]['inferred_relationships'])

    def test_large_merge_and_actual_cell_limits_are_bounded(self):
        path = self.source(zip_bytes(package_parts(merge='<mergeCells><mergeCell ref="A1:XFD1048576"/></mergeCells>')))
        self.assertNotEqual(office.inspect_document(path)['status'], 'structure_checked')
        with patch.object(office, 'MAX_CELLS', 1):
            path2 = self.source(zip_bytes(package_parts('<c r="A1"/><c r="B1"/>')), 'two.xlsx')
            self.assertNotEqual(office.inspect_document(path2)['status'], 'structure_checked')

    def test_entity_and_traversal_members_are_rejected(self):
        for index, extra in enumerate([{'../escape.xml': b'<x/>'}, {'evil.xml': b'<!DOCTYPE x [<!ENTITY a "bad">]><x>&a;</x>'}]):
            parts = package_parts()
            parts.update(extra)
            result = office.inspect_document(self.source(zip_bytes(parts), f'bad{index}.xlsx'))
            self.assertNotEqual(result['status'], 'structure_checked')

    def test_html_escapes_source_text_and_never_has_external_resources(self):
        parts = package_parts('<c r="A1" t="inlineStr"><is><t>&lt;script src="https://example.invalid"&gt;&lt;/script&gt;</t></is></c>')
        del parts['xl/workbook.xml']
        result = office.salvage_document(self.source(zip_bytes(parts)), self.root / 'out')
        text = Path(result['partial_view']).read_text('utf-8')
        self.assertIn('&lt;script', text)
        self.assertNotIn('<script', text)
        self.assertNotIn('src="https:', text)

    def test_existing_outputs_are_not_overwritten(self):
        parts = package_parts('<c r="A1" t="s"><v>0</v></c>')
        source = self.source(zip_bytes(parts))
        first = office.salvage_document(source, self.root / 'out')
        before = Path(first['partial_view']).read_bytes()
        second = office.salvage_document(source, self.root / 'out')
        self.assertTrue(second['warnings'])
        self.assertEqual(before, Path(first['partial_view']).read_bytes())

    def test_cancelled_salvage_does_not_create_results(self):
        result = office.salvage_document(self.source(), self.root / 'out', cancel=lambda: True)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['candidates'], [])

    def test_temporary_office_lock_file_is_classified(self):
        self.assertEqual(office.inspect_document(self.source(b'lock', '~$example.xlsx'))['status'], 'temporary')

    def test_valid_ole_biff_and_truncated_record(self):
        raw = ole_workbook()
        result = office.inspect_document(self.source(raw, 'example.xls'))
        self.assertEqual(result['status'], 'structure_checked', result)
        self.assertEqual(result['sheet_streams_checked'], 1)
        damaged = bytearray(raw)
        struct.pack_into('<H', damaged, 1536 + 42 + 2, 65535)
        self.assertNotEqual(office.inspect_document(self.source(damaged, 'bad.xls'))['status'], 'structure_checked')

    def test_sheet_relation_cannot_claim_a_workbook_is_a_worksheet(self):
        parts = package_parts()
        del parts['xl/worksheets/sheet1.xml']
        parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace(b'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>', b'')
        parts['xl/_rels/workbook.xml.rels'] = parts['xl/_rels/workbook.xml.rels'].replace(b'worksheets/sheet1.xml', b'workbook.xml')
        self.assertNotEqual(office.inspect_document(self.source(zip_bytes(parts)))['status'], 'structure_checked')

    def test_main_part_needs_correct_office_content_type(self):
        parts = package_parts()
        parts['[Content_Types].xml'] = parts['[Content_Types].xml'].replace(b'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml', b'application/xml')
        self.assertNotEqual(office.inspect_document(self.source(zip_bytes(parts)))['status'], 'structure_checked')

    def test_ole_header_does_not_claim_legacy_word_content_fully_checked(self):
        raw = bytearray(ole_workbook())
        name = ('WordDocument\0').encode('utf-16le')
        raw[1024 + 128:1024 + 128 + 64] = name.ljust(64, b'\0')
        struct.pack_into('<H', raw, 1024 + 128 + 64, len(name))
        result = office.inspect_document(self.source(raw, 'example.doc'))
        self.assertEqual(result['status'], 'unverified')

    def test_real_openpyxl_writer_output_stays_sparse(self):
        import openpyxl
        stream = io.BytesIO()
        book = openpyxl.Workbook()
        sheet = book.active
        sheet['A1'] = 'Example text'
        sheet['B1'] = '=2+3'
        sheet['XFD1048576'].number_format = '0.00'
        book.save(stream)
        book.close()
        result = office.inspect_document(self.source(stream.getvalue()))
        self.assertEqual(result['status'], 'structure_checked', result)
        self.assertEqual(result['actual_cells'], 3)

    def test_duplicate_and_case_colliding_member_names_fail(self):
        for index, name in enumerate(['xl/workbook.xml', 'XL/workbook.xml']):
            stream = io.BytesIO(zip_bytes())
            with zipfile.ZipFile(stream, 'a') as archive:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr(name, b'<x/>')
            self.assertNotEqual(office.inspect_document(self.source(stream.getvalue(), f'dup{index}.xlsx'))['status'], 'structure_checked')

    def test_link_member_and_escaping_relationship_are_rejected(self):
        parts = package_parts()
        parts['xl/_rels/workbook.xml.rels'] = parts['xl/_rels/workbook.xml.rels'].replace(b'worksheets/sheet1.xml', b'../../escape.xml')
        self.assertNotEqual(office.inspect_document(self.source(zip_bytes(parts)))['status'], 'structure_checked')
        stream = io.BytesIO(zip_bytes())
        with zipfile.ZipFile(stream, 'a') as archive:
            info = zipfile.ZipInfo('link.xml')
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, b'outside')
        self.assertNotEqual(office.inspect_document(self.source(stream.getvalue(), 'link.xlsx'))['status'], 'structure_checked')

    def test_source_and_output_symlinks_are_rejected_when_available(self):
        source = self.source()
        target = self.root / 'alias.xlsx'
        try:
            target.symlink_to(source)
        except OSError:
            self.skipTest('This account cannot create Windows symbolic links')
        self.assertEqual(office.inspect_document(target)['status'], 'unverified')
        out = self.root / 'out'
        out.mkdir()
        alias = self.root / 'alias-out'
        alias.symlink_to(out, target_is_directory=True)
        parts = package_parts('<c r="A1" t="s"><v>0</v></c>')
        result = office.salvage_document(self.source(zip_bytes(parts), 'partial.xlsx'), alias)
        self.assertFalse(result['candidates'])
        self.assertIsNone(result['partial_view'])

    def test_unc_and_device_paths_are_not_opened(self):
        for path in [r'\\server\share\document.xlsx', r'\\.\PhysicalDrive0']:
            result = office.inspect_document(path)
            self.assertEqual(result['status'], 'unverified')
            self.assertTrue(any('本地普通文件' in message for message in result['warnings']))

    def test_xml_node_and_depth_limits_stop_resource_abuse(self):
        path = self.source()
        with patch.object(office, 'MAX_XML_NODES', 3, create=True):
            self.assertNotEqual(office.inspect_document(path)['status'], 'structure_checked')
        with patch.object(office, 'MAX_XML_DEPTH', 2, create=True):
            self.assertNotEqual(office.inspect_document(path)['status'], 'structure_checked')

    def test_partial_word_preserves_only_genuine_paragraph_text(self):
        word_ns = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
        parts = {'word/document.xml': f'<w:document xmlns:w="{word_ns}" xmlns:r="{R}"><w:body><w:p><w:hyperlink r:id="missing"><w:r><w:t>Example paragraph</w:t></w:r></w:hyperlink></w:p></w:body></w:document>'.encode()}
        result = office.salvage_document(self.source(zip_bytes(parts), 'partial.docx'), self.root / 'out')
        self.assertFalse(result['candidates'])
        self.assertIsNotNone(result['partial_view'])
        text = Path(result['partial_view']).read_text('utf-8')
        self.assertIn('Example paragraph', text)
        self.assertIn('word/document.xml', text)

    @unittest.skipUnless(os.name == 'nt', 'Windows output-directory sharing semantics')
    def test_output_parent_is_pinned_during_create_new(self):
        root = self.root / 'out'
        root.mkdir()
        original_open = os.open
        attempts = []
        def racing_open(path, *args, **kwargs):
            try:
                root.rename(self.root / 'moved')
                attempts.append('replaced')
            except OSError:
                attempts.append('blocked')
            return original_open(path, *args, **kwargs)
        with patch.object(office.os, 'open', racing_open):
            try:
                office._write_new(root / 'candidate.bin', b'generic test bytes')
            except FileNotFoundError:
                pass
        self.assertEqual(attempts, ['blocked'])
        self.assertEqual((root / 'candidate.bin').read_bytes(), b'generic test bytes')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction semantics')
    def test_junction_swap_before_pin_cannot_create_an_external_file(self):
        import subprocess
        from usb_recovery.engine import exfat
        root, external = self.root / 'out', self.root / 'external'
        external.mkdir()
        prepare = exfat._prepare_directory
        swaps = []
        def racing_prepare(directory):
            result = prepare(directory)
            if Path(directory) == root and not swaps:
                try:
                    root.rename(self.root / 'moved-output')
                    subprocess.run(['cmd', '/c', 'mklink', '/J', str(root), str(external)], capture_output=True, check=True)
                    swaps.append('junction-created')
                except OSError:
                    swaps.append('blocked')
            return result
        try:
            with patch.object(exfat, '_prepare_directory', racing_prepare):
                try:
                    office._write_new(root / 'candidate.bin', b'generic test bytes')
                except ValueError:
                    self.assertEqual(swaps, ['junction-created'])
            self.assertIn(swaps, [['blocked'], ['junction-created']])
            self.assertEqual(list(external.iterdir()), [])
        finally:
            if swaps == ['junction-created']:
                root.rmdir()

    def test_output_budget_guard_stops_partial_files_before_creation(self):
        source = self.source(zip_bytes(package_parts('<c r="A1" t="s"><v>17</v></c>')))
        calls = []
        def guard(path, size):
            calls.append((path, size))
            raise ValueError('synthetic output budget reached')
        try:
            result = office.salvage_document(source, self.root / 'out', output_guard=guard)
        except TypeError as exc:
            self.fail(f'Output budget cannot be enforced: {exc}')
        self.assertTrue(calls)
        self.assertTrue(result['output_limited'])
        self.assertFalse(list((self.root / 'out').glob('*')))


if __name__ == '__main__':
    unittest.main()
