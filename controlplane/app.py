"""OpenAI-compatible proxy. Point any app at it; it retrieves, calls the model, logs.

Day 1 scope: the request path and the evidence trail. The tier router drops in
at the marked seam.
"""
import os
import time

import litellm
from fastapi import FastAPI, Header
from pydantic import BaseModel

from . import audit, rag

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


def _mean_logprob(response) -> float | None:
    """Tier 0's cheapest signal. None when the provider withholds logprobs."""
    try:
        content = response.choices[0].logprobs.content
        return sum(t.logprob for t in content) / len(content)
    except (AttributeError, TypeError, ZeroDivisionError):
        return None


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

    # --- seam: the tier router goes here (day 2) ---
    # signals = tier0(answer, response, hits) -> tier1(...) -> action

    latency_ms = int((time.perf_counter() - started) * 1000)
    mean_logprob = _mean_logprob(response)
    audit.log(
        use_case=x_controlplane_use_case,
        latency_ms=latency_ms,
        question=question,
        answer=answer,
        model=req.model,
        context_ids=[h["id"] for h in hits],
        top_score=hits[0]["score"] if hits else None,
        mean_logprob=mean_logprob,
        logprobs_available=mean_logprob is not None,
        usage=dict(response.usage),
    )
    return response.model_dump()


@app.get("/decisions")
def decisions(n: int = 50):
    return audit.recent(n)
