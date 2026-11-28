# pyferrum API reference

This document describes the public Python API in pyferrum 1.1.0. It covers
application setup, route registration, requests and responses, server options,
and the command-line runner. For architecture and execution order, see
[ARCHITECTURE.md](ARCHITECTURE.md). For a runnable app, see
[example-app/](example-app/).

## Application

### `Ferrum`

```python
from pyferrum import Ferrum

app = Ferrum(
    server_name: str = "pyferrum/1.1",
    max_python_in_flight: int = 100,
)
```

Creates an application and its route table.

| Argument | Meaning |
| --- | --- |
| `server_name` | Value of the HTTP `Server` response header. An empty string disables the header. |
| `max_python_in_flight` | Maximum concurrent Python handler invocations for this application instance. Must be greater than zero; excess Python dispatch receives `503 Service Unavailable`. |

Register routes before calling `run()`. Routes are moved into the server when
it starts; the same application instance is not intended to be restarted.

### Route decorators

The application supports `get`, `post`, `put`, `patch`, `delete`, `head`, and
`options`:

```python
@app.get("/users/{user_id}")
async def get_user(request):
    return {"id": request.path_params["user_id"]}
```

Each decorator takes one route pattern and returns a decorator that registers
the function and returns it unchanged. A route handler accepts one `Request`
and may be synchronous or asynchronous. Path parameters use braces, such as
`{user_id}`. Duplicate or invalid registrations raise `ValueError`.

The HTTP method and route pattern together identify a route. A path that has
routes registered for other methods returns `405 Method Not Allowed`; a path
that does not match any route returns `404 Not Found`.

### `route_count()`

Returns the number of registered routes. Once `run()` has started, routes have
been moved into the server; after it returns, this count reflects the
application's remaining route table.

### `load_routes(route_file, python_handlers=None)`

Loads a TOML file containing a `[[py.routes]]` array and returns its entry
count. Each table requires string `method`, `path`, and `handler` values.
`runtime` defaults to `"python"`; `"rust"` is not supported. Legacy files
with top-level `[[routes]]` are also accepted.

```toml
[[py.routes]]
method = "GET"
path = "/hello"
handler = "hello"
```

```python
app.load_routes(
    "config.toml",
    python_handlers={"hello": hello_handler},
)
```

Python handlers must be explicitly supplied in `python_handlers`; TOML does
not import or execute arbitrary Python names. Rust routes must be implemented
in Rust with `export_routes!` and compiled into the installed extension with
`pyferrum build`. TOML entries that specify `runtime = "rust"` raise
`ValueError`. Invalid TOML, missing files, unsupported runtimes, unknown
handlers, or invalid routes also raise `ValueError`. If loading fails partway
through, routes registered earlier in that call are not rolled back.

### `static_file(path, file_path, content_type="application/octet-stream", method="GET")`

Registers a Rust-served route. The file is read into memory immediately when
the route is registered; later changes to the file are not reflected. File
read failures raise `OSError`; invalid methods, paths, or content types raise
`ValueError`. This is not a streaming or range-request file server.

### `redirect(path, location, status_code=302)`

Registers a Rust-served `GET` redirect. The status must be in the 3xx range.
Invalid status codes or locations raise `ValueError`. Validate user-provided
redirect destinations to prevent open redirects.

## Request object

Each handler receives one `Request`:

| Attribute | Type | Description |
| --- | --- | --- |
| `method` | `str` | HTTP method, such as `"GET"` or `"POST"`. |
| `path` | `str` | URL path without the query string. |
| `query_string` | `str` | Raw query string without the leading `?`; empty when absent. |
| `query_params` | `dict[str, str]` | URL-decoded query values. Repeated keys are joined with commas; this is a flat mapping, not a multi-value mapping. |
| `path_params` | `dict[str, str]` | Named route parameters, such as `{"user_id": "42"}`. |
| `headers` | `dict[str, str]` | Request headers whose values can be represented as text. Header names are normalized to lowercase by the HTTP parser. |
| `body` | `bytes` | Fully buffered request body. |
| `remote_addr` | `str \| None` | Client socket address, or the forwarded address when the peer is a configured trusted proxy. |
| `scheme` | `str` | `"http"` or `"https"`; trusted forwarded protocol is used for configured proxies. |

