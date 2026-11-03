// Technician registry and assignment policy. DEMO technicians are fictional staff for the simulation
// and only take SIMULATED tickets; registered technicians can take tickets from any source.
import { api } from "../api.js";
import { esc, tag, toast, errorBox, confirmBox } from "../ui.js";
import { openTicket } from "../drawers.js";

let root, ctx, meta = null, busy = false;
const STATUS_PILL = { available: "st-available", assigned: "st-charging", at_capacity: "st-unavailable", unavailable: "st-offline",
  off_duty: "st-offline", inactive: "st-faulted" };

export async function render(el, c) {
  root = el; ctx = c;
  meta = meta || await api("/api/tickets/meta");
  el.innerHTML = `
    <div class="note">Automatic assignment uses one documented rule for every ticket source: active, available (and on shift), required skill, AC/DC equipment, safety certification for safety faults, and spare capacity; then the lowest workload and, only when both locations are known, the shortest straight-line distance. ${tag("DEMO")} technicians are fictional and never take real (OCPP / operator API / imported) tickets.</div>
    <section class="panel"><div class="panel-h"><h2>Technicians</h2><div class="toolbar"><button class="primary sm" id="addTech">Add technician</button></div></div>
      <div class="panel-b flush tbl-wrap" id="techList"><div class="loading">Loading…</div></div></section>
    <section class="panel"><div class="panel-h"><h2>Assignment policy ${tag("ASSUMPTION")}</h2></div><div class="panel-b" id="policy"></div></section>`;
  el.querySelector("#addTech").onclick = () => editTech(null);
  await refresh();
}

