"""Fixtures: the repo on the path, a fake origin, and a real proxy subprocess.

The proxy runs as a detached child, as the web app runs it, and is read back through its
log. Much of what needs testing is how it behaves on the wire.
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import airplay  # noqa: E402
import hls_proxy  # noqa: E402
from origin import FakeOrigin  # noqa: E402

START_TIMEOUT = 30


@pytest.fixture(autouse=True)
def remembered_receivers(tmp_path, monkeypatch):
    """Point the remembered-receivers file somewhere disposable, for every test.

    The default is a real path in the runner's home, and `airplay` writes to it whenever
    a scan finds a paired receiver. A fake television must not end up in the file a real
    deployment on this machine reads.
    """
    monkeypatch.setattr(airplay, "REMEMBERED", str(tmp_path / "receivers.json"))
    monkeypatch.setattr(airplay, "_remembered", None)


def http(url, headers=None, method="GET", data=None):
    """Fetch without raising on a 4xx, since the status is usually the assertion.

    `data` is a JSON body; the web app's POST routes take nothing else.
    """
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, dict(exc.headers), exc.read()


class Proxy:
    """A running hls_proxy, addressed over loopback."""

    def __init__(self, base, log, source):
        self.base = base
        self.log = log
        self.source = source

    def get(self, path, headers=None, method="GET"):
        return http(self.base + path, headers, method)

    def text(self, path):
        status, _, body = self.get(path)
        assert status == 200, "%s returned %d" % (path, status)
        return body.decode()

    def routes(self, path, kind):
        """The /seg/ or /pl/ paths a playlist points at, in order."""
        return re.findall(r"/%s/[A-Za-z0-9_=.-]+" % kind, self.text(path))


@pytest.fixture
def origin():
    server = FakeOrigin()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def start_proxy(tmp_path):
    """Launch proxies and tear them all down afterwards."""
    running = []

    def start(source, referer=None, *args):
        log = tmp_path / ("proxy-%d.log" % len(running))
        cmd = [sys.executable, str(ROOT / "hls_proxy.py"), "--source", source,
               "--idle-timeout", "0", "--no-reuse"]
        if referer:
            cmd += ["--referer", referer]
        cmd += [str(a) for a in args]

        handle = open(log, "wb")        # noqa: SIM115 (lives as long as the child)
        proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT, cwd=str(ROOT))
        running.append((proc, handle, source))

        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            text = log.read_text(errors="replace") if log.exists() else ""
            match = re.search(r"serving : (http://\S+?)/?\n", text)
            if match:
                parts = urllib.parse.urlsplit(match.group(1))
                return Proxy("http://127.0.0.1:%d%s" % (parts.port, parts.path), log, source)
            if proc.poll() is not None:
                raise RuntimeError("the proxy exited:\n" + text)
            time.sleep(0.15)
        raise RuntimeError("the proxy never started:\n" + log.read_text(errors="replace"))

    yield start

    for proc, handle, source in running:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()
        # SIGTERM skips the proxy's own cleanup, so the state file is ours to remove.
        try:
            os.unlink(hls_proxy.state_path(source))
        except OSError:
            pass
