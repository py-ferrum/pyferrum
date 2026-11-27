# pyferrum architecture

This document describes the **implemented 1.0.0 design**. Planned work is
tracked separately in [ROADMAP.md](ROADMAP.md); a proposal is not a feature
commitment.

## Components

```text
Client
  │ HTTP
  ▼
pyferrum worker process
  ├─ Hyper connection handling and Tokio runtime
  ├─ request limits, timeouts, rate limiting, IDs, metrics
  ├─ matchit router
  ├─ Rust handlers ── built-in health, static responses, redirects, plugins
  └─ Python handler adapter (PyO3)
       ├─ sync callable on Tokio's blocking pool
       └─ async callable submitted to the process-level asyncio loop
```

The Python package in `python/pyferrum` provides the application class,
decorators, route-file loader, and CLI. The native extension and Rust server
are in `crates/ferrum-core`. Rust-only handlers avoid Python dispatch; Python
handlers still execute Python code and follow normal CPython GIL behavior.
This design does not make Python business logic execute at Rust speed.

## Source layout

```text
Cargo.toml                         workspace metadata and shared dependencies
pyproject.toml                     Python package and maturin configuration
crates/ferrum-core/
  src/
    handler.rs                     Rust handler interface
    router.rs                      method/path matching
    request.rs, response.rs        engine request and response types
    json.rs                        JSON conversion helpers
    python/                        PyO3 app, handler, request, response, loop
    server/
      config.rs                    server defaults and limits
      dispatch.rs                  per-request processing and response rendering
      lifecycle.rs                 listener, TLS, connections, shutdown
      metrics.rs                   request counters and rate limiter
    native_abi.rs, plugins.rs      native route ABI and trusted plugin loader
python/pyferrum/                   public Python API and CLI
tests/                             Rust/Python behavior and integration tests
example-app/                       runnable app, including native routes
```

## Request lifecycle

1. The Tokio listener accepts a connection. If both TLS certificate and key
   paths are configured, rustls performs a TLS handshake subject to its
   handshake deadline.
2. Hyper parses the HTTP request. The engine rejects requests over the
   configured header count or byte limit with `431`.
3. Request IDs are accepted from `X-Request-ID` when non-empty and at most
   128 characters; otherwise the server generates one. Optional request
   logging records the response status, method, path (without the query
   string), and request ID after a response is selected, including early
   rejections. The log level is INFO for 1xx/2xx, DEBUG for 3xx, WARN for
   4xx or nonstandard 6xx-9xx, and ERROR for 5xx, subject to `RUST_LOG`
   filtering. For streaming responses this is the initial status; body errors
   may occur after the access log.
4. CORS is disabled by default. When enabled, exact allowed origins receive
   `Access-Control-Allow-Origin`; credentialed wildcard use echoes the request
   origin. Browser `OPTIONS` preflight requests are checked against configured
   methods and headers (including `Authorization` by default) and are
   short-circuited before routing.
5. The fixed-window per-IP limiter runs if enabled. A `/metrics` request is
   served by Rust when metrics are enabled.
6. The process-level in-flight request cap is acquired. The body is then
   buffered under the 10 MiB default and body-read deadline. Oversized bodies
   receive `413`; body-read failures receive `400`; body timeouts receive
   `504`.
7. The router matches method and path, extracts path parameters, and returns
   `404` or `405` for unmatched requests.
8. A Rust handler responds in the engine, or the Python adapter invokes the
   Python callable. Synchronous callables run on Tokio's blocking pool.
   Asynchronous callables are submitted to a persistent asyncio loop running
   on a dedicated thread shared by the process.
9. Supported Python return values are converted to Rust responses. JSON
   values use the engine's serde-based conversion; supported values include
   dictionaries, lists, tuples, strings, bytes, `Response`, and
   `StreamingResponse`.
10. The engine adds response headers such as `X-Request-ID`, the configured
   `Server` value, and the default security headers. Eligible buffered
   responses may be gzipped. Hyper writes the result to the connection.

The request body is fully buffered before route dispatch. Streaming is
currently a **response-only** facility: an async iterator may yield bytes or
strings, and a bounded channel applies backpressure to its producer. This
does not provide streaming request bodies or guarantee cancellation of
synchronous Python work after a timeout.

Python response helpers provide HTML and XML from caller-serialized text,
CSV serialization, buffered file downloads, redirects, and SSE framing on
the existing async response stream. File downloads read the entire file into
memory and do not implement ranges. SSE connections are still subject to the
configured connection lifetime and shutdown deadline; reconnection and
event replay are the application's responsibility.

## Python boundary and concurrency

