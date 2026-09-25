#!/usr/bin/env python3
"""Drive an AirPlay receiver's play queue, the only way most receivers now play video.

pyatv's public `play_url` sends `POST /play` and then RTSP `PUT /setProperty`. Modern
receivers take a play queue over `POST /command` instead, and the family room TV answers
the old verbs with `501 Not Implemented`. So this speaks the newer protocol over pyatv's
transport, bypassing its stream API.

Provenance: the payload shapes come from pyatv PR #2846 and PR #2899, via
jcarcinogen/PearPlay's `/command` adapter. All MIT, originating with pyatv (Pierre
Stahl). None of it is our own reverse engineering.

The receiver requires every step, in this order:

  1. pair-verify, which yields the verifier the event channel is keyed from;
  2. RTSP SETUP, naming the clock: PTP, or for an older receiver NTP and a timing
     port it will call back on;
  3. the event channel, on the port SETUP returns. Playback state arrives only here;
     `GET /playback-info` does not apply to this kind of session;
  4. INFO and RECORD, which the receiver expects before it will grant a session;
  5. a second SETUP for a type 130 stream, which registers the remote control
     session. `POST /command` returns 500 until it exists. The receiver answers with
     no data port, which is expected;
  6. the queue itself: insert the item, set its properties, set the rate.

Two receiver behaviours (found by PR #2899's author) close the connection with no
error response: `playerLoggingID` longer than six characters, and feedback in flight
while queue commands are issued. We skip the field, and start feedback only after the
queue is loaded.

Timing is PTP first, NTP only if the receiver refuses PTP. Up to tvOS 26 an NTP SETUP
worked, and this module used it. From tvOS 26.5 the receiver accepts NTP (SETUP
succeeds, the queue loads, the picture may start) but sends nothing on the event
channel, so step 6's wait for `playing` times out and the hand-off is reported as
refused. The picture dies after about twenty seconds anyway. The receiver now wants a
SETUP naming PTP with a peer list. We run no PTP clock; the receiver keeps its own.
Established by aptgetrekt/airplay-utils on tvOS 26.5 and 27.0 (PTP still playing at 60s
with 26 events; NTP silent and gone by 27s), also the source of the payload, via the
same pyatv PRs. `PWS_AIRPLAY_TIMING` forces one or the other, for a receiver that
accepts PTP and then misbehaves on it.

For NTP, the timing port is pinned. pyatv binds it ephemerally (`airplay/player.py`:
`local_addr = (rtsp.connection.local_ip, 0)`), and an ephemeral port cannot be published
through a docker bridge, which would force host networking on the whole app.
"""

import asyncio
import plistlib
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from pyatv import exceptions
from pyatv.auth.hap_channel import setup_channel
from pyatv.protocols.airplay.auth import verify_connection
from pyatv.protocols.airplay.channels import BaseEventChannel
from pyatv.protocols.raop.protocols import TimingServer
from pyatv.support.http import HttpResponse, http_connect
from pyatv.support.rtsp import RtspSession

# Apple's fixed UUID. The receiver grants the remote control session only to a client
# that asks with it.
REMOTE_CONTROL_UUID = "A6B27562-B43A-4F2D-B75F-82391E250194"

FEEDBACK_INTERVAL = 2       # the receiver drops a session that stops asking
TIMINGS = ("auto", "ptp", "ntp")

# Sender description for both kinds of SETUP. These are the values every working
# reference sends; do not vary them.
SENDER = {
    "deviceID": "AA:BB:CC:DD:EE:FF",
    "macAddress": "AA:BB:CC:DD:EE:FF",
    "model": "iPhone14,3",
    "name": "play-web-stream",
    "osBuildVersion": "20F66",
    "osName": "iPhone OS",
    "osVersion": "16.5",
    "sourceVersion": "690.7.1",
    "statsCollectionEnabled": False,
}


def _brief(data, limit=300):
    """Whatever the receiver sent, short enough to read in a log line."""
    try:
        text = ", ".join("%s=%r" % (k, v) for k, v in sorted(data.items())
                         if k != "type")
    except Exception:                                             # noqa: BLE001
        text = repr(data)
    return text[:limit] + ("..." if len(text) > limit else "")


def say(text):
    """One timestamped log line. The event channel is the only place a receiver
    reports on itself, so anything not logged here is lost."""
    sys.stderr.write("%s airplay: %s\n" % (time.strftime("%H:%M:%S"), text))
EVENT_LIMIT = 1024 * 1024   # a receiver that floods the event channel is broken, not busy


def _uid():
    return str(uuid4()).upper()


