import { api } from "../api.js";
import { esc, tag, toast, scoreBar, factorLegend, errorBox } from "../ui.js";
import { openTicket } from "../drawers.js";

let root, ctx;
const KEYS = ["severity", "utilisation", "capacity", "age"];
const NAMES = { severity: "Severity S", utilisation: "Site utilisation U", capacity: "Capacity lost R", age: "Fault age A" };

export async function render(el, c) {
  root = el; ctx = c;
  const [cfg, cat] = await Promise.all([api("/api/priority/config"), api("/api/catalog")]);
  el.innerHTML = `
    <div class="grid-2e">
      <section class="panel"><div class="panel-h"><h2>Weights ${tag("ASSUMPTION")}</h2><span class="hint">Normalised to sum to 1</span></div>
        <form class="panel-b stack" id="wForm" style="gap:12px">
          ${KEYS.map(k => `<label class="field"><span>${NAMES[k]}: <b data-show="${k}">${cfg.weights[k]}</b> → normalised <b data-norm="${k}"></b></span><input type="range" min="0" max="1" step="0.05" name="${k}" value="${cfg.weights[k]}"></label>`).join("")}
          <div class="form-grid">
            <label class="field"><span>Age cap (hours)</span><input type="number" name="age_cap_hours" min="1" step="1" value="${cfg.age_cap_hours}"></label>
            <label class="field"><span>Starvation cap (hours)</span><input type="number" name="starvation_cap_hours" min="1" step="1" value="${cfg.starvation_cap_hours}"></label>
          </div>
          <div class="toolbar"><button type="button" id="preview">Preview ranking</button><button class="primary" type="submit">Save weights</button><button type="button" id="defaults">Restore defaults</button></div>
          <div class="small muted">${esc(cfg.note)}</div>
        </form></section>
      <section class="panel"><div class="panel-h"><h2>How the score is defined</h2></div>
        <div class="panel-b stack" style="gap:10px">
          <pre class="formula">P = wS·S + wU·U + wR·R + wA·A          (each factor 0–1, weights sum to 1)</pre>
          <table class="t"><tbody>
            <tr><td><b>S</b> Severity</td><td class="small">${Object.entries(cat.severity_values).map(([k, v]) => `${esc(k)} = ${v} (${esc(cat.severity_meaning[k])})`).join("<br>")}<br>Unknown fault types are treated as critical and flagged.</td></tr>
            <tr><td><b>U</b> Utilisation</td><td class="small">Site expected sessions/day ÷ highest expected sessions/day among sites of the same data source. 0 if that maximum is 0.</td></tr>
            <tr><td><b>R</b> Capacity lost</td><td class="small">Charger kW ÷ (charger kW + kW of other chargers at the site that can still charge). 1 when it was the last working charger.</td></tr>
            <tr><td><b>A</b> Age</td><td class="small">min(hours waiting ÷ age cap, 1).</td></tr>
            <tr><td><b>Order</b></td><td class="small">Tickets waiting at least the starvation cap are escalated and served first, oldest first. Otherwise higher score first; ties go to the earlier detection, then to the ticket code.</td></tr>
          </tbody></table>
          <div class="small muted">Fault severity per type: ${cat.fault_types.map(f => `${esc(f.label)} (${f.severity})`).join(", ")}.</div>
        </div></section>
    </div>
    <section class="panel"><div class="panel-h"><h2 id="rankTitle">Current ranking</h2><span class="hint" id="rankHint">Open tickets, ${esc(ctx.state.source)} data</span></div>
      <div class="panel-b flush tbl-wrap" id="rank"></div><div class="panel-b">${factorLegend()}</div></section>`;
  const form = el.querySelector("#wForm");
  const updateNorm = () => {
    const v = Object.fromEntries(KEYS.map(k => [k, Number(form[k].value)]));
    const sum = KEYS.reduce((a, k) => a + v[k], 0);
    KEYS.forEach(k => { el.querySelector(`[data-show="${k}"]`).textContent = v[k].toFixed(2); el.querySelector(`[data-norm="${k}"]`).textContent = sum > 0 ? (v[k] / sum).toFixed(2) : "—"; });
  };
  form.addEventListener("input", updateNorm);
  updateNorm();
  const body = () => ({ weights: Object.fromEntries(KEYS.map(k => [k, Number(form[k].value)])), age_cap_hours: Number(form.age_cap_hours.value), starvation_cap_hours: Number(form.starvation_cap_hours.value) });
  form.onsubmit = async (e) => { e.preventDefault(); try { await api("/api/priority/config", { method: "PUT", body: body() }); toast("Weights saved — all rankings now use them."); loadRank(); } catch (er) { toast(er.message, true); } };
  el.querySelector("#preview").onclick = async () => {
    try { const rows = await api(`/api/priority/preview?source=${ctx.state.source}`, { method: "POST", body: body() }); showRank(rows, true); }
    catch (er) { toast(er.message, true); }
  };
  el.querySelector("#defaults").onclick = () => { KEYS.forEach(k => form[k].value = cfg.defaults.weights[k]); form.age_cap_hours.value = cfg.defaults.age_cap_hours; form.starvation_cap_hours.value = cfg.defaults.starvation_cap_hours; updateNorm(); };
  await loadRank();
}

async function loadRank() {
  try { showRank(await api(`/api/priority/ranking?source=${ctx.state.source}`), false); }
  catch (e) { root.querySelector("#rank").innerHTML = errorBox(e.message); }
}

function showRank(rows, preview) {
  root.querySelector("#rankTitle").textContent = preview ? "Preview with unsaved weights" : "Current ranking";
  const el = root.querySelector("#rank");
  if (!rows.length) { el.innerHTML = '<div class="empty">No open tickets to rank. Inject a fault on the Simulator page.</div>'; return; }
  el.innerHTML = `<table class="t"><thead><tr><th>Rank</th>${preview ? "<th>Saved rank</th>" : ""}<th>Ticket</th><th>Score</th>${KEYS.map(k => `<th class="r">${k[0].toUpperCase()}</th>`).join("")}<th>Why</th></tr></thead><tbody>
    ${rows.map(r => `<tr class="click" data-code="${esc(r.ticket_ref)}"><td class="num"><b>${r.rank}</b></td>${preview ? `<td class="num">${r.current_rank ?? "—"}${r.current_rank && r.current_rank !== r.rank ? (r.current_rank > r.rank ? ' <span class="sig-yes">▲</span>' : ' <span class="sig-worse">▼</span>') : ""}</td>` : ""}<td class="mono">${esc(r.ticket_ref)}</td><td>${scoreBar(r)}</td>${r.factors.map(f => `<td class="r num small">${f.value.toFixed(2)}<div class="muted">${f.contribution.toFixed(2)}</div></td>`).join("")}<td class="small" style="max-width:420px">${esc(r.explanation.split(". ").slice(2).join(". "))}</td></tr>`).join("")}
    </tbody></table>`;
  el.querySelectorAll("tr[data-code]").forEach(tr => tr.onclick = () => openTicket(tr.dataset.code, loadRank));
}
