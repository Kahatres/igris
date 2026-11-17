import os

import pytest

from igris.tools import ToolError, ToolRegistry
from igris.tools_files import Workspace, register_file_tools


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / "a.txt").write_text("one\nTwo Needle\nthree\n")
    (root / "sub" / "b.py").write_text("x = 1\n  needle here  \n")
    (root / ".hidden" / "c.txt").write_text("needle secret\n")
    (root / ".dot.txt").write_text("needle dot\n")
    (root / "bin.dat").write_bytes(b"needle\x00\x01")
    (tmp_path / "outside.txt").write_text("secret needle\n")
    return Workspace(root)


def test_resolve_ok_and_escape(ws, tmp_path):
    assert ws.resolve("a.txt") == ws.root / "a.txt"
    with pytest.raises(ToolError):
        ws.resolve("../outside.txt")
    with pytest.raises(ToolError):
        ws.resolve(str(tmp_path / "outside.txt"))


def test_symlink_escape(ws, tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("no symlink")
    try:
        os.symlink(tmp_path / "outside.txt", ws.root / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlink not permitted")
    with pytest.raises(ToolError):
        ws.read_file("link.txt")
    assert "secret" not in ws.search_text("needle")


def test_read_file_lines(ws):
    assert ws.read_file("a.txt") == "1: one\n2: Two Needle\n3: three"
    assert ws.read_file("a.txt", 2, 2) == "2: Two Needle"


def test_read_file_truncate(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 100)
    w = Workspace(tmp_path, max_bytes=10)
    out = w.read_file("big.txt")
    assert out.endswith("... [truncated]")
    assert len(out) < 40


def test_read_file_errors(ws):
    for p in ("missing.txt", "sub", "bin.dat"):
        with pytest.raises(ToolError):
            ws.read_file(p)


def test_list_dir(ws):
    assert ws.list_dir() == "a.txt\nbin.dat\nsub/"
    assert ws.list_dir("sub") == "b.py"
    with pytest.raises(ToolError):
        ws.list_dir("nope")


def test_search(ws):
    out = ws.search_text("NEEDLE")
    assert out.splitlines() == ["a.txt:2: Two Needle", "sub/b.py:2: needle here"]
    assert ws.search_text("needle", glob="*.py") == "sub/b.py:2: needle here"
    assert ws.search_text("zzz") == "no matches"
    with pytest.raises(ToolError):
        ws.search_text("")


def test_search_limit(tmp_path):
    (tmp_path / "f.txt").write_text("hit\n" * 5)
    w = Workspace(tmp_path, max_results=3)
    lines = w.search_text("hit").splitlines()
    assert len(lines) == 4
    assert lines[-1] == "... [more results]"


def test_registry(ws):
    reg = ToolRegistry()
    register_file_tools(reg, ws)
    assert {"read_file", "list_dir", "search_text"} <= set(reg.names())
    assert reg.run("read_file", {"path": "a.txt", "start": 3}) == "3: three"
    assert "a.txt/" not in reg.run("list_dir", {})
    assert "a.txt:2" in reg.run("search_text", {"query": "needle"})
    with pytest.raises(ToolError):
        reg.run("read_file", {"path": "../outside.txt"})
