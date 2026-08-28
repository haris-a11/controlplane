"""Generate one enterprise-week of traffic, so the dashboard is at the brief's scale.

The Round 2 reference parameters assume "tens of thousands of interactions per
week" across several use cases. Making 40,000 real model calls to demonstrate that
would cost money and hours and prove nothing that one call does not, so the volume
is simulated — and labelled `simulated` on every row it writes, in the database, in
the API, and with a badge in the dashboard.

**What is simulated and what is real.** The traffic *shape* is invented: who asked,
when, under which profile. The *decisions* are not — every row is produced by
calling the real `router.needs_tier1` / `needs_judge` / `decide` over signals and
grounding scores actually measured in `eval/results.json`. So the flag rates,
escalation rates and decision mix on the dashboard are what this checker really
does to this corpus; only the arrival pattern is made up.

Measured numbers in RESULTS.md come from `eval/run_eval.py` and never from here.

    python -m demo.simulate_week              # ~40k interactions over 7 days
    python -m demo.simulate_week --n 5000     # smaller
    python -m demo.simulate_week --reset      # drop simulated rows first
"""
import json
import math
import random
import sys
import time
from pathlib import Path

from controlplane import audit, router

RESULTS = Path(__file__).resolve().parent.parent / "eval" / "results.json"

# Reference parameters, stated here so they are one edit away rather than assumed.
INTERACTIONS_PER_WEEK = 40_000
N_USERS = 250
DAYS = 7
USE_CASE_MIX = {"support": 0.65, "copilot": 0.28, "decision_support": 0.07}

# Hourly weights, local time: a support desk with a working-day peak and a long
# tail of evening self-service.
HOURLY = [.2, .12, .08, .06, .06, .1, .25, .5, .85, 1.0, 1.0, .95,
          .8, .9, 1.0, .95, .85, .7, .6, .5, .45, .4, .35, .28]
DAILY = [1.0, 1.0, .98, .97, .9, .45, .35]     # Mon…Sun

# Token pricing assumption for the simulated spend column. Stated, not hidden:
# roughly a small hosted instruct model at $0.20/$0.60 per million tokens.
USD_PER_PROMPT_TOKEN = 0.20 / 1e6
USD_PER_COMPLETION_TOKEN = 0.60 / 1e6
CHECK_USD_PER_SEC = 0.05 / 3600


def _load_rows():
    if not RESULTS.exists():
        sys.exit("No eval/results.json — run `python -m eval.run_eval` first so the "
                 "simulation can resample real signals rather than inventing them.")
    rows = json.loads(RESULTS.read_text())["rows"]
    if not rows:
        sys.exit("eval/results.json has no rows.")
    return rows


def _users(n=N_USERS):
    """A few users generate most of the traffic, as they do in a real deployment.

    Zipf weights: without this the per-user table is uniform and useless, and the
    "which user is generating the risk" question the dashboard exists to answer
    has no interesting answer.
    """
    ids = [f"u_{i:04d}" for i in range(1, n + 1)]
    weights = [1 / (i ** 0.85) for i in range(1, n + 1)]
    return ids, weights


def _timestamps(n, days=DAYS):
    """Arrival times shaped by hour of day and day of week."""
    now = time.time()
    start = now - days * 86400
    buckets, weights = [], []
    for h in range(days * 24):
        t = start + h * 3600
        lt = time.localtime(t)
        buckets.append(t)
        weights.append(HOURLY[lt.tm_hour] * DAILY[lt.tm_wday])
    picked = random.choices(buckets, weights=weights, k=n)
    return sorted(t + random.random() * 3600 for t in picked)


