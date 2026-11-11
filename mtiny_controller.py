"""Lease-aware eyes/audio controller for main .011/.012 and radio .006.

Audio diagnostics require .012. Playback has no proven completion ACK: a write
or echo is never reported as audible success. This API exposes no motor command.
"""
import asyncio
from contextlib import suppress
import secrets
import time

from bleak import BleakClient, BleakScanner
from mtiny_ble import NUS, RX, TX, eye_frame
from mtiny_protocol import (FrameDecoder, frame, main_version_request,
                            host_activity_lease_request, audio_version_request,
                            audio_echo_request, audio_filename_request, decode_diagnostics)


class MTinyController:
    def __init__(self, event=None):
        self.event = event or (lambda kind, **fields: None)
        self.client = None
        self.version = None
        self.decoder = FrameDecoder()
        self.pending = []
        self.write_lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.lease_task = None
        self.failure = None
        self.lease_acks = 0
        self.last_lease_ack = 0
        self.eyes_changed = False
        self.disconnect_task = None
        self.write_response = True
        self.lease_sent = 0
        self.last_frame_write = 0

    def _healthy(self):
        if self.failure:
            raise RuntimeError(f'Control session failed: {self.failure}') from self.failure
        if self.client is None or not self.client.is_connected:
            raise ConnectionError('mTiny is disconnected')

    def _disconnected(self, _):
        self.event('disconnected')
        for _, future in self.pending:
            if not future.done():
                future.set_exception(ConnectionError('mTiny disconnected during request'))

    def _notify(self, _, data):
        self.event('notification_chunk', hex=bytes(data).hex())
        for reply in self.decoder.feed(data):
            self.event('application_frame', hex=reply.raw.hex())
            if (self.lease_task is not None and not self.lease_task.done()
                    and reply.destination == 1 and reply.subservice == 0
                    and reply.payload == b'\x20\x7e\0'):
                self.last_lease_ack = time.monotonic()
                self.lease_acks += 1
                self.event('lease_ack', count=self.lease_acks)
            for predicate, future in self.pending:
                if not future.done() and predicate(reply):
                    future.set_result(reply)
                    break
            if reply.subservice == 1 and reply.payload[:1] in (b'\x50', b'\x51'):
                self.event('audio_status', payload_hex=reply.payload.hex(), interpretation='unclassified')

    async def _write(self, packet):
        self._healthy()
        async with self.write_lock:
            self._healthy()
            # Avoid back-to-back application frames from foreground/renewal
            # tasks. Live missing replies coincided with writes only 9 ms apart.
            await asyncio.sleep(max(0, .05-(time.monotonic()-self.last_frame_write)))
            self._healthy()
            # Conservative 20-byte ATT payloads; hold the lock for the whole frame.
            async with asyncio.timeout(2):
                for offset in range(0, len(packet), 20):
                    await self.client.write_gatt_char(RX, packet[offset:offset+20], response=self.write_response)
            self.last_frame_write = time.monotonic()
            self.event('gatt_write_ack' if self.write_response else 'gatt_write_queued',
                       hex=packet.hex(), application_success=False)

    async def _request(self, packet, matches, timeout=3):
        self._healthy()
        future = asyncio.get_running_loop().create_future()
        entry = (matches, future)
        self.pending.append(entry)  # Register before writing: notifications may arrive immediately.
        try:
            # The serialized write has its own transport timeout. Waiting for that
            # lock is not time spent waiting for a reply to an unsent request.
            await self._write(packet)
            async with asyncio.timeout(timeout):
                return await future
        except Exception as exc:
            self.failure = exc  # Do not accept a delayed untagged reply as a later request.
            self.event('request_failed', request_hex=packet.hex(),
                       type=type(exc).__name__, reason=str(exc),
                       lease_ack_age_seconds=(round(time.monotonic()-self.last_lease_ack, 3)
                                              if self.last_lease_ack else None))
            raise
        finally:
            self.pending.remove(entry)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume a disconnect exception if the write failed first.

    async def main_version(self):
        async with self.operation_lock:
            reply = await self._request(main_version_request(), lambda r:
                r.destination == 1 and r.subservice == 0 and r.payload[:2] == b'\x20\x02'
                and len(r.payload) > 3)
            return reply.payload[3:].decode('ascii')

    async def diagnostics(self):
        async with self.operation_lock:
            reply = await self._request(frame(1, 0, b'\x20\x7d'), lambda r:
                r.destination == 1 and r.subservice == 0 and len(r.payload) == 30
                and r.payload[:2] == b'\x20\x7d')
            result = decode_diagnostics(reply.payload)
            self.event('main_diagnostics', **result)
            return result

    async def presence(self):
        async with self.operation_lock:
            reply = await self._request(frame(1, 0, b'\x56'), lambda r:
                r.destination == 1 and r.subservice == 0 and len(r.payload) == 2
                and r.payload[0] == 0x56 and r.payload[1] <= 2)
            return reply.payload[1]

    async def _renew(self):
        began = time.monotonic()
        try:
            while True:
                started = time.monotonic()
                # Renewal is periodic even when notifications are delayed. ACKs
                # are observed independently; never retry a side-effect command.
                if started - (self.last_lease_ack or began) > 3:
                    raise TimeoutError('No application lease acknowledgment for 3 seconds')
                await self._write(host_activity_lease_request())
                self.lease_sent += 1
                await asyncio.sleep(max(0, .25 - (time.monotonic()-started)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc
            self.event('lease_failed', reason=str(exc), type=type(exc).__name__)
            await self._disconnect()

    def _require_lease(self):
        self._healthy()
        if (self.lease_task is None or self.lease_task.done()
                or time.monotonic()-self.last_lease_ack > 3):
            raise RuntimeError('A fresh acknowledged activity lease is required')

    async def _control_ready(self):
        # Do not issue eyes/playback merely because outbound writes were queued.
        # Wait for recent application receipt and an explicit present-state read.
        async with asyncio.timeout(4):
            while True:
                self._healthy()
                if self.lease_task is None or self.lease_task.done():
                    raise RuntimeError('Activity renewal is not running')
                if time.monotonic()-self.last_lease_ack < .75 and await self.presence() == 2:
                    return
                await asyncio.sleep(.05)

    def _require_audio_bridge(self):
        self._require_lease()
        if self.version not in ('28.01.005.012.7100', '28.01.005.013.7100', '28.01.005.014.7100',
                                '28.01.005.015.7100', '28.01.005.016.7100'):
            raise RuntimeError('Audio diagnostics require the verified .012 or .013 bridge package')

    async def audio_version(self):
        self._require_audio_bridge()
        async with self.operation_lock:
            reply = await self._request(audio_version_request(), lambda r:
                r.destination == 255 and r.subservice == 1 and len(r.payload) == 7
                and r.payload[0] == 0x60)
            return reply.payload.hex()

    async def echo(self, token=None):
        self._require_audio_bridge()
        token = secrets.token_hex(4).encode('ascii') if token is None else bytes(token)
        async with self.operation_lock:
            await self._request(audio_echo_request(token), lambda r:
                r.destination == 255 and r.subservice == 1 and r.payload == b'\x55'+token)
        self.event('audio_echo_verified', token=token.decode('ascii'))
        return token

    async def eyes(self, animation_a=2, animation_b=2):
        packet = eye_frame(animation_a, animation_b)
        await self._control_ready()
        async with self.operation_lock:
            # Track before sending so cleanup also attempts restoration after partial writes.
            self.eyes_changed = (animation_a, animation_b) != (2, 2)
            await self._write(packet)
        self.event('eyes_requested', animation_a=animation_a, animation_b=animation_b,
                   physical_effect_confirmed=False)

    async def play_file(self, path):
        packet = audio_filename_request(path)
        self._require_audio_bridge()
        # Verify the reply route immediately before the one-shot playback write.
        await self.echo()
        await self._control_ready()
        async with self.operation_lock:
            await self._write(packet)
        self.event('playback_requested', path=path, audible_playback_confirmed=False)

    async def hold(self, seconds):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            self._require_lease()
            await asyncio.sleep(min(.1, max(0, deadline-time.monotonic())))

    async def release(self):
        if self.lease_task is None:
            return
        if self.eyes_changed and not self.failure and self.client.is_connected:
            await self.eyes(2, 2)
        self.lease_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.lease_task
        self.lease_task = None
        self.event('lease_stopped', sent=self.lease_sent, acknowledged=self.lease_acks)
        if not self.failure and self.client.is_connected:
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                if await self.presence() == 0:
                    self.event('presence_expired')
                    return
                await asyncio.sleep(.25)
            raise RuntimeError('Presence failed to expire after lease release')

    async def __aenter__(self):
        device = await BleakScanner.find_device_by_filter(lambda d, a:
            (a.local_name or d.name) == 'm_tiny_car' and NUS in a.service_uuids, timeout=12)
        if device is None:
            raise RuntimeError('mTiny not found; no commands sent')
        self.client = BleakClient(device, timeout=10, disconnected_callback=self._disconnected)
        try:
            await self.client.connect()
            self.event('connected', identifier=device.address)
            rx = self.client.services.get_characteristic(RX)
            self.write_response = 'write-without-response' not in rx.properties
            self.event('write_mode', response=self.write_response,
                       properties=list(rx.properties), chunk_bytes=20)
            await self.client.start_notify(TX, self._notify)
            await asyncio.sleep(1)
            self.version = await self.main_version()
            self.event('main_version', version=self.version)
            if self.version not in ('28.01.005.011.7100', '28.01.005.012.7100', '28.01.005.013.7100',
                                    '28.01.005.014.7100', '28.01.005.015.7100',
                                    '28.01.005.016.7100'):
                raise RuntimeError(f'Unsupported main image: {self.version}; no lease sent')
            if await self.presence() != 0:
                raise RuntimeError('Presence already active: stop other controllers/pen first')
            self.lease_task = asyncio.create_task(self._renew())
            deadline = time.monotonic()+4
            while time.monotonic() < deadline:
                await asyncio.sleep(.25)
                if await self.presence() == 2 and self.last_lease_ack:
                    self.event('presence_acquired')
                    return self
            raise RuntimeError('Host presence was not acquired')
        except BaseException:
            await self._close()
            raise

    async def _close(self):
        try:
            await self.release()
        finally:
            # release may fail during restoration; always stop renewal and disconnect.
            if self.lease_task:
                self.lease_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.lease_task
                self.lease_task = None
            await self._disconnect()

    async def _disconnect(self):
        # Cleanup may cancel renewal while it is disconnecting. Keep one owned
        # disconnect task alive and join it before closing the event loop.
        if self.disconnect_task is None and self.client and self.client.is_connected:
            self.disconnect_task = asyncio.create_task(self.client.disconnect())
        if self.disconnect_task is not None:
            await asyncio.shield(self.disconnect_task)

    async def __aexit__(self, exc_type, exc, tb):
        if exc is not None:
            try:
                await self._close()
            except Exception as cleanup_error:
                self.event('cleanup_error', reason=str(cleanup_error))
        else:
            await self._close()
