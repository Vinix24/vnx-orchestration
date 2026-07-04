#!/usr/bin/env python3
"""D3 proposal-tier tests.

Covers:
  1. _supersede_stale_patterns routes to pending_archival.json (G-L4 gate — NOT auto-sets valid_until)
  2. No-provider failure receipts are filtered before mining
  3. Rule activation status remains "pending" (operator approval required)
  4. Real-history run: ≥1 non-trivial proposal from actual t0_receipts.ndjson trail
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll  # noqa: E402


def _bootstrap_schema(conn: sqlite3.Connection) -> None:
    """Create minimal quality_intelligence.db schema for supersede tests.

    Includes valid_from/valid_until columns added via migration in the real DB.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT DEFAULT 'behavioral',
            category TEXT,
            title TEXT,
            description TEXT,
            pattern_data TEXT,
            code_example TEXT,
            prerequisites TEXT,
            outcomes TEXT,
            success_rate REAL DEFAULT 0.0,
            usage_count INTEGER DEFAULT 0,
            avg_completion_time REAL DEFAULT 0.0,
            confidence_score REAL DEFAULT 0.5,
            source_dispatch_ids TEXT,
            source_receipts TEXT,
            first_seen TEXT,
            last_used TEXT,
            valid_from DATETIME DEFAULT (datetime('now')),
            valid_until DATETIME DEFAULT NULL,
            tags TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT,
            rule_type TEXT,
            description TEXT,
            recommendation TEXT,
            confidence REAL DEFAULT 0.5,
            created_at TEXT,
            triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT,
            valid_from DATETIME DEFAULT (datetime('now')),
            valid_until DATETIME DEFAULT NULL
        );
    """)


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

def _now_iso(offset_hours: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()


def _write_receipts(path: Path, receipts: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in receipts) + "\n", encoding="utf-8"
    )


class _FakePaths:
    def __init__(self, state_dir: Path, vnx_home: Path):
        self._d = {
            "VNX_STATE_DIR": str(state_dir),
            "VNX_HOME": str(vnx_home),
            "VNX_DATA_DIR": str(state_dir.parent),
        }

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


@pytest.fixture
def loop_env(tmp_path):
    """LearningLoop bound to a tmp state dir via patched ensure_env."""
    state_dir = tmp_path / "vnx-data" / "vnx-dev" / "state"
    state_dir.mkdir(parents=True)
    vnx_home = tmp_path / "repo"
    vnx_home.mkdir()

    fake = _FakePaths(state_dir, vnx_home)
    with patch.object(ll, "ensure_env", return_value=fake):
        loop = ll.LearningLoop()
        yield loop, state_dir
    try:
        loop.conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. _supersede_stale_patterns: routes to pending_archival.json, NOT valid_until
# ---------------------------------------------------------------------------

def _seed_success_pattern(loop, *, title="old-low-conf", confidence=0.1):
    """Insert a stale, low-confidence success_pattern directly into the DB."""
    old_date = (datetime.now() - timedelta(days=40)).isoformat()
    loop.conn.execute(
        """
        INSERT OR REPLACE INTO success_patterns
        (title, pattern_data, category, confidence_score, valid_from, valid_until,
         source_dispatch_ids, usage_count, tags)
        VALUES (?, ?, ?, ?, ?, NULL, '[]', 0, '[]')
        """,
        (title, json.dumps({"source": "learning_loop"}), "general", confidence, old_date),
    )
    loop.conn.commit()
    return loop.conn.execute(
        "SELECT id FROM success_patterns WHERE title = ?", (title,)
    ).fetchone()["id"]


def test_supersede_writes_pending_archival_not_valid_until(loop_env):
    """_supersede_stale_patterns writes to pending_archival.json, never sets valid_until."""
    loop, state_dir = loop_env
    _bootstrap_schema(loop.conn)
    pat_id = _seed_success_pattern(loop)

    count = loop._supersede_stale_patterns()

    # valid_until must NOT have been set
    row = loop.conn.execute(
        "SELECT valid_until FROM success_patterns WHERE id = ?", (pat_id,)
    ).fetchone()
    assert row is not None
    assert row["valid_until"] is None, "valid_until must remain NULL — operator approval required"

    # pending_archival.json must exist with the candidate
    pending_path = state_dir / "pending_archival.json"
    assert pending_path.exists(), "pending_archival.json must be written"

    data = json.loads(pending_path.read_text(encoding="utf-8"))
    archival = data.get("pending_archival", [])
    assert len(archival) >= 1, "at least one candidate must be queued"

    sp_candidates = [c for c in archival if c.get("source_table") == "success_patterns"]
    assert sp_candidates, "success_patterns candidate missing from pending_archival.json"

    c = sp_candidates[0]
    assert c["action"] == "supersede"
    assert c["status"] == "pending"
    assert c["pattern_id"] == str(pat_id)


def test_supersede_does_not_auto_apply_without_operator(loop_env):
    """Running _supersede_stale_patterns multiple times is idempotent and never touches valid_until."""
    loop, state_dir = loop_env
    _bootstrap_schema(loop.conn)
    pat_id = _seed_success_pattern(loop, title="stale-pat-2", confidence=0.05)

    loop._supersede_stale_patterns()
    loop._supersede_stale_patterns()  # second call — must not duplicate or auto-apply

    row = loop.conn.execute(
        "SELECT valid_until FROM success_patterns WHERE id = ?", (pat_id,)
    ).fetchone()
    assert row["valid_until"] is None

    pending_path = state_dir / "pending_archival.json"
    data = json.loads(pending_path.read_text(encoding="utf-8"))
    sp_candidates = [
        c for c in data.get("pending_archival", [])
        if c.get("source_table") == "success_patterns" and c.get("pattern_id") == str(pat_id)
    ]
    assert len(sp_candidates) == 1, "second call must not duplicate the pending entry"


def test_supersede_off_switch(loop_env, monkeypatch):
    """VNX_LEARN_SUPERSEDE=0 skips the entire step."""
    loop, state_dir = loop_env
    _bootstrap_schema(loop.conn)
    _seed_success_pattern(loop, title="off-switch-pat", confidence=0.05)

    monkeypatch.setenv("VNX_LEARN_SUPERSEDE", "0")
    count = loop._supersede_stale_patterns()
    assert count == 0

    pending_path = state_dir / "pending_archival.json"
    assert not pending_path.exists(), "pending_archival.json must not be written when off"


# ---------------------------------------------------------------------------
# 2. No-provider failure receipts are filtered before mining
# ---------------------------------------------------------------------------

def test_no_provider_failure_receipts_filtered(loop_env, capsys):
    """Failure receipts with explicitly-none provider are excluded from corpus.

    Absent provider field (e.g. contract_invalid from the receipt processor)
    passes through — only explicit none-sentinels are filtered.
    """
    loop, state_dir = loop_env
    receipts = [
        {   # INCLUDE: has real provider + failure
            "status": "failed",
            "provider": "claude",
            "failure_reason": "Exhausted 3 retries",
            "terminal": "T1",
            "dispatch_id": "d-real",
            "timestamp": _now_iso(-1),
        },
        {   # EXCLUDE: explicit provider="none" + failure (the 9,052 unknown:unknown receipts)
            "status": "failed",
            "provider": "none",
            "sub_provider": "none",
            "failure_reason": "some error",
            "terminal": "T2",
            "dispatch_id": "d-noprov1",
            "timestamp": _now_iso(-1),
        },
        {   # EXCLUDE: explicit empty string provider + failure
            "status": "failed",
            "provider": "",
            "failure_reason": "another error",
            "terminal": "T3",
            "dispatch_id": "d-noprov2",
            "timestamp": _now_iso(-1),
        },
        {   # INCLUDE: absent provider field (e.g. contract_invalid from receipt processor)
            "status": "error",
            "failure_reason": "crash",
            "terminal": "T4",
            "dispatch_id": "d-noprov3",
            "timestamp": _now_iso(-1),
        },
        {   # EXCLUDE: success receipts — already rejected by status filter (irrelevant)
            "status": "done",
            "provider": "none",
            "terminal": "T1",
            "dispatch_id": "d-ok",
            "timestamp": _now_iso(-1),
        },
    ]
    _write_receipts(state_dir / "t0_receipts.ndjson", receipts)

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )

    # 2 pass: real provider + absent provider (governance receipt)
    assert len(failures) == 2, (
        "real-provider failure and absent-provider failure both pass; "
        "only explicit none-sentinel providers are filtered"
    )
    agents = {f["agent"] for f in failures}
    assert "claude" in agents, "real-provider failure must be included"

    out, _ = capsys.readouterr()
    assert "no-provider filtered" in out, "corpus size must be reported"


def test_provider_unknown_filtered(loop_env):
    """provider='unknown' is treated as no-provider and filtered out."""
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "failed",
                "provider": "unknown",
                "failure_reason": "crash",
                "terminal": "T1",
                "dispatch_id": "d-unk",
                "timestamp": _now_iso(-1),
            }
        ],
    )
    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )
    assert failures == [], "provider='unknown' must be filtered"


def test_corpus_size_reported(loop_env, capsys):
    """extract_failure_patterns always reports post-filter corpus size."""
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "done",
                "provider": "claude",
                "terminal": "T1",
                "dispatch_id": "d-ok",
                "timestamp": _now_iso(-1),
            }
        ],
    )
    loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )
    out, _ = capsys.readouterr()
    assert "Receipt corpus:" in out
    assert "scanned" in out
    assert "filtered" in out


# ---------------------------------------------------------------------------
# 3. Rule activation status: pending (operator approval required)
# ---------------------------------------------------------------------------

def test_rule_activation_requires_operator_approval(loop_env):
    """Rules queued by the learning loop always have status='pending' (G-L1)."""
    loop, state_dir = loop_env
    _write_receipts(
        state_dir / "t0_receipts.ndjson",
        [
            {
                "status": "failed",
                "failure_reason": "Exhausted 3 retries",
                "provider": "claude",
                "terminal": "T1",
                "dispatch_id": "d-1",
                "timestamp": _now_iso(-2),
            },
            {
                "status": "failed",
                "failure_reason": "Exhausted 3 retries",
                "provider": "claude",
                "terminal": "T1",
                "dispatch_id": "d-2",
                "timestamp": _now_iso(-1),
            },
        ],
    )

    failures = loop.extract_failure_patterns(
        start_time=datetime.now(timezone.utc) - timedelta(days=2)
    )
    rules = loop.generate_prevention_rules(failures)
    assert len(rules) >= 1
    loop.update_terminal_constraints(rules)

    pending_path = state_dir / "pending_rules.json"
    assert pending_path.exists()
    data = json.loads(pending_path.read_text(encoding="utf-8"))
    pending = data.get("pending_rules", [])
    assert len(pending) >= 1
    assert all(r["status"] == "pending" for r in pending), "G-L1: no auto-activation"
    # Confirm prevention_rules table has 0 rows (table may not exist in minimal test DB)
    try:
        table_rows = loop.conn.execute("SELECT COUNT(*) FROM prevention_rules").fetchone()[0]
        assert table_rows == 0, "rules must not be inserted without operator approval"
    except sqlite3.OperationalError:
        pass  # table absent = no rows inserted, assertion already holds


# ---------------------------------------------------------------------------
# 4. Minimum-signal gate: ≥1 non-trivial proposal from real history
# ---------------------------------------------------------------------------

_REAL_RECEIPTS = Path(os.environ.get(
    "VNX_STATE_DIR",
    str(Path.home() / ".vnx-data" / "vnx-dev" / "state"),
)) / "t0_receipts.ndjson"


@pytest.mark.skipif(
    not _REAL_RECEIPTS.exists(),
    reason=f"real receipt trail not found at {_REAL_RECEIPTS}",
)
def test_real_history_produces_at_least_one_proposal():
    """Full receipt history yields ≥1 recurring failure pattern proposal.

    This is the minimum-signal gate (D3): a run against the actual governed
    receipt stream must surface at least one non-trivial proposal (occurrence >= 2).
    The no-provider-failure filter must be applied first.
    """
    from unittest.mock import patch as _patch

    state_dir = _REAL_RECEIPTS.parent
    vnx_home = state_dir.parent.parent  # best-effort

    fake = _FakePaths(state_dir, vnx_home)
    with _patch.object(ll, "ensure_env", return_value=fake):
        loop = ll.LearningLoop()
        try:
            failures = loop.extract_failure_patterns(
                start_time=datetime(2000, 1, 1, tzinfo=timezone.utc)
            )
            rules = loop.generate_prevention_rules(failures)
        finally:
            try:
                loop.conn.close()
            except Exception:
                pass

    assert len(rules) >= 1, (
        f"Expected ≥1 proposal from {_REAL_RECEIPTS} "
        f"(after no-provider filter); got {len(rules)}. "
        "Receipt corpus may be too narrow — consider using --from-history."
    )
    # All proposals must be non-trivial (≥2 occurrences)
    assert all(r.get("occurrence_count", 0) >= 2 for r in rules), (
        "All proposals must have occurrence_count >= 2 (recurrence threshold)"
    )
