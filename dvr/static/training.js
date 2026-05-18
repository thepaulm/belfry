"use strict";

// In-browser bounding-box labeler over the /staging tree. Mirrors the
// "click-drag to draw, click to select, drag handles to resize, delete
// key to remove" semantics labelImg has, but renders against the
// existing belfry style and saves through the staging-CRUD endpoints
// added to dvr/server.py.
//
// All box coordinates are stored normalized 0..1 (x1, y1, x2, y2) so
// they round-trip through the YOLO `<cls_id> <cx> <cy> <w> <h>` format
// without ever caring what the image's pixel size is.

// Class palette — mirror overlay.js where possible, fall back through
// a palette indexed by class id for anything new (deer, raccoon, …).
const CLASS_COLOR = {
  person:  "#4ea1ff",
  dog:     "#5ad17c",
  cat:     "#5ad17c",
  bird:    "#5ad17c",
  animal:  "#5ad17c",
  car:     "#ff9b3f",
  truck:   "#ff9b3f",
  vehicle: "#ff9b3f",
  deer:    "#ffd23f",
};
const FALLBACK_PALETTE = ["#4ea1ff", "#5ad17c", "#ff9b3f", "#ffd23f", "#e879f9", "#7ed4ff", "#ff7a7a"];

function colorFor(clsName, clsId) {
  if (CLASS_COLOR[clsName]) return CLASS_COLOR[clsName];
  if (clsId == null) return "#aaa";
  return FALLBACK_PALETTE[clsId % FALLBACK_PALETTE.length];
}

// Minimum box edge length in normalized coordinates; smaller drags
// are treated as accidental clicks and discarded.
const MIN_BOX_NORM = 0.01;

// Display-pixel tolerance for corner-handle hit testing.
const HANDLE_HIT_PX = 10;
const HANDLE_DRAW_PX = 6;

const els = {
  counts:        document.getElementById("counts"),
  status:        document.getElementById("status"),
  categoryChips: document.getElementById("category-chips"),
  stateChips:    document.getElementById("state-chips"),
  imageList:     document.getElementById("image-list"),
  railImageLbl:  document.getElementById("rail-image-label"),
  canvas:        document.getElementById("label-canvas"),
  image:         document.getElementById("label-image"),
  stageEmpty:    document.getElementById("stage-empty"),
  newBoxClass:   document.getElementById("new-box-class"),
  selSection:    document.getElementById("selected-section"),
  selClass:      document.getElementById("selected-class"),
  delBoxBtn:     document.getElementById("delete-box-btn"),
  saveBtn:       document.getElementById("save-btn"),
  promoteBtn:    document.getElementById("promote-btn"),
  trashBtn:      document.getElementById("trash-btn"),
  prevBtn:       document.getElementById("prev-btn"),
  nextBtn:       document.getElementById("next-btn"),
  imageMeta:     document.getElementById("image-meta"),
};

const state = {
  classes: [],           // id_to_name
  allImages: [],         // [{category, filename, has_label, cam, ts}]
  filtered: [],          // current filter result
  currentIdx: -1,        // index into filtered
  boxes: [],             // current image's boxes; normalized
  selectedBox: -1,
  dirty: false,
  newBoxClassId: 0,
  filter: { categories: new Set(), state: "all" },  // empty cats = all
  // Pointer interaction
  drag: null,            // {mode: "draw"|"move"|"resize", ...}
};

// ---------- API ----------

async function fetchStaging() {
  const r = await fetch("/api/training/staging", { credentials: "same-origin" });
  if (!r.ok) throw new Error(`staging ${r.status}`);
  return r.json();
}

async function fetchLabel(category, filename) {
  const r = await fetch(
    `/api/training/label/${encodeURIComponent(category)}/${encodeURIComponent(filename)}`,
    { credentials: "same-origin" },
  );
  if (!r.ok) throw new Error(`label ${r.status}`);
  return r.text();
}

async function putLabel(category, filename, body) {
  const r = await fetch(
    `/api/training/label/${encodeURIComponent(category)}/${encodeURIComponent(filename)}`,
    {
      method: "PUT",
      body: body,
      headers: { "Content-Type": "text/plain" },
      credentials: "same-origin",
    },
  );
  if (!r.ok) throw new Error(`save ${r.status}: ${await r.text()}`);
  return r.json();
}

