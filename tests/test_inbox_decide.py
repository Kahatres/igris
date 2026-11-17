"""The inbox with the decision step: categories, who gets told, Görev Takvimi items, forwards."""

from datetime import datetime
import json
import sqlite3

from igris import gmail, inbox
from igris.agenda import AgendaStore
from igris.notices import NoticeStore

PASS = "Authentication-Results: mx.google.com; spf=pass; dkim=pass; dmarc=pass\r\n"
SCHOOL = ("stud.uni.example", "uni.example")
RENT_BODY = ("Merhaba,\r\n"
             "Kasım ayı kirası 500 EUR, lütfen 15 Kasım 2026 tarihine kadar hesabıma yatır.\r\n"
             "Teşekkürler, Ali\r\n")
RENT_ANSWER = {
    "category": "gorev", "reason": "Ev sahibi Kasım kirasını istiyor.",
    "task": {"title": "Kasım kirasını öde", "kind": "payment", "due": "2026-11-15",
             "start": None, "branch": "kisisel", "mandatory": True, "confidence": 0.95},
    "evidence": "Kasım ayı kirası 500 EUR, lütfen 15 Kasım 2026 tarihine kadar hesabıma yatır.",
}


def raw(sender="Ali <ali@landlord.example>", subject="Kasım kirası", body=RENT_BODY,
        auth=PASS, extra=""):
    return (auth + f"From: {sender}\r\nSubject: {subject}\r\n{extra}"
            "Date: Sat, 07 Nov 2026 10:00:00 +0000\r\n\r\n" + body).encode()


class Client:
    def __init__(self, mails=None, labels=None):
        self.mails, self.labels = dict(mails or {}), dict(labels or {})

    def list_ids(self, query="", max_results=10):
        return list(self.mails)

    def raw_with_labels(self, message_id):
        return self.mails[message_id], tuple(self.labels.get(message_id, ("INBOX",)))


class Model:
    def __init__(self, answer=RENT_ANSWER):
        self.answer, self.calls = answer, []

    def __call__(self, system, prompt):
        self.calls.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return json.dumps(self.answer)


def make(tmp_path, client, *, model=None, school=(), digest=False):
    sent = []
    w = inbox.InboxWatcher(
        lambda: client, inbox.InboxStore(tmp_path / "inbox.sqlite3"), sent.append,
        gmail.AuthAlarm(tmp_path / "gmail_auth_down", sent.append),
        known_domains=("ziraatbank.com.tr",), now=lambda: datetime(2026, 11, 9, 20, 0),
        model=model, school_domains=school, digest=digest,
        notices=NoticeStore(tmp_path / "n.sqlite3"),
        agenda=AgendaStore(tmp_path / "a.sqlite3"), notice_owner=lambda: "owner")
    return w, sent


def first_poll_then(w, client, mails, labels=None):
    w.poll()  # the baseline: what was already there is only recorded
    client.mails.update(mails)
    client.labels.update(labels or {})
    w.poll()
    return {r["id"]: r for r in w.store.recent(50)}


def test_rent_mail_becomes_a_task_alert_notice_and_calendar_item(tmp_path):
    client, model = Client({"old": raw(subject="eski kira")}), Model()
    w, sent = make(tmp_path, client, model=model)
    rows = first_poll_then(w, client, {"rent": raw()})
    assert len(model.calls) == 1  # the baseline asked no model
    assert rows["rent"]["category"] == "gorev" and rows["rent"]["task"]["kind"] == "payment"
    assert rows["old"]["category"] == "gorev" and rows["old"]["task"] is None
    assert len(sent) == 1 and "Yapman gereken" in sent[0] and "15.11.2026" in sent[0]
    [item] = w.agenda.list("owner")
    assert item["status"] == "added" and item["source"] == "gmail" and item["source_ref"] == "rent"
    [notice] = w.notices.list("owner")
    assert notice["kind"] == "mail_important" and notice["action"] == {"agenda": item["id"]}
    w.poll()
    assert len(sent) == 1 and len(w.agenda.list("owner")) == 1


