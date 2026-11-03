// Platform administration: organisations, subscriptions, plan catalogue, billing policy, enquiries, audit.
// The server allows these endpoints only for platform administrators; every action needs a reason and is audited.
import { api } from "../api.js";
import { esc, toast, errorBox, dt } from "../ui.js";
import { promptBox } from "../drawers.js";

let root, ctx, tab = "orgs";

export async function render(el, c) {
  root = el; ctx = c;
  root.innerHTML = `<div class="seg-btns" role="tablist" style="margin-bottom:12px">${[["orgs", "Organisations"], ["plans", "Plans & policy"], ["enq", "Enquiries"], ["audit", "Audit"]]
    .map(([k, v]) => `<button class="sm" role="tab" data-tab="${k}" aria-pressed="${k === tab}">${v}</button>`).join("")}</div><div id="adm"><div class="loading">Loading…</div></div>`;
  root.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => { tab = b.dataset.tab; render(root, ctx); });
  const box = root.querySelector("#adm");
  try { await ({ orgs, plans, enq, audit })[tab](box); } catch (e) { box.innerHTML = errorBox(e.message); }
}

async function reason(title, extra = "") {
  const r = await promptBox({ title, label: "Reason (recorded in the audit log, at least 5 characters)", extra });
  if (!r) return null;
  if (r.text.length < 5) { toast("Give a reason of at least 5 characters.", true); return null; }
  return r;
}

async function orgs(box) {
  const [ov, rows] = await Promise.all([api("/api/admin/overview"), api("/api/admin/organisations")]);
  box.innerHTML = `<div class="kpis">${[["Organisations", ov.organisations], ["Users", ov.users], ["Verified live connections", ov.verified_connections], ["New enquiries", ov.enquiries_new]]
    .map(([k, v]) => `<div class="kpi"><span class="lbl">${esc(k)}</span><span class="val">${v}</span></div>`).join("")}</div>
    <p class="small muted">${esc(ov.note)}</p>
    <section class="panel"><div class="panel-b flush tbl-wrap"><table class="t"><thead><tr><th>Organisation</th><th>Owner</th><th>Plan</th><th>State</th><th>Data</th><th class="r">Chargers</th><th class="r">Users</th><th></th></tr></thead><tbody>
    ${rows.map(o => `<tr><td><b>${esc(o.name)}</b> <span class="mono small muted">${esc(o.code)}</span>${o.is_system_demo ? ' <span class="small muted">(system demo)</span>' : ""}<div class="small muted">${esc(o.org_type)} · ${dt(o.created_at)}</div></td>
      <td class="small">${esc(o.owner || "—")}</td><td>${esc(o.plan)}</td><td><span class="plan-chip plan-${esc(o.state)}">${esc(o.state_label)}</span>${o.suspended_reason ? `<div class="small muted">${esc(o.suspended_reason)}</div>` : ""}</td>
      <td class="small">${o.data_modes.demo ? "Demo (SIMULATED)" : ""}${o.data_modes.live_connections.map(c => `<div>${esc(c.kind)}: ${esc(c.state)}</div>`).join("") || (o.data_modes.demo ? "" : "No data source yet")}</td>
      <td class="r num">${o.usage.chargers}</td><td class="r num">${o.usage.users}</td>
      <td style="white-space:nowrap">${o.state === "suspended" ? `<button class="sm" data-act="reactivate" data-id="${o.id}">Reactivate</button>` : `<button class="sm danger" data-act="suspend" data-id="${o.id}">Suspend</button>`}
        <button class="sm" data-act="detail" data-id="${o.id}">Details</button></td></tr>`).join("")}</tbody></table></div></section><div id="detail"></div>`;
  box.querySelectorAll("[data-act]").forEach(b => b.onclick = async () => {
    const id = b.dataset.id;
    if (b.dataset.act === "detail") return detail(box.querySelector("#detail"), id);
    const r = await reason(b.dataset.act === "suspend" ? "Suspend this organisation (its workspace becomes read-only)?" : "Reactivate this organisation?");
    if (!r) return;
    try { await api(`/api/admin/organisations/${id}/${b.dataset.act}`, { method: "POST", body: { reason: r.text } }); toast("Done"); render(root, ctx); } catch (e) { toast(e.message, true); }
  });
}

