"use strict";

const state = {
  data: null,
  selectedId: localStorage.getItem("blockops-selected") || null,
  tab: localStorage.getItem("blockops-tab") || "console",
  logSignature: "",
  clearBefore: 0,
  lastJobId: null,
  lastJobStatus: null,
  players: {},
  config: { roots: [], root: null, path: "", entries: [], file: null, dirty: false },
  restoreBackup: null,
  settingsDirty: false,
  backupPolicyDirty: false,
  performance: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...(options.body && typeof options.body === "string" ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
  });
  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await response.json() : null;
  if (!response.ok) throw new Error(data?.error || `Request failed (${response.status})`);
  return data;
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = String(value ?? "");
  return element.innerHTML;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 1) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function formatDate(seconds) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(seconds * 1000));
}

function formatMetric(value, suffix = "", digits = 1) {
  return Number.isFinite(value) ? `${Number(value).toFixed(digits)}${suffix}` : "—";
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  $("#toast-stack").append(node);
  setTimeout(() => node.remove(), 4500);
}

function selectedProfile() {
  const profiles = state.data?.profiles || [];
  return profiles.find((profile) => profile.id === state.selectedId) || profiles[0] || null;
}

function selectProfile(profileId) {
  if (state.config.dirty && !confirm("Discard the unsaved configuration edits?")) return;
  state.selectedId = profileId;
  state.settingsDirty = false;
  state.backupPolicyDirty = false;
  localStorage.setItem("blockops-selected", profileId);
  state.logSignature = "";
  render();
  loadLog();
  if (state.tab === "mods") loadMods();
  if (state.tab === "backups") loadBackups();
  if (state.tab === "players") loadPlayers();
  state.performance = null;
  if (state.tab === "performance") loadPerformance(true);
  state.config = { roots: [], root: null, path: "", entries: [], file: null, dirty: false };
  if (state.tab === "files") loadConfigRoots();
  $("#sidebar").classList.remove("open");
}

function statusDescription(profile) {
  if (profile.status === "online") return `Online on port ${profile.port}. Commands and players are live.`;
  if (profile.status === "starting") return `The server runner is preparing port ${profile.port}. Watch the console below.`;
  return `Ready to launch Minecraft ${profile.minecraftVersion} with ${profile.loader}.`;
}

function renderServerList(profiles) {
  $("#profile-count").textContent = profiles.length;
  $("#server-list").innerHTML = profiles.map((profile) => `
    <button class="server-card ${profile.id === state.selectedId ? "selected" : ""}" data-profile="${escapeHtml(profile.id)}">
      <span class="server-thumb">${escapeHtml(profile.name.slice(0, 1).toUpperCase())}</span>
      <span><strong>${escapeHtml(profile.name)}</strong><small>${escapeHtml(profile.minecraftVersion)} · ${escapeHtml(profile.loader)}</small></span>
      <i class="mini-state ${profile.status === "online" ? "online" : ""}" title="${escapeHtml(profile.status)}"></i>
    </button>
  `).join("");
  $$("[data-profile]").forEach((button) => button.addEventListener("click", () => selectProfile(button.dataset.profile)));
}

function setFormValue(form, name, value) {
  const field = form.elements.namedItem(name);
  if (!field) return;
  if (field.type === "checkbox") field.checked = Boolean(value);
  else field.value = value ?? "";
}

function populateSettings(profile) {
  if (state.settingsDirty) return;
  const form = $("#settings-form");
  setFormValue(form, "name", profile.name);
  setFormValue(form, "motd", profile.properties.motd);
  setFormValue(form, "minimumRam", profile.minimumRam);
  setFormValue(form, "maximumRam", profile.maximumRam);
  setFormValue(form, "jvmArguments", profile.jvmArguments.join(" "));
  for (const key of ["gamemode", "difficulty", "maxPlayers", "whiteList", "hardcore", "onlineMode", "pvp"]) {
    setFormValue(form, key, profile.properties[key]);
  }
}

function populateBackupSettings(profile) {
  if (state.backupPolicyDirty) return;
  const form = $("#backup-settings-form");
  const settings = profile.backupSettings || {};
  for (const key of ["enabled", "onlyWhenEmpty", "backupOnStop", "intervalMinutes", "retention", "compressionLevel"]) setFormValue(form, key, settings[key]);
}

