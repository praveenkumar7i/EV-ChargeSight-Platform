// Hand-written SVG charts with a shared hover tooltip.
// Every chart returns an HTML string; callers swap it into a fixed container,
// so periodic refreshes replace the SVG in place (no loading flash, no flicker).
import { esc, fmt } from "./ui.js";

// Categorical slots (fixed order, never cycled). Dark-surface steps of a
// CVD-validated palette; light steps are used when the light theme is active.
const CAT_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
const CAT_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
export function cat(i) {
  const dark = document.documentElement.dataset.theme !== "light";
  return (dark ? CAT_DARK : CAT_LIGHT)[i % 8];
}
// Status colours follow the app's status tokens (never reused for series).
export const STATUS_COLOR = { available: "var(--ok)", charging: "var(--charge)", faulted: "var(--crit)", offline: "var(--off)", unavailable: "var(--warn)" };
export const SEV_COLOR = { critical: "var(--crit)", major: "var(--warn)", minor: "var(--off)" };

// ---------------------------------------------------------------- tooltip
let tip = null;
function ensureTip() {
  if (tip) return tip;
  tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.setAttribute("role", "tooltip");
  document.body.appendChild(tip);
  return tip;
}
function hideTip() { if (tip) tip.style.display = "none"; }
function showTip(lines, clientX, clientY) {
  const el = ensureTip();
  el.innerHTML = lines.map((l, i) => (i === 0 ? `<b>${esc(l)}</b>` : esc(l))).join("<br>");
  el.style.display = "block";
  const pad = 14, w = el.offsetWidth, h = el.offsetHeight;
  let x = clientX + pad, y = clientY + pad;
  if (x + w > window.innerWidth - 8) x = clientX - w - pad;
  if (y + h > window.innerHeight - 8) y = clientY - h - pad;
  el.style.left = `${Math.max(4, x)}px`; el.style.top = `${Math.max(4, y)}px`;
}

// ---------------------------------------------------------------- time-series crosshair
// Every line chart is an <svg class="chart-ts" data-ts="{x, y, tips, keys}">. One delegated
// handler snaps a thin vertical line (and a marker per series) to the data point nearest
// the pointer, keyboard focus or touch. The crosshair only ever sits on real samples.
// When a chart is re-rendered with new samples, the crosshair is restored on the new SVG:
// under the pointer for mouse/touch, or on the same timestamp for keyboard users.
const xh = { svg: null, idx: -1, mode: null, clientX: null, clientY: null, key: null, label: null };

