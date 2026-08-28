"""OpenAI-compatible proxy, plus the APIs the three console pages read.

Solutioning area: **architecture** — "where the checker sits in the pipeline, and
how checks can run in parallel to protect latency". It sits here, as inline
middleware: any app already speaking the OpenAI protocol is pointed at this URL
and gets checked without knowing it. That demonstrates model-agnosticism instead
of claiming it.

Tier 0 and Tier 1 both need the finished answer and neither needs the other, so
they are gathered rather than sequenced. Every response records `check_ms` (wall
clock) next to `sequential_ms` (the sum of the parts), which means the saving is a
number rather than a claim — **and the measured number is about 2 ms**, because
Tier 0 is regex and a dictionary lookup and there was never much there to hide.
Reported honestly rather than dropped: the structure is what allows a second
expensive detector to be added later without adding its latency, and that is the
argument for it. It is not a latency win today.

Request path:

    client → retrieve (governance-tagged) → model → ┌ Tier 0 signals ┐
                                                    └ Tier 1 grounding ┘
                                          → provenance → Tier 2 judge?
                                          → router.decide → act → audit

`check_ms` covers checking only. Acting on the decision is timed separately as
`action_ms`, because a repair is a second generation and charging the model's
queue to the checker's latency budget would make the budget meaningless.
"""
import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import audit, checks, judge, learning, llm, rag, router

# Re-exported: eval/run_eval.py and demo/ import these from here.
DEFAULT_MODEL = llm.DEFAULT_MODEL
GROUNDING_PROMPT = llm.GROUNDING_PROMPT
_complete = llm.complete
_logprob_stats = llm.logprob_stats

CONSOLE = Path(__file__).resolve().parent.parent / "console"

# A rough CPU cost for the local checkers, so overhead-% is derivable rather than
# left blank. Assumes ~$0.05/hour for a small always-on instance; the Tier 1
# classifier is the only thing that takes real time. Stated, not hidden: this is
# an assumption, and RESULTS.md says so.
CHECK_USD_PER_SEC = 0.05 / 3600

app = FastAPI(title="ControlPlane")


class Msg(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Msg]
    model: str = DEFAULT_MODEL
    stream: bool = False


class Feedback(BaseModel):
    decision_id: int
    verdict: str                      # agree | false_positive | false_negative
    reviewer: str = "reviewer"
    corrected_answer: str | None = None
    note: str | None = None


@app.on_event("startup")
def _startup():
    audit.init()


