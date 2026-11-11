#!/usr/bin/env python3
"""Copy patched firmware files onto the mTiny's USB storage (or roll back). UNTESTED ON HARDWARE.

This only copies files, like dragging them onto the drive. The robot's own updater
then installs them the next time it starts. It cannot be tested without a robot; read
the README and docs/firmware.md first. A bad firmware can permanently break the robot.

Without --confirm-write this is a dry run: every check runs, nothing is written.

Order that was used on the tested robot (two separate runs, a restart between them):
  1. --component radio   then unplug USB, restart the robot, wait for the update to finish
  2. --component main    (requires the patched radio to be on the storage already)
  --rollback with either component restores that component's files from the backup.

Checks before writing: the volume is an external, writable USB disk of the expected
size; the backup's files match its manifest; every file on the robot equals the backup
(or a known patched file); the files to write are exactly the pinned build outputs
(unless --accept-untested-build) and their metadata is self-consistent. After writing
each file is read back and compared, nothing else on the volume may have changed, and
the volume is ejected. Success of the robot's own update is never assumed.

    python install_update.py --component radio --volume /Volumes/<name> \
        --backup device-backups/<folder> --build build              # dry run
    ... --confirm-write                                              # really write
"""
import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import unicodedata
import zlib
from datetime import datetime
from pathlib import Path

import build_patch as bp

EXPECTED_VOLUME_BYTES = 127657984  # the tested robot's storage
COMPONENTS = {'main': bp.MAIN_REL, 'radio': bp.RADIO_REL}
FILES = (bp.BIN, bp.CSV)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise SystemExit(f'refused: {message}')


def check_volume_info(info, volume, expected_bytes):
    """Pure check of `diskutil info -plist` output."""
    require(info.get('BusProtocol') == 'USB' and info.get('Internal') is False
            and info.get('Writable') is True, 'expected an external, writable USB volume.')
    require(unicodedata.normalize('NFC', info.get('MountPoint', '')) ==
            unicodedata.normalize('NFC', str(volume)), 'mount point mismatch; pass the exact /Volumes path.')
    if expected_bytes:
        require(info.get('TotalSize') == expected_bytes,
                f'unexpected storage size {info.get("TotalSize")} (tested robot: {expected_bytes}). '
                f'Pass --expected-size 0 only if you are sure this is an mTiny.')


def volume_info(volume):
    try:
        return plistlib.loads(subprocess.check_output(['diskutil', 'info', '-plist', str(volume)]))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f'refused: cannot inspect the volume with diskutil (macOS only): {exc}')


def check_metadata(image, meta, label):
    tokens = meta.decode().strip().split(',')
    fields = dict(zip(tokens[::2], tokens[1::2]))
    require(fields.get('firmware_name=') in ('mtiny_car_master_mcu', 'mtiny_car_ble_mcu'),
            f'{label}: unexpected firmware name in metadata')
    require(int(fields['firmware_size=']) == len(image) and int(fields['firmware_crc32=']) == zlib.crc32(image),
            f'{label}: metadata size/CRC32 do not match the image')


def load_overlay(build, component, accept_untested):
    manifest = json.loads((build / 'manifest.json').read_text())
    require(not manifest.get('untested_build') or accept_untested,
            'this build was made with --accept-untested-build; pass it here as well to use it.')
    files = {}
    for name in FILES:
        data = (build / 'overlay' / COMPONENTS[component] / name).read_bytes()
        if not accept_untested:
            require(sha256(data) == bp.OUTPUTS[component][name],
                    f'{component}/{name} is not the pinned build output. Rebuild with build_patch.py.')
        files[name] = data
    check_metadata(files[bp.BIN], files[bp.CSV], component)
    return files


def load_backup(backup):
    manifest = json.loads((backup / 'manifest.json').read_text())
    entries = {}
    for entry in manifest:
        relative = Path(entry['path'])
        require(not relative.is_absolute() and '..' not in relative.parts, 'unsafe path in backup manifest.')
        require(entry['path'] not in entries, 'duplicate path in backup manifest.')
        entries[entry['path']] = entry['sha256']
    return entries


