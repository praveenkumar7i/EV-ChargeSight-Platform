import { api } from "../api.js";
import { esc, fmt, fmtINR, dt, tag, toast, errorBox, download } from "../ui.js";

let root, ctx;
const ST_CLASS = { not_started: "st-offline", awaiting_access: "st-unavailable", data_received: "st-charging", in_progress: "st-charging", completed: "st-available" };

export async function render(el, c) {
  root = el; ctx = c;
  el.innerHTML = `
    <div class="note">Validation status reflects recorded evidence only. Nothing here is pre-filled: interviews, datasets and reference figures appear only after you record or import them. Datasets flagged as synthetic samples never count as evidence. ${tag("NOT YET VALIDATED")} applies until evidence exists.</div>
    <section class="panel"><div class="panel-h"><h2>Validation status</h2><button class="sm" id="anon">Export anonymised report (CSV)</button></div><div class="panel-b"><div class="cards" id="tracks"></div></div></section>
    <div class="grid-2e">
      <section class="panel"><div class="panel-h"><h2>Record an operator interview</h2></div>
        <form class="panel-b stack" id="ivForm" style="gap:10px">
          <div class="form-grid">
            <label class="field"><span>Interview date *</span><input type="date" name="interview_date" required></label>
            <label class="field"><span>Participant role *</span><input type="text" name="role" required maxlength="80" placeholder="e.g. maintenance lead"></label>
            <label class="field"><span>Organisation type</span><input type="text" name="organisation_type" maxlength="80" placeholder="e.g. small CPO"></label>
          </div>
          <label class="check"><input type="checkbox" name="consent_identifiable"> Participant consented to an identifying reference being stored</label>
          <label class="field"><span>Participant / organisation reference (stored only with consent)</span><input type="text" name="participant_ref" maxlength="80"></label>
          ${[["current_workflow", "Current maintenance workflow"], ["prioritisation_today", "How faults are prioritised today"], ["difficulties", "Common operational difficulties"], ["data_shareable", "What data they can share"], ["feedback", "Feedback on ChargeSight's workflow"], ["disagreements", "Corrections or disagreements with our assumptions"], ["follow_ups", "Follow-up actions"], ["confidentiality_notes", "Consent and confidentiality notes (excluded from exports)"]]
            .map(([k, l]) => `<label class="field"><span>${l}</span><textarea name="${k}"></textarea></label>`).join("")}
          <div><button class="primary" type="submit">Save interview</button></div>
        </form></section>
      <div class="stack">
        <section class="panel"><div class="panel-h"><h2>Interviews</h2></div><div class="panel-b flush tbl-wrap" id="ivList"></div></section>
        <section class="panel"><div class="panel-h"><h2>Import historical ticket data</h2></div>
          <form class="panel-b stack" id="dsForm" style="gap:10px">
            <label class="field"><span>Dataset label *</span><input type="text" name="label" required maxlength="200"></label>
            <label class="field"><span>Provided by</span><input type="text" name="provided_by" maxlength="160"></label>
            <label class="field"><span>How permission was obtained (required unless synthetic)</span><textarea name="permission_note"></textarea></label>
            <label class="check"><input type="checkbox" name="is_synthetic_sample" value="true"> This is a synthetic sample for testing (never counts as evidence)</label>
            <label class="field"><span>CSV file</span><input type="file" name="file" accept=".csv,text/csv" required></label>
            <div><button class="primary" type="submit">Import dataset</button></div>
            <div class="small muted">Columns: record_id, charger_id, fault_category, detected_at (required); site_code, site_daily_sessions, charger_kw, acknowledged_at, resolved_at, repair_minutes, energy_kwh (optional). Timestamps must include a timezone offset. Sample: <span class="mono">sample_data/SYNTHETIC_ticket_history_sample.csv</span>.</div>
          </form></section>
      </div>
    </div>
    <section class="panel"><div class="panel-h"><h2>Historical datasets</h2></div><div class="panel-b flush tbl-wrap" id="dsList"></div><div class="panel-b" id="dsOut"></div></section>
    <section class="panel"><div class="panel-h"><h2>Financial estimate validation</h2></div>
      <div class="panel-b stack" style="gap:10px">
        <div class="toolbar"><label class="field" style="min-width:240px"><span>Proposed site</span><select id="finSite"></select></label></div>
        <form class="form-grid" id="refForm">
          <label class="field"><span>Metric</span><select name="metric" id="finMetric"></select></label>
          <label class="field"><span>Reference value</span><input type="number" step="any" name="reference_value" required></label>
          <label class="field"><span>Period</span><input type="text" name="period" required placeholder="e.g. FY2025-26"></label>
          <label class="field"><span>Data source</span><input type="text" name="data_source" required placeholder="who supplied it"></label>
          <label class="field"><span>Limitations</span><input type="text" name="limitations"></label>
          <label class="field"><span>&nbsp;</span><button class="primary" type="submit">Add reference</button></label>
        </form>
        <div id="finOut"></div>
      </div></section>`;
  el.querySelector("#anon").onclick = () => download("/api/reports/validation.csv");
  el.querySelector("#ivForm").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target), body = {};
    fd.forEach((v, k) => body[k] = v);
    body.consent_identifiable = fd.get("consent_identifiable") === "on";
    try { await api("/api/validation/interviews", { method: "POST", body }); e.target.reset(); toast("Interview saved"); loadAll(); }
    catch (er) { toast(er.message, true); }
  };
  el.querySelector("#dsForm").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    if (!fd.get("is_synthetic_sample")) fd.set("is_synthetic_sample", "false");
    try { const r = await api("/api/validation/datasets", { method: "POST", form: fd }); e.target.reset(); toast(`Imported ${r.records} records`); loadAll(); showQuality(r); }
    catch (er) { toast(er.message, true); }
  };
  el.querySelector("#refForm").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const sid = Number(el.querySelector("#finSite").value);
    if (!sid) { toast("Save a proposed site on the Site feasibility page first.", true); return; }
    try {
      await api("/api/validation/financial-references", { method: "POST", body: { investment_site_id: sid, metric: fd.get("metric"), reference_value: Number(fd.get("reference_value")), period: fd.get("period"), data_source: fd.get("data_source"), limitations: fd.get("limitations") } });
      e.target.reset(); toast("Reference added"); loadFin(); loadTracks();
    } catch (er) { toast(er.message, true); }
  };
  const metrics = await api("/api/validation/financial-metrics");
  el.querySelector("#finMetric").innerHTML = Object.entries(metrics).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  el.querySelector("#finSite").onchange = loadFin;
  await loadAll();
}

