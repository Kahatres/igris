"""Decide what one mail means for the owner: read it, sort it, and say whether to tell them.

Every mail ends in one category:

- ``guvenlik``: a risky mail (mailcheck says high) or an account-security alert
  (new sign-in, password reset, two-step change, suspicious activity);
- ``gorev``: something the owner has to do (pay, submit, attend, register, reply);
- ``bilgi``: worth reading, nothing to do;
- ``rutin``: notifications and receipts;
- ``tanitim``: promotions and newsletters.

Stages, cheapest first: security checks, then header and subject rules (no model), then a
model reads sender, subject and the start of the body and answers in strict JSON. A task
from the model counts only when its evidence sentence is really in the mail. Any model
failure falls back to the rule result, so a poll never breaks on a bad answer; that result
has ``stage == "model_failed"`` (no model configured: ``"fallback"``) so the caller can
read the mail again later and notice a model that is down.

``guvenlik`` and a mandatory, confident ``gorev`` notify the owner. When no model answered,
a mail with a task word in its subject, or in the body of a personal (not bulk) mail,
notifies too ("Görev olabilir"): a possible task the owner checks themselves is better than
a missed one. A working model that files a mail with a task word in its subject as anything
but a task is doubted (``Decision.doubt``); a doubted personal bilgi or rutin mail notifies.

No ``igris`` imports: the model is a callable ``model(system, prompt) -> str`` and the mail
facts come in as a dict, so tests use fakes and no text leaves the process without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
import json
import re
from typing import Callable

from igris.textfold import fold_i_words as _fold

CATEGORIES = ("gorev", "guvenlik", "bilgi", "rutin", "tanitim")
TASK_KINDS = ("exam", "assignment", "lab", "presentation", "attendance", "payment", "meeting", "other")
BRANCHES = ("kisisel", "okul", "esn")
NOTIFY_CONFIDENCE = 0.8
BODY_LIMIT = 4000
REASON_LIMIT = 200
TITLE_LIMIT = 200
EVIDENCE_MIN = 3  # characters of folded evidence a task needs

_SECURITY_WORDS = (
    "new sign-in", "new sign in", "new login", "unusual sign-in", "unusual activity",
    "suspicious", "security alert", "password reset", "reset your password",
    "password was changed", "password changed", "2-step verification", "two-step",
    "two-factor", "2fa", "yeni oturum", "oturum açıldı", "giriş yapıldı",
    "şüpheli", "güvenlik uyarısı", "şifre sıfırlama", "şifreniz değiştirildi", "şifre değişikliği",
    "iki adımlı", "olağan dışı",
)
# Gmail tabs for mail sent to many people
_BULK_LABELS = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL")
_CATEGORY_NAMES = {"bilgi": "bilgi", "rutin": "rutin", "tanitim": "tanıtım"}
_PROMO_WORDS = (
    "indirim", "kampanya", "fırsat", "kupon", "ücretsiz kargo", "sale", "discount", "% off",
    "offer", "deal", "coupon", "free shipping", "black friday", "newsletter", "bülten",
)
_TASK_WORDS = (
    "son teslim", "deadline", "due", "ödeme", "payment", "kira", "rent", "fatura", "invoice",
    "sınav", "exam", "midterm", "final", "quiz", "ödev", "assignment", "kayıt", "registration",
    "register", "yoklama", "attendance", "toplantı", "meeting", "randevu", "appointment",
    "action required", "işlem gerekli", "reply by", "yanıt bekleniyor", "tarihine kadar",
    "en geç", "submit", "teslim",
)
_NOTIFIER_RE = re.compile(r"(no-?reply|do-?not-?reply|notifications?|bildirim|mailer-daemon)@",
                          re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A link or a mail address, matched in casefolded text and removed by itself, so a word next to
# it ("Exam(teacher@school.example)", "Kira:ödeme") stays. Domains are ASCII ([a-z] without
# re.IGNORECASE, which would let it match "ı"). A bare domain counts only with a path or a
# common top-level domain, so "Final.Exam" or "Ödev.pdf" is no link.
_URL_REST = r"[^\s<>()\[\]{}\"'`]*"
_DOMAIN = r"(?<![\w.@/-])(?:[a-z0-9-]+\.)+"
_COMMON_TLDS = ("com", "net", "org", "edu", "gov", "mil", "int", "info", "biz", "io", "co",
                "me", "app", "dev", "ai", "tr", "lt", "lv", "ee", "de", "fr", "uk", "eu", "nl",
                "it", "es", "pl", "ru", "us", "ca", "au", "ch", "at", "be", "se", "no", "dk",
                "fi", "cz", "example", "test", "local")
_LINK_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@(?:[a-z0-9-]+\.)+[a-z0-9-]+"  # local@domain
    r"|[a-z][a-z0-9+.-]*://" + _URL_REST  # any scheme://...
    + r"|(?<![\w.-])www\." + _URL_REST  # www....
    + "|" + _DOMAIN + r"[a-z]{2,}/" + _URL_REST  # a bare domain with a path
    # a bare domain; kept as text when a label is a task word, since a file name such as
    # "final.ai" looks the same (see _strip_link)
    + "|(?P<bare>" + _DOMAIN + "(?:" + "|".join(_COMMON_TLDS) + r"))(?![\w-])")
_FLAG_TEXT = {
    "auth_failed": "kimlik doğrulaması başarısız",
    "reply_to_mismatch": "yanıt adresi başka bir alan adına gidiyor",
    "lookalike_domain": "bilinen bir alan adına benzeyen adres",
    "display_name_spoof": "görünen ad başka bir kurumu taklit ediyor",
    "link_mismatch": "link metni ile gittiği adres farklı",
    "unreadable": "mail okunamadı",
}

DEFAULT_OWNER_CONTEXT = "The owner is a busy individual managing study, work and personal mail."
OWNER_CONTEXT_LIMIT = 500

_SYSTEM = """You sort one email for its owner, who wants to be told only about things they
must act on. {owner_context}
Answer with one JSON object and nothing else:
{"category": "gorev" | "bilgi" | "rutin" | "tanitim" | "guvenlik",
 "reason": "one short Turkish sentence saying why",
 "task": null | {"title": "short Turkish title", "kind": "exam" | "assignment" | "lab" |
   "presentation" | "attendance" | "payment" | "meeting" | "other",
   "due": "ISO 8601 date or date-time with offset, or null",
   "start": "ISO 8601 date or date-time with offset, or null",
   "branch": "kisisel" | "okul" | "esn", "mandatory": true | false,
   "confidence": number from 0 to 1},
 "evidence": "the exact sentence from the email the task and its date come from, or \\"\\""}
