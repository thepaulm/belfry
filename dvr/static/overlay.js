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

// Registry of live overlays so the labels-toggle can fan out start /
// stop calls. We open the SSE connection lazily — only while the
// "Show labels" toggle is on — because each EventSource holds a
// dedicated TCP connection, and at 4–8 tiles we'd otherwise exhaust
// the browser's HTTP/1.1 per-origin limit (6) and starve the HLS
// playlist + segment requests, breaking video playback.
const _overlays = new Set();

// Per-page counter so we can stagger SSE opens across multiple
// overlays. Resets on full navigation (each page reload starts at 0).
// We stagger by 300 ms per overlay; with 4 cams that's 0/300/600/900
// added on top of any base delay, which gives the browser a chance to
// recycle HTTP/1.1 connection slots between SSE attaches and HLS
// playlist+segment fetches.
let _overlayCounter = 0;
const _STAGGER_MS = 300;

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
    this._startTimer = null;
    this._index = _overlayCounter++;
    // "live" (default — live tiles + playback live mode) or "past"
    // (playback past mode, set by subscribePast). The mode persists
    // across stop()/start() cycles so labels-off → labels-on resumes
    // the right subscription.
    this._mode = "live";
    this._pastUrl = null;
    this._pastVideo = null;
    this._pastWindowStartUnix = null;
    this._pastSamples = null;
    this._pastListener = null;
    this._ro = new ResizeObserver(() => this._resizeToHost());
    this._ro.observe(this.host);
    this._resizeToHost();
    _overlays.add(this);
    // Construction-time SSE auto-start is intentionally OFF. With
    // localStorage previously persisting labels-on, every fresh page
    // load (including set-nav between /sets/set1 and /sets/set2) was
    // racing the SSE attach against HLS playlist+segment fetches for
    // the browser's HTTP/1.1 per-origin slots; sometimes HLS lost
    // and videos went blank. Now SSE only opens when the user
    // explicitly clicks the labels toggle this session — see
    // wireLabelsToggle below. Toggle-on still staggers opens to dodge
    // a 4-camera simultaneous-attach burst.
  }

  start() {
    if (this._startTimer) {
      clearTimeout(this._startTimer);
      this._startTimer = null;
    }
    if (this._es) return;
    if (this._mode === "past") {
      if (this._pastUrl) this._connectPast();
    } else {
      this._connect();
    }
  }

  stop() {
    if (this._startTimer) {
      clearTimeout(this._startTimer);
      this._startTimer = null;
    }
    if (this._es) {
      this._es.close();
      this._es = null;
    }
    this._stopPastWatcher();
    // Drop the buffered samples — when start() fires again we'll
    // reconnect and the server replays from scratch. Mode + URL
    // persist so we know which subscription to re-open.
    this._pastSamples = null;
    this._clear();
  }

  // ---------------------------------------------------------------
  // Past-mode subscription. Caller passes the absolute window start
  // (unix seconds) so we can map sample.ts → (currentTime offset).
  // Stores params on the instance and connects iff labels are on; if
  // labels are off the connection waits for the toggle.

  subscribePast({ url, video, windowStartUnix }) {
    // Tear down any prior connection before configuring a new one.
    // This is the path the playback page takes when scrubbing across
    // 5-min window boundaries (also when transitioning live → past).
    this.stop();
    this._mode = "past";
    this._pastUrl = url;
    this._pastVideo = video;
    this._pastWindowStartUnix = windowStartUnix;
    if (document.body.classList.contains("labels-on")) {
      this.start();
    }
  }

  // Switch back to the live subscription (the default for live tiles
  // and playback live mode). Same gating: connects only if labels-on.
  subscribeLive() {
    if (this._mode === "live" && this._es) return;
    this.stop();
    this._mode = "live";
    this._pastUrl = null;
    this._pastVideo = null;
    this._pastWindowStartUnix = null;
    if (document.body.classList.contains("labels-on")) {
      this.start();
    }
  }

  _connectPast() {
    this._pastSamples = [];           // sorted by ts (server emits in order)
    this._pastTolerance = 0.6;        // seconds; ≥ our 1 fps sample period
    this._es = new EventSource(this._pastUrl, { withCredentials: true });
    this._es.onmessage = (ev) => this._onPastMessage(ev);
    this._es.onerror = () => {
      // Past stream is one-shot; on EOF or error there's nothing to
      // reconnect to. Close the EventSource so the browser doesn't
      // waste retries.
      if (this._es) {
        this._es.close();
        this._es = null;
      }
    };
    this._startPastWatcher();
  }

  _onPastMessage(ev) {
    let payload;
    try { payload = JSON.parse(ev.data); }
    catch { return; }
    if (!this._pastSamples) return;
    // Server emits in chronological order; cheap append without resort.
    this._pastSamples.push(payload);
  }

  _startPastWatcher() {
    if (!this._pastVideo) return;
    this._pastListener = () => this._renderPastFrame();
    this._pastVideo.addEventListener("timeupdate", this._pastListener);
    // Render once immediately so the very first emitted box shows
    // before the first timeupdate fires (timeupdate cadence is ~250 ms).
    this._renderPastFrame();
  }

  _stopPastWatcher() {
    if (this._pastVideo && this._pastListener) {
      this._pastVideo.removeEventListener("timeupdate", this._pastListener);
    }
    this._pastListener = null;
    this._pastVideo = null;
  }

  _renderPastFrame() {
    if (!this._pastSamples || !this._pastVideo) return;
    const target = this._pastWindowStartUnix + this._pastVideo.currentTime;
    // Linear backwards walk: samples are sorted ascending, the right
    // hit is almost always among the most recent few. Cap at 60
    // samples scanned (= 1 minute of 1 fps history) before giving up.
    const samples = this._pastSamples;
    let best = null;
    let bestDist = this._pastTolerance + 1;
    for (let i = samples.length - 1, n = 0; i >= 0 && n < 60; i--, n++) {
      const dist = Math.abs(samples[i].ts - target);
      if (dist < bestDist) {
        best = samples[i];
        bestDist = dist;
      }
      if (samples[i].ts < target - this._pastTolerance) break;
    }
    if (best && bestDist <= this._pastTolerance) {
      this._draw(best.boxes || []);
    } else {
      this._clear();
    }
  }

  destroy() {
    _overlays.delete(this);
    this.stop();
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

function setAllOverlays(on) {
  if (!on) {
    for (const o of _overlays) o.stop();
    return;
  }
  // Stagger starts even on user-toggle-on: opening 4–8 EventSources
  // simultaneously after a long-idle session can still trip the
  // HTTP/1.1 connection cap if HLS happens to be re-fetching playlists
  // at the same moment.
  let i = 0;
  for (const o of _overlays) {
    setTimeout(() => o.start(), i * _STAGGER_MS);
    i++;
  }
}

// "Show labels" toggle wiring shared across pages. Each session starts
// labels-OFF regardless of localStorage — the auto-restart-from-storage
// path repeatedly raced HLS for connection slots on page load. The
// trade-off is one click per session to enable labels; the cost is a
// gesture, the benefit is video that always plays.
function wireLabelsToggle() {
  const btn = document.getElementById("labels-toggle");
  if (!btn) return;
  document.body.classList.remove("labels-on");
  btn.setAttribute("aria-pressed", "false");
  btn.classList.remove("active");
  btn.addEventListener("click", () => {
    const on = !document.body.classList.contains("labels-on");
    document.body.classList.toggle("labels-on", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.classList.toggle("active", on);
    setAllOverlays(on);
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
