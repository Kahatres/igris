"""Durable outgoing mail drafts and their approval state."""

from dataclasses import dataclass
import json
import sqlite3
import time
import uuid
from typing import Callable


class OutboxError(ValueError):
    """An outbox operation is invalid."""


@dataclass(frozen=True)
class Draft:
    id: str
    owner: str
    identity: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: str
    status: str
    revision: int
    approval_id: str | None
    in_reply_to: str | None
    references: str | None
    thread_id: str | None
    sent_message_id: str | None
    sent_thread_id: str | None
    error: str
    created_at: float
    sent_at: float | None


def payload(draft: Draft) -> dict:
    return {'draft_id': draft.id, 'identity': draft.identity, 'to': list(draft.to),
            'cc': list(draft.cc), 'subject': draft.subject, 'body': draft.body}


def _local_midnight(now: float) -> float:
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, -1, -1, -1))


class OutboxStore:
    def __init__(self, path: str, *, clock=time.time, max_recipients: int = 5,
                 daily_limit: int = 20, day_start: Callable[[float], float] | None = None):
        self.path = path
        self.clock = clock
        self.max_recipients = max_recipients
        self.daily_limit = daily_limit
        self.day_start = day_start or _local_midnight
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, identity TEXT NOT NULL,
                recipients TEXT NOT NULL, cc TEXT NOT NULL, subject TEXT NOT NULL,
                body TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
                approval_id TEXT, in_reply_to TEXT, refs TEXT, thread_id TEXT,
                sent_message_id TEXT, sent_thread_id TEXT, error TEXT NOT NULL,
                created_at REAL NOT NULL, sent_at REAL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS mail_send_requests (
                request_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                draft_id TEXT NOT NULL, revision INTEGER NOT NULL,
                action TEXT NOT NULL, send_request_id TEXT NOT NULL)''')

    def link_request(self, owner, draft_id, revision, request_id, action, send_request_id):
        """Keep approval associations after a revision or restart, without storing bodies."""
        if action not in ('mail.send', 'mail.confirm_sent') or not request_id or not send_request_id:
            raise OutboxError('Invalid approval association')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM outbox WHERE owner=? AND id=?',
                             (owner, draft_id)).fetchone()
            if row is None or row['revision'] != revision:
                raise OutboxError('Invalid draft revision')
            try:
                db.execute('INSERT INTO mail_send_requests VALUES (?, ?, ?, ?, ?, ?)',
                           (request_id, owner, draft_id, revision, action, send_request_id))
            except sqlite3.IntegrityError:
                raise OutboxError('Approval association already exists') from None

    def request_link(self, owner, request_id):
        """Return (draft id, revision, action, send request id), scoped to the owner."""
        with self._connect() as db:
            row = db.execute('SELECT draft_id, revision, action, send_request_id '
                             'FROM mail_send_requests WHERE owner=? AND request_id=?',
                             (owner, request_id)).fetchone()
            return tuple(row) if row is not None else None

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _draft(row):
        if row is None:
            return None
        return Draft(row['id'], row['owner'], row['identity'], tuple(json.loads(row['recipients'])),
                     tuple(json.loads(row['cc'])), row['subject'], row['body'], row['status'],
                     row['revision'], row['approval_id'], row['in_reply_to'], row['refs'],
                     row['thread_id'], row['sent_message_id'], row['sent_thread_id'],
                     row['error'], row['created_at'], row['sent_at'])

    def _validate(self, identity, to, cc, subject, body):
        if (identity not in ('personal', 'school') or not to or
                len(to) + len(cc) > self.max_recipients or
                any(not isinstance(address, str) or not address.strip() for address in (*to, *cc)) or
                not isinstance(subject, str) or not subject.strip() or
                not isinstance(body, str) or not body.strip()):
            raise OutboxError('Invalid draft fields')

    def create(self, owner, *, identity, to, cc=(), subject, body, in_reply_to=None,
               references=None, thread_id=None):
        to, cc = tuple(to), tuple(cc)
        self._validate(identity, to, cc, subject, body)
        draft_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (draft_id, owner, identity, json.dumps(to), json.dumps(cc), subject, body,
                        'draft', 1, None, in_reply_to, references, thread_id, None, None, '',
                        self.clock(), None))
        return self.get(owner, draft_id)

    def _change(self, owner, draft_id, statuses, changes, *, approval_id=None, check_limit=False):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM outbox WHERE owner=? AND id=?', (owner, draft_id)).fetchone()
            if row is None or row['status'] not in statuses or (approval_id is not None and row['approval_id'] != approval_id):
                raise OutboxError('Invalid outbox state or approval')
            if check_limit:
                now = self.clock()
                count = db.execute('SELECT COUNT(*) FROM outbox WHERE owner=? AND status=? AND sent_at>=? AND sent_at<=?',
                                   (owner, 'sent', self.day_start(now), now)).fetchone()[0]
                if count >= self.daily_limit:
                    raise OutboxError('Daily send limit reached')
            updates = changes(self._draft(row)) if callable(changes) else changes
            assignments = ', '.join(key + '=?' for key in updates)
            result = db.execute(f'UPDATE outbox SET {assignments} WHERE owner=? AND id=? AND status=?',
                                (*updates.values(), owner, draft_id, row['status']))
            if result.rowcount != 1:
                raise OutboxError('Invalid outbox state')
            return self._draft(db.execute('SELECT * FROM outbox WHERE owner=? AND id=?',
                                          (owner, draft_id)).fetchone())

    def revise(self, owner, draft_id, *, subject=None, body=None, to=None, cc=None, identity=None):
        def changes(draft):
            new_to = draft.to if to is None else tuple(to)
            new_cc = draft.cc if cc is None else tuple(cc)
            new_identity = draft.identity if identity is None else identity
            new_subject = draft.subject if subject is None else subject
            new_body = draft.body if body is None else body
            self._validate(new_identity, new_to, new_cc, new_subject, new_body)
            return {'identity': new_identity, 'recipients': json.dumps(new_to), 'cc': json.dumps(new_cc),
                    'subject': new_subject, 'body': new_body, 'status': 'draft',
                    'approval_id': None, 'revision': draft.revision + 1}
        return self._change(owner, draft_id, ('draft', 'awaiting'), changes)

    def mark_awaiting(self, owner, draft_id, approval_id):
        if not isinstance(approval_id, str) or not approval_id:
            raise OutboxError('Invalid approval')
        return self._change(owner, draft_id, ('draft',), {'status': 'awaiting', 'approval_id': approval_id})

    def begin_send(self, owner, draft_id, approval_id):
        if not isinstance(approval_id, str) or not approval_id:
            raise OutboxError('Invalid approval')
        return self._change(owner, draft_id, ('awaiting',), {'status': 'sending'},
                            approval_id=approval_id, check_limit=True)

    def mark_sent(self, owner, draft_id, *, message_id=None, thread_id=None):
        return self._change(owner, draft_id, ('sending', 'handed'),
                            {'status': 'sent', 'sent_message_id': message_id,
                             'sent_thread_id': thread_id, 'sent_at': self.clock()})

    def mark_handed(self, owner, draft_id):
        return self._change(owner, draft_id, ('sending',), {'status': 'handed'})

    def mark_failed(self, owner, draft_id, error):
        return self._change(owner, draft_id, ('sending', 'handed'), {'status': 'failed', 'error': error})

    def mark_uncertain(self, owner, draft_id, error):
        """Gmail may have delivered the mail; lock the draft so it can never be resent."""
        return self._change(owner, draft_id, ('sending',), {'status': 'uncertain', 'error': error})

    def expire_approval(self, owner, draft_id, approval_id):
        """An approval request ran out unanswered or unspent; reopen as a new revision."""
        if not approval_id:
            raise OutboxError('Invalid approval')
        return self._change(owner, draft_id, ('awaiting',),
                            lambda draft: {'status': 'draft', 'approval_id': None,
                                           'revision': draft.revision + 1},
                            approval_id=approval_id)

    def open_requests(self, owner):
        """Pending approval links of drafts still waiting on an answer, newest first.

        Returns (draft, request id, action) for each awaiting draft's send request and
        for each confirmation request of a draft handed to Outlook.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT o.*, m.request_id AS link_id, m.action AS link_action "
                "FROM outbox o JOIN mail_send_requests m "
                "ON m.owner=o.owner AND m.draft_id=o.id AND m.revision=o.revision "
                "AND m.send_request_id=o.approval_id "
                "WHERE o.owner=? AND ((o.status='awaiting' AND m.action='mail.send') "
                "OR (o.status='handed' AND m.action='mail.confirm_sent')) "
                "ORDER BY o.created_at DESC, m.rowid DESC", (owner,)).fetchall()
            return [(self._draft(row), row['link_id'], row['link_action']) for row in rows]

    def return_handed(self, owner, draft_id, approval_id):
        """The owner denied sending from Outlook; invalidate the old confirmation."""
        if not approval_id:
            raise OutboxError('Invalid approval')
        return self._change(owner, draft_id, ('handed',),
                            lambda draft: {'status': 'draft', 'approval_id': None,
                                           'error': '', 'revision': draft.revision + 1},
                            approval_id=approval_id)

    def reopen(self, owner, draft_id):
        return self._change(owner, draft_id, ('failed',),
                            {'status': 'draft', 'approval_id': None, 'error': ''})

    def cancel(self, owner, draft_id):
        return self._change(owner, draft_id, ('draft', 'awaiting', 'handed'), {'status': 'cancelled'})

    def get(self, owner, draft_id):
        with self._connect() as db:
            return self._draft(db.execute('SELECT * FROM outbox WHERE owner=? AND id=?', (owner, draft_id)).fetchone())

    def newest_open(self, owner):
        with self._connect() as db:
            return self._draft(db.execute("SELECT * FROM outbox WHERE owner=? AND status IN ('draft', 'awaiting') ORDER BY created_at DESC, rowid DESC LIMIT 1", (owner,)).fetchone())

    def list(self, owner, limit=20):
        with self._connect() as db:
            rows = db.execute('SELECT * FROM outbox WHERE owner=? ORDER BY created_at DESC, rowid DESC LIMIT ?', (owner, limit)).fetchall()
            return [self._draft(row) for row in rows]

    def sent_today(self, owner):
        now = self.clock()
        with self._connect() as db:
            return db.execute('SELECT COUNT(*) FROM outbox WHERE owner=? AND status=? AND sent_at>=? AND sent_at<=?',
                              (owner, 'sent', self.day_start(now), now)).fetchone()[0]

    def awaiting_reply(self, owner):
        now = self.clock()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM outbox WHERE owner=? AND status='sent' AND sent_at>=? AND sent_at<=? ORDER BY sent_at DESC, rowid DESC",
                              (owner, now - 30 * 86400, now)).fetchall()
            return [self._draft(row) for row in rows]
