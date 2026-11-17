"""Tests for igris.assistant."""

from dataclasses import replace

import pytest

from igris.assistant import Assistant, Reply, build_assistant
from igris.config import Settings
from igris.memory import Memory
from igris.tools import ToolError


class ScriptedRouter:
    """Router stand-in returning scripted replies and recording calls."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, messages):
        self.calls.append((system, list(messages)))
        text = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return text, f"fake{len(self.calls)}"


class FakeTools:
    def __init__(self):
        self.ran = []

    def describe(self):
        return "- upper(text): uppercases"

    def run(self, name, args):
        self.ran.append((name, args))
        if name == "upper":
            return args["text"].upper()
        if name == "boom":
            raise ToolError("it broke")
        raise KeyError(name)


def make(tmp_path, replies, max_steps=3):
    router = ScriptedRouter(replies)
    memory = Memory(tmp_path / "mem.jsonl")
    tools = FakeTools()
    assistant = Assistant(Settings(), router, memory, tools, max_tool_steps=max_steps)
    return assistant, router, memory, tools


def test_per_call_step_limit_does_not_change_default(tmp_path):
    assistant, router, _, tools = make(tmp_path, ['TOOL: upper {"text": "a"}'], max_steps=2)
    reply = assistant.ask("loop", max_tool_steps=10)
    assert len(reply.tool_calls) == len(tools.ran) == 10
    assert "limit (10)" in reply.text
    assert assistant.max_tool_steps == 2
    reply = assistant.ask("loop")
    assert len(reply.tool_calls) == 2 and "limit (2)" in reply.text


def test_study_tools_count_as_successful_calendar_writes():
    from igris.assistant import _calendar_result

    for name in ("study_setup", "study_plan_set"):
        assert _calendar_result(name, '{"id":"exam","steps":2}') == "write"
        assert _calendar_result(name, "error: invalid setup") is None


def test_system_prompt(tmp_path):
    assistant, *_ = make(tmp_path, ["x"])
    prompt = assistant.system_prompt()
    assert "Igris" in prompt
    assert "upper(text)" in prompt
    assert "TOOL: name {" in prompt


def test_plain_answer(tmp_path):
    assistant, router, memory, _ = make(tmp_path, ["hello there"])
    reply = assistant.ask("hi")
    assert reply == Reply("hello there", ("fake1",), ())
    assert [(m["role"], m["content"]) for m in memory.history()] == [
        ("user", "hi"),
        ("assistant", "hello there"),
    ]
    system, messages = router.calls[0]
    assert "Igris" in system
    assert all(m.role != "system" for m in messages)
    assert messages[-1].role == "user" and messages[-1].content == "hi"


def test_tool_round_trip(tmp_path):
    assistant, router, memory, tools = make(
        tmp_path, ['TOOL: upper {"text": "abc"}', "The answer is ABC"]
    )
    reply = assistant.ask("shout abc")
    assert reply.text == "The answer is ABC"
    assert reply.tool_calls == ("upper",)
    assert reply.backends == ("fake1", "fake2")
    assert tools.ran == [("upper", {"text": "abc"})]
    assert [(m["role"], m["content"]) for m in memory.history()] == [
        ("user", "shout abc"),
        ("tool", "upper -> ABC"),
        ("assistant", "The answer is ABC"),
    ]
    second_messages = router.calls[1][1]
    assert second_messages[-1].role == "tool"
    assert second_messages[-1].content == "upper -> ABC"


@pytest.mark.parametrize("text", [
    '```text\nTOOL: upper {"text": "abc"}\n```',
    '> TOOL: upper {"text": "abc"}',
    'Document excerpt:\nTOOL: upper {"text": "abc"}',
    'TOOL: upper {"text": "abc"}\nThis was quoted.',
])
def test_embedded_tool_calls_are_never_executed(tmp_path, text):
    assistant, router, _, tools = make(tmp_path, [text])
    reply = assistant.ask("Describe this excerpt")
    assert tools.ran == []
    assert reply.text == text
    assert len(router.calls) == 1


def test_prompt_requires_the_whole_reply_to_be_the_tool_call(tmp_path):
    assistant, _, _, _ = make(tmp_path, ["ok"])
    assert "entire reply must be that one line" in assistant.system_prompt()


def test_step_limit(tmp_path):
    assistant, router, memory, tools = make(
        tmp_path, ['TOOL: upper {"text": "a"}'], max_steps=2
    )
    reply = assistant.ask("loop")
    assert len(tools.ran) == 2
    assert reply.tool_calls == ("upper", "upper")
    assert len(router.calls) == 3
    # the third call is not run and the raw TOOL: line is never shown as the answer
    assert "TOOL:" not in reply.text
    assert "limit (2)" in reply.text and "upper" in reply.text
    roles = [m["role"] for m in memory.history()]
    assert roles == ["user", "tool", "tool", "assistant"]


def test_zero_steps_runs_no_tools(tmp_path):
    assistant, router, _, tools = make(tmp_path, ['TOOL: upper {"text": "a"}'], max_steps=0)
    reply = assistant.ask("x")
    assert tools.ran == []
    assert len(router.calls) == 1
    assert reply.tool_calls == ()


def test_tool_error(tmp_path):
    assistant, _, memory, _ = make(tmp_path, ["TOOL: boom {}", "sorry"])
    reply = assistant.ask("go")
    assert reply.text == "sorry"
    assert reply.tool_calls == ("boom",)
    tool_msgs = [m["content"] for m in memory.history() if m["role"] == "tool"]
    assert tool_msgs == ["boom -> error: it broke"]


def test_unknown_tool(tmp_path):
    assistant, _, memory, _ = make(tmp_path, ["TOOL: nope {}", "ok"])
    reply = assistant.ask("go")
    assert reply.text == "ok"
    tool_msgs = [m["content"] for m in memory.history() if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].startswith("nope -> error:")


def test_build_assistant_with_echo(tmp_path):
    settings = replace(Settings(), backends=("echo",), memory_path=tmp_path / "m.jsonl")
    assistant = build_assistant(settings)
    reply = assistant.ask("ping")
    assert reply.text == "echo: ping"
    assert reply.backends == ("echo",)


def make_sessions(tmp_path, replies):
    from igris.sessions import SessionManager

    router = ScriptedRouter(replies)
    sessions = SessionManager(tmp_path / "sessions")
    assistant = Assistant(
        Settings(), router, Memory(tmp_path / "x.jsonl"), FakeTools(), sessions=sessions
    )
    return assistant, router


def test_sessions_disabled_raises(tmp_path):
    import pytest

    assistant, *_ = make(tmp_path, ["x"])
    for call in (
        lambda: assistant.use_session("a"),
        assistant.session_name,
        assistant.list_sessions,
    ):
        with pytest.raises(RuntimeError, match="sessions are not enabled"):
            call()


def test_session_switch_changes_history(tmp_path):
    assistant, router = make_sessions(tmp_path, ["ok"])
    assert assistant.session_name() == "default"
    assistant.ask("first")
    assistant.use_session("other")
    assert assistant.session_name() == "other"
    assistant.ask("second")
    contents = [m.content for m in router.calls[-1][1]]
    assert contents == ["second"]
    assistant.use_session("default")
    assistant.ask("third")
    contents = [m.content for m in router.calls[-1][1]]
    assert contents[0] == "first" and contents[-1] == "third"
    names = {s["name"] for s in assistant.list_sessions()}
    assert {"default", "other"} <= names


def test_persona_in_system_prompt(tmp_path):
    import pytest

    from igris.personas import BUILTIN, PersonaError, persona_prompt

    assistant, router = make_sessions(tmp_path, ["ok"])
    base = assistant.system_prompt()
    assert assistant.persona_name() == "default"
    assert "coder" in assistant.list_personas()
    assert assistant.list_personas() == sorted(assistant.list_personas())
    assistant.set_persona("coder")
    prompt = assistant.system_prompt()
    assert prompt.startswith(base)
    assert persona_prompt(BUILTIN["coder"]) in prompt
    assistant.ask("hi")
    assert persona_prompt(BUILTIN["coder"]) in router.calls[0][0]
    with pytest.raises(PersonaError):
        assistant.set_persona("nope")


def test_build_assistant_sessions_and_persona(tmp_path):
    settings = replace(
        Settings(),
        backends=("echo",),
        sessions_dir=tmp_path / "s",
        personas_dir=tmp_path / "p",
        persona="coder",
    )
    assistant = build_assistant(settings)
    assert assistant.persona_name() == "coder"
    assert assistant.session_name() == "default"
    assistant.ask("hi")
    assert (tmp_path / "s").exists()


def test_build_assistant_unknown_persona_falls_back(tmp_path):
    settings = replace(
        Settings(),
        backends=("echo",),
        sessions_dir=tmp_path / "s",
        personas_dir=tmp_path / "p",
        persona="missing",
    )
    assert build_assistant(settings).persona_name() == "default"


def test_file_tools_only_with_workspace(tmp_path):
    base = replace(
        Settings(),
        backends=("echo",),
        sessions_dir=tmp_path / "s",
        personas_dir=tmp_path / "p",
    )
    without = build_assistant(base)
    assert "read_file" not in without.tools.describe()
    ws = tmp_path / "ws"
    ws.mkdir()
    with_ws = build_assistant(replace(base, workspace=ws))
    desc = with_ws.tools.describe()
    assert "read_file" in desc and "list_dir" in desc and "search_text" in desc


def test_ask_slow_uses_slow_router(tmp_path):
    fast = ScriptedRouter(["fast"])
    slow = ScriptedRouter(['TOOL: upper {"text": "a"}', "slow done"])
    assistant = Assistant(Settings(), fast, Memory(tmp_path / "m.jsonl"), FakeTools(),
                          slow_router=slow)
    reply = assistant.ask("x", slow=True)
    assert reply.text == "slow done"
    assert len(slow.calls) == 2 and fast.calls == []
    assert assistant.ask("y").text == "fast"
    assert len(fast.calls) == 1


def test_slow_router_defaults_to_main(tmp_path):
    assistant, router, _, _ = make(tmp_path, ["ok"])
    assert assistant.slow_router is router
    assistant.ask("x", slow=True)
    assert len(router.calls) == 1


def _fake_openai(monkeypatch):
    import sys
    import types

    created = []

    class Budget:
        def __init__(self, path, daily_usd):
            created.append((path, daily_usd))

    class Backend:
        name = "openai"

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def complete(self, system, messages):
            return "from openai"

    mod = types.ModuleType("igris.llm_openai")
    mod.Budget = Budget
    mod.OpenAIAPIBackend = Backend
    monkeypatch.setitem(sys.modules, "igris.llm_openai", mod)
    return created


def test_build_assistant_no_budget_without_openai(tmp_path, monkeypatch):
    created = _fake_openai(monkeypatch)
    settings = replace(Settings(), backends=("echo",), slow_backends=("echo",),
                       sessions_dir=tmp_path / "s", personas_dir=tmp_path / "p")
    build_assistant(settings)
    assert created == []


def test_build_assistant_two_routers_one_budget(tmp_path, monkeypatch):
    created = _fake_openai(monkeypatch)
    settings = replace(Settings(), backends=("echo",), slow_backends=("openai",),
                       budget_path=tmp_path / "b.json", openai_daily_budget_usd=3.0,
                       sessions_dir=tmp_path / "s", personas_dir=tmp_path / "p")
    assistant = build_assistant(settings)
    assert created == [(tmp_path / "b.json", 3.0)]
    assert assistant.ask("hi").backends == ("echo",)
    reply = assistant.ask("hi", slow=True)
    assert reply.text == "from openai" and reply.backends == ("openai",)


def test_build_assistant_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRIS_BACKENDS", "echo")
    monkeypatch.chdir(tmp_path)
    assistant = build_assistant()
    reply = assistant.ask("hello")
    assert reply.text == "echo: hello"
    assert reply.backends == ("echo",)


# -- routing to multiagent ----------------------------------------------------

from pathlib import Path  # noqa: E402

from igris.llm import BackendError, PaidUsageBlocked  # noqa: E402
from igris.multiagent_jobs import Job  # noqa: E402

BIG = "tüm projeyi incele ve planla"


class FakeJobs:
    def __init__(self, exc=None):
        self.exc = exc
        self.submitted = []
        self.on_finish = None

    def submit(self, goal, *, context=None, **kw):
        self.submitted.append((goal, context, kw))
        if self.exc is not None:
            raise self.exc
        return Job(id="job1", goal=goal, status="queued", parallel=False, context=context,
                   implement=False, test_cmd=None, run_id=None, submitted=0.0)


def make_jobs(tmp_path, replies, jobs, settings=None):
    router = ScriptedRouter(replies)
    memory = Memory(tmp_path / "mem.jsonl")
    a = Assistant(settings or Settings(), router, memory, FakeTools(), jobs=jobs)
    return a, router, memory


def test_no_jobs_answers_normally(tmp_path):
    a, router, _ = make_jobs(tmp_path, ["ok"], None)
    reply = a.ask(BIG)
    assert reply.job_id is None and reply.text == "ok" and len(router.calls) == 1


def test_big_task_goes_to_multiagent(tmp_path):
    jobs = FakeJobs()
    settings = replace(Settings(), workspace=tmp_path)
    a, router, memory = make_jobs(tmp_path, ["unused"], jobs, settings)
    reply = a.ask(BIG)
    assert jobs.submitted[0][0] == BIG and jobs.submitted[0][1] == tmp_path
    assert reply.job_id == "job1" and reply.backends == ("routing",)
    assert "multiagent" in reply.text and "kurallara göre" in reply.text.casefold()
    assert router.calls == []
    assert [m["role"] for m in memory.history()] == ["user", "assistant"]


def test_small_question_not_submitted(tmp_path):
    jobs = FakeJobs()
    a, router, _ = make_jobs(tmp_path, ["answer"], jobs)
    reply = a.ask("what is a list?")
    assert jobs.submitted == [] and reply.text == "answer" and reply.job_id is None


def test_slow_skips_routing(tmp_path):
    jobs = FakeJobs()
    a, router, _ = make_jobs(tmp_path, ["slow answer"], jobs)
    reply = a.ask(BIG, slow=True)
    assert jobs.submitted == [] and reply.text == "slow answer"


def test_paid_blocked_gives_explanation(tmp_path):
    a, router, memory = make_jobs(tmp_path, ["x"], FakeJobs(PaidUsageBlocked("paid")))
    reply = a.ask(BIG)
    assert reply.job_id is None and router.calls == []
    assert "IGRIS_ALLOW_PAID" in reply.text and "ücretli" in reply.text
    en = a.ask("review the whole project and plan it")
    assert en.job_id is None and "paid" in en.text.lower() and "ücretli" not in en.text
    assert memory.history()[-1]["role"] == "assistant"


def test_value_error_gives_explanation(tmp_path):
    a, _, _ = make_jobs(tmp_path, ["x"], FakeJobs(ValueError("bad context")))
    reply = a.ask(BIG)
    assert reply.job_id is None and "bad context" in reply.text


def test_classifier_single_request_and_backend_error(tmp_path):
    jobs = FakeJobs()
    # gray zone: one big keyword (+2) only
    gray = "please review this"
    a, router, _ = make_jobs(tmp_path, ["multiagent"], jobs)
    reply = a.ask(gray)
    assert len(router.calls) == 1 and reply.job_id == "job1"
    assert len(router.calls[0][1]) == 1

    class Failing:
        calls = 0

        def complete(self, system, messages):
            raise BackendError("down")

    jobs2 = FakeJobs()
    b = Assistant(Settings(), Failing(), Memory(tmp_path / "m2.jsonl"), FakeTools(), jobs=jobs2)
    assert b._classify(gray) == "self"


def test_job_finished_messages(tmp_path):
    a, _, memory = make_jobs(tmp_path, ["x"], FakeJobs())
    for status in ("done", "failed", "cancelled"):
        goal = "tüm projeyi incele " + "a" * 100
        a.job_finished(Job(id="j", goal=goal, status=status, parallel=False, context=None,
                           implement=False, test_cmd=None, run_id="run-7", submitted=0.0))
    msgs = memory.history()
    assert len(msgs) == 3 and all(m["role"] == "assistant" for m in msgs)
    assert goal[:80] in msgs[0]["content"] and goal[:81] not in msgs[0]["content"]
    assert "run-7" in msgs[1]["content"]


def test_build_assistant_wires_jobs(tmp_path):
    settings = replace(Settings(), multiagent_dir=tmp_path / "ma", jobs_path=tmp_path / "j.json",
                       sessions_dir=tmp_path / "s", backends=("claude",), slow_backends=("claude",))
    a = build_assistant(settings)
    assert a.jobs is not None and a.jobs.on_finish == a.job_finished


# ---- final review fixes ----

class ResultJobs(FakeJobs):
    def __init__(self, result):
        super().__init__()
        self._result = result

    def result(self, job):
        return dict(self._result)


def finished(a, status, goal="tüm projeyi incele"):
    a.job_finished(Job(id="j", goal=goal, status=status, parallel=False, context=None,
                       implement=False, test_cmd=None, run_id="20261026_101010",
                       submitted=0.0))
    return a.memory.history()[-1]["content"]


def test_job_finished_done_includes_final_path_and_excerpt(tmp_path):
    res = {"final": "C:/ma/runs/20261026_101010/FINAL.md", "excerpt": "# Sonuç\nbulgular",
           "error": None, "log": None, "log_tail": None}
    a, _, _ = make_jobs(tmp_path, ["x"], ResultJobs(res))
    msg = finished(a, "done")
    assert "tamamlandı" in msg and res["final"] in msg and "# Sonuç\nbulgular" in msg
    en = finished(a, "done", goal="review the whole project")
    assert "finished" in en and "Result file" in en and res["final"] in en


def test_job_finished_failed_includes_run_error(tmp_path):
    res = {"final": None, "excerpt": None, "error": "RuntimeError: SPENDING BLOCKED",
           "log": "/x/j.log", "log_tail": "Traceback"}
    a, _, _ = make_jobs(tmp_path, ["x"], ResultJobs(res))
    msg = finished(a, "failed")
    assert "RuntimeError: SPENDING BLOCKED" in msg and "Traceback" not in msg


def test_job_finished_failed_falls_back_to_log_tail(tmp_path):
    res = {"final": None, "excerpt": None, "error": None,
           "log": "/x/j.log", "log_tail": "Traceback: boom"}
    a, _, _ = make_jobs(tmp_path, ["x"], ResultJobs(res))
    msg = finished(a, "failed")
    assert "Traceback: boom" in msg and "/x/j.log" in msg
    en = finished(a, "failed", goal="review the whole project")
    assert "Log" in en and "Traceback: boom" in en


def test_job_finished_result_errors_are_ignored(tmp_path):
    class Broken(FakeJobs):
        def result(self, job):
            raise OSError("disk")

    a, _, _ = make_jobs(tmp_path, ["x"], Broken())
    assert "tamamlandı" in finished(a, "done")


def test_paid_hint_mentions_both_switches(tmp_path):
    a, _, _ = make_jobs(tmp_path, ["x"], FakeJobs(PaidUsageBlocked("paid")))
    tr = a.ask(BIG).text
    assert "IGRIS_ALLOW_PAID=1" in tr and "ALLOW_PAID=1" in tr.replace("IGRIS_ALLOW_PAID=1", "")
    en = a.ask("review the whole project and plan it").text
    assert "IGRIS_ALLOW_PAID=1" in en and "ALLOW_PAID=1" in en.replace("IGRIS_ALLOW_PAID=1", "")


def test_ask_route_false_never_submits(tmp_path):
    jobs = FakeJobs()
    a, router, _ = make_jobs(tmp_path, ["direct"], jobs)
    reply = a.ask(BIG, route=False)
    assert jobs.submitted == [] and reply.text == "direct" and reply.job_id is None


# ---- calendar tools: honesty checks, retry, backend attribution ----------------

import re  # noqa: E402

import pytest  # noqa: E402
from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402

from igris.agenda import AgendaStore  # noqa: E402
from igris.agenda_tools import register_agenda_tools  # noqa: E402
from igris.assistant import claims_calendar_change, claims_no_calendar  # noqa: E402
from igris.tools import ToolRegistry  # noqa: E402

TZ = _tz(_td(hours=2))
NOW = _dt(2026, 11, 12, 18, 0, tzinfo=TZ)  # Thursday
OWNER = "a" * 32


class CalendarTools(FakeTools):
    """FakeTools that also has the calendar tool names, so the checks are on."""

    def names(self):
        return ["agenda_add", "agenda_list", "agenda_remove", "agenda_update", "free_slots",
                "upper"]

    def run(self, name, args):
        self.ran.append((name, args))
        if name == "agenda_add":
            return "1. added id=abc [kisisel/other] Spor — 2026-11-13 Fri 18:00-20:30"
        if name == "agenda_list":
            return "no items"
        return super().run(name, args) if name in ("upper", "boom") else "error: nope"


def make_calendar(tmp_path, replies):
    router = ScriptedRouter(replies)
    memory = Memory(tmp_path / "mem.jsonl")
    tools = CalendarTools()
    return Assistant(Settings(), router, memory, tools), router, memory, tools


CAL_REQUEST = "Takvimdeki vardiyalarımın bitiş tarihlerini kontrol et."


@pytest.mark.parametrize("text, request_text", [
    # the two real failures, as the owner saw them
    ("Bu oturumda takvime yazabilen bağlı bir araç bulunmadığından sınav kaydını ekleyemedim.",
     "Ayın 19'unda sınavım var, takvime ekle"),
    ("Önceki işlem kaydı bitiş tarihlerini göstermediğinden doğru kaydedildiğini doğrulayamıyorum.",
     "yanlış eklemişsin cumartesi 22.00 pazar 08.00"),
    ("I cannot verify the end dates.", "Check the end dates of my calendar shifts."),
    # Turkish typed without Turkish characters
    ("Takvime yazabilecek bagli bir arac bulunmuyor.", ""),
    ("Takvim düzenleme aracım olmadığı için düzeltemedim.", ""),
    ("Takvimine erişemiyorum.", ""),
    ("I don't have access to your calendar.", ""),
    ("I can't add events to your calendar from here.", ""),
    ("I cannot verify the end dates in the calendar.", ""),
    ("There is no calendar tool connected in this session.", ""),
])
def test_detects_claims_of_missing_calendar_access(text, request_text):
    assert claims_no_calendar(text, request_text)


@pytest.mark.parametrize("text, request_text", [
    # a true limit about another capability, next to a list that names the calendar
    ("Okulun web sitesine erişemiyorum. Elimdeki araçlar dosya okuma, takvim ve hesap makinesi, "
     "internetten sayfa çekebilen bir araç yok.",
     "ders saatlerini okulun sitesinden çekip takvimime ekle"),
    ("I can't reach the school website.", "add my school timetable to my calendar"),
    # positive access statements
    ("I can access your calendar without any additional permission.", "check my calendar"),
    ("Your calendar is available. I cannot verify the university website.", ""),
    ("Your calendar has no free slot on Friday.", "find time on friday"),
    ("Here is your study plan.", ""),
    # the end-date reply without a calendar request this turn
    ("I cannot verify the end dates.", "what is a list?"),
])
def test_does_not_flag_other_limits_or_positive_statements(text, request_text):
    assert not claims_no_calendar(text, request_text)


ORDINARY_TR = [
    "Bu hafta takviminde 3 kayıt var: Pazartesi spor, Çarşamba lab, Cuma sınav.",
    "Takviminde bu tarihlerde hiçbir kayıt yok.",
    "Takvimin bugün boş.",
    "Hangi güne eklememi istersin?",
    "Cuma günü 12:00-22:00 arası boş görünüyor.",
    "Vardiyan 14 Kasım 22:00'de başlıyor ve 15 Kasım 08:00'de bitiyor.",
    "Bu saatte başka bir etkinliğin var, yine de ekleyeyim mi?",
    "Takvim araçlarım hazır, ne eklememi istersin?",
    "Sinav 19 Kasimda, takvimde tum gun olarak duruyor.",
    "Takvimine erişimim var; şu an 2 kayıt görünüyor.",
    "Takvimde bu hafta çakışma yok.",
]


@pytest.mark.parametrize("text", ORDINARY_TR)
def test_ordinary_turkish_calendar_replies_do_not_trigger(text):
    assert not claims_no_calendar(text, CAL_REQUEST)
    assert not claims_calendar_change(text, CAL_REQUEST)


def test_detects_claimed_calendar_changes():
    assert claims_calendar_change("Takvime ekledim.")
    assert claims_calendar_change("Vardiyaları güncelledim.")
    assert claims_calendar_change("I added the exam to your calendar.")
    # a guessed write request is context for claims (not for denials)
    assert claims_calendar_change("Spor 18.00–20.30 olarak güncellendi.", "18.00-20.30 arasına koy")
    assert claims_calendar_change("Eklendi.", "yarın 18.00 spor ekle")
    assert not claims_calendar_change("Şifreni değiştirdim.", "Bugün şifremi değiştir")
    assert claims_calendar_change("Eklendi.", request="takvime ekle")
    assert claims_calendar_change("Takvime eklendi", request="")  # without Turkish characters
    assert not claims_calendar_change("Takvime eklemedim.")
    assert not claims_calendar_change("Takvime eklendiğinde hatırlatma gelir.")


@pytest.mark.parametrize("text, request_text", [
    ("I added the 10:00 example to the document.", "improve my notes"),
    ("I added a paragraph explaining the calendar integration.", "write my report"),
    ("Bölüm ekledim.", "şablonu düzenle"),
    ("Şablona yeni bir bölüm ekledim.", "takvim ödevim için şablon hazırla"),
    ("Spor 18.00 olarak güncellendi.", "what is a list?"),  # a clock alone is not context
])
def test_document_and_text_edits_are_not_calendar_changes(text, request_text):
    assert not claims_calendar_change(text, request_text)


def test_an_unverifiable_reply_to_a_calendar_request_is_retried_exactly_once(tmp_path):
    a, router, _, tools = make_calendar(tmp_path, ["I cannot verify the end dates."])
    reply = a.ask("Check the end dates of my calendar shifts.")
    assert len(router.calls) == 2
    assert reply.text == ("I cannot verify the end dates.\n\n"
                          "Note: no change was made to the calendar in this message.")


def test_a_change_reported_after_a_successful_write_is_accepted(tmp_path):
    a, router, _, tools = make_calendar(tmp_path, [
        'TOOL: agenda_add {"items": [{"title": "Spor"}]}', "Spor takvime eklendi."])
    reply = a.ask("yarın 18.00 spor ekle")
    assert reply.text == "Spor takvime eklendi." and len(router.calls) == 2


def test_inability_reply_is_retried_once_and_then_uses_the_tool(tmp_path):
    a, router, memory, tools = make_calendar(tmp_path, [
        "Takvime yazabilen bir araç bulunmadığı için ekleyemedim.",
        'TOOL: agenda_add {"items": [{"title": "Spor"}]}',
        "Spor yarın 18:00-20:30 olarak eklendi.",
    ])
    reply = a.ask("yarın 18.00-20.30 spor ekle")
    assert reply.text == "Spor yarın 18:00-20:30 olarak eklendi."
    assert reply.tool_calls == ("agenda_add",)
    assert reply.backends == ("fake1", "fake2", "fake3")
    retry_messages = router.calls[1][1]
    assert retry_messages[-2].role == "assistant" and "araç" in retry_messages[-2].content
    assert retry_messages[-1].role == "user" and "Igris note" in retry_messages[-1].content
    entries = memory.history()
    # the corrective note and the rejected reply are not stored
    assert [e["role"] for e in entries] == ["user", "tool", "assistant"]
    assert all("Igris note" not in e["content"] for e in entries)
    assert entries[-1]["backend"] == "fake3"
    assert "backend" not in entries[0] and "backend" not in entries[1]


def test_inability_twice_keeps_the_reply_and_adds_a_true_note(tmp_path):
    a, router, memory, tools = make_calendar(tmp_path, ["Takvimine erişemiyorum."])
    reply = a.ask("yarın spor ekle takvime")
    assert len(router.calls) == 2  # one retry, never a loop
    assert tools.ran == []
    assert reply.text == ("Takvimine erişemiyorum.\n\n"
                          "Not: bu mesajda takvimde hiçbir değişiklik yapılmadı.")
    assert memory.history()[-1]["content"] == reply.text
    en = a.ask("add gym to my calendar tomorrow")
    assert en.text.endswith("Note: no change was made to the calendar in this message.")


def test_a_claimed_change_without_a_write_is_retried_then_annotated(tmp_path):
    a, router, _, tools = make_calendar(tmp_path, ["Takvime ekledim."])
    reply = a.ask("cuma 10:00 çalışma ekle")
    assert len(router.calls) == 2
    assert reply.text.startswith("Takvime ekledim.")
    assert reply.text.endswith("Not: bu mesajda takvimde hiçbir değişiklik yapılmadı.")


def test_a_claimed_change_after_free_slots_only_is_caught(tmp_path):
    a, router, _, tools = make_calendar(tmp_path, [
        'TOOL: free_slots {"start_date": "2026-11-13"}',
        "Plan takvime eklendi.",
        'TOOL: agenda_add {"items": [{"title": "Spor"}]}',
        "Plan takvime eklendi.",
    ])
    reply = a.ask("planı takvime koy")
    assert [n for n, _ in tools.ran] == ["free_slots", "agenda_add"]
    assert reply.text == "Plan takvime eklendi."
    assert len(router.calls) == 4


def test_a_retry_backend_error_keeps_the_reply_with_the_note(tmp_path):
    class OnceRouter(ScriptedRouter):
        def complete(self, system, messages):
            if self.calls:
                self.calls.append((system, list(messages)))
                raise BackendError("down")
            return super().complete(system, messages)

    router = OnceRouter(["Takvimine erişemiyorum."])
    a = Assistant(Settings(), router, Memory(tmp_path / "m.jsonl"), CalendarTools())
    assert a.ask("takvime şimdi spor ekle").text == (
        "Takvimine erişemiyorum.\n\nNot: bu mesajda takvimde hiçbir değişiklik yapılmadı.")


def test_without_calendar_tools_replies_are_not_checked(tmp_path):
    assistant, router, _, _ = make(tmp_path, ["Takvimine erişemiyorum."])
    assert assistant.ask("takvime ekle").text == "Takvimine erişemiyorum."
    assert len(router.calls) == 1
    assert "agenda_update" not in assistant.system_prompt()


def test_prompt_says_the_calendar_tools_are_available(tmp_path):
    a, *_ = make_calendar(tmp_path, ["x"])
    prompt = a.system_prompt()
    assert "ARE connected" in prompt and "TOOL RESULT" in prompt
    assert "call free_slots for that period first" in prompt
    assert "Never say you have no calendar access" in prompt
    assert "agenda_update" in prompt


class PlanningRouter:
    """A fake model: asks for free slots, then puts one 2-hour block in each day's first slot."""

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        last = messages[-1]
        if last.role == "user":
            return ('TOOL: free_slots {"start_date": "2026-11-13", "end_date": "2026-11-15", '
                    '"minutes": 120}'), "fake"
        if last.content.startswith("free_slots ->"):
            items = []
            for day, begin in re.findall(r"(\d{4}-\d{2}-\d{2}) \w+: (\d{2}:\d{2})-",
                                         last.content):
                hour = int(begin[:2])
                items.append({"title": "Çalışma", "branch": "okul",
                              "start": f"{day}T{begin}", "due": f"{day}T{hour + 2:02d}:{begin[3:]}"})
            import json
            return "TOOL: agenda_add " + json.dumps({"items": items}), "fake"
        return "Çalışma blokları boş saatlere eklendi.", "fake"


