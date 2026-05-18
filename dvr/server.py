from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import os
import re
import sqlite3
import time
import uuid
from hashlib import sha1
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import auth, training
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


# --- mobile-app auth ---------------------------------------------------
# /auth/exchange takes a Google ID token from the Flutter google_sign_in
# plugin and returns a 30-day server JWT. /auth/verify is the forward_auth
# target Caddy on EC2 will call when a request carries Authorization:
# Bearer — bare 200/401 plus an X-Auth-Email header for downstream logs.
# Neither endpoint gates anything else here; Caddy is the WAN gate, LAN
# stays open by network position. See dvr/auth.py for the trust model.


@app.post("/auth/exchange")
async def auth_exchange(body: dict) -> dict:
    token = body.get("id_token")
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=400, detail="missing id_token")
    try:
        email = auth.verify_google_id_token(token)
    except auth.AuthError as e:
        # Config errors (missing env) and verification failures both land
        # here; 401 vs 503 doesn't help the app, just pass the message
        # through. The app surfaces a generic "sign-in failed" anyway.
        raise HTTPException(status_code=401, detail=str(e))
    jwt_str, exp = auth.mint_jwt(email)
    return {"token": jwt_str, "email": email, "expires_at": exp}


@app.get("/auth/verify")
async def auth_verify(request: Request) -> JSONResponse:
    try:
        bearer = auth.extract_bearer(request.headers.get("authorization"))
        email = auth.verify_jwt(bearer)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return JSONResponse({"email": email}, headers={"X-Auth-Email": email})


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


# --- live overlay SSE proxy --------------------------------------------
# The inference process exposes /live?cam=X on 127.0.0.1:9091. We
# proxy it here so the OAuth gate at Caddy / nginx in front of FastAPI
# is the only public surface; the inference port stays loopback-only.

_INFERENCE_LIVE = "http://127.0.0.1:9091"


_INFERENCE_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _proxy_inference_sse(path: str, params: dict):
    """Stream-pass-through to the inference loopback FastAPI. Long
    timeouts because the SSE is long-lived; the inference server emits
    a heartbeat comment every 15 s so an idle connection still produces
    traffic on the wire."""
    timeout = httpx.Timeout(connect=5.0, read=None, write=None, pool=None)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "GET", f"{_INFERENCE_LIVE}{path}", params=params
            ) as r:
                if r.status_code != 200:
                    body = await r.aread()
                    yield (
                        f"event: error\ndata: upstream {r.status_code}: "
                        f"{body.decode('latin1', errors='replace')}\n\n"
                    ).encode()
                    return
                async for chunk in r.aiter_raw():
                    yield chunk
    except httpx.HTTPError as e:
        yield f"event: error\ndata: inference unreachable: {e}\n\n".encode()


@app.get("/api/inference/live")
async def api_inference_live(cam: str) -> StreamingResponse:
    if cam not in {c.name for c in config.all_cameras}:
        raise HTTPException(status_code=404, detail=f"unknown camera: {cam}")
    return StreamingResponse(
        _proxy_inference_sse("/live", {"cam": cam}),
        media_type="text/event-stream",
        headers=_INFERENCE_SSE_HEADERS,
    )


@app.get("/api/inference/playback")
async def api_inference_playback(
    cam: str, start: str, duration: str
) -> StreamingResponse:
    """Stream box detections over a past mp4 window. Inference runs
    flat-out (1 fps target sample × ~30 ms/frame), so a 5-min window
    is fully processed in ~9 s of GPU. Boxes carry absolute timestamps
    so the browser can sync them to the player's currentTime."""
    if cam not in {c.name for c in config.all_cameras}:
        raise HTTPException(status_code=404, detail=f"unknown camera: {cam}")
    return StreamingResponse(
        _proxy_inference_sse(
            "/playback", {"cam": cam, "start": start, "duration": duration}
        ),
        media_type="text/event-stream",
        headers=_INFERENCE_SSE_HEADERS,
    )


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


# --- training-image capture --------------------------------------------
# Saves JPEG frames grabbed from the playback <video> element into the
# staging area of the fine-tune dataset tree (see dvr/training.py).
# When events.db has detections that overlap the captured timestamp,
# we pre-seed a YOLO-format .txt sidecar so labelImg opens with the
# boxes already drawn — the human then reviews/fixes/promotes.
#
# Negative captures (negative=True) intentionally skip pre-seeding:
# the user has flagged the frame as a hard negative for the named
# class, and we let them add boxes (if any) by hand in labelImg.

_CATEGORY_RE = re.compile(r"^[a-z0-9_-]+$")


