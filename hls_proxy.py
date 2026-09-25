#!/usr/bin/env python3
"""Re-serve an HLS stream with correct MIME types so AVFoundation will play it natively.

Browser players built on hls.js feed bytes to Media Source Extensions, which ignores
Content-Type and never exposes an AirPlay route. Safari and QuickTime play HLS through
AVFoundation instead, which does expose AirPlay -- but AVFoundation refuses segments
whose Content-Type is wrong, and stream CDNs routinely serve MPEG-TS as text/plain.

This proxy sits in between: it fetches the upstream playlist with whatever headers the
origin demands, rewrites every URI to point back at itself, and streams each segment
through with a Content-Type sniffed from the actual bytes.

It binds all interfaces because AirPlay hands the media URL to the receiver, which then
fetches it from this machine over the LAN. To keep that exposure bounded it serves under
an unguessable path and shuts itself down once nothing has been watching for a while.

    python3 hls_proxy.py --discover <page-url>
    python3 hls_proxy.py --source <playlist-url> [--referer <url>]
    python3 hls_proxy.py --source <playlist-url> --probe
    python3 hls_proxy.py --self-test
"""

import argparse
import base64
import collections
import concurrent.futures
import contextlib
import email.message
import errno
import glob
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15")

PLAYLIST_MIME = "application/vnd.apple.mpegurl"
# A Host header is client-supplied, so it is matched rather than trusted: hostname or
# IPv4, bracketed IPv6, optional port, nothing else.
HOST_HEADER = re.compile(r'^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.\-]+)(?::\d{1,5})?$')
URI_ATTR = re.compile(r'(URI=")([^"]+)(")')
SNIFF_BYTES = 65536
DEFAULT_PORT = 8787
PORT_SEARCH_RANGE = 20

# Every URI this proxy rewrites is signed with a key that lives and dies with the
# process. Without that check /seg/ fetches whatever URL is handed to it, which makes
# the path token a key to the whole LAN, and that token travels: it is in a URL people
# paste, open on a television and hand to each other.
_SIGN_KEY = secrets.token_bytes(32)

opts = None
last_activity = time.time()


# ----------------------------------------------------------------------------- egress

# Where our own requests leave from. What is at stake is the address a stream origin
# sees: with a proxy set, every fetch this process makes for somebody else's media goes
# out through it, and the origin sees the exit node rather than this network.
#
# It is deliberately not the whole machine's traffic. This proxy also *serves* the LAN --
# Safari asks it for segments, and an Apple TV fetches them off it directly -- and
# routing that side through a tunnel would break AirPlay while hiding nothing. So the
# split is by destination: upstream through the proxy, this network direct. The two
# halves of that rule are `egress_bypass` here and BYPASS_LIST for chromium.
PROXY_ENV = ("PWS_EGRESS_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")

# Chromium's own bypass syntax, handed to it through playwright, saying what
# `egress_bypass` says for urllib. Chromium keeps loopback direct without being told.
BYPASS_LIST = ("localhost,*.local,*.lan,*.internal,*.home.arpa,127.0.0.0/8,10.0.0.0/8,"
               "172.16.0.0/12,192.168.0.0/16,169.254.0.0/16,::1,fc00::/7,fe80::/10")

PRIVATE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in
                         ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                          "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10"))
DIRECT_SUFFIX = (".local", ".lan", ".internal", ".home.arpa")

# Asked through the proxy, and only through it -- see `egress_check`.
PROBE_URL = "https://api.ipify.org"


def egress_proxy():
    """The proxy upstream fetches go through, or "" when there is none.

    PWS_EGRESS_PROXY names it; the conventional variables are honoured after it, so a
    host that already exports `https_proxy` needs nothing new. Naming it the first way
    is the better one here: the second sends every other library in the process out the
    same door, the container's own health check included, and that door is a tunnel
    that cannot route back to 127.0.0.1.

    A bare host:port is read as http://host:port, which is what a proxy that omits the
    scheme means by it.
    """
    for name in PROXY_ENV:
        value = os.environ.get(name, "").strip()
        if value:
            return value if "://" in value else "http://" + value
    return ""


def egress_label(proxy=None):
    """The proxy as it is safe to print: no credentials, just where it is."""
    proxy = egress_proxy() if proxy is None else proxy
    if not proxy:
        return ""
    parts = urllib.parse.urlsplit(proxy)
    try:
        port = ":%d" % parts.port if parts.port else ""
    except ValueError:                        # a port that is not a number
        port = ""
    host = parts.hostname or ""
    host = "[%s]" % host if ":" in host else host
    return "%s://%s%s%s" % (parts.scheme, host, port,
                            " (authenticated)" if parts.username else "")


def _no_proxy_hosts():
    raw = os.environ.get("no_proxy", "") or os.environ.get("NO_PROXY", "")
    return [entry.strip().lower().lstrip(".") for entry in raw.split(",") if entry.strip()]


def egress_bypass(host):
    """Whether `host` is fetched directly rather than through the egress proxy.

    Everything on this network is. The proxy is here to keep our address from stream
    origins, and the LAN already has it; sending loopback through it would mean a health
    check leaving the house to come back, and sending the LAN through it would mean
    handing an exit node an address it cannot route at all.

    The test is on the literal host, so a *name* that resolves to a LAN address is not
    caught. Upstream media is named and addressed publicly, so what that misses is a
    fetch nothing here makes.
    """
    host = (host or "").strip().lower()
    if host.startswith("["):                  # bracketed IPv6, with or without a port
        host = host.partition("]")[0][1:]
    elif host.count(":") == 1:                # host:port; a bare IPv6 has more colons
        host = host.partition(":")[0]
    if not host:
        return True
    for entry in _no_proxy_hosts():
        if entry == "*" or host == entry or host.endswith("." + entry):
            return True
    if host == "localhost" or host.endswith(DIRECT_SUFFIX):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in network for network in PRIVATE_NETWORKS)


class EgressProxyHandler(urllib.request.ProxyHandler):
    """A ProxyHandler holding to our idea of what stays on this network."""

    def proxy_open(self, req, proxy, type):
        if egress_bypass(req.host or ""):
            return None                       # handled by the plain handler instead
        return urllib.request.ProxyHandler.proxy_open(self, req, proxy, type)


_openers = {}
_opener_lock = threading.Lock()


def egress_opener():
    """An opener pinned to the egress proxy -- or to no proxy, when none is set.

    urlopen's own opener reads the environment itself and applies `no_proxy` by rules
    that predate CIDR and know nothing about a LAN. Pinning it here means one answer for
    every fetch in this process, and the same answer in the proxy the web app spawns,
    which inherits the environment and arrives at it again.
    """
    proxy = egress_proxy()
    with _opener_lock:
        opener = _openers.get(proxy)
        if opener is None:
            proxies = {"http": proxy, "https": proxy} if proxy else {}
            opener = urllib.request.build_opener(EgressProxyHandler(proxies))
            _openers[proxy] = opener
        return opener


def egress_playwright():
    """The egress proxy as playwright's launch(proxy=...) wants it, or None.

    Chromium takes no credentials in --proxy-server; playwright answers the 407 with
    them instead, which is why the browser fallback is given this rather than an
    argument. Without it that fallback is the one fetch in the pipeline that would
    still leave from this network's own address.
    """
    proxy = egress_proxy()
    if not proxy:
        return None
    parts = urllib.parse.urlsplit(proxy)
    spec = {"server": "%s://%s" % (parts.scheme, parts.netloc.rpartition("@")[2]),
            "bypass": BYPASS_LIST}
    if parts.username:
        spec["username"] = urllib.parse.unquote(parts.username)
        spec["password"] = urllib.parse.unquote(parts.password or "")
    return spec


def force_proxy():
    """Whether a stream is served through this proxy even when it would play direct.

    With an egress proxy set this defaults on, and it is the point of the exercise. When
    the resolver finds a stream that needs no rewriting, the app hands the origin URL
    straight to the player -- and the player, or the Apple TV the URL was passed to,
    then fetches the origin itself from this network's address. One stream that needed
    no fixing undoes the hiding. PWS_FORCE_PROXY=0 says to accept that.
    """
    raw = os.environ.get("PWS_FORCE_PROXY", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(egress_proxy())


def egress_check(timeout=8):
    """Report the address upstream origins see us as, by asking through the proxy.

    Through it and only through it. Asking directly as well would hand this network's
    address to the very service being asked to describe it -- the one thing the proxy
    exists to prevent -- so a tunnel that is down reads here as an error rather than as
    a second answer.
    """
    proxy = egress_proxy()
    report = {"enabled": bool(proxy), "proxy": egress_label(proxy),
              "forced": force_proxy(), "ip": "", "error": ""}
    if not proxy:
        return report
    url = os.environ.get("PWS_EGRESS_PROBE_URL", "").strip() or PROBE_URL
    try:
        with egress_opener().open(build_request(url), timeout=timeout) as resp:
            report["ip"] = scrub(resp.read(64).decode("utf-8", "replace").strip(), 64)
    except Exception as exc:                                      # noqa: BLE001
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
    return report


# --------------------------------------------------------------------------- fetching

def request_headers(referer=None, extra=None):
    """What every upstream request carries: a browser's UA and, when the origin demanded
    one, the Referer and the Origin that goes with it."""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(referer))
    headers.update(extra or {})
    return headers


