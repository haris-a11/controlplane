# Measured results

`32` questions against the policy corpus, model `openrouter/meta-llama/llama-3.1-8b-instruct`.
Every question was verified at Tier 1 so decisions can be replayed at any
threshold. Numbers are from one run on a 32-question set — directional, not
a benchmark.

- Raw model accuracy before any checking: **84%**
- Tier 0 + Tier 1 checking cost: median **706 ms**, p95 **792 ms**
- Log-probabilities available from this provider: **no — Tier 0 degraded**

## At each use case's shipped thresholds

| Use case | Flag rate | False positive | False negative | Escalation | Abstained |
|---|---|---|---|---|---|
| `support` | 9% | 4% | 60% | 6% | 41% |
| `copilot` | 6% | 0% | 60% | 6% | 41% |
| `decision_support` | 19% | 15% | 60% | 6% | 41% |

## The tradeoff

Raising the block threshold catches more wrong answers and flags more right
ones. There is no setting that does both. This is the dial in the console.

| Block threshold | Flag rate | False positive | False negative |
|---|---|---|---|
| 0.00 | 6% | 0% | 60% |
| 0.18 | 6% | 0% | 60% |
| 0.36 | 9% | 4% | 60% |
| 0.54 | 19% | 15% | 60% |
| 0.72 | 59% | 59% | 40% |
| 0.90 | 59% | 59% | 40% |

## Same checker, different models

The checker is unchanged across these runs; only the model under
test differs. A weaker model produces more for it to catch — which is
the argument for downgrading a model *and* the reason cost belongs in
the same decision as risk.

| Model | Accuracy | Flagged | False positive | False negative | Abstained |
|---|---|---|---|---|---|
| `llama-3.1-8b-instruct` | 84% | 9% | 4% | 60% (5 wrong) | 41% |
| `llama-3.3-70b-instruct` | 97% | 6% | 6% | 100% (1 wrong) | 44% |

Rates over a handful of wrong answers are not stable estimates — the counts are shown for that reason.


## Where it degrades

Findings from the runs above, including the ones that did not work.

**A wrong answer can score as well grounded.** `Since cataract has no waiting period, can I claim in month 2?` was answered
incorrectly and still scored **0.90** grounding — above the
release threshold for every profile. The question asserts a false premise, the
model agreed with it, and the agreement is phrased in language the source
supports. Groundedness measures whether an answer *echoes* the source, not
whether it is *true*. This is the structural limit of the approach and no
threshold setting fixes it — only a second, differently-shaped check would.

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
| Fast-path p95 | < 150 ms | **792 ms** (checks only) |
| Verification share | 12% of traffic | 100% — see below |

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
