import base64
import io
import json
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from igris import gmail


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, limit=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Answers requests from a queue of (status, json-able body) and records them."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        status, body = self.replies.pop(0)
        data = json.dumps(body).encode()
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(data))
        return FakeResponse(data)


TOKEN_OK = (200, {"access_token": "ya29.access", "expires_in": 3600, "token_type": "Bearer"})


def client(opener, clock=lambda: 1000.0):
    return gmail.GmailClient("cid", "csecret", "1//refresh", opener=opener, clock=clock)


def form(request):
    return {k: v[0] for k, v in parse_qs(request.data.decode()).items()}


# --- authorization URL and redirect ---------------------------------------------------------

def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = gmail.pkce_pair()
    import hashlib
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()
    assert 43 <= len(verifier) <= 128


def test_auth_url_asks_readonly_offline_with_pkce():
    url = gmail.auth_url("cid", "http://127.0.0.1:5000/", state="st", code_challenge="ch")
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(gmail.AUTH_URL + "?")
    assert query["scope"] == gmail.READONLY_SCOPE
    assert query["access_type"] == "offline" and query["prompt"] == "consent"
    assert query["code_challenge_method"] == "S256" and query["code_challenge"] == "ch"
    assert query["state"] == "st" and query["response_type"] == "code"


def test_parse_redirect_returns_code_when_state_matches():
    assert gmail.parse_redirect("/?state=st&code=4/abc", "st") == "4/abc"


@pytest.mark.parametrize("path", ["/?state=other&code=x", "/?code=x", "/?state=st&error=access_denied",
                                  "/?state=st"])
def test_parse_redirect_rejects_bad_answers(path):
    with pytest.raises(gmail.GmailError):
        gmail.parse_redirect(path, "st")


# --- tokens ---------------------------------------------------------------------------------

def test_exchange_code_returns_refresh_token():
    opener = FakeOpener((200, {"access_token": "a", "refresh_token": "1//r", "expires_in": 10}))
    assert gmail.exchange_code("cid", "sec", "code", "ver", "http://127.0.0.1:1/",
                               opener=opener) == "1//r"
    sent = form(opener.requests[0])
    assert sent["grant_type"] == "authorization_code" and sent["code_verifier"] == "ver"
    assert opener.requests[0].full_url == gmail.TOKEN_URL


def test_exchange_code_without_refresh_token_fails():
    opener = FakeOpener((200, {"access_token": "a", "expires_in": 10}))
    with pytest.raises(gmail.GmailError, match="refresh token"):
        gmail.exchange_code("cid", "sec", "code", "ver", "http://127.0.0.1:1/", opener=opener)


def test_invalid_grant_is_an_auth_error_and_hides_secrets():
    opener = FakeOpener((400, {"error": "invalid_grant", "error_description": "Token expired"}))
    with pytest.raises(gmail.GmailAuthError) as caught:
        client(opener).list_ids()
    text = str(caught.value)
    assert "csecret" not in text and "1//refresh" not in text


def test_other_token_errors_are_not_auth_errors():
    opener = FakeOpener((500, {"error": "backend"}))
    with pytest.raises(gmail.GmailError) as caught:
        client(opener).list_ids()
    assert not isinstance(caught.value, gmail.GmailAuthError)


# --- API calls ------------------------------------------------------------------------------

def test_list_ids_sends_bearer_and_query():
    opener = FakeOpener(TOKEN_OK, (200, {"messages": [{"id": "a1"}, {"id": "b2"}]}))
    assert client(opener).list_ids("in:inbox", max_results=5) == ["a1", "b2"]
    api = opener.requests[1]
    assert api.get_header("Authorization") == "Bearer ya29.access"
    query = parse_qs(urlsplit(api.full_url).query)
    assert query == {"q": ["in:inbox"], "maxResults": ["5"]}


def test_list_ids_empty_mailbox():
    opener = FakeOpener(TOKEN_OK, (200, {"resultSizeEstimate": 0}))
    assert client(opener).list_ids() == []


