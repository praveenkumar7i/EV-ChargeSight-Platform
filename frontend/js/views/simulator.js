import { api } from "../api.js";
import { esc, fmt, dt, tag, toast, errorBox, download, confirmBox, debounce } from "../ui.js";

let root, ctx, busy = false;
const tf = { site: "", charger: "", type: "", severity: "", q: "", include_chatter: false, operational: false, offset: 0, limit: 50 };

export async function render(el, c) {
  root = el; ctx = c;
  const [st, cat, chargers, sites] = await Promise.all([api("/api/sim/state"), api("/api/catalog"), api("/api/chargers?source=simulation"), api("/api/sites?source=simulation")]);
  const p = st.params;
  el.innerHTML = `
    <div class="note">${tag("SIMULATED")} This simulator is an internal event generator that emits OCPP 1.6-style messages. It does not open an OCPP connection. Runs are reproducible: the same seed and parameters always produce the same events, unless you intervene manually.</div>
    <div class="grid-2e">
      <section class="panel"><div class="panel-h"><h2>Controls</h2><span class="hint" id="simInfo"></span></div>
        <div class="panel-b stack" style="gap:12px">
          <div class="toolbar">
            <button class="primary" id="btnStart">Start</button><button id="btnPause">Pause</button>
            <label class="field" style="width:110px"><span>Step</span><input type="number" id="stepN" value="40" min="1" max="5000"></label><button id="btnStep">Advance</button>
          </div>
          <label class="field"><span>Speed: <b id="spdLbl">${st.speed}</b> steps per tick (${st.step_seconds} s simulated per step, tick ${st.tick_ms} ms)</span><input type="range" id="speed" min="1" max="60" value="${st.speed}"></label>
          <div class="toolbar"><label class="field" style="width:140px"><span>Seed</span><input type="number" id="seed" value="${st.seed}" min="0"></label>
            <button class="danger" id="btnReset">Reset simulation</button></div>
          <div class="small muted">Reset deletes all simulated events, tickets and telemetry history and returns the 40 chargers to Available at 08:00 IST (an empty run). To get the populated demo back, use “Reset / restore demo” on the Operations console. Imported and live data are never touched.</div>
        </div></section>
      <section class="panel"><div class="panel-h"><h2>Inject a fault</h2><span class="hint">Demonstration control</span></div>
        <div class="panel-b stack" style="gap:10px">
          <div class="form-grid">
            <label class="field"><span>Charger</span><select id="injCharger">${chargers.map(c => `<option value="${esc(c.id)}">${esc(c.id)} · ${esc(c.status)}</option>`).join("")}</select></label>
            <label class="field"><span>Fault</span><select id="injCat">${cat.fault_types.map(f => `<option value="${f.key}">${esc(f.label)} — ${f.severity}</option>`).join("")}</select></label>
            <label class="field"><span>Severity</span><select id="injSev"><option value="">catalogue default</option><option value="critical">critical</option><option value="major">major</option><option value="minor">minor</option></select></label>
            <label class="field"><span>Note (optional)</span><input type="text" id="injNote" maxlength="200" placeholder="e.g. demo for viva"></label>
          </div>
          <div><button class="danger" id="btnInject">Inject fault</button></div>
          <div class="small muted">A SIMULATED ticket is created at once. Communication loss marks the charger offline immediately. A severity override is stored on the ticket and used by the priority engine. A second unresolved fault of the same type on the same charger is deduplicated (no duplicate ticket).</div>
        </div></section>
    </div>
    <section class="panel"><div class="panel-h"><h2>Parameters ${tag("ASSUMPTION")}</h2><span class="hint">Demo-accelerated defaults; applied from the next step</span></div>
      <form class="panel-b stack" id="paramForm" style="gap:12px">
        <div class="form-grid">
          <label class="field"><span>Faults per charger per day</span><input type="number" step="0.01" min="0" name="fault_rate_per_charger_day" value="${p.fault_rate_per_charger_day}"></label>
          <label class="field"><span>Session start prob. per step</span><input type="number" step="0.001" min="0" name="session_start_prob_per_step" value="${p.session_start_prob_per_step}"></label>
          <label class="field"><span>Mean session (min)</span><input type="number" step="1" min="1" name="mean_session_min" value="${p.mean_session_min}"></label>
          <label class="field"><span>Reconnect prob. per step</span><input type="number" step="0.001" min="0" name="reconnect_prob_per_step" value="${p.reconnect_prob_per_step}"></label>
          <label class="field"><span>Remote reset delay (min)</span><input type="number" step="1" min="0" name="reset_delay_min" value="${p.reset_delay_min}"></label>
          <label class="field"><span>Heartbeat interval (s)</span><input type="number" step="30" min="30" name="heartbeat_interval_s" value="${p.heartbeat_interval_s}"></label>
        </div>
        <div class="form-grid">${Object.entries(p.category_mix).map(([k, v]) => `<label class="field"><span>Share: ${esc(cat.fault_types.find(f => f.key === k)?.label || k)}</span><input type="number" step="0.01" min="0" data-mix="${k}" value="${v}"></label>`).join("")}</div>
        <div class="toolbar">
          <label class="check"><input type="checkbox" name="auto_dispatch" ${p.auto_dispatch ? "checked" : ""}> Auto-dispatch technicians</label>
          <select name="auto_dispatch_policy" aria-label="Auto-dispatch policy"><option value="priority" ${p.auto_dispatch_policy === "priority" ? "selected" : ""}>by priority score</option><option value="fifo" ${p.auto_dispatch_policy === "fifo" ? "selected" : ""}>first come, first served</option></select>
          <button class="primary" type="submit">Apply parameters</button>
        </div>
        <div class="small muted">With auto-dispatch off, you move tickets through the workflow yourself. With it on, free technicians take the next ticket and travel/repair using the ASSUMPTION estimates.</div>
      </form></section>
    <section class="panel" id="impSimPanel"><div class="panel-h"><h2>Simulation on Imported Stations ${tag("SIMULATED")}</h2><span class="hint" id="impSimInfo"></span></div>
      <div class="panel-b stack" style="gap:12px">
        <div class="small muted">Run a realistic, reproducible simulation scenario on your imported station inventory. Eligible imported stations will be mapped into simulated operational chargers emitting availability, telemetry, and potential fault events that feed into the maintenance ticket workflow. Resetting removes only the simulated run; imported inventory is never deleted.</div>
        <div class="toolbar" style="align-items:flex-end">
          <label class="field" style="width:130px"><span>Random Seed</span><input type="number" id="impSimSeed" value="42" min="0"></label>
          <label class="field" style="width:130px"><span>Station Limit</span><input type="number" id="impSimLimit" value="15" min="1" max="50"></label>
          <label class="field" style="width:140px"><span>Fault Probability</span><input type="number" id="impSimProb" value="0.35" min="0" max="1" step="0.05"></label>
          <button class="primary" id="btnImpSimStart">Run Simulation on Imported Stations</button>
          <button class="danger" id="btnImpSimReset">Reset Imported Simulation</button>
        </div>
        <div id="impSimDetails" class="box small" style="background:var(--bg-card);border:1px solid var(--border);padding:10px;border-radius:6px;">Checking imported station inventory status...</div>
      </div></section>
    <section class="panel"><div class="panel-h"><h2>Event timeline ${tag("SIMULATED")}</h2><span class="hint" id="tlInfo"></span></div>
      <div class="panel-b" style="padding-bottom:6px"><div class="toolbar">
        <select data-t="site" aria-label="Site"><option value="">All sites</option>${sites.map(x => `<option value="${esc(x.code)}">${esc(x.name)}</option>`).join("")}</select>
        <select data-t="charger" aria-label="Charger"><option value="">All chargers</option>${chargers.map(x => `<option>${esc(x.id)}</option>`).join("")}</select>
        <select data-t="type" aria-label="Type"><option value="">All types</option><option value="ticket">ticket lifecycle</option>${["fault", "offline", "reconnect", "reset_attempt", "reset_result", "fault_resolved", "boot", "session_start", "session_stop", "status_available", "heartbeat", "meter_values"].map(t => `<option>${t}</option>`).join("")}</select>
        <select data-t="severity" aria-label="Severity"><option value="">Any severity</option><option>critical</option><option>major</option><option>minor</option><option>info</option></select>
        <input type="text" data-t="q" placeholder="Search charger, ticket, note" aria-label="Search" style="width:190px">
        <label class="check"><input type="checkbox" id="opOnly"> Faults &amp; tickets only</label>
        <label class="check"><input type="checkbox" id="chatter"> Include heartbeats &amp; meter values</label>
        <button id="exportEv">Export events CSV</button>
      </div></div>
      <div class="tbl-wrap"><table class="t"><thead><tr><th>Time (IST)</th><th>Site</th><th>Charger</th><th>Type</th><th>Severity</th><th>Source</th><th>Description</th></tr></thead><tbody id="evlog"></tbody></table></div>
      <div class="pager"><span id="pgInfo"></span><button class="sm" id="pgPrev">← Newer</button><button class="sm" id="pgNext">Older →</button>
        <select id="pgSize" aria-label="Rows per page">${[25, 50, 100, 200].map(n => `<option ${n === tf.limit ? "selected" : ""}>${n}</option>`).join("")}</select></div></section>`;
  const $ = (s) => el.querySelector(s);
  $("#btnStart").onclick = () => act("/api/sim/start", "Simulation running");
  $("#btnPause").onclick = () => act("/api/sim/pause", "Simulation paused");
  $("#btnStep").onclick = () => act("/api/sim/step", "Advanced", { n: Number($("#stepN").value) || 1 });
  $("#btnReset").onclick = async () => {
    const ok = await confirmBox({
      title: "Reset the simulation to an empty run?", danger: true, confirmLabel: "Reset simulation",
      body: `<ul><li>Deletes all SIMULATED events, tickets and telemetry history.</li><li>All 40 chargers return to Available at 08:00 IST; technicians back on shift; active scenario cleared.</li><li>Imported and live data are not touched.</li><li>The demo dataset is <b>not</b> rebuilt; use “Reset / restore demo” on the console for that.</li></ul>`
    });
    if (!ok) return;
    await act("/api/sim/reset", "Simulation reset", { seed: Number($("#seed").value) });
    ctx.rerender();
  };

  $("#btnImpSimStart").onclick = async () => {
    const seed = Number($("#impSimSeed").value) || 42;
    const stations_limit = Number($("#impSimLimit").value) || 15;
    const fault_probability = Number($("#impSimProb").value) || 0.35;
    try {
      const res = await api("/api/sim/imported/start", {
        method: "POST",
        body: { seed, stations_limit, fault_probability }
      });
      toast(`Simulated ${res.simulated_chargers} chargers on ${res.simulated_stations} imported stations (${res.faults_generated} faults opened)`);
      refresh();
    } catch (e) {
      toast(e.message, true);
    }
  };

  $("#btnImpSimReset").onclick = async () => {
    const ok = await confirmBox({
      title: "Reset Simulation on Imported Stations?",
      danger: true,
      confirmLabel: "Reset Imported Simulation",
      body: `<ul><li>Deletes only simulated chargers, tickets, and events generated from imported stations.</li><li><b>Imported station inventory records are strictly preserved</b> and never deleted.</li></ul>`
    });
    if (!ok) return;
    try {
      const res = await api("/api/sim/imported/reset", { method: "POST" });
      toast(`Imported simulation reset: ${res.removed_chargers} chargers removed. Original inventory preserved.`);
      refresh();
    } catch (e) {
      toast(e.message, true);
    }
  };
  $("#speed").oninput = (e) => { $("#spdLbl").textContent = e.target.value; };
  $("#speed").onchange = (e) => api("/api/sim/config", { method: "PATCH", body: { speed: Number(e.target.value) } }).then(() => toast("Speed updated")).catch(er => toast(er.message, true));
  $("#btnInject").onclick = async () => {
    try {
      const body = { charger_id: $("#injCharger").value, category: $("#injCat").value, severity: $("#injSev").value || null, note: $("#injNote").value || null };
      const r = await api("/api/sim/inject", { method: "POST", body });
      toast(r.deduplicated ? `Not created: ${r.message}` : `SIMULATED fault injected — ${r.ticket} opened (${r.severity}).`, r.deduplicated);
      refresh();
    } catch (e) { toast(e.message, true); }
  };
  $("#paramForm").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const params = {};
    ["fault_rate_per_charger_day", "session_start_prob_per_step", "mean_session_min", "reconnect_prob_per_step", "reset_delay_min", "heartbeat_interval_s"].forEach(k => params[k] = Number(fd.get(k)));
    params.auto_dispatch = fd.get("auto_dispatch") === "on";
    params.auto_dispatch_policy = fd.get("auto_dispatch_policy");
    params.category_mix = {};
    e.target.querySelectorAll("[data-mix]").forEach(i => params.category_mix[i.dataset.mix] = Number(i.value));
    try { await api("/api/sim/config", { method: "PATCH", body: { params } }); toast("Parameters applied"); }
    catch (er) { toast(er.message, true); }
  };
  el.querySelectorAll("[data-t]").forEach(i => {
    i.value = tf[i.dataset.t];
    const h = () => { tf[i.dataset.t] = i.value; tf.offset = 0; refresh(); };
    i.addEventListener(i.tagName === "INPUT" ? "input" : "change", i.tagName === "INPUT" ? debounce(h, 300) : h);
  });
  $("#opOnly").checked = tf.operational;
  $("#opOnly").onchange = (e) => { tf.operational = e.target.checked; tf.offset = 0; refresh(); };
  $("#chatter").checked = tf.include_chatter;
  $("#chatter").onchange = (e) => { tf.include_chatter = e.target.checked; tf.offset = 0; refresh(); };
  $("#pgPrev").onclick = () => { tf.offset = Math.max(0, tf.offset - tf.limit); refresh(); };
  $("#pgNext").onclick = () => { tf.offset += tf.limit; refresh(); };
  $("#pgSize").onchange = (e) => { tf.limit = Number(e.target.value); tf.offset = 0; refresh(); };
  $("#exportEv").onclick = () => download("/api/reports/events.csv?source=simulation");
  await refresh();
}

