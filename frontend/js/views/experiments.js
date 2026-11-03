import { api } from "../api.js";
import { esc, fmt, tag, toast, lineChart, diffWhisker, download, errorBox, dt } from "../ui.js";
import { barChart, cat } from "../charts.js";

let root, ctx, defaults, current = null;
const HIGHER_BETTER = new Set(["availability_pct"]);
const NEUTRAL = new Set(["faults", "remote_resolved", "unresolved_at_horizon"]);
const KEY_METRICS = ["availability_pct", "downtime_h", "weighted_downtime_h", "high_sev_wait_mean_min", "high_sev_wait_p95_min", "low_sev_wait_max_min", "mtta_min", "mttr_min", "energy_not_delivered_kwh", "downtime_cost_inr"];

export async function render(el, c) {
  root = el; ctx = c;
  defaults = await api("/api/experiments/defaults");
  const d = defaults.config;
  el.innerHTML = `
    <div class="note">${tag("SIMULATED")} ${tag("ASSUMPTION")} Each seed generates one fault trace (arrivals, categories, reset outcomes, travel and repair times). FIFO and priority replay that <b>identical</b> trace, so the repair order is the only difference. Results are whatever the simulation produces, including no difference or a worse result.</div>
    <section class="panel"><div class="panel-h"><h2>Experiment settings</h2><span class="hint">Defaults follow the research report's initial design</span></div>
      <form class="panel-b stack" id="xForm" style="gap:12px">
        <div class="form-grid">
          <label class="field"><span>Days simulated</span><input type="number" name="days" min="1" max="366" value="${d.days}"></label>
          <label class="field"><span>Seeds (replications)</span><input type="number" name="nseeds" min="1" max="200" value="${d.seeds.length}"></label>
          <label class="field"><span>First seed</span><input type="number" name="seed0" min="0" value="${d.seeds[0]}"></label>
          <label class="field"><span>Technicians</span><input type="number" name="technicians" min="1" max="20" value="${d.technicians}"></label>
          <label class="field"><span>Fleet size × (40 chargers each)</span><input type="number" name="fleet_multiplier" min="1" max="5" step="1" value="${d.fleet_multiplier || 1}"></label>
          <label class="field"><span>Faults per charger-day</span><input type="number" name="fault_rate_per_charger_day" min="0.001" step="0.01" value="${d.fault_rate_per_charger_day}"></label>
          <label class="field"><span>Repair time ×</span><input type="number" name="repair_multiplier" min="0.1" step="0.1" value="${d.repair_multiplier}"></label>
          <label class="field"><span>Travel time ×</span><input type="number" name="travel_multiplier" min="0.1" step="0.1" value="${d.travel_multiplier}"></label>
          <label class="field"><span>Remote reset delay (min)</span><input type="number" name="reset_delay_min" min="0" value="${d.reset_delay_min}"></label>
          <label class="field"><span>Margin ₹/kWh (cost estimate)</span><input type="number" name="margin_per_kwh_inr" min="0" step="0.5" value="${d.margin_per_kwh_inr}"></label>
        </div>
        <div class="form-grid">${["severity", "utilisation", "capacity", "age"].map(k => `<label class="field"><span>Weight ${k}</span><input type="number" step="0.05" min="0" name="w_${k}" value="${d.priority.weights[k]}"></label>`).join("")}
          <label class="field"><span>Starvation cap (h)</span><input type="number" name="starvation_cap_hours" min="1" value="${d.priority.starvation_cap_hours}"></label></div>
        <details><summary>Fault mix, remote-reset success and repair times</summary>
          <div class="tbl-wrap" style="margin-top:8px"><table class="t"><thead><tr><th>Fault</th><th>Share</th><th>Remote reset success</th><th>Mean repair (min)</th></tr></thead><tbody>
          ${Object.keys(d.category_mix).map(k => `<tr><td>${esc(k)}</td><td><input type="number" step="0.01" min="0" data-mix="${k}" value="${d.category_mix[k]}" style="width:90px"></td><td><input type="number" step="0.05" min="0" max="1" data-reset="${k}" value="${d.remote_reset_success[k]}" style="width:90px"></td><td><input type="number" step="5" min="1" data-repair="${k}" value="${d.repair_mean_min[k]}" style="width:90px"></td></tr>`).join("")}
          </tbody></table></div></details>
        <div class="toolbar">
          <button type="button" class="primary" data-mode="quick">Quick run (5 seeds × 14 days)</button>
          <button type="button" id="example" title="1 technician, 0.3 faults per charger-day, 10 seeds × 30 days">One-click example: busy network</button>
          <button type="button" data-mode="custom">Run with these settings</button>
          <button type="button" data-mode="full">Full evaluation (30 seeds × 30 days)</button>
          <button type="button" id="sens">Sensitivity analysis</button>
          <span class="small muted" id="runStatus"></span>
        </div>
      </form></section>
    <section class="panel" id="resultsPanel"><div class="panel-h"><h2 id="resTitle">Results</h2><div class="toolbar" id="resTools"></div></div><div class="panel-b" id="results"><div class="empty">Run an experiment to see results.</div></div></section>
    <section class="panel"><div class="panel-h"><h2>Sensitivity analysis</h2><span class="hint">Priority − FIFO for each variant; green = significant improvement, red = significantly worse</span></div><div class="panel-b flush tbl-wrap" id="sensBox"><div class="empty">Not run yet.</div></div></section>
    <section class="panel"><div class="panel-h"><h2>Saved runs</h2></div><div class="panel-b flush tbl-wrap" id="runs"></div></section>`;
  el.querySelectorAll("[data-mode]").forEach(b => b.onclick = () => run(b.dataset.mode));
  el.querySelector("#sens").onclick = sensitivity;
  el.querySelector("#example").onclick = async () => {
    busyUI(true, "Running example…");
    try {
      const r = await api("/api/experiments/run", { method: "POST", body: { mode: "custom", label: "Example: busy network", config: { seeds: Array.from({ length: 10 }, (_, i) => i + 1), days: 30, technicians: 1, fault_rate_per_charger_day: 0.3 } } });
      showRun(r); await loadRuns(); toast(`Run #${r.id} complete`);
    } catch (e) { toast(e.message, true); } finally { busyUI(false); }
  };
  await loadRuns();
  const last = (await api("/api/experiments?kind=comparison"))[0];
  if (last) showRun(await api(`/api/experiments/${last.id}`));
  const lastS = (await api("/api/experiments?kind=sensitivity"))[0];
  if (lastS) showSens(await api(`/api/experiments/${lastS.id}`));
}

