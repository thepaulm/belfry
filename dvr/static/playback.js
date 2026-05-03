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
const eventPipsEl = document.getElementById("event-pips");
const eventLegendEl = document.getElementById("event-legend");
const cursor = document.getElementById("cursor");
const windowLabel = document.getElementById("window-label");
const backLink = document.getElementById("back-to-set");
const goLiveBtn = document.getElementById("go-live");
const prevEventBtn = document.getElementById("prev-event");
const nextEventBtn = document.getElementById("next-event");

if (SET_ID && CAM) {
  backLink.href = `/sets/${encodeURIComponent(SET_ID)}`;
}

let availableRanges = [];
let dayEvents = [];          // events with ts_start inside the selected day
let scrubDebounce = null;
let mode = "past"; // "past" | "live"
let liveHls = null;
let liveOverlay = null;

// Coarse class → pip color. Two reasons we collapse the rainbow into
// three buckets: (1) keeps the legend short (person / animal / vehicle),
// (2) the COCO subclasses dog / cat / bird don't add visual signal at
// pip-width — a single green is enough.
const CLASS_COLOR = {
  person:  "#4ea1ff",
  animal:  "#5ad17c",
  dog:     "#5ad17c",
  cat:     "#5ad17c",
  bird:    "#5ad17c",
  vehicle: "#ff9b3f",
  car:     "#ff9b3f",
  truck:   "#ff9b3f",
};
const CLASS_COLOR_DEFAULT = "#aaa";

function classColor(cls) {
  return CLASS_COLOR[cls] || CLASS_COLOR_DEFAULT;
}

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
    dayPicker.appendChild(opt);
  }
  relabelDayPicker();
}

function daysWithData() {
  const out = new Set();
  for (const r of availableRanges) {
    const start = new Date(r.start);
    const end = new Date(start.getTime() + r.duration * 1000);
    const cursor = startOfLocalDay(start);
    while (cursor.getTime() <= end.getTime()) {
      out.add(cursor.getTime());
      cursor.setDate(cursor.getDate() + 1);
    }
  }
  return out;
}

