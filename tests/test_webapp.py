"""The web app's logic, and the state files it shares with the proxy."""

import argparse
import json
import os
import socket
import stat
import threading

import pytest

import conftest
import hls_proxy
import webapp

ADVERTISED = "192.168.1.10"
AIRPLAY = {"name": "Family Room", "address": "192.168.1.50",
           "identifier": "AA:BB", "paired": True, "seen": True, "remembered": True}


@pytest.fixture(autouse=True)
def advertising():
    webapp.advertised = ADVERTISED
    webapp.opts = argparse.Namespace(allow_any=False)
    webapp.allow_hosts = frozenset()
    yield
    webapp.advertised = None
    webapp.allow_hosts = frozenset()


# --------------------------------------------------------------------------- hosts

@pytest.mark.parametrize("header, expected", [
    ("192.168.1.10:8786", "192.168.1.10"),
    ("box.local:8786", "box.local"),
    ("box.local", "box.local"),
    ("[fe80::1]:8786", "[fe80::1]"),
])
def test_hostname_of(header, expected):
    assert webapp.hostname_of(header) == expected


@pytest.mark.parametrize("header, known", [
    ("192.168.1.10:8786", True),        # the address people type
    ("192.168.1.10", True),
    ("127.0.0.1:8786", True),
    ("[fd00::5]:8786", True),           # bracketed IPv6 address
    ("localhost:8786", True),           # the box's own browser and the health check
    ("LOCALHOST:8786", True),
    ("evil.example:8786", False),       # DNS rebinding
    ("box.local:8786", False),          # not in the allowlist
    ("", False),
    ("not a host header", False),
])
def test_only_addresses_and_named_hosts_are_answered(header, known):
    """DNS rebinding needs a hostname; a literal address cannot be repointed."""
    assert webapp.known_host(header) is known


def test_a_named_host_is_answered_once_it_is_allowed():
    webapp.allow_hosts = webapp.host_allowlist(" Box.local , pws.lan ")
    assert webapp.known_host("box.local:8786") is True
    assert webapp.known_host("pws.lan") is True
    assert webapp.known_host("other.lan") is False


def test_host_allowlist_ignores_the_gaps():
    assert webapp.host_allowlist("") == frozenset()
    assert webapp.host_allowlist("a.lan,,  ,B.LAN") == frozenset({"a.lan", "b.lan"})


def test_localize_follows_the_address_the_client_used():
    """On localhost or through a port forward, the LAN address is unreachable."""
    url = "http://%s:8787/tok" % ADVERTISED
    assert webapp.localize(url, "localhost:8786") == "http://localhost:8787/tok"


def test_localize_leaves_an_origin_url_alone():
    """With no proxy the URL is the origin's, so it is left as is."""
    url = "https://cdn.example/a/index.m3u8"
    assert webapp.localize(url, "localhost:8786") == url


def test_localize_without_a_host_header_changes_nothing():
    url = "http://%s:8787/tok" % ADVERTISED
    assert webapp.localize(url, "") == url


def test_localize_keeps_the_path_and_query():
    url = "http://%s:8787/tok/live.m3u8" % ADVERTISED
    assert webapp.localize(url, "box.local:8786").endswith("/tok/live.m3u8")


# --------------------------------------------------------------------------- clients

@pytest.mark.parametrize("address, allowed", [
    ("192.168.1.20", True),
    ("10.0.0.5", True),
    ("172.16.4.4", True),
    ("127.0.0.1", True),
    ("169.254.9.9", True),          # link-local counts as private
    ("8.8.8.8", False),
    ("93.184.216.34", False),
    ("100.64.0.1", False),          # carrier-grade NAT is not a LAN
    ("not an address", False),
])
def test_only_private_clients_are_served(address, allowed):
    handler = webapp.Handler.__new__(webapp.Handler)
    handler.client_address = (address, 1234)
    assert handler._private_client() is allowed


def test_allow_any_opens_it_up():
    webapp.opts = argparse.Namespace(allow_any=True)
    handler = webapp.Handler.__new__(webapp.Handler)
    handler.client_address = ("8.8.8.8", 1234)
    assert handler._private_client() is True


