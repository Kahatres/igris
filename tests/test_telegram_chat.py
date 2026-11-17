"""The owner's Telegram chat with fake channels, assistants and clocks; no network."""

import json
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from igris.agenda import AgendaStore
from igris.approval_flow import ApprovalFlow
from igris.approvals import ApprovalStore
from igris.llm import BackendError, RateLimited
from igris.notify import Answer, NotifyError, TextMessage
from igris.telegram_chat import (BUSY_TEXT, HELP_TEXT, NON_TEXT_TEXT, SESSION, TelegramChat,
                                 split_message)

OWNER = "a" * 32
NOW = datetime(2026, 11, 12, 9, 0).astimezone()  # a Thursday, local time
WAIT = 5  # seconds an event may take before a test fails; never a sleep


def at(day, hour=None, minute=0):
    """An agenda value on November ``day``: a bare date, or local time with its offset."""
    if hour is None:
        return f"2026-11-{day:02d}"
    return datetime(2026, 11, day, hour, minute).astimezone().isoformat(timespec="minutes")


class Channel:
    """A Telegram notifier stand-in: records what was sent, can fail or block on purpose."""

    def __init__(self, fail=0, fail_at=(), block=None):
        self.sent, self.actions, self.fail = [], [], fail
        self.fail_at, self.calls, self.block = set(fail_at), 0, block
        self.typed_twice = threading.Event()
        self.entered = threading.Event()

    def send(self, text):
        self.entered.set()
        if self.block is not None:
            assert self.block.wait(WAIT)
        self.calls += 1
        if self.fail or self.calls in self.fail_at:
            self.fail = max(0, self.fail - 1)
            raise NotifyError("Telegram sendMessage request failed: down")
        self.sent.append(text)

    def send_chat_action(self, action="typing"):
        self.actions.append(action)
        if len(self.actions) >= 2:
            self.typed_twice.set()


class Approvals:
    def __init__(self, items=()):
        self.items = list(items)

    def pending(self, owner):
        assert owner == OWNER
        return self.items


class MailStore:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def recent(self, limit=50):
        return self.rows[:limit]


def never_ask(text):
    raise AssertionError("a shortcut must not call the assistant")


def answer(value="tamam"):
    return lambda text: SimpleNamespace(text=value, job_id=None)


def fill(agenda):
    add = lambda **item: agenda.upsert(OWNER, dict(item, branch="kisisel", source="manual",
                                                   status=item.get("status", "added")))[0]
    add(title="Spor", start=at(12, 18), due=at(12, 19))
    add(title="Kira günü", kind="payment", due=at(12))
    add(title="Ödev 1", kind="assignment", due=at(10, 23, 59))
    add(title="Sınav", kind="exam", due=at(14, 10), status="proposed")
    add(title="Uzak iş", due=at(22))
    done = add(title="Bitmiş", due=at(12, 12))
    agenda.set_status(OWNER, done["id"], "done")


MAIL = [{"seen_at": "2026-11-12T08:00:00", "sender": "Ev sahibi <ev@x.com>",
         "subject": "Kira ödemesi", "category": "gorev",
         "task": {"title": "Kirayı öde", "due": "2026-11-15"}},
        {"seen_at": "2026-11-12T07:00:00", "sender": "Google", "subject": "Yeni oturum",
         "category": "guvenlik", "task": None},
        {"seen_at": "2026-11-12T06:00:00", "sender": "Bülten", "subject": "Haberler",
         "category": "tanitim", "task": None}]


def make(tmp_path, *, ask=never_ask, items=True, mail=MAIL, approvals=("Mail gönderilsin mi?",),
         channel=None, **kwargs):
    agenda = AgendaStore(tmp_path / "agenda.sqlite3")
    if items and not agenda.list(OWNER):
        fill(agenda)
    channel = channel or Channel()
    kwargs.setdefault("now", lambda: NOW)
    kwargs.setdefault("state_path", tmp_path / "telegram_chat.json")
    chat = TelegramChat(channel, ask, owner=lambda: OWNER, agenda=agenda,
                        inbox=MailStore(mail),
                        approvals=Approvals({"request_id": str(i), "summary": s}
                                            for i, s in enumerate(approvals)), **kwargs)
    return chat, channel


