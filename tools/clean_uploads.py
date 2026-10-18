"""Re-save photos uploaded before uploads were cleaned, removing GPS and other metadata.

    python -m tools.clean_uploads

New uploads are cleaned when they arrive (app/photos.py); this only fixes older files.
Generated sketches and illustrations are left alone: they never had metadata.
"""
from app import db, photos


def main() -> None:
    cleaned = skipped = 0
    for path in sorted(db.UPLOAD_DIR.iterdir()):
        if not path.is_file() or path.suffix not in photos.FORMATS:
            continue
        try:
            path.write_bytes(photos.clean(path.read_bytes(), path.suffix))
            cleaned += 1
        except photos.BadImage as exc:
            skipped += 1
            print(f"skipped {path.name}: {exc}")
    print(f"Cleaned {cleaned} photo(s), skipped {skipped}.")


if __name__ == "__main__":
    main()
