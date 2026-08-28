"""Feedback loop: what the system learns from the cases it got wrong.

Solutioning area: **feedback loops** — "how flagged or overridden cases feed back
to improve detection quality over time" — and half of **metrics & monitoring**,
because the honest false-negative number can only come from here.

Three inputs, each a different kind of label:

1. **Reviewer verdicts** on held responses (`feedback` table). A reviewer marking
   a block as `false_positive` is saying the threshold is too tight, in the one
   place where someone actually knows.
2. **Audit-sampled traffic** — responses Tier 0 passed that were verified anyway.
   This is the only unbiased false-negative estimate available, because you cannot
   measure what you missed by looking at what you caught. It is also the standard
   mitigation for cascade gaming (arXiv 2605.17288), where input is crafted to
   look easy to the cheap tier.
3. **Tier disagreements** — a confident answer that scored badly, or a hesitant one
   that scored well. These are the rows worth a human's time.

What this deliberately does NOT do: retrain anything. A prototype that claimed to
improve a detector from 40 labels would be lying. It produces the labels, counts
the disagreements, and recommends a threshold — and says that is where it stops.
"""
from . import audit, router

# Reviewer verdicts, and what each implies about the threshold that produced it.
TIGHTEN = "false_negative"   # we released something wrong → block more
LOOSEN = "false_positive"    # we held something right → block less


def _sig(rec):
    return rec.get("signals") or {}


def _grounding(rec):
    g = rec.get("grounding")
    return g.get("grounding") if isinstance(g, dict) else g


def labelled(limit=2000) -> list[dict]:
    """Join reviewer verdicts back onto the decisions they were about.

    This is the labelled dataset the loop produces. It is small on purpose — it is
    real review effort, not synthetic data.
    """
    fb = {f["decision_id"]: f for f in audit.feedback(limit)}
    if not fb:
        return []
    out = []
    for row in audit.traffic(n=limit * 5):
        f = fb.get(row["id"])
        if not f:
            continue
        rec = row["record"]
        out.append({
            "decision_id": row["id"], "use_case": row["use_case"],
            "action": row["decision"], "reason": rec.get("reason"),
            "grounding": _grounding(rec), "signals": _sig(rec),
            "judge": rec.get("judge"),
            "ungoverned_sources": rec.get("ungoverned_sources") or [],
            "verdict": f["verdict"], "reviewer": f["reviewer"],
            "question": rec.get("question"),
            # The reviewer's ground truth: was the answer actually right?
            # A false_positive means we flagged a right answer; a false_negative
            # means we released a wrong one. "agree" means the action was correct.
            "answer_was_correct": f["verdict"] == LOOSEN
            or (f["verdict"] == "agree" and row["decision"] in ("pass", "abstain")),
        })
    return out


def disagreements(limit=2000) -> dict:
    """Tier 0 said one thing, Tier 1 said another. Free labels, no reviewer needed."""
    conf_ungrounded = hesitant_grounded = judged_over = n = 0
    for row in audit.traffic(n=limit):
        rec = row["record"]
        g, sig = _grounding(rec), _sig(rec)
        if g is None:
            continue
        n += 1
        pol = router.policy(row["use_case"])
        lp = sig.get("mean_logprob")
        if lp is not None:
            if lp >= pol["tier0_trigger_logprob"] and g < pol["grounding_block"]:
                conf_ungrounded += 1
            if lp < pol["tier0_trigger_logprob"] and g > pol["grounding_annotate"]:
                hesitant_grounded += 1
        # The judge overturning a high grounding score is the most valuable
        # disagreement in the system — it is the failure mode Tier 1 cannot see.
        if g >= pol["grounding_annotate"] and (rec.get("judge") or {}).get("verdict") \
                in ("contradicted", "false_premise"):
            judged_over += 1
    return {"checked": n, "confident_but_ungrounded": conf_ungrounded,
            "hesitant_but_grounded": hesitant_grounded,
            "judge_overturned_high_grounding": judged_over,
            "total": conf_ungrounded + hesitant_grounded + judged_over}


def audit_sample_estimate(limit=5000) -> dict:
    """False-negative rate estimated from traffic Tier 0 had already passed.

    Sample size is reported alongside the rate, always. A rate over eleven rows is
    not an estimate and presenting it as one would be the exact dishonesty this
    project is trying to avoid.
    """
    sampled = [r for r in audit.traffic(n=limit)
               if r["record"].get("tier1_reason") == "audit_sample"]
    caught = sum(1 for r in sampled if r["decision"] in audit.FLAGGED)
    return {
        "n": len(sampled),
        "would_have_been_missed": caught,
        "estimated_fn_rate": round(caught / len(sampled), 4) if sampled else None,
        "note": "Share of Tier-0-passed responses that full verification then flagged. "
                "This is the only unbiased false-negative signal available in "
                "production, where no answer key exists."
        if sampled else "No audit-sampled traffic yet — raise audit_sample_rate or "
                        "send more traffic.",
    }


