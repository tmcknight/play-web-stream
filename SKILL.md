---
name: play-web-stream
description: Get the real video stream out of a web page and playing in Safari's native player, given the page URL — from there the user can AirPlay it, fullscreen it, or just watch it. Use when a site's player has no AirPlay button, or when the user wants the underlying stream rather than the page — typically live streams built on hls.js, Clappr, Plyr, Video.js or JW Player. Also use when the user reports a crossed-out play icon after opening a stream URL in Safari or QuickTime, or asks why a page's AirPlay icon is missing. Not for DRM services, and not for a local file.
---

# Play a web stream natively

Take a page URL, end with the video playing in Safari's native player — where
AirPlay, fullscreen and the rest of the system controls actually work.

## Why the page's own player won't do

Browser players (hls.js, Clappr, Plyr, Video.js, JW) feed bytes to **Media Source
Extensions**. MSE has no AirPlay route — the button cannot appear, and no amount of
clicking in the page will produce one. Picture-in-Picture and the native scrubber are
usually missing or broken for the same reason.

All of that lives in **AVFoundation**, which means handing the stream to Safari's native
`<video>`. AVFoundation is stricter than hls.js in one way that matters: it refuses
segments whose `Content-Type` is wrong, and stream CDNs routinely serve MPEG-TS as
`text/plain`. That mismatch is what produces a crossed-out play icon.

So the job is: find the real playlist URL, check its MIME types, and if they're wrong,
re-serve it through the bundled proxy.

## Steps

### 1. Find the playlist URL

**Try this first — no browser needed:**

```
python3 ~/.claude/skills/play-web-stream/hls_proxy.py --discover "<page-url>"
```

These pages are server-rendered, so the player config is already in the HTML. It follows
iframes two levels deep, decodes base64 blobs, and confirms each candidate by fetching it
and checking for `#EXTM3U`. On success it prints the playlist URL **and whether a Referer
is required**, which also settles step 2:

```
page     : https://embed.example/new-stream-embed/56685
playlist : https://origin.example/playlist/56685/load-playlist
referer  : https://embed.example/new-stream-embed/56685
```

Takes a few seconds. Exits 1 if it finds nothing, which means the player is genuinely
built at runtime — only then fall back to Playwright.

Prefer this over the browser: it is faster, and launching Playwright pops a visible
window over whatever the user is watching.

#### Playwright fallback

**Match on content, not on a `.m3u8` extension.** Playlist URLs frequently have no
extension at all (`/load-playlist`), or carry query strings, or arrive base64'd in the
player config. An extension regex will miss them.

