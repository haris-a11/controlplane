# ControlPlane.ai — Round 2 prototype

Team Fremen · Accenture Innovation Challenge 2026 · Track 1.

A checking layer that sits between an enterprise application and the LLM, inspects every
response for *confidently wrong*, *quietly expensive* and *quietly unsafe*, and decides
per request how much checking that response is worth and what to do about the result.

Status: **working end to end, and measured.** Numbers in [`RESULTS.md`](RESULTS.md) —
including the ones that missed their target and the mechanism that turned out not to
pay for itself.

---

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # add your key
uvicorn controlplane.app:app
```

Then, in another terminal, fill the log so the dashboard has something to show:

```bash
python -m demo.simulate_week               # ~40k simulated interactions over 7 days
python -m demo.seed_traffic 12             # real calls across all three profiles
python -m eval.run_eval                    # the measured numbers → RESULTS.md
```

Three pages, one app:

| | | |
|---|---|---|
| **Chat** | http://localhost:8000/chat | Ask a question and watch every check run on the answer |
| **Dashboard** | http://localhost:8000/dashboard | Per-user traffic monitoring, review queue, feedback loop |
| **Tuning** | http://localhost:8000/console | The false-positive / false-negative dial |

Or drive it as a plain OpenAI-compatible endpoint — any app already speaking that
protocol is checked without knowing it:

```bash
curl localhost:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-controlplane-use-case: support' \
  -H 'x-controlplane-user: u_demo' \
  -d '{"messages":[{"role":"user","content":"When is maternity covered?"}]}'
