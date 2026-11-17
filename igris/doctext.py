"""Plain text from uploaded documents: ZIP, DOCX, XLSX, PPTX and PDF (standard library only).

PDF support covers files with a text layer (exports from Word, LaTeX, browsers). Scanned
pages are images and come back empty; there is no OCR. Every limit below exists so a
hostile archive or PDF cannot exhaust memory: oversized members are skipped, not read.
"""

from __future__ import annotations

import base64
import binascii
import io
import posixpath
import re
import zipfile
import zlib
from xml.etree import ElementTree

__all__ = ["DocError", "extract_text", "SUPPORTED"]

SUPPORTED = ("zip", "docx", "xlsx", "pptx", "pdf")
MAX_MEMBER_BYTES = 50 * 1024 * 1024     # one file inside an archive, uncompressed
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024   # everything read from one archive
MAX_DEPTH = 2                            # a zip inside a zip inside the upload
MAX_STREAM_BYTES = 64 * 1024 * 1024      # one decompressed PDF stream
MAX_CHARS = 2_000_000                    # text returned for one upload
TEXT_EXTENSIONS = (".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".py",
                   ".js", ".ts", ".c", ".h", ".cpp", ".java", ".m", ".tex", ".log", ".ini",
                   ".cfg", ".yaml", ".yml", ".toml", ".dat", ".inp", ".jou", ".mac", ".apdl",
                   ".wbjn", ".rst", ".sql", ".sh", ".bat", ".ps1")


class DocError(ValueError):
    """The upload is not text and not a readable document."""


class _Budget:
    """Uncompressed bytes still allowed for one upload, shared by every member and nested file."""

    def __init__(self, total: int):
        self.left = total

    def fits(self, info: zipfile.ZipInfo) -> bool:
        return info.file_size <= self.left

    def take(self, info: zipfile.ZipInfo) -> None:
        # ZipExtFile never returns more than the declared file_size, so it is a hard bound
        if not self.fits(info):
            raise DocError(f"{info.filename}: decompression budget for this upload exhausted")
        self.left -= info.file_size


def extract_text(name: str, data: bytes, _depth: int = 0, _budget: _Budget | None = None) -> str:
    """Readable text of ``data``; raises DocError when there is none."""
    budget = _Budget(MAX_ARCHIVE_BYTES) if _budget is None else _budget
    kind = _kind(name, data)
    if kind == "pdf":
        text = _pdf(data)
    elif kind in ("docx", "xlsx", "pptx", "zip"):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, ValueError):
            raise DocError(f"{name}: damaged or unsupported archive") from None
        with archive:
            text = {"docx": _docx, "xlsx": _xlsx, "pptx": _pptx}.get(kind, _zip)(
                archive, _depth, budget)
    else:
        if b"\x00" in data:
            raise DocError("This file is not UTF-8 text and not a supported document "
                           "(" + ", ".join(SUPPORTED) + ")")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise DocError("This file is not UTF-8 text and not a supported document "
                           "(" + ", ".join(SUPPORTED) + ")") from None
    return text[:MAX_CHARS]


def _kind(name: str, data: bytes) -> str:
    ext = posixpath.splitext(name.lower())[1]
    if ext in (".docx", ".docm", ".dotx"):
        return "docx"
    if ext in (".xlsx", ".xlsm"):
        return "xlsx"
    if ext == ".pptx":
        return "pptx"
    if ext == ".pdf" or data[:5] == b"%PDF-":
        return "pdf"
    if ext == ".zip" or data[:4] == b"PK\x03\x04":
        return "zip"
    return "text"


# ---- Office files (zip archives of XML) ----

def _member(archive: zipfile.ZipFile, name: str, budget: _Budget) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_MEMBER_BYTES:
        raise DocError(f"{name} is too large to read")
    budget.take(info)
    return archive.read(info)


