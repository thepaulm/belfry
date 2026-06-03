# Fine-tune plan

> **Status: v1 SHIPPED 2026-06-03.** belfry-v1 is trained and live on the Orin
> (`inference/belfry-v1.{pt,engine}`). The plan below is what was followed —
> kept as the record and as the basis for v1.1. What actually happened:
> Roboflow "Trailcam Detection" imported via `scripts/import-external.py`
> (`--max-per-class 300`, hog dropped) on top of our footage → ~1,948 images;
> `extend-head.py` 80→86; `freeze=10` train on a rented GPU (12 min, 100 epochs);
> TRT export + swap. Verified: deer 0.95, cat 0.91, dog 0.62-0.69 (base classes
> preserved). Known-weak: coyote (59 imgs) and squirrel (9); rat is our-footage-only.
> **v1.1 candidate:** the true zero-drift path (§"Decision for v1" step 1 / §"Strategy"
> step 5) — mask gradients on the first 80 head channels — was *not* done; we used
> the simpler `freeze=10` which let base head channels drift slightly. Revisit if
> dog/cat precision ever needs to be airtight, or to top up coyote/squirrel.

See CLAUDE.md "Training data & labeler" for the staging→promoted pipeline, dataset.yaml layout, and the COCO-aligned sparse class id scheme that this plan depends on.

## Goal

Add wildlife classes (deer, coyote, raccoon, rabbit, squirrel, rat — ids 80+) that YOLO11l has no prior on, **without** degrading the existing COCO classes (person/dog/cat/bird/car/truck) we already rely on, and without retraining the whole network.

## Volume targets

Before a training run is worth attempting:

- ~50–100 promoted images per existing COCO class (person/dog/cat/bird/car/truck) to teach this-scene precision (the wall edges that fire false-positive person at night, the specific car angles in the driveway, etc.).
- ~150–300 for each new wildlife class, since YOLO11l has zero prior on them. **Most of this diversity should come from external data, not our footage** — see "External data" below.
- Net ~700–1500 images is a reasonable v1 target.

## Three ways to fine-tune, cheapest/safest → most aggressive

We are *already* not training from scratch — `model=yolo11l.pt` loads pretrained weights. The trap is the default `yolo detect train` with a changed `nc`: it **reinitializes the entire detection head** and throws away COCO's training on all 80 classes. Avoid that. The real options:

| Approach | What moves | Base-class risk | Effort |
|---|---|---|---|
| **Frozen backbone, head-only** (`freeze=10`) | Detection head only | Low | One CLI flag |
| **Head extension surgery** (chosen for v1) | New output channels appended; old 80 copied verbatim; backbone frozen | **~Zero** if only the new neurons train | ~30 lines PyTorch |
| **Full fine-tune** | Everything | High (catastrophic forgetting) | Same effort, worse outcome |

## How much does it degrade base classes?

Depends entirely on what you let move:

- **New channels only, backbone frozen** → ~0% degradation, *by construction*. The 80 existing output channels compute literally the same function from the same frozen features, so their predictions are mathematically unchanged.
- **All head channels unfrozen, backbone frozen** → the 80 existing channels drift toward *our scene's* distribution. For a fixed-camera DVR this is often **desirable, not a bug**: specializing the person channel to our scene is exactly what kills the night wall/edge false positives. The cost is generic-photo accuracy we don't care about.
- **Backbone unfrozen, no COCO replay** → real catastrophic forgetting; naive small-dataset fine-tuning can drop base mAP by tens of percent. Never do this without replay.

These are genuinely *different paths* with different data requirements, not a single knob — pick one explicitly before the run (see "Decision" below).

This assumption underwrites the frozen-backbone choice: COCO's backbone has seen dog/cat/bird/horse/sheep/cow and learned fur/leg/quadruped features that transfer, so it can almost certainly *separate* deer/coyote/raccoon without backbone adaptation. If a future new class turned out to need features COCO never learned, that's the one case that forces an unfreeze + replay.

## External data — opposite goals for new vs. existing classes

This is the part that's easy to get backwards.

**New classes (deer/coyote/raccoon/…): pull external data aggressively.** With zero prior and limited footage from one fixed angle/lighting, training on our frames alone teaches "deer = that spot in the yard at dusk" instead of "deer." External diversity is what prevents overfitting to our exact images. Sources, best domain match first:

- **LILA BC** (lila.science) — camera-trap datasets (Caltech Camera Traps, Snapshot Serengeti, etc.). *Best match*: grainy, IR-at-night, wide-angle — the same conditions the TVB-5301s produce. Deer/coyote/raccoon/rabbit all well-represented.
- **Roboflow Universe** — many surveillance/wildlife datasets already boxed in YOLO format; saves labeling.
- **iNaturalist / Open Images** — huge and diverse but mostly pristine daylight photos. Useful for shape/texture priors, but a domain gap to our IR night frames. Supplement, not bulk.