```js
() => {
  const html = document.documentElement.outerHTML;
  const hits = new Set();
  // Any URL that looks like a playlist OR any plausible stream endpoint.
  for (const m of html.matchAll(/https?:\/\/[^\s"'`<>\\]+/g)) {
    if (/\.m3u8|\.mpd|playlist|stream|manifest|\/hls\//i.test(m[0])) hits.add(m[0]);
  }
  // Base64 blobs long enough to be a URL, decoded.
  for (const m of html.matchAll(/["']([A-Za-z0-9+/=]{24,})["']/g)) {
    try {
      const d = atob(m[1]);
      if (/^https?:\/\//.test(d)) hits.add(d);
    } catch (e) {}
  }
  return {
    candidates: [...hits],
    globals: ['Hls','videojs','jwplayer','Clappr','dashjs','shaka','Plyr']
      .filter(k => typeof window[k] !== 'undefined'),
    iframes: [...document.querySelectorAll('iframe')].map(f => f.src),
  };
}
```

Then confirm which candidate is really a playlist — fetch it and look for `#EXTM3U`, or
check `browser_network_requests` for a response whose content-type contains `mpegurl`.

| Symptom | Where the URL actually is |
|---|---|
| Nothing matched | check network requests for a `mpegurl` content-type |
| `iframes` non-empty | recurse into the iframe, the player is one level down |
| `.mpd` only | DASH — no native AirPlay path, go to **Fallback** |

If the user passed an `.m3u8` URL directly, skip this step.

### 2. Find the required headers

Skip this if `--discover` already printed a `referer` line — it tested both ways.

Otherwise: most gated origins want a `Referer` of the embedding page.

```
curl -sS -o /dev/null -w "%{http_code}\n" "<playlist-url>"
curl -sS -o /dev/null -w "%{http_code}\n" -H "Referer: <page-origin>/" "<playlist-url>"
```

403 then 200 means `--referer` is required below. Both 200 means none is.

403 both ways, and still 403 with a browser `User-Agent` added, usually means the origin
is screening the client itself rather than the request — most often a TLS fingerprint
check that Python's handshake fails, whatever headers it carries. `--probe` and
`--discover` recognise this (the same status under every header profile) and exit 2
with a report. If `curl_cffi` is installed (`pip install curl_cffi`) they first retry
once presenting Safari's handshake, and the proxy does the same at startup;
`--browser-handshake` makes it present that from its first request. Without it, the
report says so, and screen mirroring (**Fallback**) is the answer.

#### Leaving from somewhere else

`--egress-proxy http://host:port` (or `PWS_EGRESS_PROXY` in the environment) sends every
upstream fetch through that proxy, so the origin sees its address rather than this
connection's. It belongs on `--discover` and `--probe` as much as on the proxy itself:
the three see the origin as the same client, and a discovery run that leaked the address
has already leaked it. The LAN is still reached directly, so the served stream is
unaffected. Nothing falls back to a direct connection — if the proxy is down, the fetch
fails and says so.

### 3. Probe the MIME types

```
python3 ~/.claude/skills/play-web-stream/hls_proxy.py \
  --source "<playlist-url>" [--referer "<page-origin>/"] --probe
```

It reports what each segment is served as versus what it really is, and prints a verdict.

- **MIME correct** → skip the proxy. Open the playlist URL in Safari and go to step 5.
- **MIME mismatch** → run the proxy, step 4.

### 4. Run the proxy

Tell the user first: it binds `0.0.0.0`, so it is reachable from their LAN while it runs.
It always binds all interfaces, even when the user only means to watch on the Mac — the
Apple TV fetches the URL itself (see *AirPlay hands over the URL* below), so a loopback
bind would make AirPlay impossible without restarting the proxy and reloading the page.
Binding wide keeps AirPlay one click away throughout. It serves under a random path and
exits on its own after 15 minutes idle, so the exposure is bounded.

```
python3 ~/.claude/skills/play-web-stream/hls_proxy.py \
  --source "<playlist-url>" [--referer "<page-origin>/"] > /tmp/hls_proxy.log 2>&1 &
```

It takes the first free port from 8787 up and prints the full URL it's serving, including
the random path segment. **Read that URL out of the log** — do not assume the port or
construct the URL by hand.

```
open -a Safari "<url from the log>"
```

Useful flags: `--window N` sets how many segments to advertise (default 12; see *Short
live windows*), `--idle-timeout 0` disables the self-shutdown, `--no-reuse` forces a
second instance for the same source, `--no-flatten` proxies the master playlist instead
of pinning one variant.

### 5. Verify

The page reports its own playback state back into the log, because Safari blocks
`do JavaScript` from Apple Events by default and the element cannot be inspected any
other way. Look for the `[player]` lines:

```
grep '\[player\]' /tmp/hls_proxy.log | tail -20
```

**The proof is `t=` climbing across successive `tick` lines** — one tick every 5s, so `t`
should gain about 5 each time. Everything else can look healthy while playback is dead.

| Reading | Means |
|---|---|
| `t` climbing, `paused=0` | genuinely playing |
| `t` frozen, `paused=0`, `buf` healthy | fell off the live window — see gotchas |
| `paused=1` | autoplay was refused, waiting on a click |
| `error code=N` | media error; check the segment MIME |
| occasional `stalled` with `t` still climbing | benign, ignore |
| `air=1` | playing on an AirPlay receiver — read nothing else on the line |

**`air=1` invalidates the rest of the line.** Once the receiver is playing, the local
element stops fetching, its `buffered` ranges go stale and then empty, and `t` arrives
in coarse jumps from the receiver. `buf` goes negative. None of it means anything about
what is on the television — the receiver's own fetches, in the lines above, are the only
honest signal. The freeze watchdog stands down for the same reason.

Segment fetch rate is a weaker second signal: roughly 0.2/s for 5s segments. A handful of
fetches proves only that AVFoundation buffered, which it does even while paused.

Do not `curl` the proxy to check it. Claude Code's auto-mode classifier can block
requests to local services (`Expose Local Services`), loopback included; the log is
the reliable path either way.

The video is now in Safari's native player, so the full system controls apply. If the
user wants it on a TV, tell them to hover the video and click the AirPlay icon in
Safari's controls.

### 6. Clean up

The proxy self-exits after its idle timeout. To stop it now, use the port from the log:

```
lsof -ti tcp:<port> | xargs kill
```

## Gotchas

**AirPlay hands over the URL.** For HLS video, Safari does not restream — it passes the
media URL to the Apple TV, which fetches it itself. A `127.0.0.1` URL is unreachable from
the TV. The proxy must advertise the LAN IP, which is why it binds all interfaces. The
URL it prints at startup uses the LAN address, but every playlist it serves is rewritten
from the request's own `Host` header — so reaching it at a different address, after a
network change or through a tunnel, works without restarting it.

A visible consequence: **once AirPlay engages, the requests in the log come from the Apple
TV's IP, not the Mac's, and Safari's own fetching stops.** That is correct behaviour, not
a stall. Sustained fetches from the TV's address are the real proof AirPlay is working.

**Autoplay.** Safari refuses to autoplay video with audio, so the served page tries an
unmuted `play()` first and falls back to a muted one, which is always permitted, showing
a click-to-unmute overlay. That means the stream starts on open but arrives silent until
clicked. For unmuted autoplay, the user can set Safari → Settings → Websites → Auto-Play →
*Allow All Auto-Play* for that host — worth suggesting if they'll reuse it. If both
`play()` calls are refused the overlay reads "Click to play" and playback needs one click.

**Never route this through QuickTime.** On macOS 27.0 (26A428) QuickTime 10.5 crashes
with `EXC_BREAKPOINT` in `AVFloatingPlaybackControlsViewController`
`_updateAuxiliaryControlsViewStateIfNeeded` when AirPlay route discovery fires while it
is laying out the playback controls. It is an AVKit bug, nothing to do with the stream,
and it takes the app down every time. Use Safari. Re-test after an OS update if curious.

**Short live windows — the classic stall.** These playlists often list only ~3 segments,
about 15 seconds. Safari buffers ~10s of that, so it sits right at the back edge; the
moment it slips past, it is waiting on a segment that has already rolled off and
AVFoundation freezes for good. The symptom is distinctive: `currentTime` stops advancing
while the element still reports `paused=0` and a healthy buffer, and `currentTime` may
jump *backwards* just before it dies.

The proxy fixes this by remembering segments the origin has dropped and advertising a
longer window (`--window`, default 12). The page also runs a watchdog that seeks to the
live edge if `currentTime` freezes for 8s anyway — except while AirPlay is driving, where
every signal it reads is about a pipeline the element no longer owns. If a stream stalls
despite both, raise `--window`; segment URLs are usually presigned with a ~300s TTL, which
caps how far back it is safe to go.

**Origins restart, and renumber downwards.** The same playlist URL will go quiet and come
back with a *lower* media sequence — 497 → 64, then 174 → 97, both seen on one URL inside
an hour. The proxy absorbs this: a poll sitting entirely behind the window, bringing
segments it does not hold, is spliced onto the end behind an `#EXT-X-DISCONTINUITY`, and
the sequence it publishes only ever climbs. A playlist that goes backwards reads to
AVFoundation as a different stream, so this matters more than it sounds. What nothing can
absorb is the gap before the restart — see below.

**Per-session variant paths.** Master playlists often hand out a variant URL containing a
session token, fresh on every request. The proxy resolves it once and pins it; re-resolving
mid-stream breaks media-sequence continuity. It re-resolves automatically on a 403/404,
including when an expired presigned segment comes back 403.

**Live streams have no seek.** Playback starts at the live edge. Expected, not a bug.

## When a stream stops after it was playing

"It worked for a bit and then stopped" has three causes that look identical from the
sofa. Tell them apart in this order — the first question is the cheap one and it is
usually the answer.

### 1. Is the origin still publishing?

Ask the upstream directly, not the proxy. The proxy faithfully mirrors a dead source,
so it will look just as stuck either way.

```
for i in 1 2 3; do
  curl -s [-H "Referer: <referer>"] "<playlist-url>" | grep MEDIA-SEQUENCE
  sleep 5
done
```

A live source gains roughly one sequence per segment duration. Frozen across all three
samples means **the source stopped**, and no proxy setting fixes that. Gaps of 15
seconds to eight minutes have been measured on one URL in an evening, and a receiver
gives up after about 25 seconds of no new media — so most of those gaps end the session
whatever the proxy does. Go to *Finding another source* below.

### 2. If it is advancing, read the log

Every line the proxy writes is stamped, which is the only way to settle what happened
first — the receiver going quiet, or the playlist doing so.

```
tail -40 /tmp/hls_proxy.log
```

The shape to look for, once AirPlay is driving:

```
17:41:12 192.168.2.194 "GET /seg/..."      <- receiver fills its buffer
17:41:14 192.168.2.194 "GET /live.m3u8"    <- then polls...
17:41:27 192.168.2.194 "GET /live.m3u8"    <- ...and never fetches another segment
```

Playlist polls continuing with no segment fetches following them means the receiver is
being handed a playlist with nothing in it that it does not already have. That is the
signature of a frozen origin, and sends you back to step 1.

### 3. If the app drove the AirPlay itself

The web app logs why a session ended, which is the difference between the media running
out and the session being lost:

```
docker logs play-web-stream 2>&1 | grep 'airplay:'
```

| Ending | Means |
|---|---|
| `the receiver reported it stopped` | the receiver decided — almost always it ran out of media |
| `feeding it failed: <exc>` | the session was lost underneath us; a transport or network fault |

### Finding another source

When the origin is simply dead, the event is usually still up elsewhere — these
aggregator pages carry a list of mirrors for the same fixture. Pull the candidates out
of the page:

```
curl -s -A "<the UA at the top of hls_proxy.py>" "<page-url>" \
  | grep -oiE 'href="[^"]*"' | sort -u
```

Ignore site navigation and keep the event-specific ones — sibling hosts (`live2.`,
`live3.`…) and third-party players naming the same fixture. Then resolve each through
the normal pipeline, so anything found is something this skill can actually play:

```
python3 ~/.claude/skills/play-web-stream/resolve.py "<candidate-page-url>"
```

Two things to expect, and to say out loud rather than quietly skip:

- **Many candidates resolve to the same upstream.** These aggregators share backends, so
  a different page is often the same dead stream. Compare the resolved playlist URLs and
  discard the duplicates before testing anything.
- **Re-resolving the original page does not help.** It returns the same playlist URL; the
  URL is not what rotates, the stream behind it is what restarts.

Then stability-test what survives, in parallel, before handing one to a television:

```
for i in $(seq 1 24); do
  curl -s [-H "Referer: <referer>"] "<playlist-url>" | grep -o 'MEDIA-SEQUENCE:[0-9]*'
  sleep 5
done
```

Two minutes with the sequence advancing every sample is a shortlist entry. It is not a
guarantee — it proves the source is not frozen *now*, and says nothing about twenty
minutes from now. Report it that way rather than as a verdict.

## Checking the proxy itself

```
python3 ~/.claude/skills/play-web-stream/hls_proxy.py --self-test
```

Runs the suite in `tests/`: playlist rewriting (master variants, alternate audio
renditions, AES-128 keys, fMP4 init sections, relative vs absolute URIs), container
sniffing, byte ranges, the signed URIs, the segment cache, and the resolver's verdicts
-- plus end-to-end checks against a fake origin that gates on a Referer or on the
client itself and serves MPEG-TS as `text/plain`. Run it after editing the script.

It needs pytest (`pip install -r requirements-dev.txt`); the proxy itself still runs on
the standard library alone.

## Fallback

If the stream is DASH, DRM-protected, or the proxy can't make AVFoundation happy, fall
back to screen mirroring — it works for any video and needs none of the above:

Control Center → Screen Mirroring → Apple TV. Then, to avoid mirroring the whole desktop,
set the display to **Use As Separate Display**, drag the browser window onto it, and
fullscreen the video there.

## Scope

Works on open HLS streams. Will not work on DRM streams, which use Widevine or FairPlay:
the segments are encrypted, and re-serving them achieves nothing. For those, screen
mirroring or the service's own cast button is the answer.