def build_request(url, referer=None, extra=None):
    return urllib.request.Request(url, headers=request_headers(referer, extra))


def _urlopen(url, referer, timeout, extra=None):
    """One GET, presenting whichever client this process currently is."""
    if handshake() == "browser":
        return _browser_open(url, request_headers(referer, extra), timeout)
    return egress_opener().open(build_request(url, referer, extra), timeout=timeout)


RETRY_BACKOFF = 0.4
MAX_FETCH = 16 * 1024 * 1024    # a playlist or a page; segments stream, never buffered


def _open_once(url, referer, timeout, extra):
    try:
        return _urlopen(url, referer, timeout, extra)
    except urllib.error.HTTPError as exc:
        if not referer or exc.code not in (401, 403):
            raise
        exc.close()     # an HTTPError holds the body; the retry's is the one we keep
        # A few origins gate the playlist on a Referer and refuse one on the media.
        return _urlopen(url, None, timeout, extra)


def open_media(url, referer=None, timeout=25, extra=None, retries=0):
    """Open a URL with the Referer the origin demanded, retrying once without it.

    A Referer is only ever supplied because the resolver proved the origin wanted one,
    so it belongs on every request -- segments as much as playlists. Fetching segments
    bare meant a Referer-gated stream resolved cleanly, started a proxy, and then 403ed
    on every segment, which the caller read as an expired presign.

    A transient failure -- a 5xx, a reset, a timeout -- is retried `retries` times.
    Only the playlist poll asks for that: a playlist that fails to refresh ends the
    stream, while a segment retried is a segment arriving after the buffer wanted it.
    """
    for attempt in range(retries):
        try:
            return _open_once(url, referer, timeout, extra)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:          # a refusal is an answer; only a fault is retried
                raise
            exc.close()
        except OSError:                 # URLError and socket timeouts both land here
            pass
        time.sleep(RETRY_BACKOFF * (attempt + 1))
    return _open_once(url, referer, timeout, extra)


def fetch(url, referer=None, timeout=25, retries=1):
    """Read a playlist or a page whole. Segments are streamed, never fetched."""
    with open_media(url, referer, timeout, retries=retries) as resp:
        body = resp.read(MAX_FETCH + 1)
        if len(body) > MAX_FETCH:
            raise ValueError("%s returned over %d bytes, so it is not a playlist"
                             % (url, MAX_FETCH))
        return body, resp.headers.get("Content-Type", "")


def fetch_head(url, referer=None, timeout=25, limit=SNIFF_BYTES):
    """Read only the opening bytes -- enough to name a container, not a download."""
    with open_media(url, referer, timeout) as resp:
        return resp.read(limit), resp.headers.get("Content-Type", "")


# --------------------------------------------------------------------------- handshake

# Some origins never read the headers. A JA3/JA4 hash of the TLS ClientHello says the
# client is Python's OpenSSL rather than any browser -- the cipher list, the extension
# order, the ALPN set -- and they refuse on that alone, which is why no header profile
# changes their answer. curl_cffi presents a real browser's handshake. It is optional:
# the proxy is the piece that runs anywhere with nothing installed, so without it every
# path behaves as it always has and a gated origin is reported as the dead end it is.
try:
    from curl_cffi import requests as _curl
except ImportError:                                               # optional dependency
    _curl = None

# The handshake and the User-Agent have to name the same browser: Safari's UA over a
# Chrome ClientHello is a tell of its own. This is the Safari behind UA above, and the
# User-Agent curl_cffi sends for it is that string byte for byte, so the headers it
# adds are left alone rather than overridden.
IMPERSONATE = "safari18_0"

_handshake = "python"                 # the process default, or "browser" once told
_scoped = threading.local()           # a per-thread override, while a resolve is in flight
_browser = None
_browser_for = None                   # the egress proxy the session was built with
_browser_lock = threading.Lock()


def handshake_available():
    """Whether a browser's handshake can be presented at all."""
    return _curl is not None and IMPERSONATE in {kind.value for kind in _curl.BrowserType}


def handshake():
    """Which client upstream requests present right now: "python" or "browser"."""
    return getattr(_scoped, "handshake", None) or _handshake


def use_browser_handshake(on=True):
    """Switch fetches to a browser's handshake, or back to Python's.

    Process-wide, unless a `handshake_scope()` is open on this thread -- then it lands
    there and nowhere else. A proxy serves one source, so process-wide is right for it:
    an origin that screens the handshake screens it on every segment, not just the
    playlist. The web app resolves several sources at once, and is the reason the
    scope exists.
    """
    global _handshake
    if on and not handshake_available():
        raise RuntimeError("a browser's handshake needs curl_cffi: pip install curl_cffi")
    value = "browser" if on else "python"
    if getattr(_scoped, "handshake", None) is not None:
        _scoped.handshake = value
    else:
        _handshake = value


@contextlib.contextmanager
def handshake_scope(presenting=None):
    """Confine handshake switches to this thread for the duration of the block.

    Without it, two resolves running at once in the same process share one switch: the
    browser handshake one origin demanded follows every fetch the other makes, and
    whichever finishes first puts the switch back while the other is still using it.
    The symptom is an intermittent "origin refuses this client" on a stream that
    resolves cleanly when nothing else is running -- so it costs a proxy that was
    about to be started with the wrong handshake, or none at all.

    Nested scopes restore the value they found, so a caller inside another caller's
    block cannot strand it. `presenting` carries a scope into a thread this one hands
    work to, which is not something a per-thread value does by itself -- see the pool
    in `discover()`.
    """
    outer = getattr(_scoped, "handshake", None)
    _scoped.handshake = presenting or _handshake
    try:
        yield
    finally:
        _scoped.handshake = outer


def _browser_session():
    """The curl_cffi session, built once -- and again if the egress proxy has changed.

    The proxy is set on the session rather than per request because this session only
    ever fetches upstream media; the LAN is served by the handler, never fetched. It is
    stated explicitly rather than left to libcurl's own reading of the environment, so
    that both clients in this process agree on where they leave from.
    """
    global _browser, _browser_for
    proxy = egress_proxy()
    with _browser_lock:
        if _browser is None or _browser_for != proxy:
            if _browser is not None:
                _browser.close()
            _browser = _curl.Session(impersonate=IMPERSONATE,
                                     proxies={"http": proxy, "https": proxy} if proxy else {})
            _browser_for = proxy
        return _browser


class BrowserResponse:
    """A streamed curl_cffi response behind the interface urlopen's response has.

    Everything downstream -- fetch, the sniff, the segment relay -- reads .status,
    .headers and .read(n) off whatever open_media hands back, so this answers the same
    way. One difference is hidden here: libcurl undoes any Content-Encoding as the body
    arrives, which leaves the origin's Content-Length describing bytes we no longer
    have. It is dropped, and the relay streams to close instead of promising a length.
    """

    def __init__(self, resp):
        self._resp = resp
        self._chunks = resp.iter_content()
        self._buffer = b""
        self._done = False
        self.status = resp.status_code
        self.reason = resp.reason
        self.headers = email.message.Message()
        for name, value in resp.headers.items():
            self.headers[name] = value
        if self.headers.get("Content-Encoding", "identity").lower() != "identity":
            del self.headers["Content-Length"]

    def read(self, amount=-1):
        """Up to `amount` bytes, everything when negative, and b"" once the body is done."""
        parts, have = [self._buffer], len(self._buffer)
        while not self._done and (amount < 0 or have < amount):
            chunk = next(self._chunks, None)
            if chunk is None:
                self._done = True
                break
            parts.append(chunk)
            have += len(chunk)
        data = b"".join(parts)
        if amount < 0:
            self._buffer = b""
            return data
        self._buffer = data[amount:]
        return data[:amount]

    def close(self):
        self._done = True
        self._resp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _browser_open(url, headers, timeout):
    """GET `url` as a browser, raising what urlopen would raise.

    curl_cffi's own headers stay, since they are the ones its handshake goes with; only
    what the origin asked of us -- Referer, Origin, Range -- is added on top.
    """
    headers = {name: value for name, value in headers.items() if name.lower() != "user-agent"}
    try:
        resp = _browser_session().get(url, headers=headers, stream=True, timeout=timeout)
    except _curl.RequestsError as exc:
        raise urllib.error.URLError(str(exc)) from None
    wrapped = BrowserResponse(resp)
    if wrapped.status >= 400:
        wrapped.close()
        raise urllib.error.HTTPError(url, wrapped.status, wrapped.reason, wrapped.headers,
                                     io.BytesIO())
    return wrapped


def scrub(text, limit=200):
    """A client-supplied string, made safe to write as one line of the log."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", text)[:limit]


def sniff_mime(data, fallback="application/octet-stream"):
    """Decide a Content-Type from the bytes, since the origin's own header is unreliable."""
    if not data:
        return fallback
    if data[:1] == b"#" and b"#EXTM3U" in data[:64]:
        return PLAYLIST_MIME
    if data[4:8] in (b"ftyp", b"styp", b"moof", b"sidx"):
        return "video/mp4"
    if data[0] == 0x47:
        # MPEG-TS packets are 188 bytes each and every one starts with a sync byte.
        probe = range(0, min(len(data), 188 * 20), 188)
        if all(data[i] == 0x47 for i in probe):
            return "video/MP2T"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf1", b"\xff\xf9"):
        return "audio/aac"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    return fallback


