from datetime import datetime
import json
import sqlite3

import pytest

from igris import gmail, inbox
from igris.mailtriage import TriageRules

GOOD = ("Authentication-Results: mx.google.com; spf=pass; dkim=pass; dmarc=pass\r\n"
        "From: {sender}\r\nSubject: {subject}\r\nDate: Sat, 07 Nov 2026 10:00:00 +0000\r\n"
        "\r\nbody")
# Display name claims a known domain, address is elsewhere: mailcheck rates it high.
SPOOF = ("Authentication-Results: mx.google.com; spf=pass; dkim=pass; dmarc=pass\r\n"
         "From: \"ziraatbank.com.tr\" <x@evil.example>\r\nSubject: Hesabınız\r\n\r\nbody")


def mail(sender="A <a@example.com>", subject="Merhaba"):
    return GOOD.format(sender=sender, subject=subject).encode()


class FakeClient:
    def __init__(self, mails=None, error=None):
        self.mails = dict(mails or {})
        self.error = error
        self.queries = []
        self.limits = []
        self.fetched = []

    def list_ids(self, query="", max_results=10):
        self.queries.append(query)
        self.limits.append(max_results)
        if self.error:
            raise self.error
        return list(self.mails)[:max_results]  # newest first, like Gmail

    def raw(self, message_id):
        self.fetched.append(message_id)
        return self.mails[message_id]


class Clock:
    def __init__(self, at="2026-11-07 10:00"):
        self.now = datetime.strptime(at, "%Y-%m-%d %H:%M")

    def __call__(self):
        return self.now


RULES = TriageRules(vip_domains=("example.com",), important_words=("fatura",),
                    junk_words=("kampanya",))


def watcher(tmp_path, client, sent, clock=None, rules=RULES, **kw):
    return inbox.InboxWatcher(
        lambda: client, inbox.InboxStore(tmp_path / "inbox.sqlite3"), sent.append,
        gmail.AuthAlarm(tmp_path / "gmail_auth_down", sent.append),
        rules=rules, known_domains=("ziraatbank.com.tr",), now=clock or Clock(), digest_hour=19,
        **kw)


def test_first_poll_records_a_baseline_without_messages(tmp_path):
    sent = []
    client = FakeClient({"m1": SPOOF.encode(), "m2": mail()})
    w = watcher(tmp_path, client, sent)
    assert w.poll() == 2
    assert sent == []
    rows = {r["id"]: r for r in w.store.recent(10)}
    assert rows["m1"]["risk"] == "high" and rows["m2"]["label"] == "important"


def test_new_high_risk_mail_is_sent_at_once(tmp_path):
    sent = []
    client = FakeClient({"m0": mail()})
    w = watcher(tmp_path, client, sent)
    w.poll()
    client.mails = {"m1": SPOOF.encode(), "m0": mail()}
    assert w.poll() == 1
    assert client.fetched == ["m0", "m1"]  # known ids are not fetched again
    assert len(sent) == 1 and "evil.example" in sent[0] and "Hesabınız" in sent[0]
    w.poll()
    assert len(sent) == 1


def test_query_asks_for_all_recently_received_mail_not_only_the_inbox(tmp_path):
    client = FakeClient()
    watcher(tmp_path, client, []).poll()
    assert client.queries == ["newer_than:2d -in:sent -in:drafts -in:chats"]
    assert "in:inbox" not in inbox.QUERY
    assert all(f"-in:{box}" in inbox.QUERY for box in ("sent", "drafts", "chats"))


def test_digest_lists_important_mail_once_a_day(tmp_path):
    sent = []
    clock = Clock("2026-11-07 10:00")
    client = FakeClient({"m0": mail(subject="eski")})
    w = watcher(tmp_path, client, sent, clock, digest=True)
    w.poll()
    client.mails = {"m1": mail(subject="Faturanız hazır"),
                    "m2": mail(sender="B <b@other.example>", subject="Kampanya"),
                    "m0": mail(subject="eski")}
    w.poll()

    def digests():  # "Faturanız hazır" also comes as a possible task; only digests count here
        return [t for t in sent if t.startswith("📬")]

    assert digests() == []
    clock.now = datetime(2026, 11, 7, 18, 59)
    w.poll()
    assert digests() == []
    clock.now = datetime(2026, 11, 7, 19, 1)
    w.poll()
    assert len(digests()) == 1
    text = digests()[0]
    assert "Faturanız hazır" in text and "Kampanya" not in text and "eski" not in text
    clock.now = datetime(2026, 11, 7, 22, 0)
    w.poll()
    assert len(digests()) == 1


def test_empty_digest_sends_nothing_but_counts_the_day(tmp_path):
    sent = []
    clock = Clock("2026-11-07 19:30")
    client = FakeClient({"m0": mail(sender="B <b@other.example>")})
    w = watcher(tmp_path, client, sent, clock)
    w.poll()
    w.poll()
    assert sent == []


def test_dead_sign_in_sends_one_alarm_and_stops_polling(tmp_path):
    sent = []
    client = FakeClient(error=gmail.GmailAuthError("invalid_grant"))
    w = watcher(tmp_path, client, sent)
    assert w.poll() == 0
    assert w.poll() == 0
    assert sent == [gmail.ALARM_TEXT]
    assert len(client.queries) == 1  # stopped until the owner signs in again
    assert w.status()["state"] == "auth_down"


def test_new_sign_in_resumes_polling(tmp_path):
    sent = []
    client = FakeClient(error=gmail.GmailAuthError("invalid_grant"))
    w = watcher(tmp_path, client, sent)
    w.poll()
    w.alarm.reset()  # what tools/gmail_auth.py login does
    client.error = None
    w.poll()
    assert len(client.queries) == 2 and w.status()["state"] == "ok"


def test_temporary_error_is_reported_in_status_not_sent(tmp_path):
    sent = []
    w = watcher(tmp_path, FakeClient(error=gmail.GmailError("HTTP 503")), sent)
    assert w.poll() == 0
    assert sent == [] and w.status() ["state"] == "error"
    assert "503" in w.status()["error"]


def test_failed_notification_does_not_lose_the_mail(tmp_path):
    calls = []

    def flaky(text):
        calls.append(text)
        if len(calls) == 1:
            raise OSError("offline")

    client = FakeClient()
    w = inbox.InboxWatcher(lambda: client, inbox.InboxStore(tmp_path / "i.sqlite3"), flaky,
                           gmail.AuthAlarm(tmp_path / "flag", flaky), rules=RULES,
                           known_domains=("ziraatbank.com.tr",), now=Clock(), digest_hour=19)
    w.poll()
    client.mails = {"m1": SPOOF.encode()}
    w.poll()
    w.poll()
    assert len(calls) == 2 and calls[0] == calls[1]


def test_without_notifier_mail_is_still_recorded(tmp_path):
    client = FakeClient()
    w = inbox.InboxWatcher(lambda: client, inbox.InboxStore(tmp_path / "i.sqlite3"), None,
                           gmail.AuthAlarm(tmp_path / "flag", lambda t: None), rules=RULES,
                           now=Clock(), digest_hour=19)
    w.poll()
    client.mails = {"m1": SPOOF.encode()}
    assert w.poll() == 1