def test_study_plan_uses_free_slots_first_and_blocks_fit_the_free_time(tmp_path):
    store = AgendaStore(tmp_path / "agenda.sqlite3")
    busy_events = [  # Google events the plan must avoid
        {"title": "Ders", "start": "2026-11-13T09:00:00+02:00", "end": "2026-11-13T12:00:00+02:00"},
        {"title": "Ders", "start": "2026-11-14T09:00:00+02:00", "end": "2026-11-14T15:30:00+02:00"},
    ]
    registry = ToolRegistry()
    register_agenda_tools(registry, store, OWNER, now=lambda: NOW,
                          busy=lambda lo, hi: {"status": "ok", "events": busy_events})
    store.upsert(OWNER, {"branch": "kisisel", "title": "Vardiya", "source": "manual",
                         "status": "added", "start": "2026-11-15T09:00+02:00",
                         "due": "2026-11-15T10:30+02:00"})
    router = PlanningRouter()
    a = Assistant(Settings(), router, Memory(tmp_path / "m.jsonl"), registry)
    reply = a.ask("19'undaki sınava kadar çalışma planı yap ve takvime koy")
    assert reply.tool_calls == ("free_slots", "agenda_add")
    assert reply.text == "Çalışma blokları boş saatlere eklendi."
    blocks = [i for i in store.list(OWNER) if i["title"] == "Çalışma"]
    assert [b["start"] for b in blocks] == ["2026-11-13T12:00+02:00", "2026-11-14T15:30+02:00",
                                            "2026-11-15T10:30+02:00"]
    taken = [(_dt.fromisoformat(e["start"]), _dt.fromisoformat(e["end"])) for e in busy_events]
    taken.append((_dt(2026, 11, 15, 9, 0, tzinfo=TZ), _dt(2026, 11, 15, 10, 30, tzinfo=TZ)))
    for block in blocks:
        begin, end = _dt.fromisoformat(block["start"]), _dt.fromisoformat(block["due"])
        assert begin.hour >= 9 and end.hour <= 22
        assert all(end <= lo or begin >= hi for lo, hi in taken)


