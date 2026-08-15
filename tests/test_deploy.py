"""Deploy tier — the exact serve command the container runs.

Everything else in the suite talks to the Flask test client, which never
touches gunicorn, its config file, or the `minibadge_designer.webapp:app`
import string in the Dockerfile CMD.  A typo in any of those ships a
container that builds green and then 502s for every user at once — and
gunicorn config errors are runtime-only by design (the first version of
gunicorn.conf.py hardcoded the Linux-only /dev/shm heartbeat dir and died
instantly on macOS; nothing but actually starting the server sees that).
"""

import http.client
import json
import socket
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pytest

pytest.importorskip("gunicorn", reason="deploy dep: pip install -e '.[dev]'")

REPO = Path(__file__).resolve().parent.parent
APP = "minibadge_designer.webapp:app"  # the WSGI entry the Dockerfile names
STARTUP_TIMEOUT_S = 20   # cold gunicorn master + 2 workers; measured ~1 s
REQUEST_BUDGET_S = 30    # two small boards; a hang here is a hang for users


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post_generate(port: int, name: str):
    """One user's download, over real HTTP: returns (status, zip names)."""
    params = json.dumps({"name": name, "leds": [
        {"x": 7, "y": 6, "color": "red", "size": "1206"}]})
    body = ("--BB\r\nContent-Disposition: form-data; name=\"params\"\r\n\r\n"
            f"{params}\r\n--BB--\r\n").encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=REQUEST_BUDGET_S)
    conn.request("POST", "/generate", body,
                 {"Content-Type": "multipart/form-data; boundary=BB"})
    resp = conn.getresponse()
    data = resp.read()
    if resp.status != 200:
        return resp.status, data[:200]
    return resp.status, sorted(zipfile.ZipFile(BytesIO(data)).namelist())


@pytest.mark.slow  # spawns a real gunicorn master + workers (~2 s)
def test_the_container_serve_command_answers_simultaneous_users(tmp_path):
    """`gunicorn -c gunicorn.conf.py minibadge_designer.webapp:app` — the
    Dockerfile CMD verbatim — starts, and two users generating at the same
    moment each receive their own board.

    WEB_CONCURRENCY=2 keeps the test light while still crossing the
    process boundary that production relies on for request isolation.
    """
    port = _free_port()
    env = {"PORT": str(port), "WEB_CONCURRENCY": "2",
           "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    with subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "-c", str(REPO / "gunicorn.conf.py"),
         APP],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    ) as srv:
        # Popen.__exit__ waits for the process, and gunicorn never exits on
        # its own — a failure that skipped terminate() would hang the suite.
        try:
            deadline = time.monotonic() + STARTUP_TIMEOUT_S
            while time.monotonic() < deadline:
                if srv.poll() is not None:
                    pytest.fail(
                        "the serve command died on startup — the hosted "
                        "container would 502 for everyone:\n"
                        + srv.stderr.read().decode("utf-8", "replace")[-800:])
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                pytest.fail(f"gunicorn did not listen within {STARTUP_TIMEOUT_S}s")

            started = time.monotonic()
            with ThreadPoolExecutor(2) as ex:
                a, b = ex.map(lambda n: _post_generate(port, n), ["alice", "bob"])
            elapsed = time.monotonic() - started

            assert a[0] == 200 and b[0] == 200, (a, b)
            assert "alice/alice.kicad_pcb" in a[1], (
                f"alice received someone else's project: {a[1]}")
            assert "bob/bob.kicad_pcb" in b[1], (
                f"bob received someone else's project: {b[1]}")
            assert elapsed <= REQUEST_BUDGET_S, (
                f"simultaneous downloads took {elapsed:.1f}s — users are queueing")
        finally:
            srv.terminate()


def test_the_dockerfile_cmd_is_the_command_the_smoke_test_proves():
    """The smoke test above launches gunicorn by hand; this pins the
    Dockerfile CMD to that same command, so the container cannot silently
    drift to serving something the suite never started."""
    cmd_lines = [l for l in (REPO / "Dockerfile").read_text().splitlines()
                 if l.startswith("CMD ")]
    assert len(cmd_lines) == 1, cmd_lines
    for token in ("gunicorn", "gunicorn.conf.py", APP):
        assert token in cmd_lines[0], (
            f"Dockerfile CMD no longer carries {token!r} — the deploy smoke "
            f"test is proving a command the container does not run: "
            f"{cmd_lines[0]}")
