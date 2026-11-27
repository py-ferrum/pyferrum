"""Tests for response helpers and their wire-format edge cases."""

import os
import signal
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from pyferrum import (
    CsvResponse,
    FileResponse,
    HtmlResponse,
    RedirectResponse,
    ServerSentEvent,
    XmlResponse,
)

from tests.test_e2e import ROOT, pick_port, wait_for


class ResponseHelperTests(unittest.TestCase):
    def test_text_formats_and_csv_escaping(self):
        html = HtmlResponse("<h1>café</h1>", status_code=201, headers={"X-Test": "yes"})
        self.assertEqual(bytes(html.body), "<h1>café</h1>".encode())
        self.assertEqual(html.content_type, "text/html; charset=utf-8")
        self.assertEqual(html.status_code, 201)
        self.assertEqual(html.headers["X-Test"], "yes")
        xml = XmlResponse("<root/>")
        self.assertEqual(bytes(xml.body), b"<root/>")
        self.assertEqual(xml.content_type, "application/xml; charset=utf-8")
        csv = CsvResponse([["a,b", 'say "hi"', "line\nbreak"], [1, None]])
        self.assertEqual(bytes(csv.body), b'"a,b","say ""hi""","line\nbreak"\r\n1,\r\n')
        self.assertEqual(csv.content_type, "text/csv; charset=utf-8")

    def test_file_download_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            path.write_bytes(b"download\x00data")
            headers = {"content-disposition": "inline", "X-Test": "yes"}
            response = FileResponse(path, filename='café "report".txt', headers=headers)
            self.assertEqual(bytes(response.body), b"download\x00data")
            self.assertEqual(response.content_type, "text/plain")
            self.assertEqual(
                response.headers["Content-Disposition"],
                'attachment; filename="caf? \\"report\\".txt"; '
                "filename*=UTF-8''caf%C3%A9%20%22report%22.txt",
            )
            self.assertEqual(headers["content-disposition"], "inline")
            self.assertNotIn("content-disposition", response.headers)
            for name in ("", "../escape", r"..\escape", "bad\r\nX: y", ".."):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    FileResponse(path, filename=name)
            with self.assertRaises(FileNotFoundError):
                FileResponse(path.parent / "missing")

    def test_redirect_validation_and_headers(self):
        headers = {"location": "/wrong", "X-Test": "yes"}
        response = RedirectResponse("/target?x=1", status_code=307, headers=headers)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["Location"], "/target?x=1")
        self.assertEqual(headers["location"], "/wrong")
        self.assertNotIn("location", response.headers)
        for location in ("", "/ok\r\nX: bad", "/bad\x00"):
            with self.subTest(location=location), self.assertRaises(ValueError):
                RedirectResponse(location)
        for status in (200, 304, 399):
            with self.subTest(status=status), self.assertRaises(ValueError):
                RedirectResponse("/target", status)

    def test_sse_multiline_and_metadata_validation(self):
        event = ServerSentEvent("a\r\nb\rc\n", event="notice", id="42", retry=0)
        self.assertEqual(
            event.encode(),
            "event: notice\nid: 42\nretry: 0\ndata: a\ndata: b\ndata: c\ndata: \n\n",
        )
        self.assertEqual(ServerSentEvent("").encode(), "data: \n\n")
        for event in (
            ServerSentEvent("x", event="bad\nname"),
            ServerSentEvent("x", id="bad\0id"),
            ServerSentEvent("x", retry=-1),
            ServerSentEvent("x", retry=True),
        ):
            with self.subTest(event=event), self.assertRaises(ValueError):
                event.encode()

    def test_sse_delivers_events_incrementally(self):
        source = """
import asyncio
import os
from pathlib import Path
from pyferrum import EventSourceResponse, Ferrum

app = Ferrum()
@app.get("/events")
def events(request):
    async def source():
        yield "first"
        while not Path(os.environ["RELEASE_FILE"]).exists():
            await asyncio.sleep(0.01)
        yield "second"
    return EventSourceResponse(source(), headers={"cache-control": "no-store"})

app.run(host="127.0.0.1", port=int(os.environ["PORT"]))
"""
        with tempfile.TemporaryDirectory() as directory:
            port = pick_port()
            release = Path(directory) / "release"
            env = {
                **os.environ,
                "PORT": str(port),
                "RELEASE_FILE": str(release),
                "PYTHONPATH": os.pathsep.join(
                    [str(ROOT / "python"), os.environ.get("PYTHONPATH", "")]
                ),
            }
            process = subprocess.Popen(
                [sys.executable, "-c", source],
                cwd=directory,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                wait_for(port)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/events", timeout=5
                ) as response:
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(response.readline(), b"data: first\n")
                    self.assertEqual(response.readline(), b"\n")
                    release.touch()
                    self.assertEqual(response.read(), b"data: second\n\n")
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    unittest.main()
