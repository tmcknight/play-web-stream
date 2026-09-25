---
name: play-web-stream
description: Get the real video stream out of a web page and playing in Safari's native player, given the page URL. From there the user can AirPlay it, fullscreen it, or just watch it. Use when a site's player has no AirPlay button, or when the user wants the underlying stream rather than the page — typically live streams built on hls.js, Clappr, Plyr, Video.js or JW Player. Also use when the user reports a crossed-out play icon after opening a stream URL in Safari or QuickTime, or asks why a page's AirPlay icon is missing. Not for DRM services, and not for a local file.
---

# Play a web stream natively

Start from a page URL and finish with the video playing in Safari's native player, where
AirPlay, fullscreen and the other system controls work.

## Why the page's own player won't do

Browser players (hls.js, Clappr, Plyr, Video.js, JW) feed bytes to **Media Source
Extensions**. MSE has no AirPlay route, so the button cannot appear from inside the page.
Picture-in-Picture and the native scrubber are usually missing or broken for the same
reason.

Those controls come from **AVFoundation**, which means handing the stream to Safari's
native `<video>`. AVFoundation is stricter than hls.js in one way: it refuses segments
whose `Content-Type` is wrong, and stream CDNs often serve MPEG-TS as `text/plain`. That
mismatch causes the crossed-out play icon.

The job: find the real playlist URL, check its MIME types, and if they are wrong,
re-serve it through the bundled proxy.

## Steps

### 1. Find the playlist URL

**Try this first. It needs no browser:**

```
python3 ~/.claude/skills/play-web-stream/hls_proxy.py --discover "<page-url>"
```

These pages are server-rendered, so the player config is in the HTML. Discovery follows
iframes two levels deep, decodes base64 blobs, and confirms each candidate by fetching it
and checking for `#EXTM3U`. On success it prints the playlist URL **and whether a Referer
is required**, which also covers step 2:

```
page     : https://embed.example/stream/123
playlist : https://origin.example/live/123/playlist
referer  : https://embed.example/stream/123
```

It takes a few seconds. Exit code 1 means it found nothing and the player is built at
runtime. Only then use Playwright.

Prefer discovery to the browser. It is faster, and Playwright opens a visible window over
whatever the user is watching.

#### Playwright fallback

**Match on content, not on a `.m3u8` extension.** Playlist URLs often have no extension
(`/live/123/playlist`), carry query strings, or are base64-encoded in the player config.
An extension regex misses them.

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

Then confirm which candidate is a playlist: fetch it and look for `#EXTM3U`, or check
`browser_network_requests` for a response whose content-type contains `mpegurl`.

| Symptom | Where the URL is |
|---|---|
| Nothing matched | check network requests for a `mpegurl` content-type |
| `iframes` non-empty | recurse into the iframe, the player is one level down |
| `.mpd` only | DASH: no native AirPlay path, go to **Fallback** |

If the user gave an `.m3u8` URL directly, skip this step.

### 2. Find the required headers

Skip this if `--discover` printed a `referer` line. It already tested with and without.

Otherwise, most gated origins want a `Referer` of the embedding page:

```
curl -sS -o /dev/null -w "%{http_code}\n" "<playlist-url>"
curl -sS -o /dev/null -w "%{http_code}\n" -H "Referer: <page-origin>/" "<playlist-url>"
```

403 then 200 means `--referer` is required below. 200 both times means it is not.

403 both times, and still 403 with a browser `User-Agent`, usually means the origin is
screening the client, not the request. Most often it is a TLS fingerprint check that
Python's handshake fails whatever the headers. `--probe` and `--discover` detect this
(the same status under every header profile) and exit 2 with a report. If `curl_cffi` is
installed (`pip install curl_cffi`), they first retry once with Safari's handshake, and
the proxy does the same at startup. `--browser-handshake` uses that handshake from the
first request. Without `curl_cffi`, the report says the retry was not possible, and
screen mirroring (**Fallback**) is the answer.

#### Leaving from somewhere else

`--egress-proxy http://host:port` (or `PWS_EGRESS_PROXY` in the environment) sends every
upstream fetch through that proxy, so the origin sees the proxy's address instead of this
connection's. Use it on `--discover` and `--probe` as well as on the proxy: the origin
sees all three as the same client, and a discovery run without it has already exposed
the address. The LAN is still reached directly, so the served stream is unaffected.
Nothing falls back to a direct connection. If the proxy is down, the fetch fails with an
error.

### 3. Probe the MIME types

```
python3 ~/.claude/skills/play-web-stream/hls_proxy.py \
  --source "<playlist-url>" [--referer "<page-origin>/"] --probe
```

It reports each segment's served type against its real type and prints a verdict.

- **MIME correct** → skip the proxy. Open the playlist URL in Safari and go to step 5.
- **MIME mismatch** → run the proxy, step 4.

### 4. Run the proxy

Tell the user first: it binds `0.0.0.0`, so it is reachable from their LAN while it runs.
It binds all interfaces even if the user only wants to watch on the Mac. The Apple TV
fetches the URL itself (see *AirPlay hands over the URL* below), so a loopback bind would
block AirPlay until the proxy is restarted and the page reloaded. It serves under a
random path and exits after 15 minutes idle, which limits the exposure.

