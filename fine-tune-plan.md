# Fine-tune plan (deferred until enough data)

Tooling is in place; what's left is data volume + the actual training run. Not blocked on code today.

See CLAUDE.md "Training data & labeler" for the staging→promoted pipeline, dataset.yaml layout, and the COCO-aligned sparse class id scheme that this plan depends on.

**Volume targets** before training is worth attempting:
- ~50–100 promoted images per existing COCO class (person/dog/cat/bird/car/truck) to teach this-scene precision (the wall edges that fire false-positive person at night, the specific car angles in the driveway, etc.).
- ~150–300 for deer (or any other new wildlife class), since YOLO11l has zero prior on them.
- Net ~700–1500 images is a reasonable v1 target.

**Strategy: surgical head extension** (chosen approach, see comments in `dataset.yaml`)

Don't use Ultralytics' default `yolo detect train` against a different class count — that reinitializes the entire detection head and throws away COCO's training on the 80 base classes. Instead, drop one level deeper into PyTorch:

1. Load `yolo11l.pt` and pull the inner `nn.Module`.
2. Find the `Detect` head (last module). It has 3 per-scale class-prediction convs; each final `nn.Conv2d` has output channels = 80 (the COCO class count).
3. For each of those convs, replace with a fresh `nn.Conv2d` whose output channels = 80 + N (where N = our new wildlife classes: deer, coyote, raccoon → N=3, so 83). Copy the pretrained 80 channels' weights and biases verbatim into the first 80 slots; init the new N channels' weights freshly (small random) and biases to zero.
4. Update the head's `nc` attribute to 80+N.
5. Train with the **backbone frozen** and a low LR on a mixed dataset (slice of COCO + our images) so the existing 80 classes don't drift. Only the new head neurons see meaningful gradients.
6. Export to TRT for the Orin, swap in.

This gives us a single-model inference path (no separate wildlife detector to merge), preserves COCO-class performance for free, and only needs enough data to teach the new classes. The COCO-aligned id layout in `dataset.yaml` is what makes step 3's weight transfer work — channel `i` in the new conv corresponds 1:1 to channel `i` in the old.

The Ultralytics CLI doesn't expose this directly, but the wrapper is just a thin layer over a `torch.nn.Module`, so the head-replacement is ~30 lines of PyTorch. The `ultralytics` package itself is fine to use for the training loop, data loading, and TRT export — we only sidestep it for the head surgery.

**Train/val split** — `dataset.yaml` currently points both `train:` and `val:` at `images/` (fine for a smoke test, bad for a real run). At training time, generate `train.txt` and `val.txt` file lists with a deterministic hash-based 90/10 split (a `scripts/split-dataset.py` to be written then) and point `dataset.yaml` at those instead of moving any files.

**Where to train: NOT the Orin.** The Jetson is fine for inference (~30 ms/frame TRT FP16) but training YOLO11l at a useful batch size wants 16–24 GB of GPU memory. Rent an A100 or 4090 hour on EC2 / Lambda / RunPod — ~$1–2 for a 50–100 epoch run. The flow:

```bash
# on the rented GPU box, with images/+labels/ rsync'd over:
pip install ultralytics
yolo detect train model=yolo11l.pt \
  data=dataset.yaml \
  epochs=100 imgsz=640 batch=16 \
  name=belfry-v1
# scp runs/detect/belfry-v1/weights/best.pt back to the Orin
```

**Export to TRT on the Orin** (the engine is device-specific; built against the Orin's TensorRT install, won't work elsewhere):

```bash
.venv-inference/bin/yolo export model=best.pt format=engine half=True device=0
```

**Swap in.** Replace the engine path in `inference/model.py` (or thread it through `cameras.yaml`'s inference block), restart `belfry-inference`. Verify via `/events` that new classes are firing and existing ones didn't regress.
