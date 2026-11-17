"""Web server access: allowed hosts and origins, session cookies, CSRF, device tokens,
sign-in and sign-out, and each user's saved view.

Route functions take the request handler ``h`` and the server context ``ctx``.
Depends on accounts and presence (imported where they are used).
"""

import hmac
import json
import os
import re
import time
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

MAX_DEVICE_NAME = 64
PREFS_USER_RE = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
VIEWS = ("odak", "klasik")  # v2 views; the first is the default
_ADMIN_PATHS = ("/calis", "/api/core", "/api/study", "/api/presence", "/api/inbox",
                "/api/calendar", "/api/notices", "/api/notices/read")
_ADMIN_PREFIXES = ("/api/multiagent/", "/api/core/", "/api/study/", "/api/admin/")


def admin_only(path: str) -> bool:
    return path in _ADMIN_PATHS or path.startswith(_ADMIN_PREFIXES)


def host_ok(h, ctx) -> bool:
    actual_port = h.server.server_address[1]
    allowed = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}",
               f"{ctx.host}:{actual_port}"}
    if ctx.public_origin:
        allowed = {urlsplit(ctx.public_origin).netloc}
    return h.headers.get("Host") in allowed


def origin_ok(h, ctx) -> bool:
    """A browser's Origin, when sent, must be this server."""
    origin = h.headers.get("Origin")
    scheme = "https://" if ctx.cookie_secure else "http://"
    return origin is None or origin == (ctx.public_origin or scheme + h.headers.get("Host", ""))


def token(h):
    cookie = SimpleCookie()
    try:
        cookie.load(h.headers.get("Cookie", ""))
        item = cookie.get("igris_session")
        return item.value if item else None
    except CookieError:
        return None


def set_cookie(h, ctx, value: str, max_age: int) -> None:
    h.response_cookie = (
        f"igris_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        + ("; Secure" if ctx.cookie_secure else ""))


def authenticate(h, ctx) -> bool:
    """Sign the request in: pick the user's assistant and lock, or answer 401/403."""
    h.assistant, h.user_lock = ctx.assistant, ctx.lock
    h.login = None
    if ctx.accounts is None:
        return True
    h.token = token(h)
    h.login = ctx.accounts.session(h.token)
    if h.login is None:
        h._error(401, "Authentication required")
        return False
    if admin_only(h._path()) and h.login.user.role != "admin":
        h._error(403, "Administrator access required")
        return False
    h.assistant, h.user_lock = ctx.contexts.get(h.login.user)
    return True


def activate(h, ctx, *, select=True) -> bool:
    if ctx.accounts is None:
        return True
    # A queued request must not outlive a password reset or logout.
    h.login = ctx.accounts.session(h.token)
    if h.login is None:
        h._error(401, "Authentication required")
        return False
    if select:
        h.assistant.use_session(h.login.conversation)
        h.assistant.set_persona(h.login.persona)
    return True


def csrf_ok(h, ctx) -> bool:
    if ctx.accounts is None:
        return True
    if not hmac.compare_digest(h.headers.get("X-CSRF-Token", ""), h.login.csrf):
        h._error(403, "Invalid CSRF token")
        return False
    return True


def device_authenticated(h, ctx) -> bool:
    """A device report carries the edge token instead of a login."""
    if ctx.presence is None or not ctx.edge_token:
        h._error(404, "not found")
        return False
    sent = h.headers.get("Authorization", "").encode("utf-8", "replace")
    if not hmac.compare_digest(sent, b"Bearer " + ctx.edge_token.encode("utf-8")):
        h._error(401, "Authentication required")
        return False
    return True


def presence_report(h, ctx, path, data):
    """Accept an authenticated device report using the server's clock, so a device with
    a wrong clock cannot go stale or stay fresh forever."""
    if not device_authenticated(h, ctx):
        return
    from igris.presence import Report

    device = data.get("device")
    if isinstance(device, str) and len(device) > MAX_DEVICE_NAME:
        return h._error(400, "device name is too long")
    try:
        report = Report(device, data.get("category"), data.get("idle_seconds"), time.time())
    except (TypeError, ValueError) as exc:
        return h._error(400, str(exc))
    ctx.presence.update(report)
    return h._json(200, {"ok": True})


def presence_state(h, ctx, path):
    if ctx.presence is None:
        return h._error(404, "not found")
    return h._json(200, ctx.presence.state())


def login(h, ctx, path, data):
    if ctx.accounts is None:
        return h._error(404, "not found")
    from igris.accounts import AccountError, LoginLimited, SESSION_SECONDS

    try:
        new = ctx.accounts.login(data.get("username"), data.get("password"), h.client_address[0])
    except LoginLimited as exc:
        return h._error(429, str(exc))
    except AccountError as exc:
        return h._error(401, str(exc))
    previous = token(h)
    if previous:
        ctx.accounts.logout(previous)
    set_cookie(h, ctx, new, SESSION_SECONDS)
    h._json(200, {"ok": True})


def sign_out(h, ctx, path, data):
    """/api/auth/logout, and /api/auth/password, which also ends the session."""
    if ctx.accounts is None:
        return h._error(404, "not found")
    if path == "/api/auth/password":
        from igris.accounts import AccountError

        try:
            ctx.accounts.change_password(h.token, data.get("current_password"),
                                         data.get("password"))
        except AccountError as exc:
            return h._error(400, str(exc))
    else:
        ctx.accounts.logout(h.token)
    set_cookie(h, ctx, "", 0)
    return h._json(200, {"ok": True})


def me(h, ctx, path):
    return h._json(200, {"enabled": True, "user": h.login.user.public(), "csrf": h.login.csrf})


def users(h, ctx, path):
    if ctx.accounts is None:
        return h._error(404, "not found")
    return h._json(200, {"users": ctx.accounts.users()})


def _prefs_file(h, ctx) -> Path | None:
    if ctx.accounts is None or not PREFS_USER_RE.fullmatch(h.login.user.id):
        return None
    return Path(ctx.assistant.settings.data_dir) / "prefs" / (h.login.user.id + ".json")


def prefs(h, ctx, path, data=None) -> None:
    """The signed-in user's v2 view; without accounts the page keeps it in localStorage."""
    target = _prefs_file(h, ctx)
    if target is None:
        return h._error(404, "not found")
    if data is not None:
        if data.get("view") not in VIEWS:
            return h._error(400, "view must be odak or klasik")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(json.dumps({"view": data["view"]}), encoding="utf-8")
        os.replace(temp, target)
        return h._json(200, {"view": data["view"]})
    try:
        view = json.loads(target.read_text(encoding="utf-8")).get("view")
    except (OSError, ValueError, AttributeError):
        view = None
    return h._json(200, {"view": view if view in VIEWS else VIEWS[0]})