def text(value, message_id=None):
    return TextMessage(text=value, message_id=message_id)


def say(chat, *messages):
    for message in messages:
        chat.handle(text(message) if isinstance(message, str) else message)
    chat.join()


def journal(tmp_path):
    return json.loads((tmp_path / "telegram_chat.json").read_text(encoding="utf-8"))


# ---- shortcuts ----

def test_today_lists_todays_and_overdue_items(tmp_path):
    chat, channel = make(tmp_path)
    say(chat, "/bugun")
    assert channel.sent == ["Bugün (12.11 Perşembe):\n"
                            "• tüm gün Kira günü\n"
                            "• 18:00–19:00 Spor\n\n"
                            "Gecikenler:\n"
                            "• 10.11 Salı 23:59 Ödev 1"]


def test_an_item_past_midnight_shows_both_days(tmp_path):
    chat, channel = make(tmp_path)
    chat.agenda.upsert(OWNER, {"branch": "kisisel", "source": "manual", "status": "added",
                               "title": "Vardiya", "start": at(12, 22), "due": at(13, 8)})
    say(chat, "/hafta")
    assert "• 12.11 Perşembe 22:00–13.11 Cuma 08:00 Vardiya" in channel.sent[0]


def test_week_groups_the_next_seven_days_by_day(tmp_path):
    chat, channel = make(tmp_path)
    say(chat, "/hafta")
    assert channel.sent == ["Önümüzdeki 7 gün:\n\n"
                            "Bugün 12.11:\n"
                            "• tüm gün Kira günü\n"
                            "• 18:00–19:00 Spor\n\n"
                            "Cumartesi 14.11:\n"
                            "• 10:00 Sınav (öneri)"]


def test_mail_counts_and_lists_task_and_security_mail(tmp_path):
    chat, channel = make(tmp_path)
    say(chat, "/mail")
    assert channel.sent == ["Son 24 saatte 3 mail: görev 1, güvenlik 1, bilgi 0, rutin 0, "
                            "tanıtım 1.\n"
                            "Görev mailleri:\n"
                            "• Ev sahibi <ev@x.com> — Kira ödemesi (son tarih: 15.11.2026)\n"
                            "Güvenlik mailleri:\n"
                            "• Google — Yeni oturum"]


def test_approvals_lists_open_requests(tmp_path):
    chat, channel = make(tmp_path)
    say(chat, "/onaylar")
    assert channel.sent == ["Açık onaylar (1):\n• Mail gönderilsin mi?\n"
                            "Cevaplamak için onay mesajındaki düğmelere bas ya da web "
                            "arayüzünü aç."]


def test_status_reads_the_existing_status_objects(tmp_path):
    chat, channel = make(
        tmp_path,
        inbox_status=lambda: {"state": "ok", "last_poll": "2026-11-12T08:55:00", "error": None,
                              "channel": "ok", "pending_alerts": 2, "model": "down"},
        calendar_status=lambda: {"state": "needs_consent", "last_sync": "2026-11-12T08:50:00",
                                 "error": None},
        model_status=lambda: {"claude": {"state": "ok"}, "codex": {"state": "resting"}})
    say(chat, "/durum")
    assert channel.sent == ["Gmail: çalışıyor (son kontrol 08:55)\n"
                            "Mail modeli: cevap vermiyor\n"
                            "Bildirim kanalı: çalışıyor\n"
                            "Bekleyen uyarı: 2\n"
                            "Model: claude hazır, codex dinleniyor\n"
                            "Takvim eşitleme: Google izni gerekiyor (son 08:50)\n"
                            "Açık onay: 1"]


