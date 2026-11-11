"""Persistent, loopback-only WebSocket owner of the mTiny BLE connection.

Run with `python mtiny_server.py`. Commands are never queued or replayed.
Firmware .013 bounds every motor pulse independently of the host heartbeat.
"""
import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from http import HTTPStatus
import json
import os
import re
from pathlib import Path
import signal
import time
from urllib.parse import parse_qsl, urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from mtiny_movement import MTinyMovement, plan_moves, pulse_frame, step_frame


SEQUENCES = Path(__file__).with_name('sequences.json')
# Generated locally by build_library.py from the user's own robot backup or vendor package.
# Nothing here is shipped with the project; the server and page work without it.
LIBRARY_DIR = Path(os.environ.get('MTINY_LIBRARY_DIR') or Path(__file__).with_name('library'))

# Single source of truth for the commands and fields accepted over WebSocket and HTTP.
SCHEMAS = {'status': set(), 'stop': set(), 'motor_status': set(),
           'diagnostics': set(), 'components': {'ask_radio'},
           'eyes': {'a', 'b'}, 'sound': {'path'},
           'pulse': {'channel0', 'channel1', 'duration_ms'},
           'drive': {'left', 'right', 'ms'}, 'step_status': set(),
           'step': {'kind', 'a', 'b', 'up', 'down', 'ms'},
           'moves': {'steps'}, 'sequences': set(), 'sequence': {'name'},
           'library': set()}
# One owner for every HTTP API call: there is no connection whose close could stop motion.
API_OWNER = object()


def load_library():
    """The generated library, or an empty one (generated=False) when it is missing or unreadable."""
    try:
        data = json.loads((LIBRARY_DIR / 'library.json').read_text())
        return dict(eyes=list(data['eyes']), sounds=list(data['sounds']), stock=list(data['stock']),
                    generated=True)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(eyes=[], sounds=[], stock=[], generated=False)


def load_sequences():
    # Read on every use so edits to sequences.json apply without a restart.
    data = json.loads(SEQUENCES.read_text())
    if not isinstance(data, dict):
        raise ValueError('sequences.json must be an object of named sequences')
    data = {name: dict(item, group='custom') for name, item in data.items()}
    for item in load_library()['stock']:
        data[item['name']] = dict(item, group='built-in')
    return data


