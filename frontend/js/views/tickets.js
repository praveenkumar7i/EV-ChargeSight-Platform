import { api } from "../api.js";
import { esc, fmtMin, dt, ago, tag, ticketPill, sev, scoreBar, factorLegend, errorBox, download, debounce } from "../ui.js";
import { openTicket } from "../drawers.js";
import { openRaiseTicket } from "../raise_ticket.js";

let root, ctx, busy = false;
const f = { status: "", severity: "", site: "", overdue: "", category: "", technician: "", origin: "", dispatch_needed: "", q: "", sort: "priority", view: "open" };

export async function render(el, c) {
  root = el; ctx = c;
  const [sites, cat, techs] = await Promise.all([api(`/api/sites?source=${c.state.source}`), api("/api/catalog"), api("/api/technicians")]);
  el.innerHTML = `
    <section class="kpis" id="tkpis"></section>
    <section class="panel">
      <div class="panel-h"><h2>Tickets ${tag(c.state.source === "simulation" ? "SIMULATED" : c.state.source === "operator_import" ? "IMPORTED" : "LIVE")}</h2>
        <div class="toolbar">
          <div class="seg" role="group" aria-label="Scope"><button data-view="open">Open, by priority</button><button data-view="all">All tickets</button></div>
          <select data-k="status" aria-label="Status"><option value="">Any status</option>${cat.workflow.map(s => `<option value="${s}">${cat.workflow_labels[s]}</option>`).join("")}</select>
          <select data-k="severity" aria-label="Severity"><option value="">Any severity</option><option>critical</option><option>major</option><option>minor</option></select>
          <select data-k="category" aria-label="Fault"><option value="">Any fault</option>${cat.fault_types.map(t => `<option value="${t.key}">${esc(t.label)}</option>`).join("")}</select>
          <select data-k="site" aria-label="Site"><option value="">All sites</option>${sites.map(s => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join("")}</select>
          <select data-k="overdue" aria-label="Overdue"><option value="">Overdue or not</option><option value="true">Overdue only</option></select>
          <select data-k="technician" aria-label="Technician"><option value="">Any technician</option>${techs.map(t => `<option value="${esc(t.id)}">${esc(t.name)}${t.is_demo ? " (DEMO)" : ""}</option>`).join("")}</select>
          <select data-k="origin" aria-label="Origin"><option value="">Any origin</option><option value="user_reported">User reported</option><option value="simulator">Simulator</option><option value="ocpp">OCPP</option><option value="operator_api">Operator API</option><option value="imported_data">Imported data</option></select>
          <select data-k="dispatch_needed" aria-label="Dispatch needed"><option value="">Dispatch: any</option><option value="true">Dispatch needed only</option></select>
          <select data-k="sort" aria-label="Sort"><option value="priority">Sort: priority</option><option value="severity">Sort: severity</option><option value="age">Sort: age (oldest first)</option><option value="status">Sort: workflow status</option><option value="newest">Sort: newest</option></select>
          <input type="text" data-k="q" placeholder="Search ticket, charger, site" aria-label="Search tickets" style="width:180px">
          <button id="exportT">Export CSV</button>
          <button class="primary" id="raiseT">Raise a ticket</button>
        </div></div>
      <div class="panel-b flush tbl-wrap" id="tlist"></div>
      <div class="panel-b">${factorLegend()}<div class="small muted" style="margin-top:6px">Recommended actions are advisory: ChargeSight records workflow steps but does not physically dispatch technicians or contact anyone. Overdue = past the SLA deadline for its severity (ASSUMPTION: critical 4 h, major 12 h, minor 48 h); an overdue ticket is escalated once. Origin shows how a ticket was raised (USER_REPORTED is a report, not telemetry). MTTA = mean(assigned − detected); MTTR = mean(resolved − detected).</div></div>
    </section>`;
  el.querySelectorAll("[data-k]").forEach(s => {
    s.value = f[s.dataset.k];
    if (s.tagName === "INPUT") s.oninput = debounce(() => { f.q = s.value; refresh(); }, 300);
    else s.onchange = () => { f[s.dataset.k] = s.value; refresh(); };
  });
  el.querySelectorAll("[data-view]").forEach(b => b.onclick = () => { f.view = b.dataset.view; refresh(); });
  el.querySelector("#exportT").onclick = () => download(`/api/reports/tickets.csv?source=${c.state.source}`);
  el.querySelector("#raiseT").onclick = () => openRaiseTicket({ onDone: () => refresh() });
  await refresh();
}