def test_status_survives_a_failing_getter(tmp_path):
    def broken():
        raise OSError("locked")

    chat, channel = make(tmp_path, inbox_status=broken)
    say(chat, "/durum")
    assert channel.sent[0].startswith("Gmail: hata")
    assert "Takvim eşitleme: kapalı" in channel.sent[0]


def test_empty_states(tmp_path):
    chat, channel = make(tmp_path, items=False, mail=(), approvals=())
    say(chat, "/bugun", "/hafta", "/mail", "/onaylar")
    assert channel.sent == ["Bugün (12.11 Perşembe):\nBugün için kayıt yok.",
                            "Önümüzdeki 7 günde kayıt yok.",
                            "Son 24 saatte mail yok.",
                            "Açık onay yok."]


def test_help_start_and_unknown_commands_show_the_shortcuts(tmp_path):
    chat, channel = make(tmp_path)
    say(chat, "/yardim", "/start", "/yardım", "/bilinmeyen", "/bugun@IgrisBot x")
    assert channel.sent[:4] == [HELP_TEXT] * 4
    assert channel.sent[4].startswith("Bugün (12.11 Perşembe):")
    assert "/bugun" in HELP_TEXT and "yarın 18:00 spor ekle" in HELP_TEXT


def test_no_shortcut_calls_the_assistant(tmp_path):
    chat, channel = make(tmp_path, ask=never_ask)
    say(chat, "/yardim", "/bugun", "/hafta", "/mail", "/onaylar", "/durum", "/x")
    assert len(channel.sent) == 7 and channel.actions == []


def test_non_text_messages_get_one_short_reply(tmp_path):
    chat, channel = make(tmp_path, ask=never_ask)
    say(chat, TextMessage(text="", kind="other"))
    assert channel.sent == [NON_TEXT_TEXT]


def test_handle_never_sends_on_the_poll_thread(tmp_path):
    """A shortcut whose send hangs (a 15 s Telegram timeout) must not hold up approvals."""
    release = threading.Event()
    channel = Channel(block=release)
    chat, _ = make(tmp_path, channel=channel, now=lambda: NOW.replace(hour=10),
                   morning_hour=8)
    store = ApprovalStore(str(tmp_path / "approvals.sqlite3"))

    class Poll:
        batches = []

        def poll_updates(self, offset=None):
            answers, messages = self.batches.pop(0)
            return answers, messages, 1

    poll = Poll()
    flow = ApprovalFlow(store, poll)
    flow.on_message = chat.handle
    request_id = store.request(OWNER, "mail", "Gönderilsin mi?", payload={})
    poll.batches = [([], [text("/bugun")]),
                    ([Answer(request_id=request_id, approved=True, source="telegram")], [])]
    flow.pump()
    assert channel.entered.wait(WAIT)  # the sender thread is stuck in send()
    assert chat.tick() is True  # the morning summary is only queued
    assert flow.pump() == 1 and store.status(request_id) == "approved"
    release.set()
    chat.join()
    assert channel.sent[0].startswith("Bugün") and channel.sent[1].startswith("Günaydın!")


# ---- free text ----

def test_free_text_reaches_the_assistant_and_the_reply_is_sent(tmp_path):
    asked = []

    def ask(value):
        asked.append(value)
        return SimpleNamespace(text="Eklendi: yarın 18:00 spor.", job_id=None)

    chat, channel = make(tmp_path, ask=ask)
    say(chat, text("yarın 18:00 spor ekle", 1))
    assert asked == ["yarın 18:00 spor ekle"]
    assert channel.sent == ["Eklendi: yarın 18:00 spor."]
    assert journal(tmp_path)["pending"] == [] and journal(tmp_path)["last_message"] == 1


