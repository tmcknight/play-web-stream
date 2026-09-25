"""The signature on every rewritten URI.

Without it /seg/ fetches any URL it is given, and the path token (which people paste
and share) would open everything the box can reach.
"""

import base64

import pytest

import hls_proxy
from hls_proxy import BadToken, decode_url, encode_url

PREFIX = "http://10.0.0.5:8787/tok"
URL = "https://cdn.example/a/seg1.ts"


def token_for(url, route="seg"):
    return encode_url(url, route, PREFIX).rsplit("/", 1)[-1]


def test_round_trips():
    assert decode_url(token_for(URL), "seg") == URL


def test_survives_a_url_needing_padding():
    for url in ("https://o.x/a", "https://o.x/ab", "https://o.x/abc"):
        assert decode_url(token_for(url), "seg") == url


def test_refuses_a_token_minted_for_the_other_route():
    with pytest.raises(BadToken):
        decode_url(token_for(URL, "seg"), "pl")


def test_refuses_a_swapped_url():
    """Keep a real signature but point the URL at the local network."""
    signature = token_for(URL).split(".")[1]
    forged = base64.urlsafe_b64encode(b"http://127.0.0.1:8786/api/streams").decode()
    with pytest.raises(BadToken):
        decode_url(forged.rstrip("=") + "." + signature, "seg")


def test_refuses_an_unsigned_token():
    with pytest.raises(BadToken):
        decode_url(base64.urlsafe_b64encode(b"http://127.0.0.1/").decode(), "seg")


def test_refuses_rubbish():
    for token in ("", ".", "!!!.!!!", "a.b", "...."):
        with pytest.raises(BadToken):
            decode_url(token, "seg")


def test_signature_covers_the_route():
    assert hls_proxy.sign("seg", URL) != hls_proxy.sign("pl", URL)


# ------------------------------------------------------------------- player telemetry

@pytest.mark.parametrize("raw, clean", [
    ("waiting", "waiting"),
    ("a\nb", "a b"),                        # the forged line, folded back into one
    ("a\r\n  [player] stalled", "a    [player] stalled"),   # CRLF is two characters
    ("a\x00b\x7f", "a b "),
])
def test_a_player_event_cannot_forge_a_log_line(raw, clean):
    """Anyone with the token can send these, and /api/log shows them to the operator."""
    assert hls_proxy.scrub(raw) == clean


def test_a_player_event_cannot_flood_the_log():
    assert len(hls_proxy.scrub("x" * 5000)) == 200
