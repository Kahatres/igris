"""web_files routes with a fake request handler; HTTP flows are in test_web_accounts."""
import base64
from types import SimpleNamespace

from igris import web_files
from igris.attachments import AttachmentStore
from web_serving import FakeRequest, login

OWNER = "b" * 32


def ctx(tmp_path, **kw):
    files = AttachmentStore(tmp_path / "files.sqlite3")
    base = dict(accounts=object(), contexts=SimpleNamespace(files=files, core=None))
    base.update(kw)
    return SimpleNamespace(**base)


def test_files_need_accounts(tmp_path):
    h = FakeRequest(login=login(OWNER))
    web_files.files(h, ctx(tmp_path, accounts=None), "/api/files")
    assert h.status == 404


def test_upload_list_and_bad_content(tmp_path):
    c = ctx(tmp_path)
    h = FakeRequest(login=login(OWNER))
    web_files.files(h, c, "/api/files/upload",
                    {"name": "a.txt", "content": base64.b64encode(b"hi").decode()})
    assert h.status == 201 and h.body["name"] == "a.txt"
    web_files.files(h, c, "/api/files")
    assert h.status == 200 and [f["name"] for f in h.body["files"]] == ["a.txt"]
    web_files.files(h, c, "/api/files/upload", {"name": "b.txt", "content": "***"})
    assert h.status == 400
    web_files.files(h, c, "/api/files/nothing")
    assert h.status == 404


def test_upload_limit_allows_the_base64_of_the_largest_file(tmp_path):
    c = ctx(tmp_path)
    limit = web_files.upload_limit(c, 1000)
    assert limit == 4 * ((c.contexts.files.max_file + 2) // 3) + 1000


def test_core_is_off_without_a_core_store(tmp_path):
    h = FakeRequest("/api/core", login=login(OWNER))
    web_files.core(h, ctx(tmp_path), "/api/core")
    assert h.status == 404
    web_files.delete_document(h, ctx(tmp_path))
    assert h.status == 404
