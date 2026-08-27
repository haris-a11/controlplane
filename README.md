# ControlPlane.ai — Round 2 prototype

Team Fremen · Accenture Innovation Challenge 2026.
A checking layer between an enterprise application and the LLM.

Status: **working end to end.** Measured results in [`RESULTS.md`](RESULTS.md).

## The core mechanism

Every response is checked. How *much* checking it gets is a per-request decision:

| Tier | Runs on | Cost | What it looks at |
|---|---|---|---|
| 0 | 100% | free | Token log-probabilities the model already produced, PII patterns, re-ask within 60s. No model call. |
| 1 | policy-dependent | ~free, CPU | Vectara HHEM-2.1-Open (110M) scores the answer against each retrieved span. |
| 2 | small share | expensive | Repair — re-ground and re-issue — or hold for a human. |

Five outcomes, not a gate: **pass · abstain · annotate · redact · repair · block**.

`abstain` earns its place. An answer that declines — *"the policy does not mention
ambulance charges"* — is by definition unsupported by the document, and scores
near zero for groundedness. Treating that as a failure flagged correct refusals at
a **42% false-positive rate**; separating it brought that to **6%** on the same
data. Measuring this is what surfaced it.

Thresholds live in `policies.toml`, one profile per use case, because regulation
varies by geography and sector and hard-coded rules age badly.

### Why Tier 1 also runs on traffic Tier 0 passed

`audit_sample_rate` sends a random slice of *passed* responses through full
verification anyway. You cannot measure a false-negative rate by looking only at
what you flagged. The same sampling also defends against a cheap first tier being
gamed by input crafted to look easy ([arXiv 2605.17288](https://arxiv.org/html/2605.17288v1)),
and the disagreements it surfaces are the labelled data the router learns from.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then add your key
uvicorn controlplane.app:app --reload
```

Any provider LiteLLM supports works — the model under test is set by
`CONTROLPLANE_MODEL` in `.env` and nothing else changes:

| Provider | `CONTROLPLANE_MODEL` | Key | Tier 0 logprobs |
|---|---|---|---|
| OpenRouter | `openrouter/meta-llama/llama-3.3-70b-instruct` | `OPENROUTER_API_KEY` | provider-dependent |
| OpenAI | `gpt-4o-mini` | `OPENAI_API_KEY` | yes |
| Ollama (local) | `ollama/llama3.2` | none | version-dependent |
| Anthropic | `claude-sonnet-4-5` | `ANTHROPIC_API_KEY` | **no** — Tier 0 degrades |

Where logprobs are unavailable the router escalates to full verification instead
of assuming confidence. `RESULTS.md` records which mode the run used.

Point any OpenAI client at `http://localhost:8000/v1`:

```bash
curl localhost:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-controlplane-use-case: support' \
  -d '{"messages":[{"role":"user","content":"When is maternity covered?"}]}'
```

The response carries a `controlplane` block alongside the usual OpenAI fields:

```json
"controlplane": {"action": "annotate", "reason": "weakly_grounded",
                 "grounding": 0.52, "tier1_reason": "policy",
                 "check_ms": 38, "over_budget": false}
```

Switch profiles with the `x-controlplane-use-case` header:
`support`, `copilot`, or `decision_support`. Same engine, different behaviour.

`GET /decisions` returns the audit trail.

## Self-checks

```bash
python -m controlplane.rag      # chunking + retrieval
python -m controlplane.audit    # log round-trip
python -m controlplane.checks   # Tier 0 signals
python -m controlplane.router   # all four actions reachable, tier triggers
python -m eval.run_eval --selftest   # the tradeoff curve behaves
```

## Corpus

Drop the policy document into `corpus/` as `.txt` or `.pdf`. A placeholder is
included so the prototype runs from a clean clone.

## Notes

`corpus/` retrieval is brute-force cosine over a few hundred chunks — a vector
database would be more moving parts than data at this scale.

Anthropic's API exposes no token log-probabilities, so Tier 0 degrades to its
non-logprob signals rather than failing. `logprobs_available` is recorded on
every decision.

## Measuring it

```bash
python -m eval.run_eval        # 32 labelled questions through the pipeline
```

Writes `eval/results.json` and `RESULTS.md`. Tier 1 runs on every question so
decisions can be replayed at any threshold without paying the model again.

Then open **http://localhost:8000/console** and drag the threshold. False
positives and false negatives move in opposite directions; there is no setting
where both fall. That is the point — the brief asks for this tradeoff to be
tuned, not solved, so the console exposes it instead of hiding a chosen number.

The console replays through the same `router.decide()` the live request path
uses, so the dial cannot drift from real behaviour.

## Answer key

`eval/answer_key.jsonl` — 32 questions in three classes:

- `supported` — the policy answers it; a correct answer cites the right figure
- `not_covered` — the policy is silent; the correct answer says so
- `contradicted` — the question asserts something false; a correct answer pushes back

The `not_covered` class is the one that matters. There is often no ground truth to
verify against, so "cannot verify" is reported as its own outcome rather than
collapsed into "verified false".

## What it does not do

Named here rather than left for a reader to discover:

- **No streaming.** Checks run on the complete response. The pitched design gates
  the stream; that is not built.
- **No Tier 2 LLM-judge.** Repair covers the ambiguous band. A second, differently
  shaped check is the main thing missing — see the confident-wrong case in `RESULTS.md`.
- **No real-time bias measurement.** Bias needs a paired-prompt set compared across
  runs; it cannot be inferred from one response and is not attempted inline.
- **No multi-turn or agent-action risk.** The brief raises compounding risk across
  turns; this checks single responses.
- **PII detection is regex, not Presidio.** Adequate for the identifier classes in
  the demo corpus, not for production entity coverage.
- **Latency misses the target by ~5x.** Measured and explained in `RESULTS.md`
  rather than tuned toward the slide.

## Demo

```bash
uvicorn controlplane.app:app        # terminal 1
python -m demo.seed_traffic 12      # terminal 2
```

Seeds the audit log across all three use-case profiles, including one planted
PII leak so the redaction path appears in the log rather than only in a test.

## Prior art

`controlplane-prior-art.html` maps 63 existing tools onto this architecture.
Every component here exists as a mature product — Cleanlab TLM, Patronus Lynx,
Guardrails AI, Portkey, Langfuse. What is composed rather than rebuilt is
deliberate; the contribution is `router.py`, and the measurement around it.
