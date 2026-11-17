"""Optional bounded PDF rendering, photo preparation and concurrent transcription.

Only transcription runs concurrently. Each upload uses an isolated native worker,
so a parser or renderer can be terminated at the document deadline. Limits are
per-call arguments; importing this module requires no third-party package.
"""

from __future__ import annotations

import io
import math
import json
import os
import subprocess
import sys
from pathlib import Path
from queue import Empty, Queue
import tempfile
import threading
import time

from igris.vision_read import ReadFailed, _REFUSAL, read_deadline

try:
    import pymupdf as _mupdf
except ImportError:
    _mupdf = None

try:
    from PIL import Image as _Image, ImageOps as _ImageOps
except ImportError:
    _Image = _ImageOps = None


class RenderError(ValueError):
    """A fixed reason for rejecting an unsafe or unsupported upload."""


def pdf_available():
    return _mupdf is not None


def photo_available():
    return _Image is not None


def _positive(**values):
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be positive and finite")


def _pixel_limits(long_side, max_pixels):
    _positive(long_side=long_side, max_pixels=max_pixels)
    if type(long_side) is not int or type(max_pixels) is not int:
        raise ValueError("long_side and max_pixels must be integers")


def _usable(text, page):
    """Reject empty, replacement-only or unreadable text layers."""
    meaningful = [c for c in text if not c.isspace()]
    if not meaningful or not any(c.isalnum() for c in meaningful) or sum(
            c == "\ufffd" or (not c.isprintable()) for c in meaningful) >= len(meaningful) / 5:
        return False
    if len(meaningful) < 40:
        # A page number over a full-page scan is not a usable text layer.
        area = page.rect.width * page.rect.height
        for image in page.get_image_info():
            box = _mupdf.Rect(image["bbox"]) & page.rect
            if box.width * box.height > area / 2:
                return False
    return True


def _transcription(text):
    if not isinstance(text, str) or not text.strip() \
            or _REFUSAL.search(text.lstrip("#* \n")):
        raise ReadFailed("unreadable")
    return text.strip()


def unread_page(number, reason):
    return f"[Page {number} unread: {reason}.]"


def prepare_photo(content, *, long_side=2000, max_pixels=40_000_000):
    """Return a metadata-free PNG, or the original bytes without Pillow.

    Check source dimensions before loading pixels. The cap applies to the source
    allocation as well as the output; a phone photo is never enlarged.
    """
    if _Image is None:
        return content
    _pixel_limits(long_side, max_pixels)
    try:
        with _Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > max_pixels:
                raise RenderError("photo pixel limit exceeded")
            with _ImageOps.exif_transpose(image) as rotated:
                rotated.thumbnail((int(long_side), int(long_side)), _Image.Resampling.LANCZOS)
                # A fresh image strips EXIF, GPS, XMP and other upload metadata.
                with _Image.new("RGB", rotated.size, "white") as clean:
                    if "A" in rotated.getbands():
                        clean.paste(rotated, mask=rotated.getchannel("A"))
                    else:
                        clean.paste(rotated)
                    output = io.BytesIO()
                    clean.save(output, format="PNG")
                    return output.getvalue()
    except RenderError:
        raise
    except Exception:
        raise RenderError("photo could not be decoded") from None


def render_page(page, *, dpi=150, long_side=2000, max_pixels=4_000_000):
    """Render to RGB PNG with dimensions bounded before allocating a pixmap."""
    if _mupdf is None:
        raise RenderError("PDF rendering is unavailable")
    _positive(dpi=dpi)
    _pixel_limits(long_side, max_pixels)
    width, height = page.rect.width, page.rect.height
    _positive(width=width, height=height)
    scale = min(dpi / 72, long_side / max(width, height),
                math.sqrt(max_pixels / (width * height)))
    # MuPDF rounds the transformed rectangle outwards. Reduce the scale when
    # fractional page boxes would exceed either allocation limit.
    while True:
        box = (page.rect * _mupdf.Matrix(scale, scale)).irect
        if max(box.width, box.height) <= long_side and box.width * box.height <= max_pixels:
            break
        scale *= 0.99
    if min(box.width, box.height) < 1:
        raise RenderError("invalid page dimensions")
    pixmap = page.get_pixmap(matrix=_mupdf.Matrix(scale, scale),
                             colorspace=_mupdf.csRGB, alpha=False)
    if max(pixmap.width, pixmap.height) > long_side \
            or pixmap.width * pixmap.height > max_pixels:
        raise RenderError("page pixel limit exceeded")
    return pixmap.tobytes("png")


