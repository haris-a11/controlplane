"""The one place the product talks to a foundation model.

Solutioning area: **architecture**. Model-agnosticism is demonstrated rather than
claimed — everything here goes through LiteLLM, so any provider it supports works
by changing `CONTROLPLANE_MODEL` and nothing else.

Prior art: LiteLLM and Portkey own the AI-gateway seat. Writing provider adapters
would have been the single worst use of the time available.
"""
import os

import litellm
from dotenv import load_dotenv

# Explicit. LiteLLM happens to load .env on import today, but depending on a
# dependency's side effect for whether the demo can find its API key is the kind
# of thing that breaks on someone else's clean clone.
load_dotenv()

DEFAULT_MODEL = os.getenv("CONTROLPLANE_MODEL", "gpt-4o-mini")

GROUNDING_PROMPT = """Answer the question using only the policy extracts below.
If the extracts do not cover it, say so plainly rather than guessing.

{context}"""


def complete(model, messages, **kw):
    """Ask for logprobs; fall back for providers that don't serve them.

    Anthropic exposes no logprobs at all, and — measured, see RESULTS.md — two
    models behind the *same* OpenRouter account differ on this. So capability is
    detected per call rather than configured, and Tier 0 degrades rather than fails.
    """
    try:
        return litellm.completion(model=model, messages=messages,
                                  logprobs=True, top_logprobs=5, **kw)
    except Exception:
        return litellm.completion(model=model, messages=messages, **kw)


def logprob_stats(response) -> tuple[float | None, float | None]:
    """Tier 0's cheapest signal. (mean, min) — None when the provider withholds them.

    The minimum matters on its own: a confident answer with one shaky token is a
    different failure from an answer that is uniformly unsure.

    Method references, both built for exactly this black-box constraint:
    arXiv 2509.04492 (entropy production rate) and 2511.07694 (top-K only).
    """
    try:
        lps = [t.logprob for t in response.choices[0].logprobs.content]
        return sum(lps) / len(lps), min(lps)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None, None


def usage(response) -> dict:
    return {k: getattr(getattr(response, "usage", None), k, None)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")}


# Fallback pricing, per token, when the provider publishes none. Defaults are
# roughly a small hosted instruct model. Overridable so a deployment can state
# its real contract rates rather than inherit ours.
USD_PER_PROMPT_TOKEN = float(os.getenv("CONTROLPLANE_USD_PER_PROMPT_TOKEN", 0.20 / 1e6))
USD_PER_COMPLETION_TOKEN = float(os.getenv("CONTROLPLANE_USD_PER_COMPLETION_TOKEN", 0.60 / 1e6))


def cost(response) -> tuple[float | None, str]:
    """Spend on one call, in USD, with where the number came from.

    Returns (usd, source) where source is "provider" | "estimated" | "unknown".

    Joining cost to the risk decision is the part of this design the prior-art
    scan found unclaimed, so it is recorded per response rather than aggregated
    into a separate billing view.

    Measured, and the reason for the fallback: LiteLLM publishes no pricing for
    `openrouter/meta-llama/llama-3.3-70b-instruct`, the model this prototype was
    evaluated on. Cost visibility turns out to vary by provider exactly the way
    log-probability availability does, so it degrades the same way — to an
    estimate from token counts — rather than reporting a blank. Which mode a
    number came from is recorded on every response and shown in the console; an
    estimate presented as a measurement would be worse than no number at all.
    """
    try:
        c = litellm.completion_cost(completion_response=response)
        if c:
            return float(c), "provider"
    except Exception:
        pass
    u = usage(response)
    if u.get("prompt_tokens") or u.get("completion_tokens"):
        return ((u.get("prompt_tokens") or 0) * USD_PER_PROMPT_TOKEN
                + (u.get("completion_tokens") or 0) * USD_PER_COMPLETION_TOKEN,
                "estimated")
    return None, "unknown"