# How far into a segment to look for media hiding behind something else. Generous:
# the shim seen in the wild is 42 bytes, and reading further costs one pass over
# bytes already in hand.
SHIM_MAX = 4096
SHIM_PACKETS = 20       # sync bytes that must line up before this is believed


def media_offset(data):
    """Where the real media starts, when something is glued in front of it.

    One origin serves its segments from an image CDN, which only accepts images: each
    one is a 42-byte RIFF/WEBP header followed by an ordinary MPEG-TS segment, whole
    packets all the way to the end. The bytes play perfectly once the header is gone,
    and a decoder handed the header sees a broken container instead -- Safari on iOS
    tolerates it, an Apple TV plays for a few seconds and reports the item stopped.

    Zero for everything that begins with its own container, which is every ordinary
    stream and costs one comparison. Twenty sync bytes at their exact spacing is the
    evidence required, so a file that merely contains a 0x47 is not mistaken for this.
    """
    if not data or data[0] == 0x47:
        return 0
    if data[4:8] in (b"ftyp", b"styp", b"moof", b"sidx"):
        return 0                                   # fMP4, carrying its own header
    for offset in range(1, min(len(data), SHIM_MAX)):
        if data[offset] != 0x47:
            continue
        probe = range(offset, min(len(data), offset + 188 * SHIM_PACKETS), 188)
        if len(data) - offset >= 188 * SHIM_PACKETS and all(data[i] == 0x47 for i in probe):
            return offset
    return 0


# --------------------------------------------------------------------------- playlists

def is_master(text):
    return "#EXT-X-STREAM-INF" in text


PLAYLIST_TYPE = re.compile(r'^#EXT-X-PLAYLIST-TYPE:\s*(VOD|EVENT)\s*$', re.M)


def keeps_everything(text):
    """Whether a media playlist promises never to drop a segment.

    VOD says the playlist will not change at all, and EVENT that it only grows. Either
    way there is no edge to fall off, so accumulate() has nothing to add -- and what it
    does do is wrong for them: it keeps the last few segments of the whole programme
    and strips #EXT-X-ENDLIST, which leaves a playlist declaring itself VOD with no end
    and no beginning. An Apple TV handed Apple's own bipbop example like that read the
    playlist twice and never asked for a segment. Only the declared type counts: an
    #EXT-X-ENDLIST on a playlist that declares none is the live case, and its handling
    stays as it was.
    """
    return PLAYLIST_TYPE.search(text) is not None


def rewrite(text, base_url, self_prefix):
    """Point every URI in a playlist back at this proxy.

    Variant playlists route to /pl/ so their own segments get rewritten in turn;
    segments, keys, and init sections route to /seg/.
    """
    media_route = "pl" if is_master(text) else "seg"
    out = []

    for line in text.splitlines():
        stripped = line.strip()

        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            # #EXT-X-KEY, #EXT-X-MAP and #EXT-X-MEDIA carry URIs in an attribute.
            def sub(m, tag=stripped):
                target = urllib.parse.urljoin(base_url, m.group(2))
                route = "pl" if "EXT-X-MEDIA" in tag else "seg"
                return m.group(1) + encode_url(target, route, self_prefix) + m.group(3)
            out.append(URI_ATTR.sub(sub, line))
        else:
            target = urllib.parse.urljoin(base_url, stripped)
            out.append(encode_url(target, media_route, self_prefix))

    return "\n".join(out) + "\n"


class BadToken(Exception):
    """A /seg/ or /pl/ token this proxy did not sign."""


def sign(route, url):
    mac = hmac.new(_SIGN_KEY, ("%s\x00%s" % (route, url)).encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()[:12]).decode().rstrip("=")


def encode_url(url, route, self_prefix):
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return "{}/{}/{}.{}".format(self_prefix, route, token, sign(route, url))


def decode_url(token, route):
    """Recover the URL from a token this proxy signed, or refuse it.

    The route is signed along with the URL, so a segment token cannot be replayed
    against /pl/, which would hand back a rewritten copy of something that is not a
    playlist. The base64 alphabet is url-safe and so never contains the separator.
    """
    blob, dot, mac = token.partition(".")
    if not dot:
        raise BadToken("unsigned token")
    pad = "=" * (-len(blob) % 4)
    try:
        url = base64.urlsafe_b64decode((blob + pad).encode()).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadToken(str(exc)) from exc
    if not hmac.compare_digest(mac, sign(route, url)):
        raise BadToken("bad signature")
    return url


# --------------------------------------------------------------------------- window

# Tags that belong to the segment that follows them, rather than to the playlist.
SEGMENT_TAGS = ("#EXTINF", "#EXT-X-BYTERANGE", "#EXT-X-DISCONTINUITY",
                "#EXT-X-KEY", "#EXT-X-MAP", "#EXT-X-PROGRAM-DATE-TIME")

_window_lock = threading.Lock()
# One window per playlist URL. A master routes its variants and its #EXT-X-MEDIA
# renditions through /pl/, so several media playlists are in flight at once; a single
# shared window keyed by media sequence merged them into each other, putting audio
# segments in the video playlist. Capped, because a re-resolved source arrives under a
# freshly signed URL and the old window is then dead weight.
_windows = collections.OrderedDict()    # playlist url -> {sequence: (tags, uri)}
_window_headers = {}                    # playlist url -> header tags
_window_epochs = {}                     # playlist url -> {"offset", "high", "clock"}
WINDOW_PLAYLISTS = 8

EXTINF = re.compile(r'^#EXTINF:\s*([0-9.]+)')
DATE_TAG = "#EXT-X-PROGRAM-DATE-TIME"


def segment_seconds(tags, fallback=4.0):
    """How long the segment behind these tags runs, per its #EXTINF."""
    for tag in tags:
        found = EXTINF.match(tag)
        if found:
            try:
                return float(found.group(1))
            except ValueError:
                break
    return fallback


def stamp(when):
    """One #EXT-X-PROGRAM-DATE-TIME, in the form the spec asks for."""
    text = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when))
    return "%s:%s.%03dZ" % (DATE_TAG, text, int((when % 1) * 1000))


def parse_media_playlist(text, base_url):
    """Split a media playlist into its header tags and (sequence, tags, url) segments."""
    header, segments, pending = [], [], []
    seq = 0
    seen_segment = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
            continue
        if line.startswith("#EXT-X-ENDLIST"):
            continue
        if line.startswith("#"):
            if line.startswith(SEGMENT_TAGS):
                pending.append(line)
            elif not seen_segment:
                header.append(line)
            continue
        segments.append((seq + len(segments), pending, urllib.parse.urljoin(base_url, line)))
        pending = []
        seen_segment = True

    return header, segments


def is_restart(segments, window, epoch):
    """Whether this poll is a new stream rather than the next few seconds of this one.

    These origins restart: the media sequence drops from 497 back to 64 and the
    encoder begins again. Numbering the window by the origin's sequence then put the
    fresh segments *below* the ones already held, so the trim below deleted them on
    arrival, the newest segment stayed a dead pre-restart one, and the playlist never
    advanced again -- the stall that ends the stream for good.

    A restart is the whole poll sitting behind everything we hold. An origin briefly
    serving an older copy of its playlist looks the same by sequence alone, so it has
    to bring segments we do not already have before it counts as one.
    """
    if not segments or epoch["high"] is None:
        return False
    if segments[-1][0] + epoch["offset"] >= epoch["high"]:
        return False
    held = {url for _, url in window.values()}
    return any(url not in held for _, _, url in segments)


def is_gap(segments, epoch):
    """Whether this poll starts beyond anything the window could join onto."""
    if not segments or epoch["high"] is None:
        return False
    return segments[0][0] + epoch["offset"] > epoch["high"] + 1


