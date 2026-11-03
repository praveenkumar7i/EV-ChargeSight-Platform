// Subscription & Billing — ChargeSight v10.2
// Professional SaaS subscription management, modern payment checkout, and billing history.
import { api } from "../api.js";
import { esc, toast, errorBox, confirmBox } from "../ui.js";
import { state, refreshMe } from "../app.js";

let root, ctx, data, months = 12;
const fmtDate = (iso) => iso ? new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "—";
const fmtDateTime = (iso) => iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
const planDisplayName = (key, name) => key === "demo" ? "Free Plan" : (name || key);

// ------------------------------------------------------------------ Official & Neutral Brand SVGs
const LOGO_GPAY = `<svg class="brand-logo-svg" viewBox="0 0 48 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Google Pay">
  <path d="M7.4 9.8v4.9H5.7V1.7h4.4c1.1 0 2.1.4 2.8 1.1.8.7 1.2 1.7 1.2 2.7 0 1.1-.4 2-1.2 2.8-.7.7-1.7 1.1-2.8 1.1H7.4v.4zm0-6.6v5.2h2.8c.7 0 1.3-.2 1.8-.7.5-.5.7-1.1.7-1.9s-.2-1.4-.7-1.9c-.5-.5-1.1-.7-1.8-.7H7.4z" fill="#fff"/>
  <path d="M17.8 6.4c1.2 0 2.2.3 2.9 1 .7.7 1.1 1.6 1.1 2.8v4.5h-1.6v-1.2h-.1c-.7 1-1.6 1.5-2.7 1.5-1 0-1.8-.3-2.4-.8-.6-.6-1-1.3-1-2.1 0-.9.3-1.6 1-2.2.7-.5 1.6-.8 2.8-.8 1 0 1.8.2 2.4.5v-.4c0-.6-.2-1.1-.7-1.5-.5-.4-1.1-.6-1.7-.6-.9 0-1.7.4-2.2 1.2l-1.5-.9c.8-1.3 2-1.9 3.6-1.9zm-2.2 5.8c0 .4.2.8.5 1.1.4.3.8.4 1.3.4.7 0 1.4-.3 1.9-.8.6-.5.8-1.1.8-1.8-.5-.4-1.2-.6-2.1-.6-.6 0-1.2.2-1.6.5-.5.3-.8.7-.8 1.2z" fill="#fff"/>
  <path d="M29.2 6.7l-5.6 12.8H22l2.1-4.5-3.7-8.3h1.8l2.8 6.5h.1l2.7-6.5h1.7z" fill="#fff"/>
  <path d="M38.8 8.4c0-.6-.1-1.1-.2-1.6h-7.8v3.1h4.5c-.2 1-.8 1.9-1.6 2.5v2.1h2.6c1.5-1.4 2.5-3.5 2.5-6.1z" fill="#4285F4"/>
  <path d="M30.8 16.5c2.3 0 4.2-.8 5.6-2.1l-2.6-2.1c-.8.5-1.8.8-3 .8-2.3 0-4.3-1.6-5-3.7h-2.7v2.1c1.4 2.8 4.3 5 7.7 5z" fill="#34A853"/>
  <path d="M25.8 9.4c-.2-.7-.3-1.4-.3-2.1s.1-1.4.3-2.1V3.1h-2.7C22.5 4.3 22 5.7 22 7.3s.5 3 1.4 4.2l2.4-2.1z" fill="#FBBC04"/>
  <path d="M30.8 4.4c1.2 0 2.4.4 3.3 1.3l2.5-2.5C35 1.8 33.1 1 30.8 1 27.4 1 24.5 3.2 23.1 6l2.7 2.1c.7-2.1 2.7-3.7 5-3.7z" fill="#EA4335"/>
</svg>`;

const LOGO_PHONEPE = `<svg class="brand-logo-svg" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="PhonePe">
  <rect width="28" height="28" rx="6" fill="#5f259f"/>
  <path d="M19.5 8h-6.2c-1.5 0-2.8 1.2-2.8 2.7v1.8H9v2.5h1.5v6.5h2.8V15h2.2c2.5 0 4.5-2 4.5-4.5 0-1.4-1.1-2.5-2.5-2.5zm-2.2 4.5h-2.2v-2h2.2c.6 0 1 .4 1 1s-.4 1-1 1z" fill="#fff"/>
  <path d="M16 15l4.5 6.5h-3.2L13.8 15H16z" fill="#fff"/>
</svg>`;

const LOGO_PAYTM = `<svg class="brand-logo-svg" viewBox="0 0 54 18" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Paytm">
  <path d="M6.2 2.5C3.3 2.5 1 4.7 1 7.6c0 1.9 1 3.5 2.5 4.3v4.1h2.7v-2.3h.1c3 0 5.3-2.3 5.3-5.2 0-3.3-2.4-6-5.4-6zm0 7.8c-1.4 0-2.6-1.1-2.6-2.5s1.2-2.5 2.6-2.5 2.6 1.1 2.6 2.5c-.1 1.4-1.2 2.5-2.6 2.5zm11.3-.9c-1.8 0-3 .9-3.5 2.1h-.1V3.2h-2.6v12.7h2.6v-1.6h.1c.5 1.1 1.7 1.9 3.5 1.9 2.5 0 4.5-2 4.5-4.8 0-2.7-2-4.8-4.5-4.8zm-.6 7.2c-1.4 0-2.4-1-2.4-2.4s1-2.4 2.4-2.4 2.4 1 2.4 2.4c0 1.3-1 2.4-2.4 2.4zm10.7-7.2l-3 8.3h-.1l-3-8.3h-2.8l4.4 11.2-2 4.3h2.7l7.7-15.5h-2.9z" fill="#002E6E"/>
  <path d="M35.6 5.8h-2.8V3.2h2.8V.8h2.6v2.4h3.6v2.6h-3.6v7.3c0 1 .4 1.4 1.4 1.4h2.2v2.4h-3.1c-2.6 0-4.1-1.2-4.1-3.6V5.8zm9.5 3.6c.5-1.1 1.7-1.9 3.3-1.9 1.8 0 3 .8 3.5 2.1h.1c.5-1.2 1.7-2.1 3.4-2.1 2.1 0 3.8 1.4 3.8 4v7.3h-2.6v-6.6c0-1.3-.7-2-1.8-2s-1.9.8-1.9 2.1v6.5h-2.6v-6.6c0-1.3-.7-2-1.8-2s-1.9.8-1.9 2.1v6.5h-2.6V7.6h2.5v1.8h.1z" fill="#00BAF2"/>
</svg>`;

