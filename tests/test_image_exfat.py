"""Synthetic-only recovery tests. Test artifacts stay inside the project."""
import hashlib
import importlib
import os
from pathlib import Path
import shutil
import struct
import unittest
import uuid
import zlib
from unittest.mock import patch

try:
    image = importlib.import_module('usb_recovery.engine.image')
    exfat = importlib.import_module('usb_recovery.engine.exfat')
except ModuleNotFoundError:
    image = exfat = None

ROOT = Path(__file__).resolve().parents[1]


def checksum(data, bits=16, skip=(2, 3)):
    value = 0
    for index, byte in enumerate(data):
        if index not in skip:
            value = (((value >> 1) | ((value & 1) << (bits - 1))) + byte) & ((1 << bits) - 1)
    return value


def file_set(name, first, size, *, directory=False, active=True, contiguous=True, valid_size=None):
    encoded = name.encode('utf-16le')
    names = (len(encoded) + 29) // 30
    data = bytearray(64 + names * 32)
    data[0:2] = bytes((0x85, 1 + names))
    struct.pack_into('<H', data, 4, 0x10 if directory else 0x20)
    data[32:34] = bytes((0xC0, 3 if contiguous else 1))
    data[35] = len(encoded) // 2
    struct.pack_into('<Q', data, 40, size if valid_size is None else valid_size)
    struct.pack_into('<I', data, 52, first)
    struct.pack_into('<Q', data, 56, size)
    for index in range(names):
        data[64 + index * 32] = 0xC1
        part = encoded[index * 30:(index + 1) * 30]
        data[66 + index * 32:66 + index * 32 + len(part)] = part
    struct.pack_into('<H', data, 2, checksum(data))
    if not active:
        for offset in range(0, len(data), 32):
            data[offset] &= 0x7F
    return data


