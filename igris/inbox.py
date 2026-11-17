"""Inbox watcher: reads new Gmail mail, decides what each one means, tells the owner what matters.

Every poll lists the ids of recently received mail (inbox or not: a Gmail filter or the
owner may archive a mail before the poll) and fetches only unseen ones. Each mail is checked
(``mailcheck``), sorted by the header rules (``mailtriage``) and then decided
(``mail_decide``): it gets a category (görev, güvenlik, bilgi, rutin, tanıtım) and a one-line
reason. One short row per mail is stored (sender, subject, verdict, category; never the
body). Only two things reach the phone at once: a security problem, and a task the owner
must do (a mandatory, confident ``gorev``, or a personal mail with a task word in its subject
that the model did not call a task: "Görev olabilir: model emin değil"; every such doubted
mail also becomes an admin panel notice "Kontrol et"). A dated task also becomes a Görev Takvimi item.
Everything else is only shown in the web interface. The old daily digest of "important"
mail is off unless ``digest=True``.

The very first poll records what is already in the inbox without sending anything and
without asking a model, so connecting Gmail does not flood the phone. A dead sign-in sends
one alarm and pauses polling until ``tools/gmail_auth.py login`` clears the flag.

The path fails loudly. When the model cannot read a mail, a task word in it still reaches
the phone ("Görev olabilir") and the mail is read again on up to three later polls; three
failed model reads in a row, or three failed polls in a row, tell the owner once per outage
(phone and admin panel). ``status()`` also says whether a notification channel works, how
many alerts wait and whether the model reads mail. The query window reaches back to the
last complete poll (2 to 30 days), so mail that came while the desktop was off is read too.
"""

from __future__ import annotations

import html
import json
import math
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from email.utils import parseaddr
from pathlib import Path
from typing import Callable

from igris import gmail, mail_decide, mailcheck, mailtriage
from igris.mail_compose import normalize_subject

# All mail received in the window, not only the inbox: a filter that skips the inbox or a mail
# archived on the phone before the poll is still read. Sent mail, drafts and chats are not
# received mail (Gmail search leaves out spam and trash by itself). A mail the owner sends to
# themself is in sent too and so is left out; that is accepted.
RECEIVED = "-in:sent -in:drafts -in:chats"
QUERY = f"newer_than:2d {RECEIVED}"
MAX_PER_POLL = 50
MAX_CATCH_UP = gmail.MAX_LIST  # ids (over several pages) when the window reaches further back
MIN_WINDOW_DAYS, MAX_WINDOW_DAYS = 2, 30
MAX_MODEL_PER_POLL = 8  # the rest wait for the next poll; a model read takes seconds
REREAD_ATTEMPTS = 3  # later model reads of a mail the model could not read
MODEL_DOWN_AFTER = 3  # failed model reads in a row before the owner is told
POLL_DOWN_AFTER = 3  # failed polls in a row before the owner is told
MODEL_DOWN_TEXT = ("Igris mailleri okuyamıyor: model cevap vermiyor (Claude kotası ya da CLI "
                   "hatası olabilir). Görev kelimesi geçen mailleri sana doğrudan haber vereceğim.")
GMAIL_DOWN_TEXT = "Igris Gmail'i okuyamıyor: {error}"
BACKLOG_TEXT = ("Çok fazla mail birikti, eskileri okunamadı: Igris en yeni {n} maili okudu. "
                "Daha eskilerine Gmail'de kendin bak.").format(n=gmail.MAX_LIST)
DIGEST_LINES = 20
_FIELD_LIMIT = 200
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TAG_RE = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_PUBLIC = ("id", "seen_at", "sender", "subject", "label", "risk", "flags",
           "spf", "dkim", "dmarc", "category", "reason", "task", "origin")
_FLAG_TEXT = {
    "auth_failed": "kimlik doğrulaması başarısız",
    "reply_to_mismatch": "yanıt adresi başka bir alan adına gidiyor",
    "lookalike_domain": "bilinen bir alan adına benzeyen adres",
    "display_name_spoof": "görünen ad başka bir kurumu taklit ediyor",
    "link_mismatch": "link metni ile gittiği adres farklı",
    "unreadable": "mail okunamadı",
}


def load_rules_file(path: Path) -> mailtriage.TriageRules:
    """Triage rules from a JSON file; a missing file means no rules."""
    path = Path(path)
    if not path.is_file():
        return mailtriage.TriageRules()
    return mailtriage.load_rules(json.loads(path.read_text(encoding="utf-8")))


def _clean(value: object) -> str:
    text = " ".join(_CONTROL_RE.sub(" ", str(value or "")).split())
    return text[:_FIELD_LIMIT]


_DOMAIN_RE = re.compile(r"@([a-z0-9.-]+\.[a-z]{2,})", re.IGNORECASE)
# where a forwarded school mail still names the school mailbox it came through
_FORWARD_HEADERS = ("to", "cc", "delivered-to", "x-forwarded-to", "x-forwarded-for",
                    "x-original-to", "resent-from", "resent-to", "resent-sender")
_ORIGINAL_FROM_RE = re.compile(r"^\s*\**(?:from|kimden|gönderen)\s*:\**\s*(.+?)\s*$",
                               re.IGNORECASE | re.MULTILINE)
