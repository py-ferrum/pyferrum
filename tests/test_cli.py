"""Integration tests for the command-line application runner."""

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

    def tearDown(self):
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.temp.cleanup()

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

    def test_run_compiles_and_serves_with_debug(self):
        (self.root / "app.py").write_text(_app("first"))
        port = _port()
        self.process = subprocess.Popen(
            self.command("run", "app:app", "--port", str(port), "--debug"),
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
        self.assertIn("Verifying Python sources and configured Rust routes...", contents)
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
                self.command("run", "app:app", "--port", str(port), "--dev", "--debug"),
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
            contents.count("Verifying Python sources and configured Rust routes..."), 3
        )
        self.assertEqual(contents.count("Server initialized"), 3)
        reload_block = (
            "\n" + "=" * 60 + "\n"
            "Detected changes - reloading server\n\n"
            "Verifying Python sources and configured Rust routes...\n"
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

    def test_explicit_health_example(self):
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
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1
                ) as response:
                    self.assertIn(b'"healthy"', response.read())
                    break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        for path, status in (("/livez", b'"alive"'), ("/readyz", b'"ready"')):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as response:
                self.assertIn(status, response.read())

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

    def test_invalid_source_and_native_configuration_fail_explicitly(self):
        (self.root / "app.py").write_text("def broken(:\n")
        result = subprocess.run(
            self.command("run", "app:app"),
            cwd=self.root,
            env=self.env,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"SyntaxError", result.stderr)
        (self.root / "app.py").write_text(_app("ok"))
        (self.root / "pyproject.toml").write_text(
            '[tool.pyferrum]\nrust_manifest = "native/Cargo.toml"\n'
        )
        result = subprocess.run(
            self.command("run", "app:app"),
            cwd=self.root,
            env=self.env,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Cargo.toml", result.stderr)

    def test_native_health_example_rebuilds_and_runs_with_python(self):
        shutil.copytree(
            ROOT / "example-app" / "native",
            self.root / "native",
            ignore=shutil.ignore_patterns("target"),
        )
        (self.root / "app.py").write_text(_app("Hello from Python"))
        (self.root / "pyproject.toml").write_text(
            '[tool.pyferrum]\nrust_manifest = "native/Cargo.toml"\n'
        )
        manifest = self.root / "native" / "Cargo.toml"
        manifest.write_text(
            manifest.read_text().replace(
                "../../crates/ferrum-core", (ROOT / "crates" / "ferrum-core").as_posix()
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
                '    ("GET", "/plugin/healthz", health),',
                '    ("GET", "/native/{name}", name),\n'
                '    ("GET", "/native-failure", failing),\n'
                '    ("GET", "/plugin/healthz", health),',
            )
        )
        port = _port()
        log = self.root / "server.log"
        runner = Path(sys.executable).with_name("pyferrum.exe" if os.name == "nt" else "pyferrum")
        cli = [str(runner), "run"] if runner.is_file() else self.command("run")
        with log.open("w") as output:
            self.process = subprocess.Popen(
                [*cli, "app:app", "--port", str(port), "--dev", "--debug"],
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
        wait_for("/plugin/healthz", b'"healthy"')
        wait_for("/plugin/livez", b'"alive"')
        wait_for("/plugin/readyz", b'"ready"')
        wait_for("/native/alice", b"alice")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}/plugin/healthz", method="POST")
            )
        self.assertEqual(error.exception.code, 405)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/native-failure")
        self.assertEqual(error.exception.code, 500)
        wait_for("/plugin/healthz", b'"healthy"')
        rust_source.write_text(rust_source.read_text().replace('"healthy"', '"reloaded"'))
        wait_for("/plugin/healthz", b'"reloaded"')
        valid_source = rust_source.read_text()
        rust_source.write_text(valid_source + "\ninvalid rust code\n")
        deadline = time.monotonic() + 120
        while "native Cargo build failed" not in log.read_text():
            if time.monotonic() >= deadline:
                self.fail(f"invalid Rust build was not reported: {log.read_text()}")
            time.sleep(0.1)
        rust_source.write_text(valid_source)
        wait_for("/plugin/healthz", b'"reloaded"')
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=15)
        if os.name == "nt":
            library_name = "pyferrum_example_plugin.dll"
        elif sys.platform == "darwin":
            library_name = "libpyferrum_example_plugin.dylib"
        else:
            library_name = "libpyferrum_example_plugin.so"
        target = Path(self.env.get("CARGO_TARGET_DIR", self.root / "native" / "target"))
        library = (target / "debug" / library_name).resolve()
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
        wait_for("/plugin/healthz", b'"reloaded"')
        wait_for("/healthz", b'"healthy"')


if __name__ == "__main__":
    unittest.main()
