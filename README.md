# belfry

A self-hosted DVR and object-detection system for IP security cameras. It runs
on a single small box on your LAN, records every camera 24/7, lets you watch
live and scrub back through recordings from a browser, and runs on-device AI to
detect and index events (people, animals, vehicles) so you can jump straight to
the moments that matter instead of scrubbing through hours of empty footage.

Everything — video, recordings, and the detection database — stays on your own
hardware. An optional cloud front door lets you reach it from anywhere behind
Google sign-in, without exposing the box to the public internet.

It was built for [Interlogix TVB-5301](https://www.interlogix.com) cameras
(OEM Hikvision) but works with any camera that exposes a standard **RTSP**
stream.

## What it does

- **24/7 recording.** Pulls RTSP from each camera and writes continuous
  1-hour video segments to disk. When the disk fills past a watermark, the
  oldest footage is automatically evicted — set it and forget it.
- **Live view.** A browser grid shows all your cameras live, grouped into named
  "sets" (e.g. *Front*, *Backyard*). Switching between sets is instant.
- **Scrub-back.** A per-camera playback page with a day picker and Day / Hour /
  5-min zoom on the timeline. Seek anywhere in your retained history.
- **Object detection.** An on-device YOLO model watches every camera at ~1 fps
  and records *events* — "a person was here from 3:14 to 3:15", with a
  thumbnail — into a small searchable database. Detects `person`, `dog`, `cat`,
  `bird`, `car`, and `truck` out of the box, plus wildlife (`deer`, `coyote`,
  `raccoon`, `rabbit`, `squirrel`, `rat`) once you fine-tune (see below).
- **Event browsing.** A cross-camera event feed with filters (by class, camera,
  time window). Click any event to jump to that moment in playback. Colored pips
  on the playback timeline mark every event; `[` / `]` step between them.
- **Live & playback overlays.** A "Show labels" toggle draws bounding boxes over
  the video — live, or by re-running detection over recorded footage you're
  watching.
- **Remote access (optional).** A tiny cloud instance terminates HTTPS and gates
  every request behind Google OAuth, then tunnels back to the box over SSH. Your
  video never leaves your hardware; the public side only enforces auth.
- **Training pipeline (optional).** An in-browser bounding-box labeler and a
  staging→promoted dataset layout for fine-tuning the detector on classes the
  stock model doesn't know (deer, raccoon, coyote, …).

## How it works

```
   cameras (RTSP)                  the box (e.g. a Jetson Orin or any Linux host)
  ┌────────────┐        ┌───────────────────────────────────────────────────┐
  │  cam1 ─────┼──RTSP──┤  MediaMTX ──┬── HLS  (live tiles)                   │
  │  cam2 ─────┼──RTSP──┤             ├── mp4 segments  → recordings/         │
  │  …         │        │             └── loopback RTSP → inference (YOLO)    │
  │  camN ─────┼──RTSP──┤                                    │                │
  └────────────┘        │  FastAPI (viewer, playback, events, training APIs)  │
                        │  nginx  (:80, fronts everything for the LAN)        │
                        └───────────────────────────────────────────────────┘
                                              │ SSH reverse tunnel (optional)
                                              ▼
                          EC2 front door: Caddy + oauth2-proxy (HTTPS + Google login)
```

- **[MediaMTX](https://github.com/bluenviron/mediamtx)** holds one RTSP session
  per camera and fans it out to three consumers: HLS for live viewing, an mp4
  recorder, and the inference reader — all off one upstream connection per
  camera.
- A **FastAPI** app serves the viewer UI, playback, the events API, and the
  training tools.
- **nginx** fronts everything on port 80 for LAN access.
- The **inference** pipeline runs as a separate service (so an AI crash can't
  take down recording) and writes events to a SQLite database plus thumbnail
  JPEGs.

For the full architecture — path topology, retention internals, the cloud front
door, inference details, and the training/fine-tune pipeline — see
[CLAUDE.md](CLAUDE.md).

## Requirements

- A Linux host on the same LAN as your cameras (built and run on an NVIDIA
  Jetson Orin, aarch64, Ubuntu 22.04; any x86-64 Linux works for the DVR).
- One or more IP cameras exposing an RTSP stream.
- A disk sized for your retention needs. Rule of thumb: ~30 GB/day per 1080p
  H.264 camera at full frame rate.
- [uv](https://github.com/astral-sh/uv) for the Python DVR.
- For object detection: an NVIDIA GPU and the Jetson PyTorch/TensorRT stack
  (the install script provisions it). Detection is optional — the DVR records
  and plays back fine without it.

## Install

```bash
git clone git@github.com:thepaulm/belfry.git
cd belfry

# 1. Fetch the MediaMTX binary for your platform.
scripts/install-mediamtx.sh

# 2. Configure MediaMTX. The real config is gitignored; start from the template.
cp mediamtx/mediamtx.example.yml mediamtx/mediamtx.yml

# 3. Configure your cameras. cameras.yaml is gitignored — keep it mode 0600.
cp cameras.example.yaml cameras.yaml
chmod 600 cameras.yaml
$EDITOR cameras.yaml      # add each camera's RTSP URL + credentials

# 4. Install Python deps (uv reads pyproject.toml / uv.lock).
uv sync
```

### `cameras.yaml`

The source of truth for camera grouping, credentials, recording path, retention
watermarks, and detection settings. Cameras are organized into named **sets**;
each camera `name` is its unique stream key. Minimal example:

```yaml
hls_base: /hls
recording:
  path: ./recordings
retention:
  evict_high_pct: 85    # start evicting oldest footage above this disk %
  evict_low_pct: 80     # evict down to this
sets:
  - id: set1
    label: Front
    cameras:
      - name: cam1
        label: Driveway
        enabled: true
        inference: true   # opt this camera into object detection
        rtsp: rtsp://admin:PASSWORD@192.168.1.10:554/Streaming/Channels/101
```

See [`cameras.example.yaml`](cameras.example.yaml) for every option (per-class
detection thresholds, event coalescing, motion detection, etc.).

### Object detection (optional)

Detection runs in its own Python 3.10 venv because Jetson PyTorch wheels are
cp310-only, separate from the DVR's interpreter:

```bash
scripts/install-inference.sh   # provisions .venv-inference/, downloads YOLO11l,
                               # builds a device-specific TensorRT engine
```

## Run

For a quick local start:

```bash
scripts/run.sh                 # starts MediaMTX + the FastAPI viewer together
```

Then open **http://localhost:9090/** (or, behind nginx, **http://&lt;box-ip&gt;/**).

For a permanent install, use the provided systemd units (run on the box):

```bash
sudo cp scripts/belfry.service scripts/belfry-inference.service /etc/systemd/system/
sudo systemctl enable --now belfry belfry-inference
```

| Service                   | What it does                                  | Logs                              |
| ------------------------- | --------------------------------------------- | --------------------------------- |
| `belfry.service`          | MediaMTX + FastAPI viewer + recording         | `journalctl -u belfry`            |
| `belfry-inference.service`| Object detection across all enabled cameras   | `journalctl -u belfry-inference`  |
| `belfry-tunnel.service`   | Reverse SSH tunnel to the cloud front door    | `journalctl -u belfry-tunnel`     |

### Pages

- `/sets/<id>` — live grid for a set
- `/sets/<set>/<cam>/playback` — scrub-back for one camera
- `/events` — cross-camera event browser
- `/training` — in-browser bounding-box labeler

## Remote access (optional)

To reach belfry from outside your LAN without exposing the box, run the cloud
front door on a small instance (the setup targets Amazon Linux 2023 on EC2):
**Caddy** auto-issues HTTPS certs and gates every request through
**oauth2-proxy** with Google sign-in; an `autossh` reverse tunnel from the box
to the instance carries the traffic. All video and recordings stay on the box.
Bootstrap with [`cloud/install-ec2.sh`](cloud/) and see the *Cloud frontdoor*
and *Adding a user* sections of [CLAUDE.md](CLAUDE.md) for the allow-list
details.

## License

[MIT](LICENSE) © 2026 Paul Mikesell
