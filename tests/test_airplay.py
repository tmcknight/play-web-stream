"""Choosing and pairing a receiver, without pyatv or a television.

pyatv may not be installed where the checks run, so `available()` is faked and the scan
and pairing handler (the parts that touch the network) are replaced. What remains is
this module's own logic: which receiver gets a hand-off, what a session records about
it, and the two-step pairing the PIN requires.
"""

import json
import os
import stat
import threading

import pytest

import airplay

FAMILY = {"name": "Family Room", "address": "192.168.1.50",
          "identifier": "AA:BB", "paired": True}
BEDROOM = {"name": "Bedroom", "address": "192.168.1.51",
           "identifier": "CC:DD", "paired": True}
UNPAIRED = {"name": "Kitchen", "address": "192.168.1.52",
            "identifier": "EE:FF", "paired": False}


class Task:
    """A fake keepalive task.

    `cancelled` and `exception` are methods, as in asyncio, because the reap path asks
    a finished task how it ended.
    """

    def __init__(self, done=False, error=None):
        self.finished = done
        self.was_cancelled = False
        self.error = error

    def done(self):
        return self.finished

    def cancel(self):
        self.was_cancelled = True

    def cancelled(self):
        return self.was_cancelled

    def exception(self):
        return self.error


@pytest.fixture
def seen(monkeypatch):
    """Fake pyatv as present, with a scan that returns whatever the test sets.

    Coroutines are replaced by plain functions so nothing is left unawaited, which the
    suite treats as an error.
    """
    monkeypatch.setattr(airplay, "pyatv", object())
    monkeypatch.setattr(airplay, "HOSTS", [FAMILY["address"]])
    monkeypatch.setattr(airplay, "_receivers", None)
    monkeypatch.setattr(airplay, "_extra", [])
    monkeypatch.setattr(airplay, "_sessions", {})
    monkeypatch.setattr(airplay, "_pairing", None)
    monkeypatch.setattr(airplay, "_call", lambda result, timeout: result)

    looks = []
    answer = [[dict(FAMILY)]]

    def look(addresses, timeout):
        looks.append(list(addresses))
        return [dict(receiver) for receiver in answer[0]]

    monkeypatch.setattr(airplay, "_look", look)
    return {"looks": looks, "answer": answer}


# ------------------------------------------------------------------------- discovery

def test_configured_hosts_are_what_a_page_load_looks_at(seen):
    assert [r["name"] for r in airplay.receivers()] == ["Family Room"]
    assert seen["looks"] == [[FAMILY["address"]]]


def test_the_list_is_cached_until_asked_again(seen):
    airplay.receivers()
    airplay.receivers()
    assert len(seen["looks"]) == 1, "a page load must not rescan"
    airplay.receivers(refresh=True)
    assert len(seen["looks"]) == 2


def test_a_sweep_looks_at_every_address_on_the_slash_24(seen):
    seen["answer"][0] = [dict(FAMILY), dict(BEDROOM)]
    found = airplay.sweep("192.168.1.0/24")
    assert [r["name"] for r in found] == ["Bedroom", "Family Room"]
    assert len(set(seen["looks"][0])) == 254
    assert FAMILY["address"] in seen["looks"][0]


