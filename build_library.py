#!/usr/bin/env python3
"""Generate the control page's sound, eye and built-in-move library on your own machine.

Nothing this script reads is shipped with the project, and its output (the `library/`
folder) is git-ignored: it contains names, durations and previews derived from
Makeblock's firmware and sound files. The control server and page work without it,
with a reduced interface (type an eye ID or a sound path by hand).

Inputs (both optional; whatever you give is generated):
  --package       the vendor package (.rar or extracted folder), for the sound list
                  and the eye animations (the audio MCU image inside it)
  --robot-backup  a folder made by backup_robot.py, for the robot's built-in moves
                  (read from its factory main firmware)

    python build_library.py --package <package.rar> --robot-backup device-backups/<folder>

Needs `pip install -r requirements-library.txt` (mutagen for durations, Pillow for the
eye preview GIFs; without Pillow the eye list is still generated, just without previews).
Offline; never touches Bluetooth or the robot.
"""
import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import build_patch as bp
import sound_labels
from mtiny_movement import plan_moves

# The audio/display MCU image in the vendor package; the offsets below are specific to it.
MVA_NAME = 'audio_mcu_firmware.MVA'
MVA_SHA256 = '45aaef617e2d10907d44c5d139d4230f2741809da7310a67370937eca84380c6'
# Stock move table in the robot's factory main firmware 28.01.005.008.
MOVE_TABLE, MOVE_COUNT = 0x0802d44c, 17
# Our own findings: the seven "emotion cards" tie an eye animation, a sound clip and a built-in move.
CARDS = [
    dict(name='cry', sound='system/common/audio/card_daku.mp3', eyes=5, move=9),
    dict(name='greet', sound='system/common/audio/card_dazhaohu.mp3', eyes=8, move=0),
    dict(name='sleepy', sound='system/common/audio/card_keshui.mp3', eyes=7, move=7),
    dict(name='angry', sound='system/common/audio/card_shengqi.mp3', eyes=3, move=8),
    dict(name='dizzy', sound='system/common/audio/card_touyun.mp3', eyes=6, move=10),
    dict(name='happy', sound='system/common/audio/card_kaixin.mp3', eyes=4, move=1),
    dict(name='startled', sound='system/common/audio/card_xiayitiao.mp3', eyes=9, move=6),
]
WORDS = dict(IDL='idle', HAP='happy', ANG='angry', TIR='tired', CRY='cry', DIZ='dizzy', SCA='scared', EXC='excited')
KIND = {0: 'forward', 1: 'backward', 2: 'left', 3: 'right'}  # kinds 2/3 are opposite turns


def fail(message):
    raise SystemExit(f'error: {message}')


# ------------------------------------------------------------------- inputs
class Package:
    """Vendor package as a .rar (read with bsdtar) or an extracted folder."""

    def __init__(self, path, scratch):
        self.path, self.scratch = path, scratch
        if path.is_file():
            if hashlib.sha256(path.read_bytes()).hexdigest() != bp.PACKAGE_RAR_SHA256:
                fail(f'{path.name} is not the supported package {bp.PACKAGE_NAME}')
            self.root = None
        else:
            self.root = next((r for r in (path, path / bp.PACKAGE_ROOT, *path.glob(f'*/{bp.PACKAGE_ROOT}'))
                              if (r / 'system').is_dir()), None)
            if self.root is None:
                fail(f'no system/ folder found under {path}')

    def read(self, relative):
        if self.root is not None:
            return (self.root / relative).read_bytes()
        result = subprocess.run(['bsdtar', '-xOf', str(self.path), f'{bp.PACKAGE_ROOT}/{relative}'],
                                capture_output=True)
        if result.returncode or not result.stdout:
            fail(f'could not read {relative} from the archive (bsdtar). Extract it and pass the folder.')
        return result.stdout

    def sound_root(self):
        """A folder containing system/**.mp3 (extracting only the sound files from a .rar)."""
        if self.root is not None:
            return self.root
        folder = Path(self.scratch) / 'package'
        folder.mkdir()
        result = subprocess.run(['bsdtar', '-xf', str(self.path), '-C', str(folder),
                                 '--include', f'{bp.PACKAGE_ROOT}/system/*.mp3'], capture_output=True)
        if result.returncode:
            fail(f'could not extract sounds with bsdtar: {result.stderr.decode(errors="replace")}')
        return folder / bp.PACKAGE_ROOT


# ------------------------------------------------------------------- sounds
def build_sounds(root):
    try:
        from mutagen.mp3 import MP3
    except ImportError:
        fail('mutagen is not installed: pip install -r requirements-library.txt')
    paths = sorted(p.relative_to(root).as_posix() for p in (root / 'system').rglob('*.mp3'))
    if not paths:
        fail(f'no .mp3 files under {root}/system')
    sounds = [dict(path=p, seconds=round(round(MP3(root / p).info.length, 3), 1)) for p in paths]
    found = sound_labels.labels(paths, root)
    for sound in sounds:
        sound.update(found[sound['path']])
    return sounds