@app.post("/api/training/capture")
async def api_training_capture(
    request: Request,
    cam: str,
    class_name: str,
    negative: bool = False,
    ts: float | None = None,
) -> dict:
    if cam not in {c.name for c in config.all_cameras}:
        raise HTTPException(status_code=404, detail=f"unknown camera: {cam}")
    cls = class_name.strip().lower().replace(" ", "_")
    if not _CATEGORY_RE.fullmatch(cls):
        raise HTTPException(
            status_code=400,
            detail=f"invalid class_name: {class_name!r} (allowed: a-z 0-9 _ -)",
        )
    category = f"negative_{cls}" if negative else cls
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty image body")
    if body[:2] != b"\xff\xd8":
        raise HTTPException(status_code=400, detail="expected JPEG body (FFD8 SOI)")
    ts_label = ts if ts is not None else time.time()
    stamp = datetime.datetime.fromtimestamp(ts_label).strftime("%Y%m%dT%H%M%S")

    training.ensure_dataset_yaml()
    class_map, _ = training.load_class_map()

    target_dir = training.STAGING_DIR / category
    target_dir.mkdir(parents=True, exist_ok=True)

    stem = f"{cam}_{stamp}_{uuid.uuid4().hex[:8]}"
    img_path = target_dir / f"{stem}.jpg"
    img_path.write_bytes(body)

    seeded_lines: list[str] = []
    if not negative:
        seeded_lines = training.seed_labels_for_capture(
            config.inference.db_path, cam, ts_label, class_map,
        )
        if seeded_lines:
            (target_dir / f"{stem}.txt").write_text("\n".join(seeded_lines) + "\n")

    return {
        "path": str(img_path),
        "category": category,
        "filename": img_path.name,
        "bytes": len(body),
        "seeded_boxes": len(seeded_lines),
    }


# --- training labeler ---------------------------------------------------
# Backs the in-browser bbox labeler at /training. Operates on the same
# staging tree the capture endpoint writes to. All endpoints are
# path-safe: category and filename are constrained-regex-validated, and
# the resolved path is checked to fall under STAGING_DIR.

_LABEL_FNAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.jpg$")


def _resolve_staging(category: str, filename: str) -> Path:
    if not _CATEGORY_RE.fullmatch(category):
        raise HTTPException(status_code=400, detail=f"bad category: {category!r}")
    if not _LABEL_FNAME_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail=f"bad filename: {filename!r}")
    target = (training.STAGING_DIR / category / filename).resolve()
    if training.STAGING_DIR.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail="path escapes staging")
    return target


def _resolve_promoted(filename: str) -> tuple[Path, Path]:
    """(images_path, labels_path) for a promoted file. The label is the
    same stem as the jpg, just under labels/ with a .txt extension."""
    if not _LABEL_FNAME_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail=f"bad filename: {filename!r}")
    img = (training.IMAGES_DIR / filename).resolve()
    txt = (training.LABELS_DIR / (Path(filename).stem + ".txt")).resolve()
    if training.IMAGES_DIR.resolve() not in img.parents:
        raise HTTPException(status_code=400, detail="path escapes images")
    if training.LABELS_DIR.resolve() not in txt.parents:
        raise HTTPException(status_code=400, detail="path escapes labels")
    return img, txt


_FNAME_TS_RE = re.compile(r"^(?P<cam>[A-Za-z0-9-]+)_(?P<stamp>\d{8}T\d{6})_")


def _parse_capture_filename(name: str) -> tuple[str | None, float | None]:
    m = _FNAME_TS_RE.match(name)
    if not m:
        return None, None
    cam = m.group("cam")
    stamp = m.group("stamp")
    try:
        ts = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%S").timestamp()
    except ValueError:
        ts = None
    return cam, ts


