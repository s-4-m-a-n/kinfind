"""Clean uploaded photos before they are stored.

Phones write the place a photo was taken (GPS), the camera and sometimes the owner's name
into the file. Kinfind serves some photos to other people (public appeals, tips shown to
families), so every upload is re-saved from its pixels alone, without that metadata.
Re-saving also caps the size, which stops "decompression bombs": small files that expand
to an image too large to hold in memory.
"""
from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps

MAX_PIXELS = 40_000_000  # 40 megapixels: more than any phone camera
MAX_EDGE = 4096          # stored photos are scaled down to this, which keeps tattoo detail
FORMATS = {".jpg": "JPEG", ".png": "PNG", ".webp": "WEBP"}


class BadImage(ValueError):
    pass


def clean(data: bytes, ext: str) -> bytes:
    """The photo re-encoded as `ext` with no metadata. Raises BadImage if it can't be used."""
    fmt = FORMATS[ext]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:  # reads the header only
                if im.width * im.height > MAX_PIXELS:
                    raise BadImage(f"is {im.width}×{im.height} pixels; the limit is {MAX_PIXELS // 1_000_000} megapixels")
                im = ImageOps.exif_transpose(im)  # keep it upright once the orientation tag is gone
                im.thumbnail((MAX_EDGE, MAX_EDGE))
                mode = "RGB" if fmt == "JPEG" or im.mode not in ("RGBA", "LA", "PA", "P") else "RGBA"
                pixels = Image.new(mode, im.size)
                pixels.paste(im.convert(mode))  # a fresh image: nothing from the original file's info comes along
    except BadImage:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise BadImage(f"has too many pixels; the limit is {MAX_PIXELS // 1_000_000} megapixels") from exc
    except Exception as exc:  # not an image, truncated, or a decompression bomb
        raise BadImage("could not be read as an image") from exc
    out = io.BytesIO()
    pixels.save(out, fmt, **({"quality": 90} if fmt in ("JPEG", "WEBP") else {"optimize": True}))
    return out.getvalue()
