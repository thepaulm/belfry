from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Recording, Retention

logger = logging.getLogger("belfry.retention")

# Files modified within this many seconds are not eligible for eviction;
# MediaMTX may still be writing to them. Must be larger than the
# recordSegmentDuration in mediamtx.yml (1h) plus a comfortable buffer.
_PROTECTED_AGE_S = 70 * 60


@dataclass
class _Segment:
    path: Path
    mtime: float
    size: int


@dataclass
class RetentionStatus:
    last_run_at: float | None = None
    disk_total_bytes: int = 0
    disk_used_bytes: int = 0
    disk_free_bytes: int = 0
    used_pct: float = 0.0
    segment_count: int = 0
    oldest_segment_at: float | None = None
    last_evicted_count: int = 0
    last_evicted_bytes: int = 0
    last_error: str | None = None


class RetentionLoop:
    def __init__(self, recording: Recording, retention: Retention) -> None:
        self.recording = recording
        self.retention = retention
        self.status = RetentionStatus()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="retention")
            logger.info(
                "retention loop started: path=%s high=%d%% low=%d%% interval=%ds",
                self.recording.path,
                self.retention.evict_high_pct,
                self.retention.evict_low_pct,
                self.retention.scan_interval_s,
            )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._tick)
            except Exception as e:
                self.status.last_error = f"{type(e).__name__}: {e}"
                logger.exception("retention tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.retention.scan_interval_s
                )
            except asyncio.TimeoutError:
                pass

    def _tick(self) -> None:
        path = self.recording.path
        if not path.exists():
            self.status.last_error = f"recording path missing: {path}"
            return

        usage = shutil.disk_usage(path)
        segments = list(self._scan_segments(path))

        now = time.time()
        self.status.last_run_at = now
        self.status.disk_total_bytes = usage.total
        self.status.disk_used_bytes = usage.used
        self.status.disk_free_bytes = usage.free
        self.status.used_pct = usage.used / usage.total * 100
        self.status.segment_count = len(segments)
        self.status.oldest_segment_at = min(
            (s.mtime for s in segments), default=None
        )
        self.status.last_error = None
        self.status.last_evicted_count = 0
        self.status.last_evicted_bytes = 0

        if self.status.used_pct < self.retention.evict_high_pct:
            return

        # Above high watermark — evict oldest closed segments until we drop
        # below the low watermark.
        candidates = [
            s for s in segments
            if (now - s.mtime) > _PROTECTED_AGE_S
        ]
        candidates.sort(key=lambda s: s.mtime)

        evicted = 0
        evicted_bytes = 0
        for seg in candidates:
            current = shutil.disk_usage(path)
            if (current.used / current.total * 100) < self.retention.evict_low_pct:
                break
            try:
                seg.path.unlink()
                evicted += 1
                evicted_bytes += seg.size
            except OSError as e:
                logger.warning("could not evict %s: %s", seg.path, e)

        self.status.last_evicted_count = evicted
        self.status.last_evicted_bytes = evicted_bytes
        final = shutil.disk_usage(path)
        self.status.disk_used_bytes = final.used
        self.status.disk_free_bytes = final.free
        self.status.used_pct = final.used / final.total * 100

        if evicted:
            logger.info(
                "retention: evicted %d files (%.2f GB); now %.1f%% used",
                evicted,
                evicted_bytes / 1e9,
                self.status.used_pct,
            )
        else:
            # Above high watermark but nothing eligible — likely all segments
            # are within the protected age. Operator should investigate.
            logger.warning(
                "retention: above high watermark (%.1f%%) but no eligible "
                "segments to evict (count=%d, all within %ds protected age)",
                self.status.used_pct,
                len(segments),
                _PROTECTED_AGE_S,
            )

    def _scan_segments(self, path: Path):
        for cam_entry in os.scandir(path):
            if not cam_entry.is_dir(follow_symlinks=False):
                continue
            try:
                for f in os.scandir(cam_entry.path):
                    if not f.is_file(follow_symlinks=False):
                        continue
                    if not f.name.endswith(".mp4"):
                        continue
                    st = f.stat()
                    yield _Segment(
                        path=Path(f.path),
                        mtime=st.st_mtime,
                        size=st.st_size,
                    )
            except OSError as e:
                logger.warning("scan error in %s: %s", cam_entry.path, e)

    def status_dict(self) -> dict:
        return asdict(self.status)
