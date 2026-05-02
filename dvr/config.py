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
class Config:
    hls_base: str
    sets: tuple[CameraSet, ...]

    def get_set(self, id: str) -> CameraSet | None:
        return next((s for s in self.sets if s.id == id), None)

    @property
    def all_cameras(self) -> tuple[Camera, ...]:
        return tuple(c for s in self.sets for c in s.cameras)


def load_config(path: Path | str = "cameras.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text())

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

    return Config(hls_base=raw["hls_base"], sets=sets)
