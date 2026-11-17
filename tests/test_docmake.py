"""docmake: Markdown to DOCX, checked by parsing the XML and reading it back with doctext."""
import io
import zipfile
from xml.etree import ElementTree

from igris.docmake import markdown_to_docx
from igris.doctext import extract_text

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

SAMPLE = """# Practical Work 7: Google Apps Analysis

| | |
|---|---|
| **Student** | [Name Surname] |
| **Group** | [Group] |

## 1. Google Apps for Business

**Chosen tool:** Google Meet · *Link:* `https://meet.google.com`

A paragraph that
continues on the next line & has <special> characters.

- first bullet
  - nested bullet
1. numbered one
- [ ] open box
- [x] ticked box

---

> a quote
"""


def document(data):
    archive = zipfile.ZipFile(io.BytesIO(data))
    assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml",
            "word/_rels/document.xml.rels"} <= set(archive.namelist())
    for name in archive.namelist():
        ElementTree.fromstring(archive.read(name))  # every part is well-formed XML
    return ElementTree.fromstring(archive.read("word/document.xml"))


def test_sample_becomes_a_valid_document_that_reads_back():
    data = markdown_to_docx(SAMPLE)
    root = document(data)
    styles = [s.get(W + "val") for s in root.iter(W + "pStyle")]
    assert styles == ["Heading1", "Heading2"]
    text = extract_text("x.docx", data)
    for expected in ("Practical Work 7: Google Apps Analysis", "Student", "[Name Surname]",
                     "Google Meet", "https://meet.google.com",
                     "A paragraph that continues on the next line & has <special> characters.",
                     "first bullet", "nested bullet", "1.", "☐", "open box", "☒", "a quote"):
        assert expected in text
    assert "**" not in text and "|---" not in text and "`" not in text


def test_table_has_a_shaded_header_only_when_a_divider_follows_the_first_row():
    root = document(markdown_to_docx("| A | B |\n|---|:--:|\n| 1 | 2 |\n\n| x | y |\n| z |"))
    tables = list(root.iter(W + "tbl"))
    assert len(tables) == 2
    first, second = tables
    assert len(first.findall(W + "tr")) == 2 and first.find(f"{W}tr/{W}trPr/{W}tblHeader") is not None
    assert second.find(f"{W}tr/{W}trPr") is None
    # a short row is padded to the widest row
    assert [len(r.findall(W + "tc")) for r in second.findall(W + "tr")] == [2, 2]


def test_an_empty_header_row_is_dropped():
    root = document(markdown_to_docx("| | |\n|---|---|\n| **Student** | [Name] |"))
    rows = list(root.iter(W + "tr"))
    assert len(rows) == 1 and rows[0].find(W + "trPr") is None


def test_run_properties_follow_the_schema_order_inside_a_header_cell():
    root = document(markdown_to_docx("| `code` |\n|---|\n| x |"))
    props = next(r.find(W + "rPr") for r in root.iter(W + "r") if r.find(W + "t").text == "code")
    assert [child.tag for child in props] == [W + "rFonts", W + "b"]


def test_bold_italic_and_code_runs():
    root = document(markdown_to_docx("plain **bold** *italic* `code`"))
    runs = {r.find(W + "t").text: r.find(W + "rPr") for r in root.iter(W + "r")}
    assert runs["bold"].find(W + "b") is not None
    assert runs["italic"].find(W + "i") is not None
    assert runs["code"].find(W + "rFonts").get(W + "ascii") == "Consolas"
    assert runs["plain "] is None


def test_control_characters_and_empty_input_still_give_a_valid_file():
    document(markdown_to_docx("bad \x01 char"))
    document(markdown_to_docx(""))


def test_code_fence_lines_are_kept_verbatim():
    text = extract_text("x.docx", markdown_to_docx("```\nprint('hi')  # **not bold**\n```"))
    assert "print('hi')  # **not bold**" in text
