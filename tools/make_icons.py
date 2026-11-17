"""Draw the Igris app icons (the v2 eye on black) as PNG files, standard library only.

    python tools/make_icons.py            # writes igris/static/icon-192.png and icon-512.png

The eye sits inside the central 80 % circle, so the same files work as maskable icons.
"""

import math
from pathlib import Path
import struct
import sys
import zlib

STATIC = Path(__file__).resolve().parent.parent / "igris" / "static"
SIZES = (192, 512)
EYE_A, EYE_B = (0x5D, 0xFF, 0xD0), (0x2F, 0xFF, 0x6A)  # --eye-a, --eye-b in v2.css


def png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _cover(sd: float, px: float) -> float:
    """Anti-aliased coverage of a shape from its signed distance (negative inside)."""
    return min(1.0, max(0.0, 0.5 - sd / px))


def draw(size: int) -> bytes:
    c = size / 2
    a, b = size * 0.31, size * 0.15            # half width and half height of the eye
    radius = (a * a + b * b) / (2 * b)          # the eye is the lens of two circles
    offset = radius - b
    stroke, iris, pupil, glow = size * 0.022, size * 0.085, size * 0.038, size * 0.05
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx, dy = x + 0.5 - c, y + 0.5 - c
            lens = max(math.hypot(dx, dy - offset), math.hypot(dx, dy + offset)) - radius
            ring = abs(math.hypot(dx, dy) - iris) - stroke * 0.8
            dot = math.hypot(dx, dy) - pupil
            edge = abs(lens) - stroke / 2
            ink = max(_cover(edge, 1), _cover(ring, 1), _cover(dot, 1))
            near = min(edge, ring, dot)
            light = max(ink, 0.55 * math.exp(-max(near, 0) / glow))
            t = min(1.0, max(0.0, (dx + a) / (2 * a)))
            row += bytes(round((EYE_A[i] * (1 - t) + EYE_B[i] * t) * light) for i in range(3))
        rows.append(bytes(row))
    return png(size, size, rows)


def main(target: Path = STATIC) -> None:
    for size in SIZES:
        (target / f"icon-{size}.png").write_bytes(draw(size))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else STATIC)
