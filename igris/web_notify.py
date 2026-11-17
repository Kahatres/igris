"""The owner's notification channel for the web server: the Telegram chat, the channel
settings and the loop that reads approval answers and messages.

Depends on telegram_chat (imported where it is used).
"""

import sys
from pathlib import Path

ANSWER_POLL_INTERVAL = 5.0


def build_telegram_chat(assistant, notifier, accounts, contexts, owner, *, agenda=None,
                        inbox=None, approvals=None, calendar_sync=None):
    """The owner's Telegram chat: free text goes to their assistant in the ``telegram``
    session (never the session their browser has selected), shortcuts read the stores."""
    from igris.telegram_chat import CHANNEL_NOTE, SESSION, TelegramChat

    def ask(text):
        owner_id = owner()
        if not owner_id:
            raise RuntimeError("no owner account")
        return contexts.ask_in_session(accounts.user(owner_id), SESSION, text, note=CHANNEL_NOTE)

    health = getattr(getattr(assistant, "router", None), "health", None)
    return TelegramChat(
        notifier, ask, owner=owner, agenda=agenda,
        inbox=getattr(inbox, "store", None), approvals=approvals,
        inbox_status=getattr(inbox, "status", None),
        calendar_status=calendar_sync.status if calendar_sync is not None else None,
        model_status=health if callable(health) else None,
        morning_hour=getattr(assistant.settings, "morning_hour", 8),
        state_path=Path(assistant.settings.data_dir) / "telegram_chat.json")


def channel_settings(get) -> dict:
    """Notification settings under the names notify.from_env reads."""
    names = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_TOPIC")
    return {"IGRIS_" + name: value for name in names if (value := get(name))}


def pump_answers(flow, stop, interval=ANSWER_POLL_INTERVAL, tick=None) -> None:
    """Read the channel (approval answers and owner messages, one poll) on its own thread:
    a slow network must not hold up tasks. ``tick`` (the morning summary) runs after it."""
    last = None
    while not stop.wait(interval):
        try:
            flow.pump()
            last = None
        except Exception as exc:  # NotifyError text is already free of the bot token
            if str(exc) != last:
                print(f"igris: approval poll failed: {exc}", file=sys.stderr)
            last = str(exc)
        if tick is not None:
            try:
                tick()
            except Exception as exc:  # the summary logs its own failures; never end the loop
                print(f"igris: morning summary failed: {type(exc).__name__}", file=sys.stderr)