const LOGO_BHIM = `<svg class="brand-logo-svg" viewBox="0 0 52 20" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="BHIM">
  <path d="M2.5 3h7.2c2.2 0 3.7 1 3.7 2.7 0 1.1-.6 2-1.6 2.4 1.3.4 2.1 1.5 2.1 2.8 0 2-1.6 3.1-4 3.1H2.5V3zm3.1 4.5h3.8c.8 0 1.4-.4 1.4-1.1 0-.7-.6-1.1-1.4-1.1H5.6v2.2zm0 4.2h4.1c.9 0 1.6-.4 1.6-1.2 0-.8-.7-1.2-1.6-1.2H5.6v2.4zm11.7-8.7h3.1v4.3h5.2V3h3.1v11h-3.1V9.7h-5.2V14h-3.1V3zm15.1 0h3.1v11h-3.1V3zm6.6 0h3.8l3.6 7.2 3.6-7.2h3.8v11h-3v-6.8l-3.3 6.8h-2.2l-3.3-6.8V14H40V3z" fill="#00843D"/>
  <path d="M1 18.5h50v1.5H1z" fill="#FF9933"/>
</svg>`;

const LOGO_VISA = `<svg class="brand-logo-svg" viewBox="0 0 48 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Visa">
  <path d="M18.8 1.2L12.3 16h-4L5 3.5c-.3-1.1-.6-1.5-1.5-2C2 1 0.8.6 0 .4L.1 0h6.8c.9 0 1.7.6 1.9 1.6l1.6 8.7L14.6 0h4.2l-0.1 1.2zm16.5 9.4c0-3.7-5.1-3.9-5-5.6 0-.5.5-1 1.6-1.1 1.9-.1 3.6.4 4.7 1l.8-3.8C36.1.6 34.3.2 32.2.2c-4.4 0-7.5 2.3-7.5 5.7 0 2.5 2.2 3.9 3.9 4.7 1.8.8 2.4 1.4 2.4 2.1 0 1.2-1.4 1.7-2.7 1.7-2.3 0-4.1-.5-5.4-1.2l-.8 3.9c1.2.6 3.4 1 5.6 1 4.7 0 7.8-2.3 7.8-5.8zm12.3 5.4h3.6L48 .2h-3.4c-.8 0-1.4.2-1.7 1.1L36.8 16h4.1l.8-2.3h5l.7 2.3zm-4.3-5.5l2-5.6.8 2.6.4 3h-3.2zm-19.8-9.3l-3.2 14.8h-3.9L19.6 1.2h3.9z" fill="#2563EB"/>
</svg>`;

const LOGO_MASTERCARD = `<svg class="brand-logo-svg" viewBox="0 0 36 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="Mastercard">
  <circle cx="13" cy="12" r="11" fill="#EB001B"/>
  <circle cx="23" cy="12" r="11" fill="#F79E1B"/>
  <path d="M18 4.2a10.95 10.95 0 010 15.6 10.95 10.95 0 010-15.6z" fill="#FF5F00"/>
</svg>`;

const LOGO_RUPAY = `<svg class="brand-logo-svg" viewBox="0 0 56 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="RuPay">
  <path d="M3.5 1.5h8.2c3.2 0 5.3 1.8 5.3 4.5 0 2.2-1.4 3.7-3.4 4.2l3.8 5.8h-4.3L9.6 10.6H6.8V16H3.5V1.5zm3.3 6.4h4.7c1.4 0 2.3-.7 2.3-1.8 0-1.1-.9-1.8-2.3-1.8H6.8v3.6z" fill="#094982"/>
  <path d="M19.2 6.5h3.2v6.1c0 1.3.8 2 2.1 2s2.1-.7 2.1-2V6.5h3.2v6.2c0 3-1.9 4.7-5.3 4.7s-5.3-1.7-5.3-4.7V6.5z" fill="#094982"/>
  <path d="M32.5 6.5h3.1v1.5h.1c.6-1.1 1.8-1.7 3.2-1.7 2.7 0 4.6 2 4.6 4.9s-1.9 4.9-4.6 4.9c-1.4 0-2.6-.7-3.2-1.8h-.1V19h-3.1V6.5zm3.1 5c0 1.4 1 2.3 2.3 2.3s2.3-.9 2.3-2.3c0-1.4-1-2.3-2.3-2.3-1.3 0-2.3.9-2.3 2.3z" fill="#094982"/>
  <path d="M46.8 11.2c.1 1.7 1.4 2.6 3 2.6 1.1 0 2.1-.4 2.8-1.1l1.8 1.8c-1.1 1.2-2.7 1.8-4.7 1.8-3.4 0-5.7-2.3-5.7-5.4 0-3.1 2.3-5.4 5.6-5.4 3.4 0 5.4 2.3 5.4 5.3v.4h-8.2zm5.4-1.9c-.2-1.3-1.2-2.1-2.6-2.1-1.3 0-2.4.8-2.7 2.1h5.3z" fill="#094982"/>
  <path d="M53.5 1.5l2.5 4-2.5 4h-2.5l2.5-4-2.5-4h2.5z" fill="#F47920"/>
</svg>`;

const LOGO_NETBANKING = `<svg class="brand-logo-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-label="Net Banking">
  <path d="M3 21h18M3 10h18M5 6l7-3 7 3M4 10v11M20 10v11M8 14v4M12 14v4M16 14v4"/>
</svg>`;

const LOGO_UPI_GENERIC = `<svg class="brand-logo-svg" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-label="UPI">
  <path d="M12 2L3 9h4v11h10V9h4L12 2z" fill="#2563EB"/>
  <path d="M8 12l4-4 4 4" stroke="#fff" stroke-width="2" stroke-linecap="round"/>
</svg>`;

export async function render(el, c) {
  root = el; ctx = c;
  await load();
}

