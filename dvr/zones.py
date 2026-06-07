"""Config DB (ROIs + alert rules + device tokens) and ROI geometry.

Mirrors ``dvr/training.py``'s role: paths + helpers around a body of
durable user config, used by the DVR's REST routes. The config DB lives
at ``recordings/config.db`` (separate from ``events.db`` so rebuilding
derived event data never wipes hand-drawn zones). Shared DDL is
``inference/config_schema.sql``.

The DVR owns all writes here; the inference recorder reads the same DB
(via its own ``inference/zones.py``) to decide when a detection inside a
named region should fire an alert.

Geometry — polygons and detection boxes are both normalized 0..1:
  * ``point_in_polygon`` is a standard even-odd ray cast.
  * ``test_point`` returns the single point we test against a polygon for
    a class: a person's feet (bottom-center, where they actually stand)
    vs the box centroid for everything else. Keep this in sync with the
    copy in ``inference/zones.py`` (duplicated rather than imported across
    the dvr py3.14 / inference cp310 venv boundary).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_SCHEMA_FILE = Path(__file__).resolve().parent.parent / "inference" / "config_schema.sql"


# --------------------------------------------------------------------------
# geometry

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
    """The point we test against the ROI polygon for `cls`.

    person → feet (bottom-center); everything else → centroid.
    """
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2.0
    if cls == "person":
        return (cx, y2)
    return (cx, (y1 + y2) / 2.0)


# --------------------------------------------------------------------------
# validation

def validate_polygon(raw) -> list[list[float]]:
    """Coerce/validate an incoming polygon into [[x,y],...] of floats in
    0..1. Raises ValueError on anything malformed (caller → HTTP 400)."""
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        raise ValueError("polygon needs at least 3 points")
    out: list[list[float]] = []
    for pt in raw:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            raise ValueError(f"bad polygon point: {pt!r}")
        x, y = float(pt[0]), float(pt[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError(f"polygon point out of 0..1 range: {pt!r}")
        out.append([x, y])
    return out


# --------------------------------------------------------------------------
# connection

def init_config_db(db_path: Path) -> sqlite3.Connection:
    """Open an autocommit WAL connection and ensure the schema exists.
    Caller closes. Safe to call concurrently (CREATE … IF NOT EXISTS)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(_SCHEMA_FILE.read_text())
    conn.row_factory = sqlite3.Row
    return conn


def _roi_to_dict(r: sqlite3.Row) -> dict:
    try:
        poly = json.loads(r["polygon"])
    except (json.JSONDecodeError, TypeError):
        poly = []
    return {
        "id": r["id"],
        "camera": r["camera"],
        "name": r["name"],
        "polygon": poly,
        "enabled": bool(r["enabled"]),
        "created_at": r["created_at"],
    }


def _rule_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "camera": r["camera"],
        "roi_id": r["roi_id"],
        "class": r["class"],
        "min_conf": r["min_conf"],
        "cooldown_s": r["cooldown_s"],
        "enabled": bool(r["enabled"]),
        "created_at": r["created_at"],
    }


# --------------------------------------------------------------------------
# ROI CRUD

