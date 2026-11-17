"""Gmail reading and sending over the REST API with OAuth 2.0 (installed app, PKCE).

Every network call goes through an injected ``opener`` (``urllib.request.urlopen``
by default) so tests never touch the network. Error messages carry only HTTP
status codes and OAuth error codes, never tokens or the client secret.

A refresh token Google no longer accepts raises ``GmailAuthError``. Wrap calls in
``guarded`` with an ``AuthAlarm`` so the owner gets exactly one message about it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from email import message_from_bytes, policy
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, urlencode, urlsplit

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
ALARM_TEXT = "Gmail bağlantısı düştü, yeniden yetkilendirme gerekiyor"
_SCOPE_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
_SEND_SCOPE_HELP = "Gmail sending is not granted; run python tools/gmail_auth.py login"

# An access token is renewed this many seconds before Google says it expires.
_EXPIRY_MARGIN = 60
_PAGE_LIMIT = 500  # Gmail's largest page of message ids
MAX_LIST = 5000  # ids one list_ids call may gather over several pages
_MAX_PAGES = 2 * MAX_LIST // _PAGE_LIMIT  # room for short pages, never an endless loop
_MAX_BODY = 40 * 1024 * 1024  # format=raw of a 25 MB mail grows by a third in base64
_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TRACED = ("received", "authentication-results", "arc-authentication-results")


class GmailError(RuntimeError):
    """A Gmail or token endpoint call failed."""


class GmailAuthError(GmailError):
    """Google refused the stored authorization; the owner must sign in again."""


class GmailScopeError(GmailError):
    """The stored sign-in lacks permission to send mail."""


class GmailUncertainError(GmailError):
    """A send request may have reached Gmail; the mail may have been delivered."""


def pkce_pair(token_bytes: Callable[[int], bytes] = secrets.token_bytes) -> tuple[str, str]:
    """Return a PKCE ``(verifier, S256 challenge)`` pair."""
    verifier = base64.urlsafe_b64encode(token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def auth_url(client_id: str, redirect_uri: str, *, state: str, code_challenge: str,
             scopes: tuple[str, ...] = (READONLY_SCOPE,)) -> str:
    """The consent page address. ``prompt=consent`` makes Google return a refresh token."""
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urlencode(query, quote_via=quote)


def parse_redirect(path: str, expected_state: str) -> str:
    """Return the authorization code from the loopback redirect path."""
    query = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
    if not secrets.compare_digest(query.get("state", ""), expected_state):
        raise GmailError("sign-in answer does not belong to this request (state mismatch)")
    if "error" in query:
        raise GmailError(f"sign-in was not completed: {query['error'][:60]}")
    code = query.get("code", "")
    if not code:
        raise GmailError("sign-in answer has no authorization code")
    return code


def _read_json(response) -> dict:
    body = response.read(_MAX_BODY + 1)
    if len(body) > _MAX_BODY:
        raise GmailError("response is too large")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise GmailError("response is not JSON") from None
    if not isinstance(data, dict):
        raise GmailError("response is not a JSON object")
    return data


def _error_code(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read(64 * 1024) or b"{}")
    except Exception:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, str):
        return error[:60]
    if isinstance(error, dict):
        # Gmail uses both legacy errors[].reason and Google RPC details[].reason.
        for key in ("errors", "details"):
            entries = error.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    reason = entry.get("reason") if isinstance(entry, dict) else None
                    if isinstance(reason, str) and reason in _SCOPE_REASONS:
                        return reason
        return str(error.get("status", ""))[:60]
    return ""


def _token_request(fields: dict, opener, timeout: float) -> dict:
    request = urllib.request.Request(
        TOKEN_URL, data=urlencode(fields).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener(request, timeout=timeout) as response:
            return _read_json(response)
    except urllib.error.HTTPError as exc:
        code = _error_code(exc)
        if code in ("invalid_grant", "unauthorized_client", "invalid_client"):
            raise GmailAuthError(f"token endpoint refused the authorization ({code})") from None
        # Unrecognized error codes are server-controlled and may echo secrets.
        raise GmailError(f"token endpoint returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise GmailError(f"token endpoint unreachable: {type(exc).__name__}") from None


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str,
                  redirect_uri: str, *, opener=urllib.request.urlopen,
                  timeout: float = 30) -> str:
    """Trade the authorization code for a refresh token and return it."""
    data = _token_request({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, opener, timeout)
    token = data.get("refresh_token")
    if not isinstance(token, str) or not token:
        raise GmailError("Google returned no refresh token; remove Igris's access at "
                         "myaccount.google.com/permissions and sign in again")
    return token


class GmailClient:
    """Lists, fetches and sends mail for the signed-in account.

    Callers must obtain approval before sending; this client handles transport only.
    """

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *,
                 opener=urllib.request.urlopen, clock: Callable[[], float] = time.time,
                 timeout: float = 30):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._opener = opener
        self._clock = clock
        self._timeout = timeout
        self._access: str | None = None
        self._expires_at = 0.0
        # scopes Google says the access token carries; None until a token arrives or when
        # Google does not say
        self.granted_scopes: frozenset[str] | None = None

    def _access_token(self) -> str:
        if self._access and self._clock() < self._expires_at:
            return self._access
        data = _token_request({
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }, self._opener, self._timeout)
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise GmailError("token endpoint returned no access token")
        try:
            lifetime = float(data.get("expires_in", 0))
        except (TypeError, ValueError):
            lifetime = 0.0
        scope = data.get("scope")
        if isinstance(scope, str):
            self.granted_scopes = frozenset(scope.split())
        self._access = token
        self._expires_at = self._clock() + max(lifetime - _EXPIRY_MARGIN, 0)
        return token

    def _get(self, path: str, params: dict) -> dict:
        url = API_URL + path + ("?" + urlencode(params) if params else "")
        for attempt in (1, 2):
            request = urllib.request.Request(
                url, headers={"Authorization": "Bearer " + self._access_token()})
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    return _read_json(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    self._access = None
                    if attempt == 1:
                        continue
                    raise GmailAuthError("Gmail refused a fresh access token (HTTP 401)") from None
                code = _error_code(exc)
                raise GmailError(f"Gmail API returned HTTP {exc.code} {code}".rstrip()) from None
            except (urllib.error.URLError, OSError) as exc:
                raise GmailError(f"Gmail API unreachable: {type(exc).__name__}") from None
        raise AssertionError("unreachable")

    def can_send(self) -> bool | None:
        """Report the known send grant without making a network call."""
        if self.granted_scopes is None:
            return None
        return SEND_SCOPE in self.granted_scopes

    def send(self, raw: bytes, *, thread_id: str | None = None) -> dict:
        """Send RFC 5322 bytes, optionally in an existing thread, and return its ids.

        Only an explicit HTTP 401 allows one retry with a fresh token. Other
        failures may follow a successful delivery, so they must never be retried.
        """
        payload = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        if thread_id is not None:
            payload["threadId"] = thread_id
        body = json.dumps(payload).encode("utf-8")
        for attempt in (1, 2):
            token = self._access_token()
            if self.can_send() is False:
                raise GmailScopeError(_SEND_SCOPE_HELP)
            request = urllib.request.Request(
                API_URL + "/messages/send", data=body, method="POST",
                headers={"Authorization": "Bearer " + token,
                         "Content-Type": "application/json"})
            try:
                with self._opener(request, timeout=self._timeout) as response:
                    try:
                        data = _read_json(response)
                    except GmailError as exc:
                        # Gmail answered with success; its body alone was unreadable.
                        raise GmailUncertainError(str(exc)) from None
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    self._access = None
                    if attempt == 1:
                        continue
                    raise GmailAuthError("Gmail refused a fresh access token (HTTP 401)") from None
                if exc.code == 403 and _error_code(exc) in _SCOPE_REASONS:
                    raise GmailScopeError(_SEND_SCOPE_HELP) from None
                # Do not include server text: it could echo the token or mail body.
                if exc.code >= 500:
                    # A server error can follow a delivery that already happened.
                    raise GmailUncertainError(f"Gmail API returned HTTP {exc.code}") from None
                raise GmailError(f"Gmail API returned HTTP {exc.code}") from None
            except (urllib.error.URLError, OSError) as exc:
                # A timeout or reset may come after Gmail accepted the mail.
                raise GmailUncertainError(
                    f"Gmail API unreachable: {type(exc).__name__}") from None
            if any(not isinstance(data.get(key), str) or not data[key]
                   for key in ("id", "threadId")):
                raise GmailUncertainError("Gmail send response has no valid message and thread ids")
            return {"id": data["id"], "threadId": data["threadId"]}
        raise AssertionError("unreachable")

    def list_ids(self, query: str = "", max_results: int = 10) -> list[str]:
        """Ids of the newest messages matching a Gmail search ``query``, newest first.

        Gmail answers at most 500 ids per page; more (up to ``MAX_LIST``) follow the
        ``nextPageToken``."""
        if not 1 <= max_results <= MAX_LIST:
            raise ValueError(f"max_results must be 1-{MAX_LIST}")
        ids: dict[str, None] = {}  # ordered and without duplicates
        token, tokens = None, set()
        for _ in range(_MAX_PAGES):  # a stuck pagination cannot loop for ever
            params = {"maxResults": str(min(_PAGE_LIMIT, max_results - len(ids)))}
            if query:
                params = {"q": query, **params}
            if token:
                params["pageToken"] = token
            data = self._get("/messages", params)
            before = len(ids)
            for m in data.get("messages") or []:
                if isinstance(m, dict) and isinstance(m.get("id"), str):
                    ids.setdefault(m["id"])
            token = data.get("nextPageToken")
            if (len(ids) >= max_results or len(ids) == before or not isinstance(token, str)
                    or not token or token in tokens):
                break
            tokens.add(token)
        return list(ids)[:max_results]

    def raw(self, message_id: str) -> bytes:
        """The full RFC 822 bytes of one message, for ``mailcheck.check_message``."""
        return self.raw_with_labels(message_id)[0]

    def raw_with_labels(self, message_id: str) -> tuple[bytes, tuple[str, ...]]:
        """The RFC 822 bytes and Gmail's label ids (CATEGORY_PROMOTIONS and the like)."""
        raw, labels, _ = self.raw_details(message_id)
        return raw, labels

    def raw_details(self, message_id: str) -> tuple[bytes, tuple[str, ...], str | None]:
        """Read message bytes, labels and the API's thread id in one request.

        The Gmail message id is not the RFC Message-ID used by reply headers.
        Thread ids come from API metadata, never from sender-controlled headers.
        """
        if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
            raise ValueError("message id must be 1-128 URL-safe characters")
        data = self._get(f"/messages/{message_id}", {"format": "raw"})
        encoded = data.get("raw")
        if not isinstance(encoded, str):
            raise GmailError("message has no raw content")
        labels = tuple(x for x in data.get("labelIds") or () if isinstance(x, str))
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError:
            raise GmailError("raw content is not valid base64") from None
        thread = data.get("threadId")
        return raw, labels, thread if isinstance(thread, str) and thread else None


