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

Modes:
- default — re-runs YOLO and writes class!='motion' events.
- ``--motion`` — runs the MOG2 motion detector against the recorded
  frames and writes only ``class='motion'`` events. To get IoU
  suppression without re-running YOLO (which would compete with the
  live recorder for the GPU), this mode reads the *existing* YOLO
  event rows from events.db and uses each row's peak_bbox as the
  suppression context for any sampled frame that falls inside its
  [ts_start, ts_end]. Approximation: the live YOLO peak_bbox doesn't
  perfectly track per-frame motion, so a person walking across frame
  might leave a few unsuppressed motion events at the start/end of
  their walk. Tunable later by tightening the IoU threshold.

Usage:
    python -m inference.backfill --cam cam12 --since 90m
    python -m inference.backfill --cam cam12 --since 2026-05-03T20:00:00Z --replace
    python -m inference.backfill --motion --all-cams --since 24h
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

from dvr.config import Camera, Config, load_config
from .model import Detection, Detector
from .motion import MotionDetector
from .recorder import draw_thumb_bbox, init_db

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
        draw_thumb_bbox(run.peak_frame, run.peak_bbox, run.cls)
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


def _active_yolo_dets_at(db, cam: str, ts: float) -> list[Detection]:
    """Synthetic Detections for every non-motion event row covering ``ts``.

    Motion-mode backfill doesn't re-run YOLO (would fight the live
    recorder for GPU), so it pulls the closest available suppression
    context from the live recorder's own YOLO event rows. The peak_bbox
    is approximate per-frame but tight enough to suppress the typical
    "walking person" double-detection.
    """
    rows = db.execute(
        "SELECT class, max_conf, peak_bbox FROM events "
        "WHERE camera=? AND class != 'motion' AND ts_start<=? AND ts_end>=?",
        (cam, ts, ts),
    ).fetchall()
    out: list[Detection] = []
    for cls, max_conf, peak_bbox in rows:
        try:
            bbox = tuple(json.loads(peak_bbox))
        except (TypeError, ValueError):
            continue
        if len(bbox) != 4:
            continue
        out.append(Detection(cls=cls, conf=float(max_conf), bbox=bbox))
    return out


def _delete_in_range(
    db,
    thumbs_dir: Path,
    cam: str,
    since: float,
    until: float,
    motion_only: bool,
) -> int:
    """Delete existing event rows + thumbnails in [since, until) for ``cam``.

    motion_only=True restricts to class='motion'. motion_only=False
    deletes everything *except* motion (so re-running YOLO doesn't blow
    away a separately-collected motion backfill).
    """
    where_cls = "class = 'motion'" if motion_only else "class != 'motion'"
    rows = list(
        db.execute(
            f"SELECT id, thumb_path FROM events "
            f"WHERE camera=? AND ts_start>=? AND ts_start<? AND {where_cls}",
            (cam, since, until),
        )
    )
    for _id, thumb_rel in rows:
        if thumb_rel:
            try:
                (thumbs_dir / thumb_rel).unlink(missing_ok=True)
            except OSError:
                pass
    db.execute(
        f"DELETE FROM events "
        f"WHERE camera=? AND ts_start>=? AND ts_start<? AND {where_cls}",
        (cam, since, until),
    )
    return len(rows)


def _run_one_cam(
    cfg: Config,
    cam_name: str,
    since: float,
    until: float,
    *,
    detector: Detector | None,
    motion: bool,
    replace: bool,
) -> dict:
    """Backfill one camera. Returns a stats dict.

    Exactly one of `detector` (YOLO mode) or `motion=True` should be set.
    """
    inf = cfg.inference
    cam_dir = (cfg.recording.path / cam_name).resolve()
    if not cam_dir.is_dir():
        logger.warning("recordings dir for %s not found: %s", cam_name, cam_dir)
        return {"frames": 0, "with_dets": 0, "events_written": 0, "missing_dir": True}

    segs = _list_segments(cam_dir, since, until)
    if not segs:
        logger.warning("%s: no mp4 segments in range", cam_name)
        return {"frames": 0, "with_dets": 0, "events_written": 0, "no_segments": True}

    logger.info(
        "%s: matched %d segment(s), mode=%s",
        cam_name, len(segs), "motion" if motion else "yolo",
    )

    db = init_db(inf.db_path)
    inf.thumbs_dir.mkdir(parents=True, exist_ok=True)

    if replace:
        n = _delete_in_range(db, inf.thumbs_dir, cam_name, since, until, motion)
        logger.info(
            "%s: --replace deleted %d existing %s events in range",
            cam_name, n, "motion" if motion else "non-motion",
        )

    motion_detector: MotionDetector | None = None
    if motion:
        motion_detector = MotionDetector(
            history=inf.motion_history,
            var_threshold=inf.motion_var_threshold,
            min_blob_pct=inf.motion_min_blob_pct,
            min_persistence_frames=inf.motion_min_persistence_frames,
        )

    runs: dict[str, _Run] = {}
    stats = {"frames": 0, "with_dets": 0, "events_written": 0}

    def update_runs(ts: float, dets: list[Detection], frame: Any) -> None:
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
                    cam_name, cls, det.conf,
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
            thumb = _save_thumb(inf.thumbs_dir, cam_name, r)
            _flush_run(db, cam_name, r, thumb)
            stats["events_written"] += 1
            logger.info(
                "%s close %s  dur=%.1fs  samples=%d  conf=%.2f",
                cam_name, r.cls,
                r.ts_end - r.ts_start, r.sample_count, r.max_conf,
            )

    t0 = time.monotonic()
    for seg_path, seg_start in segs:
        cap = cv2.VideoCapture(str(seg_path))
        if not cap.isOpened():
            logger.warning("could not open %s; skipping", seg_path.name)
            continue
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        sample_every = max(1, int(round(fps / max(1, inf.record_fps))))
        logger.info(
            "%s: processing %s  fps=%.2f  sample_every=%d frames",
            cam_name, seg_path.name, fps, sample_every,
        )
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
                    if motion:
                        yolo_ctx = _active_yolo_dets_at(db, cam_name, ts)
                        dets = motion_detector.detect(frame, yolo_ctx)
                    else:
                        assert detector is not None
                        dets = detector.predict(frame)
                except Exception:
                    logger.exception("%s: detector failed; skipping frame", cam_name)
                    frame_idx += 1
                    continue
                stats["frames"] += 1
                if dets:
                    stats["with_dets"] += 1
                update_runs(ts, dets, frame)
                close_stale(ts)
            frame_idx += 1
        cap.release()

    for cls in list(runs.keys()):
        r = runs.pop(cls)
        thumb = _save_thumb(inf.thumbs_dir, cam_name, r)
        _flush_run(db, cam_name, r, thumb)
        stats["events_written"] += 1
        logger.info(
            "%s close %s  dur=%.1fs  samples=%d  conf=%.2f  (eof)",
            cam_name, r.cls, r.ts_end - r.ts_start, r.sample_count, r.max_conf,
        )

    db.close()
    elapsed = time.monotonic() - t0
    logger.info(
        "%s: done in %.1fs.  sampled=%d  with_dets=%d  events_written=%d",
        cam_name, elapsed,
        stats["frames"], stats["with_dets"], stats["events_written"],
    )
    stats["elapsed_s"] = elapsed
    return stats


