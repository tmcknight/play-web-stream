#!/usr/bin/env python3
"""Playwright fallback for pages whose player is assembled at runtime.

This is the slow path, used only when the HTML scrape finds nothing. It collects two
signals: responses with a playlist Content-Type (the strongest evidence, and they show the
Referer the player used), and URLs scraped from every frame's DOM once the player has
booted.

Candidates are verified from this process with urllib, because a plain HTTP client is
what fetches them afterwards.
"""

import os
import re
import urllib.parse

import hls_proxy

PLAYLIST_CT = re.compile(r'mpegurl|dash\+xml', re.I)


def _env_ms(name, fallback):
    value = os.environ.get(name, "").strip()
    return int(value) if value.isdigit() else fallback


# Tunable for slow pages and connections. A player that has not started fetching within
# BOOT_MS looks the same as a page with no stream.
BOOT_MS = _env_ms("PWS_BROWSER_BOOT_MS", 6000)
NAV_MS = _env_ms("PWS_BROWSER_NAV_MS", 25000)

# From the skill. Matches on content because playlist URLs often have no .m3u8
# extension.
SCRAPE_JS = r"""
() => {
  const html = document.documentElement.outerHTML;
  const hits = new Set();
  for (const m of html.matchAll(/https?:\/\/[^\s"'`<>\\]+/g)) {
    if (/\.m3u8|\.mpd|playlist|stream|manifest|\/hls\//i.test(m[0])) hits.add(m[0]);
  }
  for (const m of html.matchAll(/["']([A-Za-z0-9+/=]{24,})["']/g)) {
    try {
      const d = atob(m[1]);
      if (/^https?:\/\//.test(d)) hits.add(d);
    } catch (e) {}
  }
  return [...hits];
}
"""

# Most embeds will not build their player until something is clicked.
PLAY_SELECTORS = ("button[class*=play i]", "div[class*=play i]", "[id*=play i]",
                  ".vjs-big-play-button", ".jw-icon-display", ".plyr__control--overlaid",
                  "video")


def _click_anything(page):
    for selector in PLAY_SELECTORS:
        try:
            element = page.locator(selector).first
            if element.count() and element.is_visible(timeout=500):
                element.click(timeout=1500, force=True)
                return
        except Exception:
            continue
    try:
        page.mouse.click(640, 360)
    except Exception:
        pass


def find_playlist(page_url, on_progress=None):
    """Return {page, playlist, referer} or None."""
    say = on_progress or (lambda _msg: None)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("playwright is not installed on this machine -- "
                           "pip install playwright && playwright install chromium") from exc

    from_network = []       # (url, referer) in the order the player asked for them
    scraped = []

    args = ["--autoplay-policy=no-user-gesture-required", "--mute-audio"]
    # The browser goes through the egress proxy like every other upstream fetch.
    # Otherwise the page load would reach the origin from this network's address.
    proxy = hls_proxy.egress_playwright()
    if proxy:
        say("upstream via %s" % hls_proxy.egress_label())
    if os.environ.get("CHROMIUM_NO_SANDBOX") == "1":
        # Chromium's sandbox needs privileges a container should not have. Inside a
        # container, the container is the boundary, so this is opt-in by env.
        args += ["--no-sandbox", "--disable-dev-shm-usage"]

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, args=args, proxy=proxy)
        context = browser.new_context(user_agent=hls_proxy.UA,
                                      viewport={"width": 1280, "height": 720})
        page = context.new_page()

        def on_response(response):
            ctype = (response.headers.get("content-type") or "")
            if PLAYLIST_CT.search(ctype):
                from_network.append((response.url,
                                     response.request.headers.get("referer")))

        page.on("response", on_response)
        try:
            say("loading page in chromium")
            page.goto(page_url, wait_until="domcontentloaded", timeout=NAV_MS)
            _click_anything(page)
            page.wait_for_timeout(BOOT_MS)

            say("scraping %d frame(s)" % len(page.frames))
            for frame in page.frames:
                try:
                    scraped.extend(frame.evaluate(SCRAPE_JS))
                except Exception:
                    continue      # cross-origin frames that refuse evaluation
        finally:
            context.close()
            browser.close()

    # A response that arrived with a playlist Content-Type needs no guessing.
    for url, referer in from_network:
        check = hls_proxy.verify_playlist(url, referer or page_url)
        if check.ok:
            say("confirmed from a network response")
            return {"page": page_url, "playlist": url, "referer": check.referer}

    ordered, seen = [], set()
    for url in scraped:
        url = url.rstrip("\\\"'")
        if url in seen or hls_proxy.NOISE.search(url):
            continue
        seen.add(url)
        ordered.append(url)

    ordered = hls_proxy.rank_candidates(ordered, page_url)[:25]
    say("verifying %d scraped candidate(s)" % len(ordered))
    for url in ordered:
        check = hls_proxy.verify_playlist(url, page_url)
        if check.ok:
            return {"page": page_url, "playlist": url, "referer": check.referer}

    # Last resort: network URLs that failed verification may want a Referer not yet
    # tried, so retry them with the page origin.
    for url, _ in from_network:
        check = hls_proxy.verify_playlist(
            url, urllib.parse.urlsplit(url).scheme + "://" +
            urllib.parse.urlsplit(page_url).netloc + "/")
        if check.ok:
            return {"page": page_url, "playlist": url, "referer": check.referer}

    return None
