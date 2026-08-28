"""The core mechanism: decide how much checking this request is worth, then act.

Solutioning area: **decision logic** (confidence scoring, tiered responses, and
the rules for when a human is pulled in) and **governance** (every threshold below
is client-set policy, read from `policies.toml`, never hard-coded here).

Everything else in this repo exists elsewhere as a mature product — the prior-art
scan found 63 of them. This file is the contribution: per-request tier routing
under a client-set policy, with the cost of that decision recorded. Nothing in the
scan joins the cost axis to the risk decision, and nothing exposes the tuning
tradeoff as the product surface instead of hiding a chosen threshold.

`decide()` is deliberately **pure**. That is what lets the console replay real
decisions at any threshold, and the eval sweep re-score a run, without paying a
model again. Do not reach into I/O from it.
"""
import random
import re
import tomllib
from functools import lru_cache
from pathlib import Path

from . import checks, judge, rag

POLICIES = Path(__file__).resolve().parent.parent / "policies.toml"

HEDGE = "\n\n> ⚠️ This answer is only partly supported by the source policy. Verify before acting on it."
STALE = ("\n\n> ⚠️ This answer is supported only by an internal source that is not "
         "under document governance ({sources}). It may be out of date. Check the "
         "policy document before acting on it.")
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
    gamed by input crafted to look easy (arXiv 2605.17288). It is also where the
    labelled data for the feedback loop comes from — see learning.py.
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


def needs_judge(sig: dict, grounding: float | None, pol: dict,
                stale_sources: list[str] | None = None) -> tuple[bool, str]:
    """Tier 2 is the expensive one, so it runs where it can change the outcome.

    Four cases:

    1. Policy says always (regulated routes).
    2. **The question asserted a premise.** This one is not optional, and it is
       deliberately checked BEFORE the grounding score is consulted. The failure
       Tier 2 exists for scores *well* at Tier 1 — RESULTS.md's 0.90-grounding
       wrong answer — so gating on a suspicious score would skip exactly the case
       that motivated building the judge. A free regex at Tier 0 says "this
       question takes something as given"; the judge rules on whether it is true.
    3. The grounding score is in the band where it is not evidence either way.
    4. **Tier 0 and Tier 1 disagree** — a confident answer that scored badly, or a
       hesitant one that scored well. Disagreement between two cheap checks is the
       cheapest possible signal that a third is worth paying for.

    Not gated on abstention: a refusal is scored by nobody, so there is nothing
    for the judge to arbitrate.
    """
    if not pol.get("judge_enabled", True):
        return False, "disabled"
    if sig.get("asserts_premise"):
        # Checked BEFORE the abstention gate, deliberately. Measured case:
        # "Since cataract has no waiting period, can I claim in month 2?" drew
        # "while cataract may not have a specified waiting period…" — which the
        # abstention regex reads as a refusal, because it is phrased like one.
        # It is not a refusal; it is agreement with a false premise wearing a
        # hedge. Abstaining on a question that asserted something is the one case
        # where a refusal still needs arbitrating.
        return True, "asserted_premise"
    if stale_sources:
        # Measured, and the reason this branch exists. The ablation in RESULTS.md
        # showed the judge catching nothing on its own: the rows it needed to see
        # scored 0.92–0.97 for groundedness with confident logprobs, so no gate
        # built on *how the answer looks* would ever select them. That is what a
        # confident hallucination is. The one signal that did fire was where the
        # support came from. Doubt about provenance is precisely when a second
        # opinion is worth paying for.
        return True, "ungoverned_source"
    if sig.get("abstains"):
        return False, "abstained"
    if pol.get("judge_always"):
        return True, "policy"
    if grounding is None:
        return False, "unchecked"
    if pol["judge_band_lo"] <= grounding <= pol["judge_band_hi"]:
        return True, "ambiguous_band"
    lp = sig.get("mean_logprob")
    if lp is not None:
        confident = lp >= pol["tier0_trigger_logprob"]
        if confident and grounding < pol["grounding_block"]:
            return True, "tier_disagreement"
        if not confident and grounding > pol["grounding_annotate"]:
            return True, "tier_disagreement"
    return False, "skipped"


