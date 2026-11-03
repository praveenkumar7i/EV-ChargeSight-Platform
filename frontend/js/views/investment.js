import { api } from "../api.js";
import { esc, fmt, fmtINR, fmtPct, tag, toast, debounce, errorBox, confirmBox } from "../ui.js";
import { lineChart, cat } from "../charts.js";

let root, ctx, defaults, editingId = null;
let fromCandidate = null;      // { candidate, area, sources } when opened from the Map
let saving = false;

const GROUPS = [
  ["Site", [["site_name", "Site name", "text"], ["lat", "Latitude", "number", "0.0001"], ["lng", "Longitude", "number", "0.0001"]]],
  ["Chargers and capital cost (₹)", [["n_chargers", "Number of chargers", "number", "1"], ["charger_type", "Type (AC/DC)", "select"], ["power_kw", "Rated power per charger (kW)", "number", "0.1"],
    ["equipment_cost_per_charger", "Equipment cost per charger", "number", "1000"], ["installation_cost", "Installation", "number", "1000"],
    ["grid_connection_cost", "Grid connection", "number", "1000"], ["site_preparation_cost", "Site preparation", "number", "1000"]]],
  ["Demand", [["sessions_per_day", "Sessions per day (whole site, year 1)", "number", "0.5"], ["kwh_per_session", "Energy per session (kWh)", "number", "0.5"], ["annual_growth_pct", "Annual demand growth (fraction, 0.08 = 8%)", "number", "0.01"]]],
  ["Prices and variable costs (₹)", [["price_per_kwh", "Charging price per kWh", "number", "0.1"], ["electricity_cost_per_kwh", "Electricity cost per kWh", "number", "0.1"],
    ["variable_cost_per_kwh", "Other variable cost per kWh", "number", "0.05"], ["payment_fee_pct", "Payment fee (fraction of revenue)", "number", "0.005"]]],
  ["Fixed operating costs (₹)", [["rent_monthly", "Rent / lease per month", "number", "500"], ["fixed_opex_monthly", "Other fixed opex per month", "number", "500"], ["maintenance_per_charger_annual", "Maintenance per charger per year", "number", "1000"]]],
  ["Analysis", [["analysis_years", "Analysis period (years)", "number", "1"], ["discount_rate_pct", "Discount rate (fraction, 0.12 = 12%)", "number", "0.01"]]],
];

export async function render(el, c) {
  root = el; ctx = c;
  defaults = await api("/api/investment/defaults");
  el.innerHTML = `
    <div class="note">${tag("ASSUMPTION")} inputs → ${tag("ESTIMATED")} outputs · ${tag("NOT YET VALIDATED")}. ${esc(defaults.note)} Demand is entered by you; nothing is inferred from the location.</div>
    <div id="fromMap"></div>
    <div class="grid-2">
      <section class="panel"><div class="panel-h"><h2>Inputs</h2><div class="toolbar"><button class="sm" id="reset">Example values</button></div></div>
        <form class="panel-b stack" id="invForm" style="gap:14px">
          ${GROUPS.map(([g, fields]) => `<fieldset style="border:0;padding:0;margin:0"><legend class="small" style="font-weight:650;margin-bottom:6px">${g}</legend><div class="form-grid">
            ${fields.map(([k, label, type, step]) => type === "select"
              ? `<label class="field"><span>${label}</span><select name="${k}"><option>DC</option><option>AC</option></select></label>`
              : `<label class="field"><span>${label}</span><input name="${k}" type="${type}" ${step ? `step="${step}"` : ""}></label>`).join("")}
          </div></fieldset>`).join("")}
          <div class="toolbar"><button type="button" class="primary" id="save">Save as proposed site</button><button type="button" class="sm" id="saveNew">Save as new site</button><span class="small muted" id="editing"></span></div>
        </form></section>
      <div class="stack">
        <section class="panel"><div class="panel-h"><h2>Estimate ${tag("ESTIMATED")}</h2></div><div class="panel-b" id="out"><div class="loading">Calculating…</div></div></section>
        <section class="panel"><div class="panel-h"><h2>Scenarios</h2><span class="hint">Multipliers are ASSUMPTIONS</span></div><div class="panel-b flush tbl-wrap" id="scen"></div></section>
      </div>
    </div>
    <section class="panel"><div class="panel-h"><h2>Cumulative cash flow</h2><span class="hint">Undiscounted, monthly, three scenarios; payback where a line crosses zero · ESTIMATED from ASSUMPTION inputs</span></div><div class="panel-b" id="cfChart"></div></section>
    <section class="panel"><div class="panel-h"><h2>Year-by-year calculation</h2></div><div class="panel-b flush tbl-wrap" id="years"></div>
      <div class="panel-b"><details><summary>Formulas</summary><pre class="formula">${esc(defaults.formulas)}</pre></details></div></section>
    <section class="panel"><div class="panel-h"><h2>Compare planning candidates ${tag("ESTIMATED")}</h2><span class="hint">identical assumptions = the inputs in the form above</span></div>
      <div class="panel-b" id="candCmp"><div class="loading">Loading candidates…</div></div></section>
    <section class="panel"><div class="panel-h"><h2>Proposed sites</h2><div class="toolbar"><button class="sm" id="compare">Compare selected</button></div></div>
      <div class="panel-b flush tbl-wrap" id="sites"></div><div class="panel-b flush tbl-wrap" id="cmp"></div></section>`;
  setForm(defaults.inputs);
  await applyCandidateFromHash();
  if (!ctx.alive()) return;
  const form = el.querySelector("#invForm");
  form.addEventListener("input", debounce(evaluate, 300));
  el.querySelector("#reset").onclick = () => { editingId = null; setForm(fromCandidate ? { ...defaults.inputs, ...fromCandidate.values } : defaults.inputs); evaluate(); el.querySelector("#editing").textContent = ""; };
  el.querySelector("#save").onclick = save;
  el.querySelector("#saveNew").onclick = saveAsNew;
  el.querySelector("#compare").onclick = compare;
  await evaluate();
  await loadSites();
  await loadCandidates();
}

