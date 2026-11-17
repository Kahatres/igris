"""Tests for igris.events: SSE formatting, change detection and the stream loop (no real waiting)."""
import pytest

from igris.events import ChangeWatcher, comment, format_event, stream


def test_format_event_with_dict_data():
    assert format_event("change", {"keys": ["tasks"], "n": "ş"}) == \
        'event: change\ndata: {"keys":["tasks"],"n":"ş"}\n\n'.encode("utf-8")


def test_format_event_splits_text_lines():
    assert format_event("note", "a\nb\r\nc\rd") == b"event: note\ndata: a\ndata: b\ndata: c\ndata: d\n\n"


def test_format_event_line_order():
    assert format_event("hello", "x", event_id=4, retry=3000) == \
        b"retry: 3000\nid: 4\nevent: hello\ndata: x\n\n"


@pytest.mark.parametrize("kwargs", [
    {"event": ""}, {"event": "a\nb"}, {"event": "a\rb"}, {"event": 3},
    {"event": "e", "event_id": -1}, {"event": "e", "event_id": True}, {"event": "e", "event_id": 1.5},
    {"event": "e", "retry": 0}, {"event": "e", "retry": -5}, {"event": "e", "retry": 2.0},
])
def test_format_event_rejects_bad_input(kwargs):
    event = kwargs.pop("event")
    with pytest.raises(ValueError):
        format_event(event, {}, **kwargs)


def test_comment():
    assert comment() == b": ping\n\n"
    assert comment("hi") == b": hi\n\n"
    with pytest.raises(ValueError):
        comment("a\nb")


def test_watcher_first_poll_returns_all_then_nothing():
    w = ChangeWatcher({"tasks": lambda: [1], "inbox": lambda: {"a": 1}})
    assert w.poll() == ["tasks", "inbox"]
    assert w.poll() == []


def test_watcher_detects_changes_and_ignores_key_order():
    box = {"v": {"a": 1, "b": 2}}
    w = ChangeWatcher({"x": lambda: box["v"]})
    w.poll()
    box["v"] = {"b": 2, "a": 1}
    assert w.poll() == []
    box["v"] = {"a": 1, "b": 3}
    assert w.poll() == ["x"]


def test_failing_source_counts_once_and_recovers():
    state = {"fail": False}

    def source():
        if state["fail"]:
            raise RuntimeError("locked")
        return 1

    w = ChangeWatcher({"forge": source, "ok": lambda: 2})
    assert w.poll() == ["forge", "ok"]
    state["fail"] = True
    assert w.poll() == ["forge"]
    assert w.errors == {"forge": "locked"}
    assert w.poll() == []
    state["fail"] = False
    assert w.poll() == ["forge"]
    assert w.errors == {}


@pytest.mark.parametrize("sources", [{}, {"Bad": lambda: 1}, {"": lambda: 1}, {"x" * 33: lambda: 1},
                                     {"ok": 5}, None])
def test_watcher_rejects_bad_sources(sources):
    with pytest.raises(ValueError):
        ChangeWatcher(sources)


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_stream_hello_changes_heartbeat_and_end():
    t = FakeTime()
    values = iter([1, 1, 2] + [2] * 100)
    out = []
    count = stream(ChangeWatcher({"tasks": lambda: next(values)}), out.append,
                   interval=2, heartbeat=5, max_seconds=12, sleep=t.sleep, clock=t.clock)
    assert out[0] == b"retry: 3000\nevent: hello\ndata: {\"retry\":3000}\n\n"
    changes = [o for o in out if b"event: change" in o]
    assert changes[0].startswith(b"id: 1\n") and changes[1].startswith(b"id: 2\n")
    assert b'"keys":["tasks"]' in changes[0]
    assert b": ping\n\n" in out
    assert count == 2
    assert t.now == 12  # stopped at max_seconds


def test_stream_stops_when_the_client_leaves():
    t = FakeTime()
    calls = []

    def write(chunk):
        calls.append(chunk)
        if len(calls) == 2:
            raise BrokenPipeError()

    count = stream(ChangeWatcher({"a": lambda: 1}), write, sleep=t.sleep, clock=t.clock)
    assert len(calls) == 2 and count == 1


@pytest.mark.parametrize("name", ["interval", "heartbeat", "max_seconds"])
@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True])
def test_stream_rejects_bad_timing(name, bad):
    with pytest.raises(ValueError):
        stream(ChangeWatcher({"a": lambda: 1}), lambda b: None, **{name: bad})
