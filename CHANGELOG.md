# Changelog

Notable changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) and the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [1.1.0] - 2026-11-28

### Added

- Rust routes can be declared with `export_routes!` and compiled into the
  active pyferrum Python extension with `pyferrum build`.
- `pyferrum verify` checks Python syntax and Rust route manifest/source
  configuration without invoking Cargo.
- `pyferrum run --build` now provides an explicit build-before-run workflow.
  Development reloads build and verify only when `--build` and `--verify` are
  enabled.
- `pyferrum run --core` explicitly bypasses an installed custom route
  extension and uses the original core extension.
- Bundled Rust SDK support for route crates, including the stable JSON-byte
  native route ABI used by the packaged build workflow.

### Changed

- Rust route configuration moved to `[rust.native].manifest` in `config.toml`;
  Rust routes are no longer selected as built-in names in TOML route tables.
- `[[py.routes]]` is now the documented Python route table; legacy
  top-level `[[routes]]` remains accepted for compatibility.
- `pyferrum run` no longer builds or verifies implicitly. It uses an existing
  custom extension when present and otherwise uses the original core extension.
- Updated the default HTTP `Server` header and package/Rust workspace release
  metadata to identify pyferrum 1.1.
- Improved documentation for build, verify, core/custom extension selection,
  development reload behavior, and trusted native-route execution.

### Packaging

- Python package, Rust workspace, core crate, and bundled SDK version set to
  `1.1.0`.
- The SDK and example native route source are included in source distributions;
  route builds can use the bundled SDK without a ferrum-core checkout.

### Security

- Native route code continues to execute synchronously in-process with the
  server process privileges and is explicitly documented as trusted code, not
  a sandbox.

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
