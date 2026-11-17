from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from igris.vision_read import ReadFailed, VisionReader, build_vision_reader, read_deadline
from igris.llm import CLAUDE_PAID_ENV


@pytest.fixture(autouse=True)
def subscription_env(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(key, raising=False)


def test_command_copy_and_cleanup(tmp_path):
    source = tmp_path / "private photo.png"
    source.write_bytes(b"image bytes")
    seen = []

    def runner(command, **kwargs):
        cwd = Path(kwargs["cwd"])
        seen.append(cwd)
        files = list(cwd.iterdir())
        assert len(files) == 1 and files[0].read_bytes() == b"image bytes"
        assert files[0] != source
        assert "-p" in command
        assert command[command.index("--tools") + 1] == "Read"
        permission = command[command.index("--allowedTools") + 1]
        assert permission == f"Read(./{files[0].name})"
        assert command[command.index("--permission-mode") + 1] == "dontAsk"
        assert command[command.index("--model") + 1] == "vision-test"
        for flag in ("--restricted", "--safe-mode", "--strict-mcp-config",
                     "--disable-slash-commands", "--no-session-persistence", "--no-chrome"):
            assert flag in command
        assert kwargs["timeout"] == 7 and kwargs["capture_output"]
        assert "LaTeX" in command[command.index("--system-prompt") + 1]
        assert str(source) not in kwargs["input"]
        assert files[0].name in kwargs["input"]
        return SimpleNamespace(returncode=0, stdout="  # Notes\n$x^2$  ")

    reader = VisionReader(runner=runner, exe="fake-claude", model="vision-test", timeout=7)
    assert reader.read(source) == "# Notes\n$x^2$"
    assert seen and not seen[0].exists()
    assert source.read_bytes() == b"image bytes"


@pytest.mark.parametrize("failure", ["exit", "timeout", "empty", "refusal", "missing_cli"])
def test_failures_clean_up(tmp_path, failure):
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"image")
    seen = []

    def runner(command, **kwargs):
        seen.append(Path(kwargs["cwd"]))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 2)
        if failure == "missing_cli":
            raise FileNotFoundError("not installed")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0,
                               stdout="I cannot read this image." if failure == "refusal" else " ")

    with pytest.raises(ReadFailed) as exc:
        VisionReader(runner=runner, exe="fake-claude").read(source)
    assert exc.value.reason
    assert seen and not seen[0].exists()


def test_spending_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    source = tmp_path / "photo.png"
    source.write_bytes(b"image")
    with pytest.raises(ReadFailed, match="paid_usage_blocked"):
        VisionReader(runner=lambda *a, **kw: pytest.fail("must not run")).read(source)


@pytest.mark.parametrize("key", CLAUDE_PAID_ENV)
@pytest.mark.parametrize("value", ["test-key", "1", " true "])
@pytest.mark.parametrize("allow", [None, "0", "true", "1 "])
def test_injected_paid_env_blocks_before_reading_or_running(key, value, allow, tmp_path):
    env = {key: value}
    if allow is not None:
        env["IGRIS_ALLOW_PAID"] = allow
    reader = VisionReader(env=env, runner=lambda *a, **kw: pytest.fail("must not run"))
    with pytest.raises(ReadFailed, match="^paid_usage_blocked$"):
        reader.read(tmp_path / "missing.png")


@pytest.mark.parametrize("key", CLAUDE_PAID_ENV)
@pytest.mark.parametrize("value,allow", [("", ""), ("0", ""), (" FALSE ", ""),
                                        ("test-key", "1")])
def test_injected_env_keeps_paid_guard_allowances(tmp_path, monkeypatch, key, value, allow):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-paid-key")
    source = tmp_path / "page.png"
    source.write_bytes(b"image")
    reader = VisionReader(env={key: value, "IGRIS_ALLOW_PAID": allow}, exe="fake-reader",
                          runner=lambda *a, **kw: SimpleNamespace(returncode=0, stdout="text"))
    assert reader.read(source) == "text"


