"""Tests for the self-learning path unification fix (PR-SELFLEARN-ACTIVE).

Root cause: memory_consolidator.py used VNX_STATE_DIR (env-pollutable) while
consolidator.py used VNX_DATA_DIR/state (canonical). Stale shell profile exports
caused writers and the dream reader to resolve different quality_intelligence.db
files. These tests verify that all parties now use the same canonical path.

ADR-007: project_id-scoped canonical paths.
ADR-019: dream consolidation produces proposals; human gate preserved.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dream"))


# ---------------------------------------------------------------------------
# Shared dream schema + helpers
# ---------------------------------------------------------------------------

_DREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_cycles (
    cycle_id          TEXT    NOT NULL,
    project_id        TEXT    NOT NULL DEFAULT 'vnx-dev',
    started_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at      TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','running','completed','failed','reviewed','rejected')),
    provider          TEXT    NOT NULL DEFAULT 'kimi',
    insights_input    INTEGER NOT NULL DEFAULT 0,
    merged_count      INTEGER NOT NULL DEFAULT 0,
    dropped_count     INTEGER NOT NULL DEFAULT 0,
    archived_count    INTEGER NOT NULL DEFAULT 0,
    flagged_count     INTEGER NOT NULL DEFAULT 0,
    operator_reviewed INTEGER NOT NULL DEFAULT 0,
    report_path       TEXT,
    PRIMARY KEY (cycle_id, project_id)
);
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    pattern_type TEXT NOT NULL DEFAULT 'approach',
    category TEXT NOT NULL DEFAULT 'general',
    title TEXT NOT NULL DEFAULT 'title',
    description TEXT NOT NULL DEFAULT 'desc',
    pattern_data TEXT NOT NULL DEFAULT '{}',
    confidence_score REAL DEFAULT 0.5,
    usage_count INTEGER DEFAULT 1,
    source_dispatch_ids TEXT DEFAULT '[]',
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS antipatterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL DEFAULT 'vnx-dev',
    pattern_type TEXT NOT NULL DEFAULT 'approach',
    category TEXT NOT NULL DEFAULT 'general',
    title TEXT NOT NULL DEFAULT 'title',
    description TEXT NOT NULL DEFAULT 'desc',
    pattern_data TEXT NOT NULL DEFAULT '{}',
    why_problematic TEXT NOT NULL DEFAULT 'bad',
    severity TEXT NOT NULL DEFAULT 'medium',
    occurrence_count INTEGER DEFAULT 1,
    source_dispatch_ids TEXT DEFAULT '[]',
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_FAKE_CONSOLIDATION = {
    "merged": [],
    "dropped": [],
    "archived": [],
    "flagged": [{"id": 1, "table": "success_patterns", "reason": "novel"}],
    "summary": "Flagged 1 novel pattern for review.",
}


def _make_db(path: Path, *, n_patterns: int = 0) -> Path:
    """Create a quality_intelligence.db at path with optional seeded patterns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_DREAM_SCHEMA)
    for i in range(n_patterns):
        conn.execute(
            "INSERT INTO success_patterns (project_id, title) VALUES (?, ?)",
            ("vnx-dev", f"seeded-pattern-{i}"),
        )
    conn.commit()
    conn.close()
    return path


def _make_fresh_receipts(data_root: Path) -> None:
    processed = data_root / "receipts" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "receipt-fresh.ndjson").write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Writer path unification: memory_consolidator uses VNX_DATA_DIR/state
# ---------------------------------------------------------------------------


class TestMemoryConsolidatorCanonicalPath:
    """memory_consolidator.MemoryConsolidator.db_path must derive from VNX_DATA_DIR/state,
    not VNX_STATE_DIR, so stale shell profile exports don't split the path."""

    def test_db_path_uses_vnx_data_dir_not_state_dir(self, tmp_path, monkeypatch):
        """With a stale VNX_STATE_DIR in the env, db_path still resolves from VNX_DATA_DIR."""
        canonical_data_dir = tmp_path / "canonical-vnx-data"
        stale_state_dir = tmp_path / "stale-state"

        # Simulate the pre-ADR-007 pollution scenario:
        # VNX_DATA_DIR resolves to the project_id-scoped canonical path,
        # but a stale VNX_STATE_DIR from ~/.zshrc still points to the old location.
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(canonical_data_dir))
        monkeypatch.setenv("VNX_STATE_DIR", str(stale_state_dir))

        import memory_consolidator
        import importlib
        importlib.reload(memory_consolidator)

        mc = memory_consolidator.MemoryConsolidator()

        expected = (canonical_data_dir / "state" / "quality_intelligence.db").resolve()
        assert mc.db_path == expected, (
            f"db_path={mc.db_path} but expected canonical path {expected}. "
            f"Stale VNX_STATE_DIR={stale_state_dir} must NOT win."
        )

    def test_db_path_does_not_use_stale_state_dir(self, tmp_path, monkeypatch):
        """db_path must NOT point at a stale VNX_STATE_DIR (the pre-ADR-007 split)."""
        stale_state_dir = tmp_path / "stale-state"
        canonical_data_dir = tmp_path / "canonical-vnx-data"

        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(canonical_data_dir))
        monkeypatch.setenv("VNX_STATE_DIR", str(stale_state_dir))

        import memory_consolidator
        import importlib
        importlib.reload(memory_consolidator)

        mc = memory_consolidator.MemoryConsolidator()

        assert stale_state_dir not in mc.db_path.parents, (
            f"db_path={mc.db_path} is under stale VNX_STATE_DIR={stale_state_dir}. "
            "Writers and readers must use the same VNX_DATA_DIR/state canonical path."
        )


