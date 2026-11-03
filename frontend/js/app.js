// Router, global state and polling.
import { api, setCsrf } from "./api.js";
import { esc, toast, drawerOpen, tag } from "./ui.js";
import * as analytics from "./views/analytics.js";
import * as dashboard from "./views/dashboard.js";
import * as map from "./views/map.js";
import * as tickets from "./views/tickets.js";
import * as simulator from "./views/simulator.js";
import * as priority from "./views/priority.js";
import * as experiments from "./views/experiments.js";
import * as investment from "./views/investment.js";
import * as integrations from "./views/integrations.js";
import * as validation from "./views/validation.js";
import * as reports from "./views/reports.js";
import * as technicians from "./views/technicians.js";
import * as onboarding from "./views/onboarding.js";
import * as billing from "./views/billing.js";
import * as historical from "./views/historical.js";
import * as orgView from "./views/org.js";
import * as profile from "./views/profile.js";
import * as admin from "./views/admin.js";

const ROUTES = {
  dashboard: { mod: dashboard, title: "Operations console", sub: "Fleet uptime, alerts, chargers, tickets and live-style telemetry" },
  analytics: { mod: analytics, title: "Analytics", sub: "Trends from stored history: faults, power, energy, MTTA/MTTR, backlog, sites, policies, investment" },
  map: { mod: map, title: "Map", sub: "Real public EV charging listings in Bengaluru (not live status), with optional demo and proposed-site layers" },
  tickets: { mod: tickets, title: "Maintenance tickets", sub: "Raise · prioritise · assign · repair · verify — Open → Assigned → En Route → On Site → Resolved → Closed" },
  technicians: { mod: technicians, title: "Technicians", sub: "Registry, availability, skills and the automatic assignment policy" },
  simulator: { mod: simulator, title: "Simulator & event timeline", sub: "Deterministic simulation controls, fault injection and the unified event timeline" },
  priority: { mod: priority, title: "Priority engine", sub: "P = wS·S + wU·U + wR·R + wA·A, explained per ticket" },
  experiments: { mod: experiments, title: "Repair policy lab", sub: "FIFO vs explainable priority on identical fault traces" },
  investment: { mod: investment, title: "Site feasibility", sub: "Transparent investment model for proposed charging sites" },
  integrations: { mod: integrations, title: "Data integrations", sub: "Authorised operator data, imports and connection health" },
  historical: { mod: historical, title: "Historical charging sessions", sub: "IEEE charging records research dataset · sessions, duration, demand, charger types and analytics" },
  validation: { mod: validation, title: "Validation", sub: "Operator interviews, historical data and estimate checks" },
  reports: { mod: reports, title: "Reports & exports", sub: "CSV exports and a printable summary" },
  onboarding: { mod: onboarding, title: "Workspace setup", sub: "Six steps to a working ChargeSight workspace; progress is saved on the server" },
  billing: { mod: billing, title: "Subscription & billing", sub: "Plan overview, usage, subscription tiers and order receipts" },
  organisation: { mod: orgView, title: "Organisation", sub: "Profile, members, invitations and audit log" },
  profile: { mod: profile, title: "Your profile", sub: "Name, password and signed-in sessions" },
  admin: { mod: admin, title: "Platform administration", sub: "Organisations, subscriptions, plan catalogue and enquiries (platform administrators only)" },
};
// Routes that need a plan feature (the server enforces it; the menu only explains it)
const ROUTE_FEATURE = { analytics: "analytics_basic", experiments: "policy_lab", investment: "site_planning", validation: "validation",
  map: "public_map", technicians: "manual_assignment", simulator: "simulator", priority: "priority_engine" };
const ROUTE_PERM = { analytics: "read.analysis", experiments: "read.analysis", investment: "read.analysis", validation: "read.analysis",
  reports: "read.analysis", integrations: "read.analysis", billing: "billing.view", organisation: "org.manage" };

