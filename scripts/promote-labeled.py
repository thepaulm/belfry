#!/usr/bin/env python3
"""Move reviewed staging images into the flat YOLO training tree.

For every `staging/<category>/<stem>.jpg` that has a sibling `.txt`:

  - Move the image into `images/`.
  - Move the label into `labels/`.

For images under `staging/negative_*/` that have no `.txt` and the
`--auto-empty-negatives` flag is set:

  - Write an empty `labels/<stem>.txt` (YOLO's "zero objects" sentinel).
  - Move the image into `images/`.

Filename collisions (same stem already in `images/`) are skipped with
a warning — the staging file is preserved so you can investigate.

Run from anywhere; paths are absolute via dvr.training.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running this script directly without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dvr import training  # noqa: E402


def _is_negative_dir(category: str) -> bool:
    return category.startswith("negative_")


def _move_pair(img: Path, txt: Path | None, dry_run: bool) -> tuple[Path, Path] | None:
    """Move image to IMAGES_DIR and label to LABELS_DIR. If txt is None,
    write an empty .txt at the destination (used for auto-empty
    negatives). Returns (img_dest, txt_dest) on success, None on
    skip (collision)."""
    img_dest = training.IMAGES_DIR / img.name
    txt_dest = training.LABELS_DIR / f"{img.stem}.txt"

    if img_dest.exists() or txt_dest.exists():
        print(f"  SKIP collision: {img.name} already in images/ or labels/")
        return None

    if dry_run:
        print(f"  would move:  {img}  →  {img_dest}")
        if txt is None:
            print(f"  would write: (empty)  →  {txt_dest}")
        else:
            print(f"  would move:  {txt}  →  {txt_dest}")
        return (img_dest, txt_dest)

    training.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    training.LABELS_DIR.mkdir(parents=True, exist_ok=True)
    img.rename(img_dest)
    if txt is None:
        txt_dest.write_text("")
    else:
        txt.rename(txt_dest)
    return (img_dest, txt_dest)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--auto-empty-negatives",
        action="store_true",
        help="Promote staging/negative_*/foo.jpg with no .txt by writing "
             "an empty labels/foo.txt (YOLO 'zero objects' marker).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen, don't move anything.",
    )
    args = ap.parse_args()

    if not training.STAGING_DIR.is_dir():
        print(f"no staging directory at {training.STAGING_DIR}", file=sys.stderr)
        return 1

    promoted = 0
    skipped_unlabeled = 0
    auto_negatives = 0
    collisions = 0

    for category_dir in sorted(training.STAGING_DIR.iterdir()):
        if not category_dir.is_dir():
            continue
        category = category_dir.name
        is_neg = _is_negative_dir(category)
        for img in sorted(category_dir.glob("*.jpg")):
            txt = img.with_suffix(".txt")
            if txt.exists():
                # Reviewed in labelImg — promote the pair.
                r = _move_pair(img, txt, args.dry_run)
                if r is None:
                    collisions += 1
                else:
                    promoted += 1
                continue
            # No label yet.
            if is_neg and args.auto_empty_negatives:
                r = _move_pair(img, None, args.dry_run)
                if r is None:
                    collisions += 1
                else:
                    auto_negatives += 1
                continue
            skipped_unlabeled += 1

    print()
    print(f"promoted (labeled): {promoted}")
    print(f"auto-empty negatives: {auto_negatives}")
    print(f"skipped (no .txt yet): {skipped_unlabeled}")
    print(f"collisions: {collisions}")
    if args.dry_run:
        print("(dry run — no files moved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
