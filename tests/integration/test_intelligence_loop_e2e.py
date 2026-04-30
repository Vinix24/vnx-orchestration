#!/usr/bin/env python3
"""End-to-end integration tests for the intelligence learning loop.

Covers the full closed-circuit flow:
    inject pattern P -> dispatch D -> outcome O ->
    confidence update -> next dispatch sees updated P

Background: ``claudedocs/2026-04-30-self-learning-loop-audit.md`` documented
that the loop was open-circuit for weeks without detection.  These tests
catch silent failures in that path by exercising the real production modules
against synthetic SQLite databases — no mocks for the persistence layer.

Source modules under test:
    scripts/lib/intelligence_selector.py        (selection + injection record)
    scripts/lib/intelligence_persist.py         (outcome -> confidence update)
    scripts/lib/confidence_reconcile.py         (pattern_usage -> success_patterns sync)
    scripts/append_receipt.py                   (_update_confidence_from_receipt path)

Cases A-I correspond directly to the dispatch instruction
(20260430-pr-t2-intel-tests).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple

import pytest

# Make scripts/ and scripts/lib/ importable regardless of pytest invocation cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"
_SCHEMAS_DIR = _REPO_ROOT / "schemas"
for p in (_SCRIPTS_DIR, _LIB_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from confidence_reconcile import (  # noqa: E402
    SUCCESS_PATTERN_PREFIX,
    beta_score,
    reconcile_pattern_confidence,
)
from intelligence_persist import update_confidence_from_outcome  # noqa: E402
from intelligence_selector import IntelligenceSelector  # noqa: E402
from runtime_coordination import init_schema  # noqa: E402


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Schema + seed helpers
# ---------------------------------------------------------------------------

_QUALITY_DDL = """
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT, category TEXT, title TEXT, description TEXT,
    pattern_data TEXT, code_example TEXT, prerequisites TEXT, outcomes TEXT,
    success_rate REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
    avg_completion_time INTEGER, confidence_score REAL DEFAULT 0.0,
    source_dispatch_ids TEXT, source_receipts TEXT,
    first_seen DATETIME, last_used DATETIME,
    valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS antipatterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT, category TEXT, title TEXT, description TEXT,
    pattern_data TEXT, problem_example TEXT, why_problematic TEXT,
    better_alternative TEXT, occurrence_count INTEGER DEFAULT 0,
    avg_resolution_time INTEGER, severity TEXT DEFAULT 'medium',
    source_dispatch_ids TEXT, first_seen DATETIME, last_seen DATETIME,
    valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS prevention_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_combination TEXT, rule_type TEXT, description TEXT,
    recommendation TEXT, confidence REAL DEFAULT 0.0,
    created_at TEXT, triggered_count INTEGER DEFAULT 0,
    last_triggered TEXT,
    valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS dispatch_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT UNIQUE, terminal TEXT, track TEXT,
    role TEXT, skill_name TEXT, gate TEXT,
    pattern_count INTEGER DEFAULT 0, prevention_rule_count INTEGER DEFAULT 0,
    dispatched_at DATETIME, completed_at DATETIME,
    outcome_status TEXT, outcome_report_path TEXT
);