function tsData(svg) {
  if (!svg.__ts) { try { svg.__ts = JSON.parse(svg.getAttribute("data-ts")); } catch { svg.__ts = null; } }
  return svg.__ts;
}
function containerKey(svg) { const c = svg.closest("[id]"); return c ? c.id : null; }
function svgX(svg, clientX) {
  const m = svg.getScreenCTM();
  return m ? (clientX - m.e) / m.a : 0;
}
function nearestIndex(svg, clientX) {
  const d = tsData(svg);
  if (!d || !d.x.length) return -1;
  const x = svgX(svg, clientX);
  let best = 0, bd = Infinity;
  for (let i = 0; i < d.x.length; i++) {
    const dd = Math.abs(d.x[i] - x);
    if (dd < bd) { bd = dd; best = i; }
  }
  return best;
}
function setIndex(svg, i, mode, clientX, clientY) {
  const d = tsData(svg);
  if (!d || i < 0 || i >= d.x.length) return;
  const layer = svg.querySelector(".xh-layer");
  if (!layer) return;
  layer.style.display = "";
  const line = layer.querySelector("line");
  line.setAttribute("x1", d.x[i]); line.setAttribute("x2", d.x[i]);
  let topY = null;
  layer.querySelectorAll("circle").forEach((c, k) => {
    const y = d.y[k] ? d.y[k][i] : null;
    if (y === null || y === undefined) { c.style.display = "none"; return; }
    c.style.display = ""; c.setAttribute("cx", d.x[i]); c.setAttribute("cy", y);
    topY = topY === null ? y : Math.min(topY, y);
  });
  if (xh.svg && xh.svg !== svg) clearCrosshair(false);
  xh.svg = svg; xh.idx = i; xh.mode = mode; xh.key = containerKey(svg); xh.label = d.keys[i];
  if (clientX !== undefined && clientX !== null) { xh.clientX = clientX; xh.clientY = clientY; }
  const live = svg.parentElement && svg.parentElement.querySelector(".ts-live");
  if (live) live.textContent = d.tips[i].join(", ");
  // tooltip next to the pointer, or next to the point for keyboard users
  let cx = clientX, cy = clientY;
  if (mode === "key" || cx === undefined || cx === null) {
    const m = svg.getScreenCTM();
    if (m) { cx = m.a * d.x[i] + m.e; cy = m.d * (topY === null ? 20 : topY) + m.f; }
  }
  showTip(d.tips[i], cx, cy);
}
function clearCrosshair(resetState = true) {
  if (xh.svg) { const l = xh.svg.querySelector(".xh-layer"); if (l) l.style.display = "none"; }
  hideTip();
  if (resetState) { xh.svg = null; xh.idx = -1; xh.mode = null; xh.key = null; xh.label = null; }
}

document.addEventListener("pointermove", (e) => {
  const svg = e.target.closest ? e.target.closest("svg.chart-ts") : null;
  if (svg) { setIndex(svg, nearestIndex(svg, e.clientX), "pointer", e.clientX, e.clientY); return; }
  if (xh.svg && xh.mode === "pointer") clearCrosshair();
  const t = e.target.closest ? e.target.closest("[data-tip]") : null;
  if (!t) { if (!xh.svg) hideTip(); return; }
  showTip(t.getAttribute("data-tip").split("\n"), e.clientX, e.clientY);
}, { passive: true });
document.addEventListener("pointerdown", (e) => {          // touch: tap / drag selects the nearest sample
  const svg = e.target.closest ? e.target.closest("svg.chart-ts") : null;
  if (svg) setIndex(svg, nearestIndex(svg, e.clientX), "pointer", e.clientX, e.clientY);
  else if (xh.svg && xh.mode === "pointer") clearCrosshair();
}, { passive: true });
document.documentElement.addEventListener("mouseleave", () => { if (xh.mode === "pointer") clearCrosshair(); });
document.addEventListener("focusin", (e) => {
  const svg = e.target.matches && e.target.matches("svg.chart-ts") ? e.target : null;
  if (!svg) return;
  const d = tsData(svg);
  if (!d || !d.x.length) return;
  let i = d.x.length - 1;
  if (xh.key === containerKey(svg) && xh.label !== null) { const j = d.keys.indexOf(xh.label); if (j >= 0) i = j; }
  setIndex(svg, i, "key");
});
document.addEventListener("focusout", (e) => {
  if (e.target === xh.svg && xh.mode === "key" && !xh.restoring) clearCrosshair();
});
document.addEventListener("keydown", (e) => {
  const svg = document.activeElement;
  if (!svg || !svg.matches || !svg.matches("svg.chart-ts")) return;
  const d = tsData(svg);
  if (!d) return;
  let i = xh.svg === svg ? xh.idx : d.x.length - 1;
  if (e.key === "ArrowLeft") i -= e.shiftKey ? 10 : 1;
  else if (e.key === "ArrowRight") i += e.shiftKey ? 10 : 1;
  else if (e.key === "Home") i = 0;
  else if (e.key === "End") i = d.x.length - 1;
  else if (e.key === "Escape") { clearCrosshair(); return; }
  else return;
  e.preventDefault();
  setIndex(svg, Math.max(0, Math.min(d.x.length - 1, i)), "key");
});
// Re-attach the crosshair when a chart is re-rendered with new samples.
new MutationObserver(() => {
  if (!xh.svg || xh.svg.isConnected || !xh.key) return;
  const box = document.getElementById(xh.key);
  const svg = box && box.querySelector("svg.chart-ts");
  if (!svg) { clearCrosshair(); return; }
  const d = tsData(svg);
  if (!d || !d.x.length) { clearCrosshair(); return; }
  if (xh.mode === "pointer") {
    const r = svg.getBoundingClientRect();
    if (xh.clientX >= r.left && xh.clientX <= r.right && xh.clientY >= r.top && xh.clientY <= r.bottom) {
      setIndex(svg, nearestIndex(svg, xh.clientX), "pointer", xh.clientX, xh.clientY);
    } else clearCrosshair();
  } else if (xh.mode === "key") {
    const j = d.keys.indexOf(xh.label);
    const i = j >= 0 ? j : d.x.length - 1;
    xh.restoring = true; svg.focus({ preventScroll: true }); xh.restoring = false;
    setIndex(svg, i, "key");
  }
}).observe(document.body, { childList: true, subtree: true });
document.addEventListener("scroll", () => { if (xh.mode !== "key") hideTip(); }, { passive: true, capture: true });

