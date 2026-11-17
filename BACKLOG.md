# Backlog

Every channel follows one loop: an event arrives, Igris weighs how much it matters, proposes an
action, the owner approves, Igris acts. Telegram is the one place for notices, approvals and
commands. At most five items are open; an item is done when it has been seen working in real
use (not only in tests) and is merged into `main`.

1. **Approved mail replies in real use.** Contacts, exact-text approval, Gmail sending, Outlook
   handoff and reply tracking are built and tested.
   *Done when:* a real mail is sent after a Telegram approval and its real answer is reported back.
2. **Full calendar from chat.** Recurring events (lessons, training, meetings) live in the task
   calendar with reminders and can be added and moved from the web chat and Telegram.
   *Done when:* for one week no calendar entry had to be typed into Google Calendar by hand.
3. **WhatsApp through the phone.** An Android notification bridge passes incoming WhatsApp
   messages to Igris and sends an approved reply through the notification's reply action (no
   unofficial WhatsApp client). Igris weighs the sender and proposes replies for missed messages.
   *Done when:* a missed message was answered after one approval.
4. **Tutor mode.** Review the owner's course work, show the mistakes and keep a record of weak
   topics, building on the study workspace.
5. **Vision fallback.** Image transcription has no Codex CLI fallback, because no flag set was
   verified to limit a Codex call to reading exactly one file. Add one once it can be verified.
