-- Belfry config DB — durable user config kept OUT of events.db so a
-- rebuild of derived event data (re-running inference / backfill) can
-- never wipe hand-drawn zones. Lives at recordings/config.db (WAL).
--
-- Written by the DVR (the /rois editor + REST API + device registration);
-- read by the inference recorder (ROI-membership evaluation) and the FCM
-- notifier (device tokens). All `CREATE … IF NOT EXISTS` so both processes
-- can run it on first open, same as inference/schema.sql.

-- Named polygonal regions of interest, per camera.
CREATE TABLE IF NOT EXISTS rois (
    id          INTEGER PRIMARY KEY,
    camera      TEXT NOT NULL,
    name        TEXT NOT NULL,
    polygon     TEXT NOT NULL,           -- JSON [[x,y],...] normalized 0..1
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rois_cam ON rois (camera);

-- Alert rules: fire when `class` is detected inside `roi_id`. `class`
-- keys on the EMITTED class name (e.g. `vehicle`, not car/truck) so it
-- matches what the recorder publishes and what /events filters on.
CREATE TABLE IF NOT EXISTS alert_rules (
    id          INTEGER PRIMARY KEY,
    camera      TEXT NOT NULL,
    roi_id      INTEGER NOT NULL,
    class       TEXT NOT NULL,
    min_conf    REAL NOT NULL DEFAULT 0,   -- 0 = use the detector's own threshold
    cooldown_s  INTEGER NOT NULL DEFAULT 60, -- min seconds between alerts for this rule
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_rules_cam ON alert_rules (camera);

-- Registered mobile push targets (FCM tokens). `token` UNIQUE so the
-- app re-registering the same install upserts rather than duplicating.
CREATE TABLE IF NOT EXISTS devices (
    id          INTEGER PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    platform    TEXT,                      -- 'ios' | 'android' | null
    user_email  TEXT,
    updated_at  REAL NOT NULL
);