def _resolve_cams(
    cfg: Config, cam_arg: str | None, all_cams: bool, motion: bool,
) -> list[Camera]:
    if all_cams:
        cams = [c for c in cfg.all_cameras if c.enabled and c.inference]
        if motion:
            cams = [c for c in cams if cfg.inference.motion_on_for(c)]
        return cams
    if cam_arg is None:
        return []
    cam = next((c for c in cfg.all_cameras if c.name == cam_arg), None)
    return [cam] if cam else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cam", default=None,
                        help="single camera name (mutually exclusive with --all-cams)")
    parser.add_argument(
        "--all-cams", action="store_true",
        help="loop over every cam with `inference: true`; with --motion, "
             "additionally filtered to cams where motion is enabled.",
    )
    parser.add_argument(
        "--since",
        required=True,
        help='ISO8601 ("2026-05-03T20:00:00Z") or relative ("90m" / "2h" / "24h")',
    )
    parser.add_argument(
        "--until",
        default=None,
        help="ISO8601; default = now",
    )
    parser.add_argument(
        "--motion", action="store_true",
        help="motion-detection mode: writes class='motion' rows only; "
             "uses existing YOLO event rows from events.db for IoU "
             "suppression instead of re-running YOLO.",
    )
    parser.add_argument("--config", default="cameras.yaml")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="in YOLO mode delete existing non-motion events in range; "
             "in --motion mode delete existing motion events in range.",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    if not args.cam and not args.all_cams:
        print("must specify --cam <name> or --all-cams", file=sys.stderr)
        return 2
    if args.cam and args.all_cams:
        print("--cam and --all-cams are mutually exclusive", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(Path(args.config))
    inf = cfg.inference

    since = _parse_time(args.since)
    until = _parse_time(args.until) if args.until else time.time()
    if until <= since:
        print("--until must be after --since", file=sys.stderr)
        return 2

    cams = _resolve_cams(cfg, args.cam, args.all_cams, args.motion)
    if not cams:
        print("no cameras matched the selection", file=sys.stderr)
        return 1

    logger.info(
        "backfill mode=%s  cams=%s  range=%s..%s (%.1f min)",
        "motion" if args.motion else "yolo",
        ",".join(c.name for c in cams),
        datetime.utcfromtimestamp(since).isoformat() + "Z",
        datetime.utcfromtimestamp(until).isoformat() + "Z",
        (until - since) / 60.0,
    )

    detector: Detector | None = None
    if not args.motion:
        detector = Detector(
            yolo_pt=inf.yolo_pt,
            yolo_engine=inf.yolo_engine,
            event_classes=inf.event_classes,
            conf_threshold=inf.conf_threshold,
            class_thresholds=inf.class_thresholds,
            class_aliases=inf.class_aliases,
        )

    overall = {"frames": 0, "with_dets": 0, "events_written": 0, "elapsed_s": 0.0}
    t_all = time.monotonic()
    for cam in cams:
        try:
            stats = _run_one_cam(
                cfg, cam.name, since, until,
                detector=detector, motion=args.motion, replace=args.replace,
            )
        except Exception:
            logger.exception("backfill failed for %s; continuing", cam.name)
            continue
        for k in ("frames", "with_dets", "events_written", "elapsed_s"):
            overall[k] += stats.get(k, 0)

    logger.info(
        "all-cams done in %.1fs (per-cam sum %.1fs).  sampled=%d  with_dets=%d  events_written=%d",
        time.monotonic() - t_all, overall["elapsed_s"],
        overall["frames"], overall["with_dets"], overall["events_written"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
