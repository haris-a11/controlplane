"""The core mechanism: decide how much checking this request is worth, then act.

Everything else in this repo exists elsewhere as a mature product. This file is
the contribution — per-request tier routing under a client-set policy, with the
cost of that decision recorded.
"""
import random
import re
import tomllib
from functools import lru_cache
from pathlib import Path

from . import checks

POLICIES = Path(__file__).resolve().parent.parent / "policies.toml"

HEDGE = "\n\n> ⚠️ This answer is only partly supported by the source policy. Verify before acting on it."
BLOCKED = ("This answer could not be verified against the policy document and has been "
           "held for review. A human agent will follow up.")


@lru_cache(maxsize=1)
def policies() -> dict:
    return tomllib.loads(POLICIES.read_text())


def policy(use_case: str) -> dict:
    p = policies()
    return p.get(use_case) or p["support"]


def needs_tier1(sig: dict, pol: dict) -> tuple[bool, str]:
    """Why Tier 1 ran matters as much as whether it did.

    'audit_sample' is the load-bearing case: without checking things Tier 0 has already
    passed, the false-negative rate is unknowable and a cheap first tier can be
    gamed by input crafted to look easy.
    """
    if pol["tier1_always"]:
        return True, "policy"
    if sig["pii"] or sig["reask"]:
        return True, "triggered"
    lp = sig["mean_logprob"]
    if lp is not None and lp < pol["tier0_trigger_logprob"]:
        return True, "triggered"
    if lp is None:
        return True, "no_logprobs"   # can't measure confidence, so don't assume it
    if random.random() < pol["audit_sample_rate"]:
        return True, "audit_sample"
    return False, "skipped"


def decide(sig: dict, grounding: float | None, pol: dict) -> tuple[str, str]:
    """Pure. Returns (action, reason). Five outcomes, not a binary gate."""
    # PII is settled before anything else. Every other branch below is a
    # judgement about correctness; this one is a leak, and a leak in an answer
    # that happens to abstain is still a leak.
    if sig["pii"]:
        if pol["pii_action"] == "block":
            return "block", f"pii:{','.join(sig['pii'])}"
        if pol["pii_action"] == "redact":
            return "redact", f"pii:{','.join(sig['pii'])}"
    if sig.get("abstains"):
        # "The policy does not cover this" is the correct answer to an uncoverable
        # question — released, recorded, never scored for grounding.
        #
        # We tried gating this on retrieval strength, on the theory that a refusal
        # issued while a strong span sat in context is really a miss. On our set it
        # did not discriminate: the one genuinely wrong refusal scored 0.271, below
        # eleven correct ones. It was a *retrieval* failure — the span was never
        # fetched — so the answer was faithful to what the model was shown. No
        # output-layer check can see that; the evidence it needs never arrived.
        # The knob was removed rather than shipped as dead flexibility. See RESULTS.md.
        return "abstain", "declined_no_source"
    if grounding is None:
        return "pass", "unchecked"
    if grounding < pol["grounding_block"]:
        return ("repair", "ungrounded") if pol["allow_repair"] else ("block", "ungrounded")
    if grounding < pol["grounding_annotate"]:
        return "annotate", "weakly_grounded"
    return "pass", "grounded"


def redact(text: str) -> str:
    for kind, pattern in checks.PII_PATTERNS.items():
        text = re.sub(pattern, f"[{kind} redacted]", text)
    return text


def apply(action: str, answer: str, contexts: list[str], regenerate=None) -> tuple[str, float | None]:
    """Carry out the decision. Returns (answer, regrounded_score)."""
    if action == "annotate":
        return answer + HEDGE, None
    if action == "redact":
        return redact(answer), None
    if action == "block":
        return BLOCKED, None
    if action == "repair" and regenerate:
        fixed = regenerate()
        score = checks.tier1(fixed, contexts)["grounding"]
        return (fixed, score) if score and score >= 0.35 else (BLOCKED, score)
    if action == "repair":
        return BLOCKED, None
    return answer, None


if __name__ == "__main__":
    strict, lax = policy("decision_support"), policy("copilot")
    clean = {"pii": {}, "reask": False, "mean_logprob": -0.1, "abstains": False}
    dirty = {"pii": {"email": ["a@b.co"]}, "reask": False, "mean_logprob": -0.1,
             "abstains": False}
    declined = {**clean, "abstains": True, "retrieval": 0.10}

    assert decide(clean, 0.95, strict) == ("pass", "grounded")
    assert decide(clean, 0.70, strict)[0] == "annotate"
    assert decide(clean, 0.10, strict) == ("block", "ungrounded")      # no repair allowed
    assert decide(clean, 0.10, lax)[0] == "repair"                     # repair allowed
    assert decide(dirty, 0.95, strict)[0] == "block"                   # regulated: pii blocks
    assert decide(dirty, 0.95, policy("support"))[0] == "redact"
    # regression: an abstaining answer that leaks must still be redacted
    assert decide({**dirty, "abstains": True}, 0.03, policy("support"))[0] == "redact"
    assert decide({**dirty, "abstains": True}, 0.03, strict)[0] == "block"
    assert decide(clean, None, lax) == ("pass", "unchecked")
    # A refusal must not be punished for scoring low against a silent document.
    assert decide(declined, 0.03, strict) == ("abstain", "declined_no_source")
    assert decide(declined, 0.03, policy("support")) == ("abstain", "declined_no_source")
    assert needs_tier1(clean, strict) == (True, "policy")
    assert needs_tier1({**clean, "mean_logprob": -2.0}, lax)[1] == "triggered"
    assert needs_tier1({**clean, "mean_logprob": None}, lax)[1] == "no_logprobs"
    assert redact("mail a@b.co now") == "mail [email redacted] now"
    assert apply("annotate", "x", [])[0].startswith("x\n\n> ⚠️")
    print("router ok: five outcomes reachable, abstention never scored for grounding")