```
python3 ~/.claude/skills/play-web-stream/hls_proxy.py \
  --source "<playlist-url>" [--referer "<page-origin>/"] > /tmp/hls_proxy.log 2>&1 &
```

It takes the first free port from 8787 upwards and prints the full URL, including the
random path segment. **Read that URL from the log.** Do not assume the port or build the
URL by hand.

```
open -a Safari "<url from the log>"
```

Useful flags: `--window N` sets how many segments to advertise (default 12; see *Short
live windows*), `--idle-timeout 0` disables the self-shutdown, `--no-reuse` forces a
second instance for the same source, `--no-flatten` proxies the master playlist instead
of pinning one variant.

### 5. Verify

The page writes its playback state to the log, because Safari blocks `do JavaScript`
from Apple Events by default and there is no other way to inspect the element. Look for
the `[player]` lines:

```
grep '\[player\]' /tmp/hls_proxy.log | tail -20
```

**Check that `t=` rises across successive `tick` lines.** There is one tick every 5s, so
`t` should gain about 5 each time. Everything else can look healthy while playback has
stopped.

| Reading | Means |
|---|---|
| `t` climbing, `paused=0` | playing |
| `t` frozen, `paused=0`, `buf` healthy | fell off the live window; see Gotchas |
| `paused=1` | autoplay was refused, waiting on a click |
| `error code=N` | media error; check the segment MIME |
| occasional `stalled` with `t` still climbing | harmless, ignore |
| `air=1` | playing on an AirPlay receiver; ignore the rest of the line |

**With `air=1`, ignore the rest of the line.** Once the receiver is playing, the local
element stops fetching, its `buffered` ranges go stale and then empty, `t` arrives in
coarse jumps from the receiver, and `buf` goes negative. None of it describes what is on
the TV. The receiver's own fetches, in the lines above, are the only reliable signal.
The freeze watchdog is disabled in this state for the same reason.

Segment fetch rate is a weaker signal: about 0.2/s for 5s segments. A few fetches only
prove AVFoundation buffered, which it does even while paused.

Do not `curl` the proxy to check it. Claude Code's auto-mode classifier can block
requests to local services (`Expose Local Services`), including loopback. Use the log.

The video is now in Safari's native player with the full system controls. If the user
wants it on a TV, tell them to hover over the video and click the AirPlay icon in
Safari's controls.

### 6. Clean up

The proxy exits after its idle timeout. To stop it now, use the port from the log:

```
lsof -ti tcp:<port> | xargs kill
```

## Gotchas

**AirPlay hands over the URL.** For HLS video, Safari does not restream. It passes the
media URL to the Apple TV, which fetches it directly. A `127.0.0.1` URL is unreachable
from the TV, so the proxy must advertise the LAN IP and binds all interfaces. The URL
printed at startup uses the LAN address, but every served playlist is rewritten from the
request's `Host` header. Reaching the proxy at a different address, after a network
change or through a tunnel, works without a restart.

So **once AirPlay starts, requests in the log come from the Apple TV's IP instead of the
Mac's, and Safari stops fetching.** This is expected and is not a stall. Steady fetches
from the TV's address confirm AirPlay is working.

**Autoplay.** Safari refuses to autoplay video with sound. The served page tries an
unmuted `play()` first, then falls back to a muted one (always allowed) with a
click-to-unmute overlay. The stream starts on open but is silent until clicked. For
unmuted autoplay, the user can set Safari → Settings → Websites → Auto-Play → *Allow All
Auto-Play* for that host; suggest it if they will reuse the host. If both `play()` calls
are refused, the overlay reads "Click to play" and playback needs one click.

**Never route this through QuickTime.** On macOS 27.0 (26A428) QuickTime 10.5 crashes
with `EXC_BREAKPOINT` in `AVFloatingPlaybackControlsViewController`
`_updateAuxiliaryControlsViewStateIfNeeded` when AirPlay route discovery runs while it
lays out the playback controls. It is an AVKit bug unrelated to the stream, and it
crashes every time. Use Safari. Re-test after an OS update if needed.

**Short live windows cause the classic stall.** These playlists often list only ~3 segments,
about 15 seconds. Safari buffers ~10s of that, so it sits near the back edge. Once it
slips past, it waits for a segment that has already rolled off, and AVFoundation freezes
permanently. The symptom: `currentTime` stops advancing while the element still reports
`paused=0` and a healthy buffer, and `currentTime` may jump *backwards* shortly before it freezes.

The proxy fixes this by keeping segments the origin has dropped and advertising a longer
window (`--window`, default 12). The page also runs a watchdog that seeks to the live
edge if `currentTime` freezes for 8s. The watchdog is off while AirPlay is playing,
because the element no longer owns the pipeline it would be reading. If a stream still
stalls, raise `--window`. Segment URLs are usually presigned with a ~300s TTL, which
limits how far back is safe.