class AuthAlarm:
    """Tells the owner once that Gmail needs a new sign-in; a flag file stops repeats."""

    def __init__(self, flag_path: Path, send: Callable[[str], None]):
        self.flag_path = Path(flag_path)
        self._send = send

    @property
    def tripped(self) -> bool:
        return self.flag_path.exists()

    def trip(self) -> bool:
        """Send the alarm unless it was already sent. True when a message went out."""
        if self.tripped:
            return False
        try:
            self._send(ALARM_TEXT)
        except Exception:
            return False  # not marked, so the next failure tries again
        self.flag_path.parent.mkdir(parents=True, exist_ok=True)
        self.flag_path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        return True

    def reset(self) -> None:
        self.flag_path.unlink(missing_ok=True)


def guarded(call: Callable[[], object], alarm: AuthAlarm):
    """Run ``call``; on ``GmailAuthError`` raise the alarm, on success clear it."""
    try:
        result = call()
    except GmailAuthError:
        alarm.trip()
        raise
    alarm.reset()
    return result


def trace_headers(raw: bytes) -> list[tuple[str, str]]:
    """Received and Authentication-Results headers top to bottom, shortened.

    For Authentication-Results the value is the authserv-id; for the ARC variant the
    instance and authserv-id; Received headers keep their first 100 characters.
    """
    message = message_from_bytes(raw, policy=policy.compat32)
    trace = []
    for name, value in message.items():
        lower = name.lower()
        if lower not in _TRACED:
            continue
        text = " ".join(str(value).split())
        if lower == "authentication-results":
            text = text.split(";", 1)[0].split()[0] if text else ""
        elif lower == "arc-authentication-results":
            text = "; ".join(part.strip() for part in text.split(";")[:2])
        else:
            text = text[:100]
        trace.append((name, text))
    return trace