def _pdf_worker(content, limits, folder):
    """Own the native document; publish only text and private image filenames."""
    with _mupdf.open(stream=content, filetype="pdf") as document:
        if document.needs_pass:
            raise RenderError("encrypted PDF")
        if not 0 < document.page_count <= limits["max_pages"]:
            raise RenderError("PDF page limit exceeded")
        remaining = limits["max_chars"]
        pages = []
        # Preserve completed pages if a later native render hangs or crashes.
        # Normal completion still publishes the final JSON index on stdout.
        progress = folder / "index.jsonl"
        progress.write_text(json.dumps({"count": document.page_count}) + "\n", encoding="utf-8")
        for index in range(document.page_count):
            try:
                page = document[index]
                text = page.get_text("text").strip()
                if len(text) > remaining:
                    source = (unread_page(index + 1, "document text limit exceeded"),
                              None, "", True)
                elif _usable(text, page):
                    remaining -= len(text)
                    source = (text, None, "", False)
                else:
                    image = render_page(page, dpi=limits["dpi"],
                                        long_side=limits["long_side"],
                                        max_pixels=limits["max_pixels"])
                    name = f"page-{index + 1}.png"
                    (folder / name).write_bytes(image)
                    source = (None, name, ".png", True)
            except Exception:
                source = (unread_page(index + 1, "page rendering failed"), None, "", True)
            pages.append(source)
            with progress.open("a", encoding="utf-8") as index_file:
                index_file.write(json.dumps(source) + "\n")
        return {"kind": "ready", "value": len(pages), "pages": pages}


def _photo_worker(content, limits, folder):
    image = prepare_photo(content, **limits)
    (folder / "photo.png").write_bytes(image)
    return {"kind": "photo", "value": "photo.png"}


def _child_entry(output_fd):
    """Read a JSON header and upload bytes from the parent's private directory."""
    folder = Path(sys.argv[2])
    description = "upload"
    try:
        with (folder / "request").open("rb") as request:
            header = json.loads(request.readline(16384))
            content = request.read()
        description = "PDF" if header["kind"] == "pdf" else "photo"
        target = {"pdf": _pdf_worker, "photo": _photo_worker}[header["kind"]]
        result = target(content, header["limits"], folder)
    except RenderError as exc:
        result = {"error": str(exc)}
    except Exception:
        result = {"error": f"{description} could not be decoded"}
    with os.fdopen(output_fd, "w", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=True)


