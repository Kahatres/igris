import pytest

from igris.approval_flow import ApprovalFlow
from igris.approvals import ApprovalError, ApprovalStore
from igris.notify import Answer, NotifyError

OWNER, OTHER = "a" * 32, "b" * 32


class Channel:
    def __init__(self, fail=False):
        self.asked, self.batches, self.offsets, self.fail = [], [], [], fail

    def ask(self, text, request_id):
        if self.fail:
            raise NotifyError("channel is down")
        self.asked.append((text, request_id))

    def poll_answers(self, offset=None):
        self.offsets.append(offset)
        return (self.batches.pop(0) if self.batches else []), 7


class ReplyingChannel(Channel):
    def __init__(self, reply_fails=False):
        super().__init__()
        self.replies, self.reply_fails = [], reply_fails

    def acknowledge(self, answer, result, *, already_answered=False):
        self.replies.append((answer.request_id, result, already_answered))
        if self.reply_fails:
            raise NotifyError("telegram is down")


def make(tmp_path, channel=None):
    woken = []
    flow = ApprovalFlow(ApprovalStore(str(tmp_path / "a.sqlite3")), channel,
                        lambda request_id, approved: woken.append((request_id, approved)))
    return flow, woken


def test_request_tells_the_human(tmp_path):
    channel = Channel()
    flow, _ = make(tmp_path, channel)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={"to": "x"})
    assert channel.asked == [("Send it?", request_id)]
    assert [item["request_id"] for item in flow.pending(OWNER)] == [request_id]


def test_request_survives_a_channel_failure(tmp_path):
    flow, _ = make(tmp_path, Channel(fail=True))
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})
    assert flow.store.status(request_id) == "pending"


def test_web_answer_opens_the_gate_and_wakes_the_waiter(tmp_path):
    flow, woken = make(tmp_path)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={"to": "x"})
    flow.answer(OWNER, request_id, True)
    assert woken == [(request_id, True)]
    flow.store.consume(OWNER, request_id, "mail", payload={"to": "x"})


def test_web_answer_cannot_reach_another_owners_request(tmp_path):
    flow, woken = make(tmp_path)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})
    with pytest.raises(ApprovalError):
        flow.answer(OTHER, request_id, True)
    assert flow.store.status(request_id) == "pending" and woken == []


def test_pump_applies_channel_answers_and_keeps_the_offset(tmp_path):
    channel = Channel()
    flow, woken = make(tmp_path, channel)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})
    channel.batches.append([Answer(request_id, False, "telegram")])
    assert flow.pump() == 1
    flow.pump()
    assert channel.offsets == [None, 7]
    assert flow.store.status(request_id) == "denied" and woken == [(request_id, False)]


def test_pump_skips_replays_and_unknown_ids(tmp_path):
    channel = Channel()
    flow, woken = make(tmp_path, channel)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})
    channel.batches.append([Answer(request_id, True, "telegram"), Answer(request_id, False, "telegram"),
                            Answer("missing", True, "telegram")])
    assert flow.pump() == 1
    assert flow.store.status(request_id) == "approved" and woken == [(request_id, True)]


def test_pump_without_a_two_way_channel_does_nothing(tmp_path):
    class OneWay:
        def ask(self, text, request_id):
            pass

    assert make(tmp_path, OneWay())[0].pump() == 0
    assert make(tmp_path)[0].pump() == 0


def press(channel, flow, request_id, approved):
    channel.batches.append([Answer(request_id, approved, "telegram")])
    return flow.pump()


@pytest.mark.parametrize("approved, result", [(True, "✅ Onaylandı"), (False, "❌ Reddedildi")])
def test_a_counted_press_is_answered_with_the_recorded_result(tmp_path, approved, result):
    channel = ReplyingChannel()
    flow, _ = make(tmp_path, channel)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})

    assert press(channel, flow, request_id, approved) == 1

    assert channel.replies == [(request_id, result, False)]


def test_a_late_press_reports_the_decision_that_stands(tmp_path):
    channel = ReplyingChannel()
    flow, _ = make(tmp_path, channel)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})
    press(channel, flow, request_id, True)

    assert press(channel, flow, request_id, False) == 0

    assert channel.replies[-1] == (request_id, "✅ Onaylandı", True)
    assert flow.store.status(request_id) == "approved"


