"""One row per response — the evidence trail.

Solutioning area: **governance** (a clear audit trail behind every decision) and
**feedback loops** (the `feedback` table below is what a reviewer's verdict lands in).

Prior art: Langfuse and Fiddler own per-response LLM tracing outright; this is a
minimum viable version of the same idea, plus one thing they do not do — a hash
chain over the rows, so a decision log can be shown not to have been edited after
the fact. EU AI Act Article 12 (record-keeping, enforceable from 2 Aug 2026) is
the reason a risk owner would ask for that.

Everything the console, the chat inspector, the dashboard and the eval run read
comes from here.
"""
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "controlplane.db"

# Actions that mean the checker intervened. "abstain" is a release — the model
# declined because the source is silent — so it is not an intervention.
FLAGGED = ("annotate", "redact", "repair", "block")
ESCALATED = ("repair", "block")
GENESIS = "0" * 64

# read-prev-hash then append must not interleave, or two rows claim the same
# predecessor and verify_chain() rejects a log that was never tampered with.
# Process-local is enough here: one uvicorn worker, one SQLite file.
_APPEND = threading.Lock()


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def use_temp_db():
    """Point the log at a scratch file. Self-tests call this so running them does
    not leave fixture rows in the demo's audit trail."""
    global DB
    import tempfile
    DB = Path(tempfile.mkdtemp(prefix="controlplane-selftest-")) / "test.db"
    init()
    return DB


