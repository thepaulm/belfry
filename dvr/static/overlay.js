// Live bounding-box overlay. Layers a <canvas> over a <video>, opens
// an SSE connection to /api/inference/live?cam=X, and draws each
// detection batch as the inference process emits it.
//
// Boxes arrive as compact arrays: [x1, y1, x2, y2, class, conf] in
// normalized 0..1 coords. We multiply by the canvas's pixel size, so
// the rendering scales correctly even when the video is letterboxed
// (we use the .video-wrap's bounding box, not the video's intrinsic
// dimensions).
//
// The "Show labels" header toggle adds/removes a body class —
// canvas visibility is purely CSS — so flipping it doesn't tear down
// the SSE connection.

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
const DEFAULT_COLOR = "#aaa";

function colorFor(cls) {
  return CLASS_COLOR[cls] || DEFAULT_COLOR;
}

class BoxOverlay {
  // host: a parent element (typically .video-wrap) that already has
  // position: relative. cam: camera name. The canvas inherits the
  // host's dimensions via 100%/100% and we pull pixel size off
  // getBoundingClientRect on each draw + on resize.
  constructor(host, cam) {
    this.host = host;
    this.cam = cam;
    this.canvas = document.createElement("canvas");
    this.canvas.className = "overlay-canvas";
    this.host.appendChild(this.canvas);
    this.ctx = this.canvas.getContext("2d");
    this._es = null;
    this._ro = new ResizeObserver(() => this._resizeToHost());
    this._ro.observe(this.host);
    this._resizeToHost();
    this._connect();
  }

  destroy() {
    if (this._es) {
      this._es.close();
      this._es = null;
    }
    if (this._ro) {
      this._ro.disconnect();
      this._ro = null;
    }
    if (this.canvas && this.canvas.parentElement) {
      this.canvas.parentElement.removeChild(this.canvas);
    }
  }

  _resizeToHost() {
    const r = this.host.getBoundingClientRect();
    // Round to whole px so the canvas's drawing buffer matches the
    // CSS pixel size and box edges stay crisp.
    const w = Math.max(1, Math.floor(r.width));
    const h = Math.max(1, Math.floor(r.height));
    if (this.canvas.width !== w) this.canvas.width = w;
    if (this.canvas.height !== h) this.canvas.height = h;
  }

  _connect() {
    const url = `/api/inference/live?cam=${encodeURIComponent(this.cam)}`;
    this._es = new EventSource(url, { withCredentials: true });
    this._es.onmessage = (ev) => this._onMessage(ev);
    this._es.onerror = () => {
      // EventSource auto-reconnects with backoff. Just clear the
      // canvas so a stale set of boxes doesn't linger if the upstream
      // restarted.
      this._clear();
    };
  }

  _onMessage(ev) {
    let payload;
    try {
      payload = JSON.parse(ev.data);
    } catch {
      return;
    }
    this._draw(payload.boxes || []);
  }

  _clear() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
  }

  _draw(boxes) {
    const w = this.canvas.width;
    const h = this.canvas.height;
    this._clear();
    this.ctx.lineWidth = 2;
    this.ctx.font = "12px -apple-system, system-ui, sans-serif";
    this.ctx.textBaseline = "top";

    for (const b of boxes) {
      const [x1, y1, x2, y2, cls, conf] = b;
      const px1 = x1 * w, py1 = y1 * h;
      const pw = (x2 - x1) * w, ph = (y2 - y1) * h;
      const color = colorFor(cls);

      this.ctx.strokeStyle = color;
      this.ctx.strokeRect(px1, py1, pw, ph);

      // Label chip above the box (or below if it'd clip the top).
      const label = `${cls} ${conf.toFixed(2)}`;
      const metrics = this.ctx.measureText(label);
      const labelW = Math.ceil(metrics.width) + 8;
      const labelH = 16;
      const labelY = py1 - labelH >= 0 ? py1 - labelH : py1;
      this.ctx.fillStyle = color;
      this.ctx.fillRect(px1, labelY, labelW, labelH);
      this.ctx.fillStyle = "#06121f";
      this.ctx.fillText(label, px1 + 4, labelY + 2);
    }
  }
}

// "Show labels" toggle wiring shared across pages. CSS rule on
// body.labels-on .overlay-canvas controls visibility; the SSE
// connection is unaffected so flipping the toggle is instant.
function wireLabelsToggle() {
  const btn = document.getElementById("labels-toggle");
  if (!btn) return;
  const initial = localStorage.getItem("belfry-labels") === "on";
  document.body.classList.toggle("labels-on", initial);
  btn.setAttribute("aria-pressed", String(initial));
  btn.classList.toggle("active", initial);
  btn.addEventListener("click", () => {
    const on = !document.body.classList.contains("labels-on");
    document.body.classList.toggle("labels-on", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.classList.toggle("active", on);
    localStorage.setItem("belfry-labels", on ? "on" : "off");
  });
}

// Expose to non-module callers (viewer.js / playback.js).
window.BoxOverlay = BoxOverlay;
window.wireLabelsToggle = wireLabelsToggle;
// Auto-wire on DOMContentLoaded so each page just needs to include
// overlay.js and have a #labels-toggle button.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", wireLabelsToggle);
} else {
  wireLabelsToggle();
}
