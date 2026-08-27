"""The two cheap tiers.

Tier 0  — free. Signals the model already produced, plus pattern matching.
Tier 1  — ~free. A 110M-parameter classifier on CPU (Vectara HHEM-2.1-Open).

Neither tier decides anything; they return evidence. router.py decides.
"""
import difflib
import re
from functools import lru_cache

from . import audit

# ponytail: regex, not Presidio. Presidio drags in spacy + a model download for
# entity types this demo never shows. Swap it in if the corpus grows real PII.
PII_PATTERNS = {
    "email": r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b",
    "phone_in": r"\b(?:\+91[-\s]?)?[6-9]\d{9}\b",
    "pan": r"\b[A-Z]{5}\d{4}[A-Z]\b",
    "aadhaar": r"\b\d{4}\s?\d{4}\s?\d{4}\b",
    "policy_no": r"\b(?:POL|MHA)[-/]?\d{6,}\b",
}

# An answer that declines to answer cannot be "grounded" in a document that, by
# definition, does not cover it. Scoring it for groundedness punishes the model
# for doing the right thing — measured at a 42% false-positive rate before this
# was separated out. Abstention is its own outcome, not a failed check.
ABSTAIN = re.compile(
    r"\b(?:not|isn't|is not|does not|doesn't|do not|don't|cannot|can't)\s+"
    r"(?:\w+\s+){0,3}?"
    r"(?:cover(?:ed)?|mention(?:ed)?|specif(?:y|ied)|state[ds]?|address(?:ed)?|"
    r"provide[ds]?|contain(?:ed)?|include[ds]?|list(?:ed)?|find|determine|answer)"
    r"|\bno\s+(?:information|provision|mention|reference|details?|indication)"
    r"|\b(?:silent|unable to (?:determine|answer|find))\b"
    r"|\bextracts?\s+(?:do|does)\s+not\b",
    re.I,
)

REASK_WINDOW_S = 60
REASK_SIMILARITY = 0.80
HHEM = "vectara/hallucination_evaluation_model"
HHEM_BASE = "google/flan-t5-base"
HHEM_PROMPT = ("<pad> Determine if the hypothesis is true given the premise?\n\n"
               "Premise: {text1}\n\nHypothesis: {text2}")


def find_pii(text: str) -> dict[str, list[str]]:
    hits = {k: re.findall(p, text) for k, p in PII_PATTERNS.items()}
    return {k: v for k, v in hits.items() if v}


def is_reask(question: str, use_case: str, now: float) -> bool:
    """A user re-asking within a minute is telling you the first answer failed.

    They rarely file a complaint; they just ask again. That is a free failure signal.
    """
    for row in audit.recent(30):
        if row["use_case"] != use_case or now - row["ts"] > REASK_WINDOW_S:
            continue
        prev = row["record"].get("question", "")
        if difflib.SequenceMatcher(None, prev.lower(), question.lower()).ratio() >= REASK_SIMILARITY:
            return True
    return False


def abstains(answer: str) -> bool:
    """Did the model decline rather than assert? See ABSTAIN."""
    return bool(ABSTAIN.search(answer))


def tier0(question, answer, use_case, now, mean_logprob, min_logprob,
          retrieval=None) -> dict:
    """Free. Runs on 100% of traffic. No model call."""
    return {
        "retrieval": retrieval,
        "mean_logprob": mean_logprob,
        "min_logprob": min_logprob,
        "logprobs_available": mean_logprob is not None,
        "pii": find_pii(answer),
        "reask": is_reask(question, use_case, now),
        "abstains": abstains(answer),
    }


@lru_cache(maxsize=1)
def _hhem():
    """Load HHEM's weights into a stock T5 instead of running its remote code.

    The published wrapper is a thin shim over T5ForTokenClassification, and it
    breaks on transformers 5.x (it predates the tied-weights refactor). Loading
    the tensors directly is both version-proof and means the repo never asks a
    reviewer to trust_remote_code.
    """
    import torch
    from huggingface_hub import hf_hub_download
    from safetensors.torch import load_file
    from transformers import AutoConfig, AutoTokenizer, T5ForTokenClassification

    cfg = AutoConfig.from_pretrained(HHEM_BASE, num_labels=2)
    model = T5ForTokenClassification(cfg)
    weights = load_file(hf_hub_download(HHEM, "model.safetensors"))
    # The shim stored everything under self.t5; strip that and the shapes line up.
    missing, unexpected = model.load_state_dict(
        {k.removeprefix("t5."): v for k, v in weights.items()}, strict=False
    )
    assert not unexpected, f"unexpected HHEM tensors: {unexpected[:3]}"
    assert not [m for m in missing if "embed_tokens" not in m], f"missing: {missing[:3]}"
    model.eval()
    return AutoTokenizer.from_pretrained(HHEM_BASE), model, torch


def _grounding_scores(pairs: list[tuple[str, str]]) -> list[float]:
    tok, model, torch = _hhem()
    prompts = [HHEM_PROMPT.format(text1=a, text2=b) for a, b in pairs]
    inputs = tok(prompts, return_tensors="pt", padding=True, truncation=True, max_length=512)
    with torch.no_grad():
        logits = model(**inputs).logits[:, 0, :]
    return torch.softmax(logits, dim=-1)[:, 1].tolist()


def tier1(answer: str, contexts: list[str]) -> dict:
    """Grounding: is this answer supported by the retrieved spans?

    Scores each span separately and keeps the best. One span supporting the claim
    is enough; averaging would punish an answer for the spans that are irrelevant.
    """
    if not contexts or not answer.strip():
        return {"grounding": None, "best_span": None}
    scores = _grounding_scores([(c, answer) for c in contexts])
    best = max(range(len(scores)), key=lambda i: scores[i])
    return {"grounding": scores[best], "best_span": best, "span_scores": scores}


if __name__ == "__main__":
    import time

    assert find_pii("write to a.b+x@mail.co.uk") == {"email": ["a.b+x@mail.co.uk"]}
    assert "pan" in find_pii("PAN ABCDE1234F")
    assert find_pii("the waiting period is 24 months") == {}
    assert not is_reask("brand new question here", "support", time.time())
    assert abstains("The policy does not cover ambulance charges.")
    assert abstains("The extracts do not mention a no-claim bonus.")
    assert abstains("There is no information about co-payment in the policy.")
    assert abstains("I cannot determine this from the extracts provided.")
    assert not abstains("Maternity is covered after 24 months of coverage.")
    assert not abstains("Cosmetic surgery is excluded unless it follows an accident.")
    sig = tier0("q", "mail me at x@y.com", "support", time.time(), -0.2, -3.1)
    assert sig["pii"] and sig["logprobs_available"]
    sig2 = tier0("q", "clean answer", "support", time.time(), None, None)
    assert not sig2["pii"] and not sig2["logprobs_available"]
    print("tier0 ok:", sig)
