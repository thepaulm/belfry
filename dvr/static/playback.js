"use strict";

// Window length per /get request. Short enough to reload quickly when
// scrubbing, long enough to watch a few minutes without re-requesting.
const WINDOW_S = 300;
const DAY_OPTIONS = 14;

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

function renderAvailabilityBar() {
  availability.innerHTML = "";
  const dayStart = selectedDayStart();
  const dayEnd = new Date(dayStart.getTime() + 86400_000);

  for (const r of availableRanges) {
    const start = new Date(r.start);
    const end = new Date(start.getTime() + r.duration * 1000);
    if (end <= dayStart || start >= dayEnd) continue;

    const clampedStart = Math.max(start - dayStart, 0) / 1000;
    const clampedEnd = Math.min(end - dayStart, 86400_000) / 1000;
    const left = (clampedStart / 86400) * 100;
    const width = ((clampedEnd - clampedStart) / 86400) * 100;

    const span = document.createElement("span");
    span.className = "avail-block";
    span.style.left = `${left}%`;
    span.style.width = `${width}%`;
    span.title = `${start.toLocaleString()} (${Math.round(r.duration)}s)`;
    availability.appendChild(span);
  }
}

function loadWindow() {
  const dayStart = selectedDayStart();
  const offset = parseInt(scrubber.value, 10);
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

function updateCursor() {
  const max = parseInt(scrubber.max, 10) || 86399;
  const val = parseInt(scrubber.value, 10);
  cursor.style.left = `${(val / max) * 100}%`;
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
  dayPicker.addEventListener("change", () => {
    renderAvailabilityBar();
    loadWindow();
  });
  scrubber.addEventListener("input", onScrub);
  scrubber.addEventListener("change", loadWindow);
  updateCursor();
  refreshAvailability();
}

init();
