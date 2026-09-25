const state = {
  session: null,        // latest session payload from the API
  zones: [],            // [{x, y, w, h, mode}] in original frame pixels
  pendingZones: [],     // zones being edited in the picker
  zoneMode: "watch",
  previewIndex: 0,
  browsePath: "",
  filter: "all",
  pollTimer: null,
  eventView: null,      // {list, pos} for the event viewer
  tableKey: "",         // skips re-rendering the event table when nothing changed
};

// Event types, in the order they're listed / stacked. Colours live in the CSS.
const KINDS = {
  motion: { label: "Motion", plural: "Motion" },
  person: { label: "Person", plural: "People" },
  vehicle: { label: "Vehicle", plural: "Vehicles" },
  weapon: { label: "Weapon", plural: "Weapons" },
  bag: { label: "Bag", plural: "Bags" },
  animal: { label: "Animal", plural: "Animals" },
  scene_change: { label: "Lighting change", plural: "Lighting changes" },
};
const OBJECT_KINDS = ["weapon", "person", "vehicle", "bag", "animal"];
const colour = (kind) => `var(--c-${kind === "scene_change" ? "scene" : kind})`;

const $ = (sel) => document.querySelector(sel);

const folderInput = $("#folder-input");
const folderStatus = $("#folder-status");
const zoneBtn = $("#zone-btn");
const zoneThumb = $("#zone-thumb");
const zoneStatus = $("#zone-status");
const sensitivitySelect = $("#sensitivity-select");
const sampleSelect = $("#sample-select");
const detectToggle = $("#detect-toggle");
const modelSelect = $("#model-select");
const confidenceSelect = $("#confidence-select");
const detectEverySelect = $("#detect-every-select");
const gatedToggle = $("#gated-toggle");
const extraModelInput = $("#extra-model-input");
const detectorStatus = $("#detector-status");
const objectsCard = $("#objects-card");
const controlsSummary = $("#controls-summary");
const runBtn = $("#run-btn");
const cancelBtn = $("#cancel-btn");
const exportBtn = $("#export-btn");
const progressSection = $("#progress-section");
const progressFill = $("#progress-bar-fill");
const progressLabel = $("#progress-label");
const warningSection = $("#warning-section");
const errorSection = $("#error-section");
const summaryCards = $("#summary-cards");
const legendEl = $("#legend");
const timelineEl = $("#timeline");
const filterTabs = $("#filter-tabs");
const eventRows = $("#event-rows");
const tooltip = $("#tooltip");

