#!/usr/bin/env python3
"""Build the patched mTiny firmware files from firmware you obtained yourself.

This repository contains no vendor firmware. This script turns the exact files we
tested into the patched files, on your own machine, and refuses anything else:

  * radio (car BLE MCU) 28.01.001.002   from the vendor package you downloaded
  * main  (car master MCU) 28.01.005.008 from YOUR robot's own backup

The vendor package's main firmware is a different build (28.01.006.002) and is not
patched. The robot's factory main firmware, 28.01.005.008, exists only on the robot
(see backup_robot.py). Outputs: main 28.01.005.016 and radio 28.01.001.006, each as
mcu_firmware.bin plus mcu_firmware_discription.csv (the vendor's spelling), laid out
as an overlay tree that install_update.py copies to the robot's USB storage.

Everything is offline: no Bluetooth, no USB storage, no network.
Requires clang (for the C part) and, for a .rar package, bsdtar (macOS has it).

    python build_patch.py --package mtiny_update_sdcard_english_28.01.006.002.t1.rar \
        --robot-backup device-backups/<backup-folder> --out build
"""
import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
C_SOURCE = HERE / 'firmware' / 'host_control.c'
MAIN_REL = Path('system/mtiny_firmware/car_master_mcu_firmware')
RADIO_REL = Path('system/mtiny_firmware/car_ble_mcu_firmware')
BIN, CSV = 'mcu_firmware.bin', 'mcu_firmware_discription.csv'

# ---------------------------------------------------------------- pinned inputs
PACKAGE_NAME = 'mtiny_update_sdcard_english_28.01.006.002.t1.rar'
PACKAGE_RAR_SHA256 = 'c42c1ea0efa4ca7d9e22eba1a5fb9d6876abd8fb400e4657f70d46c09ed66ca8'
PACKAGE_ROOT = 'update_sdcard_english'
# Every firmware file of that package (checked when you pass an extracted folder).
PACKAGE_FILES = {
    'system/mtiny_firmware/car_ble_mcu_firmware/mcu_firmware.bin':
        'c09601cce8dfd9ce94a0389e3a49b5c8acd860fd71bd657cdc2286dcade1a6d0',
    'system/mtiny_firmware/car_ble_mcu_firmware/mcu_firmware_discription.csv':
        '559636fd36bf9abfe8f374f362fbabce06e3424cf7f30cb6922a6fc6e6b31165',
    'system/mtiny_firmware/car_master_mcu_firmware/mcu_firmware.bin':
        '99e1e16cbae58a8ad8a438aa4adc016389b7fbe8199e09be5e0d322b04878d2e',
    'system/mtiny_firmware/car_master_mcu_firmware/mcu_firmware_discription.csv':
        'cc1427418e13c5bbbb37900d9a0198889b25d4b5cd918a1d23b36491354d8bf6',
    'system/mtiny_firmware/pen_ble_mcu_firmware/mcu_firmware.bin':
        'db678a8418f22a145f7e82163743bd8d9e06e03ca4c84969e5028a9d45bdbf73',
    'system/mtiny_firmware/pen_ble_mcu_firmware/mcu_firmware_discription.csv':
        'bcca514edc9fa85d3845c4a29baf6ba5375b297dbb2878a4e4758867da19a595',
    'system/mtiny_firmware/pen_master_mcu_firmware/mcu_firmware.bin':
        '80d591d5c5232d705794e706a1335f5faa9b6f9d4a462bb484cd79c3dbd45975',
    'system/mtiny_firmware/pen_master_mcu_firmware/mcu_firmware_discription.csv':
        'ddab95cc549640d848452e34638372bba1743738b96c41f16af5786b48ff705c',
    'system/mtiny_firmware/car_audio_mcu_firmware/mcu_firmware_discription.csv':
        '2602951dd7b509c14e5658cc06b9db1bf8ee6da1448f31108394073ffd32600d',
}
RADIO_STOCK = {BIN: PACKAGE_FILES[f'{RADIO_REL}/{BIN}'], CSV: PACKAGE_FILES[f'{RADIO_REL}/{CSV}']}
# The robot's factory main firmware (from the robot's storage, not in the package).
MAIN_STOCK = {BIN: '17781108fccde7f6477940226bec59a5664b0db75535960433dff14d04f857a7',
              CSV: 'c63b49f3d02e2b56ac79d7eab4d03ad9807aca88e64ee67dfa5f3e5c3d27d358'}
