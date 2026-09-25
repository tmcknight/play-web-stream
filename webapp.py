#!/usr/bin/env python3
"""A LAN web front end for the stream resolver: paste a page URL, get a playable one.

Runs the skill's sequence automatically: discover, probe, start the proxy if needed,
return the URL. Run it on a box that shares the household's egress IP, so origins see
the address the Mac would have used. With PWS_EGRESS_PROXY set, they see the proxy's
exit address instead (see `check_egress`).

It binds all interfaces so phones and Apple TVs on the LAN can reach it, and refuses
clients outside private address space. Do not put it behind a public hostname or a
tunnel: that makes it an open relay for other people's video.

There is no remote-access feature. Anything that forwards traffic here from another
network still meets the same checks: address, Host name, and the AirPlay test below.

`known_host()` and `_gate()` also check that the page driving the client is ours.

AirPlay routes need `_local_client()`. The hand-off is server-initiated, so a remote
viewer would be starting playback on a TV here from our network position. Forwarded
traffic arrives on loopback, which passes the private-address check.

    python3 webapp.py [--port 8786] [--bind 0.0.0.0]
"""

import argparse
import ipaddress
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import airplay
import hls_proxy
import resolve as resolver

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "hls_proxy.py")
UI = os.path.join(HERE, "ui.html")
START_TIMEOUT = 40          # resolve_source() fetches upstream before the state file lands

opts = None
advertised = None
allow_hosts = frozenset()


# --------------------------------------------------------------------------- proxies

def log_path(source):
    return hls_proxy.state_path(source)[:-len(".json")] + ".log"


def tail(path, limit=120, needle=None):
    try:
        with open(path, "r", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    if needle:
        lines = [line for line in lines if needle in line]
    return [line.rstrip("\n") for line in lines[-limit:]]


def live_states():
    """The proxies currently serving, read from their state files.

    A state file outlives a proxy that crashed, so each is checked against a live
    process.
    """
    out = []
    for path in hls_proxy.state_files():
        try:
            with open(path) as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            continue
        if hls_proxy.existing_instance(state.get("source", "")):
            out.append(state)
    return out


def capacity():
    """How many streams may run at once, and how many do.

    Each stream is its own proxy on its own port. Under bridge networking the port
    range is published at deploy time, so the app needs to know where it ends.
    Otherwise a stream past the end starts inside the container, is unreachable, and
    looks broken when the real problem is that every slot is taken.
    """
    first = opts.proxy_port or hls_proxy.DEFAULT_PORT
    last = opts.proxy_port_last
    limit = last - first + 1 if last else hls_proxy.PORT_SEARCH_RANGE
    return {"limit": max(limit, 1), "used": len(live_states())}


def start_proxy(source, referer, handshake="python"):
    """Start hls_proxy for this source, or hand back the one already serving it.

    The child is detached so restarting this app does not kill streams in use. Its
    idle timeout ends it.
    """
    running = hls_proxy.existing_instance(source)
    if running:
        return running["url"], True

    room = capacity()
    if room["used"] >= room["limit"]:
        raise resolver.ResolveError(
            "All %d stream slots are in use." % room["limit"],
            "Stop one under Running and try again. The limit is the range of ports "
            "published to this machine, so a further stream would serve on a port "
            "nothing forwards.")

    cmd = [sys.executable, PROXY, "--source", source]
    if referer:
        cmd += ["--referer", referer]
    if handshake == "browser":       # the resolver already learnt Python's is refused
        cmd += ["--browser-handshake"]
    if opts.window is not None:
        cmd += ["--window", str(opts.window)]
    if opts.cache_mb is not None:
        cmd += ["--cache-mb", str(opts.cache_mb)]
    if opts.advertise_ip:
        cmd += ["--ip", opts.advertise_ip]
    if opts.proxy_port:
        cmd += ["--port", str(opts.proxy_port)]
    if opts.proxy_port_last:
        cmd += ["--port-last", str(opts.proxy_port_last)]

    # 0600 like the state file: the log prints the serving URL, which carries the
    # token. The directory is already private; this does not rely on it.
    log = log_path(source)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        child = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                 start_new_session=True, cwd=HERE)

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        time.sleep(0.4)
        state = hls_proxy.existing_instance(source)
        if state:
            return state["url"], False
        if child.poll() is not None:
            raise resolver.ResolveError(
                "The proxy exited before it started serving.",
                " / ".join(tail(log, 6)) or "nothing in the log")

    raise resolver.ResolveError("The proxy did not come up within %ds." % START_TIMEOUT,
                                " / ".join(tail(log, 6)))


