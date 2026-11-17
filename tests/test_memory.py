import json

import pytest

from igris.memory import Memory


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        self.now += 1.0
        return self.now


@pytest.fixture
def mem(tmp_path):
    return Memory(tmp_path / "sub" / "dir" / "mem.jsonl", max_history=3, clock=FakeClock())


def test_missing_file_is_empty(mem):
    assert len(mem) == 0
    assert mem.history() == []
    assert mem.search("x") == []
    mem.clear()


def test_append_returns_entry_and_creates_dirs(mem):
    entry = mem.append("user", "merhaba")
    assert entry == {"ts": 1001.0, "role": "user", "content": "merhaba"}
    assert mem.path.exists()
    assert len(mem) == 1


def test_backend_is_stored_only_when_given_and_old_lines_still_read(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_text(json.dumps({"ts": 1.0, "role": "assistant", "content": "old"}) + "\n",
                    encoding="utf-8")
    mem = Memory(path, clock=FakeClock())
    entry = mem.append("assistant", "new", backend="codex")
    assert entry == {"ts": 1001.0, "role": "assistant", "content": "new", "backend": "codex"}
    assert "backend" not in mem.append("user", "hi")
    hist = Memory(path).history()
    assert [e["content"] for e in hist] == ["old", "new", "hi"]
    assert "backend" not in hist[0] and hist[1]["backend"] == "codex"
    with pytest.raises(ValueError):
        mem.append("assistant", "x", backend=3)


def test_round_trip_across_instances(tmp_path):
    path = tmp_path / "m.jsonl"
    a = Memory(path, clock=FakeClock())
    a.append("user", "İstanbul'a gidiyorum")
    a.append("assistant", "Güzel şehir")
    b = Memory(path)
    hist = b.history()
    assert [e["role"] for e in hist] == ["user", "assistant"]
    assert hist[0]["content"] == "İstanbul'a gidiyorum"
    assert len(b) == 2


def test_utf8_not_escaped(mem):
    mem.append("user", "İstanbul ğüşıö")
    text = mem.path.read_text(encoding="utf-8")
    assert "İstanbul ğüşıö" in text
    assert len(text.strip().splitlines()) == 1


def test_history_limit_and_order(mem):
    for i in range(5):
        mem.append("user", f"m{i}")
    assert [e["content"] for e in mem.history()] == ["m2", "m3", "m4"]
    assert [e["content"] for e in mem.history(2)] == ["m3", "m4"]
    assert len(mem.history(10)) == 5


def test_search_newest_first_and_limit(mem):
    mem.append("user", "İstanbul one")
    mem.append("assistant", "other")
    mem.append("user", "İSTANBUL two")
    res = mem.search("İstanbul")
    assert [e["content"] for e in res] == ["İSTANBUL two", "İstanbul one"]
    assert len(mem.search("İstanbul", limit=1)) == 1
    assert mem.search("İstanbul", limit=1)[0]["content"] == "İSTANBUL two"


def test_search_case_insensitive_ascii(mem):
    mem.append("user", "Hello World")
    assert len(mem.search("hello")) == 1
    assert len(mem.search("WORLD")) == 1


def test_empty_query_returns_empty(mem):
    mem.append("user", "abc")
    assert mem.search("") == []


def test_corrupted_lines_skipped(tmp_path):
    path = tmp_path / "m.jsonl"
    good = {"ts": 1.0, "role": "user", "content": "ok"}
    lines = [
        json.dumps(good),
        "{not json",
        "[1, 2]",
        "42",
        '"str"',
        "",
        json.dumps({"ts": 2.0, "role": "user"}),
        json.dumps({"ts": "x", "role": "user", "content": "bad"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    m = Memory(path)
    assert m.history() == [good]
    assert len(m) == 1
    assert m.search("ok") == [good]


def test_invalid_utf8_does_not_raise(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_bytes(b"\xff\xfe\n" + json.dumps(
        {"ts": 1.0, "role": "tool", "content": "fine"}).encode() + b"\n")
    assert len(Memory(path)) == 1


def test_validation(tmp_path, mem):
    with pytest.raises(ValueError):
        mem.append("system", "x")
    with pytest.raises(ValueError):
        mem.append("user", 123)
    with pytest.raises(ValueError):
        Memory(tmp_path / "a", max_history=0)
    with pytest.raises(ValueError):
        mem.history(0)
    with pytest.raises(ValueError):
        mem.search("x", limit=0)
    assert len(mem) == 0


def test_all_roles_accepted(mem):
    for role in ("user", "assistant", "tool"):
        mem.append(role, "x")
    assert len(mem) == 3


def test_clear(mem):
    mem.append("user", "a")
    mem.clear()
    assert len(mem) == 0
    assert mem.history() == []
    mem.append("user", "b")
    assert len(mem) == 1


def test_history_reads_only_needed_tail_of_10k_entries(tmp_path, monkeypatch):
    path = tmp_path / "large.jsonl"
    entries = [{"ts": i, "role": "user", "content": f"message {i}"} for i in range(10_000)]
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    import igris.memory as module
    original_loads = module.json.loads
    parsed = []
    reads = []
    original_open = open

    class TrackedFile:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.fh.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.fh, name)

        def read(self, size=-1):
            assert 0 < size <= 8192
            data = self.fh.read(size)
            reads.append(len(data))
            return data

    def tracked_open(file, mode="r", **kwargs):
        fh = original_open(file, mode, **kwargs)
        return TrackedFile(fh) if mode == "rb" else fh

    def loads(raw):
        parsed.append(raw)
        return original_loads(raw)

    monkeypatch.setattr(module.json, "loads", loads)
    monkeypatch.setattr(module, "open", tracked_open, raising=False)
    mem = Memory(path)
    assert mem.history(3) == entries[-3:]
    assert len(parsed) == 3
    assert 0 < sum(reads) <= 8192 < path.stat().st_size
    parsed.clear()
    assert len(mem) == 10_000
    assert len(parsed) == 10_000  # validate each line once, without building a full history
    parsed.clear()
    assert len(mem) == 10_000
    assert parsed == []  # unchanged files reuse their validated count
    Memory(path).append("assistant", "new")
    assert len(mem) == 10_001  # another instance's write invalidates the count
    mem.clear()
    assert len(mem) == 0
    mem.append("user", "after clear")
    assert len(mem) == 1


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("tail", [b'{"ts": 99, "role": "user", "content":', b"not json\n",
                                  b'{"ts": true, "role": "user", "content": "bad"}',
                                  b"[]\n\n", b""])
def test_tail_keeps_corrupt_partial_and_universal_newline_handling(tmp_path, newline, tail):
    path = tmp_path / "tail.jsonl"
    entries = [{"ts": i, "role": "user", "content": "long \u0130stanbul " + "x" * 9000}
               for i in range(4)]
    path.write_bytes(newline.join(json.dumps(e, ensure_ascii=False).encode("utf-8")
                                  for e in entries) + newline + tail)
    mem = Memory(path)
    assert mem.history(2) == entries[-2:]
    assert mem.history(10) == entries
    assert len(mem) == 4


def test_history_reads_valid_unterminated_last_line(tmp_path):
    path = tmp_path / "tail.jsonl"
    good = {"ts": 1, "role": "tool", "content": "valid \u0130stanbul"}
    path.write_bytes(b"not json\n" + json.dumps(good, ensure_ascii=False).encode("utf-8"))
    assert Memory(path).history(1) == [good]
    assert len(Memory(path)) == 1