function render() {
  if (!state.data) return;
  const profiles = state.data.profiles;
  if (!profiles.some((profile) => profile.id === state.selectedId)) state.selectedId = profiles[0]?.id || null;
  renderServerList(profiles);
  $("#playit-status").textContent = state.data.playitRunning ? "ONLINE" : "OFFLINE";
  $("#playit-status").classList.toggle("online", state.data.playitRunning);
  $("#global-status .status-light").classList.toggle("online", state.data.minecraftRunning);
  $("#global-status-text").textContent = state.data.minecraftRunning ? "A WORLD IS ONLINE" : "ALL WORLDS ARE SAFE";
  const profile = selectedProfile();
  $("#empty-state").hidden = Boolean(profile);
  $("#dashboard-content").hidden = !profile;
  if (!profile) return;
  const running = profile.status === "online" || profile.status === "starting";
  $("#selected-type").textContent = `MINECRAFT ${profile.minecraftVersion} · ${profile.loader.toUpperCase()} SERVER`;
  $("#selected-name").textContent = profile.name;
  $("#selected-description").textContent = statusDescription(profile);
  $("#selected-state").className = `server-state ${profile.status}`;
  $("#selected-state").innerHTML = `<i></i> ${profile.status.toUpperCase()}`;
  $("#stat-mods").textContent = `${profile.modsCount} enabled`;
  $("#stat-backups").textContent = `${profile.backupsCount} saved`;
  const players = state.players[profile.id];
  $("#stat-players").textContent = players ? `${players.online}/${players.maximum} online` : (running ? "View list" : "0 online");
  $("#start-button").disabled = Boolean(state.data.activeProfileId) || state.data.job.status === "running";
  $("#stop-button").disabled = !running || state.data.job.status === "running";
  $("#command-input").disabled = !running;
  $("#command-form button").disabled = !running;
  $("#console-light").classList.toggle("online", running);
  populateSettings(profile);
  populateBackupSettings(profile);
  renderJob(state.data.job);
}

function renderJob(current) {
  const drawer = $("#job-drawer");
  if (!current?.id || current.status === "idle") return;
  drawer.hidden = false;
  drawer.classList.toggle("done", current.status === "succeeded" || current.status === "attention");
  drawer.classList.toggle("failed", current.status === "failed");
  $("#job-title").textContent = `${current.kind || "SERVER"} · ${current.status}`.toUpperCase();
  $("#job-message").textContent = current.message || "Working…";
  $("#job-log").textContent = (current.lines || []).join("\n") || "Preparing operation…";
  $("#job-log").scrollTop = $("#job-log").scrollHeight;
  const claim = $("#claim-link");
  claim.hidden = !current.claimUrl;
  if (current.claimUrl) claim.href = current.claimUrl;
  if (current.id === state.lastJobId && current.status !== state.lastJobStatus && ["succeeded", "failed", "attention"].includes(current.status)) {
    toast(current.message, current.status === "failed" ? "error" : "success");
    if (current.kind === "create" && current.status === "succeeded") closeCreateModal();
    if (current.kind === "restore backup" && current.status === "succeeded") loadBackups();
    if (current.kind === "start" && current.status === "failed" && /playit is not installed/i.test(current.message || "")) openSetupGuide();
  }
  state.lastJobId = current.id;
  state.lastJobStatus = current.status;
}

async function refreshState(silent = false) {
  try {
    state.data = await api("/api/state");
    render();
    if (!state.data.profiles.length && !sessionStorage.getItem("blockops-setup-shown")) {
      sessionStorage.setItem("blockops-setup-shown", "1");
      openSetupGuide();
    }
  } catch (error) {
    if (!silent) toast(error.message, "error");
  }
}

async function openSetupGuide() {
  const modal = $("#setup-modal");
  modal.hidden = false;
  const list = $("#setup-steps");
  list.innerHTML = "<p>Checking this computer…</p>";
  try {
    const data = await api("/api/setup");
    list.innerHTML = data.steps.map((step, index) => `<article class="setup-step ${step.done ? "done" : ""}"><i>${step.done ? "✓" : index + 1}</i><div><strong>${escapeHtml(step.title)}</strong><span>${escapeHtml(step.done ? "Ready." : step.help)}</span></div></article>`).join("");
  } catch (error) {
    list.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
  }
}

function closeSetupGuide() { $("#setup-modal").hidden = true; }

function colorizeLog(line) {
  let html = escapeHtml(line);
  html = html.replace(/(\[\d{2}:\d{2}:\d{2}\])/g, '<span class="time">$1</span>');
  html = html.replace(/(WARN|WARNING)/gi, '<span class="warn">$1</span>');
  html = html.replace(/(ERROR|SEVERE|FATAL)/gi, '<span class="error">$1</span>');
  html = html.replace(/(INFO)/g, '<span class="info">$1</span>');
  return html;
}

async function loadLog() {
  const profile = selectedProfile();
  if (!profile || state.tab !== "console") return;
  try {
    const data = await api(`/api/log?profile=${encodeURIComponent(profile.id)}`);
    const signature = `${profile.id}:${data.lines.length}:${data.lines.at(-1) || ""}:${state.clearBefore}`;
    if (signature === state.logSignature) return;
    state.logSignature = signature;
    const output = $("#console-output");
    output.innerHTML = data.lines.length
      ? data.lines.map((line) => `<div class="console-line">${colorizeLog(line)}</div>`).join("")
      : '<p class="muted-line">No logs yet. Start this server to begin the story.</p>';
    $("#log-source").textContent = data.source;
    output.scrollTop = output.scrollHeight;
  } catch (error) {
    console.warn(error);
  }
}

function openCreateModal() {
  $("#create-modal").hidden = false;
  setTimeout(() => $("#create-form [name=name]").focus(), 30);
}

function closeCreateModal() {
  $("#create-modal").hidden = true;
}

