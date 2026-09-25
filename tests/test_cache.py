"""The segment cache.

accumulate() re-advertises segments the origin has already dropped. Without a cached
copy the player gets a 404 for one and stalls, which the window is meant to prevent.
"""

import pytest

import hls_proxy
from hls_proxy import cache_get, cache_put, cache_size, configure_cache


@pytest.fixture(autouse=True)
def small_cache():
    configure_cache(1)          # one megabyte, so eviction is reachable
    yield
    configure_cache(0)


def test_round_trips():
    cache_put("a", "video/MP2T", b"xyz")
    assert cache_get("a") == ("video/MP2T", b"xyz")


def test_a_miss_is_none():
    assert cache_get("nothing") is None


def test_evicts_the_least_recently_served():
    half = b"." * (400 * 1024)
    cache_put("a", "video/MP2T", half)
    cache_put("b", "video/MP2T", half)
    cache_get("a")                                  # touching "a" makes "b" the oldest
    cache_put("c", "video/MP2T", half)
    assert cache_get("a") is not None
    assert cache_get("b") is None
    assert cache_get("c") is not None


def test_stays_inside_the_budget():
    for index in range(12):
        cache_put(str(index), "video/MP2T", b"." * (200 * 1024))
    _, held = cache_size()
    assert held <= 1024 * 1024


def test_re_putting_a_url_does_not_double_count():
    cache_put("a", "video/MP2T", b"." * 1000)
    cache_put("a", "video/MP2T", b"." * 1000)
    assert cache_size() == (1, 1000)


def test_a_segment_larger_than_the_budget_is_skipped():
    cache_put("huge", "video/MP2T", b"." * (2 * 1024 * 1024))
    assert cache_get("huge") is None


def test_an_outsized_segment_is_skipped_even_with_room():
    configure_cache(64)
    cache_put("huge", "video/MP2T", b"." * (hls_proxy.SEGMENT_MAX + 1))
    assert cache_get("huge") is None


def test_disabled_cache_keeps_nothing():
    configure_cache(0)
    cache_put("a", "video/MP2T", b"xyz")
    assert cache_get("a") is None
