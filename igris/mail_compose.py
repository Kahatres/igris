"""Build plain-text email messages and Outlook compose links."""

from __future__ import annotations

import re
from email.message import EmailMessage
from email.policy import SMTP
from urllib.parse import quote


LINK_LIMIT = 1800


class ComposeError(ValueError):
    """Raised when an outgoing message cannot be safely composed."""


def _has_line_break(value: str) -> bool:
    return "\r" in value or "\n" in value


def _validate_header(value: str, name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise ComposeError(f"{name} must be text")
    if _has_line_break(value):
        raise ComposeError(f"{name} contains a line break")
    if not allow_empty and not value.strip():
        raise ComposeError(f"{name} cannot be empty")


def _validate_address(address: str) -> None:
    if not isinstance(address, str) or not address or len(address) > 254:
        raise ComposeError("Invalid email address")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in address):
        raise ComposeError("Invalid email address")
    if any(char in address for char in "<>,;"):
        raise ComposeError("Invalid email address")
    if address.count("@") != 1:
        raise ComposeError("Invalid email address")
    local, domain = address.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise ComposeError("Invalid email address")
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise ComposeError("Invalid email address")


def _addresses(values: list[str] | tuple[str, ...], name: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ComposeError(f"{name} must be a list of addresses")
    try:
        addresses = list(values)
    except TypeError as exc:
        raise ComposeError(f"{name} must be a list of addresses") from exc
    for address in addresses:
        _validate_address(address)
    return addresses


def _validate_content(*, subject: str, body: str) -> None:
    _validate_header(subject, "Subject")
    if not isinstance(body, str) or not body.strip():
        raise ComposeError("Body cannot be empty")


def build_message(
    *,
    to: list[str],
    cc: list[str] = (),
    subject: str,
    body: str,
    sender: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    message_id: str | None = None,
) -> bytes:
    """Return an RFC 5322 UTF-8 plain-text message as SMTP-policy bytes."""
    recipients = _addresses(to, "To")
    copy_recipients = _addresses(cc, "Cc")
    if not recipients and not copy_recipients:
        raise ComposeError("At least one recipient is required")
    _validate_content(subject=subject, body=body)

    if sender is not None:
        _validate_address(sender)
    for name, value in (
        ("In-Reply-To", in_reply_to),
        ("References", references),
        ("Message-ID", message_id),
    ):
        if value is not None:
            _validate_header(value, name)

    message = EmailMessage(policy=SMTP)
    if sender is not None:
        message["From"] = sender
    message["To"] = ", ".join(recipients)
    if copy_recipients:
        message["Cc"] = ", ".join(copy_recipients)
    message["Subject"] = subject
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    if references is not None:
        message["References"] = references
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content(body.encode("utf-8"), maintype="text", subtype="plain", cte="base64")
    message.set_param("charset", "utf-8")
    return message.as_bytes(policy=SMTP)


def outlook_compose_url(
    *, to: list[str], cc: list[str] = (), subject: str, body: str
) -> str:
    """Build an Outlook web compose URL with every query value percent-encoded."""
    recipients = _addresses(to, "To")
    copy_recipients = _addresses(cc, "Cc")
    if not recipients and not copy_recipients:
        raise ComposeError("At least one recipient is required")
    _validate_content(subject=subject, body=body)

    params = [f"to={quote(';'.join(recipients), safe='')}"]
    if copy_recipients:
        params.append(f"cc={quote(';'.join(copy_recipients), safe='')}")
    params.extend((f"subject={quote(subject, safe='')}", f"body={quote(body, safe='')}"))
    return "https://outlook.office.com/mail/deeplink/compose?" + "&".join(params)


def fits_link(url: str) -> bool:
    """Return whether a compose URL is within the supported length limit."""
    return len(url) <= LINK_LIMIT


_PREFIX_RE = re.compile(
    r"^(?:(?:re|fwd|fw|ats|atsakymas|yanıt|ynt|ılt)\s*:\s*)+",
    flags=re.IGNORECASE,
)


def normalize_subject(subject: str) -> str:
    """Remove leading reply/forward prefixes and normalize case and whitespace."""
    if not isinstance(subject, str):
        raise ComposeError("Subject must be text")
    normalized = subject.strip()
    while True:
        without_prefix = _PREFIX_RE.sub("", normalized, count=1).strip()
        if without_prefix == normalized:
            break
        normalized = without_prefix
    return " ".join(normalized.casefold().split())

