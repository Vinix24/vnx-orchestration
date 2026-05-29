"""Tests for vnx dream CLI + review_gate (ADR-019 auto-dream PR-2).

Coverage:
- test_dream_status_with_no_cycles: returns empty gracefully
- test_dream_review_approve_applies_consolidation: verify DB state writes on approve
- test_dream_review_reject_archives_only: verify no archive rows on reject
- test_dream_history_shows_recent_cycles: verify ordering DESC by completed_at
- test_review_gate_lists_only_pending: verify pending-only filtering
- TestResolvePathsCanonical: _resolve_paths uses vnx_paths.resolve_state_dir (not hardcoded local path)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "dream"))

import review_gate  # noqa: E402

# ---------------------------------------------------------------------------
# Schema helpers
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
CREATE TABLE IF NOT EXISTS dream_pattern_archives (
    archive_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id                TEXT    NOT NULL,
    project_id              TEXT    NOT NULL DEFAULT 'vnx-dev',
    original_pattern_id     INTEGER NOT NULL,
    original_table          TEXT    NOT NULL,
    archived_reason         TEXT    NOT NULL,
    archived_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

_CONSOLIDATION = {
    "merged": [],
    "dropped": [{"id": 1, "table": "antipatterns", "reason": "stale_30d"}],
    "archived": [{"id": 2, "table": "success_patterns", "reason": "merged_into_other"}],
    "flagged": [],
    "summary": "test",
}


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DREAM_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def _write_pending_review(tmp_path: Path, cycle_id: str, project_id: str) -> Path:
    state_dir = tmp_path / ".vnx-data" / "state" / "dream"
    state_dir.mkdir(parents=True, exist_ok=True)
    review_path = state_dir / f"{cycle_id}-pending-review.json"
    review_path.write_text(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "project_id": project_id,
                "input_count": 10,
                "consolidation": _CONSOLIDATION,
                "requires_operator_review": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return review_path


def _insert_cycle(db_path: Path, cycle_id: str, project_id: str, completed_at: str,
                  status: str = "completed") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dream_cycles (cycle_id, project_id, completed_at, status) VALUES (?,?,?,?)",
        (cycle_id, project_id, completed_at, status),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDreamStatusNoCycles:
    def test_dream_status_with_no_cycles(self, tmp_path):
        """list_pending_reviews returns empty list when state dir absent."""
        result = review_gate.list_pending_reviews("vnx-dev", tmp_path / ".vnx-data")
        assert result == []

    def test_dream_status_empty_state_dir(self, tmp_path):
        """list_pending_reviews returns empty when state dir exists but has no files."""
        state_dir = tmp_path / ".vnx-data" / "state" / "dream"
        state_dir.mkdir(parents=True)
        result = review_gate.list_pending_reviews("vnx-dev", tmp_path / ".vnx-data")
        assert result == []


class TestDreamReviewApprove:
    def test_approve_applies_consolidation(self, tmp_path):
        """approve_cycle inserts archive rows + sets status=reviewed in dream_cycles."""
        db_path = _make_db(tmp_path)
        cycle_id = "dream-20260529-120000-abcd1234"
        _insert_cycle(db_path, cycle_id, "vnx-dev", "2026-05-29T12:00:00+00:00")
        _write_pending_review(tmp_path, cycle_id, "vnx-dev")

        with patch("review_gate.resolve_project_root", return_value=tmp_path):
            review_gate.approve_cycle(
                cycle_id, "vnx-dev", db_path, data_root=tmp_path / ".vnx-data"
            )

        conn = sqlite3.connect(str(db_path))
        archive_rows = conn.execute(
            "SELECT archived_reason FROM dream_pattern_archives WHERE cycle_id=?",
            (cycle_id,),
        ).fetchall()
        cycle_row = conn.execute(
            "SELECT status, operator_reviewed FROM dream_cycles WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
        conn.close()

        assert len(archive_rows) == 2
        reasons = {r[0] for r in archive_rows}
        assert "stale_30d" in reasons
        assert "merged_into_other" in reasons
        assert cycle_row[0] == "reviewed"
        assert cycle_row[1] == 1

    def test_approve_emits_ndjson_event(self, tmp_path):
        """approve_cycle emits dream_cycle_approved event (ADR-005)."""
        db_path = _make_db(tmp_path)
        cycle_id = "dream-20260529-130000-ef012345"
        _insert_cycle(db_path, cycle_id, "vnx-dev", "2026-05-29T13:00:00+00:00")
        _write_pending_review(tmp_path, cycle_id, "vnx-dev")

        with patch("review_gate.resolve_project_root", return_value=tmp_path):
            review_gate.approve_cycle(
                cycle_id, "vnx-dev", db_path, data_root=tmp_path / ".vnx-data"
            )

        events = list((tmp_path / ".vnx-data" / "events" / "dream").glob("*.ndjson"))
        assert len(events) == 1
        lines = [json.loads(l) for l in events[0].read_text().strip().splitlines()]
        assert any(e["event_type"] == "dream_cycle_approved" for e in lines)


class TestDreamReviewReject:
    def test_reject_archives_only_no_db_rows(self, tmp_path):
        """reject_cycle sets status=rejected without writing archive rows."""
        db_path = _make_db(tmp_path)
        cycle_id = "dream-20260529-140000-gh678901"
        _insert_cycle(db_path, cycle_id, "vnx-dev", "2026-05-29T14:00:00+00:00")
        _write_pending_review(tmp_path, cycle_id, "vnx-dev")

        with patch("review_gate.resolve_project_root", return_value=tmp_path):
            review_gate.reject_cycle(
                cycle_id, "vnx-dev", "test rejection", db_path,
                data_root=tmp_path / ".vnx-data",
            )

        conn = sqlite3.connect(str(db_path))
        archive_count = conn.execute(
            "SELECT COUNT(*) FROM dream_pattern_archives WHERE cycle_id=?", (cycle_id,)
        ).fetchone()[0]
        cycle_row = conn.execute(
            "SELECT status, operator_reviewed FROM dream_cycles WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
        conn.close()

        assert archive_count == 0
        assert cycle_row[0] == "rejected"
        assert cycle_row[1] == 1

    def test_reject_stores_reason_in_review_file(self, tmp_path):
        """reject_cycle writes rejected_reason into the review JSON."""
        db_path = _make_db(tmp_path)
        cycle_id = "dream-20260529-150000-ij234567"
        _insert_cycle(db_path, cycle_id, "vnx-dev", "2026-05-29T15:00:00+00:00")
        review_path = _write_pending_review(tmp_path, cycle_id, "vnx-dev")

        with patch("review_gate.resolve_project_root", return_value=tmp_path):
            review_gate.reject_cycle(
                cycle_id, "vnx-dev", "low confidence", db_path,
                data_root=tmp_path / ".vnx-data",
            )

        review = json.loads(review_path.read_text())
        assert review["rejected_reason"] == "low confidence"
        assert review["requires_operator_review"] is False


class TestDreamHistory:
    def test_history_shows_recent_cycles(self, tmp_path):
        """Dream cycles are ordered DESC by completed_at."""
        db_path = _make_db(tmp_path)
        dates = ["2026-05-27T10:00:00+00:00", "2026-05-28T10:00:00+00:00",
                 "2026-05-29T10:00:00+00:00"]
        for i, dt in enumerate(dates):
            _insert_cycle(db_path, f"dream-cycle-{i:03d}", "vnx-dev", dt)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT cycle_id FROM dream_cycles WHERE project_id='vnx-dev'"
            " ORDER BY completed_at DESC LIMIT 10"
        ).fetchall()
        conn.close()

        ids = [r[0] for r in rows]
        assert ids[0] == "dream-cycle-002"
        assert ids[-1] == "dream-cycle-000"


class TestCycleIdValidation:
    """Finding 1 — cycle_id path-traversal guard."""

    def test_dotdot_segment_is_rejected(self):
        """`../escape` must raise ValueError."""
        with pytest.raises(ValueError, match="Invalid cycle_id"):
            review_gate._validate_cycle_id("../escape")

    def test_slash_in_id_is_rejected(self):
        """`a/b` must raise ValueError."""
        with pytest.raises(ValueError, match="Invalid cycle_id"):
            review_gate._validate_cycle_id("a/b")

    def test_normal_cycle_id_passes(self):
        """Typical cycle_id format must not raise."""
        review_gate._validate_cycle_id("dream-20260529-120000-abcd1234")

    def test_load_review_rejects_path_traversal(self, tmp_path):
        """`_load_review` raises ValueError before constructing any path."""
        state_dir = tmp_path / "state" / "dream"
        state_dir.mkdir(parents=True)
        with pytest.raises(ValueError, match="Invalid cycle_id"):
            review_gate._load_review(state_dir, "../escape", "vnx-dev")

    def test_approve_cycle_rejects_path_traversal(self, tmp_path):
        """`approve_cycle` raises ValueError on unsafe cycle_id."""
        db_path = _make_db(tmp_path)
        with pytest.raises(ValueError, match="Invalid cycle_id"):
            review_gate.approve_cycle(
                "../escape", "vnx-dev", db_path, data_root=tmp_path / ".vnx-data"
            )

    def test_reject_cycle_rejects_path_traversal(self, tmp_path):
        """`reject_cycle` raises ValueError on unsafe cycle_id."""
        db_path = _make_db(tmp_path)
        with pytest.raises(ValueError, match="Invalid cycle_id"):
            review_gate.reject_cycle(
                "../escape", "vnx-dev", "reason", db_path, data_root=tmp_path / ".vnx-data"
            )


def _make_db_no_archives(tmp_path: Path) -> Path:
    """DB with dream_cycles only — archive INSERT will raise OperationalError."""
    db_path = tmp_path / "quality_intelligence_no_arch.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS dream_cycles (
        cycle_id          TEXT    NOT NULL,
        project_id        TEXT    NOT NULL DEFAULT 'vnx-dev',
        started_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        completed_at      TEXT,
        status            TEXT    NOT NULL DEFAULT 'pending',
        operator_reviewed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (cycle_id, project_id)
    );
    """)
    conn.commit()
    conn.close()
    return db_path