export const state = {
  me: null,
  dashSel: null,
  config: null,
  source: localStorageGet("cs.source") || "simulation",
  sim: null,
  route: null,
};

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

const view = document.getElementById("view");
let current = null;
let renderToken = 0;

async function navigate() {
  const name = (location.hash || "#dashboard").slice(1).split("?")[0];
  let route = ROUTES[name] || ROUTES.dashboard;
  state.route = ROUTES[name] ? name : "dashboard";
  if (!state.me.organisation && state.route !== "profile" && state.route !== "admin") {
    route = state.me.user.is_platform_admin ? ROUTES.admin : ROUTES.profile;
    state.route = state.me.user.is_platform_admin ? "admin" : "profile";
  }
  document.querySelectorAll(".nav a").forEach(a => a.classList.toggle("active", a.dataset.route === state.route));
  document.getElementById("pageTitle").textContent = route.title;
  document.getElementById("pageSub").textContent = route.sub;
  document.getElementById("nav").classList.remove("open");
  if (current && current.unmount) current.unmount();
  current = route.mod;
  const token = ++renderToken;
  view.innerHTML = '<div class="loading">Loading…</div>';
  try {
    await route.mod.render(view, ctx(token));
  } catch (e) {
    if (token !== renderToken) return;
    view.innerHTML = `<div class="error-box"><span>${esc(e.message)}</span><button class="sm" id="retryView">Retry</button></div>`;
    document.getElementById("retryView").onclick = navigate;
  }
}

function ctx(token) {
  return {
    state,
    alive: () => token === renderToken,
    rerender: navigate,
  };
}

// Fallback badge when the status request fails: never claim LIVE without evidence.
const SOURCE_TAG = {
  simulation: "SIMULATED",
  simulated_imported: "SIMULATED IMPORTED",
  station_inventory: "IMPORTED INVENTORY",
  historical_sessions: "HISTORICAL RESEARCH",
  operator_import: "IMPORTED",
  operator_api: "UNKNOWN",
  ocpp: "UNKNOWN",
};
const MODE_PILL = {
  "SIMULATED": "st-charging",
  "SIMULATING IMPORTED": "st-charging",
  "NOT SIMULATING": "st-offline",
  "RECEIVING DATA": "st-available",
  "RECEIVING TEST DATA": "st-unavailable",
  "IMPORTED": "st-available",
  "IMPORTED INVENTORY": "st-available",
  "HISTORICAL RESEARCH": "st-available",
  "STALE": "st-unavailable",
  "WAITING FOR DATA": "st-unavailable",
  "NO LIVE DATA": "st-offline",
  "NO IMPORTED DATA": "st-offline",
  "NO HISTORICAL DATA": "st-offline",
  "NOT CONNECTED": "st-offline",
  "NOT CONFIGURED": "st-offline",
  "ERROR": "st-faulted",
};
async function renderSourceTag() {
  const el = document.getElementById("srcTag");
  if (!el) return;
  // The badge never claims LIVE data on its own: it shows the actual data state of the selected mode.
  try {
    const st = await api("/api/sources/status");
    state.sourceStatus = st.sources;
    const m = st.sources[state.source];
    el.innerHTML = state.source === "simulation" ? tag("SIMULATED")
      : `${m.state === "RECEIVING TEST DATA" ? tag("SIMULATED OCPP") : m.type === "LIVE" ? (m.state === "RECEIVING DATA" ? tag("LIVE OPERATIONAL DATA") : "") : tag(m.type)} <span class="pill ${MODE_PILL[m.state] || "st-offline"}" title="${esc(m.next_action || m.detail || "")}">${esc(m.state)}</span>`;
  } catch {
    el.innerHTML = tag(SOURCE_TAG[state.source] || "NOT YET VALIDATED");
  }
}

