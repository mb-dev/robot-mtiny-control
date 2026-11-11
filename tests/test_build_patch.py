"""build_patch.py checks that need no vendor files, plus an opt-in byte-identity test that does.

The opt-in test runs only when both of these point at your own copies:
  MTINY_VENDOR_PACKAGE   the vendor .rar (or the extracted folder)
  MTINY_ROBOT_BACKUP     a backup folder made by backup_robot.py
It also needs clang. Otherwise it is skipped.
"""
import hashlib
import os
import shutil
import tempfile
import unittest
import zlib
from pathlib import Path

import build_patch as bp


class PinsTests(unittest.TestCase):
    def test_pins_are_well_formed(self):
        hashes = [bp.PACKAGE_RAR_SHA256, bp.MAIN_LAYER1_SHA256, *bp.PACKAGE_FILES.values(),
                  *bp.MAIN_STOCK.values(), *bp.RADIO_STOCK.values(),
                  *(h for part in bp.OUTPUTS.values() for h in part.values())]
        for value in hashes:
            self.assertRegex(value, r'^[0-9a-f]{64}$')

    def test_appended_code_blocks_are_unchanged(self):
        self.assertEqual((len(bp.MAIN_APPEND), len(bp.RADIO_APPEND)), (596, 376))
        self.assertEqual(hashlib.sha256(bp.MAIN_APPEND).hexdigest(),
                         '408d3c20b7a37f60f959a2652313eed5089e8548f7c2b573641246920e353541')
        self.assertEqual(hashlib.sha256(bp.RADIO_APPEND).hexdigest(),
                         '7ab8fedc7dbb2b57a1554ceb25519e7806e1676f6c175b72e172e44ff07f1a00')

    def test_edits_are_small_and_same_length(self):
        for edits in (bp.MAIN_LAYER1_EDITS, bp.RADIO_EDITS):
            for _offset, old, new in edits:
                self.assertEqual(len(old), len(new))
                self.assertLessEqual(len(old) // 2, 16)  # verification snippets only

    def test_package_files_pin_the_radio_stock(self):
        self.assertEqual(bp.RADIO_STOCK[bp.BIN], bp.PACKAGE_FILES[f'{bp.RADIO_REL}/{bp.BIN}'])


class EncoderTests(unittest.TestCase):
    def test_branch_encodings_match_known_images(self):
        # Bytes found at these addresses in the tested images.
        self.assertEqual(bp.thumb_branch(0x0800bdd0, 0x08032e94, False), bytes.fromhex('27f060b8'))
        self.assertEqual(bp.thumb_branch(0x08018dcc, 0x08033014, False), bytes.fromhex('1af022b9'))
        self.assertEqual(bp.thumb_branch(0x08027346, 0x08028e70, True), bytes.fromhex('01f093fd'))

    def test_branch_range_is_checked(self):
        with self.assertRaises(SystemExit):
            bp.thumb_branch(0, 1 << 26, False)

    def test_apply_edits_checks_old_bytes(self):
        image = bytes(range(16))
        self.assertEqual(bp.apply_edits(image, [(4, '04050607', 'aabbccdd')], 't')[4:8], bytes.fromhex('aabbccdd'))
        with self.assertRaises(SystemExit):
            bp.apply_edits(image, [(4, '00000000', 'aabbccdd')], 't')
        with self.assertRaises(SystemExit):
            bp.apply_edits(image, [(4, '0405', 'aabbccdd')], 't')

    def test_derive_metadata_replaces_each_field_once(self):
        stock = b'\x01' * 20
        image = stock + b'\x02' * 8
        csv = (f'firmware_name=,x,firmware_vervion=,28.01.005.008,firmware_size=,{len(stock)},'
               f'firmware_crc32=,{zlib.crc32(stock)}\n').encode()
        out = bp.derive_metadata(csv, stock, image, '28.01.005.008', '28.01.005.016').decode()
        self.assertIn('28.01.005.016', out)
        self.assertIn(f',{len(image)},', out)
        self.assertIn(str(zlib.crc32(image)), out)
        with self.assertRaises(SystemExit):
            bp.derive_metadata(csv, stock, image, '28.01.005.999', '28.01.005.016')

    def test_elf_reader_rejects_garbage(self):
        with self.assertRaises(SystemExit):
            bp.elf_section(b'not an elf' * 20, '.text.x')


class RefusalTests(unittest.TestCase):
    def test_wrong_package_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(SystemExit):
                bp.load_radio_stock(Path(folder))
            junk = Path(folder) / 'x.rar'
            junk.write_bytes(b'junk')
            with self.assertRaises(SystemExit):
                bp.load_radio_stock(junk)

    def test_wrong_main_firmware_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / bp.MAIN_REL
            target.mkdir(parents=True)
            (target / bp.BIN).write_bytes(b'not the stock image')
            (target / bp.CSV).write_bytes(b'x')
            with self.assertRaises(SystemExit):
                bp.load_main_stock(Path(folder))


@unittest.skipUnless(os.environ.get('MTINY_VENDOR_PACKAGE') and os.environ.get('MTINY_ROBOT_BACKUP')
                     and shutil.which('clang'),
                     'needs your own vendor package and robot backup (see module docstring) and clang')
class ByteIdentityTest(unittest.TestCase):
    def test_outputs_match_pinned_hashes(self):
        with tempfile.TemporaryDirectory() as out:
            result = bp.main(['--package', os.environ['MTINY_VENDOR_PACKAGE'],
                              '--robot-backup', os.environ['MTINY_ROBOT_BACKUP'],
                              '--out', str(Path(out) / 'build')])
            self.assertEqual(result, 0)
            for part, rel in (('main', bp.MAIN_REL), ('radio', bp.RADIO_REL)):
                for name, expected in bp.OUTPUTS[part].items():
                    data = (Path(out) / 'build' / 'overlay' / rel / name).read_bytes()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), expected)


if __name__ == '__main__':
    unittest.main()