export async function refresh() {
  if (busy || !ctx.alive()) return;
  busy = true;
  root.querySelectorAll("[data-view]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.view === f.view)));
  const p = new URLSearchParams({ source: ctx.state.source });
  ["status", "severity", "site", "overdue", "category", "technician", "origin", "dispatch_needed", "q", "sort"].forEach(k => f[k] && p.set(k, f[k]));
  if (f.view === "open") p.set("open_only", "true");
  try {
    const [rows, m] = await Promise.all([api(`/api/tickets?${p}`), api(`/api/tickets/metrics?source=${ctx.state.source}`)]);
    if (!ctx.alive()) return;
    const now = ctx.state.source === "simulation" ? ctx.state.sim?.sim_time : new Date().toISOString();
    root.querySelector("#tkpis").innerHTML = [
      ["Open", m.open, ""], ["Critical unresolved", m.unresolved_high_severity, m.unresolved_high_severity ? "crit" : ""],
      ["Dispatch needed", m.dispatch_needed, m.dispatch_needed ? "crit" : ""], ["In progress", m.in_progress, ""],
      ["Overdue", m.overdue, m.overdue ? "warn" : ""], ["Escalated", m.escalated, m.escalated ? "warn" : ""], ["Resolved", m.resolved, "ok"],
      ["MTTA", fmtMin(m.mtta_min), ""], ["MTTR", fmtMin(m.mttr_min), ""],
      ["Remote resets", m.resolved_by.remote_reset, ""], ["Reconnects", m.resolved_by.reconnect, ""],
    ].map(([l, v, c2]) => `<div class="kpi"><span class="lbl">${l}</span><span class="val ${c2}">${esc(v)}</span></div>`).join("");
    root.querySelector("#tlist").innerHTML = rows.length ? `<table class="t"><thead><tr><th>Rank</th><th>Ticket</th><th>Charger / site</th><th>Fault</th><th>Priority score</th><th>Status</th><th>Technician</th><th>SLA</th><th>Age</th><th>Recommended next action</th></tr></thead><tbody>
      ${rows.map(t => `<tr class="click" data-code="${esc(t.code)}" tabindex="0"><td class="num">${t.priority ? t.priority.rank : "—"}</td><td class="mono"><b>${esc(t.code)}</b>${t.overdue ? ' <span class="tag tag-ESTIMATED">OVERDUE</span>' : ""}<div>${tag(t.origin_label)}</div></td><td><div class="mono small">${esc(t.charger_id)}</div><div class="small muted">${esc(t.site_name)}</div></td><td>${esc(t.fault_label)}<div class="small">${sev(t.severity)}${t.severity_overridden ? ' <span class="muted">(override)</span>' : ""}</div></td><td>${scoreBar(t.priority)}</td><td>${ticketPill(t.status)}</td><td class="small">${t.technician_name ? esc(t.technician_name) : t.dispatch_needed ? '<b style="color:var(--crit)">dispatch needed</b>' : "—"}</td><td class="small num" style="white-space:nowrap">${t.status === "resolved" || t.status === "closed" ? '<span class="muted">—</span>' : t.overdue_min ? `<span class="sla-over">+${fmtMin(t.overdue_min)}</span>` : t.sla_remaining_min !== null ? `${fmtMin(t.sla_remaining_min)} left` : "—"}</td><td class="small num">${t.status === "resolved" || t.status === "closed" ? `<span class="muted">took ${fmtMin(t.age_min)}</span>` : fmtMin(t.age_min)}</td><td class="small">${esc(t.recommended_action)}</td></tr>`).join("")}
      </tbody></table><div class="pager">${rows.length} ticket${rows.length === 1 ? "" : "s"}</div>` : `<div class="empty">${f.view === "open" && !f.q ? "No open tickets." : "No tickets match these filters."}</div>`;
    root.querySelectorAll("tr[data-code]").forEach(tr => {
      const go = () => openTicket(tr.dataset.code, refresh);
      tr.onclick = go; tr.onkeydown = e => { if (e.key === "Enter") go(); };
    });
  } catch (e) {
    root.querySelector("#tlist").innerHTML = errorBox(e.message);
    root.querySelector("[data-retry]")?.addEventListener("click", refresh);
  } finally { busy = false; }
}