def test_a_late_press_on_a_spent_approval_still_reads_approved(tmp_path):
    channel = ReplyingChannel()
    flow, _ = make(tmp_path, channel)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={"to": "x"})
    press(channel, flow, request_id, True)
    flow.store.consume(OWNER, request_id, "mail", payload={"to": "x"})

    press(channel, flow, request_id, False)

    assert channel.replies[-1] == (request_id, "✅ Onaylandı", True)


def test_a_press_on_an_expired_request_says_so(tmp_path):
    channel = ReplyingChannel()
    now = [1000.0]
    flow = ApprovalFlow(ApprovalStore(str(tmp_path / "a.sqlite3"), clock=lambda: now[0]), channel)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={}, ttl=10)
    now[0] += 20

    assert press(channel, flow, request_id, True) == 0

    assert channel.replies == [(request_id, "⌛ Süresi doldu", True)]


def test_a_press_on_an_unknown_request_is_answered_too(tmp_path):
    channel = ReplyingChannel()
    flow, _ = make(tmp_path, channel)

    assert press(channel, flow, "nope", True) == 0

    assert channel.replies == [("nope", "Bu istek artık geçerli değil", True)]


def test_a_failed_reply_does_not_lose_the_answer(tmp_path):
    channel = ReplyingChannel(reply_fails=True)
    flow, woken = make(tmp_path, channel)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})

    assert press(channel, flow, request_id, False) == 1

    assert flow.store.status(request_id) == "denied"
    assert woken == [(request_id, False)]


def test_request_writes_an_admin_panel_notice(tmp_path):
    from igris.notices import NoticeStore

    notices = NoticeStore(tmp_path / "n.sqlite3")
    flow = ApprovalFlow(ApprovalStore(str(tmp_path / "a.sqlite3")), notices=notices)
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})
    [item] = notices.list(OWNER)
    assert item["kind"] == "approval" and item["title"] == "Send it?"
    assert item["ref"] == request_id and item["action"] == {"approval": request_id}
    assert notices.list(OTHER) == []


def test_a_broken_notice_store_does_not_stop_the_request(tmp_path):
    class Broken:
        def add(self, *a, **k):
            raise OSError("disk full")

    flow = ApprovalFlow(ApprovalStore(str(tmp_path / "a.sqlite3")), notices=Broken())
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})
    assert flow.store.status(request_id) == "pending"


class UpdatesChannel(Channel):
    """A channel with one poll for answers and owner messages."""

    def __init__(self):
        super().__init__()
        self.updates = []

    def poll_answers(self, offset=None):
        raise AssertionError("a second reader of the channel's updates")

    def poll_updates(self, offset=None):
        self.offsets.append(offset)
        answers, messages = self.updates.pop(0) if self.updates else ([], [])
        return answers, messages, 9


def test_pump_reads_answers_and_messages_with_one_poll(tmp_path):
    channel = UpdatesChannel()
    flow, woken = make(tmp_path, channel)
    seen = []
    flow.on_message = seen.append
    request_id = flow.request(OWNER, "mail", "Send it?", payload={})
    channel.updates = [([Answer(request_id, True, "telegram")], ["bugün ne var?"])]
    assert flow.pump() == 1
    assert woken == [(request_id, True)] and seen == ["bugün ne var?"]
    flow.pump()
    assert channel.offsets == [None, 9]


def test_failing_message_handler_retains_offset_and_preserves_message_order(tmp_path):
    channel = UpdatesChannel()
    flow, _ = make(tmp_path, channel)
    channel.updates = [([], ["one"])]
    assert flow.pump() == 0  # no on_message: the text is dropped

    def broken(message):
        seen.append(message)
        raise RuntimeError("bad")

    seen = []
    flow.on_message = broken
    channel.updates = [([], ["a", "b"])]
    assert flow.pump() == 0 and seen == ["a"]
    channel.updates = [([], ["a", "b"])]
    flow.on_message = seen.append
    assert flow.pump() == 0 and seen == ["a", "a", "b"]
    flow.pump()
    assert channel.offsets == [None, 9, 9, 9]