def _xml(raw: bytes | None):
    if raw is None:
        return None
    # Office XML never declares a DTD; refusing one rules out entity expansion attacks
    if re.search(rb"<!(DOCTYPE|ENTITY)", raw[:4096], re.IGNORECASE) or b"<!ENTITY" in raw:
        raise DocError("document XML declares a DTD; refusing to parse it")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        raise DocError("damaged document XML") from None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _docx_paragraph(p) -> str:
    out = []
    for el in p.iter():
        tag = _local(el.tag)
        if tag in ("t", "delText") and el.text:
            out.append(el.text)
        elif tag == "tab":
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
    text = "".join(out)
    for el in p.iter():
        if _local(el.tag) == "pStyle":
            style = next((v for k, v in el.attrib.items() if _local(k) == "val"), "")
            m = re.match(r"(?i)heading\s*(\d)", style) or re.match(r"(?i)ba[sş]l[iı]k\s*(\d)", style)
            if m and text.strip():
                return "#" * int(m.group(1)) + " " + text
            if style.lower() == "title" and text.strip():
                return "# " + text
    return text


def _docx_block(el, lines: list) -> None:
    tag = _local(el.tag)
    if tag == "p":
        lines.append(_docx_paragraph(el))
    elif tag == "tbl":
        for row in (c for c in el if _local(c.tag) == "tr"):
            cells = []
            for cell in (c for c in row if _local(c.tag) == "tc"):
                inner = []
                for child in cell:
                    _docx_block(child, inner)
                cells.append(" ".join(s.strip() for s in inner if s.strip()))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    elif tag in ("sdt", "sdtContent", "customXml", "ins", "smartTag"):
        for child in el:
            _docx_block(child, lines)


def _docx(archive: zipfile.ZipFile, _depth: int, budget: _Budget) -> str:
    root = _xml(_member(archive, "word/document.xml", budget))
    if root is None:
        raise DocError("not a Word document (word/document.xml is missing)")
    body = next((c for c in root if _local(c.tag) == "body"), root)
    lines: list[str] = []
    for el in body:
        _docx_block(el, lines)
    return "\n".join(lines).strip()


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    n = 0
    for ch in letters.group(0) if letters else "":
        n = n * 26 + ord(ch) - 64
    return n


def _xlsx(archive: zipfile.ZipFile, _depth: int, budget: _Budget) -> str:
    shared = []
    sst = _xml(_member(archive, "xl/sharedStrings.xml", budget))
    if sst is not None:
        for si in (c for c in sst if _local(c.tag) == "si"):
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    workbook = _xml(_member(archive, "xl/workbook.xml", budget))
    if workbook is None:
        raise DocError("not an Excel workbook (xl/workbook.xml is missing)")
    rels = {}
    rel_root = _xml(_member(archive, "xl/_rels/workbook.xml.rels", budget))
    for rel in (rel_root if rel_root is not None else []):
        target = rel.get("Target", "")
        target = target.lstrip("/") if target.startswith("/") else "xl/" + target
        rels[rel.get("Id")] = posixpath.normpath(target)
    out = []
    chars = 0  # length of "\n".join(out) so far; reading stops once MAX_CHARS is reached

    def add(line: str) -> None:
        nonlocal chars
        out.append(line)
        chars += len(line) + 1

    for sheet in (el for el in workbook.iter() if _local(el.tag) == "sheet"):
        if chars >= MAX_CHARS:
            break
        rid = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
        path = rels.get(rid)
        root = _xml(_member(archive, path, budget)) if path else None
        add(f"## Sheet: {sheet.get('name', '?')}")
        if root is None:
            continue
        for row in (el for el in root.iter() if _local(el.tag) == "row"):
            if chars >= MAX_CHARS:
                break
            cells = []
            for c in (el for el in row if _local(el.tag) == "c"):
                kind = c.get("t")
                value = None
                for child in c:
                    tag = _local(child.tag)
                    if tag == "v":
                        value = child.text
                    elif tag == "is":
                        value = "".join(t.text or "" for t in child.iter() if _local(t.tag) == "t")
                if value is None:
                    continue
                if kind == "s":
                    try:
                        value = shared[int(value)]
                    except (ValueError, IndexError):
                        pass
                elif kind == "b":
                    value = "TRUE" if value == "1" else "FALSE"
                cells.append(f"{c.get('r', '?')}={value}")
            if cells:
                add(" | ".join(cells))
        add("")
    return "\n".join(out).strip()


