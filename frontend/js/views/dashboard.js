// Operations console: KPIs, uptime trend, status mix, alerts, selected charger,
// charger table with filters, site health, unified timeline and demo scenarios.
// All numbers come from the backend; nothing is computed or invented here.
import { api } from "../api.js";
import { esc, fmt, fmtMin, fmtPct, dt, ago, timeOnly, tag, statusPill, ticketPill, sev, errorBox, toast, confirmBox, STATUS_LABEL } from "../ui.js";
import { lineChart, donut, hBars, sparkline, chartEmpty, chartError, cat, STATUS_COLOR } from "../charts.js";
import { openCharger, openTicket } from "../drawers.js";
import { openRaiseTicket } from "../raise_ticket.js";
import { switchWorkspace } from "../app.js";

let root, ctx, busy = false;
const filters = { site: "", status: "", connector: "", severity: "", ticket_state: "", q: "" };
let range = 30;
let catalogCache = null, scenarioCache = null, optionsFilled = false;

const SRC_TAG = {
  simulation: "SIMULATED",
  simulated_imported: "SIMULATED",
  operator_import: "IMPORTED",
  station_inventory: "IMPORTED",
  historical_sessions: "HISTORICAL",
  operator_api: "LIVE",
  ocpp: "LIVE"
};
const RANGES = [[30, "30 min"], [120, "2 h"], [360, "6 h"]];

