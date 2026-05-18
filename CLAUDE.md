# belfry

Self-hosted DVR for Interlogix TVB-5301 cameras (OEM Hikvision) on a dedicated, isolated camera subnet. The on-prem Jetson Orin pulls RTSP from each camera, MediaMTX repackages to HLS for live viewing and writes 1-hour fMP4 segments to disk for recording, and a small FastAPI service serves a per-set browser viewer plus a per-camera scrubback page. nginx fronts everything on port 80 for LAN access. A small EC2 frontdoor (Caddy + oauth2-proxy) terminates HTTPS for `yellowchicken.io`, gates every request behind Google OAuth, and reverse-proxies through a persistent SSH tunnel back to the Orin — so all video and recordings stay on the Orin while the public side enforces auth.

## Layout

- `dvr/` — FastAPI app:
  - `server.py` — routes (live, playback, retention status, set/camera APIs, `/api/events*`, `/api/inference/live` SSE proxy, `/api/training/*` (staging CRUD, label CRUD, promote, stage-event, capture), `/events`, `/training` pages)
  - `config.py` — `cameras.yaml` loader; `Camera`, `CameraSet`, `Recording`, `Retention`, `Inference`, `Config` dataclasses
  - `health.py` — `ffprobe`-based per-camera reachability checks
  - `retention.py` — async lifespan task that evicts oldest mp4 segments when the recordings volume crosses watermarks; also sweeps events DB rows + thumbnail JPEGs whose underlying footage has been evicted
  - `training.py` — paths + helpers for the training-data tree (`STAGING_DIR`, `IMAGES_DIR`, `LABELS_DIR`), dataset.yaml read/write, `bbox_to_yolo_line`, `seed_labels_for_capture` (events.db → YOLO lines at a timestamp). Used by both `/api/training/capture` and the labeler endpoints.
  - `static/` — vanilla-JS viewer: `index.html` (live grid), `playback.html` (scrubback + timeline pips + prev/next event nav), `events.html` (cross-camera event browse), `training.html`+`training.js` (in-browser bbox labeler), shared `viewer.css`, `overlay.js` (live bounding-box canvas — registered as `window.BoxOverlay`, lazy SSE)
- `inference/` — object detection pipeline (Phase 4):
  - `model.py` — `Detector` wrapping a single YOLO11l (COCO) and filtering to the `person/dog/cat/bird/car/truck` subset; thread-safe so one instance is shared across all camera threads. Replaced an earlier MegaDetector-larch + YOLO11n ensemble whose IoU-merge produced too many low-confidence person false-positives on noisy backdrops; running one model end-to-end means the live recorder and past-mode re-inference can't disagree with each other.
  - `recorder.py` — `EventRecorder` per camera, two threads. A **drain thread** does a tight `cv2.VideoCapture.read()` loop on the loopback RTSP at native ~30 fps and stores only the latest frame in `self._latest`. A separate **inference loop** wakes at `record_fps` (default 1 fps), grabs `self._latest`, runs the detector, coalesces detections into event runs (open / extend / close on cooldown), writes one row per closed run to SQLite + a peak-conf JPEG thumbnail, and publishes every detection (incl. empty batches) to `live.broadcaster`. The drain/sample split exists because a single-thread version that slept ~1 s between reads let OS-level TCP buffers fill, MediaMTX's RTSP packets stack up, and the H.264 stream eventually corrupt — `read()` would return `False`, the recorder reopened, and a few seconds of frames were lost. Past-mode re-inference on the recorded mp4 found events the live path missed for exactly that reason. Drain thread keeps the socket continuously consumed; inference cadence stays decoupled.
  - `live.py` + `server.py` — `LiveBroadcaster` (per-camera fan-out from sync recorder threads to async SSE subscribers) and a tiny FastAPI on `127.0.0.1:9091` (`/live?cam=X` SSE for the live feed, `/playback?cam=&start=&duration=` SSE for on-demand box detection over a past mp4 window, `/health`)
  - `playback.py` — async generator that pulls a playback mp4 from MediaMTX's loopback `/get`, decodes at 1 fps in a worker thread, runs the same Detector, streams `{ts, boxes}` SSE messages. One-shot per request; tmp file cleaned up afterwards
  - `runner.py` — production multi-camera coordinator; spawns one EventRecorder thread per camera with `inference: true`, plus a uvicorn thread for the SSE server, all sharing one Detector
  - `cli.py` — single-camera smoke-test entry (`python -m inference.cli --cam cam6`); not used in production
  - `schema.sql` — events table DDL, `CREATE … IF NOT EXISTS` so safe to re-run