gorev = the reader must do something: pay (rent, bills, fees), submit, attend, register,
sign, reply when a reply is required. mandatory = there is a real consequence if they do not.
bilgi = worth reading, nothing to do. rutin = notifications, receipts, confirmations.
tanitim = promotions, newsletters, marketing. guvenlik = phishing, account security alerts.
branch: okul for school, university and courses, esn for student organisation or volunteer
work, else kisisel.
Copy the evidence word for word. Resolve relative dates ("next Friday") from today's date.
Never invent a date that is not in the email."""


def system_prompt(owner_context: str | None = None) -> str:
    """The sorting instructions with one sentence about the owner (IGRIS_OWNER_CONTEXT);
    empty or None uses a generic sentence. Whitespace is collapsed and the text capped."""
    context = " ".join(str(owner_context or "").split())[:OWNER_CONTEXT_LIMIT]
    return _SYSTEM.replace("{owner_context}", context or DEFAULT_OWNER_CONTEXT)


SYSTEM = system_prompt()


@dataclass(frozen=True)
class Decision:
    category: str
    reason: str
    notify: bool
    task: dict | None = None
    stage: str = "rule"  # "security", "rule", "model", "fallback" (no model) or "model_failed"
    evidence: str = ""
    extra: dict = field(default_factory=dict)
    # a doubted model answer: the task word in the subject of a mail the model did not call a
    # task (or security); for a gorev answer without a usable task, that word or "görev"
    doubt: str | None = None


# Turkish has two i's: I/ı and İ/i. casefold() turns "SINAV" into "sinav" (not "sınav") and
# "KİRA" into "ki̇ra" (i + U+0307), so a capitalised subject would miss its word. All four
# letters fold to one "i" and the combining dot goes, on the text and on the word lists alike.
# Trade-off: words that differ only by dotted/dotless i become equal ("kira" also starts
# "kırat", "kıraç"); none of the word lists has a pair where that matters.
# ``_fold`` is ``igris.textfold.fold_i_words``.


def _has(text: str, words) -> str | None:
    folded = _fold(text)
    for word in words:
        if re.search(r"(?<!\w)" + re.escape(_fold(word)), folded):
            return word
    return None


def _task_word(text: str) -> str | None:
    """A task word outside links and mail addresses ("shop.example" is no "exam")."""
    return _has(_LINK_RE.sub(_strip_link, _fold(text)), _TASK_WORDS)


def _strip_link(match: re.Match) -> str:
    """A link goes; a bare ``label.tld`` with a task word as a label ("final.ai") stays."""
    bare = match.group("bare")
    if bare and set(bare.split(".")) & {_fold(w) for w in _TASK_WORDS}:
        return bare
    return " "


def _bulk(mail: dict) -> bool:
    """Mail sent to many people: a List-Unsubscribe header or Gmail's promotions or social tab."""
    return bool(mail.get("unsubscribe")) or bool(set(mail.get("labels") or ()) & set(_BULK_LABELS))


