"""web_auth helpers with a fake request handler; the full flows are in test_web_accounts."""
from types import SimpleNamespace

import pytest

from igris import web_auth
from web_serving import FakeRequest, login


def ctx(**kw):
    base = dict(host="127.0.0.1", public_origin=None, cookie_secure=False, accounts=None,
                presence=None, edge_token=None, assistant=None, lock=None, contexts=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("path,admin", [
    ("/calis", True), ("/api/core", True), ("/api/core/doc", True), ("/api/study/chat", True),
    ("/api/multiagent/jobs", True), ("/api/admin/users", True), ("/api/inbox", True),
    ("/api/notices/read", True), ("/api/state", False), ("/api/agenda", False),
    ("/api/files", False), ("/api/coreX", False)])
def test_admin_only_paths(path, admin):
    assert web_auth.admin_only(path) is admin


def test_host_and_origin_checks():
    h = FakeRequest(headers={"Host": "127.0.0.1:8770"})
    assert web_auth.host_ok(h, ctx())
    assert not web_auth.host_ok(FakeRequest(headers={"Host": "evil:8770"}), ctx())
    public = ctx(public_origin="https://igris.example", cookie_secure=True)
    assert web_auth.host_ok(FakeRequest(headers={"Host": "igris.example"}), public)
    assert web_auth.origin_ok(h, ctx())
    assert web_auth.origin_ok(FakeRequest(headers={"Host": "a:1", "Origin": "http://a:1"}), ctx())
    assert not web_auth.origin_ok(FakeRequest(headers={"Host": "a:1", "Origin": "http://b"}), ctx())
    assert web_auth.origin_ok(FakeRequest(headers={"Origin": "https://igris.example"}), public)


def test_session_cookie():
    assert web_auth.token(FakeRequest(headers={"Cookie": "a=1; igris_session=tok"})) == "tok"
    assert web_auth.token(FakeRequest(headers={"Cookie": "bad\x00"})) is None
    h = FakeRequest()
    web_auth.set_cookie(h, ctx(cookie_secure=True), "tok", 60)
    assert h.response_cookie.startswith("igris_session=tok; Path=/; HttpOnly; SameSite=Strict")
    assert h.response_cookie.endswith("Max-Age=60; Secure")


def test_without_accounts_everyone_is_the_local_owner():
    h = FakeRequest("/api/admin/users")
    assert web_auth.authenticate(h, ctx(assistant="A", lock="L"))
    assert (h.assistant, h.user_lock, h.login) == ("A", "L", None)
    assert web_auth.csrf_ok(h, ctx()) and web_auth.activate(h, ctx())


def test_non_admin_is_refused_admin_paths():
    accounts = SimpleNamespace(session=lambda token: login(role="user"))
    h = FakeRequest("/api/inbox")
    assert not web_auth.authenticate(h, ctx(accounts=accounts))
    assert h.status == 403


def test_csrf_must_match():
    h = FakeRequest(headers={"X-CSRF-Token": "wrong"}, login=login())
    assert not web_auth.csrf_ok(h, ctx(accounts=object()))
    assert h.status == 403


def test_device_token():
    h = FakeRequest()
    assert not web_auth.device_authenticated(h, ctx()) and h.status == 404
    c = ctx(presence=object(), edge_token="t")
    h = FakeRequest(headers={"Authorization": "Bearer x"})
    assert not web_auth.device_authenticated(h, c) and h.status == 401
    assert web_auth.device_authenticated(FakeRequest(headers={"Authorization": "Bearer t"}), c)


def test_prefs_need_accounts_and_a_safe_user_id(tmp_path):
    h = FakeRequest()
    web_auth.prefs(h, ctx(), "/api/prefs")
    assert h.status == 404
    c = ctx(accounts=object(), assistant=SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path)))
    h = FakeRequest(login=login("u1"))
    web_auth.prefs(h, c, "/api/prefs")
    assert (h.status, h.body) == (200, {"view": "odak"})
    web_auth.prefs(h, c, "/api/prefs", {"view": "klasik"})
    web_auth.prefs(h, c, "/api/prefs")
    assert h.body == {"view": "klasik"}
    web_auth.prefs(h, c, "/api/prefs", {"view": "x"})
    assert h.status == 400
    bad = FakeRequest(login=login("../x"))
    web_auth.prefs(bad, c, "/api/prefs")
    assert bad.status == 404