Use `request.headers.get("authorization")` to read an `Authorization` header.
The framework does not parse credentials, authenticate a principal, or enforce
permissions.

### Request methods

- `request.text()` decodes the body as UTF-8. Invalid UTF-8 raises `ValueError`.
- `request.json()` parses a JSON body and returns the corresponding Python
  value. Invalid JSON raises `ValueError`.

Request bodies are fully buffered before a handler runs. The default engine
limit is 10 MiB. This limit is not currently exposed through the Python
`run()` API.

## Handler return values

A handler can return:

- `Response` or `StreamingResponse` for explicit status, headers, and content
  type.
- A `dict`, `list`, tuple, or another supported JSON-compatible value for a
  pretty-printed JSON response.
- `str` for a UTF-8 `text/plain` response.
- `bytes` for an `application/octet-stream` response.

Unsupported objects and serialization errors are reported as handler errors;
they are not silently converted to strings.

## Response types and helpers

### `Response`

```python
Response(body, status_code=200, content_type="application/json", headers=None)
Response.json(data, status_code=200, headers=None)
Response.text(text, status_code=200, headers=None)
```

`body` is bytes-like input; `headers` is an optional mapping of string names
to string values. `Response.json` serializes JSON-compatible values. Use
`JsonResponse` or `TextResponse` below for the same common response patterns.

### `StreamingResponse`

```python
StreamingResponse(
    source,
    status_code=200,
    content_type="application/octet-stream",
    headers=None,
)
```

`source` must be an asynchronous iterable that yields only `bytes` or `str`.
The stream uses a bounded channel to apply backpressure. Once response headers
have been sent, a stream failure ends the stream; the server cannot replace
the already-sent status with an error response.

### Convenience response helpers

```python
JsonResponse(data, status_code=200, headers=None)
TextResponse(text, status_code=200, headers=None)
HtmlResponse(html, status_code=200, headers=None)
XmlResponse(xml, status_code=200, headers=None)
CsvResponse(rows, status_code=200, headers=None)
```

- `JsonResponse` returns JSON.
- `TextResponse` returns UTF-8 `text/plain`.
- `HtmlResponse` returns UTF-8 `text/html`; callers must escape untrusted
  values before embedding them in markup.
- `XmlResponse` returns UTF-8 `application/xml`; callers supply serialized
  XML.
- `CsvResponse` serializes an iterable of rows with Python's CSV writer.
  Spreadsheet-formula injection in untrusted cells is not neutralized.

### `FileResponse`

```python
FileResponse(
    path,
    filename=None,
    content_type=None,
    status_code=200,
    headers=None,
)
```

Reads the entire file into memory and returns it as an attachment. The
filename defaults to the basename of `path`, and content type is inferred from
the filename when not specified. A filename must not be empty, `"."`, `".."`,
contain a path separator, or contain control characters. File errors
propagate. This helper does not stream or support byte ranges; validate
untrusted paths and permissions in application code.

### `RedirectResponse`

```python
RedirectResponse(location, status_code=302, headers=None)
```

Returns an empty-body redirect. Supported status codes are `301`, `302`,
`303`, `307`, and `308`. A location must be non-empty and contain no control
characters. Validate destinations supplied by clients.

### Server-sent events

```python
ServerSentEvent(data, event=None, id=None, retry=None)
EventSourceResponse(source, status_code=200, headers=None)
```