@app.get("/api/training/staging")
async def api_training_staging() -> dict:
    """List training images (both staging and promoted) and the class
    map.

    Each item has a `location` field — `"staging"` (workbench, in
    staging/<category>/) or `"promoted"` (training set, in images/+
    labels/) — so the frontend can hit the right CRUD route.
    """
    training.ensure_dataset_yaml()
    _, id_to_name = training.load_class_map()
    items: list[dict] = []

    # Staging — workbench. One subdir per class hint (positive or negative).
    if training.STAGING_DIR.is_dir():
        for cat_dir in sorted(training.STAGING_DIR.iterdir()):
            if not cat_dir.is_dir():
                continue
            if not _CATEGORY_RE.fullmatch(cat_dir.name):
                continue
            for img in sorted(cat_dir.glob("*.jpg")):
                cam, ts = _parse_capture_filename(img.name)
                items.append({
                    "location": "staging",
                    "category": cat_dir.name,
                    "filename": img.name,
                    "has_label": img.with_suffix(".txt").exists(),
                    "cam": cam,
                    "ts": ts,
                })

    # Promoted — in the training set. Flat tree, no class folders.
    if training.IMAGES_DIR.is_dir():
        for img in sorted(training.IMAGES_DIR.glob("*.jpg")):
            cam, ts = _parse_capture_filename(img.name)
            label_path = training.LABELS_DIR / f"{img.stem}.txt"
            items.append({
                "location": "promoted",
                "category": None,
                "filename": img.name,
                "has_label": label_path.is_file(),
                "cam": cam,
                "ts": ts,
            })

    # Frontend needs {id, name} pairs because class ids are sparse
    # (COCO-aligned). A flat list of names would lose the ids.
    return {
        "classes": [{"id": cid, "name": name} for cid, name in id_to_name],
        "images": items,
    }


