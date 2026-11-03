// Analytics: every chart is drawn from stored history returned by the backend.
import { api } from "../api.js";
import { esc, fmt, fmtINR, fmtMin, dt, tag, errorBox } from "../ui.js";
import { lineChart, barChart, hBars, chartEmpty, chartError, cat, STATUS_COLOR, SEV_COLOR } from "../charts.js";

let root, ctx, busy = false;
let hours = 6, charger = "SIM-KRM-02", runId = null, invSite = "";
const SRC_TAG = { simulation: "SIMULATED", operator_import: "IMPORTED", operator_api: "LIVE", ocpp: "LIVE" };
const RANGES = [[1, "1 h"], [2, "2 h"], [6, "6 h"], [12, "12 h"]];

const hhmm = (iso) => new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
const bucketFor = (h) => (h <= 1 ? 5 : h <= 2 ? 10 : h <= 6 ? 30 : 60);

function panel(id, title, hint = "", wide = false) {
  return `<section class="panel" ${wide ? 'style="grid-column:1/-1"' : ""}><div class="panel-h"><h2>${title}</h2><span class="hint" id="${id}H">${hint}</span></div><div class="panel-b chart-box" id="${id}"><div class="loading">Loading…</div></div></section>`;
}

export async function render(el, c) {
  root = el; ctx = c;
  const src = c.state.source;
  const [chargers, runs, sites] = await Promise.all([
    api(`/api/chargers?source=${src}`), api("/api/experiments?kind=comparison"), api("/api/investment/sites"),
  ]);
  if (!chargers.find(x => x.id === charger)) charger = chargers.length ? chargers[0].id : null;
  if (runId && !runs.find(r => r.id === runId)) runId = null;
  el.innerHTML = `
    <div class="console-bar"><div class="scn">${tag(SRC_TAG[src])} Charts read persisted history (fleet snapshots each simulated minute, charger readings every 5 minutes, tickets and events). Hover any chart for exact values.</div>
      <div class="toolbar"><span class="small muted">Window</span><div class="seg sm" role="group" aria-label="Window">${RANGES.map(([h, l]) => `<button data-h="${h}" aria-pressed="${h === hours}">${l}</button>`).join("")}</div></div></div>
    <div class="grid-eq">
      ${panel("cStatus", "Charger status over time", "count of chargers per state")}
      ${panel("cFaults", "Fault frequency by type", "tickets opened per interval")}
      <section class="panel"><div class="panel-h"><h2>Charger power over time</h2>
        <select id="chSel" aria-label="Charger">${chargers.map(x => `<option value="${esc(x.id)}" ${x.id === charger ? "selected" : ""}>${esc(x.id)}</option>`).join("")}</select></div>
        <div class="panel-b chart-box" id="cPower"></div><div class="panel-b chart-box" style="padding-top:0" id="cChPower"></div></section>
      <section class="panel"><div class="panel-h"><h2>Energy and sessions</h2><span class="hint" id="cEnergyH"></span></div>
        <div class="panel-b chart-box" id="cEnergy"></div><div class="panel-b chart-box" style="padding-top:0" id="cSessions"></div></section>
      ${panel("cMtt", "MTTA and MTTR trend", "cumulative means, minutes")}
      <section class="panel"><div class="panel-h"><h2>Open-ticket age and backlog</h2><span class="hint" id="cAgeH"></span></div>
        <div class="panel-b chart-box" id="cAge"></div><div class="panel-b chart-box" style="padding-top:0" id="cBacklog"></div></section>
      ${panel("cSites", "Site uptime comparison", "availability over the window")}
      <section class="panel"><div class="panel-h"><h2>FIFO vs priority repair</h2>
        <select id="runSel" aria-label="Experiment run">${runs.map(r => `<option value="${r.id}" ${r.id === runId ? "selected" : ""}>#${r.id} ${esc(r.label)}</option>`).join("") || "<option value=''>no runs</option>"}</select></div>
        <div class="panel-b chart-box" id="cPolicy"></div></section>
      <section class="panel" style="grid-column:1/-1"><div class="panel-h"><h2>Investment cash flow — scenario comparison ${tag("ESTIMATED")} ${tag("NOT YET VALIDATED")}</h2>
        <select id="invSel" aria-label="Investment site"><option value="">Default example inputs</option>${sites.map(s => `<option value="${s.id}" ${String(s.id) === invSite ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>
        <div class="panel-b chart-box" id="cInv"></div></section>
    </div>`;
  el.querySelectorAll("[data-h]").forEach(b => b.onclick = () => {
    hours = Number(b.dataset.h);
    el.querySelectorAll("[data-h]").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    refresh();
  });
  el.querySelector("#chSel").onchange = (e) => { charger = e.target.value; drawChargerPower(); };
  el.querySelector("#runSel").onchange = (e) => { runId = Number(e.target.value) || null; drawPolicy(); };
  el.querySelector("#invSel").onchange = (e) => { invSite = e.target.value; drawInvestment(sites); };
  runId = runId || (runs[0] && runs[0].id) || null;
  drawPolicy();
  drawInvestment(sites);
  await refresh();
}

export async function refresh() {
  if (busy || !ctx.alive()) return;
  busy = true;
  const src = ctx.state.source, b = bucketFor(hours);
  try {
    const [ts, faults, maint, sites] = await Promise.all([
      api(`/api/analytics/timeseries?minutes=${hours * 60}&bucket_min=${b}&source=${src}`),
      api(`/api/analytics/faults?hours=${hours}&bucket_min=${b}&source=${src}`),
      api(`/api/analytics/maintenance?hours=${hours}&bucket_min=${b}&source=${src}`),
      api(`/api/analytics/sites?hours=${hours}&source=${src}`),
    ]);
    if (!ctx.alive()) return;
    drawStatus(ts); drawFleetPower(ts); drawEnergy(ts); drawFaults(faults); drawMaint(maint); drawSites(sites);
    await drawChargerPower();
  } catch (e) {
    ["cStatus", "cFaults", "cPower", "cEnergy", "cMtt", "cAge", "cSites"].forEach(id => { const n = root.querySelector("#" + id); if (n) n.innerHTML = chartError(e.message); });
  } finally { busy = false; }
}

function set(id, html) { const n = root.querySelector("#" + id); if (n) n.innerHTML = html; }

// 2. status distribution over time
function drawStatus(ts) {
  if (!ts.supported || !ts.points.length) { set("cStatus", chartEmpty(ts.reason || "No history yet.")); return; }
  const p = ts.points;
  set("cStatus", barChart({
    labels: p.map(x => hhmm(x.ts)), tipLabels: p.map(x => dt(x.ts)), stacked: true, height: 220, unit: "",
    series: ["available", "charging", "faulted", "offline", "unavailable"].filter(k => p.some(x => x[k]))
      .map(k => ({ name: k[0].toUpperCase() + k.slice(1), color: STATUS_COLOR[k], values: p.map(x => x[k]) })),
  }));
}

// 3. fault frequency
function drawFaults(f) {
  const b = f.buckets;
  if (!b.length) { set("cFaults", chartEmpty("No faults in this window.")); return; }
  const total = Object.values(f.totals_by_category).reduce((a, x) => a + x, 0);
  if (!total) { set("cFaultsH", ""); set("cFaults", chartEmpty("No faults recorded in this window for this data source.")); return; }
  set("cFaultsH", `${total} faults · critical ${f.totals_by_severity.critical}, major ${f.totals_by_severity.major}, minor ${f.totals_by_severity.minor}`);
  set("cFaults", barChart({
    labels: b.map(x => hhmm(x.start)), tipLabels: b.map(x => `${hhmm(x.start)}–${hhmm(x.end)} IST`), stacked: true, height: 220,
    series: f.categories.map((c, i) => ({ name: `${c.label} (${c.severity})`, color: cat(i), values: b.map(x => x.by_category[c.key]) })),
  }) + `<div class="small muted" style="margin-top:4px">${esc(f.definition)}</div>`);
}

// 4. power
function drawFleetPower(ts) {
  if (!ts.supported || !ts.points.length) { set("cPower", chartEmpty(ts.reason || "No history yet.")); return; }
  const p = ts.points;
  set("cPower", `<div class="small muted">Aggregate fleet power (kW)</div>` + lineChart({
    labels: p.map(x => hhmm(x.ts)), tipLabels: p.map(x => dt(x.ts)), height: 170, unit: "kW", zero: true,
    series: [{ name: "Fleet power", color: cat(0), values: p.map(x => x.power_kw), area: true }],
  }));
}
async function drawChargerPower() {
  if (!charger) { set("cChPower", chartEmpty("No chargers for this data source.")); return; }
  try {
    const r = await api(`/api/chargers/${encodeURIComponent(charger)}/readings?hours=${Math.min(hours, 12)}`);
    if (!ctx.alive()) return;
    if (!r.supported || !r.points.length) { set("cChPower", chartEmpty(r.reason || "No readings.")); return; }
    set("cChPower", `<div class="small muted">${esc(charger)} output (kW, rated ${fmt(r.rated_kw, 0)} kW) — sampled every 5 min</div>` + lineChart({
      labels: r.points.map(x => hhmm(x.ts)), tipLabels: r.points.map(x => `${dt(x.ts)} · ${x.status}${x.temp_c !== null ? ` · ${fmt(x.temp_c, 1)} °C` : ""}`),
      height: 150, unit: "kW", yMin: 0, yMax: Math.max(r.rated_kw, ...r.points.map(x => x.power_kw)),
      series: [{ name: `${charger} power`, color: cat(2), values: r.points.map(x => x.power_kw) }],
    }));
  } catch (e) { set("cChPower", chartError(e.message)); }
}

// 5. energy + sessions (two charts, never a dual axis)
function drawEnergy(ts) {
  if (!ts.supported || !ts.buckets || !ts.buckets.length) { set("cEnergy", chartEmpty(ts.reason || "No history yet.")); set("cSessions", ""); return; }
  const b = ts.buckets;
  set("cEnergyH", `${fmt(ts.summary.energy_kwh, 0)} kWh · ${ts.summary.sessions} sessions in window · ${ts.bucket_min}-min intervals`);
  const labels = b.map(x => hhmm(x.start)), tips = b.map(x => `${hhmm(x.start)}–${hhmm(x.end)} IST`);
  set("cEnergy", `<div class="small muted">Energy delivered per interval (kWh)</div>` + barChart({ labels, tipLabels: tips, height: 160, unit: "kWh", series: [{ name: "Energy", color: cat(0), values: b.map(x => x.energy_kwh) }] }));
  set("cSessions", `<div class="small muted">Sessions started per interval</div>` + barChart({ labels, tipLabels: tips, height: 140, series: [{ name: "Sessions", color: cat(1), values: b.map(x => x.sessions) }] }));
}

// 6. MTTA / MTTR
function drawMaint(m) {
  const t = m.trend;
  if (!t.length || t.every(x => x.mtta_min === null && x.mttr_min === null)) set("cMtt", chartEmpty("No assigned or resolved tickets yet."));
  else set("cMtt", lineChart({
    labels: t.map(x => hhmm(x.ts)), tipLabels: t.map(x => `${dt(x.ts)} · n = ${x.mtta_n} assigned / ${x.mttr_n} resolved`), height: 220, unit: "min", yMin: 0,
    valueFmt: v => fmtMin(v),
    series: [{ name: "MTTA", color: cat(0), values: t.map(x => x.mtta_min) }, { name: "MTTR", color: cat(1), values: t.map(x => x.mttr_min), dash: "7 4" }],
  }) + `<div class="small muted" style="margin-top:4px">MTTA: ${esc(m.definitions.mtta_min)} MTTR: ${esc(m.definitions.mttr_min)}</div>`);
  // 7. age + backlog
  set("cAgeH", `${m.open_now} open now`);
  const ah = m.age_histogram;
  set("cAge", `<div class="small muted">Open tickets by age</div>` + (m.open_now ? barChart({
    labels: ah.map(x => x.bucket), height: 150, stacked: true, tipLabels: ah.map(x => `${x.bucket}: ${x.tickets.join(", ") || "none"}`),
    series: ["critical", "major", "minor"].map(s => ({ name: s, color: SEV_COLOR[s], values: ah.map(x => x.by_severity[s]) })),
  }) : chartEmpty("No open tickets.")));
  set("cBacklog", `<div class="small muted">Backlog over time (unresolved tickets)</div>` + lineChart({
    labels: t.map(x => hhmm(x.ts)), tipLabels: t.map(x => dt(x.ts)), height: 150, yMin: 0,
    series: [{ name: "Backlog", color: cat(0), values: t.map(x => x.backlog) }, { name: "Critical", color: cat(1), values: t.map(x => x.backlog_critical) }, { name: "Overdue", color: cat(3), values: t.map(x => x.overdue), dash: "6 4" }],
  }));
}

// 8. sites
function drawSites(s) {
  if (!s.sites.length) { set("cSites", chartEmpty("No sites for this source.")); return; }
  const lo = Math.max(0, Math.floor(Math.min(...s.sites.map(x => x.window_availability_pct ?? 100), s.target_uptime_pct) / 10) * 10 - 10);
  set("cSites", hBars({
    items: [...s.sites].sort((a, b) => (a.window_availability_pct ?? 0) - (b.window_availability_pct ?? 0)).map(x => ({
      label: `${x.name}`, value: x.window_availability_pct ?? 0, min: lo,
      color: (x.window_availability_pct ?? 0) >= s.target_uptime_pct ? "var(--ok)" : "var(--warn)",
      tip: [`${x.chargers} chargers · now ${fmt(x.availability_now_pct, 0)}% able to charge`, `${x.faults_in_window} faults in window · ${x.open_tickets} open`, `downtime ${fmt(x.downtime_h, 1)} charger-h`],
    })),
    max: 100, fmtV: v => fmt(v, 1), unit: "%", target: { value: s.target_uptime_pct, label: `${s.target_uptime_pct}% target (assumption)` },
  }) + `<div class="small muted" style="margin-top:8px">${esc(s.definition)}</div>`);
}

// 9. FIFO vs priority
async function drawPolicy() {
  if (!runId) { set("cPolicy", chartEmpty("No experiment runs yet. Use the Repair policy lab or the 'Repair policy comparison' scenario.")); return; }
  try {
    const r = await api(`/api/experiments/${runId}`);
    if (!ctx.alive()) return;
    const s = r.results.summary, q = r.results.queueing;
    const keys = ["availability_pct", "weighted_downtime_h", "high_sev_wait_mean_min", "mttr_min"];
    const better = (k, m) => (k === "availability_pct" ? m.mean_diff > 0 : m.mean_diff < 0);
    const blocks = keys.map(k => {
      const m = s[k];
      const verdict = m.ci95_low === null || m.ci95_low === undefined ? "no CI" : m.significant ? (better(k, m) ? '<span class="sig-yes">priority better</span>' : '<span class="sig-worse">priority worse</span>') : '<span class="sig-no">no significant difference</span>';
      const mx = Math.max(m.mean_a, m.mean_b) * 1.1 || 1;
      return `<div><div class="small" style="margin-bottom:4px"><b>${esc(m.label)}</b> <span class="muted">(${esc(m.unit)})</span></div>${hBars({
        items: [{ label: "FIFO", value: m.mean_a, color: cat(1), tip: ["mean over seeds"] }, { label: "Priority", value: m.mean_b, color: cat(0), tip: ["mean over seeds"] }],
        max: mx, fmtV: v => fmt(v, v < 100 ? 1 : 0),
      })}<div class="small muted" style="margin-top:4px">diff ${fmt(m.mean_diff, 2)} · 95% CI ${m.ci95_low === null || m.ci95_low === undefined ? "—" : `${fmt(m.ci95_low, 2)} to ${fmt(m.ci95_high, 2)}`} · ${verdict}</div></div>`;
    }).join("");
    set("cPolicy", `<div class="small muted" style="margin-bottom:8px">${tag("SIMULATED")} ${r.config.seeds.length} seeds × ${r.config.days} days, ${r.config.technicians} technician(s), ${r.config.fault_rate_per_charger_day} faults/charger-day${r.config.fleet_multiplier > 1 ? `, fleet ×${r.config.fleet_multiplier}` : ""}. Identical fault traces for both policies.</div>
      <div class="grid-eq" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px">${blocks}</div>
      ${q ? `<div class="note ${q.contention_pct < 10 ? "warn" : ""}" style="margin-top:10px">${esc(q.explanation)}</div>` : ""}`);
  } catch (e) { set("cPolicy", chartError(e.message)); }
}

// 10. investment scenarios
async function drawInvestment(sites) {
  try {
    const site = sites.find(s => String(s.id) === invSite);
    const r = await api("/api/investment/evaluate", { method: "POST", body: { inputs: site ? site.inputs : {} } });
    if (!ctx.alive()) return;
    const sc = r.scenarios, months = r.monthly.map(m => m.month);
    const names = ["conservative", "base", "optimistic"];
    set("cInv", lineChart({
      labels: months.map(m => (m % 12 === 0 ? `Y${m / 12}` : `m${m}`)), tipLabels: months.map(m => `Month ${m}`), height: 260, zero: true, width: 1000,
      valueFmt: v => fmtINR(v), yFmt: v => (Math.abs(v) >= 1e5 ? `₹${fmt(v / 1e5, 0)}L` : `₹${fmt(v, 0)}`),
      series: names.map((n, i) => ({ name: `${n[0].toUpperCase() + n.slice(1)} (NPV ${fmtINR(sc[n].npv)}, payback ${sc[n].payback_years ? fmt(sc[n].payback_years, 1) + " y" : "not reached"})`, color: cat(i), values: sc[n].monthly_cumulative, dash: n === "base" ? null : "7 4" })),
    }) + `<div class="small muted" style="margin-top:6px">Cumulative undiscounted cash flow after the initial investment of ${fmtINR(r.initial_investment.total)}. Scenarios scale demand and price inputs by ASSUMPTION multipliers; all inputs are editable on the Site feasibility page and are not validated against operator data.</div>`);
  } catch (e) { set("cInv", chartError(e.message)); }
}
