from email import message_from_bytes, policy
from urllib.parse import parse_qs, urlsplit

import pytest

from igris.mail_compose import (
    ComposeError,
    LINK_LIMIT,
    build_message,
    fits_link,
    normalize_subject,
    outlook_compose_url,
)


def test_round_trip_keeps_non_ascii():
    subject = "Etkinlik duyurusu: ÅŸÄŸÄ±Ä° Ä—Å¾"
    body = "Merhaba, ÅŸÄŸÄ±Ä° Ä—Å¾"

    raw = build_message(to=["ece@example.lt"], subject=subject, body=body)

    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed["Subject"] == subject
    assert parsed.get_content() == body


def test_recipients_in_headers():
    raw = build_message(
        to=["ece@example.lt", "deniz@example.lt"],
        cc=["team@example.lt"],
        subject="Hello",
        body="Hi",
    )

    parsed = message_from_bytes(raw, policy=policy.default)
    assert "ece@example.lt" in str(parsed["To"])
    assert "deniz@example.lt" in str(parsed["To"])
    assert "team@example.lt" in str(parsed["Cc"])


def test_reply_headers_set():
    raw = build_message(
        to=["ece@example.lt"],
        subject="Re: Hello",
        body="Hi",
        in_reply_to="<message-1@example.lt>",
        references="<message-0@example.lt> <message-1@example.lt>",
    )

    parsed = message_from_bytes(raw, policy=policy.default)
    assert parsed["In-Reply-To"] == "<message-1@example.lt>"
    assert parsed["References"] == "<message-0@example.lt> <message-1@example.lt>"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"subject": "hello\nBcc: victim@example.lt"},
        {"subject": "hello\rBcc: victim@example.lt"},
        {"to": ["victim@example.lt\nBcc: other@example.lt"]},
        {"in_reply_to": "<id@example.lt>\nBcc: victim@example.lt"},
        {"references": "<id@example.lt>\rBcc: victim@example.lt"},
    ],
)
def test_rejects_header_injection(kwargs):
    values = {
        "to": ["ece@example.lt"],
        "subject": "Hello",
        "body": "Hi",
    }
    values.update(kwargs)
    with pytest.raises(ComposeError):
        build_message(**values)


def test_rejects_empty():
    with pytest.raises(ComposeError):
        build_message(to=[], subject="Hello", body="Hi")
    with pytest.raises(ComposeError):
        build_message(to=["ece@example.lt"], subject="", body="Hi")
    with pytest.raises(ComposeError):
        build_message(to=["ece@example.lt"], subject="Hello", body="")


def test_outlook_url_encodes_everything():
    url = outlook_compose_url(
        to=["a@x.lt", "b@x.lt"],
        cc=[],
        subject="Soru?",
        body="a&b=c\nsatÄ±r #2",
    )

    query = parse_qs(urlsplit(url).query)
    assert query["to"] == ["a@x.lt;b@x.lt"]
    assert query["subject"] == ["Soru?"]
    assert query["body"] == ["a&b=c\nsatÄ±r #2"]
    assert "cc" not in query


def test_fits_link_boundary():
    assert fits_link("x" * LINK_LIMIT)
    assert not fits_link("x" * (LINK_LIMIT + 1))


def test_normalize_subject():
    assert normalize_subject("RE: Fwd:  Ats: Game Night ") == normalize_subject("game night")
