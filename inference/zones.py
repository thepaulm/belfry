"""Recorder-side view of the ROI / alert-rule config (read-only).

The DVR owns writes to config.db (see ``dvr/zones.py``); the recorder
only reads ``rois`` + ``alert_rules`` to decide when a detection inside a
named region should fire an alert. ``ZoneIndex`` caches one camera's rows
and refreshes them on a slow timer (the tables are tiny and edits are
rare, so re-querying every ~15 s is negligible at 1 fps).

The two geometry helpers are duplicated from ``dvr/zones.py`` rather than
imported: the recorder runs in the cp310 ``.venv-inference`` and the DVR
in py3.14, so the packages don't share an interpreter. They are ~15 lines
of pure stdlib — keep them in sync.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("belfry.inference.zones")


def point_in_polygon(pt: tuple[float, float], poly: list[list[float]]) -> bool:
    """Even-odd ray cast. `poly` is [[x,y],...]; needs >= 3 vertices."""
    n = len(poly)
    if n < 3:
        return False
    x, y = pt
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def test_point(bbox: tuple[float, float, float, float], cls: str) -> tuple[float, float]:
    """The point we test against the ROI polygon for `cls`:
    person → feet (bottom-center); everything else → centroid."""
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2.0
    if cls == "person":
        return (cx, y2)
    return (cx, (y1 + y2) / 2.0)


class ZoneIndex:
    """Per-camera cache of enabled ROIs + alert rules from config.db.

    Read-only; safe to call from the single recorder thread that owns it.
    `maybe_refresh()` is cheap to call every tick — it only hits the DB
    when the refresh interval has elapsed.
    """

    def __init__(self, config_db_path: Path, camera: str, refresh_s: float = 15.0) -> None:
        self.db_path = config_db_path
        self.camera = camera
        self.refresh_s = refresh_s
        self._rois_by_id: dict[int, dict] = {}
        self._rules: list[dict] = []
        self._next_refresh = 0.0  # monotonic; 0 forces a load on first tick
        self.refresh()

    def maybe_refresh(self) -> None:
        if time.monotonic() >= self._next_refresh:
            self.refresh()

    def refresh(self) -> None:
        self._next_refresh = time.monotonic() + self.refresh_s
        rois_by_id: dict[int, dict] = {}
        rules: list[dict] = []
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            # config.db not created yet (no ROIs ever drawn) — no zones.
            self._rois_by_id, self._rules = {}, []
            return
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute(
                "SELECT id, name, polygon, enabled FROM rois WHERE camera = ?",
                (self.camera,),
            ):
                if not r["enabled"]:
                    continue
                try:
                    poly = json.loads(r["polygon"])
                except (json.JSONDecodeError, TypeError):
                    continue
                rois_by_id[r["id"]] = {"id": r["id"], "name": r["name"], "polygon": poly}
            for r in conn.execute(
                "SELECT id, roi_id, class, min_conf, cooldown_s, enabled "
                "FROM alert_rules WHERE camera = ?",
                (self.camera,),
            ):
                if not r["enabled"]:
                    continue
                rules.append({
                    "id": r["id"], "roi_id": r["roi_id"], "class": r["class"],
                    "min_conf": r["min_conf"], "cooldown_s": r["cooldown_s"],
                })
        except sqlite3.OperationalError:
            # Tables not created yet, or a mid-write race; treat as empty.
            rois_by_id, rules = {}, []
        finally:
            conn.close()
        self._rois_by_id, self._rules = rois_by_id, rules

    @property
    def has_rules(self) -> bool:
        return bool(self._rules)

    def matches(self, cls: str, conf: float, bbox) -> list[dict]:
        """Rules that fire for this detection (membership + conf only;
        per-run dedup and cooldown are the recorder's job). Each returned
        item is {rule_id, roi_id, roi_name, cooldown_s}."""
        if not self._rules:
            return []
        pt = test_point(bbox, cls)
        out: list[dict] = []
        for rule in self._rules:
            if rule["class"] != cls:
                continue
            if conf < (rule["min_conf"] or 0.0):
                continue
            roi = self._rois_by_id.get(rule["roi_id"])
            if roi is None:
                continue
            if point_in_polygon(pt, roi["polygon"]):
                out.append({
                    "rule_id": rule["id"],
                    "roi_id": roi["id"],
                    "roi_name": roi["name"],
                    "cooldown_s": rule["cooldown_s"],
                    "polygon": roi["polygon"],
                })
        return out
