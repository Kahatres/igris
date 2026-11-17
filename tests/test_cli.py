import io
import os
from types import SimpleNamespace

from igris.cli import main


def test_setup_and_recovery_without_model_factory(tmp_path, monkeypatch):
    from igris.accounts import AccountStore

    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    answers = iter(["admin-test-password"] * 2 + ["guest-test-password"] * 2)
    output = io.StringIO()
    assert main(["--setup-users"], stdout=output, password_reader=lambda _: next(answers)) == 0
    store = AccountStore(tmp_path / "accounts.sqlite3")
    token = store.login("guest", "guest-test-password", "local")
    assert main(["--reset-password", "Guest"], stdout=output,
                password_reader=lambda _: "replacement-password") == 0
    assert store.session(token) is None
    assert "replacement-password" not in output.getvalue()


def test_setup_refuses_weak_password_before_confirmation(tmp_path, monkeypatch):
    from igris.accounts import AccountStore

    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    prompts = []

    def reader(prompt):
        prompts.append(prompt)
        return "short-pass1"  # 11 characters

    output = io.StringIO()
    assert main(["--setup-users"], stdout=output, password_reader=reader) == 1
    assert len(prompts) == 1 and "12" in output.getvalue()
    assert "short-pass1" not in output.getvalue()
    assert not AccountStore(tmp_path / "accounts.sqlite3").initialized()


def test_setup_mismatch_and_existing_accounts(tmp_path, monkeypatch):
    from igris.accounts import AccountStore

    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    answers = iter(["admin-test-password", "admin-test-passwore"])
    output = io.StringIO()
    assert main(["--setup-users"], stdout=output, password_reader=lambda _: next(answers)) == 1
    assert "do not match" in output.getvalue()
    store = AccountStore(tmp_path / "accounts.sqlite3")
    assert not store.initialized()
    store.bootstrap("admin-test-password", "guest-test-password")

    def never(prompt):
        raise AssertionError("must not prompt when accounts exist")

    assert main(["--setup-users"], stdout=output, password_reader=never) == 1
    assert "already exist" in output.getvalue()


def test_setup_passwords_are_never_cli_arguments_and_use_getpass(tmp_path, monkeypatch):
    import getpass
    import pytest

    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    for argv in (["--setup-users", "admin-test-password"],
                 ["--setup-users", "--password", "admin-test-password"]):
        with pytest.raises(SystemExit):
            main(argv, stdout=io.StringIO())
    answers = iter(["admin-test-password"] * 2 + ["guest-test-password"] * 2)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert main(["--setup-users"], stdout=io.StringIO()) == 0


class FakeAssistant:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.questions = []

    def ask(self, text, route=True):
        self.questions.append(text)
        self.routes = getattr(self, "routes", []) + [route]
        if text == self.fail_on:
            raise RuntimeError("boom")
        return SimpleNamespace(text=f"echo {text}", backends=("fake",))


def run(argv, stdin_text="", assistant=None):
    assistant = assistant or FakeAssistant()
    out = io.StringIO()
    code = main(argv, factory=lambda: assistant,
                stdin=io.StringIO(stdin_text), stdout=out)
    return code, out.getvalue(), assistant


def test_once():
    code, out, a = run(["--once", "hi"])
    assert code == 0
    assert "[fake] echo hi" in out
    assert a.questions == ["hi"]


def test_once_error():
    code, out, _ = run(["--once", "bad"], assistant=FakeAssistant(fail_on="bad"))
    assert code == 1
    assert "error: boom" in out


def test_version():
    code, out, _ = run(["--version"])
    assert code == 0
    assert "igris" in out


def test_repl_eof():
    code, out, _ = run([], "")
    assert code == 0
    assert "igris> " in out


def test_repl_exit_stops_reading():
    code, out, a = run([], "/exit\nhello\n")
    assert code == 0
    assert a.questions == []


def test_repl_question_and_empty_lines():
    code, out, a = run([], "\n   \nhello\n/exit\n")
    assert code == 0
    assert a.questions == ["hello"]
    assert "[fake] echo hello" in out


def test_repl_help():
    _, out, _ = run([], "/help\n")
    assert "/exit" in out and "/backend" in out


def test_repl_backend_command():
    _, out, _ = run([], "/backend\nhello\n/backend\n")
    assert "no answer yet" in out
    assert out.count("fake") >= 2


def test_repl_error_continues():
    a = FakeAssistant(fail_on="bad")
    code, out, a = run([], "bad\ngood\n", assistant=a)
    assert code == 0
    assert "error: boom" in out
    assert "[fake] echo good" in out


