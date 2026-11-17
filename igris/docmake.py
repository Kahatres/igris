"""Markdown to a Word document (DOCX) with the standard library only.

Covers what chat answers use: headings (#, ##, ###), paragraphs, **bold**, *italic*,
`code`, bullet, numbered and checkbox lists, tables (| a | b |, the --- row is the header
divider) and horizontal rules (dropped). Anything else is kept as plain text.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

_HEADING = re.compile(r"(#{1,6})\s+(.*)")
_BULLET = re.compile(r"(\s*)[-*+]\s+(.*)")
_NUMBER = re.compile(r"(\s*)(\d+)[.)]\s+(.*)")
_CHECK = re.compile(r"\[( |x|X)\]\s+(.*)")
_RULE = re.compile(r"\s*([-*_])(\s*\1){2,}\s*")
_DIVIDER = re.compile(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|`[^`]+`)")
_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '</Types>')
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/styles" Target="styles.xml"/></Relationships>')


def _style(style_id: str, name: str, size: int, *, bold=True, before=240, after=120) -> str:
    return (f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>'
            '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
            f'<w:pPr><w:keepNext/><w:spacing w:before="{before}" w:after="{after}"/></w:pPr>'
            f'<w:rPr>{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr></w:style>')


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles xmlns:w="{_W}">'
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" '
    'w:cs="Calibri" w:eastAsia="Calibri"/><w:sz w:val="22"/><w:lang w:val="en-GB"/></w:rPr>'
    '</w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" '
    'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    '<w:qFormat/></w:style>'
    + _style("Heading1", "heading 1", 32, before=360)
    + _style("Heading2", "heading 2", 28)
    + _style("Heading3", "heading 3", 24)
    + _style("Heading4", "heading 4", 22)
    + '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
    '<w:tblPr><w:tblCellMar><w:left w:w="108" w:type="dxa"/><w:right w:w="108" w:type="dxa"/>'
    '</w:tblCellMar></w:tblPr></w:style>'
    '</w:styles>')


def _clean(text: str) -> str:
    return _BAD_XML.sub("", text)


def _runs(text: str, *, bold=False) -> str:
    """Inline markdown as w:r elements."""
    out = []
    for part in _INLINE.split(_clean(text)):
        if not part:
            continue
        code, strong, italic, body = False, bold, False, part
        if (part.startswith("**") and part.endswith("**")) or (part.startswith("__") and part.endswith("__")):
            strong, body = True, part[2:-2]
        elif part.startswith("`") and part.endswith("`") and len(part) > 1:
            code, body = True, part[1:-1]
        elif len(part) > 2 and part[0] == part[-1] and part[0] in "*_" and _INLINE.fullmatch(part):
            italic, body = True, part[1:-1]
        # WordprocessingML wants rFonts, then b, then i inside rPr
        props = ('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>' if code else "") \
            + ("<w:b/>" if strong else "") + ("<w:i/>" if italic else "")
        rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(body)}</w:t></w:r>')
    return "".join(out)


def _para(text: str, *, style: str | None = None, indent: int = 0, hanging: int = 0,
          bold=False) -> str:
    ppr = []
    if style:
        ppr.append(f'<w:pStyle w:val="{style}"/>')
    if indent:
        ppr.append(f'<w:ind w:left="{indent}" w:hanging="{hanging}"/>')
    props = f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else ""
    return f"<w:p>{props}{_runs(text, bold=bold)}</w:p>"


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _table(rows: list[list[str]], header: bool) -> str:
    width = max(len(r) for r in rows)
    total = 9000  # twips, about the text width of an A4 page with normal margins
    col = total // width
    border = "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
                     for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="{total}" w:type="dxa"/><w:tblBorders>{border}'
           '</w:tblBorders><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>'
           + f'<w:gridCol w:w="{col}"/>' * width + "</w:tblGrid>"]
    for number, row in enumerate(rows):
        head = header and number == 0
        trpr = "<w:trPr><w:tblHeader/></w:trPr>" if head else ""
        cells = []
        for text in row + [""] * (width - len(row)):
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="E7E6E6"/>' if head else ""
            cells.append(f'<w:tc><w:tcPr><w:tcW w:w="{col}" w:type="dxa"/>{shade}</w:tcPr>'
                         f'{_para(text, bold=head)}</w:tc>')
        out.append(f"<w:tr>{trpr}{''.join(cells)}</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def _body(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped.startswith("```"):
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                out.append(_para(f"`{lines[i]}`" if lines[i].strip() else ""))
                i += 1
            i += 1
            continue
        if stripped.startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            header = len(block) > 1 and bool(_DIVIDER.fullmatch(block[1].strip()))
            rows = [_cells(r) for n, r in enumerate(block) if not (header and n == 1)]
            if header and len(rows) > 1 and not any(rows[0]):
                rows, header = rows[1:], False  # "| | |" only gives the column count
            out.append(_table(rows, header))
            out.append("<w:p/>")
            continue
        if _RULE.fullmatch(stripped):
            i += 1
            continue
        heading = _HEADING.fullmatch(stripped)
        if heading:
            level = min(len(heading.group(1)), 4)
            out.append(_para(heading.group(2).strip().strip("#").strip(), style=f"Heading{level}"))
            i += 1
            continue
        bullet, number = _BULLET.fullmatch(line), _NUMBER.fullmatch(line)
        if bullet or number:
            depth = len((bullet or number).group(1).expandtabs(4)) // 2
            text = bullet.group(2) if bullet else number.group(3)
            check = _CHECK.fullmatch(text)
            if check:
                mark = "☒" if check.group(1).lower() == "x" else "☐"
                text = check.group(2)
            else:
                mark = "•" if bullet else f"{number.group(2)}."
            out.append(_para(f"{mark}\t{text}", indent=360 + 360 * depth, hanging=360))
            i += 1
            continue
        if stripped.startswith(">"):
            out.append(_para(stripped.lstrip(">").strip(), indent=360))
            i += 1
            continue
        # a paragraph runs until a blank line or another block starts
        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith(("|", "#", ">", "```")) or _RULE.fullmatch(nxt)
                    or _BULLET.fullmatch(lines[i]) or _NUMBER.fullmatch(lines[i])):
                break
            para.append(nxt)
            i += 1
        out.append(_para(" ".join(para)))
    return "".join(out)


def markdown_to_docx(markdown: str) -> bytes:
    """The DOCX file for ``markdown`` as bytes."""
    if not isinstance(markdown, str):
        raise TypeError("markdown must be text")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{_body(markdown)}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" '
        'w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
        '</w:body></w:document>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", _STYLES)
    return buffer.getvalue()
