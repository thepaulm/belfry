# Motion detection + fine-tuning data pipeline

## Context

YOLO11l on COCO covers `person/dog/cat/bird/car/truck` and nothing else. The
`recordings/` are full of things YOLO can't see — deer, raccoons, fox, coyote,
opossums, and the occasional neighbor's odd-looking dog that the model misses.
We want two complementary capabilities:

1. **A motion-only detector** that runs alongside YOLO and surfaces "something
   moved here" boxes in the live overlay and timeline pips, *without* firing on
   leaves blowing or shadow drift. This catches the wildlife that YOLO doesn't
   know about, today.
2. **A snapshot capture path** that builds a labeled-but-uncurated dataset of
   real Belfry footage — both motion-only events (unknown classes, ready for
   hand labeling) and YOLO events (already labeled, used to keep existing
   classes from drifting during fine-tune). End goal: enough data to fine-tune
   YOLO11l with the `animal/deer/raccoon/...` classes our cameras actually see,
   tracked in the existing `inference/` follow-ups.

Drives toward the existing CLAUDE.md "Wildlife fine-tune (Phase B)" goal but
adds the missing piece: a way to *gather* labeled data from production traffic
instead of pulling from LILA BC alone.

## Approach summary

- Add `inference/motion.py` — a per-camera `MotionDetector` wrapping OpenCV
  MOG2 background subtraction + morphological denoise + contour-to-bbox.
  Tuned conservatively (large min-blob, multi-frame persistence) so leaves and
  shadow flicker don't fire it.
- Wire it into `EventRecorder._loop` after YOLO, with IoU-suppression so a
  motion box that overlaps a YOLO detection is dropped (no double-counting a
  walking person as both `person` and `motion`).
- Reuse the existing event-coalescing state machine: motion events flow
  through `_update_runs` with `class="motion"`, get one row in `events.db`,
  one peak thumbnail JPEG, and live-broadcast like any other class.
- Add `recordings/snapshots/` — a separate dataset-capture path that writes
  *multiple* full frames per active event run plus YOLO-format label sidecars,
  for both motion and YOLO events. Default off per camera; user enables when
  collecting fine-tune data, disables again afterwards.
- Add `inference/export_dataset.py` CLI that turns the snapshots directory
  into a YOLO dataset folder (`images/`, `labels/`, `data.yaml`).
- Frontend: drop a `motion` color into the existing `CLASS_COLOR` and
  `PIP_COLOR` dicts and into the `events.html` filter chips. Everything else
  is class-agnostic and renders for free.
- Recommended labeling tool: **Label Studio**, self-hosted on EC2, with the
  exported YOLO dataset as input.
- Fine-tune on AWS (g5.xlarge, A10G) — ~5–10× faster than the Orin and the
  job is one-off enough that the cost is trivial. Export the resulting `.pt`
  back to TensorRT FP16 on the Orin (engines are device-specific).

---

## 1. Motion detection algorithm

**Choice: `cv2.createBackgroundSubtractorMOG2`** — a Gaussian-mixture model
per pixel, the standard in surveillance. Adapts slowly to lighting changes,
ignores periodic motion (slowly absorbed into background), and is already
available in the installed OpenCV 4.13.

**Tuning to reject leaves blowing / shadow drift:**

| Knob | Value | Why |
|---|---|---|
| `proc_size` | 640×360 | Downsample input frames before MOG2. ~10× speedup vs full 1080p (a few ms/frame instead of 20–40 ms) and removes any CPU-headroom doubt under contention (e.g. multiple past-mode windows running). Doesn't hurt blob detection: at 640×360 a squirrel is still ~6×6 px and the 0.5%-area threshold maps to the same physical objects. Output bboxes are normalized 0..1 so the resize is invisible to downstream code. |
| `history` | 500 | At 1 fps that's ~8 min of background model — long enough that a leaf gust gets averaged in, short enough that a real intruder still pops out. |
| `varThreshold` | 25 | Default 16 is too sensitive — fires on JPEG noise. 25 cleanly rejects mild compression artifacts while still catching low-contrast wildlife. |
| `detectShadows` | True | MOG2 marks shadow pixels with value 127 (foreground = 255). We threshold them out, so a long late-afternoon shadow doesn't get reported as motion. |
| `min_blob_pct` | 0.5% of frame | A single leaf is much smaller than 0.5%. A squirrel is ~0.5–1%. A deer is several percent. |
| `min_persistence_frames` | 2 | The blob must be present in 2 consecutive 1-fps samples (within IoU > 0.3 of the prior box) before opening an event run. Single-frame blips (sensor noise, momentary glare) get dropped. |
| `morph_kernel` | 5×5 ellipse, MORPH_OPEN then MORPH_CLOSE | Standard cleanup — open kills isolated pixel noise, close fills small holes inside a real blob. Kernel size is for the downsampled 640×360 frame. |