class FullAssistant(FakeAssistant):
    def __init__(self, sessions=None, fail=False):
        super().__init__()
        self.session = "default"
        self.persona = "plain"
        self.sessions = sessions if sessions is not None else []
        self.fail = fail

    def use_session(self, name):
        if self.fail:
            raise ValueError("bad session")
        self.session = name

    def session_name(self):
        if self.fail:
            raise ValueError("bad session")
        return self.session

    def list_sessions(self):
        if self.fail:
            raise ValueError("not enabled")
        return self.sessions

    def set_persona(self, name):
        if self.fail:
            raise ValueError("bad persona")
        self.persona = name

    def persona_name(self):
        return self.persona

    def list_personas(self):
        if self.fail:
            raise ValueError("no personas")
        return ["plain", "pirate"]


def test_session_show_and_switch():
    a = FullAssistant()
    _, out, _ = run([], "/session\n/session work\n/session\n", assistant=a)
    assert "default\n" in out
    assert "session: work" in out
    assert a.session == "work"


def test_sessions_list():
    a = FullAssistant(sessions=[{"name": "a", "messages": 2, "updated": "x"},
                                {"name": "b", "messages": 0, "updated": "y"}])
    a.session = "b"
    _, out, _ = run([], "/sessions\n", assistant=a)
    assert "  a (2 messages)" in out
    assert "* b (0 messages)" in out


def test_sessions_empty():
    _, out, _ = run([], "/sessions\n", assistant=FullAssistant())
    assert "no sessions yet" in out


def test_persona_show_switch_and_list():
    a = FullAssistant()
    _, out, _ = run([], "/persona\n/persona pirate\n/personas\n", assistant=a)
    assert "plain\n" in out
    assert "persona: pirate" in out
    assert "* pirate" in out
    assert "plain\n* pirate\n" in out


def test_repl_errors_continue():
    a = FullAssistant(fail=True)
    code, out, _ = run([], "/session\n/session x\n/sessions\n/persona x\n"
                           "/personas\nhello\n", assistant=a)
    assert code == 0
    assert out.count("error:") == 5
    assert "error: bad session" in out
    assert "error: not enabled" in out
    assert "error: bad persona" in out
    assert "error: no personas" in out
    assert "[fake] echo hello" in out


def test_unknown_command():
    code, out, a = run([], "/x\n/exit\n")
    assert code == 0
    assert "unknown command: /x (try /help)" in out
    assert a.questions == []


def test_help_lists_new_commands():
    _, out, _ = run([], "/help\n")
    for cmd in ("/session", "/sessions", "/persona", "/personas"):
        assert cmd in out


def test_options_apply_once():
    code, out, a = run(["--session", "s1", "--persona", "pirate", "--once", "hi"],
                       assistant=FullAssistant())
    assert code == 0
    assert a.session == "s1" and a.persona == "pirate"
    assert "[fake] echo hi" in out


def test_options_apply_repl():
    code, _, a = run(["--session", "s2", "--persona", "pirate"], "",
                     assistant=FullAssistant())
    assert code == 0
    assert a.session == "s2" and a.persona == "pirate"


def test_session_option_failure():
    code, out, a = run(["--session", "s", "--once", "hi"],
                       assistant=FullAssistant(fail=True))
    assert code == 1
    assert "error: bad session" in out
    assert a.questions == []


def test_persona_option_failure():
    a = FullAssistant()

    def boom(name):
        raise ValueError("nope")

    a.set_persona = boom
    code, out, _ = run(["--persona", "z"], "", assistant=a)
    assert code == 1
    assert "error: nope" in out


def _run_web(argv, assistant=None, server=None):
    assistant = assistant or FullAssistant()
    calls = []

    def fake_server(a, host, port):
        calls.append((a, host, port))
        if server:
            server()

    out = io.StringIO()
    code = main(argv, factory=lambda: assistant, stdin=io.StringIO(""),
                stdout=out, web_server=fake_server)
    return code, out.getvalue(), assistant, calls


def test_web_default_port():
    code, out, a, calls = _run_web(["--web"])
    assert code == 0
    assert "Igris web: http://127.0.0.1:8770" in out
    assert calls == [(a, "127.0.0.1", 8770)]


def test_web_custom_port_and_options_applied_first():
    seen = {}

    a = FullAssistant()

    def server():
        seen["state"] = (a.session, a.persona)

    code, out, _, calls = _run_web(
        ["--web", "--port", "9000", "--session", "s", "--persona", "pirate"],
        assistant=a, server=server)
    assert code == 0
    assert "Igris web: http://127.0.0.1:9000" in out
    assert calls[0][2] == 9000
    assert seen["state"] == ("s", "pirate")


