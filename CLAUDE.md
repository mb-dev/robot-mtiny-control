# CLAUDE.md

Guidance for AI coding agents (also served as `AGENTS.md`, a symlink to this file).

## What this is

Unofficial host control for the Makeblock mTiny robot over Bluetooth, plus a firmware patch builder. See
`README.md` for the human story and `docs/` for the protocol, firmware and hardware notes.

## Hard rules

1. **Anything that touches the robot needs explicit human confirmation first**: Bluetooth (running
   `mtiny_server.py`, `mtiny_send.py` against a live server, `mtiny_ble.py`, the controller), mounting or
   reading the robot's USB storage (`backup_robot.py`), and writing/flashing (`install_update.py --confirm-write`,
   rollback). Ask, state exactly what will run, and wait for a clear yes. Dry runs of `install_update.py` still read
   the robot's disk, so they need confirmation too. Never use `--confirm-write` or `--accept-untested-build` on your
   own initiative.
2. **Never commit or add vendor files or anything derived from them**: the vendor `.rar`, extracted package files,
   robot backups, firmware images (`*.bin`), patched images and the `build/` folder, sound files, eye images/GIFs,
   `library/`, decompiled or disassembled listings, strings dumps, Ghidra exports. `.gitignore` covers the common
   ones; do not work around it. Short hex strings used as "is this the right place" checks in `build_patch.py` are
   the only vendor bytes allowed. (`MAIN_APPEND` and `RADIO_APPEND` there are this project's own code, kept as
   bytes.)
3. No personal data: no real device addresses/UUIDs, usernames, home paths, hostnames. Use placeholders
   (`example.ts.net`, `tail1234.ts.net`). The robot is found by its advertised name, never by a stored address.
4. Do not change pinned hashes in `build_patch.py` to make a build "pass". A mismatch means the input or the
   output is not the tested one; report it.

## Layout and commands

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .
python -m unittest discover -s tests          # no hardware, no vendor files; takes about a minute
```

- Flat runtime modules at the repo root: `mtiny_server.py` (HTTP/WebSocket, owns the BLE link),
  `mtiny_controller.py` (BLE client, lease), `mtiny_movement.py` (pulses, steps, `plan_moves`), `mtiny_protocol.py`
  (framing), `mtiny_ble.py`, `mtiny_send.py`, `mtiny_control.html`, `sequences.json`.
- Offline tooling: `build_patch.py` (stock -> patched firmware; needs the user's own vendor package and robot backup),
  `build_library.py` + `sound_labels.py` (local library generator), `backup_robot.py`, `install_update.py`.
- `firmware/host_control.c` is our firmware source, compiled by `build_patch.py`; addresses in it are specific to
  main firmware 28.01.005.008.
- `tests/` use simulated Bluetooth and simulated volumes. Tests that need vendor files must skip cleanly when they
  are absent (see `tests/test_build_patch.py`).

## Conventions

- Keep the server loopback-only by default; keep the API GET-only with its origin and host checks.
- Motion code must stay bounded (caps, deadlines, stop on disconnect) and never replay side-effecting commands.
- Run `ruff check .` and the tests before proposing changes. Keep changes small; do not mass-reformat.
- Docs in `docs/` are short rewritten references: no vendor function listings, no session logs.
