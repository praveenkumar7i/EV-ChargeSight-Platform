// Explainable suitability ranking of planning candidates (Map page).
// Weights are ASSUMPTIONS the user can change; every factor shows its evidence class and why it scored as it did.
import { api } from "../api.js";
import { esc, fmt, fmtINR, fmtPct, tag, toast } from "../ui.js";

let method = null, weights = null, running = false, last = null;
const CLS_TAG = { "SOURCE DATA": "SOURCE DATA", "USER-ENTERED": "USER-ENTERED", "ASSUMPTION": "ASSUMPTION", "ESTIMATED": "ESTIMATED", "MISSING": "MISSING" };

export async function renderScorePanel(el, candidates, getSelected) {
  if (!el) return;
  if (!method) {
    try { method = await api("/api/planning/method"); } catch (e) { el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; return; }
  }
  if (!weights) weights = { ...method.default_weights };
  el.innerHTML = `
    <h3 style="margin:0 0 6px">Suitability ranking ${tag("ESTIMATED")}</h3>
    <div class="small muted">Ranks the ticked candidates for <b>further investigation</b>. Weights are ASSUMPTIONS: change them to see how the ranking responds. Factors without evidence are left out of the score and listed as missing.</div>
    <details style="margin-top:8px"><summary class="small">Weights (${Object.entries(weights).map(([k, v]) => `${short(k)} ${Math.round(v * 100)}`).join(" · ")})</summary>
      <div class="form-grid" style="margin-top:8px" id="wForm">
        ${method.factors.map(f => `<label class="field"><span>${esc(f.label)} <output id="wo_${f.key}" class="num">${Math.round(weights[f.key] * 100)}</output></span>
          <input type="range" min="0" max="50" step="1" name="${f.key}" value="${Math.round(weights[f.key] * 100)}" aria-label="Weight for ${esc(f.label)}"></label>`).join("")}
      </div>
      <div class="toolbar" style="margin-top:6px"><button class="sm" id="wReset" type="button">Default weights</button><span class="small muted">Weights are normalised to 100 %.</span></div>
    </details>
    <div class="toolbar" style="margin-top:10px"><button class="primary" id="scoreRun" type="button">Rank ticked candidates</button><span class="small muted" id="scoreInfo">${candidates.length < 3 ? "Add at least three candidates for a meaningful comparison." : ""}</span></div>
    <div id="scoreOut" aria-live="polite"></div>
    <details style="margin-top:10px"><summary class="small">How the score works</summary><div class="small" style="margin-top:6px">
      <p>${esc(method.formula)}</p>
      <table class="t"><thead><tr><th>Factor</th><th class="r">Default weight</th><th>Evidence</th><th>Definition</th></tr></thead><tbody>
        ${method.factors.map(f => `<tr><td>${esc(f.label)}</td><td class="r num">${Math.round(f.default_weight * 100)}%</td><td class="small">${esc(f.evidence)}</td><td class="small">${esc(f.definition)}</td></tr>`).join("")}</tbody></table>
      <p class="muted">Thresholds (ASSUMPTIONS): coverage full at ${method.thresholds.coverage_full_km} km; ${method.thresholds.competition_half_connectors} connectors within 2 km halve the competition score; ${method.thresholds.demand_full_amenities} amenities within 500 m = full demand-indicator score; area ${method.thresholds.area_per_charger_m2.DC} m² per DC / ${method.thresholds.area_per_charger_m2.AC} m² per AC charger; power factor ${method.thresholds.power_factor}; payback ≤ ${method.thresholds.payback_full_years} years = full financial score.</p>
      <p class="muted">${Object.entries(method.evidence_classes).map(([k, v]) => `<b>${esc(k)}</b>: ${esc(v)}`).join(" · ")}</p></div></details>`;
  el.querySelectorAll("#wForm input[type=range]").forEach(r => r.oninput = () => {
    weights[r.name] = Number(r.value) / 100; el.querySelector(`#wo_${r.name}`).textContent = r.value;
  });
  el.querySelector("#wReset").onclick = () => { weights = { ...method.default_weights }; renderScorePanel(el, candidates, getSelected); };
  el.querySelector("#scoreRun").onclick = () => run(el, getSelected());
  if (last) show(el, last);
}

