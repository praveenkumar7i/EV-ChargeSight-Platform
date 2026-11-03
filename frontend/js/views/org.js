// Organisation profile, members, invitations and audit log.
import { api } from "../api.js";
import { esc, toast, errorBox, confirmBox, dt } from "../ui.js";
import { state, can, refreshMe } from "../app.js";

let root, ctx;

export async function render(el, c) {
  root = el; ctx = c;
  const manageUsers = can("users.manage"), manageOrg = can("org.manage");
  const [org, members, auditRows] = await Promise.all([api("/api/org"), manageUsers ? api("/api/org/members") : null,
    manageOrg ? api("/api/org/audit") : null]);
  if (!ctx.alive()) return;
  root.innerHTML = `
    <section class="panel"><div class="panel-h"><h2>Organisation profile</h2></div><div class="panel-b">
      <form id="of" class="stack" style="gap:10px"><div class="form-grid">
        <label class="field"><span>Name</span><input name="name" required minlength="2" maxlength="120" value="${esc(org.name)}"></label>
        <label class="field"><span>Type</span><select name="org_type">${Object.entries(org.org_types).map(([k, v]) => `<option value="${k}" ${k === org.org_type ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <label class="field"><span>Country</span><input name="country" maxlength="60" value="${esc(org.country)}"></label>
        <label class="field"><span>Phone (optional)</span><input name="contact_phone" maxlength="40" value="${esc(org.contact_phone)}"></label>
        <label class="field"><span>Website (optional)</span><input name="website" maxlength="200" value="${esc(org.website)}"></label>
        <label class="field"><span>GSTIN / tax ID (optional, not validated)</span><input name="tax_id" maxlength="40" value="${esc(org.tax_id)}"></label>
      </div>
      <label class="field"><span>Billing address (optional)</span><input name="billing_address" maxlength="1000" value="${esc(org.billing_address)}"></label>
      <div class="small muted">Organisation code <span class="mono">${esc(org.code)}</span> prefixes your ticket and charger IDs. ${org.is_system_demo ? "This is the system demo workspace that holds the prototype data from before v9." : ""}</div>
      ${manageOrg ? '<div><button class="primary">Save profile</button></div>' : ""}</form></div></section>
    ${members ? membersHtml(members) : '<div class="note">Members are visible to organisation owners and administrators.</div>'}
    ${auditRows ? `<section class="panel"><div class="panel-h"><h2>Audit log</h2></div><div class="panel-b flush tbl-wrap"><table class="t"><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th></tr></thead><tbody>
      ${auditRows.map(a => `<tr><td>${dt(a.at)}</td><td>${esc(a.actor)}</td><td class="mono small">${esc(a.action)}</td><td class="small">${esc(a.target)}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">No events yet.</td></tr>'}</tbody></table></div></section>` : ""}`;
  const f = root.querySelector("#of");
  if (!manageOrg) f.querySelectorAll("input,select").forEach(i => i.disabled = true);
  f.onsubmit = async (e) => {
    e.preventDefault();
    if (!f.reportValidity()) return;
    try { await api("/api/org", { method: "PUT", body: Object.fromEntries(new FormData(f)) }); toast("Profile saved"); await refreshMe(); }
    catch (err) { toast(err.message, true); }
  };
  if (members) wireMembers(members);
}

function membersHtml(m) {
  const lim = m.limits.users;
  return `<section class="panel"><div class="panel-h"><h2>Members</h2><span class="small muted">${m.usage.users}${lim ? ` of ${lim}` : ""} users including pending invitations</span></div>
    <div class="panel-b flush tbl-wrap"><table class="t"><thead><tr><th>Name</th><th>E-mail</th><th>Role</th><th>Technician record</th><th></th></tr></thead><tbody>
    ${m.members.map(u => `<tr><td>${esc(u.full_name)}${u.you ? " (you)" : ""}</td><td>${esc(u.email)}${u.email_verified ? "" : ' <span class="small muted">unverified</span>'}</td>
      <td>${u.role === "owner" || u.you ? esc(u.role_label) : `<select data-role="${u.id}" aria-label="Role of ${esc(u.full_name)}">${Object.entries(m.roles).filter(([k]) => k !== "owner").map(([k, v]) => `<option value="${k}" ${k === u.role ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`}</td>
      <td>${u.role === "technician" ? `<input data-tech="${u.id}" value="${esc(u.technician_id || "")}" placeholder="e.g. TECH-02" style="width:150px" aria-label="Technician ID">` : '<span class="muted small">—</span>'}</td>
      <td>${u.role !== "owner" && !u.you ? `<button class="sm danger" data-rm="${u.id}">Remove</button>` : ""}</td></tr>`).join("")}</tbody></table></div>
    <div class="panel-b"><h3 class="small" style="margin:0 0 6px">Invite a colleague</h3>
      <form id="inv" class="toolbar" style="flex-wrap:wrap"><input name="email" type="email" required placeholder="name@company.com" aria-label="E-mail" style="min-width:240px">
        <select name="role" aria-label="Role">${Object.entries(m.roles).filter(([k]) => k !== "owner").map(([k, v]) => `<option value="${k}" ${k === "viewer" ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>
        <button class="primary sm">Create invitation</button></form>
      <div id="invOut"></div>
      ${m.invitations.length ? `<h3 class="small" style="margin:12px 0 6px">Pending invitations</h3><ul class="small">${m.invitations.map(i => `<li>${esc(i.email)} · ${esc(i.role_label)} · expires ${dt(i.expires_at)} <button class="ghost sm" data-revoke="${i.id}">Revoke</button></li>`).join("")}</ul>` : ""}
      <p class="small muted">Technicians see operational pages and can update only tickets assigned to their linked technician record. Viewers have read-only access.</p></div></section>`;
}

function wireMembers(m) {
  root.querySelectorAll("[data-role]").forEach(s => s.onchange = async () => {
    try { await api(`/api/org/members/${s.dataset.role}`, { method: "PUT", body: { role: s.value } }); toast("Role updated"); render(root, ctx); }
    catch (e) { toast(e.message, true); render(root, ctx); }
  });
  root.querySelectorAll("[data-tech]").forEach(i => i.onchange = async () => {
    try { await api(`/api/org/members/${i.dataset.tech}`, { method: "PUT", body: { technician_id: i.value.trim() } }); toast("Technician record linked"); }
    catch (e) { toast(e.message, true); }
  });
  root.querySelectorAll("[data-rm]").forEach(b => b.onclick = async () => {
    const u = m.members.find(x => String(x.id) === b.dataset.rm);
    if (!(await confirmBox({ title: `Remove ${u.full_name}?`, body: "<p>They lose access to this organisation immediately. Their account and the audit trail are kept.</p>", confirmLabel: "Remove", danger: true }))) return;
    try { await api(`/api/org/members/${u.id}/remove`, { method: "POST" }); toast("Member removed"); render(root, ctx); } catch (e) { toast(e.message, true); }
  });
  root.querySelectorAll("[data-revoke]").forEach(b => b.onclick = async () => {
    try { await api(`/api/org/invitations/${b.dataset.revoke}/revoke`, { method: "POST" }); toast("Invitation revoked"); render(root, ctx); } catch (e) { toast(e.message, true); }
  });
  const f = root.querySelector("#inv");
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/org/invitations", { method: "POST", body: { email: f.email.value.trim(), role: f.role.value } });
      root.querySelector("#invOut").innerHTML = `<div class="note ok" style="margin-top:8px"><b>Invitation created.</b> ${esc(r.message)} ${esc(r.share_note)}
        <div class="toolbar" style="margin-top:6px"><input readonly value="${esc(r.invite_link)}" style="flex:1;min-width:260px" aria-label="Invitation link" id="invLink"><button class="sm" id="copyInv" type="button">Copy link</button></div></div>`;
      root.querySelector("#copyInv").onclick = async () => { try { await navigator.clipboard.writeText(r.invite_link); toast("Link copied"); } catch { root.querySelector("#invLink").select(); } };
      f.reset();
    } catch (err) { toast(err.message, true); }
  };
}

export function unmount() {}
export { errorBox, state };