def test_a_message_redelivered_after_a_restart_is_not_answered_twice(tmp_path):
    asked = []

    def ask(value):
        asked.append(value)
        return SimpleNamespace(text="tamam", job_id=None)

    chat, channel = make(tmp_path, ask=ask)
    say(chat, text("spor ekle", 7), text("spor ekle", 7))
    chat.close()
    again, other = make(tmp_path, ask=ask)  # a restart; Telegram hands out id 7 again
    say(again, text("spor ekle", 7), text("/bugun", 8))
    assert asked == ["spor ekle"] and channel.sent == ["tamam"]
    assert len(other.sent) == 1 and other.sent[0].startswith("Bugün")


def test_queued_messages_survive_a_stop_and_are_answered_once_after_the_restart(tmp_path):
    started, never = threading.Event(), threading.Event()
    asked = []

    def stuck(value):
        asked.append(value)
        started.set()
        never.wait(WAIT)  # the process "exits" while the model works
        raise BackendError("killed")

    chat, channel = make(tmp_path, ask=stuck)
    chat.handle(text("birinci", 1))
    chat.handle(text("ikinci", 2))
    assert started.wait(WAIT)
    begin = time.monotonic()
    chat.close(timeout=0.2)  # bounded: it does not wait for the model
    assert time.monotonic() - begin < 2
    assert [e["id"] for e in journal(tmp_path)["pending"]] == [1, 2]

    def ask(value):
        asked.append(value)
        return SimpleNamespace(text="cevap " + value, job_id=None)

    again, other = make(tmp_path, ask=ask, morning_hour=None)
    again.tick()  # the first poll after the start queues the journal again
    again.join()
    again.handle(text("ikinci", 2))  # handed out again: ignored
    again.join()
    assert other.sent == ["cevap birinci", "cevap ikinci"] and channel.sent == []
    assert asked == ["birinci", "birinci", "ikinci"]  # the stopped one is asked again, once
    assert journal(tmp_path)["pending"] == []
    never.set()  # let the first process's stuck thread end
    chat.close(timeout=WAIT)
    assert journal(tmp_path)["pending"] == []  # the old worker must not overwrite the restart


def test_an_undelivered_reply_is_retried_without_asking_the_model_again(tmp_path):
    asked = []

    def ask(value):
        asked.append(value)
        return SimpleNamespace(text="cevap", job_id=None)

    channel = Channel(fail=1)
    chat, _ = make(tmp_path, ask=ask, channel=channel, morning_hour=None)
    say(chat, text("soru", 1))
    assert channel.sent == [] and journal(tmp_path)["pending"][0]["parts"] == ["cevap"]
    chat.close()
    again, other = make(tmp_path, ask=ask, morning_hour=None)  # also across a restart
    again.tick()
    again.join()
    assert other.sent == ["cevap"] and asked == ["soru"]
    assert journal(tmp_path)["pending"] == []


def test_a_multipart_reply_resumes_at_the_failed_part(tmp_path):
    words = [f"kelime{i}" for i in range(1100)]
    channel = Channel(fail_at={2})
    chat, _ = make(tmp_path, ask=answer(" ".join(words)), channel=channel, morning_hour=None)
    say(chat, text("uzun", 1))
    assert len(channel.sent) == 1
    chat.tick()
    chat.join()
    assert " ".join(channel.sent).split() == words  # no part twice, none missing


def test_a_long_reply_is_split_without_cutting_words(tmp_path):
    words = [f"kelime{i}" for i in range(1100)]
    long = " ".join(words[:400]) + "\n\n" + " ".join(words[400:])
    assert len(long) > 9000
    chat, channel = make(tmp_path, ask=answer(long))
    say(chat, "uzun anlat")
    assert len(channel.sent) >= 3
    assert all(len(part) <= 4096 for part in channel.sent)
    assert " ".join(channel.sent).split() == words


