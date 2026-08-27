"""OpenAI-compatible proxy. Point any app at it; it retrieves, calls the model, logs.

Day 1 scope: the request path and the evidence trail. The tier router drops in
at the marked seam.
"""
import os
import time
from pathlib import Path

import litellm
from fastapi.responses import FileResponse
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import audit, checks, rag, router

DEFAULT_MODEL = os.getenv("CONTROLPLANE_MODEL", "gpt-4o-mini")

GROUNDING_PROMPT = """Answer the question using only the policy extracts below.
If the extracts do not cover it, say so plainly rather than guessing.

{context}"""

app = FastAPI(title="ControlPlane")


class Msg(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Msg]
    model: str = DEFAULT_MODEL
    stream: bool = False


def _logprob_stats(response) -> tuple[float | None, float | None]:
    """Tier 0's cheapest signal. (mean, min) — None when the provider withholds them.

    The minimum matters on its own: a confident answer with one shaky token is a
    different failure from an answer that is uniformly unsure.
    """
    try:
        lps = [t.logprob for t in response.choices[0].logprobs.content]
        return sum(lps) / len(lps), min(lps)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None, None


def _complete(model, messages):
    """Ask for logprobs; fall back for providers that don't serve them.

    Anthropic exposes no logprobs at all, so Tier 0 must degrade rather than fail.
    """
    try:
        return litellm.completion(
            model=model, messages=messages, logprobs=True, top_logprobs=5
        )
    except Exception:
        return litellm.completion(model=model, messages=messages)


@app.on_event("startup")
def _startup():
    audit.init()


@app.post("/v1/chat/completions")
def chat(req: ChatRequest, x_controlplane_use_case: str = Header("support")):
    started = time.perf_counter()
    question = next(m.content for m in reversed(req.messages) if m.role == "user")

    hits = rag.search(question)
    context = "\n\n---\n\n".join(f"[{h['id']}] {h['text']}" for h in hits)
    messages = [
        {"role": "system", "content": GROUNDING_PROMPT.format(context=context)},
        *[m.model_dump() for m in req.messages],
    ]

    response = _complete(req.model, messages)
    answer = response.choices[0].message.content
    generated_ms = int((time.perf_counter() - started) * 1000)

    pol = router.policy(x_controlplane_use_case)
    contexts = [h["text"] for h in hits]
    mean_lp, min_lp = _logprob_stats(response)

    checks_started = time.perf_counter()
    signals = checks.tier0(question, answer, x_controlplane_use_case,
                           time.time(), mean_lp, min_lp)
    run_tier1, tier1_reason = router.needs_tier1(signals, pol)
    grounding = checks.tier1(answer, contexts) if run_tier1 else {"grounding": None}

    action, reason = router.decide(signals, grounding["grounding"], pol)
    final, regrounded = router.apply(
        action, answer, contexts,
        regenerate=lambda: _repair(req.model, question, contexts),
    )
    check_ms = int((time.perf_counter() - checks_started) * 1000)

    response.choices[0].message.content = final
    latency_ms = int((time.perf_counter() - started) * 1000)
    audit.log(
        use_case=x_controlplane_use_case,
        latency_ms=latency_ms,
        decision=action,
        question=question,
        answer=final,
        original_answer=answer if final != answer else None,
        model=req.model,
        reason=reason,
        tier1_ran=run_tier1,
        tier1_reason=tier1_reason,
        signals=signals,
        grounding=grounding,
        regrounded=regrounded,
        context_ids=[h["id"] for h in hits],
        top_score=hits[0]["score"] if hits else None,
        generated_ms=generated_ms,
        check_ms=check_ms,
        over_budget=latency_ms > pol["latency_budget_ms"],
        usage={k: getattr(response.usage, k, None)
               for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
    )
    out = response.model_dump()
    out["controlplane"] = {
        "action": action, "reason": reason, "grounding": grounding["grounding"],
        "tier1_reason": tier1_reason, "check_ms": check_ms,
        "over_budget": latency_ms > pol["latency_budget_ms"],
    }
    return out


def _repair(model, question, contexts):
    """Re-ground and re-issue: same question, only the retrieved spans, no latitude."""
    spans = "\n\n".join(contexts)
    r = _complete(model, [
        {"role": "system", "content":
            "Answer using ONLY the extracts below. Quote the wording where you can. "
            "If they do not answer the question, say exactly that.\n\n" + spans},
        {"role": "user", "content": question},
    ])
    return r.choices[0].message.content


@app.get("/decisions")
def decisions(n: int = 50):
    return audit.recent(n)


def _eval_rows():
    import json
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
    s["examples"] = [
        {"question": r["question"], "correct": r["correct"], "grounding": r["grounding"],
         "category": r["category"],
         "action": router.decide(r["signals"], r["grounding"],
                                 {**router.policy(use_case), "grounding_block": block,
                                  "grounding_annotate": annotate})[0]}
        for r in rows
    ]
    return s


@app.get("/console")
def console():
    return FileResponse(Path(__file__).resolve().parent.parent / "console" / "index.html")