@pytest.mark.parametrize("address, allowed", [
    ("192.168.1.20", True),         # a phone on the advertised LAN
    ("192.168.1.10", True),         # the box itself, by its LAN address
    ("127.0.0.1", False),           # local browser or a port forward (indistinguishable)
    ("::1", False),
    ("192.168.2.20", False),        # private, but a different LAN
    ("10.0.0.5", False),            # a VPN peer from another RFC1918 range
    ("172.17.0.1", False),          # the docker gateway, under bridge networking
    ("8.8.8.8", False),
    ("not an address", False),
])
def test_only_lan_clients_may_work_the_handoff(address, allowed):
    """Loopback passes _private_client() but must fail this check."""
    handler = webapp.Handler.__new__(webapp.Handler)
    handler.client_address = (address, 1234)
    assert handler._local_client() is allowed


def test_allow_any_does_not_open_up_the_handoff():
    """--allow-any widens who can use the app, not who can start AirPlay playback."""
    webapp.opts = argparse.Namespace(allow_any=True)
    handler = webapp.Handler.__new__(webapp.Handler)
    handler.client_address = ("127.0.0.1", 1234)
    assert handler._local_client() is False


@pytest.mark.parametrize("address", ["box.local", "", "fe80::1"])
def test_no_client_is_local_without_an_advertised_ipv4(address):
    """With no /24 to compare against, treat every client as off the LAN."""
    webapp.advertised = address
    handler = webapp.Handler.__new__(webapp.Handler)
    handler.client_address = ("192.168.1.20", 1234)
    assert handler._local_client() is False


def test_a_non_local_client_is_not_told_which_receiver_is_playing(monkeypatch):
    """GET /api/airplay withholds the device name too."""
    monkeypatch.setattr(webapp.hls_proxy, "state_files", list)

    def refuse():
        raise AssertionError("a non-local client must not be asked about sessions")

    monkeypatch.setattr(webapp.airplay, "status", refuse)
    assert webapp.running_streams("localhost:8786", local=False) == []


@pytest.fixture
def app(monkeypatch):
    """The real handler on a real socket, so every client is loopback.

    Anything forwarded to the app arrives from 127.0.0.1, the same as the server's own
    browser. AirPlay is faked as configured and paired, so a refusal shows the guard
    withholding real data.
    """
    monkeypatch.setattr(webapp.airplay, "available", lambda: True)
    monkeypatch.setattr(webapp.airplay, "status",
                        lambda: {"https://o.x/a.m3u8": [{"device": "Family Room",
                                                         "address": "192.168.1.50"}]})
    monkeypatch.setattr(webapp.airplay, "receivers",
                        lambda refresh=False: [AIRPLAY])
    monkeypatch.setattr(webapp.airplay, "pairing", lambda: None)
    monkeypatch.setattr(webapp.airplay, "sweep",
                        lambda network: pytest.fail("no sweep was asked for"))
    monkeypatch.setattr(webapp, "opts", argparse.Namespace(
        allow_any=False, window=None, cache_mb=None, advertise_ip=None, proxy_port=None,
        proxy_port_last=None))
    httpd = webapp.ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


# --------------------------------------------------------------------------- egress

@pytest.fixture
def unasked_egress(monkeypatch):
    """Clear the egress cache and unset the proxy, as on a fresh deploy."""
    monkeypatch.setattr(webapp, "_egress", {"at": 0.0, "report": None})
    for name in hls_proxy.PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


def test_the_page_is_told_when_nothing_is_proxied(app, unasked_egress):
    status, _, body = conftest.http(app + "/api/egress")
    assert status == 200
    assert json.loads(body) == {"enabled": False, "proxy": "", "forced": False,
                                "ip": "", "error": ""}


def test_the_egress_address_is_held_rather_than_asked_on_every_page_load(app,
                                                                         unasked_egress,
                                                                         monkeypatch):
    """The check costs an upstream round trip and the page asks on every load."""
    asked = []
    report = {"enabled": True, "proxy": "http://vpn.lan:8888", "forced": True,
              "ip": "203.0.113.7", "error": ""}
    monkeypatch.setattr(hls_proxy, "egress_check",
                        lambda *a, **kw: (asked.append(1), report)[1])

    for _ in range(3):
        status, _, body = conftest.http(app + "/api/egress")
    assert status == 200 and json.loads(body)["ip"] == "203.0.113.7"
    assert len(asked) == 1

    conftest.http(app + "/api/egress?refresh=1")
    assert len(asked) == 2


