import { api } from "../api.js";
import { esc, download } from "../ui.js";

export async function render(el, ctx) {
  const reports = await api("/api/reports");
  const src = ctx.state.source;
  el.innerHTML = `
    <div class="note">Every CSV begins with lines starting with <span class="mono">#</span> that state the data source, generation time, time range, definitions and assumptions, followed by the table. Values are computed by the same functions the screens use. Fleet, ticket, priority and event reports use the data source selected at the top (now: <b>${esc(src)}</b>).</div>
    <section class="panel"><div class="panel-h"><h2>Exports</h2></div><div class="panel-b flush tbl-wrap">
      <table class="t"><thead><tr><th>Report</th><th>Format</th><th></th></tr></thead><tbody>
      ${reports.map(r => `<tr><td><b>${esc(r.title)}</b></td><td>${r.csv ? "CSV" : "HTML"}</td><td>${r.csv
        ? `<button class="sm" data-dl="${esc(r.csv)}${["fleet", "tickets", "priority", "events"].includes(r.name) ? `?source=${src}` : ""}">Download</button>`
        : `<a href="${esc(r.html)}" target="_blank" rel="noopener"><button class="sm primary">Open report</button></a>`}</td></tr>`).join("")}
      </tbody></table></div></section>`;
  el.querySelectorAll("[data-dl]").forEach(b => b.onclick = () => download(b.dataset.dl));
}