async function promoteCurrent(category, filename) {
  const r = await fetch(
    `/api/training/promote/${encodeURIComponent(category)}/${encodeURIComponent(filename)}`,
    { method: "POST", credentials: "same-origin" },
  );
  if (!r.ok) throw new Error(`promote ${r.status}: ${await r.text()}`);
  return r.json();
}

async function trashCurrent(category, filename) {
  const r = await fetch(
    `/api/training/staging/${encodeURIComponent(category)}/${encodeURIComponent(filename)}`,
    { method: "DELETE", credentials: "same-origin" },
  );
  if (!r.ok) throw new Error(`trash ${r.status}: ${await r.text()}`);
  return r.json();
}

// ---------- YOLO parse / serialize ----------

function parseYolo(text) {
  const out = [];
  for (const raw of (text || "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const p = line.split(/\s+/);
    if (p.length !== 5) continue;
    const cls = parseInt(p[0], 10);
    const cx = parseFloat(p[1]);
    const cy = parseFloat(p[2]);
    const w  = parseFloat(p[3]);
    const h  = parseFloat(p[4]);
    if ([cls, cx, cy, w, h].some(v => !Number.isFinite(v))) continue;
    out.push({
      cls_id: cls,
      x1: Math.max(0, cx - w / 2),
      y1: Math.max(0, cy - h / 2),
      x2: Math.min(1, cx + w / 2),
      y2: Math.min(1, cy + h / 2),
    });
  }
  return out;
}

function serializeYolo(boxes) {
  return boxes.map(b => {
    const cx = (b.x1 + b.x2) / 2;
    const cy = (b.y1 + b.y2) / 2;
    const w  = Math.max(0, b.x2 - b.x1);
    const h  = Math.max(0, b.y2 - b.y1);
    return `${b.cls_id} ${cx.toFixed(6)} ${cy.toFixed(6)} ${w.toFixed(6)} ${h.toFixed(6)}`;
  }).join("\n");
}

// ---------- Filter / list rendering ----------

function applyFilter() {
  const { categories, state: stateFilter } = state.filter;
  state.filtered = state.allImages.filter(im => {
    if (categories.size && !categories.has(im.category)) return false;
    if (stateFilter === "unlabeled" && im.has_label) return false;
    if (stateFilter === "labeled" && !im.has_label) return false;
    return true;
  });
}

function renderImageList() {
  els.imageList.innerHTML = "";
  for (let i = 0; i < state.filtered.length; i++) {
    const im = state.filtered[i];
    const li = document.createElement("li");
    if (i === state.currentIdx) li.classList.add("active");
    const dot = document.createElement("span");
    dot.className = "il-state " + (im.has_label ? "labeled" : "unlabeled");
    dot.textContent = im.has_label ? "✓" : "•";
    const name = document.createElement("span");
    name.className = "il-name";
    name.textContent = im.filename;
    name.title = `${im.category}/${im.filename}`;
    li.appendChild(dot);
    li.appendChild(name);
    li.addEventListener("click", () => loadAt(i));
    els.imageList.appendChild(li);
  }
  els.railImageLbl.textContent = `Images · ${state.filtered.length}`;
}

function renderCategoryChips() {
  els.categoryChips.innerHTML = "";
  const cats = new Set(state.allImages.map(im => im.category));
  // Sort: positive classes first by name, then negatives.
  const order = [...cats].sort((a, b) => {
    const an = a.startsWith("negative_") ? 1 : 0;
    const bn = b.startsWith("negative_") ? 1 : 0;
    if (an !== bn) return an - bn;
    return a.localeCompare(b);
  });
  for (const cat of order) {
    const btn = document.createElement("button");
    btn.className = "chip";
    if (state.filter.categories.has(cat)) btn.classList.add("active");
    btn.textContent = cat;
    btn.dataset.category = cat;
    btn.addEventListener("click", () => {
      if (state.filter.categories.has(cat)) state.filter.categories.delete(cat);
      else state.filter.categories.add(cat);
      btn.classList.toggle("active");
      refilter();
    });
    els.categoryChips.appendChild(btn);
  }
}

function refilter() {
  const prevKey = currentKey();
  applyFilter();
  // Try to preserve the focused image across filter changes.
  if (prevKey) {
    const idx = state.filtered.findIndex(im => keyOf(im) === prevKey);
    state.currentIdx = idx >= 0 ? idx : 0;
  } else {
    state.currentIdx = state.filtered.length ? 0 : -1;
  }
  renderImageList();
  loadAt(state.currentIdx);
}

function keyOf(im) {
  return im ? `${im.category}/${im.filename}` : null;
}

function currentKey() {
  if (state.currentIdx < 0) return null;
  return keyOf(state.filtered[state.currentIdx]);
}

// ---------- Image loading ----------

async function loadAt(idx) {
  if (state.dirty && state.currentIdx !== idx && state.currentIdx >= 0) {
    // Silent autosave before navigating away so the user doesn't lose
    // edits if they click the wrong list item. Errors are surfaced.
    try {
      await saveCurrent({ silent: true });
    } catch (e) {
      els.status.textContent = `autosave failed: ${e.message}`;
      return;
    }
  }
  state.currentIdx = idx;
  state.boxes = [];
  state.selectedBox = -1;
  state.dirty = false;

  if (idx < 0 || idx >= state.filtered.length) {
    els.image.hidden = true;
    els.image.src = "";
    els.stageEmpty.hidden = false;
    els.imageMeta.textContent = "";
    drawCanvas();
    updateButtons();
    return;
  }

  const im = state.filtered[idx];
  els.stageEmpty.hidden = true;
  els.image.hidden = false;
  // Pre-fill the new-box class dropdown from the folder name when the
  // hint is a positive class. Negative folders carry no implied class
  // for *new* boxes — the user is presumably labeling something other
  // than the negated class — so we leave the dropdown as-is.
  if (!im.category.startsWith("negative_")) {
    const guessId = state.classes.indexOf(im.category);
    if (guessId >= 0) {
      state.newBoxClassId = guessId;
      els.newBoxClass.value = String(guessId);
    }
  }

  // Start the image fetch and the label fetch in parallel.
  const imgUrl = `/api/training/image/${encodeURIComponent(im.category)}/${encodeURIComponent(im.filename)}`;
  els.image.onload = () => {
    sizeCanvas();
    drawCanvas();
  };
  els.image.onerror = () => {
    els.status.textContent = `image load failed: ${im.filename}`;
  };
  els.image.src = imgUrl;

  els.imageMeta.textContent = `${im.category} · ${im.filename}`
    + (im.ts ? ` · ${new Date(im.ts * 1000).toLocaleString()}` : "");

  try {
    const text = await fetchLabel(im.category, im.filename);
    state.boxes = parseYolo(text);
    drawCanvas();
  } catch (e) {
    els.status.textContent = `label fetch failed: ${e.message}`;
  }

  renderImageList();
  updateButtons();
}

function sizeCanvas() {
  const w = els.image.clientWidth;
  const h = els.image.clientHeight;
  els.canvas.width = w;
  els.canvas.height = h;
}

// ---------- Drawing ----------

function classNameOf(cls_id) {
  return state.classes[cls_id] || `cls${cls_id}`;
}

function drawCanvas() {
  const c = els.canvas;
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  if (!state.filtered.length || state.currentIdx < 0) return;

  for (let i = 0; i < state.boxes.length; i++) {
    drawBox(ctx, state.boxes[i], i === state.selectedBox);
  }

  // In-flight drag rectangle
  if (state.drag && state.drag.mode === "draw" && state.drag.cur) {
    const a = state.drag.anchor, b = state.drag.cur;
    const x = Math.min(a.x, b.x) * c.width;
    const y = Math.min(a.y, b.y) * c.height;
    const w = Math.abs(b.x - a.x) * c.width;
    const h = Math.abs(b.y - a.y) * c.height;
    ctx.strokeStyle = colorFor(classNameOf(state.newBoxClassId), state.newBoxClassId);
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);
  }
}