async function runAction(path, body = {}) {
  try {
    const response = await api(path, { method: "POST", body: JSON.stringify(body) });
    if (response.message && !response.id) toast(response.message);
    await refreshState(true);
    return response;
  } catch (error) {
    toast(error.message, "error");
    throw error;
  }
}

async function loadMods() {
  const profile = selectedProfile();
  if (!profile) return;
  try {
    const { mods } = await api(`/api/profiles/${encodeURIComponent(profile.id)}/mods`);
    $("#mods-list").innerHTML = mods.length ? mods.map((mod) => `
      <div class="list-item ${mod.enabled ? "" : "disabled"}">
        <span class="item-block">${mod.enabled ? "M" : "×"}</span>
        <span><strong>${escapeHtml(mod.name)}</strong><small>${formatBytes(mod.bytes)} · ${mod.enabled ? "Enabled" : "Disabled"}</small></span>
        <button class="tiny-button" data-toggle-mod="${escapeHtml(mod.name)}">${mod.enabled ? "DISABLE" : "ENABLE"}</button>
      </div>`).join("") : '<div class="list-empty">NO MODS IN THIS WORLD YET</div>';
    $$('[data-toggle-mod]').forEach((button) => button.addEventListener("click", async () => {
      await runAction(`/api/profiles/${encodeURIComponent(profile.id)}/mods/toggle`, { name: button.dataset.toggleMod });
      await loadMods();
    }));
  } catch (error) { toast(error.message, "error"); }
}

async function loadBackups() {
  const profile = selectedProfile();
  if (!profile) return;
  try {
    const { backups } = await api(`/api/profiles/${encodeURIComponent(profile.id)}/backups`);
    const operationRunning = state.data?.job?.status === "running";
    $("#backups-list").innerHTML = backups.length ? backups.map((backup) => `
      <div class="list-item">
        <span class="item-block">▰</span>
        <span><strong>${escapeHtml(backup.name)}</strong><small>${formatBytes(backup.bytes)} · ${formatDate(backup.modified)}</small></span>
        <button class="tiny-button restore-button" data-restore-backup="${escapeHtml(backup.name)}" ${operationRunning ? "disabled" : ""}>APPLY</button>
      </div>`).join("") : '<div class="list-empty">NO BACKUPS CREATED YET</div>';
    $$('[data-restore-backup]').forEach((button) => button.addEventListener("click", () => openRestoreModal(button.dataset.restoreBackup)));
  } catch (error) { toast(error.message, "error"); }
}

function openRestoreModal(backupName) {
  const profile = selectedProfile();
  state.restoreBackup = backupName;
  $("#restore-backup-name").textContent = backupName;
  $("#restore-behavior").textContent = profile.running
    ? `${profile.name} is running. It will be saved, stopped, restored, and brought online again.`
    : `${profile.name} is offline. The backup can be applied without starting the server.`;
  $("#restore-restart-step").hidden = !profile.running;
  $("#restore-confirm-name").value = "";
  $("#confirm-restore").disabled = true;
  $("#create-modal").hidden = true;
  $("#restore-modal").hidden = false;
  setTimeout(() => $("#restore-confirm-name").focus(), 30);
}

function closeRestoreModal() {
  $("#restore-modal").hidden = true;
  state.restoreBackup = null;
}

function renderPlayers(profile, players) {
  state.players[profile.id] = players;
  $("#stat-players").textContent = `${players.online}/${players.maximum} online`;
  $("#players-online-count").textContent = players.online;
  $("#players-capacity").textContent = `${players.online} of ${players.maximum} slots occupied`;
  const capacityBucket = Math.min(10, Math.round(players.maximum ? players.online / players.maximum * 10 : 0));
  $("#players-capacity-bar").className = `capacity-${capacityBucket}`;
  $("#players-light").classList.toggle("online", players.status === "online");
  const list = $("#players-list");
  if (players.players.length) {
    list.innerHTML = players.players.map((name) => `
      <article class="player-card">
        <span class="player-head">${escapeHtml(name.slice(0, 2).toUpperCase())}</span>
        <span><strong>${escapeHtml(name)}</strong><small>CONNECTED</small></span>
        <span class="player-signal" title="Online">▮▮▮</span>
      </article>`).join("");
  } else {
    const title = players.status === "offline" ? "SERVER IS OFFLINE" : "NO PLAYERS CONNECTED";
    const copy = players.status === "offline" ? "Start this world to see connected players." : "This world is online and waiting for adventurers.";
    list.innerHTML = `<div class="players-empty"><strong>${title}</strong><span>${copy}</span></div>`;
  }
}

async function loadPlayers(force = false) {
  const profile = selectedProfile();
  if (!profile) return;
  try {
    const players = await api(`/api/players?profile=${encodeURIComponent(profile.id)}${force ? "&refresh=1" : ""}`);
    renderPlayers(profile, players);
  } catch (error) {
    if (force) toast(error.message, "error");
  }
}

function configQuery(extra = {}) {
  const profile = selectedProfile();
  return new URLSearchParams({ profile: profile.id, root: state.config.root || "config", ...extra }).toString();
}

