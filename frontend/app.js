const state = {
  session: null,        // latest session payload from the API
  region: null,         // [x, y, w, h] in original frame pixels
  pendingRegion: null,  // region being drawn in the picker
  previewIndex: 0,
  browsePath: "",
  filter: "all",
  pollTimer: null,
};

const $ = (sel) => document.querySelector(sel);

const folderInput = $("#folder-input");
const folderStatus = $("#folder-status");
const regionBtn = $("#region-btn");
const regionThumb = $("#region-thumb");
const regionStatus = $("#region-status");
const dateOrderSelect = $("#date-order-select");
const hourFormatSelect = $("#hour-format-select");
const runBtn = $("#run-btn");
const cancelBtn = $("#cancel-btn");
const exportBtn = $("#export-btn");
const progressSection = $("#progress-section");
const progressFill = $("#progress-bar-fill");
const progressLabel = $("#progress-label");
const errorSection = $("#error-section");
const summaryCards = $("#summary-cards");
const timelineEl = $("#timeline");
const fileRows = $("#file-rows");
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
    throw new Error(err.detail || `${res.status} ${res.statusText}`);
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

// Timestamps are wall-clock times read off the footage with no timezone, so
// treat them as UTC throughout - no local-timezone/DST shifts in the maths.
const toMs = (iso) => Date.parse(iso + "Z");

// Timestamps are shown (and typed in) in the same format as the footage,
// as picked with the Date order / Time format selectors.
function dateParts(ms) {
  const iso = new Date(ms).toISOString();
  return { Y: iso.slice(0, 4), M: iso.slice(5, 7), D: iso.slice(8, 10), h: Number(iso.slice(11, 13)), mi: iso.slice(14, 16), s: iso.slice(17, 19) };
}

function fmtDate(p, withYear = true) {
  const order = { DMY: [p.D, p.M, p.Y], MDY: [p.M, p.D, p.Y], YMD: [p.Y, p.M, p.D] }[dateOrderSelect.value];
  const parts = withYear ? order : order.filter((v) => v !== p.Y);
  return parts.join("-");
}

function fmtTime(p, withSeconds = true) {
  const sec = withSeconds ? `:${p.s}` : "";
  if (hourFormatSelect.value === "12") {
    const h12 = p.h % 12 || 12;
    return `${String(h12).padStart(2, "0")}:${p.mi}${sec} ${p.h < 12 ? "AM" : "PM"}`;
  }
  return `${String(p.h).padStart(2, "0")}:${p.mi}${sec}`;
}

const fmtDateTime = (ms) => { const p = dateParts(ms); return `${fmtDate(p)} ${fmtTime(p)}`; };
const fmtIso = (iso) => (iso ? fmtDateTime(toMs(iso)) : "");

function formatExample() {
  const date = { DMY: "DD-MM-YYYY", MDY: "MM-DD-YYYY", YMD: "YYYY-MM-DD" }[dateOrderSelect.value];
  return `${date} ${hourFormatSelect.value === "12" ? "hh:mm:ss AM" : "HH:MM:SS"}`;
}