class TestApproveArchiveFailure:
    """Finding 2 — archive insert failure must not silently mark cycle reviewed."""

    def test_approve_archive_failure_does_not_mark_reviewed(self, tmp_path):
        """If archive INSERT fails, cycle must NOT be left as 'reviewed'."""
        db_path = _make_db_no_archives(tmp_path)
        cycle_id = "dream-20260529-160000-ab123456"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO dream_cycles (cycle_id, project_id, completed_at, status)"
            " VALUES (?,?,?,?)",
            (cycle_id, "vnx-dev", "2026-05-29T16:00:00+00:00", "completed"),
        )
        conn.commit()
        conn.close()
        _write_pending_review(tmp_path, cycle_id, "vnx-dev")

        with patch("review_gate.resolve_project_root", return_value=tmp_path):
            with pytest.raises(sqlite3.OperationalError):
                review_gate.approve_cycle(
                    cycle_id, "vnx-dev", db_path, data_root=tmp_path / ".vnx-data"
                )

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT status, operator_reviewed FROM dream_cycles WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
        conn.close()

        assert row[0] != "reviewed", "cycle must not be marked reviewed when archive insert fails"
        assert row[1] == 0, "operator_reviewed must remain 0 on archive failure"


class TestReviewGateListsOnlyPending:
    def test_lists_only_pending(self, tmp_path):
        """list_pending_reviews filters out non-pending and wrong project_id files."""
        # pending for correct project
        _write_pending_review(tmp_path, "cycle-001", "vnx-dev")

        # non-pending (requires_operator_review=False) for correct project
        state_dir = tmp_path / ".vnx-data" / "state" / "dream"
        non_pending = state_dir / "cycle-002-pending-review.json"
        non_pending.write_text(
            json.dumps({"cycle_id": "cycle-002", "project_id": "vnx-dev",
                        "requires_operator_review": False}),
            encoding="utf-8",
        )

        # pending for different project
        other_project = state_dir / "cycle-003-pending-review.json"
        other_project.write_text(
            json.dumps({"cycle_id": "cycle-003", "project_id": "other-project",
                        "requires_operator_review": True}),
            encoding="utf-8",
        )

        result = review_gate.list_pending_reviews("vnx-dev", tmp_path / ".vnx-data")

        assert len(result) == 1
        assert result[0]["cycle_id"] == "cycle-001"


