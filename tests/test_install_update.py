"""install_update.py safety checks against a synthetic robot volume. No device, no vendor files."""
import hashlib
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import build_patch as bp
import install_update as iu


def sha(data):
    return hashlib.sha256(data).hexdigest()


def csv(name, version, image):
    return (f'firmware_name=,{name},firmware_vervion=,{version},firmware_size=,{len(image)},'
            f'firmware_crc32=,{zlib.crc32(image)}\n').encode()


class Rig:
    """A fake robot volume, a backup of it, and a build folder, all in a temp dir."""

    def __init__(self, root):
        self.root = Path(root)
        self.volume, self.backup, self.build = (self.root / n for n in ('vol', 'backup', 'build'))
        self.images = {
            'main': (b'MAIN-STOCK' * 8, b'MAIN-PATCHED' * 8, 'mtiny_car_master_mcu', '28.01.005.008', '28.01.005.016'),
            'radio': (b'RADIO-STOCK' * 8, b'RADIO-PATCHED' * 8, 'mtiny_car_ble_mcu', '28.01.001.002', '28.01.001.006'),
        }
        entries = []
        for part, rel in iu.COMPONENTS.items():
            stock, patched, name, old, new = self.images[part]
            for label, folder in (('stock', (self.volume, self.backup)), ('patched', (self.build / 'overlay',))):
                image, version = (stock, old) if label == 'stock' else (patched, new)
                for base in folder:
                    (base / rel).mkdir(parents=True, exist_ok=True)
                    (base / rel / bp.BIN).write_bytes(image)
                    (base / rel / bp.CSV).write_bytes(csv(name, version, image))
        other = self.volume / 'system/common/audio/a.mp3'
        other.parent.mkdir(parents=True)
        other.write_bytes(b'mp3')
        (self.backup / 'system/common/audio').mkdir(parents=True)
        (self.backup / 'system/common/audio/a.mp3').write_bytes(b'mp3')
        for path in sorted(p for p in self.backup.rglob('*') if p.is_file()):
            entries.append({'path': str(path.relative_to(self.backup)), 'bytes': path.stat().st_size,
                            'sha256': sha(path.read_bytes())})
        (self.backup / 'manifest.json').write_text(json.dumps(entries))
        (self.build / 'manifest.json').write_text(json.dumps({'untested_build': False}))

    def patches(self):
        def stock(part, index):
            return {bp.BIN: sha(self.images[part][0]),
                    bp.CSV: sha(csv(self.images[part][2], self.images[part][3], self.images[part][0]))}[index]
        outputs = {part: {n: sha((self.build / 'overlay' / rel / n).read_bytes()) for n in iu.FILES}
                   for part, rel in iu.COMPONENTS.items()}
        return [patch.object(bp, 'OUTPUTS', outputs),
                patch.object(bp, 'MAIN_STOCK', {n: stock('main', n) for n in iu.FILES}),
                patch.object(bp, 'RADIO_STOCK', {n: stock('radio', n) for n in iu.FILES})]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rig = Rig(self.tmp.name)
        for p in self.rig.patches():
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_preflight(self, component, rollback=False):
        return iu.preflight(self.rig.volume, self.rig.backup, self.rig.build, component, rollback, log=lambda *_: None)

    def live(self, part, name=bp.BIN):
        return self.rig.volume / iu.COMPONENTS[part] / name

    def test_radio_first_then_main(self):
        writes, expected = self.run_preflight('radio')
        self.assertEqual(sorted(writes), sorted(str(iu.COMPONENTS['radio'] / n) for n in iu.FILES))
        with self.assertRaises(SystemExit) as caught:  # main requires the patched radio to be installed
            self.run_preflight('main')
        self.assertIn('radio first', str(caught.exception))
        for relative, data in writes.items():
            iu.write_verified(self.rig.volume, relative, data)
        self.assertEqual(self.live('radio').read_bytes(), self.rig.images['radio'][1])
        writes, _ = self.run_preflight('main')
        self.assertEqual(len(writes), 2)

    def test_changed_robot_file_is_refused(self):
        (self.rig.volume / 'system/common/audio/a.mp3').write_bytes(b'tampered')
        with self.assertRaises(SystemExit):
            self.run_preflight('radio')

    def test_already_patched_target_is_refused_for_install_but_allowed_for_rollback(self):
        self.live('radio').write_bytes(self.rig.images['radio'][1])
        self.live('radio', bp.CSV).write_bytes((self.rig.build / 'overlay' / iu.COMPONENTS['radio'] / bp.CSV).read_bytes())
        with self.assertRaises(SystemExit):
            self.run_preflight('radio')
        writes, _ = self.run_preflight('radio', rollback=True)
        self.assertEqual(writes[str(iu.COMPONENTS['radio'] / bp.BIN)], self.rig.images['radio'][0])

    def test_rollback_of_unpatched_robot_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_preflight('radio', rollback=True)

    def test_wrong_build_output_is_refused(self):
        (self.rig.build / 'overlay' / iu.COMPONENTS['radio'] / bp.BIN).write_bytes(b'something else')
        with self.assertRaises(SystemExit):
            self.run_preflight('radio')

    def test_metadata_must_match_image(self):
        iu.check_metadata(*(self.rig.images['radio'][0], csv('mtiny_car_ble_mcu', 'v', self.rig.images['radio'][0]), 'radio'))
        with self.assertRaises(SystemExit):
            iu.check_metadata(b'other image', csv('mtiny_car_ble_mcu', 'v', self.rig.images['radio'][0]), 'radio')

    def test_leftover_temporary_file_is_refused(self):
        (self.live('radio').with_name('.mtiny-update-mcu_firmware.bin.tmp')).write_bytes(b'x')
        with self.assertRaises(SystemExit):
            self.run_preflight('radio')

    def test_backup_of_modified_robot_is_refused(self):
        entries = json.loads((self.rig.backup / 'manifest.json').read_text())
        for entry in entries:
            if entry['path'].endswith('car_ble_mcu_firmware/mcu_firmware.bin'):
                entry['sha256'] = '0' * 64
        (self.rig.backup / 'manifest.json').write_text(json.dumps(entries))
        with self.assertRaises(SystemExit):
            self.run_preflight('radio')

    def test_unsafe_backup_paths_are_refused(self):
        (self.rig.backup / 'manifest.json').write_text(json.dumps([{'path': '../x', 'bytes': 0, 'sha256': '0' * 64}]))
        with self.assertRaises(SystemExit):
            iu.load_backup(self.rig.backup)


class VolumeTests(unittest.TestCase):
    good = dict(BusProtocol='USB', Internal=False, Writable=True, MountPoint='/Volumes/X', TotalSize=iu.EXPECTED_VOLUME_BYTES)

    def test_accepts_expected_volume(self):
        iu.check_volume_info(self.good, Path('/Volumes/X'), iu.EXPECTED_VOLUME_BYTES)

    def test_rejects_wrong_disks(self):
        for change in (dict(BusProtocol='SATA'), dict(Internal=True), dict(Writable=False),
                       dict(MountPoint='/Volumes/Y'), dict(TotalSize=1)):
            with self.assertRaises(SystemExit):
                iu.check_volume_info(dict(self.good, **change), Path('/Volumes/X'), iu.EXPECTED_VOLUME_BYTES)

    def test_size_check_can_be_skipped_explicitly(self):
        iu.check_volume_info(dict(self.good, TotalSize=5), Path('/Volumes/X'), 0)


if __name__ == '__main__':
    unittest.main()
