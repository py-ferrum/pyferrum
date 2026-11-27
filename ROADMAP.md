# pyferrum roadmap

pyferrum 1.0.0 is the first stable public API baseline. The project is
intentionally narrower than full Python web frameworks. This roadmap tracks
unimplemented work; it does not imply a release date or guarantee that every
item will be built. Priorities may change with user feedback.

## 1.0.0 baseline

Implemented and described in [ARCHITECTURE.md](ARCHITECTURE.md):

- Rust HTTP runtime, method/path routing, Python sync and async handlers, and
  a process-level asyncio loop.
- Bounded request bodies and concurrency, configurable limits and deadlines,
  request IDs, optional fixed-window IP rate limiting, and default security
  headers.
- Optional native TLS, trusted-proxy forwarding, gzip for buffered responses,
  basic request metrics, response streaming, and Unix worker subprocesses.
- TOML routes, Rust built-in health handlers, static-file/redirect fast paths,
  and a trusted synchronous native-plugin ABI.

## Priorities for future 1.x work

### Protocol and runtime verification

- Add explicit HTTP/2 TLS ALPN configuration and integration tests before
  documenting HTTP/2 as supported.
- Expand protocol and platform integration coverage, including TLS,
  shutdown, worker behavior, and packaging across supported targets.
- Clarify timeout and cancellation behavior for synchronous handlers and
  streaming responses; preserve bounded work under disconnects and errors.
- Consider an in-process test client after the public app lifecycle is stable.

### Application features

- Consider broader composable middleware and CSRF controls after the server
  CORS policy API has established ordering and validation behavior.
- Add OpenAPI generation and optional schema-validation integrations only
  after defining a small, stable route metadata API.
- Consider an ASGI adapter if compatibility with existing ASGI applications
  is a concrete goal. Do not claim that pyferrum's router accelerates an app
  that is still routed by an external ASGI framework.
- Explore WebSockets and streaming request bodies only
  with explicit backpressure, cancellation, and resource-limit semantics.

### Operations and measurement

- Add richer metrics and tracing integration without turning the small
  built-in `/metrics` counters into a broader guarantee.
- Maintain reproducible comparisons with equivalent Flask/Gunicorn and
  FastAPI/Uvicorn deployments. Report workload, configuration, latency,
  throughput, errors, and resource use; publish no universal speedup claim.
- Review dependency-audit coverage and make its reporting and release policy
  explicit.
- Document worker limitations and deployment examples for supported
  platforms.

## Longer-term possibilities

The following are exploratory and have no committed schedule:

- HTTP/3, subject to transport maturity and demand.
- Authentication helpers, database/ORM integrations, caching, background
  jobs, or additional protocol integrations.
- Framework migration tools or broad FastAPI/Flask/Django compatibility.
- A richer worker supervisor and additional observability exporters.

The project does not plan to reimplement every framework ecosystem component.
Prefer interoperability with established Python libraries where practical.

## Proposing roadmap changes

For substantial features, open an issue describing the use case, affected
public API, operational/security consequences, and alternatives. A design
discussion is not a promise of implementation. Small fixes and documentation
improvements can be proposed directly in a pull request; see
[CONTRIBUTING.md](CONTRIBUTING.md).
