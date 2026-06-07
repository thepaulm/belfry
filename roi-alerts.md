# ROI alerts

Per-camera **polygonal regions of interest** plus **alert rules** (a class detected inside a named region) that fire **alerts**. Alerts persist to `events.db`, serve over `/api/alerts`, and push to the Flutter mobile app (iOS + Android) via Firebase Cloud Messaging. Regions and rules are drawn/configured in a browser editor at `/rois`.

Built in three slices; all backend + the browser editor are live. Mobile push is wired but **dormant until Firebase credentials are configured** — alerts still persist and serve without it.

## Data model

Two SQLite DBs, split so a rebuild of derived event data never wipes hand-drawn zones:

- **`recordings/config.db`** (WAL, durable, never swept) — shared DDL in `inference/config_schema.sql`. Path overridable via `inference.config_db_path` in `cameras.yaml` (default `recordings/config.db`).
  - `rois(id, camera, name, polygon, enabled, created_at)` — `polygon` = JSON `[[x,y],…]` normalized 0..1.
  - `alert_rules(id, camera, roi_id, class, min_conf, cooldown_s, enabled, created_at)` — `class` keys on the **emitted** class name (`vehicle`, not car/truck — matches what the recorder publishes and what `/events` filters on). `min_conf` 0 = use the detector's own threshold.
  - `devices(id, token, platform, user_email, updated_at)` — FCM tokens; `token` UNIQUE (re-register upserts).
- **`recordings/events.db`** — `alerts(id, rule_id, camera, roi_id, roi_name, class, ts, conf, peak_bbox, thumb_path, pushed)`. Event-like, so it lives here and is swept with footage by `retention.py`, mirroring the events sweep.

The DVR owns all writes to config.db (the `/rois` editor + REST API + device registration). The inference recorder reads it. WAL handles the multi-process read/write.

## Geometry

Polygons and detection boxes are both normalized 0..1.

- `point_in_polygon(pt, poly)` — even-odd ray cast.
- `test_point(bbox, cls)` — the single point tested against a polygon: a person's **feet** (bottom-center `(cx, y2)`, where they actually stand) vs the box **centroid** for everything else. Feet-for-person avoids false hits when a tall box overlaps a low zone but the subject is standing elsewhere.

These two helpers are **duplicated** in `dvr/zones.py` (py3.14) and `inference/zones.py` (cp310) rather than imported — the DVR and inference run in separate venvs. ~15 lines of pure stdlib; keep them in sync.

## Live evaluation (recorder)

- `inference/zones.py:ZoneIndex` caches one camera's enabled ROIs + rules from config.db, refreshed every ~15 s on a monotonic timer (tiny tables, edits rare → negligible at 1 fps).
- In `EventRecorder._update_runs`, every frame's best detection per class is tested via `ZoneIndex.matches(cls, conf, bbox)`. A match fires when class + region-membership + `conf ≥ min_conf` all hold.
- **Dedup**: one alert per event-*run* — `_Run.alerted_rule_ids` tracks rules already fired during the run (cleared on close). A **per-rule cooldown** (`recorder._rule_last_fired`, `cooldown_s`) spans run boundaries so a subject pacing in/out of a zone doesn't re-alert. Default cooldown 60 s (`alerts.default_cooldown_s`, overridable per rule).
- Fires at run **entry** (not close), so push latency is ~1–2 s rather than ≥ `cooldown_s`.
- On fire: write an `alerts` row, save a fire-time thumbnail (the class bbox + the ROI outline in yellow baked in, under `thumbs/<cam>/alerts/<day>/<ts>_<cls>.jpg`), and enqueue a push job (no-op if push is off).
- Deep-link uses `cam`+`ts` (same as `/events` cards → `/sets/<set>/<cam>/playback?ts=`), so no `event_id` backfill is needed.

## API