function drawBox(ctx, box, selected) {
  const c = els.canvas;
  const x = box.x1 * c.width;
  const y = box.y1 * c.height;
  const w = (box.x2 - box.x1) * c.width;
  const h = (box.y2 - box.y1) * c.height;
  const color = colorFor(classNameOf(box.cls_id), box.cls_id);

  ctx.lineWidth = selected ? 3 : 2;
  ctx.strokeStyle = color;
  ctx.strokeRect(x, y, w, h);

  // Class label chip
  const label = classNameOf(box.cls_id);
  ctx.font = "12px system-ui, sans-serif";
  const padX = 4, padY = 2;
  const tw = ctx.measureText(label).width + padX * 2;
  const th = 16;
  let cy = y - th - 2;
  if (cy < 0) cy = y + 2;
  ctx.fillStyle = color;
  ctx.fillRect(x, cy, tw, th);
  ctx.fillStyle = "#000";
  ctx.textBaseline = "top";
  ctx.fillText(label, x + padX, cy + padY + 1);

  if (selected) {
    // Four corner handles
    ctx.fillStyle = color;
    const corners = [
      [x, y], [x + w, y], [x, y + h], [x + w, y + h],
    ];
    for (const [cx, cy2] of corners) {
      ctx.fillRect(cx - HANDLE_DRAW_PX, cy2 - HANDLE_DRAW_PX,
                   HANDLE_DRAW_PX * 2, HANDLE_DRAW_PX * 2);
    }
  }
}

