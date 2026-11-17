"""web_mail routes with fakes; mail wiring flows are in test_web and test_web_accounts."""
from types import SimpleNamespace

from igris import web_mail
from web_serving import FakeRequest, login


def ctx(**kw):
    base = dict(inbox=None, approvals=None, notices=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_inbox_disabled_and_enabled():
    h = FakeRequest()
    web_mail.inbox(h, ctx(), "/api/inbox")
    assert h.body == {"status": {"state": "disabled"}, "mails": []}
    inbox = SimpleNamespace(status=lambda: {"state": "ok"},
                            store=SimpleNamespace(recent=lambda n: [{"id": n}]))
    web_mail.inbox(h, ctx(inbox=inbox), "/api/inbox")
    assert h.body == {"status": {"state": "ok"}, "mails": [{"id": 50}]}


def test_approvals_are_off_without_a_flow():
    h = FakeRequest(login=login())
    web_mail.approvals(h, ctx(), "/api/approvals")
    assert h.status == 404
    web_mail.answer(h, ctx(), "/api/approvals/answer", {})
    assert h.status == 404


def test_answer_checks_types():
    answered = []
    flow = SimpleNamespace(answer=lambda *a: answered.append(a))
    h = FakeRequest(login=login("u"))
    web_mail.answer(h, ctx(approvals=flow), "/api/approvals/answer", {"id": "r", "approved": 1})
    assert h.status == 400 and answered == []
    web_mail.answer(h, ctx(approvals=flow), "/api/approvals/answer", {"id": "r", "approved": True})
    assert h.status == 200 and answered == [("u", "r", True)]


def test_notices():
    h = FakeRequest("/api/notices?unread=1")
    web_mail.notices(h, ctx(), "/api/notices")
    assert h.body == {"notices": [], "unread": 0}
    store = SimpleNamespace(list=lambda owner, unread_only: [owner, unread_only],
                            unread_count=lambda owner: 2, mark_read=lambda owner, ids: len(ids),
                            mark_all_read=lambda owner: 9)
    web_mail.notices(h, ctx(notices=store), "/api/notices")
    assert h.body == {"notices": ["owner", True], "unread": 2}
    for bad in ({}, {"ids": []}, {"ids": [1]}, {"ids": ["x"] * 501}):
        web_mail.notices_read(h, ctx(notices=store), "/api/notices/read", bad)
        assert h.status == 400, bad
    web_mail.notices_read(h, ctx(notices=store), "/api/notices/read", {"ids": ["a", "b"]})
    assert h.body == {"marked": 2}
    web_mail.notices_read(h, ctx(notices=store), "/api/notices/read", {"all": True})
    assert h.body == {"marked": 9}


def test_mail_model_needs_claude():
    assert web_mail.mail_model(SimpleNamespace(backends=("echo",))) is None
    calls = []
    backend = SimpleNamespace(complete=lambda system, messages: calls.append(system) or "ok")
    read = web_mail.mail_model(SimpleNamespace(backends=("claude",)), backend_factory=lambda: backend)
    assert read("sys", "mail") == "ok" and calls == ["sys"]