Mind the domain gap: a model trained mostly on crisp daylight web deer transfers poorly to IR-illuminated night frames. Weight toward camera-trap sources; keep our own footage in the mix as the "what this camera actually sees" anchor.

**Existing classes (person/car/…): do NOT add generic internet examples.** Here the goal is the *reverse* — lean into our exact scene to suppress false positives. Generic web persons dilute that and reintroduce the generality we're trying to specialize away. Our own footage is the asset for these classes.

**COCO replay is a third, distinct use of external data.** *If* a path unfreezes the head or backbone, mix in a slice of original COCO (a few hundred to a couple thousand images across the base classes) so gradients don't pull the old classes apart. Its job is *preservation*, not balancing. With the pure surgical "new neurons only" path the old channels never move, so replay isn't needed at all.

## Decision for v1

1. **Surgical head extension + frozen backbone, train new neurons only.** Zero base-class drift, guaranteed. No COCO replay required.
2. Wildlife data = our labeled footage **+** a few hundred camera-trap images per new class from LILA BC / Roboflow (already-boxed where possible). Let external data carry most of the diversity.
3. Keep person/car false-positive suppression in the **`class_thresholds:`** lever in `cameras.yaml`, *not* in training — it's reversible and never touches the base weights. (Likely `person: 0.55` for night wall/edge FPs, `bird: 0.30` for partial-frame.)
4. Fix the train/val split before the real run (see below) and validate on a held-out slice of *our* footage specifically, so the metric reflects our scene.
5. After swap, sanity-check via `/events` that base classes didn't regress and new ones fire.

Defer the head-unfrozen scene-specialization path (which *would* help person precision via training but needs COCO replay and risks base drift) to a v2 once v1 proves the pipeline end-to-end.

## Strategy: surgical head extension (the ~30 lines)

The Ultralytics CLI doesn't expose head surgery, but the wrapper is a thin layer over a `torch.nn.Module`, so:

1. Load `yolo11l.pt` and pull the inner `nn.Module`.
2. Find the `Detect` head (last module). It has 3 per-scale class-prediction convs; each final `nn.Conv2d` has output channels = 80 (the COCO class count).
3. For each of those convs, replace with a fresh `nn.Conv2d` whose output channels = 80 + N (N = our new wildlife classes). Copy the pretrained 80 channels' weights and biases verbatim into the first 80 slots; init the new N channels' weights freshly (small random) and biases to zero.
4. Update the head's `nc` attribute to 80+N.
5. **Freeze the backbone and the existing 80 head channels; train only the new N channels** with a low LR on the mixed dataset. (This is the v1 zero-drift path — distinct from the older "low LR on mixed COCO + our images" wording, which implied a head-drift + replay path. Pick deliberately.)
6. Export to TRT for the Orin, swap in.

The COCO-aligned id layout in `dataset.yaml` is what makes step 3's weight transfer work — channel `i` in the new conv corresponds 1:1 to channel `i` in the old. New wildlife classes take the next free id (86, 87, …) to keep the head extension a clean append.

`ultralytics` is fine for the training loop, data loading, and TRT export — we only sidestep it for the head surgery itself.

## Train/val split

`dataset.yaml` currently points both `train:` and `val:` at `images/` (fine for a smoke test, bad for a real run). At training time, generate `train.txt` and `val.txt` file lists with a deterministic hash-based 90/10 split (a `scripts/split-dataset.py` to be written then) and point `dataset.yaml` at those instead of moving any files. Keep external data and our-footage data both represented in val, but report our-footage val metrics separately — that's the number that reflects production performance.

## Where to train: NOT the Orin

The Jetson is fine for inference (~30 ms/frame TRT FP16) but training YOLO11l at a useful batch size wants 16–24 GB of GPU memory. Rent an A100 or 4090 hour on EC2 / Lambda / RunPod — ~$1–2 for a 50–100 epoch run.

```bash
# on the rented GPU box, with images/+labels/ rsync'd over.
# NOTE: this is the baseline CLI flow. For the v1 zero-drift path, the
# head surgery (above) runs first and produces the modified .pt that
# feeds `model=`; `freeze=` then pins the backbone + base head channels.
pip install ultralytics
yolo detect train model=yolo11l-headext.pt \
  data=dataset.yaml \
  epochs=100 imgsz=640 batch=16 freeze=10 \
  name=belfry-v1
# scp runs/detect/belfry-v1/weights/best.pt back to the Orin
```

## Export to TRT on the Orin

The engine is device-specific; built against the Orin's TensorRT install, won't work elsewhere.

```bash
.venv-inference/bin/yolo export model=best.pt format=engine half=True device=0
```

## Swap in

Replace the engine path in `inference/model.py` (or thread it through `cameras.yaml`'s inference block), restart `belfry-inference`. Verify via `/events` that new classes are firing and existing ones didn't regress.
