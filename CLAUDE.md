# homecam

Self-hosted DVR for Interlogix TVB-5301 cameras (OEM Hikvision) on a dedicated, isolated camera subnet. The on-prem box pulls RTSP from each camera, MediaMTX repackages to HLS, and a small FastAPI service serves a per-set browser viewer.

## Layout

- `dvr/` — FastAPI app: `server.py`, `config.py`, `health.py`, `static/` viewer.
- `mediamtx/` — MediaMTX binary + `mediamtx.yml` (one path per known camera, on-demand RTSP). Real config is gitignored; `mediamtx.example.yml` is the template.
- `cameras.yaml` — source of truth for camera grouping (sets) and credentials. Gitignored, mode 0600. Template: `cameras.example.yaml`.
- `scripts/run.sh` — starts MediaMTX + uvicorn together; cleans up on Ctrl-C.
- `scripts/install-mediamtx.sh` — fetches the MediaMTX binary for the host arch.
- `.env` — `DVR_USERNAME` / `DVR_PASSWORD` for HTTP Basic auth on the viewer. Gitignored.

## Cameras and sets

The user has a 4-port camera switch and rotates physical cameras through those ports. cameras.yaml groups cameras into named **sets** (e.g. `set1`, `set2`). The DVR has per-set viewer pages at `/sets/<id>` with cross-links in the header. Camera `name` must be unique across all sets — it's the global MediaMTX path key.

## v1 architecture (LAN-only) and v2 plan

- **v1 (current):** on-prem box on the camera subnet runs MediaMTX + FastAPI; viewer is browser-on-LAN with HTTP Basic auth.
- **v2 (planned):** on-prem MediaMTX pushes streams up to an EC2 MediaMTX over SRT; EC2 fronts HLS to authenticated browsers via Google OAuth, TLS terminated by Caddy.
