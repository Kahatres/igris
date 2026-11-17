import json
from pathlib import Path

import pytest

from igris.llm import PaidUsageBlocked
from igris.multiagent_jobs import JobManager

CLEAN = {}


class FakeProc:
    def __init__(self, pid=100):
        self.pid = pid
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = -15


class Spawner:
    def __init__(self, root=None, run_names=()):
        self.calls = []
        self.procs = []
        self.root = root
        self.run_names = list(run_names)

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        proc = FakeProc(100 + len(self.procs))
        self.procs.append(proc)
        if self.root is not None and self.run_names:
            (self.root / "runs" / self.run_names.pop(0)).mkdir(parents=True)
        return proc


def make(tmp_path, spawner=None, **kw):
    spawner = spawner or Spawner()
    kw.setdefault("env", CLEAN)
    kw.setdefault("python", "py")
    return JobManager(tmp_path, popen=spawner, **kw), spawner


def test_submit_starts_process(tmp_path):
    m, sp = make(tmp_path)
    job = m.submit("do it")
    argv, kw = sp.calls[0]
    assert argv == ["py", "multiagent.py", "--", "do it"]
    assert kw["cwd"] == tmp_path
    assert not kw.get("shell")
    assert job.status == "running" and job.pid == 100 and job.owned


def test_provider_secrets_reach_only_the_child_env(tmp_path):
    env = {"PATH": "p", "GROQ_API_KEY": "from-env"}
    m, sp = make(tmp_path, env=env,
                 secrets=lambda: {"GROQ_API_KEY": "kr-groq", "GEMINI_API_KEY": "kr-gem"})
    m.submit("g")
    child = sp.calls[0][1]["env"]
    assert child == {"PATH": "p", "GROQ_API_KEY": "from-env", "GEMINI_API_KEY": "kr-gem"}
    assert env == {"PATH": "p", "GROQ_API_KEY": "from-env"}  # Igris' own env is untouched
    assert "kr-gem" not in json.dumps([j.to_dict() for j in m.jobs()])


def test_failing_secret_lookup_still_starts_the_job(tmp_path):
    def broken():
        raise RuntimeError("keyring locked")

    m, sp = make(tmp_path, secrets=broken)
    assert m.submit("g").status == "running"
    assert sp.calls[0][1]["env"] == {}


def test_goal_with_flag_text_is_after_separator(tmp_path):
    m, sp = make(tmp_path)
    m.submit("--implement fix")
    argv = sp.calls[0][0]
    assert argv[-2:] == ["--", "--implement fix"]
    assert argv.index("--") == len(argv) - 2


def test_context_flags(tmp_path):
    ctx = tmp_path / "repo"
    ctx.mkdir()
    m, sp = make(tmp_path)
    m.submit("g", context=ctx, implement=True, test_cmd="pytest -q")
    argv = sp.calls[0][0]
    sep = argv.index("--")
    head = argv[:sep]
    assert f"--context={ctx.resolve()}" in head
    assert "--implement" in head
    assert "--test=pytest -q" in head
    assert argv[sep + 1:] == ["g"]
    with pytest.raises(ValueError):
        m.submit("g", context=tmp_path / "nope")
    with pytest.raises(ValueError):
        m.submit("  ")
    with pytest.raises(ValueError):
        m.submit("x" * 8001)


def test_implement_requires_test_and_context(tmp_path):
    ctx = tmp_path / "repo"
    ctx.mkdir()
    m, sp = make(tmp_path)
    with pytest.raises(ValueError, match="test"):
        m.submit("g", context=ctx, implement=True)
    with pytest.raises(ValueError, match="test"):
        m.submit("g", context=ctx, implement=True, test_cmd="  ")
    with pytest.raises(ValueError, match="context"):
        m.submit("g", implement=True, test_cmd="pytest")
    with pytest.raises(ValueError):
        m.submit("g", context=ctx, implement=True, test_cmd="a\nb")
    with pytest.raises(ValueError):
        m.submit("g", context=ctx, implement=True, test_cmd="x" * 501)
    assert sp.calls == []


