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

Browser players feed MSE, which has no AirPlay route. AVFoundation (Safari's native
`<video>`) has one but refuses segments with the wrong `Content-Type`, and CDNs serve
MPEG-TS as `text/plain`. So: find the real playlist, probe its MIME types, and re-serve it
through a local proxy that corrects them.

One pipeline, three front ends:

- `hls_proxy.py` (2100 lines): the engine. Discovery, gate classification, MIME probe,
  playlist rewriting, the serving `Handler`, window accumulation, segment cache, state
  files, idle exit.
- `resolve.py`: find, probe, verdict as one deterministic call (`resolve()`). Used by the
  web app; also runs alone.
- `SKILL.md`: the same sequence for Claude Code to run by hand, with the gotchas.
- `webapp.py` + `ui.html`: HTTP front end over `resolve.py`, proxy lifecycle, AirPlay
  hand-off. `ui.html` is one file, served whole.
- `airplay.py` / `airplay_protocol.py`: receiver discovery, pairing, and a hand-off using
  pyatv internals, because stock `play_url` fails on modern receivers.
- `browser_find.py`: Playwright fallback for players assembled at runtime.

### Key mechanics

**Process model.** Each stream is a detached `hls_proxy.py` child on its own port,
started by `webapp.py:start_proxy()`. Children survive an app restart and exit when idle.
They coordinate only through state files (source, port, pid, URL) in
`hls_proxy.state_dir()`, a 0700 dir under tempdir keyed by uid. `live_states()` checks
each against a live process. The published Docker port range is the concurrent-stream
limit, enforced by `capacity()`.

**Signing.** Rewritten `/pl/` and `/seg/` URIs are HMAC-signed with `_SIGN_KEY`,
generated per process. Otherwise `/seg/` would be an open fetcher for the LAN. The path
token (16 random bytes) is the only credential on a proxy bound to all interfaces, which
it must be because AirPlay makes the Apple TV fetch the media URL.

**Window accumulation** (`accumulate()`, `_windows`). Origins often publish 3 segments
(~15s), and a buffering player falls off the edge and stalls. The proxy remembers past
segments and advertises a wider window, numbered with a monotonic offset so an upstream
restart can't make the media sequence go backwards (AVFoundation treats that as a new
stream). One window per playlist URL: a shared one merged audio renditions into the
video playlist. `cache_*` keeps the bytes for segments the origin has dropped.

**Gates.** `classify_gate()` / `fetch_through_gate()` separate a header gate (needs
`Referer`; cycling profiles fixes it) from TLS handshake screening (JA3/JA4; no header
profile helps). The latter is retried once with `curl_cffi` presenting Safari's
handshake, behind an `ImportError` guard, only after Python's handshake was refused.
`--browser-handshake` uses it from the first request.

**Egress split.** `PWS_EGRESS_PROXY` routes upstream fetches (playlist, segments, probes,
headless browser) through a proxy. Loopback, private ranges, link-local, `*.local` and
`no_proxy` go direct. The app must keep and answer on a LAN address, so it can't sit in a
VPN namespace. `https_proxy` would send the whole process, health check included, through
the tunnel. `PWS_FORCE_PROXY` (default on with an egress proxy) proxies even streams that
need no fixing, so the origin never sees the real address.

**Access control**, three layers:

- `_private_client()`: client in private address space.
- `known_host()` + `_gate()`: Host header must be ours.
- `_local_client()`: AirPlay routes only. Client must be in the advertised `/24`;
  loopback refused. AirPlay is server-initiated, so a remote viewer would borrow the
  server's network position.

Refusals don't reveal what they protect: `/api/airplay` reports `available: false`,
`/api/receivers` returns empty.

## Constraints

- **`hls_proxy.py` is standard library only.** It runs on whatever Python a machine has,
  and `SKILL.md` relies on that. The one exception, `curl_cffi`, sits behind an
  `ImportError` guard, and without it behaviour must be unchanged. Hard dependencies go in
  `webapp.py` or a module beside it. It is not a package.
- **`pyatv==0.18.0` is pinned** because `airplay_protocol.py` uses its internals. Read
  the changes before bumping.
- **Out of scope:** DRM (YouTube/Netflix/Disney+), DASH, getting around access controls
  (this only corrects `Content-Type`), and anything needing the app safely reachable from
  the public internet. The LAN boundary is the security model (`SECURITY.md`).
- Don't route through QuickTime: 10.5 on macOS 27.0 crashes with `EXC_BREAKPOINT` on
  AirPlay route discovery. Use Safari.

## Testing style

`tests/origin.py` is a fake origin with each hostile behaviour as a switch: Referer gate,
client gate, `text/plain` segments, expiring presigned URLs, byte ranges, sliding window.
Most tests run a real proxy as a subprocess (`start_proxy` fixture in `conftest.py`) and
assert on the wire, since a stream that 403s on every segment can look healthy from
inside the process. Add a switch to `origin.py` instead of mocking the network. Tests
skip without `curl_cffi` or `pyatv`.

## Prose style

- Comments and docstrings explain *why*, briefly, in plain sentences. No comment that
  restates the code.
- Avoid stock phrasing: "deliberately", "on purpose", "load-bearing", "that is
  deliberate", "the one thing", "worth knowing", "X, not Y" for effect, dashes for
  dramatic asides, closing lines that restate the paragraph, and filler like "simply",
  "just", "actually", "genuinely".
- Log lines and error messages should tell someone debugging what happened and what to
  do next.
- British spelling.
