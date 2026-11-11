"""Host concurrency/failure tests with a simulated BLE peripheral; no hardware."""
import asyncio
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from mtiny_controller import MTinyController
from mtiny_protocol import FrameDecoder, frame, audio_filename_request


class Peripheral:
    version = b'28.01.005.012.7100'
    echo_delay = .65
    drop_lease = False
    drop_echo = False
    frames = []

    def __init__(self, device, timeout, disconnected_callback):
        self.is_connected = False
        self.disconnected_callback = disconnected_callback
        self.decode = FrameDecoder()
        self.last_lease = 0
        self.lease_times = []
        self.handles = []
        self.frames = []
        self.services = SimpleNamespace(get_characteristic=lambda _: SimpleNamespace(
            properties=['write', 'write-without-response']))

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        await asyncio.sleep(getattr(self, 'disconnect_delay', 0))
        self.is_connected = False
        for handle in self.handles:
            handle.cancel()
        self.disconnected_callback(self)

    async def start_notify(self, characteristic, callback):
        self.notify = callback

    def send(self, packet):
        # Fragment every reply and concatenate an unrelated status frame.
        self.notify(None, packet[:3])
        self.notify(None, packet[3:] + frame(255, 1, b'\x56\0'))

    async def write_gatt_char(self, characteristic, data, response):
        assert isinstance(response, bool) and len(data) <= 20
        for request in self.decode.feed(data):
            self.frames.append(request)
            body = request.payload
            if body[:1] == b'\x55' and getattr(self, 'slow_echo_write', False):
                self.slow_echo_write = False
                await asyncio.sleep(1.05)
            if body == b'\x20\x02':
                self.send(frame(1, 0, b'\x20\x02'+bytes((len(self.version),))+self.version))
            elif body == b'\x20\x7e':
                if not self.drop_lease:
                    self.last_lease = time.monotonic()
                    self.lease_times.append(self.last_lease)
                    delay = getattr(self, 'lease_ack_delay', 0)
                    if delay:
                        self.handles.append(asyncio.get_running_loop().call_later(
                            delay, self.send, frame(1, 0, b'\x20\x7e\0')))
                    else:
                        self.send(frame(1, 0, b'\x20\x7e\0'))
            elif body == b'\x56':
                self.send(frame(1, 0, bytes((0x56, 2 if time.monotonic()-self.last_lease < .4 else 0))))
            elif body[:1] == b'\x55' and not self.drop_echo:
                self.handles.append(asyncio.get_running_loop().call_later(
                    self.echo_delay, self.send, frame(255, 1, b'\x55'+body[2:])))
            elif body == b'\x60':
                self.send(frame(255, 1, bytes.fromhex('601c0104000100')))


async def scan(*args, **kwargs):
    return SimpleNamespace(address='SIMULATED')


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [patch('mtiny_controller.BleakScanner.find_device_by_filter', scan),
                        patch('mtiny_controller.BleakClient', Peripheral)]
        for p in self.patches:
            p.start()
        Peripheral.version = b'28.01.005.012.7100'
        Peripheral.drop_echo = Peripheral.drop_lease = False

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    async def test_audio_wait_does_not_starve_lease_and_cleanup_restores(self):
        events = []
        async with MTinyController(lambda kind, **kw: events.append(kind)) as robot:
            await robot.audio_version()
            count = robot.lease_acks
            self.assertEqual(await robot.echo(b'nonce'), b'nonce')
            self.assertGreaterEqual(robot.lease_acks-count, 2)
            await robot.eyes(13, 14)
            await robot.play_file('system/common/audio/demo_hello.mp3')
            self.assertEqual(robot.client.frames[-1].raw,
                             audio_filename_request('system/common/audio/demo_hello.mp3'))
        self.assertFalse(robot.client.is_connected)
        self.assertIsNone(robot.lease_task)
        self.assertFalse(robot.pending)
        self.assertIn('presence_expired', events)
        eyes = [r.payload for r in robot.client.frames if r.subservice == 1 and r.payload[0] == 0x30]
        self.assertEqual(eyes[-1], b'\x30\x02\0\x02\0')

    async def test_missing_lease_closes_connection(self):
        async with MTinyController() as robot:
            robot.client.drop_lease = True
            await asyncio.sleep(3.6)
            self.assertFalse(robot.client.is_connected)
            with self.assertRaises(RuntimeError):
                await robot.eyes(13, 14)
        self.assertFalse(robot.pending)

    async def test_lease_reply_timeout_excludes_waiting_for_write_lock(self):
        async with MTinyController() as robot:
            robot.client.slow_echo_write = True
            await robot.echo(b'queued')
            self.assertIsNone(robot.failure)
            self.assertTrue(robot.client.is_connected)
            self.assertLess(time.monotonic()-robot.last_lease_ack, .5)

    async def test_cleanup_joins_disconnect_started_by_failed_lease(self):
        async with MTinyController() as robot:
            robot.client.disconnect_delay = .4
            robot.client.drop_lease = True
            await asyncio.sleep(3.3)
        self.assertFalse(robot.client.is_connected)
        self.assertTrue(robot.disconnect_task.done())
        self.assertFalse(robot.pending)

    async def test_periodic_renewal_survives_delayed_notifications(self):
        async with MTinyController() as robot:
            robot.client.lease_ack_delay = 1.2
            await asyncio.sleep(2)
            self.assertIsNone(robot.failure)
            times = robot.client.lease_times
            self.assertLess(max(b-a for a, b in zip(times, times[1:])), .4)
            self.assertEqual(await robot.presence(), 2)

    async def test_missing_audio_reply_fails_without_playback(self):
        async with MTinyController() as robot:
            robot.client.drop_echo = True
            with self.assertRaises(TimeoutError):
                await robot.play_file('system/common/audio/demo_hello.mp3')
            self.assertFalse(any(r.subservice == 1 and r.payload[0] == 0x20 for r in robot.client.frames))
        self.assertFalse(robot.client.is_connected)

    async def test_wrong_firmware_never_sends_lease(self):
        Peripheral.version = b'28.01.005.008.7100'
        robot = MTinyController()
        with self.assertRaises(RuntimeError):
            async with robot:
                self.fail('Unsupported firmware entered session')
        self.assertFalse(robot.client.is_connected)
        self.assertFalse(robot.client.lease_times)

    async def test_cancelled_foreground_operation_cleans_up(self):
        robot = MTinyController()
        async def session():
            async with robot:
                await robot.echo()
        task = asyncio.create_task(session())
        while not any(r.payload[:1] == b'\x55' for r in getattr(robot.client, 'frames', [])):
            await asyncio.sleep(.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(robot.client.is_connected)
        self.assertFalse(robot.pending)
        self.assertIsNone(robot.lease_task)

    def test_filename_bounds(self):
        for path in ('../bad.mp3', 'system/../bad.mp3', 'system/hi.wav', 'system/hi\0.mp3',
                     'system/'+'x'*120+'.mp3'):
            with self.assertRaises(ValueError):
                audio_filename_request(path)


if __name__ == '__main__':
    unittest.main()