def test_access_token_is_reused_until_near_expiry():
    now = [1000.0]
    opener = FakeOpener(TOKEN_OK, (200, {}), (200, {}), TOKEN_OK, (200, {}))
    c = client(opener, clock=lambda: now[0])
    c.list_ids()
    c.list_ids()
    now[0] += 3600
    c.list_ids()
    assert sum(r.full_url == gmail.TOKEN_URL for r in opener.requests) == 2


def test_401_refreshes_once_then_retries():
    opener = FakeOpener(TOKEN_OK, (401, {"error": {"code": 401}}), TOKEN_OK, (200, {"messages": []}))
    assert client(opener).list_ids() == []
    assert len(opener.requests) == 4


def test_401_twice_is_an_auth_error():
    opener = FakeOpener(TOKEN_OK, (401, {}), TOKEN_OK, (401, {}))
    with pytest.raises(gmail.GmailAuthError):
        client(opener).list_ids()


# --- sending --------------------------------------------------------------------------------


@pytest.mark.parametrize("thread_id", [None, "thread123"])
def test_send_posts_base64url_raw(thread_id):
    raw = b"Subject: test\r\n\r\n\xfb\xff"
    opener = FakeOpener(TOKEN_OK, (200, {"id": "m1", "threadId": "t1"}))
    client(opener).send(raw, thread_id=thread_id)
    request = opener.requests[1]
    assert request.full_url == gmail.API_URL + "/messages/send"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Authorization") == "Bearer ya29.access"
    payload = json.loads(request.data)
    assert payload["raw"] == base64.urlsafe_b64encode(raw).decode("ascii")
    assert payload["raw"].endswith("=")
    assert base64.urlsafe_b64decode(payload.pop("raw")) == raw
    assert payload == ({} if thread_id is None else {"threadId": thread_id})


def test_send_returns_ids():
    opener = FakeOpener(TOKEN_OK, (200, {"id": "m1", "threadId": "t1", "labelIds": ["SENT"]}))
    assert client(opener).send(b"mail") == {"id": "m1", "threadId": "t1"}


@pytest.mark.parametrize("scopes, expected", [
    (None, None), (frozenset(), False),
    (frozenset({gmail.READONLY_SCOPE}), False),
    (frozenset({"https://www.googleapis.com/auth/gmail.send"}), True),
])
def test_can_send_reports_known_scopes_without_network(scopes, expected):
    opener = FakeOpener()
    c = client(opener)
    c.granted_scopes = scopes
    assert c.can_send() is expected
    assert opener.requests == []


def test_send_without_scope_raises_scope_error():
    opener = FakeOpener((200, {**TOKEN_OK[1], "scope": gmail.READONLY_SCOPE}))
    c = client(opener)
    with pytest.raises(gmail.GmailScopeError, match="python tools/gmail_auth.py login"):
        c.send(b"mail")
    assert c.can_send() is False
    assert [r.full_url for r in opener.requests] == [gmail.TOKEN_URL]


def test_send_with_scope_succeeds():
    assert gmail.SEND_SCOPE == "https://www.googleapis.com/auth/gmail.send"
    opener = FakeOpener((200, {**TOKEN_OK[1], "scope": gmail.SEND_SCOPE}),
                        (200, {"id": "m1", "threadId": "t1"}))
    c = client(opener)
    assert c.send(b"mail") == {"id": "m1", "threadId": "t1"}
    assert c.can_send() is True


