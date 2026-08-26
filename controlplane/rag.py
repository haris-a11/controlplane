"""Retrieval over the policy corpus.

One document, a few hundred chunks. Brute-force cosine over a numpy matrix is
the whole index — a vector DB here would be more moving parts than data.
"""
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

CORPUS = Path(__file__).resolve().parent.parent / "corpus"
MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_CHARS = 600


def _read(path: Path) -> str:
    if path.suffix == ".pdf":
        # ponytail: shells out to poppler's pdftotext. Swap for pypdf if a
        # target machine lacks it; not worth a dependency while it's present.
        return subprocess.run(
            ["pdftotext", str(path), "-"], capture_output=True, text=True, check=True
        ).stdout
    return path.read_text()


def chunks() -> list[dict]:
    """Split on blank lines, then glue neighbours up to MAX_CHARS."""
    out = []
    for path in sorted([*CORPUS.glob("*.txt"), *CORPUS.glob("*.pdf")]):
        if path.name == "README.md":
            continue
        buf = ""
        for para in (p.strip() for p in _read(path).split("\n\n")):
            if not para:
                continue
            if len(buf) + len(para) > MAX_CHARS and buf:
                out.append({"id": f"{path.name}#{len(out)}", "text": buf, "source": path.name})
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            out.append({"id": f"{path.name}#{len(out)}", "text": buf, "source": path.name})
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
    hits = search("how long before maternity is covered?")
    assert "maternity" in hits[0]["text"].lower(), hits[0]["text"][:200]
    print(f"rag ok: {len(docs)} chunks, top hit {hits[0]['id']} @ {hits[0]['score']:.3f}")