def test_unsure_task_is_proposed_and_quiet(tmp_path):
    answer = dict(RENT_ANSWER, task=dict(RENT_ANSWER["task"], confidence=0.6))
    client = Client()
    w, sent = make(tmp_path, client, model=Model(answer))
    first_poll_then(w, client, {"rent": raw()})
    assert sent == [] and w.notices.list("owner") == []
    assert [i["status"] for i in w.agenda.list("owner")] == ["proposed"]


def test_promotions_and_info_mail_never_reach_the_phone(tmp_path):
    info = {"category": "bilgi", "reason": "Kulüp duyurusu.", "task": None, "evidence": ""}
    client, model = Client(), Model(info)
    w, sent = make(tmp_path, client, model=model)
    rows = first_poll_then(w, client, {
        "promo": raw(sender="Shop <shop@store.example>", subject="Yeni sezon", body="Kampanya"),
        "news": raw(sender="Kulüp <club@example.org>", subject="Kulüp duyurusu", body="Toplantı yok")},
        labels={"promo": ("INBOX", "CATEGORY_PROMOTIONS")})
    assert rows["promo"]["category"] == "tanitim" and rows["news"]["category"] == "bilgi"
    assert len(model.calls) == 1 and sent == [] and w.notices.list("owner") == []


def test_account_security_alert_is_sent_at_once(tmp_path):
    client = Client()
    w, sent = make(tmp_path, client, model=Model())
    rows = first_poll_then(w, client, {"sec": raw(sender="Google <no-reply@accounts.google.com>",
                                                  subject="Security alert", body="New sign-in")})
    assert rows["sec"]["category"] == "guvenlik"
    assert len(sent) == 1 and sent[0].startswith("🔐")
    assert w.notices.list("owner")[0]["kind"] == "mail_risky"


def test_a_failing_model_falls_back_and_the_poll_goes_on(tmp_path):
    client = Client()
    w, sent = make(tmp_path, client, model=Model(RuntimeError("usage limit")))
    rows = first_poll_then(w, client, {"rent": raw()})
    assert w.status()["state"] == "ok" and rows["rent"]["category"] == "gorev"
    assert "model okuyamadı" in rows["rent"]["reason"]
    # a possible task the model could not read still reaches the phone
    assert len(sent) == 1 and sent[0].startswith("📌 Görev olabilir, model okuyamadı.")


def test_model_reads_per_poll_are_bounded_and_the_rest_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "MAX_MODEL_PER_POLL", 2)
    client, model = Client(), Model()
    w, _ = make(tmp_path, client, model=model)
    w.poll()
    client.mails.update({f"m{i}": raw(subject=f"Kira {i}") for i in range(5)})
    assert w.poll() == 2
    assert w.poll() == 2 and w.poll() == 1 and len(model.calls) == 5


def test_digest_is_off_unless_asked(tmp_path):
    client = Client()
    w, sent = make(tmp_path, client)
    first_poll_then(w, client, {"m": raw(subject="Faturanız hazır")})
    assert not any(t.startswith("📬") for t in sent) and w.store.get_meta("digest_day") is None


