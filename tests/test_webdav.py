"""Nextcloud WebDAV client against a tiny fake DAV server."""
import base64
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from channel_builder import webdav

TREE = {"Docs/": None, "Docs/Sci ence/": None, "Docs/Sci ence/a.pdf": b"%PDF-a", "Docs/b.mp3": b"ID3b"}
AUTH = "Basic " + base64.b64encode(b"teacher:pw").decode()


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self):
        if self.headers.get("Authorization") != AUTH:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_PROPFIND(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self._ok():
            return
        root = "/remote.php/dav/files/teacher/"
        path = urllib.parse.unquote(self.path)[len(root):]
        if self.headers.get("Depth") == "0":
            items = [""]
        else:
            items = [path] + [k for k in TREE if k.startswith(path) and k != path and "/" not in k[len(path):].rstrip("/")]
        resp = ""
        for k in items:
            coll = "<d:collection/>" if (k == "" or k.endswith("/")) else ""
            resp += ("<d:response><d:href>%s</d:href><d:propstat><d:prop><d:resourcetype>%s</d:resourcetype>"
                     "<oc:owner-display-name>Ms Teacher</oc:owner-display-name></d:prop></d:propstat></d:response>"
                     % (urllib.parse.quote(root + k), coll))
        body = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">%s</d:multistatus>' % resp).encode()
        self.send_response(207)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._ok():
            return
        k = urllib.parse.unquote(self.path)[len("/remote.php/dav/files/teacher/"):]
        data = TREE[k]
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def dav():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def test_login_and_display_name(dav):
    assert webdav.Nextcloud(dav, "teacher", "pw").display_name() == "Ms Teacher"
    with pytest.raises(webdav.AuthError):
        webdav.Nextcloud(dav, "teacher", "wrong").display_name()


def test_download_tree(dav, tmp_path):
    n = webdav.Nextcloud(dav, "teacher", "pw").download("Docs", str(tmp_path))
    assert n == 2
    assert (tmp_path / "Sci ence" / "a.pdf").read_bytes() == b"%PDF-a"
    assert (tmp_path / "b.mp3").read_bytes() == b"ID3b"