class ControlServer:
    def __init__(self, factory=MTinyMovement, retry_seconds=3, log=None):
        self.factory = factory
        self.retry_seconds = retry_seconds
        self.log = log or (lambda record: print(json.dumps(record), flush=True))
        self.robot = None
        self.ready = False
        self.generation = 0
        self.last_error = None
        self.busy = False
        self.motion_owner = None
        self.stop_task = None
        self.clients = set()
        self.shutdown = asyncio.Event()

    def event(self, kind, **fields):
        # Heartbeat and raw notification callbacks must remain cheap.
        if kind in ('notification_chunk', 'application_frame', 'lease_ack',
                    'gatt_write_queued', 'gatt_write_ack'):
            return
        # Controller failures include an exception `type`. Keep it distinct
        # from the WebSocket envelope so logging cannot interrupt BLE cleanup.
        if 'type' in fields:
            fields['exception_type'] = fields.pop('type')
        record = dict(fields, type='event', event=kind,
                      time=datetime.now(timezone.utc).isoformat())
        self.log(record)
        for queue in tuple(self.clients):
            if not queue.full():
                queue.put_nowait(record)

    def status(self):
        r = self.robot
        healthy = self.ready and r is not None
        if healthy:
            try:
                r._require_lease()
            except Exception:
                healthy = False
        return dict(ready=healthy, generation=self.generation, busy=self.busy,
                    version=r.version if r else None, last_error=self.last_error,
                    lease_ack_age_seconds=round(time.monotonic()-r.last_lease_ack, 3)
                    if r and r.last_lease_ack else None,
                    lease_acknowledgments=r.lease_acks if r else 0,
                    limits=dict(max_input=200, max_duration_ms=250,
                                max_drive_ms=3000,
                                physical_channel_mapping_confirmed=True,
                                channel0='left wheel', channel1='right wheel',
                                positive='forward'))

    async def connection_loop(self):
        while not self.shutdown.is_set():
            try:
                self.event('connecting')
                async with self.factory(event=self.event) as robot:
                    self.robot = robot
                    # A new session starts at zero; no previous commands survive it.
                    await robot.stop_motors()
                    self.generation += 1
                    self.ready = True
                    self.last_error = None
                    self.event('ready', **self.status())
                    try:
                        while not self.shutdown.is_set():
                            robot._require_lease()
                            try:
                                await asyncio.wait_for(self.shutdown.wait(), .2)
                            except TimeoutError:
                                pass
                    finally:
                        self.ready = False
                        self.motion_owner = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f'{type(exc).__name__}: {exc}'
                self.event('connection_unavailable', reason=self.last_error)
            finally:
                self.ready = False
                self.robot = None
            if not self.shutdown.is_set():
                try:
                    await asyncio.wait_for(self.shutdown.wait(), self.retry_seconds)
                except TimeoutError:
                    pass

    async def stop(self, robot):
        # Coalesce simultaneous stop requests; don't create an unbounded queue.
        if self.stop_task is None or self.stop_task.done():
            self.stop_task = asyncio.create_task(robot.stop_motors())
        try:
            return await asyncio.shield(self.stop_task)
        finally:
            self.motion_owner = None

    async def command(self, msg, owner):
        if not isinstance(msg, dict):
            raise ValueError('Expected a JSON object')
        cmd = msg.get('command')
        if not isinstance(cmd, str) or cmd not in SCHEMAS:
            raise ValueError('Unknown command')
        if set(msg) - SCHEMAS[cmd] - {'command', 'id'}:
            raise ValueError('Unexpected command fields')
        if cmd == 'status':
            return self.status()
        if cmd == 'sequences':
            return dict(sequences=[dict(name=name, label=item.get('label', name), group=item['group'],
                                        steps=len(item['steps']))
                                   for name, item in load_sequences().items()])
        if cmd == 'library':
            library = load_library()
            return dict(eyes=library['eyes'], sounds=library['sounds'], generated=library['generated'])
        if not self.status()['ready']:
            raise ConnectionError('Bluetooth is not ready; command was not queued')
        robot = self.robot
        if cmd == 'stop':
            return await self.stop(robot)
        if self.busy or (self.stop_task and not self.stop_task.done()):
            raise RuntimeError('Controller busy; command was not queued. Stop remains available.')
        self.busy = True
        try:
            if cmd == 'motor_status':
                return await robot.motor_status()
            if cmd == 'diagnostics':
                return await robot.diagnostics()
            if cmd == 'components':
                ask = msg.get('ask_radio', False)
                if type(ask) is not bool:
                    raise ValueError('ask_radio must be true or false')
                return await robot.component_report(ask_radio=ask)
            if cmd == 'eyes':
                a, b = msg.get('a', 2), msg.get('b', 2)
                if any(type(v) is not int or not 0 <= v <= 127 for v in (a, b)):
                    raise ValueError('Eye animation IDs must be integers 0..127')
                await robot.eyes(a, b)
                return dict(requested=True, physical_effect_confirmed=False)
            if cmd == 'sound':
                path = msg.get('path')
                if not isinstance(path, str) or not path.startswith('system/') or '..' in path.split('/'):
                    raise ValueError('Sound path must name an installed file under system/')
                await robot.play_file(path)
                return dict(requested=True, audible_playback_confirmed=False)
            if cmd == 'pulse':
                a, b, ms = (msg.get('channel0', 0), msg.get('channel1', 0),
                            msg.get('duration_ms', 250))
                pulse_frame(0, a, b, ms)  # Validate before recording motion ownership.
                if ms == 0:
                    raise ValueError('Use stop for zero duration')
                self.motion_owner = owner
                return await robot.pulse(a, b, ms)
            if cmd == 'step_status':
                return await robot.step_status()
            if cmd == 'step':
                args = (msg.get('kind'), msg.get('a', 0), msg.get('b', 0),
                        msg.get('up', 0), msg.get('down', 0), msg.get('ms', 300))
                step_frame(0, *args)  # Validate before recording motion ownership.
                self.motion_owner = owner
                return await robot.step(*args)
            if cmd == 'moves':
                plan_moves(msg.get('steps'))  # Validate before recording motion ownership.
                self.motion_owner = owner
                return await robot.moves(msg.get('steps'))
            if cmd == 'sequence':
                item = load_sequences().get(msg.get('name'))
                if item is None:
                    raise ValueError('Unknown sequence name')
                plan_moves(item['steps'])
                self.motion_owner = owner
                return dict(await robot.moves(item['steps']), name=msg['name'])
            if cmd == 'drive':
                left, right, ms = msg.get('left', 0), msg.get('right', 0), msg.get('ms', 600)
                pulse_frame(0, left, right, 250)  # Validate before recording motion ownership.
                if type(ms) is not int or not 50 <= ms <= 3000:
                    raise ValueError('Drive time must be 50..3000 ms')
                if not (left or right):
                    raise ValueError('Use stop for zero power')
                self.motion_owner = owner
                return await robot.drive(left, right, ms)
        finally:
            self.busy = False

    async def handle(self, ws):
        queue = asyncio.Queue(maxsize=64)
        self.clients.add(queue)
        owner = object()
        tasks = set()
        client_closed = False

        async def sender():
            while True:
                await ws.send(json.dumps(await queue.get()))

        async def reply(raw):
            request_id = None
            try:
                if client_closed:
                    return
                msg = json.loads(raw)
                if isinstance(msg, dict):
                    request_id = msg.get('id')
                result = await self.command(msg, owner)
                response = dict(type='response', id=request_id, ok=True, result=result)
            except Exception as exc:
                response = dict(type='response', id=request_id, ok=False,
                                error=f'{type(exc).__name__}: {exc}')
            # A slow client must not retain command tasks or stop the BLE owner.
            if queue.full():
                await ws.close(code=1013, reason='Client is not reading responses')
            else:
                queue.put_nowait(response)

        writer = asyncio.create_task(sender())
        queue.put_nowait(dict(type='status', **self.status()))
        try:
            async for raw in ws:
                if len(tasks) >= 8:
                    await ws.close(code=1013, reason='Too many outstanding commands')
                    break
                task = asyncio.create_task(reply(raw))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except ConnectionClosed:
            pass
        finally:
            client_closed = True
            self.clients.discard(queue)
            # Let in-flight requests finish so delayed untagged replies cannot
            # be mistaken for later requests. Stop invalidates an unsent pulse.
            if self.motion_owner is owner and self.robot and self.ready:
                try:
                    await self.stop(self.robot)
                except Exception as exc:
                    self.event('client_cleanup_failed', reason=str(exc))
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.cancel()
            with suppress(asyncio.CancelledError, ConnectionClosed):
                await writer