export async function refresh() {
  if (busy || !ctx.alive()) return;
  busy = true;
  try {
    const [techs, pol] = await Promise.all([api("/api/technicians"), api("/api/assignment/policy")]);
    if (!ctx.alive()) return;
    root.querySelector("#techList").innerHTML = `<table class="t"><thead><tr><th>Technician</th><th>Status</th><th>Availability</th><th>Skills</th><th>Equipment</th><th>Safety</th><th class="r">Workload</th><th>Active tickets</th><th>Base</th><th>Shift</th><th></th></tr></thead><tbody>
      ${techs.map(t => `<tr><td><b>${esc(t.name)}</b> ${t.is_demo ? tag("DEMO") : ""}<div class="small muted mono">${esc(t.id)}</div></td>
        <td><span class="pill ${STATUS_PILL[t.status] || "st-offline"}">${esc(t.status.replace("_", " "))}</span></td>
        <td><select data-av="${esc(t.id)}" aria-label="Availability of ${esc(t.name)}" ${t.active ? "" : "disabled"}>${["available", "unavailable", "off_duty"].map(a => `<option value="${a}" ${t.availability === a ? "selected" : ""}>${a.replace("_", " ")}</option>`).join("")}</select></td>
        <td class="small">${t.skills.map(s => esc(s.replace("_", " "))).join(", ") || '<span class="muted">none</span>'}</td>
        <td class="small">${t.equipment.map(x => x.toUpperCase()).join(", ") || '<span class="muted">none</span>'}</td>
        <td>${t.safety_certified ? '<span class="pill st-available">certified</span>' : '<span class="muted small">no</span>'}</td>
        <td class="r num">${t.workload}/${t.capacity}</td>
        <td class="small">${t.active_tickets.map(c => `<a href="#" data-code="${esc(c)}" class="mono">${esc(c)}</a>`).join(", ") || '<span class="muted">—</span>'}</td>
        <td class="small">${esc(t.base_label || "—")}</td><td class="small">${t.shift_start && t.shift_end ? `${esc(t.shift_start)}–${esc(t.shift_end)}` : "any time"}</td>
        <td style="white-space:nowrap"><button class="sm" data-edit="${esc(t.id)}">Edit</button> <button class="sm ${t.active ? "danger" : ""}" data-act="${esc(t.id)}">${t.active ? "Deactivate" : "Activate"}</button></td></tr>`).join("")}
      </tbody></table><div class="pager">${techs.length} technician${techs.length === 1 ? "" : "s"} · changes trigger re-assignment of waiting tickets</div>`;
    root.querySelectorAll("[data-code]").forEach(a => a.onclick = (e) => { e.preventDefault(); openTicket(a.dataset.code, refresh); });
    root.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => editTech(techs.find(t => t.id === b.dataset.edit)));
    root.querySelectorAll("[data-av]").forEach(s => s.onchange = () => save(s.dataset.av, { availability: s.value }, `Availability updated`));
    root.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => {
      const t = techs.find(x => x.id === b.dataset.act);
      if (t.active && t.workload && !(await confirmBox({ title: `Deactivate ${t.name}?`, body: `<p>${t.name} still has ${t.workload} active ticket(s). They stay assigned until you reassign them; no new work will be assigned.</p>`, confirmLabel: "Deactivate", danger: true }))) return;
      save(t.id, { active: !t.active }, t.active ? "Deactivated" : "Activated");
    });
    const p = root.querySelector("#policy");
    p.innerHTML = `<div class="stack" style="gap:8px">
      <label class="check"><input type="checkbox" data-pol="auto_assign" ${pol.auto_assign ? "checked" : ""}> Automatic assignment (SIMULATED tickets, while the simulator's auto-dispatch is on)</label>
      <label class="check"><input type="checkbox" data-pol="auto_assign_live_sources" ${pol.auto_assign_live_sources ? "checked" : ""}> Also auto-assign OCPP / operator-API tickets (registered technicians only)</label>
      <label class="check"><input type="checkbox" data-pol="wait_for_remote_reset" ${pol.wait_for_remote_reset ? "checked" : ""}> Wait for a scheduled remote reset before dispatching (not for safety faults)</label>
      <label class="check"><input type="checkbox" data-pol="emergency_override" ${pol.emergency_override ? "checked" : ""}> Emergency override: a qualified technician may exceed capacity for critical/escalated tickets (skills and safety are never bypassed)</label>
      <label class="field" style="max-width:260px"><span>Travel speed for straight-line estimates (km/h, ASSUMPTION)</span><input type="number" min="1" max="120" step="1" data-pol="travel_speed_kmh" value="${pol.travel_speed_kmh}"></label>
      <div class="small muted">${esc(pol.note)}</div></div>`;
    p.querySelectorAll("[data-pol]").forEach(i => i.onchange = async () => {
      const v = i.type === "checkbox" ? i.checked : Number(i.value);
      try { await api("/api/assignment/policy", { method: "PUT", body: { [i.dataset.pol]: v } }); toast("Assignment policy updated"); refresh(); }
      catch (e) { toast(e.message, true); refresh(); }
    });
  } catch (e) { root.querySelector("#techList").innerHTML = errorBox(e.message); }
  finally { busy = false; }
}

async function save(id, body, msg) {
  try { await api(`/api/technicians/${encodeURIComponent(id)}`, { method: "PUT", body }); toast(msg); refresh(); }
  catch (e) { toast(e.message, true); refresh(); }
}