- `mediamtx/` — MediaMTX binary + `mediamtx.yml`. Real config is gitignored; `mediamtx.example.yml` is the template.
- `cameras.yaml` — source of truth for camera grouping (sets), credentials, recording path, retention watermarks, and inference defaults / per-camera enable. Gitignored, mode 0600. Template: `cameras.example.yaml`.
- `recordings/` — gitignored, on the 3.6 TB NVMe (`/dev/nvme0n1p1`). One subdir per camera (`recordings/cam5/...`). MediaMTX writes 1-hour fMP4 segments here. The inference pipeline writes `recordings/events.db` (SQLite WAL) and `recordings/thumbs/<cam>/<YYYY-MM-DD>/<ts>_<class>.jpg` here too.
- `playback_cache/` — gitignored sibling of `recordings/`. FastAPI buffers each MediaMTX `/get` response here so byte-range requests work (iOS Safari requires it for `<video>`); LRU-evicted at a 2 GiB cap.
- `scripts/run.sh` — starts MediaMTX + uvicorn together; cleans up on Ctrl-C. Invoked by the `belfry` systemd unit at `/etc/systemd/system/belfry.service`.
- `scripts/run-inference.sh` — invokes `.venv-inference/bin/python -m inference.runner`. Invoked by `belfry-inference.service`.
- `scripts/install-mediamtx.sh` — fetches the MediaMTX binary for the host arch.
- `scripts/install-inference.sh` — provisions `.venv-inference/` (Python 3.10), installs Jetson torch + ultralytics + onnx deps, downloads MegaDetector + YOLO11n weights, builds device-specific TensorRT FP16 engines.
- `scripts/promote-labeled.py` — batch alternative to the labeler's per-image Promote button; walks `belfry-training/staging/`, moves any image+`.txt` pair into flat `images/`+`labels/`. `--auto-empty-negatives` writes empty `.txt`s for `negative_*/` hard negatives.
- `scripts/remap-class-ids.py` — one-time migration that walked existing label `.txt`s to remap from the original dense 0..6 ids to the current COCO-aligned sparse scheme (0/2/7/14/15/16/80). Kept around so the *why* of the id layout stays visible; safe to delete once the original code reviewers are no longer surprised by sparse ids.
- `scripts/nginx-belfry.conf` — tracked copy of the Orin's nginx site (`/etc/nginx/sites-available/belfry`).
- `scripts/belfry-tunnel.service` — autossh systemd unit; opens the reverse SSH tunnel from Orin to EC2 (Caddy upstream + admin SSH back-in).
- `scripts/belfry-inference.service` — systemd unit for the multi-camera inference runner.
- `.venv-inference/` — separate Python 3.10 venv for the inference pipeline; the main DVR runs on uv-managed Python 3.14, but Jetson PyTorch wheels are cp310-only so inference needs its own interpreter. Gitignored.
- `.venv-playwright/` — separate Python 3.10 venv with `playwright` + a Chromium-headless build, used for browser self-tests. Gitignored. See "Browser self-test" below.
- `cloud/` — EC2 frontdoor: `Caddyfile`, `oauth2-proxy.cfg`, systemd units, and `install-ec2.sh` bootstrap script. Run on EC2 only.
- `runme.sh` — gitignored throwaway shell script. Convention: when something needs sudo, write the steps here for the user to run from their own terminal.

## Path topology in MediaMTX

Each camera is a single always-on MediaMTX path (`sourceOnDemand: no`). The path holds exactly one upstream RTSP session to the camera and feeds multiple consumers off the loopback RTSP server (`rtsp://127.0.0.1:8554/<cam>`):

- HLS muxer (live tiles) — `hlsAlwaysRemux: yes` keeps a muxer warm per path so set-switching is instant.
- Recorder — writes fMP4 segments under `recordings/<cam>/`.
- Future inference and SRT-forward consumers tap the same loopback path, no extra camera connection.

The MediaMTX `playback` (`:9996`) and `api` (`:9997`) endpoints are loopback-only; nginx fronts `playback` at `/playback/` for the scrubback UI.

## nginx

`/etc/nginx/sites-available/belfry` listens on `:80` and proxies:

- `/hls/` → `127.0.0.1:8888` (MediaMTX HLS), with `proxy_redirect ~^/(.*)$ /hls/$1` to rewrite MediaMTX's bare-path 302 Locations so the cookie-check redirect stays inside `/hls/`.
- `/playback/` → `127.0.0.1:9996` (MediaMTX playback API), same redirect-rewrite trick.
- everything else → `127.0.0.1:9090` (FastAPI).

`hls_base: /hls` in `cameras.yaml` makes camera HLS URLs same-origin so the browser stays under one host on port 80.

## Cloud frontdoor

`yellowchicken.io` resolves to a small EC2 instance (Amazon Linux 2023 x86_64, us-west-2). It runs two services:

