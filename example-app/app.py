"""A configurable pyferrum application; see README.md for usage."""

import asyncio
import os
from pathlib import Path

from pyferrum import (
    CsvResponse,
    EventSourceResponse,
    Ferrum,
    FileResponse,
    HtmlResponse,
    JsonResponse,
    RedirectResponse,
    Request,
    ServerSentEvent,
    StreamingResponse,
    TextResponse,
    XmlResponse,
)

HERE = Path(__file__).resolve().parent


def boolean(name, default):
    value = os.environ.get(name, str(default)).lower()
    if value not in ("true", "false", "1", "0"):
        raise ValueError(f"{name} must be true, false, 1, or 0")
    return value in ("true", "1")


app = Ferrum(
    server_name=os.environ.get("SERVER_NAME", "pyferrum-example/1.0"),
    max_python_in_flight=int(os.environ.get("MAX_PYTHON_IN_FLIGHT", "100")),
)


@app.get("/")
def index(request: Request):
    return {"hello": "world", "documentation": "/static"}


@app.get("/users/{id}")
def user(request: Request):
    try:
        user_id = int(request.path_params["id"])
    except ValueError:
        return JsonResponse({"error": "id must be an integer"}, status_code=400)
    return {"id": user_id, "name": f"User {user_id}"}


@app.get("/sync")
def sync_handler(request: Request):
    return {"sync": True, "method": request.method}


@app.get("/slow")
async def slow(request: Request):
    await asyncio.sleep(0.05)
    return {"slept": "50ms"}


@app.get("/text")
def text(request: Request):
    return TextResponse("hello, world\n")


@app.get("/html")
def html(request: Request):
    return HtmlResponse("<h1>Hello from pyferrum</h1>")


@app.get("/xml")
def xml(request: Request):
    return XmlResponse('<?xml version="1.0" encoding="UTF-8"?><message>Hello</message>')


@app.get("/csv")
def csv_file(request: Request):
    return CsvResponse([["id", "name"], [1, "Example, Inc."]])


@app.get("/download")
def download(request: Request):
    return FileResponse(HERE / "index.html", filename="example.html")


@app.get("/redirect")
def redirect(request: Request):
    return RedirectResponse("/html", status_code=307)


@app.get("/events")
async def events(request: Request):
    async def source():
        yield ServerSentEvent("connected", event="status", id="1", retry=3000)
        await asyncio.sleep(0.05)
        yield "hello\nworld"

    return EventSourceResponse(source())


@app.get("/stream")
async def stream(request: Request):
    async def chunks():
        yield b"hello,"
        await asyncio.sleep(0.01)
        yield b" stream\n"

    return StreamingResponse(chunks(), content_type="text/plain; charset=utf-8")


@app.get("/large")
def large(request: Request):
    return "compressible-" * 2000


@app.post("/echo")
def echo(request: Request):
    try:
        body = request.json()
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status_code=400)
    return JsonResponse({"echoed": body})


@app.get("/query")
def query(request: Request):
    return {"query": dict(request.query_params)}


@app.get("/headers")
def headers(request: Request):
    return {"headers": dict(request.headers)}


@app.get("/inspect/{id}")
@app.post("/inspect/{id}")
@app.put("/inspect/{id}")
@app.patch("/inspect/{id}")
@app.delete("/inspect/{id}")
@app.head("/inspect/{id}")
@app.options("/inspect/{id}")
def inspect_request(request: Request):
    return {
        "method": request.method,
        "path": request.path,
        "id": request.path_params["id"],
        "query": dict(request.query_params),
        "body_bytes": len(request.body),
        "scheme": request.scheme,
        "remote_addr": request.remote_addr,
    }


def route_file_handler(request: Request):
    return {"message": "Python handler registered from TOML"}


app.load_routes(str(HERE / "routes.toml"), {"example": route_file_handler})
app.static_file("/static", str(HERE / "index.html"), "text/html; charset=utf-8")
app.redirect("/docs", "/static")

native_library = os.environ.get("NATIVE_LIBRARY")
if native_library:
    app._load_native_routes(native_library)


def run():
    options = dict(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
        request_timeout_seconds=float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30")),
        body_timeout_seconds=float(os.environ.get("BODY_TIMEOUT_SECONDS", "30")),
        shutdown_timeout_seconds=float(os.environ.get("SHUTDOWN_TIMEOUT_SECONDS", "10")),
        security_headers_enabled=boolean("SECURITY_HEADERS_ENABLED", True),
        cors_enabled=boolean("CORS_ENABLED", False),
        cors_allowed_origins=[
            origin.strip()
            for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        ],
        cors_allowed_methods=[
            method.strip().upper()
            for method in os.environ.get(
                "CORS_ALLOWED_METHODS",
                "GET,HEAD,POST,PUT,PATCH,DELETE,OPTIONS",
            ).split(",")
            if method.strip()
        ],
        cors_allowed_headers=[
            header.strip()
            for header in os.environ.get(
                "CORS_ALLOWED_HEADERS",
                "Authorization,Content-Type,Accept,Origin,X-Requested-With",
            ).split(",")
            if header.strip()
        ],
        cors_exposed_headers=[
            header.strip()
            for header in os.environ.get("CORS_EXPOSED_HEADERS", "").split(",")
            if header.strip()
        ],
        cors_allow_credentials=boolean("CORS_ALLOW_CREDENTIALS", False),
        cors_max_age=int(os.environ.get("CORS_MAX_AGE", "86400")),
        rate_limit_requests=int(os.environ.get("RATE_LIMIT_REQUESTS", "0")),
        rate_limit_window_seconds=float(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60")),
        max_rate_limit_clients=int(os.environ.get("MAX_RATE_LIMIT_CLIENTS", "10000")),
        max_header_size=int(os.environ.get("MAX_HEADER_SIZE", "65536")),
        max_header_count=int(os.environ.get("MAX_HEADER_COUNT", "128")),
        max_in_flight_requests=int(os.environ.get("MAX_IN_FLIGHT_REQUESTS", "1000")),
        trusted_proxy_hosts=[
            host.strip()
            for host in os.environ.get("TRUSTED_PROXY_HOSTS", "").split(",")
            if host.strip()
        ],
        tls_cert_file=os.environ.get("TLS_CERT_FILE") or None,
        tls_key_file=os.environ.get("TLS_KEY_FILE") or None,
        tls_handshake_timeout_seconds=float(os.environ.get("TLS_HANDSHAKE_TIMEOUT_SECONDS", "10")),
        request_logging=boolean("REQUEST_LOGGING", True),
        compression_enabled=boolean("COMPRESSION_ENABLED", True),
        compression_threshold=int(os.environ.get("COMPRESSION_THRESHOLD", "1024")),
        metrics_enabled=boolean("METRICS_ENABLED", True),
        connection_timeout_seconds=float(os.environ.get("CONNECTION_TIMEOUT_SECONDS", "120")),
        workers=int(os.environ.get("WORKERS", "1")),
        worker_restart_limit=int(os.environ.get("WORKER_RESTART_LIMIT", "3")),
    )
    if os.environ.get("REUSE_PORT"):
        options["reuse_port"] = boolean("REUSE_PORT", False)
    app.run(**options)


if __name__ == "__main__":
    run()
