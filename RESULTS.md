# Measured results

`40` questions against the policy corpus, model `openrouter/meta-llama/llama-3.3-70b-instruct`.
Every question was verified at Tier 1 and Tier 2 so decisions can be replayed
at any threshold. Numbers are from one run on a 40-question set —
directional, not a benchmark.

- Raw model accuracy before any checking: **88%**
- Tier 0 + Tier 1 checking cost: median **527 ms**, p95 **837 ms**
- Log-probabilities available from this provider: **yes**

## At each use case's shipped thresholds

Same engine, same run, three policy profiles. Nothing but `policies.toml`
differs between these rows.

| Use case | Flag rate | False positive | False negative | Escalation | Abstained | Tier 2 share |
|---|---|---|---|---|---|---|
| `support` | 32% | 26% | 20% | 20% | 38% | 38% |
| `copilot` | 32% | 26% | 20% | 12% | 38% | 38% |
| `decision_support` | 42% | 37% | 20% | 32% | 38% | 65% |

## Ablation: which check is doing the work

The same stored run, re-scored with Tier 2 and the governance check
switched off independently. No new model calls — the same answers,
decided differently. Profile `support`, at its shipped thresholds.

| Tier 2 judge | Governance tier | False negative | False positive | Flag rate |
|---|---|---|---|---|
| on | on | 20% | 26% | 32% |
| on | off | 60% | 26% | 28% |
| off | on | 40% | 26% | 30% |
| off | off | 80% | 26% | 25% |

Neither check on, **80%** of wrong answers are released; with both, **20%**.

**Both checks earn their place: each catches wrong answers the other**
**misses.** Governance alone leaves 40% of wrong answers
released and the judge alone 60%; together, 20%.

Judge verdicts across the set: `contradicted` 4, `not_in_source` 1, `supported` 21.


## Answers grounded only in a loosely governed source

**4 of 40** answers were supported by a span from
`corpus/ungoverned/` with no governed span behind them; **2**
of those were wrong. This is the failure groundedness cannot see: the
answer is faithful to what it was shown, and what it was shown is stale.
The `ungoverned_action` knob is what acts on it — `annotate` for support,
`allow` for the internal copilot, `block` on the regulated route.


## The tradeoff

Raising the block threshold catches more wrong answers and flags more right
ones. There is no setting that does both. This is the dial in the console.

| Block threshold | Flag rate | False positive | False negative |
|---|---|---|---|
| 0.00 | 25% | 20% | 40% |
| 0.18 | 28% | 23% | 40% |
| 0.36 | 32% | 26% | 20% |
| 0.54 | 42% | 37% | 20% |
| 0.72 | 62% | 60% | 20% |
| 0.90 | 62% | 60% | 20% |

## Same checker, different models

The checker is unchanged across these runs; only the model under
test differs. A weaker model produces more for it to catch — which is
the argument for downgrading a model *and* the reason cost belongs in
the same decision as risk.

| Model | Accuracy | Flagged | False positive | False negative | Abstained |
|---|---|---|---|---|---|
| `llama-3.1-8b-instruct` | 62% | 38% | 16% | 27% (15 wrong) | 32% |
| `llama-3.3-70b-instruct` | 88% | 32% | 26% | 20% (5 wrong) | 38% |

Rates over a handful of wrong answers are not stable estimates — the counts are shown for that reason.


## Where it degrades

Findings from the runs above, including the ones that did not work.

**A wrong answer can score as well grounded.** `What is the ICU charge cap?` was answered
incorrectly and still scored **0.97** grounding — above the
release threshold for every profile.
The supporting span came from `support-macros.txt` — a source
with no owner that contradicts the governed policy. The answer is
faithful to what it was shown; what it was shown was wrong.
Groundedness measures whether an answer *echoes* the source, not
whether it is *true*. No threshold setting fixes this; only a differently
shaped check does, which is what Tier 2 and the governance tier are for.

**Abstention had to be separated from groundedness.** An answer that declines —
"the policy does not mention ambulance charges" — is by definition unsupported by
the document, and scored 0.03–0.52. Treating that as a grounding failure flagged
correct refusals at a **42% false-positive rate**. Detecting abstention at Tier 0
and releasing it as its own outcome brought that to **6%** on the same data. The
brief notes that risk categories overlap; this is where that bites in practice.

**Retrieval strength did not detect false refusals.** A refusal is wrong when the
source *does* cover the question. We tried gating abstention on retrieval score, on
the theory that a strong span in context makes a refusal suspect. It did not
discriminate: the one genuinely wrong refusal scored 0.271, below eleven correct
ones. It was a retrieval failure — the span was never fetched — so the answer was
faithful to what the model was shown. **No output-layer checker can catch this**;
the evidence it would need never arrived. The knob was removed rather than shipped.

**Log-probability availability varies by provider, not just by vendor.** Within a
single OpenRouter account, one model returned logprobs and another did not. Tier 0
degrades to its non-logprob signals and the router escalates rather than assuming
confidence, but the assumption that a deployment has one known capability is wrong.

## Targets vs measured

| Metric | Round 1 target | Measured |
|---|---|---|
| Fast-path p95 | < 150 ms | **837 ms** (checks only) |
| Verification share | 12% of traffic | 100% — see below |
| Cost overhead, Tier 0+1 only | ~3% of inference spend | **6.7%** |
| Cost overhead, with Tier 2 on every row | ~3% | **113%** |

**The cost target is the right order of magnitude for the cheap tiers and**
**collapses the moment a second model call is involved.** Tier 0 and Tier 1
together add **6.7%** — about 2x the ~3% target, though still small enough that nobody re-plans a budget around it, because a
110M CPU classifier is nearly free next to a generation. Judging every
response instead costs **113%**: the judge re-reads the whole retrieved
context to produce one word of verdict, so it roughly doubles the spend on
any response it touches.

That is the economic argument for the router stated as a number rather than a
claim: detection is cheap and can run on everything, while a second opinion has
to be *aimed*. It is also why `judge_always` is set only on the regulated
profile, where the cost of being wrong dwarfs the cost of asking twice.

Cost basis: this provider publishes no per-token pricing that LiteLLM knows,
so spend is **estimated from token counts** at the rates in `controlplane/llm.py`
rather than reported by the provider. Which mode each number came from is
recorded per response. Cost visibility varies by provider the same way
log-probability availability does — that is worth knowing before a deployment
promises a finance team a number.

**The latency target is missed by roughly 5x.** Tier 1 is a T5-base
encoder on CPU in float32, scoring four spans per request. The target assumed
the check was cheaper than it is. Honest paths: ONNX or int8 quantisation,
batching across concurrent requests, scoring only the top two spans, or a GPU.
None were attempted here — the number is reported as measured, not tuned
toward the slide.

Tier 1 turned out cheap enough to run on everything: HHEM-2.1-Open is a
110M classifier on CPU. The 12% budget assumed sub-agent LLM calls. The
scarce resource is the *action* — repair costs a second generation, and a
block costs human time — not the detection. The escalation column above is
the number that matters, and it is well under 12%.
