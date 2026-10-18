"""The resolver: finding the playlist, and deciding whether the proxy is needed."""

import sys

import pytest

import hls_proxy
import resolve as resolver
from origin import WEBP_SHIM, FakeOrigin

GATE = "https://page.example/"


# --------------------------------------------------------------------------- refusals

@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=1",
    "https://netflix.com/title/1",
    "https://www.disneyplus.com/video/1",
    "https://tv.apple.com/show/1",
])
def test_drm_services_are_refused_with_a_reason(url):
    with pytest.raises(resolver.ResolveError) as caught:
        resolver.find_playlist(url, allow_browser=False)
    assert "DRM" in caught.value.message
    assert caught.value.hint


def test_a_lookalike_host_is_not_treated_as_drm():
    with pytest.raises(resolver.ResolveError) as caught:
        resolver.find_playlist("https://netflix.com.example.org/x", allow_browser=False)
    assert "DRM" not in caught.value.message


def test_dash_is_refused():
    with pytest.raises(resolver.ResolveError) as caught:
        resolver.find_playlist("https://o.x/a/manifest.mpd", allow_browser=False)
    assert "DASH" in caught.value.message


def test_a_page_with_no_playlist_says_so():
    with pytest.raises(resolver.ResolveError) as caught:
        resolver.find_playlist("http://127.0.0.1:1/nothing", allow_browser=False)
    assert caught.value.hint


def test_a_missing_playwright_is_reported_not_raised(monkeypatch):
    """A None entry in sys.modules makes the import fail, as on a machine without it."""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(resolver.ResolveError) as caught:
        resolver.find_playlist("http://127.0.0.1:1/nothing")
    assert sys.executable in caught.value.message
    assert "Docker" in caught.value.hint


# --------------------------------------------------------------------------- verifying

def test_verify_playlist_accepts_a_real_one():
    with FakeOrigin() as origin:
        check = hls_proxy.verify_playlist(origin.url + "/video.m3u8", None)
    assert check.ok and check.referer is None


def test_verify_playlist_finds_the_referer_the_origin_wants():
    with FakeOrigin(referer=GATE) as origin:
        check = hls_proxy.verify_playlist(origin.url + "/video.m3u8", GATE)
    assert check.ok and check.referer == GATE


def test_verify_playlist_reports_what_happened_per_attempt():
    """A 403, a 404 and a non-playlist page must be reported differently."""
    with FakeOrigin(referer=GATE) as origin:
        check = hls_proxy.verify_playlist(origin.url + "/video.m3u8", None)
    assert not check.ok
    assert "403" in check.detail
    assert check.codes == (403,)


def test_a_page_is_not_a_playlist():
    with FakeOrigin() as origin:
        check = hls_proxy.verify_playlist(origin.url + "/page.html", None)
    assert not check.ok and "no #EXTM3U" in check.detail


# --------------------------------------------------------------------------- gating

def test_a_client_gate_is_told_apart_from_a_referer_gate():
    with FakeOrigin(referer=GATE) as origin:
        referer_gated = hls_proxy.classify_gate(origin.url + "/video.m3u8", GATE)
        client_gated = hls_proxy.classify_gate(origin.url + "/video.m3u8", None)
    assert not referer_gated.gated, "the Referer cleared it, so it is not a client gate"
    assert client_gated.gated and client_gated.status == 403


# --------------------------------------------------------------------------- verdicts

def test_a_wrong_mime_type_calls_for_the_proxy():
    with FakeOrigin(segment_type="text/plain") as origin:
        verdict = resolver.resolve(origin.url + "/page.html", allow_browser=False)
    assert verdict["needs_proxy"]
    assert verdict["mime"]["served_as"] == "text/plain"
    assert verdict["mime"]["really_is"] == "video/MP2T"
    assert verdict["method"] == "html"


def test_media_behind_an_image_header_is_reported_as_the_media():
    with FakeOrigin(segment_type="image/webp", shim=WEBP_SHIM) as origin:
        verdict = resolver.resolve(origin.url + "/page.html", allow_browser=False)
    assert verdict["needs_proxy"]
    assert verdict["mime"]["served_as"] == "image/webp"
    assert verdict["mime"]["really_is"] == "video/MP2T"


def test_a_correct_mime_type_needs_no_proxy():
    with FakeOrigin(segment_type="video/MP2T") as origin:
        verdict = resolver.resolve(origin.url + "/page.html", allow_browser=False)
    assert not verdict["needs_proxy"]
    assert verdict["mime"]["match"]


def test_a_required_referer_calls_for_the_proxy_on_its_own():
    """Safari will not send one, so the proxy has to fetch the stream."""
    with FakeOrigin(segment_type="video/MP2T") as origin:
        origin.referer = origin.url + "/page.html"
        verdict = resolver.resolve(origin.url + "/page.html", allow_browser=False)
    assert verdict["needs_proxy"]
    assert "Referer" in verdict["reason"]


def test_a_direct_playlist_url_is_taken_as_given():
    with FakeOrigin() as origin:
        verdict = resolver.resolve(origin.url + "/video.m3u8", allow_browser=False)
    assert verdict["method"] == "direct"
