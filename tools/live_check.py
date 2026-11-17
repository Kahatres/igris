"""Manual, owner-run live check of one free provider. Never run by pytest.

    python tools/live_check.py groq
    python tools/live_check.py gemini
    python tools/live_check.py opencode glm-5.3-flash-free

Sends exactly one short request, counted against the provider's daily quota, and
prints the answering model, the latency and any rate-limit headers. For opencode it
also prints whether the model trains on data and the reported cost. The key is read
from keyring (service "igris") or IGRIS_<KEY_NAME> and is never printed.
"""

import dataclasses
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from igris.config import COMPAT_BACKENDS, load_settings  # noqa: E402
from igris.llm import BackendError, Message, PaidUsageBlocked, build_backend  # noqa: E402

PROMPT = "Reply with exactly one Turkish word meaning 'hello'."
PROVIDERS = COMPAT_BACKENDS + ("gemini", "opencode")


def main(argv: list[str]) -> int:
    name = argv[0] if argv else ""
    if name not in PROVIDERS or len(argv) > (2 if name == "opencode" else 1):
        print("usage: python tools/live_check.py <" + "|".join(PROVIDERS) + ">"
              " [model, opencode only]")
        return 2
    settings = load_settings()
    if name == "opencode" and len(argv) == 2:
        settings = dataclasses.replace(settings, opencode_model=argv[1])
    try:
        backend = build_backend(name, claude_model="", codex_model="", timeout=60,
                                settings=settings)
    except ValueError as exc:
        print(f"{name}: {exc}")
        return 2
    started = time.monotonic()
    try:
        text = backend.complete("You are a terse assistant.", [Message("user", PROMPT)])
    except PaidUsageBlocked as exc:
        print(f"{name} ({backend.model}): PAID USAGE reported after "
              f"{time.monotonic() - started:.2f}s: {exc}")
        return 1
    except BackendError as exc:
        print(f"{name}: FAILED after {time.monotonic() - started:.2f}s: {exc}")
        return 1
    model = getattr(backend, "_free_model", None) or backend.model
    print(f"{name} ({model}): {text!r} in {time.monotonic() - started:.2f}s")
    if name == "opencode":
        # the backend raises PaidUsageBlocked on any cost above zero, so reaching here means 0
        print(f"trains on data: {'yes' if backend.trains_on_data else 'no'}; reported cost: 0")
    limits = getattr(backend, "limits", lambda: {})()
    if limits:
        print("rate limits:", limits)
    quota = getattr(backend, "quota", None)
    if quota is not None:
        print(f"used today: {quota.used(name)}, remaining: {quota.remaining(name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
