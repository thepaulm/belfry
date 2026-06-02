#!/usr/bin/env python3
"""Generate a deterministic train/val split + a training-time dataset yaml.

The labeler's `dataset.yaml` (dvr/training.py) points both `train:` and
`val:` at `images/` — fine for a smoke test, wrong for a real run (you'd
validate on your training data). This script produces the artifacts a
real fine-tune needs, *without moving any files*:

  belfry-training/
    train.txt            # absolute image paths, ~90%
    val.txt              # absolute image paths, ~10%
    dataset.train.yaml   # nc=86 full-names spec, points at the .txt lists

Ultralytics resolves each image's label by swapping `/images/` → `/labels/`
and `.jpg` → `.txt` in the path, so the flat images/+labels/ tree just works
from a file-list.

## Why a separate training yaml with nc=86

The labeler yaml lists only the classes we actually label (the sparse
COCO-aligned ids 0,2,7,14,15,16 plus wildlife 80+). Ultralytics derives
`nc = len(names)`, so a sparse 12-entry dict would give nc=12 — but a label
referencing class id 85 then points past the end of a 12-wide head and the
loader rejects it. The fix the "COCO-aligned sparse ids" scheme assumes is:
declare a *dense* nc = max_id + 1 (= 86) with names for every slot 0..85.
COCO's 80 names fill 0..79 (so the head-extension weight transfer lines up
1:1), wildlife fills 80..85. The unused COCO slots (bicycle, toaster, …)
simply never get a positive label, so their neurons only ever see
background — "skipped" exactly as intended.

## Deterministic split

A given image always lands in the same bucket (sha1(stem) mod 10000), so
re-running after adding images keeps prior assignments stable — no churn
that would leak train images into val across runs.

## Our-footage vs external

Filenames matching `<cam>_<YYYYmmddTHHMMSS>_<uuid>` are our own frames;
anything else (LILA BC, Roboflow, etc.) is external. We report a val count
for our-footage *only* — that's the number that reflects production
performance on this scene, separate from external-data diversity.

Run from anywhere; paths are absolute via dvr.training.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

# Allow running this script directly without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dvr import training  # noqa: E402

# Canonical COCO-80 class names in id order. These fill head slots 0..79 so
# the surgical head extension (scripts/extend-head.py) can copy the pretrained
# channels verbatim — channel i here must mean the same class as channel i in
# yolo11l.pt. Do not reorder.
COCO80 = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

# `<cam>_<YYYYmmddTHHMMSS>_<uuid>.jpg` — our own captured/staged frames.
_OUR_FOOTAGE = re.compile(r"^cam\d+_\d{8}T\d{6}_")


def _bucket(stem: str) -> int:
    """Stable 0..9999 bucket for an image stem. Hash, not RNG, so the
    assignment is identical across runs as the dataset grows."""
    h = hashlib.sha1(stem.encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 10000


def _build_names() -> dict[int, str]:
    """Dense {id: name} covering 0..max_id. COCO names for 0..79, wildlife
    (>=80) from the labeler dataset.yaml, placeholders for any gap."""
    _, id_to_name = training.load_class_map()
    names: dict[int, str] = {i: COCO80[i] for i in range(80)}
    for cid, name in id_to_name:
        if cid >= 80:
            names[cid] = name
        elif cid < 80 and COCO80[cid] != name:
            print(
                f"  WARN: dataset.yaml id {cid} is '{name}' but COCO {cid} is "
                f"'{COCO80[cid]}'. Head transfer assumes COCO alignment — "
                f"keeping COCO name.",
                file=sys.stderr,
            )
    nc = max(names) + 1
    for i in range(80, nc):
        names.setdefault(i, f"class_{i}")  # named gap so the yaml is dense
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--val-frac", type=float, default=0.10,
        help="Fraction of images held out for validation (default 0.10).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report the split, don't write any files.",
    )
    args = ap.parse_args()

    if not training.IMAGES_DIR.is_dir():
        print(f"no images directory at {training.IMAGES_DIR}", file=sys.stderr)
        print("promote some labeled frames first (labeler, or "
              "scripts/promote-labeled.py).", file=sys.stderr)
        return 1

    val_cutoff = int(round(args.val_frac * 10000))

    train_imgs: list[Path] = []
    val_imgs: list[Path] = []
    missing_label = 0
    our_total = our_val = 0

    for img in sorted(training.IMAGES_DIR.glob("*.jpg")):
        label = training.LABELS_DIR / f"{img.stem}.txt"
        if not label.exists():
            # An empty .txt is a valid hard negative; a *missing* one means
            # the image was never labeled and can't be trained on.
            print(f"  WARN: no label for {img.name}, skipping", file=sys.stderr)
            missing_label += 1
            continue
        is_val = _bucket(img.stem) < val_cutoff
        (val_imgs if is_val else train_imgs).append(img)
        if _OUR_FOOTAGE.match(img.name):
            our_total += 1
            if is_val:
                our_val += 1

    if not train_imgs and not val_imgs:
        print("no labeled images found.", file=sys.stderr)
        return 1

    names = _build_names()
    nc = max(names) + 1
    train_txt = training.TRAINING_ROOT / "train.txt"
    val_txt = training.TRAINING_ROOT / "val.txt"
    yaml_path = training.TRAINING_ROOT / "dataset.train.yaml"

    if not args.dry_run:
        train_txt.write_text("".join(f"{p}\n" for p in train_imgs))
        val_txt.write_text("".join(f"{p}\n" for p in val_imgs))
        names_block = "\n".join(f"  {i}: {names[i]}" for i in range(nc))
        yaml_path.write_text(
            "# Generated by scripts/split-dataset.py — do not hand-edit.\n"
            "# Dense nc so sparse COCO-aligned label ids stay in range; see\n"
            "# the script docstring for why this differs from the labeler yaml.\n"
            f"path: {training.TRAINING_ROOT}\n"
            f"train: {train_txt.name}\n"
            f"val: {val_txt.name}\n"
            f"nc: {nc}\n"
            "names:\n"
            f"{names_block}\n"
        )

    print()
    print(f"train: {len(train_imgs)}   val: {len(val_imgs)}   "
          f"(val_frac target {args.val_frac:.0%})")
    print(f"our-footage val: {our_val} / {our_total}  "
          f"← the production-relevant metric; report this separately")
    print(f"nc: {nc}  ({sum(1 for i in range(80, nc))} wildlife slots)")
    if missing_label:
        print(f"skipped (no label .txt): {missing_label}")
    if args.dry_run:
        print("(dry run — nothing written)")
    else:
        print(f"\nwrote {train_txt}")
        print(f"wrote {val_txt}")
        print(f"wrote {yaml_path}")
        print("\nnext: scripts/extend-head.py to build the 86-class .pt, then")
        print(f"  yolo detect train model=yolo11l-headext.pt data={yaml_path} \\")
        print("    epochs=100 imgsz=640 batch=16 freeze=10 name=belfry-v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
