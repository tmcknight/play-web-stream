"""Playlist rewriting, the sliding window, and the byte-range and container rules."""

import argparse

import pytest

import hls_proxy
from hls_proxy import (
    accumulate,
    decode_url,
    keeps_everything,
    media_offset,
    parse_range,
    reset_windows,
    rewrite,
    should_flatten,
    sniff_mime,
)

PREFIX = "http://10.0.0.5:8787/tok"
BASE = "https://o.x/a/master.m3u8"


@pytest.fixture(autouse=True)
def window_of_four():
    hls_proxy.opts = argparse.Namespace(window=4, prefix=PREFIX)
    reset_windows()
    yield
    reset_windows()


def routed(text, base=BASE):
    """Every URI the rewrite emits, as (route, target) in playlist order."""
    out = []
    for line in rewrite(text, base, PREFIX).splitlines():
        for match in hls_proxy.re.finditer(
                r"%s/(seg|pl)/([A-Za-z0-9_=.-]+)" % hls_proxy.re.escape(PREFIX), line):
            out.append((match.group(1), decode_url(match.group(2), match.group(1))))
    return out


# --------------------------------------------------------------------------- rewriting

def test_variants_route_to_pl_whether_relative_or_absolute():
    assert routed('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://cdn.x/hi.m3u8\n'
                  '#EXT-X-STREAM-INF:BANDWIDTH=2\nlo/index.m3u8') == [
        ("pl", "https://cdn.x/hi.m3u8"), ("pl", "https://o.x/a/lo/index.m3u8")]


def test_alternate_rendition_routes_to_pl():
    got = routed('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio/en.m3u8"\n'
                 '#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8')
    assert got[0] == ("pl", "https://o.x/a/audio/en.m3u8")


def test_keys_init_sections_and_segments_route_to_seg():
    assert routed('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://k.x/key",IV=0x1\n'
                  '#EXT-X-MAP:URI="init.mp4"\n#EXTINF:5.0,\nseg1.m4s',
                  "https://o.x/a/index.m3u8") == [
        ("seg", "https://k.x/key"),
        ("seg", "https://o.x/a/init.mp4"),
        ("seg", "https://o.x/a/seg1.m4s")]


def test_blank_lines_and_plain_tags_survive():
    out = rewrite("#EXTM3U\n\n#EXT-X-TARGETDURATION:5\n", BASE, PREFIX)
    assert out.splitlines()[:3] == ["#EXTM3U", "", "#EXT-X-TARGETDURATION:5"]


# --------------------------------------------------------------------------- window

POLL = ("#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:%d\n"
        "#EXTINF:5.0,\n%s")
MEDIA = "https://o.x/a/index.m3u8"


def names(text):
    return [line.rsplit("/", 1)[-1] for line in text.splitlines() if line.startswith("http")]


def test_window_outlives_the_origins_own():
    first = ("#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:10\n"
             "#EXTINF:5.0,\na.ts\n#EXTINF:5.0,\nb.ts\n#EXTINF:5.0,\nc.ts")
    second = ("#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:12\n"
              "#EXTINF:5.0,\nc.ts\n#EXTINF:5.0,\nd.ts\n#EXTINF:5.0,\ne.ts")
    accumulate(first, MEDIA)
    merged = accumulate(second, MEDIA)
    assert names(merged) == ["b.ts", "c.ts", "d.ts", "e.ts"]     # origin published 3
    assert "#EXT-X-MEDIA-SEQUENCE:11" in merged
    assert merged.splitlines()[:2] == ["#EXTM3U", "#EXT-X-TARGETDURATION:5"]


def test_each_playlist_keeps_its_own_window():
    """Video and audio renditions both come through /pl/ and must not merge."""
    video, audio = "https://o.x/a/v.m3u8", "https://o.x/a/en.m3u8"
    accumulate(POLL % (10, "v1.ts"), video)
    assert names(accumulate(POLL % (10, "a1.aac"), audio)) == ["a1.aac"]
    assert names(accumulate(POLL % (11, "v2.ts"), video)) == ["v1.ts", "v2.ts"]


def test_windows_are_capped():
    for index in range(hls_proxy.WINDOW_PLAYLISTS + 3):
        accumulate(POLL % (0, "s.ts"), "https://o.x/a/%d.m3u8" % index)
    assert len(hls_proxy._windows) == hls_proxy.WINDOW_PLAYLISTS