def test_several_tool_calls_in_one_turn_are_capped(tmp_path):
    a, router, memory, tools = make_calendar(tmp_path, ['TOOL: agenda_list {}'])
    reply = a.ask("takvimimi şimdi kontrol et")
    assert len(tools.ran) == a.max_tool_steps == 8
    assert "sınırına (8)" in reply.text and "TOOL:" not in reply.text


# ---- review reproductions: a detector mistake costs one call and a true line ----

NOTE_EN = "Note: no change was made to the calendar in this message."
NOTE_TR = "Not: bu mesajda takvimde hiçbir değişiklik yapılmadı."


def test_1_a_clarifying_reply_is_kept_without_a_retry(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, ["I cannot add the event until you provide a date."])
    reply = a.ask("Add an event to my calendar.")
    assert reply.text == "I cannot add the event until you provide a date."
    assert len(router.calls) == 1
    b, router_b, _, _ = make_calendar(tmp_path / "b", ["Hangi saate eklememi istersin?"])
    assert b.ask("yarın spor ekle").text == "Hangi saate eklememi istersin?"
    assert len(router_b.calls) == 1


def test_2_a_document_edit_with_a_time_is_kept_without_a_note(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, ["I added the 10:00 example to the document."])
    reply = a.ask("Add the 10:00 example to the document today.")
    assert reply.text == "I added the 10:00 example to the document."
    assert len(router.calls) == 1