`source` is an asynchronous iterable of strings or `ServerSentEvent`
instances. `ServerSentEvent.encode()` formats an event according to the SSE
wire format, splitting multiline data into `data:` fields. `event` and `id`
cannot contain CR, LF, or NUL; `retry` must be a non-negative integer.
`EventSourceResponse` uses `text/event-stream; charset=utf-8` and defaults to
`Cache-Control: no-cache`. A caller-provided cache-control header is preserved.
Connection lifetime and shutdown deadlines also apply to SSE streams.

## Running the server

### `app.run(...)`

`app.run()` starts the server and blocks the calling thread. The supported
options are:

| Option | Type | Default | Description |
| --- | --- | ---: | --- |
| `host` | `str` | `"127.0.0.1"` | Bind address. |
| `port` | `int` | `8080` | Listening port. |
| `request_timeout_seconds` | `float` | `30` | Maximum handler response time. Expiry returns `504`; it cannot forcibly stop synchronous Python code already running. |
| `body_timeout_seconds` | `float` | `30` | Maximum time to collect a request body. |
| `shutdown_timeout_seconds` | `float` | `10` | Maximum drain time during shutdown. |
| `connection_timeout_seconds` | `float` | `120` | Maximum lifetime of an accepted connection. |
| `security_headers_enabled` | `bool` | `True` | Add `X-Content-Type-Options`, `X-Frame-Options`, and `Referrer-Policy`. |
| `request_logging` | `bool` | `False` | Emit status-dependent access logs with method, path, and request ID. |
| `compression_enabled` | `bool` | `True` | Enable gzip compression for eligible buffered responses. |
| `compression_threshold` | `int` | `1024` | Minimum buffered response size, in bytes, eligible for gzip. |
| `metrics_enabled` | `bool` | `True` | Serve basic request counters at `/metrics`. |
| `max_in_flight_requests` | `int` | `1000` | Process-level request cap; `0` disables this cap. |
| `max_header_size` | `int \| None` | `None` | Maximum aggregate request-header size; `None` uses 64 KiB. |
| `max_header_count` | `int \| None` | `None` | Maximum request header count; `None` uses 128. |
| `rate_limit_requests` | `int` | `0` | Requests allowed per fixed window and remote IP; `0` disables rate limiting. |
| `rate_limit_window_seconds` | `float` | `60` | Fixed-window duration when rate limiting is enabled. |
| `max_rate_limit_clients` | `int` | `10000` | Maximum tracked client IPs; must be positive when rate limiting is enabled. |
| `trusted_proxy_hosts` | `list[str] \| None` | `None` | IP addresses allowed to supply `X-Forwarded-For` and `X-Forwarded-Proto`. |
| `tls_cert_file` | `str \| None` | `None` | PEM certificate path. |
| `tls_key_file` | `str \| None` | `None` | PEM private-key path. Set both certificate and key to enable TLS. |
| `tls_handshake_timeout_seconds` | `float` | `10` | Maximum TLS handshake time. |
| `reuse_port` | `bool` | `False` | Allow multiple worker processes to bind the same address where supported. |
| `workers` | `int` | `1` | Number of Python worker subprocesses. |
| `worker_restart_limit` | `int` | `3` | Maximum worker restarts after non-zero exit. |

Invalid durations, limits, proxy addresses, or worker settings raise
`ValueError`. Listener and TLS initialization errors raise `OSError`.
`workers > 1` requires platform support for `SO_REUSEPORT`; each process has
independent app state, metrics, and rate-limit state.

The top-level convenience function `run(app, ...)` accepts the same server
options as keyword arguments, with the same defaults, except that `app` is
required as its first argument.

## CORS

CORS is disabled by default. It controls whether a browser may expose a
cross-origin response to frontend JavaScript; it is **not** authentication,
authorization, or a request firewall. A disallowed origin on a normal request
does not stop the request handler from running; the response simply has no
CORS allow-origin header. Enforce access permissions in application code.

| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `cors_enabled` | `bool` | `False` | Enable CORS response headers and preflight handling. |
| `cors_allowed_origins` | `list[str] \| None` | `None` (no origins allowed) | Exact origins allowed, for example `https://app.example`. Use `"*"` only for non-credentialed public access. |
| `cors_allowed_methods` | `list[str] \| None` | `GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS` | Methods accepted in preflight requests. |
| `cors_allowed_headers` | `list[str] \| None` | `Authorization, Content-Type, Accept, Origin, X-Requested-With` | Headers accepted in preflight requests. |
| `cors_exposed_headers` | `list[str] \| None` | Empty | Response headers browser JavaScript may read. |
| `cors_allow_credentials` | `bool` | `False` | Include `Access-Control-Allow-Credentials: true`. |
| `cors_max_age` | `int` | `86400` | Preflight cache duration in seconds. |

Example:

```python
app.run(
    host="127.0.0.1",
    port=8080,
    cors_enabled=True,
    cors_allowed_origins=["https://frontend.example"],
    cors_allowed_methods=["GET", "POST", "OPTIONS"],
    cors_allowed_headers=["Authorization", "Content-Type"],
    cors_exposed_headers=["X-Request-ID"],
    cors_allow_credentials=True,
    cors_max_age=600,
)
```

Origins are matched exactly; scheme, host, and port are significant. Preflight
`OPTIONS` requests are short-circuited with `204 No Content` when the origin,
requested method, and requested headers are allowed. A rejected CORS preflight
returns `403 Forbidden`. The default allowed request-header list includes
`Authorization`, so a browser can make a preflight request for bearer-token
headers once CORS is enabled.

For credentialed access, configure explicit origins rather than `"*"`. The
application must independently validate credentials and authorize requests.
For example:

```python
@app.get("/profile")
def profile(request):
    authorization = request.headers.get("authorization")
    if authorization != "Bearer expected-token":
        return JsonResponse({"error": "unauthorized"}, status_code=401)
    return {"user": "example"}
```

The example above is illustrative only; production authentication should use
an appropriate token-validation strategy and secret management.

## Command line

Run the default application (`app:app` from `app.py`):

```sh
pyferrum run --host 127.0.0.1 --port 8080
```

For a different entry point, pass `module:object`, for example
`pyferrum run server:web`.

Useful options:

- `--dev` watches Python, Rust, and TOML project files and reloads after
  changes. Building and verification remain opt-in with `--build` and
  `--verify`.
- `--build` builds the configured Rust routes before starting the server.
- `--core` bypasses routes integrated into the custom extension and uses the
  original core extension.
- `--verify` checks project Python syntax and Rust route configuration before
  starting; it does not run Cargo.
- `--debug` enables debug-level server logs and access logging.
- `--host` and `--port` select the bind address.

`pyferrum run` does not build or verify automatically. It uses an already-built
custom extension when present, otherwise the original core. Use
`pyferrum verify` to check without compiling, `pyferrum build` to compile
without starting the server, or `pyferrum run --build` to build and then run.
`--build` and `--core` are mutually exclusive. See `pyferrum run --help` for
the installed version's options. Debug logging does not expose handler
tracebacks in HTTP responses.

## Runtime limits and security notes

- The default maximum request body is 10 MiB; bodies are buffered before
  dispatch.
- The default header limits are 64 KiB total and 128 fields.
- The process-level request limit defaults to 1,000; Python handler
  concurrency defaults to 100 per app.
- Rate limiting is disabled by default and is per process, not distributed.
- The metrics endpoint is a small built-in counter endpoint, not a full
  Prometheus instrumentation suite.
- TLS uses PEM files and does not provide automatic certificate management
  or client-certificate authentication.
- Forwarded metadata is honored only from configured proxy IPs. Do not trust
  arbitrary client-provided forwarding headers.
- Native route plugins execute trusted native code in-process with the
  server's privileges.

See [SECURITY.md](SECURITY.md) for reporting and security guidance.
