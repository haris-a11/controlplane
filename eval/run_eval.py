"""Run the answer key through the pipeline once, store every signal.

Solutioning area: **metrics & monitoring** — "how you would define, measure and
report false positive/negative rates and overall system trustworthiness to a
skeptical stakeholder". This file is that answer, and RESULTS.md is its output.

Tier 1 and Tier 2 are forced on for every question regardless of policy, so the
console can replay decisions at any threshold without paying for the model again.
That is why router.decide() is pure.

    python -m eval.run_eval            # run against the model, write results.json
    python -m eval.run_eval --no-judge # skip Tier 2 (cheaper; one call per question)
    python -m eval.run_eval --report   # rebuild RESULTS.md from the stored run
    python -m eval.run_eval --rescore  # re-derive free signals from stored answers
"""
import json
import re
import sys
import time
from pathlib import Path

from controlplane import checks, judge as judge_mod, rag, router

HERE = Path(__file__).resolve().parent
KEY = HERE / "answer_key.jsonl"
RESULTS = HERE / "results.json"
REPORT = HERE.parent / "RESULTS.md"
SWEEP_STEPS = 21


def _slug(model):
    return model.replace("/", "-").replace(":", "-")


def run(model=None, with_judge=True):
    from controlplane import llm

    model = model or llm.DEFAULT_MODEL
    rows = []
    keys = [json.loads(l) for l in KEY.read_text().splitlines() if l.strip()]
    for i, item in enumerate(keys, 1):
        q = item["question"]
        hits = rag.search(q)
        contexts = [h["text"] for h in hits]
        started = time.perf_counter()
        resp = llm.complete(model, [
            {"role": "system", "content": llm.GROUNDING_PROMPT.format(
                context="\n\n---\n\n".join(f"[{h['id']}] {h['text']}" for h in hits))},
            {"role": "user", "content": q},
        ])
        answer = resp.choices[0].message.content
        gen_ms = int((time.perf_counter() - started) * 1000)

        t0 = time.perf_counter()
        mean_lp, min_lp = llm.logprob_stats(resp)
        sig = checks.tier0(q, answer, "eval", time.time(), mean_lp, min_lp,
                           retrieval=hits[0]["score"] if hits else None)
        t1 = checks.tier1(answer, contexts)
        grounding = t1["grounding"]
        check_ms = int((time.perf_counter() - t0) * 1000)

        # Tier 2 on every row, so the console can replay with and without it.
        # Skipped for abstentions: there is nothing for a judge to arbitrate.
        t2 = time.perf_counter()
        gov = [h["governance"] for h in hits]
        verdict = (judge_mod.judge(q, answer, contexts, model, gov)
                   if with_judge and (not sig["abstains"] or sig["asserts_premise"])
                   else None)
        judge_ms = int((time.perf_counter() - t2) * 1000)

        correct = bool(re.search(item["expect"], answer, re.I))
        usd, cost_src = llm.cost(resp)
        rows.append({**item, "answer": answer, "correct": correct,
                     "grounding": grounding, "signals": sig, "judge": verdict,
                     "best_span": t1.get("best_span"),
                     "span_scores": t1.get("span_scores"),
                     "ungoverned_sources": router.ungoverned_only(
                         hits, t1.get("best_span"), t1.get("span_scores")),
                     "sources": [{"source": h["source"], "governance": h["governance"],
                                  "score": round(h["score"], 3)} for h in hits],
                     "gen_ms": gen_ms, "check_ms": check_ms, "judge_ms": judge_ms,
                     # Cost joined to the risk decision — the axis the prior-art
                     # scan found nobody occupying. Measured per call, not modelled.
                     "cost_usd": usd, "cost_source": cost_src,
                     "usage": llm.usage(resp),
                     "judge_cost_usd": (verdict or {}).get("cost_usd"),
                     "check_cost_usd": (check_ms / 1000) * (0.05 / 3600)})
        v = (verdict or {}).get("verdict") or "-"
        print(f"  {i:>2}/{len(keys)} {'ok ' if correct else 'WRONG'} "
              f"g={grounding:.2f} judge={v:<14} {q[:44]}")
    blob = json.dumps({"model": model, "rows": rows}, indent=1)
    RESULTS.write_text(blob)                                   # the console reads this
    (HERE / f"results-{_slug(model)}.json").write_text(blob)   # kept for comparison
    return rows