def accumulate(text, base_url):
    """Build a longer playlist than the origin publishes, by remembering past segments.

    These streams often expose only three segments -- about fifteen seconds. A player
    that buffers ten seconds sits right at the edge of that window, and the moment it
    slips past it is waiting on a segment that no longer exists, which stalls it for
    good. Re-advertising recent segments gives it room to fall behind and recover.

    The window is numbered by the origin's sequence plus an offset, so the numbering
    we publish only ever climbs. The offset is what absorbs a restart: see
    is_restart(). AVFoundation reads a media sequence that goes backwards as a
    different stream, which is the other half of why a restart used to be fatal.

    Every segment is stamped with a wall-clock time unless the origin already gave it
    one. These origins publish none, and a live playlist carrying no dates leaves a
    player with nothing to place the stream on a timeline with -- while the AirPlay
    hand-off tells the receiver, as the reference it was ported from does, that it is
    interested in date ranges. The streams that hand-off is known to work against all
    carry dates. Ours did not.
    """
    header, segments = parse_media_playlist(text, base_url)

    with _window_lock:
        window = _windows.get(base_url)
        if window is None:
            window = _windows[base_url] = {}
            _window_epochs[base_url] = {"offset": 0, "high": None, "clock": None}
            while len(_windows) > WINDOW_PLAYLISTS:
                dropped, _ = _windows.popitem(last=False)
                _window_headers.pop(dropped, None)
                _window_epochs.pop(dropped, None)
        _windows.move_to_end(base_url)
        epoch = _window_epochs[base_url]

        if header:
            _window_headers[base_url] = header

        # A restart is spliced onto the end of the window rather than replacing it, so
        # a player part-way through still has the segments it is working on. The join
        # carries a discontinuity, which is how the decoder is told to expect a new
        # timeline and new codec parameters at that point.
        opening = []
        if is_restart(segments, window, epoch):
            epoch["offset"] = epoch["high"] + 1 - segments[0][0]
            opening = ["#EXT-X-DISCONTINUITY"]
            epoch["clock"] = None       # the new epoch begins at the wall clock, not
                                        #   wherever the old one had run to
        elif is_gap(segments, epoch):
            # Nobody asked for the playlist for longer than the origin keeps a
            # segment, so what we hold and what it publishes no longer touch. Splicing
            # them anyway made a playlist that numbered nine-minute-old segments and
            # fresh ones consecutively, with nothing to say the timeline jumps between
            # them; an Apple TV that started inside the old part hit the jump and
            # ended the item. The old part is dropped instead. The sequence still only
            # climbs, and a player coming back after a pause re-syncs to the live edge,
            # which is what it would have done against the origin itself.
            window.clear()
            epoch["clock"] = None

        fresh = [(seq, tags, url) for seq, tags, url in segments
                 if seq + epoch["offset"] not in window]
        if epoch["clock"] is None and fresh:
            # Anchor so the newest of this batch starts one duration ago, which is when
            # the origin will have published it. Everything after continues from there,
            # so a segment keeps the date it was first given however often it is
            # re-advertised -- a date that moved between reloads would be worse than none.
            epoch["clock"] = time.time() - sum(segment_seconds(t) for _, t, _ in fresh)

        for seq, tags, url in fresh:
            key = seq + epoch["offset"]
            tags = opening + [t for t in tags if t not in opening]
            if not any(t.startswith(DATE_TAG) for t in tags):
                tags = [stamp(epoch["clock"])] + tags
            epoch["clock"] += segment_seconds(tags)
            window[key] = (tags, url)
            opening = []
            epoch["high"] = key if epoch["high"] is None else max(epoch["high"], key)
        for stale in sorted(window)[:-opts.window]:
            del window[stale]

        keys = sorted(window)
        out = list(_window_headers.get(base_url, []))
        out.append("#EXT-X-MEDIA-SEQUENCE:%d" % keys[0])
        for key in keys:
            tags, url = window[key]
            out.extend(tags)
            out.append(url)

    return "\n".join(out) + "\n"


def reset_windows():
    with _window_lock:
        _windows.clear()
        _window_headers.clear()
        _window_epochs.clear()


# --------------------------------------------------------------------------- cache

# Segments are kept as they pass through. accumulate() re-advertises ones the origin
# has already dropped, and without a copy here the player asks for one and gets a 404 --
# the stall the widened window exists to prevent. It also means Safari and the Apple TV,
# which fetch the same stream independently, cost one trip upstream between them.

_cache_lock = threading.Lock()
_cache = collections.OrderedDict()      # segment url -> (content-type, bytes)
_cache_bytes = 0
_cache_limit = 0
SEGMENT_MAX = 24 * 1024 * 1024          # one segment; a larger one streams uncached

# A segment that comes back 403 or 404 is usually a presign that has expired, and the
# answer to that is to re-resolve. Not always, though: a CDN under load refuses a
# request it would serve a moment later with the very same codes, and the segment that
# ended a stream here fetched cleanly thirty times in a row once the burst was over.
# Passing that refusal on ends the stream -- an Apple TV does not forgive a 404 on a
# segment -- so one refusal is retried before it is believed. A presign that really has
# expired costs one extra request to establish that, once.
SEGMENT_REFUSALS = 1


def configure_cache(megabytes):
    global _cache_limit
    _cache_limit = max(0, megabytes) * 1024 * 1024
    reset_cache()


def reset_cache():
    global _cache_bytes
    with _cache_lock:
        _cache.clear()
        _cache_bytes = 0


def cache_get(url):
    with _cache_lock:
        hit = _cache.get(url)
        if hit:
            _cache.move_to_end(url)
        return hit


def cache_put(url, ctype, data):
    """Keep a segment, dropping the least recently served to stay inside the budget."""
    global _cache_bytes
    if not _cache_limit or len(data) > min(SEGMENT_MAX, _cache_limit):
        return
    with _cache_lock:
        if url in _cache:
            _cache_bytes -= len(_cache.pop(url)[1])
        _cache[url] = (ctype, data)
        _cache_bytes += len(data)
        while _cache_bytes > _cache_limit:
            _, (_, evicted) = _cache.popitem(last=False)
            _cache_bytes -= len(evicted)


def cache_size():
    with _cache_lock:
        return len(_cache), _cache_bytes


BYTE_RANGE = re.compile(r'^bytes=(\d*)-(\d*)$')


def parse_range(header, size):
    """Resolve one byte range against a known size, or None for the whole thing.

    Only a single range is honoured. Multipart ranges are legal HTTP and no HLS player
    asks for them, so the whole body is a correct answer rather than a wrong one.
    """
    match = BYTE_RANGE.match((header or "").strip())
    if not match or size <= 0:
        return None
    start, end = match.group(1), match.group(2)
    if not start:
        length = min(int(end), size) if end else 0
        return (size - length, size - 1) if length else None
    low = int(start)
    high = min(int(end), size - 1) if end else size - 1
    return (low, high) if low <= high and low < size else None


# --------------------------------------------------------------------------- resolving

_lock = threading.Lock()
_resolved = None


AUDIO_RENDITION = re.compile(r'#EXT-X-MEDIA:[^\n]*TYPE=AUDIO', re.I)


def should_flatten(text):
    """Whether pinning one variant of this master loses nothing.

    It does lose something when the audio is declared as a separate rendition: the
    variant carries video only, and the #EXT-X-MEDIA line naming its audio lives in the
    master we would be dropping. The stream then plays perfectly, and silently.
    """
    return is_master(text) and not AUDIO_RENDITION.search(text)


def current_source():
    """The resolved source, read under the lock resolve_source() writes it with."""
    with _lock:
        return _resolved


def resolve_source(force=False):
    """Resolve the source URL once and reuse it.

    Some origins hand out a per-session variant path; re-resolving mid-stream would
    return a different session and break media-sequence continuity.
    """
    global _resolved
    with _lock:
        if _resolved and not force:
            return _resolved
        # The first resolution is where a client gate shows itself, and where trying
        # once more as a browser belongs. A re-resolve mid-stream is answering an
        # expired URL, and gets the plain fetch and the plain error it always has.
        fetcher = fetch if _resolved else fetch_through_gate
        body, _ = fetcher(opts.source, referer=opts.referer)
        text = body.decode("utf-8", "replace")
        if opts.flatten and should_flatten(text):
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    _resolved = urllib.parse.urljoin(opts.source, line)
                    return _resolved
        _resolved = opts.source
        return _resolved


# --------------------------------------------------------------------------- serving

def say(text):
    """One log line, stamped. Without the time there is no telling whether a receiver
    went quiet before the playlist stopped advancing or because of it, and that is the
    question every stall reported against this proxy has come down to."""
    sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), text))


# Safari refuses to autoplay video with audio, so an unmuted play() is tried first and
# a muted one is the fallback -- muted autoplay is always permitted. Either way the
# stream is already running by the time the page is looked at; the overlay only exists
# to turn sound back on, which does need a gesture.
PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Stream</title>
<style>
  html, body { margin: 0; height: 100%; background: #000; overflow: hidden; }
  video { width: 100%; height: 100%; }
  #overlay {
    position: fixed; inset: 0; display: none;
    align-items: flex-end; justify-content: center; padding-bottom: 14vh;
    font: 600 15px/1 -apple-system, system-ui, sans-serif;
    color: #fff; cursor: pointer; z-index: 2;
  }
  #overlay span {
    background: rgba(0,0,0,.72); padding: 10px 18px; border-radius: 999px;
    backdrop-filter: blur(8px);
    display: inline-flex; align-items: center; gap: 8px;
  }
  /* The same Heroicons outline set the app's own page uses, so the one screen a person
     reaches over AirPlay is not the one drawn in a different hand. Two glyphs is not
     worth a sprite here. */
  #overlay svg { width: 18px; height: 18px; }
</style>
</head>
<body>
  <video id="v" controls playsinline src="__PREFIX__/live.m3u8"></video>
  <div id="overlay"><span><svg id="glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path
    d="M17.25 9.75 19.5 12m0 0 2.25 2.25M19.5 12l2.25-2.25M19.5 12l-2.25 2.25m-10.5-6
       4.72-4.72a.75.75 0 0 1 1.28.53v15.88a.75.75 0 0 1-1.28.53l-4.72-4.72H4.51c-.88
       0-1.704-.507-1.938-1.354A9.009 9.009 0 0 1 2.25 12c0-.83.112-1.633.322-2.396C2.806
       8.756 3.63 8.25 4.51 8.25H6.75Z"/></svg><span
    id="label">Click to unmute</span></span></div>
