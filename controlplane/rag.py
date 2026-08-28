"""Retrieval over the policy corpus, with each chunk's data governance recorded.

A few hundred chunks. Brute-force cosine over a numpy matrix is the whole index —
a vector DB here would be more moving parts than data.

Solutioning area: **governance**. The brief's reference parameters assume "a mix
of well-governed and loosely governed internal data sources". That mix is the
point of `corpus/governed/` vs `corpus/ungoverned/`: every chunk carries the tier
it came from, so the router can act on *where* an answer's support came from and
not only on how strong that support scored. See `corpus/README.md`.
"""
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

CORPUS = Path(__file__).resolve().parent.parent / "corpus"
MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_CHARS = 600
GOVERNED, UNGOVERNED = "governed", "ungoverned"


def _read(path: Path) -> str:
    if path.suffix == ".pdf":
        # ponytail: shells out to poppler's pdftotext. Swap for pypdf if a
        # target machine lacks it; not worth a dependency while it's present.
        return subprocess.run(
            ["pdftotext", str(path), "-"], capture_output=True, text=True, check=True
        ).stdout
    return path.read_text()


def _sources() -> list[tuple[Path, str]]:
    """Every corpus file with the governance tier of the directory it sits in.

    A file dropped loose in `corpus/` is treated as ungoverned. Untagged
    provenance is not a reason to trust something.
    """
    found = []
    for tier, root in ((GOVERNED, CORPUS / GOVERNED), (UNGOVERNED, CORPUS / UNGOVERNED),
                       (UNGOVERNED, CORPUS)):
        for path in sorted([*root.glob("*.txt"), *root.glob("*.pdf")]):
            found.append((path, tier))
    return found


def chunks() -> list[dict]:
    """Split on blank lines, then glue neighbours up to MAX_CHARS."""
    out = []
    for path, tier in _sources():
        def emit(text):
            out.append({"id": f"{path.name}#{len(out)}", "text": text,
                        "source": path.name, "governance": tier})
        buf = ""
        for para in (p.strip() for p in _read(path).split("\n\n")):
            if not para:
                continue
            if len(buf) + len(para) > MAX_CHARS and buf:
                emit(buf)
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            emit(buf)
    return out


@lru_cache(maxsize=1)
def _index():
    docs = chunks()
    if not docs:
        raise RuntimeError(f"no .txt or .pdf files in {CORPUS}")
    model = SentenceTransformer(MODEL)
    vecs = model.encode([d["text"] for d in docs], normalize_embeddings=True)
    return docs, np.asarray(vecs), model


def search(query: str, k: int = 4) -> list[dict]:
    docs, vecs, model = _index()
    q = model.encode([query], normalize_embeddings=True)[0]
    scores = vecs @ q
    return [{**docs[i], "score": float(scores[i])} for i in np.argsort(-scores)[:k]]


if __name__ == "__main__":
    docs = chunks()
    assert docs, "no chunks parsed"
    assert all(len(d["text"]) <= MAX_CHARS * 2 for d in docs), "chunk ran away"
    tiers = {t: sum(d["governance"] == t for d in docs) for t in (GOVERNED, UNGOVERNED)}
    assert all(tiers.values()), f"both tiers must be populated: {tiers}"
    hits = search("how long before maternity is covered?")
    assert "maternity" in hits[0]["text"].lower(), hits[0]["text"][:200]
    # The contradiction has to be *retrievable*, or the governance check never fires.
    assert any(h["governance"] == UNGOVERNED for h in search("maternity waiting period", 4)), \
        "the stale wiki should surface alongside the policy — that is the whole scenario"
    print(f"rag ok: {len(docs)} chunks ({tiers[GOVERNED]} governed, "
          f"{tiers[UNGOVERNED]} ungoverned), top hit {hits[0]['id']} "
          f"[{hits[0]['governance']}] @ {hits[0]['score']:.3f}")