- `GET /api/alert-classes` — emitted class names a rule may target.
- `GET/POST/PUT/DELETE /api/rois` (filter by `cam`).
- `GET/POST/PUT/DELETE /api/alert-rules`.
- `GET /api/alerts?cam=&since=&before_id=&limit=` (cursor-paginated, newest first; mirrors `/api/events`) + `GET /api/alerts/thumb/{id}` (immutable long-cache, path-traversal-checked).
- `POST /api/devices` `{token, platform}` / `DELETE /api/devices/{token}` — **bearer-authed** (the app's existing server JWT); email comes from the verified token, not the body.

## Browser editor — `/rois`

Vanilla JS + `<canvas>` over a live HLS frame (`dvr/static/rois.html` + `rois.js`, reusing the `overlay.js` normalized-coords/`.video-wrap` pattern). Pick a camera, **Freeze frame**, click to drop polygon vertices (Backspace removes the last, Enter/double-click finishes), name + Save. Lists existing regions with show/hide-on-canvas, enable toggle, delete. A rules panel adds class-in-region rules (class + region + min-conf + cooldown). Linked from the Cameras and Events page headers.

## Mobile push (FCM) — gated

`inference/notify.py:FcmNotifier` is a worker-thread sender off the recorder's hot path: `enqueue()` drops an alert on a queue and returns; the worker POSTs to FCM HTTP v1 (`messages:send`). **One code path covers iOS and Android** — FCM relays to APNs for iOS. Each message carries **both** a `notification` block (title/body — required for the iOS banner when backgrounded) and a `data` block (`{alert_id, cam, roi, class, ts}` for deep-linking). Stale tokens (FCM 404 / UNREGISTERED) are pruned.

**Gating**: with no service-account JSON configured, `start()` logs once and the notifier stays disabled; `enqueue()` no-ops. Alerts still persist + serve over `/api/alerts`. Token minting uses `google-auth` (added to `scripts/install-inference.sh`; the notifier disables gracefully if it's absent). The HTTP POST uses `requests` (already an ultralytics dep).

**Config** — top-level `alerts:` block in `cameras.yaml` → `Notify` dataclass:
```yaml
alerts:
  # fcm_credentials_path: /etc/belfry/fcm-service-account.json
  # fcm_project_id: ""        # optional; read from the JSON if omitted
  default_cooldown_s: 60
```

**External setup (none of this is in the repo):**
1. Create a Firebase project, enable Cloud Messaging.
2. For iOS: generate an APNs auth key (`.p8`) in the Apple Developer account and upload it to Firebase so FCM can relay to APNs. Android needs nothing extra.
3. Download a service-account JSON, drop it on the Orin (gitignored, mode 0600, like `cameras.yaml`), point `alerts.fcm_credentials_path` at it.
4. Restart `belfry-inference`. Push lights up — no code change.

**Flutter app** (built on a separate machine): register its FCM token via `POST /api/devices`; read history via `GET /api/alerts`; deep-link from the `data` payload.

## Deferred — live SSE (`/api/alerts/stream`)

Not built. The mobile flow is fully covered by FCM push (live nudge, app-closed) + the pull API (history). SSE would add **instant delivery to an already-open client without FCM**, reusing the `LiveBroadcaster` + `/api/inference/live` SSE-proxy pattern. Add it when one of these shows up:
- A **browser alerts dashboard** — the web side has no push at all (FCM only reaches the mobile app). Most likely first trigger.
- **Skipping Firebase entirely** — SSE becomes the live channel with zero push infrastructure.
- A **live-ops console** where sub-second, reliable, on-connection delivery beats FCM's few-second best-effort latency.
- Foregrounded-app freshness without polling.

## Files

- `dvr/config.py` — `config_db_path` on `Inference`; new `Notify` dataclass + `alerts:` loader.
- `dvr/zones.py` — config.db CRUD + geometry + validation.
- `dvr/server.py` — ROI/rule/alerts/devices routes; `/rois` page; `_emitted_classes`.
- `dvr/retention.py` — `_sweep_alerts`.
- `dvr/static/rois.html`, `rois.js`, `viewer.css` (editor styles); nav links in `index.html`, `events.html`.
- `inference/config_schema.sql` — config.db DDL; `inference/schema.sql` — `alerts` table.
- `inference/zones.py` — `ZoneIndex` + geometry.
- `inference/recorder.py` — alert evaluation/fire/thumbnail.
- `inference/notify.py` — FCM sender.
- `inference/runner.py` — wires `ZoneIndex` + notifier per recorder.
- `scripts/install-inference.sh` — `google-auth`. `cameras.example.yaml` — `alerts:` + `config_db_path` docs.
