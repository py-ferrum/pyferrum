"""Command-line entry point for running pyferrum applications."""

import argparse
import hashlib
import importlib
import importlib.machinery
import json
import os
import py_compile
import shutil
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
from . import _pyferrum as _native_extension

_IGNORED_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "target", "node_modules"}
_WATCHED_SUFFIXES = {".py", ".rs", ".toml"}


def _select_core_extension() -> None:
    global _native_extension
    core_extension = importlib.import_module("pyferrum._pyferrum_core")
    _native_extension = core_extension
    setattr(importlib.import_module("pyferrum"), "_native_extension", core_extension)


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


def _rust_route_project(root: Path, required: bool = False) -> tuple[Path, dict, Path] | None:
    config = root / "config.toml"
    if not config.is_file():
        if required:
            raise ValueError("pyferrum build requires [rust.native].manifest in config.toml")
        return None
    with config.open("rb") as file:
        config_doc = tomllib.load(file)
    rust_config = config_doc.get("rust", {})
    if not isinstance(rust_config, dict):
        raise ValueError("[rust] must be a table in config.toml")
    native_config = rust_config.get("native")
    if native_config is None:
        if required:
            raise ValueError("pyferrum build requires [rust.native].manifest in config.toml")
        return None
    if not isinstance(native_config, dict):
        raise ValueError("[rust.native] must be a table in config.toml")
    manifest_value = native_config.get("manifest")
    if manifest_value is None:
        if required:
            raise ValueError("pyferrum build requires [rust.native].manifest in config.toml")
        return None
    if not isinstance(manifest_value, str) or not manifest_value:
        raise ValueError("[rust.native].manifest must be a non-empty path")
    manifest_path = (root / manifest_value).resolve()
    if not manifest_path.is_file():
        raise ValueError(f"Rust route manifest does not exist: {manifest_path}")
    with manifest_path.open("rb") as file:
        manifest_doc = tomllib.load(file)
    package_info = manifest_doc.get("package")
    if not isinstance(package_info, dict):
        raise ValueError("[rust.native].manifest must identify a Cargo package, not a workspace")
    library = manifest_doc.get("lib", {})
    crate_types = library.get("crate-type") if isinstance(library, dict) else None
    if not isinstance(crate_types, list) or "cdylib" not in crate_types:
        raise ValueError("[rust.native].manifest must define a cdylib library target")
    source_value = library.get("path", "src/lib.rs")
    if not isinstance(source_value, str) or not source_value:
        raise ValueError("Cargo [lib].path must be a non-empty path")
    route_source = (manifest_path.parent / source_value).resolve()
    if not route_source.is_file():
        raise ValueError(f"Rust route source does not exist: {route_source}")
    return manifest_path, manifest_doc, route_source


def _verify_project(root: Path) -> None:
    _compile_project(root)
    _rust_route_project(root)
    _python_context()


def _python_context() -> tuple[Path, dict[str, object]]:
    python = Path(sys.executable).resolve()
    package_dir = Path(_native_extension.__file__).resolve().parent
    abi3_suffix = next(
        (
            suffix
            for suffix in importlib.machinery.EXTENSION_SUFFIXES
            if suffix.startswith(".abi3.")
        ),
        None,
    )
    if not python.is_file():
        raise RuntimeError("cannot locate the Python interpreter running pyferrum")
    if not abi3_suffix:
        raise RuntimeError("the active Python interpreter does not support abi3 extensions")
    if not (package_dir / "_pyferrum_sdk" / "Cargo.toml").is_file():
        raise RuntimeError(
            "the active pyferrum installation is missing its Rust plugin SDK; reinstall it"
        )
    return python, {
        "package": str(package_dir),
        "version": str(_native_extension.__version__),
        "abi3_suffix": abi3_suffix,
    }


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{json.dumps(str(key))} = {_toml_value(item)}" for key, item in value.items()
            )
            + " }"
        )
    raise ValueError(f"unsupported Cargo dependency value: {value!r}")


