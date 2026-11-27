"""pyferrum: a Rust-core Python web framework.

The native extension `_pyferrum` (built by maturin from the Rust workspace)
provides the HTTP engine, router, and the `_Ferrum` app class. This pure-Python
package wraps it with the ergonomic decorator API and convenience types.

Design rule: every hot path — HTTP parsing, routing, header munging, byte
serialization — lives in Rust. Python is reduced to a thin decorator shell
plus the user's business logic.
"""

import csv
import io
import mimetypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    TypeVar,
)
from urllib.parse import quote

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from ._pyferrum import (
    Request,
    Response,
    StreamingResponse,
    _Ferrum,
)
from ._pyferrum import __version__ as __version__

__all__ = [
    "Ferrum",
    "Request",
    "Response",
    "StreamingResponse",
    "JsonResponse",
    "TextResponse",
    "HtmlResponse",
    "XmlResponse",
    "CsvResponse",
    "FileResponse",
    "RedirectResponse",
    "ServerSentEvent",
    "EventSourceResponse",
    "run",
    "__version__",
]

F = TypeVar("F", bound=Callable[..., Any])


def JsonResponse(
    data: Any,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """Build a 200 JSON response from any JSON-serializable Python object.

    Uses the native `Response.json` staticmethod, which serializes JSON-compatible
    Python containers with Rust `serde_json`.
    """
    return Response.json(data, status_code=status_code, headers=headers)


def TextResponse(
    text: str,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """Build a 200 `text/plain; charset=utf-8` response."""
    return Response.text(text, status_code=status_code, headers=headers)


def HtmlResponse(
    html: str, status_code: int = 200, headers: dict[str, str] | None = None
) -> Response:
    """Return UTF-8 HTML. The caller is responsible for escaping untrusted content."""
    return Response(html.encode("utf-8"), status_code, "text/html; charset=utf-8", headers)


def XmlResponse(
    xml: str, status_code: int = 200, headers: dict[str, str] | None = None
) -> Response:
    """Return an already serialized UTF-8 XML document."""
    return Response(xml.encode("utf-8"), status_code, "application/xml; charset=utf-8", headers)


def CsvResponse(
    rows: Iterable[Iterable[Any]],
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """Serialize rows as UTF-8 CSV using the standard CSV quoting rules."""
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    return Response(
        output.getvalue().encode("utf-8"), status_code, "text/csv; charset=utf-8", headers
    )


def _set_header(headers: dict[str, str] | None, name: str, value: str) -> dict[str, str]:
    result = {key: item for key, item in (headers or {}).items() if key.lower() != name.lower()}
    result[name] = value
    return result


def FileResponse(
    path: str | Path,
    filename: str | None = None,
    content_type: str | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """Read a file into memory and return it as an attachment."""
    file_path = Path(path)
    download_name = file_path.name if filename is None else filename
    if (
        not download_name
        or download_name in (".", "..")
        or any(char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in download_name)
    ):
        raise ValueError(
            "filename must be a non-empty filename without path separators or controls"
        )
    fallback = download_name.encode("ascii", "replace").decode("ascii").replace('"', '\\"')
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''" + quote(
        download_name, safe=""
    )
    media_type = (
        content_type
        if content_type is not None
        else mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    )
    return Response(
        file_path.read_bytes(),
        status_code,
        media_type,
        _set_header(headers, "Content-Disposition", disposition),
    )


def RedirectResponse(
    location: str, status_code: int = 302, headers: dict[str, str] | None = None
) -> Response:
    """Return a redirect to a caller-supplied location."""
    if status_code not in (301, 302, 303, 307, 308):
        raise ValueError("redirect status must be 301, 302, 303, 307, or 308")
    if not location or any(ord(char) < 32 or ord(char) == 127 for char in location):
        raise ValueError("redirect location must be non-empty and contain no control characters")
    return Response(
        b"", status_code, "text/plain; charset=utf-8", _set_header(headers, "Location", location)
    )


@dataclass(frozen=True)
class ServerSentEvent:
    """An SSE event; data may contain multiple lines."""

    data: str
    event: str | None = None
    id: str | None = None
    retry: int | None = None

    def encode(self) -> str:
        fields = []
        for name, value in (("event", self.event), ("id", self.id)):
            if value is not None:
                if any(char in value for char in "\r\n\0"):
                    raise ValueError(f"SSE {name} must not contain CR, LF, or NUL")
                fields.append(f"{name}: {value}")
        if self.retry is not None:
            if isinstance(self.retry, bool) or not isinstance(self.retry, int) or self.retry < 0:
                raise ValueError("SSE retry must be a non-negative integer")
            fields.append(f"retry: {self.retry}")
        data = self.data.replace("\r\n", "\n").replace("\r", "\n")
        fields.extend(f"data: {line}" for line in data.split("\n"))
        return "\n".join(fields) + "\n\n"


def EventSourceResponse(
    source: AsyncIterable[str | ServerSentEvent],
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> StreamingResponse:
    """Stream strings or ServerSentEvent instances using the SSE wire format."""

    async def events():
        async for event in source:
            if isinstance(event, str):
                event = ServerSentEvent(event)
            if not isinstance(event, ServerSentEvent):
                raise TypeError("SSE events must be strings or ServerSentEvent instances")
            yield event.encode()

    response_headers = dict(headers or {})
    if not any(name.lower() == "cache-control" for name in response_headers):
        response_headers["Cache-Control"] = "no-cache"
    return StreamingResponse(
        events(),
        status_code=status_code,
        content_type="text/event-stream; charset=utf-8",
        headers=response_headers,
    )


# ---------------------------------------------------------------------------
# Decorator factory
# ---------------------------------------------------------------------------

_HTTP_METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


def _make_method_decorator(method_name: str) -> Callable[..., Any]:
    """Build a decorator factory for `method_name` (e.g. "get" -> "GET")."""

    method_upper = method_name.upper()

    def decorator(self: "Ferrum", path: str) -> Callable[[F], F]:
        def wrapper(fn: F) -> F:
            self._register(method_upper, path, fn)
            return fn

        return wrapper

    decorator.__name__ = method_name
    decorator.__doc__ = (
        f"Register a handler for HTTP {method_upper} requests matching `path`.\n\n"
        f"Args:\n"
        f"    path: Absolute route path, optionally containing parameters such as `{{id}}`.\n\n"
        f"Returns:\n"
        f"    A decorator that registers the handler and returns it unchanged.\n\n"
        f"Example:\n\n"
        f'    @app.{method_name}("/path/{{id}}")\n'
        f"    async def handler(request: Request) -> Response:\n"
        f"        ...\n\n"
        f"The handler receives one `Request` and may be sync or async. "
        f"Path parameters are available via `request.path_params`. Duplicate "
        f"or invalid routes raise `ValueError` during registration."
    )
    return decorator


class Ferrum(_Ferrum):
    """Create an application and register HTTP routes.

    Args:
        server_name: Value of the HTTP Server header. An empty string disables it.
            Defaults to ``"pyferrum/1.0"``.
        max_python_in_flight: Maximum concurrent Python handler invocations
            per application instance. Must be positive; excess requests receive
            HTTP 503. Defaults to 100.

    Route decorators ``get``, ``post``, ``put``, ``patch``, ``delete``,
    ``head`` and ``options`` accept a path and return a decorator. Each
    handler receives one ``Request`` and may be synchronous or asynchronous.
    Path parameters are exposed through ``request.path_params``. Handlers may
    return response objects, JSON-compatible containers, strings or bytes.
    Routes must be registered before the server starts.

    ``load_routes``, ``static_file`` and ``redirect`` register additional
    routes; ``route_count`` returns the number currently registered.

    Example:
        ```python
        from pyferrum import Ferrum

        app = Ferrum()

        @app.get("/")
        async def index(request):
            return {"hello": "world"}

        if __name__ == "__main__":
            app.run(host="127.0.0.1", port=8080)
        ```
    """

    def load_routes(
        self,
        route_file: str,
        python_handlers: Mapping[str, Callable[..., Any]] | None = None,
    ) -> int:
        """Register routes from a TOML file and return the number loaded.

        ``route_file`` must contain a ``[[routes]]`` array whose entries have
        string ``method``, ``path`` and ``handler`` fields. ``runtime``
        defaults to ``"python"``. Python handler names must be supplied in
        ``python_handlers``; Rust handler names are ``health``, ``liveness``
        and ``readiness``. These built-ins return fixed status values, not
        dependency checks.

        Raises:
            ValueError: The file cannot be read, is invalid TOML, or contains
                an invalid route, unknown handler or unsupported runtime.
                Routes registered before an error are not rolled back.
        """
        try:
            document = tomllib.loads(Path(route_file).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"invalid route file {route_file!r}: {error}") from error

        routes = document.get("routes")
        if not isinstance(routes, list):
            raise ValueError("route file must contain a [[routes]] array")

        for index, route in enumerate(routes):
            if not isinstance(route, dict):
                raise ValueError(f"route {index} must be a table")
            method = route.get("method")
            path = route.get("path")
            handler = route.get("handler")
            runtime = route.get("runtime", "python")
            if not all(isinstance(value, str) for value in (method, path, handler, runtime)):
                raise ValueError(
                    f"route {index} requires string method, path, handler, and runtime"
                )
            if runtime == "python":
                callable_ = (python_handlers or {}).get(handler)
                if callable_ is None:
                    raise ValueError(
                        f"route {index} handler {handler!r} was not explicitly supplied"
                    )
                self._register(method.upper(), path, callable_)
            elif runtime == "rust":
                self._register_builtin(method.upper(), path, handler)
            else:
                raise ValueError(f"route {index} has unsupported runtime {runtime!r}")
        return len(routes)

    def static_file(
        self,
        path: str,
        file_path: str,
        content_type: str = "application/octet-stream",
        method: str = "GET",
    ) -> None:
        """Register a Rust-only response whose file is read immediately.

        Args:
            path: Route path.
            file_path: Path of the file to read into memory at registration.
            content_type: Response media type; defaults to
                ``"application/octet-stream"``.
            method: HTTP method to register, defaulting to ``"GET"``.

        Raises:
            OSError: The file cannot be read.
            ValueError: The route, method or content type is invalid.
        """
        self._register_static_file(method.upper(), path, file_path, content_type)

    def redirect(self, path: str, location: str, status_code: int = 302) -> None:
        """Register a Rust-only GET redirect.

        ``location`` is the response's Location header and ``status_code``
        must be in the 3xx range. Invalid routes, locations or statuses raise
        ``ValueError``. Validate untrusted destinations to avoid open redirects.
        """
        self._register_redirect(path, location, status_code)

    def run(self, *args: Any, **kwargs: Any) -> None:
        """Start the HTTP server and block until shutdown.

        When serving in a single process or a worker, positional and keyword
        server options are forwarded to the native runtime. The first two are
        ``host: Optional[str]`` and ``port: Optional[int]`` (defaults
        ``127.0.0.1`` and 8080). Supported keyword options are:

        * ``request_timeout_seconds``, ``body_timeout_seconds`` and
          ``shutdown_timeout_seconds``: optional floats, defaulting to 30,
          30 and 10 seconds. ``connection_timeout_seconds`` is a float
          defaulting to 120.
        * ``security_headers_enabled``, ``request_logging``,
          ``compression_enabled`` and ``metrics_enabled``: booleans, defaulting
          to true, false, true and true respectively. ``compression_threshold``
          is an integer number of bytes, defaulting to 1024.
        * ``cors_enabled``: bool (false). ``cors_allowed_origins``: optional
          list of exact origins (an empty list denies all cross-origin access);
          ``cors_allowed_methods``, ``cors_allowed_headers`` and
          ``cors_exposed_headers``: optional header/method lists.
          ``cors_allow_credentials`` is false and ``cors_max_age`` is 86400.
          Authorization is allowed by the default CORS header list.
        * ``rate_limit_requests``: int (0 disables limiting);
          ``rate_limit_window_seconds``: float (60);
          ``max_rate_limit_clients``: int (10000);
          ``max_header_size``: optional int (64 KiB);
          ``max_header_count``: optional int (128);
          ``max_in_flight_requests``: int (1000; 0 disables the cap).
        * ``trusted_proxy_hosts``: optional list of trusted IP strings;
          ``tls_cert_file`` and ``tls_key_file``: optional strings (both
          required for TLS); ``tls_handshake_timeout_seconds``: float (10);
          ``reuse_port``: bool (false).
        * ``workers``: int (1) and ``worker_restart_limit``: int (3) control the Python
          subprocess supervisor. Multiple workers require port-reuse support
          and re-run the application entry point in each child process. The
          entry point must configure the run options again in each worker.

        Raises:
            ValueError: A worker count, limit or native configuration is invalid.
            OSError: The listener or TLS configuration cannot be initialized.
        """
        workers = int(kwargs.pop("workers", 1))
        restart_limit = int(kwargs.pop("worker_restart_limit", 3))
        if workers <= 0:
            raise ValueError("workers must be greater than zero")
        if restart_limit < 0:
            raise ValueError("worker_restart_limit cannot be negative")
        if workers == 1 or os.environ.get("PYFERRUM_WORKER"):
            entry, dev, debug = getattr(self, "_pyferrum_launch_info", (sys.argv[0], None, None))
            print(
                f"Registered routes: {self.route_count()}\n"
                f"App: {entry}\n"
                f"Dev mode: {'enabled' if dev else 'disabled' if dev is not None else 'not applicable'}\n"
                f"Debug mode: {'enabled' if debug else 'disabled' if debug is not None else 'not applicable'}\n",
                flush=True,
            )
            kwargs.setdefault("reuse_port", workers > 1)
            super().run(*args, **kwargs)
            return
        _run_workers(workers, restart_limit)


# Attach the HTTP method decorators as class methods.
for _method in _HTTP_METHODS:
    setattr(Ferrum, _method, _make_method_decorator(_method))
del _method


def _run_workers(workers: int, restart_limit: int) -> None:
    command = [sys.executable, *sys.argv]
    children = []
    restarts = 0
    stopping = False

    def start_worker(index: int) -> subprocess.Popen:
        environment = {
            **os.environ,
            "PYFERRUM_WORKER": str(index + 1),
            "PYTHONUNBUFFERED": "1",
        }
        return subprocess.Popen(command, cwd=str(Path.cwd()), env=environment)

    def stop_workers(signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
        for _, process in children:
            if process.poll() is None:
                process.send_signal(signum)

    previous_sigterm = signal.signal(signal.SIGTERM, stop_workers)
    previous_sigint = signal.signal(signal.SIGINT, stop_workers)
    try:
        children.extend((index, start_worker(index)) for index in range(workers))
        while not stopping:
            for index, process in list(children):
                status = process.poll()
                if status is None:
                    continue
                children.remove((index, process))
                if status == 0 or restarts >= restart_limit:
                    stopping = True
                    break
                restarts += 1
                children.append((index, start_worker(index)))
            if not stopping:
                time.sleep(0.1)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        for _, process in children:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        deadline = time.time() + 10
        for _, process in children:
            try:
                process.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                process.kill()


def run(
    app: Ferrum,
    host: str = "127.0.0.1",
    port: int = 8080,
    request_timeout_seconds: float = 30.0,
    body_timeout_seconds: float = 30.0,
    shutdown_timeout_seconds: float = 10.0,
    security_headers_enabled: bool = True,
    cors_enabled: bool = False,
    cors_allowed_origins: list[str] | None = None,
    cors_allowed_methods: list[str] | None = None,
    cors_allowed_headers: list[str] | None = None,
    cors_exposed_headers: list[str] | None = None,
    cors_allow_credentials: bool = False,
    cors_max_age: int = 86400,
    rate_limit_requests: int = 0,
    rate_limit_window_seconds: float = 60.0,
    max_rate_limit_clients: int = 10_000,
    max_header_size: int | None = None,
    max_header_count: int | None = None,
    max_in_flight_requests: int = 1000,
    trusted_proxy_hosts: list[str] | None = None,
    tls_cert_file: str | None = None,
    tls_key_file: str | None = None,
    tls_handshake_timeout_seconds: float = 10.0,
    request_logging: bool = False,
    compression_enabled: bool = True,
    compression_threshold: int = 1024,
    metrics_enabled: bool = True,
    connection_timeout_seconds: float = 120.0,
    workers: int = 1,
    worker_restart_limit: int = 3,
) -> None:
    """Run a `Ferrum` app on the given host/port. Blocks the calling thread."""
    app.run(
        host=host,
        port=port,
        request_timeout_seconds=request_timeout_seconds,
        body_timeout_seconds=body_timeout_seconds,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
        security_headers_enabled=security_headers_enabled,
        cors_enabled=cors_enabled,
        cors_allowed_origins=cors_allowed_origins,
        cors_allowed_methods=cors_allowed_methods,
        cors_allowed_headers=cors_allowed_headers,
        cors_exposed_headers=cors_exposed_headers,
        cors_allow_credentials=cors_allow_credentials,
        cors_max_age=cors_max_age,
        rate_limit_requests=rate_limit_requests,
        rate_limit_window_seconds=rate_limit_window_seconds,
        max_rate_limit_clients=max_rate_limit_clients,
        max_header_size=max_header_size,
        max_header_count=max_header_count,
        max_in_flight_requests=max_in_flight_requests,
        trusted_proxy_hosts=trusted_proxy_hosts,
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        tls_handshake_timeout_seconds=tls_handshake_timeout_seconds,
        request_logging=request_logging,
        compression_enabled=compression_enabled,
        compression_threshold=compression_threshold,
        metrics_enabled=metrics_enabled,
        connection_timeout_seconds=connection_timeout_seconds,
        workers=workers,
        worker_restart_limit=worker_restart_limit,
    )
