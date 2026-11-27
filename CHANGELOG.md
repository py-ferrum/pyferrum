# Changelog

Notable changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [Unreleased]

### Added

- Configurable CORS origin, method, request-header, credential, exposed-header,
  and preflight max-age controls. `Authorization` is allowed by the default
  CORS request-header policy and remains available to Python handlers through
  `Request.headers`.
- Python response helpers for HTML, XML, CSV, file downloads, redirects, and
  server-sent events over the existing streaming response support.

## [1.0.0] - 2026-11-27

The first stable 1.x public API baseline. This release reflects the current
implemented feature set; it does not claim compatibility with ASGI/WSGI
frameworks or feature parity with FastAPI, Flask, or Django.

### Added

- Tokio/Hyper HTTP server, method/path routing, sync and async Python handlers,
  and a process-level asyncio event loop.
- Rust JSON conversion, Python request/response types, and async response
  streaming with bounded backpressure.
- Request/body/connection/shutdown timeouts, body/header limits, bounded
  concurrency, request IDs, optional fixed-window IP rate limiting, and
  default security headers.
- Optional PEM-based TLS, trusted-proxy forwarding, gzip for eligible
  buffered responses, and a basic Prometheus-format `/metrics` endpoint.
- TOML route loading, Rust built-in health handlers, static-file and redirect
  routes, and synchronous native route plugins.
- Python CLI runner and optional worker subprocesses using `SO_REUSEPORT` on
  supported Unix platforms.
- Updated architecture, roadmap, security, and contribution documentation.

### Packaging

- Python package and Rust workspace version set to `1.0.0`.
- MIT License; this project is not dual-licensed under Apache-2.0.
- CPython abi3 extension targeting CPython 3.8 and newer. CI tests CPython
  3.8, 3.10, 3.12, and 3.13.

### Limitations

- No ASGI/WSGI hosting, built-in authentication or CSRF middleware,
  OpenAPI generation, ORM, WebSockets, server-sent events, or HTTP/3.
- Request bodies are buffered. HTTP/2 TLS ALPN behavior is not yet documented
  as supported or covered by integration tests.
- Native plugins execute synchronously in-process and are trusted code.
