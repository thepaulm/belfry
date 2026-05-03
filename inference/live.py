"""On-demand live overlay: stream per-camera detections to browsers via
Server-Sent Events. Nothing is persisted — these are ephemeral pushes
from the recorder thread to whichever subscribers happen to be watching.

Architecture:
- Recorder threads publish one message per detection batch (sync, called
  from the camera-loop thread) via `broadcaster.publish_threadsafe`.
- The broadcaster bridges sync → async by holding a reference to the
  asyncio loop and using `run_coroutine_threadsafe` to put messages
  onto each subscriber's `asyncio.Queue`.
- The SSE endpoint registers a queue per connection, pumps from it, and
  deregisters on disconnect.

Subscriber queues are bounded; if a slow client falls behind, oldest
messages get dropped rather than ballooning memory. At 1 fps × tiny
JSON payloads the queue should never actually back up in practice.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Detection

logger = logging.getLogger("belfry.inference.live")

_QUEUE_MAX = 8


class LiveBroadcaster:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        # cam_name -> set of asyncio.Queue subscribers
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from the asyncio thread before SSE accepts traffic."""
        self._loop = loop

    def subscriber_count(self, cam: str) -> int:
        return len(self._subs.get(cam, ()))

    def publish_threadsafe(self, cam: str, ts: float, detections: list["Detection"]) -> None:
        """Called from the recorder (sync) thread. Cheap when nobody is
        watching: just a dict lookup + length check, no asyncio touched.
        """
        if not self._loop:
            return
        subs = self._subs.get(cam)
        if not subs:
            return
        msg = {
            "ts": ts,
            "boxes": [
                # Compact array form keeps the SSE message small. Order:
                # x1, y1, x2, y2 (normalized 0..1), class, conf.
                [d.bbox[0], d.bbox[1], d.bbox[2], d.bbox[3], d.cls, round(d.conf, 3)]
                for d in detections
            ],
        }
        for q in list(subs):
            try:
                # call_soon_threadsafe + put_nowait is cheaper than
                # run_coroutine_threadsafe and avoids creating a Future
                # we never await.
                self._loop.call_soon_threadsafe(self._enqueue_or_drop, q, msg)
            except RuntimeError:
                # Loop closed mid-shutdown; nothing to do.
                pass

    @staticmethod
    def _enqueue_or_drop(q: asyncio.Queue, msg: dict) -> None:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # Slow client. Drop the oldest message and try again.
            try:
                q.get_nowait()
                q.put_nowait(msg)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def stream(self, cam: str):
        """Async generator: register a subscriber queue, yield SSE-formatted
        bytes for each detection batch, deregister on cancellation.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subs[cam].add(q)
        logger.info(
            "live: subscriber attached to %s (now %d)", cam, self.subscriber_count(cam)
        )
        try:
            # Initial comment lets clients confirm the connection is open
            # without needing to wait for the first detection.
            yield b": ok\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Heartbeat keeps any intermediary (Caddy, nginx) from
                    # closing the SSE connection on idle.
                    yield b": ping\n\n"
                    continue
                yield f"data: {json.dumps(msg)}\n\n".encode()
        except asyncio.CancelledError:
            raise
        finally:
            self._subs[cam].discard(q)
            logger.info(
                "live: subscriber detached from %s (now %d)",
                cam,
                self.subscriber_count(cam),
            )


broadcaster = LiveBroadcaster()
