"""Single-model YOLO11l detector (COCO classes, filtered to a subset).

We previously ran MegaDetector + YOLO11n in parallel and merged via IoU,
but that ensemble was too eager on cam12-style backdrops (driveway,
shadows, distant figures) — it produced person events at confidence
0.40–0.46 that the live recorder wrote to events.db while past-mode
re-inference correctly declined to redraw. Net effect on the UI was
person pips sitting on patches of empty driveway.

YOLO11l alone is more consistent: live and past inference run the
same weights, so what gets logged is what gets re-detected. We lose
MegaDetector's coarse `animal` class, but a wildlife-species fine-tune
was already in the v2 plan, so this is the natural cutover.

Loading is deferred to first `predict()` so this module is import-safe
even when ultralytics or the engine file is missing — the failure
surfaces at the call site where it's actionable.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("belfry.inference.model")


# Subset of COCO classes we surface as events. Everything else from
# YOLO11 (toaster, frisbee, etc.) is dropped before the per-class
# threshold check.
_YOLO_KEEP = {"person", "dog", "cat", "bird", "car", "truck"}


@dataclass(frozen=True)
class Detection:
    cls: str
    conf: float
    # Normalized 0..1 box coords (x1, y1, x2, y2) so we don't need to know
    # the source frame size downstream.
    bbox: tuple[float, float, float, float]


class Detector:
    """Wraps the Ultralytics YOLO11l model and produces filtered Detections.

    Loading is deferred to first `predict()`. predict() is internally
    locked because Ultralytics' YOLO mutates state on the model object
    and CUDA is single-stream by default — concurrent dispatch buys us
    nothing.
    """

    def __init__(
        self,
        yolo_pt: Path,
        yolo_engine: Path,
        event_classes: tuple[str, ...],
        conf_threshold: float,
        class_thresholds: dict[str, float],
        imgsz: int = 640,
    ) -> None:
        self._yolo_pt = yolo_pt
        self._yolo_engine = yolo_engine
        self._event_classes = set(event_classes)
        self._conf_default = conf_threshold
        self._class_thresholds = dict(class_thresholds)
        self._imgsz = imgsz

        self._yolo: Any = None
        self._yolo_names: dict[int, str] = {}
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._yolo is not None:
            return
        from ultralytics import YOLO

        path = self._yolo_engine if self._yolo_engine.exists() else self._yolo_pt
        if not path.exists():
            raise FileNotFoundError(
                f"YOLO11l weights not found at {path}. Run scripts/install-inference.sh."
            )

        logger.info("loading YOLO11l from %s", path)
        self._yolo = YOLO(str(path))
        self._yolo_names = dict(self._yolo.names)
        logger.info(
            "YOLO11l has %d classes (using subset %s)",
            len(self._yolo_names),
            sorted(_YOLO_KEEP),
        )

    def _threshold_for(self, cls: str) -> float:
        return self._class_thresholds.get(cls, self._conf_default)

    @staticmethod
    def _normalize_boxes(
        result: Any,
    ) -> list[tuple[str, float, tuple[float, float, float, float]]]:
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        h, w = result.orig_shape  # (H, W) in pixels
        names = result.names
        out: list[tuple[str, float, tuple[float, float, float, float]]] = []
        for cls_id, conf, xyxy in zip(
            boxes.cls.tolist(), boxes.conf.tolist(), boxes.xyxy.tolist()
        ):
            x1, y1, x2, y2 = xyxy
            cls_name = names[int(cls_id)]
            out.append((cls_name, float(conf), (x1 / w, y1 / h, x2 / w, y2 / h)))
        return out

    def predict(self, frame: "np.ndarray") -> list[Detection]:
        """Run YOLO11l on one BGR frame and return filtered Detections.

        `frame` is the raw OpenCV BGR ndarray (HxWx3 uint8). Thread-safe.
        """
        with self._lock:
            self._load()
            # Low model-level conf so our per-class thresholds can still
            # surface low-confidence classes if a class_thresholds entry
            # asks for it. Ultralytics' default 0.25 would pre-filter
            # below where some legit per-class thresholds want to look.
            raw = self._yolo.predict(
                frame, imgsz=self._imgsz, conf=0.10, verbose=False
            )[0]

        dets = self._normalize_boxes(raw)

        out: list[Detection] = []
        for cls, conf, bbox in dets:
            if cls not in _YOLO_KEEP:
                continue
            if cls not in self._event_classes:
                continue
            if conf < self._threshold_for(cls):
                continue
            out.append(Detection(cls=cls, conf=conf, bbox=bbox))
        return out
