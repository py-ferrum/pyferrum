"""Repeatable single-worker comparison for pyferrum, Flask/Gunicorn, and FastAPI/Uvicorn."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
REQUESTS = int(os.environ.get("PYFERRUM_BENCH_REQUESTS", "1000"))

RUST_APP = """
from pyferrum import Ferrum
app = Ferrum()
@app.get("/json")
async def endpoint(request):
    return {"message": "hello", "n": 42}
"""

FLASK_APP = """
from flask import Flask, jsonify
app = Flask(__name__)
@app.get("/json")
def endpoint():
    return jsonify(message="hello", n=42)
"""

FASTAPI_APP = """
from fastapi import FastAPI
app = FastAPI()
@app.get("/json")
async def endpoint():
    return {"message": "hello", "n": 42}
"""


def pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port: int, proc, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("benchmark server exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("benchmark server did not start")


async def benchmark(port: int) -> dict:
    latencies = []
    errors = 0
    semaphore = asyncio.Semaphore(50)

    async def one(session):
        nonlocal errors
        async with semaphore:
            started = time.perf_counter_ns()
            try:
                async with session.get(f"http://127.0.0.1:{port}/json") as response:
                    await response.read()
                    if response.status != 200:
                        errors += 1
            except Exception:
                errors += 1
            latencies.append(time.perf_counter_ns() - started)

    connector = aiohttp.TCPConnector(limit=100, limit_per_host=100)
    async with aiohttp.ClientSession(connector=connector) as session:
        for _ in range(100):
            async with session.get(f"http://127.0.0.1:{port}/json") as response:
                await response.read()
        started = time.perf_counter()
        await asyncio.gather(*(one(session) for _ in range(REQUESTS)))
        elapsed = time.perf_counter() - started
    latencies.sort()
    return {
        "rps": REQUESTS / elapsed,
        "p50": latencies[int(REQUESTS * 0.50)] / 1_000_000,
        "p95": latencies[int(REQUESTS * 0.95)] / 1_000_000,
        "p99": latencies[int(REQUESTS * 0.99)] / 1_000_000,
        "errors": errors,
    }


async def run_one(name: str, command: list, app_file: Path, app_source: str) -> dict:
    port = pick_port()
    app_file.write_text(app_source + f"\nAPP_PORT = {port}\n")
    if name == "pyferrum":
        app_file.write_text(app_source + f"\napp.run(host='127.0.0.1', port={port})\n")
    resolved = [argument.format(port=port, root=ROOT) for argument in command]
    proc = subprocess.Popen(
        resolved,
        cwd=str(ROOT),
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                path for path in (str(ROOT / "python"), os.environ.get("PYTHONPATH", "")) if path
            ),
            "PYTHONUNBUFFERED": "1",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(port, proc)
        return await benchmark(port)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        app_file.unlink(missing_ok=True)


async def amain() -> None:
    rust_file = ROOT / "tests" / "_benchmark_rust.py"
    flask_file = ROOT / "tests" / "_benchmark_flask.py"
    fastapi_file = ROOT / "tests" / "_benchmark_fastapi.py"
    cases = [
        ("pyferrum", [sys.executable, str(rust_file)], rust_file, RUST_APP),
        (
            "flask-gunicorn",
            [
                str(Path(sys.executable).with_name("gunicorn")),
                "-w",
                "1",
                "-b",
                "127.0.0.1:{port}",
                "_benchmark_flask:app",
                "--chdir",
                str(ROOT / "tests"),
            ],
            flask_file,
            FLASK_APP,
        ),
        (
            "fastapi-uvicorn",
            [
                str(Path(sys.executable).with_name("uvicorn")),
                "_benchmark_fastapi:app",
                "--app-dir",
                str(ROOT / "tests"),
                "--host",
                "127.0.0.1",
                "--port",
                "{port}",
                "--workers",
                "1",
            ],
            fastapi_file,
            FASTAPI_APP,
        ),
    ]
    print(f"Requests={REQUESTS}, concurrency=50, single worker")
    for name, command, app_file, source in cases:
        result = await run_one(name, command, app_file, source)
        print(
            f"{name:16s} {result['rps']:8.1f} RPS  "
            f"p50={result['p50']:6.2f}ms p95={result['p95']:6.2f}ms "
            f"p99={result['p99']:6.2f}ms errors={result['errors']}"
        )


if __name__ == "__main__":
    asyncio.run(amain())
