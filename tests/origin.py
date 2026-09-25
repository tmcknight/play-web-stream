"""A fake origin with a switch for each awkward behaviour the proxy handles.

Switches: a Referer gate, a client gate, segments served as the wrong type, expiring
presigned URLs, ranges, a sliding window that drops old segments, and a finished (VOD
or EVENT) playlist.
"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TS_PACKET = b"\x47" + b"\x00" * 187
SEGMENT = TS_PACKET * 40                # sniffs as video/MP2T, served as text/plain

# One real origin prefixes every segment with this so an image CDN will carry it: a
# 42-byte RIFF/WEBP header, then the MPEG-TS. Same bytes as seen in the wild, so the
# proxy finds the same offset.
WEBP_SHIM = b"RIFF" + b"\x00" * 4 + b"WEBPVP8L" + b"\x00" * 26

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
                 playlist_type=None):
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
        self.sequence = 0               # bumped to slide the window forward
        self.gone = set()               # segment names the origin has dropped
        self.flaky = {}                 # segment name -> refusals still to hand out
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
            return 200, "text/html; charset=utf-8", self.page().encode(), None
        if self.referer and referer != self.referer:
            return 403, "text/plain", b"forbidden", None
        if name.endswith(".m3u8"):
            return 200, "application/vnd.apple.mpegurl", self.playlist(name).encode(), None
        if name.endswith(".ts"):
            return self.segment(name, rng)
        return 404, "text/plain", b"not found", None

    def page(self):
        """A server-rendered player config for discover() to read."""
        return ('<!doctype html><html><body><div id="player"></div>'
                '<script>var cfg = {"file": "%s/master.m3u8", "type": "hls"};</script>'
                "</body></html>" % self.url)

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
        data = self.shim + SEGMENT
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
