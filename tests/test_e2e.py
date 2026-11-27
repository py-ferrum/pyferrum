"""End-to-end integration test for pyferrum.

Boots the demo server in a subprocess, fires HTTP requests at it with
urllib, and asserts the responses. Proves the full request lifecycle works:
hyper → matchit → PyO3 → Python handler → serde/JSON → hyper response.

Run with:
    python tests/test_e2e.py
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "example-app" / "app.py"


def pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"server on :{port} didn't come up in {timeout}s")


def get(port: int, path: str, method: str = "GET", body: Optional[bytes] = None, headers=None):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            # `r.headers` is an http.client.HTTPMessage (case-insensitive);
            # convert to a plain dict but keep a case-insensitive lookup helper.
            raw_headers = dict(r.headers)
            return r.status, r.read(), raw_headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def hget(headers: dict, name: str) -> str:
    """Case-insensitive header lookup (urllib preserves wire case)."""
    name_lower = name.lower()
    for k, v in headers.items():
        if k.lower() == name_lower:
            return v
    return ""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    port = pick_port()
    python_path = os.pathsep.join(
        path for path in (str(ROOT / "python"), os.environ.get("PYTHONPATH", "")) if path
    )
    env = {
        **os.environ,
        "PYTHONPATH": python_path,
        "PYTHONUNBUFFERED": "1",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "REQUEST_LOGGING": "true",
        "RUST_LOG": "info",
        "NATIVE_LIBRARY": "",
    }
    proc = subprocess.Popen(
        [sys.executable, str(DEMO)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_for(port)
        print(f"[ok] server up on :{port}")

        failures = 0

        def check(name, cond, detail=""):
            nonlocal failures
            mark = "ok" if cond else "FAIL"
            if not cond:
                failures += 1
            print(f"  [{mark}] {name} {detail}")

        # 1. JSON root
        status, body, _ = get(port, "/")
        check("GET / -> 200", status == 200, f"(got {status})")
        check("GET / body has hello", b"hello" in body, f"(body={body[:80]!r})")

        # 2. Health
        status, body, _ = get(port, "/healthz")
        check("GET /healthz -> 200", status == 200)
        check("GET /healthz body", json.loads(body)["status"] == "healthy", f"(body={body!r})")

        # 3. Path params
        status, body, _ = get(port, "/users/42")
        check("GET /users/42 -> 200", status == 200)
        payload = json.loads(body)
        check("path param id=42", payload.get("id") == 42, f"(payload={payload})")

        # 4. Path param validation
        status, body, _ = get(port, "/users/abc")
        check("GET /users/abc -> 400", status == 400, f"(got {status})")

        # 5. Sync handler
        status, body, _ = get(port, "/sync")
        check("GET /sync -> 200 (sync handler)", status == 200)
        check("sync handler returns sync=True", b'"sync": true' in body.lower(), f"(body={body!r})")

        # 6. Text response
        status, body, hdrs = get(port, "/text")
        check("GET /text -> 200", status == 200)
        check("text body", body == b"hello, world\n", f"(body={body!r})")
        check(
            "text content-type",
            "text/plain" in hget(hdrs, "Content-Type"),
            f"(ct={hget(hdrs, 'Content-Type')!r})",
        )

        status, body, hdrs = get(port, "/stream")
        check("GET /stream -> 200", status == 200)
        check("streamed body", body == b"hello, stream\n", f"(body={body!r})")
        check(
            "stream content-type",
            "text/plain" in hget(hdrs, "Content-Type"),
            f"(ct={hget(hdrs, 'Content-Type')!r})",
        )

        status, body, hdrs = get(port, "/large", headers={"Accept-Encoding": "gzip"})
        check("GET /large gzip -> 200", status == 200)
        check("gzip content encoding", hget(hdrs, "Content-Encoding") == "gzip")
        check("gzip payload", body.startswith(b"\x1f\x8b"))

        status, body, hdrs = get(port, "/metrics")
        check("GET /metrics -> 200", status == 200)
        check("metrics request counter", b"pyferrum_requests_total" in body)

        # 7. Async with await
        status, body, _ = get(port, "/slow")
        check("GET /slow (async await) -> 200", status == 200)
        check("async handler completed", b'"slept": "50ms"' in body, f"(body={body!r})")

        # 8. POST echo
        payload = {"msg": "hi", "n": 7}
        status, body, _ = get(
            port,
            "/echo",
            method="POST",
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        check("POST /echo -> 200", status == 200)
        echoed = json.loads(body)
        check("echoed body matches", echoed.get("echoed") == payload, f"(echoed={echoed})")

        # 9. Request body limit
        status, body, _ = get(
            port,
            "/echo",
            method="POST",
            body=b"x" * (10 * 1024 * 1024 + 1),
        )
        check("POST body over 10 MiB -> 413", status == 413, f"(got {status})")
        check("oversized body error is generic", b"payload too large" in body)

        # 10. Headers endpoint
        status, body, _ = get(port, "/headers", headers={"X-Custom": "abc"})
        check("GET /headers -> 200", status == 200)
        hdrs_payload = json.loads(body)
        check(
            "custom header seen",
            hdrs_payload.get("headers", {}).get("x-custom") == "abc",
            f"(hdrs={hdrs_payload})",
        )

        # 11. Query params
        status, body, _ = get(port, "/query?a=1&b=two")
        check("GET /query?a=1&b=two -> 200", status == 200)
        qp = json.loads(body)
        check("query params parsed", qp.get("query") == {"a": "1", "b": "two"}, f"(qp={qp})")

        # 12. 404
        status, _, _ = get(port, "/definitely-not-a-route")
        check("GET /missing -> 404", status == 404, f"(got {status})")

        # 13. 405
        status, _, _ = get(port, "/users/42", method="DELETE")
        check("DELETE /users/42 -> 405", status == 405, f"(got {status})")

        # 14. Server header
        status, _, hdrs = get(port, "/")
        check(
            "server header set",
            "pyferrum" in hget(hdrs, "Server").lower(),
            f"(server={hget(hdrs, 'Server')!r})",
        )

        # 15. Rust-only health routes and request IDs
        for path, expected in (("/livez", "alive"), ("/readyz", "ready")):
            status, body, _ = get(port, path)
            check(f"GET {path} -> 200", status == 200 and json.loads(body)["status"] == expected)

        status, body, _ = get(port, "/from-table")
        check("TOML Python handler", status == 200 and b"registered from TOML" in body)
        status, body, _ = get(port, "/static")
        check("Rust static file", status == 200 and b"pyferrum example" in body)
        status, body, _ = get(port, "/inspect/42?view=full", method="PATCH", body=b"example")
        payload = json.loads(body)
        check(
            "PATCH request details",
            status == 200
            and payload["method"] == "PATCH"
            and payload["id"] == "42"
            and payload["query"] == {"view": "full"}
            and payload["body_bytes"] == 7,
        )

        for path, content_type, expected in (
            ("/html", "text/html; charset=utf-8", b"<h1>Hello from pyferrum</h1>"),
            ("/xml", "application/xml; charset=utf-8", b"<message>Hello</message>"),
            ("/csv", "text/csv; charset=utf-8", b'1,"Example, Inc."\r\n'),
        ):
            status, body, hdrs = get(port, path)
            check(f"{path} response", status == 200 and expected in body)
            check(f"{path} content type", hget(hdrs, "Content-Type") == content_type)

        status, body, hdrs = get(port, "/download")
        check("file download body", status == 200 and b"pyferrum example" in body)
        check(
            "file attachment header",
            hget(hdrs, "Content-Disposition")
            == "attachment; filename=\"example.html\"; filename*=UTF-8''example.html",
        )
        check("file content type", hget(hdrs, "Content-Type") == "text/html")

        status, body, hdrs = get(port, "/events")
        check(
            "SSE status and content type",
            status == 200 and hget(hdrs, "Content-Type") == "text/event-stream; charset=utf-8",
        )
        check("SSE cache control", hget(hdrs, "Cache-Control") == "no-cache")
        check(
            "SSE wire format",
            body == b"event: status\nid: 1\nretry: 3000\ndata: connected\n\n"
            b"data: hello\ndata: world\n\n",
        )
        status, body, _ = get(port, "/redirect")
        check("redirect follows target", status == 200 and b"<h1>Hello from pyferrum</h1>" in body)
        try:
            urllib.request.build_opener(NoRedirect()).open(f"http://127.0.0.1:{port}/redirect")
        except urllib.error.HTTPError as error:
            check(
                "redirect status and location",
                error.code == 307 and error.headers["Location"] == "/html",
            )
        else:
            check("redirect status and location", False)

        print()
        if failures == 0:
            print(f"ALL PASS — full request lifecycle works on port {port}")
            return 0
        else:
            print(f"{failures} FAILURE(S)")
            return 1
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
