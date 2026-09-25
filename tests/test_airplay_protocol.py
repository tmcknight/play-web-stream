"""The SETUP that opens a hand-off, against a receiver that is only a script.

Which clock the session asks for is the difference between a television that plays
and one that accepts everything and then says nothing, so the order is pinned here:
PTP first, NTP only when the receiver refuses PTP, and neither when told otherwise.
Everything past pair-verify is real; the transport underneath it is not.
"""

import asyncio
import plistlib

import pytest

pytest.importorskip("pyatv")

from pyatv import exceptions  # noqa: E402

import airplay_protocol  # noqa: E402


class Response:
    def __init__(self, code=200, body=None):
        self.code = code
        self.body = plistlib.dumps(body or {}, fmt=plistlib.FMT_BINARY)


class Receiver:
    """Answers the exchange a real receiver would, and remembers what it was sent.

    `refuse_ptp` is a receiver from before PTP-timed video, which refuses that SETUP
    outright. Starting playback is what makes it report `playing`, since that is the
    event `play()` waits for.
    """

    def __init__(self, refuse_ptp=False):
        self.refuse_ptp = refuse_ptp
        self.setups = []
        self.commands = []
        self.session = None
        self.requests = {}
        self.local_ip = "127.0.0.1"
        self.remote_ip = "127.0.0.1"

    # -- the RtspSession half

    async def setup(self, body=None, headers=None):
        self.setups.append(body)
        if "streams" in body:
            return Response(body={"streams": [{"streamID": 7}]})
        if body["timingProtocol"] == "PTP" and self.refuse_ptp:
            raise exceptions.HttpError("RTSP method SETUP failed with code 400", 400)
        return Response(body={"eventPort": 50000})

    async def info(self):
        return {}

    async def record(self):
        return Response()

    async def feedback(self):
        return Response()

    # -- the connection half

    async def post(self, path, headers=None, body=None, allow_error=False):
        command = plistlib.loads(plistlib.loads(body)["params"]["data"])
        self.commands.append(command["type"])
        if command["type"] == "setRate":
            self.session._on_state({"type": "playbackState",
                                    "params": {"playbackState": "playing"}})
        return Response()

    def close(self):
        pass


@pytest.fixture
def receiver(monkeypatch):
    fake = Receiver()

    async def connect(host, port):
        return fake

    async def verify(credentials, connection):
        return object()

    async def events(self, port):
        assert port == 50000

    monkeypatch.setattr(airplay_protocol, "http_connect", connect)
    monkeypatch.setattr(airplay_protocol, "RtspSession", lambda connection: fake)
    monkeypatch.setattr(airplay_protocol, "verify_connection", verify)
    monkeypatch.setattr(airplay_protocol.AirPlaySession, "_open_events", events)
    return fake


def hand_off(receiver, timing="auto"):
    session = airplay_protocol.AirPlaySession("127.0.0.1", 7000, None, 0, timing)
    receiver.session = session

    async def run():
        try:
            return await session.play("http://192.168.1.2:8787/pl/x.m3u8", 5)
        finally:
            session.close()

    return asyncio.run(run())


def clocks(receiver):
    return [body.get("timingProtocol") for body in receiver.setups
            if "streams" not in body]


def test_a_receiver_that_takes_ptp_is_never_asked_about_ntp(receiver):
    assert hand_off(receiver) == "PTP timing"
    assert clocks(receiver) == ["PTP"]
    ptp = receiver.setups[0]
    assert ptp["timingPeerList"] == [ptp["timingPeerInfo"]]
    assert ptp["timingPeerInfo"]["Addresses"] == ["127.0.0.1"]
    assert "timingPort" not in ptp
    assert receiver.commands[-1] == "setRate"


def test_a_receiver_that_refuses_ptp_is_asked_again_with_ntp(receiver, capsys):
    receiver.refuse_ptp = True
    assert hand_off(receiver).startswith("NTP timing on 127.0.0.1:")
    assert clocks(receiver) == ["PTP", "NTP"]
    assert isinstance(receiver.setups[1]["timingPort"], int)
    assert receiver.setups[1]["timingPort"] > 0
    assert "refused PTP timing" in capsys.readouterr().err


def test_the_remote_control_session_follows_either_clock(receiver):
    receiver.refuse_ptp = True
    hand_off(receiver)
    assert receiver.setups[-1]["streams"][0]["type"] == 130


def test_pinning_ntp_skips_ptp_altogether(receiver):
    hand_off(receiver, "ntp")
    assert clocks(receiver) == ["NTP"]


def test_pinning_ptp_gives_up_rather_than_falling_back(receiver):
    receiver.refuse_ptp = True
    with pytest.raises(airplay_protocol.PlaybackFailed, match="PTP"):
        hand_off(receiver, "ptp")
    assert clocks(receiver) == ["PTP"]


def test_a_timing_that_is_neither_is_refused_before_anything_is_sent():
    with pytest.raises(ValueError, match="auto, ptp, ntp"):
        airplay_protocol.AirPlaySession("127.0.0.1", 7000, None, 0, "gps")