function fmtDuration(seconds) {
  seconds = Math.round(seconds);
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const hms = `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return d ? `${d}d ${hms}` : hms;
}

function showError(message) {
  errorSection.textContent = message;
  errorSection.classList.remove("hidden");
}

function clearError() {
  errorSection.classList.add("hidden");
}

function frameUrl(index, which, cropped) {
  return `/api/sessions/${state.session.id}/files/${index}/frame.jpg?which=${which}` +
    `${cropped ? "&cropped=true" : ""}&t=${Date.now()}`;
}

function settings() {
  return {
    region: state.region,
    date_order: dateOrderSelect.value,
    hour_format: hourFormatSelect.value,
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
    const n = session.files.length;
    folderStatus.textContent = `${n} video file${n === 1 ? "" : "s"} found`;
    folderStatus.className = "file-status ok";
    regionBtn.disabled = false;
    // Exports from the same camera share an overlay position, so a region
    // drawn for a previous folder is kept - just re-check it against this one.
    if (state.region) updateRegionThumb();
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

// ---------- step 2: region ----------
const regionModal = $("#region-modal");
const regionStage = $("#region-stage");
const regionImage = $("#region-image");
const regionBox = $("#region-box");
const previewSelect = $("#preview-file-select");
const testOcrBtn = $("#test-ocr-btn");
const testResult = $("#test-result");
const regionConfirmBtn = $("#region-confirm-btn");

regionBtn.addEventListener("click", openRegionPicker);

function openRegionPicker() {
  if (!state.session) return;
  previewSelect.innerHTML = state.session.files
    .map((f) => `<option value="${f.index}">${escapeHtml(f.name)}</option>`)
    .join("");
  previewSelect.value = String(state.previewIndex);
  state.pendingRegion = state.region;
  testResult.innerHTML = "";
  openModal(regionModal);
  loadPreviewImage();
}

previewSelect.addEventListener("change", () => {
  state.previewIndex = Number(previewSelect.value);
  testResult.innerHTML = "";
  loadPreviewImage();
});

function loadPreviewImage() {
  regionBox.classList.add("hidden");
  regionImage.onload = drawRegionBox;
  regionImage.onerror = () => { testResult.innerHTML = `<span class="err">Couldn't read a frame from this file.</span>`; };
  regionImage.src = frameUrl(state.previewIndex, "first", false);
}

function imageScale() {
  return regionImage.clientWidth / regionImage.naturalWidth;
}

function drawRegionBox() {
  const r = state.pendingRegion;
  const ready = !!r && regionImage.naturalWidth > 0;
  regionConfirmBtn.disabled = !ready;
  testOcrBtn.disabled = !ready;
  if (!ready) {
    regionBox.classList.add("hidden");
    return;
  }
  const s = imageScale();
  Object.assign(regionBox.style, {
    left: `${r[0] * s}px`, top: `${r[1] * s}px`, width: `${r[2] * s}px`, height: `${r[3] * s}px`,
  });
  regionBox.classList.remove("hidden");
}

let dragStart = null;

function stagePoint(e) {
  const rect = regionImage.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(rect.width, e.clientX - rect.left)),
    y: Math.max(0, Math.min(rect.height, e.clientY - rect.top)),
  };
}

regionStage.addEventListener("pointerdown", (e) => {
  if (!regionImage.naturalWidth) return;
  regionStage.setPointerCapture(e.pointerId);
  dragStart = stagePoint(e);
  testResult.innerHTML = "";
});

regionStage.addEventListener("pointermove", (e) => {
  if (!dragStart) return;
  const p = stagePoint(e);
  const s = imageScale();
  const x0 = Math.min(dragStart.x, p.x), y0 = Math.min(dragStart.y, p.y);
  const x1 = Math.max(dragStart.x, p.x), y1 = Math.max(dragStart.y, p.y);
  state.pendingRegion = [x0 / s, y0 / s, (x1 - x0) / s, (y1 - y0) / s].map(Math.round);
  drawRegionBox();
});

regionStage.addEventListener("pointerup", () => {
  if (!dragStart) return;
  dragStart = null;
  const r = state.pendingRegion;
  if (r && (r[2] < 5 || r[3] < 5)) {
    state.pendingRegion = null;
    drawRegionBox();
    testResult.innerHTML = `<span class="err">Selection too small - drag a larger box.</span>`;
  }
});

window.addEventListener("resize", () => {
  if (!regionModal.classList.contains("hidden")) drawRegionBox();
});

testOcrBtn.addEventListener("click", async () => {
  testOcrBtn.disabled = true;
  testResult.textContent = "Reading…";
  try {
    const res = await postJson(`/api/sessions/${state.session.id}/test`, {
      ...settings(), region: state.pendingRegion, index: state.previewIndex,
    });
    const conf = `${Math.round(res.confidence * 100)}% confidence`;
    testResult.innerHTML = res.datetime
      ? `<span class="ok">&#10003; ${escapeHtml(fmtIso(res.datetime))}</span> · read <code>${escapeHtml(res.raw_text)}</code> · ${conf}`
      : `<span class="err">Couldn't parse a timestamp</span> · read <code>${escapeHtml(res.raw_text || "(nothing)")}</code> - check the box and the date/time format`;
  } catch (e) {
    testResult.innerHTML = `<span class="err">${escapeHtml(e.message)}</span>`;
  } finally {
    testOcrBtn.disabled = false;
  }
});

