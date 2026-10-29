"""Controller integration uses fictional files and mocked USB discovery only."""
import hashlib
import json
from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import patch
import uuid

from usb_recovery.controller import Controller, run_recovery_sync
from usb_recovery.engine.demo import create_demo_image
from usb_recovery import reports


ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / '验证' / '临时'


def device():
    return dict(id='test-usb', label='虚构USB', model='Fictional', bus_type='USB',
                source_kind='physical', disk_number=88, partition_number=None, drive_letter=None,
                size=4096, disk_size=4096, offset=0, unique_id='fictional-88', serial='fictional',
                logical_sector_size=512, is_boot=False, is_system=False)


def terminal(controller):
    while True:
        event = controller.events.get(timeout=15)
        if event['type'] in ('result', 'error'):
            return event


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.work = TEMP / ('controller-' + uuid.uuid4().hex)
        self.work.mkdir(parents=True)
        self.image = create_demo_image(self.work / '虚构镜像.img')
        self.output = self.work / '输出 目录'
        self.output.mkdir()

    def test_real_demo_preserves_source_and_reports_unknown_completeness(self):
        before = hashlib.sha256(self.image.read_bytes()).hexdigest()
        result = run_recovery_sync(self.image, self.output, include_history=False)
        self.assertEqual(hashlib.sha256(self.image.read_bytes()).hexdigest(), before)
        self.assertEqual(result['counts']['structure_checked'], 1)
        self.assertGreaterEqual(result['counts']['unverified'], 1)
        self.assertEqual(result['counts']['historical'], 0)
        self.assertEqual(result['image_completeness'], 'unknown')
        self.assertTrue(Path(result['report_path']).is_file())
        self.assertTrue(Path(result['csv_path']).is_file())
        self.assertTrue(Path(result['json_path']).is_file())
        self.assertIn('未进行原生', Path(result['report_path']).read_text(encoding='utf-8'))

    def test_history_and_deep_candidates_never_inflate_checked_count(self):
        result = run_recovery_sync(self.image, self.output, include_history=True, deep=True)
        self.assertEqual(result['counts']['structure_checked'], 1)
        self.assertGreaterEqual(result['counts']['historical'], 1)
        self.assertEqual(sum(result['counts'].values()), len([f for f in result['files'] if not f.get('duplicate_of')]))

    def test_duplicate_content_records_alias_without_second_file(self):
        from usb_recovery.engine import exfat
        original = exfat.inventory
        def aliased(reader, include_deleted=False):
            data = original(reader, include_deleted=include_deleted)
            entry = dict(next(f for f in data['files'] if f['path'].endswith('.xlsx')))
            entry.update(path='另一个名字.xlsx', components=['另一个名字.xlsx'], id='alias')
            data['files'].append(entry)
            return data
        with patch('usb_recovery.controller.exfat.inventory', side_effect=aliased):
            result = run_recovery_sync(self.image, self.output, include_history=False)
        aliases = [f for f in result['files'] if f.get('duplicate_of')]
        self.assertEqual(len(aliases), 1)
        self.assertFalse(aliases[0].get('output_path'))
        self.assertEqual(result['counts']['structure_checked'], 1)

    def test_output_budget_skips_oversize_without_filling_disk(self):
        result = run_recovery_sync(self.image, self.output, max_file_bytes=8)
        self.assertFalse(result['complete'])
        self.assertGreater(result['counts']['partial'], 0)
        self.assertTrue(any('上限' in item for item in result['warnings']))
        self.assertEqual(list(Path(result['job_dir']).glob('files/**/*xlsx')), [])

    def test_map_missing_data_is_not_turned_into_zero_or_complete(self):
        size = self.image.stat().st_size
        record = self.work / '读取记录.map'
        record.write_text(f'0 {size} ?\n', encoding='utf-8')
        result = run_recovery_sync(self.image, self.output, map_path=record, deep=True)
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['structure_checked'], 0)
        self.assertEqual(result['image_completeness'], 'partial')

    def test_image_job_cancel_is_preserved_and_resume_is_explicitly_unsupported(self):
        controller = Controller()
        ready = threading.Event()
        def inventory_wait(reader, include_deleted=False):
            ready.set()
            if not reader.cancel.wait(5):
                raise AssertionError('cancel was not passed to reader')
            from usb_recovery.engine.image import check_cancel
            check_cancel(reader.cancel)
        with patch('usb_recovery.controller.exfat.inventory', side_effect=inventory_wait):
            job = controller.start_recovery(self.image, self.output)
            self.assertTrue(ready.wait(5))
            controller.cancel()
            result = terminal(controller)
        self.assertFalse(result['complete'])
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['can_resume'])
        self.assertTrue(Path(result['report_path']).is_file())
        with self.assertRaisesRegex(ValueError, '重新扫描'):
            controller.resume(job)

    def test_second_job_is_rejected_while_first_is_preparing(self):
        gate, ready = threading.Event(), threading.Event()
        class Runner:
            def run(self, source, output_dir, **kwargs):
                ready.set(); gate.wait(5)
                return dict(image_path=str(output_dir / 'source.img'), map_path=str(output_dir / 'source.map'),
                            complete=False, cancelled=True, copied_bytes=0, expected_bytes=4096, exit_code=1)
        controller = Controller(runner=Runner())
        with patch('usb_recovery.controller.win.enumerate_devices', return_value=[device()]), \
             patch('usb_recovery.controller.win.guard_destination'), \
             patch('usb_recovery.controller.win.check_source_access'):
            controller.start_image(device(), self.output)
            self.assertTrue(ready.wait(5))
            with self.assertRaisesRegex(RuntimeError, '正在'):
                controller.start_recovery(self.image, self.output)
            gate.set()
            terminal(controller)

    def test_capture_start_rechecks_identity_and_refuses_changed_device(self):
        class Runner:
            def run(self, *args, **kwargs):
                raise AssertionError('runner must not be called')
        controller = Controller(runner=Runner())
        changed = device(); changed['unique_id'] = 'different-device'
        with patch('usb_recovery.controller.win.enumerate_devices', return_value=[changed]):
            with self.assertRaisesRegex(ValueError, '身份'):
                controller.start_image(device(), self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_same_source_destination_start_is_rejected_before_any_target_write(self):
        controller = Controller()
        with patch('usb_recovery.controller.win.enumerate_devices', return_value=[device()]), \
             patch('usb_recovery.controller.win.guard_destination', side_effect=ValueError('同一块物理磁盘')):
            with self.assertRaisesRegex(ValueError, '同一块'):
                controller.start_image(device(), self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(controller.status()['running'])

    def test_same_source_destination_resume_preserves_every_original_byte(self):
        job = self.output / 'existing-job'; job.mkdir()
        manifest = dict(schema=1, job_id='synthetic-job', kind='image', source=device(), options={},
                        complete=False, cancelled=True, stage='cancelled', can_resume=True)
        (job / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        (job / 'source.img').write_bytes(b'saved partial image')
        (job / 'source.map').write_text('0 19 +\n19 4077 ?\n', encoding='utf-8')
        (job / 'acquisition.json').write_text('{}', encoding='utf-8')
        before = {path.name: path.read_bytes() for path in job.iterdir()}
        controller = Controller()
        with patch('usb_recovery.controller.win.enumerate_devices', return_value=[device()]), \
             patch('usb_recovery.controller.win.guard_destination', side_effect=ValueError('同一块物理磁盘')):
            with self.assertRaisesRegex(ValueError, '同一块'):
                controller.resume(job)
        after = {path.name: path.read_bytes() for path in job.iterdir()}
        self.assertEqual(after, before)
        self.assertFalse(controller.status()['running'])

    def test_cancel_during_preparation_reaches_runner_without_token_reset(self):
        preparing, allow = threading.Event(), threading.Event()
        seen = []
        def access(_source):
            preparing.set(); allow.wait(5)
        class Runner:
            def run(self, source, output_dir, **kwargs):
                seen.append(kwargs['cancel'].is_set())
                return dict(image_path=str(output_dir / 'source.img'), map_path=str(output_dir / 'source.map'),
                            complete=False, cancelled=True, copied_bytes=0, expected_bytes=4096, exit_code=1)
        controller = Controller(runner=Runner())
        failures = []
        def starting():
            try:
                controller.start_image(device(), self.output)
            except Exception as error:
                failures.append(error)
        with patch('usb_recovery.controller.win.enumerate_devices', return_value=[device()]), \
             patch('usb_recovery.controller.win.guard_destination'), \
             patch('usb_recovery.controller.win.check_source_access', side_effect=access):
            starter = threading.Thread(target=starting)
            starter.start()
            self.assertTrue(preparing.wait(5))
            controller.cancel(); allow.set()
            starter.join(5)
        self.assertFalse(starter.is_alive())
        self.assertEqual(seen, [])
        self.assertEqual(len(failures), 1)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_resume_rechecks_device_identity_before_reading_saved_image(self):
        calls = []
        class Runner:
            def run(self, source, output_dir, **kwargs):
                calls.append(kwargs['resume'])
                image = output_dir / 'source.img'; image.write_bytes(b'fictional')
                record = output_dir / 'source.map'; record.write_text('0 9 +\n9 4087 ?\n', encoding='utf-8')
                (output_dir / 'acquisition.json').write_text('{}', encoding='utf-8')
                return dict(image_path=str(image), map_path=str(record), complete=False, cancelled=True,
                            copied_bytes=9, expected_bytes=4096, exit_code=1, can_resume=True)
        controller = Controller(runner=Runner())
        with patch('usb_recovery.controller.win.enumerate_devices', return_value=[device()]), \
             patch('usb_recovery.controller.win.guard_destination'), \
             patch('usb_recovery.controller.win.check_source_access'):
            job = controller.start_image(device(), self.output)
            terminal(controller)
        original_image = (job / 'source.img').read_bytes()
        changed = device(); changed['unique_id'] = 'replacement'
        with patch('usb_recovery.controller.win.enumerate_devices', return_value=[changed]):
            with self.assertRaisesRegex(ValueError, '身份'):
                controller.resume(job)
        self.assertEqual(calls, [False])
        self.assertEqual((job / 'source.img').read_bytes(), original_image)

    def test_reports_escape_html_and_csv_formula_cells(self):
        data = dict(job_dir=str(self.output), complete=False, cancelled=False, counts={}, warnings=['<script>alert(1)</script>'],
                    files=[dict(original_path='=cmd()', status='unverified', origin='current', warnings=['<img src=x>'])])
        paths = reports.write_reports(self.output, data)
        html = Path(paths['report_path']).read_text(encoding='utf-8')
        csv = Path(paths['csv_path']).read_text(encoding='utf-8-sig')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<img', html)
        self.assertIn("'=cmd()", csv)
        self.assertNotIn('http-equiv="refresh"', html)

    def test_report_is_create_new_and_does_not_overwrite_existing(self):
        data = dict(job_dir=str(self.output), complete=False, files=[], counts={}, warnings=[])
        first = reports.write_reports(self.output, data)
        second = reports.write_reports(self.output, data)
        self.assertNotEqual(first['report_path'], second['report_path'])
        self.assertTrue(Path(first['report_path']).is_file())

    def test_report_failure_keeps_recovered_file_evidence(self):
        real = reports.write_reports
        calls = []
        def once(job, result):
            calls.append(1)
            if len(calls) == 1:
                raise OSError('synthetic report write failure')
            return real(job, result)
        with patch('usb_recovery.controller.reports.write_reports', side_effect=once):
            result = run_recovery_sync(self.image, self.output, include_history=False)
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['structure_checked'], 1)
        self.assertEqual(len([f for f in result['files'] if f.get('output_path')]), 2)
        self.assertTrue(Path(result['report_path']).is_file())

    def test_deep_budget_is_enforced_before_writing_candidate(self):
        result = run_recovery_sync(self.image, self.output, deep=True, max_file_bytes=8)
        self.assertFalse(result['complete'])
        self.assertTrue(result['deep_scan']['output_limited'])
        self.assertEqual(list((Path(result['job_dir']) / 'deep_scan').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
