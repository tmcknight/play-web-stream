"""Getting past an origin that screens the client itself, with and without curl_cffi.

Most of these stand a fake browser in for curl_cffi, so the retry logic is checked on
every interpreter CI runs, whether or not the extra is installed there. The ones that
need the real thing say so and skip without it.
"""

import io
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

import conftest
import hls_proxy
import resolve as resolver
from origin import SEGMENT, FakeOrigin

GATE = "https://page.example/"
MARK = "X-Browser-Handshake"        # what the fake browser sends and the fake gate demands
BROWSER = hls_proxy.handshake_available()


@pytest.fixture(autouse=True)
def python_handshake():
    """Every test starts and ends presenting Python's handshake, whatever it did between."""
    hls_proxy.use_browser_handshake(False)
    yield
    hls_proxy.use_browser_handshake(False)


@pytest.fixture
def fake_browser(monkeypatch):
    """A stand-in for curl_cffi: urllib wearing the marker the fake gate looks for."""
    calls = []

    def browser_open(url, headers, timeout):
        calls.append(url)
        headers = dict(headers, **{MARK: "1"})
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                      timeout=timeout)

    monkeypatch.setattr(hls_proxy, "handshake_available", lambda: True)
    monkeypatch.setattr(hls_proxy, "_browser_open", browser_open)
    return calls


@pytest.fixture
def no_browser(monkeypatch):
    monkeypatch.setattr(hls_proxy, "handshake_available", lambda: False)


# ----------------------------------------------------------------------------- scope

def test_a_switch_outside_a_scope_is_process_wide(fake_browser):
    """One proxy serves one source, so the switch it makes belongs to the process."""
    hls_proxy.use_browser_handshake()
    assert hls_proxy.handshake() == "browser"

    done = {}
    thread = threading.Thread(target=lambda: done.update(saw=hls_proxy.handshake()))
    thread.start()
    thread.join(5)
    assert done["saw"] == "browser", "another thread presents it too"


def test_a_switch_inside_a_scope_does_not_outlive_it(fake_browser):
    with hls_proxy.handshake_scope():
        hls_proxy.use_browser_handshake()
        assert hls_proxy.handshake() == "browser"
    assert hls_proxy.handshake() == "python"


def test_a_scope_starts_from_the_process_default(fake_browser):
    """A proxy started with --browser-handshake still presents one inside a scope."""
    hls_proxy.use_browser_handshake()
    with hls_proxy.handshake_scope():
        assert hls_proxy.handshake() == "browser"


def test_a_nested_scope_puts_back_what_it_found(fake_browser):
    with hls_proxy.handshake_scope():
        hls_proxy.use_browser_handshake()
        with hls_proxy.handshake_scope():
            hls_proxy.use_browser_handshake(False)
        assert hls_proxy.handshake() == "browser", "the inner block did not strand it"


