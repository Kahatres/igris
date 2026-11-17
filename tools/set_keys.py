"""Manual, owner-run helper that stores free-provider API keys in the OS keyring. Never run by pytest.

    python tools/set_keys.py              # ask for every key in turn
    python tools/set_keys.py groq mistral # only these providers
    python tools/set_keys.py --status     # show which keys are stored, change nothing

Keys are typed with echo off and stored in keyring under service "igris", the place
igris reads them from. Press Enter on an empty prompt to skip a provider (an existing
key is kept). Keys are never printed, logged or written to a file.
Requires the `keyring` package (pip install keyring); on Windows it uses Credential Manager.
"""

from __future__ import annotations

import sys
from getpass import getpass

SERVICE = "igris"

# (provider, secret name, where to create it, note). URLs were written from memory:
# if a page has moved, find "API keys" in the provider's console.
STEPS = (
    ("gemini", "GEMINI_API_KEY", "https://aistudio.google.com/apikey",
     "Google AI Studio key. A Google AI Pro subscription does not include API quota."),
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/keys",
     "Only ':free' models are used. Never add credit unless you want the 1,000/day tier."),
    ("groq", "GROQ_API_KEY", "https://console.groq.com/keys", ""),
    ("cerebras", "CEREBRAS_API_KEY", "https://cloud.cerebras.ai/",
     "Trial credits, not a renewing free tier."),
    ("mistral", "MISTRAL_API_KEY", "https://console.mistral.ai/api-keys",
     "Needs phone verification; the free plan may train on your data."),
    ("nvidia", "NVIDIA_API_KEY", "https://build.nvidia.com/",
     "Key starts with 'nvapi-'."),
    ("cloudflare", "CLOUDFLARE_ACCOUNT_ID", "https://dash.cloudflare.com/",
     "Account ID (not secret, shown on the Workers AI page); asked first."),
    ("cloudflare", "CLOUDFLARE_API_TOKEN", "https://dash.cloudflare.com/profile/api-tokens",
     "Token with the 'Workers AI' permission."),
    ("sambanova", "SAMBANOVA_API_KEY", "https://cloud.sambanova.ai/apis", ""),
    ("cohere", "COHERE_API_KEY", "https://dashboard.cohere.com/api-keys",
     "Trial key: evaluation use only."),
    ("evren", "EVREN_API_KEY", "EVREN console, 'API Anahtarları' page (sign in with e-Devlet)",
     "Free daily quota until 2026-12-01. Shared by all your EVREN keys."),
    ("telegram", "TELEGRAM_BOT_TOKEN", "Telegram, @BotFather, /newbot",
     "Then send your bot one message so it may write to you."),
    ("telegram", "TELEGRAM_CHAT_ID", "Telegram, @userinfobot",
     "Your own numeric id (not secret). Group chats are refused."),
    ("ntfy", "NTFY_TOPIC", "Any long random name; subscribe to it in the ntfy app",
     "Used only without Telegram. Anyone who knows the name can read it."),
    ("gmail", "GMAIL_CLIENT_ID", "https://console.cloud.google.com/apis/credentials",
     "OAuth client of type 'Desktop app'; ends in '.apps.googleusercontent.com'."),
    ("gmail", "GMAIL_CLIENT_SECRET", "same OAuth client, 'Client secrets' section",
     "Starts with 'GOCSPX-'. Consent screen: External, published 'In production'."),
    ("moodle", "MOODLE_ICAL_URL",
     "Moodle > Calendar > Export calendar: all courses, recent and next 60 days, Get calendar URL",
     "The whole https:// link; it carries a private token, so it is stored only here."),
    ("edge", "EDGE_TOKEN", "python -c \"import secrets; print(secrets.token_urlsafe(32))\"",
     "Shared by the hub and each edge daemon (IGRIS_EDGE_TOKEN on the device)."),
)


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def main(argv: list[str], *, keyring_module=None, ask=getpass, out=print) -> int:
    kr = keyring_module or _keyring()
    if kr is None:
        out("keyring is not installed. Run:  pip install keyring   then start this again.")
        return 1
    status_only = "--status" in argv
    wanted = [a for a in argv if not a.startswith("--")]
    known = {p for p, *_ in STEPS}
    unknown = [w for w in wanted if w not in known]
    if unknown:
        out(f"unknown provider(s): {', '.join(unknown)}. Choose from: {', '.join(sorted(known))}")
        return 2
    steps = [s for s in STEPS if not wanted or s[0] in wanted]

    saved = skipped = 0
    for number, (provider, name, url, note) in enumerate(steps, 1):
        try:
            present = bool(kr.get_password(SERVICE, name))
        except Exception as exc:
            out(f"keyring is not usable: {exc}")
            return 1
        mark = "stored" if present else "missing"
        if status_only:
            out(f"{provider:<11} {name:<24} {mark}")
            continue
        out(f"\n[{number}/{len(steps)}] {provider}  ({name}, currently {mark})")
        out(f"    create it at: {url}")
        if note:
            out(f"    note: {note}")
        try:
            value = ask(f"    paste {name} (Enter = skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            out("\nstopped; keys entered so far are saved.")
            return 130
        if not value:
            skipped += 1
            continue
        if any(c.isspace() for c in value):
            out("    that looks like more than one word; skipped. Run again for this provider.")
            skipped += 1
            continue
        try:
            kr.set_password(SERVICE, name, value)
        except Exception as exc:
            out(f"    could not store the key: {exc}")
            return 1
        saved += 1
        out("    saved.")
    if not status_only:
        out(f"\ndone: {saved} saved, {skipped} skipped. Check with:  python tools/set_keys.py --status")
        out("Then try one:  python tools/live_check.py groq")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