def _workspace_data(manifest: Path) -> tuple[Path | None, dict]:
    for parent in manifest.parent.parents:
        candidate = parent / "Cargo.toml"
        if candidate.is_file():
            with candidate.open("rb") as file:
                document = tomllib.load(file)
            if "workspace" in document:
                return parent, document["workspace"]
    with manifest.open("rb") as file:
        document = tomllib.load(file)
    if "workspace" in document:
        return manifest.parent, document["workspace"]
    return None, {}


def _native_extension_dependencies(manifest_doc: dict, manifest: Path, sdk: Path) -> str:
    sections: dict[str, list[str]] = {}
    has_ferrum_core = False
    workspace_root, workspace = _workspace_data(manifest)

    def add_dependencies(section: str, values: dict, target: str | None = None) -> None:
        nonlocal has_ferrum_core
        cargo_section = "build-dependencies" if section == "build-dependencies" else section
        for dependency_name, dependency_value in values.items():
            if dependency_name == "pyo3":
                continue
            fields = (
                {"version": dependency_value}
                if isinstance(dependency_value, str)
                else dict(dependency_value)
            )
            package_name = str(fields.get("package", dependency_name))
            if dependency_name in {"ferrum-core", "pyferrum-sdk"} or package_name in {
                "ferrum-core",
                "pyferrum-sdk",
            }:
                has_ferrum_core = True
                fields = {"package": "pyferrum-sdk", "path": str(sdk)}
                output_section = cargo_section
                if target:
                    output_section = f"target.{json.dumps(target)}.{cargo_section}"
                sections.setdefault(output_section, []).append(
                    f"{json.dumps(dependency_name)} = {_toml_value(fields)}"
                )
                continue
            inherited = bool(fields.pop("workspace", False))
            if inherited:
                workspace_value = workspace.get("dependencies", {}).get(dependency_name)
                if workspace_value is None:
                    raise ValueError(f"workspace dependency {dependency_name!r} is not defined")
                fields = (
                    {"version": workspace_value}
                    if isinstance(workspace_value, str)
                    else dict(workspace_value)
                )
            package_name = str(fields.get("package", dependency_name))
            if fields.get("path"):
                base = workspace_root if inherited and workspace_root else manifest.parent
                fields["path"] = str((base / str(fields["path"])).resolve())
            output_section = cargo_section
            if target:
                output_section = f"target.{json.dumps(target)}.{cargo_section}"
            sections.setdefault(output_section, []).append(
                f"{json.dumps(dependency_name)} = {_toml_value(fields)}"
            )

    add_dependencies("dependencies", manifest_doc.get("dependencies", {}))
    add_dependencies("build-dependencies", manifest_doc.get("build-dependencies", {}))
    for target, target_sections in manifest_doc.get("target", {}).items():
        add_dependencies("dependencies", target_sections.get("dependencies", {}), target)
        add_dependencies(
            "build-dependencies", target_sections.get("build-dependencies", {}), target
        )
    if not has_ferrum_core:
        sections.setdefault("dependencies", []).append(
            f'"ferrum-core" = {{ package = "pyferrum-sdk", path = {json.dumps(str(sdk))} }}'
        )
    sections.setdefault("dependencies", []).append(
        '"pyo3" = { version = "0.29", features = ["extension-module", "abi3-py310"] }'
    )
    return "\n".join(f"[{section}]\n" + "\n".join(entries) for section, entries in sections.items())


def _stream_cargo_build(
    command: list[str], cwd: str, env: dict[str, str]
) -> tuple[int, list[Path]]:
    build = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    artifacts: list[Path] = []
    assert build.stdout is not None
    for line in build.stdout:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(line, file=sys.stderr, end="", flush=True)
            continue
        if message.get("reason") == "compiler-message":
            rendered = message.get("message", {}).get("rendered")
            if rendered:
                print(rendered, file=sys.stderr, end="", flush=True)
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("target", {}).get("name") == "_pyferrum"
            and "cdylib" in message.get("target", {}).get("kind", [])
        ):
            artifacts.extend(
                Path(name)
                for name in message["filenames"]
                if Path(name).suffix in (".so", ".dylib", ".dll")
            )
    return build.wait(), artifacts


