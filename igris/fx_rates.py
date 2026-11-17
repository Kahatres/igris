"""Exchange rates from the Turkish central bank's daily XML, as the ``doviz_kuru`` chat tool.

A rate is a number an official feed publishes, so it is read from the feed rather than
asked of a model. The feed is TCMB's indicative rates bulletin
(https://www.tcmb.gov.tr/kurlar/today.xml, no key): one bulletin per business day,
published around 15:30 Turkey time. Before that, and on weekends and holidays, the newest
bulletin is from an earlier day; the result always names the bulletin's own date and says
that it is not a live market price.

The feed is read at most once per cache period (default 5 minutes) with a short timeout.
The fetcher is injected, so tests never touch the network.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Callable

from igris.tools import Tool, ToolError, ToolRegistry

log = logging.getLogger(__name__)

TOOL_NAME = "doviz_kuru"
FEED_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
DEFAULT_TIMEOUT = 8
DEFAULT_TTL = 300
MAX_CODES = 10
_FIELDS = (("ForexBuying", "forex buying"), ("ForexSelling", "forex selling"),
           ("BanknoteBuying", "banknote buying"), ("BanknoteSelling", "banknote selling"))
_CODE_RE = re.compile(r"[A-Z]{3}")
SOURCE = ("TCMB (Central Bank of the Republic of Turkey) indicative rates; one bulletin per "
          "business day around 15:30 Turkey time; not a live market price")


class FeedError(RuntimeError):
    """The feed could not be read or understood."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


