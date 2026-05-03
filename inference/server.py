"""FastAPI server inside the inference process — loopback-only, fronted
by the DVR's /api/inference/live proxy so OAuth gating stays in one
place.

Runs on 127.0.0.1:9091 in a daemon thread spun up by inference.runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from .live import broadcaster

logger = logging.getLogger("belfry.inference.server")


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    # Capture the running loop so the recorder threads can hand
    # detections to subscribers without owning a reference.
    broadcaster.attach_loop(asyncio.get_running_loop())
    logger.info("live broadcaster attached to event loop")
    yield


app = FastAPI(title="belfry-inference", lifespan=_lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/live")
async def live(cam: str) -> StreamingResponse:
    return StreamingResponse(
        broadcaster.stream(cam),
        media_type="text/event-stream",
        headers={
            # Disable any intermediary buffering; SSE depends on lines
            # arriving promptly to feel "live".
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
