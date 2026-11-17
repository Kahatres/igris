import importlib.util
from pathlib import Path
import struct
import zlib

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("make_icons", ROOT / "tools" / "make_icons.py")
make_icons = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_icons)


def chunks(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    at = 8
    while at < len(data):
        (length,) = struct.unpack(">I", data[at:at + 4])
        kind, body = data[at + 4:at + 8], data[at + 8:at + 8 + length]
        (crc,) = struct.unpack(">I", data[at + 8 + length:at + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        yield kind, body
        at += 12 + length


def pixels(data):
    parts = dict((k, b) for k, b in chunks(data) if k != b"IDAT")
    width, height = struct.unpack(">II", parts[b"IHDR"][:8])
    raw = zlib.decompress(b"".join(b for k, b in chunks(data) if k == b"IDAT"))
    stride = width * 3 + 1
    return width, height, [raw[y * stride + 1:(y + 1) * stride] for y in range(height)]


def test_drawn_icon_is_a_valid_rgb_png_with_a_lit_eye_on_black():
    width, height, rows = pixels(make_icons.draw(64))
    assert (width, height) == (64, 64) and all(len(r) == 64 * 3 for r in rows)
    assert rows[0][:3] == b"\x00\x00\x00"                  # black corner
    assert max(rows[32]) > 200                             # the eye is bright


def test_committed_icons_match_the_generator(tmp_path):
    make_icons.main(tmp_path)
    for size in make_icons.SIZES:
        name = f"icon-{size}.png"
        assert pixels((tmp_path / name).read_bytes()) == pixels(
            (ROOT / "igris" / "static" / name).read_bytes()
        )
