// Charger and ticket detail drawers, shared by dashboard, map and tickets.
import { api } from "./api.js";
import {
  esc, fmt, fmtMin, dt, ago, tag, statusPill, ticketPill, sev, scoreBar, factorLegend,
  toast, openDrawer, closeDrawer, confirmBox,
} from "./ui.js";
import { state } from "./app.js";

let catalogCache = null;
async function catalog() {
  if (!catalogCache) catalogCache = await api("/api/catalog");
  return catalogCache;
}

export function priorityBlock(p) {
  if (!p) return '<div class="muted small">No priority score (ticket is resolved).</div>';
  const rows = p.factors.map(f => `<tr><td>${esc(f.label)}</td><td class="r num">${f.value.toFixed(2)}</td><td class="r num">× ${f.weight.toFixed(2)}</td><td class="r num"><b>${f.contribution.toFixed(3)}</b></td><td class="small muted">${esc(f.detail)}</td></tr>`).join("");
  const above = (p.above || []).length
    ? `<div class="small" style="margin-top:8px"><b>Ranked above this ticket:</b><ul style="margin:4px 0 0;padding-left:18px">${p.above.map(a => `<li><span class="mono">${esc(a.ticket_ref)}</span> (score ${a.score.toFixed(2)}, +${a.score_gap.toFixed(2)}): ${esc(a.reason)}</li>`).join("")}</ul></div>`
    : `<div class="small muted" style="margin-top:8px">No open ticket ranks above this one.</div>`;
  return `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">${scoreBar(p)}<span class="small">Rank <b>${p.rank}</b></span></div>
    <div class="tbl-wrap" style="margin-top:8px"><table class="t"><thead><tr><th>Factor</th><th class="r">Input</th><th class="r">Weight</th><th class="r">Contribution</th><th>Raw input</th></tr></thead>
    <tbody>${rows}<tr><td><b>Total P</b></td><td></td><td></td><td class="r num"><b>${p.score.toFixed(3)}</b></td><td></td></tr></tbody></table></div>
    <div class="explain" style="margin-top:8px">${esc(p.explanation)}</div>${above}
    <div style="margin-top:6px">${factorLegend()}</div>`;
}

