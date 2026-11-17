"""Tests for the notification module; no real network access is used."""

from __future__ import annotations

import json

import pytest

from igris.notify import (
    Answer,
    NotifyError,
    NtfyNotifier,
    TelegramNotifier,
    from_env,
)

TOKEN = "123456:SECRET-TOKEN-VALUE"


class FakeResponse:
    def __init__(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            self._payload = bytes(payload)
        else:
            self._payload = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self):
        return self._payload

    def close(self):
        self.closed = True


class FakeOpener:
    """Callable opener that records requests and replays canned responses."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)

    @property
    def last(self):
        return self.requests[-1][0]

    def last_body(self):
        return json.loads(self.last.data.decode("utf-8"))


def make_telegram(*responses, chat_id="42"):
    opener = FakeOpener(*responses)
    notifier = TelegramNotifier(TOKEN, chat_id, opener=opener)
    return notifier, opener


def test_send_builds_url_and_json_body():
    notifier, opener = make_telegram({"ok": True, "result": {}})

    notifier.send("hello")

    request = opener.last
    assert request.full_url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert request.get_method() == "POST"
    body = opener.last_body()
    assert body == {"chat_id": "42", "text": "hello"}


def test_ask_sends_two_inline_buttons():
    notifier, opener = make_telegram({"ok": True, "result": {}})

    notifier.ask("Deploy now?", "req-1")

    body = opener.last_body()
    assert body["chat_id"] == "42"
    assert body["text"] == "Deploy now?"
    keyboard = body["reply_markup"]["inline_keyboard"]
    assert len(keyboard) == 1
    buttons = keyboard[0]
    assert [button["text"] for button in buttons] == ["✅ Onayla", "❌ Reddet"]
    assert [button["callback_data"] for button in buttons] == [
        "approve:req-1",
        "deny:req-1",
    ]


def test_long_text_is_cut_to_telegram_limit():
    notifier, opener = make_telegram({"ok": True, "result": {}})

    notifier.send("x" * 10000)

    body = opener.last_body()
    assert len(body["text"]) <= 4096


def test_poll_answers_ignores_foreign_and_malformed_callbacks():
    updates = {
        "ok": True,
        "result": [
            {
                "update_id": 10,
                "callback_query": {
                    "id": "a",
                    "from": {"id": 42},
                    "message": {"chat": {"id": 42, "type": "private"}},
                    "data": "approve:req-1",
                },
            },
            {
                "update_id": 11,
                "callback_query": {
                    "id": "b",
                    "from": {"id": 42},
                    "message": {"chat": {"id": 99, "type": "private"}},
                    "data": "approve:req-2",
                },
            },
            {
                "update_id": 12,
                "callback_query": {
                    "id": "c",
                    "from": {"id": 42},
                    "message": {"chat": {"id": 42, "type": "private"}},
                    "data": "garbage-without-separator",
                },
            },
            {
                "update_id": 13,
                "callback_query": {
                    "id": "d",
                    "from": {"id": 7},
                    "message": {"chat": {"id": 42, "type": "private"}},
                    "data": "deny:req-3",
                },
            },
            {
                "update_id": 14,
                "callback_query": {
                    "id": "e",
                    "from": {"id": 42},
                    "message": {"chat": {"id": 42, "type": "private"}},
                    "data": "deny:req-4",
                },
            },
        ],
    }
    notifier, opener = make_telegram(updates)

    answers, next_offset = notifier.poll_answers(offset=7)

    assert answers == [
        Answer(request_id="req-1", approved=True, source="telegram"),
        Answer(request_id="req-4", approved=False, source="telegram"),
    ]
    assert next_offset == 15
    assert opener.last.full_url.endswith("/getUpdates")
    assert opener.last_body()["offset"] == 7


def test_poll_answers_ignores_group_chat():
    updates = {
        "ok": True,
        "result": [
            {
                "update_id": 20,
                "callback_query": {
                    "id": "g",
                    "from": {"id": 42},
                    "message": {"chat": {"id": 42, "type": "group"}},
                    "data": "approve:req-1",
                },
            },
        ],
    }
    notifier, _ = make_telegram(updates)

    answers, next_offset = notifier.poll_answers()

    assert answers == []
    assert next_offset == 21


def test_poll_answers_ignores_foreign_sender():
    updates = {
        "ok": True,
        "result": [
            {
                "update_id": 30,
                "callback_query": {
                    "id": "f",
                    "from": {"id": 999},
                    "message": {"chat": {"id": 42, "type": "private"}},
                    "data": "approve:req-1",
                },
            },
        ],
    }
    notifier, _ = make_telegram(updates)

    answers, _ = notifier.poll_answers()

    assert answers == []


def test_poll_answers_keeps_replayed_answer_in_one_batch():
    """ApprovalStore rejects replays, so every answer must be reported."""
    callback = {
        "id": "r",
        "from": {"id": 42},
        "message": {"chat": {"id": 42, "type": "private"}},
        "data": "approve:req-1",
    }
    updates = {
        "ok": True,
        "result": [
            {"update_id": 40, "callback_query": dict(callback)},
            {"update_id": 41, "callback_query": dict(callback)},
        ],
    }
    notifier, _ = make_telegram(updates)

    answers, next_offset = notifier.poll_answers()

    expected = Answer(request_id="req-1", approved=True, source="telegram")
    assert answers == [expected, expected]
    assert next_offset == 42


def test_from_env_rejects_non_positive_chat_id():
    for chat_id in ("-5", "0", "abc", "42.5", ""):
        with pytest.raises(NotifyError):
            from_env(
                {"IGRIS_TELEGRAM_BOT_TOKEN": TOKEN, "IGRIS_TELEGRAM_CHAT_ID": chat_id}
            )


def test_poll_answers_without_offset_and_empty_result():
    notifier, opener = make_telegram({"ok": True, "result": []})

    answers, next_offset = notifier.poll_answers()

    assert answers == []
    assert next_offset is None
    assert "offset" not in opener.last_body()


def test_poll_answers_ignores_malformed_update_shapes():
    updates = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"text": "hi"}},
            {"update_id": 2, "callback_query": "nope"},
            {
                "update_id": 3,
                "callback_query": {
                    "from": {"id": 42},
                    "message": {"chat": {"id": 42}},
                    "data": "unknown:req-5",
                },
            },
        ],
    }
    notifier, _ = make_telegram(updates)

    answers, next_offset = notifier.poll_answers()

    assert answers == []
    assert next_offset == 4


def test_token_is_redacted_from_error_messages():
    error = Exception(f"HTTP 500 for https://api.telegram.org/bot{TOKEN}/sendMessage")
    notifier, _ = make_telegram(error)

    with pytest.raises(NotifyError) as excinfo:
        notifier.send("hello")

    message = str(excinfo.value)
    assert TOKEN not in message
    assert "<redacted>" in message


def test_telegram_api_error_is_redacted_and_raised():
    notifier, _ = make_telegram({"ok": False, "description": "bad request"})

    with pytest.raises(NotifyError) as excinfo:
        notifier.send("hello")

    assert "bad request" in str(excinfo.value)


def test_invalid_json_raises_notify_error():
    notifier, _ = make_telegram(b"not json")

    with pytest.raises(NotifyError):
        notifier.send("hello")


def test_ntfy_send_url_and_body():
    opener = FakeOpener(b"ok")
    notifier = NtfyNotifier("my-topic", opener=opener)

    notifier.send("hello")

    request = opener.last
    assert request.full_url == "https://ntfy.sh/my-topic"
    assert request.get_method() == "POST"
    assert request.data == b"hello"


def test_ntfy_custom_server_and_ask_names_request_id():
    opener = FakeOpener(b"ok")
    notifier = NtfyNotifier("alerts", server="https://ntfy.example.com/", opener=opener)

    notifier.ask("Approve?", "req-9")

    assert opener.last.full_url == "https://ntfy.example.com/alerts"
    assert b"Approve?" in opener.last.data
    assert b"req-9" in opener.last.data


def test_ntfy_failure_raises_notify_error():
    opener = FakeOpener(Exception("connection refused"))
    notifier = NtfyNotifier("topic", opener=opener)

    with pytest.raises(NotifyError):
        notifier.send("hello")


def test_from_env_prefers_telegram():
    notifier = from_env(
        {
            "IGRIS_TELEGRAM_BOT_TOKEN": TOKEN,
            "IGRIS_TELEGRAM_CHAT_ID": "42",
            "IGRIS_NTFY_TOPIC": "topic",
        }
    )

    assert isinstance(notifier, TelegramNotifier)
    assert notifier.token == TOKEN
    assert notifier.chat_id == "42"


def test_from_env_falls_back_to_ntfy():
    notifier = from_env({"IGRIS_NTFY_TOPIC": "topic"})

    assert isinstance(notifier, NtfyNotifier)
    assert notifier.topic == "topic"


def test_from_env_returns_none_when_unconfigured():
    assert from_env({}) is None
    assert from_env({"IGRIS_TELEGRAM_BOT_TOKEN": TOKEN}) is None


def pressed(**changes):
    fields = {"request_id": "req-1", "approved": True, "source": "telegram",
              "callback_id": "cb-1", "message_id": 77, "message_text": "Deploy now?"}
    return Answer(**{**fields, **changes})


def sent(opener):
    return [(request.full_url.rsplit("/", 1)[1], json.loads(request.data.decode("utf-8")))
            for request, _ in opener.requests]


def test_poll_answers_keeps_what_a_reply_to_the_press_needs():
    updates = {
        "ok": True,
        "result": [
            {
                "update_id": 40,
                "callback_query": {
                    "id": "cb-1",
                    "from": {"id": 42},
                    "message": {"message_id": 77, "text": "Deploy now?",
                                "chat": {"id": 42, "type": "private"}},
                    "data": "approve:req-1",
                },
            },
        ],
    }
    notifier, _ = make_telegram(updates)

    answers, _ = notifier.poll_answers()

    assert (answers[0].callback_id, answers[0].message_id, answers[0].message_text) == (
        "cb-1", 77, "Deploy now?")


def test_acknowledge_stops_the_spinner_and_replaces_the_buttons_with_the_result():
    notifier, opener = make_telegram({"ok": True, "result": True}, {"ok": True, "result": {}})

    notifier.acknowledge(pressed(), "✅ Onaylandı")

    assert sent(opener) == [
        ("answerCallbackQuery",
         {"callback_query_id": "cb-1", "text": "✅ Onaylandı", "show_alert": False}),
        ("editMessageText",
         {"chat_id": "42", "message_id": 77, "text": "Deploy now?\n\n✅ Onaylandı"}),
    ]


def test_acknowledge_warns_that_a_late_press_changed_nothing():
    notifier, opener = make_telegram({"ok": True, "result": True}, {"ok": True, "result": {}})

    notifier.acknowledge(pressed(approved=False), "✅ Onaylandı", already_answered=True)

    call = sent(opener)[0][1]
    assert call["show_alert"] is True
    assert call["text"] == "Yanıt değişmedi: ✅ Onaylandı"


def test_acknowledge_still_updates_the_message_when_the_press_is_too_old():
    notifier, opener = make_telegram({"ok": False, "description": "query is too old"},
                                     {"ok": True, "result": {}})

    with pytest.raises(NotifyError):
        notifier.acknowledge(pressed(), "✅ Onaylandı")

    assert [method for method, _ in sent(opener)] == ["answerCallbackQuery", "editMessageText"]


def test_acknowledge_keeps_the_result_on_a_full_length_message():
    notifier, opener = make_telegram({"ok": True, "result": True}, {"ok": True, "result": {}})

    notifier.acknowledge(pressed(message_text="x" * 5000), "❌ Reddedildi")

    text = sent(opener)[1][1]["text"]
    assert len(text) <= 4096
    assert text.endswith("❌ Reddedildi")


# -- one poll for answers and owner messages -------------------------------------


def _callback(update_id, data="approve:req-1", chat=42, sender=42):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "from": {"id": sender},
        "message": {"message_id": 5, "chat": {"id": chat, "type": "private"}, "text": "?"},
        "data": data}}


def _message(update_id, chat=42, sender=42, chat_type="private", **content):
    content = content or {"text": "bugün ne var?"}
    return {"update_id": update_id, "message": {
        "message_id": 100 + update_id, "from": {"id": sender, "is_bot": False},
        "chat": {"id": chat, "type": chat_type}, **content}}


def test_poll_updates_yields_answers_and_text_from_one_call():
    from igris.notify import TextMessage

    notifier, opener = make_telegram({"ok": True, "result": [_callback(20), _message(21)]})

    answers, messages, next_offset = notifier.poll_updates(offset=20)

    assert answers == [Answer(request_id="req-1", approved=True, source="telegram")]
    assert messages == [TextMessage(text="bugün ne var?", kind="text")]
    assert messages[0].message_id == 121
    assert next_offset == 22
    assert len(opener.requests) == 1  # one getUpdates, the offset advanced once
    assert opener.last.full_url.endswith("/getUpdates")
    assert opener.last_body() == {"offset": 20}


def test_poll_updates_ignores_text_from_another_chat_or_sender():
    updates = [_message(1, chat=99), _message(2, sender=7),
               _message(3, chat=-42, chat_type="group"),
               {"update_id": 4, "edited_message": _message(4)["message"]},
               {"update_id": 5, "message": "junk"}]
    notifier, _ = make_telegram({"ok": True, "result": updates})

    answers, messages, next_offset = notifier.poll_updates()

    assert answers == [] and messages == [] and next_offset == 6


def test_poll_updates_marks_non_text_messages():
    updates = [_message(1, photo=[{"file_id": "x"}]), _message(2, voice={"file_id": "v"}),
               _message(3, sticker={"file_id": "s"}), _message(4, document={"file_id": "d"})]
    notifier, _ = make_telegram({"ok": True, "result": updates})

    _, messages, _ = notifier.poll_updates()

    assert [m.kind for m in messages] == ["other"] * 4
    assert all(m.text == "" for m in messages)


def test_poll_answers_still_returns_only_answers():
    notifier, _ = make_telegram({"ok": True, "result": [_callback(1), _message(2)]})

    answers, next_offset = notifier.poll_answers()

    assert [a.request_id for a in answers] == ["req-1"] and next_offset == 3


def test_send_chat_action_posts_typing():
    notifier, opener = make_telegram({"ok": True, "result": True})

    notifier.send_chat_action()

    assert opener.last.full_url.endswith("/sendChatAction")
    assert opener.last_body() == {"chat_id": "42", "action": "typing"}


def test_token_is_redacted_from_errors_of_the_new_calls():
    for call in (lambda n: n.poll_updates(), lambda n: n.send_chat_action()):
        notifier, _ = make_telegram(OSError(f"connection to /bot{TOKEN}/x refused"))
        with pytest.raises(NotifyError) as info:
            call(notifier)
        assert TOKEN not in str(info.value) and "<redacted>" in str(info.value)
    notifier, _ = make_telegram({"ok": False, "description": f"bad token {TOKEN}"})
    with pytest.raises(NotifyError) as info:
        notifier.poll_updates()
    assert TOKEN not in str(info.value)