@pytest.mark.parametrize("request_text, claim, note", [
    ("Add a document review to my calendar tomorrow.",
     "I added the document review to your calendar.", NOTE_EN),
    ("Add the meeting tomorrow at 10:00.", "The meeting was added to your calendar.", NOTE_EN),
    ("Yarın 10:00 toplantı ekle.", "Toplantıyı takvimine kaydettim.", NOTE_TR),
])
def test_3_4_a_claim_without_a_write_is_retried_once_then_noted(tmp_path, request_text, claim,
                                                                note):
    a, router, memory, _ = make_calendar(tmp_path, [claim])
    reply = a.ask(request_text)
    assert len(router.calls) == 2
    assert reply.text == f"{claim}\n\n{note}"
    assert memory.history()[-1]["content"] == reply.text


@pytest.mark.parametrize("request_text, denial, note", [
    ("Add gym to my calendar tomorrow.", "I can't use the calendar tools.", NOTE_EN),
    ("Add gym to my calendar tomorrow.", "I don’t have access to your calendar.", NOTE_EN),
    ("Yarın spor ekle.", "Takvime erişemem.", NOTE_TR),
    ("Yarın spor ekle.", "Takvim erişimim yok.", NOTE_TR),
])
def test_5_a_denial_is_retried_once_then_noted(tmp_path, request_text, denial, note):
    a, router, _, _ = make_calendar(tmp_path, [denial])
    reply = a.ask(request_text)
    assert len(router.calls) == 2
    assert reply.text == f"{denial}\n\n{note}"


