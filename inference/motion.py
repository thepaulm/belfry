"""Per-camera motion detector.

Wraps OpenCV's MOG2 background subtractor with the denoise + persistence
filters needed to keep "leaves blowing in the wind" off the events DB.

Why bother when YOLO11l already runs at 1 fps per camera? YOLO only
recognises the COCO classes we asked for (person/dog/cat/bird/car/truck)
and is blind to everything else in the back yard — deer, raccoons,
opossums, fox, coyote, the neighbour's spaniel-that-isn't-quite-a-dog.
Motion detection surfaces those as ``class="motion"`` events so they
don't fall through the cracks while we collect data for a wildlife
fine-tune.

Algorithm per inference tick (called from ``EventRecorder._loop`` after
the YOLO predict, with the same BGR frame and the resulting YOLO
detections):

1. Resize to 640x360 — MOG2 is the dominant cost and downsampling
   gives a ~10× speedup on the Orin's CPU with no loss in blob detection
   for the object sizes we care about (squirrel and up).
2. ``mog2.apply(small)`` produces a fg mask. Shadow pixels come back
   marked 127; we threshold to 255-only so a long evening shadow doesn't
   trip the detector.
3. Morphological open then close cleans isolated speckle and fills
   pinholes in real blobs.
4. Contour the mask, take bounding rects, drop anything below
   ``min_blob_pct`` of the (downsampled) frame area.
5. Persistence filter: require each surviving box to overlap (IoU > 0.3)
   a box from the previous tick. Single-frame blips drop here.
6. IoU suppression against YOLO detections: if a motion box overlaps a
   YOLO box at IoU > 0.5, drop the motion box — a walking person is
   already a ``person`` event, not also a ``motion`` event.
7. Normalise to 0..1 coords. Output piggybacks the ``Detection`` shape
   so the recorder's per-class event-coalescer treats motion like any
   other class.

The instance is **not** thread-safe — MOG2 keeps a mutable per-pixel
Gaussian-mixture model that's tuned to one camera's scene. Each
``EventRecorder`` (one thread per camera) owns its own ``MotionDetector``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import cv2

from .model import Detection

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("belfry.inference.motion")


# Internal proc resolution. 640x360 is 1/9 the pixel count of 1080p and
# keeps the smallest objects we care about (~6x6 px for a squirrel) well
# above the area threshold.
_PROC_W = 640
_PROC_H = 360


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Axis-aligned IoU on two (x1, y1, x2, y2) boxes in the same coord system."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    aa = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    bb = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = aa + bb - inter
    if union <= 0:
        return 0.0
    return inter / union


@dataclass(frozen=True)
class _PixBox:
    """Box in downsampled-pixel coords (x1, y1, x2, y2)."""
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def area(self) -> int:
        return max(0, self.x2 - self.x1) * max(0, self.y2 - self.y1)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (float(self.x1), float(self.y1), float(self.x2), float(self.y2))


class MotionDetector:
    def __init__(
        self,
        history: int = 500,
        var_threshold: int = 25,
        min_blob_pct: float = 0.005,
        min_persistence_frames: int = 2,
        yolo_suppress_iou: float = 0.5,
        persistence_iou: float = 0.3,
    ) -> None:
        self._min_blob_px = int(_PROC_W * _PROC_H * min_blob_pct)
        self._min_persistence_frames = max(1, min_persistence_frames)
        self._yolo_suppress_iou = yolo_suppress_iou
        self._persistence_iou = persistence_iou
        self._mog2 = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=var_threshold, detectShadows=True,
        )
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        # Previous tick's surviving boxes (downsampled-pixel coords) and
        # their match counts. Used by the persistence filter.
        self._prev: list[tuple[_PixBox, int]] = []

    def detect(self, frame: "np.ndarray", yolo_dets: list[Detection]) -> list[Detection]:
        """Run one motion tick. Returns ``Detection`` objects with cls='motion'.

        ``yolo_dets`` carries the YOLO detections for the same frame — used
        for IoU suppression so walking people don't double-fire as motion.
        """
        small = cv2.resize(frame, (_PROC_W, _PROC_H), interpolation=cv2.INTER_AREA)
        mask = self._mog2.apply(small)
        # MOG2 marks shadows with value 127. Threshold to foreground-only
        # so we don't fire on long late-afternoon shadows.
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes: list[_PixBox] = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            b = _PixBox(x, y, x + w, y + h)
            if b.area >= self._min_blob_px:
                boxes.append(b)

        # Persistence filter: a box has to match (IoU > persistence_iou) a
        # prior-tick box at least min_persistence_frames times in a row
        # before it's emitted. Single-frame sensor blips drop out here.
        emitted: list[_PixBox] = []
        next_prev: list[tuple[_PixBox, int]] = []
        for b in boxes:
            best = 0.0
            best_count = 0
            for pb, pcount in self._prev:
                i = _iou(b.as_tuple(), pb.as_tuple())
                if i > best:
                    best = i
                    best_count = pcount
            count = best_count + 1 if best >= self._persistence_iou else 1
            next_prev.append((b, count))
            if count >= self._min_persistence_frames:
                emitted.append(b)
        self._prev = next_prev

        # YOLO suppression in *normalized* coords so we compare in a single
        # space (YOLO bboxes are 0..1 against the original frame; motion
        # boxes are pixel coords against the 640x360 downsample, but the
        # normalised-against-_PROC_W/H representation lines up exactly).
        yolo_norm = [d.bbox for d in yolo_dets]
        out: list[Detection] = []
        for b in emitted:
            nx1, ny1 = b.x1 / _PROC_W, b.y1 / _PROC_H
            nx2, ny2 = b.x2 / _PROC_W, b.y2 / _PROC_H
            nbbox = (nx1, ny1, nx2, ny2)
            if any(_iou(nbbox, yb) > self._yolo_suppress_iou for yb in yolo_norm):
                continue
            # Use the blob's area fraction as the "confidence" so the
            # recorder's peak-by-conf picks the moment of biggest motion
            # for the thumbnail and event row. Naturally lives in [0, 1].
            area_pct = b.area / float(_PROC_W * _PROC_H)
            out.append(Detection(cls="motion", conf=area_pct, bbox=nbbox))
        return out


# ----------------------------------------------------------------------
# Single-camera smoke test:  python -m inference.motion --cam cam5
#
# Reads the loopback RTSP for the named camera at native ~30 fps using
# the same drain pattern the recorder uses, samples at 1 fps, prints
# every motion detection. Useful for eyeballing thresholds before
# turning motion on in production.

def _smoketest() -> int:
    parser = argparse.ArgumentParser(description="Single-camera motion smoke test")
    parser.add_argument("--cam", required=True, help="camera name (must match cameras.yaml)")
    parser.add_argument("--duration", type=int, default=60, help="seconds to run")
    parser.add_argument("--rtsp", default=None,
                        help="override RTSP URL (default: rtsp://127.0.0.1:8554/<cam>)")
    parser.add_argument("--min-blob-pct", type=float, default=0.005)
    parser.add_argument("--var-threshold", type=int, default=25)
    parser.add_argument("--save-mask-dir", type=Path, default=None,
                        help="if set, write the MOG2 mask png each tick — for tuning")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    url = args.rtsp or f"rtsp://127.0.0.1:8554/{args.cam}"
    logger.info("opening %s", url)
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        logger.error("could not open %s", url)
        return 1
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    md = MotionDetector(
        min_blob_pct=args.min_blob_pct, var_threshold=args.var_threshold,
    )

    if args.save_mask_dir:
        args.save_mask_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + args.duration
    next_tick = time.monotonic()
    last_frame = None
    while time.time() < deadline:
        ok, frame = cap.read()
        if not ok or frame is None:
            time.sleep(0.05)
            continue
        last_frame = frame
        now = time.monotonic()
        if now < next_tick:
            continue
        next_tick = now + 1.0
        dets = md.detect(last_frame, yolo_dets=[])
        if dets:
            for d in dets:
                logger.info(
                    "motion  area=%.3f  bbox=(%.2f,%.2f,%.2f,%.2f)",
                    d.conf, *d.bbox,
                )
        else:
            logger.info("motion  -")

    cap.release()
    return 0


if __name__ == "__main__":
    sys.exit(_smoketest())