**Per-frame algorithm** (called once per inference tick from `_loop`, after YOLO):

```python
small = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA)
mask = mog2.apply(small)
mask = (mask == 255).astype("uint8") * 255          # drop shadows (=127)
mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
boxes = [cv2.boundingRect(c) for c in contours]
boxes = [b for b in boxes if (b.w * b.h) / (640 * 360) >= 0.005]
boxes = merge_close_boxes(boxes)                     # join boxes that overlap
boxes = persistence_filter(boxes, prev_boxes)        # require 2-frame match
boxes = [normalize(b, 640, 360) for b in boxes]      # 0..1 coords for overlay
```

**YOLO IoU suppression** — for each surviving motion box, if `max IoU` against
any YOLO detection's bbox in the same frame is > 0.5, drop the motion box.
Walking person → `person` event only, not `person` + `motion`.

**Per-camera state** — each `MotionDetector` instance owns its own MOG2
subtractor (background model is per-scene), its own previous-frame box list
(persistence filter), and is *not* thread-safe (only called from the
EventRecorder's own inference thread, no sharing).

**Frame-size note** — MOG2 is the dominant cost. On 1080p frames it's ~10
ms/frame on the Orin's CPU. At 8 cams × 1 fps that's well under one core. No
GPU needed for motion (and reusing the YOLO Detector lock for it would
serialize a CPU-bound op behind GPU work).

---

## 2. Recorder integration

**New file: `inference/motion.py`**

```python
class MotionDetection:
    bbox: tuple[float, float, float, float]   # normalized 0..1
    area_pct: float                           # fractional area, for logging

class MotionDetector:
    def __init__(self, history=500, var_threshold=25,
                 min_blob_pct=0.005, min_persistence_frames=2): ...
    def detect(self, frame, yolo_dets: list[Detection]) -> list[MotionDetection]:
        # Apply MOG2, denoise, contour, area-filter, persistence-filter,
        # IoU-suppress against yolo_dets, return surviving normalized boxes.
```

**Edits to `inference/recorder.py`:**

- Constructor takes a new `motion_detector: MotionDetector | None` (None
  disables motion). Line ~92.
- In `_loop` between lines 207 (YOLO predict) and 218 (`_update_runs`):
  ```python
  motion_dets = []
  if self.motion_detector is not None:
      motion_dets = self.motion_detector.detect(frame, dets)
  ```
- Convert `motion_dets` to `Detection`-shaped objects with `cls="motion"` and
  `conf=1.0` (motion has no confidence — area pct could fill the slot if we
  want a sensible value, but `1.0` keeps the existing `_update_runs` logic
  trivially correct). Append to `dets` before line 216
  (`broadcaster.publish_threadsafe`) so the live overlay sees motion boxes
  alongside YOLO boxes through the existing fan-out.
- The existing `_update_runs` already keys by class string and the existing
  `cooldown_s` handles run extension. No state-machine changes needed: motion
  becomes another class.

**Edits to `inference/runner.py`:**

- After the shared `Detector` is built (line 99–105), build one
  `MotionDetector()` per camera (each needs its own MOG2 background). Pass it
  into `EventRecorder.__init__`. Read motion knobs from config (see below).

**Edits to `dvr/config.py`:**

- Add `motion_enabled: bool` (default `False`) and a small `MotionConfig`
  block to `Inference`:
  ```python
  motion_enabled: bool
  motion_history: int
  motion_var_threshold: int
  motion_min_blob_pct: float
  motion_min_persistence_frames: int
  ```
- Loaded from `cameras.yaml` `inference.motion:` block.
- Per-camera override: add an optional `motion: bool | None` field to the
  `Camera` dataclass so individual cameras can opt out (e.g. a windy outdoor
  cam where leaves are unavoidable). Falls back to global default.

**Edits to `inference/schema.sql`:**

- No changes. Reusing the existing `events` table with `class='motion'` keeps
  retention sweeps and `/api/events*` working unchanged.

---

## 3. UI changes

**`dvr/static/overlay.js` line 15-24** — extend `CLASS_COLOR`:

```js
const CLASS_COLOR = {
  person: "#4ea1ff",
  animal: "#5ad17c", dog: "#5ad17c", cat: "#5ad17c", bird: "#5ad17c",
  vehicle: "#ff9b3f", car: "#ff9b3f", truck: "#ff9b3f",
  motion: "#e879f9",   // magenta — visually distinct from all class colors
};
```

**`overlay.js` `_draw()` (line 267)** — small branch for motion: dashed
stroke (`ctx.setLineDash([6, 4])` for motion, solid otherwise) and skip the
class-name + confidence label since motion has neither. Visual signal:
"something here, no class assigned."

**`dvr/static/playback.js` line 79-88** — add `motion: "#e879f9"` to
`PIP_COLOR`. **`renderEventLegend` line 238** — add a `motion` bucket to the
ordered legend list.

**`dvr/static/events.js` line 5** — add `"motion"` to `CLASS_CHIPS`. The
existing `makeChips()` auto-generates the button.

---

## 4. Snapshot capture for fine-tuning

The existing thumbnail captures one peak frame per event run. For training
diversity we want several frames spread across each run, and we want them
*labeled in YOLO format* so the export step is trivial.

**New layout:**

```
recordings/snapshots/
  <camera>/
    <YYYY-MM-DD>/
      <ts_start>_<class>_<n>.jpg     # full frame, no resize, JPEG q90
      <ts_start>_<class>_<n>.txt     # YOLO labels: <cls_id> <cx> <cy> <w> <h>
```

The `.txt` sidecar is the standard YOLO format: one line per box, normalized.

**Mechanic** — a new `SnapshotWriter` invoked from `EventRecorder._loop`:

- Toggle in config: top-level `inference.snapshots.enabled: bool` plus
  per-camera `snapshots: bool | None` override (same pattern as motion).
- Triggered only while at least one event run is active for the camera.
- Rate-limited to one snapshot every `snapshot_interval_s` (default 3) per
  active run, capped at `max_snapshots_per_run` (default 10) to bound storage.
- Writes the full frame plus a `.txt` sidecar containing every YOLO box for
  classes in `event_classes` *and* every motion box. Motion boxes get a
  placeholder class id (e.g. `99` mapped to `"unlabeled-motion"`); the
  labeling step is where these get renamed/relabeled by hand.
- Independent of the thumbnail flow — thumbnails are for the events browse UI
  and stay as-is.

**Storage budget** — at 8 cams × ~50 events/day × 5 snapshots × 250 KB ≈
500 MB/day = 180 GB/year worst case. Bounded by:

1. Per-camera enable: only turn on a few cams at a time.
2. New snapshots-specific retention: cap `recordings/snapshots/` at
   `snapshots_max_gb` (default 30 GB). Add a fourth pass to `RetentionLoop`
   that deletes oldest `snapshots/` files when the cap is hit. Independent of
   the mp4 watermark sweep.

Realistic plan: enable on 2–3 cameras for 2 weeks, gather ~20–40 GB of
snapshots, disable, label, fine-tune, deploy. Repeat in a couple of months
for any classes still showing weak performance.

---

## 5. Dataset export CLI

**New file: `inference/export_dataset.py`**

```bash
python -m inference.export_dataset \
  --since 2026-04-20 --until 2026-05-04 \
  --cameras cam5,cam6 \
  --include-motion --include-yolo \
  --out datasets/belfry-2026-05/
```

Output layout (Ultralytics standard):

```
datasets/belfry-2026-05/
  data.yaml                  # paths, class list, train/val splits
  images/
    train/<cam>_<ts>_<n>.jpg
    val/<cam>_<ts>_<n>.jpg
  labels/
    train/<cam>_<ts>_<n>.txt
    val/<cam>_<ts>_<n>.txt
```

- 80/20 split is **by event**, not by frame. Frames from the same event run
  go into the same split — otherwise temporally adjacent frames would leak
  between train and val and inflate val accuracy.
- `data.yaml` initial `names:` is the existing 6 YOLO classes plus a
  placeholder `unlabeled-motion`. After the labeling pass updates the `.txt`
  sidecars in place, re-run export (or just re-edit `data.yaml`) to add the
  new classes (`deer`, `raccoon`, `coyote`, `fox`, `opossum`, ...).
- Reads from `recordings/snapshots/`, copies (not moves) the JPEGs into the
  dataset dir so the snapshots store stays intact and the export is
  re-runnable with different filters.

---

## 6. Recommended labeling tool

**Primary: Label Studio**, self-hosted on EC2.

- Open source, single-binary install via `pip install label-studio`.
- Web UI, multi-user (just you, but accessible from laptop and iPad).
- Imports YOLO format directly, exports YOLO format directly. Reads the
  `.txt` sidecars our exporter writes; writes them back with the new class
  labels.
- Can run on the existing EC2 frontdoor box behind the same Caddy +
  oauth2-proxy gate, so the UI is at e.g. `label.yellowchicken.io` with the
  same Google login. Footage stays accessible to you, nobody else.
- Storage backend: local filesystem. Point Label Studio at a directory synced
  from the Orin via `rsync` (one-off, not a live mount). Manageable at 30 GB.

**Alternative: Roboflow** — best UX, fastest path to results. Free tier
covers small projects (~10k images). Tradeoff: footage uploaded to
roboflow.com (private workspace). Reasonable since the same footage already
flows through EC2 behind OAuth, but slightly less on-prem.

**Avoid: labelImg** — old, single-user, no SAM-assist, no review workflow.
Fine for 50 images, painful for 1000.

**Concrete labeling workflow:**

1. Run exporter, get `datasets/belfry-2026-05/` with images + (machine-
   generated) labels for known YOLO classes plus `unlabeled-motion` boxes.
2. Set up Label Studio project, import the dataset.
3. Hand-pass: for each `unlabeled-motion` box, rename to the right class
   (`deer`, `raccoon`, ...) or delete (false positive). For YOLO-detected
   boxes, spot-check accuracy and fix the wrong ones.
4. Export back to YOLO format. Rsync the updated `.txt` files back into the
   dataset directory.

Budget: ~5–10 seconds per image at a steady clip, so 1000 images = 2–3 hours.

---

## 7. Fine-tuning workflow

**Hardware: AWS, not Orin.**

Why: YOLO11l fine-tune at 640 imgsz, 50 epochs, ~2000 images:

- **Orin (Ampere, 8 SMs)**: ~30 min/epoch → 24+ hours total. Also pulls the
  GPU away from production inference for the duration; would have to disable
  `belfry-inference` while training.
- **AWS g5.xlarge (A10G, 80 SMs)**: ~3-5 min/epoch → 2-4 hours total. ~$1.00/hr
  on-demand in us-west-2 (matches the EC2 region). One run = ~$5.

A one-off ~$5 cloud run vs. taking the Orin offline for a day is an obvious
choice. Bigger models or larger datasets make AWS even more lopsided.

**Steps:**

1. Spin up `g5.xlarge` (or `g4dn.xlarge` if you want to spend less). Deep
   Learning AMI has CUDA + PyTorch pre-installed.
2. `pip install ultralytics`. Copy `yolo11l.pt` from the Orin (the base
   weights, not the .engine — engines are Orin-specific).
3. Rsync the labeled dataset directory.
4. Train:
   ```bash
   yolo detect train \
     data=datasets/belfry-2026-05/data.yaml \
     model=yolo11l.pt \
     epochs=50 imgsz=640 batch=16 device=0 \
     project=runs name=belfry-finetune
   ```
5. Inspect `runs/belfry-finetune/results.png` and `confusion_matrix.png`.
   Per-class AP should be >0.7 for the new classes if 150+ images each were
   labeled. Below that, more data needed.
6. Copy `runs/belfry-finetune/weights/best.pt` back to the Orin
   (`inference/yolo11l-finetune.pt`).
7. **On the Orin** (engines are device-specific), export to TensorRT FP16:
   ```bash
   .venv-inference/bin/yolo export model=inference/yolo11l-finetune.pt \
     format=engine half=True imgsz=640
   ```
   This produces `inference/yolo11l-finetune.engine`.
8. Update `cameras.yaml` `inference.yolo_pt` and `yolo_engine` paths to point
   at the new files. Update `event_classes` to include the new wildlife
   classes (`deer`, `raccoon`, etc.). Update `inference/model.py` `_YOLO_KEEP`
   to extend the allow-list with the new classes.
9. `sudo systemctl restart belfry-inference` and tail
   `journalctl -u belfry-inference -f` for the engine-load message.
10. Sanity-check: open `/sets/<set>` with labels on, walk past a camera, see
    `person` boxes. Then wait for a deer to wander through and confirm the
    `deer` box fires. Use the past-mode overlay on a known-deer mp4 to
    validate without waiting.

**Re-running** is cheap: kick off another AWS box, pull in updated labels,
retrain. The model files are versioned by filename
(`yolo11l-finetune-v2.pt`) so rollback is just a config-file change.

---

## Critical files

- **New**: `inference/motion.py`, `inference/snapshots.py`,
  `inference/export_dataset.py`
- **Edit**: `inference/recorder.py` (`__init__`, `_loop`)
- **Edit**: `inference/runner.py` (instantiate per-camera `MotionDetector` +
  `SnapshotWriter`, pass to recorders)
- **Edit**: `inference/model.py` (no changes for v1; later add fine-tune
  classes to `_YOLO_KEEP`)
- **Edit**: `dvr/config.py` (`Inference` dataclass: motion + snapshots
  blocks; optional per-`Camera` overrides)
- **Edit**: `dvr/retention.py` (`_tick` adds a `_sweep_snapshots` pass with
  its own GB cap)
- **Edit**: `dvr/static/overlay.js` (CLASS_COLOR + dashed motion stroke in
  `_draw`)
- **Edit**: `dvr/static/playback.js` (PIP_COLOR + legend bucket)
- **Edit**: `dvr/static/events.js` (CLASS_CHIPS adds "motion")
- **Edit**: `cameras.example.yaml` (document the new `motion:` and
  `snapshots:` config blocks)

## Verification

**Unit-ish**:

- `python -m inference.motion --cam cam5 --duration 60` — a one-shot CLI that
  runs the motion detector against the loopback RTSP for 60 seconds and prints
  every detection. Validates the algorithm in isolation.
- `python -m inference.export_dataset --dry-run` — counts what would be
  exported without writing files, prints class histogram.

**End-to-end** (run from the Orin):

1. Edit `cameras.yaml`: enable motion + snapshots on cam5 only. Restart
   `belfry-inference`. Tail `journalctl -u belfry-inference -f` for
   `motion open`/`motion close` log lines.
2. Walk past cam5 — expect a `person` event (YOLO) and **no** `motion` event
   (IoU suppression).
3. Throw a small object across cam5's view — expect a `motion` event
   (assuming it's not a COCO class).
4. Open `http://127.0.0.1/sets/set1`, toggle "Show labels". Repeat (3),
   confirm magenta dashed box renders live.
5. Open the playback page for cam5. Confirm magenta pips appear on the
   timeline at the motion events. Click one, confirm it deep-links and the
   past overlay re-renders the motion box.
6. Open `/events`, click the `motion` filter chip, confirm only motion
   events are shown.
7. After ~24h, run `du -sh recordings/snapshots/cam5/` — expect a few hundred
   MB. Check that `recordings/snapshots/cam5/<today>/*.txt` files have valid
   YOLO-format labels (`<cls_id> <cx> <cy> <w> <h>` with all values 0..1).
8. `python -m inference.export_dataset --since 1d --cameras cam5 --out /tmp/ds`
   — verify the dataset folder structure, then `head /tmp/ds/data.yaml`.

**Browser self-test** (Phase 1+ frontend changes):

```python
# adapted from CLAUDE.md "Browser self-test" section
page.goto("http://127.0.0.1/sets/set1/cam5/playback")
page.wait_for_selector("#event-pips")
pips = page.evaluate("[...document.querySelectorAll('.event-pip')]"
                     ".map(p => getComputedStyle(p).backgroundColor)")
assert "rgb(232, 121, 249)" in pips  # magenta motion pip rendered
```

## Open questions to revisit after a week of data

- **Motion sensitivity** — `min_blob_pct=0.5%` and `var_threshold=25` are
  educated guesses. Once a week of motion events has accumulated, sample 30
  random events from `/events?class=motion`, classify by hand as
  true/false-positive, then tune. Same `class_thresholds` pattern as YOLO
  — overrides go in `cameras.yaml`.
- **Per-camera tuning** — cam6 (yard, swaying trees) will likely need a
  higher threshold than cam5 (driveway, mostly static).
- **Snapshot cadence** — 1 every 3 seconds may be too dense for slow events
  (deer grazing for 5 minutes = 100 snapshots, mostly identical). Possibly
  add a "skip if frame-diff vs previous snapshot is small" heuristic.
- **Phase B model size** — Stay on `yolo11l` for the first fine-tune; switch
  to `yolo11x` later only if `l` plateaus on the new wildlife classes.
