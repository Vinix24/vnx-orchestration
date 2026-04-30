#!/usr/bin/env python3
"""Regression tests for PR #311 round-2 codex findings.

Three findings covered:

* **Finding 1** — subprocess path silently bypassed the confidence loop.
  ``_build_intelligence_section`` selected items but never called
  ``IntelligenceSelector.emit_event()`` or ``record_injection()`` so
  ``intelligence_injections`` and ``pattern_usage`` had no dispatch-scoped rows
  for the later feedback step to update.

* **Finding 2** — random ``item_id`` values fragmented the same underlying
  pattern into many ``pattern_usage`` rows, breaking dedup and scrambling
  ``ignored/used/confidence`` aggregates.  ``pattern_hash`` was also reused
  from the same random value, defeating its purpose.

* **Finding 3** — ``_update_pattern_confidence`` incremented
  ``pattern_usage.used_count`` (and ``success_count`` / ``failure_count``) for
  every *offered* item on both success and failure runs.  Existing consumers
  treat ``used_count > 0`` as evidence that a worker actually used a pattern;
  the offered-only feedback loop must touch only timestamps + the confidence
  score in ``success_patterns``.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Quality DB scaffolding (mirrors the production schema used by these paths)
# ---------------------------------------------------------------------------

def _bootstrap_quality_db(db_path: Path) -> None:
    """Create the minimum schema needed by intelligence_selector + feedback."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, description TEXT, category TEXT,
            confidence_score REAL DEFAULT 0.0,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT,
            first_seen TEXT, last_used TEXT,
            valid_from DATETIME, valid_until DATETIME
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, description TEXT, category TEXT, severity TEXT,
            why_problematic TEXT, better_alternative TEXT,
            occurrence_count INTEGER DEFAULT 0,
            first_seen TEXT, last_seen TEXT,
            valid_from DATETIME, valid_until DATETIME
        );
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT, rule_type TEXT, description TEXT,
            recommendation TEXT, confidence REAL DEFAULT 0.0,
            triggered_count INTEGER DEFAULT 0, last_triggered TEXT,
            valid_from DATETIME, valid_until DATETIME
        );
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT UNIQUE, terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT,
            pattern_count INTEGER DEFAULT 0,
            prevention_rule_count INTEGER DEFAULT 0,
            outcome_status TEXT, dispatched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            used_count INTEGER DEFAULT 0,
            ignored_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_used TEXT,
            last_offered TEXT,
            confidence REAL DEFAULT 1.0,
            created_at TEXT,
            updated_at TEXT,
            dispatch_id TEXT
        );
        CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
            dispatch_id   TEXT NOT NULL,
            pattern_id    TEXT NOT NULL,
            pattern_title TEXT NOT NULL,
            offered_at    TEXT NOT NULL,
            PRIMARY KEY (dispatch_id, pattern_id)
        );
        """
    )
    conn.commit()
    conn.close()


def _seed_proven_pattern(db_path: Path, *, title: str, confidence: float = 0.8,
                         usage_count: int = 5, category: str = "backend-developer") -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        """INSERT INTO success_patterns
           (title, description, category, confidence_score, usage_count,
            first_seen, last_used)
           VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (title, f"desc:{title}", category, confidence, usage_count),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


# ---------------------------------------------------------------------------
# Finding 1 — subprocess path must persist the injection
# ---------------------------------------------------------------------------