// ------------------------------------------------------------------ planning candidates under identical assumptions
async function loadCandidates() {
  const box = root.querySelector("#candCmp");
  let cands;
  try { cands = await api("/api/planning/candidates"); } catch (e) { box.innerHTML = errorBox(e.message); return; }
  if (!cands.length) { box.innerHTML = '<div class="empty">No planning candidates yet. On the Map, press “Plan a new charging site”, pick points and save them as candidates.</div>'; return; }
  const pre = fromCandidate ? fromCandidate.candidate.id : null;
  box.innerHTML = `
    <div class="toolbar" style="flex-wrap:wrap;gap:6px 14px">${cands.map(c => `<label class="check"><input type="checkbox" data-cand="${c.id}" ${pre === null || pre === c.id || cands.length <= 4 ? "checked" : ""}> ${esc(c.name)} <span class="small muted">${esc(c.area_name)}</span></label>`).join("")}</div>
    <div class="toolbar" style="margin-top:8px"><label class="check"><input type="checkbox" id="useCandVals" checked> Use each candidate's own charger plan and demand estimate (entered on the Map)</label>
      <button class="primary sm" id="candCmpRun">Compare with these assumptions</button></div>
    <div id="candCmpOut"></div>`;
  let busy = false;
  box.querySelector("#candCmpRun").onclick = async () => {
    if (busy) return;
    const ids = [...box.querySelectorAll("[data-cand]:checked")].map(i => Number(i.dataset.cand));
    if (!ids.length) { toast("Tick at least one candidate.", true); return; }
    busy = true; const b = box.querySelector("#candCmpRun"); b.disabled = true; b.textContent = "Calculating…";
    try {
      const base = readForm();
      const r = await api("/api/planning/compare-finance", { method: "POST", body: { candidate_ids: ids, base_inputs: base, use_candidate_values: box.querySelector("#useCandVals").checked } });
      box.querySelector("#candCmpOut").innerHTML = `<div class="note" style="margin-top:8px">${esc(r.note)}</div>
        <div class="tbl-wrap"><table class="t" id="candCmpTable"><thead><tr><th>Candidate</th><th class="r">Capacity</th><th class="r">Initial investment</th><th class="r">Yr-1 operating contribution</th><th class="r">NPV</th><th class="r">IRR</th><th class="r">Payback</th><th class="r">Break-even</th><th class="r">Utilisation</th><th>Candidate values used</th></tr></thead><tbody>
        ${r.rows.map(x => `<tr><td><b>${esc(x.site_name)}</b><div class="small muted">${fmt(x.lat, 4)}, ${fmt(x.lng, 4)}</div></td><td class="r num">${fmt(x.capacity_kw, 0)} kW</td><td class="r num">${fmtINR(x.initial_investment)}</td><td class="r num">${fmtINR(x.year1_operating_contribution)}</td><td class="r num">${fmtINR(x.npv)}</td><td class="r num">${x.irr === null ? "n/a" : fmtPct(100 * x.irr, 1)}</td><td class="r num">${x.payback_years ? fmt(x.payback_years, 1) + " y" : "not reached"}</td><td class="r num">${x.breakeven_sessions_per_day === null ? "none" : fmt(x.breakeven_sessions_per_day, 1) + "/day"}</td><td class="r num">${fmtPct(100 * x.implied_utilisation, 1)}</td>
          <td class="small">${Object.keys(x.candidate_values_used).length ? Object.entries(x.candidate_values_used).map(([k, v]) => `${esc(k.replace(/_/g, " "))} ${esc(v)}`).join(", ") : "none (all shared assumptions)"}</td></tr>`).join("")}
        </tbody></table></div>`;
    } catch (e) { box.querySelector("#candCmpOut").innerHTML = errorBox(e.message); }
    finally { busy = false; b.disabled = false; b.textContent = "Compare with these assumptions"; }
  };
}

