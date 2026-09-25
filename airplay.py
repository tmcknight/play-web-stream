#!/usr/bin/env python3
"""Hand a stream to an AirPlay receiver directly, without Safari.

AirPlay video does not restream: the receiver is given a URL and fetches it. The proxy
binds all interfaces and advertises a LAN address for this reason, so Safari only acts
as a remote control. This module replaces it, so the web app can put a stream on a TV.

The hand-off goes through `airplay_protocol` because modern receivers answer pyatv's
`play_url` with `501 Not Implemented`. Holding the session ourselves also means playback
lasts as long as we keep feeding it, and ends when we stop it, not when a connection
drops.

Which receiver
--------------

A house can have several TVs, so the receiver is chosen per hand-off. Sessions are keyed
by (source, receiver): one stream can play on two receivers and each stops separately.

Discovery is unicast. Multicast mDNS does not cross the docker bridge and has never
worked on this network, so `pyatv.scan` always gets `hosts=[...]`. The list is filled
two ways:

* `PWS_AIRPLAY_HOST`: comma-separated addresses, probed on page load and cached for the
  life of the process.
* `sweep()`: probes every address on a /24. Too slow (254 probes) for a page load, so it
  runs only on request. What it finds joins the cached list.

Paired receivers are also saved beside the credentials. pyatv keys credentials by device
identifier and does not store addresses, so after a redeploy the container would hold
the keys but not know where the TV is, and would need another sweep. Saved addresses are
probed like configured ones. The web app probes them at startup too, so the first page
load is fast and the boot log shows which TVs answered.

A remembered receiver stays listed whatever the scan says, with its status:

* answered and paired: it can take a stream.
* answered and not paired: the TV lost its credentials, usually after a reset. It stays
  listed so the user knows to pair it again.
* no answer: off, asleep, or moved. `seen` is false and no hand-off is offered. It stays
  listed because "the TV is off" and "there is no TV" are different.

A scan never removes an entry; only `forget()` does. That drops the address and stops
probing it. The credentials stay in pyatv's storage (keyed by device), so a later sweep
finds the TV already paired.

Pairing
-------

Pairing is once per receiver. pyatv's `FileStorage` keys credentials by device, so one
file holds every paired receiver: `PWS_ATV_STORAGE` if set, else the container's
`/config` volume, else `~/.config/play-web-stream/pyatv.conf`.

It takes two requests, because the receiver shows its PIN only after pairing begins. The
handler waits on the same long-lived loop as the playback sessions, and `PAIR_TIMEOUT`
closes an abandoned attempt so it does not block the next.

From a shell, in one step:

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

    The image creates `/config`, so its presence means we are in the container and
    credentials go on the volume that survives a rebuild. Outside the container there
    is no `/config`, and writing there used to make pairing fail. `PWS_ATV_STORAGE`
    overrides both.
    """
    if os.path.isdir("/config"):
        return "/config/pyatv.conf"
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "play-web-stream", "pyatv.conf")


STORAGE = os.environ.get("PWS_ATV_STORAGE") or _default_storage()

# Beside the credentials, so addresses survive a rebuild too. pyatv's file does not
# store them.
REMEMBERED = (os.environ.get("PWS_RECEIVERS")
              or os.path.join(os.path.dirname(STORAGE) or ".", "receivers.json"))

# Pinned, because the receiver calls back on it and a bridged container can only
# publish a port it knows in advance. See airplay_protocol's module docstring.
TIMING_PORT = int(os.environ.get("PWS_AIRPLAY_TIMING_PORT", "49170"))

# PTP unless the receiver refuses it (see airplay_protocol's docstring). Set it for a
# receiver that accepts PTP and then misbehaves on it.
TIMING = os.environ.get("PWS_AIRPLAY_TIMING", "auto").strip().lower() or "auto"

FD_WANT = 8192              # descriptors to ask for: a /24 of sockets, and slack
FD_RESERVED = 128           # descriptors the rest of the process is assumed to want
FD_MIN_BATCH = 32           # smallest sweep batch; each batch waits a full timeout

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
    """Whether direct AirPlay is usable, i.e. pyatv is installed.

    No configuration is needed: receivers can be found by sweeping and paired in the
    app.
    """
    return pyatv is not None


def receivers(refresh=False):
    """What we know of, as [{"name", "address", "identifier", "paired", "seen",
    "remembered"}].

    Cached, because this runs on page load and a scan is a network round trip.
    `paired` means credentials for the device are in storage, so it can take a stream.
    `seen` means it answered this scan. For a remembered receiver that is switched off,
    `paired` is the last known value.
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

    The slow path, run only on request. Known addresses outside `network` are
    included too, so a sweep cannot lose one.
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

    The picker only offers discovered receivers, so this is how an address typed on
    the command line gets onto the list.
    """
    if not available():
        raise RuntimeError("pyatv is not installed")
    with _receiver_lock:
        if address not in HOSTS and address not in _extra:
            _extra.append(address)
    return receivers(refresh=True)


