"""Manual, owner-run Google sign-in (Gmail and Calendar) and first look. Never run by pytest against Google.

    python tools/gmail_auth.py login     # browser sign-in, stores the refresh token
    python tools/gmail_auth.py peek      # newest inbox mail: header order + mailcheck verdict
    python tools/gmail_auth.py peek 5    # the five newest

Needs GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in keyring (python tools/set_keys.py gmail).
The sign-in asks for Gmail read and send access, read-only Google Calendar, plus
calendar.app.created (Igris may create its own calendars and manage only their events), in one consent; the
Google Cloud project must have the Gmail API and the Google Calendar API enabled. The
refresh token goes to keyring under
service "igris", name GMAIL_REFRESH_TOKEN, and is never printed. Peek prints no mail
bodies, only headers that show where a message came from.
"""

from __future__ import annotations

import http.server
import secrets
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from igris import gcal, gmail, mailcheck  # noqa: E402
from igris.config import default_data_dir  # noqa: E402

SERVICE = "igris"
WAIT_SECONDS = 300
PAGE = ("<!doctype html><meta charset=utf-8><title>Igris</title>"
        "<p>Igris: {text} Bu sekmeyi kapatabilirsin.</p>").format


def alarm_flag() -> Path:
    return default_data_dir() / "gmail_auth_down"


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def wait_for_redirect(start, timeout: float = WAIT_SECONDS) -> str:
    """Serve one loopback redirect. ``start(redirect_uri)`` opens the consent page."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            done = "state=" in self.path
            if done:
                seen.append(self.path)
            text = "giriş cevabı alındı, terminale dön." if done else "bekleniyor."
            body = PAGE(text=text).encode()
            self.send_response(200 if done else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # the code in the URL must not reach the console
            pass

    class Server(http.server.HTTPServer):
        timed_out = False

        def handle_timeout(self):
            self.timed_out = True

    server = Server(("127.0.0.1", 0), Handler)
    server.timeout = timeout
    try:
        start(f"http://127.0.0.1:{server.server_address[1]}/")
        # a favicon request or a stray visit does not end the wait
        while not seen and not server.timed_out:
            server.handle_request()
    finally:
        server.server_close()
    if not seen:
        raise gmail.GmailError("no sign-in answer arrived in time")
    return seen[0]


def login(kr, *, out=print, wait=wait_for_redirect, open_page=webbrowser.open,
          opener=None, flag: Path | None = None) -> int:
    client_id = kr.get_password(SERVICE, "GMAIL_CLIENT_ID")
    client_secret = kr.get_password(SERVICE, "GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        out("GMAIL_CLIENT_ID or GMAIL_CLIENT_SECRET is missing. Run:  python tools/set_keys.py gmail")
        return 1
    verifier, challenge = gmail.pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect: dict[str, str] = {}

    def start(redirect_uri: str) -> None:
        redirect["uri"] = redirect_uri
        url = gmail.auth_url(client_id, redirect_uri, state=state, code_challenge=challenge,
                             scopes=(gmail.READONLY_SCOPE, gmail.SEND_SCOPE, *gcal.SCOPES))
        out("Opening the Google sign-in page. If no browser opens, visit:\n" + url)
        open_page(url)

    extra = {} if opener is None else {"opener": opener}
    try:
        code = gmail.parse_redirect(wait(start), state)
        token = gmail.exchange_code(client_id, client_secret, code, verifier,
                                    redirect["uri"], **extra)
    except gmail.GmailError as exc:
        out(f"sign-in failed: {exc}")
        return 1
    kr.set_password(SERVICE, "GMAIL_REFRESH_TOKEN", token)
    gmail.AuthAlarm(flag or alarm_flag(), lambda text: None).reset()
    out("Gmail sending: unknown until checked with python tools/gmail_auth.py peek.")
    out("signed in: Gmail read and send access requested, read-only Calendar, plus write access "
        "to the calendars Igris "
        "creates (Igris · Okul, Igris · Kişisel, Igris · ESN); refresh token stored in keyring. "
        "Calendar needs the Google Calendar API enabled in the Google Cloud project. "
        "Next:  python tools/gmail_auth.py peek")
    return 0


def peek(kr, count: int, *, out=print, opener=None) -> int:
    names = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")
    values = [kr.get_password(SERVICE, name) for name in names]
    if not all(values):
        out("not signed in yet. Run:  python tools/gmail_auth.py login")
        return 1
    extra = {} if opener is None else {"opener": opener}
    client = gmail.GmailClient(*values, **extra)
    try:
        ids = client.list_ids("in:inbox", max_results=count)
        sending = client.can_send()
        if sending is True:
            out("Gmail sending: granted.")
        elif sending is False:
            out("Gmail sending: not granted. Run: python tools/gmail_auth.py login")
        else:
            out("Gmail sending: unknown (Google did not report granted scopes).")
        if not ids:
            out("inbox is empty.")
            return 0
        for number, message_id in enumerate(ids, 1):
            raw = client.raw(message_id)
            out(f"\n[{number}/{len(ids)}] message {message_id}, {len(raw)} bytes")
            out("  header order, top = added last by the receiving server:")
            for name, value in gmail.trace_headers(raw):
                out(f"    {name}: {value}")
            verdict = mailcheck.check_message(raw)
            out(f"  mailcheck: spf={verdict.spf} dkim={verdict.dkim} dmarc={verdict.dmarc} "
                f"from={verdict.from_domain or '-'} risk={verdict.risk} "
                f"flags={','.join(verdict.flags) or '-'}")
    except gmail.GmailAuthError as exc:
        out(f"Google refused the stored sign-in ({exc}). Run:  python tools/gmail_auth.py login")
        return 1
    except gmail.GmailError as exc:
        out(f"Gmail call failed: {exc}")
        return 1
    out("\nCheck: the topmost Authentication-Results should be mx.google.com, right below the "
        "'Received: from <sender> by mx.google.com' line and above every Received line the "
        "sender's own servers added.")
    return 0


def main(argv: list[str], *, keyring_module=None, out=print) -> int:
    kr = keyring_module or _keyring()
    if kr is None:
        out("keyring is not installed. Run:  pip install keyring   then start this again.")
        return 1
    if argv[:1] == ["login"] and len(argv) == 1:
        return login(kr, out=out)
    if argv[:1] == ["peek"] and len(argv) <= 2:
        try:
            count = int(argv[1]) if len(argv) == 2 else 1
        except ValueError:
            count = 0
        if not 1 <= count <= 20:
            out("peek takes a count from 1 to 20")
            return 2
        return peek(kr, count, out=out)
    out("usage: python tools/gmail_auth.py login | peek [count]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
