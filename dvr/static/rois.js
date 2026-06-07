"use strict";

// ROI editor + alert-rule config. Draws polygons over a (freezable) live
// HLS frame; coordinates are stored normalized 0..1 like detection boxes,
// so a region drawn here lines up with the inference recorder's bboxes
// regardless of source resolution. The recorder reads the same rois /
// alert_rules tables (config.db) to decide when to fire an alert.

const ROI_COLORS = [
  "#4ea1ff", "#5ad17c", "#ff9b3f", "#e879f9",
  "#f2d94e", "#ff6b6b", "#46d3d0", "#b388ff",
];

const els = {
  camSelect: document.getElementById("cam-select"),
  status: document.getElementById("status"),
  video: document.getElementById("roi-video"),
  canvas: document.getElementById("roi-canvas"),
  wrap: document.querySelector(".roi-wrap"),
  freeze: document.getElementById("freeze-btn"),
  draw: document.getElementById("draw-btn"),
  finish: document.getElementById("finish-btn"),
  cancel: document.getElementById("cancel-btn"),
  name: document.getElementById("roi-name"),
  saveRoi: document.getElementById("save-roi-btn"),
  hint: document.getElementById("draw-hint"),
  roiList: document.getElementById("roi-list"),
  roiEmpty: document.getElementById("roi-empty"),
  ruleList: document.getElementById("rule-list"),
  ruleEmpty: document.getElementById("rule-empty"),
  ruleForm: document.getElementById("rule-form"),
  ruleRoi: document.getElementById("rule-roi"),
  ruleClass: document.getElementById("rule-class"),
  ruleConf: document.getElementById("rule-conf"),
  ruleCooldown: document.getElementById("rule-cooldown"),
  roiItemTpl: document.getElementById("roi-item-template"),
  ruleItemTpl: document.getElementById("rule-item-template"),
};

const ctx = els.canvas.getContext("2d");

const state = {
  cams: [],
  classes: [],
  cam: null,
  hls: null,
  rois: [],
  rules: [],
  hidden: new Set(),       // roi ids the user toggled off the canvas
  draw: { active: false, points: [] },
};

