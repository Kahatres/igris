"""Owner-scoped people and outgoing draft tools for chat."""

from __future__ import annotations

import re
from email import message_from_bytes, policy
from email.utils import getaddresses

from igris.people import PeopleError, valid_address
from igris.inbox import original_sender, plain_body, school_forward
from igris.outbox import OutboxError
from igris.tools import Tool, ToolError, ToolRegistry


def register_people_mail_tools(registry: ToolRegistry, *, people, outbox, sender, owner: str,
                               inbox=None, gmail_client=None, school_domains=()) -> None:
    """Register contact and draft management tools; approval remains separate."""

    def person_line(person):
        return (f"id={person.id} name={person.name} role={person.role or '-'} "
                f"address={person.emails[0] if person.emails else '-'} "
                f"identity={person.identity} language={person.language} tone={person.tone}")

    def split(value):
        return [part.strip() for part in value.split(',') if part.strip()]

    def owner_typed(addresses):
        """An address is saved only when the owner wrote it in this turn's message."""
        turn = getattr(registry, 'turn_text', None)
        text = (turn() if callable(turn) else None) or ''
        # Whole address only: "notece@x.lt" does not vouch for "ece@x.lt", while a
        # Turkish suffix ("ece@x.lt'ye") or punctuation around it is fine.
        missing = [address for address in addresses
                   if not re.search(r'(?<![\w.+-])' + re.escape(address) + r'(?![\w@-]|\.\w)',
                                    text, re.IGNORECASE)]
        if missing:
            raise ToolError('Addresses are saved only when the owner writes them in their '
                            'message; ask the owner for the address of this person.')

    def person_find(query):
        found = people.find(owner, query)
        return '\n'.join(person_line(person) for person in found) if found else 'No people found.'

    def person_save(name, email='', aliases='', role='', importance='normal', language='en',
                    tone='formal', identity='personal', notes='', id=''):
        owner_typed(split(email))
        try:
            if id:
                # Default-valued arguments do not overwrite unrelated stored preferences.
                fields = {'name': name}
                for key, value, default in (('emails', tuple(split(email)), ()),
                                            ('aliases', tuple(split(aliases)), ()),
                                            ('role', role, ''), ('importance', importance, 'normal'),
                                            ('language', language, 'en'), ('tone', tone, 'formal'),
                                            ('identity', identity, 'personal'), ('notes', notes, '')):
                    if value != default:
                        fields[key] = value
                person = people.update(owner, id, **fields)
            else:
                person = people.add(owner, name, emails=tuple(split(email)), aliases=tuple(split(aliases)),
                                    role=role, importance=importance, language=language, tone=tone,
                                    identity=identity, notes=notes)
        except PeopleError as exc:
            raise ToolError(str(exc)) from None
        return person_line(person)

    def person_remove(id):
        if not people.remove(owner, id):
            raise ToolError('Person not found.')
        return 'Person removed.'

    def resolve(value):
        addresses, persons = [], []
        for token in split(value):
            person = people.get(owner, token)
            address = None
            if person is None and '@' in token:
                person = people.by_address(owner, token)
                if person is not None:
                    address = next(item for item in person.emails if item.casefold() == token.casefold())
            elif person is None:
                matches = people.find(owner, token)
                if len(matches) > 1:
                    raise ToolError('Ambiguous person; choose one: ' + '; '.join(person_line(p) for p in matches))
                person = matches[0] if matches else None
            if person is None:
                raise ToolError('Person not found. Ask the owner for the address and save it first.')
            if not person.emails:
                raise ToolError(f'Ask the owner for the address of {person.name} and save it first.')
            addresses.append(address or person.emails[0])
            persons.append(person)
        return addresses, persons

    def draft_text(draft):
        return (f"Draft id={draft.id}\nIdentity: {draft.identity}\nTo: {', '.join(draft.to)}\n"
                f"Cc: {', '.join(draft.cc)}\nSubject: {draft.subject}\n\n{draft.body}")

    def get_draft(draft_id):
        draft = outbox.get(owner, draft_id) if draft_id else outbox.newest_open(owner)
        if draft is None:
            raise ToolError('Draft not found.')
        return draft

    def reply_source(message_id):
        if inbox is None or message_id not in inbox.known([message_id]):
            raise ToolError('Received mail not found. Use an id from mail_recent.')
        try:
            client = gmail_client() if gmail_client else None
            raw, labels, thread = client.raw_details(message_id)
            message = message_from_bytes(raw, policy=policy.default)
            school = school_forward(message, school_domains)
            forwarded = (original_sender(message, plain_body(message), school_domains)
                         if school else None)
            address_header = forwarded or message.get('Reply-To') or message.get('From', '')
            addresses = getaddresses([str(address_header)])
            if len(addresses) != 1 or not valid_address(addresses[0][1]) or 'SENT' in labels:
                raise ValueError('Invalid reply source')
            # A Reply-To that differs from From can redirect the answer (a phishing trick);
            # then both header addresses are offered and the owner picks one.
            alternative = None
            if not forwarded and message.get('Reply-To'):
                senders = getaddresses([str(message.get('From', ''))])
                if (len(senders) == 1 and valid_address(senders[0][1])
                        and senders[0][1].casefold() != addresses[0][1].casefold()):
                    alternative = senders[0][1]
            rfc_id = str(message.get('Message-ID', '')).strip()
            refs = str(message.get('References', '')).strip()
            source_subject = str(message.get('Subject', '')).strip()
            if not rfc_id or not source_subject or any(c in rfc_id + refs + source_subject for c in '\r\n'):
                raise ValueError('Missing or invalid reply headers')
            return (addresses[0][1], school, rfc_id, ' '.join(filter(None, (refs, rfc_id))),
                    source_subject, thread, alternative)
        except Exception:
            raise ToolError('Unable to read valid reply headers from received mail.') from None

    def mail_draft(to='', subject='', body='', cc='', identity='', reply_to_id=''):
        reply = reply_source(reply_to_id) if reply_to_id else None
        header_addresses = [reply[0], reply[6]] if reply and reply[6] else []
        if header_addresses and not any(to.strip().casefold() == address.casefold()
                                        for address in header_addresses):
            raise ToolError(f'The mail asks for answers at {reply[0]} (Reply-To) but was sent '
                            f'from {reply[6]} (From). Ask the owner which address to answer, '
                            'then pass it as to.')
        if header_addresses:
            recipients = [next(address for address in header_addresses
                                if address.casefold() == to.strip().casefold())]
            first = []
        elif reply and (not to or to.casefold() == reply[0].casefold()):
            recipients, first = [reply[0]], []
        else:
            recipients, first = resolve(to)
            if reply and recipients != [reply[0]]:
                raise ToolError('Reply recipient must match the received mail headers.')
        copies, others = resolve(cc)
        persons = first + others
        if not recipients:
            raise ToolError('At least one recipient is required.')
        if not identity and len({person.identity for person in persons}) > 1:
            raise ToolError('Recipients have mixed identities; ask which identity to use.')
        default_identity = ('school' if reply[1] else 'personal') if reply else persons[0].identity
        try:
            draft = outbox.create(owner, identity=identity or default_identity,
                                  to=recipients, cc=copies,
                                  subject=subject or ('Re: ' + reply[4] if reply else ''), body=body,
                                  in_reply_to=reply[2] if reply else None,
                                  references=reply[3] if reply else None,
                                  thread_id=reply[5] if reply else None)
        except OutboxError as exc:
            raise ToolError(str(exc)) from None
        return draft_text(draft)

    def mail_revise(draft_id='', subject='', body='', to='', cc='', identity=''):
        draft = get_draft(draft_id)
        changes = {key: value for key, value in (('subject', subject), ('body', body),
                                                  ('identity', identity)) if value}
        if to:
            changes['to'] = resolve(to)[0]
        if cc:
            changes['cc'] = resolve(cc)[0]
        try:
            revised = outbox.revise(owner, draft.id, **changes)
        except OutboxError as exc:
            raise ToolError(str(exc)) from None
        return draft_text(revised)

    def mail_ask(draft_id=''):
        draft = get_draft(draft_id)
        try:
            sender.ask(owner, draft.id)
        except Exception:
            raise ToolError('Unable to request approval.') from None
        return 'Approval was sent to Telegram.'

    def mail_cancel(draft_id=''):
        draft = get_draft(draft_id)
        try:
            outbox.cancel(owner, draft.id)
        except OutboxError as exc:
            raise ToolError(str(exc)) from None
        return 'Draft cancelled.'

    def mail_outbox():
        drafts = outbox.list(owner, limit=10)
        now = outbox.clock()
        return '\n'.join(
            f"id={draft.id} status={draft.status} to={', '.join(draft.to)} "
            f"cc={', '.join(draft.cc)} subject={draft.subject} "
            f"age={max(0, int(now - draft.created_at))}s" for draft in drafts
        ) if drafts else 'Outbox is empty.'

    for name, func, description in (
        ('person_find', person_find, 'Find contacts by name or alias. Args: query.'),
        ('person_save', person_save, 'Add or update a contact. Args: name, email, aliases, role, importance, language, tone, identity, notes, id.'),
        ('person_remove', person_remove, 'Remove a contact. Args: id.'),
        ('mail_draft', mail_draft, 'Draft mail to saved contacts; comma-separate names or ids. For a reply use a received mail id from mail_recent as reply_to_id; omit to and subject to use its headers. Args: to, subject, body, cc, identity, reply_to_id.'),
        ('mail_revise', mail_revise, 'Revise a draft. Args: draft_id, subject, body, to, cc, identity.'),
        ('mail_ask', mail_ask, 'Request Telegram approval for a draft. Args: draft_id.'),
        ('mail_cancel', mail_cancel, 'Cancel a draft. Args: draft_id.'),
        ('mail_outbox', mail_outbox, 'List the last ten outgoing mails without bodies.'),
    ):
        registry.add(Tool(name, description, func))
