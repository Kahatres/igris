"""User-owned runtime isolation, with local echo backends only."""

from dataclasses import replace

import pytest

from igris.accounts import User
from igris.assistant import build_assistant
from igris.config import Settings
from igris.user_context import UserContexts


def owner_settings(tmp_path):
    return Settings(backends=("echo",), slow_backends=("echo",), user_backends=("echo",),
                    data_dir=tmp_path / "server", sessions_dir=tmp_path / "owner-sessions",
                    personas_dir=tmp_path / "owner-personas", workspace=tmp_path / "owner-workspace")


def test_owner_keeps_existing_data_and_regular_users_get_private_roots(tmp_path):
    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    admin = User("a" * 32, "admin", "Admin", "admin")
    user = User("b" * 32, "guest", "Guest", "user")
    assert contexts.get(admin)[0] is owner
    assistant, lock = contexts.get(user)
    assert contexts.get(user) == (assistant, lock)
    assert assistant.settings.sessions_dir != owner.settings.sessions_dir
    assert assistant.settings.workspace.is_dir()
    assert assistant.settings.budget_path != owner.settings.budget_path
    assert assistant.jobs is None
    owner.ask("owner secret")
    assistant.ask("user message")
    assert all("owner secret" not in x["content"] for x in assistant.memory.history())
    assert "user message" not in str(owner.memory.history())
    (owner.settings.workspace / "secret.txt").parent.mkdir(parents=True)
    (owner.settings.workspace / "secret.txt").write_text("owner-only")
    assert "outside workspace" in assistant._run_tool("read_file", {"path": "../secret.txt"})


def test_regular_users_cannot_inherit_cli_backends_or_host_workspace(tmp_path):
    owner = build_assistant(replace(owner_settings(tmp_path), user_backends=("codex",)))
    contexts = UserContexts(owner)
    with pytest.raises(ValueError, match="CLI"):
        contexts.get(User("b" * 32, "guest", "Guest", "user"))
    with pytest.raises(ValueError, match="user id"):
        contexts.get(User("../../owner", "bad", "bad", "user"))


def test_task_uses_private_transcript_and_does_not_switch_chat(tmp_path):
    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    user = User("b" * 32, "guest", "Guest", "user")
    assistant, _ = contexts.get(user)
    assistant.use_session("chat")
    assistant.ask("conversation")
    before = assistant.memory.history()
    task = {"id": "c" * 32, "owner": user.id, "prompt": "background work"}
    assert "background work" in contexts.run_task(user, task)
    assert assistant.session_name() == "chat" and assistant.memory.history() == before
    task["is_cancelled"] = lambda: True
    assert contexts.run_task(user, task) == ""
    task["owner"] = "a" * 32
    with pytest.raises(ValueError):
        contexts.run_task(user, task)


def test_upload_limits_come_from_settings(tmp_path):
    settings = replace(owner_settings(tmp_path), file_max_mb=3, file_quota_mb=7,
                       file_max_count=9)
    files = UserContexts(build_assistant(settings)).files
    assert (files.max_file, files.max_total, files.max_count) == (
        3 * 1024 * 1024, 7 * 1024 * 1024, 9)


def test_agenda_tools_reach_each_users_own_agenda_and_only_the_admin_sees_google(tmp_path):
    from igris.agenda import AgendaStore

    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    contexts.agenda = AgendaStore(tmp_path / "agenda.sqlite3")
    reads = []
    contexts.busy = lambda lo, hi: reads.append((lo, hi)) or {"status": "ok", "events": []}
    admin = User("a" * 32, "admin", "Admin", "admin")
    user = User("b" * 32, "guest", "Guest", "user")
    admin_assistant, _ = contexts.get(admin)
    user_assistant, _ = contexts.get(user)
    for assistant in (admin_assistant, user_assistant):
        assert {"agenda_add", "agenda_list", "agenda_remove", "agenda_update",
                "free_slots"} <= set(assistant.tools.names())
        assert "agenda_add" in assistant.system_prompt()
    user_assistant._run_tool("agenda_add", {"title": "Misafir işi", "branch": "kisisel",
                                            "due": "2030-01-02"})
    assert [i["title"] for i in contexts.agenda.list(user.id)] == ["Misafir işi"]
    assert contexts.agenda.list(admin.id) == []
    assert "not connected" in user_assistant._run_tool("free_slots", {})
    assert reads == []
    admin_assistant._run_tool("free_slots", {})
    assert len(reads) == 1


