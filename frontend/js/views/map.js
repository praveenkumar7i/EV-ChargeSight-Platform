// Map: locality-first exploration of real PUBLIC LISTING charging stations in Bengaluru,
// plus optional layers for SIMULATED demo sites, proposed (ESTIMATED) investment sites and
// planning candidates. Engines: Google Maps (browser key) -> Leaflet + OSM tiles -> coordinate plot.
// Public listings never carry live status: every station shows STATUS UNKNOWN.
import { api } from "../api.js";
import { esc, fmt, fmtINR, dt, tag, statusPill, toast, debounce, confirmBox } from "../ui.js";
import { hBars, cat, chartEmpty } from "../charts.js";
import * as planning from "./map_planning.js";

export const SRC_COLOR = { open_charge_map: "#199e70", osm: "#3987e5", google_places: "#d95926", demo: "#9085e9", proposed: "#c98500", candidate: "#e0368a", search: "#5b6477", imported_inventory: "#06b6d4" };
export const SRC_NAME = { open_charge_map: "Open Charge Map", osm: "OpenStreetMap", google_places: "Google Places (live lookup)", demo: "Demo site (SIMULATED)", proposed: "Proposed site (ESTIMATED)", candidate: "Planning candidate (user-selected)", search: "Search result (not a charger)", imported_inventory: "Imported Station Inventory (factual)" };
const BLR = { lat: 12.97, lng: 77.64 };
const BBOX = { s: 12.75, w: 77.45, n: 13.2, e: 77.8 };

let root, ctx, busy = false;
let pub = null, inv = null, locs = null, fleet = null, investSites = [], googleLive = [];
let engine = null;           // see engine contract below
let selected = null;         // { kind, id }
let searchPoint = null;      // { lat, lng, label } shown as a neutral pin (never a charger)
let invalidCoords = 0;
const f = { q: "", area: "", operator: "", showPublic: true, showInventory: true, showGoogle: true, showDemo: false, showProposed: false, showCandidates: true };

export function unmount() { planning.unmount(); if (engine && engine.destroy) engine.destroy(); engine = null; }

// Shared with the planning module.
export const shared = {
  get root() { return root; }, get ctx() { return ctx; }, get engine() { return engine; }, get pub() { return pub; },
  get locs() { return locs; }, f, draw: (...a) => draw(...a), select: (...a) => select(...a), features: () => features(),
  areaByKey: (k) => areaByKey(k), setHashState: () => setHashState(),
};

/* Engine contract:
   render(feats) · fit(feats) · reset() · focus(ft) · focusArea(area|null) · highlight(ft|null)
   setType(t) · tilt() · rotate(deg) · north() · toggle3d() · streetView(lat,lng) · geocode(text)
   onPointer(cb(lat,lng)) · onClick(cb(lat,lng)) · destroy() · attribution · kind · caps{types,tilt,rotate,3d,streetview,geocode} */

export async function render(el, c) {
  root = el; ctx = c;
  const hp = hashParams();
  if (hp.area !== undefined) f.area = hp.area;
  el.innerHTML = `
    <div id="mapNote"></div>
    <section class="panel">
      <div class="panel-h"><h2>Explore a locality ${tag("PUBLIC LISTING")}</h2><span class="hint" id="locHint">counts from stored listings · a place listed by several sources is counted once</span></div>
      <div class="panel-b"><div class="loc-strip" id="locs" role="group" aria-label="Choose a locality"><div class="loading">Loading localities…</div></div></div>
    </section>
    <section class="panel">
      <div class="panel-h"><h2 id="mapTitle">Public EV charging stations — Bengaluru</h2><span class="hint" id="pubCount"></span></div>
      <div class="panel-b" style="padding-bottom:8px">
        <div class="toolbar">
          <form id="searchForm" class="toolbar" role="search" style="gap:6px">
            <input type="search" id="msearch" placeholder="Station, operator, locality, address or “lat, lng”" aria-label="Search the map" style="width:300px">
            <button class="sm" type="submit">Search</button>
          </form>
          <select id="mop" aria-label="Operator"><option value="">All operators</option></select>
          <button class="sm" id="mrefresh" title="Refresh stored listings from OpenStreetMap / Open Charge Map">Refresh listings</button>
          <button class="sm" id="mgoogle" title="Live Google Places lookup (not stored)">Google Places lookup</button>
          <button class="sm primary" id="mplan" aria-pressed="false" title="Pick a point on the map and evaluate it as a new charging site">Plan a new charging site</button>
        </div>
        <div class="toolbar map-tools" id="mapTools" style="margin-top:8px" aria-label="Map view controls"></div>
        <div class="toolbar" style="margin-top:8px">
          <label class="check"><input type="checkbox" data-layer="showPublic"> Stored public listings</label>
          <label class="check"><input type="checkbox" data-layer="showInventory"> Imported station inventory</label>
          <label class="check"><input type="checkbox" data-layer="showGoogle"> Google Places results (live, not stored)</label>
          <label class="check"><input type="checkbox" data-layer="showDemo"> Demo fleet sites ${tag("SIMULATED")}</label>
          <label class="check"><input type="checkbox" data-layer="showProposed"> Proposed sites ${tag("ESTIMATED")}</label>
          <label class="check"><input type="checkbox" data-layer="showCandidates"> Planning candidates</label>
        </div>
        <div id="planBar"></div>
        <div id="refreshStatus" style="margin-top:8px"></div>
      </div>
      <div class="map-wrap" style="padding:0 16px 16px">
        <div><div id="mapBox"><div class="loading">Loading map…</div></div>
          <div class="map-foot"><span class="small mono" id="coord" aria-live="polite">Pointer: —</span><span class="small mono" id="selCoord"></span></div>
          <div class="legend" id="mapLegend" style="margin-top:6px"></div>
          <div class="small muted" id="mapAttrib" style="margin-top:6px"></div></div>
        <aside class="panel"><div class="panel-h"><h2 id="detTitle">Station details</h2></div><div class="panel-b" id="detail"><div class="muted small">Select a marker or a row in the station list.</div></div></aside>
      </div>
    </section>
    <div id="planPanels"></div>
    <section class="panel"><div class="panel-h"><h2 id="sumTitle">Selection summary</h2><span class="hint">markers, list and these figures use the same filters</span></div>
      <div class="panel-b" id="selSummary"></div></section>
    <section class="panel"><div class="panel-h"><h2>Station list</h2><span class="hint">click or press Enter on a row to locate it</span></div>
      <div class="panel-b flush tbl-wrap scroll-y" id="mlist"></div></section>
    <div class="grid-eq" id="inventory"></div>`;
  el.querySelectorAll("[data-layer]").forEach(cb => {
    cb.checked = f[cb.dataset.layer];
    cb.onchange = async () => { f[cb.dataset.layer] = cb.checked; if (cb.checked && cb.dataset.layer === "showDemo" && !fleet) await loadFleet(); draw(); };
  });
  el.querySelector("#searchForm").onsubmit = (e) => { e.preventDefault(); search(el.querySelector("#msearch").value.trim()); };
  el.querySelector("#msearch").oninput = debounce(e => { if (!e.target.value.trim() && f.q) { f.q = ""; draw(); } }, 250);
  el.querySelector("#mop").onchange = e => { f.operator = e.target.value; draw(true); };
  el.querySelector("#mrefresh").onclick = () => refreshListings(false);   // never pass the click event as `force`
  el.querySelector("#mgoogle").onclick = loadGoogle;
  // v9: public listings are shared reference data; refreshing them and paid Google lookups are platform-administrator actions
  if (!(window.__csMe && window.__csMe.user.is_platform_admin)) ["#mrefresh", "#mgoogle"].forEach(id => el.querySelector(id).classList.add("hidden"));
  try { await loadAll(); }
  catch (e) { el.querySelector("#locs").innerHTML = `<div class="error-box">Could not load stored listings: ${esc(e.message)}</div>`; return; }
  fillFilters();
  renderLocalities();
  await initEngine();
  if (!ctx.alive()) return;
  await planning.init(shared, hp);
  draw(false);
  focusCurrentArea();
  if (hp.station) { const ft = features().find(x => x.kind === "public" && String(x.id) === hp.station); if (ft) { select(ft, "hash"); engine.focus(ft); } }
  renderInventory();
  renderRefreshStatus();
}

export async function refresh() {
  // Public listings change only on explicit refresh; only the SIMULATED demo layer is live-updated.
  if (busy || !ctx.alive() || !f.showDemo) return;
  busy = true;
  try { await loadFleet(); if (ctx.alive()) draw(); } catch { /* keep last good view */ } finally { busy = false; }
}

function hashParams() {
  const q = (location.hash.split("?")[1] || "");
  const p = Object.fromEntries(new URLSearchParams(q));
  return p;
}
function setHashState() {
  const p = new URLSearchParams();
  if (f.area) p.set("area", f.area);
  const cand = planning.currentCandidateId();
  if (cand) p.set("candidate", cand);
  else if (selected && selected.kind === "public") p.set("station", selected.id);
  const h = `#map${p.toString() ? "?" + p : ""}`;
  if (location.hash !== h) history.replaceState(null, "", h);
}

