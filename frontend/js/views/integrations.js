// Data integrations: per-mode data state, connector health, CSV import (with sample),
// historical charging sessions (IEEE format), station inventory import,
// operator REST API diagnostics, OCPP central system workflow and public listings.
import { api } from "../api.js";
import { esc, dt, tag, toast, errorBox, confirmBox } from "../ui.js";
import { state } from "../app.js";
import { mountConnections } from "./connections.js";

let root, ctx;
const STATE_CLASS = { connected: "st-available", configured_unverified: "st-unavailable", not_configured: "st-offline", error: "st-faulted", disabled: "st-offline" };
const STATE_LABEL = { connected: "Connected (verified)", configured_unverified: "Configured, not verified", not_configured: "Not configured", error: "Error", disabled: "Disabled" };
export const MODE_STATE_CLASS = {
  "SIMULATED": "st-charging", "SIMULATING IMPORTED": "st-charging", "NOT SIMULATING": "st-offline",
  "RECEIVING DATA": "st-available", "RECEIVING TEST DATA": "st-unavailable", "IMPORTED": "st-available",
  "IMPORTED INVENTORY": "st-available", "HISTORICAL RESEARCH": "st-available",
  "STALE": "st-unavailable", "WAITING FOR DATA": "st-unavailable", "NO LIVE DATA": "st-offline",
  "NO IMPORTED DATA": "st-offline", "NO HISTORICAL DATA": "st-offline",
  "NOT CONNECTED": "st-offline", "NOT CONFIGURED": "st-offline", "ERROR": "st-faulted",
};
const OP_NEEDS = [
  "Written permission to read their charger data for this academic project (scope, retention, no redistribution).",
  "API base URL and API documentation (endpoints, pagination, rate limits).",
  "Authentication method and a read-only, revocable key or OAuth client.",
  "Charger/connector identifiers and site metadata (location, power, connector type).",
  "Event or status endpoint with timestamps and timezone, plus their status vocabulary.",
  "Fault/error-code list and mapping to OCPP 1.6 error codes if they use their own codes.",
  "A sandbox or test account, and the expected polling interval.",
];

