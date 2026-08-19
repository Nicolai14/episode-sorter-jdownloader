/* Episode Sorter dashboard. Vanilla ES modules, no build step. */

const state = {
  view: "overview",
  status: null,
  openJob: null,
  paused: false,
  librarySort: localStorage.getItem("es-library-sort") || "recent",
  libraryOpen: JSON.parse(localStorage.getItem("es-library-open") || "{}"),
  libraryShowAll: {},
  settings: null,
  timer: null,
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
  return `<span class="meter"><span class="meter-bar"><span class="meter-fill" data-level="${level}" style="width:${Math.max(4, value)}%"></span></span>${value}%</span>`;
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
    qs("#railHealth").textContent = "Backend nicht erreichbar";
    return;
  }
  const counts = state.status.counts || {};
  const openCount = (counts.waiting || 0) + (counts.analyzing || 0) + (counts.ready || 0) + (counts.planned || 0);
  setCount("open", openCount);
  setCount("review", counts.review || 0, true);
  setCount("duplicate", counts.duplicate || 0, true);

  const toggle = qs("#dryRunToggle");
  if (document.activeElement !== toggle) toggle.checked = Boolean(state.status.dry_run);

  const jd = state.status.jd || {};
  const parts = [];
  parts.push(state.status.dry_run ? "Dry Run aktiv" : "Verschieben aktiv");
  parts.push(jd.enabled ? (jd.connected ? `JD verbunden (${jd.device || "Gerät"})` : "JD getrennt") : "JD aus, nur Ordnerwache");
  qs("#railHealth").textContent = parts.join(" / ");
}

function setCount(key, value, alert = false) {
  const node = qs(`[data-count="${key}"]`);
  if (!node) return;
  node.textContent = value > 0 ? String(value) : "";
  if (alert && value > 0) node.dataset.tone = "alert"; else delete node.dataset.tone;
}

/* ------------------------------------------------------------------ views */

async function renderOverview(root) {
  const status = state.status || {};
  const counts = status.counts || {};
  const [jobsPayload, eventsPayload] = await Promise.all([
    api("/api/jobs?limit=40"),
    api("/api/events?limit=12"),
  ]);
  const jobs = jobsPayload.jobs;

  const stats = [
    { label: "wartet auf Entpacken", value: (counts.waiting || 0) + (counts.analyzing || 0) },
    { label: "Entscheidung nötig", value: counts.review || 0, tone: (counts.review || 0) > 0 ? "alert" : null },
    { label: "Dubletten", value: counts.duplicate || 0, tone: (counts.duplicate || 0) > 0 ? "alert" : null },
    { label: "geplant", value: counts.planned || 0 },
    { label: "einsortiert", value: counts.done || 0, tone: "good" },
    { label: "Fehler", value: counts.failed || 0, tone: (counts.failed || 0) > 0 ? "bad" : null },
  ];

  const warnings = [];
  if (!status.download_dir_ok) warnings.push(`Der Downloadordner <strong>${esc(status.download_dir)}</strong> ist nicht erreichbar.`);
  (status.library_roots || []).forEach((root_) => {
    if (!root_.exists) warnings.push(`Zielpfad <strong>${esc(root_.path)}</strong> existiert nicht.`);
    else if (!root_.writable) warnings.push(`Zielpfad <strong>${esc(root_.path)}</strong> ist nicht beschreibbar.`);
  });
  if (!status.tmdb_configured) warnings.push("Kein TMDb-Schlüssel hinterlegt. Filme und Serien werden nur über AniList oder gar nicht geprüft.");
  if (status.dry_run) warnings.push("Dry Run ist aktiv. Es wird nichts verschoben, nur geplant.");

  root.innerHTML = `
    <section class="block">
      <div class="statbar">
        ${stats.map((stat) => `
          <div class="stat"${stat.tone ? ` data-tone="${stat.tone}"` : ""}>
            <div class="stat-value">${stat.value}</div>
            <div class="stat-label">${esc(stat.label)}</div>
          </div>`).join("")}
      </div>
      ${warnings.map((text) => `<div class="notice" data-tone="warn">${text}</div>`).join("")}
    </section>

    <section class="block">
      <div class="block-head">
        <div><h2>Letzte Dateien</h2><p>${plural(jobs.length, "Eintrag", "Einträge")}, neueste zuerst</p></div>
      </div>
      ${jobs.length ? `<div class="rows">${jobs.map(jobRow).join("")}</div>` : emptyState("Noch nichts gesehen", "Sobald JDownloader eine Datei fertig entpackt hat, taucht sie hier auf.")}
    </section>

    <section class="block">
      <div class="block-head"><div><h2>Protokoll</h2><p>letzte Ereignisse</p></div></div>
      <div class="list">${eventsPayload.events.map(logLine).join("") || emptyState("Leer", "Noch keine Ereignisse.")}</div>
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
      <div class="row-meter">${meter(job.confidence)}</div>
      <div class="row-actions">
        <button class="btn btn-small" data-action="toggle" data-job="${job.id}">${state.openJob === job.id ? "Schließen" : "Details"}</button>
      </div>
      ${state.openJob === job.id ? jobDetail(job) : ""}
    </article>`;
}

