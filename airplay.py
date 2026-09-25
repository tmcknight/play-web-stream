#!/usr/bin/env python3
"""Hand a stream to an AirPlay receiver directly, instead of asking Safari to do it.

AirPlay video never restreams: the receiver is given a URL and fetches it itself. The
proxy already binds all interfaces and advertises a LAN address for exactly that
reason, so the receiver can already reach our streams -- Safari is only acting as a
remote control. This replaces that remote control, so a stream can be put on a TV from
the web app rather than from whichever device happens to be in the room.

The hand-off itself goes through `airplay_protocol`, not pyatv's `play_url`, because
modern receivers answer that older route with `501 Not Implemented`. What that
buys, besides working, is certainty about the two things this module used to have to
hedge against: the session is ours to hold, so playback lasts exactly as long as we
keep feeding it, and it ends when we say rather than whenever a connection happens to
drop.

Which receiver
--------------

There is more than one television in a house, so a receiver is chosen per hand-off
rather than fixed at import, and a session remembers which one took it. Sessions are
keyed by (source, receiver): the same stream can be running on two receivers at once,
and each is stopped on its own.

Discovery is unicast. Multicast mDNS does not survive the docker bridge, and on this
network it has never been shown to work at all, so `pyatv.scan` is always given
`hosts=[...]` and never left to browse. That leaves two ways to fill the list, and the
choice between them is deliberate:

* `PWS_AIRPLAY_HOST` names addresses, comma-separated. This is what a page load uses.
  It is a handful of unicast probes, it is cached for the life of the process, and it
  costs nothing on a LAN that never changes.
* `sweep()` looks at every address on a /24. It is how a receiver is found in the
  first place, but 254 probes is not a thing to do on every page load, so it
  never runs on one: it happens only when somebody asks for it. What it finds joins
  the cached list and is then indistinguishable from a configured address.

A paired receiver is also written down, beside the credentials, because otherwise a
redeploy loses exactly half of what pairing produced. pyatv's storage keys credentials
by device identifier and never records where the device was, so a container that comes
back up still holds the keys to the television and no longer knows its address -- and
the only way back is the sweep nobody wants to run twice. Remembered addresses are
probed like configured ones, which is what makes a redeploy cost a handful of packets
instead of 254. The web app probes them once at startup too, so the first page load
finds the answer already waiting and the boot log says what the house looks like.

A remembered receiver stays on the list whatever the scan says about it, and carries
what the scan said:

* it answered and is paired -- it can be handed a stream.
* it answered and is not paired -- the credentials are gone from the receiver's side,
  which is a thing a television does when it is reset. Saying so is the whole reason
  this is not quietly dropped: the fix is to pair it again, and a receiver that has
  vanished from the list tells nobody that.
* it said nothing -- off, asleep, or moved to another address. `seen` is false and no
  hand-off is offered, but it is still listed, because "the TV in the den is off" and
  "there is no TV in the den" are different facts.

So nothing is forgotten by a scan. `forget()` is how an entry leaves, and it is the
only way. It drops the address and stops probing for it; the credentials in pyatv's
storage are left where they are, since they are keyed by device and cost nothing, and a
later sweep that finds the television again will find it already paired.

Pairing
-------

Pairing is once per receiver, and pyatv's `FileStorage` keys credentials by device
identifier, so one file holds as many receivers as have been paired: the container's
`/config` volume, else `~/.config/play-web-stream/pyatv.conf`, else wherever
`PWS_ATV_STORAGE` says.

It cannot be one request: the receiver displays its PIN only after pairing has begun,
so the handler has to stay alive between being told to begin and being given the PIN.
It lives on the same long-lived loop the playback sessions use. An attempt that nobody
finishes is closed by `PAIR_TIMEOUT`, so an abandoned one does not wedge the next.

From a shell, the same thing in one step:

    python3 airplay.py pair 192.168.1.50
    python3 airplay.py scan 192.168.1.0/24
    python3 airplay.py forget 192.168.1.50
"""

import asyncio
import ipaddress
import json
import os
import resource
import sys
import threading
import time

try:
    import pyatv
    from pyatv.auth.hap_pairing import parse_credentials
    from pyatv.const import Protocol
    from pyatv.storage.file_storage import FileStorage

    import airplay_protocol
