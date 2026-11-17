"""The owner's Telegram chat: free text to the assistant, shortcuts and the morning summary.

Igris reads Telegram through one poll (``ApprovalFlow.pump`` calls the notifier's
``poll_updates``); the owner's messages from that poll land in :meth:`TelegramChat.handle`,
which must return at once so approval answers keep flowing. It does no network call: it
writes the message to a journal in ``state_path`` and queues it. Two worker threads do the
rest:

* the sender answers a slash shortcut (``/bugun``, ``/mail``...) from the stores, with no
  model call, so it works even when the model quota is used up; a photo, voice note,
  sticker or document gets one short reply; it also sends every reply and the morning
  summary;
* the model worker asks the assistant about free text, one message at a time (at most
  ``max_waiting`` waiting), shows "typing..." meanwhile and hands the plain-text answer,
  split under Telegram's limit, to the sender.

A message leaves the journal only when its whole reply was delivered. A reply is kept in the
journal before it is sent and resumes at the part that failed without another model call;
after a restart the journal is queued again, and a message Telegram hands out again
(its id is not above the journal's highest id) is ignored. :meth:`close` stops the workers
with a bounded wait; what is left is answered after the next start.

:meth:`tick` (after every poll) queues the morning summary once a day from ``morning_hour``
(local time) and retries replies that could not be sent. The summary's parts and progress are
kept for the day; a part is marked sent before it goes out, so it is sent at most once.

Everything the module talks to is passed in (notifier, ``ask``, stores, status getters,
clock), so the tests use fakes. Nothing the owner writes is logged.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

from igris.mail_tools import recent_mail
from igris.notify import TELEGRAM_TEXT_LIMIT
from igris.textfold import lower_ascii

SESSION = "telegram"
MAX_WAITING = 5
TYPING_INTERVAL = 4.0
DAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
LIST_LIMIT = 25  # agenda lines per section

HELP_TEXT = (
    "Kısayollar (modelsiz, hemen cevap verir):\n"
    "/bugun – bugünün ve gecikmiş kayıtlar\n"
    "/hafta – önümüzdeki 7 gün, güne göre\n"
    "/mail – son 24 saatin mailleri\n"
    "/onaylar – açık onaylar\n"
    "/durum – Gmail, model ve takvim durumu\n"
    "/yardim – bu liste\n\n"
    "Bunların dışında ne yazarsan Igris'e gider: soru sorabilir ya da "
    "\"yarın 18:00 spor ekle\" gibi Görev Takvimi'ne kayıt ekletebilirsin."
)
BUSY_TEXT = "Sırada çok mesaj var, biraz bekle."
NON_TEXT_TEXT = "Şimdilik yalnızca yazı okuyabiliyorum."
FAIL_TEXT = "Şu an cevap veremiyorum: {reason}. Kısayollar çalışıyor: /yardim"
EMPTY_TEXT = "Boş bir cevap geldi, bir daha sorar mısın?"
JOB_TEXT = ("Bu büyük bir iş, multiagent'a verdim. İş kuyruğa alındı (iş no: {job_id}). "
            "İlerlemeyi web arayüzündeki Multiagent panelinden izleyebilirsin.")
NO_AGENDA_TEXT = "Görev Takvimi bağlı değil."
CHANNEL_NOTE = (
    "The user is writing from Telegram on the phone. Answer in plain text: no Markdown "
    "(no **, #, tables or code fences), short, in the user's language. Never put mail "
    "bodies, secrets or file contents into the answer."
)



def _local_now() -> datetime:
    return datetime.now().astimezone()


def _stderr(text: str) -> None:
    print(text, file=sys.stderr)


def split_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Cut ``text`` into parts of at most ``limit`` characters, at a paragraph break, else
    a line break, else a space; a word is cut only when it alone is longer than a part."""
    text = str(text).strip()
    parts = []
    while len(text) > limit:
        window = text[: limit + 1]  # a separator right after the limit is a clean cut too
        cut = -1
        for separator in ("\n\n", "\n", " "):
            at = window.rfind(separator, 0, limit + 1)
            if at >= limit // 2:
                cut = at
                break
        if cut < 0:
            at = window.rfind(" ", 0, limit + 1)
            cut = at if at > 0 else limit
        head = text[:cut].rstrip()
        if head:
            parts.append(head)
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def _reason(exc: Exception) -> str:
    """A short, fixed Turkish reason for a failed answer, chosen by the kind of error.

    The error's own text (CLI output, provider messages) never reaches the phone; the
    worker writes it to the log instead.
    """
    names = {klass.__name__ for klass in type(exc).__mro__}
    if "PaidUsageBlocked" in names:
        return "ücretli (API) kullanım engellendi"
    if "RateLimited" in names:
        return "model kotası doldu ya da hız sınırına takıldı"
    if "TimeoutError" in names or "timed out" in str(exc):
        return "model zamanında cevap vermedi"
    if "BackendError" in names:
        return "modele ulaşılamadı"
    return "beklenmeyen bir hata oldu"


