# Contributing

Issues and pull requests are welcome. This is a personal project, so expect a slow and
occasionally silent maintainer rather than a triage rota.

## Running the checks

```sh
pip install -r requirements-dev.txt
python3 -m pytest -q          # or: python3 hls_proxy.py --self-test
ruff check .
```

CI runs the suite on Python 3.9 through 3.14 and ruff over everything, so a change that
only passes on the interpreter you happen to have will come back.

`tests/origin.py` is a fake origin with every awkward behaviour this code exists to cope
with as a switch: a Referer gate, a gate on the client itself, segments served as
`text/plain`, presigned URLs that expire, byte ranges, and a window that slides. Most of
the suite runs a real proxy as a subprocess against it and asserts on the wire, because
that is where the failures live — a stream that resolves cleanly and then 403s on every
segment looks perfectly healthy from inside the process. Prefer adding a switch there
over mocking the network.

Some tests skip without `curl_cffi` or `pyatv` installed. That is expected from a bare
environment; `requirements-dev.txt` pulls both in.

## Two constraints worth knowing before you start

**`hls_proxy.py` is standard library only.** It is a single file that runs from anywhere
with nothing installed, and the Claude Code skill depends on that — it is dropped onto
whatever Python a machine already has. `curl_cffi` is reached for only behind an
`ImportError` guard, and its absence has to leave the old behaviour intact. Anything that
needs a dependency outright belongs in `webapp.py` or one of the modules beside it.

**`pyatv` is pinned.** `airplay_protocol.py` reaches past its public API, so a version
bump has to be read rather than taken.

## Style

Comments and docstrings explain *why*, in prose, in sentences. The existing ones are the
specification for this — read a few before writing more. A comment that restates the line
below it is worse than no comment. Names and messages are written for whoever is reading
a log at eleven at night.

Line length is 100. Ruff's config in `pyproject.toml` is the arbiter of everything else;
the three ignores there are deliberate and explained in place.

## What is out of scope

DRM services, DASH, and anything that amounts to circumventing an access control rather
than correcting a `Content-Type`. See the README's [Scope](README.md#scope). Features
that would need the app to be safely reachable from the public internet are also out —
see `SECURITY.md` for why the LAN boundary is load-bearing rather than incidental.
