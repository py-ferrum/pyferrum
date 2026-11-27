"""Verify static-file and redirect routes execute without a Python handler."""

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


def pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port: int, proc, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Rust route server exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("Rust route server did not start")


def main() -> int:
    port = pick_port()
    asset = ROOT / "tests" / "_asset.txt"
    app_file = ROOT / "tests" / "_rust_routes_app.py"
    asset.write_text("served-by-rust\n")
    app_file.write_text(
        "from pyferrum import Ferrum\n"
        "app = Ferrum()\n"
        f"app.static_file('/asset', {str(asset)!r}, 'text/plain')\n"
        "app.redirect('/old', '/asset', 307)\n"
        f"app.run(host='127.0.0.1', port={port})\n"
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
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/asset", timeout=3) as response:
            if response.read() != b"served-by-rust\n":
                raise AssertionError("Rust static route returned the wrong body")

        request = urllib.request.Request(f"http://127.0.0.1:{port}/old", method="GET")
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=3) as response:
            if response.status != 307 or response.headers["Location"] != "/asset":
                raise AssertionError("Rust redirect response was incorrect")
        print("PASS — Rust-only static and redirect routes")
        return 0
    except urllib.error.HTTPError as error:
        if error.code == 307 and error.headers.get("Location") == "/asset":
            print("PASS — Rust-only static and redirect routes")
            return 0
        raise
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        app_file.unlink(missing_ok=True)
        asset.unlink(missing_ok=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
