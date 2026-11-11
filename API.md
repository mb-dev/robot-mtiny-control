# mTiny HTTP API

A small GET-only JSON API on the same server as the control page. Every WebSocket command is available.

Base URL: `http://127.0.0.1:8765` on the robot's computer, or e.g. `https://tiny.example.ts.net` through a reverse proxy (see Security).
`GET /api` lists the commands and example URLs.

## Commands

Call `GET /api/<command>?field=value&...`. Values that parse as JSON are decoded as JSON (`ms=400` is a number, `ask_radio=true` a boolean, `steps=[...]` a list); anything else is a string (`name=hello`). URL-encode JSON values. Repeating a field is an error, and `id` is not accepted.

| Command | Fields |
|---|---|
| status, stop, motor_status, diagnostics, step_status, sequences, library | none |
| components | ask_radio |
| eyes | a, b (animation IDs 0..127) |
| sound | path (under `system/`) |
| pulse | channel0, channel1, duration_ms |
| drive | left, right, ms |
| step | kind, a, b, up, down, ms |
| moves | steps (list of step objects) |
| sequence | name |

`library` returns the eye and sound lists from `library/library.json`; the built-in moves it holds appear under `sequences`. You generate that file locally with `build_library.py` (it is not shipped). Until then `library` returns empty lists and `"generated": false`, `sequences` lists only the shows in `sequences.json`, and everything else still works.

Replies: `{"ok": true, "result": ...}` or `{"ok": false, "error": "Type: message"}`.

## Examples

```sh
BASE=http://127.0.0.1:8765
curl "$BASE/api/status"
curl "$BASE/api/stop"
curl "$BASE/api/sequence?name=hello"
curl -G "$BASE/api/moves" --data-urlencode 'steps=[{"kind":"forward","mm":100}]'
curl "$BASE/api/eyes?a=2&b=2"
curl "$BASE/api/sound?path=system/common/audio/wow.mp3"
```

## Errors

| Status | Meaning |
|---|---|
| 400 | Invalid command or fields (ValueError) |
| 403 | Wrong Host, disallowed Origin, or cross-site request |
| 404 | Unknown command |
| 409 | Controller busy (command not queued; `stop` still works) |
| 503 | Bluetooth not ready |
| 500 | Anything else |

## Calling from another tailnet site

Start the server with `--allow-origin http://localhost:8000` for one exact origin (repeatable), or with `--allow-origin-suffix tail1234.ts.net` (repeatable). Pages at `https://<label>.tail1234.ts.net` (one label, https, no port) may then call the API; the response carries `Access-Control-Allow-Origin` echoing that origin. Use a plain `fetch(url)` with no custom headers and no options; anything else triggers a preflight (OPTIONS), which the server cannot answer, so the browser will block it.

```js
const r = await fetch('https://tiny.tail1234.ts.net/api/sequence?name=hello');
const data = await r.json();   // {ok: true, result: ...}
```

## Security notes

- GET-only because the server's HTTP parser accepts nothing else. This means any web page could try to trigger a request (for example an `<img>` tag), so `/api` rejects requests with a `Sec-Fetch-Site` other than `same-origin` or `none` (unless the Origin is CORS-allowed as above), and rejects disallowed `Origin` values. Scripts such as curl send neither header and are allowed. Responses are `Cache-Control: no-store`.
- Host allow-list: only `127.0.0.1:<port>` and `localhost:<port>` by default. For a reverse proxy such as Tailscale Serve, add its name with `--allow-host tiny.example.ts.net` (repeatable); it is accepted with and without `:443`, and `https://<host>` becomes an allowed Origin.
- There is no login. Anyone who can reach the URL can drive the robot, so only expose it on a network you trust. Nothing stops motion automatically when an API call ends; call `/api/stop`.