def test_what_a_sweep_found_is_looked_at_on_later_page_loads(seen):
    """Once found, a swept address is treated like a configured one."""
    seen["answer"][0] = [dict(FAMILY), dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")
    airplay.receivers(refresh=True)
    assert sorted(seen["looks"][-1]) == [FAMILY["address"], BEDROOM["address"]]


def test_a_configured_host_survives_a_sweep_that_does_not_cover_it(seen):
    airplay.sweep("10.0.0.0/24")
    assert FAMILY["address"] in seen["looks"][0]


def test_sweeping_wider_than_a_slash_24_is_refused(seen):
    with pytest.raises(RuntimeError, match="wider than a /24"):
        airplay.sweep("192.168.0.0/16")
    assert seen["looks"] == [], "nothing should have gone on the wire"


def test_a_network_that_is_not_one_is_refused(seen):
    with pytest.raises(RuntimeError, match="not a network"):
        airplay.sweep("the living room")


def test_an_address_named_rather_than_found_is_probed_and_kept(seen):
    """`pair <ip>` from a shell can name a receiver discovery never saw."""
    airplay.remember("192.168.1.99")
    assert "192.168.1.99" in seen["looks"][-1]


def test_nothing_is_scanned_when_there_is_nowhere_to_look(monkeypatch):
    """pyatv treats an empty host list as "browse", which means multicast."""
    monkeypatch.setattr(airplay, "pyatv", object())
    monkeypatch.setattr(airplay, "HOSTS", [])
    monkeypatch.setattr(airplay, "_extra", [])
    monkeypatch.setattr(airplay, "_call", lambda result, timeout:
                        pytest.fail("no scan should have been run"))
    assert airplay._look([], 1) == []


def test_without_pyatv_there_is_nothing_to_offer(monkeypatch):
    monkeypatch.setattr(airplay, "pyatv", None)
    assert airplay.available() is False
    assert airplay.receivers() == []


def test_pyatv_alone_is_enough_to_be_available(seen):
    """With no hosts configured it can still pair."""
    assert airplay.available() is True


# ---------------------------------------------------------------------- which receiver

@pytest.fixture
def three(seen):
    seen["answer"][0] = [dict(FAMILY), dict(BEDROOM), dict(UNPAIRED)]
    return seen


def test_a_receiver_is_chosen_by_address(three, monkeypatch):
    started = {}
    monkeypatch.setattr(airplay, "_begin", lambda url, target: started.setdefault(
        "entry", {"device": target["name"], "address": target["address"],
                  "task": Task(), "session": None, "url": url}))
    out = airplay.start("s", "http://x/live.m3u8", BEDROOM["address"])
    assert out == {"device": "Bedroom", "address": BEDROOM["address"]}


def test_an_address_nothing_answered_at_is_refused(three, monkeypatch):
    monkeypatch.setattr(airplay, "_begin",
                        lambda url, target: pytest.fail("nothing should be handed over"))
    with pytest.raises(RuntimeError, match="not a receiver this app has found"):
        airplay.start("s", "http://x/live.m3u8", "192.168.1.200")


def test_an_unpaired_receiver_is_named_rather_than_attempted(three, monkeypatch):
    """The error must name the television that needs pairing."""
    monkeypatch.setattr(airplay, "_begin",
                        lambda url, target: pytest.fail("nothing should be handed over"))
    with pytest.raises(RuntimeError, match="Kitchen is not paired"):
        airplay.start("s", "http://x/live.m3u8", UNPAIRED["address"])


def test_naming_no_receiver_works_only_while_there_is_one(seen, monkeypatch):
    monkeypatch.setattr(airplay, "_begin", lambda url, target: {
        "device": target["name"], "address": target["address"], "task": Task()})
    assert airplay.start("s", "http://x/live.m3u8")["device"] == "Family Room"


def test_naming_no_receiver_is_refused_when_several_are_paired(three, monkeypatch):
    monkeypatch.setattr(airplay, "_begin",
                        lambda url, target: pytest.fail("nothing should be handed over"))
    with pytest.raises(RuntimeError, match="say which one"):
        airplay.start("s", "http://x/live.m3u8")


def test_with_nothing_paired_the_advice_is_to_pair(three, monkeypatch):
    for receiver in three["answer"][0]:
        receiver["paired"] = False
    with pytest.raises(RuntimeError, match="pair one first"):
        airplay.start("s", "http://x/live.m3u8")


# -------------------------------------------------------------------------- sessions

@pytest.fixture
def handing_over(three, monkeypatch):
    """Fake the hand-off so sessions can be tested without a receiver."""
    monkeypatch.setattr(airplay, "_begin", lambda url, target: {
        "device": target["name"], "address": target["address"], "task": Task()})
    stopped = []
    monkeypatch.setattr(airplay, "_discard", stopped.append)
    monkeypatch.setattr(airplay, "_reap", stopped.append)
    return stopped


def test_a_session_remembers_which_receiver_took_it(handing_over):
    airplay.start("s", "http://x/live.m3u8", BEDROOM["address"])
    assert airplay.status() == {"s": [{"device": "Bedroom",
                                       "address": BEDROOM["address"]}]}


def test_one_stream_can_be_on_two_receivers_at_once(handing_over):
    airplay.start("s", "http://x/live.m3u8", FAMILY["address"])
    airplay.start("s", "http://x/live.m3u8", BEDROOM["address"])
    assert sorted(s["device"] for s in airplay.status()["s"]) == ["Bedroom", "Family Room"]


def test_starting_the_same_pair_twice_re_uses_the_session(handing_over):
    airplay.start("s", "http://x/live.m3u8", FAMILY["address"])
    again = airplay.start("s", "http://x/live.m3u8", FAMILY["address"])
    assert again["already"] is True
    assert len(airplay.status()["s"]) == 1


def test_stopping_names_the_receiver_and_leaves_the_others(handing_over):
    airplay.start("s", "http://x/live.m3u8", FAMILY["address"])
    airplay.start("s", "http://x/live.m3u8", BEDROOM["address"])
    assert airplay.stop("s", FAMILY["address"]) is True
    assert [s["device"] for s in airplay.status()["s"]] == ["Bedroom"]
    assert len(handing_over) == 1


def test_stopping_the_stream_stops_every_receiver_playing_it(handing_over):
    """`stop_stream` names no receiver and must still stop all of them."""
    airplay.start("s", "http://x/live.m3u8", FAMILY["address"])
    airplay.start("s", "http://x/live.m3u8", BEDROOM["address"])
    assert airplay.stop("s") is True
    assert airplay.status() == {}
    assert len(handing_over) == 2


def test_stopping_something_that_is_not_playing_says_so(handing_over):
    assert airplay.stop("s") is False


def test_a_session_the_receiver_finished_with_is_dropped(handing_over):
    airplay.start("s", "http://x/live.m3u8", FAMILY["address"])
    list(airplay._sessions.values())[0]["task"].finished = True
    assert airplay.status() == {}


def test_a_dropped_session_says_which_ending_it_had(handing_over, capsys):
    """The log distinguishes a receiver stopping from a feed failure, for debugging."""
    airplay.start("s", "http://x/live.m3u8", FAMILY["address"])
    list(airplay._sessions.values())[0]["task"].finished = True
    airplay.status()
    assert "the receiver reported it stopped" in capsys.readouterr().err

    airplay.start("s", "http://x/live.m3u8", FAMILY["address"])
    task = list(airplay._sessions.values())[0]["task"]
    task.finished, task.error = True, OSError("no route to host")
    airplay.status()
    assert "feeding it failed" in capsys.readouterr().err


# --------------------------------------------------------------------------- pairing

class Handler:
    """The parts of the pyatv pairing handler this module uses."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def pairing(three, monkeypatch):
    """A fake receiver that starts pairing and finishes on request."""
    state = {"handler": None, "pins": [], "refuse": False}

    def begin(address):
        state["handler"] = Handler()
        return {"handler": state["handler"], "storage": None, "timer": None,
                "name": UNPAIRED["name"], "address": address}

    def finish(entry, pin):
        state["pins"].append(pin)
        entry["handler"].close()        # the real one always closes it
        if state["refuse"]:
            raise RuntimeError("the receiver refused it")
        return

    monkeypatch.setattr(airplay, "_pair_begin", begin)
    monkeypatch.setattr(airplay, "_pair_finish", finish)
    yield state
    airplay.pair_cancel()


def test_pairing_is_two_steps_with_the_pin_between_them(pairing):
    begun = airplay.pair_begin(UNPAIRED["address"])
    assert begun == {"pairing": True, "device": "Kitchen",
                     "address": UNPAIRED["address"]}
    assert airplay.pairing() == {"device": "Kitchen", "address": UNPAIRED["address"]}

    done = airplay.pair_finish(" 1234 ")
    assert done["paired"] is True
    assert pairing["pins"] == ["1234"], "the PIN arrives stripped"
    assert airplay.pairing() is None


def test_finishing_a_pairing_tightens_the_credentials_file(pairing, tmp_path,
                                                          monkeypatch):
    """pyatv writes the file with the umask's mode, so we tighten it afterwards."""
    store = tmp_path / "pyatv.conf"
    store.write_text("{}")
    store.chmod(0o644)
    monkeypatch.setattr(airplay, "STORAGE", str(store))

    airplay.pair_begin(UNPAIRED["address"])
    airplay.pair_finish("1234")
    assert stat.S_IMODE(os.lstat(str(store)).st_mode) == 0o600


def test_a_pin_with_no_pairing_behind_it_is_refused(pairing):
    with pytest.raises(RuntimeError, match="begin one first"):
        airplay.pair_finish("1234")


def test_a_receiver_nothing_answered_at_is_not_paired_with(pairing):
    """Pairing writes credentials, so, like casting, it needs a discovered receiver."""
    with pytest.raises(RuntimeError, match="not a receiver this app has found"):
        airplay.pair_begin("192.168.1.200")


def test_an_empty_pin_closes_the_attempt_rather_than_sending_it(pairing):
    airplay.pair_begin(UNPAIRED["address"])
    with pytest.raises(RuntimeError, match="waiting for the PIN"):
        airplay.pair_finish("   ")
    assert pairing["pins"] == []
    assert pairing["handler"].closed is True


def test_a_refused_pin_names_the_receiver_and_ends_the_attempt(pairing):
    pairing["refuse"] = True
    airplay.pair_begin(UNPAIRED["address"])
    with pytest.raises(RuntimeError, match="pairing with Kitchen failed"):
        airplay.pair_finish("9999")
    assert airplay.pairing() is None, "a refused PIN must not hold the receiver"


def test_beginning_again_drops_the_attempt_nobody_finished(pairing):
    airplay.pair_begin(UNPAIRED["address"])
    first = pairing["handler"]
    airplay.pair_begin(UNPAIRED["address"])
    assert first.closed is True
    assert pairing["handler"] is not first


def test_an_abandoned_attempt_times_itself_out(pairing, monkeypatch):
    """Otherwise the next Pair press finds the receiver still busy."""
    timers = []
    monkeypatch.setattr(airplay.threading, "Timer",
                        lambda delay, fn: timers.append((delay, fn)) or Fake(timers))
    airplay.pair_begin(UNPAIRED["address"])
    assert timers[0][0] == airplay.PAIR_TIMEOUT
    timers[0][1]()                                  # as the timer would
    assert airplay.pairing() is None
    assert pairing["handler"].closed is True


class Fake:
    """A timer that records instead of scheduling."""

    def __init__(self, timers):
        self.timers = timers
        self.daemon = False

    def start(self):
        pass

    def cancel(self):
        pass


def test_cancelling_releases_the_receiver(pairing):
    airplay.pair_begin(UNPAIRED["address"])
    assert airplay.pair_cancel() is True
    assert pairing["handler"].closed is True
    assert airplay.pair_cancel() is False


def test_the_timer_does_not_hold_the_process_open(pairing):
    airplay.pair_begin(UNPAIRED["address"])
    with airplay._pair_lock:
        timer = airplay._pairing["timer"]
    assert isinstance(timer, threading.Timer)
    assert timer.daemon is True


# ------------------------------------------------------------------- what is remembered

def redeploy():
    """Clear the in-memory state a restart loses, keeping files on disk."""
    airplay._receivers = None
    airplay._extra = []
    airplay._remembered = None


def written():
    """The remembered receivers on disk, or None if the file was never written."""
    try:
        with open(airplay.REMEMBERED) as handle:
            return json.load(handle)["receivers"]
    except OSError:
        return None


def test_a_paired_receiver_is_written_down_when_a_sweep_finds_it(seen):
    seen["answer"][0] = [dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")
    assert written() == [{"name": "Bedroom", "address": BEDROOM["address"],
                          "identifier": "CC:DD"}]


def test_an_unpaired_receiver_is_not_worth_a_line(seen):
    """A sweep can rediscover it, so it is not written (no file is created)."""
    seen["answer"][0] = [dict(UNPAIRED)]
    airplay.sweep("192.168.1.0/24")
    assert written() is None


def test_a_redeploy_probes_what_it_remembered_instead_of_sweeping(seen):
    """The address survives a container restart, like the credentials."""
    seen["answer"][0] = [dict(FAMILY), dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")

    redeploy()
    airplay.receivers()
    assert sorted(seen["looks"][-1]) == [FAMILY["address"], BEDROOM["address"]]
    assert len(seen["looks"][-1]) == 2, "a page load must not sweep"


def test_a_remembered_receiver_that_says_nothing_is_still_listed(seen):
    """A receiver that is switched off stays listed, marked as not answering."""
    seen["answer"][0] = [dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")

    redeploy()
    seen["answer"][0] = []
    row = airplay.receivers()[0]
    assert row["name"] == "Bedroom"
    assert row["seen"] is False and row["remembered"] is True
    assert airplay.state(row) == "remembered, no answer"


def test_a_receiver_that_forgot_us_is_reported_rather_than_dropped(seen):
    """A reset television still answers but has lost the pairing keys."""
    seen["answer"][0] = [dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")

    redeploy()
    seen["answer"][0] = [dict(BEDROOM, paired=False)]
    row = airplay.receivers()[0]
    assert row["seen"] is True and row["paired"] is False
    assert airplay.state(row) == "no longer paired"
    assert written() != [], "keep the address; pairing again needs it"


def test_a_receiver_nobody_ever_paired_is_not_called_unpaired(seen):
    seen["answer"][0] = [dict(UNPAIRED)]
    assert airplay.state(airplay.receivers()[0]) == "not paired"


def test_a_silent_receiver_is_not_handed_a_stream(seen, monkeypatch):
    """`paired` on that row is remembered, not confirmed by this scan."""
    seen["answer"][0] = [dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")
    redeploy()
    seen["answer"][0] = []
    monkeypatch.setattr(airplay, "_begin",
                        lambda url, target: pytest.fail("nothing should be handed over"))
    with pytest.raises(RuntimeError, match="Bedroom did not answer"):
        airplay.start("s", "http://x/live.m3u8", BEDROOM["address"])


def test_a_silent_receiver_is_not_the_one_obvious_receiver_either(seen, monkeypatch):
    seen["answer"][0] = [dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")
    redeploy()
    seen["answer"][0] = []
    with pytest.raises(RuntimeError, match="pair one first"):
        airplay.start("s", "http://x/live.m3u8")


def test_a_file_that_is_nonsense_costs_a_sweep_and_nothing_else(seen):
    with open(airplay.REMEMBERED, "w") as handle:
        handle.write("{ this is not json")
    airplay._remembered = None
    assert [r["name"] for r in airplay.receivers()] == ["Family Room"]


# ----------------------------------------------------------------------------- forgetting

def test_forgetting_drops_the_address_and_stops_probing_for_it(seen):
    seen["answer"][0] = [dict(FAMILY), dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")

    airplay.forget(BEDROOM["address"])
    assert [r["name"] for r in airplay.receivers()] == ["Family Room"]
    assert written() == [{"name": "Family Room", "address": FAMILY["address"],
                          "identifier": "AA:BB"}]

    airplay.receivers(refresh=True)
    assert BEDROOM["address"] not in seen["looks"][-1]


def test_what_is_forgotten_stays_forgotten_across_a_redeploy(seen):
    seen["answer"][0] = [dict(FAMILY), dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")
    airplay.forget(BEDROOM["address"])

    redeploy()
    airplay.receivers()
    assert seen["looks"][-1] == [FAMILY["address"]]


def test_forgetting_leaves_the_credentials_alone(seen, monkeypatch):
    """Credentials are keyed by device, so a later sweep still finds it paired."""
    seen["answer"][0] = [dict(BEDROOM)]
    airplay.sweep("192.168.1.0/24")
    airplay.forget(BEDROOM["address"])
    assert [r["paired"] for r in airplay.sweep("192.168.1.0/24")] == [True]


def test_forgetting_a_configured_host_is_refused_rather_than_half_done(seen):
    """The next page load would read it back from the environment."""
    with pytest.raises(RuntimeError, match="PWS_AIRPLAY_HOST"):
        airplay.forget(FAMILY["address"])


def test_forgetting_something_never_known_is_refused(seen):
    with pytest.raises(RuntimeError, match="not a receiver this app remembers"):
        airplay.forget("192.168.1.200")
