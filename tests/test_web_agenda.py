"""web_agenda routes and helpers with fakes; HTTP flows are in test_web_accounts."""
from types import SimpleNamespace

from igris import web_agenda
from igris.agenda import AgendaStore
from web_serving import FakeRequest, login

OWNER = "c" * 32


def ctx(tmp_path, **kw):
    base = dict(accounts=object(), agenda=AgendaStore(tmp_path / "agenda.sqlite3"),
                calendar_sync=None, calendar_loader=lambda: {"status": "ok", "events": [1]},
                month_loader=lambda a, b: {"status": "disabled", "events": []})
    base.update(kw)
    return SimpleNamespace(**base)


def test_calendar_comes_from_the_loader(tmp_path):
    h = FakeRequest()
    web_agenda.calendar(h, ctx(tmp_path), "/api/calendar")
    assert (h.status, h.body) == (200, {"status": "ok", "events": [1]})


def test_agenda_needs_accounts(tmp_path):
    h = FakeRequest(login=login(OWNER))
    web_agenda.agenda(h, ctx(tmp_path, accounts=None), "/api/agenda")
    assert h.status == 404
    web_agenda.month(h, ctx(tmp_path, accounts=None), "/api/agenda/month")
    assert h.status == 404


def test_add_then_month_view(tmp_path):
    c = ctx(tmp_path)
    h = FakeRequest(login=login(OWNER))
    web_agenda.agenda(h, c, "/api/agenda/add", {"branch": "okul", "kind": "exam",
                                                "title": "Vize", "due": "2099-01-15"})
    assert h.status == 201 and h.body["source"] == "manual"
    h = FakeRequest("/api/agenda/month?month=2099-01", login=login(OWNER))
    web_agenda.month(h, c, "/api/agenda/month")
    assert h.status == 200 and [i["title"] for i in h.body["items"]] == ["Vize"]
    assert h.body["google"]["status"] == "disabled"  # the admin also gets Google events


def test_month_must_be_year_and_month(tmp_path):
    h = FakeRequest("/api/agenda/month?month=2099-1", login=login(OWNER))
    web_agenda.month(h, ctx(tmp_path), "/api/agenda/month")
    assert h.status == 400


def test_google_helpers_without_a_sign_in():
    assert web_agenda.calendar_client(lambda name: None)() is None
    assert web_agenda.gcal_read(lambda: None, None) == {"status": "not_configured", "events": []}


def test_first_admin_skips_disabled_admins():
    accounts = SimpleNamespace(users=lambda: [
        {"id": "u", "role": "user"}, {"id": "a1", "role": "admin", "enabled": False},
        {"id": "a2", "role": "admin"}])
    assert web_agenda.first_admin(accounts)() == "a2"
    assert web_agenda.first_admin(SimpleNamespace(users=lambda: []))() is None