<script>
  const v = document.getElementById('v');
  const overlay = document.getElementById('overlay');
  const label = document.getElementById('label');
  const glyph = document.getElementById('glyph');
  // Heroicons' play, for the case where the browser refused playback outright: the
  // overlay is then asking for a tap to start, not a tap to turn the sound on.
  const PLAY = 'M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.347a1.125 1.125 0 0 1 0 1.972'
             + 'l-11.54 6.347a1.125 1.125 0 0 1-1.667-.986V5.653Z';

  async function start() {
    try {
      await v.play();                 // sound allowed: nothing more to do
      return;
    } catch (e) {}
    v.muted = true;
    try {
      await v.play();                 // muted autoplay is always permitted
      overlay.style.display = 'flex';
    } catch (e) {
      label.textContent = 'Click to play';
      glyph.firstElementChild.setAttribute('d', PLAY);
      overlay.style.display = 'flex';
    }
  }

  overlay.addEventListener('click', async () => {
    v.muted = false;
    try { await v.play(); } catch (e) {}
    overlay.style.display = 'none';
  });

  // Report playback state back to the proxy log. Safari blocks JavaScript from Apple
  // Events by default, so this is the only way to see what the element is actually doing.
  // True while the Apple TV, not this element, is the thing actually playing.
  // Everything the element reports about itself is about a pipeline it no longer
  // drives, so the watchdog below has to stand down for the duration.
  function wireless() {
    if (v.webkitCurrentPlaybackTargetIsWireless) return true;
    try { return v.remote && v.remote.state === 'connected'; } catch (e) { return false; }
  }

  function report(name, extra) {
    const q = new URLSearchParams(Object.assign({
      e: name,
      t: v.currentTime.toFixed(1),
      paused: v.paused ? 1 : 0,
      muted: v.muted ? 1 : 0,
      rs: v.readyState,
      ns: v.networkState,
      air: wireless() ? 1 : 0,
      buf: v.buffered.length
        ? (v.buffered.end(v.buffered.length - 1) - v.currentTime).toFixed(1)
        : -1,
    }, extra || {}));
    fetch('__PREFIX__/_evt?' + q, { keepalive: true }).catch(() => {});
  }

  for (const name of ['loadedmetadata','canplay','play','playing','pause','waiting',
                      'stalled','suspend','ended','emptied']) {
    v.addEventListener(name, () => report(name));
  }
  v.addEventListener('error', () => report('error', { code: v.error && v.error.code }));
  v.addEventListener('webkitcurrentplaybacktargetiswirelesschanged',
                     () => report('airplay-changed'));
  setInterval(() => report('tick'), 5000);

  // AVFoundation does not recover on its own from falling off the back of a live
  // window: currentTime simply freezes while the element still claims to be playing.
  // Watch for that and jump to the live edge, reloading only if that fails too.
  //
  // Never while the stream is on an AirPlay receiver. There the element stops
  // fetching segments, its buffered ranges go stale and then empty, and currentTime
  // updates in coarse jumps from the receiver -- all three read as a frozen stream
  // when they are a healthy one. The reload that follows drops the AirPlay route, so
  // the watchdog was the thing ending playback a minute or so in.
  let lastTime = -1, frozenFor = 0;
  setInterval(() => {
    if (v.paused || v.seeking || wireless()) {
      frozenFor = 0; lastTime = v.currentTime; return;
    }
    if (v.currentTime === lastTime) {
      frozenFor += 2;
      if (frozenFor >= 8) {
        frozenFor = 0;
        const n = v.buffered.length;
        const edge = n ? v.buffered.end(n - 1) - 0.5 : 0;
        if (edge > v.currentTime) {
          report('recover-seek', { to: edge.toFixed(1) });
          v.currentTime = edge;
        } else {
          report('recover-reload');
          v.load();
          v.play().catch(() => {});
        }
      }
    } else {
      frozenFor = 0;
      lastTime = v.currentTime;
    }
  }, 2000);

  start();
