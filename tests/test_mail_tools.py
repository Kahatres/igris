"""The recent-mail summary and the owner's mail_recent tool, over a fake inbox store."""

from datetime import datetime

import pytest

from igris.mail_tools import mail_text, recent_mail, register_mail_tools
from igris.tools import ToolError, ToolRegistry

NOW = datetime(2026, 11, 12, 9, 0).astimezone()


class FakeStore:
    """Rows as InboxStore.recent returns them, plus a body the summary must never show."""

    def __init__(self, rows):
        self.rows, self.limits = rows, []

    def recent(self, limit=50):
        self.limits.append(limit)
        return self.rows[:limit]


def row(seen, category, sender="A <a@x.com>", subject="Konu", task=None):
    return {"id": seen + str(category), "seen_at": seen, "sender": sender, "subject": subject,
            "category": category, "reason": "sebep", "task": task,
            "body": "GİZLİ GÖVDE METNİ"}


ROWS = [
    row("2026-11-12T08:00:00", "gorev", "Ev sahibi <ev@x.com>", "Kira ödemesi",
        {"title": "Kirayı öde", "due": "2026-11-15"}),
    row("2026-11-12T07:00:00", "guvenlik", "Google <no-reply@google.com>", "Yeni oturum açma"),
    row("2026-11-11T20:00:00", "bilgi"),
    row("2026-11-11T19:00:00", "tanitim"),
    row("2026-11-11T18:00:00", None),
    row("2026-11-10T08:00:00", "gorev", subject="Eski görev"),  # older than 24 hours
]


def test_summary_counts_the_last_24_hours_per_category():
    summary = recent_mail(FakeStore(ROWS), 24, now=lambda: NOW)
    assert summary["total"] == 5
    assert summary["counts"] == {"gorev": 1, "guvenlik": 1, "bilgi": 1, "rutin": 0,
                                 "tanitim": 1, "diger": 1}
    assert summary["gorev"] == [{"sender": "Ev sahibi <ev@x.com>", "subject": "Kira ödemesi",
                                 "task": "Kirayı öde", "due": "15.11.2026"}]
    assert [r["subject"] for r in summary["guvenlik"]] == ["Yeni oturum açma"]


def test_text_names_counts_and_headers_but_never_a_body():
    text = mail_text(recent_mail(FakeStore(ROWS), 24, now=lambda: NOW))
    assert text.startswith("Son 24 saatte 5 mail: görev 1, güvenlik 1, bilgi 1, rutin 0, "
                           "tanıtım 1, diğer 1.")
    assert "• Ev sahibi <ev@x.com> — Kira ödemesi (son tarih: 15.11.2026)" in text
    assert "Yeni oturum açma" in text
    assert "GÖVDE" not in text and "sebep" not in text and "Eski görev" not in text


def test_empty_window():
    assert mail_text(recent_mail(FakeStore([]), 24, now=lambda: NOW)) == "Son 24 saatte mail yok."


def test_tool_is_read_only_and_checks_its_hours():
    registry = ToolRegistry()
    store = FakeStore(ROWS)
    register_mail_tools(registry, store, now=lambda: NOW)
    assert registry.names() == ["mail_recent"]
    out = registry.run("mail_recent", {"hours": 72})
    assert "Son 72 saatte 6 mail" in out and "GÖVDE" not in out
    assert registry.run("mail_recent", {}).startswith("Son 24 saatte 5 mail")
    for bad in (0, 169, "x"):
        with pytest.raises(ToolError):
            registry.run("mail_recent", {"hours": bad})


def test_mail_recent_exposes_received_ids_even_for_information_mail():
    registry = ToolRegistry()
    register_mail_tools(registry, FakeStore(ROWS), now=lambda: NOW)
    text = registry.run('mail_recent', {})
    assert ROWS[0]['id'] in text and ROWS[2]['id'] in text
    assert ROWS[2]['subject'] in text
    assert ROWS[2]['body'] not in text
