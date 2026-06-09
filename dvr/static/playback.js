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
const scaleDayBtn = document.getElementById("scale-day");
const scaleHourBtn = document.getElementById("scale-hour");
const scale5MinBtn = document.getElementById("scale-5min");
const prevSliceBtn = document.getElementById("prev-slice");
const nextSliceBtn = document.getElementById("next-slice");
const sliceLabel = document.getElementById("slice-label");
const captureBtn = document.getElementById("capture-btn");
const captureModal = document.getElementById("capture-modal");
const captureCanvas = document.getElementById("capture-canvas");
const captureClassInput = document.getElementById("capture-class");
const captureNegativeInput = document.getElementById("capture-negative");
const captureMetaEl = document.getElementById("capture-meta");
const captureSaveBtn = document.getElementById("capture-save");
const captureCancelBtn = document.getElementById("capture-cancel");
const captureStatusEl = document.getElementById("capture-status");

if (SET_ID && CAM) {
  backLink.href = `/sets/${encodeURIComponent(SET_ID)}`;
}

let availableRanges = [];
let dayEvents = [];          // events with ts_start inside the selected day
let mode = "past"; // "past" | "live"
let liveHls = null;
// Sec-of-day where the currently-loaded past mp4 starts. Set in
// loadWindow(), nulled in enterLiveMode(). Used by the timeupdate
// listener to advance the slider thumb as playback progresses (the
// slider's position is otherwise frozen at the load point, which made
// detections look like they were happening "before" the cursor when
// really the video had played past them).
let pastLoadStartOffsetSec = null;
// True between pointerdown/touchstart on the scrubber and the next
// change event — suppresses timeupdate-driven thumb updates so they
// don't fight a user mid-drag.
let userScrubbing = false;
// Slider visible-window scale. Day shows the full 24h (or up to "now"
// for today); Hour zooms the slider to a single hour for finer
// scrubbing; 5min zooms to a single 5-minute slot. Prev/Next-slice
// buttons step the visible window by viewSpanSec().
const SCALE_SPAN = { day: 86400, hour: 3600, "5min": 300 };
let viewScale = "day";
let viewStartSec = 0;        // sec-of-day where the visible window starts
// Single BoxOverlay that lives across mode transitions. When entering
// live mode we hook it to the SSE feed; when entering past mode (or
// scrubbing into a different 5-min window) we re-bind it to the new
// playback SSE with the right window-start offset. Destroyed on
// page navigation only.
let playbackOverlay = null;
let pastWindowStartUnix = null;   // tracks the active past window so
                                  // we don't re-fire subscribePast
                                  // every time loadWindow runs

// Coarse class → pip color. Two reasons we collapse the rainbow into
// three buckets: (1) keeps the legend short (person / animal / vehicle),
// (2) the COCO subclasses dog / cat / bird don't add visual signal at
// pip-width — a single green is enough.
// Renamed from CLASS_COLOR to dodge a clash with overlay.js (both run
// in classic-script global scope — duplicate `const` is a SyntaxError
// that aborts the whole file).
const PIP_COLOR = {
  person:  "#4ea1ff",
  animal:  "#5ad17c",
  dog:     "#5ad17c",
  cat:     "#5ad17c",
  bird:    "#5ad17c",
  // belfry-v1 wildlife fine-tune classes — all green, same animal bucket.
  deer:     "#5ad17c",
  coyote:   "#5ad17c",
  raccoon:  "#5ad17c",
  rabbit:   "#5ad17c",
  squirrel: "#5ad17c",
  rat:      "#5ad17c",
  vehicle: "#ff9b3f",
  car:     "#ff9b3f",
  truck:   "#ff9b3f",
  motion:  "#e879f9",   // unknown-class "something moved" events
};
const PIP_COLOR_DEFAULT = "#aaa";