def fetch_feed(url: str = FEED_URL, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """Read the feed (the real network; tests inject a fake)."""
    request = urllib.request.Request(url, headers={"User-Agent": "igris"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(1_000_000)


MAX_UNIT = 1_000_000


def _number(text):
    """A finite positive number, else None."""
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _unit(text):
    """The bulletin's unit (1, 100, ...): a whole number from 1 to MAX_UNIT, else None.
    A missing unit means 1."""
    if text is None or not str(text).strip():
        return 1
    value = _number(text)
    if value is None or value != int(value) or not 1 <= value <= MAX_UNIT:
        return None
    return int(value)


def parse_tcmb(data: bytes) -> tuple[date, dict]:
    """(bulletin date, {code: {"name", "unit", field: value}}) from the TCMB XML.

    The file says UTF-8 but has been seen with Turkish ISO-8859-9 bytes in the names."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise FeedError("bad_feed", "empty response")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        text = bytes(data).decode("iso-8859-9")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    # the bulletin never has a DTD; refusing one keeps entity expansion tricks out
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise FeedError("bad_feed", "unexpected DTD")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FeedError("bad_feed", f"not XML: {exc}") from None
    try:
        day = datetime.strptime(root.get("Tarih", ""), "%d.%m.%Y").date()
    except ValueError:
        raise FeedError("bad_feed", "no bulletin date") from None
    rates = {}
    for node in root.iter("Currency"):
        code = (node.get("CurrencyCode") or node.get("Kod") or "").strip().upper()
        if not _CODE_RE.fullmatch(code):
            continue
        unit = _unit(node.findtext("Unit"))
        if unit is None:
            continue  # a malformed currency is skipped, the rest of the bulletin is used
        entry = {"name": (node.findtext("CurrencyName") or code).strip(), "unit": unit}
        for tag, _ in _FIELDS:
            value = _number(node.findtext(tag))
            if value is not None:
                entry[tag] = value
        if "ForexBuying" in entry or "ForexSelling" in entry:
            rates[code] = entry
    if not rates:
        raise FeedError("bad_feed", "no rates")
    return day, rates


def _mid_per_unit(entry: dict) -> float:
    values = [entry[t] for t in ("ForexBuying", "ForexSelling") if t in entry]
    return sum(values) / len(values) / entry["unit"]


def _fmt(value: float) -> str:
    return f"{value:.4f}" if value >= 0.01 else f"{value:.6f}"


class FxRates:
    """Cached reader of the TCMB bulletin."""

    def __init__(self, *, fetch: Callable[[], bytes] | None = None,
                 timeout: int = DEFAULT_TIMEOUT, ttl: int = DEFAULT_TTL,
                 clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], date] = lambda: datetime.now().astimezone().date()):
        self.fetch = fetch if fetch is not None else (lambda: fetch_feed(timeout=timeout))
        self.ttl = ttl
        self.clock = clock
        self.today = today
        self._cached = None  # (read at, bulletin date, rates)
        self._lock = threading.Lock()

    def bulletin(self) -> tuple[date, dict]:
        """The bulletin, from the cache while it is fresh; raises FeedError."""
        with self._lock:
            if self._cached is not None and self.clock() - self._cached[0] < self.ttl:
                return self._cached[1], self._cached[2]
            try:
                data = self.fetch()
            except FeedError:
                raise
            except Exception as exc:  # urllib, socket and timeout errors
                raise FeedError("network", type(exc).__name__) from None
            day, rates = parse_tcmb(data)
            self._cached = (self.clock(), day, rates)
            return day, rates

    def answer(self, codes, base: str = "TRY") -> str:
        """The tool result for ``codes`` (a list or a comma separated string) against ``base``."""
        if isinstance(codes, str):
            codes = [c for c in re.split(r"[\s,;/]+", codes) if c]
        if not isinstance(codes, (list, tuple)) or not codes:
            raise ToolError("codes must name at least one currency code, e.g. USD")
        codes = [str(c).strip().upper() for c in codes][:MAX_CODES]
        base = str(base or "TRY").strip().upper()
        for code in codes + [base]:
            if not _CODE_RE.fullmatch(code):
                raise ToolError(f"{code!r} is not a three-letter currency code")
        try:
            return self._answer(codes, base)
        except ToolError:
            raise
        except FeedError as exc:
            return f"FX_RATES status=failed reason={exc.reason}"
        except Exception:  # never let a feed oddity break the chat turn
            log.exception("doviz_kuru failed unexpectedly")
            return "FX_RATES status=failed reason=internal_error"

    def _answer(self, codes: list[str], base: str) -> str:
        day, rates = self.bulletin()
        if base != "TRY" and base not in rates:
            raise ToolError(f"{base} is not in the TCMB bulletin")
        today = self.today()
        lines = [f"FX_RATES status=ok bulletin_date={day:%d.%m.%Y} today={today:%d.%m.%Y}",
                 f"source: {SOURCE}"]
        if day < today:
            lines.append(f"note: this is the newest bulletin, from {day:%d.%m.%Y}; the "
                         "bulletin for a business day appears around 15:30 Turkey time")
        for code in codes:
            entry = rates.get(code)
            if code == base:
                continue
            if entry is None:
                lines.append(f"{code}: not in the TCMB bulletin")
                continue
            if base == "TRY":
                unit = entry["unit"]
                parts = [f"{label} {_fmt(entry[tag])}" for tag, label in _FIELDS if tag in entry]
                lines.append(f"{unit} {code} ({entry['name']}) in TRY: " + ", ".join(parts))
            else:
                cross = _mid_per_unit(entry) / _mid_per_unit(rates[base])
                lines.append(f"1 {code} = {_fmt(cross)} {base} (derived from the TCMB TRY "
                             "mid rates, buying and selling averaged)")
        return "\n".join(lines)


def register_fx_tools(registry: ToolRegistry, fx: FxRates) -> None:
    """Register ``doviz_kuru`` over ``fx``."""

    def doviz_kuru(codes="USD", base="TRY") -> str:
        return fx.answer(codes, base)

    registry.add(Tool(
        name=TOOL_NAME,
        description=("Official exchange rates from the Turkish central bank (TCMB) daily "
                     "bulletin: forex and banknote buying and selling in TRY, or a cross "
                     "rate. Args: codes (e.g. \"USD\" or \"USD,EUR,GBP\"), base=\"TRY\". "
                     "Use it for every currency question before web_ara. Tell the owner the "
                     "bulletin date it shows and that it is the central bank's daily rate, "
                     "not a live market price."),
        func=doviz_kuru,
    ))