```

The response carries a `controlplane` block alongside the usual OpenAI fields, with the
full decision path: signals, tier path, grounding per span, judge verdict, action,
latency breakdown and cost.

---

## What the brief asked for, and where each piece is

The Round 2 brief lists six solutioning areas. This table is the direct answer: what is
built, the file it is in, and what is deliberately not built.

### 1. Detection techniques

| Technique | Built? | Where |
|---|---|---|
| Rule-based heuristics | ✅ | [`checks.py`](controlplane/checks.py) — PII patterns (`PII_PATTERNS`), abstention detection (`ABSTAIN`), **premise detection** (`PREMISE`), re-ask within 60 s (`is_reask`) |
| Statistical / uncertainty signal | ✅ | [`llm.py:logprob_stats`](controlplane/llm.py) — mean and minimum token log-probability, with graceful degradation when the provider withholds them |
| Retrieval verification against source | ✅ | [`checks.py:tier1`](controlplane/checks.py) — Vectara HHEM-2.1-Open scores the answer against each retrieved span separately, best span wins |
| Secondary AI-as-judge | ✅ | [`judge.py`](controlplane/judge.py) — a second model returns a *verdict* (`supported` / `not_in_source` / `contradicted` / `false_premise`), not a score |
| **Data-provenance check** *(not in the brief's list)* | ✅ | [`router.py:ungoverned_only`](controlplane/router.py) — is any **governed** source backing this answer? |
| Dedicated PII/entity detection (Presidio) | ❌ | Regex covers the identifier classes in this corpus. Presidio adds spaCy and a ~500 MB download to a clean clone. |
| Embedding anomaly detection | ❌ | Not built. |

### 2. Decision logic

| | Where |
|---|---|
| Confidence scoring | Grounding score per span, judge confidence, token log-probabilities — all recorded, none of them collapsed into a single number |
| Tiered responses | **Six** outcomes, not four and not a binary gate: `pass` · `abstain` · `annotate` · `redact` · `repair` · `block` — [`router.py:decide`](controlplane/router.py) |
| When a human is pulled in | `block` and un-repairable `repair` land in the review queue; `allow_repair = false` on the regulated profile means doubt always reaches a person |
| Escalation rules | [`router.py:needs_tier1`](controlplane/router.py) and `needs_judge` — each returns *why*, and the reason is logged |

`decide()` is a **pure function**. That is load-bearing: it is what lets the tuning
console replay real decisions at any threshold, the eval sweep re-score a run, and the
chat page show "what this same answer would have done under another profile" — all
without paying a model again.

### 3. Architecture

| | |
|---|---|
| Where the checker sits | Inline middleware, as an OpenAI-compatible proxy — [`app.py`](controlplane/app.py). Model-agnosticism is *demonstrated* by pointing any client at it, not claimed. |
| Parallel checks | Tier 0 and Tier 1 are `asyncio.gather`-ed; every response logs `check_ms` next to `sequential_ms`. **Measured saving: ~2 ms.** Reported rather than dropped — see "Things that did not work" below. |
| Cost/latency accounting | `check_ms` (checking) is separated from `action_ms` (acting), because a repair is a second generation and charging that to the checker's budget would make the budget meaningless |

### 4. Governance

| | Where |
|---|---|
| Configurable policy layer | [`policies.toml`](policies.toml) — three profiles, twelve knobs each. **Nothing in `controlplane/` branches on a use-case name**; the three behaviours come from this file alone. |
| Varying by risk appetite | `support` redacts PII and hedges; `copilot` only flags; `decision_support` blocks, never repairs, and judges everything |
| Audit trail | [`audit.py`](controlplane/audit.py) — one row per response with every signal, plus a **SHA-256 hash chain** so the log can be shown not to have been edited after the fact (`verify_chain`). The dashboard shows it verifying live. |
| Data governance | Corpus split into `corpus/governed/` and `corpus/ungoverned/`, with the tier carried on every chunk and acted on by `ungoverned_action` |

### 5. Feedback loops

[`learning.py`](controlplane/learning.py), surfaced in the dashboard. Three inputs, each
a different kind of label:

1. **Reviewer verdicts** — the queue of held responses; a human marks each `agree` /
   `false_positive` / `false_negative`.
2. **Audit-sampled traffic** — `audit_sample_rate` sends a random slice of responses
   Tier 0 already *passed* through full verification anyway. This is the only unbiased
   false-negative estimate available in production, where there is no answer key.
3. **Tier disagreements** — a confident answer that scored badly, a hesitant one that
   scored well, or the judge overturning a high grounding score. Free labels, no
   reviewer needed.

Those become a **suggested threshold** (`suggest_threshold`), shown next to the shipped
one. It is a recommendation for a human to apply in `policies.toml`, never auto-applied
— and no detector is retrained. A prototype claiming to have improved a model from forty
labels would be lying.

### 6. Metrics & monitoring

[`eval/run_eval.py`](eval/run_eval.py) → [`RESULTS.md`](RESULTS.md), and the dashboard
for live traffic. False positive and false negative rates at three dial settings and in
a full sweep; an **ablation** isolating what each check contributes; measured latency and
cost against the Round 1 targets; and the audit-sample false-negative estimate with its
sample size always stated beside it.

---

## The core mechanism

Every response is checked. How *much* checking it gets is a per-request decision.

| Tier | Runs on | Cost | What it looks at |
|---|---|---|---|
| **0** | 100% | free | Token log-probabilities the model already produced, PII patterns, re-ask within 60 s, abstention, premise detection. No model call. |
| **1** | policy-dependent | ~free, CPU | HHEM-2.1-Open (110M) scores the answer against each retrieved span. |
| **2** | measured 22–38% on `support` | expensive | A second model returns a verdict. Gated — see below. |

### Why Tier 1 also runs on traffic Tier 0 passed

`audit_sample_rate` sends a random slice of *passed* responses through full verification
anyway. You cannot measure a false-negative rate by looking only at what you flagged.
The same sampling defends against a cheap first tier being gamed by input crafted to look
easy ([arXiv 2605.17288](https://arxiv.org/html/2605.17288v1)), and the disagreements it
surfaces are the labelled data the router learns from. One mechanism, three jobs.

### Why `abstain` is its own outcome

An answer that declines — *"the policy does not mention ambulance charges"* — is by
definition unsupported by the document, and scores near zero for groundedness. Treating
that as a failure flagged correct refusals at a **42% false-positive rate**; separating it
brought that to 6% on the same data. Measuring is what surfaced it.

"Cannot verify" is reported separately from "verified false" throughout. The brief notes
there is often no real-time ground truth; collapsing that into a binary is the common
shortcut and this does not take it.

### Why the Tier 2 gate is not built on the grounding score

The failure Tier 2 exists for scores **well** at Tier 1. `"Since cataract has no waiting
period, can I claim in month 2?"` was answered wrongly at **0.90** grounding: the question
smuggles in a false premise, the model agreed, and the agreement is phrased in language
the source supports. Groundedness is not mistaken there — it is measuring something else.

So the gate does not ask how confident the answer looks. It fires on:

- the **question asserting a premise** (a free Tier 0 regex — cheap to spot the
  assertion, expensive to judge whether it is false, so each is done where it is cheap);
- the **supporting span being ungoverned** (provenance doubt);
- Tier 0 and Tier 1 **disagreeing**;
- the ambiguous grounding band, or policy.

---

## Reference parameters and assumptions

Stated explicitly, per the brief, and sized to the reference parameters it gives.

| Assumption | Value | Where it comes from |
|---|---|---|
| Interactions per week | **~40,000** across all use cases | Brief: "tens of thousands per week". `demo/simulate_week.py` |
| Distinct users | ~250, Zipf-weighted so a few generate most traffic | A uniform per-user table answers no question worth asking |
| Use cases | 3 — customer support (65%), internal copilot (28%), decision support (7%) | Brief's three named examples; the mix is ours |
| Latency tolerance | 150 ms / 400 ms / 2000 ms of **added** latency | Different risk and latency tolerance per use case |
| Data sources | Mixed governance: 1 governed policy document, 2 loosely governed internal sources | Brief: "a mix of well-governed and loosely governed internal data sources" |
| Model pricing | $0.20 / $0.60 per 1M tokens when the provider publishes none | `controlplane/llm.py`, overridable by env var |
| Checker compute | ~$0.05/hour for a small always-on CPU instance | `controlplane/app.py` |

