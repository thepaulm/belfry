"""Two-model detector: MegaDetector v6 (animal/person/vehicle) + YOLO11n COCO.

Both models run on every frame; their outputs are merged by IoU so the more
specific COCO label wins when the bounding boxes overlap. Anything outside
the configured `event_classes` set is dropped.

Loaded lazily so this module is import-safe even when ultralytics or the
weight files aren't present on the host.
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


# MegaDetector v6 native class names. We map these to the broader buckets
# our event taxonomy uses ("animal", "person", "vehicle") via _MD_CLASS_MAP.
_MD_CLASS_MAP = {
    "animal": "animal",
    "person": "person",
    "vehicle": "vehicle",
}

# Subset of COCO classes we actually want to surface as events. Everything
# else from YOLO11 (toaster, frisbee, etc.) is dropped.
_YOLO_KEEP = {"person", "dog", "cat", "bird", "car", "truck"}

# Coarse-type compatibility: when both detectors fire on overlapping boxes,
# we'd like "animal" + "dog" to merge to "dog", not coexist as two events.
# This map tells us which YOLO class each MegaDetector class is allowed to
# refine to.
_MERGE_COMPAT: dict[str, set[str]] = {
    "animal": {"dog", "cat", "bird"},
    "person": {"person"},
    "vehicle": {"car", "truck"},
}


@dataclass(frozen=True)
class Detection:
    cls: str
    conf: float
    # Normalized 0..1 box coords (x1, y1, x2, y2) so we don't need to know
    # the source frame size downstream.
    bbox: tuple[float, float, float, float]


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    a_area = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    b_area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0


class Detector:
    """Wraps both Ultralytics models and produces merged Detection lists.

    Loading is deferred to first `predict()` so importing this module is
    free even when ultralytics / torch / engine files are missing — the
    failure surfaces at the call site where it's actionable.
    """

    def __init__(
        self,
        megadetector_pt: Path,
        megadetector_engine: Path,
        yolo_pt: Path,
        yolo_engine: Path,
        event_classes: tuple[str, ...],
        conf_threshold: float,
        class_thresholds: dict[str, float],
        merge_iou: float,
        imgsz: int = 640,
    ) -> None:
        self._md_pt = megadetector_pt
        self._md_engine = megadetector_engine
        self._yolo_pt = yolo_pt
        self._yolo_engine = yolo_engine
        self._event_classes = set(event_classes)
        self._conf_default = conf_threshold
        self._class_thresholds = dict(class_thresholds)
        self._merge_iou = merge_iou
        self._imgsz = imgsz

        self._md: Any = None
        self._yolo: Any = None
        self._md_names: dict[int, str] = {}
        self._yolo_names: dict[int, str] = {}
        # Ultralytics' YOLO.predict mutates state on the model object,
        # so concurrent calls from the per-camera recorder threads need
        # to be serialized. CUDA is single-stream by default anyway, so
        # there's nothing real to gain from concurrent dispatch.
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._md is not None and self._yolo is not None:
            return
        from ultralytics import YOLO

        md_path = self._md_engine if self._md_engine.exists() else self._md_pt
        yolo_path = self._yolo_engine if self._yolo_engine.exists() else self._yolo_pt
        for label, p in (("MegaDetector", md_path), ("YOLO11", yolo_path)):
            if not p.exists():
                raise FileNotFoundError(
                    f"{label} weights not found at {p}. Run scripts/install-inference.sh."
                )

        logger.info("loading MegaDetector from %s", md_path)
        self._md = YOLO(str(md_path))
        self._md_names = dict(self._md.names)
        logger.info("MegaDetector classes: %s", self._md_names)

        logger.info("loading YOLO11 from %s", yolo_path)
        self._yolo = YOLO(str(yolo_path))
        self._yolo_names = dict(self._yolo.names)
        logger.info("YOLO11 has %d classes (using subset %s)", len(self._yolo_names), sorted(_YOLO_KEEP))

    def _threshold_for(self, cls: str) -> float:
        return self._class_thresholds.get(cls, self._conf_default)

    @staticmethod
    def _normalize_boxes(result: Any) -> list[tuple[str, float, tuple[float, float, float, float]]]:
        # Ultralytics returns one Result per input image; we always pass
        # one image so this is the first element.
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        h, w = result.orig_shape  # (H, W) in pixels
        out: list[tuple[str, float, tuple[float, float, float, float]]] = []
        names = result.names
        # xyxy is pixel coords; normalize to 0..1.
        for cls_id, conf, xyxy in zip(
            boxes.cls.tolist(), boxes.conf.tolist(), boxes.xyxy.tolist()
        ):
            x1, y1, x2, y2 = xyxy
            cls_name = names[int(cls_id)]
            out.append((cls_name, float(conf), (x1 / w, y1 / h, x2 / w, y2 / h)))
        return out

    def predict(self, frame: "np.ndarray") -> list[Detection]:
        """Run both models on one BGR frame and return merged Detections.

        `frame` is the raw OpenCV BGR ndarray (HxWx3 uint8). Thread-safe:
        the GPU dispatch is serialized by an internal lock.
        """
        with self._lock:
            self._load()

            # We tell ultralytics to use a low conf at the model level so
            # we can apply our own per-class thresholds after the merge —
            # its default 0.25 would already filter out boxes we'd want
            # to count at a lower per-class threshold.
            md_raw = self._md.predict(
                frame, imgsz=self._imgsz, conf=0.10, verbose=False
            )[0]
            yolo_raw = self._yolo.predict(
                frame, imgsz=self._imgsz, conf=0.10, verbose=False
            )[0]

        md_dets = self._normalize_boxes(md_raw)
        yolo_dets = self._normalize_boxes(yolo_raw)

        # Map MegaDetector raw class names through _MD_CLASS_MAP and drop
        # anything we don't recognize. Lowercase the lookup since the
        # v1000 release didn't standardize casing across variants.
        md_dets = [
            (_MD_CLASS_MAP[c.lower()], conf, bbox)
            for c, conf, bbox in md_dets
            if c.lower() in _MD_CLASS_MAP
        ]
        # Filter YOLO down to the COCO classes we care about.
        yolo_dets = [(c, conf, bbox) for c, conf, bbox in yolo_dets if c in _YOLO_KEEP]

        merged = self._merge(md_dets, yolo_dets)

        # Per-class threshold + event-class filter.
        out: list[Detection] = []
        for cls, conf, bbox in merged:
            if cls not in self._event_classes:
                continue
            if conf < self._threshold_for(cls):
                continue
            out.append(Detection(cls=cls, conf=conf, bbox=bbox))
        return out

    def _merge(
        self,
        md: list[tuple[str, float, tuple[float, float, float, float]]],
        yolo: list[tuple[str, float, tuple[float, float, float, float]]],
    ) -> list[tuple[str, float, tuple[float, float, float, float]]]:
        # For each MD det, find the best YOLO det that's compatible AND
        # overlapping above the IoU threshold. If found, drop the MD det
        # and let the more-specific YOLO label survive. Otherwise keep
        # the MD det as-is. Any YOLO det not matched survives unchanged.
        consumed_md: set[int] = set()
        for mi, (mc, _mconf, mbox) in enumerate(md):
            best_j = -1
            best_iou = self._merge_iou
            compat = _MERGE_COMPAT.get(mc, set())
            for yj, (yc, _yconf, ybox) in enumerate(yolo):
                if yc not in compat:
                    continue
                iou = _iou(mbox, ybox)
                if iou >= best_iou:
                    best_iou = iou
                    best_j = yj
            if best_j >= 0:
                consumed_md.add(mi)
        survivors: list[tuple[str, float, tuple[float, float, float, float]]] = list(yolo)
        for mi, det in enumerate(md):
            if mi not in consumed_md:
                survivors.append(det)
        return survivors
