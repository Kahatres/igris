"""SQLite accounts and revocable browser sessions, using only the standard library."""

from contextlib import closing
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import time


SESSION_SECONDS = 12 * 60 * 60
PASSWORD_ROUNDS = 600_000
# the regular (non-owner) account created by --setup-users; IGRIS_GUEST_USERNAME overrides it
DEFAULT_USERNAME = "guest"
_USERNAME_RE = re.compile(r"[a-z0-9_-]{1,32}")


class AccountError(ValueError):
    """An account operation was rejected."""


class LoginLimited(AccountError):
    """Too many failed login attempts occurred in the current window."""


@dataclass(frozen=True)
class User:
    id: str
    username: str
    display_name: str
    role: str

    def public(self) -> dict:
        return dict(id=self.id, username=self.username,
                    display_name=self.display_name, role=self.role)


@dataclass(frozen=True)
class LoginSession:
    user: User
    csrf: str
    conversation: str
    persona: str


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_username(username: str) -> str:
    """The normalised regular-user name; raise AccountError when it is not usable."""
    name = username.strip().lower() if isinstance(username, str) else ""
    if not _USERNAME_RE.fullmatch(name) or name == "admin":
        raise AccountError("The user name must be 1-32 characters of a-z, 0-9, _ or - "
                           "and not 'admin'")
    return name


def validate_password(password: str) -> None:
    """Raise AccountError unless the password has 12 to 256 characters."""
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise AccountError("Password must contain 12 to 256 characters")


def _password_hash(password: str, salt: str | None = None, rounds: int | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    rounds = rounds or PASSWORD_ROUNDS
    value = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                              bytes.fromhex(salt), rounds).hex()
    return f"pbkdf2_sha256${rounds}${salt}${value}"


def _matches(password: str, stored: str | None) -> bool:
    # Unknown accounts do the same expensive work as known accounts.
    salt, rounds = "00" * 16, PASSWORD_ROUNDS
    if stored:
        _, count, salt, _ = stored.split("$")
        rounds = int(count)
    calculated = _password_hash(password, salt, rounds)
    return hmac.compare_digest(calculated, stored or "")


class AccountStore:
    """Open a short-lived SQLite connection per operation, safe across HTTP threads."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL, role TEXT NOT NULL
                        CHECK(role IN ('admin', 'user')),
                    password_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS login_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
                    csrf TEXT NOT NULL, expires REAL NOT NULL,
                    conversation TEXT NOT NULL DEFAULT 'default',
                    persona TEXT NOT NULL DEFAULT 'default'
                );
                CREATE TABLE IF NOT EXISTS login_limits (
                    bucket TEXT PRIMARY KEY, failures INTEGER NOT NULL, until REAL NOT NULL
                );
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def initialized(self) -> bool:
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def bootstrap(self, admin_password: str, user_password: str,
                  username: str = DEFAULT_USERNAME) -> None:
        """Atomically create the owner's admin account and one regular user account."""
        validate_password(admin_password)
        validate_password(user_password)
        username = validate_username(username)
        rows = [
            (secrets.token_hex(16), "admin", "Admin", "admin", _password_hash(admin_password)),
            (secrets.token_hex(16), username, username.capitalize(), "user",
             _password_hash(user_password)),
        ]
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise AccountError("Accounts already exist; use --reset-password USER")
            db.executemany("INSERT INTO users (id, username, display_name, role, password_hash) "
                           "VALUES (?, ?, ?, ?, ?)", rows)

    def login(self, username: str, password: str, address: str) -> str:
        """Issue an opaque session token, persisting only its digest."""
        if not isinstance(username, str) or not isinstance(password, str):
            raise AccountError("Invalid username or password")
        if len(username) > 64 or len(password) > 256:
            raise AccountError("Invalid username or password")
        username = username.strip().lower()
        buckets = [(_digest("user:" + username), 5), (_digest("ip:" + address), 20)]
        now = self.clock()
        error = None
        token = None
        # Serialize authentication and limit updates, including concurrent attempts.
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_limits WHERE until <= ?", (now,))
            db.execute("DELETE FROM login_sessions WHERE expires <= ?", (now,))
            for bucket, limit in buckets:
                row = db.execute("SELECT failures FROM login_limits WHERE bucket=?",
                                 (bucket,)).fetchone()
                if row and row["failures"] >= limit:
                    raise LoginLimited("Too many login attempts; try again later")
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            valid = _matches(password, row["password_hash"] if row else None)
            if not row or not valid or not row["enabled"]:
                for bucket, _ in buckets:
                    db.execute("INSERT INTO login_limits VALUES (?, 1, ?) "
                               "ON CONFLICT(bucket) DO UPDATE SET failures=failures+1",
                               (bucket, now + 900))
                error = AccountError("Invalid username or password")
            else:
                db.execute("DELETE FROM login_limits WHERE bucket=?", (buckets[0][0],))
                token = secrets.token_urlsafe(32)
                db.execute("INSERT INTO login_sessions (token_hash, user_id, csrf, expires) "
                           "VALUES (?, ?, ?, ?)",
                           (_digest(token), row["id"], secrets.token_urlsafe(32),
                            now + SESSION_SECONDS))
        if error:
            raise error
        return token

    def session(self, token: str | None) -> LoginSession | None:
        if not isinstance(token, str) or len(token) > 128:
            return None
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT users.*, s.csrf, s.conversation, s.persona FROM login_sessions s "
                "JOIN users ON users.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires>? AND users.enabled=1",
                (_digest(token), self.clock())).fetchone()
        if row is None:
            return None
        user = User(row["id"], row["username"], row["display_name"], row["role"])
        return LoginSession(user, row["csrf"], row["conversation"], row["persona"])

    def logout(self, token: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM login_sessions WHERE token_hash=?", (_digest(token),))

    def select(self, token: str, conversation: str, persona: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE login_sessions SET conversation=?, persona=? WHERE token_hash=?",
                       (conversation, persona, _digest(token)))

    def reset_password(self, username: str, password: str) -> None:
        """Local recovery invalidates all of the account's existing sessions."""
        validate_password(password)
        hashed = _password_hash(password)
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT id FROM users WHERE username=?",
                             (username.strip().lower(),)).fetchone()
            if row is None:
                raise AccountError("Unknown user")
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, row["id"]))
            db.execute("DELETE FROM login_sessions WHERE user_id=?", (row["id"],))
            db.execute("DELETE FROM login_limits WHERE bucket=?",
                       (_digest("user:" + username.strip().lower()),))

    def change_password(self, token: str, old_password: str, password: str) -> None:
        validate_password(password)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT u.* FROM users u JOIN login_sessions s ON s.user_id=u.id "
                "WHERE s.token_hash=? AND s.expires>? AND u.enabled=1",
                (_digest(token), self.clock())).fetchone()
            if (not isinstance(old_password, str) or len(old_password) > 256 or row is None
                    or not _matches(old_password, row["password_hash"])):
                raise AccountError("Invalid current password")
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (_password_hash(password), row["id"]))
            db.execute("DELETE FROM login_sessions WHERE user_id=?", (row["id"],))

    def users(self) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id, username, display_name, role, enabled "
                              "FROM users ORDER BY username").fetchall()
        return [dict(row) for row in rows]

    def user(self, user_id: str) -> User:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM users WHERE id=? AND enabled=1", (user_id,)).fetchone()
        if row is None:
            raise AccountError("User is unavailable")
        return User(row["id"], row["username"], row["display_name"], row["role"])
