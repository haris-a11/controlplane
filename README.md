# ControlPlane.ai — Round 2 prototype

Team Fremen · Accenture Innovation Challenge 2026.
A checking layer between an enterprise application and the LLM.

Status: **day 1 of 4.** Request path and evidence trail work. Tier router lands next.

## Run

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...          # or any provider LiteLLM supports
uvicorn controlplane.app:app --reload
```

Point any OpenAI client at `http://localhost:8000/v1`:

```bash
curl localhost:8000/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-controlplane-use-case: support' \
  -d '{"messages":[{"role":"user","content":"When is maternity covered?"}]}'
```

`GET /decisions` returns the audit trail.

## Self-checks

```bash
python -m controlplane.rag      # chunking + retrieval
python -m controlplane.audit    # log round-trip
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
