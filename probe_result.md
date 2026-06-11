# SQLite Locking Trade-offs: WAL-mode Multi-writer Dispatch Queue

## Optimistic vs Pessimistic Locking Analysis

**Pessimistic locking** (BEGIN IMMEDIATE / BEGIN EXCLUSIVE) reserves the write lock upfront.
Under WAL-mode this means other readers keep running — WAL separates read and write paths —
but only one writer holds the lock at a time. The cost is contention serialization: if two
workers try to claim a job simultaneously, one blocks immediately at BEGIN IMMEDIATE until
the other commits or rolls back. No retry loop required; SQLite's internal busy-handler
handles the wait. The guarantee is strong: you never touch stale state.

**Optimistic locking** reads first (BEGIN DEFERRED or no explicit transaction), then
validates a version field or row-hash at commit time. Under WAL this avoids the write-lock
during the read phase, which improves throughput when conflicts are rare. The cost is a
required retry loop: if the version changed between your read and your UPDATE, you catch
SQLITE_BUSY / mismatched rowcount and retry. Under high concurrency with many short jobs
this retry amplification can exceed the savings from avoiding early lock acquisition.

**WAL-mode specific note:** WAL allows one writer + many concurrent readers. Under default
journal mode the tradeoffs shift (readers block writers). With WAL, pessimistic locking
costs less than it looks — the lock contention window is just the write transaction itself,
not the entire read + decide + write cycle.

**For a multi-writer queue the key metric is conflict rate.** Low conflict rate → optimistic
wins on throughput. High conflict rate (many workers racing for the same N rows) → pessimistic
wins because retry storms under optimistic degrade to worse-than-serial behavior.

## Concrete Example: BEGIN IMMEDIATE in Python

```python
import sqlite3
import time

def claim_next_dispatch(db_path: str, worker_id: str) -> dict | None:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # BEGIN IMMEDIATE acquires the write lock before any reads.
        # Under WAL-mode readers are unaffected; only writers serialize here.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, payload FROM dispatch_queue
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        conn.execute(
            "UPDATE dispatch_queue SET status = 'claimed', worker_id = ? WHERE id = ?",
            (worker_id, row["id"]),
        )
        conn.commit()
        return dict(row)
    except sqlite3.OperationalError:
        # SQLITE_BUSY — another writer holds the lock; timeout exhausted.
        conn.rollback()
        raise
    finally:
        conn.close()
```

The `timeout=10` in `sqlite3.connect` activates SQLite's built-in busy-handler: it retries
on SQLITE_BUSY up to 10 seconds before raising. No manual retry loop needed for the common
case. For a queue worker you typically wrap the call site in a short exponential-backoff loop
to handle the rare exhaustion case.

## Conclusion: Which Fits a Single-Operator Orchestrator?

**Pessimistic locking (BEGIN IMMEDIATE) is the right default here.**

A single-operator orchestrator has a predictable, low number of concurrent writers — typically
T1, T2, T3 (3 workers) plus the orchestrator itself. Conflict rate is bounded and low per
second. Under these conditions:

- Retry-loop complexity of optimistic locking brings no throughput benefit.
- BEGIN IMMEDIATE gives a clean, single-code-path: claim succeeds or raises after timeout.
- WAL-mode neutralizes the main cost of pessimistic locking (reader starvation) entirely.
- Audit trail integrity is higher: no partial-claim ambiguity if the process dies mid-retry.

Use optimistic locking only if the queue grows to dozens of concurrent workers racing for
sub-second jobs — a scenario outside the current VNX single-operator model.
