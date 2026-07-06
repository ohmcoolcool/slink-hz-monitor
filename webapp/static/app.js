const state = {
  hours: 8,
  status: "all",
  search: "",
  data: null,
  selectedStation: null,
};

const statusOrder = ["green", "yellow", "red", "missing"];

function qs(selector) {
  return document.querySelector(selector);
}

function formatDateTime(value) {
  if (!value) return "Never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatAge(value) {
  if (value === null || value === undefined || value === "") return "no data";
  const numeric = Number(value);
  if (Number.isNaN(numeric)) return "no data";
  return `${numeric.toFixed(1)}m`;
}

function statusClass(status) {
  return statusOrder.includes(status) ? status : "missing";
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

function setText(id, value) {
  qs(`#${id}`).textContent = value;
}

function renderSummary(data) {
  const summary = data.summary || {};
  const green = summary.green || 0;
  const yellow = summary.yellow || 0;
  const red = summary.red || 0;
  const missing = summary.missing || 0;
  setText("greenCount", green);
  setText("yellowCount", yellow);
  setText("redCount", red);
  setText("totalCount", green + yellow + red + missing);
}

function renderHeader(data) {
  const config = data.config || {};
  const lastPoll = data.last_poll;
  qs("#serverLine").textContent = `${config.server || "-"} \u00b7 ${config.network || "TM"}/${config.channel || "HZ"} \u00b7 poll ${config.poll_seconds || "-"}s \u00b7 slot ${config.slot_minutes || "-"}m`;
  qs("#lastUpdated").textContent = lastPoll
    ? `Last poll ${formatDateTime(lastPoll.poll_time)}`
    : "No poll recorded yet";

  const alert = qs("#systemAlert");
  if (lastPoll && !lastPoll.success) {
    alert.classList.remove("hidden");
    alert.textContent = `slinktool error: ${lastPoll.error_message || "unknown error"}`;
  } else {
    alert.classList.add("hidden");
    alert.textContent = "";
  }

  qs("#exportLink").href = "/api/export.csv";
}

function passesFilters(row) {
  const query = state.search.trim().toUpperCase();
  if (query && !row.station.toUpperCase().includes(query)) return false;
  if (state.status !== "all" && row.last_status !== state.status) return false;
  return true;
}

function cellTitle(cell) {
  if (!cell || cell.slot_status === "missing") return "No poll data in this slot";
  const total = cell.total_count || 0;
  return [
    `slot: ${formatDateTime(cell.slot_start)}`,
    `summary: ${cell.slot_status}`,
    `last: ${cell.last_status}`,
    `green/yellow/red: ${cell.green_count || 0}/${cell.yellow_count || 0}/${cell.red_count || 0}`,
    `green pct: ${cell.green_pct ?? 0}%`,
    `latest packet: ${formatDateTime(cell.latest_packet_time)}`,
    `polls: ${total}`,
  ].join("\n");
}

function renderTable(data) {
  const table = qs("#slotTable");
  const thead = table.querySelector("thead");
  const tbody = table.querySelector("tbody");
  const emptyState = qs("#emptyState");
  const rows = (data.rows || []).filter(passesFilters);

  thead.innerHTML = "";
  tbody.innerHTML = "";

  const headRow = document.createElement("tr");
  const stationHead = document.createElement("th");
  stationHead.textContent = "Station";
  headRow.appendChild(stationHead);
  for (const slot of data.slots || []) {
    const th = document.createElement("th");
    th.textContent = slot.label;
    th.title = formatDateTime(slot.start);
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);

  emptyState.classList.toggle("hidden", rows.length > 0);

  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.dataset.station = row.station;
    tr.addEventListener("click", () => selectStation(row.station));

    const stationCell = document.createElement("td");
    const nameWrap = document.createElement("span");
    nameWrap.className = "station-name";
    const dot = document.createElement("span");
    dot.className = `status-dot status-${statusClass(row.last_status)}`;
    const name = document.createElement("span");
    name.textContent = row.station;
    nameWrap.append(dot, name);
    stationCell.appendChild(nameWrap);
    stationCell.title = `Latest packet: ${formatDateTime(row.latest_packet_time)}\nAge: ${formatAge(row.age_minutes)}`;
    tr.appendChild(stationCell);

    for (const cell of row.cells || []) {
      const td = document.createElement("td");
      const mark = document.createElement("span");
      const status = statusClass(cell.slot_status);
      mark.className = `cell ${status}`;
      const cellDot = document.createElement("span");
      cellDot.className = `status-dot status-${status}`;
      mark.appendChild(cellDot);
      td.appendChild(mark);
      td.title = cellTitle(cell);
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function renderData(data) {
  state.data = data;
  renderSummary(data);
  renderHeader(data);
  renderTable(data);
}

async function loadDashboard() {
  try {
    const data = await fetchJson(`/api/status/slots?hours=${state.hours}`);
    renderData(data);
  } catch (error) {
    const alert = qs("#systemAlert");
    alert.classList.remove("hidden");
    alert.textContent = `Dashboard API error: ${error.message}`;
  }
}

async function pollNow() {
  qs("#pollNowButton").disabled = true;
  try {
    await fetchJson("/api/poll-now", { method: "POST" });
    await loadDashboard();
  } finally {
    qs("#pollNowButton").disabled = false;
  }
}

function metric(label, value) {
  return `<div class="detail-metric"><span>${label}</span><strong>${value}</strong></div>`;
}

async function selectStation(station) {
  state.selectedStation = station;
  qs("#detailTitle").textContent = station;
  qs("#detailSubtitle").textContent = "Poll-level history for the selected station";
  qs("#detailBody").innerHTML = `<div class="detail-placeholder">Loading ${station}...</div>`;

  const latest = (state.data?.rows || []).find((row) => row.station === station);
  const history = await fetchJson(`/api/stations/${encodeURIComponent(station)}/history?hours=24`);
  const records = history.records || [];
  const green = records.filter((record) => record.status === "green").length;
  const yellow = records.filter((record) => record.status === "yellow").length;
  const red = records.filter((record) => record.status === "red").length;
  const total = Math.max(records.length, 1);
  const healthyPct = (((green + yellow) / total) * 100).toFixed(1);

  const list = records.slice(0, 12).map((record) => `
    <div class="history-row">
      <span>${formatDateTime(record.poll_time)}</span>
      <span><span class="status-dot status-${statusClass(record.status)}"></span> ${record.status}</span>
      <span>${formatDateTime(record.latest_packet_time)}</span>
      <span>${formatAge(record.age_minutes)}</span>
    </div>
  `).join("");

  qs("#detailBody").innerHTML = `
    <div class="detail-grid">
      ${metric("Current", latest?.last_status || "missing")}
      ${metric("Age", formatAge(latest?.age_minutes))}
      ${metric("Healthy 24h", `${healthyPct}%`)}
      ${metric("Records", records.length)}
    </div>
    <div class="history-list">${list || "<div class=\"detail-placeholder\">No recent poll records</div>"}</div>
  `;
}

function bindControls() {
  qs("#stationSearch").addEventListener("input", (event) => {
    state.search = event.target.value;
    if (state.data) renderTable(state.data);
  });

  qs("#hoursSelect").addEventListener("change", async (event) => {
    state.hours = Number(event.target.value);
    await loadDashboard();
  });

  for (const button of document.querySelectorAll(".segment")) {
    button.addEventListener("click", () => {
      for (const item of document.querySelectorAll(".segment")) item.classList.remove("active");
      button.classList.add("active");
      state.status = button.dataset.status;
      if (state.data) renderTable(state.data);
    });
  }

  qs("#pollNowButton").addEventListener("click", pollNow);
}

bindControls();
loadDashboard();
setInterval(loadDashboard, 30000);