def test_split_message_prefers_paragraphs_and_cuts_a_huge_word_only_when_needed():
    text_ = "a" * 3000 + "\n\n" + "b" * 3000
    assert split_message(text_) == ["a" * 3000, "b" * 3000]
    assert [len(p) for p in split_message("x" * 9000)] == [4096, 4096, 808]
    assert split_message("kısa") == ["kısa"]


@pytest.mark.parametrize("error, reason", [
    (BackendError("claude exited 1: stderr says SECRET-PATH C:/Users/x"), "modele ulaşılamadı"),
    (BackendError("claude timed out after 300s"), "model zamanında cevap vermedi"),
    (RateLimited("groq", None, "429 body SECRET"),"model kotası doldu ya da hız sınırına takıldı"),
    (ValueError("SECRET internal"), "beklenmeyen bir hata oldu"),
])
def test_a_failing_assistant_sends_a_fixed_reason_and_logs_the_detail(tmp_path, error, reason):
    def ask(value):
        raise error

    logs = []
    chat, channel = make(tmp_path, ask=ask, log=logs.append)
    say(chat, "bugün ne var?")
    assert channel.sent == [f"Şu an cevap veremiyorum: {reason}. Kısayollar çalışıyor: /yardim"]
    assert "SECRET" not in channel.sent[0] and "exited" not in channel.sent[0]
    assert any(str(error).split()[0] in line for line in logs)
    assert not any("bugün ne var" in line for line in logs)  # the owner's text is never logged


def test_a_routed_job_is_reported_with_its_id(tmp_path):
    chat, channel = make(tmp_path, ask=lambda v: SimpleNamespace(text="routed", job_id="j42"))
    say(chat, "büyük bir kod işi")
    assert "j42" in channel.sent[0] and "kuyruğa alındı" in channel.sent[0]


def test_the_queue_holds_five_waiting_messages(tmp_path):
    started, release = threading.Event(), threading.Event()
    asked = []

    def ask(value):
        asked.append(value)
        started.set()
        assert release.wait(WAIT)
        return SimpleNamespace(text="ok " + value, job_id=None)

    chat, channel = make(tmp_path, ask=ask)
    chat.handle(text("m0"))
    assert started.wait(WAIT)  # m0 is being answered, nothing waits
    for i in range(1, 7):
        chat.handle(text(f"m{i}"))
    chat._out.join()
    assert channel.sent == [BUSY_TEXT]  # m6 was turned away
    release.set()
    chat.join()
    assert asked == [f"m{i}" for i in range(6)]
    assert channel.sent[1:] == [f"ok m{i}" for i in range(6)]


def test_typing_is_shown_while_the_assistant_works(tmp_path):
    channel = Channel()

    def ask(value):
        assert channel.typed_twice.wait(WAIT)  # the action repeats while the model works
        return SimpleNamespace(text="tamam", job_id=None)

    chat, _ = make(tmp_path, ask=ask, channel=channel, typing_interval=0.01)
    say(chat, "düşün")
    assert set(channel.actions) == {"typing"} and channel.sent == ["tamam"]


def test_an_approval_is_delivered_while_a_chat_answer_is_in_progress(tmp_path):
    started, release = threading.Event(), threading.Event()

    def ask(value):
        started.set()
        assert release.wait(WAIT)
        return SimpleNamespace(text="cevap", job_id=None)

    class Poll(Channel):
        def __init__(self):
            super().__init__()
            self.batches = []

        def ask(self, summary, request_id):
            pass

        def poll_updates(self, offset=None):
            answers, messages = self.batches.pop(0)
            return answers, messages, (offset or 0) + 1

    channel = Poll()
    chat, _ = make(tmp_path, ask=ask, channel=channel)
    store = ApprovalStore(str(tmp_path / "approvals.sqlite3"))
    flow = ApprovalFlow(store, channel)
    flow.on_message = chat.handle
    request_id = flow.request(OWNER, "mail", "Gönderilsin mi?", payload={})
    channel.batches = [([], [text("uzun bir soru")]),
                       ([Answer(request_id=request_id, approved=True, source="telegram")], [])]

    flow.pump()
    assert started.wait(WAIT)
    assert flow.pump() == 1  # the model is still busy
    assert store.status(request_id) == "approved" and not release.is_set()
    release.set()
    chat.join()
    assert channel.sent == ["cevap"]


