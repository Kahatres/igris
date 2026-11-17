"""Send the exact mail a human approved, with durable replay protection."""

import logging
import time
from typing import Callable

from igris.approvals import ApprovalError
from igris.gmail import GmailError, GmailScopeError, GmailUncertainError
from igris.mail_compose import (
    ComposeError, build_message, fits_link, normalize_subject, outlook_compose_url,
)
from igris.outbox import OutboxError, payload
from igris.people import valid_address


TELEGRAM_LIMIT = 4096

logger = logging.getLogger("igris.mail_send")


def _parts(text: str) -> list[str]:
    """Keep every character, preferring line boundaries over hard splits."""
    result = []
    while len(text) > TELEGRAM_LIMIT:
        end = text.rfind("\n", 0, TELEGRAM_LIMIT) + 1
        if not end:
            end = TELEGRAM_LIMIT
        result.append(text[:end])
        text = text[end:]
    if text:
        result.append(text)
    return result


def _approval_parts(draft, names: dict[str, str] | None) -> tuple[str, str]:
    """The identifying header and the closing question of an approval message."""
    names = names or {}
    def recipients(addresses):
        return ", ".join(f"{names[address]} <{address}>" if address in names else address
                         for address in addresses)

    identity = "Kişisel (Gmail)" if draft.identity == "personal" else "Okul"
    header = f"Kimlik: {identity}\nAlıcı: {recipients(draft.to)}"
    if draft.cc:
        header += f"\nBilgi: {recipients(draft.cc)}"
    header += f"\nKonu: {draft.subject}"
    question = "\n\nBu e-postayı göndereyim mi?"
    if len(header + question) > TELEGRAM_LIMIT:
        raise ComposeError("Approval details exceed the message limit")
    return header, question


def approval_summary(draft, names: dict[str, str] | None = None) -> str:
    """The complete approval text (identity, recipients, subject and the whole body).

    This is the stored approval summary the web panel shows, so whoever approves there
    always sees the exact body that will be sent, however long it is.
    """
    header, question = _approval_parts(draft, names)
    return header + "\n\n" + draft.body + question


def approval_text(draft, names: dict[str, str] | None = None) -> list[str]:
    """Telegram messages: the complete text, keeping the identifying details by the buttons."""
    header, question = _approval_parts(draft, names)
    complete = approval_summary(draft, names)
    if len(complete) <= TELEGRAM_LIMIT:
        return [complete]
    return _parts(draft.body) + [header + question]


def thread_key(draft) -> str:
    """Use a Gmail thread when known, otherwise a subject/recipient match key."""
    if draft.identity == "personal" and (draft.sent_thread_id or draft.thread_id):
        return draft.sent_thread_id or draft.thread_id
    return "subj:" + normalize_subject(draft.subject) + "|" + draft.to[0].casefold()


