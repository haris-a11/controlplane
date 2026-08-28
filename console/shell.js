// Shared helpers for the operator console. No framework, no build step.
//
// This file belongs to the BACKEND surface only. The member-facing app under
// frontend/ shares nothing with it — not a stylesheet, not a helper — because the
// two have opposite jobs: the console exposes every signal behind a decision, and
// the member app exposes none of them. Splitting them apart is what makes that
// asymmetry enforceable rather than a matter of remembering.

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
      <small>governance console · team Fremen</small></div>
    <nav>${link("/console", "Operations")}${link("/console/tuning", "Tuning")}${
      link("/console/policy", "Policy")}
      <a href="/" class="outlink" title="The member-facing app this console governs"
        >Member app ↗</a></nav>
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

/* --- the decision inspector --------------------------------------------------
   Moved here out of the old member-facing chat page. Every signal behind a
   decision belongs on the operator surface, so it is built once and used by the
   dashboard's detail view.

   Takes a stored audit row and normalises it to the same shape the live
   `controlplane` response block uses, so one renderer serves both. */

function normalizeDecision(row) {
  const r = row.record || {};
  const g = r.grounding || {};
  return {
    decision_id: row.id,
    action: row.decision,
    reason: r.reason,
    tier_path: row.tier_path,
    tier1_ran: r.tier1_ran, tier1_reason: r.tier1_reason,
    judge_ran: r.judge_ran, judge_reason: r.judge_reason, judge: r.judge,
    signals: r.signals || {},
    grounding: typeof g === "object" ? g.grounding : g,
    best_span: g.best_span, span_scores: g.span_scores,
    sources: r.sources || [],
    ungoverned_sources: r.ungoverned_sources || [],
    question: r.question, answer: r.answer, original_answer: r.original_answer,
    use_case: row.use_case, user: row.user_id,
    generated_ms: r.generated_ms, check_ms: row.check_ms,
    sequential_ms: r.sequential_ms, action_ms: r.action_ms,
    latency_ms: row.latency_ms,
    cost_usd: row.cost_usd, check_cost_usd: row.check_cost_usd,
    cost_source: r.cost_source, over_budget: r.over_budget,
    simulated: !!row.simulated,
    ts: row.ts, row_hash: row.row_hash, chain_ok: row.chain_ok,
  };
}