def preflight(volume, backup, build, component, rollback, accept_untested=False, log=print):
    """Verify everything and return (paths_to_write {relative: bytes}, expected_after {relative: sha})."""
    entries = load_backup(backup)
    overlays = {c: load_overlay(build, c, accept_untested) for c in COMPONENTS}
    overlay_hash = {str(COMPONENTS[c] / n): sha256(d) for c, f in overlays.items() for n, d in f.items()}
    stock_hash = {str(COMPONENTS['main'] / n): bp.MAIN_STOCK[n] for n in FILES}
    stock_hash.update({str(COMPONENTS['radio'] / n): bp.RADIO_STOCK[n] for n in FILES})

    target = {str(COMPONENTS[component] / n) for n in FILES}
    for path in target:
        require(entries.get(path) == stock_hash[path],
                f'the backup does not hold the stock {path}; it was made from an already modified robot. '
                f'Rollback from this backup would not restore the factory file.')
    for index, (path, digest) in enumerate(sorted(entries.items()), 1):
        require(sha256((backup / path).read_bytes()) == digest, f'backup file changed: {path}')
        live = sha256((volume / path).read_bytes()) if (volume / path).is_file() else None
        require(live is not None, f'file missing from the robot storage: {path}')
        allowed, hint = {digest} | ({overlay_hash[path]} if path in overlay_hash else set()), ''
        if path in target:
            allowed = {overlay_hash[path]} if rollback else {digest}
        elif component == 'main' and not rollback and path.startswith(str(bp.RADIO_REL)) and path in overlay_hash:
            allowed, hint = {overlay_hash[path]}, ' Install the patched radio first (--component radio).'
        require(live in allowed, f'robot file {path} is not in the expected state.{hint}')
        if index % 100 == 0:
            log(f'verified {index}/{len(entries)} backup and live files')
    for name in FILES:
        leftover = (volume / COMPONENTS[component] / name).with_name(f'.mtiny-update-{name}.tmp')
        require(not leftover.exists(), f'unfinished temporary file exists: {leftover}; inspect it first.')
    if rollback:
        writes = {path: (backup / path).read_bytes() for path in target}
    else:
        writes = {str(COMPONENTS[component] / n): overlays[component][n] for n in FILES}
    return writes, {path: sha256(data) for path, data in writes.items()}


def write_verified(volume, relative, data):
    target = volume / relative
    temporary = target.with_name(f'.mtiny-update-{target.name}.tmp')
    with temporary.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    require(temporary.read_bytes() == data, f'temporary readback failed: {relative}')
    os.replace(temporary, target)
    with target.open('rb') as handle:
        os.fsync(handle.fileno())
        require(handle.read() == data, f'final readback failed: {relative}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--component', choices=sorted(COMPONENTS), required=True)
    parser.add_argument('--volume', type=Path, required=True, help='mounted mTiny storage')
    parser.add_argument('--backup', type=Path, required=True, help='folder made by backup_robot.py')
    parser.add_argument('--build', type=Path, default=Path('build'), help='folder made by build_patch.py')
    parser.add_argument('--rollback', action='store_true',
                        help="write the backup's original files for this component instead")
    parser.add_argument('--confirm-write', action='store_true',
                        help='actually write to the robot (otherwise: dry run)')
    parser.add_argument('--accept-untested-build', action='store_true')
    parser.add_argument('--expected-size', type=int, default=EXPECTED_VOLUME_BYTES,
                        help='expected storage size in bytes, 0 to skip (default: the tested robot)')
    parser.add_argument('--no-eject', action='store_true')
    parser.add_argument('--log-dir', type=Path, default=Path('device-actions'))
    args = parser.parse_args(argv)
    volume, backup, build = args.volume.resolve(), args.backup.resolve(), args.build.resolve()

    info = volume_info(volume)
    check_volume_info(info, volume, args.expected_size)
    writes, expected_after = preflight(volume, backup, build, args.component, args.rollback,
                                       args.accept_untested_build)
    action = 'rollback' if args.rollback else 'install'
    if not args.confirm_write:
        print(json.dumps({'status': 'Preflight passed; dry run, nothing written', 'action': action,
                          'component': args.component, 'would_write': expected_after}, indent=2))
        print('\nRe-run with --confirm-write to write these files to the robot.')
        return 0

    stamp = datetime.now().astimezone()
    log = {'time': stamp.isoformat(), 'action': action, 'component': args.component,
           'volume': str(volume), 'device': info.get('DeviceNode'), 'backup': str(backup),
           'status': 'preflight passed; writing', 'files': {}, 'ejected': False}
    args.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.log_dir / f'{action}-{args.component}-{stamp:%Y%m%d-%H%M%S}.json'
    log_path.write_text(json.dumps(log, indent=2) + '\n')
    entries = load_backup(backup)
    before = {p: sha256((volume / p).read_bytes()) for p in writes}
    for relative, data in writes.items():
        write_verified(volume, relative, data)
        log['files'][relative] = {'before_sha256': before[relative], 'after_sha256': sha256(data),
                                  'readback_verified': True}
        log_path.write_text(json.dumps(log, indent=2) + '\n')
    changed = sorted(p for p in entries if sha256((volume / p).read_bytes()) != entries[p])
    other = [p for p in changed if p not in writes and p not in
             {str(COMPONENTS[c] / n) for c in COMPONENTS for n in FILES}]
    require(not other, f'unexpected files changed: {other}')
    log['status'] = 'written and read back; the robot has NOT installed anything yet'
    subprocess.run(['/bin/sync'], check=True)
    if not args.no_eject:
        result = subprocess.run(['diskutil', 'eject', info['DeviceNode']], capture_output=True, text=True)
        log['eject'] = {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
        log['ejected'] = result.returncode == 0 and not volume.exists()
    log_path.write_text(json.dumps(log, indent=2) + '\n')
    print(json.dumps({'log': str(log_path), 'status': log['status'], 'ejected': log['ejected'],
                      'written': sorted(writes)}, indent=2))
    print('\nNext: unplug USB, restart the robot, let its update animation finish, then check '
          'the version with the control server (status command). See the README.')
    require(args.no_eject or log['ejected'], f'eject did not complete: {log.get("eject")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