export async function render(el, c) {
  root = el; ctx = c; optionsFilled = false;
  const sim = c.state.source === "simulation";
  [catalogCache, scenarioCache] = await Promise.all([api("/api/catalog"), sim ? api("/api/demo/scenarios") : Promise.resolve(null)]);
  el.innerHTML = `
    <div id="consoleBar"></div>
    <section class="kpis console" id="kpis" aria-label="Fleet KPIs"><div class="loading">Loading…</div></section>
    <div class="grid-3">
      <section class="panel"><div class="panel-h"><h2>Fleet uptime <span class="hint" id="upHint"></span></h2>
        <div class="seg sm" role="group" aria-label="Time range">${RANGES.map(([m, l]) => `<button data-range="${m}" aria-pressed="${m === range}">${l}</button>`).join("")}</div></div>
        <div class="panel-b chart-box" id="upChart"></div>
        <div class="panel-b small muted" style="padding-top:0" id="upDef"></div></section>
      <section class="panel"><div class="panel-h"><h2>Status distribution</h2><span class="hint">now</span></div><div class="panel-b" id="donut"></div></section>
    </div>
    <div class="grid-3">
      <section class="panel"><div class="panel-h"><h2>Critical alerts</h2><div class="toolbar"><span class="hint">ranked by the priority engine · <a href="#tickets">all tickets →</a></span><button class="sm primary" id="raiseDash">Raise a ticket</button></div></div>
        <div class="panel-b" id="dispatchBox" style="padding-bottom:0"></div>
        <div class="panel-b flush" id="alerts"></div></section>
      <section class="panel sel-panel"><div class="panel-h"><h2 id="selTitle">Selected charger</h2><span class="hint">click any charger row</span></div><div class="panel-b" id="selBox"></div></section>
    </div>
    <div class="grid-3">
      <section class="panel"><div class="panel-h"><h2>Chargers</h2>
        <div class="toolbar" id="filters">
          <select data-f="site" aria-label="Site"><option value="">All sites</option></select>
          <select data-f="status" aria-label="Status"><option value="">Any status</option>${Object.keys(STATUS_LABEL).map(s => `<option value="${s}">${STATUS_LABEL[s]}</option>`).join("")}</select>
          <select data-f="connector" aria-label="Connector"><option value="">Any connector</option></select>
          <select data-f="severity" aria-label="Open fault severity"><option value="">Any severity</option><option value="critical">Critical fault</option><option value="major">Major fault</option><option value="minor">Minor fault</option></select>
          <select data-f="ticket_state" aria-label="Ticket state"><option value="">Any ticket state</option>${(catalogCache.workflow || []).map(s => `<option value="${s}">${catalogCache.workflow_labels[s]}</option>`).join("")}</select>
          <input type="text" data-f="q" placeholder="Charger ID" aria-label="Search charger" style="width:120px">
          <button class="sm ghost" id="clearF">Clear</button>
        </div></div>
        <div class="panel-b flush tbl-wrap scroll-y" id="chargers"></div></section>
      <section class="panel"><div class="panel-h"><h2>Site health</h2><span class="hint">availability now · open tickets · power</span></div><div class="panel-b" id="sites"></div></section>
    </div>
    <div class="grid-3">
      <section class="panel"><div class="panel-h"><h2>Recent faults, events and ticket activity</h2><span class="hint">sessions hidden here</span><a class="hint" href="#simulator">full timeline →</a></div>
        <div class="panel-b flush" id="timeline"></div></section>
      <section class="panel" id="scnPanel"><div class="panel-h"><h2>Demo mode · quick scenarios</h2>${tag("SIMULATED")}</div><div class="panel-b" id="scenarios"></div></section>
    </div>`;
  el.querySelectorAll("[data-f]").forEach(inp => {
    inp.value = filters[inp.dataset.f] || "";
    inp.addEventListener(inp.tagName === "INPUT" ? "input" : "change", () => { filters[inp.dataset.f] = inp.value; loadChargers(); });
  });
  el.querySelector("#clearF").onclick = () => { Object.keys(filters).forEach(k => filters[k] = ""); el.querySelectorAll("[data-f]").forEach(i => i.value = ""); loadChargers(); };
  el.querySelectorAll("[data-range]").forEach(b => b.onclick = () => {
    range = Number(b.dataset.range);
    el.querySelectorAll("[data-range]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    refresh();
  });
  el.querySelector("#raiseDash").onclick = () => openRaiseTicket({ chargerId: ctx.state.dashSel || null, onDone: () => refresh() });
  renderScenarios();
  await refresh();
}

export async function refresh() {
  if (busy || !ctx.alive()) return;
  busy = true;
  const src = ctx.state.source;
  try {
    const [sum, ts, open, tl, srcStatus] = await Promise.all([
      api(`/api/fleet/summary?source=${src}`),
      api(`/api/analytics/timeseries?minutes=${range}&source=${src}`),
      api(`/api/tickets?source=${src}&open_only=true&sort=priority`),
      api(`/api/timeline?source=${src}&limit=14&operational=true`),
      src === "simulation" ? Promise.resolve(null) : api("/api/sources/status"),
    ]);
    if (!ctx.alive()) return;
    renderBar(sum, srcStatus ? srcStatus.sources[src] : null);
    if (src === "simulation" && !sum.total_chargers) {
      renderNoFleet();                      // v9.2: an honest empty state instead of zero KPIs and 0 % charts
    } else if (src !== "simulation" && !sum.total_chargers) {
      const m = srcStatus.sources[src];
      root.querySelector("#kpis").innerHTML = `<div class="kpi" style="grid-column:1/-1"><span class="lbl">${esc(m.mode)}</span><span class="val off">${esc(m.state)}</span>
        <span class="sub">${esc(m.detail || "")} ${m.next_action ? "Next: " + esc(m.next_action) : ""}</span>
        <span class="sub">No charger records exist for this mode, so no KPIs are shown. Nothing from the simulated fleet is substituted.</span></div>`;
    } else renderKpis(sum, ts);
    renderUptime(ts);
    renderDonut(sum);
    renderAlerts(open, sum.as_of, !sum.total_chargers);
    renderSites(sum);
    renderTimeline(tl);
    if (!optionsFilled) fillOptions(sum);
    await Promise.all([loadChargers(open), renderSelected(open)]);
  } catch (e) {
    root.querySelector("#kpis").innerHTML = errorBox(e.message);
    root.querySelector("[data-retry]")?.addEventListener("click", refresh);
  } finally { busy = false; }
}

// ------------------------------------------------------------------ console bar
const MODE_PILL = { "RECEIVING DATA": "st-available", "RECEIVING TEST DATA": "st-unavailable", "IMPORTED": "st-available", "STALE": "st-unavailable",
  "WAITING FOR DATA": "st-unavailable", "NO LIVE DATA": "st-offline", "NO IMPORTED DATA": "st-offline", "NOT CONNECTED": "st-offline",
  "NOT CONFIGURED": "st-offline", "ERROR": "st-faulted" };

function renderBar(sum, mode) {
  const bar = root.querySelector("#consoleBar");
  if (sum.source !== "simulation") {
    const m = mode || {};
    const typeTag = sum.test_traffic_chargers && !sum.real_chargers ? tag("SIMULATED OCPP") : tag(m.type || SRC_TAG[sum.source]);
    bar.innerHTML = `<div class="console-bar"><div class="scn">${typeTag}
        <span>Mode: <b>${esc(m.mode || sum.source)}</b></span>
        <span class="pill ${MODE_PILL[m.state] || "st-offline"}">${esc(m.state || "UNKNOWN")}</span>
        <span class="muted">${esc(m.detail || "")}</span>
        <span class="muted">${m.last_update ? "last update " + dt(m.last_update) : "no data received yet"}</span></div>
      <div class="small">${m.next_action ? `<span style="color:var(--warn)">Next: ${esc(m.next_action)}</span> · ` : ""}<a href="#integrations">Data integrations</a></div></div>
      ${sum.total_chargers ? `<div class="note" style="margin-top:8px">Showing ${esc(sum.source === "operator_import" ? "IMPORTED" : sum.source === "ocpp" ? "OCPP" : sum.source === "station_inventory" ? "IMPORTED STATION INVENTORY" : sum.source === "historical_sessions" ? "HISTORICAL RESEARCH" : sum.source === "simulated_imported" ? "SIMULATED IMPORTED STATIONS" : "OPERATOR API")} records only${sum.test_traffic_chargers ? ` (${sum.test_traffic_chargers} charger(s) are SIMULATED OCPP test traffic from the local simulator)` : ""}. ${sum.note || "Simulated fleet data is never mixed in."}</div>` : ""}`;
    return;
  }
  const st = ctx.state.sim || {};
  const org = ctx.state.me.organisation || {};
  if (!sum.total_chargers) {
    bar.innerHTML = `<div class="console-bar"><div class="scn">${tag("SIMULATED")}<span><b>No simulated chargers in this workspace.</b></span>
      <span class="pill st-offline">Simulator idle</span><span class="muted">Nothing is being simulated, so no uptime, energy or ticket figures are shown.</span></div></div>`;
    return;
  }
  const demoWs = org.is_system_demo || org.workspace_kind === "presentation" || org.demo_workspace_ready;
  const sc = st.active_scenario;
  const stateTxt = st.running ? '<span class="pill st-available">Running</span>' : '<span class="pill st-offline">Paused</span>';
  bar.innerHTML = `<div class="console-bar">
    <div class="scn">${tag("SIMULATED")}
      <span>Scenario: <b>${esc(sc ? sc.title : st.demo ? "Demo dataset (baseline)" : "Manual")}</b></span>
      <span class="muted">source: simulator</span>
      ${sc ? `<span class="muted">started ${dt(sc.started_sim_time)} (sim time)</span>` : st.demo ? `<span class="muted">history from ${dt(st.demo.history_start)}</span>` : ""}
      ${stateTxt}<span class="muted mono">${dt(st.sim_time)} · ×${st.speed} speed</span></div>
    <div class="toolbar">
      <button class="sm primary" id="cStart">${st.step ? "Resume" : "Start"}</button>
      <button class="sm" id="cPause">Pause</button>
      <button class="sm" id="cStep" title="Advance 4 steps = 1 simulated minute">Step 1 min</button>
      <button class="sm" id="cStep15" title="Advance 60 steps = 15 simulated minutes">Step 15 min</button>
      ${demoWs ? '<button class="sm danger" id="cRestore">Reset / restore demo</button>' : ""}
    </div></div>`;
  const act = async (url, body, msg) => {
    try { await api(url, { method: "POST", body: body || {} }); toast(msg); await ctx.state.refreshChip?.(); refresh(); }
    catch (e) { toast(e.message, true); }
  };
  bar.querySelector("#cStart").onclick = () => act("/api/sim/start", null, "Simulation running");
  bar.querySelector("#cPause").onclick = () => act("/api/sim/pause", null, "Simulation paused");
  bar.querySelector("#cStep").onclick = () => act("/api/sim/step", { n: 4 }, "Advanced 1 simulated minute");
  bar.querySelector("#cStep15").onclick = () => act("/api/sim/step", { n: 60 }, "Advanced 15 simulated minutes");
  bar.querySelector("#cRestore")?.addEventListener("click", restoreDemo);
}

function renderNoFleet() {
  const inPres = (ctx.state.me.organisation || {}).workspace_kind === "presentation";
  root.querySelector("#kpis").innerHTML = `<div class="empty-ws" style="grid-column:1/-1">
    <h3>${inPres ? "The Presentation Demo has no chargers yet" : "Your workspace has no chargers yet"}</h3>
    <p>${inPres ? "Restore the demo dataset to rebuild the SIMULATED sample fleet."
      : "My Fleet only shows your own records, and nothing synthetic is added automatically. Add your data, or open the separate Presentation Demo to explore ChargeSight with a ready-made SIMULATED fleet."}</p>
    <div class="toolbar">${inPres ? '<button class="primary sm" id="emRestore">Restore demo dataset</button>'
      : `<button class="primary sm" id="emPres">Open Presentation Demo</button>
         <a class="btn-link" href="#integrations">Import an authorised CSV</a>
         <a class="btn-link" href="#integrations">Configure operator API or OCPP</a>`}</div>
    <p class="small muted">Operator API and OCPP connections need the operator's authorisation and credentials or equipment. Until then they show Not configured.</p></div>`;
  root.querySelector("#emPres")?.addEventListener("click", (e) => switchWorkspace("presentation", e.currentTarget));
  root.querySelector("#emRestore")?.addEventListener("click", restoreDemo);
}

async function restoreDemo() {
  const ok = await confirmBox({
    title: "Reset and restore the demo dataset?",
    body: `This rebuilds the SIMULATED demo: 40 chargers, 6 hours of history and 8 scripted tickets, starting at 08:00 IST.
      <ul><li>Deleted: all simulated events, tickets, telemetry history and [DEMO] sample runs/sites.</li>
      <li>Kept: imported and live data, your own experiment runs and investment sites.</li>
      <li>The simulator is left paused. Takes a few seconds.</li></ul>`,
    confirmLabel: "Restore demo", danger: true, requireText: "RESET",
  });
  if (!ok) return;
  toast("Rebuilding demo dataset…");
  try {
    const r = await api("/api/demo/restore", { method: "POST", body: { confirm: "RESET" } });
    toast(r.message);
    ctx.state.dashSel = null;
    ctx.rerender();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ KPIs
function renderKpis(s, ts) {
  const m = ts && ts.supported && ts.summary ? ts.summary : null;
  const ch = m ? m.uptime_change : null;
  const trend = ch === null || ch === undefined ? "" : `<span class="trend ${ch > 0 ? "up" : ch < 0 ? "down" : "flat"}">${ch > 0 ? "▲" : ch < 0 ? "▼" : "■"} ${fmt(Math.abs(ch), 1)} pts in ${RANGES.find(r => r[0] === range)[1]}</span>`;
  const tgt = ts && ts.target_uptime_pct;
  const upCls = s.availability_pct === null ? "" : (tgt && s.availability_pct < tgt ? (s.availability_pct < 90 ? "crit" : "warn") : "ok");
  const tm = s.ticket_metrics || {};
  const k = [
    ["Fleet uptime", fmtPct(s.availability_pct, 1), trend || `${s.able_to_charge}/${s.total_chargers} able to charge`, upCls, "100 × (available + charging) / all chargers"],
    ["Charging", s.status_counts.charging, "active sessions", "charge"],
    ["Available", s.status_counts.available, "idle, ready", "ok"],
    ["Faulted", s.status_counts.faulted, "reported a fault", s.status_counts.faulted ? "crit" : ""],
    ["Offline", s.status_counts.offline, "missed 3 heartbeats", s.status_counts.offline ? "off" : ""],
    ["Open tickets", s.open_tickets, `${s.assigned_or_active_tickets ?? 0} with a technician`, ""],
    ["Critical open", s.unresolved_high_severity, "charger cannot charge", s.unresolved_high_severity ? "crit" : ""],
    ["Overdue", s.overdue_tickets, "past severity SLA (assumption)", s.overdue_tickets ? "warn" : ""],
    ["Energy delivered", m ? `${fmt(m.energy_kwh, 0)} kWh` : `${fmt(s.energy_kwh_total, 0)} kWh`, m ? `in window · ${fmt(s.energy_kwh_total, 0)} kWh total` : "total recorded", ""],
    ["Sessions", m ? m.sessions : (s.sessions_total ?? "—"), m ? `started in window · ${s.sessions_total} total` : "total started", ""],
    ["Aggregate power", `${fmt(s.power_kw_now, 0)} kW`, m && m.peak_power_kw !== null ? `peak ${fmt(m.peak_power_kw, 0)} kW in window` : "instantaneous", "charge"],
    ["MTTA / MTTR", `${fmtMin(s.mtta_min)} / ${fmtMin(s.mttr_min)}`, `n = ${tm.mtta_n ?? 0} / ${tm.mttr_n ?? 0} tickets`, "", `MTTA = ${tm.definitions?.mtta_min || ""}\nMTTR = ${tm.definitions?.mttr_min || ""}`],
  ];
  root.querySelector("#kpis").innerHTML = k.map(([l, v, sub, cls, def]) => `<div class="kpi" ${def ? `data-tip="${esc(l + "\n" + def)}"` : ""}><span class="lbl">${esc(l)}</span><span class="val ${cls}">${esc(v)}</span><span class="sub">${sub}</span></div>`).join("");
}

// ------------------------------------------------------------------ charts
function hhmm(iso) { return new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false }); }

function renderUptime(ts) {
  const box = root.querySelector("#upChart");
  root.querySelector("#upDef").textContent = ts.uptime_definition ? `${ts.uptime_definition} Target line: ${ts.target_uptime_pct}% (${ts.target_label}).` : "";
  if (!ts.supported) { box.innerHTML = chartEmpty(ts.reason); root.querySelector("#upHint").innerHTML = ""; return; }
  if (!ts.points.length) { box.innerHTML = chartEmpty(ts.reason || "No history yet."); return; }
  const pts = ts.points;
  root.querySelector("#upHint").innerHTML = `${tag(SRC_TAG[ts.source])} mean ${fmtPct(ts.summary.uptime_mean, 1)} · ${ts.summary.minutes_below_target} of ${pts.length} samples below target`;
  const lo = Math.min(...pts.map(p => p.uptime_pct), ts.target_uptime_pct);
  box.innerHTML = lineChart({
    labels: pts.map(p => hhmm(p.ts)), tipLabels: pts.map(p => `${dt(p.ts)} (sim)`),
    series: [{ name: "Fleet uptime", color: cat(0), values: pts.map(p => p.uptime_pct), area: true }],
    yMin: Math.max(0, Math.floor((lo - 3) / 5) * 5), yMax: 100, unit: "%", yFmt: v => `${fmt(v, 0)}%`,
    target: { value: ts.target_uptime_pct, label: `Target ${ts.target_uptime_pct}%` }, height: 230, width: 1000,
  });
}

function renderDonut(s) {
  const items = Object.keys(STATUS_LABEL).map(k => ({ label: STATUS_LABEL[k], value: s.status_counts[k] || 0, color: STATUS_COLOR[k] }));
  root.querySelector("#donut").innerHTML = donut({ items, center: fmtPct(s.availability_pct, 0), sub: "able to charge" });
}

// ------------------------------------------------------------------ alerts
function renderAlerts(open, now, noChargers = false) {
  const el = root.querySelector("#alerts");
  const urgent = open.filter(t => t.dispatch_needed);
  const dbox = root.querySelector("#dispatchBox");
  dbox.innerHTML = urgent.length ? `<div class="dispatch-alert" role="alert"><b>${urgent.length} urgent ticket${urgent.length === 1 ? "" : "s"} with no eligible technician (dispatch needed)</b>
    <div class="small" style="margin-top:4px">${urgent.slice(0, 4).map(t => `<a href="#" data-code="${esc(t.code)}" class="mono">${esc(t.code)}</a> ${esc(t.fault_label)} · ${esc(t.charger_id)}`).join(" · ")}</div>
    <div class="small muted" style="margin-top:4px">${esc(urgent[0].assignment_note || "")} <a href="#technicians">Technicians →</a></div></div>` : "";
  dbox.querySelectorAll("[data-code]").forEach(a => a.onclick = (e) => { e.preventDefault(); openTicket(a.dataset.code, refresh); });
  const rows = open.filter(t => t.severity === "critical" || t.overdue || (t.priority && t.priority.rank <= 6)).slice(0, 8);
  if (!rows.length) { el.innerHTML = `<div class="empty">${noChargers ? "No chargers in this view, so there are no alerts." : "No open alerts. Every charger is clear."}</div>`; return; }
  el.innerHTML = rows.map(t => `<div class="alert" data-code="${esc(t.code)}" tabindex="0" role="button">
      <span class="rail ${esc(t.severity)}"></span>
      <div><div><b class="mono">${esc(t.code)}</b> · ${esc(t.fault_label)} · <span class="mono">${esc(t.charger_id)}</span> <span class="muted">${esc(t.site_name || "")}</span> ${t.overdue ? '<span class="tag tag-ESTIMATED">OVERDUE</span>' : ""}${t.origin === "user_reported" ? ` ${tag("USER_REPORTED")}` : ""}${t.dispatch_needed ? ' <span class="tag tag-MISSING">DISPATCH</span>' : ""}</div>
        <div class="act">→ ${esc(t.recommended_action)}</div></div>
      <div class="meta">${sev(t.severity)} · rank ${t.priority ? t.priority.rank : "—"} (${t.priority ? t.priority.score.toFixed(2) : "—"})<br>age ${fmtMin(t.age_min)} · ${ticketPill(t.status)}</div></div>`).join("");
  el.querySelectorAll(".alert").forEach(a => {
    const go = () => openTicket(a.dataset.code, refresh);
    a.onclick = go; a.onkeydown = e => { if (e.key === "Enter") go(); };
  });
}

// ------------------------------------------------------------------ chargers table
function fillOptions(s) {
  const siteSel = root.querySelector('[data-f="site"]');
  s.sites.forEach(x => siteSel.insertAdjacentHTML("beforeend", `<option value="${esc(x.code)}">${esc(x.name)}</option>`));
  siteSel.value = filters.site;
  optionsFilled = true;
}

async function loadChargers(openTickets) {
  const p = new URLSearchParams({ source: ctx.state.source });
  Object.entries(filters).forEach(([k, v]) => v && p.set(k, v));
  let rows;
  try { rows = await api(`/api/chargers?${p}`); }
  catch (e) { root.querySelector("#chargers").innerHTML = errorBox(e.message); return; }
  if (!ctx.alive()) return;
  const conn = root.querySelector('[data-f="connector"]');
  if (conn.options.length <= 1 && rows.length) {
    [...new Set(rows.map(r => r.connector_type))].sort().forEach(t => conn.insertAdjacentHTML("beforeend", `<option>${esc(t)}</option>`));
    conn.value = filters.connector;
  }
  const now = ctx.state.source === "simulation" ? ctx.state.sim?.sim_time : new Date().toISOString();
  const sel = ctx.state.dashSel;
  const active = Object.values(filters).some(Boolean);
  root.querySelector("#chargers").innerHTML = rows.length ? `<table class="t"><thead><tr><th>Charger</th><th>Site</th><th>Status</th><th class="r">Power</th><th class="r">Temp</th><th>Fault code</th><th>Ticket</th><th>Heartbeat</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>
    ${rows.map(r => `<tr class="click ${r.id === sel ? "sel" : ""}" data-id="${esc(r.id)}" tabindex="0"><td class="mono" style="white-space:nowrap"><b>${esc(r.id)}</b><div class="small muted">${esc(r.connector_type)} · ${fmt(r.power_kw, 1)} kW</div></td>
      <td class="small">${esc(r.site_name)}</td><td>${statusPill(r.status)}</td>
      <td class="r num">${r.status === "charging" ? `${fmt(r.power_now_kw, 1)} kW` : '<span class="muted">0</span>'}</td>
      <td class="r num ${r.temp_c > 50 ? "sev-critical" : ""}">${r.temp_c === null || r.status === "offline" ? '<span class="muted">—</span>' : `${fmt(r.temp_c, 1)} °C`}</td>
      <td class="mono small">${esc(r.fault_code || "")}${r.fault_code ? "" : '<span class="muted">—</span>'}</td>
      <td class="mono small">${r.open_tickets.map(esc).join(", ") || '<span class="muted">—</span>'}</td>
      <td class="small muted">${r.source === "simulation" ? ago(r.last_heartbeat_at, now) : dt(r.last_heartbeat_at)}</td>
      <td><button class="sm ghost" data-report="${esc(r.id)}" title="Raise a ticket for ${esc(r.id)}" aria-label="Raise a ticket for ${esc(r.id)}">Report</button></td></tr>`).join("")}
    </tbody></table><div class="pager">${rows.length} charger${rows.length === 1 ? "" : "s"}${active ? " match the filters" : ""}</div>`
    : `<div class="empty">${active ? "No chargers match these filters. Use Clear to reset them." : "No chargers for this data source yet."}</div>`;
  root.querySelectorAll("#chargers [data-report]").forEach(b => b.onclick = (e) => { e.stopPropagation(); openRaiseTicket({ chargerId: b.dataset.report, onDone: () => refresh() }); });
  root.querySelectorAll("#chargers tr[data-id]").forEach(tr => {
    const pick = () => { ctx.state.dashSel = tr.dataset.id; root.querySelectorAll("#chargers tr").forEach(x => x.classList.toggle("sel", x === tr)); renderSelected(); root.querySelector(".sel-panel").scrollIntoView({ behavior: "smooth", block: "nearest" }); };
    tr.onclick = pick; tr.onkeydown = e => { if (e.key === "Enter" && e.target === tr) pick(); };
  });
}

// ------------------------------------------------------------------ selected charger
async function renderSelected(openTickets) {
  const box = root.querySelector("#selBox");
  let id = ctx.state.dashSel;
  if (!id && openTickets && openTickets.length) id = ctx.state.dashSel = openTickets[0].charger_id;
  if (!id) { box.innerHTML = '<div class="empty">Select a charger in the table to see its live detail.</div>'; return; }
  let c, rd;
  try { [c, rd] = await Promise.all([api(`/api/chargers/${encodeURIComponent(id)}`), api(`/api/chargers/${encodeURIComponent(id)}/readings?hours=6`)]); }
  catch (e) { box.innerHTML = errorBox(e.message); ctx.state.dashSel = null; return; }
  if (!ctx.alive()) return;
  if (c.source !== ctx.state.source) { ctx.state.dashSel = null; box.innerHTML = '<div class="empty">Select a charger in the table to see its live detail.</div>'; return; }
  const now = c.source === "simulation" ? ctx.state.sim?.sim_time : new Date().toISOString();
  const cur = c.current_ticket;
  const pts = rd.points || [];
  root.querySelector("#selTitle").innerHTML = `<span class="mono">${esc(c.id)}</span> ${statusPill(c.status)} ${tag(c.label)}`;
  box.innerHTML = `
    <dl class="kv">
      <div><dt>Site</dt><dd>${esc(c.site_name)}</dd></div>
      <div><dt>Connector</dt><dd>${esc(c.connector_type)} · ${fmt(c.power_kw, 0)} kW</dd></div>
      <div><dt>Power now</dt><dd class="num">${fmt(c.power_now_kw, 1)} kW</dd></div>
      <div><dt>Temperature</dt><dd class="num ${c.temp_c > 50 ? "sev-critical" : ""}">${c.temp_c === null ? "—" : fmt(c.temp_c, 1) + " °C"}</dd></div>
      <div><dt>Last heartbeat</dt><dd>${ago(c.last_heartbeat_at, now)}</dd></div>
      <div><dt>Fault code</dt><dd class="mono">${esc(c.fault_code || "none")}</dd></div>
      <div><dt>Session</dt><dd>${c.status === "charging" ? `${fmt(c.session_kwh, 1)} kWh · since ${c.session_started_at ? hhmm(c.session_started_at) : "—"}` : "no active session"}</dd></div>
      <div><dt>Energy total</dt><dd class="num">${fmt(c.total_kwh, 0)} kWh</dd></div>
      <div><dt>Open ticket</dt><dd>${cur ? `<button class="sm" data-ticket="${esc(cur.code)}">${esc(cur.code)}</button>` : "none"}</dd></div>
    </dl>
    ${cur ? `<div class="note ${cur.severity === "critical" ? "crit" : "warn"}" style="margin-top:10px"><b>${esc(cur.fault_label)}</b> (${esc(cur.severity)}), ${ticketPill(cur.status)} — ${esc(cur.recommended_action)}</div>` : ""}
    <div style="margin-top:12px">
      ${rd.supported ? `<div class="small muted">Power, last 6 h (kW, every 5 min) ${tag(SRC_TAG[c.source])}</div>${sparkline(pts.map(p => p.power_kw), { color: cat(0), labels: pts.map(p => dt(p.ts)), unit: " kW" })}
      <div class="small muted" style="margin-top:6px">Connector temperature (°C) · ${esc(rd.temp_label || "")}</div>${sparkline(pts.map(p => p.temp_c), { color: cat(1), labels: pts.map(p => dt(p.ts)), unit: " °C" })}`
      : `<div class="small muted">${esc(rd.reason || "No readings.")}</div>`}
    </div>
    <div class="toolbar" style="margin-top:12px"><button class="sm" id="selMore">Full detail &amp; history</button><button class="sm primary" id="selRaise">Raise a ticket</button>
      ${c.source === "simulation" ? `<select id="selCat" aria-label="Fault type">${catalogCache.fault_types.map(f => `<option value="${f.key}">${esc(f.label)}</option>`).join("")}</select>
      <select id="selSev" aria-label="Severity"><option value="">catalogue severity</option><option value="critical">critical</option><option value="major">major</option><option value="minor">minor</option></select>
      <button class="sm danger" id="selInject">Inject fault</button>` : ""}</div>`;
  box.querySelector("#selMore").onclick = () => openCharger(c.id, refresh);
  box.querySelector("#selRaise").onclick = () => openRaiseTicket({ chargerId: c.id, onDone: () => refresh() });
  box.querySelectorAll("[data-ticket]").forEach(b => b.onclick = () => openTicket(b.dataset.ticket, refresh));
  const inj = box.querySelector("#selInject");
  if (inj) inj.onclick = async () => {
    const category = box.querySelector("#selCat").value, severity = box.querySelector("#selSev").value || null;
    try {
      const r = await api("/api/sim/inject", { method: "POST", body: { charger_id: c.id, category, severity } });
      toast(r.deduplicated ? `Not created: ${r.message}` : `SIMULATED fault injected — ${r.ticket} opened (${r.severity}).`, r.deduplicated);
      refresh();
    } catch (e) { toast(e.message, true); }
  };
}

// ------------------------------------------------------------------ sites
function renderSites(s) {
  const el = root.querySelector("#sites");
  if (!s.sites.length) { el.innerHTML = '<div class="empty">No sites for this data source.</div>'; return; }
  el.innerHTML = hBars({
    items: s.sites.map(x => ({
      label: x.name, value: x.availability_pct ?? 0,
      color: (x.availability_pct ?? 0) >= 97 ? "var(--ok)" : (x.availability_pct ?? 0) >= 85 ? "var(--warn)" : "var(--crit)",
      tip: [`${x.able_to_charge}/${x.chargers} able to charge`, `${x.open_tickets} open tickets (${x.critical_open} critical)`, `${fmt(x.power_kw, 0)} kW now`, `downtime this run ${fmt(x.downtime_h, 1)} h`],
    })),
    max: 100, fmtV: v => fmt(v, 0), unit: "%", target: { value: 97, label: "97% target (assumption)" },
  }) + `<table class="t" style="margin-top:10px"><thead><tr><th>Site</th><th class="r">Open</th><th class="r">Critical</th><th class="r">Power</th></tr></thead><tbody>
    ${s.sites.map(x => `<tr><td class="small">${esc(x.name)}</td><td class="r num">${x.open_tickets}</td><td class="r num ${x.critical_open ? "sev-critical" : ""}">${x.critical_open}</td><td class="r num">${fmt(x.power_kw, 0)} kW</td></tr>`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ timeline
function renderTimeline(tl) {
  const el = root.querySelector("#timeline");
  if (!tl.items.length) { el.innerHTML = '<div class="empty">No events yet for this source.</div>'; return; }
  el.innerHTML = tl.items.map(i => `<div class="tl-row"><span class="when">${timeOnly(i.ts).slice(0, 5)}</span>
      <span class="sv ${esc(i.severity)}">${esc(i.kind === "ticket" ? "ticket" : i.severity)}</span>
      <span class="what"><span class="mono">${esc(i.charger_id)}</span> ${esc(i.description)} ${i.ocpp_action ? `<span class="muted small">[${esc(i.ocpp_action)}]</span>` : ""}</span></div>`).join("")
    + (tl.note ? `<div class="pager">${esc(tl.note)}</div>` : "");
}

// ------------------------------------------------------------------ scenarios
function renderScenarios() {
  const el = root.querySelector("#scenarios");
  if (!el) return;
  if (!scenarioCache) { root.querySelector("#scnPanel").classList.add("hidden"); return; }
  const active = ctx.state.sim?.active_scenario?.key;
  el.innerHTML = `<div class="scn-grid">${scenarioCache.scenarios.map(s => `<button class="scn-btn ${s.key === active ? "active" : ""}" data-scn="${esc(s.key)}" data-tip="${esc(s.title + "\n" + s.changes.join("\n"))}"><b>${esc(s.title)}</b><span>${esc(s.summary)}</span></button>`).join("")}</div>
    <div class="small muted" style="margin-top:8px">Each scenario changes real simulator state (parameters, technician availability, injected faults). Use Pause/Step above to control it; "Reset / restore demo" returns to the baseline dataset.</div>`;
  el.querySelectorAll("[data-scn]").forEach(b => b.onclick = async () => {
    const s = scenarioCache.scenarios.find(x => x.key === b.dataset.scn);
    const ok = await confirmBox({ title: `Start “${s.title}”?`, body: `${esc(s.summary)}<ul>${s.changes.map(c => `<li>${esc(c)}</li>`).join("")}</ul>Applies to SIMULATED data only.`, confirmLabel: "Start scenario" });
    if (!ok) return;
    try {
      const r = await api(`/api/demo/scenarios/${s.key}`, { method: "POST", body: {} });
      toast(r.detail && r.detail.experiment_run_id ? `Policy comparison stored as run #${r.detail.experiment_run_id} (Repair policy lab).` : `Scenario started: ${r.title}`);
      await ctx.state.refreshChip?.();
      renderScenarios();
      refresh();
    } catch (e) { toast(e.message, true); }
  });
}