def suggest_threshold(use_case="support", step=0.05) -> dict:
    """Pick the grounding_block that disagrees with reviewers least often.

    The error being minimised is disagreement about **holding**, not about
    flagging. A reviewer only ever sees the escalation queue, so their verdict is
    a statement about `repair`/`block` — annotate is a release with a hedge on it,
    and nobody was asked to rule on those. Scoring annotations as flags here would
    read every hedge as an overreach the human never complained about.

    The two thresholds move together at their shipped distance, the same way
    `eval.run_eval.sweep` moves them, so the recommendation is reachable by editing
    one number in policies.toml.

    Deliberately simple: a grid search over one knob, ties broken toward the
    shipped value so it does not thrash on a handful of labels. Sophistication
    here would out-run the evidence.
    """
    pol = router.policy(use_case)
    rows = [r for r in labelled() if r["grounding"] is not None]
    shipped = pol["grounding_block"]
    gap = pol["grounding_annotate"] - shipped
    if len(rows) < 5:
        return {"suggested": None, "shipped": shipped, "n_labelled": len(rows),
                "note": f"{len(rows)} labelled rows — too few to move a threshold on. "
                        "Review more of the queue."}
    best, best_err = shipped, None
    t = 0.0
    while t <= 0.95:
        block = round(t, 3)
        trial = {**pol, "grounding_block": block,
                 "grounding_annotate": round(min(block + gap, 1.0), 3)}
        err = 0
        for r in rows:
            action, _ = router.decide(r["signals"], r["grounding"], trial,
                                      r["judge"], r["ungoverned_sources"])
            held = action in audit.ESCALATED
            # Disagreement with the human: we held what they called right, or
            # released what they called wrong.
            err += held != (not r["answer_was_correct"])
        if best_err is None or err < best_err or (err == best_err
                                                  and abs(t - shipped) < abs(best - shipped)):
            best, best_err = block, err
        t += step
    return {"suggested": best, "shipped": shipped, "n_labelled": len(rows),
            "disagreements_at_suggested": best_err,
            "note": "Grid search over grounding_block against reviewer verdicts, "
                    "scored on hold-vs-release. A recommendation for a human to "
                    "accept, not an auto-applied change."}


def report(use_case="support") -> dict:
    rows = labelled()
    return {
        "use_case": use_case,
        "reviewed": len(rows),
        "verdict_counts": {v: sum(r["verdict"] == v for r in rows)
                           for v in ("agree", LOOSEN, TIGHTEN)},
        "queue_depth": audit.queue_depth(),
        "disagreements": disagreements(),
        "audit_sample": audit_sample_estimate(),
        "threshold": suggest_threshold(use_case),
        "what_this_does_not_do": "No detector is retrained here. The loop produces "
                                 "labelled data, counts disagreement, and recommends "
                                 "a threshold; a human applies it in policies.toml.",
    }


if __name__ == "__main__":
    import time as _t

    audit.use_temp_db()
    pol = router.policy("support")
    clean = {"pii": {}, "reask": False, "mean_logprob": -0.1, "abstains": False}

    # Two rows a reviewer will overturn: both held, both actually right.
    ids = []
    for g in (0.10, 0.15, 0.12, 0.18, 0.11, 0.14):
        ids.append(audit.log("support", 500, decision="block", user_id="u_learn",
                             question=f"q{g}", reason="ungrounded",
                             signals=clean, grounding={"grounding": g},
                             tier1_reason="policy"))
    for i in ids:
        audit.add_feedback(i, LOOSEN, note="policy does cover this")

    lab = labelled()
    assert len(lab) >= 6, lab
    assert all(r["answer_was_correct"] for r in lab), "false_positive ⇒ answer was right"

    s = suggest_threshold("support")
    assert s["n_labelled"] >= 6, s
    # Every labelled row is a right answer we blocked at 0.35, so the search must
    # move the threshold DOWN to stop blocking them.
    assert s["suggested"] < pol["grounding_block"], s
    assert s["disagreements_at_suggested"] == 0, s

    # A confident answer that scored badly is a tier disagreement.
    audit.log("support", 500, decision="repair", user_id="u_learn", question="qd",
              signals=clean, grounding={"grounding": 0.05}, tier1_reason="policy")
    d = disagreements()
    assert d["confident_but_ungrounded"] >= 1, d

    audit.log("support", 500, decision="annotate", user_id="u_learn", question="qs",
              signals=clean, grounding={"grounding": 0.5}, tier1_reason="audit_sample")
    est = audit_sample_estimate()
    assert est["n"] >= 1 and est["estimated_fn_rate"] is not None, est

    r = report("support")
    assert r["reviewed"] >= 6 and r["threshold"]["suggested"] is not None
    assert audit.verify_chain()[0], "feedback must not disturb the decision chain"
    print(f"learning ok: {r['reviewed']} labelled, "
          f"{r['disagreements']['total']} disagreements, "
          f"threshold {r['threshold']['shipped']} → {r['threshold']['suggested']}")