# ---------------------------------------------------------------------------
# 2. Writer + reader agreement: same canonical db path
# ---------------------------------------------------------------------------


class TestWriterReaderCanonicalPathAgreement:
    """Both memory_consolidator (writer) and dream consolidator (reader) must resolve
    to the same quality_intelligence.db when the env is consistent."""

    def test_consolidator_and_memory_consolidator_agree_on_db_path(
        self, tmp_path, monkeypatch
    ):
        """_resolve_data_root(None) and MemoryConsolidator.db_path must point to same db."""
        canonical_data_dir = tmp_path / "shared-vnx-data"

        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(canonical_data_dir))
        # Explicitly remove VNX_STATE_DIR to test clean-env case
        monkeypatch.delenv("VNX_STATE_DIR", raising=False)

        import consolidator
        import memory_consolidator
        import importlib
        importlib.reload(memory_consolidator)
        importlib.reload(consolidator)

        reader_root = consolidator._resolve_data_root(None)
        reader_db = reader_root / "state" / "quality_intelligence.db"

        mc = memory_consolidator.MemoryConsolidator()
        writer_db = mc.db_path

        assert writer_db == reader_db.resolve(), (
            f"Writer db_path={writer_db} != reader db_path={reader_db}. "
            "Path split: dream consolidator reads an empty db while patterns accumulate elsewhere."
        )

    def test_agreement_holds_with_stale_state_dir_in_env(self, tmp_path, monkeypatch):
        """Path agreement holds even when a stale VNX_STATE_DIR is exported."""
        canonical_data_dir = tmp_path / "shared-vnx-data"
        stale_state_dir = tmp_path / "pre-adr007-state"

        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(canonical_data_dir))
        monkeypatch.setenv("VNX_STATE_DIR", str(stale_state_dir))

        import consolidator
        import memory_consolidator
        import importlib
        importlib.reload(memory_consolidator)
        importlib.reload(consolidator)

        reader_root = consolidator._resolve_data_root(None)
        reader_db = (reader_root / "state" / "quality_intelligence.db").resolve()
        writer_db = memory_consolidator.MemoryConsolidator().db_path

        assert writer_db == reader_db, (
            f"Stale VNX_STATE_DIR={stale_state_dir} caused path split: "
            f"writer={writer_db} vs reader={reader_db}."
        )


# ---------------------------------------------------------------------------
# 3. End-to-end: seed patterns → dream cycle consolidates (not skipped)
# ---------------------------------------------------------------------------


