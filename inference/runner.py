"""Multi-camera inference runner — production entry point.

Spawns one EventRecorder thread per camera with `inference: true` in
cameras.yaml. All threads share a single Detector (and thus a single
GPU model — its predict() is internally locked); each thread owns its
own SQLite connection so writes don't trip sqlite3's
check_same_thread guard.

Wired to systemd via scripts/belfry-inference.service. SIGTERM /
SIGINT triggers a clean shutdown: the stop flag flips, every recorder
loop notices it on its next tick, releases its capture, flushes any
in-flight event runs to the DB, and joins.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from dvr.config import load_config
from .model import Detector
from .recorder import EventRecorder

logger = logging.getLogger("belfry.inference.runner")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="cameras.yaml",
        help="Path to cameras.yaml (default: ./cameras.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(Path(args.config))
    cams = [c for c in cfg.all_cameras if c.enabled and c.inference]
    if not cams:
        logger.warning(
            "no cameras have `inference: true` in %s — nothing to do", args.config
        )
        return 0

    inf = cfg.inference
    detector = Detector(
        megadetector_pt=inf.megadetector_pt,
        megadetector_engine=inf.megadetector_engine,
        yolo_pt=inf.yolo_pt,
        yolo_engine=inf.yolo_engine,
        event_classes=inf.event_classes,
        conf_threshold=inf.conf_threshold,
        class_thresholds=inf.class_thresholds,
        merge_iou=inf.merge_iou,
    )
    inf.thumbs_dir.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()

    def _on_signal(signum, _frame):
        logger.info("got signal %d; signalling all recorders to stop", signum)
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    threads: list[threading.Thread] = []
    for cam in cams:
        recorder = EventRecorder(
            camera_name=cam.name,
            rtsp_url=cam.rtsp,
            detector=detector,
            db_path=inf.db_path,
            thumbs_dir=inf.thumbs_dir,
            record_fps=inf.record_fps,
            cooldown_s=inf.cooldown_s,
        )
        t = threading.Thread(
            target=recorder.run,
            args=(stop.is_set,),
            name=f"rec-{cam.name}",
            daemon=False,
        )
        t.start()
        threads.append(t)
        logger.info("started recorder thread for %s", cam.name)

    logger.info("running %d recorder thread(s); waiting for signal", len(threads))
    # Wait on the stop event so the main thread doesn't busy-spin and
    # SIGTERM is delivered promptly. Re-check thread liveness so we
    # exit if every recorder dies (e.g. all RTSP feeds permanently fail).
    while not stop.is_set():
        stop.wait(timeout=10.0)
        alive = [t for t in threads if t.is_alive()]
        if not alive:
            logger.error("all recorder threads died; exiting")
            break

    logger.info("waiting for recorders to flush and join")
    for t in threads:
        t.join(timeout=30.0)
        if t.is_alive():
            logger.warning("thread %s did not join within 30s", t.name)
    logger.info("all recorders stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