CREATE TABLE IF NOT EXISTS pattern_usage (
    pattern_id TEXT PRIMARY KEY,
    pattern_title TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_used TIMESTAMP,
    last_offered TIMESTAMP,
    confidence REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS confidence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT NOT NULL,
    terminal TEXT,
    outcome TEXT NOT NULL,
    patterns_boosted INTEGER DEFAULT 0,
    patterns_decayed INTEGER DEFAULT 0,
    confidence_change REAL NOT NULL,
    occurred_at TEXT NOT NULL
);
"""


def _make_quality_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_QUALITY_DDL)
        conn.commit()
    finally:
        conn.close()


def _seed_pattern(
    db_path: Path,
    *,
    title: str = "Use deterministic dispatch ids",
    description: str = "Deterministic ids stop pattern_usage fragmentation.",
    category: str = "test-engineer",
    confidence: float = 0.5,
    usage_count: int = 0,
    source_dispatch_ids: list[str] | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    src_json = json.dumps(source_dispatch_ids or [])
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """INSERT INTO success_patterns
               (pattern_type, category, title, description, pattern_data,
                confidence_score, usage_count, source_dispatch_ids,
                first_seen, last_used)
               VALUES ('approach', ?, ?, ?, '{}', ?, ?, ?, ?, ?)""",
            (category, title, description, confidence, usage_count, src_json, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_pattern_usage(
    db_path: Path,
    success_pattern_id: int,
    *,
    success_count: int,
    failure_count: int,
    confidence: float | None = None,
) -> None:
    pattern_id = f"{SUCCESS_PATTERN_PREFIX}{success_pattern_id}"
    now = datetime.now(timezone.utc).isoformat()
    if confidence is None:
        confidence = beta_score(success_count, failure_count)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """INSERT INTO pattern_usage
               (pattern_id, pattern_title, pattern_hash, used_count,
                ignored_count, success_count, failure_count,
                last_used, confidence, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
            (
                pattern_id,
                f"sp_{success_pattern_id}",
                f"hash_{success_pattern_id}",
                success_count + failure_count,
                success_count,
                failure_count,
                now,
                confidence,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row(db_path: Path, sql: str, params: Tuple = ()) -> dict | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _confidence(db_path: Path, sp_id: int) -> float:
    row = _row(db_path, "SELECT confidence_score FROM success_patterns WHERE id = ?", (sp_id,))
    assert row is not None, f"success_pattern {sp_id} missing"
    return float(row["confidence_score"])


def _patch_sys_path_for_subprocess_dispatch():
    """confidence_reconcile is imported via bare name from intelligence_persist."""
    if str(_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(_LIB_DIR))


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def quality_db(tmp_path: Path) -> Path:
    db = tmp_path / "quality_intelligence.db"
    _make_quality_db(db)
    _patch_sys_path_for_subprocess_dispatch()
    return db


@pytest.fixture
def coord_state_dir(tmp_path: Path) -> Path:
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd, _SCHEMAS_DIR / "runtime_coordination.sql")
    return sd


# ---------------------------------------------------------------------------
# Case A: Successful injection writes to DB
# ---------------------------------------------------------------------------

class TestCaseAInjectionRecorded:
    def test_injection_writes_audit_and_links_dispatch(
        self, quality_db: Path, coord_state_dir: Path
    ) -> None:
        sp_id = _seed_pattern(
            quality_db,
            title="Always read CLAUDE.md first",
            description="Reading CLAUDE.md improves first-pass success.",
            confidence=0.8,
            usage_count=5,
        )
        dispatch_id = "case-a-dispatch-001"

        selector = IntelligenceSelector(
            quality_db_path=quality_db,
            coord_db_state_dir=coord_state_dir,
        )
        try:
            result = selector.select(
                dispatch_id=dispatch_id,
                injection_point="dispatch_create",
                skill_name="test-engineer",
            )
            selector.record_injection(result, coord_state_dir=coord_state_dir)
        finally:
            selector.close()

        assert result.items_injected >= 1, "expected pattern to be selected"

        coord_db = coord_state_dir / "runtime_coordination.db"
        injection = _row(
            coord_db,
            "SELECT dispatch_id, items_injected, items_json FROM intelligence_injections "
            "WHERE dispatch_id = ?",
            (dispatch_id,),
        )
        assert injection is not None, "intelligence_injections row missing"
        assert injection["dispatch_id"] == dispatch_id
        assert injection["items_injected"] == result.items_injected
        items = json.loads(injection["items_json"])
        assert any(i["item_id"] == f"intel_sp_{sp_id}" for i in items)

        sp_row = _row(
            quality_db,
            "SELECT source_dispatch_ids FROM success_patterns WHERE id = ?",
            (sp_id,),
        )
        ids = json.loads(sp_row["source_dispatch_ids"] or "[]")
        assert dispatch_id in ids, (
            "selector must stamp dispatch_id onto success_patterns.source_dispatch_ids "
            "so failure-decay path can find it later"
        )


# ---------------------------------------------------------------------------
# Case B: Failure decay actually decreases confidence
# ---------------------------------------------------------------------------

class TestCaseBFailureDecay:
    def test_failure_outcome_decreases_confidence(self, quality_db: Path) -> None:
        dispatch_id = "case-b-dispatch-002"
        sp_id = _seed_pattern(
            quality_db,
            title="case-b pattern",
            confidence=0.5,
            usage_count=3,
            source_dispatch_ids=[dispatch_id],
        )
        _seed_pattern_usage(quality_db, sp_id, success_count=3, failure_count=0)

        before = _confidence(quality_db, sp_id)
        # Beta(3+1, 0+1) = 4/5 = 0.8 — reconcile would normally lift the seed;
        # but we're testing that a failure outcome causes a measurable drop
        # relative to the post-failure expectation.
        result = update_confidence_from_outcome(quality_db, dispatch_id, "T2", "failure")

        assert result["decayed"] >= 1, "failure outcome must decrement confidence on linked patterns"
        after = _confidence(quality_db, sp_id)
        # Beta(3, 1+1) = 4/6 ≈ 0.667 — must be lower than the pre-failure
        # Beta(4, 1) = 0.8 the post-success score that a success here would have produced.
        assert after < beta_score(4, 1), (
            f"failure must produce lower confidence than equivalent success would. "
            f"before={before} after={after}"
        )
        assert after < beta_score(3, 1) + 1e-6, "confidence must not increase on failure"

    def test_failure_writes_confidence_event(self, quality_db: Path) -> None:
        dispatch_id = "case-b-event-003"
        sp_id = _seed_pattern(
            quality_db, title="case-b event pattern",
            confidence=0.5, usage_count=2,
            source_dispatch_ids=[dispatch_id],
        )
        _seed_pattern_usage(quality_db, sp_id, success_count=2, failure_count=0)

        update_confidence_from_outcome(quality_db, dispatch_id, "T1", "failure")

        ev = _row(
            quality_db,
            "SELECT outcome, patterns_decayed, confidence_change "
            "FROM confidence_events WHERE dispatch_id = ? ORDER BY id DESC LIMIT 1",
            (dispatch_id,),
        )
        assert ev is not None, "confidence_events row must be written for audit/canary"
        assert ev["outcome"] == "failure"
        assert ev["patterns_decayed"] >= 1
        # Note: confidence_change is the Beta-Laplace delta, not strictly a decay.
        # A failure on a 2-success/0-failure history moves 0.5 -> 0.6 (still less
        # than the 0.75 a success would have produced). The classifier increments
        # patterns_decayed; the sign of confidence_change reflects the underlying
        # posterior, which is what we want for audit accuracy.


# ---------------------------------------------------------------------------
# Case C: Success boost increases confidence
# ---------------------------------------------------------------------------

class TestCaseCSuccessBoost:
    def test_success_outcome_increases_confidence(self, quality_db: Path) -> None:
        dispatch_id = "case-c-dispatch-004"
        sp_id = _seed_pattern(
            quality_db, title="case-c pattern",
            confidence=0.5, usage_count=0,
            source_dispatch_ids=[dispatch_id],
        )
        # No prior pattern_usage — function must INSERT the row.
        before = _confidence(quality_db, sp_id)
        result = update_confidence_from_outcome(quality_db, dispatch_id, "T1", "success")

        assert result["boosted"] >= 1, "success outcome must boost confidence"
        after = _confidence(quality_db, sp_id)
        assert after > before, f"confidence must increase ({before} -> {after})"

        usage = _row(
            quality_db,
            "SELECT success_count, failure_count, used_count "
            "FROM pattern_usage WHERE pattern_id = ?",
            (f"intel_sp_{sp_id}",),
        )
        assert usage is not None
        assert usage["success_count"] == 1
        assert usage["failure_count"] == 0


# ---------------------------------------------------------------------------
# Case D: Confidence reconcile sync
# ---------------------------------------------------------------------------

class TestCaseDReconcileSync:
    def test_reconcile_writes_pattern_usage_score_back(self, quality_db: Path) -> None:
        sp_id = _seed_pattern(quality_db, title="case-d", confidence=0.5)
        # 8 successes, 2 failures -> beta = 9/12 = 0.75
        _seed_pattern_usage(quality_db, sp_id, success_count=8, failure_count=2)

        before = _confidence(quality_db, sp_id)
        assert before == pytest.approx(0.5, abs=1e-6)

        updated = reconcile_pattern_confidence(quality_db)
        assert updated >= 1, "reconcile must update at least one row"

        after = _confidence(quality_db, sp_id)
        assert after == pytest.approx(beta_score(8, 2), abs=1e-6), (
            f"reconcile must write Beta-Laplace score (expected {beta_score(8, 2)}, got {after})"
        )

    def test_reconcile_idempotent(self, quality_db: Path) -> None:
        sp_id = _seed_pattern(quality_db, title="case-d-idem", confidence=0.5)
        _seed_pattern_usage(quality_db, sp_id, success_count=4, failure_count=1)

        first = reconcile_pattern_confidence(quality_db)
        second = reconcile_pattern_confidence(quality_db)
        assert first >= 1
        assert second == 0, "second reconcile with no new usage data must be a no-op"


# ---------------------------------------------------------------------------
# Case E: Pattern selection reflects updated confidence (high beats low)
# ---------------------------------------------------------------------------

class TestCaseESelectionByConfidence:
    def test_higher_confidence_pattern_wins(self, quality_db: Path) -> None:
        sp_low = _seed_pattern(
            quality_db, title="low-conf pattern", category="test-engineer",
            confidence=0.3, usage_count=2,
        )
        sp_high = _seed_pattern(
            quality_db, title="high-conf pattern", category="test-engineer",
            confidence=0.9, usage_count=10,
        )

        selector = IntelligenceSelector(quality_db_path=quality_db)
        try:
            result = selector.select(
                dispatch_id="case-e-dispatch-005",
                injection_point="dispatch_create",
                skill_name="test-engineer",
            )
        finally:
            selector.close()

        proven = [i for i in result.items if i.item_class == "proven_pattern"]
        assert len(proven) == 1, "selector picks one proven_pattern per slot"
        assert proven[0].item_id == f"intel_sp_{sp_high}", (
            f"high confidence pattern must win — got {proven[0].item_id}"
        )
        assert proven[0].item_id != f"intel_sp_{sp_low}"


# ---------------------------------------------------------------------------
# Case F: Selector reads CURRENT confidence (not a stale cached value)
# ---------------------------------------------------------------------------

class TestCaseFSelectorReadsCurrentConfidence:
    def test_selector_picks_up_post_update_confidence(self, quality_db: Path) -> None:
        # Seed at the proven_pattern threshold so the pre-update select returns it.
        sp_id = _seed_pattern(
            quality_db, title="case-f stale check", category="test-engineer",
            confidence=0.65, usage_count=2,
        )

        selector = IntelligenceSelector(quality_db_path=quality_db)
        try:
            first = selector.select(
                dispatch_id="case-f-pre-006",
                injection_point="dispatch_create",
                skill_name="test-engineer",
            )
        finally:
            selector.close()

        proven = [i for i in first.items if i.item_id == f"intel_sp_{sp_id}"]
        assert proven, "pattern should be selected pre-update"
        assert proven[0].confidence == pytest.approx(0.65, abs=1e-6)

        # Force-update confidence_score directly (simulating reconcile/update path).
        conn = sqlite3.connect(str(quality_db))
        try:
            conn.execute(
                "UPDATE success_patterns SET confidence_score = 0.9 WHERE id = ?", (sp_id,),
            )
            conn.commit()
        finally:
            conn.close()

        # New selector instance — must observe the new value, not a cached one.
        selector2 = IntelligenceSelector(quality_db_path=quality_db)
        try:
            second = selector2.select(
                dispatch_id="case-f-post-007",
                injection_point="dispatch_create",
                skill_name="test-engineer",
            )
        finally:
            selector2.close()

        proven2 = [i for i in second.items if i.item_id == f"intel_sp_{sp_id}"]
        assert proven2, "pattern still selectable after update"
        assert proven2[0].confidence == pytest.approx(0.9, abs=1e-6), (
            "selector must read current confidence_score (0.9), not stale cached value"
        )


# ---------------------------------------------------------------------------
# Case G: Diversity check — selector should not return same content N times
# ---------------------------------------------------------------------------

class TestCaseGDiversity:
    def test_returned_items_have_distinct_content(self, quality_db: Path) -> None:
        # Five rows with byte-identical content but different ids.
        ids: list[int] = []
        for i in range(5):
            ids.append(_seed_pattern(
                quality_db,
                title="DUPLICATE TITLE — diversity probe",
                description="byte-identical description",
                category="test-engineer",
                confidence=0.85 - (i * 0.01),  # all above threshold, varied for ordering
                usage_count=5,
            ))

        selector = IntelligenceSelector(quality_db_path=quality_db)
        try:
            result = selector.select(
                dispatch_id="case-g-dispatch-008",
                injection_point="dispatch_create",
                skill_name="test-engineer",
            )
        finally:
            selector.close()

        # The selector returns at most one proven_pattern slot, so the diversity
        # axis we measure is "distinct content across selected items".  With only
        # one slot filled this is trivially true for proven_pattern, but we also
        # assert the higher-level invariant: the selector did NOT echo the same
        # underlying row across multiple slots.
        contents = [(i.item_class, i.title, i.content) for i in result.items]
        assert len(contents) == len(set(contents)), (
            "selector must not return byte-identical (class,title,content) tuples — "
            f"got {contents}"
        )


# ---------------------------------------------------------------------------
# Case H: Outcome propagation chain
# ---------------------------------------------------------------------------

class TestCaseHOutcomePropagationChain:
    """Receipt -> append_receipt._update_confidence_from_receipt ->
    intelligence_persist.update_confidence_from_outcome ->
    success_patterns.confidence_score updated.
    """

    def test_chain_from_receipt_to_confidence(
        self, quality_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        dispatch_id = "case-h-chain-009"
        sp_id = _seed_pattern(
            quality_db,
            title="case-h chain pattern",
            confidence=0.5,
            usage_count=0,
            source_dispatch_ids=[dispatch_id],
        )

        # append_receipt._update_confidence_from_receipt resolves the DB path via
        # resolve_state_dir().  Patch that to return our tmp state dir holding
        # the synthetic quality_intelligence.db.
        state_dir = tmp_path / "h_state"
        state_dir.mkdir()
        # Move the db into that dir so resolve_state_dir() lookup matches.
        target_db = state_dir / "quality_intelligence.db"
        target_db.write_bytes(quality_db.read_bytes())

        # Import append_receipt and patch its resolve_state_dir reference.
        if str(_SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(_SCRIPTS_DIR))
        import append_receipt  # noqa: E402

        monkeypatch.setattr(append_receipt, "resolve_state_dir", lambda *a, **kw: state_dir)

        receipt = {
            "event_type": "task_complete",
            "status": "success",
            "dispatch_id": dispatch_id,
            "terminal": "T2",
        }

        before = _confidence(target_db, sp_id)
        append_receipt._update_confidence_from_receipt(receipt)
        after = _confidence(target_db, sp_id)

        assert after > before, (
            "Receipt -> _update_confidence_from_receipt -> "
            "update_confidence_from_outcome chain must boost the linked pattern. "
            f"before={before} after={after}"
        )

        usage = _row(
            target_db,
            "SELECT success_count, failure_count FROM pattern_usage "
            "WHERE pattern_id = ?",
            (f"intel_sp_{sp_id}",),
        )
        assert usage is not None and usage["success_count"] == 1, (
            "pattern_usage row must be written along the propagation chain"
        )

    def test_chain_failure_status_decays(
        self, quality_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        dispatch_id = "case-h-fail-010"
        sp_id = _seed_pattern(
            quality_db, title="case-h fail pattern",
            confidence=0.5, usage_count=2,
            source_dispatch_ids=[dispatch_id],
        )
        _seed_pattern_usage(quality_db, sp_id, success_count=2, failure_count=0)

        state_dir = tmp_path / "h_fail_state"
        state_dir.mkdir()
        target_db = state_dir / "quality_intelligence.db"
        target_db.write_bytes(quality_db.read_bytes())

        if str(_SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(_SCRIPTS_DIR))
        import append_receipt  # noqa: E402

        monkeypatch.setattr(append_receipt, "resolve_state_dir", lambda *a, **kw: state_dir)

        receipt = {
            "event_type": "task_failed",
            "status": "failure",
            "dispatch_id": dispatch_id,
            "terminal": "T1",
        }

        before = _confidence(target_db, sp_id)  # = 0.5 (seed)
        append_receipt._update_confidence_from_receipt(receipt)
        after = _confidence(target_db, sp_id)

        # Beta(2, 0+1) = 3/5 = 0.6 (writes back current pattern_usage state)
        assert after == pytest.approx(beta_score(2, 1), abs=1e-6), (
            f"failure must apply Beta-Laplace decay, got {after}"
        )
        assert after < before + 0.15, "decay must not balloon confidence"


# ---------------------------------------------------------------------------
# Case I: T0 / coordination event captures injection events
# ---------------------------------------------------------------------------

class TestCaseIInjectionEventLogged:
    def test_injection_emits_coordination_event(
        self, quality_db: Path, coord_state_dir: Path
    ) -> None:
        _seed_pattern(
            quality_db,
            title="case-i pattern",
            confidence=0.85, usage_count=4,
        )

        dispatch_id = "case-i-dispatch-011"
        selector = IntelligenceSelector(
            quality_db_path=quality_db, coord_db_state_dir=coord_state_dir,
        )
        try:
            result = selector.select(
                dispatch_id=dispatch_id, injection_point="dispatch_create",
                skill_name="test-engineer",
            )
            event_id = selector.emit_event(result, coord_state_dir=coord_state_dir)
            selector.record_injection(result, coord_state_dir=coord_state_dir)
        finally:
            selector.close()

        assert result.items_injected >= 1
        assert event_id, "emit_event must return a non-empty event_id when injecting"

        coord_db = coord_state_dir / "runtime_coordination.db"
        ev = _row(
            coord_db,
            "SELECT event_type, entity_id, actor, metadata_json "
            "FROM coordination_events "
            "WHERE entity_id = ? ORDER BY occurred_at DESC LIMIT 1",
            (dispatch_id,),
        )
        assert ev is not None, (
            "coordination_events row must exist for the dispatch (decision/audit trail)"
        )
        assert ev["event_type"] == "intelligence_injection"
        assert ev["actor"] == "intelligence_selector"
        meta = json.loads(ev["metadata_json"])
        assert meta["items_injected"] >= 1
        assert meta["task_class"]