function pipColor(cls) {
  return PIP_COLOR[cls] || PIP_COLOR_DEFAULT;
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
  const minVal = scrubMinSec();
  const max = scrubMaxSec();
  const range = max - minVal || 1;
  const dayStart = selectedDayStart();
  const dayStartUnix = dayStart.getTime() / 1000;
  for (const ev of dayEvents) {
    const startSec = ev.ts_start - dayStartUnix;
    const endSec = ev.ts_end - dayStartUnix;
    if (endSec < minVal || startSec > max) continue;
    const clampedStart = Math.max(startSec, minVal);
    const clampedEnd = Math.min(endSec, max);
    const left = ((clampedStart - minVal) / range) * 100;
    const widthPct = ((clampedEnd - clampedStart) / range) * 100;
    const pip = document.createElement("span");
    pip.className = "event-pip";
    pip.style.left = `${left}%`;
    // Floor the rendered width at a tiny minimum so a 0-second event
    // (sample_count=1) is still visible as a hairline mark.
    pip.style.width = `${Math.max(widthPct, 0.15)}%`;
    pip.style.background = pipColor(ev.class);
    pip.dataset.ts = String(ev.ts_start);
    pip.title = `${ev.class} · ${new Date(ev.ts_start * 1000).toLocaleTimeString()}`
      + ` · conf ${ev.max_conf.toFixed(2)}`;
    pip.addEventListener("click", () => {
      seekToTimestamp(ev.ts_start);
    });
    pip.addEventListener("mouseenter", () => showPipPreview(pip, ev));
    pip.addEventListener("mouseleave", hidePipPreview);
    eventPipsEl.appendChild(pip);
  }
}

// Hover-preview on a timeline pip: pops up the event's thumbnail with
// its peak bounding box drawn over the image. The hairline-width pips
// for short events are easy to miss; the preview lets the user mouse
// down the timeline and see where in the frame to look.
let pipPreviewEl = null;

function ensurePipPreview() {
  if (pipPreviewEl) return pipPreviewEl;
  pipPreviewEl = document.createElement("div");
  pipPreviewEl.id = "pip-preview";
  pipPreviewEl.innerHTML =
    '<div class="pip-preview-frame">' +
      '<img alt="">' +
      '<canvas></canvas>' +
    '</div>' +
    '<div class="pip-preview-meta"></div>';
  document.body.appendChild(pipPreviewEl);
  return pipPreviewEl;
}

function drawPipPreviewBox(canvas, ev, dispW, dispH) {
  canvas.width = dispW;
  canvas.height = dispH;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, dispW, dispH);
  const b = ev.peak_bbox;
  if (!Array.isArray(b) || b.length !== 4) return;
  const x1 = b[0] * dispW, y1 = b[1] * dispH;
  const x2 = b[2] * dispW, y2 = b[3] * dispH;
  const w = x2 - x1, h = y2 - y1;
  const color = pipColor(ev.class);
  ctx.lineWidth = 2;
  ctx.strokeStyle = color;
  if (ev.class === "motion") {
    // Mirror overlay.js: dashed stroke, no label chip — motion has no
    // class identity and the "confidence" is just blob-area fraction.
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(x1, y1, w, h);
    ctx.setLineDash([]);
    return;
  }
  ctx.strokeRect(x1, y1, w, h);
  const label = `${ev.class} ${ev.max_conf.toFixed(2)}`;
  ctx.font = "11px system-ui, sans-serif";
  const padX = 4, padY = 2;
  const chipW = ctx.measureText(label).width + padX * 2;
  const chipH = 14;
  let chipY = y1 - chipH - 2;
  if (chipY < 0) chipY = y1 + 2;
  ctx.fillStyle = color;
  ctx.fillRect(x1, chipY, chipW, chipH);
  ctx.fillStyle = "#000";
  ctx.textBaseline = "top";
  ctx.fillText(label, x1 + padX, chipY + padY);
}

function positionPipPreview(pipEl) {
  if (!pipPreviewEl) return;
  const r = pipEl.getBoundingClientRect();
  const p = pipPreviewEl.getBoundingClientRect();
  const pad = 8;
  let left = r.left + r.width / 2 - p.width / 2;
  left = Math.max(pad, Math.min(left, window.innerWidth - p.width - pad));
  // Prefer above the pip so the preview doesn't cover the video below.
  let top = r.top - p.height - pad;
  if (top < pad) top = r.bottom + pad;
  pipPreviewEl.style.left = `${left}px`;
  pipPreviewEl.style.top = `${top}px`;
}

