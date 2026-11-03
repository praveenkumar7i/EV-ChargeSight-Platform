// The signed-in user's profile, password and sessions.
import { api } from "../api.js";
import { esc, toast, dt } from "../ui.js";
import { state, refreshMe } from "../app.js";

let root, ctx;

export async function render(el, c) {
  root = el; ctx = c;
  const sessions = await api("/api/auth/sessions");
  if (!ctx.alive()) return;
  const u = state.me.user;
  root.innerHTML = `
    ${state.me.organisation ? "" : '<div class="note warn">Your account is not a member of an active organisation. Ask an organisation owner for an invitation, or <a href="/signup">register an organisation</a>.</div>'}
    <div class="grid-2">
      <section class="panel"><div class="panel-h"><h2>Profile</h2></div><div class="panel-b">
        <form id="pf" class="stack" style="gap:10px"><label class="field"><span>Full name</span><input name="full_name" required minlength="2" maxlength="120" value="${esc(u.full_name)}"></label>
          <div class="small">E-mail: <b>${esc(u.email)}</b> ${u.email_verified ? "(verified)" : `(unverified) <button type="button" class="ghost sm" id="resend">Send verification link</button>`}</div>
          <div id="verOut" class="small muted"></div>
          <div><button class="primary sm">Save</button></div></form></div></section>
      <section class="panel"><div class="panel-h"><h2>Change password</h2></div><div class="panel-b">
        <form id="pw" class="stack" style="gap:8px">
          <label class="field"><span>Current password</span><input name="current_password" type="password" required autocomplete="current-password"></label>
          <label class="field"><span>New password</span><input name="new_password" type="password" required minlength="10" autocomplete="new-password"></label>
          <label class="field"><span>Repeat new password</span><input name="confirm_password" type="password" required autocomplete="new-password"></label>
          <p class="small muted">At least 10 characters with three of: lower case, upper case, digits, symbols (or 16+ characters). Other sessions are signed out.</p>
          <div><button class="primary sm">Change password</button></div></form></div></section>
    </div>
    <section class="panel"><div class="panel-h"><h2>Signed-in sessions</h2></div><div class="panel-b flush tbl-wrap"><table class="t"><thead><tr><th>Started</th><th>Last active</th><th>Expires</th><th>Browser</th><th></th></tr></thead><tbody>
      ${sessions.map(s => `<tr><td>${dt(s.created_at)}</td><td>${dt(s.last_seen_at)}</td><td>${dt(s.expires_at)}${s.remember ? " (remembered)" : ""}</td><td class="small">${esc(s.user_agent.slice(0, 70))}</td>
        <td>${s.current ? "This session" : `<button class="sm" data-rev="${s.id}">Sign out</button>`}</td></tr>`).join("")}</tbody></table></div></section>`;
  root.querySelector("#pf").onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/auth/profile", { method: "PUT", body: { full_name: e.target.full_name.value.trim() } }); toast("Saved"); await refreshMe(); } catch (err) { toast(err.message, true); }
  };
  const rs = root.querySelector("#resend");
  if (rs) rs.onclick = async () => { try { const r = await api("/api/auth/resend-verification", { method: "POST" }); root.querySelector("#verOut").textContent = r.message; } catch (err) { toast(err.message, true); } };
  root.querySelector("#pw").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    try { const r = await api("/api/auth/change-password", { method: "POST", body: Object.fromEntries(new FormData(f)) }); toast(r.message); f.reset(); render(root, ctx); }
    catch (err) { const fields = err.detail && err.detail.fields; toast(fields ? Object.values(fields).join(" ") : err.message, true); }
  };
  root.querySelectorAll("[data-rev]").forEach(b => b.onclick = async () => { try { await api(`/api/auth/sessions/${b.dataset.rev}/revoke`, { method: "POST" }); render(root, ctx); } catch (err) { toast(err.message, true); } });
}

export function unmount() {}