// ---------------------------------------------------------------- helpers
function niceTicks(min, max, n = 4, integer = false) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  if (min === max) { max = min + (min === 0 ? 1 : Math.abs(min) * 0.1); }
  const span = max - min;
  const step0 = Math.pow(10, Math.floor(Math.log10(span / n)));
  const err = (span / n) / step0;
  let step = step0 * (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1);
  if (integer) step = Math.max(1, Math.round(step));
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const out = [];
  for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
  return out;
}
export function legend(items) {
  return `<div class="legend chart-legend">${items.map(i => `<span><i style="background:${i.color}${i.dash ? ";height:3px;border-radius:0" : ""}"></i>${esc(i.name)}</span>`).join("")}</div>`;
}
export const chartEmpty = (msg) => `<div class="chart-empty">${esc(msg)}</div>`;
export const chartError = (msg) => `<div class="chart-empty err">${esc(msg)}</div>`;

// ---------------------------------------------------------------- time / line chart
// series: [{name, color, values, dash, area, width}], labels: x labels (strings)
export function lineChart({ series, labels, height = 220, yMin, yMax, yFmt = (v) => fmt(v, 0), unit = "",
  target = null, tipLabels = null, valueFmt = null, zero = false, width = 640 }) {
  const W = width, H = height, pl = 56, pr = 14, pt = 12, pb = 26;
  const all = series.flatMap(s => s.values.filter(v => v !== null && v !== undefined && Number.isFinite(v)));
  if (!all.length) return chartEmpty("No data for this period yet.");
  let lo = yMin ?? Math.min(...all), hi = yMax ?? Math.max(...all);
  if (target) { lo = Math.min(lo, target.value); hi = Math.max(hi, target.value); }
  if (zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  const integer = series.every(s => s.values.every(v => v === null || v === undefined || Number.isInteger(v)));
  const ticks = niceTicks(lo, hi, 4, integer);
  const y0 = yMin ?? ticks[0], y1 = yMax ?? ticks[ticks.length - 1];
  const n = labels.length;
  const X = (i) => pl + (n <= 1 ? (W - pl - pr) / 2 : i * (W - pl - pr) / (n - 1));
  const Y = (v) => pt + (y1 - Math.min(Math.max(v, y0), y1)) * (H - pt - pb) / ((y1 - y0) || 1);
  let g = "";
  ticks.filter(t => t >= y0 && t <= y1).forEach(t => {
    g += `<line x1="${pl}" x2="${W - pr}" y1="${Y(t)}" y2="${Y(t)}" class="grid"/>`;
    g += `<text x="${pl - 8}" y="${Y(t) + 4}" text-anchor="end" class="axis">${esc(yFmt(t))}</text>`;
  });
  if (zero && y0 < 0 && y1 > 0) g += `<line x1="${pl}" x2="${W - pr}" y1="${Y(0)}" y2="${Y(0)}" class="zero"/>`;
  const every = Math.max(1, Math.ceil(n / 7));
  labels.forEach((l, i) => { if (i % every === 0 || i === n - 1) g += `<text x="${X(i)}" y="${H - 7}" text-anchor="middle" class="axis">${esc(l)}</text>`; });
  if (target) {
    g += `<line x1="${pl}" x2="${W - pr}" y1="${Y(target.value)}" y2="${Y(target.value)}" class="target"/>`;
    g += `<text x="${W - pr - 4}" y="${Y(target.value) - 5}" text-anchor="end" class="target-lbl">${esc(target.label)}</text>`;
  }
  series.forEach(s => {
    let d = "", on = false, first = null, last = null;
    s.values.forEach((v, i) => {
      if (v === null || v === undefined || !Number.isFinite(v)) { on = false; return; }
      d += `${on ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`; on = true;
      if (first === null) first = i; last = i;
    });
    if (s.area && first !== null) g += `<path d="${d}L${X(last)} ${Y(Math.max(y0, 0))}L${X(first)} ${Y(Math.max(y0, 0))}Z" fill="${s.color}" fill-opacity="0.12"/>`;
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""} stroke-linejoin="round" stroke-linecap="round"/>`;
    if (last !== null) g += `<circle cx="${X(last)}" cy="${Y(s.values[last])}" r="4" fill="${s.color}" stroke="var(--surface)" stroke-width="2"/>`;
  });
  // crosshair data: snapped x positions, per-series y positions and tooltip lines
  const vf = valueFmt || ((v) => `${fmt(v, Math.abs(v) < 10 ? 1 : 0)}${unit ? " " + unit : ""}`);
  const xs = [], tips = [], keys = [];
  const ys = series.map(() => []);
  for (let i = 0; i < n; i++) {
    xs.push(+X(i).toFixed(1));
    keys.push(String(tipLabels ? tipLabels[i] : labels[i]));
    const lines = [tipLabels ? tipLabels[i] : labels[i]];
    series.forEach((s, k) => {
      const v = s.values[i];
      const ok = v !== null && v !== undefined && Number.isFinite(v);
      ys[k].push(ok ? +Y(v).toFixed(1) : null);
      lines.push(`${s.name}: ${ok ? vf(v) : "—"}`);
    });
    if (target) lines.push(`${target.label}`);
    tips.push(lines);
  }
  g += `<rect class="ts-overlay" x="${pl}" y="${pt}" width="${W - pl - pr}" height="${H - pt - pb}" fill="transparent"/>`;
  g += `<g class="xh-layer" style="display:none" aria-hidden="true"><line x1="0" x2="0" y1="${pt}" y2="${H - pb}" class="xh-line"/>${series.map(s => `<circle r="4.5" fill="${s.color}" stroke="var(--surface)" stroke-width="2"/>`).join("")}</g>`;
  const lg = series.length > 1 || target ? legend([...series.map(s => ({ name: s.name, color: s.color, dash: s.dash })), ...(target ? [{ name: target.label, color: "var(--muted)", dash: true }] : [])]) : "";
  const data = esc(JSON.stringify({ x: xs, y: ys, tips, keys }));
  const name = esc(series.map(s => s.name).join(", "));
  return `${lg}<svg class="chart chart-ts" viewBox="0 0 ${W} ${H}" role="img" tabindex="0" aria-label="${name} over time. Focus and use the left and right arrow keys to read values." data-ts="${data}">${g}</svg><span class="ts-live sr-only" aria-live="polite"></span>`;
}

export function barChart({ labels, series, stacked = true, height = 220, yFmt = (v) => fmt(v, 0), unit = "",
  tipLabels = null, valueFmt = null, target = null, width = 640 }) {
  const W = width, H = height, pl = 56, pr = 14, pt = 12, pb = 26;
  const n = labels.length;
  if (!n || !series.length) return chartEmpty("No data for this period yet.");
  const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const maxV = stacked ? Math.max(...totals, 0) : Math.max(...series.flatMap(s => s.values.map(v => v || 0)), 0);
  const hiV = Math.max(maxV, target ? target.value : 0) || 1;
  const integer = series.every(s => s.values.every(v => v === null || v === undefined || Number.isInteger(v)));
  const ticks = niceTicks(0, hiV, 4, integer);
  const y1 = ticks[ticks.length - 1];
  const Y = (v) => pt + (y1 - v) * (H - pt - pb) / (y1 || 1);
  const band = (W - pl - pr) / n, gap = Math.min(10, band * 0.25), bw = band - gap;
  let g = "";
  ticks.forEach(t => {
    g += `<line x1="${pl}" x2="${W - pr}" y1="${Y(t)}" y2="${Y(t)}" class="grid"/>`;
    g += `<text x="${pl - 8}" y="${Y(t) + 4}" text-anchor="end" class="axis">${esc(yFmt(t))}</text>`;
  });
  const vf = valueFmt || ((v) => `${fmt(v, Number.isInteger(v) ? 0 : 1)}${unit ? " " + unit : ""}`);
  const every = Math.max(1, Math.ceil(n / 8));
  labels.forEach((l, i) => {
    const x0 = pl + i * band + gap / 2;
    if (i % every === 0 || i === n - 1) g += `<text x="${x0 + bw / 2}" y="${H - 7}" text-anchor="middle" class="axis">${esc(l)}</text>`;
    const lines = [tipLabels ? tipLabels[i] : l, ...series.map(s => `${s.name}: ${vf(s.values[i] || 0)}`)];
    if (stacked && series.length > 1) lines.push(`Total: ${vf(totals[i])}`);
    let acc = 0;
    if (stacked) {
      series.forEach(s => {
        const v = s.values[i] || 0;
        if (v <= 0) return;
        const yTop = Y(acc + v), yBot = Y(acc);
        g += `<rect x="${x0}" y="${yTop + 1}" width="${bw}" height="${Math.max(0, yBot - yTop - 2)}" rx="2" fill="${s.color}"/>`;
        acc += v;
      });
    } else {
      const sw = (bw - (series.length - 1) * 2) / series.length;
      series.forEach((s, k) => {
        const v = s.values[i] || 0;
        g += `<rect x="${x0 + k * (sw + 2)}" y="${Y(v)}" width="${sw}" height="${Math.max(0, Y(0) - Y(v))}" rx="2" fill="${s.color}"/>`;
      });
    }
    g += `<rect x="${pl + i * band}" y="${pt}" width="${band}" height="${H - pt - pb}" fill="transparent" class="hitbar" data-tip="${esc(lines.join("\n"))}"/>`;
  });
  if (target) g += `<line x1="${pl}" x2="${W - pr}" y1="${Y(target.value)}" y2="${Y(target.value)}" class="target"/><text x="${W - pr - 4}" y="${Y(target.value) - 5}" text-anchor="end" class="target-lbl">${esc(target.label)}</text>`;
  const lg = series.length > 1 ? legend(series.map(s => ({ name: s.name, color: s.color }))) : "";
  return `${lg}<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="bar chart">${g}</svg>`;
}

// ---------------------------------------------------------------- horizontal bars (category comparison)
export function hBars({ items, max = null, fmtV = (v) => fmt(v, 1), unit = "", target = null }) {
  if (!items.length) return chartEmpty("Nothing to compare yet.");
  const hi = max ?? Math.max(...items.map(i => i.value || 0), target ? target.value : 0, 1);
  const lo = items.some(i => i.min !== undefined) ? Math.min(...items.map(i => i.min)) : 0;
  const pct = (v) => Math.max(0, Math.min(100, ((v - lo) / ((hi - lo) || 1)) * 100));
  return `<div class="hbars">${items.map(i => `<div class="hbar-row" data-tip="${esc([i.label, `${fmtV(i.value)}${unit}`, ...(i.tip || [])].join("\n"))}">
      <span class="hbar-lbl">${esc(i.label)}</span>
      <span class="hbar-track">${target ? `<span class="hbar-target" style="left:${pct(target.value)}%" title="${esc(target.label)}"></span>` : ""}<span class="hbar-fill" style="width:${pct(i.value || 0)}%;background:${i.color || cat(0)}"></span></span>
      <span class="hbar-val num">${esc(fmtV(i.value))}${esc(unit)}</span></div>`).join("")}
    ${target ? `<div class="legend" style="margin-top:6px"><span><i class="tgt-key"></i>${esc(target.label)}</span>${lo ? `<span>axis starts at ${esc(fmtV(lo))}${esc(unit)}</span>` : ""}</div>` : ""}</div>`;
}

// ---------------------------------------------------------------- donut (status distribution)
export function donut({ items, size = 168, center = "", sub = "" }) {
  const total = items.reduce((a, i) => a + i.value, 0);
  if (!total) return chartEmpty("No chargers for this data source.");
  const r = 62, c = 2 * Math.PI * r;
  let off = 0, arcs = "";
  items.forEach(i => {
    if (!i.value) return;
    const len = (i.value / total) * c;
    arcs += `<circle r="${r}" cx="80" cy="80" fill="none" stroke="${i.color}" stroke-width="20" stroke-dasharray="${Math.max(0, len - 2)} ${c}" stroke-dashoffset="${-off}" transform="rotate(-90 80 80)" data-tip="${esc(`${i.label}\n${i.value} of ${total} (${fmt(100 * i.value / total, 0)}%)`)}"/>`;
    off += len;
  });
  return `<div class="donut-wrap"><svg width="${size}" height="${size}" viewBox="0 0 160 160" role="img" aria-label="status distribution">${arcs}
      <text x="80" y="78" text-anchor="middle" class="donut-c">${esc(center)}</text><text x="80" y="98" text-anchor="middle" class="axis">${esc(sub)}</text></svg>
    <ul class="donut-legend">${items.map(i => `<li data-tip="${esc(`${i.label}: ${i.value}`)}"><i style="background:${i.color}"></i><span>${esc(i.label)}</span><b class="num">${i.value}</b></li>`).join("")}</ul></div>`;
}

// ---------------------------------------------------------------- sparkline
export function sparkline(values, { color = cat(0), height = 36, fmtV = (v) => fmt(v, 1), labels = null, unit = "" } = {}) {
  const vals = values.filter(v => v !== null && v !== undefined);
  if (vals.length < 2) return '<span class="muted small">not enough history</span>';
  const W = 200, H = height, lo = Math.min(...vals), hi = Math.max(...vals);
  const X = (i) => (i * (W - 4)) / (values.length - 1) + 2;
  const Y = (v) => H - 3 - ((v - lo) / ((hi - lo) || 1)) * (H - 6);
  let d = "", on = false;
  values.forEach((v, i) => { if (v === null || v === undefined) { on = false; return; } d += `${on ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`; on = true; });
  let hits = "";
  const cw = (W - 4) / (values.length - 1);
  values.forEach((v, i) => { hits += `<rect x="${X(i) - cw / 2}" y="0" width="${cw}" height="${H}" fill="transparent" data-tip="${esc(`${labels ? labels[i] : ""}\n${v === null || v === undefined ? "—" : fmtV(v) + unit}`)}"/>`; });
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="trend"><path d="${d}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>${hits}</svg>`;
}
