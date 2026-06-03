"""Import an external YOLO-format detection dataset into the belfry
fine-tune set, remapping its class ids to our COCO-aligned sparse scheme.

## Why this exists

The fine-tune plan (CLAUDE.md "Fine-tune plan", fine-tune-plan.md) says the
*new* wildlife classes (deer/coyote/raccoon/rabbit/squirrel/rat) must get
most of their diversity from external camera-trap data, not our own fixed-
angle footage — training deer on 50 of our frames teaches "deer = that spot
in the yard at dusk", not "deer". The best-matched source is Roboflow
Universe (already YOLO-boxed; e.g. the "Trailcam Detection" set covers
coyote/deer/rabbit/raccoon in IR trailcam conditions).

Every external set has its *own* dense class indices (0..N from its own
data.yaml). We remap by **name** into our sparse ids (deer=80, coyote=81,
…) read from the labeler dataset.yaml, dropping any class we don't want
(hog, turkey, vulture, …). Images land in images/+labels/ with an
`ext_<tag>_` filename prefix — which scripts/split-dataset.py's
`_OUR_FOOTAGE` regex does NOT match, so external data is included in
train/val but correctly excluded from the our-footage val metric that
reflects production performance on this scene.

## Download first (not done here — needs your Roboflow key / azcopy)

Roboflow: on the dataset page, Download → format "YOLOv8" → "download zip to
computer", unzip. You get a dir with data.yaml + train/ valid/ test/, each
holding images/ + labels/. Point --src at that dir.

## Usage

    # Inspect the source's class names, then dry-run the remap:
    python scripts/import-external.py --src ~/dl/trailcam --names-only
    python scripts/import-external.py --src ~/dl/trailcam \\
        --tag trailcam \\
        --map deer:deer,coyote:coyote,raccoon:raccoon,rabbit:rabbit \\
        --dry-run

    # Do it (only deer/coyote/raccoon/rabbit kept; hog/turkey/etc. dropped):
    python scripts/import-external.py --src ~/dl/trailcam --tag trailcam \\
        --map deer:deer,coyote:coyote,raccoon:raccoon,rabbit:rabbit

The map is `their_name:our_name` pairs (their side case-insensitive). Any
source class not in the map is dropped from every label. An image left with
zero boxes after dropping is skipped, unless --keep-empty-as-negative makes
it a hard negative (empty .txt) — useful when the dropped class is something
we never want to fire on (a turkey-only frame is a fine "not-wildlife" neg).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dvr.training import IMAGES_DIR, LABELS_DIR, ensure_dataset_yaml, load_class_map

# Roboflow exports split into train/valid/test, each with images/ + labels/.
# A flat export just has images/ + labels/ at the root. Handle both.
_SPLIT_DIRS = ("train", "valid", "val", "test", "")
_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]")


def _load_source_names(src: Path) -> dict[int, str]:
    """Parse the external set's data.yaml → {index: name}. Accepts both the
    list form (`names: [a, b]`) and the dict form (`names: {0: a, 1: b}`)."""
    yml = src / "data.yaml"
    if not yml.is_file():
        yml = src / "dataset.yaml"
    if not yml.is_file():
        raise FileNotFoundError(f"no data.yaml/dataset.yaml in {src}")
    spec = yaml.safe_load(yml.read_text())
    names = spec.get("names")
    if isinstance(names, list):
        return {i: str(n) for i, n in enumerate(names)}
    if isinstance(names, dict):
        return {int(i): str(n) for i, n in names.items()}
    raise ValueError(f"couldn't parse a names list/dict from {yml}")


def _parse_map(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in s.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(f"--map entry {pair!r} is not their_name:our_name")
        their, ours = pair.split(":", 1)
        out[their.strip().lower()] = ours.strip()
    if not out:
        raise ValueError("--map parsed to nothing")
    return out


def _iter_pairs(src: Path):
    """Yield (image_path, label_path|None) across all split subdirs."""
    seen: set[Path] = set()
    for split in _SPLIT_DIRS:
        base = src / split if split else src
        img_dir = base / "images"
        lbl_dir = base / "labels"
        if not img_dir.is_dir():
            continue
        for img in sorted(img_dir.iterdir()):
            if img.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            if img in seen:
                continue
            seen.add(img)
            lbl = lbl_dir / f"{img.stem}.txt"
            yield img, (lbl if lbl.is_file() else None)


def _remap_label(text: str, idx_to_our_id: dict[int, int]) -> list[str]:
    """Rewrite YOLO lines, keeping only mapped classes with our ids."""
    out: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue  # skip malformed / segmentation-poly lines
        try:
            src_idx = int(parts[0])
        except ValueError:
            continue
        our_id = idx_to_our_id.get(src_idx)
        if our_id is None:
            continue  # dropped class
        out.append(f"{our_id} {parts[1]} {parts[2]} {parts[3]} {parts[4]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--src", type=Path, required=True,
                    help="unzipped external YOLO dataset dir (has data.yaml)")
    ap.add_argument("--tag", default=None,
                    help="source tag for the ext_<tag>_ filename prefix "
                         "(default: --src dir name). Keep it short + stable; "
                         "re-importing the same set overwrites cleanly.")
    ap.add_argument("--map", dest="cmap", default=None,
                    help="comma list of their_name:our_name (their side "
                         "case-insensitive). Unmapped source classes dropped.")
    ap.add_argument("--names-only", action="store_true",
                    help="just print the source's class names and exit")
    ap.add_argument("--keep-empty-as-negative", action="store_true",
                    help="images left with no kept boxes become hard negatives "
                         "(empty .txt) instead of being skipped")
    ap.add_argument("--max-per-class", type=int, default=0,
                    help="cap how many imported IMAGES contain each class "
                         "(0 = no cap). Scarce classes are admitted first so an "
                         "abundant class (e.g. deer) can't crowd out a rare one "
                         "(e.g. coyote). Stops one lopsided set from dominating.")
    ap.add_argument("--max-negatives", type=int, default=0,
                    help="cap hard negatives kept (0 = no cap), evenly strided "
                         "across the source so they don't swamp the positives.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report counts, write nothing")
    args = ap.parse_args()

    src = args.src.expanduser().resolve()
    if not src.is_dir():
        print(f"--src not a directory: {src}", file=sys.stderr)
        return 1

    src_names = _load_source_names(src)
    if args.names_only:
        print(f"{src} classes:")
        for i in sorted(src_names):
            print(f"  {i}: {src_names[i]}")
        return 0

    if not args.cmap:
        print("--map is required (or use --names-only first to see the "
              "source classes)", file=sys.stderr)
        return 2

    ensure_dataset_yaml()
    our_name_to_id, _ = load_class_map()
    name_map = _parse_map(args.cmap)

    # Build {source_index: our_id}, validating both ends of every mapping.
    idx_to_our_id: dict[int, int] = {}
    src_name_lower = {n.lower(): i for i, n in src_names.items()}
    for their_name, our_name in name_map.items():
        if their_name not in src_name_lower:
            print(f"WARN: source has no class named {their_name!r}; "
                  f"known: {sorted(src_names.values())}", file=sys.stderr)
            continue
        if our_name not in our_name_to_id:
            print(f"ERROR: {our_name!r} not in dataset.yaml; "
                  f"known: {sorted(our_name_to_id)}", file=sys.stderr)
            return 2
        idx_to_our_id[src_name_lower[their_name]] = our_name_to_id[our_name]
    if not idx_to_our_id:
        print("nothing to import — no --map entry matched a source class",
              file=sys.stderr)
        return 2

    tag = _SAFE_STEM.sub("-", (args.tag or src.name).strip()) or "ext"
    print(f"importing from {src}")
    print(f"  tag: {tag}")
    print("  mapping (source idx → our id):")
    for idx, oid in sorted(idx_to_our_id.items()):
        print(f"    {idx} {src_names[idx]!r} → {oid}")

    # ---- gather candidates (no writes yet, so caps can subsample) ----
    # positives: (img, lines, classes_present); negatives: just the image.
    positives: list[tuple[Path, list[str], set[int]]] = []
    negatives: list[Path] = []
    skipped_empty = no_label = 0
    for img, lbl in _iter_pairs(src):
        lines = _remap_label(lbl.read_text(), idx_to_our_id) if lbl else []
        if lbl is None:
            no_label += 1
        if lines:
            positives.append((img, lines, {int(ln.split()[0]) for ln in lines}))
        elif args.keep_empty_as_negative:
            negatives.append(img)
        else:
            skipped_empty += 1

    avail_per_class: dict[int, int] = {}  # images containing each class, pre-cap
    for _, _, classes in positives:
        for c in classes:
            avail_per_class[c] = avail_per_class.get(c, 0) + 1

    # ---- apply caps ----
    capped = 0
    if args.max_per_class > 0:
        # Rare-first: order by the scarcest class an image carries, so
        # coyote-bearing frames are admitted before pure-deer ones. Admit
        # while ANY of an image's classes is still under cap (a multi-class
        # frame can nudge an already-full class slightly over — fine, the
        # rare class is what we're protecting).
        ordered = sorted(
            positives,
            key=lambda it: (min(avail_per_class[c] for c in it[2]), it[0].name),
        )
        counts: dict[int, int] = {}
        chosen: list[tuple[Path, list[str], set[int]]] = []
        for item in ordered:
            classes = item[2]
            if any(counts.get(c, 0) < args.max_per_class for c in classes):
                chosen.append(item)
                for c in classes:
                    counts[c] = counts.get(c, 0) + 1
        capped = len(positives) - len(chosen)
        positives = chosen
    if 0 < args.max_negatives < len(negatives):
        negatives.sort(key=lambda p: p.name)
        step = len(negatives) / args.max_negatives
        negatives = [negatives[int(i * step)] for i in range(args.max_negatives)]

    if not args.dry_run:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        LABELS_DIR.mkdir(parents=True, exist_ok=True)

    def _write(img: Path, lines: list[str]) -> None:
        stem = _SAFE_STEM.sub("-", img.stem)
        if not args.dry_run:
            # Roboflow exports are already jpg; copy bytes rather than
            # re-encode (a .png source keeps a .jpg *name* but raw bytes —
            # cv2/PIL in the training loader sniff content, not extension).
            shutil.copyfile(img, IMAGES_DIR / f"ext_{tag}_{stem}.jpg")
            (LABELS_DIR / f"ext_{tag}_{stem}.txt").write_text(
                "\n".join(lines) + ("\n" if lines else ""))

    per_class: dict[int, int] = {}
    for img, lines, _ in positives:
        _write(img, lines)
        for ln in lines:
            cid = int(ln.split()[0])
            per_class[cid] = per_class.get(cid, 0) + 1
    for img in negatives:
        _write(img, [])

    print()
    print(f"imported (with boxes): {len(positives)}")
    if negatives:
        print(f"hard negatives (empty .txt): {len(negatives)}")
    if capped:
        print(f"dropped to honor --max-per-class {args.max_per_class}: {capped}")
    print(f"skipped (no boxes after remap): {skipped_empty}")
    if no_label:
        print(f"source images with no label file: {no_label}")
    if per_class:
        _, id_to_name = load_class_map()
        names = dict((cid, n) for cid, n in id_to_name)
        print("instances added per class:")
        for cid in sorted(per_class):
            print(f"  {cid} {names.get(cid, '?')}: {per_class[cid]}")
    if args.dry_run:
        print("\n(dry run — nothing written)")
    else:
        print(f"\nwrote into {IMAGES_DIR} and {LABELS_DIR}")
        print("next: re-run scripts/split-dataset.py to regenerate the split.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
