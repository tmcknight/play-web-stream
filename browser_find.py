#!/usr/bin/env python3
"""Playwright fallback for pages whose player is assembled at runtime.

Reaching for a browser is the slow path -- it exists only for the pages where the
HTML scrape comes back empty. Two signals are collected: responses whose Content-Type
is actually a playlist (the strongest evidence there is, and it also reveals the exact
Referer the player used), and URLs scraped out of every frame's DOM once the player
has booted.

Candidates are then verified from *this* process with urllib rather than trusted from
inside the browser, because a plain HTTP client is what will fetch them afterwards.
"""

import os
import re
import urllib.parse

import hls_proxy

PLAYLIST_CT = re.compile(r'mpegurl|dash\+xml', re.I)


def _env_ms(name, fallback):
    value = os.environ.get(name, "").strip()
    return int(value) if value.isdigit() else fallback


# A slow page or a slow connection is the first thing worth turning up, so these are
# not buried constants: a player that has not started fetching within BOOT_MS looks
# from here exactly like a page with no stream in it.
BOOT_MS = _env_ms("PWS_BROWSER_BOOT_MS", 6000)
NAV_MS = _env_ms("PWS_BROWSER_NAV_MS", 25000)

# Lifted from the skill: match on content, not on a .m3u8 extension, because playlist
# URLs frequently have no extension at all.
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
    # The browser is a fetcher like any other here, so it leaves from wherever the
    # rest of the pipeline does. Left out, it would be the one step that still reached
    # the origin from this network's own address -- and the step that loads the page
    # the origin is most interested in.
    proxy = hls_proxy.egress_playwright()
    if proxy:
        say("upstream via %s" % hls_proxy.egress_label())
    if os.environ.get("CHROMIUM_NO_SANDBOX") == "1":
        # Chromium's own sandbox needs privileges a container should not be given.
        # The container is the boundary instead, which is why this is opt-in by env
        # and off everywhere else.
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

    # Last resort: the network URLs that failed verification from here may simply want
    # a Referer we have not tried, so retry them against every frame origin we saw.
    for url, _ in from_network:
        check = hls_proxy.verify_playlist(
            url, urllib.parse.urlsplit(url).scheme + "://" +
            urllib.parse.urlsplit(page_url).netloc + "/")
        if check.ok:
            return {"page": page_url, "playlist": url, "referer": check.referer}

    return None