**Origins restart, and renumber downwards.** The same playlist URL can go quiet and come
back with a *lower* media sequence: 497 → 64, then 174 → 97, both on one URL within an
hour. The proxy handles this. A poll that sits entirely behind the window and brings new
segments is appended after an `#EXT-X-DISCONTINUITY`, and the published sequence only
ever rises. AVFoundation treats a playlist that goes backwards as a different stream, so
this matters. The gap before the restart cannot be fixed; see below.

**Per-session variant paths.** Master playlists often return a variant URL with a
session token that changes on every request. The proxy resolves it once and pins it,
because re-resolving mid-stream breaks media-sequence continuity. It re-resolves
automatically on a 403/404, including when an expired presigned segment returns 403.

**Live streams have no seek.** Playback starts at the live edge. This is expected.

## When a stream stops after it was playing

"It worked for a while and then stopped" has three causes that look identical to the
viewer. Check them in this order. The first check is quick and usually finds the cause.

### 1. Is the origin still publishing?

Ask the upstream directly, not the proxy. The proxy mirrors a dead source, so it looks
stuck either way.

```
for i in 1 2 3; do
  curl -s [-H "Referer: <referer>"] "<playlist-url>" | grep MEDIA-SEQUENCE
  sleep 5
done
```

A live source gains about one sequence number per segment duration. If it is unchanged
across all three samples, **the source stopped**, and no proxy setting fixes that. Gaps
from 15 seconds to eight minutes have been measured on one URL in one evening, and a
receiver gives up after about 25 seconds without new media, so most of those gaps end
the session regardless. Go to *Finding another source* below.

### 2. If it is advancing, read the log

Every log line is timestamped, which shows whether the receiver or the playlist went
quiet first.

```
tail -40 /tmp/hls_proxy.log
```

The pattern to look for while AirPlay is playing:

```
17:41:12 192.168.1.50 "GET /seg/..."      <- receiver fills its buffer
17:41:14 192.168.1.50 "GET /live.m3u8"    <- then polls...
17:41:27 192.168.1.50 "GET /live.m3u8"    <- ...and never fetches another segment
```

Playlist polls with no segment fetches after them mean the receiver keeps getting a
playlist with nothing new in it. That indicates a frozen origin; go back to step 1.

### 3. If the app drove the AirPlay itself

The web app logs why a session ended, which separates running out of media from losing
the session:

```
docker logs play-web-stream 2>&1 | grep 'airplay:'
```

| Ending | Means |
|---|---|
| `the receiver reported it stopped` | the receiver decided; almost always it ran out of media |
| `feeding it failed: <exc>` | the session was lost underneath us; a transport or network fault |

### Finding another source

When the origin is dead, the event is usually still available elsewhere. These
aggregator pages list mirrors for the same fixture. Extract the candidates from the page:

```
curl -s -A "<the UA at the top of hls_proxy.py>" "<page-url>" \
  | grep -oiE 'href="[^"]*"' | sort -u
```

Ignore site navigation and keep the event-specific links: sibling hosts (`live2.`,
`live3.`…) and third-party players naming the same fixture. Resolve each through the
normal pipeline, so anything found is playable by this skill:

```
python3 ~/.claude/skills/play-web-stream/resolve.py "<candidate-page-url>"
```

Tell the user about these two cases instead of skipping them:

- **Many candidates resolve to the same upstream.** These aggregators share backends, so
  a different page is often the same dead stream. Compare the resolved playlist URLs and
  drop duplicates before testing.
- **Re-resolving the original page does not help.** It returns the same playlist URL.
  The URL does not change; the stream behind it restarts.

Then test the remaining candidates for stability, in parallel, before sending one to a
TV:

```
for i in $(seq 1 24); do
  curl -s [-H "Referer: <referer>"] "<playlist-url>" | grep -o 'MEDIA-SEQUENCE:[0-9]*'
  sleep 5
done
```

Two minutes with the sequence advancing on every sample puts a source on the shortlist.
It only shows the source is not frozen *now*, not that it will still be running in
twenty minutes. Report it as a candidate, not a guarantee.

## Checking the proxy itself

```
python3 ~/.claude/skills/play-web-stream/hls_proxy.py --self-test
```

Runs the suite in `tests/`: playlist rewriting (master variants, alternate audio
renditions, AES-128 keys, fMP4 init sections, relative vs absolute URIs), container
sniffing, byte ranges, the signed URIs, the segment cache, and the resolver's verdicts.
It also runs end-to-end checks against a fake origin that gates on a Referer or on the
client and serves MPEG-TS as `text/plain`. Run it after editing the script.

It needs pytest (`pip install -r requirements-dev.txt`). The proxy itself still runs on
the standard library alone.

## Fallback

If the stream is DASH, DRM-protected, or the proxy cannot get AVFoundation to play it,
use screen mirroring. It works for any video and needs none of the above:

Control Center → Screen Mirroring → Apple TV. To avoid mirroring the whole desktop, set
the display to **Use As Separate Display**, drag the browser window onto it, and
fullscreen the video there.

## Scope

Works on open HLS streams. Does not work on DRM streams (Widevine or FairPlay): the
segments are encrypted, so re-serving them does nothing. For those, use screen mirroring
or the service's own cast button.
