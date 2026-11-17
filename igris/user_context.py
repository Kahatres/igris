"""User-owned assistant instances and per-user request serialization."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import re
import threading

from igris.accounts import User
from igris.assistant import Assistant, build_assistant
from igris.attachments import AttachmentStore
from igris.config import USER_SAFE_BACKENDS


class _TaskMemory:
    """Ephemeral transcript; only the final task result is persisted by the queue."""

    def __init__(self, max_history):
        self.max_history = max_history
        self.entries = []

    def append(self, role, content, *, backend=None):
        self.entries.append({"role": role, "content": content})

    def history(self):
        return self.entries[-self.max_history:]


class UserContexts:
    """Keep the owner's existing data and give regular users separate data roots.

    Regular users only use text-generation backends. Host CLI agents and shared
    multiagent execution remain owner-only until process isolation is available.
    """

    def __init__(self, owner_assistant, *, factory=build_assistant):
        self.owner = owner_assistant
        self.factory = factory
        # set by web.make_server: the Görev Takvimi store for every user, and the owner's
        # Google Calendar reader (time_min, time_max) -> {"status", "events"} for the admin
        self.agenda = None
        self.busy = None
        # set by web.make_server: the owner's inbox store (igris.inbox.InboxStore) for the
        # read-only mail_recent tool; only the admin gets it
        self.mail = None
        self.people = self.outbox = self.mail_sender = None
        self.gmail_client = None
        self._items = {}
        self._guard = threading.Lock()
        settings = owner_assistant.settings
        # the owner's web_ara tool (igris.web_research.WebResearcher), None when it is off
        # or no research CLI is installed; owner-only like the host CLIs it runs
        from igris.web_research import build_web_researcher

        self.web_research = build_web_researcher(settings)
        # the doviz_kuru tool (igris.fx_rates.FxRates, one cache for everyone), None when off;
        # every user gets it: a public, keyless read of the central bank's daily bulletin
        from igris.fx_rates import FxRates

        self.fx = FxRates() if getattr(settings, "fx_rates", False) else None
        # the owner's sayfa_oku tool (igris.browser_read.BrowserReader): reads a page on an
        # allowed domain in the owner's Chrome; None when it is off or Claude CLI is missing
        from igris.browser_read import build_browser_reader

        self.browser_read = build_browser_reader(settings)
        from igris.core_store import CoreStore
        from igris.study_store import StudyStore
        from igris.vision_read import build_vision_reader

        self.core = CoreStore(settings.data_dir / "core.sqlite3") if settings.core else None
        self.study = StudyStore(settings.data_dir / "study.sqlite3") if settings.study else None
        self.vision = build_vision_reader(settings)
        # web.create_server binds the private model; tools call this dynamically.
        self.make_card = lambda text: ""
        mib = 1024 * 1024
        self.files = AttachmentStore(settings.data_dir / "files.sqlite3",
                                     max_file=getattr(settings, "file_max_mb", 25) * mib,
                                     max_total=getattr(settings, "file_quota_mb", 500) * mib,
                                     max_count=getattr(settings, "file_max_count", 500))

    def get(self, user: User):
        with self._guard:
            if user.id not in self._items:
                if user.role == "admin":
                    assistant = self.owner
                else:
                    if not re.fullmatch(r"[a-f0-9]{32}", user.id):
                        raise ValueError("Invalid user id")
                    settings = self.owner.settings
                    root = Path(settings.data_dir).resolve() / "users" / user.id
                    # Do not follow a pre-existing junction outside the data directory.
                    base = Path(settings.data_dir).resolve()
                    root = root.resolve()
                    if base not in root.parents:
                        raise ValueError("User directory escapes data directory")
                    workspace = root / "workspace"
                    for path in (workspace, root / "sessions", root / "personas"):
                        if root not in path.resolve().parents:
                            raise ValueError("User directory contains an external link")
                        path.mkdir(parents=True, exist_ok=True)
                    if set(settings.user_backends) - set(USER_SAFE_BACKENDS):
                        raise ValueError("Regular users cannot run host CLI backends")
                    isolated = replace(
                        settings, backends=settings.user_backends,
                        slow_backends=settings.user_backends,
                        memory_path=root / "memory.jsonl",
                        sessions_dir=root / "sessions", personas_dir=root / "personas",
                        workspace=workspace, multiagent_dir=None, jobs_path=root / "jobs.json",
                        budget_path=root / "budget.json",
                    )
                    assistant = self.factory(isolated)
                # Account-free HTTP serving uses "owner"; uploads require an account UUID.
                if user.id != "owner" and "list_uploads" not in assistant.tools.names():
                    self.files.register_tools(assistant.tools, user.id)
                if (user.role == "admin" and self.core is not None
                        and "core_search" not in assistant.tools.names()):
                    from igris.core_tools import register_core_tools
                    from igris.core_ingest import save_research

                    register_core_tools(assistant.tools, self.core, user.id,
                        save_research=lambda title, text, urls: save_research(
                            self.core, user.id, title, text, urls, make_card=self.make_card))
                if (user.role == "admin" and self.study is not None and self.agenda is not None
                        and "study_overview" not in assistant.tools.names()):
                    from igris.study_tools import register_study_tools

                    register_study_tools(assistant.tools, self.study, self.agenda, user.id,
                        now=lambda: datetime.now().astimezone(), zone=datetime.now().astimezone().tzinfo,
                        core=self.core)
                if self.agenda is not None and "agenda_add" not in assistant.tools.names():
                    from igris.agenda_import import register_import_tools
                    from igris.agenda_tools import register_agenda_tools

                    busy = self.busy if user.role == "admin" else None
                    register_agenda_tools(assistant.tools, self.agenda, user.id, busy=busy)
                    register_import_tools(assistant.tools, self.agenda, user.id, busy=busy,
                                          uploads=lambda file_id, owner=user.id:
                                          self.files.download(owner, file_id))
                if (user.role == "admin" and self.mail is not None
                        and "mail_recent" not in assistant.tools.names()):
                    from igris.mail_tools import register_mail_tools

                    register_mail_tools(assistant.tools, self.mail)
                if (user.role == "admin" and self.people is not None
                        and self.outbox is not None and self.mail_sender is not None
                        and "mail_draft" not in assistant.tools.names()):
                    from igris.outbox_tools import register_people_mail_tools

                    register_people_mail_tools(
                        assistant.tools, people=self.people, outbox=self.outbox,
                        sender=self.mail_sender, owner=user.id, inbox=self.mail,
                        gmail_client=self.gmail_client,
                        school_domains=getattr(assistant.settings, 'school_domains', ()))
                if (user.role == "admin" and self.web_research is not None
                        and "web_ara" not in assistant.tools.names()):
                    from igris.web_research import register_web_research

                    register_web_research(assistant.tools, self.web_research)
                if self.fx is not None and "doviz_kuru" not in assistant.tools.names():
                    from igris.fx_rates import register_fx_tools

                    register_fx_tools(assistant.tools, self.fx)
                if (user.role == "admin" and self.browser_read is not None
                        and "sayfa_oku" not in assistant.tools.names()):
                    from igris.browser_read import register_browser_read

                    register_browser_read(assistant.tools, self.browser_read)
                self._items[user.id] = (assistant, threading.RLock())
            return self._items[user.id]

    def run_task(self, user, task):
        assistant, lock = self.get(user)
        if task["owner"] != user.id or not re.fullmatch(r"[a-f0-9]{32}", task["id"]):
            raise ValueError("Invalid task owner or id")
        # Each task has its own transcript and never changes an active browser conversation.
        with lock:
            if task.get("is_cancelled", lambda: False)():
                return ""
            worker = Assistant(assistant.settings, assistant.slow_router,
                               _TaskMemory(assistant.settings.max_history), assistant.tools,
                               personas=assistant.personas, persona="default")
            return worker.ask(task["prompt"], route=False).text

    def ask_in_session(self, user, session, text, *, note="", route=True, max_tool_steps=None):
        """Answer ``text`` in the user's named session with their tools, backends and
        default persona, without changing the session their browser has selected.

        Used by the Telegram chat (session ``telegram``). The session file is created on
        the first message and then appears in the user's session list. ``note`` is added
        to the system prompt (how the channel shows the answer).
        """
        assistant, lock = self.get(user)
        with lock:
            if assistant.sessions is None:
                raise ValueError("Sessions are not enabled")
            persona = assistant.settings.persona
            if persona not in assistant.personas:
                persona = "default"
            worker = Assistant(assistant.settings, assistant.router,
                               assistant.sessions.open(session), assistant.tools,
                               personas=assistant.personas, persona=persona,
                               slow_router=assistant.slow_router, jobs=assistant.jobs)
            worker.extra_prompt = note
            options = {} if max_tool_steps is None else {"max_tool_steps": max_tool_steps}
            if not route:
                options["route"] = False
            return worker.ask(text, **options)
