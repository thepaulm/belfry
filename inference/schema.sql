-- Belfry events table — written by inference/recorder.py, read by dvr/server.py.
-- Single-writer / many-reader; SQLite WAL mode is set on connection.

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    camera          TEXT NOT NULL,
    class           TEXT NOT NULL,
    ts_start        REAL NOT NULL,         -- unix epoch seconds
    ts_end          REAL NOT NULL,         -- last sample within cooldown
    max_conf        REAL NOT NULL,
    peak_bbox       TEXT NOT NULL,         -- JSON [x1,y1,x2,y2] in 0..1
    thumb_path      TEXT,                  -- relative under recordings/thumbs/
    sample_count    INTEGER NOT NULL,
    staged_filename TEXT                   -- training jpg name once staged into the labeler
);

CREATE INDEX IF NOT EXISTS events_cam_ts ON events (camera, ts_start);
CREATE INDEX IF NOT EXISTS events_cls_ts ON events (class, ts_start);

-- Alerts — one row per ROI-rule firing. Event-like (swept with footage
-- by retention.py), so it lives here rather than in config.db. Written
-- by inference/recorder.py when a detection enters a region with a
-- matching alert rule; read by the DVR's /api/alerts and the FCM notifier.
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    rule_id     INTEGER NOT NULL,
    camera      TEXT NOT NULL,
    roi_id      INTEGER NOT NULL,
    roi_name    TEXT NOT NULL,
    class       TEXT NOT NULL,
    ts          REAL NOT NULL,          -- unix epoch seconds (fire time)
    conf        REAL NOT NULL,
    peak_bbox   TEXT NOT NULL,          -- JSON [x1,y1,x2,y2] in 0..1
    thumb_path  TEXT,                   -- relative under recordings/thumbs/
    pushed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_cam_ts ON alerts (camera, ts);
