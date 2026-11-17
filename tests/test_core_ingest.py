import io
from pathlib import Path
import zipfile

import pytest

from igris import core_ingest, pagerender
from igris.core_ingest import CARD_PROMPT, PAGE_CHARS, ingest_upload, save_research
from igris.core_store import CoreStore


@pytest.fixture(autouse=True)
def legacy_optional_dependencies(monkeypatch):
    """Exercise the original contract independently of installed extras."""
    monkeypatch.setattr(pagerender, "_mupdf", None)
    monkeypatch.setattr(pagerender, "_Image", None)


@pytest.fixture
def optimized(monkeypatch):
    mupdf = pytest.importorskip("pymupdf")
    image = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(pagerender, "_mupdf", mupdf)
    monkeypatch.setattr(pagerender, "_Image", image)
    return mupdf, image


def no_image(path):
    pytest.fail("text documents must not call vision")


def docx(text):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="w"><w:body>'
                         f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
                         '</w:body></w:document>')
    return output.getvalue()


def pdf_with_pages(*texts):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [" + b" ".join(
                   f"{3 + 2 * i} 0 R".encode() for i in range(len(texts))) +
               b"] /Count " + str(len(texts)).encode() + b" >>"]
    for i, text in enumerate(texts):
        objects.append(f"<< /Type /Page /Parent 2 0 R /Contents {4 + 2 * i} 0 R >>".encode())
        content = b"BT (" + text.encode() + b") Tj ET" if text else b""
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" +
                       content + b"\nendstream")
    return b"%PDF-1.7\n" + b"\n".join(
        f"{n} 0 obj\n".encode() + obj + b"\nendobj" for n, obj in enumerate(objects, 1)
    ) + b"\ntrailer\n<< /Root 1 0 R >>\n%%EOF"


@pytest.mark.parametrize("name,content", [("notes.txt", b"Notes on vectors"),
                                          ("notes.docx", docx("Notes on vectors"))])
def test_text_upload_uses_extractor_and_card(tmp_path, name, content):
    store = CoreStore(tmp_path / "core.db")
    seen = []

    def make_card(text):
        seen.append(text)
        return "Topics\nVectors"

    doc_id = ingest_upload(store, "a", name, content, read_image=no_image, make_card=make_card)
    doc = store.get("a", doc_id)
    assert doc["read_as"] == "text" and doc["source"] == "upload"
    assert doc["title"] == name and doc["card"] == "Topics\nVectors"
    assert seen == ["Notes on vectors"]
    assert doc["pages"] == [{"n": 1, "text": "Notes on vectors", "from_image": False}]


@pytest.mark.parametrize("suffix", ["png", "jpg", "jpeg", "webp"])
def test_image_upload_once_and_temp_cleanup(tmp_path, suffix):
    store = CoreStore(tmp_path / "core.db")
    seen = []

    def read(path):
        path = Path(path)
        assert path.read_bytes() == b"photo"
        assert path.suffix == "." + suffix
        seen.append(path)
        return "$a \\cdot b = 2$"

    doc_id = ingest_upload(store, "a", "../../photo." + suffix, b"photo",
                           read_image=read, make_card=lambda text: "Formulas\n" + text)
    doc = store.get("a", doc_id)
    assert len(seen) == 1 and not seen[0].exists()
    assert doc["read_as"] == "image"
    assert [p["from_image"] for p in doc["pages"]] == [True]


@pytest.mark.parametrize("result", [None, "", "  ", "READ_FAILED", "I cannot read this image.",
                                    "I'm sorry, I can't view this file.", "Bu görseli okuyamıyorum."])
