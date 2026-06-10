"""FCM push sender — delivers fired alerts to the mobile app.

Runs entirely off the recorder's hot path: ``enqueue()`` (called from a
recorder thread inside ``_fire_alert``) drops the alert on a queue and
returns immediately; a single worker thread drains the queue and POSTs
to FCM's HTTP v1 API. One sender covers both iOS and Android — FCM
relays to APNs for iOS, so the payload and code path are identical.

**Gated by credentials.** With no service-account JSON present (the
default — there's no Firebase project yet), ``start()`` logs once and the
notifier stays disabled; ``enqueue()`` becomes a no-op. Alerts still
persist to events.db and serve over ``/api/alerts``; only the push leg is
off. Drop a valid JSON in and restart belfry-inference to light it up.

Device tokens are registered by the app via the DVR's ``POST /api/devices``
and live in config.db; the worker reads them per send and prunes any FCM
reports as unregistered.
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger("belfry.inference.notify")

_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_QUEUE_MAX = 256
_HTTP_TIMEOUT = 10.0


class FcmNotifier:
    def __init__(self, config_db_path: Path, credentials_path: Path | None, project_id: str = "") -> None:
        self.config_db_path = config_db_path
        self.credentials_path = credentials_path
        self.project_id = project_id
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._creds = None
        self._auth_request = None
        self.enabled = False

    # -- lifecycle ------------------------------------------------------
    def _load_creds(self) -> bool:
        """Load the service-account creds. Returns True if usable. Shared by
        the long-lived worker (start) and the one-shot send_text path."""
        if self._creds is not None:
            return True
        if not self.credentials_path or not Path(self.credentials_path).is_file():
            logger.warning(
                "FCM not configured (no credentials at %s); alerts will persist "
                "and serve over /api/alerts but will NOT push to devices",
                self.credentials_path,
            )
            return False
        try:
            from google.oauth2 import service_account
            import google.auth.transport.requests as greq

            self._creds = service_account.Credentials.from_service_account_file(
                str(self.credentials_path), scopes=[_FCM_SCOPE]
            )
            self._auth_request = greq.Request()
            if not self.project_id:
                self.project_id = self._creds.project_id or ""
        except ImportError:
            logger.warning(
                "google-auth not installed in the inference venv; push disabled. "
                "Add it via scripts/install-inference.sh."
            )
            return False
        except Exception:
            logger.exception("FCM credentials failed to load; push disabled")
            return False
        if not self.project_id:
            logger.warning("FCM project id unknown (not in JSON, not in config); push disabled")
            return False
        return True

    def start(self) -> None:
        """Load credentials and spin up the worker. No-op (disabled) if
        credentials are absent or fail to load — push is best-effort."""
        if not self._load_creds():
            return
        self.enabled = True
        self._thread = threading.Thread(target=self._run, name="fcm-notify", daemon=True)
        self._thread.start()
        logger.info("FCM notifier started for project %s", self.project_id)

    def send_text(self, title: str, body: str, data: dict | None = None) -> int:
        """Synchronous one-shot broadcast to every registered device — for
        operational pings (e.g. a retrain finishing), not the alert hot path.
        Returns the number of devices messaged. Best-effort: returns 0 if push
        is unconfigured rather than raising."""
        if not self._load_creds():
            return 0
        tokens = self._device_tokens()
        if not tokens:
            logger.info("no registered devices; nothing to push")
            return 0
        access = self._access_token()
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        sent = 0
        for tok in tokens:
            msg = {"message": {"token": tok, "notification": {"title": title, "body": body}}}
            if data:
                msg["message"]["data"] = {k: str(v) for k, v in data.items()}
            try:
                r = requests.post(url, headers=headers, data=json.dumps(msg), timeout=_HTTP_TIMEOUT)
            except requests.RequestException as e:
                logger.warning("FCM POST error: %s", e)
                continue
            if r.status_code == 200:
                sent += 1
            elif r.status_code == 404 or "UNREGISTERED" in (r.text or ""):
                self._prune_token(tok)
        return sent

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)  # unblock the worker's get()
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def enqueue(self, alert: dict) -> None:
        """Called from a recorder thread. Cheap; drops on overflow rather
        than back-pressuring the inference loop."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(alert)
        except queue.Full:
            logger.warning("FCM queue full; dropping alert id=%s", alert.get("id"))

    # -- worker ---------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            alert = self._queue.get()
            if alert is None:
                continue
            try:
                self._send(alert)
            except Exception:
                logger.exception("FCM send failed for alert id=%s", alert.get("id"))

    def _send(self, alert: dict) -> None:
        tokens = self._device_tokens()
        if not tokens:
            return
        access = self._access_token()
        url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
        headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}

        when = datetime.fromtimestamp(alert["ts"]).strftime("%H:%M:%S")
        title = f"{alert['class']} in {alert['roi_name']}"
        body = f"{alert['camera']} · {when}"
        # Both blocks on purpose: `notification` makes iOS show a banner
        # when backgrounded; `data` carries the deep-link payload. All
        # data values must be strings.
        data = {
            "alert_id": str(alert.get("id", "")),
            "cam": str(alert["camera"]),
            "roi": str(alert["roi_name"]),
            "class": str(alert["class"]),
            "ts": str(alert["ts"]),
        }
        # Collapse tray notifications per-camera via an Android `tag`: a new
        # alert for the same camera replaces the prior one in the drawer
        # rather than stacking. Without this, undismissed alerts accumulate
        # and hit Android's hard 50-notifications-per-app cap, after which the
        # system silently drops every further push until the user clears the
        # tray. Per-camera (not a single global tag) keeps up to ~8 distinct
        # nudges visible; the Alerts tab holds the full history regardless.
        for tok in tokens:
            msg = {
                "message": {
                    "token": tok,
                    "notification": {"title": title, "body": body},
                    "data": data,
                    "android": {"notification": {"tag": f"belfry_alert_{alert['camera']}"}},
                }
            }
            try:
                r = requests.post(url, headers=headers, data=json.dumps(msg), timeout=_HTTP_TIMEOUT)
            except requests.RequestException as e:
                logger.warning("FCM POST error: %s", e)
                continue
            if r.status_code == 200:
                continue
            # 404 NOT_FOUND or 400 UNREGISTERED → token is dead; prune it.
            text = r.text or ""
            if r.status_code == 404 or "UNREGISTERED" in text or "registration-token-not-registered" in text:
                self._prune_token(tok)
                logger.info("pruned stale FCM token (HTTP %d)", r.status_code)
            else:
                logger.warning("FCM send HTTP %d: %s", r.status_code, text[:200])

    def _access_token(self) -> str:
        if not self._creds.valid:
            self._creds.refresh(self._auth_request)
        return self._creds.token

    # -- config.db (read tokens / prune) --------------------------------
    def _device_tokens(self) -> list[str]:
        try:
            conn = sqlite3.connect(f"file:{self.config_db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return []
        try:
            return [r[0] for r in conn.execute("SELECT token FROM devices")]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def _prune_token(self, token: str) -> None:
        try:
            conn = sqlite3.connect(str(self.config_db_path))
            try:
                conn.execute("DELETE FROM devices WHERE token = ?", (token,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.warning("could not prune token: %s", e)


def _main() -> int:
    """One-shot CLI: broadcast a plain notification to all devices, reading
    the same FCM config as the inference runner from cameras.yaml. Used by
    scripts/runpod-auto.sh to ping when an unattended retrain finishes/fails.
    Always exits 0 — a push failure must never fail the caller."""
    import argparse

    from dvr.config import load_config

    ap = argparse.ArgumentParser(description="broadcast an FCM notification to all devices")
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--config", default="cameras.yaml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        cfg = load_config(Path(args.config))
        n = FcmNotifier(
            cfg.inference.config_db_path,
            cfg.notify.fcm_credentials_path,
            cfg.notify.fcm_project_id,
        )
        # data.type lets the app skip alert deep-linking for system pings.
        sent = n.send_text(args.title, args.body, data={"type": "system"})
        logger.info("pushed to %d device(s)", sent)
    except Exception:
        logger.exception("one-shot push failed (ignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
