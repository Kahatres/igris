import pytest

from igris.outbox import OutboxError, OutboxStore, payload


@pytest.fixture
def store(tmp_path):
    return OutboxStore(str(tmp_path / 'outbox.db'))


def make(store, owner='a', **kwargs):
    values = dict(identity='personal', to=['a@example.com'], subject='Subject', body='Body')
    values.update(kwargs)
    return store.create(owner, **values)


def send(store, draft, approval='A1'):
    store.mark_awaiting(draft.owner, draft.id, approval)
    store.begin_send(draft.owner, draft.id, approval)
    return store.mark_sent(draft.owner, draft.id)


def test_create_rejects_too_many_recipients(store):
    with pytest.raises(OutboxError):
        make(store, to=['a'] * 4, cc=['b'] * 2)
    assert len(make(store, to=['a'] * 3, cc=['b'] * 2).cc) == 2


@pytest.mark.parametrize('changes', [dict(identity='work'), dict(to=[]), dict(subject=''), dict(body='')])
def test_create_rejects_bad_identity_and_empty_fields(store, changes):
    with pytest.raises(OutboxError):
        make(store, **changes)


def test_revise_invalidates_approval(store):
    draft = make(store)
    store.mark_awaiting('a', draft.id, 'A1')
    revised = store.revise('a', draft.id, body='Changed')
    assert (revised.status, revised.approval_id, revised.revision) == ('draft', None, 2)
    with pytest.raises(OutboxError):
        store.begin_send('a', draft.id, 'A1')


def test_begin_send_needs_matching_approval(store):
    draft = make(store)
    store.mark_awaiting('a', draft.id, 'A1')
    with pytest.raises(OutboxError):
        store.begin_send('a', draft.id, 'wrong')
    assert store.begin_send('a', draft.id, 'A1').status == 'sending'


def test_begin_send_is_single_use(store):
    draft = make(store)
    store.mark_awaiting('a', draft.id, 'A1')
    store.begin_send('a', draft.id, 'A1')
    with pytest.raises(OutboxError):
        store.begin_send('a', draft.id, 'A1')