def test_missing_client_means_not_configured(tmp_path):
    w = inbox.InboxWatcher(lambda: None, inbox.InboxStore(tmp_path / "i.sqlite3"), None,
                           gmail.AuthAlarm(tmp_path / "flag", lambda t: None), now=Clock())
    assert w.poll() == 0 and w.status()["state"] == "not_configured"


def test_recent_returns_newest_first_with_public_fields(tmp_path):
    clock = Clock()
    client = FakeClient({"m1": mail(subject="bir")})
    w = watcher(tmp_path, client, [], clock)
    w.poll()
    clock.now = datetime(2026, 11, 7, 11, 0)
    client.mails = {"m2": mail(subject="iki"), "m1": mail(subject="bir")}
    w.poll()
    rows = w.store.recent(10)
    assert [r["subject"] for r in rows] == ["iki", "bir"]
    assert set(rows[0]) == {"id", "seen_at", "sender", "subject", "label", "risk", "flags",
                            "spf", "dkim", "dmarc", "category", "reason", "task", "origin"}


def test_long_and_odd_header_values_are_trimmed(tmp_path):
    client = FakeClient()
    w = watcher(tmp_path, client, [])
    w.poll()
    client.mails = {"m1": mail(subject="x" * 2000 + "\x1b[31m")}
    w.poll()
    subject = w.store.recent(1)[0]["subject"]
    assert len(subject) <= 200 and "\x1b" not in subject


def test_store_keeps_at_most_the_newest_rows(tmp_path):
    store = inbox.InboxStore(tmp_path / "i.sqlite3", keep=3)
    for n in range(5):  # a month apart: only rows beyond any query window may go
        store.add(stored(f"m{n}", f"2026-0{n + 1}-07T10:00:00"), notify=False)
    assert len(store.recent(10)) == 5  # adding never prunes
    store.prune("2026-05-07T10:00:00")
    assert [r["id"] for r in store.recent(10)] == ["m4", "m3", "m2"]


def stored(message_id, seen_at, **kw):
    return dict({"id": message_id, "seen_at": seen_at, "sender": "", "subject": "",
                 "label": "routine", "risk": "low", "flags": "", "spf": "", "dkim": "",
                 "dmarc": ""}, **kw)


def test_pruning_never_drops_an_undelivered_alert_or_a_reread_mark(tmp_path):
    store = inbox.InboxStore(tmp_path / "i.sqlite3", keep=1)
    store.add(stored("alert", "2026-08-01T10:00:00", alert=True), notify=True)
    store.add(stored("reread", "2026-08-02T10:00:00", reread=3), notify=True)
    store.add(stored("plain", "2026-08-03T10:00:00"), notify=True)
    store.add(stored("new", "2026-11-07T10:00:00"), notify=True)
    store.prune("2026-11-07T10:00:00")
    assert {r["id"] for r in store.recent(10)} == {"alert", "reread", "new"}
    assert [r["id"] for r in store.pending_alerts()] == ["alert"]


def test_pruning_never_drops_a_row_a_query_window_can_still_list(tmp_path):
    store = inbox.InboxStore(tmp_path / "i.sqlite3", keep=2)
    for n in range(5):  # the reported case (2001 rows against keep=2000) in small
        store.add(stored(f"m{n}", f"2026-10-{10 + n}T10:00:00"), notify=True)
    store.prune("2026-11-07T10:00:00")  # 28 days after the oldest: inside the 30-day window
    assert len(store.recent(10)) == 5
    store.prune("not a time")  # no usable clock keeps everything
    assert len(store.recent(10)) == 5


def test_the_watcher_prunes_once_per_poll(tmp_path, monkeypatch):
    client = FakeClient({f"m{i}": mail(subject=f"Selam {i}") for i in range(3)})
    store = inbox.InboxStore(tmp_path / "i.sqlite3", keep=1)
    store.add(stored("old", "2026-08-01T10:00:00"), notify=False)
    calls = []
    real = store.prune
    monkeypatch.setattr(store, "prune", lambda now: (calls.append(now), real(now)))
    w = inbox.InboxWatcher(lambda: client, store, None,
                           gmail.AuthAlarm(tmp_path / "flag", lambda t: None), now=Clock())
    w.poll()
    assert calls == ["2026-11-07T10:00:00"]
    assert {r["id"] for r in store.recent(10)} == {"m0", "m1", "m2"}


def test_a_listing_longer_than_keep_is_not_read_again_on_the_next_poll(tmp_path):
    sent, client = [], FakeClient({f"m{i}": mail(subject=f"Kira {i}") for i in range(4)})
    w = inbox.InboxWatcher(lambda: client, inbox.InboxStore(tmp_path / "i.sqlite3", keep=2),
                           sent.append, gmail.AuthAlarm(tmp_path / "flag", sent.append),
                           now=Clock())
    w.poll()  # the baseline records four mails
    fetched = len(client.fetched)
    w.poll()
    w.poll()
    assert len(client.fetched) == fetched == 4 and sent == []


def test_load_rules_file(tmp_path):
    path = tmp_path / "mail_rules.json"
    assert inbox.load_rules_file(path) == TriageRules()
    path.write_text('{"vip_domains": ["firma.com"]}', encoding="utf-8")
    assert inbox.load_rules_file(path).vip_domains == ("firma.com",)
    path.write_text('{"nope": []}', encoding="utf-8")
    with pytest.raises(ValueError):
        inbox.load_rules_file(path)


LINK = ("Authentication-Results: mx.google.com; spf=pass; dkim=pass; dmarc={dmarc}\r\n"
        "From: News <news@shop.example>\r\nSubject: Yeni ürünler\r\n"
        "Content-Type: text/html\r\n\r\n"
        "<a href=\"https://track.mailer.example/c/1\">https://shop.example/yeni</a>")


def test_link_mismatch_alone_with_dmarc_pass_is_only_medium(tmp_path):
    sent = []
    client = FakeClient()
    w = watcher(tmp_path, client, sent)
    w.poll()
    client.mails = {"m1": LINK.format(dmarc="pass").encode()}
    w.poll()
    row = w.store.recent(1)[0]
    assert row["flags"] == "link_mismatch" and row["risk"] == "medium" and sent == []


def test_link_mismatch_without_dmarc_pass_stays_high(tmp_path):
    sent = []
    client = FakeClient()
    w = watcher(tmp_path, client, sent)
    w.poll()
    client.mails = {"m1": LINK.format(dmarc="none").encode()}
    w.poll()
    assert w.store.recent(1)[0]["risk"] == "high" and len(sent) == 1


def test_without_notices_or_owner_the_watcher_writes_none(tmp_path):
    from igris.notices import NoticeStore

    notices = NoticeStore(tmp_path / "n.sqlite3")
    client = FakeClient({"m0": mail()})
    w = watcher(tmp_path, client, [])
    w.notices, w.notice_owner = notices, lambda: None
    w.poll()
    client.mails["m1"] = SPOOF.encode()
    assert w.poll() == 1
    assert notices.list("owner-id") == []


# ---------- the mail path fails loudly ----------

