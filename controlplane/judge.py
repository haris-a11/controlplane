"""Tier 2 — a second model, asked a different question than Tier 1 was.

Solutioning area: **detection techniques**, the "secondary AI-as-judge pattern".

Why it exists, specifically. Tier 1 (HHEM) scores *groundedness*: does this answer
echo the retrieved spans? That is not the same question as: is this answer true?
RESULTS.md documents the gap with a real row — `"Since cataract has no waiting
period, can I claim in month 2?"` scored **0.90** grounding and was released by
every profile. The question smuggles in a false premise, the model agreed with it,
and the agreement was phrased in language the source supports. No threshold fixes
that, because the score is not wrong; it is measuring something else.

So the judge is asked for a *verdict*, not a score, and the verdict includes two
outcomes groundedness has no way to express: `false_premise` and `not_in_source`.

Prior art: Patronus Lynx is this pattern shipped as fine-tuned open weights, and
it returns PASS/FAIL with reasoning. We call a general model instead — a repo that
downloads an 8B model to run a demo is a repo nobody runs. The verdict taxonomy
borrows from Amazon RefChecker, whose contribution is that "hallucination" is not
one failure mode.

Cost: this is the expensive tier. It runs on a band, not on traffic — see
`router.needs_judge()`.
"""
import json
import re

from . import llm

VERDICTS = ("supported", "not_in_source", "contradicted", "false_premise")

PROMPT = """You are auditing another model's answer for an insurance company.

You will be given a QUESTION, the SOURCE EXTRACTS the answering model was shown,
and its ANSWER. Judge only whether the answer is true given the extracts. Do not
rewrite it and do not judge its tone.

Each extract is tagged [GOVERNED] or [UNGOVERNED]. GOVERNED extracts come from the
authoritative, version-controlled policy document. UNGOVERNED extracts are internal
notes, wiki pages and support macros with no owner — they are frequently out of
date and they often contradict the policy.

The extracts will frequently disagree with each other. Resolve that as follows,
and read both rules before answering:

- The GOVERNED extract is always the one that is true.
- An answer that follows a GOVERNED extract is **"supported"** — even when an
  UNGOVERNED extract disagrees with it. Do NOT return "contradicted" merely
  because some extract conflicts with the answer; check which one.
- An answer resting only on UNGOVERNED extracts is **not** supported, however
  exactly it quotes them. That is "contradicted" when a GOVERNED extract says
  otherwise, and "not_in_source" when none addresses the point.

Reply with JSON only, no prose around it:
{{"verdict": "...", "confidence": 0.0-1.0, "reason": "one short sentence"}}

verdict must be exactly one of:
- "supported"      a GOVERNED extract establishes the answer
- "not_in_source"  the extracts neither establish nor contradict it
- "contradicted"   a GOVERNED extract states something incompatible with the answer
- "false_premise"  the QUESTION assumes something the extracts contradict, and
                   the ANSWER went along with it instead of correcting it

"false_premise" outranks the others: if the question is built on a false
assumption and the answer accepted it, say false_premise even where the wording
otherwise tracks the extracts. An answer that hedges ("may not have...", "it is
unclear") while still repeating the question's false assumption has gone along
with it.

SOURCE EXTRACTS:
{context}

QUESTION: {question}

ANSWER: {answer}"""

FAILED = {"verdict": None, "confidence": None, "reason": "judge unavailable",
          "ran": False}


def _parse(text: str) -> dict | None:
    """Models wrap JSON in prose and fences no matter how firmly you ask them not to."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    verdict = str(got.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return None
    try:
        conf = min(max(float(got.get("confidence", 0.5)), 0.0), 1.0)
    except (TypeError, ValueError):
        conf = 0.5
    return {"verdict": verdict, "confidence": conf,
            "reason": str(got.get("reason", ""))[:300], "ran": True}


def _label(contexts, governance):
    """Tag each extract with its governance tier.

    Measured, and the reason this exists: without the tags the judge scored both
    stale-source failures on our set as `supported` at confidence 1.0 — correctly,
    on the question it was actually being asked. It was reading the same
    contaminated context the answering model read. A judge is only ever as good as
    the evidence it is handed.
    """
    tiers = list(governance or [])
    return [f"[{(tiers[i] if i < len(tiers) else 'ungoverned').upper()}]\n{c}"
            for i, c in enumerate(contexts)]


def judge(question: str, answer: str, contexts: list[str], model=None,
          governance: list[str] | None = None) -> dict:
    """Returns a verdict dict. Never raises — an unreachable judge must not 500
    the request path, it must degrade the same way a missing logprob does."""
    model = model or llm.DEFAULT_MODEL
    try:
        r = llm.complete(model, [{"role": "user", "content": PROMPT.format(
            context="\n\n---\n\n".join(_label(contexts, governance)),
            question=question, answer=answer)}])
    except Exception as e:
        return {**FAILED, "reason": f"judge call failed: {type(e).__name__}"}
    got = _parse(r.choices[0].message.content or "")
    if got is None:
        return {**FAILED, "reason": "judge returned unparseable output"}
    usd, src = llm.cost(r)
    return {**got, "cost_usd": usd, "cost_source": src, "model": model}


def contradicts(verdict: dict | None) -> bool:
    """The two verdicts that override a high grounding score."""
    return bool(verdict) and verdict.get("verdict") in ("contradicted", "false_premise")


if __name__ == "__main__":
    assert _parse('{"verdict":"false_premise","confidence":0.9,"reason":"no"}')["ran"]
    assert _parse('here you go:\n```json\n{"verdict": "supported", '
                  '"confidence": 0.8, "reason": "s.3.2"}\n```')["verdict"] == "supported"
    assert _parse('{"verdict":"maybe"}') is None, "unknown verdicts must not pass"
    assert _parse("no json at all") is None
    assert _parse('{"verdict":"supported","confidence":"NaNsense"}')["confidence"] == 0.5
    assert contradicts({"verdict": "false_premise"})
    assert contradicts({"verdict": "contradicted"})
    assert not contradicts({"verdict": "not_in_source"})
    assert not contradicts(None) and not contradicts(FAILED)

    lab = _label(["policy text", "wiki text"], ["governed", "ungoverned"])
    assert lab[0].startswith("[GOVERNED]") and lab[1].startswith("[UNGOVERNED]")
    # Missing or short governance lists must not silently promote a source.
    assert _label(["a", "b"], None)[0].startswith("[UNGOVERNED]")
    assert _label(["a", "b"], ["governed"])[1].startswith("[UNGOVERNED]")

    import sys
    if "--live" in sys.argv:
        from dotenv import load_dotenv
        load_dotenv()
        ctx = ["3.2 Specified illnesses. The following conditions carry a waiting "
               "period of twenty-four (24) months: cataract, hernia, piles."]
        v = judge("Since cataract has no waiting period, can I claim in month 2?",
                  "Yes, cataract has no waiting period so you can claim in month 2.",
                  ctx)
        print("live:", v)
        assert contradicts(v), f"judge should catch the false premise, got {v}"
    print("judge ok: verdict parsing is strict, failure degrades instead of raising")
