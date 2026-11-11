"""Send one JSON command to the existing server; never opens a BLE connection."""
import argparse
import asyncio
import json
import uuid
from websockets.asyncio.client import connect


async def send(command, url):
    request = json.loads(command) if command.startswith('{') else {'command': command}
    if not isinstance(request, dict):
        raise ValueError('Expected command name or JSON object')
    request.setdefault('id', uuid.uuid4().hex)
    async with connect(url) as socket:
        await socket.send(json.dumps(request))
        async with asyncio.timeout(15):
            async for raw in socket:
                response = json.loads(raw)
                if response.get('type') == 'response' and response.get('id') == request['id']:
                    print(json.dumps(response, indent=2))
                    if not response['ok']:
                        raise SystemExit(1)
                    # For pulses, keep the owner connected beyond the MCU cap.
                    # Closing earlier deliberately invokes the server's stop path.
                    if request.get('command') == 'pulse':
                        await asyncio.sleep(.3)
                    return
        raise ConnectionError('Server closed before returning a response')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', help='Command name or complete JSON object')
    parser.add_argument('--url', default='ws://127.0.0.1:8765/ws')
    args = parser.parse_args()
    asyncio.run(send(args.command, args.url))