class TestDreamCycleConsolidatesSeededPatterns:
    """Seed N>=threshold patterns in the canonical db, run dream cycle → consolidates."""

    def test_seeded_patterns_cause_consolidation_not_skip(self, tmp_path):
        """N>=1 success_patterns in db → dream cycle status is NOT skipped."""
        data_root = tmp_path / "vnx-data"
        db_path = data_root / "state" / "quality_intelligence.db"
        _make_db(db_path, n_patterns=3)
        _make_fresh_receipts(data_root)

        import consolidator
        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=True, data_root=data_root
            )

        assert result.get("status") != "skipped", (
            f"Expected consolidation but got status={result.get('status')}. "
            f"detail={result.get('detail')}"
        )
        assert "cycle_id" in result

    def test_seeded_patterns_produce_pending_review(self, tmp_path):
        """After seeding patterns and running the dream cycle, pending-review.json exists."""
        data_root = tmp_path / "vnx-data"
        db_path = data_root / "state" / "quality_intelligence.db"
        _make_db(db_path, n_patterns=5)
        _make_fresh_receipts(data_root)

        import consolidator
        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=True, data_root=data_root
            )

        review_path = Path(result["review_path"])
        assert review_path.exists(), f"pending-review.json not written: {review_path}"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        assert review["requires_operator_review"] is True, (
            "Human gate must be preserved: requires_operator_review must be True (ADR-019)."
        )

    def test_zero_patterns_still_skips(self, tmp_path):
        """Empty db with fresh receipts → dream cycle is skipped (insufficient_data)."""
        data_root = tmp_path / "vnx-data"
        db_path = data_root / "state" / "quality_intelligence.db"
        _make_db(db_path, n_patterns=0)
        _make_fresh_receipts(data_root)

        import consolidator
        with patch("consolidator._dispatch_kimi_consolidation") as mock_kimi:
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, data_root=data_root
            )

        assert result["status"] == "skipped"
        assert result["reason"] == "insufficient_data"
        mock_kimi.assert_not_called()

    def test_dream_cycle_row_status_is_completed_not_skipped(self, tmp_path):
        """Non-dry-run: dream_cycles row inserted with status='completed' (not 'skipped')."""
        data_root = tmp_path / "vnx-data"
        db_path = data_root / "state" / "quality_intelligence.db"
        _make_db(db_path, n_patterns=2)
        _make_fresh_receipts(data_root)

        import consolidator
        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=False, data_root=data_root
            )

        assert result.get("status") != "skipped", (
            f"Cycle was skipped with core_count=0 even though patterns were seeded. "
            f"detail={result.get('detail')}"
        )

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT status FROM dream_cycles WHERE project_id = 'vnx-dev'"
        ).fetchall()
        conn.close()

        assert len(rows) == 1, f"Expected 1 dream_cycles row, got {len(rows)}"
        assert rows[0][0] == "completed", f"Expected status='completed', got {rows[0][0]}"

    def test_human_gate_preserved_no_auto_apply(self, tmp_path):
        """ADR-019: consolidation never auto-applies; requires_operator_review must be True."""
        data_root = tmp_path / "vnx-data"
        db_path = data_root / "state" / "quality_intelligence.db"
        _make_db(db_path, n_patterns=1)
        _make_fresh_receipts(data_root)

        import consolidator
        with patch(
            "consolidator._dispatch_kimi_consolidation",
            return_value=_FAKE_CONSOLIDATION,
        ):
            result = consolidator.run_dream_cycle(
                "vnx-dev", db_path, dry_run=False, data_root=data_root
            )

        review_path = Path(result["review_path"])
        review = json.loads(review_path.read_text(encoding="utf-8"))
        assert review["requires_operator_review"] is True


# ---------------------------------------------------------------------------
# 4. pattern_extractor._default_db_path uses VNX_DATA_DIR
# ---------------------------------------------------------------------------


class TestPatternExtractorCanonicalPath:
    """pattern_extractor._default_db_path must use VNX_DATA_DIR/state, not VNX_STATE_DIR."""

    def test_default_db_path_uses_vnx_data_dir(self, tmp_path, monkeypatch):
        """VNX_DATA_DIR set → _default_db_path returns VNX_DATA_DIR/state/quality_intelligence.db."""
        canonical = tmp_path / "vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR", str(canonical))
        monkeypatch.delenv("VNX_STATE_DIR", raising=False)

        import pattern_extractor
        import importlib
        importlib.reload(pattern_extractor)

        result = pattern_extractor._default_db_path()
        expected = canonical / "state" / "quality_intelligence.db"
        assert result == expected

    def test_default_db_path_vnx_data_dir_beats_stale_state_dir(
        self, tmp_path, monkeypatch
    ):
        """VNX_DATA_DIR wins over VNX_STATE_DIR when both are set."""
        canonical = tmp_path / "canonical"
        stale = tmp_path / "stale"
        monkeypatch.setenv("VNX_DATA_DIR", str(canonical))
        monkeypatch.setenv("VNX_STATE_DIR", str(stale))

        import pattern_extractor
        import importlib
        importlib.reload(pattern_extractor)

        result = pattern_extractor._default_db_path()
        assert stale not in result.parents, (
            f"_default_db_path={result} is under stale VNX_STATE_DIR={stale}"
        )
        assert canonical / "state" / "quality_intelligence.db" == result

    def test_default_db_path_falls_back_to_state_dir_when_no_data_dir(
        self, tmp_path, monkeypatch
    ):
        """When VNX_DATA_DIR is unset, falls back to VNX_STATE_DIR (backward compat)."""
        state = tmp_path / "fallback-state"
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.setenv("VNX_STATE_DIR", str(state))

        import pattern_extractor
        import importlib
        importlib.reload(pattern_extractor)

        result = pattern_extractor._default_db_path()
        assert result == state / "quality_intelligence.db"


