import builtins
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import struct
import threading
import time

import pytest

from igris import pagerender
from igris.vision_read import VisionReader


@pytest.fixture
def inline_native(monkeypatch):
    """Inject native failures locally while preserving the file/index protocol."""
    class Process:
        returncode = None

        def __init__(self, command, **kwargs):
            self.folder = Path(command[-1])

        def communicate(self, *args, **kwargs):
            if self.returncode is not None:
                return self.output, None
            with (self.folder / "request").open("rb") as request:
                header = json.loads(request.readline())
                content = request.read()
            try:
                worker = {"pdf": pagerender._pdf_worker, "photo": pagerender._photo_worker}[header["kind"]]
                result = worker(content, header["limits"], self.folder)
            except pagerender.RenderError as exc:
                result = {"error": str(exc)}
            self.returncode = 0
            self.output = json.dumps(result).encode()
            return self.output, None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(pagerender.subprocess, "Popen", Process)


@pytest.fixture
def mupdf():
    return pytest.importorskip("pymupdf")


@pytest.fixture(autouse=True)
def subscription_env(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def image():
    return pytest.importorskip("PIL.Image")


def make_pdf(mupdf, texts):
    with mupdf.open() as pdf:
        for text in texts:
            page = pdf.new_page(width=595, height=842)
            if text:
                page.insert_text((72, 72), text)
            else:
                page.draw_line((20, 20), (200, 200))
        return pdf.tobytes()


def png_size(content):
    assert content[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", content[16:24])


def test_optional_imports_allow_standard_library_only(monkeypatch):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "pymupdf" or name == "PIL" or name.startswith("PIL."):
            raise ImportError("optional dependency disabled")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    spec = importlib.util.spec_from_file_location("isolated_pagerender", pagerender.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not module.pdf_available() and not module.photo_available()
    assert module.prepare_photo(b"original photo") == b"original photo"
    with pytest.raises(module.RenderError, match="unavailable"):
        module.read_pdf(b"pdf", lambda path: "text")


@pytest.mark.parametrize("size,cap,pixels", [((595, 842), 2000, 4_000_000),
                                           ((2000, 3000), 2000, 500_000),
                                           ((72, 144), 2000, 4_000_000)])
def test_render_dimensions_dpi_and_allocation_caps(mupdf, size, cap, pixels):
    with mupdf.open() as pdf:
        page = pdf.new_page(width=size[0], height=size[1])
        content = pagerender.render_page(page, long_side=cap, max_pixels=pixels)
    width, height = png_size(content)
    assert max(width, height) <= cap and width * height <= pixels
    if size == (72, 144):
        assert (width, height) == (150, 300)


def test_pdf_uses_text_only_where_usable_and_separate_images(mupdf):
    seen = []

    def read(path):
        seen.append(path)
        assert path.suffix == ".png"
        assert list(path.parent.iterdir()) == [path]
        assert max(png_size(path.read_bytes())) <= 2000
        return "Transcribed handwriting"

    pages = pagerender.read_pdf(make_pdf(mupdf, ["Typed", "", "Last", ""]), read)
    assert pages == [("Typed", False), ("Transcribed handwriting", True),
                     ("Last", False), ("Transcribed handwriting", True)]
    assert len(seen) == 2 and all(not path.exists() for path in seen)


def test_render_failure_is_local_and_not_retried_as_whole_pdf(mupdf, monkeypatch, inline_native):
    original = pagerender.render_page

    def render(page, **kwargs):
        if page.number == 1:
            raise RuntimeError("private path and parser details")
        return original(page, **kwargs)

    monkeypatch.setattr(pagerender, "render_page", render)
    pages = pagerender.read_pdf(make_pdf(mupdf, ["", "", ""]), lambda path: "read")
    assert pages == [("read", True), ("[Page 2 unread: page rendering failed.]", True),
                     ("read", True)]


@pytest.mark.parametrize("concurrency", [1, 3])
def test_concurrency_is_bounded_configurable_and_ordered(mupdf, concurrency):
    lock = threading.Lock()
    release = threading.Event()
    reached = threading.Event()
    active = peak = calls = 0
    results = []

    def read(path):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == concurrency:
                reached.set()
        assert release.wait(3)
        with lock:
            active -= 1
        return "scan"

    def ingest():
        results.extend(pagerender.read_pdf(content, read, concurrency=concurrency))

    content = make_pdf(mupdf, [""] * 6)
    thread = threading.Thread(target=ingest)
    thread.start()
    try:
        assert reached.wait(3)
        assert peak == concurrency and calls == concurrency
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and len(results) == 6
    assert peak == concurrency and results == [("scan", True)] * 6


def test_one_failed_page_preserves_other_pages(mupdf):
    lock = threading.Lock()
    calls = 0

    def read(path):
        nonlocal calls
        with lock:
            calls += 1
            number = calls
        if number == 2:
            raise RuntimeError("private model output")
        return "good"

    pages = pagerender.read_pdf(make_pdf(mupdf, [""] * 4), read, concurrency=1)
    assert [text for text, _ in pages] == ["good", "[Page 2 unread: vision read failed.]",
                                          "good", "good"]


def test_per_page_deadline_reaches_real_runner_without_cli(mupdf):
    from types import SimpleNamespace

    timeouts = []

    def runner(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        assert list(Path(kwargs["cwd"]).iterdir())[0].suffix == ".png"
        return SimpleNamespace(returncode=0, stdout="read")

    reader = VisionReader(runner=runner, exe="fake-reader", timeout=120)
    # Match web.py's path-only closure, rather than passing a bound method.
    pages = pagerender.read_pdf(make_pdf(mupdf, ["", ""]), lambda path: reader.read(path),
                                page_timeout=2, total_timeout=5)
    assert pages == [("read", True)] * 2
    assert len(timeouts) == 2 and all(0 < timeout <= 2 for timeout in timeouts)


def test_cli_timeout_only_marks_that_page_and_continues(mupdf):
    import subprocess
    from types import SimpleNamespace

    calls = 0

    def runner(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="read")

    reader = VisionReader(runner=runner, exe="fake-reader")
    pages = pagerender.read_pdf(make_pdf(mupdf, [""] * 3), reader.read, concurrency=1,
                                page_timeout=1)
    assert pages == [("read", True), ("[Page 2 unread: page read timed out.]", True),
                     ("read", True)]
    assert calls == 3


def test_time_budget_does_not_wait_for_stuck_reader_or_delete_its_file(mupdf):
    release = threading.Event()
    finished = threading.Event()
    paths = []

    def read(path):
        paths.append(path)
        try:
            assert release.wait(3)
            assert path.exists()
            return "late result must be ignored"
        finally:
            finished.set()

    started = time.monotonic()
    try:
        pages = pagerender.read_pdf(make_pdf(mupdf, [""] * 4), read, concurrency=1,
                                    page_timeout=0.05, total_timeout=0.8)
        assert time.monotonic() - started < 2
        assert "timed out" in pages[0][0]
        assert all("time budget exceeded" in text for text, _ in pages[1:])
        assert len(paths) == 1 and paths[0].exists()
    finally:
        release.set()
        assert finished.wait(3)
    # The worker removes its directory before publishing a result.
    for _ in range(100):
        if not paths[0].exists():
            break
        time.sleep(0.001)
    assert not paths[0].exists()


@pytest.mark.parametrize("kwargs,reason", [({"max_pages": 2}, "page limit"),
                                          ({"max_bytes": 10}, "byte limit")])
def test_document_limits_reject_without_reading(mupdf, kwargs, reason):
    with pytest.raises(pagerender.RenderError, match=reason):
        pagerender.read_pdf(make_pdf(mupdf, [""] * 3),
                             lambda path: pytest.fail("must reject"), **kwargs)


def test_encrypted_pdf_is_rejected(mupdf):
    with mupdf.open(stream=make_pdf(mupdf, [""]), filetype="pdf") as document:
        content = document.tobytes(encryption=mupdf.PDF_ENCRYPT_AES_256,
                                   owner_pw="owner", user_pw="reader")
    with pytest.raises(pagerender.RenderError, match="encrypted"):
        pagerender.read_pdf(content, lambda path: pytest.fail("encrypted input"))


@pytest.mark.parametrize("kwargs", [{"concurrency": 0}, {"concurrency": 1.5},
                                     {"total_timeout": float("inf")}, {"max_pixels": -1},
                                     {"max_pixels": 0.1}, {"long_side": 0.5}])
def test_invalid_limits_fail_before_parsing(mupdf, kwargs):
    with pytest.raises(ValueError):
        pagerender.read_pdf(b"invalid", lambda path: "read", **kwargs)


def test_photo_rotation_cap_no_upscale_and_metadata_stripping(image):
    for size, expected in [((3000, 1500), (1000, 2000)), ((300, 150), (150, 300))]:
        output = io.BytesIO()
        exif = image.Exif()
        exif[274] = 6
        exif[270] = "private metadata"
        with image.new("RGB", size, "white") as photo:
            photo.save(output, format="JPEG", exif=exif)
        prepared = pagerender.prepare_photo(output.getvalue())
        with image.open(io.BytesIO(prepared)) as photo:
            assert photo.size == expected
            assert not photo.getexif() and not photo.info


def test_photo_pixel_limit_is_checked_before_decoding(image, monkeypatch):
    output = io.BytesIO()
    with image.new("RGB", (50, 50)) as photo:
        photo.save(output, format="PNG")
    monkeypatch.setattr(image.Image, "load", lambda *args, **kwargs: pytest.fail("no decode"))
    with pytest.raises(pagerender.RenderError, match="pixel limit"):
        pagerender.prepare_photo(output.getvalue(), max_pixels=2000)


def test_invalid_photo_fails_without_forwarding_original(image):
    with pytest.raises(pagerender.RenderError, match="decoded"):
        pagerender.read_photo(b"invalid", lambda path: pytest.fail("unsafe input"))


def test_unusable_text_layer_renders_instead(mupdf, monkeypatch, inline_native):
    monkeypatch.setattr(pagerender._mupdf.Page, "get_text", lambda *args, **kwargs: "\ufffd\ufffd")
    assert pagerender.read_pdf(make_pdf(mupdf, ["Typed"]), lambda path: "read") == [("read", True)]


def test_page_number_over_scan_does_not_replace_handwriting(mupdf, image):
    output = io.BytesIO()
    with image.new("RGB", (100, 100), "white") as scan:
        scan.save(output, format="PNG")
    with mupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_image(page.rect, stream=output.getvalue())
        page.insert_text((72, 72), "1")
        content = pdf.tobytes()
    assert pagerender.read_pdf(content, lambda path: "Handwritten notes") == [
        ("Handwritten notes", True)]


def test_total_transcription_text_is_bounded(mupdf):
    pages = pagerender.read_pdf(make_pdf(mupdf, [""] * 2), lambda path: "x" * 60,
                                max_chars=100)
    assert pages == [("x" * 60, True),
                     ("[Page 2 unread: document text limit exceeded.]", True)]


def stalled_child(monkeypatch, code):
    original = subprocess.Popen
    children, folders, killed = [], [], []

    def start(command, **kwargs):
        folders.append(Path(command[-1]))
        process = original([*command[:3], code, *command[4:]], **kwargs)
        children.append(process)
        kill = process.kill

        def terminate():
            killed.append(process.pid)
            kill()

        monkeypatch.setattr(process, "kill", terminate)
        return process

    monkeypatch.setattr(pagerender.subprocess, "Popen", start)
    return children, folders, killed


def test_native_render_is_terminated_at_total_budget(mupdf, monkeypatch):
    code = """import json, sys, time
from pathlib import Path
folder = Path(sys.argv[2])
with (folder / 'index.jsonl').open('w') as output:
    output.write(json.dumps({'count': 3}) + '\\n')
    output.write(json.dumps(['Typed first page', None, '', False]) + '\\n')
time.sleep(30)
"""
    children, folders, killed = stalled_child(monkeypatch, code)
    started = time.monotonic()
    pages = pagerender.read_pdf(b"unused", lambda path: pytest.fail("typed input"),
                                total_timeout=0.8)
    assert time.monotonic() - started < 2
    assert pages[0] == ("Typed first page", False)
    assert all("time budget exceeded" in text for text, _ in pages[1:])
    assert killed == [children[0].pid] and children[0].poll() is not None
    assert all(not folder.exists() for folder in folders)


@pytest.mark.parametrize("kind", ["pdf", "photo"])
def test_native_startup_and_photo_decode_obey_total_budget(mupdf, image, monkeypatch, kind):
    children, folders, killed = stalled_child(monkeypatch, "import time; time.sleep(30)")
    read = pagerender.read_pdf if kind == "pdf" else pagerender.read_photo
    started = time.monotonic()
    with pytest.raises(pagerender.RenderError, match="time budget"):
        read(b"unused", lambda path: pytest.fail("stuck native worker"), total_timeout=0.05)
    assert time.monotonic() - started < 1
    assert killed == [children[0].pid] and children[0].poll() is not None
    assert all(not folder.exists() for folder in folders)


def test_real_render_uses_isolated_child_and_private_files(mupdf, monkeypatch):
    original = subprocess.Popen
    commands, folders, children = [], [], []

    def start(command, **kwargs):
        commands.append(command)
        assert command[:3] == [sys.executable, "-I", "-c"]
        assert command[4] == str(Path(pagerender.__file__).resolve().parent.parent)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["creationflags"] == (subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        folder = Path(command[5])
        folders.append(folder)
        with (folder / "request").open("rb") as request:
            assert json.loads(request.readline())["kind"] == "pdf"
            assert request.read().startswith(b"%PDF")
        # Both Python and native diagnostics must stay off the JSON pipe.
        command = list(command)
        command[3] += "; print('diagnostic'); os.write(1, b'native diagnostic')"
        process = original(command, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(pagerender.subprocess, "Popen", start)
    assert pagerender.read_pdf(make_pdf(mupdf, ["Typed", ""]), lambda path: "scan") == [
        ("Typed", False), ("scan", True)]
    assert len(commands) == 1 and children[0].poll() == 0
    assert all(not folder.exists() for folder in folders)
    assert "multiprocessing" not in Path(pagerender.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("launch", ["script", "stdin"])
def test_real_render_never_reimports_unguarded_main(mupdf, tmp_path, launch):
    root = str(Path(pagerender.__file__).resolve().parent.parent)
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(make_pdf(mupdf, ["Typed", ""]))
    marker = tmp_path / "main-starts"
    source = f"""import json, sys
from pathlib import Path
sys.path.insert(0, {root!r})
marker = Path({str(marker)!r})
with marker.open('a') as output:
    output.write('started\\n')
assert marker.read_text().count('started') == 1, 'main was re-imported'
from igris.pagerender import read_pdf
print(json.dumps(read_pdf(Path({str(pdf)!r}).read_bytes(), lambda path: 'scan')))
"""
    if launch == "script":
        script = tmp_path / "unguarded.py"
        script.write_text(source, encoding="utf-8")
        command, data = [sys.executable, str(script)], ""
    else:
        command, data = [sys.executable, "-"], source
    completed = subprocess.run(command, input=data, text=True, capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [["Typed", False], ["scan", True]]
    assert marker.read_text().count("started") == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI interpreter")
def test_real_render_supports_pythonw(mupdf, monkeypatch):
    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.is_file():
        pytest.skip("pythonw.exe is not installed")
    monkeypatch.setattr(pagerender.sys, "executable", str(executable))
    assert pagerender.read_pdf(make_pdf(mupdf, ["Typed", ""]), lambda path: "scan") == [
        ("Typed", False), ("scan", True)]


@pytest.mark.parametrize("output,code", [(b"not json", 0), (b"{}", 0),
                                        (b'{"kind":"ready","value":2,"pages":[]}', 0),
                                        (b"{}", 1)])
def test_child_failures_are_fixed_errors_and_clean_up(mupdf, monkeypatch, output, code):
    folders = []

    class FailedProcess:
        returncode = code

        def __init__(self, command, **kwargs):
            folders.append(Path(command[-1]))

        def communicate(self, *args, **kwargs):
            return output, None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(pagerender.subprocess, "Popen", FailedProcess)
    with pytest.raises(pagerender.RenderError, match="rendering worker stopped"):
        pagerender.read_pdf(b"unused", lambda path: pytest.fail("failed child"))
    assert all(not folder.exists() for folder in folders)
