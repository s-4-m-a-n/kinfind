"""Colour illustrations of photos of deceased people, with blood and injuries removed.

Responders see these instead of the original photos, which may be graphic. An image
model redraws each photo as a coloured-pencil identification illustration, keeping
tattoos, marks, hair and clothing. Each illustration gets a header (record ref and
sex) and an "approximate illustration" footer, drawn here so the text is always exact.

The model can drift (e.g. add a mole that isn't there), so illustrations are only
for viewing. Matching always uses the original photos, and the exact pencil sketch
(app/sketch.py) and the original stay one click away.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from pathlib import Path

from . import db, llm

MODEL = os.environ.get("AI_ILLUSTRATION_MODEL", "google/gemini-3.1-flash-image")
DISCLAIMER = ("Approximate illustration — not a verified reconstruction.", "Obscured details are uncertain.")

PROMPT = """Redraw this photo as a respectful identification illustration, the kind disaster-victim
identification teams publish so families can recognise someone without seeing distressing images.

Style: hand-drawn coloured-pencil illustration with soft, natural colour, on a plain white background.
Keep the same framing, pose and proportions as the photo.

Remove: blood, wounds, abrasions, swelling, discolouration, mud, silt, water, dirt, and any medical or
recovery equipment. If the photo has a scale ruler, keep it; never add one. Show clean, healthy skin in
the person's natural skin tone.

Preserve exactly, in the same position, shape, size and colours: every tattoo (including all line work
and any lettering, letter for letter), birthmarks, moles, scars, piercings, hair and any visible
clothing. If a face is visible, keep its features, with eyes closed and a calm expression.

Do not invent, complete or "improve" details that are hidden or unclear: leave them faint or unfinished
instead. Do not add moles, spots, marks or tattoos that are not clearly visible in the photo. Do not add
any text, labels, watermarks, rulers or scale bars that are not in the photo."""

FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
RETRY_AFTER = 3600  # seconds before retrying a photo whose illustration failed
_pending: set[int] = set()
_failed: dict[int, tuple[float, str]] = {}  # pid -> (when, why)
_lock = threading.Lock()


def enabled() -> bool:
    return llm.enabled()


def path_for(filename: str) -> Path:
    return db.UPLOAD_DIR / "illustration" / (Path(filename).stem + ".jpg")


def is_sensitive(conn, pid: int):
    """The photo row (with record ref/sex) if it shows a deceased person, else None."""
    return conn.execute("""SELECT p.id, p.filename, f.ref, f.sex FROM photos p
                           JOIN found_records f ON p.owner_type = 'found' AND f.id = p.owner_id
                           WHERE p.id = ? AND f.status = 'deceased'""", (pid,)).fetchone()


def redraw(src: Path) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((1536, 1536))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
    return llm.generate_image(MODEL, PROMPT, buf.getvalue())


def _font(size: int, bold: bool = True):
    from PIL import ImageFont

    for p in FONT_PATHS[0 if bold else 1:]:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def compose(drawing: bytes, ref: str, sex: str | None) -> bytes:
    """Add the header (ref, sex) and the disclaimer footer around the drawing."""
    from PIL import Image, ImageDraw

    art = Image.open(io.BytesIO(drawing)).convert("RGB")
    art.thumbnail((1200, 1200))
    w = art.width
    pad, head, foot = w // 30, w // 9, w // 9
    canvas = Image.new("RGB", (w, art.height + head + foot), "white")
    canvas.paste(art, (0, head))
    d = ImageDraw.Draw(canvas)
    big, small = _font(head // 2), _font(foot // 4, bold=False)
    d.text((pad, head // 2), ref, font=big, fill="black", anchor="lm")
    if sex in ("male", "female"):
        d.text((w - pad, head // 2), sex.capitalize(), font=big, fill="black", anchor="rm")
    y = head + art.height + foot // 3
    d.text((w // 2, y), DISCLAIMER[0], font=_font(foot // 4), fill="black", anchor="mm")
    d.text((w // 2, y + foot // 3), DISCLAIMER[1], font=small, fill="#333", anchor="mm")
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=88)
    return out.getvalue()


def ready(filename: str) -> bool:
    return path_for(filename).exists()


def ensure(pid: int) -> bool:
    """Make the illustration for one photo if needed. Safe to call repeatedly."""
    with _lock:
        if pid in _pending or time.time() - _failed.get(pid, (0, ""))[0] < RETRY_AFTER:
            return False
        _pending.add(pid)
    try:
        with db.session() as conn:
            row = is_sensitive(conn, pid)
        if not row or ready(row["filename"]) or not enabled():
            return False
        dest = path_for(row["filename"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(compose(redraw(db.UPLOAD_DIR / row["filename"]), row["ref"], row["sex"]))
        tmp.replace(dest)
        return True
    except Exception as exc:  # the exact sketch is shown instead; retried after RETRY_AFTER
        _failed[pid] = (time.time(), str(exc)[:200])
        logging.getLogger("uvicorn.error").warning("Illustration for photo %s failed: %s", pid, exc)
        return False
    finally:
        with _lock:
            _pending.discard(pid)


def ensure_in_background(pid: int) -> None:
    threading.Thread(target=ensure, args=(pid,), daemon=True).start()


def failure(pid: int) -> str | None:
    """Why the last attempt for this photo failed, if it failed recently."""
    when, why = _failed.get(pid, (0, ""))
    return why if time.time() - when < RETRY_AFTER else None