def test_vision_uses_shared_cli_resolver(tmp_path, monkeypatch):
    source = tmp_path / "page.png"
    source.write_bytes(b"image")
    seen = []

    def resolve(exe, *, strict=False):
        assert exe == "fake-reader" and strict
        return ["node", "/isolated/reader.js"]

    monkeypatch.setattr("igris.vision_read.resolve_cli", resolve)

    def runner(command, **kwargs):
        seen.append(command[:2])
        return SimpleNamespace(returncode=0, stdout="text")

    assert VisionReader(env={}, exe="fake-reader", runner=runner).read(source) == "text"
    assert seen == [["node", "/isolated/reader.js"]]


@pytest.mark.parametrize("error,reason", [(ValueError("unsupported_cli_shim"),
                                          "unsupported_cli_shim"),
                                         (FileNotFoundError("missing shim"), "unavailable")])
def test_shared_resolver_failures_keep_vision_failure_codes(tmp_path, monkeypatch, error, reason):
    source = tmp_path / "page.png"
    source.write_bytes(b"image")

    def resolve(exe, *, strict=False):
        raise error

    monkeypatch.setattr("igris.vision_read.resolve_cli", resolve)
    with pytest.raises(ReadFailed, match=f"^{reason}$"):
        VisionReader(env={}, runner=lambda *a, **kw: pytest.fail("must not run")).read(source)


def test_factory_uses_settings_and_disables_without_cli(monkeypatch):
    monkeypatch.setattr("igris.vision_read.shutil.which", lambda name: "/bin/claude")
    settings = SimpleNamespace(core=True, vision_model="vision-model", web_model="web-model",
                               web_timeout=35)
    reader = build_vision_reader(settings)
    assert reader.model == "vision-model" and reader.timeout == 35
    assert build_vision_reader(SimpleNamespace(core=False)) is None
    assert build_vision_reader(SimpleNamespace(web_model="web-model")).model == "web-model"
    monkeypatch.setattr("igris.vision_read.shutil.which", lambda name: None)
    assert build_vision_reader(settings) is None


def test_per_call_deadline_is_local_and_preserves_configured_timeout(tmp_path):
    source = tmp_path / "page.png"
    source.write_bytes(b"image")
    timeouts = []

    def runner(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="text")

    reader = VisionReader(runner=runner, exe="fake-reader", timeout=7)
    with read_deadline(time.monotonic() + 2):
        assert reader.read(source) == "text"
        # A concurrent request without a deadline retains the configured limit.
        thread = threading.Thread(target=lambda: reader.read(source))
        thread.start()
        thread.join(3)
        assert not thread.is_alive()
    assert reader.read(source) == "text"
    assert 0 < timeouts[0] <= 2
    assert timeouts[1:] == [7, 7] and reader.timeout == 7


def test_expired_deadline_does_not_run_cli_and_cleans_copy(tmp_path, monkeypatch):
    source = tmp_path / "page.png"
    source.write_bytes(b"image")
    folders = []
    import igris.vision_read as module
    original = module.shutil.copyfile

    def copy(src, dest):
        folders.append(Path(dest).parent)
        return original(src, dest)

    monkeypatch.setattr(module.shutil, "copyfile", copy)
    with read_deadline(time.monotonic() - 1):
        with pytest.raises(ReadFailed, match="timeout"):
            VisionReader(runner=lambda *a, **kw: pytest.fail("expired read"),
                         exe="fake-reader").read(source)
    assert folders and not folders[0].exists()


def test_nested_deadline_cannot_extend_outer_deadline(tmp_path):
    source = tmp_path / "page.png"
    source.write_bytes(b"image")

    def runner(command, **kwargs):
        assert 0 < kwargs["timeout"] <= 1
        return SimpleNamespace(returncode=0, stdout="text")

    with read_deadline(time.monotonic() + 1):
        with read_deadline(time.monotonic() + 20):
            assert VisionReader(runner=runner, exe="fake-reader").read(source) == "text"