const TERMINAL = new Set(["done", "skipped"]);

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

async function renderDuplicates(root) {
  const { jobs } = await api("/api/jobs?status=duplicate&limit=200");
  if (!jobs.length) {
    root.innerHTML = emptyState("Keine Dubletten", "Es liegt keine Datei vor, die es in der Bibliothek schon gibt.");
    return;
  }
  root.innerHTML = `<section class="block">
    <div class="block-head"><div><h2>${plural(jobs.length, "Dublette", "Dubletten")}</h2><p>Nichts wird automatisch überschrieben.</p></div></div>
    ${jobs.map(duplicateCard).join("")}
  </section>`;
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

function duplicateCard(job) {
  const info = job.duplicate_info || {};
  return `<article class="block" data-job="${job.id}">
    <div class="block-head">
      <div>
        <h2>${esc(job.title || job.parsed_title || job.filename)}</h2>
        <p class="path">${esc(job.duplicate_of || "")}</p>
      </div>
    </div>
    <div class="compare">
      <div class="compare-col" data-role="existing"><h3>In der Bibliothek</h3>${mediaFacts(info.existing)}</div>
      <div class="compare-col" data-role="incoming"><h3>Neu aus dem Download</h3>${mediaFacts(info.incoming)}</div>
    </div>
    <div class="row-actions" style="justify-content: flex-start;">
      <button class="btn" data-action="decision" data-decision="duplicate_discard" data-job="${job.id}">Neue Datei verwerfen</button>
      <button class="btn" data-action="decision" data-decision="duplicate_keep_both" data-job="${job.id}">Zusätzlich behalten</button>
      <button class="btn btn-danger" data-action="decision" data-decision="duplicate_replace" data-job="${job.id}">Vorhandene ersetzen</button>
      <button class="btn" data-action="decision" data-decision="defer" data-job="${job.id}">Später entscheiden</button>
    </div>
  </article>`;
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
    hint: "TMDb deckt Filme und Serien ab, AniList zusätzlich Anime.",
    keys: ["tmdb_api_key", "tmdb_language", "use_anilist", "metadata_cache_hours"],
  },
  {
    title: "Verhalten",
    hint: "Solange Dry Run aktiv ist, wird nur geplant.",
    keys: ["dry_run", "auto_threshold", "scan_interval_seconds", "stability_checks", "min_video_size_mb", "verify_mode", "free_space_margin_mb", "move_subtitles", "delete_empty_source_dirs"],
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
  metadata_cache_hours: "Cache in Stunden",
  dry_run: "Dry Run",
  auto_threshold: "Automatische Erkennungsschwelle in Prozent",
  scan_interval_seconds: "Prüfintervall in Sekunden",
  stability_checks: "Stabile Prüfungen vor der Analyse",
  min_video_size_mb: "Mindestgröße Video in MB",
  verify_mode: "Prüfung beim Kopieren",
  free_space_margin_mb: "Reserve freier Speicher in MB",
  move_subtitles: "Untertitel mitnehmen",
  delete_empty_source_dirs: "Leere Quellordner entfernen",
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

function bindJobRows(root) {
  root.addEventListener("click", onClick);
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
  state.timer = setInterval(async () => {
    if (document.hidden) return;
    await refreshStatus();
    if (!state.paused && LIVE_VIEWS.has(state.view)) await render();
  }, 7000);
}

boot();