def running_streams(host="", local=True):
    """The running streams, filtered for the client asking.

    A client that fails _local_client() is not shown which receivers a stream went to,
    for the same reason GET /api/airplay hides device names from it.
    """
    casts = airplay.status() if local else {}
    out = []
    for state in live_states():
        player = tail(log_path(state["source"]), 1, needle="[player]")
        out.append({"source": state["source"],
                    "airplay": casts.get(state["source"], []),
                    "url": localize(state["url"], host),
                    "port": state["port"], "pid": state["pid"],
                    "player": player[0] if player else ""})
    return out


def stop_stream(source):
    airplay.stop(source)
    state = hls_proxy.existing_instance(source)
    if not state:
        return False
    # existing_instance() has already established this pid is one of ours.
    os.kill(state["pid"], signal.SIGTERM)
    # SIGTERM skips the proxy's own cleanup, so the state file is ours to remove.
    try:
        os.unlink(hls_proxy.state_path(source))
    except OSError:
        pass
    return True


def lan_network(address):
    """The /24 around an address, taken as its LAN.

    None for a hostname or IPv6, where a /24 means nothing. Callers treat None as
    "no client counts as local".
    """
    try:
        addr = ipaddress.ip_address(address or "")
    except ValueError:
        return None
    if addr.version != 4:
        return None
    return ipaddress.ip_network("%s/24" % addr, strict=False)


def hostname_of(host_header):
    parsed = urllib.parse.urlsplit("//" + host_header)
    name = parsed.hostname
    return "[%s]" % name if name and ":" in name else name


def host_allowlist(raw):
    """Parse --allow-hosts. Names only; an address never needs an entry."""
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def known_host(host_header):
    """Whether this Host is a name we answer to.

    The socket address shows the client is on a network we serve, but not which page
    is driving it. A site that points its own name at our address (DNS rebinding) gets
    a browser that passes `_private_client()` and `_local_client()`, and can read every
    reply because the browser thinks it is talking to that site.

    A bare address has no name to repoint, so it is safe. So is a name whose DNS we
    serve ourselves (`--allow-hosts`). Every other name is refused, since the attack
    needs one.
    """
    name = hostname_of(host_header)     # already lowercased, and bracketed for IPv6
    if not name:
        return False                    # no Host at all, or one urlsplit made nothing of
    if name == "localhost":             # the box's own browser, and the health check
        return True
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True
    except ValueError:
        return name in allow_hosts


def localize(url, host_header):
    """Point a playback URL at whatever address the client used to reach this app.

    A client that used a name, or localhost on the box itself, cannot use a URL with
    the LAN address. The proxies rewrite their playlists from the Host header, so only
    the host needs changing; ports are assumed unchanged. Origin URLs are left alone.
    """
    if not url or not host_header:
        return url
    parts = urllib.parse.urlsplit(url)
    host = hostname_of(host_header)
    if not host or not parts.port or parts.hostname != advertised:
        return url
    return urllib.parse.urlunsplit((parts.scheme, "%s:%d" % (host, parts.port),
                                    parts.path, parts.query, ""))


# --------------------------------------------------------------------------- qr

def qr_svg(data):
    """An inline QR for the playback URL, so nobody types a tokenised path.

    Dark on white in both themes: many phone cameras cannot read an inverted code. The
    border is the standard four-module quiet zone, and the only margin the code has.
    """
    try:
        import segno
    except ImportError:
        return ""
    import io
    buf = io.BytesIO()          # segno writes encoded bytes even for SVG
    segno.make(data, error="m").save(buf, kind="svg", scale=4, border=4,
                                     dark="#0b0b0e", light="#ffffff", xmldecl=False,
                                     svgns=True, omitsize=True)
    return buf.getvalue().decode("utf-8")


