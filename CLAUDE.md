# homecam

Self-hosted DVR for Interlogix TVB-5301 cameras (OEM Hikvision) on a dedicated, isolated camera subnet. The on-prem box pulls RTSP from each camera, MediaMTX repackages to HLS for live viewing and writes 1-hour fMP4 segments to disk for recording, and a small FastAPI service serves a per-set browser viewer plus a per-camera scrubback page. nginx fronts everything on port 80.

## Layout

- `dvr/` — FastAPI app:
  - `server.py` — routes (live, playback, retention status, set/camera APIs)
  - `config.py` — `cameras.yaml` loader; `Camera`, `CameraSet`, `Recording`, `Retention`, `Config` dataclasses
  - `health.py` — `ffprobe`-based per-camera reachability checks
  - `retention.py` — async lifespan task that evicts oldest mp4 segments when the recordings volume crosses watermarks
  - `static/` — vanilla-JS viewer: `index.html` (live grid), `playback.html` (scrubback), shared `viewer.css`
- `mediamtx/` — MediaMTX binary + `mediamtx.yml`. Real config is gitignored; `mediamtx.example.yml` is the template.
- `cameras.yaml` — source of truth for camera grouping (sets), credentials, recording path, retention watermarks. Gitignored, mode 0600. Template: `cameras.example.yaml`.
- `recordings/` — gitignored, on the 3.6 TB NVMe (`/dev/nvme0n1p1`). One subdir per camera (`recordings/cam5/...`). MediaMTX writes 1-hour fMP4 segments here.
- `scripts/run.sh` — starts MediaMTX + uvicorn together; cleans up on Ctrl-C. Invoked by the `homecam` systemd unit at `/etc/systemd/system/homecam.service`.
- `scripts/install-mediamtx.sh` — fetches the MediaMTX binary for the host arch.
- `.env` — `DVR_USERNAME` / `DVR_PASSWORD` for HTTP Basic auth on the viewer. Gitignored.
- `runme.sh` — gitignored throwaway shell script. Convention: when something needs sudo, write the steps here for the user to run from their own terminal.

## Path topology in MediaMTX

Each camera is a single always-on MediaMTX path (`sourceOnDemand: no`). The path holds exactly one upstream RTSP session to the camera and feeds multiple consumers off the loopback RTSP server (`rtsp://127.0.0.1:8554/<cam>`):

- HLS muxer (live tiles) — `hlsAlwaysRemux: yes` keeps a muxer warm per path so set-switching is instant.
- Recorder — writes fMP4 segments under `recordings/<cam>/`.
- Future inference and SRT-forward consumers tap the same loopback path, no extra camera connection.

The MediaMTX `playback` (`:9996`) and `api` (`:9997`) endpoints are loopback-only; nginx fronts `playback` at `/playback/` for the scrubback UI.

## nginx

`/etc/nginx/sites-available/homecam` listens on `:80` and proxies:

- `/hls/` → `127.0.0.1:8888` (MediaMTX HLS), with `proxy_redirect ~^/(.*)$ /hls/$1` to rewrite MediaMTX's bare-path 302 Locations so the cookie-check redirect stays inside `/hls/`.
- `/playback/` → `127.0.0.1:9996` (MediaMTX playback API), same redirect-rewrite trick.
- everything else → `127.0.0.1:9090` (FastAPI).

`hls_base: /hls` in `cameras.yaml` makes camera HLS URLs same-origin so the browser stays under one host on port 80.

## Cameras and sets

8 cameras total. 7 live on the isolated camera subnet `192.168.254.0/24`; cam12 lives at `192.168.1.70` on the LAN subnet (a re-IP via the camera UI didn't take). To reach cam12 from the box, `192.168.1.50/24` is a persistent secondary address on `eno1` via NetworkManager (`nmcli connection modify "Wired connection 1" +ipv4.addresses 192.168.1.50/24`).

cameras.yaml groups cameras into named **sets** (e.g. `set1`, `set2`). The DVR has per-set viewer pages at `/sets/<id>` with cross-links in the header. Camera `name` must be unique across all sets — it's the global MediaMTX path key.

## Recording and retention

- 24/7 recording. Each camera writes 1-hour fMP4 segments (`recordSegmentDuration: 1h`, `recordPartDuration: 1s`).
- Disk-aware retention runs as a FastAPI lifespan task (`dvr/retention.py`). Every `scan_interval_s` (default 60s) it `shutil.disk_usage`s the recordings volume; if usage > `evict_high_pct` (default 85), it deletes oldest mp4 files globally until usage < `evict_low_pct` (default 80). Files modified within the last 70 minutes are protected so MediaMTX never has its in-progress segment yanked. Per-camera dirs share the disk fairly under "oldest-globally" because all 8 cams record at similar bitrate.
- Status visible at `GET /api/retention/status` (HTTPBasic-gated). Eviction events log to journald under `homecam`.

## Scrubback UI

`/sets/<set>/<cam>/playback` serves a per-camera page with:

- Day picker (last 14 local days; days with footage marked `●`, empty days `○`).
- 24-hour scrubber with availability bar driven by `/api/playback/list?cam=<cam>`. Both bar and scrubber share the same denominator (scrubber.max), so on today they stretch from 00:00 to "now".
- Vertical orange cursor line tracking the scrubber thumb (with thumb-radius offset so it lines up at the extremes).
- Snap-to-live: scrubbing within 30 s of the live edge swaps the `<video>` to the live HLS stream via hls.js; the thumb pins to the live edge and 5-second ticks slide it forward.
- Past-mode loads `format=mp4` 5-minute windows from `/playback/get?path=<cam>&start=<iso>&duration=300s`. Debounced 250 ms while dragging.

## v1 → v2 roadmap

v1 (current state, this codebase): LAN-only viewer + 24/7 recording + scrubback. Phase 1 of the roadmap is complete.

v2 (planned, see `~/.claude/plans/we-have-this-dvr-resilient-lighthouse.md` for the full plan):

- **Phase 2** — push selected per-camera streams to an EC2 MediaMTX over SRT (caller mode, `runOnReady` + ffmpeg), driven by a new `forward: none|sub|main` field on `Camera`. Sub-stream (Channels/102, ~1 Mbps) by default to fit residential upstream.
- **Phase 3** — Caddy + oauth2-proxy on EC2 for Google OAuth, same FastAPI codebase deployed cloud-side with a swappable auth mode (`basic`/`forwarded`/`bearer`). Flutter mobile app (iOS + Android) consumes `/api/*` and HLS via bearer JWT issued from a Google ID token.
- **Phase 4** — on-Jetson YOLOv8n inference per camera (sub-stream tap, 1 fps), SQLite event store under `recordings/events.db`, event surfacing in the viewer + Flutter app, FCM push.

## Operational

- systemd: `homecam.service` and `nginx.service` are both `enabled` at boot. `homecam` `Restart=on-failure` so a crash recovers automatically. Logs: `journalctl -u homecam`.
- Box: NVIDIA Jetson Orin (aarch64), Ubuntu 22.04. NVIDIA GPU/NPU available but unused until Phase 4.
- Disk: 3.6 TB NVMe at `/dev/nvme0n1p1` mounted at `/`. At ~240 GB/day for 8 main-stream H.264 1080p cameras, retention defaults give ~12 days before eviction kicks in.
