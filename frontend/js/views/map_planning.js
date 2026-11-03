// Candidate-site planning on the Map page: pick a point (map click, coordinates or address),
// see nearby PUBLIC LISTING chargers with distances, record what you know (USER-ENTERED),
// fetch OpenStreetMap context on request, compare candidates and continue to Site Feasibility.
import { api } from "../api.js";
import { esc, fmt, fmtINR, tag, toast, confirmBox } from "../ui.js";
import { renderScorePanel } from "./map_scoring.js";

let S = null;                      // shared map state (see map.js `shared`)
let candidates = [];
let planMode = false;
let pending = null;                // { lat, lng, preview }
let current = null;                // selected candidate id
let busyContext = false, busySave = false;
const CTX_KEY = "cs.planningContext";

export const ROAD_LABEL = { arterial: "Arterial road (primary/trunk)", main: "Main road (secondary/tertiary)", local: "Local street", unknown: "Unknown" };

export async function init(shared, hp) {
  S = shared;
  const btn = S.root.querySelector("#mplan");
  btn.onclick = () => setPlanMode(!planMode);
  await loadCandidates();
  renderPanels();
  if (hp.candidate) {
    const c = candidates.find(x => String(x.id) === String(hp.candidate));
    if (c) {
      current = c.id;
      // restore after the first draw (map.js calls draw right after init)
      setTimeout(() => { const ft = features(true).find(x => String(x.id) === String(c.id)); if (ft && S) { S.select(ft, "hash"); S.engine && S.engine.focus(ft); } }, 0);
    } else {
      toast(`Candidate #${hp.candidate} no longer exists.`, true);
    }
  }
}
export function unmount() { S = null; planMode = false; pending = null; }

async function loadCandidates() {
  try { candidates = await api("/api/planning/candidates"); }
  catch (e) { candidates = []; toast(`Could not load planning candidates: ${e.message}`, true); }
}

export function currentCandidateId() { return current; }
export function currentPoint() {
  if (pending) return { lat: pending.lat, lng: pending.lng };
  const c = candidates.find(x => x.id === current);
  return c ? { lat: c.lat, lng: c.lng } : null;
}

export function features(show) {
  const out = [];
  if (show) candidates.forEach(c => out.push({ kind: "candidate", id: c.id, lat: c.lat, lng: c.lng, title: `${c.name} — planning candidate`,
    color: "#e0368a", shape: "star", data: { ...c, area_name: c.area_name } }));
  if (pending) out.push({ kind: "candidate", id: "pending", lat: pending.lat, lng: pending.lng, title: "New candidate point (not saved)", color: "#e0368a", shape: "pin", data: { name: "New candidate point (not saved)", pending: true } });
  return out;
}

// ------------------------------------------------------------------ plan mode
function setPlanMode(on) {
  planMode = on;
  const btn = S.root.querySelector("#mplan");
  btn.setAttribute("aria-pressed", String(on));
  btn.textContent = on ? "Stop planning" : "Plan a new charging site";
  const bar = S.root.querySelector("#planBar");
  if (!on) { bar.innerHTML = ""; if (pending) { pending = null; S.draw(); } return; }
  const area = S.f.area ? S.areaByKey(S.f.area) : null;
  bar.innerHTML = `<div class="plan-bar">
    <div><b>Plan a new charging site.</b> ${area ? `Locality: <b>${esc(area.name)}</b>. ` : "Tip: choose a locality above first. "}Click the map, type coordinates, or search an address in the search box.</div>
    <form class="toolbar" id="planForm" style="margin-top:8px;gap:6px">
      <label class="field inline"><span class="small">Latitude</span><input id="pLat" type="number" step="0.000001" style="width:130px" required></label>
      <label class="field inline"><span class="small">Longitude</span><input id="pLng" type="number" step="0.000001" style="width:130px" required></label>
      <button class="sm" type="submit">Use these coordinates</button>
      <span class="small muted">Study area: latitude 12.75–13.20, longitude 77.45–77.80</span>
    </form></div>`;
  bar.querySelector("#planForm").onsubmit = (e) => { e.preventDefault(); setPending(Number(bar.querySelector("#pLat").value), Number(bar.querySelector("#pLng").value)); };
}

export function onMapClick(lat, lng) { if (planMode) setPending(lat, lng); }
export function onSearchPoint(lat, lng) { if (planMode) setPending(lat, lng); }