def test_test_cmd_dropped_without_implement(tmp_path):
    m, sp = make(tmp_path)
    job = m.submit("g", test_cmd="pytest")
    assert job.test_cmd is None
    assert not any(a.startswith("--test") for a in sp.calls[0][0])


def test_second_job_waits_when_max_parallel_1(tmp_path):
    done = []
    m, sp = make(tmp_path, on_finish=done.append)
    a = m.submit("a")
    b = m.submit("b")
    assert (a.status, b.status) == ("running", "queued")
    assert len(sp.calls) == 1
    sp.procs[0].code = 0
    m.poll()
    assert (a.status, b.status) == ("done", "running")
    assert [j.id for j in done] == [a.id]
    assert a.exit_code == 0


def test_parallel_flag_bypasses_queue(tmp_path):
    m, sp = make(tmp_path)
    a = m.submit("a")
    b = m.submit("b", parallel=True)
    assert a.status == "running" and b.status == "running"


def test_failed_exit_code(tmp_path):
    m, sp = make(tmp_path)
    a = m.submit("a")
    sp.procs[0].code = 1
    m.poll()
    assert a.status == "failed" and a.exit_code == 1


def test_cancel_running_and_queued(tmp_path):
    done = []
    m, sp = make(tmp_path, on_finish=done.append)
    a = m.submit("a")
    b = m.submit("b")
    assert m.cancel(b.id) is True
    assert b.status == "cancelled" and len(sp.calls) == 1
    assert m.cancel(a.id) is True
    assert sp.procs[0].terminated and a.status == "cancelled"
    assert a in done
    assert m.cancel("nope") is False


def test_paid_env_blocks_without_spawn(tmp_path):
    m, sp = make(tmp_path, env={"ANTHROPIC_API_KEY": "x"})
    with pytest.raises(PaidUsageBlocked):
        m.submit("a")
    assert sp.calls == [] and m.jobs() == []
    env = {"ANTHROPIC_API_KEY": "x", "IGRIS_ALLOW_PAID": "1"}
    m2, sp2 = make(tmp_path, env=env)
    m2.submit("a")
    assert sp2.calls[0][1]["env"] == env


def test_run_id_detection(tmp_path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "20200101_000000").mkdir()
    sp = Spawner(tmp_path, ["20261026_101010", "20261026_101011"])
    m, _ = make(tmp_path, sp)
    a = m.submit("a")
    b = m.submit("b", parallel=True)
    m.poll()
    assert a.run_id == "20261026_101010"
    assert b.run_id == "20261026_101011"


def test_persistence_and_reattach(tmp_path):
    state = tmp_path / "jobs.json"
    m, sp = make(tmp_path, state_path=state)
    a = m.submit("a")
    assert state.exists()

    m2, _ = make(tmp_path, state_path=state, pid_alive=lambda p: True)
    m2.reattach()
    [j] = m2.jobs()
    assert j.status == "running" and j.owned is False
    assert m2.cancel(j.id) is False

    (tmp_path / "runs" / "r1").mkdir(parents=True)
    (tmp_path / "runs" / "r1" / "FINAL.md").write_text("x")
    data = json.loads(state.read_text())
    data["jobs"][0]["run_id"] = "r1"
    state.write_text(json.dumps(data))
    m3, _ = make(tmp_path, state_path=state, pid_alive=lambda p: False)
    m3.reattach()
    assert m3.jobs()[0].status == "done"

    data["jobs"][0]["run_id"] = None
    data["jobs"][0]["status"] = "running"
    state.write_text(json.dumps(data))
    m4, _ = make(tmp_path, state_path=state, pid_alive=lambda p: False)
    m4.reattach()
    assert m4.jobs()[0].status == "failed"


