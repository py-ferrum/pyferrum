"""Verify integrated multi-process startup and shared-port distribution."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

APP_SRC = """
import os
from pyferrum import Ferrum

app = Ferrum()

@app.get("/worker")
async def worker(request):
    return {"pid": os.getpid()}
"""


def pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port: int, proc, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("worker supervisor exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("workers did not start")


def main() -> int:
    port = pick_port()
    app_file = ROOT / "tests" / "_workers_app.py"
    app_file.write_text(
        APP_SRC + f"\napp.run(host='127.0.0.1', port={port}, workers=2, worker_restart_limit=0)\n"
    )
    python_path = os.pathsep.join(
        path for path in (str(ROOT / "python"), os.environ.get("PYTHONPATH", "")) if path
    )
    proc = subprocess.Popen(
        [sys.executable, str(app_file)],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": python_path, "PYTHONUNBUFFERED": "1"},
    )
    try:
        wait_for(port, proc)
        pids = set()
        for _ in range(40):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/worker", timeout=3) as response:
                pids.add(json.loads(response.read())["pid"])
        if len(pids) < 2:
            raise AssertionError(f"requests did not reach both workers: {sorted(pids)}")
        print("PASS — integrated supervisor shared the listener across two worker PIDs")
        return 0
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        app_file.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