function config(mode) {
  const f = root.querySelector("#xForm");
  const n = Number(f.nseeds.value), s0 = Number(f.seed0.value);
  const cfg = {
    days: Number(f.days.value), seeds: Array.from({ length: n }, (_, i) => s0 + i), technicians: Number(f.technicians.value),
    fleet_multiplier: Number(f.fleet_multiplier.value) || 1,
    fault_rate_per_charger_day: Number(f.fault_rate_per_charger_day.value), repair_multiplier: Number(f.repair_multiplier.value),
    travel_multiplier: Number(f.travel_multiplier.value), reset_delay_min: Number(f.reset_delay_min.value),
    margin_per_kwh_inr: Number(f.margin_per_kwh_inr.value),
    priority: { weights: Object.fromEntries(["severity", "utilisation", "capacity", "age"].map(k => [k, Number(f["w_" + k].value)])), age_cap_hours: 24, starvation_cap_hours: Number(f.starvation_cap_hours.value) },
    category_mix: {}, remote_reset_success: {}, repair_mean_min: {},
  };
  f.querySelectorAll("[data-mix]").forEach(i => cfg.category_mix[i.dataset.mix] = Number(i.value));
  f.querySelectorAll("[data-reset]").forEach(i => cfg.remote_reset_success[i.dataset.reset] = Number(i.value));
  f.querySelectorAll("[data-repair]").forEach(i => cfg.repair_mean_min[i.dataset.repair] = Number(i.value));
  if (mode === "quick") { delete cfg.seeds; delete cfg.days; }
  return cfg;
}

function busyUI(on, msg = "") {
  root.querySelectorAll("#xForm button").forEach(b => b.disabled = on);
  root.querySelector("#runStatus").textContent = msg;
}

async function run(mode) {
  busyUI(true, "Running…");
  try {
    const r = await api("/api/experiments/run", { method: "POST", body: { mode, config: config(mode), label: `${mode} run` } });
    showRun(r); await loadRuns(); toast(`Run #${r.id} complete`);
  } catch (e) { toast(e.message, true); }
  finally { busyUI(false); }
}

async function sensitivity() {
  busyUI(true, "Running 11 variants…");
  try {
    const r = await api("/api/experiments/sensitivity", { method: "POST", body: { mode: "custom", config: config("custom"), label: "sensitivity" } });
    showSens(r); await loadRuns(); toast(`Sensitivity run #${r.id} complete`);
  } catch (e) { toast(e.message, true); }
  finally { busyUI(false); }
}

function verdict(key, s) {
  if (NEUTRAL.has(key) || s.ci95_low === null || s.ci95_low === undefined) return '<span class="sig-no">—</span>';
  if (!s.significant) return '<span class="sig-no">no significant difference</span>';
  const better = HIGHER_BETTER.has(key) ? s.mean_diff > 0 : s.mean_diff < 0;
  return better ? '<span class="sig-yes">priority better</span>' : '<span class="sig-worse">priority worse</span>';
}