/** The three tiers, in the order they ran, with the evidence each produced. */
function inspectorHTML(cp, policy) {
  const s = cp.signals || {};
  const p = policy || {};
  const lp = s.logprobs_available;
  const g = cp.grounding;
  const budget = p.latency_budget_ms || 1;

  const spans = (cp.sources || []).map((src, i) => {
    const sc = (cp.span_scores || [])[i];
    const best = i === cp.best_span;
    return `<div class="span-row" title="${esc(src.id || src.source || "")}">
      ${govBadge(src.governance)}
      <span class="meter"><i style="width:${((sc ?? 0) * 100).toFixed(0)}%;
        background:var(--${best ? "accent" : "line-strong"})"></i></span>
      <span class="sc">${sc == null ? "—" : sc.toFixed(2)}</span>
      ${best ? "<b style='font-size:.7rem;color:var(--accent)'>best</b>" : ""}
    </div>`;
  }).join("");
  const bestSrc = (cp.sources || [])[cp.best_span];

  return `
    <h3>Tier 0 · free signals <span class="badge">no model call</span></h3>
    <div class="card">
      <div class="sig"><span>Mean log-probability</span>
        <b>${lp ? s.mean_logprob.toFixed(3) : "unavailable"}</b></div>
      <div class="sig"><span>Weakest token</span>
        <b>${lp && s.min_logprob != null ? s.min_logprob.toFixed(3) : "—"}</b></div>
      <div class="sig"><span>PII detected</span>
        <b style="color:var(--${Object.keys(s.pii || {}).length ? "redact" : "text-primary"})">
          ${Object.keys(s.pii || {}).length ? Object.keys(s.pii).join(", ") : "none"}</b></div>
      <div class="sig"><span>Re-ask within 60 s</span><b>${s.reask ? "yes" : "no"}</b></div>
      <div class="sig"><span>Answer declined</span><b>${s.abstains ? "yes" : "no"}</b></div>
      <div class="sig"><span>Question asserts a premise</span>
        <b>${s.asserts_premise ? "yes → judge" : "no"}</b></div>
      <div class="sig"><span>Retrieval score</span>
        <b>${s.retrieval == null ? "—" : s.retrieval.toFixed(3)}</b></div>
      ${lp ? "" : `<p class="hint" style="margin:9px 0 0">This provider returns no
        token log-probabilities, so Tier 0 is running degraded. The router escalates
        to full verification rather than assuming the answer was confident.</p>`}
    </div>

    <h3>Tier 1 · grounding <span class="badge">${esc(cp.tier1_reason || "—")}</span></h3>
    <div class="card">
      ${cp.tier1_ran ? `
        <div class="sig"><span>Best span score</span>
          <b style="color:var(--${g >= (p.grounding_annotate ?? 1) ? "pass"
            : g >= (p.grounding_block ?? 0) ? "annotate" : "block"})">
          ${g == null ? "—" : g.toFixed(3)}</b></div>
        <div style="margin-top:10px">${spans}</div>
        ${bestSrc ? `<div class="quote">${esc((bestSrc.text || "").slice(0, 420))}</div>
          <div class="muted" style="font-size:.75rem;margin-top:6px">
            ${esc(bestSrc.source)} · ${esc(bestSrc.governance)}</div>` : ""}`
      : `<p class="muted" style="margin:0;font-size:.83rem">Not run — ${
          esc(cp.tier1_reason || "—")}.</p>`}
    </div>

    <h3>Tier 2 · AI judge <span class="badge">${esc(cp.judge_reason || "—")}</span></h3>
    <div class="card">
      ${cp.judge_ran && cp.judge ? `
        <div class="sig"><span>Verdict</span>
          <b style="color:var(--${["contradicted", "false_premise"].includes(cp.judge.verdict)
            ? "block" : "pass"})">${esc(cp.judge.verdict ?? "unavailable")}</b></div>
        <div class="sig"><span>Confidence</span>
          <b>${cp.judge.confidence == null ? "—" : cp.judge.confidence.toFixed(2)}</b></div>
        <p class="hint" style="margin:9px 0 0">${esc(cp.judge.reason || "")}</p>`
      : `<p class="muted" style="margin:0;font-size:.83rem">Not run — ${
          esc(cp.judge_reason || "—")}. Tier 2 costs a second model call, so it runs
          where it can change the outcome.</p>`}
    </div>

    ${(cp.ungoverned_sources || []).length ? `
      <h3>Data governance</h3>
      <div class="card">
        <p style="margin:0 0 7px;font-size:.83rem">The only span supporting this answer
        came from a source that is not under document governance:</p>
        ${cp.ungoverned_sources.map(x =>
          `<span class="badge ungoverned">${esc(x)}</span>`).join(" ")}
        <p class="hint" style="margin:9px 0 0">Groundedness cannot see this — the answer
        really is faithful to what it was shown. This profile's
        <code>ungoverned_action</code> is <code>${esc(p.ungoverned_action || "—")}</code>.</p>
      </div>` : ""}

    <h3>Cost and latency</h3>
    <div class="card">
      <div class="budget">
        <span class="meter" title="checking time against this profile's budget">
          <i style="width:${Math.min(100, (cp.check_ms || 0) / budget * 100).toFixed(0)}%;
            background:var(--${cp.over_budget ? "block" : "pass"})"></i></span>
        <span class="muted">${ms(cp.check_ms)} added / ${budget} ms budget</span>
      </div>
      <dl class="kv" style="margin-top:10px">
        <dt>Generation</dt><dd>${ms(cp.generated_ms)}</dd>
        <dt>Checking (wall clock)</dt><dd>${ms(cp.check_ms)}</dd>
        <dt>Checking if run in series</dt><dd>${ms(cp.sequential_ms)}
          ${cp.sequential_ms > cp.check_ms
            ? `<span class="badge ok">−${ms(cp.sequential_ms - cp.check_ms)} parallel</span>`
            : ""}</dd>
        <dt>Acting on the decision</dt><dd>${ms(cp.action_ms)}${cp.action_ms > 200
          ? ` <span class="muted">— a repair is a second generation</span>` : ""}</dd>
        <dt>End to end</dt><dd>${ms(cp.latency_ms)}</dd>
        <dt>Model spend</dt><dd>${usd(cp.cost_usd)}
          <span class="badge${cp.cost_source === "estimated" ? "" : " ok"}"
            title="Provider pricing is not always published; an estimate is labelled as one."
            >${esc(cp.cost_source || "unknown")}</span></dd>
        <dt>Checking spend</dt><dd>${usd(cp.check_cost_usd)}</dd>
        <dt>Overhead</dt><dd>${cp.cost_usd
          ? pct(cp.check_cost_usd / (cp.cost_usd + cp.check_cost_usd)) : "—"}</dd>
      </dl>
    </div>`;
}