_FORWARD_MARK_RE = re.compile(r"forwarded message|original message|iletilen ileti|özgün ileti"
                              r"|^\s*fw[d]?\s*:|^\s*ilt\s*:", re.IGNORECASE | re.MULTILINE)
_COMMENT_RE = re.compile(r"\([^()]*\)")
# Microsoft 365 seals the mail an Outlook rule forwards; the school's own domains may too
_TRUSTED_SEALERS = ("microsoft.com",)


def _is_school(domain: str, school_domains: tuple[str, ...]) -> bool:
    domain = domain.lower().rstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in school_domains)


def _domain_of(address: str) -> str:
    match = _DOMAIN_RE.search(address or "")
    return match.group(1).lower() if match else ""


def school_forward(message, school_domains: tuple[str, ...]) -> bool:
    """Whether a mail came from the owner's school mailbox (an Outlook forward) or a school
    sender: a school domain in the From, in the recipient and forward headers, or in an
    Exchange header the school's Microsoft 365 adds on the way out."""
    if not school_domains:
        return False
    names = []
    for name, value in message.items():
        lowered = name.lower()
        if lowered == "from" or lowered in _FORWARD_HEADERS or lowered.startswith("x-ms-exchange-"):
            names.append(str(value))
    return any(_is_school(d.lower(), school_domains)
               for value in names for d in _DOMAIN_RE.findall(value))


def _entries(value) -> list[str]:
    """The ";"-separated entries of an authentication header, comments removed."""
    text = str(value)
    for _ in range(3):  # comments nest rarely and shallowly
        text = _COMMENT_RE.sub(" ", text)
    return [" ".join(e.split()).lower() for e in text.split(";")]


def _arc_set(message, name: str, instance: str = "1") -> list[str] | None:
    """The entries of the ARC header ``name`` of one instance (i=1 is the first hop)."""
    for value in message.get_all(name) or ():
        entries = _entries(value)
        if entries and entries[0] == "i=" + instance:
            return entries
    return None


def forward_authenticated(message, verdict: mailcheck.MailVerdict,
                          school_domains: tuple[str, ...]) -> bool:
    """A forward hop that cannot be faked by the sender.

    Either the school domain itself (an Outlook rule that rewrites From) passed Gmail's DMARC,
    or all of these hold: Gmail's own verdict (the topmost Authentication-Results, which Gmail
    adds above anything the sender wrote) says arc=pass, the first ARC hop was sealed by the
    school's Microsoft 365 or the school's own domain, and that hop recorded a DMARC pass for
    the original sender. Headers further down, To/X-Forwarded-To and a self-made ARC chain
    are all written by the sender, so none of them counts alone.
    """
    if _is_school(verdict.from_domain, school_domains) and verdict.dmarc == "pass":
        return True
    results = message.get_all("Authentication-Results") or ()
    if not results:
        return False
    gmail = _entries(results[0])
    if not gmail or gmail[0].split()[:1] != ["mx.google.com"] or "arc=pass" not in {
            e.split()[0] for e in gmail[1:] if e}:
        return False
    seal = _arc_set(message, "ARC-Seal")
    first_hop = _arc_set(message, "ARC-Authentication-Results")
    if seal is None or first_hop is None:
        return False
    sealer = next((e[2:] for e in seal if e.startswith("d=")), "")
    passed = [e.split() for e in first_hop[1:] if e and e.split()[0] == "dmarc=pass"]
    if _is_school(sealer, school_domains):  # the school's own seal: any sender it vouched for
        return bool(passed)
    if sealer not in _TRUSTED_SEALERS:
        return False
    # every Microsoft 365 tenant seals as microsoft.com, an attacker's too; so that seal counts
    # only when the hop verified a school sender, which no other tenant can make pass DMARC
    return any(_is_school(word[len("header.from="):], school_domains)
               for words in passed for word in words if word.startswith("header.from="))


def forward_risk(verdict: mailcheck.MailVerdict, risk: str) -> str:
    """The risk of an authenticated school forward: the forward itself (a failed SPF or DMARC
    on the forwarding hop, a Reply-To that differs from a rewritten From) raises nothing;
    every sign in the content still counts."""
    rest = [f for f in verdict.flags if f not in ("auth_failed", "reply_to_mismatch")]
    if risk == "high" and not set(rest) & {"lookalike_domain", "display_name_spoof",
                                          "link_mismatch"}:
        risk = "medium"
    if risk == "medium" and not rest:
        risk = "low"
    return risk


def original_sender(message, body: str, school_domains: tuple[str, ...]) -> str | None:
    """The real sender of a forward whose From was rewritten to the owner's school address:
    the original From quoted in the body, else the Reply-To."""
    own = parseaddr(str(message.get("From", "")))[1].lower()
    if not _is_school(_domain_of(own), school_domains):
        return None

    def other(value: str) -> bool:
        address = parseaddr(value)[1].lower()
        return bool(_domain_of(address)) and address != own

    if _FORWARD_MARK_RE.search(body[:3000]) or _FORWARD_MARK_RE.search(str(message.get("Subject", ""))):
        for match in _ORIGINAL_FROM_RE.finditer(body[:3000]):
            if other(match.group(1)):
                return match.group(1)
    reply_to = str(message.get("Reply-To", "") or "")
    return reply_to if other(reply_to) else None


