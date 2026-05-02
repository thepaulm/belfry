from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

from .config import Camera

Status = Literal["live", "offline", "disabled"]

_PROBE_TIMEOUT_S = 6.0
_CACHE_TTL_S = 10.0


@dataclass(frozen=True)
class Probe:
    name: str
    status: Status
    checked_at: float


_cache: dict[str, Probe] = {}
_locks: dict[str, asyncio.Lock] = {}


async def _ffprobe(rtsp: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-rw_timeout", str(int(_PROBE_TIMEOUT_S * 1_000_000)),
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        rtsp,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S + 1)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    return proc.returncode == 0 and b"video" in stdout


async def probe(camera: Camera) -> Probe:
    if not camera.enabled:
        return Probe(camera.name, "disabled", time.time())

    cached = _cache.get(camera.name)
    if cached and (time.time() - cached.checked_at) < _CACHE_TTL_S:
        return cached

    lock = _locks.setdefault(camera.name, asyncio.Lock())
    async with lock:
        cached = _cache.get(camera.name)
        if cached and (time.time() - cached.checked_at) < _CACHE_TTL_S:
            return cached
        ok = await _ffprobe(camera.rtsp)
        result = Probe(camera.name, "live" if ok else "offline", time.time())
        _cache[camera.name] = result
        return result


async def probe_all(cameras: tuple[Camera, ...]) -> list[Probe]:
    return list(await asyncio.gather(*(probe(c) for c in cameras)))
