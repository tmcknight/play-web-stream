# play-web-stream

Turns a page playing video into a URL Safari's native player can play, with AirPlay,
Picture-in-Picture and the scrubber.

## Why the AirPlay button is missing

Web players (hls.js, Clappr, Plyr, Video.js, JW) use Media Source Extensions, which has
no AirPlay route. AirPlay needs AVFoundation, meaning Safari's plain `<video>` element.
AVFoundation refuses segments with the wrong `Content-Type`, and CDNs often serve MPEG-TS
as `text/plain`: the crossed-out play icon.

The steps:

1. **Find** the real playlist (`.m3u8`) URL inside the page.
2. **Probe** it. Are the segment MIME types usable? Does the origin require a `Referer`?
3. **Proxy** it, only if step 2 says so, re-serving with corrected types.

## Ways to run it

- **[Command line](#run-it-from-the-command-line).** `hls_proxy.py`, standard library only.
- **[Claude Code skill](#run-it-as-a-claude-code-skill).** `SKILL.md`, so Claude runs the
  steps and reads the logs.
- **[Web app](#run-it-as-a-web-app).** Paste a page URL on your phone and send the result
  to a television.

## Run it from the command line

No install or venv needed.

```sh
# 1. Find the playlist URL in a page
python3 hls_proxy.py --discover "<page-url>"

# 2. Check whether the segments are served with usable MIME types
python3 hls_proxy.py --source "<playlist-url>" [--referer "<page-origin>/"] --probe

# 3. Re-serve with corrected types, then open the printed URL in Safari
python3 hls_proxy.py --source "<playlist-url>" [--referer "<page-origin>/"]
```

If `--probe` says the types are fine, skip step 3 and open the playlist URL directly.

The proxy binds `0.0.0.0` (the Apple TV fetches the stream itself) on the first free
port from 8787. It serves under a random path, signs its URLs with a per-process key (a
forged URL gets a 404), and exits after 15 minutes idle.

## Run it as a Claude Code skill

`SKILL.md` gives Claude Code the same steps: discover, work out headers, probe, proxy if
needed, and check the `[player]` log for a viewer. It also covers expiring presigned
URLs, sliding windows and origins that screen the client.

```sh
ln -s "$PWD" ~/.claude/skills/play-web-stream
```

`SKILL.md` calls `~/.claude/skills/play-web-stream/hls_proxy.py` through that symlink.
Then ask for a page to play, or invoke the skill by name.

## Run it as a web app

The CLI steps behind a page: paste a URL, get one Safari will play, send it to a
television.

### Run it with Docker

```sh
docker compose up -d --build            # http://<this-box>:8786/
```

The image bundles Chromium for the browser fallback, so the first build is slow.

`docker-compose.yml` uses `network_mode: host` so the container can find its own LAN
address, which the Apple TV needs in the media URL. For bridge networking, supply it:

```sh
PWS_ADVERTISE_IP=192.168.1.10 \
  docker compose -f docker-compose.yml -f docker-compose.bridge.yml up -d --build
```

Then:

- Publish the proxy port range, since streams take free ports at runtime. Set it with
  `PWS_PROXY_PORT` and `PWS_PROXY_PORT_LAST`. The container won't start if a port in it is
  busy.
- On Docker Desktop every client appears as the VM gateway, so the `[player]` log can't
  show requests from the Apple TV's address. Linux keeps real source addresses.

The app warns at startup if it would advertise a Docker bridge address.

In Portainer: *Stacks, Add stack, Repository*, this repository's URL, compose path
`docker-compose.portainer.yml` (bridge overlay included, as a git stack takes one path),
and `PWS_ADVERTISE_IP` set to the host's LAN address. The stack won't deploy without it,
because a wrong address looks like a broken stream.

### Run it without Docker

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium     # for the runtime-built-player fallback
.venv/bin/python webapp.py                # http://<this-box>:8786/
```

`deploy/install.sh` does the same and adds a systemd unit.

### Environment variables

| Variable | Default | Does |
|---|---|---|
| `PWS_PORT` | `8786` | Port for the UI. Streams take 8787 and up |
| `PWS_ADVERTISE_IP` | this host's LAN IP | Address the playback URLs carry. Required under bridge networking |
| `PWS_PROXY_PORT` | `8787` | First port a stream may take. Each takes the next free one above |
| `PWS_PROXY_PORT_LAST` | `PWS_PROXY_PORT` + 19, or + 3 under Portainer | Last port a stream may take |
| `PWS_WINDOW` | the proxy's own | Segments to advertise. Raise it if a stream stalls |
| `PWS_CACHE_MB` | `64` | Memory kept for recent segments, so a widened window still serves after the origin drops them |
| `PWS_BROWSER_BOOT_MS` | `6000` | How long the fallback waits for a player to build itself |
| `PWS_BROWSER_NAV_MS` | `25000` | Page load timeout for the fallback |
| `PWS_AIRPLAY_HOST` | unset | Receiver addresses, comma-separated. Skips mDNS, which bridge networking blocks. Optional: the app can sweep the LAN |
| `PWS_AIRPLAY_TIMING` | `auto` | `ptp`, `ntp`, or `auto` for PTP falling back to NTP. Newer Apple TVs (tvOS 26.5 on) need PTP; pin `ntp` only if an older one plays badly on it |
| `PWS_ALLOW_HOSTS` | unset | Hostnames the UI answers to (`box.local,pws.lan`). Only needed if you reach it by name |
| `PWS_ALLOW_ANY` | unset | `1` serves clients outside private address space (don't) |
| `PWS_ATV_STORAGE` | `/config/pyatv.conf`, or `~/.config/play-web-stream/pyatv.conf` outside a container | AirPlay credentials file |
| `PWS_RECEIVERS` | beside the credentials | Remembered receiver addresses |
| `PWS_EGRESS_PROXY` | unset | Send every upstream fetch through this proxy. See [Keep your address private](#keep-your-address-private) |
| `PWS_FORCE_PROXY` | on with `PWS_EGRESS_PROXY` | Proxy streams even when they need no fixing |

Each stream has its own proxy and port, so the port range sets how many streams can
run. When it is full, the page says so and **Find video** is disabled until a stream
under *On now* is stopped. Restarting the container ends all streams.

### AirPlay it to a television

A found stream leads with **Play on *name*** for the receiver used last, and each
stream under *On now* gets a button per paired receiver. The hand-off uses
`airplay_protocol.py` and keeps the session open; the button then reads **Stop on
*name***. Sessions are per stream *and* per receiver.

**Pairing** is once per receiver, on the *TVs* screen (the pill at the top right):
press **Pair** and type in the PIN the television shows. The pairing handler stays open between the two
requests and times out if unfinished. Credentials go to `/config/pyatv.conf`, or
`~/.config/play-web-stream/pyatv.conf` from a checkout.

**Finding receivers.** mDNS doesn't cross the Docker bridge, so addresses in
`PWS_AIRPLAY_HOST` are probed on each page load and cached. **Look for more TVs** probes
all 254 addresses on the advertised `/24` (up to a minute). Paired receivers are saved to
`receivers.json` beside `pyatv.conf`, since pyatv doesn't record addresses, so one sweep
is enough. Remembered addresses are probed and listed at startup:

```
play-web-stream: 192.168.1.50 Family Room -- paired
play-web-stream: 192.168.1.51 Bedroom -- remembered, no answer
```

A remembered receiver stays listed whatever a scan finds:

- **Paired.** Ready to be handed a stream.
- **No answer.** Off, asleep or moved. Listed, but no button.
- **Not paired any more.** It answered but lost its credentials (e.g. a factory reset).
  Shows **Pair again**.

Scans never remove anything. **Forget** drops the address and stops probing it;
credentials stay in `pyatv.conf`, so a later sweep finds it paired.

```sh
python3 airplay.py scan 192.168.1.0/24    # no network: probe configured and remembered
python3 airplay.py pair 192.168.1.50
python3 airplay.py forget 192.168.1.50
```

Pairing can succeed and playback still fail (stock `play_url` fails on modern
receivers). Errors show beside that television's button.

## Keep it on the LAN, off the internet

The app binds all interfaces and refuses clients outside private address space. Don't
expose it through nginx-proxy-manager or a Cloudflare tunnel: it becomes an open relay
for third-party video under your domain and address, and sustained video breaks
Cloudflare's terms.

AirPlay is stricter because the server starts it, so a remote viewer would be using the
server's LAN access. Forwarded requests arrive from `127.0.0.1`, which counts as private,
so AirPlay requires the client to be in the advertised `/24`. Loopback is refused too, so
the button doesn't appear on the machine running the app. Refused clients see nothing:
`GET /api/airplay` reports `available: false`, `GET /api/receivers` returns an empty
list, and `POST /api/airplay`, `POST /api/airplay/pair` and `POST /api/cast` answer 403.
Pairing follows the same rule, since it writes credentials.

With `network_mode: host` this is invisible. Under bridge networking every client
arrives as the Docker gateway, off the advertised LAN, so the controls are off for
everyone; the app warns at startup.

Safari's own AirPlay route is unaffected: the client starts it and finds receivers over
Bonjour. See `SECURITY.md`.

## Which machine to run it on

Your home connection. Many origins tie segment URLs to the address that requested the
page, so a cloud host gets refused. The proxies are detached and survive a restart of the
unit.

## Keep your address private

Origins and CDNs log who fetches from them. To keep your address out, send upstream
fetches through a proxy:

```sh
PWS_EGRESS_PROXY=http://127.0.0.1:8888 python3 webapp.py
python3 hls_proxy.py --source "<playlist-url>" --egress-proxy http://127.0.0.1:8888
```

This changes where requests come from, not what you can watch. A stream your browser
can't play won't play through this.

`docker-compose.vpn.yml` runs a commercial VPN as a gluetun sidecar, with the app outside
its network. Any gluetun provider works; the example is NordVPN:

```sh
docker compose -f docker-compose.yml -f docker-compose.vpn.yml up -d --build
```

Set `VPN_PROVIDER` (default `nordvpn`), `VPN_PRIVATE_KEY` (WireGuard key) and
`VPN_COUNTRIES` (exit country: the one you are in) in a `.env` beside it, which
`.gitignore` covers. Nord's key comes from their API with an access token, not the
dashboard; the compose file's header has the commands and where to get the token.

The app needs a LAN address for Safari and the Apple TV, which a VPN namespace would
remove, so traffic is split by destination. Through the proxy: playlist, segments, header
probe and headless browser. Direct: loopback, private ranges, link-local, `*.local` and
`no_proxy`.

Note:

- **Streams that need no fixing are still proxied.** Otherwise Safari or the Apple TV
  would fetch the origin's URL from your address. This is `PWS_FORCE_PROXY`; `0` accepts
  the leak.
- **Use `PWS_EGRESS_PROXY`, not `https_proxy`.** The standard variables are honoured if
  already set, but setting them sends everything in the process through the tunnel,
  health check included.

`GET /api/egress` reports the exit and the address it answers with. It asks only through
the proxy, so a down tunnel shows an error without leaking your address. Authenticated
proxies work; Chromium takes no credentials in `--proxy-server`, so the browser fallback
passes them through Playwright.

## When the origin rejects Python's TLS handshake

**Only for streams your own browser can already play.** It does nothing for DRM, or for
a stream the origin would refuse your browser too.

Python's OpenSSL handshake (cipher list, extension order, ALPN set) matches no shipping
browser. Some origins refuse its JA3/JA4 hash before reading any header, so header
profiles don't help. `--probe` and `--discover` report that the origin is screening the
client, and exit 2.

With `curl_cffi` installed (it is in `requirements.txt`), after Python's handshake is
refused the proxy retries with Safari's, matching its `User-Agent`. If that works, the
stream keeps using it. `--browser-handshake` uses it from the first request; the web app
passes it once the resolver finds it is needed.

## HTTP API

| Endpoint | Does |
|---|---|
| `GET /api/resolve?url=…&browser=1` | Server-sent events: progress lines, then the verdict |
| `GET /api/streams` | Proxies currently serving, with the newest `[player]` line |
| `GET /api/egress` | Where upstream fetches leave from, and the address the exit answers with. `?refresh=1` asks again |
| `GET /api/log?source=…` | Tail of one proxy's log |
| `POST /api/stop` | `{"source": "…"}` to stop one proxy |
| `GET /api/airplay` | Whether the hand-off is usable, which receivers answered, what is playing, and any pairing in progress. `?scan=1` sweeps the `/24` first (LAN only) |
| `POST /api/airplay` | `{"source": "…", "action": "start", "receiver": "…"}` to start or stop a stream there (LAN only) |
| `POST /api/airplay/pair` | `{"action": "begin", "host": "…"}` then `{"action": "finish", "pin": "…"}`, or `{"action": "cancel"}` (LAN only) |

## Repo layout

| Path | What it is |
|---|---|
| `hls_proxy.py` | The core: playlist discovery, MIME probe, rewriting proxy, self-test |
| `SKILL.md` | Claude Code skill wrapping the script |
| `resolve.py` | The skill's sequence as one deterministic pipeline: find, probe, verdict |
| `browser_find.py` | Playwright fallback for players assembled at runtime |
| `webapp.py`, `ui.html` | LAN web front end |
| `airplay.py`, `airplay_protocol.py` | Put a stream on an AirPlay receiver |
| `Dockerfile`, `docker-compose*.yml` | Image, and a stack per way of running it |
| `deploy/` | systemd unit and installer, for hosts without docker |
| `tests/` | The checks, including a fake origin that behaves like a hostile one |

`hls_proxy.py` has no dependencies. The web app's are in `requirements.txt`;
`requirements-dev.txt` adds pytest and ruff.

## Running the tests

```sh
pip install -r requirements-dev.txt
python3 hls_proxy.py --self-test     # or: python3 -m pytest -q
ruff check .
```

`tests/origin.py` is a fake origin with each awkward behaviour as a switch: Referer
gate, client gate, `text/plain` segments, expiring presigned URLs, byte ranges, sliding
window. Most tests run a real proxy as a subprocess and check the wire, because a stream
that 403s on every segment can look fine from inside the process.

CI runs the suite on Python 3.9 to 3.14, plus ruff. See `CONTRIBUTING.md`.

## Scope

It corrects a `Content-Type` and re-serves a stream you can already play. Getting around
access controls (logins, paywalls, geo-restrictions, DRM) is out of scope, as is anything
needing the app reachable from the public internet (see `SECURITY.md`).

DRM streams (Widevine, FairPlay) are encrypted, so re-serving them does nothing. DASH has
no native AirPlay path. Use screen mirroring for both.

Don't use QuickTime. On macOS 27.0, QuickTime 10.5 crashes with `EXC_BREAKPOINT` when
AirPlay route discovery fires during playback-control layout (an AVKit bug). Use Safari.

## Licence

MIT. See `LICENSE`.