def _clean(text, limit: int) -> str:
    return " ".join(_CONTROL_RE.sub(" ", str(text or "")).split())[:limit]


def _security_reason(mail: dict) -> str | None:
    if mail.get("risk") == "high":
        reasons = [_FLAG_TEXT.get(f, f) for f in mail.get("flags", ()) if f]
        if mail.get("dmarc") == "fail":
            reasons.insert(0, "DMARC başarısız")
        return "Riskli mail: " + ("; ".join(reasons) or "güvenlik kontrolü yüksek risk dedi")
    word = _has(mail.get("subject", ""), _SECURITY_WORDS)
    if word:
        if mail.get("dmarc") != "pass":
            return f"Hesap güvenliği uyarısı gibi görünüyor ama gönderen doğrulanamadı ({word})"
        return f"Hesap güvenliği uyarısı: {word}"
    return None


def rule_decision(mail: dict) -> Decision | None:
    """The answer the cheap rules are sure of, or None when a reader is needed."""
    reason = _security_reason(mail)
    if reason:
        return Decision("guvenlik", _clean(reason, REASON_LIMIT), True, stage="security")
    labels = set(mail.get("labels") or ())
    subject, sender = mail.get("subject", ""), mail.get("sender", "")
    task_word = _has(subject, _TASK_WORDS)
    # a tab or a promo subject alone says nothing about a bill or an exam in the body
    bulk_quiet = not task_word and not _task_word(str(mail.get("body") or "")[:600])
    if "CATEGORY_PROMOTIONS" in labels and bulk_quiet:
        return Decision("tanitim", "Gmail tanıtım kategorisinde", False)
    if mail.get("triage") == "junk":
        return Decision("tanitim", "Konu kendi gereksiz kelime listende", False)
    if mail.get("unsubscribe") and _has(subject, _PROMO_WORDS) and bulk_quiet:
        return Decision("tanitim", "Abonelikten çıkma bağlantılı tanıtım maili", False)
    if "CATEGORY_SOCIAL" in labels and bulk_quiet:
        return Decision("rutin", "Sosyal ağ bildirimi", False)
    if _NOTIFIER_RE.search(sender) and not task_word and not _has(mail.get("body", "")[:600], _TASK_WORDS):
        return Decision("rutin", "Otomatik bildirim, yapılacak bir şey görünmüyor", False)
    return None


def fallback_decision(mail: dict) -> Decision:
    """What the rules can say when no model answered. A task word in the subject, or in the
    body of a personal mail, notifies: the owner checks a possible task themselves rather
    than miss it. In the body of bulk mail (a newsletter that mentions "fatura") it does not."""
    subject_word = _has(mail.get("subject", ""), _TASK_WORDS)
    body_word = _task_word(str(mail.get("body") or "")[:1500])
    word = subject_word or (None if _bulk(mail) else body_word)
    if word:
        return Decision("gorev", f"Görev olabilir ({word}); model okuyamadı, kendin kontrol et",
                        True, stage="fallback")
    if body_word:  # bulk mail: filed as the tab or the list says, without an alert
        labels = set(mail.get("labels") or ())
        social = ("CATEGORY_SOCIAL" in labels and "CATEGORY_PROMOTIONS" not in labels
                  and not mail.get("unsubscribe"))
        return Decision("rutin" if social else "tanitim",
                        f"Toplu gönderimde görev kelimesi geçiyor ({body_word}); model okuyamadı",
                        False, stage="fallback")
    if mail.get("unsubscribe"):
        return Decision("tanitim", "Toplu gönderim (abonelik bağlantısı var)", False, stage="fallback")
    return Decision("bilgi", "Model okuyamadı; yapılacak bir iş görünmüyor", False, stage="fallback")