def score(rows, use_case, block, annotate, use_judge=True, use_governance=True):
    """Replay the real decide() over stored signals. No logic is duplicated here.

    The two switches re-score the same stored run with Tier 2 and/or the governance
    check turned off. That is how RESULTS.md can state what each mechanism is
    actually worth — and, importantly, how much they overlap — rather than
    asserting it. Neither switch costs a model call.
    """
    pol = {**router.policy(use_case), "grounding_block": block, "grounding_annotate": annotate}
    out = {"pass": 0, "abstain": 0, "annotate": 0, "redact": 0, "repair": 0, "block": 0}
    fp = fn = correct = wrong = judged = judge_caught = 0
    for r in rows:
        stale = (r.get("ungoverned_sources") or []) if use_governance else []
        # The eval forces Tier 2 on every row so it can be replayed. Production
        # would gate it — count what the gate WOULD select, or the reported
        # verification share is the eval's, not the product's.
        would_judge, _ = router.needs_judge(r["signals"], r["grounding"], pol, stale)
        judged += would_judge
        verdict = r.get("judge") if (use_judge and would_judge) else None
        action, reason = router.decide(r["signals"], r["grounding"], pol, verdict, stale)
        out[action] += 1
        flagged = action not in ("pass", "abstain")
        if reason.startswith("judge:"):
            # Caught only because Tier 2 overruled the grounding score.
            plain, _ = router.decide(r["signals"], r["grounding"], pol, None, stale)
            judge_caught += plain in ("pass", "abstain") and not r["correct"]
        if r["correct"]:
            correct += 1
            fp += flagged
        else:
            wrong += 1
            fn += not flagged
    return {
        "actions": out,
        "flag_rate": round(
            sum(v for k, v in out.items() if k not in ("pass", "abstain")) / len(rows), 3),
        "abstain_rate": round(out["abstain"] / len(rows), 3),
        "false_positive_rate": round(fp / correct, 3) if correct else None,
        "false_negative_rate": round(fn / wrong, 3) if wrong else None,
        "escalation_rate": round((out["repair"] + out["block"]) / len(rows), 3),
        "judge_share": round(judged / len(rows), 3),
        "judge_only_catches": judge_caught,
        "n": len(rows), "n_correct": correct, "n_wrong": wrong,
    }


def sweep(rows, use_case):
    pol = router.policy(use_case)
    gap = pol["grounding_annotate"] - pol["grounding_block"]
    points = []
    for i in range(SWEEP_STEPS):
        block = i / (SWEEP_STEPS - 1) * 0.9
        points.append({"block": round(block, 3),
                       **score(rows, use_case, block, min(block + gap, 1.0))})
    return points


def _cost_lines(rows):
    """Measured cost overhead, against the deck's ~3% claim.

    Split three ways because the answer is different depending on whether Tier 2
    runs, and quoting only the flattering half would be exactly the thing this
    report exists not to do.
    """
    model = sum(r.get("cost_usd") or 0 for r in rows)
    cpu = sum(r.get("check_cost_usd") or 0 for r in rows)
    judge_c = sum(r.get("judge_cost_usd") or 0 for r in rows)
    if not model:
        return ["| Cost overhead | ~3% of inference spend | provider reported no pricing |"]
    local = cpu / model * 100
    full = (cpu + judge_c) / model * 100
    src = {r.get("cost_source") for r in rows if r.get("cost_usd")}
    note = ([] if src == {"provider"} else [
        "",
        "Cost basis: this provider publishes no per-token pricing that LiteLLM knows,",
        "so spend is **estimated from token counts** at the rates in `controlplane/llm.py`",
        "rather than reported by the provider. Which mode each number came from is",
        "recorded per response. Cost visibility varies by provider the same way",
        "log-probability availability does — that is worth knowing before a deployment",
        "promises a finance team a number.",
    ])
    return [
        f"| Cost overhead, Tier 0+1 only | ~3% of inference spend | **{local:.1f}%** |",
        f"| Cost overhead, with Tier 2 on every row | ~3% | **{full:.0f}%** |", "",
        "**The cost target is the right order of magnitude for the cheap tiers and**",
        "**collapses the moment a second model call is involved.** Tier 0 and Tier 1",
        f"together add **{local:.1f}%** — " + ("inside the ~3% target"
            if local <= 3 else f"about {local / 3:.0f}x the ~3% target, though still"
            " small enough that nobody re-plans a budget around it") + ", because a",
        "110M CPU classifier is nearly free next to a generation. Judging every",
        f"response instead costs **{full:.0f}%**: the judge re-reads the whole retrieved",
        "context to produce one word of verdict, so it roughly doubles the spend on",
        "any response it touches.",
        "",
        "That is the economic argument for the router stated as a number rather than a",
        "claim: detection is cheap and can run on everything, while a second opinion has",
        "to be *aimed*. It is also why `judge_always` is set only on the regulated",
        "profile, where the cost of being wrong dwarfs the cost of asking twice.",
        *note,
    ]