RENT_LINE = "Kasım ayı kirası 500 EUR, lütfen 15 Kasım 2026 tarihine kadar hesabıma yatır."
RENT = ("Authentication-Results: mx.google.com; spf=pass; dkim=pass; dmarc=pass\r\n"
        "From: Ali <ali@landlord.example>\r\nSubject: Kira ödemesi\r\n"
        "Date: Sat, 07 Nov 2026 10:00:00 +0000\r\n\r\nMerhaba,\r\n" + RENT_LINE + "\r\n").encode()
RENT_ANSWER = {
    "category": "gorev", "reason": "Ev sahibi Kasım kirasını istiyor.",
    "task": {"title": "Kasım kirasını öde", "kind": "payment", "due": "2026-11-15",
             "start": None, "branch": "kisisel", "mandatory": True, "confidence": 0.9},
    "evidence": RENT_LINE,
}
QUIET_ANSWER = {"category": "bilgi", "reason": "Selamlaşma.", "task": None, "evidence": ""}


class Model:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def __call__(self, system, prompt):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return json.dumps(self.answer)


def loud(tmp_path, client, sent, *, model=None, send="list", clock=None, owner="owner"):
    from igris.agenda import AgendaStore
    from igris.notices import NoticeStore

    sender = sent.append if send == "list" else send
    return inbox.InboxWatcher(
        lambda: client, inbox.InboxStore(tmp_path / "inbox.sqlite3"), sender,
        gmail.AuthAlarm(tmp_path / "gmail_auth_down", lambda text: None),
        now=clock or Clock("2026-11-09 20:00"), model=model,
        notices=NoticeStore(tmp_path / "n.sqlite3"), agenda=AgendaStore(tmp_path / "a.sqlite3"),
        notice_owner=owner)


def with_baseline(w, client, mails):
    w.poll()  # the baseline records the empty inbox
    client.mails.update(mails)
    return w.poll()


def test_rent_mail_with_a_failing_model_still_reaches_the_phone_and_waits_for_a_reread(tmp_path):
    sent, client = [], FakeClient()
    w = loud(tmp_path, client, sent, model=Model(RuntimeError("usage limit")))
    assert with_baseline(w, client, {"rent": RENT}) == 1
    row = w.store.recent(1)[0]
    assert row["category"] == "gorev" and "model okuyamadı" in row["reason"]
    assert len(sent) == 1 and sent[0].startswith("📌 Görev olabilir, model okuyamadı. Kendin bak.")
    assert "Konu: Kira ödemesi" in sent[0] and "Riskli" not in sent[0]
    assert [(r["id"], r["reread"]) for r in w.store.rereads()] == [("rent", 3)]
    assert [n["kind"] for n in w.notices.list("owner")] == ["mail_important"]


def test_rent_mail_without_a_model_reaches_the_phone_and_is_not_reread(tmp_path):
    sent, client = [], FakeClient()
    w = loud(tmp_path, client, sent)
    with_baseline(w, client, {"rent": RENT})
    assert len(sent) == 1 and "Görev olabilir" in sent[0]
    assert w.store.rereads() == []
    w.poll()
    assert len(sent) == 1 and w.status()["model"] == "none"


def test_a_successful_reread_updates_the_row_and_sends_the_task_alert_once(tmp_path):
    sent, client, model = [], FakeClient(), Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, sent, model=model)
    with_baseline(w, client, {"rent": RENT})
    model.answer = RENT_ANSWER
    assert w.poll() == 0  # nothing new; the waiting mail is read again
    row = w.store.recent(1)[0]
    assert row["task"]["kind"] == "payment" and row["reason"] == "Ev sahibi Kasım kirasını istiyor."
    assert w.store.rereads() == []
    [item] = w.agenda.list("owner")
    assert item["status"] == "added" and item["source_ref"] == "rent"
    assert len(sent) == 2 and sent[1].startswith("📌 Yapman gereken bir iş var.")
    assert "15.11.2026" in sent[1]
    assert {n["ref"] for n in w.notices.list("owner")} == {"rent", "rent:reread"}
    w.poll()
    assert len(sent) == 2 and model.calls == 2


def test_a_reread_that_finds_no_sure_task_sends_nothing_more(tmp_path):
    sent, client, model = [], FakeClient(), Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, sent, model=model)
    with_baseline(w, client, {"rent": RENT})
    model.answer = dict(RENT_ANSWER, task=dict(RENT_ANSWER["task"], confidence=0.5))
    w.poll()
    assert len(sent) == 1 and w.store.rereads() == []
    assert w.store.recent(1)[0]["task"]["confidence"] == 0.5


def test_a_reread_while_the_first_alert_still_waits_sends_only_the_better_one(tmp_path):
    flaky = {"down": True}
    sent = []

    def send(text):
        if flaky["down"]:
            raise OSError("offline")
        sent.append(text)

    client, model = FakeClient(), Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, sent, model=model, send=send)
    with_baseline(w, client, {"rent": RENT})
    assert w.status()["channel"] == "failing" and w.status()["pending_alerts"] == 1
    model.answer, flaky["down"] = RENT_ANSWER, False
    w.poll()
    assert len(sent) == 1 and sent[0].startswith("📌 Yapman gereken")
    assert w.status()["channel"] == "ok" and w.status()["pending_alerts"] == 0


def test_a_reread_that_keeps_failing_stops_after_three_attempts(tmp_path):
    sent, client, model = [], FakeClient(), Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, sent, model=model)
    with_baseline(w, client, {"rent": RENT})
    for _ in range(5):
        w.poll()
    assert model.calls == 4  # the first read and three re-reads
    assert w.store.rereads() == []
    row = w.store.recent(1)[0]
    assert row["category"] == "gorev" and row["task"] is None
    assert sum(t.startswith("📌 Görev olabilir") for t in sent) == 1


def test_a_gone_mail_is_not_reread_for_ever(tmp_path):
    sent, client, model = [], FakeClient(), Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, sent, model=model)
    with_baseline(w, client, {"rent": RENT})
    del client.mails["rent"]
    model.answer = RENT_ANSWER
    for _ in range(4):
        w.poll()
    assert w.store.rereads() == [] and model.calls == 1
    assert w.status()["state"] == "ok"


def test_three_failed_model_reads_tell_the_owner_once_per_outage(tmp_path):
    sent, client, clock = [], FakeClient(), Clock("2026-11-09 20:00")
    model = Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, sent, model=model, clock=clock)
    assert w.status()["model"] == "ok"
    with_baseline(w, client, {"a": mail(subject="Merhaba"), "b": mail(subject="Selam")})
    assert sent == [] and w.status()["model"] == "ok"  # two failures are not an outage yet
    client.mails["c"] = mail(subject="Nasılsın")
    w.poll()
    down = [t for t in sent if t.startswith("Igris mailleri okuyamıyor")]
    assert down == [inbox.MODEL_DOWN_TEXT] and w.status()["model"] == "down"
    for _ in range(3):
        w.poll()
    assert sent.count(inbox.MODEL_DOWN_TEXT) == 1
    assert [n["kind"] for n in w.notices.list("owner")].count("models") == 1
    # recovery: the waiting re-reads succeed and end the outage
    model.answer = QUIET_ANSWER
    clock.now = datetime(2026, 11, 9, 21, 0)
    client.mails["d"] = mail(subject="Günaydın")
    w.poll()
    assert w.status()["model"] == "ok"
    # a later outage alarms again
    model.answer = RuntimeError("usage limit")
    clock.now = datetime(2026, 11, 10, 9, 0)
    client.mails.update({f"x{i}": mail(subject=f"Selam {i}") for i in range(3)})
    w.poll()
    assert sent.count(inbox.MODEL_DOWN_TEXT) == 2
    assert [n["kind"] for n in w.notices.list("owner")].count("models") == 2