def test_one_resolve_does_not_drag_another_along(fake_browser):
    """The bug: the web app resolves several sources at once, in threads of its own.

    A gated origin switched the whole process to a browser's handshake, so an unrelated
    resolve presented one too -- and whichever finished first put the switch back while
    the other was still fetching with it. Intermittent, and invisible on a quiet box.
    """
    gated_switched = threading.Event()
    other_finished = threading.Event()
    seen = {}

    def gated():
        with hls_proxy.handshake_scope():
            hls_proxy.use_browser_handshake()
            seen["gated"] = hls_proxy.handshake()
            gated_switched.set()
            other_finished.wait(5)
            seen["gated_after"] = hls_proxy.handshake()

    def other():
        gated_switched.wait(5)
        with hls_proxy.handshake_scope():
            seen["other"] = hls_proxy.handshake()
        other_finished.set()

    threads = [threading.Thread(target=gated), threading.Thread(target=other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert seen == {"gated": "browser",
                    "other": "python",          # not dragged into the other's switch
                    "gated_after": "browser"}   # nor reset by the other finishing
    assert hls_proxy.handshake() == "python", "and the process default is untouched"


def test_discover_carries_the_scope_into_its_pool(fake_browser):
    """Candidates are verified in a thread pool, which the per-thread scope misses.

    Without the value being carried in, the workers fall back to the process default,
    every candidate is refused, and a gated page resolves as "no playlist found".
    """
    with FakeOrigin(client_gate=MARK) as origin, hls_proxy.handshake_scope():
        hls_proxy.use_browser_handshake()
        found = hls_proxy.discover(origin.url + "/page.html")
    assert found and found["playlist"].endswith(".m3u8")


# --------------------------------------------------------------------------- clearing

def test_without_curl_cffi_a_client_gate_is_reported_as_before(no_browser):
    with FakeOrigin(client_gate=MARK) as origin:
        gate = hls_proxy.clear_gate(origin.url + "/video.m3u8", None)
    assert gate.gated and gate.status == 403
    assert gate.handshake == "unavailable"
    assert hls_proxy.handshake() == "python"

    out = io.StringIO()
    hls_proxy.report_gate(gate, origin.url + "/video.m3u8", out)
    text = out.getvalue()
    assert "screening the client itself" in text
    assert "curl_cffi" in text, "the way past it is worth naming"


def test_a_browser_handshake_clears_a_client_gate(fake_browser):
    with FakeOrigin(client_gate=MARK) as origin:
        url = origin.url + "/video.m3u8"
        gate = hls_proxy.clear_gate(url, None)
        assert gate.gated and gate.handshake == "passed"
        assert hls_proxy.handshake() == "browser", "every fetch from here on is a browser"
        body, _ = hls_proxy.fetch(url)
    assert body.startswith(b"#EXTM3U")
    assert fake_browser == [url, url]


def test_a_gate_the_browser_cannot_clear_is_reported_with_that_attempt(fake_browser):
    """A Referer gate probed without the Referer refuses the browser too."""
    with FakeOrigin(referer=GATE) as origin:
        gate = hls_proxy.clear_gate(origin.url + "/video.m3u8", None)
    assert gate.gated and gate.handshake == "refused"
    assert hls_proxy.handshake() == "python", "a refused handshake is not kept"
    label, code, _ = gate.attempts[-1]
    assert "browser" in label and code == 403

    out = io.StringIO()
    hls_proxy.report_gate(gate, origin.url + "/video.m3u8", out)
    assert "refused as well" in out.getvalue()
    assert "pip install" not in out.getvalue(), "it is installed; do not suggest it"


def test_a_referer_gate_is_not_mistaken_for_a_client_gate(fake_browser):
    with FakeOrigin(referer=GATE) as origin:
        gate = hls_proxy.clear_gate(origin.url + "/video.m3u8", GATE)
    assert not gate.gated and gate.handshake is None
    assert fake_browser == [], "the Referer cleared it, so no browser was needed"


def test_fetch_through_gate_raises_gated_when_nothing_gets_through(no_browser):
    with FakeOrigin(client_gate=MARK) as origin, pytest.raises(hls_proxy.Gated) as caught:
        hls_proxy.fetch_through_gate(origin.url + "/video.m3u8")
    assert caught.value.report.handshake == "unavailable"
    assert "HTTP 403" in str(caught.value)


def test_fetch_through_gate_leaves_a_404_alone(fake_browser):
    """Only a refusal is worth a second look; a missing file is raised as it always was."""
    with FakeOrigin(referer=GATE) as origin:
        try:
            hls_proxy.fetch_through_gate(origin.url + "/missing.txt", referer=GATE)
        except urllib.error.HTTPError as exc:
            code = exc.code             # consumed here, before the origin goes away
            exc.close()
        else:
            code = None
    assert code == 404
    assert fake_browser == []


def test_discovery_gets_past_a_gated_page(fake_browser):
    with FakeOrigin(client_gate=MARK) as origin:
        found = hls_proxy.discover_through_gate(origin.url + "/page.html")
    assert found and found["playlist"] == origin.url + "/master.m3u8"
    assert hls_proxy.handshake() == "browser"


# --------------------------------------------------------------------------- resolving

def test_the_verdict_carries_the_handshake_and_the_switch_is_undone(fake_browser):
    with FakeOrigin(client_gate=MARK) as origin:
        stages = []
        verdict = resolver.resolve(origin.url + "/page.html", allow_browser=False,
                                   on_progress=stages.append)
    assert verdict["handshake"] == "browser"
    assert verdict["needs_proxy"]
    assert any("TLS handshake" in stage for stage in stages)
    assert hls_proxy.handshake() == "python", "the next URL starts where every one did"


def test_an_ungated_origin_never_meets_the_browser(fake_browser):
    with FakeOrigin() as origin:
        verdict = resolver.resolve(origin.url + "/page.html", allow_browser=False)
    assert verdict["handshake"] == "python"
    assert fake_browser == []


def test_the_app_explains_a_gate_it_cannot_clear(no_browser):
    with FakeOrigin(client_gate=MARK) as origin, pytest.raises(resolver.ResolveError) as caught:
        resolver.resolve(origin.url + "/page.html", allow_browser=False)
    assert "HTTP 403" in caught.value.message
    assert "curl_cffi" in caught.value.hint


def test_a_direct_playlist_url_is_verified_as_a_browser_too(fake_browser):
    with FakeOrigin(client_gate=MARK) as origin:
        verdict = resolver.resolve(origin.url + "/video.m3u8", allow_browser=False)
    assert verdict["method"] == "direct" and verdict["handshake"] == "browser"


# --------------------------------------------------------------------------- the adapter

class StubResponse:
    """The parts of a streamed curl_cffi response BrowserResponse reads."""

    def __init__(self, chunks, headers, status=200):
        self.chunks = chunks
        self.headers = headers
        self.status_code = status
        self.reason = "OK"
        self.closed = False

    def iter_content(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def test_read_honours_the_amount_across_chunk_boundaries():
    resp = hls_proxy.BrowserResponse(StubResponse([b"abc", b"defg", b"h"], {}))
    assert resp.read(2) == b"ab"
    assert resp.read(4) == b"cdef"
    assert resp.read() == b"gh"
    assert resp.read(10) == b""
    assert resp.read() == b""


def test_a_decoded_body_does_not_carry_the_encoded_length():
    """libcurl inflates on the way in; a Content-Length for the gzip would be a lie."""
    plain = hls_proxy.BrowserResponse(StubResponse([b"x"], {"Content-Length": "1",
                                                             "Content-Type": "text/plain"}))
    assert plain.headers.get("Content-Length") == "1"
    inflated = hls_proxy.BrowserResponse(StubResponse([b"x" * 40], {
        "Content-Encoding": "gzip", "Content-Length": "20", "content-type": "text/plain"}))
    assert inflated.headers.get("Content-Length") is None
    assert inflated.headers.get("Content-Type") == "text/plain", "lookups are case-blind"
    assert inflated.read() == b"x" * 40


def test_closing_closes_the_stream():
    stub = StubResponse([b"x"], {})
    with hls_proxy.BrowserResponse(stub):
        pass
    assert stub.closed


# --------------------------------------------------------------------------- the CLI

def run_without_curl_cffi(*args):
    """Run hls_proxy as a machine with the extra missing would, whatever this one has."""
    code = ("import runpy, sys; sys.modules['curl_cffi'] = None; "
            "sys.argv = ['hls_proxy.py'] + sys.argv[1:]; "
            "runpy.run_path('hls_proxy.py', run_name='__main__')")
    return subprocess.run([sys.executable, "-c", code, *args], cwd=str(conftest.ROOT),
                          capture_output=True, text=True, timeout=60)


def test_probe_reports_a_gate_and_exits_2_without_the_extra():
    with FakeOrigin(client_gate=MARK) as origin:
        done = run_without_curl_cffi("--source", origin.url + "/video.m3u8", "--probe")
    assert done.returncode == 2, done.stdout + done.stderr
    assert "every request returned HTTP 403" in done.stdout
    assert "curl_cffi" in done.stdout


def test_discover_reports_a_gate_and_exits_2_without_the_extra():
    with FakeOrigin(client_gate=MARK) as origin:
        done = run_without_curl_cffi("--discover", origin.url + "/page.html")
    assert done.returncode == 2, done.stdout + done.stderr
    assert "screening the client itself" in done.stdout


def test_the_proxy_reports_a_gate_and_exits_2_without_the_extra():
    with FakeOrigin(client_gate=MARK) as origin:
        done = run_without_curl_cffi("--source", origin.url + "/master.m3u8",
                                     "--idle-timeout", "0", "--no-reuse")
    assert done.returncode == 2, done.stdout + done.stderr
    assert "every request returned HTTP 403" in done.stderr
    assert "serving :" not in done.stderr


def test_the_flag_is_refused_without_the_extra():
    done = run_without_curl_cffi("--source", "http://127.0.0.1:1/x", "--browser-handshake")
    assert done.returncode == 2
    assert "curl_cffi" in done.stderr


# --------------------------------------------------------------------------- with curl_cffi

# Over plain HTTP there is no handshake to screen, so the fake origin demands a header
# only a browser sends: Python's urllib has no Accept-Language, every browser does.
needs_curl_cffi = pytest.mark.skipif(not BROWSER, reason="curl_cffi is not installed")
LANGUAGE = "Accept-Language"


@needs_curl_cffi
def test_curl_cffi_gets_through_and_streams_the_segments():
    with FakeOrigin(client_gate=LANGUAGE) as origin:
        gate = hls_proxy.clear_gate(origin.url + "/video.m3u8", None)
        assert gate.handshake == "passed"
        body, ctype = hls_proxy.fetch(origin.url + "/video.m3u8")
        assert body.startswith(b"#EXTM3U") and "mpegurl" in ctype
        head, served = hls_proxy.fetch_head(origin.url + "/video0.ts")
        assert head == SEGMENT and served == "text/plain"
        with hls_proxy.open_media(origin.url + "/video0.ts",
                                  extra={"Range": "bytes=10-19"}) as resp:
            assert resp.status == 206
            assert resp.headers.get("Content-Range") == "bytes 10-19/%d" % len(SEGMENT)
            assert resp.read() == SEGMENT[10:20]


@needs_curl_cffi
def test_curl_cffi_sends_the_user_agent_its_handshake_goes_with():
    """The two must agree, so the UA is left to curl_cffi -- and it is ours already."""
    with FakeOrigin() as origin:
        hls_proxy.use_browser_handshake()
        hls_proxy.fetch(origin.url + "/video.m3u8", referer=GATE)
    sent = {name.lower(): value for name, value in origin.received[-1].items()}
    assert sent["user-agent"] == hls_proxy.UA
    assert sent["referer"] == GATE and sent["origin"] == GATE.rstrip("/")
    assert "accept-language" in sent, "a browser's headers came with the handshake"


@needs_curl_cffi
def test_a_connection_failure_is_a_urlerror_like_urllib_gives():
    hls_proxy.use_browser_handshake()
    with pytest.raises(urllib.error.URLError):
        hls_proxy.fetch("http://127.0.0.1:1/x", timeout=3)


@needs_curl_cffi
def test_the_proxy_finds_its_own_way_past_a_client_gate(start_proxy):
    """The acceptance case: gated under every header profile, served all the same."""
    with FakeOrigin(client_gate=LANGUAGE) as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        assert "handshake: browser" in proxy.log.read_text()
        variant = proxy.routes("/live.m3u8", "pl")
        path = variant[-1] if variant else "/live.m3u8"
        segment = proxy.routes(path, "seg")[0]
        status, headers, body = proxy.get(segment)
        assert status == 200 and body == SEGMENT
        assert headers["Content-Type"] == "video/MP2T"
        status, headers, body = proxy.get(segment, {"Range": "bytes=0-99"})
        assert status == 206 and body == SEGMENT[:100]


@needs_curl_cffi
def test_the_proxy_can_be_told_to_start_as_a_browser(start_proxy):
    with FakeOrigin(client_gate=LANGUAGE) as origin:
        proxy = start_proxy(origin.url + "/master.m3u8", None, "--browser-handshake")
        assert proxy.get("/live.m3u8")[0] == 200
    refused = [path for path, _ in origin.requests if path.endswith(".m3u8")]
    assert refused, "the playlist was fetched"
