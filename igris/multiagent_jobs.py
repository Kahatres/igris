"""Multiagent job manager: launches `multiagent.py` as a subprocess with a queue, cancel and persisted state."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable, Literal, Mapping

from igris.llm import CLAUDE_PAID_ENV, CODEX_PAID_ENV, PaidUsageBlocked

MAX_GOAL_CHARS = 8000
MAX_TEST_CHARS = 500
# multiagent names same-second runs YYYYMMDD_HHMMSS_N (N up to 99)
_RUN_RE = re.compile(r"\d{8}_\d{6}(?:_\d{1,2})?")
EXCERPT_CHARS = 1500
LOG_TAIL_CHARS = 800

_STATUSES = ("queued", "running", "done", "failed", "cancelled")
Status = Literal["queued", "running", "done", "failed", "cancelled"]


@dataclass
class Job:
    id: str
    goal: str
    status: Status
    parallel: bool
    context: Path | None
    implement: bool
    test_cmd: str | None
    run_id: str | None
    submitted: float
    started: float | None = None
    ended: float | None = None
    exit_code: int | None = None
    owned: bool = True
    pid: int | None = None
    log: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["context"] = str(self.context) if self.context is not None else None
        return data


def _job_from_dict(data: dict) -> Job:
    names = {f.name for f in fields(Job)}
    kwargs = {k: v for k, v in data.items() if k in names}
    ctx = kwargs.get("context")
    kwargs["context"] = Path(ctx) if ctx else None
    return Job(**kwargs)


def _default_pid_alive(pid: int) -> bool:
    """Cross-platform liveness check using only the standard library."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_windows() -> bool:
    return os.name == "nt"


def _paid_vars(env: Mapping[str, str]) -> list[str]:
    names = CLAUDE_PAID_ENV + CODEX_PAID_ENV
    return [n for n in names if str(env.get(n, "")).strip().lower() not in ("", "0", "false")]


def _read_text(path: Path, *, head: int | None = None, tail: int | None = None) -> str | None:
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    if head is not None:
        text = text[:head]
    if tail is not None:
        text = text[-tail:]
    return text