def test_5_a_denial_retry_that_uses_the_tool_is_accepted(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, [
        "I can't use the calendar tools.",
        'TOOL: agenda_add {"items": [{"title": "Gym"}]}',
        "Gym was added to your calendar."])
    reply = a.ask("Add gym to my calendar tomorrow.")
    assert reply.text == "Gym was added to your calendar." and len(router.calls) == 3


def test_6_a_denial_after_a_calendar_read_is_still_checked(tmp_path):
    a, router, _, tools = make_calendar(tmp_path, [
        "TOOL: agenda_list {}", "I don't have access to your calendar."])
    reply = a.ask("What is on my calendar tomorrow?")
    assert [n for n, _ in tools.ran] == ["agenda_list"]
    assert len(router.calls) == 3  # tool step, reply, one retry
    assert reply.text == f"I don't have access to your calendar.\n\n{NOTE_EN}"


def test_7_a_persisted_false_claim_is_kept_and_contradicted_once(tmp_path):
    a, router, memory, _ = make_calendar(tmp_path, ["Takvime ekledim."])
    reply = a.ask("cuma 10:00 çalışma ekle")
    assert len(router.calls) == 2
    assert reply.text == f"Takvime ekledim.\n\n{NOTE_TR}"
    assert reply.text.count(NOTE_TR) == 1
    assert memory.history()[-1]["content"] == reply.text


