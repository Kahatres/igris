"""Account and session tests without model calls or network access."""

import sqlite3

import pytest

from igris.accounts import AccountError, AccountStore, LoginLimited, SESSION_SECONDS


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    # Keep repeated authentication tests fast; production work factor is tested separately.
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    store = AccountStore(tmp_path / "accounts.sqlite3", clock=lambda: 100)
    store.bootstrap("admin-test-password", "guest-test-password")
    return store


def test_bootstrap_roles_and_idempotence(accounts):
    users = accounts.users()
    assert [(u["username"], u["role"]) for u in users] == [
        ("admin", "admin"), ("guest", "user")]
    assert users[1]["display_name"] == "Guest"
    assert all("password_hash" not in u for u in users)
    with pytest.raises(AccountError, match="already exist"):
        accounts.bootstrap("another-password", "another-password")


def test_bootstrap_validation_is_atomic(tmp_path):
    store = AccountStore(tmp_path / "a.sqlite3")
    with pytest.raises(AccountError):
        store.bootstrap("long-enough-password", "short")
    assert not store.initialized()


def test_login_case_insensitive_and_only_hashes_persist(accounts):
    token = accounts.login(" GuEsT ", "guest-test-password", "client")
    session = accounts.session(token)
    assert session.user.username == "guest"
    assert session.conversation == session.persona == "default"
    with sqlite3.connect(accounts.path) as db:
        password = db.execute("SELECT password_hash FROM users WHERE username='guest'").fetchone()[0]
        saved_token = db.execute("SELECT token_hash FROM login_sessions").fetchone()[0]
    assert password.startswith("pbkdf2_sha256$") and "guest-test-password" not in password
    assert saved_token != token
    assert accounts.session("forged-token") is None


def test_logout_and_expiry(accounts):
    token = accounts.login("admin", "admin-test-password", "a")
    accounts.logout(token)
    assert accounts.session(token) is None
    token = accounts.login("admin", "admin-test-password", "a")
    accounts.clock = lambda: 100 + SESSION_SECONDS
    assert accounts.session(token) is None


def test_unknown_and_wrong_password_have_same_error(accounts):
    errors = []
    for username in ("admin", "missing"):
        with pytest.raises(AccountError) as error:
            accounts.login(username, "bad-password", "a")
        errors.append(str(error.value))
    assert errors[0] == errors[1]


def test_login_limits_persist_expire_and_cover_account(accounts):
    for _ in range(5):
        with pytest.raises(AccountError):
            accounts.login("admin", "wrong", "a")
    reopened = AccountStore(accounts.path, clock=lambda: 100)
    with pytest.raises(LoginLimited):
        reopened.login("admin", "admin-test-password", "different-client")
    accounts.clock = lambda: 1001
    assert accounts.session(accounts.login("admin", "admin-test-password", "a"))


def test_ip_limit_covers_unknown_usernames(accounts):
    for i in range(20):
        with pytest.raises(AccountError):
            accounts.login(f"unknown-{i}", "wrong", "same-ip")
    with pytest.raises(LoginLimited):
        accounts.login("guest", "guest-test-password", "same-ip")


def test_preferences_belong_to_login_not_other_browser(accounts):
    one = accounts.login("admin", "admin-test-password", "a")
    two = accounts.login("admin", "admin-test-password", "a")
    accounts.select(one, "project", "coder")
    assert accounts.session(one).conversation == "project"
    assert accounts.session(two).conversation == "default"


def test_password_reset_revokes_only_target_user(accounts):
    admin = accounts.login("admin", "admin-test-password", "a")
    other = accounts.login("guest", "guest-test-password", "a")
    accounts.reset_password("Guest", "new-test-password")
    assert accounts.session(other) is None
    assert accounts.session(admin)
    with pytest.raises(AccountError):
        accounts.login("guest", "guest-test-password", "a")
    assert accounts.login("guest", "new-test-password", "a")


def test_change_password_requires_current_and_revokes_all_sessions(accounts):
    token = accounts.login("admin", "admin-test-password", "a")
    second = accounts.login("admin", "admin-test-password", "a")
    with pytest.raises(AccountError):
        accounts.change_password(token, "wrong", "new-test-password")
    assert accounts.session(token)
    accounts.change_password(token, "admin-test-password", "new-test-password")
    assert accounts.session(token) is accounts.session(second) is None
    assert accounts.login("admin", "new-test-password", "a")


def test_disabled_account_cannot_login_or_keep_session(accounts):
    token = accounts.login("admin", "admin-test-password", "a")
    with sqlite3.connect(accounts.path) as db:
        db.execute("UPDATE users SET enabled=0 WHERE username='admin'")
    assert accounts.session(token) is None
    with pytest.raises(AccountError):
        accounts.login("admin", "admin-test-password", "a")


def test_production_password_work_factor():
    from igris.accounts import PASSWORD_ROUNDS

    assert PASSWORD_ROUNDS >= 600_000


def test_bootstrap_uses_a_configured_regular_user_name(tmp_path, monkeypatch):
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    store = AccountStore(tmp_path / "named.sqlite3")
    store.bootstrap("admin-test-password", "friend-test-password", " Friend_1 ")
    users = store.users()
    assert [(u["username"], u["display_name"], u["role"]) for u in users] == [
        ("admin", "Admin", "admin"), ("friend_1", "Friend_1", "user")]
    for bad in ("", "admin", "a b", "x" * 33, "ömer"):
        with pytest.raises(AccountError, match="user name"):
            AccountStore(tmp_path / f"bad{len(bad)}.sqlite3").bootstrap(
                "admin-test-password", "friend-test-password", bad)
