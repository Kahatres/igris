"""The approval round trip: ask the human, record the answer, wake what waits.

The store decides what is valid; this module only connects it to a notification
channel. The channel belongs to the server owner, so every request sent through
it reaches the owner, whoever asked.
"""

import logging

from igris.approvals import ApprovalError
from igris.notify import NotifyError

logger = logging.getLogger("igris.approval_flow")

# Built from the stored status, so the phone shows what was recorded, not what was pressed.
RESULT_TEXT = {
    "approved": "✅ Onaylandı",
    "used": "✅ Onaylandı",
    "denied": "❌ Reddedildi",
    "expired": "⌛ Süresi doldu",
}
GONE_TEXT = "Bu istek artık geçerli değil"


class ApprovalFlow:
    def __init__(self, store, notifier=None, on_answer=None, *, notices=None):
        self.store, self.notifier, self.on_answer = store, notifier, on_answer
        # an admin panel notice store (igris.notices.NoticeStore); None writes no notices
        self.notices = notices
        # called with every owner message (notify.TextMessage) read by the same poll;
        # None drops them. Set by the web server to the Telegram chat's handle().
        self.on_message = None
        self._offset = None

    def request(self, owner, action, summary, *, payload, ttl=None):
        """Open a request and tell the human. A lost message only delays the answer."""
        request_id = self.store.request(owner, action, summary, payload=payload, ttl=ttl)
        if self.notifier is not None:
            try:
                self.notifier.ask(summary, request_id)
            except NotifyError as exc:
                logger.warning("approval notice was not delivered: %s", exc)
        if self.notices is not None:
            try:
                self.notices.add(owner, "approval", summary, source="approvals", ref=request_id,
                                 action={"approval": request_id})
            except Exception as exc:  # the request stands without its panel notice
                logger.warning("approval panel notice was not stored: %s", exc)
        return request_id

    def pending(self, owner):
        return self.store.pending(owner)

    def answer(self, owner, request_id, approved):
        """Record a signed-in user's answer to one of their own open requests."""
        if request_id not in {item["request_id"] for item in self.store.pending(owner)}:
            raise ApprovalError("No open approval request with this id")
        self._record(request_id, approved, "web")

    def pump(self):
        """Apply answers waiting on the channel and return how many counted.

        Telegram redelivers updates after a restart, so an answer the store
        refuses (a replay, an expired request) is skipped. Every press gets a
        reply that states the decision on record. This is the only reader of the
        channel's updates: owner messages from the same poll go to ``on_message``
        after the answers, which must return quickly (it queues slow work).
        Advance the offset only after every message handler succeeds. A failed handoff
        leaves the batch available for redelivery, including harmless approval replays.
        """
        messages = []
        poll = getattr(self.notifier, "poll_updates", None)
        if poll is not None:
            answers, messages, next_offset = poll(self._offset)
        else:
            poll = getattr(self.notifier, "poll_answers", None)
            if poll is None:
                return 0
            answers, next_offset = poll(self._offset)
        counted = self._apply(answers)
        if self._hand_over(messages):
            self._offset = next_offset
        return counted

    def _hand_over(self, messages):
        if self.on_message is None:
            return True
        for message in messages:
            try:
                self.on_message(message)
            except Exception as exc:  # keep the batch available for Telegram redelivery
                logger.warning("owner message was not handled: %s", type(exc).__name__)
                # Preserve order: accepting a later id could hide this failed message.
                return False
        return True

    def _apply(self, answers):
        counted = 0
        for item in answers:
            try:
                self._record(item.request_id, item.approved, item.source)
            except ApprovalError:
                self._reply(item, already_answered=True)
                continue
            counted += 1
            self._reply(item, already_answered=False)
        return counted

    def _reply(self, item, *, already_answered):
        acknowledge = getattr(self.notifier, "acknowledge", None)
        if acknowledge is None:
            return
        try:
            result = RESULT_TEXT.get(self.store.status(item.request_id), GONE_TEXT)
        except ApprovalError:
            result = GONE_TEXT
        try:
            acknowledge(item, result, already_answered=already_answered)
        except NotifyError as exc:
            # The answer is already stored; a redelivered press often lands here.
            logger.info("reply to a button press failed: %s", exc)

    def _record(self, request_id, approved, source):
        self.store.answer(request_id, approved, source)
        if self.on_answer is not None:
            self.on_answer(request_id, approved)
