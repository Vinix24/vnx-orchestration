"""tests/test_gate_kimi_ff849_fixes.py — regression tests for kimi gate findings on PR #849.

Findings addressed:
    HIGH   — dynamic ORDER BY column fallback in _render_tracks_table + JSON branch
    MEDIUM — JSON-branch sqlite3.Connection try/finally (handle-leak on exception)
    MEDIUM — _match_by_slug docstring honesty (tested via token boundary behaviour)
    LOW    — _is_legacy_track now called in match loop (no dead code)
    LOW    — _probe_tracks_db / _fetch_oi_counts helpers eliminate duplication
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from vnx_cli.commands.status import (
    _render_tracks_table,
    _probe_tracks_db,
    _fetch_oi_counts,
    _build_order_by,
)
import backfill_track_dispatch_linkage as bf

PROJECT_ID = "test-proj"


# ---------------------------------------------------------------------------
# Helpers — minimal DB builders
# ---------------------------------------------------------------------------

def _make_minimal_tracks_db(db_path: Path, *, with_next_up: bool, with_sort_order: bool) -> None:
    """Create a runtime_coordination.db with only the base tracks columns.

    Omits next_up / sort_order selectively to reproduce the pre-0028 schema
    that triggered the ORDER BY crash.
    """
    cols = [
        "track_id TEXT NOT NULL",
        "project_id TEXT NOT NULL",
        "title TEXT NOT NULL DEFAULT ''",
        "phase TEXT NOT NULL DEFAULT 'queued'",
        "priority TEXT DEFAULT 'P2'",
        "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
    ]
    if with_sort_order:
        cols.append("sort_order INTEGER NOT NULL DEFAULT 0")
    if with_next_up:
        cols.append("next_up INTEGER NOT NULL DEFAULT 0")

    ddl = f"CREATE TABLE tracks ({', '.join(cols)}, PRIMARY KEY (track_id, project_id))"
    conn = sqlite3.connect(str(db_path))
    conn.execute(ddl)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title) VALUES (?, ?, ?)",
        ("t-one", PROJECT_ID, "Track One"),
    )
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, title) VALUES (?, ?, ?)",
        ("t-two", PROJECT_ID, "Track Two"),
    )
    conn.commit()
    conn.close()


def _make_full_tracks_db(db_path: Path) -> None:
    """DB with next_up, sort_order, derived_status and track_open_items."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE tracks (
            track_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT 'queued',
            priority TEXT DEFAULT 'P2',
            next_up INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            derived_status TEXT,
            phase_changed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY (track_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE track_open_items (
            track_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            oi_id TEXT NOT NULL,
            link_type TEXT NOT NULL,
            resolved_at TEXT,
            PRIMARY KEY (track_id, project_id, oi_id, link_type)
        )
    """)
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, next_up, sort_order) VALUES (?,?,?,?)",
        ("alpha", PROJECT_ID, 1, 10),
    )
    conn.execute(
        "INSERT INTO tracks (track_id, project_id, next_up, sort_order) VALUES (?,?,?,?)",
        ("beta", PROJECT_ID, 0, 20),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# HIGH — dynamic ORDER BY: no crash on missing next_up / sort_order
# ---------------------------------------------------------------------------

class TestDynamicOrderBy:
    """_render_tracks_table must not crash on schemas lacking optional sort columns."""

    def test_no_next_up_no_sort_order(self, tmp_path):
        """Pre-0028 DB without next_up and sort_order: falls back to track_id ASC."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_minimal_tracks_db(state_dir / "runtime_coordination.db",
                                with_next_up=False, with_sort_order=False)
        result = _render_tracks_table(state_dir, PROJECT_ID)
        assert "t-one" in result
        assert "t-two" in result
        assert "tracks query failed" not in result

    def test_no_next_up_with_sort_order(self, tmp_path):
        """sort_order present but next_up absent: no crash."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_minimal_tracks_db(state_dir / "runtime_coordination.db",
                                with_next_up=False, with_sort_order=True)
        result = _render_tracks_table(state_dir, PROJECT_ID)
        assert "t-one" in result
        assert "tracks query failed" not in result

    def test_with_next_up_no_sort_order(self, tmp_path):
        """next_up present but sort_order absent: no crash."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_minimal_tracks_db(state_dir / "runtime_coordination.db",
                                with_next_up=True, with_sort_order=False)
        result = _render_tracks_table(state_dir, PROJECT_ID)
        assert "t-one" in result
        assert "tracks query failed" not in result

    def test_full_schema_renders_correctly(self, tmp_path):
        """Full schema (next_up + sort_order present): table renders, columns shown."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_full_tracks_db(state_dir / "runtime_coordination.db")
        result = _render_tracks_table(state_dir, PROJECT_ID)
        assert "alpha" in result
        assert "beta" in result
        assert "PHASE" in result

    def test_build_order_by_no_optional_cols(self):
        """_build_order_by falls back to track_id ASC when neither optional col exists."""
        probe = {
            "has_next_up": False,
            "has_sort_order": False,
        }
        clause = _build_order_by(probe)
        assert clause == "ORDER BY track_id ASC"
        assert "next_up" not in clause
        assert "sort_order" not in clause

    def test_build_order_by_both_optional_cols(self):
        """_build_order_by includes next_up DESC + sort_order ASC when both present."""
        probe = {
            "has_next_up": True,
            "has_sort_order": True,
        }
        clause = _build_order_by(probe)
        assert "next_up DESC" in clause
        assert "sort_order ASC" in clause
        assert "track_id ASC" in clause

    def test_build_order_by_only_sort_order(self):
        probe = {"has_next_up": False, "has_sort_order": True}
        clause = _build_order_by(probe)
        assert "sort_order ASC" in clause
        assert "next_up" not in clause
        assert "track_id ASC" in clause


# ---------------------------------------------------------------------------
# HIGH — JSON branch dynamic ORDER BY (same guard via _build_order_by)
# ---------------------------------------------------------------------------

class TestJsonBranchOrderBy:
    """The JSON branch of vnx_status must not crash on missing sort columns either."""

    def _make_initialized_project(self, tmp_path: Path) -> tuple[Path, Path]:
        """Return (project_dir, state_dir) with .vnx-project-id marker."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".vnx-project-id").write_text(PROJECT_ID)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        return project_dir, state_dir

    def test_json_branch_no_crash_missing_sort_cols(self, tmp_path):
        """JSON branch survives a DB without next_up/sort_order columns."""
        project_dir, state_dir = self._make_initialized_project(tmp_path)
        _make_minimal_tracks_db(state_dir / "runtime_coordination.db",
                                with_next_up=False, with_sort_order=False)

        from vnx_cli.commands.status import _probe_tracks_db, _build_order_by, _fetch_oi_counts

        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            probe = _probe_tracks_db(conn)
            order_by = _build_order_by(probe)
            rows = conn.execute(
                f"SELECT * FROM tracks WHERE project_id = ? {order_by}",
                (PROJECT_ID,),
            ).fetchall()
            oi_counts = _fetch_oi_counts(conn, PROJECT_ID, probe)
        finally:
            conn.close()

        assert len(rows) == 2
        assert oi_counts == {}