// ---------- Pointer handling ----------

function canvasNormPos(e) {
  const r = els.canvas.getBoundingClientRect();
  const x = (e.clientX - r.left) / r.width;
  const y = (e.clientY - r.top) / r.height;
  return { x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y)) };
}

function pointInBox(p, b) {
  return p.x >= b.x1 && p.x <= b.x2 && p.y >= b.y1 && p.y <= b.y2;
}

function topBoxAt(p) {
  // Last drawn is topmost; iterate reverse.
  for (let i = state.boxes.length - 1; i >= 0; i--) {
    if (pointInBox(p, state.boxes[i])) return i;
  }
  return -1;
}

function handleAt(p, b) {
  // Returns "tl"|"tr"|"bl"|"br" or null.
  const tolX = HANDLE_HIT_PX / els.canvas.width;
  const tolY = HANDLE_HIT_PX / els.canvas.height;
  const corners = {
    tl: { x: b.x1, y: b.y1 },
    tr: { x: b.x2, y: b.y1 },
    bl: { x: b.x1, y: b.y2 },
    br: { x: b.x2, y: b.y2 },
  };
  for (const [k, c] of Object.entries(corners)) {
    if (Math.abs(p.x - c.x) <= tolX && Math.abs(p.y - c.y) <= tolY) return k;
  }
  return null;
}

function onPointerDown(e) {
  if (e.button !== 0) return;
  if (state.currentIdx < 0) return;
  els.canvas.setPointerCapture(e.pointerId);
  const p = canvasNormPos(e);

  // 1) Hit a handle of the currently-selected box?
  if (state.selectedBox >= 0) {
    const sel = state.boxes[state.selectedBox];
    const handle = handleAt(p, sel);
    if (handle) {
      state.drag = { mode: "resize", handle, boxIdx: state.selectedBox };
      return;
    }
  }
  // 2) Click on any box body → select it, prepare to move.
  const boxIdx = topBoxAt(p);
  if (boxIdx >= 0) {
    state.selectedBox = boxIdx;
    const b = state.boxes[boxIdx];
    state.drag = { mode: "move", boxIdx, dx: p.x - b.x1, dy: p.y - b.y1 };
    updateSelectedUI();
    drawCanvas();
    return;
  }
  // 3) Empty space → start drawing a new box.
  state.selectedBox = -1;
  updateSelectedUI();
  state.drag = { mode: "draw", anchor: p, cur: p };
  drawCanvas();
}

function onPointerMove(e) {
  const p = canvasNormPos(e);
  if (!state.drag) {
    updateHoverCursor(p);
    return;
  }
  if (state.drag.mode === "draw") {
    state.drag.cur = p;
    drawCanvas();
  } else if (state.drag.mode === "move") {
    const b = state.boxes[state.drag.boxIdx];
    const w = b.x2 - b.x1, h = b.y2 - b.y1;
    const nx1 = Math.max(0, Math.min(1 - w, p.x - state.drag.dx));
    const ny1 = Math.max(0, Math.min(1 - h, p.y - state.drag.dy));
    b.x1 = nx1; b.y1 = ny1;
    b.x2 = nx1 + w; b.y2 = ny1 + h;
    state.dirty = true;
    drawCanvas();
  } else if (state.drag.mode === "resize") {
    const b = state.boxes[state.drag.boxIdx];
    const h = state.drag.handle;
    if (h === "tl") { b.x1 = p.x; b.y1 = p.y; }
    else if (h === "tr") { b.x2 = p.x; b.y1 = p.y; }
    else if (h === "bl") { b.x1 = p.x; b.y2 = p.y; }
    else if (h === "br") { b.x2 = p.x; b.y2 = p.y; }
    // Normalize so x1<x2, y1<y2 (and flip the handle name if dragged past).
    if (b.x1 > b.x2) { const t = b.x1; b.x1 = b.x2; b.x2 = t;
      state.drag.handle = h.replace("l", "X").replace("r", "l").replace("X", "r"); }
    if (b.y1 > b.y2) { const t = b.y1; b.y1 = b.y2; b.y2 = t;
      state.drag.handle = h.replace("t", "X").replace("b", "t").replace("X", "b"); }
    state.dirty = true;
    drawCanvas();
  }
}