except ImportError:                                               # optional dependency
    pyatv = None

HOSTS = [host.strip() for host in os.environ.get("PWS_AIRPLAY_HOST", "").split(",")
         if host.strip()]
def _default_storage():
    """Where pyatv credentials live when nobody says.

    `/config` is the container's volume and is created by the image, so its presence
    is what tells the two deployments apart: inside, credentials belong on the volume
    that survives a rebuild; outside -- a checkout run straight from a shell -- there
    is no such directory and writing to one that does not exist is how pairing used to
    end. `PWS_ATV_STORAGE` still overrides both.
    """
    if os.path.isdir("/config"):
        return "/config/pyatv.conf"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "play-web-stream", "pyatv.conf")


STORAGE = os.environ.get("PWS_ATV_STORAGE") or _default_storage()

# Beside the credentials, and for the same reason: both halves of a pairing have to
# survive a rebuild, and pyatv's file holds only one of them.
REMEMBERED = (os.environ.get("PWS_RECEIVERS")
              or os.path.join(os.path.dirname(STORAGE) or ".", "receivers.json"))

# Pinned, because the receiver calls back on it and a bridged container can only
# publish a port it knows in advance. See airplay_protocol's module docstring.
TIMING_PORT = int(os.environ.get("PWS_AIRPLAY_TIMING_PORT", "49170"))

# PTP unless the receiver refuses it; see airplay_protocol's module docstring for why.
# Pinning one is for a receiver that accepts PTP and then misbehaves on it.
TIMING = os.environ.get("PWS_AIRPLAY_TIMING", "auto").strip().lower() or "auto"

FD_WANT = 8192              # descriptors to ask for: a /24 of sockets, and slack
FD_RESERVED = 128           # descriptors the rest of the process is assumed to want
FD_MIN_BATCH = 32           # smallest sweep batch worth the round trip it costs

SCAN_TIMEOUT = 8
SWEEP_TIMEOUT = 20          # a /24 at once, rather than a handful of named addresses
STEP_TIMEOUT = 20           # any single exchange with the receiver
START_WAIT = 45             # scan + connect + queue + first playback event
PAIR_TIMEOUT = 180          # how long a begun pairing waits for its PIN

_lock = threading.Lock()
_loop = None
_sessions = {}              # (source, address) -> {"session", "task", "device", "address"}

_receiver_lock = threading.Lock()
_receivers = None           # the merged rows `receivers()` hands out, or None
_extra = []                 # addresses learnt rather than configured, kept for later looks
_remembered = None          # [{"name", "address", "identifier"}] read back from REMEMBERED

_pair_lock = threading.Lock()
_pairing = None             # {"handler", "storage", "name", "address", "timer"}


def available():
    """Whether direct AirPlay can do anything at all.

    Configuration is no longer part of this. Pairing happens in the app now, and a
    receiver can be found by sweeping, so an unconfigured install is not a dead end --
    it is the state the pairing UI exists for. Only a missing pyatv is.
    """
    return pyatv is not None


def receivers(refresh=False):
    """What we know of, as [{"name", "address", "identifier", "paired", "seen",
    "remembered"}].

    Cached, because this is on the page-load path and a scan is a network round trip.
    `paired` says whether credentials for that device are in storage, which is the
    difference between a receiver that can be handed a stream and one that can only be
    paired. `seen` says whether it answered this scan at all: a remembered receiver
    that is switched off is reported rather than dropped, and for it `paired` is the
    last thing we knew rather than anything just established.
    """
    global _receivers
    if not available():
        return []
    with _receiver_lock:
        if _receivers is None or refresh:
            found = _look(_addresses(), SCAN_TIMEOUT)
            _keep(found)
            _receivers = _merge(found)
        return [dict(receiver) for receiver in _receivers]


