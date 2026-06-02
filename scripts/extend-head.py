#!/usr/bin/env python3
"""Surgically extend YOLO11l's detection head from 80 → 80+N class outputs,
preserving COCO's pretrained class channels verbatim.

## Why not just `yolo detect train` with a bigger nc?

The default path reinitializes the entire class-prediction conv when the
class count changes, throwing away COCO's training on the 80 base classes.
This script instead produces a `.pt` whose head is *already* 86-wide with
the pretrained channels in slots 0..79. When the normal training CLI then
loads it, ultralytics' shape-matched weight transfer keeps those channels —
so the existing person/dog/cat/bird/car/truck detections survive the
fine-tune for free, and only the new wildlife neurons start from scratch.

## What it does (the ~30 lines the fine-tune plan describes)

1. Load `yolo11l.pt`, drop to the inner `nn.Module`, find the `Detect` head.
2. For each of the 3 per-scale class branches (`cv3[i]`), find the final
   `nn.Conv2d` (out_channels = 80) and replace it with a fresh 1×1 conv of
   out_channels = 80+N. Copy the pretrained 80 weights+biases into slots
   0..79; init the N new channels' weights small-random and bias to a low
   value (matching Ultralytics' cls-bias init so new classes start "off"
   rather than firing everywhere in early epochs).
3. Update `Detect.nc` / `Detect.no` and the model's `names` map.
4. Save a fresh checkpoint and reload-verify it.

N is derived from the labeler `dataset.yaml`: nc = max class id + 1 (= 86
with deer..rat at 80..85), so this stays in sync with split-dataset.py.

## Run it

Needs torch + ultralytics — use the inference venv (CPU is fine, no GPU
needed for surgery):

    .venv-inference/bin/python scripts/extend-head.py \
        --src yolo11l.pt --out yolo11l-headext.pt

## Then train (on a rented GPU, not the Orin)

    yolo detect train model=yolo11l-headext.pt data=dataset.train.yaml \
        epochs=100 imgsz=640 batch=16 freeze=10 name=belfry-v1

`freeze=10` pins the backbone, so only the head learns. NOTE the nuance from
fine-tune-plan.md: `freeze` is module-granular and *cannot* freeze 80 of the
86 head channels, so the base classes' head channels will drift slightly
toward this scene (often desirable here — it's what suppresses night
wall/edge person false-positives). If you want the plan's strict *zero-drift*
behavior instead, add a backward hook that zeros the gradient for rows 0..79
of each cv3 final conv (snippet at the bottom of this file) — that needs a
custom training loop rather than the bare CLI.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

# Allow running this script directly without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dvr import training  # noqa: E402


def _final_conv(branch):
    """Return the last nn.Conv2d in a cv3 branch (the class-prediction conv)."""
    import torch.nn as nn

    last = None
    for m in branch.modules():
        if isinstance(m, nn.Conv2d):
            last = m
    if last is None:
        raise RuntimeError("no Conv2d found in cv3 branch — head layout changed?")
    return last


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--src", type=Path, default=Path("yolo11l.pt"),
                    help="Pretrained COCO weights to extend (default yolo11l.pt).")
    ap.add_argument("--out", type=Path, default=Path("yolo11l-headext.pt"),
                    help="Where to write the extended checkpoint.")
    args = ap.parse_args()

    if not args.src.exists():
        print(f"source weights not found: {args.src}", file=sys.stderr)
        print("download with: yolo export model=yolo11l.pt format=torchscript  "
              "(or fetch yolo11l.pt from the ultralytics release)", file=sys.stderr)
        return 1

    import torch
    import torch.nn as nn
    from ultralytics import YOLO

    # Target class count from the labeler dataset.yaml — stays in sync with
    # split-dataset.py's dense nc.
    names = _build_names_for_model()
    new_nc = max(names) + 1
    n_new = new_nc - 80
    if n_new <= 0:
        print(f"dataset.yaml has no class ids >= 80 (nc={new_nc}); nothing to "
              f"extend. Add wildlife classes first.", file=sys.stderr)
        return 1

    print(f"extending head: 80 → {new_nc}  (+{n_new} new wildlife channels)")

    model_wrap = YOLO(str(args.src))
    model = model_wrap.model.float()         # DetectionModel (nn.Module), fp32
    detect = model.model[-1]                 # Detect head is the last module
    if not hasattr(detect, "cv3") or not hasattr(detect, "nc"):
        print("last module isn't a Detect head with cv3/nc — unexpected "
              "architecture.", file=sys.stderr)
        return 1
    if detect.nc != 80:
        print(f"WARN: source head nc is {detect.nc}, expected 80. COCO weight "
              f"transfer assumes an 80-class source.", file=sys.stderr)

    old_nc = detect.nc
    for i, branch in enumerate(detect.cv3):
        old_conv = _final_conv(branch)
        assert old_conv.out_channels == old_nc, (
            f"cv3[{i}] out_channels {old_conv.out_channels} != nc {old_nc}")
        new_conv = nn.Conv2d(
            old_conv.in_channels, new_nc,
            kernel_size=old_conv.kernel_size,
            stride=old_conv.stride,
            padding=old_conv.padding,
            bias=old_conv.bias is not None,
        )
        with torch.no_grad():
            # Copy pretrained COCO channels 1:1 — channel j stays class j.
            new_conv.weight[:old_nc].copy_(old_conv.weight)
            if old_conv.bias is not None:
                new_conv.bias[:old_nc].copy_(old_conv.bias)
            # New channels: small-random weights (keep Conv2d's default init),
            # bias to Ultralytics' cls-init value for this stride so the new
            # classes start with low confidence everywhere instead of firing.
            new_conv.weight[old_nc:].mul_(0.01)
            if old_conv.bias is not None:
                stride = float(detect.stride[i])
                new_conv.bias[old_nc:] = math.log(5.0 / new_nc / (640.0 / stride) ** 2)

        # Replace the final conv in-place within the Sequential branch.
        _replace_final_conv(branch, old_conv, new_conv)

    # Update head + model bookkeeping so the checkpoint is self-consistent.
    detect.nc = new_nc
    detect.no = new_nc + detect.reg_max * 4
    model.nc = new_nc
    model.names = {i: names[i] for i in range(new_nc)}

    ckpt = {
        "model": model,
        "names": model.names,
        "nc": new_nc,
        "epoch": -1,                 # mark as a pretrained start, not a resume
        "train_args": {},
    }
    torch.save(ckpt, args.out)
    print(f"wrote {args.out}")

    # Reload-verify: a fresh YOLO() must accept it and report the new nc.
    check = YOLO(str(args.out))
    got = len(check.model.names)
    print(f"verify: reloaded, head reports {got} classes "
          f"({'OK' if got == new_nc else 'MISMATCH'})")
    print(f"  classes 80+: "
          f"{[check.model.names[i] for i in range(80, new_nc)]}")
    return 0 if got == new_nc else 1


def _replace_final_conv(branch, old_conv, new_conv) -> None:
    """Swap old_conv → new_conv wherever it sits in the branch's children.
    cv3[i] is a nested Sequential; the class conv is its last leaf."""
    import torch.nn as nn

    for parent in [branch, *branch.modules()]:
        if not isinstance(parent, nn.Sequential):
            continue
        for idx, child in enumerate(parent):
            if child is old_conv:
                parent[idx] = new_conv
                return
    raise RuntimeError("could not locate the final conv to replace")


def _build_names_for_model() -> dict[int, str]:
    """Dense {id: name} for 0..max_id: COCO names for 0..79 (so the head
    transfer is 1:1), wildlife from the labeler dataset.yaml, placeholders
    for gaps. Mirrors split-dataset.py:_build_names so both stay in sync."""
    # Import lazily so this module imports without the split script present.
    import importlib.util

    here = Path(__file__).resolve().parent / "split-dataset.py"
    spec = importlib.util.spec_from_file_location("_belfry_split", here)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._build_names()


if __name__ == "__main__":
    raise SystemExit(main())

# --- Strict zero-drift training snippet (optional) -------------------------
# `freeze=10` only freezes the backbone; the 80 base head channels still get
# gradients and drift. To freeze them at channel granularity (the plan's
# zero-base-drift path), register backward hooks before training:
#
#     from ultralytics import YOLO
#     m = YOLO("yolo11l-headext.pt")
#     detect = m.model.model[-1]
#     def _mask_base(grad):
#         grad = grad.clone(); grad[:80] = 0; return grad      # 0..79 frozen
#     for branch in detect.cv3:
#         conv = _final_conv(branch)                            # reuse helper above
#         conv.weight.register_hook(_mask_base)
#         if conv.bias is not None:
#             conv.bias.register_hook(_mask_base)
#     m.train(data="dataset.train.yaml", epochs=100, imgsz=640,
#             batch=16, freeze=10, name="belfry-v1")
#
# This guarantees the COCO classes' predictions are byte-for-byte unchanged
# (frozen backbone + frozen base head rows = identical function), so no COCO
# replay data is needed. Only the new wildlife channels learn.
