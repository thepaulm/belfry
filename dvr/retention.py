from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Inference, Recording, Retention

logger = logging.getLogger("belfry.retention")

# Files modified within this many seconds are not eligible for eviction;
# MediaMTX may still be writing to them. Must be larger than the
# recordSegmentDuration in mediamtx.yml (1h) plus a comfortable buffer.
_PROTECTED_AGE_S = 70 * 60


@dataclass
class _Segment:
    camera: str
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
    # Number of events DB rows + thumbnail files cleaned up on the most
    # recent tick. Cleanup runs every tick, not only above the high
    # watermark, because an event with no surviving footage is a dead
    # link regardless of disk pressure.
    last_events_evicted: int = 0
    last_thumbs_evicted: int = 0
    last_alerts_evicted: int = 0
    last_error: str | None = None


class RetentionLoop:
    def __init__(
        self,
        recording: Recording,
        retention: Retention,
        inference: Inference | None = None,
    ) -> None:
        self.recording = recording
        self.retention = retention
        self.inference = inference
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
        self.status.last_events_evicted = 0
        self.status.last_thumbs_evicted = 0
        self.status.last_alerts_evicted = 0

        if self.status.used_pct < self.retention.evict_high_pct:
            self._sweep_events(segments)
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

        # Re-scan after evictions so the events sweep uses the new
        # oldest-segment-per-camera floor; the eviction above may have
        # just taken the floor up by an hour or more.
        self._sweep_events(list(self._scan_segments(path)))

    def _scan_segments(self, path: Path):
        for cam_entry in os.scandir(path):
            if not cam_entry.is_dir(follow_symlinks=False):
                continue
            cam_name = cam_entry.name
            try:
                for f in os.scandir(cam_entry.path):
                    if not f.is_file(follow_symlinks=False):
                        continue
                    if not f.name.endswith(".mp4"):
                        continue
                    st = f.stat()
                    yield _Segment(
                        camera=cam_name,
                        path=Path(f.path),
                        mtime=st.st_mtime,
                        size=st.st_size,
                    )
            except OSError as e:
                logger.warning("scan error in %s: %s", cam_entry.path, e)

    def _sweep_events(self, segments: list[_Segment]) -> None:
        """Delete events whose mp4 footage has been evicted, plus their
        thumbnail JPEGs. Skipped if no inference config is wired in or
        the events DB hasn't been created yet (running with no inference
        cameras opted in)."""
        if self.inference is None:
            return
        db_path = self.inference.db_path
        if not db_path.exists():
            return

        # Per-camera floor: events with ts_end older than the earliest
        # surviving mp4 segment for that camera have nothing to play
        # back, so they get pruned. A camera with zero segments evicts
        # everything (cutoff = +inf).
        oldest_per_cam: dict[str, float] = {}
        for s in segments:
            cur = oldest_per_cam.get(s.camera)
            if cur is None or s.mtime < cur:
                oldest_per_cam[s.camera] = s.mtime

        rows_evicted = 0
        thumbs_evicted = 0
        try:
            conn = sqlite3.connect(str(db_path), isolation_level=None)
            conn.execute("PRAGMA journal_mode = WAL")
            try:
                cameras_in_db = [
                    row[0]
                    for row in conn.execute("SELECT DISTINCT camera FROM events")
                ]
                for cam in cameras_in_db:
                    cutoff = oldest_per_cam.get(cam, float("inf"))
                    rows = list(
                        conn.execute(
                            "SELECT id, thumb_path FROM events "
                            "WHERE camera = ? AND ts_end < ?",
                            (cam, cutoff),
                        )
                    )
                    if not rows:
                        continue
                    for _id, thumb_rel in rows:
                        if thumb_rel:
                            thumb_path = self.inference.thumbs_dir / thumb_rel
                            try:
                                thumb_path.unlink(missing_ok=True)
                                thumbs_evicted += 1
                            except OSError as e:
                                logger.warning(
                                    "thumb unlink failed: %s: %s", thumb_rel, e
                                )
                            # Sibling clean (no-bbox) frame saved alongside
                            # the boxed thumb by the recorder. Best-effort
                            # cleanup; older events predate this file.
                            clean_path = (
                                thumb_path.parent / f"{thumb_path.stem}.frame.jpg"
                            )
                            try:
                                clean_path.unlink(missing_ok=True)
                            except OSError as e:
                                logger.warning(
                                    "clean frame unlink failed: %s: %s",
                                    clean_path,
                                    e,
                                )
                    conn.execute(
                        "DELETE FROM events WHERE camera = ? AND ts_end < ?",
                        (cam, cutoff),
                    )
                    rows_evicted += len(rows)

                alerts_evicted = self._sweep_alerts(conn, oldest_per_cam)
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning("events sweep failed: %s", e)
            return

        self.status.last_events_evicted = rows_evicted
        self.status.last_thumbs_evicted = thumbs_evicted
        self.status.last_alerts_evicted = alerts_evicted
        if rows_evicted or alerts_evicted:
            logger.info(
                "retention: evicted %d events + %d thumbs + %d alerts (footage gone)",
                rows_evicted,
                thumbs_evicted,
                alerts_evicted,
            )

    def _sweep_alerts(
        self, conn: sqlite3.Connection, oldest_per_cam: dict[str, float]
    ) -> int:
        """Delete alert rows (+ their thumbs) whose footage has been
        evicted, mirroring the events sweep. Same connection so it shares
        the events.db writer slot. No-op if the table doesn't exist yet."""
        try:
            cams = [r[0] for r in conn.execute("SELECT DISTINCT camera FROM alerts")]
        except sqlite3.OperationalError:
            return 0  # alerts table not created yet
        evicted = 0
        for cam in cams:
            cutoff = oldest_per_cam.get(cam, float("inf"))
            rows = list(
                conn.execute(
                    "SELECT id, thumb_path FROM alerts WHERE camera = ? AND ts < ?",
                    (cam, cutoff),
                )
            )
            if not rows:
                continue
            for _id, thumb_rel in rows:
                if thumb_rel:
                    try:
                        (self.inference.thumbs_dir / thumb_rel).unlink(missing_ok=True)
                    except OSError as e:
                        logger.warning("alert thumb unlink failed: %s: %s", thumb_rel, e)
            conn.execute(
                "DELETE FROM alerts WHERE camera = ? AND ts < ?", (cam, cutoff)
            )
            evicted += len(rows)
        return evicted

    def status_dict(self) -> dict:
        return asdict(self.status)
