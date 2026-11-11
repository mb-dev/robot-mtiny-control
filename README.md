# robot-mtiny-control

Unofficial host control for the Makeblock **mTiny** toy robot over Bluetooth, from a computer: an HTTP and
WebSocket API, a small control web page, scripted "shows", eye animations, sounds and precise moves
(distance in millimetres, turns in degrees).

It is the optional robot for the spelling game at
[mb-dev/robo-words-spelling-game](https://github.com/mb-dev/robo-words-spelling-game).
[Watch a 35-second demo on YouTube](https://youtube.com/shorts/RzYreInii0E).

This is a hobby project to learn robotics and make a fun game for kids. The code, this README and the rest of the
repository were written by an AI coding agent (Claude) working with a parent.

**You need your own robot.** To use any of this you have to buy an mTiny from
[Makeblock](https://www.makeblock.com). This repository contains no robot, no firmware and no sounds of theirs.
Planned for the future: an open-source robot built on a micro:bit, as I work my way up the chain.

> **Read this first**
>
> - **Unofficial.** Not affiliated with, endorsed by, or supported by Makeblock. "mTiny" and "Makeblock" are
>   their trademarks.
> - **Tested on exactly one hardware revision and one firmware version** (see [docs/hardware.md](docs/hardware.md)).
>   Anything else is unsupported.
> - **This modifies your robot's firmware, which can permanently break it.** Whether the robot can recover from a
>   bad image is unproven. You do this entirely at your own risk. The installer and backup scripts have been
>   exercised only against simulated volumes in this repository's tests, not against hardware in their final form.
> - **No vendor firmware or assets are included.** You download Makeblock's package yourself; the scripts here
>   turn it (plus a backup of your own robot) into the patched files on your machine.
> - **No login.** Anyone who can reach the control server can drive the robot. See [Security](#security).

## What you need

- A Mac (macOS is what was tested), Python 3.11+ (3.14 tested), `clang` (Xcode command line tools:
  `xcode-select --install`) and `bsdtar` (built into macOS; it can read the vendor `.rar`).
- The robot, its USB cable, and Bluetooth on the computer.
- An mTiny of the same hardware revision as the tested one.

## Step by step

```sh
git clone https://github.com/mb-dev/robot-mtiny-control.git && cd robot-mtiny-control
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-control.txt -r requirements-library.txt
```

**1. Download the vendor package yourself.** Open Makeblock's article
[How to Update Firmware for mTiny](https://support.makeblock.com/hc/en-us/articles/4402732695447-How-to-Update-Firmware-for-mTiny)
and download the package it links for the older ("home") board:
`mtiny_update_sdcard_english_28.01.006.002.t1.rar`. Do not use any other file. The scripts check its SHA-256 and
refuse anything else. (If your Mac cannot read the `.rar`, extract it with an archive tool and pass the extracted
folder instead.)

**2. Back up the robot.** Connect it by USB with the rear knob on and mount its disk. Do not format it from macOS.

```sh
python backup_robot.py --volume /Volumes/<robot disk>
```

This only reads. It creates `device-backups/<timestamp>/`. **Keep it**: your robot's original main firmware exists
only there (it is not in the vendor package), and rollback uses it.

**3. Build the patch.**

```sh
python build_patch.py --package ~/Downloads/mtiny_update_sdcard_english_28.01.006.002.t1.rar \
    --robot-backup device-backups/<timestamp> --out build
```

It verifies every input hash, compiles `firmware/host_control.c` with clang and writes `build/overlay/...`. If your
compiler produces different bytes from the tested build it says so and stops; `--accept-untested-build` overrides
that at your own risk. It writes two components: the **radio** 28.01.001.006 and the **main** 28.01.005.016. The
radio patch is required: without it the robot drops a computer's connection and returns no replies. Details:
[docs/firmware.md](docs/firmware.md).

**4. Install, radio first.** Each command is a dry run until you add `--confirm-write`.

```sh
python install_update.py --component radio --volume /Volumes/<robot disk> --backup device-backups/<timestamp>
python install_update.py --component radio --volume /Volumes/<robot disk> --backup device-backups/<timestamp> --confirm-write
```

The script verifies the disk and every file, writes with read-back and ejects. Unplug USB and restart the robot so
its own updater installs the radio firmware; wait for the update to finish. Then repeat for `--component main`.
Check the result by running the server (below) and reading `status`: the version should end `005.016`.

**5. (Optional) Generate the sound, eye and built-in-move library** the page uses. Nothing from it is shipped:

```sh
python build_library.py --package ~/Downloads/mtiny_update_sdcard_english_28.01.006.002.t1.rar \
    --robot-backup device-backups/<timestamp>
```

Without it the server and page still work: type an eye ID (0-127) or a sound path by hand.

**6. Run the server and open the page.** Keep the robot's pen off and other Bluetooth controllers disconnected.

```sh
python mtiny_server.py          # then open http://127.0.0.1:8765/
python mtiny_send.py status     # command line client for the running server
```

The server owns the Bluetooth connection. It binds only to `127.0.0.1`. The API is described in [API.md](API.md),
the Bluetooth protocol in [docs/protocol.md](docs/protocol.md). Add your own shows to `sequences.json`.

## Optional: reach it from other devices with Tailscale Serve

One way (untested here, and the Tailscale CLI changes between versions; check `tailscale serve --help`) is to
publish the local port on your tailnet and tell the server which names to accept:

```sh
tailscale serve --bg 8765
python mtiny_server.py --allow-host tiny.example.ts.net --allow-origin-suffix tail1234.ts.net
```

`--allow-host` adds a Host name the server accepts (repeatable). `--allow-origin-suffix` lets pages on
`https://<label>.tail1234.ts.net` (for example the spelling game) call the API from a browser. Use your own names
in place of the placeholders. The robot needs its own tailnet name; it cannot share one with another service on a
different port.

## Using it with the spelling game on the same computer

A web page may only call the API if its address is allowed. For a game running at `http://localhost:8000`:

```sh
python mtiny_server.py --allow-origin http://localhost:8000
```

Then enter `http://localhost:8765` as the robot address on the game's grown-ups page. `--allow-origin` takes one
exact origin and can be repeated.

## Security

- There is **no authentication**. Reaching the server means controlling the robot's wheels, eyes and speaker.
  Only expose it on a network you trust. Do not forward the port to the internet.
- By default it accepts only `127.0.0.1`/`localhost` Host headers and same-origin or no-origin requests, and the
  GET-only API rejects cross-site browser requests, but that is a guard against drive-by web pages, not a login.
- The patched radio firmware admits **any** nearby Bluetooth central while the robot is on.
- Nothing stops motion automatically when an API call ends; call `/api/stop`. The robot's pulse and step limits are
  software deadlines, not a hardware watchdog.

## Rollback

`install_update.py --component main --rollback ...` (and `--component radio`) rewrites that component's original
files from your backup (dry run first, then `--confirm-write`), after which the robot installs them at its next
start. Rolling back restores files on the storage; it cannot repair a robot whose firmware failed to start.

## Using an AI coding agent

Agents are welcome to read, test and edit the code. Anything that touches the robot (Bluetooth, writing to its
storage, flashing) must be run by you or explicitly approved by you. See [CLAUDE.md](CLAUDE.md). Prompts you can
paste:

- *"Read CLAUDE.md and README.md, set up a virtual environment, run ruff and the tests, and tell me what you
  found. Do not connect to Bluetooth or to the robot."*
- *"Walk me through steps 1-3 of the README (download, backup, build). Run only the offline scripts, show me each
  command before running it, and stop before anything that writes to the robot."*
- *"Add a new show to sequences.json that does X using plan_moves items, and check it validates with the tests."*

## Layout

| Path | What |
|---|---|
| `mtiny_server.py`, `mtiny_control.html` | HTTP/WebSocket server and the control page |
| `mtiny_controller.py`, `mtiny_movement.py`, `mtiny_protocol.py`, `mtiny_ble.py`, `mtiny_send.py` | Bluetooth client, moves, protocol, CLI |
| `sequences.json` | Our own shows |
| `build_patch.py`, `firmware/host_control.c` | Stock-to-patched firmware builder and our firmware source |
| `backup_robot.py`, `install_update.py` | Backup, install and rollback for the robot's USB storage |
| `build_library.py`, `sound_labels.py` | Local generator for the page's sound, eye and move library |
| `docs/` | Short protocol, firmware and hardware references |
| `tests/` | Unit tests (no vendor files, no Bluetooth needed) |

## Development

```sh
pip install -r requirements-dev.txt
ruff check .
python -m unittest discover -s tests
```

Optional byte-identity test of the firmware builder, with your own files:
`MTINY_VENDOR_PACKAGE=... MTINY_ROBOT_BACKUP=... python -m unittest discover -s tests -p test_build_patch.py`.

## License

MIT, see [LICENSE](LICENSE). This license covers the code in this repository only, not Makeblock's firmware,
sounds or artwork, which you obtain from Makeblock.
