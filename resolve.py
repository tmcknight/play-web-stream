#!/usr/bin/env python3
"""Turn a page URL into a URL Safari's native player will actually accept.

Everything the skill does by hand between "here is a page" and "open this in Safari"
is mechanical, so it lives here as one pipeline: find the playlist, learn whether the
origin wants a Referer, check what the segments are really served as, and decide
whether the stream can be handed over untouched or has to go through hls_proxy.

No model in the loop -- every branch below is a string comparison.

    python3 resolve.py <page-url>
"""

import json
import re
import sys
import urllib.parse

import hls_proxy

# Re-serving these achieves nothing: the segments are Widevine/FairPlay encrypted, so
# AVFoundation would reject them for a reason no proxy can fix.
DRM_HOSTS = re.compile(
    r'(^|\.)(youtube\.com|youtu\.be|netflix\.com|disneyplus\.com|primevideo\.com|'
    r'amazon\.[a-z.]+|hulu\.com|max\.com|peacocktv\.com|paramountplus\.com|'
    r'bbc\.co\.uk|itv\.com|channel4\.com|tv\.apple\.com)$', re.I)

PLAYLIST_EXT = re.compile(r'\.m3u8(\?|$)', re.I)
DASH_EXT = re.compile(r'\.mpd(\?|$)', re.I)


class ResolveError(Exception):
    """A failure the caller should show the user verbatim, with what to do about it."""

    def __init__(self, message, hint=""):
        super().__init__(message)
        self.message = message
        self.hint = hint


def gate_error(gated):
    """A client gate that could not be cleared, as the message and hint the app shows."""
    why, instead = hls_proxy.explain_gate(gated.report)
    return ResolveError("The origin refuses this client -- every request returned HTTP %s."
                        % gated.report.status, "%s %s" % (why[0].upper() + why[1:], instead))


BROWSER_NOTE = "the origin refused Python's TLS handshake -- presenting Safari's instead"


# --------------------------------------------------------------------------- finding

def find_playlist(page_url, allow_browser=True, on_progress=None):
    """Locate the playlist behind a page, escalating to a browser only if forced to."""
    say = on_progress or (lambda _msg: None)
    host = urllib.parse.urlsplit(page_url).netloc.split(":")[0]
    if DRM_HOSTS.search(host):
        raise ResolveError(
            "%s is a DRM service -- the segments are encrypted and re-serving them "
            "achieves nothing." % host,
            "Use the service's own cast button, or screen mirroring.")

    if DASH_EXT.search(page_url):
        raise ResolveError("That is a DASH manifest, which has no native AirPlay path.",
                           "Screen mirroring is the fallback for DASH.")

    if PLAYLIST_EXT.search(page_url) or "/hls/" in page_url.lower():
        try:
            check = hls_proxy.verify_through_gate(page_url, None)
        except hls_proxy.Gated as exc:
            raise gate_error(exc) from None
        if check.ok:
            say("playlist given directly")
            return {"page": None, "playlist": page_url, "referer": check.referer,
                    "method": "direct"}
        # It looked like a playlist and did not serve one. The detail matters: a 403
        # means gated or expired, a 404 means the URL is stale, and a 200 of HTML means
        # it was never a playlist at all.
        raise ResolveError(
            "That URL did not return an HLS playlist -- %s." % check.detail,
            "A 403 or 404 usually means a signed URL that has expired, or an origin "
            "that wants the Referer of the page embedding it; paste the page URL "
            "instead. A 200 that is not a playlist was never one.")

    say("reading page HTML")
    gated = None
    try:
        found = hls_proxy.discover_through_gate(page_url)
    except hls_proxy.Gated as exc:
        # The page is gated, but a real browser -- the fallback below -- may still get
        # in and find the playlist; if that too is gated, probe_mime will say so.
        found, gated = None, exc
    if found:
        found["method"] = "html"
        return found

    if not allow_browser:
        if gated:
            raise gate_error(gated)
        raise ResolveError("No playlist in the page HTML.",
                           "The player is built at runtime; retry with the browser "
                           "fallback enabled.")

    say("no playlist in the HTML -- launching headless browser")
    import browser_find  # imported late; chromium is a slow import
    found = browser_find.find_playlist(page_url, on_progress=say)
    if found:
        found["method"] = "browser"
        return found

    raise ResolveError("No playlist found, in the HTML or at runtime.",
                       "The page may be DRM-protected, geo-blocked from this network, "
                       "or not streaming right now.")