def test_a_plain_read_answer_to_a_write_request_gets_no_note(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, [
        "TOOL: agenda_list {}", "Yarın o saatte başka bir kaydın yok."])
    reply = a.ask("yarın 10:00 toplantı ekle")
    assert len(router.calls) == 3  # the write was asked for, so one retry
    assert reply.text == "Yarın o saatte başka bir kaydın yok."


def test_a_read_question_answered_plainly_is_not_retried(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, ["TOOL: agenda_list {}", "Yarın takvimin boş."])
    reply = a.ask("Yarın takvimimde ne var?")
    assert reply.text == "Yarın takvimin boş." and len(router.calls) == 2


# ---- whole-word matching of act and time words ------------------------------------

from igris.assistant import calendar_write_request  # noqa: E402


def test_an_exchange_rate_question_gets_no_retry_and_no_note(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, ["Today's exchange rate is 0.92 EUR per USD."])
    reply = a.ask("What is today's exchange rate?")
    assert reply.text == "Today's exchange rate is 0.92 EUR per USD."
    assert len(router.calls) == 1


@pytest.mark.parametrize("request_text", [
    "What is today's exchange rate?",
    "What's the weather today?",
    "bugün dolar kaç",
    "fosil yakıtlar nedir",
    "yarın hava nasıl",
    "silah kanunu ne zaman değişti",
    "send the file today",
    "How do I change my password today?",
    "Tekler maçı yarın oynanıyor",
    "Bugün ekler kısmına bak",
    "Silindir hacmi bugün sınavda çıkar mı",
    "Cumartesi maç kaçta?",
    "Update the document tomorrow",
    "Yarın köye gidiyorum",
])
def test_everyday_requests_are_not_calendar_writes(request_text):
    assert not calendar_write_request(request_text)