# ---- agenda formatting ----

def _parse(value, tz):
    """(moment, timed) for an agenda date or date-time; a bare date is its day's start."""
    if not isinstance(value, str) or not value:
        return None, False
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), dtime(), tz), False
        return datetime.fromisoformat(value).astimezone(tz), True
    except ValueError:
        return None, False


def _moment(item, tz):
    """The instant an item counts from: its due date, else its start (as agenda does)."""
    return _parse(item.get("due") or item.get("start"), tz)[0]


def _day_label(day: date) -> str:
    return f"{day:%d.%m} {DAYS[day.weekday()]}"


def _clock(item, tz) -> str:
    start, start_timed = _parse(item.get("start"), tz)
    due, due_timed = _parse(item.get("due"), tz)
    if item.get("all_day") or not (start_timed or due_timed):
        return "tüm gün"
    if start_timed and due_timed and due > start:
        if due.date() != start.date():  # past midnight: both days, so the end is not misread
            return (f"{_day_label(start.date())} {start:%H:%M}–"
                    f"{_day_label(due.date())} {due:%H:%M}")
        return f"{start:%H:%M}–{due:%H:%M}"
    return f"{(start if start_timed else due):%H:%M}"


def _spans_days(item, tz) -> bool:
    start, start_timed = _parse(item.get("start"), tz)
    due, due_timed = _parse(item.get("due"), tz)
    return bool(start_timed and due_timed and due > start and due.date() != start.date())


def _item_line(item, tz, *, with_day=False) -> str:
    when = _clock(item, tz)
    if with_day and not _spans_days(item, tz):
        moment = _moment(item, tz)
        if moment is not None:
            when = f"{_day_label(moment.date())} {when}"
    tail = " (öneri)" if item.get("status") == "proposed" else ""
    return f"• {when} {item.get('title') or '-'}{tail}"


def _lines(items, tz, *, with_day=False) -> list[str]:
    lines = [_item_line(it, tz, with_day=with_day) for it in items[:LIST_LIMIT]]
    if len(items) > LIST_LIMIT:
        lines.append(f"…ve {len(items) - LIST_LIMIT} kayıt daha (web arayüzünde)")
    return lines


def _hm(stamp) -> str:
    """HH:MM of an ISO stamp, or an empty string."""
    try:
        return datetime.fromisoformat(str(stamp)).strftime("%H:%M")
    except ValueError:
        return ""


_GMAIL_STATES = {"ok": "çalışıyor", "starting": "başlıyor, ilk kontrol bekleniyor",
                 "error": "hata", "auth_down": "Google oturumu düştü (tools/gmail_auth.py login)",
                 "not_configured": "bağlı değil", "disabled": "kapalı"}
_MAIL_MODEL_STATES = {"ok": "mailleri okuyor", "down": "cevap vermiyor",
                      "none": "yok, yalnızca kurallar"}
_CHANNEL_STATES = {"ok": "çalışıyor", "failing": "mesaj gönderilemiyor", "none": "yok"}
_MODEL_STATES = {"ok": "hazır", "available": "hazır", "resting": "dinleniyor",
                 "skipped": "art arda hata verdi, atlanıyor"}
_SYNC_STATES = {"ok": "çalışıyor", "starting": "başlıyor", "needs_consent": "Google izni gerekiyor",
                "auth_down": "Google oturumu düştü", "not_configured": "Google bağlı değil",
                "no_owner": "sahip hesabı yok", "error": "hata"}