# --------------------------------------------------------------------------- probing

def probe_mime(playlist, referer):
    """Report what the first segment is served as versus what its bytes say it is.

    AVFoundation refuses a segment whose Content-Type is wrong, which is what produces
    the crossed-out play icon. A mismatch here is the whole reason the proxy exists.
    """
    try:
        body, ctype = hls_proxy.fetch_through_gate(playlist, referer=referer)
    except hls_proxy.Gated as exc:
        raise gate_error(exc) from None
    text = body.decode("utf-8", "replace")

    url = playlist
    variant = None
    if hls_proxy.is_master(text):
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                variant = url = urllib.parse.urljoin(playlist, line)
                break
        if variant:
            body, _ = hls_proxy.fetch(variant, referer=referer)
            text = body.decode("utf-8", "replace")

    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            seg = urllib.parse.urljoin(url, line)
            # Some origins gate the segments as well as the playlist; the fetch sends
            # the Referer and drops it again for the origins that object to one. Only
            # the opening bytes are read -- naming a container does not need the rest.
            data, seg_ct = hls_proxy.fetch_head(seg, referer=referer)
            served = (seg_ct or "").split(";")[0].strip().lower()
            sniffed = hls_proxy.sniff_mime(data)
            return {"kind": "master" if variant else "media",
                    "variant": variant,
                    "playlist_content_type": ctype or "",
                    "segment": seg,
                    "served_as": served or "(none)",
                    "really_is": sniffed,
                    "match": served == sniffed.lower()}

    raise ResolveError("The playlist lists no segments.",
                       "The stream is probably not live right now.")


# --------------------------------------------------------------------------- verdict

def resolve(page_url, allow_browser=True, on_progress=None):
    """Find the stream and say whether it needs the proxy. Starts nothing.

    A client gate met on the way switches hls_proxy to a browser's handshake, and the
    verdict says so in `handshake`, so the proxy can be started presenting it from its
    first request. The switch lives inside `handshake_scope()`, which holds it to this
    thread: it was this origin's need, the next URL through here starts where every one
    before it did, and a resolve running alongside this one is not dragged along with
    it -- the web app serves these concurrently.
    """
    progress = on_progress or (lambda _msg: None)

    with hls_proxy.handshake_scope():
        noted = [hls_proxy.handshake()]

        def note():
            # The switch happens inside hls_proxy, which has no voice here; it is
            # worth a line of progress the first time it shows.
            if hls_proxy.handshake() != noted[0]:
                noted[0] = hls_proxy.handshake()
                progress(BROWSER_NOTE)

        def say(message):
            note()
            progress(message)

        found = find_playlist(page_url, allow_browser, say)
        say("checking segment MIME types")
        mime = probe_mime(found["playlist"], found["referer"])
        note()
        found = dict(found, handshake=hls_proxy.handshake())

    # A correct MIME type is not enough on its own: if the origin demanded a Referer,
    # Safari cannot send one, so the stream still has to be fetched on its behalf.
    if not mime["match"]:
        reason = "segments served as %s but are really %s" % (mime["served_as"],
                                                              mime["really_is"])
    elif found["referer"]:
        reason = "origin requires a Referer, which Safari will not send"
    elif hls_proxy.force_proxy():
        # A stream handed over as its own URL is fetched by the player -- or by the
        # Apple TV the player passed it to -- from this network's address, whatever
        # this process was careful to do. With an egress proxy on, that is the whole
        # hiding undone by the one stream that happened to need no fixing.
        reason = "an egress proxy is set, so the stream is fetched here, not by the player"
    else:
        reason = ""

    found = dict(found)
    found["mime"] = mime
    found["needs_proxy"] = bool(reason)
    found["reason"] = reason or "MIME types correct and no Referer needed"
    return found


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: resolve.py <page-url>")
    try:
        print(json.dumps(resolve(sys.argv[1], on_progress=lambda m: print("..", m,
                                                                          file=sys.stderr)),
                         indent=2))
    except ResolveError as exc:
        print(exc.message, file=sys.stderr)
        if exc.hint:
            print(exc.hint, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
