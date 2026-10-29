import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from usb_recovery.app import self_test

TEMP = Path(__file__).resolve().parents[1] / '验证' / '临时'
TEMP.mkdir(parents=True, exist_ok=True)


class SelfTestSchedulingTests(unittest.TestCase):
    def test_stop_does_not_depend_on_published_map_progress(self):
        class DelayedMapRunner:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, source, output, *, cancel=None, progress=None, resume=False):
                image, mapping = output / 'source.img', output / 'source.map'
                if not resume:
                    for _ in range(4):
                        progress({'copied_bytes': 0, 'has_map': False})
                    return dict(cancelled=cancel.is_set(), complete=not cancel.is_set(), can_resume=cancel.is_set())
                shutil.copyfile(source, image)
                mapping.write_text(f'0x0 {source.stat().st_size:#x} +\n')
                return dict(complete=True, image_path=str(image), map_path=str(mapping), copied_bytes=image.stat().st_size)

        with tempfile.TemporaryDirectory(dir=TEMP) as folder, \
                patch('usb_recovery.app.AcquisitionRunner', DelayedMapRunner), \
                patch('usb_recovery.app.diagnostics', return_value={}), \
                patch('usb_recovery.app.run_recovery_sync', return_value={'counts': {'structure_checked': 1}, 'complete': True}):
            self.assertTrue(self_test(Path(folder))['passed'])