class TestSubprocessInjectionPersists:
    """``_build_intelligence_section`` must emit and record the selection."""

    def test_emit_event_and_record_injection_called(self, tmp_path):
        """selector.emit_event AND selector.record_injection must run.

        Without these calls the post-dispatch feedback step has no rows to
        update — the confidence loop becomes a no-op for the subprocess route.
        """
        from subprocess_dispatch import _build_intelligence_section
        import intelligence_selector as _mod

        instance = MagicMock()
        # Real result with one item so the section is non-empty
        from intelligence_selector import IntelligenceItem, InjectionResult
        item = IntelligenceItem(
            item_id="intel_sp_1",
            item_class="proven_pattern",
            title="Test pattern",
            content="Use proper checks.",
            confidence=0.9,
            evidence_count=5,
            last_seen="2026-04-29T00:00:00.000000Z",
            scope_tags=["backend-developer"],
        )
        result = InjectionResult(
            injection_point="dispatch_create",
            injected_at="2026-04-29T00:00:00.000000Z",
            items=[item],
            suppressed=[],
            task_class="coding_interactive",
            dispatch_id="d-r2-001",
        )
        instance.select.return_value = result
        mock_cls = MagicMock(return_value=instance)

        with patch.object(_mod, "IntelligenceSelector", mock_cls):
            section = _build_intelligence_section("d-r2-001", "backend-developer")

        assert "Test pattern" in section
        # Both event emission and injection record must fire.
        assert instance.emit_event.called, (
            "Finding 1: emit_event() must be called so the coordination event "
            "for this dispatch is recorded"
        )
        assert instance.record_injection.called, (
            "Finding 1: record_injection() must be called so pattern_usage / "
            "dispatch_pattern_offered rows exist for the feedback step"
        )

    def test_pattern_usage_row_written_after_selection(self, tmp_path):
        """End-to-end: section build must leave a pattern_usage row behind.

        Uses a real (temp) quality_intelligence.db so the feedback loop has
        something to query later.
        """
        from subprocess_dispatch import _build_intelligence_section

        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        db_path = state_dir / "quality_intelligence.db"
        _bootstrap_quality_db(db_path)
        _seed_proven_pattern(db_path, title="Stable pattern A",
                             confidence=0.9, usage_count=4)

        with patch("dispatch_context._default_state_dir", return_value=state_dir):
            section = _build_intelligence_section("d-r2-002", "backend-developer")

        # Section may be empty depending on coord-DB availability; what we
        # require is that the pattern_usage row exists for the feedback loop.
        del section
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT pattern_id, dispatch_id FROM dispatch_pattern_offered "
            "WHERE dispatch_id = ?",
            ("d-r2-002",),
        ).fetchall()
        conn.close()

        assert rows, (
            "Finding 1: dispatch_pattern_offered must have a row for this "
            "dispatch so _update_pattern_confidence can find offered items"
        )


# ---------------------------------------------------------------------------
# Finding 2 — stable item_id, distinct pattern_hash
# ---------------------------------------------------------------------------

class TestStableItemId:
    """The same underlying pattern must produce the same item_id every call."""

    def test_same_pattern_gets_same_item_id_across_selections(self, tmp_path):
        """Two consecutive ``select()`` calls return identical item_ids.

        Random uuids broke this — every call generated a fresh pattern_id and
        pattern_usage rows multiplied instead of aggregating.
        """
        from intelligence_selector import IntelligenceSelector

        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap_quality_db(db_path)
        _seed_proven_pattern(db_path, title="Repeatable pattern",
                             confidence=0.85, usage_count=4)

        selector = IntelligenceSelector(quality_db_path=db_path)
        try:
            r1 = selector.select("d-001", "dispatch_create",
                                 skill_name="backend-developer")
            r2 = selector.select("d-002", "dispatch_create",
                                 skill_name="backend-developer")
        finally:
            selector.close()

        ids_1 = {i.item_id for i in r1.items}
        ids_2 = {i.item_id for i in r2.items}
        assert ids_1, "expected at least one item to be selected"
        assert ids_1 == ids_2, (
            f"Finding 2: stable item_id required across calls. "
            f"call1={ids_1!r} call2={ids_2!r}"
        )

    def test_pattern_usage_dedups_across_dispatches(self, tmp_path):
        """Two dispatches offering the same pattern => ONE pattern_usage row."""
        from intelligence_selector import IntelligenceSelector
        from runtime_coordination import init_schema

        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap_quality_db(db_path)
        _seed_proven_pattern(db_path, title="Dedup target",
                             confidence=0.85, usage_count=6)
        coord_dir = tmp_path / "state"
        coord_dir.mkdir()
        init_schema(str(coord_dir))

        selector = IntelligenceSelector(
            quality_db_path=db_path,
            coord_db_state_dir=coord_dir,
        )
        try:
            r1 = selector.select("dispatch-A", "dispatch_create",
                                 skill_name="backend-developer")
            selector.record_injection(r1)
            r2 = selector.select("dispatch-B", "dispatch_create",
                                 skill_name="backend-developer")
            selector.record_injection(r2)
        finally:
            selector.close()

        conn = sqlite3.connect(str(db_path))
        n_pattern_rows = conn.execute(
            "SELECT COUNT(*) FROM pattern_usage"
        ).fetchone()[0]
        n_offered_rows = conn.execute(
            "SELECT COUNT(*) FROM dispatch_pattern_offered"
        ).fetchone()[0]
        conn.close()

        # ONE pattern_usage row per underlying pattern
        assert n_pattern_rows == 1, (
            f"Finding 2: pattern_usage must dedup; got {n_pattern_rows} rows "
            "for one underlying pattern offered to two dispatches"
        )
        # TWO offered rows (one per dispatch) — that table is the per-dispatch
        # junction, not pattern identity
        assert n_offered_rows == 2, (
            f"Finding 2: dispatch_pattern_offered should keep per-dispatch "
            f"audit rows; expected 2, got {n_offered_rows}"
        )

    def test_pattern_hash_differs_from_pattern_id(self, tmp_path):
        """``pattern_hash`` must NOT be identical to ``pattern_id``.

        The original code used the same random value for both fields, defeating
        the purpose of having a separate content hash.
        """
        from intelligence_selector import IntelligenceSelector
        from runtime_coordination import init_schema

        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap_quality_db(db_path)
        _seed_proven_pattern(db_path, title="Hash-id distinct",
                             confidence=0.85, usage_count=4)
        coord_dir = tmp_path / "state"
        coord_dir.mkdir()
        init_schema(str(coord_dir))

        selector = IntelligenceSelector(
            quality_db_path=db_path,
            coord_db_state_dir=coord_dir,
        )
        try:
            result = selector.select("d-hash", "dispatch_create",
                                     skill_name="backend-developer")
            selector.record_injection(result)
        finally:
            selector.close()

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT pattern_id, pattern_hash FROM pattern_usage"
        ).fetchall()
        conn.close()

        assert rows, "expected at least one pattern_usage row"
        for pattern_id, pattern_hash in rows:
            assert pattern_hash != pattern_id, (
                "Finding 2: pattern_hash must be a real hash, not the same "
                "string as pattern_id"
            )
            # learning_loop convention is sha1 hex (40 chars).
            assert len(pattern_hash) == 40, (
                f"Finding 2: pattern_hash should be sha1 hex; got "
                f"{pattern_hash!r}"
            )


