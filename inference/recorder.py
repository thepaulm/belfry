"""Always-on event recorder for one camera.

Per-frame loop at `record_fps` (default 1 fps): grab a frame off the
loopback RTSP feed, run the two-model ensemble, run a per-class state
machine that opens/extends/closes "event runs," and persist closed runs
to SQLite plus a thumbnail JPEG snapshot.

The state machine writes one row per *run* (a contiguous burst of
detections of the same class on the same camera, gap-closed by
`cooldown_s`), not one row per frame. A person walking past for 12 s
is one row, not 12 rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2

from .model import Detection, Detector

logger = logging.getLogger("belfry.inference.recorder")

_SCHEMA_FILE = Path(__file__).parent / "schema.sql"


@dataclass
class _Run:
    """In-flight event run for one (camera, class) pair, not yet flushed."""
    cls: str
    ts_start: float
    ts_end: float
    max_conf: float
    peak_bbox: tuple[float, float, float, float]
    peak_frame: object  # numpy ndarray; kept in memory until flush
    sample_count: int = 1


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(_SCHEMA_FILE.read_text())
    return conn


class EventRecorder:
    def __init__(
        self,
        camera_name: str,
        rtsp_url: str,
        detector: Detector,
        db: sqlite3.Connection,
        thumbs_dir: Path,
        record_fps: int,
        cooldown_s: int,
    ) -> None:
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.detector = detector
        self.db = db
        self.thumbs_dir = thumbs_dir
        self.frame_interval_s = 1.0 / max(1, record_fps)
        self.cooldown_s = cooldown_s
        # Active runs keyed by class. Multiple classes can run concurrently
        # on the same camera (a person walking a dog).
        self._runs: dict[str, _Run] = {}

    # ------------------------------------------------------------------
    # main loop

    def run(self, stop_check=lambda: False) -> None:
        """Block until stop_check() returns True or the capture dies."""
        cap = self._open_capture()
        if cap is None:
            return
        try:
            self._loop(cap, stop_check)
        finally:
            cap.release()
            self._flush_all_runs(reason="shutdown")

    def _open_capture(self):
        # FFmpeg backend is more reliable for RTSP than the default on
        # Jetson; if it's not available cv2 will fall back internally.
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            logger.error("could not open %s", self.rtsp_url)
            return None
        # Smallest receive buffer we can ask for so `read()` returns
        # something close to "now" rather than a stale buffered frame.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        logger.info("opened %s for camera %s", self.rtsp_url, self.camera_name)
        return cap

    def _loop(self, cap, stop_check) -> None:
        next_tick = time.monotonic()
        while not stop_check():
            now = time.monotonic()
            if now < next_tick:
                time.sleep(min(0.1, next_tick - now))
                continue
            next_tick = now + self.frame_interval_s

            ok, frame = cap.read()
            if not ok or frame is None:
                logger.warning("read failed on %s; reopening", self.camera_name)
                cap.release()
                time.sleep(2.0)
                new_cap = self._open_capture()
                if new_cap is None:
                    return
                cap = new_cap
                continue

            ts = time.time()
            try:
                dets = self.detector.predict(frame)
            except Exception:
                logger.exception("detector failed on %s", self.camera_name)
                continue

            self._update_runs(ts, dets, frame)
            self._close_stale_runs(ts)

    # ------------------------------------------------------------------
    # state machine

    def _update_runs(self, ts: float, dets: list[Detection], frame) -> None:
        # Group detections by class, keeping only the highest-conf box
        # per class for this frame (we only persist the peak anyway).
        best_by_class: dict[str, Detection] = {}
        for d in dets:
            cur = best_by_class.get(d.cls)
            if cur is None or d.conf > cur.conf:
                best_by_class[d.cls] = d

        for cls, det in best_by_class.items():
            run = self._runs.get(cls)
            if run is None:
                # New run starts.
                self._runs[cls] = _Run(
                    cls=cls,
                    ts_start=ts,
                    ts_end=ts,
                    max_conf=det.conf,
                    peak_bbox=det.bbox,
                    peak_frame=frame.copy(),
                    sample_count=1,
                )
                logger.info(
                    "%s open  %s @ %.2f", self.camera_name, cls, det.conf
                )
            else:
                # Extend existing run.
                run.ts_end = ts
                run.sample_count += 1
                if det.conf > run.max_conf:
                    run.max_conf = det.conf
                    run.peak_bbox = det.bbox
                    run.peak_frame = frame.copy()

    def _close_stale_runs(self, ts: float) -> None:
        stale = [c for c, r in self._runs.items() if (ts - r.ts_end) > self.cooldown_s]
        for cls in stale:
            self._flush_run(self._runs.pop(cls), reason="cooldown")

    def _flush_all_runs(self, reason: str) -> None:
        for cls in list(self._runs.keys()):
            self._flush_run(self._runs.pop(cls), reason=reason)

    def _flush_run(self, run: _Run, reason: str) -> None:
        thumb_rel = self._save_thumb(run)
        self.db.execute(
            """
            INSERT INTO events
              (camera, class, ts_start, ts_end, max_conf, peak_bbox,
               thumb_path, sample_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.camera_name,
                run.cls,
                run.ts_start,
                run.ts_end,
                run.max_conf,
                json.dumps(list(run.peak_bbox)),
                thumb_rel,
                run.sample_count,
            ),
        )
        duration = run.ts_end - run.ts_start
        logger.info(
            "%s close %s  dur=%.1fs  samples=%d  conf=%.2f  thumb=%s  (%s)",
            self.camera_name,
            run.cls,
            duration,
            run.sample_count,
            run.max_conf,
            thumb_rel,
            reason,
        )

    def _save_thumb(self, run: _Run) -> str | None:
        try:
            day = datetime.fromtimestamp(run.ts_start, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            out_dir = self.thumbs_dir / self.camera_name / day
            out_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{run.ts_start:.3f}_{run.cls}.jpg"
            out_path = out_dir / fname
            cv2.imwrite(str(out_path), run.peak_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return str(out_path.relative_to(self.thumbs_dir))
        except Exception:
            logger.exception("thumbnail save failed")
            return None
