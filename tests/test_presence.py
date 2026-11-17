"""Tests for the user presence state machine."""

import threading

import pytest

from igris.presence import CATEGORIES, STATES, PresenceTracker, Report


class FakeClock:
    """Deterministic clock used by every test."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def make_tracker(clock, hour=12, **kwargs):
    return PresenceTracker(clock=clock, local_hour=lambda _ts: hour, **kwargs)


# --------------------------------------------------------------- Report


def test_report_accepts_every_category():
    for category in CATEGORIES:
        report = Report("laptop", category, 0.0, 10.0)
        assert report.category == category


def test_report_rejects_unknown_category():
    with pytest.raises(ValueError):
        Report("laptop", "gaming", 0.0, 10.0)


def test_report_rejects_negative_idle():
    with pytest.raises(ValueError):
        Report("laptop", "coding", -1.0, 10.0)


@pytest.mark.parametrize("device", ["", "   "])
def test_report_rejects_empty_device(device):
    with pytest.raises(ValueError):
        Report(device, "coding", 0.0, 10.0)


def test_report_is_frozen():
    report = Report("laptop", "coding", 0.0, 10.0)
    with pytest.raises(Exception):
        report.category = "media"  # type: ignore[misc]


# ---------------------------------------------------------------- states


def test_coding_gives_deep_work():
    clock = FakeClock()
    tracker = make_tracker(clock)
    tracker.update(Report("laptop", "coding", 5.0, clock.now))
    snapshot = tracker.state()
    assert snapshot["state"] == "deep_work"
    assert snapshot["device"] == "laptop"


def test_studying_gives_study():
    clock = FakeClock()
    tracker = make_tracker(clock)
    tracker.update(Report("laptop", "studying", 5.0, clock.now))
    assert tracker.state()["state"] == "study"


@pytest.mark.parametrize("category", ["browsing", "media", "other"])
def test_other_categories_give_idle(category):
    clock = FakeClock()
    tracker = make_tracker(clock)
    tracker.update(Report("laptop", category, 5.0, clock.now))
    assert tracker.state()["state"] == "idle"


def test_high_idle_gives_idle_even_while_coding():
    clock = FakeClock()
    tracker = make_tracker(clock, idle_after=600.0)
    tracker.update(Report("laptop", "coding", 600.0, clock.now))
    assert tracker.state()["state"] == "idle"


def test_idle_just_below_threshold_keeps_working_state():
    clock = FakeClock()
    tracker = make_tracker(clock, idle_after=600.0)
    tracker.update(Report("laptop", "coding", 599.0, clock.now))
    assert tracker.state()["state"] == "deep_work"


def test_away_without_reports_outside_quiet_hours():
    clock = FakeClock()
    tracker = make_tracker(clock, hour=12)
    snapshot = tracker.state()
    assert snapshot["state"] == "away"
    assert snapshot["device"] is None


def test_sleeping_without_reports_inside_quiet_hours():
    clock = FakeClock()
    tracker = make_tracker(clock, hour=3)
    snapshot = tracker.state()
    assert snapshot["state"] == "sleeping"
    assert snapshot["device"] is None


def test_quiet_hours_wrap_around_midnight():
    clock = FakeClock()
    tracker = PresenceTracker(clock=clock, quiet_hours=(23, 6), local_hour=lambda _ts: 1)
    assert tracker.state()["state"] == "sleeping"
    tracker = PresenceTracker(clock=clock, quiet_hours=(23, 6), local_hour=lambda _ts: 12)
    assert tracker.state()["state"] == "away"


def test_quiet_hours_boundaries_are_half_open():
    clock = FakeClock()
    assert make_tracker(clock, hour=1).state()["state"] == "sleeping"
    assert make_tracker(clock, hour=6).state()["state"] == "sleeping"
    assert make_tracker(clock, hour=7).state()["state"] == "away"


def test_every_state_is_reachable_and_declared():
    clock = FakeClock()
    seen = set()
    tracker = make_tracker(clock, hour=12)
    seen.add(tracker.state()["state"])  # away
    tracker = make_tracker(clock, hour=3)
    seen.add(tracker.state()["state"])  # sleeping
    tracker = make_tracker(clock)
    tracker.update(Report("laptop", "coding", 0.0, clock.now))
    seen.add(tracker.state()["state"])  # deep_work
    tracker.update(Report("laptop", "studying", 0.0, clock.now))
    seen.add(tracker.state()["state"])  # study
    tracker.update(Report("laptop", "browsing", 0.0, clock.now))
    seen.add(tracker.state()["state"])  # idle
    assert seen == set(STATES)


# ----------------------------------------------------------------- stale


def test_report_becomes_stale_after_stale_after():
    clock = FakeClock()
    tracker = make_tracker(clock, stale_after=300.0)
    tracker.update(Report("laptop", "coding", 0.0, clock.now))
    assert tracker.state()["state"] == "deep_work"
    clock.advance(300.0)
    assert tracker.state()["state"] == "deep_work"
    clock.advance(1.0)
    assert tracker.state()["state"] == "away"


def test_stale_incoming_report_is_ignored():
    clock = FakeClock()
    tracker = make_tracker(clock, stale_after=300.0)
    clock.advance(1_000.0)
    tracker.update(Report("laptop", "coding", 0.0, clock.now - 500.0))
    snapshot = tracker.state()
    assert snapshot["state"] == "away"
    assert snapshot["device"] is None


# ------------------------------------------------------------ two devices


def test_lowest_idle_device_decides():
    clock = FakeClock()
    tracker = make_tracker(clock)
    tracker.update(Report("desktop", "coding", 30.0, clock.now))
    tracker.update(Report("phone", "browsing", 2.0, clock.now))
    snapshot = tracker.state()
    assert snapshot["state"] == "idle"
    assert snapshot["device"] == "phone"


def test_freshest_active_device_takes_over():
    clock = FakeClock()
    tracker = make_tracker(clock, stale_after=300.0)
    tracker.update(Report("desktop", "coding", 10.0, clock.now))
    clock.advance(100.0)
    tracker.update(Report("phone", "browsing", 1.0, clock.now))
    assert tracker.state()["device"] == "phone"
    clock.advance(250.0)  # desktop report is now stale, phone is still fresh
    snapshot = tracker.state()
    assert snapshot["device"] == "phone"  # phone is the only fresh device
    assert snapshot["state"] == "idle"  # phone still says idle

    clock.advance(51.0)  # now the phone report is stale too
    assert tracker.state()["state"] == "away"


# ---------------------------------------------------------------- 'since'


def test_since_is_stable_while_state_is_unchanged():
    clock = FakeClock()
    tracker = make_tracker(clock)
    tracker.update(Report("laptop", "coding", 0.0, clock.now))
    first = tracker.state()["since"]
    assert first == clock.now
    clock.advance(60.0)
    tracker.update(Report("laptop", "coding", 0.0, clock.now))
    snapshot = tracker.state()
    assert snapshot["state"] == "deep_work"
    assert snapshot["since"] == first


def test_since_changes_with_state():
    clock = FakeClock()
    tracker = make_tracker(clock)
    tracker.update(Report("laptop", "coding", 0.0, clock.now))
    first = tracker.state()["since"]
    clock.advance(10.0)
    tracker.update(Report("laptop", "browsing", 0.0, clock.now))
    snapshot = tracker.state()
    assert snapshot["state"] == "idle"
    assert snapshot["since"] == clock.now
    assert snapshot["since"] != first


# ------------------------------------------------------------- on_change


def test_on_change_fires_once_per_real_change():
    clock = FakeClock()
    tracker = make_tracker(clock)
    calls = []
    tracker.on_change(lambda old, new: calls.append((old, new)))

    tracker.update(Report("laptop", "coding", 0.0, clock.now))
    assert calls == []  # the first evaluation is not a change

    clock.advance(1.0)
    tracker.update(Report("laptop", "browsing", 0.0, clock.now))
    assert calls == [("deep_work", "idle")]

    # Repeated reads and reports with the same outcome do not fire again.
    tracker.state()
    tracker.state()
    clock.advance(1.0)
    tracker.update(Report("laptop", "other", 0.0, clock.now))
    assert calls == [("deep_work", "idle")]


def test_on_change_supports_multiple_listeners():
    clock = FakeClock()
    tracker = make_tracker(clock)
    first = []
    second = []
    tracker.on_change(lambda old, new: first.append((old, new)))
    tracker.on_change(lambda old, new: second.append((old, new)))

    tracker.update(Report("laptop", "coding", 0.0, clock.now))
    clock.advance(1.0)
    tracker.update(Report("laptop", "browsing", 0.0, clock.now))
    assert first == [("deep_work", "idle")]
    assert second == [("deep_work", "idle")]


def test_on_change_fires_when_going_stale():
    clock = FakeClock()
    tracker = make_tracker(clock, stale_after=300.0)
    calls = []
    tracker.on_change(lambda old, new: calls.append((old, new)))
    tracker.update(Report("laptop", "coding", 0.0, clock.now))
    clock.advance(301.0)
    assert tracker.state()["state"] == "away"
    assert calls == [("deep_work", "away")]


# ---------------------------------------------------------- thread safety


def test_concurrent_updates_and_reads_stay_consistent():
    clock = FakeClock()
    tracker = make_tracker(clock)
    errors = []

    def worker(name):
        try:
            for i in range(200):
                tracker.update(Report(name, "coding", float(i % 5), clock.now))
                snapshot = tracker.state()
                assert snapshot["state"] in STATES
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"dev{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert tracker.state()["state"] in STATES


@pytest.mark.parametrize("field", ["idle_seconds", "at"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), "5", None, True])
def test_report_rejects_values_that_are_not_finite_numbers(field, value):
    values = {"device": "d", "category": "coding", "idle_seconds": 1.0, "at": 1.0, field: value}
    with pytest.raises(ValueError):
        Report(**values)
