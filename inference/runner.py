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
import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import uvicorn

from dvr.config import load_config
from .model import Detector
from .motion import MotionDetector
from .notify import FcmNotifier
from .recorder import EventRecorder
from .server import app as inference_app
from .zones import ZoneIndex

logger = logging.getLogger("belfry.inference.runner")

_LIVE_HOST = "127.0.0.1"
_LIVE_PORT = 9091

# Watchdog: if a recorder that was healthy produces no frames for this
# long, its drain thread is almost certainly wedged inside cv2's blocking
# VideoCapture open (a known failure after a MediaMTX blip 404-storms the
# loopback paths — see recorder._open_capture). Such a thread stays
# is_alive() == True, so the "all threads died" check below can't catch it.
# A wedged C call can't be interrupted from Python, so recovery is to exit
# the process and let systemd (Restart=on-failure) rebuild every capture.
# 120 s comfortably clears legitimate reopen gaps (~2–7 s) while bounding an
# outage to ~2 min instead of the ~28 h a silent wedge cost on 2026-06-30.
_WATCHDOG_STALL_S = 120.0
_WATCHDOG_INTERVAL_S = 20.0


def _watchdog(recorders: "list[EventRecorder]", stop: threading.Event) -> None:
    """Force a process exit if any healthy recorder goes frame-silent."""
    while not stop.wait(_WATCHDOG_INTERVAL_S):
        now = time.monotonic()
        stale = []
        for rec in recorders:
            age = rec.frames_stalled_for(now)
            if age is not None and age > _WATCHDOG_STALL_S:
                stale.append((rec.camera_name, age))
        if stale:
            for name, age in stale:
                logger.error(
                    "watchdog: %s produced no frames for %.0fs (drain likely "
                    "wedged in cv2 open)", name, age,
                )
            logger.error(
                "watchdog: %d camera(s) frame-silent; exiting so systemd "
                "restarts and rebuilds all captures", len(stale),
            )
            # os._exit, not sys.exit: a wedged drain thread is non-daemon and
            # stuck in C, so a normal interpreter shutdown would hang on join.
            os._exit(1)


def _run_uvicorn(stop: threading.Event) -> None:
    """Run the inference FastAPI on a private asyncio loop in this
    thread. install_signal_handlers=False so it doesn't fight the
    runner's main-thread SIGTERM/SIGINT handlers; we ask the server
    to exit ourselves once the stop event flips."""
    config = uvicorn.Config(
        inference_app,
        host=_LIVE_HOST,
        port=_LIVE_PORT,
        log_level="warning",       # uvicorn's INFO is per-request noise
        access_log=False,
    )
    config.install_signal_handlers = False
    server = uvicorn.Server(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _watch_stop() -> None:
        # Poll the threading.Event from inside the asyncio loop and
        # ask uvicorn to exit when it flips. asyncio.Event would be
        # cleaner but the signal handler is on a different thread.
        while not stop.is_set():
            await asyncio.sleep(0.5)
        server.should_exit = True

    try:
        loop.run_until_complete(asyncio.gather(server.serve(), _watch_stop()))
    finally:
        loop.close()


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
        yolo_pt=inf.yolo_pt,
        yolo_engine=inf.yolo_engine,
        event_classes=inf.event_classes,
        conf_threshold=inf.conf_threshold,
        class_thresholds=inf.class_thresholds,
        class_aliases=inf.class_aliases,
    )
    # Make the detector reachable to /playback inside the FastAPI app.
    # db_path rides along so the playback handler can surface stored
    # motion events alongside the re-run YOLO boxes.
    inference_app.state.detector = detector
    inference_app.state.db_path = inf.db_path
    inf.thumbs_dir.mkdir(parents=True, exist_ok=True)

    # FCM push for ROI alerts. Disabled (no-op enqueue) when no
    # credentials JSON is configured — alerts still persist + serve.
    notifier = FcmNotifier(
        inf.config_db_path,
        cfg.notify.fcm_credentials_path,
        cfg.notify.fcm_project_id,
    )
    notifier.start()

    stop = threading.Event()

    def _on_signal(signum, _frame):
        logger.info("got signal %d; signalling all recorders to stop", signum)
        stop.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    threads: list[threading.Thread] = []
    recorders: list[EventRecorder] = []
    # SSE server thread first — recorders publish to a broadcaster that's
    # only useful once the loop exists. publish_threadsafe no-ops while
    # the loop reference is None, so any race here is benign.
    uvicorn_thread = threading.Thread(
        target=_run_uvicorn, args=(stop,), name="uvicorn", daemon=True,
    )
    uvicorn_thread.start()
    logger.info("started uvicorn on %s:%d (live SSE)", _LIVE_HOST, _LIVE_PORT)

    for cam in cams:
        # Pull from MediaMTX's loopback RTSP rather than the camera's
        # direct stream. MediaMTX keeps a single upstream session per
        # path and multiplexes it to all readers, so this adds zero
        # extra TCP connections to the camera. Using the camera URL
        # directly meant the Hikvisions had to serve two concurrent
        # main-stream consumers (recording + inference), which they
        # handle poorly — every recorder thread saw "read failed;
        # reopening" warnings every 1–2 minutes and dropped frames.
        loopback_rtsp = f"rtsp://127.0.0.1:8554/{cam.name}"
        motion_detector: MotionDetector | None = None
        if inf.motion_on_for(cam):
            motion_detector = MotionDetector(
                history=inf.motion_history,
                var_threshold=inf.motion_var_threshold,
                min_blob_pct=inf.motion_min_blob_pct,
                min_persistence_frames=inf.motion_min_persistence_frames,
            )
            logger.info("motion detector enabled for %s", cam.name)
        zone_index = ZoneIndex(inf.config_db_path, cam.name)
        recorder = EventRecorder(
            camera_name=cam.name,
            rtsp_url=loopback_rtsp,
            detector=detector,
            db_path=inf.db_path,
            thumbs_dir=inf.thumbs_dir,
            record_fps=inf.record_fps,
            cooldown_s=inf.cooldown_s,
            motion_detector=motion_detector,
            zone_index=zone_index,
            notifier=notifier.enqueue,
        )
        t = threading.Thread(
            target=recorder.run,
            args=(stop.is_set,),
            name=f"rec-{cam.name}",
            daemon=False,
        )
        t.start()
        threads.append(t)
        recorders.append(recorder)
        logger.info("started recorder thread for %s", cam.name)

    watchdog_thread = threading.Thread(
        target=_watchdog, args=(recorders, stop), name="watchdog", daemon=True,
    )
    watchdog_thread.start()

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
    notifier.stop()
    logger.info("all recorders stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
