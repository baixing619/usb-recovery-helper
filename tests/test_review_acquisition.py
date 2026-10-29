import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from usb_recovery.acquisition import AcquisitionRunner, _file_identity
from usb_recovery.windows_devices import SafetyError
from tests.test_windows_safety import sample_device

ROOT=Path(__file__).resolve().parents[1]
TEMP=ROOT/'验证'/'临时'
ENGINE=ROOT/'resources'/'cygwin'/'bin'


def paused_fixture(base):
    source=base/'source.bin';source.write_bytes(b'X'*4096)
    job=base/'job';job.mkdir();image=job/'source.img'
    image.write_bytes(b'X'*512+b'\0'*3072+b'X'*512)
    map_data=b'0x0 ? 1\n0x0 0x200 +\n0x200 0xC00 ?\n0xE00 0x200 +\n'
    (job/'source.map').write_bytes(map_data)
    info=image.stat()
    owned={}
    for name in ['source.img','source.map']:
        st=(job/name).stat();owned[name]=[st.st_dev,st.st_ino]
    metadata=dict(schema=1,source=dict(kind='file',identity=_file_identity(source)),expected_bytes=4096,
                  state='cancelled',image_identity=[info.st_dev,info.st_ino],image_stat=[info.st_size,info.st_mtime_ns],
                  map_sha256=hashlib.sha256(map_data).hexdigest(),owned_outputs=owned,
                  source_prefix=dict(bytes=512,sha256=hashlib.sha256(b'X'*512).hexdigest()))
    (job/'acquisition.json').write_text(json.dumps(metadata),encoding='utf-8')
    return source,job,metadata


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):TEMP.mkdir(parents=True,exist_ok=True)

    def test_foreign_full_map_cannot_turn_holes_into_complete_image(self):
        with tempfile.TemporaryDirectory(dir=TEMP,prefix='review-map-') as temp:
            source,job,_=paused_fixture(Path(temp));before=(job/'source.img').read_bytes()
            (job/'source.map').write_text('0x0 + 1\n0x0 0x1000 +\n',encoding='utf-8')
            with self.assertRaisesRegex(SafetyError,'读取记录'):
                AcquisitionRunner(ENGINE).run(source,job,resume=True)
            self.assertEqual((job/'source.img').read_bytes(),before)

    def test_resume_rejects_external_hardlinked_write_paths(self):
        for name in ('events.log','rates.log','source.map.bak'):
            with self.subTest(name=name),tempfile.TemporaryDirectory(dir=TEMP,prefix='review-log-') as temp:
                base=Path(temp);source,job,_=paused_fixture(base)
                outside=base/'outside-user-file.txt';outside.write_bytes(b'USER ORIGINAL')
                os.link(outside,job/name)
                with self.assertRaises(SafetyError):AcquisitionRunner(ENGINE).run(source,job,resume=True)
                self.assertEqual(outside.read_bytes(),b'USER ORIGINAL')

    def test_unclean_exit_has_no_automatic_trust_in_live_map(self):
        with tempfile.TemporaryDirectory(dir=TEMP,prefix='review-crash-') as temp:
            source,job,metadata=paused_fixture(Path(temp));metadata['state']='running'
            (job/'acquisition.json').write_text(json.dumps(metadata),encoding='utf-8')
            before=(job/'source.img').read_bytes()
            with self.assertRaisesRegex(SafetyError,'异常|快照'):
                AcquisitionRunner(ENGINE).run(source,job,resume=True)
            self.assertEqual((job/'source.img').read_bytes(),before)

    def test_runner_guards_device_target_before_any_file_creation(self):
        with tempfile.TemporaryDirectory(dir=TEMP,prefix='review-first-write-') as temp:
            job=Path(temp)/'job';job.mkdir();device=sample_device()
            with patch('usb_recovery.acquisition.enumerate_devices',return_value=[device]), \
                 patch('usb_recovery.acquisition.check_source_access'), \
                 patch('usb_recovery.acquisition.guard_destination',side_effect=SafetyError('same source')):
                with self.assertRaises(SafetyError):AcquisitionRunner(ENGINE).run(device,job)
            self.assertEqual(list(job.iterdir()),[])


if __name__=='__main__':unittest.main()