# --------------------------------------------------------------------------- serving

EGRESS_TTL = 120

_egress = {"at": 0.0, "report": None}
_egress_lock = threading.Lock()


def egress_status(refresh=False):
    """What the UI is told about where our upstream fetches leave from.

    Looking up the address costs a round trip through the proxy, and the page asks on
    every load, so the answer is cached for EGRESS_TTL seconds. `refresh` forces a new
    lookup.
    """
    with _egress_lock:
        fresh = _egress["report"] is not None and time.time() - _egress["at"] < EGRESS_TTL
        if fresh and not refresh:
            return _egress["report"]
    report = hls_proxy.egress_check()
    with _egress_lock:
        _egress.update(at=time.time(), report=report)
    return report


class ClientGone(Exception):
    """The page that asked for a resolve has stopped listening for its answer."""


class Handler(BaseHTTPRequestHandler):
    server_version = "play-web-stream"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers

    def _private_client(self):
        if opts.allow_any:
            return True
        try:
            return ipaddress.ip_address(self.client_address[0]).is_private
        except ValueError:
            return False

    def _local_client(self):
        """LAN-attached, as opposed to merely private.

        Starting playback on a TV in the house needs more than _private_client().
        Forwarded traffic arrives on loopback, the same as the server's own browser, so
        loopback is refused. Nobody controls the TV from the server box anyway.

        The main test is the subnet: the client must be on the advertised LAN, where
        the receiver is. That also excludes VPN peers and routed guest networks on
        other RFC1918 ranges.
        """
        network = lan_network(advertised)
        if network is None:
            return False
        try:
            client = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if client.is_loopback or not client.is_private:
            return False
        return client in network

    def _gate(self):
        """What every request passes before a route sees it.

        Three checks: the client is on a network we serve, it used a name we answer
        to, and no other site is driving it.
        """
        if not self._private_client():
            self._send(403, "LAN clients only.\n", "text/plain")
            return False
        if not (opts.allow_any or known_host(self.headers.get("Host", ""))):
            self._send(403, "Unrecognised Host. Reach this app by address, or name the "
                            "hostname you use in PWS_ALLOW_HOSTS.\n", "text/plain")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            # Another site sent this request. GET /api/resolve is a "simple" request
            # that CORS allows without a preflight, so it has to be refused here.
            self._send(403, "Cross-site requests are refused.\n", "text/plain")
            return False
        return True

    def _handoff_url(self, payload):
        """What to hand a receiver for the stream this request names, or None.

        The receiver fetches the media itself. So it gets the playlist, because the
        proxy's main URL is an HTML player page for Safari. And it gets the advertised
        address, because the fetch comes from across the LAN.

        A stream that needed no proxy has no instance, so its origin URL comes with the
        request. Safari would have sent the receiver there too.
        """
        state = hls_proxy.existing_instance(payload.get("source", ""))
        if state:
            return state["url"] + "/live.m3u8"
        url = payload.get("url", "")
        return url if re.match(r'^https?://', url) else None

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload), "application/json")

    def _client_gone(self):
        """Whether the far end of this connection has closed it.

        Checked directly because a write to a closed socket usually succeeds (it lands
        in our buffer and the reset comes later). A failed write would notice one event
        late, and the event that matters is the one before a proxy starts. The page
        sends nothing after its GET, so a readable socket means EOF or a reset.
        """
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _event(self, payload):
        self.wfile.write(("data: %s\n\n" % json.dumps(payload)).encode())
        self.wfile.flush()

    # -- routes

    def do_GET(self):
        if not self._gate():
            return

        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)

        if parts.path in ("/", "/index.html"):
            try:
                with open(UI, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "ui.html is missing\n", "text/plain")
        elif parts.path == "/api/streams":
            self._json(200, {"streams": running_streams(self.headers.get("Host", ""),
                                                        self._local_client()),
                             "capacity": capacity()})
        elif parts.path == "/api/log":
            source = (query.get("source") or [""])[0]
            needle = "[player]" if (query.get("player") or ["1"])[0] == "1" else None
            self._json(200, {"lines": tail(log_path(source), 60, needle)})
        elif parts.path == "/api/egress":
            self._json(200, egress_status(refresh=(query.get("refresh") or ["0"])[0] == "1"))
        elif parts.path == "/api/airplay":
            # Report "unavailable" to non-local clients so a remote one cannot learn
            # there is a paired receiver here. ui.html hides the control on this flag.
            if not self._local_client():
                self._json(200, {"available": False, "sessions": {}, "receivers": []})
            else:
                self.airplay_receivers((query.get("scan") or ["0"])[0] == "1")
        elif parts.path == "/api/resolve":
            self.resolve_stream((query.get("url") or [""])[0].strip(),
                                (query.get("browser") or ["1"])[0] == "1")
        else:
            self._send(404, "not found\n", "text/plain")

    def do_POST(self):
        if not self._gate():
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "bad JSON"})
            return

        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/stop":
            self._json(200, {"stopped": stop_stream(payload.get("source", ""))})
        elif path == "/api/airplay":
            if not self._local_client():
                self._send(403, "AirPlay is for LAN clients only.\n", "text/plain")
                return
            source = payload.get("source", "")
            receiver = payload.get("receiver") or None
            if payload.get("action") != "start":
                self._json(200, {"stopped": airplay.stop(source, receiver)})
                return
            url = self._handoff_url(payload)
            if url is None:
                self._json(404, {"error": "no proxy is serving that stream"})
                return
            try:
                self._json(200, airplay.start(source, url, receiver))
            except Exception as exc:                              # noqa: BLE001
                # Name the receiver so a TV that pairs then refuses the hand-off is
                # blamed, not the app.
                self._json(200, {"error": str(exc), "receiver": receiver or ""})
        elif path == "/api/airplay/pair":
            if not self._local_client():
                self._send(403, "Pairing is for LAN clients only.\n", "text/plain")
                return
            self.airplay_pair(payload)
        elif path == "/api/airplay/forget":
            # Same check as pairing: it changes the stored credentials.
            if not self._local_client():
                self._send(403, "AirPlay is for LAN clients only.\n", "text/plain")
                return
            try:
                airplay.forget(payload.get("receiver", ""))
                self._json(200, {"receivers": airplay.receivers()})
            except Exception as exc:                              # noqa: BLE001
                self._json(200, {"error": str(exc)})
        else:
            self._send(404, "not found\n", "text/plain")

    # -- the AirPlay picker, and two-request pairing

    def airplay_receivers(self, scan):
        """What the picker needs: the feature, the receivers, and what is playing.

        Sweeps only when asked: 254 probes take most of a minute. The sweep needs an
        advertised IPv4 address to pick the /24, as `_local_client` does.
        """
        answer = {"available": airplay.available(), "error": ""}
        if scan:
            network = lan_network(advertised)
            try:
                if network is None:
                    raise RuntimeError("no IPv4 LAN is advertised, so there is nothing "
                                       "to sweep")
                airplay.sweep(network)
            except Exception as exc:                              # noqa: BLE001
                answer["error"] = str(exc)
        answer["receivers"] = airplay.receivers()
        answer["sessions"] = airplay.status()
        answer["pairing"] = airplay.pairing()
        self._json(200, answer)

    def airplay_pair(self, payload):
        """Begin or finish a pairing, or drop one nobody finished.

        Two requests, because the receiver shows its PIN only after pairing begins.
        `airplay` keeps the handler alive between them and times it out so an abandoned
        attempt does not block the next.
        """
        action = payload.get("action", "")
        try:
            if action == "begin":
                self._json(200, airplay.pair_begin(payload.get("host", "")))
            elif action == "finish":
                self._json(200, airplay.pair_finish(payload.get("pin", "")))
            elif action == "cancel":
                self._json(200, {"cancelled": airplay.pair_cancel()})
            else:
                self._json(400, {"error": "action must be begin, finish or cancel"})
        except Exception as exc:                                  # noqa: BLE001
            # A 200, because a receiver that will not pair is not a server fault. The UI
            # shows the string.
            self._json(200, {"error": str(exc)})

    # -- the slow route

    def resolve_stream(self, url, allow_browser):
        """Streamed as events, because the browser fallback can take half a minute."""
        if not re.match(r'^https?://', url):
            self._json(400, {"error": "Give a http(s) URL."})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        # Cancel on the page only closes the event stream. Without this check the
        # resolve went on and started a proxy nobody wanted, which then appeared under
        # Running. Each stage checks and stops.
        def progress(message):
            if self._client_gone():
                raise ClientGone()
            try:
                self._event({"stage": message})
            except OSError:
                raise ClientGone() from None

        try:
            verdict = resolver.resolve(url, allow_browser=allow_browser,
                                       on_progress=progress)
            if verdict["needs_proxy"]:
                progress("starting the proxy")
                playback, reused = start_proxy(verdict["playlist"], verdict["referer"],
                                               verdict.get("handshake", "python"))
            else:
                playback, reused = verdict["playlist"], False

            playback = localize(playback, self.headers.get("Host", ""))
            verdict.update({"playback_url": playback, "reused": reused,
                            "qr": qr_svg(playback)})
            self._event({"done": verdict})
        except ClientGone:
            sys.stderr.write("play-web-stream: resolve of %s abandoned -- the page "
                             "stopped listening\n" % url)
        except resolver.ResolveError as exc:
            self._event({"error": exc.message, "hint": exc.hint})
        except Exception as exc:                                  # noqa: BLE001
            traceback.print_exc()
            self._event({"error": "%s: %s" % (type(exc).__name__, exc), "hint": ""})
        finally:
            self.close_connection = True


