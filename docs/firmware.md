# The firmware patch (what it does and how it is built)

This repository does not contain Makeblock firmware. `build_patch.py` produces the patched files on your machine
from two inputs you must supply yourself, and refuses anything whose SHA-256 differs from the tested files:

| Input | Where it comes from | Pinned |
|---|---|---|
| Radio `28.01.001.002` (`car_ble_mcu_firmware`) | the vendor package you download (`.rar` or extracted) | package SHA-256 and per-file SHA-256 |
| Main `28.01.005.008` (`car_master_mcu_firmware`) | **your robot's own storage**, via `backup_robot.py` | per-file SHA-256 |

The package's main firmware is a different build (28.01.006.002) and is not patched. The factory main firmware
of the tested robot exists only on the robot.

## Radio 28.01.001.006 (required)

The stock radio disconnects unknown Bluetooth centrals, asks for a connection interval a computer's Bluetooth stack
does not use (the link was dropped after roughly 95 s), and does not return main-controller replies over the UART
service. Without the patch a computer cannot hold a connection or receive replies, so no host-control feature
works. The one patched image contains all of: admission of normal-mode centrals, an accepted 7.5-30 ms interval
range, routing of replies for main-controller requests to the Bluetooth link, and a fix for a length-load bug in the
reply duplication code. Edits are a handful of in-place instruction changes plus about 376 bytes of our own
appended code (`RADIO_EDITS`, `RADIO_APPEND`).

Consequence: it admits **any** nearby BLE central, not only your computer.

## Main 28.01.005.016

Built in two layers (`MAIN_LAYER1_EDITS`/`MAIN_APPEND`, then `firmware/host_control.c`):

1. Host activity lease and diagnostics, and a bridge that returns audio-controller replies, as about 600 bytes of
   our own appended machine code reached through two hooks (checked against pinned intermediate hash).
2. `firmware/host_control.c`, compiled by `build_patch.py` with clang and linked by hand: bounded wheel pulses,
   renewable pulses, read-only diagnostics pages, host-submitted speed-controller steps, and a version fix.
   The robot's updater compares a numeric version reply with the metadata and re-sends the main image on every start
   when they differ, so the numeric version, both version strings and the CSV must change together.

The C source uses absolute addresses valid only for main 28.01.005.008. It is our code; it contains no vendor code.

## Reproducibility

`build_patch.py` pins the SHA-256 of every output. The tested build used `Apple clang version 17.0.0
(clang-1700.6.3.2)`. A different compiler may produce different bytes; the script then says so and refuses unless
you pass `--accept-untested-build`. Such a build is not hardware-verified.

## Install and rollback

`backup_robot.py` -> `build_patch.py` -> `install_update.py --component radio` -> restart robot -> verify ->
`install_update.py --component main`. Both are dry runs unless `--confirm-write` is given. `--rollback` rewrites
the factory files for a component from your backup. The robot then installs what is on its storage at its next
start (the vendor's normal update behaviour). See the README for the full procedure and warnings.
