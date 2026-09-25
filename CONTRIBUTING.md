# Contributing

Issues and pull requests are welcome. This is a personal project, so replies may be slow.

## Running the checks

```sh
pip install -r requirements-dev.txt
python3 -m pytest -q          # or: python3 hls_proxy.py --self-test
ruff check .
```

CI runs the suite on Python 3.9 to 3.14, plus ruff, so test beyond your local Python.

`tests/origin.py` is a fake origin with each awkward behaviour as a switch: a Referer
gate, a gate on the client, segments served as `text/plain`, expiring presigned URLs,
byte ranges and a sliding window. Most tests run a real proxy as a subprocess against it
and check the wire, because a stream that 403s on every segment can look fine from inside
the process. Add a switch to `origin.py` instead of mocking the network.

Some tests skip without `curl_cffi` or `pyatv`. `requirements-dev.txt` installs both.

## Two constraints

**`hls_proxy.py` is standard library only.** It is a single file that runs anywhere with
nothing installed, and the Claude Code skill relies on that: it is copied onto whatever
Python a machine has. `curl_cffi` is only used behind an `ImportError` guard, and without
it the old behaviour must stay intact. Anything needing a hard dependency goes in
`webapp.py` or a module beside it.

**`pyatv` is pinned.** `airplay_protocol.py` uses its internals, so read the changes
before bumping the version.

## Style

Comments and docstrings explain *why*, briefly, in plain sentences. Don't write comments
that restate the code. Log lines and error messages should be clear to someone debugging
a broken stream.

Line length is 100. Ruff's config in `pyproject.toml` covers the rest; each of its three
ignores has a comment explaining it.

## What is out of scope

DRM services, DASH, and anything that gets around an access control instead of
correcting a `Content-Type`. See the README's [Scope](README.md#scope). Features that
would need the app to be safely reachable from the public internet are also out. See
`SECURITY.md` for why the LAN boundary matters.