def ungoverned_only(hits: list[dict] | None, best_span: int | None,
                    span_scores: list[float] | None = None,
                    floor: float = 0.35) -> list[str]:
    """Sources behind the answer, when *no governed source supports it*.

    Returns the source filenames, or [] when a governed span does support it.

    The question this answers is "is there a governed document backing this
    claim?", not "which single span scored highest". Those come apart constantly:
    a correct answer drawn from the policy will often have some stale wiki
    paragraph score marginally higher for entailment, because the wiki is written
    in plainer language than the policy is. Taking the argmax alone flagged **14 of
    40** answers on our set and pushed the false-positive rate from 4% to 23% —
    mostly on answers that were right and properly sourced.

    So: the answer is ungoverned-only when the best-scoring span is ungoverned AND
    no governed span clears `floor`. One governed source that supports the claim is
    enough, wherever it ranked.
    """
    if not hits:
        return []
    if best_span is None or not (0 <= best_span < len(hits)):
        # No winning span identified — fall back to whether anything governed
        # was retrieved at all.
        if any(h.get("governance") == rag.GOVERNED for h in hits):
            return []
        return sorted({h.get("source", "?") for h in hits})

    if hits[best_span].get("governance") == rag.GOVERNED:
        return []
    if span_scores:
        best_score = span_scores[best_span] if best_span < len(span_scores) else None
        if best_score is not None and best_score < floor:
            # Nothing supports this answer, governed or otherwise. "Grounded only
            # in a stale source" is not the right thing to say about an answer
            # that is not grounded at all — the ungrounded branch of decide()
            # owns this case. Reporting provenance here flagged ten more rows on
            # our set, every one of them already handled or correctly released.
            return []
        for i, h in enumerate(hits):
            if h.get("governance") == rag.GOVERNED and i < len(span_scores) \
                    and span_scores[i] >= floor:
                return []           # a governed source does back this claim
    return [hits[best_span].get("source", "?")]


def decide(sig: dict, grounding: float | None, pol: dict,
           verdict: dict | None = None, stale_sources: list[str] | None = None
           ) -> tuple[str, str]:
    """Pure. Returns (action, reason). Six outcomes, not a binary gate.

    Order is load-bearing, and each step is a different kind of claim:
    leak → refusal → second-opinion override → provenance → strength of support.
    """
    # PII is settled before anything else. Every other branch below is a
    # judgement about correctness; this one is a leak, and a leak in an answer
    # that happens to abstain is still a leak.
    if sig["pii"]:
        if pol["pii_action"] == "block":
            return "block", f"pii:{','.join(sig['pii'])}"
        if pol["pii_action"] == "redact":
            return "redact", f"pii:{','.join(sig['pii'])}"
    if judge.contradicts(verdict):
        # The whole reason Tier 2 exists: this branch fires on answers that scored
        # WELL for groundedness. A false premise echoed back in the source's own
        # vocabulary is grounded and wrong at the same time.
        #
        # Ordered ABOVE abstention on purpose. A model that agrees with a false
        # premise while hedging — "cataract may not have a specified waiting
        # period" — reads as a refusal to the Tier 0 regex, and releasing it
        # unexamined is how the measured case escaped. A genuine refusal draws
        # `not_in_source` from the judge, not `contradicted`, so real abstentions
        # still fall through to the branch below.
        v = verdict["verdict"]
        return ("repair", f"judge:{v}") if pol["allow_repair"] else ("block", f"judge:{v}")
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
    if stale_sources:
        # Faithful to a source that should not have been trusted. Groundedness
        # cannot see this — the answer really is supported by what it was shown.
        act = pol.get("ungoverned_action", "annotate")
        if act != "allow":
            return act, f"ungoverned:{','.join(stale_sources)}"
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