def sweep(network):
    """Look at every address on `network`, and keep what answered.

    This is the slow, deliberate path: it is never run on a page load, only when
    somebody asks for it. Addresses already known are included even when they sit
    outside the network being swept, so asking again cannot lose one.
    """
    global _receivers, _extra
    if not available():
        raise RuntimeError("pyatv is not installed")
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError as exc:
        raise RuntimeError("%s is not a network to sweep" % (network or "(none)")) from exc
    if net.num_addresses > 256:
        raise RuntimeError("refusing to sweep %s: wider than a /24" % net)

    with _receiver_lock:
        addresses = [str(host) for host in net.hosts()] + _addresses()
    found = _look(addresses, SWEEP_TIMEOUT)
    with _receiver_lock:
        _extra = [receiver["address"] for receiver in found
                  if receiver["address"] not in HOSTS]
        _keep(found)
        _receivers = _merge(found)
        return [dict(receiver) for receiver in _receivers]


def remember(address):
    """Probe one address and keep it, for a receiver named rather than found.

    The picker only ever offers what discovery turned up, so this is the way an
    address that was typed -- on the command line -- gets onto that list at all.
    """
    if not available():
        raise RuntimeError("pyatv is not installed")
    with _receiver_lock:
        if address not in HOSTS and address not in _extra:
            _extra.append(address)
    return receivers(refresh=True)


def state(receiver):
    """The one thing worth saying about a row, in the order the answers matter.

    Public because the boot log says it too, and one wording for the shell, the log and
    the card is one fewer thing to keep in step.
    """
    if not receiver["seen"]:
        return "remembered, no answer"
    if receiver["paired"]:
        return "paired"
    return "no longer paired" if receiver["remembered"] else "not paired"


def forget(address):
    """Stop remembering a receiver, and stop probing for it.

    The only way an entry leaves the list, since a scan never removes one. What goes is
    the address: the credentials stay in pyatv's storage, keyed by device rather than
    by where it lives, so a sweep that turns the television up again turns it up
    already paired. An address that came from `PWS_AIRPLAY_HOST` cannot go at all --
    the next page load would read it straight back out of the environment -- so that is
    said plainly rather than half-done.

    The list afterwards is `receivers()`, not something returned from here: this drops
    a row from the cache but does not stand in for having one, and a caller on a
    process that has not scanned yet should get a scan rather than an empty answer.
    """
    global _receivers, _extra
    if not available():
        raise RuntimeError("pyatv is not installed")
    if address in HOSTS:
        raise RuntimeError("%s is named by PWS_AIRPLAY_HOST, so it cannot be forgotten "
                           "here; unset it there" % address)
    with _receiver_lock:
        kept = [entry for entry in _recall() if entry["address"] != address]
        if len(kept) == len(_recall()) and address not in _extra:
            raise RuntimeError("%s is not a receiver this app remembers"
                               % (address or "(none)"))
        _extra = [known for known in _extra if known != address]
        _store(kept)
        if _receivers is not None:
            _receivers = [receiver for receiver in _receivers
                          if receiver["address"] != address]
    return True


def status():
    """What is playing where, dropping sessions the receiver has finished with.

    The keepalive task ends for two reasons: the receiver reported the item stopped,
    or feeding it failed. Neither leaves anything worth showing.

    Keyed by source, but a list per source: the same stream may be on two receivers,
    and collapsing them would be the assumption this module used to make.
    """
    dead = []
    live = {}
    with _lock:
        for key in list(_sessions):
            if _sessions[key]["task"].done():
                dead.append(_sessions.pop(key))
        for (source, address), entry in _sessions.items():
            live.setdefault(source, []).append({"device": entry["device"],
                                                "address": address})

    for entry in dead:                        # outside the lock: this touches the loop
        _say_ended(entry)
        _reap(entry)
    return live


def _say_ended(entry):
    """Log why a session finished, because a stream ending is the thing we debug.

    The two endings need telling apart. A receiver reporting the item stopped has
    made a decision -- it ran out of media, or somebody picked up the remote. A
    feedback that raised is the session being lost underneath us. Until this was
    written the session simply vanished from the page and said neither.
    """
    task = entry["task"]
    if task.cancelled():
        return
    exc = task.exception()          # also retrieves it, so asyncio stops complaining
    why = "feeding it failed: %r" % exc if exc else "the receiver reported it stopped"
    print("%s airplay: %s on %s ended - %s"
          % (time.strftime("%H:%M:%S"), entry["address"], entry["device"], why),
          file=sys.stderr)


