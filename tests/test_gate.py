import pytest

from igris.gate import GatedRouter, request_is_private
from igris.llm import BackendError, Message, RateLimited

MSGS = [Message("user", "hi")]


class Fake:
    def __init__(self, name, *, trains=False, fail=None, answer="ok"):
        self.name = name
        self.trains_on_data = trains
        self.fail = fail
        self.answer = answer
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self.answer


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_private_request_skips_training_provider():
    train, safe = Fake("train", trains=True), Fake("safe")
    router = GatedRouter([train, safe])
    for _ in range(4):
        _, name = router.complete("s", MSGS)
        assert name == "safe"
    assert train.calls == 0


def test_public_request_may_use_training_provider():
    train = Fake("train", trains=True)
    router = GatedRouter([train])
    assert router.complete("s", MSGS, private=False) == ("ok", "train")


def test_override_flag_lets_private_through():
    train = Fake("train", trains=True)
    router = GatedRouter([train], allow_training=True)
    assert router.complete("s", MSGS)[1] == "train"


def test_only_training_backends_gives_clear_error():
    router = GatedRouter([Fake("train", trains=True)])
    with pytest.raises(BackendError, match="IGRIS_ALLOW_TRAINING_PROVIDERS"):
        router.complete("s", MSGS)


def test_fallback_order_and_reported_backend():
    a = Fake("a", fail=BackendError("boom"))
    b = Fake("b", answer="from b")
    router = GatedRouter([a, b])
    assert router.complete("s", MSGS) == ("from b", "b")


def test_all_fail_lists_every_error():
    router = GatedRouter([Fake("a", fail=BackendError("x")), Fake("b", fail=BackendError("y"))])
    with pytest.raises(BackendError) as info:
        router.complete("s", MSGS)
    assert "a: x" in str(info.value) and "b: y" in str(info.value)


def test_rate_limited_backend_rests_then_recovers():
    clock = Clock()
    a = Fake("a", fail=RateLimited("a", 30))
    b = Fake("b")
    router = GatedRouter([a, b], clock)
    router.complete("s", MSGS)
    calls = a.calls
    router.complete("s", MSGS)
    router.complete("s", MSGS)
    assert a.calls == calls  # still resting
    clock.now = 31
    a.fail = None
    seen = {router.complete("s", MSGS)[1] for _ in range(4)}
    assert "a" in seen


def test_three_failures_in_window_skips_backend_then_recovers():
    clock = Clock()
    a = Fake("a", fail=BackendError("down"))
    b = Fake("b")
    router = GatedRouter([a, b], clock)
    for _ in range(8):
        router.complete("s", MSGS)
        clock.now += 1
    assert a.calls == 3
    assert router.health()["a"]["state"] == "skipped"
    clock.now += 700
    a.fail = None
    assert "a" in {router.complete("s", MSGS)[1] for _ in range(4)}
    assert router.health()["a"]["state"] == "ok"


def test_failures_outside_window_do_not_add_up():
    clock = Clock()
    a = Fake("a", fail=BackendError("down"))
    router = GatedRouter([a], clock)
    for _ in range(5):
        with pytest.raises(BackendError):
            router.complete("s", MSGS)
        clock.now += 400
    assert a.calls == 5
    assert router.health()["a"]["state"] == "ok"


def test_success_clears_failure_count():
    a = Fake("a", fail=BackendError("x"))
    router = GatedRouter([a])
    for _ in range(2):
        with pytest.raises(BackendError):
            router.complete("s", MSGS)
    a.fail = None
    router.complete("s", MSGS)
    assert router.health()["a"]["recent_failures"] == 0


def test_health_reports_training_flag():
    router = GatedRouter([Fake("t", trains=True), Fake("s")])
    report = router.health()
    assert report["t"]["trains_on_data"] is True and report["s"]["trains_on_data"] is False


def test_flag_is_read_from_provider_attribute():
    class Provider:
        trains_on_data = True

    class Compat:
        name = "compat"
        provider = Provider()

        def complete(self, system, messages):
            return "x"

    router = GatedRouter([Compat(), Fake("safe")])
    assert {router.complete("s", MSGS)[1] for _ in range(3)} == {"safe"}


def test_request_is_private_defaults():
    assert request_is_private(has_attachments=True)
    assert request_is_private(uses_memory=True)
    assert not request_is_private()
def test_ranked_gate_uses_domain_and_falls_back_after_rate_limit():
    from igris.llm import Message, RateLimited

    class Fake:
        def __init__(self, name, trains=False, limited=False):
            self.name = name
            self.trains_on_data = trains
            self.limited = limited
            self.calls = 0

        def complete(self, system, messages):
            self.calls += 1
            if self.limited:
                raise RateLimited(self.name, 60)
            return self.name

    train = Fake("train", trains=True)
    top = Fake("top", limited=True)
    second = Fake("second")
    router = GatedRouter([second, top, train],
                         ranker=lambda domain: ["train", "top", "second"])
    assert router.complete("", [Message("user", "Fix Python code")]) == ("second", "second")
    assert train.calls == 0 and top.calls == 1
    assert router.last_decision == {"domain": "code", "ranked": ["train", "top", "second"],
                                    "chosen": "second"}
