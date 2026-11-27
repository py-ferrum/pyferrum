# Security policy

## Supported versions

Security fixes are intended for the latest supported 1.x release. Pre-1.0
versions are no longer in the support window. If you are unsure whether a
version is supported, check the latest release and contact the maintainers
privately.

## Reporting a vulnerability

Do not open a public issue or pull request for a suspected vulnerability.
Use GitHub's private vulnerability reporting for
[py-ferrum/pyferrum](https://github.com/py-ferrum/pyferrum/security/advisories/new).
Include the affected version, impact, a minimal reproduction, and any
mitigation you have identified. Avoid sending credentials or unrelated
personal data.

Maintainers will coordinate investigation, a fix, and disclosure through the
private advisory. No response-time or patch-time guarantee is made.

## Security model and limitations

pyferrum provides server-level safeguards, but does not secure an application
by itself:

- The engine buffers request bodies, with a 10 MiB default limit. It also
  supports header limits, body/request/connection deadlines, bounded
  concurrency, and graceful shutdown.
- Security response headers are enabled by default. Fixed-window per-IP rate
  limiting is available but disabled by default; it is process-local, not a
  distributed control.
- TLS is optional and configured with PEM certificate and key files. The
  server does not provision certificates or authenticate clients with
  certificates. Certificate rotation requires a worker restart.
- Forwarded client metadata is trusted only for configured proxy addresses.
  Keep `trusted_proxy_hosts` accurate and do not accept arbitrary forwarded
  headers as proof of client identity.
- CORS is configurable but disabled by default. The framework does not provide
  built-in authentication, authorization, CSRF policy, or request-model
  validation. Applications must implement
  and test those controls for their own threat model.
- Native route plugins execute in the server process with its privileges.
  They are not sandboxed and must be treated as trusted native code.
- Python handlers execute application code. Resource limits do not make
  untrusted handlers safe; synchronous code may continue after a request
  timeout.

Place pyferrum behind an appropriately configured proxy or deployment
boundary when required by your threat model. Configure TLS, authentication,
authorization, request validation, secrets management, and observability for
the application rather than assuming the framework supplies them.

## Dependency checks

The CI workflow runs `cargo audit` as a non-blocking review job. It does not
run `pip-audit` or claim that all dependencies are vulnerability-free.
Report relevant dependency issues through the same private reporting process.
