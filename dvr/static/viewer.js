"use strict";

const HEALTH_POLL_MS = 15_000;
const GRID_SLOTS = 4;

const grid = document.getElementById("grid");
const statusPill = document.getElementById("status");
const setNav = document.getElementById("set-nav");
const tileTemplate = document.getElementById("tile-template");

const players = new Map(); // name -> { tile, video, hls, cam }

function currentSetId() {
  const m = window.location.pathname.match(/^\/sets\/([^/]+)/);
  return m ? m[1] : null;
}

async function fetchJSON(url) {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

function makeTile(cam) {
  const tile = tileTemplate.content.firstElementChild.cloneNode(true);
  tile.dataset.name = cam.name;
  tile.querySelector(".label").textContent = cam.label;
  const host = tile.querySelector(".host");
  host.textContent = cam.host || "";
  host.href = cam.web_url || "#";
  const setId = currentSetId();
  const playbackUrl = `/sets/${encodeURIComponent(setId)}/${encodeURIComponent(cam.name)}/playback`;
  const playback = tile.querySelector(".playback-link");
  if (playback) playback.href = playbackUrl;
  tile.querySelector(".reload").addEventListener("click", () => attach(cam));
  // Tap-anywhere on the live video → playback page. The Reload button has
  // pointer-events:auto and lives in the overlay, so its clicks don't reach
  // the video element below; everything else in the overlay is
  // pointer-events:none and falls through to here.
  tile.querySelector("video").addEventListener("click", () => {
    window.location.href = playbackUrl;
  });
  grid.appendChild(tile);
  return tile;
}

function makeEmptyTile() {
  const tile = document.createElement("div");
  tile.className = "tile empty";
  tile.innerHTML = '<div class="video-wrap"></div>';
  grid.appendChild(tile);
}

function attach(cam) {
  const entry = players.get(cam.name);
  if (!entry) return;
  const { video, tile } = entry;

  if (entry.hls) {
    entry.hls.destroy();
    entry.hls = null;
  }

  if (!cam.hls_url) {
    setState(tile, "disabled");
    return;
  }

  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = cam.hls_url;
    video.play().catch(() => {});
  } else if (window.Hls && Hls.isSupported()) {
    const hls = new Hls({
      lowLatencyMode: true,
      liveSyncDurationCount: 2,
      maxLiveSyncPlaybackRate: 1.2,
      // MediaMTX runs sources on-demand, so the first manifest load almost
      // always 404s while the RTSP source is starting up. Retry long enough
      // to ride out the source startup (sourceOnDemandStartTimeout=10s).
      manifestLoadingMaxRetry: 8,
      manifestLoadingRetryDelay: 500,
      manifestLoadingMaxRetryTimeout: 4000,
      levelLoadingMaxRetry: 6,
      levelLoadingRetryDelay: 500,
    });
    hls.loadSource(cam.hls_url);
    hls.attachMedia(video);
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) {
        setState(tile, "offline");
        hls.destroy();
        entry.hls = null;
      }
    });
    entry.hls = hls;
  } else {
    setState(tile, "offline");
  }
}

function setState(tile, state) {
  tile.classList.remove("offline", "disabled");
  tile.dataset.state = state;
  if (state === "offline" || state === "disabled") {
    tile.classList.add(state);
  }
  const span = tile.querySelector(".state");
  span.textContent = state === "live" ? "" : state;
}

async function refreshHealth(setId) {
  let probes;
  try {
    probes = await fetchJSON(`/api/sets/${encodeURIComponent(setId)}/health`);
    statusPill.textContent = `last check ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    statusPill.textContent = `health failed: ${e.message}`;
    return;
  }
  for (const p of probes) {
    const entry = players.get(p.name);
    if (!entry) continue;
    setState(entry.tile, p.status);
  }
}

function renderSetNav(sets, currentId) {
  setNav.innerHTML = "";
  for (const s of sets) {
    const a = document.createElement("a");
    a.href = `/sets/${encodeURIComponent(s.id)}`;
    a.textContent = s.label;
    if (s.id === currentId) a.classList.add("active");
    setNav.appendChild(a);
  }
}

async function init() {
  const setId = currentSetId();
  if (!setId) {
    statusPill.textContent = "no set in URL";
    return;
  }

  let sets, cameras;
  try {
    [sets, cameras] = await Promise.all([
      fetchJSON("/api/sets"),
      fetchJSON(`/api/sets/${encodeURIComponent(setId)}/cameras`),
    ]);
  } catch (e) {
    statusPill.textContent = `failed to load: ${e.message}`;
    return;
  }

  renderSetNav(sets, setId);

  for (const cam of cameras.slice(0, GRID_SLOTS)) {
    const tile = makeTile(cam);
    const video = tile.querySelector("video");
    players.set(cam.name, { tile, video, hls: null, cam });
    if (cam.enabled) {
      setState(tile, "live");
      attach(cam);
    } else {
      setState(tile, "disabled");
    }
  }

  for (let i = cameras.length; i < GRID_SLOTS; i++) {
    makeEmptyTile();
  }

  await refreshHealth(setId);
  setInterval(() => refreshHealth(setId), HEALTH_POLL_MS);
}

init();