**On the simulated traffic.** 40,000 real model calls would cost money and hours and
demonstrate nothing that one call does not, so the *volume* is simulated and every such
row is flagged `simulated` in the database, in the API, and with a badge in the
dashboard. What is simulated is the traffic **shape** — who asked, when, under which
profile. The **decisions are real**: each row is produced by calling the actual
`needs_tier1` / `needs_judge` / `decide` over signals and grounding scores measured in
`eval/results.json`. Measured numbers in `RESULTS.md` come from the eval run and never
from the simulation.

---

## Where the ideas come from

Everything except the router already ships as a mature product — the scan in
[`controlplane-prior-art.html`](controlplane-prior-art.html) maps 63 of them. Composing
rather than rebuilding is the deliberate choice, and it is stated here rather than left
for a judge to discover.

| Idea | Prior art | What was taken | What is different here |
|---|---|---|---|
| Trust score on every response, no second model call | **Cleanlab TLM** | The shape of Tier 0 — a cheap per-response signal from what the model already produced | Ours degrades explicitly when a provider withholds logprobs, and records which mode it ran in |
| Uncertainty from black-box APIs | **arXiv [2509.04492](https://arxiv.org/abs/2509.04492)**, **[2511.07694](https://arxiv.org/abs/2511.07694)** | The method reference for using only the few log-probabilities an API exposes | Implemented as mean + minimum rather than full entropy — the minimum separates "one shaky token" from "uniformly unsure" |
| Claim-to-source verification | **Vectara HHEM-2.1-Open** | Used directly. 110M, Apache-2.0, CPU. This is Tier 1. | Weights loaded into a stock T5 so the repo never asks a reviewer to `trust_remote_code`, and never breaks on a transformers upgrade |
| Fine-tuned RAG-faithfulness judge | **Patronus Lynx** / HaluBench | The pattern: verdict with reasoning, not a score | We call a general model — a repo that downloads 8B of weights to run a demo is a repo nobody runs |
| Hallucination is not one failure mode | **Amazon RefChecker** | The idea of a verdict *taxonomy* | Our four verdicts include `false_premise`, which the grounding score structurally cannot express |
| Action ladder beyond a binary gate | **Guardrails AI**, **NVIDIA NeMo Guardrails** | Pass / annotate / repair / block | Six outcomes, with `abstain` and `redact` split out because they are different claims about the answer |
| The middleware seat | **LiteLLM**, **Portkey** | Used directly, via LiteLLM. No provider adapters were written. | — |
| Per-response audit trail | **Langfuse**, **Fiddler** | One row per decision with the full signal set | A SHA-256 hash chain over the rows, so the log is tamper-evident (EU AI Act Art. 12) |
| Cheap → small → heavy cascade with a per-route latency budget | **arXiv [2510.19877](https://arxiv.org/pdf/2510.19877)**, *Policy-Checked RAG with Cryptographic Receipts* | This paper independently specifies the Round 1 architecture. Cited prominently on purpose. | This work is an implementation and empirical evaluation of that idea, not its invention |
| Cascade gaming | **arXiv [2605.17288](https://arxiv.org/html/2605.17288v1)** | Tiered systems can be attacked with input crafted to look easy to the cheap tier | Random audit sampling is the standard mitigation, and it is why `audit_sample_rate` exists |

**What is not borrowed**, because nothing in the scan does it:

1. **The router itself** — a per-request decision about how much checking to spend, under
   client-set policy, with the cost of that decision recorded.
2. **Cost joined to the risk decision**, rather than reported in a separate billing view.
3. **The tuning tradeoff as the product surface** — the console exposes the dial instead
   of hiding a chosen threshold.
4. **The governance tier as a first-class check** — acting on *where* an answer's support
   came from, not only on how strongly it scored.
5. **`abstain` as a distinct outcome** — every product in the scan collapses "cannot
   verify" into "verified false".

---

## What the measurement found

Full detail in [`RESULTS.md`](RESULTS.md). The three findings worth reading:

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

---

## What it does not do

Named here rather than left for a reader to discover.

- **No streaming.** Checks run on the complete response. The pitched design gates the
  stream; that is not built.
- **No real-time bias measurement.** Bias needs a paired-prompt set compared across runs.
  It cannot be inferred from a single response and is not attempted inline. Naming it as
  roadmap is more honest than a plausible-looking inline check.
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

---

## Architecture

```
                    ┌──────────────────────── ControlPlane ────────────────────────┐
  client ──────────▶│                                                              │
  (any OpenAI       │  retrieve ──▶ LLM ──▶ ┌ Tier 0  signals   (free, 100%)  ┐    │
   -compatible app) │  governance-  (via     └ Tier 1  grounding (CPU, ~free)  ┘    │
                    │  tagged      LiteLLM)          │                             │
                    │                                ▼                             │
                    │                        provenance check                      │
                    │                                │                             │
                    │                                ▼                             │
                    │                        Tier 2  judge?   (gated, expensive)   │
                    │                                │                             │
                    │                                ▼                             │
                    │                     router.decide()  ◀── policies.toml       │
                    │                                │                             │
                    │      pass · abstain · annotate · redact · repair · block      │
                    │                                │                             │
  response ◀────────│                                ▼                             │
                    │                     audit log (hash-chained)                 │
                    └──────────────────────────────┬───────────────────────────────┘
                                                   │
                              chat inspector · dashboard · tuning dial · feedback loop
```

More detail, including the data model, in [`docs/architecture.md`](docs/architecture.md).

### Dependencies

| Layer | Choice | Why |
|---|---|---|
| Service | FastAPI + uvicorn | The OpenAI-compatible route is short, and async gives parallel checks for free |
| Model access | **LiteLLM** | Model-agnostic seat solved; no provider adapters written |
| Grounding | **Vectara HHEM-2.1-Open** | 110M, CPU, Apache-2.0 — the licence matters for a public repo |
| Retrieval | sentence-transformers MiniLM + brute-force cosine | One document and a few hundred chunks. A vector DB would be more moving parts than data. |
| Store | SQLite | 40k rows a week is nothing. Postgres would be unjustifiable. |
| Console | One CSS file, one JS file, three HTML pages | No build step, no framework, no charting library — the SVG is hand-written |

### Provider support

The model under test is set by `CONTROLPLANE_MODEL` and nothing else changes:

| Provider | `CONTROLPLANE_MODEL` | Key | Tier 0 logprobs | Cost reported |
|---|---|---|---|---|
| OpenRouter | `openrouter/meta-llama/llama-3.3-70b-instruct` | `OPENROUTER_API_KEY` | provider-dependent | estimated |
| OpenAI | `gpt-4o-mini` | `OPENAI_API_KEY` | yes | provider |
| Ollama (local) | `ollama/llama3.2` | none | version-dependent | estimated |
| Anthropic | `claude-sonnet-4-5` | `ANTHROPIC_API_KEY` | **no** — Tier 0 degrades | provider |

Both capabilities degrade explicitly rather than failing, and which mode a run used is
recorded on every response and shown in the console. Measured, and worth knowing: two
models behind the *same* OpenRouter account differ on logprob availability, so capability
is detected per call rather than configured.

---

## Corpus

```
corpus/
├── governed/     sample-policy.txt — the authoritative, versioned policy
└── ungoverned/   intranet-wiki-export.txt, support-macros.txt — real, internal, stale
```

The ungoverned files contradict the policy on seven points each. They are the reason a
groundedness score is not enough: an answer can be perfectly faithful to the source it was
given and still be wrong, because the source should not have been trusted. Full
contradiction table in [`corpus/README.md`](corpus/README.md).

Replace `corpus/governed/sample-policy.txt` with the Round 1 demo document before
recording the video. Files dropped loose in `corpus/` are treated as ungoverned —
untagged provenance is not a reason to trust something.

---

## Self-checks

Every module runs its own tests with no test framework:

```bash
python -m controlplane.rag        # chunking, retrieval, governance tagging
python -m controlplane.audit      # log round-trip, hash chain, tamper detection
python -m controlplane.checks     # Tier 0 signals
python -m controlplane.judge      # verdict parsing; --live to hit a real model
python -m controlplane.router     # all six outcomes, every escalation trigger
python -m controlplane.learning   # feedback → threshold recommendation
python -m eval.run_eval --selftest # the tradeoff curve behaves
```

Each writes to a temporary database, so running them leaves no fixture rows in the demo's
audit trail.

## Answer key

[`eval/answer_key.jsonl`](eval/answer_key.jsonl) — 40 questions in four classes:

- `supported` (21) — the policy answers it; a correct answer cites the right figure
- `not_covered` (8) — the policy is silent; the correct answer says so
- `contradicted` (7) — the question asserts something false; a correct answer pushes back
- `ungoverned` (4) — a loosely governed source contradicts the policy; the correct answer
  follows the policy

## Prior art

[`controlplane-prior-art.html`](controlplane-prior-art.html) maps 63 existing tools onto
this architecture. The contribution is [`router.py`](controlplane/router.py) and the
measurement around it; everything else is composed, and the table above says from what.
