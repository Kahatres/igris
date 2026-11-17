import base64
import importlib.util
import threading
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from test_gmail import TOKEN_OK, FakeOpener

spec = importlib.util.spec_from_file_location(
    "gmail_auth", Path(__file__).resolve().parents[1] / "tools" / "gmail_auth.py")
gmail_auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gmail_auth)


class FakeKeyring:
    def __init__(self, **stored):
        self.stored = dict(stored)

    def get_password(self, service, name):
        assert service == "igris"
        return self.stored.get(name)

    def set_password(self, service, name, value):
        assert service == "igris"
        self.stored[name] = value


CLIENT = {"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "csecret"}


def fake_wait(answer_for):
    """Stands in for the loopback server: calls start, returns a redirect path."""
    def wait(start):
        start("http://127.0.0.1:5555/")
        return answer_for
    return wait


def test_login_stores_refresh_token_and_clears_alarm(tmp_path, monkeypatch, capsys):
    from igris import gcal, gmail

    kr = FakeKeyring(**CLIENT)
    flag = tmp_path / "gmail_auth_down"
    flag.write_text("x")
    opened, lines, auth_calls, exchange_calls, http_calls = [], [], [], [], []
    original_auth_url = gmail.auth_url
    original_exchange_code = gmail.exchange_code
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        __import__("hashlib").sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    monkeypatch.setattr(gmail, "pkce_pair", lambda: (verifier, challenge))

    def auth_url(*args, **kwargs):
        auth_calls.append((args, dict(kwargs)))
        return original_auth_url(*args, **kwargs)

    def exchange_code(*args, **kwargs):
        exchange_calls.append((args, dict(kwargs)))
        return original_exchange_code(*args, **kwargs)

    monkeypatch.setattr(gmail, "auth_url", auth_url)
    monkeypatch.setattr(gmail, "exchange_code", exchange_code)

    def wait(start):
        start("http://127.0.0.1:5555/")
        assert len(opened) == 1
        query = parse_qs(urlsplit(opened[0]).query)
        assert query["state"][0]
        return f"/?state={query['state'][0]}&code=4/abc"

    canned = FakeOpener(
        (200, {"access_token": "a", "refresh_token": "1//new", "expires_in": 1})
    )

    def opener(request, timeout=None):
        http_calls.append((request, timeout))
        return canned(request, timeout=timeout)

    code = gmail_auth.login(
        kr, out=lines.append, wait=wait, open_page=opened.append,
        opener=opener, flag=flag,
    )
    assert code == 0
    assert kr.stored == {**CLIENT, "GMAIL_REFRESH_TOKEN": "1//new"}
    assert not flag.exists()
    assert len(auth_calls) == len(exchange_calls) == len(http_calls) == 1
    assert gmail_auth.gcal is gcal

    args, kwargs = auth_calls[0]
    assert args == ("cid", "http://127.0.0.1:5555/")
    assert kwargs["scopes"] == (gmail.READONLY_SCOPE, gmail.SEND_SCOPE, gcal.CALENDAR_SCOPE, gcal.WRITE_SCOPE)
    assert kwargs["code_challenge"] == challenge

    url = opened[0]
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == [" ".join((gmail.READONLY_SCOPE, gmail.SEND_SCOPE, gcal.CALENDAR_SCOPE, gcal.WRITE_SCOPE))]
    for scope in (gmail.READONLY_SCOPE, gcal.CALENDAR_SCOPE):
        assert quote(scope, safe="") in urlsplit(url).query
    assert query["client_id"] == ["cid"]
    assert query["redirect_uri"] == ["http://127.0.0.1:5555/"]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["state"] == [kwargs["state"]]
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]

    args, kwargs = exchange_calls[0]
    assert args == ("cid", "csecret", "4/abc", verifier, "http://127.0.0.1:5555/")
    assert kwargs == {"opener": opener}
    request, _ = http_calls[0]
    assert request.get_method() == "POST"
    form = parse_qs(request.data.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["4/abc"]
    assert form["code_verifier"] == [verifier]
    assert form["redirect_uri"] == ["http://127.0.0.1:5555/"]

    assert url in "\n".join(lines)
    success = lines[-1].lower()
    assert "gmail" in success and "calendar" in success
    assert "read-only" in success
    assert "google cloud" in success
    assert "google calendar api" in success and "enabl" in success
    assert "keyring" in success
    assert "python tools/gmail_auth.py peek" in success
    captured = capsys.readouterr()
    visible = "\n".join(lines) + captured.out + captured.err
    for secret in ("1//new", "csecret", verifier):
        assert secret not in visible


def test_module_documentation_describes_both_read_only_services_and_api_setup():
    documentation = gmail_auth.__doc__.lower()
    assert "gmail" in documentation and "calendar" in documentation
    assert "read-only" in documentation
    assert "google cloud" in documentation
    assert "google calendar api" in documentation
    assert "enabl" in documentation


def test_login_rejects_foreign_state():
    kr = FakeKeyring(**CLIENT)
    lines = []
    code = gmail_auth.login(kr, out=lines.append, wait=fake_wait("/?state=forged&code=x"),
                            open_page=lambda url: None, opener=FakeOpener())
    assert code == 1 and "GMAIL_REFRESH_TOKEN" not in kr.stored


def test_login_without_client_points_to_set_keys():
    lines = []
    assert gmail_auth.login(FakeKeyring(), out=lines.append) == 1
    assert "set_keys.py gmail" in lines[0]


def test_peek_prints_trace_and_verdict_without_body():
    raw = (b"Authentication-Results: mx.google.com; spf=pass; dkim=pass; dmarc=pass\r\n"
           b"Received: from mail.example.com\r\n"
           b"From: A <a@example.com>\r\nSubject: s\r\n\r\nSECRET BODY")
    encoded = base64.urlsafe_b64encode(raw).decode()
    opener = FakeOpener(TOKEN_OK, (200, {"messages": [{"id": "m1"}]}),
                        (200, {"id": "m1", "raw": encoded}))
    kr = FakeKeyring(**CLIENT, GMAIL_REFRESH_TOKEN="1//r")
    lines = []
    assert gmail_auth.peek(kr, 1, out=lines.append, opener=opener) == 0
    text = "\n".join(lines)
    assert "Authentication-Results: mx.google.com" in text
    assert "dmarc=pass" in text and "SECRET BODY" not in text


def test_peek_on_dead_token_says_login_again():
    opener = FakeOpener((400, {"error": "invalid_grant"}))
    kr = FakeKeyring(**CLIENT, GMAIL_REFRESH_TOKEN="1//r")
    lines = []
    assert gmail_auth.peek(kr, 1, out=lines.append, opener=opener) == 1
    assert "gmail_auth.py login" in lines[-1]


def test_peek_count_is_bounded():
    lines = []
    kr = FakeKeyring(**CLIENT, GMAIL_REFRESH_TOKEN="1//r")
    assert gmail_auth.main(["peek", "99"], keyring_module=kr, out=lines.append) == 2


def test_wait_for_redirect_ignores_favicon_and_returns_answer():
    # local loopback only: this is the tool's own server, not Google
    def start(redirect_uri):
        def visit():
            for path in ("favicon.ico", "?state=s&code=c"):
                try:
                    urllib.request.urlopen(redirect_uri + path, timeout=5).read()
                except Exception:
                    pass
        threading.Thread(target=visit, daemon=True).start()

    assert gmail_auth.wait_for_redirect(start, timeout=5) == "/?state=s&code=c"