function configCanLeave() {
  return !state.config.dirty || confirm("Discard the unsaved configuration edits?");
}

function renderConfigRoots() {
  const select = $("#config-root");
  select.innerHTML = state.config.roots.map((root) => `<option value="${escapeHtml(root.id)}">${escapeHtml(root.name)}</option>`).join("");
  select.value = state.config.root;
}

function renderBreadcrumbs() {
  const parts = state.config.path ? state.config.path.split("/") : [];
  const crumbs = [{ name: state.config.roots.find((root) => root.id === state.config.root)?.name || "Files", path: "" }];
  parts.forEach((part, index) => crumbs.push({ name: part, path: parts.slice(0, index + 1).join("/") }));
  $("#config-breadcrumbs").innerHTML = crumbs.map((crumb) => `<button class="crumb" data-config-path="${escapeHtml(crumb.path)}">${escapeHtml(crumb.name)}</button>`).join("");
  $$('[data-config-path]').forEach((button) => button.addEventListener("click", () => {
    if (configCanLeave()) loadConfigDirectory(button.dataset.configPath);
  }));
}

function renderConfigEntries() {
  const filter = $("#config-filter").value.trim().toLowerCase();
  const entries = state.config.entries.filter((entry) => entry.name.toLowerCase().includes(filter));
  const list = $("#config-file-list");
  if (!entries.length) {
    list.innerHTML = `<div class="file-empty">${filter ? "NO MATCHING FILES" : "THIS FOLDER HAS NO VISIBLE CONFIGURATION FILES"}</div>`;
    return;
  }
  list.innerHTML = entries.map((entry) => `
    <button class="file-entry ${entry.directory ? "folder" : ""} ${!entry.directory && !entry.editable ? "uneditable" : ""} ${state.config.file?.path === entry.path ? "selected" : ""}" data-entry-path="${escapeHtml(entry.path)}">
      <span class="file-kind">${entry.directory ? "▰" : entry.editable ? "≡" : "×"}</span>
      <strong>${escapeHtml(entry.name)}</strong>
      <small>${entry.directory ? "FOLDER" : entry.editable ? formatBytes(entry.bytes) : "BINARY"}</small>
    </button>`).join("");
  $$('[data-entry-path]').forEach((button) => button.addEventListener("click", () => {
    const entry = state.config.entries.find((item) => item.path === button.dataset.entryPath);
    if (!entry) return;
    if (entry.directory) { if (configCanLeave()) loadConfigDirectory(entry.path); }
    else if (entry.editable) { if (configCanLeave()) openConfigFile(entry.path); }
    else toast("That file is binary, unsupported, or larger than 2 MB.", "error");
  }));
}

function clearConfigEditor() {
  state.config.file = null;
  state.config.dirty = false;
  $("#config-editor").hidden = true;
  $("#editor-empty").hidden = false;
  $("#config-file-name").textContent = "SELECT A CONFIG FILE";
  $("#config-file-meta").textContent = "CFG, JSON, TOML, properties, scripts, and other text formats";
  $("#config-save-state").textContent = "No file selected";
  $("#config-save").disabled = true;
  $("#config-reload").disabled = true;
  $("#config-modified").hidden = true;
}

async function loadConfigRoots() {
  const profile = selectedProfile();
  if (!profile) return;
  try {
    const { roots } = await api(`/api/config/roots?profile=${encodeURIComponent(profile.id)}`);
    state.config.roots = roots;
    if (!roots.some((root) => root.id === state.config.root)) state.config.root = roots.find((root) => root.id === "config")?.id || roots[0]?.id;
    state.config.path = "";
    clearConfigEditor();
    renderConfigRoots();
    await loadConfigDirectory("");
  } catch (error) { toast(error.message, "error"); }
}

async function loadConfigDirectory(path) {
  try {
    const data = await api(`/api/config/list?${configQuery({ path })}`);
    state.config.path = data.path;
    state.config.entries = data.entries;
    clearConfigEditor();
    renderBreadcrumbs();
    renderConfigEntries();
  } catch (error) { toast(error.message, "error"); }
}

async function openConfigFile(path) {
  try {
    const file = await api(`/api/config/file?${configQuery({ path })}`);
    state.config.file = file;
    state.config.dirty = false;
    const editor = $("#config-editor");
    editor.value = file.content;
    editor.hidden = false;
    $("#editor-empty").hidden = true;
    $("#config-file-name").textContent = file.name;
    $("#config-file-meta").textContent = `${file.path} · ${formatBytes(file.bytes)}`;
    $("#config-save-state").textContent = "Loaded safely";
    $("#config-save").disabled = true;
    $("#config-reload").disabled = false;
    $("#config-modified").hidden = true;
    renderConfigEntries();
  } catch (error) { toast(error.message, "error"); }
}

