"""One-shot backfill of events.db from on-disk mp4 segments.

Useful for testing detector changes (re-run YOLO11l over the last 90
minutes of cam12 to compare against what the live recorder caught) and
for filling gaps left when the live recorder was offline or its RTSP
stream was dropping frames.

Reads the same `Detector` and the same coalescing rules as the live
recorder, just driven by sampling decoded mp4 frames at `record_fps`
instead of an RTSP feed. Frame timestamps come from the mp4's filename
(MediaMTX writes `YYYY-MM-DD_HH-MM-SS-uuuuuu.mp4` in UTC) plus the
in-segment offset, so the resulting events line up with whatever the
viewer requests by absolute time.

Loads its own Detector instance — that means a second copy of the TRT
engine on the GPU while the backfill is running. On Jetson that's
~50 MiB; the live service still works while this runs.

Usage:
    python -m inference.backfill --cam cam12 --since 90m
    python -m inference.backfill --cam cam12 --since 2026-05-03T20:00:00Z --replace
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from dvr.config import load_config
from .model import Detection, Detector
from .recorder import init_db

logger = logging.getLogger("belfry.inference.backfill")


@dataclass
class _Run:
    cls: str
    ts_start: float
    ts_end: float
    max_conf: float
    peak_bbox: tuple[float, float, float, float]
    peak_frame: Any  # numpy ndarray; held until the run closes
    sample_count: int = 1


# MediaMTX recordPath: %Y-%m-%d_%H-%M-%S-%f.mp4. %f is microseconds.
_FILENAME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})-(\d{6})\.mp4$"
)


def _segment_start_unix(p: Path) -> float | None:
    m = _FILENAME_RE.match(p.name)
    if not m:
        return None
    y, mo, d, h, mi, s, us = map(int, m.groups())
    return datetime(y, mo, d, h, mi, s, us, tzinfo=timezone.utc).timestamp()


def _parse_time(s: str) -> float:
    """Accept ISO8601 (with or without `Z`) or relative ("90m", "2h", "30s")."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    m = re.match(r"^(\d+)\s*([smh])$", s.strip())
    if not m:
        raise ValueError(f"can't parse {s!r} as ISO8601 or relative duration")
    n, unit = int(m.group(1)), m.group(2)
    return time.time() - n * {"s": 1, "m": 60, "h": 3600}[unit]


def _list_segments(
    cam_dir: Path, since: float, until: float
) -> list[tuple[Path, float]]:
    """Return [(path, segment_start_unix), ...] sorted by start time, for
    segments that *might* overlap [since, until]. We fudge by 1h because
    we don't read each segment's actual duration here — the per-frame
    `since <= ts <= until` check inside the main loop tightens it up."""
    out: list[tuple[Path, float]] = []
    for p in sorted(cam_dir.glob("*.mp4")):
        start = _segment_start_unix(p)
        if start is None:
            continue
        if start + 3600 < since or start > until:
            continue
        out.append((p, start))
    return out