function short(k) { return { coverage_gap: "coverage", competition: "competition", demand_indicators: "demand", road_access: "road", site_area: "area", grid_capacity: "grid", financial: "finance" }[k] || k; }

async function run(el, ids) {
  if (running) return;
  if (!ids.length) { toast("Tick at least one candidate.", true); return; }
  running = true;
  const b = el.querySelector("#scoreRun"); b.disabled = true; b.textContent = "Ranking…";
  try {
    last = await api("/api/planning/evaluate", { method: "POST", body: { candidate_ids: ids, weights } });
    show(el, last);
  } catch (e) { el.querySelector("#scoreOut").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
  finally { running = false; const bb = el.querySelector("#scoreRun"); if (bb) { bb.disabled = false; bb.textContent = "Rank ticked candidates"; } }
}

function bar(v) { return v === null || v === undefined ? '<span class="muted">—</span>' : `<span class="scorebar"><i style="width:${Math.max(2, v)}%"></i></span> <b class="num">${fmt(v, 0)}</b>`; }
const CONF = { high: "st-available", medium: "st-unavailable", low: "st-faulted", insufficient: "st-offline" };

function show(el, r) {
  const out = el.querySelector("#scoreOut");
  const byId = Object.fromEntries(r.results.map(x => [x.candidate_id, x]));
  const order = [...r.ranking, ...r.results.filter(x => !x.sufficient).map(x => x.candidate_id)];
  const rankOf = Object.fromEntries(r.ranking.map((id, i) => [id, i + 1]));
  out.innerHTML = `
    ${r.note ? `<div class="note warn" style="margin-top:10px">${esc(r.note)}</div>` : ""}
    <table class="t" style="margin-top:10px" id="rankTable"><thead><tr><th class="r">Rank</th><th>Candidate</th><th>Score (0–100)</th><th class="r">Evidence</th><th>Confidence</th><th class="r">Payback</th><th class="r">NPV</th></tr></thead><tbody>
      ${order.map(id => { const x = byId[id]; return `<tr data-rank="${id}"><td class="r num">${rankOf[id] || "—"}</td><td><b>${esc(x.name)}</b><div class="small muted">${esc(x.area_name)}</div></td>
        <td>${x.sufficient ? bar(x.score) : `<span class="small" style="color:var(--warn)"><b>${esc(x.status)}</b></span>${x.partial_score !== null ? `<div class="small muted">partial score ${fmt(x.partial_score, 0)} (not comparable)</div>` : ""}`}</td>
        <td class="r num">${fmtPct(100 * x.completeness, 0)}</td><td><span class="pill ${CONF[x.confidence]}">${esc(x.confidence)}</span></td>
        <td class="r num">${x.financial ? (x.financial.payback_years ? fmt(x.financial.payback_years, 1) + " y" : "not reached") : "—"}</td>
        <td class="r num">${x.financial ? fmtINR(x.financial.npv) : "—"}</td></tr>`; }).join("")}
    </tbody></table>
    <div class="small muted" style="margin-top:6px">${esc(r.disclaimer)} ${esc(r.base_inputs_note)}</div>
    ${order.map(id => factorBlock(byId[id], rankOf[id])).join("")}
    ${sensBlock(r, byId)}`;
}

function factorBlock(x, rk) {
  return `<details class="cand-expl" style="margin-top:10px" ${rk === 1 ? "open" : ""}><summary><b>${esc(x.name)}</b> — ${x.sufficient ? `rank ${rk}, score ${fmt(x.score, 0)}` : esc(x.status)} · evidence ${fmtPct(100 * x.completeness, 0)} · confidence ${esc(x.confidence)}</summary>
    ${x.insufficient_reasons.length ? `<div class="note warn" style="margin-top:6px"><b>Insufficient data to rank reliably:</b> ${x.insufficient_reasons.map(esc).join("; ")}.</div>` : ""}
    <div style="margin-top:6px">${x.factors.map(f => `<div class="factor-row"><div><b class="small">${esc(f.label)}</b><div class="small muted">weight ${fmt(f.weight * 100, 0)}%</div></div>
      <div class="num">${f.score === null ? '<span class="muted">—</span>' : fmt(f.score * 100, 0)}</div>
      <div class="small">${tag(CLS_TAG[f.evidence_class] || f.evidence_class)} ${esc(f.explanation)}</div></div>`).join("")}</div>
    ${x.missing_evidence.length ? `<h4 class="sub-h">Missing evidence</h4><ul class="small">${x.missing_evidence.map(m => `<li><b>${esc(m.factor)}:</b> ${esc(m.needed)}</li>`).join("")}</ul>` : ""}
    ${x.unconfirmed_assumptions.length ? `<div class="small" style="color:var(--warn)">Unconfirmed assumptions: ${x.unconfirmed_assumptions.map(esc).join(", ")}.</div>` : ""}
    ${x.financial ? `<div class="small" style="margin-top:6px">Financial (ESTIMATED, Site Feasibility model): investment ${fmtINR(x.financial.initial_investment)} · year-1 operating contribution ${fmtINR(x.financial.year1_operating_contribution)} · NPV ${fmtINR(x.financial.npv)} · IRR ${x.financial.irr === null ? "n/a" : fmtPct(100 * x.financial.irr, 1)} · break-even ${x.financial.breakeven_sessions_per_day === null ? "none" : fmt(x.financial.breakeven_sessions_per_day, 1) + " sessions/day"} · utilisation ${fmtPct(100 * x.financial.implied_utilisation, 1)}. Plan: ${x.plan.n_chargers} × ${x.plan.power_kw} kW ${esc(x.plan.charger_type)}${x.plan.notes.length ? ` (${x.plan.notes.map(esc).join(", ")})` : ""}.</div>` : ""}
    <h4 class="sub-h">Field checks before any decision</h4><ul class="small">${x.field_checks.map(c => `<li>${esc(c)}</li>`).join("")}</ul>
  </details>`;
}

function sensBlock(r, byId) {
  if (!r.sensitivity.length) return `<div class="small muted" style="margin-top:10px">Sensitivity analysis needs at least two candidates with enough evidence.</div>`;
  const name = (id) => esc(byId[id] ? byId[id].name : `#${id}`);
  const fin = r.sensitivity.filter(s => s.kind === "financial"), wts = r.sensitivity.filter(s => s.kind === "weights");
  const changed = wts.filter(s => s.top_changes);
  return `<h4 class="sub-h">Sensitivity</h4>
    <div class="small">${r.robust_top ? `<b>Robust:</b> ${name(r.ranking[0])} stays first in every scenario tested.` : `<b>Not robust:</b> the top candidate changes in ${r.sensitivity.filter(s => s.top_changes).length} of ${r.sensitivity.length} scenarios.`}</div>
    <table class="t" style="margin-top:6px"><thead><tr><th>Financial scenario (shared assumptions changed)</th><th>Ranking</th>${r.ranking.map(id => `<th class="r">Payback ${name(id)}</th>`).join("")}</tr></thead><tbody>
      ${fin.map(s => `<tr><td>${esc(s.scenario)}${s.top_changes ? ' <span class="pill st-unavailable">top changes</span>' : ""}</td><td class="small">${s.ranking.map(name).join(" › ")}</td>
        ${r.ranking.map(id => `<td class="r num">${s.payback_years[id] ? fmt(s.payback_years[id], 1) + " y" : s.payback_years[id] === null ? "not reached" : "—"}</td>`).join("")}</tr>`).join("")}
    </tbody></table>
    <div class="small" style="margin-top:6px">Weight changes (each weight ×0.5 and ×1.5): ${changed.length ? `the top candidate changes when ${changed.map(s => esc(s.scenario)).join("; ")}.` : "the top candidate never changes."}</div>`;
}