// ---------- helpers ----------
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function formatBytes(bytes) {
  if (bytes === undefined || bytes === null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let val = bytes;
  let i = 0;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(val < 10 && i > 0 ? 2 : 0)} ${units[i]}`;
}

async function api(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const detail = Array.isArray(err.detail) ? err.detail.map((d) => d.msg).join("; ") : err.detail;
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function postJson(url, body) {
  return api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

// Times are seconds from the start of a clip - there's no wall clock here
// (that's VTT's job), so they're shown as h:mm:ss.
function fmtClock(seconds, withTenths = false) {
  const s = Math.max(0, seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = withTenths ? (s % 60).toFixed(1).padStart(4, "0") : String(Math.floor(s % 60)).padStart(2, "0");
  return `${h}:${String(m).padStart(2, "0")}:${sec}`;
}

function fmtLength(seconds) {
  if (seconds < 1) return "< 1 s";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  return fmtClock(seconds);
}

function showError(message) {
  errorSection.textContent = message;
  errorSection.classList.remove("hidden");
}

function clearError() {
  errorSection.classList.add("hidden");
}

function frameUrl(index) {
  return `/api/sessions/${state.session.id}/files/${index}/frame.jpg?t=${Date.now()}`;
}

function snapshotUrl(ev, thumb = false) {
  return `/api/sessions/${state.session.id}/events/${ev.id}.jpg${thumb ? "?thumb=true" : ""}`;
}

function selectedCategories() {
  return [...document.querySelectorAll("#category-chips input:checked")].map((el) => el.value);
}

function settings() {
  return {
    zones: state.zones,
    sensitivity: sensitivitySelect.value,
    sample_fps: Number(sampleSelect.value),
    detect_objects: detectToggle.checked,
    categories: selectedCategories(),
    model: modelSelect.value,
    extra_model: extraModelInput.value.trim(),
    confidence: Number(confidenceSelect.value),
    detect_every: Number(detectEverySelect.value),
    motion_gated: gatedToggle.checked,
  };
}

// ---------- modals ----------
function openModal(el) { el.classList.remove("hidden"); }
function closeModal(el) { el.classList.add("hidden"); }

document.querySelectorAll(".modal").forEach((modal) => {
  modal.addEventListener("click", (e) => {
    if (e.target === modal || e.target.hasAttribute("data-close")) closeModal(modal);
  });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.querySelectorAll(".modal:not(.hidden)").forEach(closeModal);
  if (!$("#event-modal").classList.contains("hidden")) {
    if (e.key === "ArrowLeft") stepEvent(-1);
    if (e.key === "ArrowRight") stepEvent(1);
  }
});

// ---------- step 1: folder ----------
folderInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadFolder(folderInput.value.trim());
});
folderInput.addEventListener("change", () => loadFolder(folderInput.value.trim()));

async function loadFolder(path) {
  if (!path || (state.session && state.session.folder === path)) return;
  folderInput.value = path;
  folderStatus.textContent = "Scanning…";
  folderStatus.className = "file-status";
  clearError();
  try {
    const session = await postJson("/api/sessions", { folder: path });
    stopPolling();
    state.session = session;
    state.previewIndex = 0;
    state.tableKey = "";
    const n = session.files.length;
    folderStatus.textContent = `${n} video file${n === 1 ? "" : "s"} found`;
    folderStatus.className = "file-status ok";
    // Exports from the same camera share a view, so zones drawn for a
    // previous folder are kept - just re-check them against this one.
    updateZoneThumb();
    render();
  } catch (e) {
    folderStatus.textContent = e.message;
    folderStatus.className = "file-status err";
  }
}

// Folder browser
const browseModal = $("#browse-modal");
const browseEntries = $("#browse-entries");
const browseCurrentPath = $("#browse-current-path");
const browseUpBtn = $("#browse-up-btn");
const browseCount = $("#browse-count");
const browseSelectBtn = $("#browse-select-btn");
let browseParent = null;

$("#folder-browse-btn").addEventListener("click", () => {
  openModal(browseModal);
  loadBrowse(folderInput.value.trim());
});

browseUpBtn.addEventListener("click", () => {
  if (browseParent !== null) loadBrowse(browseParent);
});

browseSelectBtn.addEventListener("click", () => {
  if (!state.browsePath) return;
  closeModal(browseModal);
  loadFolder(state.browsePath);
});

async function loadBrowse(path) {
  browseEntries.innerHTML = `<div class="browse-entry"><span class="name">Loading…</span></div>`;
  let data;
  try {
    data = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
  } catch (e) {
    if (path) return loadBrowse("");  // typed path doesn't exist - start from the drive list
    browseEntries.innerHTML = `<div class="browse-entry"><span class="name">Error: ${escapeHtml(e.message)}</span></div>`;
    return;
  }
  state.browsePath = data.path;
  browseParent = data.parent;
  browseCurrentPath.textContent = data.path || "Drives";
  browseUpBtn.disabled = data.parent === null;
  browseSelectBtn.disabled = !data.path;
  browseCount.textContent = data.path
    ? `${data.video_count} video file${data.video_count === 1 ? "" : "s"} in this folder`
    : "";

  const rows = data.entries.map((e) => {
    const icon = e.type === "dir" ? "&#128193;" : "&#127909;";
    const size = e.type === "file" ? `<span class="size">${formatBytes(e.size)}</span>` : "";
    return `<div class="browse-entry ${e.type === "file" ? "is-file" : ""}" data-type="${e.type}" data-path="${escapeHtml(e.path)}">
      <span class="icon">${icon}</span><span class="name">${escapeHtml(e.name)}</span>${size}
    </div>`;
  });
  browseEntries.innerHTML = rows.join("") || `<div class="browse-entry"><span class="name">(empty)</span></div>`;
  browseEntries.querySelectorAll(".browse-entry[data-type='dir']").forEach((el) => {
    el.addEventListener("click", () => loadBrowse(el.dataset.path));
  });
}

// ---------- step 2: zones ----------
const zoneModal = $("#zone-modal");
const zoneStage = $("#zone-stage");
const zoneImage = $("#zone-image");
const previewSelect = $("#preview-file-select");
const zoneHint = $("#zone-hint");

zoneBtn.addEventListener("click", openZonePicker);

function openZonePicker() {
  if (!state.session) return;
  previewSelect.innerHTML = state.session.files
    .map((f) => `<option value="${f.index}">${escapeHtml(f.name)}</option>`)
    .join("");
  previewSelect.value = String(state.previewIndex);
  state.pendingZones = state.zones.map((z) => ({ ...z }));
  zoneHint.textContent = "";
  openModal(zoneModal);
  loadPreviewImage();
}

previewSelect.addEventListener("change", () => {
  state.previewIndex = Number(previewSelect.value);
  loadPreviewImage();
});

document.querySelectorAll("#zone-mode button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.zoneMode = btn.dataset.mode;
    document.querySelectorAll("#zone-mode button").forEach((b) => b.classList.toggle("active", b === btn));
  });
});

$("#zone-clear-btn").addEventListener("click", () => {
  state.pendingZones = [];
  drawZones();
});

function loadPreviewImage() {
  zoneImage.onload = drawZones;
  zoneImage.onerror = () => { zoneHint.textContent = "Couldn't read a frame from this file."; };
  zoneImage.src = frameUrl(state.previewIndex);
}

function imageScale() {
  return zoneImage.clientWidth / zoneImage.naturalWidth;
}

// Watch zones are numbered Zone 1, 2… and ignore zones Ignore 1, 2…, the
// same way the backend names them in the results.
function zoneNames(zones) {
  let w = 0, i = 0;
  return zones.map((z) => (z.mode === "watch" ? `Zone ${++w}` : `Ignore ${++i}`));
}

function zoneSummary(zones) {
  const watch = zones.filter((z) => z.mode === "watch").length;
  const ignore = zones.length - watch;
  if (!zones.length) return "Activity anywhere in the frame counts.";
  const parts = [];
  if (watch) parts.push(`only activity inside ${watch} watch zone${watch === 1 ? "" : "s"} counts`);
  if (ignore) parts.push(`${ignore} area${ignore === 1 ? " is" : "s are"} ignored`);
  const text = parts.join("; ");
  return text[0].toUpperCase() + text.slice(1) + ".";
}

function drawZones() {
  zoneStage.querySelectorAll(".zone-box").forEach((el) => el.remove());
  $("#zone-summary").textContent = zoneSummary(state.pendingZones);
  if (!zoneImage.naturalWidth) return;
  const s = imageScale();
  const names = zoneNames(state.pendingZones);
  state.pendingZones.forEach((z, i) => {
    const box = document.createElement("div");
    box.className = `zone-box ${z.mode}`;
    Object.assign(box.style, { left: `${z.x * s}px`, top: `${z.y * s}px`, width: `${z.w * s}px`, height: `${z.h * s}px` });
    box.innerHTML = `<span class="zone-name">${names[i]}</span><button class="zone-del" title="Remove">&times;</button>`;
    box.querySelector(".zone-del").addEventListener("click", () => {
      state.pendingZones.splice(i, 1);
      drawZones();
    });
    zoneStage.appendChild(box);
  });
}

let dragStart = null;
let dragBox = null;

function stagePoint(e) {
  const rect = zoneImage.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(rect.width, e.clientX - rect.left)),
    y: Math.max(0, Math.min(rect.height, e.clientY - rect.top)),
  };
}

zoneStage.addEventListener("pointerdown", (e) => {
  if (!zoneImage.naturalWidth || e.target.closest(".zone-del")) return;
  zoneStage.setPointerCapture(e.pointerId);
  dragStart = stagePoint(e);
  dragBox = document.createElement("div");
  dragBox.className = `zone-box drawing ${state.zoneMode}`;
  zoneStage.appendChild(dragBox);
  zoneHint.textContent = "";
});

zoneStage.addEventListener("pointermove", (e) => {
  if (!dragStart) return;
  const p = stagePoint(e);
  Object.assign(dragBox.style, {
    left: `${Math.min(dragStart.x, p.x)}px`, top: `${Math.min(dragStart.y, p.y)}px`,
    width: `${Math.abs(p.x - dragStart.x)}px`, height: `${Math.abs(p.y - dragStart.y)}px`,
  });
});

zoneStage.addEventListener("pointerup", (e) => {
  if (!dragStart) return;
  const p = stagePoint(e);
  const s = imageScale();
  const [x, y, w, h] = [
    Math.min(dragStart.x, p.x) / s, Math.min(dragStart.y, p.y) / s,
    Math.abs(p.x - dragStart.x) / s, Math.abs(p.y - dragStart.y) / s,
  ].map(Math.round);
  dragStart = null;
  dragBox.remove();
  if (w < 5 || h < 5) {
    zoneHint.textContent = "Too small - drag a larger box.";
    return;
  }
  state.pendingZones.push({ x, y, w, h, mode: state.zoneMode });
  drawZones();
});

window.addEventListener("resize", () => {
  if (!zoneModal.classList.contains("hidden")) drawZones();
});

$("#zone-confirm-btn").addEventListener("click", () => {
  state.zones = state.pendingZones.map((z) => ({ ...z }));
  closeModal(zoneModal);
  updateZoneThumb();
  render();
});

function updateZoneThumb() {
  zoneStatus.textContent = zoneSummary(state.zones);
  zoneStatus.className = "file-status";
  if (!state.zones.length || !state.session) {
    zoneThumb.style.backgroundImage = "";
    zoneThumb.classList.add("empty");
    zoneThumb.textContent = "Whole frame";
    return;
  }
  const img = new Image();
  img.onload = () => {
    if (state.zones.some((z) => z.x + z.w > img.naturalWidth || z.y + z.h > img.naturalHeight)) {
      zoneStatus.textContent = "A zone is outside this folder's frame size - draw the zones again.";
      zoneStatus.className = "file-status err";
    }
    // the frame at thumbnail size with the zones drawn over it
    const canvas = document.createElement("canvas");
    const scale = 76 / img.naturalHeight;
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = 76;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    ctx.lineWidth = 2;
    for (const z of state.zones) {
      ctx.strokeStyle = z.mode === "watch" ? "#3ecf8e" : "#f05a5a";
      ctx.fillStyle = z.mode === "watch" ? "rgba(62,207,142,.2)" : "rgba(240,90,90,.3)";
      ctx.fillRect(z.x * scale, z.y * scale, z.w * scale, z.h * scale);
      ctx.strokeRect(z.x * scale, z.y * scale, z.w * scale, z.h * scale);
    }
    zoneThumb.style.backgroundImage = `url(${canvas.toDataURL()})`;
    zoneThumb.classList.remove("empty");
  };
  img.src = frameUrl(state.previewIndex);
}

// ---------- steps 3-4: settings ----------
async function loadDetectorStatus() {
  try {
    const st = await api("/api/status");
    detectorStatus.textContent = st.available
      ? `${st.message}. Stock models only know knives - add a firearms model above to detect guns.`
      : `Object detection unavailable (${st.message}).`;
    detectorStatus.className = `file-status ${st.available ? "" : "err"}`;
    if (!st.available) detectToggle.checked = false;
  } catch (e) {
    detectorStatus.textContent = e.message;
  }
  render();
}

[detectToggle, ...document.querySelectorAll("#category-chips input")].forEach((el) =>
  el.addEventListener("change", render));

// ---------- analyse ----------
runBtn.addEventListener("click", async () => {
  clearError();
  try {
    state.session = await postJson(`/api/sessions/${state.session.id}/process`, settings());
    state.tableKey = "";
    render();
    startPolling();
  } catch (e) {
    showError(e.message);
  }
});

cancelBtn.addEventListener("click", async () => {
  try {
    await postJson(`/api/sessions/${state.session.id}/cancel`);
  } catch (e) {
    console.error(e);
  }
});

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      state.session = await api(`/api/sessions/${state.session.id}`);
    } catch (e) {
      stopPolling();
      showError(e.message);
      return;
    }
    if (state.session.status !== "running") {
      stopPolling();
      if (state.session.status === "error") showError(state.session.error || "Analysis failed");
    }
    render();
  }, 800);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

// ---------- rendering ----------
function allEvents() {
  if (!state.session) return [];
  return state.session.files.flatMap((f) => f.events.map((ev) => ({ ...ev, fileName: f.name })));
}

function render() {
  const s = state.session;
  const running = s && s.status === "running";
  const analysed = s && s.files.some((f) => f.status === "done" || f.status === "partial");
  runBtn.disabled = !s || running;
  runBtn.textContent = analysed ? "Re-analyse Files" : "Analyse Files";
  cancelBtn.classList.toggle("hidden", !running);
  zoneBtn.disabled = !s || running;
  objectsCard.classList.toggle("disabled", !detectToggle.checked);
  document.querySelectorAll("select, .picker-card input").forEach((el) => {
    if (el !== folderInput) el.disabled = running;
  });

  const cats = selectedCategories();
  const objects = detectToggle.checked && cats.length
    ? `${cats.map((c) => KINDS[c].plural.toLowerCase()).join(", ")}`
    : "no objects";
  controlsSummary.textContent = s
    ? `${s.files.length} file${s.files.length === 1 ? "" : "s"} · motion + ${objects}`
    : "Load a folder to begin.";

  const hasEvents = s && s.files.some((f) => f.events.length);
  exportBtn.classList.toggle("disabled", !hasEvents);
  exportBtn.setAttribute("aria-disabled", String(!hasEvents));
  if (hasEvents) exportBtn.href = `/api/sessions/${s.id}/export.csv`;
  else exportBtn.removeAttribute("href");

  warningSection.textContent = s && s.warning ? s.warning : "";
  warningSection.classList.toggle("hidden", !(s && s.warning));

  renderProgress();
  renderSummary();
  renderLegend();
  renderTimeline();
  renderTable();
}

function renderProgress() {
  const s = state.session;
  if (!s || s.status === "idle") {
    progressSection.classList.add("hidden");
    return;
  }
  progressSection.classList.remove("hidden");
  const current = s.files.find((f) => f.status === "running");
  const done = s.processed + (current ? current.progress : 0);
  const pct = s.total ? (done / s.total) * 100 : 0;
  progressFill.style.width = `${pct}%`;
  const errors = s.files.filter((f) => f.status === "error").length;
  const errText = errors ? ` ${errors} file${errors === 1 ? "" : "s"} couldn't be read.` : "";
  if (s.status === "running") {
    const model = s.detector ? ` · ${s.detector}` : "";
    progressLabel.textContent = `${s.stage} — ${s.processed}/${s.total} files (${pct.toFixed(0)}%)${model}`;
  } else if (s.status === "cancelled") {
    progressLabel.textContent = `Cancelled after ${s.processed}/${s.total} files.${errText}`;
  } else if (s.status === "error") {
    progressSection.classList.add("hidden");
  } else {
    const n = allEvents().length;
    progressLabel.textContent = `Done — ${n} event${n === 1 ? "" : "s"} found in ${s.total} file${s.total === 1 ? "" : "s"}.${errText}`;
  }
}