def parse_api_query(query):
    """Decode ?a=1&b=true&c=text into command fields (JSON where it parses, else string)."""
    fields = {}
    for key, value in parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query)):
        if key in fields:
            raise ValueError(f'Repeated query field: {key}')
        if key in ('id', 'command'):
            raise ValueError(f'Field not accepted from the API: {key}')
        try:
            fields[key] = json.loads(value)
        except ValueError:
            fields[key] = value
    return fields


def api_response(status, payload, cors=None):
    body = json.dumps(payload).encode()
    headers = [('Content-Type', 'application/json'), ('Content-Length', str(len(body))),
               ('Cache-Control', 'no-store'), ('Connection', 'close')]
    if cors:  # Exact allowed origin only, never '*', never credentials.
        headers += [('Access-Control-Allow-Origin', cors), ('Vary', 'Origin')]
    return Response(status, HTTPStatus(status).phrase, Headers(headers), body)


def api_error(status, exc, cors=None):
    return api_response(status, dict(ok=False, error=f'{type(exc).__name__}: {exc}'), cors)


def http_handler(control, port, extra_hosts=(), origin_suffixes=(), cors_origins=()):
    allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    allowed_origins = {f'http://{host}' for host in allowed_hosts}
    for host in extra_hosts:
        allowed_hosts |= {host, f'{host}:443'}
        allowed_origins.add(f'https://{host}')

    cors_patterns = [re.compile(rf'https://[a-z0-9-]+\.{re.escape(suffix.strip(".").lower())}',
                                re.IGNORECASE) for suffix in origin_suffixes]

    exact_origins = {origin.rstrip('/').lower() for origin in cors_origins}

    def cors_allowed(origin):
        return origin is not None and (origin.lower() in exact_origins
                                       or any(p.fullmatch(origin) for p in cors_patterns))

    async def api(request, url, cors):
        site = request.headers.get('Sec-Fetch-Site')
        if not cors and site is not None and site not in ('same-origin', 'none'):
            return api_error(403, PermissionError('Cross-site requests are not allowed'))
        parts = url.path.split('/')
        if url.path in ('/api', '/api/'):
            return api_response(200, dict(
                ok=True, commands={name: sorted(fields) for name, fields in SCHEMAS.items()},
                usage='GET /api/<command>?field=value; values that parse as JSON are decoded as JSON',
                examples=['/api/status', '/api/stop', '/api/sequence?name=hello',
                          '/api/drive?left=200&right=200&ms=600',
                          '/api/moves?steps=%5B%7B%22kind%22%3A%22forward%22%2C%22mm%22%3A100%7D%5D',
                          '/api/eyes?a=2&b=2']), cors)
        if len(parts) != 3 or parts[2] not in SCHEMAS:
            return api_error(404, LookupError('Unknown command'), cors)
        try:
            fields = parse_api_query(url.query)
            result = await control.command(dict(fields, command=parts[2]), API_OWNER)
            return api_response(200, dict(ok=True, result=result), cors)
        except ValueError as exc:
            return api_error(400, exc, cors)
        except ConnectionError as exc:
            return api_error(503, exc, cors)
        except RuntimeError as exc:
            return api_error(409 if 'busy' in str(exc).lower() else 500, exc, cors)
        except Exception as exc:
            return api_error(500, exc, cors)

    async def process_request(connection, request):
        if request.headers.get('Host') not in allowed_hosts:
            return connection.respond(HTTPStatus.FORBIDDEN, 'Local host required\n')
        origin = request.headers.get('Origin')
        url = urlsplit(request.path)
        is_api = url.path == '/api' or url.path.startswith('/api/')
        cors = origin if is_api and cors_allowed(origin) else None
        if origin is not None and origin not in allowed_origins and not cors:
            return connection.respond(HTTPStatus.FORBIDDEN, 'Local origin required\n')
        if is_api:
            return await api(request, url, cors)
        if request.path == '/ws':
            return None
        if request.path == '/':
            response = connection.respond(HTTPStatus.OK,
                Path(__file__).with_name('mtiny_control.html').read_text())
            response.headers['Content-Type'] = 'text/html; charset=utf-8'
            return response
        match = re.fullmatch(r'/eyes/(\d{1,3})\.gif', request.path)
        gif = LIBRARY_DIR / 'eyes' / f'animation-{int(match[1]):02d}.gif' if match else None
        if gif and gif.is_file():
            body = gif.read_bytes()
            return Response(200, 'OK', Headers([('Content-Type', 'image/gif'), ('Content-Length', str(len(body))),
                                                ('Connection', 'close')]), body)
        if request.path == '/health':
            response = connection.respond(HTTPStatus.OK, json.dumps(control.status()))
            response.headers['Content-Type'] = 'application/json'
            return response
        return connection.respond(HTTPStatus.NOT_FOUND, 'Not found\n')
    return process_request


