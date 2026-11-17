"""Deterministic, offline safety checks for one raw mail message.

The module never performs network or DNS lookups. It only inspects the bytes
it is given so that later stages (and models) see a curated verdict first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.utils import parseaddr
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

# A raw message larger than this is rejected before any parsing happens.
MAX_RAW_SIZE = 10 * 1024 * 1024
# At most this many distinct links are reported.
MAX_LINKS = 50

_HIGH_RISK_FLAGS = frozenset(
    {"lookalike_domain", "display_name_spoof", "link_mismatch"}
)
_FLAG_ORDER = (
    "auth_failed",
    "reply_to_mismatch",
    "lookalike_domain",
    "display_name_spoof",
    "link_mismatch",
)

# One Authentication-Results entry must start with its method result. Matching
# anywhere would let a sender plant "dmarc=pass" in a DKIM selector or address.
_AUTH_METHOD_RE = re.compile(r"\s*(spf|dkim|dmarc)\s*=\s*([A-Za-z]+)", re.IGNORECASE)
# Worst first. When one header repeats SPF or DMARC, the worst result stands, and
# any other non-pass result still beats a pass. DKIM works the other way: mail often
# carries several signatures, RFC 6376 treats a failed one as absent, so one pass is
# enough.
_AUTH_RESULTS_WORST_FIRST = ("fail", "softfail", "permerror", "temperror")
_WORST_RESULT_METHODS = frozenset({"spf", "dmarc"})
# Plain-text URLs; HTML is parsed with html.parser instead.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?'\")]}>"


@dataclass(frozen=True)
class MailVerdict:
    """Result of the deterministic checks on a single mail message."""

    spf: str
    dkim: str
    dmarc: str
    from_domain: str
    reply_to_domain: str
    flags: tuple[str, ...]
    links: tuple[str, ...]
    risk: str


_UNREADABLE = MailVerdict("none", "none", "none", "", "", ("unreadable",), (), "medium")


class _LinkHTMLParser(HTMLParser):
    """Collect hrefs and anchor text/href pairs from an HTML part."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.anchors: list[tuple[str, str | None]] = []
        self._in_anchor = False
        self._text_parts: list[str] = []
        self._anchor_href: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        href = attributes.get("href")
        if href:
            self.hrefs.append(href)
        if tag.lower() == "a":
            self._in_anchor = True
            self._text_parts = []
            self._anchor_href = href

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._in_anchor:
            self._in_anchor = False
            self.anchors.append(("".join(self._text_parts), self._anchor_href))
            self._text_parts = []
            self._anchor_href = None

    def handle_data(self, data):
        if self._in_anchor:
            self._text_parts.append(data)


def check_message(
    raw: bytes,
    trusted_authserv: str = "mx.google.com",
    known_domains: tuple[str, ...] = (),
) -> MailVerdict:
    """Run all offline safety checks on ``raw`` and return a verdict.

    ``ValueError`` is raised only when ``raw`` exceeds 10 MiB; malformed mail
    never raises and yields at least a ``medium`` risk.
    """

    if isinstance(raw, (bytearray, memoryview)):
        raw = bytes(raw)
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    if len(raw) > MAX_RAW_SIZE:
        raise ValueError("raw message exceeds 10 MiB")
    try:
        return _check(raw, trusted_authserv, tuple(known_domains))
    except Exception:
        # Defensive fallback: unreadable mail must never bubble up.
        return _UNREADABLE


