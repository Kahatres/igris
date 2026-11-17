"""gcal writing: Igris's own calendars only, through a fake opener (no network)."""

import io
import json
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from igris import gcal, gmail

SECRET, REFRESH, ACCESS = "client-secret-SENTINEL", "refresh-SENTINEL", "access-SENTINEL"


class FakeOpener:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return io.BytesIO(response if isinstance(response, bytes) else json.dumps(response).encode())


def token(**extra):
    return {"access_token": ACCESS, "expires_in": 3600, **extra}


def error(code, reason="x"):
    body = json.dumps({"error": {"message": SECRET + REFRESH + ACCESS}}).encode()
    return urllib.error.HTTPError("https://example.invalid", code, reason, {}, io.BytesIO(body))


def client(opener):
    return gcal.CalendarClient("id", SECRET, REFRESH, opener=opener, clock=lambda: 1000.0)


def test_scopes_ask_only_for_calendars_igris_creates():
    assert gcal.SCOPES == ("https://www.googleapis.com/auth/calendar.readonly",
                           "https://www.googleapis.com/auth/calendar.app.created")
    assert not any(s.endswith(("/calendar", "calendar.events")) for s in gcal.SCOPES)


def test_require_write_reads_the_granted_scopes():
    old = token(scope=gmail.READONLY_SCOPE + " " + gcal.CALENDAR_SCOPE)
    with pytest.raises(gcal.CalendarScopeError):
        client(FakeOpener(old)).require_write()
    client(FakeOpener(token(scope=" ".join((gmail.READONLY_SCOPE,) + gcal.SCOPES)))).require_write()
    client(FakeOpener(token())).require_write()  # Google did not say: try and see


def test_create_calendar_insert_update_delete_send_the_right_requests():
    opener = FakeOpener(token(), {"timeZone": "Europe/Vilnius"}, {"id": "cal@group"},
                        {"id": "e1"}, {"id": "e1"}, b"")
    c = client(opener)
    assert c.primary_time_zone() == "Europe/Vilnius"
    assert c.create_calendar("Igris · Okul", "Europe/Vilnius") == "cal@group"
    assert c.insert_event("cal@group", {"summary": "x"}) == {"id": "e1"}
    assert c.update_event("cal@group", "e1", {"summary": "y"}) == {"id": "e1"}
    assert c.delete_event("cal@group", "e1") is None
    requests = opener.calls[1:]
    assert [(r.get_method(), urlsplit(r.full_url).path) for r in requests] == [
        ("GET", "/calendar/v3/calendars/primary"),
        ("POST", "/calendar/v3/calendars"),
        ("POST", "/calendar/v3/calendars/cal%40group/events"),
        ("PUT", "/calendar/v3/calendars/cal%40group/events/e1"),
        ("DELETE", "/calendar/v3/calendars/cal%40group/events/e1"),
    ]
    assert json.loads(requests[1].data) == {"summary": "Igris · Okul", "timeZone": "Europe/Vilnius"}
    assert json.loads(requests[3].data) == {"summary": "y"}
    assert all(r.get_header("Authorization") == "Bearer " + ACCESS for r in requests)


def test_a_gone_event_and_a_missing_calendar_are_not_errors():
    c = client(FakeOpener(token(), error(410), error(404)))
    c.delete_event("cal", "e1")
    assert c.calendar_exists("cal") is False


def test_list_events_follows_pages():
    opener = FakeOpener(token(), {"items": [{"id": "a"}], "nextPageToken": "p2"},
                        {"items": [{"id": "b"}, "junk"]})
    assert [e["id"] for e in client(opener).list_events("cal")] == ["a", "b"]
    second = parse_qs(urlsplit(opener.calls[2].full_url).query)
    assert second["pageToken"] == ["p2"] and second["showDeleted"] == ["false"]


def test_write_errors_stay_safe():
    with pytest.raises(gcal.CalendarError) as raised:
        client(FakeOpener(token(), error(500))).insert_event("cal", {"summary": "x"})
    assert type(raised.value) is gcal.CalendarError
    for secret in (SECRET, REFRESH, ACCESS):
        assert secret not in str(raised.value)
