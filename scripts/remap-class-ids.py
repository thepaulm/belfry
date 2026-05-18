#!/usr/bin/env python3
"""One-time class-id remap from the old dense 0..6 scheme to the new
COCO-aligned sparse scheme. Walks every .txt under belfry-training/
and rewrites class ids in place.

Old → new:

    0 person → 0   (unchanged; COCO 0 is also person)
    1 dog    → 16  (COCO 16)
    2 cat    → 15  (COCO 15)
    3 bird   → 14  (COCO 14)
    4 car    → 2   (COCO 2)
    5 truck  → 7   (COCO 7)
    6 deer   → 80  (new, beyond COCO's 0..79)

We do this so a future fine-tune can extend YOLO11l's detection head
from 80 → 80+N classes and weight-transfer the pretrained channels
for the existing 80 classes verbatim. Aligning our ids with COCO
preserves that channel correspondence.

Defensive: if any file contains an id NOT in {0..6}, we abort with
an error — that probably means the file was already migrated (ids
already 14/15/16/etc.) or it came from somewhere else. Run with
--dry-run first to see what would change.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dvr import training  # noqa: E402

OLD_TO_NEW = {
    0: 0,    # person
    1: 16,   # dog
    2: 15,   # cat
    3: 14,   # bird
    4: 2,    # car
    5: 7,    # truck
    6: 80,   # deer
}


def remap_file(path: Path, dry_run: bool) -> tuple[int, int]:
    """Returns (lines_remapped, lines_total) for `path`."""
    raw = path.read_text()
    if not raw.strip():
        return 0, 0
    new_lines: list[str] = []
    total = 0
    changed = 0
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        total += 1
        parts = s.split()
        if len(parts) != 5:
            raise ValueError(f"{path}: bad line {ln!r} (expected 5 tokens)")
        try:
            old_id = int(parts[0])
        except ValueError:
            raise ValueError(f"{path}: non-integer class id in {ln!r}")
        if old_id not in OLD_TO_NEW:
            raise ValueError(
                f"{path}: class id {old_id} not in the old 0..6 scheme — "
                f"file may already be migrated, or came from elsewhere"
            )
        new_id = OLD_TO_NEW[old_id]
        if new_id != old_id:
            changed += 1
        new_lines.append(f"{new_id} {' '.join(parts[1:])}")
    if not dry_run and changed > 0:
        path.write_text("\n".join(new_lines) + "\n")
    return changed, total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change; don't write.")
    args = ap.parse_args()

    targets: list[Path] = []
    if training.LABELS_DIR.is_dir():
        targets.extend(sorted(training.LABELS_DIR.glob("*.txt")))
    if training.STAGING_DIR.is_dir():
        for cat_dir in sorted(training.STAGING_DIR.iterdir()):
            if not cat_dir.is_dir():
                continue
            # Skip classes.txt — that's a labelImg artifact, not a YOLO label.
            for p in sorted(cat_dir.glob("*.txt")):
                if p.name == "classes.txt":
                    continue
                targets.append(p)

    if not targets:
        print("no label files found")
        return 0

    total_changed = 0
    total_lines = 0
    files_with_changes = 0
    for p in targets:
        try:
            ch, tot = remap_file(p, args.dry_run)
        except ValueError as e:
            print(f"ABORT: {e}", file=sys.stderr)
            return 1
        if ch > 0:
            files_with_changes += 1
            print(f"  {'(dry-run) ' if args.dry_run else ''}{p}: "
                  f"{ch}/{tot} lines remapped")
        total_changed += ch
        total_lines += tot

    print()
    verb = "would remap" if args.dry_run else "remapped"
    print(f"{verb} {total_changed} of {total_lines} label lines across "
          f"{files_with_changes} of {len(targets)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
