// Workspace setup wizard. Progress is stored on the server (PUT /api/org/onboarding), so refreshing or
// signing out never loses it. Status lines come from the server checklist: nothing is shown as
// "Connected" or "Imported" unless the server has verified it.
import { api } from "../api.js";
import { esc, tag, toast, errorBox } from "../ui.js";
import { state, can, refreshMe, sessionStorageSet } from "../app.js";

let root, ctx, data;

export async function render(el, c) {
  root = el; ctx = c;
  data = await api("/api/org/onboarding");
  if (!ctx.alive()) return;
  draw();
}

const step = () => data.state.step || 1;

async function save(body, msg) {
  const r = await api("/api/org/onboarding", { method: "PUT", body });
  data.state = r.state;
  data.checklist = r.checklist;
  if (msg) toast(msg);
}

function draw() {
  const ro = !can("org.manage");
  const s = step();
  root.innerHTML = `
    <ol class="wizard-steps" aria-label="Setup progress">${data.steps.map((name, i) => `<li class="${i + 1 === s ? "current" : ""} ${i + 1 < s || data.state.completed ? "done" : ""}" ${i + 1 === s ? 'aria-current="step"' : ""}>
      <b>${i + 1}. ${esc(name)}</b>${i + 1 < s || data.state.completed ? "Done" : i + 1 === s ? "In progress" : "To do"}</li>`).join("")}</ol>
    ${ro ? '<div class="note">Only organisation owners and administrators can change setup. You can view the checklist.</div>' : ""}
    <section class="panel"><div class="panel-b" id="stepBody"></div></section>`;
  const body = root.querySelector("#stepBody");
  ({ 1: stepProfile, 2: stepUseCase, 3: stepDataMode, 4: stepWorkspace, 5: stepVerify, 6: stepEnter })[s](body, ro);
}

function nav(prev, nextLabel = "Save and continue") {
  return `<div class="toolbar" style="justify-content:space-between;margin-top:16px">
    ${prev ? '<button type="button" id="back">Back</button>' : "<span></span>"}<button type="submit" class="primary" id="next">${esc(nextLabel)}</button></div>`;
}
function wireBack(body) {
  const b = body.querySelector("#back");
  if (b) b.onclick = async () => { await save({ step: step() - 1 }); draw(); };
}