def test_origin_restart_is_spliced_on_rather_than_dropped():
    """The origin's sequence drops from 497 to 64 when the encoder restarts.

    Numbered by the origin's sequence, the new segments sorted below the held ones, the
    trim deleted them on arrival, and the playlist stopped advancing.
    """
    for seq in (495, 496, 497):
        accumulate(POLL % (seq, "old%d.ts" % seq), MEDIA)
    restarted = accumulate(POLL % (64, "new64.ts"), MEDIA)

    assert names(restarted)[-1] == "new64.ts"           # the restart is what plays next
    assert "old497.ts" in names(restarted)              # and the old segments survive
    assert "#EXT-X-DISCONTINUITY" in restarted


def test_sequence_never_goes_backwards_across_a_restart():
    """AVFoundation reads a media sequence that drops as a different stream."""
    seen = []
    for seq in (495, 496, 497, 64, 65, 66, 67):
        merged = accumulate(POLL % (seq, "s%d.ts" % seq), MEDIA)
        seen.append(int(merged.split("#EXT-X-MEDIA-SEQUENCE:")[1].split("\n")[0]))
    assert seen == sorted(seen)


def test_an_older_copy_of_the_playlist_is_not_a_restart():
    """Origins re-serve stale playlists; only new segments at a lower sequence mean a
    restart."""
    for seq in (10, 11, 12):
        accumulate(POLL % (seq, "s%d.ts" % seq), MEDIA)
    current = accumulate(POLL % (12, "s12.ts"), MEDIA)
    assert accumulate(POLL % (10, "s10.ts"), MEDIA) == current
    assert "#EXT-X-DISCONTINUITY" not in current


def test_a_poll_past_everything_held_replaces_the_window():
    """No one polled for longer than the origin keeps a segment.

    Splicing new segments onto stale ones numbered them consecutively with no sign of
    the time jump, and an Apple TV that started in the stale part stopped at the jump.
    So the stale part is dropped.
    """
    for seq in (10, 11, 12):
        accumulate(POLL % (seq, "s%d.ts" % seq), MEDIA)
    assert names(accumulate(POLL % (13, "s13.ts"), MEDIA))[-2:] == ["s12.ts", "s13.ts"]

    later = accumulate(POLL % (200, "s200.ts"), MEDIA)
    assert names(later) == ["s200.ts"]
    assert "#EXT-X-MEDIA-SEQUENCE:200" in later
    assert "#EXT-X-DISCONTINUITY" not in later
    assert names(accumulate(POLL % (201, "s201.ts"), MEDIA)) == ["s200.ts", "s201.ts"]


