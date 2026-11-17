"""Command line interface for Igris: REPL and one-shot mode."""

import argparse
import dataclasses
import os
import sys

from igris import __version__ as VERSION
PROMPT = "igris> "

HELP_TEXT = (
    "Commands:\n"
    "  /help     show this help\n"
    "  /backend  show backends used for the last answer\n"
    "  /session  show the active session; /session NAME switches\n"
    "  /sessions list sessions\n"
    "  /persona  show the active persona; /persona NAME switches\n"
    "  /personas list personas\n"
    "  /exit     quit"
)


def _format_reply(reply) -> str:
    backends = ", ".join(reply.backends)
    return f"[{backends}] {reply.text}"


def _handle_slash(assistant, text, stdout) -> bool:
    """Handle session/persona/unknown slash commands; return True if consumed."""
    if not text.startswith("/"):
        return False
    parts = text.split(None, 1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "/backend":
        return False
    if cmd not in ("/session", "/sessions", "/persona", "/personas"):
        print(f"unknown command: {cmd} (try /help)", file=stdout)
        return True
    try:
        if cmd == "/session":
            if arg:
                assistant.use_session(arg)
                print(f"session: {arg}", file=stdout)
            else:
                print(assistant.session_name(), file=stdout)
        elif cmd == "/sessions":
            items = assistant.list_sessions()
            if not items:
                print("no sessions yet", file=stdout)
            else:
                active = assistant.session_name()
                for item in items:
                    mark = "* " if item["name"] == active else "  "
                    print(f"{mark}{item['name']} ({item['messages']} messages)",
                          file=stdout)
        elif cmd == "/persona":
            if arg:
                assistant.set_persona(arg)
                print(f"persona: {arg}", file=stdout)
            else:
                print(assistant.persona_name(), file=stdout)
        else:
            active = assistant.persona_name()
            for name in assistant.list_personas():
                mark = "* " if name == active else ""
                print(f"{mark}{name}", file=stdout)
    except Exception as exc:
        print(f"error: {exc}", file=stdout)
    return True


def _quota_left(backend):
    """Requests left today, or None when the backend has no daily quota."""
    own = getattr(backend, "quota_remaining", None)
    if callable(own):
        return own()
    quota = getattr(backend, "quota", None)
    if quota is not None:
        return quota.remaining(backend.name)
    return None


def _has_quota(backend) -> bool:
    """True when requests are limited: a counted daily quota, or OpenCode's free-tier limits."""
    return _quota_left(backend) is not None or getattr(backend, "name", "") == "opencode"


def _run_bench(args, stdout, bench_factory=None, stdin=None) -> int:
    """Manual benchmark: real provider calls, so it is never started by tests or the web UI."""
    from igris.config import load_settings
    from igris.llm import build_backend
    from igris.modelbench import (DOMAINS, EXEC_DISABLED, BenchStore, format_rank, load_cases,
                                  model_requests, run_benchmark)

    try:
        settings = load_settings()
        domains = [args.domain] if args.domain else list(DOMAINS)
        for domain in domains:
            if domain not in DOMAINS:
                raise ValueError(f"unknown domain {domain!r}; choose from {', '.join(DOMAINS)}")
        if args.model and not args.provider:
            raise ValueError("--model needs --provider")
        if args.model and args.provider != "opencode":
            raise ValueError("--model is only supported with --provider opencode")
        if args.model:
            settings = dataclasses.replace(settings, opencode_model=args.model)
        providers = [args.provider] if args.provider else list(settings.backends)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = BenchStore(settings.data_dir / "bench.sqlite3")
    except Exception as exc:
        print(f"error: {exc}", file=stdout)
        return 1

    def make(name):
        if bench_factory is not None:
            return bench_factory(name, settings)
        return build_backend(name, claude_model=settings.claude_model,
                             codex_model=settings.codex_model, timeout=settings.timeout,
                             settings=settings)

    status = 0
    for provider in providers:
        try:
            backend = make(provider)
        except Exception as exc:
            print(f"error: {provider}: {exc}", file=stdout)
            status = 1
            continue
        runnable = []
        left = _quota_left(backend)
        for domain in domains:
            cases = load_cases(domain)
            needed = model_requests(backend, cases)
            if left is not None and left < needed:
                print(f"skipped: {provider}/{domain} needs {needed} requests but the daily "
                      f"quota has {left} left", file=stdout)
                status = 1
                continue
            runnable.append((domain, cases, needed))
        if runnable and _has_quota(backend):
            total = sum(needed for _, _, needed in runnable)
            model = getattr(backend, "model", provider)
            print(f"{provider} ({model}) will send {total} requests.", file=stdout)
            if not args.yes:
                print("Continue? [y/N] ", end="", file=stdout, flush=True)
                if (stdin or sys.stdin).readline().strip().lower() not in ("y", "yes"):
                    print("aborted", file=stdout)
                    status = 1
                    continue
        for domain, cases, _ in runnable:
            results = run_benchmark(backend, cases)
            store.record(provider, getattr(backend, "model", provider), domain, results)
            scored = [r.score for r in results if r.score is not None]
            mean = sum(scored) / len(scored) if scored else 0.0
            disabled = sum(1 for r in results if r.error == EXEC_DISABLED)
            why = (f"; {disabled} code case(s) skipped: code execution is off "
                   "(set IGRIS_BENCH_EXEC=1 to run it unsandboxed)") if disabled else ""
            print(f"{provider}/{domain}: {len(scored)}/{len(results)} scored, mean {mean:.2f}"
                  f"{why}", file=stdout)
    for domain in domains:
        print(format_rank(domain, store.rank(domain)), file=stdout)
    return status


# Models the subscription CLIs can run besides the configured one. GPT-6.1 Sol's
# availability in Codex depends on the plan; it is listed so it can be ranked.
CLAUDE_MODELS = ("opus", "sonnet", "haiku", "claude-fable-5-1")
CODEX_MODELS = ("gpt-6-astra", "gpt-6.1-sol")


def _known_agents(settings, has_key) -> list[tuple[str, str]]:
    """Every (provider, model) Igris can use: subscription CLIs, Gemini, OpenCode's free
    models and the OpenAI-compatible providers whose key is stored."""
    from igris.llm_compat import PROVIDERS
    from igris.llm_opencode import FREE_MODELS

    agents = [("claude", settings.claude_model), ("codex", settings.codex_model)]
    agents += [("claude", m) for m in CLAUDE_MODELS] + [("codex", m) for m in CODEX_MODELS]
    if has_key("GEMINI_API_KEY"):
        agents.append(("gemini", settings.gemini_model))
    agents += [("opencode", model) for model in FREE_MODELS]
    for name, provider in PROVIDERS.items():
        model = settings.compat_models.get(name) or provider.default_model
        if model and has_key(provider.key_name):
            agents.append((name, model))
    return agents


_CHECK_HINTS = {
    "dir": "set IGRIS_MULTIAGENT_DIR to the multiagent folder",
    "script": "the folder has no multiagent.py",
    "claude": "install the Claude Code CLI and log in once (claude, then /login)",
    "codex": "optional: npm install -g @openai/codex, then codex login (Astra and GPT workers)",
    "python": "optional: create multiagent's .venv; Igris's own Python is used meanwhile",
    "workspace": "optional: set IGRIS_WORKSPACE to the repository jobs may change",
}


def _run_multiagent_check(stdout) -> int:
    """Print what multiagent jobs need; exit 1 when a required check fails."""
    from igris.config import load_settings
    from igris.multiagent_jobs import readiness

    settings = load_settings()
    checks = readiness(settings.multiagent_dir, settings.workspace)
    for c in checks:
        mark = "ok  " if c["ok"] else "MISS" if c["required"] else "warn"
        line = f"[{mark}] {c['id']:<9} {c['detail']}"
        if not c["ok"]:
            line += f"  ({_CHECK_HINTS[c['id']]})"
        print(line, file=stdout)
    ready = all(c["ok"] for c in checks if c["required"])
    print("multiagent is ready" if ready else "multiagent is not ready", file=stdout)
    return 0 if ready else 1


def _run_ranks(stdout, has_key=None) -> int:
    """Print S-E tiers per agent from own benchmark results and published scores."""
    from igris.config import load_settings
    from igris.modelbench import BenchStore
    from igris.ranks import RANK_DOMAINS, format_ranks, rank_agents

    if has_key is None:
        from igris.llm_openai import get_secret

        has_key = lambda name: bool(get_secret(name))
    try:
        settings = load_settings()
        agents = _known_agents(settings, has_key)
        bench_rows = {}
        path = settings.data_dir / "bench.sqlite3"
        if path.is_file():
            store = BenchStore(path)
            bench_rows = {d: store.rank(d) for d in RANK_DOMAINS if d != "research"}
            agents += [(row.provider, row.model) for rows in bench_rows.values() for row in rows]
        print(format_ranks(rank_agents(agents, bench_rows)), file=stdout)
    except Exception as exc:
        print(f"error: {exc}", file=stdout)
        return 1
    return 0


def main(argv=None, *, factory=None, stdin=sys.stdin, stdout=sys.stdout,
         web_server=None, password_reader=None, bench_factory=None, has_key=None) -> int:
    """Run the Igris CLI and return a process exit code."""
    parser = argparse.ArgumentParser(prog="igris", description="Igris AI assistant")
    parser.add_argument("--once", metavar="TEXT", help="answer one question and exit")
    parser.add_argument("--backend", metavar="NAME", help="backend to use")
    parser.add_argument("--session", metavar="NAME", help="session to use")
    parser.add_argument("--persona", metavar="NAME", help="persona to use")
    parser.add_argument("--web", action="store_true", help="start the web interface")
    parser.add_argument("--host", metavar="ADDRESS",
                        help="web interface address (default IGRIS_HOST, else 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8770, metavar="N",
                        help="web interface port (default 8770)")
    parser.add_argument("--version", action="store_true", help="print version")
    parser.add_argument("--bench", action="store_true",
                        help="manually benchmark backends per domain and print ranks")
    parser.add_argument("--domain", metavar="NAME", help="with --bench: one domain only")
    parser.add_argument("--provider", metavar="NAME", help="with --bench: one backend only")
    parser.add_argument("--model", metavar="NAME",
                        help="with --bench --provider opencode: the model to benchmark")
    parser.add_argument("--yes", action="store_true",
                        help="with --bench: skip the request-count confirmation")
    parser.add_argument("--ranks", action="store_true",
                        help="print S-E ranks and specialties for every known agent")
    parser.add_argument("--multiagent-check", action="store_true",
                        help="check that multiagent jobs can run from Igris")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--setup-users", action="store_true",
                          help="locally set passwords for admin and the regular user "
                               "(IGRIS_GUEST_USERNAME, default guest)")
    recovery.add_argument("--reset-password", metavar="USER",
                          help="locally reset a user's password and revoke their sessions")
    args = parser.parse_args(argv)

    if args.version:
        print(f"igris {VERSION}", file=stdout)
        return 0

    if args.setup_users or args.reset_password:
        from getpass import getpass
        from igris.accounts import AccountStore, validate_password
        from igris.config import load_settings

        read_password = password_reader or getpass

        def password_for(username):
            password = read_password(f"New password for {username} (12+ characters): ")
            # refuse a weak password before asking for the confirmation
            validate_password(password)
            if password != read_password("Confirm password: "):
                raise ValueError("Passwords do not match")
            return password

        try:
            settings = load_settings()
            accounts = AccountStore(settings.data_dir / "accounts.sqlite3")
            if args.setup_users:
                if accounts.initialized():
                    raise ValueError("Accounts already exist; use --reset-password USER")
                admin_password = password_for("admin")
                username = settings.guest_username
                user_password = password_for(username)
                accounts.bootstrap(admin_password, user_password, username)
                print(f"Created admin (administrator) and {username} (user).", file=stdout)
            else:
                accounts.reset_password(args.reset_password, password_for(args.reset_password))
                print("Password reset; previous login sessions revoked.", file=stdout)
        except (Exception, KeyboardInterrupt) as exc:
            print(f"error: {exc}", file=stdout)
            return 1
        return 0

    if args.bench:
        return _run_bench(args, stdout, bench_factory, stdin)

    if args.ranks:
        return _run_ranks(stdout, has_key)

    if args.multiagent_check:
        return _run_multiagent_check(stdout)

    if args.backend:
        os.environ["IGRIS_BACKENDS"] = args.backend

    if factory is None:
        from igris.assistant import build_assistant

        factory = build_assistant

    try:
        assistant = factory()
    except Exception as exc:
        print(f"error: {exc}", file=stdout)
        return 1

    try:
        if args.session:
            assistant.use_session(args.session)
        if args.persona:
            assistant.set_persona(args.persona)
    except Exception as exc:
        print(f"error: {exc}", file=stdout)
        return 1

    if args.web:
        if web_server is None:
            from igris.web import serve as web_server
        host = args.host or getattr(getattr(assistant, "settings", None), "host", "127.0.0.1")
        print(f"Igris web: http://{host}:{args.port}", file=stdout)
        stdout.flush()
        try:
            web_server(assistant, host, args.port)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"error: {exc}", file=stdout)
            return 1
        return 0

    if args.once is not None:
        try:
            # a one-shot process exits at once and could never watch a multiagent job
            reply = assistant.ask(args.once, route=False)
        except Exception as exc:
            print(f"error: {exc}", file=stdout)
            return 1
        print(_format_reply(reply), file=stdout)
        return 0

    last_backends = None
    jobs = getattr(assistant, "jobs", None)
    notices: list[str] = []
    if jobs is not None:
        finish = jobs.on_finish

        def on_finish(job):
            msg = finish(job) if finish is not None else None
            notices.append(msg if isinstance(msg, str) and msg else f"multiagent job {job}")

        jobs.on_finish = on_finish
    while True:
        if jobs is not None:
            # the REPL owns its jobs: check them before every prompt
            try:
                jobs.poll()
            except Exception as exc:
                print(f"error: job poll failed: {exc}", file=stdout)
            while notices:
                print(f"[multiagent] {notices.pop(0)}", file=stdout)
        stdout.write(PROMPT)
        stdout.flush()
        line = stdin.readline()
        if line == "":
            print(file=stdout)
            return 0
        text = line.strip()
        if not text:
            continue
        if text == "/exit":
            return 0
        if text == "/help":
            print(HELP_TEXT, file=stdout)
            continue
        if _handle_slash(assistant, text, stdout):
            continue
        if text == "/backend":
            if last_backends is None:
                print("no answer yet", file=stdout)
            else:
                print(", ".join(last_backends), file=stdout)
            continue
        try:
            reply = assistant.ask(text)
        except Exception as exc:
            print(f"error: {exc}", file=stdout)
            continue
        last_backends = tuple(reply.backends)
        print(_format_reply(reply), file=stdout)
