"""Pencil-sketch versions of photos, so responders don't have to look at graphic images.

Made locally with a plain image filter (no AI): the outlines, lettering and placement of
tattoos, scars and birthmarks are kept exactly, nothing is invented, and the photo never
leaves the server. Colour and skin texture, where most graphic detail is, are removed.
Sketches are cached next to the uploads.
"""
from __future__ import annotations

from pathlib import Path

from . import db

MAX_EDGE = 1600


def make_sketch(src: Path, dest: Path, radius: float = 10, shade: float = 0.45) -> None:
    from PIL import Image, ImageChops, ImageFilter, ImageMath, ImageOps

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_EDGE, MAX_EDGE))
    gray = ImageOps.autocontrast(ImageOps.grayscale(im), cutoff=1)
    # Colour-dodge the image with its blurred inverse: flat areas go white, edges become pencil lines.
    blur = ImageOps.invert(gray).filter(ImageFilter.GaussianBlur(radius))
    lines = ImageMath.eval("convert(float(a) * 255 / (256 - float(b)), 'L')", a=gray, b=blur)
    lines = ImageOps.autocontrast(lines, cutoff=1)
    # Light grey shading so filled ink (colour tattoos, birthmarks) still reads; skin stays pale.
    tone = gray.point(lambda v: int(255 - (255 - v) * shade))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    ImageChops.multiply(lines, tone).save(tmp, "JPEG", quality=88)
    tmp.replace(dest)  # never serve a half-written file


def sketch_for(filename: str) -> Path:
    dest = db.UPLOAD_DIR / "sketch" / (Path(filename).stem + ".jpg")
    if not dest.exists():
        make_sketch(db.UPLOAD_DIR / filename, dest)
    return dest
