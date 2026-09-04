"""Generate assets/logipair.ico (and a matching SVG) with no third-party dependency.

The mark is two rounded squares - the keyboard and the mouse - sitting on a
diagonal and joined by a thick link: one pair, kept together. It is deliberately
a silhouette rather than an illustration so it still reads at 16x16.

Geometry below is the source of truth; the SVG is emitted from the same numbers
so the two can never drift apart.

    python scripts/generate_icon.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

CANVAS = 256.0
SIZES = (16, 24, 32, 48, 64, 128, 256)

# Both device tiles, on a diagonal, plus the link that pairs them.
TILE = 92.0
RADIUS = 28.0
FIRST = (72.0, 72.0)  # centre of the upper-left tile
SECOND = (184.0, 184.0)  # centre of the lower-right tile
LINK_WIDTH = 36.0

FIRST_COLOR = (0x3B, 0x6F, 0xF5)  # indigo
SECOND_COLOR = (0x16, 0xC3, 0xA6)  # teal
LINK_COLOR = (0x2E, 0x9A, 0xD4)  # the two mixed, so the join reads as one object

Color = tuple[int, int, int]


def _rounded_rect_sdf(x: float, y: float, cx: float, cy: float, half: float, radius: float) -> float:
    qx = abs(x - cx) - (half - radius)
    qy = abs(y - cy) - (half - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - radius


def _capsule_sdf(x: float, y: float, ax: float, ay: float, bx: float, by: float, radius: float) -> float:
    pax, pay = x - ax, y - ay
    bax, bay = bx - ax, by - ay
    projection = (pax * bax + pay * bay) / (bax * bax + bay * bay)
    projection = min(1.0, max(0.0, projection))
    return math.hypot(pax - bax * projection, pay - bay * projection) - radius


def _render(size: int) -> bytes:
    """Anti-aliased RGBA buffer, painted back to front from signed distances."""
    scale = size / CANVAS
    half = TILE / 2.0
    # One pixel of feathering, expressed in canvas units, keeps small sizes crisp.
    feather = 0.5 / scale
    shapes = (
        (LINK_COLOR, lambda x, y: _capsule_sdf(x, y, *FIRST, *SECOND, LINK_WIDTH / 2.0)),
        (FIRST_COLOR, lambda x, y: _rounded_rect_sdf(x, y, *FIRST, half, RADIUS)),
        (SECOND_COLOR, lambda x, y: _rounded_rect_sdf(x, y, *SECOND, half, RADIUS)),
    )

    rows = bytearray()
    for row in range(size):
        y = (row + 0.5) / scale
        rows.append(0)  # PNG filter type: none
        for column in range(size):
            x = (column + 0.5) / scale
            red = green = blue = 0.0
            alpha = 0.0
            for color, distance in shapes:
                coverage = min(1.0, max(0.0, 0.5 - distance(x, y) / (2.0 * feather)))
                if coverage <= 0.0:
                    continue
                red = color[0] * coverage + red * (1.0 - coverage)
                green = color[1] * coverage + green * (1.0 - coverage)
                blue = color[2] * coverage + blue * (1.0 - coverage)
                alpha = coverage + alpha * (1.0 - coverage)
            rows.extend((round(red), round(green), round(blue), round(alpha * 255)))
    return bytes(rows)


def _png(size: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _dib(size: int, raw: bytes) -> bytes:
    """Classic BMP icon entry: BITMAPINFOHEADER, bottom-up BGRA, then an AND mask."""
    stride = size * 4 + 1  # each PNG scanline carries a leading filter byte
    pixels = bytearray()
    for row in reversed(range(size)):
        line = raw[row * stride + 1 : (row + 1) * stride]
        for column in range(size):
            red, green, blue, alpha = line[column * 4 : column * 4 + 4]
            pixels.extend((blue, green, red, alpha))
    # 1bpp AND mask, rows padded to 4 bytes. Zeroed: the alpha channel does the work.
    mask_stride = ((size + 31) // 32) * 4
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    return header + bytes(pixels) + bytes(mask_stride * size)


def build_ico() -> bytes:
    entries: list[tuple[int, bytes]] = []
    for size in SIZES:
        raw = _render(size)
        # PNG for 256 (the convention, and far smaller); DIB below it for maximum
        # compatibility with older icon consumers and resource compilers.
        entries.append((size, _png(size, raw) if size == 256 else _dib(size, raw)))

    offset = 6 + 16 * len(entries)
    directory = bytearray(struct.pack("<HHH", 0, 1, len(entries)))
    for size, payload in entries:
        dimension = 0 if size >= 256 else size
        directory.extend(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(payload), offset))
        offset += len(payload)
    return bytes(directory) + b"".join(payload for _, payload in entries)


def build_svg() -> str:
    def tile(center: tuple[float, float], color: Color) -> str:
        x = center[0] - TILE / 2.0
        y = center[1] - TILE / 2.0
        return (
            f'  <rect x="{x:g}" y="{y:g}" width="{TILE:g}" height="{TILE:g}" '
            f'rx="{RADIUS:g}" fill="#{color[0]:02X}{color[1]:02X}{color[2]:02X}"/>'
        )

    return "\n".join(
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS:g} {CANVAS:g}" '
            f'width="{CANVAS:g}" height="{CANVAS:g}">',
            "  <title>LogiPair</title>",
            f'  <line x1="{FIRST[0]:g}" y1="{FIRST[1]:g}" x2="{SECOND[0]:g}" y2="{SECOND[1]:g}" '
            f'stroke="#{LINK_COLOR[0]:02X}{LINK_COLOR[1]:02X}{LINK_COLOR[2]:02X}" '
            f'stroke-width="{LINK_WIDTH:g}" stroke-linecap="round"/>',
            tile(FIRST, FIRST_COLOR),
            tile(SECOND, SECOND_COLOR),
            "</svg>",
            "",
        )
    )


def main() -> None:
    assets = Path(__file__).resolve().parents[1] / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "logipair.svg").write_text(build_svg(), encoding="utf-8", newline="\n")
    ico = build_ico()
    (assets / "logipair.ico").write_bytes(ico)
    print(f"assets/logipair.ico written: {len(ico)} bytes, sizes {', '.join(str(x) for x in SIZES)}")


if __name__ == "__main__":
    main()