def _save_thumb(thumbs_dir: Path, cam: str, run: _Run) -> str | None:
    try:
        day = datetime.fromtimestamp(run.ts_start, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        out_dir = thumbs_dir / cam / day
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{run.ts_start:.3f}_{run.cls}.jpg"
        cv2.imwrite(str(out), run.peak_frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return str(out.relative_to(thumbs_dir))
    except Exception:
        logger.exception("thumbnail save failed")
        return None


def _flush_run(db, cam: str, run: _Run, thumb_rel: str | None) -> None:
    db.execute(
        """INSERT INTO events
           (camera, class, ts_start, ts_end, max_conf, peak_bbox,
            thumb_path, sample_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            cam,
            run.cls,
            run.ts_start,
            run.ts_end,
            run.max_conf,
            json.dumps(list(run.peak_bbox)),
            thumb_rel,
            run.sample_count,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cam", required=True)
    parser.add_argument(
        "--since",
        required=True,
        help='ISO8601 ("2026-05-03T20:00:00Z") or relative ("90m" / "2h")',
    )
    parser.add_argument(
        "--until",
        default=None,
        help="ISO8601; default = now",
    )
    parser.add_argument("--config", default="cameras.yaml")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="delete existing events for this cam in the time range first",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(Path(args.config))
    inf = cfg.inference

    cam_dir = (cfg.recording.path / args.cam).resolve()
    if not cam_dir.is_dir():
        print(f"recordings dir for {args.cam} not found: {cam_dir}", file=sys.stderr)
        return 2

    since = _parse_time(args.since)
    until = _parse_time(args.until) if args.until else time.time()
    if until <= since:
        print("--until must be after --since", file=sys.stderr)
        return 2

    logger.info(
        "backfill cam=%s  range=%s..%s (%.1f min)",
        args.cam,
        datetime.utcfromtimestamp(since).isoformat() + "Z",
        datetime.utcfromtimestamp(until).isoformat() + "Z",
        (until - since) / 60.0,
    )

    segs = _list_segments(cam_dir, since, until)
    if not segs:
        print(f"no mp4 segments in {cam_dir} for that range", file=sys.stderr)
        return 1
    logger.info("matched %d segment(s):", len(segs))
    for p, s in segs:
        logger.info("  %s  starts %s", p.name, datetime.utcfromtimestamp(s).isoformat() + "Z")

    detector = Detector(
        yolo_pt=inf.yolo_pt,
        yolo_engine=inf.yolo_engine,
        event_classes=inf.event_classes,
        conf_threshold=inf.conf_threshold,
        class_thresholds=inf.class_thresholds,
    )
    db = init_db(inf.db_path)
    inf.thumbs_dir.mkdir(parents=True, exist_ok=True)

    if args.replace:
        rows = list(
            db.execute(
                "SELECT id, thumb_path FROM events "
                "WHERE camera=? AND ts_start>=? AND ts_start<?",
                (args.cam, since, until),
            )
        )
        for _id, thumb_rel in rows:
            if thumb_rel:
                try:
                    (inf.thumbs_dir / thumb_rel).unlink(missing_ok=True)
                except OSError:
                    pass
        db.execute(
            "DELETE FROM events WHERE camera=? AND ts_start>=? AND ts_start<?",
            (args.cam, since, until),
        )
        logger.info("--replace: deleted %d existing events in range", len(rows))

    runs: dict[str, _Run] = {}
    stats = {"frames": 0, "with_dets": 0, "events_written": 0}

    def update_runs(ts: float, dets: list[Detection], frame: Any) -> None:
        # Group by class, keep the highest-conf box for the frame (peak only persisted).
        best: dict[str, Detection] = {}
        for d in dets:
            cur = best.get(d.cls)
            if cur is None or d.conf > cur.conf:
                best[d.cls] = d
        for cls, det in best.items():
            r = runs.get(cls)
            if r is None:
                runs[cls] = _Run(
                    cls=cls,
                    ts_start=ts,
                    ts_end=ts,
                    max_conf=det.conf,
                    peak_bbox=det.bbox,
                    peak_frame=frame.copy(),
                )
                logger.info(
                    "%s open  %s @ %.2f  ts=%s",
                    args.cam,
                    cls,
                    det.conf,
                    datetime.utcfromtimestamp(ts).strftime("%H:%M:%S"),
                )
            else:
                r.ts_end = ts
                r.sample_count += 1
                if det.conf > r.max_conf:
                    r.max_conf = det.conf
                    r.peak_bbox = det.bbox
                    r.peak_frame = frame.copy()

    def close_stale(ts: float) -> None:
        stale = [c for c, r in runs.items() if (ts - r.ts_end) > inf.cooldown_s]
        for cls in stale:
            r = runs.pop(cls)
            thumb = _save_thumb(inf.thumbs_dir, args.cam, r)
            _flush_run(db, args.cam, r, thumb)
            stats["events_written"] += 1
            logger.info(
                "%s close %s  dur=%.1fs  samples=%d  conf=%.2f",
                args.cam,
                r.cls,
                r.ts_end - r.ts_start,
                r.sample_count,
                r.max_conf,
            )

    t0 = time.monotonic()
    for seg_path, seg_start in segs:
        cap = cv2.VideoCapture(str(seg_path))
        if not cap.isOpened():
            logger.warning("could not open %s; skipping", seg_path.name)
            continue
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        sample_every = max(1, int(round(fps)))
        logger.info("processing %s  fps=%.2f  sample_every=%d frames", seg_path.name, fps, sample_every)
        frame_idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_idx % sample_every == 0:
                ts = seg_start + frame_idx / fps
                if ts < since:
                    frame_idx += 1
                    continue
                if ts > until:
                    break
                try:
                    dets = detector.predict(frame)
                except Exception:
                    logger.exception("detector failed; skipping frame")
                    frame_idx += 1
                    continue
                stats["frames"] += 1
                if dets:
                    stats["with_dets"] += 1
                update_runs(ts, dets, frame)
                close_stale(ts)
            frame_idx += 1
        cap.release()

    # Final flush — any open runs at the end of the range get written too.
    for cls in list(runs.keys()):
        r = runs.pop(cls)
        thumb = _save_thumb(inf.thumbs_dir, args.cam, r)
        _flush_run(db, args.cam, r, thumb)
        stats["events_written"] += 1
        logger.info(
            "%s close %s  dur=%.1fs  samples=%d  conf=%.2f  (eof)",
            args.cam,
            r.cls,
            r.ts_end - r.ts_start,
            r.sample_count,
            r.max_conf,
        )

    db.close()
    logger.info(
        "done in %.1fs.  sampled=%d  with_dets=%d  events_written=%d",
        time.monotonic() - t0,
        stats["frames"],
        stats["with_dets"],
        stats["events_written"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