def _custom_route_build(root: Path) -> Path:
    route_project = _rust_route_project(root, required=True)
    if route_project is None:
        raise RuntimeError("Rust route project was not found")
    manifest, manifest_doc, route_source = route_project
    python, python_info = _python_context()
    package_info = manifest_doc.get("package")
    if not isinstance(package_info, dict):
        raise RuntimeError("validated Rust route package is not a Cargo package")

    package_dir = Path(str(python_info["package"]))
    sdk = package_dir / "_pyferrum_sdk"
    build_dir = root / ".pyferrum-build"
    project_dir = build_dir / "native-extension"
    if project_dir.exists():
        shutil.rmtree(project_dir)
    (project_dir / "src").mkdir(parents=True)

    version = str(python_info["version"])
    digest = hashlib.sha256(manifest.read_bytes())
    source_root = route_source.parent
    for source in sorted(source_root.rglob("*.rs")):
        digest.update(source.relative_to(source_root).as_posix().encode())
        digest.update(source.read_bytes())
    route_digest = digest.hexdigest()[:12]
    base_version = version.split("+", 1)[0]
    custom_version = f"{base_version}+routes.{route_digest}"
    cargo_dependencies = _native_extension_dependencies(manifest_doc, manifest, sdk)
    cargo_manifest = (
        "[package]\n"
        'name = "pyferrum-custom-routes"\n'
        f'version = "{custom_version}"\n'
        f'edition = "{package_info.get("edition", "2021")}"\n'
        "publish = false\n\n"
        "[lib]\n"
        'name = "_pyferrum"\n'
        'crate-type = ["cdylib"]\n\n'
        "[workspace]\n\n"
        f"{cargo_dependencies}\n"
    )
    (project_dir / "Cargo.toml").write_text(cargo_manifest, encoding="utf-8")
    (project_dir / "src" / "lib.rs").write_text(
        f"#[path = {json.dumps(route_source.as_posix())}]\n"
        "mod native_routes;\n\n"
        "use pyo3::prelude::*;\n"
        "use pyo3::types::PyModule;\n\n"
        "#[pymodule]\n"
        "fn _pyferrum(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {\n"
        '    let core = PyModule::import(py, "pyferrum._pyferrum_core")?;\n'
        '    for name in ["Request", "Response", "StreamingResponse", "_Ferrum"] {\n'
        "        module.add(name, core.getattr(name)?)?;\n"
        "    }\n"
        '    module.add("__version__", env!("CARGO_PKG_VERSION"))?;\n'
        '    module.add("__pyferrum_native_routes__", true)?;\n'
        "    Ok(())\n"
        "}\n",
        encoding="utf-8",
    )

    target_dir = build_dir / "target"
    build_env = {
        **os.environ,
        "CARGO_TARGET_DIR": str(target_dir),
        "PYO3_PYTHON": str(python),
        "PATH": str(python.parent) + os.pathsep + os.environ.get("PATH", ""),
    }
    command = [
        "cargo",
        "build",
        "--release",
        "--manifest-path",
        str(project_dir / "Cargo.toml"),
        "--message-format=json-render-diagnostics",
    ]
    returncode, artifacts = _stream_cargo_build(command, str(project_dir), build_env)
    if returncode:
        raise RuntimeError(f"custom Rust route build failed with status {returncode}")
    if len(artifacts) != 1:
        raise RuntimeError("Cargo did not produce exactly one custom Python extension")
    extension = artifacts[0].resolve()

    destination = package_dir / f"_pyferrum{python_info['abi3_suffix']}"
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copy2(extension, temporary)
    temporary.replace(destination)
    return destination


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