async function detail(box, id) {
  const d = await api(`/api/admin/organisations/${id}`);
  box.innerHTML = `<section class="panel"><div class="panel-h"><h2>${esc(d.name)}</h2></div><div class="panel-b stack" style="gap:10px">
    <div class="toolbar" style="flex-wrap:wrap">
      <button class="sm" data-t="reach_renewal_date">Simulate: renewal date reached</button>
      <button class="sm" data-t="end_grace_period">Simulate: grace period ended</button>
      <button class="sm" data-t="expire_checkouts">Simulate: pending checkouts expired</button>
      <button class="sm" data-s="grant_trial">Grant 14-day Growth trial</button>
      <button class="sm" data-s="assign_enterprise">Record Enterprise contract…</button>
      <button class="sm" data-s="set_demo">Return to Demo plan</button></div>
    <p class="small muted">Time simulation moves the subscription's own dates to now so expiry and grace behaviour can be demonstrated. It is recorded in the audit log and never invents a payment.</p>
    <div class="grid-2"><div><h3 class="small">Subscription events</h3><ul class="timeline small">${d.subscription_events.map(e => `<li><b>${esc(e.kind)}</b> ${e.to ? `(${esc(e.from || "—")} → ${esc(e.to)})` : ""} · ${dt(e.at)} · ${esc(e.actor)}<div class="muted">${esc(e.note)}</div></li>`).join("")}</ul></div>
      <div><h3 class="small">Account and organisation events</h3><ul class="timeline small">${d.audit.map(a => `<li><span class="mono">${esc(a.action)}</span> · ${dt(a.at)} · ${esc(a.actor)}</li>`).join("") || "<li>None</li>"}</ul></div></div></div></section>`;
  box.querySelectorAll("[data-t]").forEach(b => b.onclick = async () => {
    const r = await reason(b.textContent); if (!r) return;
    try { const out = await api(`/api/admin/organisations/${id}/simulate-time`, { method: "POST", body: { action: b.dataset.t, reason: r.text } }); toast(`Applied: ${out.applied.join(", ") || "nothing due"}`); render(root, ctx); }
    catch (e) { toast(e.message, true); }
  });
  box.querySelectorAll("[data-s]").forEach(b => b.onclick = async () => {
    const body = { action: b.dataset.s };
    const ent = b.dataset.s === "assign_enterprise";
    const extra = ent ? `<div class="form-grid" style="margin-top:8px"><label class="field"><span>Chargers</span><input id="lc" type="number" min="0" value="500"></label>
      <label class="field"><span>Users</span><input id="lu" type="number" min="0" value="40"></label><label class="field"><span>Sites</span><input id="ls" type="number" min="0" value="30"></label><label class="field"><span>Integrations</span><input id="li" type="number" min="0" value="8"></label>
      <label class="field"><span>Monthly price ₹ (optional, demo)</span><input id="lp" type="number" min="0"></label></div>` : "";
    const r = await reason(b.textContent, extra); if (!r) return;
    body.reason = r.text;
    if (ent) {
      const q = (s) => r.extra.querySelector(s).value;
      body.limits = { chargers: parseInt(q("#lc"), 10), users: parseInt(q("#lu"), 10), sites: parseInt(q("#ls"), 10), integrations: parseInt(q("#li"), 10) };
      if (q("#lp")) body.monthly_price_paise = Math.round(Number(q("#lp")) * 100);
    }
    try { await api(`/api/admin/organisations/${id}/subscription`, { method: "POST", body }); toast("Subscription updated"); render(root, ctx); } catch (e) { toast(e.message, true); }
  });
}

