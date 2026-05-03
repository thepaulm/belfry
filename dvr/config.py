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
        cooldown_s=int(block.get("cooldown_s", 10)),
        record_fps=int(block.get("record_fps", 1)),
        live_fps=int(block.get("live_fps", 5)),
        thumbs_dir=_resolve(
            block.get("thumbs_dir", recording.path / "thumbs"), project_root
        ),
        db_path=_resolve(
            block.get("db_path", recording.path / "events.db"), project_root
        ),
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
