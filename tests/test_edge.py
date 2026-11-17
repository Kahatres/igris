"""Tests for the Igris edge daemon.

All probes and network access are faked: these tests never touch the
network, real windows APIs, the filesystem or the clock.
"""

import json
import logging
import threading

import pytest

from igris import edge
from igris.presence import PresenceTracker


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _script(values, default):
    """Return a probe that yields *values* once, then repeats *default*."""
    remaining = list(values)

    def probe():
        if remaining:
            return remaining.pop(0)
        return default

    return probe


def _clock(values, stop):
    """Return a clock that yields *values* once, then repeats the last one.

    When the scripted times run out the stop event is set so that the
    daemon loop under test terminates.
    """
    remaining = list(values)
    state = {"now": values[-1]}

    def clock():
        if remaining:
            state["now"] = remaining.pop(0)
        else:
            stop.set()
        return state["now"]

    return clock


class RecordingOpener:
    """Fake urllib opener that records the JSON bodies it is given."""

    def __init__(self):
        self.payloads = []
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.payloads.append(json.loads(request.data.decode("utf-8")))
        return object()


class FailingOpener:
    """Fake urllib opener that always fails."""

    def open(self, request, timeout=None):
        raise OSError("connection refused sekret")


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------

def test_report_has_exactly_four_keys_and_never_includes_the_title():
    payload = edge.report(
        "laptop",
        lambda: "Secret Project - Visual Studio Code",
        lambda: 42.0,
        lambda: 123.5,
    )

    assert set(payload.keys()) == {"device", "category", "idle_seconds", "at"}
    assert payload == {
        "device": "laptop",
        "category": "coding",
        "idle_seconds": 42.0,
        "at": 123.5,
    }
    # The raw window title must never leak into the payload.
    assert "Secret Project" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# categorize()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,expected",
    [
        ("main.py - Visual Studio Code", "coding"),
        ("PyCharm", "coding"),
        ("VISUAL STUDIO CODE", "coding"),
        ("lecture.pdf - SumatraPDF", "studying"),
        ("Obsidian - my notes", "studying"),
        ("Google Chrome", "browsing"),
        ("Mozilla Firefox", "browsing"),
        ("Microsoft Edge", "browsing"),
        ("YouTube - Google Chrome", "media"),
        ("Netflix - Mozilla Firefox", "media"),
        ("Spotify", "media"),
        ("totally unknown window", "other"),
        ("", "other"),
    ],
)
def test_categorize(title, expected):
    assert edge.categorize(title) == expected


def test_categorize_media_wins_over_browsing():
    # The title matches both the browsing and media rules; media wins.
    assert edge.categorize("YouTube - Google Chrome") == "media"


def test_categorize_with_custom_rules():
    rules = {"coding": ["vim"], "media": ["vlc"]}
    assert edge.categorize("editing in vim", rules) == "coding"
    assert edge.categorize("watching vlc", rules) == "media"
    # Custom rules replace the defaults entirely.
    assert edge.categorize("Google Chrome", rules) == "other"


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------

def test_send_success_posts_json_with_bearer_token():
    opener = RecordingOpener()
    payload = {
        "device": "laptop",
        "category": "coding",
        "idle_seconds": 1.5,
        "at": 123.0,
    }

    assert edge.send("http://hub.example/report", "sekret", payload, opener) is True

    (request,) = opener.requests
    assert request.full_url == "http://hub.example/report"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer sekret"
    assert opener.payloads == [payload]


def test_send_failure_returns_false_without_raising_or_leaking_the_token(caplog):
    payload = {"device": "laptop", "category": "other", "idle_seconds": 0.0, "at": 1.0}

    with caplog.at_level(logging.WARNING, logger="igris.edge"):
        result = edge.send(
            "http://hub.example/report", "sekret", payload, FailingOpener()
        )

    assert result is False
    # A warning was logged, but the token must never appear in it.
    assert caplog.records, "expected a warning to be logged"
    assert "sekret" not in caplog.text


# ---------------------------------------------------------------------------
# run(): send-on-change, idle crossing and heartbeat rules
# ---------------------------------------------------------------------------

