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
