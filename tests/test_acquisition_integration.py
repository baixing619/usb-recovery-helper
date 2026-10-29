import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from usb_recovery.acquisition import AcquisitionRunner
from usb_recovery.windows_devices import SafetyError

ROOT=Path(__file__).resolve().parents[1]
ENGINE=ROOT/'resources'/'cygwin'/'bin'
TEMP=ROOT/'验证'/'临时'


@unittest.skipUnless((ENGINE/'ddrescue.exe').is_file(),'verified engine resources not yet prepared')
class NativeAcquisitionTests(unittest.TestCase):
    def test_real_file_stop_resume_and_source_unchanged(self):
        TEMP.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP,prefix='真实引擎 空格-') as base:
            base=Path(base);source=base/'虚构 源文件.bin';job=base/'采集任务';job.mkdir()
            source.write_bytes((b'SYNTHETIC USB DATA\x00\x01\x02'*250000)[:4*1024*1024])
            # Keep the input an exact sector multiple.
            with source.open('r+b') as f:f.truncate(4*1024*1024)
            original=hashlib.sha256(source.read_bytes()).hexdigest()
            cancel=threading.Event();events=[]
            def progress(row):
                events.append(row)
                if row['copied_bytes']>0:cancel.set()
            runner=AcquisitionRunner(ENGINE,max_read_rate=1024*1024)
            first=runner.run(source,job,cancel=cancel,progress=progress)
            self.assertTrue(first['cancelled'])
            self.assertFalse(first['complete'])
            self.assertTrue(first['can_resume'])
            self.assertGreater(first['copied_bytes'],0)
            self.assertLess(first['copied_bytes'],source.stat().st_size)
            second=runner.run(source,job,resume=True)
            self.assertTrue(second['complete'])
            self.assertEqual(second['copied_bytes'],source.stat().st_size)
            self.assertEqual(hashlib.sha256(Path(second['image_path']).read_bytes()).hexdigest(),original)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),original)

    def test_source_file_change_blocks_resume(self):
        TEMP.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP,prefix='source-change-') as base:
            base=Path(base);source=base/'source.bin';source.write_bytes(b'A'*(4*1024*1024));job=base/'job';job.mkdir()
            cancel=threading.Event()
            def progress(row):
                if row['copied_bytes']>0:cancel.set()
            runner=AcquisitionRunner(ENGINE,max_read_rate=1024*1024)
            first=runner.run(source,job,cancel=cancel,progress=progress)
            with source.open('r+b') as f:f.write(b'CHANGED')
            before=Path(first['image_path']).read_bytes()
            with self.assertRaises(SafetyError):runner.run(source,job,resume=True)
            self.assertEqual(Path(first['image_path']).read_bytes(),before)


if __name__=='__main__':unittest.main()