function showPipPreview(pipEl, ev) {
  if (!ev.thumb_url) return;
  const preview = ensurePipPreview();
  const img = preview.querySelector("img");
  const canvas = preview.querySelector("canvas");
  const meta = preview.querySelector(".pip-preview-meta");
  const tLabel = new Date(ev.ts_start * 1000).toLocaleTimeString();
  meta.textContent = `${ev.class} · ${ev.camera} · ${tLabel}`
    + ` · conf ${ev.max_conf.toFixed(2)}`;
  // Render hidden first so layout can measure for positioning.
  preview.style.visibility = "hidden";
  preview.style.display = "block";
  const setup = () => {
    const dispW = img.clientWidth || img.naturalWidth;
    const dispH = img.clientHeight || img.naturalHeight;
    drawPipPreviewBox(canvas, ev, dispW, dispH);
    positionPipPreview(pipEl);
    preview.style.visibility = "visible";
  };
  if (img.complete && img.src.endsWith(ev.thumb_url)) {
    setup();
  } else {
    img.onload = setup;
    img.onerror = () => { preview.style.display = "none"; };
    img.src = ev.thumb_url;
  }
}

function hidePipPreview() {
  if (pipPreviewEl) pipPreviewEl.style.display = "none";
}

function renderEventLegend() {
  // Show only the classes actually present in today's events to keep
  // the header tidy. Classes are bucketed by visual color above; the
  // legend uses the displayed bucket names.
  const buckets = new Set();
  for (const ev of dayEvents) {
    if (ev.class === "person")        buckets.add("person");
    else if (ev.class === "vehicle" || ev.class === "car" || ev.class === "truck") buckets.add("vehicle");
    else if (ev.class === "motion")   buckets.add("motion");
    else                              buckets.add("animal");
  }
  if (!buckets.size) {
    eventLegendEl.textContent = "";
    return;
  }
  eventLegendEl.innerHTML = "";
  const order = ["person", "animal", "vehicle", "motion"];
  for (const name of order) {
    if (!buckets.has(name)) continue;
    const dot = document.createElement("span");
    dot.className = "legend-dot";
    dot.style.background = pipColor(name);
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

function viewSpanSec() {
  return SCALE_SPAN[viewScale];
}

function snapToScale(sec) {
  const span = viewSpanSec();
  return Math.floor(Math.max(0, sec) / span) * span;
}

function recomputeViewStart(anchor) {
  // Snap viewStartSec to contain `anchor` (sec-of-day). Anchor defaults
  // to the current scrubber value so that switching scales preserves
  // "where the user was looking."
  const cur = (anchor != null ? anchor : parseInt(scrubber.value, 10)) || 0;
  const live = liveEdgeOfSelectedDay();
  const dayMax = live !== null ? live : 86399;
  const clamped = Math.max(0, Math.min(cur, dayMax));
  viewStartSec = snapToScale(clamped);
}

function applyScrubberMax() {
  // The slider's range is the visible window: [viewStartSec, viewStartSec+span-1],
  // capped on today by the live edge. On other days, capped at end-of-day.
  const span = viewSpanSec();
  const live = liveEdgeOfSelectedDay();
  const dayMax = live !== null ? live : 86399;
  const minVal = Math.max(0, Math.min(viewStartSec, dayMax));
  const maxVal = Math.max(minVal, Math.min(viewStartSec + span - 1, dayMax));
  scrubber.min = String(minVal);
  scrubber.max = String(maxVal);
  const cur = parseInt(scrubber.value, 10);
  if (cur < minVal) scrubber.value = String(minVal);
  else if (cur > maxVal) scrubber.value = String(maxVal);
  updateCursor();
}

function scrubMinSec() {
  return parseInt(scrubber.min, 10) || 0;
}

function scrubMaxSec() {
  return parseInt(scrubber.max, 10) || 86399;
}

function renderAvailabilityBar() {
  availability.innerHTML = "";
  // The scrubber and the availability bar share a denominator so the blue
  // blocks line up with the cursor. The visible window is
  // [scrubber.min, scrubber.max] sec-of-day; ranges outside it are skipped,
  // ranges that straddle it are clamped.
  const minVal = scrubMinSec();
  const max = scrubMaxSec();
  const range = max - minVal || 1;
  const dayStart = selectedDayStart();
  const visibleStartMs = dayStart.getTime() + minVal * 1000;
  const visibleEndMs = dayStart.getTime() + max * 1000;

  for (const r of availableRanges) {
    const start = new Date(r.start);
    const end = new Date(start.getTime() + r.duration * 1000);
    if (end.getTime() <= visibleStartMs || start.getTime() >= visibleEndMs) continue;

    const clampedStartMs = Math.max(start.getTime(), visibleStartMs);
    const clampedEndMs = Math.min(end.getTime(), visibleEndMs);
    const left = ((clampedStartMs - visibleStartMs) / (range * 1000)) * 100;
    const width = ((clampedEndMs - clampedStartMs) / (range * 1000)) * 100;

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
  const minVal = scrubMinSec();
  const max = scrubMaxSec();
  const range = max - minVal;
  ticksEl.innerHTML = "";
  for (let i = 0; i <= 4; i++) {
    const sec = Math.round(minVal + (range * i) / 4);
    const span = document.createElement("span");
    span.textContent = fmtClock(sec);
    ticksEl.appendChild(span);
  }
}

function rerenderTimeline() {
  applyScrubberMax();
  renderTicks();
  renderAvailabilityBar();
  renderEventPips();
  updateSliceLabel();
}

function updateScaleButtons() {
  scaleDayBtn.classList.toggle("active", viewScale === "day");
  scaleHourBtn.classList.toggle("active", viewScale === "hour");
  scale5MinBtn.classList.toggle("active", viewScale === "5min");
}

function updateSliceLabel() {
  if (viewScale === "day") {
    sliceLabel.textContent = "";
    return;
  }
  const start = viewStartSec;
  const end = Math.min(viewStartSec + viewSpanSec(), 86400);
  sliceLabel.textContent = `${fmtClock(start)} – ${fmtClock(end)}`;
}

function applyScale(scale) {
  if (!(scale in SCALE_SPAN) || scale === viewScale) return;
  viewScale = scale;
  recomputeViewStart();
  rerenderTimeline();
  updateScaleButtons();
}

function shiftSlice(direction) {
  // direction: -1 = earlier, +1 = later. In Day mode this steps the day
  // picker (options are listed today→oldest, so "later" = lower index).
  if (viewScale === "day") {
    const idx = dayPicker.selectedIndex;
    const newIdx = idx + (direction > 0 ? -1 : 1);
    if (newIdx < 0 || newIdx >= dayPicker.options.length) return;
    dayPicker.selectedIndex = newIdx;
    dayPicker.dispatchEvent(new Event("change"));
    return;
  }
  const span = viewSpanSec();
  const live = liveEdgeOfSelectedDay();
  const dayMax = live !== null ? live : 86399;
  const newStart = viewStartSec + direction * span;
  if (newStart < 0 || newStart > dayMax) return;
  viewStartSec = newStart;
  // Reshape the slider's range BEFORE assigning value: an HTML5 range
  // input auto-clamps value to its current [min, max], so writing the
  // new value first while min is still the old slice clamps it away.
  rerenderTimeline();
  scrubber.value = String(newStart);
  updateCursor();
  loadWindow();
}

function tearDownLive() {
  if (liveHls) {
    liveHls.destroy();
    liveHls = null;
  }
}

function tearDownOverlay() {
  if (playbackOverlay) {
    playbackOverlay.destroy();
    playbackOverlay = null;
  }
  pastWindowStartUnix = null;
}

function ensureOverlay() {
  if (playbackOverlay || !window.BoxOverlay) return playbackOverlay;
  const wrap = document.querySelector(".playback-video-wrap");
  if (!wrap) return null;
  playbackOverlay = new BoxOverlay(wrap, CAM);
  return playbackOverlay;
}

function enterLiveMode() {
  mode = "live";
  pastLoadStartOffsetSec = null;
  // At Hour / 5-min scales, the live edge has to fall inside the visible
  // window or the slider/cursor pin to a stale position. Snap viewStart
  // to whichever slice contains "now" first, then derive scrubber range.
  const live = liveEdgeOfSelectedDay();
  if (live !== null) viewStartSec = snapToScale(live);
  rerenderTimeline();
  if (live !== null) {
    scrubber.value = String(scrubMaxSec());
    requestAnimationFrame(updateCursor);
  }
  windowLabel.textContent = "LIVE";
  updateGoLiveBtn();

  const hlsUrl = `/hls/${encodeURIComponent(CAM)}/index.m3u8`;
  tearDownLive();
  // Refresh the bounding-box overlay for live: the past-mode subscription
  // (if any) is dropped by stop()/start() inside the overlay itself
  // when the toggle re-fires the live URL. We re-create the overlay
  // outright on each live-mode entry so its internal state is clean.
  tearDownOverlay();
  ensureOverlay();
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
  pastLoadStartOffsetSec = offset;
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

  // Past-mode bounding-box overlay: only re-subscribe when the active
  // window actually changes (loadWindow can fire repeatedly during a
  // single window — e.g. the 5s tick that bumps scrubber.max).
  // subscribePast unconditionally — it sets the overlay's mode/URL
  // and only opens the EventSource if "Show labels" is currently on.
  // We need the mode flip even when labels are off so that toggling
  // them on later opens the past SSE rather than defaulting to live.
  const newWindowStart = target.getTime() / 1000;
  if (newWindowStart !== pastWindowStartUnix) {
    pastWindowStartUnix = newWindowStart;
    const overlay = ensureOverlay();
    if (overlay) {
      overlay.subscribePast({
        url: `/api/inference/playback?cam=${encodeURIComponent(CAM)}`
          + `&start=${encodeURIComponent(isoStart)}`
          + `&duration=${WINDOW_S}s`,
        video: player,
        windowStartUnix: newWindowStart,
      });
    }
  }
}

// Approximate range-input thumb radius. Browsers inset the thumb by this
// much from each end of the track so the circle is fully visible at the
// extremes, and we have to match the inset or the cursor line drifts off
// the thumb's center (most visible at value == max — looks like the line
// doesn't move when we snap to live).
const SCRUB_THUMB_RADIUS_PX = 8;

function updateCursor() {
  const minVal = scrubMinSec();
  const max = scrubMaxSec();
  const val = parseInt(scrubber.value, 10);
  const range = max - minVal;
  const pct = range > 0 ? (val - minVal) / range : 0;
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
  }
  const live = liveEdgeOfSelectedDay();
  if (live !== null) {
    viewStartSec = snapToScale(live);
    rerenderTimeline();
    scrubber.value = String(scrubMaxSec());
  } else {
    rerenderTimeline();
  }
  loadWindow();
}

function onScrub() {
  // Preview-only: update the cursor line and label as the user drags,
  // but don't load a new video window until they release (the change
  // event). Loading mid-drag was confusing — the player would jump
  // every time the slider paused.
  const offset = parseInt(scrubber.value, 10);
  windowLabel.textContent = `${fmtClock(offset)} local · release to load`;
  updateCursor();
}

function seekToTimestamp(ts) {
  // Move the day picker to ts's local day, snap the visible window to
  // contain ts, then put the scrubber there and load. Used when the
  // page opens with ?ts=<unix epoch>, e.g. via a deep-link from /events.
  const target = new Date(ts * 1000);
  const dayMs = startOfLocalDay(target).getTime();
  if (![...dayPicker.options].some(o => parseInt(o.value, 10) === dayMs)) {
    // ts predates the day picker's range — fall back to live.
    goLive();
    return;
  }
  dayPicker.value = String(dayMs);
  const offset = Math.max(0, Math.floor(ts - dayMs / 1000));
  recomputeViewStart(offset);
  rerenderTimeline();
  scrubber.value = String(Math.min(offset, scrubMaxSec()));
  updateCursor();
  loadWindow();
}

// ---------- training-image capture ----------
// Grabs the currently-painted frame off the <video> element, posts the
// JPEG to /api/training/capture, files it under <class> (or
// negative_<class> if the checkbox is on). Overlay canvas isn't part
// of the grab — drawImage(player) pulls pixels from the media element
// only, so training images are box-free regardless of the labels
// toggle state.

let captureBlob = null;
let captureTs = null;

function captureTimestamp() {
  // Best-effort absolute unix-epoch for the captured frame: in past
  // mode the loaded window's start plus video.currentTime; in live
  // mode just now. Used only for the saved filename — the capture is
  // still well-defined if this is approximate.
  if (mode === "live") return Date.now() / 1000;
  if (pastLoadStartOffsetSec == null) return Date.now() / 1000;
  const dayStart = selectedDayStart();
  return dayStart.getTime() / 1000 + pastLoadStartOffsetSec + (player.currentTime || 0);
}

function grabCurrentFrame() {
  // Returns a Promise<Blob|null>. Pauses playback first so what the
  // user sees in the modal preview matches what gets saved.
  if (player.readyState < 2 || !player.videoWidth) return Promise.resolve(null);
  player.pause();
  captureCanvas.width = player.videoWidth;
  captureCanvas.height = player.videoHeight;
  const ctx = captureCanvas.getContext("2d");
  ctx.drawImage(player, 0, 0, captureCanvas.width, captureCanvas.height);
  return new Promise((resolve) => {
    captureCanvas.toBlob((b) => resolve(b), "image/jpeg", 0.92);
  });
}

async function openCaptureModal() {
  captureStatusEl.textContent = "";
  const blob = await grabCurrentFrame();
  if (!blob) {
    statusPill.textContent = "no frame to capture (video not loaded)";
    return;
  }
  captureBlob = blob;
  captureTs = captureTimestamp();
  const tsLabel = new Date(captureTs * 1000).toLocaleString();
  captureMetaEl.textContent = `${CAM} · ${tsLabel} · ${Math.round(blob.size / 1024)} KB`;
  captureModal.hidden = false;
  // Don't clobber the class field if the user is iterating on captures
  // of the same class — leave whatever was there last time. Just put
  // focus + select so they can overtype.
  captureClassInput.focus();
  captureClassInput.select();
}

function closeCaptureModal() {
  captureModal.hidden = true;
  captureBlob = null;
  captureTs = null;
}

async function saveCapture() {
  const cls = captureClassInput.value.trim().toLowerCase().replace(/\s+/g, "_");
  if (!cls) {
    captureStatusEl.textContent = "class is required";
    return;
  }
  if (!captureBlob) {
    captureStatusEl.textContent = "no image to save";
    return;
  }
  const params = new URLSearchParams();
  params.set("cam", CAM);
  params.set("class_name", cls);
  params.set("negative", captureNegativeInput.checked ? "true" : "false");
  if (captureTs != null) params.set("ts", String(captureTs));
  captureSaveBtn.disabled = true;
  captureStatusEl.textContent = "Saving…";
  try {
    const r = await fetch(`/api/training/capture?${params.toString()}`, {
      method: "POST",
      body: captureBlob,
      headers: { "Content-Type": "image/jpeg" },
      credentials: "same-origin",
    });
    if (!r.ok) {
      const txt = await r.text();
      captureStatusEl.textContent = `save failed: ${r.status} ${txt}`;
      return;
    }
    const j = await r.json();
    statusPill.textContent = `saved → ${j.category}/${j.filename}`;
    closeCaptureModal();
  } catch (e) {
    captureStatusEl.textContent = `save failed: ${e.message}`;
  } finally {
    captureSaveBtn.disabled = false;
  }
}

function wireCaptureUI() {
  captureBtn.addEventListener("click", openCaptureModal);
  captureCancelBtn.addEventListener("click", closeCaptureModal);
  captureSaveBtn.addEventListener("click", saveCapture);
  // Click the backdrop to dismiss (but not clicks inside the modal-content).
  captureModal.addEventListener("click", (e) => {
    if (e.target === captureModal || e.target.classList.contains("modal-backdrop")) {
      closeCaptureModal();
    }
  });
  captureClassInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveCapture(); }
  });
  window.addEventListener("keydown", (e) => {
    if (!captureModal.hidden) {
      if (e.key === "Escape") { closeCaptureModal(); }
      return;
    }
    // 'c' shortcut to open — only when not focused on a text input/select.
    if ((e.key === "c" || e.key === "C") && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || e.target.tagName === "TEXTAREA")) return;
      e.preventDefault();
      openCaptureModal();
    }
  });
}