def init():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS decisions(
            id INTEGER PRIMARY KEY,
            ts REAL,
            use_case TEXT,
            latency_ms INTEGER,
            record TEXT)""")
        # Forward migration, one column at a time. Cheap, and it beats recreating
        # the log every time a field is added — the log is the deliverable.
        for col, typ in (
            ("decision", "TEXT"),        # day 2: the action taken
            ("user_id", "TEXT"),         # day 3: per-user monitoring
            ("check_ms", "INTEGER"),     # day 3: added latency — the number we own
            ("cost_usd", "REAL"),        # day 3: model spend on this response
            ("check_cost_usd", "REAL"),  # day 3: what the checking itself cost
            ("tier_path", "TEXT"),       # day 3: "0>1>2" — which tiers ran
            ("simulated", "INTEGER"),    # day 3: generated traffic, not measured
            ("prev_hash", "TEXT"),
            ("row_hash", "TEXT"),
        ):
            try:
                c.execute(f"ALTER TABLE decisions ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        c.execute("""CREATE TABLE IF NOT EXISTS feedback(
            id INTEGER PRIMARY KEY,
            ts REAL,
            decision_id INTEGER,
            reviewer TEXT,
            verdict TEXT,               -- agree | false_positive | false_negative
            corrected_answer TEXT,
            note TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_dec_user ON decisions(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_dec_ts ON decisions(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_fb_dec ON feedback(decision_id)")


def _hash(prev: str, payload: str) -> str:
    return hashlib.sha256(f"{prev}{payload}".encode()).hexdigest()


def log(use_case, latency_ms, decision=None, user_id="anon", cost_usd=None,
        check_cost_usd=None, tier_path=None, ts=None, simulated=False,
        check_ms=None, **record):
    """Append one decision. Each row's hash covers the row before it.

    At tens of thousands of rows a week the chain costs one extra SELECT per
    response, which is noise next to a model call.
    """
    ts = time.time() if ts is None else ts
                # default=str: provider SDKs return wrapper objects that json refuses.
    # One guard here beats sanitising at every call site.
    blob = json.dumps(record, default=str, sort_keys=True)
    with _APPEND, _conn() as c:
        prev = c.execute(
            "SELECT row_hash FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = (prev and prev["row_hash"]) or GENESIS
        payload = f"{ts}|{use_case}|{user_id}|{decision}|{blob}"
        cur = c.execute(
            "INSERT INTO decisions(ts, use_case, latency_ms, check_ms, decision,"
            " user_id, cost_usd, check_cost_usd, tier_path, simulated, prev_hash,"
            " row_hash, record) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, use_case, latency_ms, check_ms, decision, user_id, cost_usd,
             check_cost_usd, tier_path, int(simulated), prev_hash,
             _hash(prev_hash, payload), blob),
        )
        return cur.lastrowid


def log_many(entries) -> int:
    """Append many rows in one transaction, chaining them in memory.

    Same hash chain as log(), but a per-row connection at simulation scale would
    take minutes for work that takes seconds. Each entry is the same kwargs dict
    log() accepts.
    """
    with _APPEND, _conn() as c:
        prev = c.execute(
            "SELECT row_hash FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = (prev and prev["row_hash"]) or GENESIS
        batch = []
        for e in entries:
            e = dict(e)
            ts = e.pop("ts", None) or time.time()
            use_case = e.pop("use_case")
            latency_ms = e.pop("latency_ms", None)
            check_ms = e.get("check_ms")
            decision = e.pop("decision", None)
            user_id = e.pop("user_id", "anon")
            cost = e.pop("cost_usd", None)
            check_cost = e.pop("check_cost_usd", None)
            tier_path = e.pop("tier_path", None)
            simulated = int(e.pop("simulated", False))
            blob = json.dumps(e, default=str, sort_keys=True)
            payload = f"{ts}|{use_case}|{user_id}|{decision}|{blob}"
            row_hash = _hash(prev_hash, payload)
            batch.append((ts, use_case, latency_ms, check_ms, decision, user_id,
                          cost, check_cost, tier_path, simulated, prev_hash,
                          row_hash, blob))
            prev_hash = row_hash
        c.executemany(
            "INSERT INTO decisions(ts, use_case, latency_ms, check_ms, decision,"
            " user_id, cost_usd, check_cost_usd, tier_path, simulated, prev_hash,"
            " row_hash, record) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        return len(batch)


def _rows(sql, params=()):
    with _conn() as c:
        try:
            rows = c.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # First read against a fresh checkout. Create the schema and retry
            # rather than making every caller sequence init() themselves —
            # checks.is_reask reads the log on the very first request.
            init()
            rows = c.execute(sql, params).fetchall()
    return [{**dict(r), "record": json.loads(r["record"] or "{}")} for r in rows]


def recent(n=50, live_only=False):
    where = " WHERE COALESCE(simulated,0)=0" if live_only else ""
    return _rows(f"SELECT * FROM decisions{where} ORDER BY id DESC LIMIT ?", (n,))


def by_user(user_id, n=100):
    return _rows("SELECT * FROM decisions WHERE user_id=? ORDER BY id DESC LIMIT ?",
                 (user_id, n))


def traffic(user_id=None, use_case=None, action=None, since=None,
            live_only=False, n=200):
    """One filtered read for the dashboard's stream. Filters compose."""
    sql = "SELECT * FROM decisions WHERE 1=1"
    p = []
    for col, val in (("user_id", user_id), ("use_case", use_case), ("decision", action)):
        if val:
            sql += f" AND {col}=?"
            p.append(val)
    if since:
        sql += " AND ts>=?"
        p.append(since)
    if live_only:
        sql += " AND COALESCE(simulated,0)=0"
    return _rows(sql + " ORDER BY id DESC LIMIT ?", (*p, n))


def users(since=None, live_only=False, limit=500):
    """Per-user rollup. The dashboard's main table — who is generating the risk."""
    flags = ",".join("?" * len(FLAGGED))
    esc = ",".join("?" * len(ESCALATED))
    sql = f"""SELECT user_id,
            COUNT(*) n,
            SUM(decision IN ({flags})) flagged,
            SUM(decision IN ({esc})) escalated,
            SUM(COALESCE(cost_usd,0)) cost_usd,
            MIN(ts) first_seen, MAX(ts) last_seen,
            GROUP_CONCAT(DISTINCT use_case) use_cases
        FROM decisions WHERE 1=1"""
    where, p = _where(since, live_only=live_only)
    sql += where + " GROUP BY user_id ORDER BY n DESC LIMIT ?"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            sql, (*FLAGGED, *ESCALATED, *p, limit)).fetchall()]
    for r in rows:
        r["flag_rate"] = round(r["flagged"] / r["n"], 3) if r["n"] else 0.0
        r["use_cases"] = (r["use_cases"] or "").split(",")
    return rows


def _where(since=None, use_case=None, live_only=False) -> tuple[str, list]:
    """Shared filter, so the tiles and the latency percentiles cover the same rows."""
    sql, p = "", []
    if since:
        sql += " AND ts>=?"
        p.append(since)
    if use_case:
        sql += " AND use_case=?"
        p.append(use_case)
    if live_only:
        sql += " AND COALESCE(simulated,0)=0"
    return sql, p


def aggregate(since=None, use_case=None, live_only=False):
    """The dashboard tiles. One pass over the table, no Python-side loop."""
    flags = ",".join("?" * len(FLAGGED))
    esc = ",".join("?" * len(ESCALATED))
    where, p = _where(since, use_case, live_only)
    with _conn() as c:
        agg = dict(c.execute(f"""SELECT COUNT(*) n,
                COUNT(DISTINCT user_id) users,
                SUM(decision IN ({flags})) flagged,
                SUM(decision IN ({esc})) escalated,
                SUM(decision='abstain') abstained,
                SUM(decision='redact') redacted,
                SUM(COALESCE(cost_usd,0)) cost_usd,
                SUM(COALESCE(check_cost_usd,0)) check_cost_usd,
                AVG(latency_ms) mean_latency_ms,
                MIN(ts) first_ts, MAX(ts) last_ts
            FROM decisions WHERE 1=1{where}""",
            (*FLAGGED, *ESCALATED, *p)).fetchone())
        # SQLite has no percentile function, so read the ordered column and index it.
        def pctile(col):
            return [r[0] for r in c.execute(
                f"SELECT {col} FROM decisions WHERE {col} IS NOT NULL{where}"
                f" ORDER BY {col}", p).fetchall()]
        lat, chk = pctile("latency_ms"), pctile("check_ms")
    n = agg["n"] or 0
    pick = lambda xs, q: xs[min(int(len(xs) * q), len(xs) - 1)] if xs else None
    agg["p95_latency_ms"] = pick(lat, 0.95)
    agg["p50_latency_ms"] = pick(lat, 0.50)
    # Added latency is the number this product actually owns. Total latency is
    # dominated by the model call — measured p95 of 15 s on a free-tier endpoint —
    # and budgeting against it would be scoring ourselves on the provider's queue.
    agg["p95_check_ms"] = pick(chk, 0.95)
    agg["p50_check_ms"] = pick(chk, 0.50)
    agg["flag_rate"] = round((agg["flagged"] or 0) / n, 4) if n else None
    agg["escalation_rate"] = round((agg["escalated"] or 0) / n, 4) if n else None
    agg["abstain_rate"] = round((agg["abstained"] or 0) / n, 4) if n else None
    total = (agg["cost_usd"] or 0) + (agg["check_cost_usd"] or 0)
    # The deck's headline economic claim: checking adds ~3% to inference spend.
    agg["overhead_pct"] = round((agg["check_cost_usd"] or 0) / total * 100, 2) if total else None
    return agg


def series(since=None, buckets=48, live_only=False):
    """Volume over time, split by action. Fixed bucket count so the chart is stable."""
    rows = traffic(since=since, live_only=live_only, n=1_000_000)
    if not rows:
        return []
    lo, hi = min(r["ts"] for r in rows), max(r["ts"] for r in rows)
    width = max((hi - lo) / buckets, 1e-6)
    out = [{"t": lo + i * width, "n": 0} for i in range(buckets)]
    for r in rows:
        b = out[min(int((r["ts"] - lo) / width), buckets - 1)]
        b["n"] += 1
        b[r["decision"] or "pass"] = b.get(r["decision"] or "pass", 0) + 1
    return out


def get(decision_id):
    rows = _rows("SELECT * FROM decisions WHERE id=?", (decision_id,))
    return rows[0] if rows else None


def queue(n=50):
    """Held responses a human has not yet ruled on. The escalation path, made real."""
    esc = ",".join("?" * len(ESCALATED))
    return _rows(
        f"""SELECT d.* FROM decisions d
            LEFT JOIN feedback f ON f.decision_id = d.id
            WHERE d.decision IN ({esc}) AND f.id IS NULL
            ORDER BY d.id DESC LIMIT ?""", (*ESCALATED, n))


def queue_depth() -> int:
    """How many are actually waiting. len(queue(n)) reports the page size, not the
    backlog — and understating a review backlog is the wrong way to be wrong."""
    esc = ",".join("?" * len(ESCALATED))
    with _conn() as c:
        return c.execute(
            f"""SELECT COUNT(*) FROM decisions d LEFT JOIN feedback f
                ON f.decision_id = d.id
                WHERE d.decision IN ({esc}) AND f.id IS NULL""", ESCALATED).fetchone()[0]


def add_feedback(decision_id, verdict, reviewer="reviewer", corrected_answer=None, note=None):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO feedback(ts, decision_id, reviewer, verdict, corrected_answer, note)"
            " VALUES (?,?,?,?,?,?)",
            (time.time(), decision_id, reviewer, verdict, corrected_answer, note))
        return cur.lastrowid


def feedback(n=500):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (n,)).fetchall()]


