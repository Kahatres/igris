"""Web routes for the owner's inbox, approvals and notices, and the mail wiring of the
server: outgoing mail behind approvals, follow-up reminders and the Gmail watcher.

Route functions take the request handler ``h`` and the server context ``ctx``.
Depends on approvals, people, outbox, followups, mail_send, gmail, inbox, mailtriage
and llm (imported where they are used).
"""

import sys
from pathlib import Path
from types import SimpleNamespace


def inbox(h, ctx, path):
    if ctx.inbox is None:
        return h._json(200, {"status": {"state": "disabled"}, "mails": []})
    return h._json(200, {"status": ctx.inbox.status(), "mails": ctx.inbox.store.recent(50)})


def approvals(h, ctx, path):
    if ctx.approvals is None:
        return h._error(404, "not found")
    return h._json(200, {"approvals": ctx.approvals.pending(h.login.user.id)})


def answer(h, ctx, path, data):
    if ctx.approvals is None:
        return h._error(404, "not found")
    from igris.approvals import ApprovalError

    request_id, approved = data.get("id"), data.get("approved")
    if not isinstance(request_id, str) or not isinstance(approved, bool):
        return h._error(400, "id must be a string and approved a boolean")
    try:
        ctx.approvals.answer(h.login.user.id, request_id, approved)
    except ApprovalError as exc:
        return h._error(400, str(exc))
    return h._json(200, {"ok": True})


def notices(h, ctx, path):
    store = ctx.notices
    if store is None:
        return h._json(200, {"notices": [], "unread": 0})
    owner = h._owner_id()
    unread_only = h._query().get("unread", [""])[0] in ("1", "true")
    return h._json(200, {"notices": store.list(owner, unread_only=unread_only),
                         "unread": store.unread_count(owner)})


def notices_read(h, ctx, path, data):
    store = ctx.notices
    if store is None:
        return h._error(404, "not found")
    owner = h._owner_id()
    if data.get("all") is True:
        return h._json(200, {"marked": store.mark_all_read(owner)})
    ids = data.get("ids")
    if (not isinstance(ids, list) or not ids or len(ids) > 500
            or not all(isinstance(i, str) for i in ids)):
        return h._error(400, "ids must be a non-empty list of notice ids, or all: true")
    return h._json(200, {"marked": store.mark_read(owner, ids)})


def wire_mail(settings, contexts, approvals, inbox, notifier, admin_id):
    """Outgoing mail for a server with accounts: the stores, the sender behind approvals,
    and the inbox's follow-ups. Returns followups, people, outbox and mail_sender."""
    from igris.followups import FollowupStore
    from igris.mail_send import MailSender
    from igris.outbox import OutboxStore
    from igris.people import PeopleStore

    people = PeopleStore(str(settings.data_dir / 'people.sqlite3'))
    outbox = OutboxStore(str(settings.data_dir / 'outbox.sqlite3'),
                         max_recipients=getattr(settings, 'mail_max_recipients', 5),
                         daily_limit=getattr(settings, 'mail_daily_limit', 20))
    followups = FollowupStore(settings.data_dir / 'followups.sqlite3')
    gmail_client = getattr(inbox, 'client_factory', lambda: None)
    mail_sender = MailSender(
        outbox=outbox, flow=approvals, notifier=notifier, followups=followups,
        gmail_client=gmail_client, school_address=getattr(settings, 'school_address', ''),
        followup_seconds=getattr(settings, 'mail_followup_days', 3) * 86400)
    contexts.people, contexts.outbox, contexts.mail_sender = people, outbox, mail_sender
    contexts.gmail_client = gmail_client
    if inbox is not None:
        inbox.followups, inbox.people = followups, people
        inbox.notice_owner = admin_id
        inbox.owner_addresses = (getattr(settings, 'school_address', ''),)
    existing_answer = getattr(approvals, 'on_answer', None)

    def on_answer(request_id, approved):
        owner = admin_id()
        try:
            if owner and mail_sender.handle_answer(owner, request_id, approved):
                return
        except Exception as exc:
            # The answer is stored; the next tick's resume() finishes it.
            # Never log the exception text: it could carry mail content.
            print(f'igris: mail approval answer failed: {type(exc).__name__}',
                  file=sys.stderr)
            return
        if existing_answer is not None:
            existing_answer(request_id, approved)

    approvals.on_answer = on_answer
    return SimpleNamespace(followups=followups, people=people, outbox=outbox,
                           mail_sender=mail_sender)


