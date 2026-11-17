"""Web routes for the task calendar (Görev Takvimi) and the owner's Google Calendar,
plus the calendar readers and the Moodle/ESN agenda sync that serve() starts.

Route functions take the request handler ``h`` and the server context ``ctx``.
Depends on agenda, agenda_sources, gcal and llm_openai (imported where they are used).
"""

import datetime
import re
import sys
import threading
import time

CALENDAR_REFRESH = 300  # seconds between Google Calendar reads
AGENDA_SYNC_INTERVAL = 900  # seconds between Moodle and ESN calendar reads
_GOOGLE_SECRETS = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")


def calendar(h, ctx, path):
    return h._json(200, ctx.calendar_loader())


def month(h, ctx, path):
    """Six weeks around a month for the calendar grid: the user's own items (done ones
    too) and, for the owner, the events of their Google Calendar."""
    agenda = ctx.agenda
    if ctx.accounts is None or agenda is None:
        return h._error(404, "not found")
    month = h._query().get("month", [""])[0]
    try:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError(month)
        first = datetime.date.fromisoformat(month + "-01")
    except ValueError:
        return h._error(400, "month must be YYYY-MM")
    tz = datetime.datetime.now().astimezone().tzinfo
    begin = datetime.datetime.combine(first - datetime.timedelta(days=first.weekday()),
                                      datetime.time(), tz)
    end = begin + datetime.timedelta(days=42)
    items = agenda.between(h.login.user.id, begin, end)
    # an item counts by its due, so one that starts inside the window and ends after
    # it (a night shift from the last day into the next) is added: views draw it on
    # every day it covers
    for it in agenda.between(h.login.user.id, end, end + datetime.timedelta(days=7)):
        start = it.get("start")
        if start and (datetime.date.fromisoformat(start) < end.date() if len(start) == 10
                      else datetime.datetime.fromisoformat(start) < end):
            items.append(it)
    payload = {"month": month, "from": begin.date().isoformat(),
               "to": end.date().isoformat(), "items": items}
    if h._is_admin():
        payload["google"] = ctx.month_loader(begin.timestamp(), end.timestamp())
    return h._json(200, payload)


def agenda(h, ctx, path, data=None):
    """The signed-in user's task calendar; every user sees only their own items."""
    agenda, calendar_sync = ctx.agenda, ctx.calendar_sync
    if ctx.accounts is None or agenda is None:
        return h._error(404, "not found")
    from igris.agenda import BRANCHES, AgendaError, AgendaMissing

    owner = h.login.user.id
    try:
        if data is None:
            branch = h._query().get("branch", [None])[0]
            names = BRANCHES if branch is None else (branch,)
            payload = {"branches": {name: agenda.sections(owner, name) for name in names}}
            sync = getattr(h.server, "agenda_sync", None)
            if sync is not None and h._is_admin():  # Moodle and ESN feed the owner
                payload["sources"] = sync.status()
            if calendar_sync is not None and owner == calendar_sync.owner():
                payload["sync"] = calendar_sync.status()
            return h._json(200, payload)
        if path == "/api/agenda/add":
            item = {key: data.get(key) for key in ("branch", "kind", "title", "start",
                                                   "due", "all_day")}
            # a manual item is the owner's own entry: it is on the agenda already
            item.update(source="manual", status="added")
            saved, _ = agenda.upsert(owner, item)
            return h._json(201, saved)
        if path == "/api/agenda/status":
            return h._json(200, agenda.set_status(owner, data.get("id"), data.get("status")))
        if path == "/api/agenda/delete":
            agenda.delete(owner, data.get("id"))
            return h._json(200, {"ok": True})
    except AgendaMissing as exc:
        return h._error(404, str(exc))
    except AgendaError as exc:
        return h._error(400, str(exc))
    return h._error(404, "not found")


# ---- Google Calendar readers (built by serve()) ----

def _google_client(get_secret):
    values = [get_secret(name) for name in _GOOGLE_SECRETS]
    if not all(values):
        return None
    from igris import gcal

    return gcal.CalendarClient(*values)


def gcal_factory(get_secret=None, client_factory=None):
    """A callable that builds the owner's Google Calendar client, or None without a sign-in."""
    def factory():
        if client_factory is not None:
            return client_factory()
        getter = get_secret
        if getter is None:
            from igris.llm_openai import get_secret as getter
        return _google_client(getter)

    return factory


