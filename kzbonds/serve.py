"""
The whole public service in one process: a background thread refreshes the
data on schedule (kzbonds.scheduler) and this module hands out the page it
builds.

Only three addresses answer: "/" and "/index.html" return the page,
"/healthz" returns "ok". Everything else is 404 - there is no file tree to
browse.
"""

from __future__ import annotations

import gzip
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from kzbonds import config, scheduler

logger = logging.getLogger("kzbonds.serve")

PLACEHOLDER = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>KZ Bond Monitor</title>
</head>
<body style="font-family:sans-serif;padding:40px 16px;text-align:center">
<h1>KZ Bond Monitor</h1>
<p>Данные загружаются. Страница появится через несколько минут и обновится сама.</p>
</body>
</html>
"""


class _Page:
    """The built page, re-read from disk only when the file changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._raw = b""
        self._gzipped = b""

    def get(self) -> tuple[bytes, bytes] | None:
        path = config.SITE_INDEX_PATH
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return None
        with self._lock:
            if mtime != self._mtime:
                self._raw = path.read_bytes()
                self._gzipped = gzip.compress(self._raw, compresslevel=6)
                self._mtime = mtime
            return self._raw, self._gzipped


_page = _Page()


class Handler(BaseHTTPRequestHandler):
    server_version = "kzbonds"

    def do_GET(self) -> None:
        self._respond(send_body=True)

    def do_HEAD(self) -> None:
        self._respond(send_body=False)

    def _respond(self, send_body: bool) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8", send_body)
        elif path in ("/", "/index.html"):
            self._send_page(send_body)
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8", send_body)

    def _send_page(self, send_body: bool) -> None:
        page = _page.get()
        if page is None:
            self._send(200, PLACEHOLDER.encode("utf-8"), "text/html; charset=utf-8", send_body)
            return
        raw, gzipped = page
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            self._send(200, gzipped, "text/html; charset=utf-8", send_body, encoding="gzip")
        else:
            self._send(200, raw, "text/html; charset=utf-8", send_body)

    def _send(self, status: int, body: bytes, content_type: str, send_body: bool, encoding: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("X-Content-Type-Options", "nosniff")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - signature of the base class
        logger.debug("%s %s", self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=scheduler.run_forever, name="scheduler", daemon=True).start()

    port = int(os.getenv("PORT", "80"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    logger.info("Serving the page on port %d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