async function loadAll() { await Promise.all([loadTracks(), loadInterviews(), loadDatasets(), loadSites()]); }

async function loadTracks() {
  try {
    const r = await api("/api/validation/tracks");
    const names = { operational: "Operational data", financial: "Financial estimates", interviews: "Operator interviews" };
    root.querySelector("#tracks").innerHTML = Object.entries(r.tracks).map(([k, t]) => `<div class="card"><h3><span>${names[k]}</span><span class="pill ${ST_CLASS[t.status]}">${esc(t.label)}</span></h3>
      <div class="small muted">Evidence: ${Object.entries(t.evidence).map(([a, b]) => `${esc(a.replace(/_/g, " "))} ${b}`).join(" · ")}</div>
      ${t.note ? `<div class="small">${esc(t.note)}</div>` : ""}
      <div class="toolbar"><select data-track="${k}" aria-label="Status for ${names[k]}">${Object.entries(r.statuses).map(([s, l]) => `<option value="${s}" ${s === t.status ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
      <input type="text" data-note="${k}" placeholder="Note (required for Completed)" maxlength="4000">
      <div><button class="sm" data-set="${k}">Update status</button></div></div>`).join("");
    root.querySelectorAll("[data-set]").forEach(b => b.onclick = async () => {
      const k = b.dataset.set;
      try { await api(`/api/validation/tracks/${k}`, { method: "PUT", body: { status: root.querySelector(`[data-track="${k}"]`).value, note: root.querySelector(`[data-note="${k}"]`).value } }); toast("Status updated"); loadTracks(); }
      catch (e) { toast(e.message, true); }
    });
  } catch (e) { root.querySelector("#tracks").innerHTML = errorBox(e.message); }
}

async function loadInterviews() {
  const rows = await api("/api/validation/interviews");
  root.querySelector("#ivList").innerHTML = rows.length ? `<table class="t"><thead><tr><th>Date</th><th>Role</th><th>Organisation type</th><th>Reference</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc(r.interview_date)}</td><td>${esc(r.role)}</td><td>${esc(r.organisation_type)}</td><td class="small">${r.consent_identifiable ? esc(r.participant_ref) : '<span class="muted">not stored (no consent)</span>'}</td><td><button class="sm danger" data-del-iv="${r.id}">Delete</button></td></tr>`).join("")}
    </tbody></table>` : '<div class="empty">No interviews recorded yet.</div>';
  root.querySelectorAll("[data-del-iv]").forEach(b => b.onclick = async () => {
    if (!b.dataset.armed) { b.dataset.armed = 1; b.textContent = "Confirm"; return; }
    await api(`/api/validation/interviews/${b.dataset.delIv}`, { method: "DELETE" }); loadAll();
  });
}

async function loadDatasets() {
  const rows = await api("/api/validation/datasets");
  root.querySelector("#dsList").innerHTML = rows.length ? `<table class="t"><thead><tr><th>#</th><th>Label</th><th>Type</th><th class="r">Records</th><th>Imported</th><th></th></tr></thead><tbody>
    ${rows.map(d => `<tr><td>${d.id}</td><td><b>${esc(d.label)}</b><div class="small muted">${esc(d.provided_by)}</div></td><td>${d.is_synthetic_sample ? '<span class="tag tag-NYV">SYNTHETIC SAMPLE</span>' : tag("IMPORTED")}</td><td class="r num">${d.records}</td><td class="small">${dt(d.imported_at)}</td>
      <td><button class="sm" data-q="${d.id}">Quality</button> <button class="sm" data-cmp="${d.id}">Compare with model</button> <button class="sm" data-rep="${d.id}">Retrospective replay</button> <button class="sm danger" data-del-ds="${d.id}">Delete</button></td></tr>`).join("")}
    </tbody></table>` : '<div class="empty">No historical datasets imported.</div>';
  root.querySelectorAll("[data-q]").forEach(b => b.onclick = async () => showQuality(await api(`/api/validation/datasets/${b.dataset.q}`)));
  root.querySelectorAll("[data-cmp]").forEach(b => b.onclick = async () => showCompare(await api(`/api/validation/datasets/${b.dataset.cmp}/compare`, { method: "POST" })));
  root.querySelectorAll("[data-rep]").forEach(b => b.onclick = async () => {
    try { showReplay(await api(`/api/validation/datasets/${b.dataset.rep}/replay`, { method: "POST", body: { technicians: 2 } })); loadTracks(); }
    catch (e) { toast(e.message, true); }
  });
  root.querySelectorAll("[data-del-ds]").forEach(b => b.onclick = async () => {
    if (!b.dataset.armed) { b.dataset.armed = 1; b.textContent = "Confirm"; return; }
    await api(`/api/validation/datasets/${b.dataset.delDs}`, { method: "DELETE" }); root.querySelector("#dsOut").innerHTML = ""; loadAll();
  });
}

function showQuality(d) {
  const q = d.quality;
  root.querySelector("#dsOut").innerHTML = `<h3 style="margin:0 0 8px;font-size:14px">Data quality: ${esc(d.label)} ${d.is_synthetic_sample ? '<span class="tag tag-NYV">SYNTHETIC SAMPLE</span>' : ""}</h3>
    <div class="small">Rows ${q.rows} · valid ${q.valid_records} · rejected ${q.rejected_rows} · duplicate IDs ${q.duplicate_record_ids} · timestamp-order errors ${q.timestamp_order_errors}</div>
    <table class="t" style="margin-top:8px"><thead><tr><th>Field</th><th class="r">Completeness</th></tr></thead><tbody>${Object.entries(q.field_completeness_pct).map(([k, v]) => `<tr><td class="mono small">${esc(k)}</td><td class="r num">${fmt(v, 1)}%</td></tr>`).join("")}</tbody></table>
    ${q.row_errors.length ? `<details style="margin-top:6px"><summary>${q.row_errors.length} row problems</summary><table class="t"><tbody>${q.row_errors.slice(0, 50).map(e => `<tr><td class="small mono">line ${e.line}</td><td class="small">${esc(e.errors.join("; "))}</td></tr>`).join("")}</tbody></table></details>` : ""}`;
}

function showCompare(r) {
  root.querySelector("#dsOut").innerHTML = `<h3 style="margin:0 0 8px;font-size:14px">Observed vs model assumptions: ${esc(r.dataset_label)} ${r.is_synthetic_sample ? '<span class="tag tag-NYV">SYNTHETIC SAMPLE</span>' : tag("IMPORTED")}</h3>
    <div class="tbl-wrap"><table class="t"><thead><tr><th>Metric</th><th>Category</th><th class="r">Model (ASSUMPTION)</th><th class="r">Observed</th><th class="r">n</th><th class="r">Abs. error</th><th class="r">% error</th><th>Basis</th></tr></thead><tbody>
    ${r.rows.map(x => `<tr><td>${esc(x.metric)}</td><td>${esc(x.category)}</td><td class="r num">${fmt(x.model_estimate, 1)}</td><td class="r num">${fmt(x.observed, 1)}</td><td class="r num">${x.n}</td><td class="r num">${fmt(x.abs_error, 1)}</td><td class="r num">${x.pct_error === null ? "—" : fmt(x.pct_error, 1) + "%"}</td><td class="small muted">${esc(x.estimate_basis)}</td></tr>`).join("")}
    </tbody></table></div><ul class="small muted">${r.limitations.map(l => `<li>${esc(l)}</li>`).join("")}</ul>`;
}

function showReplay(r) {
  const keys = Object.keys(r.fifo);
  root.querySelector("#dsOut").innerHTML = `<h3 style="margin:0 0 8px;font-size:14px">${esc(r.label)}: ${esc(r.dataset_label)} ${r.is_synthetic_sample ? '<span class="tag tag-NYV">SYNTHETIC SAMPLE</span>' : ""}</h3>
    <div class="note warn">${esc(r.disclaimer)}</div>
    <div class="small muted" style="margin:6px 0">${r.faults} faults · ${r.chargers} chargers · ${r.technicians} technicians · travel ${r.assumptions.travel_min} min (ASSUMPTION) · ${r.assumptions.repairs_using_assumed_duration} repairs used assumed durations · remote reset: ${esc(r.assumptions.remote_reset)}</div>
    <table class="t"><thead><tr><th>Metric</th><th class="r">FIFO</th><th class="r">Priority</th><th class="r">Difference</th></tr></thead><tbody>
    ${keys.map(k => `<tr><td class="mono small">${esc(k)}</td><td class="r num">${fmt(r.fifo[k], 2)}</td><td class="r num">${fmt(r.priority[k], 2)}</td><td class="r num">${fmt(r.difference[k], 2)}</td></tr>`).join("")}
    </tbody></table>`;
}

async function loadSites() {
  const sites = await api("/api/investment/sites");
  const sel = root.querySelector("#finSite");
  const keep = sel.value;
  sel.innerHTML = sites.length ? sites.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("") : '<option value="">No saved sites</option>';
  if (keep) sel.value = keep;
  loadFin();
}

async function loadFin() {
  const sid = root.querySelector("#finSite").value;
  const out = root.querySelector("#finOut");
  if (!sid) { out.innerHTML = '<div class="muted small">Save a proposed site on the Site feasibility page to validate its estimates.</div>'; return; }
  const r = await api(`/api/validation/financial/${sid}`);
  out.innerHTML = r.comparisons.length ? `<div class="note ok">${esc(r.status)}</div><table class="t"><thead><tr><th>Metric</th><th class="r">Estimate</th><th class="r">Reference</th><th class="r">Abs. error</th><th class="r">% error</th><th>Source / period</th><th>Limitations</th></tr></thead><tbody>
    ${r.comparisons.map(x => `<tr><td>${esc(x.label)}</td><td class="r num">${fmt(x.estimate, 1)}</td><td class="r num">${fmt(x.reference, 1)}</td><td class="r num">${fmt(x.abs_error, 1)}</td><td class="r num">${x.pct_error === null ? "—" : fmt(x.pct_error, 1) + "%"}</td><td class="small">${esc(x.data_source)} · ${esc(x.period)}</td><td class="small">${esc(x.limitations)}</td></tr>`).join("")}
    </tbody></table><div class="small muted">${esc(r.note)}</div>` : `<div class="note">${tag("NOT YET VALIDATED")} No reference figures from an operator have been recorded for ${esc(r.site_name)}. Estimates stay unvalidated until real values are added.</div>`;
}