function relabelDayPicker() {
  const todayMs = startOfLocalDay(new Date()).getTime();
  const yesterdayMs = todayMs - 86400_000;
  const days = daysWithData();
  for (const opt of dayPicker.options) {
    const dayMs = parseInt(opt.value, 10);
    const marker = days.has(dayMs) ? "● " : "○ ";
    const prefix = dayMs === todayMs
      ? "today · "
      : dayMs === yesterdayMs
        ? "yesterday · "
        : "";
    opt.textContent = marker + prefix + fmtDayLabel(new Date(dayMs));
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
  relabelDayPicker();
  statusPill.textContent = `${availableRanges.length} segment${
    availableRanges.length === 1 ? "" : "s"
  }`;
  // Events tag along on the same refresh cadence — kept in lockstep
  // with the availability bar so the timeline is consistent.
  refreshDayEvents();
}

async function refreshDayEvents() {
  // Fetch the events whose ts_start falls inside the selected day. The
  // endpoint accepts since/until in unix seconds. Pull a comfortable
  // limit — a full day of 8-cam activity is in the hundreds of rows
  // for this single camera, well under the API's 500 cap.
  const dayStart = selectedDayStart();
  const dayEndMs = dayStart.getTime() + 86400 * 1000;
  const params = new URLSearchParams();
  params.set("cam", CAM);
  params.set("since", String(dayStart.getTime() / 1000));
  params.set("until", String(dayEndMs / 1000));
  params.set("limit", "500");
  try {
    const r = await fetch(`/api/events?${params.toString()}`, { credentials: "same-origin" });
    if (!r.ok) throw new Error(`${r.status}`);
    dayEvents = await r.json();
  } catch {
    // Soft-fail: events are an enhancement, not load-bearing for playback.
    dayEvents = [];
  }
  renderEventPips();
  renderEventLegend();
}

function renderEventPips() {
  eventPipsEl.innerHTML = "";
  const max = scrubMaxSec();
  const dayStart = selectedDayStart();
  for (const ev of dayEvents) {
    // Position the pip at ts_start; stretch to ts_end so longer events
    // (someone lingering for 30s) get a proportionally wider mark.
    const startSec = (ev.ts_start - dayStart.getTime() / 1000);
    const endSec = (ev.ts_end - dayStart.getTime() / 1000);
    if (endSec < 0 || startSec > max) continue;
    const left = (Math.max(0, startSec) / max) * 100;
    const widthPct = ((Math.min(endSec, max) - Math.max(0, startSec)) / max) * 100;
    const pip = document.createElement("span");
    pip.className = "event-pip";
    pip.style.left = `${left}%`;
    // Floor the rendered width at a tiny minimum so a 0-second event
    // (sample_count=1) is still visible as a hairline mark.
    pip.style.width = `${Math.max(widthPct, 0.15)}%`;
    pip.style.background = classColor(ev.class);
    pip.dataset.ts = String(ev.ts_start);
    pip.title = `${ev.class} · ${new Date(ev.ts_start * 1000).toLocaleTimeString()}`
      + ` · conf ${ev.max_conf.toFixed(2)}`;
    pip.addEventListener("click", () => {
      seekToTimestamp(ev.ts_start);
    });
    eventPipsEl.appendChild(pip);
  }
}

function renderEventLegend() {
  // Show only the classes actually present in today's events to keep
  // the header tidy. Classes are bucketed by visual color above; the
  // legend uses the displayed bucket names.
  const buckets = new Set();
  for (const ev of dayEvents) {
    if (ev.class === "person")        buckets.add("person");
    else if (ev.class === "vehicle" || ev.class === "car" || ev.class === "truck") buckets.add("vehicle");
    else                              buckets.add("animal");
  }
  if (!buckets.size) {
    eventLegendEl.textContent = "";
    return;
  }
  eventLegendEl.innerHTML = "";
  const order = ["person", "animal", "vehicle"];
  for (const name of order) {
    if (!buckets.has(name)) continue;
    const dot = document.createElement("span");
    dot.className = "legend-dot";
    dot.style.background = classColor(name);
    eventLegendEl.appendChild(dot);
    const lbl = document.createElement("span");
    lbl.className = "legend-label";
    lbl.textContent = name;
    eventLegendEl.appendChild(lbl);
  }
  const total = document.createElement("span");
  total.className = "legend-count muted";
  total.textContent = `· ${dayEvents.length} event${dayEvents.length === 1 ? "" : "s"}`;
  eventLegendEl.appendChild(total);
}

async function gotoNeighborEvent(direction) {
  // direction: "prev" | "next". Use the current scrubber position
  // converted to unix epoch as the cursor. Server returns the
  // immediately-prior or immediately-following event for this camera.
  const offset = parseInt(scrubber.value, 10);
  const dayStart = selectedDayStart();
  const ts = dayStart.getTime() / 1000 + offset;
  try {
    const r = await fetch(
      `/api/events/neighbors?cam=${encodeURIComponent(CAM)}&ts=${ts}`,
      { credentials: "same-origin" },
    );
    if (!r.ok) return;
    const j = await r.json();
    const target = j[direction];
    if (target == null) {
      statusPill.textContent = `no ${direction === "prev" ? "earlier" : "later"} event`;
      return;
    }
    seekToTimestamp(target.ts_start);
  } catch (e) {
    statusPill.textContent = `neighbor lookup failed: ${e.message}`;
  }
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
  if (liveOverlay) {
    liveOverlay.destroy();
    liveOverlay = null;
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
  updateGoLiveBtn();

  const hlsUrl = `/hls/${encodeURIComponent(CAM)}/index.m3u8`;
  tearDownLive();
  // Live bounding-box overlay only attaches in live mode — past mp4
  // segments don't have a live SSE feed (and deferring overlays for
  // past playback to a future slice). Layered on the playback video's
  // wrapper so it scales with the player.
  if (window.BoxOverlay) {
    const wrap = document.querySelector(".playback-video-wrap");
    if (wrap) liveOverlay = new BoxOverlay(wrap, CAM);
  }
  if (player.canPlayType("application/vnd.apple.mpegurl")) {
    player.pause();
    player.src = hlsUrl;
    player.load();
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
  updateGoLiveBtn();
  const dayStart = selectedDayStart();
  // dayStart is local-midnight; adding offset seconds gives a Date at the
  // user's chosen local moment, and toISOString converts to UTC for the API.
  const target = new Date(dayStart.getTime() + offset * 1000);
  const isoStart = target.toISOString();
  // /api/playback/get proxies MediaMTX's /get and adds byte-range support
  // (iPad Safari refuses to play <video> sources without it).
  const url = `/api/playback/get?cam=${encodeURIComponent(CAM)}`
    + `&start=${encodeURIComponent(isoStart)}`
    + `&duration=${WINDOW_S}s`;
  // Safari (especially iPad) keeps the previous pipeline alive across a bare
  // src swap — pause + load forces it to actually pick up the new URL.
  player.pause();
  player.src = url;
  player.load();
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

function updateGoLiveBtn() {
  // Show "LIVE" with an active style while pinned to the live edge; otherwise
  // it's a normal "Go Live" affordance.
  const live = mode === "live";
  goLiveBtn.classList.toggle("active", live);
  goLiveBtn.textContent = live ? "● LIVE" : "● Go Live";
}

function goLive() {
  // Jump to today's live edge regardless of which day is currently selected.
  const todayMs = startOfLocalDay(new Date()).getTime();
  if (parseInt(dayPicker.value, 10) !== todayMs) {
    dayPicker.value = String(todayMs);
    applyScrubberMax();
    renderTicks();
    renderAvailabilityBar();
  }
  if (scrubDebounce) {
    clearTimeout(scrubDebounce);
    scrubDebounce = null;
  }
  const live = liveEdgeOfSelectedDay();
  if (live !== null) scrubber.value = String(live);
  loadWindow();
}

function onScrub() {
  const offset = parseInt(scrubber.value, 10);
  windowLabel.textContent = `${fmtClock(offset)} local · drag and release to load`;
  updateCursor();
  if (scrubDebounce) clearTimeout(scrubDebounce);
  scrubDebounce = setTimeout(loadWindow, 250);
}

function seekToTimestamp(ts) {
  // Move the day picker to ts's local day, then put the scrubber at
  // (ts - dayStart) seconds and call loadWindow(). Used when the page
  // is opened with ?ts=<unix epoch>, e.g. via a deep-link from /events.
  const target = new Date(ts * 1000);
  const dayMs = startOfLocalDay(target).getTime();
  if (![...dayPicker.options].some(o => parseInt(o.value, 10) === dayMs)) {
    // ts predates the day picker's range — fall back to live.
    goLive();
    return;
  }
  dayPicker.value = String(dayMs);
  applyScrubberMax();
  renderTicks();
  renderAvailabilityBar();
  const offset = Math.max(0, Math.floor(ts - dayMs / 1000));
  scrubber.value = String(Math.min(offset, parseInt(scrubber.max, 10)));
  loadWindow();
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
    refreshDayEvents();
    loadWindow();
  });
  scrubber.addEventListener("input", onScrub);
  scrubber.addEventListener("change", () => {
    // change fires on release; cancel any pending input-debounce so we don't
    // fire two identical loads (the server cache key would race on the partial
    // file).
    if (scrubDebounce) {
      clearTimeout(scrubDebounce);
      scrubDebounce = null;
    }
    loadWindow();
  });
  goLiveBtn.addEventListener("click", goLive);
  prevEventBtn.addEventListener("click", () => gotoNeighborEvent("prev"));
  nextEventBtn.addEventListener("click", () => gotoNeighborEvent("next"));
  // Keyboard shortcuts: [ = prev event, ] = next event. Skip when the
  // user is typing in an input (the day picker is the only one) so we
  // don't fight the browser's keyboard nav.
  window.addEventListener("keydown", (e) => {
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "SELECT")) return;
    if (e.key === "[") { e.preventDefault(); gotoNeighborEvent("prev"); }
    else if (e.key === "]") { e.preventDefault(); gotoNeighborEvent("next"); }
  });
  player.addEventListener("error", () => {
    const err = player.error;
    const code = err ? err.code : "?";
    const msg = err && err.message ? err.message : "media error";
    statusPill.textContent = `playback error ${code}: ${msg}`;
  });
  // Keep the live edge moving on today: every 5s, advance scrubber.max.
  // If we're in live mode, also slide the pinned thumb forward.
  setInterval(() => {
    const wasAtMax = parseInt(scrubber.value, 10) === parseInt(scrubber.max, 10);
    applyScrubberMax();
    renderTicks();
    renderAvailabilityBar();
    // Pip positions are in % of scrubMaxSec(), and that denominator
    // just changed; redraw so they don't drift right relative to the
    // availability bar between 15s event refreshes.
    renderEventPips();
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
  updateGoLiveBtn();
  refreshAvailability();
  // Honor ?ts=<unix epoch> deep-links from /events. Default to live
  // when not present — opening playback with the slider parked at
  // 00:00 and nothing playing isn't useful; the user almost always
  // wants "what's happening now" until they scrub back.
  const tsParam = parseFloat(new URLSearchParams(window.location.search).get("ts"));
  if (Number.isFinite(tsParam) && tsParam > 0) {
    seekToTimestamp(tsParam);
  } else {
    goLive();
  }
}

init();
