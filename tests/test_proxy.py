"""The proxy on the wire, against an origin that behaves the way real ones do."""

import base64

from origin import MASTER_PLAIN, SEGMENT, WEBP_SHIM, FakeOrigin

GATE = "https://page.example/"


def first_segment(proxy):
    """Walk master -> variant -> segment, the way a player does."""
    variant = proxy.routes("/live.m3u8", "pl")
    path = variant[-1] if variant else "/live.m3u8"
    return proxy.routes(path, "seg")[0]


# --------------------------------------------------------------------------- referer

def test_a_referer_gated_stream_plays_all_the_way_down(start_proxy):
    """The whole chain used to resolve, start, and then 403 on every segment."""
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
    """text/plain on an MPEG-TS segment is the crossed-out play icon."""
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
    """Windowing a VOD playlist kept its tail, dropped its end, and an Apple TV
    read it twice and never asked for a segment."""
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
    """One origin serves its segments from an image CDN, which only accepts images.

    Each is a 42-byte RIFF/WEBP header in front of an ordinary MPEG-TS segment. Safari
    on iOS tolerates being handed the header; an Apple TV plays for a few seconds and
    reports the item stopped, which is the whole bug.
    """
    with FakeOrigin(segment_type="image/webp", shim=WEBP_SHIM) as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        status, headers, body = proxy.get(first_segment(proxy))

    assert status == 200
    assert headers["Content-Type"] == "video/MP2T"
    assert body == SEGMENT, "the shim is still on the front"
    assert headers["Content-Length"] == str(len(SEGMENT))


def test_a_range_on_a_shimmed_segment_counts_from_the_media(start_proxy):
    """The client is addressing the video, which knows nothing of the header."""
    with FakeOrigin(segment_type="image/webp", shim=WEBP_SHIM) as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        status, headers, body = proxy.get(first_segment(proxy), {"Range": "bytes=0-187"})

    assert status == 206
    assert body == SEGMENT[:188]
    assert headers["Content-Range"] == "bytes 0-187/%d" % len(SEGMENT)


def test_an_unshimmed_segment_is_left_exactly_as_it_was(start_proxy):
    """The common case must not pay for the rare one, or lose a byte to it."""
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
    """The point of the widened window: the origin publishes three, we advertise more."""
    with FakeOrigin() as origin:
        proxy = start_proxy(origin.url + "/master.m3u8")
        segment = first_segment(proxy)
        assert proxy.get(segment)[0] == 200
        origin.slide()                                  # that segment now 403s upstream
        status, _, body = proxy.get(segment)
    assert status == 200 and body == SEGMENT


def test_a_segment_refused_once_is_asked_for_again(start_proxy):
    """One blip must not end the stream: an Apple TV does not forgive a 404."""
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
    """The retry buys one chance, it does not paper over a presign that is gone."""
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


# --------------------------------------------------------------------------- tokens

def test_a_forged_segment_token_is_refused(start_proxy):
    """What a token holder could otherwise point at anything on the network."""
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
    """Pinning a variant would drop the audio rendition and play the stream silent."""
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