def _check(raw: bytes, trusted_authserv: str, known_domains) -> MailVerdict:
    known = tuple(domain.strip().lower() for domain in known_domains if domain)
    message = message_from_bytes(raw, policy=policy.default)

    spf, dkim, dmarc = _auth_results(message, trusted_authserv)
    display_name, from_domain = _address(message, "From")
    _, reply_to_domain = _address(message, "Reply-To")
    links, link_mismatch = _collect_content(message)

    flags = set()
    if spf in ("fail", "softfail") or dkim in ("fail", "softfail") or dmarc in (
        "fail",
        "softfail",
    ):
        flags.add("auth_failed")
    if reply_to_domain and from_domain and reply_to_domain != from_domain:
        flags.add("reply_to_mismatch")
    if _is_lookalike(from_domain, known):
        flags.add("lookalike_domain")
    if _display_name_spoofs(display_name, known, from_domain):
        flags.add("display_name_spoof")
    if link_mismatch:
        flags.add("link_mismatch")

    ordered_flags = tuple(flag for flag in _FLAG_ORDER if flag in flags)
    return MailVerdict(
        spf=spf,
        dkim=dkim,
        dmarc=dmarc,
        from_domain=from_domain,
        reply_to_domain=reply_to_domain,
        flags=ordered_flags,
        links=links,
        risk=_risk(dmarc, ordered_flags),
    )


def _risk(dmarc: str, flags: tuple[str, ...]) -> str:
    if dmarc == "fail" or _HIGH_RISK_FLAGS.intersection(flags):
        return "high"
    # Only a real pass is low: none, permerror, temperror and the like prove nothing.
    if flags or dmarc != "pass":
        return "medium"
    return "low"


def _without_comments(value: str) -> str:
    """Drop the comments and quoted strings of a header value (RFC 5322)."""
    kept: list[str] = []
    depth, quoted, escaped = 0, False, False
    for char in value:
        if escaped:
            escaped = False
        elif char == "\\" and (depth or quoted):
            escaped = True
        elif quoted:
            quoted = char != '"'
        elif char == '"' and not depth:
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif not depth:
            kept.append(char)
    return "".join(kept)


def _auth_results(message, trusted_authserv: str) -> tuple[str, str, str]:
    try:
        headers = message.get_all("Authentication-Results") or []
    except Exception:
        headers = []

    trusted_id = (trusted_authserv or "").strip().lower()
    selected = None
    for header in headers:
        try:
            entries = _without_comments(str(header)).split(";")
        except Exception:
            continue
        # The authserv-id comes first and may be followed by a version number.
        authserv = entries[0].split()
        if authserv and authserv[0].lower() == trusted_id:
            # The receiving server adds its header on top of whatever the
            # sender forged, so the topmost match is the real one.
            selected = entries[1:]
            break

    results = {"spf": "none", "dkim": "none", "dmarc": "none"}
    seen = set()
    for entry in selected or ():
        match = _AUTH_METHOD_RE.match(entry)
        if not match:
            continue
        method, result = match.group(1).lower(), match.group(2).lower()
        if (
            method not in seen
            or (method == "dkim" and result == "pass")
            or (
                method in _WORST_RESULT_METHODS
                and _severity(result) < _severity(results[method])
            )
        ):
            results[method] = result
        seen.add(method)
    return results["spf"], results["dkim"], results["dmarc"]


def _severity(result: str) -> int:
    """Rank one result; a lower number is worse and a pass is the highest."""
    if result in _AUTH_RESULTS_WORST_FIRST:
        return _AUTH_RESULTS_WORST_FIRST.index(result)
    return len(_AUTH_RESULTS_WORST_FIRST) + (result == "pass")


def _address(message, header_name: str) -> tuple[str, str]:
    """Return ``(display_name, lowercased_domain)`` of the first address."""

    try:
        header = message.get(header_name)
    except Exception:
        return "", ""
    if header is None:
        return "", ""
    try:
        addresses = header.addresses
        if addresses:
            address = addresses[0]
            domain = (address.domain or "").lower()
            return (address.display_name or ""), domain
    except Exception:
        pass
    try:
        display, addr = parseaddr(str(header))
    except Exception:
        return "", ""
    domain = addr.rsplit("@", 1)[1].lower() if "@" in addr else ""
    return display, domain