def _pptx(archive: zipfile.ZipFile, _depth: int, budget: _Budget) -> str:
    names = [n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    names.sort(key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    out = []
    chars = 0  # text collected so far; reading stops once MAX_CHARS is reached
    for i, name in enumerate(names, 1):
        if chars >= MAX_CHARS:
            break
        root = _xml(_member(archive, name, budget))
        head = f"--- Slide {i} ---\n"
        chars += len(head) + (2 if out else 0)
        paras = []
        for p in (el for el in root.iter() if _local(el.tag) == "p"):
            if chars >= MAX_CHARS:
                break
            text = "".join(t.text or "" for t in p.iter() if _local(t.tag) == "t")
            if text.strip():
                chars += len(text) + (1 if paras else 0)
                paras.append(text)
        out.append(head + "\n".join(paras))
    return "\n\n".join(out).strip()


def _zip(archive: zipfile.ZipFile, depth: int, budget: _Budget) -> str:
    out = []
    infos = [i for i in archive.infolist()
             if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
    out.append("Archive contents:\n" + "\n".join(
        f"- {i.filename} ({i.file_size} bytes)" for i in infos))
    for info in infos:
        head = f"=== {info.filename} ==="
        ext = posixpath.splitext(info.filename.lower())[1]
        readable = ext in TEXT_EXTENSIONS or ext in (".docx", ".docm", ".xlsx", ".xlsm",
                                                     ".pptx", ".pdf", ".zip") or not ext
        if not readable:
            out.append(f"{head}\n[not a text or document file; not read]")
            continue
        if ext == ".zip" and depth + 1 > MAX_DEPTH:
            out.append(f"{head}\n[nested archive too deep; not read]")
            continue
        if info.file_size > MAX_MEMBER_BYTES or not budget.fits(info):
            out.append(f"{head}\n[too large to read: {info.file_size} bytes]")
            continue
        budget.take(info)
        try:
            raw = archive.read(info)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError):
            out.append(f"{head}\n[could not be extracted (damaged or encrypted)]")
            continue
        try:
            text = extract_text(info.filename, raw, depth + 1, budget)
        except DocError as exc:
            text = f"[{exc}]"
        out.append(f"{head}\n{text}")
    return "\n\n".join(out)


# ---- PDF ----

class _Name(str):
    pass


class _Op(str):
    pass


class _Ref(tuple):
    pass


_WS = b" \t\r\n\x0c\x00"
_DELIM = b"()<>[]{}/%"
_REF_RE = re.compile(rb"\s*(\d+)\s+R(?=[\s/<>\[\]()%]|$)")
_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b",
            ord("f"): b"\f", ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}


def _skip(data: bytes, pos: int) -> int:
    n = len(data)
    while pos < n:
        c = data[pos]
        if c in _WS:
            pos += 1
        elif c == 0x25:  # % comment
            while pos < n and data[pos] not in b"\r\n":
                pos += 1
        else:
            break
    return pos


def _literal(data: bytes, pos: int) -> tuple[bytes, int]:
    out = bytearray()
    depth = 1
    pos += 1
    n = len(data)
    while pos < n:
        c = data[pos]
        if c == 0x5C:  # backslash
            pos += 1
            if pos >= n:
                break
            e = data[pos]
            if e in _ESCAPES:
                out += _ESCAPES[e]
                pos += 1
            elif 0x30 <= e <= 0x37:
                m = re.match(rb"[0-7]{1,3}", data[pos:pos + 3])
                out.append(int(m.group(0), 8) & 0xFF)
                pos += len(m.group(0))
            elif e in b"\r\n":
                pos += 2 if data[pos:pos + 2] == b"\r\n" else 1
            else:
                out.append(e)
                pos += 1
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), pos + 1
        out.append(c)
        pos += 1
    return bytes(out), pos


