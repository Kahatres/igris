import io
import zipfile
import zlib

import pytest

from igris import doctext
from igris.doctext import DocError, extract_text

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def zipped(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def docx(body: str) -> bytes:
    return zipped({"word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>"})


def para(text, style=None):
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def pdf(*objects: bytes) -> bytes:
    out = b"%PDF-1.7\n"
    for i, body in enumerate(objects, 1):
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    return out + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


def stream(content: bytes, flate=False, extra=b"") -> bytes:
    if flate:
        content = zlib.compress(content)
        extra += b" /Filter /FlateDecode"
    return b"<< /Length %d%s >>\nstream\n" % (len(content), extra) + content + b"\nendstream"


def simple_pdf(content: bytes, flate=False, font=b"<< /Type /Font /Subtype /Type1 >>") -> bytes:
    return pdf(b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 /Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
               stream(content, flate),
               font)


def test_plain_text_and_binary():
    assert extract_text("a.txt", "şğü".encode()) == "şğü"
    with pytest.raises(DocError, match="UTF-8"):
        extract_text("x.bin", b"\x00\xff")


def test_docx_paragraphs_headings_and_tables():
    body = (para("Lab report", "Heading1") + para("Intro text") +
            "<w:tbl><w:tr><w:tc>" + para("Angle") + "</w:tc><w:tc>" + para("Cl") +
            "</w:tc></w:tr><w:tr><w:tc>" + para("2") + "</w:tc><w:tc>" + para("0.21") +
            "</w:tc></w:tr></w:tbl>")
    text = extract_text("t.docx", docx(body))
    assert text.splitlines()[:4] == ["# Lab report", "Intro text", "| Angle | Cl |", "| 2 | 0.21 |"]


def test_xlsx_shared_and_inline_values():
    data = zipped({
        "xl/workbook.xml": '<workbook xmlns="m" xmlns:r="r"><sheets>'
                           '<sheet name="Book 1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="rId1" '
                                      'Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": "<sst><si><t>Angle</t></si></sst>",
        "xl/worksheets/sheet1.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                                    '<c r="B1"><v>4</v></c><c r="C1" t="inlineStr"><is><t>deg</t>'
                                    "</is></c></row></sheetData></worksheet>"})
    assert extract_text("b.xlsx", data) == "## Sheet: Book 1\nA1=Angle | B1=4 | C1=deg"


def test_pptx_slides_in_order():
    slide = '<p:sld xmlns:p="p" xmlns:a="a"><a:p><a:r><a:t>{}</a:t></a:r></a:p></p:sld>'
    data = zipped({"ppt/slides/slide10.xml": slide.format("ten"),
                   "ppt/slides/slide2.xml": slide.format("two")})
    assert extract_text("s.pptx", data) == "--- Slide 1 ---\ntwo\n\n--- Slide 2 ---\nten"


def office_members(kind):
    if kind == "pptx":
        return {f"ppt/slides/slide{i}.xml": "<slide><p><t>text</t></p></slide>"
                for i in range(1, 4)}
    return {
        "xl/sharedStrings.xml": "<sst><si><t>text</t></si></sst>",
        "xl/workbook.xml": '<workbook xmlns:r="r"><sheets>'
                           '<sheet name="One" r:id="r1"/>'
                           '<sheet name="Two" r:id="r2"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships>'
            '<Relationship Id="r1" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="r2" Target="worksheets/sheet2.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": '<worksheet><row><c r="A1" t="s"><v>0</v></c></row></worksheet>',
        "xl/worksheets/sheet2.xml": '<worksheet><row><c r="A1"><v>2</v></c></row></worksheet>',
    }


@pytest.mark.parametrize("kind", ["xlsx", "pptx"])
def test_office_members_share_decompression_budget(monkeypatch, kind):
    files = office_members(kind)
    data = zipped(files)
    total = sum(len(value.encode()) for value in files.values())
    monkeypatch.setattr(doctext, "MAX_ARCHIVE_BYTES", total - 1)
    opened = []
    original = zipfile.ZipFile.open

    def track(archive, name, *args, **kwargs):
        info = name if isinstance(name, zipfile.ZipInfo) else archive.getinfo(name)
        opened.append(info.file_size)
        assert sum(opened) <= total - 1, "member opened after exhausting aggregate budget"
        return original(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", track)
    with pytest.raises(DocError, match="budget"):
        extract_text("document." + kind, data)


@pytest.mark.parametrize("kind", ["xlsx", "pptx"])
def test_office_stops_reading_members_at_character_limit(monkeypatch, kind):
    files = office_members(kind)
    second = "ppt/slides/slide2.xml" if kind == "pptx" else "xl/worksheets/sheet2.xml"
    files[second] = "not valid XML: must never be read"
    first_text = "--- Slide 1 ---\ntext" if kind == "pptx" else "## Sheet: One\nA1=text"
    monkeypatch.setattr(doctext, "MAX_CHARS", len(first_text))
    assert extract_text("document." + kind, zipped(files)) == first_text


def test_pptx_stops_accumulating_paragraphs_at_character_limit(monkeypatch):
    files = {"ppt/slides/slide1.xml": "<slide><p><t>abcdefghijk</t></p>"
             + "<p><t>more text</t></p>" * 100 + "</slide>"}
    original = doctext._local
    visits = []

    def track(tag):
        visits.append(tag)
        assert len(visits) < 20, "kept collecting paragraphs after reaching MAX_CHARS"
        return original(tag)

    monkeypatch.setattr(doctext, "_local", track)
    monkeypatch.setattr(doctext, "MAX_CHARS", 20)
    assert extract_text("s.pptx", zipped(files)) == "--- Slide 1 ---\nabcd"


def test_nested_documents_share_the_upload_budget(monkeypatch):
    document = zipped(office_members("pptx"))
    monkeypatch.setattr(doctext, "MAX_ARCHIVE_BYTES", len(document) + 1)
    text = extract_text("outer.zip", zipped({"slides.pptx": document}))
    assert "budget" in text and "--- Slide" not in text


def test_zip_lists_and_reads_members_including_documents():
    inner = zipped({"deep.txt": "deep"})
    data = zipped({"notes.txt": "hello", "report.docx": docx(para("Body")),
                   "model.step": b"\x00binary", "nested.zip": inner})
    text = extract_text("task.zip", data)
    assert "- notes.txt (5 bytes)" in text
    assert "=== notes.txt ===\nhello" in text and "=== report.docx ===\nBody" in text
    assert "=== model.step ===\n[not a text or document file; not read]" in text
    assert "=== deep.txt ===\ndeep" in text


def test_zip_skips_oversized_members(monkeypatch):
    monkeypatch.setattr(doctext, "MAX_MEMBER_BYTES", 10)
    text = extract_text("a.zip", zipped({"big.txt": "x" * 50}))
    assert "[too large to read: 50 bytes]" in text and "xxxxx" not in text


def test_zip_depth_limit(monkeypatch):
    monkeypatch.setattr(doctext, "MAX_DEPTH", 0)
    text = extract_text("a.zip", zipped({"n.zip": zipped({"x.txt": "x"})}))
    assert "[nested archive too deep; not read]" in text


def test_xml_with_a_dtd_is_refused():
    bomb = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]>'
            f"<w:document {W}><w:body>&a;</w:body></w:document>")
    with pytest.raises(DocError, match="DTD"):
        extract_text("x.docx", zipped({"word/document.xml": bomb}))


