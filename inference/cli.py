"""Slice 1 entry point: run the event recorder against one camera in the
foreground. `python -m inference.cli --cam cam6` is the smoke-test path.

Slice 2 will replace this with a multi-camera systemd service that fans
out into one EventRecorder per camera with `inference: true`.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from dvr.config import load_config
from .model import Detector
from .recorder import EventRecorder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cam",
        required=True,
        help="Camera name (must exist in cameras.yaml)",
    )
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
    cam = next((c for c in cfg.all_cameras if c.name == args.cam), None)
    if cam is None:
        names = sorted(c.name for c in cfg.all_cameras)
        print(f"camera {args.cam!r} not found. Known: {', '.join(names)}", file=sys.stderr)
        return 2

    inf = cfg.inference
    detector = Detector(
        yolo_pt=inf.yolo_pt,
        yolo_engine=inf.yolo_engine,
        event_classes=inf.event_classes,
        conf_threshold=inf.conf_threshold,
        class_thresholds=inf.class_thresholds,
    )
    inf.thumbs_dir.mkdir(parents=True, exist_ok=True)

    recorder = EventRecorder(
        camera_name=cam.name,
        rtsp_url=cam.rtsp,
        detector=detector,
        db_path=inf.db_path,
        thumbs_dir=inf.thumbs_dir,
        record_fps=inf.record_fps,
        cooldown_s=inf.cooldown_s,
    )

    stopped = {"flag": False}

    def _stop(signum, _frame):
        logging.getLogger("belfry.inference.cli").info("got signal %d, stopping", signum)
        stopped["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    recorder.run(stop_check=lambda: stopped["flag"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