def test_a_failed_egress_check_is_asked_again_soon(app, unasked_egress, monkeypatch):
    """A tunnel still connecting must not show as down for the whole cache lifetime."""
    asked = []
    report = {"enabled": True, "proxy": "http://gluetun:8888", "forced": True,
              "ip": "", "error": "URLError: timed out"}
    monkeypatch.setattr(hls_proxy, "egress_check",
                        lambda *a, **kw: (asked.append(1), report)[1])

    conftest.http(app + "/api/egress")
    conftest.http(app + "/api/egress")
    assert len(asked) == 1

    webapp._egress["at"] -= webapp.EGRESS_RETRY_TTL + 1
    conftest.http(app + "/api/egress")
    assert len(asked) == 2


def test_startup_waits_for_a_tunnel_that_is_still_connecting(unasked_egress, monkeypatch,
                                                             capsys):
    """Gluetun connecting beside the app is not a fault, so it is not reported as one."""
    answers = iter([{"error": "URLError: timed out", "ip": ""},
                    {"error": "URLError: timed out", "ip": ""},
                    {"error": "", "ip": "203.0.113.7"}])
    base = {"enabled": True, "proxy": "http://gluetun:8888", "forced": True}
    monkeypatch.setattr(hls_proxy, "egress_check",
                        lambda *a, **kw: dict(base, **next(answers)))
    monkeypatch.setattr(webapp.time, "sleep", lambda _s: None)

    webapp.check_egress()

    err = capsys.readouterr().err
    assert "waiting for the egress proxy" in err
    assert "origins see 203.0.113.7" in err
    assert "WARNING" not in err


def test_startup_warns_about_a_tunnel_that_never_answers(unasked_egress, monkeypatch,
                                                         capsys):
    report = {"enabled": True, "proxy": "http://gluetun:8888", "forced": True,
              "ip": "", "error": "URLError: timed out"}
    monkeypatch.setattr(hls_proxy, "egress_check", lambda *a, **kw: dict(report))
    clock = [1000.0]
    monkeypatch.setattr(webapp.time, "time", lambda: clock[0])
    monkeypatch.setattr(webapp.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))

    webapp.check_egress()

    err = capsys.readouterr().err
    assert "did not answer within %ds" % webapp.EGRESS_STARTUP_WAIT in err
    assert "docker logs play-web-stream-vpn" in err


def test_a_loopback_client_is_not_told_there_is_a_receiver(app):
    status, _, body = conftest.http(app + "/api/airplay")
    assert status == 200
    assert json.loads(body) == {"available": False, "sessions": {}, "receivers": []}


def test_a_loopback_client_may_not_pair_a_receiver(app):
    """Pairing writes credentials for a local television."""
    status, _, body = conftest.http(
        app + "/api/airplay/pair", {"Content-Type": "application/json"}, method="POST",
        data={"action": "begin", "host": "192.168.1.50"})
    assert status == 403
    assert b"LAN clients only" in body


def test_a_loopback_client_may_not_start_airplay(app):
    status, _, body = conftest.http(
        app + "/api/airplay", {"Content-Type": "application/json"}, method="POST")
    assert status == 403
    assert b"LAN clients only" in body


def test_a_loopback_client_may_not_forget_a_receiver(app):
    """Forgetting edits the stored receivers, so it is LAN-only too."""
    status, _, body = conftest.http(
        app + "/api/airplay/forget", {"Content-Type": "application/json"},
        method="POST", data={"receiver": "192.168.1.50"})
    assert status == 403
    assert b"LAN clients only" in body


@pytest.fixture
def lan_app(app, monkeypatch):
    """The same app, with the client treated as on the LAN.

    A real socket makes every client loopback, which the guard refuses, so the allowed
    path is patched in.
    """
    monkeypatch.setattr(webapp.Handler, "_local_client", lambda self: True)
    return app


def test_a_lan_client_gets_the_whole_airplay_picture(lan_app):
    """One fetch returns availability, receivers, sessions and pairing state."""
    status, _, body = conftest.http(lan_app + "/api/airplay")
    assert status == 200
    answer = json.loads(body)
    assert answer["available"] is True
    assert answer["receivers"] == [AIRPLAY]
    assert answer["sessions"]["https://o.x/a.m3u8"][0]["address"] == "192.168.1.50"
    assert answer["pairing"] is None
    assert answer["error"] == ""