@pytest.mark.parametrize("request_text", [
    "yarın 18.00 spor ekle",
    "add gym tomorrow at 6",
    "cumartesi toplantıyı sil",
    "move my meeting to Friday",
    "Yarın 10:00 toplantıyı ekler misin?",
    "pazartesi dersi 14:00'e taşı",
    "Add a dentist appointment to my calendar.",
    "takvimden cuma vardiyasını kaldır",
])
def test_true_calendar_write_requests(request_text):
    assert calendar_write_request(request_text)


def test_a_denial_followed_by_a_question_is_retried_and_noted(tmp_path):
    # intended: the reply does deny calendar access, and the appended line is true
    denial = "I cannot access your calendar. What date and time should I use?"
    a, router, _, _ = make_calendar(tmp_path, [denial])
    reply = a.ask("Add gym to my calendar.")
    assert len(router.calls) == 2
    assert reply.text == f"{denial}\n\n{NOTE_EN}"


# ---- weak evidence (a guessed write request) never earns the note -----------------

@pytest.mark.parametrize("request_text, answer", [
    ("Change my password today", "You can change your password in Settings."),
    ("Bugün şifremi değiştir", "Şifreni ayarlardan değiştirebilirsin."),
])
def test_a_guessed_write_request_answered_in_prose_keeps_the_reply(tmp_path, request_text,
                                                                     answer):
    a, router, memory, _ = make_calendar(tmp_path, [answer])
    reply = a.ask(request_text)
    assert reply.text == answer
    # a password request names another object, so it is not even a guessed calendar write
    assert len(router.calls) == 1
    assert memory.history()[-1]["content"] == answer


def test_a_guessed_write_request_whose_retry_adds_is_accepted(tmp_path):
    a, router, _, tools = make_calendar(tmp_path, [
        "Tamam, yarın 18:00 spor güzel olur.",
        'TOOL: agenda_add {"items": [{"title": "Spor"}]}',
        "Spor yarın 18:00'e eklendi."])
    reply = a.ask("yarın 18.00 spor ekle")
    assert reply.text == "Spor yarın 18:00'e eklendi."
    assert [n for n, _ in tools.ran] == ["agenda_add"] and len(router.calls) == 3


def test_a_guessed_write_request_whose_retry_claims_a_change_gets_the_note(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, [
        "Tamam, yarın 18:00 spor güzel olur.", "Spor takvime eklendi."])
    reply = a.ask("yarın 18.00 spor ekle")
    assert len(router.calls) == 2
    assert reply.text == f"Spor takvime eklendi.\n\n{NOTE_TR}"


def test_a_request_naming_the_calendar_answered_in_prose_twice_gets_the_note(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, ["Spor yapmak sağlığın için çok iyi."])
    reply = a.ask("Takvimime yarın spor ekle")
    assert len(router.calls) == 2
    assert reply.text == f"Spor yapmak sağlığın için çok iyi.\n\n{NOTE_TR}"


# ---- backend failure inside the retry; weak context for denials -------------------

class FailAfter(ScriptedRouter):
    """Scripted replies; every call after the first ``ok`` ones raises BackendError."""

    def __init__(self, replies, ok):
        super().__init__(replies)
        self.ok = ok

    def complete(self, system, messages):
        if len(self.calls) >= self.ok:
            self.calls.append((system, list(messages)))
            raise BackendError("down")
        return super().complete(system, messages)


def test_a_backend_failure_after_a_retry_tool_call_falls_back_with_the_note(tmp_path):
    router = FailAfter(["I cannot access your calendar.", "TOOL: agenda_list {}"], ok=2)
    memory = Memory(tmp_path / "m.jsonl")
    tools = CalendarTools()
    a = Assistant(Settings(), router, memory, tools)
    reply = a.ask("Check my calendar")
    assert [n for n, _ in tools.ran] == ["agenda_list"]
    assert reply.text == f"I cannot access your calendar.\n\n{NOTE_EN}"
    assert [e["role"] for e in memory.history()] == ["user", "tool", "assistant"]


def test_a_backend_failure_after_a_retry_write_says_the_tool_ran(tmp_path):
    router = FailAfter(["Takvime erişemiyorum.",
                        'TOOL: agenda_add {"items": [{"title": "Spor"}]}'], ok=2)
    memory = Memory(tmp_path / "m.jsonl")
    a = Assistant(Settings(), router, memory, CalendarTools())
    reply = a.ask("Takvime yarın spor ekle")
    assert "hiçbir değişiklik" not in reply.text
    assert reply.text.startswith("Takvim aracı çalıştı")
    entries = memory.history()
    assert entries[1]["role"] == "tool" and "added id=" in entries[1]["content"]


def test_a_backend_failure_in_an_ordinary_tool_step_still_raises(tmp_path):
    router = FailAfter(["TOOL: agenda_list {}"], ok=1)
    a = Assistant(Settings(), router, Memory(tmp_path / "m.jsonl"), CalendarTools())
    with pytest.raises(BackendError):
        a.ask("Check my calendar")