function setForm(inp) {
  const f = root.querySelector("#invForm");
  Object.entries(inp).forEach(([k, v]) => { if (f[k]) f[k].value = v ?? ""; });
}

function readForm() {
  const f = root.querySelector("#invForm");
  const out = {};
  GROUPS.forEach(([, fields]) => fields.forEach(([k, , type]) => {
    const v = f[k].value;
    if (type === "number") out[k] = v === "" ? null : Number(v);
    else out[k] = v;
  }));
  ["n_chargers", "analysis_years"].forEach(k => { if (out[k] !== null) out[k] = Math.round(out[k]); });
  return out;
}

async function evaluate() {
  const out = root.querySelector("#out");
  let r;
  try { r = await api("/api/investment/evaluate", { method: "POST", body: { inputs: readForm() } }); }
  catch (e) { out.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; return; }
  const y = r.year1;
  const flags = r.flags.map(f => `<div class="note ${f.level === "critical" ? "crit" : "warn"}" style="margin-top:6px">${esc(f.message)}</div>`).join("");
  out.innerHTML = `
    <div class="kpis" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
      <div class="kpi"><span class="lbl">Initial investment</span><span class="val">${fmtINR(r.initial_investment.total)}</span></div>
      <div class="kpi"><span class="lbl">Year-1 gross revenue</span><span class="val">${fmtINR(y.revenue)}</span><span class="sub">not profit</span></div>
      <div class="kpi"><span class="lbl">Year-1 operating contribution</span><span class="val ${y.operating_contribution > 0 ? "ok" : "crit"}">${fmtINR(y.operating_contribution)}</span><span class="sub">${fmtINR(y.monthly_operating_contribution)} / month</span></div>
      <div class="kpi"><span class="lbl">Simple payback</span><span class="val">${r.payback_years ? fmt(r.payback_years, 1) + " y" : "not reached"}</span><span class="sub">within ${r.inputs.analysis_years} years</span></div>
      <div class="kpi"><span class="lbl">NPV @ ${fmtPct(100 * r.inputs.discount_rate_pct, 0)}</span><span class="val ${r.npv >= 0 ? "ok" : "crit"}">${fmtINR(r.npv)}</span></div>
      <div class="kpi"><span class="lbl">IRR</span><span class="val">${r.irr === null ? "n/a" : fmtPct(100 * r.irr, 1)}</span></div>
      <div class="kpi"><span class="lbl">Break-even demand</span><span class="val">${r.breakeven_sessions_per_day === null ? "none" : fmt(r.breakeven_sessions_per_day, 1)}</span><span class="sub">sessions/day, year 1</span></div>
      <div class="kpi"><span class="lbl">Implied utilisation</span><span class="val">${fmtPct(100 * y.implied_utilisation, 1)}</span><span class="sub">energy ÷ 24 h capacity</span></div>
    </div>${flags}
    <table class="t" style="margin-top:12px"><tbody>
      <tr><td>Energy delivered, year 1</td><td class="r num">${fmt(y.energy_kwh, 0)} kWh</td></tr>
      <tr><td>Gross revenue</td><td class="r num">${fmtINR(y.revenue)}</td></tr>
      <tr><td>− Electricity</td><td class="r num">${fmtINR(y.electricity_cost)}</td></tr>
      <tr><td>− Variable costs and payment fees</td><td class="r num">${fmtINR(y.variable_cost)}</td></tr>
      <tr><td>= Gross margin</td><td class="r num">${fmtINR(y.gross_margin)}</td></tr>
      <tr><td>− Fixed opex (rent, fixed, maintenance)</td><td class="r num">${fmtINR(y.fixed_opex)}</td></tr>
      <tr><td><b>= Operating contribution</b> (pre-tax cash flow)</td><td class="r num"><b>${fmtINR(y.operating_contribution)}</b></td></tr>
      <tr><td class="muted small" colspan="2">Margin per session: ${fmtINR(y.margin_per_session)}. Investment = equipment ${fmtINR(r.initial_investment.equipment)} + installation ${fmtINR(r.initial_investment.installation)} + grid ${fmtINR(r.initial_investment.grid_connection)} + site prep ${fmtINR(r.initial_investment.site_preparation)}.</td></tr>
    </tbody></table>`;
  const sc = r.scenarios;
  root.querySelector("#scen").innerHTML = `<table class="t"><thead><tr><th>Case</th><th class="r">Sessions/day</th><th class="r">Yr-1 contribution</th><th class="r">NPV</th><th class="r">Payback</th></tr></thead><tbody>
    ${Object.entries(sc).map(([k, s]) => `<tr><td><b>${esc(k)}</b><div class="small muted">${Object.keys(s.multipliers).length ? Object.entries(s.multipliers).map(([m, v]) => `${esc(m.replace(/_/g, " ").replace(" per kwh", "").replace(" pct", ""))} ×${v}`).join(", ") : "inputs as entered"}</div></td><td class="r num">${fmt(s.sessions_per_day, 1)}</td><td class="r num">${fmtINR(s.year1_operating_contribution)}</td><td class="r num">${fmtINR(s.npv)}</td><td class="r num">${s.payback_years ? fmt(s.payback_years, 1) + " y" : "not reached"}</td></tr>`).join("")}
    </tbody></table>`;
  const names = ["conservative", "base", "optimistic"].filter(n => sc[n]);
  root.querySelector("#cfChart").innerHTML = lineChart({
    labels: r.monthly.map(m => (m.month % 12 === 0 ? `Y${m.month / 12}` : `m${m.month}`)), tipLabels: r.monthly.map(m => `Month ${m.month}`),
    series: names.map((n, i) => ({ name: `${n[0].toUpperCase() + n.slice(1)}${n === "base" ? " (inputs as entered)" : ""}`, color: cat(i), values: sc[n].monthly_cumulative, dash: n === "base" ? null : "7 4", width: n === "base" ? 2.5 : 2 })),
    zero: true, valueFmt: v => fmtINR(v), yFmt: v => (Math.abs(v) >= 1e5 ? `₹${fmt(v / 1e5, 0)}L` : `₹${fmt(v, 0)}`), height: 260, width: 1000,
  });
  root.querySelector("#years").innerHTML = `<table class="t"><thead><tr><th>Year</th><th class="r">Sessions</th><th class="r">Energy kWh</th><th class="r">Revenue</th><th class="r">Electricity</th><th class="r">Variable</th><th class="r">Fixed opex</th><th class="r">Operating contribution</th><th class="r">Cumulative</th><th class="r">Discounted CF</th></tr></thead><tbody>
    <tr><td>0</td><td colspan="7" class="muted small">Initial investment</td><td class="r num">${fmtINR(-r.initial_investment.total)}</td><td class="r num">${fmtINR(-r.initial_investment.total)}</td></tr>
    ${r.years.map(y2 => `<tr><td>${y2.year}</td><td class="r num">${fmt(y2.sessions, 0)}</td><td class="r num">${fmt(y2.energy_kwh, 0)}</td><td class="r num">${fmtINR(y2.revenue)}</td><td class="r num">${fmtINR(y2.electricity_cost)}</td><td class="r num">${fmtINR(y2.variable_cost)}</td><td class="r num">${fmtINR(y2.fixed_opex)}</td><td class="r num">${fmtINR(y2.operating_contribution)}</td><td class="r num">${fmtINR(y2.cumulative_cash_flow)}</td><td class="r num">${fmtINR(y2.discounted_cash_flow)}</td></tr>`).join("")}
    </tbody></table>`;
}

