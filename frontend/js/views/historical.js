// Historical Charging Sessions view: IEEE ChargingRecords research dataset analytics,
// demand and duration breakdown, hourly distribution, top locations, distributions and session records.
import { api } from "../api.js";
import { esc, fmt, dt, tag, toast, debounce } from "../ui.js";
import { hBars, donut, barChart, cat, chartEmpty } from "../charts.js";

let root, ctx;
let currentSummary = null;
let currentSessions = [];
let queryParams = { limit: 50, offset: 0 };
let filters = {
  date_from: "",
  date_to: "",
  charger_type: "",
  location: "",
  company: "",
  q: "",
};
let filterOptions = {
  charger_types: [],
  companies: [],
  locations: [],
  date_range: null,
};
let totalRecords = 0;

export async function render(el, c) {
  root = el; ctx = c;
  queryParams = { limit: 50, offset: 0 };
  filters = {
    date_from: "",
    date_to: "",
    charger_type: "",
    location: "",
    company: "",
    q: "",
  };

  el.innerHTML = `
    <div class="note">
      <b>Historical Research Data</b> — IEEE charging records dataset. Original column names, source duration and demand values are preserved without unwarranted assumptions. This is historical empirical data, not live telemetry.
    </div>

    <!-- Filter Bar -->
    <div class="panel toolbar" id="histFilters" style="gap:12px;flex-wrap:wrap;align-items:flex-end;margin-bottom:14px;padding:12px 16px">
      <div class="field" style="margin:0">
        <span class="small muted">Date from</span>
        <input type="date" id="filterDateFrom" style="padding:4px 8px">
      </div>
      <div class="field" style="margin:0">
        <span class="small muted">Date to</span>
        <input type="date" id="filterDateTo" style="padding:4px 8px">
      </div>
      <div class="field" style="margin:0">
        <span class="small muted">Charger type</span>
        <select id="filterChargerType" style="padding:4px 8px">
          <option value="">All charger types</option>
        </select>
      </div>
      <div class="field" style="margin:0">
        <span class="small muted">Location</span>
        <select id="filterLocation" style="padding:4px 8px">
          <option value="">All locations</option>
        </select>
      </div>
      <div class="field" style="margin:0">
        <span class="small muted">Company / Network</span>
        <select id="filterCompany" style="padding:4px 8px">
          <option value="">All companies</option>
        </select>
      </div>
      <div class="field" style="margin:0">
        <button type="button" class="sm" id="resetFilters">Reset filters</button>
      </div>
    </div>

    <section class="kpis" id="histKpis" aria-label="Historical Session Metrics">
      <div class="loading">Loading metrics…</div>
    </section>

    <!-- Row 1: Hourly arrivals & Charger types -->
    <div class="grid-2e" id="chartsRow">
      <section class="panel">
        <div class="panel-h">
          <h2>Session arrival by hour of day (00:00 – 23:00)</h2>
          <span class="hint">hourly session distribution</span>
        </div>
        <div class="panel-b" id="hourlyChart" style="min-height:220px"></div>
      </section>

      <section class="panel">
        <div class="panel-h">
          <h2>Charger type distribution</h2>
          <span class="hint">empirical session share</span>
        </div>
        <div class="panel-b" id="typeChart" style="min-height:220px"></div>
      </section>
    </div>

    <!-- Row 2: Top locations & Companies -->
    <div class="grid-2e" id="locationsRow">
      <section class="panel">
        <div class="panel-h">
          <h2>Top charging locations</h2>
          <span class="hint">by total recorded sessions</span>
        </div>
        <div class="panel-b" id="locChart" style="min-height:200px"></div>
      </section>

      <section class="panel">
        <div class="panel-h">
          <h2>Charging network / company</h2>
          <span class="hint">recorded operator share</span>
        </div>
        <div class="panel-b" id="compChart" style="min-height:200px"></div>
      </section>
    </div>

    <!-- Row 3: Duration & Demand Distributions -->
    <div class="grid-2e" id="distributionRow">
      <section class="panel">
        <div class="panel-h">
          <h2>Session duration distribution</h2>
          <span class="hint">empirical session length brackets</span>
        </div>
        <div class="panel-b" id="durationChart" style="min-height:200px"></div>
      </section>

      <section class="panel">
        <div class="panel-h">
          <h2>Charging demand distribution</h2>
          <span class="hint">empirical energy demand brackets</span>
        </div>
        <div class="panel-b" id="demandChart" style="min-height:200px"></div>
      </section>
    </div>

    <!-- Historical Charging Records Table -->
    <section class="panel">
      <div class="panel-h">
        <h2>Historical charging records</h2>
        <div class="toolbar" style="gap:8px">
          <input type="search" id="histSearch" placeholder="Search charger, user or location…" style="width:260px" aria-label="Search historical records">
          <span class="hint" id="countHint"></span>
          <a class="btn-link sm" href="#integrations">Import more records →</a>
        </div>
      </div>
      <div class="panel-b flush tbl-wrap scroll-y" id="histTable" style="max-height:480px"></div>
      <div class="panel-b toolbar" id="histPagination" style="justify-content:space-between;border-top:1px solid var(--border)"></div>
    </section>
  `;

  wireFilterEvents();
  await loadAll();
}