def test_a_page_load_never_sweeps_the_lan(lan_app):
    """The app fixture fails on a sweep. 254 probes should need a button press."""
    assert conftest.http(lan_app + "/api/airplay")[0] == 200


def test_asking_for_a_sweep_sweeps_the_advertised_slash_24(lan_app, monkeypatch):
    swept = []
    monkeypatch.setattr(webapp.airplay, "sweep", lambda network: swept.append(network))
    status, _, body = conftest.http(lan_app + "/api/airplay?scan=1")
    assert status == 200
    assert [str(net) for net in swept] == ["192.168.1.0/24"]
    assert json.loads(body)["error"] == ""


def test_a_sweep_that_will_not_run_is_reported_rather_than_500ing(lan_app, monkeypatch):
    monkeypatch.setattr(webapp, "advertised", "box.local")
    status, _, body = conftest.http(lan_app + "/api/airplay?scan=1")
    assert status == 200
    answer = json.loads(body)
    assert "nothing to sweep" in answer["error"]
    assert answer["receivers"] == [AIRPLAY], "the rest of the picture still arrives"


def test_airplay_hands_the_named_receiver_the_advertised_url(lan_app, monkeypatch):
    sent = {}

    def record(source, url, address=None):
        sent.update(source=source, url=url, address=address)
        return {"device": "Family Room", "address": address}

    monkeypatch.setattr(webapp.airplay, "start", record)
    monkeypatch.setattr(webapp.hls_proxy, "existing_instance",
                        lambda source: {"url": "http://%s:8787/tok" % ADVERTISED})
    status, _, body = conftest.http(
        lan_app + "/api/airplay", {"Content-Type": "application/json"}, method="POST",
        data={"action": "start", "source": "https://o.x/a.m3u8",
              "receiver": "192.168.1.50"})
    assert status == 200
    assert json.loads(body)["device"] == "Family Room"
    assert sent == {"source": "https://o.x/a.m3u8",
                    "url": "http://%s:8787/tok/live.m3u8" % ADVERTISED,
                    "address": "192.168.1.50"}


def test_stopping_names_the_receiver_too(lan_app, monkeypatch):
    """One stream may be on two televisions, so Stop names one."""
    stopped = {}
    monkeypatch.setattr(webapp.airplay, "stop",
                        lambda source, address=None: stopped.update(
                            source=source, address=address) or True)
    status, _, _ = conftest.http(
        lan_app + "/api/airplay", {"Content-Type": "application/json"}, method="POST",
        data={"action": "stop", "source": "s", "receiver": "192.168.1.51"})
    assert status == 200
    assert stopped == {"source": "s", "address": "192.168.1.51"}


def test_a_receiver_that_refuses_the_handoff_is_named_in_the_answer(lan_app, monkeypatch):
    """Pairing does not guarantee playback, so the error names the television."""
    def refuse(*args, **kwargs):
        raise RuntimeError("Bedroom would not take the stream: 501 Not Implemented")

    monkeypatch.setattr(webapp.airplay, "start", refuse)
    monkeypatch.setattr(webapp.hls_proxy, "existing_instance",
                        lambda source: {"url": "http://%s:8787/tok" % ADVERTISED})
    status, _, body = conftest.http(
        lan_app + "/api/airplay", {"Content-Type": "application/json"}, method="POST",
        data={"action": "start", "source": "s", "receiver": "192.168.1.51"})
    assert status == 200
    answer = json.loads(body)
    assert "501" in answer["error"]
    assert answer["receiver"] == "192.168.1.51"


def test_pairing_begins_and_finishes_across_two_requests(lan_app, monkeypatch):
    """The receiver shows its PIN between the two requests."""
    calls = []
    monkeypatch.setattr(webapp.airplay, "pair_begin",
                        lambda host: calls.append(("begin", host)) or {
                            "pairing": True, "device": "Kitchen", "address": host})
    monkeypatch.setattr(webapp.airplay, "pair_finish",
                        lambda pin: calls.append(("finish", pin)) or {
                            "paired": True, "device": "Kitchen"})

    status, _, body = conftest.http(
        lan_app + "/api/airplay/pair", {"Content-Type": "application/json"},
        method="POST", data={"action": "begin", "host": "192.168.1.52"})
    assert status == 200
    assert json.loads(body)["pairing"] is True

    status, _, body = conftest.http(
        lan_app + "/api/airplay/pair", {"Content-Type": "application/json"},
        method="POST", data={"action": "finish", "pin": "1234"})
    assert status == 200
    assert json.loads(body) == {"paired": True, "device": "Kitchen"}
    assert calls == [("begin", "192.168.1.52"), ("finish", "1234")]


