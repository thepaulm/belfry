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


async def stream_playback(cam: str, start: str, duration: str, detector: Detector):
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
                        msg = {
                            "ts": start_unix + frame_idx / fps,
                            "boxes": [
                                [
                                    d.bbox[0],
                                    d.bbox[1],
                                    d.bbox[2],
                                    d.bbox[3],
                                    d.cls,
                                    round(d.conf, 3),
                                ]
                                for d in dets
                            ],
                        }
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