def _run_end_error(events: Path) -> str | None:
    """The error of the run's failed run_end event, if any."""
    text = _read_text(events)
    if not text:
        return None
    for line in reversed(text.splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "run_end":
            err = ev.get("error")
            return err if ev.get("status") != "ok" and isinstance(err, str) and err else None
    return None


def default_python(root: Path) -> str:
    """multiagent's own virtualenv interpreter when it has one, else this interpreter."""
    for parts in (("Scripts", "python.exe"), ("bin", "python")):
        candidate = Path(root) / ".venv" / Path(*parts)
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def readiness(root: Path | None, workspace: Path | None = None, *,
              which: Callable[[str], str | None] | None = None) -> list[dict]:
    """What Igris needs to run multiagent jobs: one ``{id, ok, required, detail}`` per check.

    Jobs cannot start without the required checks. The others limit what runs: no Codex
    means no Astra review and Claude-only workers, no workspace means report jobs only.
    """
    if root is None:
        return [{"id": "dir", "ok": False, "required": True, "detail": "IGRIS_MULTIAGENT_DIR"}]
    root = Path(root)
    python = default_python(root)
    which = which or shutil.which

    def check(id_, ok, required, detail):
        return {"id": id_, "ok": bool(ok), "required": required, "detail": detail}

    return [
        check("dir", root.is_dir(), True, str(root)),
        check("script", (root / "multiagent.py").is_file(), True, "multiagent.py"),
        check("claude", which("claude") is not None, True, "claude"),
        check("codex", which("codex") is not None, False, "codex"),
        check("python", python != sys.executable, False, python),
        check("workspace", workspace is not None and Path(workspace).is_dir(), False,
              str(workspace) if workspace is not None else "IGRIS_WORKSPACE"),
    ]


class JobManager:
    """Runs multiagent goals as subprocesses; one at a time unless parallel is requested."""

    def __init__(self, root: Path, *, max_parallel: int = 1, popen=subprocess.Popen,
                 env: Mapping[str, str] | None = None, clock=time.time,
                 state_path: Path | None = None, python: str | None = None,
                 pid_alive: Callable[[int], bool] | None = None,
                 on_finish: Callable[[Job], None] | None = None,
                 kill_tree: Callable[[object], None] | None = None,
                 runner=subprocess.run,
                 secrets: Callable[[], Mapping[str, str]] | None = None,
                 notices=None,
                 notice_owner: str | Callable[[], str | None] | None = None) -> None:
        self.root = Path(root)
        self.max_parallel = max_parallel
        self._popen = popen
        self._env = os.environ if env is None else env
        self._clock = clock
        self._state_path = Path(state_path) if state_path is not None else None
        self._python = python or default_python(self.root)
        self._pid_alive = pid_alive or _default_pid_alive
        self.on_finish = on_finish
        self._kill_tree = kill_tree or self._default_kill_tree
        self._runner = runner
        self._secrets = secrets
        # admin panel notices (igris.notices.NoticeStore) for the owner; None writes none
        self.notices = notices
        self.notice_owner = notice_owner
        self._jobs: list[Job] = []
        self._procs: dict[str, object] = {}
        self._snapshots: dict[str, set[str]] = {}
        self._cancelling: set[str] = set()
        self._lock = threading.RLock()

    # -- public API ---------------------------------------------------------

    def submit(self, goal: str, *, context: Path | None = None, implement: bool = False,
               test_cmd: str | None = None, parallel: bool = False) -> Job:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("goal must not be empty")
        if len(goal) > MAX_GOAL_CHARS:
            raise ValueError(f"goal must be at most {MAX_GOAL_CHARS} characters")
        if context is not None:
            context = Path(context).resolve()
            if not context.is_dir():
                raise ValueError(f"context folder does not exist: {context}")
        if implement:
            # multiagent.py exits at once without these; fail here so the caller sees why
            test_cmd = (test_cmd or "").strip()
            if not test_cmd:
                raise ValueError("implement needs a test command")
            if len(test_cmd) > MAX_TEST_CHARS or any(ch in test_cmd for ch in "\r\n\0"):
                raise ValueError(f"test command must be one line of at most {MAX_TEST_CHARS} characters")
            if context is None:
                raise ValueError("implement needs a context folder (the repository to change)")
        else:
            test_cmd = None
        blocked = self._paid_problem()
        if blocked:
            raise PaidUsageBlocked(blocked)
        with self._lock:
            job = Job(id=uuid.uuid4().hex[:12], goal=goal, status="queued", parallel=parallel,
                      context=context, implement=implement, test_cmd=test_cmd or None,
                      run_id=None, submitted=self._clock())
            self._jobs.append(job)
            self._start_queued()
            self._save()
            return job

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs)

    def worker_models(self, timeout: float = 30) -> dict:
        """multiagent's worker chain as a run would see it (`saglayici_durum.py`).

        Runs with the same interpreter and keys a job gets, so a provider whose key is only
        in the keyring counts as active here too. No model is called. Raises RuntimeError."""
        if not (self.root / "saglayici_durum.py").is_file():
            raise RuntimeError("multiagent is too old: saglayici_durum.py is missing (git pull)")
        try:
            proc = self._runner([self._python, "saglayici_durum.py"], cwd=self.root,
                                env=self._child_env(), capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"saglayici_durum.py could not run: {exc}") from None
        out = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else proc.stdout or ""
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else proc.stderr or ""
            raise RuntimeError((err.strip() or out.strip() or f"exit code {proc.returncode}")[-300:])
        try:
            data = json.loads(out)
        except ValueError:
            raise RuntimeError("saglayici_durum.py did not print JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("isciler"), list):
            raise RuntimeError("saglayici_durum.py printed an unexpected shape")
        return data

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None:
                return False
            if job.status == "queued":
                job.status = "cancelled"
                job.ended = self._clock()
                self._save()
                self._notify(job)
                return True
            if not (job.status == "running" and job.owned and job.id in self._procs):
                return False
            # the job stays "running" (and keeps its slot) until the process is gone
            proc = self._procs.pop(job.id)
            self._cancelling.add(job.id)
        # killing the tree and waiting can take seconds: never hold the lock meanwhile
        try:
            self._kill_tree(proc)
        except Exception:
            pass
        self._reap(proc)
        with self._lock:
            self._cancelling.discard(job.id)
            job.status = "cancelled"
            job.ended = self._clock()
            self._start_queued()
            self._save()
            self._notify(job)
            return True

    def result(self, job: Job) -> dict:
        """What a finished job left behind: FINAL.md path and excerpt, run_end error, log tail."""
        out = {"final": None, "excerpt": None, "error": None, "log": job.log, "log_tail": None}
        if job.run_id and _RUN_RE.fullmatch(job.run_id):
            run = self.root / "runs" / job.run_id
            final = run / "FINAL.md"
            if final.is_file():
                out["final"] = str(final)
                out["excerpt"] = _read_text(final, head=EXCERPT_CHARS)
            out["error"] = _run_end_error(run / "events.jsonl")
        if job.log:
            out["log_tail"] = _read_text(Path(job.log), tail=LOG_TAIL_CHARS)
        return out

    def poll(self) -> None:
        with self._lock:
            self._claim_run_dirs()
            for job in self._jobs:
                if job.status != "running" or job.id in self._cancelling:
                    continue
                if job.owned and job.id in self._procs:
                    code = self._procs[job.id].poll()  # type: ignore[attr-defined]
                    if code is None:
                        continue
                    self._procs.pop(job.id, None)
                    job.exit_code = code
                    self._finish(job, "done" if code == 0 else "failed")
                elif not job.owned:
                    if job.pid is not None and self._pid_alive(job.pid):
                        continue
                    self._finish_orphan(job)
            self._start_queued()
            self._save()

    def reattach(self) -> None:
        with self._lock:
            self._jobs = self._load()
            self._procs.clear()
            for job in self._jobs:
                if job.status == "running":
                    job.owned = False
            self.poll()

    # -- internals ----------------------------------------------------------

    def _paid_problem(self) -> str | None:
        paid = _paid_vars(self._env)
        if paid and self._env.get("IGRIS_ALLOW_PAID") != "1":
            return (f"{', '.join(paid)} set: multiagent would bill an API account; "
                    "unset it, or set IGRIS_ALLOW_PAID=1 (and ALLOW_PAID=1 for multiagent)")
        return None

    def _child_env(self) -> dict[str, str]:
        """Igris' environment plus the stored free-provider keys it does not set itself.

        The keys live in the OS keyring; they go only to the multiagent process."""
        env = dict(self._env)
        try:
            stored = dict(self._secrets()) if self._secrets is not None else {}
        except Exception:  # a locked keyring must not stop the job; it runs with fewer workers
            stored = {}
        for name, value in stored.items():
            env.setdefault(name, value)
        return env

    def _default_kill_tree(self, proc) -> None:
        """Stop the process and its children (the worker CLIs it started)."""
        pid = getattr(proc, "pid", None)
        ok = False
        if pid is not None:
            if _is_windows():
                try:
                    res = self._runner(["taskkill", "/T", "/F", "/PID", str(pid)],
                                       capture_output=True, text=True)
                    ok = getattr(res, "returncode", 1) == 0
                except (OSError, subprocess.SubprocessError):
                    ok = False
            else:
                try:
                    os.killpg(pid, signal.SIGTERM)
                    ok = True
                except (OSError, AttributeError):
                    ok = False
        if not ok:
            try:
                proc.terminate()
            except OSError:
                pass

    @staticmethod
    def _reap(proc) -> None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def _notify(self, job: Job) -> None:
        self._notice(job)
        if self.on_finish is not None:
            try:
                self.on_finish(job)
            except Exception:
                pass

    def _notice(self, job: Job) -> None:
        """A finished or failed job becomes an admin panel notice; a cancel is the owner's own."""
        kind = {"done": "job_done", "failed": "job_failed"}.get(job.status)
        owner = self.notice_owner() if callable(self.notice_owner) else self.notice_owner
        if kind is None or self.notices is None or not owner:
            return
        goal = " ".join(job.goal.split())
        title = ("Multiagent işi bitti: " if kind == "job_done" else "Multiagent işi başarısız: ")
        try:
            self.notices.add(owner, kind, title + goal[:120], goal, source="multiagent",
                             ref=job.id, action={"job": job.id})
        except Exception:
            pass  # the job result stands; a lost notice only means the panel misses it

    def _finish(self, job: Job, status: Status) -> None:
        job.status = status
        job.ended = self._clock()
        self._notify(job)

    def _finish_orphan(self, job: Job) -> None:
        final = job.run_id is not None and (self.root / "runs" / job.run_id / "FINAL.md").is_file()
        self._finish(job, "done" if final else "failed")

    def _running_count(self) -> int:
        return sum(1 for j in self._jobs if j.status == "running")

    def _start_queued(self) -> None:
        for job in self._jobs:
            if job.status != "queued":
                continue
            if not job.parallel and self._running_count() >= self.max_parallel:
                continue
            self._spawn(job)

    def _run_names(self) -> set[str]:
        runs = self.root / "runs"
        try:
            return {p.name for p in runs.iterdir() if p.is_dir() and _RUN_RE.fullmatch(p.name)}
        except OSError:
            return set()

    def _argv(self, job: Job) -> list[str]:
        argv = [self._python, "multiagent.py"]
        if job.context is not None:
            argv.append(f"--context={job.context}")
        if job.implement:
            argv.append("--implement")
        if job.test_cmd:
            argv.append(f"--test={job.test_cmd}")
        return argv + ["--", job.goal]

    def _spawn(self, job: Job) -> None:
        if self._paid_problem():
            job.status = "failed"
            job.ended = self._clock()
            self._notify(job)
            return
        self._snapshots[job.id] = self._run_names()
        out = self._open_log(job)
        try:
            proc = self._popen(self._argv(job), cwd=self.root, env=self._child_env(),
                               stdin=subprocess.DEVNULL,
                               stdout=out if out is not None else subprocess.DEVNULL,
                               stderr=subprocess.STDOUT if out is not None else subprocess.DEVNULL,
                               shell=False, **self._group_kwargs())
        except OSError:
            job.status = "failed"
            job.ended = self._clock()
            self._notify(job)
            return
        finally:
            if out is not None:
                out.close()  # the child holds its own handle
        self._procs[job.id] = proc
        job.status = "running"
        job.started = self._clock()
        job.pid = getattr(proc, "pid", None)
        job.owned = True

    def _open_log(self, job: Job):
        """Open <state dir>/jobs/<job id>.log for the child's stdout and stderr."""
        if self._state_path is None:
            return None
        path = self._state_path.parent / "jobs" / f"{job.id}.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "wb")
        except OSError:
            return None
        job.log = str(path)
        return fh

    @staticmethod
    def _group_kwargs() -> dict:
        if _is_windows():
            return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        return {"start_new_session": True}

    def _claim_run_dirs(self) -> None:
        pending = [j for j in self._jobs
                   if j.status == "running" and j.run_id is None and j.id in self._snapshots]
        if not pending:
            return
        taken = {j.run_id for j in self._jobs if j.run_id}
        current = self._run_names()
        for job in sorted(pending, key=lambda j: j.started or 0):
            new = sorted(current - self._snapshots[job.id] - taken)
            if new:
                job.run_id = new[0]
                taken.add(new[0])

    def _save(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(self._state_path.name + ".tmp")
            tmp.write_text(json.dumps({"jobs": [j.to_dict() for j in self._jobs]}, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def _load(self) -> list[Job]:
        if self._state_path is None or not self._state_path.is_file():
            return []
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            raw = data["jobs"]
        except (OSError, ValueError, KeyError, TypeError):
            return []
        jobs: list[Job] = []
        if not isinstance(raw, list):
            return jobs
        for item in raw:
            try:
                job = _job_from_dict(item)
            except (TypeError, ValueError, AttributeError):
                continue
            if job.status in _STATUSES:
                jobs.append(job)
        return jobs