function onPointerUp(_e) {
  if (!state.drag) return;
  if (state.drag.mode === "draw" && state.drag.cur) {
    const a = state.drag.anchor, b = state.drag.cur;
    const x1 = Math.min(a.x, b.x), x2 = Math.max(a.x, b.x);
    const y1 = Math.min(a.y, b.y), y2 = Math.max(a.y, b.y);
    if (x2 - x1 >= MIN_BOX_NORM && y2 - y1 >= MIN_BOX_NORM) {
      state.boxes.push({ cls_id: state.newBoxClassId, x1, y1, x2, y2 });
      state.selectedBox = state.boxes.length - 1;
      state.dirty = true;
      updateSelectedUI();
    }
  }
  state.drag = null;
  drawCanvas();
  updateButtons();
}

function updateHoverCursor(p) {
  els.canvas.classList.remove("over-handle", "over-box");
  if (state.selectedBox >= 0) {
    const sel = state.boxes[state.selectedBox];
    if (handleAt(p, sel)) {
      els.canvas.classList.add("over-handle");
      return;
    }
  }
  if (topBoxAt(p) >= 0) {
    els.canvas.classList.add("over-box");
  }
}

// ---------- Tools panel ----------

function populateClassDropdowns() {
  for (const sel of [els.newBoxClass, els.selClass]) {
    sel.innerHTML = "";
    state.classes.forEach((name, id) => {
      const opt = document.createElement("option");
      opt.value = String(id);
      opt.textContent = `${id}: ${name}`;
      sel.appendChild(opt);
    });
  }
  els.newBoxClass.value = String(state.newBoxClassId);
}

function updateSelectedUI() {
  if (state.selectedBox < 0) {
    els.selSection.hidden = true;
    return;
  }
  els.selSection.hidden = false;
  els.selClass.value = String(state.boxes[state.selectedBox].cls_id);
}

function updateButtons() {
  const hasImage = state.currentIdx >= 0;
  els.saveBtn.disabled = !hasImage;
  els.promoteBtn.disabled = !hasImage;
  els.trashBtn.disabled = !hasImage;
  els.prevBtn.disabled = state.currentIdx <= 0;
  els.nextBtn.disabled = state.currentIdx < 0 || state.currentIdx >= state.filtered.length - 1;
  const labeled = state.filtered.filter(im => im.has_label).length;
  const dirty = state.dirty ? " ●" : "";
  els.counts.textContent = `${labeled}/${state.filtered.length} labeled${dirty}`;
}

function deleteSelectedBox() {
  if (state.selectedBox < 0) return;
  state.boxes.splice(state.selectedBox, 1);
  state.selectedBox = -1;
  state.dirty = true;
  updateSelectedUI();
  drawCanvas();
}

async function saveCurrent({ silent = false } = {}) {
  if (state.currentIdx < 0) return;
  const im = state.filtered[state.currentIdx];
  const body = serializeYolo(state.boxes);
  await putLabel(im.category, im.filename, body);
  state.dirty = false;
  if (!im.has_label) {
    im.has_label = true;
    // Reflect in the master list too so filter toggles stay correct.
    const master = state.allImages.find(x => keyOf(x) === keyOf(im));
    if (master) master.has_label = true;
    renderImageList();
  }
  updateButtons();
  if (!silent) {
    els.status.textContent = `saved ${im.filename}` + (state.boxes.length ? ` (${state.boxes.length} boxes)` : " (empty)");
  }
}

async function promoteCurrentBtn() {
  if (state.currentIdx < 0) return;
  const im = state.filtered[state.currentIdx];
  try {
    await saveCurrent({ silent: true });
    await promoteCurrent(im.category, im.filename);
  } catch (e) {
    els.status.textContent = `promote failed: ${e.message}`;
    return;
  }
  removeCurrentFromList();
  els.status.textContent = `promoted ${im.filename}`;
}

