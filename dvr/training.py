"""Training-data staging helpers for the /api/training/capture endpoint
and the promote-labeled script.

Layout:

    /home/paulm/belfry-training/
        dataset.yaml           # Ultralytics dataset spec (source of truth
                               # for class name ↔ id mapping)
        staging/
            <class>/           # human-flagged class hint
                cam5_…jpg
                cam5_…txt      # pre-seeded boxes if events.db had them;
                               # absent until labelImg saves
                classes.txt    # labelImg's per-dir class list
            negative_<class>/  # hard-negative for <class>
                …
        images/                # promoted, in the training set
        labels/

Three unambiguous states:

    staging/<class>/foo.jpg          → captured, awaiting human review
    staging/<class>/foo.jpg + .txt   → reviewed, ready to promote
    images/foo.jpg + labels/foo.txt  → in the training set
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

TRAINING_ROOT = Path("/home/paulm/belfry-training")
STAGING_DIR = TRAINING_ROOT / "staging"
IMAGES_DIR = TRAINING_ROOT / "images"
LABELS_DIR = TRAINING_ROOT / "labels"
DATASET_YAML = TRAINING_ROOT / "dataset.yaml"

# Source of truth for class id ↔ name. Edit dataset.yaml on disk to add
# classes; we read from there at runtime. This default is written on
# first capture if the file doesn't exist yet.
#
# Ordering: COCO subset that YOLO11l already knows about (so events.db
# pre-seed boxes round-trip cleanly into the same ids), followed by
# fine-tune-only classes that the base model has no concept of.
_DEFAULT_CLASS_NAMES = [
    "person",   # 0
    "dog",      # 1
    "cat",      # 2
    "bird",     # 3
    "car",      # 4
    "truck",    # 5
    "deer",     # 6 — wildlife fine-tune target, not in base YOLO11l
]

_DATASET_YAML_TEMPLATE = """\
# Belfry fine-tune dataset spec (Ultralytics format).
# Source of truth for class id ↔ name; edit here, then re-run any
# captures that should be re-seeded.

path: {root}
train: images
val: images   # no train/val split yet — pick one once we have >~50 imgs/class

names:
{names_block}
"""


def ensure_dataset_yaml() -> None:
    """Write a default dataset.yaml if absent. Cheap to call on every
    request; the .exists() check short-circuits."""
    TRAINING_ROOT.mkdir(parents=True, exist_ok=True)
    if DATASET_YAML.exists():
        return
    names_block = "\n".join(
        f"  {i}: {name}" for i, name in enumerate(_DEFAULT_CLASS_NAMES)
    )
    DATASET_YAML.write_text(
        _DATASET_YAML_TEMPLATE.format(root=TRAINING_ROOT, names_block=names_block)
    )


def load_class_map() -> tuple[dict[str, int], list[str]]:
    """Parse dataset.yaml's `names:` block. Returns (name→id, id→name).

    The YAML is hand-edited and tiny, so we parse the names section
    line-by-line rather than pulling in a PyYAML dependency. Format
    accepted: `  0: person` (dict-style) or `  - person` (list-style).
    """
    ensure_dataset_yaml()
    text = DATASET_YAML.read_text()
    in_names = False
    by_id: dict[int, str] = {}
    list_idx = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith("names:"):
            in_names = True
            continue
        if in_names:
            if not line.startswith((" ", "\t")):
                break  # left the names block
            body = line.strip()
            if body.startswith("- "):
                by_id[list_idx] = body[2:].strip()
                list_idx += 1
            elif ":" in body:
                k, v = body.split(":", 1)
                try:
                    by_id[int(k.strip())] = v.strip()
                except ValueError:
                    continue
    id_to_name = [by_id[i] for i in sorted(by_id)]
    name_to_id = {n: i for i, n in by_id.items()}
    return name_to_id, id_to_name


def bbox_to_yolo_line(bbox: list[float], cls_id: int) -> str:
    """Convert events.db's [x1,y1,x2,y2] in 0..1 to a YOLO label line.

    YOLO format per line: `<cls_id> <cx> <cy> <w> <h>` with all four
    geometry values normalized 0..1. Six decimal places — labelImg
    writes that, so re-saving doesn't churn whitespace.
    """
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    w = max(0.0, x2 - x1)
    h = max(0.0, y2 - y1)
    return f"{cls_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"


def seed_labels_for_capture(
    db_path: Path, cam: str, ts: float, class_map: dict[str, int]
) -> list[str]:
    """Look up events.db rows whose [ts_start, ts_end] straddles ts on
    this camera, and return YOLO label lines for the ones whose class
    is in the dataset's class map. Empty list if the DB doesn't exist,
    or no events overlap, or none match a known class.

    The peak_bbox is the position at the moment of peak confidence,
    not necessarily at `ts` — the human re-tightens it in labelImg.
    """
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            "SELECT class, peak_bbox FROM events "
            "WHERE camera = ? AND ts_start <= ? AND ts_end >= ?",
            (cam, ts, ts),
        ).fetchall()
    finally:
        conn.close()
    lines: list[str] = []
    for cls, peak_bbox_json in rows:
        cls_id = class_map.get(cls)
        if cls_id is None:
            continue  # legacy aggregate or motion class — no fine-tune target
        try:
            bbox = json.loads(peak_bbox_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not (isinstance(bbox, list) and len(bbox) == 4):
            continue
        lines.append(bbox_to_yolo_line(bbox, cls_id))
    return lines


def write_classes_txt(target_dir: Path, id_to_name: list[str]) -> None:
    """Drop labelImg's per-dir classes.txt so opening the folder in the
    GUI shows the right class names without manual setup."""
    p = target_dir / "classes.txt"
    expected = "\n".join(id_to_name) + "\n"
    if p.exists() and p.read_text() == expected:
        return
    p.write_text(expected)