def start(source, url, address=None):
    """Put one stream on one receiver and return which device took it."""
    target = _target(address)

    key = (source, target["address"])
    with _lock:
        live = _sessions.get(key)
    if live and not live["task"].done():
        return {"device": live["device"], "address": key[1], "already": True}

    entry = _call(_begin(url, target), START_WAIT)
    with _lock:
        _sessions[key] = entry
    return {"device": entry["device"], "address": key[1]}


def stop(source, address=None):
    """Stop this stream on one receiver, or on every receiver playing it."""
    with _lock:
        keys = [key for key in _sessions
                if key[0] == source and address in (None, key[1])]
        entries = [_sessions.pop(key) for key in keys]
    for entry in entries:
        _discard(entry)
    return bool(entries)


# ----------------------------------------------------------------------------- pairing

def pair_begin(address):
    """Ask a receiver to display its PIN, and hold the handler open for it.

    Any attempt already in flight is closed first. Two half-finished pairings against
    one receiver would fight over the same PIN, and the earlier one is by definition
    the one nobody completed.
    """
    if not available():
        raise RuntimeError("pyatv is not installed")
    target = _known(address)
    pair_cancel()

    entry = _call(_pair_begin(target["address"]), SCAN_TIMEOUT + STEP_TIMEOUT)
    timer = threading.Timer(PAIR_TIMEOUT, _pair_expire)
    timer.daemon = True
    entry["timer"] = timer
    with _pair_lock:
        global _pairing
        _pairing = entry
    timer.start()
    return {"pairing": True, "device": entry["name"], "address": entry["address"]}


def pair_finish(pin):
    """Give the receiver the PIN the person read off the television."""
    with _pair_lock:
        global _pairing
        entry = _pairing
        _pairing = None
    if entry is None:
        raise RuntimeError("no pairing is waiting for a PIN; begin one first")
    entry["timer"].cancel()

    if not (pin or "").strip():
        _discard_pairing(entry)
        raise RuntimeError("the receiver is waiting for the PIN it displayed")
    try:
        _call(_pair_finish(entry, pin.strip()), STEP_TIMEOUT * 2)
    except Exception as exc:                                      # noqa: BLE001
        raise RuntimeError("pairing with %s failed: %s" % (entry["name"], exc)) from exc

    _protect_storage()
    receivers(refresh=True)         # `paired` has just changed for this one
    return {"paired": True, "device": entry["name"], "address": entry["address"]}


def pair_cancel():
    """Drop a begun pairing, so the next attempt starts from a clean receiver."""
    with _pair_lock:
        global _pairing
        entry = _pairing
        _pairing = None
    if entry is None:
        return False
    entry["timer"].cancel()
    _discard_pairing(entry)
    return True


def pairing():
    """The receiver waiting for a PIN, if one is, so a reload can pick the flow back up."""
    with _pair_lock:
        if _pairing is None:
            return None
        return {"device": _pairing["name"], "address": _pairing["address"]}


# --------------------------------------------------------------------------- internals

def _get_loop():
    """One long-lived loop in a daemon thread.

    The session outlives the request that made it -- something has to keep feeding the
    receiver -- so it cannot live in a per-request asyncio.run(). Pairing needs the
    same thing for the same reason: its handler spans two requests.
    """
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
        return _loop


def _call(coro, timeout):
    """Run a coroutine on the shared loop from a request thread."""
    return asyncio.run_coroutine_threadsafe(coro, _get_loop()).result(timeout)


def _look(addresses, timeout):
    """Scan the addresses given, or nothing at all when there are none.

    The empty case matters: pyatv reads `hosts=[]` as "browse", which is the multicast
    scan this module exists without.

    A sweep is 254 addresses and pyatv holds a socket open for each one at the same
    time, which is more descriptors than a process gets by default on macOS, where the
    soft limit is 256 and the sweep dies with `[Errno 24] Too many open files`. So the
    limit is raised where that is allowed, and the scan is run in batches that fit
    inside whatever the limit turned out to be. Batching costs wall clock -- each
    batch waits out `timeout` -- which is why the limit is raised first and the
    batches are as large as the descriptors allow.
    """
    unique = list(dict.fromkeys(addresses))
    if not unique:
        return []
    batch = _fd_batch()
    seen = {}
    for start in range(0, len(unique), batch):
        for receiver in _call(_scan(unique[start:start + batch], timeout), timeout + 10):
            seen[receiver["address"]] = receiver
    return sorted(seen.values(), key=lambda receiver: receiver["name"].lower())