function stepProfile(body, ro) {
  const o = data.organisation;
  body.innerHTML = `<h2 class="sub-h">Organisation profile</h2>
    <p class="muted">Tell us about your charging organisation. Estimates help size your plan; they can be changed later.</p>
    <form id="f" class="stack" style="gap:10px">
      <div class="form-grid">
        <label class="field"><span>Organisation name *</span><input name="name" required minlength="2" maxlength="120" value="${esc(o.name)}"></label>
        <label class="field"><span>Organisation type *</span><select name="org_type">${Object.entries({ cpo: "Charging Point Operator", fleet_operator: "EV fleet operator", service_provider: "Charging infrastructure service provider", site_owner: "Property / site owner", other: "Other" }).map(([k, v]) => `<option value="${k}" ${o.org_type === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
        <label class="field"><span>Country *</span><input name="country" required maxlength="60" value="${esc(o.country)}"></label>
        <label class="field"><span>Number of sites *</span><input name="sites_estimate" type="number" min="0" max="100000" required value="${o.sites_estimate ?? ""}"></label>
        <label class="field"><span>Estimated charger count *</span><input name="chargers_estimate" type="number" min="0" max="1000000" required value="${o.chargers_estimate ?? ""}"></label>
        <label class="field"><span>Primary contact name *</span><input name="primary_contact_name" required maxlength="120" value="${esc(o.primary_contact_name)}"></label>
        <label class="field"><span>Primary contact e-mail</span><input name="primary_contact_email" type="email" maxlength="254" value="${esc(o.primary_contact_email)}"></label>
      </div>${nav(false)}</form>`;
  const f = body.querySelector("#f");
  if (ro) f.querySelectorAll("input,select,button").forEach(i => i.disabled = true);
  f.onsubmit = async (e) => {
    e.preventDefault();
    if (!f.reportValidity()) return;
    const v = Object.fromEntries(new FormData(f));
    v.sites_estimate = Number(v.sites_estimate); v.chargers_estimate = Number(v.chargers_estimate);
    if (!v.primary_contact_email) delete v.primary_contact_email;
    try { await api("/api/org", { method: "PUT", body: v }); await save({ step: 2 }, "Profile saved"); await refreshMe(); draw(); }
    catch (err) { toast(err.message, true); }
  };
}

function stepUseCase(body, ro) {
  body.innerHTML = `<h2 class="sub-h">What will you use ChargeSight for?</h2>
    <p class="muted">Choose all that apply. This only tailors suggestions; every feature in your plan stays available.</p>
    <form id="f"><div class="choice-grid">${Object.entries(data.use_cases).map(([k, v]) => `<label class="choice"><input type="checkbox" name="uc" value="${k}" ${data.state.use_cases.includes(k) ? "checked" : ""}><span><b>${esc(v)}</b></span></label>`).join("")}</div>
    <p class="small muted" id="ucErr" role="alert"></p>${nav(true)}</form>`;
  const f = body.querySelector("#f");
  wireBack(body);
  if (ro) f.querySelectorAll("input,button[type=submit]").forEach(i => i.disabled = true);
  f.onsubmit = async (e) => {
    e.preventDefault();
    const uc = [...f.querySelectorAll("[name=uc]:checked")].map(i => i.value);
    if (!uc.length) { body.querySelector("#ucErr").textContent = "Choose at least one use case."; return; }
    try { await save({ use_cases: uc, step: 3 }); draw(); } catch (err) { toast(err.message, true); }
  };
}

const MODE_TEXT = {
  demo: ["Works immediately with synthetic data: 6 demo sites, 40 chargers, demo technicians and scenarios. Everything is labelled SIMULATED and kept separate from real records.", "SIMULATED"],
  csv: ["Upload an export from your operator system. Rows are validated against the ChargeSight event schema and you get a report of accepted and rejected rows. Marked imported only after a successful import.", "IMPORTED"],
  operator_api: ["Connect an authorised operator REST API. You need the operator's written authorisation, the API base URL, a read-only credential and an agreed data mapping. Shows Not configured until a connection test succeeds.", "LIVE"],
  ocpp: ["Point OCPP 1.6J charge points at your organisation's own endpoint on the ChargeSight central system. The local OCPP test simulator is always labelled as test traffic and never counts as a real charger.", "LIVE"],
};

function stepDataMode(body, ro) {
  const ent = state.me.entitlements;
  const mode = data.state.data_mode;
  const item = data.checklist.find(i => i.key === "data_mode");
  const noIntegrations = !ent.features.includes("integrations");
  const noImport = false;  // CSV import (historical, station inventory, operator events) is free on all plans
  body.innerHTML = `<h2 class="sub-h">Choose how data gets into ChargeSight</h2>
    <p class="muted">You can start with the demo and add real data later. No API key is needed to finish setup.</p>
    <p class="small muted">Only want to show ChargeSight to someone? The <b>Presentation Demo</b> (sidebar, under Workspace) is a separate SIMULATED workspace, so your own workspace stays free of sample data.</p>
    <form id="f"><div class="choice-grid">${Object.entries(data.data_modes).map(([k, v]) => `<label class="choice"><input type="radio" name="mode" value="${k}" ${mode === k ? "checked" : ""}>
      <span><b>${esc(v)}</b> ${tag(MODE_TEXT[k][1])}<br><span class="small muted">${esc(MODE_TEXT[k][0])}</span>
      ${k === "csv" && noImport ? '<br><span class="small" style="color:var(--warn)">Needs Starter or above.</span>' : ""}${(k === "operator_api" || k === "ocpp") && noIntegrations ? '<br><span class="small" style="color:var(--warn)">Needs Growth or above.</span>' : ""}</span></label>`).join("")}</div>
    <div id="modeDetail" style="margin-top:14px"></div>
    <div class="note" style="margin-top:12px"><b>Current status:</b> ${esc(item.status)}</div>${nav(true)}</form>`;
  const f = body.querySelector("#f");
  wireBack(body);
  const detail = body.querySelector("#modeDetail");
  const showDetail = (m) => {
    if (m === "demo") {
      detail.innerHTML = state.me.organisation.demo_workspace_ready
        ? `<div class="note ok">Your private simulated workspace is ready. ${tag("SIMULATED")}</div>`
        : `<button type="button" class="primary" id="mkDemo" ${ro ? "disabled" : ""}>Create my simulated workspace</button> <span class="small muted" id="mkMsg">Takes about 10–20 seconds.</span>`;
      const b = detail.querySelector("#mkDemo");
      if (b) b.onclick = async () => {
        b.disabled = true; b.textContent = "Creating…";
        detail.querySelector("#mkMsg").textContent = "Building 6 hours of simulated history…";
        try { const r = await api("/api/org/demo-workspace", { method: "POST" }); toast("Simulated workspace ready"); await refreshMe(); data = await api("/api/org/onboarding"); draw(); root.querySelector("#modeDetail").insertAdjacentHTML("afterbegin", `<div class="note ok">${esc(r.message)}</div>`); }
        catch (err) { toast(err.message, true); b.disabled = false; b.textContent = "Create my simulated workspace"; }
      };
    } else if (m === "csv") {
      detail.innerHTML = `<p class="small">Download the <a href="/api/integrations/samples/operator-events.csv">CSV schema and labelled synthetic sample</a>, then upload your export under <a href="#integrations">Data integrations → Import</a>. Come back here to see the validation result.</p>`;
    } else if (m === "operator_api") {
      detail.innerHTML = `<p class="small">Add the connection under <a href="#integrations">Data integrations → Connections</a>. Without credentials it stays <b>Not configured</b>; it shows <b>Connected</b> only after a successful connection test.</p>`;
    } else if (m === "ocpp") {
      detail.innerHTML = `<p class="small">Create an OCPP connection under <a href="#integrations">Data integrations → Connections</a> to get your endpoint URL (OCPP 1.6J, subprotocol <span class="mono">ocpp1.6</span>). It shows <b>Connected</b> only after a real charge point's BootNotification.</p>`;
    } else detail.innerHTML = "";
  };
  showDetail(mode);
  f.querySelectorAll("[name=mode]").forEach(r => r.onchange = () => showDetail(r.value));
  if (ro) f.querySelectorAll("input,button[type=submit]").forEach(i => i.disabled = true);
  f.onsubmit = async (e) => {
    e.preventDefault();
    const m = (f.querySelector("[name=mode]:checked") || {}).value;
    if (!m) { toast("Choose a data mode.", true); return; }
    try { await save({ data_mode: m, step: 4 }); draw(); } catch (err) { toast(err.message, true); }
  };
}

