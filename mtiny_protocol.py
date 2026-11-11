"""Bounded host framing for the reviewed mTiny application protocol.

These builders never create firmware, pairing-storage, motor, or power commands.
Application replies, rather than ATT acknowledgments, establish request success.
"""
from dataclasses import dataclass


def frame(destination, subservice, payload):
    payload = bytes(payload)
    if not 0 <= destination <= 255 or not 0 <= subservice < 128:
        raise ValueError("Invalid destination/subservice")
    if any(byte >= 128 for byte in payload) or len(payload) > 240:
        raise ValueError("Payload must contain at most 240 seven-bit bytes")
    body = bytes((destination, 0x71, subservice)) + payload
    return b"\xf0" + body + bytes((sum(body) & 127, 0xf7))


def main_version_request():
    return frame(1, 0, b"\x20\x02")


def audio_version_request():
    return frame(0x7d, 1, b"\x60")


def audio_echo_request(token):
    token = bytes(token)
    if not 1 <= len(token) <= 32:
        raise ValueError("Use a 1–32 byte ASCII token")
    return frame(0x7d, 1, b"\x55" + bytes((len(token),)) + token)


def primary_remote_heartbeat():
    """Requires address-1 NUS routing; remote presence is separate from wake."""
    return frame(1, 3, b"\x0e")


def host_activity_lease_request():
    """Custom main-firmware extension: presence plus expiring activity marker."""
    return frame(1, 0, b"\x20\x7e")


def audio_filename_request(path):
    """Bounded filename playback request; provision the MP3 separately by USB."""
    if not isinstance(path, str) or not path or '\0' in path:
        raise ValueError('Expected a nonempty path without NUL')
    parts = path.replace('\\', '/').strip('/').split('/')
    if parts[0] != 'system' or any(p in ('', '.', '..') for p in parts):
        raise ValueError('Use a file beneath system/ without traversal')
    if not parts[-1].lower().endswith('.mp3'):
        raise ValueError('Only MP3 playback has been reviewed')
    raw = ('\\' + '\\'.join(parts)).encode('ascii') + b'\0'
    if len(raw) >= 120 or any(b >= 128 or (b < 32 and b != 0) for b in raw):
        raise ValueError('Filename must be printable ASCII, below 120 bytes including NUL')
    return frame(0x7d, 1, bytes((0x20, len(raw))) + raw)


def decode_diagnostics(body):
    if len(body) != 30 or body[:2] != b'\x20\x7d':
        raise ValueError('Invalid main diagnostic reply')
    word = lambda b: sum(v << (7*i) for i, v in enumerate(b)) & 0xffffffff
    return dict(audio_ready=body[2], animation_available=body[3], presence_stage=body[4],
                activity_marker=body[5], idle_state=body[6], audio_report_count=word(body[7:12]),
                idle_age_ms=word(body[12:17]), primary_counter=word(body[17:22]),
                audio_error_count=word(body[22:27]), audio_status_a=body[27],
                audio_status_b=body[28], audio_status_c=body[29])


@dataclass(frozen=True)
class Reply:
    raw: bytes

    @property
    def destination(self):
        return self.raw[1]

    @property
    def subservice(self):
        return self.raw[3]

    @property
    def payload(self):
        return self.raw[4:-2]


class FrameDecoder:
    """Reassemble split/coalesced NUS notifications and reject corrupt frames."""
    def __init__(self):
        self.pending = bytearray()
        self.rejected = 0

    def feed(self, data):
        replies = []
        for byte in data:
            if byte == 0xf0:
                if self.pending:
                    self.rejected += 1
                self.pending = bytearray((byte,))
            elif self.pending:
                self.pending.append(byte)
                if byte == 0xf7:
                    raw = bytes(self.pending)
                    self.pending.clear()
                    if (len(raw) >= 6 and raw[2] == 0x71
                            and raw[-2] == (sum(raw[1:-2]) & 127)
                            and all(x < 128 for x in raw[2:-1])):
                        replies.append(Reply(raw))
                    else:
                        self.rejected += 1
                elif len(self.pending) >= 255:
                    self.pending.clear()
                    self.rejected += 1
        return replies