def gcal_read(factory, call) -> dict:
    """{"status", "events"} from call(client); statuses as calendar_loader documents."""
    client = factory()
    if client is None:
        return {"status": "not_configured", "events": []}
    from igris import gcal

    try:
        return {"status": "ok", "events": call(client)}
    except gcal.CalendarScopeError:
        return {"status": "needs_consent", "events": []}
    except gcal.CalendarAuthError:
        return {"status": "auth_down", "events": []}
    except gcal.CalendarError as exc:
        return {"status": "error", "error": str(exc), "events": []}


def calendar_loader(get_secret=None, client_factory=None, clock=time.monotonic):
    """Upcoming events of the owner's Google Calendar, read at most every CALENDAR_REFRESH s.

    Returns {"status": ..., "events": [...]} where status is ok, not_configured (no Google
    sign-in stored), needs_consent (the sign-in lacks calendar access: run
    tools/gmail_auth.py login again), auth_down or error. Secrets never leave this function.
    """
    lock = threading.Lock()
    cache = {"at": None, "value": None}
    factory = gcal_factory(get_secret, client_factory)

    def load():
        with lock:
            now = clock()
            if cache["at"] is None or now - cache["at"] >= CALENDAR_REFRESH:
                cache["value"] = gcal_read(factory, lambda c: c.upcoming(days=14, max_results=50))
                cache["at"] = now
            return cache["value"]

    return load


def calendar_month_loader(get_secret=None, client_factory=None, clock=time.monotonic):
    """(time_min, time_max) -> the owner's Google events in that window for the month view,
    each window read at most every CALENDAR_REFRESH s (a few months are kept)."""
    lock = threading.Lock()
    cache = {}
    factory = gcal_factory(get_secret, client_factory)

    def load(time_min, time_max):
        key = (round(time_min), round(time_max))
        with lock:
            now = clock()
            hit = cache.get(key)
            if hit is None or now - hit[0] >= CALENDAR_REFRESH:
                value = gcal_read(factory, lambda c: c.between(time_min, time_max))
                for old in sorted(cache, key=lambda k: cache[k][0])[:max(0, len(cache) - 5)]:
                    del cache[old]
                cache[key] = hit = (now, value)
            return hit[1]

    return load


def calendar_client(get_secret):
    """A factory for the calendar writer; None while no Google sign-in is stored, so a
    later tools/gmail_auth.py login takes effect without a restart."""
    return lambda: _google_client(get_secret)


# ---- Moodle and ESN sources of the owner's agenda ----

def build_agenda_sync(settings, get_secret, store, owner):
    """Moodle and shared ESN calendar sources for the owner's task calendar.

    Secrets are read on every pass, so ``python tools/set_keys.py moodle`` and a new Google
    sign-in take effect without a restart; the ESN ids come from IGRIS_ESN_CALENDAR_ID.
    """
    from igris.agenda_sources import AgendaSync, esn_source, moodle_source

    return AgendaSync(store, owner, {
        "moodle": moodle_source(lambda: get_secret("MOODLE_ICAL_URL")),
        "esn": esn_source(tuple(getattr(settings, "esn_calendar_ids", ())),
                          lambda: _google_client(get_secret)),
    })


def sync_agenda(sync, stop, interval=AGENDA_SYNC_INTERVAL) -> None:
    """Sync the calendar sources now and then every ``interval`` seconds until ``stop``."""
    last = None
    while True:
        try:
            status = sync.run()
            problem = "; ".join(f"{name}: {s.get('error') or s['reason']}"
                                for name, s in status.items() if s["state"] == "error") or None
        except Exception as exc:  # never end the thread
            problem = f"{type(exc).__name__}: {exc}"
        if problem and problem != last:
            print(f"igris: agenda sync failed: {problem}", file=sys.stderr)
        last = problem
        if stop.wait(interval):
            return


def first_admin(accounts):
    """Calendar sources feed the server owner: the first enabled admin."""
    def owner():
        for user in accounts.users():
            if user.get("role") == "admin" and user.get("enabled", True):
                return user["id"]
        return None
    return owner