def _token(data: bytes, pos: int):
    """One value or operator from ``pos``; returns (value, new_pos) or (None, pos) at the end."""
    pos = _skip(data, pos)
    n = len(data)
    if pos >= n:
        return None, pos
    c = data[pos]
    if c == 0x2F:  # /Name
        end = pos + 1
        while end < n and data[end] not in _WS and data[end] not in _DELIM:
            end += 1
        raw = data[pos + 1:end]
        name = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
        return _Name(name.decode("latin-1")), end
    if c == 0x28:
        return _literal(data, pos)
    if c == 0x3C:
        if data[pos + 1:pos + 2] == b"<":
            return _dict(data, pos + 2)
        end = data.find(b">", pos)
        end = n if end < 0 else end
        hexs = re.sub(rb"[^0-9A-Fa-f]", b"", data[pos + 1:end])
        if len(hexs) % 2:
            hexs += b"0"
        return binascii.unhexlify(hexs), end + 1
    if c == 0x5B:
        items = []
        pos += 1
        while True:
            pos = _skip(data, pos)
            if pos >= n or data[pos] == 0x5D:
                return items, pos + 1
            value, new = _token(data, pos)
            if new <= pos:
                return items, n
            items.append(value)
            pos = new
    if c in b"])>}{":
        return _Op(chr(c)), pos + 1
    end = pos
    while end < n and data[end] not in _WS and data[end] not in _DELIM:
        end += 1
    word = data[pos:end]
    if re.fullmatch(rb"[+-]?(\d+\.?\d*|\.\d+)", word):
        if b"." not in word:
            m = _REF_RE.match(data, end)
            if m:
                return _Ref((int(word), int(m.group(1)))), m.end()
            return int(word), end
        return float(word), end
    text = word.decode("latin-1")
    return {"true": True, "false": False, "null": None}.get(text, _Op(text)), end


def _dict(data: bytes, pos: int) -> tuple[dict, int]:
    out = {}
    n = len(data)
    while True:
        pos = _skip(data, pos)
        if pos >= n:
            return out, pos
        if data[pos:pos + 2] == b">>":
            return out, pos + 2
        key, new = _token(data, pos)
        if new <= pos:
            return out, n
        value, pos = _token(data, new)
        if isinstance(key, _Name):
            out[str(key)] = value


class _Pdf:
    def __init__(self, data: bytes):
        self.data = data
        self.objects: dict[int, object] = {}
        self.streams: dict[int, bytes] = {}
        self._fonts: dict = {}
        for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", data):
            try:
                self._read_object(int(m.group(1)), m.end())
            except (ValueError, IndexError, RecursionError):
                continue
        for num, value in list(self.objects.items()):
            if isinstance(value, dict) and value.get("Type") == "ObjStm" and num in self.streams:
                self._read_object_stream(value, self.streams[num])

    def _read_object(self, num: int, pos: int) -> None:
        value, pos = _token(self.data, pos)
        self.objects[num] = value
        if not isinstance(value, dict):
            return
        pos = _skip(self.data, pos)
        if not self.data.startswith(b"stream", pos):
            return
        pos += 6
        if self.data[pos:pos + 2] == b"\r\n":
            pos += 2
        elif self.data[pos:pos + 1] in (b"\n", b"\r"):
            pos += 1
        length = value.get("Length")
        end = pos + length if isinstance(length, int) else -1
        if end < 0 or self.data.find(b"endstream", end, end + 32) < 0:
            end = self.data.find(b"endstream", pos)
            if end < 0:
                return
            while end > pos and self.data[end - 1] in b"\r\n":
                end -= 1
        self.streams[num] = self.data[pos:end]

    def _read_object_stream(self, info: dict, raw: bytes) -> None:
        body = self.decode(info, raw)
        first, count = info.get("First"), info.get("N")
        if not isinstance(first, int) or not isinstance(count, int):
            return
        header = re.findall(rb"\d+", body[:first])
        pairs = list(zip(header[0::2], header[1::2]))[:count]
        for num, offset in pairs:
            num = int(num)
            if num in self.objects and not isinstance(self.objects[num], dict):
                continue
            try:
                value, _ = _token(body, first + int(offset))
            except (ValueError, IndexError, RecursionError):
                continue
            self.objects.setdefault(num, value)

    def resolve(self, value, depth: int = 0):
        while isinstance(value, _Ref) and depth < 32:
            value = self.objects.get(value[0])
            depth += 1
        return value

    def decode(self, info: dict, raw: bytes) -> bytes:
        filters = self.resolve(info.get("Filter"))
        filters = filters if isinstance(filters, list) else [filters] if filters else []
        data = raw
        for f in filters:
            f = str(self.resolve(f))
            if f in ("FlateDecode", "Fl"):
                d = zlib.decompressobj()
                try:
                    data = d.decompress(data, MAX_STREAM_BYTES)
                except zlib.error:
                    return b""
            elif f in ("ASCII85Decode", "A85"):
                text = data.strip()
                text = text[2:] if text.startswith(b"<~") else text
                text = text[:-2] if text.endswith(b"~>") else text
                try:
                    data = base64.a85decode(re.sub(rb"\s", b"", text))
                except ValueError:
                    return b""
            elif f in ("ASCIIHexDecode", "AHx"):
                hexs = re.sub(rb"[^0-9A-Fa-f]", b"", data.split(b">")[0])
                data = binascii.unhexlify(hexs + (b"0" if len(hexs) % 2 else b""))
            else:
                return b""  # images and other filters carry no text
        return data

    def stream(self, ref) -> bytes:
        if isinstance(ref, _Ref) and ref[0] in self.streams:
            info = self.objects.get(ref[0])
            return self.decode(info if isinstance(info, dict) else {}, self.streams[ref[0]])
        return b""

    def pages(self) -> list[tuple[dict, dict]]:
        """(page, inherited resources) in reading order."""
        root = None
        for value in self.objects.values():
            if isinstance(value, dict) and value.get("Type") == "Catalog":
                root = value
        out: list = []
        seen: set = set()

        def walk(node_ref, resources, depth):
            node = self.resolve(node_ref)
            key = node_ref[0] if isinstance(node_ref, _Ref) else id(node)
            if not isinstance(node, dict) or key in seen or depth > 64:
                return
            seen.add(key)
            resources = self.resolve(node.get("Resources")) or resources
            if node.get("Type") == "Pages" or "Kids" in node:
                for kid in self.resolve(node.get("Kids")) or []:
                    walk(kid, resources, depth + 1)
            else:
                out.append((node, resources or {}))

        if root is not None:
            walk(root.get("Pages"), {}, 0)
        if not out:  # no usable page tree: fall back to file order
            for num in sorted(self.objects):
                value = self.objects[num]
                if isinstance(value, dict) and value.get("Type") == "Page":
                    out.append((value, self.resolve(value.get("Resources")) or {}))
        return out

    def font(self, resources: dict, name: str):
        fonts = self.resolve(resources.get("Font")) or {}
        ref = fonts.get(name) if isinstance(fonts, dict) else None
        key = ref[0] if isinstance(ref, _Ref) else (id(resources), name)
        if key not in self._fonts:
            self._fonts[key] = _Font(self, self.resolve(ref))
        return self._fonts[key]


