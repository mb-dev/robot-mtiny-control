# Host protocol

What `mtiny_protocol.py`, `mtiny_controller.py` and `mtiny_movement.py` speak. It is derived from our own
observations of one robot and is not an official specification. The commands marked **(patched)** exist only
after `build_patch.py` firmware is installed; the stock robot does not answer them.

## Transport

- BLE GATT, Nordic UART Service: service `6e400001-b5a3-f393-e0a9-e50e24dcca9e`, write characteristic
  `6e400002-...`, notify characteristic `6e400003-...`.
- The robot advertises as `m_tiny_car`. The client finds it by that name and the service UUID; there is no
  hard-coded address.
- Writes are split to at most 20 bytes and complete frames are spaced at least 50 ms apart. Without that gap,
  replies went missing intermittently in testing. The cause was not established.
- One BLE central at a time. Keep the robot's pen off and other controllers (phone apps) disconnected.

## Frames

```
F0  dest  71  sub  payload...  checksum  F7
```

Every byte after `F0` is 7-bit (`< 0x80`). `checksum = sum(dest, 0x71, sub, payload) & 0x7F`. Payload is at most
240 bytes. Multi-byte numbers use 7-bit groups, least significant first ("units").

| dest | sub | Meaning |
|---|---|---|
| `01` | `00` | Main controller (host commands below) **(patched radio routes these)** |
| `7D` | `01` | Audio/display controller: eyes, sound, version, echo |

Replies come back as notifications in the same framing and are matched by content; a GATT write
acknowledgment alone never counts as success.

## Audio/display controller (`7D`/`01`)

| Payload | Meaning |
|---|---|
| `60` | Version query |
| `55 len token...` | Echo a 1-32 byte token |
| `30 a_lo a_hi b_lo b_hi` | Show eye animation IDs `a` and `b` (0..127; low 7 bits then high bits) |
| `20 len \path\to\file.mp3 NUL` | Play an MP3 already on the robot's storage (path under `system/`) |

New sounds and artwork cannot be uploaded over Bluetooth; copy MP3s to the robot's USB storage. Playback
requests have no completion acknowledgment.

## Main controller (`01`/`00`), payload starts with `20`

| Payload | Meaning |
|---|---|
| `20 02` | Version text, e.g. `28.01.005.016.7100` |
| `20 7E` | **(patched)** Activity lease: marks recent host activity, advances remote presence, replies `20 7E 00` |
| `20 7D` | **(patched)** 30-byte diagnostics: audio ready, presence stage, idle state, audio error count, ... |
| `20 7B seq` | **(patched)** Motor status |
| `20 7C seq l r t` | **(patched)** Wheel pulse: `l`, `r` are `(value/10)+64`, `t` is duration/10 ms |
| `20 7C seq 40 40 00` | **(patched)** Stop both wheels (also ends a step) |
| `20 7A seq page` | **(patched)** Read-only records: component versions seen by the main controller (pages 0-3) and radio update traffic (4-5) |
| `20 79 seq` | **(patched)** Step status |
| `20 79 seq kind a b up down ms_lo ms_hi` | **(patched)** One step through the robot's own speed controller |

`seq` is a 7-bit sequence number; a repeated sequence is refused as a replay, and a new one may replace a
running pulse or host step.

### Activity lease

The host must renew the lease (`20 7E`) well inside 750 ms; the client renews every 250 ms and gives up if no
acknowledgment arrives for 3 s. Motion commands are refused without a fresh lease and presence, and a step is cut
if host activity stops for more than 750 ms.

### Pulses (`20 7C`)

Raw PWM input units, not calibrated speeds. Channel 0 is the left wheel, channel 1 the right; positive is
forward. Values are multiples of 10 within +-200; duration 10-250 ms in 10 ms steps. The firmware ends each pulse
by its own deadline. `drive` in the server chains pulses for up to 3 s and stops.

### Steps (`20 79`)

Kinds: `forward`, `backward`, `left`, `right` (turn on the spot), `wheels` (independent wheel speeds in `a`, `b`).
`a`, `b` are multiples of 5 within +-200 (forward: speed, about mm/s; turns: about degrees/s). `up`, `down` are
ramp times, multiples of 5 ms up to 635. `ms` is 20-5000 and must contain both ramps. A step that starts from rest
ramps over about 200 ms whatever `up` says. Status carries the stock profile flags and signed wheel encoder
counts. Steps are refused during stock motion, protection conditions, updates, or a running pulse.

## Reply cleanup and safety behaviour in the host code

- `stop` bypasses any pending command and is attempted when the client that started motion disconnects, and on
  server shutdown.
- Commands are never queued or replayed after a reconnect; a busy controller rejects new work (HTTP 409).
- The firmware pulse and step deadlines are software deadlines in the main controller, not an independent
  hardware watchdog.

The HTTP/WebSocket server API is documented in [`../API.md`](../API.md).
