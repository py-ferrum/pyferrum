"""Verify TOML routes map to explicit Python handlers, not Rust handlers."""

from pathlib import Path

from pyferrum import Ferrum


def main() -> int:
    route_file = Path(__file__).with_name("_routes.toml")
    route_file.write_text(
        """
[[py.routes]]
method = "GET"
path = "/configured"
handler = "configured"
runtime = "python"

[[py.routes]]
method = "GET"
path = "/configured-rust"
handler = "custom"
runtime = "rust"
""".strip()
    )
    try:
        app = Ferrum()
        try:
            app.load_routes(str(route_file))
        except ValueError as error:
            if "explicitly supplied" not in str(error):
                raise
        else:
            raise AssertionError("missing Python handler map was accepted")

        app = Ferrum()

        async def configured(request):
            return {"configured": True}

        route_file.write_text(
            """
[[py.routes]]
method = "GET"
path = "/configured"
handler = "configured"
runtime = "python"
""".strip()
        )
        count = app.load_routes(str(route_file), {"configured": configured})
        if count != 1 or app.route_count() != 1:
            raise AssertionError("TOML Python route was not registered")

        route_file.write_text(
            """
[[py.routes]]
method = "GET"
path = "/configured-rust"
handler = "custom"
runtime = "rust"
""".strip()
        )
        app = Ferrum()
        try:
            app.load_routes(str(route_file))
        except ValueError as error:
            if "export_routes!" not in str(error):
                raise
        else:
            raise AssertionError("TOML Rust route was accepted")
        if app.route_count() != 0:
            raise AssertionError("rejected TOML Rust route was registered")

        route_file.write_text(
            """
[[routes]]
method = "GET"
path = "/legacy"
handler = "configured"
""".strip()
        )
        app = Ferrum()
        count = app.load_routes(str(route_file), {"configured": configured})
        if count != 1 or app.route_count() != 1:
            raise AssertionError("legacy top-level TOML route was not registered")
        print("PASS — TOML accepts Python handlers and rejects Rust routes")
        return 0
    finally:
        route_file.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