def env_int(name, fallback=None):
    value = os.environ.get(name, "").strip()
    return int(value) if value else fallback


def check_advertised(address):
    """Shout if the address we are about to hand out is one no Apple TV can reach.

    In a bridge-networked container `lan_ip()` returns the container's docker bridge
    address. Everything looks healthy but nothing plays, because the receiver fetches
    the media URL itself.
    """
    if not os.path.exists("/.dockerenv"):
        return
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return
    if addr in ipaddress.ip_network("172.16.0.0/12"):
        sys.stderr.write(
            "WARNING: advertising %s, which is the docker bridge, not your LAN.\n"
            "         Use network_mode: host, or set PWS_ADVERTISE_IP to this host's "
            "LAN address.\n" % address)


def check_handoff_reach(address):
    """Shout when the AirPlay controls will be off for everybody.

    _local_client() requires the client on the advertised LAN. Under bridge networking
    every client arrives as the docker gateway, outside that subnet, so the AirPlay
    controls disappear for everyone. Safe, but it looks like a bug.
    """
    if not airplay.available():
        return
    network = lan_network(address)
    if network is None:
        sys.stderr.write(
            "WARNING: advertising %s, which is not an IPv4 address, so no client can\n"
            "         be recognised as LAN-attached and the AirPlay controls stay "
            "hidden.\n" % address)
        return
    try:
        own = ipaddress.ip_address(hls_proxy.lan_ip())
    except (ValueError, OSError):
        return
    if own not in network:
        sys.stderr.write(
            "WARNING: this host answers on %s, off the %s we advertise -- under bridge\n"
            "         networking every client arrives as the docker gateway. None of "
            "them\n"
            "         will count as LAN-attached, so the AirPlay controls stay "
            "hidden.\n"
            "         Use network_mode: host.\n"
            % (own, network))