async function load() {
  try { data = await api("/api/billing"); } catch (e) { root.innerHTML = errorBox(e.message); return; }
  if (!ctx.alive()) return;
  draw();
}

function usageRow(label, used, limit) {
  const pct = limit ? Math.min(100, Math.round(100 * used / limit)) : 0;
  return `<div><div class="toolbar" style="justify-content:space-between"><span>${esc(label)}</span><b class="num">${used}${limit === null || limit === undefined ? " (negotiated)" : ` / ${limit}`}</b></div>
    ${limit ? `<div class="usage-bar" role="img" aria-label="${used} of ${limit}"><i class="${used >= limit ? "full" : ""}" style="width:${pct}%"></i></div>` : ""}</div>`;
}

function draw() {
  const s = data.subscription, ent = data.entitlements, manage = data.can_manage;
  const open = data.open_checkout;
  root.innerHTML = `
    ${state.me.organisation && state.me.organisation.workspace_kind === "presentation" ? '<div class="note" style="margin-bottom:12px">You are currently viewing the <b>Presentation Demo</b> workspace. Subscriptions managed here apply to this demonstration workspace.</div>' : ""}
    <div class="grid-2">
      <section class="panel"><div class="panel-h"><h2>Current plan</h2></div><div class="panel-b stack" style="gap:10px">
        <div class="toolbar" style="justify-content:space-between"><span style="font-size:22px;font-weight:700">${esc(planDisplayName(s.plan_key, s.plan_name))}</span><span class="plan-chip plan-${esc(s.state)}">${s.plan_key === "demo" ? "Active" : esc(s.state_label)}</span></div>
        <dl class="kv">
          <div><dt>Billing period</dt><dd>${s.plan_key === "demo" ? "Free" : esc(s.period_label)}</dd></div>
          <div><dt>Price</dt><dd>${s.plan_key === "demo" ? "₹0" : `${esc(s.monthly_price)} per month`}</dd></div>
          <div><dt>Started</dt><dd>${fmtDate(s.started_at)}</dd></div>
          <div><dt>${s.cancel_at_period_end ? "Ends on" : s.state === "trial" ? "Trial ends" : "Next renewal"}</dt><dd>${fmtDate(s.current_period_end)}</dd></div>
          ${s.grace_until ? `<div><dt>Grace period until</dt><dd>${fmtDateTime(s.grace_until)}</dd></div>` : ""}
          <div><dt>Estimated next payment</dt><dd>${s.next_payment ? `${esc(s.next_payment.total)} <span class="small muted">(${esc(s.next_payment.plan)}, ${esc(s.next_payment.period_label)})</span>` : "None scheduled"}</dd></div>
          ${s.scheduled_change ? `<div><dt>Scheduled at renewal</dt><dd>${esc(s.scheduled_change.plan_name)}, ${s.scheduled_change.billing_months} month(s)</dd></div>` : ""}
          ${s.suspended_reason ? `<div><dt>Suspended</dt><dd>${esc(s.suspended_reason)}</dd></div>` : ""}
        </dl>
        ${s.current_period_paid ? `<p class="small">Paid for this period: <b>${esc(s.current_period_paid.total)}</b> (receipt ${esc(s.current_period_paid.receipt_number || "—")}).</p>` : ""}
        ${s.grandfathered ? '<div class="note small">Your purchased limits and features are kept from the previous catalogue until your next checkout.</div>' : ""}
        <p class="small muted">${esc(s.price_policy)} Dates are stored in UTC and shown in your local time.</p>
        ${manage ? `<div class="toolbar">
          ${s.state === "active" && !s.cancel_at_period_end ? '<button id="cancelRenew" class="danger sm">Cancel renewal</button>' : ""}
          ${s.state === "active" && s.cancel_at_period_end ? '<button id="resumeRenew" class="sm">Resume renewal</button>' : ""}
          ${s.state === "past_due" ? '<button id="cancelRenew" class="danger sm">Cancel subscription</button>' : ""}
          <a href="/enterprise" class="small">Talk to us about Enterprise</a></div>` : '<p class="small muted">Only the organisation owner can change the subscription.</p>'}
      </div></section>
      <section class="panel"><div class="panel-h"><h2>Usage and features</h2></div><div class="panel-b stack" style="gap:10px">
        ${usageRow("Managed chargers", ent.usage.chargers, ent.limits.chargers)}
        ${usageRow("Managed sites", ent.usage.sites, ent.limits.sites)}
        ${usageRow("Organisation users (incl. pending invitations)", ent.usage.users, ent.limits.users)}
        ${usageRow("Configured integrations", ent.usage.integrations, ent.limits.integrations)}
        <p class="small muted">${esc(ent.managed_charger_note)} Simulated chargers in use: ${ent.usage.simulated_chargers}.</p>
        <details><summary class="small">Features in your plan (${ent.features.length})</summary>
          <ul class="small">${Object.entries(ent.feature_labels).map(([k, v]) => `<li>${ent.features.includes(k) ? "✓" : "—"} ${esc(v)}</li>`).join("")}</ul></details>
      </div></section>
    </div>
    ${open ? `<section class="panel" style="margin-top:14px"><div class="panel-b"><div class="note">A pending checkout for <b>${esc(planDisplayName(open.plan_key, open.plan_key))}</b> (${esc(open.period_label)}, ${esc(open.display.total)}) is ${open.payment_ready ? "ready for confirmation" : "awaiting payment details"} until ${fmtDateTime(open.expires_at)}. Your plan does not change until confirmed.
      ${manage ? `<button class="primary sm" id="resume">Continue checkout</button> <button class="sm" id="abandon">Cancel it</button>` : ""}</div></div></section>` : ""}
    <section class="panel" style="margin-top:14px"><div class="panel-h"><h2>Change plan</h2>
      <div class="seg-btns" role="group" aria-label="Billing period">${[1, 6, 12].map(m => `<button class="sm" data-m="${m}" aria-pressed="${m === months}">${m === 1 ? "Monthly" : m + " months"}</button>`).join("")}</div></div>
      <div class="panel-b"><div class="plan-cards">${data.plans.map(planCard).join("")}</div>
      <p class="small muted" style="margin-top:12px">Upgrades start immediately, with the unused part of your current paid period credited. Downgrades and shorter periods take effect at your next renewal. ${esc(data.policy.tax_note)} If a renewal is not completed, the subscription is past due for ${data.policy.grace_days} days and then becomes read-only: your data is kept and stays readable.</p></div></section>
    <div class="grid-2" style="margin-top:14px">
      <section class="panel"><div class="panel-h"><h2>Invoices &amp; receipts</h2></div><div class="panel-b flush tbl-wrap">${data.receipts.length ? `<table class="t"><thead><tr><th>Receipt</th><th>Date</th><th class="r">Amount</th><th></th></tr></thead><tbody>
        ${data.receipts.map(r => `<tr><td class="mono">${esc(r.number)}</td><td>${fmtDate(r.issued_at)}</td><td class="r num">${esc(r.total)}</td><td><button class="sm" data-rec="${esc(r.number)}">View</button></td></tr>`).join("")}</tbody></table>` : '<div class="empty">No receipts yet. Invoices appear here after confirming a plan.</div>'}</div></section>
      <section class="panel"><div class="panel-h"><h2>Checkout history</h2></div><div class="panel-b flush tbl-wrap">${data.checkouts.length ? `<table class="t"><thead><tr><th>Created</th><th>Plan</th><th>Status</th><th>Payment Method</th><th class="r">Total</th></tr></thead><tbody>
        ${data.checkouts.map(c => `<tr><td>${fmtDateTime(c.created_at)}</td><td>${esc(planDisplayName(c.plan_key, c.plan_key))} · ${esc(c.period_label)}</td><td>${esc(c.status)}</td><td class="small">${c.payment_display ? esc(c.payment_display) : '<span class="muted">—</span>'}</td><td class="r num">${esc(c.display.total)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No checkouts yet.</div>'}</div></section>
    </div>
    <section class="panel" style="margin-top:14px"><div class="panel-h"><h2>Subscription events</h2></div><div class="panel-b"><ul class="timeline">${data.events.map(e => `<li><b>${esc(e.kind.replaceAll("_", " "))}</b>${e.to_state ? ` (${esc(e.from_state || "—")} → ${esc(e.to_state)})` : ""} · ${fmtDateTime(e.at)} · <span class="muted">${esc(e.actor)}</span>${e.note ? `<div class="muted small">${esc(e.note)}</div>` : ""}</li>`).join("")}</ul></div></section>`;

  root.querySelectorAll("[data-m]").forEach(b => b.onclick = () => { months = Number(b.dataset.m); draw(); });
  root.querySelectorAll("[data-plan]").forEach(b => b.onclick = () => startCheckout(b.dataset.plan, b.dataset.kind));
  root.querySelectorAll("[data-rec]").forEach(b => b.onclick = () => showReceipt(b.dataset.rec));
  const cr = root.querySelector("#cancelRenew");
  if (cr) cr.onclick = cancelRenewal;
  const rr = root.querySelector("#resumeRenew");
  if (rr) rr.onclick = async () => { try { await api("/api/billing/resume-renewal", { method: "POST" }); toast("Renewal resumed"); await refreshMe(); load(); } catch (e) { toast(e.message, true); } };
  const rs = root.querySelector("#resume");
  if (rs) rs.onclick = () => checkoutModal(open);
  const ab = root.querySelector("#abandon");
  if (ab) ab.onclick = async () => { try { await api(`/api/billing/checkout/${encodeURIComponent(open.id)}/cancel`, { method: "POST" }); toast("Checkout cancelled"); await refreshMe(); load(); } catch (e) { toast(e.message, true); } };
}

function planCard(p) {
  const s = data.subscription;
  const current = p.key === s.plan_key && (p.key === "demo" || s.billing_months === months) && s.state !== "expired" && s.state !== "cancelled";
  const q = p.quotes ? p.quotes[String(months)] : null;
  const displayName = planDisplayName(p.key, p.name);
  let action = "";
  if (!data.can_manage) action = "";
  else if (p.key === "enterprise") action = '<a class="small" href="/enterprise">Contact sales</a>';
  else if (p.key === "demo") action = '<span class="small muted">Free forever</span>';
  else if (current) action = '<span class="small plan-chip plan-active">Current plan</span>';
  else {
    const cur = data.plans.find(x => x.key === s.plan_key);
    const isDown = s.state === "active" && cur && cur.monthly_price_paise !== null && ((p.monthly_price_paise < cur.monthly_price_paise) || (p.key === s.plan_key && months < s.billing_months));
    action = isDown ? `<button class="sm" data-plan="${p.key}" data-kind="schedule">Schedule at renewal</button>` : `<button class="primary sm" data-plan="${p.key}" data-kind="buy">${s.state === "active" ? "Upgrade to" : "Choose"} ${esc(displayName)}</button>`;
  }

  const highlights = p.key === "demo" ? [
    "Full evaluation & demonstration fleet access",
    "Operations dashboard & live telemetry",
    "Priority engine & automated ticketing",
    "Sample analytics & reporting",
    "EV charging site planning"
  ] : p.highlights.slice(0, 5);

  return `<div class="plan-card ${current ? "current" : ""}">
    <div>
      <b style="font-size:16px">${esc(displayName)}</b>
      <div class="price" style="margin-top:6px">${p.custom_pricing ? "Custom" : p.free ? "Free" : esc(q.display.monthly_equivalent)}<span class="small muted" style="font-weight:400">${!p.custom_pricing && !p.free ? " / month" : ""}</span></div>
      <div class="small muted" style="min-height:36px;margin-top:4px">${q ? `${esc(q.display.total)} billed for ${months === 1 ? "1 month" : months + " months"}${q.discount_paise ? ` (saves ${esc(q.display.discount)})` : ""}` : esc(p.tagline)}</div>
    </div>
    <ul>${highlights.map(h => `<li>${esc(h)}</li>`).join("")}</ul>
    ${p.recommended ? `<div class="small" style="color:var(--accent);font-weight:600">${esc(p.recommended_note)}</div>` : ""}
    <div style="margin-top:auto;padding-top:8px">${action}</div>
  </div>`;
}

async function startCheckout(plan, kind) {
  if (kind === "schedule") {
    const p = data.plans.find(x => x.key === plan);
    const displayName = planDisplayName(p.key, p.name);
    if (!(await confirmBox({ title: `Schedule ${displayName} at renewal?`, body: `<p>${esc(displayName)} (${months === 1 ? "monthly" : months + " months"}) will replace your current plan at the next renewal. Nothing changes before then and nothing is refunded. If you use more than the new plan's limits, nothing is deleted, but new additions will be blocked.</p>`, confirmLabel: "Schedule change" }))) return;
    try { await api("/api/billing/schedule-change", { method: "POST", body: { plan_key: plan, billing_months: months, confirm: true } }); toast("Change scheduled for renewal"); load(); }
    catch (e) { toast(e.message, true); }
    return;
  }
  let quote;
  try { quote = await api(`/api/billing/quote?plan=${encodeURIComponent(plan)}&months=${months}`); } catch (e) { toast(e.message, true); return; }
  reviewModal(plan, quote);
}

function modal(html) {
  const wrap = document.createElement("div");
  wrap.className = "modal-back";
  wrap.innerHTML = `<div class="modal wide" role="dialog" aria-modal="true">${html}</div>`;
  document.body.appendChild(wrap);
  const close = () => { wrap.remove(); document.removeEventListener("keydown", esc_); };
  const esc_ = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", esc_);
  return { wrap, close, $: (s) => wrap.querySelector(s) };
}

// ------------------------------------------------------------------ v10.2 Professional Checkout Flow
const STEPS = [["Review", "Plan and billing details"], ["Payment", "Payment method"], ["Confirm", "Review and confirm"], ["Confirmation", "Order status"]];
const TEST_CARD_SET = new Set(["4111111111111111", "5555555555554444", "4000000000000002"]);

function reviewModal(plan, q) { checkoutFlow({ plan, quote: q, chk: null, step: 1 }); }
function checkoutModal(chk) { checkoutFlow({ plan: chk.plan_key, quote: null, chk, step: chk.payment_ready ? 3 : 2 }); }

function checkoutFlow(st) {
  const p = data.plans.find(x => x.key === st.plan);
  const displayName = planDisplayName(p.key, p.name);
  const tp = data.test_payment;
  const key = (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2)).replace(/[^A-Za-z0-9-]/g, "");
  const wrap = document.createElement("div");
  wrap.className = "modal-back";
  wrap.innerHTML = `<div class="modal checkout" role="dialog" aria-modal="true" aria-labelledby="coTitle"></div>`;
  document.body.appendChild(wrap);
  const box = wrap.firstElementChild;
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  function close() { wrap.remove(); document.removeEventListener("keydown", onKey); load(); }
  const $ = (sel) => box.querySelector(sel);

  function amounts() {
    const c = st.chk, q = st.quote;
    const src = c || q;
    return { months: src.billing_months, period: src.period_label, d: src.display, discount: src.discount_paise, credit: src.credit_paise,
             pct: c ? c.discount_bps / 100 : q.discount_percent, mEq: q ? q.display.monthly_equivalent : null };
  }

  function summary() {
    const a = amounts();
    return `<aside class="co-summary" aria-label="Order summary">
      <div class="toolbar" style="justify-content:space-between"><b>Order Summary</b><span class="plan-chip plan-active">Secure</span></div>
      <div class="row"><span>${esc(displayName)} plan</span><span>${esc(a.period)}</span></div>
      <div class="row"><span>${esc(a.d.monthly_price)} × ${a.months} month(s)</span><span class="num">${esc(a.d.subtotal)}</span></div>
      ${a.discount ? `<div class="row"><span>Commitment discount (${a.pct}%)</span><span class="num" style="color:var(--ok)">− ${esc(a.d.discount)}</span></div>` : ""}
      ${a.credit ? `<div class="row"><span>Credit for unused period</span><span class="num" style="color:var(--ok)">− ${esc(a.d.credit)}</span></div>` : ""}
      <div class="row total"><span>Total Payable</span><span class="num">${esc(a.d.total)}</span></div>
      ${a.mEq ? `<div class="row small muted"><span>Monthly equivalent</span><span>${esc(a.mEq)}</span></div>` : ""}
      ${st.chk && st.chk.payment_display ? `<div class="row small"><span>Payment</span><span style="text-align:right;font-weight:600">${esc(st.chk.payment_display)}</span></div>` : ""}
      <p class="small muted" style="margin:4px 0 0">${esc(data.policy.tax_note)}</p>
      <div class="small muted" style="display:flex;align-items:center;gap:6px;margin-top:6px">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        <span>Encrypted local billing simulation</span>
      </div>
    </aside>`;
  }

  function frame(inner, step) {
    const title = step === 4 ? `Order Confirmed: ${esc(displayName)}` :
                  step === 1 ? `Review Order: ${esc(displayName)} · ${esc(amounts().period)}` :
                  step === 2 ? `Choose Payment Method: ${esc(displayName)} · ${esc(amounts().period)}` :
                  `Confirm Subscription: ${esc(displayName)} · ${esc(amounts().period)}`;
    box.innerHTML = `<h3 id="coTitle">${title}</h3>
      <ol class="co-steps">${STEPS.map(([t, s], i) => `<li class="${i + 1 === step ? "current" : i + 1 < step ? "done" : ""}"${i + 1 === step ? ' aria-current="step"' : ""}><b>${i + 1}. ${t}</b>${s}</li>`).join("")}</ol>
      <div class="co-grid"><div class="stack" style="gap:12px">${inner}</div>${summary()}</div>`;
  }

  // ---- Step 1: Review Order & Billing Information
  function stepReview() {
    const org = state.me.organisation;
    frame(`<div>
        <h4 style="margin:0 0 8px">Plan Inclusions</h4>
        <ul class="small" style="display:grid;gap:4px">${p.highlights.map(h => `<li>✓ ${esc(h)}</li>`).join("")}</ul>
        <p class="small muted" style="margin-top:8px">Limits: ${p.limits.chargers} managed chargers, ${p.limits.sites} site(s), ${p.limits.users} users, ${p.limits.integrations} configured integration(s).</p>
        <p class="small muted">${esc(st.quote.commitment_note)}</p>
      </div>
      <form id="bf" class="stack" style="gap:10px" novalidate>
        <h4 style="margin:0">Billing Contact Details</h4>
        <div class="form-grid">
          <label class="field"><span>Billing Organisation Name</span><input name="billing_name" type="text" maxlength="120" autocomplete="organization" value="${esc(org.name)}" required></label>
          <label class="field"><span>Billing Email Address</span><input name="billing_email" type="email" maxlength="254" autocomplete="email" value="${esc(state.me.user.email)}" required></label>
        </div>
        <label class="field"><span>Billing Address (Optional)</span><input name="billing_address" type="text" maxlength="1000" autocomplete="street-address" value="${esc(org.billing_address || "")}"></label>
        <div class="toolbar" style="justify-content:flex-end;margin-top:6px">
          <button type="button" id="x">Cancel</button>
          <button class="primary" id="go">Continue to payment method</button>
        </div>
      </form>`, 1);
    $("#x").onclick = close;
    $("#bf").onsubmit = async (e) => {
      e.preventDefault();
      const btn = $("#go"); btn.disabled = true; btn.textContent = "Preparing order…";
      const f = e.target;
      try {
        st.chk = await api("/api/billing/checkout", { method: "POST", body: { plan_key: st.plan, billing_months: st.quote.billing_months,
          billing_name: f.billing_name.value.trim(), billing_email: f.billing_email.value.trim(), billing_address: f.billing_address.value.trim() } });
        stepPayment();
      } catch (err) { toast(err.message, true); btn.disabled = false; btn.textContent = "Continue to payment method"; }
    };
  }

  // ---- Step 2: Payment Method Selection
  function stepPayment(notice) {
    frame(`${notice ? `<div class="note warn" role="alert">${esc(notice)}</div>` : ""}
      <div>
        <h4 style="margin:0 0 4px">Select a Payment Option</h4>
        <p class="small muted" style="margin:0 0 12px">Choose your preferred payment method to complete this subscription.</p>
      </div>
      <form id="pmForm" class="stack" style="gap:12px" novalidate autocomplete="off">
        <div class="pm-grid" role="radiogroup" aria-label="Payment method">
          <label class="pm-card" for="m_upi">
            <div class="pm-card-top">
              <span class="pm-card-title"><input type="radio" id="m_upi" name="method" value="upi" checked> UPI</span>
              <div class="pm-card-logos">${LOGO_UPI_GENERIC}</div>
            </div>
            <div class="pm-card-sub">Google Pay, PhonePe, Paytm, BHIM</div>
          </label>
          <label class="pm-card" for="m_card">
            <div class="pm-card-top">
              <span class="pm-card-title"><input type="radio" id="m_card" name="method" value="credit_card"> Credit / Debit Card</span>
              <div class="pm-card-logos">${LOGO_VISA} ${LOGO_MASTERCARD}</div>
            </div>
            <div class="pm-card-sub">Visa, Mastercard, RuPay cards</div>
          </label>
          <label class="pm-card" for="m_nb">
            <div class="pm-card-top">
              <span class="pm-card-title"><input type="radio" id="m_nb" name="method" value="netbanking"> Net Banking</span>
              <div class="pm-card-logos">${LOGO_NETBANKING}</div>
            </div>
            <div class="pm-card-sub">All major Indian banks</div>
          </label>
        </div>

        <!-- UPI Panel -->
        <div class="pm-panel" data-panel="upi">
          <div>
            <div class="small muted" style="margin-bottom:8px;font-weight:600">Select UPI Application</div>
            <div class="upi-app-grid" role="radiogroup" aria-label="UPI Application">
              <label class="upi-app-card"><input type="radio" name="upi_app" value="gpay" checked> ${LOGO_GPAY} <span>Google Pay</span></label>
              <label class="upi-app-card"><input type="radio" name="upi_app" value="phonepe"> ${LOGO_PHONEPE} <span>PhonePe</span></label>
              <label class="upi-app-card"><input type="radio" name="upi_app" value="paytm"> ${LOGO_PAYTM} <span>Paytm</span></label>
              <label class="upi-app-card"><input type="radio" name="upi_app" value="bhim"> ${LOGO_BHIM} <span>BHIM</span></label>
              <label class="upi-app-card"><input type="radio" name="upi_app" value="other"> ${LOGO_UPI_GENERIC} <span>Other UPI App</span></label>
            </div>
          </div>
          <label class="field" style="margin-top:6px">
            <span>UPI ID / VPA</span>
            <input name="upi_id" type="text" inputmode="email" autocomplete="off" spellcheck="false" placeholder="username@upi" maxlength="60">
          </label>
          <div class="field-err" id="err-upi_id"></div>
          <div class="sim-helper">
            <span>Quick test handle:</span>
            <button type="button" class="sim-chip" data-upi="success@test">success@test (Approves)</button>
            <button type="button" class="sim-chip" data-upi="failure@test">failure@test (Declines)</button>
          </div>
          <p class="small muted" style="margin:0">Your transaction will be processed via your selected UPI application.</p>
        </div>

        <!-- Card Panel -->
        <div class="pm-panel hidden" data-panel="card">
          <div class="card-brands-row">
            <span class="small muted" style="font-weight:600;margin-right:6px">Accepted Networks:</span>
            ${LOGO_VISA} ${LOGO_MASTERCARD} ${LOGO_RUPAY}
          </div>
          <div class="form-grid">
            <label class="field"><span>Name on Card</span><input name="card_name" type="text" autocomplete="cc-name" maxlength="60" placeholder="Cardholder Name"></label>
            <label class="field"><span>Card Number</span><input name="card_number" type="text" inputmode="numeric" autocomplete="cc-number" maxlength="23" placeholder="•••• •••• •••• ••••"></label>
            <label class="field"><span>Expiry (MM/YY)</span><input name="card_expiry" type="text" inputmode="numeric" autocomplete="cc-exp" maxlength="5" placeholder="MM/YY"></label>
          </div>
          <div class="field-err" id="err-card_name"></div><div class="field-err" id="err-card_number"></div><div class="field-err" id="err-card_expiry"></div>
          <div class="sim-helper">
            <span>Test card options:</span>
            <button type="button" class="sim-chip" data-card="4111 1111 1111 1111">Visa (4111 •••• 1111)</button>
            <button type="button" class="sim-chip" data-card="5555 5555 5555 4444">Mastercard (5555 •••• 4444)</button>
            <button type="button" class="sim-chip" data-card="4000 0000 0000 0002">Visa (Simulates Decline)</button>
          </div>
          <p class="small muted" style="margin:0">🔒 Industry-standard safe payment simulation. ChargeSight never requests, handles or stores CVV codes or OTPs.</p>
        </div>

        <!-- Net Banking Panel -->
        <div class="pm-panel hidden" data-panel="netbanking">
          <label class="field">
            <span>Select Bank</span>
            <select name="bank">
              <option value="test_bank_approve">State Bank of India</option>
              <option value="test_bank_approve">HDFC Bank</option>
              <option value="test_bank_approve">ICICI Bank</option>
              <option value="test_bank_approve">Axis Bank</option>
              <option value="test_bank_approve">Kotak Mahindra Bank</option>
              <option value="test_bank_approve">ChargeSight Commercial Bank</option>
              <option value="test_bank_decline">ChargeSight Test Bank (Simulates Decline)</option>
            </select>
          </label>
          <p class="small muted" style="margin:0">You will complete the authorization directly with your bank institution.</p>
        </div>

        <div class="field-err" id="err-method"></div>
        <div class="toolbar" style="justify-content:space-between;margin-top:6px">
          <button type="button" id="later">Decide later</button>
          <button class="primary" id="payNext">Continue to review</button>
        </div>
      </form>`, 2);

    const f = $("#pmForm");
    const show = () => {
      const m = f.method.value;
      const targetPanel = m.endsWith("_card") ? "card" : m;
      box.querySelectorAll("[data-panel]").forEach(el => el.classList.toggle("hidden", el.dataset.panel !== targetPanel));
    };
    f.querySelectorAll("[name=method]").forEach(r => r.onchange = show);
    show();

    box.querySelectorAll("[data-upi]").forEach(b => b.onclick = () => { f.upi_id.value = b.dataset.upi; });
    box.querySelectorAll("[data-card]").forEach(b => b.onclick = () => {
      f.card_number.value = b.dataset.card;
      if (!f.card_name.value) f.card_name.value = "ChargeSight Customer";
      if (!f.card_expiry.value) f.card_expiry.value = "12/30";
    });

    f.card_number.addEventListener("input", () => {
      const d = f.card_number.value.replace(/\D/g, "").slice(0, 19);
      f.card_number.value = d.replace(/(.{4})/g, "$1 ").trim();
    });
    f.card_expiry.addEventListener("input", () => {
      const d = f.card_expiry.value.replace(/\D/g, "").slice(0, 4);
      f.card_expiry.value = d.length > 2 ? d.slice(0, 2) + "/" + d.slice(2) : d;
    });

    $("#later").onclick = close;

    const fieldErr = (field, msg) => {
      box.querySelectorAll(".field-err").forEach(e => { e.textContent = ""; });
      box.querySelectorAll("label.field.invalid").forEach(l => l.classList.remove("invalid"));
      if (!field) return;
      const el = $(`#err-${field}`) || $("#err-method");
      if (el) el.textContent = msg;
      const inp = f.querySelector(`[name=${field}]`);
      if (inp) { inp.closest("label.field")?.classList.add("invalid"); inp.focus(); }
    };

    f.onsubmit = async (e) => {
      e.preventDefault();
      const m = f.method.value;
      const body = { method: m };
      if (m === "upi") {
        body.upi_app = f.upi_app.value; body.upi_id = f.upi_id.value.trim().toLowerCase();
        if (!/^[a-z0-9][a-z0-9._-]{1,39}@test$/.test(body.upi_id)) {
          return fieldErr("upi_id", "Please enter a valid test UPI handle such as success@test or failure@test.");
        }
      } else if (m.endsWith("_card")) {
        const digits = f.card_number.value.replace(/\D/g, "");
        if (!TEST_CARD_SET.has(digits)) {
          return fieldErr("card_number", "Please choose one of the supported test cards (Visa or Mastercard).");
        }
        Object.assign(body, { card_name: f.card_name.value.trim(), card_number: digits, card_expiry: f.card_expiry.value.trim() });
      } else {
        body.bank = f.bank.value;
      }

      const btn = $("#payNext"); btn.disabled = true; btn.textContent = "Verifying…";
      try {
        st.chk = await api(`/api/billing/checkout/${encodeURIComponent(st.chk.id)}/payment-method`, { method: "POST", body });
        stepConfirm();
      } catch (err) {
        const fields = err.detail && err.detail.fields;
        const k = fields ? Object.keys(fields)[0] : null;
        if (k) fieldErr(k, fields[k]); else toast(err.message, true);
        btn.disabled = false; btn.textContent = "Continue to review";
      }
    };
  }

  // ---- Step 3: Review and Confirm Subscription
  function stepConfirm() {
    const c = st.chk;
    frame(`<div>
        <h4 style="margin:0 0 6px">Review Subscription Order</h4>
        <p class="small muted" style="margin:0 0 14px">Please review your subscription and billing details before confirmation.</p>
      </div>
      <dl class="kv">
        <div><dt>Selected Plan</dt><dd><b>${esc(displayName)}</b> · ${esc(c.period_label)}</dd></div>
        <div><dt>Total Payable Now</dt><dd><b class="num" style="font-size:16px;color:var(--accent)">${esc(c.display.total)}</b></dd></div>
        <div><dt>Payment Method</dt><dd>${esc(c.payment_display)} <button class="sm ghost" id="chg" style="margin-left:8px">Change</button></dd></div>
        <div><dt>Billing Contact</dt><dd>${esc(c.billing_name)} · ${esc(c.billing_email)}</dd></div>
        <div><dt>Order Validity</dt><dd>${fmtDateTime(c.expires_at)}</dd></div>
      </dl>
      <label class="check" style="margin-top:10px">
        <input type="checkbox" id="ack"> I authorise ChargeSight to activate this subscription plan for my organisation.
      </label>
      <div class="toolbar" style="justify-content:space-between;margin-top:12px">
        <button id="later">Decide later</button>
        <button class="primary" id="confirm" disabled>Confirm Subscription · ${esc(c.display.total)}</button>
      </div>
      <div id="result" aria-live="polite"></div>`, 3);

    $("#later").onclick = close;
    $("#chg").onclick = () => stepPayment();
    $("#ack").onchange = (e) => { $("#confirm").disabled = !e.target.checked; };
    $("#confirm").onclick = async () => {
      const btn = $("#confirm"); btn.disabled = true; btn.textContent = "Processing order…";
      try {
        const r = await api(`/api/billing/checkout/${encodeURIComponent(c.id)}/complete`, {
          method: "POST",
          headers: { "Idempotency-Key": key },
          body: { acknowledge_simulation: true }
        });
        st.chk = r.checkout;
        await refreshMe();
        stepDone(r);
      } catch (err) {
        const code = err.detail && err.detail.code;
        if (code === "test_payment_declined" || code === "payment_method_required") {
          st.chk = { ...c, payment_method: null, payment_display: "", payment_ready: false };
          stepPayment("Your payment option was declined. Please choose an alternative payment method to proceed.");
        } else {
          toast(err.message, true);
          btn.disabled = false;
          btn.textContent = `Confirm Subscription · ${c.display.total}`;
        }
      }
    };
  }

  // ---- Step 4: Completion / Order Confirmation
  function stepDone(r) {
    frame(`<div class="order-conf-card">
        <div class="order-conf-header">
          <div class="order-conf-icon">
            <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
          <div>
            <h4 style="margin:0 0 4px;font-size:17px;color:var(--ink)">Order Confirmed</h4>
            <p class="small muted" style="margin:0">Thank you! Your <b>${esc(r.subscription.plan_name)}</b> subscription is now active.</p>
          </div>
        </div>
        <div class="order-conf-details">
          <div class="order-conf-kv"><dt>Order Reference</dt><dd class="mono">${esc(r.checkout.provider_reference || r.checkout.id)}</dd></div>
          <div class="order-conf-kv"><dt>Receipt Number</dt><dd class="mono">${esc(r.receipt.number)}</dd></div>
          <div class="order-conf-kv"><dt>Subscription Plan</dt><dd>${esc(r.subscription.plan_name)} (${esc(r.checkout.period_label)})</dd></div>
          <div class="order-conf-kv"><dt>Order Total</dt><dd class="num">${esc(r.receipt.total)}</dd></div>
          <div class="order-conf-kv"><dt>Payment Option</dt><dd>${esc(r.receipt.payment_label || r.checkout.payment_display)}</dd></div>
          <div class="order-conf-kv"><dt>Confirmation Date</dt><dd>${fmtDateTime(r.checkout.completed_at)}</dd></div>
          <div class="order-conf-kv"><dt>Subscription Status</dt><dd><span class="plan-chip plan-active">Active</span></dd></div>
        </div>
        <div class="toolbar" style="margin-top:6px">
          <button class="primary sm" id="toSetup">Go to setup checklist</button>
          <button class="sm" id="toBilling">View subscription &amp; receipts</button>
        </div>
      </div>`, 4);

    $("#toSetup").onclick = () => { close(); location.hash = "#onboarding"; };
    $("#toBilling").onclick = close;
  }

  if (st.step === 1) stepReview();
  else if (st.step === 2) stepPayment();
  else stepConfirm();
}

async function cancelRenewal() {
  const s = data.subscription;
  const displayName = planDisplayName(s.plan_key, s.plan_name);
  const pastDue = s.state === "past_due";
  if (!(await confirmBox({
    title: pastDue ? "Cancel subscription?" : "Cancel renewal?",
    danger: true,
    confirmLabel: pastDue ? "Cancel subscription" : "Cancel renewal",
    body: pastDue ? "<p>The subscription becomes expired now and the workspace becomes read-only. Your data is kept.</p>"
      : `<p>Your ${esc(displayName)} plan stays active until ${fmtDate(s.current_period_end)} and then ends. The current period is not refunded. The workspace then becomes read-only; your data is kept. Nothing is sent outside ChargeSight.</p>`
  }))) return;

  try {
    const r = await api("/api/billing/cancel-renewal", { method: "POST", body: { confirm: true } });
    toast(r.message || "Renewal cancelled");
    await refreshMe(); load();
  } catch (e) { toast(e.message, true); }
}

async function showReceipt(number) {
  let r;
  try { r = await api(`/api/billing/receipts/${encodeURIComponent(number)}`); } catch (e) { toast(e.message, true); return; }
  const m = modal(`<div class="receipt">
    <div class="toolbar" style="justify-content:space-between">
      <h3 style="margin:0">Receipt ${esc(r.number)}</h3>
      <span class="plan-chip plan-active">PAID</span>
    </div>
    <p class="small muted" style="margin:4px 0 14px">Official subscription billing receipt for ${esc(r.organisation)}.</p>
    <dl class="kv">
      <div><dt>Date Issued</dt><dd>${fmtDateTime(r.issued_at)}</dd></div>
      <div><dt>Organisation</dt><dd>${esc(r.organisation)}</dd></div>
      <div><dt>Billed To</dt><dd>${esc(r.billed_to.name || "")} · ${esc(r.billed_to.email || "")}${r.billed_to.address ? "<br>" + esc(r.billed_to.address) : ""}</dd></div>
      <div><dt>Payment Method</dt><dd>${esc(r.payment_label)}</dd></div>
    </dl>
    <table class="t" style="margin-top:14px">
      <thead><tr><th>Description</th><th class="r">Amount</th></tr></thead>
      <tbody>
        ${r.lines.map(l => `<tr><td>${esc(l.description)}</td><td class="r num">${esc(l.amount)}</td></tr>`).join("")}
        <tr style="border-top:2px solid var(--line-strong)"><td><b>Total</b></td><td class="r num"><b>${esc(r.total)}</b></td></tr>
      </tbody>
    </table>
    <div class="toolbar" style="justify-content:flex-end;margin-top:16px">
      <button id="pr">Print Receipt</button>
      <button class="primary" id="cl">Close</button>
    </div>
  </div>`);
  m.$("#cl").onclick = m.close;
  m.$("#pr").onclick = () => window.print();
}

export function unmount() {}
