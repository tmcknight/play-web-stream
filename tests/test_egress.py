"""Where our upstream fetches leave from, and what stays on this network.

The point of the feature is an address: with a proxy set, a stream origin sees the
proxy and not this house. So the tests that matter are the ones that watch a request
arrive somewhere -- at the proxy for an origin, at the origin itself for the LAN --
rather than ones that read the configuration back.
"""

import http.server
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

import browser_find
import hls_proxy
import resolve as resolver
from origin import FakeOrigin, free_port

EXIT_IP = "203.0.113.7"          # what the proxy says our address is, when asked


class RecordingProxy:
    """An HTTP proxy that forwards absolute-URI GETs to one origin, and says who asked.

    Enough of a proxy for urllib to talk to, and no more: the destination host never
    resolves, which is itself the assertion -- a request that reached here was one the
    client refused to make for itself.
    """

    def __init__(self, upstream):
        self.upstream = upstream        # http://127.0.0.1:port of the fake origin
        self.seen = []                  # every absolute URI asked of us, in order
        self._server = None
        self._thread = None

    def start(self):
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                proxy.seen.append(self.path)
                parts = urllib.parse.urlsplit(self.path)
                if not parts.scheme:                  # a relative path is not proxying
                    self.send_error(400, "absolute URI expected")
                    return
                if parts.path == "/echo-ip":          # stands in for the probe service
                    self._reply(200, EXIT_IP.encode(), "text/plain")
                    return
                target = proxy.upstream + parts.path + (
                    "?" + parts.query if parts.query else "")
                try:
                    with urllib.request.urlopen(target, timeout=10) as resp:
                        body, status = resp.read(), resp.status
                        ctype = resp.headers.get("Content-Type", "text/plain")
                except urllib.error.HTTPError as exc:
                    with exc:
                        body, status, ctype = exc.read(), exc.code, "text/plain"
                self._reply(status, body, ctype)

            def _reply(self, status, body, ctype):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        port = free_port()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = "http://127.0.0.1:%d" % port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """No test inherits the runner's own proxy settings, or leaves any behind."""
    for name in hls_proxy.PROXY_ENV + ("no_proxy", "NO_PROXY", "PWS_FORCE_PROXY",
                                       "PWS_EGRESS_PROBE_URL"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------------ the setting

def test_the_explicit_variable_wins_over_the_conventional_ones(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://conventional:3128")
    monkeypatch.setenv("PWS_EGRESS_PROXY", "http://ours:8888")
    assert hls_proxy.egress_proxy() == "http://ours:8888"


def test_the_conventional_variables_are_still_honoured(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://conventional:3128")
    assert hls_proxy.egress_proxy() == "http://conventional:3128"


def test_a_bare_host_and_port_is_read_as_http(monkeypatch):
    monkeypatch.setenv("PWS_EGRESS_PROXY", "vpn.lan:8888")
    assert hls_proxy.egress_proxy() == "http://vpn.lan:8888"


def test_nothing_set_is_no_proxy():
    assert hls_proxy.egress_proxy() == ""
    assert hls_proxy.egress_label() == ""


def test_the_label_keeps_the_credentials_out_of_the_log(monkeypatch):
    monkeypatch.setenv("PWS_EGRESS_PROXY", "http://user:hunter2@vpn.lan:8888")
    label = hls_proxy.egress_label()
    assert "hunter2" not in label and "user" not in label
    assert label == "http://vpn.lan:8888 (authenticated)"


# ------------------------------------------------------------------------- the split

@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:8786", "localhost", "10.1.2.3", "192.168.1.50",
    "172.20.0.2", "169.254.10.1", "[::1]", "[fe80::1]:8786", "appletv.local",
    "box.home.arpa",
])
def test_this_network_is_reached_directly(host):
    assert hls_proxy.egress_bypass(host)


@pytest.mark.parametrize("host", [
    "cdn.example.com", "1.2.3.4", "8.8.8.8:443", "[2606:4700::1111]",
])
def test_everything_else_goes_through_the_proxy(host):
    assert not hls_proxy.egress_bypass(host)


def test_no_proxy_adds_hosts_to_the_direct_side(monkeypatch):
    monkeypatch.setenv("no_proxy", ".mirror.example, other.example")
    assert hls_proxy.egress_bypass("cache.mirror.example")
    assert hls_proxy.egress_bypass("other.example")
    assert not hls_proxy.egress_bypass("cdn.example.com")


# -------------------------------------------------------------------- what urllib does

def test_an_origin_is_fetched_through_the_proxy(monkeypatch):
    """The host never resolves, so arriving at all proves the proxy carried it."""
    with FakeOrigin() as origin, RecordingProxy(origin.url) as proxy:
        monkeypatch.setenv("PWS_EGRESS_PROXY", proxy.url)
        body = hls_proxy.fetch("http://origin.invalid/video.m3u8")[0]
    assert b"#EXTM3U" in body
    assert proxy.seen == ["http://origin.invalid/video.m3u8"]


def test_the_lan_is_fetched_directly_even_with_a_proxy_set(monkeypatch):
    """A proxy on a tunnel cannot route back to 127.0.0.1, and should never be asked to."""
    with FakeOrigin() as origin, RecordingProxy(origin.url) as proxy:
        monkeypatch.setenv("PWS_EGRESS_PROXY", proxy.url)
        body = hls_proxy.fetch(origin.url + "/video.m3u8")[0]
    assert b"#EXTM3U" in body
    assert proxy.seen == []


def test_the_header_probe_goes_through_the_proxy_too(monkeypatch):
    """probe_profiles ran on urlopen's own opener once, which was a hole in this."""
    with FakeOrigin() as origin, RecordingProxy(origin.url) as proxy:
        monkeypatch.setenv("PWS_EGRESS_PROXY", proxy.url)
        results = hls_proxy.probe_profiles("http://origin.invalid/video.m3u8")
    assert [status for _, status, _ in results] == [200, 200]
    assert len(proxy.seen) == 2


def test_no_proxy_wins_over_a_proxy_that_is_set(monkeypatch):
    """A star in no_proxy turns the whole thing off without unsetting anything."""
    monkeypatch.setenv("PWS_EGRESS_PROXY", "http://127.0.0.1:%d" % free_port())
    monkeypatch.setenv("no_proxy", "*")
    with FakeOrigin() as origin:
        assert b"#EXTM3U" in hls_proxy.fetch("http://" + origin.url.split("//")[1]
                                             + "/video.m3u8")[0]


@pytest.mark.skipif(not hls_proxy.handshake_available(), reason="curl_cffi is not installed")
def test_the_browser_handshake_leaves_through_the_proxy_too(monkeypatch):
    """The second client in this process has to agree with the first about the door."""
    monkeypatch.setattr(hls_proxy, "_browser", None)
    monkeypatch.setattr(hls_proxy, "_browser_for", None)
    with FakeOrigin() as origin, RecordingProxy(origin.url) as proxy:
        monkeypatch.setenv("PWS_EGRESS_PROXY", proxy.url)
        with hls_proxy.handshake_scope("browser"):
            body = hls_proxy.fetch("http://origin.invalid/video.m3u8")[0]
    assert b"#EXTM3U" in body
    assert proxy.seen == ["http://origin.invalid/video.m3u8"]


# ------------------------------------------------------------------------- the browser

def test_the_browser_is_handed_the_same_proxy(monkeypatch):
    monkeypatch.setenv("PWS_EGRESS_PROXY", "http://user:hunter2@vpn.lan:8888")
    spec = hls_proxy.egress_playwright()
    assert spec["server"] == "http://vpn.lan:8888"      # chromium takes no credentials
    assert spec["username"] == "user" and spec["password"] == "hunter2"
    assert "192.168.0.0/16" in spec["bypass"] and "localhost" in spec["bypass"]


def test_the_browser_is_handed_nothing_when_there_is_no_proxy():
    assert hls_proxy.egress_playwright() is None


def test_the_browser_fallback_launches_with_it(monkeypatch):
    """Without this the one step that loads the origin's page would leave from here."""
    launched = {}

    class FakeChromium:
        def launch(self, **kw):
            launched.update(kw)
            raise RuntimeError("far enough")

    class FakeDriver:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import sys
    module = type("m", (), {"sync_playwright": lambda: FakeDriver()})
    monkeypatch.setitem(sys.modules, "playwright", type("p", (), {}))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setenv("PWS_EGRESS_PROXY", "http://vpn.lan:8888")
    with pytest.raises(RuntimeError, match="far enough"):
        browser_find.find_playlist("http://page.invalid/")
    assert launched["proxy"]["server"] == "http://vpn.lan:8888"


# --------------------------------------------------------------------- serving it here

def test_forcing_the_proxy_is_on_once_an_egress_proxy_is_set(monkeypatch):
    assert hls_proxy.force_proxy() is False
    monkeypatch.setenv("PWS_EGRESS_PROXY", "http://vpn.lan:8888")
    assert hls_proxy.force_proxy() is True


def test_forcing_the_proxy_can_be_turned_off_deliberately(monkeypatch):
    monkeypatch.setenv("PWS_EGRESS_PROXY", "http://vpn.lan:8888")
    monkeypatch.setenv("PWS_FORCE_PROXY", "0")
    assert hls_proxy.force_proxy() is False


def _stub_resolver(monkeypatch, referer=None, match=True):
    monkeypatch.setattr(resolver, "find_playlist",
                        lambda url, allow_browser, say: {"page": url,
                                                         "playlist": url,
                                                         "referer": referer})
    monkeypatch.setattr(resolver, "probe_mime",
                        lambda playlist, ref: {"match": match,
                                               "served_as": "video/MP2T",
                                               "really_is": "video/MP2T"})


def test_a_clean_stream_is_served_here_when_the_egress_proxy_is_on(monkeypatch):
    """Otherwise the Apple TV fetches the origin itself, from this network's address."""
    _stub_resolver(monkeypatch)
    monkeypatch.setenv("PWS_EGRESS_PROXY", "http://vpn.lan:8888")
    verdict = resolver.resolve("https://origin.example/video.m3u8", allow_browser=False)
    assert verdict["needs_proxy"] is True
    assert "fetched here" in verdict["reason"]


def test_a_clean_stream_is_still_handed_over_directly_without_one(monkeypatch):
    _stub_resolver(monkeypatch)
    verdict = resolver.resolve("https://origin.example/video.m3u8", allow_browser=False)
    assert verdict["needs_proxy"] is False


# ------------------------------------------------------------------------- the report

def test_the_check_says_which_address_origins_see(monkeypatch):
    with RecordingProxy("http://127.0.0.1:1") as proxy:
        monkeypatch.setenv("PWS_EGRESS_PROXY", proxy.url)
        monkeypatch.setenv("PWS_EGRESS_PROBE_URL", "http://ipcheck.invalid/echo-ip")
        report = hls_proxy.egress_check(timeout=10)
    assert report["enabled"] and report["forced"]
    assert report["ip"] == EXIT_IP
    assert not report["error"]


def test_the_check_reports_a_tunnel_that_is_down_rather_than_going_around_it(monkeypatch):
    dead = "http://127.0.0.1:%d" % free_port()
    monkeypatch.setenv("PWS_EGRESS_PROXY", dead)
    monkeypatch.setenv("PWS_EGRESS_PROBE_URL", "http://ipcheck.invalid/echo-ip")
    report = hls_proxy.egress_check(timeout=5)
    assert report["enabled"] and report["error"] and not report["ip"]


def test_the_check_asks_nothing_at_all_when_there_is_no_proxy(monkeypatch):
    def fail(*args, **kw):
        raise AssertionError("asked the probe service for our own address directly")

    monkeypatch.setattr(hls_proxy, "egress_opener", fail)
    report = hls_proxy.egress_check()
    assert report == {"enabled": False, "proxy": "", "forced": False, "ip": "", "error": ""}


def test_a_socket_never_leaves_for_the_probe_when_the_proxy_is_unset():
    """The direct question is the one thing this feature must never ask."""
    assert hls_proxy.egress_check()["ip"] == ""
