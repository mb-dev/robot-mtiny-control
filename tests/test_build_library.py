"""Library generator helpers that need no vendor files."""
import tempfile
import unittest
from pathlib import Path

import build_library as bl
import sound_labels


class LibraryTests(unittest.TestCase):
    def test_eye_names(self):
        recipes = [dict(id=2, frames=[dict(key='IDL0', ticks=8)] * 3),
                   dict(id=1, frames=[dict(key='IDL1', ticks=8), dict(key='TIR5', ticks=2), dict(key='HAP2', ticks=2)])]
        self.assertEqual(bl.eye_entries(recipes), [dict(id=2, name='idle', frames=3),
                                                   dict(id=1, name='tired + happy', frames=3)])

    def test_cards_cover_distinct_moves_and_valid_ids(self):
        self.assertEqual(len({c['move'] for c in bl.CARDS}), len(bl.CARDS))
        self.assertTrue(all(0 <= c['eyes'] <= 127 and c['sound'].startswith('system/') for c in bl.CARDS))

    def test_stock_moves_refuse_unknown_firmware(self):
        with self.assertRaises(SystemExit):
            bl.build_stock_moves(b'\0' * 1000)

    def test_package_folder_without_system_is_refused(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as scratch:
            with self.assertRaises(SystemExit):
                bl.Package(Path(folder), scratch)

    def test_code_labels(self):
        self.assertEqual(sound_labels.code_label('x', 0x84D), 'Note G3')
        self.assertEqual(sound_labels.code_label('x', 0xA01), 'Do (long)')
        self.assertIsNone(sound_labels.code_label('x', 0x1))


if __name__ == '__main__':
    unittest.main()