async function save() {
  if (saving) return;
  const inputs = readForm();
  const name = (inputs.site_name || "").trim();
  if (!name) { toast("Give the site a name.", true); return; }
  // Never overwrite a saved proposed site without asking; saving always needs a confirmation.
  let mode = "new";
  if (editingId) {
    const update = await confirmBox({ title: `Update saved site #${editingId}?`,
      body: `<p>You loaded saved site #${editingId}. <b>Update</b> replaces its inputs with the values in the form. Choose Cancel, then use “Save as new site” to keep the old version.</p>`,
      confirmLabel: `Update site #${editingId}` });
    if (!update) return;
    mode = "update";
  } else {
    const ok = await confirmBox({ title: "Save as a new proposed site?",
      body: `<p>“${esc(name)}” will be added to Proposed sites with these inputs (ASSUMPTION) and appear on the Map as an ESTIMATED proposed site.${fromCandidate ? ` It will be linked to planning candidate #${fromCandidate.candidate.id}.` : ""} Existing proposed sites are not changed.</p>`,
      confirmLabel: "Save proposed site" });
    if (!ok) return;
  }
  saving = true;
  try {
    const url = mode === "update" ? `/api/investment/sites/${editingId}` : "/api/investment/sites";
    const r = await api(url, { method: mode === "update" ? "PUT" : "POST", body: { name, inputs } });
    editingId = r.id;
    root.querySelector("#editing").textContent = `Editing saved site #${r.id}`;
    if (fromCandidate && mode === "new") {
      try { await api(`/api/planning/candidates/${fromCandidate.candidate.id}/link`, { method: "POST", body: { site_id: r.id } }); } catch { /* link is informational */ }
    }
    toast(mode === "update" ? `Updated site #${r.id}` : `Saved "${name}" as proposed site #${r.id}`); loadSites();
  } catch (e) { toast(e.message, true); }
  finally { saving = false; }
}