async function act(url, msg, body) {
  try { await api(url, { method: "POST", body: body || {} }); toast(msg); refresh(); }
  catch (e) { toast(e.message, true); }
}

export async function refresh() {
  if (busy || !ctx.alive()) return;
  busy = true;
  try {
    const q = new URLSearchParams({ source: "simulation", limit: String(tf.limit), offset: String(tf.offset) });
    ["site", "charger", "type", "severity", "q"].forEach(k => tf[k] && q.set(k, tf[k]));
    if (tf.include_chatter) q.set("include_chatter", "true");
    if (tf.operational) q.set("operational", "true");
    const [st, tl, impSt] = await Promise.all([
      api("/api/sim/state"),
      api(`/api/timeline?${q}`),
      api("/api/sim/imported/status").catch(() => null)
    ]);
    if (!ctx.alive()) return;
    root.querySelector("#simInfo").textContent = `${st.running ? "Running" : "Paused"} · step ${st.step} · ${dt(st.sim_time)} · ${st.events_total.toLocaleString("en-IN")} events stored`;
    root.querySelector("#tlInfo").textContent = tl.note || "";

    const impInfo = root.querySelector("#impSimInfo");
    const impBox = root.querySelector("#impSimDetails");
    const impBtn = root.querySelector("#btnImpSimStart");
    if (impSt && impBox && impInfo) {
      if (!impSt.has_imported_stations) {
        impInfo.textContent = "No imported stations";
        impBox.innerHTML = `<strong>No imported station inventory found.</strong> Go to <a href="#integrations">Data integrations</a> and import a Station Inventory CSV (e.g. Karnataka / Bengaluru stations) before running simulation.`;
        if (impBtn) impBtn.disabled = true;
      } else if (impSt.is_simulated) {
        impInfo.textContent = `Active: ${impSt.simulated_chargers_count} chargers, ${impSt.simulated_tickets_count} tickets`;
        impBox.innerHTML = `<div><strong>Simulation active on imported stations:</strong> ${impSt.simulated_chargers_count} simulated chargers generated across ${impSt.total_imported_stations} stations. <b>${impSt.simulated_tickets_count} maintenance tickets</b> currently open, with ${impSt.simulated_events_count} simulated events recorded. Switch your top data filter to <code>Simulated imported stations</code> or view them on the <a href="#tickets">Maintenance Tickets</a> and <a href="#map">Map</a> views.</div>`;
        if (impBtn) impBtn.disabled = false;
      } else {
        impInfo.textContent = `${impSt.total_imported_stations} stations ready`;
        impBox.innerHTML = `<div><strong>${impSt.total_imported_stations} imported stations available for simulation.</strong> Click <em>Run Simulation on Imported Stations</em> to provision simulated operational chargers and synthesize realistic charging, telemetry, and fault tickets.</div>`;
        if (impBtn) impBtn.disabled = false;
      }
    }
    root.querySelector("#evlog").innerHTML = tl.items.length ? tl.items.map(i => `<tr><td class="small num" style="white-space:nowrap">${dt(i.ts)}</td><td class="small">${esc(i.site_code || "")}</td><td class="mono small" style="white-space:nowrap">${esc(i.charger_id)}</td>
      <td class="small">${esc(i.kind === "ticket" ? "ticket " + (i.status_after || "") : i.type)}${i.ocpp_action ? `<div class="muted">${esc(i.ocpp_action)}</div>` : ""}</td>
      <td><span class="sv ${esc(i.severity)}">${esc(i.severity)}</span></td><td>${tag(i.label)}</td><td class="small">${esc(i.description)}</td></tr>`).join("")
      : `<tr><td colspan="7"><div class="empty">${tf.q || tf.site || tf.charger || tf.type || tf.severity ? "No events match these filters." : "No events yet. Press Start or Advance."}</div></td></tr>`;
    const to = Math.min(tf.offset + tl.items.length, tl.total);
    root.querySelector("#pgInfo").textContent = tl.total ? `${tf.offset + 1}–${to} of ${tl.total.toLocaleString("en-IN")}` : "";
    root.querySelector("#pgPrev").disabled = tf.offset === 0;
    root.querySelector("#pgNext").disabled = to >= tl.total;
  } catch (e) {
    root.querySelector("#evlog").innerHTML = `<tr><td colspan="7">${errorBox(e.message)}</td></tr>`;
  } finally { busy = false; }
}
