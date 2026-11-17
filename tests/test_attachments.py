"""Private file storage and model-tool access tests."""

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from igris.attachments import AttachmentStore, FileError, FileLimit, FileMissing
from igris.tools import ToolError, ToolRegistry

OWNER = "a" * 32
OTHER = "b" * 32


def test_upload_roundtrip_ownership_and_duplicate_names(tmp_path):
    store = AttachmentStore(tmp_path / "files.sqlite3")
    first = store.upload(OWNER, "notlar.txt", b"private")
    second = store.upload(OWNER, "notlar.txt", b"second")
    assert first["id"] != second["id"]
    assert store.download(OWNER, first["id"]) == ("notlar.txt", b"private")
    assert store.list(OTHER)["files"] == []
    for action in (store.download, store.delete, store.read_text):
        with pytest.raises(FileMissing):
            action(OTHER, first["id"])
    assert store.events(OTHER) == []
    store.delete(OWNER, first["id"])
    assert store.list(OWNER)["used_bytes"] == 6
    assert store.events(OWNER)[0]["action"] == "delete"
    assert AttachmentStore(store.path).list(OWNER)["files"][0]["id"] == second["id"]


@pytest.mark.parametrize("name", ["../a", "C:\\a", "a/b", "a:b", "..", "bad\r\nheader", "", "a" * 201])
def test_invalid_names_never_write(tmp_path, name):
    store = AttachmentStore(tmp_path / "f.sqlite3")
    with pytest.raises(FileError):
        store.upload(OWNER, name, b"x")
    assert not store.list(OWNER)["files"]


def test_size_and_storage_quotas_and_delete_release(tmp_path):
    store = AttachmentStore(tmp_path / "f.sqlite3", max_file=5, max_total=7)
    with pytest.raises(FileLimit):
        store.upload(OWNER, "large", b"123456")
    first = store.upload(OWNER, "a", b"12345")
    with pytest.raises(FileLimit):
        store.upload(OWNER, "b", b"123")
    store.upload(OTHER, "b", b"12345")
    store.delete(OWNER, first["id"])
    store.upload(OWNER, "b", b"123")


def test_concurrent_count_quota_is_atomic(tmp_path):
    store = AttachmentStore(tmp_path / "f.sqlite3", max_count=1)
    def put(_):
        try:
            store.upload(OWNER, "x", b"x")
            return True
        except FileLimit:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(put, range(2))) == 1


def test_model_reads_only_own_text_and_marks_truncation(tmp_path):
    store = AttachmentStore(tmp_path / "f.sqlite3")
    own = store.upload(OWNER, "code.py", ("x" * 20001).encode())
    other = store.upload(OTHER, "private.txt", b"secret")
    binary = store.upload(OWNER, "binary.dat", b"\x00\xff")
    registry = ToolRegistry()
    store.register_tools(registry, OWNER)
    text = json.loads(registry.run("read_upload", {"file_id": own["id"]}))
    assert text["truncated"] and len(text["text"]) == 20000
    with pytest.raises(ToolError):
        registry.run("read_upload", {"file_id": other["id"]})
    with pytest.raises(ToolError, match="UTF-8"):
        registry.run("read_upload", {"file_id": binary["id"]})
    assert "private.txt" not in registry.run("list_uploads", {})


def test_read_upload_pages_long_text_and_reads_documents(tmp_path):
    import io
    import zipfile

    store = AttachmentStore(tmp_path / "f.sqlite3")
    long = store.upload(OWNER, "long.txt", ("a" * 20000 + "b" * 5).encode())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes.txt", "inside the zip")
    archive = store.upload(OWNER, "task.zip", buf.getvalue())
    registry = ToolRegistry()
    store.register_tools(registry, OWNER)
    first = json.loads(registry.run("read_upload", {"file_id": long["id"]}))
    assert first["next_offset"] == 20000 and first["total_chars"] == 20005
    rest = json.loads(registry.run("read_upload", {"file_id": long["id"], "offset": 20000}))
    assert rest["text"] == "bbbbb" and rest["next_offset"] is None and not rest["truncated"]
    assert "inside the zip" in json.loads(
        registry.run("read_upload", {"file_id": archive["id"]}))["text"]
    with pytest.raises(ToolError, match="offset"):
        registry.run("read_upload", {"file_id": long["id"], "offset": -1})


def test_create_file_saves_text_and_word_documents_for_the_owner_only(tmp_path):
    from igris.doctext import extract_text
    from igris.tools import parse_tool_call

    store = AttachmentStore(tmp_path / "f.sqlite3")
    reg = ToolRegistry()
    store.register_tools(reg, OWNER)
    assert "create_file" in reg.names()
    name, args = parse_tool_call('TOOL: create_file {"name": "Ödev şablonu.docx"}\n<<<CONTENT\n'
                                 '# Practical Work 7\n\n| A | B |\n|---|---|\n| 1 | 2 |\n>>>CONTENT')
    saved = json.loads(reg.run(name, args))
    assert saved["name"] == "Ödev şablonu.docx" and saved["download"] == f"/api/files/{saved['id']}/download"
    data = store.download(OWNER, saved["id"])[1]
    assert data[:2] == b"PK" and "Practical Work 7" in extract_text("x.docx", data)
    note = json.loads(reg.run("create_file", {"name": "notes.md", "content": "# Not\nçalış"}))
    assert store.download(OWNER, note["id"])[1] == "# Not\nçalış".encode("utf-8")
    assert len(store.list(OWNER)["files"]) == 2 and store.list(OTHER)["files"] == []
    for bad in ({"name": "x.exe", "content": "a"}, {"name": "x.md", "content": "  "},
                {"name": "../x.md", "content": "a"}, {"name": "x.md"}):
        with pytest.raises(ToolError):
            reg.run("create_file", bad)
