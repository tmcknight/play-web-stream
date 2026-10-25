"""A fake origin with a switch for each awkward behaviour the proxy handles.

Switches: a Referer gate, a client gate, segments served as the wrong type, expiring
presigned URLs, ranges, a sliding window that drops old segments, a finished (VOD or
EVENT) playlist, segments carried inside PNGs, rate limiting, and a player nested in
iframes.
"""

import gzip
import socket
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TS_PACKET = b"\x47" + b"\x00" * 187
SEGMENT = TS_PACKET * 40                # sniffs as video/MP2T, served as text/plain

# One real origin prefixes every segment with this so an image CDN will carry it: a
# 42-byte RIFF/WEBP header, then the MPEG-TS. Same bytes as seen in the wild, so the
# proxy finds the same offset.
WEBP_SHIM = b"RIFF" + b"\x00" * 4 + b"WEBPVP8L" + b"\x00" * 26



def _chunk(kind, body):
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def _filtered(rows, bpp):
    """Filter each row with PNG filter types 0 to 4 in turn, so decoding meets all five."""
    out, prev = [], bytes(len(rows[0]))
    for y, row in enumerate(rows):
        kind = y % 5
        enc = bytearray()
        for i, x in enumerate(row):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if kind == 0:
                guess = 0
            elif kind == 1:
                guess = a
            elif kind == 2:
                guess = b
            elif kind == 3:
                guess = (a + b) >> 1
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                guess = a if pa <= pb and pa <= pc else b if pb <= pc else c
            enc.append((x - guess) & 255)
        out.append(bytes([kind]) + bytes(enc))
        prev = row
    return b"".join(out)