def probe_receivers():
    """Probe what we remember, once, while the server is coming up.

    The first page load then reads a warm cache instead of waiting for a scan, and the
    boot log lists which TVs answered. That shows lost credentials after a redeploy, or
    a receiver that moved, without opening the UI.

    Runs in a thread because a scan waits up to `SCAN_TIMEOUT`. Failures are logged and
    ignored; a failed scan is no reason to stop serving.
    """
    if not airplay.available():
        return
    try:
        found = airplay.receivers(refresh=True)
    except Exception as exc:                                      # noqa: BLE001
        sys.stderr.write("play-web-stream: could not look for receivers: %s\n" % exc)
        return
    for receiver in found:
        sys.stderr.write("play-web-stream: %s %s -- %s\n"
                         % (receiver["address"], receiver["name"],
                            airplay.state(receiver)))


def check_egress():
    """Say at startup where upstream fetches are leaving from, and shout if nowhere.

    An unreachable proxy already fails closed (every upstream fetch errors), so this
    does not stop startup. It makes sure the log says which address origins see, so a
    tunnel assumed to be up can be checked.
    """
    report = egress_status()
    if not report["enabled"]:
        return
    if report["error"]:
        sys.stderr.write("WARNING: the egress proxy %s did not answer: %s\n"
                         "         Upstream fetches will fail until it does; nothing "
                         "falls back to this network's own address.\n"
                         % (report["proxy"], report["error"]))
        return
    sys.stderr.write("play-web-stream: upstream via %s -- origins see %s\n"
                     % (report["proxy"], report["ip"] or "an address it would not say"))
    if not report["forced"]:
        sys.stderr.write("WARNING: PWS_FORCE_PROXY is off, so a stream needing no "
                         "rewriting is handed\n         to the player as its own URL "
                         "and fetched from this network.\n")