</script>
</body>
</html>
"""


_types_lock = threading.Lock()
_segment_types = collections.OrderedDict()      # segment url -> Content-Type
_segment_shims = collections.OrderedDict()      # segment url -> bytes before the media
SEGMENT_TYPES = 64


def remember_shim(url, offset):
    """Keep what was found in front of one segment, so a range need not find it again."""
    with _types_lock:
        _segment_shims[url] = offset
        _segment_shims.move_to_end(url)
        while len(_segment_shims) > SEGMENT_TYPES:
            _segment_shims.popitem(last=False)
    return offset


def known_shim(url):
    with _types_lock:
        if url in _segment_shims:
            _segment_shims.move_to_end(url)
            return _segment_shims[url]
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    head_only = False

    def handle_one_request(self):
        # Players hold several connections open and drop the idle ones. That surfaces
        # here as a reset while waiting on the next request line, and is not an error.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        line = fmt % args
        if "/favicon.ico" in line:
            return
        # Tokens are long and drown the log; show only the route and status.
        line = line.replace("/" + opts.token, "")
        line = re.sub(r"/(seg|pl)/[A-Za-z0-9_=.-]+", r"/\1/...", line)
        say("%s %s" % (self.address_string(), line))

    def _send(self, body, ctype, code=200, cache=True):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if not cache:
            self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()
        if not self.head_only:
            self.wfile.write(body)

    def _prefix(self):
        """Build this proxy's own base URL from the address the client used to reach it.

        Pinning the prefix at startup meant one proxy could only ever be reached at one
        address: change networks, or come in over Tailscale instead of the LAN, and every
        rewritten segment URL still pointed at the old one. Deriving it per request means
        the playlist a client gets back always refers to the host that client asked, which
        is what the Apple TV needs when Safari hands the URL over.
        """
        host = self.headers.get("Host", "")
        if not HOST_HEADER.match(host):
            return opts.prefix          # missing or malformed; fall back to the pinned one
        return "http://%s/%s" % (host, opts.token)

    def _serve_playlist(self, url, referer):
        try:
            body, _ = fetch(url, referer=referer)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404) and url == current_source():
                exc.close()
                body, _ = fetch(resolve_source(force=True), referer=referer)
            else:
                raise
        text = body.decode("utf-8", "replace")
        if opts.window and not is_master(text) and not keeps_everything(text):
            text = accumulate(text, url)
        text = rewrite(text, url, self._prefix())
        self._send(text.encode(), PLAYLIST_MIME, cache=False)

    def _segment_type(self, url, head, resp):
        """Name the container, remembering it for the byte ranges that follow.

        An #EXT-X-BYTERANGE stream addresses one file at many offsets, and the bytes
        at offset zero are the ones carrying a header to sniff. The type worked out
        from that request stands in for the rest, since it is the same file throughout.
        Failing that the sniff is still worth trying -- ranges tend to land on a TS
        packet or an fMP4 box boundary -- and only the answer from offset zero is kept.
        """
        upstream = resp.headers.get("Content-Type") or "video/MP2T"
        partial = (resp.headers.get("Content-Range") or "")
        if resp.status == 206 and not partial.startswith("bytes 0-"):
            with _types_lock:
                cached = _segment_types.get(url)
            return cached or sniff_mime(head, upstream)

        ctype = sniff_mime(head, upstream)
        with _types_lock:
            _segment_types[url] = ctype
            _segment_types.move_to_end(url)
            while len(_segment_types) > SEGMENT_TYPES:
                _segment_types.popitem(last=False)
        return ctype

    def _expired_presign(self, exc):
        """Whether this error was an expired presign, which is answered by re-resolving.

        The playlist we handed out is stale, so force a re-resolve; the client's next
        poll gets freshly signed URLs.
        """
        if exc.code not in (403, 404):
            return False
        exc.close()
        resolve_source(force=True)
        say("segment %d (expired presign) - re-resolved source" % exc.code)
        self._send(b"segment expired", "text/plain", 404)
        return True

    def _open_segment(self, url, referer, rng=None):
        """Fetch a segment, giving a refusal one more chance before believing it.

        See SEGMENT_REFUSALS. Only 403 and 404 are retried here; everything else is
        already handled where it arises, and a 5xx by open_media itself.
        """
        extra = {"Range": rng} if rng else None
        for attempt in range(SEGMENT_REFUSALS + 1):
            try:
                return open_media(url, referer, timeout=25, extra=extra)
            except urllib.error.HTTPError as exc:
                if exc.code not in (403, 404) or attempt == SEGMENT_REFUSALS:
                    raise
                exc.close()
                say("segment %d - asking once more before calling it expired" % exc.code)
                time.sleep(RETRY_BACKOFF)
        raise AssertionError("unreachable")          # the loop returns or raises

    def _shim_offset(self, url, referer):
        """How much is glued in front of this segment's media, remembered per URL.

        Whichever request first sees the head of the file works this out. A ranged one
        need not see it, so for a segment we have not met the opening bytes are fetched
        on their own -- once per URL, and never at all for a stream without a shim,
        since the first request for each segment is the unranged one that fills it.

        A probe that fails answers nothing and is not remembered: the next request for
        this segment asks again rather than inheriting a guess made during a blip.
        """
        seen = known_shim(url)
        if seen is not None:
            return seen
        try:
            with self._open_segment(url, referer,
                                    "bytes=0-%d" % (SNIFF_BYTES - 1)) as resp:
                head = resp.read(SNIFF_BYTES)
        except Exception:                                         # noqa: BLE001
            return 0                    # unknowable for now; relay it as it comes
        return remember_shim(url, media_offset(head))

    def _serve_segment(self, url, referer):
        """Answer from the cache if we hold this segment, otherwise from the origin.

        A Range is honoured either way. AVFoundation opens most segments with one, and
        an #EXT-X-BYTERANGE playlist is nothing but ranges -- answering all of them with
        the whole file handed the decoder garbage.
        """
        rng = self.headers.get("Range")
        held = cache_get(url)
        if held:
            self._send_segment(held[1], held[0], rng)
            return

        # A range is measured against the media, not against the file it is buried in,
        # so a shimmed segment cannot be answered by passing the client's range
        # upstream. Fetch it whole and slice it here; the cache means that is one trip
        # for the segment rather than one per range.
        if rng and self._shim_offset(url, referer):
            try:
                resp = self._open_segment(url, referer)
            except urllib.error.HTTPError as exc:
                if self._expired_presign(exc):
                    return
                raise
            with resp:
                body = resp.read(SEGMENT_MAX + 1)
            body = body[media_offset(body[:SNIFF_BYTES]):]
            ctype = sniff_mime(body, resp.headers.get("Content-Type") or "video/MP2T")
            cache_put(url, ctype, body)
            self._send_segment(body, ctype, rng)
            return

        try:
            resp = self._open_segment(url, referer, rng)
        except urllib.error.HTTPError as exc:
            if self._expired_presign(exc):
                return
            raise

        self._relay_segment(url, resp)

    def _send_segment(self, data, ctype, rng):
        """Answer out of bytes we already hold, cutting the range from them ourselves."""
        span = parse_range(rng, len(data))
        body = data[span[0]:span[1] + 1] if span else data

        self.send_response(206 if span else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        if span:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (span[0], span[1], len(data)))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not self.head_only:
            self.wfile.write(body)

    def _relay_segment(self, url, resp):
        """Copy the segment out as it arrives, keeping a copy if it is worth keeping.

        Only the first chunk is held to sniff the container; everything after it goes
        straight to the client, so playback starts without waiting on a full download.
        """
        with resp:
            head = resp.read(SNIFF_BYTES)
            partial = resp.status == 206
            # Only an answer that starts at the beginning of the file can show what is
            # in front of the media, and only then is there anything to take off.
            offset = 0 if partial else remember_shim(url, media_offset(head))
            head = head[offset:]
            ctype = self._segment_type(url, head, resp)
            length = resp.headers.get("Content-Length")
            if length and offset:
                length = str(max(int(length) - offset, 0))

            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Accept-Ranges", "bytes")
            if partial and resp.headers.get("Content-Range"):
                self.send_header("Content-Range", resp.headers["Content-Range"])
            if length:
                self.send_header("Content-Length", length)
            else:
                # Length unknown upstream, so the client reads until we hang up.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()

            if self.head_only:
                return

            # A ranged answer is a slice of a file, not the segment, so it is never
            # kept under the segment's own URL. Anything outsized stops being kept the
            # moment it outgrows the budget, and still finishes streaming.
            kept = None if partial else [head]
            size = len(head)
            self.wfile.write(head)

            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if kept is not None:
                    if size > SEGMENT_MAX:
                        kept = None
                    else:
                        kept.append(chunk)
                self.wfile.write(chunk)

            if kept is not None:
                cache_put(url, ctype, b"".join(kept))

    def do_HEAD(self):
        """Answer HEAD with the GET headers and no body.

        AVFoundation opens a segment with one before it commits to the download, and
        the default handler answers 501, which reads to the player as a broken stream.
        """
        self.head_only = True
        try:
            self.do_GET()
        finally:
            self.head_only = False

    def do_GET(self):
        global last_activity
        path = self.path.split("?")[0]
        # Telemetry must not count as activity, or a paused tab would keep the proxy
        # alive forever and defeat the idle shutdown.
        if not path.endswith("/_evt"):
            last_activity = time.time()
        prefix = "/" + opts.token
        if path == prefix:
            path = "/"
        elif path.startswith(prefix + "/"):
            path = path[len(prefix):]
        else:
            self._send(b"not found", "text/plain", 404)
            return

        try:
            if path in ("/", "/index.html"):
                body = PAGE.replace("__PREFIX__", self._prefix()).encode()
                self._send(body, "text/html; charset=utf-8")

            elif path == "/_evt":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                # Whoever holds the token writes these, and /api/log serves them
                # back to the operator. Control characters would let a line be forged
                # in the middle of another one, so they do not survive the trip.
                fields = " ".join("%s=%s" % (k, scrub(v[0]))
                                  for k, v in sorted(query.items()) if k != "e")
                say("  [player] %-16s %s" % (scrub(query.get("e", ["?"])[0]), fields))
                self._send(b"", "text/plain", 200)

            elif path == "/live.m3u8":
                self._serve_playlist(resolve_source(), opts.referer)

            elif path.startswith("/pl/"):
                self._serve_playlist(decode_url(path[4:], "pl"), opts.referer)

            elif path.startswith("/seg/"):
                self._serve_segment(decode_url(path[5:], "seg"), opts.referer)

            else:
                self._send(b"not found", "text/plain", 404)

        except (BrokenPipeError, ConnectionResetError):
            pass
        except BadToken as exc:
            say("refused a token on %s: %s" % (path, exc))
            self._send(b"not found", "text/plain", 404)
        except Exception as exc:
            say("error %s: %r" % (path, exc))
            try:
                self._send(str(exc).encode(), "text/plain", 502)
            except Exception:
                pass


# --------------------------------------------------------------------------- lifecycle

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))  # TEST-NET-1; no packets are sent
        return s.getsockname()[0]
    finally:
        s.close()


def bind_server(first_port, last_port=None):
    """Take the first free port at or above first_port, so concurrent streams coexist.

    `last_port` is a ceiling somebody else is holding us to -- under docker, the end of
    the range published at deploy time. Binding past it would succeed inside the
    container and be unreachable outside it, which is worse than not binding at all, so
    the ceiling is honoured rather than the search range.
    """
    end = last_port + 1 if last_port else first_port + PORT_SEARCH_RANGE
    for port in range(first_port, end):
        try:
            return ThreadingHTTPServer(("0.0.0.0", port), Handler), port
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
    raise SystemExit("no free port in %d-%d" % (first_port, end - 1))


def state_dir():
    """A directory only this user can read, for files that name a token and a pid.

    They used to sit in /tmp under a predictable name and the default umask: any local
    account could read a running stream's token, or plant a file naming a pid of its
    choosing for the web app to go and signal. Ownership is checked rather than assumed,
    so a directory somebody else got there first with is refused instead of used.
    """
    path = os.path.join(tempfile.gettempdir(), "play-web-stream-%d" % os.getuid())
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SystemExit("%s is not a directory this user owns; refusing to use it" % path)
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def state_path(source):
    digest = hashlib.sha1(source.encode()).hexdigest()[:12]
    return os.path.join(state_dir(), "hls_proxy_%s.json" % digest)


def state_files():
    """Every proxy's state file, for the web app's listing of what is running."""
    return sorted(glob.glob(os.path.join(state_dir(), "hls_proxy_*.json")))


def owns_pid(pid):
    """Whether this pid is one of our proxies, so a recycled one is not signalled.

    Without procfs there is nothing to read, and the state file -- now in a directory
    only this user can write -- is trustworthy enough on its own.
    """
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return b"hls_proxy.py" in fh.read()
    except OSError:
        return not os.path.isdir("/proc")


def existing_instance(source):
    """Return a still-running proxy's URL for this source, if there is one."""
    path = state_path(source)
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None

    try:
        os.kill(state["pid"], 0)
    except OSError:
        os.unlink(path)
        return None

    if not owns_pid(state["pid"]):
        os.unlink(path)
        return None

    probe = socket.socket()
    probe.settimeout(1)
    try:
        probe.connect(("127.0.0.1", state["port"]))
    except OSError:
        os.unlink(path)
        return None
    finally:
        probe.close()

    return state


def write_state(source, port, url):
    fd = os.open(state_path(source), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"pid": os.getpid(), "port": port, "url": url, "source": source}, fh)


def watch_idle(timeout, source):
    """Shut down once nothing has fetched anything for `timeout` seconds.

    The point is that the LAN-facing port closes on its own when the stream stops being
    watched, instead of staying open until someone remembers to kill it.
    """
    while True:
        time.sleep(15)
        idle = time.time() - last_activity
        if idle > timeout:
            say("idle %ds - shutting down" % int(idle))
            try:
                os.unlink(state_path(source))
            except OSError:
                pass
            os._exit(0)


# --------------------------------------------------------------------------- discovery

IFRAME_SRC = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
URL_IN_PAGE = re.compile(r'https?://[^\s"\'`<>\\)]+')
B64_BLOB = re.compile(r'["\']([A-Za-z0-9+/]{24,}={0,2})["\']')
PLAYLISTY = re.compile(r'\.m3u8|\.mpd|playlist|manifest|/hls/|stream', re.I)
NOISE = re.compile(r'youtube|ytimg|google|doubleclick|facebook|twitter|gstatic|'
                   r'jsdelivr|cloudflareinsights|cdnjs|w3\.org|schema\.org', re.I)


STRONG_SIGNAL = re.compile(r'\.m3u8|\.mpd|playlist|manifest|/hls/', re.I)