def remind_followups(followups, outbox, people, notifier, *, days=3) -> None:
    """Claim due timers once and offer a reminder draft; never create or send mail."""
    if notifier is None:
        return  # leave timers open until a channel is available
    from igris.mail_send import thread_key

    for timer in followups.due():
        try:
            drafts = outbox.awaiting_reply(timer['owner'])
            draft = next((draft for draft in drafts if thread_key(draft) == timer['thread_id']), None)
            addresses = draft.to if draft else ()
            if not addresses and timer['thread_id'].startswith('subj:'):
                addresses = (timer['thread_id'].rsplit('|', 1)[-1],)
            names = []
            for address in addresses:
                person = people.by_address(timer['owner'], address)
                names.append(person.name if person else address)
            name = ', '.join(names) or 'Alıcı'
            notifier.send(f"{name} {days} gündür cevap vermedi: {timer['note']}. "
                          "Hatırlatma taslağı hazırlayayım mı?")
        except Exception:
            # A timer is claimed once, even when delivery fails. Never log mail content.
            print('igris: mail follow-up notice failed', file=sys.stderr)


def build_inbox(settings, get_secret, notifier):
    """The inbox watcher for the owner's Gmail, reading the OAuth secrets from keyring.

    Without a stored sign-in the watcher reports ``not_configured`` and checks again
    on every poll, so ``tools/gmail_auth.py login`` takes effect without a restart.
    """
    from igris import gmail
    from igris.inbox import InboxStore, InboxWatcher, load_rules_file

    def client():
        values = [get_secret(name) for name in
                  ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")]
        return gmail.GmailClient(*values) if all(values) else None

    send = notifier.send if notifier is not None else None
    data_dir = Path(settings.data_dir)
    rules_path = data_dir / "mail_rules.json"
    try:
        rules = load_rules_file(rules_path)
    except (ValueError, OSError) as exc:  # a typo in the rules must not stop the server
        print(f"igris: {rules_path} ignored: {exc}", file=sys.stderr)
        from igris.mailtriage import TriageRules

        rules = TriageRules()
    return InboxWatcher(client, InboxStore(data_dir / "inbox.sqlite3"), send,
                        gmail.AuthAlarm(data_dir / "gmail_auth_down", send or (lambda text: None)),
                        rules=rules, digest_hour=settings.digest_hour,
                        digest=getattr(settings, "mail_digest", False),
                        school_domains=getattr(settings, "school_domains", ()),
                        owner_context=getattr(settings, "owner_context", ""),
                        model=mail_model(settings))


def mail_model(settings, *, backend_factory=None):
    """The model that reads mail the rules cannot sort: the owner's Claude subscription
    only (mail never goes to a provider that may train on it). None without Claude."""
    if "claude" not in tuple(getattr(settings, "backends", ())):
        return None
    if backend_factory is None:
        from igris.llm import ClaudeCLIBackend

        def backend_factory():
            return ClaudeCLIBackend(model=settings.claude_model, timeout=180)
    backend = backend_factory()

    def read(system: str, prompt: str) -> str:
        from igris.llm import Message

        return backend.complete(system, [Message("user", prompt)])
    return read


def watch_inbox(watcher, stop, interval) -> None:
    """Poll Gmail now and then every ``interval`` seconds until ``stop`` is set."""
    last = None
    while True:
        try:
            watcher.poll()
            problem = watcher.status().get("error")
        except Exception as exc:  # a bad mail or a locked file must not end the thread
            problem = f"{type(exc).__name__}: {exc}"
        if problem and problem != last:
            print(f"igris: inbox poll failed: {problem}", file=sys.stderr)
        last = problem
        if stop.wait(interval):
            return