def _event_channel(on_state):
    """An event channel that reads playback state instead of discarding it.

    pyatv acknowledges these messages and discards them, so its player has to poll.
    Here they are the only record of when the receiver starts and stops playing. It
    expects nothing back beyond the acknowledgement.
    """

    class Events(BaseEventChannel):
        def handle_received(self):
            try:
                if len(self.buffer) > EVENT_LIMIT:
                    raise ValueError("oversized event")
                while self.buffer:
                    request, _, remaining = self.parse_request(self.buffer)
                    if request is None:
                        return
                    self.buffer = remaining

                    headers = {"Content-Length": "0", "Audio-Latency": "0"}
                    cseq = str(request.headers.get("CSeq", ""))
                    if cseq.isascii() and cseq.isdigit() and len(cseq) <= 10:
                        headers["CSeq"] = cseq
                    self.send(self.format_response(HttpResponse(
                        request.protocol, request.version, 200, "OK", headers, b"")))

                    raw = request.body
                    if isinstance(raw, str):
                        raw = raw.encode("utf-8")
                    data = plistlib.loads(raw).get("params", {}).get("data")
                    if isinstance(data, bytes):
                        on_state(plistlib.loads(data))
            except Exception:                                     # noqa: BLE001
                # A malformed event means the session is no longer intelligible.
                self.buffer = b""
                self.close()

    return Events


class PlaybackFailed(Exception):
    """The receiver refused some step of the hand-off."""