def test_daily_limit_blocks_the_21st(tmp_path):
    now = [100000.0]
    store = OutboxStore(str(tmp_path / 'outbox.db'), clock=lambda: now[0], daily_limit=2,
                        day_start=lambda t: (t // 86400) * 86400)
    send(store, make(store), 'A1')
    send(store, make(store), 'A2')
    draft = make(store)
    store.mark_awaiting('a', draft.id, 'A3')
    with pytest.raises(OutboxError):
        store.begin_send('a', draft.id, 'A3')
    now[0] += 86400
    assert store.begin_send('a', draft.id, 'A3').status == 'sending'


def test_failed_send_cannot_be_retried_with_old_approval(store):
    draft = make(store)
    store.mark_awaiting('a', draft.id, 'A1')
    store.begin_send('a', draft.id, 'A1')
    assert store.mark_failed('a', draft.id, 'Failed').status == 'failed'
    with pytest.raises(OutboxError):
        store.begin_send('a', draft.id, 'A1')
    with pytest.raises(OutboxError):
        store.mark_awaiting('a', draft.id, 'A2')
    assert store.reopen('a', draft.id).status == 'draft'
    assert store.mark_awaiting('a', draft.id, 'A2').status == 'awaiting'


def test_owner_isolation(store):
    draft = make(store)
    assert store.get('b', draft.id) is None
    assert store.list('b') == []
    assert store.newest_open('b') is None
    with pytest.raises(OutboxError):
        store.cancel('b', draft.id)


def test_payload_shape(store):
    draft = make(store, cc=['b@example.com'])
    assert payload(draft) == dict(draft_id=draft.id, identity='personal', to=['a@example.com'],
                                  cc=['b@example.com'], subject='Subject', body='Body')


def test_cancel_states(store):
    draft = make(store)
    assert store.cancel('a', draft.id).status == 'cancelled'
    draft = make(store)
    store.mark_awaiting('a', draft.id, 'A1')
    assert store.cancel('a', draft.id).status == 'cancelled'
    draft = make(store)
    store.mark_awaiting('a', draft.id, 'A2')
    store.begin_send('a', draft.id, 'A2')
    store.mark_handed('a', draft.id)
    assert store.cancel('a', draft.id).status == 'cancelled'
    draft = send(store, make(store))
    with pytest.raises(OutboxError):
        store.cancel('a', draft.id)


def test_survives_reopen(tmp_path):
    path = str(tmp_path / 'outbox.db')
    draft = make(OutboxStore(path), in_reply_to='<id>', references='<ref>', thread_id='thread')
    assert OutboxStore(path).get('a', draft.id) == draft


def test_queries_and_sent_metadata(tmp_path):
    now = [10000000.0]
    store = OutboxStore(str(tmp_path / 'outbox.db'), clock=lambda: now[0])
    old = make(store)
    store.mark_awaiting('a', old.id, 'old')
    store.begin_send('a', old.id, 'old')
    store.mark_sent('a', old.id, message_id='msg', thread_id='thread')
    now[0] += 31 * 86400
    fresh = make(store)
    assert store.newest_open('a') == fresh
    assert store.list('a', limit=1) == [fresh]
    assert store.awaiting_reply('a') == []
    store.mark_awaiting('a', fresh.id, 'fresh')
    store.begin_send('a', fresh.id, 'fresh')
    result = store.mark_sent('a', fresh.id, message_id='msg', thread_id='thread')
    assert (result.sent_message_id, result.sent_thread_id, result.sent_at) == ('msg', 'thread', now[0])
    assert store.awaiting_reply('a') == [result]
def test_request_links_are_durable_owner_scoped_and_revision_bound(store):
    draft = make(store)
    store.link_request('a', draft.id, draft.revision, 'request', 'mail.send', 'request')
    store.revise('a', draft.id, body='New body')
    reopened = OutboxStore(store.path)
    assert reopened.request_link('a', 'request') == (draft.id, 1, 'mail.send', 'request')
    assert reopened.request_link('b', 'request') is None
    with pytest.raises(OutboxError):
        store.link_request('b', draft.id, 2, 'foreign', 'mail.send', 'foreign')
    with pytest.raises(OutboxError):
        store.link_request('a', draft.id, 1, 'stale', 'mail.send', 'stale')


def test_return_handed_to_draft_requires_matching_approval(store):
    draft = make(store, identity='school')
    store.mark_awaiting('a', draft.id, 'approval')
    store.begin_send('a', draft.id, 'approval')
    store.mark_handed('a', draft.id)
    for owner, approval in [('b', 'approval'), ('a', 'wrong')]:
        with pytest.raises(OutboxError):
            store.return_handed(owner, draft.id, approval)
    result = store.return_handed('a', draft.id, 'approval')
    assert result.status == 'draft' and result.approval_id is None
    assert result.revision == 2
    with pytest.raises(OutboxError):
        store.return_handed('a', draft.id, 'approval')


def test_uncertain_is_locked(store):
    draft = make(store)
    store.mark_awaiting('a', draft.id, 'A1')
    store.begin_send('a', draft.id, 'A1')
    assert store.mark_uncertain('a', draft.id, 'Gmail send outcome unknown').status == 'uncertain'
    for change in (lambda: store.reopen('a', draft.id), lambda: store.cancel('a', draft.id),
                   lambda: store.mark_sent('a', draft.id), lambda: store.revise('a', draft.id, body='x')):
        with pytest.raises(OutboxError):
            change()


def test_expire_approval_reopens_new_revision(store):
    draft = make(store)
    store.mark_awaiting('a', draft.id, 'A1')
    with pytest.raises(OutboxError):
        store.expire_approval('a', draft.id, 'OTHER')
    reopened = store.expire_approval('a', draft.id, 'A1')
    assert (reopened.status, reopened.approval_id, reopened.revision) == ('draft', None, 2)


def test_open_requests_lists_current_links_only(store):
    draft = make(store)
    store.link_request('a', draft.id, 1, 'A1', 'mail.send', 'A1')
    store.mark_awaiting('a', draft.id, 'A1')
    assert [(d.id, r, a) for d, r, a in store.open_requests('a')] == [(draft.id, 'A1', 'mail.send')]
    assert store.open_requests('b') == []
    store.revise('a', draft.id, body='changed')
    assert store.open_requests('a') == []
