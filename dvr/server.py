from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import Camera, load_config
from .health import probe_all

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

config = load_config(PROJECT_ROOT / "cameras.yaml")
USERNAME = os.environ.get("DVR_USERNAME", "admin")
PASSWORD = os.environ["DVR_PASSWORD"]  # required; fail fast if absent

app = FastAPI(title="homecam-dvr")
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


@app.get("/")
async def index(_: str = Depends(require_auth)):
    if not config.sets:
        raise HTTPException(status_code=500, detail="no camera sets configured")
    return RedirectResponse(url=f"/sets/{config.sets[0].id}", status_code=302)


@app.get("/sets/{set_id}")
async def view_set(set_id: str, _: str = Depends(require_auth)) -> FileResponse:
    _resolve_set(set_id)  # 404 if unknown
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/{path:path}")
async def static_files(path: str, _: str = Depends(require_auth)) -> FileResponse:
    target = (STATIC_DIR / path).resolve()
    if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
        raise HTTPException(status_code=404)
    return FileResponse(target)
