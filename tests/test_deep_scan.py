import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usb_recovery.engine import deep_scan
from usb_recovery.engine.image import ImageReader
from tests.test_office import TEMP, zip_bytes


class Reader:
    def __init__(self, data):
        self.data, self.size, self.calls = data, len(data), []
    def read(self, position, length):
        self.calls.append((position, length))
        return self.data[position:position + length]


class ScanTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='deep-', dir=TEMP)
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_cross_chunk_eocd_and_tail_candidate_have_exact_bounds(self):
        raw = zip_bytes()
        prefix = b'\0' * (4096 - (len(raw) - 22) % 4096 - 2)
        data = prefix + raw
        with patch.object(deep_scan, 'CHUNK_SIZE', 4096):
            result = deep_scan.scan_office(Reader(data), self.root)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['scanned_bytes'], len(data))
        item = result['files'][0]
        self.assertEqual((item['offset'], item['length']), (len(prefix), len(raw)))
        self.assertEqual(Path(item['path']).read_bytes(), raw)
        self.assertEqual(item['origin'], 'historical_candidate')
        self.assertFalse(item['original_name_known'])

    def test_zip_preamble_is_preserved_in_exact_candidate_range(self):
        raw = zip_bytes(prefix=b'generic preamble bytes')
        data = b'outside' + raw + b'after'
        result = deep_scan.scan_office(Reader(data), self.root)
        self.assertEqual(len(result['files']), 1, result)
        self.assertEqual(result['files'][0]['offset'], 7)
        self.assertEqual(Path(result['files'][0]['path']).read_bytes(), raw)

    def test_duplicate_hash_is_not_exported(self):
        raw = zip_bytes()
        result = deep_scan.scan_office(Reader(raw), self.root, {hashlib.sha256(raw).hexdigest()})
        self.assertEqual(result['files'], [])
        self.assertTrue(result['complete'])

    def test_zero_and_ff_regions_are_actually_read(self):
        reader = Reader(b'\0' * 8192 + b'\xff' * 4096)
        with patch.object(deep_scan, 'CHUNK_SIZE', 1024):
            result = deep_scan.scan_office(reader, self.root)
        self.assertTrue(result['complete'])
        self.assertEqual(sum(length for _, length in reader.calls), reader.size)
        self.assertEqual(result['scanned_bytes'], reader.size)

    def test_cancel_after_progress_retains_incomplete_state(self):
        reader = Reader(b'\0' * 8192)
        state = {'cancel': False}
        def progress(event):
            state['cancel'] = True
        with patch.object(deep_scan, 'CHUNK_SIZE', 1024):
            result = deep_scan.scan_office(reader, self.root, progress=progress, cancel=lambda: state['cancel'])
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['complete'])
        self.assertLess(result['scanned_bytes'], reader.size)

    def test_short_read_and_unread_map_block_are_never_full_coverage(self):
        class Short(Reader):
            def read(self, position, length):
                return self.data[position:position + max(0, length - 1)]
        self.assertFalse(deep_scan.scan_office(Short(b'\0' * 100), self.root)['complete'])
        class Missing(Reader):
            def read(self, position, length):
                raise OSError('unread map range')
        result = deep_scan.scan_office(Missing(b'\0' * 100), self.root)
        self.assertFalse(result['complete'])
        self.assertEqual(result['scanned_bytes'], 0)

    def test_bad_crc_and_resource_limited_candidate_are_not_exported(self):
        raw = bytearray(zip_bytes(compression=0))
        raw[raw.index(b'<worksheet') + 2] ^= 1
        result = deep_scan.scan_office(Reader(raw), self.root)
        self.assertEqual(result['files'], [])
        with patch.object(deep_scan, 'MAX_ARCHIVE_BYTES', 64):
            result = deep_scan.scan_office(Reader(zip_bytes()), self.root)
        self.assertEqual(result['files'], [])
        self.assertFalse(result['complete'])
        self.assertTrue(result['warnings'])

    def test_forged_eocd_member_count_is_not_a_complete_candidate(self):
        import struct
        raw = bytearray(zip_bytes())
        end = raw.rfind(b'PK\x05\x06')
        struct.pack_into('<HH', raw, end + 8, 1, 1)
        result = deep_scan.scan_office(Reader(raw), self.root)
        self.assertEqual(result['files'], [])

    def test_large_candidate_is_read_in_cancelable_chunks(self):
        class Bounded(Reader):
            def read(self, position, length):
                if length > 1024:
                    raise OSError('reader refuses large reads')
                return super().read(position, length)
        raw = zip_bytes(prefix=b'pre' * 1000)
        with patch.object(deep_scan, 'CHUNK_SIZE', 1024):
            result = deep_scan.scan_office(Bounded(raw), self.root)
        self.assertEqual(len(result['files']), 1, result)

    def test_output_budget_guard_marks_scan_incomplete_without_writing(self):
        calls = []
        def guard(path, size):
            calls.append((path, size))
            raise ValueError('synthetic output budget reached')
        try:
            result = deep_scan.scan_office(Reader(zip_bytes()), self.root, output_guard=guard)
        except TypeError as exc:
            self.fail(f'Output budget cannot be enforced: {exc}')
        self.assertTrue(calls)
        self.assertFalse(result['complete'])
        self.assertTrue(result['output_limited'])
        self.assertEqual(result['files'], [])
        self.assertFalse(list(self.root.glob('*')))

    def test_good_office_inside_a_partly_unread_megabyte_is_still_scanned(self):
        raw = zip_bytes()
        data = bytearray(1024 * 1024)
        data[4096:4096 + len(raw)] = raw
        image_path = self.root / 'partial.img'
        map_path = self.root / 'partial.map'
        image_path.write_bytes(data)
        map_path.write_text(f'0x0 0x200 ?\n0x200 0x{len(data)-512:x} +\n', encoding='utf-8')
        with ImageReader(image_path, map_path) as reader:
            result = deep_scan.scan_office(reader, self.root / 'output')
        self.assertEqual(len(result['files']), 1, result)
        self.assertEqual(result['files'][0]['offset'], 4096)
        self.assertEqual(result['scanned_bytes'], len(data) - 512)
        self.assertFalse(result['complete'])

    def test_map_view_offsets_and_adjacent_good_parts_preserve_cross_chunk_signatures(self):
        raw = zip_bytes()
        prefix = 100
        # Put EOCD across a 256-byte scanner boundary starting after the gap.
        before = 256 - (len(raw) - 22) % 256 - 2
        payload = bytes(512 + before) + raw + bytes(100)
        physical = bytes(prefix) + payload
        start = prefix + 512
        split = start + 256
        image_path, map_path = self.root / 'view.img', self.root / 'view.map'
        image_path.write_bytes(physical)
        map_path.write_text(f'0x0 0x{start:x} ?\n0x{start:x} 0x100 +\n0x{split:x} 0x{len(physical)-split:x} +\n', encoding='utf-8')
        with ImageReader(image_path, map_path, offset=prefix, length=len(payload)) as reader:
            with patch.object(deep_scan, 'CHUNK_SIZE', 256):
                result = deep_scan.scan_office(reader, self.root / 'output')
        self.assertEqual(len(result['files']), 1, result)
        self.assertEqual(result['files'][0]['offset'], 512 + before)
        self.assertEqual(result['scanned_bytes'], len(payload) - 512)
        self.assertFalse(result['complete'])

    def test_unknown_gap_is_not_zero_filled_or_joined_into_a_candidate(self):
        raw = zip_bytes()
        data = bytes(4096) + raw + bytes(512)
        image_path, map_path = self.root / 'gap.img', self.root / 'gap.map'
        image_path.write_bytes(data)
        gap = 4096 + len(raw) // 2
        map_path.write_text(f'0x0 0x{gap:x} +\n0x{gap:x} 0x1 ?\n0x{gap+1:x} 0x{len(data)-gap-1:x} +\n', encoding='utf-8')
        with ImageReader(image_path, map_path) as reader:
            result = deep_scan.scan_office(reader, self.root / 'output')
        self.assertEqual(result['files'], [])
        self.assertEqual(result['scanned_bytes'], len(data) - 1)
        self.assertFalse(result['complete'])


if __name__ == '__main__':
    unittest.main()
