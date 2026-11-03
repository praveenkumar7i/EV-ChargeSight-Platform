// ChargeSight public website: one small page renderer keyed by the URL path. Plans and prices always come
// from the server's catalogue (/api/public/plans); nothing about pricing is hard-coded here.
const $ = (s, r = document) => r.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const params = new URLSearchParams(location.search);
const path = location.pathname.replace(/\/+$/, "") || "/";
let me = null;

// Old console bookmarks (/#dashboard etc.) now live under /app
if (path === "/" && /^#(dashboard|map|analytics|tickets|technicians|simulator|priority|experiments|investment|integrations|validation|reports)/.test(location.hash)) {
  location.replace("/app" + location.hash);
}

async function api(url, { method = "GET", body, csrf } = {}) {
  const headers = { "Accept": "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrf) headers["X-CSRF-Token"] = csrf;
  let res;
  try { res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" }); }
  catch { throw Object.assign(new Error("ChargeSight could not be reached. Check that the server is running and try again."), { status: 0 }); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = d && typeof d === "object" && !Array.isArray(d) ? d.message : Array.isArray(d) ? "Please check the highlighted fields." : (d || `The request failed (HTTP ${res.status}).`);
    throw Object.assign(new Error(msg), { status: res.status, detail: d });
  }
  return data;
}

const LOGO = `<i aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" fill="#fff"/></svg></i>ChargeSight`;

// v10.1: only same-origin console paths are accepted as a post-sign-in destination (no open redirects)
function safeNext(raw) {
  if (!raw || typeof raw !== "string") return null;
  if (!/^\/app(?:[\/?#]|$)/.test(raw) || raw.includes("//") || raw.includes("\\")) return null;
  return raw.includes("#") || !location.hash ? raw : raw + location.hash;
}
async function signOutHere(to) {
  try { await api("/api/auth/signout", { method: "POST", csrf: me && me.csrf_token }); } catch { /* signed out anyway */ }
  location.href = to;
}
// Presentation Demo: a private, SIMULATED workspace of the signed-in user (POST /api/auth/presentation-demo)
async function openPresentationDemo(csrf) {
  await api("/api/auth/presentation-demo", { method: "POST", csrf });
  try { localStorage.setItem("cs.source", "simulation"); } catch { /* storage unavailable */ }
  location.href = "/app#dashboard";
}

function header() {
  const nav = [["/", "Product"], ["/features", "Features"], ["/pricing", "Pricing"], ["/enterprise", "Enterprise"]];
  $("#siteHeader").innerHTML = `<div class="wrap">
    <a class="logo" href="/">${LOGO}</a>
    <nav class="site-nav" aria-label="Main">${nav.map(([h, t]) => `<a href="${h}" ${path === h ? 'aria-current="page"' : ""}>${t}</a>`).join("")}</nav>
    <div class="h-actions">${me
      ? `<span class="signed-in small" title="${esc(me.user.email)}">Signed in as <b>${esc(me.user.full_name)}</b></span>
         <button type="button" class="btn btn-quiet btn-sm" id="hdrSignOut">Sign out</button>
         <a class="btn btn-primary btn-sm" href="/app">Open workspace</a>`
      : `<a class="btn btn-quiet btn-sm" href="/signin">Sign in</a><a class="btn btn-secondary btn-sm" href="/signup">Sign up</a>
         <a class="btn btn-primary btn-sm" href="/signin?next=${encodeURIComponent("/app")}">Open workspace</a>`}</div>
    <button class="btn btn-secondary btn-sm menu-toggle" aria-expanded="false" aria-controls="siteHeader" id="menuToggle">Menu</button></div>`;
  const so = $("#hdrSignOut");
  if (so) so.onclick = () => signOutHere("/");
  $("#menuToggle").onclick = (e) => {
    const open = $("#siteHeader").classList.toggle("open");
    e.currentTarget.setAttribute("aria-expanded", String(open));
  };
  const onScroll = () => $("#siteHeader").classList.toggle("scrolled", scrollY > 8);
  addEventListener("scroll", onScroll, { passive: true });
  onScroll();
}

function footer() {
  $("#siteFooter").innerHTML = `<div class="wrap"><div class="cols">
    <div><a class="logo" href="/">${LOGO}</a><p style="max-width:34ch;margin-top:12px">EV charger operations and maintenance intelligence. A PME capstone prototype from BMS College of Engineering, Bengaluru.</p></div>
    <div><h4>Product</h4><ul><li><a href="/features">Features</a></li><li><a href="/pricing">Pricing</a></li><li><a href="/demo">Start free demo</a></li><li><a href="/signup">Sign up</a></li><li><a href="/signin">Sign in</a></li></ul></div>
    <div><h4>Company</h4><ul><li><a href="/enterprise">Enterprise</a></li><li><a href="/contact">Contact</a></li><li><a href="/security">Security</a></li></ul></div>
    <div><h4>Legal and docs</h4><ul><li><a href="/privacy">Privacy notice</a></li><li><a href="/terms">Terms of use</a></li><li><a href="/app#integrations">Integration guide (in the app)</a></li></ul></div></div>
    <div class="legal">Prototype: prices are illustrative assumptions, checkout is simulated and no payment is taken. Demo data is synthetic and labelled SIMULATED. Real operator data requires the operator's authorisation.</div></div>`;
}

// ------------------------------------------------------------------ landing page
const SHOT_DASH = "/static/site/img/console-dashboard.png";
const SHOT_TICKETS = "/static/site/img/console-tickets.png";
const SHOT_PRIORITY = "/static/site/img/console-priority.png";
const SHOT_MAP = "/static/site/img/console-map.png";

function shot(src, alt, caption) {
  return `<figure><div class="bar" aria-hidden="true"><span></span><span></span><span></span></div>
    <img src="${src}" alt="${esc(alt)}" width="1400" height="875" loading="lazy"><figcaption>${esc(caption)}</figcaption></figure>`;
}

function landing() {
  return `
  <section class="hero"><div class="wrap hero-grid">
    <div>
      <span class="badge"><b aria-hidden="true"></b>Prototype for a PME capstone, open for evaluation</span>
      <h1>Know which charger to fix next, and who should fix it.</h1>
      <p class="lede">ChargeSight watches your EV chargers, opens a maintenance ticket the moment one fails, ranks every fault with a priority score you can explain, and sends a technician who is qualified for the job.</p>
      <div class="cta"><a class="btn btn-primary" href="/demo">Start free demo</a><a class="btn btn-secondary" href="/pricing">Explore plans</a></div>
      <p class="fine">The free demo runs on a simulated fleet, clearly labelled. No operator credentials or payment details needed.</p>
    </div>
    <div class="shot">${shot(SHOT_DASH, "The ChargeSight operations console showing fleet uptime, charger statuses, open tickets and alerts", "Real screenshot of the ChargeSight console running on the simulated demo fleet (SIMULATED data).")}
      <aside class="explainer" aria-label="How a priority score is explained">
        <div class="code">P = 0.4S + 0.3U + 0.2R + 0.1A</div>
        <div class="row"><span>Severity</span><i style="width:100%"></i><span>0.40</span></div>
        <div class="row"><span>Utilisation</span><i style="width:62%"></i><span>0.19</span></div>
        <div class="row"><span>Capacity lost</span><i style="width:45%"></i><span>0.09</span></div>
        <div class="row"><span>Age</span><i style="width:30%"></i><span>0.03</span></div>
        <div class="total"><span>Priority score</span><span>0.71</span></div>
        <div class="note">Worked example of the engine's formula. Every ticket shows its own factors and weights.</div>
      </aside></div>
  </div></section>

  <section id="product"><div class="wrap">
    <div class="sec-head"><h2>One workspace for keeping chargers available</h2><p>From the first fault signal to a verified repair, with the evidence behind each decision kept on the ticket.</p></div>
    <div class="lanes">
      <div class="lane"><h3>Monitor</h3><p>See every charger and connection.</p>
        <div class="cap"><b>Fleet health and uptime</b><span>Availability, charging sessions, power and faults per site, refreshed continuously.</span></div>
        <div class="cap"><b>Public station mapping</b><span>Public charging listings from Open Charge Map and OpenStreetMap beside your own sites, never confused with live status.</span></div>
        <div class="cap"><b>Operator API and OCPP integrations</b><span>Authorised REST APIs and OCPP 1.6J charge points, each shown as connected only after verification.</span></div></div>
      <div class="lane"><h3>Maintain</h3><p>Turn faults into finished repairs.</p>
        <div class="cap"><b>Fault detection and ticketing</b><span>Telemetry faults and user reports become one ticket per charger and fault, with duplicates merged.</span></div>
        <div class="cap"><b>Explainable fault prioritisation</b><span>Severity, utilisation, lost capacity and age, weighted and shown factor by factor. Safety faults are never downgraded.</span></div>
        <div class="cap"><b>Automated technician assignment</b><span>Skills, AC/DC equipment, safety certification, shifts and workload decide who is sent, with reasons for every exclusion.</span></div>
        <div class="cap"><b>SLA and maintenance tracking</b><span>Deadlines by severity, escalation once overdue, waiting states and post-repair safety inspection.</span></div></div>
      <div class="lane"><h3>Plan</h3><p>Decide where to improve next.</p>
        <div class="cap"><b>Analytics and reports</b><span>Fault trends, response and repair times, backlog and site comparisons, with CSV exports and a printable summary.</span></div>
        <div class="cap"><b>Charging-site planning</b><span>Score candidate locations with the evidence you have and test the financial case before you invest.</span></div></div>
    </div></div></section>

  <section><div class="wrap">
    <div class="sec-head"><h2>From signup to a working dashboard</h2><p>Start on the simulated fleet today. Connect real data when your operator authorises it.</p></div>
    <div class="steps">
      <div class="step"><h3>Connect</h3><p>Register your organisation, choose demo mode or import an operator export, and add authorised API or OCPP connections when credentials are available.</p></div>
      <div class="step"><h3>Monitor</h3><p>Watch uptime and alerts, raise tickets from the field and let the priority engine order the queue.</p></div>
      <div class="step"><h3>Optimise</h3><p>Dispatch qualified technicians automatically, track SLAs and use analytics and site planning to decide what to change.</p></div>
    </div></div></section>

  <section><div class="wrap band flip">
    <div class="shot">${shot(SHOT_TICKETS, "Maintenance tickets list with priority scores, SLA deadlines and technician assignments", "Real screenshot: maintenance tickets on the simulated demo fleet.")}</div>
    <div><h2>Every ticket carries its reasons</h2>
      <p class="lede" style="font-size:17px">Operators asked why one charger was fixed before another. ChargeSight answers on the ticket itself.</p>
      <ul class="ticks"><li>Priority factors, weights and the resulting score</li><li>Who was eligible to repair it, and why others were not</li><li>Every status change, reassignment and escalation in an audit trail</li><li>Safety faults held out of service until an inspection is recorded</li></ul></div>
  </div></section>

  <section><div class="wrap">
    <div class="sec-head"><h2>Plans</h2><p>From ₹499 a month. Monthly, six-month or annual billing. Prices are proposed for this prototype, not validated market prices.</p></div>
    <div id="planPreview" class="plans"><p class="loading-line">Loading plans…</p></div>
    <p style="margin-top:24px"><a class="btn btn-secondary" href="/pricing">Compare plans in detail</a></p></div></section>

  <section><div class="wrap">
    <div class="sec-head"><h2>What is real in this prototype</h2><p>We would rather tell you than let a screenshot imply it.</p></div>
    <div class="truth">
      <div class="is"><h3>Working today</h3><p>Accounts, organisations, roles, the priority engine, technician assignment, SLA tracking, analytics, site planning, CSV import, the OCPP 1.6J central system and the subscription workflow.</p></div>
      <div class="needs"><h3>Needs your operator</h3><p>Live operator telemetry requires the operator's written authorisation, an API base URL and credentials. Until then those connections show Not configured.</p></div>
      <div class="is"><h3>Clearly labelled demo data</h3><p>The demo fleet, technicians and scenarios are synthetic and marked SIMULATED everywhere. They are never mixed with real records.</p></div>
      <div class="needs"><h3>Not yet in place</h3><p>Real payments, e-mail delivery and production security review. Checkout is a simulation and receipts are marked DEMO.</p></div>
    </div></div></section>

  <section><div class="wrap"><h2 style="margin-bottom:24px">Questions</h2>${faq(LANDING_FAQ)}</div></section>`;
}

const LANDING_FAQ = [
  ["Do I need operator API credentials to try ChargeSight?", "No. The free Demo / Sandbox plan creates a private simulated fleet for your organisation with sample tickets, demo technicians and scenarios. Nothing in it is presented as live charger telemetry."],
  ["How does ChargeSight decide which fault to repair first?", "Each open ticket gets a score from severity, site utilisation, lost charging capacity and waiting time. The weights are visible and adjustable by administrators, and each ticket shows its own calculation. Tickets waiting longer than 24 hours move to the front so nothing is starved."],
  ["Can ChargeSight connect to my chargers?", "Yes, when you have authorisation: through an operator REST API that follows the documented ChargeSight contract, a CSV export, or OCPP 1.6J charge points pointed at your organisation's endpoint. A connection is shown as connected only after a successful test."],
  ["Is my organisation's data separate from others?", "Yes. Every operational record belongs to one organisation and every request is checked on the server, so changing an ID in a URL cannot reveal another organisation's chargers, tickets or settings."],
  ["Is this a finished commercial product?", "No. It is a capstone prototype. Prices are illustrative, checkout is simulated, e-mail is not sent and the security controls have not had an independent review."],
];

function faq(items) {
  return `<div class="faq">${items.map(([q, a]) => `<details><summary>${esc(q)}</summary><p>${esc(a)}</p></details>`).join("")}</div>`;
}

// ------------------------------------------------------------------ plans data
let catalogue = null;
async function plans() {
  if (!catalogue) catalogue = await api("/api/public/plans");
  return catalogue;
}

function planCard(p, months, compact = false) {
  const q = p.quotes ? p.quotes[String(months)] : null;
  const amount = p.custom_pricing ? "Custom" : p.free ? "Free" : q.display.monthly_equivalent.replace(".00", "");
  const per = p.custom_pricing || p.free ? "" : "<small> / month</small>";
  const billed = p.custom_pricing ? "Quoted for your network and contract" : p.free ? "Simulated fleet only, no time limit"
    : months === 1 ? `Billed monthly: ${esc(q.display.total)}` : `${esc(q.display.total)} billed every ${months} months (saves ${esc(q.display.discount)})`;
  const cta = p.key === "enterprise" ? `<a class="btn btn-secondary btn-block" href="/enterprise">Contact sales</a>`
    : p.key === "demo" ? `<a class="btn btn-secondary btn-block" href="/demo">Start free demo</a>`
    : `<a class="btn ${p.recommended ? "btn-primary" : "btn-secondary"} btn-block" href="/signup?plan=${p.key}&months=${months}">Choose ${esc(p.name)}</a>`;
  return `<article class="plan ${p.recommended ? "rec" : ""}">
    ${p.recommended ? `<div class="rec-note" title="${esc(p.recommended_note || "")}">Recommended starting plan</div>` : ""}
    <h3>${esc(p.name)}</h3><p class="for">${esc(p.target)}</p>
    <div class="amount">${esc(amount)}${per}</div><div class="billed">${billed}</div>
    <ul>${(compact ? p.highlights.slice(0, 4) : p.highlights).map(h => `<li>${esc(h)}</li>`).join("")}</ul>${cta}</article>`;
}

async function fillPlanPreview() {
  const box = $("#planPreview");
  if (!box) return;
  try { const d = await plans(); box.innerHTML = d.plans.map(p => planCard(p, 1, true)).join(""); }
  catch (e) { box.innerHTML = `<p class="form-msg error">${esc(e.message)}</p>`; }
}

// ------------------------------------------------------------------ pricing page
function pricing() {
  return `<section class="page-head"><div class="wrap"><h1>Pricing</h1>
    <p class="lede">Choose a plan for your network. Every price below is calculated by the server from one central catalogue and is an illustrative assumption for this prototype, not a validated market price.</p></div></section>
    <section style="padding-top:8px"><div class="wrap">
      <div class="period" role="group" aria-label="Billing period" id="period"></div>
      <div class="plans" id="planGrid"><p class="loading-line">Loading plans…</p></div>
      <p class="small muted" id="taxNote" style="margin-top:16px"></p></div></section>
    <section><div class="wrap"><div class="sec-head"><h2>Estimate your annual cost</h2><p>Tell us roughly how many chargers you manage. We suggest the smallest plan that fits and show the yearly total for the billing period you pick above.</p></div>
      <div class="calc"><div>
        <label class="field"><span>Managed chargers</span>
          <div class="range-row"><input type="range" id="chRange" min="1" max="150" value="4" aria-label="Managed chargers (slider)"><input type="number" id="chNum" min="1" max="100000" value="4" aria-label="Managed chargers"></div></label>
        <label class="field"><span>Sites</span><input type="number" id="siteNum" min="1" max="10000" value="1" aria-label="Sites" style="max-width:120px"></label>
        <p class="small muted">Simulated demo chargers and sites never count towards limits. ${esc("The suggestion is the smallest plan whose charger and site limits both fit.")}</p>
        <p class="small muted" style="margin-top:8px">Recommended starting plan: Starter. This is a product recommendation for small operators, not a statement about how many customers choose it.</p></div>
        <output id="calcOut" aria-live="polite"></output></div></div></section>
    <section><div class="wrap"><div class="sec-head"><h2>Compare features</h2><p>Limits and features are enforced by the server for your organisation, not just shown here.</p></div>
      <div class="compare-wrap" id="compare"></div></div></section>
    <section><div class="wrap band"><div><h2>Changing plans and renewals</h2>
      <ul class="ticks">
        <li>Upgrades start immediately. The unused part of your current paid period is credited against the new total.</li>
        <li>Downgrades and shorter billing periods take effect at your next renewal; nothing is refunded mid-period.</li>
        <li>Six- and twelve-month commitments are billed in advance for the whole period. Cancelling stops the next renewal; it does not refund the current period.</li>
        <li>If a renewal is not completed, the subscription is past due for a grace period and then becomes read-only. Your data is kept and stays readable, including safety history.</li>
      </ul></div>
      <div><h2>Need more than 100 chargers or 10 sites?</h2><p class="lede" style="font-size:17px">Enterprise limits, integrations, onboarding support and response targets are agreed per contract. We never sell unlimited resources we cannot support.</p>
      <p style="margin-top:20px"><a class="btn btn-primary" href="/enterprise">Talk to us about Enterprise</a></p></div></div></section>
    <section><div class="wrap"><h2 style="margin-bottom:24px">Pricing questions</h2>${faq(PRICING_FAQ)}</div></section>`;
}

const PRICING_FAQ = [
  ["Is any money taken at checkout?", "No. This prototype has no payment provider. The checkout is a clearly labelled simulation: it activates the plan in DEMO/TEST mode and issues a DEMO receipt, which is not a tax invoice. No card, UPI or bank details are ever requested."],
  ["Do prices include GST?", "No. Prices exclude GST, and the demo checkout applies no tax."],
  ["What do I need to connect an operator API?", "The operator's written authorisation, the API base URL, a read-only credential and an agreed data mapping. Credentials are encrypted when stored, never shown again and never sent to your browser. A paid plan alone never enables a connection."],
  ["How is my data protected?", "Passwords are hashed with Argon2id, sessions use HttpOnly cookies with CSRF protection, every request is checked against your organisation and role on the server, and operator credentials are encrypted at rest. The prototype has not had an independent security review yet."],
  ["Can I cancel?", "Yes, from Subscription & billing. Access continues until the end of the period you have paid for and the workspace then becomes read-only. Your data is not deleted."],
  ["What happens to the demo after I upgrade?", "Your simulated fleet stays available and remains labelled SIMULATED. Real chargers you import or connect appear separately under their own data source."],
];

async function wirePricing() {
  let months = Number(params.get("months")) || 12;
  let d;
  try { d = await plans(); } catch (e) { $("#planGrid").innerHTML = `<p class="form-msg error">${esc(e.message)}</p>`; return; }
  $("#taxNote").textContent = `${d.tax_note} ${d.disclaimer}`;
  const draw = () => {
    $("#period").innerHTML = d.periods.map(p => `<button type="button" data-m="${p.months}" aria-pressed="${p.months === months}">${esc(p.label)}${p.discount_percent ? `<small>−${p.discount_percent}%</small>` : ""}</button>`).join("");
    $("#period").querySelectorAll("button").forEach(b => b.onclick = () => { months = Number(b.dataset.m); draw(); });
    $("#planGrid").innerHTML = d.plans.map(p => planCard(p, months)).join("");
    calc();
  };
  const calc = () => {
    const n = Math.max(1, Number($("#chNum").value) || 1);
    const sites = Math.max(1, Number($("#siteNum").value) || 1);
    const fit = d.plans.find(p => p.self_serve && p.limits.chargers !== null && p.limits.chargers >= n && (p.limits.sites ?? 0) >= sites);
    const largest = d.plans.filter(p => p.self_serve).slice(-1)[0];
    if (!fit) { $("#calcOut").innerHTML = `<span class="muted">Suggested plan</span><div class="big">Enterprise</div><p>${n} charger${n === 1 ? "" : "s"} across ${sites} site${sites === 1 ? "" : "s"} is above the largest self-serve plan (${esc(largest.name)}: ${largest.limits.chargers} chargers, ${largest.limits.sites} sites). Enterprise pricing is quoted per contract.</p><a class="btn btn-secondary" href="/enterprise">Request a quotation</a>`; return; }
    const q = fit.quotes[String(months)];
    const yearly = q.total_paise * (12 / months);
    const inr = (paise) => "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    $("#calcOut").innerHTML = `<span class="muted">Suggested plan for ${n} charger${n === 1 ? "" : "s"} at ${sites} site${sites === 1 ? "" : "s"}</span><div class="big">${esc(fit.name)}: ${inr(yearly)} a year</div>
      <p class="small">${esc(q.calculation)}. ${months === 12 ? "One payment for 12 months" : `${12 / months} payments a year`}. Limits: ${fit.limits.chargers} managed chargers, ${fit.limits.sites} site${fit.limits.sites === 1 ? "" : "s"}, ${fit.limits.users} users. Excludes GST.</p>
      <a class="btn btn-primary" href="/signup?plan=${fit.key}&months=${months}">Start with ${esc(fit.name)}</a>`;
  };
  const sync = (v) => { $("#chNum").value = v; $("#chRange").value = Math.min(250, v); calc(); };
  $("#chRange").oninput = (e) => sync(e.target.value);
  $("#chNum").oninput = (e) => sync(e.target.value);
  $("#siteNum").oninput = () => calc();
  const feats = Object.entries(d.feature_labels);
  $("#compare").innerHTML = `<table class="compare"><thead><tr><th scope="col">Feature or limit</th>${d.plans.map(p => `<th scope="col">${esc(p.name)}</th>`).join("")}</tr></thead><tbody>
    ${[["chargers", "Managed chargers"], ["sites", "Sites"], ["users", "Organisation users"], ["integrations", "Configured integrations (operator API / OCPP)"]].map(([k, l]) => `<tr><td>${l}</td>${d.plans.map(p => `<td>${p.limits[k] === null || p.limits[k] === undefined ? "Negotiated" : p.key === "demo" && (k === "chargers" || k === "sites") ? "Simulated only" : p.limits[k]}</td>`).join("")}</tr>`).join("")}
    ${feats.map(([k, l]) => `<tr><td>${esc(l)}</td>${d.plans.map(p => p.features.includes(k) ? (p.key === "demo" ? '<td class="yes">On demo data<span class="sr-only"> in ' + esc(p.name) + "</span></td>" : '<td class="yes">Included<span class="sr-only"> in ' + esc(p.name) + "</span></td>") : '<td class="no">—<span class="sr-only">not included</span></td>').join("")}</tr>`).join("")}
    <tr><td>Support</td>${d.plans.map(p => `<td class="small">${esc(p.support)}</td>`).join("")}</tr></tbody></table>`;
  draw();
}

// ------------------------------------------------------------------ features page
function features() {
  return `<section class="page-head"><div class="wrap"><h1>Features</h1><p class="lede">What ChargeSight does today, shown on the simulated demo fleet. Every screenshot is the real application.</p></div></section>
    <section><div class="wrap band"><div class="shot">${shot(SHOT_DASH, "Operations console", "Fleet dashboard (SIMULATED data).")}</div>
      <div><h2>Fleet health and uptime</h2><ul class="ticks"><li>Uptime, charging, faulted and offline counts across sites</li><li>Live alerts and a dispatch-needed list when no qualified technician is free</li><li>Four strictly separate data sources: simulated, imported, operator API and OCPP</li><li>Scenarios such as high demand, degraded network and a major incident for training</li></ul></div></div></section>
    <section><div class="wrap band flip"><div class="shot">${shot(SHOT_PRIORITY, "Priority engine ranking with factor breakdown", "Priority engine (SIMULATED data).")}</div>
      <div><h2>Explainable prioritisation</h2><ul class="ticks"><li>P = wS·S + wU·U + wR·R + wA·A with visible, adjustable weights</li><li>Safety faults are always critical and never downgraded</li><li>Starvation guard: tickets waiting 24 hours move to the front</li><li>Every change of priority is recorded with its reason</li></ul></div></div></section>
    <section><div class="wrap band"><div class="shot">${shot(SHOT_TICKETS, "Maintenance tickets", "Maintenance tickets (SIMULATED data).")}</div>
      <div><h2>Ticketing, dispatch and SLAs</h2><ul class="ticks"><li>Raise a ticket from the dashboard, a charger or the tickets page</li><li>Automatic assignment by skill, equipment, certification, shift and workload</li><li>Open, assigned, en route, on site, waiting, resolved and closed, validated on the server</li><li>SLA deadlines and a single escalation when they pass</li></ul></div></div></section>
    <section><div class="wrap band flip"><div class="shot">${shot(SHOT_MAP, "Map of public charging listings in Bengaluru", "Public listings map (Open Charge Map data, not live status).")}</div>
      <div><h2>Maps, planning and reports</h2><ul class="ticks"><li>Public listings from Open Charge Map and OpenStreetMap, with attribution and no claim of live status</li><li>Candidate-site scoring with evidence classes and missing-data warnings</li><li>Financial feasibility with transparent assumptions</li><li>CSV exports scoped to your organisation and a printable summary</li></ul></div></div></section>
    <section><div class="wrap"><div class="sec-head"><h2>Integrations</h2><p>Real data needs the operator's permission. ChargeSight never guesses endpoints, scrapes apps or shows placeholder data as live.</p></div>
      <div class="truth"><div class="is"><h3>CSV import</h3><p>Validate an operator export against the event schema and get a report of accepted, duplicate and rejected rows.</p></div>
      <div class="is"><h3>Operator REST API</h3><p>Encrypted credentials, https-only destinations, timeouts and bounded retries, and an explicit connection test before any sync.</p></div>
      <div class="is"><h3>OCPP 1.6J</h3><p>A central system with an endpoint per organisation. The local test simulator is always labelled test traffic.</p></div>
      <div class="is"><h3>Roles and audit</h3><p>Owner, admin, operations manager, technician and viewer roles enforced on the server, with an audit log.</p></div></div>
      <p style="margin-top:32px"><a class="btn btn-primary" href="/demo">Start free demo</a> <a class="btn btn-secondary" href="/pricing">See pricing</a></p></div></section>`;
}

// ------------------------------------------------------------------ enterprise / contact
function enquiryPage(kind) {
  const ent = kind === "enterprise";
  return `<section class="page-head"><div class="wrap"><h1>${ent ? "Enterprise" : "Contact"}</h1>
    <p class="lede">${ent ? "For large and multi-site networks: negotiated limits, multiple operator integrations, custom onboarding, audit and retention settings and agreed support targets." : "Questions about ChargeSight, the prototype or the capstone project."}</p></div></section>
    <section style="padding-top:0"><div class="wrap"><div class="auth-card wide" style="margin:0">
      <form id="enq" novalidate>
        <div id="enqMsg" class="form-msg" role="status"></div>
        <div class="row2">${fld("org_name", "Organisation name", "text", true)}${fld("contact_name", "Your name", "text", false)}</div>
        <div class="row2">${fld("contact_email", "Work e-mail", "email", true)}${fld("phone", "Phone", "tel", false)}</div>
        ${ent ? `<div class="row2">${fld("charger_count", "Estimated chargers", "number", false)}${fld("site_count", "Number of sites", "number", false)}</div>
        <label class="field"><span>Integration requirements <span class="opt">(optional)</span></span><textarea name="integration_requirements" maxlength="4000" placeholder="For example: OCPP 1.6J charge points, an operator REST API, CSV exports"></textarea></label>` : ""}
        <label class="field"><span>Message <span class="opt">(optional)</span></span><textarea name="message" maxlength="4000"></textarea></label>
        <input type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true" style="position:absolute;left:-9999px">
        <button class="btn btn-primary" id="enqBtn">Send enquiry</button>
        <p class="small muted" style="margin-top:12px">Your enquiry is stored in ChargeSight for a platform administrator to review. No automatic e-mail is sent from this prototype.</p>
      </form></div></div></section>`;
}

function fld(name, label, type, required, extra = "") {
  return `<label class="field" data-f="${name}"><span>${esc(label)}${required ? "" : ' <span class="opt">(optional)</span>'}</span>
    <input type="${type}" name="${name}" ${required ? "required" : ""} ${extra} autocomplete="${{ contact_email: "email", email: "email", contact_name: "name", full_name: "name", phone: "tel", contact_phone: "tel", org_name: "organization", organisation_name: "organization" }[name] || "off"}"><div class="err" id="err-${name}"></div></label>`;
}

function wireEnquiry(kind) {
  const f = $("#enq");
  f.onsubmit = async (e) => {
    e.preventDefault();
    clearErrors(f);
    const v = Object.fromEntries(new FormData(f));
    const errs = {};
    if ((v.org_name || "").trim().length < 2) errs.org_name = "Enter your organisation's name.";
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v.contact_email || "")) errs.contact_email = "Enter a valid e-mail address.";
    if (showErrors(f, errs)) return;
    const body = { kind, org_name: v.org_name.trim(), contact_name: (v.contact_name || "").trim(), contact_email: v.contact_email.trim(), phone: (v.phone || "").trim(),
      message: (v.message || "").trim(), integration_requirements: (v.integration_requirements || "").trim(), website: v.website || "",
      charger_count: v.charger_count ? Number(v.charger_count) : null, site_count: v.site_count ? Number(v.site_count) : null };
    const btn = $("#enqBtn"); busy(btn, true, "Sending…");
    try {
      const r = await api("/api/public/enquiries", { method: "POST", body });
      f.querySelectorAll("input,textarea,button").forEach(i => i.disabled = true);
      msg($("#enqMsg"), "ok", r.message || "Thank you, your enquiry has been stored.");
    } catch (err) { showServerErrors(f, err); msg($("#enqMsg"), "error", err.message); busy(btn, false, "Send enquiry"); }
  };
}

// ------------------------------------------------------------------ auth pages
function pwField(name, label, autocomplete) {
  return `<label class="field" data-f="${name}"><span>${esc(label)}</span><div class="pw"><input type="password" name="${name}" required autocomplete="${autocomplete}">
    <button type="button" class="pw-toggle" aria-label="Show password" aria-pressed="false">Show</button></div><div class="err" id="err-${name}"></div></label>`;
}
function wirePw(root = document) {
  root.querySelectorAll(".pw-toggle").forEach(b => b.onclick = () => {
    const i = b.previousElementSibling, show = i.type === "password";
    i.type = show ? "text" : "password"; b.textContent = show ? "Hide" : "Show";
    b.setAttribute("aria-pressed", String(show)); b.setAttribute("aria-label", show ? "Hide password" : "Show password");
  });
}
function busy(btn, on, label) { btn.disabled = on; btn.setAttribute("aria-busy", String(on)); btn.textContent = label; }
function msg(el, kind, text) { el.className = `form-msg ${kind}`; el.textContent = text; }
function clearErrors(f) { f.querySelectorAll(".field").forEach(x => x.classList.remove("invalid")); f.querySelectorAll(".err").forEach(x => x.textContent = ""); }
function showErrors(f, errs) {
  let first = null;
  for (const [k, v] of Object.entries(errs)) {
    const box = f.querySelector(`#err-${k}`), field = f.querySelector(`[data-f="${k}"]`);
    if (box) box.textContent = v;
    if (field) { field.classList.add("invalid"); first = first || field.querySelector("input,select,textarea"); }
    const inp = field && field.querySelector("input,select,textarea");
    if (inp) { inp.setAttribute("aria-invalid", "true"); inp.setAttribute("aria-describedby", `err-${k}`); }
  }
  if (first) first.focus();
  return Object.keys(errs).length > 0;
}
function showServerErrors(f, err) { if (err.detail && err.detail.fields) showErrors(f, err.detail.fields); }

function strength(pw) {
  let s = 0;
  if (pw.length >= 10) s++;
  if (pw.length >= 16) s++;
  s += [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter(r => r.test(pw)).length >= 3 ? 1 : 0;
  return Math.min(3, s);
}
function passwordRules(pw, email = "", name = "") {
  if (pw.length < 10) return "Use at least 10 characters.";
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter(r => r.test(pw)).length;
  if (pw.length < 16 && classes < 3) return "Mix at least three of: lower case, upper case, digits, symbols (or use 16+ characters).";
  const local = email.split("@")[0].toLowerCase();
  if (local.length >= 4 && pw.toLowerCase().includes(local)) return "Do not include your e-mail address in the password.";
  return "";
}

function signedInCard(title) {
  const demo = params.get("intent") === "demo";
  return `<div class="auth"><div class="auth-card"><h1>${esc(title)}</h1>
    <div class="form-msg info" role="status">You are already signed in as <b>${esc(me.user.full_name)}</b> (${esc(me.user.email)}).</div>
    <a class="btn btn-primary btn-block" href="${esc(safeNext(params.get("next")) || "/app")}">Open workspace</a>
    ${demo ? '<button type="button" class="btn btn-secondary btn-block" id="siDemo" style="margin-top:10px">Open my Presentation Demo</button>' : ""}
    <button type="button" class="btn btn-secondary btn-block" id="siSwitch" style="margin-top:10px">Sign out and use another account</button>
    <p class="alt"><a href="/">Back to the home page</a></p></div></div>`;
}
function wireSignedIn() {
  $("#siSwitch").onclick = () => signOutHere(location.pathname + (params.get("intent") ? `?intent=${encodeURIComponent(params.get("intent"))}` : ""));
  const d = $("#siDemo");
  if (d) d.onclick = async () => { busy(d, true, "Opening the Presentation Demo…"); try { await openPresentationDemo(me.csrf_token); } catch (e) { busy(d, false, "Open my Presentation Demo"); alert(e.message); } };
}

function signup() {
  if (me) return signedInCard("Create an account");
  const plan = params.get("plan");
  return `<div class="auth"><div class="auth-card wide">
    <h1>Create your ChargeSight account</h1>
    ${params.get("intent") === "demo" ? '<div class="form-msg info" role="status">Create a free account to open your private <b>Presentation Demo</b>: a separate workspace with SIMULATED data only. Your account is created through the normal secure sign-up; no payment details are needed.</div>' : ""}
    <p class="sub">Your organisation starts on the free Demo / Sandbox plan. Explore with the separate Presentation Demo (SIMULATED data) or add your own data.${plan ? ` You can choose the ${esc(plan[0].toUpperCase() + plan.slice(1))} plan right after setup.` : ""} No payment details needed.</p>
    <form id="su" novalidate><div id="suMsg" class="form-msg" role="alert"></div>
      <div class="row2">${fld("full_name", "Full name", "text", true, 'maxlength="120"')}${fld("email", "Work e-mail", "email", true, 'maxlength="254"')}</div>
      <div class="row2">${pwField("password", "Password", "new-password")}${pwField("confirm_password", "Confirm password", "new-password")}</div>
      <div class="strength" aria-hidden="true"><i id="pwBar"></i></div><p class="small muted" style="margin:6px 0 16px">At least 10 characters with three of: lower case, upper case, digits, symbols, or 16+ characters.</p>
      <div class="row2">${fld("organisation_name", "Organisation name", "text", true, 'maxlength="120"')}
        <label class="field" data-f="organisation_type"><span>Organisation type</span><select name="organisation_type" required>
          <option value="">Choose…</option><option value="cpo">Charging Point Operator</option><option value="fleet_operator">EV fleet operator</option>
          <option value="service_provider">Charging infrastructure service provider</option><option value="site_owner">Property / site owner</option><option value="other">Other</option></select><div class="err" id="err-organisation_type"></div></label></div>
      <div class="row2"><label class="field" data-f="country"><span>Country</span><select name="country" required>${["India", "Bangladesh", "Nepal", "Sri Lanka", "United Arab Emirates", "Singapore", "United Kingdom", "United States", "Other"].map(c => `<option ${c === "India" ? "selected" : ""}>${c}</option>`).join("")}</select><div class="err" id="err-country"></div></label>
        ${fld("contact_phone", "Business phone", "tel", false, 'maxlength="40"')}</div>
      ${fld("website", "Company website", "url", false, 'maxlength="200" placeholder="https://"')}
      <label class="check" data-f="accept_terms"><input type="checkbox" name="accept_terms"> <span>I accept the <a href="/terms" target="_blank" rel="noopener">prototype terms of use</a> and have read the <a href="/privacy" target="_blank" rel="noopener">privacy notice</a>.</span></label><div class="err" id="err-accept_terms" style="margin-top:-10px;margin-bottom:12px"></div>
      <button class="btn btn-primary btn-block" id="suBtn">Create account</button>
      <p class="alt">Already have an account? <a href="/signin${params.get("intent") === "demo" ? "?intent=demo" : ""}">Sign in</a></p></form></div></div>`;
}

function wireSignup() {
  if (me) return wireSignedIn();
  const f = $("#su");
  wirePw(f);
  f.password.oninput = () => { const s = strength(f.password.value); const bar = $("#pwBar"); bar.style.width = `${[8, 40, 70, 100][s]}%`; bar.style.background = ["#b91c1c", "#b45309", "#2f5bea", "#15803d"][s]; };
  f.onsubmit = async (e) => {
    e.preventDefault();
    clearErrors(f); $("#suMsg").className = "form-msg"; $("#suMsg").textContent = "";
    const v = Object.fromEntries(new FormData(f));
    const errs = {};
    if ((v.full_name || "").trim().length < 2) errs.full_name = "Enter your full name.";
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v.email || "")) errs.email = "Enter a valid work e-mail address.";
    const pr = passwordRules(v.password || "", v.email || "", v.full_name || "");
    if (pr) errs.password = pr;
    if (v.password !== v.confirm_password) errs.confirm_password = "The passwords do not match.";
    if ((v.organisation_name || "").trim().length < 2) errs.organisation_name = "Enter your organisation's name.";
    if (!v.organisation_type) errs.organisation_type = "Choose an organisation type.";
    if (!f.accept_terms.checked) errs.accept_terms = "Please accept the prototype terms and privacy notice.";
    if (showErrors(f, errs)) return;
    const btn = $("#suBtn"); busy(btn, true, "Creating your account…");
    try {
      const r = await api("/api/auth/signup", { method: "POST", body: { ...v, accept_terms: true } });
      const plan = params.get("plan");
      if (params.get("intent") === "demo") { busy(btn, true, "Opening your Presentation Demo…"); await openPresentationDemo(r.csrf_token); return; }
      location.href = plan && plan !== "demo" ? "/app#billing" : (r.next || "/app#onboarding");
    } catch (err) { showServerErrors(f, err); msg($("#suMsg"), "error", err.message); busy(btn, false, "Create account"); }
  };
}

function signin() {
  if (me) return signedInCard("Sign in");
  const out = params.get("signed_out"), reset = params.get("reset");
  return `<div class="auth"><div class="auth-card">
    <h1>Sign in</h1><p class="sub">Welcome back to ChargeSight.</p>
    ${params.get("intent") === "demo" ? '<div class="form-msg info" role="status">Sign in to open your private <b>Presentation Demo</b> (SIMULATED data, separate from your own workspace).</div>' : ""}
    <form id="si" novalidate><div id="siMsg" class="form-msg ${out ? "ok" : reset ? "ok" : ""}" role="alert">${out ? "You have been signed out." : reset ? "Password updated. Sign in with your new password." : ""}</div>
      ${fld("email", "E-mail", "email", true)}${pwField("password", "Password", "current-password")}
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
        <label class="check" style="margin:0"><input type="checkbox" name="remember"> Keep me signed in for 30 days</label><a class="small" href="/forgot-password">Forgot password?</a></div>
      <button class="btn btn-primary btn-block" id="siBtn" style="margin-top:18px">Sign in</button>
      <p class="alt">New to ChargeSight? <a href="/signup${params.get("intent") === "demo" ? "?intent=demo" : ""}">Create an account</a></p></form></div></div>`;
}

function wireSignin() {
  if (me) return wireSignedIn();
  const f = $("#si");
  wirePw(f);
  f.onsubmit = async (e) => {
    e.preventDefault();
    clearErrors(f);
    const errs = {};
    if (!f.email.value.trim()) errs.email = "Enter your e-mail address.";
    if (!f.password.value) errs.password = "Enter your password.";
    if (showErrors(f, errs)) return;
    const btn = $("#siBtn"); busy(btn, true, "Signing in…");
    try {
      const r = await api("/api/auth/signin", { method: "POST", body: { email: f.email.value.trim(), password: f.password.value, remember: f.remember.checked } });
      if (params.get("intent") === "demo" && r.organisation) { busy(btn, true, "Opening your Presentation Demo…"); await openPresentationDemo(r.csrf_token); return; }
      location.href = safeNext(params.get("next")) || r.next || "/app";
    } catch (err) { msg($("#siMsg"), "error", err.message); busy(btn, false, "Sign in"); f.password.value = ""; f.password.focus(); }
  };
}

function forgot() {
  return `<div class="auth"><div class="auth-card"><h1>Reset your password</h1>
    <p class="sub">Enter the e-mail address of your account.</p>
    <div class="form-msg info">E-mail delivery is not configured in this prototype, so no reset e-mail is sent. In a local development setup the reset link is written to the server's <span style="font-family:var(--f-mono)">data/dev_outbox</span> folder. An administrator can also reset a password with <span style="font-family:var(--f-mono)">scripts/manage.py reset-password</span>.</div>
    <form id="fp" novalidate><div id="fpMsg" class="form-msg" role="status"></div>${fld("email", "E-mail", "email", true)}
      <button class="btn btn-primary btn-block" id="fpBtn">Prepare reset link</button><p class="alt"><a href="/signin">Back to sign in</a></p></form></div></div>`;
}
function wireForgot() {
  const f = $("#fp");
  f.onsubmit = async (e) => {
    e.preventDefault(); clearErrors(f);
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(f.email.value)) { showErrors(f, { email: "Enter a valid e-mail address." }); return; }
    const btn = $("#fpBtn"); busy(btn, true, "Working…");
    try { const r = await api("/api/auth/forgot-password", { method: "POST", body: { email: f.email.value.trim() } }); msg($("#fpMsg"), "ok", r.message); }
    catch (err) { msg($("#fpMsg"), "error", err.message); }
    busy(btn, false, "Prepare reset link");
  };
}

function resetPage() {
  return `<div class="auth"><div class="auth-card"><h1>Choose a new password</h1><p class="sub">Your other sessions will be signed out.</p>
    <form id="rp" novalidate><div id="rpMsg" class="form-msg" role="alert">${params.get("token") ? "" : "This page needs the link from the reset message."}</div>
      ${pwField("password", "New password", "new-password")}${pwField("confirm_password", "Confirm new password", "new-password")}
      <button class="btn btn-primary btn-block" id="rpBtn" ${params.get("token") ? "" : "disabled"}>Update password</button></form></div></div>`;
}
function wireReset() {
  const f = $("#rp"); wirePw(f);
  if (params.get("token")) $("#rpMsg").className = "form-msg";
  f.onsubmit = async (e) => {
    e.preventDefault(); clearErrors(f);
    const errs = {};
    const pr = passwordRules(f.password.value); if (pr) errs.password = pr;
    if (f.password.value !== f.confirm_password.value) errs.confirm_password = "The passwords do not match.";
    if (showErrors(f, errs)) return;
    const btn = $("#rpBtn"); busy(btn, true, "Updating…");
    try { await api("/api/auth/reset-password", { method: "POST", body: { token: params.get("token"), password: f.password.value, confirm_password: f.confirm_password.value } }); location.href = "/signin?reset=1"; }
    catch (err) { showServerErrors(f, err); msg($("#rpMsg"), "error", err.message); busy(btn, false, "Update password"); }
  };
}

function verifyPage() { return `<div class="auth"><div class="auth-card"><h1>Verify your e-mail</h1><div id="vMsg" class="form-msg info" role="status">Checking your link…</div><p class="alt"><a href="/app">Open your workspace</a></p></div></div>`; }
async function wireVerify() {
  const t = params.get("token");
  if (!t) { msg($("#vMsg"), "error", "This page needs the link from the verification message."); return; }
  try { const r = await api("/api/auth/verify-email", { method: "POST", body: { token: t } }); msg($("#vMsg"), "ok", `${r.email} is verified.`); }
  catch (err) { msg($("#vMsg"), "error", err.message); }
}

function invitePage() { return `<div class="auth"><div class="auth-card" id="inviteBox"><h1>Join an organisation</h1><p class="loading-line">Checking your invitation…</p></div></div>`; }
async function wireInvite() {
  const box = $("#inviteBox"), token = params.get("token");
  let inv;
  try { inv = await api(`/api/auth/invitations/${encodeURIComponent(token || "")}`); }
  catch (err) { box.innerHTML = `<h1>Invitation unavailable</h1><div class="form-msg error">${esc(err.message)}</div><p class="alt"><a href="/signin">Sign in</a></p>`; return; }
  const head = `<h1>Join ${esc(inv.organisation)}</h1><p class="sub">You were invited as <b>${esc(inv.role_label)}</b> using ${esc(inv.email)}.</p>`;
  if (me) {
    box.innerHTML = `${head}<div id="iMsg" class="form-msg" role="alert"></div>${me.user.email === inv.email ? "" : `<div class="form-msg info">You are signed in as ${esc(me.user.email)}. Sign in with ${esc(inv.email)} to accept.</div>`}
      <button class="btn btn-primary btn-block" id="acc" ${me.user.email === inv.email ? "" : "disabled"}>Accept invitation</button>`;
    $("#acc").onclick = async () => {
      try { await api(`/api/auth/invitations/${encodeURIComponent(token)}/accept`, { method: "POST", csrf: me.csrf_token }); location.href = "/app"; }
      catch (err) { msg($("#iMsg"), "error", err.message); }
    };
    return;
  }
  if (inv.has_account) {
    box.innerHTML = `${head}<div class="form-msg info">You already have a ChargeSight account. Sign in with ${esc(inv.email)}, then open this link again to accept.</div>
      <a class="btn btn-primary btn-block" href="/signin?next=${encodeURIComponent("/app")}">Sign in</a>`;
    return;
  }
  box.innerHTML = `${head}<form id="iv" novalidate><div id="iMsg" class="form-msg" role="alert"></div>
    ${fld("full_name", "Full name", "text", true)}${pwField("password", "Password", "new-password")}${pwField("confirm_password", "Confirm password", "new-password")}
    <label class="check" data-f="accept_terms"><input type="checkbox" name="accept_terms"> <span>I accept the <a href="/terms" target="_blank" rel="noopener">prototype terms</a> and <a href="/privacy" target="_blank" rel="noopener">privacy notice</a>.</span></label><div class="err" id="err-accept_terms"></div>
    <button class="btn btn-primary btn-block" id="ivBtn">Create account and join</button></form>`;
  const f = $("#iv"); wirePw(f);
  f.onsubmit = async (e) => {
    e.preventDefault(); clearErrors(f);
    const errs = {};
    if (f.full_name.value.trim().length < 2) errs.full_name = "Enter your full name.";
    const pr = passwordRules(f.password.value, inv.email, f.full_name.value); if (pr) errs.password = pr;
    if (f.password.value !== f.confirm_password.value) errs.confirm_password = "The passwords do not match.";
    if (!f.accept_terms.checked) errs.accept_terms = "Please accept the prototype terms and privacy notice.";
    if (showErrors(f, errs)) return;
    const btn = $("#ivBtn"); busy(btn, true, "Creating…");
    try { await api(`/api/auth/invitations/${encodeURIComponent(token)}/signup`, { method: "POST", body: { full_name: f.full_name.value.trim(), password: f.password.value, confirm_password: f.confirm_password.value, accept_terms: true } }); location.href = "/app"; }
    catch (err) { showServerErrors(f, err); msg($("#iMsg"), "error", err.message); busy(btn, false, "Create account and join"); }
  };
}

// ------------------------------------------------------------------ policy pages
function legal(title, intro, sections) {
  return `<section class="page-head"><div class="wrap prose"><h1>${esc(title)}</h1><p class="lede">${esc(intro)}</p>
    <p class="badge" style="margin-top:20px"><b aria-hidden="true"></b>Prototype document, to be finalised with legal advice before any commercial launch</p></div></section>
    <section style="padding-top:0"><div class="wrap prose">${sections.map(([h, ps]) => `<h2 style="font-size:22px;margin-top:32px">${esc(h)}</h2>${ps.map(p => `<p>${esc(p)}</p>`).join("")}`).join("")}</div></section>`;
}
const TERMS = ["Terms of use", "These terms describe how the ChargeSight prototype may be used during evaluation.", [
  ["What ChargeSight is", ["ChargeSight is an academic prototype developed for a PME capstone at BMS College of Engineering. It is provided for evaluation and demonstration, without any warranty of availability or fitness for a particular purpose."]],
  ["Your account", ["Keep your password confidential. You are responsible for activity in your organisation's workspace. Organisation owners decide who has access and with which role."]],
  ["Data you add", ["Only import or connect data you are authorised to use. Operator data requires the operator's written permission. You can ask for your organisation's data to be exported or removed by contacting the platform administrator."]],
  ["Subscriptions and payments", ["Plans and prices are illustrative. The checkout is a simulation: no payment is taken, receipts are marked DEMO and are not tax invoices. No refunds arise because no money is charged."]],
  ["Acceptable use", ["Do not attempt to access another organisation's data, disrupt the service, or use it to process data unlawfully."]],
]];
const PRIVACY = ["Privacy notice", "What the ChargeSight prototype stores about you and why.", [
  ["What we store", ["Account details (name, e-mail, a hashed password), organisation details you enter, sign-in sessions, an audit log of security-relevant actions (including the IP address of sign-in attempts), and operational data your organisation adds.", "Passwords are stored only as Argon2id hashes. Operator API credentials are encrypted at rest with a key held outside the database."]],
  ["What we do not do", ["We do not sell data, show advertising or send marketing e-mail. The prototype does not send any e-mail at all."]],
  ["Cookies", ["One strictly necessary session cookie keeps you signed in (HttpOnly, SameSite=Lax). The console remembers a few interface choices, such as the theme, in your browser's local storage; no secrets are stored there."]],
  ["Retention and your rights", ["Data is kept while the prototype is in use. Automatic deletion schedules are not implemented yet. You can ask the platform administrator to export or delete your account and your organisation's data."]],
]];
const SECURITY = ["Security", "The controls implemented in this prototype, and what has not been done yet.", [
  ["Implemented", ["Argon2id password hashing; server-side sessions in HttpOnly SameSite cookies (Secure in production); CSRF tokens on every state-changing request and an Origin check; rate limits on sign-in, sign-up, password reset and public forms; generic sign-in errors; session expiry and idle timeout; organisation isolation enforced on every database query; role permissions checked on the server; encrypted operator credentials; outbound URL checks against private-network destinations; secrets redacted from logs."]],
  ["Not yet done", ["No independent security review or penetration test. Rate limits are per server process. E-mail verification and password reset links are not delivered by e-mail. These must be addressed before production use."]],
]];

// ------------------------------------------------------------------ Start free demo (v10.1)
// No anonymous or shared demo account exists: the Presentation Demo is a private, SIMULATED workspace created for the
// signed-in user. Visitors are asked to sign up or sign in first; nothing is opened without authentication.
function demoPage() {
  return `<div class="auth"><div class="auth-card"><h1>Start the free demo</h1>
    ${me ? `<div class="form-msg info" role="status" id="demoMsg">Opening your private Presentation Demo as <b>${esc(me.user.full_name)}</b>…</div>
            <p class="small muted">The first time, the SIMULATED sample fleet is built for you (about 15–60 seconds). It is separate from your own workspace.</p>
            <a class="btn btn-secondary btn-block" href="/app">Open my own workspace instead</a>`
         : `<p class="sub">The Presentation Demo is a private workspace with SIMULATED data only: 6 demonstration sites, 40 synthetic chargers, fictional technicians and scripted maintenance tickets. It opens after you sign in, so it is never shared with anyone else and never mixed with real records.</p>
            <a class="btn btn-primary btn-block" href="/signup?intent=demo">Sign up free</a>
            <a class="btn btn-secondary btn-block" style="margin-top:10px" href="/signin?intent=demo">I already have an account: sign in</a>
            <p class="alt">No payment details are needed.</p>`}</div></div>`;
}
async function wireDemo() {
  if (!me) return;
  try { await openPresentationDemo(me.csrf_token); }
  catch (e) { const m = $("#demoMsg"); m.className = "form-msg error"; m.textContent = e.message; }
}

// ------------------------------------------------------------------ boot
const PAGES = {
  "/": { html: landing, after: fillPlanPreview, title: "ChargeSight — EV charger uptime and maintenance intelligence" },
  "/features": { html: features, title: "Features — ChargeSight" },
  "/pricing": { html: pricing, after: wirePricing, title: "Pricing — ChargeSight" },
  "/enterprise": { html: () => enquiryPage("enterprise"), after: () => wireEnquiry("enterprise"), title: "Enterprise — ChargeSight" },
  "/contact": { html: () => enquiryPage("contact"), after: () => wireEnquiry("contact"), title: "Contact — ChargeSight" },
  "/signup": { html: signup, after: wireSignup, title: "Create account — ChargeSight" },
  "/signin": { html: signin, after: wireSignin, title: "Sign in — ChargeSight" },
  "/demo": { html: demoPage, after: wireDemo, title: "Start free demo — ChargeSight" },
  "/forgot-password": { html: forgot, after: wireForgot, title: "Reset password — ChargeSight" },
  "/reset-password": { html: resetPage, after: wireReset, title: "New password — ChargeSight" },
  "/verify-email": { html: verifyPage, after: wireVerify, title: "Verify e-mail — ChargeSight" },
  "/accept-invite": { html: invitePage, after: wireInvite, title: "Invitation — ChargeSight" },
  "/terms": { html: () => legal(...TERMS), title: "Terms of use — ChargeSight" },
  "/privacy": { html: () => legal(...PRIVACY), title: "Privacy notice — ChargeSight" },
  "/security": { html: () => legal(...SECURITY), title: "Security — ChargeSight" },
};

const AUTH_PAGES = new Set(["/signin", "/signup", "/demo", "/forgot-password", "/reset-password", "/verify-email", "/accept-invite"]);

async function boot() {
  try { me = await api("/api/auth/me"); } catch { me = null; }
  const page = PAGES[path] || { html: () => `<section class="page-head"><div class="wrap"><h1>Page not found</h1><p class="lede">There is nothing at this address. <a href="/">Go to the home page</a>.</p></div></section>`, title: "Not found — ChargeSight" };
  document.title = page.title;
  // v9.2: only the authentication pages get the dark navy design (scoped to body.auth-page in site.css)
  document.body.classList.toggle("auth-page", AUTH_PAGES.has(path));
  header();
  $("#main").innerHTML = page.html();
  footer();
  if (page.after) page.after();
}
boot();
