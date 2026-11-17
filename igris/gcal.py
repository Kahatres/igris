"""Google Calendar access with the same OAuth sign-in as Gmail.

The owner signs in once with ``tools/gmail_auth.py login``, which asks for read-only Gmail,
read-only Calendar and ``calendar.app.created`` together. That last scope lets Igris create
its own calendars and manage events on them only: it cannot touch the owner's own
calendars or events, so writing needs no broader ``calendar.events`` grant. Every network
call goes through an injected
``opener`` so tests never touch the network. Error messages carry only HTTP status
codes, reasons and exception class names, never tokens or the client secret.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote, urlencode

from igris import gmail

API_URL = "https://www.googleapis.com/calendar/v3"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
# create secondary calendars and manage events on the calendars this app created, only
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
SCOPES = (CALENDAR_SCOPE, WRITE_SCOPE)
MAX_EVENTS_PER_CALENDAR = 5000

_SCOPE_REASONS = {"insufficientpermissions", "access_token_scope_insufficient"}


class CalendarError(RuntimeError):
    """A Calendar API or token endpoint call failed."""


class CalendarAuthError(CalendarError):
    """Google refused the refresh token, or a fresh access token (HTTP 401 twice)."""


class CalendarScopeError(CalendarError):
    """The sign-in does not include calendar access yet: run gmail_auth login again."""


class CalendarNotFoundError(CalendarError):
    """Google does not know the calendar id, or it is not shared with this account (HTTP 404)."""


def _rfc3339(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bounded(value, name: str, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
        raise ValueError(f"{name} must be an integer from 1 to {high}")
    return value


def _is_scope_error(exc: urllib.error.HTTPError) -> bool:
    """True for a 403 that says the token lacks the calendar scope."""
    if exc.code != 403:
        return False
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError, OSError, AttributeError):
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    reasons = []
    for key in ("errors", "details"):
        entries = error.get(key)
        if isinstance(entries, list):
            reasons += [e.get("reason") for e in entries if isinstance(e, dict)]
    if any(isinstance(r, str) and r.lower() in _SCOPE_REASONS for r in reasons):
        return True
    message = error.get("message")
    return isinstance(message, str) and "scope" in message.lower()


def _event(item) -> dict | None:
    """One API item as a small dict, or None when it cannot be shown."""
    if not isinstance(item, dict) or item.get("status") == "cancelled":
        return None
    event_id = item.get("id")
    start = item.get("start")
    if not isinstance(event_id, str) or not event_id or not isinstance(start, dict):
        return None
    if isinstance(start.get("dateTime"), str) and start["dateTime"]:
        begin, all_day = start["dateTime"], False
    elif isinstance(start.get("date"), str) and start["date"]:
        begin, all_day = start["date"], True
    else:
        return None
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    finish = end.get("date" if all_day else "dateTime") or end.get("dateTime") or end.get("date") or ""
    title = item.get("summary")
    location = item.get("location")
    return {
        "id": event_id,
        "title": title if isinstance(title, str) and title else "(no title)",
        "start": begin,
        "end": finish if isinstance(finish, str) else "",
        "all_day": all_day,
        "location": location if isinstance(location, str) else "",
    }


class CalendarMissing(CalendarError):
    """The calendar or event does not exist (HTTP 404 or 410)."""


class CalendarClient(gmail.GmailClient):
    """Reads the owner's primary calendar; writes only to calendars Igris created."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *,
                 opener=urllib.request.urlopen, clock: Callable[[], float] = time.time,
                 timeout: float = 30):
        super().__init__(client_id, client_secret, refresh_token,
                         opener=opener, clock=clock, timeout=timeout)

    def _token(self) -> str:
        try:
            return self._access_token()
        except gmail.GmailAuthError as exc:
            raise CalendarAuthError(str(exc)) from None
        except gmail.GmailError as exc:
            raise CalendarError(str(exc)) from None

    def _events(self, params: dict, calendar_id: str = "primary") -> dict:
        return self._call("GET", "/calendars/" + quote(calendar_id, safe="") + "/events", params)

    def _call(self, method: str, path: str, params: dict | None = None,
              body: dict | None = None, *, missing: bool = False) -> dict:
        url = API_URL + path + ("?" + urlencode(params) if params else "")
        data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in (1, 2):
            headers = {"Authorization": "Bearer " + self._token()}
            if data_bytes is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"
            request = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
            try:
                response = self._opener(request, timeout=self._timeout)
                try:
                    raw = response.read()
                finally:
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    self._access = None
                    if attempt == 1:
                        continue
                    raise CalendarAuthError("Calendar refused a fresh access token (HTTP 401)") from None
                if missing and exc.code in (404, 410):
                    raise CalendarMissing(f"Calendar API returned HTTP {exc.code}") from None
                if exc.code == 404:
                    raise CalendarNotFoundError(
                        "Calendar not found or not shared with this account (HTTP 404)") from None
                if _is_scope_error(exc):
                    raise CalendarScopeError(
                        "Calendar access is not granted (HTTP 403); run tools/gmail_auth.py login") from None
                raise CalendarError(f"Calendar API returned HTTP {exc.code} {exc.reason}".rstrip()) from None
            except (urllib.error.URLError, OSError) as exc:
                raise CalendarError(f"Calendar API unreachable: {type(exc).__name__}") from None
            if not raw and method == "DELETE":  # DELETE answers 204 with no body
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise CalendarError("Calendar API returned invalid JSON") from None
            if not isinstance(data, dict):
                raise CalendarError("Calendar API returned invalid JSON")
            return data
        raise AssertionError("unreachable")

    def upcoming(self, *, days: int = 14, max_results: int = 50, now: float | None = None,
                 calendar_id: str = "primary") -> list[dict]:
        """Events from now until ``days`` ahead, in the order Google returns them.

        ``calendar_id`` reads another calendar the account can see, such as a shared one.
        """
        _bounded(days, "days", 60)
        start = self._clock() if now is None else now
        return self.between(start, start + days * 86400, max_results=max_results,
                            calendar_id=calendar_id)

    def between(self, time_min: float, time_max: float, *, max_results: int = 250,
                calendar_id: str = "primary") -> list[dict]:
        """Events overlapping [time_min, time_max) (epoch seconds), at most 62 days apart:
        the six weeks a month view shows."""
        if not isinstance(calendar_id, str) or not calendar_id or len(calendar_id) > 256:
            raise ValueError("calendar_id must be a non-empty calendar id")
        _bounded(max_results, "max_results", 250)
        if not 0 < time_max - time_min <= 62 * 86400:
            raise ValueError("time_max must be after time_min and at most 62 days later")
        data = self._events({
            "timeMin": _rfc3339(time_min),
            "timeMax": _rfc3339(time_max),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max_results),
        }, calendar_id)
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return [e for e in (_event(item) for item in items) if e is not None]

    # ---- writing: only to calendars Igris created (calendar.app.created) ----

    def require_write(self) -> None:
        """Raise CalendarScopeError when the stored sign-in cannot write Igris's calendars."""
        self._token()
        granted = self.granted_scopes
        if granted is not None and WRITE_SCOPE not in granted:
            raise CalendarScopeError(
                "The Google sign-in cannot write calendars yet; run tools/gmail_auth.py login")

    def primary_time_zone(self) -> str | None:
        zone = self._call("GET", "/calendars/primary").get("timeZone")
        return zone if isinstance(zone, str) and zone else None

    def create_calendar(self, summary: str, time_zone: str | None = None) -> str:
        body = {"summary": summary}
        if time_zone:
            body["timeZone"] = time_zone
        calendar_id = self._call("POST", "/calendars", body=body).get("id")
        if not isinstance(calendar_id, str) or not calendar_id:
            raise CalendarError("Calendar API returned no calendar id")
        return calendar_id

    def calendar_exists(self, calendar_id: str) -> bool:
        try:
            self._call("GET", "/calendars/" + quote(calendar_id, safe=""), missing=True)
        except CalendarMissing:
            return False
        return True

    def list_events(self, calendar_id: str) -> list[dict]:
        """Every live event of one calendar, raw as Google returns it (all pages)."""
        events, token = [], None
        path = "/calendars/" + quote(calendar_id, safe="") + "/events"
        while True:
            params = {"maxResults": "2500", "showDeleted": "false"}
            if token:
                params["pageToken"] = token
            data = self._call("GET", path, params)
            items = data.get("items")
            events += [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
            token = data.get("nextPageToken")
            if not isinstance(token, str) or not token or len(events) >= MAX_EVENTS_PER_CALENDAR:
                return events

    def insert_event(self, calendar_id: str, event: dict) -> dict:
        return self._call("POST", "/calendars/" + quote(calendar_id, safe="") + "/events",
                          body=event)

    def update_event(self, calendar_id: str, event_id: str, event: dict) -> dict:
        return self._call("PUT", "/calendars/" + quote(calendar_id, safe="") + "/events/"
                          + quote(event_id, safe=""), body=event)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Remove one event; one that is already gone counts as removed."""
        try:
            self._call("DELETE", "/calendars/" + quote(calendar_id, safe="") + "/events/"
                       + quote(event_id, safe=""), missing=True)
        except CalendarMissing:
            pass