class _MailStore:
    def recent(self, limit=50):
        return [{"seen_at": "2999-01-01T00:00:00", "sender": "S", "subject": "Konu",
                 "category": "gorev", "task": None, "body": "GÖVDE"}]


def test_only_the_admin_gets_the_read_only_mail_tool(tmp_path):
    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    contexts.mail = _MailStore()
    admin = User("a" * 32, "admin", "Admin", "admin")
    user = User("b" * 32, "guest", "Guest", "user")
    admin_assistant, _ = contexts.get(admin)
    user_assistant, _ = contexts.get(user)
    assert "mail_recent" in admin_assistant.tools.names()
    assert "mail_recent" not in user_assistant.tools.names()
    out = admin_assistant._run_tool("mail_recent", {"hours": 24})
    assert "S — Konu" in out and "GÖVDE" not in out


class _Researcher:
    def __init__(self):
        self.queries = []

    def research(self, query, context=""):
        from igris.web_research import Research

        self.queries.append(query)
        return Research(status="ok", answer="cevap", sources=("https://e.org",), backend="fake")


def test_only_the_admin_gets_web_ara_in_web_and_telegram_sessions(tmp_path):
    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    contexts.web_research = _Researcher()
    admin = User("a" * 32, "admin", "Admin", "admin")
    user = User("b" * 32, "guest", "Guest", "user")
    admin_assistant, _ = contexts.get(admin)
    user_assistant, _ = contexts.get(user)
    assert "web_ara" in admin_assistant.tools.names()
    assert "web_ara" not in user_assistant.tools.names()
    assert "use web_ara" in admin_assistant.system_prompt()
    assert "- web_ara:" not in user_assistant.system_prompt()
    assert "use web_ara" not in user_assistant.system_prompt()
    out = admin_assistant._run_tool("web_ara", {"query": "python version"})
    assert out.startswith("WEB_RESEARCH status=ok") and "https://e.org" in out
    # the Telegram session runs on the same registry. A reply is a tool call only when it
    # is the whole reply, so the model here answers with exactly the tool-call line.
    from types import SimpleNamespace

    admin_assistant.router = SimpleNamespace(
        complete=lambda system, messages: ('TOOL: web_ara {"query": "q"}', "fake"))
    reply = contexts.ask_in_session(admin, "telegram", "ara")
    assert "web_ara" in reply.tool_calls and contexts.web_research.queries[-1] == "q"


def test_every_user_gets_doviz_kuru_unless_it_is_off(tmp_path):
    from igris.fx_rates import FxRates

    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    contexts.fx = FxRates(fetch=lambda: b"")  # never the network
    admin = User("a" * 32, "admin", "Admin", "admin")
    user = User("b" * 32, "guest", "Guest", "user")
    for assistant in (contexts.get(admin)[0], contexts.get(user)[0]):
        assert "doviz_kuru" in assistant.tools.names()
        assert "use doviz_kuru first" in assistant.system_prompt()
        assert assistant._run_tool("doviz_kuru", {"codes": "USD"}) == (
            "FX_RATES status=failed reason=bad_feed")
    off = UserContexts(build_assistant(replace(owner_settings(tmp_path / "off"),
                                               fx_rates=False)))
    assert off.fx is None
    assert "doviz_kuru" not in off.get(admin)[0].tools.names()


