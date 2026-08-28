# Architecture

How a request flows, what each check sees, what runs in parallel, and what is stored.
Companion to the [README](../README.md); this is the detail that would clutter it.

---

## 1. Where the checker sits

The brief asks for a choice between a pre-response gate, inline middleware, and a
post-hoc audit. This is **inline middleware**, exposed as an OpenAI-compatible endpoint.

```
your app  ──▶  http://controlplane/v1/chat/completions  ──▶  any LiteLLM provider
                              │
                              └── checks, decides, acts, logs
```

The consequence that matters: an application already speaking the OpenAI protocol is
checked by changing a base URL. Nothing in the calling app knows the layer exists, except
that the response carries an extra `controlplane` block. That is what makes
model-agnosticism demonstrable rather than a claim on a slide.

Post-hoc audit is not a separate mode — it falls out, because the router has to log its
decisions anyway. The dashboard and the tuning console both read that same log.

A true pre-response *gate* is only used where policy demands it: `decision_support` sets
`allow_repair = false`, so an unverifiable answer is held rather than released and
corrected. Everything else releases and annotates.

---

## 2. The request path

```
 1  question
        │
 2      ├──▶ rag.search()                    governance-tagged spans, k=4
        │
 3      ├──▶ llm.complete()                  the answer, + logprobs if the provider gives them
        │
 4      ├──▶ ┌─ checks.tier0()  ~2 ms  ─┐    gathered — neither needs the other
        │    └─ checks.tier1()  ~500 ms ┘
        │
 5      ├──▶ router.needs_tier1()            was Tier 1's result allowed to count?
        │
 6      ├──▶ router.ungoverned_only()        does any GOVERNED span support this?
        │
 7      ├──▶ router.needs_judge()  ──▶ judge.judge()   only if it can change the outcome
        │
 8      ├──▶ router.decide()                 pure. (action, reason)
        │
 9      ├──▶ router.apply()                  timed separately as action_ms
        │
10      └──▶ audit.log()                     one hash-chained row
```

### Step 4 — what actually runs in parallel

Tier 0 and Tier 1 both need the finished answer, and neither needs the other, so they are
`asyncio.gather`-ed onto threads. Every response records `check_ms` (wall clock) beside
`sequential_ms` (the sum of the parts).

**The measured saving is about 2 ms**, because Tier 0 is regex plus one indexed SQLite
read. This is reported rather than quietly dropped. The structure earns its place by
letting a second *expensive* detector be added later without adding its latency — not by
being faster today.

Tier 2 is not in the parallel section: its result depends on nothing the other tiers
produce, but *whether to run it at all* depends on both, and paying for a model call that
the gate would have skipped is the opposite of the point.

### Step 4→5 — Tier 1 runs speculatively

Tier 1 is started before the router decides whether it is needed, and its result is
discarded if `needs_tier1` says no. Deciding first would serialise the two tiers and give
back the parallelism. The trade is CPU on responses that did not need it — acceptable
because HHEM is a 110M classifier, and it would not be acceptable for Tier 2.

### Step 9 — acting is not checking

`check_ms` covers steps 4–8. `action_ms` covers step 9 alone, because a `repair` is a
second generation — measured at 5–50 s on a free-tier endpoint. Folding that into the
checking budget would report the provider's queue as our overhead and make the latency
budget meaningless on exactly the responses that matter most.

---

## 3. What each check sees

| Check | Input | Output | Cost |
|---|---|---|---|
| `tier0` | question, answer, session history | mean/min logprob, PII hits, re-ask, abstention, premise assertion | free |
| `tier1` | answer, retrieved spans | grounding score per span, best span index | ~500 ms CPU |
| `ungoverned_only` | spans + their governance tier + span scores | which unowned sources are the *only* support | microseconds |
| `judge` | question, answer, spans **tagged by governance tier** | verdict + confidence + reason | one model call |

The governance tags on the judge's input are load-bearing. Without them the judge scored
both stale-source failures as `supported` at confidence 1.0 — correctly, on the question
it was actually being asked. It was reading the same contaminated context the answering
model read.

---

## 4. The escalation gates

Each gate returns `(bool, reason)`, and the reason is logged. Knowing *why* a tier ran
matters as much as whether it did — it is what makes the tier-share numbers interpretable
and what the feedback loop counts.

### `needs_tier1`