def test_three_failed_polls_tell_the_owner_once(tmp_path):
    sent, client = [], FakeClient()
    w = loud(tmp_path, client, sent)
    w.poll()
    client.error = gmail.GmailError("Gmail API returned HTTP 503")
    for _ in range(2):
        w.poll()
    assert sent == []
    for _ in range(3):
        w.poll()
    assert sent == ["Igris Gmail'i okuyamıyor: Gmail API returned HTTP 503"]
    assert [n["kind"] for n in w.notices.list("owner")] == ["connection"]
    client.error = None
    w.poll()
    assert w.status()["state"] == "ok"
    client.error = gmail.GmailError("Gmail API unreachable: URLError")
    w._now = lambda: datetime(2026, 11, 10, 9, 0)
    for _ in range(3):
        w.poll()
    assert len(sent) == 2 and sent[1].endswith("Gmail API unreachable: URLError")


def test_an_unexpected_error_inside_the_fetch_does_not_escape_poll(tmp_path):
    sent, client = [], FakeClient()
    w = loud(tmp_path, client, sent)
    w.poll()
    client.error = KeyError("boom")
    for _ in range(3):
        assert w.poll() == 0
    assert w.status()["state"] == "error" and "KeyError" in w.status()["error"]
    assert sent == ["Igris Gmail'i okuyamıyor: KeyError"]


def test_a_factory_that_raises_does_not_escape_poll(tmp_path):
    def factory():
        raise OSError("keyring locked")

    w = inbox.InboxWatcher(factory, inbox.InboxStore(tmp_path / "i.sqlite3"), None,
                           gmail.AuthAlarm(tmp_path / "flag", lambda t: None), now=Clock())
    assert w.poll() == 0 and w.status()["state"] == "error"


def test_without_a_channel_system_problems_still_become_notices(tmp_path):
    client = FakeClient()
    w = loud(tmp_path, client, [], send=None)
    w.poll()
    client.error = gmail.GmailError("HTTP 503")
    for _ in range(3):
        w.poll()
    assert [n["kind"] for n in w.notices.list("owner")] == ["connection"]


def test_status_reports_the_channel_pending_alerts_and_the_model(tmp_path):
    client = FakeClient()
    none = loud(tmp_path / "none", client, [], send=None)
    with_baseline(none, client, {"rent": RENT})
    status = none.status()
    assert status["channel"] == "none" and status["pending_alerts"] == 1
    assert status["model"] == "none" and status["state"] == "ok"
    assert {"state", "last_poll", "error"} <= set(status)

    def broken(text):
        raise OSError("offline")

    client = FakeClient()
    failing = loud(tmp_path / "failing", client, [], send=broken, model=Model(RENT_ANSWER))
    with_baseline(failing, client, {"rent": RENT})
    assert failing.status()["channel"] == "failing" and failing.status()["pending_alerts"] == 1
    assert failing.status()["model"] == "ok"

    client, sent = FakeClient(), []
    ok = loud(tmp_path / "ok", client, sent)
    with_baseline(ok, client, {"rent": RENT})
    assert ok.status()["channel"] == "ok" and ok.status()["pending_alerts"] == 0


@pytest.mark.parametrize("ago, query, limit", [
    ({"hours": 1}, "newer_than:2d -in:sent -in:drafts -in:chats", 50),
    ({"days": 1, "hours": 2}, "newer_than:3d -in:sent -in:drafts -in:chats", 5000),
    ({"days": 5}, "newer_than:6d -in:sent -in:drafts -in:chats", 5000),
    ({"days": 90}, "newer_than:30d -in:sent -in:drafts -in:chats", 5000),
])
def test_the_query_window_reaches_back_to_the_last_complete_poll(tmp_path, ago, query, limit):
    from datetime import timedelta

    clock, client = Clock("2026-11-09 20:00"), FakeClient()
    w = loud(tmp_path, client, [], clock=clock)
    w.poll()  # the baseline keeps today's query
    assert client.queries == [inbox.QUERY] and client.limits == [50]
    clock.now += timedelta(**ago)
    w.poll()
    assert client.queries[-1] == query and client.limits[-1] == limit
    w.poll()  # the poll just now was complete: back to two days
    assert client.queries[-1] == "newer_than:2d -in:sent -in:drafts -in:chats" and client.limits[-1] == 50


def test_a_catch_up_cut_by_the_model_budget_keeps_its_window(tmp_path, monkeypatch):
    from datetime import timedelta

    monkeypatch.setattr(inbox, "MAX_MODEL_PER_POLL", 1)
    clock, client = Clock("2026-11-09 20:00"), FakeClient()
    w = loud(tmp_path, client, [], clock=clock, model=Model(QUIET_ANSWER))
    w.poll()
    clock.now += timedelta(days=5)
    client.mails.update({"a": mail(subject="Merhaba"), "b": mail(subject="Selam")})
    assert w.poll() == 1
    clock.now += timedelta(minutes=5)
    assert w.poll() == 1 and client.queries[-1] == "newer_than:7d -in:sent -in:drafts -in:chats"  # same start
    w.poll()
    assert client.queries[-1] == "newer_than:2d -in:sent -in:drafts -in:chats"