function interpretation(sum, q) {
  const sig = KEY_METRICS.filter(k => sum[k] && sum[k].significant);
  if (!sig.length) return `No metric differs significantly between FIFO and priority in this configuration. ${q ? q.explanation : "Queues rarely form under this load, so repair order seldom matters."}`;
  const better = sig.filter(k => HIGHER_BETTER.has(k) ? sum[k].mean_diff > 0 : sum[k].mean_diff < 0);
  const worse = sig.filter(k => !better.includes(k));
  let t = "";
  if (better.length) t += `Priority repair is significantly better on: ${better.map(k => sum[k].label.toLowerCase()).join(", ")}. `;
  if (worse.length) t += `It is significantly worse on: ${worse.map(k => sum[k].label.toLowerCase()).join(", ")}. `;
  t += "These are simulation results under the stated assumptions; they do not show what would happen on a real network.";
  return t;
}

function showRun(r) {
  current = r;
  const res = r.results, sum = res.summary, cfg = r.config;
  root.querySelector("#resTitle").innerHTML = `Run #${r.id} ${tag("SIMULATED")} <span class="hint">${esc(r.label)} · ${cfg.seeds.length} seeds × ${cfg.days} days · ${cfg.technicians} technicians · ${cfg.fault_rate_per_charger_day} faults/charger-day${cfg.fleet_multiplier > 1 ? ` · fleet ×${cfg.fleet_multiplier}` : ""} · config ${r.config_hash.slice(0, 10)}</span>`;
  root.querySelector("#resTools").innerHTML = `<button class="sm" id="expRes">Export CSV</button><button class="sm" id="expCost">Downtime cost CSV</button>`;
  root.querySelector("#expRes").onclick = () => download(`/api/reports/experiment.csv?run_id=${r.id}`);
  root.querySelector("#expCost").onclick = () => download(`/api/reports/downtime_cost.csv?run_id=${r.id}`);
  const metricOpts = KEY_METRICS.map(k => `<option value="${k}">${esc(sum[k].label)}</option>`).join("");
  const sigs = res.per_seed.map(s => s.trace_signature);
  root.querySelector("#results").innerHTML = `
    <div class="note ${KEY_METRICS.some(k => sum[k].significant) ? "" : "warn"}" style="margin-bottom:12px">${esc(interpretation(sum, res.queueing))}</div>
    ${res.queueing ? `<div class="small muted" style="margin-bottom:10px">Queue contention: ${fmt(res.queueing.contention_pct, 1)}% of ${res.queueing.dispatch_decisions} technician assignments were made with more than one fault waiting. ${esc(res.queueing.definition)}</div>` : ""}
    <div id="cmpChart" style="margin-bottom:12px"></div>
    <div class="tbl-wrap"><table class="t"><thead><tr><th>Metric</th><th class="r">FIFO</th><th class="r">Priority</th><th class="r">Diff (P − F)</th><th class="r">Relative</th><th>95% CI of paired diff</th><th>Verdict</th></tr></thead><tbody>
      ${Object.entries(sum).map(([k, s]) => `<tr><td title="${esc(s.definition)}">${esc(s.label)} <span class="muted small">${esc(s.unit)}</span></td><td class="r num">${fmt(s.mean_a, 2)}</td><td class="r num">${fmt(s.mean_b, 2)}</td><td class="r num">${fmt(s.mean_diff, 3)}</td><td class="r num">${s.relative_diff === null || s.relative_diff === undefined ? "—" : fmt(100 * s.relative_diff, 1) + "%"}</td><td>${NEUTRAL.has(k) ? '<span class="muted small">same trace by design</span>' : `${diffWhisker(s.mean_diff, s.ci95_low, s.ci95_high, !HIGHER_BETTER.has(k))} <span class="small muted num">${s.ci95_low === null || s.ci95_low === undefined ? "" : `${fmt(s.ci95_low, 2)} to ${fmt(s.ci95_high, 2)}`}</span>`}</td><td class="small">${verdict(k, s)}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="toolbar" style="margin:14px 0 4px"><label class="field" style="min-width:260px"><span>Per-seed values</span><select id="seedMetric">${metricOpts}</select></label></div>
    <div id="seedChart"></div>
    <details style="margin-top:8px"><summary>Fairness check: identical traces</summary><div class="small muted" style="margin-top:6px">Each seed's trace signature is computed once and used by both policies: ${sigs.slice(0, 10).map(esc).join(", ")}${sigs.length > 10 ? " …" : ""}</div></details>
    <details style="margin-top:6px"><summary>Metric definitions</summary><table class="t" style="margin-top:6px"><tbody>${Object.values(res.metric_definitions).map(m => `<tr><td><b>${esc(m.label)}</b><div class="small muted">${esc(m.unit)}</div></td><td class="small">${esc(m.definition)}</td></tr>`).join("")}</tbody></table></details>`;
  const draw = () => {
    const k = root.querySelector("#seedMetric").value;
    root.querySelector("#seedChart").innerHTML = lineChart({
      labels: res.per_seed.map(s => `s${s.seed}`),
      series: [{ name: "FIFO (dashed)", color: "#8a93a6", width: 4, dash: "8 5", values: res.per_seed.map(s => s.fifo[k]) }, { name: "Priority", color: "#2f5bea", values: res.per_seed.map(s => s.priority[k]) }],
      yFmt: v => fmt(v, Math.abs(v) < 10 ? 1 : 0),
    });
  };
  const cm = ["high_sev_wait_mean_min", "high_sev_wait_p95_min", "mtta_min", "mttr_min"];
  root.querySelector("#cmpChart").innerHTML = `<div class="small muted">Mean over seeds, minutes (lower is better). Hover for values; the table gives the paired confidence intervals.</div>` + barChart({
    labels: cm.map(k => sum[k].label.replace(", high-severity faults", " (critical)").replace(" (mean time to acknowledge)", "").replace(" (mean time to resolve)", "")),
    stacked: false, height: 200, unit: "min", width: 1000,
    series: [{ name: "FIFO", color: cat(1), values: cm.map(k => sum[k].mean_a) }, { name: "Priority", color: cat(0), values: cm.map(k => sum[k].mean_b) }],
  });
  root.querySelector("#seedMetric").onchange = draw;
  root.querySelector("#seedMetric").value = "weighted_downtime_h";
  draw();
}

function showSens(r) {
  const vs = r.results.variants;
  const metrics = Object.keys(vs[0].summary);
  root.querySelector("#sensBox").innerHTML = `<table class="t"><thead><tr><th>Variant</th>${metrics.map(m => `<th class="r">${esc(vs[0].summary[m].label)}</th>`).join("")}</tr></thead><tbody>
    ${vs.map(v => `<tr><td><b>${esc(v.variant)}</b><div class="small muted mono">${esc(JSON.stringify(v.changes))}</div></td>${metrics.map(m => {
      const s = v.summary[m];
      const cls = !s.significant ? "sig-no" : ((HIGHER_BETTER.has(m) ? s.mean_diff > 0 : s.mean_diff < 0) ? "sig-yes" : "sig-worse");
      return `<td class="r num"><span class="${cls}">${fmt(s.mean_diff, 2)}</span><div class="small muted">${s.ci95_low === null ? "" : `[${fmt(s.ci95_low, 1)}, ${fmt(s.ci95_high, 1)}]`}</div></td>`;
    }).join("")}</tr>`).join("")}
    </tbody></table><div class="panel-b small muted">Run #${r.id}. Each variant changes one setting from the base configuration and reuses the same seeds. Cells show priority − FIFO and the paired 95% CI. <a href="/api/reports/sensitivity.csv?run_id=${r.id}">Export CSV</a></div>`;
}