class _NativeWorker:
    """Keep large inputs and rendered images off the subprocess pipes."""

    def __init__(self, target, content, limits, deadline):
        self.deadline = deadline
        self.directory = tempfile.TemporaryDirectory(prefix="igris-render-")
        self.folder = Path(self.directory.name)
        self.process = None
        self.max_pages = limits.get("max_pages", 1)
        self.sources = iter(())
        self.kind = "pdf" if target is _pdf_worker else "photo"
        bootstrap = ("import os,sys; output=os.dup(1); os.dup2(2,1); "
                     "sys.path.insert(0,sys.argv[1]); "
                     "from igris.pagerender import _child_entry; _child_entry(output)")
        command = [sys.executable, "-I", "-c", bootstrap,
                   str(Path(__file__).resolve().parent.parent), str(self.folder)]
        try:
            with (self.folder / "request").open("wb") as request:
                request.write(json.dumps({"kind": self.kind, "limits": limits}).encode("utf-8") + b"\n")
                request.write(content)
            self.process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception:
            self.close()
            raise RenderError("rendering worker stopped") from None

    def _image(self, name):
        # The index may only refer to a direct child of our private directory.
        if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
            raise RenderError("rendering worker stopped")
        path = self.folder / name
        image = path.read_bytes()
        path.unlink()
        return image

    def receive(self):
        remaining = self.deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.process.args, 0)
            stdout, _ = self.process.communicate(timeout=remaining)
            if self.process.returncode:
                raise ValueError("Child failed")
            result = json.loads(stdout)
            if "error" in result:
                raise RenderError(result["error"])
            kind, value = result["kind"], result["value"]
            if kind == "ready":
                if (type(value) is not int or not 0 < value <= self.max_pages
                        or len(result["pages"]) != value):
                    raise ValueError("Invalid page index")
                self.sources = iter(result["pages"])
            elif kind == "photo":
                value = self._image(value)
            else:
                raise ValueError("Invalid result kind")
            return kind, value
        except subprocess.TimeoutExpired:
            self._stop()
            return self._recover("document time budget exceeded")
        except RenderError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            return self._recover("page rendering failed")

    def _recover(self, reason):
        try:
            with (self.folder / "index.jsonl").open(encoding="utf-8") as progress:
                count = json.loads(progress.readline())["count"]
                if type(count) is not int or not 0 < count <= self.max_pages:
                    raise ValueError("Invalid page count")
                pages = []
                for line in progress:
                    try:
                        source = json.loads(line)
                    except ValueError:
                        break  # The child may have stopped during the last write.
                    if len(source) != 4 or len(pages) >= count:
                        raise ValueError("Invalid page index")
                    pages.append(source)
            pages.extend((unread_page(n + 1, reason), None, "", True)
                         for n in range(len(pages), count))
            self.sources = iter(pages)
            return "ready", count
        except (OSError, ValueError, KeyError, TypeError):
            raise RenderError(reason if "time budget" in reason else
                              "rendering worker stopped") from None

    def __iter__(self):
        return self

    def __next__(self):
        text, image, suffix, from_image = next(self.sources)
        if image is not None:
            image = self._image(image)
        return text, image, suffix, from_image

    def _stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.communicate()

    def close(self):
        self._stop()
        self.directory.cleanup()


def _read_pages(sources, count, read_image, *, concurrency, page_timeout, deadline,
                validate):
    """Keep at most concurrency live callbacks, and discard all late results.

    A callback owns its temporary file until it exits. Timed-out callbacks keep
    their slot: a misbehaving injected reader cannot create unlimited threads.
    VisionReader additionally enforces the deadline on its subprocess itself.
    """
    results = [(unread_page(n + 1, "document time budget exceeded"), True)
               for n in range(count)]
    completed = Queue()
    active = {}
    next_page = 0

    def read(number, content, suffix, expires):
        try:
            with tempfile.TemporaryDirectory(prefix="igris-page-") as folder:
                path = Path(folder) / ("document" + suffix)
                path.write_bytes(content)
                with read_deadline(expires):
                    text = validate(read_image(path))
                result = text if time.monotonic() <= expires else unread_page(
                    number + 1, "page read timed out")
        except Exception as exc:
            reason = "page read timed out" if isinstance(exc, ReadFailed) \
                and exc.reason == "timeout" else "vision read failed"
            result = unread_page(number + 1, reason)
        completed.put((number, result, time.monotonic()))

    def collect(item):
        number, result, finished = item
        expires = active.pop(number)
        results[number] = (result if finished <= expires else unread_page(
            number + 1, "page read timed out"), True)

    while next_page < count or active:
        if time.monotonic() >= deadline:
            break
        while next_page < count and len(active) < concurrency:
            number = next_page
            next_page += 1
            if time.monotonic() >= deadline:
                break
            try:
                text, image, suffix, from_image = next(sources)
            except Exception:
                if time.monotonic() >= deadline:
                    break
                results[number] = (unread_page(number + 1, "page rendering failed"), True)
                continue
            if time.monotonic() >= deadline:
                break
            if image is None:
                results[number] = (text, from_image)
                continue
            expires = min(deadline, time.monotonic() + page_timeout)
            active[number] = expires
            threading.Thread(target=read, args=(number, image, suffix, expires),
                             daemon=True, name="igris-page-read").start()
        if not active:
            continue
        now = time.monotonic()
        for number, expires in active.items():
            if expires <= now:
                results[number] = (unread_page(number + 1, "page read timed out"), True)
        # Expired callbacks still own their slots. Poll only for their completion
        # or the total deadline; never resubmit their work.
        wake = min([deadline] + [expires for expires in active.values() if expires > now])
        try:
            collect(completed.get(timeout=max(0, wake - now)))
        except Empty:
            continue
    # Native rendering may have occupied the coordinator until the deadline;
    # retain reads that already finished successfully while it was rendering.
    while True:
        try:
            collect(completed.get_nowait())
        except Empty:
            break
    return results