BLOCK_CODES = frozenset({401, 403, 406, 409, 429})

GateReport = collections.namedtuple("GateReport", "gated status attempts handshake")
GateReport.__new__.__defaults__ = (None,)

Verdict = collections.namedtuple("Verdict", "ok referer detail codes")
Verdict.__new__.__defaults__ = ((),)


def probe_profiles(url, referer=None):
    """Fetch `url` under several header profiles and report each outcome."""
    profiles = [("no headers", {}), ("User-Agent only", {"User-Agent": UA})]
    if referer:
        origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(referer))
        profiles.append(("User-Agent + Referer", {"User-Agent": UA, "Referer": referer}))
        profiles.append(("User-Agent + Referer + Origin",
                         {"User-Agent": UA, "Referer": referer, "Origin": origin}))

    results = []
    for label, headers in profiles:
        try:
            req = urllib.request.Request(url, headers=headers)
            with egress_opener().open(req, timeout=6) as resp:
                results.append((label, resp.status, None))
        except urllib.error.HTTPError as exc:
            results.append((label, exc.code, exc.reason))
            exc.close()
        except Exception as exc:                                  # noqa: BLE001
            results.append((label, None, str(exc)))
    return results


def classify_gate(url, referer=None):
    """Decide whether an origin is refusing this client outright.

    A Referer gate and a client gate both surface as 403, but they part company under
    varied headers: supplying the Referer clears the first and changes nothing for the
    second. When every profile comes back with the same refusal, no header this proxy
    can send will help -- the origin is screening the client itself rather than the
    request, and urllib cannot pass however it is configured.

    Worth naming because the two failures look identical from here. Reporting a client
    gate as "no playlist found" sent people hunting for a missing Referer that was
    never the problem.
    """
    attempts = probe_profiles(url, referer)
    codes = {code for _, code, _ in attempts}
    gated = len(codes) == 1 and next(iter(codes)) in BLOCK_CODES
    return GateReport(gated, next(iter(codes)) if gated else None, attempts)


def clear_gate(url, referer=None):
    """Classify a refusal and, if it is a client gate, try once more as a browser.

    The report's `handshake` says how that went. "passed" means the browser's handshake
    got through, and every fetch from here on presents it. "refused" means the origin
    turned that down as well, and the attempt is listed with the others. "unavailable"
    means curl_cffi is not installed, so there was nothing to try. It stays None when
    the origin was not gated at all -- a Referer problem, or an expired URL.
    """
    report = classify_gate(url, referer)
    if not report.gated:
        return report
    if not handshake_available():
        return report._replace(handshake="unavailable")
    if handshake() == "browser":                  # the refusal came to a browser already
        return report._replace(handshake="refused")

    use_browser_handshake()
    label = "browser handshake (curl_cffi)"
    try:
        with open_media(url, referer, timeout=10):
            pass
    except urllib.error.HTTPError as exc:
        exc.close()
        use_browser_handshake(False)
        return report._replace(handshake="refused",
                               attempts=report.attempts + [(label, exc.code, exc.reason)])
    except OSError as exc:
        use_browser_handshake(False)
        return report._replace(handshake="refused",
                               attempts=report.attempts + [(label, None, str(exc))])
    return report._replace(handshake="passed")


class Gated(Exception):
    """An origin refused every client this proxy can be. `report` says what was tried."""

    def __init__(self, url, report):
        super().__init__("origin refuses this client: every request returned HTTP %s"
                         % report.status)
        self.url = url
        self.report = report


def fetch_through_gate(url, referer=None, **kw):
    """fetch(), and once more as a browser if a client gate refused the first attempt.

    Only a refusal that classify_gate confirms is retried: a 403 that a Referer would
    have cleared, or an expired URL, is raised exactly as fetch() raises it. A gate
    that cannot be cleared raises Gated instead, carrying the report.
    """
    try:
        return fetch(url, referer=referer, **kw)
    except urllib.error.HTTPError as exc:
        if exc.code not in BLOCK_CODES:
            raise
        gate = clear_gate(url, referer)
        if not gate.gated:
            raise
        if gate.handshake != "passed":
            exc.close()     # it stays chained to the Gated, and its socket need not
            raise Gated(url, gate) from None
    return fetch(url, referer=referer, **kw)


def verify_through_gate(url, referer):
    """verify_playlist(), and once more as a browser if a client gate refused it."""
    check = verify_playlist(url, referer)
    if check.ok or not check.codes or not set(check.codes) <= BLOCK_CODES:
        return check
    gate = clear_gate(url, referer)
    if not gate.gated:
        return check
    if gate.handshake != "passed":
        raise Gated(url, gate)
    return verify_playlist(url, referer)


def discover_through_gate(page_url, referer=None):
    """discover(), and once more as a browser if a client gate stood in the way.

    The page is fetched as discover() fetches it. `referer` is for the gate probe only,
    which wants the Referer the origin would expect -- the page itself, unless the
    caller knows better. Raises Gated when a gate was found and could not be cleared.
    """
    blocked = []
    found = discover(page_url, blocked=blocked)
    if found:
        return found
    for url in blocked:
        gate = clear_gate(url, referer or page_url)
        if not gate.gated:
            continue
        if gate.handshake != "passed":
            raise Gated(url, gate)
        return discover(page_url)
    return None


def explain_gate(report):
    """Why a client gate is where this ends, and what to do instead: a paragraph each.

    report_gate prints these; the web app shows them as the error and its hint.
    """
    why = ("no Referer or User-Agent changes this, so the proxy cannot fetch the stream "
           "to re-serve it. The origin is screening the client itself -- most often the "
           "TLS handshake -- and will serve only a real browser.")
    mirror = ("To watch it on a TV, use screen mirroring: Control Center > Screen "
              "Mirroring > Apple TV, then set the display to Use As Separate Display and "
              "fullscreen the video there.")
    if report.handshake == "refused":
        return (why + " A browser's handshake -- Safari's, presented by curl_cffi -- was "
                "refused as well.",
                "This is not a stream the proxy can help with. " + mirror)
    return (why,
            "With curl_cffi installed (pip install curl_cffi) the proxy presents a real "
            "browser's handshake and tries once more before giving up. Failing that, "
            + mirror[0].lower() + mirror[1:])


def report_gate(report, url, out=None):
    """Print why a client-gated origin is a dead end for this proxy."""
    out = out or sys.stdout
    out.write("origin refuses this client: every request returned HTTP %s.\n\n"
              % report.status)
    out.write("  url: %s\n" % url)
    for label, code, reason in report.attempts:
        out.write("  %-30s %s\n" % (label, code if code else reason))
    why, instead = explain_gate(report)
    out.write("\n%s\n\n%s\n" % (textwrap.fill(why, 78), textwrap.fill(instead, 78)))


def verify_playlist(url, referer):
    """Say whether a URL really serves an HLS playlist, and with which Referer.

    Reads only the opening bytes: a candidate that turns out to be a web page should
    not cost a full download.

    On failure `detail` says what actually happened for each attempt -- a status code,
    a network error, or what arrived in place of a playlist. Reporting only "not a
    playlist" made an expired token, a blocked request and a typo look identical, which
    sends people hunting for the wrong problem.
    """
    attempts, tried, codes = [], [], []

    for ref in (None, referer):
        if ref in tried:
            continue
        tried.append(ref)
        label = "with Referer" if ref else "no Referer"

        try:
            with _urlopen(url, ref, timeout=6) as resp:
                head = resp.read(512)
                status = resp.status
                ctype = (resp.headers.get("Content-Type", "") or "none").split(";")[0]
        except urllib.error.HTTPError as exc:
            attempts.append("%s: HTTP %s %s" % (label, exc.code, exc.reason))
            codes.append(exc.code)
            exc.close()
            continue
        except urllib.error.URLError as exc:
            attempts.append("%s: %s" % (label, exc.reason))
            continue
        except Exception as exc:                                  # noqa: BLE001
            attempts.append("%s: %s" % (label, exc))
            continue

        if head.lstrip()[:7] == b"#EXTM3U":
            return Verdict(True, ref, "")
        attempts.append("%s: HTTP %s, %s, no #EXTM3U" % (label, status, ctype))
        codes.append(status)

    return Verdict(False, None, "; ".join(attempts), tuple(codes))


def rank_candidates(urls, page_url):
    """Strongest-looking candidates first, dropping the page's own navigation links."""
    page_host = urllib.parse.urlsplit(page_url).netloc
    scored = []
    for url in urls:
        strong = bool(STRONG_SIGNAL.search(url))
        # A same-host URL matching only the word "stream" is almost always a nav link.
        if not strong and urllib.parse.urlsplit(url).netloc == page_host:
            continue
        scored.append((0 if strong else 1, url))
    return [url for _, url in sorted(scored, key=lambda pair: pair[0])]


