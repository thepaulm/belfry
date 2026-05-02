"use strict";

// Window length per /get request. Short enough to reload quickly when
// scrubbing, long enough to watch a few minutes without re-requesting.
const WINDOW_S = 300;
const DAY_OPTIONS = 14;
// Snap to live when the scrubber lands within this many seconds of now;
// past that, the recording lags real-time anyway and what the user wants
// is the live HLS feed.
const LIVE_SNAP_S = 30;

const m = window.location.pathname.match(/^\/sets\/([^/]+)\/([^/]+)\/playback/);
const SET_ID = m ? m[1] : null;
const CAM = m ? m[2] : null;

const statusPill = document.getElementById("status");
const player = document.getElementById("player");
const dayPicker = document.getElementById("day-picker");
const scrubber = document.getElementById("scrubber");
const availability = document.getElementById("availability");
const cursor = document.getElementById("cursor");
const windowLabel = document.getElementById("window-label");
const backLink = document.getElementById("back-to-set");

if (SET_ID && CAM) {
  backLink.href = `/sets/${encodeURIComponent(SET_ID)}`;
}

let availableRanges = [];
let scrubDebounce = null;
let mode = "past"; // "past" | "live"
let liveHls = null;

// Start of the local day (00:00:00 local). The user thinks in local time;
// we translate to UTC ISO only when constructing the MediaMTX API request.
function startOfLocalDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function fmtDayLabel(d) {
  return d.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

function fmtClock(secOfDay) {
  const h = String(Math.floor(secOfDay / 3600)).padStart(2, "0");
  const m = String(Math.floor((secOfDay % 3600) / 60)).padStart(2, "0");
  const s = String(Math.floor(secOfDay % 60)).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

function populateDayPicker() {
  const today = startOfLocalDay(new Date());
  for (let i = 0; i < DAY_OPTIONS; i++) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    const opt = document.createElement("option");
    // Store the local-midnight epoch ms as the option value.
    opt.value = String(d.getTime());
    opt.textContent = (i === 0 ? "today · " : i === 1 ? "yesterday · " : "")
      + fmtDayLabel(d);
    dayPicker.appendChild(opt);
  }
}

async function refreshAvailability() {
  availability.innerHTML = "";
  try {
    const r = await fetch(
      `/api/playback/list?cam=${encodeURIComponent(CAM)}`,
      { credentials: "same-origin" },
    );
    if (!r.ok) throw new Error(`${r.status}`);
    availableRanges = await r.json();
  } catch (e) {
    statusPill.textContent = `availability failed: ${e.message}`;
    return;
  }
  renderAvailabilityBar();
  statusPill.textContent = `${availableRanges.length} segment${
    availableRanges.length === 1 ? "" : "s"
  }`;
}

function selectedDayStart() {
  return new Date(parseInt(dayPicker.value, 10));
}

function isTodaySelected() {
  const today = startOfLocalDay(new Date());
  return selectedDayStart().getTime() === today.getTime();
}

function liveEdgeOfSelectedDay() {
  // Seconds-of-day for the live edge if today is picked; null otherwise.
  if (!isTodaySelected()) return null;
  const now = new Date();
  return Math.floor((now - startOfLocalDay(now)) / 1000);
}

function applyScrubberMax() {
  // On today, clamp to "now" so the user can't drag past the live edge.
  // On other days, the full 24h is available.
  const live = liveEdgeOfSelectedDay();
  scrubber.max = String(live !== null ? live : 86399);
  if (parseInt(scrubber.value, 10) > parseInt(scrubber.max, 10)) {
    scrubber.value = scrubber.max;
  }
  updateCursor();
}

function scrubMaxSec() {
  return parseInt(scrubber.max, 10) || 86399;
}

function renderAvailabilityBar() {
  availability.innerHTML = "";
  // The scrubber and the availability bar must share a denominator so the
  // blue blocks line up with the cursor. On today, scrubber.max is clamped
  // to "now" (e.g. 40 min into the day), so a 30-min recording covers most
  // of the timeline rather than appearing as a 2% sliver.
  const max = scrubMaxSec();
  const dayStart = selectedDayStart();
  const visibleEnd = new Date(dayStart.getTime() + max * 1000);

  for (const r of availableRanges) {
    const start = new Date(r.start);
    const end = new Date(start.getTime() + r.duration * 1000);
    if (end <= dayStart || start >= visibleEnd) continue;

    const clampedStart = Math.max(start - dayStart, 0) / 1000;
    const clampedEnd = Math.min(end - dayStart, max * 1000) / 1000;
    const left = (clampedStart / max) * 100;
    const width = ((clampedEnd - clampedStart) / max) * 100;

    const span = document.createElement("span");
    span.className = "avail-block";
    span.style.left = `${left}%`;
    span.style.width = `${width}%`;
    span.title = `${start.toLocaleString()} (${Math.round(r.duration)}s)`;
    availability.appendChild(span);
  }
}

function renderTicks() {
  const ticksEl = document.querySelector(".timeline .ticks");
  if (!ticksEl) return;
  const max = scrubMaxSec();
  ticksEl.innerHTML = "";
  for (let i = 0; i <= 4; i++) {
    const sec = Math.round((max * i) / 4);
    const span = document.createElement("span");
    span.textContent = fmtClock(sec);
    ticksEl.appendChild(span);
  }
}

function tearDownLive() {
  if (liveHls) {
    liveHls.destroy();
    liveHls = null;
  }
}

function enterLiveMode() {
  mode = "live";
  // Pin the scrubber to the live edge so the cursor + thumb track real time.
  // applyScrubberMax may have just bumped scrubber.max forward; reset value
  // explicitly so both thumb and cursor land on the new edge.
  applyScrubberMax();
  const live = liveEdgeOfSelectedDay();
  if (live !== null) {
    scrubber.value = String(live);
    requestAnimationFrame(updateCursor);
  }
  windowLabel.textContent = "LIVE";

  const hlsUrl = `/hls/${encodeURIComponent(CAM)}/index.m3u8`;
  tearDownLive();
  if (player.canPlayType("application/vnd.apple.mpegurl")) {
    player.src = hlsUrl;
    player.play().catch(() => {});
  } else if (window.Hls && Hls.isSupported()) {
    const hls = new Hls({
      lowLatencyMode: true,
      liveSyncDurationCount: 2,
      maxLiveSyncPlaybackRate: 1.2,
      manifestLoadingMaxRetry: 8,
      manifestLoadingRetryDelay: 500,
      manifestLoadingMaxRetryTimeout: 4000,
      levelLoadingMaxRetry: 6,
      levelLoadingRetryDelay: 500,
    });
    hls.loadSource(hlsUrl);
    hls.attachMedia(player);
    liveHls = hls;
    player.play().catch(() => {});
  } else {
    windowLabel.textContent = "LIVE — browser cannot play HLS";
  }
}

function loadWindow() {
  applyScrubberMax(); // refresh the live edge in case time has passed
  const offset = parseInt(scrubber.value, 10);
  const live = liveEdgeOfSelectedDay();
  if (live !== null && offset >= live - LIVE_SNAP_S) {
    enterLiveMode();
    return;
  }

  mode = "past";
  tearDownLive();
  const dayStart = selectedDayStart();
  // dayStart is local-midnight; adding offset seconds gives a Date at the
  // user's chosen local moment, and toISOString converts to UTC for the API.
  const target = new Date(dayStart.getTime() + offset * 1000);
  const isoStart = target.toISOString();
  const url = `/playback/get?path=${encodeURIComponent(CAM)}`
    + `&start=${encodeURIComponent(isoStart)}`
    + `&duration=${WINDOW_S}s`
    + `&format=mp4`;
  player.src = url;
  player.play().catch(() => {});
  windowLabel.textContent = `${fmtClock(offset)} local · ${WINDOW_S / 60}m window`;
}

// Approximate range-input thumb radius. Browsers inset the thumb by this
// much from each end of the track so the circle is fully visible at the
// extremes, and we have to match the inset or the cursor line drifts off
// the thumb's center (most visible at value == max — looks like the line
// doesn't move when we snap to live).
const SCRUB_THUMB_RADIUS_PX = 8;

function updateCursor() {
  const max = parseInt(scrubber.max, 10) || 86399;
  const val = parseInt(scrubber.value, 10);
  const pct = max > 0 ? val / max : 0;
  const w = scrubber.offsetWidth;
  if (!w) {
    cursor.style.left = `${pct * 100}%`;
    return;
  }
  const x = SCRUB_THUMB_RADIUS_PX + pct * (w - 2 * SCRUB_THUMB_RADIUS_PX);
  cursor.style.left = `${x}px`;
}

function onScrub() {
  const offset = parseInt(scrubber.value, 10);
  windowLabel.textContent = `${fmtClock(offset)} local · drag and release to load`;
  updateCursor();
  if (scrubDebounce) clearTimeout(scrubDebounce);
  scrubDebounce = setTimeout(loadWindow, 250);
}

function init() {
  if (!SET_ID || !CAM) {
    statusPill.textContent = "bad URL";
    return;
  }
  populateDayPicker();
  applyScrubberMax();
  renderTicks();
  dayPicker.addEventListener("change", () => {
    applyScrubberMax();
    renderTicks();
    renderAvailabilityBar();
    loadWindow();
  });
  scrubber.addEventListener("input", onScrub);
  scrubber.addEventListener("change", loadWindow);
  // Keep the live edge moving on today: every 5s, advance scrubber.max.
  // If we're in live mode, also slide the pinned thumb forward.
  setInterval(() => {
    const wasAtMax = parseInt(scrubber.value, 10) === parseInt(scrubber.max, 10);
    applyScrubberMax();
    renderTicks();
    renderAvailabilityBar();
    if (mode === "live" || wasAtMax) {
      scrubber.value = scrubber.max;
      updateCursor();
    }
  }, 5000);
  // Re-poll /list so the availability bar grows with recording in real
  // time. MediaMTX's /list reflects on-disk segments, including the
  // currently-being-written one with its duration-so-far.
  setInterval(refreshAvailability, 15000);
  updateCursor();
  refreshAvailability();
}

init();