def test_web_ara_is_not_registered_when_turned_off(tmp_path):
    settings = replace(owner_settings(tmp_path), web_research=False)
    contexts = UserContexts(build_assistant(settings))
    assert contexts.web_research is None
    admin_assistant, _ = contexts.get(User("a" * 32, "admin", "Admin", "admin"))
    assert "web_ara" not in admin_assistant.tools.names()


def test_ask_in_session_uses_its_own_session_and_keeps_the_browser_selection(tmp_path):
    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    admin = User("a" * 32, "admin", "Admin", "admin")
    owner.use_session("web")
    owner.ask("browser message")
    reply = contexts.ask_in_session(admin, "telegram", "from the phone", note="plain text")
    assert "from the phone" in reply.text
    assert owner.session_name() == "web"
    assert owner.sessions.current()[0] == "web"
    assert "from the phone" not in str(owner.memory.history())
    names = {s["name"] for s in owner.list_sessions()}
    assert {"web", "telegram"} <= names
    contents = [m["content"] for m in owner.sessions.open("telegram").history()]
    assert contents[0] == "from the phone"


def test_telegram_session_gets_the_calendar_tools_and_the_same_retry(tmp_path):
    from igris.agenda import AgendaStore

    class Router:
        def __init__(self, replies):
            self.replies, self.calls = list(replies), []

        def complete(self, system, messages):
            self.calls.append((system, list(messages)))
            return self.replies.pop(0), f"b{len(self.calls)}"

    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    contexts.agenda = AgendaStore(tmp_path / "agenda.sqlite3")
    admin = User("a" * 32, "admin", "Admin", "admin")
    owner.router = Router([
        "Bu oturumda bağlı bir takvim aracı yok, ekleyemedim.",
        'TOOL: agenda_add {"items": [{"title": "Spor", "branch": "kisisel", '
        '"start": "2030-01-02T18:00", "due": "2030-01-02T20:30"}]}',
        "Spor 18:00-20:30 olarak eklendi.",
    ])
    reply = contexts.ask_in_session(admin, "telegram", "yarın 18.00-20.30 spor ekle", note="x")
    assert reply.text == "Spor 18:00-20:30 olarak eklendi."
    assert reply.tool_calls == ("agenda_add",)
    assert "agenda_update" in owner.router.calls[0][0]  # the tools are in the system prompt
    assert [i["title"] for i in contexts.agenda.list(admin.id)] == ["Spor"]
    stored = owner.sessions.open("telegram").history()
    assert [e["role"] for e in stored] == ["user", "tool", "assistant"]
    assert stored[-1]["backend"] == "b3"


def test_extra_prompt_is_added_to_the_system_prompt():
    assistant = build_assistant(Settings(backends=("echo",), slow_backends=("echo",)))
    assistant.extra_prompt = "Answer in plain text."
    assert assistant.system_prompt().endswith("Answer in plain text.")


def test_system_prompt_names_today():
    from datetime import datetime, timedelta, timezone

    assistant = build_assistant(Settings(backends=("echo",), slow_backends=("echo",)))
    assistant.today = lambda: datetime(2026, 11, 11, 9, 0, tzinfo=timezone(timedelta(hours=3)))
    assert "Today is Wednesday, 2026-11-11 (time zone UTC+0300)." in assistant.system_prompt()