async function loadAll() {
  const [p, i, l, s] = await Promise.all([api("/api/public-stations"), api("/api/public-stations/inventory"),
    api("/api/public-stations/localities"), api("/api/investment/sites")]);
  pub = p; inv = i; locs = l; investSites = s.filter(x => validCoord(x.lat, x.lng));
  if (f.showDemo) await loadFleet();
}
async function loadFleet() { fleet = await api("/api/fleet/summary?source=simulation"); }

export function validCoord(lat, lng) {
  return typeof lat === "number" && typeof lng === "number" && Number.isFinite(lat) && Number.isFinite(lng)
    && lat >= BBOX.s && lat <= BBOX.n && lng >= BBOX.w && lng <= BBOX.e;
}

function areaByKey(k) {
  if (!locs) return null;
  if (!k || k === "all") return locs.all;
  if (k === "other") return locs.other;
  return locs.areas.find(a => a.key === k) || null;
}

function fillFilters() {
  const o = root.querySelector("#mop");
  o.innerHTML = '<option value="">All operators</option>' + pub.operators.map(x => `<option>${esc(x)}</option>`).join("");
  o.value = pub.operators.includes(f.operator) ? f.operator : (f.operator = "");
  const g = pub.providers.find(x => x.provider === "google_places");
  const b = root.querySelector("#mgoogle");
  if (!g.configured) { b.title = g.setup; }
}

// ------------------------------------------------------------------ locality selectors
function freshness(a) {
  if (!a.newest_retrieved_at) return "no stored records";
  const src = a.by_origin.api ? (a.by_origin.snapshot ? "API + snapshot" : "fetched from API") : "bundled snapshot only";
  const age = a.data_age_days === null ? "" : a.data_age_days < 1 ? "today" : `${fmt(a.data_age_days, 0)} d old`;
  return `${src} · ${age}${a.stale ? " · STALE" : ""}`;
}
function renderLocalities() {
  const el = root.querySelector("#locs");
  const cards = [locs.all, ...locs.areas];
  el.innerHTML = cards.map(a => {
    const key = a.key === "all" ? "" : a.key;
    const top = Object.entries(a.operators).slice(0, 3).map(([o, n]) => `${esc(o)} ${n}`).join(" · ");
    const zero = !a.stations;
    const zeroLine = zero ? `<span class="loc-zero ${a.coverage_status === "true_zero" ? "true" : "unknown"}">${a.coverage_status === "true_zero" ? "0 listed (true zero for refreshed sources)" : "0 — coverage unknown"}</span>` : "";
    return `<button type="button" class="loc ${f.area === key ? "on" : ""}" data-area="${esc(key)}" aria-pressed="${f.area === key}" title="${esc(a.coverage_message || "")}">
      <span class="loc-name">${esc(a.name)}</span>
      <span class="loc-count num">${a.stations}<small> station${a.stations === 1 ? "" : "s"}</small></span>
      ${zeroLine || `<span class="loc-sub">${a.operator_count} operator${a.operator_count === 1 ? "" : "s"} · ${a.dc_connectors} DC connector${a.dc_connectors === 1 ? "" : "s"}</span>`}
      <span class="loc-ops">${top || "&nbsp;"}</span>
      <span class="loc-fresh ${a.stale ? "stale" : ""}">${esc(freshness(a))}</span>
    </button>`;
  }).join("") + (locs.other.stations ? `<button type="button" class="loc slim ${f.area === "other" ? "on" : ""}" data-area="other" aria-pressed="${f.area === "other"}"><span class="loc-name">Other Bengaluru</span><span class="loc-count num">${locs.other.stations}<small> stations</small></span><span class="loc-sub">outside the six focus radii</span></button>` : "");
  el.querySelectorAll("[data-area]").forEach(b => b.onclick = () => chooseArea(b.dataset.area));
}

export function chooseArea(key) {
  f.area = key;
  root.querySelectorAll("#locs [data-area]").forEach(b => { const on = b.dataset.area === key; b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); });
  if (selected && selected.kind === "public") {
    const ft = features().find(x => x.kind === "public" && String(x.id) === String(selected.id));
    if (!ft) selected = null;
  }
  draw(false);
  focusCurrentArea();
  setHashState();
}

function focusCurrentArea() {
  if (!engine) return;
  const a = areaByKey(f.area);
  if (f.area === "other") { engine.focusArea(null); const fe = features(); if (fe.length) engine.fit(fe); return; }
  engine.focusArea(f.area ? a : null);
}

// ------------------------------------------------------------------ features
function matches(s) {
  if (f.area && s.area !== f.area) return false;
  if (f.operator && (s.operator || "Unknown operator") !== f.operator) return false;
  if (f.q) {
    const hay = `${s.name} ${s.operator || ""} ${s.address || ""} ${s.area_name || ""}`.toLowerCase();
    if (!hay.includes(f.q.toLowerCase())) return false;
  }
  return true;
}

export function visibleStations() {
  if (!pub) return [];
  invalidCoords = 0;
  return pub.stations.filter(s => {
    if (!validCoord(s.lat, s.lng)) { invalidCoords++; return false; }
    if (s.provider === "imported_inventory") {
      if (!f.showInventory) return false;
    } else {
      if (!f.showPublic) return false;
    }
    return matches(s);
  });
}
function visibleGoogle() { return f.showGoogle ? googleLive.filter(s => !s.matches_stored && validCoord(s.lat, s.lng)).filter(matches) : []; }

function features() {
  const out = [], seen = new Set();
  const push = (ft) => { const k = `${ft.kind}:${ft.id}`; if (seen.has(k)) return; seen.add(k); out.push(ft); };
  visibleStations().forEach(s => push({ kind: "public", id: s.id, lat: s.lat, lng: s.lng, title: s.name, color: SRC_COLOR[s.provider] || SRC_COLOR.open_charge_map, shape: "square", data: s }));
  visibleGoogle().forEach(s => push({ kind: "google", id: s.provider_id, lat: s.lat, lng: s.lng, title: s.name, color: SRC_COLOR.google_places, shape: "square", data: s }));
  if (f.showDemo && fleet) fleet.sites.filter(s => s.has_position).forEach(s => {
    const worst = s.status_counts.faulted ? "faulted" : s.status_counts.offline ? "offline" : "available";
    push({ kind: "demo", id: s.code, lat: s.lat, lng: s.lng, title: `${s.name} — SIMULATED demo site`, color: SRC_COLOR.demo, ring: worst, shape: "circle", data: s });
  });
  if (f.showProposed) investSites.forEach(s => push({ kind: "proposed", id: s.id, lat: s.lat, lng: s.lng, title: `${s.name} — proposed (ESTIMATED)`, color: SRC_COLOR.proposed, shape: "diamond", data: s }));
  planning.features(f.showCandidates).forEach(push);
  if (searchPoint) push({ kind: "search", id: "search", lat: searchPoint.lat, lng: searchPoint.lng, title: `Search result: ${searchPoint.label}`, color: SRC_COLOR.search, shape: "pin", data: searchPoint });
  return out;
}

function draw(refit = false) {
  if (!root || !pub) return;
  const feats = features();
  const nPub = visibleStations().length, nG = visibleGoogle().length;
  const a = areaByKey(f.area);
  root.querySelector("#mapTitle").textContent = `Public EV charging stations — ${a ? a.name : "Bengaluru"}`;
  root.querySelector("#pubCount").textContent = `${nPub} of ${pub.total} stored listings shown${googleLive.length ? ` · ${nG} extra Google Places results` : ""}${invalidCoords ? ` · ${invalidCoords} hidden (coordinates outside Bengaluru or invalid)` : ""}`;
  if (engine) {
    engine.render(feats);
    if (refit && feats.length) engine.fit(feats);
    if (selected) { const ft = feats.find(x => x.kind === selected.kind && String(x.id) === String(selected.id)); engine.highlight(ft || null); }
  }
  renderList();
  renderSummary();
  renderLegend();
  if (selected) renderDetail();
}

