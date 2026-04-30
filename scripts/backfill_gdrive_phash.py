"""Backfill perceptual_hash for Google Drive image files that have a cached thumbnail.

Does not download anything — only computes pHash from already-cached thumbnails.
"""

import pathlib
import sqlite3
import sys

THUMBS_DIR = pathlib.Path.home() / ".magpie" / "thumbs"
DB_PATH = pathlib.Path.home() / ".magpie" / "magpie.db"


def main() -> None:
    db_path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT f.id
        FROM files f
        WHERE f.perceptual_hash IS NULL
          AND f.status = 'present'
          AND (f.mime_type LIKE 'image/%'
               OR f.extension IN ('jpg','jpeg','png','gif','bmp','tiff','tif','webp','heic','heif'))
          AND EXISTS (
              SELECT 1 FROM file_metadata fm
              WHERE fm.file_id = f.id AND fm.key = 'gdrive_file_id'
          )
        """
    ).fetchall()

    total = len(rows)
    print(f"Found {total:,} gdrive images missing pHash")

    from magpie.thumbs import compute_phash

    updated = skipped = failed = 0
    for i, row in enumerate(rows, 1):
        file_id = row["id"]
        thumb_path = THUMBS_DIR / f"{file_id}.jpg"

        if not thumb_path.exists():
            skipped += 1
            continue

        phash = compute_phash(thumb_path)
        if phash:
            conn.execute(
                "UPDATE files SET perceptual_hash = ? WHERE id = ?",
                (phash, file_id),
            )
            updated += 1
        else:
            failed += 1

        if i % 1000 == 0:
            conn.commit()
            print(f"  {i:,}/{total:,}  updated={updated:,}  skipped={skipped:,}  failed={failed:,}")

    conn.commit()
    conn.close()
    print(f"\nDone. updated={updated:,}  skipped (no thumb)={skipped:,}  failed={failed:,}")


if __name__ == "__main__":
    main()
