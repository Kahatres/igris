import io
import json
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from igris import gcal, gmail


CLIENT_ID = "calendar-test-client"
CLIENT_SECRET = "private-client-secret-SENTINEL"
REFRESH_TOKEN = "private-refresh-token-SENTINEL"
ACCESS_TOKEN = "private-access-token-SENTINEL"
NOW = 1735689600  # 2025-01-01T00:00:00Z
SECRETS = (CLIENT_SECRET, REFRESH_TOKEN, ACCESS_TOKEN)
LEAK = " ".join(SECRETS)


class FakeOpener:
    """Record requests and consume responses without opening any sockets."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        assert self.responses, "Unexpected extra HTTP request"
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        body = response if isinstance(response, bytes) else json.dumps(response).encode()
        return io.BytesIO(body)


class FakeClock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def token(value=ACCESS_TOKEN):
    return {"access_token": value, "expires_in": 3600, "token_type": "Bearer"}


def http_error(code, reason, body=None):
    if body is None:
        body = {"error": {"message": LEAK}}
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return urllib.error.HTTPError(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        code,
        reason,
        {},
        io.BytesIO(raw),
    )


def client(opener, *, clock=None, timeout=30):
    return gcal.CalendarClient(
        CLIENT_ID,
        CLIENT_SECRET,
        REFRESH_TOKEN,
        opener=opener,
        clock=clock if clock is not None else FakeClock(),
        timeout=timeout,
    )


def assert_safe(message):
    for secret in SECRETS:
        assert secret not in str(message)


def assert_events_request(call, *, minimum, maximum, limit, access=ACCESS_TOKEN):
    request, timeout = call
    parts = urlsplit(request.full_url)
    assert parts.scheme == "https"
    assert parts.netloc == "www.googleapis.com"
    assert parts.path == "/calendar/v3/calendars/primary/events"
    assert not parts.fragment
    assert parse_qs(parts.query) == {
        "timeMin": [minimum],
        "timeMax": [maximum],
        "singleEvents": ["true"],
        "orderBy": ["startTime"],
        "maxResults": [str(limit)],
    }
    assert request.get_method() == "GET"
    assert request.data is None
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == "Bearer " + access
    assert CLIENT_SECRET not in request.full_url
    assert REFRESH_TOKEN not in request.full_url
    return timeout


def test_constants_and_exception_hierarchy():
    assert gcal.API_URL == "https://www.googleapis.com/calendar/v3"
    assert gcal.CALENDAR_SCOPE == "https://www.googleapis.com/auth/calendar.readonly"
    assert issubclass(gcal.CalendarError, RuntimeError)
    assert issubclass(gcal.CalendarAuthError, gcal.CalendarError)
    assert issubclass(gcal.CalendarScopeError, gcal.CalendarError)


def test_default_request_uses_clock_primary_calendar_and_default_timeout():
    opener = FakeOpener(token(), {"items": []})
    assert client(opener).upcoming() == []
    assert len(opener.calls) == 2
    refresh, timeout = opener.calls[0]
    assert refresh.get_method() == "POST"
    form = parse_qs(refresh.data.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["client_id"] == [CLIENT_ID]
    assert form["client_secret"] == [CLIENT_SECRET]
    assert form["refresh_token"] == [REFRESH_TOKEN]
    assert timeout == 30
    assert assert_events_request(
        opener.calls[1],
        minimum="2025-01-01T00:00:00Z",
        maximum="2025-01-15T00:00:00Z",
        limit=50,
    ) == 30
    assert not opener.responses


@pytest.mark.parametrize(
    "now,days,limit,minimum,maximum",
    [
        (NOW, 1, 1, "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z"),
        (NOW, 60, 250, "2025-01-01T00:00:00Z", "2025-03-02T00:00:00Z"),
        (0, 1, 1, "1970-01-01T00:00:00Z", "1970-01-02T00:00:00Z"),
        (1709164800, 1, 50, "2024-02-29T00:00:00Z", "2024-03-01T00:00:00Z"),
    ],
)
def test_explicit_now_boundaries_and_custom_timeout(now, days, limit, minimum, maximum):
    opener = FakeOpener(token(), {"items": []})
    assert client(opener, timeout=7).upcoming(
        now=now, days=days, max_results=limit
    ) == []
    assert assert_events_request(
        opener.calls[1], minimum=minimum, maximum=maximum, limit=limit
    ) == 7
    assert opener.calls[0][1] == 7


@pytest.mark.parametrize("name", ["days", "max_results"])
@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, -1, 1.0, 1.5, "1", "", [], {}, float("nan"),
     float("inf"), float("-inf")],
)
def test_invalid_arguments_raise_value_error_before_network(name, value):
    opener = FakeOpener()
    with pytest.raises(ValueError) as raised:
        client(opener).upcoming(**{name: value})
    assert_safe(raised.value)
    assert opener.calls == []


@pytest.mark.parametrize("arguments", [{"days": 61}, {"max_results": 251}])
def test_integer_upper_bounds_are_enforced(arguments):
    opener = FakeOpener()
    with pytest.raises(ValueError):
        client(opener).upcoming(**arguments)
    assert opener.calls == []


def test_access_token_is_cached_across_calls_but_time_window_is_not():
    clock = FakeClock()
    opener = FakeOpener(token(), {"items": []}, {"items": []})
    calendar = client(opener, clock=clock)
    assert calendar.upcoming() == []
    clock.value += 864
    assert calendar.upcoming() == []
    assert len(opener.calls) == 3
    assert_events_request(
        opener.calls[2],
        minimum="2025-01-01T00:14:24Z",
        maximum="2025-01-15T00:14:24Z",
        limit=50,
    )


@pytest.mark.parametrize("elapsed", [3599, 3601])
def test_token_refreshes_shortly_before_or_after_expiry(elapsed):
    clock = FakeClock()
    opener = FakeOpener(token(), {"items": []}, token("replacement"), {"items": []})
    calendar = client(opener, clock=clock)
    calendar.upcoming(now=NOW)
    clock.value += elapsed
    calendar.upcoming(now=NOW)
    assert len(opener.calls) == 4
    assert opener.calls[2][0].get_method() == "POST"
    assert_events_request(
        opener.calls[3],
        minimum="2025-01-01T00:00:00Z",
        maximum="2025-01-15T00:00:00Z",
        limit=50,
        access="replacement",
    )


def test_401_invalidates_cached_token_retries_once_and_caches_replacement():
    opener = FakeOpener(
        token(),
        {"items": []},
        http_error(401, "Unauthorized"),
        token("replacement"),
        {"items": []},
        {"items": []},
    )
    calendar = client(opener)
    assert calendar.upcoming() == []
    assert calendar.upcoming() == []
    assert calendar.upcoming() == []
    assert len(opener.calls) == 6
    assert opener.calls[2][0].get_header("Authorization") == "Bearer " + ACCESS_TOKEN
    assert opener.calls[3][0].get_method() == "POST"
    for index in (4, 5):
        assert_events_request(
            opener.calls[index],
            minimum="2025-01-01T00:00:00Z",
            maximum="2025-01-15T00:00:00Z",
            limit=50,
            access="replacement",
        )


def test_double_401_is_calendar_auth_error_without_another_retry(capsys):
    opener = FakeOpener(
        token(),
        http_error(401, "Unauthorized"),
        token("replacement"),
        http_error(401, "Unauthorized"),
    )
    with pytest.raises(gcal.CalendarAuthError) as raised:
        client(opener).upcoming()
    assert len(opener.calls) == 4
    assert_safe(raised.value)
    captured = capsys.readouterr()
    assert_safe(captured.out + captured.err)


@pytest.mark.parametrize(
    "error",
    [
        {"errors": [{"reason": "insufficientPermissions"}], "message": LEAK},
        {"errors": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}], "message": LEAK},
        {
            "details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
            }],
            "message": LEAK,
        },
        {"message": "Request had insufficient authentication scopes. " + LEAK},
        {"message": "Required SCOPE was not granted. " + LEAK},
    ],
)
def test_scope_related_403_has_distinct_safe_error(error, capsys):
    opener = FakeOpener(token(), http_error(403, "Forbidden", {"error": error}))
    with pytest.raises(gcal.CalendarScopeError) as raised:
        client(opener).upcoming()
    assert len(opener.calls) == 2
    assert_safe(raised.value)
    captured = capsys.readouterr()
    assert_safe(captured.out + captured.err)


@pytest.mark.parametrize(
    "code,reason,body",
    [
        (500, "Internal Server Error", None),
        (403, "Forbidden", {"error": {
            "errors": [{"reason": "dailyLimitExceeded"}], "message": LEAK,
        }}),
        (429, "Too Many Requests", None),
        (500, "Internal Server Error", {"error": {
            "message": "scope " + LEAK,
            "errors": [{"reason": "insufficientPermissions"}],
        }}),
    ],
)
def test_other_http_errors_have_exact_safe_message(code, reason, body, capsys):
    opener = FakeOpener(token(), http_error(code, reason, body))
    with pytest.raises(gcal.CalendarError) as raised:
        client(opener).upcoming()
    assert type(raised.value) is gcal.CalendarError
    assert str(raised.value) == f"Calendar API returned HTTP {code} {reason}"
    assert len(opener.calls) == 2
    assert_safe(raised.value)
    captured = capsys.readouterr()
    assert_safe(captured.out + captured.err)


@pytest.mark.parametrize("exception_type", [urllib.error.URLError, OSError, TimeoutError])
def test_network_errors_use_class_name_not_exception_details(exception_type, capsys):
    opener = FakeOpener(token(), exception_type(LEAK))
    with pytest.raises(gcal.CalendarError) as raised:
        client(opener).upcoming()
    assert type(raised.value) is gcal.CalendarError
    assert str(raised.value) == f"Calendar API unreachable: {exception_type.__name__}"
    assert len(opener.calls) == 2
    assert_safe(raised.value)
    captured = capsys.readouterr()
    assert_safe(captured.out + captured.err)


def refresh_failure(kind):
    if kind == "refused":
        return http_error(400, "Bad Request", {
            "error": "invalid_grant", "error_description": LEAK,
        })
    if kind == "server":
        return http_error(500, "Internal Server Error")
    return urllib.error.URLError(LEAK)


@pytest.mark.parametrize("kind", ["refused", "server", "network"])
def test_refresh_errors_preserve_gmail_safe_message_and_translate_type(kind, capsys):
    reference_opener = FakeOpener(refresh_failure(kind))
    reference = gmail.GmailClient(
        CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN,
        opener=reference_opener, clock=FakeClock(),
    )
    with pytest.raises(gmail.GmailError) as reference_error:
        reference.list_ids("in:inbox", max_results=1)
    expected_type = (
        gcal.CalendarAuthError
        if isinstance(reference_error.value, gmail.GmailAuthError)
        else gcal.CalendarError
    )
    if kind == "refused":
        assert expected_type is gcal.CalendarAuthError

    opener = FakeOpener(refresh_failure(kind))
    with pytest.raises(gcal.CalendarError) as raised:
        client(opener).upcoming()
    assert type(raised.value) is expected_type
    assert str(raised.value) == str(reference_error.value)
    assert len(opener.calls) == 1
    assert_safe(raised.value)
    captured = capsys.readouterr()
    assert_safe(captured.out + captured.err)


def test_refresh_refusal_during_401_retry_is_translated():
    opener = FakeOpener(
        token(), http_error(401, "Unauthorized"), refresh_failure("refused")
    )
    with pytest.raises(gcal.CalendarAuthError) as raised:
        client(opener).upcoming()
    assert len(opener.calls) == 3
    assert_safe(raised.value)


def test_normalization_preserves_api_order_and_skips_unusable_items():
    items = [
        {
            "id": "later",
            "summary": "Meeting",
            "start": {"dateTime": "2025-01-04T10:30:00+02:00"},
            "end": {"dateTime": "2025-01-04T11:30:00+02:00"},
            "location": "Room 2",
            "description": "Do not include extra fields",
        },
        {
            "id": "cancelled",
            "status": "cancelled",
            "start": {"date": "2025-01-02"},
        },
        None,
        "not an event",
        42,
        {},
        {"start": {"date": "2025-01-02"}},
        {"id": "", "start": {"date": "2025-01-02"}},
        {"id": "no-start"},
        {"id": "empty-start", "start": {}},
        {"id": "null-start", "start": None},
        {"id": "bad-start", "start": "2025-01-02"},
        {"id": "blank-start", "start": {"dateTime": "", "date": ""}},
        {
            "id": "earlier",
            "summary": "",
            "status": "tentative",
            "start": {"date": "2025-01-02"},
            "end": {"date": "2025-01-04"},
        },
        {
            "id": "untitled",
            "summary": None,
            "start": {"dateTime": "2025-01-05T12:00:00Z"},
            "end": {"dateTime": "2025-01-05T13:00:00Z"},
        },
        {"id": "no-end", "start": {"date": "2025-01-06"}},
    ]
    opener = FakeOpener(token(), {"items": items})
    assert client(opener).upcoming() == [
        {
            "id": "later", "title": "Meeting",
            "start": "2025-01-04T10:30:00+02:00",
            "end": "2025-01-04T11:30:00+02:00",
            "all_day": False, "location": "Room 2",
        },
        {
            "id": "earlier", "title": "(no title)",
            "start": "2025-01-02", "end": "2025-01-04",
            "all_day": True, "location": "",
        },
        {
            "id": "untitled", "title": "(no title)",
            "start": "2025-01-05T12:00:00Z", "end": "2025-01-05T13:00:00Z",
            "all_day": False, "location": "",
        },
        {
            "id": "no-end", "title": "(no title)",
            "start": "2025-01-06", "end": "",
            "all_day": True, "location": "",
        },
    ]


@pytest.mark.parametrize("payload", [{}, {"items": []}])
def test_empty_calendar(payload):
    assert client(FakeOpener(token(), payload)).upcoming() == []


def test_shared_calendar_id_is_quoted_into_the_path():
    opener = FakeOpener(token(), {"items": [
        {"id": "e1", "summary": "ESN", "start": {"dateTime": "2026-11-12T18:00:00+02:00"}}]})
    events = client(opener).upcoming(calendar_id="esn#team@group.calendar.google.com")
    assert [e["id"] for e in events] == ["e1"]
    path = urlsplit(opener.calls[1][0].full_url).path
    assert path == "/calendar/v3/calendars/esn%23team%40group.calendar.google.com/events"


def test_unknown_or_unshared_calendar_is_a_not_found_error():
    assert issubclass(gcal.CalendarNotFoundError, gcal.CalendarError)
    opener = FakeOpener(token(), http_error(404, "Not Found", b"not JSON"))
    with pytest.raises(gcal.CalendarNotFoundError) as raised:
        client(opener).upcoming(calendar_id="x@group.calendar.google.com")
    assert "404" in str(raised.value)
    assert_safe(raised.value)


@pytest.mark.parametrize("calendar_id", ["", None, 5, "x" * 257])
def test_bad_calendar_id_raises_before_network(calendar_id):
    opener = FakeOpener()
    with pytest.raises(ValueError):
        client(opener).upcoming(calendar_id=calendar_id)
    assert opener.calls == []


@pytest.mark.parametrize("payload", [b"", b"{", b'{"items":', LEAK.encode()])
def test_malformed_json_is_safe_calendar_error(payload, capsys):
    opener = FakeOpener(token(), payload)
    with pytest.raises(gcal.CalendarError) as raised:
        client(opener).upcoming()
    assert type(raised.value) is gcal.CalendarError
    assert len(opener.calls) == 2
    assert_safe(raised.value)
    captured = capsys.readouterr()
    assert_safe(captured.out + captured.err)


def test_between_reads_an_explicit_window_for_the_month_view():
    opener = FakeOpener(token(), {"items": [
        {"id": "e1", "summary": "Doktor", "start": {"dateTime": "2025-01-20T10:00:00+02:00"},
         "end": {"dateTime": "2025-01-20T11:00:00+02:00"}}]})
    events = client(opener).between(NOW, NOW + 42 * 86400)
    assert [e["title"] for e in events] == ["Doktor"]
    query = parse_qs(urlsplit(opener.calls[-1][0].full_url).query)
    assert query["timeMin"] == ["2025-01-01T00:00:00Z"] and query["timeMax"] == ["2025-02-12T00:00:00Z"]
    assert query["singleEvents"] == ["true"] and query["maxResults"] == ["250"]


def test_between_refuses_an_empty_or_too_long_window():
    c = client(FakeOpener())
    for span in (0, -1, 63 * 86400):
        with pytest.raises(ValueError):
            c.between(NOW, NOW + span)