def _part_text(part) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if isinstance(payload, bytes):
        try:
            charset = part.get_content_charset() or "utf-8"
        except Exception:
            charset = "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, TypeError):
            return payload.decode("utf-8", errors="replace")
    try:
        raw_payload = part.get_payload()
    except Exception:
        return ""
    return raw_payload if isinstance(raw_payload, str) else ""


def _collect_content(message) -> tuple[tuple[str, ...], bool]:
    candidates: list[str] = []
    link_mismatch = False
    try:
        parts = list(message.walk())
    except Exception:
        parts = []

    for part in parts:
        try:
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
        except Exception:
            continue
        if content_type == "text/plain":
            text = _part_text(part)
            for match in _URL_RE.finditer(text):
                candidates.append(match.group(0).rstrip(_URL_TRAILING))
        elif content_type == "text/html":
            text = _part_text(part)
            parser = _LinkHTMLParser()
            try:
                parser.feed(text)
                parser.close()
            except Exception:
                pass
            candidates.extend(parser.hrefs)
            for anchor_text, href in parser.anchors:
                if _anchor_mismatch(anchor_text, href):
                    link_mismatch = True

    seen = set()
    links: list[str] = []
    for candidate in candidates:
        normalized = _normalize_url(candidate)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
        if len(links) >= MAX_LINKS:
            break
    return tuple(links), link_mismatch


def _normalize_url(url: str) -> str | None:
    """Keep only http(s) URLs, dropping user info, query string and fragment.

    Those parts may carry personal tokens or passwords. A path can too, so
    these links are still not safe to send to an outside service unreviewed.
    """

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


def _host(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.lower().rstrip(".")


def _text_domain(text: str) -> str | None:
    """Return the domain shown as anchor text, or ``None`` if it is not one."""

    value = (text or "").strip()
    if not value or any(char.isspace() for char in value):
        return None
    lowered = value.lower()
    if "://" in value:
        host = _host(value)
    elif lowered.startswith("www.") or "/" in value:
        host = _host("http://" + value)
    else:
        host = lowered.rstrip(".")
    if not host:
        return None
    # "v1.2" and "1.5" read as text, not as domains.
    if "@" in host or "." not in host or not host.rsplit(".", 1)[1].isalpha():
        return None
    return host


def _within(domain: str, parent: str) -> bool:
    """Whether domain is parent or one of its subdomains."""
    return domain == parent or domain.endswith("." + parent)


def _anchor_mismatch(anchor_text: str, href: str | None) -> bool:
    if not href:
        return False
    text_domain = _text_domain(anchor_text)
    href_domain = _host(href)
    if not text_domain or not href_domain:
        return False
    # A link from example.com to click.example.com is the same sender.
    bare_text = text_domain[4:] if text_domain.startswith("www.") else text_domain
    bare_href = href_domain[4:] if href_domain.startswith("www.") else href_domain
    return not (_within(bare_href, bare_text) or _within(bare_text, bare_href))


def _is_lookalike(domain: str, known: tuple[str, ...]) -> bool:
    if not domain or any(_within(domain, candidate) for candidate in known):
        return False
    for label in domain.split("."):
        if label.startswith("xn--"):
            return True
    return any(_levenshtein(domain, candidate) <= 2 for candidate in known)


def _display_name_spoofs(
    display_name: str, known: tuple[str, ...], from_domain: str
) -> bool:
    if not display_name:
        return False
    lowered = display_name.lower()
    return any(
        candidate
        and not _within(from_domain, candidate)
        and re.search(r"(?<![\w.-])" + re.escape(candidate) + r"(?![\w-])", lowered)
        for candidate in known
    )


def _levenshtein(first: str, second: str) -> int:
    if first == second:
        return 0
    if not first:
        return len(second)
    if not second:
        return len(first)
    previous = list(range(len(second) + 1))
    for i, char_a in enumerate(first, start=1):
        current = [i]
        for j, char_b in enumerate(second, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_a != char_b),
                )
            )
        previous = current
    return previous[-1]
