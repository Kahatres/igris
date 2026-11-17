"""Web routes for each user's files and the owner's Core documents.

Route functions take the request handler ``h`` and the server context ``ctx``.
Depends on attachments and core_ingest (imported where they are used).
"""

import base64
import binascii
import re


def files(h, ctx, path, data=None):
    if ctx.accounts is None:
        return h._error(404, "not found")
    from igris.attachments import FileError, FileLimit, FileMissing

    store, owner = ctx.contexts.files, h.login.user.id
    try:
        if data is None:
            if path == "/api/files":
                return h._json(200, store.list(owner))
            if path == "/api/files/events":
                return h._json(200, {"events": store.events(owner)})
            match = re.fullmatch(r"/api/files/([a-f0-9]{32})/download", path)
            if match:
                name, content = store.download(owner, match[1])
                h.download_name = name
                return h._send(200, content, "application/octet-stream")
        elif path == "/api/files/upload":
            encoded = data.get("content")
            if not isinstance(encoded, str):
                raise FileError("File content must be base64")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise FileError("Invalid base64 file content") from None
            return h._json(201, store.upload(owner, data.get("name"), content))
        elif path == "/api/files/delete":
            store.delete(owner, data.get("id"))
            return h._json(200, {"ok": True})
    except FileMissing as exc:
        return h._error(404, str(exc))
    except FileLimit as exc:
        return h._error(413, str(exc))
    except FileError as exc:
        return h._error(400, str(exc))
    return h._error(404, "not found")


def upload_limit(ctx, max_body: int) -> int:
    """Largest /api/files/upload body: the base64 of the biggest file plus the JSON."""
    return 4 * ((ctx.contexts.files.max_file + 2) // 3) + max_body


def _core_enabled(ctx) -> bool:
    return ctx.contexts is not None and getattr(ctx.contexts, "core", None) is not None


def core(h, ctx, path, data=None):
    if not _core_enabled(ctx):
        return h._error(404, "not found")
    from igris.attachments import FileMissing
    from igris.core_ingest import ingest_upload

    contexts = ctx.contexts
    store, owner = contexts.core, h._owner_id()
    query = h._query()
    try:
        if data is None:
            if path == "/api/core":
                return h._json(200, {"documents": store.list(owner)})
            if path == "/api/core/doc":
                identifier = h._text_field({"id": query.get("id", [None])[0]}, "id")
                return h._json(200, store.get(owner, identifier))
        elif path == "/api/core/page":
            identifier = h._text_field(data, "id")
            n, text = data.get("n"), data.get("text")
            if type(n) is not int or n < 1 or not isinstance(text, str):
                raise ValueError("n must be a positive integer and text must be text")
            store.set_page(owner, identifier, n, text)
            return h._json(200, {"ok": True})
        elif path == "/api/core/add":
            file_id = h._text_field(data, "file_id")
            name, content = contexts.files.download(owner, file_id)
            identifier = store.reserve_upload(owner, name, file_id)
            pending = store.get(owner, identifier)

            def read_image(path):
                if contexts.vision is None:
                    raise RuntimeError("Vision reader unavailable")
                return contexts.vision.read(path)

            def work():
                try:
                    ingest_upload(store, owner, name, content, read_image=read_image,
                                  make_card=contexts.make_card, doc_id=identifier)
                except KeyError:
                    # The owner may delete a pending document while a model reads it.
                    pass
                except Exception:
                    try:
                        store.complete_upload(owner, identifier, "failed", "", [],
                                              error="Dosya okunamadı. Yeniden ekleyin.")
                    except (KeyError, ValueError):
                        pass
            try:
                ctx.start_ingest(work)
            except Exception:
                store.complete_upload(owner, identifier, "failed", "", [],
                                      error="Okuma başlatılamadı. Dosyayı yeniden ekleyin.")
                return h._error(503, "Ingest could not be started")
            return h._json(202, pending)
    except (KeyError, FileMissing):
        return h._error(404, "Document or upload not found")
    except ValueError as exc:
        return h._error(400, str(exc))
    return h._error(404, "not found")


def delete_document(h, ctx):
    """DELETE /api/core/doc?id=..., after the caller checked host, origin and CSRF."""
    if not _core_enabled(ctx):
        return h._error(404, "not found")
    try:
        identifier = h._text_field({"id": h._query().get("id", [None])[0]}, "id")
        ctx.contexts.core.delete(h._owner_id(), identifier)
        return h._json(200, {"ok": True})
    except KeyError:
        return h._error(404, "Document not found")
    except ValueError as exc:
        return h._error(400, str(exc))
    except Exception as exc:
        return h._failure(exc)