@pytest.mark.parametrize("error", [
    {"errors": [{"reason": "insufficientPermissions"}]},
    {"status": "PERMISSION_DENIED", "details": [
        {"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]},
    {"status": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"},
])
def test_send_403_insufficient_scope_maps_to_scope_error(error):
    opener = FakeOpener(TOKEN_OK, (403, {"error": error}))
    c = client(opener)
    with pytest.raises(gmail.GmailScopeError, match="python tools/gmail_auth.py login"):
        c.send(b"mail")
    assert c.granted_scopes is None
    assert len(opener.requests) == 2


def test_send_retries_once_on_401_then_auth_error():
    opener = FakeOpener(TOKEN_OK, (401, {}), TOKEN_OK, (401, {}))
    with pytest.raises(gmail.GmailAuthError):
        client(opener).send(b"mail", thread_id="t1")
    assert [r.full_url for r in opener.requests] == [
        gmail.TOKEN_URL, gmail.API_URL + "/messages/send",
        gmail.TOKEN_URL, gmail.API_URL + "/messages/send",
    ]
    assert opener.requests[1].data == opener.requests[3].data


def test_send_401_refreshes_token_and_can_succeed():
    opener = FakeOpener(TOKEN_OK, (401, {}),
                        (200, {**TOKEN_OK[1], "access_token": "fresh"}),
                        (200, {"id": "m1", "threadId": "t1"}))
    assert client(opener).send(b"mail") == {"id": "m1", "threadId": "t1"}
    assert opener.requests[3].get_header("Authorization") == "Bearer fresh"


def test_send_rechecks_scope_after_401():
    opener = FakeOpener(TOKEN_OK, (401, {}),
                        (200, {**TOKEN_OK[1], "scope": gmail.READONLY_SCOPE}))
    with pytest.raises(gmail.GmailScopeError):
        client(opener).send(b"mail")
    assert len(opener.requests) == 3


@pytest.mark.parametrize("endpoint", ["api", "token"])
def test_send_error_text_has_no_token_or_body(endpoint):
    marker = "PRIVATE_MAIL_BODY"
    leaked = "ya29.access csecret 1//refresh " + marker
    error = {"error": {"status": leaked, "message": leaked, "errors": [{"reason": leaked}]}}
    opener = FakeOpener(*([TOKEN_OK] if endpoint == "api" else []), (500, error))
    with pytest.raises(gmail.GmailError) as caught:
        client(opener).send(marker.encode())
    for secret in ("ya29.access", "csecret", "1//refresh", marker):
        assert secret not in str(caught.value)


def test_send_is_not_retried_on_500():
    opener = FakeOpener(TOKEN_OK, (500, {"error": {"status": "INTERNAL"}}))
    with pytest.raises(gmail.GmailUncertainError, match="HTTP 500"):
        client(opener).send(b"mail")
    assert len(opener.requests) == 2


def test_send_unrelated_403_is_not_scope_error():
    opener = FakeOpener(TOKEN_OK, (403, {"error": {"status": "PERMISSION_DENIED"}}))
    with pytest.raises(gmail.GmailError, match="HTTP 403") as caught:
        client(opener).send(b"mail")
    assert not isinstance(caught.value, gmail.GmailScopeError)
    assert not isinstance(caught.value, gmail.GmailUncertainError)
    assert len(opener.requests) == 2


@pytest.mark.parametrize("error_type", [urllib.error.URLError, TimeoutError, OSError])
def test_send_connection_failure_is_not_retried_and_hides_details(error_type):
    requests = []

    def opener(request, timeout=None):
        requests.append(request)
        if request.full_url == gmail.TOKEN_URL:
            return FakeResponse(json.dumps(TOKEN_OK[1]).encode())
        raise error_type("ya29.access PRIVATE_MAIL_BODY")

    with pytest.raises(gmail.GmailUncertainError) as caught:
        client(opener).send(b"PRIVATE_MAIL_BODY")
    assert "ya29.access" not in str(caught.value)
    assert "PRIVATE_MAIL_BODY" not in str(caught.value)
    assert len(requests) == 2


@pytest.mark.parametrize("response", [{}, {"id": "m1"}, {"id": 1, "threadId": "t1"},
                                     {"id": "m1", "threadId": ""}])
def test_send_rejects_missing_or_invalid_ids_without_retry(response):
    opener = FakeOpener(TOKEN_OK, (200, response))
    with pytest.raises(gmail.GmailUncertainError):
        client(opener).send(b"mail")
    assert len(opener.requests) == 2


def test_raw_decodes_base64url():
    mail = b"From: a@b.c\r\nSubject: x\r\n\r\nhi\xff"
    encoded = base64.urlsafe_b64encode(mail).rstrip(b"=").decode()
    opener = FakeOpener(TOKEN_OK, (200, {"id": "a1", "raw": encoded}))
    assert client(opener).raw("a1") == mail
    assert urlsplit(opener.requests[1].full_url).path.endswith("/messages/a1")
    assert parse_qs(urlsplit(opener.requests[1].full_url).query) == {"format": ["raw"]}


def test_raw_with_labels_returns_gmail_label_ids():
    mail = b"From: a@b.c\r\nSubject: x\r\n\r\nhi"
    encoded = base64.urlsafe_b64encode(mail).decode()
    opener = FakeOpener(TOKEN_OK, (200, {"id": "a1", "raw": encoded,
                                         "labelIds": ["INBOX", "CATEGORY_PROMOTIONS", 5]}))
    assert client(opener).raw_with_labels("a1") == (mail, ("INBOX", "CATEGORY_PROMOTIONS"))


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a?b", "x" * 200])
def test_raw_rejects_odd_ids(bad):
    with pytest.raises(ValueError):
        client(FakeOpener()).raw(bad)


def test_max_results_is_bounded():
    with pytest.raises(ValueError):
        client(FakeOpener()).list_ids(max_results=0)
    with pytest.raises(ValueError):
        client(FakeOpener()).list_ids(max_results=gmail.MAX_LIST + 1)  # pages make up to 1000


# --- alarm ----------------------------------------------------------------------------------

def test_alarm_sends_once_until_reset(tmp_path):
    sent = []
    alarm = gmail.AuthAlarm(tmp_path / "gmail_auth_down", sent.append)
    assert alarm.trip() is True
    assert alarm.trip() is False
    assert sent == [gmail.ALARM_TEXT] and alarm.tripped
    alarm.reset()
    assert not alarm.tripped
    alarm.trip()
    assert len(sent) == 2


def test_alarm_retries_when_sending_failed(tmp_path):
    calls = []

    def flaky(text):
        calls.append(text)
        if len(calls) == 1:
            raise OSError("offline")

    alarm = gmail.AuthAlarm(tmp_path / "flag", flaky)
    assert alarm.trip() is False and not alarm.tripped
    assert alarm.trip() is True and alarm.tripped


def test_guarded_trips_on_auth_error_and_resets_on_success(tmp_path):
    sent = []
    alarm = gmail.AuthAlarm(tmp_path / "flag", sent.append)

    def dead():
        raise gmail.GmailAuthError("invalid_grant")

    with pytest.raises(gmail.GmailAuthError):
        gmail.guarded(dead, alarm)
    with pytest.raises(gmail.GmailAuthError):
        gmail.guarded(dead, alarm)
    assert sent == [gmail.ALARM_TEXT]
    assert gmail.guarded(lambda: 7, alarm) == 7 and not alarm.tripped


def test_guarded_ignores_other_errors(tmp_path):
    sent = []
    alarm = gmail.AuthAlarm(tmp_path / "flag", sent.append)

    def flaky():
        raise gmail.GmailError("503")

    with pytest.raises(gmail.GmailError):
        gmail.guarded(flaky, alarm)
    assert sent == [] and not alarm.tripped


# --- header trace ---------------------------------------------------------------------------

def test_trace_headers_keeps_order_and_authserv():
    raw = (b"Received: by 10.0.0.1 with SMTP\r\n"
           b"Authentication-Results: mx.google.com; spf=pass\r\n"
           b"Received: from mail.example.com (mail.example.com)\r\n"
           b"ARC-Authentication-Results: i=1; mx.google.com; dkim=pass\r\n"
           b"Authentication-Results: evil.example; dmarc=pass\r\n"
           b"Subject: hi\r\n\r\nbody")
    assert gmail.trace_headers(raw) == [
        ("Received", "by 10.0.0.1 with SMTP"),
        ("Authentication-Results", "mx.google.com"),
        ("Received", "from mail.example.com (mail.example.com)"),
        ("ARC-Authentication-Results", "i=1; mx.google.com"),
        ("Authentication-Results", "evil.example"),
    ]


def test_list_ids_follows_the_next_page_token_up_to_the_limit():
    first = [{"id": f"a{i}"} for i in range(500)]
    second = [{"id": f"b{i}"} for i in range(300)]
    opener = FakeOpener(TOKEN_OK, (200, {"messages": first, "nextPageToken": "p2"}),
                        (200, {"messages": second, "nextPageToken": "p3"}))
    ids = client(opener).list_ids("in:inbox", max_results=800)
    assert len(ids) == 800 and ids[0] == "a0" and ids[-1] == "b299"
    pages = [parse_qs(urlsplit(r.full_url).query) for r in opener.requests[1:]]
    assert pages[0]["maxResults"] == ["500"] and "pageToken" not in pages[0]
    assert pages[1]["maxResults"] == ["300"] and pages[1]["pageToken"] == ["p2"]
    assert len(opener.requests) == 3  # the limit is reached: no third page


def test_list_ids_stops_on_a_stuck_pagination():
    # entries without string ids and the same token again: no progress, so it stops
    opener = FakeOpener(TOKEN_OK, (200, {"messages": [{"id": 1}], "nextPageToken": "t"}),
                        *[(200, {"messages": [{"id": 1}], "nextPageToken": "t"})] * 50)
    assert client(opener).list_ids(max_results=1000) == [] and len(opener.requests) == 2
    # new ids but a token seen before: one repeat is enough to stop
    opener = FakeOpener(TOKEN_OK, (200, {"messages": [{"id": "a"}], "nextPageToken": "t"}),
                        (200, {"messages": [{"id": "b"}], "nextPageToken": "t"}),
                        *[(200, {"messages": [{"id": "c"}], "nextPageToken": "t"})] * 50)
    assert client(opener).list_ids(max_results=1000) == ["a", "b"] and len(opener.requests) == 3


def test_list_ids_counts_an_id_on_two_pages_once():
    opener = FakeOpener(TOKEN_OK, (200, {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"}),
                        (200, {"messages": [{"id": "b"}, {"id": "c"}]}))
    assert client(opener).list_ids(max_results=3) == ["a", "b", "c"]


def test_list_ids_reads_a_bounded_number_of_pages():
    pages = [(200, {"messages": [{"id": f"x{i}"}], "nextPageToken": f"p{i}"}) for i in range(100)]
    opener = FakeOpener(TOKEN_OK, *pages)
    ids = client(opener).list_ids(max_results=gmail.MAX_LIST)
    assert len(ids) == gmail._MAX_PAGES and len(opener.requests) == 1 + gmail._MAX_PAGES


def test_list_ids_stops_when_there_is_no_next_page():
    opener = FakeOpener(TOKEN_OK, (200, {"messages": [{"id": "a"}]}))
    assert client(opener).list_ids(max_results=1000) == ["a"] and len(opener.requests) == 2


def test_raw_details_returns_trusted_thread_and_labels():
    raw = b'From: a@example.com\r\nMessage-ID: <rfc@example.com>\r\n\r\nhello'
    opener = FakeOpener(TOKEN_OK, (200, {'raw': base64.urlsafe_b64encode(raw).decode(),
                                      'labelIds': ['INBOX'], 'threadId': 'thread-1'}))
    assert client(opener).raw_details('message-1') == (raw, ('INBOX',), 'thread-1')
    assert parse_qs(urlsplit(opener.requests[-1].full_url).query)['format'] == ['raw']


def test_send_unreadable_success_body_is_uncertain():
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        if request.full_url == gmail.TOKEN_URL:
            return FakeResponse(json.dumps(TOKEN_OK[1]).encode())
        return FakeResponse(b"not json")

    with pytest.raises(gmail.GmailUncertainError):
        client(opener).send(b"mail")
    assert len(calls) == 2