async function refreshSimChip() {
  try {
    const s = await api("/api/sim/state");
    state.sim = s;
    const chip = document.getElementById("simChip");
    chip.classList.toggle("live", s.running && s.fleet_size > 0);
    const t = new Date(s.sim_time).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
    // v9.2: never show "Simulating" for a workspace that has no simulated chargers
    document.getElementById("simChipText").textContent = s.fleet_size === 0 ? "Simulator idle · no simulated fleet"
      : `${s.running ? "Simulating" : "Paused"} · ${t} IST · seed ${s.seed}`;
  } catch { /* server unreachable: shown by views */ }
}

let tick = 0;
async function poll() {
  tick++;
  if (!state.me || !state.me.organisation) return;
  if (tick % 15 === 0) { try { await refreshMe(); } catch { /* signed out: api() redirects */ } }
  await refreshSimChip();
  if (tick % 5 === 0) renderSourceTag();
  if (document.hidden || !current || !current.refresh) return;
  // When nothing is changing (simulator paused, simulated view) refresh 5x less often.
  const idle = state.source === "simulation" && state.sim && !state.sim.running;
  if (idle && tick % 5 !== 0) return;
  if (drawerOpen() && !current.refreshWithDrawer) return;
  try { await current.refresh(); } catch { /* views show their own errors */ }
}

// ------------------------------------------------------------------ v9 account chrome
export async function refreshMe() {
  state.me = await api("/api/auth/me");
  window.__csMe = state.me;
  setCsrf(state.me.csrf_token);
  renderAccountChrome();
  return state.me;
}

export const can = (perm) => !!(state.me && state.me.permissions.includes(perm));
export const hasFeature = (f) => !!(state.me && state.me.entitlements && state.me.entitlements.features.includes(f));

