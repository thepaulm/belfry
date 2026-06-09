// Alerts history browse page. Mirrors events.js (same grid/chip/cursor
// machinery, reusing the events CSS) but reads /api/alerts and renders one
// card per ROI-rule firing. Each card shows the fire-time thumbnail (bbox +
// ROI outline baked in), the class/confidence, the ROI it fired in, the
// camera and time, and deep-links to /sets/<set>/<cam>/playback?ts=<ts>.
//
// Distinct from alerts.js, which is the header-toggle live notification
// watcher loaded alongside this page (live toasts + history together).

const PAGE_SIZE = 60;

const grid = document.getElementById("events-grid");
const status = document.getElementById("status");
const loadMoreBtn = document.getElementById("load-more");
const emptyMsg = document.getElementById("empty-msg");
const cardTpl = document.getElementById("alert-card-template");
const classChipsEl = document.getElementById("class-chips");
const camChipsEl = document.getElementById("cam-chips");
const windowChipsEl = document.getElementById("window-chips");

const state = {
  cls: "all",          // "all" disables the filter (client-side, see loadPage)
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
    btn.textContent = v === "all" ? "All" : v;
    btn.addEventListener("click", () => onClick(v));
    parent.appendChild(btn);
  }
}

function refreshChipActiveState(parent, dataKey, current) {
  for (const btn of parent.querySelectorAll(".chip")) {
    btn.classList.toggle("active", btn.dataset[dataKey] === current);
  }
}

// Class chips come from /api/alert-classes — the emitted classes that can
// actually have alert rules (event_classes with aliases applied).
async function loadClassChips() {
  let classes = [];
  try {
    classes = await (await fetch("/api/alert-classes")).json();
  } catch { /* leave just "all" */ }
  makeChips(classChipsEl, ["all", ...classes], "cls", v => v === state.cls, v => {
    state.cls = v;
    refreshChipActiveState(classChipsEl, "cls", v);
    resetAndLoad();
  });
}

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

// /api/alerts has no server-side class filter (the table mixes classes per
// camera), so we ask for cam + since + cursor and filter class client-side.
function buildAlertsUrl() {
  const p = new URLSearchParams();
  if (state.cam !== "all") p.set("cam", state.cam);
  const since = windowSinceTs();
  if (since !== null) p.set("since", since.toString());
  if (state.cursor !== null) p.set("before_id", state.cursor.toString());
  p.set("limit", PAGE_SIZE.toString());
  return `/api/alerts?${p.toString()}`;
}

function formatTime(ts) {
  const d = new Date(ts * 1000);
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

function renderAlerts(alerts) {
  for (const a of alerts) {
    const node = cardTpl.content.cloneNode(true);
    const card = node.querySelector(".event-card");
    const setId = a.set_id || camToSet.get(a.camera);
    if (setId) {
      // from=alerts so playback shows a "← Alerts" link back here.
      card.href = `/sets/${setId}/${a.camera}/playback?ts=${a.ts}&from=alerts`;
    } else {
      card.removeAttribute("href");
    }
    card.dataset.cls = a.class;
    if (a.thumb_url) {
      const img = node.querySelector(".event-thumb");
      img.src = a.thumb_url;
      img.alt = `${a.class} in ${a.roi_name} on ${a.camera}`;
    } else {
      node.querySelector(".event-thumb").remove();
    }
    node.querySelector(".event-class-badge").textContent = a.class;
    node.querySelector(".event-conf-badge").textContent = a.conf.toFixed(2);
    node.querySelector(".alert-roi-badge").textContent = a.roi_name;
    node.querySelector(".event-cam").textContent = a.camera;
    node.querySelector(".event-time").textContent = formatTime(a.ts);
    grid.appendChild(node);
  }
}

async function loadPage() {
  if (state.exhausted) return;
  loadMoreBtn.disabled = true;
  status.textContent = "Loading…";
  try {
    const alerts = await (await fetch(buildAlertsUrl())).json();
    // Advance the cursor off the raw page (pre class-filter) so paging
    // stays correct even when the class filter hides the whole page.
    if (alerts.length < PAGE_SIZE) {
      state.exhausted = true;
      loadMoreBtn.hidden = true;
    } else {
      state.cursor = alerts[alerts.length - 1].id;
      loadMoreBtn.hidden = false;
    }
    const shown = state.cls === "all"
      ? alerts
      : alerts.filter((a) => a.class === state.cls);
    renderAlerts(shown);
    const total = grid.children.length;
    status.textContent = total === 0 ? "" : `${total} alert${total === 1 ? "" : "s"}`;
    // Only show the empty message once paging is exhausted — a class
    // filter can legitimately yield an empty intermediate page.
    emptyMsg.hidden = total > 0 || !state.exhausted;
  } catch (e) {
    status.textContent = "Error loading alerts";
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
  await Promise.all([loadClassChips(), loadCameraChips()]);
  resetAndLoad();
})();
