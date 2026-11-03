// Shared UI helpers: escaping, formatting, provenance tags, pills, toasts, drawer, SVG charts.

export const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function fmt(v, d = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
}
export const fmtInt = (v) => (v === null || v === undefined ? "—" : Math.round(v).toLocaleString("en-IN"));
export const fmtINR = (v) => (v === null || v === undefined ? "—" : "₹" + Math.round(v).toLocaleString("en-IN"));
export const fmtPct = (v, d = 1) => (v === null || v === undefined ? "—" : `${fmt(v, d)}%`);
export function fmtMin(m) {
  if (m === null || m === undefined) return "—";
  if (m < 60) return `${fmt(m, m < 10 ? 1 : 0)} min`;
  const h = Math.floor(m / 60), r = Math.round(m % 60);
  return h >= 24 ? `${fmt(m / 1440, 1)} d` : `${h}h ${r}m`;
}
export function dt(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) + " IST";
}
export function timeOnly(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
export function ago(iso, nowIso) {
  if (!iso || !nowIso) return "—";
  const m = (new Date(nowIso) - new Date(iso)) / 60000;
  return m < 1 ? "just now" : fmtMin(m) + " ago";
}

const TAG_CLASS = { "SIMULATED": "SIMULATED", "SIMULATED OCPP": "SIMULATED", "LIVE": "LIVE", "IMPORTED": "IMPORTED", "ESTIMATED": "ESTIMATED",
  "ASSUMPTION": "ASSUMPTION", "NOT YET VALIDATED": "NYV", "PUBLIC LISTING": "PUBLIC", "LIVE OPERATIONAL DATA": "LIVE",
  "UNKNOWN": "NYV", "USER-ENTERED": "USER", "MISSING": "MISSING", "VERIFIED": "VERIFIED", "SOURCE DATA": "VERIFIED", "CANDIDATE": "ASSUMPTION",
  "USER_REPORTED": "USER", "SIMULATOR": "SIMULATED", "OCPP": "LIVE", "OPERATOR_API": "LIVE", "IMPORTED_DATA": "IMPORTED", "DEMO": "NYV" };
export const tag = (label) => `<span class="tag tag-${TAG_CLASS[label] || "NYV"}">${esc(label)}</span>`;

export const STATUS_LABEL = { available: "Available", charging: "Charging", faulted: "Faulted", offline: "Offline", unavailable: "Unavailable" };
export const statusPill = (s) => `<span class="pill st-${esc(s)}">${esc(STATUS_LABEL[s] || s)}</span>`;
export const TK_LABEL = { open: "Open", assigned: "Assigned", en_route: "En Route", on_site: "On Site", waiting_parts: "Waiting for parts",
  waiting_access: "Waiting for access", resolved: "Resolved", closed: "Closed" };
export const ticketPill = (s) => `<span class="pill tk-${esc(s)}">${esc(TK_LABEL[s] || s)}</span>`;
export const sev = (s) => `<span class="sev-${esc(s)}">${esc(s)}</span>`;

export function scoreBar(p) {
  if (!p) return '<span class="muted">—</span>';
  const segs = p.factors.map(f => `<span class="f-${f.key}" style="width:${Math.max(0, f.contribution * 100)}%" title="${esc(f.label)} ${f.contribution.toFixed(2)}"></span>`).join("");
  return `<div style="display:flex;gap:8px;align-items:center"><div class="scorebar" role="img" aria-label="score ${p.score.toFixed(2)}">${segs}</div><b class="num">${p.score.toFixed(2)}</b>${p.escalated ? ' <span class="tag tag-ESTIMATED" title="Escalated after the starvation cap">ESC</span>' : ""}</div>`;
}
export const factorLegend = () => `<div class="legend"><span><i class="f-severity"></i>Severity S</span><span><i class="f-utilisation"></i>Utilisation U</span><span><i class="f-capacity"></i>Capacity R</span><span><i class="f-age"></i>Age A</span></div>`;

export function toast(msg, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), err ? 7000 : 3500);
}

export function errorBox(msg, retryLabel = "Retry") {
  return `<div class="error-box"><span>${esc(msg)}</span><button class="sm" data-retry>${esc(retryLabel)}</button></div>`;
}