class _Font:
    def __init__(self, pdf: _Pdf, info):
        info = info if isinstance(info, dict) else {}
        self.width = 2 if info.get("Subtype") == "Type0" else 1
        self.map: dict[bytes, str] = {}
        cmap = pdf.stream(info.get("ToUnicode"))
        if cmap:
            self._read_cmap(cmap)

    def _read_cmap(self, data: bytes) -> None:
        pos, stack, mode = 0, [], None
        widths = set()
        while True:
            value, new = _token(data, pos)
            if new <= pos or new > len(data):
                break
            pos = new
            if isinstance(value, _Op):
                word = str(value)
                if word in ("begincodespacerange", "beginbfchar", "beginbfrange"):
                    mode, stack = word[5:], []
                elif word.startswith("end"):
                    if mode == "codespacerange":
                        widths.update(len(v) for v in stack if isinstance(v, bytes))
                    elif mode == "bfchar":
                        for src, dst in zip(stack[0::2], stack[1::2]):
                            if isinstance(src, bytes) and isinstance(dst, bytes):
                                self.map[src] = _utf16(dst)
                    elif mode == "bfrange":
                        for lo, hi, dst in zip(stack[0::3], stack[1::3], stack[2::3]):
                            self._range(lo, hi, dst)
                    mode, stack = None, []
            elif mode:
                stack.append(value)
        if widths:
            self.width = max(widths)

    def _range(self, lo, hi, dst) -> None:
        if not isinstance(lo, bytes) or not isinstance(hi, bytes) or not lo:
            return
        a, b = int.from_bytes(lo, "big"), int.from_bytes(hi, "big")
        if b < a or b - a > 65535:
            return
        for i, code in enumerate(range(a, b + 1)):
            key = code.to_bytes(len(lo), "big")
            if isinstance(dst, list):
                if i < len(dst) and isinstance(dst[i], bytes):
                    self.map[key] = _utf16(dst[i])
            elif isinstance(dst, bytes) and dst:
                base = int.from_bytes(dst, "big") + i
                self.map[key] = _utf16(base.to_bytes(len(dst), "big"))

    def text(self, raw: bytes) -> str:
        if not self.map:
            if self.width == 2:
                return ""  # glyph ids with no Unicode map cannot be read
            return raw.decode("cp1252", errors="replace")
        out = []
        step = self.width
        for i in range(0, len(raw), step):
            code = raw[i:i + step]
            out.append(self.map.get(code, "" if step > 1 else code.decode("cp1252", "replace")))
        return "".join(out)


