"""Outgoing notifications, yes/no answers and owner messages through Telegram and ntfy.

The module only depends on the standard library. Callers may inject an
``opener`` compatible with :func:`urllib.request.urlopen`, which keeps the
tests free of real network access.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = 15


class NotifyError(RuntimeError):
    """Raised when a notification cannot be delivered or parsed."""


@dataclass(frozen=True)
class Answer:
    """A yes/no answer collected from a notification channel."""

    request_id: str
    approved: bool
    source: str
    # What a reply to the button press needs; not part of the answer itself.
    callback_id: str | None = field(default=None, compare=False)
    message_id: int | None = field(default=None, compare=False)
    message_text: str = field(default="", compare=False)


@dataclass(frozen=True)
class TextMessage:
    """A message the owner wrote to the bot in their private chat.

    ``kind`` is ``"text"`` for a text message and ``"other"`` for anything Igris cannot
    read yet (a photo, voice note, sticker or document); ``text`` is empty then.
    """

    text: str
    kind: str = "text"
    message_id: int | None = field(default=None, compare=False)


def _truncate(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Cut ``text`` so that it never exceeds Telegram's message limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


def _read_response(response: Any) -> bytes:
    """Read and close a response object returned by an opener."""
    try:
        raw = response.read()
    finally:
        closer = getattr(response, "close", None)
        if callable(closer):
            closer()
    return raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")


class TelegramNotifier:
    """Send messages and collect inline-keyboard answers through Telegram."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        opener: Any = urllib.request.urlopen,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.token = str(token)
        self.chat_id = str(chat_id)
        self._opener = opener
        self.timeout = timeout

    # -- internals ---------------------------------------------------------

    def _redact(self, message: str) -> str:
        """Remove the bot token from a message before exposing it."""
        text = str(message)
        if self.token:
            text = text.replace(self.token, "<redacted>")
        return text

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.token}/{method}"

    def _call(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Perform a Telegram Bot API call and return the decoded response."""
        url = self._url(method)
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = self._opener(request, timeout=self.timeout)
            raw = _read_response(response)
        except Exception as exc:  # noqa: BLE001 - wrapped in NotifyError
            raise NotifyError(
                self._redact(f"Telegram {method} request failed: {exc}")
            ) from None

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NotifyError(
                self._redact(f"Telegram {method} returned invalid JSON: {exc}")
            ) from None

        if not isinstance(parsed, dict) or not parsed.get("ok", False):
            description = parsed.get("description") if isinstance(parsed, dict) else None
            raise NotifyError(
                self._redact(
                    f"Telegram {method} failed: {description or 'unknown error'}"
                )
            )
        return parsed

    def _parse_callback(self, update: Any) -> Answer | None:
        """Turn one update into an answer, ignoring anything untrusted.

        Only a private chat with the owner may answer: the message chat must
        be ``private``, the chat id must match ``chat_id`` and the sender must
        be the very same account. Group chats keep their own members from
        approving, and their ids are negative.
        """
        if not isinstance(update, dict):
            return None
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return None

        message = callback.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return None
        if str(chat.get("id")) != self.chat_id:
            return None

        sender = callback.get("from")
        if not isinstance(sender, dict) or str(sender.get("id")) != self.chat_id:
            return None

        data = callback.get("data")
        if not isinstance(data, str):
            return None
        action, separator, request_id = data.partition(":")
        if not separator or not request_id:
            return None
        if action == "approve":
            approved = True
        elif action == "deny":
            approved = False
        else:
            return None

        return Answer(
            request_id=request_id,
            approved=approved,
            source="telegram",
            callback_id=callback.get("id"),
            message_id=message.get("message_id"),
            message_text=str(message.get("text") or ""),
        )

    def _parse_message(self, update: Any) -> TextMessage | None:
        """Turn one update into an owner message, ignoring anything untrusted.

        The same checks as for answers: a private chat whose id is ``chat_id`` and a
        sender that is that very account. Edited messages and channel posts are ignored.
        """
        if not isinstance(update, dict):
            return None
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return None
        if str(chat.get("id")) != self.chat_id:
            return None
        sender = message.get("from")
        if not isinstance(sender, dict) or str(sender.get("id")) != self.chat_id:
            return None
        if sender.get("is_bot"):
            return None
        message_id = message.get("message_id")
        message_id = message_id if isinstance(message_id, int) else None
        text = message.get("text")
        if isinstance(text, str) and text.strip():
            return TextMessage(text=text, kind="text", message_id=message_id)
        return TextMessage(text="", kind="other", message_id=message_id)

    # -- public API --------------------------------------------------------

    def send(self, text: str) -> None:
        """Send a plain text message."""
        self._call(
            "sendMessage",
            {"chat_id": self.chat_id, "text": _truncate(str(text))},
        )

    def ask(self, text: str, request_id: str) -> None:
        """Send a question with approve and deny inline buttons."""
        request_id = str(request_id)
        payload = {
            "chat_id": self.chat_id,
            "text": _truncate(str(text)),
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "✅ Onayla", "callback_data": f"approve:{request_id}"},
                        {"text": "❌ Reddet", "callback_data": f"deny:{request_id}"},
                    ]
                ]
            },
        }
        self._call("sendMessage", payload)

    def acknowledge(
        self, answer: Answer, result: str, *, already_answered: bool = False
    ) -> None:
        """Show the recorded result on the phone and take the buttons away.

        Telegram keeps a spinner on a pressed button until the press is
        answered. A late press changes nothing, so it gets a pop-up that says
        so. The message is updated even when the press is too old to answer.
        """
        text = f"Yanıt değişmedi: {result}" if already_answered else result
        try:
            if answer.callback_id:
                self._call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": answer.callback_id,
                        "text": text,
                        "show_alert": already_answered,
                    },
                )
        finally:
            if answer.message_id is not None:
                room = TELEGRAM_TEXT_LIMIT - len(result) - 2
                self._call(
                    "editMessageText",
                    {
                        "chat_id": self.chat_id,
                        "message_id": answer.message_id,
                        "text": f"{_truncate(answer.message_text, room)}\n\n{result}",
                    },
                )

    def send_chat_action(self, action: str = "typing") -> None:
        """Show a short-lived status such as "typing..." in the owner's chat."""
        self._call("sendChatAction", {"chat_id": self.chat_id, "action": str(action)})

    def poll_updates(
        self, offset: int | None = None
    ) -> tuple[list[Answer], list[TextMessage], int | None]:
        """Fetch new approval answers and owner messages with one getUpdates call.

        Telegram hands every update out once per offset, so this is the only call that
        may read updates: answers and messages come from the same poll. Returns the
        answers, the messages and a candidate offset for the next poll. Passing that
        offset confirms the whole batch: callers must durably accept messages first,
        and reuse the previous offset if acceptance fails.
        """
        payload: dict[str, Any] = {}
        if offset is not None:
            payload["offset"] = int(offset)

        parsed = self._call("getUpdates", payload)
        updates = parsed.get("result") or []
        if not isinstance(updates, list):
            raise NotifyError(
                self._redact("Telegram getUpdates returned an invalid result")
            )

        answers: list[Answer] = []
        messages: list[TextMessage] = []
        last_update_id: int | None = None
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                if last_update_id is None or update_id > last_update_id:
                    last_update_id = update_id
            answer = self._parse_callback(update)
            if answer is not None:
                answers.append(answer)
                continue
            message = self._parse_message(update)
            if message is not None:
                messages.append(message)

        next_offset = last_update_id + 1 if last_update_id is not None else offset
        return answers, messages, next_offset

    def poll_answers(
        self, offset: int | None = None
    ) -> tuple[list[Answer], int | None]:
        """Fetch new answers and the offset to use on the next poll.

        Owner text messages in the same batch are dropped; a caller that handles them
        uses :meth:`poll_updates` instead (never both).
        """
        answers, _messages, next_offset = self.poll_updates(offset)
        return answers, next_offset


class NtfyNotifier:
    """Send one-way notifications through an ntfy topic."""

    def __init__(
        self,
        topic: str,
        server: str = "https://ntfy.sh",
        opener: Any = urllib.request.urlopen,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.topic = str(topic)
        self.server = str(server).rstrip("/")
        self._opener = opener
        self.timeout = timeout

    def _url(self) -> str:
        return f"{self.server}/{self.topic}"

    def send(self, text: str) -> None:
        """Publish a plain text message to the topic."""
        request = urllib.request.Request(
            self._url(),
            data=str(text).encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        try:
            response = self._opener(request, timeout=self.timeout)
            _read_response(response)
        except Exception as exc:  # noqa: BLE001 - wrapped in NotifyError
            raise NotifyError(f"ntfy request failed: {exc}") from None

    def ask(self, text: str, request_id: str) -> None:
        """Send a question; ntfy cannot carry authenticated answers.

        Anyone who knows the topic name can read it, so the topic must be a
        long random name.
        """
        self.send(f"{text}\n\nRequest id: {request_id}")


def from_env(env: Mapping[str, str]):
    """Build a notifier from environment variables, or return ``None``."""
    token = env.get("IGRIS_TELEGRAM_BOT_TOKEN")
    chat_id = env.get("IGRIS_TELEGRAM_CHAT_ID")
    if chat_id is not None:
        # Group chats have negative ids, so only a positive id can be an
        # owner's private chat id.
        try:
            parsed_chat_id = int(str(chat_id).strip())
        except (TypeError, ValueError):
            raise NotifyError(
                "IGRIS_TELEGRAM_CHAT_ID must be a positive integer"
            ) from None
        if parsed_chat_id <= 0:
            raise NotifyError("IGRIS_TELEGRAM_CHAT_ID must be a positive integer")
        chat_id = str(parsed_chat_id)
    if token and chat_id:
        return TelegramNotifier(token, chat_id)

    topic = env.get("IGRIS_NTFY_TOPIC")
    if topic:
        return NtfyNotifier(topic)

    return None