def test_an_abandoned_pairing_can_be_dropped(lan_app, monkeypatch):
    monkeypatch.setattr(webapp.airplay, "pair_cancel", lambda: True)
    status, _, body = conftest.http(
        lan_app + "/api/airplay/pair", {"Content-Type": "application/json"},
        method="POST", data={"action": "cancel"})
    assert status == 200
    assert json.loads(body) == {"cancelled": True}


def test_a_receiver_that_will_not_pair_reports_rather_than_500s(lan_app, monkeypatch):
    def refuse(host):
        raise RuntimeError("pairing with Kitchen failed: the receiver refused it")

    monkeypatch.setattr(webapp.airplay, "pair_begin", refuse)
    status, _, body = conftest.http(
        lan_app + "/api/airplay/pair", {"Content-Type": "application/json"},
        method="POST", data={"action": "begin", "host": "192.168.1.52"})
    assert status == 200
    assert "refused it" in json.loads(body)["error"]


def test_forgetting_a_receiver_answers_with_what_is_left(lan_app, monkeypatch):
    forgotten = []
    monkeypatch.setattr(webapp.airplay, "forget",
                        lambda address: forgotten.append(address) or True)
    status, _, body = conftest.http(
        lan_app + "/api/airplay/forget", {"Content-Type": "application/json"},
        method="POST", data={"receiver": "192.168.1.51"})
    assert status == 200
    assert json.loads(body) == {"receivers": [AIRPLAY]}, "the card redraws from this"
    assert forgotten == ["192.168.1.51"]


def test_a_receiver_that_cannot_be_forgotten_reports_rather_than_500s(lan_app,
                                                                     monkeypatch):
    def refuse(address):
        raise RuntimeError("192.168.1.50 is named by PWS_AIRPLAY_HOST")

    monkeypatch.setattr(webapp.airplay, "forget", refuse)
    status, _, body = conftest.http(
        lan_app + "/api/airplay/forget", {"Content-Type": "application/json"},
        method="POST", data={"receiver": "192.168.1.50"})
    assert status == 200
    assert "PWS_AIRPLAY_HOST" in json.loads(body)["error"]


# ------------------------------------------------------------------ the startup probe

def test_startup_probes_what_is_remembered_and_says_what_answered(monkeypatch, capsys):
    """So a redeploy that lost a television shows up in the log."""
    monkeypatch.setattr(webapp.airplay, "available", lambda: True)
    monkeypatch.setattr(webapp.airplay, "receivers", lambda refresh=False: [
        dict(AIRPLAY),
        {"name": "Bedroom", "address": "192.168.1.51", "identifier": "CC:DD",
         "paired": True, "seen": False, "remembered": True}])
    webapp.probe_receivers()
    out = capsys.readouterr().err
    assert "192.168.1.50 Family Room -- paired" in out
    assert "192.168.1.51 Bedroom -- remembered, no answer" in out


def test_a_probe_that_will_not_run_does_not_stop_the_app_serving(monkeypatch, capsys):
    def refuse(refresh=False):
        raise RuntimeError("the network is not there")

    monkeypatch.setattr(webapp.airplay, "available", lambda: True)
    monkeypatch.setattr(webapp.airplay, "receivers", refuse)
    webapp.probe_receivers()
    assert "could not look for receivers" in capsys.readouterr().err


def test_without_pyatv_startup_says_nothing_about_receivers(monkeypatch, capsys):
    monkeypatch.setattr(webapp.airplay, "available", lambda: False)
    monkeypatch.setattr(webapp.airplay, "receivers",
                        lambda refresh=False: pytest.fail("nothing to probe"))
    webapp.probe_receivers()
    assert capsys.readouterr().err == ""


