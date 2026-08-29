/* Episode Sorter dashboard. Vanilla ES modules, no build step. */

const state = {
  view: "overview",
  status: null,
  openJob: null,
  paused: false,
  librarySort: localStorage.getItem("es-library-sort") || "recent",
  libraryOpen: JSON.parse(localStorage.getItem("es-library-open") || "{}"),
  overviewOpen: {},
  libraryShowAll: {},
  scanShare: 0,
  settings: null,
  timer: null,
  stream: null,
  version: null,
  activity: null,
  transfers: [],
  activityTimer: null,
  gemeldet: new Set(),
  anteilZuletzt: { id: null, wert: 0 },
};

const VIEWS = {
  overview: { title: "Übersicht", hint: "Was gerade läuft, was wartet und was schon einsortiert ist." },
  decisions: { title: "Entscheidungen", hint: "Dateien, bei denen die Zuordnung nicht sicher genug ist." },
  duplicates: { title: "Dubletten", hint: "Vorhandene Datei gegen neue Datei. Nichts wird automatisch überschrieben." },
  jdownloader: { title: "JDownloader", hint: "Laufende Downloads, Entpackvorgänge und fehlgeschlagene Pakete." },
  library: { title: "Bibliothek", hint: "Ordner, die in den konfigurierten Zielpfaden bereits existieren." },
  rules: { title: "Regeln", hint: "Gespeicherte Zuordnungen, die künftige Dateien automatisch erkennen." },
  log: { title: "Protokoll", hint: "Die letzten Ereignisse aus Watcher, Pipeline und Verschiebung." },
  settings: { title: "Einstellungen", hint: "Pfade, Schlüssel, Schwellen und Dateinamenregeln." },
};

const STATUS_LABEL = {
  waiting: "wartet",
  analyzing: "wird geprüft",
  review: "Entscheidung nötig",
  duplicate: "Dublette",
  ready: "bereit",
  planned: "geplant",
  moving: "wird verschoben",
  done: "einsortiert",
  failed: "Fehler",
  skipped: "verworfen",
};

const MEDIA_LABEL = { anime: "Anime", series: "Serie", movie: "Film", unknown: "unbekannt" };

// Fertige Jobs: die Erkennungssicherheit sagt danach nichts mehr aus.
const TERMINAL = new Set(["done", "skipped"]);

/* ------------------------------------------------------------------ utils */

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));

const qs = (selector, root = document) => root.querySelector(selector);
const qsa = (selector, root = document) => Array.from(root.querySelectorAll(selector));

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      if (payload.detail) message = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch (error) { /* keep the status text */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

let toastTimer = null;
function toast(message, tone = "neutral") {
  const node = qs("#toast");
  node.textContent = message;
  node.dataset.tone = tone;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 4200);
}

function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const diff = Math.round((Date.now() - then) / 1000);
  if (diff < 60) return `vor ${diff}s`;
  if (diff < 3600) return `vor ${Math.round(diff / 60)} min`;
  if (diff < 86400) {
    const hours = Math.round(diff / 3600);
    return hours === 1 ? "vor 1 h" : `vor ${hours} h`;
  }
  return new Date(iso).toLocaleDateString("de-DE");
}

function bytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function meter(confidence) {
  const value = Math.round(confidence || 0);
  const level = value >= 85 ? "high" : value >= 55 ? "mid" : "low";
  const hinweis = `Sicherheit der Erkennung: ${value} Prozent. Das ist kein Fortschritt.`;
  return `<span class="meter" title="${hinweis}" aria-label="${hinweis}">`
    + `<span class="meter-bar"><span class="meter-fill" data-level="${level}" style="width:${Math.max(4, value)}%"></span></span>`
    + `<span class="meter-value">${value}%</span></span>`;
}