def list_rois(db_path: Path, camera: str | None = None) -> list[dict]:
    conn = init_config_db(db_path)
    try:
        if camera is not None:
            rows = conn.execute(
                "SELECT * FROM rois WHERE camera = ? ORDER BY id", (camera,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM rois ORDER BY id").fetchall()
    finally:
        conn.close()
    return [_roi_to_dict(r) for r in rows]


def create_roi(
    db_path: Path, camera: str, name: str, polygon: list[list[float]], enabled: bool = True
) -> dict:
    conn = init_config_db(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO rois (camera, name, polygon, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (camera, name, json.dumps(polygon), 1 if enabled else 0, time.time()),
        )
        row = conn.execute("SELECT * FROM rois WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return _roi_to_dict(row)


def update_roi(db_path: Path, roi_id: int, fields: dict) -> dict | None:
    sets, args = [], []
    if "name" in fields:
        sets.append("name = ?")
        args.append(str(fields["name"]))
    if "polygon" in fields:
        sets.append("polygon = ?")
        args.append(json.dumps(fields["polygon"]))
    if "enabled" in fields:
        sets.append("enabled = ?")
        args.append(1 if fields["enabled"] else 0)
    if not sets:
        return get_roi(db_path, roi_id)
    args.append(roi_id)
    conn = init_config_db(db_path)
    try:
        conn.execute(f"UPDATE rois SET {', '.join(sets)} WHERE id = ?", args)
        row = conn.execute("SELECT * FROM rois WHERE id = ?", (roi_id,)).fetchone()
    finally:
        conn.close()
    return _roi_to_dict(row) if row else None


def get_roi(db_path: Path, roi_id: int) -> dict | None:
    conn = init_config_db(db_path)
    try:
        row = conn.execute("SELECT * FROM rois WHERE id = ?", (roi_id,)).fetchone()
    finally:
        conn.close()
    return _roi_to_dict(row) if row else None


def delete_roi(db_path: Path, roi_id: int) -> bool:
    """Delete an ROI and any alert rules that referenced it (no FK
    enforcement in sqlite by default, so cascade by hand)."""
    conn = init_config_db(db_path)
    try:
        conn.execute("DELETE FROM alert_rules WHERE roi_id = ?", (roi_id,))
        cur = conn.execute("DELETE FROM rois WHERE id = ?", (roi_id,))
    finally:
        conn.close()
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# alert-rule CRUD

def list_rules(db_path: Path, camera: str | None = None) -> list[dict]:
    conn = init_config_db(db_path)
    try:
        if camera is not None:
            rows = conn.execute(
                "SELECT * FROM alert_rules WHERE camera = ? ORDER BY id", (camera,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM alert_rules ORDER BY id").fetchall()
    finally:
        conn.close()
    return [_rule_to_dict(r) for r in rows]


def create_rule(
    db_path: Path,
    camera: str,
    roi_id: int,
    cls: str,
    min_conf: float = 0.0,
    cooldown_s: int = 60,
    enabled: bool = True,
) -> dict:
    conn = init_config_db(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO alert_rules (camera, roi_id, class, min_conf, cooldown_s, "
            "enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (camera, roi_id, cls, min_conf, cooldown_s, 1 if enabled else 0, time.time()),
        )
        row = conn.execute(
            "SELECT * FROM alert_rules WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    finally:
        conn.close()
    return _rule_to_dict(row)


def update_rule(db_path: Path, rule_id: int, fields: dict) -> dict | None:
    col_map = {
        "class": "class",
        "min_conf": "min_conf",
        "cooldown_s": "cooldown_s",
        "enabled": "enabled",
        "roi_id": "roi_id",
    }
    sets, args = [], []
    for key, col in col_map.items():
        if key not in fields:
            continue
        val = fields[key]
        if key == "enabled":
            val = 1 if val else 0
        sets.append(f"{col} = ?")
        args.append(val)
    conn = init_config_db(db_path)
    try:
        if sets:
            args.append(rule_id)
            conn.execute(f"UPDATE alert_rules SET {', '.join(sets)} WHERE id = ?", args)
        row = conn.execute("SELECT * FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
    finally:
        conn.close()
    return _rule_to_dict(row) if row else None


def delete_rule(db_path: Path, rule_id: int) -> bool:
    conn = init_config_db(db_path)
    try:
        cur = conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
    finally:
        conn.close()
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# devices (used by the FCM notifier)

def upsert_device(
    db_path: Path, token: str, platform: str | None, user_email: str | None
) -> None:
    conn = init_config_db(db_path)
    try:
        conn.execute(
            "INSERT INTO devices (token, platform, user_email, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(token) DO UPDATE SET platform = excluded.platform, "
            "user_email = excluded.user_email, updated_at = excluded.updated_at",
            (token, platform, user_email, time.time()),
        )
    finally:
        conn.close()


def delete_device(db_path: Path, token: str) -> bool:
    conn = init_config_db(db_path)
    try:
        cur = conn.execute("DELETE FROM devices WHERE token = ?", (token,))
    finally:
        conn.close()
    return cur.rowcount > 0


def list_devices(db_path: Path) -> list[dict]:
    conn = init_config_db(db_path)
    try:
        rows = conn.execute("SELECT token, platform, user_email FROM devices").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