regionConfirmBtn.addEventListener("click", () => {
  state.region = state.pendingRegion;
  closeModal(regionModal);
  updateRegionThumb();
  render();
});

function updateRegionThumb() {
  const [x, y, w, h] = state.region;
  regionStatus.textContent = `x ${x}, y ${y} · ${w}×${h} px`;
  regionStatus.className = "file-status ok";
  const img = new Image();
  img.onload = () => {
    if (x + w > img.naturalWidth || y + h > img.naturalHeight) {
      regionStatus.textContent = "Region is outside this folder's frame size - select it again.";
      regionStatus.className = "file-status err";
    }
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    canvas.getContext("2d").drawImage(img, x, y, w, h, 0, 0, w, h);
    regionThumb.style.backgroundImage = `url(${canvas.toDataURL()})`;
    regionThumb.classList.remove("empty");
  };
  img.src = frameUrl(state.previewIndex, "first", false);
}

// ---------- step 3: process ----------
runBtn.addEventListener("click", async () => {
  clearError();
  try {
    state.session = await postJson(`/api/sessions/${state.session.id}/process`, settings());
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
      if (state.session.status === "error") showError(state.session.error || "Processing failed");
    }
    render();
  }, 600);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

// ---------- rendering ----------
function render() {
  const s = state.session;
  const running = s && s.status === "running";
  runBtn.disabled = !s || !state.region || running;
  runBtn.textContent = s && s.files.some((f) => f.status !== "pending") ? "Reprocess Files" : "Process Files";
  cancelBtn.classList.toggle("hidden", !running);
  regionBtn.disabled = !s || running;
  dateOrderSelect.disabled = hourFormatSelect.disabled = running;

  const hasResults = s && s.files.some((f) => f.status !== "pending");
  exportBtn.classList.toggle("disabled", !hasResults);
  exportBtn.setAttribute("aria-disabled", String(!hasResults));
  if (hasResults) exportBtn.href = `/api/sessions/${s.id}/export.csv`;
  else exportBtn.removeAttribute("href");

  renderProgress();
  renderSummary();
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
  const pct = s.total ? (s.processed / s.total) * 100 : 0;
  progressFill.style.width = `${pct}%`;
  if (s.status === "running") {
    progressLabel.textContent = `${s.stage} — ${s.processed}/${s.total} files (${pct.toFixed(0)}%)`;
  } else if (s.status === "cancelled") {
    progressLabel.textContent = `Cancelled after ${s.processed}/${s.total} files.`;
  } else if (s.status === "error") {
    progressSection.classList.add("hidden");
  } else {
    const review = s.files.filter((f) => f.status === "needs_review" || f.status === "error").length;
    const estimated = s.files.filter((f) => f.status === "estimated").length;
    const est = estimated ? ` ${estimated} had one end worked out from the clip length.` : "";
    progressLabel.textContent = review
      ? `Done — ${review} file${review === 1 ? "" : "s"} need${review === 1 ? "s" : ""} review.${est}`
      : `Done — every file read.${est}`;
  }
}

function plottable() {
  if (!state.session) return [];
  return state.session.files
    .filter((f) => f.start && f.end)
    .map((f) => ({ ...f, t0: toMs(f.start), t1: toMs(f.end) }))
    .filter((f) => f.t1 >= f.t0)  // end-before-start is a misread; it's in the file list for review
    .sort((a, b) => a.t0 - b.t0);
}

// Merge overlapping clips into continuous coverage; the spaces between are gaps.
function coverage(items) {
  const merged = [];
  for (const it of items) {
    const last = merged[merged.length - 1];
    if (last && it.t0 <= last[1] + 1000) last[1] = Math.max(last[1], it.t1);
    else merged.push([it.t0, it.t1]);
  }
  const gaps = [];
  for (let i = 1; i < merged.length; i++) gaps.push([merged[i - 1][1], merged[i][0]]);
  return { merged, gaps };
}

