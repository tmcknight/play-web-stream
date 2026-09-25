# play-web-stream

Takes the URL of a page playing video. Gives back a URL that Safari's native player will
play, so AirPlay, Picture-in-Picture and the scrubber all work.

## Why the AirPlay button is missing

Web players (hls.js, Clappr, Plyr, Video.js, JW) push bytes through Media Source
Extensions, and MSE has no AirPlay route. Those features live in AVFoundation, which
means getting the stream into Safari's plain `<video>` element.

AVFoundation is stricter about one thing: it refuses segments whose `Content-Type` is
wrong, and CDNs often serve MPEG-TS as `text/plain`. That is the crossed-out play icon.

So the job is:

1. **Find** the real playlist (`.m3u8`) URL inside the page.
2. **Probe** it. Are the segment MIME types usable? Does the origin require a `Referer`?
3. **Proxy** it, only if step 2 says so, re-serving with corrected types.

## Ways to run it

- **[Command line](#run-it-from-the-command-line).** `hls_proxy.py`, standard library only.
- **[Claude Code skill](#run-it-as-a-claude-code-skill).** `SKILL.md`, so Claude runs the
  sequence and reads the logs.
- **[Web app](#run-it-as-a-web-app).** Paste a page URL on your phone, send the result to
  a television.

## Run it from the command line

`hls_proxy.py` is Python 3 standard library only. No install, no venv.

```sh
# 1. Find the playlist URL in a page
python3 hls_proxy.py --discover "<page-url>"

# 2. Check whether the segments are served with usable MIME types
python3 hls_proxy.py --source "<playlist-url>" [--referer "<page-origin>/"] --probe

# 3. Re-serve with corrected types, then open the printed URL in Safari
python3 hls_proxy.py --source "<playlist-url>" [--referer "<page-origin>/"]
```

If `--probe` says the types are fine, skip step 3 and open the playlist URL directly.

The proxy binds `0.0.0.0` on the first free port from 8787, serves under a random path,
signs its URLs with a per-process key (a forged URL gets a 404), and exits after 15
minutes idle. It binds all interfaces because the Apple TV fetches the stream itself, so
loopback would rule AirPlay out.

## Run it as a Claude Code skill

`SKILL.md` is the same sequence written for Claude Code: discover, work out the headers,
probe, start a proxy only if needed, then read the `[player]` log to confirm something is
watching. It also covers expiring presigned URLs, sliding windows and origins that screen
the client, so a stream that dies on segment three gets diagnosed.

```sh
ln -s "$PWD" ~/.claude/skills/play-web-stream
```

`SKILL.md` refers to `~/.claude/skills/play-web-stream/hls_proxy.py`, which resolves
through that symlink. Then ask for a page to play, or invoke the skill by name.

## Run it as a web app

Paste a page URL, get a URL Safari will play, hand it to a television. Same steps as the
CLI, without typing them.

### Run it with Docker

```sh
docker compose up -d --build            # http://<this-box>:8786/
```

The image bundles chromium for the browser fallback, so the first build is slow.

`docker-compose.yml` uses `network_mode: host`. The Apple TV fetches the media URL
itself, so that URL must carry a LAN address, and host networking lets the container work
its own address out.

Bridge networking works with the address supplied by hand:

```sh
PWS_ADVERTISE_IP=192.168.1.10 \
  docker compose -f docker-compose.yml -f docker-compose.bridge.yml up -d --build
```

Then you handle this yourself:

- Publish the proxy port range up front, since each stream takes the next free port at
  runtime. Move it with `PWS_PROXY_PORT` and `PWS_PROXY_PORT_LAST`. The container refuses
  to start if a port in the range is busy.
- On Docker Desktop every client appears as the VM gateway, so the `[player]` log can no
  longer show segment requests coming from the Apple TV's address. Linux keeps the real
  source address.

The app warns at startup if it is about to advertise a docker bridge address.

In Portainer: *Stacks, Add stack, Repository*, this repository's URL, compose path
`docker-compose.portainer.yml` (the bridge overlay already applied, since a git stack
takes one compose path), and `PWS_ADVERTISE_IP` set to the host's LAN address. The stack
refuses to deploy without it, because a wrong address just looks like a broken stream.

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

Each stream gets its own proxy on its own port, so the published range is the number of
streams that can run at once. Once every slot is taken, the Resolve button says which
stream to stop. Restarting the container ends every stream it is serving.

### AirPlay it to a television

Every running stream gets a **▲ *name*** button per paired receiver, so the phone that
found the stream is what starts it. The hand-off uses `airplay_protocol.py` and holds the
session open, so the button becomes **Stop *name***. Sessions are per stream *and* per
receiver.

**Pairing** happens once per receiver, in the *AirPlay receivers* card at the bottom of
the page: press **Pair**, read the PIN off the television, type it in. The receiver only
shows the PIN after pairing has begun, so the handler is held open from one request to
the next and times out if nobody finishes. Credentials go to `/config/pyatv.conf`, or
`~/.config/play-web-stream/pyatv.conf` from a checkout.

**Finding receivers.** Addresses in `PWS_AIRPLAY_HOST` are probed on every page load and
cached, since multicast mDNS does not cross the docker bridge. **Sweep the LAN** probes
all 254 addresses on the advertised `/24`; it is a button rather than automatic because
it takes a moment. Anything paired is written to `receivers.json` beside `pyatv.conf`, so
the sweep is a one-off: pyatv's own credentials file keys devices by identifier and never
records their addresses. Remembered addresses are probed at startup too, and listed:

```
play-web-stream: 192.168.1.50 Family Room -- paired
play-web-stream: 192.168.1.51 Bedroom -- remembered, no answer
```

A remembered receiver keeps its row whatever a scan makes of it. The states:

- **Paired.** Ready to be handed a stream.
- **No answer.** Off, asleep or moved. Listed, but no button.
- **Not paired any more.** It answered and has lost its credentials, which is what a
  factory reset does. Comes with **Pair again**.

Scans never remove anything. **Forget** drops the address and stops probing for it;
credentials stay in `pyatv.conf`, keyed by device, so a later sweep finds it paired.

```sh
python3 airplay.py scan 192.168.1.0/24    # no network: probe configured and remembered
python3 airplay.py pair 192.168.1.50
python3 airplay.py forget 192.168.1.50
```

Pairing can succeed and playback still fail, since stock `play_url` fails on modern
receivers. Failures are reported beside the button of the television that produced them.

## Keep it on the LAN, off the internet

The app binds all interfaces so phones and Apple TVs can reach it, and refuses clients
outside private address space. Do not give it a public hostname through
nginx-proxy-manager or a Cloudflare tunnel: that makes it an open relay for third-party
video under your domain and address, and sustained video through a tunnel breaks
Cloudflare's terms anyway.

The AirPlay hand-off is stricter, because the server initiates it, and a viewer outside
the house pressing that button would be using the server's access to the LAN. Forwarded
requests arrive from `127.0.0.1`, which counts as private, so the client's address must
instead sit in the same `/24` the proxies advertise. Loopback is refused with everything
else off that subnet, so the button is absent on the box running the app.

Refused clients learn nothing: `GET /api/airplay` reports `available: false`,
`GET /api/receivers` returns an empty list, and `POST /api/airplay`,
`POST /api/airplay/pair` and `POST /api/cast` answer 403. Pairing is held to the same
bar, since it writes credentials for a television in this house.

Under `network_mode: host` this is invisible. Under bridge networking every client
arrives as the docker gateway, off the advertised LAN, so the controls switch themselves
off for everybody; the app says so at startup.

Safari's own AirPlay route is unaffected: the client initiates it and its receivers come
from link-local Bonjour. `SECURITY.md` has the rest.

## Which machine to run it on

Run it on your home connection. Many origins tie segment URLs to the address that
requested the page, so the proxy has to fetch from the same place your browser does. A
cloud host is somewhere else, and gets refused. The proxies it starts are detached, so
they survive a restart of the unit.

## Keep your address private

Stream origins and their CDNs log the address of everyone who fetches from them. To keep
yours out of those logs, send upstream fetches through a proxy:

```sh
PWS_EGRESS_PROXY=http://127.0.0.1:8888 python3 webapp.py
python3 hls_proxy.py --source "<playlist-url>" --egress-proxy http://127.0.0.1:8888
```

This is privacy, not access. It changes where the requests come from, not what you may
watch: a stream you cannot play in your own browser will not play through this either.

`docker-compose.vpn.yml` stands one up as a sidecar on a commercial VPN, with the app
outside its network. It uses gluetun, so any provider gluetun supports will do; the
worked example is NordVPN:

```sh
docker compose -f docker-compose.yml -f docker-compose.vpn.yml up -d --build
```

`VPN_PROVIDER` names the provider (`nordvpn` if unset), `VPN_PRIVATE_KEY` its wireguard
key, and `VPN_COUNTRIES` the exit -- the country you are in. Nord's key comes from their
API with an access token, not from the dashboard; that compose file's header has the
commands and says where the token is issued. The values go in a `.env` beside it, which
`.gitignore` already covers.

A sidecar, because Safari and the Apple TV fetch *from* this app over the LAN, so it has
to keep a LAN address; a VPN namespace takes that away. The split is by destination
instead. Upstream goes through the proxy: playlist, segments, header probe, and the
headless browser. Local stays direct: loopback, the private ranges, link-local, `*.local`
and anything in `no_proxy`.

Consequences worth knowing:

- **A stream that needs no fixing is still proxied.** Otherwise the app hands out the
  origin's own URL, Safari or the Apple TV fetches it from your address, and one
  correctly served stream undoes the hiding. That is `PWS_FORCE_PROXY`; `0` accepts the
  leak.
- **It is `PWS_EGRESS_PROXY`, not `https_proxy`.** The conventional variables are honoured
  if already set, but setting them yourself points every library in the process at the
  tunnel, health check included.

`GET /api/egress` reports where upstream fetches leave from and the address the exit
answers with. It asks through the proxy and never directly, so a tunnel that is down
reads as an error rather than leaking the address being hidden. Authenticated proxies
work throughout: Chromium takes no credentials in `--proxy-server`, so the browser
fallback gets them through Playwright.

## When the origin rejects Python's TLS handshake

**This is for streams you can already play in your own browser.** It does nothing for
DRM, and nothing for a stream the origin would refuse your browser too.

Some origins never read the headers. Python's OpenSSL handshake (cipher list, extension
order, ALPN set) matches no shipping browser, and a JA3/JA4 hash of it is refused before
a header is looked at, so cycling header profiles changes nothing. `--probe` and
`--discover` report that the origin is screening the client itself, and exit 2.

With `curl_cffi` installed (it is in `requirements.txt`) the proxy retries presenting
Safari's handshake, matching the `User-Agent` it already sends. If that works, every
fetch for the stream uses it. Nothing is tried until Python's own handshake is refused.
`--browser-handshake` starts a proxy that way from the first request, which is what the
web app does once the resolver has learnt it is needed.

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

`hls_proxy.py` needs no dependencies. The web app's are in `requirements.txt`, the checks
add pytest and ruff in `requirements-dev.txt`.

## Running the tests

```sh
pip install -r requirements-dev.txt
python3 hls_proxy.py --self-test     # or: python3 -m pytest -q
ruff check .
```

`tests/origin.py` is a fake origin with each awkward behaviour as a switch: a Referer
gate, a gate on the client itself, `text/plain` segments, presigned URLs that expire,
byte ranges, and a sliding window. Most tests run a real proxy as a subprocess and assert
on the wire, since a stream that resolves cleanly and then 403s on every segment looks
fine from inside the process.

CI runs the suite on Python 3.9 through 3.14, and ruff over everything. `CONTRIBUTING.md`
has the rest: run the checks, keep `hls_proxy.py` on the standard library.

## Scope

It corrects a `Content-Type` and re-serves a stream you can already play. It does not
get around access controls -- logins, paywalls, geo-restrictions, DRM -- and changes
that would are out of scope, as is anything that needs the app reachable from the public
internet (see `SECURITY.md`).

DRM streams use Widevine or FairPlay, so the segments are encrypted and re-serving them
achieves nothing. DASH has no native AirPlay path. Screen mirroring is the answer for
both.

Do not route this through QuickTime. On macOS 27.0, QuickTime 10.5 crashes with
`EXC_BREAKPOINT` when AirPlay route discovery fires during playback-control layout. It is
an AVKit bug. Use Safari.

## Licence

MIT. See `LICENSE`.