async function loadRuns() {
  try {
    const runs = await api("/api/experiments");
    root.querySelector("#runs").innerHTML = runs.length ? `<table class="t"><thead><tr><th>#</th><th>Kind</th><th>Label</th><th>When</th><th>Seeds × days</th><th>Technicians</th><th>Config</th><th></th></tr></thead><tbody>
      ${runs.map(r => `<tr><td class="num">${r.id}</td><td>${esc(r.kind)}</td><td>${esc(r.label)}</td><td class="small">${dt(r.created_at)}</td><td class="num">${r.seeds ?? "—"} × ${r.days ? fmt(r.days, 0) : "—"}</td><td class="num">${r.technicians ?? "—"}</td><td class="mono small">${esc(r.config_hash.slice(0, 10))}</td><td>${r.kind !== "retrospective" ? `<button class="sm" data-open="${r.id}" data-kind="${r.kind}">Open</button>` : ""}</td></tr>`).join("")}
      </tbody></table>` : '<div class="empty">No runs yet.</div>';
    root.querySelectorAll("[data-open]").forEach(b => b.onclick = async () => {
      const r = await api(`/api/experiments/${b.dataset.open}`);
      b.dataset.kind === "sensitivity" ? showSens(r) : showRun(r);
      root.querySelector(b.dataset.kind === "sensitivity" ? "#sensBox" : "#resultsPanel").scrollIntoView({ behavior: "smooth" });
    });
  } catch (e) { root.querySelector("#runs").innerHTML = errorBox(e.message); }
}