let drawerClose = null;
export function openDrawer(html, onClose) {
  closeDrawer();
  const root = document.getElementById("drawerRoot");
  root.innerHTML = `<div class="drawer-back" data-close></div><aside class="drawer" role="dialog" aria-modal="true">${html}</aside>`;
  drawerClose = onClose || null;
  root.querySelectorAll("[data-close]").forEach(el => el.addEventListener("click", closeDrawer));
  const first = root.querySelector(".drawer button, .drawer select, .drawer input");
  first && first.focus();
  return root.querySelector(".drawer");
}
export function closeDrawer() {
  const root = document.getElementById("drawerRoot");
  if (!root.innerHTML) return;
  root.innerHTML = "";
  const cb = drawerClose; drawerClose = null;
  cb && cb();
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

export const drawerOpen = () => !!document.getElementById("drawerRoot").innerHTML;

export function download(url) {
  const a = document.createElement("a");
  a.href = url; a.download = ""; document.body.appendChild(a); a.click(); a.remove();
}

// ---------------- SVG charts ----------------
function niceTicks(min, max, n = 5) {
  if (min === max) { max = min + 1; }
  const span = max - min;
  const step0 = Math.pow(10, Math.floor(Math.log10(span / n)));
  const err = (span / n) / step0;
  const step = step0 * (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1);
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

// series: [{name, color, values:[numbers|null]}], labels: x labels
export function lineChart({ series, labels, height = 260, yFmt = (v) => fmt(v, 0), zeroLine = false, area = false }) {
  const W = 1000, H = height, pl = 64, pr = 16, pt = 14, pb = 28;
  const all = series.flatMap(s => s.values.filter(v => v !== null && v !== undefined));
  if (!all.length) return '<div class="empty">No data to chart.</div>';
  let min = Math.min(...all), max = Math.max(...all);
  if (zeroLine) { min = Math.min(min, 0); max = Math.max(max, 0); }
  const ticks = niceTicks(min, max);
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const n = labels.length;
  const X = (i) => pl + (n <= 1 ? 0 : i * (W - pl - pr) / (n - 1));
  const Y = (v) => pt + (y1 - v) * (H - pt - pb) / (y1 - y0 || 1);
  let g = "";
  ticks.forEach(t => {
    g += `<line x1="${pl}" x2="${W - pr}" y1="${Y(t)}" y2="${Y(t)}" stroke="var(--line)" stroke-width="1"/>`;
    g += `<text x="${pl - 8}" y="${Y(t) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${esc(yFmt(t))}</text>`;
  });
  if (zeroLine && y0 < 0 && y1 > 0) g += `<line x1="${pl}" x2="${W - pr}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--line-strong)" stroke-width="1.5"/>`;
  const every = Math.max(1, Math.ceil(n / 8));
  labels.forEach((l, i) => {
    if (i % every === 0 || i === n - 1) g += `<text x="${X(i)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="var(--muted)">${esc(l)}</text>`;
  });
  series.forEach(s => {
    let d = "", started = false;
    s.values.forEach((v, i) => {
      if (v === null || v === undefined) { started = false; return; }
      d += `${started ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`; started = true;
    });
    if (area && s.values.every(v => v !== null)) {
      g += `<path d="${d}L${X(n - 1)} ${Y(Math.max(y0, 0))}L${X(0)} ${Y(Math.max(y0, 0))}Z" fill="${s.color}" fill-opacity="0.10"/>`;
    }
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""} stroke-linejoin="round"/>`;
    const li = s.values.length - 1;
    if (s.values[li] !== null && s.values[li] !== undefined) g += `<circle cx="${X(li)}" cy="${Y(s.values[li])}" r="3.5" fill="${s.color}"/>`;
  });
  const legend = series.length > 1 ? `<div class="legend" style="padding:0 4px 4px">${series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "";
  return `${legend}<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">${g}</svg>`;
}

// One dot-and-whisker per row on its own symmetric scale around zero.
export function diffWhisker(mean, lo, hi, betterWhenNegative = true) {
  if (mean === null || mean === undefined) return '<span class="muted">—</span>';
  const W = 160, H = 18, m = Math.max(Math.abs(lo ?? mean), Math.abs(hi ?? mean), Math.abs(mean), 1e-9);
  const X = (v) => W / 2 + (v / m) * (W / 2 - 6);
  const sig = lo !== null && hi !== null && (lo > 0 || hi < 0);
  const good = sig && ((betterWhenNegative && hi < 0) || (!betterWhenNegative && lo > 0));
  const col = !sig ? "var(--muted)" : good ? "var(--ok)" : "var(--crit)";
  let s = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="difference ${fmt(mean, 2)}"><line x1="${W / 2}" x2="${W / 2}" y1="1" y2="${H - 1}" stroke="var(--line-strong)"/>`;
  if (lo !== null && hi !== null) s += `<line x1="${X(lo)}" x2="${X(hi)}" y1="${H / 2}" y2="${H / 2}" stroke="${col}" stroke-width="2"/><line x1="${X(lo)}" x2="${X(lo)}" y1="${H / 2 - 4}" y2="${H / 2 + 4}" stroke="${col}" stroke-width="2"/><line x1="${X(hi)}" x2="${X(hi)}" y1="${H / 2 - 4}" y2="${H / 2 + 4}" stroke="${col}" stroke-width="2"/>`;
  s += `<circle cx="${X(mean)}" cy="${H / 2}" r="4" fill="${col}"/></svg>`;
  return s;
}

export function debounce(fn, ms = 350) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function formData(form) {
  const out = {};
  new FormData(form).forEach((v, k) => { out[k] = v; });
  return out;
}

// Accessible in-page confirmation dialog (no browser alert/confirm). Resolves true/false.
export function confirmBox({ title, body, confirmLabel = "Confirm", danger = false, requireText = null }) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "modal-back";
    wrap.innerHTML = `<div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="mTitle">
      <h3 id="mTitle">${esc(title)}</h3><div class="modal-body">${body}</div>
      ${requireText ? `<label class="field" style="margin-top:10px"><span>Type <b class="mono">${esc(requireText)}</b> to confirm</span><input type="text" id="mText" autocomplete="off"></label>` : ""}
      <div class="toolbar" style="justify-content:flex-end;margin-top:14px"><button id="mNo">Cancel</button><button id="mYes" class="${danger ? "danger" : "primary"}" ${requireText ? "disabled" : ""}>${esc(confirmLabel)}</button></div></div>`;
    document.body.appendChild(wrap);
    const done = (v) => { wrap.remove(); document.removeEventListener("keydown", onKey, true); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
    document.addEventListener("keydown", onKey, true);
    wrap.querySelector("#mNo").onclick = () => done(false);
    wrap.querySelector("#mYes").onclick = () => done(true);
    wrap.addEventListener("click", (e) => { if (e.target === wrap) done(false); });
    const txt = wrap.querySelector("#mText");
    if (txt) { txt.oninput = () => { wrap.querySelector("#mYes").disabled = txt.value.trim() !== requireText; }; txt.focus(); }
    else wrap.querySelector("#mYes").focus();
  });
}