async function saveAsNew() {
  const keep = editingId;
  editingId = null;
  await save();
  if (!editingId) editingId = keep;          // cancelled: keep editing the loaded site
}

// ------------------------------------------------------------------ opened from the Map (candidate context)
function hashParams() { return Object.fromEntries(new URLSearchParams(location.hash.split("?")[1] || "")); }

async function applyCandidateFromHash() {
  const hp = hashParams();
  const box = root.querySelector("#fromMap");
  if (!hp.candidate) { fromCandidate = null; box.innerHTML = ""; return; }
  let r;
  try { r = await api(`/api/planning/candidates/${encodeURIComponent(hp.candidate)}/prefill`); }
  catch (e) { box.innerHTML = `<div class="error-box">Could not load planning candidate #${esc(hp.candidate)}: ${esc(e.message)}</div>`; return; }
  fromCandidate = { ...r, area: hp.area || r.area };
  setForm({ ...defaults.inputs, ...r.values });
  const back = `#map?${fromCandidate.area ? "area=" + encodeURIComponent(fromCandidate.area) + "&" : ""}candidate=${r.candidate.id}`;
  const LABEL = Object.fromEntries(GROUPS.flatMap(([, fs]) => fs.map(([k, l]) => [k, l])));
  box.innerHTML = `<section class="panel"><div class="panel-h"><h2>From the Map: ${esc(r.candidate.name)} ${tag("CANDIDATE")}</h2>
      <div class="toolbar"><a class="btn-link" id="backMap" href="${back}">← Back to map</a></div></div>
    <div class="panel-b"><div class="small">Locality: <b>${esc(r.locality)}</b> · ${r.candidate.lat.toFixed(6)}, ${r.candidate.lng.toFixed(6)}${r.candidate.saved_site_id ? ` · already saved as proposed site #${r.candidate.saved_site_id}` : ""}</div>
      <table class="t" style="margin-top:8px"><thead><tr><th>Prefilled input</th><th>Value</th><th>Source</th></tr></thead><tbody>
        ${Object.entries(r.values).map(([k, v]) => `<tr><td>${esc(LABEL[k] || k)}</td><td class="num">${esc(v)}</td><td class="small">${tag(r.sources[k].label)} ${esc(r.sources[k].text)}</td></tr>`).join("")}
      </tbody></table>
      <div class="small muted" style="margin-top:6px">${esc(r.not_prefilled_note)}</div></div></section>`;
}

async function loadSites() {
  let sites;
  try { sites = await api("/api/investment/sites"); } catch (e) { root.querySelector("#sites").innerHTML = errorBox(e.message); return; }
  root.querySelector("#sites").innerHTML = sites.length ? `<table class="t"><thead><tr><th></th><th>Site</th><th>Location</th><th class="r">Investment</th><th class="r">Yr-1 contribution</th><th class="r">Payback</th><th>Flags</th><th></th></tr></thead><tbody>
    ${sites.map(s => `<tr><td><input type="checkbox" data-cmp="${s.id}" aria-label="Select ${esc(s.name)}"></td><td><b>${esc(s.name)}</b></td><td class="small num">${s.lat !== null ? `${fmt(s.lat, 4)}, ${fmt(s.lng, 4)}` : "—"}</td>
      <td class="r num">${s.summary.valid ? fmtINR(s.summary.initial_investment) : "invalid"}</td><td class="r num">${s.summary.valid ? fmtINR(s.summary.year1_operating_contribution) : ""}</td><td class="r num">${s.summary.payback_years ? fmt(s.summary.payback_years, 1) + " y" : "not reached"}</td>
      <td class="small">${(s.summary.flags || []).map(esc).join(", ") || "—"}</td><td><button class="sm" data-edit="${s.id}">Load</button> <button class="sm danger" data-del="${s.id}">Delete</button></td></tr>`).join("")}
    </tbody></table>` : '<div class="empty">No saved sites. Adjust the inputs and press "Save as proposed site". Saved sites with coordinates appear on the Map.</div>';
  root.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => {
    const s = sites.find(x => x.id === Number(b.dataset.edit));
    editingId = s.id; setForm({ ...s.inputs, site_name: s.name }); evaluate();
    root.querySelector("#editing").textContent = `Editing saved site #${s.id}`;
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  root.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (!b.dataset.armed) { b.dataset.armed = "1"; b.textContent = "Confirm"; return; }
    try { await api(`/api/investment/sites/${b.dataset.del}`, { method: "DELETE" }); if (editingId === Number(b.dataset.del)) editingId = null; loadSites(); }
    catch (e) { toast(e.message, true); }
  });
}