# ---- morning summary ----

class Clock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


def test_morning_summary_text(tmp_path):
    chat, _ = make(tmp_path)
    assert chat.morning_text(NOW) == (
        "Günaydın! Bugün 12.11.2026 Perşembe.\n\n"
        "Bugün:\n• tüm gün Kira günü\n• 18:00–19:00 Spor\n\n"
        "Gecikenler:\n• 10.11 Salı 23:59 Ödev 1\n\n"
        "Önümüzdeki 7 gün:\n• 14.11 Cumartesi 10:00 Sınav (öneri)\n\n"
        "Açık onay: 1\n\n"
        "Son 24 saatte maildeki görevler:\n"
        "• Kirayı öde (Ev sahibi <ev@x.com>), son tarih 15.11.2026")


def test_morning_summary_is_sent_once_at_the_hour_and_not_again_after_a_restart(tmp_path):
    clock = Clock(NOW.replace(hour=7, minute=59))
    chat, channel = make(tmp_path, now=clock, morning_hour=8)
    assert chat.tick() is False and channel.sent == []
    clock.moment = NOW.replace(hour=8, minute=0)
    assert chat.tick() is True
    chat.join()
    assert len(channel.sent) == 1 and channel.sent[0].startswith("Günaydın!")
    assert chat.tick() is False and chat.run_morning() is False and len(channel.sent) == 1
    chat.close()
    again, other = make(tmp_path, now=clock, morning_hour=8)  # a restart
    assert again.tick() is False and again.run_morning() is False and other.sent == []
    clock.moment = NOW.replace(hour=8) + timedelta(days=1)
    assert again.run_morning() is True and len(other.sent) == 1


def test_morning_summary_is_sent_late_when_the_server_starts_after_the_hour(tmp_path):
    chat, channel = make(tmp_path, now=Clock(NOW.replace(hour=15)), morning_hour=8)
    assert chat.run_morning() is True and channel.sent[0].startswith("Günaydın!")


def test_morning_summary_keeps_its_claim_after_a_failed_delivery(tmp_path):
    channel = Channel(fail=1)
    logs = []
    chat, _ = make(tmp_path, now=Clock(NOW), morning_hour=8, channel=channel, log=logs.append)
    assert chat.run_morning() is False and channel.sent == []
    assert journal(tmp_path)["morning"]["next"] == 1
    assert chat.run_morning() is False and channel.sent == [] and len(logs) == 1


def test_a_multipart_morning_summary_resumes_and_never_repeats_a_part(tmp_path):
    channel = Channel(fail_at={2})
    chat, _ = make(tmp_path, now=Clock(NOW), morning_hour=8, channel=channel)
    parts = ["a" * 3000, "b" * 3000, "c" * 3000]
    chat.morning_text = lambda now=None: "\n\n".join(parts)
    assert chat.run_morning() is False and channel.sent == parts[:1]
    chat.close()
    again, other = make(tmp_path, now=Clock(NOW), morning_hour=8, channel=channel)
    again.morning_text = lambda now=None: "a changed text must not be built again"
    assert again.run_morning() is True and channel.sent == [parts[0], parts[2]]


def test_an_unreadable_state_file_skips_todays_summary(tmp_path):
    (tmp_path / "telegram_chat.json").write_text("{broken", encoding="utf-8")
    clock = Clock(NOW)
    logs = []
    chat, channel = make(tmp_path, now=clock, morning_hour=8, log=logs.append)
    assert chat.tick() is False and chat.run_morning() is False and channel.sent == []
    assert any("unreadable" in line for line in logs)
    clock.moment = NOW + timedelta(days=1)
    assert chat.run_morning() is True and len(channel.sent) == 1