function editTech(t) {
  const wrap = document.createElement("div");
  wrap.className = "modal-back";
  const v = t || { name: "", zone: "", skills: [], equipment: ["ac"], safety_certified: false, capacity: 1, availability: "available", base_label: "", base_lat: null, base_lng: null, shift_start: "", shift_end: "", notes: "" };
  wrap.innerHTML = `<div class="modal wide" role="dialog" aria-modal="true" aria-labelledby="teT"><h3 id="teT">${t ? `Edit ${esc(t.name)}` : "Add technician"} ${t && t.is_demo ? tag("DEMO") : ""}</h3>
    <form id="teF" class="stack" style="gap:10px">
      <div class="form-grid">
        ${t ? "" : '<label class="field"><span>ID (optional)</span><input name="id" maxlength="32" pattern="[A-Za-z0-9_-]+" placeholder="auto"></label>'}
        <label class="field"><span>Display name *</span><input name="name" required maxlength="80" value="${esc(v.name)}"></label>
        <label class="field"><span>Zone / team</span><input name="zone" maxlength="40" value="${esc(v.zone || "")}"></label>
        <label class="field"><span>Capacity (concurrent tickets)</span><input name="capacity" type="number" min="1" max="10" value="${v.capacity}"></label>
        <label class="field"><span>Availability</span><select name="availability">${["available", "unavailable", "off_duty"].map(a => `<option value="${a}" ${v.availability === a ? "selected" : ""}>${a.replace("_", " ")}</option>`).join("")}</select></label>
        <label class="field"><span>Shift start (optional)</span><input name="shift_start" type="time" value="${esc(v.shift_start || "")}"></label>
        <label class="field"><span>Shift end (optional)</span><input name="shift_end" type="time" value="${esc(v.shift_end || "")}"></label>
      </div>
      <fieldset style="border:0;padding:0"><legend class="small" style="font-weight:650">Skills (fault categories this technician can repair)</legend>
        <div class="toolbar" style="flex-wrap:wrap">${meta.skills.map(s => `<label class="check"><input type="checkbox" name="skill" value="${s}" ${v.skills.includes(s) ? "checked" : ""}> ${esc(s.replace("_", " "))}</label>`).join("")}</div></fieldset>
      <div class="toolbar"><label class="check"><input type="checkbox" name="eq_ac" ${v.equipment.includes("ac") ? "checked" : ""}> AC chargers</label><label class="check"><input type="checkbox" name="eq_dc" ${v.equipment.includes("dc") ? "checked" : ""}> DC chargers</label>
        <label class="check"><input type="checkbox" name="safety_certified" ${v.safety_certified ? "checked" : ""}> Safety-certified (electrical / thermal hazards)</label></div>
      <div class="form-grid">
        <label class="field"><span>Home base label</span><input name="base_label" maxlength="80" value="${esc(v.base_label || "")}"></label>
        <label class="field"><span>Base latitude (optional)</span><input name="base_lat" type="number" step="0.0001" value="${v.base_lat ?? ""}"></label>
        <label class="field"><span>Base longitude (optional)</span><input name="base_lng" type="number" step="0.0001" value="${v.base_lng ?? ""}"></label>
      </div>
      <div class="small muted">Leave the base coordinates empty if unknown: ChargeSight then makes no proximity or travel-time claim for this technician.</div>
      <label class="field"><span>Notes</span><input name="notes" maxlength="1000" value="${esc(v.notes || "")}"></label>
      <div class="toolbar" style="justify-content:flex-end"><button type="button" id="teC">Cancel</button><button class="primary" type="submit" id="teS">Save</button></div></form></div>`;
  document.body.appendChild(wrap);
  const close = () => wrap.remove();
  wrap.querySelector("#teC").onclick = close;
  wrap.querySelector("#teF").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    const num = (x) => (x === "" ? null : Number(x));
    const body = { name: f.name.value.trim(), zone: f.zone.value.trim(), capacity: Number(f.capacity.value || 1), availability: f.availability.value,
      skills: [...f.querySelectorAll("[name=skill]:checked")].map(i => i.value), equipment: [f.eq_ac.checked ? "ac" : null, f.eq_dc.checked ? "dc" : null].filter(Boolean),
      safety_certified: f.safety_certified.checked, base_label: f.base_label.value.trim(), base_lat: num(f.base_lat.value), base_lng: num(f.base_lng.value),
      shift_start: f.shift_start.value || null, shift_end: f.shift_end.value || null, notes: f.notes.value.trim() };
    if (!body.name) { toast("Name is required.", true); return; }
    if ((body.base_lat === null) !== (body.base_lng === null)) { toast("Give both base coordinates or neither.", true); return; }
    if (!t && f.id && f.id.value.trim()) body.id = f.id.value.trim();
    const btn = wrap.querySelector("#teS"); btn.disabled = true;
    try {
      await api(t ? `/api/technicians/${encodeURIComponent(t.id)}` : "/api/technicians", { method: t ? "PUT" : "POST", body });
      toast(t ? "Technician updated" : "Technician added"); close(); refresh();
    } catch (err) { toast(err.message, true); btn.disabled = false; }
  };
}
