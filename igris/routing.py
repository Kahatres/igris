"""Decide whether a message is answered by Igris or handed to multiagent."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

from igris.textfold import fold_ascii as _fold

__all__ = ["Decision", "detect_lang", "decide", "classify_domain", "order_free_backends"]

# ``_fold`` (igris.textfold.fold_ascii) maps Turkish diacritics and dotted/dotless i
# variants to ASCII so Turkish and English text matches the same ASCII lowercase patterns.


# Override phrases, checked before scoring. Written in folded form.
SELF_OVERRIDES = (
    r"bunu sen (yanitla|cevapla)",
    r"sen yanitla",
    r"answer (this |it )?yourself",
    r"do not use multiagent",
)
MULTIAGENT_OVERRIDES = (
    r"multiagent\W{0,2}a\s+ver",
    r"multiagent\W{0,2}a\s+gonder",
    r"give (this |it )?to multiagent",
    r"send (this |it )?to multiagent",
)

# Big-job keywords (+2 each, folded form) and the short explanation pattern (-2).
BIG_KEYWORDS = (
    r"\bincele", r"\breview", r"\baudit", r"\bplan", r"\bmimari",
    r"\barchitecture", r"tum proje", r"whole project", r"\brefactor",
)
_FOLDED_BIG = tuple(_fold(p) for p in BIG_KEYWORDS)
SMALL_PATTERNS = (r"\bnedir\b", r"\bacikla", r"\bwhat is\b", r"\bexplain\b", r"\bhow\b")

_FILE_RE = re.compile(r"\w+\.(py|js|md|json)\b")
_MODULE_RE = re.compile(r"\b(modul|module)", re.IGNORECASE)
_TR_CHARS = set("ışğüöçİŞĞÜÖÇ")  # checked on raw text
_TR_WORDS = {
    "bu", "bir", "ve", "ne", "nedir", "nasil", "icin", "kac", "eder", "mi",
    "mu", "acikla", "incele", "tum", "proje", "sen", "ver", "bunu",
}


@dataclass(frozen=True)
class Decision:
    target: Literal["self", "multiagent"]
    source: Literal["override", "rules", "classifier"]
    lang: Literal["tr", "en"]
    reason: str


def detect_lang(text: str) -> Literal["tr", "en"]:
    if any(c in _TR_CHARS for c in text):
        return "tr"
    words = re.findall(r"[a-z]+", _fold(text))
    return "tr" if any(w in _TR_WORDS for w in words) else "en"


def classify_domain(text: str, *, has_attachments: bool = False) -> str:
    """Classify a request for benchmark routing without calling a model."""
    folded = _fold(text)
    if re.search(r"\b(tool|function call|api cagir|arac cagir)\b", folded):
        return "tool_use"
    if re.search(r"\b(code|python|javascript|function|bug|kod|fonksiyon|hata ayikla)\b", folded):
        return "code"
    if re.search(r"\b(summar|ozet|summary)\w*", folded):
        return "summarization"
    if has_attachments or re.search(r"\b(long context|needle|uzun baglam)\b", folded):
        return "long_context"
    if re.search(r"\b(solve|reason|equation|denklem|akil yurut|mantik)\b", folded):
        return "reasoning"
    return "turkish_chat"


def order_free_backends(configured, ranked, *, training=(), private=True,
                        unavailable=(), allow_training=False):
    """Rank configured free providers, then enforce privacy and availability."""
    names = [item[0] if isinstance(item, (tuple, list)) else item for item in ranked]
    ordered = list(dict.fromkeys([n for n in names if n in configured] + list(configured)))
    return [n for n in ordered if n not in unavailable
            and (allow_training or not private or n not in training)]


def _reason(target: str, source: str, lang: str) -> str:
    if lang == "tr":
        who = "multiagent'a veriyorum" if target == "multiagent" else "kendim yanıtlıyorum"
        how = {"override": "isteğin üzerine", "rules": "kurallara göre",
               "classifier": "sınıflandırıcıya göre"}[source]
    else:
        who = "handing this to multiagent" if target == "multiagent" else "answering myself"
        how = {"override": "as requested", "rules": "by the rules",
               "classifier": "by the classifier"}[source]
    return f"{how.capitalize()}: {who}."


def _score(raw: str, folded: str) -> int:
    score = 2 * sum(1 for p in _FOLDED_BIG if re.search(p, folded))
    if len(raw) > 600:
        score += 1
    if len(_FILE_RE.findall(raw)) + len(_MODULE_RE.findall(folded)) >= 2:
        score += 1
    if len(raw) < 120 and any(re.search(p, folded) for p in SMALL_PATTERNS):
        score -= 2
    return score


def _first_word(answer) -> str:
    """Normalize a classifier answer: casefold, drop punctuation, keep the first word."""
    words = re.sub(r"[^\w]+", " ", str(answer or "").casefold()).split()
    return words[0] if words else ""


def decide(text: str, *, classify: Callable[[str], str] | None = None) -> Decision:
    lang = detect_lang(text)
    folded = _fold(text)

    def make(target: str, source: str) -> Decision:
        return Decision(target, source, lang, _reason(target, source, lang))

    if any(re.search(p, folded) for p in SELF_OVERRIDES):
        return make("self", "override")
    if any(re.search(p, folded) for p in MULTIAGENT_OVERRIDES):
        return make("multiagent", "override")

    score = _score(text, folded)
    if score >= 3:
        return make("multiagent", "rules")
    if score <= 0:
        return make("self", "rules")
    if classify is not None:
        answer = _first_word(classify(text))
        return make("multiagent" if answer == "multiagent" else "self", "classifier")
    return make("self", "rules")
