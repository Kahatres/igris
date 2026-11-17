"""Exercise the approval boundary using durable stores and fake transports."""

from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from igris.approval_flow import ApprovalFlow
from igris.approvals import ApprovalStore
from igris.followups import FollowupStore
from igris.gmail import GmailError, GmailScopeError, GmailUncertainError
from igris.mail_compose import ComposeError
from igris.mail_send import MailSender, TELEGRAM_LIMIT, approval_text, thread_key
from igris.outbox import OutboxStore, payload


OWNER = "owner"


class Notifier:
    def __init__(self):
        self.messages = []
        self.questions = []

    def send(self, text):
        self.messages.append(text)

    def ask(self, text, request_id):
        self.questions.append((text, request_id))


class Gmail:
    def __init__(self):
        self.calls = []
        self.error = None

    def send(self, raw, *, thread_id=None):
        self.calls.append((raw, thread_id))
        if self.error:
            raise self.error
        return {"id": "message-1", "threadId": "thread-1"}


@pytest.fixture
def env(tmp_path):
    now = [100000.0]
    clock = lambda: now[0]
    outbox = OutboxStore(str(tmp_path / "outbox.db"), clock=clock)
    approvals = ApprovalStore(str(tmp_path / "approvals.db"), clock=clock,
                              default_ttl=60)
    followups = FollowupStore(tmp_path / "followups.db", clock=clock)
    notifier, gmail = Notifier(), Gmail()
    flow = ApprovalFlow(approvals, notifier)
    options = dict(outbox=outbox, flow=flow, notifier=notifier,
                   followups=followups, gmail_client=lambda: gmail,
                   school_address="owner@school.example", clock=clock)
    return SimpleNamespace(now=now, outbox=outbox, approvals=approvals,
                           followups=followups, notifier=notifier, gmail=gmail,
                           options=options, sender=MailSender(**options))


def draft(env, **changes):
    values = dict(identity="personal", to=["ece@example.com"],
                  cc=["deniz@example.com"], subject="Öğrenci duyurusu: ė ž",
                  body="Merhaba Ece,\nÖğrencilere duyurur musunuz? ė ž\nTeşekkürler.")
    values.update(changes)
    return env.outbox.create(OWNER, **values)


def answer(env, request_id, approved=True):
    env.approvals.answer(request_id, approved, "telegram")
    assert env.sender.handle_answer(OWNER, request_id, approved) is True