function presentKinds() {
  const seen = new Set(allEvents().map((e) => e.kind));
  return Object.keys(KINDS).filter((k) => seen.has(k));
}

function renderLegend() {
  const kinds = state.session && allEvents().length ? presentKinds() : ["motion", ...selectedCategories()];
  legendEl.innerHTML = kinds.map((k) => `<span><i class="swatch ${k}"></i>${KINDS[k].label}</span>`).join("");
}

function renderSummary() {
  const s = state.session;
  if (!s) {
    summaryCards.innerHTML = "";
    return;
  }
  const events = allEvents();
  const card = (name, value, sub = "", cls = "", cardCls = "") =>
    `<div class="score-card ${cardCls}"><div class="metric-name">${name}</div>
      <div class="metric-value small ${cls}">${value}</div>${sub ? `<div class="metric-sub">${sub}</div>` : ""}</div>`;

  const analysed = s.files.filter((f) => f.status === "done" || f.status === "partial");
  const footage = analysed.reduce((a, f) => a + (f.duration || 0), 0);
  let html = card("Files", `${analysed.length} / ${s.files.length}`, footage ? `${fmtClock(footage)} of footage` : "analysed");

  const motion = events.filter((e) => e.kind === "motion");
  const motionTime = motion.reduce((a, e) => a + Math.max(e.end - e.start, 0.2), 0);
  const scenes = events.filter((e) => e.kind === "scene_change").length;
  const motionSub = [
    motion.length ? `${fmtLength(motionTime)} total${footage ? ` (${((motionTime / footage) * 100).toFixed(1)}%)` : ""}` : "none",
    scenes ? `${scenes} lighting change${scenes === 1 ? "" : "s"}` : "",
  ].filter(Boolean).join(" · ");
  html += card("Motion events", String(motion.length), motionSub);

  for (const kind of OBJECT_KINDS) {
    const list = events.filter((e) => e.kind === kind);
    if (!list.length && !(kind === "weapon" || selectedCategories().includes(kind))) continue;
    const most = Math.max(0, ...list.map((e) => e.count));
    const files = new Set(list.map((e) => e.file)).size;
    const sub = list.length ? `in ${files} file${files === 1 ? "" : "s"} · up to ${most} at once` : "none seen";
    const alert = kind === "weapon" && list.length;
    html += card(`${KINDS[kind].plural}`, String(list.length), sub, alert ? "conf-bad" : "", alert ? "alert" : "");
  }
  summaryCards.innerHTML = html;
}