class MailSender:
    def __init__(self, *, outbox, flow, notifier, followups=None,
                 gmail_client: Callable[[], object | None], school_address: str = "",
                 followup_seconds: float = 3 * 86400, clock=time.time):
        self.outbox, self.flow, self.notifier = outbox, flow, notifier
        self.followups, self.gmail_client = followups, gmail_client
        self.school_address = school_address
        self.followup_seconds, self.clock = followup_seconds, clock

    @staticmethod
    def _message(draft):
        if any(not valid_address(address) for address in (*draft.to, *draft.cc)):
            raise ComposeError("Invalid recipient address")
        return build_message(to=list(draft.to), cc=list(draft.cc),
                             subject=draft.subject, body=draft.body,
                             in_reply_to=draft.in_reply_to, references=draft.references)

    def ask(self, owner: str, draft_id: str) -> str:
        draft = self.outbox.get(owner, draft_id)
        if draft is None or draft.status not in {"draft", "awaiting"}:
            raise OutboxError("Draft is not available for approval")
        self._message(draft)
        parts = approval_text(draft)
        summary = approval_summary(draft)
        if draft.status == "awaiting":
            draft = self.outbox.revise(owner, draft_id)
        for part in parts[:-1]:
            self.notifier.send(part)
        if len(parts) == 1:
            request_id = self.flow.request(owner, "mail.send", summary, payload=payload(draft))
        else:
            request_id = self._request_long(owner, summary, parts[-1], payload(draft))
        self.outbox.link_request(owner, draft.id, draft.revision, request_id, "mail.send", request_id)
        self.outbox.mark_awaiting(owner, draft_id, request_id)
        return request_id

    def _request_long(self, owner, summary, question, data):
        """Open a mail.send request whose stored summary holds the whole body.

        Telegram already received the body in parts, so its button message stays the
        short header and question; the stored summary and the panel notice, which the
        web interface shows next to its approve button, carry the complete text.
        Mirrors ApprovalFlow.request, which sends its summary to the notifier unchanged.
        """
        flow = self.flow
        request_id = flow.store.request(owner, "mail.send", summary, payload=data)
        notifier = getattr(flow, "notifier", None)
        if notifier is not None:
            try:
                notifier.ask(question, request_id)
            except Exception as exc:  # a lost message only delays the answer
                logger.warning("approval notice was not delivered: %s", type(exc).__name__)
        notices = getattr(flow, "notices", None)
        if notices is not None:
            try:
                notices.add(owner, "approval", summary, source="approvals", ref=request_id,
                            action={"approval": request_id})
            except Exception as exc:  # the request stands without its panel notice
                logger.warning("approval panel notice was not stored: %s", type(exc).__name__)
        return request_id

    def _tell(self, text):
        # A notification failure must never turn a delivered mail into a retry.
        try:
            self.notifier.send(text)
        except Exception:
            pass

    def handle_answer(self, owner: str, request_id: str, approved: bool) -> bool:
        association = self.outbox.request_link(owner, request_id)
        if association is None:
            return False
        draft_id, revision, action, send_request_id = association
        draft = self.outbox.get(owner, draft_id)
        if draft is None:
            self._tell("Bu e-posta taslağı artık bulunamıyor.")
            return True
        if draft.revision != revision or draft.approval_id != send_request_id:
            self._tell("E-posta metni veya onay isteği değişti; yeniden onay gerekiyor.")
            return True
        expected = "handed" if action == "mail.confirm_sent" else "awaiting"
        if draft.status != expected:
            messages = {
                "sent": "Bu e-posta zaten gönderildi; tekrar gönderilmeyecek.",
                "sending": "Gönderim başlatılmış; sonucu belirsiz olabilir. Tekrar gönderilmeyecek.",
                "handed": "Outlook bağlantısı zaten verildi; gönderim onayını bekliyorum.",
                "failed": "Önceki işlem başarısız oldu; yeni bir onay gerekiyor.",
                "uncertain": "Bu e-postanın gidip gitmediği belirsiz; tekrar gönderilmeyecek.",
                "cancelled": "Bu e-posta iptal edildi.",
            }
            self._tell(messages.get(draft.status, "Bu onay artık geçerli değil."))
            return True
        if not approved:
            # The callback flag alone is not an authenticated human decision.
            if self.flow.store.status(request_id) != "denied":
                self._tell("Kaydedilmiş bir ret yanıtı bulunamadı.")
                return True
            if action == "mail.confirm_sent":
                self.outbox.return_handed(owner, draft_id, send_request_id)
                self._tell("E-posta gönderilmedi; taslak yeniden açıldı.")
            else:
                self.outbox.cancel(owner, draft_id)
                self._tell("E-posta iptal edildi.")
            return True
        if action == "mail.confirm_sent":
            self._confirm(draft, request_id)
            return True
        try:
            draft = self.outbox.begin_send(owner, draft_id, request_id)
        except OutboxError as exc:
            if str(exc) == "Daily send limit reached":
                self._tell("Günlük e-posta gönderim limitine ulaşıldı.")
            else:
                self._tell("Bu gönderim zaten işleniyor veya onay artık geçerli değil.")
            return True
        delivered = False
        try:
            self.flow.store.consume(owner, request_id, "mail.send", payload=payload(draft))
            if draft.identity == "school":
                self._hand_school(draft, request_id)
            else:
                client = self.gmail_client()
                if client is None:
                    raise GmailError("Gmail is not configured")
                result = client.send(self._message(draft), thread_id=draft.thread_id)
                delivered = True
                sent = self.outbox.mark_sent(owner, draft_id, message_id=result["id"],
                                             thread_id=result["threadId"])
                self._after_sent(sent)
        except GmailUncertainError:
            self._uncertain(draft)
        except Exception as exc:
            if delivered:
                # Keep the durable sending/sent state locked even if recording failed.
                self._tell("E-posta gönderildi; sonuç kaydedilemedi. Tekrar gönderilmeyecek.")
            else:
                self._failed(draft, exc)
        return True

    def _hand_school(self, draft, send_request_id):
        url = outlook_compose_url(to=list(draft.to), cc=list(draft.cc),
                                  subject=draft.subject, body=draft.body)
        separate_body = not fits_link(url)
        if separate_body:
            # The builder rejects empty bodies; body is its final query field.
            url = url.rsplit("&body=", 1)[0]
        if len(url) > TELEGRAM_LIMIT:
            raise ComposeError("Compose link exceeds the message limit")
        self.outbox.mark_handed(draft.owner, draft.id)
        self.notifier.send("Outlook'ta okul hesabından gönder."
                           + (" Aşağıdaki metni gövdeye kopyala." if separate_body else ""))
        self.notifier.send(url)
        if separate_body:
            for part in _parts(draft.body):
                self.notifier.send(part)
        request_id = self.flow.request(draft.owner, "mail.confirm_sent",
                                       "Outlook'tan gönderdin mi?", payload=payload(draft))
        self.outbox.link_request(draft.owner, draft.id, draft.revision, request_id,
                                 "mail.confirm_sent", send_request_id)

    def _confirm(self, draft, request_id):
        try:
            self.flow.store.consume(draft.owner, request_id, "mail.confirm_sent",
                                    payload=payload(draft))
        except ApprovalError:
            self._tell("Gönderim onayı geçerli değil veya süresi doldu; gönderildi olarak işaretlenmedi.")
            return
        try:
            sent = self.outbox.mark_sent(draft.owner, draft.id)
        except Exception:
            self._tell("Gönderim onayı kaydedilemedi; tekrar gönderme, durumu kontrol et.")
            return
        self._after_sent(sent)

    def _uncertain(self, draft):
        # Gmail may have accepted the mail before the answer was lost. A retry could
        # send it twice, so the draft stays locked and the owner checks Sent mail.
        try:
            self.outbox.mark_uncertain(draft.owner, draft.id, "Gmail send outcome unknown")
        except Exception:
            pass
        self._tell(f"Gmail'e gönderirken bağlantı kesildi; \"{draft.subject}\" e-postası gitmiş "
                   "olabilir. Gmail'de Gönderilenler klasörüne bak. Gitmediyse yeni bir taslak "
                   "iste; bu taslak tekrar gönderilmeyecek.")

    def resume(self, owner: str) -> None:
        """Finish answers that were stored but never acted on (a restart or an error
        between the button press and the send), and reopen drafts whose approval ran out.

        Safe to repeat: every path re-checks the draft state and spends the approval once.
        """
        try:
            pending = self.outbox.open_requests(owner)
        except Exception:
            return
        for draft, request_id, action in pending:
            try:
                status = self.flow.store.status(request_id)
            except ApprovalError:
                continue
            try:
                if status in ("approved", "denied"):
                    self.handle_answer(owner, request_id, status == "approved")
                elif status == "expired" and action == "mail.send":
                    self.outbox.expire_approval(owner, draft.id, request_id)
                    self._tell(f"\"{draft.subject}\" e-postasının onay süresi doldu; "
                               "gönderilmedi. İstersen yeniden onay iste.")
            except Exception:
                continue

    def _failed(self, draft, exc):
        # Never copy exception text: transports and callbacks can echo secrets.
        if isinstance(exc, GmailScopeError):
            reason = "Gmail send permission missing"
            message = "Gmail gönderim izni eksik. python tools/gmail_auth.py login komutunu çalıştır."
        elif isinstance(exc, ApprovalError):
            reason = "Approval invalid or expired"
            message = "Onay geçersiz veya süresi dolmuş; e-posta gönderilmedi. Yeni onay gerekiyor."
        elif isinstance(exc, GmailError):
            reason = "Gmail send failed"
            message = "Gmail gönderimi başarısız oldu; tekrar denemek için yeni onay gerekiyor."
        elif isinstance(exc, ComposeError):
            reason = "Mail composition failed"
            message = "E-posta hazırlanamadı; taslağı düzeltip yeniden onayla."
        else:
            reason = "Mail operation failed"
            message = "E-posta işlemi tamamlanamadı; tekrar denemeden önce gönderim durumunu kontrol et."
        current = self.outbox.get(draft.owner, draft.id)
        if current.status in {"sending", "handed"}:
            self.outbox.mark_failed(draft.owner, draft.id, reason)
        self._tell(message)

    def _after_sent(self, draft):
        # Delivery has already been recorded. Follow-up/notice failures must not
        # reopen the send path or erase evidence that the mail left the system.
        if self.followups is not None:
            try:
                key = thread_key(draft)
                if not any(row["thread_id"] == key for row in self.followups.list(draft.owner)):
                    self.followups.add(draft.owner, key, self.followup_seconds, note=draft.subject)
            except Exception:
                self._tell("E-posta gönderildi; yanıt takip hatırlatıcısı kurulamadı.")
                return
        self._tell("E-posta gönderildi.")
