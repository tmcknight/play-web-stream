#!/usr/bin/env python3
"""A LAN web front end for the stream resolver: paste a page URL, get a playable one.

This is the skill's manual sequence with the human taken out of it -- discover, probe,
start the proxy if the stream needs one, hand back the URL. It is meant to run on a
box that shares the household's egress IP, so origins see the same address they would
have seen had the Mac fetched the stream itself -- or, with PWS_EGRESS_PROXY set, an
address belonging to whatever the proxy exits from instead (see `check_egress`).

It binds all interfaces so phones and Apple TVs on the LAN can reach it, and refuses
clients outside private address space. Do not put it behind a public hostname or a
tunnel: that turns it into an open relay for other people's video.

There is deliberately no remote-access feature here. Reaching it from another network
is the operator's own business, and whatever carries it lands in front of the same
checks: the address, the name, and the stricter test on the hand-off below.

The socket address is only half of who is asking, though -- see `known_host()` and
`_gate()`, which establish that the page driving that client is ours as well.

The AirPlay hand-off is held to a stricter test again, see `_local_client()`. It is
server-initiated, so a viewer who is not in the house would be starting playback on a
television here using our network position, and anything forwarded to us arrives on
loopback, which passes the private-address check like anyone else.

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
    """The proxies actually serving, as their own state files describe them.

    A state file outlives a proxy that died badly, so every one of them is checked
    against a live process before it counts.
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

    Each stream is a proxy of its own on a port of its own, taken at runtime. Under
    bridge networking those ports have to be published before anything binds them, so
    the range is settled at deploy time and the app has to be told where it ends --
    otherwise the stream past the end starts happily inside the container and is simply
    unreachable, which reads as a broken stream rather than a full house.
    """
    first = opts.proxy_port or hls_proxy.DEFAULT_PORT
    last = opts.proxy_port_last
    limit = last - first + 1 if last else hls_proxy.PORT_SEARCH_RANGE
    return {"limit": max(limit, 1), "used": len(live_states())}


def start_proxy(source, referer, handshake="python"):
    """Start hls_proxy for this source, or hand back the one already serving it.

    The child is detached, so restarting this app does not kill streams people are
    watching, and its own idle timeout still ends it when nobody is.
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

    # 0600 like the state file beside it: the log prints the serving URL, and that
    # URL carries the token. The directory already keeps other accounts out; this
    # means the file does not depend on the directory to do it.
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
    """The streams in flight, as the asking client is allowed to see them.

    A client that may not work the AirPlay controls is not told which receivers a
    stream went to either -- the device names are the same thing _local_client()
    withholds from GET /api/airplay.
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
    """The /24 around an address -- the LAN anything reachable from it sits on.

    None when the address admits no such network: a hostname, or IPv6, where a /24
    means nothing. Callers read that as "no client can be shown to be local".
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

    The socket address establishes that the client is on a network we serve. It says
    nothing about the page driving that client: a site that points its own name at our
    address gets a browser that passes `_private_client()` -- and `_local_client()`
    too, since the browser is the phone in the room -- and reads every reply, because
    it still believes it is talking to that site. So the name has to be recognised as
    well as the address.

    An address is safe by construction: there is no name to repoint. So is a name whose
    DNS we serve ourselves, which is what `--allow-hosts` is for. Everything else is
    refused, and that is the whole defence -- the attack needs a name to work with.
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

    A client that reached us by a name, or on localhost from the box itself, has no
    use for a URL naming the LAN address. The proxies already rewrite their own
    playlists from the request's Host header, so pointing the client at the right host
    is all that is left, on the assumption that the port numbers are unchanged. An
    origin URL is left alone: it is not ours.
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
    """An inline QR for the playback URL -- nobody is typing a tokenised path by hand.

    Dark modules on white, in both of the page's themes. It used to be drawn light on
    the dark card, which is an inverted code, and a good many phone cameras will not
    read one. The border is the four-module quiet zone the standard asks for, since
    the white patch it paints is the only margin the code has.
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

    The address is looked up through the proxy, which is a real round trip, so it is
    held for a couple of minutes: the page asks on every load and the answer changes
    only when the tunnel does. `refresh` is the button that says ask again now.
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

        Handing a stream to a television touches something in this house, so it wants
        a stronger signal than _private_client(). Anything forwarded to us arrives from
        loopback, exactly as the server's own browser does; the two are
        indistinguishable, so treat loopback as the untrusted case. Losing the button on
        the box running the app costs nothing -- that is not the machine anyone is
        holding in front of the TV.

        What carries the weight is the subnet test: the client is on the LAN we
        advertise, which is the LAN the receiver is on. That also rules out a VPN peer
        or a routed guest network arriving from some other RFC1918 range.
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

        Three separate things are being established, which is why it is three checks
        and not one: the client is on a network we serve, it reached us by a name we
        answer to, and no other site is the one driving it.
        """
        if not self._private_client():
            self._send(403, "LAN clients only.\n", "text/plain")
            return False
        if not (opts.allow_any or known_host(self.headers.get("Host", ""))):
            self._send(403, "Unrecognised Host. Reach this app by address, or name the "
                            "hostname you use in PWS_ALLOW_HOSTS.\n", "text/plain")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            # The browser telling us another site asked for this. No page but ours has
            # any business driving these routes, and the one that would carry -- GET
            # /api/resolve -- is a "simple" request CORS lets through without asking.
            self._send(403, "Cross-site requests are refused.\n", "text/plain")
            return False
        return True

    def _handoff_url(self, payload):
        """What to hand a receiver for the stream this request names, or None.

        The receiver fetches the media itself, which decides both halves of this. The
        playlist, not the page: what the proxy advertises is an HTML player for Safari
        to open, and a receiver would make nothing of it. And the advertised address,
        not the localized one, since the fetch comes from across the LAN rather than
        from the client that asked.

        A stream that needed no proxy has no instance to look up, so the URL travels
        with the request. It is the origin's, which is where Safari would have sent
        the receiver too -- the whole reason no proxy was needed.
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

        Asked rather than waited for, because a write to a socket the peer has closed
        usually succeeds -- it lands in our buffer and the peer's reset comes back
        afterwards -- so a failed write finds out one event late, and the event that
        matters is the one before a proxy is started. The page sends nothing after its
        GET, so a readable socket here is an end of file or a reset.
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
            # Cached, because it costs an upstream round trip and the answer only
            # changes when the tunnel does. The UI asks for it on every page load.
            self._json(200, egress_status(refresh=(query.get("refresh") or ["0"])[0] == "1"))
        elif parts.path == "/api/airplay":
            # Off for a non-local client, rather than confirming to a remote one that
            # there is a paired receiver here. ui.html already renders the control on
            # this flag, so the UI follows with no client-side logic.
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
                # With the receiver named, so a television that pairs and then refuses
                # the hand-off is reported against itself rather than against the app.
                self._json(200, {"error": str(exc), "receiver": receiver or ""})
        elif path == "/api/airplay/pair":
            if not self._local_client():
                self._send(403, "Pairing is for LAN clients only.\n", "text/plain")
                return
            self.airplay_pair(payload)
        elif path == "/api/airplay/forget":
            # Held to the same bar as pairing: it edits what this house has on file,
            # which is not a remote client's to do.
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

    # -- the AirPlay picker, and the pairing the PIN splits into two requests

    def airplay_receivers(self, scan):
        """What the picker needs: the feature, the receivers, and what is playing.

        A sweep only when asked. It is 254 probes and the better part of a minute, so
        it is a button rather than something a page load falls into -- and it needs an
        advertised IPv4 address to know which /24 to look at, which is the same thing
        `_local_client` needed to let this request through at all.
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

        Two requests, because the receiver shows its PIN only once pairing has begun:
        the handler stays alive between them inside `airplay`, and times out on its own
        so an abandoned attempt does not wedge the next.
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
            # The UI shows this string, so it arrives as a normal answer -- a receiver
            # that will not pair is news, not a server fault.
            self._json(200, {"error": str(exc)})

    # -- the one slow route

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

        # Cancel on the page closes the event stream, and that is all it can do. Without
        # this the resolve carried on regardless and started a proxy for a stream
        # nobody was waiting for, which then turned up under Running a moment after
        # the person had said no. Every stage is a chance to notice and stop.
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

    Inside a bridge-networked container `lan_ip()` reports the container's own address
    on the docker bridge. Everything looks healthy and nothing ever plays, because the
    receiver fetches the media URL itself -- so this is worth failing loudly over.
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

    _local_client() wants the client on the LAN we advertise. Under bridge networking
    every client arrives as the docker gateway instead, which is in no such subnet, so
    the AirPlay controls quietly switch themselves off for the whole house. That is the
    safe direction to fail in, but from the outside it looks like a bug.
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

    Two things come of it. The addresses in the file are proved or disproved before
    anybody asks, so the first page load reads a warm cache instead of waiting out a
    scan; and the boot log says which televisions answered, which is where a redeploy
    that lost its credentials or a receiver that moved becomes visible without anyone
    opening the UI.

    In a thread, because a scan is `SCAN_TIMEOUT` of waiting and nothing should hold
    the listener shut for that long. Failures are reported and dropped: a scan is not
    a reason to refuse to serve.
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

    A proxy that is set and unreachable fails closed on its own -- every upstream fetch
    goes through it and every one of them errors -- so this does not refuse to start.
    What it prevents is the quieter version: a tunnel believed to be up, a probe that
    never happened, and no line anywhere saying which address the origins are seeing.
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
