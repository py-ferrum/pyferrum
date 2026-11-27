"""Verify overload rejection and persistent async event-loop reuse."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

APP_SRC = """
import asyncio
from pyferrum import Ferrum

try:
    Ferrum(max_python_in_flight=0)
except ValueError:
    pass
else:
    raise AssertionError("zero Python handler capacity must be rejected")

app = Ferrum(max_python_in_flight=1)
observed_loops = []

@app.get("/slow")
async def slow(request):
    event_loop = asyncio.get_running_loop()
    if not any(loop is event_loop for loop in observed_loops):
        observed_loops.append(event_loop)
    await asyncio.sleep(0.5)
    return {"loop_count": len(observed_loops)}
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


def hit(port: int):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/slow", timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def main() -> int:
    port = pick_port()
    app_file = ROOT / "tests" / "_overload_app.py"
    app_file.write_text(APP_SRC + f"\napp.run(host='127.0.0.1', port={port})\n")
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
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: hit(port), range(2)))
        if sorted(status for status, _ in responses) != [200, 503]:
            raise AssertionError(f"expected one 200 and one 503 response, got {responses}")

        first_success = next(body for status, body in responses if status == 200)
        next_status, next_body = hit(port)
        if next_status != 200:
            raise AssertionError(f"expected a subsequent request to return 200, got {next_status}")
        if json.loads(first_success)["loop_count"] != 1 or json.loads(next_body)["loop_count"] != 1:
            raise AssertionError("async handlers did not reuse the process event loop")

        print("PASS — overload returns 503 and async requests reuse the process event loop")
        return 0
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        app_file.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
