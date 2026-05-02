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
class Config:
    hls_base: str
    sets: tuple[CameraSet, ...]
    recording: Recording
    retention: Retention

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

    return Config(
        hls_base=raw["hls_base"],
        sets=sets,
        recording=_load_recording(raw, project_root),
        retention=retention,
    )