def _utf16(raw: bytes) -> str:
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _content_text(pdf: _Pdf, data: bytes, resources: dict, depth: int = 0) -> str:
    out: list[str] = []
    stack: list = []
    font = None
    last_y = None
    pos = 0
    n = len(data)

    def newline():
        if out and not out[-1].endswith("\n"):
            out.append("\n")

    while pos < n:
        value, new = _token(data, pos)
        if new <= pos:
            break
        pos = new
        if not isinstance(value, _Op):
            stack.append(value)
            continue
        op = str(value)
        if op == "ID":  # inline image data: skip to EI
            end = re.compile(rb"\sEI(?=[\s]|$)").search(data, pos)
            pos = end.end() if end else n
        elif op == "Tf" and len(stack) >= 2 and isinstance(stack[-2], _Name):
            font = pdf.font(resources, str(stack[-2]))
        elif op in ("Tj", "'", '"') and stack and isinstance(stack[-1], bytes):
            if op != "Tj":
                newline()
            out.append(font.text(stack[-1]) if font else stack[-1].decode("cp1252", "replace"))
        elif op == "TJ" and stack and isinstance(stack[-1], list):
            for item in stack[-1]:
                if isinstance(item, bytes):
                    out.append(font.text(item) if font else item.decode("cp1252", "replace"))
                elif isinstance(item, (int, float)) and item < -200:
                    out.append(" ")
        elif op in ("Td", "TD") and len(stack) >= 2 and isinstance(stack[-1], (int, float)):
            if abs(stack[-1]) > 0.5:
                newline()
            elif isinstance(stack[-2], (int, float)) and stack[-2] > 1 and out \
                    and not out[-1].endswith((" ", "\n")):
                out.append(" ")
        elif op == "T*":
            newline()
        elif op == "Tm" and len(stack) >= 6 and isinstance(stack[-1], (int, float)):
            if last_y is not None and abs(stack[-1] - last_y) > 0.5:
                newline()
            elif out and not out[-1].endswith((" ", "\n")):
                out.append(" ")
            last_y = stack[-1]
        elif op == "ET":
            newline()
        elif op == "Do" and stack and isinstance(stack[-1], _Name) and depth < 8:
            xobjects = pdf.resolve(resources.get("XObject")) or {}
            ref = xobjects.get(str(stack[-1])) if isinstance(xobjects, dict) else None
            info = pdf.resolve(ref)
            if isinstance(info, dict) and info.get("Subtype") == "Form":
                inner = pdf.resolve(info.get("Resources")) or resources
                out.append(_content_text(pdf, pdf.stream(ref), inner, depth + 1))
        stack = []
    return "".join(out)


def _pdf(data: bytes) -> str:
    if re.search(rb"/Encrypt\s", data[-4096:]) or re.search(rb"trailer[^%]*?/Encrypt", data):
        raise DocError("encrypted PDF; save an unprotected copy first")
    pdf = _Pdf(data)
    pages = pdf.pages()
    if not pages:
        raise DocError("damaged PDF or no pages found")
    out = []
    total = 0
    for i, (page, resources) in enumerate(pages, 1):
        contents = pdf.resolve(page.get("Contents"))
        refs = contents if isinstance(contents, list) else [page.get("Contents")]
        raw = b"\n".join(pdf.stream(r) for r in refs)
        text = re.sub(r"[ \t]+\n", "\n", _content_text(pdf, raw, resources)).strip()
        out.append(f"--- Page {i} ---\n{text}")
        total += len(text)
        if total > MAX_CHARS:
            break
    if total == 0:
        raise DocError("this PDF has no text layer (probably scanned images; OCR is not available)")
    return "\n\n".join(out)
