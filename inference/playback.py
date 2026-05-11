"""On-demand box detection over a past playback window.

Pulls the same mp4 the DVR proxies for video — directly from
MediaMTX's loopback `/get` — to a tmp file, then decodes in a worker
thread with `cv2.VideoCapture` at 1 fps and runs the same `Detector`
the live recorder uses. Boxes are emitted as SSE `data: {ts, boxes}`
messages where `ts` is the absolute unix-epoch timestamp of the
sampled frame. The browser side maps these by `ts` and draws on the
video player's `timeupdate` event.

The async generator keeps the worker thread cooperative — it polls
a stop_flag on every frame and the async caller flips that on
disconnect/cancel. Tmp file is cleaned up in the `finally` block
regardless of how the request ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import httpx

from .model import Detector

logger = logging.getLogger("belfry.inference.playback")

_MEDIAMTX_PLAYBACK = "http://127.0.0.1:9996"


def _parse_iso_to_unix(s: str) -> float:
    # MediaMTX accepts ISO-8601 with or without an explicit Z suffix.
    # datetime.fromisoformat() handles "+00:00" but not the bare "Z"
    # tail before Python 3.11; replace to be safe across interpreters.
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _parse_duration_to_seconds(s: str) -> int:
    """Browser sends '<int>s' (e.g. '300s'). Accept that, plain int, or
    bare ints in case the caller forgot the unit. Caller is internal-only."""
    m = re.match(r"^(\d+)\s*s?$", s.strip())
    if not m:
        return 300
    return int(m.group(1))


def _load_motion_events(
    db_path: Path, cam: str, window_start: float, window_end: float,
) -> list[tuple[float, float, list[float], float]]:
    """Motion events overlapping the playback window.

    Returns ``[(ts_start, ts_end, [x1, y1, x2, y2], max_conf), ...]``.
    Used to inject the stored peak_bbox as a static box into the
    re-detection SSE stream — the playback worker only re-runs YOLO,
    which doesn't know about motion. Without this, scrubbing past a
    motion event with "Show labels" on shows nothing.
    """
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            "SELECT ts_start, ts_end, peak_bbox, max_conf FROM events "
            "WHERE camera = ? AND class = 'motion' "
            "AND ts_end >= ? AND ts_start <= ?",
            (cam, window_start, window_end),
        ).fetchall()
    finally:
        conn.close()
    out: list[tuple[float, float, list[float], float]] = []
    for ts_start, ts_end, peak_bbox_json, max_conf in rows:
        try:
            bbox = json.loads(peak_bbox_json)
        except (TypeError, ValueError):
            continue
        if isinstance(bbox, list) and len(bbox) == 4:
            out.append((float(ts_start), float(ts_end), bbox, float(max_conf)))
    return out


async def stream_playback(
    cam: str, start: str, duration: str, detector: Detector, db_path: Path,
):
    """Async generator yielding SSE bytes for every sampled frame in
    the requested playback window.

    Caller is responsible for the FastAPI StreamingResponse wrapping;
    this just emits properly-framed `data:`/`event:` lines.
    """
    tmp_dir = Path(tempfile.gettempdir())
    tmp_path = tmp_dir / f"belfry-playback-{uuid.uuid4().hex}.mp4"

    try:
        # --- fetch the mp4 from MediaMTX loopback ---------------------
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "GET",
                    f"{_MEDIAMTX_PLAYBACK}/get",
                    params={
                        "path": cam,
                        "start": start,
                        "duration": duration,
                        "format": "mp4",
                    },
                ) as r:
                    if r.status_code != 200:
                        body = await r.aread()
                        msg = body.decode("latin1", errors="replace")
                        yield f"event: error\ndata: upstream {r.status_code}: {msg}\n\n".encode()
                        return
                    with tmp_path.open("wb") as f:
                        async for chunk in r.aiter_bytes(64 * 1024):
                            f.write(chunk)
        except httpx.HTTPError as e:
            yield f"event: error\ndata: mediamtx unreachable: {e}\n\n".encode()
            return

        # Initial heartbeat — lets the client know the connection is
        # live before we spend time on inference.
        yield b": ok\n\n"

        # --- decode + detect in a worker thread -----------------------
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        stop_flag = threading.Event()
        start_unix = _parse_iso_to_unix(start)
        duration_s = _parse_duration_to_seconds(duration)
        # Stored motion events whose [ts_start, ts_end] overlaps the
        # window. We re-emit their peak_bbox at every YOLO-sampled
        # frame inside that range so "Show labels" surfaces motion in
        # past mode the same way it does live.
        motion_events = _load_motion_events(
            db_path, cam, start_unix, start_unix + duration_s,
        )

        def worker() -> None:
            try:
                cap = cv2.VideoCapture(str(tmp_path))
                if not cap.isOpened():
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        ("error", f"could not open mp4 at {tmp_path}"),
                    )
                    return
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                # Aim for 1 frame/sec sampled inference. Fall back to
                # every-frame if fps is unexpectedly low (avoids div-0).
                sample_every = max(1, int(round(fps)))
                frame_idx = 0
                while not stop_flag.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        break
                    if frame_idx % sample_every == 0:
                        try:
                            dets = detector.predict(frame)
                        except Exception as e:
                            logger.exception("playback predict failed")
                            loop.call_soon_threadsafe(
                                queue.put_nowait, ("error", str(e))
                            )
                            return
                        ts = start_unix + frame_idx / fps
                        boxes = [
                            [
                                d.bbox[0],
                                d.bbox[1],
                                d.bbox[2],
                                d.bbox[3],
                                d.cls,
                                round(d.conf, 3),
                            ]
                            for d in dets
                        ]
                        for ts_s, ts_e, bbox, max_conf in motion_events:
                            if ts_s <= ts <= ts_e:
                                boxes.append([
                                    bbox[0], bbox[1], bbox[2], bbox[3],
                                    "motion", round(max_conf, 3),
                                ])
                        msg = {"ts": ts, "boxes": boxes}
                        loop.call_soon_threadsafe(queue.put_nowait, ("data", msg))
                    frame_idx += 1
                cap.release()
                loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
            except Exception as e:
                logger.exception("playback worker crashed")
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))
                except RuntimeError:
                    pass

        thread = threading.Thread(
            target=worker, daemon=True, name=f"playback-{cam}-{start}"
        )
        thread.start()

        try:
            while True:
                kind, payload = await queue.get()
                if kind == "error":
                    yield f"event: error\ndata: {payload}\n\n".encode()
                    return
                if kind == "done":
                    return
                yield f"data: {json.dumps(payload)}\n\n".encode()
        except asyncio.CancelledError:
            # Browser disconnected. Tell the worker to bail; tmp cleanup
            # still runs in the outer `finally`.
            stop_flag.set()
            raise
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("could not unlink %s: %s", tmp_path, e)