def discover(page_url, depth=2, referer=None, seen=None, blocked=None):
    """Find the playlist behind a page without a browser.

    These pages are server-rendered, so the player config is already in the HTML: either
    a URL outright or a base64 blob. Only reach for Playwright when this comes back
    empty, which means the player is genuinely built at runtime.
    """
    seen = seen if seen is not None else set()
    if depth < 0 or page_url in seen:
        return None
    seen.add(page_url)

    try:
        body, _ = fetch(page_url, referer=referer, timeout=20)
    except urllib.error.HTTPError as exc:
        # A page the origin refuses outright is worth surfacing: it is a different
        # problem from a page that loads and simply has no playlist in it.
        if blocked is not None and exc.code in BLOCK_CODES:
            blocked.append(page_url)
        exc.close()
        return None
    except Exception:
        return None
    html = body.decode("utf-8", "replace")

    candidates = []
    for match in URL_IN_PAGE.finditer(html):
        url = match.group(0).rstrip("\\\"'")
        if PLAYLISTY.search(url) and not NOISE.search(url):
            candidates.append(url)
    for match in B64_BLOB.finditer(html):
        try:
            decoded = base64.b64decode(match.group(1)).decode("utf-8")
        except Exception:
            continue
        if decoded.startswith("http") and not NOISE.search(decoded):
            candidates.append(decoded)

    ordered, deduped = [], set()
    for url in candidates:
        if url not in deduped:
            deduped.add(url)
            ordered.append(url)

    ordered = rank_candidates(ordered, page_url)[:25]
    if ordered:
        # The pool's threads are not this one, and which client we present is held per
        # thread so that one resolve cannot switch another's. It has to be carried in.
        presenting = handshake()

        def check(url):
            with handshake_scope(presenting):
                return verify_playlist(url, page_url)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            checked = list(pool.map(check, ordered))
        for url, check in zip(ordered, checked):
            if check.ok:
                return {"page": page_url, "playlist": url, "referer": check.referer}
            # Same refusal under every Referer means the origin never looked at the
            # request -- worth reporting rather than filing as "not a playlist".
            if (blocked is not None and check.codes
                    and set(check.codes) <= BLOCK_CODES and len(set(check.codes)) == 1):
                blocked.append(url)

    for match in IFRAME_SRC.finditer(html):
        src = urllib.parse.urljoin(page_url, match.group(1))
        if NOISE.search(src):
            continue
        found = discover(src, depth - 1, referer=page_url, seen=seen, blocked=blocked)
        if found:
            return found

    return None


# --------------------------------------------------------------------------- diagnostics

BROWSER_NOTE = "browser (the origin refused Python's TLS handshake; presenting Safari's)"


def probe():
    """Report what the source actually serves, without starting a server."""
    try:
        body, ctype = fetch_through_gate(opts.source, referer=opts.referer)
    except Gated as exc:
        report_gate(exc.report, exc.url)
        raise SystemExit(2) from None
    except urllib.error.HTTPError as exc:
        exc.close()
        raise SystemExit("source returned HTTP %s %s" % (exc.code, exc.reason)) from None
    text = body.decode("utf-8", "replace")
    kind = "master" if is_master(text) else "media"
    print("source      : %s" % opts.source)
    if handshake() == "browser":
        print("handshake   : %s" % BROWSER_NOTE)
    print("content-type: %s" % (ctype or "(none)"))
    print("playlist    : %s" % kind)

    url = opts.source
    if kind == "master":
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                url = urllib.parse.urljoin(opts.source, line)
                break
        print("variant     : %s" % url)
        body, _ = fetch(url, referer=opts.referer)
        text = body.decode("utf-8", "replace")

    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            seg = urllib.parse.urljoin(url, line)
            data, seg_ct = fetch_head(seg, referer=opts.referer)
            sniffed = sniff_mime(data)
            print("segment     : %s" % seg[:100])
            print("  served as : %s" % (seg_ct or "(none)"))
            print("  really is : %s" % sniffed)
            served = (seg_ct or "").split(";")[0].strip().lower()
            if served != sniffed.lower():
                print("  VERDICT   : MIME mismatch -- native playback needs this proxy")
            else:
                print("  VERDICT   : MIME correct -- try the URL in Safari directly first")
            break


def self_test():
    """Run the checks that live beside this script.

    They used to sit in this function as a hand-rolled harness, which could not be
    filtered, collected, or pointed at the resolver and the web app -- so they are
    pytest now, in tests/. This stays as the entry point the skill documents.
    """
    tests = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")
    if not os.path.isdir(tests):
        sys.stderr.write("no tests/ directory beside %s\n" % os.path.basename(__file__))
        return 1
    try:
        import pytest  # noqa: F401
    except ImportError:
        sys.stderr.write("the checks need pytest:\n"
                         "  pip install -r requirements-dev.txt\n")
        return 1
    return subprocess.call([sys.executable, "-m", "pytest", tests, "-q"])


# --------------------------------------------------------------------------- entry

def main():
    global opts
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="upstream .m3u8 URL")
    ap.add_argument("--discover", metavar="PAGE_URL",
                    help="find the playlist behind a page and exit")
    ap.add_argument("--referer", help="Referer header the origin requires")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="first port to try; the next free one above it is used")
    ap.add_argument("--port-last", type=int, default=None,
                    help="last port this stream may take (default: %d above the first); "
                         "set it to the end of a published docker range, so a port "
                         "nothing forwards is never bound" % (PORT_SEARCH_RANGE - 1))
    ap.add_argument("--ip", help="address to advertise (default: this machine's LAN IP)")
    ap.add_argument("--window", type=int, default=12,
                    help="segments to advertise, re-adding ones the origin has dropped "
                         "(0 passes the origin's own window through unchanged)")
    ap.add_argument("--cache-mb", type=int, default=64,
                    help="memory for recently served segments, so the widened window "
                         "survives the origin dropping them (0 disables)")
    ap.add_argument("--idle-timeout", type=int, default=900,
                    help="shut down after this many seconds with no requests (0 disables)")
    ap.add_argument("--probe", action="store_true",
                    help="report the source's MIME types and exit")
    ap.add_argument("--self-test", action="store_true",
                    help="run offline checks of the rewrite and sniff logic")
    ap.add_argument("--no-reuse", dest="reuse", action="store_false",
                    help="start a new proxy even if one is already serving this source")
    ap.add_argument("--no-flatten", dest="flatten", action="store_false",
                    help="proxy the master playlist instead of pinning one variant")
    ap.add_argument("--egress-proxy", metavar="URL",
                    help="fetch upstream through this proxy, so the origin sees its "
                         "address and not ours (same as PWS_EGRESS_PROXY); this network "
                         "is still reached directly")
    ap.add_argument("--browser-handshake", action="store_true",
                    help="present a browser's TLS handshake from the start (needs curl_cffi); "
                         "otherwise it is tried only once the origin has refused Python's")
    opts = ap.parse_args()

    if opts.egress_proxy:
        # Into the environment rather than a global: the same variable is what a
        # container sets, and it is what the browser fallback reads in its turn.
        os.environ["PWS_EGRESS_PROXY"] = opts.egress_proxy

    if opts.self_test:
        raise SystemExit(self_test())

    if opts.browser_handshake:
        if not handshake_available():
            ap.error("--browser-handshake needs curl_cffi: pip install curl_cffi")
        use_browser_handshake()

    if opts.discover:
        try:
            found = discover_through_gate(opts.discover, referer=opts.referer)
        except Gated as exc:
            report_gate(exc.report, exc.url)
            raise SystemExit(2) from None
        if not found:
            print("no playlist found in the page HTML.")
            print("the player is probably built at runtime -- fall back to Playwright.")
            raise SystemExit(1)
        print("page     : %s" % found["page"])
        print("playlist : %s" % found["playlist"])
        print("referer  : %s" % (found["referer"] or "(not required)"))
        if egress_proxy():
            print("egress   : upstream via %s" % egress_label())
        if handshake() == "browser":
            print("handshake: %s" % BROWSER_NOTE)
        return

    if not opts.source:
        ap.error("--source or --discover is required")

    if opts.probe:
        probe()
        return

    if opts.reuse:
        running = existing_instance(opts.source)
        if running:
            sys.stderr.write("already serving this source (pid %d)\n" % running["pid"])
            sys.stderr.write("serving : %s/\n" % running["url"])
            return

    opts.ip = opts.ip or lan_ip()
    # 16 bytes, not 8: this token is the only credential on a proxy that binds every
    # interface, so it is worth being unguessable rather than merely unlikely.
    opts.token = secrets.token_hex(16)
    configure_cache(opts.cache_mb)

    httpd, port = bind_server(opts.port, opts.port_last)
    opts.prefix = "http://{}:{}/{}".format(opts.ip, port, opts.token)

    try:
        resolve_source()
    except Gated as exc:
        report_gate(exc.report, exc.url, sys.stderr)
        raise SystemExit(2) from None
    write_state(opts.source, port, opts.prefix)

    if handshake() == "browser":
        sys.stderr.write("handshake: %s\n" % BROWSER_NOTE)
    if egress_proxy():
        sys.stderr.write("egress  : upstream via %s\n" % egress_label())
    sys.stderr.write("upstream: %s\n" % _resolved)
    sys.stderr.write("serving : %s/\n" % opts.prefix)
    if opts.idle_timeout:
        sys.stderr.write("idle-exit: %ds\n" % opts.idle_timeout)
        threading.Thread(target=watch_idle, args=(opts.idle_timeout, opts.source),
                         daemon=True).start()

    try:
        httpd.serve_forever()
    finally:
        try:
            os.unlink(state_path(opts.source))
        except OSError:
            pass


if __name__ == "__main__":
    main()