async function setPending(lat, lng) {
  if (!S) return;
  let pv;
  try { pv = await api(`/api/planning/preview?lat=${encodeURIComponent(lat)}&lng=${encodeURIComponent(lng)}`); }
  catch (e) { toast(e.message, true); return; }
  pending = { lat: pv.lat, lng: pv.lng, preview: pv };
  const bar = S.root.querySelector("#planBar");
  if (bar.querySelector("#pLat")) { bar.querySelector("#pLat").value = pv.lat; bar.querySelector("#pLng").value = pv.lng; }
  S.draw();
  const ft = S.features().find(x => x.kind === "candidate" && x.id === "pending");
  if (ft) { S.select(ft, "map"); S.engine && S.engine.focus(ft); }
}

// ------------------------------------------------------------------ details panel
export function onCandidateSelected(id) { if (id !== "pending") current = Number(id); }

function nearbyHtml(ev) {
  const nb = ev.nearby;
  if (!nb.stored_listings_considered) return `<div class="note warn">No stored public listings to compare against, so coverage cannot be assessed.</div>`;
  return `<dl class="kv">
      <div><dt>Locality</dt><dd>${esc(ev.area_name)}${ev.in_focus_area ? ` <span class="small muted">${fmt(ev.distance_to_area_centre_km, 1)} km from centre</span>` : ' <span class="small muted">outside the six focus radii</span>'}</dd></div>
      <div><dt>Nearest listed charger</dt><dd class="num">${nb.nearest_km === null ? "—" : nb.nearest_km < 1 ? fmt(nb.nearest_km * 1000, 0) + " m" : fmt(nb.nearest_km, 2) + " km"}</dd></div>
      <div><dt>Listed within 0.5 / 1 / 2 / 5 km</dt><dd class="num">${nb.within_km["0.5"]} / ${nb.within_km["1.0"]} / ${nb.within_km["2.0"]} / ${nb.within_km["5.0"]}</dd></div>
      <div><dt>Connectors within 2 km</dt><dd class="num">${nb.connectors_within_2km} (${nb.dc_connectors_within_2km} DC)</dd></div>
      <div style="grid-column:1/-1"><dt>Operators within 2 km</dt><dd class="small">${nb.operators_within_2km.length ? nb.operators_within_2km.map(esc).join(", ") : "none listed"}</dd></div>
    </dl>
    <h4 class="sub-h">Nearest listed chargers ${tag("PUBLIC LISTING")}</h4>
    <table class="t"><thead><tr><th>Station</th><th class="r">Distance</th><th class="r">Max kW</th></tr></thead><tbody>
      ${nb.nearest.slice(0, 6).map(n => `<tr class="click" data-goto="${n.id}" tabindex="0"><td class="small"><b>${esc(n.name)}</b><div class="muted">${esc(n.operator || "Unknown operator")} · ${esc(n.provider_label || "")}</div></td><td class="r num">${n.distance_km < 1 ? fmt(n.distance_km * 1000, 0) + " m" : fmt(n.distance_km, 2) + " km"}</td><td class="r num">${n.max_power_kw ? fmt(n.max_power_kw, 0) : "—"}</td></tr>`).join("")}
    </tbody></table>
    <div class="small muted" style="margin-top:6px">${esc(nb.caveat)}</div>`;
}

function bindGoto(el) {
  el.querySelectorAll("[data-goto]").forEach(tr => {
    const go = () => {
      S.f.showPublic = true;
      let ft = S.features().find(x => x.kind === "public" && String(x.id) === tr.dataset.goto);
      if (!ft) { S.f.area = ""; S.f.operator = ""; S.f.q = ""; S.draw(); ft = S.features().find(x => x.kind === "public" && String(x.id) === tr.dataset.goto); }
      if (ft) { S.select(ft, "list"); S.engine && S.engine.focus(ft); }
    };
    tr.onclick = go; tr.onkeydown = (e) => { if (e.key === "Enter") go(); };
  });
}