function chip(status) {
  return `<span class="chip" data-state="${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;
}

function plural(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

function emptyState(title, text) {
  return `<div class="empty"><strong>${esc(title)}</strong>${esc(text)}</div>`;
}

/* ------------------------------------------------------------------ status */

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
  } catch (error) {
    state.status = null;
    renderStatusLine();
    return;
  }
  const counts = state.status.counts || {};
  const openCount = (counts.waiting || 0) + (counts.analyzing || 0) + (counts.ready || 0) + (counts.planned || 0);
  setCount("open", openCount);
  setCount("review", counts.review || 0, true);
  setCount("duplicate", counts.duplicate || 0, true);

  const toggle = qs("#dryRunToggle");
  if (document.activeElement !== toggle) toggle.checked = Boolean(state.status.dry_run);

  // Nur übernehmen, solange die eigene Umfrage nicht läuft, sonst überschreibt
  // der langsamere Status den frischeren Wert.
  if (!state.activityTimer) {
    state.activity = state.status.batch || null;
    state.transfers = state.status.transfers || [];
    renderActivity();
    syncActivityPolling();
  }
  renderStatusLine();
}

/* -------------------------------------------------------------- Fortschritt */

function aktuellerTransfer() {
  return (state.transfers || [])[0] || null;
}

function anteilDerDatei(transfer) {
  if (!transfer || !transfer.total) return 0;
  return Math.min(1, (transfer.copied || 0) / transfer.total);
}

function nurVorwaerts(id, anteil) {
  // Zwischen zwei Dateien ist kurz keine Kopie unterwegs. Ohne diese Sperre
  // fiele der Balken in dem Moment auf den Stand der fertigen Dateien zurück.
  if (state.anteilZuletzt.id !== id) state.anteilZuletzt = { id, wert: 0 };
  state.anteilZuletzt.wert = Math.max(state.anteilZuletzt.wert, Math.min(1, anteil));
  return state.anteilZuletzt.wert;
}

function activityText() {
  const batch = state.activity;
  const transfer = aktuellerTransfer();
  if (!batch && !transfer) return null;

  if (!batch) {
    // Einzelne Datei, etwa ein Klick auf "ersetzen" oder ein Lauf des Scheduler.
    return {
      key: `transfer:${transfer.name}`,
      label: `${transfer.phase === "prüfen" ? "Prüft" : "Verschiebt"} eine Datei`,
      detail: `${transfer.name} · ${Math.round(anteilDerDatei(transfer) * 100)} Prozent`,
      anteil: anteilDerDatei(transfer),
      tone: "live",
      batchId: null,
    };
  }

  const erledigt = (batch.done || 0) + (batch.failed || 0);
  if (batch.finished_at) {
    const fehler = batch.failed || 0;
    return {
      key: `batch:${batch.id}:fertig`,
      label: batch.cancelled ? "Abgebrochen" : "Fertig",
      detail: fehler
        ? `${batch.done} von ${batch.total} ${batch.label}, ${plural(fehler, "Fehler", "Fehler")}`
        : `${batch.done} von ${batch.total} ${batch.label}`,
      anteil: 1,
      tone: fehler ? "bad" : "good",
      batchId: null,
    };
  }

  const detail = [batch.current, transfer ? `${Math.round(anteilDerDatei(transfer) * 100)} Prozent` : null]
    .filter(Boolean).join(" · ");
  return {
    key: `batch:${batch.id}`,
    label: `${erledigt} von ${batch.total} ${batch.label}`,
    detail: detail || "wird vorbereitet",
    anteil: nurVorwaerts(batch.id, (erledigt + anteilDerDatei(transfer)) / Math.max(1, batch.total)),
    tone: "live",
    batchId: batch.id,
  };
}

function renderActivity() {
  const node = qs("#activity");
  if (!node) return;
  const info = activityText();
  if (!info) {
    node.hidden = true;
    node.dataset.key = "";
    node.innerHTML = "";
    return;
  }
  node.hidden = false;
  node.dataset.tone = info.tone;
  // Nur neu bauen, wenn sich die Form ändert. Sonst würde der Abbrechen-Knopf
  // jede Sekunde unter dem Zeiger neu entstehen.
  if (node.dataset.key !== info.key) {
    node.dataset.key = info.key;
    node.innerHTML = `
      <div class="activity-row">
        <strong class="activity-label"></strong>
        <span class="activity-detail"></span>
        ${info.batchId ? `<button type="button" class="btn btn-small" data-action="batch-cancel" data-batch="${esc(info.batchId)}">Abbrechen</button>` : ""}
      </div>
      <span class="activity-track" aria-hidden="true"><span class="activity-fill"></span></span>`;
  }
  qs(".activity-label", node).textContent = info.label;
  qs(".activity-detail", node).textContent = info.detail;
  qs(".activity-fill", node).style.width = `${Math.round(info.anteil * 100)}%`;
}

function activityAktiv() {
  const batch = state.activity;
  if (batch && !batch.finished_at) return true;
  if ((state.transfers || []).length) return true;
  return Boolean(batch);  // fertiger Stapel bleibt kurz stehen, danach räumt der Server ihn weg
}

async function pollActivity() {
  let payload;
  try {
    payload = await api("/api/activity");
  } catch (error) {
    return;  // ein Aussetzer beendet die Anzeige nicht
  }
  const vorher = state.activity;
  state.activity = payload.batch;
  state.transfers = payload.transfers || [];
  renderActivity();

  const batch = payload.batch;
  if (batch && batch.finished_at && !state.gemeldet.has(batch.id)) {
    state.gemeldet.add(batch.id);
    const fehler = batch.failed || 0;
    toast(fehler
      ? `${batch.done} von ${batch.total} ${batch.label}, ${plural(fehler, "Fehler", "Fehler")}`
      : `${batch.done} von ${batch.total} ${batch.label}`, fehler ? "bad" : "good");
    await refreshAndRender();
  } else if (vorher && batch && vorher.done !== batch.done) {
    // Eine Datei ist durch, die Listen dürfen nachziehen.
    await refreshStatus();
  }
  syncActivityPolling();
}

function syncActivityPolling() {
  if (activityAktiv()) {
    if (!state.activityTimer) state.activityTimer = setInterval(pollActivity, 1000);
  } else if (state.activityTimer) {
    clearInterval(state.activityTimer);
    state.activityTimer = null;
    renderActivity();
  }
}

function statusItem(label, tone = "idle", title = "") {
  // A dot only where the colour carries state. Grey dots would be decoration.
  const dot = tone === "idle" ? "" : '<i aria-hidden="true"></i>';
  return `<span class="status-item" data-tone="${tone}"${title ? ` title="${esc(title)}"` : ""}>${dot}${esc(label)}</span>`;
}

function renderStatusLine() {
  const node = qs("#statusLine");
  if (!node) return;
  const status = state.status;
  if (!status) {
    node.innerHTML = statusItem("Backend nicht erreichbar", "bad");
    return;
  }
  const jd = status.jd || {};
  const items = [];

  items.push(status.dry_run
    ? statusItem("Dry Run", "idle", "Es wird nur geplant, nichts verschoben")
    : statusItem("Verschieben aktiv", "live", "Dateien werden wirklich verschoben"));

  if (!jd.enabled) items.push(statusItem("Ordnerwache", "idle", "JDownloader-Anbindung ist aus, der Downloadordner wird trotzdem überwacht"));
  else if (jd.connected) items.push(statusItem(`JDownloader ${jd.device || ""}`.trim(), "ok"));
  else items.push(statusItem("JDownloader getrennt", "bad", jd.error || ""));

  if (!status.tmdb_configured) items.push(statusItem("TMDb fehlt", "warn", "Ohne Schlüssel werden Filme und Serien nicht geprüft"));

  const sources = status.metadata_sources || {};
  const NAMES = { tmdb: "TMDb", anilist: "AniList", jikan: "MyAnimeList" };
  // AniList und MyAnimeList sind Ersatz füreinander. Antwortet eine von beiden,
  // ist nichts kaputt und es gibt nichts zu melden.
  const ANIME_QUELLEN = ["anilist", "jikan"];
  const animeOk = ANIME_QUELLEN.some((key) => sources[key] && sources[key].ok);
  const down = Object.entries(sources).filter(([key, value]) =>
    value && value.ok === false && !(animeOk && ANIME_QUELLEN.includes(key)));
  if (down.length) {
    const labels = down.map(([key]) => NAMES[key] || key).join(", ");
    const why = down.map(([key, value]) => `${NAMES[key] || key}: ${value.error || "unbekannt"}`).join(" | ");
    items.push(statusItem(`${labels} antwortet nicht`, "warn", why));
  }
  if (status.prefer_anime) items.push(statusItem("Anime bevorzugt", "idle", "Unklare Fälle werden als Anime behandelt"));

  if (status.library_index && status.library_index.running) items.push(statusItem("Bibliothek wird eingelesen", "warn"));

  items.push(`<span class="status-item" data-tone="idle" id="scanTimer">${esc(scanTimerText())}</span>`);
  node.innerHTML = items.join("");
  tickScanTimer();
}

function scanTimerText() {
  const scheduler = (state.status && state.status.scheduler) || {};
  const last = scheduler.last_run && scheduler.last_run.at;
  if (scheduler.running) return "Prüfung läuft";
  if (!last) return "noch nicht geprüft";
  const since = Math.max(0, Math.round(Date.now() / 1000 - last));
  const interval = Number(scheduler.interval) || 60;
  const next = Math.max(0, interval - since);
  const sinceText = since < 60 ? `vor ${since} s` : `vor ${Math.round(since / 60)} min`;
  return `geprüft ${sinceText}, nächste in ${next} s`;
}

function tickScanTimer() {
  const node = qs("#scanTimer");
  if (node) node.textContent = scanTimerText();

  const bar = qs("#scanProgress");
  if (!bar) return;
  const scheduler = (state.status && state.status.scheduler) || {};
  const last = scheduler.last_run && scheduler.last_run.at;
  const interval = Number(scheduler.interval) || 60;
  if (!last) { bar.style.transform = "scaleX(0)"; return; }
  const share = Math.min(1, Math.max(0, (Date.now() / 1000 - last) / interval));
  // Reset jumps back instead of sweeping backwards through the whole bar.
  if (share < (state.scanShare || 0)) {
    bar.style.transition = "none";
    bar.style.transform = "scaleX(0)";
    void bar.offsetWidth;
    bar.style.transition = "";
  }
  state.scanShare = share;
  bar.style.transform = `scaleX(${share.toFixed(3)})`;
}

function setCount(key, value, alert = false) {
  const node = qs(`[data-count="${key}"]`);
  if (!node) return;
  node.textContent = value > 0 ? String(value) : "";
  if (alert && value > 0) node.dataset.tone = "alert"; else delete node.dataset.tone;
}

/* ------------------------------------------------------------------ icons */

const svgIcon = (paths) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;

const ICONS = {
  clock: svgIcon('<circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.6 2.6"/>'),
  alert: svgIcon('<circle cx="12" cy="12" r="8"/><path d="M12 8v4.5"/><path d="M12 15.5h.01"/>'),
  copy: svgIcon('<rect x="8" y="8" width="12" height="12" rx="2.4"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>'),
  check: svgIcon('<circle cx="12" cy="12" r="8"/><path d="M8.6 12.3l2.3 2.3 4.5-5"/>'),
  cross: svgIcon('<circle cx="12" cy="12" r="8"/><path d="M9.5 9.5l5 5M14.5 9.5l-5 5"/>'),
  star: svgIcon('<path d="M12 4.5l2.1 4.6 5 .5-3.8 3.4 1.1 4.9-4.4-2.6-4.4 2.6 1.1-4.9-3.8-3.4 5-.5z"/>'),
  tv: svgIcon('<rect x="4" y="7" width="16" height="12" rx="2"/><path d="M9 3.5l3 3 3-3"/>'),
  film: svgIcon('<rect x="4" y="4" width="16" height="16" rx="2.4"/><path d="M8 4v16M16 4v16M4 9h4M4 15h4M16 9h4M16 15h4"/>'),
  file: svgIcon('<path d="M7 3.5h7l4 4V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19V5A1.5 1.5 0 0 1 7 3.5z"/><path d="M14 3.5V8h4.5"/>'),
  dot: svgIcon('<circle cx="12" cy="12" r="3.4"/>'),
};

/* ------------------------------------------------------------------ charts */

// Shared tooltip for all SVG charts, follows the pointer.
document.addEventListener("mousemove", (event) => {
  const tip = qs("#chartTip");
  if (!tip) return;
  const target = event.target.closest("[data-tip]");
  if (!target) { tip.hidden = true; return; }
  tip.textContent = target.dataset.tip;
  tip.hidden = false;
  tip.style.left = `${Math.min(event.clientX + 14, window.innerWidth - tip.offsetWidth - 10)}px`;
  tip.style.top = `${event.clientY + 16}px`;
});

function pad2(value) { return String(value).padStart(2, "0"); }

function lastDays(count) {
  const days = [];
  const now = new Date();
  for (let i = count - 1; i >= 0; i -= 1) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    days.push({
      key: `${day.getFullYear()}-${pad2(day.getMonth() + 1)}-${pad2(day.getDate())}`,
      label: `${day.getDate()}.${day.getMonth() + 1}.`,
    });
  }
  return days;
}

function bucketByDay(jobs, days) {
  const buckets = new Map(days.map((day) => [day.key, { count: 0, bytes: 0 }]));
  for (const job of jobs) {
    const iso = job.finished_at || job.updated_at;
    if (!iso) continue;
    const when = new Date(iso);
    const key = `${when.getFullYear()}-${pad2(when.getMonth() + 1)}-${pad2(when.getDate())}`;
    const bucket = buckets.get(key);
    if (!bucket) continue;
    bucket.count += 1;
    bucket.bytes += job.size_bytes || 0;
  }
  return days.map((day) => buckets.get(day.key));
}

function niceMax(value) {
  if (value <= 4) return 4;
  const power = 10 ** Math.floor(Math.log10(value));
  const unit = value / power;
  return (unit <= 2 ? 2 : unit <= 5 ? 5 : 10) * power;
}

// Catmull-Rom spline through the points, so the line bends like the reference.
function smoothPath(points) {
  if (points.length < 3) return `M${points.map((p) => p.join(",")).join(" L")}`;
  let path = `M${points[0][0]},${points[0][1]}`;
  for (let i = 0; i < points.length - 1; i += 1) {
    const p0 = points[i - 1] || points[i];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[i + 2] || p2;
    const c1x = (p1[0] + (p2[0] - p0[0]) / 6).toFixed(1);
    const c1y = (p1[1] + (p2[1] - p0[1]) / 6).toFixed(1);
    const c2x = (p2[0] - (p3[0] - p1[0]) / 6).toFixed(1);
    const c2y = (p2[1] - (p3[1] - p1[1]) / 6).toFixed(1);
    path += `C${c1x},${c1y} ${c2x},${c2y} ${p2[0]},${p2[1]}`;
  }
  return path;
}

function areaChart(days, values, unit) {
  const width = 640;
  const height = 230;
  const padL = 34; const padR = 14; const padT = 14; const padB = 26;
  const plotH = height - padT - padB;
  const max = niceMax(Math.max(...values));
  const stepX = (width - padL - padR) / Math.max(1, days.length - 1);
  const x = (i) => +(padL + i * stepX).toFixed(1);
  const y = (v) => +(padT + plotH * (1 - v / max)).toFixed(1);
  const points = values.map((v, i) => [x(i), y(v)]);

  const gridLines = [0, 0.5, 1].map((share) => {
    const gy = y(max * share);
    return `<line class="grid-line" x1="${padL}" x2="${width - padR}" y1="${gy}" y2="${gy}"/>`
      + `<text class="axis-label" x="${padL - 8}" y="${gy + 3}" text-anchor="end">${Math.round(max * share)}</text>`;
  }).join("");

  const xLabels = days.map((day, i) => {
    if (i % 3 !== 0 && i !== days.length - 1) return "";
    return `<text class="axis-label" x="${x(i)}" y="${height - 8}" text-anchor="middle">${esc(day.label)}</text>`;
  }).join("");

  const line = smoothPath(points);
  const area = `${line}L${x(days.length - 1)},${y(0)}L${x(0)},${y(0)}Z`;

  const hits = days.map((day, i) => `
    <g>
      <rect class="hit" x="${(x(i) - stepX / 2).toFixed(1)}" y="${padT}" width="${stepX.toFixed(1)}" height="${plotH}"
            data-tip="${esc(day.label)} ${values[i]} ${esc(unit)}"/>
      <circle class="dot" cx="${x(i)}" cy="${y(values[i])}" r="4"/>
    </g>`).join("");

  return `<svg viewBox="0 0 ${width} ${height}" role="img">
    <defs>
      <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#21d4fd" stop-opacity="0.4"/>
        <stop offset="1" stop-color="#2152ff" stop-opacity="0"/>
      </linearGradient>
    </defs>
    ${gridLines}${xLabels}
    <path class="area-fill" d="${area}"/>
    <path class="area-line" d="${line}"/>
    ${hits}
  </svg>`;
}

function barChart(days, values, unit) {
  const width = 320;
  const height = 190;
  const padT = 12; const padB = 22; const padX = 10;
  const plotH = height - padT - padB;
  const max = Math.max(1, ...values);
  const stepX = (width - padX * 2) / days.length;
  const barW = 6;

  const bars = days.map((day, i) => {
    const value = values[i];
    const barH = plotH * (value / max);
    const bx = (padX + i * stepX + stepX / 2 - barW / 2).toFixed(1);
    const by = (padT + plotH - barH).toFixed(1);
    return `<g>
      <rect class="bar-hit" x="${(padX + i * stepX).toFixed(1)}" y="${padT}" width="${stepX.toFixed(1)}" height="${plotH}"
            data-tip="${esc(day.label)} ${value.toFixed(1)} ${esc(unit)}"/>
      ${value > 0 ? `<rect class="bar" x="${bx}" y="${by}" width="${barW}" height="${barH.toFixed(1)}" rx="3"/>` : ""}
    </g>`;
  }).join("");

  const labels = days.map((day, i) => {
    if (i !== 0 && i !== days.length - 1 && i !== Math.floor(days.length / 2)) return "";
    return `<text class="axis-label" x="${(padX + i * stepX + stepX / 2).toFixed(1)}" y="${height - 6}" text-anchor="middle">${esc(day.label)}</text>`;
  }).join("");

  return `<svg viewBox="0 0 ${width} ${height}" role="img">${bars}${labels}</svg>`;
}

function gaugeArc(cx, cy, r, startDeg, endDeg) {
  const rad = (deg) => ((deg - 90) * Math.PI) / 180;
  const sx = (cx + r * Math.cos(rad(startDeg))).toFixed(1);
  const sy = (cy + r * Math.sin(rad(startDeg))).toFixed(1);
  const ex = (cx + r * Math.cos(rad(endDeg))).toFixed(1);
  const ey = (cy + r * Math.sin(rad(endDeg))).toFixed(1);
  const large = endDeg - startDeg > 180 ? 1 : 0;
  return `M${sx},${sy}A${r},${r} 0 ${large} 1 ${ex},${ey}`;
}

function gaugeSvg(share) {
  const sweep = 270;
  const start = -135;
  const end = start + sweep * Math.min(1, Math.max(0, share));
  return `<svg viewBox="0 0 160 160" role="img">
    <defs>
      <linearGradient id="gaugeGrad" x1="0" y1="1" x2="1" y2="0">
        <stop offset="0" stop-color="#2152ff"/>
        <stop offset="1" stop-color="#21d4fd"/>
      </linearGradient>
    </defs>
    <path class="gauge-track" d="${gaugeArc(80, 80, 66, start, start + sweep)}"/>
    ${share > 0 ? `<path class="gauge-fill" d="${gaugeArc(80, 80, 66, start, Math.max(end, start + 4))}"/>` : ""}
  </svg>`;
}

/* ------------------------------------------------------------------ views */

function kpiTile(tile) {
  const delta = tile.delta
    ? `<span class="kpi-delta" data-tone="${tile.tone || "good"}">${esc(tile.delta)}</span>` : "";
  return `<div class="kpi">
    <div>
      <div class="kpi-label">${esc(tile.label)}</div>
      <div class="kpi-value">${tile.value}${delta}</div>
    </div>
    <span class="kpi-icon">${tile.icon}</span>
  </div>`;
}

function ministat(icon, label, value, share) {
  return `<div class="ministat">
    <span class="ministat-head"><span class="nav-icon">${icon}</span>${esc(label)}</span>
    <span class="ministat-value">${value}</span>
    <span class="ministat-bar"><i style="width:${Math.round(Math.min(1, share) * 100)}%"></i></span>
  </div>`;
}

function timelineItem(event) {
  const icon = event.level === "error" ? ICONS.cross : event.level === "warn" ? ICONS.alert : ICONS.dot;
  return `<div class="tl-item" data-level="${esc(event.level)}">
    <span class="tl-dot">${icon}</span>
    <div>
      <div class="tl-text">${esc(event.message)}</div>
      <div class="tl-meta">${esc(event.source)} · ${new Date(event.ts).toLocaleString("de-DE")}</div>
    </div>
  </div>`;
}

// Recent jobs collapse into one line per title. A season of downloads is one
// story, not twelve rows.
const GROUP_STATUS_ORDER = ["failed", "review", "duplicate", "moving", "analyzing", "waiting", "ready", "planned", "done", "skipped"];

function groupRecentJobs(jobs) {
  const groups = new Map();
  for (const job of jobs) {
    const title = job.title || job.parsed_title || job.filename;
    const key = `${job.media_type}|${title}`;
    if (!groups.has(key)) groups.set(key, { key, title, media_type: job.media_type, jobs: [] });
    groups.get(key).jobs.push(job);
  }
  return [...groups.values()];
}

function groupChips(jobs) {
  const counts = {};
  for (const job of jobs) counts[job.status] = (counts[job.status] || 0) + 1;
  return GROUP_STATUS_ORDER
    .filter((status) => counts[status])
    .map((status) => `<span class="chip" data-state="${status}">${counts[status]} ${esc(STATUS_LABEL[status] || status)}</span>`)
    .join("");
}

function groupSub(group) {
  const parts = [MEDIA_LABEL[group.media_type] || "unbekannt"];
  const seasons = [...new Set(group.jobs.map((job) => job.season).filter((s) => s !== null && s !== undefined))]
    .sort((a, b) => a - b);
  if (seasons.length === 1) parts.push(`Staffel ${String(seasons[0]).padStart(2, "0")}`);
  else if (seasons.length > 1) parts.push(`Staffel ${seasons[0]}-${seasons[seasons.length - 1]}`);
  const size = group.jobs.reduce((sum, job) => sum + (job.size_bytes || 0), 0);
  if (size) parts.push(bytes(size));
  const newest = group.jobs.reduce((latest, job) => (job.updated_at > latest ? job.updated_at : latest), "");
  if (newest) parts.push(relTime(newest));
  return parts.join(" / ");
}

function overviewGroupRow(group) {
  if (group.jobs.length === 1) return jobRow(group.jobs[0]);
  const open = Boolean(state.overviewOpen[group.key]);
  const episodes = [...group.jobs].sort((a, b) =>
    (a.season ?? 0) - (b.season ?? 0)
    || (a.episode ?? a.absolute_episode ?? 0) - (b.episode ?? b.absolute_episode ?? 0));
  const unit = group.jobs.every((job) => job.episode || job.absolute_episode) ? "Folgen" : "Dateien";
  return `
    <article class="row row-group${open ? " is-open" : ""}">
      <div class="group-toggle" data-action="group-toggle" data-key="${esc(group.key)}" role="button" tabindex="0"
           aria-expanded="${open}" aria-label="${esc(group.title)}: ${group.jobs.length} ${unit} ${open ? "einklappen" : "ausklappen"}">
        <div class="row-title">${esc(group.title)}</div>
        <div class="row-sub">${esc(groupSub(group))}</div>
      </div>
      <div class="group-chips">${groupChips(group.jobs)}</div>
      <div class="row-actions">
        <button class="btn btn-small" data-action="group-toggle" data-key="${esc(group.key)}">
          ${group.jobs.length} ${unit}<span class="group-caret" aria-hidden="true"></span>
        </button>
      </div>
      ${open ? `<div class="row-children">${episodes.map(jobRow).join("")}</div>` : ""}
    </article>`;
}

async function renderOverview(root) {
  const status = state.status || {};
  const counts = status.counts || {};
  const [jobsPayload, donePayload, eventsPayload, libraryPayload] = await Promise.all([
    api("/api/jobs?limit=60"),
    api("/api/jobs?status=done&limit=1000"),
    api("/api/events?limit=8"),
    api("/api/library?limit=2000"),
  ]);
  const jobs = jobsPayload.jobs;
  const doneJobs = donePayload.jobs;
  const libraryItems = libraryPayload.items || [];

  const days = lastDays(14);
  const buckets = bucketByDay(doneJobs, days);
  const dayCounts = buckets.map((bucket) => bucket.count);
  const dayGigabytes = buckets.map((bucket) => bucket.bytes / 2 ** 30);
  const doneToday = dayCounts[dayCounts.length - 1];
  const doneWeek = dayCounts.slice(-7).reduce((a, b) => a + b, 0);
  const totalVolume = buckets.reduce((sum, bucket) => sum + bucket.bytes, 0);

  const open = (counts.waiting || 0) + (counts.analyzing || 0) + (counts.ready || 0)
    + (counts.planned || 0) + (counts.moving || 0);
  const kpis = [
    { label: "Offen", value: open, icon: ICONS.clock },
    { label: "Entscheidung nötig", value: counts.review || 0, icon: ICONS.alert,
      delta: (counts.review || 0) > 0 ? "wartet" : "", tone: "warn" },
    { label: "Dubletten", value: counts.duplicate || 0, icon: ICONS.copy,
      delta: (counts.duplicate || 0) > 0 ? "wartet" : "", tone: "warn" },
    { label: "Einsortiert", value: counts.done || 0, icon: ICONS.check,
      delta: doneToday > 0 ? `+${doneToday} heute` : "", tone: "good" },
  ];
  if (counts.failed) kpis.push({ label: "Fehler", value: counts.failed, icon: ICONS.cross, delta: "prüfen", tone: "bad" });

  const warnings = [];
  if (!status.download_dir_ok) warnings.push(`Der Downloadordner <strong>${esc(status.download_dir)}</strong> ist nicht erreichbar.`);
  (status.library_roots || []).forEach((root_) => {
    if (!root_.exists) warnings.push(`Zielpfad <strong>${esc(root_.path)}</strong> existiert nicht.`);
    else if (!root_.writable) warnings.push(`Zielpfad <strong>${esc(root_.path)}</strong> ist nicht beschreibbar.`);
  });
  if (!status.tmdb_configured) warnings.push("Kein TMDb-Schlüssel hinterlegt. Filme und Serien werden nur über AniList oder gar nicht geprüft.");
  if (status.dry_run) warnings.push("Dry Run ist aktiv. Es wird nichts verschoben, nur geplant.");

  // Success share across everything that reached a final state.
  const finished = (counts.done || 0) + (counts.failed || 0) + (counts.skipped || 0);
  const successShare = finished ? (counts.done || 0) / finished : 0;

  const groups = groupRecentJobs(jobs).slice(0, 10);

  const mediaCounts = { anime: 0, series: 0, movie: 0 };
  let fileTotal = 0;
  for (const item of libraryItems) {
    if (mediaCounts[item.media_type] !== undefined) mediaCounts[item.media_type] += 1;
    fileTotal += item.file_count || 0;
  }
  const maxMedia = Math.max(1, mediaCounts.anime, mediaCounts.series, mediaCounts.movie);

  root.innerHTML = `
    <section class="kpis">${kpis.map(kpiTile).join("")}</section>

    ${warnings.length ? `<section class="block">${warnings.map((text) => `<div class="notice" data-tone="warn">${text}</div>`).join("")}</section>` : ""}

    <section class="grid-charts">
      <div class="chart-card">
        <div class="card-title">Einsortiert pro Tag</div>
        <div class="card-sub"><strong>${doneWeek}</strong> ${doneWeek === 1 ? "Datei" : "Dateien"} in den letzten 7 Tagen</div>
        <div class="chart">${areaChart(days, dayCounts, "einsortiert")}</div>
      </div>
      <div class="chart-card">
        <div class="card-title">Datenvolumen pro Tag</div>
        <div class="card-sub"><strong>${bytes(totalVolume)}</strong> in 14 Tagen verschoben</div>
        <div class="bars-panel">${barChart(days, dayGigabytes, "GB")}</div>
        <div class="ministats">
          ${ministat(ICONS.star, "Anime", mediaCounts.anime, mediaCounts.anime / maxMedia)}
          ${ministat(ICONS.tv, "Serien", mediaCounts.series, mediaCounts.series / maxMedia)}
          ${ministat(ICONS.film, "Filme", mediaCounts.movie, mediaCounts.movie / maxMedia)}
          ${ministat(ICONS.file, "Dateien", fileTotal, 1)}
        </div>
      </div>
    </section>

    <section class="grid-bottom">
      <div class="block">
        <div class="block-head">
          <div><h2>Letzte Dateien</h2><p>${plural(jobs.length, "Eintrag", "Einträge")} in ${plural(groups.length, "Titel", "Titeln")}, neueste zuerst</p></div>
        </div>
        ${groups.length ? `<div class="rows">${groups.map(overviewGroupRow).join("")}</div>` : emptyState("Noch nichts gesehen", "Sobald JDownloader eine Datei fertig entpackt hat, taucht sie hier auf.")}
      </div>
      <div class="block" style="gap: 18px;">
        <div class="gauge-card">
          <div class="card-title">Erfolgsquote</div>
          <div class="card-sub">Anteil einsortierter Dateien</div>
          <div class="gauge-wrap">
            ${gaugeSvg(successShare)}
            <div class="gauge-center">
              <div class="gauge-value">${finished ? Math.round(successShare * 100) : 0}%</div>
              <div class="gauge-hint">${plural(finished, "Datei gesamt", "Dateien gesamt")}</div>
            </div>
          </div>
          <div class="gauge-foot">
            <span><strong>${counts.done || 0}</strong> einsortiert</span>
            <span><strong>${counts.skipped || 0}</strong> verworfen</span>
            <span><strong>${counts.failed || 0}</strong> Fehler</span>
          </div>
        </div>
        <div class="side-card">
          <div class="card-title">Protokoll</div>
          <div class="card-sub">letzte Ereignisse</div>
          <div class="timeline">${eventsPayload.events.map(timelineItem).join("") || `<div class="card-sub">Noch keine Ereignisse.</div>`}</div>
        </div>
      </div>
    </section>
  `;
  bindJobRows(root);
}

function jobRow(job) {
  const target = job.target_path
    ? job.target_path.replace(/^(.*\/)([^/]+)$/, (_, dir, file) => `${esc(dir)}<em>${esc(file)}</em>`)
    : "<span class=\"row-sub\">noch kein Ziel geplant</span>";
  const label = [MEDIA_LABEL[job.media_type] || "unbekannt", job.title || job.parsed_title || "kein Titel"]
    .filter(Boolean)
    .join(" / ");
  const numbers = job.season !== null && job.season !== undefined && job.episode
    ? `S${String(job.season).padStart(2, "0")}E${String(job.episode).padStart(2, "0")}`
    : job.absolute_episode
      ? `absolute Folge ${job.absolute_episode}`
      : job.year
        ? String(job.year)
        : "";
  return `
    <article class="row${state.openJob === job.id ? " is-open" : ""}" data-job="${job.id}">
      <div>
        <div class="row-title">${esc(job.filename)}</div>
        <div class="row-sub">${esc(label)}${numbers ? ` / ${esc(numbers)}` : ""} / ${esc(bytes(job.size_bytes))}</div>
      </div>
      <div class="path">${target}</div>
      <div>${chip(job.status)}<div class="row-sub">${relTime(job.updated_at)}</div></div>
      <div class="row-meter">${TERMINAL.has(job.status) ? "" : meter(job.confidence)}</div>
      <div class="row-actions">
        <button class="btn btn-small" data-action="toggle" data-job="${job.id}">${state.openJob === job.id ? "Schließen" : "Details"}</button>
      </div>
      ${state.openJob === job.id ? jobDetail(job) : ""}
    </article>`;
}

function jobDetail(job) {
  const parse = job.parse_debug || {};
  const candidates = (job.candidates || []).slice(0, 6);
  const decided = TERMINAL.has(job.status);
  return `
    <div class="detail">
      <div>
        <h3>Erkannt</h3>
        <dl class="kv">
          <dt>Quelle</dt><dd class="path">${esc(job.source_path)}</dd>
          <dt>Paket</dt><dd>${esc(job.package_name || "kein Paket")}</dd>
          <dt>Parser</dt><dd>${esc(parse.pattern || "unbekannt")}${parse.group ? ` / Gruppe ${esc(parse.group)}` : ""}</dd>
          <dt>Titel roh</dt><dd>${esc(job.parsed_title || "")}</dd>
          <dt>Status</dt><dd>${esc(job.reason || "")}</dd>
          ${job.error ? `<dt>Fehler</dt><dd>${esc(job.error)}</dd>` : ""}
          ${job.existing_folder ? `<dt>Ordner</dt><dd class="path">${esc(job.existing_folder)}</dd>` : ""}
          ${(job.companions || []).length ? `<dt>Untertitel</dt><dd>${job.companions.map((item) => esc((item.path || "").split("/").pop())).join("<br>")}</dd>` : ""}
        </dl>
      </div>

      <div>
        <h3>Treffer aus TMDb und AniList</h3>
        <div class="candidates">
          ${candidates.length ? candidates.map((candidate) => `
            <button class="candidate" data-action="candidate" data-job="${job.id}" data-source="${esc(candidate.source)}" data-id="${candidate.external_id}">
              <span>${esc(candidate.english_title || candidate.title)}${candidate.year ? ` (${candidate.year})` : ""}
                <span class="candidate-meta">${esc(candidate.source)} / ${MEDIA_LABEL[candidate.media_type] || candidate.media_type}${candidate.episodes ? ` / ${candidate.episodes} Folgen` : ""}</span>
              </span>
              <span class="candidate-meta">${Math.round((candidate.score || 0) * 100)}%</span>
            </button>`).join("") : `<p class="row-sub">Keine Metadaten gefunden. Titel unten manuell setzen.</p>`}
        </div>
      </div>

      <div>
        <h3>${decided ? "Abgeschlossen" : "Ziel festlegen"}</h3>
        ${decided ? `<dl class="kv">
          <dt>Ziel</dt><dd class="path">${esc(job.target_path || "")}</dd>
          <dt>Ergebnis</dt><dd>${esc(job.reason || "")}</dd>
        </dl>` : ""}
        <form class="form"${decided ? ' hidden' : ''} data-action="override" data-job="${job.id}" style="grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));">
          <div class="field"><label for="title-${job.id}">Titel</label><input type="text" id="title-${job.id}" name="title" value="${esc(job.title || job.parsed_title || "")}"></div>
          <div class="field"><label for="year-${job.id}">Jahr</label><input type="number" id="year-${job.id}" name="year" value="${job.year || ""}"></div>
          <div class="field"><label for="type-${job.id}">Art</label>
            <select id="type-${job.id}" name="media_type">
              ${["anime", "series", "movie"].map((type) => `<option value="${type}"${job.media_type === type ? " selected" : ""}>${MEDIA_LABEL[type]}</option>`).join("")}
            </select>
          </div>
          <div class="field"><label for="season-${job.id}">Staffel</label><input type="number" id="season-${job.id}" name="season" value="${job.season ?? ""}"></div>
          <div class="field"><label for="episode-${job.id}">Folge</label><input type="number" id="episode-${job.id}" name="episode" value="${job.episode ?? job.absolute_episode ?? ""}"></div>
          <div class="field" style="grid-column: 1 / -1;"><label for="root-${job.id}">Zielordner</label>
            <input type="text" id="root-${job.id}" name="target_dir" placeholder="${esc(job.target_dir || "")}" value="${esc(job.existing_folder || "")}">
            <small>Leer lassen, damit der Standardpfad der gewählten Art verwendet wird.</small>
          </div>
          <div class="field" style="grid-column: 1 / -1;">
            <label class="switch" for="rule-${job.id}">
              <input type="checkbox" id="rule-${job.id}" name="save_rule">
              <span class="switch-track" aria-hidden="true"><span class="switch-knob"></span></span>
              <span class="switch-label">Als Regel speichern</span>
            </label>
          </div>
          <div class="row-actions" style="grid-column: 1 / -1; justify-content: flex-start;">
            <button class="btn btn-primary" type="submit" data-decision="approve">Übernehmen</button>
            <button class="btn" type="submit" data-decision="override">Ziel merken</button>
            <button class="btn" type="button" data-action="decision" data-decision="retry" data-job="${job.id}">Neu prüfen</button>
            <button class="btn btn-danger" type="button" data-action="decision" data-decision="skip" data-job="${job.id}">Verwerfen</button>
          </div>
        </form>
      </div>
    </div>`;
}

function logLine(event) {
  return `<div class="logline" data-level="${esc(event.level)}">
    <time>${new Date(event.ts).toLocaleString("de-DE")}</time>
    <span>${esc(event.source)}</span>
    <span>${esc(event.message)}</span>
  </div>`;
}

async function renderDecisions(root) {
  const { jobs } = await api("/api/jobs?status=review,failed&limit=200");
  root.innerHTML = jobs.length
    ? `<section class="block">
        <div class="block-head"><div><h2>${plural(jobs.length, "offene Entscheidung", "offene Entscheidungen")}</h2><p>Titel prüfen, Ziel setzen, übernehmen.</p></div></div>
        <div class="rows">${jobs.map(jobRow).join("")}</div>
      </section>`
    : emptyState("Nichts offen", "Alle erkannten Dateien konnten automatisch zugeordnet werden.");
  bindJobRows(root);
}

function dublettenGruppen(jobs) {
  const gruppen = new Map();
  for (const job of jobs) {
    const titel = job.title || job.parsed_title || job.filename;
    const staffel = job.season === null || job.season === undefined ? null : job.season;
    const schluessel = `${titel}|${staffel}`;
    if (!gruppen.has(schluessel)) {
      gruppen.set(schluessel, { titel, staffel, jobs: [] });
    }
    gruppen.get(schluessel).jobs.push(job);
  }
  for (const gruppe of gruppen.values()) {
    gruppe.jobs.sort((a, b) => (a.episode || 0) - (b.episode || 0));
  }
  return [...gruppen.values()].sort((a, b) => b.jobs.length - a.jobs.length || a.titel.localeCompare(b.titel, "de"));
}

function folgenName(job) {
  if (job.season !== null && job.season !== undefined && job.episode) {
    return `S${String(job.season).padStart(2, "0")}E${String(job.episode).padStart(2, "0")}`;
  }
  if (job.absolute_episode) return `Folge ${job.absolute_episode}`;
  return job.filename.slice(0, 28);
}

function kurzInfo(info) {
  if (!info) return "unbekannt";
  const teile = [];
  if (info.size) teile.push(bytes(info.size));
  if (info.resolution) teile.push(info.resolution);
  if (info.video_codec) teile.push(info.video_codec);
  if ((info.audio || []).length) teile.push(`${info.audio.length} Ton`);
  return teile.join(" / ") || "unbekannt";
}

function groessenVergleich(neu, alt) {
  if (!alt || !neu) return "";
  const faktor = neu / alt;
  const runden = (wert) => (wert >= 10 ? Math.round(wert) : wert.toFixed(1));
  if (faktor >= 2) return `neu ist ${runden(faktor)} mal so groß`;
  if (faktor <= 0.5) return `neu ist ${runden(1 / faktor)} mal kleiner`;
  const prozent = Math.round((faktor - 1) * 100);
  if (Math.abs(prozent) <= 2) return "gleich groß";
  return prozent > 0 ? `neu ist ${prozent} Prozent größer` : `neu ist ${Math.abs(prozent)} Prozent kleiner`;
}

function dublettenZeile(job) {
  const info = job.duplicate_info || {};
  const neu = (info.incoming || {}).size || job.size_bytes || 0;
  const alt = (info.existing || {}).size || 0;
  const tendenz = groessenVergleich(neu, alt);
  return `
    <div class="list-item" data-job="${job.id}">
      <span>
        <strong>${esc(folgenName(job))}</strong>
        <span class="row-sub" style="margin-left: 10px;">neu ${esc(kurzInfo(info.incoming))}</span>
        <div class="row-sub">alt ${esc(kurzInfo(info.existing))}${tendenz ? ` / ${esc(tendenz)}` : ""}</div>
      </span>
      <span class="row-actions">
        <button class="btn btn-small" data-action="decision" data-decision="duplicate_discard" data-job="${job.id}">verwerfen</button>
        <button class="btn btn-small" data-action="decision" data-decision="duplicate_keep_both" data-job="${job.id}">behalten</button>
        <button class="btn btn-small btn-danger" data-action="decision" data-decision="duplicate_replace" data-job="${job.id}">ersetzen</button>
      </span>
    </div>`;
}

function dublettenGruppe(gruppe) {
  const ids = gruppe.jobs.map((job) => job.id).join(",");
  const anzahl = gruppe.jobs.length;
  const summe = gruppe.jobs.reduce((wert, job) => wert + (job.size_bytes || 0), 0);
  const staffel = gruppe.staffel === null ? "" : ` / Staffel ${String(gruppe.staffel).padStart(2, "0")}`;
  const ziel = gruppe.jobs[0].target_dir || "";
  // Solange ein Stapel läuft, gäbe ein zweiter Klick nur einen zweiten Stapel.
  const sperre = state.activity && !state.activity.finished_at
    ? ' disabled title="Es läuft gerade ein Vorgang"' : "";
  return `
    <section class="block" data-gruppe="${esc(gruppe.titel)}">
      <div class="block-head">
        <div>
          <h2>${esc(gruppe.titel)}${esc(staffel)}</h2>
          <p>${plural(anzahl, "Dublette", "Dubletten")}, ${esc(bytes(summe))} neu geladen<br>
             <span class="path">${esc(ziel)}</span></p>
        </div>
        ${anzahl > 1 ? `<div class="row-actions">
          <button class="btn btn-small" data-action="dup-gruppe" data-decision="duplicate_discard" data-ids="${ids}"${sperre}>Alle verwerfen</button>
          <button class="btn btn-small" data-action="dup-gruppe" data-decision="duplicate_keep_both" data-ids="${ids}"${sperre}>Alle behalten</button>
          <button class="btn btn-small btn-danger" data-action="dup-gruppe" data-decision="duplicate_replace" data-ids="${ids}"
                  data-frage="Alle ${anzahl} ersetzen?"${sperre}>Alle ersetzen</button>
        </div>` : ""}
      </div>
      <div class="list">${gruppe.jobs.map(dublettenZeile).join("")}</div>
    </section>`;
}

async function renderDuplicates(root) {
  const { jobs } = await api("/api/jobs?status=duplicate&limit=400");
  if (!jobs.length) {
    root.innerHTML = emptyState("Keine Dubletten", "Es liegt keine Datei vor, die es in der Bibliothek schon gibt.");
    return;
  }
  const gruppen = dublettenGruppen(jobs);
  root.innerHTML = `
    <section class="block">
      <div class="block-head">
        <div>
          <h2>${plural(jobs.length, "Dublette", "Dubletten")} in ${plural(gruppen.length, "Titel", "Titeln")}</h2>
          <p>Nichts wird automatisch überschrieben. Ganze Staffeln lassen sich in einem Zug entscheiden.</p>
        </div>
      </div>
    </section>
    ${gruppen.map(dublettenGruppe).join("")}`;
  bindJobRows(root);
}

function mediaFacts(info) {
  if (!info) return "<p class=\"row-sub\">Keine technischen Daten verfügbar.</p>";
  const rows = [
    ["Datei", (info.path || "").split("/").pop()],
    ["Größe", bytes(info.size)],
    ["Auflösung", info.resolution || "unbekannt"],
    ["Codec", info.video_codec || "unbekannt"],
    ["Audio", (info.audio || []).join(", ") || "unbekannt"],
    ["Untertitel", (info.subtitles || []).join(", ") || "keine"],
    ["Laufzeit", info.duration_minutes ? `${info.duration_minutes} min` : "unbekannt"],
    ["Bitrate", info.bitrate_kbps ? `${info.bitrate_kbps} kbit/s` : "unbekannt"],
  ];
  return `<dl class="kv">${rows.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>`;
}

async function renderJDownloader(root) {
  const payload = await api("/api/jd/packages");
  const packages = payload.packages || [];
  const jd = (state.status && state.status.jd) || {};
  root.innerHTML = `
    <section class="block">
      <div class="block-head">
        <div><h2>Verbindung</h2><p>My JDownloader steuert die Paketliste, der Ordnerwächter läuft unabhängig.</p></div>
        <div class="row-actions"><button class="btn" data-action="jd-connect">Neu verbinden</button></div>
      </div>
      <div class="notice"${payload.error ? ' data-tone="bad"' : ""}>
        ${jd.enabled ? "" : "Die JDownloader-Anbindung ist in den Einstellungen deaktiviert. "}
        ${payload.connected ? `Verbunden mit <strong>${esc(jd.device || "Gerät")}</strong>.` : "Nicht verbunden."}
        ${payload.error ? ` Letzter Fehler: ${esc(payload.error)}` : ""}
        Der Ordnerwächter arbeitet unabhängig davon weiter.
      </div>
    </section>
    <section class="block">
      <div class="block-head"><div><h2>Pakete</h2><p>${plural(packages.length, "Paket", "Pakete")} bekannt</p></div></div>
      ${packages.length ? `<div class="rows">${packages.map(packageRow).join("")}</div>` : emptyState("Keine Pakete", "JDownloader meldet gerade keine Downloads.")}
    </section>`;
  bindJobRows(root);
}

function packageRow(pkg) {
  const stateText = pkg.failed ? "Fehler" : pkg.extracting ? "wird entpackt" : pkg.finished ? "fertig" : "lädt";
  const tone = pkg.failed ? "failed" : pkg.extracting ? "analyzing" : pkg.finished ? "done" : "waiting";
  return `<article class="row">
    <div>
      <div class="row-title">${esc(pkg.name)}</div>
      <div class="row-sub">${esc(pkg.status_text || "")}</div>
    </div>
    <div class="path">${esc(pkg.save_to || "")}</div>
    <div><span class="chip" data-state="${tone}">${esc(stateText)}</span></div>
    <div class="progress">
      <span class="progress-bar"><span class="progress-fill" style="width:${Math.min(100, pkg.progress)}%"></span></span>
      ${pkg.progress.toFixed(0)}%
    </div>
  </article>`;
}

const LIBRARY_GROUPS = [
  { key: "anime", label: "Anime" },
  { key: "series", label: "Serien" },
  { key: "movie", label: "Filme" },
];
const LIBRARY_LIMIT = 30;

function rootLabel(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  return parts.slice(-2).join("/") || path;
}

function addedLabel(iso) {
  if (!iso) return "kein Datum";
  const then = new Date(iso);
  const days = (Date.now() - then.getTime()) / 86400000;
  if (days < 1) return relTime(iso);
  if (days < 14) {
    const whole = Math.round(days);
    return whole === 1 ? "vor 1 Tag" : `vor ${whole} Tagen`;
  }
  return then.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
}

function sortLibrary(items) {
  const byName = (a, b) => a.title.localeCompare(b.title, "de", { sensitivity: "base" });
  if (state.librarySort === "name") return [...items].sort(byName);
  return [...items].sort((a, b) => {
    if (!a.last_added && !b.last_added) return byName(a, b);
    if (!a.last_added) return 1;
    if (!b.last_added) return -1;
    return b.last_added.localeCompare(a.last_added);
  });
}

function libraryRow(item) {
  const facts = [
    rootLabel(item.root),
    (item.seasons || []).length ? plural(item.seasons.length, "Staffel", "Staffeln") : "",
    item.file_count ? plural(item.file_count, "Datei", "Dateien") : "",
  ].filter(Boolean);
  return `
    <div class="list-item" data-title="${esc((item.title + " " + item.folder_name).toLowerCase())}">
      <span>
        ${esc(item.folder_name)}
        <div class="row-sub">${esc(facts.join(" / "))}</div>
      </span>
      <span class="row-sub" title="${esc(item.last_added || "")}">${esc(addedLabel(item.last_added))}</span>
    </div>`;
}

async function renderLibrary(root) {
  const payload = await api("/api/library?limit=2000");
  const items = payload.items;
  const indexing = state.status && state.status.library_index && state.status.library_index.running;
  const groups = LIBRARY_GROUPS
    .map((group) => ({ ...group, items: sortLibrary(items.filter((item) => item.media_type === group.key)) }))
    .filter((group) => group.items.length);

  root.innerHTML = `
    <section class="block">
      <div class="block-head">
        <div>
          <h2>${plural(items.length, "Ordner indexiert", "Ordner indexiert")}</h2>
          <p>Ein Titel, der hier steht, bekommt neue Folgen genau in diesen Ordner.</p>
        </div>
        <div class="row-actions"><button class="btn" data-action="reindex">Neu einlesen</button></div>
      </div>
      ${indexing ? '<div class="notice" data-tone="warn">Die Bibliothek wird gerade eingelesen. Die Liste ist noch nicht vollständig.</div>' : ""}
      <div class="toolbar">
        <div class="field">
          <label for="libSearch">Suche</label>
          <input type="search" id="libSearch" placeholder="Titel filtern" autocomplete="off">
        </div>
        <div class="field">
          <span class="field-label" id="sortLabel">Sortierung</span>
          <div class="segmented" role="group" aria-labelledby="sortLabel">
            <button type="button" class="segment${state.librarySort === "recent" ? " is-active" : ""}" data-action="lib-sort" data-sort="recent">Zuletzt ergänzt</button>
            <button type="button" class="segment${state.librarySort === "name" ? " is-active" : ""}" data-action="lib-sort" data-sort="name">Name</button>
          </div>
        </div>
      </div>
    </section>

    ${groups.map((group) => `
      <section class="block group" data-group="${group.key}">
        <details${state.libraryOpen[group.key] === false ? "" : " open"} data-group="${group.key}">
          <summary class="group-head">
            <span class="group-caret" aria-hidden="true"></span>
            <span class="group-title">${esc(group.label)}</span>
            <span class="group-meta">${plural(group.items.length, "Ordner", "Ordner")}</span>
          </summary>
          <div class="list">${group.items.map(libraryRow).join("")}</div>
          <div class="more">
            <button type="button" class="btn btn-small" data-action="lib-more" data-group="${group.key}"></button>
          </div>
        </details>
      </section>`).join("") || emptyState("Nichts indexiert", "Prüfe die Zielpfade in den Einstellungen und lies die Bibliothek neu ein.")}
  `;

  qsa("details[data-group]", root).forEach((details) => {
    details.addEventListener("toggle", () => {
      state.libraryOpen[details.dataset.group] = details.open;
      localStorage.setItem("es-library-open", JSON.stringify(state.libraryOpen));
    });
  });

  const search = qs("#libSearch", root);
  search.addEventListener("input", () => applyLibraryFilter(root));
  applyLibraryFilter(root);
  bindJobRows(root);
}

function applyLibraryFilter(root) {
  const search = qs("#libSearch", root);
  const needle = (search ? search.value : "").toLowerCase().trim();

  qsa("section[data-group]", root).forEach((section) => {
    const key = section.dataset.group;
    const showAll = Boolean(state.libraryShowAll[key]) || Boolean(needle);
    const rows = qsa(".list-item", section);
    let hits = 0;
    rows.forEach((row) => {
      const hit = !needle || row.dataset.title.includes(needle);
      if (hit) hits += 1;
      row.hidden = !hit || (!showAll && hits > LIBRARY_LIMIT);
    });

    section.hidden = needle ? hits === 0 : false;
    const details = qs("details", section);
    if (needle && hits) details.open = true;

    const more = qs(".more", section);
    const button = qs("button", more);
    const hidden = Math.max(0, hits - LIBRARY_LIMIT);
    if (needle || (!hidden && !state.libraryShowAll[key])) {
      more.hidden = true;
    } else {
      more.hidden = false;
      button.textContent = state.libraryShowAll[key]
        ? `Nur die letzten ${LIBRARY_LIMIT} zeigen`
        : `Alle ${hits} anzeigen`;
    }
  });
}

async function renderRules(root) {
  const { rules } = await api("/api/rules");
  root.innerHTML = `
    <section class="block">
      <div class="block-head"><div><h2>${plural(rules.length, "Regel", "Regeln")}</h2><p>Entstehen aus manuellen Zuordnungen und greifen bei ähnlichen Dateinamen.</p></div></div>
      ${rules.length ? `<div class="list">${rules.map((rule) => `
        <div class="list-item">
          <span>
            <strong>${esc(rule.title)}</strong>${rule.year ? ` (${rule.year})` : ""}
            <div class="row-sub">${esc(rule.match_kind)}: <span class="path">${esc(rule.pattern)}</span></div>
            ${rule.target_dir ? `<div class="row-sub path">${esc(rule.target_dir)}</div>` : ""}
          </span>
          <span class="row-actions">
            <span class="chip">${esc(MEDIA_LABEL[rule.media_type] || rule.media_type)}</span>
            <span class="row-sub">${rule.hits}x</span>
            <button class="btn btn-small btn-danger" data-action="delete-rule" data-rule="${rule.id}">Löschen</button>
          </span>
        </div>`).join("")}</div>` : emptyState("Noch keine Regeln", "Beim Übernehmen einer Entscheidung lässt sich eine Regel mitspeichern.")}
    </section>`;
  bindJobRows(root);
}

async function renderLog(root) {
  const { events } = await api("/api/events?limit=250");
  root.innerHTML = `<section class="block">
    <div class="block-head"><div><h2>Protokoll</h2><p>${plural(events.length, "Eintrag", "Einträge")}</p></div></div>
    <div class="list">${events.map(logLine).join("") || emptyState("Leer", "Noch keine Ereignisse.")}</div>
  </section>`;
}

const SETTING_GROUPS = [
  {
    title: "Pfade",
    hint: "Der Downloadordner wird überwacht, alle gefüllten Zielpfade werden indexiert. Ein vorhandener Ordner gewinnt immer gegen den Standardpfad.",
    keys: [
      "download_dir", "anime_path_1", "anime_path_2", "series_path", "series_path_2",
      "movies_path", "movies_path_2", "default_anime_path", "default_series_path", "default_movie_path",
    ],
  },
  {
    title: "Metadaten",
    hint: "TMDb deckt Filme und Serien ab, AniList und MyAnimeList zusätzlich Anime. Fällt eine Anime-Quelle aus, erkennt TMDb Anime an Sprache und Herkunftsland.",
    keys: ["tmdb_api_key", "tmdb_language", "use_anilist", "use_jikan", "prefer_anime", "metadata_cache_hours"],
  },
  {
    title: "Verhalten",
    hint: "Solange Dry Run aktiv ist, wird nur geplant. Das Prüfintervall bestimmt auch, wie lange eine fertige Datei auf ihre Einsortierung wartet.",
    keys: [
      "dry_run", "auto_threshold", "scan_interval_seconds", "stability_checks", "min_video_size_mb",
      "verify_mode", "free_space_margin_mb", "move_subtitles", "delete_empty_source_dirs",
      "event_retention", "job_retention_days",
    ],
  },
  {
    title: "Dateinamen",
    hint: "Platzhalter: {title} {year} {season:02d} {episode:02d} {episode_end:02d}",
    keys: ["episode_template", "episode_range_template", "movie_template", "season_folder_template", "specials_folder"],
  },
  {
    title: "Filter",
    hint: "Kommagetrennt. Treffer werden ignoriert.",
    keys: ["video_extensions", "subtitle_extensions", "ignored_terms", "ignored_extensions"],
  },
  {
    title: "JDownloader",
    hint: "My JDownloader Zugangsdaten. Ohne Verbindung läuft nur die Ordnerwache.",
    keys: ["jd_enabled", "jd_email", "jd_password", "jd_device", "jd_path_prefix", "watch_folder_fallback"],
  },
];

const SETTING_LABEL = {
  download_dir: "Downloadordner",
  anime_path_1: "Anime-Pfad 1",
  anime_path_2: "Anime-Pfad 2",
  series_path: "Serienpfad 1",
  series_path_2: "Serienpfad 2",
  movies_path: "Filmpfad 1",
  movies_path_2: "Filmpfad 2 (optional)",
  default_anime_path: "Standardpfad für neue Anime",
  default_series_path: "Standardpfad für neue Serien",
  default_movie_path: "Standardpfad für neue Filme",
  tmdb_api_key: "TMDb API-Schlüssel",
  tmdb_language: "TMDb Sprache",
  use_anilist: "AniList verwenden",
  use_jikan: "MyAnimeList als Ersatz verwenden",
  prefer_anime: "Im Zweifel Anime annehmen",
  metadata_cache_hours: "Cache in Stunden",
  dry_run: "Dry Run",
  auto_threshold: "Automatische Erkennungsschwelle in Prozent",
  scan_interval_seconds: "Prüfintervall in Sekunden",
  stability_checks: "Unveränderte Durchläufe vor der Analyse",
  min_video_size_mb: "Mindestgröße Video in MB",
  verify_mode: "Prüfung beim Kopieren",
  free_space_margin_mb: "Reserve freier Speicher in MB",
  move_subtitles: "Untertitel mitnehmen",
  delete_empty_source_dirs: "Leere Quellordner entfernen",
  event_retention: "Protokolleinträge aufbewahren",
  job_retention_days: "Erledigte Jobs aufbewahren in Tagen",
  episode_template: "Episodenname",
  episode_range_template: "Mehrfachfolge",
  movie_template: "Filmname",
  season_folder_template: "Staffelordner",
  specials_folder: "Ordner für Specials",
  video_extensions: "Videoendungen",
  subtitle_extensions: "Untertitelendungen",
  ignored_terms: "Ignorierte Begriffe",
  ignored_extensions: "Ignorierte Endungen",
  jd_enabled: "JDownloader-Anbindung aktiv",
  jd_email: "My JDownloader E-Mail",
  jd_password: "My JDownloader Passwort",
  jd_device: "Gerätename",
  jd_path_prefix: "JD-Pfad im Container",
  watch_folder_fallback: "Ordnerwache als Rückfallebene",
};

function settingField(key, value) {
  const id = `set-${key}`;
  const label = SETTING_LABEL[key] || key;
  if (typeof value === "boolean") {
    return `<div class="field">
      <label class="switch" for="${id}">
        <input type="checkbox" id="${id}" name="${key}"${value ? " checked" : ""}>
        <span class="switch-track" aria-hidden="true"><span class="switch-knob"></span></span>
        <span class="switch-label">${esc(label)}</span>
      </label>
    </div>`;
  }
  if (Array.isArray(value)) {
    return `<div class="field" data-span="wide">
      <label for="${id}">${esc(label)}</label>
      <textarea id="${id}" name="${key}" data-type="list">${esc(value.join(", "))}</textarea>
    </div>`;
  }
  if (key === "verify_mode") {
    return `<div class="field"><label for="${id}">${esc(label)}</label>
      <select id="${id}" name="${key}">
        <option value="size"${value === "size" ? " selected" : ""}>Dateigröße</option>
        <option value="sha256"${value === "sha256" ? " selected" : ""}>SHA-256 Prüfsumme</option>
      </select></div>`;
  }
  const type = typeof value === "number" ? "number" : key === "jd_password" ? "password" : "text";
  return `<div class="field">
    <label for="${id}">${esc(label)}</label>
    <input type="${type}" id="${id}" name="${key}" value="${esc(value)}" data-type="${typeof value === "number" ? "number" : "text"}">
  </div>`;
}

async function renderSettings(root) {
  const payload = await api("/api/settings");
  state.settings = payload.settings;
  root.innerHTML = `
    <form id="settingsForm">
      ${SETTING_GROUPS.map((group) => `
        <section class="block">
          <div class="block-head"><div><h2>${esc(group.title)}</h2><p>${esc(group.hint)}</p></div></div>
          <div class="form">${group.keys.map((key) => settingField(key, state.settings[key])).join("")}</div>
        </section>`).join("")}
      <section class="block">
        <div class="row-actions" style="justify-content: flex-start;">
          <button class="btn btn-primary" type="submit">Einstellungen speichern</button>
          <button class="btn" type="button" data-action="reindex">Bibliothek neu einlesen</button>
          <button class="btn" type="button" data-action="jd-connect">JDownloader verbinden</button>
        </div>
      </section>
    </form>`;

  const form = qs("#settingsForm", root);
  form.addEventListener("input", () => { state.paused = true; });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = {};
    qsa("input, select, textarea", form).forEach((input) => {
      if (input.type === "checkbox") values[input.name] = input.checked;
      else if (input.dataset.type === "list") values[input.name] = input.value.split(",").map((part) => part.trim()).filter(Boolean);
      else if (input.dataset.type === "number" || input.type === "number") values[input.name] = Number(input.value);
      else values[input.name] = input.value;
    });
    const button = qs("button[type=submit]", form);
    button.disabled = true;
    button.classList.add("is-busy");
    try {
      await api("/api/settings", { method: "PUT", body: { values } });
      toast("Einstellungen gespeichert", "good");
      state.paused = false;
      await refreshStatus();
    } catch (error) {
      toast(`Speichern fehlgeschlagen: ${error.message}`, "bad");
    } finally {
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  });
  bindJobRows(root);
}

/* ------------------------------------------------------------------ actions */

function onKeydown(event) {
  if (event.key !== "Enter" && event.key !== " ") return;
  const target = event.target.closest('[role="button"][data-action]');
  if (!target) return;
  event.preventDefault();
  target.click();
}

function bindJobRows(root) {
  root.addEventListener("click", onClick);
  root.addEventListener("keydown", onKeydown);
  qsa("form[data-action='override']", root).forEach((form) => {
    form.addEventListener("submit", onOverrideSubmit);
    form.addEventListener("input", () => { state.paused = true; });
  });
}

async function onClick(event) {
  const trigger = event.target.closest("[data-action]");
  if (!trigger) return;
  const action = trigger.dataset.action;

  if (action === "toggle") {
    const id = Number(trigger.dataset.job);
    state.openJob = state.openJob === id ? null : id;
    state.paused = state.openJob !== null;
    await render();
    return;
  }

  if (action === "candidate") {
    await sendDecision(trigger.dataset.job, "select_candidate", {
      source: trigger.dataset.source,
      external_id: Number(trigger.dataset.id),
    }, trigger);
    return;
  }

  if (action === "decision") {
    await sendDecision(trigger.dataset.job, trigger.dataset.decision, {}, trigger);
    return;
  }

  if (action === "dup-gruppe") {
    const ids = (trigger.dataset.ids || "").split(",").filter(Boolean).map(Number);
    const frage = trigger.dataset.frage;
    if (frage && trigger.dataset.scharf !== "ja") {
      // Ersetzen ist nicht umkehrbar, deshalb ein zweiter bewusster Klick.
      const beschriftung = trigger.textContent;
      trigger.dataset.scharf = "ja";
      trigger.textContent = frage;
      setTimeout(() => {
        if (trigger.isConnected) { trigger.dataset.scharf = ""; trigger.textContent = beschriftung; }
      }, 6000);
      return;
    }
    await run(trigger, async () => {
      const stapel = await api("/api/jobs/bulk", {
        method: "POST",
        body: { action: trigger.dataset.decision, ids, payload: {} },
      });
      // Der Server arbeitet die Liste im Hintergrund ab. Die Leiste oben zeigt
      // ab sofort, welche Folge gerade dran ist.
      state.activity = stapel;
      state.transfers = [];
      renderActivity();
      syncActivityPolling();
      await refreshAndRender();
    });
    return;
  }

  if (action === "batch-cancel") {
    await run(trigger, async () => {
      await api(`/api/batches/${trigger.dataset.batch}/cancel`, { method: "POST" });
      toast("Wird nach der laufenden Datei beendet");
    });
    return;
  }

  if (action === "group-toggle") {
    const key = trigger.dataset.key;
    state.overviewOpen[key] = !state.overviewOpen[key];
    await render();
    return;
  }

  if (action === "lib-more") {
    const key = trigger.dataset.group;
    state.libraryShowAll[key] = !state.libraryShowAll[key];
    applyLibraryFilter(qs("#view"));
    return;
  }

  if (action === "lib-sort") {
    state.librarySort = trigger.dataset.sort;
    localStorage.setItem("es-library-sort", state.librarySort);
    await render();
    return;
  }

  if (action === "reindex") {
    await run(trigger, async () => {
      const result = await api("/api/library/reindex", { method: "POST" });
      const total = Object.values(result.roots).filter((value) => value >= 0).reduce((a, b) => a + b, 0);
      toast(`${total} Ordner eingelesen`, "good");
    });
    return;
  }

  if (action === "jd-connect") {
    await run(trigger, async () => {
      const result = await api("/api/jd/connect", { method: "POST" });
      toast(result.connected ? `Verbunden mit ${result.device}` : `Nicht verbunden: ${result.error}`, result.connected ? "good" : "bad");
      await refreshStatus();
    });
    return;
  }

  if (action === "delete-rule") {
    await run(trigger, async () => {
      await api(`/api/rules/${trigger.dataset.rule}`, { method: "DELETE" });
      toast("Regel gelöscht", "good");
      await render();
    });
  }
}

async function onOverrideSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const decision = (event.submitter && event.submitter.dataset.decision) || "override";
  const payload = {};
  qsa("input, select", form).forEach((input) => {
    if (input.type === "checkbox") { payload[input.name] = input.checked; return; }
    if (input.value === "") return;
    payload[input.name] = input.type === "number" ? Number(input.value) : input.value;
  });
  await sendDecision(form.dataset.job, decision, payload, event.submitter);
}

async function sendDecision(jobId, action, payload, trigger) {
  await run(trigger, async () => {
    const job = await api(`/api/jobs/${jobId}/decision`, { method: "POST", body: { action, payload } });
    const message = {
      approve: `Übernommen: ${job.status === "done" ? "verschoben" : job.status}`,
      skip: "Datei verworfen",
      retry: "Wird neu analysiert",
      duplicate_replace: "Vorhandene Datei wird ersetzt",
      duplicate_keep_both: "Zusätzlich behalten",
      duplicate_discard: "Neue Datei verworfen",
      select_candidate: `Titel gesetzt: ${job.title || ""}`,
      override: "Ziel aktualisiert",
      defer: "Später entscheiden",
    }[action] || "Erledigt";
    toast(message, job.status === "failed" ? "bad" : "good");
    if (job.status === "done" || job.status === "skipped") state.openJob = null;
    state.paused = state.openJob !== null;
    await refreshStatus();
    await render();
  });
}

async function run(trigger, task) {
  if (trigger) { trigger.disabled = true; trigger.classList.add("is-busy"); }
  try {
    await task();
  } catch (error) {
    toast(error.message, "bad");
  } finally {
    if (trigger) { trigger.disabled = false; trigger.classList.remove("is-busy"); }
  }
}

/* ------------------------------------------------------------------ shell */

// Views that change while you watch. Library, rules and settings would only
// wipe what you typed.
const LIVE_VIEWS = new Set(["overview", "decisions", "duplicates", "jdownloader", "log"]);

const RENDERERS = {
  overview: renderOverview,
  decisions: renderDecisions,
  duplicates: renderDuplicates,
  jdownloader: renderJDownloader,
  library: renderLibrary,
  rules: renderRules,
  log: renderLog,
  settings: renderSettings,
};

async function render({ entering = false } = {}) {
  const root = qs("#view");
  if (entering) {
    root.classList.remove("is-entering");
    void root.offsetWidth; // restart the animation
    root.classList.add("is-entering");
  }
  const meta = VIEWS[state.view];
  qs("#viewTitle").textContent = meta.title;
  qs("#viewHint").textContent = meta.hint;
  if (!root.dataset.loaded) root.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  try {
    await RENDERERS[state.view](root);
    root.dataset.loaded = "1";
  } catch (error) {
    root.innerHTML = `<div class="notice" data-tone="bad">Ansicht konnte nicht geladen werden: ${esc(error.message)}</div>`;
  }
}

function setView(view) {
  state.view = view;
  state.openJob = null;
  state.paused = view === "settings";
  qsa(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === view));
  const root = qs("#view");
  delete root.dataset.loaded;
  location.hash = view;
  render({ entering: true });
}

function watchScroll() {
  const sentinel = qs("#scrollSentinel");
  const topbar = qs(".topbar");
  if (!sentinel || !("IntersectionObserver" in window)) return;
  new IntersectionObserver(
    ([entry]) => topbar.classList.toggle("is-stuck", !entry.isIntersecting),
    { threshold: 1 },
  ).observe(sentinel);
}

function boot() {
  watchScroll();
  qs("#nav").addEventListener("click", (event) => {
    const button = event.target.closest(".nav-item");
    if (button) setView(button.dataset.view);
  });

  qs("#scanNow").addEventListener("click", async (event) => {
    await run(event.currentTarget, async () => {
      const result = await api("/api/scan", { method: "POST" });
      toast(`Durchlauf beendet: ${result.discovered || 0} neu, ${result.analyzed || 0} geprüft, ${result.moved || 0} verarbeitet`, "good");
      await refreshStatus();
      await render();
    });
  });

  qs("#dryRunToggle").addEventListener("change", async (event) => {
    const checked = event.currentTarget.checked;
    try {
      await api("/api/settings", { method: "PUT", body: { values: { dry_run: checked } } });
      toast(checked ? "Dry Run aktiv, es wird nichts verschoben" : "Verschieben aktiviert", checked ? "neutral" : "good");
      await refreshStatus();
      await render();
    } catch (error) {
      event.currentTarget.checked = !checked;
      toast(`Umschalten fehlgeschlagen: ${error.message}`, "bad");
    }
  });

  window.addEventListener("hashchange", () => {
    const next = location.hash.replace("#", "");
    if (VIEWS[next] && next !== state.view) setView(next);
  });

  const initial = location.hash.replace("#", "");
  if (VIEWS[initial]) state.view = initial;
  qsa(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === state.view));

  refreshStatus().then(() => render({ entering: true }));
  setInterval(() => { if (!document.hidden) tickScanTimer(); }, 1000);
  connectStream();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshAndRender();
  });
}

/* --------------------------------------------------------- Aktualisierung */

async function refreshAndRender() {
  await refreshStatus();
  if (!state.paused && LIVE_VIEWS.has(state.view)) await render();
}

// Der Server meldet sich, wenn sich wirklich etwas geändert hat. Die Umfrage im
// Sekundentakt entfällt damit, sie bleibt nur als Rückfallebene bestehen.
function connectStream() {
  if (!("EventSource" in window)) { startFallbackPolling(15000); return; }

  const source = new EventSource("/api/stream");
  let watchdog = null;
  let beendet = false;

  // Läuft auch, wenn die Verbindung nie zustande kam. Sonst bliebe das
  // Dashboard ohne Strom und ohne Umfrage einfach stehen.
  const onDrop = () => {
    if (beendet) return;
    beendet = true;
    clearTimeout(watchdog);
    try { source.close(); } catch (error) { /* schon zu */ }
    if (state.stream === source) state.stream = null;
    startFallbackPolling(10000);
    setTimeout(connectStream, 20000);
  };

  const arm = () => {
    clearTimeout(watchdog);
    watchdog = setTimeout(onDrop, 45000);  // 45 s ohne ein Lebenszeichen
  };

  arm();
  source.onopen = () => { state.stream = source; stopFallbackPolling(); arm(); };
  source.onmessage = arm;
  source.onerror = onDrop;
  source.addEventListener("change", async (event) => {
    arm();
    try {
      const payload = JSON.parse(event.data || "{}");
      if (payload.version && payload.version === state.version) return;
      state.version = payload.version;
    } catch (error) { /* dann eben ohne Versionsvergleich */ }
    await refreshAndRender();
  });
}

function startFallbackPolling(interval) {
  if (state.timer) return;
  state.timer = setInterval(() => { if (!document.hidden) refreshAndRender(); }, interval);
}

function stopFallbackPolling() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

boot();