- **Caddy** (`/etc/caddy/Caddyfile`, systemd unit `caddy.service`) — listens on `:443`, auto-issues Let's Encrypt certs. `forward_auth 127.0.0.1:4180` (oauth2-proxy's `/oauth2/auth`) lives inside the catch-all `handle` block (not at the site-block top level) because Caddy's default directive order runs `handle` before `forward_auth`; at the top level the catch-all handle would terminate the request before the gate ever ran. Inside `forward_auth`, a `handle_response @bad-status` block intercepts oauth2-proxy's 401 and `redir * /oauth2/start?rd={uri} 302` redirects the browser into the Google login flow — note the explicit `*` matcher: `redir [<matcher>] <to> [<code>]` treats a leading-`/` first arg as an implicit path matcher, so without `*` the URL gets parsed as a never-matching matcher and the gate silently fails open. Authenticated requests reverse-proxy to `127.0.0.1:8080` (the tunnel).
- **oauth2-proxy** (`/etc/oauth2-proxy/oauth2-proxy.cfg`, systemd unit `oauth2-proxy.service`) — bound `127.0.0.1:4180`, Google provider. Allow-listed emails live in `/etc/oauth2-proxy/emails`. Secrets (client_id/secret, cookie_secret) in `/etc/oauth2-proxy/oauth2-proxy.env`, read by the systemd unit's `EnvironmentFile=`.

The reverse SSH tunnel runs on the **Orin**, outbound to EC2 (autossh, `belfry-tunnel.service`). Two `-R` forwards land on EC2's loopback (`GatewayPorts no` keeps them off the public interface):

- `127.0.0.1:8080` → Orin `:80` — the upstream Caddy reverse-proxies into.
- `127.0.0.1:2222` → Orin `:22` — `ssh -J paulm@yellowchicken.io paulm@127.0.0.1 -p 2222` from a laptop while travelling.

Auth model: there is no FastAPI-level auth. Caddy + oauth2-proxy gates the public path; LAN is trusted by network position. Removing the previous HTTP Basic layer eliminated the duplicate password popup once OAuth landed.

## Adding a user

A user's Google email has to be allow-listed in three places — there's no central source, so all three are kept in sync by hand:

1. **EC2** — `/etc/oauth2-proxy/emails`, one email per line. Gates the web frontdoor; oauth2-proxy reads it live, no restart needed.
2. **Orin** — `/etc/belfry/allowed-emails`, one email per line. Gates the mobile app's `/auth/exchange` JWT-mint endpoint. Read once at FastAPI startup, so `sudo systemctl restart belfry` after editing.
3. **Google Cloud Console** — the OAuth consent screen's "Test users" list (project name `yellowchicken.io`, since the app is in Testing mode and not verified). Without this Google blocks the sign-in flow itself, before the email ever reaches our allow-lists.

## Cameras and sets