// --------------------------------------------------------------- fetch
async function fetchJSON(url) {
  const r = await fetch(url, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
async function sendJSON(method, url, body) {
  const r = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = r.status;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.status === 204 ? null : r.json();
}
function flash(msg, isErr) {
  els.status.textContent = msg;
  els.status.style.color = isErr ? "var(--offline)" : "";
}

// --------------------------------------------------------------- HLS
function attachVideo(cam) {
  if (state.hls) { state.hls.destroy(); state.hls = null; }
  els.video.removeAttribute("src");
  if (!cam || !cam.hls_url) return;
  if (els.video.canPlayType("application/vnd.apple.mpegurl")) {
    els.video.src = cam.hls_url;
    els.video.play().catch(() => {});
  } else if (window.Hls && Hls.isSupported()) {
    const hls = new Hls({ lowLatencyMode: true, liveSyncDurationCount: 2 });
    hls.loadSource(cam.hls_url);
    hls.attachMedia(els.video);
    state.hls = hls;
  }
  els.video.play().catch(() => {});
  els.freeze.textContent = "Freeze frame";
}

// --------------------------------------------------------------- canvas
function resizeCanvas() {
  const r = els.wrap.getBoundingClientRect();
  const w = Math.max(1, Math.floor(r.width));
  const h = Math.max(1, Math.floor(r.height));
  if (els.canvas.width !== w) els.canvas.width = w;
  if (els.canvas.height !== h) els.canvas.height = h;
  render();
}

function colorFor(i) { return ROI_COLORS[i % ROI_COLORS.length]; }

function drawPolygon(poly, color, opts = {}) {
  const w = els.canvas.width, h = els.canvas.height;
  if (poly.length < 2) {
    // single point marker while drawing
    if (poly.length === 1) {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(poly[0][0] * w, poly[0][1] * h, 4, 0, Math.PI * 2);
      ctx.fill();
    }
    return;
  }
  ctx.beginPath();
  ctx.moveTo(poly[0][0] * w, poly[0][1] * h);
  for (let i = 1; i < poly.length; i++) ctx.lineTo(poly[i][0] * w, poly[i][1] * h);
  if (opts.close) ctx.closePath();
  ctx.lineWidth = opts.selected ? 3 : 2;
  ctx.strokeStyle = color;
  ctx.stroke();
  if (opts.close) {
    ctx.fillStyle = color + "22";   // ~13% alpha hex suffix
    ctx.fill();
  }
  if (opts.vertices) {
    ctx.fillStyle = color;
    for (const p of poly) {
      ctx.beginPath();
      ctx.arc(p[0] * w, p[1] * h, 4, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  if (opts.label) {
    const x = poly[0][0] * w, y = poly[0][1] * h;
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillStyle = color;
    ctx.fillText(opts.label, x + 6, y - 6);
  }
}

function render() {
  ctx.clearRect(0, 0, els.canvas.width, els.canvas.height);
  state.rois.forEach((roi, i) => {
    if (state.hidden.has(roi.id)) return;
    drawPolygon(roi.polygon, colorFor(i), {
      close: true, vertices: false, label: roi.name + (roi.enabled ? "" : " (off)"),
    });
  });
  if (state.draw.active) {
    drawPolygon(state.draw.points, "#ffffff", { close: false, vertices: true });
  }
}

function pointFromEvent(ev) {
  const r = els.canvas.getBoundingClientRect();
  const x = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
  const y = Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height));
  return [x, y];
}

// --------------------------------------------------------------- draw flow
function setDrawControls(on) {
  els.finish.hidden = !on;
  els.cancel.hidden = !on;
  els.name.hidden = !on;
  els.saveRoi.hidden = !on;
  els.draw.hidden = on;
  els.hint.textContent = on ? "Click to add points · Backspace removes the last · Enter to finish" : "";
}

function startDraw() {
  state.draw = { active: true, points: [] };
  setDrawControls(true);
  els.name.value = "";
  render();
}

function cancelDraw() {
  state.draw = { active: false, points: [] };
  setDrawControls(false);
  render();
}

function finishDraw() {
  if (state.draw.points.length < 3) {
    flash("need at least 3 points", true);
    return;
  }
  els.name.focus();
  els.hint.textContent = "Name the region, then Save";
}

async function saveDraw() {
  const name = els.name.value.trim();
  if (!name) { flash("name required", true); els.name.focus(); return; }
  if (state.draw.points.length < 3) { flash("need at least 3 points", true); return; }
  try {
    await sendJSON("POST", "/api/rois", {
      camera: state.cam.name, name, polygon: state.draw.points,
    });
    cancelDraw();
    await loadRois();
    flash(`saved “${name}”`);
  } catch (e) { flash(`save failed: ${e.message}`, true); }
}

// --------------------------------------------------------------- ROIs
async function loadRois() {
  state.rois = await fetchJSON(`/api/rois?cam=${encodeURIComponent(state.cam.name)}`);
  renderRoiList();
  populateRuleRoiSelect();
  render();
}

function renderRoiList() {
  els.roiList.innerHTML = "";
  els.roiEmpty.hidden = state.rois.length > 0;
  state.rois.forEach((roi, i) => {
    const li = els.roiItemTpl.content.firstElementChild.cloneNode(true);
    const swatch = li.querySelector(".roi-swatch");
    swatch.style.background = colorFor(i);
    swatch.style.opacity = state.hidden.has(roi.id) ? "0.25" : "1";
    swatch.addEventListener("click", () => {
      if (state.hidden.has(roi.id)) state.hidden.delete(roi.id);
      else state.hidden.add(roi.id);
      renderRoiList(); render();
    });
    li.querySelector(".roi-item-name").textContent = roi.name;
    const cb = li.querySelector(".roi-enabled input");
    cb.checked = roi.enabled;
    cb.addEventListener("change", async () => {
      try {
        await sendJSON("PUT", `/api/rois/${roi.id}`, { enabled: cb.checked });
        roi.enabled = cb.checked; render();
      } catch (e) { flash(`update failed: ${e.message}`, true); cb.checked = roi.enabled; }
    });
    li.querySelector(".roi-del").addEventListener("click", async () => {
      if (!confirm(`Delete region “${roi.name}” and its rules?`)) return;
      try {
        await sendJSON("DELETE", `/api/rois/${roi.id}`);
        await loadRois(); await loadRules();
        flash(`deleted “${roi.name}”`);
      } catch (e) { flash(`delete failed: ${e.message}`, true); }
    });
    els.roiList.appendChild(li);
  });
}

// --------------------------------------------------------------- rules
function populateRuleRoiSelect() {
  els.ruleRoi.innerHTML = "";
  for (const roi of state.rois) {
    const o = document.createElement("option");
    o.value = roi.id;
    o.textContent = roi.name;
    els.ruleRoi.appendChild(o);
  }
}

function roiName(id) {
  const r = state.rois.find((x) => x.id === id);
  return r ? r.name : `#${id}`;
}

async function loadRules() {
  state.rules = await fetchJSON(`/api/alert-rules?cam=${encodeURIComponent(state.cam.name)}`);
  renderRuleList();
}

function renderRuleList() {
  els.ruleList.innerHTML = "";
  els.ruleEmpty.hidden = state.rules.length > 0;
  for (const rule of state.rules) {
    const li = els.ruleItemTpl.content.firstElementChild.cloneNode(true);
    const conf = rule.min_conf > 0 ? ` ≥${rule.min_conf}` : "";
    li.querySelector(".rule-desc").textContent =
      `${rule.class}${conf} in “${roiName(rule.roi_id)}” · ${rule.cooldown_s}s`;
    const cb = li.querySelector(".roi-enabled input");
    cb.checked = rule.enabled;
    cb.addEventListener("change", async () => {
      try {
        await sendJSON("PUT", `/api/alert-rules/${rule.id}`, { enabled: cb.checked });
        rule.enabled = cb.checked;
      } catch (e) { flash(`update failed: ${e.message}`, true); cb.checked = rule.enabled; }
    });
    li.querySelector(".rule-del").addEventListener("click", async () => {
      try {
        await sendJSON("DELETE", `/api/alert-rules/${rule.id}`);
        await loadRules();
      } catch (e) { flash(`delete failed: ${e.message}`, true); }
    });
    els.ruleList.appendChild(li);
  }
}

async function addRule(ev) {
  ev.preventDefault();
  if (!state.rois.length) { flash("create a region first", true); return; }
  try {
    await sendJSON("POST", "/api/alert-rules", {
      camera: state.cam.name,
      roi_id: parseInt(els.ruleRoi.value, 10),
      class: els.ruleClass.value,
      min_conf: parseFloat(els.ruleConf.value) || 0,
      cooldown_s: parseInt(els.ruleCooldown.value, 10) || 0,
    });
    await loadRules();
    flash("rule added");
  } catch (e) { flash(`add rule failed: ${e.message}`, true); }
}

// --------------------------------------------------------------- camera switch
async function selectCamera(name) {
  state.cam = state.cams.find((c) => c.name === name) || state.cams[0];
  if (!state.cam) return;
  state.hidden.clear();
  cancelDraw();
  els.camSelect.value = state.cam.name;
  const url = new URL(window.location);
  url.searchParams.set("cam", state.cam.name);
  history.replaceState(null, "", url);
  attachVideo(state.cam);
  await Promise.all([loadRois(), loadRules()]);
}

// --------------------------------------------------------------- init
async function init() {
  try {
    [state.cams, state.classes] = await Promise.all([
      fetchJSON("/api/cameras"),
      fetchJSON("/api/alert-classes"),
    ]);
  } catch (e) { flash(`load failed: ${e.message}`, true); return; }

  state.cams = state.cams.filter((c) => c.enabled);
  for (const c of state.cams) {
    const o = document.createElement("option");
    o.value = c.name; o.textContent = c.label;
    els.camSelect.appendChild(o);
  }
  for (const cls of state.classes) {
    const o = document.createElement("option");
    o.value = cls; o.textContent = cls;
    els.ruleClass.appendChild(o);
  }

  els.camSelect.addEventListener("change", () => selectCamera(els.camSelect.value));
  els.freeze.addEventListener("click", () => {
    if (els.video.paused) { els.video.play().catch(() => {}); els.freeze.textContent = "Freeze frame"; }
    else { els.video.pause(); els.freeze.textContent = "Resume"; }
  });
  els.draw.addEventListener("click", startDraw);
  els.finish.addEventListener("click", finishDraw);
  els.cancel.addEventListener("click", cancelDraw);
  els.saveRoi.addEventListener("click", saveDraw);
  els.ruleForm.addEventListener("submit", addRule);

  els.canvas.addEventListener("click", (ev) => {
    if (!state.draw.active) return;
    state.draw.points.push(pointFromEvent(ev));
    render();
  });
  els.canvas.addEventListener("dblclick", (ev) => {
    if (state.draw.active) { ev.preventDefault(); finishDraw(); }
  });
  document.addEventListener("keydown", (ev) => {
    if (!state.draw.active) return;
    if (ev.key === "Enter") { ev.preventDefault(); finishDraw(); }
    else if (ev.key === "Escape") { cancelDraw(); }
    else if (ev.key === "Backspace" && document.activeElement !== els.name) {
      ev.preventDefault();
      state.draw.points.pop();
      render();
    }
  });

  new ResizeObserver(resizeCanvas).observe(els.wrap);
  resizeCanvas();

  const wanted = new URL(window.location).searchParams.get("cam");
  await selectCamera(wanted || (state.cams[0] && state.cams[0].name));
}

init();