function renderSummary() {
  const s = state.session;
  if (!s) {
    summaryCards.innerHTML = "";
    return;
  }
  const items = plottable();
  const { merged, gaps } = coverage(items);
  const card = (name, value, sub = "", cls = "") =>
    `<div class="score-card"><div class="metric-name">${name}</div>
      <div class="metric-value small ${cls}">${value}</div>${sub ? `<div class="metric-sub">${sub}</div>` : ""}</div>`;

  const review = s.files.filter((f) => f.status === "needs_review" || f.status === "error").length;
  let html = card("Files", `${items.length} / ${s.files.length}`, "on the timeline");
  if (items.length) {
    const tMin = items[0].t0;
    const tMax = Math.max(...items.map((i) => i.t1));
    const covered = merged.reduce((acc, [a, b]) => acc + (b - a), 0) / 1000;
    const gapTotal = gaps.reduce((acc, [a, b]) => acc + (b - a), 0) / 1000;
    html += card("Span", fmtDuration((tMax - tMin) / 1000), `${fmtDateTime(tMin)} →<br>${fmtDateTime(tMax)}`);
    html += card("Covered", fmtDuration(covered), `${(((covered * 1000) / Math.max(tMax - tMin, 1)) * 100).toFixed(1)}% of span`);
    html += card("Gaps", String(gaps.length), gaps.length ? `${fmtDuration(gapTotal)} missing` : "continuous", gaps.length ? "conf-warn" : "conf-good");
  }
  html += card("Needs review", String(review), "", review ? "conf-warn" : "conf-good");
  summaryCards.innerHTML = html;
}

const NICE_INTERVALS = [
  1, 2, 5, 10, 15, 30,
  60, 120, 300, 600, 900, 1800,
  3600, 7200, 10800, 21600, 43200,
  86400, 172800, 604800, 1209600, 2592000,
];

function computeTicks(tMin, tMax, maxTicks) {
  const span = (tMax - tMin) / 1000;
  let interval = NICE_INTERVALS[NICE_INTERVALS.length - 1];
  for (const c of NICE_INTERVALS) {
    if (span / c <= maxTicks) {
      interval = c;
      break;
    }
  }
  const step = interval * 1000;
  const ticks = [];
  for (let t = Math.ceil(tMin / step) * step; t <= tMax; t += step) ticks.push(t);
  return { interval, ticks };
}

function tickLabel(ms, interval, multiDay) {
  const p = dateParts(ms);
  if (interval >= 86400) return fmtDate(p);
  if (multiDay) return `${fmtDate(p, false)} ${fmtTime(p, false)}`;
  return fmtTime(p, interval < 60);
}

function barColor(f) {
  if (f.manual) return "var(--bar-manual)";
  if (f.status === "ok") return "var(--bar-ok)";
  return f.status === "estimated" ? "var(--bar-estimated)" : "var(--bar-review)";
}

function renderTimeline() {
  const items = plottable();
  if (!items.length) {
    const msg = state.session && state.session.files.some((f) => f.status !== "pending")
      ? "No file has both a start and end timestamp yet - correct them in the file list below."
      : "Load a folder, draw the timestamp region and process the files to build the timeline.";
    timelineEl.innerHTML = `<p class="placeholder">${msg}</p>`;
    return;
  }

  const width = Math.max(timelineEl.clientWidth, 480);
  const labelW = Math.min(220, width * 0.28);
  const padR = 24;
  const top = 34;
  const rowH = 26;
  const barH = 16;
  const plotW = width - labelW - padR;
  const height = top + items.length * rowH + 8;

  const tMin = items[0].t0;
  const tMax = Math.max(...items.map((i) => i.t1));
  const span = Math.max(tMax - tMin, 1000);
  const x = (t) => labelW + ((t - tMin) / span) * plotW;
  const multiDay = new Date(tMin).toISOString().slice(0, 10) !== new Date(tMax).toISOString().slice(0, 10);
  const { interval, ticks } = computeTicks(tMin, tMin + span, Math.max(3, Math.floor(plotW / 110)));
  const { gaps } = coverage(items);

  let svg = `<svg width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI, system-ui, sans-serif">`;

  for (const [a, b] of gaps) {
    svg += `<rect class="gap" x="${x(a)}" y="${top - 6}" width="${Math.max(x(b) - x(a), 1)}" height="${height - top}"><title>Gap: ${fmtDuration((b - a) / 1000)}</title></rect>`;
  }

  for (const t of ticks) {
    const tx = x(t);
    svg += `<line x1="${tx}" y1="${top - 6}" x2="${tx}" y2="${height}" stroke="var(--border)" />`;
    svg += `<text x="${tx}" y="${top - 12}" fill="var(--text-dim)" font-size="11" text-anchor="middle">${tickLabel(t, interval, multiDay)}</text>`;
  }
  svg += `<line x1="${labelW}" y1="${top - 6}" x2="${labelW + plotW}" y2="${top - 6}" stroke="var(--text-dim)" />`;

  items.forEach((f, row) => {
    const y = top + row * rowH;
    const x0 = x(f.t0);
    const w = Math.max(x(f.t1) - x0, 3);
    const name = f.name.length > 30 ? f.name.slice(0, 28) + "…" : f.name;
    svg += `<text x="${labelW - 10}" y="${y + barH / 2 + 4}" fill="var(--text)" font-size="11.5" text-anchor="end" font-family="Consolas, monospace">${escapeHtml(name)}</text>`;
    svg += `<rect class="bar" data-index="${f.index}" x="${x0}" y="${y}" width="${w}" height="${barH}" rx="3" fill="${barColor(f)}" />`;
  });
  svg += "</svg>";
  timelineEl.innerHTML = svg;

  timelineEl.querySelectorAll(".bar").forEach((bar) => {
    const f = state.session.files[Number(bar.dataset.index)];
    bar.addEventListener("mousemove", (e) => showTooltip(e, f));
    bar.addEventListener("mouseleave", hideTooltip);
    bar.addEventListener("click", () => openClip(f.index));
  });
}

