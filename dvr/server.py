from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Camera, CameraSet, load_config
from .health import probe_all
from .retention import RetentionLoop

# MediaMTX HLS lives on this box; warmup hits it directly, bypassing nginx.
_MEDIAMTX_HLS = "http://127.0.0.1:8888"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

config = load_config(PROJECT_ROOT / "cameras.yaml")
USERNAME = os.environ.get("DVR_USERNAME", "admin")
PASSWORD = os.environ["DVR_PASSWORD"]  # required; fail fast if absent

retention_loop = RetentionLoop(config.recording, config.retention)


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    retention_loop.start()
    try:
        yield
    finally:
        await retention_loop.stop()


app = FastAPI(title="homecam-dvr", lifespan=_lifespan)
_basic = HTTPBasic()


def require_auth(creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    user_ok = secrets.compare_digest(creds.username.encode(), USERNAME.encode())
    pass_ok = secrets.compare_digest(creds.password.encode(), PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


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
async def api_sets(_: str = Depends(require_auth)) -> list[dict]:
    return [
        {"id": s.id, "label": s.label, "camera_count": len(s.cameras)}
        for s in config.sets
    ]


@app.get("/api/sets/{set_id}/cameras")
async def api_set_cameras(set_id: str, _: str = Depends(require_auth)) -> list[dict]:
    s = _resolve_set(set_id)
    return [_camera_payload(c) for c in s.cameras]


@app.get("/api/sets/{set_id}/health")
async def api_set_health(set_id: str, _: str = Depends(require_auth)) -> list[dict]:
    s = _resolve_set(set_id)
    probes = await probe_all(s.cameras)
    return [{"name": p.name, "status": p.status, "checked_at": p.checked_at} for p in probes]


@app.get("/api/retention/status")
async def api_retention_status(_: str = Depends(require_auth)) -> dict:
    return retention_loop.status_dict()


@app.get("/")
async def index(_: str = Depends(require_auth)):
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
async def view_set(set_id: str, _: str = Depends(require_auth)) -> FileResponse:
    s = _resolve_set(set_id)
    _kick_warmups(s)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/{path:path}")
async def static_files(path: str, _: str = Depends(require_auth)) -> FileResponse:
    target = (STATIC_DIR / path).resolve()
    if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
        raise HTTPException(status_code=404)
    return FileResponse(target)
