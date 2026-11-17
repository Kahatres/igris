"""Shared test setup: tests never depend on this machine's API keys or real codex login."""
import pytest

import igris.llm as llm

PAID_ENV = llm.CLAUDE_PAID_ENV + llm.CODEX_PAID_ENV + ("IGRIS_ALLOW_PAID",)


@pytest.fixture(autouse=True)
def _subscription_env(monkeypatch, tmp_path_factory):
    # Nothing a test writes may reach the owner's real data folder.
    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path_factory.mktemp("igris-data")))
    for name in PAID_ENV:
        monkeypatch.delenv(name, raising=False)
    # Settings read from the real environment would find this machine's multiagent checkout.
    monkeypatch.setenv("IGRIS_MULTIAGENT_DIR", "")
    # Codex backends built without an explicit login_check would run the real `codex login status`.
    monkeypatch.setattr(llm, "codex_login_problem", lambda exe, runner: None)
    # Commands must not depend on where this machine installed the CLIs.
    monkeypatch.setattr(llm, "resolve_cli", lambda exe: [exe])
