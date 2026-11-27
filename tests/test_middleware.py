"""Verify security, CORS, request ID, Authorization, and rate-limit middleware."""

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
from pyferrum import Ferrum

app = Ferrum()

@app.get("/ok")
async def ok(request):
    return {
        "ok": True,
        "remote": request.remote_addr,
        "scheme": request.scheme,
        "authorization": request.headers.get("authorization"),
    }
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
    raise TimeoutError(f"server on :{port} didn't come up")


def get(
    port: int,
    path: str,
    request_id: str | None = None,
    forwarded: bool = False,
):
    headers = {"X-Request-ID": request_id} if request_id else {}
    if forwarded:
        headers.update({"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"})
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def header(headers, name: str) -> str:
    name = name.lower()
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


def oversized_header(port: int) -> tuple:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/ok",
        headers={"X-Large": "x" * 8192},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def request(port: int, path: str, method: str = "GET", headers=None) -> tuple:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def main() -> int:
    port = pick_port()
    app_file = ROOT / "tests" / "_middleware_app.py"
    app_file.write_text(
        APP_SRC
        + f"\napp.run(host='127.0.0.1', port={port}, "
        + "security_headers_enabled=True, rate_limit_requests=2, "
        + "rate_limit_window_seconds=60, max_header_size=4096, "
        + "cors_enabled=True, "
        + "cors_allowed_origins=['https://client.example'], "
        + "cors_allowed_methods=['GET', 'OPTIONS'], "
        + "cors_allowed_headers=['Authorization', 'Content-Type'], "
        + "cors_exposed_headers=['X-Request-ID'], "
        + "cors_allow_credentials=True, cors_max_age=600, "
        + "trusted_proxy_hosts=['127.0.0.1'])\n"
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

        status, body, headers = get(port, "/ok", "trace-123", forwarded=True)
        if status != 200:
            raise AssertionError(f"first request returned {status}")
        if header(headers, "X-Request-ID") != "trace-123":
            raise AssertionError("configured request ID was not preserved")
        if header(headers, "X-Content-Type-Options") != "nosniff":
            raise AssertionError("missing X-Content-Type-Options")
        if header(headers, "X-Frame-Options") != "DENY":
            raise AssertionError("missing X-Frame-Options")
        if b'"scheme": "https"' not in body or b"203.0.113.9" not in body:
            raise AssertionError("trusted forwarded client metadata was not exposed")

        status, body, headers = request(
            port,
            "/ok",
            headers={
                "Origin": "https://client.example",
                "Authorization": "Bearer test-token",
            },
        )
        if status != 200:
            raise AssertionError("CORS/Authorization request did not reach handler")
        if b'"authorization": "Bearer test-token"' not in body:
            raise AssertionError("Authorization header was not exposed to the handler")
        if header(headers, "Access-Control-Allow-Origin") != "https://client.example":
            raise AssertionError("allowed CORS origin was not echoed")
        if header(headers, "Access-Control-Allow-Credentials") != "true":
            raise AssertionError("CORS credentials were not enabled")
        if "X-Request-ID" not in header(headers, "Access-Control-Expose-Headers"):
            raise AssertionError("CORS exposed headers were not returned")

        status, body, headers = get(port, "/ok")
        if status != 429 or b"too many requests" not in body:
            raise AssertionError(f"third request did not receive 429: {status}, {body!r}")
        if not header(headers, "Retry-After"):
            raise AssertionError("429 response is missing Retry-After")

        status, body, headers = oversized_header(port)
        if status != 431 or b"request headers too large" not in body:
            raise AssertionError(f"oversized headers did not receive 431: {status}, {body!r}")
        if not header(headers, "X-Request-ID"):
            raise AssertionError("431 response is missing X-Request-ID")

        status, body, headers = request(
            port,
            "/ok",
            method="OPTIONS",
            headers={
                "Origin": "https://client.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization,Content-Type",
            },
        )
        if status != 204:
            raise AssertionError(f"CORS preflight returned {status}: {body!r}")
        if header(headers, "Access-Control-Allow-Origin") != "https://client.example":
            raise AssertionError("CORS preflight origin was not returned")
        if "GET" not in header(headers, "Access-Control-Allow-Methods"):
            raise AssertionError("CORS preflight methods were not returned")
        if "Authorization" not in header(headers, "Access-Control-Allow-Headers"):
            raise AssertionError("Authorization was not returned as an allowed CORS header")
        if header(headers, "Access-Control-Max-Age") != "600":
            raise AssertionError("CORS preflight max age was not returned")

        status, _, headers = request(
            port,
            "/ok",
            method="OPTIONS",
            headers={
                "Origin": "https://denied.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        if status != 403:
            raise AssertionError("denied CORS origin did not receive 403")

        print("PASS — CORS, Authorization header, security headers, IDs, 429, and 431")
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
