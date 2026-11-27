"""Command-line entry point for running pyferrum applications."""

import argparse
import importlib
import json
import os
import py_compile
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from . import Ferrum

_IGNORED_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "target", "node_modules"}
_WATCHED_SUFFIXES = {".py", ".rs", ".toml"}


def _project_files(root: Path) -> list[Path]:
    files = []
    for directory, dirs, names in os.walk(str(root)):
        dirs[:] = [name for name in dirs if name not in _IGNORED_DIRS and not name.startswith(".")]
        for name in names:
            path = Path(directory) / name
            if path.suffix in _WATCHED_SUFFIXES:
                files.append(path)
    return files


def _compile_project(root: Path) -> None:
    for path in _project_files(root):
        if path.suffix == ".py":
            py_compile.compile(str(path), doraise=True)


def _build_native(root: Path) -> Path | None:
    config = root / "pyproject.toml"
    if not config.is_file():
        return None
    with config.open("rb") as file:
        document = tomllib.load(file)
    pyferrum = document.get("tool", {}).get("pyferrum", {})
    manifest = pyferrum.get("rust_manifest")
    if manifest is None:
        return None
    if not isinstance(manifest, str) or not manifest:
        raise ValueError("tool.pyferrum.rust_manifest must be a non-empty path")
    manifest_path = (root / manifest).resolve()
    metadata = subprocess.run(
        [
            "cargo",
            "metadata",
            "--format-version",
            "1",
            "--no-deps",
            "--manifest-path",
            str(manifest_path),
        ],
        cwd=str(root),
        stdout=subprocess.PIPE,
        check=True,
        text=True,
    )
    packages = json.loads(metadata.stdout)["packages"]
    packages = [item for item in packages if Path(item["manifest_path"]).resolve() == manifest_path]
    if len(packages) != 1:
        raise ValueError("rust_manifest must identify a Cargo package, not a workspace")
    package = packages[0]
    if not any("cdylib" in target["kind"] for target in package["targets"]):
        raise ValueError("native Cargo package must have a cdylib library target")
    build = subprocess.run(
        [
            "cargo",
            "build",
            "--lib",
            "--manifest-path",
            str(manifest_path),
            "--message-format=json-render-diagnostics",
        ],
        cwd=str(root),
        stdout=subprocess.PIPE,
        text=True,
    )
    artifacts = []
    for line in build.stdout.splitlines():
        message = json.loads(line)
        if message.get("reason") == "compiler-message":
            rendered = message["message"].get("rendered")
            if rendered:
                print(rendered, file=sys.stderr, end="")
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("package_id") == package["id"]
            and "cdylib" in message["target"]["kind"]
        ):
            artifacts.extend(
                Path(name)
                for name in message["filenames"]
                if Path(name).suffix in (".so", ".dylib", ".dll")
            )
    if build.returncode:
        raise RuntimeError(f"native Cargo build failed with status {build.returncode}")
    if len(artifacts) != 1:
        raise RuntimeError("native Cargo build did not produce exactly one shared library")
    return artifacts[0].resolve()


def _snapshot(root: Path) -> dict[Path, tuple[int, int]]:
    result = {}
    for path in _project_files(root):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        result[path] = (stat.st_mtime_ns, stat.st_size)
    return result


def _serve(
    entry: str, host: str, port: int, debug: bool, native_library: Path | None, dev: bool
) -> None:
    module_name, separator, object_name = entry.partition(":")
    if not separator or not module_name or not object_name:
        raise ValueError("application must be specified as module:object, for example app:app")
    sys.path.insert(0, str(Path.cwd()))
    if debug:
        os.environ["RUST_LOG"] = "debug"
    module = importlib.import_module(module_name)
    app = getattr(module, object_name)
    if not isinstance(app, Ferrum):
        raise TypeError(f"{entry!r} must refer to a Ferrum instance")
    if native_library is not None:
        app._load_native_routes(str(native_library))
    app._pyferrum_launch_info = (entry, dev, debug)
    app.run(host=host, port=port, request_logging=debug)


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _dev(entry: str, host: str, port: int, debug: bool, root: Path) -> int:
    command = [
        sys.executable,
        "-m",
        "pyferrum.cli",
        "_serve",
        entry,
        "--host",
        host,
        "--port",
        str(port),
        "--dev-mode",
    ]
    if debug:
        command.append("--debug")
    process: subprocess.Popen | None = None
    previous_handler = signal.getsignal(signal.SIGTERM)

    def terminate(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        snapshot = _snapshot(root)
        while True:
            try:
                print("Verifying Python sources and configured Rust routes...", flush=True)
                _compile_project(root)
                native_library = _build_native(root)
                worker_command = list(command)
                if native_library is not None:
                    worker_command.extend(["--native-library", str(native_library)])
                process = subprocess.Popen(worker_command, cwd=str(root))
            except (
                OSError,
                ValueError,
                RuntimeError,
                subprocess.CalledProcessError,
                py_compile.PyCompileError,
            ) as error:
                print(f"pyferrum: {error}", file=sys.stderr, flush=True)
            while True:
                time.sleep(0.5)
                current = _snapshot(root)
                if current != snapshot:
                    snapshot = current
                    if process is not None:
                        _stop(process)
                        process = None
                    print(
                        f"\n{'=' * 60}\nDetected changes - reloading server\n",
                        flush=True,
                    )
                    break
                if process is not None and process.poll() is not None:
                    print(
                        f"pyferrum: server exited with status {process.returncode}; waiting for changes",
                        file=sys.stderr,
                        flush=True,
                    )
                    process = None
    except KeyboardInterrupt:
        return 0
    finally:
        if process is not None:
            _stop(process)
        signal.signal(signal.SIGTERM, previous_handler)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyferrum")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "_serve"):
        command = commands.add_parser(name)
        command.add_argument("app", help="application as module:object")
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=_port, default=8080)
        command.add_argument("--debug", action="store_true")
        if name == "run":
            command.add_argument("--dev", action="store_true")
        else:
            command.add_argument("--native-library", type=Path)
            command.add_argument("--dev-mode", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "run":
        print()
        print("{:#^60}".format(" PyFerrum "), flush=True)
    try:
        if args.command == "_serve":
            _serve(args.app, args.host, args.port, args.debug, args.native_library, args.dev_mode)
        elif args.dev:
            return _dev(args.app, args.host, args.port, args.debug, Path.cwd())
        else:
            print("Verifying Python sources and configured Rust routes...", flush=True)
            _compile_project(Path.cwd())
            native_library = _build_native(Path.cwd())
            _serve(args.app, args.host, args.port, args.debug, native_library, False)
    except (
        OSError,
        ValueError,
        RuntimeError,
        TypeError,
        ImportError,
        AttributeError,
        subprocess.CalledProcessError,
        py_compile.PyCompileError,
    ) as error:
        print(f"pyferrum: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