def png_carrying(media, layout="pixels", alpha=False, width=4):
    """A real PNG with `media` inside, in one of the two layouts origins use.

    "pixels": a marker, a length and the gzipped media in the RGB values.
    "trailer": an ordinary image with the media appended after IEND.
    """
    bpp = 4 if alpha else 3
    if layout == "pixels":
        packed = gzip.compress(media)
        rgb = b"TIKTIKPX" + struct.pack(">I", len(packed)) + packed
    else:
        rgb = b"\x00" * 3
    height = -(-len(rgb) // (width * 3))
    rgb = rgb.ljust(width * height * 3, b"\x00")
    pixels = rgb
    if alpha:
        pixels = b"".join(rgb[i:i + 3] + b"\xff" for i in range(0, len(rgb), 3))
    stride = width * bpp
    rows = [pixels[y * stride:(y + 1) * stride] for y in range(height)]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    image = (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
             + _chunk(b"IDAT", zlib.compress(_filtered(rows, bpp))) + _chunk(b"IEND", b""))
    return image + media if layout == "trailer" else image


MASTER_WITH_AUDIO = ("#EXTM3U\n"
                     '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="en",URI="audio.m3u8"\n'
                     '#EXT-X-STREAM-INF:BANDWIDTH=1,AUDIO="a"\nvideo.m3u8\n')
MASTER_PLAIN = ("#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=1\nvideo.m3u8\n")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeOrigin:
    """A tiny HLS origin. Start it, point the proxy at `url`, assert on `requests`."""

    def __init__(self, referer=None, segment_type="text/plain", ranges=True,
                 master=MASTER_WITH_AUDIO, window=2, client_gate=None, shim=b"",
                 playlist_type=None, page_query=None, page_iframe=None, image=None,
                 nesting=0, relative_playlist=False):
        self.referer = referer          # None serves anyone; a string gates on it
        # Stands in for a TLS fingerprint check. Plain HTTP has no handshake to screen,
        # so the origin requires a header only the impersonating client sends.
        self.client_gate = client_gate
        self.segment_type = segment_type
        self.shim = shim                # bytes glued in front of the media, if any
        self.ranges = ranges
        self.master = master
        self.window = window            # segments advertised per media playlist
        # "VOD" or "EVENT": keeps every segment. VOD also ends with #EXT-X-ENDLIST, like
        # Apple's bipbop example.
        self.playlist_type = playlist_type
        # A query on the page's playlist URL, written the way players embed it in a
        # script: "&" as "&".
        self.page_query = page_query
        # An iframe the page embeds in place of its own player config.
        self.page_iframe = page_iframe
        # "pixels" or "trailer": each segment arrives as a PNG with the media inside.
        self.image = image
        # Levels of iframe between page.html and the player config.
        self.nesting = nesting
        # The player config names the playlist by a path relative to the page.
        self.relative_playlist = relative_playlist
        self.sequence = 0               # bumped to slide the window forward
        self.gone = set()               # segment names the origin has dropped
        self.flaky = {}                 # segment name -> refusals still to hand out
        self.limited = {}               # segment name -> 429s still to hand out
        self.requests = []              # (path, Referer) in arrival order
        self.received = []              # every request's headers, in the same order
        self._server = None
        self._thread = None

    # -- lifecycle

    def start(self):
        origin = self
        port = free_port()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self, body=True):
                origin.requests.append((self.path, self.headers.get("Referer")))
                origin.received.append(dict(self.headers.items()))
                status, ctype, data, extra = origin.answer(self.path, self.headers)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if body:
                    self.wfile.write(data)

            def do_HEAD(self):
                self.do_GET(body=False)

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = "http://127.0.0.1:%d" % port
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- responses

    def answer(self, path, headers):
        name = path.split("?")[0].lstrip("/")
        referer, rng = headers.get("Referer"), headers.get("Range")
        # A client gate refuses page and media alike, whatever the Referer, which is
        # why it can be mistaken for a missing Referer.
        if self.client_gate and self.client_gate not in headers:
            return 403, "text/plain", b"forbidden", None
        # Open page, gated media: the case where the Referer has to be discovered.
        if name.endswith(".html"):
            return 200, "text/html; charset=utf-8", self.page(name).encode(), None
        if self.referer and referer != self.referer:
            return 403, "text/plain", b"forbidden", None
        if name.endswith(".m3u8"):
            return 200, "application/vnd.apple.mpegurl", self.playlist(name).encode(), None
        if name.endswith(".ts"):
            return self.segment(name, rng)
        return 404, "text/plain", b"not found", None

    def page(self, name="page.html"):
        """A server-rendered player config for discover() to read."""
        iframe = '<!doctype html><html><body><iframe src="%s"></iframe></body></html>'
        if self.page_iframe:
            return iframe % self.page_iframe
        level = int(name[5:-5]) if name.startswith("frame") else 0
        if level < self.nesting:
            return iframe % ("/frame%d.html" % (level + 1))
        query = "?" + self.page_query.replace("&", "\\u0026") if self.page_query else ""
        base = "" if self.relative_playlist else self.url
        return ('<!doctype html><html><body><div id="player"></div>'
                '<script>var cfg = {"file": "%s/master.m3u8%s", "type": "hls"};</script>'
                "</body></html>" % (base, query))

    def playlist(self, name):
        if name == "master.m3u8":
            return self.master
        stem = name[:-5]
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:4",
                 "#EXT-X-MEDIA-SEQUENCE:%d" % self.sequence]
        if self.playlist_type:
            lines.append("#EXT-X-PLAYLIST-TYPE:%s" % self.playlist_type)
        for index in range(self.sequence, self.sequence + self.window):
            lines += ["#EXTINF:4.0,", "%s%d.ts" % (stem, index)]
        if self.playlist_type == "VOD":
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def segment(self, name, rng):
        if name in self.gone:
            return 403, "text/plain", b"expired", None
        # A CDN under load refuses a request it would serve a moment later, with the
        # same codes as an expired presign. Only a retry tells them apart.
        if self.flaky.get(name):
            self.flaky[name] -= 1
            return 404, "text/plain", b"not right now", None
        if self.limited.get(name):
            self.limited[name] -= 1
            return 429, "text/plain", b"slow down", None
        data = png_carrying(SEGMENT, self.image) if self.image else self.shim + SEGMENT
        if self.ranges and rng and rng.startswith("bytes="):
            low, _, high = rng[6:].partition("-")
            low = int(low or 0)
            high = int(high) if high else len(data) - 1
            high = min(high, len(data) - 1)
            return (206, self.segment_type, data[low:high + 1],
                    {"Content-Range": "bytes %d-%d/%d" % (low, high, len(data))})
        return 200, self.segment_type, data, None

    def slide(self, drop=True):
        """Advance the live window and, if `drop`, expire the segment that fell off."""
        if drop:
            for stem in ("video", "audio"):
                self.gone.add("%s%d.ts" % (stem, self.sequence))
        self.sequence += 1