const NICE_INTERVALS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];

function computeTicks(span, maxTicks) {
  let interval = NICE_INTERVALS[NICE_INTERVALS.length - 1];
  for (const c of NICE_INTERVALS) {
    if (span / c <= maxTicks) {
      interval = c;
      break;
    }
  }
  const ticks = [];
  for (let t = 0; t <= span; t += interval) ticks.push(t);
  return ticks;
}

function tickLabel(t, span) {
  if (span < 3600) return `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
  return fmtClock(t).slice(0, -3);
}

function renderTimeline() {
  const s = state.session;
  if (!s || !s.files.some((f) => f.status !== "pending")) {
    timelineEl.innerHTML = `<p class="placeholder">Load a folder and analyse the files to see where the activity is.</p>`;
    return;
  }

  // one lane of motion, plus one thin lane per object type present
  const lanes = OBJECT_KINDS.filter((k) => allEvents().some((e) => e.kind === k));
  const width = Math.max(timelineEl.clientWidth, 480);
  const labelW = Math.min(220, width * 0.28);
  const padR = 24;
  const top = 30;
  const motionH = 20;
  const laneH = 8;
  const rowH = motionH + lanes.length * (laneH + 2) + 14;
  const plotW = width - labelW - padR;
  const files = s.files;
  const height = top + files.length * rowH;
  const span = Math.max(1, ...files.map((f) => f.duration || 0));
  const x = (t) => labelW + (t / span) * plotW;

  let svg = `<svg width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI, system-ui, sans-serif">`;
  for (const t of computeTicks(span, Math.max(3, Math.floor(plotW / 90)))) {
    const tx = x(t);
    svg += `<line x1="${tx}" y1="${top - 6}" x2="${tx}" y2="${height}" stroke="var(--border)" />`;
    svg += `<text x="${tx}" y="${top - 12}" fill="var(--text-dim)" font-size="11" text-anchor="middle">${tickLabel(t, span)}</text>`;
  }
  svg += `<line x1="${labelW}" y1="${top - 6}" x2="${labelW + plotW}" y2="${top - 6}" stroke="var(--text-dim)" />`;

  files.forEach((f, row) => {
    const y = top + row * rowH + 4;
    const name = f.name.length > 30 ? f.name.slice(0, 28) + "…" : f.name;
    const clickable = f.status === "done" || f.status === "partial";
    svg += `<text class="${clickable ? "file-label" : ""}" data-index="${f.index}" x="${labelW - 10}" y="${y + motionH / 2 + 4}" fill="var(--text)" font-size="11.5" text-anchor="end" font-family="Consolas, monospace">${escapeHtml(name)}<title>${escapeHtml(f.name)}</title></text>`;

    if (f.status === "error") {
      svg += `<text x="${labelW + 6}" y="${y + motionH / 2 + 4}" fill="var(--bad)" font-size="11.5">Couldn't read: ${escapeHtml(f.error)}</text>`;
      return;
    }
    if (f.status === "pending" || f.status === "running") {
      const w = plotW * (f.status === "running" ? f.progress : 0);
      svg += `<rect x="${labelW}" y="${y + motionH / 2 - 2}" width="${plotW}" height="4" rx="2" fill="var(--border)" />`;
      if (w) svg += `<rect x="${labelW}" y="${y + motionH / 2 - 2}" width="${w}" height="4" rx="2" fill="var(--accent)" />`;
      return;
    }

    const dur = f.duration || 0;
    const base = y + motionH;
    svg += `<rect x="${labelW}" y="${y}" width="${Math.max(x(dur) - labelW, 1)}" height="${motionH}" fill="var(--bg)" opacity=".6" />`;
    // motion level per second, square-root scaled so a distant figure still shows next to a close one
    if (f.activity.some((a) => a > 0)) {
      let d = `M${labelW},${base}`;
      f.activity.forEach((a, i) => {
        const h = Math.min(1, Math.sqrt(a / 0.08)) * motionH;
        d += `L${x(i)},${base - h}L${x(Math.min(i + 1, dur))},${base - h}`;
      });
      d += `L${x(Math.min(f.activity.length, dur))},${base}Z`;
      svg += `<path d="${d}" fill="var(--c-motion)" opacity=".75" />`;
    }
    for (const ev of f.events) {
      if (ev.kind === "motion" || ev.kind === "scene_change") {
        const x0 = x(ev.start);
        const w = Math.max(x(ev.end) - x0, 4);
        const fill = ev.kind === "scene_change" ? `fill="var(--c-scene)" opacity=".5"` : `fill="transparent"`;
        svg += `<rect class="hit" data-id="${ev.id}" x="${x0}" y="${y}" width="${w}" height="${motionH}" ${fill} />`;
      }
    }
    lanes.forEach((kind, li) => {
      const ly = base + 2 + li * (laneH + 2);
      for (const ev of f.events.filter((e) => e.kind === kind)) {
        const x0 = x(ev.start);
        const w = Math.max(x(ev.end) - x0, 4);
        svg += `<rect class="hit" data-id="${ev.id}" x="${x0}" y="${ly}" width="${w}" height="${laneH}" rx="2" fill="${colour(kind)}" />`;
      }
    });
  });
  svg += "</svg>";
  timelineEl.innerHTML = svg;

  const byId = Object.fromEntries(allEvents().map((e) => [e.id, e]));
  timelineEl.querySelectorAll(".hit").forEach((el) => {
    const ev = byId[el.dataset.id];
    el.addEventListener("mousemove", (e) => showTooltip(e, ev));
    el.addEventListener("mouseleave", hideTooltip);
    el.addEventListener("click", () => {
      hideTooltip();
      openEvent(ev, eventsInView());
    });
  });
  timelineEl.querySelectorAll(".file-label").forEach((el) => {
    el.addEventListener("click", () => openHeatmap(Number(el.dataset.index)));
  });
}