def match_followup(message, body: str, *, open_keys: set[str], school_domains,
                   thread_id=None, owner_addresses=(), labels=()) -> str | None:
    """Match a received reply using API metadata or normalized subject and sender.

    SENT labels and owner addresses exclude self-mail; an Outlook forward uses its
    original sender before that check. Never trust a thread id from a mail header.
    """
    if 'SENT' in labels or 'DRAFT' in labels:
        return None
    sender = (original_sender(message, body, school_domains)
              if school_forward(message, school_domains) else None)
    sender = parseaddr(sender or str(message.get('From', '')))[1].casefold()
    own = {address.casefold() for address in owner_addresses if address}
    own.add(parseaddr(str(message.get('Delivered-To', '')))[1].casefold())
    if not sender or sender in own:
        return None
    if thread_id and thread_id in open_keys:
        return thread_id
    key = 'subj:' + normalize_subject(str(message.get('Subject', ''))) + '|' + sender
    return key if key in open_keys else None


def plain_body(message) -> str:
    """The readable text of a mail: the plain part, else the HTML part without tags."""
    try:
        part = message.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        data = part.get_payload(decode=True) or b""
        try:
            text = data.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:  # an unknown charset name
            text = data.decode("utf-8", errors="replace")
        if part.get_content_subtype() == "html":
            text = html.unescape(_TAG_RE.sub(" ", text))
    except Exception:
        return ""
    lines = (" ".join(line.split()) for line in str(text).splitlines())
    return "\n".join(line for line in lines if line)


