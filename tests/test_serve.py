import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from kzbonds import serve


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_link_preview_picture_is_served(server):
    status, headers, body = _get(server + "/static/og-image.png")
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert "max-age" in headers["Cache-Control"]
    assert body.startswith(b"\x89PNG")
    # Open Graph image of 1200x627, as the page's og:image tags say.
    assert int.from_bytes(body[16:20], "big") == 1200 and int.from_bytes(body[20:24], "big") == 627


def test_other_paths_stay_closed(server):
    assert _get(server + "/static/")[0] == 404
    assert _get(server + "/static/../serve.py")[0] == 404
    assert _get(server + "/healthz")[:1] == (200,)
