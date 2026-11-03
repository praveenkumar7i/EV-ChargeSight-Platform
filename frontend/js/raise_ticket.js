// "Raise a Ticket": one shared, accessible form used by the Tickets page, the Fleet Dashboard and the
// charger drawer. The backend creates, prioritises and (when appropriate) assigns the ticket; this form
// only collects the report, validates it and shows the outcome.
import { api } from "./api.js";
import { esc, tag, toast } from "./ui.js";
import { state } from "./app.js";

let meta = null;

function rid() {
  try { return (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36)).replace(/[^A-Za-z0-9_-]/g, ""); }
  catch { return "r" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10); }
}

export async function openRaiseTicket({ chargerId = null, onDone = null } = {}) {
  const source = state.source || "simulation";
  let chargers;
  try {
    [meta, chargers] = await Promise.all([meta ? Promise.resolve(meta) : api("/api/tickets/meta"), api(`/api/chargers?source=${encodeURIComponent(source)}`)]);
  } catch (e) { toast(`Cannot open the ticket form: ${e.message}`, true); return; }
  if (!chargers.length) { toast("No chargers in the selected data source to report a problem for.", true); return; }
  const requestId = rid();
  const wrap = document.createElement("div");
  wrap.className = "modal-back";
  wrap.innerHTML = `<div class="modal wide" role="dialog" aria-modal="true" aria-labelledby="rtTitle">
    <h3 id="rtTitle">Raise a ticket ${tag(source === "simulation" ? "SIMULATED" : source === "operator_import" ? "IMPORTED" : "LIVE OPERATIONAL DATA")}</h3>
    <form id="rtForm" class="stack" style="gap:10px" novalidate>
      <label class="field"><span>Charger <b aria-hidden="true">*</b></span>
        <input id="rtCharger" list="rtChargers" required autocomplete="off" placeholder="Type a charger ID, site or location" value="${esc(chargerId || "")}">
        <datalist id="rtChargers">${chargers.map(c => `<option value="${esc(c.id)}">${esc(c.site_name || "")} · ${esc(c.connector_type)} ${c.power_kw} kW · ${esc(c.status)}</option>`).join("")}</datalist>
        <span class="small muted" id="rtChInfo"></span></label>
      <div class="form-grid">
        <label class="field"><span>Issue category <b aria-hidden="true">*</b></span><select id="rtCat" required>
          <option value="">Choose…</option>${meta.issue_categories.map(c => `<option value="${c.key}">${esc(c.label)}${c.safety ? " (safety)" : ""}</option>`).join("")}</select></label>
        <label class="field"><span>Impact</span><select id="rtImpact"><option value="">Not sure</option>${meta.impacts.map(i => `<option value="${i.key}">${esc(i.label)}</option>`).join("")}</select></label>
        <label class="field"><span>Charger completely unable to charge?</span><select id="rtUnable"><option value="">Not sure</option><option value="true">Yes</option><option value="false">No</option></select></label>
        <label class="field"><span>Chargers affected (site-wide issue)</span><input id="rtN" type="number" min="1" max="100" value="1"></label>
      </div>
      <label class="field"><span>Problem description <b aria-hidden="true">*</b></span><textarea id="rtDesc" rows="3" maxlength="4000" required placeholder="What happened? What did you see?"></textarea></label>
      <div class="form-grid">
        <label class="field"><span>Symptoms (optional)</span><input id="rtSym" maxlength="2000" placeholder="e.g. screen frozen, burning smell, cable hot"></label>
        <label class="field"><span>Error code (optional)</span><input id="rtErr" maxlength="60" placeholder="as shown on the charger"></label>
        <label class="field"><span>Your name or role (optional)</span><input id="rtBy" maxlength="80" placeholder="e.g. site attendant"></label>
      </div>
      <div class="small muted">A report is recorded as <b>USER_REPORTED</b>; it is not charger telemetry. Priority is calculated by the Priority Engine and a qualified technician is assigned automatically when the rules allow. No attachments: this prototype has no secure file storage. Fields marked * are required.</div>
      <div class="error-box hidden" id="rtErrBox" role="alert"></div>
      <div class="toolbar" style="justify-content:flex-end"><button type="button" id="rtCancel">Cancel</button><button type="submit" class="primary" id="rtSubmit">Submit ticket</button></div>
    </form>
    <div id="rtResult" class="hidden" aria-live="polite"></div></div>`;
  document.body.appendChild(wrap);
  const $ = (s) => wrap.querySelector(s);
  const close = () => { wrap.remove(); document.removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape" && !$("#rtSubmit").disabled) { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", onKey, true);
  $("#rtCancel").onclick = close;
  const byId = Object.fromEntries(chargers.map(c => [c.id, c]));
  const info = () => {
    const c = byId[$("#rtCharger").value.trim()];
    $("#rtChInfo").textContent = c ? `${c.site_name} · ${c.connector_type} ${c.power_kw} kW · currently ${c.status}${c.open_tickets.length ? ` · open: ${c.open_tickets.join(", ")}` : ""}` : "";
  };
  $("#rtCharger").oninput = info; info();
  (chargerId ? $("#rtCat") : $("#rtCharger")).focus();
  let submitting = false;
  $("#rtForm").onsubmit = async (e) => {
    e.preventDefault();
    if (submitting) return;                         // prevents duplicate submissions
    const err = [];
    const cid = $("#rtCharger").value.trim();
    if (!byId[cid]) err.push("Choose a charger from the list (unknown charger ID).");
    if (!$("#rtCat").value) err.push("Choose an issue category.");
    if (!$("#rtDesc").value.trim()) err.push("Describe the problem.");
    const n = Number($("#rtN").value || 1);
    if (!(n >= 1 && n <= 100)) err.push("Chargers affected must be between 1 and 100.");
    const box = $("#rtErrBox");
    if (err.length) { box.innerHTML = err.map(esc).join("<br>"); box.classList.remove("hidden"); return; }
    box.classList.add("hidden");
    submitting = true;
    const btn = $("#rtSubmit"); btn.disabled = true; btn.textContent = "Submitting…";
    const body = { charger_id: cid, issue_category: $("#rtCat").value, description: $("#rtDesc").value.trim(),
      symptoms: $("#rtSym").value.trim(), error_code: $("#rtErr").value.trim() || null, impact: $("#rtImpact").value || null,
      unable_to_charge: $("#rtUnable").value === "" ? null : $("#rtUnable").value === "true", affected_chargers: n,
      reported_by: $("#rtBy").value.trim(), client_request_id: requestId };
    try {
      const r = await api("/api/tickets", { method: "POST", body });
      $("#rtForm").classList.add("hidden");
      const res = $("#rtResult"); res.classList.remove("hidden");
      res.innerHTML = `<div class="note ${r.created ? "ok" : "warn"}"><b>${r.created ? `Ticket ${esc(r.code)} created` : `Already reported: ${esc(r.code)}`}</b>${r.message ? ` — ${esc(r.message)}` : ""}</div>
        <dl class="kv" style="margin-top:10px">
          <div><dt>Priority</dt><dd>${esc(r.severity)}${r.priority ? ` · score ${r.priority.score.toFixed(2)} · rank ${r.priority.rank}` : ""}</dd></div>
          <div><dt>SLA deadline</dt><dd>${esc((r.sla_due_at || "").replace("T", " ").replace("Z", " UTC"))} <span class="muted small">(${esc(r.time_basis)})</span></dd></div>
          <div><dt>Technician</dt><dd>${r.technician_id ? `${esc(r.technician_name)}${r.technician_is_demo ? " (DEMO)" : ""}` : r.dispatch_needed ? '<b style="color:var(--crit)">No eligible technician — dispatch needed</b>' : "not assigned yet"}</dd></div>
          <div style="grid-column:1/-1"><dt>Why</dt><dd class="small">${esc(r.assignment_note || r.severity_basis || "")}</dd></div>
          <div style="grid-column:1/-1"><dt>Next action</dt><dd>${esc(r.next_action || r.recommended_action)}</dd></div></dl>
        <div class="toolbar" style="justify-content:flex-end;margin-top:12px"><button id="rtOpen">Open ticket</button><button class="primary" id="rtDone">Done</button></div>`;
      $("#rtDone").onclick = close;
      $("#rtOpen").onclick = async () => { close(); const { openTicket } = await import("./drawers.js"); openTicket(r.code, onDone); };
      toast(r.created ? `${r.code} created (${r.severity})` : `${r.code} already open; report added`);
      onDone && onDone(r);
    } catch (e2) {
      box.textContent = `Submission failed: ${e2.message}`; box.classList.remove("hidden");
      submitting = false; btn.disabled = false; btn.textContent = "Submit ticket";
    }
  };
}
