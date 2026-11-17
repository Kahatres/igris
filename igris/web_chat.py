"""Web routes for the chat: state, history, asking, switching session or persona,
and the roadmap.

Route functions take the request handler ``h`` and the server context ``ctx``.
Depends on llm and roadmap (imported where they are used).
"""

from igris.web_http import MAX_HISTORY, MAX_TEXT


def state_payload(assistant) -> dict:
    from igris import __version__

    return {
        "name": assistant.settings.name,
        "version": __version__,
        "session": assistant.session_name(),
        "persona": assistant.persona_name(),
        "backends": list(assistant.settings.backends),
        "sessions": list(assistant.list_sessions()),
        "personas": list(assistant.list_personas()),
        "tools": list(assistant.tools.names()),
    }


def _state_with_user(h) -> dict:
    with h.user_lock:
        payload = state_payload(h.assistant)
        if h.login:
            payload["user"] = h.login.user.public()
    return payload


def state(h, ctx, path):
    return h._json(200, _state_with_user(h))


def history(h, ctx, path):
    with h.user_lock:
        count = len(h.assistant.memory)
        messages = [] if count == 0 else h.assistant.memory.history(limit=min(count, MAX_HISTORY))
    return h._json(200, {"messages": messages})


def roadmap(h, ctx, path):
    loader = ctx.roadmap_loader
    if loader is None:
        from igris.roadmap import load as loader
    return h._json(200, loader())


def ask(h, ctx, path, data):
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return h._error(400, "text must be a non-empty string")
    if len(text) > MAX_TEXT:
        return h._error(400, f"text exceeds {MAX_TEXT} characters")
    slow = data.get("slow", False)
    if not isinstance(slow, bool):
        return h._error(400, "slow must be a boolean")
    from igris.llm import BackendError

    try:
        with h.user_lock:
            if slow:
                reply = h.assistant.ask(text, slow=True)
            else:
                reply = h.assistant.ask(text)
    except BackendError as exc:
        return h._error(502, "Model unavailable" if ctx.accounts is not None else str(exc))
    except Exception as exc:
        return h._failure(exc)
    h._json(200, {"text": reply.text,
                  "backends": list(reply.backends),
                  "tool_calls": list(reply.tool_calls),
                  "job_id": getattr(reply, "job_id", None)})


def switch(h, ctx, path, data):
    """/api/session and /api/persona."""
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return h._error(400, "name must be a non-empty string")
    try:
        with h.user_lock:
            if path == "/api/session":
                h.assistant.use_session(name)
            else:
                h.assistant.set_persona(name)
            if ctx.accounts is not None:
                ctx.accounts.select(h.token, h.assistant.session_name(),
                                    h.assistant.persona_name())
    except Exception as exc:
        return h._error(400, str(exc))
    try:
        payload = _state_with_user(h)
    except Exception as exc:
        return h._failure(exc)
    h._json(200, payload)