@app.get("/api/training/image/{category}/{filename}")
async def api_training_image(category: str, filename: str) -> FileResponse:
    target = _resolve_staging(category, filename)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(
        target,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/training/label/{category}/{filename}")
async def api_training_label_get(category: str, filename: str) -> Response:
    target = _resolve_staging(category, filename)
    txt = target.with_suffix(".txt")
    if not txt.is_file():
        # 200 with empty body — the labeler treats this as "no boxes
        # yet"; 404 would clutter the console for the common case.
        return Response(content="", media_type="text/plain")
    return Response(content=txt.read_bytes(), media_type="text/plain")


@app.put("/api/training/label/{category}/{filename}")
async def api_training_label_put(
    category: str, filename: str, request: Request,
) -> dict:
    target = _resolve_staging(category, filename)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    body = (await request.body()).decode("utf-8", errors="replace")
    # Light validation: every non-empty line must be `<int> <f> <f> <f> <f>`.
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split()
        if len(parts) != 5:
            raise HTTPException(status_code=400, detail=f"bad label line: {ln!r}")
        try:
            int(parts[0])
            for p in parts[1:]:
                float(p)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad label line: {ln!r}")
    txt = target.with_suffix(".txt")
    stripped = body.strip()
    if not stripped:
        # Empty payload = explicit "this image has zero boxes" — we
        # still create an empty .txt so promote treats it as labeled.
        txt.write_text("")
    else:
        txt.write_text(stripped + "\n")
    return {"saved": True, "bytes": len(body)}


@app.delete("/api/training/label/{category}/{filename}")
async def api_training_label_delete(category: str, filename: str) -> dict:
    target = _resolve_staging(category, filename)
    txt = target.with_suffix(".txt")
    if txt.exists():
        txt.unlink()
    return {"deleted": True}


@app.post("/api/training/promote/{category}/{filename}")
async def api_training_promote(category: str, filename: str) -> dict:
    """Move staging/<cat>/<stem>.{jpg,txt} → images/+labels/. Requires
    the .txt to exist (caller saves first). If missing, the caller
    should PUT an empty body to mark the image as a zero-boxes
    negative before promoting."""
    target = _resolve_staging(category, filename)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    txt = target.with_suffix(".txt")
    if not txt.is_file():
        raise HTTPException(status_code=400, detail="no labels saved yet")
    img_dest = training.IMAGES_DIR / target.name
    txt_dest = training.LABELS_DIR / txt.name
    if img_dest.exists() or txt_dest.exists():
        raise HTTPException(status_code=409, detail="destination already exists")
    training.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    training.LABELS_DIR.mkdir(parents=True, exist_ok=True)
    target.rename(img_dest)
    txt.rename(txt_dest)
    return {"promoted": True, "image": str(img_dest), "label": str(txt_dest)}


@app.delete("/api/training/staging/{category}/{filename}")
async def api_training_staging_delete(category: str, filename: str) -> dict:
    """Trash a capture entirely: removes image and any .txt sibling."""
    target = _resolve_staging(category, filename)
    txt = target.with_suffix(".txt")
    deleted = []
    if target.is_file():
        target.unlink()
        deleted.append("jpg")
    if txt.is_file():
        txt.unlink()
        deleted.append("txt")
    if not deleted:
        raise HTTPException(status_code=404, detail="nothing to delete")
    return {"deleted": deleted}


# Promoted-image CRUD — same shape as the staging routes but rooted at
# images/+labels/. The labeler uses these when a list item's
# location is "promoted". No promote endpoint here (it's already
# promoted); a trash removes the pair from the training set.


@app.get("/api/training/promoted/image/{filename}")
async def api_training_promoted_image(filename: str) -> FileResponse:
    img, _ = _resolve_promoted(filename)
    if not img.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(
        img,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/api/training/promoted/label/{filename}")
async def api_training_promoted_label_get(filename: str) -> Response:
    _, txt = _resolve_promoted(filename)
    if not txt.is_file():
        return Response(content="", media_type="text/plain")
    return Response(content=txt.read_bytes(), media_type="text/plain")


@app.put("/api/training/promoted/label/{filename}")
async def api_training_promoted_label_put(filename: str, request: Request) -> dict:
    img, txt = _resolve_promoted(filename)
    if not img.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    body = (await request.body()).decode("utf-8", errors="replace")
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split()
        if len(parts) != 5:
            raise HTTPException(status_code=400, detail=f"bad label line: {ln!r}")
        try:
            int(parts[0])
            for p in parts[1:]:
                float(p)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"bad label line: {ln!r}")
    stripped = body.strip()
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text("" if not stripped else stripped + "\n")
    return {"saved": True, "bytes": len(body)}


@app.delete("/api/training/promoted/{filename}")
async def api_training_promoted_delete(filename: str) -> dict:
    img, txt = _resolve_promoted(filename)
    deleted = []
    if img.is_file():
        img.unlink()
        deleted.append("jpg")
    if txt.is_file():
        txt.unlink()
        deleted.append("txt")
    if not deleted:
        raise HTTPException(status_code=404, detail="nothing to delete")
    return {"deleted": deleted}


# --- stage event -------------------------------------------------------
# One-click "send this event into the labeler" from /events cards.
# Pulls a clean (un-boxed) frame from the recorded mp4 via ffmpeg
# against MediaMTX /get, drops it in staging/<class>/, and pre-seeds
# the .txt with the event's stored peak_bbox. We can't use the
# events.db thumbnail directly because that has the bbox baked into
# the JPEG (drawn before encoding in inference/recorder.py), which
# would teach the fine-tune to predict neon rectangles.

_FFMPEG_BIN = "/usr/bin/ffmpeg"


@app.post("/api/training/stage-event/{event_id}")
async def api_training_stage_event(event_id: int) -> dict:
    conn = _open_events_db()
    if conn is None:
        raise HTTPException(status_code=404, detail="no events db")
    try:
        row = conn.execute(
            "SELECT camera, class, ts_start, peak_bbox FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"event {event_id} not found")

    cam = row["camera"]
    cls = row["class"]
    ts = row["ts_start"]

    training.ensure_dataset_yaml()
    class_map, _ = training.load_class_map()
    if cls not in class_map:
        # Legacy aggregate classes (animal/vehicle) or motion — no
        # fine-tune target. The user can add the class to dataset.yaml.
        raise HTTPException(
            status_code=400,
            detail=f"event class {cls!r} not in dataset.yaml; edit it to add the class",
        )

    # MediaMTX expects the trailing `Z` form, not `+00:00`; Python's
    # default isoformat() produces the offset style which /get rejects
    # with a 400.
    start_iso = datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    mp4_url = (
        f"{_MEDIAMTX_PLAYBACK}/get?path={cam}"
        f"&start={start_iso}&duration=1s&format=mp4"
    )

    target_dir = training.STAGING_DIR / cls
    target_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.datetime.fromtimestamp(ts).strftime("%Y%m%dT%H%M%S")
    stem = f"{cam}_{stamp}_{uuid.uuid4().hex[:8]}"
    img_path = target_dir / f"{stem}.jpg"

    proc = await asyncio.create_subprocess_exec(
        _FFMPEG_BIN,
        "-y",
        "-loglevel", "error",
        "-i", mp4_url,
        "-frames:v", "1",
        "-q:v", "2",
        str(img_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(status_code=504, detail="ffmpeg timed out")
    if proc.returncode != 0 or not img_path.is_file():
        # ffmpeg can write a 0-byte file then exit non-zero; clean up.
        if img_path.exists():
            img_path.unlink()
        msg = stderr.decode("latin1", errors="replace")[-200:]
        raise HTTPException(status_code=502, detail=f"ffmpeg failed: {msg}")

    seeded = training.seed_labels_for_capture(
        config.inference.db_path, cam, ts, class_map,
    )
    if seeded:
        (target_dir / f"{stem}.txt").write_text("\n".join(seeded) + "\n")

    return {
        "staged": True,
        "category": cls,
        "filename": img_path.name,
        "seeded_boxes": len(seeded),
    }


@app.get("/training")
async def view_training() -> FileResponse:
    return FileResponse(STATIC_DIR / "training.html")


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