def read_pdf(content, read_image, *, concurrency=3, page_timeout=120, total_timeout=600,
             max_pages=300, max_pixels=4_000_000, long_side=2000, dpi=150,
             max_bytes=50 * 1024 * 1024, max_chars=2_000_000, validate=_transcription):
    """Return ordered (text, from_image) pages; isolate individual page failures.

    Call only when pdf_available() is true. Limits include worker startup, parsing,
    rendering and transcription. The native worker is terminated on return.
    The card summarizer belongs to the caller and is outside this read budget.
    """
    if _mupdf is None:
        raise RenderError("PDF rendering is unavailable")
    _positive(concurrency=concurrency, page_timeout=page_timeout, total_timeout=total_timeout,
              max_pages=max_pages, max_pixels=max_pixels, long_side=long_side, dpi=dpi,
              max_bytes=max_bytes, max_chars=max_chars)
    _pixel_limits(long_side, max_pixels)
    if any(type(value) is not int for value in (concurrency, max_pages, max_chars)):
        raise ValueError("concurrency, max_pages and max_chars must be integers")
    if len(content) > max_bytes:
        raise RenderError("PDF byte limit exceeded")
    deadline = time.monotonic() + total_timeout
    limits = dict(max_pages=max_pages, max_chars=max_chars, dpi=dpi,
                  long_side=long_side, max_pixels=max_pixels)
    worker = _NativeWorker(_pdf_worker, content, limits, deadline)
    try:
        kind, count = worker.receive()
        if kind != "ready":
            raise RenderError("PDF rendering worker stopped")
        if time.monotonic() >= deadline:
            pages = [(text, from_image) if image is None else
                     (unread_page(n + 1, "document time budget exceeded"), True)
                     for n, (text, image, _, from_image) in enumerate(worker.sources)]
        else:
            pages = _read_pages(worker, count, read_image, concurrency=concurrency,
                                page_timeout=page_timeout, deadline=deadline, validate=validate)
        remaining = max_chars
        for index, (text, from_image) in enumerate(pages):
            if len(text) > remaining:
                pages[index] = (unread_page(index + 1, "document text limit exceeded"), from_image)
            else:
                remaining -= len(text)
        return pages
    except RenderError:
        raise
    except Exception:
        raise RenderError("PDF could not be decoded") from None
    finally:
        worker.close()


def read_photo(content, read_image, *, page_timeout=120, total_timeout=120,
               long_side=2000, max_pixels=40_000_000, validate=_transcription):
    """Prepare and read a single photo under a local deadline."""
    _positive(page_timeout=page_timeout, total_timeout=total_timeout)
    deadline = time.monotonic() + total_timeout
    _pixel_limits(long_side, max_pixels)
    worker = _NativeWorker(_photo_worker, content,
                           dict(long_side=long_side, max_pixels=max_pixels), deadline)
    try:
        kind, image = worker.receive()
        if kind != "photo":
            raise RenderError("photo could not be decoded")
    finally:
        worker.close()
    return _read_pages(iter([(None, image, ".png", True)]), 1, read_image,
                       concurrency=1, page_timeout=page_timeout, deadline=deadline,
                       validate=validate)[0][0]