// ------------------------------------------------------------------ search
const COORD_RE = /^\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$/;
async function search(text) {
  if (!text) { f.q = ""; searchPoint = null; draw(); return; }
  const m = text.match(COORD_RE);
  if (m) {
    const lat = Number(m[1]), lng = Number(m[2]);
    if (!validCoord(lat, lng)) { toast(`${lat}, ${lng} is outside the Bengaluru study area (lat ${BBOX.s}–${BBOX.n}, lng ${BBOX.w}–${BBOX.e}).`, true); return; }
    showPoint(lat, lng, `${lat.toFixed(5)}, ${lng.toFixed(5)}`);
    planning.onSearchPoint(lat, lng);
    return;
  }
  const low = text.toLowerCase();
  const loc = [locs.all, ...locs.areas, locs.other].find(a => a.name.toLowerCase() === low || a.name.toLowerCase().startsWith(low));
  if (loc && low.length >= 3) { root.querySelector("#msearch").value = ""; f.q = ""; chooseArea(loc.key === "all" ? "" : loc.key); return; }
  f.q = text;
  const hits = visibleStations();
  if (hits.length) { searchPoint = null; draw(true); toast(`${hits.length} stored listing${hits.length === 1 ? "" : "s"} match “${text}”.`); return; }
  f.q = "";
  if (engine && engine.caps.geocode) {
    try {
      const r = await engine.geocode(text);
      if (!validCoord(r.lat, r.lng)) { toast(`Google found “${r.label}”, but it is outside the Bengaluru study area.`, true); draw(); return; }
      showPoint(r.lat, r.lng, r.label);
      planning.onSearchPoint(r.lat, r.lng);
      toast(`Address found by Google Geocoding: ${r.label}. Shown as a grey pin (not a charger).`);
    } catch (e) { toast(e.message, true); draw(); }
  } else {
    toast(`No stored listing matches “${text}”. Address search needs the Google basemap; you can type coordinates as “12.97, 77.64”.`, true);
    draw();
  }
}
function showPoint(lat, lng, label) {
  searchPoint = { lat, lng, label };
  draw();
  engine && engine.focus({ lat, lng });
  root.querySelector("#selCoord").textContent = `Search point: ${lat.toFixed(6)}, ${lng.toFixed(6)}`;
}

// ------------------------------------------------------------------ engines
function noteHtml(msg, cls = "") { return `<div class="note ${cls}">${msg}</div>`; }

async function initEngine() {
  const key = ctx.state.config?.google_maps_browser_key;
  const note = root.querySelector("#mapNote");
  if (key) {
    try {
      engine = await googleEngine(key, ctx.state.config.google_maps_map_id);
      note.innerHTML = noteHtml(`Basemap © Google. Markers are ${tag("PUBLIC LISTING")} records from the sources named in each station's details; Google supplies the basemap (and, only when you press “Google Places lookup”, live place results). ${esc(pub.status_note)}`);
      report("connected");
    } catch (e) {
      report("error", e.message);
      note.innerHTML = noteHtml(`Google Maps could not be used: ${esc(e.message)} Falling back to OpenStreetMap tiles.`, "warn");
    }
  } else {
    note.innerHTML = noteHtml(`Google Maps is not configured (set GOOGLE_MAPS_BROWSER_KEY in .env and restart). Showing the same real public listings on an OpenStreetMap basemap. ${esc(pub.status_note)}`);
  }
  if (!engine) {
    try { engine = await leafletEngine(); }
    catch (e) {
      note.insertAdjacentHTML("beforeend", noteHtml(`The OpenStreetMap basemap could not load (${esc(e.message)}). Showing a coordinate plot; all station details, filters and planning still work.`, "warn"));
      engine = schematicEngine();
    }
  }
  engine.onPointer((lat, lng) => { root.querySelector("#coord").textContent = `Pointer: ${lat.toFixed(6)}, ${lng.toFixed(6)}`; });
  engine.onClick((lat, lng) => planning.onMapClick(lat, lng));
  renderTools();
}

function renderTools() {
  const t = root.querySelector("#mapTools"), c = engine.caps;
  const b = (id, label, title, on = true) => `<button type="button" class="sm" id="${id}" title="${esc(title)}" ${on ? "" : "disabled"}>${label}</button>`;
  t.innerHTML = `
    ${c.types.length ? `<span class="seg" role="group" aria-label="Map type">${c.types.map(x => `<button type="button" class="sm" data-type="${x}" aria-pressed="${x === "roadmap"}">${x[0].toUpperCase() + x.slice(1)}</button>`).join("")}</span>` : ""}
    ${b("t3d", "3D view", c["3d"] ? "Photorealistic 3D (Google Maps 3D, needs WebGL/hardware acceleration; coverage depends on Google imagery)" : "3D needs the Google basemap", c["3d"])}
    ${b("ttilt", "Tilt", c.tilt ? "Tilt the camera" : "Tilt needs a vector map (GOOGLE_MAPS_MAP_ID) or the 3D view", c.tilt)}
    ${b("trotl", "⟲", "Rotate left 45°", c.rotate)}${b("trotr", "⟳", "Rotate right 45°", c.rotate)}
    ${b("tnorth", "North up", "Reset heading to north and remove tilt", c.rotate || c.tilt || c["3d"])}
    ${b("treset", "Reset camera", "Bengaluru overview, roadmap, north up, no tilt")}
    ${b("tfitarea", "Fit locality", "Zoom to the selected locality (or all Bengaluru)")}
    ${b("tfit", "Fit visible", "Zoom to the markers currently shown")}
    ${b("tsv", "Street View", c.streetview ? "Open Google Street View at the selected item, if imagery exists within 50 m" : "Street View needs the Google basemap", c.streetview)}
    <span class="small muted" id="toolNote">${esc(engine.toolNote || "")}</span>`;
  t.querySelectorAll("[data-type]").forEach(x => x.onclick = () => {
    engine.setType(x.dataset.type);
    t.querySelectorAll("[data-type]").forEach(y => y.setAttribute("aria-pressed", String(y === x)));
  });
  const on = (id, fn) => { const n = t.querySelector("#" + id); if (n) n.onclick = fn; };
  on("t3d", async () => { const r = await engine.toggle3d(); if (r && r.message) toast(r.message, !!r.error); const n = t.querySelector("#t3d"); if (n) n.textContent = r && r.on ? "2D map" : "3D view"; });
  on("ttilt", () => engine.tilt());
  on("trotl", () => engine.rotate(-45));
  on("trotr", () => engine.rotate(45));
  on("tnorth", () => engine.north());
  on("treset", () => { engine.reset(); t.querySelectorAll("[data-type]").forEach(y => y.setAttribute("aria-pressed", String(y.dataset.type === "roadmap"))); });
  on("tfitarea", () => focusCurrentArea());
  on("tfit", () => { const fe = features(); if (fe.length) engine.fit(fe); else toast("No markers are shown with the current filters.", true); });
  on("tsv", async () => {
    const ft = selected && features().find(x => x.kind === selected.kind && String(x.id) === String(selected.id));
    const p = ft || (searchPoint && { lat: searchPoint.lat, lng: searchPoint.lng }) || planning.currentPoint();
    if (!p) { toast("Select a station, candidate or search result first.", true); return; }
    try { await engine.streetView(p.lat, p.lng); } catch (e) { toast(e.message, true); }
  });
}

async function report(state, error = "") {
  try { await api("/api/integrations/maps/status", { method: "POST", body: { state, error } }); } catch { /* ignore */ }
}

function loadScript(src, timeoutMs = 12000) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    const t = setTimeout(() => reject(new Error("script load timed out")), timeoutMs);
    s.src = src; s.async = true;
    s.onload = () => { clearTimeout(t); resolve(); };
    s.onerror = () => { clearTimeout(t); reject(new Error("script could not be downloaded (offline or blocked)")); };
    document.head.appendChild(s);
  });
}