# ---------------------------------------------------------------------------
# 5. resolve_project_id() public API in vnx_paths
# ---------------------------------------------------------------------------


class TestResolveProjectId:
    """vnx_paths.resolve_project_id() must exist and return a string or None."""

    def test_resolve_project_id_exists_and_is_callable(self):
        """resolve_project_id() is importable and callable."""
        import vnx_paths
        assert hasattr(vnx_paths, "resolve_project_id"), (
            "resolve_project_id() not found in vnx_paths — required by nightly pipeline."
        )
        result = vnx_paths.resolve_project_id()
        assert result is None or isinstance(result, str), (
            f"resolve_project_id() returned unexpected type: {type(result)}"
        )

    def test_resolve_project_id_with_env_override(self, monkeypatch):
        """VNX_PROJECT_ID env var is honored by resolve_project_id()."""
        monkeypatch.setenv("VNX_PROJECT_ID", "test-proj")
        import vnx_paths
        import importlib
        importlib.reload(vnx_paths)

        result = vnx_paths.resolve_project_id()
        assert result == "test-proj", (
            f"Expected 'test-proj' from VNX_PROJECT_ID env var, got {result!r}"
        )

    def test_resolve_project_id_returns_none_or_valid_id(self):
        """resolve_project_id() returns None or a valid project_id (no crash)."""
        import vnx_paths
        result = vnx_paths.resolve_project_id()
        if result is not None:
            import re
            assert re.match(r"^[a-z][a-z0-9-]{1,31}$", result), (
                f"resolve_project_id() returned invalid project_id: {result!r}"
            )


# ---------------------------------------------------------------------------
# 6. Nightly pipeline has dream phase
# ---------------------------------------------------------------------------


class TestNightlyPipelineHasDreamPhase:
    """Verify the nightly pipeline script includes the dream consolidation phase."""

    def _read_pipeline(self) -> str:
        path = REPO_ROOT / "scripts" / "nightly_intelligence_pipeline.sh"
        return path.read_text(encoding="utf-8")

    def test_dream_consolidation_phase_exists(self):
        """nightly_intelligence_pipeline.sh contains a dream-consolidation phase."""
        content = self._read_pipeline()
        assert "4b-dream-consolidation" in content, (
            "Dream consolidation phase '4b-dream-consolidation' not found in nightly pipeline. "
            "Self-learning loop requires a nightly trigger (dispatch: Fix 3)."
        )

    def test_dream_phase_calls_dream_consolidator(self):
        """Dream phase invokes scripts/dream/consolidator.py."""
        content = self._read_pipeline()
        assert "dream/consolidator.py" in content, (
            "Nightly pipeline dream phase does not invoke dream/consolidator.py."
        )

    def test_dream_phase_passes_project_id(self):
        """Dream phase passes --project-id to the consolidator."""
        content = self._read_pipeline()
        assert "--project-id" in content, (
            "Nightly pipeline dream phase does not pass --project-id to consolidator (ADR-007)."
        )

    def test_dream_phase_passes_db_path(self):
        """Dream phase passes --db-path so it uses the same DB_PATH as other phases."""
        content = self._read_pipeline()
        assert "--db-path" in content, (
            "Nightly pipeline dream phase does not pass --db-path, risking path split."
        )

    def test_dream_enabled_flag_exists(self):
        """VNX_DREAM_ENABLED flag allows operators to disable the dream phase."""
        content = self._read_pipeline()
        assert "VNX_DREAM_ENABLED" in content, (
            "Nightly pipeline has no VNX_DREAM_ENABLED guard — operators cannot disable dream phase."
        )

    def test_db_path_uses_vnx_data_dir_not_state_dir(self):
        """DB_PATH in the pipeline is derived from VNX_DATA_DIR/state, not VNX_STATE_DIR."""
        content = self._read_pipeline()
        # Must have the canonical assignment
        assert 'DB_PATH="$VNX_DATA_DIR/state/quality_intelligence.db"' in content, (
            "DB_PATH in nightly pipeline is not derived from VNX_DATA_DIR/state. "
            "Stale VNX_STATE_DIR from shell profiles would cause path split."
        )

    def test_vnx_state_dir_reanchored_after_profile_load(self):
        """Pipeline re-anchors VNX_STATE_DIR to VNX_DATA_DIR/state after profile load."""
        content = self._read_pipeline()
        assert 'export VNX_STATE_DIR="$VNX_DATA_DIR/state"' in content, (
            "Pipeline does not re-anchor VNX_STATE_DIR after loading shell profile. "
            "Stale profile exports can override the correctly resolved value."
        )