def apply(action: str, answer: str, contexts: list[str], regenerate=None,
          reason: str = "") -> tuple[str, float | None]:
    """Carry out the decision. Returns (answer, regrounded_score)."""
    if action == "annotate":
        note = STALE.format(sources=", ".join(reason.removeprefix("ungoverned:").split(","))) \
            if reason.startswith("ungoverned:") else HEDGE
        return answer + note, None
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
    strict, lax, support = policy("decision_support"), policy("copilot"), policy("support")
    clean = {"pii": {}, "reask": False, "mean_logprob": -0.1, "abstains": False}
    dirty = {"pii": {"email": ["a@b.co"]}, "reask": False, "mean_logprob": -0.1,
             "abstains": False}
    declined = {**clean, "abstains": True, "retrieval": 0.10}

    assert decide(clean, 0.95, strict) == ("pass", "grounded")
    assert decide(clean, 0.70, strict)[0] == "annotate"
    assert decide(clean, 0.10, strict) == ("block", "ungrounded")      # no repair allowed
    assert decide(clean, 0.10, lax)[0] == "repair"                     # repair allowed
    assert decide(dirty, 0.95, strict)[0] == "block"                   # regulated: pii blocks
    assert decide(dirty, 0.95, support)[0] == "redact"
    # regression: an abstaining answer that leaks must still be redacted
    assert decide({**dirty, "abstains": True}, 0.03, support)[0] == "redact"
    assert decide({**dirty, "abstains": True}, 0.03, strict)[0] == "block"
    assert decide(clean, None, lax) == ("pass", "unchecked")
    # A refusal must not be punished for scoring low against a silent document.
    assert decide(declined, 0.03, strict) == ("abstain", "declined_no_source")
    assert decide(declined, 0.03, support) == ("abstain", "declined_no_source")

    # Tier 2: the confident-wrong case RESULTS.md documents. High grounding, and
    # the judge overrides it — this is the row that motivated building the judge.
    fp = {"verdict": "false_premise", "confidence": 0.9, "reason": "s.3.2 lists cataract"}
    assert decide(clean, 0.90, support, verdict=fp) == ("repair", "judge:false_premise")
    assert decide(clean, 0.90, strict, verdict=fp) == ("block", "judge:false_premise")
    assert decide(clean, 0.90, support, verdict={"verdict": "supported"})[0] == "pass"
    assert decide(clean, 0.90, support, verdict=judge.FAILED)[0] == "pass"  # degrades

    # Governance: faithful to a source nobody owns.
    assert decide(clean, 0.95, support, stale_sources=["wiki.txt"]) \
        == ("annotate", "ungoverned:wiki.txt")
    assert decide(clean, 0.95, strict, stale_sources=["wiki.txt"])[0] == "block"
    assert decide(clean, 0.95, lax, stale_sources=["wiki.txt"])[0] == "pass"   # allow
    # PII still outranks provenance, and a refusal is still a refusal.
    assert decide(dirty, 0.95, support, stale_sources=["wiki.txt"])[0] == "redact"
    assert decide(declined, 0.95, support, stale_sources=["wiki.txt"])[0] == "abstain"

    gov = [{"governance": "governed", "source": "policy.txt"},
           {"governance": "ungoverned", "source": "wiki.txt"}]
    assert ungoverned_only(gov, 0) == []                  # governed span won
    assert ungoverned_only(gov, 1) == ["wiki.txt"]        # ungoverned span won
    assert ungoverned_only(gov, None) == []               # a governed span was retrieved
    assert ungoverned_only([gov[1]], None) == ["wiki.txt"]
    assert ungoverned_only([], 0) == [] and ungoverned_only(None, None) == []
    # The fix that mattered: a governed span that also supports the claim clears it,
    # even when a plainer-worded wiki paragraph out-scores it.
    assert ungoverned_only(gov, 1, [0.71, 0.74]) == [], "governed support must count"
    assert ungoverned_only(gov, 1, [0.11, 0.74]) == ["wiki.txt"], "nothing governed backs it"
    assert ungoverned_only(gov, 1, [0.40, 0.99], floor=0.6) == ["wiki.txt"]

    assert needs_tier1(clean, strict) == (True, "policy")
    assert needs_tier1({**clean, "mean_logprob": -2.0}, lax)[1] == "triggered"
    assert needs_tier1({**clean, "mean_logprob": None}, lax)[1] == "no_logprobs"

    # A hedged answer to a premise-bearing question must still be judged, and a
    # contradicted verdict must outrank the abstention release.
    hedged = {**clean, "abstains": True, "asserts_premise": True}
    assert needs_judge(hedged, 0.63, support) == (True, "asserted_premise")
    assert decide(hedged, 0.63, support, verdict=fp)[0] == "repair"
    # …while a genuine refusal, which draws not_in_source, still releases.
    assert decide({**declined, "asserts_premise": True}, 0.03, support,
                  verdict={"verdict": "not_in_source"}) == ("abstain", "declined_no_source")

    assert needs_judge(clean, 0.50, support)[1] == "ambiguous_band"
    assert needs_judge(clean, 0.95, strict)[1] == "policy"
    # The case the judge was built for: high grounding, and it must STILL be judged.
    premise = {**clean, "asserts_premise": True}
    assert needs_judge(premise, 0.90, support) == (True, "asserted_premise")
    assert needs_judge(premise, 0.90, lax) == (True, "asserted_premise")
    assert needs_judge(clean, 0.90, support) == (False, "skipped"), \
        "without the premise signal this is exactly the row that escapes"
    assert needs_judge(declined, 0.03, support) == (False, "abstained")
    assert needs_judge(clean, None, support) == (False, "unchecked")
    # confident (logprob above trigger) yet ungrounded → the two tiers disagree
    assert needs_judge(clean, 0.05, lax)[1] == "tier_disagreement"
    # hesitant yet well grounded → also a disagreement
    assert needs_judge({**clean, "mean_logprob": -2.0}, 0.99, lax)[1] == "tier_disagreement"
    assert needs_judge(clean, 0.99, lax) == (False, "skipped")
    # A well-grounded, confident answer resting on an unowned source is exactly
    # the row no other gate selects.
    assert needs_judge(clean, 0.97, lax, ["wiki.txt"]) == (True, "ungoverned_source")

    assert redact("mail a@b.co now") == "mail [email redacted] now"
    assert apply("annotate", "x", [])[0].startswith("x\n\n> ⚠️")
    assert "not under document governance" in \
        apply("annotate", "x", [], reason="ungoverned:wiki.txt")[0]
    print("router ok: six outcomes reachable · judge overrides high grounding · "
          "ungoverned provenance acted on · abstention never scored")
