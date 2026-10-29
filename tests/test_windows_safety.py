import copy
import unittest
from pathlib import Path

from usb_recovery.windows_devices import (
    SafetyError, cygwin_device_path, device_fingerprint, guard_destination,
    matching_device, validate_device,
)


def sample_device():
    return dict(id='disk-8-part-1', disk_number=8, partition_number=1,
                drive_letter='T', source_kind='volume', size=16*1024*1024,
                offset=1024*1024, disk_size=32*1024*1024, unique_id='SYNTHETIC-USB-ID',
                serial='DEMO123', model='Synthetic USB', bus_type='USB',
                is_boot=False, is_system=False, logical_sector_size=512)


class SourceIdentityTests(unittest.TestCase):
    def test_reject_internal_boot_and_malformed_sources(self):
        for key, value in [('bus_type', 'SATA'), ('is_boot', True), ('is_system', True),
                           ('disk_number', -1), ('size', 0), ('drive_letter', 'T:evil')]:
            with self.subTest(key=key):
                item=sample_device(); item[key]=value
                with self.assertRaises(SafetyError): validate_device(item)

    def test_resume_rejects_replaced_device_even_with_same_letter(self):
        saved=sample_device(); current=copy.deepcopy(saved); current['unique_id']='OTHER-USB'
        with self.assertRaises(SafetyError): matching_device(saved, [current])

    def test_resume_rejects_size_and_partition_changes(self):
        saved=sample_device()
        for key, value in [('size', saved['size']+512), ('offset', 2*1024*1024), ('disk_number', 9)]:
            other=copy.deepcopy(saved); other[key]=value
            with self.subTest(key=key), self.assertRaises(SafetyError): matching_device(saved,[other])

    def test_identity_is_stable_and_requires_unique_match(self):
        item=sample_device()
        self.assertEqual(device_fingerprint(item),device_fingerprint(copy.deepcopy(item)))
        self.assertEqual(matching_device(item,[item])['unique_id'],item['unique_id'])
        with self.assertRaises(SafetyError): matching_device(item,[item,copy.deepcopy(item)])

    def test_cygwin_names_have_no_user_supplied_path(self):
        item=sample_device(); self.assertEqual(cygwin_device_path(item),'/dev/sdi1')
        item.update(source_kind='physical',partition_number=None,drive_letter=None,offset=0,size=item['disk_size'])
        for number,suffix in [(0,'a'),(25,'z'),(26,'aa')]:
            item['disk_number']=number
            self.assertEqual(cygwin_device_path(item),'/dev/sd'+suffix)


class DestinationTests(unittest.TestCase):
    def info(self, **updates):
        result=dict(disk_numbers=[3],filesystem='NTFS',free_bytes=1024**3,
                    canonical_path=str(Path.cwd()),has_reparse=False)
        result.update(updates);return result

    def test_same_physical_disk_rejected_even_with_different_path(self):
        with self.assertRaises(SafetyError):
            guard_destination(Path.cwd(),sample_device(),required_bytes=100,
                              inspector=lambda _: self.info(disk_numbers=[8]))

    def test_unproven_or_linked_output_rejected(self):
        for info in [self.info(disk_numbers=[]),self.info(has_reparse=True)]:
            with self.subTest(info=info),self.assertRaises(SafetyError):
                guard_destination(Path.cwd(),sample_device(),required_bytes=100,inspector=lambda _:info)

    def test_space_and_fat32_limits_checked(self):
        for info,needed in [(self.info(free_bytes=1),100),(self.info(filesystem='FAT32',free_bytes=20*1024**3),5*1024**3)]:
            with self.subTest(info=info),self.assertRaises(SafetyError):
                guard_destination(Path.cwd(),sample_device(),required_bytes=needed,inspector=lambda _:info)

    def test_different_proven_disk_is_allowed(self):
        result=guard_destination(Path.cwd(),sample_device(),required_bytes=100,inspector=lambda _:self.info())
        self.assertEqual(result['disk_numbers'],[3])


if __name__=='__main__': unittest.main()
