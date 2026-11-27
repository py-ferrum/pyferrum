"""Verify handler timeout and graceful SIGTERM shutdown behavior."""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

APP_SRC = """
import asyncio
from pyferrum import Ferrum
from pyferrum import Response

app = Ferrum()

@app.get("/timeout")
async def timeout(request):
    try:
        await asyncio.sleep(1)
    finally:
        with open(__CANCEL_MARKER__, "w") as marker:
            marker.write("cancelled")
    return {"ok": True}

@app.get("/healthy")
async def healthy(request):
    return {"ok": True}

@app.get("/invalid")
async def invalid(request):
    return Response(b"bad", content_type="invalid\\nheader")
"""


def pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port: int, proc, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            output, _ = proc.communicate()
            raise RuntimeError(f"server exited during startup:\n{output}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"server on :{port} didn't come up in {timeout}s")


def get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def main() -> int:
    port = pick_port()
    app_file = ROOT / "tests" / "_reliability_app.py"
    cancel_marker = ROOT / "tests" / "_cancelled.marker"
    cancel_marker.unlink(missing_ok=True)
    app_file.write_text(
        APP_SRC.replace("__CANCEL_MARKER__", repr(str(cancel_marker)))
        + f"\napp.run(host='127.0.0.1', port={port}, "
        + "request_timeout_seconds=0.1, shutdown_timeout_seconds=1)\n"
    )
    python_path = os.pathsep.join(
        path for path in (str(ROOT / "python"), os.environ.get("PYTHONPATH", "")) if path
    )
    proc = subprocess.Popen(
        [sys.executable, str(app_file)],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": python_path, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        wait_for(port, proc)
        status, body = get(port, "/timeout")
        if status != 504 or b"gateway timeout" not in body:
            proc.send_signal(signal.SIGTERM)
            output, _ = proc.communicate(timeout=5)
            raise AssertionError(
                f"expected HTTP 504 gateway timeout, got {status}: {body!r}\n{output}"
            )
        deadline = time.time() + 2
        while time.time() < deadline and not cancel_marker.exists():
            time.sleep(0.05)
        if not cancel_marker.exists():
            raise AssertionError("timed-out coroutine was not cancelled")

        status, body = get(port, "/healthy")
        if status != 200 or b'"ok": true' not in body:
            raise AssertionError(f"server did not remain healthy after timeout: {status}: {body!r}")

        status, body = get(port, "/invalid")
        if status != 500 or b"invalid" in body:
            raise AssertionError("invalid response metadata leaked to the client")

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
        print("PASS — handler timeout returns 504 and SIGTERM shuts the server down")
        return 0
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        app_file.unlink(missing_ok=True)
        cancel_marker.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
