# belfry

Self-hosted DVR for Interlogix TVB-5301 cameras (OEM Hikvision) on a dedicated, isolated camera subnet. The on-prem Jetson Orin pulls RTSP from each camera, MediaMTX repackages to HLS for live viewing and writes 1-hour fMP4 segments to disk for recording, and a small FastAPI service serves a per-set browser viewer plus a per-camera scrubback page. nginx fronts everything on port 80 for LAN access. A small EC2 frontdoor (Caddy + oauth2-proxy) terminates HTTPS for `yellowchicken.io`, gates every request behind Google OAuth, and reverse-proxies through a persistent SSH tunnel back to the Orin — so all video and recordings stay on the Orin while the public side enforces auth.

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
- `playback_cache/` — gitignored sibling of `recordings/`. FastAPI buffers each MediaMTX `/get` response here so byte-range requests work (iOS Safari requires it for `<video>`); LRU-evicted at a 2 GiB cap.
- `scripts/run.sh` — starts MediaMTX + uvicorn together; cleans up on Ctrl-C. Invoked by the `belfry` systemd unit at `/etc/systemd/system/belfry.service`.
- `scripts/install-mediamtx.sh` — fetches the MediaMTX binary for the host arch.
- `scripts/nginx-belfry.conf` — tracked copy of the Orin's nginx site (`/etc/nginx/sites-available/belfry`).
- `scripts/belfry-tunnel.service` — autossh systemd unit; opens the reverse SSH tunnel from Orin to EC2 (Caddy upstream + admin SSH back-in).
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

## Cameras and sets

8 cameras total. 7 live on the isolated camera subnet `192.168.254.0/24`; cam12 lives at `192.168.1.70` on the LAN subnet (a re-IP via the camera UI didn't take). To reach cam12 from the box, `192.168.1.50/24` is a persistent secondary address on `eno1` via NetworkManager (`nmcli connection modify "Wired connection 1" +ipv4.addresses 192.168.1.50/24`).

cameras.yaml groups cameras into named **sets** (e.g. `set1`, `set2`). The DVR has per-set viewer pages at `/sets/<id>` with cross-links in the header. Camera `name` must be unique across all sets — it's the global MediaMTX path key.

## Recording and retention

- 24/7 recording. Each camera writes 1-hour fMP4 segments (`recordSegmentDuration: 1h`, `recordPartDuration: 1s`).
- Disk-aware retention runs as a FastAPI lifespan task (`dvr/retention.py`). Every `scan_interval_s` (default 60s) it `shutil.disk_usage`s the recordings volume; if usage > `evict_high_pct` (default 85), it deletes oldest mp4 files globally until usage < `evict_low_pct` (default 80). Files modified within the last 70 minutes are protected so MediaMTX never has its in-progress segment yanked. Per-camera dirs share the disk fairly under "oldest-globally" because all 8 cams record at similar bitrate.
- Status visible at `GET /api/retention/status`. Eviction events log to journald under `belfry`.

## Scrubback UI

`/sets/<set>/<cam>/playback` serves a per-camera page with:

- Day picker (last 14 local days; days with footage marked `●`, empty days `○`).
- 24-hour scrubber with availability bar driven by `/api/playback/list?cam=<cam>`. Both bar and scrubber share the same denominator (scrubber.max), so on today they stretch from 00:00 to "now".
- Vertical orange cursor line tracking the scrubber thumb (with thumb-radius offset so it lines up at the extremes).
- Snap-to-live: scrubbing within 30 s of the live edge swaps the `<video>` to the live HLS stream via hls.js; the thumb pins to the live edge and 5-second ticks slide it forward.
- Past-mode loads 5-minute mp4 windows from `/api/playback/get?cam=<cam>&start=<iso>&duration=300s`. The endpoint proxies MediaMTX's `/get` and buffers the response into `playback_cache/` so the served file supports `Range` requests (iOS Safari refuses to play a `<video>` source without it). Debounced 250 ms while dragging; the slider's `change` event cancels the pending input-debounce so duplicate requests don't race.

## v1 → v2 roadmap

Shipped:

- **Phase 1** — 24/7 recording, disk-watermark retention, scrubback UI.
- **Remote access (slice of phases 2 + 3)** — Caddy + oauth2-proxy on EC2 with Google OAuth allow-listing, autossh reverse tunnel from the Orin so all video and recordings still live on-prem.

Remaining (see `~/.claude/plans/we-have-this-dvr-resilient-lighthouse.md` for the full plan):

- **Phase 2 leftovers** — push selected per-camera streams to an EC2 MediaMTX over SRT (caller mode, `runOnReady` + ffmpeg), driven by a new `forward: none|sub|main` field on `Camera`. Worth doing if the all-traffic-through-tunnel model strains residential upload; today every remote viewer pulls HLS through the Orin's upload.
- **Phase 3 leftovers** — Flutter mobile app (iOS + Android) consuming `/api/*` and HLS. Needs a `bearer` auth mode in FastAPI: Flutter swaps a Google ID token for a server-issued JWT and sends it via `Authorization: Bearer`.
- **Phase 4** — on-Jetson YOLOv8n inference per camera (sub-stream tap, 1 fps), SQLite event store under `recordings/events.db`, event surfacing in the viewer + Flutter app, FCM push.

## Operational

- systemd on the Orin: `belfry.service`, `belfry-tunnel.service`, and `nginx.service` are all `enabled` at boot. `belfry` and `belfry-tunnel` `Restart=on-failure` / `Restart=always` so crashes and tunnel drops recover automatically. Logs: `journalctl -u belfry` / `journalctl -u belfry-tunnel`.
- systemd on EC2: `caddy.service` and `oauth2-proxy.service`. Logs: `journalctl -u caddy` / `journalctl -u oauth2-proxy`.
- Orin: NVIDIA Jetson Orin (aarch64), Ubuntu 22.04. NVIDIA GPU/NPU available but unused until Phase 4.
- EC2: Amazon Linux 2023 x86_64, us-west-2, paulm user, EIP attached to `yellowchicken.io`.
- Disk on Orin: 3.6 TB NVMe at `/dev/nvme0n1p1` mounted at `/`. At ~240 GB/day for 8 main-stream H.264 1080p cameras, retention defaults give ~12 days before eviction kicks in.