def test_damaged_archive():
    with pytest.raises(DocError, match="damaged"):
        extract_text("x.docx", b"PK\x03\x04garbage")


@pytest.mark.parametrize("flate", [False, True])
def test_pdf_text_operators(flate):
    content = (b"BT /F1 12 Tf 72 700 Td (Step 27: set the angle) Tj 0 -14 Td "
               b"[(to ) -300 (4 deg\\)) ] TJ T* (next \\(line\\)) Tj ET")
    text = extract_text("t.pdf", simple_pdf(content, flate))
    assert text == "--- Page 1 ---\nStep 27: set the angle\nto  4 deg)\nnext (line)"


def test_pdf_type0_font_with_tounicode_map():
    cmap = (b"begincmap 1 begincodespacerange <0000> <FFFF> endcodespacerange "
            b"2 beginbfchar <0001> <0041> <0002> <015F> endbfchar "
            b"1 beginbfrange <0010> <0012> <0061> endbfrange endcmap")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               stream(b"BT /F1 10 Tf <000100020010001100120099> Tj ET"),
               b"<< /Type /Font /Subtype /Type0 /ToUnicode 6 0 R >>",
               stream(cmap, flate=True)]
    assert extract_text("u.pdf", pdf(*objects)) == "--- Page 1 ---\nAşabc"


def test_pdf_objects_inside_an_object_stream_and_page_order():
    page = b"<< /Type /Page /Parent 2 0 R /Contents 5 0 R >> "
    inner = page + b"<< /Type /Font /Subtype /Type1 >>"
    first = b"9 0 10 %d " % len(page)
    objstm = zlib.compress(first + inner)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [7 0 R 9 0 R] /Count 2 "
               b"/Resources << /Font << /F1 10 0 R >> >> >>",
               b"<< /Type /ObjStm /N 2 /First %d /Length %d /Filter /FlateDecode >>\nstream\n"
               % (len(first), len(objstm)) + objstm + b"\nendstream",
               b"null",
               stream(b"BT /F1 9 Tf (second) Tj ET"),
               b"null",
               b"<< /Type /Page /Parent 2 0 R /Contents 8 0 R >>",
               stream(b"BT /F1 9 Tf (first) Tj ET")]
    text = extract_text("o.pdf", pdf(*objects))
    assert text == "--- Page 1 ---\nfirst\n\n--- Page 2 ---\nsecond"


def test_pdf_form_xobject_text():
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources "
               b"<< /XObject << /X1 5 0 R >> /Font << /F1 6 0 R >> >> >>",
               stream(b"q /X1 Do Q"),
               stream(b"BT /F1 9 Tf (in a form) Tj ET", extra=b" /Subtype /Form"),
               b"<< /Type /Font /Subtype /Type1 >>"]
    assert extract_text("f.pdf", pdf(*objects)) == "--- Page 1 ---\nin a form"


def test_pdf_without_text_and_encrypted_pdf():
    with pytest.raises(DocError, match="no text layer"):
        extract_text("scan.pdf", simple_pdf(b"q 100 0 0 100 0 0 cm /Im1 Do Q"))
    with pytest.raises(DocError, match="encrypted"):
        extract_text("e.pdf", simple_pdf(b"BT ET") + b"trailer << /Encrypt 9 0 R >>")
    with pytest.raises(DocError):
        extract_text("broken.pdf", b"%PDF-1.4 nothing here")