The handler adapter owns invocation and result conversion. For async handlers,
the adapter schedules the coroutine on the process-level event loop rather
than creating a new loop for each request. The event loop is not one loop per
app or per worker thread.

Two independent limits bound work:

- `Ferrum(max_python_in_flight=100)` limits concurrent Python handler work per
  application instance. Saturated Python dispatch receives `503`.
- `app.run(max_in_flight_requests=1000)` limits process-level in-flight HTTP
  requests. Set it to `0` to disable this cap.

`request_timeout_seconds` bounds handler response time. A timeout returns
`504`; it is not a general-purpose way to forcibly stop synchronous Python
code. Request bodies, connections, TLS handshakes, and shutdown have separate
deadlines.

## Implemented server controls

The Python `app.run()` API exposes server options. Relevant defaults and
behavior:

| Control | Default | Behavior |
| --- | ---: | --- |
| Request body size | 10 MiB | Engine limit; not currently configurable through the Python `run()` API. |
| Body/request deadlines | 30 seconds each | Expired body or handler work returns `504`. |
| Connection lifetime | 120 seconds | Bounds an accepted connection. |
| Shutdown drain | 10 seconds | Stops accepting and drains active connections until the deadline. |
| Header size/count | 64 KiB / 128 | Can be overridden through `app.run()`. |
| Process in-flight requests | 1,000 | `0` disables the cap. |
| Python handler concurrency | 100 | Configured on `Ferrum(...)`; excess work receives `503`. |
| Rate limiting | Disabled | Fixed-window, per remote IP; configure with `rate_limit_requests` and `rate_limit_window_seconds`. |
| Security headers | Enabled | Emits `X-Content-Type-Options`, `X-Frame-Options`, and `Referrer-Policy`. |
| CORS | Disabled | Exact-origin allowlist, preflight methods/headers, exposed headers, credentials, and max-age. |
| Gzip | Enabled | Applies to buffered responses meeting the threshold when the client accepts gzip. |
| Metrics | Enabled | `/metrics` exposes request total and in-flight counters. |
| TLS | Disabled | Configure both `tls_cert_file` and `tls_key_file` to enable PEM-based TLS. |

Rate limiting is per process and is not a distributed limiter. Its in-memory
client table is bounded. The metrics endpoint exposes a small set of counters,
not a full Prometheus instrumentation suite.

## Routes that stay in Rust

Python decorators register Python handlers. The `Ferrum` wrapper also exposes
these startup-time Rust routes:

- `app.static_file(path, file_path, content_type=..., method="GET")` reads the
  file at registration and serves the in-memory bytes.
- `app.redirect(path, location, status_code=302)` registers a GET redirect.
- `app.load_routes(...)` can load a TOML `[[routes]]` table. Python handlers
  must be supplied explicitly; built-in Rust handler names are `health`,
  `liveness`, and `readiness`.
- The CLI can build and load an optional native `cdylib` configured by
  `[tool.pyferrum].rust_manifest`.

Native plugins use the versioned JSON-byte ABI in `native_abi.rs`. They are
synchronous, buffered, execute in-process, and are **not sandboxed**. Plugin
code runs with the server process's privileges. Plugin response buffers are
limited to 128 MiB; only load code you trust.

## Workers and deployment

`app.run(workers=N)` starts N application subprocesses and uses
`SO_REUSEPORT` to share the listening address on supported Unix platforms.
Each worker has its own Python interpreter, GIL, Tokio runtime, and app state.
The lightweight parent restarts a worker that exits non-zero up to
`worker_restart_limit`; it is not a general-purpose process supervisor.

Use one worker where `SO_REUSEPORT` is unavailable or unverified. A reverse
proxy or external process manager may be used for TLS termination, traffic
distribution, and supervision. Forwarded client metadata is used only when
the connecting proxy IP is in `trusted_proxy_hosts`; do not trust arbitrary
client-supplied forwarding headers.

Native TLS reads PEM certificate/key files at startup. Certificate rotation
requires restarting workers. TLS does not configure client-certificate
authentication or automatic certificate provisioning. The documented and
integration-tested protocol contract is HTTP/1.1; explicit HTTP/2 TLS ALPN
configuration and protocol integration coverage remain roadmap work.

## Extension boundaries

`crates/ferrum-core/src/handler.rs` defines the Rust handler interface.
Pure-Rust routes can implement it without Python. Python route handlers use
the PyO3 adapter. CORS is an engine-level server policy rather than a general
Python middleware extension. The public extension boundary does not include an
ASGI/WSGI adapter or request-body streaming API; see [ROADMAP.md](ROADMAP.md)
for possible future work.