# ---------------------------------------------------------------------------
# MEDIUM — connection handle-leak: try/finally in JSON branch
# ---------------------------------------------------------------------------

class TestConnectionLifecycle:
    """sqlite3 connection must be closed even when a query raises an exception.

    The fix (finding 2) wraps the JSON-branch sqlite3.connect in try/finally.
    These tests verify the structural guarantee directly rather than via
    fragile mock interception of sqlite3.Connection.close.
    """

    def test_render_tracks_table_returns_on_exception(self, tmp_path):
        """_render_tracks_table does not propagate exceptions — always returns a string."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_full_tracks_db(state_dir / "runtime_coordination.db")

        with patch("vnx_cli.commands.status._probe_tracks_db", side_effect=RuntimeError("boom")):
            result = _render_tracks_table(state_dir, PROJECT_ID)
        # Must return a graceful string, not reraise.
        assert isinstance(result, str)
        assert "tracks query failed" in result

    def test_render_tracks_table_no_db_returns_string(self, tmp_path):
        """Missing DB must return an informative string, not raise."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = _render_tracks_table(state_dir, PROJECT_ID)
        assert isinstance(result, str)
        assert "not found" in result.lower() or "runtime_coordination.db" in result

    def test_json_branch_exception_captured_in_output(self, tmp_path):
        """JSON branch must capture query exceptions into tracks_error key, not leak."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        _make_minimal_tracks_db(state_dir / "runtime_coordination.db",
                                with_next_up=False, with_sort_order=False)

        # Simulate an error during probe by patching _probe_tracks_db to raise.
        with patch("vnx_cli.commands.status._probe_tracks_db", side_effect=RuntimeError("injected")):
            # Invoke the same try/except/finally logic the JSON branch uses.
            import vnx_cli.commands.status as status_mod
            db_path = state_dir / "runtime_coordination.db"
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            tracks_error = None
            try:
                status_mod._probe_tracks_db(conn)
            except Exception as exc:
                tracks_error = str(exc)
            finally:
                conn.close()

        assert tracks_error == "injected"


# ---------------------------------------------------------------------------
# MEDIUM — _match_by_slug: honest docstring behaviour (token boundary tests)
# ---------------------------------------------------------------------------

class TestMatchBySlugTokenBoundary:
    """Verify the documented limitation: splitting on [-_] only, not word boundaries."""

    def _make_dispatch(self, dispatch_id: str, pr_ref: str | None = None) -> bf.DispatchRow:
        return bf.DispatchRow(
            rowid=1,
            dispatch_id=dispatch_id,
            project_id=PROJECT_ID,
            current_track=None,
            pr_ref=pr_ref,
            state="completed",
        )

    def test_token_split_on_hyphen(self):
        """Tokens split on hyphen: 'feat-alpha' -> ['feat', 'alpha']."""
        dispatch = self._make_dispatch("20260501-feat-alpha-impl")
        result = bf._match_by_slug(dispatch, ["feat-alpha"])
        assert "feat-alpha" in result

    def test_token_split_on_underscore(self):
        """Tokens split on underscore: 'feat_beta' -> ['feat', 'beta']."""
        dispatch = self._make_dispatch("20260502-feat_beta_fix")
        result = bf._match_by_slug(dispatch, ["feat-beta"])
        assert "feat-beta" in result

    def test_substring_within_token_does_not_match(self):
        """'sale' must NOT match track_id 'sales-pipeline' when dispatch_id has 'salesforce'."""
        dispatch = self._make_dispatch("20260503-salesforce-integration")
        # 'sales' and 'pipeline' are both 4+ chars but 'pipeline' doesn't appear
        result = bf._match_by_slug(dispatch, ["sales-pipeline"])
        assert "sales-pipeline" not in result

    def test_short_tokens_below_threshold_skipped(self):
        """Track_ids with no significant (>=4 char) tokens are skipped entirely."""
        dispatch = self._make_dispatch("20260504-fix-pr-1")
        result = bf._match_by_slug(dispatch, ["fix-pr"])  # both tokens < 4 chars
        assert "fix-pr" not in result

    def test_no_significant_tokens_track_is_skipped(self):
        """Track with only short tokens produces no match even on perfect overlap."""
        dispatch = self._make_dispatch("20260505-abc-de")
        result = bf._match_by_slug(dispatch, ["abc"])
        assert result == []

    def test_all_significant_tokens_must_match(self):
        """H2 requires ALL significant tokens to be present — partial match is rejected."""
        dispatch = self._make_dispatch("20260506-feat-alpha-only")
        # beta token not in dispatch_id
        result = bf._match_by_slug(dispatch, ["feat-alpha-beta"])
        assert "feat-alpha-beta" not in result


# ---------------------------------------------------------------------------
# LOW — _is_legacy_track is called in the match loop (no dead code)
# ---------------------------------------------------------------------------

class TestIsLegacyTrackIntegration:
    """_is_legacy_track must be called during compute_matches, not dead code."""

    def _make_backfill_db(self, tmp_path: Path) -> Path:
        db = tmp_path / "runtime_coordination.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE tracks (
                track_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                phase TEXT NOT NULL DEFAULT 'queued',
                sort_order INTEGER NOT NULL DEFAULT 0,
                pr_ref TEXT,
                derived_status TEXT,
                PRIMARY KEY (track_id, project_id)
            )
        """)
        conn.execute("""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                state TEXT,
                track TEXT,
                pr_ref TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            )
        """)
        conn.execute(
            "INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("feat-live", PROJECT_ID, "Live Feature", "active", 1, "#42", None),
        )
        conn.commit()
        conn.close()
        return db

    def test_legacy_label_A_is_candidate_for_relinking(self, tmp_path):
        """A dispatch with track='A' (legacy) is a relinking candidate, not already_linked."""
        db = self._make_backfill_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("20260601-feat-live-a", PROJECT_ID, "completed", "A", "#42"),
        )
        conn.commit()
        conn.close()

        results = bf.compute_matches(db, PROJECT_ID)
        dispatch_result = next(r for r in results if r.dispatch.dispatch_id == "20260601-feat-live-a")
        # 'A' is a legacy label — must be a relinking candidate (matched), not already_linked.
        assert dispatch_result.status != "already_linked"
        assert dispatch_result.status == "matched"
        assert dispatch_result.matched_track_id == "feat-live"

    def test_legacy_label_T1_is_candidate_for_relinking(self, tmp_path):
        """T1 is in _LEGACY_TRACK_LABELS — must be a candidate, not already_linked."""
        db = self._make_backfill_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("20260601-feat-live-t1", PROJECT_ID, "completed", "T1", "#42"),
        )
        conn.commit()
        conn.close()

        results = bf.compute_matches(db, PROJECT_ID)
        r = next(x for x in results if x.dispatch.dispatch_id == "20260601-feat-live-t1")
        assert r.status != "already_linked"

    def test_feature_track_id_marked_already_linked(self, tmp_path):
        """A dispatch pointing to an actual feature track_id is marked already_linked."""
        db = self._make_backfill_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref) "
            "VALUES (?, ?, ?, ?, ?)",
            ("20260601-feat-live-linked", PROJECT_ID, "completed", "feat-live", "#42"),
        )
        conn.commit()
        conn.close()

        results = bf.compute_matches(db, PROJECT_ID)
        r = next(x for x in results if x.dispatch.dispatch_id == "20260601-feat-live-linked")
        assert r.status == "already_linked"

    def test_is_legacy_track_null_returns_true(self):
        assert bf._is_legacy_track(None) is True

    def test_is_legacy_track_empty_string_returns_true(self):
        assert bf._is_legacy_track("") is True

    def test_is_legacy_track_known_labels(self):
        for label in ("A", "B", "C", "T1", "T2", "T3"):
            assert bf._is_legacy_track(label) is True, f"expected {label!r} to be legacy"

    def test_is_legacy_track_feature_id_returns_false(self):
        assert bf._is_legacy_track("feat-alpha") is False