export async function render(el, c) {
  root = el; ctx = c;
  el.innerHTML = `
    <div class="note">A connector is marked <b>Connected</b> only after a verified response or handshake. Every record keeps its provenance: ${tag("IMPORTED")} for operator events, ${tag("HISTORICAL")} for IEEE empirical research sessions, ${tag("INVENTORY")} for station inventory facts, ${tag("LIVE")} for authorised API or OCPP data, ${tag("SIMULATED")} for internal simulation, ${tag("PUBLIC LISTING")} for map listings.</div>
    <section class="panel"><div class="panel-h"><h2>Connections</h2><span class="hint">your organisation's own operator API and OCPP connections</span></div><div class="panel-b" id="conns"><div class="loading">Loading…</div></div></section>
    <section class="panel"><div class="panel-h"><h2>Data-source modes</h2><span class="hint">what each option in the Data selector currently contains</span></div><div class="panel-b"><div class="cards" id="modes"></div></div></section>
    <section class="panel"><div class="panel-h"><h2>Connector health</h2><button class="sm" id="reload">Refresh</button></div><div class="panel-b"><div class="cards" id="health"></div></div></section>

    <!-- WORKFLOW A: Historical Charging Sessions (IEEE format) -->
    <section class="panel" style="border-left:4px solid var(--accent)">
      <div class="panel-h">
        <div>
          <h2>Import Historical Charging Sessions (IEEE format)</h2>
          <span class="hint">Empirical research dataset (e.g. IEEE ChargingRecords.csv) · demand, duration and arrival analytics</span>
        </div>
        ${tag("HISTORICAL")}
      </div>
      <div class="panel-b stack" style="gap:12px">
        <div class="toolbar" style="gap:10px">
          <a class="btn-link sm" href="/api/integrations/samples/charging-records.csv" download>Download IEEE sample CSV</a>
          <button type="button" class="sm" id="importHistSample">Import IEEE sample now</button>
          <a class="btn-link sm" href="#historical">View Historical Sessions page →</a>
        </div>
        <form id="histForm" class="stack" style="gap:10px">
          <label class="field">
            <span>Dataset label</span>
            <input type="text" name="dataset_label" value="IEEE ChargingRecords Research Dataset" required maxlength="200">
          </label>
          <label class="field">
            <span>CSV file (UTF-8, max 15 MB)</span>
            <input type="file" id="histFile" name="file" accept=".csv,text/csv" required>
          </label>
          <div class="toolbar" style="gap:8px">
            <button class="ghost" type="button" id="histPreviewBtn">Preview &amp; Validate</button>
            <button class="primary" type="submit" id="histImportBtn">Import Dataset</button>
          </div>
          <div class="small muted">Expected columns: UserID, ChargerID, ChargerCompany, Location, ChargerType, StartDay, StartTime, EndDay, EndTime, StartDatetime, EndDatetime, Duration, Demand. Original column values and units are retained without unverified assumptions. Duplicate records are skipped automatically.</div>
        </form>
        <div id="histPreviewBox"></div>
        <div id="histResult"></div>
      </div>
    </section>

    <!-- WORKFLOW B: Station Inventory CSV -->
    <section class="panel" style="border-left:4px solid #10b981">
      <div class="panel-h">
        <div>
          <h2>Import Station Inventory (CSV)</h2>
          <span class="hint">State / national EV charging infrastructure inventory · coordinates, operators and connector ratings</span>
        </div>
        ${tag("INVENTORY")}
      </div>
      <div class="panel-b stack" style="gap:12px">
        <div class="toolbar" style="gap:10px">
          <a class="btn-link sm" href="/api/integrations/samples/station-inventory.csv" download>Download Karnataka sample CSV</a>
          <button type="button" class="sm" id="importStationSample">Import Karnataka sample now</button>
          <a class="btn-link sm" href="#map">View stations on Map →</a>
        </div>
        <form id="stationForm" class="stack" style="gap:10px">
          <label class="field">
            <span>Dataset label</span>
            <input type="text" name="dataset_label" value="Karnataka EV Station Inventory" required maxlength="200">
          </label>
          <label class="field">
            <span>CSV file (UTF-8, max 5 MB)</span>
            <input type="file" id="stationFile" name="file" accept=".csv,text/csv" required>
          </label>
          <div class="toolbar" style="gap:16px;align-items:center">
            <label class="check"><input type="checkbox" id="filterKarnataka" name="filter_karnataka" checked> Filter for Karnataka locations</label>
            <label class="check"><input type="checkbox" id="filterBengaluru" name="filter_bengaluru"> Filter for Bengaluru metropolitan area</label>
          </div>
          <div class="toolbar" style="gap:8px">
            <button class="ghost" type="button" id="stationPreviewBtn">Preview &amp; Validate</button>
            <button class="primary" type="submit" id="stationImportBtn">Import Inventory</button>
          </div>
          <div class="small muted">Supported fields: Operator, Government/private ownership, State, District, City or village, Station/location name, Latitude, Longitude, Charger type, Charger rating, Connector rating, Total connectors. Automatically syncs with Map page. Stable station hashing prevents duplicate imports.</div>
        </form>
        <div id="stationPreviewBox"></div>
        <div id="stationResult"></div>
      </div>
    </section>

    <!-- WORKFLOW C: Authorised Operator Events (Existing) -->
    <div class="grid-2e">
      <section class="panel"><div class="panel-h"><h2>Import authorised operator events (CSV)</h2>${tag("IMPORTED")}</div>
        <form class="panel-b stack" id="csvForm" style="gap:10px">
          <div class="toolbar"><a class="btn-link" href="/api/integrations/samples/operator-events.csv" download>Download sample CSV</a>
            <button type="button" class="sm" id="importSample">Import the synthetic sample now</button>
            <span class="small muted">SYNTHETIC test file with deliberate errors. Not real operator data.</span></div>
          <label class="field"><span>Dataset label (required): who shared it and for which period</span><input type="text" name="dataset_label" required maxlength="200"></label>
          <label class="field"><span>CSV file (UTF-8, max 5 MB)</span><input type="file" name="file" accept=".csv,text/csv" required></label>
          <div><button class="primary" type="submit">Import</button></div>
          <div class="small muted">Required columns: external_event_id, external_charger_id, timestamp (ISO 8601 with offset), event_type. Optional: status, fault_category, site_code, site_name, lat, lng, power_kw, connector_type, meter_kwh. Duplicate external_event_id values are skipped.</div>
          <div id="csvResult"></div>
        </form></section>
      <section class="panel ${state.me.organisation.is_system_demo ? "" : "hidden"}" id="opPanel"><div class="panel-h"><h2>Operator REST API (server .env, system demo workspace)</h2>${tag("LIVE")}</div>
        <div class="panel-b stack" style="gap:10px">
          <div id="opStatus"></div>
          <div class="toolbar"><button id="opDiagnose">Diagnose</button><button id="opTest">Test /health</button><button id="opSync">Sync events</button></div>
          <div id="opResult"></div>
          <details><summary>What to ask the operator for (e.g. Kazam)</summary><ol class="small" style="margin:6px 0 0;padding-left:18px">${OP_NEEDS.map(x => `<li>${esc(x)}</li>`).join("")}</ol>
            <div class="small muted" style="margin-top:6px">No operator has granted access yet. ChargeSight will not guess endpoints, scrape apps or show placeholder data. Adapter contract: docs/INTEGRATION.md §2.</div></details>
        </div></section>
    </div>

    <div class="grid-2e">
      <section class="panel"><div class="panel-h"><h2>OCPP 1.6J central system</h2>${tag("LIVE")}</div><div class="panel-b stack" style="gap:10px" id="ocpp"></div></section>
      <section class="panel"><div class="panel-h"><h2>Public listings (map)</h2>${tag("PUBLIC LISTING")}</div><div class="panel-b stack" style="gap:10px">
        <div id="pubBox" class="stack" style="gap:10px"></div>
        <div class="toolbar ${state.me.user.is_platform_admin ? "" : "hidden"}"><button class="primary" id="ocmFetch">Fetch Bengaluru listings</button><span class="small muted">Open Charge Map only · one request · stored records are kept if it fails</span></div>
        <div id="ocmResult" aria-live="polite"></div>
      </div></section>
    </div>
    <section class="panel"><div class="panel-h"><h2>Ingestion runs</h2></div><div class="panel-b flush tbl-wrap" id="runs"></div></section>`;

  el.querySelector("#reload").onclick = load;
  mountConnections(el.querySelector("#conns"), () => load());
  el.querySelector("#ocmFetch").onclick = () => fetchOcm(false);

  // Existing Operator events import handlers
  el.querySelector("#csvForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/integrations/import/events", { method: "POST", form: new FormData(e.target) });
      el.querySelector("#csvResult").innerHTML = runResult(r);
      toast(`Imported: ${r.accepted} accepted, ${r.duplicates} duplicates, ${r.rejected} rejected`);
      load();
    } catch (er) { el.querySelector("#csvResult").innerHTML = `<div class="error-box">${esc(er.message)}</div>`; }
  };
  el.querySelector("#importSample").onclick = async () => {
    const ok = await confirmBox({ title: "Import the synthetic sample?", body: "Imports <span class='mono'>sample_data/SYNTHETIC_operator_events_sample.csv</span> through the normal import path. Records are labelled IMPORTED, appear only under Data → Imported data, and are never mixed with simulated, OCPP or operator-API data. Re-importing is safe (duplicates are skipped).", confirmLabel: "Import sample" });
    if (!ok) return;
    try { const r = await api("/api/integrations/import/sample", { method: "POST" }); el.querySelector("#csvResult").innerHTML = runResult(r); toast(`Sample imported: ${r.accepted} accepted, ${r.duplicates} duplicates, ${r.rejected} rejected`); load(); }
    catch (er) { toast(er.message, true); }
  };

  // Historical Sessions handlers
  const MAX_HIST_BYTES = 15 * 1024 * 1024;

  el.querySelector("#histPreviewBtn").onclick = async () => {
    const fileInput = el.querySelector("#histFile");
    if (!fileInput.files.length) {
      toast("Please select a CSV file first to preview", true);
      return;
    }
    if (fileInput.files[0].size > MAX_HIST_BYTES) {
      toast("File too large (limit 15 MB).", true);
      return;
    }
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    const box = el.querySelector("#histPreviewBox");
    box.innerHTML = '<div class="loading">Validating and generating preview…</div>';
    try {
      const res = await api("/api/integrations/historical/preview", { method: "POST", form: fd });
      renderHistoricalPreview(box, res);
    } catch (er) {
      box.innerHTML = `<div class="error-box">${esc(er.message)}</div>`;
    }
  };

  el.querySelector("#histForm").onsubmit = async (e) => {
    e.preventDefault();
    const fileInput = el.querySelector("#histFile");
    if (fileInput.files.length && fileInput.files[0].size > MAX_HIST_BYTES) {
      toast("File too large (limit 15 MB).", true);
      return;
    }
    const box = el.querySelector("#histResult");
    box.innerHTML = '<div class="loading">Importing records…</div>';
    try {
      const r = await api("/api/integrations/historical/import", { method: "POST", form: new FormData(e.target) });
      box.innerHTML = `
        <div class="note ok">
          <b>Import successful:</b> ${r.accepted} sessions accepted, ${r.duplicates} duplicates skipped, ${r.rejected} rejected.
          <div style="margin-top:6px"><a class="btn-link sm" href="#historical">Explore sessions in the Historical Charging Sessions view →</a></div>
        </div>
      `;
      toast(`Historical import: ${r.accepted} accepted, ${r.duplicates} duplicates`);
      load();
    } catch (er) {
      box.innerHTML = `<div class="error-box">${esc(er.message)}</div>`;
    }
  };

  el.querySelector("#importHistSample").onclick = async () => {
    const box = el.querySelector("#histResult");
    box.innerHTML = '<div class="loading">Fetching sample and importing…</div>';
    try {
      const sampleBlob = await fetch("/api/integrations/samples/charging-records.csv").then(r => r.blob());
      const fd = new FormData();
      fd.append("file", sampleBlob, "IEEE_ChargingRecords_sample.csv");
      fd.append("dataset_label", "IEEE ChargingRecords Sample Dataset");
      const r = await api("/api/integrations/historical/import", { method: "POST", form: fd });
      box.innerHTML = `
        <div class="note ok">
          <b>Sample imported:</b> ${r.accepted} sessions accepted, ${r.duplicates} duplicates skipped.
          <div style="margin-top:6px"><a class="btn-link sm" href="#historical">Explore sessions in the Historical Charging Sessions view →</a></div>
        </div>
      `;
      toast(`IEEE Sample: ${r.accepted} sessions accepted`);
      load();
    } catch (er) {
      box.innerHTML = `<div class="error-box">${esc(er.message)}</div>`;
    }
  };

  // Station Inventory handlers
  el.querySelector("#stationPreviewBtn").onclick = async () => {
    const fileInput = el.querySelector("#stationFile");
    if (!fileInput.files.length) {
      toast("Please select a Station Inventory CSV file first to preview", true);
      return;
    }
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("filter_karnataka", el.querySelector("#filterKarnataka").checked);
    fd.append("filter_bengaluru", el.querySelector("#filterBengaluru").checked);
    const box = el.querySelector("#stationPreviewBox");
    box.innerHTML = '<div class="loading">Validating station records…</div>';
    try {
      const res = await api("/api/integrations/stations/preview", { method: "POST", form: fd });
      renderStationPreview(box, res);
    } catch (er) {
      box.innerHTML = `<div class="error-box">${esc(er.message)}</div>`;
    }
  };

  el.querySelector("#stationForm").onsubmit = async (e) => {
    e.preventDefault();
    const box = el.querySelector("#stationResult");
    box.innerHTML = '<div class="loading">Importing station inventory…</div>';
    try {
      const fd = new FormData(e.target);
      fd.set("filter_karnataka", el.querySelector("#filterKarnataka").checked);
      fd.set("filter_bengaluru", el.querySelector("#filterBengaluru").checked);
      const r = await api("/api/integrations/stations/import", { method: "POST", form: fd });
      box.innerHTML = `
        <div class="note ok">
          <b>Station inventory imported:</b> ${r.accepted} stations accepted, ${r.duplicates} duplicates skipped, ${r.filtered_out || 0} filtered out, ${r.rejected} rejected.
          <div style="margin-top:6px"><a class="btn-link sm" href="#map">View stations on the Map page →</a></div>
        </div>
      `;
      toast(`Inventory imported: ${r.accepted} stations accepted`);
      load();
    } catch (er) {
      box.innerHTML = `<div class="error-box">${esc(er.message)}</div>`;
    }
  };

  el.querySelector("#importStationSample").onclick = async () => {
    const box = el.querySelector("#stationResult");
    box.innerHTML = '<div class="loading">Fetching sample and importing…</div>';
    try {
      const sampleBlob = await fetch("/api/integrations/samples/station-inventory.csv").then(r => r.blob());
      const fd = new FormData();
      fd.append("file", sampleBlob, "Karnataka_Station_Inventory_sample.csv");
      fd.append("dataset_label", "Karnataka EV Station Inventory Sample");
      fd.append("filter_karnataka", true);
      fd.append("filter_bengaluru", false);
      const r = await api("/api/integrations/stations/import", { method: "POST", form: fd });
      box.innerHTML = `
        <div class="note ok">
          <b>Sample imported:</b> ${r.accepted} stations accepted, ${r.duplicates} duplicates skipped.
          <div style="margin-top:6px"><a class="btn-link sm" href="#map">View stations on the Map page →</a></div>
        </div>
      `;
      toast(`Karnataka Station sample: ${r.accepted} stations accepted`);
      load();
    } catch (er) {
      box.innerHTML = `<div class="error-box">${esc(er.message)}</div>`;
    }
  };

  el.querySelector("#opDiagnose").onclick = diagnose;
  el.querySelector("#opTest").onclick = () => call("/api/integrations/operator/test", "Operator API /health OK");
  el.querySelector("#opSync").onclick = () => call("/api/integrations/operator/sync", "Sync complete");
  await load();
}