function showTooltip(e, f) {
  const dur = (toMs(f.end) - toMs(f.start)) / 1000;
  tooltip.innerHTML = `<div class="tt-name">${escapeHtml(f.name)}</div>
    <div class="tt-row">${fmtIso(f.start)} → ${fmtIso(f.end)}</div>
    <div class="tt-row">${fmtDuration(dur)} · ${f.manual ? "manual" : `${Math.round(f.confidence * 100)}% confidence`}</div>`;
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

const STATUS_LABELS = { pending: "Pending", ok: "OK", estimated: "Estimated", needs_review: "Needs review", error: "Error" };

function renderTable() {
  const s = state.session;
  if (!s) {
    fileRows.innerHTML = `<tr><td colspan="7" class="placeholder">No folder loaded.</td></tr>`;
    return;
  }
  let files = s.files;
  if (state.filter === "review") files = files.filter((f) => f.status === "needs_review" || f.status === "error");
  if (!files.length) {
    fileRows.innerHTML = `<tr><td colspan="7" class="placeholder">Nothing needs review.</td></tr>`;
    return;
  }

  const timeCell = (iso, raw, estimated) =>
    iso
      ? `<td${estimated ? ` class="est" title="Worked out from the clip length"` : ""}>${estimated ? "≈ " : ""}${fmtIso(iso)}</td>`
      : `<td class="raw" title="Raw OCR text">${raw ? escapeHtml(raw) : "-"}</td>`;

  fileRows.innerHTML = files.map((f) => {
    const secs = f.start && f.end ? (toMs(f.end) - toMs(f.start)) / 1000 : null;
    const dur = secs === null ? "-" : secs < 0 ? `<span class="conf-bad">end before start</span>` : fmtDuration(secs);
    let conf = "-";
    if (f.manual) conf = "—";
    else if (f.status !== "pending") {
      const cls = f.confidence >= 0.8 ? "conf-good" : f.confidence >= 0.5 ? "conf-warn" : "conf-bad";
      conf = `<span class="${cls}">${Math.round(f.confidence * 100)}%</span>`;
    }
    const badge = f.manual
      ? `<span class="badge manual">Manual</span>`
      : `<span class="badge ${f.status}" ${f.error || f.note ? `title="${escapeHtml(f.error || f.note)}"` : ""}>${STATUS_LABELS[f.status]}</span>`;
    return `<tr>
      <td class="name">${escapeHtml(f.name)}</td>
      ${timeCell(f.start, f.start_raw, f.estimated === "start")}
      ${timeCell(f.end, f.end_raw, f.estimated === "end")}
      <td>${dur}</td>
      <td>${conf}</td>
      <td>${badge}</td>
      <td><button class="edit-btn" data-index="${f.index}">Edit</button></td>
    </tr>`;
  }).join("");

  fileRows.querySelectorAll(".edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => openEdit(Number(btn.dataset.index)));
  });
}

