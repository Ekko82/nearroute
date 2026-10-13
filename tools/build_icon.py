"""Build NearRoute's original sailing-boat icon and optional preview sheets.

Build-only dependency: Pillow (``python -m pip install Pillow``).
Run from any directory: ``python tools/build_icon.py``.
The application only needs the generated assets/nearroute.ico at runtime.

The geometry below is the canonical editable source. The SVG export shares the
same coordinates and colors. Small ICO entries use ordinary 32-bit DIB images
with an AND mask; the 256 px entry is a PNG. No fonts or external images are
required to build the icon.
"""

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
import struct

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
BLUE = (24, 96, 192)
TEAL = (4, 119, 142)
RIM = (109, 207, 224)
WHITE = (255, 255, 255)
PAD = 8
RADIUS = 54
RIM_WIDTH = 4
MAIN_SAIL = ((126, 43), (61, 150), (126, 150))
FRONT_SAIL = ((146, 82), (194, 150), (146, 150))
HULL = ((53, 170), (205, 170), (180, 205), (82, 205))


def color_hex(color):
    return "#" + "".join(f"{component:02x}" for component in color)


def render_icon(size: int) -> Image.Image:
    """Render every size separately with supersampling, preserving small detail."""
    oversample = 8 if size <= 48 else 4
    side = size * oversample
    scale = side / 256
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    pixels = image.load()
    for y in range(side):
        for x in range(side):
            fraction = (x + y) / (2 * (side - 1))
            pixels[x, y] = tuple(round(first + (second - first) * fraction) for first, second in zip(BLUE, TEAL)) + (255,)
    mask = Image.new("L", (side, side), 0)
    bounds = tuple(value * scale for value in (PAD, PAD, 256 - PAD, 256 - PAD))
    ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=RADIUS * scale, fill=255)
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(bounds, radius=RADIUS * scale, outline=RIM + (255,), width=max(1, round(RIM_WIDTH * scale)))
    for polygon in (MAIN_SAIL, FRONT_SAIL, HULL):
        draw.polygon([(x * scale, y * scale) for x, y in polygon], fill=WHITE + (255,))
    return image.resize((size, size), Image.Resampling.LANCZOS)


def svg_source() -> str:
    points = lambda polygon: " ".join(f"{x},{y}" for x, y in polygon)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" role="img" aria-labelledby="title description">
  <title id="title">NearRoute</title>
  <desc id="description">A white sailing boat on a blue and teal rounded square, with transparent outer corners.</desc>
  <defs>
    <linearGradient id="sea" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{color_hex(BLUE)}"/>
      <stop offset="1" stop-color="{color_hex(TEAL)}"/>
    </linearGradient>
  </defs>
  <rect x="{PAD}" y="{PAD}" width="{256 - PAD * 2}" height="{256 - PAD * 2}" rx="{RADIUS}" fill="url(#sea)" stroke="{color_hex(RIM)}" stroke-width="{RIM_WIDTH}"/>
  <g fill="#ffffff">
    <polygon points="{points(MAIN_SAIL)}"/>
    <polygon points="{points(FRONT_SAIL)}"/>
    <polygon points="{points(HULL)}"/>
  </g>
</svg>
'''


def dib_icon(image: Image.Image) -> bytes:
    """Encode an ICO DIB with straight BGRA pixels and a 1-bit transparency mask."""
    width, height = image.size
    flipped = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    color_data = flipped.tobytes("raw", "BGRA")
    alpha = flipped.getchannel("A")
    mask_stride = ((width + 31) // 32) * 4
    mask = bytearray(mask_stride * height)
    for y in range(height):
        for x in range(width):
            if alpha.getpixel((x, y)) == 0:
                mask[y * mask_stride + x // 8] |= 0x80 >> (x % 8)
    header = struct.pack(
        "<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0,
        len(color_data) + len(mask), 0, 0, 0, 0,
    )
    return header + color_data + bytes(mask)


def write_ico(path: Path, images: dict[int, Image.Image]) -> None:
    entries = []
    payloads = []
    offset = 6 + 16 * len(images)
    for size, image in images.items():
        if size == 256:
            buffer = BytesIO()
            image.save(buffer, format="PNG", optimize=False)
            payload = buffer.getvalue()
        else:
            payload = dib_icon(image)
        dimension = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(payload), offset))
        payloads.append(payload)
        offset += len(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + b"".join(entries) + b"".join(payloads))


def previews(directory: Path, images: dict[int, Image.Image]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for size, image in images.items():
        image.save(directory / f"nearroute-{size}.png")
    sheet = Image.new("RGB", (1220, 790), "#e5e9ef")
    draw = ImageDraw.Draw(sheet)
    heading = ImageFont.load_default(size=27)
    body = ImageFont.load_default(size=16)
    draw.text((28, 20), "NearRoute - original icon / actual pixel sizes", font=heading, fill="#203047")
    for top, background, text in ((80, "#19212b", "#e9f2ff"), (430, "#fafbfd", "#203047")):
        draw.rounded_rectangle((24, top, 1196, top + 330), radius=12, fill=background)
        draw.text((44, top + 18), "Dark taskbar" if top == 80 else "Light taskbar", font=body, fill=text)
        x = 54
        for size, icon in images.items():
            y = top + 64 + (256 - size) // 2
            sheet.paste(icon, (x, y), icon)
            draw.text((x, top + 302), str(size) + " px", font=body, fill=text)
            x += size + 34
    sheet.save(directory / "nearroute-preview.png")

    detail = Image.new("RGB", (1056, 410), "#e5e9ef")
    draw = ImageDraw.Draw(detail)
    draw.text((24, 16), "Small icon detail - nearest-neighbor magnification", font=heading, fill="#203047")
    for index, size in enumerate((16, 20, 24, 32)):
        x = 24 + index * 258
        for top, background in ((64, "#19212b"), (234, "#fafbfd")):
            draw.rounded_rectangle((x, top, x + 238, top + 152), radius=8, fill=background)
            icon = images[size].resize((size * 4, size * 4), Image.Resampling.NEAREST)
            detail.paste(icon, (x + (238 - icon.width) // 2, top + (152 - icon.height) // 2), icon)
        draw.text((x + 12, 214), str(size) + " px at 4x", font=body, fill="#203047")
    detail.save(directory / "nearroute-small-detail.png")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the NearRoute sailing-boat application icon")
    parser.add_argument("--output", type=Path, default=ROOT / "assets" / "nearroute.ico")
    parser.add_argument("--preview-dir", type=Path, default=ROOT / "state" / "icon-preview")
    parser.add_argument("--no-preview", action="store_true")
    args = parser.parse_args()
    images = {size: render_icon(size) for size in SIZES}
    write_ico(args.output, images)
    args.output.with_suffix(".svg").write_text(svg_source(), encoding="utf-8")
    with Image.open(args.output) as decoded:
        if decoded.ico.sizes() != {(size, size) for size in SIZES}:
            raise RuntimeError("ICO size verification failed")
        for size in SIZES:
            frame = decoded.ico.getimage((size, size)).convert("RGBA")
            if frame.tobytes() != images[size].tobytes():
                raise RuntimeError(f"ICO transparency/pixel round-trip failed at {size} px")
    if not args.no_preview:
        previews(args.preview_dir, images)
    print(f"Built {args.output} with sizes: {', '.join(map(str, SIZES))}")
    if not args.no_preview:
        print(f"Preview sheets: {args.preview_dir}")


if __name__ == "__main__":
    main()
