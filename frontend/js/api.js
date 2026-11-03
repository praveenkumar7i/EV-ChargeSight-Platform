// Thin fetch wrapper. Errors carry the server's `detail` message.
export class ApiError extends Error {
  constructor(message, status, detail = null) { super(message); this.status = status; this.detail = detail; }
}

// v9: the session lives in an HttpOnly cookie; state-changing requests carry the per-session CSRF token
// (kept in memory only, never in localStorage).
let csrfToken = null;
export function setCsrf(t) { csrfToken = t; }

export async function api(path, { method = "GET", body, form, headers = {} } = {}) {
  const opts = { method, headers: { ...headers }, credentials: "same-origin" };
  if (method !== "GET" && method !== "HEAD" && csrfToken) opts.headers["X-CSRF-Token"] = csrfToken;
  if (form) {
    opts.body = form;
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new ApiError("Cannot reach the ChargeSight server. Is it still running?", 0);
  }
  checkBuild(res.headers.get("X-ChargeSight-Build"));
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let msg = `Request failed (HTTP ${res.status})`;
    let detail = null;
    if (data && typeof data === "object" && data.detail) {
      detail = data.detail;
      msg = Array.isArray(data.detail)
        ? data.detail.map(d => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
        : typeof data.detail === "object" ? (data.detail.message || JSON.stringify(data.detail)) : data.detail;
    }
    if (res.status === 401 && detail && detail.code === "auth_required" && !path.startsWith("/api/auth/")) {
      location.href = "/signin?next=" + encodeURIComponent(location.pathname + location.hash);
    }
    throw new ApiError(msg, res.status, detail);
  }
  return data;
}

// The page records the frontend build it was loaded with; every API response carries the
// server's build. If they differ, this tab is running old JavaScript: say so instead of
// letting outdated buttons send requests the server no longer expects.
const PAGE_BUILD = document.querySelector('meta[name="cs-build"]')?.content || null;
let buildWarned = false;
function checkBuild(serverBuild) {
  if (buildWarned || !PAGE_BUILD || !serverBuild || serverBuild === PAGE_BUILD) return;
  buildWarned = true;
  const bar = document.createElement("div");
  bar.className = "note warn";
  bar.setAttribute("role", "alert");
  bar.style.cssText = "position:fixed;top:8px;left:50%;transform:translateX(-50%);z-index:1000;max-width:min(640px,calc(100vw - 32px));display:flex;gap:10px;align-items:center";
  bar.innerHTML = '<span>ChargeSight was updated on the server. This tab is running the previous version.</span><button class="primary sm">Reload</button>';
  bar.querySelector("button").onclick = () => location.reload();
  document.body.appendChild(bar);
}
