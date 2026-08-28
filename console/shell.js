// Shared helpers for the three console pages. No framework, no build step —
// the whole console is three static files served by the same FastAPI app.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const ACTIONS = ["pass", "abstain", "annotate", "redact", "repair", "block"];
const FLAGGED = ["annotate", "redact", "repair", "block"];

const pct = v => v == null ? "—" : (v * 100).toFixed(v < 0.1 && v > 0 ? 1 : 0) + "%";
const num = v => v == null ? "—" : v.toLocaleString();
const ms = v => v == null ? "—" : v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(2)} s`;
const usd = v => v == null ? "—" : v >= 1 ? `$${v.toFixed(2)}`
  : v >= 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(6)}`;
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const ago = ts => {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return `${Math.max(0, Math.round(d))}s ago`;
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
};
const when = ts => new Date(ts * 1000).toLocaleString(undefined,
  { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

const actionPill = a => `<span class="pill" style="background:var(--${a || "pass"})">${esc(a || "pass")}</span>`;

/** "0>1>2" → three badges, the ones that ran lit up. */
function tierPath(path) {
  const ran = new Set((path || "0").split(">"));
  return `<span class="tiers">${["0", "1", "2"].map((t, i) =>
    `${i ? "<span>›</span>" : ""}<b class="${ran.has(t) ? "on" : ""}">T${t}</b>`).join("")}</span>`;
}

const govBadge = g => `<span class="badge ${g === "governed" ? "governed" : "ungoverned"}">${
  g === "governed" ? "governed" : "ungoverned"}</span>`;

function topbar(current) {
  const link = (href, label) =>
    `<a href="${href}"${href === current ? ' aria-current="page"' : ""}>${label}</a>`;
  return `<div class="topbar">
    <div class="brand"><span class="dot"></span>ControlPlane
      <small>checking layer · team Fremen</small></div>
    <nav>${link("/chat", "Chat")}${link("/dashboard", "Dashboard")}${link("/console", "Tuning")}</nav>
  </div>`;
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({ detail: `${r.status} ${r.statusText}` }));
  if (!r.ok) throw new Error(body.detail || `request failed: ${r.status}`);
  return body;
}

/** Inline SVG line chart. No charting library — the console has no build step. */
function lineChart(points, keys, { w = 660, h = 200, xKey = "x", xMax = 1,
                                   pad = { t: 12, r: 90, b: 30, l: 44 },
                                   xLabel = "", yFmt = v => `${v * 100}%` } = {}) {
  if (!points.length) return `<div class="empty">no data</div>`;
  const x = v => pad.l + (v / xMax) * (w - pad.l - pad.r);
  const y = v => pad.t + (1 - v) * (h - pad.t - pad.b);
  const grid = [0, .25, .5, .75, 1].map(v =>
    `<line x1="${pad.l}" y1="${y(v)}" x2="${w - pad.r}" y2="${y(v)}" stroke="var(--line)"/>
     <text x="${pad.l - 8}" y="${y(v) + 4}" text-anchor="end" font-size="10"
       fill="var(--text-muted)">${yFmt(v)}</text>`).join("");
  const lines = keys.map(k => {
    const d = points.map((p, i) =>
      `${i ? "L" : "M"}${x(p[xKey]).toFixed(1)} ${y(p[k.key] ?? 0).toFixed(1)}`).join(" ");
    const last = points[points.length - 1];
    return `<path d="${d}" fill="none" stroke="var(--${k.color})" stroke-width="2"
      stroke-linejoin="round"/>
      <text x="${w - pad.r + 8}" y="${y(last[k.key] ?? 0) + 4}" font-size="11"
        fill="var(--${k.color})">${k.label}</text>`;
  }).join("");
  return `<figure><svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img"
    aria-label="${esc(xLabel)}">${grid}${lines}
    <text x="${(pad.l + w - pad.r) / 2}" y="${h - 1}" text-anchor="middle" font-size="10"
      fill="var(--text-muted)">${esc(xLabel)}</text></svg></figure>`;
}