function renderAccountChrome() {
  const me = state.me;
  const org = me.organisation;
  const ent = me.entitlements;
  const box = document.getElementById("acct");
  const lim = ent ? ent.limits.chargers : null;
  const used = ent ? ent.usage.chargers : 0;
  box.innerHTML = `
    ${org ? `<span class="org-chip" title="${esc(org.org_type_label)}"><b>${esc(org.name)}</b>${org.is_system_demo ? " " + tag("DEMO") : ""}</span>
      <a class="plan-chip plan-${esc(ent.state)}" href="#billing" title="Subscription">${esc(ent.plan_name)}${!["active", "demo"].includes(ent.state) ? ` · ${esc(ent.state.replace("_", " "))}` : ""}</a>
      <span class="chip small" title="${esc(ent.managed_charger_note)}">${lim === 0 ? "Simulated fleet only" : `Managed chargers ${used}${lim === null || lim === undefined ? "" : `/${lim}`}`}</span>` : '<span class="chip">No organisation</span>'}
    <div class="user-menu">
      <button class="ghost sm" id="userBtn" aria-haspopup="true" aria-expanded="false">${esc(me.user.full_name)} ▾</button>
      <div class="menu hidden" id="userMenu" role="menu">
        <div class="menu-h">${esc(me.user.email)}<br><span class="muted small">${esc(me.role_label || "")}${me.user.email_verified ? "" : " · e-mail unverified"}</span></div>
        <a role="menuitem" href="#profile">Profile</a>
        ${org ? '<a role="menuitem" href="#organisation">Organisation</a>' : ""}
        ${org && can("billing.view") ? '<a role="menuitem" href="#billing">Subscription &amp; billing</a>' : ""}
        ${org && can("org.manage") ? '<a role="menuitem" href="#onboarding">Settings &amp; setup</a>' : ""}
        ${me.user.is_platform_admin ? '<a role="menuitem" href="#admin">Platform administration</a>' : ""}
        ${me.organisations.length > 1 ? `<div class="menu-h">Switch organisation</div>${me.organisations.map(o => `<a role="menuitem" href="#" data-org="${o.id}">${o.workspace_kind === "presentation" ? "Presentation Demo · " : ""}${esc(o.name)}${o.id === (org && org.id) ? " ✓" : ""}</a>`).join("")}` : ""}
        <button role="menuitem" class="linkish" id="signOut">Sign out</button>
      </div>
    </div>`;
  const btn = document.getElementById("userBtn"), menu = document.getElementById("userMenu");
  const close = () => { menu.classList.add("hidden"); btn.setAttribute("aria-expanded", "false"); };
  btn.onclick = (e) => { e.stopPropagation(); const open = menu.classList.toggle("hidden"); btn.setAttribute("aria-expanded", String(!open)); };
  if (!window.__csMenuListener) { window.__csMenuListener = true; document.addEventListener("click", () => {
    const m = document.getElementById("userMenu"); if (m) m.classList.add("hidden"); }); }
  menu.addEventListener("keydown", (e) => { if (e.key === "Escape") { close(); btn.focus(); } });
  menu.querySelectorAll("a[href^='#']").forEach(a => a.addEventListener("click", close));
  menu.querySelectorAll("[data-org]").forEach(a => a.onclick = async (e) => {
    e.preventDefault();
    await api("/api/auth/switch-org", { method: "POST", body: { org_id: Number(a.dataset.org) } });
    location.hash = "#dashboard"; location.reload();
  });
  document.getElementById("signOut").onclick = async () => {
    try { await api("/api/auth/signout", { method: "POST" }); } catch { /* signed out anyway */ }
    location.href = "/signin?signed_out=1";
  };
  // navigation: hide what the role cannot open, mark what the plan does not include
  document.querySelectorAll(".nav a[data-route]").forEach(a => {
    const r = a.dataset.route;
    const perm = ROUTE_PERM[r];
    a.classList.toggle("hidden", !!perm && !can(perm) || (r === "admin" && !me.user.is_platform_admin) || (!org && !["profile", "admin"].includes(r)));
    const f = ROUTE_FEATURE[r];
    a.classList.toggle("locked", !!(org && f && !hasFeature(f)));
    a.title = org && f && !hasFeature(f) ? "Not included in your plan" : "";
  });
  renderWorkspaceSwitch(me);
  const banner = document.getElementById("acctBanner");
  const msgs = [];
  if (org && org.workspace_kind === "presentation")
    msgs.push(`<div class="note pres-banner" role="status">${tag("SIMULATED")} <b>Presentation Demo.</b> Every charger, ticket, technician and chart here is synthetic demonstration data, kept in a separate workspace from your own records. ${homeOrg(me) ? '<button class="sm" data-ws="my-fleet">Back to My Fleet</button>' : ""}</div>`);
  if (ent && ent.access === "read_only") msgs.push(`<div class="note warn" role="status"><b>Read-only workspace.</b> Your subscription is ${esc(ent.state)}. All data stays available and existing tickets can still be progressed; other changes are paused. ${can("billing.manage") ? '<a href="#billing">Reactivate the subscription</a>.' : "Ask the organisation owner to reactivate it."}</div>`);
  else if (ent && ent.state === "past_due") msgs.push(`<div class="note warn" role="status"><b>Payment due.</b> The renewal date has passed; the workspace stays fully available during the grace period. ${can("billing.manage") ? '<a href="#billing">Renew subscription</a>.' : ""}</div>`);
  if (org && me.onboarding && !me.onboarding.completed && can("org.manage") && location.hash !== "#onboarding")
    msgs.push('<div class="note" role="status">Your workspace setup is not finished. <a href="#onboarding">Continue setup</a></div>');
  if (org && me.onboarding && me.onboarding.completed && sessionStorageGet("cs.onbDone") === "1") {
    msgs.push('<div class="note ok" role="status"><b>Setup complete.</b> Welcome to your ChargeSight workspace. You can return to setup any time from the user menu (Settings &amp; setup). <button class="ghost sm" id="dismissOnb">Dismiss</button></div>');
  }
  banner.innerHTML = msgs.join("");
  banner.querySelectorAll("[data-ws]").forEach(b => b.onclick = () => switchWorkspace(b.dataset.ws, b));
  const d = document.getElementById("dismissOnb");
  if (d) d.onclick = () => { sessionStorageSet("cs.onbDone", ""); renderAccountChrome(); };
  // operational data-source controls are meaningless without an organisation
  document.querySelector(".topbar .right").classList.toggle("hidden", !org);
}
// ------------------------------------------------------------------ v9.2 My Fleet / Presentation Demo
const homeOrg = (me) => (me.organisations || []).find(o => o.workspace_kind !== "presentation");
const presOrg = (me) => (me.organisations || []).find(o => o.workspace_kind === "presentation");