# ---------------------------------------------------------------------------
# Finding 3 — offered != used: don't bump used/success/failure counts
# ---------------------------------------------------------------------------

class TestOfferedDoesNotIncrementUsedCount:
    """Confidence feedback must not corrupt the used/success/failure signal."""

    def _seed_offered(self, db_path: Path, dispatch_id: str,
                     pattern_id: str, pattern_title: str) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO success_patterns
                  (title, description, confidence_score, usage_count,
                   first_seen, last_used)
               VALUES (?, '', 0.7, 0, datetime('now'), datetime('now'))""",
            (pattern_title,),
        )
        conn.execute(
            """INSERT INTO pattern_usage
                  (pattern_id, pattern_title, pattern_hash, used_count,
                   ignored_count, success_count, failure_count,
                   last_offered, confidence, created_at, updated_at)
               VALUES (?, ?, ?, 0, 0, 0, 0, datetime('now'), 1.0,
                       datetime('now'), datetime('now'))""",
            (pattern_id, pattern_title, "deadbeef" * 5),
        )
        conn.execute(
            """INSERT INTO dispatch_pattern_offered
                  (dispatch_id, pattern_id, pattern_title, offered_at)
               VALUES (?, ?, ?, datetime('now'))""",
            (dispatch_id, pattern_id, pattern_title),
        )
        conn.commit()
        conn.close()

    def test_success_does_not_bump_used_or_success_count(self, tmp_path):
        from subprocess_dispatch import _update_pattern_confidence

        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap_quality_db(db_path)
        self._seed_offered(db_path, "d-S1", "intel_sp_1", "Pattern One")

        updated = _update_pattern_confidence("d-S1", "success", db_path)
        assert updated == 1

        conn = sqlite3.connect(str(db_path))
        used, succ, fail = conn.execute(
            "SELECT used_count, success_count, failure_count FROM pattern_usage"
        ).fetchone()
        conf = conn.execute(
            "SELECT confidence_score FROM success_patterns"
        ).fetchone()[0]
        conn.close()

        assert used == 0, (
            f"Finding 3: used_count must stay 0 for offered-only success; got {used}"
        )
        assert succ == 0, (
            f"Finding 3: success_count must stay 0 for offered-only success; got {succ}"
        )
        assert fail == 0, (
            f"Finding 3: failure_count must stay 0 for offered-only success; got {fail}"
        )
        # The confidence side-effect on success_patterns is the whole point —
        # +0.05 still applied.
        assert conf == pytest.approx(0.75, abs=1e-6), (
            f"success_patterns.confidence_score should boost +0.05; got {conf}"
        )

    def test_failure_does_not_bump_used_or_failure_count(self, tmp_path):
        from subprocess_dispatch import _update_pattern_confidence

        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap_quality_db(db_path)
        self._seed_offered(db_path, "d-F1", "intel_sp_2", "Pattern Two")

        updated = _update_pattern_confidence("d-F1", "failure", db_path)
        assert updated == 1

        conn = sqlite3.connect(str(db_path))
        used, succ, fail = conn.execute(
            "SELECT used_count, success_count, failure_count FROM pattern_usage"
        ).fetchone()
        conf = conn.execute(
            "SELECT confidence_score FROM success_patterns"
        ).fetchone()[0]
        conn.close()

        assert used == 0, (
            f"Finding 3: used_count must stay 0 on offered-only failure; got {used}"
        )
        assert fail == 0, (
            f"Finding 3: failure_count must stay 0 on offered-only failure; got {fail}"
        )
        assert succ == 0, (
            f"Finding 3: success_count must stay 0 on offered-only failure; got {succ}"
        )
        assert conf == pytest.approx(0.6, abs=1e-6), (
            f"success_patterns.confidence_score should decay -0.10; got {conf}"
        )

    def test_last_used_still_updated(self, tmp_path):
        """We DO want last_used / updated_at to advance — only the counters
        must stay put."""
        from subprocess_dispatch import _update_pattern_confidence

        db_path = tmp_path / "quality_intelligence.db"
        _bootstrap_quality_db(db_path)
        self._seed_offered(db_path, "d-T1", "intel_sp_3", "Pattern Three")

        # zero out last_used so the change is detectable
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE pattern_usage SET last_used = NULL, updated_at = NULL")
        conn.commit()
        conn.close()

        _update_pattern_confidence("d-T1", "success", db_path)

        conn = sqlite3.connect(str(db_path))
        last_used, updated_at = conn.execute(
            "SELECT last_used, updated_at FROM pattern_usage"
        ).fetchone()
        conn.close()

        assert last_used is not None
        assert updated_at is not None


# ---------------------------------------------------------------------------
# Static guard: regate findings stay fixed in the source
# ---------------------------------------------------------------------------

class TestSourceLevelGuards:
    """Cheap source-level checks that protect against regressions."""

    def test_subprocess_dispatch_calls_record_injection(self):
        # record_injection and emit_event live in dispatch_context.py after the
        # subprocess_dispatch.py split (OI-1205).  Check there instead.
        src = (SCRIPTS_LIB / "dispatch_context.py").read_text(encoding="utf-8")
        assert "record_injection(" in src, (
            "Finding 1: dispatch_context.py must call record_injection()"
        )
        assert "emit_event(" in src, (
            "Finding 1: dispatch_context.py must call emit_event()"
        )

    def test_intelligence_selector_uses_stable_item_id(self):
        src = (SCRIPTS_LIB / "intelligence_selector.py").read_text(encoding="utf-8")
        assert "_stable_item_id(" in src, (
            "Finding 2: intelligence_selector.py must use _stable_item_id() "
            "for content-derived ids"
        )
        # The old random helper must no longer be used as the item_id source.
        assert "item_id=_item_id()" not in src, (
            "Finding 2: random uuid item_id assignments still present"
        )

    def test_offered_only_does_not_increment_used_count(self):
        src = (SCRIPTS_LIB / "subprocess_dispatch.py").read_text(encoding="utf-8")
        # Targeted: the SQL fragment that bumped used_count must be gone.
        assert "used_count    = used_count + 1" not in src, (
            "Finding 3: used_count increment in offered-only path is back"
        )
        assert "used_count     = used_count + 1" not in src, (
            "Finding 3: used_count increment in offered-only path is back"
        )
        assert "success_count = success_count + 1" not in src, (
            "Finding 3: success_count increment in offered-only path is back"
        )
        assert "failure_count  = failure_count + 1" not in src, (
            "Finding 3: failure_count increment in offered-only path is back"
        )