class InboxStore:
    """SQLite rows for seen mail, pending alerts and the digest day."""

    def __init__(self, path: Path, keep: int = 2000):
        self.path = Path(path)
        self.keep = keep
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS mails (
                id TEXT PRIMARY KEY, seen_at TEXT NOT NULL, sender TEXT, subject TEXT,
                label TEXT, risk TEXT, flags TEXT, spf TEXT, dkim TEXT, dmarc TEXT,
                pending_alert INTEGER NOT NULL DEFAULT 0,
                digested INTEGER NOT NULL DEFAULT 1,
                category TEXT, reason TEXT, task TEXT, origin TEXT,
                reread INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS pending_notices "
                       "(ref TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            # databases from before the decision step gain its columns here
            existing = {row[1] for row in db.execute("PRAGMA table_info(mails)")}
            for name in ("category", "reason", "task", "origin"):
                if name not in existing:
                    db.execute(f"ALTER TABLE mails ADD COLUMN {name} TEXT")
            # model re-reads left for a mail the model could not read; 0 = none
            if "reread" not in existing:
                db.execute("ALTER TABLE mails ADD COLUMN reread INTEGER NOT NULL DEFAULT 0")
            # the word a doubted model answer is doubted for (mail_decide.Decision.doubt), so
            # the alert says "model emin değil" and not "model okuyamadı"; NULL = not doubted
            if "doubt" not in existing:
                db.execute("ALTER TABLE mails ADD COLUMN doubt TEXT")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _run(self, sql: str, params=(), *, notice: dict | None = None) -> list[sqlite3.Row]:
        with self._lock:
            db = self._connect()
            try:
                with db:
                    if notice is not None:
                        db.execute("INSERT OR IGNORE INTO pending_notices (ref, payload) "
                                   "VALUES (?, ?)",
                                   (notice["ref"], json.dumps(notice, ensure_ascii=False)))
                    return db.execute(sql, params).fetchall()
            finally:
                db.close()

    def get_meta(self, key: str) -> str | None:
        rows = self._run("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._run("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def known(self, ids: list[str]) -> set[str]:
        found: set[str] = set()
        for start in range(0, len(ids), 500):  # older SQLite allows 999 variables per query
            chunk = list(ids[start:start + 500])
            marks = ",".join("?" * len(chunk))
            found |= {r["id"] for r in self._run(
                f"SELECT id FROM mails WHERE id IN ({marks})", chunk)}
        return found

    def add(self, row: dict, *, notify: bool, notice: dict | None = None) -> None:
        """Store one mail. With ``notify`` a mail the decision says to tell (``alert``; a high
        risk when no decision ran) waits for an alert, and an important one for the digest;
        without it (the baseline) nothing is sent."""
        alert = int(notify and row.get("alert", row["risk"] == "high"))
        digested = int(not (notify and row["label"] == "important" and row["risk"] != "high"))
        task = row.get("task")
        self._run(
            "INSERT OR IGNORE INTO mails (id, seen_at, sender, subject, label, risk, flags, "
            "spf, dkim, dmarc, category, reason, task, origin, pending_alert, digested, reread, "
            "doubt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row[k] for k in _PUBLIC[:10])
            + (row.get("category"), row.get("reason"),
               json.dumps(task, ensure_ascii=False) if task else None, row.get("origin") or "",
               alert, digested, int(notify and row.get("reread", 0)), row.get("doubt")),
            notice=notice)

    def pending_notices(self) -> list[dict]:
        """Durable notice payloads, independent of mail rows and model re-reads."""
        return [json.loads(r["payload"]) for r in self._run(
            "SELECT payload FROM pending_notices ORDER BY rowid")]

    def mark_noticed(self, ref: str) -> None:
        self._run("DELETE FROM pending_notices WHERE ref = ?", (ref,))

    def prune(self, now: str) -> None:
        """Drop old rows; the watcher calls it once per poll (``now``: the poll's ISO stamp).

        Pruning never makes a mail new again and never drops something still owed. A row goes
        only when it is beyond the newest ``keep``, has no undelivered alert and no re-read
        mark, and was seen before any query window can reach: a mail is received before it
        is seen, so a row seen more than MAX_WINDOW_DAYS + 1 days ago cannot be listed again
        (a pruned id still listed would be read, decided and alerted a second time). The rule
        trusts the local clock: a clock set far ahead may prune a row Gmail can still list."""
        try:
            horizon = (datetime.fromisoformat(now)
                       - timedelta(days=MAX_WINDOW_DAYS + 1)).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            return  # no usable clock: keep everything rather than guess
        self._run("DELETE FROM mails WHERE pending_alert = 0 AND reread = 0 AND seen_at < ? "
                  "AND id NOT IN (SELECT id FROM mails ORDER BY seen_at DESC, rowid DESC LIMIT ?)",
                  (horizon, self.keep))

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            item["task"] = json.loads(item["task"]) if item.get("task") else None
        except ValueError:
            item["task"] = None
        return item

    def pending_alerts(self) -> list[dict]:
        return [self._public(r) for r in self._run(
            "SELECT * FROM mails WHERE pending_alert = 1 ORDER BY seen_at, rowid")]

    def mark_alerted(self, message_id: str) -> None:
        self._run("UPDATE mails SET pending_alert = 0 WHERE id = ?", (message_id,))

    def pending_count(self) -> int:
        return self._run("SELECT COUNT(*) AS n FROM mails WHERE pending_alert = 1")[0]["n"]

    def rereads(self) -> list[dict]:
        """Mails waiting for another model read, newest first."""
        return [self._public(r) for r in self._run(
            "SELECT id, category, pending_alert, reread FROM mails WHERE reread > 0 "
            "ORDER BY seen_at DESC, rowid DESC")]

    def set_reread(self, message_id: str, left: int, *, notice: dict | None = None) -> None:
        self._run("UPDATE mails SET reread = ? WHERE id = ?", (max(0, left), message_id),
                  notice=notice)

    def update_decision(self, message_id: str, row: dict, *, pending: bool,
                        notice: dict | None = None) -> None:
        """Commit a later decision and its notice together; the re-read mark goes."""
        task = row.get("task")
        self._run("UPDATE mails SET category = ?, reason = ?, task = ?, pending_alert = ?, "
                  "doubt = ?, reread = 0 WHERE id = ?",
                  (row.get("category"), row.get("reason"),
                   json.dumps(task, ensure_ascii=False) if task else None, int(pending),
                   row.get("doubt"), message_id), notice=notice)

    def undigested(self) -> list[dict]:
        return [dict(r) for r in self._run(
            "SELECT * FROM mails WHERE digested = 0 ORDER BY seen_at, rowid")]

    def mark_digested(self, ids: list[str]) -> None:
        for message_id in ids:
            self._run("UPDATE mails SET digested = 1 WHERE id = ?", (message_id,))

    def recent(self, limit: int = 50) -> list[dict]:
        cols = ", ".join(_PUBLIC)
        return [self._public(r) for r in self._run(
            f"SELECT {cols} FROM mails ORDER BY seen_at DESC, rowid DESC LIMIT ?", (limit,))]


def alert_risk(verdict: mailcheck.MailVerdict) -> str:
    """The risk the owner is told about.

    Newsletters wrap every link in a tracking redirect, so a link mismatch is common in
    mail whose sender DMARC has proven. Alone and with DMARC passing it is "medium";
    with any other sign, or without a DMARC pass, mailcheck's "high" stands.
    """
    if verdict.risk == "high" and verdict.dmarc == "pass" and verdict.flags == ("link_mismatch",):
        return "medium"
    return verdict.risk


def _when_text(value: str | None) -> str:
    if not value:
        return "-"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if len(value) == 10:
        return moment.strftime("%d.%m.%Y")
    return moment.strftime("%d.%m.%Y %H:%M")


def alert_text(row: dict) -> str:
    """The phone message for a mail the decision says to tell."""
    head = f"Kimden: {row['sender'] or '-'}\nKonu: {row['subject'] or '-'}"
    task = row.get("task")
    if row.get("category") == "gorev" and task:
        return ("📌 Yapman gereken bir iş var.\n"
                f"{task.get('title') or row['subject'] or '-'}\n"
                f"Son tarih: {_when_text(task.get('due') or task.get('start'))}\n{head}\n"
                f"Neden: {row.get('reason') or '-'}")
    if row.get("category") == "gorev" and not row.get("doubt"):  # the model could not read it
        return ("📌 Görev olabilir, model okuyamadı. Kendin bak.\n"
                f"{head}\nNeden: {row.get('reason') or '-'}")
    if row.get("doubt") or row.get("category") in ("bilgi", "rutin", "tanitim"):
        # a doubted answer: the model read the mail but the owner should check it
        return ("📌 Görev olabilir: model emin değil. Kendin bak.\n"
                f"{head}\nNeden: {row.get('reason') or '-'}")
    if row["risk"] != "high" and row.get("category") == "guvenlik":
        return f"🔐 Hesap güvenliği uyarısı.\n{head}\nNeden: {row.get('reason') or '-'}"
    reasons = [_FLAG_TEXT.get(f, f) for f in row["flags"].split(",") if f]
    if row["dmarc"] == "fail":
        reasons.insert(0, "DMARC başarısız")
    return ("⚠️ Riskli mail geldi, açmadan önce dikkat et.\n" + head + "\n"
            f"Neden: {'; '.join(reasons) or 'güvenlik kontrolü yüksek risk dedi'}")


def digest_text(rows: list[dict]) -> str:
    lines = [f"📬 Günün önemli mailleri ({len(rows)})"]
    lines += [f"• {r['sender'] or '-'}: {r['subject'] or '-'}" for r in rows[:DIGEST_LINES]]
    if len(rows) > DIGEST_LINES:
        lines.append(f"…ve {len(rows) - DIGEST_LINES} mail daha (web arayüzünde)")
    return "\n".join(lines)


class InboxWatcher:
    """One ``poll()`` per interval, called from the web server's background thread.

    ``model(system, prompt) -> str`` reads mail the rules cannot sort; it must be a
    private model (the owner's Claude subscription). Without it the rules decide alone.
    """

    def __init__(self, client_factory: Callable[[], object | None], store: InboxStore,
                 send: Callable[[str], None] | None, alarm: gmail.AuthAlarm, *,
                 rules: mailtriage.TriageRules = mailtriage.TriageRules(),
                 known_domains: tuple[str, ...] = (),
                 now: Callable[[], datetime] = datetime.now, digest_hour: int = 19,
                 digest: bool = False,
                 model: Callable[[str, str], str] | None = None,
                 school_domains: tuple[str, ...] = (),
                 owner_context: str = "",
                 notices=None, agenda=None, followups=None, people=None, owner_addresses=(),
                 notice_owner: str | Callable[[], str | None] | None = None):
        if not 0 <= digest_hour <= 23:
            raise ValueError("digest_hour must be 0-23")
        self._factory = client_factory
        self.client_factory = client_factory
        self._client = None
        self.store = store
        self._send = send
        self.alarm = alarm
        self.rules = rules
        self.known_domains = tuple(known_domains) + tuple(rules.vip_domains)
        self._now = now
        self.digest_hour = digest_hour
        self.digest = digest
        self.model = model
        self.school_domains = tuple(d.lower() for d in school_domains)
        self.owner_context = owner_context
        self._status = {"state": "starting", "last_poll": None, "error": None}
        # admin panel notices (igris.notices.NoticeStore) and Görev Takvimi items
        # (igris.agenda.AgendaStore) for the inbox owner; None writes none
        self.notices = notices
        self.agenda = agenda
        self.notice_owner = notice_owner
        self.followups, self.people = followups, people
        self.owner_addresses = tuple(owner_addresses)
        self._channel_failing = False  # the last delivery attempt raised
        self._failed_polls = 0
        self._gmail_down = False  # the failing polls are an outage (its notice is written)
        self._gmail_told = False  # its phone message was delivered
        self._gmail_text = ""
        self._model_told = None  # the model outage (its start stamp) this process told about
        self._model_failed_now = False  # a model read failed during this poll

    def _owner(self) -> str | None:
        """The notice owner; a failing lookup (the accounts database) means none."""
        try:
            return self.notice_owner() if callable(self.notice_owner) else self.notice_owner
        except Exception:
            return None

    def _notice_payload(self, row: dict, agenda_id: str | None, *,
                        ref: str | None = None) -> dict | None:
        """A security problem, a task to do or a doubted model answer (alerted or not)
        becomes an admin panel notice (never the body). The caller commits this payload
        atomically with the mail decision before attempting delivery."""
        if row.get("category") == "guvenlik":
            kind, title = "mail_risky", "Güvenlik: " + (row["subject"] or "-")
        elif row.get("doubt"):  # before gorev: a doubted gorev has no usable task
            kind, title = "mail_important", "Kontrol et: " + (row["subject"] or "-")
        elif row.get("category") == "gorev" and row.get("alert"):
            task = row.get("task") or {}
            kind, title = "mail_important", "Görev: " + (task.get("title") or row["subject"] or "-")
        else:
            return None
        owner = self._owner()
        if self.notices is None or not self.notice_owner:
            return None  # no panel to write to
        body = f"Kimden: {row['sender'] or '-'}\n{row.get('reason') or ''}".strip()
        return {"owner": owner, "kind": kind, "title": title, "body": body,
                "source": "gmail", "ref": ref or row["id"],
                "action": {"agenda": agenda_id} if agenda_id else None}

    def _flush_notices(self) -> None:
        """Retry saved payloads without asking Gmail or the model to read the mail again."""
        if self.notices is None:
            return
        for payload in self.store.pending_notices():
            owner = payload.get("owner") or self._owner()
            if not owner:
                continue
            try:
                # NoticeStore deduplicates by owner/kind/ref if a crash or a failed
                # deletion leaves an already delivered notice in the outbox.
                self.notices.add(**dict(payload, owner=owner))
                self.store.mark_noticed(payload["ref"])
            except Exception:
                continue  # one failed write must not prevent other queued deliveries

    def _agenda_item(self, row: dict) -> str | None:
        """A dated task goes to the Görev Takvimi: added when it is a sure one, else proposed."""
        task = row.get("task")
        owner = self._owner()
        if (self.agenda is None or not owner or row.get("category") != "gorev" or not task
                or not (task.get("due") or task.get("start"))):
            return None
        try:
            item, _ = self.agenda.upsert(owner, {
                "branch": task["branch"], "kind": task["kind"], "title": task["title"],
                "start": task.get("start"), "due": task.get("due"), "source": "gmail",
                "source_ref": row["id"], "mandatory": task["mandatory"],
                "confidence": task["confidence"],
                "status": "added" if row.get("alert") else "proposed"})
        except Exception:
            return None  # the mail is stored; the calendar misses only this item
        return item.get("id")

    def _system_notice(self, kind: str, title: str, text: str, ref: str) -> None:
        """The admin panel notice of a system problem, written once when the outage starts
        (also without a channel). Never raises."""
        owner = self._owner()
        if self.notices is None or not owner:
            return
        try:
            self.notices.add(owner, kind, title, text, source="gmail", ref=ref)
        except Exception:
            pass

    def _tell_model_down(self) -> None:
        """Send the model-down message until one delivery succeeds; an outage that ended
        first leaves nothing to send."""
        down = self.store.get_meta("model_down")
        if not down or down == self._model_told or self.store.get_meta("model_down_told"):
            return
        if self._deliver(MODEL_DOWN_TEXT):
            self._model_told = down  # within this process, even if the write below fails
            try:
                self.store.set_meta("model_down_told", "1")
            except sqlite3.Error:
                pass

    def _backlog(self, truncated: bool) -> None:
        """Remember a catch-up listing that hit the ceiling (one notice when it starts);
        the next listing that did not hit it clears the warning."""
        was = self.store.get_meta("backlog_truncated")
        if truncated and not was:
            stamp = self._now().isoformat(timespec="seconds")
            self.store.set_meta("backlog_truncated", stamp)
            self._system_notice("connection", "Eski mailler okunamadı", BACKLOG_TEXT,
                                ref="inbox-backlog:" + stamp)
        elif not truncated and was:
            self.store.set_meta("backlog_truncated", "")

    def _model_state(self) -> str:
        if self.model is None:
            return "none"
        try:
            return "down" if self.store.get_meta("model_down") else "ok"
        except sqlite3.Error:
            return "ok"

    def status(self) -> dict:
        """The poll state plus the notification channel (ok, none, failing), the number of
        alerts not delivered yet and the model state (ok, down, none)."""
        out = dict(self._status)
        out["channel"] = ("none" if self._send is None
                          else "failing" if self._channel_failing else "ok")
        try:
            out["pending_alerts"] = self.store.pending_count()
        except sqlite3.Error:
            out["pending_alerts"] = None
        out["model"] = self._model_state()
        try:
            out["backlog_truncated"] = bool(self.store.get_meta("backlog_truncated"))
        except sqlite3.Error:
            out["backlog_truncated"] = False
        return out

    def _model_outcome(self, stage: str) -> None:
        """Count failed model reads in a row; a working read ends an outage."""
        if stage == "model_failed":
            self._model_failed_now = True
            failures = int(self.store.get_meta("model_failures") or 0) + 1
            self.store.set_meta("model_failures", str(failures))
            if failures >= MODEL_DOWN_AFTER and not self.store.get_meta("model_down"):
                stamp = self._now().isoformat(timespec="seconds")
                self.store.set_meta("model_down_told", "")
                self.store.set_meta("model_down", stamp)
                self._system_notice("models", "Model mailleri okuyamıyor", MODEL_DOWN_TEXT,
                                    ref="inbox-model-down:" + stamp)
                self._tell_model_down()  # retried on later polls until delivered
        elif stage == "model":
            if self.store.get_meta("model_failures") not in (None, "0"):
                self.store.set_meta("model_failures", "0")
            if self.store.get_meta("model_down"):  # over: a message not delivered yet is dropped
                self.store.set_meta("model_down", "")
                self.store.set_meta("model_down_told", "")

    def _row(self, message_id: str, raw: bytes, labels: tuple[str, ...], *,
             use_model: bool) -> dict:
        verdict = mailcheck.check_message(raw, known_domains=self.known_domains)
        risk, school, sender = alert_risk(verdict), False, None
        try:
            message = message_from_bytes(raw, policy=policy.default)
            headers = {name: str(value) for name, value in message.items()}
            body = plain_body(message)
            school = school_forward(message, self.school_domains)
            if school:
                sender = original_sender(message, body, self.school_domains)
                if forward_authenticated(message, verdict, self.school_domains):
                    risk = forward_risk(verdict, risk)
        except Exception:
            headers, body = {}, ""
        try:
            label = mailtriage.classify(headers, self.rules)
        except Exception:
            label = "routine"
        row = {"id": message_id, "seen_at": self._now().isoformat(timespec="seconds"),
               "sender": _clean(sender or headers.get("From")),
               "subject": _clean(headers.get("Subject")),
               "label": label, "risk": risk, "flags": ",".join(verdict.flags),
               "spf": verdict.spf, "dkim": verdict.dkim, "dmarc": verdict.dmarc,
               "origin": "okul" if school else ""}
        lowered = {k.lower() for k in headers}
        facts = {"sender": row["sender"], "subject": row["subject"], "body": body,
                 "labels": labels, "unsubscribe": "list-unsubscribe" in lowered,
                 "triage": label, "risk": row["risk"], "flags": verdict.flags,
                 "dmarc": verdict.dmarc, "school": school}
        decision = mail_decide.decide(facts, self.model if use_model else None,
                                      today=self._now().date(),
                                      owner_context=self.owner_context)
        row.update(category=decision.category, reason=decision.reason,
                   task=decision.task, alert=decision.notify, stage=decision.stage,
                   doubt=decision.doubt,
                   model_read=use_model and self.model is not None
                   and decision.stage in ("model", "model_failed"))
        return row

    def _window(self) -> tuple[str, int]:
        """The query and id limit: back to the last complete poll, plus a day of margin."""
        last = self.store.get_meta("last_ok_poll")
        days = MIN_WINDOW_DAYS
        if last:
            try:
                elapsed = (self._now() - datetime.fromisoformat(last)).total_seconds()
            except (ValueError, TypeError):
                elapsed = 0
            days = min(max(math.ceil(max(elapsed, 0) / 86400) + 1, MIN_WINDOW_DAYS),
                       MAX_WINDOW_DAYS)
        limit = MAX_CATCH_UP if days > MIN_WINDOW_DAYS else MAX_PER_POLL
        return f"newer_than:{days}d {RECEIVED}", limit

    @staticmethod
    def _read(client, message_id: str) -> tuple[bytes, tuple[str, ...]]:
        reads = getattr(client, "raw_with_labels", None)
        return reads(message_id) if reads else (client.raw(message_id), ())

    def _fetch_new(self) -> tuple[int, bool]:
        """Read new mail, then mail waiting for another model read. Returns how many new
        mails were recorded and whether every listed mail was read (no budget cut)."""
        client = self._client
        baseline = self.store.get_meta("baseline") is None
        query, limit = (QUERY, MAX_PER_POLL) if baseline else self._window()
        ids = client.list_ids(query, max_results=limit)
        # a listing that filled its limit may hide older mail: the window must not shrink
        complete = baseline or len(ids) < limit
        if query != QUERY:  # catching up: oldest first, so a budget cut still moves on
            ids = ids[::-1]
        known = self.store.known(ids)
        # a catch-up that filled the ceiling cannot reach older mail at all: say so, and once
        # the newest mails are known let the window shrink instead of listing them for ever
        truncated = not baseline and query != QUERY and len(ids) >= limit
        if truncated and not set(ids) - known:
            complete = True
        if not baseline:
            self._backlog(truncated)
        self._model_failed_now = False
        added = model_reads = 0
        for message_id in ids:
            if message_id in known:
                continue
            if self.model is not None and not baseline and model_reads >= MAX_MODEL_PER_POLL:
                complete = False
                break  # unseen mail stays unseen and is read on the next poll
            thread_id = None
            if self.followups is not None and hasattr(client, 'raw_details'):
                raw, labels, thread_id = client.raw_details(message_id)
            else:
                raw, labels = self._read(client, message_id)
            row = self._row(message_id, raw, labels, use_model=not baseline)
            model_reads += int(row.pop("model_read"))
            stage = row.pop("stage")
            if stage == "model_failed":
                row["reread"] = REREAD_ATTEMPTS
            notice = (self._notice_payload(row, self._agenda_item(row))
                      if not baseline else None)
            self.store.add(row, notify=not baseline, notice=notice)
            if not baseline:  # connecting Gmail must not flood the panel or the calendar
                self._reply_arrived(raw, labels, thread_id, row)
                self._flush_notices()
                self._model_outcome(stage)
            added += 1
        if baseline:
            self.store.set_meta("baseline", self._now().isoformat(timespec="seconds"))
        elif self.model is not None and not self._model_failed_now:
            # new mail first; a model that failed in this poll is not asked again until the next
            for item in self.store.rereads():
                if model_reads >= MAX_MODEL_PER_POLL or self._model_failed_now:
                    break
                model_reads += self._reread(client, item)
        return added, complete

    def _reply_arrived(self, raw, labels, thread_id, row):
        owner = self._owner()
        if self.followups is None or not owner:
            return
        message = message_from_bytes(raw, policy=policy.default)
        key = match_followup(message, plain_body(message),
                             open_keys={item['thread_id'] for item in self.followups.list(owner)},
                             school_domains=self.school_domains, thread_id=thread_id,
                             owner_addresses=self.owner_addresses, labels=labels)
        if key and self.followups.resolve(owner, key):
            name, address = parseaddr(row['sender'])
            person = self.people.by_address(owner, address) if self.people is not None else None
            name = person.name if person else name or address
            text = f"{_clean(name)} cevap verdi: {row['subject']}"
            if row.get('reason'):
                text += f" — {row['reason']}"
            self._deliver(text)

    def _reread(self, client, item: dict) -> int:
        """Ask the model again about a mail it could not read. Returns the model reads used."""
        message_id, left = item["id"], item["reread"] - 1
        try:
            raw, labels = self._read(client, message_id)
        except gmail.GmailAuthError:
            raise
        except Exception:  # gone or unreadable: one attempt fewer
            self.store.set_reread(message_id, left)
            return 0
        row = self._row(message_id, raw, labels, use_model=True)
        row.pop("model_read")
        stage = row.pop("stage")
        self._model_outcome(stage)
        if stage == "model_failed":
            self.store.set_reread(message_id, left)  # at 0 the row stays as it is
            return 1
        if item["pending_alert"]:
            # the owner has not been told yet: a better text may replace the waiting alert,
            # but a quieter answer never cancels it; the row and its alert stay as they are
            if not row["alert"]:
                # Keep the waiting phone alert, but durably retain the quieter answer's
                # notice before clearing the model retry. Notice retries need no model.
                notice = self._notice_payload(row, self._agenda_item(row),
                                              ref=message_id + ":reread")
                self.store.set_reread(message_id, 0, notice=notice)
                self._flush_notices()
                return 1
            pending = True  # the waiting alert gets the new text (a task, security or doubt)
        elif item.get("category") != "gorev":
            pending = bool(row["alert"])
        else:
            # a fallback notifies only a possible task, so a stored "gorev" was delivered;
            # only a sure task or a security problem is worth a second message
            pending = bool(row["alert"] and (row["category"] == "guvenlik" or (
                row["category"] == "gorev" and row.get("task"))))
        notice = self._notice_payload(row, self._agenda_item(row), ref=message_id + ":reread")
        self.store.update_decision(message_id, row, pending=pending, notice=notice)
        self._flush_notices()
        return 1

    def _deliver(self, text: str) -> bool:
        if self._send is None:
            return False
        try:
            self._send(text)
        except Exception:
            self._channel_failing = True
            return False
        self._channel_failing = False
        return True

    def _send_alerts(self) -> None:
        if self._send is None:
            return
        for row in self.store.pending_alerts():
            if not self._deliver(alert_text(row)):
                return  # keep the rest pending for the next poll
            self.store.mark_alerted(row["id"])

    def _send_digest(self) -> None:
        now = self._now()
        today = now.date().isoformat()
        if now.hour < self.digest_hour or self.store.get_meta("digest_day") == today:
            return
        rows = self.store.undigested()
        if rows and not self._deliver(digest_text(rows)):
            return
        self.store.mark_digested([r["id"] for r in rows])
        self.store.set_meta("digest_day", today)

    def _poll_failed(self, stamp: str, exc: Exception) -> None:
        """Report a failed poll; from the third in a row the owner is told, once per outage:
        the notice at once, the phone message on every failed poll until it is delivered.
        Runs inside poll()'s except block, so it never raises."""
        try:
            known = isinstance(exc, (gmail.GmailError, ValueError, sqlite3.Error))
            error = str(exc) if known else f"{type(exc).__name__}: {exc}"
        except Exception:
            error = type(exc).__name__
        self._status = {"state": "error", "last_poll": stamp, "error": error[:200]}
        self._failed_polls += 1
        if self._failed_polls < POLL_DOWN_AFTER:
            return
        try:
            if not self._gmail_down:
                self._gmail_down, self._gmail_told = True, False
                # Gmail's own errors are short and carry no secret; anything else by its type
                short = (str(exc)[:120] if isinstance(exc, gmail.GmailError)
                         else "veritabanı hatası" if isinstance(exc, sqlite3.Error)
                         else type(exc).__name__)
                self._gmail_text = GMAIL_DOWN_TEXT.format(error=short)
                self._system_notice("connection", "Gmail okunamıyor", self._gmail_text,
                                    ref="inbox-gmail-down:" + stamp)
            if not self._gmail_told and self._deliver(self._gmail_text):
                self._gmail_told = True
        except Exception:
            pass

    def poll(self) -> int:
        """Fetch, decide, store and notify. Returns how many new mails were recorded.

        Never raises: every failure ends in the status (and, repeated, in one message)."""
        stamp = self._now().isoformat(timespec="seconds")
        try:
            self._flush_notices()
            if self.alarm.tripped:
                self._client = None
                self._status = {"state": "auth_down", "last_poll": stamp, "error": None}
                return 0
            if self._client is None:
                self._client = self._factory()
            if self._client is None:
                self._status = {"state": "not_configured", "last_poll": stamp, "error": None}
                return 0
            added, complete = gmail.guarded(self._fetch_new, self.alarm)
            if complete:  # a cut catch-up keeps its window for the next poll
                self.store.set_meta("last_ok_poll", stamp)
            self._send_alerts()
            self._tell_model_down()  # a model-down message the channel missed so far
            try:
                self.store.prune(stamp)  # once per poll, not per stored mail
            except sqlite3.Error:
                pass  # old rows stay a poll longer
            if self.digest:
                self._send_digest()
        except gmail.GmailAuthError:
            self._client = None
            self._status = {"state": "auth_down", "last_poll": stamp, "error": None}
            return 0
        except Exception as exc:  # the polling thread must never die on one bad poll
            self._poll_failed(stamp, exc)
            return 0
        # Gmail works again: the outage is over and an undelivered message about it is dropped
        self._failed_polls, self._gmail_down, self._gmail_told = 0, False, False
        self._status = {"state": "ok", "last_poll": stamp, "error": None}
        return added
