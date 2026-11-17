"""Helpers shared by the web tests: run a test server on a thread and stop it quickly,
and a fake request handler for calling route functions directly.

serve_forever's default poll_interval of 0.5 s makes every shutdown wait up to half
a second; a short interval keeps the suite fast.
"""
import threading
from types import SimpleNamespace

POLL_INTERVAL = 0.02


def start(server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": POLL_INTERVAL}, daemon=True)
    thread.start()
    return thread


def stop(server, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(5)


class FakeRequest:
    """Stands in for the request handler of the web route functions: records the
    answer instead of writing HTTP."""

    def __init__(self, path="/", *, headers=None, login=None, assistant=None):
        from igris.web_http import BaseHandler

        self.path, self.headers, self.login = path, dict(headers or {}), login
        self.assistant, self.user_lock = assistant, threading.RLock()
        self.server = SimpleNamespace(server_address=("127.0.0.1", 8770), agenda_sync=None)
        self.status = self.body = None
        self._text_field = BaseHandler._text_field
        self._path = lambda: BaseHandler._path(self)
        self._query = lambda: BaseHandler._query(self)
        self._owner_id = lambda: BaseHandler._owner_id(self)
        self._is_admin = lambda: BaseHandler._is_admin(self)

    def _json(self, status, payload):
        self.status, self.body = status, payload

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _send(self, status, body, ctype, **kwargs):
        self.status, self.body = status, body

    def _failure(self, exc):
        self._error(500, str(exc))


def login(user_id="a" * 32, role="admin"):
    """A signed-in session as accounts.session returns it."""
    user = SimpleNamespace(id=user_id, role=role, public=lambda: {"id": user_id, "role": role})
    return SimpleNamespace(user=user, csrf="csrf-token", conversation="default", persona="plain")