| Reason | Condition |
|---|---|
| `policy` | `tier1_always` |
| `triggered` | PII present, re-ask, or mean logprob below `tier0_trigger_logprob` |
| `no_logprobs` | the provider withheld them — do not assume confidence you cannot measure |
| `audit_sample` | random, at `audit_sample_rate`. **The load-bearing one.** |
| `skipped` | none of the above |

### `needs_judge`

Ordered, and the order is the design.

| Reason | Condition | Why here |
|---|---|---|
| `disabled` | `judge_enabled = false` | — |
| `asserted_premise` | Tier 0's `PREMISE` regex fired | **Before the abstention gate.** A model that agrees with a false premise while hedging reads as a refusal to the regex. |
| `abstained` | the answer declined | nothing for a judge to arbitrate |
| `policy` | `judge_always` | regulated routes |
| `ungoverned_source` | only an unowned source supports the answer | provenance doubt is the one signal that fires on a confident, well-grounded, wrong answer |
| `unchecked` | Tier 1 did not run | no score to arbitrate |
| `ambiguous_band` | `judge_band_lo ≤ grounding ≤ judge_band_hi` | the score is not evidence either way |
| `tier_disagreement` | confident but ungrounded, or hesitant but well grounded | two cheap checks disagreeing is the cheapest signal that a third is worth paying for |
| `skipped` | none of the above | |

**The gate is deliberately not built on "does this answer look risky".** The failure Tier
2 exists for scores 0.92–0.97 at Tier 1 with confident log-probabilities. Looking fine is
what a confident hallucination *is*.

---

## 5. The decision

`router.decide(signals, grounding, policy, verdict, stale_sources) -> (action, reason)`

**Pure.** No I/O, no clock, no randomness. That is what lets the tuning console replay
real decisions at any threshold, the eval sweep re-score a stored run, and the chat page
show what the same answer would do under another profile — none of which costs a model
call. Do not reach into I/O from it.

Branch order, and what kind of claim each one is:

| # | Branch | Kind of claim | Outcome |
|---|---|---|---|
| 1 | PII present | a **leak** — settled before any question of correctness | `redact` / `block` (per `pii_action`) |
| 2 | judge says `contradicted` / `false_premise` | a **second opinion overriding the first** | `repair` / `block` |
| 3 | answer abstains | a **refusal**, which is often the correct answer | `abstain` (released) |
| 4 | only ungoverned sources support it | a **provenance** problem, invisible to any score | per `ungoverned_action` |
| 5 | grounding below `grounding_block` | **no support** | `repair` / `block` |
| 6 | grounding below `grounding_annotate` | **weak support** | `annotate` |
| 7 | otherwise | | `pass` |

Two orderings are worth defending:

- **PII first.** Every other branch is a judgement about correctness; this one is a leak,
  and a leak inside an answer that happens to abstain is still a leak.
- **Judge above abstention.** A hedged agreement with a false premise — *"cataract may
  not have a specified waiting period"* — matches the abstention regex, and releasing it
  unexamined is how the measured case escaped. A genuine refusal draws `not_in_source`
  from the judge, not `contradicted`, so real abstentions still reach branch 3.

---

## 6. The policy layer

[`policies.toml`](../policies.toml). Three profiles, twelve knobs. **Nothing in
`controlplane/` branches on a use-case name** — the three behaviours come from this file
alone, which is what `POST /api/replay/{id}?use_case=…` demonstrates by re-deciding a
stored response under a different profile with no model call.

| Knob | Trades away |
|---|---|
| `latency_budget_ms` | measured against **added** latency, not end to end |
| `tier1_always` | CPU on everything, vs. missing what Tier 0 passed |
| `tier0_trigger_logprob` | escalation volume |
| `grounding_block` / `grounding_annotate` | the main dial — false positives against false negatives |
| `judge_enabled` / `judge_always` / `judge_band_lo` / `judge_band_hi` | spend against semantic coverage |
| `pii_action` | `redact` keeps the answer useful; `block` is for regulated routes |
| `ungoverned_action` | `allow` trusts staff to know a wiki is a wiki |
| `allow_repair` | a machine re-issue vs. a human always seeing it |
| `audit_sample_rate` | CPU against being able to measure your own false negatives |

Thresholds are client-set per deployment, not product behaviour, because regulation
varies by geography and sector and hard-coded rules age badly.

---

## 7. Data model

### `decisions` — one row per response

Columns are the things that get filtered, aggregated or sorted; everything else lives in
the `record` JSON blob. Schema changes are forward migrations (`ALTER TABLE`, ignoring
"duplicate column"), because the log is a deliverable and recreating it is not an option.