class TestResolvePathsCanonical:
    """_resolve_paths must use vnx_paths.resolve_state_dir, not the hardcoded local path.

    Regression guard: before the fix, _resolve_paths returned
    resolve_project_root() / '.vnx-data' / 'state' / 'quality_intelligence.db'
    (the local worktree DB, schema v19, 951 patterns — causing the kimi hang).
    After the fix it must use vnx_paths.resolve_state_dir() so the canonical
    central DB is targeted (ADR-007: path is project_id-scoped).
    """

    def test_returns_canonical_db_path(self, tmp_path):
        """db_path comes from resolve_state_dir(), not project_root/.vnx-data/state/."""
        # Simulate: central state dir (e.g. ~/.vnx-data/vnx-dev/state)
        # is different from the local project root's .vnx-data/state.
        central_state = tmp_path / "central" / "vnx-dev" / "state"
        local_root = tmp_path / "local-project"
        local_root.mkdir(parents=True, exist_ok=True)

        # Ensure vnx_cli is importable
        repo_root = Path(__file__).resolve().parents[1]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))

        import vnx_paths
        import project_root as pr_mod
        import vnx_cli.commands.dream as dream_mod

        with patch.object(vnx_paths, "resolve_state_dir", return_value=central_state), \
             patch.object(pr_mod, "resolve_project_root", return_value=local_root):
            _, db_path = dream_mod._resolve_paths()

        assert db_path == central_state / "quality_intelligence.db", (
            f"Expected canonical central path, got {db_path}"
        )
        # Must NOT be the old hardcoded local path
        assert db_path != local_root / ".vnx-data" / "state" / "quality_intelligence.db", (
            "db_path must not be the hardcoded local worktree path"
        )