def dates(text):
    return [line.split(":", 1)[1] for line in text.splitlines()
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME")]


def test_a_playlist_without_dates_is_given_them():
    """Without dates a player has no timeline, and the AirPlay hand-off asks for date
    ranges."""
    merged = accumulate(POLL % (10, "a.ts"), MEDIA)
    assert len(dates(merged)) == 1
    assert dates(merged)[0].endswith("Z")


def test_dates_advance_by_each_segment_s_own_duration():
    for seq, name in ((10, "a.ts"), (11, "b.ts"), (12, "c.ts")):
        merged = accumulate(POLL % (seq, name), MEDIA)
    stamps = [hls_proxy.time.mktime(hls_proxy.time.strptime(d[:19], "%Y-%m-%dT%H:%M:%S"))
              for d in dates(merged)]
    assert [round(b - a) for a, b in zip(stamps, stamps[1:])] == [5, 5]   # POLL is 5.0s


def test_a_segment_keeps_the_date_it_was_first_given():
    """A date that moves between reloads is worse than none."""
    first = accumulate(POLL % (10, "a.ts"), MEDIA)
    later = accumulate(POLL % (11, "b.ts"), MEDIA)
    assert dates(later)[0] == dates(first)[0]


def test_an_origin_that_dates_its_own_segments_is_left_alone():
    own = ("#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:5\n"
           "#EXT-X-PROGRAM-DATE-TIME:2020-01-01T00:00:00.000Z\n#EXTINF:5.0,\na.ts")
    merged = accumulate(own, MEDIA)
    assert dates(merged) == ["2020-01-01T00:00:00.000Z"]


def test_endlist_and_sequence_tags_are_not_re_emitted():
    merged = accumulate(POLL % (3, "a.ts") + "\n#EXT-X-ENDLIST\n", MEDIA)
    assert "#EXT-X-ENDLIST" not in merged
    assert merged.count("#EXT-X-MEDIA-SEQUENCE") == 1


VOD = ("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:0\n"
       "#EXT-X-PLAYLIST-TYPE:%s\n" + "".join("#EXTINF:10.0,\nfileSequence%d.ts\n" % n
                                            for n in range(20))
       + "#EXT-X-ENDLIST\n")


@pytest.mark.parametrize("kind", ["VOD", "EVENT"])
def test_a_playlist_that_keeps_every_segment_is_not_windowed(kind):
    assert keeps_everything(VOD % kind)


def test_a_live_playlist_is_still_windowed():
    assert not keeps_everything(POLL % (3, "a.ts"))
    assert not keeps_everything(POLL % (3, "a.ts") + "\n#EXT-X-ENDLIST\n")


# --------------------------------------------------------------------------- flattening

def test_plain_master_flattens():
    assert should_flatten("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8") is True


def test_separate_audio_blocks_flattening():
    """Pinning a variant drops the #EXT-X-MEDIA line, and the stream plays silent."""
    assert should_flatten('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="en.m3u8"\n'
                          '#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8') is False


def test_subtitles_alone_do_not_block_flattening():
    assert should_flatten('#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",URI="s.m3u8"\n'
                          '#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8') is True


def test_a_media_playlist_is_not_a_master():
    assert should_flatten("#EXTM3U\n#EXTINF:5.0,\na.ts") is False


# --------------------------------------------------------------------------- sniffing

@pytest.mark.parametrize("data, expected", [
    (b"\x47" + b"\x00" * 187 + b"\x47" + b"\x00" * 187, "video/MP2T"),
    (b"\x00\x00\x00\x18ftypiso5", "video/mp4"),
    (b"\x00\x00\x00\x18moof\x00\x00", "video/mp4"),
    (b"\xff\xf1\x50\x80", "audio/aac"),
    (b"ID3\x04", "audio/aac"),
    (b"\x1aE\xdf\xa3", "video/webm"),
    (b"#EXTM3U\n#EXT-X-VERSION:3", hls_proxy.PLAYLIST_MIME),
    (b"not media at all", "application/octet-stream"),
    (b"", "application/octet-stream"),
])
def test_sniff_mime(data, expected):
    assert sniff_mime(data) == expected


def test_a_lone_sync_byte_is_not_mpeg_ts():
    assert sniff_mime(b"\x47" + b"\x00" * 300) == "application/octet-stream"


# ------------------------------------------------------------------------------ shims

TS = (b"\x47" + b"\x00" * 187) * 40
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBPVP8L" + b"\x00" * 26      # 42 bytes, as in the wild


def test_media_behind_an_image_header_is_found():
    assert media_offset(WEBP + TS) == len(WEBP) == 42


def test_media_that_starts_where_it_should_is_left_alone():
    assert media_offset(TS) == 0


@pytest.mark.parametrize("data", [
    b"",
    b"\x00\x00\x00\x18ftypiso5" + b"\x00" * 4000,          # fMP4 carries its own header
    b"\x1aE\xdf\xa3" + b"\x00" * 4000,                       # webm
    b"nothing resembling media " * 200,
])
def test_nothing_is_dug_out_of_what_has_no_media_in_it(data):
    assert media_offset(data) == 0


def test_one_stray_sync_byte_is_not_a_shim():
    """Detection needs twenty packets at exact spacing, not one 0x47."""
    assert media_offset(b"junk" + b"\x47" + b"\x00" * 4000) == 0


def test_a_shim_beyond_the_search_window_is_left_alone():
    assert media_offset(b"\x00" * (hls_proxy.SHIM_MAX + 10) + TS) == 0


# --------------------------------------------------------------------------- ranges

@pytest.mark.parametrize("header, size, expected", [
    ("bytes=0-99", 1000, (0, 99)),
    ("bytes=100-", 1000, (100, 999)),
    ("bytes=-100", 1000, (900, 999)),
    ("bytes=0-99999", 1000, (0, 999)),
    ("bytes=-99999", 1000, (0, 999)),
    ("bytes=999-999", 1000, (999, 999)),
    ("bytes=1000-", 1000, None),            # past the end
    ("bytes=500-100", 1000, None),          # inside out
    ("bytes=0-99", 0, None),                # nothing to cut
    ("bytes=-0", 1000, None),
    ("items=0-99", 1000, None),             # not a byte range
    ("bytes=0-99, 200-299", 1000, None),    # multipart: serve the whole body
    ("", 1000, None),
    (None, 1000, None),
])
def test_parse_range(header, size, expected):
    assert parse_range(header, size) == expected
