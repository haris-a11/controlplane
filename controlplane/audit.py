"""One row per response. Everything the console and the eval run read comes from here."""
import json
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "controlplane.db"


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS decisions(
            id INTEGER PRIMARY KEY,
            ts REAL,
            use_case TEXT,
            latency_ms INTEGER,
            record TEXT)""")


def log(use_case, latency_ms, **record):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO decisions(ts, use_case, latency_ms, record) VALUES (?,?,?,?)",
            (time.time(), use_case, latency_ms, json.dumps(record)),
        )
        return cur.lastrowid


def recent(n=50):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return [{**dict(r), "record": json.loads(r["record"])} for r in rows]


if __name__ == "__main__":
    init()
    rid = log("support", 42, question="q", answer="a", mean_logprob=-0.3)
    got = recent(1)[0]
    assert got["id"] == rid and got["record"]["mean_logprob"] == -0.3, got
    print("audit ok:", got)