def test_a_state_that_cannot_be_written_gives_the_day_up(tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    logs = []
    chat, channel = make(tmp_path, now=Clock(NOW), morning_hour=8, log=logs.append,
                         state_path=blocker / "telegram_chat.json")
    assert chat.run_morning() is False and chat.run_morning() is False
    assert channel.sent == [] and chat.tick() is False
    assert any("skipped today" in line for line in logs)


def test_morning_summary_off(tmp_path):
    chat, channel = make(tmp_path, now=Clock(NOW.replace(hour=12)), morning_hour=None)
    assert chat.tick() is False and chat.run_morning() is False and channel.sent == []
    with pytest.raises(ValueError):
        TelegramChat(Channel(), morning_hour=24)


def test_digest_state_loaded_first_does_not_skip_pending_message_recovery(tmp_path):
    path = tmp_path / "telegram_chat.json"
    path.write_text(json.dumps({"last_message": 4, "pending": [
        {"id": 4, "kind": "text", "text": "recover me"}]}), encoding="utf-8")
    chat, channel = make(tmp_path, ask=answer("recovered"), morning_hour=None)
    assert chat.run_morning() is False
    chat.tick()
    chat.join()
    assert channel.sent == ["recovered"] and journal(tmp_path)["pending"] == []
    chat.close()


def test_original_digest_day_marker_is_honored_after_upgrade(tmp_path):
    (tmp_path / "telegram_chat.json").write_text(
        json.dumps({"morning_sent": NOW.date().isoformat()}), encoding="utf-8")
    chat, channel = make(tmp_path, morning_hour=8)
    assert chat.run_morning() is False and chat.tick() is False
    assert channel.sent == []


def test_direct_digest_call_skips_unreadable_state(tmp_path):
    (tmp_path / "telegram_chat.json").write_text("{broken", encoding="utf-8")
    chat, channel = make(tmp_path, morning_hour=8)
    assert chat.run_morning() is False and channel.sent == []


@pytest.mark.parametrize("stage", ["build", "send"])
def test_digest_work_never_holds_up_owner_messages_or_approvals(tmp_path, stage):
    entered, release = threading.Event(), threading.Event()
    channel = Channel(block=release if stage == "send" else None)
    chat, _ = make(tmp_path, channel=channel, morning_hour=8)

    def build(now=None):
        entered.set()
        assert release.wait(WAIT)
        return "digest"

    if stage == "build":
        chat.morning_text = build
    else:
        entered = channel.entered
    store = ApprovalStore(str(tmp_path / "approvals.sqlite3"))
    request_id = store.request(OWNER, "mail", "Approve?", payload={})

    class Poll:
        def poll_updates(self, offset=None):
            return ([Answer(request_id=request_id, approved=True, source="telegram")],
                    [text("/yardim", 10)], 1)

    flow = ApprovalFlow(store, Poll())
    flow.on_message = chat.handle
    finished = threading.Event()
    counts = []

    def pump():
        counts.append(flow.pump())
        finished.set()

    try:
        assert chat.tick() is True
        assert entered.wait(WAIT)
        worker = threading.Thread(target=pump)
        worker.start()
        assert finished.wait(1), "digest work blocked the approval poll"
        assert counts == [1] and store.status(request_id) == "approved"
    finally:
        release.set()
        chat.join()
        chat.close()
    assert channel.sent[-1] == HELP_TEXT


def test_concurrent_digest_call_does_not_duplicate_an_inflight_part(tmp_path):
    release = threading.Event()
    channel = Channel(block=release)
    chat, _ = make(tmp_path, channel=channel, morning_hour=8)
    try:
        assert chat.tick() is True and channel.entered.wait(WAIT)
        assert chat.run_morning() is False
        assert chat.tick() is False
    finally:
        release.set()
        chat.join()
        chat.close()
    assert len(channel.sent) == 1


def test_digest_claim_survives_a_crash_before_delivery_progress_is_known(tmp_path):
    parts = ["already claimed", "remaining"]
    (tmp_path / "telegram_chat.json").write_text(json.dumps({"morning": {
        "day": NOW.date().isoformat(), "parts": parts, "next": 1}}), encoding="utf-8")
    chat, channel = make(tmp_path, morning_hour=8)
    assert chat.run_morning() is True and channel.sent == ["remaining"]


def test_unsaved_message_is_redelivered_before_later_messages_are_accepted(tmp_path, monkeypatch):
    chat, channel = make(tmp_path, ask=answer("saved"), morning_hour=None)
    persist = chat._persist
    monkeypatch.setattr(chat, "_persist", lambda what: False)
    store = ApprovalStore(str(tmp_path / "approvals.sqlite3"))
    request_id = store.request(OWNER, "mail", "Approve?", payload={})

    class Poll:
        offsets = []

        def poll_updates(self, offset=None):
            self.offsets.append(offset)
            if offset == 30:
                return [], [], 30
            return ([Answer(request_id=request_id, approved=True, source="telegram")],
                    [text("one", 1), text("two", 2)], 30)

    poll = Poll()
    flow = ApprovalFlow(store, poll)
    flow.on_message = chat.handle
    assert flow.pump() == 1  # approval still works while the journal is unavailable
    assert chat._get_state().get("pending") == [] and channel.sent == []
    assert chat._get_state().get("last_message") is None
    monkeypatch.setattr(chat, "_persist", persist)
    assert flow.pump() == 0  # approval replay is harmless, messages are accepted now
    chat.join()
    flow.pump()
    assert poll.offsets == [None, None, 30]
    assert channel.sent == ["saved", "saved"] and journal(tmp_path)["pending"] == []
    chat.close()


def test_successful_send_finishing_during_close_is_recorded_before_restart(tmp_path):
    release = threading.Event()
    channel = Channel(block=release)
    chat, _ = make(tmp_path, channel=channel, morning_hour=None)
    chat.handle(text("/yardim", 1))
    assert channel.entered.wait(WAIT)
    chat.close(timeout=0)
    release.set()
    chat.close(timeout=WAIT)
    assert journal(tmp_path)["pending"][0]["sent"] == 1
    again, other = make(tmp_path, morning_hour=None)
    again.tick()
    again.join()
    assert channel.sent == [HELP_TEXT] and other.sent == []
    assert journal(tmp_path)["pending"] == []
    again.close()


def test_accepted_digest_with_lost_response_is_not_repeated_after_restart(tmp_path):
    class LostResponse(Channel):
        def send(self, value):
            self.sent.append(value)  # Telegram accepted it; only the response is lost
            raise NotifyError("timed out")

    channel = LostResponse()
    chat, _ = make(tmp_path, channel=channel, morning_hour=8)
    assert chat.run_morning() is False and len(channel.sent) == 1
    again, other = make(tmp_path, morning_hour=8)
    assert again.run_morning() is False and again.tick() is False
    assert other.sent == []


# ---- the web wiring ----

def test_web_wiring_asks_in_the_telegram_session_as_the_owner(tmp_path):
    from igris import web

    calls = []

    class Contexts:
        def ask_in_session(self, user, session, value, *, note=""):
            calls.append((user, session, value, bool(note)))
            return SimpleNamespace(text="ok", job_id=None)

    class Accounts:
        def user(self, user_id):
            return "user:" + user_id

    assistant = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path, morning_hour=None),
                                router=None)
    chat = web.build_telegram_chat(assistant, Channel(), Accounts(), Contexts(), lambda: OWNER)
    assert chat.ask("merhaba").text == "ok"
    assert calls == [("user:" + OWNER, SESSION, "merhaba", True)] and SESSION == "telegram"