def test_no_send_without_approval(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    assert env.gmail.calls == []
    assert env.outbox.get(OWNER, item.id).status == "awaiting"
    assert env.approvals.status(request_id) == "pending"


def test_approved_personal_mail_is_sent_once(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    answer(env, request_id)
    assert env.sender.handle_answer(OWNER, request_id, True)
    assert len(env.gmail.calls) == 1
    sent = env.outbox.get(OWNER, item.id)
    assert (sent.status, sent.sent_message_id, sent.sent_thread_id) == (
        "sent", "message-1", "thread-1")
    timers = env.followups.list(OWNER)
    assert len(timers) == 1
    assert timers[0]["thread_id"] == "thread-1"
    assert timers[0]["note"] == item.subject
    assert timers[0]["due_at"] == env.now[0] + 3 * 86400
    assert env.approvals.status(request_id) == "used"


def test_sent_bytes_match_what_was_shown(env):
    item = draft(env, thread_id="original-thread", in_reply_to="<reply@example.com>",
                 references="<root@example.com> <reply@example.com>")
    request_id = env.sender.ask(OWNER, item.id)
    shown = env.notifier.questions[-1][0]
    answer(env, request_id)
    raw, thread_id = env.gmail.calls[0]
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(parsed["To"]) == item.to[0]
    assert str(parsed["Cc"]) == item.cc[0]
    assert str(parsed["Subject"]) == item.subject
    assert parsed.get_content() == item.body
    assert all(value in shown for value in (*item.to, *item.cc, item.subject, item.body))
    assert parsed["In-Reply-To"] == item.in_reply_to
    assert parsed["References"] == item.references
    assert thread_id == item.thread_id


def test_revised_text_cannot_use_old_approval(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    env.outbox.revise(OWNER, item.id, body="changed")
    answer(env, request_id)
    assert not env.gmail.calls
    assert "değiş" in env.notifier.messages[-1]
    assert env.outbox.get(OWNER, item.id).body == "changed"


def test_rejected_cancels(env):
    item = draft(env)
    answer(env, env.sender.ask(OWNER, item.id), False)
    assert env.outbox.get(OWNER, item.id).status == "cancelled"
    assert not env.gmail.calls
    assert "iptal" in env.notifier.messages[-1]


def test_expired_approval_does_not_send(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    env.approvals.answer(request_id, True, "web")
    env.now[0] += 61
    assert env.sender.handle_answer(OWNER, request_id, True)
    assert not env.gmail.calls
    assert env.outbox.get(OWNER, item.id).status in {"draft", "failed"}
    assert "onay" in env.notifier.messages[-1].lower()


@pytest.mark.parametrize("error", [GmailError("Gmail API returned HTTP 500"),
                                   RuntimeError("SECRET_TOKEN BODY_MARKER")])
def test_gmail_failure_marks_failed_without_secret(env, error):
    item = draft(env, body="BODY_MARKER")
    request_id = env.sender.ask(OWNER, item.id)
    env.gmail.error = error
    answer(env, request_id)
    failed = env.outbox.get(OWNER, item.id)
    assert failed.status == "failed"
    assert failed.error
    assert env.notifier.messages
    for sensitive in ("BODY_MARKER", "SECRET_TOKEN"):
        assert sensitive not in failed.error + "".join(env.notifier.messages)
    env.sender.handle_answer(OWNER, request_id, True)
    assert len(env.gmail.calls) == 1
    assert not env.followups.list(OWNER)


def test_missing_scope_message_names_the_command(env):
    item = draft(env)
    env.gmail.error = GmailScopeError("secret")
    answer(env, env.sender.ask(OWNER, item.id))
    assert "gmail_auth.py login" in env.notifier.messages[-1]
    assert "secret" not in env.outbox.get(OWNER, item.id).error


def test_school_identity_hands_link_and_asks_confirmation(env):
    item = draft(env, identity="school")
    answer(env, env.sender.ask(OWNER, item.id))
    assert not env.gmail.calls
    assert env.outbox.get(OWNER, item.id).status == "handed"
    link = next(text for text in env.notifier.messages
                if text.startswith("https://outlook.office.com/mail/deeplink/compose?"))
    fields = parse_qs(urlsplit(link).query)
    assert fields == {"to": list(item.to), "cc": list(item.cc),
                      "subject": [item.subject], "body": [item.body]}
    pending = env.approvals.pending(OWNER)
    assert len(pending) == 1 and pending[0]["action"] == "mail.confirm_sent"
    assert not env.followups.list(OWNER)
    answer(env, pending[0]["request_id"])
    assert env.outbox.get(OWNER, item.id).status == "sent"
    assert env.followups.list(OWNER)[0]["thread_id"] == thread_key(item)
    env.sender.handle_answer(OWNER, pending[0]["request_id"], True)
    assert len(env.followups.list(OWNER)) == 1


def test_school_long_body_is_sent_in_full(env):
    item = draft(env, identity="school", body="ş" * 6000)
    request_id = env.sender.ask(OWNER, item.id)
    env.notifier.messages.clear()
    answer(env, request_id)
    assert item.body in "".join(env.notifier.messages)
    assert all(len(text) <= TELEGRAM_LIMIT for text in env.notifier.messages)
    link = next(text for text in env.notifier.messages if text.startswith("https://"))
    assert "body" not in parse_qs(urlsplit(link).query)


@pytest.mark.parametrize("identity,label", [("personal", "Kişisel (Gmail)"),
                                           ("school", "Okul")])
def test_approval_text_shows_identity_recipients_subject(env, identity, label):
    item = draft(env, identity=identity)
    parts = approval_text(item, names={item.to[0]: "Ece"})
    assert len(parts) == 1
    assert all(value in parts[-1] for value in
               (label, *item.to, *item.cc, item.subject, item.body, "Ece"))


def test_long_personal_body_is_not_cut(env):
    item = draft(env, body="a" * 9000)
    env.sender.ask(OWNER, item.id)
    parts = env.notifier.messages + [env.notifier.questions[-1][0]]
    assert item.body in "".join(parts)
    assert all(len(text) <= TELEGRAM_LIMIT for text in parts)
    assert all(value in parts[-1] for value in (*item.to, *item.cc, item.subject))


@pytest.mark.parametrize("body", ["kısa metin", "a" * 9000, ("ė" * 2000 + "\n") * 4])
def test_stored_approval_summary_holds_the_whole_body(env, body):
    """The web panel approves from the stored summary, so it must show the full mail."""
    notices = []

    class Notices:
        def add(self, owner, kind, text, **kwargs):
            notices.append(text)

    env.sender.flow.notices = Notices()
    item = draft(env, body=body)
    request_id = env.sender.ask(OWNER, item.id)
    [stored] = env.approvals.pending(OWNER)
    assert stored["request_id"] == request_id and stored["action"] == "mail.send"
    for text in (stored["summary"], notices[-1]):
        assert body in text
        assert all(value in text for value in (*item.to, *item.cc, item.subject))
    question, asked_id = env.notifier.questions[-1]
    assert asked_id == request_id and len(question) <= TELEGRAM_LIMIT
    answer(env, request_id)
    assert len(env.gmail.calls) == 1


def test_long_body_splits_on_line_breaks_without_changing_text(env):
    item = draft(env, body=("ė" * 2000 + "\n") * 4)
    parts = approval_text(item)
    assert "".join(parts[:-1]) == item.body
    assert all(part.endswith("\n") for part in parts[:-1])


def test_unknown_request_id_returns_false(env):
    assert env.sender.handle_answer(OWNER, "unknown", True) is False
    foreign = env.approvals.request(OWNER, "unrelated", "question", payload={})
    assert env.sender.handle_answer(OWNER, foreign, True) is False
    assert not env.notifier.messages


def test_daily_limit_message(env):
    env.outbox.daily_limit = 1
    first, second = draft(env), draft(env)
    answer(env, env.sender.ask(OWNER, first.id))
    request_id = env.sender.ask(OWNER, second.id)
    answer(env, request_id)
    assert len(env.gmail.calls) == 1
    assert "limit" in env.notifier.messages[-1].lower()
    assert env.approvals.status(request_id) == "approved"


def test_begin_consume_send_order(env, monkeypatch):
    events = []
    for obj, name in ((env.outbox, "begin_send"), (env.approvals, "consume"),
                      (env.gmail, "send")):
        original = getattr(obj, name)
        def record(*args, _original=original, _name=name, **kwargs):
            events.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(obj, name, record)
    answer(env, env.sender.ask(OWNER, draft(env).id))
    assert events == ["begin_send", "consume", "send"]


def test_unrecorded_approval_cannot_send(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    env.sender.handle_answer(OWNER, request_id, True)
    assert not env.gmail.calls
    assert env.outbox.get(OWNER, item.id).status == "failed"


def test_owner_isolation(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    env.approvals.answer(request_id, True, "web")
    assert env.sender.handle_answer("other", request_id, True) is False
    assert env.outbox.get(OWNER, item.id).status == "awaiting"


def test_ask_again_invalidates_old_request_even_with_same_text(env):
    item = draft(env)
    old = env.sender.ask(OWNER, item.id)
    new = env.sender.ask(OWNER, item.id)
    assert env.outbox.get(OWNER, item.id).revision == 2
    answer(env, old)
    assert not env.gmail.calls
    answer(env, new)
    assert len(env.gmail.calls) == 1


def test_restart_preserves_stale_request_association(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    env.outbox.revise(OWNER, item.id, body="changed")
    env.sender = MailSender(**env.options)
    answer(env, request_id)
    assert not env.gmail.calls
    assert "değiş" in env.notifier.messages[-1]


@pytest.mark.parametrize("interrupted", [False, True])
def test_restart_does_not_resend(env, interrupted):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    env.approvals.answer(request_id, True, "web")
    if interrupted:
        env.outbox.begin_send(OWNER, item.id, request_id)
        env.approvals.consume(OWNER, request_id, "mail.send", payload=payload(item))
    else:
        env.sender.handle_answer(OWNER, request_id, True)
    env.sender = MailSender(**env.options)
    env.sender.handle_answer(OWNER, request_id, True)
    assert len(env.gmail.calls) == (0 if interrupted else 1)
    assert env.notifier.messages


def test_school_rejection_reopens_without_cancelling(env):
    item = draft(env, identity="school")
    answer(env, env.sender.ask(OWNER, item.id))
    confirmation = env.approvals.pending(OWNER)[0]["request_id"]
    env.sender = MailSender(**env.options)
    answer(env, confirmation, False)
    reopened = env.outbox.get(OWNER, item.id)
    assert reopened.status == "draft" and reopened.approval_id is None
    assert not env.followups.list(OWNER)
    assert not env.gmail.calls
    assert env.notifier.messages


def test_failed_retry_requires_new_approval(env):
    item = draft(env)
    env.gmail.error = GmailError("failure")
    old = env.sender.ask(OWNER, item.id)
    answer(env, old)
    env.outbox.reopen(OWNER, item.id)
    new = env.sender.ask(OWNER, item.id)
    env.gmail.error = None
    env.sender.handle_answer(OWNER, old, True)
    assert len(env.gmail.calls) == 1
    answer(env, new)
    assert len(env.gmail.calls) == 2


def test_rejected_old_request_does_not_cancel_revision(env):
    item = draft(env)
    old = env.sender.ask(OWNER, item.id)
    env.outbox.revise(OWNER, item.id, body="changed")
    answer(env, old, False)
    assert env.outbox.get(OWNER, item.id).status == "draft"


def test_metadata_too_long_is_refused_before_approval(env):
    item = draft(env, subject="x" * TELEGRAM_LIMIT)
    with pytest.raises(ComposeError):
        env.sender.ask(OWNER, item.id)
    assert not env.approvals.pending(OWNER)


def test_invalid_address_is_refused_before_approval(env):
    item = draft(env, to=["invalid"])
    with pytest.raises(ComposeError):
        env.sender.ask(OWNER, item.id)
    assert not env.approvals.pending(OWNER)


def test_missing_gmail_client_marks_failed(env):
    env.sender = MailSender(**{**env.options, "gmail_client": lambda: None})
    item = draft(env)
    answer(env, env.sender.ask(OWNER, item.id))
    assert env.outbox.get(OWNER, item.id).status == "failed"
    assert not env.gmail.calls


def test_thread_key_prefers_gmail_thread_and_normalizes_fallback(env):
    assert thread_key(draft(env, thread_id="reply-thread")) == "reply-thread"
    assert thread_key(draft(env, subject=" Re: FW:  Meeting   Tomorrow ")) == (
        "subj:meeting tomorrow|ece@example.com")


def test_school_handoff_failure_marks_failed_without_secret(env, monkeypatch):
    item = draft(env, identity="school")
    request_id = env.sender.ask(OWNER, item.id)
    original = env.notifier.send
    def fail_link(text):
        if text.startswith("https://"):
            raise RuntimeError("SECRET_TOKEN")
        original(text)
    monkeypatch.setattr(env.notifier, "send", fail_link)
    answer(env, request_id)
    failed = env.outbox.get(OWNER, item.id)
    assert failed.status == "failed"
    assert "SECRET_TOKEN" not in failed.error + "".join(env.notifier.messages)


def test_confirmation_cannot_be_forged(env):
    item = draft(env, identity="school")
    answer(env, env.sender.ask(OWNER, item.id))
    confirmation = env.approvals.pending(OWNER)[0]["request_id"]
    env.sender.handle_answer(OWNER, confirmation, True)
    assert env.outbox.get(OWNER, item.id).status == "handed"
    assert not env.followups.list(OWNER)


def test_followup_failure_does_not_make_sent_mail_retryable(env, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET_TOKEN")
    monkeypatch.setattr(env.followups, "add", fail)
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    answer(env, request_id)
    assert env.outbox.get(OWNER, item.id).status == "sent"
    assert "takip" in env.notifier.messages[-1]
    assert "SECRET_TOKEN" not in env.notifier.messages[-1]
    env.sender.handle_answer(OWNER, request_id, True)
    assert len(env.gmail.calls) == 1


@pytest.mark.parametrize('committed', [False, True])
def test_successful_send_cannot_be_reopened_after_recording_failure(env, monkeypatch, committed):
    from igris.outbox import OutboxError
    original = env.outbox.mark_sent
    def fail(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise RuntimeError('SECRET_TOKEN BODY_MARKER')
    monkeypatch.setattr(env.outbox, 'mark_sent', fail)
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    answer(env, request_id)
    assert env.outbox.get(OWNER, item.id).status in {'sending', 'sent'}
    with pytest.raises(OutboxError):
        env.outbox.reopen(OWNER, item.id)
    env.sender = MailSender(**env.options)
    env.sender.handle_answer(OWNER, request_id, True)
    with pytest.raises(OutboxError):
        env.sender.ask(OWNER, item.id)
    assert len(env.gmail.calls) == 1
    assert 'SECRET_TOKEN' not in ''.join(env.notifier.messages)





def test_school_reply_timer_does_not_use_forward_gmail_thread(env):
    item = draft(env, identity='school', to=['Ece@Example.com'], subject='FW: Meeting', thread_id='forward-thread')
    assert thread_key(item) == 'subj:meeting|ece@example.com'


def test_uncertain_send_locks_draft_and_warns(env):
    item = draft(env, body="BODY_MARKER")
    request_id = env.sender.ask(OWNER, item.id)
    env.gmail.error = GmailUncertainError("Gmail API unreachable: TimeoutError")
    answer(env, request_id)
    locked = env.outbox.get(OWNER, item.id)
    assert locked.status == "uncertain"
    assert "Gönderilenler" in env.notifier.messages[-1]
    assert "BODY_MARKER" not in "".join(env.notifier.messages) + locked.error
    with pytest.raises(Exception):
        env.outbox.reopen(OWNER, item.id)
    env.sender.handle_answer(OWNER, request_id, True)
    env.sender.resume(OWNER)
    assert len(env.gmail.calls) == 1
    assert "belirsiz" in env.notifier.messages[-1]
    assert not env.followups.list(OWNER)


def test_resume_sends_answer_that_was_stored_but_not_handled(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    env.approvals.answer(request_id, True, "telegram")  # restart before on_answer
    restarted = MailSender(**env.options)
    restarted.resume(OWNER)
    restarted.resume(OWNER)
    assert len(env.gmail.calls) == 1
    assert env.outbox.get(OWNER, item.id).status == "sent"


def test_resume_applies_stored_rejection(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    env.approvals.answer(request_id, False, "telegram")
    env.sender.resume(OWNER)
    assert env.outbox.get(OWNER, item.id).status == "cancelled"
    assert not env.gmail.calls


def test_resume_leaves_pending_requests_alone(env):
    item = draft(env)
    env.sender.ask(OWNER, item.id)
    env.sender.resume(OWNER)
    assert env.outbox.get(OWNER, item.id).status == "awaiting"
    assert not env.gmail.calls


def test_resume_reopens_expired_approval_once_and_tells_owner(env):
    item = draft(env)
    request_id = env.sender.ask(OWNER, item.id)
    env.approvals.answer(request_id, True, "telegram")
    env.now[0] += 61
    env.sender.resume(OWNER)
    reopened = env.outbox.get(OWNER, item.id)
    assert reopened.status == "draft" and reopened.revision == item.revision + 1
    assert "süresi doldu" in env.notifier.messages[-1]
    count = len(env.notifier.messages)
    env.sender.resume(OWNER)
    assert len(env.notifier.messages) == count
    assert env.sender.handle_answer(OWNER, request_id, True)
    assert not env.gmail.calls


def test_resume_finishes_school_confirmation(env):
    item = draft(env, identity="school")
    answer(env, env.sender.ask(OWNER, item.id))
    confirm_id = env.notifier.questions[-1][1]
    env.approvals.answer(confirm_id, True, "telegram")
    env.sender.resume(OWNER)
    assert env.outbox.get(OWNER, item.id).status == "sent"


def test_resume_survives_a_failing_store(env, monkeypatch):
    monkeypatch.setattr(env.outbox, "open_requests",
                        lambda owner: (_ for _ in ()).throw(RuntimeError("db")))
    env.sender.resume(OWNER)