function maybeAutoOpenCapture() {
  // Deep-link from /events: ?capture=1 auto-opens the modal once a
  // frame is decoded. We listen once on whichever event fires first
  // (loadeddata after first decode, or seeked if the seek to ?ts=
  // hasn't completed yet) and add a short delay so the seeked frame
  // is what gets painted, not the initial keyframe.
  if (new URLSearchParams(window.location.search).get("capture") !== "1") return;
  let fired = false;
  const tryOpen = () => {
    if (fired) return;
    if (player.readyState < 2 || !player.videoWidth) return;
    fired = true;
    player.removeEventListener("loadeddata", tryOpen);
    player.removeEventListener("seeked", tryOpen);
    setTimeout(openCaptureModal, 250);
  };
  player.addEventListener("loadeddata", tryOpen);
  player.addEventListener("seeked", tryOpen);
}

function init() {
  if (!SET_ID || !CAM) {
    statusPill.textContent = "bad URL";
    return;
  }
  populateDayPicker();
  applyScrubberMax();
  renderTicks();
  updateScaleButtons();
  updateSliceLabel();
  dayPicker.addEventListener("change", () => {
    // Day changed: stale events from the old day shouldn't render against
    // the new day's timeline. Clear, then refresh asynchronously.
    dayEvents = [];
    recomputeViewStart();
    rerenderTimeline();
    refreshDayEvents();
    loadWindow();
  });
  scrubber.addEventListener("input", onScrub);
  scrubber.addEventListener("change", () => {
    userScrubbing = false;
    loadWindow();
  });
  // Track active drag so the timeupdate-driven thumb update below
  // doesn't fight the user mid-scrub. Pointer events cover both mouse
  // and touch on modern browsers.
  scrubber.addEventListener("pointerdown", () => { userScrubbing = true; });
  // Belt-and-braces for the touch path on Safari, which historically
  // hasn't fired pointer events on range inputs as reliably.
  scrubber.addEventListener("touchstart", () => { userScrubbing = true; }, { passive: true });
  // Drive the slider thumb forward as the past-mode mp4 plays. Live
  // mode is already pinned to the live edge by the 5s setInterval so
  // we leave it alone here.
  player.addEventListener("timeupdate", () => {
    if (mode !== "past") return;
    if (userScrubbing) return;
    if (pastLoadStartOffsetSec === null) return;
    const newOffset = pastLoadStartOffsetSec + Math.floor(player.currentTime);
    const clamped = Math.min(Math.max(newOffset, scrubMinSec()), scrubMaxSec());
    if (clamped !== parseInt(scrubber.value, 10)) {
      scrubber.value = String(clamped);
      windowLabel.textContent = `${fmtClock(clamped)} local · ${WINDOW_S / 60}m window`;
      updateCursor();
    }
  });
  goLiveBtn.addEventListener("click", goLive);
  prevEventBtn.addEventListener("click", () => gotoNeighborEvent("prev"));
  nextEventBtn.addEventListener("click", () => gotoNeighborEvent("next"));
  scaleDayBtn.addEventListener("click", () => applyScale("day"));
  scaleHourBtn.addEventListener("click", () => applyScale("hour"));
  scale5MinBtn.addEventListener("click", () => applyScale("5min"));
  prevSliceBtn.addEventListener("click", () => shiftSlice(-1));
  nextSliceBtn.addEventListener("click", () => shiftSlice(+1));
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
  // If we're in live mode, also slide the pinned thumb forward — and at
  // Hour / 5-min scale, slide viewStart forward too when the live edge
  // crosses into the next slice.
  setInterval(() => {
    const wasAtMax = parseInt(scrubber.value, 10) === parseInt(scrubber.max, 10);
    if (mode === "live") {
      const live = liveEdgeOfSelectedDay();
      if (live !== null) viewStartSec = snapToScale(live);
    }
    rerenderTimeline();
    if (mode === "live" || wasAtMax) {
      scrubber.value = String(scrubMaxSec());
      updateCursor();
    }
  }, 5000);
  // Re-poll /list so the availability bar grows with recording in real
  // time. MediaMTX's /list reflects on-disk segments, including the
  // currently-being-written one with its duration-so-far.
  setInterval(refreshAvailability, 15000);
  updateCursor();
  updateGoLiveBtn();
  wireCaptureUI();
  maybeAutoOpenCapture();
  refreshAvailability();
  // Honor ?ts=<unix epoch> deep-links from /events. Default to live
  // when not present — opening playback with the slider parked at
  // 00:00 and nothing playing isn't useful; the user almost always
  // wants "what's happening now" until they scrub back.
  const qp = new URLSearchParams(window.location.search);
  // Reveal the "← Alerts" header link when we arrived from /alerts, so the
  // user can step back to the alert list they clicked from.
  if (qp.get("from") === "alerts") {
    const backToAlerts = document.getElementById("back-to-alerts");
    if (backToAlerts) backToAlerts.hidden = false;
  }
  const tsParam = parseFloat(qp.get("ts"));
  if (Number.isFinite(tsParam) && tsParam > 0) {
    seekToTimestamp(tsParam);
  } else {
    goLive();
  }
}

init();