def _dev(
    entry: str,
    host: str,
    port: int,
    debug: bool,
    root: Path,
    build_routes: bool,
    verify: bool,
    use_core: bool,
) -> int:
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
                if verify:
                    print("Verifying Python sources and Rust route configuration...", flush=True)
                    _verify_project(root)
                if build_routes:
                    print("Building Rust routes into the Python extension...", flush=True)
                    _custom_route_build(root)
                worker_command = list(command)
                if use_core:
                    worker_command.append("--core")
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


def _exec_built_app(args: argparse.Namespace) -> None:
    command = [
        sys.executable,
        "-m",
        "pyferrum.cli",
        "run",
        args.app,
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.debug:
        command.append("--debug")
    os.execv(sys.executable, command)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyferrum",
        description="Build and run pyferrum applications.",
        epilog=(
            "Project settings go in config.toml: [rust.native].manifest selects "
            "the Cargo package, and repeatable [[py.routes]] entries define "
            "Python routes. Rust routes are defined in Rust with export_routes!."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "_serve"):
        command = commands.add_parser(name)
        if name == "run":
            command.add_argument(
                "app",
                nargs="?",
                default="app:app",
                help="application as module:object (default: app:app)",
            )
        else:
            command.add_argument("app", help="application as module:object")
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=_port, default=8080)
        command.add_argument("--debug", action="store_true")
        if name == "run":
            command.add_argument(
                "--dev",
                action="store_true",
                help="watch project files and reload the server",
            )
            modes = command.add_mutually_exclusive_group()
            modes.add_argument(
                "--build",
                action="store_true",
                help="build Rust routes from config.toml before starting",
            )
            modes.add_argument(
                "--core",
                action="store_true",
                help="always use the original core extension",
            )
            command.add_argument(
                "--verify",
                action="store_true",
                help="verify Python and config.toml without running Cargo",
            )
        else:
            command.add_argument("--native-library", type=Path)
            command.add_argument("--dev-mode", action="store_true")
            command.add_argument("--core", action="store_true", help=argparse.SUPPRESS)
    commands.add_parser(
        "verify",
        help="check Python sources and config.toml",
        description=(
            "Check Python syntax and [rust.native].manifest in config.toml. "
            "This command does not compile Rust."
        ),
    )
    commands.add_parser(
        "build",
        help="build Rust routes configured in config.toml",
        description=(
            "Build and install the custom extension using [rust.native].manifest from config.toml."
        ),
    )
    args = parser.parse_args(argv)
    if args.command == "run" and (not args.build or args.dev):
        print()
        print("{:#^60}".format(" PyFerrum "), flush=True)
    try:
        if args.command == "verify":
            print("Verifying Python sources and Rust route configuration...", flush=True)
            _verify_project(Path.cwd())
            print("Verification passed.", flush=True)
        elif args.command == "_serve":
            if args.core:
                _select_core_extension()
            _serve(args.app, args.host, args.port, args.debug, args.native_library, args.dev_mode)
        elif args.command == "build":
            result = _custom_route_build(Path.cwd())
            print(f"Replaced pyferrum extension: {result}")
        else:
            root = Path.cwd()
            if args.dev:
                return _dev(
                    args.app,
                    args.host,
                    args.port,
                    args.debug,
                    root,
                    args.build,
                    args.verify,
                    args.core,
                )
            if args.verify:
                print("Verifying Python sources and Rust route configuration...", flush=True)
                _verify_project(root)
            if args.build:
                print("Building Rust routes into the Python extension...", flush=True)
                result = _custom_route_build(root)
                print(f"Built customized pyferrum extension: {result}", flush=True)
                _exec_built_app(args)
            if args.core:
                _select_core_extension()
            _serve(args.app, args.host, args.port, args.debug, None, False)
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
    except KeyboardInterrupt:
        print("\n[!] Server Stopped...!\n", flush=True)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