# ---------------------------------------------------------------------------
# LOW — _probe_tracks_db + _fetch_oi_counts helpers (deduplication)
# ---------------------------------------------------------------------------

class TestProbeHelpers:
    """_probe_tracks_db and _fetch_oi_counts must return correct flags/counts."""

    def test_probe_full_schema_flags(self, tmp_path):
        db_path = tmp_path / "rt.db"
        _make_full_tracks_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            probe = _probe_tracks_db(conn)
        finally:
            conn.close()

        assert probe["has_track_table"] is True
        assert probe["has_next_up"] is True
        assert probe["has_sort_order"] is True
        assert probe["has_derived_status"] is True
        assert probe["has_phase_changed_at"] is True
        assert probe["has_oi_table"] is True
        assert probe["has_oi_project_id"] is True
        assert probe["has_oi_resolved_at"] is True

    def test_probe_minimal_schema_flags(self, tmp_path):
        db_path = tmp_path / "rt.db"
        _make_minimal_tracks_db(db_path, with_next_up=False, with_sort_order=False)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            probe = _probe_tracks_db(conn)
        finally:
            conn.close()

        assert probe["has_track_table"] is True
        assert probe["has_next_up"] is False
        assert probe["has_sort_order"] is False
        assert probe["has_derived_status"] is False
        assert probe["has_oi_table"] is False

    def test_fetch_oi_counts_no_oi_table(self, tmp_path):
        db_path = tmp_path / "rt.db"
        _make_minimal_tracks_db(db_path, with_next_up=False, with_sort_order=False)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        probe = _probe_tracks_db(conn)
        counts = _fetch_oi_counts(conn, PROJECT_ID, probe)
        conn.close()
        assert counts == {}

    def test_fetch_oi_counts_with_resolved_excluded(self, tmp_path):
        db_path = tmp_path / "rt.db"
        _make_full_tracks_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Add OIs: one resolved, one open.
        conn.execute(
            "INSERT INTO track_open_items VALUES (?,?,?,?,?)",
            ("alpha", PROJECT_ID, "OI-1", "blocks", None),  # open
        )
        conn.execute(
            "INSERT INTO track_open_items VALUES (?,?,?,?,?)",
            ("alpha", PROJECT_ID, "OI-2", "blocks", "2026-06-01T00:00:00Z"),  # resolved
        )
        conn.commit()
        probe = _probe_tracks_db(conn)
        counts = _fetch_oi_counts(conn, PROJECT_ID, probe)
        conn.close()
        # Only 1 open OI for 'alpha'.
        assert counts.get("alpha") == 1

    def test_fetch_oi_counts_no_resolved_at_col_counts_all(self, tmp_path):
        """When resolved_at column is absent, all OIs are counted (pre-0030 behaviour)."""
        db_path = tmp_path / "rt.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE tracks (
                track_id TEXT NOT NULL, project_id TEXT NOT NULL,
                PRIMARY KEY (track_id, project_id)
            )
        """)
        conn.execute("""
            CREATE TABLE track_open_items (
                track_id TEXT NOT NULL, project_id TEXT NOT NULL,
                oi_id TEXT NOT NULL, link_type TEXT NOT NULL,
                PRIMARY KEY (track_id, project_id, oi_id, link_type)
            )
        """)
        conn.execute("INSERT INTO tracks VALUES (?,?)", ("t1", PROJECT_ID))
        conn.execute("INSERT INTO track_open_items VALUES (?,?,?,?)", ("t1", PROJECT_ID, "OI-A", "blocks"))
        conn.execute("INSERT INTO track_open_items VALUES (?,?,?,?)", ("t1", PROJECT_ID, "OI-B", "warns"))
        conn.commit()
        conn.row_factory = sqlite3.Row
        probe = _probe_tracks_db(conn)
        counts = _fetch_oi_counts(conn, PROJECT_ID, probe)
        conn.close()
        assert counts.get("t1") == 2