def test_corrupt_state_file(tmp_path):
    state = tmp_path / "jobs.json"
    state.write_text("{not json")
    m, _ = make(tmp_path, state_path=state)
    m.reattach()
    assert m.jobs() == []


def test_max_parallel_attribute(tmp_path):
    m, _ = make(tmp_path, max_parallel=3)
    assert m.max_parallel == 3


def test_queued_job_blocked_on_reattach_when_paid_env(tmp_path):
    state = tmp_path / "jobs.json"
    m, _ = make(tmp_path, state_path=state)
    m.submit("a")
    m.submit("b")
    data = json.loads(state.read_text())
    data["jobs"][0]["status"] = "failed"
    state.write_text(json.dumps(data))
    done = []
    sp = Spawner()
    m2, _ = make(tmp_path, sp, state_path=state, env={"ANTHROPIC_API_KEY": "x"},
                 on_finish=done.append, pid_alive=lambda p: True)
    m2.reattach()
    assert sp.calls == []
    assert m2.jobs()[1].status == "failed"
    assert len(done) == 1


def test_cancel_uses_kill_tree(tmp_path):
    killed = []
    m, sp = make(tmp_path, kill_tree=killed.append)
    a = m.submit("a")
    m.cancel(a.id)
    assert killed == [sp.procs[0]]


def test_default_kill_tree_windows_taskkill(tmp_path, monkeypatch):
    import igris.multiagent_jobs as mj
    monkeypatch.setattr(mj, "_is_windows", lambda: True)
    calls = []

    class R:
        returncode = 0

    def runner(argv, **kw):
        calls.append((argv, kw))
        return R()

    m, sp = make(tmp_path, runner=runner)
    a = m.submit("a")
    assert "creationflags" in sp.calls[0][1]
    m.cancel(a.id)
    assert calls[0][0] == ["taskkill", "/T", "/F", "/PID", "100"]
    assert not calls[0][1].get("shell")
    assert not sp.procs[0].terminated


def test_default_kill_falls_back_to_terminate(tmp_path, monkeypatch):
    import igris.multiagent_jobs as mj
    monkeypatch.setattr(mj, "_is_windows", lambda: True)

    def runner(argv, **kw):
        raise OSError("no taskkill")

    m, sp = make(tmp_path, runner=runner)
    a = m.submit("a")
    m.cancel(a.id)
    assert sp.procs[0].terminated


def test_raising_on_finish_does_not_break_state(tmp_path):
    state = tmp_path / "jobs.json"

    def boom(job):
        raise RuntimeError("x")

    m, sp = make(tmp_path, state_path=state, on_finish=boom)
    a = m.submit("a")
    b = m.submit("b")
    sp.procs[0].code = 0
    m.poll()
    assert a.status == "done" and b.status == "running"
    saved = json.loads(state.read_text())["jobs"]
    assert [j["status"] for j in saved] == ["done", "running"]


def test_wrong_shape_state_file(tmp_path):
    state = tmp_path / "jobs.json"
    bad_status = ('{"jobs":[{"id":"a","goal":"g","status":"weird","parallel":false,'
                  '"context":null,"implement":false,"test_cmd":null,"run_id":null,"submitted":1}]}')
    for text in ('{"jobs":[1]}', '{"jobs":"x"}', '[]', bad_status):
        state.write_text(text)
        m, _ = make(tmp_path, state_path=state)
        m.reattach()
        assert m.jobs() == []


# ---- final review fixes ----

def test_same_second_run_dir_claimed(tmp_path):
    (tmp_path / "runs").mkdir()
    sp = Spawner(tmp_path, ["20261026_101010", "20261026_101010_1"])
    m, _ = make(tmp_path, sp)
    a = m.submit("a")
    b = m.submit("b", parallel=True)
    m.poll()
    assert (a.run_id, b.run_id) == ("20261026_101010", "20261026_101010_1")