def test_an_old_database_gains_the_decision_columns(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE mails (id TEXT PRIMARY KEY, seen_at TEXT NOT NULL, sender TEXT,
        subject TEXT, label TEXT, risk TEXT, flags TEXT, spf TEXT, dkim TEXT, dmarc TEXT,
        pending_alert INTEGER NOT NULL DEFAULT 0, digested INTEGER NOT NULL DEFAULT 1)""")
    db.execute("INSERT INTO mails (id, seen_at, sender, subject, label, risk, flags, spf, dkim, "
               "dmarc) VALUES ('x', '2026-11-01T10:00:00', 'a', 'b', 'routine', 'low', '', "
               "'pass', 'pass', 'pass')")
    db.commit()
    db.close()
    [row] = inbox.InboxStore(path).recent()
    assert row["category"] is None and row["task"] is None and row["origin"] is None


# ---- school mail forwarded from Outlook into this Gmail inbox ----

FORWARD_BODY = ("---------- Forwarded message ---------\r\n"
                "From: Dr. Ada Lovelace <ada@uni.example>\r\n"
                "Subject: Midterm\r\n\r\n"
                "The midterm is on 20 November 2026 at 10:00 in room 101.\r\n")
MIDTERM = {"category": "gorev", "reason": "Ara sınav tarihi duyurulmuş.",
           "task": {"title": "Ara sınav", "kind": "exam", "due": None,
                    "start": "2026-11-20T10:00:00+02:00", "branch": "kisisel",
                    "mandatory": True, "confidence": 0.9},
           "evidence": "The midterm is on 20 November 2026 at 10:00 in room 101."}


def test_rewritten_from_forward_is_school_work_from_the_original_sender(tmp_path):
    client, model = Client(), Model(MIDTERM)
    w, sent = make(tmp_path, client, model=model, school=SCHOOL)
    mail = raw(sender="Student <student@stud.uni.example>", subject="FW: Midterm", body=FORWARD_BODY,
               extra="Reply-To: ada@uni.example\r\nTo: student@gmail.example\r\n")
    rows = first_poll_then(w, client, {"fw": mail})
    row = rows["fw"]
    assert row["origin"] == "okul" and "ada@uni.example" in row["sender"]
    assert row["risk"] == "low"  # the Reply-To of a rewritten From is the forward, not a risk
    assert row["category"] == "gorev" and row["task"]["branch"] == "okul"
    assert "university mailbox" in model.calls[0]
    assert [i["branch"] for i in w.agenda.list("owner")] == ["okul"]
    assert len(sent) == 1


def test_redirect_whose_hop_breaks_dmarc_is_not_raised_when_gmail_vouches_by_arc(tmp_path):
    hop = ("Authentication-Results: mx.google.com; arc=pass (i=1 spf=fail); spf=fail; dkim=fail; "
           "dmarc=fail\r\n" + school_arc())
    client = Client()
    w, sent = make(tmp_path, client, model=Model(MIDTERM), school=SCHOOL)
    mail = raw(sender="Dr. Ada <ada@uni.example>", subject="Midterm", body=FORWARD_BODY,
               auth=hop, extra="X-Forwarded-To: student@gmail.example\r\n"
                             "To: student@stud.uni.example\r\n")
    row = first_poll_then(w, client, {"fw": mail})["fw"]
    assert row["origin"] == "okul" and "auth_failed" in row["flags"]
    assert row["risk"] == "low" and row["category"] == "gorev"
    assert not any(text.startswith("⚠️") for text in sent)


def school_arc(sealer="microsoft.com", first_hop="dmarc=pass action=none header.from=uni.example"):
    """The ARC set the school's Microsoft 365 adds when it forwards: its seal and what it saw."""
    return (f"ARC-Seal: i=1; a=rsa-sha256; s=arcselector; d={sealer}; cv=none; b=xyz\r\n"
            f"ARC-Authentication-Results: i=1; mx.microsoft.com 1; spf=pass; {first_hop}\r\n")


def test_a_forged_gmail_arc_line_below_the_real_one_does_not_lower_the_risk(tmp_path):
    # Gmail's real verdict is on top; the attacker's own copy that only looks like Gmail's
    # (and an ARC set that looks like the school's) sits further down
    auth = ("Authentication-Results: mx.google.com; spf=fail; dkim=fail; dmarc=fail\r\n"
            "Authentication-Results: mx.google.com; arc=pass; spf=pass; dkim=pass; dmarc=pass\r\n"
            + school_arc())
    client = Client()
    w, sent = make(tmp_path, client, model=Model(MIDTERM), school=SCHOOL)
    mail = raw(sender="Dr. Ada <ada@uni.example>", subject="Midterm", body=FORWARD_BODY,
               auth=auth, extra="To: student@stud.uni.example\r\n")
    row = first_poll_then(w, client, {"fw": mail})["fw"]
    assert row["risk"] == "high" and row["category"] == "guvenlik"
    assert sent and sent[0].startswith("⚠️")


def test_arc_from_a_sealer_that_is_not_the_school_does_not_lower_the_risk(tmp_path):
    real = "Authentication-Results: mx.google.com; arc=pass; spf=fail; dkim=fail; dmarc=fail\r\n"
    client = Client()
    w, _ = make(tmp_path, client, model=Model(MIDTERM), school=SCHOOL)
    mail = raw(sender="Dr. Ada <ada@uni.example>", subject="Midterm", body=FORWARD_BODY,
               auth=real + school_arc(sealer="attacker.example"),
               extra="To: student@stud.uni.example\r\n")
    assert first_poll_then(w, client, {"fw": mail})["fw"]["risk"] == "high"


def test_arc_whose_first_hop_saw_a_dmarc_failure_does_not_lower_the_risk(tmp_path):
    real = "Authentication-Results: mx.google.com; arc=pass; spf=fail; dkim=fail; dmarc=fail\r\n"
    client = Client()
    w, _ = make(tmp_path, client, model=Model(MIDTERM), school=SCHOOL)
    mail = raw(sender="Dr. Ada <ada@uni.example>", subject="Midterm", body=FORWARD_BODY,
               auth=real + school_arc(first_hop="dmarc=fail action=oreject header.from=uni.example"),
               extra="To: student@stud.uni.example\r\n")
    assert first_poll_then(w, client, {"fw": mail})["fw"]["risk"] == "high"


def test_a_microsoft_seal_from_another_tenant_does_not_lower_the_risk(tmp_path):
    # an attacker's own Microsoft 365 tenant seals as microsoft.com too, but its first hop can
    # only pass DMARC for the attacker's domain, never for the school
    real = "Authentication-Results: mx.google.com; arc=pass; spf=fail; dkim=fail; dmarc=fail\r\n"
    client = Client()
    w, _ = make(tmp_path, client, model=Model(MIDTERM), school=SCHOOL)
    mail = raw(sender="Prof <prof@attacker.example>", subject="Midterm", body=FORWARD_BODY,
               auth=real + school_arc(first_hop="dmarc=pass action=none header.from=attacker.example"),
               extra="To: student@stud.uni.example\r\n")
    assert first_poll_then(w, client, {"fw": mail})["fw"]["risk"] == "high"


def test_the_school_s_own_seal_vouches_for_any_sender_it_verified(tmp_path):
    real = "Authentication-Results: mx.google.com; arc=pass; spf=fail; dkim=fail; dmarc=fail\r\n"
    client = Client()
    w, _ = make(tmp_path, client, model=Model(MIDTERM), school=SCHOOL)
    mail = raw(sender="Prof <prof@other-uni.example>", subject="Midterm", body=FORWARD_BODY,
               auth=real + school_arc(sealer="uni.example",
                                      first_hop="dmarc=pass header.from=other-uni.example"),
               extra="To: student@stud.uni.example\r\n")
    assert first_poll_then(w, client, {"fw": mail})["fw"]["risk"] == "low"


def test_a_claimed_forward_without_proof_keeps_its_risk(tmp_path):
    fail = "Authentication-Results: mx.google.com; spf=fail; dkim=fail; dmarc=fail\r\n"
    client = Client()
    w, sent = make(tmp_path, client, model=Model(MIDTERM), school=SCHOOL)
    mail = raw(sender="Dr. Ada <ada@uni.example>", subject="Midterm", body=FORWARD_BODY,
               auth=fail, extra="To: student@stud.uni.example\r\n")
    row = first_poll_then(w, client, {"fw": mail})["fw"]
    assert row["risk"] == "high" and row["category"] == "guvenlik"
    assert sent and sent[0].startswith("⚠️")


def test_content_signs_still_count_in_a_forward(tmp_path):
    hop = "Authentication-Results: mx.google.com; arc=pass; spf=fail; dkim=pass; dmarc=pass\r\n"
    client = Client()
    w, _ = make(tmp_path, client, model=Model(MIDTERM), school=SCHOOL)
    mail = raw(sender='"ziraatbank.com.tr" <x@evil.example>', subject="Hesabınız",
               body="Giriş yapın", auth=hop, extra="To: student@stud.uni.example\r\n")
    row = first_poll_then(w, client, {"fw": mail})["fw"]
    assert row["risk"] == "high" and row["category"] == "guvenlik"


def test_without_school_domains_nothing_is_school(tmp_path):
    client = Client()
    w, _ = make(tmp_path, client, model=Model(MIDTERM))
    mail = raw(sender="Dr. Ada <ada@uni.example>", subject="Midterm", body=FORWARD_BODY)
    row = first_poll_then(w, client, {"fw": mail})["fw"]
    assert row["origin"] == "" and row["task"]["branch"] == "kisisel"
