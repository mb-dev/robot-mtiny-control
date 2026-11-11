"""Real local WebSocket transport with a simulated BLE controller; no hardware."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
import mtiny_server
from mtiny_server import ControlServer, http_handler


def write_library(folder, eyes=3, sounds=2, stock=17):
    """A tiny synthetic library: the real one is generated locally from vendor files."""
    folder = Path(folder)
    (folder / 'eyes').mkdir(parents=True, exist_ok=True)
    (folder / 'eyes' / 'animation-04.gif').write_bytes(b'GIF89a')
    library = dict(
        eyes=[dict(id=i + 2, name=f'eye {i}', frames=4) for i in range(eyes)],
        sounds=[dict(path=f'system/common/audio/s{i}.mp3', seconds=1.0, label=f's{i}', known=True)
                for i in range(sounds)],
        stock=[dict(name=f'stock_{i}', label=f'Built-in move {i}',
                    steps=[dict(kind='forward', a=100, b=0, up=10, down=10, ms=300)] * (5 if i == 10 else 1))
               for i in range(stock)])
    (folder / 'library.json').write_text(json.dumps(library))


class FakeRobot:
    def __init__(self, event=None):
        self.version = '28.01.005.013.7100'
        self.last_lease_ack = time.monotonic()
        self.lease_acks = 1
        self.broken = False
        self.closed = False
        self.calls = []
        self.block = asyncio.Event()
        self.epoch = 0

    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    def _require_lease(self):
        if self.broken: raise ConnectionError('Simulated loss')
    async def stop_motors(self):
        self.epoch += 1
        self.calls.append('stop')
        return dict(active=0, left=0, right=0)
    async def diagnostics(self):
        self.calls.append('diagnostics')
        await self.block.wait()
        return dict(audio_error_count=0)
    async def motor_status(self): return dict(active=0, left=0, right=0)
    async def step(self, *args):
        self.calls.append(('step',) + args)
        return dict(result=0, owned=1)
    async def moves(self, steps):
        self.calls.append(('moves', len(steps)))
        return dict(steps_run=len(steps), stopped_early=False)
    async def drive(self, left, right, ms):
        self.calls.append(('drive', left, right, ms))
        return dict(pulses=3)
    async def pulse(self, a, b, ms):
        self.calls.append(('pulse', a, b, ms))
        return dict(active=1)
    async def eyes(self, a, b): self.calls.append(('eyes', a, b))
    async def play_file(self, path): self.calls.append(('sound', path))


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.library_dir = tempfile.TemporaryDirectory()
        write_library(self.library_dir.name)
        self.library_patch = patch.object(mtiny_server, 'LIBRARY_DIR', Path(self.library_dir.name))
        self.library_patch.start()
        self.robots = []
        def factory(**kwargs):
            r = FakeRobot(**kwargs)
            self.robots.append(r)
            return r
        self.control = ControlServer(factory, retry_seconds=.15, log=lambda _: None)
        self.loop_task = asyncio.create_task(self.control.connection_loop())
        await self.until(lambda: self.control.ready)
        self.server = await serve(self.control.handle, '127.0.0.1', 0)
        self.url = f'ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/ws'

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.control.shutdown.set()
        await self.loop_task
        self.library_patch.stop()
        self.library_dir.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate(): await asyncio.sleep(.01)

    async def response(self, ws, id):
        async with asyncio.timeout(2):
            while True:
                data = json.loads(await ws.recv())
                if data.get('type') == 'response' and data.get('id') == id: return data

    async def test_shared_connection_and_disconnect_stop(self):
        async with connect(self.url) as a, connect(self.url) as b:
            await a.send(json.dumps(dict(id=1, command='pulse', channel0=200)))
            self.assertTrue((await self.response(a, 1))['ok'])
            await b.send(json.dumps(dict(id=2, command='status')))
            self.assertTrue((await self.response(b, 2))['result']['ready'])
        await self.until(lambda: self.robots[0].calls.count('stop') == 2)
        self.assertEqual(len(self.robots), 1)
        self.assertTrue(self.control.ready)
        self.assertFalse(self.robots[0].closed)

    async def test_stop_bypasses_busy_same_socket(self):
        async with connect(self.url) as ws:
            await ws.send('{"id":1,"command":"diagnostics"}')
            await self.until(lambda: self.control.busy)
            await ws.send('{"id":2,"command":"pulse","channel0":200}')
            self.assertFalse((await self.response(ws, 2))['ok'])
            await ws.send('{"id":3,"command":"stop"}')
            self.assertTrue((await self.response(ws, 3))['ok'])
            self.assertEqual(self.robots[0].calls, ['stop', 'diagnostics', 'stop'])
            self.robots[0].block.set()
            self.assertTrue((await self.response(ws, 1))['ok'])

    async def test_reconnect_never_replays(self):
        owner = object()
        await self.control.command(dict(command='pulse', channel0=200), owner)
        self.robots[0].broken = True
        with self.assertRaises(ConnectionError):
            await self.control.command(dict(command='pulse', channel0=200), owner)
        await self.until(lambda: len(self.robots) == 2 and self.control.ready)
        self.assertEqual(self.robots[1].calls, ['stop'])
        self.assertEqual(self.control.generation, 2)

    async def test_invalid_requests_have_no_effects(self):
        async with connect(self.url) as ws:
            for i, request in enumerate(([], dict(command='pulse', channel0=210),
                    dict(command='pulse', channel0=True), dict(command='pulse', duration_ms=0),
                    dict(command='pulse', duration_ms=260), dict(command='pulse', extra=1),
                    dict(command='sound', path='../x.mp3'), dict(command='raw'))):
                if isinstance(request, dict): request['id'] = i
                await ws.send(json.dumps(request))
                self.assertFalse((await self.response(ws, i if isinstance(request, dict) else None))['ok'])
        self.assertEqual(self.robots[0].calls, ['stop'])

    async def test_failure_event_preserves_envelope_and_exception(self):
        records = []
        self.control.log = records.append
        self.control.event('lease_failed', type='TimeoutError', reason='No acknowledgment')
        self.assertEqual(records[-1]['type'], 'event')
        self.assertEqual(records[-1]['exception_type'], 'TimeoutError')

    async def test_real_controller_lease_failure_disconnects_with_server_logger(self):
        from mtiny_controller import MTinyController
        class Client:
            is_connected = True
            async def disconnect(self): self.is_connected = False
        robot = MTinyController(event=self.control.event)
        robot.client = Client()
        robot.last_lease_ack = time.monotonic() - 4
        await robot._renew()
        self.assertIsInstance(robot.failure, TimeoutError)
        self.assertFalse(robot.client.is_connected)

    async def test_step_and_moves_validate_and_reach_robot(self):
        async with connect(self.url) as ws:
            await ws.send(json.dumps(dict(id=1, command='step', kind='left', a=185, up=100, down=100, ms=2000)))
            self.assertTrue((await self.response(ws, 1))['ok'])
            await ws.send(json.dumps(dict(id=2, command='moves', steps=[dict(kind='forward', a=100, ms=300)])))
            self.assertTrue((await self.response(ws, 2))['ok'])
            for i, bad in enumerate((dict(kind='spin'), dict(kind='forward', a=205), dict(kind='forward', ms=5001),
                                     dict(kind='forward', speed=1)), 3):
                await ws.send(json.dumps(dict(id=i, command='step', **bad)))
                self.assertFalse((await self.response(ws, i))['ok'])
        self.assertEqual([c for c in self.robots[0].calls if c[0] in ('step', 'moves')],
                         [('step', 'left', 185, 0, 100, 100, 2000), ('moves', 1)])

    async def test_named_sequences_list_validate_and_run(self):
        from mtiny_movement import plan_moves
        from mtiny_server import load_sequences
        for name, item in load_sequences().items():
            plan_moves(item['steps'])
        async with connect(self.url) as ws:
            await ws.send(json.dumps(dict(id=1, command='sequences')))
            listed = (await self.response(ws, 1))['result']['sequences']
            self.assertIn('hello', [q['name'] for q in listed])
            await ws.send(json.dumps(dict(id=2, command='sequence', name='hello')))
            self.assertEqual((await self.response(ws, 2))['result']['name'], 'hello')
            await ws.send(json.dumps(dict(id=3, command='sequence', name='nope')))
            self.assertFalse((await self.response(ws, 3))['ok'])
            await ws.send(json.dumps(dict(id=4, command='sequence', name='stock_10')))
            self.assertTrue((await self.response(ws, 4))['ok'])
            await ws.send(json.dumps(dict(id=5, command='library')))
            library = (await self.response(ws, 5))['result']
            self.assertEqual((len(library['eyes']), len(library['sounds'])), (3, 2))
            self.assertTrue(library['generated'])
            self.assertEqual(len([q for q in listed if q['group'] == 'built-in']), 17)
        self.assertEqual([c for c in self.robots[0].calls if c[0] == 'moves'], [('moves', 8), ('moves', 5)])

    async def test_drive_validates_and_reaches_robot(self):
        async with connect(self.url) as ws:
            await ws.send(json.dumps(dict(id=1, command='drive', left=200, right=200, ms=600)))
            self.assertTrue((await self.response(ws, 1))['ok'])
            for i, bad in enumerate((dict(left=205), dict(left=200, ms=3001), dict(left=0, right=0), dict(left=200, speed=1)), 2):
                await ws.send(json.dumps(dict(id=i, command='drive', **bad)))
                self.assertFalse((await self.response(ws, i))['ok'])
        self.assertEqual([c for c in self.robots[0].calls if c[0] == 'drive'], [('drive', 200, 200, 600)])

    async def test_http_panel_and_origin_restrictions(self):
        self.server.close()
        await self.server.wait_closed()
        # Reserve the port, then apply the production handshake handler.
        self.server = await serve(self.control.handle, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.server.process_request = http_handler(self.control, port)
        url = f'ws://127.0.0.1:{port}/ws'
        async with connect(url, origin=f'http://127.0.0.1:{port}') as ws:
            self.assertEqual(json.loads(await ws.recv())['type'], 'status')
        from websockets.exceptions import InvalidStatus
        with self.assertRaises(InvalidStatus):
            async with connect(url, origin='https://example.com'): pass
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(f'GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n'.encode())
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        self.assertIn(b'200 OK', response)
        self.assertIn(b'<title>mTiny control</title>', response)
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(f'GET /eyes/4.gif HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n'.encode())
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        self.assertIn(b'200 OK', response)
        self.assertIn(b'GIF8', response)

    async def http_get(self, port, path, host=None, **headers):
        host = host or f'127.0.0.1:{port}'
        extra = ''.join(f'{k.replace("_", "-")}: {v}\r\n' for k, v in headers.items())
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(f'GET {path} HTTP/1.1\r\nHost: {host}\r\n{extra}Connection: close\r\n\r\n'.encode())
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        head, _, body = raw.partition(b'\r\n\r\n')
        status = int(head.split()[1])
        try: data = json.loads(body)
        except ValueError: data = None
        return status, head.decode(), data

    async def api_port(self, **kwargs):
        self.server.close()
        await self.server.wait_closed()
        self.server = await serve(self.control.handle, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.server.process_request = http_handler(self.control, port, **kwargs)
        return port

    async def test_http_api(self):
        from urllib.parse import quote
        port = await self.api_port(extra_hosts=('tiny.example.ts.net',))
        status, head, data = await self.http_get(port, '/api')
        self.assertEqual(status, 200)
        self.assertIn('application/json', head)
        self.assertIn('Cache-Control: no-store', head)
        self.assertEqual(data['commands']['drive'], ['left', 'ms', 'right'])
        status, _, data = await self.http_get(port, '/api/status')
        self.assertEqual((status, data['ok'], data['result']['ready']), (200, True, True))
        status, _, data = await self.http_get(port, '/api/drive?left=200&right=200&ms=600')
        self.assertEqual(status, 200)
        status, _, data = await self.http_get(port, '/api/sequence?name=hello')
        self.assertEqual((status, data['result']['name']), (200, 'hello'))
        steps = quote(json.dumps([dict(kind='forward', a=100, ms=300)]))
        status, _, data = await self.http_get(port, f'/api/moves?steps={steps}')
        self.assertEqual(status, 200, data)
        calls = self.robots[0].calls
        self.assertIn(('drive', 200, 200, 600), calls)
        self.assertIn(('moves', 1), calls)
        # Finishing API calls must not stop the wheels.
        self.assertEqual(calls.count('stop'), 1)
        self.assertIs(self.control.motion_owner, __import__('mtiny_server').API_OWNER)
        status, _, data = await self.http_get(port, '/api/drive?left=999')
        self.assertEqual(status, 400)
        self.assertFalse(data['ok'])
        self.assertTrue(data['error'].startswith('ValueError: '))
        for bad in ('/api/drive?left=1&left=2', '/api/drive?id=5&left=100', '/api/nope'):
            status, _, data = await self.http_get(port, bad)
            self.assertEqual(status, 404 if 'nope' in bad else 400)
        status, _, data = await self.http_get(port, '/api/stop')
        self.assertEqual((status, data['ok']), (200, True))
        self.assertIsNone(self.control.motion_owner)
        self.assertEqual(self.robots[0].calls[-1], 'stop')

    async def test_http_api_security(self):
        port = await self.api_port(extra_hosts=('tiny.example.ts.net',))
        status, _, _ = await self.http_get(port, '/api/drive?left=200', Sec_Fetch_Site='cross-site')
        self.assertEqual(status, 403)
        status, _, _ = await self.http_get(port, '/api/drive?left=200', Sec_Fetch_Site='same-site')
        self.assertEqual(status, 403)
        status, _, _ = await self.http_get(port, '/api/status', Sec_Fetch_Site='same-origin')
        self.assertEqual(status, 200)
        status, _, _ = await self.http_get(port, '/api/status', Sec_Fetch_Site='none')
        self.assertEqual(status, 200)
        status, _, _ = await self.http_get(port, '/api/status', Origin='https://example.com')
        self.assertEqual(status, 403)
        self.assertNotIn(('drive', 200, 0, 600), self.robots[0].calls)
        for host in ('tiny.example.ts.net', 'tiny.example.ts.net:443'):
            status, _, _ = await self.http_get(port, '/api/status', host=host,
                                               Origin='https://tiny.example.ts.net')
            self.assertEqual(status, 200)
        status, _, _ = await self.http_get(port, '/api/status', host='evil.example')
        self.assertEqual(status, 403)
        status, _, _ = await self.http_get(port, '/api/status', host='tiny.example.ts.net:8080')
        self.assertEqual(status, 403)

    async def test_http_api_exact_origin(self):
        port = await self.api_port(cors_origins=('http://localhost:8000',))
        status, head, data = await self.http_get(port, '/api/status', Origin='http://localhost:8000',
                                                 Sec_Fetch_Site='same-site')
        self.assertEqual(status, 200, data)
        self.assertIn('Access-Control-Allow-Origin: http://localhost:8000', head)
        status, _, _ = await self.http_get(port, '/api/status', Origin='http://localhost:8001',
                                           Sec_Fetch_Site='same-site')
        self.assertEqual(status, 403)

    async def test_http_api_cors(self):
        port = await self.api_port(origin_suffixes=('tail1234.ts.net',))
        for site in ('cross-site', 'same-site'):
            status, head, data = await self.http_get(
                port, '/api/drive?left=200&right=200&ms=600', Origin='https://spell.tail1234.ts.net',
                Sec_Fetch_Site=site)
            self.assertEqual(status, 200, data)
            self.assertIn('Access-Control-Allow-Origin: https://spell.tail1234.ts.net', head)
            self.assertIn('Vary: Origin', head)
            self.assertNotIn('Allow-Credentials', head)
        status, head, _ = await self.http_get(port, '/api/drive?left=999', Origin='https://SPELL.tail1234.ts.net')
        self.assertEqual(status, 400)
        self.assertIn('Access-Control-Allow-Origin: https://SPELL.tail1234.ts.net', head)
        status, head, _ = await self.http_get(port, '/api/nope', Origin='https://spell.tail1234.ts.net')
        self.assertEqual(status, 404)
        self.assertIn('Access-Control-Allow-Origin', head)
        before = len(self.robots[0].calls)
        for origin in ('https://spell.tail1234.ts.net.evil.com', 'https://a.b.tail1234.ts.net',
                       'http://spell.tail1234.ts.net', 'https://spell.tail1234.ts.net:8443',
                       'https://tail1234.ts.net', 'https://spell.tail12345.ts.net'):
            status, head, _ = await self.http_get(port, '/api/drive?left=200', Origin=origin)
            self.assertEqual(status, 403, origin)
            self.assertNotIn('Access-Control', head)
        self.assertEqual(len(self.robots[0].calls), before)
        # CORS does not extend beyond /api.
        for path in ('/', '/health'):
            status, head, _ = await self.http_get(port, path, Origin='https://spell.tail1234.ts.net')
            self.assertEqual(status, 403)




class LibraryOptionalTests(unittest.TestCase):
    """The server and page must work, degraded, before build_library.py has been run."""

    def test_missing_library_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as empty, patch.object(mtiny_server, 'LIBRARY_DIR', Path(empty)):
            library = mtiny_server.load_library()
            self.assertEqual((library['eyes'], library['sounds'], library['stock'], library['generated']),
                             ([], [], [], False))
            names = mtiny_server.load_sequences()
            self.assertIn('hello', names)
            self.assertFalse([n for n in names if n.startswith('stock_')])

    def test_corrupt_library_is_treated_as_missing(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(mtiny_server, 'LIBRARY_DIR', Path(folder)):
            (Path(folder) / 'library.json').write_text('{not json')
            self.assertFalse(mtiny_server.load_library()['generated'])
            (Path(folder) / 'library.json').write_text('{"eyes": []}')
            self.assertFalse(mtiny_server.load_library()['generated'])

    def test_shipped_sequences_validate_without_library(self):
        from mtiny_movement import plan_moves
        with tempfile.TemporaryDirectory() as empty, patch.object(mtiny_server, 'LIBRARY_DIR', Path(empty)):
            for item in mtiny_server.load_sequences().values():
                plan_moves(item['steps'])

if __name__ == '__main__': unittest.main()