8 cameras total. 7 live on the isolated camera subnet `192.168.254.0/24`; cam12 lives at `192.168.1.70` on the LAN subnet (a re-IP via the camera UI didn't take). To reach cam12 from the box, `192.168.1.50/24` is a persistent secondary address on `eno1` via NetworkManager (`nmcli connection modify "Wired connection 1" +ipv4.addresses 192.168.1.50/24`).

cameras.yaml groups cameras into named **sets** (e.g. `set1`, `set2`). The DVR has per-set viewer pages at `/sets/<id>` with cross-links in the header. Camera `name` must be unique across all sets — it's the global MediaMTX path key.

## Recording and retention

- 24/7 recording. Each camera writes 1-hour fMP4 segments (`recordSegmentDuration: 1h`, `recordPartDuration: 1s`).
- Disk-aware retention runs as a FastAPI lifespan task (`dvr/retention.py`). Every `scan_interval_s` (default 60s) it `shutil.disk_usage`s the recordings volume; if usage > `evict_high_pct` (default 85), it deletes oldest mp4 files globally until usage < `evict_low_pct` (default 80). Files modified within the last 70 minutes are protected so MediaMTX never has its in-progress segment yanked. Per-camera dirs share the disk fairly under "oldest-globally" because all 8 cams record at similar bitrate.
- Each tick also sweeps the events DB: any row whose `ts_end` is older than the oldest surviving mp4 segment for the same camera gets deleted along with its thumbnail JPEG (events outliving their footage are dead links). Runs every tick, not only above the high watermark.
- Status visible at `GET /api/retention/status`. Eviction events log to journald under `belfry`.

## Inference

Runs in `belfry-inference.service` (separate from the DVR so a torch/TRT crash doesn't take down recording or the viewer; separate venv because Jetson torch wheels are cp310, the DVR is on Python 3.14).

- **Model**: YOLO11l on COCO, exported to TensorRT FP16 once per Orin (engines are device-specific) and held resident on the GPU. Active classes: `person, dog, cat, bird, car, truck` — the rest of COCO (toaster, frisbee, etc.) is dropped at the `_YOLO_KEEP` filter in `inference/model.py`. Older `animal`/`vehicle` rows from the MegaDetector era still display correctly via the existing pip color map; new rows just won't have those classes.
- **Per-camera fan-out** (`inference/runner.py`): one thread per camera with `inference: true`, all sharing one Detector. Detector's `predict()` is internally locked because Ultralytics' YOLO mutates state on the model object and CUDA is single-stream by default anyway. Each thread owns its own SQLite connection (sqlite3 connections aren't safe to share across threads).
- **Event coalescing**: detections are sampled at `record_fps` (default 1 fps). A detection above the per-class threshold opens a "run" for that (camera, class). Subsequent detections within `cooldown_s` (default 10s) extend the run. After `cooldown_s` of silence the run closes — one row written, peak-conf frame saved as a JPEG. A person walking past for 12 s is one row, not 12. The peak `bbox` is drawn in-place onto the frame (`draw_thumb_bbox`) before JPEG-encoding so the `/events` grid thumbnails and the timeline-pip hover preview show exactly what was detected; the BGR colours mirror `overlay.js`'s `CLASS_COLOR` so the baked rectangles match the live overlay / pip palette.
- **Storage**: SQLite at `recordings/events.db` (WAL), thumbnails at `recordings/thumbs/<cam>/<YYYY-MM-DD>/<ts_start>_<class>.jpg`. Realistic volume: a few hundred events/day across all cameras, ~30 KB thumbnail each, ~10 GB/year — trivial.
- **Tuning**: `cameras.yaml` has a top-level `inference:` block with the defaults; `class_thresholds` overrides the global `conf_threshold` per class for noisy classes (e.g. raise `person` to suppress wall/edge false positives, lower `bird` to catch partial-frame detections).
- **GPU budget**: TRT FP16 YOLO11l ≈ 30–50 ms/frame on the Orin. At 1 fps × 8 cams that's 240–400 ms/sec sequential, well under one stream-second; verified via `tegrastats --interval 100` (`nvidia-smi` is useless on Jetson, the iGPU doesn't expose NVML).
- **Loopback RTSP + drain-thread reader**: the recorder reads `rtsp://127.0.0.1:8554/<cam>` (MediaMTX's loopback), so each camera only serves the one upstream consumer that MediaMTX itself maintains — HLS, recording, and the inference recorder all multiplex off that one session. Inside the recorder a dedicated drain thread (see `recorder.py` above) keeps the socket continuously consumed at ~30 fps so the H.264 stream stays clean even when inference takes a moment. Together these two changes eliminated the per-cam "read failed; reopening" warnings that used to fire every 1–2 min and the resulting holes in events.db where the live recorder missed events that past-mode re-inference could later see in the recorded mp4. Both pieces were needed: loopback alone didn't fix it because the bottleneck was on the consumer side, not the camera side.

### Read-side surface (slice 3)

- `GET /api/events?cam=&class=&since=&until=&before_id=&limit=` — filtered events list, newest first. Cursor-paginate by passing the smallest `id` from the prior page back as `before_id`. Read-only `mode=ro` URI sqlite3 connection per request.
- `GET /api/events/recent?cam=&limit=` — convenience wrapper.
- `GET /api/events/neighbors?cam=&ts=` — `{prev, next}` event ids+timestamps; backs the prev/next-event buttons.
- `GET /api/events/thumb/{id}` — serves the JPEG with an immutable long-cache header. Path-traversal-checked against the thumbs root.
- `GET /events` — cross-camera browse page with filter chips (class / camera / time-window: 24h / today / 7d / all), thumbnail grid, "Load more" cursor pagination. Click a card → `/sets/<set>/<cam>/playback?ts=<ts_start>` deep-link.

### Playback page event nav (slice 4)

- Timeline pip overlay (`#event-pips`) sits exactly over the recording-availability bar. One colored mark per event, width proportional to event duration (clamped to a 0.15% hairline so a single-frame event still shows). Three-color bucketing — blue=person, green=animal/dog/cat/bird, orange=vehicle/car/truck. Click a pip → seek there.
- `◀ event` / `event ▶` buttons next to Go Live, plus `[` / `]` keyboard shortcuts; back-end is `/api/events/neighbors`.
- `#event-legend` shows only the buckets present in the current day's events plus the row count.
- `playback.js` reads `?ts=<unix-epoch>` on init so deep-links from `/events` land on the right moment.

### Live overlay (slice 5)

- `/api/inference/live?cam=X` is an SSE pass-through proxy in the DVR (`dvr/server.py`) onto `127.0.0.1:9091/live?cam=X` inside the inference process. Keeps the OAuth gate in front; the inference port is loopback-only. `X-Accel-Buffering: no` so nginx flushes immediately; long timeouts since the SSE is long-lived (15 s heartbeat from the server keeps intermediaries from closing on idle).
- `LiveBroadcaster` bridges the sync recorder threads to async SSE subscribers via `loop.call_soon_threadsafe(q.put_nowait, msg)`. Per-subscriber `asyncio.Queue` is bounded — slow clients get drop-oldest. `publish_threadsafe` is cheap (~one dict lookup) when no subscribers, so it doesn't tax the recorder loop.
- Browser side: `dvr/static/overlay.js` exposes `BoxOverlay` on `window`. Each instance owns a `<canvas>` layered over its host element (a `.video-wrap` per live tile, or `.playback-video-wrap` in playback live mode). The SSE connection is **lazy** — only opened when the "Show labels" pill in the header is on. Without lazy SSE the four live-tile EventSource connections plus HLS pulls blew through the browser's HTTP/1.1 per-origin cap of 6 and broke video playback. The toggle calls `start()`/`stop()` on every registered overlay; CSS hides/shows the canvas independently so flipping is instant.
- The toggle does **not** auto-restore from localStorage — each session starts labels-off. The persistence path repeatedly raced HLS for connection slots on page load; making the first click each session an explicit user gesture eliminates that.

### Past-playback overlay (slice 4.5)

Same canvas, same `BoxOverlay`, but driven by re-running the detector on the playback mp4 the browser is watching instead of subscribing to the live feed.

- **`/api/inference/playback?cam=&start=&duration=`** is an SSE pass-through to the inference process's `/playback` endpoint, mirroring the live proxy.
- Inference handler (`inference/playback.py`) pulls the same mp4 the DVR proxies for video — direct from MediaMTX `127.0.0.1:9996/get` — to a tmp file, then decodes in a worker thread with `cv2.VideoCapture` at 1 fps target sampling. Boxes from each sampled frame are emitted as `data: {"ts": <unix>, "boxes": [...]}\n\n`. Worker stops promptly when the browser disconnects (a stop-flag the async generator sets in its `finally`).
- Inference goes flat-out — at ~30 ms/frame × 300 frames in a 5-minute window it finishes processing in ~9 s of GPU time, so by the time the user is mid-window every later box is already on the wire. Past-inference shares the same `Detector` lock as the live recorder, so live recording slows briefly while a past window is processing but does not drop frames.
- **Stored-event fallback** (`_load_events_for_window`): re-inference samples at integer-second offsets from the window start, while the live recorder samples at arbitrary RTSP-arrival moments. A brief visit (a bird flap of ~1 s) the live recorder caught can fall entirely between past-inference's sample points and never re-detect — same problem `motion` always had, since YOLO doesn't know about motion. The worker now loads every event overlapping the window from `events.db` and, for each YOLO-sampled frame whose timestamp falls inside an event's `[ts_start, ts_end]`, appends the stored `peak_bbox` to that frame's boxes *only* when re-inference at that frame didn't already detect the same class. Motion always injects (no YOLO equivalent to suppress against). Net effect: a long person walk gets the moving re-detected box (no static duplicate), but a brief bird flash that re-detection misses entirely still shows a static box for the duration of the event. Why keep re-inference at all instead of driving the overlay purely from stored events: per-frame re-detection tracks moving subjects (the walking person's box follows them across the frame); a stored event collapses to one peak position. We accept the GPU cost for the moving-box UX and use stored events as the fallback for what re-inference misses.
- Browser side: `BoxOverlay` gets a `subscribePast(url)` mode that subscribes to the playback SSE instead of the live one. Boxes are buffered into a sorted `ts → boxes` array; on the player's `timeupdate` event we look up the closest sample within ±0.5 s of the absolute play time (`window_start_unix + video.currentTime`) and draw it. Switching playback windows (scrubbing into a different 5-min window) tears down the old SSE and opens a new one.
- No persisted per-frame data: re-running inference for a re-watched window is fine at our scale, and an in-memory LRU cache by `(cam, start, duration)` is the obvious next move if rewinding the same window starts to feel slow.

## Training data & labeler

The fine-tune dataset lives outside the repo at `/home/paulm/belfry-training/` (configurable via `dvr/training.py:TRAINING_ROOT`). Two-stage layout:

```
belfry-training/
  dataset.yaml             # Ultralytics class id ↔ name (source of truth)
  staging/                 # workbench — not visible to ultralytics
    <class>/               # class hint from the capture flow
      cam5_…jpg            # frame
      cam5_…txt            # YOLO labels, pre-seeded or human-edited
    negative_<class>/      # hard negatives for <class>
  images/                  # promoted — in the training set
  labels/                  # YOLO `<cls_id> <cx> <cy> <w> <h>` per line
```

Three unambiguous file states drop out of this:

- `staging/<class>/foo.jpg` with no `.txt` → captured, awaiting review.
- `staging/<class>/foo.jpg` + `.txt` → reviewed, ready to promote.
- `images/foo.jpg` + `labels/foo.txt` → in the training set.

An *empty* `.txt` is the YOLO sentinel for "this image has zero objects, on purpose" — a hard negative. A *missing* `.txt` means unlabeled. Don't confuse them.

**Class ids are COCO-aligned and sparse**, not dense 0..N. The current layout:

```yaml
names:
  0:  person      # COCO 0
  2:  car         # COCO 2
  7:  truck       # COCO 7
  14: bird        # COCO 14
  15: cat         # COCO 15
  16: dog         # COCO 16
  80: deer        # new — beyond COCO's 0..79
  81: coyote      # new
  82: raccoon     # new
  83: rabbit      # new
```

Sparse ids are fine for Ultralytics — it skips unused slots during training. We pay this complexity because the fine-tune strategy (see below) extends YOLO11l's pretrained 80-class head by appending new neurons for ids 80, 81, 82, …, and weight-transferring the pretrained channels for ids 0..79 verbatim. That only works if our ids match COCO's. New wildlife classes get the next free id (84, 85, …) to keep the head extension a clean append. The labeler dropdowns show real ids (not array indices); `load_class_map()` returns `(id, name)` pairs.

### How frames get into staging

Three paths, all writing the same `<cam>_<YYYYmmddTHHMMSS>_<uuid>.jpg` filename so events.db round-trips work:

1. **Manual capture** — Capture button on `/sets/<set>/<cam>/playback` (or the 📷 icon on event cards, which deep-links into playback with the modal open). Grabs the current `<video>` frame to a canvas, posts the JPEG to `POST /api/training/capture` along with class + negative flag + ts. The overlay canvas isn't part of the grab so saved frames are box-free.
2. **One-click event staging** — 📥 button on each event card on `/events`. `POST /api/training/stage-event/{id}` looks up the event row, pulls a **clean frame** from MediaMTX `/get` via ffmpeg (one-second window, first frame), writes it to `staging/<class>/`, pre-seeds the `.txt` with the event's `peak_bbox`. Do **not** reuse the events.db thumbnail for this — it has the bbox drawn into the JPEG before encoding (see `inference/recorder.py:_save_thumb`), which would teach the fine-tune to predict neon rectangles. MediaMTX `/get` requires `Z`-suffix UTC timestamps (`%Y-%m-%dT%H:%M:%S.%fZ`) and rejects Python's default `+00:00` offset isoformat with a 400.
3. **Pre-seed on capture** — `POST /api/training/capture` also queries `events.db` for boxes overlapping the capture timestamp and writes a pre-seeded `.txt` next to the image (skipped for negative captures, since the user explicitly flagged them as no-class hits).

`dataset.yaml` is the source of truth for class id ↔ name. `dvr/training.py:load_class_map()` parses it; `bbox_to_yolo_line(bbox, cls_id)` converts `[x1,y1,x2,y2]` (events.db's `peak_bbox` format) to `<cls_id> <cx> <cy> <w> <h>`.

### Labeler — `/training`

In-browser bounding-box labeler at `/training` (linked from the `/events` top-right). Vanilla JS + `<canvas>` + the staging-CRUD endpoints — replaces an earlier plan to use `labelImg`, which we rejected as a dead unmaintained external tool that's essentially just a canvas with a file walker.

- Three-column layout: filter rail / canvas / tools panel.
- Canvas: click-drag empty space to draw a new box (assigned the "new-box class" dropdown), click an existing box to select it, drag corners to resize, drag body to move, Delete/Backspace to remove the selected box.
- Keyboard: `j`/`k`/arrows navigate, `s` save, `p` save+promote, `x` trash, `Esc` deselect.
- **Autosaves silently on navigation** — switching to the next image with `j` flushes pending edits via `PUT /api/training/label`; the explicit Save button is mostly there for "commit and stay here."
- New-box class is pre-filled from the staging folder name for positive classes (so drawing in `staging/person/` defaults new boxes to `person`). Negative folders leave it alone — the user is presumably labeling something other than the negated class.
- Save with zero boxes writes an explicit empty `.txt` — that's the hard-negative sentinel.
- The list includes both staging and promoted images, with a `location` field per item so the JS routes image/label/trash URLs to either `/api/training/{image,label,staging}/<cat>/<name>` (staging) or `/api/training/promoted/{image,label}/<name>` (already in `images/`+`labels/`). Filter chips: Unlabeled / Labeled / Promoted / All. Status dots: `•` unlabeled (muted), `✓` labeled (green), `★` promoted (gold).
- Promoted images can still be edited (Save writes `labels/foo.txt` in place); Promote is disabled with a hint, Trash deletes the image+label pair.
- All staging CRUD endpoints are path-safe via `_resolve_staging` / `_resolve_promoted` — regex-validated category and filename, plus a `.parents` containment check against `STAGING_DIR`/`IMAGES_DIR`/`LABELS_DIR`.

### Promotion

`scripts/promote-labeled.py` is the batch alternative to the labeler's Promote button — useful for working through a backlog. Walks `staging/`, moves any image with a `.txt` sibling into flat `images/`+`labels/`; `--auto-empty-negatives` additionally promotes any `staging/negative_*/foo.jpg` without a `.txt` by writing an empty `labels/foo.txt`.

### Fine-tune plan (deferred until enough data)

Tooling is in place; what's left is data volume + the actual training run. Not blocked on code today.

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

## Scrubback UI

`/sets/<set>/<cam>/playback` serves a per-camera page with:

- Day picker (last 14 local days; days with footage marked `●`, empty days `○`).
- Scale buttons **Day / Hour / 5 min** — reshape the slider's visible range to the chosen window snapped around the current cursor, plus prev/next slice buttons that step the visible window by one scale unit (in Day mode they shift the day picker). Availability bar, event pips, ticks and the orange cursor line all rescale together.
- Availability bar drawn from `/api/playback/list?cam=<cam>`. Bar and scrubber share `[scrubber.min, scrubber.max]` as the denominator so blocks line up with the cursor at every scale. The bar is rendered in a neutral slate (`#4a525c`) — it must NOT match the person-pip blue (`#4ea1ff` / `--accent`), or recorded-segment fills look indistinguishable from person events.
- Vertical orange cursor line tracking the scrubber thumb (with thumb-radius offset so it lines up at the extremes). The slider input is custom-styled — gray track, orange thumb that visually continues the cursor line; the browser default progress fill is the same blue as a person pip on macOS Chrome/Safari, which we override here.
- Slider-tracks-playback: while a past mp4 is playing, a `timeupdate` listener bumps `scrubber.value = pastLoadStartOffsetSec + currentTime` so the thumb moves forward with the on-screen frame. Without this the slider stayed parked at the load point and detections that fired N seconds into playback looked like they were "before" the cursor. Gated on `userScrubbing` (set on pointerdown/touchstart, cleared on the next change event) so a mid-drag thumb doesn't jump.
- Slider input loads video only on **release** (`change` event), not on input-pause-debounce. Input-while-dragging just previews the time and updates the cursor.
- Snap-to-live: scrubbing within 30 s of the live edge swaps the `<video>` to the live HLS stream via hls.js; the thumb pins to the live edge and 5-second ticks slide it forward.
- Past-mode loads 5-minute mp4 windows from `/api/playback/get?cam=<cam>&start=<iso>&duration=300s`. The endpoint proxies MediaMTX's `/get` and buffers the response into `playback_cache/` so the served file supports `Range` requests (iOS Safari refuses to play a `<video>` source without it).

## v1 → v2 roadmap

Shipped:

- **Phase 1** — 24/7 recording, disk-watermark retention, scrubback UI.
- **Remote access (slice of phases 2 + 3)** — Caddy + oauth2-proxy on EC2 with Google OAuth allow-listing, autossh reverse tunnel from the Orin so all video and recordings still live on-prem.
- **Phase 4 (all 5 slices)** — MegaDetector + YOLO11n ensemble at 1 fps per camera, event coalescing into SQLite, retention sweep, `/api/events*` + `/events` browse page, timeline pips + prev/next event nav on the playback page, live bounding-box overlay via SSE with a "Show labels" header toggle. See `~/.claude/plans/object-detection.md`.
- **Training data pipeline** — staging→promoted layout under `/home/paulm/belfry-training/`, `dataset.yaml`-driven class map, pre-seed-from-events.db on capture, ffmpeg-driven one-click event staging from `/events`, in-browser bbox labeler at `/training` with promoted-image review. See the "Training data & labeler" section above.

Remaining (see `~/.claude/plans/we-have-this-dvr-resilient-lighthouse.md` for the rest):

- **Phase 2 leftovers** — push selected per-camera streams to an EC2 MediaMTX over SRT (caller mode, `runOnReady` + ffmpeg), driven by a new `forward: none|sub|main` field on `Camera`. Worth doing if the all-traffic-through-tunnel model strains residential upload; today every remote viewer pulls HLS through the Orin's upload.
- **Phase 3 leftovers** — Flutter mobile app (iOS + Android) consuming `/api/*` and HLS. Needs a `bearer` auth mode in FastAPI: Flutter swaps a Google ID token for a server-issued JWT and sends it via `Authorization: Bearer`.

Inference follow-ups:

- **Backfill CLI** — `inference/backfill.py` exists for one cam + a time range (e.g. `python -m inference.backfill --cam cam12 --since 90m --replace`); reuses the production `Detector` and the recorder's coalescing rules but drives off mp4 segments instead of RTSP. Useful for re-running after detector changes or filling gaps. Open work: an `--all-cams` mode + a `processed_until_mtime` watermark for incremental runs.
- **Per-class threshold tuning** — after a week of real footage, drop a calibrated `class_thresholds:` block into `cameras.yaml` (likely `person: 0.55` to silence wall/edge false positives at night, `bird: 0.30` to catch partial-frame).
- **Wildlife fine-tune (Phase B)** — YOLO11l alone has no `animal` class beyond the COCO `dog/cat/bird`, so anything else (deer, raccoon, coyote, fox) is invisible until we fine-tune. Data-collection tooling is shipped (training pipeline + labeler at `/training`); blocked on volume of labeled images. See the "Fine-tune plan" subsection under "Training data & labeler" above for the strategy/split/where-to-train/export-and-swap plan.

## Operational

- systemd on the Orin: `belfry.service`, `belfry-tunnel.service`, `belfry-inference.service`, and `nginx.service` are all `enabled` at boot. `belfry` / `belfry-tunnel` / `belfry-inference` all `Restart=on-failure` (tunnel is `Restart=always`) so crashes and drops recover automatically. Logs: `journalctl -u belfry`, `journalctl -u belfry-tunnel`, `journalctl -u belfry-inference`.
- systemd on EC2: `caddy.service` and `oauth2-proxy.service`. Logs: `journalctl -u caddy` / `journalctl -u oauth2-proxy`.
- Orin: NVIDIA Jetson Orin (aarch64), Ubuntu 22.04. NVIDIA GPU/NPU available but unused until Phase 4.
- EC2: Amazon Linux 2023 x86_64, us-west-2, paulm user, EIP attached to `yellowchicken.io`.
- Disk on Orin: 3.6 TB NVMe at `/dev/nvme0n1p1` mounted at `/`. At ~240 GB/day for 8 main-stream H.264 1080p cameras, retention defaults give ~12 days before eviction kicks in.

## Browser self-test

Belfry has a real LAN HTTP surface at `http://127.0.0.1/` on the Orin, so browser-side bugs can be reproduced and debugged directly from a shell rather than ping-ponging with the user. `.venv-playwright/` holds a Playwright install plus a linux-arm64 Chromium-headless build under `~/.cache/ms-playwright/`. Driven from system Python 3.10 (Playwright supports it; the main DVR's 3.14 venv is fine too if you'd rather not use the dedicated one).

What it lets us do that we can't do otherwise:

- **Read the JS console.** `page.on("console", lambda m: print(m.type, m.text))` catches every `Uncaught …` and `console.error(...)` — three slice-5 bugs would have been caught in one round-trip if we'd had this earlier (`CLASS_COLOR` collision, the connection-storm-on-load, the connection-storm-on-set-switch).
- **Watch network behaviour.** `page.on("requestfinished", ...)` / `page.on("requestfailed", ...)` shows whether HLS playlist fetches actually complete vs queue forever behind sticky SSE connections. Same for `/api/sets` and `/api/events` calls.
- **Time things.** `page.evaluate("performance.getEntriesByType('resource')")` gives per-resource timing; useful for measuring whether the 1.5 s SSE-deferral is actually winning the race.
- **Drive flows.** Click set links, toggle "Show labels", hit `[` / `]` to step through events, etc., then assert about resulting DOM state.

Provisioning is in `runme.sh` (creates `.venv-playwright/`, pip-installs `playwright`, `playwright install-deps chromium` under sudo for apt-side libs, then `playwright install chromium` as the user so the browser binary lands in `~/.cache/ms-playwright/`). Reentrant — re-runs skip the slow steps.

Minimal test pattern:

```python
from playwright.sync_api import sync_playwright

errs = []
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    page.on("console", lambda m: m.type in ("error", "warning") and errs.append(m.text))
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto("http://127.0.0.1/sets/set1")
    page.wait_for_selector("#grid .tile")
    # ... interact, then read state ...
    b.close()
print("errors:", errs)
```