def test_a_store_from_before_the_reread_column_opens_and_works(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE mails (
        id TEXT PRIMARY KEY, seen_at TEXT NOT NULL, sender TEXT, subject TEXT,
        label TEXT, risk TEXT, flags TEXT, spf TEXT, dkim TEXT, dmarc TEXT,
        pending_alert INTEGER NOT NULL DEFAULT 0, digested INTEGER NOT NULL DEFAULT 1,
        category TEXT, reason TEXT, task TEXT, origin TEXT)""")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('baseline', '2026-11-01T10:00:00')")
    db.execute("INSERT INTO mails (id, seen_at, sender, subject, label, risk, flags, spf, dkim, "
               "dmarc, category, reason) VALUES ('old', '2026-11-01T10:00:00', 'a', 'b', "
               "'routine', 'low', '', 'pass', 'pass', 'pass', 'bilgi', 'x')")
    db.commit()
    db.close()
    sent, client = [], FakeClient({"old": mail()})
    w = loud(tmp_path, client, sent, model=Model(RuntimeError("down")))
    client.mails["rent"] = RENT
    assert w.poll() == 1
    assert [r["id"] for r in w.store.rereads()] == ["rent"]
    assert {r["id"] for r in w.store.recent(10)} == {"old", "rent"} and len(sent) == 1


# ---------- review fixes: nothing counts as told before it is delivered ----------

class Channel:
    """A notification channel that can be switched off; records what it delivered."""

    def __init__(self, up=True):
        self.up, self.sent = up, []

    def __call__(self, text):
        if not self.up:
            raise OSError("offline")
        self.sent.append(text)


def test_a_quiet_reread_never_cancels_an_alert_that_was_not_delivered(tmp_path):
    channel, client = Channel(up=False), FakeClient()
    model = Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, [], model=model, send=channel)
    with_baseline(w, client, {"rent": RENT})
    assert w.status()["pending_alerts"] == 1
    model.answer = dict(RENT_ANSWER, task=dict(RENT_ANSWER["task"], confidence=0.5))
    w.poll()  # the re-read is unsure: the waiting generic alert stays as it is
    assert w.store.rereads() == [] and w.status()["pending_alerts"] == 1
    assert w.store.recent(1)[0]["reason"].startswith("Görev olabilir")
    channel.up = True
    w.poll()
    w.poll()
    assert len(channel.sent) == 1 and channel.sent[0].startswith("📌 Görev olabilir, model okuyamadı.")


def test_a_full_listing_keeps_the_window_and_is_read_oldest_first(tmp_path, monkeypatch):
    from datetime import timedelta

    monkeypatch.setattr(inbox, "MAX_CATCH_UP", 3)
    monkeypatch.setattr(inbox, "MAX_MODEL_PER_POLL", 1)
    clock, client = Clock("2026-11-09 20:00"), FakeClient()
    model = Model(QUIET_ANSWER)
    w = loud(tmp_path, client, [], clock=clock, model=model)
    w.poll()
    start = w.store.get_meta("last_ok_poll")
    clock.now += timedelta(days=5)
    client.mails.update({f"n{i}": mail(subject=f"Selam {i}") for i in (4, 3, 2, 1, 0)})
    w.poll()  # the listing filled its limit (n4, n3, n2): older mail may hide behind it
    assert [r["id"] for r in w.store.recent(10)] == ["n2"]  # the oldest listed comes first
    for _ in range(2):
        clock.now += timedelta(minutes=5)
        w.poll()
    assert {r["id"] for r in w.store.recent(10)} == {"n2", "n3", "n4"}
    assert w.store.get_meta("last_ok_poll") == start  # never advanced over a full listing
    assert client.queries[-1] == "newer_than:7d -in:sent -in:drafts -in:chats"
    # n1 and n0 are beyond the ceiling: the owner is warned once, in status and the panel
    assert w.status()["backlog_truncated"] is True
    assert [n["title"] for n in w.notices.list("owner")] == ["Eski mailler okunamadı"]
    clock.now += timedelta(minutes=5)
    w.poll()  # every listed mail is known now: the catch-up is over and the window may shrink
    assert w.store.get_meta("last_ok_poll") == clock.now.isoformat(timespec="seconds")
    clock.now += timedelta(minutes=5)
    w.poll()
    assert client.queries[-1] == "newer_than:2d -in:sent -in:drafts -in:chats" and client.limits[-1] == 50
    assert w.status()["backlog_truncated"] is False  # a clean poll ends the warning
    assert len(w.notices.list("owner")) == 1


def test_a_model_down_message_the_channel_missed_is_sent_once_it_works(tmp_path):
    channel, client = Channel(up=False), FakeClient()
    w = loud(tmp_path, client, [], model=Model(RuntimeError("usage limit")), send=channel)
    with_baseline(w, client, {f"m{i}": mail(subject=f"Selam {i}") for i in range(3)})
    assert channel.sent == [] and w.status()["model"] == "down"
    assert [n["kind"] for n in w.notices.list("owner")] == ["models"]  # the notice at once
    w.poll()
    channel.up = True
    w.poll()
    w.poll()
    assert channel.sent == [inbox.MODEL_DOWN_TEXT]
    assert [n["kind"] for n in w.notices.list("owner")] == ["models"]


def test_a_model_down_message_survives_a_restart_until_delivered(tmp_path):
    channel, client = Channel(up=False), FakeClient()
    w = loud(tmp_path, client, [], model=Model(RuntimeError("usage limit")), send=channel)
    with_baseline(w, client, {f"m{i}": mail(subject=f"Selam {i}") for i in range(3)})
    channel.up = True
    again = loud(tmp_path, client, [], model=Model(RuntimeError("usage limit")), send=channel)
    again.poll()
    assert channel.sent == [inbox.MODEL_DOWN_TEXT]


def test_an_outage_that_ends_before_delivery_sends_no_stale_message(tmp_path):
    channel, client = Channel(up=False), FakeClient()
    model = Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, [], model=model, send=channel)
    with_baseline(w, client, {f"m{i}": mail(subject=f"Selam {i}") for i in range(3)})
    model.answer = QUIET_ANSWER
    w.poll()  # the re-reads work: the outage is over while the channel is still down
    channel.up = True
    w.poll()
    assert inbox.MODEL_DOWN_TEXT not in channel.sent and w.status()["model"] == "ok"
    # the same for Gmail
    client.error = gmail.GmailError("HTTP 503")
    channel.up = False
    for _ in range(3):
        w.poll()
    client.error, channel.up = None, True
    w.poll()
    assert not any(t.startswith("Igris Gmail'i") for t in channel.sent)


def test_a_gmail_down_message_is_retried_while_gmail_stays_down(tmp_path):
    channel, client = Channel(up=False), FakeClient()
    w = loud(tmp_path, client, [], send=channel)
    w.poll()
    client.error = gmail.GmailError("HTTP 503")
    for _ in range(3):
        w.poll()
    assert channel.sent == [] and [n["kind"] for n in w.notices.list("owner")] == ["connection"]
    channel.up = True
    for _ in range(3):
        w.poll()
    assert channel.sent == ["Igris Gmail'i okuyamıyor: HTTP 503"]
    assert [n["kind"] for n in w.notices.list("owner")] == ["connection"]


def test_an_owner_lookup_that_raises_on_the_third_failed_poll_does_not_escape(tmp_path):
    calls = []

    def owner():
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    sent, client = [], FakeClient()
    w = loud(tmp_path, client, sent, owner=owner)
    w.poll()
    client.error = gmail.GmailError("HTTP 503")
    for _ in range(3):
        assert w.poll() == 0
    assert calls and w.status()["state"] == "error"
    assert sent == ["Igris Gmail'i okuyamıyor: HTTP 503"]


def test_an_owner_lookup_that_raises_keeps_mail_and_model_alarms_going(tmp_path):
    def owner():
        raise sqlite3.OperationalError("database is locked")

    sent, client = [], FakeClient()
    w = loud(tmp_path, client, sent, owner=owner, model=Model(RuntimeError("usage limit")))
    with_baseline(w, client, {"rent": RENT, "a": mail(subject="Selam"), "b": mail(subject="Merhaba")})
    assert w.status()["state"] == "ok" and inbox.MODEL_DOWN_TEXT in sent
    assert any(t.startswith("📌 Görev olabilir") for t in sent)


def test_a_poll_whose_failure_report_breaks_still_does_not_raise(tmp_path, monkeypatch):
    sent, client = [], FakeClient()
    w = loud(tmp_path, client, sent, send=lambda text: None)
    w.poll()
    client.error = gmail.GmailError("HTTP 503")
    monkeypatch.setattr(w, "_system_notice", lambda *a, **k: 1 / 0)
    for _ in range(4):
        assert w.poll() == 0
    assert w.status()["state"] == "error"


def test_a_delivered_model_down_message_is_not_repeated_when_its_flag_cannot_be_written(
        tmp_path, monkeypatch):
    sent, client = [], FakeClient()
    w = loud(tmp_path, client, sent, model=Model(RuntimeError("usage limit")))
    real = w.store.set_meta

    def set_meta(key, value):
        if key == "model_down_told" and value == "1":
            raise sqlite3.OperationalError("disk I/O error")
        real(key, value)

    monkeypatch.setattr(w.store, "set_meta", set_meta)
    with_baseline(w, client, {f"m{i}": mail(subject=f"Selam {i}") for i in range(3)})
    for _ in range(3):
        w.poll()
    assert sent.count(inbox.MODEL_DOWN_TEXT) == 1 and w.status()["state"] == "ok"


# ---------- mail gaps: doubted model answers, archived mail, the body of tab mail ----------

BULK_RENT = RENT.replace(b"Subject: Kira", b"List-Unsubscribe: <https://list.example/u>\r\n"
                                           b"Subject: Kira")


class LabelClient(FakeClient):
    """A client that also returns Gmail labels, like the real one."""

    def __init__(self, mails=None, labels=None):
        super().__init__(mails)
        self.labels = dict(labels or {})

    def raw_with_labels(self, message_id):
        return self.raw(message_id), tuple(self.labels.get(message_id, ("INBOX",)))


def test_a_doubted_personal_mail_alerts_once_and_writes_one_notice(tmp_path):
    sent, client, model = [], FakeClient(), Model(QUIET_ANSWER)
    w = loud(tmp_path, client, sent, model=model)
    assert with_baseline(w, client, {"rent": RENT}) == 1
    row = w.store.recent(1)[0]
    assert row["category"] == "bilgi" and "'ödeme'" in row["reason"] and "Selamlaşma." in row["reason"]
    assert len(sent) == 1 and sent[0].startswith("📌 Görev olabilir: model emin değil. Kendin bak.")
    assert "Konu: Kira ödemesi" in sent[0] and "okuyamadı" not in sent[0]
    [notice] = w.notices.list("owner")
    assert notice["kind"] == "mail_important" and notice["title"] == "Kontrol et: Kira ödemesi"
    assert w.store.rereads() == [] and w.agenda.list("owner") == []
    w.poll()
    assert len(sent) == 1 and model.calls == 1 and len(w.notices.list("owner")) == 1


def test_a_doubted_bulk_mail_writes_the_notice_and_sends_nothing(tmp_path):
    sent, client = [], FakeClient()
    w = loud(tmp_path, client, sent, model=Model(QUIET_ANSWER))
    with_baseline(w, client, {"rent": BULK_RENT})
    assert sent == [] and w.status()["pending_alerts"] == 0
    assert "telefona bildirilmedi" in w.store.recent(1)[0]["reason"]
    assert [n["title"] for n in w.notices.list("owner")] == ["Kontrol et: Kira ödemesi"]
    assert w.store.rereads() == []


def test_the_baseline_writes_no_doubt_notice_and_asks_no_model(tmp_path):
    sent, client, model = [], FakeClient({"rent": RENT, "bulk": BULK_RENT}), Model(QUIET_ANSWER)
    w = loud(tmp_path, client, sent, model=model)
    assert w.poll() == 2
    assert sent == [] and model.calls == 0 and w.notices.list("owner") == []


def test_a_reread_that_doubts_a_delivered_possible_task_sends_nothing_more(tmp_path):
    sent, client, model = [], FakeClient(), Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, sent, model=model)
    with_baseline(w, client, {"rent": RENT})
    assert len(sent) == 1 and sent[0].startswith("📌 Görev olabilir, model okuyamadı.")
    model.answer = QUIET_ANSWER
    w.poll()
    assert len(sent) == 1 and w.store.rereads() == []
    assert w.store.recent(1)[0]["category"] == "bilgi"
    assert "Kontrol et: Kira ödemesi" in [n["title"] for n in w.notices.list("owner")]


def test_a_promotions_mail_with_a_body_task_word_goes_to_the_model(tmp_path):
    promo = mail(sender="Shop <news@shop.example>", subject="Yeni sezon").replace(
        b"\r\n\r\nbody", b"\r\n\r\nFatura adresini kontrol et.")
    plain = mail(sender="Shop <news@shop.example>", subject="Yeni sezon")
    tab = ("INBOX", "CATEGORY_PROMOTIONS")
    sent, client, model = [], LabelClient(labels={"p": tab, "q": tab}), Model(QUIET_ANSWER)
    w = loud(tmp_path, client, sent, model=model)
    with_baseline(w, client, {"p": promo, "q": plain})
    rows = {r["id"]: r for r in w.store.recent(10)}
    assert model.calls == 1 and rows["p"]["reason"] == "Selamlaşma." and rows["p"]["category"] == "bilgi"
    assert rows["q"]["category"] == "tanitim" and sent == []


def test_a_promotions_mail_with_a_body_task_word_and_a_failing_model_is_quiet_and_reread(tmp_path):
    promo = mail(sender="Shop <news@shop.example>", subject="Yeni sezon").replace(
        b"\r\n\r\nbody", b"\r\n\r\nFatura adresini kontrol et.")
    sent, client = [], LabelClient(labels={"p": ("INBOX", "CATEGORY_PROMOTIONS")})
    w = loud(tmp_path, client, sent, model=Model(RuntimeError("usage limit")))
    with_baseline(w, client, {"p": promo})
    row = w.store.recent(1)[0]
    assert sent == [] and row["category"] == "tanitim"
    assert row["reason"] == "Toplu gönderimde görev kelimesi geçiyor (fatura); model okuyamadı"
    assert [(r["id"], r["reread"]) for r in w.store.rereads()] == [("p", 3)]


def test_a_personal_mail_with_a_body_task_word_and_a_failing_model_still_alerts(tmp_path):
    personal = mail(sender="Ali <ali@friend.example>", subject="Merhaba").replace(
        b"\r\n\r\nbody", b"\r\n\r\nYarinki quiz saat 10:00'da.")
    sent, client = [], LabelClient()
    w = loud(tmp_path, client, sent, model=Model(RuntimeError("usage limit")))
    with_baseline(w, client, {"m": personal})
    assert len(sent) == 1 and sent[0].startswith("📌 Görev olabilir, model okuyamadı.")


# ---------- every first decision x re-read outcome ----------

GEN = "📌 Görev olabilir, model okuyamadı."
TASK = "📌 Yapman gereken bir iş var."
SEC = "🔐 Hesap güvenliği uyarısı."
DOUBT = "📌 Görev olabilir: model emin değil."
SURE = RENT_ANSWER
UNSURE = dict(RENT_ANSWER, task=dict(RENT_ANSWER["task"], confidence=0.5))
SECURITY = {"category": "guvenlik", "reason": "Banka taklidi.", "task": None, "evidence": ""}
PROMO_ANSWER = {"category": "tanitim", "reason": "Reklam.", "task": None, "evidence": ""}
REJECTED = dict(RENT_ANSWER, evidence="Bu cümle mailde yok.")
# subject task word (first: gorev + alert); body word only (first: gorev + alert);
# promotions tab, body word only (first: tanitim, quiet)
BODY_ONLY = RENT.replace(b"Subject: Kira \xc3\xb6demesi", b"Subject: Merhaba")
PROMO_BODY = RENT.replace(b"Subject: Kira \xc3\xb6demesi", b"Subject: Yeni sezon")
FIRST = {"subject": (RENT, ("INBOX",)), "body": (BODY_ONLY, ("INBOX",)),
         "promo": (PROMO_BODY, ("INBOX", "CATEGORY_PROMOTIONS"))}
G_RENT, G_HI = "Görev: Kira ödemesi", "Görev: Merhaba"
G_TASK, CHECK = "Görev: Kasım kirasını öde", "Kontrol et: Kira ödemesi"


@pytest.mark.parametrize("first, delivered, answer, phone, panel", [
    # a possible task was delivered before the re-read
    ("subject", True, SURE, [GEN, TASK], {G_RENT, G_TASK}),
    ("subject", True, UNSURE, [GEN], {G_RENT}),
    ("subject", True, SECURITY, [GEN, SEC], {G_RENT, "Güvenlik: Kira ödemesi"}),
    ("subject", True, QUIET_ANSWER, [GEN], {G_RENT, CHECK}),  # doubt that would notify
    ("subject", True, PROMO_ANSWER, [GEN], {G_RENT, CHECK}),  # quiet doubt
    ("body", True, QUIET_ANSWER, [GEN], {G_HI}),  # plain non-task
    # the possible task still waits for the channel at the re-read
    ("subject", False, SURE, [TASK], {G_RENT, G_TASK}),
    ("subject", False, UNSURE, [GEN], {G_RENT}),
    ("subject", False, SECURITY, [SEC], {G_RENT, "Güvenlik: Kira ödemesi"}),
    ("subject", False, QUIET_ANSWER, [DOUBT], {G_RENT, CHECK}),
    ("subject", False, PROMO_ANSWER, [GEN], {G_RENT, CHECK}),
    ("body", False, QUIET_ANSWER, [GEN], {G_HI}),
    # first filed quietly (bulk mail, body word); a doubt is impossible here (see
    # test_a_subject_that_can_be_doubted_always_alerts_in_the_fallback)
    ("promo", None, SURE, [TASK], {G_TASK}),
    ("promo", None, UNSURE, [], set()),
    ("promo", None, SECURITY, [SEC], {"Güvenlik: Yeni sezon"}),
    ("promo", None, QUIET_ANSWER, [], set()),
    # a task answer whose evidence is not in the mail: doubted, never silent, never twice
    ("subject", True, REJECTED, [GEN], {G_RENT, CHECK}),
    ("subject", False, REJECTED, [DOUBT], {G_RENT, CHECK}),
    ("promo", None, REJECTED, [], {"Kontrol et: Yeni sezon"}),  # bulk: panel only
])
def test_what_a_reread_tells_for_each_first_decision(tmp_path, first, delivered, answer,
                                                    phone, panel):
    raw, labels = FIRST[first]
    channel = Channel(up=delivered is not False)
    client, model = LabelClient(labels={"m": labels}), Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, [], model=model, send=channel)
    with_baseline(w, client, {"m": raw})
    assert [r["id"] for r in w.store.rereads()] == ["m"]
    model.answer, channel.up = answer, True
    for _ in range(3):
        w.poll()
    assert model.calls == 2 and w.store.rereads() == []
    assert [next((p for p in (GEN, TASK, SEC, DOUBT) if t.startswith(p)), t)
            for t in channel.sent] == phone
    titles = [n["title"] for n in w.notices.list("owner")]
    assert sorted(titles) == sorted(panel)  # each notice exactly once


def test_a_task_answer_with_rejected_evidence_alerts_once_as_a_doubt(tmp_path):
    sent, client, model = [], FakeClient(), Model(REJECTED)
    w = loud(tmp_path, client, sent, model=model)
    with_baseline(w, client, {"rent": RENT})
    row = w.store.recent(1)[0]
    assert row["category"] == "gorev" and row["task"] is None
    assert len(sent) == 1 and sent[0].startswith(DOUBT) and "okuyamadı" not in sent[0]
    assert [n["title"] for n in w.notices.list("owner")] == [CHECK]
    assert w.store.rereads() == [] and w.agenda.list("owner") == []
    w.poll()
    assert len(sent) == 1 and model.calls == 1


@pytest.mark.parametrize("attempts", [1, 3])
def test_a_quiet_reread_notice_retries_without_rereading_the_model(tmp_path, monkeypatch,
                                                                 attempts):
    channel, client = Channel(up=False), FakeClient()
    model = Model(RuntimeError("usage limit"))
    w = loud(tmp_path, client, [], model=model, send=channel)
    with_baseline(w, client, {"rent": RENT})
    w.store.set_reread("rent", attempts)

    def broken(**payload):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(w.notices, "add", broken)
    model.answer = PROMO_ANSWER
    w.poll()
    assert w.store.rereads() == []
    [queued] = w.store.pending_notices()
    assert queued["ref"] == "rent:reread" and queued["title"] == CHECK
    # A quiet answer keeps the original phone alert, even when its panel notice fails.
    assert w.store.pending_count() == 1
    model.answer = RuntimeError("usage limit")
    channel.up = True
    again = loud(tmp_path, client, [], model=model, send=channel)
    for _ in range(2):
        again.poll()
    assert model.calls == 2 and again.store.pending_notices() == []
    assert [n["title"] for n in again.notices.list("owner")].count(CHECK) == 1
    assert len(channel.sent) == 1 and channel.sent[0].startswith(GEN)


@pytest.mark.parametrize("reread", [False, True])
@pytest.mark.parametrize("answer", [REJECTED, SURE, SECURITY])
def test_notice_outbox_survives_restart_and_later_model_failure(tmp_path, monkeypatch,
                                                               reread, answer):
    sent = []
    client = LabelClient(labels={"m": FIRST["promo"][1]})
    model = Model(RuntimeError("usage limit") if reread else answer)
    channel = Channel(up=False)
    w = loud(tmp_path, client, sent, model=model, send=channel)
    w.poll()
    client.mails["m"] = FIRST["promo"][0]
    if reread:
        w.poll()
        w.store.set_reread("m", 1)
        model.answer = answer

    def broken(**payload):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(w.notices, "add", broken)
    w.poll()
    assert w.status()["state"] == "ok" and w.store.rereads() == []
    [queued] = w.store.pending_notices()
    assert queued["ref"] == "m" + (":reread" if reread else "")
    assert w.notices.list("owner") == []
    [decision] = w.store.recent(1)
    assert decision["category"] == answer["category"]
    assert bool(decision["task"]) == (answer == SURE)
    calls = model.calls

    # Keep the notice store down for another process, while unrelated new mail
    # really does fail a model read. The successful decision must stay intact.
    model.answer = RuntimeError("usage limit")
    again = loud(tmp_path, client, sent, model=model, send=channel)
    monkeypatch.setattr(again.notices, "add", broken)
    client.mails["other"] = mail(subject="Hello")
    again.poll()
    assert model.calls == calls + 1
    assert next(r for r in again.store.recent() if r["id"] == "m") == decision
    assert again.store.pending_notices() == [queued]

    # Delivery still works while Gmail is unavailable, before any further read.
    final = loud(tmp_path, client, sent, model=model, send=channel)
    client.error = gmail.GmailError("HTTP 503")
    final.poll()
    assert final.store.pending_notices() == [] and model.calls == calls + 1
    [notice] = final.notices.list("owner")
    for key in ("ref", "title", "body", "kind", "action"):
        assert notice[key] == queued[key]
    client.error = None
    channel.up = True
    final.poll()
    final.poll()
    assert len([n for n in final.notices.list("owner") if n["ref"] == queued["ref"]]) == 1
    mail_alerts = [text for text in channel.sent if text != inbox.MODEL_DOWN_TEXT]
    assert len(mail_alerts) == (1 if answer in (SURE, SECURITY) else 0)


def test_a_delivered_notice_with_failed_outbox_cleanup_is_not_duplicated(tmp_path, monkeypatch):
    client = FakeClient()
    w = loud(tmp_path, client, [], model=Model(REJECTED))

    def broken(ref):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(w.store, "mark_noticed", broken)
    with_baseline(w, client, {"rent": RENT})
    assert len(w.notices.list("owner")) == len(w.store.pending_notices()) == 1
    again = loud(tmp_path, client, [], model=Model(RuntimeError("usage limit")))
    again.poll()
    again.poll()
    assert again.store.pending_notices() == [] and len(again.notices.list("owner")) == 1


@pytest.mark.parametrize("reread", [False, True])
@pytest.mark.parametrize("fail_outbox", [False, True])
def test_decision_and_notice_outbox_commit_atomically(tmp_path, reread, fail_outbox):
    client = FakeClient()
    model = Model(RuntimeError("usage limit") if reread else REJECTED)
    w = loud(tmp_path, client, [], model=model)
    w.poll()
    client.mails["rent"] = RENT
    if reread:
        w.poll()
        previous = w.store.recent(1)
        model.answer = REJECTED
    operation = "UPDATE" if reread else "INSERT"
    table = "pending_notices" if fail_outbox else "mails"
    if fail_outbox:
        operation = "INSERT"
    with sqlite3.connect(w.store.path) as db:
        db.execute(f"CREATE TRIGGER fail_decision BEFORE {operation} ON {table} "
                   "BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    w.poll()
    assert w.status()["state"] == "error" and w.store.pending_notices() == []
    if reread:
        assert w.store.recent(1) == previous and w.store.rereads()[0]["reread"] == 3
    else:
        assert w.store.recent() == [] and w.notices.list("owner") == []
    with sqlite3.connect(w.store.path) as db:
        db.execute("DROP TRIGGER fail_decision")
    w.poll()
    assert w.store.rereads() == []
    assert [n["title"] for n in w.notices.list("owner")].count(CHECK) == 1


def test_notice_outbox_retries_owner_lookup_before_gmail_is_configured(tmp_path):
    def unavailable_owner():
        raise sqlite3.OperationalError("database is locked")

    client = FakeClient()
    w = loud(tmp_path, client, [], model=Model(REJECTED), owner=unavailable_owner)
    with_baseline(w, client, {"rent": RENT})
    assert len(w.store.pending_notices()) == 1 and w.notices.list("owner") == []
    again = loud(tmp_path, None, [], model=Model(RuntimeError("usage limit")))
    again.poll()
    assert again.status()["state"] == "not_configured"
    assert again.store.pending_notices() == []
    assert [n["title"] for n in again.notices.list("owner")] == [CHECK]


def test_one_failed_notice_does_not_block_other_notices(tmp_path, monkeypatch):
    client = FakeClient()
    w = loud(tmp_path, client, [], model=Model(REJECTED))
    real = w.notices.add

    def add(**payload):
        if payload["ref"] == "first":
            raise sqlite3.OperationalError("database is locked")
        return real(**payload)

    monkeypatch.setattr(w.notices, "add", add)
    with_baseline(w, client, {"first": RENT, "second": RENT})
    assert [n["ref"] for n in w.notices.list("owner")] == ["second"]
    assert [n["ref"] for n in w.store.pending_notices()] == ["first"]


def test_a_subject_task_word_alerts_from_the_promotions_tab_when_the_model_fails(tmp_path):
    sent, client = [], LabelClient(labels={"rent": ("INBOX", "CATEGORY_PROMOTIONS")})
    w = loud(tmp_path, client, sent, model=Model(RuntimeError("usage limit")))
    with_baseline(w, client, {"rent": BULK_RENT})
    assert len(sent) == 1 and "Konu: Kira ödemesi" in sent[0]


def test_match_followup_by_thread():
    from email import message_from_bytes, policy
    message = message_from_bytes(mail(), policy=policy.default)
    assert inbox.match_followup(message, '', open_keys={'thread'}, school_domains=(), thread_id='thread') == 'thread'


def test_match_followup_by_subject_and_sender():
    from email import message_from_bytes, policy
    message = message_from_bytes(mail(sender='A <A@example.com>', subject='Re: Fwd: Meeting'), policy=policy.default)
    key = 'subj:meeting|a@example.com'
    assert inbox.match_followup(message, '', open_keys={key}, school_domains=()) == key
    assert inbox.match_followup(message, '', open_keys={'unrelated'}, school_domains=()) is None


def test_match_followup_school_forward_uses_original_sender():
    from email import message_from_bytes, policy
    message = message_from_bytes(mail(sender='owner@school.example', subject='FW: Meeting'), policy=policy.default)
    key = 'subj:meeting|ece@example.lt'
    assert inbox.match_followup(message, 'Forwarded message\nFrom: Ece <ece@example.lt>',
                                open_keys={key}, school_domains=('school.example',),
                                owner_addresses=('owner@school.example',)) == key


@pytest.mark.parametrize('labels,addresses', [(('SENT',), ()), ((), ('a@example.com',))])
def test_match_followup_ignores_owner_own_mail(labels, addresses):
    from email import message_from_bytes, policy
    message = message_from_bytes(mail(), policy=policy.default)
    assert inbox.match_followup(message, '', open_keys={'thread'}, school_domains=(),
                                thread_id='thread', labels=labels, owner_addresses=addresses) is None


def test_reply_resolves_timer_and_notifies_once(tmp_path):
    from igris.followups import FollowupStore
    followups = FollowupStore(tmp_path / 'followups.db')
    followups.add('owner', 'thread', 100)
    sent, client = [], FakeClient()
    client.raw_details = lambda message_id: (client.raw(message_id), ('INBOX',), 'thread')
    w = watcher(tmp_path, client, sent, followups=followups, notice_owner='owner')
    w.poll()
    client.mails['reply'] = mail(subject='Re: Meeting')
    assert w.poll() == 1
    assert not followups.list('owner')
    assert len([text for text in sent if 'cevap verdi' in text]) == 1
    w.poll()
    assert len([text for text in sent if 'cevap verdi' in text]) == 1
    assert not followups.due()
