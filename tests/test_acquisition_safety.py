import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usb_recovery.acquisition import AcquisitionRunner, source_identity_matches, JobFileLock
from usb_recovery.windows_devices import SafetyError, device_fingerprint
from tests.test_windows_safety import sample_device

ROOT=Path(__file__).resolve().parents[1]
TEMP=ROOT/'验证'/'临时'


class AcquisitionSafetyTests(unittest.TestCase):
    def test_drive_letter_change_is_not_a_different_device(self):
        a=sample_device();b=copy.deepcopy(a);b['drive_letter']='U';b['label']='new label'
        old=dict(kind='device',fingerprint=device_fingerprint(a),device=a)
        new=dict(kind='device',fingerprint=device_fingerprint(b),device=b)
        self.assertTrue(source_identity_matches(old,new))
        new['fingerprint']='different'
        self.assertFalse(source_identity_matches(old,new))

    def test_existing_event_log_is_not_overwritten_or_partial_image_created(self):
        TEMP.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP,prefix='acq-safety-') as base:
            base=Path(base);source=base/'input.bin';source.write_bytes(b'A'*4096)
            job=base/'job';job.mkdir();events=job/'events.log';events.write_bytes(b'USER ORIGINAL')
            runner=AcquisitionRunner(ROOT/'resources'/'cygwin'/'bin')
            runner.verify_engine=lambda:None
            runner._cygpath=lambda p:str(p)
            with patch('usb_recovery.acquisition.subprocess.Popen',side_effect=AssertionError('must not start')):
                with self.assertRaises(SafetyError):runner.run(source,job)
            self.assertEqual(events.read_bytes(),b'USER ORIGINAL')
            self.assertFalse((job/'source.img').exists())
            self.assertEqual(source.read_bytes(),b'A'*4096)

    def test_one_task_cannot_have_two_imaging_writers(self):
        TEMP.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEMP,prefix='acq-lock-') as base:
            path=Path(base)/'lock'
            with JobFileLock(path):
                with self.assertRaises(SafetyError):
                    with JobFileLock(path):pass
            with JobFileLock(path):pass


if __name__=='__main__':unittest.main()
