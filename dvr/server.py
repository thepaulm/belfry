from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from hashlib import sha1
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from .config import Camera, CameraSet, load_config
from .health import probe_all
from .retention import RetentionLoop

# MediaMTX HLS lives on this box; warmup hits it directly, bypassing nginx.
_MEDIAMTX_HLS = "http://127.0.0.1:8888"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

config = load_config(PROJECT_ROOT / "cameras.yaml")

retention_loop = RetentionLoop(config.recording, config.retention, config.inference)


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    retention_loop.start()
    try:
        yield
    finally:
        await retention_loop.stop()


app = FastAPI(title="belfry-dvr", lifespan=_lifespan)


def _camera_payload(c: Camera) -> dict:
    return {
        "name": c.name,
        "label": c.label,
        "enabled": c.enabled,
        "host": c.host,
        "web_url": c.web_url,
        "hls_url": c.hls_url(config.hls_base) if c.enabled else None,
    }


def _resolve_set(set_id: str):
    s = config.get_set(set_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"unknown set: {set_id}")
    return s


@app.get("/api/sets")
async def api_sets() -> list[dict]:
    return [
        {"id": s.id, "label": s.label, "camera_count": len(s.cameras)}
        for s in config.sets
    ]


@app.get("/api/sets/{set_id}/cameras")
async def api_set_cameras(set_id: str) -> list[dict]:
    s = _resolve_set(set_id)
    return [_camera_payload(c) for c in s.cameras]


@app.get("/api/sets/{set_id}/health")
async def api_set_health(set_id: str) -> list[dict]:
    s = _resolve_set(set_id)
    probes = await probe_all(s.cameras)
    return [{"name": p.name, "status": p.status, "checked_at": p.checked_at} for p in probes]


@app.get("/api/retention/status")
async def api_retention_status() -> dict:
    return retention_loop.status_dict()


_MEDIAMTX_PLAYBACK = "http://127.0.0.1:9996"

# MediaMTX's /get streams chunked MP4 with `Accept-Ranges: none`. iOS Safari
# (especially iPad) refuses to play a <video> source that doesn't honor Range
# requests, so we buffer the response to disk on first hit and let FastAPI's
# FileResponse serve subsequent reads with proper byte-range support.
_PLAYBACK_CACHE_DIR = config.recording.path.parent / "playback_cache"
_PLAYBACK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_PLAYBACK_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def _evict_playback_cache() -> None:
    files = sorted(
        _PLAYBACK_CACHE_DIR.glob("*.mp4"),
        key=lambda p: p.stat().st_mtime,
    )
    total = sum(p.stat().st_size for p in files)
    while total > _PLAYBACK_CACHE_MAX_BYTES and files:
        oldest = files.pop(0)
        try:
            sz = oldest.stat().st_size
            oldest.unlink()
            total -= sz
        except OSError:
            pass


@app.get("/api/playback/list")
async def api_playback_list(cam: str) -> list[dict]:
    """Available recorded ranges for a camera, proxied so the cloud OAuth gate
    sits in front of MediaMTX."""
    if cam not in {c.name for c in config.all_cameras}:
        raise HTTPException(status_code=404, detail=f"unknown camera: {cam}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{_MEDIAMTX_PLAYBACK}/list", params={"path": cam})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"mediamtx playback unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    # Strip the absolute internal URL — the browser uses /api/playback/get directly.
    ranges = r.json()
    return [{"start": x["start"], "duration": x["duration"]} for x in ranges]


@app.get("/api/playback/get")
async def api_playback_get(
    cam: str,
    start: str,
    duration: str,
) -> FileResponse:
    if cam not in {c.name for c in config.all_cameras}:
        raise HTTPException(status_code=404, detail=f"unknown camera: {cam}")

    key = sha1(f"{cam}|{start}|{duration}".encode()).hexdigest()
    cache_path = _PLAYBACK_CACHE_DIR / f"{key}.mp4"

    if not cache_path.exists():
        # Unique tmp suffix per request — the browser fires two parallel hits
        # for the same URL on each scrub (one from the input-debounce, one
        # from the change event). With a shared .partial path they'd race and
        # one os.replace would yank the file from under the other.
        tmp_path = cache_path.with_suffix(f".mp4.{uuid.uuid4().hex}.partial")
        upstream = f"{_MEDIAMTX_PLAYBACK}/get"
        params = {"path": cam, "start": start, "duration": duration, "format": "mp4"}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("GET", upstream, params=params) as r:
                    if r.status_code != 200:
                        body = await r.aread()
                        raise HTTPException(
                            status_code=r.status_code,
                            detail=body.decode("latin1", errors="replace"),
                        )
                    with tmp_path.open("wb") as f:
                        async for chunk in r.aiter_bytes(64 * 1024):
                            f.write(chunk)
        except httpx.HTTPError as e:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=502, detail=f"mediamtx unreachable: {e}")
        os.replace(tmp_path, cache_path)
        _evict_playback_cache()
    else:
        # Bump mtime so LRU eviction treats this as recently used.
        os.utime(cache_path, None)

    return FileResponse(cache_path, media_type="video/mp4")


@app.get("/")
async def index():
    if not config.sets:
        raise HTTPException(status_code=500, detail="no camera sets configured")
    return RedirectResponse(url=f"/sets/{config.sets[0].id}", status_code=302)


async def _warm_source(name: str) -> None:
    # Touching the manifest triggers MediaMTX's on-demand source startup.
    # Fire-and-forget; the browser's own request a moment later will see
    # the source already coming up.
    url = f"{_MEDIAMTX_HLS}/{name}/index.m3u8"
    try:
        async with httpx.AsyncClient(timeout=2.0, follow_redirects=True) as client:
            await client.get(url)
    except Exception:
        pass


def _kick_warmups(s: CameraSet) -> None:
    for cam in s.cameras:
        if cam.enabled:
            asyncio.create_task(_warm_source(cam.name))


@app.get("/sets/{set_id}")
async def view_set(set_id: str) -> FileResponse:
    s = _resolve_set(set_id)
    _kick_warmups(s)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/sets/{set_id}/{cam}/playback")
async def view_playback(set_id: str, cam: str) -> FileResponse:
    s = _resolve_set(set_id)
    if cam not in {c.name for c in s.cameras}:
        raise HTTPException(status_code=404, detail=f"camera {cam!r} not in set {set_id!r}")
    return FileResponse(STATIC_DIR / "playback.html")


@app.get("/static/{path:path}")
async def static_files(path: str) -> FileResponse:
    target = (STATIC_DIR / path).resolve()
    if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
        raise HTTPException(status_code=404)
    return FileResponse(target)