export async function openCharger(id, onChange) {
  const el = openDrawer(`<div class="drawer-h"><h3>${esc(id)}</h3><button class="ghost" data-close aria-label="Close">✕</button></div><div class="drawer-b"><div class="loading">Loading…</div></div>`);
  el.querySelector("[data-close]").onclick = closeDrawer;
  let c;
  try { c = await api(`/api/chargers/${encodeURIComponent(id)}`); }
  catch (e) { el.querySelector(".drawer-b").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; return; }
  const cat = await catalog();
  const now = state.sim && c.source === "simulation" ? state.sim.sim_time : new Date().toISOString();
  const cur = c.current_ticket;
  el.querySelector(".drawer-h h3").innerHTML = `<span class="mono">${esc(c.id)}</span> ${statusPill(c.status)} ${tag(c.label)}`;
  el.querySelector(".drawer-b").innerHTML = `
    <section><dl class="kv">
      <div><dt>Site</dt><dd>${esc(c.site_name)} <span class="muted small">(${esc(c.site_code)})</span></dd></div>
      <div><dt>Location</dt><dd class="num">${c.site.has_position ? `${fmt(c.lat, 4)}, ${fmt(c.lng, 4)}` : "unknown"} ${c.site.is_demo_location ? '<span class="muted small">demo area centroid</span>' : ""}</dd></div>
      <div><dt>Connector</dt><dd>${esc(c.connector_type)} · ${fmt(c.power_kw, 1)} kW</dd></div>
      <div><dt>Last event</dt><dd>${c.last_event ? `${esc(c.last_event.type)} · ${ago(c.last_event.ts, now)}` : "—"}</dd></div>
      <div><dt>Last heartbeat</dt><dd>${ago(c.last_heartbeat_at, now)}</dd></div>
      <div><dt>Energy delivered</dt><dd class="num">${fmt(c.total_kwh, 1)} kWh</dd></div>
      <div><dt>Power now</dt><dd class="num">${fmt(c.power_now_kw, 1)} kW</dd></div>
      <div><dt>Temperature</dt><dd class="num">${c.temp_c === null ? "—" : fmt(c.temp_c, 1) + " °C"} <span class="muted small">${esc(c.temp_label)}</span></dd></div>
      <div><dt>Fault code</dt><dd class="mono">${esc(c.fault_code || "none")}</dd></div>
    </dl></section>
    <section><div class="toolbar"><button class="primary sm" id="raiseHere">Raise a ticket for this charger</button></div></section>
    <section><h4>Current ticket</h4>${cur ? `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px"><button class="sm" data-ticket="${esc(cur.code)}">${esc(cur.code)}</button>${ticketPill(cur.status)} ${sev(cur.severity)} · ${esc(cur.fault_label)}</div>
      ${priorityBlock(cur.priority)}` : '<div class="muted small">No open ticket.</div>'}</section>
    ${c.source === "simulation" ? `<section><h4>Inject a fault (demo)</h4><div class="toolbar">
      <select id="injCat">${cat.fault_types.map(f => `<option value="${f.key}">${esc(f.label)} (${f.severity})</option>`).join("")}</select>
      <select id="injSev" aria-label="Severity"><option value="">default severity</option><option value="critical">critical</option><option value="major">major</option><option value="minor">minor</option></select>
      <button class="danger sm" id="injBtn">Inject fault</button></div>
      <div class="small muted" style="margin-top:4px">Creates a SIMULATED fault at the current simulation time. A second fault of the same type is deduplicated.</div></section>` : ""}
    <section><h4>Fault history</h4>${c.fault_history.length ? `<div class="tbl-wrap"><table class="t"><thead><tr><th>Ticket</th><th>Fault</th><th>Status</th><th>Detected</th><th>Resolved by</th></tr></thead><tbody>
      ${c.fault_history.map(t => `<tr class="click" data-ticket="${esc(t.code)}"><td class="mono">${esc(t.code)}</td><td>${esc(t.fault_label)}</td><td>${ticketPill(t.status)}</td><td class="small">${dt(t.detected_at)}</td><td class="small">${esc(t.resolved_by || "—")}</td></tr>`).join("")}
      </tbody></table></div>` : '<div class="muted small">No faults recorded.</div>'}</section>
    <section><h4>Recent events</h4><div class="feed" style="max-height:260px;border:1px solid var(--line);border-radius:6px">
      ${c.recent_events.map(e => `<div class="ev ${e.type === "fault" || e.type === "offline" ? "bad" : ""}"><span class="t">${dt(e.ts)}</span><span class="a">${esc(e.type)}</span><span>${e.ocpp_action ? `<span class="muted">${esc(e.ocpp_action)}</span>` : ""} ${e.status_after ? "→ " + esc(e.status_after) : ""}</span></div>`).join("") || '<div class="empty">No events yet.</div>'}
    </div></section>`;
  el.querySelectorAll("[data-ticket]").forEach(b => b.addEventListener("click", () => openTicket(b.dataset.ticket, onChange)));
  el.querySelector("#raiseHere").onclick = async () => {
    const { openRaiseTicket } = await import("./raise_ticket.js");
    openRaiseTicket({ chargerId: c.id, onDone: () => { onChange && onChange(); openCharger(id, onChange); } });
  };
  const inj = el.querySelector("#injBtn");
  if (inj) inj.onclick = async () => {
    inj.disabled = true;
    try {
      const r = await api("/api/sim/inject", { method: "POST", body: { charger_id: c.id, category: el.querySelector("#injCat").value, severity: el.querySelector("#injSev").value || null } });
      toast(r.deduplicated ? `Not created: ${r.message}` : `Fault injected — ${r.ticket} opened.`, r.deduplicated);
      onChange && onChange();
      openCharger(id, onChange);
    } catch (e) { toast(e.message, true); inj.disabled = false; }
  };
}

function currentUserName() { return (window.__csMe && window.__csMe.user && window.__csMe.user.full_name) || "you"; }

