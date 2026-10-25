"""The proxy on the wire, against a fake origin that behaves like real ones."""

import base64
import time

import pytest

from origin import MASTER_PLAIN, SEGMENT, WEBP_SHIM, FakeOrigin

GATE = "https://page.example/"


def first_segment(proxy):
    """Walk master, variant, segment, as a player does."""
    variant = proxy.routes("/live.m3u8", "pl")
    path = variant[-1] if variant else "/live.m3u8"
    return proxy.routes(path, "seg")[0]


# --------------------------------------------------------------------------- referer

def test_a_referer_gated_stream_plays_all_the_way_down(start_proxy):
    """This used to resolve and start, then 403 on every segment."""
    with FakeOrigin(referer=GATE) as origin:
        proxy = start_proxy(origin.url + "/master.m3u8", GATE)
        assert proxy.get("/live.m3u8")[0] == 200
        for route in proxy.routes("/live.m3u8", "pl"):
            assert proxy.get(route)[0] == 200
        status, headers, body = proxy.get(first_segment(proxy))
        assert status == 200
        assert body == SEGMENT

    sent = [ref for path, ref in origin.requests if path.endswith(".ts")]
    assert sent and all(ref == GATE for ref in sent), "segments went out without a Referer"


def test_an_ungated_origin_is_not_sent_a_referer(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        assert proxy.get(first_segment(proxy))[0] == 200
    assert all(ref is None for _, ref in origin.requests)


# --------------------------------------------------------------------------- mime

def test_the_declared_type_is_corrected_from_the_bytes(start_proxy):
    """text/plain on an MPEG-TS segment gives the crossed-out play icon."""
    with FakeOrigin(segment_type="text/plain") as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        _, headers, _ = proxy.get(first_segment(proxy))
        assert headers["Content-Type"] == "video/MP2T"


def test_the_playlist_is_served_as_a_playlist(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        _, headers, _ = proxy.get("/live.m3u8")
        assert headers["Content-Type"] == "application/vnd.apple.mpegurl"


# --------------------------------------------------------------------------- vod

def test_a_finished_programme_is_served_whole_and_with_its_end(start_proxy):
    """Windowing a VOD playlist dropped its ENDLIST, and an Apple TV read it twice and
    never asked for a segment."""
    with FakeOrigin(master=MASTER_PLAIN, playlist_type="VOD", window=30) as origin:
        proxy = start_proxy(origin.url + "/video.m3u8")
        _, _, body = proxy.get("/live.m3u8")
    text = body.decode()
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
    assert "#EXT-X-MEDIA-SEQUENCE:0" in text
    assert text.count("#EXTINF") == 30
    assert "#EXT-X-PROGRAM-DATE-TIME" not in text


# --------------------------------------------------------------------------- shims

def test_media_buried_behind_an_image_header_is_dug_out(start_proxy):
    """One origin serves segments from an image CDN, each behind a 42-byte RIFF/WEBP
    header. Safari on iOS tolerates the header; an Apple TV plays a few seconds and
    then reports the item stopped.
    """
    with FakeOrigin(segment_type="image/webp", shim=WEBP_SHIM) as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        status, headers, body = proxy.get(first_segment(proxy))

    assert status == 200
    assert headers["Content-Type"] == "video/MP2T"
    assert body == SEGMENT, "the shim is still on the front"
    assert headers["Content-Length"] == str(len(SEGMENT))


def test_a_range_on_a_shimmed_segment_counts_from_the_media(start_proxy):
    """Range offsets refer to the media, not the stripped header."""
    with FakeOrigin(segment_type="image/webp", shim=WEBP_SHIM) as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        status, headers, body = proxy.get(first_segment(proxy), {"Range": "bytes=0-187"})

    assert status == 206
    assert body == SEGMENT[:188]
    assert headers["Content-Range"] == "bytes 0-187/%d" % len(SEGMENT)


@pytest.mark.parametrize("layout", ["pixels", "trailer"])
def test_media_carried_inside_a_png_is_unpacked(start_proxy, layout):
    """One origin hosts segments on an image CDN as real PNGs, and its page unpacks
    them in JavaScript. AVFoundation is handed a PNG unless the proxy does the same."""
    with FakeOrigin(segment_type="image/png", image=layout) as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        status, headers, body = proxy.get(first_segment(proxy))

    assert status == 200
    assert headers["Content-Type"] == "video/MP2T"
    assert body == SEGMENT
    assert headers["Content-Length"] == str(len(SEGMENT))


def test_a_range_on_a_png_segment_counts_from_the_media(start_proxy):
    """The first request is ranged, so the proxy has to find out it is an image first."""
    with FakeOrigin(segment_type="image/png", image="pixels") as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        status, headers, body = proxy.get(first_segment(proxy), {"Range": "bytes=188-375"})

    assert status == 206
    assert body == SEGMENT[188:376]
    assert headers["Content-Type"] == "video/MP2T"
    assert headers["Content-Range"] == "bytes 188-375/%d" % len(SEGMENT)


def test_an_unshimmed_segment_is_left_exactly_as_it_was(start_proxy):
    with FakeOrigin(segment_type="text/plain") as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        _, headers, body = proxy.get(first_segment(proxy))

    assert body == SEGMENT
    assert headers["Content-Type"] == "video/MP2T"


# --------------------------------------------------------------------------- ranges

def test_a_range_comes_back_as_a_range(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        segment = first_segment(proxy)
        status, headers, body = proxy.get(segment, {"Range": "bytes=0-99"})
        assert status == 206
        assert headers["Content-Range"] == "bytes 0-99/%d" % len(SEGMENT)
        assert body == SEGMENT[:100]


def test_a_mid_file_range_is_still_named_correctly(start_proxy):
    with FakeOrigin(segment_type="text/plain") as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        _, headers, body = proxy.get(first_segment(proxy), {"Range": "bytes=188-375"})
        assert headers["Content-Type"] == "video/MP2T"
        assert body == SEGMENT[188:376]


def test_ranges_are_advertised(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        _, headers, _ = proxy.get(first_segment(proxy))
        assert headers["Accept-Ranges"] == "bytes"


def test_head_returns_the_headers_and_no_body(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        status, headers, body = proxy.get(first_segment(proxy), method="HEAD")
        assert status == 200
        assert headers["Content-Type"] == "video/MP2T"
        assert body == b""


# --------------------------------------------------------------------------- cache

def test_a_second_client_costs_nothing_upstream(start_proxy):
    """Safari and the Apple TV fetch the same stream independently."""
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        segment = first_segment(proxy)
        proxy.get(segment)
        before = len([p for p, _ in origin.requests if p.endswith(".ts")])
        assert proxy.get(segment)[2] == SEGMENT
        after = len([p for p, _ in origin.requests if p.endswith(".ts")])
    assert after == before, "the second fetch went upstream again"


def test_a_segment_survives_the_origin_dropping_it(start_proxy):
    """The widened window advertises segments the origin no longer publishes."""
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        segment = first_segment(proxy)
        assert proxy.get(segment)[0] == 200
        origin.slide()                                  # that segment now 403s upstream
        status, _, body = proxy.get(segment)
    assert status == 200 and body == SEGMENT


def test_a_segment_refused_once_is_asked_for_again(start_proxy):
    """An Apple TV stops on a 404, so one transient refusal gets a retry."""
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8", None, "--cache-mb", 0)
        segment = first_segment(proxy)
        origin.flaky = dict.fromkeys(["video0.ts", "audio0.ts"], 1)
        before = len([p for p, _ in origin.requests if p.endswith(".ts")])
        status, _, body = proxy.get(segment)
        tries = len([p for p, _ in origin.requests if p.endswith(".ts")]) - before

    assert status == 200 and body == SEGMENT, "one refusal ended the stream"
    assert tries == 2, "the refusal was believed, or asked about more than once"


def test_a_segment_refused_every_time_is_still_an_expiry(start_proxy):
    """The retry is one attempt; an expired presign still reports as expired."""
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8", None, "--cache-mb", 0)
        segment = first_segment(proxy)
        origin.slide()                                  # every name now 403s for good
        assert proxy.get(segment)[0] == 404


def test_an_uncached_expired_segment_still_reports_expiry(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8", None, "--cache-mb", 0)
        segment = first_segment(proxy)
        origin.slide()
        assert proxy.get(segment)[0] == 404


def test_a_range_is_cut_from_the_cached_copy(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        segment = first_segment(proxy)
        proxy.get(segment)
        origin.slide()
        status, headers, body = proxy.get(segment, {"Range": "bytes=10-19"})
    assert status == 206
    assert headers["Content-Range"] == "bytes 10-19/%d" % len(SEGMENT)
    assert body == SEGMENT[10:20]


# --------------------------------------------------------------------------- prefetch

def fetched(origin):
    """Segment names the origin has been asked for, in order."""
    return [path.lstrip("/") for path, _ in origin.requests if path.endswith(".ts")]


def wait_until(check, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_png_segments_are_fetched_before_the_player_asks(start_proxy):
    """Unpacking needs the whole image, which through a VPN took longer than the segment
    plays. An Apple TV that waited on each one stalled."""
    with FakeOrigin(segment_type="image/png", image="pixels", window=6) as origin:
        proxy = start_proxy(origin.url + "/video.m3u8")
        proxy.text("/live.m3u8")                    # learns it is an image stream
        assert wait_until(lambda: fetched(origin) == ["video5.ts"])
        segments = proxy.routes("/live.m3u8", "seg")
        assert wait_until(lambda: len(set(fetched(origin))) == 4)
        assert set(fetched(origin)) == {"video2.ts", "video3.ts", "video4.ts", "video5.ts"}

        status, headers, body = proxy.get(segments[-1])
        assert status == 200
        assert body == SEGMENT
        assert headers["Content-Type"] == "video/MP2T"
        assert fetched(origin).count("video5.ts") == 1, "served from the prefetch"


def test_segments_behind_an_image_header_are_prefetched_too(start_proxy):
    with FakeOrigin(segment_type="image/webp", shim=WEBP_SHIM, window=6) as origin:
        proxy = start_proxy(origin.url + "/video.m3u8")
        proxy.text("/live.m3u8")
        assert wait_until(lambda: fetched(origin) == ["video5.ts"])
        segments = proxy.routes("/live.m3u8", "seg")
        assert wait_until(lambda: len(set(fetched(origin))) == 4)

        status, headers, body = proxy.get(segments[-1])
        assert status == 200
        assert body == SEGMENT, "the header is still on the front"
        assert fetched(origin).count("video5.ts") == 1, "served from the prefetch"


def test_a_segment_listed_before_the_cdn_has_it_is_waited_for(start_proxy):
    """The origin can list a segment that answers 404 for a few seconds. The player's
    own request retries once; the prefetch keeps going, so the player is not kept
    waiting on it or handed the 404."""
    with FakeOrigin(segment_type="image/png", image="pixels", window=6) as origin:
        proxy = start_proxy(origin.url + "/video.m3u8")
        proxy.text("/live.m3u8")
        assert wait_until(lambda: fetched(origin) == ["video5.ts"])
        origin.slide(drop=False)
        origin.flaky["video6.ts"] = 3
        segments = proxy.routes("/live.m3u8", "seg")
        assert wait_until(lambda: fetched(origin).count("video6.ts") == 4)

        status, _, body = proxy.get(segments[-1])
        assert status == 200
        assert body == SEGMENT
        assert fetched(origin).count("video6.ts") == 4, "three refusals, then the copy"


def gap_before(text, name_count):
    """Whether #EXT-X-GAP precedes the segment line at this position (0-based)."""
    lines = [line for line in text.splitlines() if line.strip()]
    uris = [i for i, line in enumerate(lines) if not line.startswith("#")]
    return lines[uris[name_count] - 1] == "#EXT-X-GAP"


def test_a_segment_that_never_arrives_is_tagged_as_a_gap(start_proxy):
    """One origin lists segments that 404 for good, and an Apple TV that asks for one
    ends the item. Tagged, the player skips it."""
    with FakeOrigin(segment_type="image/png", image="pixels", window=6) as origin:
        origin.gone.add("video4.ts")
        proxy = start_proxy(origin.url + "/video.m3u8")
        proxy.text("/live.m3u8")
        assert wait_until(lambda: fetched(origin) == ["video5.ts"])
        proxy.text("/live.m3u8")
        # Five more refusals, 1.5 seconds apart, before the prefetch gives up on it.
        assert wait_until(lambda: fetched(origin).count("video4.ts") == 6, timeout=15)
        assert wait_until(lambda: "#EXT-X-GAP" in proxy.text("/live.m3u8"), timeout=5)

        text = proxy.text("/live.m3u8")
        assert text.count("#EXT-X-GAP") == 1
        assert gap_before(text, 4), "the gap is on video4"

        segments = proxy.routes("/live.m3u8", "seg")
        started = time.time()
        status, _, _ = proxy.get(segments[4])
        assert status == 404
        assert time.time() - started < 1, "answered at once, not after more retries"
        assert fetched(origin).count("video4.ts") == 6


def test_a_stream_refusing_everything_is_not_mistaken_for_gaps(start_proxy):
    """Every segment refused, as with an expired presign, is not a stream with holes."""
    with FakeOrigin(segment_type="image/png", image="pixels", window=3) as origin:
        origin.gone.update({"video0.ts", "video1.ts", "video2.ts"})
        proxy = start_proxy(origin.url + "/video.m3u8")
        proxy.text("/live.m3u8")
        assert wait_until(lambda: fetched(origin).count("video2.ts") == 6, timeout=15)
        status, _, _ = proxy.get(proxy.routes("/live.m3u8", "seg")[0])
        assert status == 404
        assert "#EXT-X-GAP" not in proxy.text("/live.m3u8"), "no later segment arrived"


def test_an_ordinary_stream_costs_one_probe_and_no_more(start_proxy):
    with FakeOrigin(segment_type="text/plain", window=6) as origin:
        proxy = start_proxy(origin.url + "/video.m3u8")
        proxy.text("/live.m3u8")
        assert wait_until(lambda: fetched(origin) == ["video5.ts"])
        for _ in range(3):
            proxy.text("/live.m3u8")
        time.sleep(0.5)
        assert fetched(origin) == ["video5.ts"]


# --------------------------------------------------------------------------- tokens

def test_a_forged_segment_token_is_refused(start_proxy):
    """Otherwise a token holder could point /seg/ at anything on the network."""
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        blob = base64.urlsafe_b64encode(b"http://127.0.0.1:8786/api/streams").decode()
        assert proxy.get("/seg/" + blob.rstrip("="))[0] == 404
        assert proxy.get("/pl/" + blob.rstrip("="))[0] == 404


def test_a_segment_token_cannot_be_replayed_against_pl(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        segment = first_segment(proxy)
        assert proxy.get("/pl/" + segment[len("/seg/"):])[0] == 404


def test_the_path_token_is_still_required(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        root = proxy.base.rsplit("/", 1)[0]
        from conftest import http
        assert http(root + "/live.m3u8")[0] == 404


# --------------------------------------------------------------------------- flattening

def test_a_master_with_separate_audio_is_served_whole(start_proxy):
    """Pinning a variant would drop the audio rendition and play silent."""
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        assert "TYPE=AUDIO" in proxy.text("/live.m3u8")
        assert len(proxy.routes("/live.m3u8", "pl")) == 2


def test_a_plain_master_is_flattened_to_one_variant(start_proxy):
    with FakeOrigin(master=MASTER_PLAIN) as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        assert proxy.routes("/live.m3u8", "seg"), "expected a media playlist, not a master"


# --------------------------------------------------------------------------- the page

def test_the_player_page_points_at_the_host_that_asked(start_proxy):
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        assert proxy.base + "/live.m3u8" in proxy.text("/")
