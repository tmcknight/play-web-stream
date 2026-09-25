# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
pip install -r requirements-dev.txt        # pulls requirements.txt too (curl_cffi, playwright, segno, pyatv)
playwright install chromium                # only for the runtime-built-player fallback

python3 -m pytest -q                       # full suite
python3 -m pytest tests/test_proxy.py -q   # one file
python3 -m pytest tests/test_proxy.py::test_name -q
python3 -m pytest -q -k handshake          # by name

python3 hls_proxy.py --self-test           # offline checks of rewrite/sniff logic, no deps
ruff check .                               # line-length 100, config in pyproject.toml
```

Running it:

```sh
python3 hls_proxy.py --discover "<page-url>"                  # find the playlist
python3 hls_proxy.py --source "<playlist-url>" --probe        # check segment MIME types
python3 hls_proxy.py --source "<playlist-url>" [--referer …]  # serve corrected stream
python3 resolve.py "<page-url>"                               # discover + probe + verdict
python3 webapp.py                                             # LAN UI on :8786
python3 airplay.py scan|pair|forget <addr>                    # receiver management
```

CI (`.github/workflows/checks.yml`) runs pytest on Python 3.9–3.14 and ruff. A change that
only passes on the local interpreter will come back.

## Architecture

The problem: browser players feed MSE, which has no AirPlay route. AVFoundation (Safari's
native `<video>`) has one but refuses segments whose `Content-Type` is wrong, and CDNs serve
MPEG-TS as `text/plain`. So: find the real playlist, probe its MIME types, re-serve through a
local proxy that corrects them.

One pipeline, three front ends:

- `hls_proxy.py` (2100 lines) — the whole engine. Discovery, gate classification, MIME
  probe, playlist rewriting, the serving `Handler`, window accumulation, segment cache,
  state files, idle exit.
- `resolve.py` — that engine's find→probe→verdict sequence as one deterministic call
  (`resolve()`), used by the web app and runnable alone.
- `SKILL.md` — the same sequence written for Claude Code to drive by hand, with the gotchas.
- `webapp.py` + `ui.html` — HTTP front end over `resolve.py`, plus proxy lifecycle and the
  AirPlay hand-off. `ui.html` is one file served whole.
- `airplay.py` / `airplay_protocol.py` — receiver discovery, pairing, and a hand-off that
  reaches past pyatv's public API because stock `play_url` fails on modern receivers.
- `browser_find.py` — Playwright fallback for players assembled at runtime.

### Load-bearing mechanics

**Process model.** Each stream is a *separate detached `hls_proxy.py` child* on its own port,
started by `webapp.py:start_proxy()`. They survive an app restart and end themselves on idle
timeout. Coordination is entirely through state files in `hls_proxy.state_dir()` — a
0700 dir under tempdir keyed by uid — holding source, port, pid, URL. `live_states()` checks
every state file against a live process before counting it. The published docker port range
is therefore also the concurrent-stream limit; `capacity()` enforces it.

**Signing.** Every rewritten `/pl/` and `/seg/` URI is HMAC-signed with `_SIGN_KEY`, generated
per process. Without it the path token would make `/seg/` an open fetcher for the LAN. The
path token itself is 16 random bytes and is the only credential on a proxy that binds all
interfaces — it binds all interfaces on purpose, because AirPlay makes the *Apple TV* fetch
the media URL.

**Window accumulation** (`accumulate()`, `_windows`). Origins often publish 3 segments (~15s);
a buffering player falls off the edge and stalls forever. The proxy remembers past segments
and re-advertises a wider window, numbered with a monotonic offset so a restart upstream does
not make the media sequence go backwards (AVFoundation reads that as a different stream). One
window *per playlist URL* — a shared one merged audio renditions into the video playlist.
`cache_*` keeps the bytes so the widened window still serves after the origin drops them.

**Gates.** `classify_gate()` / `fetch_through_gate()` distinguish a header gate (needs
`Referer`, fixed by cycling profiles) from an origin screening the TLS handshake itself
(JA3/JA4 — no header profile helps). The second case is retried once with `curl_cffi`
presenting Safari's handshake, only behind an `ImportError` guard and only after Python's own
handshake was refused. `--browser-handshake` starts that way from the first request.

**Egress split.** `PWS_EGRESS_PROXY` routes *upstream* fetches (playlist, segments, probes,
the headless browser) through a proxy while anything on this network — loopback, private
ranges, link-local, `*.local`, `no_proxy` — is reached directly. The app must keep a LAN
address and answer there, which is why it is not placed inside a VPN namespace. Setting
`https_proxy` instead points the whole process, health check included, at the tunnel.
`PWS_FORCE_PROXY` (on by default with an egress proxy) makes even an unbroken stream go
through the local proxy, so the origin never sees the real address.

**Access control** is three layers, deliberately: `_private_client()` (private address space),
`known_host()` + `_gate()` (the Host header must be ours), and `_local_client()` — stricter,
for the AirPlay routes only, requiring the client in the same `/24` as the advertised address.
AirPlay is *server*-initiated, so a remote viewer pressing it would be borrowing the server's
network position; loopback is refused there too. The refusal is silent about what it protects
(`/api/airplay` reports `available: false`, `/api/receivers` returns empty).

## Constraints

- **`hls_proxy.py` is standard library only.** It is dropped onto whatever Python a machine
  already has, and `SKILL.md` depends on that. `curl_cffi` is the one exception, guarded by
  `ImportError`, and its absence must leave old behaviour intact. Anything needing a hard
  dependency goes in `webapp.py` or a module beside it. It is deliberately not a package.
- **`pyatv==0.18.0` is pinned** because `airplay_protocol.py` reaches past its public API. A
  bump has to be read, not taken.
- **Out of scope:** DRM (YouTube/Netflix/Disney+), DASH, anything circumventing an access
  control rather than correcting a `Content-Type`, and any feature needing the app to be
  safely reachable from the public internet — the LAN boundary is load-bearing (`SECURITY.md`).
- Do not route through QuickTime: QuickTime 10.5 on macOS 27.0 crashes with `EXC_BREAKPOINT`
  on AirPlay route discovery. Use Safari.

## Testing style

`tests/origin.py` is a fake origin with every hostile behaviour as a switch: Referer gate,
client gate, `text/plain` segments, expiring presigned URLs, byte ranges, sliding window. Most
tests run a real proxy **as a subprocess** (`start_proxy` fixture in `conftest.py`) and assert
on the wire — a stream that resolves cleanly and then 403s on every segment looks perfectly
healthy from inside the process. Prefer adding a switch to `origin.py` over mocking the
network. Tests skip without `curl_cffi` or `pyatv`.

## Prose style

Comments and docstrings explain *why*, in full sentences, at length where the reason is
subtle. The existing ones are the specification — read a few before writing more. A comment
restating the line below it is worse than none. Log lines and error messages are written for
someone reading them at eleven at night. British spelling throughout.