| Column | Why a column and not JSON |
|---|---|
| `ts`, `use_case`, `user_id`, `decision` | every dashboard filter |
| `latency_ms`, `check_ms` | percentiles, which need an ordered column |
| `cost_usd`, `check_cost_usd` | summed for the overhead figure |
| `tier_path` | `"0>1>2"` — which tiers ran |
| `simulated` | separates generated volume from measured traffic |
| `prev_hash`, `row_hash` | the chain |
| `record` | signals, grounding, spans, judge verdict, both answers |

### The hash chain

```
row_hash[n] = sha256( row_hash[n-1] + "ts|use_case|user_id|decision|record_json" )
```

`verify_chain()` recomputes every hash and returns `(ok, rows_checked, first_bad_id)`.
Editing or deleting any row breaks it — the audit self-test proves this by tampering with
a row and asserting the chain notices.

Appends take a process-local lock: reading the previous hash and inserting must not
interleave, or two rows claim the same predecessor and the chain is rejected when nothing
was actually tampered with. `log_many()` chains a batch in memory inside one transaction,
which is what makes a 40,000-row simulation take two seconds instead of minutes.

This is the [EU AI Act Article 12](https://artificialintelligenceact.eu/article/12/)
record-keeping story, enforceable from 2 August 2026. Rows before the migration have a
null `row_hash` and are skipped; the chain starts after them.

### `feedback` — one row per human verdict

`decision_id`, `reviewer`, `verdict` (`agree` / `false_positive` / `false_negative`),
`corrected_answer`, `note`. Deliberately a separate table: the decision log is
append-only and tamper-evident, and a reviewer's later opinion must not mutate the record
of what the system did at the time.

---

## 8. The feedback loop

```
   held responses ──▶ review queue ──▶ reviewer verdict ─┐
                                                         ├──▶ suggest_threshold()
   audit-sampled traffic ──▶ FN estimate ────────────────┤        │
                                                         │        ▼
   tier disagreements ───────────────────────────────────┘   a recommendation
                                                             a human applies
```

`suggest_threshold` grid-searches `grounding_block` against reviewer verdicts, scoring
disagreement about **holding** rather than about flagging — a reviewer only ever sees the
escalation queue, so their verdict is a statement about `repair`/`block`. An `annotate` is
a release with a hedge on it, and nobody was asked to rule on those.

The two thresholds move together at their shipped distance, the same way the console's
sweep moves them, so the recommendation is reachable by editing one number.

**No detector is retrained.** The loop produces labelled data, counts disagreement, and
recommends a threshold. Claiming to have improved a model from forty labels would be a lie
and an easy one to catch.

---

## 9. The console

Three pages, one CSS file, one JS file, no framework and no build step. The charts are
hand-written SVG and stacked `<div>`s; a charting library would be a dependency carrying
two charts.

| Page | Reads | Shows |
|---|---|---|
| `/chat` | `POST /v1/chat/completions` | The live request path with a decision inspector: every Tier 0 signal, per-span grounding with governance badges, the judge's verdict, the action and why, latency and cost breakdown, and a replay-under-another-profile control |
| `/dashboard` | `/api/metrics`, `/api/users`, `/api/traffic`, `/api/queue`, `/api/learning`, SSE `/api/events` | Volume at enterprise scale, per-use-case against each budget, per-user monitoring, live traffic, the full audit record with chain verification, the review queue, and what the loop has learned |
| `/console` | `/api/sweep`, `/api/score` | The false-positive / false-negative dial, replayed through the real `decide()` |

The tuning page reads `eval/results.json` — a labelled set, where false positives and
negatives are *knowable*. The dashboard reads the audit log — production traffic, where
they are not, which is why the audit-sample estimate exists.

`/api/events` polls the log's high-water mark on a 1.5 s loop rather than using a message
bus. At tens of thousands of rows a week a bus would be infrastructure with nothing to
carry.

---

## 10. Known structural limits

- **No output-layer check can catch a retrieval failure.** If the right span was never
  fetched, the answer is faithful to what the model was shown. The evidence needed to
  notice never arrives. Measured and documented in `RESULTS.md`.
- **Groundedness measures echo, not truth.** This is why Tier 2 and the governance tier
  exist, and neither fully closes it.
- **Risk categories overlap.** A fabricated detail about a person is both a hallucination
  and a privacy event. The branch order in §5 is a decision about which one wins, not a
  claim that they are separable.
- **The evaluation set is 40 questions.** Rates over four or five wrong answers are not
  stable estimates. Counts are printed beside every rate for that reason.