# --------------------------------------------------------------------- eyes
def parse_mva(raw):
    """(recipes, frame images) from the audio MCU image: animation recipes and RGB565 eye frames."""
    if hashlib.sha256(raw).hexdigest() != MVA_SHA256:
        fail(f'{MVA_NAME} does not match the supported package (offsets are specific to it)')
    payload = raw[22:]
    descriptor = 0x6E673  # last segment: length and load address; it ends exactly at EOF
    size, flash_base = struct.unpack_from('<II', raw, descriptor)
    start = descriptor + 8
    if start + size != len(raw) or flash_base != 0x100000 or raw[start:start + 4] != b'MVUB':
        fail(f'unexpected {MVA_NAME} layout')
    frames = {}
    for i in range(raw[start + 8]):
        name, address, length = struct.unpack_from('<4sII', raw, start + 9 + i * 12)
        begin = start + address - flash_base
        if length != 128 * 128 * 2 or begin < start or begin + length > len(raw):
            fail('unexpected eye frame size')
        frames[name.decode('ascii')] = raw[begin:begin + length]
    image_table = struct.unpack_from('<I', payload, 0x21DA4)[0]
    keys = {}
    for i in range(285):
        image_id, key = struct.unpack_from('<I4s', payload, image_table + i * 8)
        keys[image_id & 0xFFFF] = key.decode('ascii')
    table = struct.unpack_from('<I', payload, 0x29EA0)[0]
    recipes = []
    for i in range(100):
        ident, mode, count, _pad, pointer, _reserved = struct.unpack_from('<BBBBII', payload, table + i * 12)
        if not ident or not count or not 0 <= pointer < len(payload):
            continue
        steps = [struct.unpack_from('<HBB', payload, pointer + j * 4) for j in range(count)]
        recipes.append(dict(id=ident, frames=[dict(key=keys.get(image_id), ticks=ticks)
                                              for image_id, ticks, _ in steps]))
    return recipes, frames


def eye_entries(recipes):
    eyes = []
    for recipe in recipes:
        families = []
        for frame in recipe['frames']:
            family = re.sub(r'\d+$', '', frame['key'] or '')
            if family not in families:
                families.append(family)
        shown = [f for f in families if f != 'IDL'] or families
        eyes.append(dict(id=recipe['id'], name=' + '.join(WORDS.get(f, f) for f in shown),
                         frames=len(recipe['frames'])))
    return eyes


def write_eye_previews(recipes, frames, folder):
    try:
        from PIL import Image
    except ImportError:
        print('Pillow is not installed; skipping eye previews (pip install -r requirements-library.txt)')
        return 0
    images = {}
    for name, data in frames.items():
        pixels = [(((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31)
                  for (v,) in struct.iter_unpack('>H', data)]
        image = Image.new('RGB', (128, 128))
        image.putdata(pixels)
        images[name] = image
    folder.mkdir(parents=True, exist_ok=True)
    written = 0
    for recipe in recipes:
        if not all(f['key'] in images for f in recipe['frames']):
            continue
        sequence = [images[f['key']] for f in recipe['frames']]
        sequence[0].save(folder / f'animation-{recipe["id"]:02d}.gif', save_all=True,
                         append_images=sequence[1:], loop=0,
                         duration=[max(20, f['ticks'] * 100) for f in recipe['frames']])
        written += 1
    return written


# -------------------------------------------------------------- built-in moves
def build_stock_moves(main_image):
    if hashlib.sha256(main_image).hexdigest() != bp.MAIN_STOCK[bp.BIN]:
        fail('the robot backup does not hold the factory main firmware 28.01.005.008 this table layout is for')
    table = MOVE_TABLE - bp.FLASH_BASE
    cards = {card['move']: card for card in CARDS}
    stock = []
    for index in range(MOVE_COUNT):
        pointer, count = struct.unpack_from('<IH', main_image, table + index * 8)
        steps = []
        for n in range(count):
            kind, speed, offset, up, down, ms, _ = struct.unpack_from(
                '<IffIIII', main_image, pointer - bp.FLASH_BASE + n * 28)
            if kind not in KIND or speed != int(speed) or offset != int(offset):
                fail(f'unexpected step in built-in move {index}')
            # Ramp times travel in 5 ms units. A step from rest ramps up for 200 ms whatever is asked.
            steps.append(dict(kind=KIND[kind], a=int(speed), b=int(offset), up=up // 5 * 5,
                              down=down // 5 * 5, ms=ms))
        card = cards.get(index)
        if card:
            label = f"{card['name'].capitalize()} card"
            steps = [dict(kind='eyes', a=card['eyes']), dict(kind='sound', path=card['sound'])] + steps \
                + [dict(kind='wait', ms=600), dict(kind='eyes', a=2)]
        else:
            label = f'Built-in move {index}'
        plan_moves(steps)
        stock.append(dict(name=f'stock_{index}', label=label, steps=steps))
    return stock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--package', type=Path, help='vendor package (.rar or extracted folder)')
    parser.add_argument('--robot-backup', type=Path, help='folder made by backup_robot.py')
    parser.add_argument('--out', type=Path, default=Path(__file__).resolve().parent / 'library',
                        help='output folder (default: ./library next to the server)')
    args = parser.parse_args(argv)
    if not args.package and not args.robot_backup:
        parser.error('give --package and/or --robot-backup')

    library = dict(eyes=[], sounds=[], stock=[])
    args.out.mkdir(parents=True, exist_ok=True)
    previews = 0
    with tempfile.TemporaryDirectory() as scratch:
        if args.package:
            package = Package(args.package, scratch)
            library['sounds'] = build_sounds(package.sound_root())
            recipes, frames = parse_mva(package.read(MVA_NAME))
            library['eyes'] = eye_entries(recipes)
            previews = write_eye_previews(recipes, frames, args.out / 'eyes')
        if args.robot_backup:
            image = (args.robot_backup / bp.MAIN_REL / bp.BIN).read_bytes()
            library['stock'] = build_stock_moves(image)
    (args.out / 'library.json').write_text(json.dumps(library, indent=1) + '\n')
    print(f'{len(library["eyes"])} eye animations ({previews} previews), {len(library["sounds"])} sounds, '
          f'{len(library["stock"])} built-in moves;', sum(not s['known'] for s in library['sounds']),
          f'sounds still unnamed. Wrote {args.out}/library.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
