// Organisation connections (operator REST API and OCPP). States come from the server; "Connected / verified"
// appears only after a successful connection test or a real charge point's BootNotification.
// The credential field is write-only: the stored value is never sent back to the browser.
import { api } from "../api.js";
import { esc, dt, toast, confirmBox } from "../ui.js";
import { can } from "../app.js";

const PILL = { not_configured: "st-offline", draft: "st-offline", awaiting_credentials: "st-unavailable", configured: "st-unavailable", testing: "st-charging",
  connected: "st-available", auth_failed: "st-faulted", connection_failed: "st-faulted", permission_insufficient: "st-faulted", stale: "st-unavailable", disabled: "st-offline" };

export async function mountConnections(el, onChange) {
  let d;
  try { d = await api("/api/org/integrations"); } catch (e) { el.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; return; }
  const manage = can("integrations.manage");
  const atLimit = d.limit !== null && d.limit !== undefined && d.usage >= d.limit;
  el.innerHTML = `
    <div class="toolbar" style="justify-content:space-between;flex-wrap:wrap">
      <span class="small muted">${d.usage}${d.limit === null || d.limit === undefined ? "" : ` of ${d.limit}`} connection(s) used by your plan.${d.entitled ? "" : " Your plan does not include configured integrations (operator API or OCPP); choose Growth or above in Subscription & billing. CSV imports are available from Starter."}</span>
      ${manage && d.entitled ? `<form id="newConn" class="toolbar"><select name="kind" aria-label="Connection type"><option value="operator_api">Operator REST API</option><option value="ocpp">OCPP 1.6J charge points</option></select>
        <input name="name" required minlength="2" maxlength="80" placeholder="Connection name" aria-label="Connection name"><button class="primary sm" ${atLimit ? "disabled title=\"Plan limit reached\"" : ""}>Add connection</button></form>` : ""}
    </div>
    ${d.connections.length ? d.connections.map(c => card(c, manage)).join("") : `<div class="empty">No connections yet. ${d.entitled ? "Add one when you have the operator's authorisation and credentials; until then everything stays Not configured." : ""}</div>`}
    <details style="margin-top:8px"><summary class="small">What you need before connecting</summary>
      <div class="grid-2 small"><div><b>Operator REST API</b><ul>${d.requirements.operator_api.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>
      <div><b>OCPP</b><ul>${d.requirements.ocpp.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div></div></details>`;
  const nf = el.querySelector("#newConn");
  if (nf) nf.onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/org/integrations", { method: "POST", body: { kind: nf.kind.value, name: nf.name.value.trim() } }); toast("Connection added (not configured yet)"); mountConnections(el, onChange); onChange && onChange(); }
    catch (err) { toast(err.message, true); }
  };
  d.connections.forEach(c => wire(el, c, () => { mountConnections(el, onChange); onChange && onChange(); }));
}