MAIN_STOCK_VERSION, RADIO_STOCK_VERSION = '28.01.005.008', '28.01.001.002'
MAIN_VERSION, RADIO_VERSION = '28.01.005.016', '28.01.001.006'

# ------------------------------------------------------------- pinned outputs
# Layer 1 of main (assembled code, see docs/firmware.md) must give exactly this image.
MAIN_LAYER1_SHA256 = '86a6e98e38b470c21aaff264b788774d44c7983ba55b5439759b9fd245f61bf5'
OUTPUTS = {
    'main': {BIN: 'f373683285778e24a624a2f2872db78a2be98d398b4c6ca063d2e59dce5085a9',
             CSV: 'e99b255665f558f7a622538aed3d739e3f12140d90f1abca8d04fa76dc8e2e54'},
    'radio': {BIN: 'c59e364f30ba481415843bafb9f71c49e4ea0814d6dffa624d44dd8f5114090f',
              CSV: '868a6f54c466dab29ae8e771d2df99910420f3f47b3c518d6dd0b4d99ce4b164'},
}
TESTED_CLANG = 'Apple clang version 17.0.0 (clang-1700.6.3.2)'
CLANG_FLAGS = ['--target=arm-none-eabi', '-mcpu=cortex-m4', '-mthumb', '-mfloat-abi=soft', '-Oz',
               '-ffreestanding', '-fno-builtin', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables']
FLASH_BASE = 0x08006000  # main application load address

# ------------------------------------------------------------------ patch data
# In-place edits as (file offset, expected old bytes, new bytes). The old bytes are
# a few-byte "is this the right place" check; the whole input file is hash-pinned too.
# Main, layer 1: hooks into the stock command and router entry points, plus the
# version text. The new bytes are branches into the code appended below.
MAIN_LAYER1_EDITS = [
    (0x5dd0, '2de9f041', '27f060b8'),
    (0x5f33, '3038', '3132'),
    (0x12dcc, '70b50446', '1af022b9'),
    (0x18867, '3038', '3132'),
]
# Our own machine code (host activity lease, audio reply bridge), appended at the
# end of the stock image (stock length is 4-byte aligned). These two early layers
# were hand-assembled during development and their assembly source has not been
# published yet, so they appear here as bytes; the later layer is firmware/host_control.c.
MAIN_APPEND = bytes.fromhex(
    "417a0229c0f09080426853787d2b2dd07e2b40f08980022940f08a801378202b"
    "40f086802de9f0410446424b9847424d2e6801362e6092b0684602a940220023"
    "dff8f8c0e047684620213d4b984768467e213b4b984768460021394b98476846"
    "2146384b984712b0bde8f08102295fd11378202b5cd12de9f041044692b06846"
    "02a940220023dff8b4c0e047684620212b4b984768467d21294b98472a4d2978"
    "6846274b9847294d29786846244b9847274d29786846224b9847264d29786846"
    "1f4b9847244d297868461d4b9847234d29686846224b9847224d2968224d2e68"
    "891b68461e4b9847134d296868461c4b98471e4d29686846194b98471c4d2978"
    "68460f4b98471b4d297868460c4b9847194d297868460a4b984768462146094b"
    "984712b0bde8f0812de9f041134b18477047000001b700084c08002099970008"
    "259a0008dbf10108300800203208002055060020a0080020e40300202c080020"
    "e19a0008b0030020e803002048080020330800203408002035080020d5bd0008"
    "002859d0017a012956d1417a002953d10178082950d350294ed842681178f029"
    "4ad15178022947d19178712944d1d178012941d11179502906d0512904d05529"
    "05d0602938d102e00378082b34d12de9f04095b004461a490a78012a28d11949"
    "0a6819490968521a40f2dc539a421fd865682678684629460022b24203d28b5c"
    "83540132f9e7ff238df80130b21e0deb02000378033b03f07f03037000206946"
    "32460a4b9847204615b0bde8f04003e0204615b0bde8f04070b50446044b1847"
    "a0080020b0030020a40800206f8d0108d18d0108"
)
# Radio: connection admission, connection-interval request, reply routing, version.
RADIO_EDITS = [
    (0x4b8a, '0fd10024', '08f0c9bb'),
    (0x6bf8, '0620adf80400adf80600', '40f20600c0f218000190'),
    (0x7302, '0121fdf7bcff', '0a4606f014f8'),
    (0xa5d2, '28b9', '05e0'),
    (0xab6a, '02', '06'),
    (0xb3e4, '32', '36'),
    (0xd244, 'dd40', 'e9d3'),
]
RADIO_APPEND = bytes.fromhex(
    "01d0012802d10024f7f731bcf7f73ebcf0b581b00446154601208df800002046"
    "0121244b98479df82000791c884239d16878012807d0ff2834d1a878712831d1"
    "e87801282ed168461b4b98471b4e7068012827d11a48818a4ff6ff72914221d0"
    "807d00281ed0d6f81861b068f268131ab18801318b4215d2c91a9df82070b942"
    "10d33168b4880023bb4208d2e85c94460cea040c01f80c0001320133f4e7bff3"
    "5b8ff26068469df80000064b984701b0f0bd000081520300b12e0300445e0020"
    "4c4f0020bd2e0300f0b583b004460d46002c38d0002d36d0208810280cd01128"
    "0ad050282fd1e088298988422bd1208a022828d1002700e0012768460830174b"
    "984720462946164b984700900191002f01d00020a8750020124ed6f81811ca68"
    "8a603061f06110490870d6f81c11ca688a600e4908700e49087068469df80800"
    "0c4b984705e020462946054b9847009001910098019903b0f0bd0000b12e0300"
    "dd400300445e00203f4300205343002054430020bd2e0300"
)


# Layer 2 of main is firmware/host_control.c, compiled here and linked by hand.
C_SYMBOLS = ('motor_query', 'motor_tick', 'version_report', 'route_hook')
LAYER2_VERSION_TEXT = (b'28.01.005.012.7100\0', b'28.01.005.016.7100\0')
LAYER2_VERSION_OFFSETS = (0x5f28, 0x1885c)


class BuildError(SystemExit):
    pass


def fail(message):
    raise BuildError(f'error: {message}')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------- inputs
def read_package_file(package, relative):
    """Bytes of one package file, from a .rar (via bsdtar, nothing extracted) or a folder."""
    if package.is_file():
        member = f'{PACKAGE_ROOT}/{relative}'
        result = subprocess.run(['bsdtar', '-xOf', str(package), member], capture_output=True)
        if result.returncode or not result.stdout:
            fail(f'could not read {member} from the archive with bsdtar. Extract the package '
                 f'yourself (Keka, The Unarchiver, 7-Zip) and pass the extracted folder instead. '
                 f'{result.stderr.decode(errors="replace").strip()}')
        return result.stdout
    for root in (package, package / PACKAGE_ROOT, *package.glob(f'*/{PACKAGE_ROOT}')):
        if (root / relative).is_file():
            return (root / relative).read_bytes()
    return None


def load_radio_stock(package):
    if package.is_file():
        digest = hashlib.sha256()
        with package.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
        if digest.hexdigest() != PACKAGE_RAR_SHA256:
            fail(f'{package.name} has SHA-256 {digest.hexdigest()}, expected {PACKAGE_RAR_SHA256} '
                 f'({PACKAGE_NAME}). Only that exact package is supported.')
    elif not package.is_dir():
        fail(f'--package {package} is neither a file nor a folder')
    else:
        for relative, expected in PACKAGE_FILES.items():
            data = read_package_file(package, relative)
            required = relative.startswith(str(RADIO_REL))
            if data is None and required:
                fail(f'{relative} not found under {package}. Pass the folder that contains '
                     f'{PACKAGE_ROOT}/ (or the .rar itself).')
            if data is not None and sha256(data) != expected:
                fail(f'{relative} in {package} does not match the pinned vendor package '
                     f'(got {sha256(data)}, expected {expected}). Only {PACKAGE_NAME} is supported.')
    files = {}
    for name in (BIN, CSV):
        data = read_package_file(package, f'{RADIO_REL}/{name}')
        if data is None or sha256(data) != RADIO_STOCK[name]:
            fail(f'radio {name} does not match the pinned stock file')
        files[name] = data
    return files


def load_main_stock(backup):
    files = {}
    for name in (BIN, CSV):
        path = backup / MAIN_REL / name
        if not path.is_file():
            fail(f'{path} not found. --robot-backup must be a folder made by backup_robot.py '
                 f'(it contains system/mtiny_firmware/...).')
        data = path.read_bytes()
        if sha256(data) != MAIN_STOCK[name]:
            fail(f'{path} has SHA-256 {sha256(data)}, expected {MAIN_STOCK[name]}. This tool patches '
                 f'main firmware {MAIN_STOCK_VERSION} exactly. The file may already be patched, '
                 f'or your robot may be a different revision. Nothing was built.')
        files[name] = data
    return files


# ----------------------------------------------------------------- patch steps
def apply_edits(image, edits, label):
    out = bytearray(image)
    for offset, old_hex, new_hex in edits:
        old, new = bytes.fromhex(old_hex), bytes.fromhex(new_hex)
        if len(old) != len(new) or out[offset:offset + len(old)] != old:
            fail(f'{label}: unexpected bytes at 0x{offset:x}; this is not the stock image')
        out[offset:offset + len(new)] = new
    return out


def derive_metadata(stock_csv, stock_image, image, old_version, new_version):
    """The vendor's CSV with version, size and CRC32 replaced (each appears exactly once)."""
    meta = stock_csv
    for old, new in ((old_version, new_version), (str(len(stock_image)), str(len(image))),
                     (str(zlib.crc32(stock_image)), str(zlib.crc32(image)))):
        if meta.count(old.encode()) != 1:
            fail(f'metadata does not contain {old!r} exactly once')
        meta = meta.replace(old.encode(), new.encode())
    return meta


def elf_section(blob, name):
    """Raw bytes of one section of a 32-bit little-endian ELF object (no relocations allowed)."""
    if blob[:6] != b'\x7fELF\x01\x01':
        fail('compiler output is not a 32-bit little-endian ELF object')
    table = struct.unpack_from('<I', blob, 32)[0]
    size, count, names_index = struct.unpack_from('<HHH', blob, 46)
    rows = [struct.unpack_from('<10I', blob, table + i * size) for i in range(count)]
    names = blob[rows[names_index][4]:rows[names_index][4] + rows[names_index][5]]
    index = next((i for i, r in enumerate(rows)
                  if names[r[0]:].split(b'\0', 1)[0].decode() == name), None)
    if index is None:
        fail(f'compiler output has no section {name}')
    if any(r[1] in (4, 9) and r[7] == index and r[5] for r in rows):
        fail('unresolved relocations in compiler output')
    row = rows[index]
    return blob[row[4]:row[4] + row[5]]


def thumb_branch(source, target, link):
    """32-bit Thumb-2 B.W (link=False) or BL (link=True) at `source` to `target`."""
    offset = target - source - 4
    if offset % 2 or not -(1 << 24) <= offset < (1 << 24):
        fail('branch out of range')
    v = offset & 0x1ffffff
    s, i1, i2 = v >> 24, (v >> 23) & 1, (v >> 22) & 1
    high = 0xf000 | (s << 10) | ((v >> 12) & 0x3ff)
    low = ((1 ^ (i1 ^ s)) << 13) | ((1 ^ (i2 ^ s)) << 11) | ((v >> 1) & 0x7ff)
    low |= 0xd000 if link else 0x9000
    return struct.pack('<HH', high, low)


def clang_version():
    try:
        out = subprocess.run(['clang', '--version'], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        fail('clang not found. Install the Xcode command line tools (xcode-select --install).')
    return out.splitlines()[0].strip()


def compile_c():
    with tempfile.TemporaryDirectory() as tmp:
        obj = Path(tmp) / 'host_control.o'
        result = subprocess.run(['clang', *CLANG_FLAGS, '-c', str(C_SOURCE), '-o', str(obj)],
                                capture_output=True, text=True)
        if result.returncode:
            fail(f'clang failed:\n{result.stderr}')
        return obj.read_bytes()


def build_main(stock_image, stock_csv):
    layer1 = bytes(apply_edits(stock_image, MAIN_LAYER1_EDITS, 'main layer 1')) + MAIN_APPEND
    if sha256(layer1) != MAIN_LAYER1_SHA256:
        fail('main layer 1 does not reproduce the pinned intermediate image (internal error)')
    elf = compile_c()
    image, symbols = bytearray(layer1), {}
    for name in C_SYMBOLS:
        image.extend(b'\xff' * (-len(image) & 3))
        symbols[name] = FLASH_BASE + len(image)
        image.extend(elf_section(elf, '.text.' + name))

    def check(offset, expected):
        if bytes(layer1[offset:offset + len(expected)]) != expected:
            fail(f'main layer 2: unexpected bytes at 0x{offset:x}')

    def place(offset, data):
        image[offset:offset + len(data)] = data

    check(0x3c, struct.pack('<I', 0x08006cad))                        # SysTick vector
    place(0x3c, struct.pack('<I', symbols['motor_tick'] | 1))
    hook = 0x0800bdd0 - FLASH_BASE                                    # command entry, layer 1 branch
    check(hook, thumb_branch(0x0800bdd0, 0x08032e94, False))
    place(hook, thumb_branch(0x0800bdd0, symbols['motor_query'], False))
    zero = 0x08032bbc - FLASH_BASE                                    # startup zero-init table row
    check(zero, struct.pack('<4I', 0x08032e94, 0x20000a68, 0x2a28, 0x08007988))
    place(zero + 8, struct.pack('<I', 0x2ab4))                        # cover our SRAM state block
    for site in (0x08027346, 0x08028e54):                             # both calls to the version reporter
        check(site - FLASH_BASE, thumb_branch(site, 0x08028e70, True))
        place(site - FLASH_BASE, thumb_branch(site, symbols['version_report'], True))
    route = 0x08018dcc - FLASH_BASE                                   # router entry, layer 1 branch
    check(route, thumb_branch(0x08018dcc, 0x08033014, False))
    place(route, thumb_branch(0x08018dcc, symbols['route_hook'], False))
    numeric = 0x0801d382 - FLASH_BASE                                 # numeric version reply
    check(numeric - 8, bytes.fromhex('05216846ecf701fc0821'))
    image[numeric] = 16
    old, new = LAYER2_VERSION_TEXT
    for offset in LAYER2_VERSION_OFFSETS:
        check(offset, old)
        place(offset, new)
    image = bytes(image)
    meta = derive_metadata(stock_csv, stock_image, image, MAIN_STOCK_VERSION, MAIN_VERSION)
    return image, meta, symbols


def build_radio(stock_image, stock_csv):
    image = bytes(apply_edits(stock_image, RADIO_EDITS, 'radio')) + RADIO_APPEND
    meta = derive_metadata(stock_csv, stock_image, image, RADIO_STOCK_VERSION, RADIO_VERSION)
    return image, meta


# ------------------------------------------------------------------------ main
def check_metadata(image, meta, version, label):
    tokens = meta.decode().strip().split(',')
    fields = dict(zip(tokens[::2], tokens[1::2]))
    if (fields.get('firmware_vervion=') != version or int(fields.get('firmware_size=', -1)) != len(image)
            or int(fields.get('firmware_crc32=', -1)) != zlib.crc32(image)):
        fail(f'{label} metadata does not describe the image (internal error)')
    if image.count(version.encode() + b'.7100\0') != (2 if label == 'main' else 0):
        fail(f'{label} version text mismatch (internal error)')


def write_overlay(out, relative, image, meta):
    folder = out / 'overlay' / relative
    folder.mkdir(parents=True)
    (folder / BIN).write_bytes(image)
    (folder / CSV).write_bytes(meta)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--package', type=Path, required=True,
                        help=f'{PACKAGE_NAME}, or the folder you extracted it to')
    parser.add_argument('--robot-backup', type=Path, required=True,
                        help="folder made by backup_robot.py (supplies the robot's own main firmware)")
    parser.add_argument('--out', type=Path, default=Path('build'), help='output folder (default: build)')
    parser.add_argument('--force', action='store_true', help='replace an existing output folder')
    parser.add_argument('--accept-untested-build', action='store_true',
                        help='keep the result even if your compiler produced bytes that differ from the '
                             f'tested build ({TESTED_CLANG}). Not hardware-verified.')
    args = parser.parse_args(argv)

    if args.out.exists() and any(args.out.iterdir()):
        if not args.force:
            fail(f'{args.out} is not empty; choose another --out or pass --force')
        shutil.rmtree(args.out)
    radio_stock = load_radio_stock(args.package)
    main_stock = load_main_stock(args.robot_backup)

    compiler = clang_version()
    main_image, main_meta, symbols = build_main(main_stock[BIN], main_stock[CSV])
    radio_image, radio_meta = build_radio(radio_stock[BIN], radio_stock[CSV])
    check_metadata(main_image, main_meta, MAIN_VERSION, 'main')
    check_metadata(radio_image, radio_meta, RADIO_VERSION, 'radio')

    built = {'main': {BIN: main_image, CSV: main_meta}, 'radio': {BIN: radio_image, CSV: radio_meta}}
    untested = False
    for part, files in built.items():
        for name, data in files.items():
            if sha256(data) != OUTPUTS[part][name]:
                untested = True
                print(f'MISMATCH {part}/{name}: built {sha256(data)}, tested build {OUTPUTS[part][name]}')
    if untested:
        print(f'\nYour build differs from the tested one. Compiler here: {compiler!r}; '
              f'tested with: {TESTED_CLANG!r}. The patch logic checked out, but these bytes were '
              f'never run on a robot.')
        if not args.accept_untested_build:
            fail('refusing to write untested output. Re-run with --accept-untested-build to keep it anyway.')
    elif compiler != TESTED_CLANG:
        print(f'note: compiler is {compiler!r} (tested: {TESTED_CLANG!r}), but output is byte-identical.')

    args.out.mkdir(parents=True, exist_ok=True)
    write_overlay(args.out, MAIN_REL, main_image, main_meta)
    write_overlay(args.out, RADIO_REL, radio_image, radio_meta)
    manifest = {
        'main': {'version': MAIN_VERSION, 'stock_version': MAIN_STOCK_VERSION, 'path': str(MAIN_REL),
                 'sha256': {n: sha256(d) for n, d in built['main'].items()},
                 'stock_sha256': MAIN_STOCK},
        'radio': {'version': RADIO_VERSION, 'stock_version': RADIO_STOCK_VERSION, 'path': str(RADIO_REL),
                  'sha256': {n: sha256(d) for n, d in built['radio'].items()},
                  'stock_sha256': RADIO_STOCK},
        'compiler': compiler, 'tested_compiler': TESTED_CLANG, 'untested_build': untested,
        'c_source_sha256': sha256(C_SOURCE.read_bytes()),
        'c_symbols': {k: hex(v) for k, v in symbols.items()},
    }
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'\nWrote {args.out}/overlay and manifest.json'
          + (' (UNTESTED BUILD)' if untested else ' (byte-identical to the tested build)'))
    for part, files in built.items():
        for name, data in files.items():
            print(f'  {part:5} {name:32} {len(data):7} bytes  sha256 {sha256(data)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