async def main(port, extra_hosts=(), origin_suffixes=(), cors_origins=()):
    control = ControlServer()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, control.shutdown.set)
    # Bind before scanning: a second server fails without taking a BLE connection.
    async with serve(control.handle, '127.0.0.1', port,
                     process_request=http_handler(control, port, extra_hosts, origin_suffixes, cors_origins),
                     origins=[None, f'http://127.0.0.1:{port}', f'http://localhost:{port}',
                              *(f'https://{host}' for host in extra_hosts)],
                     max_size=4096, max_queue=8, compression=None, close_timeout=2):
        task = asyncio.create_task(control.connection_loop())
        control.event('server_started', url=f'http://127.0.0.1:{port}',
                      websocket=f'ws://127.0.0.1:{port}/ws')
        await control.shutdown.wait()
        control.ready = False
        if control.robot:
            with suppress(Exception):
                await control.stop(control.robot)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        control.event('server_stopped')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--library-dir', type=Path, default=None, metavar='DIR',
                        help='generated library folder (default: ./library next to this file, or $MTINY_LIBRARY_DIR)')
    parser.add_argument('--allow-host', action='append', default=[], metavar='HOST',
                        help='extra Host name accepted (repeatable), e.g. a reverse proxy name')
    parser.add_argument('--allow-origin', action='append', default=[], metavar='ORIGIN',
                        help='allow CORS API calls from this exact origin (repeatable), e.g. http://localhost:8000')
    parser.add_argument('--allow-origin-suffix', action='append', default=[], metavar='SUFFIX',
                        help='allow CORS API calls from https://<label>.SUFFIX (repeatable), e.g. tail1234.ts.net')
    args = parser.parse_args()
    if args.library_dir:
        LIBRARY_DIR = args.library_dir
    asyncio.run(main(args.port, tuple(args.allow_host), tuple(args.allow_origin_suffix),
                     tuple(args.allow_origin)))
