"""Training-data staging helpers for the /api/training/capture endpoint
and the labeler.

Layout:

    /home/paulm/belfry-training/
        dataset.yaml           # Ultralytics dataset spec — source of truth
                               # for class id ↔ name (COCO-aligned sparse ids)
        staging/
            <class>/           # human-flagged class hint
                cam5_…jpg
                cam5_…txt      # pre-seeded boxes if events.db had them;
                               # absent until the labeler saves
            negative_<class>/  # hard-negative for <class>
                …
        images/                # promoted, in the training set
        labels/

Three unambiguous states:

    staging/<class>/foo.jpg          → captured, awaiting human review
    staging/<class>/foo.jpg + .txt   → reviewed, ready to promote
    images/foo.jpg + labels/foo.txt  → in the training set

Class ids are COCO-aligned (sparse 0, 2, 7, 14, 15, 16, 80, …) so a
future fine-tune can extend YOLO11l's head from 80 → 80+N outputs
and weight-transfer the pretrained channels for the existing
classes verbatim.
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

# Defaults written into dataset.yaml when it doesn't exist yet. Sparse
# COCO-aligned ids — see the module docstring for the why. Edit
# dataset.yaml on disk to add classes; we always read from there at
# runtime, so this list is *only* the seed for a fresh install.
_DEFAULT_CLASSES: list[tuple[int, str]] = [
    (0,  "person"),
    (2,  "car"),
    (7,  "truck"),
    (14, "bird"),
    (15, "cat"),
    (16, "dog"),
    (80, "deer"),
    (81, "coyote"),
    (82, "raccoon"),
    (83, "rabbit"),
]

_DATASET_YAML_TEMPLATE = """\
# Belfry fine-tune dataset spec (Ultralytics format).
# Source of truth for class id ↔ name.
#
# IDs are COCO-aligned (not dense 0..N) so a future fine-tune can extend
# YOLO11l's head from 80 → 80+N outputs and weight-transfer the
# pretrained channels for the existing classes verbatim. Sparse ids are
# fine for Ultralytics — it skips unused slots during training.

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
        f"  {cid}: {name}" for cid, name in _DEFAULT_CLASSES
    )
    DATASET_YAML.write_text(
        _DATASET_YAML_TEMPLATE.format(root=TRAINING_ROOT, names_block=names_block)
    )


def load_class_map() -> tuple[dict[str, int], list[tuple[int, str]]]:
    """Parse dataset.yaml's `names:` block. Returns:

      * `name_to_id` — `{"person": 0, "car": 2, ...}`
      * `id_to_name` — `[(0, "person"), (2, "car"), ...]` sorted by id

    `id_to_name` is a list of `(id, name)` tuples (not a flat list of
    names) because our ids are *sparse* — array index ≠ class id once
    we go COCO-aligned. Consumers that need to display class options
    in a UI iterate the list and use the real id, not the index.

    The YAML is hand-edited and tiny, so we parse the names section
    line-by-line rather than pulling in a PyYAML dependency. Accepted
    forms: `  0: person` (dict-style, sparse-id-friendly) or `  - foo`
    (list-style, dense-only — legacy).
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
    id_to_name = [(i, by_id[i]) for i in sorted(by_id)]
    name_to_id = {n: i for i, n in by_id.items()}
    return name_to_id, id_to_name


def bbox_to_yolo_line(bbox: list[float], cls_id: int) -> str:
    """Convert events.db's [x1,y1,x2,y2] in 0..1 to a YOLO label line.

    YOLO format per line: `<cls_id> <cx> <cy> <w> <h>` with all four
    geometry values normalized 0..1.
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
    not necessarily at `ts` — the human re-tightens it in the labeler.
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
