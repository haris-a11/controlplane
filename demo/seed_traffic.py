"""Fill the audit log with traffic across all three use cases, for the demo.

    uvicorn controlplane.app:app          # in one terminal
    python -m demo.seed_traffic 15        # in another
"""
import json
import random
import sys
import urllib.request
from pathlib import Path

URL = "http://localhost:8000/v1/chat/completions"
USE_CASES = ["support", "copilot", "decision_support"]
KEY = Path(__file__).resolve().parent.parent / "eval" / "answer_key.jsonl"

# One planted leak, so the PII path shows up in the log rather than being asserted.
LEAKY = "My policy is MHA/889201 and my email is r.mehta@example.com — what is my room rent cap?"


def ask(question, use_case):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"messages": [{"role": "user", "content": question}]}).encode(),
        headers={"content-type": "application/json", "x-controlplane-use-case": use_case},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["controlplane"]


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    questions = [json.loads(l)["question"] for l in KEY.read_text().splitlines() if l.strip()]
    picked = random.sample(questions, min(n - 1, len(questions))) + [LEAKY]
    for i, q in enumerate(picked):
        uc = USE_CASES[i % len(USE_CASES)]
        cp = ask(q, uc)
        print(f"{uc:<17} {cp['action']:<9} {cp['reason']:<22} "
              f"g={cp['grounding'] if cp['grounding'] is None else round(cp['grounding'], 2)} "
              f"{q[:44]}")