function wireFilterEvents() {
  const fDateFrom = root.querySelector("#filterDateFrom");
  const fDateTo = root.querySelector("#filterDateTo");
  const fType = root.querySelector("#filterChargerType");
  const fLoc = root.querySelector("#filterLocation");
  const fComp = root.querySelector("#filterCompany");
  const bReset = root.querySelector("#resetFilters");
  const sSearch = root.querySelector("#histSearch");

  const onFilterChange = () => {
    filters.date_from = fDateFrom.value;
    filters.date_to = fDateTo.value;
    filters.charger_type = fType.value;
    filters.location = fLoc.value;
    filters.company = fComp.value;
    queryParams.offset = 0;
    loadAll();
  };

  fDateFrom.onchange = onFilterChange;
  fDateTo.onchange = onFilterChange;
  fType.onchange = onFilterChange;
  fLoc.onchange = onFilterChange;
  fComp.onchange = onFilterChange;

  bReset.onclick = () => {
    filters = {
      date_from: "",
      date_to: "",
      charger_type: "",
      location: "",
      company: "",
      q: "",
    };
    fDateFrom.value = "";
    fDateTo.value = "";
    fType.value = "";
    fLoc.value = "";
    fComp.value = "";
    sSearch.value = "";
    queryParams.offset = 0;
    loadAll();
  };

  sSearch.oninput = debounce(e => {
    filters.q = e.target.value.trim();
    queryParams.offset = 0;
    loadAll();
  }, 300);
}

function updateFilterDropdowns(opts) {
  if (!opts) return;
  filterOptions = opts;

  const fType = root.querySelector("#filterChargerType");
  const fLoc = root.querySelector("#filterLocation");
  const fComp = root.querySelector("#filterCompany");
  const fDateFrom = root.querySelector("#filterDateFrom");
  const fDateTo = root.querySelector("#filterDateTo");

  // Populate charger types
  if (opts.charger_types && opts.charger_types.length > 0 && fType.options.length <= 1) {
    opts.charger_types.forEach(t => {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = `Type ${t}`;
      if (filters.charger_type === t) opt.selected = true;
      fType.appendChild(opt);
    });
  }

  // Populate locations
  if (opts.locations && opts.locations.length > 0 && fLoc.options.length <= 1) {
    opts.locations.forEach(l => {
      const opt = document.createElement("option");
      opt.value = l;
      opt.textContent = l;
      if (filters.location === l) opt.selected = true;
      fLoc.appendChild(opt);
    });
  }

  // Populate companies
  if (opts.companies && opts.companies.length > 0 && fComp.options.length <= 1) {
    opts.companies.forEach(c => {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = `Company ${c}`;
      if (filters.company === c) opt.selected = true;
      fComp.appendChild(opt);
    });
  }

  // Set date bounds if available and not yet set
  if (opts.date_range && opts.date_range.min && opts.date_range.max) {
    if (!fDateFrom.min) fDateFrom.min = opts.date_range.min;
    if (!fDateFrom.max) fDateFrom.max = opts.date_range.max;
    if (!fDateTo.min) fDateTo.min = opts.date_range.min;
    if (!fDateTo.max) fDateTo.max = opts.date_range.max;
  }
}

function buildParams() {
  const p = new URLSearchParams();
  if (filters.date_from) p.set("date_from", filters.date_from);
  if (filters.date_to) p.set("date_to", filters.date_to);
  if (filters.charger_type) p.set("charger_type", filters.charger_type);
  if (filters.location) p.set("location", filters.location);
  if (filters.company) p.set("company", filters.company);
  if (filters.q) p.set("q", filters.q);
  return p;
}

async function loadAll() {
  await Promise.all([loadSummary(), loadSessions()]);
}