def test_a_pairing_request_with_no_action_is_a_400(lan_app):
    status, _, body = conftest.http(
        lan_app + "/api/airplay/pair", {"Content-Type": "application/json"},
        method="POST", data={"host": "192.168.1.52"})
    assert status == 400
    assert "begin, finish or cancel" in json.loads(body)["error"]


def test_a_rebound_name_is_refused_before_any_route(app):
    """The socket is a LAN client either way; the Host shows which site is driving it."""
    status, _, body = conftest.http(app + "/api/streams", {"Host": "evil.example"})
    assert status == 403
    assert b"Unrecognised Host" in body


def test_a_rebound_name_cannot_drive_a_post_route_either(app):
    status, _, body = conftest.http(
        app + "/api/stop", {"Host": "evil.example",
                            "Content-Type": "application/json"},
        method="POST", data={"source": "s"})
    assert status == 403


def test_an_allowed_name_is_served(app):
    """For a LAN with its own DNS name for the box."""
    webapp.allow_hosts = frozenset({"box.local"})
    assert conftest.http(app + "/api/streams", {"Host": "box.local:8786"})[0] == 200


def test_another_site_may_not_drive_the_resolver(app):
    """GET /api/resolve is a simple request, so CORS sends no preflight."""
    status, _, body = conftest.http(
        app + "/api/resolve?url=https://o.x/a.m3u8", {"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert b"Cross-site" in body


def test_our_own_page_still_drives_it(app, monkeypatch):
    monkeypatch.setattr(webapp.resolver, "resolve",
                        lambda url, **kw: pytest.fail("resolve is not the assertion"))
    assert conftest.http(app + "/", {"Sec-Fetch-Site": "same-origin"})[0] == 200


def test_a_cancelled_resolve_starts_no_proxy(app, monkeypatch):
    """Cancel closes the event stream, and the resolve must stop.

    It used to carry on and start a proxy, which then appeared under Running just after
    the user cancelled.
    """
    closed, finished = threading.Event(), threading.Event()
    outcome = []

    def slow_resolve(url, allow_browser=True, on_progress=None):
        on_progress("reading page HTML")
        closed.wait(10)
        try:
            on_progress("checking segment MIME types")
            outcome.append("carried on")
        except webapp.ClientGone:
            outcome.append("stopped")
            raise
        finally:
            finished.set()
        return {"needs_proxy": True, "playlist": "https://o.x/a.m3u8", "referer": None}

    monkeypatch.setattr(webapp.resolver, "resolve", slow_resolve)
    monkeypatch.setattr(webapp, "start_proxy",
                        lambda *a, **kw: pytest.fail("a proxy was started for nobody"))

    host, port = app.split("//")[1].split(":")
    sock = socket.create_connection((host, int(port)), timeout=10)
    sock.sendall(b"GET /api/resolve?url=https://o.x/page HTTP/1.1\r\n"
                 b"Host: 127.0.0.1\r\n\r\n")
    seen = b""
    while b"reading page HTML" not in seen:
        chunk = sock.recv(4096)
        assert chunk, "the stream closed before its first stage"
        seen += chunk
    sock.close()
    closed.set()

    assert finished.wait(10)
    assert outcome == ["stopped"]


def test_allow_any_lifts_the_host_check_but_not_the_cross_site_one(app):
    """--allow-any is for networks the operator knows better than we do.

    It says nothing about which site is driving the browser, so the cross-site check
    stays.
    """
    webapp.opts.allow_any = True
    assert conftest.http(app + "/api/streams", {"Host": "evil.example"})[0] == 200
    assert conftest.http(app + "/api/streams",
                         {"Host": "evil.example",
                          "Sec-Fetch-Site": "cross-site"})[0] == 403


def test_a_loopback_client_still_gets_the_page_and_the_streams(app):
    """The guard covers the hand-off only; watching from the box is allowed."""
    assert conftest.http(app + "/")[0] == 200
    status, _, body = conftest.http(app + "/api/streams")
    assert status == 200
    assert "streams" in json.loads(body)


# --------------------------------------------------------------------------- hand-off

def handler_for(payload, state=None, monkeypatch=None):
    monkeypatch.setattr(webapp.hls_proxy, "existing_instance", lambda source: state)
    return webapp.Handler.__new__(webapp.Handler)._handoff_url(payload)


def test_a_proxied_stream_is_handed_the_playlist_on_the_advertised_address(monkeypatch):
    """The receiver fetches over the LAN, so the localized URL would not work."""
    state = {"url": "http://%s:8787/tok" % ADVERTISED, "port": 8787}
    url = handler_for({"source": "https://o.x/a.m3u8"}, state, monkeypatch)
    assert url == "http://%s:8787/tok/live.m3u8" % ADVERTISED


def test_a_stream_with_no_proxy_is_handed_the_origin_url(monkeypatch):
    url = handler_for({"source": "https://o.x/a.m3u8", "url": "https://o.x/a.m3u8"},
                      None, monkeypatch)
    assert url == "https://o.x/a.m3u8"


@pytest.mark.parametrize("url", ["", "file:///etc/passwd", "javascript:alert(1)",
                                 "not a url"])
def test_nothing_but_a_http_url_is_handed_over(url, monkeypatch):
    assert handler_for({"source": "s", "url": url}, None, monkeypatch) is None


# --------------------------------------------------------------------------- warnings

def test_a_docker_bridge_address_is_called_out(capsys, monkeypatch):
    """With a bridge address nothing plays and nothing looks wrong, so warn loudly."""
    monkeypatch.setattr(webapp.os.path, "exists", lambda path: path == "/.dockerenv")
    webapp.check_advertised("172.17.0.3")
    assert "docker bridge" in capsys.readouterr().err


def test_a_lan_address_passes_quietly(capsys, monkeypatch):
    monkeypatch.setattr(webapp.os.path, "exists", lambda path: path == "/.dockerenv")
    webapp.check_advertised("192.168.1.10")
    assert capsys.readouterr().err == ""


@pytest.fixture
def handoffs(monkeypatch):
    """Report the hand-off as available without touching the network."""
    monkeypatch.setattr(webapp.airplay, "available", lambda: True)


def test_a_host_off_the_advertised_lan_is_called_out(capsys, monkeypatch, handoffs):
    """Under bridge networking the AirPlay controls hide for everyone, so warn."""
    monkeypatch.setattr(webapp.hls_proxy, "lan_ip", lambda: "172.17.0.3")
    webapp.check_handoff_reach(ADVERTISED)
    assert "AirPlay controls" in capsys.readouterr().err


def test_a_host_on_the_advertised_lan_passes_quietly(capsys, monkeypatch, handoffs):
    monkeypatch.setattr(webapp.hls_proxy, "lan_ip", lambda: ADVERTISED)
    webapp.check_handoff_reach(ADVERTISED)
    assert capsys.readouterr().err == ""


def test_nothing_is_said_when_no_handoff_is_configured(capsys, monkeypatch):
    """Without pyatv there are no controls to hide."""
    monkeypatch.setattr(webapp.airplay, "available", lambda: False)
    monkeypatch.setattr(webapp.hls_proxy, "lan_ip", lambda: "172.17.0.3")
    webapp.check_handoff_reach(ADVERTISED)
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------- state

def test_the_state_directory_is_private():
    """It holds each stream's token, so other accounts must not read it."""
    path = hls_proxy.state_dir()
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    assert mode & 0o077 == 0, oct(mode)
    assert os.lstat(path).st_uid == os.getuid()


def test_state_files_are_written_unreadable_to_others(tmp_path, monkeypatch):
    monkeypatch.setattr(hls_proxy, "state_dir", lambda: str(tmp_path))
    hls_proxy.write_state("https://o.x/a.m3u8", 8787, "http://10.0.0.5:8787/tok")
    written = hls_proxy.state_path("https://o.x/a.m3u8")
    assert stat.S_IMODE(os.lstat(written).st_mode) & 0o077 == 0


def test_the_proxy_log_is_private_too(tmp_path, monkeypatch):
    """It contains the serving URL, which carries the token."""
    monkeypatch.setattr(hls_proxy, "state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(webapp, "opts", argparse.Namespace(
        window=None, cache_mb=None, advertise_ip=None, proxy_port=None,
        proxy_port_last=None))


    class DeadChild:                        # a proxy that exits before it serves
        def __init__(self, *args, **kwargs):
            pass

        def poll(self):
            return 1

    monkeypatch.setattr(webapp.subprocess, "Popen", DeadChild)
    source = "https://o.x/a.m3u8"
    with pytest.raises(webapp.resolver.ResolveError):
        webapp.start_proxy(source, None)
    written = webapp.log_path(source)
    assert stat.S_IMODE(os.lstat(written).st_mode) & 0o077 == 0


def test_state_paths_differ_per_source(tmp_path, monkeypatch):
    monkeypatch.setattr(hls_proxy, "state_dir", lambda: str(tmp_path))
    assert hls_proxy.state_path("https://a/x") != hls_proxy.state_path("https://b/x")


def test_owns_pid_rejects_a_process_that_is_not_a_proxy():
    """Do not signal a recycled pid named by a stale state file."""
    if not os.path.isdir("/proc"):
        pytest.skip("no procfs")
    assert hls_proxy.owns_pid(os.getpid()) is False


def test_a_hostile_state_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(hls_proxy.tempfile, "gettempdir", lambda: str(tmp_path))
    impostor = tmp_path / ("play-web-stream-%d" % os.getuid())
    impostor.symlink_to(tmp_path)               # a symlink, not our directory
    with pytest.raises(SystemExit):
        hls_proxy.state_dir()


# ------------------------------------------------------------------------ capacity

@pytest.fixture
def ports():
    """Set the published port range, as the compose files do."""
    def configure(first, last):
        webapp.opts = argparse.Namespace(allow_any=False, proxy_port=first,
                                         proxy_port_last=last)
    return configure


def test_the_limit_is_the_published_range(ports, monkeypatch):
    ports(8787, 8790)
    monkeypatch.setattr(webapp, "live_states", lambda: [{}, {}])
    assert webapp.capacity() == {"limit": 4, "used": 2}


def test_an_unpublished_range_falls_back_to_the_search_range(ports, monkeypatch):
    """With no published range, the limit is the proxy's own port search range."""
    ports(None, None)
    monkeypatch.setattr(webapp, "live_states", list)
    assert webapp.capacity()["limit"] == hls_proxy.PORT_SEARCH_RANGE


def test_a_full_house_is_refused_before_a_proxy_is_started(ports, monkeypatch):
    """An extra stream would bind a port nothing forwards, and fail to play."""
    ports(8787, 8788)
    monkeypatch.setattr(webapp, "live_states", lambda: [{}, {}])
    monkeypatch.setattr(hls_proxy, "existing_instance", lambda source: None)

    def refuse(*args, **kwargs):
        raise AssertionError("a proxy was started with nowhere to serve")

    monkeypatch.setattr(webapp.subprocess, "Popen", refuse)
    with pytest.raises(webapp.resolver.ResolveError) as raised:
        webapp.start_proxy("https://o.x/a.m3u8", None)
    assert "2 stream slots" in raised.value.message


def test_a_stream_already_running_is_handed_back_even_when_full(ports, monkeypatch):
    """Reusing a proxy takes no new port, so the limit does not apply."""
    ports(8787, 8788)
    monkeypatch.setattr(webapp, "live_states", lambda: [{}, {}])
    monkeypatch.setattr(hls_proxy, "existing_instance",
                        lambda source: {"url": "http://192.168.1.10:8787/tok"})
    assert webapp.start_proxy("https://o.x/a.m3u8", None) == \
        ("http://192.168.1.10:8787/tok", True)


def test_the_streams_route_reports_the_capacity(app, monkeypatch):
    """The page shows remaining capacity from this field."""
    monkeypatch.setattr(webapp, "live_states", list)
    webapp.opts.proxy_port, webapp.opts.proxy_port_last = 8787, 8790
    status, _, body = conftest.http(app + "/api/streams")
    assert status == 200
    assert json.loads(body)["capacity"] == {"limit": 4, "used": 0}


def test_a_stream_will_not_bind_past_the_ceiling_it_was_given():
    """A port outside the published range is not forwarded."""
    held = socket.socket()
    held.bind(("0.0.0.0", 0))
    held.listen(1)
    taken = held.getsockname()[1]
    try:
        with pytest.raises(SystemExit):
            hls_proxy.bind_server(taken, taken)     # the only port allowed is in use

        httpd, port = hls_proxy.bind_server(taken)  # no ceiling: try higher ports
        httpd.server_close()
        assert port > taken
    finally:
        held.close()