def _tokens(question, answer, contexts_chars=2400):
    """Rough token counts — 4 chars per token is close enough for a cost estimate."""
    prompt = (len(question) + contexts_chars) // 4
    completion = max(len(answer or "") // 4, 8)
    return prompt, completion


def simulate(n=INTERACTIONS_PER_WEEK, days=DAYS, batch=4000):
    rows = _load_rows()
    ids, weights = _users()
    use_cases = list(USE_CASE_MIX)
    uc_weights = [USE_CASE_MIX[u] for u in use_cases]
    times = _timestamps(n, days)

    written, pending = 0, []
    for ts in times:
        src = random.choice(rows)
        uc = random.choices(use_cases, weights=uc_weights)[0]
        pol = router.policy(uc)
        sig = src["signals"]

        # Real routing over real signals. Only the arrival is invented.
        run_t1, t1_reason = router.needs_tier1(sig, pol)
        grounding = src["grounding"] if run_t1 else None
        run_judge, judge_reason = router.needs_judge(sig, grounding, pol)
        verdict = src.get("judge") if run_judge else None
        stale = src.get("ungoverned_sources") or [] if run_t1 else []
        action, reason = router.decide(sig, grounding, pol, verdict, stale)

        gen_ms = max(120, int(random.gauss(src.get("gen_ms", 900), 260)))
        check_ms = max(5, int(random.gauss(src.get("check_ms", 700), 120))) if run_t1 else 3
        if run_judge:
            check_ms += max(80, int(random.gauss(src.get("judge_ms", 700), 200)))
        latency = gen_ms + check_ms

        p_tok, c_tok = _tokens(src["question"], src.get("answer"))
        cost = p_tok * USD_PER_PROMPT_TOKEN + c_tok * USD_PER_COMPLETION_TOKEN
        if action == "repair":
            cost *= 2                       # repair is a second generation
        check_cost = (check_ms / 1000) * CHECK_USD_PER_SEC
        if run_judge:
            check_cost += p_tok * USD_PER_PROMPT_TOKEN + 40 * USD_PER_COMPLETION_TOKEN

        pending.append({
            "ts": ts, "use_case": uc, "user_id": random.choices(ids, weights=weights)[0],
            "latency_ms": latency, "decision": action, "simulated": True,
            "cost_usd": round(cost, 8), "check_cost_usd": round(check_cost, 8),
            "tier_path": "0" + (">1" if run_t1 else "") + (">2" if run_judge else ""),
            "question": src["question"], "answer": src.get("answer"),
            "reason": reason, "category": src.get("category"),
            "correct": src.get("correct"),
            "tier1_ran": run_t1, "tier1_reason": t1_reason,
            "judge_ran": run_judge, "judge_reason": judge_reason, "judge": verdict,
            "signals": sig, "grounding": {"grounding": grounding},
            "ungoverned_sources": stale,
            "sources": src.get("sources", []),
            "generated_ms": gen_ms, "check_ms": check_ms,
            # Budget measures what the checker adds, not the model's queue time.
            "over_budget": check_ms > pol["latency_budget_ms"],
        })
        if len(pending) >= batch:
            written += audit.log_many(pending)
            pending = []
            print(f"  {written:,}/{n:,}", end="\r", flush=True)
    if pending:
        written += audit.log_many(pending)
    return written


def reset():
    """Drop simulated rows. Live traffic and the chain over it are rebuilt after."""
    import sqlite3
    with sqlite3.connect(audit.DB) as c:
        n = c.execute("SELECT COUNT(*) FROM decisions WHERE simulated=1").fetchone()[0]
        c.execute("DELETE FROM decisions WHERE simulated=1")
    print(f"removed {n:,} simulated rows")
    print("note: deleting rows breaks the hash chain by design — that is the point "
          "of having one. Re-seed from empty for a clean chain.")


if __name__ == "__main__":
    audit.init()
    if "--reset" in sys.argv:
        reset()
        sys.exit()
    n = INTERACTIONS_PER_WEEK
    if "--n" in sys.argv:
        n = int(sys.argv[sys.argv.index("--n") + 1])
    started = time.perf_counter()
    print(f"simulating {n:,} interactions over {DAYS} days, {N_USERS} users, "
          f"{len(USE_CASE_MIX)} use cases…")
    written = simulate(n)
    took = time.perf_counter() - started
    agg = audit.aggregate()
    print(f"\nwrote {written:,} rows in {took:.1f}s")
    print(f"  volume        {agg['n']:,} total, {agg['users']} users")
    print(f"  flagged       {agg['flag_rate']:.1%}")
    print(f"  escalated     {agg['escalation_rate']:.1%}")
    print(f"  abstained     {agg['abstain_rate']:.1%}")
    print(f"  spend         ${agg['cost_usd']:.2f} model + "
          f"${agg['check_cost_usd']:.2f} checking ({agg['overhead_pct']}% overhead)")
    print(f"  added latency p50 {agg['p50_check_ms']} ms, p95 {agg['p95_check_ms']} ms")
    print(f"  end to end    p95 {agg['p95_latency_ms']} ms "
          f"(dominated by the model, not the checker)")
    ok, rows_checked, bad = audit.verify_chain()
    print(f"  audit chain   {'verified' if ok else f'BROKEN at {bad}'} over {rows_checked:,} rows")
    print("\nopen http://localhost:8000/dashboard")