async function loadSummary() {
  const kpiEl = root.querySelector("#histKpis");
  try {
    const p = buildParams();
    const s = await api(`/api/historical/summary?${p.toString()}`);
    currentSummary = s;

    if (!s.has_data) {
      kpiEl.innerHTML = `
        <div class="kpi" style="grid-column:1/-1">
          <span class="lbl">Historical EV Charging Sessions</span>
          <span class="val off">NO HISTORICAL DATA</span>
          <span class="sub">No IEEE charging records have been imported yet into this workspace.</span>
          <div class="toolbar" style="margin-top:10px">
            <a class="primary sm btn-link" href="#integrations">Go to Data Integrations to import</a>
          </div>
        </div>
      `;
      root.querySelector("#chartsRow").classList.add("hidden");
      root.querySelector("#locationsRow").classList.add("hidden");
      root.querySelector("#distributionRow").classList.add("hidden");
      root.querySelector("#histFilters").classList.add("hidden");
      return;
    }

    root.querySelector("#histFilters").classList.remove("hidden");
    root.querySelector("#chartsRow").classList.remove("hidden");
    root.querySelector("#locationsRow").classList.remove("hidden");
    root.querySelector("#distributionRow").classList.remove("hidden");

    if (s.filter_options) {
      updateFilterDropdowns(s.filter_options);
    }

    if (s.total_sessions === 0) {
      kpiEl.innerHTML = `
        <div class="kpi" style="grid-column:1/-1">
          <span class="lbl">Filtered Historical Records</span>
          <span class="val off">0 SESSIONS</span>
          <span class="sub">No records match the current filter selection. Try adjusting or resetting filters.</span>
        </div>
      `;
      renderCharts(s);
      return;
    }

    // 6 required KPIs
    kpiEl.innerHTML = `
      <div class="kpi">
        <span class="lbl">Total sessions</span>
        <span class="val">${fmt(s.total_sessions)}</span>
        <span class="sub">historical records</span>
      </div>
      <div class="kpi">
        <span class="lbl">Unique EV users</span>
        <span class="val">${fmt(s.unique_users)}</span>
        <span class="sub">anonymised UserIDs</span>
      </div>
      <div class="kpi">
        <span class="lbl">Unique chargers</span>
        <span class="val">${fmt(s.unique_chargers)}</span>
        <span class="sub">distinct charging stations</span>
      </div>
      <div class="kpi">
        <span class="lbl">Average duration</span>
        <span class="val">${fmt(s.avg_duration)}</span>
        <span class="sub">${esc(s.duration_unit_note || "original duration units")}</span>
      </div>
      <div class="kpi">
        <span class="lbl">Average demand</span>
        <span class="val">${fmt(s.avg_demand)}</span>
        <span class="sub">${esc(s.demand_unit_note || "original units (commonly kWh)")}</span>
      </div>
      <div class="kpi">
        <span class="lbl">Total recorded demand</span>
        <span class="val">${fmt(s.total_demand)}</span>
        <span class="sub">${esc(s.demand_unit_note || "original units (commonly kWh)")}</span>
      </div>
    `;

    renderCharts(s);
  } catch (err) {
    kpiEl.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function renderCharts(s) {
  // 1. Session arrival by hour of day (00:00 - 23:00)
  const hourlyEl = root.querySelector("#hourlyChart");
  if (s.hourly_distribution && s.hourly_distribution.some(v => v > 0)) {
    const labels = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, "0")}:00`);
    hourlyEl.innerHTML = barChart({
      labels,
      series: [{ name: "Arrivals", color: cat(0), values: s.hourly_distribution }],
      height: 220,
      unit: " sessions",
    });
  } else {
    hourlyEl.innerHTML = chartEmpty("No timestamp hours recorded for this selection");
  }

  // 2. Charger type distribution
  const typeEl = root.querySelector("#typeChart");
  if (s.charger_type_breakdown && Object.keys(s.charger_type_breakdown).length > 0) {
    const items = Object.entries(s.charger_type_breakdown).map(([label, value], i) => ({
      label: `Type ${label}`,
      value: value || 0,
      color: cat(i),
    }));
    typeEl.innerHTML = hBars({ items, fmtV: (v) => fmt(v, 0), unit: " sessions" });
  } else {
    typeEl.innerHTML = chartEmpty("No charger type data available");
  }

  // 3. Top charging locations
  const locEl = root.querySelector("#locChart");
  if (s.top_locations && Object.keys(s.top_locations).length > 0) {
    const items = Object.entries(s.top_locations).map(([label, value], i) => ({
      label,
      value: value || 0,
      color: cat(i),
    }));
    locEl.innerHTML = hBars({ items, fmtV: (v) => fmt(v, 0), unit: " sessions" });
  } else {
    locEl.innerHTML = chartEmpty("No location data recorded");
  }

  // 4. Charging network / company distribution
  const compEl = root.querySelector("#compChart");
  if (s.company_breakdown && Object.keys(s.company_breakdown).length > 0) {
    const items = Object.entries(s.company_breakdown).map(([label, value], i) => ({
      label: `Company ${label}`,
      value: value || 0,
      color: cat(i + 2),
    }));
    compEl.innerHTML = hBars({ items, fmtV: (v) => fmt(v, 0), unit: " sessions" });
  } else {
    compEl.innerHTML = chartEmpty("No company data recorded");
  }

  // 5. Session duration distribution
  const durEl = root.querySelector("#durationChart");
  if (s.duration_distribution && Object.keys(s.duration_distribution).length > 0 && Object.values(s.duration_distribution).some(v => v > 0)) {
    const items = Object.entries(s.duration_distribution).map(([label, value], i) => ({
      label,
      value: value || 0,
      color: cat(i),
    }));
    durEl.innerHTML = hBars({ items, fmtV: (v) => fmt(v, 0), unit: " sessions" });
  } else {
    durEl.innerHTML = chartEmpty("No duration data recorded");
  }

  // 6. Charging demand distribution
  const demEl = root.querySelector("#demandChart");
  if (s.demand_distribution && Object.keys(s.demand_distribution).length > 0 && Object.values(s.demand_distribution).some(v => v > 0)) {
    const items = Object.entries(s.demand_distribution).map(([label, value], i) => ({
      label,
      value: value || 0,
      color: cat(i + 4),
    }));
    demEl.innerHTML = hBars({ items, fmtV: (v) => fmt(v, 0), unit: " sessions" });
  } else {
    demEl.innerHTML = chartEmpty("No demand data recorded");
  }
}

async function loadSessions() {
  const tblEl = root.querySelector("#histTable");
  const countHint = root.querySelector("#countHint");
  tblEl.innerHTML = '<div class="loading">Loading sessions…</div>';

  try {
    const p = buildParams();
    p.set("limit", String(queryParams.limit));
    p.set("offset", String(queryParams.offset));

    const res = await api(`/api/historical/sessions?${p.toString()}`);
    currentSessions = res.sessions || [];
    totalRecords = res.total || 0;

    countHint.textContent = `${fmt(totalRecords)} sessions found`;

    if (!res.sessions || res.sessions.length === 0) {
      tblEl.innerHTML = `
        <div class="muted small" style="padding:24px;text-align:center">
          ${filters.q || filters.location || filters.charger_type || filters.company || filters.date_from || filters.date_to
            ? "No historical records match your filter criteria."
            : "No historical charging session records in this workspace."}
        </div>
      `;
      root.querySelector("#histPagination").innerHTML = "";
      return;
    }

    tblEl.innerHTML = `
      <table class="tbl">
        <thead>
          <tr>
            <th>User ID</th>
            <th>Charger ID</th>
            <th>Company</th>
            <th>Location</th>
            <th>Type</th>
            <th>Start Time</th>
            <th>End Time</th>
            <th>Duration</th>
            <th>Demand</th>
            <th>Dataset</th>
          </tr>
        </thead>
        <tbody>
          ${res.sessions.map(r => `
            <tr>
              <td class="mono small">${esc(r.user_id || "—")}</td>
              <td class="mono small"><b>${esc(r.charger_id)}</b></td>
              <td>${esc(r.charger_company || "—")}</td>
              <td class="small" title="${esc(r.location || "")}">${esc(r.location || "—")}</td>
              <td><span class="chip sm">${esc(r.charger_type || "Standard")}</span></td>
              <td class="small mono">${r.start_datetime ? dt(r.start_datetime) : esc(r.start_day || "—")}</td>
              <td class="small mono">${r.end_datetime ? dt(r.end_datetime) : esc(r.end_day || "—")}</td>
              <td class="num">${r.duration !== null ? fmt(r.duration) : esc(r.duration_raw || "—")}</td>
              <td class="num"><b>${r.demand !== null ? fmt(r.demand) : esc(r.demand_raw || "—")}</b></td>
              <td class="small muted">${esc(r.dataset_label || "IEEE")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;

    renderPagination();
  } catch (err) {
    tblEl.innerHTML = `<div class="error-box">${esc(err.message)}</div>`;
  }
}

function renderPagination() {
  const pEl = root.querySelector("#histPagination");
  const totalPages = Math.ceil(totalRecords / queryParams.limit);
  const currentPage = Math.floor(queryParams.offset / queryParams.limit) + 1;

  pEl.innerHTML = `
    <span class="small muted">Page ${currentPage} of ${totalPages || 1} (${fmt(totalRecords)} records)</span>
    <div class="toolbar" style="gap:6px">
      <button class="sm" id="prevPage" ${currentPage <= 1 ? "disabled" : ""}>Previous</button>
      <button class="sm" id="nextPage" ${currentPage >= totalPages ? "disabled" : ""}>Next</button>
    </div>
  `;

  pEl.querySelector("#prevPage").onclick = () => {
    if (queryParams.offset >= queryParams.limit) {
      queryParams.offset -= queryParams.limit;
      loadSessions();
    }
  };

  pEl.querySelector("#nextPage").onclick = () => {
    if (currentPage < totalPages) {
      queryParams.offset += queryParams.limit;
      loadSessions();
    }
  };
}
