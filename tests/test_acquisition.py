import unittest
from pathlib import Path

from usb_recovery.acquisition import build_command, summarize_map


class AcquisitionArgumentsTests(unittest.TestCase):
    def test_conservative_read_only_copy_arguments(self):
        command=build_command(Path('ddrescue.exe'), '/dev/sdb1', '/out/image.img', '/out/image.map',
                              1024*1024,512,1024*1024)
        self.assertEqual(command[-4:], ['--','/dev/sdb1','/out/image.img','/out/image.map'])
        for forbidden in ('--force','--truncate','--same-file','--fill-mode','--reopen-on-error'):
            self.assertNotIn(forbidden,command)
        for required in ('--no-trim','--no-sweep','--no-scrape','--retry-passes=0','--max-read-errors=2'):
            self.assertIn(required,command)

    def test_invalid_or_unbounded_sizes_rejected(self):
        for size in (0,-1,2**63):
            with self.subTest(size=size),self.assertRaises(ValueError):
                build_command(Path('ddrescue.exe'),'/in','/out','/map',size,512,1048576)

    def test_map_not_file_length_determines_completion(self):
        report=summarize_map('# map\n0x0 ? 1\n0x0 0x200 +\n0x200 0x200 ?\n',1024)
        self.assertEqual(report['copied_bytes'],512)
        self.assertFalse(report['complete'])
        good=summarize_map('0x0 0x400 +\n',1024)
        self.assertTrue(good['complete'])

    def test_map_wrong_domain_rejected(self):
        with self.assertRaises(ValueError):summarize_map('0x0 0x200 +\n',1024)


if __name__=='__main__':unittest.main()