@app.post("/v1/chat/completions")
async def chat(req: ChatRequest,
               x_controlplane_use_case: str = Header("support"),
               x_controlplane_user: str = Header("anon")):
    started = time.perf_counter()
    question = next(m.content for m in reversed(req.messages) if m.role == "user")
    pol = router.policy(x_controlplane_use_case)

    hits = await asyncio.to_thread(rag.search, question)
    context = "\n\n---\n\n".join(f"[{h['id']}] {h['text']}" for h in hits)
    messages = [
        {"role": "system", "content": GROUNDING_PROMPT.format(context=context)},
        *[m.model_dump() for m in req.messages],
    ]

    response = await asyncio.to_thread(llm.complete, req.model, messages)
    answer = response.choices[0].message.content
    generated_ms = int((time.perf_counter() - started) * 1000)

    contexts = [h["text"] for h in hits]
    mean_lp, min_lp = llm.logprob_stats(response)

    # --- the parallel section -------------------------------------------------
    # Tier 0 is free and Tier 1 is the expensive local check. Both depend only on
    # the finished answer, so neither waits for the other.
    checks_started = time.perf_counter()

    def _t0():
        t = time.perf_counter()
        sig = checks.tier0(question, answer, x_controlplane_use_case, time.time(),
                           mean_lp, min_lp,
                           retrieval=hits[0]["score"] if hits else None)
        return sig, time.perf_counter() - t

    def _t1():
        t = time.perf_counter()
        # Tier 1 runs speculatively: whether its result is *used* is the router's
        # call, made below. Deciding first would serialise the two tiers and give
        # back the parallelism this section exists for.
        return checks.tier1(answer, contexts), time.perf_counter() - t

    (signals, t0_s), (grounding, t1_s) = await asyncio.gather(
        asyncio.to_thread(_t0), asyncio.to_thread(_t1))

    run_tier1, tier1_reason = router.needs_tier1(signals, pol)
    if not run_tier1:
        grounding = {"grounding": None, "best_span": None}

    # Provenance before the Tier 2 gate: "the only source backing this is an
    # unowned one" is a reason to pay for a second opinion, and it is the only
    # signal that fires on a confident, well-grounded, wrong answer.
    stale = router.ungoverned_only(hits, grounding.get("best_span"),
                                   grounding.get("span_scores"),
                                   pol["grounding_block"]) if run_tier1 else []

    # --- Tier 2: only where a second opinion can change the outcome -----------
    run_judge, judge_reason = router.needs_judge(signals, grounding["grounding"],
                                                 pol, stale)
    verdict = None
    if run_judge:
        t = time.perf_counter()
        verdict = await asyncio.to_thread(
            judge.judge, question, answer, contexts, req.model,
            [h["governance"] for h in hits])
        judge_s = time.perf_counter() - t
    else:
        judge_s = 0.0

    action, reason = router.decide(signals, grounding["grounding"], pol, verdict, stale)
    # Checking stops here. What follows is *acting* on the decision, and for a
    # repair that means a second generation — measured at 50 s on this provider.
    # Folding that into check_ms would report the model's queue as our overhead
    # and make the latency budget meaningless on exactly the responses that
    # matter most.
    check_ms = int((time.perf_counter() - checks_started) * 1000)
    sequential_ms = int((t0_s + t1_s + judge_s) * 1000)

    acting = time.perf_counter()
    final, regrounded = await asyncio.to_thread(
        router.apply, action, answer, contexts,
        lambda: _repair(req.model, question, contexts), reason)
    action_ms = int((time.perf_counter() - acting) * 1000)

    response.choices[0].message.content = final
    latency_ms = int((time.perf_counter() - started) * 1000)
    tier_path = "0" + (">1" if run_tier1 else "") + (">2" if run_judge else "")
    model_cost, cost_source = llm.cost(response)
    check_cost = round((check_ms / 1000) * CHECK_USD_PER_SEC
                       + ((verdict or {}).get("cost_usd") or 0.0), 8)

    decision_id = audit.log(
        use_case=x_controlplane_use_case,
        user_id=x_controlplane_user,
        latency_ms=latency_ms,
        decision=action,
        check_ms=check_ms,
        cost_usd=model_cost,
        check_cost_usd=check_cost,
        tier_path=tier_path,
        question=question,
        answer=final,
        original_answer=answer if final != answer else None,
        model=req.model,
        reason=reason,
        tier1_ran=run_tier1,
        tier1_reason=tier1_reason,
        judge_ran=run_judge,
        judge_reason=judge_reason,
        judge=verdict,
        signals=signals,
        grounding=grounding,
        regrounded=regrounded,
        ungoverned_sources=stale,
        sources=[{"id": h["id"], "source": h["source"], "governance": h["governance"],
                  "score": h["score"], "text": h["text"]} for h in hits],
        context_ids=[h["id"] for h in hits],
        top_score=hits[0]["score"] if hits else None,
        generated_ms=generated_ms,
        sequential_ms=sequential_ms,
        action_ms=action_ms,
        cost_source=cost_source,
        over_budget=check_ms > pol["latency_budget_ms"],
        usage=llm.usage(response),
    )
    out = response.model_dump()
    out["controlplane"] = {
        "decision_id": decision_id,
        "action": action, "reason": reason, "grounding": grounding["grounding"],
        "best_span": grounding.get("best_span"), "span_scores": grounding.get("span_scores"),
        "tier_path": tier_path, "tier1_ran": run_tier1, "tier1_reason": tier1_reason,
        "judge_ran": run_judge, "judge_reason": judge_reason, "judge": verdict,
        "signals": signals, "ungoverned_sources": stale,
        "sources": [{"id": h["id"], "source": h["source"],
                     "governance": h["governance"], "score": round(h["score"], 3),
                     "text": h["text"]} for h in hits],
        "original_answer": answer if final != answer else None,
        "use_case": x_controlplane_use_case, "user": x_controlplane_user,
        "policy": pol,
        "generated_ms": generated_ms, "check_ms": check_ms,
        "sequential_ms": sequential_ms, "action_ms": action_ms,
        "latency_ms": latency_ms,
        "cost_usd": model_cost, "check_cost_usd": check_cost,
        "cost_source": cost_source,
        "over_budget": check_ms > pol["latency_budget_ms"],
    }
    return out


def _repair(model, question, contexts):
    """Re-ground and re-issue: same question, only the retrieved spans, no latitude."""
    spans = "\n\n".join(contexts)
    r = llm.complete(model, [
        {"role": "system", "content":
            "Answer using ONLY the extracts below. Quote the wording where you can. "
            "If they do not answer the question, say exactly that.\n\n" + spans},
        {"role": "user", "content": question},
    ])
    return r.choices[0].message.content


# --- audit trail and live traffic --------------------------------------------

@app.get("/decisions")
def decisions(n: int = 50):
    return audit.recent(n)


@app.get("/api/traffic")
def api_traffic(user: str = None, use_case: str = None, action: str = None,
                since: float = None, live_only: bool = False, n: int = 200):
    return audit.traffic(user_id=user, use_case=use_case, action=action,
                         since=since, live_only=live_only, n=n)


@app.get("/api/users")
def api_users(since: float = None, live_only: bool = False):
    return audit.users(since=since, live_only=live_only)