class TelegramChat:
    """The owner's side of the Telegram bot; see the module docstring."""

    def __init__(self, notifier, ask: Callable[[str], object] | None = None, *,
                 owner: Callable[[], str | None] | str | None = None,
                 agenda=None, inbox=None, approvals=None,
                 inbox_status: Callable[[], dict] | None = None,
                 calendar_status: Callable[[], dict] | None = None,
                 model_status: Callable[[], dict] | None = None,
                 now: Callable[[], datetime] = _local_now,
                 morning_hour: int | None = 8, state_path: Path | None = None,
                 typing_interval: float = TYPING_INTERVAL, max_waiting: int = MAX_WAITING,
                 log: Callable[[str], None] = _stderr):
        if morning_hour is not None and not 0 <= morning_hour <= 23:
            raise ValueError("morning_hour must be 0-23 or None")
        self.notifier, self.ask = notifier, ask
        self._owner = owner
        self.agenda, self.inbox, self.approvals = agenda, inbox, approvals
        self.inbox_status, self.calendar_status = inbox_status, calendar_status
        self.model_status = model_status
        self.now, self.morning_hour = now, morning_hour
        self.state_path = Path(state_path) if state_path is not None else None
        self.typing_interval, self.max_waiting = typing_interval, max_waiting
        # free text for the model worker; replies, shortcuts and the morning summary for the
        # sender. Both are unbounded: the waiting limit is counted in _waiting, and every
        # entry is in the journal before it is queued.
        self._chat: queue.Queue = queue.Queue()
        self._out: queue.Queue = queue.Queue()
        self._waiting = 0
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self._stopped = False
        self._restored = False
        self._morning_lock = threading.Lock()
        self._flush_queued = self._morning_queued = False
        self._state = None  # the state file's content, loaded on first use
        self._unknown_day = None  # the day the state file could not be read
        self._skip_day = None  # a day whose morning summary must not be (re)tried
        self._state_lock = threading.RLock()
        self._log, self._last_log = log, None

    # ---- incoming (the approval poll thread: no network, only the state file) ----

    def owner(self) -> str | None:
        try:
            return self._owner() if callable(self._owner) else self._owner
        except Exception:
            return None

    def handle(self, message) -> None:
        """One owner message from the poll. Returns at once: the message is written to the
        journal and queued; every reply is sent by the worker threads."""
        self._restore()
        entry = {"id": getattr(message, "message_id", None),
                 "kind": getattr(message, "kind", "text"),
                 "text": str(getattr(message, "text", "") or "").strip()}
        with self._state_lock:
            if self._stopped:
                raise RuntimeError("Telegram chat is stopped")
            state = self._get_state()
            last = state.get("last_message")
            if isinstance(entry["id"], int):
                if isinstance(last, int) and entry["id"] <= last:
                    return  # handed out again after a restart: already in the journal or done
                state["last_message"] = entry["id"]
            state.setdefault("pending", []).append(entry)
            if not self._persist("message journal"):
                state["pending"].pop()
                if last is None:
                    state.pop("last_message", None)
                else:
                    state["last_message"] = last
                raise OSError("Telegram message was not durably accepted")
        self._dispatch(entry)

    def _dispatch(self, entry, *, restored=False) -> None:
        """Free text to the model worker (at most ``max_waiting`` waiting), the rest to the
        sender."""
        text = entry["text"]
        if entry["kind"] == "text" and text and not text.startswith("/") and self.ask is not None:
            with self._guard:
                full = not restored and self._waiting >= self.max_waiting
                if not full:
                    self._waiting += 1
                    self._chat.put(entry)
            if full:
                self._reply(entry, BUSY_TEXT)
        else:
            self._out.put(("message", entry))
        self._ensure_workers()

    def _restore(self) -> None:
        """Queue again what the journal holds from before a restart (once)."""
        with self._state_lock:
            if self._stopped or self._restored:
                return
            self._restored = True
            pending = list(self._get_state().get("pending") or [])
        for entry in pending:
            if not isinstance(entry, dict):
                continue
            if "parts" in entry:
                self._out.put(("deliver", entry))
                self._ensure_workers()
            else:
                self._dispatch(entry, restored=True)

    # ---- workers ----

    def _ensure_workers(self) -> None:
        with self._guard:
            if self._stopped:
                return
            for name, target in (("chat", self._work_chat), ("out", self._work_out)):
                thread = self._threads.get(name)
                if thread is None or not thread.is_alive():
                    thread = threading.Thread(target=target, name=f"igris-telegram-{name}",
                                              daemon=True)
                    self._threads[name] = thread
                    thread.start()

    def join(self) -> None:
        """Wait until every queued message was answered and sent (tests)."""
        self._chat.join()
        self._out.join()

    def close(self, timeout: float = 5.0) -> None:
        """Stop the workers after their current item, waiting at most ``timeout`` seconds.
        What is still queued stays in the journal and is answered after the next start."""
        with self._state_lock, self._guard:
            self._stopped = True
            threads = list(self._threads.values())
        self._chat.put(None)
        self._out.put(None)
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _work_chat(self) -> None:
        while True:
            entry = self._chat.get()
            try:
                if entry is None or self._stopped:
                    return
                with self._guard:
                    self._waiting -= 1
                self._reply(entry, self._answer(entry["text"]))
            except Exception as exc:  # never end the worker; the text itself is not logged
                self._note(f"igris: telegram chat failed: {type(exc).__name__}")
            finally:
                self._chat.task_done()

    def _work_out(self) -> None:
        while True:
            item = self._out.get()
            try:
                if item is None or self._stopped:
                    return
                kind = item[0]
                if kind == "message":
                    self._shortcut_entry(item[1])
                elif kind == "deliver":
                    self._deliver_entry(item[1])
                elif kind == "flush":
                    self._flush_queued = False
                    for entry in self._undelivered():
                        self._deliver_entry(entry)
                elif kind == "morning":
                    self._morning_queued = False
                    self.run_morning()
            except Exception as exc:
                self._note(f"igris: telegram send failed: {type(exc).__name__}")
            finally:
                self._out.task_done()

    def _shortcut_entry(self, entry) -> None:
        """Answer a message that needs no model: a shortcut, a non-text message, or free
        text while no model is connected."""
        text = entry["text"]
        if entry["kind"] != "text":
            out = NON_TEXT_TEXT
        elif not text:
            self._finish(entry)
            return
        elif text.startswith("/"):
            out = self.shortcut(text)
        else:
            out = FAIL_TEXT.format(reason="model bağlı değil")
        self._reply(entry, out, deliver_now=True)

    def _typing(self, done: threading.Event) -> None:
        action = getattr(self.notifier, "send_chat_action", None)
        if action is None:
            return
        while True:
            try:
                action("typing")
            except Exception:
                pass  # only a hint on the phone
            if done.wait(self.typing_interval):
                return

    def _answer(self, text: str) -> str:
        """The reply to free text; an error becomes a fixed Turkish sentence and its detail
        goes to the log only."""
        done = threading.Event()
        typing = threading.Thread(target=self._typing, args=(done,), daemon=True)
        typing.start()
        try:
            reply = self.ask(text)
        except Exception as exc:
            detail = " ".join(str(exc).split())[:300]
            self._note(f"igris: telegram chat model error: {type(exc).__name__}: {detail}")
            out = FAIL_TEXT.format(reason=_reason(exc))
        else:
            job_id = getattr(reply, "job_id", None)
            body = str(getattr(reply, "text", reply) or "").strip()
            out = JOB_TEXT.format(job_id=job_id) if job_id else (body or EMPTY_TEXT)
        finally:
            done.set()
        typing.join(timeout=5)
        return out

    # ---- outgoing (worker threads only) ----

    def _note(self, text: str) -> None:
        if text != self._last_log:
            self._log(text)
        self._last_log = text

    def _reply(self, entry, text: str, *, deliver_now=False) -> None:
        """Keep the reply with its message in the journal, then send it. A reply that could
        not be sent is tried again from the part that failed (``tick`` queues a retry),
        without another model call once the reply is durably saved."""
        with self._state_lock:
            if self._stopped:
                return  # a model call that outlived close must not overwrite restart state
            entry["parts"] = split_message(text) or [EMPTY_TEXT]
            entry["sent"] = 0
            self._persist("message journal")
        if deliver_now:
            self._deliver_entry(entry)
        else:
            self._out.put(("deliver", entry))
            self._ensure_workers()

    def _deliver_entry(self, entry) -> bool:
        parts = entry.get("parts") or []
        while entry.get("sent", 0) < len(parts):
            if self._stopped:
                return False
            try:
                self.notifier.send(parts[entry["sent"]])
            except Exception as exc:  # NotifyError text is already free of the bot token
                self._note(f"igris: telegram reply failed, will retry: {exc}")
                return False
            with self._state_lock:
                entry["sent"] = entry.get("sent", 0) + 1
                self._persist("message journal")
        self._finish(entry)
        return True

    def _finish(self, entry) -> None:
        with self._state_lock:
            if self._stopped:
                return
            pending = self._get_state().get("pending") or []
            self._get_state()["pending"] = [e for e in pending if e is not entry]
            self._persist("message journal")

    def _undelivered(self) -> list[dict]:
        with self._state_lock:
            return [e for e in self._get_state().get("pending") or []
                    if isinstance(e, dict) and "parts" in e]

    # ---- shortcuts ----

    def shortcut(self, text: str) -> str:
        """The answer to a slash command, read from the stores without a model call."""
        command = lower_ascii(text.split()[0][1:].split("@")[0])
        handlers = {"yardim": self.help_text, "start": self.help_text, "help": self.help_text,
                    "bugun": self.today_text, "hafta": self.week_text, "mail": self.mail_text,
                    "onaylar": self.approvals_text, "onay": self.approvals_text,
                    "durum": self.status_text}
        handler = handlers.get(command, self.help_text)
        try:
            return handler()
        except Exception as exc:
            return f"Bu kısayol şu an çalışmadı ({type(exc).__name__}). Web arayüzüne bak."

    def help_text(self) -> str:
        return HELP_TEXT

    def _sections(self, now):
        owner = self.owner()
        if self.agenda is None or not owner:
            return None
        return self.agenda.sections(owner, None, now)

    def today_text(self) -> str:
        now = self.now()
        sections = self._sections(now)
        if sections is None:
            return NO_AGENDA_TEXT
        lines = [f"Bugün ({_day_label(now.date())}):"]
        lines += _lines(sections["today"], now.tzinfo) or ["Bugün için kayıt yok."]
        if sections["overdue"]:
            lines += ["", "Gecikenler:"] + _lines(sections["overdue"], now.tzinfo, with_day=True)
        return "\n".join(lines)

    def _days_ahead(self, now, first: int, last: int) -> list[dict]:
        """Open items whose moment falls from day ``first`` to day ``last`` after today."""
        owner = self.owner()
        tz = now.tzinfo
        start = datetime.combine(now.date() + timedelta(days=first), dtime(), tz)
        end = datetime.combine(now.date() + timedelta(days=last + 1), dtime(), tz)
        items = [it for it in self.agenda.list(owner)
                 if (m := _moment(it, tz)) is not None and start <= m < end]
        items.sort(key=lambda it: _moment(it, tz))
        return items

    def week_text(self) -> str:
        now = self.now()
        if self.agenda is None or not self.owner():
            return NO_AGENDA_TEXT
        items = self._days_ahead(now, 0, 6)
        if not items:
            return "Önümüzdeki 7 günde kayıt yok."
        tz = now.tzinfo
        lines = ["Önümüzdeki 7 gün:"]
        day = None
        for item in items[:LIST_LIMIT * 2]:
            moment_day = _moment(item, tz).date()
            if moment_day != day:
                day = moment_day
                label = "Bugün" if day == now.date() else DAYS[day.weekday()]
                lines += ["", f"{label} {day:%d.%m}:"]
            lines.append(_item_line(item, tz))
        if len(items) > LIST_LIMIT * 2:
            lines.append(f"…ve {len(items) - LIST_LIMIT * 2} kayıt daha (web arayüzünde)")
        return "\n".join(lines)

    def mail_text(self) -> str:
        if self.inbox is None:
            return "Mail takibi kapalı."
        from igris.mail_tools import mail_text

        return mail_text(recent_mail(self.inbox, 24, now=self.now))

    def _pending(self) -> list[dict]:
        owner = self.owner()
        if self.approvals is None or not owner:
            return []
        return list(self.approvals.pending(owner))

    def approvals_text(self) -> str:
        if self.approvals is None:
            return "Onay sistemi kapalı."
        items = self._pending()
        if not items:
            return "Açık onay yok."
        lines = [f"Açık onaylar ({len(items)}):"]
        lines += [f"• {' '.join(str(it.get('summary') or '-').split())}" for it in items[:LIST_LIMIT]]
        lines.append("Cevaplamak için onay mesajındaki düğmelere bas ya da web arayüzünü aç.")
        return "\n".join(lines)

    @staticmethod
    def _read(getter):
        if getter is None:
            return None
        try:
            return getter() or {}
        except Exception as exc:
            return {"state": "error", "error": type(exc).__name__}

    def status_text(self) -> str:
        lines = []
        gmail = self._read(self.inbox_status)
        if gmail is None:
            lines.append("Gmail: kapalı")
        else:
            state = gmail.get("state")
            line = f"Gmail: {_GMAIL_STATES.get(state, state or '-')}"
            if gmail.get("last_poll") and _hm(gmail["last_poll"]):
                line += f" (son kontrol {_hm(gmail['last_poll'])})"
            if gmail.get("error"):
                line += f": {' '.join(str(gmail['error']).split())[:150]}"
            lines.append(line)
            if "model" in gmail:
                lines.append("Mail modeli: " + _MAIL_MODEL_STATES.get(gmail["model"],
                                                                     str(gmail["model"])))
            if "channel" in gmail:
                lines.append("Bildirim kanalı: " + _CHANNEL_STATES.get(gmail["channel"],
                                                                       str(gmail["channel"])))
            if gmail.get("pending_alerts") is not None:
                lines.append(f"Bekleyen uyarı: {gmail['pending_alerts']}")
        models = self._read(self.model_status)
        if models:
            if "state" in models and "error" in models:
                lines.append("Model: okunamadı")
            else:
                lines.append("Model: " + ", ".join(
                    f"{name} {_MODEL_STATES.get(info.get('state'), info.get('state') or '-')}"
                    for name, info in models.items() if isinstance(info, dict)))
        sync = self._read(self.calendar_status)
        if sync is None:
            lines.append("Takvim eşitleme: kapalı")
        else:
            state = sync.get("state")
            line = f"Takvim eşitleme: {_SYNC_STATES.get(state, state or '-')}"
            if sync.get("last_sync") and _hm(sync["last_sync"]):
                line += f" (son {_hm(sync['last_sync'])})"
            if sync.get("error"):
                line += f": {' '.join(str(sync['error']).split())[:150]}"
            lines.append(line)
        lines.append(f"Açık onay: {len(self._pending())}" if self.approvals is not None
                     else "Onay sistemi: kapalı")
        return "\n".join(lines)

    # ---- morning summary text ----

    def morning_text(self, now: datetime | None = None) -> str:
        """Today's items, overdue items, the next 7 days, open approvals and mail tasks."""
        now = now or self.now()
        tz = now.tzinfo
        lines = [f"Günaydın! Bugün {now:%d.%m.%Y} {DAYS[now.weekday()]}."]
        sections = self._sections(now)
        if sections is None:
            lines += ["", NO_AGENDA_TEXT]
        else:
            lines += ["", "Bugün:"] + (_lines(sections["today"], tz) or ["Bugün için kayıt yok."])
            if sections["overdue"]:
                lines += ["", "Gecikenler:"] + _lines(sections["overdue"], tz, with_day=True)
            ahead = self._days_ahead(now, 1, 7)
            lines += ["", "Önümüzdeki 7 gün:"]
            lines += _lines(ahead, tz, with_day=True) or ["Önümüzdeki 7 günde kayıt yok."]
        if self.approvals is not None:
            lines += ["", f"Açık onay: {len(self._pending())}"]
        if self.inbox is not None:
            tasks = recent_mail(self.inbox, 24, now=lambda: now)["gorev"]
            if tasks:
                lines += ["", "Son 24 saatte maildeki görevler:"]
                for row in tasks[:LIST_LIMIT]:
                    line = f"• {row['task'] or row['subject'] or '-'} ({row['sender'] or '-'})"
                    if row["due"]:
                        line += f", son tarih {row['due']}"
                    lines.append(line)
            else:
                lines += ["", "Son 24 saatte mailde görev yok."]
        return "\n".join(lines)

    # ---- persisted state: the message journal and the morning summary's progress ----

    def _get_state(self) -> dict:
        """The state file, read once; kept in memory also when it cannot be written.

        A missing file is a first start. A file that exists but cannot be read leaves the
        journal empty and, to stay at most once, skips that day's morning summary.
        """
        with self._state_lock:
            if self._state is None:
                data = {}
                if self.state_path is not None:
                    try:
                        data = json.loads(self.state_path.read_text(encoding="utf-8"))
                        if not isinstance(data, dict):
                            raise ValueError("not an object")
                    except FileNotFoundError:
                        data = {}
                    except (OSError, ValueError) as exc:
                        data = {}
                        try:
                            self._unknown_day = self.now().date().isoformat()
                        except Exception:
                            self._unknown_day = date.today().isoformat()
                        self._note(f"igris: telegram chat state unreadable ({type(exc).__name__});"
                                   " today's morning summary is skipped")
                self._state = data
            return self._state

    def _persist(self, what: str) -> bool:
        """Write the state file atomically; False (and one log line) when it fails."""
        with self._state_lock:
            if self.state_path is None:
                return True
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.state_path.with_name(self.state_path.name + ".tmp")
                temp.write_text(json.dumps(self._get_state(), ensure_ascii=False),
                                encoding="utf-8")
                os.replace(temp, self.state_path)
            except (OSError, TypeError, ValueError) as exc:
                self._note(f"igris: telegram chat state not saved ({what}): {type(exc).__name__}")
                return False
            return True

    # ---- morning summary ----

    def _morning_done(self, today: str) -> bool:
        state = self._get_state()
        if today in (self._skip_day, self._unknown_day):
            return True
        if state.get("morning_sent") == today:  # upgrade from the original day marker
            return True
        morning = state.get("morning")
        return (isinstance(morning, dict) and morning.get("day") == today
                and morning.get("next", 0) >= len(morning.get("parts") or []))

    def _morning_due(self, now: datetime) -> bool:
        return (self.morning_hour is not None and now.hour >= self.morning_hour
                and not self._morning_done(now.date().isoformat()))

    def tick(self) -> bool:
        """Called after every poll (the approval thread): queue the morning summary when it
        is due and a retry of replies that could not be sent. Never sends itself.
        Returns True when the morning summary was queued."""
        self._restore()
        if self._stopped:
            return False
        if not self._flush_queued and self._undelivered():
            self._flush_queued = True
            self._out.put(("flush",))
            self._ensure_workers()
        if self._morning_queued or not self._morning_due(self.now()):
            return False
        self._morning_queued = True
        self._out.put(("morning",))
        self._ensure_workers()
        return True

    def run_morning(self) -> bool:
        """Send a due digest on the sender; concurrent callers leave it to the active send."""
        if not self._morning_lock.acquire(blocking=False):
            return False
        try:
            return self._run_morning()
        finally:
            self._morning_lock.release()

    def _run_morning(self) -> bool:
        """Send today's morning summary when it is due (the sender thread); True when its
        last part was delivered now.

        Due once the local hour reaches ``morning_hour`` on a day it was not delivered, so a
        server started later that day still sends it. Its parts are kept for the day; each
        part is claimed in the state file before it goes out. Claims remain on errors:
        Telegram may have accepted a timed-out send. The next tick resumes with the next
        unclaimed part, choosing possible omission over duplicate delivery.
        When the state cannot be written the day is given up (at most once per day).
        """
        now = self.now()
        if self._stopped or not self._morning_due(now):
            return False
        today = now.date().isoformat()
        state = self._get_state()
        morning = state.get("morning")
        if not (isinstance(morning, dict) and morning.get("day") == today):
            try:
                parts = split_message(self.morning_text(now))
            except Exception as exc:  # a locked store: try again on the next tick
                self._note(f"igris: morning summary not built: {type(exc).__name__}")
                return False
            # Store reads may block; only hold the journal lock for state operations.
            with self._state_lock:
                if self._stopped:
                    return False
                morning = state["morning"] = {"day": today, "parts": parts, "next": 0}
                if not self._persist("morning summary"):
                    return self._give_up(today)
        while morning["next"] < len(morning["parts"]):
            index = morning["next"]
            with self._state_lock:
                if self._stopped:
                    return False
                morning["next"] = index + 1  # claimed before it goes out
                if not self._persist("morning summary"):
                    morning["next"] = index
                    return self._give_up(today)
            try:
                self.notifier.send(morning["parts"][index])
            except Exception as exc:  # NotifyError text is already free of the bot token
                # Telegram may have accepted the part before a timeout. Keep its durable
                # claim even on error: digests favor omission over duplicate delivery.
                self._note(f"igris: morning summary delivery uncertain; part not retried: {exc}")
                return False
        return True

    def _give_up(self, today: str) -> bool:
        self._skip_day = today
        self._note("igris: morning summary skipped today: its progress cannot be saved")
        return False
