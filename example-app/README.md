# Configurable pyferrum example

This single application demonstrates Python sync and async handlers, path and
query parameters, JSON, text, HTML, XML, CSV, file, redirect, and SSE
responses, streaming, gzip, metrics, request
logging, a Python route loaded from TOML, a static file, a redirect, and
optional custom Rust routes defined with `export_routes!`. It requires
pyferrum installed in your Python environment; no additional Python packages
are needed.

From the repository root:

```sh
cp example-app/.env.example example-app/.env
set -a
. example-app/.env
set +a
python example-app/app.py
```

The application reads environment variables; it does **not** load `.env` on
its own. Edit the copy before sourcing it, and only source trusted files.
The example `.env` documents all options exposed by `Ferrum(...)` and
`app.run(...)`, plus `RUST_LOG` and the optional plugin path. Boolean values
must be `true`, `false`, `1`, or `0`. Empty TLS paths disable TLS; specify
both PEM paths to enable it. Supply trusted proxy IPs as a comma-separated
list, and configure only proxies under your control. Multiple workers require
`SO_REUSEPORT` support; their limits, metrics and state are per process.
Leave `REUSE_PORT` empty for the default behavior, especially with multiple
workers; explicitly disabling it prevents workers from sharing the port.
The request body limit is fixed at 10 MiB in the Python API.
Set `CORS_ENABLED=true` and configure `CORS_ALLOWED_ORIGINS` with exact browser
origins (or `*` without credentialed requests). The default allowed header list
includes `Authorization`, so browser requests can send bearer or other
authorization schemes. CORS controls only browser access; application handlers
must still implement authentication and authorization.

`/healthz`, `/livez`, and `/readyz` are provided by the optional Rust plugin.
They return 404 with the standard Python-only invocation until you build the
custom extension or load the shared library described below.

Try these in another terminal (use HTTPS if TLS is enabled):

```sh
curl http://127.0.0.1:8080/
curl http://127.0.0.1:8080/users/42
curl 'http://127.0.0.1:8080/query?name=example'
curl -X POST http://127.0.0.1:8080/echo -H 'Content-Type: application/json' -d '{"message":"hello"}'
curl http://127.0.0.1:8080/headers -H 'Authorization: Bearer example-token'
curl -i -X OPTIONS http://127.0.0.1:8080/echo \
  -H 'Origin: http://localhost:3000' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: Authorization,Content-Type'
curl -X PATCH 'http://127.0.0.1:8080/inspect/42?view=full' -d 'example'
curl http://127.0.0.1:8080/stream
curl http://127.0.0.1:8080/html
curl http://127.0.0.1:8080/xml
curl http://127.0.0.1:8080/csv
curl -OJ http://127.0.0.1:8080/download
curl -i http://127.0.0.1:8080/redirect
curl -N http://127.0.0.1:8080/events
curl --compressed http://127.0.0.1:8080/large
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/from-table
curl -i http://127.0.0.1:8080/docs
curl http://127.0.0.1:8080/metrics
```

Other Python routes include `/sync`, `/slow`, `/text`, `/headers`, and `/static`.
The plugin's health and readiness responses are static, not dependency checks.
`/headers` echoes all request headers, including
credentials: remove or restrict it before exposing this example to others.
The example has no authentication or authorization and is not production-ready.

## Response helpers

`HtmlResponse` and `XmlResponse` take already serialized UTF-8 text; escape
untrusted input yourself. `CsvResponse` serializes rows using Python's CSV
writer. It does not neutralize spreadsheet formulas in untrusted cells.
All three accept optional `status_code` and `headers`.

`FileResponse(path, filename=None, content_type=None, ...)` reads the whole
file into memory and sends it as an attachment, with MIME type detection, an
ASCII fallback filename, and a UTF-8 `filename*`. Read errors propagate. It
does not support byte ranges
or streaming; never use an untrusted path without validating access.
`RedirectResponse(location, status_code=302, ...)` supports 301, 302, 303,
307, and 308; validate untrusted destinations to avoid open redirects.

`EventSourceResponse` accepts an async iterable of strings or
`ServerSentEvent(data, event=None, id=None, retry=None)`. It frames multiline
data, streams with `text/event-stream`, and defaults to `Cache-Control:
no-cache`. The example stream ends after two events. Long-lived connections
are bounded by `CONNECTION_TIMEOUT_SECONDS` and shutdown deadlines; clients
may reconnect, but replay, heartbeats, and event IDs are application-managed.
Errors after streaming begins terminate the response rather than changing its
status.

## Optional native plugin

For an extension built into this app's pyferrum install, install pyferrum into
the Python environment that will run the app, then build in place:

```sh
cd example-app
python -m pip install pyferrum
python -m pyferrum.cli build
python app.py
```

`pyferrum build` atomically replaces the pyferrum extension in
the active Python environment; the Rust routes are registered automatically
when `app.py` creates its `Ferrum` instance. Rust routes are defined only in
`native/src/lib.rs` with `export_routes!`; `config.toml` contains the
`[rust.native]` manifest setting and the Python `[[py.routes]]` entry for
`/from-table`. The command requires Rust/Cargo and pyferrum installed
in the active Python environment, but does not require a virtual environment
or produce a wheel. A venv remains an optional way to isolate that environment.
The pip-installed Rust plugin SDK is used to compile the route code.
The SDK package location is resolved dynamically by `pyferrum build` from
the active Python environment. It does not require a hardcoded
`site-packages` path or a source-checkout path. The example manifest uses
Cargo workspace inheritance in the repository and `pyferrum build` replaces
that dependency with `pyferrum/_pyferrum_sdk` from the installed wheel or
virtual environment.

The separate shared-library plugin workflow remains available below:

Build the included plugin from the repository root:

```sh
cargo build --manifest-path example-app/native/Cargo.toml
```

Set `NATIVE_LIBRARY` to the absolute path of the resulting shared library
(`example-app/native/target/debug/libnative_plugin.so` on Linux;
the filename differs on other platforms), then start the app as above. It
adds `/healthz`, `/livez`, and `/readyz`. Native plugins
execute trusted code in-process with the server's privileges; they are not
sandboxed. See [the architecture guide](../ARCHITECTURE.md) for limitations.

Run this example with `python example-app/app.py`, not the CLI: the CLI calls
`app.run` itself and therefore does not apply the example's `run()` options
from the environment.