async function plans(box) {
  const d = await api("/api/admin/plans");
  box.innerHTML = `<div class="note">Prices, discounts, limits and features are edited here centrally; the public pricing page, checkout and entitlement checks all read the same catalogue. Prices are prototype assumptions.</div>
    ${d.plans.map(p => `<section class="panel"><div class="panel-h"><h2>${esc(p.name)} <span class="mono small muted">${esc(p.key)}</span></h2></div><div class="panel-b">
      <form class="stack" data-plan="${p.key}" style="gap:8px"><div class="form-grid">
        <label class="field"><span>Monthly price (₹)</span><input name="price" type="number" min="0" step="1" value="${p.monthly_price_paise === null ? "" : p.monthly_price_paise / 100}" ${p.key === "enterprise" || p.key === "demo" ? "disabled" : ""}></label>
        <label class="field"><span>Charger limit</span><input name="chargers" type="number" min="0" value="${p.limits.chargers ?? ""}" placeholder="negotiated"></label>
        <label class="field"><span>User limit</span><input name="users" type="number" min="0" value="${p.limits.users ?? ""}" placeholder="negotiated"></label>
        <label class="field"><span>Site limit</span><input name="sites" type="number" min="0" value="${p.limits.sites ?? ""}" placeholder="negotiated"></label>
        <label class="field"><span>Integration limit</span><input name="integrations" type="number" min="0" value="${p.limits.integrations ?? ""}" placeholder="negotiated"></label></div>
        <details><summary class="small">Features (${p.features.length})</summary><div class="choice-grid" style="margin-top:6px">${Object.entries(d.feature_labels).map(([k, v]) => `<label class="check small"><input type="checkbox" name="feat" value="${k}" ${p.features.includes(k) ? "checked" : ""}> ${esc(v)}</label>`).join("")}</div></details>
        <div><button class="sm primary">Save ${esc(p.name)}</button></div></form></div></section>`).join("")}
    <section class="panel"><div class="panel-h"><h2>Billing policy</h2></div><div class="panel-b"><form id="pol" class="toolbar" style="flex-wrap:wrap">
      <label class="field" style="width:130px"><span>6-month discount %</span><input name="d6" type="number" min="0" max="50" step="0.5" value="${d.policy.discount_bps["6"] / 100}"></label>
      <label class="field" style="width:130px"><span>12-month discount %</span><input name="d12" type="number" min="0" max="50" step="0.5" value="${d.policy.discount_bps["12"] / 100}"></label>
      <label class="field" style="width:110px"><span>Grace days</span><input name="grace" type="number" min="0" max="60" value="${d.policy.grace_days}"></label>
      <label class="field" style="width:140px"><span>Checkout valid (min)</span><input name="ttl" type="number" min="5" max="1440" value="${d.policy.checkout_ttl_min}"></label>
      <button class="sm primary">Save policy</button></form></div></section>`;
  box.querySelectorAll("[data-plan]").forEach(f => f.onsubmit = async (e) => {
    e.preventDefault();
    const n = (v) => v === "" ? null : parseInt(v, 10);
    const body = { limits: { chargers: n(f.chargers.value), users: n(f.users.value), sites: n(f.sites.value), integrations: n(f.integrations.value) },
      features: [...f.querySelectorAll("[name=feat]:checked")].map(i => i.value) };
    if (!f.price.disabled && f.price.value !== "") body.monthly_price_paise = Math.round(Number(f.price.value) * 100);
    const r = await reason(`Update the ${f.dataset.plan} plan?`); if (!r) return;
    body.reason = r.text;
    try { await api(`/api/admin/plans/${f.dataset.plan}`, { method: "PUT", body }); toast("Plan saved"); } catch (err) { toast(err.message, true); }
  });
  const pf = box.querySelector("#pol");
  pf.onsubmit = async (e) => {
    e.preventDefault();
    const body = { discount_bps: { "6": Math.round(Number(pf.d6.value) * 100), "12": Math.round(Number(pf.d12.value) * 100) },
      grace_days: Number(pf.grace.value), checkout_ttl_min: Number(pf.ttl.value) };
    const r = await reason("Update the billing policy?"); if (!r) return;
    body.reason = r.text;
    try { await api("/api/admin/billing-policy", { method: "PUT", body }); toast("Policy saved"); } catch (err) { toast(err.message, true); }
  };
}

async function enq(box) {
  const rows = await api("/api/admin/enquiries");
  box.innerHTML = rows.length ? `<section class="panel"><div class="panel-b flush tbl-wrap"><table class="t"><thead><tr><th>Ref</th><th>Received</th><th>Organisation</th><th>Contact</th><th class="r">Chargers</th><th class="r">Sites</th><th>Integration needs / message</th></tr></thead><tbody>
    ${rows.map(e => `<tr><td class="mono">${esc(e.reference)}</td><td>${dt(e.created_at)}</td><td>${esc(e.org_name)} <span class="small muted">${esc(e.kind)}</span></td>
      <td class="small">${esc(e.contact_name)}<br>${esc(e.contact_email)}${e.phone ? "<br>" + esc(e.phone) : ""}</td><td class="r num">${e.charger_count ?? "—"}</td><td class="r num">${e.site_count ?? "—"}</td>
      <td class="small">${esc(e.integration_requirements)}${e.message ? `<div class="muted">${esc(e.message)}</div>` : ""}</td></tr>`).join("")}</tbody></table></div></section>`
    : '<div class="empty">No enquiries yet. They arrive from the Enterprise and Contact pages.</div>';
}

async function audit(box) {
  const rows = await api("/api/admin/events");
  box.innerHTML = `<section class="panel"><div class="panel-b flush tbl-wrap"><table class="t"><thead><tr><th>When</th><th>Org</th><th>Actor</th><th>Action</th><th>Target</th></tr></thead><tbody>
    ${rows.map(a => `<tr><td>${dt(a.at)}</td><td>${a.org_id ?? "—"}</td><td class="small">${esc(a.actor)}</td><td class="mono small">${esc(a.action)}</td><td class="small">${esc(a.target)}</td></tr>`).join("")}</tbody></table></div></section>`;
}

export function unmount() {}