def make_volume(root_entries=None, *, root_cluster=2):
    sector, clusters, heap = 512, 64, 25 * 512
    data = bytearray(heap + clusters * sector)
    boot = bytearray(12 * sector)
    boot[0:3], boot[3:11] = b'\xEB\x76\x90', b'EXFAT   '
    struct.pack_into('<Q', boot, 72, len(data) // sector)
    struct.pack_into('<IIII', boot, 80, 24, 1, 25, clusters)
    struct.pack_into('<I', boot, 96, root_cluster)
    struct.pack_into('<H', boot, 104, 0x100)
    boot[108:113] = bytes((9, 0, 1, 0x80, 0))
    boot[510:512] = b'\x55\xAA'
    for index in range(1, 11):
        boot[(index + 1) * sector - 4:(index + 1) * sector] = b'\x00\x00\x55\xAA'
    boot[11 * sector:] = struct.pack('<I', checksum(boot[:11 * sector], 32, (106, 107, 112))) * (sector // 4)
    data[:12 * sector], data[12 * sector:24 * sector] = boot, boot
    def fat(cluster, nxt):
        struct.pack_into('<I', data, 24 * sector + cluster * 4, nxt)
    def put(cluster, value):
        offset = heap + (cluster - 2) * sector
        data[offset:offset + len(value)] = value
    for cluster in (root_cluster, 3):
        fat(cluster, 0xFFFFFFFF)
    bitmap = bytearray(32)
    bitmap[0] = 0x81
    struct.pack_into('<I', bitmap, 20, 3)
    struct.pack_into('<Q', bitmap, 24, 8)
    put(3, b'\xFF' * 8)
    put(root_cluster, bitmap + (root_entries or file_set('示例.txt', 4, 5)))
    put(4, b'hello')
    return data, fat, put


class ImageExfatTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(image, 'The read-only image module is not implemented')
        self.assertIsNotNone(exfat, 'The exFAT module is not implemented')
        self.temp = ROOT / '验证' / '临时' / ('image-exfat-' + uuid.uuid4().hex)
        self.temp.mkdir(parents=True)

    def tearDown(self):
        if hasattr(self, 'temp'):
            shutil.rmtree(self.temp)

    def save(self, data, name='测试 镜像.img'):
        path = self.temp / name
        path.write_bytes(data)
        return path

    def test_map_guards_physical_offsets_and_rejects_ambiguous_ranges(self):
        path = self.save(bytes(range(256)) * 8)
        map_path = self.save(b'0x0 0x200 +\n0x200 0x200 ?\n0x400 0x400 +\n', 'saved.map')
        with image.ImageReader(path, map_path, offset=1024, length=512) as reader:
            self.assertEqual(reader.read(0, 512), path.read_bytes()[1024:1536])
            self.assertEqual(reader.base_offset, 1024)
            self.assertEqual(reader.size, 512)
            with self.assertRaises(ValueError):
                reader.read(511, 2)
        with image.ImageReader(path, map_path, offset=256, length=512) as reader:
            with self.assertRaisesRegex(ValueError, 'map|读取|确认'):
                reader.read(0, 512)
        for text in ('0x0 0x200 +\n0x100 0x200 +', '0x200 0x200 +', '0x0 0x0 +'):
            with self.assertRaises(ValueError):
                image.MapRanges(text)

    def test_reader_rejects_device_and_directory_and_honors_cancel(self):
        with self.assertRaises((ValueError, OSError)):
            image.ImageReader(self.temp)
        with self.assertRaises(ValueError):
            image.ImageReader(r'\\.\PhysicalDrive0')
        path = self.save(b'hello')
        with image.ImageReader(path, cancel=lambda: True) as reader:
            with self.assertRaises(image.Cancelled):
                reader.read(0, 1)

    def test_raw_and_mbr_offsets_and_view_extraction_preserve_source(self):
        volume, _, _ = make_volume()
        raw = self.save(volume)
        original = hashlib.sha256(raw.read_bytes()).hexdigest()
        with image.ImageReader(raw) as reader:
            self.assertEqual(image.detect_volumes(reader)[0]['filesystem'], 'exfat')
            result = exfat.inventory(reader)
            self.assertIn('unknown', ' '.join(result['warnings']).lower())
            entry = result['files'][0]
            self.assertEqual((entry['path'], entry['origin'], entry['readable']), ('示例.txt', 'current', True))
            output = exfat.extract_file(reader, entry, self.temp / '结果')
            self.assertEqual(Path(output['path']).read_bytes(), b'hello')
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), original)
        offset = 2048
        disk = bytearray(offset) + volume
        disk[510:512] = b'\x55\xAA'
        disk[450] = 7
        struct.pack_into('<II', disk, 454, offset // 512, len(volume) // 512)
        path = self.save(disk, 'mbr.img')
        with image.ImageReader(path) as reader:
            part = image.detect_volumes(reader)[0]
            self.assertEqual((part['offset'], part['filesystem']), (offset, 'exfat'))
        with image.ImageReader(path, offset=part['offset'], length=part['size']) as reader:
            entry = exfat.inventory(reader)['files'][0]
            self.assertEqual(exfat.extract_file(reader, entry, self.temp / '分区结果')['bytes'], 5)

    def test_gpt_crc_and_partition_bounds_are_checked(self):
        volume, _, _ = make_volume()
        start = 40
        disk = bytearray((start + len(volume) // 512 + 40) * 512)
        disk[start * 512:start * 512 + len(volume)] = volume
        disk[510:512], disk[450] = b'\x55\xAA', 0xEE
        struct.pack_into('<II', disk, 454, 1, len(disk) // 512 - 1)
        entries = bytearray(128 * 4)
        entries[:16] = uuid.UUID('EBD0A0A2-B9E5-4433-87C0-68B6B72699C7').bytes_le
        entries[16:32] = uuid.uuid4().bytes_le
        struct.pack_into('<QQ', entries, 32, start, start + len(volume) // 512 - 1)
        entries[56:60] = '演示'.encode('utf-16le')
        disk[1024:1024 + len(entries)] = entries
        header = bytearray(512)
        header[:8] = b'EFI PART'
        struct.pack_into('<II', header, 8, 0x10000, 92)
        struct.pack_into('<QQQQ', header, 24, 1, len(disk) // 512 - 1, 34, len(disk) // 512 - 34)
        struct.pack_into('<QIII', header, 72, 2, 4, 128, zlib.crc32(entries))
        struct.pack_into('<I', header, 16, zlib.crc32(header[:92]))
        disk[512:1024] = header
        path = self.save(disk)
        with image.ImageReader(path) as reader:
            part = image.detect_volumes(reader)[0]
            self.assertEqual((part['offset'], part['label'], part['filesystem']), (start * 512, '演示', 'exfat'))
        disk[512 + 16] ^= 1
        bad = self.save(disk, 'bad-gpt.img')
        with image.ImageReader(bad) as reader:
            self.assertTrue(any('GPT' in str(v['warnings']) for v in image.detect_volumes(reader)))

    def test_fragmented_chain_and_cycle_and_unread_map(self):
        volume, fat, put = make_volume(file_set('fragment.bin', 4, 600, contiguous=False))
        fat(4, 6); fat(6, 0xFFFFFFFF)
        put(4, b'A' * 512); put(6, b'B' * 88)
        path = self.save(volume)
        with image.ImageReader(path) as reader:
            entry = exfat.inventory(reader)['files'][0]
            self.assertEqual(len(entry['extents']), 2)
            out = exfat.extract_file(reader, entry, self.temp / 'out')
            self.assertEqual(Path(out['path']).read_bytes(), b'A' * 512 + b'B' * 88)
        fat(6, 4)
        path.write_bytes(volume)
        with image.ImageReader(path) as reader:
            entry = exfat.inventory(reader)['files'][0]
            self.assertFalse(entry['readable'])
        fat(6, 0xFFFFFFFF); path.write_bytes(volume)
        gap = 25 * 512 + 4 * 512
        mapping = self.save(f'0x0 0x{gap:x} +\n0x{gap:x} 0x200 ?\n0x{gap+512:x} 0x{len(volume)-gap-512:x} +\n'.encode(), 'gap.map')
        with image.ImageReader(path, mapping) as reader:
            entry = exfat.inventory(reader)['files'][0]
            self.assertFalse(entry['readable'])
            with self.assertRaises(ValueError):
                exfat.extract_file(reader, entry, self.temp / 'blocked')

    def test_history_after_end_marker_and_inactive_directory_descendants(self):
        current = file_set('live.txt', 4, 5)
        historical = file_set('旧目录', 5, 512, directory=True, active=False)
        volume, _, put = make_volume(current + bytes(32) + historical)
        put(5, file_set('六月.xlsx', 6, 4) + bytes(32) + file_set('残留.txt', 7, 3, active=False))
        put(6, b'PK!!'); put(7, b'old')
        path = self.save(volume)
        with image.ImageReader(path) as reader:
            self.assertEqual([e['path'] for e in exfat.inventory(reader)['files']], ['live.txt'])
            result = exfat.inventory(reader, include_deleted=True)
            found = {e['path']: e for e in result['files']}
            self.assertEqual(found['旧目录/六月.xlsx']['origin'], 'historical')
            self.assertEqual(found['旧目录/残留.txt']['origin'], 'historical')
            self.assertTrue(found['旧目录/六月.xlsx']['warnings'])

    def test_output_sanitizes_components_preserves_names_and_never_overwrites(self):
        volume, _, _ = make_volume(file_set('../NUL:bad.txt', 4, 5))
        path = self.save(volume)
        with image.ImageReader(path) as reader:
            entry = exfat.inventory(reader)['files'][0]
            entry['components'] = ('..', 'NUL:bad.txt')
            out1 = exfat.extract_file(reader, entry, self.temp / 'result')
            out2 = exfat.extract_file(reader, entry, self.temp / 'result')
            self.assertNotEqual(out1['path'], out2['path'])
            self.assertTrue(Path(out1['path']).resolve().is_relative_to((self.temp / 'result').resolve()))
            self.assertEqual(out1['original_path'], '../NUL:bad.txt')
            self.assertEqual(Path(out1['path']).read_bytes(), b'hello')
            self.assertTrue(out1['warnings'])

    def test_output_reparse_and_source_replacement_are_refused(self):
        volume, _, _ = make_volume()
        path = self.save(volume)
        with image.ImageReader(path) as reader:
            entry = exfat.inventory(reader)['files'][0]
            entry['components'] = (path.name,)
            out = exfat.extract_file(reader, entry, self.temp)
            self.assertNotEqual(Path(out['path']), path)
        target = self.temp / 'junction'
        # Windows junction creation does not require symbolic-link privileges.
        if os.name == 'nt':
            import subprocess
            external = self.temp / 'external'
            external.mkdir()
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(target), str(external)], capture_output=True)
            if result.returncode:
                self.skipTest('Junction unavailable')
        else:
            target.symlink_to(self.temp, target_is_directory=True)
        with image.ImageReader(path) as reader:
            with self.assertRaises(ValueError):
                exfat.extract_file(reader, entry, target)

    def test_invalid_boot_is_explicit_hypothesis_and_oversized_stream_is_blocked(self):
        volume, _, _ = make_volume(file_set('absurd.bin', 4, (1 << 63)))
        volume[11 * 512] ^= 1
        volume[23 * 512] ^= 1
        path = self.save(volume)
        with image.ImageReader(path) as reader:
            result = exfat.inventory(reader)
            self.assertFalse(result['geometry']['verified'])
            self.assertTrue(result['geometry']['assumptions'])
            self.assertFalse(result['files'][0]['readable'])

    def test_backup_boot_can_identify_and_inventory_a_damaged_raw_volume(self):
        volume, _, _ = make_volume()
        volume[3:11] = b'BROKEN!!'
        path = self.save(volume)
        with image.ImageReader(path) as reader:
            self.assertEqual(image.detect_volumes(reader)[0]['filesystem'], 'exfat')
            result = exfat.inventory(reader)
            self.assertEqual(result['geometry']['boot_source'], 'backup')
            self.assertFalse(result['geometry']['verified'])
            self.assertEqual(result['files'][0]['size'], 5)

    def test_partition_boot_is_not_mistaken_for_a_raw_backup(self):
        volume, _, _ = make_volume()
        offset = 12 * 512
        disk = bytearray(offset) + volume
        disk[510:512], disk[450] = b'\x55\xAA', 7
        struct.pack_into('<II', disk, 454, 12, len(volume) // 512)
        path = self.save(disk)
        with image.ImageReader(path) as reader:
            self.assertEqual(image.detect_volumes(reader)[0]['offset'], offset)

    def test_cancelled_extraction_keeps_explicit_partial_without_replacing_existing_file(self):
        volume, _, _ = make_volume()
        path = self.save(volume)
        with image.ImageReader(path) as reader:
            entry = exfat.inventory(reader)['files'][0]
            root = self.temp / 'out'
            root.mkdir()
            existing = root / (entry['name'] + '.partial')
            existing.write_bytes(b'previous-result')
            reader.cancel = lambda: True
            with self.assertRaises(image.Cancelled):
                exfat.extract_file(reader, entry, root)
            self.assertEqual(existing.read_bytes(), b'previous-result')
            self.assertFalse((root / entry['name']).exists(), 'Incomplete content must not have a completed filename')
            self.assertEqual(sorted(item.name for item in root.iterdir()), [existing.name])

    @unittest.skipUnless(os.name == 'nt', 'Windows directory sharing semantics')
    def test_output_parent_cannot_be_replaced_while_streaming(self):
        volume, _, _ = make_volume()
        path = self.save(volume)
        root = self.temp / 'result'
        with image.ImageReader(path) as reader:
            entry = exfat.inventory(reader)['files'][0]
            original_read = reader.read
            attempts = []
            def racing_read(position, length):
                try:
                    root.rename(self.temp / 'escaped')
                    attempts.append('replaced')
                except OSError:
                    attempts.append('blocked')
                return original_read(position, length)
            reader.read = racing_read
            output = exfat.extract_file(reader, entry, root)
            self.assertEqual(attempts, ['blocked'])
            self.assertEqual(Path(output['path']).read_bytes(), b'hello')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction semantics')
    def test_racing_junction_cannot_create_even_an_empty_file_outside_root(self):
        from unittest.mock import patch
        import subprocess
        volume, _, _ = make_volume()
        path = self.save(volume)
        root, external = self.temp / 'result', self.temp / 'external'
        external.mkdir()
        prepare = exfat._prepare_directory
        swaps = []
        def racing_prepare(directory):
            result = prepare(directory)
            if Path(directory) == root and not swaps:
                try:
                    root.rename(self.temp / 'moved-result')
                    subprocess.run(['cmd', '/c', 'mklink', '/J', str(root), str(external)], capture_output=True, check=True)
                    swaps.append('swapped')
                except OSError:
                    swaps.append('blocked')
            return result
        with image.ImageReader(path) as reader:
            entry = exfat.inventory(reader)['files'][0]
            with patch.object(exfat, '_prepare_directory', racing_prepare):
                try:
                    exfat.extract_file(reader, entry, root)
                except ValueError:
                    pass
        self.assertEqual(list(external.iterdir()), [])

    def test_demo_contains_text_and_valid_office_zip_and_is_create_new(self):
        demo = importlib.import_module('usb_recovery.engine.demo')
        path = self.temp / '演示.img'
        demo.create_demo_image(path)
        import io
        import zipfile
        with image.ImageReader(path) as reader:
            result = exfat.inventory(reader, include_deleted=True)
            self.assertTrue(result['geometry']['verified'])
            files = result['files']
            self.assertTrue(any(entry['path'].endswith('.txt') for entry in files))
            office = next(entry for entry in files if entry['path'].endswith('.xlsx'))
            content = b''.join(reader.read(part['offset'], part['length']) for part in office['extents'])
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn('xl/workbook.xml', archive.namelist())
            self.assertTrue(any(entry['origin'] == 'historical' for entry in files))
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            demo.create_demo_image(path)
        self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(os.name == 'nt', 'Windows drive anchors')
    def test_drive_root_output_is_pinned_without_writing_to_a_real_drive_root(self):
        import ctypes
        from types import SimpleNamespace
        pinned, redirected = [], []
        class NativeCall:
            def __init__(self, action):
                self.action = action
            def __call__(self, *args):
                return self.action(*args)
        def create(path, access, share, *args):
            pinned.append((path, share))
            return 1000 + len(pinned)
        kernel = SimpleNamespace(CreateFileW=NativeCall(create), CloseHandle=NativeCall(lambda handle: True))
        original_open, original_unlink = Path.open, Path.unlink
        drive = Path('D:/')
        def safe_open(path, *args, **kwargs):
            self.assertEqual(path.parent, drive)
            self.assertTrue(path.name.startswith('.recovery-output-guard-'))
            target = self.temp / path.name
            redirected.append(target)
            return original_open(target, *args, **kwargs)
        def safe_unlink(path, *args, **kwargs):
            self.assertEqual(path.parent, drive)
            return original_unlink(self.temp / path.name, *args, **kwargs)
        with patch.object(ctypes, 'WinDLL', return_value=kernel), patch.object(exfat, 'no_reparse'), \
                patch.object(Path, 'open', safe_open), patch.object(Path, 'unlink', safe_unlink):
            with exfat._output_tree(drive, []) as (root, parent):
                self.assertEqual((root, parent), (drive, drive))
        self.assertTrue(pinned)
        self.assertTrue(all(Path(path) == drive for path, share in pinned))
        self.assertTrue(redirected)
        self.assertFalse(any(path.exists() for path in redirected))

    def test_inventory_has_a_cumulative_extent_budget_and_marks_partial(self):
        volume, fat, put = make_volume(b''.join(file_set(f'cross-{index}.bin', 4, 4 * 512, contiguous=False) for index in range(4)))
        for cluster in range(4, 8):
            fat(cluster, cluster + 1 if cluster < 7 else 0xFFFFFFFF)
        path = self.save(volume)
        with patch.object(exfat, 'MAX_TOTAL_EXTENTS', 8, create=True):
            with image.ImageReader(path) as reader:
                result = exfat.inventory(reader)
        self.assertLessEqual(sum(len(entry['extents']) for entry in result['files']), 8)
        self.assertFalse(result.get('complete', True))
        self.assertTrue(result.get('resource_limited'))
        self.assertTrue(any('extent' in str(warning).lower() for warning in result['warnings']))

    def test_unread_main_boot_probes_backup_or_returns_an_unknown_view(self):
        volume, _, _ = make_volume()
        path = self.save(volume)
        mapping = self.save(f'0x0 0x200 ?\n0x200 0x{len(volume)-512:x} +\n'.encode(), 'unread-main.map')
        with image.ImageReader(path, mapping) as reader:
            parts = image.detect_volumes(reader)
            self.assertEqual(parts[0]['filesystem'], 'exfat')
            self.assertTrue(parts[0]['warnings'])
            self.assertEqual(exfat.inventory(reader)['geometry']['boot_source'], 'backup')
        mapping.write_text(f'0x0 0x{len(volume):x} ?\n', encoding='utf-8')
        with image.ImageReader(path, mapping) as reader:
            parts = image.detect_volumes(reader)
            self.assertEqual((parts[0]['filesystem'], parts[0]['offset'], parts[0]['size']), ('unknown', 0, len(volume)))
            self.assertTrue(parts[0]['warnings'])

    def test_truncated_saved_prefix_can_recover_complete_current_files_without_filling_tail(self):
        from usb_recovery.engine.demo import create_demo_image
        full = self.temp / 'full-demo.img'
        create_demo_image(full)
        data = full.read_bytes()
        saved = 200704
        self.assertGreater(len(data), saved)
        prefix = self.save(data[:saved], 'prefix.img')
        mapping = self.save(f'0x0 0x{saved:x} +\n0x{saved:x} 0x{len(data)-saved:x} ?\n'.encode(), 'prefix.map')
        with image.ImageReader(prefix, mapping) as reader:
            result = exfat.inventory(reader)
            self.assertEqual(reader.size, saved)
            self.assertEqual(len(result['files']), 2)
            self.assertFalse(result['complete'])
            self.assertTrue(result['geometry']['truncated_image'])
            self.assertEqual(result['geometry']['volume_bytes'], len(data))
            self.assertTrue(any('truncat' in str(warning).lower() for warning in result['warnings']))
            for entry in result['files']:
                self.assertTrue(entry['readable'])
                self.assertTrue(all(part['offset'] + part['length'] <= saved for part in entry['extents']))
                output = exfat.extract_file(reader, entry, self.temp / 'prefix-output')
                expected = b''.join(data[part['offset']:part['offset'] + part['length']] for part in entry['extents'])
                self.assertEqual(Path(output['path']).read_bytes(), expected)
            with self.assertRaises(ValueError):
                reader.read(saved, 1)

    def test_truncation_exception_does_not_relax_complete_file_or_non_tail_view_bounds(self):
        from usb_recovery.engine.demo import create_demo_image
        full = self.temp / 'full-demo.img'
        create_demo_image(full)
        data = full.read_bytes()
        saved = 200704
        full_map = self.save(f'0x0 0x{len(data):x} +\n'.encode(), 'full.map')
        # A complete backing file cannot use whole-disk map space to cross the
        # selected partition/view boundary.
        with image.ImageReader(full, full_map, length=saved) as reader:
            with self.assertRaises(ValueError):
                exfat.inventory(reader)
        prefix = self.save(data[:saved], 'prefix.img')
        partial_map = self.save(f'0x0 0x{saved:x} +\n0x{saved:x} 0x{len(data)-saved:x} ?\n'.encode(), 'partial.map')
        # A shorter selected view that does not reach the saved tail is also
        # a real boundary, even when its backing file itself is truncated.
        with image.ImageReader(prefix, partial_map, length=saved - 512) as reader:
            with self.assertRaises(ValueError):
                exfat.inventory(reader)
        with image.ImageReader(prefix) as reader:
            with self.assertRaises(ValueError):
                exfat.inventory(reader)

    @unittest.skipUnless(os.name == 'nt', 'Windows directory sharing semantics')
    def test_output_pins_allow_unrelated_ancestor_sibling_rename_but_keep_directory_chain_fixed(self):
        base = self.temp / 'user-files'
        base.mkdir()
        sibling = base / 'ordinary-user-file.txt'
        sibling.write_bytes(b'ordinary unrelated user content')
        job = base / 'job'
        with exfat._output_tree(job, []) as (root, parent):
            try:
                sibling.rename(base / 'renamed-user-file.txt')
            except OSError as error:
                self.fail(f'Output protection blocked an unrelated sibling rename: {error}')
            self.assertEqual((base / 'renamed-user-file.txt').read_bytes(), b'ordinary unrelated user content')
            for protected in (base, job):
                with self.assertRaises(OSError):
                    protected.rename(protected.with_name(protected.name + '-replaced'))
        self.assertFalse(list(job.glob('.recovery-output-guard-*')))

    @unittest.skipUnless(os.name == 'nt', 'Windows directory sharing semantics')
    def test_guard_is_retained_when_safe_leaf_share_restoration_fails(self):
        import ctypes
        from ctypes import wintypes
        from types import SimpleNamespace
        native = ctypes.WinDLL('kernel32', use_last_error=True)
        native.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        native.CreateFileW.restype = wintypes.HANDLE
        native.CloseHandle.argtypes = [wintypes.HANDLE]
        job = self.temp / 'restore-failure'
        state = {'fail_restore': False, 'restore_attempted': False}
        class NativeCall:
            def __init__(self, action):
                self.action = action
            def __call__(self, *args):
                return self.action(*args)
        def create(path, access, share, *args):
            if state['fail_restore'] and Path(path) == job and share == 1:
                state['restore_attempted'] = True
                ctypes.set_last_error(32)
                return wintypes.HANDLE(-1).value
            return native.CreateFileW(path, access, share, *args)
        proxy = SimpleNamespace(CreateFileW=NativeCall(create), CloseHandle=native.CloseHandle)
        with patch.object(ctypes, 'WinDLL', return_value=proxy):
            with self.assertRaises(OSError):
                with exfat._output_tree(job, []):
                    state['fail_restore'] = True
        self.assertTrue(state['restore_attempted'])
        guards = list(job.glob('.recovery-output-guard-*'))
        self.assertEqual(len(guards), 1, 'Unsafe cleanup must retain the owned guard file')


if __name__ == '__main__':
    unittest.main()