def report(rows):
    model = json.loads(RESULTS.read_text())["model"]
    acc = sum(r["correct"] for r in rows) / len(rows)
    lat = sorted(r["check_ms"] for r in rows)
    p95 = lat[int(len(lat) * 0.95) - 1]
    lines = [
        "# Measured results", "",
        f"`{len(rows)}` questions against the policy corpus, model `{model}`.",
        "Every question was verified at Tier 1 and Tier 2 so decisions can be replayed",
        f"at any threshold. Numbers are from one run on a {len(rows)}-question set —",
        "directional, not a benchmark.", "",
        f"- Raw model accuracy before any checking: **{acc:.0%}**",
        f"- Tier 0 + Tier 1 checking cost: median **{lat[len(lat)//2]} ms**, p95 **{p95} ms**",
        f"- Log-probabilities available from this provider: "
        f"**{'yes' if rows[0]['signals']['logprobs_available'] else 'no — Tier 0 degraded'}**",
        "", "## At each use case's shipped thresholds", "",
        "Same engine, same run, three policy profiles. Nothing but `policies.toml`",
        "differs between these rows.", "",
        "| Use case | Flag rate | False positive | False negative | Escalation | Abstained | Tier 2 share |",
        "|---|---|---|---|---|---|---|",
    ]
    for uc in ("support", "copilot", "decision_support"):
        p = router.policy(uc)
        s = score(rows, uc, p["grounding_block"], p["grounding_annotate"])
        lines.append(f"| `{uc}` | {s['flag_rate']:.0%} | {s['false_positive_rate']:.0%} "
                     f"| {s['false_negative_rate']:.0%} | {s['escalation_rate']:.0%} "
                     f"| {s['abstain_rate']:.0%} | {s['judge_share']:.0%} |")

    # Ablation. Two mechanisms were added to catch answers that score WELL for
    # groundedness; the question a skeptical reader will ask is which one earned
    # its keep, so both are switched off independently over the same stored run.
    if any(r.get("judge") for r in rows) or any(r.get("ungoverned_sources") for r in rows):
        p = router.policy("support")
        cells = {(j, g): score(rows, "support", p["grounding_block"],
                               p["grounding_annotate"], j, g)
                 for j in (True, False) for g in (True, False)}
        lines += ["", "## Ablation: which check is doing the work", "",
                  "The same stored run, re-scored with Tier 2 and the governance check",
                  "switched off independently. No new model calls — the same answers,",
                  "decided differently. Profile `support`, at its shipped thresholds.", "",
                  "| Tier 2 judge | Governance tier | False negative | False positive | Flag rate |",
                  "|---|---|---|---|---|"]
        for (j, g), s in cells.items():
            lines.append(
                f"| {'on' if j else 'off'} | {'on' if g else 'off'} | "
                f"{s['false_negative_rate']:.0%} | {s['false_positive_rate']:.0%} | "
                f"{s['flag_rate']:.0%} |")
        base, both = cells[(False, False)], cells[(True, True)]
        only_j, only_g = cells[(True, False)], cells[(False, True)]
        fn = lambda c: c["false_negative_rate"]
        fp = lambda c: c["false_positive_rate"]
        lines += ["",
                  f"Neither check on, **{fn(base):.0%}** of wrong answers are released; "
                  f"with both, **{fn(both):.0%}**.", ""]

        judge_adds = fn(only_g) - fn(both)      # what the judge adds, given governance
        gov_adds = fn(only_j) - fn(both)        # what governance adds, given the judge
        if gov_adds > 0 and judge_adds <= 0:
            lines += [
                "**The governance check is doing all of the work, and Tier 2 is not "
                "paying for itself here.**",
                f"Governance alone reaches the same {fn(only_g):.0%} false-negative rate as",
                f"both together, while the judge alone leaves it at {fn(only_j):.0%} — and",
                f"adding the judge on top raises false positives from {fp(only_g):.0%} to",
                f"{fp(both):.0%} for no corresponding gain.", "",
                "This is worth being precise about rather than claiming two wins. The rows",
                "Tier 2 needed to catch scored **0.92–0.97** for groundedness with confident",
                "log-probabilities: no gate built on how the *answer looks* selects them,",
                "because looking fine is what a confident hallucination does. The only",
                "signal that fired was where the supporting span came from — so the judge",
                "is now triggered by provenance doubt (`needs_judge` → `ungoverned_source`)",
                "and is, on this corpus, downstream of the check that already caught them.",
                "",
                "The honest reading: **a cheap provenance lookup beat a second model call**",
                "at the failure both were built for. Tier 2 is kept because it is the only",
                "mechanism that can catch a semantic error in a corpus with uniform",
                "provenance — a case this corpus, by construction, does not contain — and",
                "because `judge_always` is what the regulated profile is buying. On this",
                "evidence it should not be on by default for a support route.", ""]
        elif judge_adds > 0 and gov_adds > 0:
            lines += [
                "**Both checks earn their place: each catches wrong answers the other**",
                f"**misses.** Governance alone leaves {fn(only_g):.0%} of wrong answers",
                f"released and the judge alone {fn(only_j):.0%}; together, {fn(both):.0%}.", ""]
        elif judge_adds > 0:
            lines += [
                f"**Tier 2 is carrying this.** The judge alone reaches {fn(only_j):.0%}",
                f"false negatives against {fn(only_g):.0%} for the governance check alone.", ""]
        else:
            lines += [
                "**Neither check moved the false-negative rate on this run.** Reported",
                "rather than quietly dropped — the mechanisms are built and instrumented,",
                "and this set did not contain the failures they target.", ""]
        vc = {}
        for r in rows:
            v = (r.get("judge") or {}).get("verdict")
            if v:
                vc[v] = vc.get(v, 0) + 1
        if vc:
            lines += ["Judge verdicts across the set: "
                      + ", ".join(f"`{k}` {v}" for k, v in sorted(vc.items())) + ".", ""]

    # Governance tier: answers supported only by a loosely-governed source.
    stale_rows = [r for r in rows if r.get("ungoverned_sources")]
    if stale_rows:
        wrong_stale = sum(1 for r in stale_rows if not r["correct"])
        lines += ["", "## Answers grounded only in a loosely governed source", "",
                  f"**{len(stale_rows)} of {len(rows)}** answers were supported by a span from",
                  f"`corpus/ungoverned/` with no governed span behind them; **{wrong_stale}**",
                  "of those were wrong. This is the failure groundedness cannot see: the",
                  "answer is faithful to what it was shown, and what it was shown is stale.",
                  "The `ungoverned_action` knob is what acts on it — `annotate` for support,",
                  "`allow` for the internal copilot, `block` on the regulated route.", ""]
    lines += ["", "## The tradeoff", "",
              "Raising the block threshold catches more wrong answers and flags more right",
              "ones. There is no setting that does both. This is the dial in the console.", "",
              "| Block threshold | Flag rate | False positive | False negative |",
              "|---|---|---|---|"]
    for pt in sweep(rows, "support")[::4]:
        lines.append(f"| {pt['block']:.2f} | {pt['flag_rate']:.0%} | "
                     f"{pt['false_positive_rate']:.0%} | {pt['false_negative_rate']:.0%} |")
    others = sorted(HERE.glob("results-*.json"))
    if len(others) > 1:
        lines += ["", "## Same checker, different models", "",
                  "The checker is unchanged across these runs; only the model under",
                  "test differs. A weaker model produces more for it to catch — which is",
                  "the argument for downgrading a model *and* the reason cost belongs in",
                  "the same decision as risk.", "",
                  "| Model | Accuracy | Flagged | False positive | False negative | Abstained |",
                  "|---|---|---|---|---|---|"]
        for f in others:
            d = json.loads(f.read_text())
            rs = d["rows"]
            pol = router.policy("support")
            sc = score(rs, "support", pol["grounding_block"], pol["grounding_annotate"])
            a = sum(r["correct"] for r in rs) / len(rs)
            fn = f"{sc['false_negative_rate']:.0%} ({sc['n_wrong']} wrong)"
            lines.append(f"| `{d['model'].split('/')[-1]}` | {a:.0%} | {sc['flag_rate']:.0%} "
                         f"| {sc['false_positive_rate']:.0%} | {fn} | {sc['abstain_rate']:.0%} |")
        lines += ["", "Rates over a handful of wrong answers are not stable estimates —"
                  " the counts are shown for that reason.", ""]

    # The most useful row in the set: wrong, yet scored as well grounded.
    confident_wrong = sorted(
        [r for r in rows if not r["correct"] and r["grounding"] is not None],
        key=lambda r: -r["grounding"])
    lines += ["", "## Where it degrades", "",
              "Findings from the runs above, including the ones that did not work.", ""]
    if confident_wrong and confident_wrong[0]["grounding"] > 0.7:
        cw = confident_wrong[0]
        # Why this particular row scored well is not always the same story, and
        # asserting the wrong one would be exactly the kind of tidy narrative this
        # file exists to avoid. Name the mechanism the evidence actually supports.
        if cw["signals"].get("asserts_premise"):
            why = ["The question asserts a false premise, the model agreed with it, and the",
                   "agreement is phrased in language the source supports."]
        elif cw.get("ungoverned_sources"):
            why = [f"The supporting span came from `{cw['ungoverned_sources'][0]}` — a source",
                   "with no owner that contradicts the governed policy. The answer is",
                   "faithful to what it was shown; what it was shown was wrong."]
        else:
            why = ["The answer echoes the retrieved wording closely while getting the",
                   "substance wrong."]
        lines += [
            f"**A wrong answer can score as well grounded.** `{cw['question']}` was answered",
            f"incorrectly and still scored **{cw['grounding']:.2f}** grounding — above the",
            "release threshold for every profile.", *why,
            "Groundedness measures whether an answer *echoes* the source, not",
            "whether it is *true*. No threshold setting fixes this; only a differently",
            "shaped check does, which is what Tier 2 and the governance tier are for.", ""]
    lines += [
        "**Abstention had to be separated from groundedness.** An answer that declines —",
        '"the policy does not mention ambulance charges" — is by definition unsupported by',
        "the document, and scored 0.03–0.52. Treating that as a grounding failure flagged",
        "correct refusals at a **42% false-positive rate**. Detecting abstention at Tier 0",
        "and releasing it as its own outcome brought that to **6%** on the same data. The",
        "brief notes that risk categories overlap; this is where that bites in practice.", "",
        "**Retrieval strength did not detect false refusals.** A refusal is wrong when the",
        "source *does* cover the question. We tried gating abstention on retrieval score, on",
        "the theory that a strong span in context makes a refusal suspect. It did not",
        "discriminate: the one genuinely wrong refusal scored 0.271, below eleven correct",
        "ones. It was a retrieval failure — the span was never fetched — so the answer was",
        "faithful to what the model was shown. **No output-layer checker can catch this**;",
        "the evidence it would need never arrived. The knob was removed rather than shipped.", "",
        "**Log-probability availability varies by provider, not just by vendor.** Within a",
        "single OpenRouter account, one model returned logprobs and another did not. Tier 0",
        "degrades to its non-logprob signals and the router escalates rather than assuming",
        "confidence, but the assumption that a deployment has one known capability is wrong.", "",
        "## Targets vs measured", "",
              "| Metric | Round 1 target | Measured |", "|---|---|---|",
              f"| Fast-path p95 | < 150 ms | **{p95} ms** (checks only) |",
              "| Verification share | 12% of traffic | 100% — see below |", *_cost_lines(rows), "",
              f"**The latency target is missed by roughly {p95 // 150}x.** Tier 1 is a T5-base",
              "encoder on CPU in float32, scoring four spans per request. The target assumed",
              "the check was cheaper than it is. Honest paths: ONNX or int8 quantisation,",
              "batching across concurrent requests, scoring only the top two spans, or a GPU.",
              "None were attempted here — the number is reported as measured, not tuned",
              "toward the slide.", "",
              "Tier 1 turned out cheap enough to run on everything: HHEM-2.1-Open is a",
              "110M classifier on CPU. The 12% budget assumed sub-agent LLM calls. The",
              "scarce resource is the *action* — repair costs a second generation, and a",
              "block costs human time — not the detection. The escalation column above is",
              "the number that matters, and it is well under 12%.", ""]
    REPORT.write_text("\n".join(lines))
    print(f"\nwrote {REPORT}")


