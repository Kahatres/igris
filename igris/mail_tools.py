"""The owner's recent mail as counts and headers, for the chat tool and Telegram's /mail.

Reads only the inbox store's public ``recent(limit)`` rows: sender, subject, category and
the task found in a mail. The store never keeps a body, and nothing here asks for one.
The tool is registered for the owner only (``user_context``), because the inbox is theirs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from igris.tools import Tool, ToolError, ToolRegistry

DEFAULT_HOURS = 24
MAX_HOURS = 168
SCAN_LIMIT = 1000  # rows read from the store; far more than a day of mail
LIST_LIMIT = 15    # görev / güvenlik lines shown per category
CATEGORY_NAMES = (("gorev", "görev"), ("guvenlik", "güvenlik"), ("bilgi", "bilgi"),
                  ("rutin", "rutin"), ("tanitim", "tanıtım"))


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _naive_local(moment: datetime) -> datetime:
    """Stored stamps are naive local time; aware values are brought to that form."""
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment


def _short(value, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _when(value) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return moment.strftime("%d.%m.%Y") if len(value) == 10 else moment.strftime("%d.%m.%Y %H:%M")


def recent_mail(store, hours: int = DEFAULT_HOURS, *,
                now: Callable[[], datetime] = _local_now) -> dict:
    """Mail seen in the last ``hours``: total, counts per category and the görev and
    güvenlik mails as {sender, subject, task, due}. Never a body."""
    since = _naive_local(now()) - timedelta(hours=hours)
    counts = {key: 0 for key, _ in CATEGORY_NAMES}
    counts["diger"] = 0
    lists = {"gorev": [], "guvenlik": []}
    total = 0
    for row in store.recent(SCAN_LIMIT):
        try:
            seen = _naive_local(datetime.fromisoformat(str(row.get("seen_at") or "")))
        except ValueError:
            continue
        if seen < since:
            continue
        total += 1
        category = row.get("category")
        counts[category if category in counts else "diger"] += 1
        if category in lists:
            task = row.get("task") if isinstance(row.get("task"), dict) else {}
            lists[category].append({
                "sender": _short(row.get("sender")), "subject": _short(row.get("subject")),
                "task": _short(task.get("title")) if task else "",
                "due": _when(task.get("due") or task.get("start")) if task else "",
            })
    return {"hours": hours, "total": total, "counts": counts, **lists}


def mail_text(summary: dict) -> str:
    """The summary in Turkish, as Telegram's /mail and the chat tool show it."""
    hours = summary["hours"]
    if not summary["total"]:
        return f"Son {hours} saatte mail yok."
    counts = summary["counts"]
    parts = [f"{name} {counts[key]}" for key, name in CATEGORY_NAMES]
    if counts.get("diger"):
        parts.append(f"diğer {counts['diger']}")
    lines = [f"Son {hours} saatte {summary['total']} mail: " + ", ".join(parts) + "."]
    for key, title in (("gorev", "Görev mailleri"), ("guvenlik", "Güvenlik mailleri")):
        rows = summary[key]
        if not rows:
            continue
        lines.append(f"{title}:")
        for row in rows[:LIST_LIMIT]:
            line = f"• {row['sender'] or '-'} — {row['subject'] or '-'}"
            if row["due"]:
                line += f" (son tarih: {row['due']})"
            lines.append(line)
        if len(rows) > LIST_LIMIT:
            lines.append(f"…ve {len(rows) - LIST_LIMIT} mail daha (web arayüzünde)")
    return "\n".join(lines)


def register_mail_tools(registry: ToolRegistry, store, *,
                        now: Callable[[], datetime] = _local_now) -> None:
    """Register the read-only ``mail_recent`` tool over the owner's inbox store."""

    def mail_recent(hours=DEFAULT_HOURS) -> str:
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            raise ToolError("hours must be a whole number") from None
        if not 1 <= hours <= MAX_HOURS:
            raise ToolError(f"hours must be from 1 to {MAX_HOURS}")
        summary = mail_text(recent_mail(store, hours, now=now))
        since = _naive_local(now()) - timedelta(hours=hours)
        received = []
        for row in store.recent(SCAN_LIMIT):
            try:
                seen = _naive_local(datetime.fromisoformat(str(row.get('seen_at') or '')))
            except ValueError:
                continue
            if seen >= since:
                received.append(f"id={_short(row.get('id'), 128)} "
                                f"{_short(row.get('sender'))} — {_short(row.get('subject'))}")
        return summary + ('\nYanıt için mail kimlikleri:\n' + '\n'.join(received[:LIST_LIMIT])
                          if received else '')

    registry.add(Tool(
        name="mail_recent",
        description=("The owner's mail of the last hours (read-only): counts per category "
                     "(görev, güvenlik, bilgi, rutin, tanıtım) and sender, subject and due date "
                     "of task (görev) and security (güvenlik) mails. Mail bodies are never "
                     f"available. Args: hours={DEFAULT_HOURS} (1-{MAX_HOURS})."),
        func=mail_recent,
    ))
