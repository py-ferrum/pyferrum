"""Verify native rustls termination and HTTPS request metadata."""

import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

APP_SRC = """
from pyferrum import Ferrum

app = Ferrum()

@app.get("/tls")
async def tls(request):
    return {"scheme": request.scheme}
"""


def pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_tls(port: int, proc, context, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            output, _ = proc.communicate()
            raise RuntimeError(f"TLS server exited during startup:\n{output}")
        try:
            with urllib.request.urlopen(
                f"https://127.0.0.1:{port}/tls", context=context, timeout=0.5
            ):
                return
        except OSError:
            time.sleep(0.05)


def main() -> int:
    openssl = subprocess.run(["openssl", "version"], capture_output=True)
    if openssl.returncode != 0:
        print("SKIP — openssl is unavailable")
        return 0

    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        cert = directory / "cert.pem"
        key = directory / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )
        port = pick_port()
        app_file = ROOT / "tests" / "_tls_app.py"
        app_file.write_text(
            APP_SRC
            + f"\napp.run(host='127.0.0.1', port={port}, "
            + f"tls_cert_file={str(cert)!r}, tls_key_file={str(key)!r})\n"
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
            context = ssl._create_unverified_context()
            wait_for_tls(port, proc, context)
            with urllib.request.urlopen(
                f"https://127.0.0.1:{port}/tls", context=context, timeout=3
            ) as response:
                body = response.read()
                if response.status != 200 or b'"scheme": "https"' not in body:
                    raise AssertionError(f"unexpected TLS response: {response.status}, {body!r}")
            print("PASS — native TLS terminates HTTPS and exposes https request scheme")
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