function card(c, manage) {
  const dis = c.state === "disabled";
  const ro = !manage || dis ? "disabled" : "";
  const head = `<div class="toolbar" style="justify-content:space-between"><b>${esc(c.name)}</b><span class="pill ${PILL[c.state] || "st-offline"}">${esc(c.state_label)}</span></div>
    ${c.next_step ? `<div class="small muted">${esc(c.next_step)}</div>` : ""}
    ${c.last_test_at ? `<div class="small">Last test ${dt(c.last_test_at)}: ${esc(c.last_test_message)}</div>` : ""}`;
  const authorised = `<label class="check small"><input type="checkbox" name="operator_authorised" ${c.operator_authorised ? "checked" : ""} ${ro}> The operator has authorised ChargeSight in writing to access this data</label>
    <label class="field"><span>Authorisation reference (optional)</span><input name="authorisation_note" maxlength="1000" value="${esc(c.authorisation_note)}" ${ro}></label>`;
  const actions = manage ? `<div class="toolbar">
      ${dis ? "" : '<button class="primary sm">Save</button>'}
      ${dis ? "" : `<button type="button" class="sm" data-test="${c.id}">Test connection</button>`}
      ${c.kind === "operator_api" && (c.state === "connected" || c.state === "stale") ? `<button type="button" class="sm" data-sync="${c.id}">Sync events</button>` : ""}
      ${dis ? `<button type="button" class="sm" data-enable="${c.id}">Enable</button>` : `<button type="button" class="sm danger" data-disable="${c.id}">Disable</button>`}</div>` : "";
  if (c.kind === "ocpp") {
    const o = c.ocpp;
    return `<form class="panel" data-conn="${c.id}" style="padding:12px;margin:10px 0">${head}
      <dl class="kv" style="margin-top:8px"><div><dt>Endpoint for your charge points</dt><dd class="mono small">${esc(o.endpoint)}</dd></div>
        <div><dt>Protocol</dt><dd>${esc(o.ocpp_version)}</dd></div>
        <div><dt>Real charge points</dt><dd>${o.real_charge_points}</dd></div>
        <div><dt>Local test simulator</dt><dd>${o.test_charge_points} (SIMULATED OCPP, never counted as real)</dd></div>
        <div><dt>Last BootNotification</dt><dd>${dt(o.last_boot_at)}</dd></div><div><dt>Last heartbeat</dt><dd>${dt(o.last_heartbeat_at)}</dd></div>
        <div><dt>Last status notification</dt><dd>${dt(o.last_status_at)}</dd></div><div><dt>Connected right now</dt><dd>${o.connected_now.length ? o.connected_now.map(x => esc(x.id)).join(", ") : "none"}</dd></div>
        <div><dt>Stale (&gt; 12 min silent)</dt><dd>${o.stale ? "yes" : "no"}</dd></div></dl>
      <label class="field"><span>Allowed charge point identities (optional, comma-separated; others are refused)</span><input name="ocpp_allowed_ids" value="${esc((c.ocpp_allowed_ids || []).join(", "))}" ${ro}></label>
      ${authorised}${actions}<div class="small muted">The central system runs as a separate process: <span class="mono">python -m app.integrations.ocpp_central</span> (from backend/).</div><div data-out></div></form>`;
  }
  return `<form class="panel" data-conn="${c.id}" style="padding:12px;margin:10px 0">${head}
    <div class="form-grid" style="margin-top:8px">
      <label class="field"><span>Adapter</span><select name="adapter" ${ro}>${Object.entries({ [c.adapter]: c.adapter_label }).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("")}</select></label>
      <label class="field"><span>API base URL (https)</span><input name="base_url" type="url" maxlength="300" placeholder="https://api.operator.example/v1" value="${esc(c.base_url)}" ${ro}></label>
      <label class="field"><span>Authentication</span><select name="auth_method" ${ro}><option value="bearer">Bearer token (existing adapter)</option></select></label>
      <label class="field"><span>API credential ${c.credential.set ? `(stored ${dt(c.credential.set_at)}: ${c.credential.masked})` : "(not set)"}</span>
        <input name="credential" type="password" autocomplete="new-password" placeholder="${c.credential.set ? "Leave empty to keep the stored credential" : "Paste the read-only credential"}" ${ro}></label>
      <label class="field"><span>Timeout (s)</span><input name="timeout_s" type="number" min="1" max="30" value="${c.timeout_s}" ${ro}></label>
      <label class="field"><span>Polling (s, 0 = manual sync)</span><input name="poll_seconds" type="number" min="0" max="86400" value="${c.poll_seconds}" ${ro}></label>
    </div>
    <label class="field"><span>Data mapping notes / adapter configuration (JSON object, optional)</span><textarea name="mapping" rows="2" ${ro}>${esc(Object.keys(c.mapping || {}).length ? JSON.stringify(c.mapping) : "")}</textarea></label>
    ${authorised}
    <div class="small muted">Last successful check: ${dt(c.last_success_at)} · last data received: ${dt(c.last_data_at)}. Credentials are encrypted at rest and never shown again. Only https destinations on the public internet are allowed.</div>
    ${actions}<div data-out></div></form>`;
}

function wire(el, c, reload) {
  const f = el.querySelector(`[data-conn="${c.id}"]`);
  if (!f) return;
  const out = f.querySelector("[data-out]");
  f.onsubmit = async (e) => {
    e.preventDefault();
    const body = { operator_authorised: f.operator_authorised.checked, authorisation_note: f.authorisation_note.value };
    if (c.kind === "ocpp") body.ocpp_allowed_ids = f.ocpp_allowed_ids.value.split(",").map(s => s.trim()).filter(Boolean);
    else {
      Object.assign(body, { base_url: f.base_url.value.trim(), auth_method: f.auth_method.value, timeout_s: Number(f.timeout_s.value), poll_seconds: Number(f.poll_seconds.value) });
      if (f.credential.value) body.credential = f.credential.value;
      if (f.mapping.value.trim()) { try { body.mapping = JSON.parse(f.mapping.value); } catch { toast("Mapping must be valid JSON.", true); return; } }
    }
    try { await api(`/api/org/integrations/${c.id}`, { method: "PUT", body }); toast("Saved. Run a connection test to verify it."); reload(); }
    catch (err) { toast(err.message, true); }
  };
  const t = f.querySelector("[data-test]");
  if (t) t.onclick = async () => {
    t.disabled = true; t.textContent = "Testing…";
    try { const r = await api(`/api/org/integrations/${c.id}/test`, { method: "POST" }); toast(r.message, !r.ok); reload(); }
    catch (err) { toast(err.message, true); t.disabled = false; t.textContent = "Test connection"; }
  };
  const s = f.querySelector("[data-sync]");
  if (s) s.onclick = async () => {
    s.disabled = true;
    try { const r = await api(`/api/org/integrations/${c.id}/sync`, { method: "POST" }); out.innerHTML = `<div class="note ok">Received ${r.received}, accepted ${r.accepted}, duplicates ${r.duplicates}, rejected ${r.rejected}.</div>`; toast("Sync finished"); }
    catch (err) { toast(err.message, true); }
    s.disabled = false;
  };
  const dsb = f.querySelector("[data-disable]");
  if (dsb) dsb.onclick = async () => {
    if (!(await confirmBox({ title: `Disable ${c.name}?`, body: "<p>Data stops arriving through this connection. Stored records and the configuration are kept; you can enable it again.</p>", confirmLabel: "Disable", danger: true }))) return;
    try { await api(`/api/org/integrations/${c.id}/disable`, { method: "POST" }); reload(); } catch (err) { toast(err.message, true); }
  };
  const en = f.querySelector("[data-enable]");
  if (en) en.onclick = async () => { try { await api(`/api/org/integrations/${c.id}/enable`, { method: "POST" }); reload(); } catch (err) { toast(err.message, true); } };
}