@app.get("/api/metrics")
def api_metrics(since: float = None, use_case: str = None, live_only: bool = False):
    """Tiles, time series and the per-use-case split, in one round trip."""
    ok, checked, bad = audit.verify_chain()
    return {
        "totals": audit.aggregate(since, use_case, live_only),
        "series": audit.series(since, live_only=live_only),
        "by_use_case": {uc: {**audit.aggregate(since, uc, live_only),
                             "latency_budget_ms": router.policy(uc)["latency_budget_ms"]}
                        for uc in router.policies()},
        "chain": {"verified": ok, "rows": checked, "first_bad_id": bad},
        "queue_depth": audit.queue_depth(),
    }


@app.get("/api/decision/{decision_id}")
def api_decision(decision_id: int):
    row = audit.get(decision_id)
    if not row:
        raise HTTPException(404, "no such decision")
    ok, _, bad = audit.verify_chain()
    row["chain_ok"] = ok and (bad is None or bad > decision_id)
    return row


@app.get("/api/events")
async def api_events():
    """SSE. Polls the log's high-water mark — at this volume a poll is honest and
    a message bus would be infrastructure with nothing to carry."""
    async def stream():
        last = (audit.recent(1) or [{"id": 0}])[0]["id"]
        while True:
            rows = [r for r in audit.recent(25) if r["id"] > last]
            if rows:
                last = rows[0]["id"]
                for r in reversed(rows):
                    yield f"data: {json.dumps(r, default=str)}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1.5)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache",
                                      "x-accel-buffering": "no"})


# --- feedback loop ------------------------------------------------------------

@app.get("/api/queue")
def api_queue(n: int = 50):
    """Held responses awaiting a human. The escalation path, made real."""
    return audit.queue(n)


@app.post("/api/feedback")
def api_feedback(fb: Feedback):
    if fb.verdict not in ("agree", "false_positive", "false_negative"):
        raise HTTPException(400, "verdict must be agree|false_positive|false_negative")
    if not audit.get(fb.decision_id):
        raise HTTPException(404, "no such decision")
    fid = audit.add_feedback(fb.decision_id, fb.verdict, fb.reviewer,
                             fb.corrected_answer, fb.note)
    return {"id": fid, "ok": True}


@app.get("/api/learning")
def api_learning(use_case: str = "support"):
    return learning.report(use_case)


# --- policy, replay, tuning ---------------------------------------------------

@app.get("/api/policies")
def api_policies():
    return router.policies()


@app.post("/api/replay/{decision_id}")
def api_replay(decision_id: int, use_case: str):
    """Re-decide a stored response under a different profile. No model call.

    This is the clearest demonstration that one engine produces different
    behaviour from config alone — and it is only possible because decide() is pure.
    """
    row = audit.get(decision_id)
    if not row:
        raise HTTPException(404, "no such decision")
    rec = row["record"]
    action, reason = router.decide(
        rec.get("signals", {}), (rec.get("grounding") or {}).get("grounding"),
        router.policy(use_case), rec.get("judge"), rec.get("ungoverned_sources") or [])
    return {"decision_id": decision_id, "use_case": use_case,
            "action": action, "reason": reason,
            "was": {"use_case": row["use_case"], "action": row["decision"],
                    "reason": rec.get("reason")}}


def _eval_rows():
    f = Path(__file__).resolve().parent.parent / "eval" / "results.json"
    if not f.exists():
        raise HTTPException(404, "No eval run yet — run: python -m eval.run_eval")
    return json.loads(f.read_text())["rows"]


@app.get("/api/sweep")
def api_sweep(use_case: str = "support"):
    """The tradeoff curve: replayed through the real decide(), not re-modelled."""
    from eval.run_eval import sweep
    return {"use_case": use_case, "points": sweep(_eval_rows(), use_case),
            "shipped": router.policy(use_case)}


@app.get("/api/score")
def api_score(use_case: str = "support", block: float = 0.35, annotate: float = 0.65):
    from eval.run_eval import score
    rows = _eval_rows()
    s = score(rows, use_case, block, annotate)
    pol = {**router.policy(use_case), "grounding_block": block,
           "grounding_annotate": annotate}
    s["examples"] = [
        {"question": r["question"], "correct": r["correct"], "grounding": r["grounding"],
         "category": r["category"],
         "action": router.decide(r["signals"], r["grounding"], pol,
                                 r.get("judge"), r.get("ungoverned_sources") or [])[0]}
        for r in rows
    ]
    return s


# --- the three pages ----------------------------------------------------------

def _page(name):
    return FileResponse(CONSOLE / name)


@app.get("/")
def root():
    return _page("chat.html")


@app.get("/chat")
def chat_page():
    return _page("chat.html")


@app.get("/dashboard")
def dashboard_page():
    return _page("dashboard.html")


@app.get("/console")
def console():
    return _page("index.html")


@app.get("/shell.css")
def shell_css():
    return FileResponse(CONSOLE / "shell.css", media_type="text/css")


@app.get("/shell.js")
def shell_js():
    return FileResponse(CONSOLE / "shell.js", media_type="text/javascript")
