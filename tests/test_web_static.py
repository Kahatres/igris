"""Static file serving: allowed names, ETags and HTTP caching."""
import os
from http.client import HTTPResponse

import pytest

from igris import web
from igris.web_static import StaticFiles, allowed, etag_matches
from test_web import FakeAssistant
from test_web_accounts import Connection, Server


def test_allowed_names():
    assert allowed("v2.js") and allowed("app.css") and allowed("study_fixture.json")
    assert allowed("vendor/three/three.module.min.js")
    assert allowed("vendor/mathlive/fonts/KaTeX_Main-Regular.woff2")
    for name in ("x.py", "../web.py", "V2.JS", "vendor/three/LICENSE", "sub/app.js", "study.html"):
        assert not allowed(name)


def test_etag_matching():
    assert etag_matches('"a"', '"a"') and etag_matches('W/"a"', '"a"')
    assert etag_matches('"b", "a"', '"a"') and etag_matches("*", '"a"')
    assert not etag_matches(None, '"a"') and not etag_matches('"b"', '"a"')


def test_load_gives_content_type_and_a_content_etag(tmp_path):
    (tmp_path / "a.js").write_bytes(b"one")
    (tmp_path / "b.js").write_bytes(b"one")
    files = StaticFiles(tmp_path)
    data, ctype, etag = files.load("a.js")
    assert data == b"one" and ctype.startswith("text/javascript")
    assert etag.startswith('"') and etag.endswith('"')
    assert files.load("b.js")[2] == etag  # same content, same tag
    assert files.load("x.py") is None and files.load("missing.js") is None


def test_etag_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "a.js"
    path.write_bytes(b"one")
    files = StaticFiles(tmp_path)
    first = files.load("a.js")
    reads = []
    original = type(path).read_bytes
    monkeypatch.setattr(type(path), "read_bytes", lambda self: reads.append(self) or original(self))
    assert files.load("a.js") == first and reads == []  # unchanged: served from memory
    path.write_bytes(b"two!")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    second = files.load("a.js")
    assert second[0] == b"two!" and second[2] != first[2] and len(reads) == 1


def test_files_outside_the_root_are_refused(tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    (tmp_path / "secret.js").write_bytes(b"x")
    assert StaticFiles(root).load("secret.js") is None


def get(server, path, headers=""):
    raw = f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1:8770\r\n{headers}\r\n".encode()
    conn = Connection(raw)
    server.RequestHandlerClass(conn, ("127.0.0.1", 1), server)
    response = HTTPResponse(Connection(bytes(conn.output)))
    response.begin()
    return response, response.read()


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    (tmp_path / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "sw.js").write_text("self.x = 1", encoding="utf-8")
    (tmp_path / "v2.html").write_text("<html></html>", encoding="utf-8")
    return web.create_server(FakeAssistant(), port=0, static_dir=tmp_path,
                             roadmap_loader=lambda: {})


def test_static_files_revalidate_with_an_etag(server, tmp_path):
    response, body = get(server, "/static/app.js")
    assert response.status == 200 and body == b"console.log(1)"
    assert response.getheader("Cache-Control") == "no-cache"
    assert response.getheader("ETag") == StaticFiles(tmp_path).load("app.js")[2]
    assert response.getheader("X-Content-Type-Options") == "nosniff"


def test_matching_if_none_match_gives_304_without_a_body(server):
    etag = get(server, "/static/app.js")[0].getheader("ETag")
    response, body = get(server, "/static/app.js", f"If-None-Match: {etag}\r\n")
    assert response.status == 304 and body == b""
    assert response.getheader("ETag") == etag and response.getheader("Content-Length") is None
    assert response.getheader("Cache-Control") == "no-cache"
    response, body = get(server, "/static/app.js", 'If-None-Match: "other"\r\n')
    assert response.status == 200 and body == b"console.log(1)"


def test_pages_service_worker_and_api_stay_uncached(server):
    for path in ("/", "/sw.js", "/api/state", "/api/roadmap"):
        response, _ = get(server, path)
        assert response.status == 200, path
        assert response.getheader("Cache-Control") == "no-store", path
        assert response.getheader("ETag") is None, path


def test_missing_static_file_is_a_404(server):
    response, _ = get(server, "/static/missing.js")
    assert response.status == 404 and response.getheader("Cache-Control") == "no-store"
