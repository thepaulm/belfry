from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import uuid
from hashlib import sha1
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
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


# --- inference events --------------------------------------------------
# Read-only views over the events DB written by inference/recorder.py.
# Schema: events(id, camera, class, ts_start, ts_end, max_conf,
# peak_bbox JSON, thumb_path, sample_count). The DB is on the same
# disk as the recordings; queries run against a per-request sqlite3
# connection (cheap on a local file in WAL mode). The block is no-op
# if the events DB hasn't been created yet (no inference runs ever).

_CAMERA_TO_SET: dict[str, str] = {
    cam.name: s.id for s in config.sets for cam in s.cameras
}


def _open_events_db() -> sqlite3.Connection | None:
    p = config.inference.db_path
    if not p.exists():
        return None
    # Read-only URI mode so a stray write would fail loudly. Multi-thread
    # safe = False here is OK because the connection is request-local.
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _event_row_to_dict(r: sqlite3.Row) -> dict:
    try:
        peak_bbox = json.loads(r["peak_bbox"])
    except (json.JSONDecodeError, TypeError):
        peak_bbox = None
    return {
        "id": r["id"],
        "camera": r["camera"],
        "set_id": _CAMERA_TO_SET.get(r["camera"]),
        "class": r["class"],
        "ts_start": r["ts_start"],
        "ts_end": r["ts_end"],
        "duration_s": round(r["ts_end"] - r["ts_start"], 2),
        "max_conf": round(r["max_conf"], 3),
        "peak_bbox": peak_bbox,
        "sample_count": r["sample_count"],
        "thumb_url": f"/api/events/thumb/{r['id']}" if r["thumb_path"] else None,
    }


def _query_events(
    cam: str | None = None,
    cls: str | None = None,
    since: float | None = None,
    until: float | None = None,
    before_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """SQL-builder shared by /api/events and /api/events/recent.
    Pure-Python defaults so it can be called directly from other
    handlers without dragging in Query() sentinel values."""
    conn = _open_events_db()
    if conn is None:
        return []
    where = []
    args: list = []
    if cam is not None:
        where.append("camera = ?")
        args.append(cam)
    if cls is not None:
        where.append("class = ?")
        args.append(cls)
    if since is not None:
        where.append("ts_end >= ?")
        args.append(since)
    if until is not None:
        where.append("ts_start <= ?")
        args.append(until)
    if before_id is not None:
        where.append("id < ?")
        args.append(before_id)
    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [_event_row_to_dict(r) for r in rows]


@app.get("/api/events")
async def api_events(
    cam: str | None = None,
    cls: str | None = Query(default=None, alias="class"),
    since: float | None = None,
    until: float | None = None,
    before_id: int | None = Query(default=None, description="Cursor: return events with id < before_id"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """Filtered events list, newest first.

    Cursor-paginate by passing the smallest `id` from the previous page
    as `before_id`. Combine with `since`/`until` for time windows;
    omitted bounds are open-ended.
    """
    return _query_events(
        cam=cam, cls=cls, since=since, until=until,
        before_id=before_id, limit=limit,
    )


@app.get("/api/events/recent")
async def api_events_recent(
    cam: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[dict]:
    """Most-recent events; thin convenience wrapper used by live tile
    badges in slice 5."""
    return _query_events(cam=cam, limit=limit)


@app.get("/api/events/neighbors")
async def api_events_neighbors(cam: str, ts: float) -> dict:
    """{prev, next} event timestamps around a cursor — backs the prev/
    next-event buttons coming in slice 4."""
    conn = _open_events_db()
    if conn is None:
        return {"prev": None, "next": None}
    try:
        prev = conn.execute(
            "SELECT id, ts_start FROM events WHERE camera = ? AND ts_start < ? "
            "ORDER BY ts_start DESC LIMIT 1",
            (cam, ts),
        ).fetchone()
        nxt = conn.execute(
            "SELECT id, ts_start FROM events WHERE camera = ? AND ts_start > ? "
            "ORDER BY ts_start ASC LIMIT 1",
            (cam, ts),
        ).fetchone()
    finally:
        conn.close()
    return {
        "prev": {"id": prev["id"], "ts_start": prev["ts_start"]} if prev else None,
        "next": {"id": nxt["id"], "ts_start": nxt["ts_start"]} if nxt else None,
    }


@app.get("/api/events/thumb/{event_id}")
async def api_events_thumb(event_id: int) -> FileResponse:
    """Serve the peak-conf JPEG for one event. Long cache because
    thumbnails are immutable (the recorder never rewrites a row)."""
    conn = _open_events_db()
    if conn is None:
        raise HTTPException(status_code=404, detail="events DB not present")
    try:
        row = conn.execute(
            "SELECT thumb_path FROM events WHERE id = ?", (event_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["thumb_path"]:
        raise HTTPException(status_code=404, detail=f"no thumbnail for event {event_id}")
    abs_path = (config.inference.thumbs_dir / row["thumb_path"]).resolve()
    # Defense in depth: refuse if the resolved path escapes the thumbs root.
    if config.inference.thumbs_dir.resolve() not in abs_path.parents:
        raise HTTPException(status_code=404)
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail=f"thumbnail file gone: {row['thumb_path']}")
    return FileResponse(
        abs_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


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


@app.get("/events")
async def view_events() -> FileResponse:
    """Cross-camera events browse page (served regardless of whether
    the events DB exists yet — page handles the empty state)."""
    return FileResponse(STATIC_DIR / "events.html")


@app.get("/static/{path:path}")
async def static_files(path: str) -> FileResponse:
    target = (STATIC_DIR / path).resolve()
    if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
        raise HTTPException(status_code=404)
    return FileResponse(target)
