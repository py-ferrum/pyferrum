# Contributing to pyferrum

Thanks for considering a contribution. For feature proposals and architectural
changes, open an issue first so the intended behavior and public API can be
discussed. Small fixes and documentation improvements can go directly to a
pull request.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). Security reports
should be sent privately as described in [SECURITY.md](SECURITY.md).

## Development setup

Requirements: stable Rust with Cargo, CPython 3.10+, and a supported platform
for building the native extension. Use a virtual environment for Python tools
and packages. Keep Cargo build output inside the environment if you want to
avoid workspace build artifacts:

On macOS, install the Xcode Command Line Tools with `xcode-select --install`
before building.

```bash
git clone https://github.com/py-ferrum/pyferrum.git
cd pyferrum
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install maturin ruff
export CARGO_TARGET_DIR="$PWD/.venv/cargo-target"
maturin develop --release
```

If you are working on benchmark comparisons, install the optional dependencies
with `python -m pip install -e ".[benchmark]"`. They are not required for the
core test suite.

## Checks

Run the Rust checks from the repository root:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --no-fail-fast
```

Run Python lint and the CI integration tests in the active virtual environment:

```bash
ruff check python/ example-app/ tests/
ruff format --check python/ example-app/ tests/
python tests/test_e2e.py
python tests/test_cli.py
```

Additional behavior tests can be run individually:

```bash
python tests/test_middleware.py
python tests/test_overload.py
python tests/test_reliability.py
python tests/test_routes_file.py
python tests/test_rust_routes.py
python tests/test_tls.py
python tests/test_workers.py
```

`tests/test_perf_async.py` requires `aiohttp`; performance scripts are smoke
tests and must not be used as published comparative results without a
controlled, reproducible benchmark setup.

## Pull request checklist

- Keep changes focused and preserve existing behavior unless a change is
  intentional and documented.
- Add or update focused tests for behavior changes.
- Run the relevant Rust and Python checks above.
- Update user-facing documentation and `CHANGELOG.md` when behavior or public
  APIs change.
- Explain the motivation, implementation, tests, and any limitations in the
  pull request description. Link a related issue when one exists.
- Do not include credentials, private data, generated build output, or
  unrelated formatting changes.

Maintainers review changes for correctness, compatibility, security impact,
and clarity. A review or merge is not a promise that a feature will be
included in a particular release.

## Design guidance

Keep HTTP parsing, routing, limits, and Rust-only handlers in
`crates/ferrum-core` where appropriate. Python application behavior belongs in
the public Python API or handler adapter. Avoid adding framework compatibility
or performance claims unless they are implemented and tested. Native plugins
are trusted in-process code; do not treat their ABI as a sandbox.

## Release process

Maintainers update the package and workspace versions together, review the
changelog and supported-platform matrix, run CI, and publish a `vX.Y.Z` tag.
The release workflow builds distribution artifacts and uses PyPI Trusted
Publishing. See [`.github/workflows/release.yml`](.github/workflows/release.yml)
for the exact workflow.