export function promptBox({ title, body = "", label = "Reason", required = true, confirmLabel = "Confirm", danger = false, extra = "" }) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "modal-back";
    wrap.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="pbT"><h3 id="pbT">${esc(title)}</h3><div class="modal-body">${body}</div>
      <label class="field" style="margin-top:10px"><span>${esc(label)}${required ? " *" : ""}</span><textarea id="pbIn" rows="2" maxlength="500"></textarea></label>${extra}
      <div class="toolbar" style="justify-content:flex-end;margin-top:12px"><button id="pbNo">Cancel</button><button id="pbYes" class="${danger ? "danger" : "primary"}">${esc(confirmLabel)}</button></div></div>`;
    document.body.appendChild(wrap);
    const done = (v) => { wrap.remove(); resolve(v); };
    wrap.querySelector("#pbNo").onclick = () => done(null);
    wrap.querySelector("#pbYes").onclick = () => {
      const v = wrap.querySelector("#pbIn").value.trim();
      if (required && !v) { wrap.querySelector("#pbIn").focus(); return; }
      done({ text: v, extra: wrap });
    };
    wrap.querySelector("#pbIn").focus();
  });
}

function slaText(t) {
  if (t.status === "resolved" || t.status === "closed") return `deadline was ${dt(t.sla_due_at)}`;
  if (t.overdue_min !== null && t.overdue_min !== undefined) return `<span class="sla-over">OVERDUE by ${fmtMin(t.overdue_min)}</span> (deadline ${dt(t.sla_due_at)})`;
  if (t.sla_remaining_min !== null && t.sla_remaining_min !== undefined) return `${fmtMin(t.sla_remaining_min)} remaining (deadline ${dt(t.sla_due_at)})`;
  return dt(t.sla_due_at);
}

const KIND_LABEL = { status: "status", priority: "priority", assignment: "assignment", escalation: "escalation", note: "note", inspection: "inspection" };

export async function openTicket(code, onChange) {
  const el = openDrawer(`<div class="drawer-h"><h3>${esc(code)}</h3><button class="ghost" data-close aria-label="Close">✕</button></div><div class="drawer-b"><div class="loading">Loading…</div></div>`);
  el.querySelector("[data-close]").onclick = closeDrawer;
  let t, el2;
  try { [t, el2] = await Promise.all([api(`/api/tickets/${encodeURIComponent(code)}`), api(`/api/tickets/${encodeURIComponent(code)}/eligible-technicians`)]); }
  catch (e) { el.querySelector(".drawer-b").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; return; }
  const reload = () => { onChange && onChange(); openTicket(code, onChange); };
  const adminOn = state.config && state.config.admin_override_enabled;
  const actions = t.allowed_actions.filter(a => a.to_status !== "assigned");
  const canUnassign = ["assigned", "en_route", "waiting_access"].includes(t.status);
  const done = t.status === "resolved" || t.status === "closed";
  el.querySelector(".drawer-h h3").innerHTML = `<span class="mono">${esc(t.code)}</span> ${ticketPill(t.status)} ${tag(t.label)} ${tag(t.origin_label)}${t.overdue ? ' <span class="tag tag-ESTIMATED">OVERDUE</span>' : ""}${t.escalated ? ' <span class="tag tag-MISSING">ESCALATED</span>' : ""}${t.dispatch_needed ? ' <span class="tag tag-MISSING">DISPATCH NEEDED</span>' : ""}`;
  const reported = t.origin === "user_reported" ? `
    <section><h4>Reported problem ${tag("USER_REPORTED")}</h4><dl class="kv">
      <div><dt>Issue</dt><dd>${esc(t.issue_label || t.fault_label)}</dd></div>
      <div><dt>Impact</dt><dd>${esc(t.impact_label || "not stated")}</dd></div>
      <div><dt>Unable to charge</dt><dd>${t.unable_to_charge === null ? "not stated" : t.unable_to_charge ? "yes" : "no"}</dd></div>
      <div><dt>Chargers affected</dt><dd class="num">${t.affected_chargers}</dd></div>
      <div style="grid-column:1/-1"><dt>Description</dt><dd>${esc(t.description)}</dd></div>
      ${t.symptoms ? `<div style="grid-column:1/-1"><dt>Symptoms</dt><dd>${esc(t.symptoms)}</dd></div>` : ""}
      ${t.error_code ? `<div><dt>Error code</dt><dd class="mono">${esc(t.error_code)}</dd></div>` : ""}
      <div><dt>Reported by</dt><dd>${esc(t.reported_by || "not given")}</dd></div></dl></section>` : "";
  el.querySelector(".drawer-b").innerHTML = `
    <section><dl class="kv">
      <div><dt>Charger</dt><dd><button class="sm" id="toCharger">${esc(t.charger_id)}</button></dd></div>
      <div><dt>Site</dt><dd>${esc(t.site_name)}</dd></div>
      <div><dt>Fault</dt><dd>${esc(t.fault_label)} · ${sev(t.severity)}${t.safety_related ? ' <span class="tag tag-MISSING">SAFETY</span>' : ""}</dd></div>
      <div><dt>Severity basis</dt><dd class="small">${esc(t.severity_basis || "—")}</dd></div>
      <div><dt>Evidence</dt><dd class="small">${esc(t.evidence)}</dd></div>
      <div><dt>Detected</dt><dd>${dt(t.detected_at)}</dd></div>
      <div style="grid-column:1/-1"><dt>SLA</dt><dd>${slaText(t)} <span class="muted small">${esc(t.time_basis)}</span>${t.escalation_reason ? `<div class="small" style="color:var(--crit)">${esc(t.escalation_reason)}</div>` : ""}</dd></div>
      <div><dt>Technician</dt><dd>${esc(t.technician_name || "—")}${t.technician_is_demo ? ` ${tag("DEMO")}` : ""}${t.auto_assigned ? ' <span class="muted small">(auto)</span>' : ""}</dd></div>
      <div><dt>Estimates ${tag("ASSUMPTION")}</dt><dd>travel ${fmtMin(t.est_travel_min)} · repair ${fmtMin(t.est_repair_min)}</dd></div>
      <div><dt>Assigned / accepted</dt><dd>${dt(t.assigned_at)} / ${dt(t.acknowledged_at)}</dd></div>
      <div><dt>On site / resolved</dt><dd>${dt(t.on_site_at)} / ${dt(t.resolved_at)} ${t.resolved_by ? `<span class="muted small">(${esc(t.resolved_by)})</span>` : ""}</dd></div>
      ${t.closed_at ? `<div><dt>Closed</dt><dd>${dt(t.closed_at)}${t.inspection_passed ? " · inspection passed" : ""}</dd></div>` : ""}
      ${t.waiting_reason ? `<div style="grid-column:1/-1"><dt>Waiting</dt><dd>${esc(t.waiting_reason)}</dd></div>` : ""}
      ${t.reopen_count ? `<div><dt>Reopened</dt><dd class="num">${t.reopen_count}×</dd></div>` : ""}
    </dl>
    <div class="note ${t.status === "closed" ? "ok" : t.overdue || t.dispatch_needed || t.severity === "critical" ? "crit" : "warn"}" style="margin-top:10px"><b>Recommended next action:</b> ${esc(t.recommended_action)} <span class="muted">(advisory; ChargeSight records the workflow but contacts nobody)</span></div></section>
    ${reported}
    <section><h4>Assignment</h4>
      <div class="small">${t.assignment_note ? esc(t.assignment_note) : "No assignment decision recorded yet."}</div>
      <div class="small muted" style="margin:6px 0">Needs: skill <b>${esc(el2.required.skill.replace("_", " "))}</b> · ${esc(el2.required.equipment.toUpperCase())} equipment${el2.required.safety_certified ? " · <b>safety certification</b>" : ""}. Rule: ${esc(el2.ranking_rule)}.</div>
      ${!done ? `<div class="small muted" id="actorNote">Your actions are recorded in the audit trail as <b>${esc(currentUserName())}</b> (the signed-in account).</div>
      <div style="margin-top:6px">${el2.candidates.map(c => `<div class="cand-row ${c.eligible ? "ok" : ""}"><div><b>${esc(c.name)}</b> ${c.is_demo ? tag("DEMO") : ""}<div class="small muted mono">${esc(c.technician_id)}</div></div>
        <div class="small">${c.eligible ? "<b>eligible</b>" : '<span style="color:var(--warn)">not eligible</span>'}<div class="muted">load ${c.workload}/${c.capacity}</div></div>
        <div class="small">${c.qualifies.map(esc).join(" · ")}${c.blockers.length ? `<div style="color:var(--warn)">${c.blockers.map(esc).join(" · ")}</div>` : ""}<div class="muted">${esc(c.travel_basis)}${c.travel_min !== null ? `, est. ${c.travel_min} min` : ""}${c.can_meet_sla === false ? ' · <b style="color:var(--crit)">would arrive after SLA</b>' : ""}</div></div>
        <div>${t.technician_id === c.technician_id ? '<span class="small muted">current</span>' : `<button class="sm" data-assign="${esc(c.technician_id)}">${t.technician_id ? "Reassign" : "Assign"}</button>`}</div></div>`).join("")}</div>
      <div class="toolbar" style="margin-top:8px">${t.status === "open" ? '<button class="sm" id="autoAssign">Retry automatic assignment</button>' : ""}${canUnassign ? '<button class="sm" id="unassign">Unassign</button>' : ""}</div>` : ""}
    </section>
    ${actions.length ? `<section><h4>Workflow</h4><div class="stack" style="gap:8px">
      <label class="field"><span>Note / diagnostic / resolution notes</span><textarea id="note" maxlength="2000" rows="2" placeholder="Required for waiting and reopen; recommended for resolve"></textarea></label>
      ${actions.some(a => a.needs_inspection) ? '<label class="check"><input type="checkbox" id="insp"> Post-repair safety inspection passed (required to close a safety-related ticket)</label>' : ""}
      <div class="toolbar">${actions.map(a => `<button class="${a.to_status === "resolved" || a.to_status === "closed" ? "primary" : ""}" data-to="${a.to_status}">${esc(a.label)}</button>`).join("")}</div></div></section>` : ""}
    <section><h4>Add a note</h4><div class="toolbar"><input id="noteOnly" maxlength="2000" placeholder="Diagnostic note (recorded with your name)" style="flex:1;min-width:200px"><button class="sm" id="addNote">Add note</button></div></section>
    ${adminOn && !done ? `<section><details><summary>Administrative override</summary><div class="stack" style="gap:8px;margin-top:8px">
      <div class="small muted">Skips workflow or safety checks. Requires the server's ADMIN_TOKEN and is recorded in the history.</div>
      <div class="form-grid"><label class="field"><span>Move to</span><select id="ovTo">${["assigned", "en_route", "on_site", "resolved", "closed"].filter(s => s !== t.status).map(s => `<option value="${s}">${s}</option>`).join("")}</select></label>
      <label class="field"><span>Technician</span><select id="ovTech">${el2.candidates.map(x => `<option value="${esc(x.technician_id)}">${esc(x.name)}</option>`).join("")}</select></label>
      <label class="field"><span>Admin token</span><input id="ovToken" type="password" autocomplete="off"></label></div>
      <label class="field"><span>Reason (required)</span><input id="ovNote" type="text" maxlength="300"></label>
      <div><button class="danger sm" id="ovBtn">Apply override</button></div></div></details></section>` : ""}
    <section><h4>Priority explanation</h4>${priorityBlock(t.priority)}${t.priority && t.priority.notes && t.priority.notes.length ? `<ul class="small" style="margin:6px 0 0;padding-left:18px">${t.priority.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      ${t.priority_snapshot ? `<div class="small muted" style="margin-top:6px">Stored snapshot ${t.priority_snapshot.score.toFixed(3)} (updated ${dt(t.priority_snapshot.updated_at)}); the live score above moves with the age factor. Snapshot changes are listed in the audit trail.</div>` : ""}</section>
    <section><h4>Audit trail</h4><ul class="timeline">${t.history.map(h => `<li><span class="audit-kind">${esc(KIND_LABEL[h.kind] || h.kind)}</span>${h.kind === "status" ? `<b>${esc(h.from ? h.from + " → " : "")}${esc(h.to)}</b> · ` : ""}${dt(h.at)} · <span class="muted">${esc(h.actor)}</span>${h.override ? ' <span class="tag tag-ESTIMATED">OVERRIDE</span>' : ""}${h.note ? `<div class="muted">${esc(h.note)}</div>` : ""}</li>`).join("")}</ul></section>`;
  const $ = (s) => el.querySelector(s);
  $("#toCharger").onclick = () => openCharger(t.charger_id, onChange);
  const actor = () => currentUserName();      // informational only: the server records the signed-in user
  const post = async (url, body, okMsg, headers = {}) => {
    try { await api(url, { method: "POST", body, headers }); toast(okMsg); reload(); return true; }
    catch (e) { toast(e.message, true); return false; }
  };
  el.querySelectorAll("[data-assign]").forEach(b => b.onclick = async () => {
    const techId = b.dataset.assign;
    const cand = el2.candidates.find(c => c.technician_id === techId);
    b.disabled = true;
    const url = `/api/tickets/${encodeURIComponent(code)}/assign`;
    try {
      await api(url, { method: "POST", body: { technician_id: techId, actor: actor(), reason: "" } });
      toast(`${code}: ${t.technician_id ? "reassigned" : "assigned"} to ${cand.name}`); reload();
    } catch (e) {
      b.disabled = false;
      if (e.status === 403) { toast(e.message || "Safety restriction: only a safety-certified technician can take this job.", true); return; }
      if (e.status !== 409) { toast(e.message, true); return; }
      const r = await promptBox({ title: `Override assignment conflicts for ${cand.name}?`, danger: true, confirmLabel: "Assign anyway",
        body: `<div class="note warn">${esc(e.message)}</div><div class="small muted" style="margin-top:6px">Your name and reason are recorded in the audit trail.</div>` });
      if (!r) return;
      await post(url, { technician_id: techId, actor: actor(), reason: r.text, confirm_conflicts: true }, `${code}: assigned to ${cand.name} (override recorded)`);
    }
  });
  const ua = $("#unassign");
  if (ua) ua.onclick = async () => {
    const r = await promptBox({ title: `Unassign ${t.technician_name}?`, body: "The ticket returns to Open and automatic assignment is retried." });
    if (r) await post(`/api/tickets/${encodeURIComponent(code)}/unassign`, { actor: actor(), reason: r.text }, `${code}: unassigned`);
  };
  const aa = $("#autoAssign");
  if (aa) aa.onclick = async () => {
    aa.disabled = true;
    try { const r = await api(`/api/tickets/${encodeURIComponent(code)}/auto-assign`, { method: "POST" }); toast(r.assigned ? `Assigned to ${r.technician_name}` : (r.assignment_note || "No eligible technician"), !r.assigned); reload(); }
    catch (e) { toast(e.message, true); aa.disabled = false; }
  };
  el.querySelectorAll("[data-to]").forEach(b => b.onclick = async () => {
    const to = b.dataset.to, a = actions.find(x => x.to_status === to);
    const note = $("#note").value.trim();
    if (a.needs_note && !note) { toast("Add a note with the reason first.", true); $("#note").focus(); return; }
    const insp = $("#insp") ? $("#insp").checked : null;
    if (a.needs_inspection && !insp) { toast("Tick the inspection box: a safety-related ticket can only be closed after a passed inspection.", true); return; }
    const ok = await confirmBox({ title: `${a.label} for ${code}?`, confirmLabel: a.label,
      body: `${esc(t.status_label)} → <b>${esc(to.replace("_", " "))}</b>. Recorded with your name and the current ${t.source === "simulation" ? "simulation" : "wall-clock"} time.` });
    if (!ok) return;
    b.disabled = true;
    await post(`/api/tickets/${encodeURIComponent(code)}/transition`, { to_status: to, note, actor: actor(), inspection_passed: a.needs_inspection ? insp : null }, `${code}: ${a.label}`);
    b.disabled = false;
  });
  $("#addNote").onclick = async () => {
    const v = $("#noteOnly").value.trim();
    if (!v) { toast("Type a note first.", true); return; }
    await post(`/api/tickets/${encodeURIComponent(code)}/notes`, { note: v, actor: actor() }, "Note added");
  };
  const ov = $("#ovBtn");
  if (ov) ov.onclick = async () => {
    const note = $("#ovNote").value.trim();
    if (!note) { toast("Give a reason for the override.", true); return; }
    await post(`/api/tickets/${encodeURIComponent(code)}/transition`, { to_status: $("#ovTo").value, technician_id: $("#ovTech").value, note, override: true, actor: actor(), inspection_passed: true },
      `${code}: override applied`, { "X-Admin-Token": $("#ovToken").value });
  };
}
