"""FastAPI server inside the inference process — loopback-only, fronted
by the DVR's /api/inference/live proxy so OAuth gating stays in one
place.

Runs on 127.0.0.1:9091 in a daemon thread spun up by inference.runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from .live import broadcaster
from .playback import stream_playback

logger = logging.getLogger("belfry.inference.server")


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    # Capture the running loop so the recorder threads can hand
    # detections to subscribers without owning a reference.
    broadcaster.attach_loop(asyncio.get_running_loop())
    logger.info("live broadcaster attached to event loop")
    yield


app = FastAPI(title="belfry-inference", lifespan=_lifespan)
# Detector is constructed in runner.py before uvicorn starts and
# stashed on app.state so the playback handler can use it. db_path
# rides along so /playback can also surface stored motion events
# (the YOLO re-detect would otherwise miss them in past mode).
app.state.detector = None
app.state.db_path = None


_SSE_HEADERS = {
    # Disable any intermediary buffering; SSE depends on lines
    # arriving promptly to feel "live".
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/live")
async def live(cam: str) -> StreamingResponse:
    return StreamingResponse(
        broadcaster.stream(cam),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@app.get("/playback")
async def playback(
    cam: str, start: str, duration: str, request: Request
) -> StreamingResponse:
    """Re-run detection over a past mp4 window. SSE; one message per
    sampled frame (1 fps), ending when the worker reaches the end of
    the mp4. Browser maps results by `ts` and draws on timeupdate."""
    detector = request.app.state.detector
    db_path = request.app.state.db_path
    if detector is None or db_path is None:
        raise HTTPException(
            status_code=503, detail="detector / db_path not yet attached to app state"
        )
    return StreamingResponse(
        stream_playback(cam, start, duration, detector, db_path),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
