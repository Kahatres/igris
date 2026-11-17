import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "set_keys", Path(__file__).resolve().parents[1] / "tools" / "set_keys.py")
set_keys = importlib.util.module_from_spec(spec)
spec.loader.exec_module(set_keys)


class FakeKeyring:
    def __init__(self, **stored):
        self.stored = dict(stored)

    def get_password(self, service, name):
        assert service == "igris"
        return self.stored.get(name)

    def set_password(self, service, name, value):
        assert service == "igris"
        self.stored[name] = value


def run(argv, kr, answers):
    lines, replies = [], iter(answers)
    code = set_keys.main(argv, keyring_module=kr, ask=lambda prompt: next(replies),
                         out=lines.append)
    return code, "\n".join(lines)


def test_only_requested_provider_is_asked_and_saved():
    kr = FakeKeyring()
    code, text = run(["groq"], kr, ["gsk_abc"])
    assert code == 0 and kr.stored == {"GROQ_API_KEY": "gsk_abc"}
    assert "gsk_abc" not in text


def test_empty_answer_skips_and_keeps_existing_key():
    kr = FakeKeyring(GROQ_API_KEY="old")
    code, text = run(["groq"], kr, [""])
    assert code == 0 and kr.stored["GROQ_API_KEY"] == "old" and "1 skipped" in text


def test_cloudflare_asks_account_id_then_token():
    kr = FakeKeyring()
    run(["cloudflare"], kr, ["acct123", "tok456"])
    assert kr.stored == {"CLOUDFLARE_ACCOUNT_ID": "acct123", "CLOUDFLARE_API_TOKEN": "tok456"}


def test_whitespace_inside_a_key_is_rejected():
    kr = FakeKeyring()
    run(["groq"], kr, ["two words"])
    assert kr.stored == {}


def test_status_changes_nothing_and_never_prints_values():
    kr = FakeKeyring(GROQ_API_KEY="secret-value")
    code, text = run(["--status"], kr, [])
    assert code == 0 and "stored" in text and "missing" in text
    assert "secret-value" not in text and kr.stored == {"GROQ_API_KEY": "secret-value"}


def test_unknown_provider():
    code, text = run(["nope"], FakeKeyring(), [])
    assert code == 2 and "unknown provider" in text


def test_missing_keyring_is_explained(monkeypatch):
    monkeypatch.setattr(set_keys, "_keyring", lambda: None)
    lines = []
    assert set_keys.main([], out=lines.append) == 1
    assert "pip install keyring" in lines[0]


def test_ctrl_c_keeps_what_was_saved():
    kr = FakeKeyring()
    replies = iter(["k1"])

    def ask(prompt):
        try:
            return next(replies)
        except StopIteration:
            raise KeyboardInterrupt

    code = set_keys.main(["gemini", "groq"], keyring_module=kr, ask=ask, out=lambda s: None)
    assert code == 130 and kr.stored == {"GEMINI_API_KEY": "k1"}


def test_gmail_asks_client_id_then_secret():
    kr = FakeKeyring()
    code, text = run(["gmail"], kr, ["123-abc.apps.googleusercontent.com", "GOCSPX-xyz"])
    assert code == 0
    assert kr.stored == {"GMAIL_CLIENT_ID": "123-abc.apps.googleusercontent.com",
                         "GMAIL_CLIENT_SECRET": "GOCSPX-xyz"}
    assert "GOCSPX-xyz" not in text and "Desktop app" in text


def test_moodle_link_is_stored_and_never_echoed():
    kr = FakeKeyring()
    link = "https://moodle.example.lt/calendar/export_execute.php?userid=1&authtoken=abc"
    code, text = run(["moodle"], kr, [link])
    assert code == 0 and kr.stored == {"MOODLE_ICAL_URL": link} and "authtoken" not in text