def _when(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if _DATE_RE.fullmatch(value):
            return date.fromisoformat(value).isoformat()
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()  # the model left out the offset: the server's zone
    return moment.isoformat(timespec="minutes")


def _task(raw, evidence: str, text: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    folded = _fold(evidence)
    # a task the mail does not say is no task; evidence that folds to (almost) nothing is
    # in every mail, so it proves nothing
    if len(folded) < EVIDENCE_MIN or folded not in _fold(text):
        return None
    title = _clean(raw.get("title"), TITLE_LIMIT)
    confidence = raw.get("confidence")
    if not title or isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    return {
        "title": title,
        "kind": raw.get("kind") if raw.get("kind") in TASK_KINDS else "other",
        "due": _when(raw.get("due")),
        "start": _when(raw.get("start")),
        "branch": raw.get("branch") if raw.get("branch") in BRANCHES else "kisisel",
        "mandatory": raw.get("mandatory") is True,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "evidence": _clean(evidence, 500),
    }


def _parse(answer: str) -> dict:
    text = (answer or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the answer")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    return data


def prompt_for(mail: dict, today: date) -> str:
    school = "Forwarded from the reader's university mailbox (branch okul).\n" if mail.get("school") else ""
    return (f"Today: {today.isoformat()} ({today.strftime('%A')})\n{school}"
            f"From: {_clean(mail.get('sender'), 300)}\n"
            f"Subject: {_clean(mail.get('subject'), 300)}\n\n"
            f"{str(mail.get('body') or '')[:BODY_LIMIT]}")


def model_decision(mail: dict, model: Callable[[str, str], str], today: date, *,
                   owner_context: str | None = None) -> Decision:
    data = _parse(model(system_prompt(owner_context), prompt_for(mail, today)))
    category = data.get("category")
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    evidence = data.get("evidence") if isinstance(data.get("evidence"), str) else ""
    text = f"{mail.get('subject', '')}\n{mail.get('body', '')}"
    task = _task(data.get("task"), evidence, text) if category == "gorev" else None
    reason = _clean(data.get("reason"), REASON_LIMIT) or "Model gerekçe vermedi"
    notify = category == "guvenlik" or bool(
        category == "gorev" and task and task["mandatory"]
        and task["confidence"] >= NOTIFY_CONFIDENCE)
    return Decision(category, reason, notify, task, stage="model", evidence=_clean(evidence, 500))


def _doubted(decision: Decision, word: str, mail: dict) -> Decision:
    """A model answer that is not a task although the subject has a task word: it keeps its
    category, and only a personal bilgi or rutin mail notifies (never a promotion or bulk mail,
    where "fatura" or "sınav" in a subject is common)."""
    notify = decision.category in ("bilgi", "rutin") and not _bulk(mail)
    name = _CATEGORY_NAMES.get(decision.category, decision.category)
    if notify:
        reason = (f"Model görev değil dedi ama konuda '{word}' geçiyor; kendin kontrol et "
                  f"({decision.reason})")
    else:
        reason = (f"Model {name} dedi ama konuda '{word}' geçiyor; telefona bildirilmedi, "
                  f"kendin bak ({decision.reason})")
    return replace(decision, notify=notify, reason=_clean(reason, REASON_LIMIT), doubt=word)


def _doubted_task(decision: Decision, mail: dict) -> Decision:
    """A model "gorev" whose task was rejected (no task, or evidence that is empty, too short
    or not in the mail) is never silent: it is doubted like any other unsure answer, and a
    personal mail notifies. It never becomes a dated task."""
    word = _task_word(mail.get("subject", "")) or "görev"
    notify = not _bulk(mail)
    tail = "kendin kontrol et" if notify else "telefona bildirilmedi, kendin bak"
    reason = (f"Model görev dedi ama dayandığı cümle mailde bulunamadı; {tail} "
              f"({decision.reason})")
    return replace(decision, notify=notify, reason=_clean(reason, REASON_LIMIT), doubt=word)


def decide(mail: dict, model: Callable[[str, str], str] | None = None, *,
           today: date | None = None, owner_context: str | None = None) -> Decision:
    """Sort one mail. ``mail`` holds sender, subject, body, labels, unsubscribe, triage,
    risk, flags and dmarc; ``model`` is None when no private model may read mail;
    ``owner_context`` is the one sentence about the owner the model is told (see
    ``system_prompt``).

    The stage tells why the model did not decide: ``"fallback"`` when there is no model,
    ``"model_failed"`` when it raised or gave an unusable answer. A model answer that is
    neither gorev nor guvenlik for a subject with a task word carries that word in ``doubt``;
    so does a gorev answer whose task was rejected (``"görev"`` when the subject has none)."""
    sure = rule_decision(mail)
    if sure is not None:
        return sure
    if model is None:
        return fallback_decision(mail)
    try:
        decision = model_decision(mail, model, today or date.today(),
                                  owner_context=owner_context)
    except Exception:  # a bad or missing answer must not stop the inbox
        return replace(fallback_decision(mail), stage="model_failed")
    if mail.get("school") and decision.task:  # a school mailbox forward is school work
        decision = replace(decision, task=dict(decision.task, branch="okul"))
    if decision.category not in ("gorev", "guvenlik"):
        # cross-check the model against the subject only; task words in bodies are too common
        word = _task_word(mail.get("subject", ""))
        if word:
            decision = _doubted(decision, word, mail)
    elif decision.category == "gorev" and decision.task is None:
        decision = _doubted_task(decision, mail)
    return decision
