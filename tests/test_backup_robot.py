"""backup_robot.py against a synthetic volume (a temp folder). Never touches a real device."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import backup_robot as br


def make_volume(root):
    for rel, text in ((br.RADIO_REL, 'firmware_name=,mtiny_car_ble_mcu,firmware_vervion=,28.01.001.002'),
                      (br.MAIN_REL, 'firmware_name=,mtiny_car_master_mcu,firmware_vervion=,28.01.005.008')):
        (root / rel).mkdir(parents=True)
        (root / rel / 'mcu_firmware_discription.csv').write_text(text)
        (root / rel / 'mcu_firmware.bin').write_bytes(b'\x01\x02' * 10)
    (root / 'system/common/audio').mkdir(parents=True)
    (root / 'system/common/audio/a.mp3').write_bytes(b'abc')
    (root / 'readme.txt').write_text('root file')
    (root / '.hidden').write_text('skipped')


class BackupTests(unittest.TestCase):
    def test_backup_copies_and_hashes_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            volume, out = Path(tmp) / 'vol', Path(tmp) / 'backup'
            volume.mkdir()
            make_volume(volume)
            before = sorted((p.name, p.read_bytes()) for p in volume.rglob('*') if p.is_file())
            entries = br.backup(volume, out)
            self.assertEqual(sorted(sorted(p.name for p in volume.rglob('*') if p.is_file())), [n for n, _ in before])
            self.assertEqual(before, sorted((p.name, p.read_bytes()) for p in volume.rglob('*') if p.is_file()))
            paths = {e['path'] for e in entries}
            self.assertIn('system/common/audio/a.mp3', paths)
            self.assertIn('readme.txt', paths)
            self.assertNotIn('.hidden', paths)
            manifest = json.loads((out / 'manifest.json').read_text())
            for entry in manifest:
                self.assertEqual(hashlib.sha256((out / entry['path']).read_bytes()).hexdigest(), entry['sha256'])
            self.assertEqual(br.firmware_versions(out), {'main': '28.01.005.008', 'radio': '28.01.001.002'})

    def test_non_mtiny_volume_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                br.backup(Path(tmp), Path(tmp) / 'out')
            self.assertFalse((Path(tmp) / 'out').exists())

    def test_existing_backup_folder_is_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            volume, out = Path(tmp) / 'vol', Path(tmp) / 'backup'
            volume.mkdir()
            make_volume(volume)
            out.mkdir()
            with self.assertRaises(FileExistsError):
                br.backup(volume, out)


if __name__ == '__main__':
    unittest.main()
