// Events browse page. Fetches /api/events with cursor pagination,
// renders a thumbnail grid, supports class/camera/window filter chips,
// and deep-links each card to /sets/<set>/<cam>/playback?ts=<ts_start>.

const CLASS_CHIPS = ["all", "person", "animal", "vehicle", "motion", "dog", "cat", "bird", "car", "truck"];
const PAGE_SIZE = 60;

const grid = document.getElementById("events-grid");
const status = document.getElementById("status");
const loadMoreBtn = document.getElementById("load-more");
const emptyMsg = document.getElementById("empty-msg");
const cardTpl = document.getElementById("event-card-template");
const classChipsEl = document.getElementById("class-chips");
const camChipsEl = document.getElementById("cam-chips");
const windowChipsEl = document.getElementById("window-chips");

const state = {
  cls: "all",          // "all" disables the filter
  cam: "all",
  window: "24h",
  cursor: null,        // before_id for next page; null = first page
  exhausted: false,
};

// ----- chip wiring -----------------------------------------------------

function makeChips(parent, values, valueKey, isActive, onClick) {
  parent.innerHTML = "";
  for (const v of values) {
    const btn = document.createElement("button");
    btn.className = "chip" + (isActive(v) ? " active" : "");
    btn.dataset[valueKey] = v;
    // "motion" isn't a real detector class — capitalise it so it reads
    // as the pseudo-class it is alongside the lowercase YOLO classes.
    btn.textContent = v === "all" ? "All" : v === "motion" ? "Motion" : v;
    btn.addEventListener("click", () => onClick(v));
    parent.appendChild(btn);
  }
}

function refreshChipActiveState(parent, dataKey, current) {
  for (const btn of parent.querySelectorAll(".chip")) {
    btn.classList.toggle("active", btn.dataset[dataKey] === current);
  }
}

makeChips(classChipsEl, CLASS_CHIPS, "cls", v => v === state.cls, v => {
  state.cls = v;
  refreshChipActiveState(classChipsEl, "cls", v);
  resetAndLoad();
});

// Window chips are pre-rendered in HTML; just wire the click handler.
for (const btn of windowChipsEl.querySelectorAll(".chip")) {
  if (btn.dataset.window === state.window) btn.classList.add("active");
  btn.addEventListener("click", () => {
    state.window = btn.dataset.window;
    refreshChipActiveState(windowChipsEl, "window", btn.dataset.window);
    resetAndLoad();
  });
}

// ----- camera chips: built from /api/sets ------------------------------

const camToSet = new Map();   // cam name -> set id (for deep-link URLs)

async function loadCameraChips() {
  // Pull every set's camera list to build the chip row + the cam→set
  // map the deep-link rendering needs.
  const sets = await (await fetch("/api/sets")).json();
  const allCams = [];
  for (const s of sets) {
    const cams = await (await fetch(`/api/sets/${s.id}/cameras`)).json();
    for (const c of cams) {
      camToSet.set(c.name, s.id);
      allCams.push(c.name);
    }
  }
  makeChips(camChipsEl, ["all", ...allCams], "cam",
    v => v === state.cam,
    v => {
      state.cam = v;
      refreshChipActiveState(camChipsEl, "cam", v);
      resetAndLoad();
    });
}

// ----- fetch + render --------------------------------------------------

function windowSinceTs() {
  const now = Date.now() / 1000;
  switch (state.window) {
    case "24h":   return now - 24 * 3600;
    case "today": {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      return d.getTime() / 1000;
    }
    case "7d":    return now - 7 * 24 * 3600;
    case "all":   return null;
    default:      return now - 24 * 3600;
  }
}

function buildEventsUrl() {
  const p = new URLSearchParams();
  if (state.cls !== "all") p.set("class", state.cls);
  if (state.cam !== "all") p.set("cam", state.cam);
  const since = windowSinceTs();
  if (since !== null) p.set("since", since.toString());
  if (state.cursor !== null) p.set("before_id", state.cursor.toString());
  p.set("limit", PAGE_SIZE.toString());
  return `/api/events?${p.toString()}`;
}

function formatTime(ts) {
  const d = new Date(ts * 1000);
  // Show date if older than today, else time-of-day.
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  if (d.getTime() < today.getTime()) {
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit", second: "2-digit",
    });
  }
  return d.toLocaleTimeString(undefined, {
    hour: "numeric", minute: "2-digit", second: "2-digit",
  });
}

function renderEvents(events) {
  for (const ev of events) {
    const node = cardTpl.content.cloneNode(true);
    const a = node.querySelector(".event-card");
    const setId = ev.set_id || camToSet.get(ev.camera);
    if (setId) {
      a.href = `/sets/${setId}/${ev.camera}/playback?ts=${ev.ts_start}`;
    } else {
      a.removeAttribute("href");
    }
    a.dataset.cls = ev.class;
    // Capture button: navigates to playback with ?capture=1 so the
    // capture modal auto-opens once the video has decoded the seek-
    // target frame. stopPropagation so clicking it doesn't also fire
    // the parent <a>'s navigation (which would land us at playback
    // without the capture flag).
    const captureBtn = node.querySelector(".event-capture-link");
    if (setId) {
      captureBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        window.location.href =
          `/sets/${setId}/${ev.camera}/playback?ts=${ev.ts_start}&capture=1`;
      });
    } else {
      captureBtn.remove();
    }
    if (ev.thumb_url) {
      const img = node.querySelector(".event-thumb");
      img.src = ev.thumb_url;
      img.alt = `${ev.class} on ${ev.camera}`;
    } else {
      node.querySelector(".event-thumb").remove();
    }
    node.querySelector(".event-class-badge").textContent = ev.class;
    node.querySelector(".event-conf-badge").textContent = ev.max_conf.toFixed(2);
    node.querySelector(".event-cam").textContent = ev.camera;
    node.querySelector(".event-time").textContent = formatTime(ev.ts_start);
    if (ev.duration_s >= 1) {
      node.querySelector(".event-dur").textContent = `· ${ev.duration_s.toFixed(0)}s`;
    }
    grid.appendChild(node);
  }
}

async function loadPage() {
  if (state.exhausted) return;
  loadMoreBtn.disabled = true;
  status.textContent = "Loading…";
  try {
    const events = await (await fetch(buildEventsUrl())).json();
    renderEvents(events);
    if (events.length < PAGE_SIZE) {
      state.exhausted = true;
      loadMoreBtn.hidden = true;
    } else {
      state.cursor = events[events.length - 1].id;
      loadMoreBtn.hidden = false;
    }
    const total = grid.children.length;
    status.textContent = total === 0 ? "" : `${total} event${total === 1 ? "" : "s"}`;
    emptyMsg.hidden = total > 0;
  } catch (e) {
    status.textContent = "Error loading events";
    console.error(e);
  } finally {
    loadMoreBtn.disabled = false;
  }
}

function resetAndLoad() {
  grid.innerHTML = "";
  state.cursor = null;
  state.exhausted = false;
  loadMoreBtn.hidden = true;
  emptyMsg.hidden = true;
  loadPage();
}

loadMoreBtn.addEventListener("click", loadPage);

(async () => {
  await loadCameraChips();
  resetAndLoad();
})();