def state(receiver):
    """A row's status as a short phrase.

    Public so the shell, the boot log and the UI card share one wording.
    """
    if not receiver["seen"]:
        return "remembered, no answer"
    if receiver["paired"]:
        return "paired"
    return "no longer paired" if receiver["remembered"] else "not paired"


def forget(address):
    """Stop remembering a receiver, and stop probing for it.

    A scan never removes an entry, so this is the only way. Only the address goes: the
    credentials stay in pyatv's storage, keyed by device, so a later sweep finds the TV
    already paired. An address from `PWS_AIRPLAY_HOST` cannot be forgotten, since the
    next page load would read it back from the environment, so that raises.

    Call `receivers()` for the updated list. This drops a row from the cache but does
    not fill it, and a process that has not scanned yet should scan, not get an empty
    list.
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

    The keepalive task ends when the receiver reports the item stopped or feeding it
    fails. Either way the session is dropped.

    Keyed by source, with a list per source, since one stream may be on two receivers.
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
    """Log why a session finished, since streams ending is what we debug most.

    A receiver reporting the item stopped means it ran out of media or someone used the
    remote. A feedback call that raised means the session was lost. Before this, the
    session vanished from the page with no reason logged.
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

    Any attempt in flight is closed first. Two pairings against one receiver would
    fight over the same PIN, and the earlier one was abandoned.
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

    A session outlives its request (the receiver must be kept fed), so it cannot use a
    per-request asyncio.run(). A pairing handler spans two requests for the same
    reason.
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

    pyatv reads `hosts=[]` as "browse", i.e. a multicast scan, so the empty case
    returns early.

    A sweep holds 254 sockets open at once. macOS's default soft limit is 256, so the
    sweep died with `[Errno 24] Too many open files`. The limit is raised where allowed
    and the scan runs in batches that fit. Each batch waits out `timeout`, so batches
    are as large as the limit allows.
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

    Raises the soft limit towards the hard one first, which on macOS fits a whole /24.
    If a sandbox refuses, batches get smaller instead of the sweep failing. Half the
    headroom is left for the proxies, the server's sockets and pyatv itself.
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
    """Every address to probe: configured, learnt this run, and remembered.

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
    """Save what a scan found, so the next deployment starts with it.

    Only paired receivers are added; an unpaired one can be found again by a sweep. An
    address already in the file stays whatever the scan said, with its name updated,
    so a receiver that lost its credentials is still reported. `forget()` removes it.
    """
    kept = {entry["address"]: entry for entry in _recall()}
    for receiver in found:
        if receiver["paired"] or receiver["address"] in kept:
            kept[receiver["address"]] = {"name": receiver["name"],
                                         "address": receiver["address"],
                                         "identifier": receiver["identifier"]}
    _store(list(kept.values()))


def _merge(found):
    """Merge the scan with the file, each row marked with where it came from.

    A remembered receiver that did not answer keeps its last known state, with `seen`
    false so no hand-off is offered. Receivers that answered are reported as scanned,
    including `paired`, so a TV that lost its pairing shows as unpaired.
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

    The file only saves a sweep, so a truncated or mangled file is treated as empty
    and costs one sweep, not a failed start. Entries are rebuilt field by field for the
    same reason.
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

    Written to a temp file and renamed, because a page load reads it and a half-written
    list is worse than a stale one. A write failure does not fail the scan: `_extra`
    still holds the addresses for this process.
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

    `_storage()` only tightens an existing file. The first time pyatv writes one it
    gets the umask's permissions, so pairing calls this after saving.
    """
    try:
        os.chmod(STORAGE, 0o600)
    except OSError:                 # not ours, or not there; load() will report it
        pass


async def _storage(loop):
    """pyatv's credential store, loaded, with somewhere to save itself to.

    The directory is made here, not at import, so a machine that never pairs gets no
    directory. If it cannot be created, `load()` reports the error.
    """
    try:
        os.makedirs(os.path.dirname(STORAGE) or ".", mode=0o700, exist_ok=True)
    except OSError:                                   # read-only, or not ours to make
        pass
    if os.path.exists(STORAGE):
        _protect_storage()
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
    """The receiver at `address`, which must be one we have found.

    The endpoints are LAN-only, but that limits who asks, not which address is
    contacted. Pairing writes credentials, so it is limited to discovered receivers.
    """
    for receiver in receivers():
        if receiver["address"] == address:
            return receiver
    raise RuntimeError("%s is not a receiver this app has found"
                       % (address or "(none)"))


def _target(address):
    """Which receiver a hand-off is for: the one named, or the only one there is.

    A remembered receiver that did not answer the last scan is excluded: it is listed
    for reporting only, and its `paired` value may be stale.
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
        # Name the receiver: some pair fine and then refuse the hand-off, and the
        # error should say which TV.
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

    Sends nothing to the receiver and waits on nothing, because this runs on the
    streams poll and an unresponsive receiver must not stall the page.
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
    """Close an abandoned pairing attempt."""
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
    """The two-step pairing, driven from a terminal."""
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
        # `pair_begin` only pairs discovered receivers, so probe the typed address first.
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
