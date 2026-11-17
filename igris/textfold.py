"""Text folding for matching Turkish and English text; standard library only.

Turkish has two i's (I/ı and İ/i), and ``str.casefold`` alone gets them wrong:
"SINAV" becomes "sinav" (not "sınav") and "KİRA" becomes "ki̇ra" (i + U+0307). Each
function below is one folding behaviour that a caller relies on; they differ on purpose,
so a caller keeps exactly the matching it was written and tested for.

- ``fold_ascii``: Turkish letters to ASCII, then casefold (routing, modelbench).
- ``fold_lower_ascii``: İ to i, lower-case, Turkish letters and curly single quotes to
  ASCII (assistant).
- ``lower_ascii``: lower-case, then Turkish letters to ASCII; İ lower-cases to i + U+0307,
  which stays (telegram_chat commands).
- ``fold_i``: only the i's fold to "i", plus casefold; a U+0307 goes only right after an
  i letter (mailtriage).
- ``fold_i_words``: the i's fold to "i", every U+0307 goes, casefold and whitespace runs
  collapse to one space; None counts as "" (mail_decide).
- ``fold_marks``: lower-case, ı to i, every combining mark removed after NFD, whitespace
  collapsed (people).

Imports nothing from igris.
"""

from __future__ import annotations

import re
import unicodedata

_TR_ASCII = str.maketrans({"İ": "i", "I": "i", "ı": "i", "ş": "s", "ğ": "g", "ü": "u",
                           "ö": "o", "ç": "c", "Ş": "s", "Ğ": "g", "Ü": "u", "Ö": "o",
                           "Ç": "c"})
_LOWER_ASCII_QUOTES = str.maketrans("çğıöşü’‘", "cgiosu''")
_LOWER_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosucgiosu")
_I_ONLY = str.maketrans({"İ": "i", "I": "i", "ı": "i"})
_I_AND_DOT = str.maketrans({"İ": "i", "I": "i", "ı": "i", "̇": None})
_DOT_AFTER_I = re.compile("(?<=[iIİı])̇")


def fold_ascii(text: str) -> str:
    """Turkish letters (both cases, all four i's) to ASCII, then casefold."""
    return text.translate(_TR_ASCII).casefold()


def fold_lower_ascii(text: str) -> str:
    """İ to i, lower-case, then ç ğ ı ö ş ü and curly single quotes to ASCII."""
    return text.replace("İ", "i").lower().translate(_LOWER_ASCII_QUOTES)


def lower_ascii(text: str) -> str:
    """Lower-case, then Turkish letters to ASCII (İ has already become i + U+0307)."""
    return text.lower().translate(_LOWER_ASCII)


def fold_i(text: str) -> str:
    """Casefold with the four i's as one "i"; drop U+0307 only right after an i letter,
    so any other combining dot stays."""
    return _DOT_AFTER_I.sub("", text).translate(_I_ONLY).casefold()


def fold_i_words(text) -> str:
    """Casefold with the four i's as one "i", every U+0307 dropped and whitespace runs
    collapsed; a false value such as None folds to ""."""
    folded = str(text or "").translate(_I_AND_DOT).casefold().translate(_I_AND_DOT)
    return " ".join(folded.split())


def fold_marks(text: str) -> str:
    """Return a lowercase, accent-free form with normalized whitespace."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    lowered = text.lower().replace("ı", "i")
    decomposed = unicodedata.normalize("NFD", lowered)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_marks.split())