def test_child_output_goes_to_per_job_log(tmp_path):
    import subprocess

    state = tmp_path / ".igris" / "jobs.json"
    m, sp = make(tmp_path, state_path=state)
    job = m.submit("a")
    kw = sp.calls[0][1]
    assert kw["stderr"] == subprocess.STDOUT
    assert kw["stdout"] not in (None, subprocess.DEVNULL)
    assert job.log == str(tmp_path / ".igris" / "jobs" / f"{job.id}.log")
    assert Path(job.log).is_file()
    saved = json.loads(state.read_text())["jobs"][0]
    assert saved["log"] == job.log


def test_no_state_path_means_no_log(tmp_path):
    import subprocess

    m, sp = make(tmp_path)
    job = m.submit("a")
    assert sp.calls[0][1]["stdout"] == subprocess.DEVNULL and job.log is None


def run_with(tmp_path, rid, events=None, final=None):
    d = tmp_path / "runs" / rid
    d.mkdir(parents=True)
    if events is not None:
        d.joinpath("events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    if final is not None:
        d.joinpath("FINAL.md").write_text(final, encoding="utf-8")
    return d


def test_result_done_has_final_excerpt(tmp_path):
    m, sp = make(tmp_path)
    job = m.submit("a")
    d = run_with(tmp_path, "20261026_101010", final="# Sonuç\n" + "x" * 3000)
    job.run_id = "20261026_101010"
    res = m.result(job)
    assert res["final"] == str(d / "FINAL.md")
    assert res["excerpt"].startswith("# Sonuç") and len(res["excerpt"]) <= 1500
    assert res["error"] is None


def test_result_failed_reads_run_end_error(tmp_path):
    m, _ = make(tmp_path)
    job = m.submit("a")
    run_with(tmp_path, "20261026_101010", events=[
        {"type": "run_start"}, {"type": "run_end", "status": "error",
                                "error": "RuntimeError: SPENDING BLOCKED"}])
    job.run_id = "20261026_101010"
    res = m.result(job)
    assert res["final"] is None and res["error"] == "RuntimeError: SPENDING BLOCKED"


def test_result_failed_falls_back_to_log_tail(tmp_path):
    state = tmp_path / ".igris" / "jobs.json"
    m, _ = make(tmp_path, state_path=state)
    job = m.submit("a")
    Path(job.log).write_bytes(b"start\n" + b"y" * 2000 + b"\nTraceback: boom\n\xff")
    res = m.result(job)
    assert res["error"] is None
    assert res["log_tail"].rstrip().endswith("Traceback: boom\n�".rstrip())
    assert len(res["log_tail"]) <= 800 and res["log"] == job.log


def test_result_without_run_or_log(tmp_path):
    m, _ = make(tmp_path)
    job = m.submit("a")
    assert m.result(job) == {"final": None, "excerpt": None, "error": None,
                             "log": None, "log_tail": None}


def test_cancel_kills_outside_the_lock(tmp_path):
    import threading

    seen = {}

    def kill(proc):
        # another thread must be able to use the manager while the kill runs
        t = threading.Thread(target=lambda: seen.setdefault("jobs", len(m.jobs())))
        t.start()
        t.join(2)
        seen["alive"] = t.is_alive()

    m, sp = make(tmp_path, kill_tree=kill)
    a = m.submit("a")
    b = m.submit("b")
    assert m.cancel(a.id) is True
    assert seen == {"jobs": 2, "alive": False}
    assert a.status == "cancelled" and b.status == "running"


def test_job_being_cancelled_is_not_double_cancelled(tmp_path):
    results = []

    def kill(proc):
        results.append(m.cancel(a.id))

    m, sp = make(tmp_path, kill_tree=kill)
    a = m.submit("a")
    assert m.cancel(a.id) is True
    assert results == [False] and a.status == "cancelled"


def test_default_python_prefers_multiagent_venv(tmp_path):
    import sys

    from igris.multiagent_jobs import default_python

    assert default_python(tmp_path) == sys.executable
    venv = tmp_path / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_text("")
    assert default_python(tmp_path) == str(venv / "python.exe")


def test_readiness_reports_required_and_optional_checks(tmp_path):
    from igris.multiagent_jobs import readiness

    (tmp_path / "multiagent.py").write_text("")
    checks = {c["id"]: c for c in readiness(tmp_path, None,
                                            which=lambda name: "x" if name == "claude" else None)}
    assert checks["dir"]["ok"] and checks["script"]["ok"] and checks["claude"]["ok"]
    assert not checks["codex"]["ok"] and not checks["codex"]["required"]
    assert not checks["workspace"]["ok"] and checks["workspace"]["detail"] == "IGRIS_WORKSPACE"
    assert readiness(None) == [{"id": "dir", "ok": False, "required": True,
                                "detail": "IGRIS_MULTIAGENT_DIR"}]


class Done:
    def __init__(self, code=0, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_worker_models_runs_status_script_with_job_env_and_keys(tmp_path):
    (tmp_path / "saglayici_durum.py").write_text("", encoding="utf-8")
    calls = []
    status = {"zincir": ["evren-glm"], "isciler": [{"ad": "evren-glm", "durum": "aktif"}]}

    def runner(argv, **kw):
        calls.append((argv, kw))
        return Done(out=json.dumps(status).encode("utf-8"))

    m, _ = make(tmp_path, runner=runner, env={"PATH": "x"},
                secrets=lambda: {"EVREN_API_KEY": "secret"})
    assert m.worker_models() == status
    argv, kw = calls[0]
    assert argv == ["py", "saglayici_durum.py"] and kw["cwd"] == tmp_path
    assert kw["env"]["EVREN_API_KEY"] == "secret" and kw["env"]["PATH"] == "x"
    assert not kw.get("shell")


@pytest.mark.parametrize("done, message", [
    (Done(code=1, err=b"Traceback: boom"), "boom"),
    (Done(out=b"not json"), "JSON"),
    (Done(out=b"[]"), "unexpected"),
])
def test_worker_models_reports_bad_output(tmp_path, done, message):
    (tmp_path / "saglayici_durum.py").write_text("", encoding="utf-8")
    m, _ = make(tmp_path, runner=lambda argv, **kw: done)
    with pytest.raises(RuntimeError, match=message):
        m.worker_models()


def test_worker_models_needs_new_multiagent(tmp_path):
    m, _ = make(tmp_path, runner=lambda argv, **kw: pytest.fail("must not run"))
    with pytest.raises(RuntimeError, match="git pull"):
        m.worker_models()


def test_finished_and_failed_jobs_become_panel_notices_but_cancels_do_not(tmp_path):
    from igris.notices import NoticeStore

    notices = NoticeStore(tmp_path / "n.sqlite3")
    m, sp = make(tmp_path, max_parallel=3, notices=notices, notice_owner="owner-id")
    ok, bad, gone = m.submit("build the\npanel"), m.submit("b"), m.submit("c")
    sp.procs[0].code, sp.procs[1].code = 0, 1
    m.cancel(gone.id)
    m.poll()
    m.poll()
    by_ref = {n["ref"]: n for n in notices.list("owner-id")}
    assert set(by_ref) == {ok.id, bad.id}
    assert by_ref[ok.id]["kind"] == "job_done" and by_ref[ok.id]["title"].endswith("build the panel")
    assert by_ref[bad.id]["kind"] == "job_failed" and by_ref[bad.id]["action"] == {"job": bad.id}


def test_a_broken_notice_store_does_not_break_the_job_state(tmp_path):
    class Broken:
        def add(self, *a, **k):
            raise OSError("disk full")

    done = []
    m, sp = make(tmp_path, notices=Broken(), notice_owner=lambda: "o", on_finish=done.append)
    job = m.submit("a")
    sp.procs[0].code = 0
    m.poll()
    assert job.status == "done" and done == [job]