function stepWorkspace(body, ro) {
  const sla = data.sla_hours;
  const prefs = data.organisation.preferences || {};
  const al = prefs.alerts || { critical_tickets: true, sla_breaches: true, integration_failures: true };
  body.innerHTML = `<h2 class="sub-h">Configure the workspace</h2>
    <div class="grid-2">
      <div class="stack" style="gap:14px">
        <div><h3 class="small" style="margin:0 0 6px">Sites</h3>
          <p class="small muted">${esc(data.checklist.find(i => i.key === "sites").status)}. Register a real site (Starter or above; shown under Imported data) or use the simulated demo sites.</p>
          <form id="siteF" class="toolbar" style="flex-wrap:wrap"><input name="name" placeholder="Site name" required minlength="2" maxlength="120" aria-label="Site name"><input name="code" placeholder="Code (e.g. MGR)" required pattern="[A-Za-z0-9-]{2,16}" aria-label="Site code" style="width:130px"><button class="sm">Add site</button></form></div>
        <div><h3 class="small" style="margin:0 0 6px">Technicians and users</h3>
          <p class="small muted">${esc(data.checklist.find(i => i.key === "technicians").status)}. Manage them under <a href="#technicians">Technicians</a>; invite colleagues under <a href="#organisation">Organisation &amp; users</a> (invitation links are shown for you to share, because e-mail delivery is not configured).</p></div>
        <div><h3 class="small" style="margin:0 0 6px">Data retention</h3>
          <form id="retF" class="toolbar"><input name="retention_days" type="number" min="30" max="3650" value="${prefs.retention_days || 365}" aria-label="Retention days" style="width:110px"><span class="small">days</span><button class="sm">Save</button></form>
          <p class="small muted">Recorded preference only: automatic deletion is not implemented in this prototype, so nothing is deleted.</p></div>
      </div>
      <div class="stack" style="gap:14px">
        <div><h3 class="small" style="margin:0 0 6px">SLA policy (response deadlines)</h3>
          <form id="slaF" class="toolbar" style="flex-wrap:wrap">
            <label class="field" style="width:110px"><span>Critical (h)</span><input name="critical" type="number" step="0.5" min="0.5" value="${sla.critical}"></label>
            <label class="field" style="width:110px"><span>Major (h)</span><input name="major" type="number" step="0.5" min="0.5" value="${sla.major}"></label>
            <label class="field" style="width:110px"><span>Minor (h)</span><input name="minor" type="number" step="0.5" min="0.5" value="${sla.minor}"></label>
            <button class="sm">Confirm SLA policy</button></form></div>
        <div><h3 class="small" style="margin:0 0 6px">Alert preferences</h3>
          <form id="alF" class="stack" style="gap:4px">
            <label class="check"><input type="checkbox" name="critical_tickets" ${al.critical_tickets ? "checked" : ""}> New critical tickets</label>
            <label class="check"><input type="checkbox" name="sla_breaches" ${al.sla_breaches ? "checked" : ""}> SLA breaches and dispatch needed</label>
            <label class="check"><input type="checkbox" name="integration_failures" ${al.integration_failures ? "checked" : ""}> Integration failures</label>
            <p class="small muted">Alerts appear in the app only. E-mail and SMS are not configured, so nothing is sent outside ChargeSight.</p>
            <div><button class="sm">Save alert preferences</button></div></form></div>
      </div></div>
    <form id="f">${nav(true, "Continue to verification")}</form>`;
  wireBack(body);
  if (ro) body.querySelectorAll("input,button:not(#back)").forEach(i => i.disabled = true);
  const on = (id, fn) => { body.querySelector(id).onsubmit = async (e) => { e.preventDefault(); if (!e.target.reportValidity()) return; try { await fn(e.target); data = await api("/api/org/onboarding"); } catch (err) { toast(err.message, true); } }; };
  on("#siteF", async (f) => { await api("/api/org/sites", { method: "POST", body: { name: f.name.value.trim(), code: f.code.value.trim() } }); toast("Site registered"); f.reset(); });
  on("#retF", async (f) => { await api("/api/org", { method: "PUT", body: { retention_days: Number(f.retention_days.value) } }); toast("Retention preference saved"); });
  on("#slaF", async (f) => { await api("/api/org/sla", { method: "PUT", body: { critical: Number(f.critical.value), major: Number(f.major.value), minor: Number(f.minor.value) } }); toast("SLA policy confirmed"); });
  on("#alF", async (f) => { await api("/api/org", { method: "PUT", body: { alerts: { critical_tickets: f.critical_tickets.checked, sla_breaches: f.sla_breaches.checked, integration_failures: f.integration_failures.checked } } }); toast("Alert preferences saved"); });
  body.querySelector("#f").onsubmit = async (e) => { e.preventDefault(); try { await save({ step: 5 }); draw(); } catch (err) { toast(err.message, true); } };
}