async function trashCurrentBtn() {
  if (state.currentIdx < 0) return;
  const im = state.filtered[state.currentIdx];
  if (!confirm(`Trash ${im.category}/${im.filename}?\nThis deletes the image and any labels.`)) return;
  try {
    await trashCurrent(im.category, im.filename);
  } catch (e) {
    els.status.textContent = `trash failed: ${e.message}`;
    return;
  }
  removeCurrentFromList();
  els.status.textContent = `trashed ${im.filename}`;
}

function removeCurrentFromList() {
  const im = state.filtered[state.currentIdx];
  state.allImages = state.allImages.filter(x => keyOf(x) !== keyOf(im));
  state.filtered.splice(state.currentIdx, 1);
  if (state.currentIdx >= state.filtered.length) state.currentIdx = state.filtered.length - 1;
  state.boxes = [];
  state.selectedBox = -1;
  state.dirty = false;
  renderImageList();
  renderCategoryChips();
  loadAt(state.currentIdx);
}

function nextImage() {
  if (state.currentIdx < state.filtered.length - 1) loadAt(state.currentIdx + 1);
}
function prevImage() {
  if (state.currentIdx > 0) loadAt(state.currentIdx - 1);
}

// ---------- Wiring ----------

function wireCanvas() {
  els.canvas.addEventListener("pointerdown", onPointerDown);
  els.canvas.addEventListener("pointermove", onPointerMove);
  els.canvas.addEventListener("pointerup", onPointerUp);
  els.canvas.addEventListener("pointercancel", onPointerUp);
}

function wireTools() {
  els.newBoxClass.addEventListener("change", () => {
    state.newBoxClassId = parseInt(els.newBoxClass.value, 10);
  });
  els.selClass.addEventListener("change", () => {
    if (state.selectedBox < 0) return;
    state.boxes[state.selectedBox].cls_id = parseInt(els.selClass.value, 10);
    state.dirty = true;
    drawCanvas();
  });
  els.delBoxBtn.addEventListener("click", deleteSelectedBox);
  els.saveBtn.addEventListener("click", () => saveCurrent().catch(e => {
    els.status.textContent = `save failed: ${e.message}`;
  }));
  els.promoteBtn.addEventListener("click", promoteCurrentBtn);
  els.trashBtn.addEventListener("click", trashCurrentBtn);
  els.prevBtn.addEventListener("click", prevImage);
  els.nextBtn.addEventListener("click", nextImage);

  for (const btn of els.stateChips.querySelectorAll("button[data-state]")) {
    btn.addEventListener("click", () => {
      for (const b of els.stateChips.querySelectorAll("button")) b.classList.remove("active");
      btn.classList.add("active");
      state.filter.state = btn.dataset.state;
      refilter();
    });
  }
}

function wireKeyboard() {
  window.addEventListener("keydown", (e) => {
    // Don't fight typing in selects.
    const tag = e.target && e.target.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    switch (e.key) {
      case "j": case "ArrowDown": case "ArrowRight":
        e.preventDefault(); nextImage(); break;
      case "k": case "ArrowUp": case "ArrowLeft":
        e.preventDefault(); prevImage(); break;
      case "s":
        e.preventDefault(); saveCurrent().catch(err => {
          els.status.textContent = `save failed: ${err.message}`;
        }); break;
      case "p":
        e.preventDefault(); promoteCurrentBtn(); break;
      case "x":
        e.preventDefault(); trashCurrentBtn(); break;
      case "Delete": case "Backspace":
        if (state.selectedBox >= 0) {
          e.preventDefault();
          deleteSelectedBox();
        }
        break;
      case "Escape":
        if (state.selectedBox >= 0) {
          state.selectedBox = -1;
          updateSelectedUI();
          drawCanvas();
        }
        break;
    }
  });
}

function wireResize() {
  window.addEventListener("resize", () => {
    if (!els.image.hidden && els.image.complete) {
      sizeCanvas();
      drawCanvas();
    }
  });
}

async function init() {
  wireCanvas();
  wireTools();
  wireKeyboard();
  wireResize();
  try {
    const data = await fetchStaging();
    state.classes = data.classes || [];
    state.allImages = data.images || [];
    populateClassDropdowns();
    renderCategoryChips();
    applyFilter();
    state.currentIdx = state.filtered.length ? 0 : -1;
    renderImageList();
    if (state.currentIdx >= 0) loadAt(0);
    else updateButtons();
    els.status.textContent = "";
  } catch (e) {
    els.status.textContent = `init failed: ${e.message}`;
  }
}

init();
