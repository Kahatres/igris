"""Lightweight, model-free mail triage rules.

This module classifies a message using only its headers and a small,
user-provided rule set.  No network access, no model call, and no
imports from the rest of ``igris`` except the leaf ``igris.textfold``.
"""

from __future__ import annotations

import email.utils
import re
from dataclasses import dataclass, fields
from typing import Mapping

from igris.textfold import fold_i as _fold

__all__ = ["LABELS", "TriageRules", "classify", "load_rules"]

#: The labels ``classify`` may return.
LABELS = ("important", "routine", "newsletter", "junk")


@dataclass(frozen=True)
class TriageRules:
    """User-configurable triage rules.

    Attributes:
        vip_senders: exact sender addresses that mark mail as important.
        vip_domains: exact sender domains that mark mail as important.
        important_words: words that mark a subject as important.
        junk_words: words that mark a subject as junk.
    """

    vip_senders: tuple[str, ...] = ()
    vip_domains: tuple[str, ...] = ()
    important_words: tuple[str, ...] = ()
    junk_words: tuple[str, ...] = ()


# Turkish has two i's: I/ı and İ/i. casefold() turns "SINAV" into "sinav" (not "sınav") and
# "KİRA" into "ki̇ra" (i + U+0307), so a capitalised subject would miss its word. All four
# letters fold to one "i", on the subject and the words alike. The combining dot goes only
# right after an i letter (what lower-casing İ leaves); any other U+0307 stays, so it keeps
# its place in word boundaries. Trade-off: words that differ only by dotted/dotless i become
# equal ("kira" also starts "kırat"). ``_fold`` is ``igris.textfold.fold_i``.


def _has_word(subject: str, words: tuple[str, ...]) -> bool:
    """Whether a word starts anywhere in the subject.

    Only the start is anchored: Turkish adds suffixes, so "fatura" must find
    "faturanız", while "urgent" must not find "nonurgent".
    """
    folded = _fold(subject)
    keys = (_fold(word) for word in words)
    return any(re.search(r"(?<!\w)" + re.escape(key), folded)
               for key in keys if key.strip())


def classify(headers: Mapping[str, str], rules: TriageRules) -> str:
    """Classify a message from its headers and return one of ``LABELS``.

    From is only a claim. A VIP result means nothing unless the mail also
    passes ``mailcheck``.

    Rule order (first match wins):

    1. VIP sender (exact address) or VIP domain (exact domain):
       ``'important'``.
    2. Junk word in the subject: ``'junk'``.
    3. ``List-Unsubscribe`` header present, or ``Precedence`` set to
       ``bulk``/``list``: ``'newsletter'``.
    4. Important word in the subject: ``'important'``.
    5. Anything else: ``'routine'``.

    Header names and values are matched case-insensitively.  The sender
    address is extracted with :func:`email.utils.parseaddr`, so a
    display name alone can never make a sender a VIP.
    """
    # Lowercase header names and values once for case-insensitive use.
    lowered = {name.lower(): value.lower() for name, value in headers.items()}

    # Lowercase the rule entries so header casing cannot dodge them.
    vip_senders = {sender.lower() for sender in rules.vip_senders}
    vip_domains = {domain.lower() for domain in rules.vip_domains}

    # 1. VIP sender or VIP domain.  parseaddr pulls the real address out
    # of any display-name formatting; a value without a local part
    # (``local`` empty) can never be a VIP match.
    _, address = email.utils.parseaddr(lowered.get("from", ""))
    local, _, domain = address.rpartition("@")
    if local and address in vip_senders:
        return "important"
    if local and domain in vip_domains:
        return "important"

    subject = lowered.get("subject", "")

    # 2. Junk word in the subject.
    if _has_word(subject, rules.junk_words):
        return "junk"

    # 3. Mailing-list signals.
    if "list-unsubscribe" in lowered:
        return "newsletter"
    if lowered.get("precedence", "").strip() in ("bulk", "list"):
        return "newsletter"

    # 4. Important word in the subject.
    if _has_word(subject, rules.important_words):
        return "important"

    # 5. Default.
    return "routine"


def load_rules(data: Mapping) -> TriageRules:
    """Build a :class:`TriageRules` from a mapping (e.g. parsed JSON).

    Every field must be a list of non-empty strings and no unknown
    field may be present, otherwise :class:`ValueError` is raised.
    """
    if not isinstance(data, Mapping):
        raise ValueError("rules must be a mapping")

    known = {field.name for field in fields(TriageRules)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown rule fields: {sorted(unknown)}")

    values: dict[str, tuple[str, ...]] = {}
    for field in fields(TriageRules):
        if field.name not in data:
            continue
        value = data[field.name]
        if not isinstance(value, list):
            raise ValueError(f"field {field.name!r} must be a list")
        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError(
                    f"field {field.name!r} must contain only non-empty strings"
                )
        values[field.name] = tuple(value)

    return TriageRules(**values)