def test_web_session_failure_does_not_start():
    code, out, _, calls = _run_web(["--web", "--session", "x"],
                                   assistant=FullAssistant(fail=True))
    assert code == 1
    assert calls == []


def test_web_keyboard_interrupt_is_clean():
    def server():
        raise KeyboardInterrupt

    code, _, _, _ = _run_web(["--web"], server=server)
    assert code == 0


def test_web_server_error():
    def server():
        raise OSError("address in use")

    code, out, _, _ = _run_web(["--web"], server=server)
    assert code == 1
    assert "error: address in use" in out


def test_backend_option_sets_env(monkeypatch):
    monkeypatch.delenv("IGRIS_BACKENDS", raising=False)
    seen = {}

    def factory():
        seen["env"] = os.environ.get("IGRIS_BACKENDS")
        return FakeAssistant()

    out = io.StringIO()
    code = main(["--backend", "claude", "--once", "x"], factory=factory,
                stdin=io.StringIO(""), stdout=out)
    assert code == 0
    assert seen["env"] == "claude"
    monkeypatch.delenv("IGRIS_BACKENDS", raising=False)


# ---- multiagent jobs in CLI mode ----

class FakeJobs:
    def __init__(self, finish_on_poll=None):
        self.polls = 0
        self.on_finish = None
        self.finish_on_poll = dict(finish_on_poll or {})

    def poll(self):
        self.polls += 1
        job = self.finish_on_poll.pop(self.polls, None)
        if job is not None and self.on_finish is not None:
            self.on_finish(job)


class JobsAssistant(FakeAssistant):
    def __init__(self, jobs):
        super().__init__()
        self.jobs = jobs
        self.finished = []

    def job_finished(self, job):
        self.finished.append(job)
        return f"multiagent job finished: {job}"


def test_once_never_routes_to_multiagent():
    jobs = FakeJobs()
    a = JobsAssistant(jobs)
    code, out, _ = run(["--once", "review the whole project"], assistant=a)
    assert code == 0 and a.routes == [False]
    assert jobs.polls == 0


def test_repl_polls_jobs_before_each_prompt_and_prints_finished():
    jobs = FakeJobs(finish_on_poll={2: "job-7"})
    a = JobsAssistant(jobs)
    jobs.on_finish = a.job_finished
    code, out, _ = run([], "hello\nagain\n", assistant=a)
    assert code == 0
    assert jobs.polls == 3  # one before each of the three prompts
    assert a.routes == [True, True]
    assert a.finished == ["job-7"]
    assert out.count("multiagent job finished: job-7") == 1
    # the notice appears after the first answer and before the second prompt's answer
    assert out.index("echo hello") < out.index("job-7") < out.index("echo again")


def test_repl_poll_errors_do_not_stop_the_loop():
    class Boom(FakeJobs):
        def poll(self):
            raise OSError("disk")

    a = JobsAssistant(Boom())
    code, out, _ = run([], "hello\n", assistant=a)
    assert code == 0 and "echo hello" in out


def test_repl_without_jobs_does_not_poll():
    code, out, a = run([], "hello\n")
    assert code == 0 and a.routes == [True]


def test_web_host_flag_is_used():
    code, out, _, calls = _run_web(["--web", "--host", "100.64.0.7"])

    assert code == 0
    assert calls[0][1] == "100.64.0.7"
    assert "Igris web: http://100.64.0.7:8770" in out


def test_web_host_comes_from_settings():
    assistant = FullAssistant()
    assistant.settings = SimpleNamespace(host="100.64.0.8")

    _, _, _, calls = _run_web(["--web"], assistant=assistant)

    assert calls[0][1] == "100.64.0.8"


def test_web_host_flag_beats_settings():
    assistant = FullAssistant()
    assistant.settings = SimpleNamespace(host="100.64.0.8")

    _, _, _, calls = _run_web(["--web", "--host", "100.64.0.7"], assistant=assistant)

    assert calls[0][1] == "100.64.0.7"


def test_setup_users_creates_the_configured_regular_user(tmp_path, monkeypatch):
    from igris.accounts import AccountStore

    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IGRIS_GUEST_USERNAME", "friend")
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    answers = iter(["admin-test-password"] * 2 + ["friend-test-password"] * 2)
    prompts = []

    def reader(prompt):
        prompts.append(prompt)
        return next(answers)

    output = io.StringIO()
    assert main(["--setup-users"], stdout=output, password_reader=reader) == 0
    assert "friend (user)" in output.getvalue() and "for friend" in prompts[2]
    store = AccountStore(tmp_path / "accounts.sqlite3")
    assert store.login("friend", "friend-test-password", "local")