def test_owner_gets_people_mail_tools_and_other_users_do_not(tmp_path):
    from types import SimpleNamespace
    from igris.people import PeopleStore
    from igris.outbox import OutboxStore
    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    contexts.people = PeopleStore(str(tmp_path / 'people.db'))
    contexts.outbox = OutboxStore(str(tmp_path / 'outbox.db'))
    contexts.mail_sender = SimpleNamespace(ask=lambda *args: 'request')
    admin = User('a' * 32, 'admin', 'Admin', 'admin')
    regular = User('b' * 32, 'user', 'User', 'user')
    admin_tools = contexts.get(admin)[0].tools
    other_tools = contexts.get(regular)[0].tools
    assert {'person_find', 'person_save', 'mail_draft', 'mail_ask'} <= set(admin_tools.names())
    assert not {'person_find', 'person_save', 'mail_draft', 'mail_ask'} & set(other_tools.names())
    assert not any('send' in name for name in admin_tools.names())
    admin_tools.set_turn_text('save Ece, ece@example.lt')
    admin_tools.run('person_save', {'name': 'Ece', 'email': 'ece@example.lt'})
    assert len(contexts.people.list(admin.id)) == 1
    assert not contexts.people.list(regular.id)
    assert 'Never invent' in contexts.get(admin)[0].system_prompt()


def test_import_tools_are_registered_per_user_and_read_only_their_own_uploads(tmp_path):
    from igris.agenda import AgendaStore

    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    contexts.agenda = AgendaStore(tmp_path / "agenda.sqlite3")
    admin = User("a" * 32, "admin", "Admin", "admin")
    user = User("b" * 32, "guest", "Guest", "user")
    from datetime import date, timedelta

    day =(date.today() + timedelta(days=30)).strftime("%Y%m%d")  # within the 12-month cap
    ics = (f"BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:1\nSUMMARY:Ders\nDTSTART:{day}T100000\n"
           f"DTEND:{day}T113000\nEND:VEVENT\nEND:VCALENDAR\n").encode()
    upload = contexts.files.upload(user.id, "ders.ics", ics)
    admin_assistant, _ = contexts.get(admin)
    user_assistant, _ = contexts.get(user)
    for assistant in (admin_assistant, user_assistant):
        assert {"agenda_import", "agenda_import_remove"} <= set(assistant.tools.names())
        assert "use agenda_import: preview first" in assistant.system_prompt()
    out = user_assistant._run_tool("agenda_import", {"batch": "b", "file_id": upload["id"],
                                                     "preview": False})
    assert out.startswith("imported batch=b: created 1"), out
    assert [i["title"] for i in contexts.agenda.list(user.id)] == ["Ders"]
    assert "file not found" in admin_assistant._run_tool(
        "agenda_import", {"batch": "b", "file_id": upload["id"]})


def test_core_and_study_tools_and_prompt_are_owner_only(tmp_path):
    from igris.agenda import AgendaStore
    from igris.study_tools import STUDY_PROMPT

    owner = build_assistant(owner_settings(tmp_path))
    contexts = UserContexts(owner)
    contexts.agenda = AgendaStore(tmp_path / "agenda.sqlite3")
    contexts.make_card = lambda text: "Card"
    admin = User("a" * 32, "admin", "Admin", "admin")
    regular = User("b" * 32, "user", "User", "user")
    tools = {"core_search", "core_read", "core_save_research", "study_setup", "study_exam_create",
             "study_topics_set", "study_problem_add", "study_mastery_set", "study_plan_set",
             "study_overview", "scene_show", "vec_compute"}
    assert tools <= set(contexts.get(admin)[0].tools.names())
    assert not tools.intersection(contexts.get(regular)[0].tools.names())
    assert STUDY_PROMPT in owner.system_prompt()
    assert STUDY_PROMPT not in contexts.get(regular)[0].system_prompt()
    result = owner._run_tool("core_save_research", {"title": "Research", "text": "Notes", "urls": []})
    assert "Saved document:" in result
    assert contexts.core.list(admin.id)[0]["card"] == "Card"


def test_disabled_core_study_have_no_tools(tmp_path):
    from igris.agenda import AgendaStore

    contexts = UserContexts(build_assistant(replace(owner_settings(tmp_path), core=False, study=False)))
    contexts.agenda = AgendaStore(tmp_path / "agenda.sqlite3")
    assistant, _ = contexts.get(User("a" * 32, "admin", "Admin", "admin"))
    assert "core_search" not in assistant.tools.names()
    assert "study_exam_create" not in assistant.tools.names()