class AirPlaySession:
    """One receiver, one queued item, held open for as long as it should play."""

    def __init__(self, host, port, credentials, timing_port, timing="auto"):
        if timing not in TIMINGS:
            raise ValueError("timing must be one of %s, not %r"
                             % (", ".join(TIMINGS), timing))
        self.host = host
        self.port = port
        self.credentials = credentials
        self.timing_port = timing_port
        self.timing = timing

        self.connection = None
        self.rtsp = None
        self.verifier = None
        self.timing_transport = None
        self.timing_port_bound = None
        self.event_transport = None

        self.playing = asyncio.Event()
        self.stopped = asyncio.Event()
        self._started = False

        session_id = _uid()
        self.headers = {
            "User-Agent": "AirPlay/870.14.1",
            "Content-Type": "application/x-apple-binary-plist",
            "X-Apple-ProtocolVersion": "1",
            "X-Apple-Session-ID": session_id,
            "X-Apple-StreamID": "1",
        }
        self.session_id = session_id
        self.item_id = _uid()

    # -- the exchange

    async def play(self, url, timeout):
        """Load the URL into the queue and wait for the receiver to confirm it plays."""
        self.connection = await asyncio.wait_for(
            http_connect(self.host, self.port), timeout)
        self.rtsp = RtspSession(self.connection)

        self.verifier = await asyncio.wait_for(
            verify_connection(self.credentials, self.connection), timeout)

        base = None
        if self.timing != "ntp":
            try:
                base = await self._setup(self._ptp_body(), timeout)
                clock = "PTP timing"
            except (exceptions.HttpError, PlaybackFailed) as exc:
                if self.timing == "ptp":
                    raise PlaybackFailed("receiver refused a PTP session: %s" % exc) from exc
                # An older receiver refuses PTP outright, then accepts an NTP SETUP on
                # the same connection.
                say("%s refused PTP timing (%s); trying NTP" % (self.host, exc))
        if base is None:
            clock = await self._bind_timing(timeout)
            base = await self._setup(self._ntp_body(), timeout)

        await asyncio.wait_for(self._open_events(base["eventPort"]), timeout)
        await asyncio.wait_for(self.rtsp.info(), timeout)
        await self._expect(self.rtsp.record(), "RECORD", timeout)

        granted = await self._setup({"streams": [{
            "clientUUID": _uid(),
            "clientTypeUUID": REMOTE_CONTROL_UUID,
            "channelID": _uid() + "-RCS-1",
            "controlType": 1,
            "type": 130,
        }]}, timeout)
        stream_id = granted["streams"][0]["streamID"]
        if not isinstance(stream_id, int) or not 0 <= stream_id < 2 ** 64:
            raise PlaybackFailed("receiver returned an unusable stream ID")
        self.headers["X-Apple-StreamID"] = str(stream_id)

        # The item carries the URL twice. The receiver picks its player from the keys
        # present. With only `Content-Location` (what every reference sends) an .m3u8
        # goes to the progressive-file player, which reads the playlist once, plays
        # those segments, and reports `itemPlayedToEnd`: 17 seconds for a four-segment
        # window, 175 for a live stream with thirty. `HLS-Content-Location` routes it to
        # the HLS player, which follows the live edge. Found by bisection on the Family
        # Room TV: adding this key made the session run until stopped. Nothing else
        # changed the ending (PTP timing, Start-Date, Start-Position, forwardEndTime,
        # actionAtItemEnd, isInterestedInDateRange, the full item pyatv PR #2899 or
        # ruhbyook/VioletRelay send). The key came from VioletRelay.
        item = {"uuid": self.item_id, "mediaType": "file", "Content-Location": url}
        if urlsplit(url).path.lower().endswith(".m3u8"):
            item["HLS-Content-Location"] = url
        for command in (
            {"type": "insertPlayQueueItem", "item": item},
            {"type": "setProperty", "property": "isInterestedInDateRange",
             "value": True, "item": {"uuid": self.item_id}},
            {"type": "setProperty", "property": "actionAtItemEnd", "value": 1},
            {"type": "setRate", "rate": 1.0},
        ):
            await self._command(command, timeout)

        # The receiver reports state only once it has an item.
        await asyncio.wait_for(self.playing.wait(), timeout)
        say("%s took the stream; %s" % (self.host, clock))
        return clock

    async def keepalive(self):
        """Hold the session open until the receiver says the item ended.

        The receiver drops a session that goes quiet. This replaces pyatv's
        `/playback-info` polling, which these receivers answer with 500 because the
        endpoint does not apply to a queue session.
        """
        while not self.stopped.is_set():
            try:
                await asyncio.wait_for(self.stopped.wait(), FEEDBACK_INTERVAL)
            except (asyncio.TimeoutError, TimeoutError):
                try:
                    await self.rtsp.feedback()
                except Exception as exc:                          # noqa: BLE001
                    say("%s stopped answering feedback: %r" % (self.host, exc))
                    raise

    async def halt(self, timeout):
        """Ask the receiver to stop, then drop the session."""
        try:
            await self._command({"type": "setRate", "rate": 0.0}, timeout)
        except Exception:                                         # noqa: BLE001
            # Already gone, or refusing. The close below ends it either way.
            pass
        self.close()

    def close(self):
        self.stopped.set()
        for transport in (self.event_transport, self.timing_transport):
            if transport:
                transport.close()
        self.event_transport = self.timing_transport = None
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.rtsp:
            self.rtsp.requests.clear()

    # -- internals

    def _on_state(self, data):
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind != "playbackState":
            # Errors and rate changes arrive here too. They are the only explanation a
            # receiver gives when it gives up.
            say("%s said %s: %s" % (self.host, kind or "something unnamed",
                                    _brief(data)))
            return
        params = data.get("params")
        state = params.get("playbackState") if isinstance(params, dict) else None
        state = state or data.get("name")
        say("%s is %s%s" % (self.host, state,
                            "" if self._started else " (first word from it)"))
        if state == "playing":
            self._started = True
            self.playing.set()
        elif state == "stopped" and self._started:
            self.stopped.set()

    def _ptp_body(self):
        peer = {
            "ID": _uid(),
            "Addresses": [self.connection.local_ip],
            "DeviceType": 0,
            "SupportsClockPortMatchingOverride": True,
        }
        return {
            **SENDER,
            "timingProtocol": "PTP",
            "timingPeerInfo": peer,
            "timingPeerList": [peer],
            "sessionUUID": self.session_id,
            "sessionCorrelationUUID": peer["ID"],
            "updateSessionRequest": False,
            "isMultiSelectAirPlay": False,
        }

    def _ntp_body(self):
        return {
            **SENDER,
            "timingProtocol": "NTP",
            "timingPort": self.timing_port_bound,
            "sessionUUID": self.session_id,
            "sessionCorrelationUUID": _uid(),
            "isMultiSelectAirPlay": True,
            "groupContainsGroupLeader": False,
            "senderSupportsRelay": False,
        }

    async def _bind_timing(self, timeout):
        """Listen for the receiver's NTP requests, before SETUP announces where.

        The receiver may call back as soon as SETUP names the port, so it is bound
        first. Only NTP needs it; under PTP the receiver sends us nothing, so no UDP
        port has to be published.
        """
        self.timing_transport, server = await asyncio.wait_for(
            asyncio.get_running_loop().create_datagram_endpoint(
                TimingServer, local_addr=(self.connection.local_ip, self.timing_port)),
            timeout)
        self.timing_port_bound = server.port
        return "NTP timing on %s:%d" % (self.connection.local_ip, server.port)

    async def _open_events(self, port):
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise PlaybackFailed("receiver did not offer an event channel")
        self.event_transport, _ = await setup_channel(
            _event_channel(self._on_state), self.verifier, self.connection.remote_ip,
            port, "Events-Salt", "Events-Read-Encryption-Key",
            "Events-Write-Encryption-Key")

    async def _setup(self, body, timeout):
        response = await self._expect(self.rtsp.setup(body=body), "SETUP", timeout)
        return plistlib.loads(response.body)

    async def _command(self, command, timeout):
        # A plist wrapping a serialized plist. The receiver rejects it flattened.
        body = plistlib.dumps(
            {"params": {"data": plistlib.dumps(command, fmt=plistlib.FMT_BINARY)}},
            fmt=plistlib.FMT_BINARY)
        response = await asyncio.wait_for(self.connection.post(
            "/command", headers=dict(self.headers), body=body, allow_error=True), timeout)
        if response.code != 200:
            raise PlaybackFailed("receiver refused %s with %d"
                                 % (command["type"], response.code))
        return response

    @staticmethod
    async def _expect(awaitable, label, timeout):
        response = await asyncio.wait_for(awaitable, timeout)
        if response.code != 200:
            raise PlaybackFailed("receiver refused %s with %d" % (label, response.code))
        return response