def test_unreadable_photo_is_failed_with_turkish_reason(tmp_path, result):
    store = CoreStore(tmp_path / "core.db")

    def read(path):
        if result is None:
            raise RuntimeError("private backend error")
        return result

    doc_id = ingest_upload(store, "a", "photo.png", b"photo", read_image=read,
                           make_card=lambda text: pytest.fail("must not summarize failed input"))
    doc = store.get("a", doc_id)
    assert doc["read_as"] == "failed" and doc["pages"] == []
    assert "okunamadı" in doc["error"]
    assert "private backend" not in doc["error"]


def test_scanned_pdf_uses_vision_after_no_text_layer(tmp_path):
    store = CoreStore(tmp_path / "core.db")
    content = pdf_with_pages("", "")
    seen = []

    def read(path):
        seen.append(Path(path).read_bytes())
        return "--- Page 1 ---\nscan one\n\n--- Page 2 ---\nscan two"

    doc_id = ingest_upload(store, "a", "scan.pdf", content, read_image=read,
                           make_card=lambda text: "Scans")
    doc = store.get("a", doc_id)
    assert seen == [content]
    assert doc["read_as"] == "image"
    assert [p["text"] for p in doc["pages"]] == ["scan one", "scan two"]
    assert all(p["from_image"] for p in doc["pages"])


def test_mixed_pdf_keeps_text_and_scanned_pages_in_order(tmp_path):
    store = CoreStore(tmp_path / "core.db")
    content = pdf_with_pages("typed one", "", "typed three", "")
    calls = []

    def read(path):
        calls.append(Path(path).read_bytes())
        return ("--- Page 1 ---\nvision should not replace text\n\n"
                "--- Page 2 ---\nscan two\n\n--- Page 3 ---\nignored\n\n"
                "--- Page 4 ---\nscan four")

    doc_id = ingest_upload(store, "a", "mixed.pdf", content,
                           read_image=read, make_card=lambda text: text)
    doc = store.get("a", doc_id)
    assert calls == [content]
    assert doc["read_as"] == "mixed"
    assert [p["text"] for p in doc["pages"]] == ["typed one", "scan two", "typed three", "scan four"]
    assert [p["from_image"] for p in doc["pages"]] == [False, True, False, True]


def test_text_pdf_never_uses_vision(tmp_path):
    store = CoreStore(tmp_path / "core.db")
    doc_id = ingest_upload(store, "a", "typed.pdf", pdf_with_pages("one", "two"),
                           read_image=no_image, make_card=lambda text: "card")
    doc = store.get("a", doc_id)
    assert doc["read_as"] == "text"
    assert [p["text"] for p in doc["pages"]] == ["one", "two"]


@pytest.mark.parametrize("vision", ["--- Page 2 ---\nonly one missing page",
                                    "all pages merged without markers",
                                    "--- Page 2 ---\nREAD_FAILED\n--- Page 4 ---\nscan four"])
def test_ambiguous_or_unreadable_mixed_pages_fail(tmp_path, vision):
    store = CoreStore(tmp_path / "core.db")
    doc_id = ingest_upload(store, "a", "mixed.pdf", pdf_with_pages("one", "", "three", ""),
                           read_image=lambda path: vision, make_card=lambda text: "card")
    assert store.get("a", doc_id)["read_as"] == "failed"


def test_split_at_paragraph_boundaries_and_hard_limit(tmp_path):
    store = CoreStore(tmp_path / "core.db")
    paragraphs = ["x" * 1800, "y" * 1800, "z" * (PAGE_CHARS * 2 + 5)]
    content = "\n\n".join(paragraphs)
    doc_id = ingest_upload(store, "a", "long.txt", content.encode(), read_image=no_image,
                           make_card=lambda text: "c" * 2000)
    doc = store.get("a", doc_id)
    pages = [p["text"] for p in doc["pages"]]
    assert all(0 < len(p) <= PAGE_CHARS for p in pages)
    assert pages[0].rstrip() == "\n\n".join(paragraphs[:2])
    assert "".join(pages) == content
    assert len(doc["card"]) == 1200


