# PyFerrum

[![PyPI version](https://img.shields.io/pypi/v/pyferrum.svg)](https://pypi.org/project/pyferrum/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyferrum.svg)](https://pypi.org/project/pyferrum/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE-MIT)
[![CI](https://github.com/py-ferrum/pyferrum/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/py-ferrum/pyferrum/actions/workflows/ci.yml)

**A Python web framework with a Rust HTTP runtime.** Define routes and
application behavior in Python; pyferrum uses Tokio and Hyper for its server,
router, and selected request/response fast paths.

Version **1.1.0** adds build-before-run Rust routing support. pyferrum is not an
ASGI/WSGI server and is not a drop-in replacement for FastAPI, Flask, or Django.
See the comparison and current limitations below before choosing it.

## Install

requires rust and cargo stable versions to build source or to build rust routes. install and verify with following commands.
```bash
sudo apt update && sudo apt install -y build-essential curl
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
rustc --version
cargo --version
```

pyferrum supports **CPython 3.10 and newer(3.15)**. 
PyPy is not a supported runtime.

Releases provide prebuilt wheels for Linux and Windows, but not macOS. On
macOS, `pip` builds the extension from source, so install Rust with Cargo and
the Xcode Command Line Tools first (`xcode-select --install`).

```bash
python -m pip install pyferrum
```

## Quick start

Save this as `app.py`:

```python
from pyferrum import Ferrum, JsonResponse, Request

app = Ferrum()


@app.get("/")
async def index(request: Request):
    return {"hello": "world"}


@app.get("/users/{user_id}")
def get_user(request: Request):
    return {"id": int(request.path_params["user_id"])}


@app.post("/echo")
async def echo(request: Request):
    return JsonResponse({"echoed": request.json()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
```

Run the app and make a request:

```bash
python app.py
curl http://127.0.0.1:8080/
curl -X POST http://127.0.0.1:8080/echo \
  -H 'Content-Type: application/json' \
  -d '{"message":"hello"}'
```

The `pyferrum` CLI can run an importable app and optionally watch project
Python, Rust, and TOML files:

```bash
pyferrum run --dev --build --verify --debug --host 127.0.0.1 --port 8080
```

By default, `pyferrum run` imports `app:app` from `app.py`. Pass a different
`module:object` value when your application uses another entry point.
`run` never builds or verifies automatically: it uses the custom extension if
one has already been built, otherwise the original core. Use `pyferrum verify`
to check Python syntax and Rust route configuration without compiling, and
`pyferrum build` to compile custom routes. `pyferrum run --build` builds and
then starts the app; `pyferrum run --core` always uses the original core
extension. `--build` and `--core` cannot be combined. In development mode,
`--build` and `--verify` opt in to those steps at startup and after changes.

### Build Rust routes into the Python extension

Define Rust handlers and route entries with `export_routes!`, then point
`rust.native.manifest` in your project's `config.toml` at that Cargo package:

```toml
[rust.native]
manifest = "native/Cargo.toml"

[[py.routes]]
method = "GET"
path = "/from-table"
handler = "example"
```

`[[py.routes]]` entries are for Python handlers; Rust routes remain defined in
Rust source with `export_routes!`.

With pyferrum installed in the same Python environment that will run the app,
build the routes into its Python extension:

```bash
python -m pyferrum.cli build
pyferrum run
```

This atomically replaces the installed `_pyferrum` extension in the active
Python environment; the build does not produce a wheel and does not require a
virtual environment. Rust routes are registered automatically when the app
creates a `Ferrum` instance. The build requires Rust/Cargo and pyferrum
installed in the active Python environment. The SDK used to compile route
crates is bundled with pyferrum; the full ferrum-core source checkout is not
needed. It is installed as `_pyferrum_sdk/` inside the active
`pyferrum` Python package (normally
`site-packages/pyferrum/_pyferrum_sdk`) and `pyferrum build` resolves that
absolute path automatically. Application `Cargo.toml` files should use a normal
`pyferrum-sdk` dependency (or omit it when the CLI injects SDK types);
the build command dynamically rewrites it to the SDK in the active installation.
Native route code runs in process with the server's privileges and must be
trusted. `pyferrum run` uses that extension until rebuilt or run with `--core`.

`--debug` enables detailed server logging by setting `RUST_LOG=debug`; it
does not return Python tracebacks to clients. Otherwise, `RUST_LOG` controls
runtime log levels. The application details are printed on startup; the
listening banner is an info-level log. Access logging is enabled by `--debug` or
`app.run(request_logging=True)` and records the response status, method,
path (without query string), and request ID. Levels are INFO for 1xx/2xx,
DEBUG for 3xx, WARN for 4xx, and ERROR for 5xx; nonstandard 6xx-9xx statuses
are WARN. `RUST_LOG` filters these levels, so 3xx logs require debug logging.
For streaming responses, the logged status is the initial response status,
not the outcome of transmitting the entire body. See `pyferrum run --help`
for the supported options.

## What is included

- Tokio/Hyper HTTP server and a `matchit` router with path parameters and
  404/405 handling.
- Synchronous and asynchronous Python handlers. Async handlers share one
  process-level asyncio event loop; Python handler concurrency is bounded.
- Rust JSON parsing/serialization for supported Python containers, plus
  `Request`, `Response`, and helpers for JSON, text, HTML, XML, CSV, file
  downloads, redirects, and server-sent events.
- Async Python response streaming from bytes/text chunks. Request bodies are
  buffered, with a 10 MiB engine default.
- Request/body/connection/shutdown timeouts, header count and size limits,
  request IDs, configurable security headers, optional fixed-window
  per-client-IP rate limiting, CORS origin/method/header controls, and an
  in-flight request cap.
- Optional PEM-based TLS, trusted-proxy forwarding, gzip for eligible buffered
  responses, and a small Prometheus-format `/metrics` endpoint.
- TOML route tables for explicitly supplied Python handlers.
- Rust routes defined with `export_routes!`, plus static-file and redirect
  helpers.
- Optional Rust native route plugins. Plugins run synchronously in-process,
  are not sandboxed, and must be trusted.
- A `pyferrum` command-line runner and optional worker subprocesses on
  platforms supporting `SO_REUSEPORT`.

The defaults include a 100-handler Python concurrency limit, a 1,000-request
process limit, enabled security headers and gzip, and disabled rate limiting.
See [ARCHITECTURE.md](ARCHITECTURE.md) for request flow, options, and limits.
For the complete Python API, route/response helpers, and server option
reference, see [API.md](API.md).

## Comparison

These projects solve related but different problems. This table compares
their documented roles, not benchmark results.

| Project | Main role | Application interface | Important distinction |
| --- | --- | --- | --- |
| **pyferrum** | Rust HTTP runtime with Python route handlers | pyferrum decorators and request/response types | Does not currently host ASGI/WSGI apps or provide built-in OpenAPI, request-model validation, or a broad extension ecosystem. |
| **FastAPI** | Python ASGI web framework | ASGI, type hints, dependency injection | Includes an established validation and OpenAPI workflow and integrates with ASGI servers such as Uvicorn. |
| **Flask** | Python WSGI web framework | WSGI and Flask extensions | Offers a mature, flexible Python framework and extension ecosystem. |
| **Uvicorn / Hypercorn** | ASGI servers | ASGI application objects | These are servers rather than direct equivalents to a routing framework; they commonly run FastAPI or Starlette apps. |

Choose pyferrum when you want its Rust-backed server and direct Python
decorator API, and its narrower ecosystem meets your needs. Choose FastAPI,
Flask, or Django when their compatibility, validation, middleware, or ecosystem
is a requirement. There are no performance numbers or universal speedup
claims in this README.

## Current limitations

The 1.1.0 API release does not imply feature parity with larger frameworks.
In particular, pyferrum does not currently include:

- ASGI/WSGI hosting or compatibility adapters.
- Built-in authentication/authorization or CSRF middleware, or an ORM.
- OpenAPI generation or built-in schema-based request validation.
- WebSockets or streaming request-body APIs.
- HTTP/3; explicit HTTP/2 TLS ALPN behavior and integration coverage remain
  roadmap work.

Rate limiting is disabled unless configured. TLS is optional and uses
certificate/key files supplied by the application; there is no automatic
certificate provisioning or client-certificate authentication. Security
headers and resource limits are safeguards, not substitutes for application
authentication, authorization, input validation, or deployment controls.
Native plugins execute with the server process's privileges.

## Examples and documentation

- [Detailed Python API reference](API.md)
- [Architecture and runtime behavior](ARCHITECTURE.md)
- [Roadmap](ROADMAP.md)
- [Contribution guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Configurable example application](example-app/README.md)

## License

pyferrum is open-source software licensed under the [MIT License](LICENSE-MIT).

## Contributing and security

Contributions are welcome; start with [CONTRIBUTING.md](CONTRIBUTING.md).
Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md),
not in a public issue.

## Publishing

Maintainers publish releases through the GitHub Actions workflow using PyPI
Trusted Publishing. The workflow and required publisher configuration are in
[`.github/workflows/release.yml`](.github/workflows/release.yml).