export function renderCandidateDetail(el, title, ft) {
  if (ft.id === "pending") {
    const pv = pending.preview;
    title.innerHTML = `New candidate point ${tag("CANDIDATE")}`;
    el.innerHTML = `
      <div class="mono small">${pv.lat.toFixed(6)}, ${pv.lng.toFixed(6)}</div>
      ${nearbyHtml(pv)}
      <form id="candSave" class="stack" style="gap:8px;margin-top:12px">
        <label class="field"><span>Candidate name</span><input id="cName" required maxlength="120" value="${esc(`${pv.area_name} candidate ${candidates.length + 1}`)}"></label>
        <div class="toolbar"><button class="primary" type="submit" id="cSaveBtn">Save as candidate</button><button type="button" class="sm" id="cDiscard">Discard point</button></div>
        <div class="small muted">Saving stores only the point, its name and what you enter later. It does not create a proposed site.</div>
      </form>`;
    bindGoto(el);
    el.querySelector("#cDiscard").onclick = () => { pending = null; S.draw(); el.innerHTML = '<div class="muted small">Point discarded.</div>'; };
    el.querySelector("#candSave").onsubmit = async (e) => {
      e.preventDefault();
      if (busySave) return;
      busySave = true; el.querySelector("#cSaveBtn").disabled = true;
      try {
        const c = await api("/api/planning/candidates", { method: "POST", body: { name: el.querySelector("#cName").value.trim(), lat: pending.lat, lng: pending.lng } });
        candidates.push(c); pending = null; current = c.id;
        toast(`Saved candidate “${c.name}”.`);
        S.draw(); renderPanels();
        const nf = S.features().find(x => x.kind === "candidate" && x.id === c.id);
        if (nf) S.select(nf, "map");
      } catch (err) { toast(err.message, true); }
      finally { busySave = false; const b = el.querySelector("#cSaveBtn"); if (b) b.disabled = false; }
    };
    return;
  }
  const c = candidates.find(x => x.id === ft.id);
  if (!c) { el.innerHTML = '<div class="muted small">Candidate not found.</div>'; return; }
  current = c.id;
  const i = c.inputs || {}, osm = c.context && c.context.osm;
  title.innerHTML = `${esc(c.name)} ${tag("CANDIDATE")}`;
  el.innerHTML = `
    <div class="mono small">${c.lat.toFixed(6)}, ${c.lng.toFixed(6)}</div>
    ${c.saved_site_id ? `<div class="note" style="margin-top:6px">Saved as proposed site #${c.saved_site_id} in Site feasibility.</div>` : ""}
    ${nearbyHtml(c.evidence)}
    <h4 class="sub-h">Area context ${tag("SOURCE DATA")}</h4>
    <div id="osmBox">${osmHtml(osm)}</div>
    <div class="toolbar" style="margin-top:6px"><button class="sm" id="cOsm">${osm ? "Refresh OpenStreetMap context" : "Look up OpenStreetMap context"}</button><span class="small muted">one bounded request (500 m)</span></div>
    <h4 class="sub-h">What you know about this site ${tag("USER-ENTERED")}</h4>
    <form id="candInputs" class="form-grid" style="gap:8px">
      ${numField("n_chargers", "Chargers planned", i.n_chargers, 1)}
      <label class="field"><span>Charger type</span><select name="charger_type"><option value="">not decided</option><option ${i.charger_type === "DC" ? "selected" : ""}>DC</option><option ${i.charger_type === "AC" ? "selected" : ""}>AC</option></select></label>
      ${numField("power_kw", "Power per charger (kW)", i.power_kw, 0.1)}
      ${numField("budget_inr", "Investment budget (₹)", i.budget_inr, 10000)}
      ${numField("sessions_per_day", "Expected sessions/day (your estimate)", i.sessions_per_day, 0.5)}
      ${numField("site_area_m2", "Usable site area (m²)", i.site_area_m2, 1)}
      ${numField("grid_available_kva", "Available grid capacity (kVA)", i.grid_available_kva, 1)}
      <label class="field"><span>Grid capacity confirmed by utility/landlord?</span><select name="grid_confirmed"><option value="">not stated</option><option value="true" ${i.grid_confirmed === true ? "selected" : ""}>yes, confirmed in writing</option><option value="false" ${i.grid_confirmed === false ? "selected" : ""}>no, my assumption</option></select></label>
      <label class="field"><span>Road access (observed)</span><select name="road_access"><option value="">not stated</option>${Object.entries(ROAD_LABEL).map(([k, v]) => `<option value="${k}" ${i.road_access === k ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      ${numField("amenities_observed", "Shops/offices/food places you counted within ~500 m", i.amenities_observed, 1)}
      <label class="field" style="grid-column:1/-1"><span>Evidence notes</span><textarea name="evidence_note" rows="2" maxlength="2000">${esc(i.evidence_note || "")}</textarea></label>
      <div class="toolbar" style="grid-column:1/-1"><button class="sm primary" type="submit" id="cInSave">Save inputs</button><span class="small muted">Leave a field empty if you do not know it; it will be listed as missing evidence.</span></div>
    </form>
    <div class="toolbar" style="margin-top:12px">
      <button class="primary" id="cFeas">Continue to Site Feasibility →</button>
      <button class="sm" id="dfocus">Zoom here</button>
      <button class="sm danger" id="cDel">Delete candidate</button>
    </div>`;
  bindGoto(el);
  el.querySelector("#cOsm").onclick = () => lookupContext(c, el);
  el.querySelector("#candInputs").onsubmit = (e) => { e.preventDefault(); saveInputs(c, e.target); };
  el.querySelector("#cFeas").onclick = () => goFeasibility(c);
  el.querySelector("#cDel").onclick = async () => {
    const ok = await confirmBox({ title: `Delete candidate “${c.name}”?`, body: `<p>This removes the candidate and its notes.${c.saved_site_id ? ` The proposed site #${c.saved_site_id} saved from it stays in Site feasibility.` : ""}</p>`, confirmLabel: "Delete", danger: true });
    if (!ok) return;
    try { await api(`/api/planning/candidates/${c.id}`, { method: "DELETE" }); candidates = candidates.filter(x => x.id !== c.id); current = null; S.draw(); renderPanels(); el.innerHTML = '<div class="muted small">Candidate deleted.</div>'; S.setHashState(); }
    catch (err) { toast(err.message, true); }
  };
}

function numField(name, label, v, step) {
  return `<label class="field"><span>${esc(label)}</span><input name="${name}" type="number" step="${step}" min="0" value="${v ?? ""}"></label>`;
}

export function osmHtml(osm) {
  if (!osm) return '<div class="small muted">No OpenStreetMap context fetched yet. Amenities and road class are treated as missing evidence unless you enter what you observed.</div>';
  const cats = Object.entries(osm.amenity_categories || {}).map(([k, n]) => `${esc(k.replace(/_/g, " "))} ${n}`).join(" · ");
  return `<dl class="kv">
    <div><dt>Mapped amenities within ${osm.amenities_within_m} m</dt><dd class="num">${osm.amenity_total}</dd></div>
    <div><dt>Best road within ${osm.roads_within_m} m</dt><dd>${osm.best_road ? `${esc(osm.best_road.class)} (${esc(osm.best_road.highway)}${osm.best_road.name ? ", " + esc(osm.best_road.name) : ""}${osm.best_road.distance_m !== null ? `, ~${osm.best_road.distance_m} m` : ""})` : "no mapped road found"}</dd></div>
    <div style="grid-column:1/-1"><dt>Categories</dt><dd class="small">${cats || "none mapped"}</dd></div></dl>
    <div class="small muted">${esc(osm.attribution)} · retrieved ${esc((osm.retrieved_at || "").replace("T", " ").replace("Z", " UTC"))}. ${esc(osm.note || "")}</div>`;
}

async function lookupContext(c, el, force = false) {
  if (busyContext) return;
  busyContext = true;
  const b = el.querySelector("#cOsm"); if (b) { b.disabled = true; b.textContent = "Looking up…"; }
  try {
    const r = await api(`/api/planning/candidates/${c.id}/context${force ? "?force=true" : ""}`, { method: "POST" });
    if (r.ok) {
      c.context = { ...(c.context || {}), osm: r.osm };
      el.querySelector("#osmBox").innerHTML = osmHtml(r.osm);
      toast(r.cached ? r.message : `OpenStreetMap: ${r.osm.amenity_total} mapped amenities within ${r.osm.amenities_within_m} m.`);
      if (r.cached && await confirmBox({ title: "Look up again now?", body: "<p>The stored lookup is less than 10 minutes old. Overpass asks clients to avoid repeated requests.</p>", confirmLabel: "Look up anyway" })) { busyContext = false; return lookupContext(c, el, true); }
    } else {
      toast(r.message, true);
      el.querySelector("#osmBox").insertAdjacentHTML("afterbegin", `<div class="error-box" style="margin-bottom:6px">${esc(r.message)}</div>`);
    }
  } catch (e) { toast(e.message, true); }
  finally { busyContext = false; const bb = el.querySelector("#cOsm"); if (bb) { bb.disabled = false; bb.textContent = "Refresh OpenStreetMap context"; } }
  renderPanels();
}

async function saveInputs(c, form) {
  const body = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    const v = el.value;
    if (v === "") continue;
    if (el.name === "charger_type" || el.name === "road_access" || el.name === "evidence_note") body[el.name] = v;
    else if (el.name === "grid_confirmed") body[el.name] = v === "true";
    else body[el.name] = Number(v);
  }
  const b = form.querySelector("#cInSave"); b.disabled = true;
  try {
    const r = await api(`/api/planning/candidates/${c.id}`, { method: "PUT", body: { inputs: body } });
    Object.assign(c, r);
    toast("Inputs saved.");
    renderPanels();
  } catch (e) { toast(e.message, true); }
  finally { b.disabled = false; }
}

function goFeasibility(c) {
  try { sessionStorage.setItem(CTX_KEY, JSON.stringify({ area: S.f.area, candidate: c.id, at: Date.now() })); } catch { /* optional */ }
  location.hash = `#investment?candidate=${c.id}&from=map${S.f.area ? "&area=" + encodeURIComponent(S.f.area) : ""}`;
}

// ------------------------------------------------------------------ candidates panel (comparison of evidence)
export function renderPanels() {
  if (!S) return;
  const el = S.root.querySelector("#planPanels");
  if (!candidates.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<section class="panel"><div class="panel-h"><h2>Planning candidates ${tag("CANDIDATE")}</h2><span class="hint">${candidates.length} saved · evidence side by side</span></div>
    <div class="panel-b flush tbl-wrap"><table class="t" id="candTable"><thead><tr><th><span class="sr-only">Select</span></th><th>Candidate</th><th>Locality</th><th class="r">Nearest listed</th><th class="r">Listed ≤1 / 2 km</th><th class="r">DC conn. ≤2 km</th><th>OSM amenities</th><th>Your inputs</th><th></th></tr></thead><tbody>
      ${candidates.map(c => {
        const nb = c.evidence.nearby, osm = c.context && c.context.osm, n = Object.keys(c.inputs || {}).length;
        return `<tr data-cid="${c.id}" class="${c.id === current ? "sel" : ""}"><td><input type="checkbox" data-cmp="${c.id}" aria-label="Select ${esc(c.name)} for comparison" checked></td>
          <td><b>${esc(c.name)}</b><div class="small mono muted">${c.lat.toFixed(5)}, ${c.lng.toFixed(5)}</div></td><td class="small">${esc(c.area_name)}</td>
          <td class="r num">${nb.nearest_km === null ? "—" : nb.nearest_km < 1 ? fmt(nb.nearest_km * 1000, 0) + " m" : fmt(nb.nearest_km, 2) + " km"}</td>
          <td class="r num">${nb.within_km["1.0"]} / ${nb.within_km["2.0"]}</td><td class="r num">${nb.dc_connectors_within_2km}</td>
          <td class="small">${osm ? `${osm.amenity_total} within ${osm.amenities_within_m} m` : '<span class="muted">not looked up</span>'}</td>
          <td class="small">${n ? `${n} field${n === 1 ? "" : "s"}` : '<span class="muted">none</span>'}${c.saved_site_id ? ` · saved #${c.saved_site_id}` : ""}</td>
          <td><button class="sm" data-show="${c.id}">Show</button> <button class="sm" data-feas="${c.id}">Feasibility →</button></td></tr>`;
      }).join("")}</tbody></table></div>
    <div class="panel-b" id="scorePanel"></div></section>`;
  el.querySelectorAll("[data-show]").forEach(b => b.onclick = () => {
    const ft = S.features().find(x => x.kind === "candidate" && String(x.id) === b.dataset.show);
    if (!S.f.showCandidates) { S.f.showCandidates = true; const cb = S.root.querySelector('[data-layer="showCandidates"]'); if (cb) cb.checked = true; S.draw(); }
    const f2 = ft || S.features().find(x => x.kind === "candidate" && String(x.id) === b.dataset.show);
    if (f2) { S.select(f2, "list"); S.engine && S.engine.focus(f2); S.root.querySelector("#mapBox").scrollIntoView({ behavior: "smooth", block: "center" }); }
  });
  el.querySelectorAll("[data-feas]").forEach(b => b.onclick = () => goFeasibility(candidates.find(x => String(x.id) === b.dataset.feas)));
  renderScorePanel(el.querySelector("#scorePanel"), candidates, () => [...el.querySelectorAll("[data-cmp]:checked")].map(i => Number(i.dataset.cmp)));
}

export function getCandidates() { return candidates; }
export { fmtINR };