async function saveConfigFile() {
  const profile = selectedProfile();
  const file = state.config.file;
  if (!profile || !file || !state.config.dirty) return;
  try {
    const response = await api("/api/config/save", {
      method: "POST",
      body: JSON.stringify({ profileId: profile.id, root: state.config.root, path: file.path, content: $("#config-editor").value, expectedHash: file.hash }),
    });
    state.config.file = response.file;
    state.config.dirty = false;
    $("#config-save-state").textContent = "Saved · recovery copy created";
    $("#config-save").disabled = true;
    $("#config-modified").hidden = true;
    $("#config-file-meta").textContent = `${response.file.path} · ${formatBytes(response.file.bytes)}`;
    toast(response.message);
  } catch (error) { toast(error.message, "error"); }
}

async function uploadMods(files) {
  const profile = selectedProfile();
  if (!profile) return;
  for (const file of files) {
    if (!file.name.toLowerCase().endsWith(".jar")) { toast(`${file.name} is not a .jar mod.`, "error"); continue; }
    try {
      toast(`Uploading ${file.name}…`);
      await api(`/api/profiles/${encodeURIComponent(profile.id)}/mods/${encodeURIComponent(file.name)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/java-archive" },
        body: file,
      });
      toast(`${file.name} added.`);
    } catch (error) { toast(error.message, "error"); }
  }
  await loadMods();
  await refreshState(true);
}

function renderPerformanceChart(history) {
  const canvas = $("#performance-chart");
  const empty = $("#chart-empty");
  const points = (history || []).filter((point) => Number.isFinite(point.cpu) || Number.isFinite(point.latency));
  empty.hidden = points.length > 1;
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * ratio));
  canvas.height = Math.round(190 * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  const width = rect.width;
  const height = 190;
  context.clearRect(0, 0, width, height);
  context.strokeStyle = "#293127";
  context.lineWidth = 1;
  for (let row = 0; row <= 4; row += 1) {
    const y = 8 + row * ((height - 20) / 4);
    context.beginPath(); context.moveTo(0, y); context.lineTo(width, y); context.stroke();
  }
  if (points.length < 2) return;
  const draw = (key, color, ceiling) => {
    const values = points.map((point) => Number.isFinite(point[key]) ? point[key] : null);
    const max = Math.max(ceiling, ...values.filter(Number.isFinite));
    context.strokeStyle = color; context.lineWidth = 2; context.beginPath();
    let drawing = false;
    values.forEach((value, index) => {
      if (!Number.isFinite(value)) { drawing = false; return; }
      const x = index / Math.max(1, values.length - 1) * width;
      const y = height - 10 - Math.min(1, value / max) * (height - 20);
      if (!drawing) { context.moveTo(x, y); drawing = true; } else context.lineTo(x, y);
    });
    context.stroke();
  };
  draw("cpu", "#8bca64", 100);
  draw("mspt", "#e3b74f", 50);
  draw("latency", "#51c8c0", 20);
}

function resourceRow(label, value, note, available = true) {
  return `<div class="resource-row ${available ? "" : "unavailable"}"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(value)}</span>${note ? `<small>${escapeHtml(note)}</small>` : ""}</div>`;
}

function renderTimeline(events) {
  const filter = $("#timeline-filter").value;
  const visible = (events || []).filter((event) => filter === "all" || event.category === filter);
  $("#timeline-count").textContent = `${events?.length || 0} retained events`;
  $("#performance-timeline").innerHTML = visible.length ? visible.map((event) => `
    <article class="timeline-event">
      <time>${escapeHtml(event.time.replace("T", " "))}<br>${escapeHtml(event.source)}</time>
      <span class="event-kind ${escapeHtml(event.category)}">${escapeHtml(event.category.toUpperCase())}</span>
      <p>${escapeHtml(event.message)}${Number.isFinite(event.behindMs) ? `<br><strong>${(event.behindMs / 1000).toFixed(1)}s behind · ${event.skippedTicks} ticks skipped</strong>` : ""}</p>
    </article>`).join("") : '<div class="timeline-empty">NO EVENTS MATCH THIS FILTER</div>';
}

function renderPerformance(profile, data) {
  state.performance = data;
  const running = data.process.running;
  const alerts = data.alerts || [];
  const severity = alerts.some((item) => item.severity === "critical") ? "critical" : alerts.length ? "warning" : (running ? "good" : "");
  $("#performance-light").classList.toggle("online", running);
  $("#health-title").textContent = running ? (alerts.length ? "ATTENTION RECOMMENDED" : "LIVE TELEMETRY ACTIVE") : "SERVER OFFLINE";
  $("#health-detail").textContent = running ? "BlockOps is collecting host and server-capability measurements without changing configuration." : "Historical log, storage, capability, and configuration diagnostics remain available.";
  $("#health-chip").className = `health-chip ${severity}`;
  $("#health-chip").textContent = running ? (alerts.length ? `${alerts.length} ALERT${alerts.length === 1 ? "" : "S"}` : "NO ACTIVE ALERTS") : "NO LIVE SAMPLE";
  $("#performance-source").textContent = data.capabilities.spark ? "Universal collectors + Spark" : `Universal collectors · ${data.capabilities.loader}`;
  $("#metric-tps").textContent = formatMetric(data.tick.tps, "", 1);
  $("#metric-mspt").textContent = formatMetric(data.tick.mspt, " ms", 1);
  $("#metric-cpu").textContent = formatMetric(data.process.cpuPercent, "%", 1);
  $("#metric-ram").textContent = Number.isFinite(data.process.rssBytes) ? formatBytes(data.process.rssBytes) : "—";
  $("#metric-latency").textContent = formatMetric(data.network.local.latencyMs, " ms", 2);
  $("#metric-latency-note").textContent = data.network.local.reachable ? "Local Minecraft TCP reachable" : (data.network.local.reason || "Not reachable");
  $("#metric-disk").textContent = formatMetric(data.storage.diskFreePercent, "%", 1);
  $("#metric-disk-note").textContent = `${formatBytes(data.storage.diskFreeBytes)} available`;
  $("#metric-tps-note").textContent = data.tick.source ? `Measured via ${data.tick.source}` : (data.capabilities.spark ? "Run Spark TPS for a reading" : "No compatible TPS provider detected");
  renderPerformanceChart(data.history);

  const capabilities = [
    ["PROCESS", data.capabilities.processMetrics], ["LOG EVENTS", data.capabilities.logDiagnostics], ["STORAGE", data.capabilities.storageMetrics],
    ["SPARK", data.capabilities.spark], ["FORGE TPS", data.capabilities.forgeTps], ["FABRIC", data.capabilities.fabric], ["PLAYIT", data.capabilities.playit],
  ];
  $("#capability-strip").innerHTML = capabilities.map(([name, available]) => `<span class="capability ${available ? "available" : ""}">${available ? "✓" : "—"} ${name}</span>`).join("");
  const actions = [];
  if (data.capabilities.spark) actions.push(
    ["spark-tps", "TPS + CPU", "Quick live reading"], ["spark-health", "Health report", "CPU, memory, disk, network"],
    ["spark-profile-30", "30s profile", "Fast CPU sample"], ["spark-profile-60", "60s profile", "Standard CPU sample"],
    ["spark-profile-slow", "Slow ticks only", "Capture ticks over 100 ms"], ["spark-profile-stop", "Stop + open", "Finish an active profile"],
    ["spark-gc", "GC history", "Pause and collector data"], ["spark-heapsummary", "Heap summary", "Memory class breakdown"], ["spark-ping", "Player ping", "Average player round trips"],
  );
  if (data.capabilities.forgeTps) actions.push(["forge-tps", "Dimension TPS", "Forge per-world tick reading"]);
  $("#profile-actions").innerHTML = actions.length ? actions.map(([action, title, detail]) => `<button class="profile-action" data-performance-action="${action}" ${running ? "" : "disabled"}><strong>${title}</strong><span>${detail}</span></button>`).join("") : '<div class="timeline-empty">UNIVERSAL TELEMETRY IS AVAILABLE. INSTALL A COMPATIBLE PROFILER FOR TICK ATTRIBUTION.</div>';
  $$('[data-performance-action]').forEach((button) => button.addEventListener("click", () => runPerformanceAction(button.dataset.performanceAction)));
  $("#performance-reports").innerHTML = (data.reports || []).map((url) => `<a class="report-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">OPEN SPARK REPORT · ${escapeHtml(url)} ↗</a>`).join("");

  $("#resource-list").innerHTML = [
    resourceRow("Java process", data.process.pid ? `PID ${data.process.pid}` : "Offline", data.process.uptime ? `Uptime ${data.process.uptime}` : "No active process"),
    resourceRow("Java heap", Number.isFinite(data.process.heapUsedBytes) ? formatBytes(data.process.heapUsedBytes) : "Unavailable", "Run a Spark health or heap probe for JVM heap detail.", Number.isFinite(data.process.heapUsedBytes)),
    resourceRow("Garbage collection", Number.isFinite(data.process.gcPauseMillis) ? `${data.process.gcPauseMillis} ms` : "Unavailable", "Spark GC provides collector history on supported servers.", Number.isFinite(data.process.gcPauseMillis)),
    resourceRow("Host", `${data.host.machine} · ${data.host.cpuCount || "?"} cores`, Number.isFinite(data.host.totalMemoryBytes) ? `${formatBytes(data.host.totalMemoryBytes)} physical memory` : data.host.system),
    resourceRow("Loaded chunks / dimensions", "Unavailable", "Use Forge dimension TPS or a Spark profile to attribute tick work.", false),
    resourceRow("Entities / block entities", "Unavailable", "A profiler is required to attribute classes without changing gameplay.", false),
    resourceRow("World size", formatBytes(data.storage.worldBytes), `${data.storage.regionFiles} region files`),
    resourceRow("Backups", formatBytes(data.storage.backupBytes), "Correlate archive creation with timeline stalls"),
    resourceRow("Logs", formatBytes(data.storage.logBytes), "Retained diagnostic evidence"),
    resourceRow("Mod files", formatBytes(data.storage.modBytes), "Inventory size, not runtime cost"),
    resourceRow("Network throughput", "Unavailable", data.capabilities.networkThroughputReason, false),
    resourceRow("Public endpoint latency", "Unavailable", "A public endpoint or external probe must be configured before route latency can be measured.", false),
    resourceRow("Playit agent", data.network.playitRunning ? "Running" : "Stopped", "Tunnel status is separate from local server response"),
  ].join("");

  $("#performance-alerts").innerHTML = alerts.length ? alerts.map((alert) => `<article class="alert-item ${escapeHtml(alert.severity)}"><i></i><div><strong>${escapeHtml(alert.title)}</strong><p>${escapeHtml(alert.detail)}</p></div></article>`).join("") : '<article class="alert-item info"><i></i><div><strong>NO RETAINED ALERTS</strong><p>No alert condition was found in the measurements currently available.</p></div></article>';
  $("#performance-recommendations").innerHTML = (data.recommendations || []).map((item) => `<article class="recommendation-item"><span class="risk-tag ${escapeHtml(item.risk)}">${escapeHtml(item.risk.toUpperCase())}</span><div><strong>${escapeHtml(item.category)} · ${escapeHtml(item.title)}</strong><p>${escapeHtml(item.detail)}</p></div></article>`).join("");
  renderTimeline(data.events);
}

async function loadPerformance(force = false) {
  const profile = selectedProfile();
  if (!profile || state.tab !== "performance") return;
  try {
    const data = await api(`/api/performance?profile=${encodeURIComponent(profile.id)}${force ? "&refresh=1" : ""}`);
    renderPerformance(profile, data);
  } catch (error) { if (force) toast(error.message, "error"); }
}

async function runPerformanceAction(action) {
  const profile = selectedProfile();
  if (!profile) return;
  try {
    const result = await api("/api/performance/action", { method: "POST", body: JSON.stringify({ profileId: profile.id, action }) });
    toast(result.message);
    setTimeout(() => loadPerformance(true), 1200);
  } catch (error) { toast(error.message, "error"); }
}

async function exportPerformanceReport() {
  const profile = selectedProfile();
  if (!profile) return;
  try {
    const report = await api(`/api/performance/report?profile=${encodeURIComponent(profile.id)}`);
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `blockops-${profile.id}-performance-${new Date().toISOString().slice(0, 19).replaceAll(":", "-")}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    toast("Diagnostic report exported. Secrets were redacted.");
  } catch (error) { toast(error.message, "error"); }
}

function activateTab(name) {
  state.tab = name;
  localStorage.setItem("blockops-tab", name);
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
  if (name === "console") loadLog();
  if (name === "mods") loadMods();
  if (name === "backups") loadBackups();
  if (name === "players") loadPlayers();
  if (name === "performance") loadPerformance(true);
  if (name === "files" && !state.config.roots.length) loadConfigRoots();
}

function wireEvents() {
  $("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
  for (const button of [$("#create-server"), $("#empty-create")]) button.addEventListener("click", openCreateModal);
  $("#setup-guide").addEventListener("click", openSetupGuide);
  $$('[data-close-setup]').forEach((button) => button.addEventListener("click", closeSetupGuide));
  $("#setup-modal").addEventListener("click", (event) => { if (event.target === $("#setup-modal")) closeSetupGuide(); });
  $$('[data-close-modal]').forEach((button) => button.addEventListener("click", closeCreateModal));
  $$('[data-close-restore]').forEach((button) => button.addEventListener("click", closeRestoreModal));
  $("#create-modal").addEventListener("click", (event) => { if (event.target === $("#create-modal")) closeCreateModal(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeCreateModal(); closeRestoreModal(); closeSetupGuide(); $("#sidebar").classList.remove("open"); } });
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));
  $("#start-button").addEventListener("click", () => runAction("/api/jobs/start", { profileId: selectedProfile().id }));
  $("#stop-button").addEventListener("click", () => runAction("/api/jobs/stop"));
  for (const button of [$("#open-folder"), $("#open-folder-top")]) button.addEventListener("click", () => {
    const profile = selectedProfile(); if (profile) runAction(`/api/profiles/${encodeURIComponent(profile.id)}/open`);
  });
  $("#command-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#command-input");
    const command = input.value.trim();
    if (!command) return;
    try { await runAction("/api/command", { command }); input.value = ""; setTimeout(loadLog, 500); } catch {}
  });
  $("#clear-console").addEventListener("click", () => { state.clearBefore = Date.now(); state.logSignature = ""; $("#console-output").innerHTML = '<p class="muted-line">View cleared. New server activity will appear here.</p>'; });
  $("#settings-form").addEventListener("input", () => { state.settingsDirty = true; });
  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const profile = selectedProfile();
    const form = new FormData(event.currentTarget);
    const jvmArguments = String(form.get("jvmArguments") || "").match(/(?:[^\s"]+|"[^"]*")+/g) || [];
    try {
      await runAction(`/api/profiles/${encodeURIComponent(profile.id)}/settings`, {
        name: form.get("name"), minimumRam: form.get("minimumRam"), maximumRam: form.get("maximumRam"), jvmArguments: jvmArguments.map((value) => value.replace(/^"|"$/g, "")),
        properties: { motd: form.get("motd"), gamemode: form.get("gamemode"), difficulty: form.get("difficulty"), maxPlayers: form.get("maxPlayers"), whiteList: form.has("whiteList"), hardcore: form.has("hardcore"), onlineMode: form.has("onlineMode"), pvp: form.has("pvp") },
      });
      state.settingsDirty = false;
      await refreshState(true);
    } catch {}
  });
  $("#create-form").addEventListener("change", (event) => {
    if (event.target.name === "loader") $("#loader-version-field").hidden = event.target.value === "vanilla";
  });
  $("#create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await runAction("/api/jobs/create", Object.fromEntries(form.entries()));
  });
  $("#create-backup").addEventListener("click", async () => { await runAction(`/api/profiles/${encodeURIComponent(selectedProfile().id)}/backup`); setTimeout(loadBackups, 1500); });
  $("#backup-settings-form").addEventListener("input", () => { state.backupPolicyDirty = true; });
  $("#backup-settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const profile = selectedProfile();
    const form = new FormData(event.currentTarget);
    try {
      const response = await runAction(`/api/profiles/${encodeURIComponent(profile.id)}/backup-settings`, {
        enabled: form.has("enabled"), onlyWhenEmpty: form.has("onlyWhenEmpty"), backupOnStop: form.has("backupOnStop"), intervalMinutes: form.get("intervalMinutes"), retention: form.get("retention"), compressionLevel: form.get("compressionLevel"),
      });
      profile.backupSettings = response.settings;
      state.backupPolicyDirty = false;
      populateBackupSettings(profile);
    } catch {}
  });
  $("#restore-confirm-name").addEventListener("input", (event) => {
    $("#confirm-restore").disabled = event.target.value.trim() !== selectedProfile().name;
  });
  $("#confirm-restore").addEventListener("click", async () => {
    const profile = selectedProfile();
    const backupName = state.restoreBackup;
    if (!backupName || $("#restore-confirm-name").value.trim() !== profile.name) return;
    try {
      await runAction("/api/jobs/restore-backup", { profileId: profile.id, backupName });
      closeRestoreModal();
    } catch {}
  });
  $("#refresh-players").addEventListener("click", () => loadPlayers(true));
  $("#refresh-performance").addEventListener("click", () => loadPerformance(true));
  $("#export-performance").addEventListener("click", exportPerformanceReport);
  $("#timeline-filter").addEventListener("change", () => renderTimeline(state.performance?.events || []));
  window.addEventListener("resize", () => { if (state.tab === "performance" && state.performance) renderPerformanceChart(state.performance.history); });
  $("#config-root").addEventListener("change", (event) => {
    if (!configCanLeave()) { event.target.value = state.config.root; return; }
    state.config.root = event.target.value;
    state.config.path = "";
    loadConfigDirectory("");
  });
  $("#config-filter").addEventListener("input", renderConfigEntries);
  $("#config-editor").addEventListener("input", () => {
    if (!state.config.file) return;
    state.config.dirty = $("#config-editor").value !== state.config.file.content;
    $("#config-save").disabled = !state.config.dirty;
    $("#config-modified").hidden = !state.config.dirty;
    $("#config-save-state").textContent = state.config.dirty ? "Unsaved changes" : "Loaded safely";
  });
  $("#config-save").addEventListener("click", saveConfigFile);
  $("#config-reload").addEventListener("click", () => { if (state.config.file && configCanLeave()) openConfigFile(state.config.file.path); });
  $("#go-to-mods").addEventListener("click", () => activateTab("mods"));
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s" && state.tab === "files") {
      event.preventDefault();
      saveConfigFile();
    }
  });
  $("#mod-upload").addEventListener("change", (event) => uploadMods(event.target.files));
  const dropZone = $("#drop-zone");
  for (const name of ["dragenter", "dragover"]) dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add("dragging"); });
  for (const name of ["dragleave", "drop"]) dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove("dragging"); });
  dropZone.addEventListener("drop", (event) => uploadMods(event.dataTransfer.files));
  $("#job-collapse").addEventListener("click", () => $("#job-drawer").classList.toggle("collapsed"));
  $("#quit-dashboard").addEventListener("click", async () => { if (confirm("Quit the dashboard? Running Minecraft servers will stay online.")) { await runAction("/api/shutdown"); document.body.innerHTML = '<main class="empty-state"><p class="eyebrow">DASHBOARD CLOSED</p><h1>YOUR SERVER KEEPS RUNNING</h1><p>You can close this tab and reopen BlockOps whenever you need it.</p></main>'; } });
}

async function init() {
  wireEvents();
  activateTab(state.tab);
  try {
    const { versions } = await api("/api/versions");
    $("#version-list").innerHTML = versions.map((version) => `<option value="${escapeHtml(version)}"></option>`).join("");
  } catch {}
  await refreshState();
  await loadLog();
  setInterval(() => refreshState(true), 1800);
  setInterval(loadLog, 1100);
  setInterval(() => { if (state.tab === "players") loadPlayers(false); }, 10000);
  setInterval(() => { if (state.tab === "performance") loadPerformance(false); }, 2000);
}

init();
