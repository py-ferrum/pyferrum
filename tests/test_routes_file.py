"""Verify declarative TOML routes use explicit handlers and validated Rust builtins."""

from pathlib import Path

from pyferrum import Ferrum


def main() -> int:
    route_file = Path(__file__).with_name("_routes.toml")
    route_file.write_text(
        """
[[routes]]
method = "GET"
path = "/configured"
handler = "configured"
runtime = "python"

[[routes]]
method = "GET"
path = "/configured-health"
handler = "health"
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

        count = app.load_routes(str(route_file), {"configured": configured})
        if count != 2 or app.route_count() != 2:
            raise AssertionError("TOML routes were not registered")
        print("PASS — TOML routes validate explicit handlers and register Rust builtins")
        return 0
    finally:
        route_file.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
