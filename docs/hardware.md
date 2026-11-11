# Hardware notes, calibration, tested versions, limits

## Tested hardware and firmware

Everything here was observed on **one robot**: the older ("home") mTiny board revision, with

| Part | Version |
|---|---|
| Main (car master MCU) factory firmware | 28.01.005.008 (patched to 28.01.005.016) |
| Radio (car BLE MCU) factory firmware | 28.01.001.002 (patched to 28.01.001.006) |
| Vendor package used as the radio source | `mtiny_update_sdcard_english_28.01.006.002.t1.rar` |
| Host | macOS, Python 3.14, Apple clang 17.0.0 (clang-1700.6.3.2) |

Makeblock distinguishes a newer board revision (identified by a label on the underside); it was not tested and
the patches are almost certainly not valid for it. `build_patch.py` refuses any input whose hash differs.

## What is inside (from public photos and the update package)

- Several microcontrollers: a main controller (marking appears to read GD32F403; provisional), a Nordic nRF52832
  radio, and a separate audio/display controller. The pen has its own main and radio controllers.
- USB mounts the robot's storage as a disk. The vendor update procedure copies firmware files onto it; the robot
  installs them on the next start. Makeblock warns not to format the disk on macOS.
- Wheels have encoders; left/right motor channels are channel 0 / 1.

## Measured calibration (steps, floor, wheels clear of walls)

| Quantity | Value |
|---|---|
| Forward distance | about 34.7 encoder counts per mm; nominal speed units behave as mm/s, real distance about 1.07x nominal (`FORWARD_SCALE` in `mtiny_movement.py`) |
| Turning | about 22.3 counts per wheel per degree; turn speed units are degrees/s; 360 degrees and 90 degrees both landed on target within tape/eye tolerance |
| Tracking | closed loop within about 2% of target; the two wheels agree within about 1% |
| Timing | effective motion time is about `ms - 100 - down/2` (forced 200 ms start ramp) |
| Left/right | step kind `left` drives the left wheel back and the right wheel forward (confirmed turning left) |

`plan_moves` in `mtiny_movement.py` turns `mm` / `deg` items into steps with these numbers. Your robot, floor
and battery level will differ; treat them as a starting point.

## Known limits

- Unofficial and tested on one revision. Other revisions or firmware versions are unsupported.
- Whether the robot's bootloader would recover from a bad image is unproven. A broken firmware may need a debug
  probe, or may be unrecoverable.
- The installer and backup scripts for the robot's storage have not been exercised against hardware in this
  repository's final form (only simulated volumes in tests).
- No login on the control server. Anyone who can reach it can drive the robot.
- Pulse and step deadlines are software deadlines, not a hardware watchdog; a stalled main CPU is not covered.
- Intermittent lost replies were seen and not fully explained; the client paces writes and never replays side
  effects.
- Pen and computer together are unsupported. Bluetooth sound/eye upload is not implemented.
- Most eye IDs, backward distances and right turns were only lightly exercised on the robot.
- Playback and eye requests report "requested", not "physically confirmed".