def test_the_tool_cap_inside_the_retry_ends_with_the_limit_message(tmp_path):
    a, router, _, tools = make_calendar(tmp_path, ["I cannot access your calendar.",
                                                   "TOOL: agenda_list {}"])
    a.max_tool_steps = 2
    reply = a.ask("Check my calendar")
    assert len(tools.ran) == 2
    assert "limit (2)" in reply.text and "TOOL:" not in reply.text


def test_a_non_calendar_inability_on_a_guessed_request_is_kept(tmp_path):
    answer = "I cannot change your password. You can change it in Settings."
    a, router, _, _ = make_calendar(tmp_path, [answer])
    reply = a.ask("Change my password today")
    assert reply.text == answer
    assert not claims_no_calendar(answer, "Change my password today")


def test_real_case_2_the_correction_is_still_caught(tmp_path):
    denial = ("Haklısın; vardiyalar ertesi gün 08.00'de bitmeli. Önceki işlem kaydı bitiş "
              "tarihlerini göstermediğinden doğru kaydedildiğini doğrulayamıyorum. Bu oturumda "
              "bağlı takvim düzenleme aracı olmadığı için kayıtları düzeltemedim.")
    a, router, _, tools = make_calendar(tmp_path, [
        denial, "TOOL: agenda_list {}", "Kayıtlar doğru: her vardiya ertesi gün 08:00'de bitiyor."])
    reply = a.ask("yanlış eklemişsin cumartesi 22.00 pazar 08.00")
    assert len(router.calls) == 3  # first reply, the retry, the answer after agenda_list
    assert [n for n, _ in tools.ran] == ["agenda_list"]
    assert reply.text == "Kayıtlar doğru: her vardiya ertesi gün 08:00'de bitiyor."


# ---- claims on a guessed request: an unbacked "added" always gets the note ----------

@pytest.mark.parametrize("claim", ["Eklendi.", "Spor yarın 18.00 için eklendi."])
def test_an_unbacked_claim_to_a_guessed_request_gets_the_note(tmp_path, claim):
    a, router, memory, tools = make_calendar(tmp_path, [claim])
    reply = a.ask("yarın 18.00 spor ekle")
    assert tools.ran == [] and len(router.calls) == 2
    assert reply.text == f"{claim}\n\n{NOTE_TR}"
    assert memory.history()[-1]["content"] == reply.text


def test_a_guessed_request_whose_retry_adds_after_a_claim_is_accepted(tmp_path):
    a, router, _, tools = make_calendar(tmp_path, [
        "Eklendi.", 'TOOL: agenda_add {"items": [{"title": "Spor"}]}', "Eklendi."])
    reply = a.ask("yarın 18.00 spor ekle")
    assert reply.text == "Eklendi." and [n for n, _ in tools.ran] == ["agenda_add"]
    assert len(router.calls) == 3


def test_a_password_change_claim_gets_no_calendar_note(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, ["Şifreni değiştirdim."])
    reply = a.ask("Bugün şifremi değiştir")
    assert reply.text == "Şifreni değiştirdim."


def test_weak_evidence_alone_is_retried_but_never_noted(tmp_path):
    a, router, _, _ = make_calendar(tmp_path, ["Spor sağlık için çok iyidir."])
    reply = a.ask("yarın 18.00 spor ekle")
    assert len(router.calls) == 2  # one invisible retry
    assert reply.text == "Spor sağlık için çok iyidir."


def test_ask_records_owner_text_for_tools(tmp_path):
    from igris.tools import Tool, ToolRegistry

    registry = ToolRegistry()
    seen = []
    registry.add(Tool("probe", "probe()", lambda: seen.append(registry.turn_text()) or "ok"))
    assistant = Assistant(Settings(), ScriptedRouter(['TOOL: probe {}', "done"]),
                          Memory(tmp_path / "mem.jsonl"), registry)
    assistant.ask("save ece@example.lt")
    assert seen == ["save ece@example.lt"]


# ---- timetable import: a preview is a read, the import is a write ----------------------

def _import_assistant(tmp_path, replies):
    from igris.agenda_import import register_import_tools

    store = AgendaStore(tmp_path / "agenda.sqlite3")
    registry = ToolRegistry()
    register_agenda_tools(registry, store, OWNER, now=lambda: NOW)
    register_import_tools(registry, store, OWNER, now=lambda: NOW, zone=TZ)
    router = ScriptedRouter(replies)
    return Assistant(Settings(), router, Memory(tmp_path / "mem.jsonl"), registry), router, store


_ENTRIES = ('{"batch": "vu", "entries": [{"title": "Matematik", "start": "2026-11-16T10:00", '
            '"end": "2026-11-16T11:30", "weekly": {"until": "2026-12-07"}}]')


def test_timetable_import_previews_first_then_imports_after_confirmation(tmp_path):
    a, router, store = _import_assistant(tmp_path, [
        "TOOL: agenda_import " + _ENTRIES + "}",
        "Önizleme: 4 ders, çakışma yok. Takvime ekleyeyim mi?",
        "TOOL: agenda_import " + _ENTRIES + ', "preview": false}',
        "4 ders takvime eklendi.",
    ])
    first = a.ask("Ders programımı takvime ekle: Pazartesi 10:00-11:30 Matematik")
    assert first.text == "Önizleme: 4 ders, çakışma yok. Takvime ekleyeyim mi?"
    assert first.tool_calls == ("agenda_import",) and store.list(OWNER) == []
    assert "subgroup" in router.calls[0][0]  # the import rule is in the system prompt
    second = a.ask("Evet, ekle")
    assert second.text == "4 ders takvime eklendi." and second.tool_calls == ("agenda_import",)
    assert len(store.list(OWNER)) == 4 and len(router.calls) == 4  # no retry


def test_a_claim_after_only_a_preview_is_retried_and_noted(tmp_path):
    a, router, store = _import_assistant(tmp_path, [
        "TOOL: agenda_import " + _ENTRIES + "}",
        "4 ders takvime eklendi.",
        "4 ders takvime eklendi.",
    ])
    reply = a.ask("Ders programımı takvime ekle")
    assert len(router.calls) == 3 and store.list(OWNER) == []
    assert reply.text.endswith(NOTE_TR)


def test_import_results_are_classified():
    from igris.assistant import _calendar_result

    assert _calendar_result("agenda_import", "preview batch=vu (nothing was written to the calendar)") == "read"
    assert _calendar_result("agenda_import", "imported batch=vu: created 4") == "write"
    assert _calendar_result("agenda_import", "error: nothing was imported") is None
    assert _calendar_result("agenda_import_remove", "removed batch=vu: 4 item(s)") == "write"