async function compare() {
  const ids = [...root.querySelectorAll("[data-cmp]:checked")].map(i => Number(i.dataset.cmp));
  if (ids.length < 2) { toast("Select at least two saved sites to compare.", true); return; }
  try {
    const r = await api("/api/investment/compare", { method: "POST", body: { site_ids: ids } });
    root.querySelector("#cmp").innerHTML = `<div class="panel-b"><div class="note">${esc(r.note)}</div></div><table class="t"><thead><tr><th>Site</th><th class="r">Capacity</th><th class="r">Investment</th><th class="r">Yr-1 revenue</th><th class="r">Yr-1 operating contribution</th><th class="r">NPV</th><th class="r">Payback</th><th class="r">Utilisation</th><th>Key risks / uncertain assumptions</th></tr></thead><tbody>
      ${r.rows.map(x => `<tr><td><b>${esc(x.site_name)}</b><div class="small muted">${x.lat !== null ? `${fmt(x.lat, 4)}, ${fmt(x.lng, 4)}` : "no location"}</div></td><td class="r num">${fmt(x.capacity_kw, 0)} kW</td><td class="r num">${fmtINR(x.initial_investment)}</td><td class="r num">${fmtINR(x.year1_revenue)}</td><td class="r num">${fmtINR(x.year1_operating_contribution)}</td><td class="r num">${fmtINR(x.npv)}</td><td class="r num">${x.payback_years ? fmt(x.payback_years, 1) + " y" : "not reached"}</td><td class="r num">${fmtPct(100 * x.implied_utilisation, 1)}</td><td class="small">${x.key_risks.map(esc).join("<br>")}</td></tr>`).join("")}
      </tbody></table>`;
  } catch (e) { toast(e.message, true); }
}