document.querySelectorAll("#filter-tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.filter = btn.dataset.filter;
    document.querySelectorAll("#filter-tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    renderTable();
  });
});

window.addEventListener("resize", () => {
  clearTimeout(renderTimeline._t);
  renderTimeline._t = setTimeout(renderTimeline, 120);
});

// ---------- manual correction ----------
const editModal = $("#edit-modal");
const editStart = $("#edit-start");
const editEnd = $("#edit-end");
const editError = $("#edit-error");
let editIndex = null;

function openEdit(index) {
  const f = state.session.files[index];
  editIndex = index;
  $("#edit-title").textContent = `Correct: ${f.name}`;
  const cropped = !!state.session.region;
  $("#edit-start-img").src = frameUrl(index, "first", cropped);
  $("#edit-end-img").src = frameUrl(index, "last", cropped);
  $("#edit-start-raw").textContent = f.start_raw || "-";
  $("#edit-end-raw").textContent = f.end_raw || "-";
  // unreadable ends start from the raw OCR text - usually only a character or two off
  editStart.value = f.start ? fmtIso(f.start) : (f.start_raw || "");
  editEnd.value = f.end ? fmtIso(f.end) : (f.end_raw || "");
  editStart.placeholder = editEnd.placeholder = formatExample();
  $("#edit-format").textContent = formatExample();
  $("#edit-note").textContent = f.error || f.note || "";
  $("#edit-actions").classList.toggle("hidden", !f.duration);
  if (f.duration) $("#edit-duration").textContent = fmtDuration(f.duration);
  editError.textContent = "";
  openModal(editModal);
  (editStart.value ? editEnd : editStart).focus();
}

async function saveEdit() {
  editError.textContent = "";
  try {
    const updated = await api(`/api/sessions/${state.session.id}/files/${editIndex}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start: editStart.value,
        end: editEnd.value,
        date_order: dateOrderSelect.value,
        hour_format: hourFormatSelect.value,
      }),
    });
    state.session.files[editIndex] = updated;
    closeModal(editModal);
    render();
  } catch (e) {
    editError.textContent = e.message;
  }
}

// Parse what's typed in an edit box (in the picked format) back to ms, or null.
function parseTyped(text) {
  const nums = (text.match(/\d+/g) || []).map(Number);
  if (nums.length < 6) return null;
  const [a, b, c, h, mi, sec] = nums;
  const [Y, M, D] = { DMY: [c, b, a], MDY: [c, a, b], YMD: [a, b, c] }[dateOrderSelect.value];
  let hour = h;
  if (hourFormatSelect.value === "12") {
    const pm = /p\.?m/i.test(text), am = /a\.?m/i.test(text);
    if (pm && hour !== 12) hour += 12;
    if (am && hour === 12) hour = 0;
  }
  const ms = Date.UTC(Y < 100 ? 2000 + Y : Y, M - 1, D, hour, mi, sec);
  return Number.isNaN(ms) ? null : ms;
}

function deriveEnd(fromInput, toInput, sign) {
  const f = state.session.files[editIndex];
  const ms = parseTyped(fromInput.value);
  if (ms === null) {
    editError.textContent = `Enter the ${fromInput === editStart ? "start" : "end"} time first`;
    return;
  }
  editError.textContent = "";
  toInput.value = fmtDateTime(ms + sign * Math.round(f.duration) * 1000);
}
$("#edit-derive-end").addEventListener("click", () => deriveEnd(editStart, editEnd, 1));
$("#edit-derive-start").addEventListener("click", () => deriveEnd(editEnd, editStart, -1));

$("#edit-save-btn").addEventListener("click", saveEdit);
[editStart, editEnd].forEach((el) => el.addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveEdit();
}));
$("#edit-open-btn").addEventListener("click", () => openClip(editIndex));

// display follows the format pickers
[dateOrderSelect, hourFormatSelect].forEach((el) => el.addEventListener("change", render));

// ---------- init ----------
render();
