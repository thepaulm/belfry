"use strict";

// Browser alert watcher (foreground-tab path). Polls /api/alerts and
// surfaces new alerts as OS notifications + in-page toasts while a belfry
// tab is open — no service worker / push infra. The mobile app gets the
// same alerts via FCM; this is for testing + desktop/LAN use.
//
// Gated behind a "🔔 Alerts" header toggle: the first enable is a user
// gesture (needed to request Notification permission). State persists in
// localStorage and auto-resumes if permission is already granted. Polling
// is plain fetch, so unlike the SSE label overlay there's no connection
// storm to worry about.

const ALERTS_POLL_MS = 4000;
// On enable, surface alerts from the last N seconds so flipping the toggle
// a beat after something happened still shows it (older alerts stay history).
const ALERTS_ENABLE_LOOKBACK_S = 30;

let _lastId = null;   // highest alert id already surfaced (null = need baseline)
let _timer = null;
let _started = false;

async function _fetchAlerts(qs) {
  const r = await fetch(`/api/alerts${qs}`, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`/api/alerts ${r.status}`);
  return r.json();
}

function _playbackUrl(a) {
  if (!a.set_id || !a.camera) return null;
  return `/sets/${encodeURIComponent(a.set_id)}/${encodeURIComponent(a.camera)}/playback?ts=${a.ts}`;
}

function _surface(a) {
  const title = `${a.class} in ${a.roi_name}`;
  const body = `${a.camera} · ${new Date(a.ts * 1000).toLocaleTimeString()}`;
  const url = _playbackUrl(a);
  if (window.Notification && Notification.permission === "granted") {
    try {
      const n = new Notification(title, {
        body,
        icon: a.thumb_url || undefined,
        tag: `belfry-alert-${a.id}`,
      });
      if (url) n.onclick = () => { window.focus(); window.location.href = url; };
    } catch { /* some browsers throw if constructed off a gesture; toast still shows */ }
  }
  _toast(title, body, a.thumb_url, url);
}

function _toast(title, body, thumb, url) {
  let host = document.getElementById("alert-toasts");
  if (!host) {
    host = document.createElement("div");
    host.id = "alert-toasts";
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = "alert-toast";
  const img = thumb ? `<img class="alert-toast-img" src="${thumb}" alt="">` : "";
  el.innerHTML =
    `${img}<div class="alert-toast-text"><strong></strong><span></span></div>`;
  el.querySelector("strong").textContent = title;
  el.querySelector("span").textContent = body;
  if (url) {
    el.style.cursor = "pointer";
    el.addEventListener("click", () => { window.location.href = url; });
  }
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 400);
  }, 8000);
}

async function _poll() {
  let rows;
  try {
    rows = await _fetchAlerts("?limit=20");
  } catch {
    return;  // transient; next tick retries
  }
  if (_lastId === null) {
    // First poll after enabling: surface only very-recent alerts (so
    // enabling right after an event still shows it) but don't replay old
    // history. Then set the baseline to the newest id.
    const cutoff = Date.now() / 1000 - ALERTS_ENABLE_LOOKBACK_S;
    const recent = rows.filter((a) => a.ts >= cutoff).sort((x, y) => x.id - y.id);
    for (const a of recent) _surface(a);
    _lastId = rows.length ? rows[0].id : 0;
    return;
  }
  const fresh = rows.filter((a) => a.id > _lastId).sort((x, y) => x.id - y.id);
  for (const a of fresh) _surface(a);
  if (rows.length) _lastId = Math.max(_lastId, rows[0].id);
}

function startAlerts() {
  if (_started) return;
  _started = true;
  _lastId = null;
  _poll();
  _timer = setInterval(_poll, ALERTS_POLL_MS);
}

function stopAlerts() {
  _started = false;
  if (_timer) { clearInterval(_timer); _timer = null; }
}

function wireAlertsToggle() {
  const btn = document.getElementById("alerts-toggle");
  if (!btn) return;
  const setUI = (on) => {
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-pressed", String(on));
  };
  const permGranted = window.Notification && Notification.permission === "granted";
  if (localStorage.getItem("belfry-alerts") === "on" && permGranted) {
    setUI(true);
    startAlerts();
  } else {
    setUI(false);
  }
  btn.addEventListener("click", async () => {
    if (_started) {
      stopAlerts();
      localStorage.setItem("belfry-alerts", "off");
      setUI(false);
      return;
    }
    if (window.Notification && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch { /* ignore */ }
    }
    localStorage.setItem("belfry-alerts", "on");
    setUI(true);
    startAlerts();
  });
}

window.belfryAlerts = { start: startAlerts, stop: stopAlerts };
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", wireAlertsToggle);
} else {
  wireAlertsToggle();
}