def test_card_failure_still_stores_document_and_research(tmp_path):
    store = CoreStore(tmp_path / "core.db")

    def broken(text):
        raise RuntimeError("model unavailable")

    doc_id = ingest_upload(store, "a", "text.txt", b"real notes", read_image=no_image,
                           make_card=broken)
    assert store.get("a", doc_id)["card"] == ""
    assert store.get("a", doc_id)["read_as"] == "text"
    research_id = save_research(store, "b", "Research", "source answer", ["https://example.org"],
                                make_card=broken)
    doc = store.get("b", research_id)
    assert doc["source"] == "research" and doc["read_as"] == "text"
    assert doc["urls"] == ["https://example.org"] and doc["card"] == ""
    assert store.list("a")[0]["id"] != research_id


def test_card_prompt_contract():
    for phrase in ("1200", "Topics", "Definitions", "Formulas", "Questions", "topic",
                   "untrusted"):
        assert phrase in CARD_PROMPT


def test_reserved_upload_is_completed_without_changing_source_id(tmp_path):
    store = CoreStore(tmp_path / "core.db")
    pending = store.reserve_upload("a", "notes.txt", "file-id")
    result = ingest_upload(store, "a", "notes.txt", b"Notes", read_image=no_image,
                           make_card=lambda text: "Card", doc_id=pending)
    assert result == pending and len(store.list("a")) == 1
    assert store.get("a", pending)["file_id"] == "file-id"
    assert store.get("a", pending)["read_as"] == "text"


def test_optimized_pdf_reads_only_missing_pages_and_keeps_failure(tmp_path, optimized):
    mupdf, image = optimized
    with mupdf.open() as pdf:
        for text in ("Typed page one", "", "Typed page three", ""):
            page = pdf.new_page()
            if text:
                page.insert_text((72, 72), text)
        content = pdf.tobytes()
    store = CoreStore(tmp_path / "core.db")
    calls = []

    def read(path):
        with image.open(path) as photo:
            assert max(photo.size) <= 2000
        calls.append(path)
        return "READ_FAILED"

    identifier = ingest_upload(store, "owner", "notes.pdf", content, read_image=read,
                               make_card=lambda text: "Card")
    doc = store.get("owner", identifier)
    assert doc["read_as"] == "mixed"
    assert [page["from_image"] for page in doc["pages"]] == [False, True, False, True]
    assert [page["text"] for page in doc["pages"]][::2] == ["Typed page one", "Typed page three"]
    assert "Page 2 unread" in doc["pages"][1]["text"]
    assert "Page 4 unread" in doc["pages"][3]["text"]
    assert len(calls) == 2 and all(not path.exists() for path in calls)


def test_optimized_photo_is_rotated_and_capped_before_callback(tmp_path, optimized):
    _, image = optimized
    output = io.BytesIO()
    exif = image.Exif()
    exif[274] = 6
    with image.new("RGB", (3000, 1500), "white") as photo:
        photo.save(output, format="JPEG", exif=exif)
    store = CoreStore(tmp_path / "core.db")

    def read(path):
        with image.open(path) as photo:
            assert photo.size == (1000, 2000)
            assert not photo.getexif()
        return "Course notes"

    identifier = ingest_upload(store, "a", "phone.jpg", output.getvalue(),
                               read_image=read, make_card=lambda text: "Card")
    assert store.get("a", identifier)["pages"][0]["text"] == "Course notes"


def test_optimized_pdf_rejects_hostile_page_count_without_vision(tmp_path, optimized):
    mupdf, _ = optimized
    with mupdf.open() as pdf:
        for _ in range(301):
            pdf.new_page(width=10, height=10)
        content = pdf.tobytes()
    store = CoreStore(tmp_path / "core.db")
    identifier = ingest_upload(store, "a", "oversized.pdf", content, read_image=no_image,
                               make_card=lambda text: pytest.fail("rejected input"))
    assert store.get("a", identifier)["read_as"] == "failed"
