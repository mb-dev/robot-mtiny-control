#!/usr/bin/env python3
"""Copy an mTiny's USB storage into a new local backup folder. Read-only.

Run this BEFORE patching anything. The robot's own main firmware (28.01.005.008) is
not in Makeblock's package, so build_patch.py needs this backup, and
install_update.py uses it to verify the robot's state and to roll back.

Connect the robot by its USB cable (rear knob on, pen off) and pass the mounted
volume. Every file is read, written locally and re-hashed. Nothing is written to the
robot, no eject or restart is requested, and no update is started. This is a
filesystem backup, not a dump of the microcontrollers' flash or bootloaders. macOS may
add hidden index files to any volume it mounts; this script cannot prevent that.

    python backup_robot.py --volume /Volumes/<robot volume>
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

MAIN_REL = Path('system/mtiny_firmware/car_master_mcu_firmware')
RADIO_REL = Path('system/mtiny_firmware/car_ble_mcu_firmware')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def check_volume(volume):
    metadata = volume / RADIO_REL / 'mcu_firmware_discription.csv'
    if not metadata.is_file() or 'mtiny_car_ble_mcu' not in metadata.read_text(errors='replace'):
        raise SystemExit('Expected mTiny radio metadata not found; no backup attempted. '
                         'Pass the mounted robot storage, e.g. /Volumes/<name>.')


def list_files(volume):
    paths = sorted(p for p in (volume / 'system').rglob('*') if p.is_file() and not p.is_symlink())
    paths += sorted(p for p in volume.iterdir()
                    if p.is_file() and not p.is_symlink() and not p.name.startswith('.'))
    return paths


def firmware_versions(entries_dir):
    """Version text from the two firmware metadata files, for the final report."""
    versions = {}
    for label, rel in (('main', MAIN_REL), ('radio', RADIO_REL)):
        csv = entries_dir / rel / 'mcu_firmware_discription.csv'
        if csv.is_file():
            tokens = csv.read_text(errors='replace').strip().split(',')
            versions[label] = dict(zip(tokens[::2], tokens[1::2])).get('firmware_vervion=')
    return versions


def backup(volume, out):
    check_volume(volume)
    out.mkdir(parents=True, exist_ok=False)
    entries = []
    for source in list_files(volume):
        relative = source.relative_to(volume)
        data = source.read_bytes()
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        checksum = sha256(data)
        if sha256(destination.read_bytes()) != checksum:
            raise SystemExit(f'Verification of local copy failed: {relative}')
        entries.append({'path': str(relative), 'bytes': len(data), 'sha256': checksum})
    (out / 'manifest.json').write_text(json.dumps(entries, indent=2) + '\n')
    return entries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--volume', required=True, type=Path, help='mounted mTiny storage')
    parser.add_argument('--out', type=Path, default=None,
                        help='new backup folder (default: device-backups/<timestamp>)')
    args = parser.parse_args(argv)
    volume = args.volume.resolve()
    out = args.out or Path('device-backups') / datetime.now().strftime('%Y-%m-%d_%H%M%S')
    entries = backup(volume, out)
    print(json.dumps({'backup': str(out), 'files': len(entries),
                      'bytes': sum(e['bytes'] for e in entries),
                      'firmware': firmware_versions(out)}, indent=2))
    print('\nKeep this folder. It is the only copy of your robot\'s original main firmware.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