function stepVerify(body) {
  body.innerHTML = `<h2 class="sub-h">Verify setup</h2>
    <p class="muted">Each line reflects what the server has actually recorded. Pending items do not block you; finish them any time.</p>
    <ul class="checklist">${data.checklist.map(i => `<li><span class="mark ${i.done ? "ok" : "pending"}" aria-hidden="true">${i.done ? "✓" : "•"}</span><span><b>${esc(i.label)}</b><br><span class="small muted">${esc(i.status)}</span></span><span class="sr-only">${i.done ? "complete" : "pending"}</span></li>`).join("")}</ul>
    <form id="f">${nav(true, "Continue")}</form>`;
  wireBack(body);
  body.querySelector("#f").onsubmit = async (e) => { e.preventDefault(); try { await save({ step: 6 }); draw(); } catch (err) { toast(err.message, true); } };
}

function stepEnter(body, ro) {
  const pending = data.checklist.filter(i => !i.done).length;
  body.innerHTML = `<h2 class="sub-h">${data.state.completed ? "Setup complete" : "Enter your workspace"}</h2>
    <p class="muted">${pending ? `${pending} item(s) are still pending; you can complete them later from <b>Settings &amp; setup</b> in the user menu.` : "Everything on the checklist is done."}</p>
    <form id="f">${nav(true, data.state.completed ? "Open the dashboard" : "Finish setup and open the dashboard")}</form>
    ${data.state.completed && !ro ? '<p class="small"><button type="button" class="ghost sm" id="reopen">Run setup again from step 1</button></p>' : ""}`;
  wireBack(body);
  const r = body.querySelector("#reopen");
  if (r) r.onclick = async () => { await save({ reopen: true }); await refreshMe(); draw(); };
  body.querySelector("#f").onsubmit = async (e) => {
    e.preventDefault();
    try {
      if (!data.state.completed && !ro) { await save({ complete: true }, "Setup complete"); sessionStorageSet("cs.onbDone", "1"); }
      await refreshMe();
      location.hash = "#dashboard";
    } catch (err) { toast(err.message, true); }
  };
}

export function unmount() {}
export { errorBox };