def _fd_batch():
    """How many addresses one scan may hold sockets for.

    The soft limit is raised towards the hard one first, which on macOS turns 256 into
    something a whole /24 fits inside; a sandbox that refuses keeps its limit and gets
    smaller batches instead of a failed sweep. Half the headroom is left alone: the
    proxies, the server's own sockets and pyatv's internals are all spending
    descriptors out of the same budget.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = FD_WANT if hard == resource.RLIM_INFINITY else min(FD_WANT, hard)
    if soft < want:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            soft = want
        except (ValueError, OSError):                    # a limit we are not allowed
            pass
    return max(FD_MIN_BATCH, min(256, (soft - FD_RESERVED) // 2))


def _addresses():
    """Every address worth a probe: configured, learnt this run, remembered from a past one.

    Read under `_receiver_lock`, since two of the three can be rewritten by a sweep.
    """
    return list(dict.fromkeys(HOSTS + _extra
                              + [entry["address"] for entry in _recall()]))


def _recall():
    """The remembered receivers, read back from disk once per process."""
    global _remembered
    if _remembered is None:
        _remembered = _read()
    return _remembered


def _keep(found):
    """Write down what a scan turned up, so the next deployment starts knowing it.

    Pairing is what earns an address its line in the file: an unpaired receiver can do
    nothing for us that a sweep could not establish again. An address already in the
    file keeps its line whatever the scan said, and only has its name brought up to
    date -- a receiver losing its credentials is news to report, not a reason to lose
    the address as well. `forget()` is the way out.
    """
    kept = {entry["address"]: entry for entry in _recall()}
    for receiver in found:
        if receiver["paired"] or receiver["address"] in kept:
            kept[receiver["address"]] = {"name": receiver["name"],
                                         "address": receiver["address"],
                                         "identifier": receiver["identifier"]}
    _store(list(kept.values()))


def _merge(found):
    """One list out of the scan and the file, each row saying where it came from.

    A remembered receiver that did not answer is carried over as it was last known,
    with `seen` false so that nothing offers to hand it a stream. Everything that did
    answer is reported as the scan found it, `paired` included -- which is how a
    television that has forgotten us becomes visible instead of merely absent.
    """
    remembered = {entry["address"]: entry for entry in _recall()}
    rows = [dict(receiver, seen=True,
                 remembered=receiver["address"] in remembered) for receiver in found]
    answered = {receiver["address"] for receiver in found}
    rows += [dict(entry, paired=True, seen=False, remembered=True)
             for address, entry in remembered.items() if address not in answered]
    return sorted(rows, key=lambda receiver: (receiver["name"].lower(),
                                              receiver["address"]))


def _read():
    """Whatever the file holds, or nothing.

    Anything unreadable is nothing: this is a convenience that saves a sweep, so a file
    that has been truncated or hand-edited into nonsense costs one sweep rather than a
    process that will not start. Entries are rebuilt field by field for the same reason.
    """
    try:
        with open(REMEMBERED) as handle:
            saved = json.load(handle).get("receivers")
    except (OSError, ValueError, AttributeError):
        return []
    entries = []
    for entry in saved if isinstance(saved, list) else []:
        address = entry.get("address") if isinstance(entry, dict) else None
        if address:
            entries.append({"name": entry.get("name") or address,
                            "address": address,
                            "identifier": entry.get("identifier") or ""})
    return entries


def _store(entries):
    """Replace the file, when there is something new to say.

    Written whole and renamed into place, because a page load reads it and a half
    written list is worse than a stale one. A volume that will not take it is not worth
    failing a scan over -- the addresses are still live in `_extra` for this process,
    and the next deployment is no worse off than it was before the file existed.
    """
    global _remembered
    entries = sorted(entries, key=lambda entry: (entry["name"].lower(), entry["address"]))
    if entries == _remembered:
        return
    _remembered = entries
    try:
        os.makedirs(os.path.dirname(REMEMBERED) or ".", mode=0o700, exist_ok=True)
        pending = REMEMBERED + ".new"
        with open(pending, "w") as handle:
            json.dump({"receivers": entries}, handle, indent=2)
            handle.write("\n")
        os.replace(pending, REMEMBERED)
    except OSError:                                   # read-only, or not ours to make
        pass


def _protect_storage():
    """Keep the credentials file to ourselves, from the moment it exists.

    `_storage()` tightens a file that is already there, which is no help the first
    time pyatv writes one: it lands with whatever the umask allows and stays that way
    until the next scan happens to tighten it. So the path that creates it says so too.
    """
    try:
        os.chmod(STORAGE, 0o600)
    except OSError:                 # not ours, or not there -- load() will complain
        pass


async def _storage(loop):
    """pyatv's credential store, loaded, with somewhere to save itself to.

    The directory is made here rather than at import: this is the only path that
    writes, and a sweep on a machine that never pairs has no business creating
    anything. A store that cannot be created is left to `load()` to complain about.
    """
    try:
        os.makedirs(os.path.dirname(STORAGE) or ".", mode=0o700, exist_ok=True)
    except OSError:                                   # read-only, or not ours to make
        pass
    if os.path.exists(STORAGE):
        _protect_storage()                            # credentials, not world readable
    storage = FileStorage(STORAGE, loop)
    await storage.load()
    return storage


async def _scan(addresses, timeout):
    loop = asyncio.get_running_loop()
    storage = await _storage(loop)
    found = await pyatv.scan(loop, timeout=timeout, hosts=addresses,
                             protocol=Protocol.AirPlay, storage=storage)
    seen = []
    for conf in found:
        service = conf.get_service(Protocol.AirPlay)
        if service is None:                   # answered, but not as an AirPlay receiver
            continue
        seen.append({"name": conf.name or str(conf.address),
                     "address": str(conf.address),
                     "identifier": conf.identifier or "",
                     "paired": bool(service.credentials)})
    return sorted(seen, key=lambda receiver: receiver["name"].lower())


def _known(address):
    """The receiver at `address`, which has to be one we have actually found.

    The endpoints in front of this are already LAN-only, but that guards who may ask,
    not where the request lands. Pairing reaches further than casting does, since it
    writes credentials, so it is held to the list too.
    """
    for receiver in receivers():
        if receiver["address"] == address:
            return receiver
    raise RuntimeError("%s is not a receiver this app has found"
                       % (address or "(none)"))


def _target(address):
    """Which receiver a hand-off is for: the one named, or the only one there is.

    A remembered receiver that did not answer the last scan is not a candidate for
    either. It is on the list to be reported, not to be played to, and its `paired` is
    a memory rather than a fact.
    """
    if not available():
        raise RuntimeError("direct AirPlay is off; install pyatv")
    if address:
        target = _known(address)
        if not target["seen"]:
            raise RuntimeError("%s did not answer; it may be off, or moved"
                               % target["name"])
        if not target["paired"]:
            raise RuntimeError("%s is not paired yet" % target["name"])
        return target

    paired = [receiver for receiver in receivers()
              if receiver["paired"] and receiver["seen"]]
    if not paired:
        raise RuntimeError("no AirPlay receiver is paired; pair one first")
    if len(paired) > 1:
        raise RuntimeError("several receivers are paired; say which one")
    return paired[0]


async def _find(loop, storage, address):
    """One receiver by address, with whatever credentials we hold for it."""
    found = await pyatv.scan(loop, timeout=SCAN_TIMEOUT, hosts=[address],
                             protocol=Protocol.AirPlay, storage=storage)
    if not found:
        raise RuntimeError("no AirPlay receiver answered at %s" % address)
    conf = found[0]
    service = conf.get_service(Protocol.AirPlay)
    if service is None:
        raise RuntimeError("%s does not offer AirPlay" % address)
    return conf, service


async def _begin(url, target):
    loop = asyncio.get_running_loop()
    storage = await _storage(loop)

    address = target["address"]
    conf, service = await _find(loop, storage, address)
    if not service.credentials:
        raise RuntimeError("no AirPlay credentials for %s; pair it first" % conf.name)

    if TIMING not in airplay_protocol.TIMINGS:
        raise RuntimeError("PWS_AIRPLAY_TIMING is %r, which is none of %s"
                           % (TIMING, ", ".join(airplay_protocol.TIMINGS)))
    session = airplay_protocol.AirPlaySession(
        address, service.port, parse_credentials(service.credentials), TIMING_PORT,
        TIMING)
    try:
        await session.play(url, STEP_TIMEOUT)
    except BaseException as exc:
        session.close()
        # Named, because pairing succeeding says nothing about the hand-off working:
        # a receiver that pairs happily and then refuses this is a thing that happens,
        # and one dead button for the house would not say which television it was.
        if isinstance(exc, Exception):
            raise RuntimeError("%s would not take the stream: %s" % (conf.name, exc)) from exc
        raise

    return {"session": session, "device": conf.name, "address": address,
            "task": loop.create_task(session.keepalive())}


async def _end(entry):
    entry["task"].cancel()
    await entry["session"].halt(STEP_TIMEOUT)


def _reap(entry):
    """Drop a session the receiver has already finished with.

    Nothing is asked of the receiver here, so nothing is waited on: this runs on the
    streams poll, and a receiver that has gone quiet must not stall the whole page.
    """
    entry["task"].cancel()
    _get_loop().call_soon_threadsafe(entry["session"].close)


def _discard(entry):
    """Tear a session down, never raising: callers are already on their way out."""
    try:
        _call(_end(entry), STEP_TIMEOUT + 5)
    except Exception:                                             # noqa: BLE001
        _reap(entry)


async def _pair_begin(address):
    loop = asyncio.get_running_loop()
    storage = await _storage(loop)

    conf, _ = await _find(loop, storage, address)
    handler = await pyatv.pair(conf, Protocol.AirPlay, loop, storage=storage)
    try:
        await handler.begin()
        if not handler.device_provides_pin:
            raise RuntimeError("%s does not display a PIN, so it cannot be paired here"
                               % conf.name)
    except BaseException:
        await handler.close()
        raise
    return {"handler": handler, "storage": storage, "name": conf.name,
            "address": address, "timer": None}


async def _pair_finish(entry, pin):
    handler = entry["handler"]
    try:
        handler.pin(pin)
        await handler.finish()
        if not handler.has_paired:
            raise RuntimeError("the receiver refused it")
        await entry["storage"].save()
    finally:
        await handler.close()


def _pair_expire():
    """Close an attempt nobody finished, rather than holding the receiver forever."""
    with _pair_lock:
        global _pairing
        entry = _pairing
        _pairing = None
    if entry is not None:
        _discard_pairing(entry)


def _discard_pairing(entry):
    """Close a pairing handler, never raising: callers are already on their way out."""
    try:
        _call(entry["handler"].close(), STEP_TIMEOUT)
    except Exception:                                             # noqa: BLE001
        pass


# --------------------------------------------------------------------------------- cli

def _pair_interactive(address):
    """The two-step pairing with a terminal standing in for the web UI."""
    try:
        begun = pair_begin(address)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("pairing with %s" % begun["device"])
    try:
        done = pair_finish(input("PIN shown on the receiver: "))
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        pair_cancel()
        print("\ncancelled", file=sys.stderr)
        return 1
    print("paired %s; credentials saved to %s" % (done["device"], STORAGE))
    return 0


def _report(found):
    if not found:
        print("nothing answered", file=sys.stderr)
        return 1
    for receiver in found:
        print("%-16s %-28s %s" % (receiver["address"], receiver["name"],
                                  state(receiver)))
    return 0


def main(argv):
    if pyatv is None:
        print("pyatv is not installed", file=sys.stderr)
        return 1
    if len(argv) == 2 and argv[0] == "pair":
        # An address typed here has not been discovered, and `pair_begin` only pairs
        # what discovery found. Probing it is what puts it on that list.
        try:
            remember(argv[1])
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        return _pair_interactive(argv[1])
    if argv and argv[0] == "scan":
        if len(argv) == 2:
            try:
                return _report(sweep(argv[1]))
            except RuntimeError as exc:
                print(exc, file=sys.stderr)
                return 1
        return _report(receivers(refresh=True))
    if len(argv) == 2 and argv[0] == "forget":
        try:
            forget(argv[1])
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        print("forgot %s; its credentials are still in %s" % (argv[1], STORAGE))
        return 0
    print("usage: airplay.py pair <receiver-ip>\n"
          "       airplay.py scan [network/24]\n"
          "       airplay.py forget <receiver-ip>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
