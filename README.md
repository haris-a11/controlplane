# ControlPlane.ai

Team Fremen · Accenture Innovation Challenge 2026 · Track 1.

A checking layer that sits between an enterprise application and the LLM, inspects every
response for *confidently wrong*, *quietly expensive* and *quietly unsafe*, and decides
per request how much checking that response is worth and what to do about the result.

Status: **working end to end, and measured.** Numbers in the published evaluation results —
including the ones that missed their target and the mechanism that turned out not to
pay for itself.

## Solution architecture

```text
User / Enterprise Application
            │
            ▼
      Application + RAG
            │
            ▼
           LLM
            │
            ▼
      ┌───────────────┐
      │  ControlPlane  │
      │                │
      │ Tier 0 Signals │
      │ Tier 1 Verify  │
      │ Provenance     │
      │ Tier 2 Judge   │
      │ Policy Router  │
      └───────┬────────┘
              │
              ▼
 pass · abstain · annotate · redact · repair · block
              │
              ▼
       Audit + Feedback
```

ControlPlane operates as an OpenAI-compatible checking layer between an enterprise application and its model. It evaluates the response using progressively more expensive checks, applies the risk policy for that request, and records the resulting decision.

The system separates detection from action. Cheap signals run broadly, while expensive model-based verification is reserved for responses that warrant deeper scrutiny. Policy determines the acceptable tradeoff between false positives, false negatives, latency, and review capacity.

## Key differentiators

- Risk-aware, per-request checking rather than a single fixed guardrail.
- Six outcomes instead of a binary pass/block decision.
- Cost is recorded alongside the risk decision, connecting governance with economics.
- Data provenance is treated as a first-class risk signal.
- Random audit sampling estimates false negatives among responses that would otherwise pass.
- Policy can be tuned and decisions replayed without another model call.
- A hash-chained audit trail provides tamper-evident decision history.

## What the measurement found

Full detail in the published evaluation results. The three findings worth reading:

**Two checks aimed at the same failure, and an ablation to tell them apart.** Tier 2 and
the governance check both exist to catch answers that score *well* for groundedness. The
ablation switches each off independently over the same stored run — no new model calls —
and reports what each is worth. On the current run both earn their place: **80% of wrong
answers are released with neither, 20% with both**, and each alone gets only partway.

That result is **not stable across runs.** On an earlier run of the same set the
governance check reached the same false-negative rate on its own and the judge added
nothing but false positives. With five wrong answers in forty, one answer changing flips
the conclusion. `RESULTS.md` is regenerated from whatever the run actually produced and
states whichever conclusion the numbers support — which is the honest way to report an
n=40 result, and the reason every rate there is printed with its count beside it.

What did not change between those runs: the rows Tier 2 needs to catch score **0.92–0.97**
for groundedness with confident log-probabilities. No gate built on how the answer *looks*
will ever select them, which is why the Tier 2 gate is built on the question and on
provenance instead.

**The ~3% cost target survives the cheap tiers and collapses on the judge.** Tier 0 and
Tier 1 together add single-digit percent. Judging every response instead roughly doubles
the spend, because the judge re-reads the whole retrieved context to produce one word.
That is the economic case for a router, stated as a number.

**The latency target is missed by about 5x** and is reported as measured rather than
tuned toward the slide. Honest paths — ONNX or int8 quantisation, batching, scoring fewer
spans, a GPU — were not attempted.

**The same checker, unchanged, across two models.** On the same 40 questions,
`llama-3.1-8b` answers 62% correctly and `llama-3.3-70b` 88%. The checker catches 73% of
the small model's fifteen errors and 80% of the large model's five. This is the argument
for joining cost to the risk decision rather than reporting them separately: a cheaper
model is a defensible choice *if* you can see what it costs you in errors caught, and
that is a number nobody has unless something is checking.

### Things that did not work

Kept in the repo, because a prototype that names its own failures is worth more than one
that doesn't.

- **Parallel checks save ~2 ms.** Tier 0 and Tier 1 genuinely run concurrently, but Tier 0
  is regex and a dictionary lookup, so there was never much to overlap. The structure
  earns its place by letting a second expensive detector be added later without adding
  its latency — not by being faster today.
- **Retrieval strength does not detect false refusals.** A refusal is wrong when the
  source *does* cover the question. Gating abstention on retrieval score did not
  discriminate; the one genuinely wrong refusal scored below eleven correct ones. It was a
  retrieval failure, so the answer was faithful to what the model was shown, and no
  output-layer check can see that. The knob was removed rather than shipped as dead
  flexibility.
- **The judge, before it was told about provenance,** scored both stale-source failures
  as `supported` at confidence 1.0 — correctly, on the question it was being asked. It was
  reading the same contaminated context the answering model read. A judge is only as good
  as the evidence it is handed.
- **Taking the argmax span as "the source"** flagged 15 of 40 answers as ungoverned and
  pushed false positives from 4% to 23%, mostly on answers that were right and properly
  sourced. The question had to become "does *any* governed span support this?".

## What it does not do

Named here rather than left for a reader to discover.

- **No streaming.** Checks run on the complete response. The pitched design gates the
  stream; that is not built.
- **No real-time bias measurement.** Bias needs a paired-prompt set compared across runs.
  It cannot be inferred from a single response and is not attempted inline. Naming it as
  roadmap is more honest than a plausible-looking inline check.
- **Cost is observed, never acted on.** Spend is measured per response, joined to the
  decision record and reported everywhere — but nothing in `decide()` reads it. There is
  no budget cap, no spend-triggered model downgrade and no cost knob in `policies.toml`.
  Of the three risks the pitch names, *quietly expensive* is the one this prototype makes
  visible rather than controllable.
- **No multi-turn or agent-action risk.** This checks single responses; the brief's
  compounding-risk-across-turns concern is real and unaddressed.
- **PII detection is regex, not Presidio.** Adequate for the identifier classes in this
  corpus, not for production entity coverage.
- **No detector is trained or retrained.** The feedback loop produces labels and a
  threshold recommendation, and stops there.
- **No auth, multi-tenancy, or horizontal scale.** SQLite and one process.
- **The evaluation set is 40 questions.** Rates over four or five wrong answers are not
  stable estimates, and `RESULTS.md` prints the counts alongside every rate for that
  reason.
