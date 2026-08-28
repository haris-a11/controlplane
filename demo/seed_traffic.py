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
USERS = ["u_demo", "u_0001", "u_0002", "u_0003"]
KEY = Path(__file__).resolve().parent.parent / "eval" / "answer_key.jsonl"

# A planted leak. Note it only exercises the redaction path when the model echoes
# the identifier back — PII is detected on the *response*, not on the user's own
# input, because this layer checks what the system says, not what it is told.
LEAKY = "My policy is MHA/889201 and my email is r.mehta@example.com — what is my room rent cap?"


def ask(question, use_case, user):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"messages": [{"role": "user", "content": question}]}).encode(),
        headers={"content-type": "application/json",
                 "x-controlplane-use-case": use_case,
                 "x-controlplane-user": user},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["controlplane"]


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    questions = [json.loads(l)["question"] for l in KEY.read_text().splitlines() if l.strip()]
    picked = random.sample(questions, min(n - 1, len(questions))) + [LEAKY]
    for i, q in enumerate(picked):
        uc = USE_CASES[i % len(USE_CASES)]
        user = USERS[i % len(USERS)]
        cp = ask(q, uc, user)
        print(f"{uc:<17} {user:<8} {cp['action']:<9} {cp['reason']:<22} "
              f"g={cp['grounding'] if cp['grounding'] is None else round(cp['grounding'], 2)} "
              f"{q[:44]}")