def main():
    global opts, advertised, allow_hosts
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=env_int("PWS_PORT", 8786),
                    help="port for this UI (proxies take 8787 and up)")
    ap.add_argument("--bind", default=os.environ.get("PWS_BIND", "0.0.0.0"),
                    help="address to bind (default: all interfaces, for phones on the LAN)")
    ap.add_argument("--advertise-ip", default=os.environ.get("PWS_ADVERTISE_IP") or None,
                    help="address the proxies should advertise; required under bridge "
                         "networking, where this machine's own IP is the docker bridge")
    ap.add_argument("--proxy-port", type=int, default=env_int("PWS_PROXY_PORT"),
                    help="first port a stream may take (default: the proxy's own 8787); "
                         "each stream takes the next free one above it")
    ap.add_argument("--proxy-port-last", type=int, default=env_int("PWS_PROXY_PORT_LAST"),
                    help="last port a stream may take, and so the number of streams that "
                         "may run at once; set it to the end of the range published to "
                         "this machine, which is what the UI then reports")
    ap.add_argument("--window", type=int, default=env_int("PWS_WINDOW"),
                    help="segment window passed to each proxy (default: the proxy's own)")
    ap.add_argument("--cache-mb", type=int, default=env_int("PWS_CACHE_MB"),
                    help="memory each proxy keeps for recently served segments, so the "
                         "widened window survives the origin dropping them")
    ap.add_argument("--allow-hosts", default=os.environ.get("PWS_ALLOW_HOSTS", ""),
                    help="comma-separated hostnames this app may be reached by, for a "
                         "name you serve the DNS for yourself (box.local, pws.lan); "
                         "addresses and localhost always work and need no entry")
    ap.add_argument("--allow-any", action="store_true",
                    default=os.environ.get("PWS_ALLOW_ANY") == "1",
                    help="serve clients outside private address space (don't)")
    opts = ap.parse_args()

    advertised = opts.advertise_ip or hls_proxy.lan_ip()
    allow_hosts = host_allowlist(opts.allow_hosts)
    check_advertised(advertised)
    check_handoff_reach(advertised)
    threading.Thread(target=check_egress, daemon=True).start()

    httpd = ThreadingHTTPServer((opts.bind, opts.port), Handler)
    sys.stderr.write("play-web-stream: http://%s:%d/\n" % (advertised, opts.port))
    threading.Thread(target=probe_receivers, daemon=True).start()
    if opts.allow_any:
        sys.stderr.write("WARNING: serving clients outside the LAN\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