function renderWorkspaceSwitch(me) {
  const box = document.getElementById("wsSwitch");
  if (!box) return;
  const org = me.organisation;
  if (!org) { box.innerHTML = ""; return; }
  const inPres = org.workspace_kind === "presentation";
  box.innerHTML = `<div class="ws-label"><span class="ws-k">Workspace</span><b>${inPres ? "Presentation Demo" : "My Fleet"}</b>${inPres ? '<span class="ws-tag">SIMULATED</span>' : ""}</div>
    ${inPres ? (homeOrg(me) ? '<button class="ws-btn" data-ws="my-fleet">Back to My Fleet</button>' : "")
             : '<button class="ws-btn" data-ws="presentation">Open Presentation Demo</button>'}`;
  box.querySelectorAll("[data-ws]").forEach(b => b.onclick = () => switchWorkspace(b.dataset.ws, b));
}

export async function switchWorkspace(which, btn) {
  const first = which === "presentation" && !presOrg(state.me);
  if (btn) { btn.disabled = true; btn.textContent = which === "presentation" ? (first ? "Building the demo… (first time ≈ 20–60 s)" : "Opening…") : "Switching…"; }
  try {
    await api(which === "presentation" ? "/api/auth/presentation-demo" : "/api/auth/my-fleet", { method: "POST" });
    if (which === "presentation") localStorageSet("cs.source", "simulation");   // the demo is SIMULATED data
    location.hash = "#dashboard";
    location.reload();
  } catch (e) {
    toast(e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = which === "presentation" ? "Open Presentation Demo" : "Back to My Fleet"; }
  }
}

function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch { return null; } }
export function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch { /* ignore */ } }

async function boot() {
  try {
    await refreshMe();
  } catch (e) {
    if (e.status === 401) { location.href = "/signin?next=" + encodeURIComponent("/app" + location.hash); return; }
    document.getElementById("view").innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
    return;
  }
  if (!location.hash && state.me.organisation && state.me.onboarding && !state.me.onboarding.completed && can("org.manage")) location.hash = "#onboarding";
  state.refreshChip = refreshSimChip;
  const sel = document.getElementById("sourceSel");
  sel.value = state.source;
  sel.addEventListener("change", () => {
    state.source = sel.value;
    state.dashSel = null;                 // never keep a charger selected from another data source
    localStorageSet("cs.source", sel.value);
    renderSourceTag();
    navigate();
  });
  if (state.me.organisation) renderSourceTag();
  document.getElementById("themeBtn").onclick = () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorageSet("cs.theme", next);
    navigate();
  };
  document.getElementById("menuBtn").onclick = () => document.getElementById("nav").classList.toggle("open");
  try {
    state.config = await api("/api/config");
  } catch (e) {
    toast(e.message, true);
    state.config = {};
  }
  // each navigation re-reads the session (role, plan, subscription state) so banners and menus never go stale
  window.addEventListener("hashchange", async () => { try { await refreshMe(); } catch { /* api() redirects on 401 */ } navigate(); });
  if (state.me.organisation) await refreshSimChip();
  await navigate();
  setInterval(poll, 2000);
}

// v10.1: Back/Forward can restore this page from the browser cache after sign-out; re-check the session then
// (api() sends the visitor to Sign In on 401), so cached history never bypasses authentication.
window.addEventListener("pageshow", (e) => { if (e.persisted) refreshMe().catch(() => { location.href = "/signin?next=" + encodeURIComponent("/app" + location.hash); }); });

boot();
