"""web_chat routes with a fake assistant; HTTP flows are in test_web."""
from types import SimpleNamespace

import pytest

from igris import __version__, web_chat
from igris.web_http import MAX_TEXT
from test_web import FakeAssistant
from web_serving import FakeRequest


def test_state_payload():
    payload = web_chat.state_payload(FakeAssistant())
    assert payload["version"] == __version__ and payload["tools"] == ["clock", "calc"]
    assert "agent_colors" not in payload


@pytest.mark.parametrize("data", [{}, {"text": " "}, {"text": "x" * (MAX_TEXT + 1)},
                                  {"text": "hi", "slow": "yes"}])
def test_ask_validates_the_request(data):
    assistant = FakeAssistant()
    h = FakeRequest(assistant=assistant)
    web_chat.ask(h, SimpleNamespace(accounts=None), "/api/ask", data)
    assert h.status == 400 and assistant.asked == []


def test_ask_and_switch():
    assistant = FakeAssistant()
    h = FakeRequest(assistant=assistant)
    web_chat.ask(h, SimpleNamespace(accounts=None), "/api/ask", {"text": "hi"})
    assert h.body["text"] == "echo hi" and h.body["job_id"] is None
    web_chat.switch(h, SimpleNamespace(accounts=None), "/api/persona", {"name": "pirate"})
    assert h.status == 200 and h.body["persona"] == "pirate"
    web_chat.switch(h, SimpleNamespace(accounts=None), "/api/session", {"name": "bad"})
    assert h.status == 400


def test_roadmap_uses_the_given_loader():
    h = FakeRequest()
    web_chat.roadmap(h, SimpleNamespace(roadmap_loader=lambda: {"plans": []}), "/api/roadmap")
    assert h.body == {"plans": []}