function loadGoogleApi(key) {
  return new Promise((resolve, reject) => {
    if (window.google && window.google.maps && window.google.maps.Map) return resolve();
    const timer = setTimeout(() => reject(new Error("Google Maps did not load within 12 seconds.")), 12000);
    window.__csMapReady = () => { clearTimeout(timer); resolve(); };
    window.gm_authFailure = () => { clearTimeout(timer); reject(new Error("Google rejected the browser key (check the key, billing, the Maps JavaScript API and HTTP-referrer restrictions).")); };
    // Weekly channel: 3D Maps (maps3d library) is available there since Oct 2025 (release 3.62.11).
    const s = document.createElement("script");
    s.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&callback=__csMapReady&v=weekly&loading=async&libraries=geometry`;
    s.async = true;
    s.onerror = () => { clearTimeout(timer); reject(new Error("The Google Maps script could not be downloaded (network or blocked).")); };
    document.head.appendChild(s);
  });
}

export function svgIcon(ft, size = 24) {
  const c = ft.color, ring = ft.ring ? STATUS_HEX[ft.ring] : "#ffffff";
  const shape = ft.shape === "circle" ? `<circle cx="12" cy="12" r="8" fill="${c}" stroke="${ring}" stroke-width="3"/>`
    : ft.shape === "diamond" ? `<path d="M12 2 L22 12 L12 22 L2 12 Z" fill="${c}" stroke="#fff" stroke-width="2"/>`
      : ft.shape === "star" ? `<path d="M12 1.8l3 6.6 7.2.8-5.4 4.9 1.5 7.1L12 17.6l-6.3 3.6 1.5-7.1L1.8 9.2 9 8.4z" fill="${c}" stroke="#fff" stroke-width="1.6"/>`
        : ft.shape === "pin" ? `<path d="M12 22s-7-6.4-7-12a7 7 0 1 1 14 0c0 5.6-7 12-7 12z" fill="${c}" stroke="#fff" stroke-width="2"/><circle cx="12" cy="10" r="2.6" fill="#fff"/>`
          : `<rect x="4" y="4" width="16" height="16" rx="3" fill="${c}" stroke="#fff" stroke-width="2"/><path d="M13 6 8 13h4l-1 5 5-7h-4l1-5Z" fill="#fff"/>`;
  return "data:image/svg+xml;charset=UTF-8," + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24">${shape}</svg>`);
}
const STATUS_HEX = { available: "#4ade80", faulted: "#ff6b60", offline: "#a1a8b5" };
const SEL_ICON = (ft) => svgIcon({ ...ft, color: ft.color }, 34);
function zoomForRadius(km) { return km ? (km >= 5 ? 13 : km >= 4 ? 13 : 14) : 11; }

async function googleEngine(key, mapId) {
  await loadGoogleApi(key);
  if (!ctx.alive()) throw new Error("view changed");
  const box = root.querySelector("#mapBox");
  box.innerHTML = '<div id="gmap" role="application" aria-label="Google map of charging stations"></div><div id="gmap3d" class="hidden map3d-box"></div>';
  const vector = !!mapId;
  const map = new google.maps.Map(box.querySelector("#gmap"), {
    center: BLR, zoom: 11, mapTypeId: "roadmap", mapTypeControl: false,
    zoomControl: true, fullscreenControl: true, streetViewControl: false, rotateControl: vector, tilt: 0, heading: 0,
    clickableIcons: false,
    ...(vector ? { mapId, renderingType: google.maps.RenderingType ? google.maps.RenderingType.VECTOR : undefined, tiltInteractionEnabled: true, headingInteractionEnabled: true } : {}),
  });
  const iw = new google.maps.InfoWindow();
  let markers = new Map(), circle = null, map3d = null, lib3d = null, markers3d = [], showing3d = false, lastFeats = [], hl = null;
  let clickCb = () => {}, ptrCb = () => {};
  map.addListener("mousemove", e => ptrCb(e.latLng.lat(), e.latLng.lng()));
  map.addListener("click", e => clickCb(e.latLng.lat(), e.latLng.lng()));
  const sv = map.getStreetView();
  const keyOf = (ft) => `${ft.kind}:${ft.id}`;
  function infoHtml(ft) {
    const src = ft.kind === "public" ? ft.data.provider : ft.kind === "google" ? "google_places" : ft.kind;
    return `<div style="font:13px system-ui;color:#141a26;max-width:240px"><b>${esc(ft.title)}</b><br><span style="color:#5b6477">${esc(SRC_NAME[src] || "")}</span></div>`;
  }
  function draw3d() {
    if (!map3d || !lib3d) return;
    markers3d.forEach(m => m.remove()); markers3d = [];
    const Mk = lib3d.Marker3DInteractiveElement || lib3d.Marker3DElement;
    lastFeats.slice(0, 400).forEach(ft => {
      const m = new Mk({ position: { lat: ft.lat, lng: ft.lng, altitude: 0 }, label: ft.title.slice(0, 40), altitudeMode: "CLAMP_TO_GROUND" });
      if (lib3d.Marker3DInteractiveElement) m.addEventListener("gmp-click", () => select(ft, "map"));
      map3d.append(m); markers3d.push(m);
    });
  }
  const eng = {
    kind: "google", attribution: "Map data © Google",
    caps: { types: ["roadmap", "satellite", "hybrid", "terrain"], tilt: true, rotate: true, "3d": true, streetview: true, geocode: true },
    toolNote: vector ? "Vector map (Map ID set): tilt and rotate the 2D map, or open the 3D view." : "Tilt/rotate the 2D map need GOOGLE_MAPS_MAP_ID (vector map); the 3D view tilts and rotates without it.",
    render(feats) {
      lastFeats = feats;
      const want = new Set(feats.map(keyOf));
      for (const [k, m] of markers) if (!want.has(k)) { m.setMap(null); markers.delete(k); }
      feats.forEach(ft => {
        const k = keyOf(ft);
        let m = markers.get(k);
        const icon = { url: svgIcon(ft), scaledSize: new google.maps.Size(24, 24), anchor: new google.maps.Point(12, 12) };
        if (!m) {
          m = new google.maps.Marker({ position: { lat: ft.lat, lng: ft.lng }, map, title: ft.title, icon, zIndex: ft.kind === "candidate" ? 5 : ft.kind === "public" ? 2 : 1 });
          m.addListener("click", () => select(ft, "map"));
          markers.set(k, m);
        } else { m.setPosition({ lat: ft.lat, lng: ft.lng }); m.setIcon(icon); m.setTitle(ft.title); }
        m.__ft = ft;
      });
      if (showing3d) draw3d();
    },
    fit(feats) {
      if (!feats.length) return;
      if (feats.length === 1) { map.setCenter({ lat: feats[0].lat, lng: feats[0].lng }); map.setZoom(15); return; }
      const b = new google.maps.LatLngBounds(); feats.forEach(ft => b.extend({ lat: ft.lat, lng: ft.lng })); map.fitBounds(b, 48);
    },
    focusArea(a) {
      if (circle) { circle.setMap(null); circle = null; }
      if (!a) { map.fitBounds({ south: BBOX.s, west: BBOX.w, north: BBOX.n, east: BBOX.e }, 16); return; }
      circle = new google.maps.Circle({ map, center: { lat: a.lat, lng: a.lng }, radius: a.radius_km * 1000, strokeColor: "#2f5bea", strokeOpacity: .8, strokeWeight: 1.5, fillColor: "#2f5bea", fillOpacity: .05, clickable: false });
      map.setCenter({ lat: a.lat, lng: a.lng }); map.setZoom(zoomForRadius(a.radius_km));
      if (showing3d && map3d) { map3d.center = { lat: a.lat, lng: a.lng, altitude: 0 }; map3d.range = a.radius_km * 2600; }
    },
    reset() { map.setCenter(BLR); map.setZoom(11); map.setTilt(0); map.setHeading(0); map.setMapTypeId("roadmap"); if (map3d) { map3d.tilt = 0; map3d.heading = 0; } if (showing3d) eng.toggle3d(); },
    focus(ft) { map.panTo({ lat: ft.lat, lng: ft.lng }); if (map.getZoom() < 15) map.setZoom(15); if (showing3d && map3d) { map3d.center = { lat: ft.lat, lng: ft.lng, altitude: 0 }; map3d.range = 700; } },
    highlight(ft) {
      if (hl) { const m = markers.get(hl); if (m && m.__ft) m.setIcon({ url: svgIcon(m.__ft), scaledSize: new google.maps.Size(24, 24), anchor: new google.maps.Point(12, 12) }); hl = null; }
      if (!ft) { iw.close(); return; }
      const m = markers.get(keyOf(ft));
      if (!m) return;
      m.setIcon({ url: SEL_ICON(ft), scaledSize: new google.maps.Size(34, 34), anchor: new google.maps.Point(17, 17) });
      m.setZIndex(10); hl = keyOf(ft);
      iw.setContent(infoHtml(ft)); iw.open({ map, anchor: m, shouldFocus: false });
    },
    setType(t) { if (showing3d) eng.toggle3d(); map.setMapTypeId(t); },
    tilt() {
      if (showing3d && map3d) { map3d.tilt = map3d.tilt > 30 ? 0 : 65; return; }
      if (!vector) { toast("Tilting the 2D map needs a vector map: set GOOGLE_MAPS_MAP_ID in .env (a Map ID with vector rendering). Use “3D view” instead.", true); return; }
      map.setTilt(map.getTilt() ? 0 : 47.5); if (map.getZoom() < 16) map.setZoom(16);
    },
    rotate(d) {
      if (showing3d && map3d) { map3d.heading = (((map3d.heading || 0) + d) % 360 + 360) % 360; return; }
      if (!vector) { toast("Rotating the 2D map needs a vector map (GOOGLE_MAPS_MAP_ID). Use “3D view” instead.", true); return; }
      map.setHeading((((map.getHeading() || 0) + d) % 360 + 360) % 360);
    },
    north() { if (map3d) { map3d.heading = 0; map3d.tilt = showing3d ? 45 : 0; } map.setHeading(0); map.setTilt(0); },
    async toggle3d() {
      const box3 = box.querySelector("#gmap3d"), box2 = box.querySelector("#gmap");
      if (showing3d) { showing3d = false; box3.classList.add("hidden"); box2.classList.remove("hidden"); return { on: false }; }
      try { lib3d = lib3d || await google.maps.importLibrary("maps3d"); }
      catch (e) { return { error: true, message: `Google 3D Maps could not load (${e.message}). The 2D map stays active.` }; }
      if (!lib3d || !lib3d.Map3DElement) return { error: true, message: "This Google Maps version did not provide the 3D library. The 2D map stays active." };
      const c = map.getCenter();
      if (!map3d) {
        try {
          map3d = new lib3d.Map3DElement({ center: { lat: c.lat(), lng: c.lng(), altitude: 0 }, range: 4000, tilt: 60, heading: 0,
            mode: lib3d.MapMode ? lib3d.MapMode.HYBRID : "HYBRID" });
        } catch (e) { return { error: true, message: `3D view could not start (${e.message}). Your browser may need WebGL/hardware acceleration enabled.` }; }
        map3d.addEventListener("gmp-error", () => toast("Google 3D Maps reported an error (WebGL unsupported or blocked). Use the 2D map.", true));
        box3.appendChild(map3d);
      } else { map3d.center = { lat: c.lat(), lng: c.lng(), altitude: 0 }; }
      showing3d = true; box2.classList.add("hidden"); box3.classList.remove("hidden");
      draw3d();
      return { on: true, message: "Photorealistic 3D: drag to pan, right-drag or Ctrl+drag to tilt/rotate. Where Google has no 3D imagery the view stays flat satellite." };
    },
    streetView(lat, lng) {
      return new Promise((resolve, reject) => {
        new google.maps.StreetViewService().getPanorama({ location: { lat, lng }, radius: 50, source: google.maps.StreetViewSource ? google.maps.StreetViewSource.OUTDOOR : undefined }, (data, status) => {
          if (status !== "OK" || !data) { reject(new Error("Google has no Street View imagery within 50 m of this point.")); return; }
          if (showing3d) eng.toggle3d();
          sv.setPosition(data.location.latLng); sv.setPov({ heading: 0, pitch: 0 }); sv.setVisible(true); resolve();
        });
      });
    },
    geocode(text) {
      return new Promise((resolve, reject) => {
        new google.maps.Geocoder().geocode({ address: text, bounds: { south: BBOX.s, west: BBOX.w, north: BBOX.n, east: BBOX.e }, componentRestrictions: { country: "IN" } }, (res, status) => {
          if (status === "OK" && res && res[0]) { const l = res[0].geometry.location; resolve({ lat: l.lat(), lng: l.lng(), label: res[0].formatted_address }); return; }
          const why = status === "REQUEST_DENIED" ? "the Geocoding API is not enabled for this browser key (enable it in Google Cloud, or type coordinates)"
            : status === "ZERO_RESULTS" ? "no address matched" : status === "OVER_QUERY_LIMIT" ? "the Geocoding quota was exceeded" : `status ${status}`;
          reject(new Error(`Address search failed: ${why}.`));
        });
      });
    },
    onPointer(cb) { ptrCb = cb; }, onClick(cb) { clickCb = cb; },
    destroy() { for (const m of markers.values()) m.setMap(null); markers3d.forEach(m => m.remove()); if (circle) circle.setMap(null); },
  };
  return eng;
}

async function leafletEngine() {
  if (!window.L) {
    const css = document.createElement("link");
    css.rel = "stylesheet"; css.href = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    document.head.appendChild(css);
    await loadScript("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js");
  }
  if (!ctx.alive()) throw new Error("view changed");
  root.querySelector("#mapBox").innerHTML = '<div id="lmap" class="map-canvas" role="application" aria-label="OpenStreetMap of charging stations"></div>';
  const map = L.map(root.querySelector("#lmap"), { zoomControl: true }).setView([BLR.lat, BLR.lng], 11);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(map);
  const layer = L.layerGroup().addTo(map);
  const markers = new Map();
  let circle = null, hl = null, clickCb = () => {}, ptrCb = () => {};
  map.on("mousemove", e => ptrCb(e.latlng.lat, e.latlng.lng));
  map.on("click", e => clickCb(e.latlng.lat, e.latlng.lng));
  const icon = (ft, big) => L.icon({ iconUrl: big ? SEL_ICON(ft) : svgIcon(ft), iconSize: big ? [34, 34] : [24, 24], iconAnchor: big ? [17, 17] : [12, 12] });
  const eng = {
    kind: "leaflet", attribution: "Basemap © OpenStreetMap contributors (tile.openstreetmap.org, light use only)",
    caps: { types: [], tilt: false, rotate: false, "3d": false, streetview: false, geocode: false },
    toolNote: "Satellite, 3D, tilt, rotation, Street View and address search need the Google basemap (GOOGLE_MAPS_BROWSER_KEY).",
    render(feats) {
      layer.clearLayers(); markers.clear();
      feats.forEach(ft => {
        const m = L.marker([ft.lat, ft.lng], { title: ft.title, icon: icon(ft, false), keyboard: true, zIndexOffset: ft.kind === "candidate" ? 500 : 0 });
        m.on("click", () => select(ft, "map"));
        m.bindTooltip(esc(ft.title));
        m.__ft = ft; layer.addLayer(m); markers.set(`${ft.kind}:${ft.id}`, m);
      });
      hl = null;
    },
    fit(feats) { if (feats.length) map.fitBounds(L.latLngBounds(feats.map(ft => [ft.lat, ft.lng])), { padding: [40, 40], maxZoom: 15 }); },
    focusArea(a) {
      if (circle) { map.removeLayer(circle); circle = null; }
      if (!a) { map.fitBounds([[BBOX.s, BBOX.w], [BBOX.n, BBOX.e]]); return; }
      circle = L.circle([a.lat, a.lng], { radius: a.radius_km * 1000, color: "#2f5bea", weight: 1.5, fillOpacity: .05, interactive: false }).addTo(map);
      map.setView([a.lat, a.lng], zoomForRadius(a.radius_km));
    },
    reset() { map.setView([BLR.lat, BLR.lng], 11); },
    focus(ft) { map.setView([ft.lat, ft.lng], Math.max(map.getZoom(), 15)); },
    highlight(ft) {
      if (hl) { const m = markers.get(hl); if (m) { m.setIcon(icon(m.__ft, false)); m.setZIndexOffset(0); m.closeTooltip(); } hl = null; }
      if (!ft) return;
      const m = markers.get(`${ft.kind}:${ft.id}`); if (!m) return;
      m.setIcon(icon(ft, true)); m.setZIndexOffset(1000); m.openTooltip(); hl = `${ft.kind}:${ft.id}`;
    },
    setType() {}, tilt() {}, rotate() {}, north() {}, async toggle3d() { return { error: true, message: "3D needs the Google basemap." }; },
    async streetView() { throw new Error("Street View needs the Google basemap."); }, async geocode() { throw new Error("Address search needs the Google basemap."); },
    onPointer(cb) { ptrCb = cb; }, onClick(cb) { clickCb = cb; },
    destroy() { map.remove(); },
  };
  return eng;
}

function schematicEngine() {
  let view = null, hl = null, clickCb = () => {}, ptrCb = () => {}, area = null, proj = null;
  const eng = {
    kind: "schematic", attribution: "Coordinate plot (no basemap)",
    caps: { types: [], tilt: false, rotate: false, "3d": false, streetview: false, geocode: false },
    toolNote: "No basemap could load. Coordinates, filters, details and planning still work.",
    render(feats) {
      const box = root.querySelector("#mapBox");
      const pts = view || (area ? [{ lat: area.lat - area.radius_km / 111, lng: area.lng - area.radius_km / 109 }, { lat: area.lat + area.radius_km / 111, lng: area.lng + area.radius_km / 109 }] : null) || (feats.length ? feats : [{ lat: BBOX.s, lng: BBOX.w }, { lat: BBOX.n, lng: BBOX.e }]);
      let minLat = Math.min(...pts.map(p => p.lat)), maxLat = Math.max(...pts.map(p => p.lat));
      let minLng = Math.min(...pts.map(p => p.lng)), maxLng = Math.max(...pts.map(p => p.lng));
      const pl = Math.max(0.005, (maxLat - minLat) * 0.1), pg = Math.max(0.005, (maxLng - minLng) * 0.1);
      minLat -= pl; maxLat += pl; minLng -= pg; maxLng += pg;
      const W = 800, H = 560, X = (v) => 40 + (v - minLng) / (maxLng - minLng) * (W - 60), Y = (v) => 20 + (maxLat - v) / (maxLat - minLat) * (H - 50);
      proj = { W, H, minLat, maxLat, minLng, maxLng };
      let g = "";
      for (let i = 0; i <= 4; i++) {
        const la = minLat + i * (maxLat - minLat) / 4, lo = minLng + i * (maxLng - minLng) / 4;
        g += `<line x1="40" x2="${W - 20}" y1="${Y(la)}" y2="${Y(la)}" stroke="var(--line)"/><text x="36" y="${Y(la) + 4}" font-size="10.5" text-anchor="end" fill="var(--muted)">${la.toFixed(3)}°</text>`;
        g += `<line y1="20" y2="${H - 30}" x1="${X(lo)}" x2="${X(lo)}" stroke="var(--line)"/><text y="${H - 14}" x="${X(lo)}" font-size="10.5" text-anchor="middle" fill="var(--muted)">${lo.toFixed(3)}°</text>`;
      }
      if (area) { const rx = (X(area.lng + area.radius_km / 109) - X(area.lng)), ry = (Y(area.lat) - Y(area.lat + area.radius_km / 111)); g += `<ellipse cx="${X(area.lng)}" cy="${Y(area.lat)}" rx="${rx}" ry="${ry}" fill="rgba(47,91,234,.05)" stroke="#2f5bea" stroke-width="1.2"/>`; }
      feats.forEach((ft, i) => { const big = hl === `${ft.kind}:${ft.id}`, s = big ? 28 : 18; g += `<image href="${big ? SEL_ICON(ft) : svgIcon(ft)}" x="${X(ft.lng) - s / 2}" y="${Y(ft.lat) - s / 2}" width="${s}" height="${s}" data-i="${i}" style="cursor:pointer"><title>${esc(ft.title)}</title></image>`; });
      box.innerHTML = `<div class="schematic"><svg id="schemSvg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Coordinate plot of stations">${g}<text x="${W - 20}" y="34" text-anchor="end" font-size="11" fill="var(--muted)">Coordinate plot · no basemap</text></svg></div>`;
      const svg = box.querySelector("#schemSvg");
      const toLL = (e) => { const r = svg.getBoundingClientRect(); const x = (e.clientX - r.left) / r.width * W, y = (e.clientY - r.top) / r.height * H; return [maxLat - (y - 20) / (H - 50) * (maxLat - minLat), minLng + (x - 40) / (W - 60) * (maxLng - minLng)]; };
      svg.addEventListener("mousemove", e => { const [a, b] = toLL(e); ptrCb(a, b); });
      svg.addEventListener("click", e => { if (e.target.dataset && e.target.dataset.i !== undefined) return; const [a, b] = toLL(e); clickCb(a, b); });
      box.querySelectorAll("[data-i]").forEach(n => n.addEventListener("click", () => select(feats[Number(n.dataset.i)], "map")));
      eng._last = feats;
    },
    fit(feats) { view = feats.length ? feats : null; area = null; eng.render(features()); },
    focusArea(a) { area = a; view = null; eng.render(features()); },
    reset() { view = null; area = null; eng.render(features()); },
    focus(ft) { view = [{ lat: ft.lat - 0.01, lng: ft.lng - 0.01 }, { lat: ft.lat + 0.01, lng: ft.lng + 0.01 }]; eng.render(features()); },
    highlight(ft) { const k = ft ? `${ft.kind}:${ft.id}` : null; if (k !== hl) { hl = k; if (eng._last) eng.render(eng._last); } },
    setType() {}, tilt() {}, rotate() {}, north() {}, async toggle3d() { return { error: true, message: "3D needs the Google basemap." }; },
    async streetView() { throw new Error("Street View needs the Google basemap."); }, async geocode() { throw new Error("Address search needs the Google basemap."); },
    onPointer(cb) { ptrCb = cb; }, onClick(cb) { clickCb = cb; }, destroy() {},
  };
  return eng;
}

// ------------------------------------------------------------------ actions
let refreshing = false;
async function refreshListings(force = false) {
  if (refreshing) return;
  refreshing = true;
  const btn = root.querySelector("#mrefresh");
  btn.disabled = true; btn.textContent = "Refreshing…";
  let cached = false;
  try {
    const r = await api("/api/public-stations/refresh", { method: "POST", body: { providers: ["osm", "open_charge_map"], force } });
    const parts = Object.entries(r.results).map(([p, x]) => {
      if (x.cached) { cached = true; return `${SRC_NAME[p]}: ${x.message}`; }
      return x.ok ? `${SRC_NAME[p]}: ${x.received} received (${x.inserted} new, ${x.updated} updated, ${x.duplicates_linked} merged, ${x.rejected} rejected)${x.message ? " — " + x.message : ""}`
        : `${SRC_NAME[p]}: ${x.message}`;
    });
    toast(parts.join(" · "), !Object.values(r.results).some(x => x.ok));
  } catch (e) { toast(e.message, true); }
  finally { refreshing = false; if (ctx.alive()) { btn.disabled = false; btn.textContent = "Refresh listings"; } }
  if (!ctx.alive()) return;
  await loadAll();
  if (ctx.alive()) { fillFilters(); renderLocalities(); draw(); renderInventory(); renderRefreshStatus(); }
  if (cached && !force) {
    const ok = await confirmBox({ title: "Refresh again now?", body: "A provider was refreshed less than 10 minutes ago, so ChargeSight kept the stored records to respect the provider's rate limits. Refresh again anyway?", confirmLabel: "Refresh now" });
    if (ok) refreshListings(true);
  }
}

function renderRefreshStatus() {
  const el = root.querySelector("#refreshStatus");
  if (!el || !pub) return;
  const counts = {};
  pub.stations.forEach(s => { const k = `${s.provider_label} · ${s.origin === "snapshot" ? "bundled snapshot" : "fetched from API"}`; counts[k] = (counts[k] || 0) + 1; });
  const prov = pub.providers.filter(p => p.provider !== "google_places").map(p => {
    const cls = p.verified ? "st-available" : p.health === "error" ? "st-faulted" : "st-offline";
    return `<span class="pill ${cls}" title="${esc(p.last_error || p.setup || "")}">${esc(p.label)}: ${esc(p.state_label)}</span>${p.last_success_at ? ` <span class="small muted">last OK ${dt(p.last_success_at)}</span>` : ""}${p.health === "error" && p.last_error ? ` <span class="small" style="color:var(--crit)">${esc(p.last_error)}</span>` : ""}`;
  }).join(" &nbsp; ");
  const g = pub.providers.find(p => p.provider === "google_places");
  el.innerHTML = `<div class="small" style="display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center">${prov}
    <span class="pill ${g.verified ? "st-available" : "st-offline"}" title="${esc(g.setup || "")}">Google Places: ${esc(g.state_label)}</span></div>
    <div class="small muted" style="margin-top:4px">Records stored: ${Object.entries(counts).map(([k, n]) => `${esc(k)} ${n}`).join(" · ") || "none"}. The bundled snapshot (41 records copied from Open Charge Map on 23 Oct 2026) is a fallback, not a complete inventory; records fetched by “Refresh listings” replace it record by record.</div>`;
}

async function loadGoogle() {
  const g = pub.providers.find(x => x.provider === "google_places");
  if (!g.configured) { toast(g.setup, true); return; }
  const areas = f.area && f.area !== "other" ? f.area : pub.areas.map(a => a.key).join(",");
  const nAreas = areas.split(",").length;
  const ok = await confirmBox({ title: "Run a paid Google Places lookup?",
    body: `This sends up to ${nAreas * 3} Text Search requests (${nAreas} area${nAreas > 1 ? "s" : ""} × up to 3 pages) to Google Places API (New) using your server-side key. Google bills these requests (the EV-charger field uses the Text Search Enterprise + Atmosphere SKU). Results are shown on the map and not stored.`,
    confirmLabel: "Run lookup once" });
  if (!ok) return;
  const btn = root.querySelector("#mgoogle");
  btn.disabled = true; btn.textContent = "Looking up…";
  try {
    const r = await api(`/api/public-stations/google?areas=${encodeURIComponent(areas)}&confirm_billing=true`);
    googleLive = r.results;
    const extra = r.results.filter(x => !x.matches_stored).length;
    toast(`Google Places: ${r.results.length} results (${extra} not already in stored listings).${r.errors.length ? " Errors: " + r.errors.map(e => e.error).join("; ") : ""}`, !!r.errors.length && !r.results.length);
    f.showGoogle = true; root.querySelector('[data-layer="showGoogle"]').checked = true;
    draw();
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = "Google Places lookup"; }
}

// ------------------------------------------------------------------ selection (marker <-> list <-> details)
function select(ft, from = "list") {
  if (!ft) return;
  selected = { kind: ft.kind, id: ft.id };
  if (ft.kind === "candidate") planning.onCandidateSelected(ft.id);
  renderDetail();
  const key = `${ft.kind}:${ft.id}`;
  root.querySelectorAll("#mlist tr[data-k]").forEach(tr => {
    const on = tr.dataset.k === key;
    tr.classList.toggle("sel", on); tr.setAttribute("aria-selected", String(on));
    if (on && from === "map") {           // scroll only the list box, never the page
      const box = root.querySelector("#mlist");
      const top = tr.offsetTop - box.clientHeight / 2;
      box.scrollTop = Math.max(0, top);
    }
  });
  engine && engine.highlight(ft);
  root.querySelector("#selCoord").textContent = `Selected: ${ft.lat.toFixed(6)}, ${ft.lng.toFixed(6)}`;
  setHashState();
}

// ------------------------------------------------------------------ panels
const UNK = '<span class="muted">unknown (not provided by the source)</span>';
const val = (v) => (v === null || v === undefined || v === "" ? UNK : esc(v));

function renderDetail() {
  const el = root.querySelector("#detail"), title = root.querySelector("#detTitle");
  const ft = features().find(x => x.kind === selected.kind && String(x.id) === String(selected.id));
  if (!ft) { title.textContent = "Station details"; el.innerHTML = '<div class="muted small">The selected item is hidden by the current filters or layers.</div>'; return; }
  const dir = `https://www.google.com/maps/dir/?api=1&destination=${ft.lat},${ft.lng}`;
  if (ft.kind === "public" || ft.kind === "google") {
    const s = ft.data;
    title.innerHTML = `${esc(s.name)} ${tag(s.classification === "IMPORTED" ? "IMPORTED" : "PUBLIC LISTING")}`;
    el.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px"><span class="pill st-offline" title="${esc(s.status_note)}">${esc(s.status || "STATUS UNKNOWN")}</span><span class="small muted">no live feed: availability and faults are not known</span></div>
      <dl class="kv">
        <div><dt>Operator / network</dt><dd>${val(s.operator)}</dd></div>
        <div><dt>Area</dt><dd>${esc(s.area_name)}${s.distance_to_area_centre_km !== undefined && s.area !== "other" ? ` <span class="small muted">${fmt(s.distance_to_area_centre_km, 1)} km from centre</span>` : ""}</dd></div>
        <div style="grid-column:1/-1"><dt>Address</dt><dd>${val(s.address)}</dd></div>
        <div><dt>Coordinates</dt><dd class="mono small">${s.lat.toFixed(6)}, ${s.lng.toFixed(6)}</dd></div>
        <div><dt>Max rated power</dt><dd>${s.max_power_kw ? fmt(s.max_power_kw, s.max_power_kw % 1 ? 1 : 0) + " kW" : UNK}</dd></div>
        <div><dt>Access</dt><dd>${val(s.usage_type)}</dd></div>
        <div><dt>Price (as listed)</dt><dd>${val(s.usage_cost)}${s.usage_cost && /kwh/i.test(s.usage_cost) && !/₹|inr|rs/i.test(s.usage_cost) ? ' <span class="muted small">(currency not stated)</span>' : ""}</dd></div>
        <div style="grid-column:1/-1"><dt>Opening hours</dt><dd>${val(s.opening_hours)}</dd></div>
      </dl>
      <h4 class="sub-h">Connectors (as listed)</h4>
      ${s.connections && s.connections.length ? `<table class="t"><thead><tr><th>Type</th><th class="r">kW</th><th class="r">Qty</th><th>Current</th></tr></thead><tbody>${s.connections.map(c => `<tr><td class="small">${esc(c.type)}</td><td class="r num">${c.power_kw ?? "?"}</td><td class="r num">${c.quantity ?? "?"}</td><td class="small">${esc(c.current || "?")}</td></tr>`).join("")}</tbody></table>` : `<div class="small">${UNK}</div>`}
      ${s.listed_status ? `<div class="note" style="margin-top:10px">Listing says: ${esc(s.listed_status)}. This is a declaration by the source, not live telemetry.</div>` : ""}
      <h4 class="sub-h">Source record and freshness</h4>
      <dl class="kv">
        <div><dt>Source</dt><dd>${esc(s.provider_label || SRC_NAME.google_places)} <span class="muted small mono">${esc(s.provider_id)}</span></dd></div>
        <div><dt>Retrieved</dt><dd>${s.retrieved_at ? dt(s.retrieved_at) : "just now (live lookup)"}</dd></div>
        <div><dt>Licence</dt><dd>${val(s.licence)}</dd></div>
        <div><dt>Classification</dt><dd>${esc(s.classification)}</dd></div>
        <div style="grid-column:1/-1"><dt>Record origin</dt><dd>${esc(s.origin_label || "live lookup (not stored)")}${s.absent_from_latest_refresh ? ' <span class="tag tag-ESTIMATED">NOT IN LATEST REFRESH</span><div class="small muted">The latest provider refresh did not return this record (removed upstream or outside the query). Kept for reference.</div>' : ""}</dd></div>
        <div style="grid-column:1/-1"><dt>Data provider</dt><dd class="small">${val(s.data_provider)}</dd></div>
        <div style="grid-column:1/-1"><dt>Attribution</dt><dd class="small">${val(s.attribution)}</dd></div>
        <div style="grid-column:1/-1"><dt>How retrieved</dt><dd class="small">${val(s.retrieval_method)}</dd></div>
      </dl>
      ${s.also_listed_by && s.also_listed_by.length ? `<div class="small" style="margin-top:8px"><b>Also listed by:</b> ${s.also_listed_by.map(o => `${esc(o.provider_label)} (${o.source_url ? `<a href="${esc(o.source_url)}" target="_blank" rel="noopener">${esc(o.provider_id)}</a>` : esc(o.provider_id)})`).join(", ")}</div>` : ""}
      ${s.matches_stored ? `<div class="small" style="margin-top:8px">Same place as stored listing “${esc(s.matches_stored.name)}” (${s.matches_stored.distance_m} m).</div>` : ""}
      <div class="toolbar" style="margin-top:12px">${s.source_url ? `<a class="btn-link" href="${esc(s.source_url)}" target="_blank" rel="noopener">Open source record ↗</a>` : ""}<a class="btn-link" href="${dir}" target="_blank" rel="noopener">Directions (Google Maps) ↗</a><button class="sm" id="dfocus">Zoom to station</button></div>
      <details class="small" style="margin-top:8px"><summary>Raw stored record</summary><pre class="formula" style="max-height:220px;overflow:auto">${esc(JSON.stringify(s, null, 2))}</pre></details>
      <div class="small muted" style="margin-top:8px">${esc(s.status_note)}</div>`;
  } else if (ft.kind === "demo") {
    const s = ft.data;
    title.innerHTML = `${esc(s.name)} ${tag("SIMULATED")}`;
    el.innerHTML = `<div class="note warn">Demonstration site: an area centroid with 40 synthetic chargers in total. It is <b>not</b> a real station and is not linked to any public listing.</div>
      <dl class="kv" style="margin-top:8px">
        <div><dt>Simulated availability</dt><dd>${fmt(s.availability_pct, 0)}% (${s.able_to_charge}/${s.chargers})</dd></div>
        <div><dt>Open tickets</dt><dd>${s.open_tickets} (${s.critical_open ?? 0} critical)</dd></div>
        <div style="grid-column:1/-1"><dt>Status mix</dt><dd>${Object.entries(s.status_counts).filter(([, n]) => n).map(([k, n]) => `${statusPill(k)} ${n}`).join(" ")}</dd></div>
      </dl><div class="toolbar" style="margin-top:10px"><a class="btn-link" href="#dashboard">Open the operations console</a><button class="sm" id="dfocus">Zoom here</button></div>`;
  } else if (ft.kind === "proposed") {
    const s = ft.data;
    title.innerHTML = `${esc(s.name)} ${tag("ESTIMATED")}`;
    el.innerHTML = `<dl class="kv"><div><dt>Investment</dt><dd>${fmtINR(s.summary.initial_investment)}</dd></div>
      <div><dt>Payback</dt><dd>${s.summary.payback_years ? fmt(s.summary.payback_years, 1) + " y" : "not reached"}</dd></div></dl>
      <div class="small muted" style="margin-top:8px">Proposed site saved in Site feasibility. Figures are ESTIMATED from ASSUMPTION inputs and NOT YET VALIDATED.</div>
      <div class="toolbar" style="margin-top:10px"><a class="btn-link" href="#investment">Open in Site feasibility</a><button class="sm" id="dfocus">Zoom here</button></div>`;
  } else if (ft.kind === "candidate") {
    planning.renderCandidateDetail(el, title, ft);
  } else if (ft.kind === "search") {
    title.textContent = "Search result";
    el.innerHTML = `<div class="small">${esc(ft.data.label)}</div><div class="mono small">${ft.lat.toFixed(6)}, ${ft.lng.toFixed(6)}</div><div class="small muted" style="margin-top:6px">A located point, not a charger listing.</div>`;
  }
  const fb = el.querySelector("#dfocus");
  if (fb) fb.onclick = () => engine && engine.focus(ft);
}

function renderList() {
  const el = root.querySelector("#mlist");
  const rows = features().filter(ft => ft.kind !== "search");
  if (!rows.length) {
    const a = areaByKey(f.area);
    const why = f.area && a && !a.stations ? esc(a.coverage_message || "") : "No stations match these filters or layers.";
    el.innerHTML = `<div class="empty">${why}</div>`; return;
  }
  el.innerHTML = `<table class="t" role="grid" aria-label="Stations shown on the map"><thead><tr><th>Name</th><th>Layer / source</th><th>Operator</th><th>Area</th><th class="r">Max kW</th><th>Status</th></tr></thead><tbody>
    ${rows.map(ft => {
      const s = ft.data, isSt = ft.kind === "public" || ft.kind === "google";
      const on = selected && selected.kind === ft.kind && String(selected.id) === String(ft.id);
      const src = ft.kind === "public" ? s.provider : ft.kind === "google" ? "google_places" : ft.kind;
      return `<tr class="click ${on ? "sel" : ""}" tabindex="0" aria-selected="${on}" data-k="${ft.kind}:${esc(ft.id)}">
        <td><b>${esc(s.name)}</b>${isSt && s.address ? `<div class="small muted">${esc(s.address.slice(0, 80))}</div>` : ""}</td>
        <td class="small"><i class="dotk" style="background:${ft.color}"></i>${esc(SRC_NAME[src])}${ft.kind === "public" ? `<div class="muted">${s.origin === "snapshot" ? "bundled snapshot" : "fetched " + dt(s.retrieved_at)}</div>` : ""}</td>
        <td class="small">${isSt ? val(s.operator) : "—"}</td><td class="small">${isSt ? esc(s.area_name) : ft.kind === "candidate" ? esc(s.area_name || "") : "—"}</td>
        <td class="r num">${isSt && s.max_power_kw ? fmt(s.max_power_kw, 0) : "—"}</td>
        <td>${isSt ? '<span class="pill st-offline">unknown</span>' : ft.kind === "demo" ? tag("SIMULATED") : ft.kind === "candidate" ? tag("CANDIDATE") : tag("ESTIMATED")}</td></tr>`;
    }).join("")}</tbody></table>`;
  const activate = (tr) => {
    const [kind, ...rest] = tr.dataset.k.split(":"); const id = rest.join(":");
    const ft = features().find(x => x.kind === kind && String(x.id) === id);
    if (ft) { select(ft, "list"); engine && engine.focus(ft); }
  };
  el.querySelectorAll("tr[data-k]").forEach(tr => {
    tr.onclick = () => activate(tr);
    tr.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(tr); }
      else if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); const n = e.key === "ArrowDown" ? tr.nextElementSibling : tr.previousElementSibling; if (n) n.focus(); }
    };
  });
}

function renderSummary() {
  const el = root.querySelector("#selSummary");
  const a = areaByKey(f.area);
  const st = visibleStations();
  root.querySelector("#sumTitle").innerHTML = `Selection summary — ${esc(a ? a.name : "All Bengaluru")}${f.operator ? ` · ${esc(f.operator)}` : ""}${f.q ? ` · “${esc(f.q)}”` : ""}`;
  if (!st.length) {
    el.innerHTML = `<div class="empty">${f.area && a && !a.stations ? `<b>${a.coverage_status === "true_zero" ? "True zero" : "Coverage unknown"}.</b> ${esc(a.coverage_message || "")}` : "No stored listings match the current filters."}</div>`;
    return;
  }
  const ops = {}, ct = {}, pc = {};
  let dc = 0, conns = 0, noConn = 0, maxKw = 0;
  const origins = { api: 0, snapshot: 0 };
  let newest = null;
  st.forEach(s => {
    const o = s.operator || "Unknown operator"; ops[o] = (ops[o] || 0) + 1;
    origins[s.origin] = (origins[s.origin] || 0) + 1;
    if (s.retrieved_at && (!newest || s.retrieved_at > newest)) newest = s.retrieved_at;
    if (!s.connections.length) noConn++;
    s.connections.forEach(c => {
      const q = c.quantity || 1; conns += q; ct[c.type || "unknown"] = (ct[c.type || "unknown"] || 0) + q;
      const kw = c.power_kw, cur = (c.current || "").toUpperCase();
      const k = kw === null || kw === undefined ? "unknown" : ("DC" === cur.slice(0, 2) || cur.includes("DC") || (!cur && kw > 22)) ? (kw > 60 ? "DC > 60 kW" : kw > 30 ? "DC 31-60 kW" : "DC <= 30 kW") : (kw <= 7.4 ? "AC <= 7.4 kW" : "AC > 7.4 kW");
      pc[k] = (pc[k] || 0) + q; if (k.startsWith("DC")) dc += q;
      if (kw && kw > maxKw) maxKw = kw;
    });
  });
  const items = (obj, color) => Object.entries(obj).sort((x, y) => y[1] - x[1]).map(([k, v], i) => ({ label: k, value: v, color: color || cat(i) }));
  el.innerHTML = `
    <div class="kpis" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
      <div class="kpi"><span class="lbl">Stations shown</span><span class="val num" id="sumStations">${st.length}</span><span class="sub">stored listings</span></div>
      <div class="kpi"><span class="lbl">Operators</span><span class="val num">${Object.keys(ops).length}</span></div>
      <div class="kpi"><span class="lbl">Connectors declared</span><span class="val num">${conns}</span><span class="sub">${dc} DC</span></div>
      <div class="kpi"><span class="lbl">Highest rated power</span><span class="val num">${maxKw ? fmt(maxKw, 0) + " kW" : "—"}</span></div>
      <div class="kpi"><span class="lbl">Without connector data</span><span class="val num">${noConn}</span><span class="sub">of ${st.length}</span></div>
      <div class="kpi"><span class="lbl">Data basis</span><span class="val" style="font-size:15px">${origins.api || 0} API · ${origins.snapshot || 0} snapshot</span><span class="sub">newest ${newest ? dt(newest) : "—"}</span></div>
    </div>
    <div class="grid-3c" style="margin-top:12px">
      <div><h4 class="sub-h">Stations by operator</h4>${hBars({ items: items(ops, cat(0)), fmtV: v => fmt(v, 0) })}</div>
      <div><h4 class="sub-h">Connector types</h4>${Object.keys(ct).length ? hBars({ items: items(ct, cat(2)), fmtV: v => fmt(v, 0) }) : chartEmpty("No connector data in these listings.")}</div>
      <div><h4 class="sub-h">AC / DC power classes</h4>${Object.keys(pc).length ? hBars({ items: items(pc, cat(3)), fmtV: v => fmt(v, 0) }) : chartEmpty("No power data in these listings.")}</div>
    </div>
    <div class="small muted" style="margin-top:8px">${esc(pub.status_note)}</div>`;
}

function renderLegend() {
  const items = [["open_charge_map", "square"], ["osm", "square"], ["google_places", "square"], ["demo", "circle"], ["proposed", "diamond"], ["candidate", "star"], ["search", "pin"]];
  root.querySelector("#mapLegend").innerHTML = items.map(([k, sh]) => `<span><img alt="" width="14" height="14" src="${svgIcon({ color: SRC_COLOR[k], shape: sh })}" style="vertical-align:-2px;margin-right:4px">${esc(SRC_NAME[k])}</span>`).join("")
    + '<span>· demo-site ring colour = worst SIMULATED status · blue circle = locality radius (ASSUMPTION)</span>';
  const a = (pub.attributions || []).map(x => `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.text)}</a>`).join(" · ");
  root.querySelector("#mapAttrib").innerHTML = `${esc(engine ? engine.attribution : "")} · Station data: ${a}`;
}

function renderInventory() {
  const el = root.querySelector("#inventory");
  if (!inv) { el.innerHTML = ""; return; }
  const areaRows = [...locs.areas, locs.other].map(a => `<tr><td><b>${esc(a.name)}</b>${a.radius_km ? `<div class="small muted">within ${a.radius_km} km of the centre</div>` : ""}</td><td class="r num">${a.stations}</td>
    <td class="small">${a.stations ? Object.entries(a.operators).map(([o, n]) => `${esc(o)} (${n})`).join(", ") : `<span style="color:var(--warn)">${esc(a.coverage_message || "none")}</span>`}</td><td class="small">${esc(freshness(a))}</td></tr>`).join("");
  const prov = inv.providers.map(p => `<tr><td>${esc(p.label)}</td><td class="r num">${p.records}</td><td class="r num">${p.linked_duplicates}</td><td class="small">${dt(p.newest_retrieved_at)}</td><td class="small">${esc(p.licence || "")}</td></tr>`).join("");
  const st = inv.providers_status.map(p => `<tr><td>${esc(p.label)}</td><td>${p.configured ? '<span class="pill st-available">configured</span>' : '<span class="pill st-offline">not configured</span>'}</td><td class="small">${p.stored ? "stored with attribution" : "live lookup, never stored"}</td><td class="small">${esc(p.setup || "")}</td></tr>`).join("");
  el.innerHTML = `
    <section class="panel" style="grid-column:1/-1"><div class="panel-h"><h2>Inventory by area ${tag("PUBLIC LISTING")}</h2><span class="hint">${locs.all.stations} stations · <a href="/api/reports/public_stations.csv">Export CSV</a></span></div>
      <div class="panel-b flush tbl-wrap"><table class="t"><thead><tr><th>Area</th><th class="r">Stations</th><th>Operators / gaps</th><th>Freshness</th></tr></thead><tbody>${areaRows}</tbody></table></div>
      <div class="panel-b small muted">${esc(locs.definitions.area)} ${esc(locs.definitions.counting)}</div></section>
    <section class="panel" style="grid-column:1/-1"><div class="panel-h"><h2>Sources and freshness</h2></div>
      <div class="panel-b flush tbl-wrap"><table class="t"><thead><tr><th>Stored source</th><th class="r">Records</th><th class="r">Merged duplicates</th><th>Newest retrieval</th><th>Licence</th></tr></thead><tbody>${prov || '<tr><td colspan="5" class="muted">No stored listings.</td></tr>'}</tbody></table>
      <table class="t" style="margin-top:8px"><thead><tr><th>Provider</th><th>Setup</th><th>Storage</th><th>What to do</th></tr></thead><tbody>${st}</tbody></table></div>
      <div class="panel-b small muted">${esc(inv.status_note)}</div></section>`;
}
