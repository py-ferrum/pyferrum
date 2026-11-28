"""Integration tests for the command-line application runner."""

import importlib.machinery
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _app(value: str) -> str:
    return (
        "import os\n"
        "from pyferrum import Ferrum\n"
        "app = Ferrum()\n"
        "@app.get('/')\n"
        "def index(request):\n"
        f"    return {value!r} + ':' + os.environ.get('RUST_LOG', '')\n"
    )


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "python"), self.env.get("PYTHONPATH", "")]
        )
        self.env.pop("RUST_LOG", None)
        self.process = None
        self.native_extension_path = None
        self.native_extension_backup = None

    def tearDown(self):
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self._restore_native_extension()
        self.temp.cleanup()

    def _restore_native_extension(self):
        if self.native_extension_path is None:
            return
        if self.native_extension_backup is None:
            self.native_extension_path.unlink(missing_ok=True)
        else:
            self.native_extension_path.write_bytes(self.native_extension_backup)
        self.native_extension_path = None
        self.native_extension_backup = None

    def command(self, *args):
        return [sys.executable, "-m", "pyferrum.cli", *args]

    def response(self, port, expected):
        deadline = time.monotonic() + 15
        last_error = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(f"server exited with {self.process.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    body = response.read().decode()
                    if body == expected:
                        return
                    last_error = body
            except (OSError, urllib.error.URLError) as error:
                last_error = error
            time.sleep(0.1)
        self.fail(f"expected {expected!r}; last response/error: {last_error}")

    def test_run_defaults_to_app_and_serves_with_debug(self):
        (self.root / "app.py").write_text(_app("first"))
        port = _port()
        self.process = subprocess.Popen(
            self.command("run", "--port", str(port), "--debug"),
            cwd=self.root,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.response(port, "first:debug")
        self.assertTrue(list((self.root / "__pycache__").glob("app.*.pyc")))
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz")
        self.assertEqual(error.exception.code, 404)

    def test_run_does_not_build_or_verify_by_default(self):
        (self.root / "app.py").write_text(_app("no-build"))
        (self.root / "config.toml").write_text(
            '[rust.native]\nmanifest = "missing/Cargo.toml"\n'
        )
        port = _port()
        log = self.root / "server.log"
        with log.open("w") as output:
            self.process = subprocess.Popen(
                self.command("run", "--port", str(port)),
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=output,
            )
        self.response(port, "no-build:")
        self.assertNotIn("Verifying", log.read_text())
        self.assertNotIn("Compiling", log.read_text())

    def test_run_core_flag(self):
        (self.root / "app.py").write_text(_app("core"))
        port = _port()
        self.process = subprocess.Popen(
            self.command("run", "--core", "--port", str(port)),
            cwd=self.root,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.response(port, "core:")

    def test_ctrl_c_prints_simple_shutdown_message(self):
        (self.root / "app.py").write_text(_app("stopping"))
        port = _port()
        log = self.root / "server.log"
        with log.open("w") as output:
            self.process = subprocess.Popen(
                self.command("run", "--port", str(port)),
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=output,
            )
        self.response(port, "stopping:")
        self.process.send_signal(signal.SIGINT)
        self.process.wait(timeout=15)
        contents = log.read_text()
        self.assertIn("\n[!] Server Stopped...!\n", contents)
        self.assertNotIn("Traceback", contents)
        self.assertNotIn("KeyboardInterrupt", contents)

    def test_run_build_and_core_are_mutually_exclusive(self):
        result = subprocess.run(
            self.command("run", "--build", "--core"),
            cwd=self.root,
            env=self.env,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"not allowed with argument", result.stderr)

    def test_human_readable_startup_and_request_logs(self):
        (self.root / "app.py").write_text(
            _app("logged") + "\nfrom pyferrum import TextResponse\n"
            "@app.get('/override')\n"
            "def override(request):\n"
            "    return TextResponse('ok', headers={'X-Request-ID': 'handler-id'})\n"
            "@app.get('/redirect')\n"
            "def redirect(request):\n"
            "    return TextResponse('redirect', status_code=302)\n"
        )
        port = _port()
        log = self.root / "server.log"
        with log.open("w") as output:
            self.process = subprocess.Popen(
                self.command("run", "app:app", "--port", str(port), "--debug"),
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=output,
            )
        self.response(port, "logged:debug")
        for path, request_id, expected in (
            ("/", "test-ok", 200),
            ("/redirect", "test-redirect", 302),
            ("/missing?secret=hidden", "test-missing", 404),
        ):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", headers={"X-Request-ID": request_id}
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, expected)
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, expected)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/override", headers={"X-Request-ID": "test-original"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers.get_all("X-Request-ID"), ["test-original"])
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=15)
        contents = log.read_text()
        self.assertNotIn("Verifying Python sources", contents)
        self.assertIn(
            "Registered routes: 3\nApp: app:app\nDev mode: disabled\nDebug mode: enabled", contents
        )
        self.assertIn(
            f"============================================================\nServer initialized\nlistening on 127.0.0.1:{port}\n============================================================\n",
            contents,
        )
        timestamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z\s+"
        self.assertRegex(contents, timestamp + r"INFO 200 GET / request_id=test-ok\n")
        self.assertRegex(
            contents, timestamp + r"DEBUG 302 GET /redirect request_id=test-redirect\n"
        )
        self.assertRegex(contents, timestamp + r"WARN 404 GET /missing request_id=test-missing\n")
        self.assertRegex(contents, timestamp + r"INFO 200 GET /override request_id=test-original\n")
        self.assertNotIn("secret=hidden", contents)
        self.assertNotIn('"fields":', contents)

    def test_access_logs_early_and_failed_responses(self):
        app = self.root / "app.py"
        port = _port()
        app.write_text(
            "from pyferrum import Ferrum\n"
            "app = Ferrum()\n"
            "@app.get('/failure')\n"
            "def failure(request):\n"
            "    raise ValueError('test failure')\n"
            f"app.run(port={port}, request_logging=True, "
            "rate_limit_requests=2, max_header_size=1024)\n"
        )
        log = self.root / "server.log"
        with log.open("w") as output:
            self.process = subprocess.Popen(
                [sys.executable, str(app)],
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=output,
            )
        deadline = time.monotonic() + 15
        while True:
            if self.process.poll() is not None:
                self.fail(f"server exited: {log.read_text()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        for path, request_id, expected, extra in (
            ("/failure", "failed", 500, {}),
            ("/missing", "missing", 404, {}),
            ("/failure", "limited", 429, {}),
            ("/failure", "headers", 431, {"X-Padding": "x" * 2048}),
        ):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                headers={"X-Request-ID": request_id, **extra},
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, expected)
            self.assertEqual(error.exception.headers["X-Request-ID"], request_id)
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=15)
        contents = log.read_text()
        for path, request_id, expected, level in (
            ("/failure", "failed", 500, "ERROR"),
            ("/missing", "missing", 404, "WARN"),
            ("/failure", "limited", 429, "WARN"),
            ("/failure", "headers", 431, "WARN"),
        ):
            self.assertRegex(
                contents,
                rf"\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z\s+{level} {expected} GET {re.escape(path)} request_id={request_id}\n",
            )

    def test_log_level_filters_startup_and_requests(self):
        (self.root / "app.py").write_text(_app("quiet"))
        port = _port()
        log = self.root / "server.log"
        with log.open("w") as output:
            self.process = subprocess.Popen(
                self.command("run", "app:app", "--port", str(port)),
                cwd=self.root,
                env={**self.env, "RUST_LOG": "warn"},
                stdout=output,
                stderr=output,
            )
        self.response(port, "quiet:warn")
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=15)
        self.assertNotIn("Server initialized", log.read_text())
        self.assertNotIn("listening on", log.read_text())

    def test_access_log_levels_obey_rust_log(self):
        port = _port()
        (self.root / "app.py").write_text(
            _app("filtered") + "\nfrom pyferrum import TextResponse\n"
            "@app.get('/redirect')\n"
            "def redirect(request):\n"
            "    return TextResponse('redirect', status_code=302)\n"
            "@app.get('/failure')\n"
            "def failure(request):\n"
            "    return TextResponse('error', status_code=500)\n"
            f"app.run(port={port}, request_logging=True)\n"
        )
        log = self.root / "server.log"
        with log.open("w") as output:
            self.process = subprocess.Popen(
                [sys.executable, str(self.root / "app.py")],
                cwd=self.root,
                env={**self.env, "RUST_LOG": "warn"},
                stdout=output,
                stderr=output,
            )
        self.response(port, "filtered:warn")
        for path, code in (("/redirect", 302), ("/missing", 404), ("/failure", 500)):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", headers={"X-Request-ID": f"filter-{code}"}
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, code)
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=15)
        contents = log.read_text()
        self.assertNotIn("200 GET /", contents)
        self.assertNotIn("302 GET /redirect", contents)
        self.assertIn("WARN 404 GET /missing request_id=filter-404", contents)
        self.assertIn("ERROR 500 GET /failure request_id=filter-500", contents)

    def test_dev_reloads_changed_python(self):
        app = self.root / "app.py"
        app.write_text(_app("first"))
        port = _port()
        log = self.root / "server.log"
        with log.open("w") as output:
            self.process = subprocess.Popen(
                self.command(
                    "run", "app:app", "--port", str(port), "--dev", "--verify", "--debug"
                ),
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=output,
            )
        self.response(port, "first:debug")
        self.assertIn("App: app:app\nDev mode: enabled\nDebug mode: enabled", log.read_text())
        app.write_text(_app("second"))
        self.response(port, "second:debug")
        app.write_text(_app("third-version"))
        self.response(port, "third-version:debug")
        contents = log.read_text()
        self.assertEqual(contents.count("PyFerrum"), 1)
        self.assertEqual(contents.count("Detected changes - reloading server"), 2)
        self.assertEqual(
            contents.count("Verifying Python sources and Rust route configuration..."), 3
        )
        self.assertEqual(contents.count("Server initialized"), 3)
        reload_block = (
            "\n" + "=" * 60 + "\n"
            "Detected changes - reloading server\n\n"
            "Verifying Python sources and Rust route configuration...\n"
            "Registered routes: 1\n"
            "App: app:app\n"
            "Dev mode: enabled\n"
            "Debug mode: enabled\n\n" + "=" * 60 + "\n"
            f"Server initialized\nlistening on 127.0.0.1:{port}\n" + "=" * 60 + "\n"
        )
        self.assertEqual(contents.count(reload_block), 2)
        first_shutdown = contents.index("shutdown signal received; stopping new connections")
        self.assertLess(first_shutdown, contents.index("Detected changes - reloading server"))

    def test_dev_recovers_after_syntax_error(self):
        app = self.root / "app.py"
        app.write_text("def broken(:\n")
        port = _port()
        log = self.root / "server.log"
        with log.open("w") as output:
            self.process = subprocess.Popen(
                self.command("run", "app:app", "--port", str(port), "--dev"),
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=output,
            )
        time.sleep(1)
        app.write_text(_app("recovered"))
        self.response(port, "recovered:")
        contents = log.read_text()
        self.assertIn("SyntaxError", contents)
        self.assertEqual(contents.count("PyFerrum"), 1)
        self.assertIn("=" * 60 + "\nDetected changes - reloading server\n", contents)

    def test_example_does_not_load_optional_health_routes_by_default(self):
        shutil.copytree(
            ROOT / "example-app",
            self.root,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("target", "__pycache__", ".env"),
        )
        port = _port()
        self.process = subprocess.Popen(
            self.command("run", "app:app", "--port", str(port)),
            cwd=self.root,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while True:
            if self.process.poll() is not None:
                self.fail(f"server exited with {self.process.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    self.assertEqual(response.status, 200)
                    break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        for path in ("/healthz", "/livez", "/readyz"):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1)
            self.assertEqual(error.exception.code, 404)

    def test_example_environment_controls_runtime(self):
        port = _port()
        env = {
            **self.env,
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "METRICS_ENABLED": "false",
            "SECURITY_HEADERS_ENABLED": "false",
            "REQUEST_LOGGING": "false",
            "SERVER_NAME": "example-test",
            "NATIVE_LIBRARY": "",
        }
        log = self.root / "server.log"
        with log.open("w") as output:
            self.process = subprocess.Popen(
                [sys.executable, str(ROOT / "example-app" / "app.py")],
                cwd=self.root,
                env=env,
                stdout=output,
                stderr=output,
            )
        deadline = time.monotonic() + 15
        while True:
            if self.process.poll() is not None:
                self.fail(f"server exited: {log.read_text()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    self.assertEqual(response.headers["Server"], "example-test")
                    self.assertIsNone(response.headers.get("X-Frame-Options"))
                    break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics")
        self.assertEqual(error.exception.code, 404)
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=15)
        self.assertNotRegex(log.read_text(), r"INFO 200 GET / request_id=")

    def test_example_rejects_invalid_boolean(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "example-app" / "app.py")],
            cwd=self.root,
            env={**self.env, "REQUEST_LOGGING": "invalid", "NATIVE_LIBRARY": ""},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REQUEST_LOGGING must be true, false, 1, or 0", result.stderr)

    def test_build_has_no_output_mode_options(self):
        result = subprocess.run(
            self.command("build", "--help"),
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--wheel", result.stdout)
        self.assertNotIn("--no-wheel", result.stdout)

    def test_cargo_build_output_is_streamed_before_completion(self):
        script = (
            "from pyferrum.cli import _stream_cargo_build\n"
            "import sys\n"
            "command = [sys.executable, '-c', "
            "\"print('Compiling live-output', flush=True); "
            "import time; time.sleep(1)\"]\n"
            "returncode, _ = _stream_cargo_build(command, '.', dict(__import__('os').environ))\n"
            "raise SystemExit(returncode)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=self.root,
            env=self.env,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            assert process.stderr is not None
            line = process.stderr.readline()
            self.assertIn("Compiling live-output", line)
            self.assertIsNone(process.poll(), "cargo output was buffered until process exit")
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def test_help_documents_config_schema(self):
        for arguments, expected in (
            (("--help",), ("config.toml", "[rust.native].manifest", "[[py.routes]]")),
            (("run", "--help"), ("--build", "config.toml", "--verify")),
            (("build", "--help"), ("config.toml", "[rust.native].manifest")),
            (("verify", "--help"), ("config.toml", "does not compile Rust")),
        ):
            result = subprocess.run(
                self.command(*arguments),
                cwd=self.root,
                env=self.env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for text in expected:
                self.assertIn(text, result.stdout)

    def test_build_uses_active_python_without_project_venv(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; "
                "from pyferrum.cli import _python_context; "
                "python, info = _python_context(); "
                "print(json.dumps({'python': str(python), 'package': info['package']}))",
            ],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)
        self.assertEqual(Path(context["python"]), Path(sys.executable).resolve())
        self.assertTrue((Path(context["package"]) / "_pyferrum_sdk" / "Cargo.toml").is_file())
        self.assertFalse((self.root / ".venv").exists())

    def test_core_selector_uses_original_extension(self):
        script = (
            "import importlib, types; "
            "from pyferrum import cli; "
            "package = importlib.import_module('pyferrum'); "
            "custom = types.SimpleNamespace(__file__='custom.so', "
            "__pyferrum_native_routes__=True); "
            "cli._native_extension = custom; "
            "setattr(package, '_native_extension', custom); "
            "cli._select_core_extension(); "
            "selected = cli._native_extension; "
            "assert Path(selected.__file__).name.startswith('_pyferrum_core.'); "
            "assert not getattr(selected, '__pyferrum_native_routes__', False); "
            "assert getattr(package, '_native_extension') is selected"
        )
        result = subprocess.run(
            [sys.executable, "-c", "from pathlib import Path; " + script],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_source_and_native_configuration_fail_explicitly(self):
        (self.root / "app.py").write_text("def broken(:\n")
        result = subprocess.run(
            self.command("verify"),
            cwd=self.root,
            env=self.env,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"SyntaxError", result.stderr)
        (self.root / "app.py").write_text(_app("ok"))
        (self.root / "config.toml").write_text(
            '[rust.native]\nmanifest = "native/Cargo.toml"\n'
            '\n[[py.routes]]\nmethod = "GET"\npath = "/from-table"\nhandler = "example"\n'
        )
        result = subprocess.run(
            self.command("verify"),
            cwd=self.root,
            env=self.env,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Rust route manifest does not exist", result.stderr)

    def test_verify_checks_rust_configuration_without_compiling(self):
        (self.root / "app.py").write_text(_app("verified"))
        (self.root / "config.toml").write_text(
            '[rust.native]\nmanifest = "native/Cargo.toml"\n'
            '\n[[py.routes]]\nmethod = "GET"\npath = "/from-table"\nhandler = "example"\n'
        )
        native = self.root / "native"
        (native / "src").mkdir(parents=True)
        (native / "Cargo.toml").write_text(
            '[package]\nname = "verify-only"\nversion = "0.1.0"\nedition = "2021"\n'
            '\n[lib]\ncrate-type = ["cdylib"]\n'
        )
        (native / "src" / "lib.rs").write_text("this is not valid Rust syntax")
        result = subprocess.run(
            self.command("verify"),
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((native / "target").exists())

    def test_native_health_example_rebuilds_and_runs_with_python(self):
        shutil.copytree(
            ROOT / "example-app" / "native",
            self.root / "native",
            ignore=shutil.ignore_patterns("target"),
        )
        (self.root / "app.py").write_text(_app("Hello from Python"))
        (self.root / "config.toml").write_text(
            '[rust.native]\nmanifest = "native/Cargo.toml"\n'
        )
        manifest = self.root / "native" / "Cargo.toml"
        manifest.write_text(
            manifest.read_text().replace(
                "../../crates/ferrum-core", (ROOT / "crates" / "ferrum-core").as_posix()
            )
            .replace(
                "../../python/pyferrum/_pyferrum_sdk",
                (ROOT / "python" / "pyferrum" / "_pyferrum_sdk").as_posix(),
            )
        )
        rust_source = self.root / "native" / "src" / "lib.rs"
        rust_source.write_text(
            rust_source.read_text()
            .replace(
                "export_routes!(",
                "fn name(request: &Request) -> Response {\n"
                '    Response::text(request.path_params["name"].as_bytes())\n'
                "}\n\n"
                "fn failing(_: &Request) -> Response {\n"
                '    panic!("native failure")\n'
                "}\n\nexport_routes!(",
            )
            .replace(
                '    ("GET", "/healthz", health),',
                '    ("GET", "/native/{name}", name),\n'
                '    ("GET", "/native-failure", failing),\n'
                '    ("GET", "/healthz", health),',
            )
        )
        port = _port()
        log = self.root / "server.log"
        abi3_suffix = next(
            suffix
            for suffix in importlib.machinery.EXTENSION_SUFFIXES
            if suffix.startswith(".abi3.")
        )
        extension_path = ROOT / "python" / "pyferrum" / f"_pyferrum{abi3_suffix}"
        self.native_extension_path = extension_path
        if extension_path.is_file():
            self.native_extension_backup = extension_path.read_bytes()
        runner = Path(sys.executable).with_name("pyferrum.exe" if os.name == "nt" else "pyferrum")
        cli = [str(runner), "run"] if runner.is_file() else self.command("run")
        with log.open("w") as output:
            self.process = subprocess.Popen(
                [*cli, "app:app", "--port", str(port), "--dev", "--build", "--verify", "--debug"],
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=output,
            )

        def wait_for(path, expected):
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    self.fail(f"server exited: {log.read_text()}")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=1
                    ) as response:
                        if expected in response.read():
                            return
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.1)
            self.fail(f"no expected response for {path}: {log.read_text()}")

        wait_for("/", b"Hello from Python")
        wait_for("/healthz", b'"healthy"')
        wait_for("/livez", b'"alive"')
        wait_for("/readyz", b'"ready"')
        wait_for("/native/alice", b"alice")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/healthz", method="POST")
            )
        self.assertEqual(error.exception.code, 405)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/native-failure")
        self.assertEqual(error.exception.code, 500)
        wait_for("/healthz", b'"healthy"')
        rust_source.write_text(rust_source.read_text().replace('"healthy"', '"reloaded"'))
        wait_for("/healthz", b'"reloaded"')
        valid_source = rust_source.read_text()
        rust_source.write_text(valid_source + "\ninvalid rust code\n")
        deadline = time.monotonic() + 120
        while "custom Rust route build failed" not in log.read_text():
            if time.monotonic() >= deadline:
                self.fail(f"invalid Rust build was not reported: {log.read_text()}")
            time.sleep(0.1)
        rust_source.write_text(valid_source)
        wait_for("/healthz", b'"reloaded"')
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=15)
        extension_path.unlink(missing_ok=True)
        build_port = _port()
        with log.open("a") as output:
            self.process = subprocess.Popen(
                [
                    *cli,
                    "app:app",
                    "--port",
                    str(build_port),
                    "--build",
                    "--verify",
                ],
                cwd=self.root,
                env=self.env,
                stdout=output,
                stderr=output,
            )
        port = build_port
        wait_for("/healthz", b'"reloaded"')
        wait_for("/native/alice", b"alice")
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=15)
        extension_path.unlink(missing_ok=True)
        if os.name == "nt":
            library_name = "native_plugin.dll"
        elif sys.platform == "darwin":
            library_name = "libnative_plugin.dylib"
        else:
            library_name = "libnative_plugin.so"
        target = Path(
            self.env.get("CARGO_TARGET_DIR", self.root / "native" / "target")
        ).resolve()
        library = (target / "debug" / library_name).resolve()
        build_env = {**self.env, "CARGO_TARGET_DIR": str(target)}
        build = subprocess.run(
            ["cargo", "build", "--manifest-path", str(self.root / "native" / "Cargo.toml")],
            cwd=self.root,
            env=build_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(build.returncode, 0, build.stderr)
        example_port = _port()
        env = {
            **self.env,
            "HOST": "127.0.0.1",
            "PORT": str(example_port),
            "NATIVE_LIBRARY": str(library),
        }
        with log.open("a") as output:
            self.process = subprocess.Popen(
                [sys.executable, str(ROOT / "example-app" / "app.py")],
                cwd=self.root,
                env=env,
                stdout=output,
                stderr=output,
            )
        port = example_port
        wait_for("/healthz", b'"reloaded"')
        wait_for("/from-table", b"Python handler registered from TOML")


if __name__ == "__main__":
    unittest.main()