function describe(ev) {
  const parts = [];
  if (ev.kind === "motion") {
    parts.push(`up to ${(ev.peak * 100).toFixed(1)}% of the frame`);
    if (ev.count > 1) parts.push(`${ev.count} areas at once`);
    if (ev.zones.length) parts.push(`in ${ev.zones.join(", ")}`);
  } else if (ev.kind === "scene_change") {
    parts.push(`${(ev.peak * 100).toFixed(0)}% of the frame changed at once`);
  } else {
    if (ev.labels.length && !(ev.labels.length === 1 && ev.labels[0] === ev.kind)) parts.push(ev.labels.join(", "));
    if (ev.count > 1) parts.push(`up to ${ev.count} at once`);
  }
  return parts.join(" · ");
}

function score(ev) {
  if (ev.kind === "motion" || ev.kind === "scene_change") return { text: "-", cls: "" };
  const cls = ev.peak >= 0.7 ? "conf-good" : ev.peak >= 0.5 ? "conf-warn" : "conf-bad";
  return { text: `${Math.round(ev.peak * 100)}%`, cls };
}

function showTooltip(e, ev) {
  tooltip.innerHTML = `<div class="tt-name">${KINDS[ev.kind].label} · ${escapeHtml(ev.fileName)}</div>
    <div class="tt-row">${fmtClock(ev.start)} → ${fmtClock(ev.end)} (${fmtLength(ev.end - ev.start)})</div>
    <div class="tt-row">${escapeHtml(describe(ev))}</div>`;
  tooltip.classList.remove("hidden");
  const pad = 14;
  const tw = tooltip.offsetWidth;
  const left = e.clientX + pad + tw > window.innerWidth ? e.clientX - pad - tw : e.clientX + pad;
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${e.clientY + pad}px`;
}

function hideTooltip() {
  tooltip.classList.add("hidden");
}

async function openClip(index) {
  try {
    await postJson(`/api/sessions/${state.session.id}/files/${index}/open`);
  } catch (e) {
    showError(e.message);
  }
}

// ---------- event table ----------
const FILTERS = ["all", ...OBJECT_KINDS, "motion", "scene_change"];
const MAX_ROWS = 500;

function eventsInView() {
  const events = allEvents();
  return state.filter === "all" ? events : events.filter((e) => e.kind === state.filter);
}

function renderTable() {
  const s = state.session;
  const events = allEvents();
  const counts = {};
  for (const e of events) counts[e.kind] = (counts[e.kind] || 0) + 1;
  const tabs = FILTERS.filter((k) => k === "all" || counts[k] || k === state.filter);
  const key = JSON.stringify([s && s.id, s && s.status, tabs, counts, state.filter, events.length && events[events.length - 1].id]);
  if (key === state.tableKey) return;  // nothing new - don't reload every thumbnail on each poll
  state.tableKey = key;

  filterTabs.innerHTML = tabs.map((k) => {
    const label = k === "all" ? "All" : KINDS[k].plural;
    const n = k === "all" ? events.length : counts[k] || 0;
    return `<button data-filter="${k}" class="${k === state.filter ? "active" : ""}">${label}<span class="count">${n}</span></button>`;
  }).join("");
  filterTabs.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.filter = btn.dataset.filter;
      renderTable();
    });
  });

  if (!s) {
    eventRows.innerHTML = `<tr><td colspan="7" class="placeholder">No folder loaded.</td></tr>`;
    return;
  }
  const list = eventsInView();
  if (!list.length) {
    const msg = s.files.some((f) => f.status === "done") ? "Nothing found." : "Analyse the files to list what's in them.";
    eventRows.innerHTML = `<tr><td colspan="7" class="placeholder">${msg}</td></tr>`;
    return;
  }

  let html = list.slice(0, MAX_ROWS).map((ev, i) => {
    const sc = score(ev);
    return `<tr data-pos="${i}">
      <td class="thumb"><img loading="lazy" src="${snapshotUrl(ev, true)}" alt="" /></td>
      <td class="name">${escapeHtml(ev.fileName)}</td>
      <td>${fmtClock(ev.start)} – ${fmtClock(ev.end)}</td>
      <td>${fmtLength(ev.end - ev.start)}</td>
      <td><span class="badge ${ev.kind}"><i class="swatch ${ev.kind}"></i>${KINDS[ev.kind].label}</span></td>
      <td class="details">${escapeHtml(describe(ev))}</td>
      <td class="${sc.cls}">${sc.text}</td>
    </tr>`;
  }).join("");
  if (list.length > MAX_ROWS) {
    html += `<tr><td colspan="7" class="placeholder">Showing the first ${MAX_ROWS} of ${list.length} - filter by type or export the CSV for the rest.</td></tr>`;
  }
  eventRows.innerHTML = html;
  eventRows.querySelectorAll("tr[data-pos]").forEach((tr) => {
    tr.addEventListener("click", () => openEvent(list[Number(tr.dataset.pos)], list));
  });
}

window.addEventListener("resize", () => {
  clearTimeout(renderTimeline._t);
  renderTimeline._t = setTimeout(renderTimeline, 120);
});

// ---------- event viewer ----------
const eventModal = $("#event-modal");

function openEvent(ev, list) {
  const pos = Math.max(0, list.findIndex((e) => e.id === ev.id));
  state.eventView = { list, pos };
  showEvent();
  openModal(eventModal);
}

function stepEvent(delta) {
  const v = state.eventView;
  if (!v) return;
  const pos = v.pos + delta;
  if (pos < 0 || pos >= v.list.length) return;
  v.pos = pos;
  showEvent();
}

function showEvent() {
  const { list, pos } = state.eventView;
  const ev = list[pos];
  $("#event-title").innerHTML = `<span class="badge ${ev.kind}"><i class="swatch ${ev.kind}"></i>${KINDS[ev.kind].label}</span>${escapeHtml(ev.fileName)}`;
  $("#event-image").src = snapshotUrl(ev);
  const sc = score(ev);
  const rows = [
    ["Time in clip", `${fmtClock(ev.start, true)} – ${fmtClock(ev.end, true)}`],
    ["Length", fmtLength(ev.end - ev.start)],
    ["Snapshot at", fmtClock(ev.key_time, true)],
  ];
  const d = describe(ev);
  if (d) rows.push(["Details", escapeHtml(d)]);
  if (sc.text !== "-") rows.push(["Best confidence", `<span class="${sc.cls}">${sc.text}</span>`]);
  $("#event-details").innerHTML = rows.map(([k, v]) => `<span><span class="k">${k}</span><span class="v">${v}</span></span>`).join("");
  $("#event-position").textContent = `${pos + 1} of ${list.length} · ← → to step through`;
  $("#event-prev-btn").disabled = pos === 0;
  $("#event-next-btn").disabled = pos === list.length - 1;
}

$("#event-prev-btn").addEventListener("click", () => stepEvent(-1));
$("#event-next-btn").addEventListener("click", () => stepEvent(1));
$("#event-open-btn").addEventListener("click", () => {
  const { list, pos } = state.eventView;
  openClip(list[pos].file);
});
$("#event-heatmap-btn").addEventListener("click", () => {
  const { list, pos } = state.eventView;
  closeModal(eventModal);
  openHeatmap(list[pos].file);
});

// ---------- heatmap ----------
const heatmapModal = $("#heatmap-modal");
let heatmapIndex = null;

function openHeatmap(index) {
  heatmapIndex = index;
  const f = state.session.files[index];
  $("#heatmap-title").textContent = `Motion heatmap: ${f.name}`;
  $("#heatmap-image").src = `/api/sessions/${state.session.id}/files/${index}/heatmap.jpg?r=${Date.now()}`;
  openModal(heatmapModal);
}

$("#heatmap-open-btn").addEventListener("click", () => openClip(heatmapIndex));

// ---------- init ----------
render();
loadDetectorStatus();