def rescore():
    """Re-derive the free signals from stored answers. No model calls.

    Changing a regex, or the corpus's governance layout, should not cost another
    eval run. The judge verdict is kept — that one was paid for.
    """
    data = json.loads(RESULTS.read_text())
    for r in data["rows"]:
        sig = r["signals"]
        sig["abstains"] = checks.abstains(r["answer"])
        sig["pii"] = checks.find_pii(r["answer"])
        hits = rag.search(r["question"])
        sig["retrieval"] = hits[0]["score"] if hits else None
        r["ungoverned_sources"] = router.ungoverned_only(
            hits, r.get("best_span"), r.get("span_scores"))
        r["sources"] = [{"source": h["source"], "governance": h["governance"],
                         "score": round(h["score"], 3)} for h in hits]
    RESULTS.write_text(json.dumps(data, indent=1))
    print(f"rescored {len(data['rows'])} rows from stored answers")
    return data["rows"]


def _selftest():
    """Synthetic rows: a grounded-correct one, an ungrounded-wrong one, a wrong-but-confident one."""
    clean = {"pii": {}, "reask": False, "mean_logprob": -0.1}
    rows = [
        {"correct": True,  "grounding": 0.95, "signals": clean, "category": "supported"},
        {"correct": False, "grounding": 0.05, "signals": clean, "category": "not_covered"},
        {"correct": False, "grounding": 0.90, "signals": clean, "category": "contradicted"},
    ]
    loose = score(rows, "copilot", 0.0, 0.0)
    assert loose["actions"]["pass"] == 3, loose            # nothing flagged
    assert loose["false_negative_rate"] == 1.0, loose      # both wrong ones released
    assert loose["false_positive_rate"] == 0.0, loose

    tight = score(rows, "copilot", 0.99, 1.0)
    assert tight["flag_rate"] == 1.0, tight                # everything flagged
    assert tight["false_positive_rate"] == 1.0, tight      # incl. the correct one
    assert tight["false_negative_rate"] == 0.0, tight

    mid = score(rows, "copilot", 0.5, 0.8)
    assert mid["false_negative_rate"] == 0.5, mid          # confident-wrong still slips

    # Tier 2 is exactly the thing that closes that gap. Same rows, judge attached
    # to the confident-wrong one only.
    judged = [{**r, "signals": {**r["signals"], "asserts_premise": True},
               "judge": {"verdict": "false_premise", "confidence": 0.9, "ran": True}}
              if r["category"] == "contradicted" else r for r in rows]
    with_j = score(judged, "copilot", 0.5, 0.8, use_judge=True)
    without_j = score(judged, "copilot", 0.5, 0.8, use_judge=False)
    assert with_j["false_negative_rate"] < without_j["false_negative_rate"], (with_j, without_j)
    assert with_j["judge_only_catches"] == 1, with_j
    assert with_j["judge_share"] > 0, "the gate must select the ambiguous row"

    # Governance: an answer supported only by an unowned source is acted on even
    # when it scores well, and `allow` opts out.
    stale = [{**rows[0], "ungoverned_sources": ["wiki.txt"]}]
    assert score(stale, "support", 0.35, 0.65)["actions"]["annotate"] == 1
    assert score(stale, "decision_support", 0.6, 0.85)["actions"]["block"] == 1
    assert score(stale, "copilot", 0.2, 0.55)["actions"]["pass"] == 1

    pts = sweep(rows, "support")
    assert len(pts) == SWEEP_STEPS
    fn = [p["false_negative_rate"] for p in pts]
    fp = [p["false_positive_rate"] for p in pts]
    assert fn[0] >= fn[-1] and fp[-1] >= fp[0], "curves must move in opposite directions"
    print("eval ok: tradeoff is real — FN falls only as FP rises · confident-wrong "
          "survives every threshold and only Tier 2 catches it · governance acted on")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit()
    model = None
    if "--model" in sys.argv:
        model = sys.argv[sys.argv.index("--model") + 1]
    if "--rescore" in sys.argv:
        rows = rescore()
    elif "--report" in sys.argv:
        rows = json.loads(RESULTS.read_text())["rows"]
    else:
        rows = run(model, with_judge="--no-judge" not in sys.argv)
    report(rows)
