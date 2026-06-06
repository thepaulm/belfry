from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class Camera:
    name: str
    label: str
    rtsp: str
    enabled: bool
    inference: bool = False
    # Per-camera override for the inference motion detector. None falls
    # back to ``inference.motion_enabled`` global. Set False on cameras
    # where wind-blown vegetation or repeated light/shadow play would
    # produce a torrent of false positives.
    motion: bool | None = None

    def hls_url(self, base: str) -> str:
        return f"{base.rstrip('/')}/{self.name}/index.m3u8"

    @property
    def host(self) -> str:
        """Hostname or IP from the RTSP URL — no credentials, no port."""
        return urlparse(self.rtsp).hostname or ""

    @property
    def web_url(self) -> str:
        """HTTP web UI URL on the camera (port 80)."""
        return f"http://{self.host}/"


@dataclass(frozen=True)
class CameraSet:
    id: str
    label: str
    cameras: tuple[Camera, ...]


@dataclass(frozen=True)
class Recording:
    path: Path


@dataclass(frozen=True)
class Retention:
    evict_high_pct: int
    evict_low_pct: int
    scan_interval_s: int


@dataclass(frozen=True)
class Inference:
    # Model files. Resolved relative to project root if not absolute.
    # `.engine` (TensorRT) preferred for speed; falls back to `.pt` (PyTorch
    # CUDA) when the engine doesn't exist on disk yet.
    yolo_pt: Path
    yolo_engine: Path
    # Active classes (subset of COCO). Anything not in this set is dropped
    # at write time. The detector additionally restricts to a hard-coded
    # COCO subset so out-of-domain classes (toaster, frisbee, etc.) never
    # leak through even if the config opens them up.
    event_classes: tuple[str, ...]
    # Single default confidence threshold; per-class overrides win when the
    # class key is present.
    conf_threshold: float
    class_thresholds: dict[str, float]
    # Output renames applied by the detector after filtering/thresholding
    # (event_classes and class_thresholds key on the raw model names).
    # Defaults to car/truck → vehicle; set `class_aliases: {}` in
    # cameras.yaml to keep the raw classes.
    class_aliases: dict[str, str]
    # Coalescing window: same (camera, class) detections within this many
    # seconds extend a single event row instead of opening a new one.
    cooldown_s: int
    # Frame-rate caps for the two consumers of the GPU worker.
    record_fps: int   # always-on event detector
    live_fps: int     # on-demand viewer overlay (slice 5)
    # Where event thumbnails get written.
    thumbs_dir: Path
    # SQLite events DB.
    db_path: Path
    # Motion-detection knobs (OpenCV MOG2). Default off; per-camera
    # ``motion`` override on Camera flips it on per cam.
    motion_enabled: bool
    motion_history: int
    motion_var_threshold: int
    motion_min_blob_pct: float
    motion_min_persistence_frames: int

    def motion_on_for(self, cam: "Camera") -> bool:
        """Effective motion setting for a camera: per-cam override wins,
        global default fills in when the cam is unset."""
        return self.motion_enabled if cam.motion is None else cam.motion


@dataclass(frozen=True)
class Config:
    hls_base: str
    sets: tuple[CameraSet, ...]
    recording: Recording
    retention: Retention
    inference: Inference

    def get_set(self, id: str) -> CameraSet | None:
        return next((s for s in self.sets if s.id == id), None)

    @property
    def all_cameras(self) -> tuple[Camera, ...]:
        return tuple(c for s in self.sets for c in s.cameras)


def _load_recording(raw: dict, project_root: Path) -> Recording:
    block = raw.get("recording") or {}
    path = Path(block.get("path", "./recordings"))
    if not path.is_absolute():
        path = (project_root / path).resolve()
    return Recording(path=path)


def _load_retention(raw: dict) -> Retention:
    block = raw.get("retention") or {}
    return Retention(
        evict_high_pct=int(block.get("evict_high_pct", 85)),
        evict_low_pct=int(block.get("evict_low_pct", 80)),
        scan_interval_s=int(block.get("scan_interval_s", 60)),
    )


def _resolve(p: str | Path, project_root: Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (project_root / path).resolve()


def _load_inference(raw: dict, project_root: Path, recording: Recording) -> Inference:
    block = raw.get("inference") or {}
    classes = tuple(
        block.get(
            "event_classes",
            ["person", "dog", "cat", "bird", "car", "truck"],
        )
    )
    motion = block.get("motion") or {}
    # `None` (key absent) gets the default; an explicit empty mapping in
    # cameras.yaml means "no aliasing", so don't `or` it away.
    aliases = block.get("class_aliases")
    if aliases is None:
        aliases = {"car": "vehicle", "truck": "vehicle"}
    return Inference(
        yolo_pt=_resolve(
            block.get("yolo_pt", "inference/yolo11l.pt"), project_root
        ),
        yolo_engine=_resolve(
            block.get("yolo_engine", "inference/yolo11l.engine"), project_root
        ),
        event_classes=classes,
        conf_threshold=float(block.get("conf_threshold", 0.40)),
        class_thresholds={k: float(v) for k, v in (block.get("class_thresholds") or {}).items()},
        class_aliases={str(k): str(v) for k, v in aliases.items()},
        cooldown_s=int(block.get("cooldown_s", 10)),
        record_fps=int(block.get("record_fps", 1)),
        live_fps=int(block.get("live_fps", 5)),
        thumbs_dir=_resolve(
            block.get("thumbs_dir", recording.path / "thumbs"), project_root
        ),
        db_path=_resolve(
            block.get("db_path", recording.path / "events.db"), project_root
        ),
        motion_enabled=bool(motion.get("enabled", False)),
        motion_history=int(motion.get("history", 500)),
        motion_var_threshold=int(motion.get("var_threshold", 25)),
        motion_min_blob_pct=float(motion.get("min_blob_pct", 0.005)),
        motion_min_persistence_frames=int(motion.get("min_persistence_frames", 2)),
    )


def load_config(path: Path | str = "cameras.yaml") -> Config:
    cfg_path = Path(path)
    raw = yaml.safe_load(cfg_path.read_text())
    project_root = cfg_path.resolve().parent

    sets = tuple(
        CameraSet(
            id=s["id"],
            label=s.get("label", s["id"]),
            cameras=tuple(
                Camera(
                    name=c["name"],
                    label=c.get("label", c["name"]),
                    rtsp=c["rtsp"],
                    enabled=bool(c.get("enabled", True)),
                    inference=bool(c.get("inference", False)),
                    motion=(None if c.get("motion") is None else bool(c.get("motion"))),
                )
                for c in (s.get("cameras") or [])
            ),
        )
        for s in raw["sets"]
    )

    seen: dict[str, str] = {}
    for s in sets:
        for c in s.cameras:
            if c.name in seen:
                raise ValueError(
                    f"camera name {c.name!r} appears in both set {seen[c.name]!r} "
                    f"and set {s.id!r}; names must be unique across all sets"
                )
            seen[c.name] = s.id

    retention = _load_retention(raw)
    if not (0 < retention.evict_low_pct < retention.evict_high_pct < 100):
        raise ValueError(
            f"retention watermarks must satisfy 0 < low ({retention.evict_low_pct}) "
            f"< high ({retention.evict_high_pct}) < 100"
        )

    recording = _load_recording(raw, project_root)
    return Config(
        hls_base=raw["hls_base"],
        sets=sets,
        recording=recording,
        retention=retention,
        inference=_load_inference(raw, project_root, recording),
    )