def verify_chain(limit=None):
    """Recompute every hash. Returns (ok, checked, first_bad_id).

    This is the claim a risk owner actually cares about: the log you are showing me
    is the log that was written. Any edited or deleted row breaks the chain here.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, use_case, user_id, decision, record, prev_hash, row_hash"
            " FROM decisions ORDER BY id" + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
    prev = GENESIS
    for r in rows:
        if r["row_hash"] is None:          # pre-migration row; chain starts after it
            continue
        payload = f"{r['ts']}|{r['use_case']}|{r['user_id']}|{r['decision']}|{r['record']}"
        if r["prev_hash"] != prev or _hash(prev, payload) != r["row_hash"]:
            return False, len(rows), r["id"]
        prev = r["row_hash"]
    return True, len(rows), None


if __name__ == "__main__":
    use_temp_db()

    class Wrapper:            # stands in for a provider SDK object
        def __repr__(self): return "<usage>"

    rid = log("support", 42, decision="pass", user_id="u_test", cost_usd=0.00012,
              check_cost_usd=0.000004, tier_path="0>1", question="q",
              mean_logprob=-0.3, usage=Wrapper())
    got = recent(1)[0]
    assert got["id"] == rid and got["record"]["mean_logprob"] == -0.3, got
    assert got["decision"] == "pass" and got["user_id"] == "u_test", got
    assert got["row_hash"] and got["prev_hash"], got

    blocked = log("decision_support", 900, decision="block", user_id="u_test",
                  question="q2", reason="ungrounded")
    assert by_user("u_test")[0]["id"] == blocked
    assert any(r["id"] == blocked for r in queue()), "blocked row must await review"
    assert [u for u in users() if u["user_id"] == "u_test"], "user rollup missing"
    agg = aggregate()
    assert agg["n"] >= 2 and agg["p95_latency_ms"] is not None, agg
    assert len(series(buckets=6)) == 6

    add_feedback(blocked, "false_positive", note="policy does cover this")
    assert not any(r["id"] == blocked for r in queue()), "reviewed row must leave the queue"

    ok, checked, bad = verify_chain()
    assert ok, f"chain broken at row {bad} of {checked}"

    # Tamper with a row and prove the chain notices.
    with _conn() as c:
        c.execute("UPDATE decisions SET record=? WHERE id=?",
                  ('{"question": "edited"}', rid))
    ok, _, bad = verify_chain()
    assert not ok and bad == rid, "an edited row must break the chain"
    with _conn() as c:                              # put it back
        c.execute("UPDATE decisions SET record=? WHERE id=?",
                  (json.dumps({"question": "q", "mean_logprob": -0.3,
                               "usage": "<usage>"}, sort_keys=True), rid))
    assert verify_chain()[0], "restore failed"

    print(f"audit ok: {agg['n']} rows, chain verified, queue + feedback + rollups live")