function renderHistoricalPreview(box, res) {
  box.innerHTML = `
    <div class="card" style="margin-top:10px;background:var(--bg)">
      <h4>Validation Summary: ${esc(res.dataset_type)}</h4>
      <div class="toolbar" style="gap:12px;margin:8px 0">
        <span class="pill st-available">Valid rows: <b>${res.valid_rows}</b></span>
        <span class="pill ${res.duplicate_rows ? "st-unavailable" : "st-available"}">Duplicates: <b>${res.duplicate_rows}</b></span>
        <span class="pill ${res.invalid_rows ? "st-faulted" : "st-available"}">Invalid rows: <b>${res.invalid_rows}</b></span>
        <span class="small muted">Total detected in file: ${res.total_rows}</span>
      </div>
      <div class="small muted" style="margin-bottom:8px">
        Detected columns: <span class="mono">${esc(res.detected_columns.join(", "))}</span>
      </div>
      <div class="tbl-wrap scroll-y" style="max-height:240px">
        <table class="tbl small">
          <thead>
            <tr><th>Line</th><th>ChargerID</th><th>UserID</th><th>Company</th><th>Location</th><th>Type</th><th>Start</th><th>Duration</th><th>Demand</th></tr>
          </thead>
          <tbody>
            ${res.preview.map(r => `
              <tr>
                <td>${r.line}</td><td class="mono"><b>${esc(r.ChargerID)}</b></td><td>${esc(r.UserID || "—")}</td>
                <td>${esc(r.ChargerCompany || "—")}</td><td>${esc(r.Location || "—")}</td>
                <td>${esc(r.ChargerType || "—")}</td><td>${esc(r.StartDatetime || r.StartDay || "—")}</td>
                <td>${esc(r.Duration || "—")}</td><td><b>${esc(r.Demand || "—")}</b></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

function renderStationPreview(box, res) {
  box.innerHTML = `
    <div class="card" style="margin-top:10px;background:var(--bg)">
      <h4>Validation Summary: ${esc(res.dataset_type)}</h4>
      <div class="toolbar" style="gap:12px;margin:8px 0">
        <span class="pill st-available">Valid stations: <b>${res.valid_rows}</b></span>
        <span class="pill ${res.duplicate_rows ? "st-unavailable" : "st-available"}">Duplicates: <b>${res.duplicate_rows}</b></span>
        <span class="pill ${res.invalid_rows ? "st-faulted" : "st-available"}">Invalid rows: <b>${res.invalid_rows}</b></span>
        <span class="pill st-charging">Karnataka: <b>${res.karnataka_stations_detected}</b></span>
        <span class="pill st-available">Bengaluru: <b>${res.bengaluru_stations_detected}</b></span>
      </div>
      <div class="small muted" style="margin-bottom:8px">
        Total connectors counted: <b>${res.summary.total_connectors}</b> across <b>${res.summary.unique_operators}</b> operators.
      </div>
      <div class="tbl-wrap scroll-y" style="max-height:240px">
        <table class="tbl small">
          <thead>
            <tr><th>Line</th><th>Operator</th><th>Station Name</th><th>District</th><th>State</th><th>Lat, Lng</th><th>Charger Type</th><th>Connectors</th></tr>
          </thead>
          <tbody>
            ${res.preview.map(r => `
              <tr>
                <td>${r.line}</td><td><b>${esc(r.Operator)}</b></td><td>${esc(r.StationName)}</td>
                <td>${esc(r.District || "—")}</td><td>${esc(r.State || "—")}</td>
                <td class="mono">${r.Latitude.toFixed(4)}, ${r.Longitude.toFixed(4)}</td>
                <td>${esc(r.ChargerType || "Standard")}</td><td class="num">${r.TotalConnectors}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

async function diagnose() {
  const box = root.querySelector("#opResult");
  box.innerHTML = '<div class="loading">Checking…</div>';
  try {
    const d = await api("/api/integrations/operator/diagnose", { method: "POST" });
    const steps = ["missing_base_url", "missing_key", "unreachable", "auth_failed", "incompatible_response", "zero_records", "ok"];
    box.innerHTML = `<div class="note ${d.ok ? (d.state === "ok" ? "ok" : "warn") : "crit"}"><b>${esc(d.state.replace(/_/g, " "))}</b>${d.step ? ` at ${esc(d.step)}` : ""}: ${esc(d.message)}</div>
      <div class="small muted">Checks in order: base URL set (${d.base_url_set ? "yes" : "no"}) → key set (${d.key_set ? "yes" : "no"}) → GET /health → GET /events → schema validation → records present. Stopped at: ${esc(d.state)} (${steps.indexOf(d.state) + 1}/${steps.length}).</div>`;
  } catch (e) { box.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
  load();
}

async function call(url, ok) {
  try { const r = await api(url, { method: "POST" }); toast(ok); root.querySelector("#opResult").innerHTML = r.received !== undefined ? runResult(r) : `<div class="note ok">${esc(JSON.stringify(r))}</div>`; }
  catch (e) { root.querySelector("#opResult").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
  load();
}

function runResult(r) {
  return `<div class="note ${r.ok ? "ok" : "warn"}"><b>Run #${r.id} (${esc(r.connector)})</b>: ${r.accepted} accepted, ${r.duplicates} duplicates, ${r.rejected} rejected. Finished ${dt(r.finished_at)}.
    ${r.errors && r.errors.length ? `<details style="margin-top:4px"><summary>${r.errors.length} error(s)</summary><div class="small mono">${r.errors.map(e => esc(JSON.stringify(e))).join("<br>")}</div></details>` : ""}</div>`;
}

function modeCard(m) {
  if (!m) return "";
  return `<div class="card"><h3><span>${esc(m.mode)}</span><span class="pill ${MODE_STATE_CLASS[m.state] || "st-offline"}">${esc(m.state)}</span></h3>
    <div class="small">${tag(m.type)} ${esc(m.detail || "")}</div>
    <div class="small muted">Chargers: ${m.chargers || 0}${m.last_update ? ` · last update ${dt(m.last_update)}${m.clock ? " (" + esc(m.clock) + ")" : ""}` : " · no data received yet"}</div>
    ${m.next_action ? `<div class="small" style="color:var(--warn)">Next: ${esc(m.next_action)}</div>` : ""}</div>`;
}

let fetching = false;
async function fetchOcm(force) {
  if (fetching) return;
  const btn = root.querySelector("#ocmFetch"), out = root.querySelector("#ocmResult");
  fetching = true; btn.disabled = true; btn.textContent = "Fetching…"; btn.setAttribute("aria-busy", "true");
  out.innerHTML = '<div class="small muted">Requesting Bengaluru listings from Open Charge Map (can take up to ~40 s)…</div>';
  try {
    const res = await api("/api/public-stations/refresh", { method: "POST", body: { providers: ["open_charge_map"], force } });
    if (!ctx.alive()) return;
    const r = res.results.open_charge_map || {};
    if (r.cached) {
      out.innerHTML = `<div class="small">${esc(r.message)}</div>`;
      fetching = false;
      if (await confirmBox({ title: "Fetch again now?", body: "<p>Open Charge Map asks clients to avoid repeated requests. The stored records are already current to within 10 minutes.</p>", confirmLabel: "Fetch anyway" })) {
        btn.textContent = "Fetch Bengaluru listings"; return await fetchOcm(true);
      }
    } else if (r.ok) {
      const n = (k) => `<b class="num">${r[k] ?? 0}</b>`;
      out.innerHTML = `<div class="small" style="color:var(--ok)">Fetched from the Open Charge Map API at ${dt(res.retrieved_at)}.</div>
        <div class="small">Received ${n("received")} · added ${n("inserted")} · updated ${n("updated")} · merged with other providers ${n("duplicates_linked")} · skipped (unparseable) ${n("skipped")} · rejected (failed validation) ${n("rejected")} · stored but not in this refresh ${n("absent_from_refresh")}</div>
        ${r.message ? `<div class="small" style="color:var(--warn)">${esc(r.message)}</div>` : ""}
        ${(r.errors || []).length ? `<details class="small"><summary>${r.errors.length} rejected record(s)</summary>${r.errors.map(e => `<div class="mono">${esc(typeof e === "string" ? e : JSON.stringify(e))}</div>`).join("")}</details>` : ""}`;
      toast(`Open Charge Map: ${r.received} received, ${r.inserted} added, ${r.updated} updated`);
    } else if (r.state === "busy") {
      out.innerHTML = errorBox(r.message);
    } else {
      out.innerHTML = errorBox(`Open Charge Map refresh failed: ${r.message || "unknown error; stored listings were not changed."}`);
    }
  } catch (e) {
    if (ctx.alive()) out.innerHTML = errorBox(e.status === 409 ? e.message : `Refresh request failed: ${e.message} Stored listings were not changed.`);
  } finally {
    fetching = false;
    if (ctx.alive()) { btn.disabled = false; btn.textContent = "Fetch Bengaluru listings"; btn.removeAttribute("aria-busy"); load(); }
  }
}

async function load() {
  try {
    let [health, runs, pub, src, op] = await Promise.all([api("/api/integrations/health"), api("/api/integrations/runs"),
      api("/api/public-stations"), api("/api/sources/status"), api("/api/integrations/operator/status")]);
    const inv = await api("/api/public-stations/inventory").catch(() => null);
    if (!ctx.alive()) return;

    // Display all 7 data source modes
    const modeKeys = [
      "simulation", "simulated_imported", "station_inventory",
      "historical_sessions", "operator_import", "operator_api", "ocpp"
    ];
    root.querySelector("#modes").innerHTML = modeKeys
      .filter(k => src.sources[k])
      .map(k => modeCard(src.sources[k]))
      .join("");

    root.querySelector("#health").innerHTML = health.map(h => `<div class="card"><h3><span>${esc(h.connector.replace(/_/g, " "))}</span><span class="pill ${STATE_CLASS[h.state]}">${STATE_LABEL[h.state] || esc(h.state)}</span></h3>
      <div class="small muted">${esc(h.description)}</div>
      <div class="small">Last check: ${dt(h.last_checked_at)}<br>Last success: ${dt(h.last_success_at)}</div>
      ${h.last_error ? `<div class="small" style="color:var(--crit)">${esc(h.last_error)}</div>` : ""}</div>`).join("");

    root.querySelector("#opStatus").innerHTML = `<div class="small"><span class="pill ${MODE_STATE_CLASS[op.state] || "st-offline"}">${esc(op.state)}</span>
      base URL ${op.base_url_set ? "set" : "<b>missing</b>"} · key ${op.key_set ? "set" : "<b>missing</b>"} · polling ${op.poll_seconds ? `every ${op.poll_seconds} s` : "manual (OPERATOR_API_POLL_SECONDS=0)"}</div>
      <div class="small muted">Last successful sync: ${op.last_success_at ? dt(op.last_success_at) : "never"}${op.stale ? ' · <b style="color:var(--warn)">STALE</b>' : ""} · chargers received: ${op.chargers}</div>
      ${op.last_error ? `<div class="small" style="color:var(--crit)">${esc(op.last_error)}</div>` : ""}
      ${op.next_action ? `<div class="small" style="color:var(--warn)">Next: ${esc(op.next_action)}</div>` : ""}`;

    const o = src.sources.ocpp;
    root.querySelector("#ocpp").innerHTML = `
      <div class="small"><span class="pill ${MODE_STATE_CLASS[o.state] || "st-offline"}">${esc(o.state)}</span> ${esc(o.detail)}</div>
      <div class="small">Endpoint: <span class="mono">${esc(o.endpoint)}</span> · subprotocol <span class="mono">ocpp1.6</span> · last message ${o.last_event ? dt(o.last_event) : "none"}</div>
      <div class="small">Connected now: ${o.connected_now && o.connected_now.length ? o.connected_now.map(x => `<span class="mono">${esc(x.id)}</span>`).join(", ") : "none (as last reported by the central system)"}</div>
      <pre class="formula">Terminal 2 (central system):
  cd backend
  python -m app.integrations.ocpp_central

Terminal 3 (test charge point, SIMULATED OCPP):
  python scripts\\ocpp_simulator.py --id SIMOCPP-001</pre>
      <div class="small muted">The simulator sends Boot → Heartbeat → Available → Start → Charging → MeterValues → Stop → Faulted (GroundFailure). Its vendor is “ChargeSight-Sim”, so its charger and tickets are labelled ${tag("SIMULATED OCPP")}. A real charger that boots against this endpoint appears as ${tag("LIVE OPERATIONAL DATA")}. Select Data → OCPP chargers to see the records.</div>`;

    const ocm = pub.providers.find(x => x.provider === "open_charge_map"), g = pub.providers.find(x => x.provider === "google_places");
    const ocmRec = inv ? (inv.providers.find(x => x.provider === "open_charge_map") || { records: 0 }).records : null;
    const org = (inv && inv.by_origin) || {};
    root.querySelector("#pubBox").innerHTML = `
      <div class="small">${pub.total} listings on the map${ocmRec !== null ? ` · ${ocmRec} Open Charge Map records stored (${org.api || 0} fetched from the API, ${org.snapshot || 0} from the bundled snapshot${inv.absent_from_latest_refresh ? `, ${inv.absent_from_latest_refresh} not in the latest refresh` : ""})` : ""}</div>
      <div class="small">Open Charge Map: <b>${esc(ocm.state_label)}</b>${ocm.last_success_at ? ` (last OK ${dt(ocm.last_success_at)})` : ""}${ocm.last_error ? ` <span style="color:var(--crit)">${esc(ocm.last_error)}</span>` : ""}</div>
      <div class="small">Google Places: <b>${esc(g.state_label)}</b>${g.setup ? ` · ${esc(g.setup)}` : ""}</div>
      <div class="small muted">Public listings are locations only, never live charger status, and are kept separate from operational data. Google Places lookups run from the <a href="#map">Map</a> page.</div>`;

    const btn = root.querySelector("#ocmFetch");
    if (btn && !fetching) { btn.disabled = !ocm.configured; btn.title = ocm.configured ? "" : (ocm.setup || "Set OCM_API_KEY in .env and restart"); }

    runs = runs.filter(r => r.connector !== "ocpp");
    root.querySelector("#runs").innerHTML = runs.length ? `<table class="t"><thead><tr><th>#</th><th>Connector</th><th>Source</th><th>Dataset</th><th>Started</th><th class="r">Received</th><th class="r">Accepted</th><th class="r">Duplicates</th><th class="r">Rejected</th></tr></thead><tbody>
      ${runs.map(r => `<tr><td>${r.id}</td><td>${esc(r.connector)}</td><td>${tag(r.source === "operator_import" ? "IMPORTED" : "LIVE")}</td><td class="small">${esc(r.dataset_label)}</td><td class="small">${dt(r.started_at)}</td><td class="r num">${r.received}</td><td class="r num">${r.accepted}</td><td class="r num">${r.duplicates}</td><td class="r num">${r.rejected}</td></tr>`).join("")}
      </tbody></table>` : '<div class="empty">No data has been imported or received yet.</div>';
  } catch (e) { root.querySelector("#health").innerHTML = errorBox(e.message); }
}