def test_run_sends_on_change_idle_crossing_and_heartbeat():
    opener = RecordingOpener()
    stop = threading.Event()
    title_probe = _script(
        ["VS Code", "VS Code", "Chrome", "Chrome", "Chrome", "Chrome", "Chrome"],
        "Chrome",
    )
    idle_probe = _script([0.0, 0.0, 0.0, 0.0, 700.0, 700.0, 700.0], 700.0)
    clock = _clock([0, 10, 20, 30, 40, 50, 350], stop)

    edge.run(
        "laptop",
        "http://hub.example/report",
        "sekret",
        interval=0,
        stop=stop,
        title_probe=title_probe,
        idle_probe=idle_probe,
        clock=clock,
        opener=opener,
    )

    assert [(p["category"], p["idle_seconds"], p["at"]) for p in opener.payloads] == [
        ("coding", 0.0, 0),     # first report
        ("browsing", 0.0, 20),  # category changed
        ("browsing", 700.0, 40),  # idle crossed the 600 s threshold
        ("browsing", 700.0, 350),  # heartbeat (>= 300 s since last send)
    ]


def test_run_sends_when_idle_drops_back_below_threshold():
    opener = RecordingOpener()
    stop = threading.Event()
    title_probe = _script(["Chrome"] * 4, "Chrome")
    idle_probe = _script([700.0, 700.0, 5.0, 5.0], 5.0)
    clock = _clock([0, 10, 20, 30], stop)

    edge.run(
        "laptop",
        "http://hub.example/report",
        "sekret",
        interval=0,
        stop=stop,
        title_probe=title_probe,
        idle_probe=idle_probe,
        clock=clock,
        opener=opener,
    )

    assert [(p["idle_seconds"], p["at"]) for p in opener.payloads] == [
        (700.0, 0),  # first report (already idle)
        (5.0, 20),   # idle crossed back below the threshold
    ]


def test_failed_send_is_retried_on_the_next_probe():
    class FlakyOpener(RecordingOpener):
        def open(self, request, timeout=None):
            if not self.requests:
                self.requests.append(request)
                raise OSError("hub is down")
            return super().open(request, timeout)

    opener = FlakyOpener()
    stop = threading.Event()

    edge.run("laptop", "http://hub.example/report", "sekret", interval=0, stop=stop,
             title_probe=lambda: "Chrome", idle_probe=lambda: 0.0,
             clock=_clock([0, 10, 20], stop), opener=opener)

    assert [p["at"] for p in opener.payloads] == [10]


def test_send_closes_the_response():
    closed = []

    class Response:
        def close(self):
            closed.append(True)

    class Opener:
        def open(self, request, timeout=None):
            return Response()

    assert edge.send("http://hub.example/report", "sekret", {}, Opener()) is True
    assert closed == [True]


def test_heartbeat_keeps_a_quiet_device_fresh_on_the_hub():
    assert edge.HEARTBEAT_SECONDS + 30 < PresenceTracker()._stale_after


@pytest.mark.parametrize("now,then,expected", [
    (5000, 1000, 4.0),
    (-2147483000, 2147483000, 1.296),  # signed tick count after 24.8 days of uptime
    (1000, 4294966296, 2.0),           # wrapped after 49.7 days
])
def test_elapsed_seconds_survives_tick_count_wrap(now, then, expected):
    assert edge._elapsed_seconds(now, then) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def test_main_refuses_to_start_without_a_token(monkeypatch, capsys):
    monkeypatch.delenv("IGRIS_EDGE_TOKEN", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        edge.main(["--url", "http://hub.example/report", "--device", "laptop"])

    assert excinfo.value.code != 0
    stderr = capsys.readouterr().err
    assert "IGRIS_EDGE_TOKEN" in stderr


def test_main_passes_arguments_and_reads_the_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("IGRIS_EDGE_TOKEN", "sekret")
    recorded = {}

    def fake_run(*args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs

    monkeypatch.setattr(edge, "run", fake_run)

    edge.main(
        ["--url", "http://hub.example/report", "--device", "laptop", "--interval", "5"]
    )

    assert recorded["args"][:3] == ("laptop", "http://hub.example/report", "sekret")
    assert recorded["kwargs"]["interval"] == 5.0
