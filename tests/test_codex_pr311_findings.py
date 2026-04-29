#!/usr/bin/env python3
"""Regression tests for PR #311 Codex blocking findings.

Finding 1: _auto_commit_changes must not stage pre-existing dirty files.
Finding 2: _auto_stash_changes must not stash pre-existing dirty files and must
           include untracked files via --include-untracked.
Finding 3: dispatch_pattern_offered table isolates per-dispatch pattern lookup so
           concurrent dispatches cannot overwrite each other's dispatch_id association.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _porcelain(*paths, prefix="M "):
    """Build a git status --porcelain-style string from a list of paths."""
    return "\n".join(f"{prefix}{p}" for p in paths)


# ---------------------------------------------------------------------------
# Finding 1: _auto_commit_changes isolation
# ---------------------------------------------------------------------------

class TestAutoCommitIsolation:
    """_auto_commit_changes must only stage files introduced by the dispatch."""

    def test_parse_dirty_files_basic(self):
        from subprocess_dispatch import _parse_dirty_files

        output = " M scripts/lib/foo.py\n?? new_file.txt\nR  old.py -> new.py"
        result = _parse_dirty_files(output)
        assert "scripts/lib/foo.py" in result
        assert "new_file.txt" in result
        assert "new.py" in result
        assert "old.py" not in result

    def test_parse_dirty_files_rename_strips_old(self):
        from subprocess_dispatch import _parse_dirty_files

        output = "R  src/old.py -> src/new.py"
        result = _parse_dirty_files(output)
        assert "src/new.py" in result
        assert "src/old.py" not in result

    def test_auto_commit_skips_pre_existing_dirty_files(self, tmp_path):
        """If all dirty files were pre-existing, no commit should be attempted."""
        from subprocess_dispatch import _auto_commit_changes

        pre_existing = frozenset(["scripts/already_dirty.py"])
        porcelain_out = " M scripts/already_dirty.py\n"

        git_calls = []

        def fake_run(cmd, **kwargs):
            git_calls.append(cmd)
            result = MagicMock()
            if cmd[1] == "status":
                result.stdout = porcelain_out
            result.returncode = 0
            return result

        with patch("subprocess_dispatch.subprocess.run", side_effect=fake_run):
            committed = _auto_commit_changes(
                "dispatch-001", "T1", gate="test",
                pre_dispatch_dirty=pre_existing,
            )

        assert committed is False
        # git add must NOT have been called
        add_calls = [c for c in git_calls if len(c) > 1 and c[1] == "add"]
        assert not add_calls, f"git add should not be called when only pre-existing files dirty: {add_calls}"

    def test_auto_commit_only_stages_new_files(self, tmp_path):
        """Only dispatch-new files are staged — pre-existing dirty file excluded."""
        from subprocess_dispatch import _auto_commit_changes

        pre_existing = frozenset(["scripts/pre_existing.py"])
        # Both pre_existing + new dispatch file are dirty now
        porcelain_out = " M scripts/pre_existing.py\n M scripts/new_dispatch.py\n"

        git_calls = []

        def fake_run(cmd, **kwargs):
            git_calls.append(list(cmd))
            result = MagicMock()
            if cmd[1] == "status":
                result.stdout = porcelain_out
            result.returncode = 0
            result.stderr = ""
            return result

        with patch("subprocess_dispatch.subprocess.run", side_effect=fake_run):
            committed = _auto_commit_changes(
                "dispatch-002", "T1", gate="test",
                pre_dispatch_dirty=pre_existing,
            )

        assert committed is True
        add_calls = [c for c in git_calls if len(c) > 1 and c[1] == "add"]
        assert len(add_calls) == 1
        staged = add_calls[0]
        assert "scripts/new_dispatch.py" in staged
        assert "scripts/pre_existing.py" not in staged

    def test_auto_commit_with_empty_pre_dispatch_stages_all(self):
        """When pre_dispatch_dirty is empty, all dirty files are staged."""
        from subprocess_dispatch import _auto_commit_changes

        porcelain_out = " M scripts/a.py\n M scripts/b.py\n"
        git_calls = []

        def fake_run(cmd, **kwargs):
            git_calls.append(list(cmd))
            result = MagicMock()
            if cmd[1] == "status":
                result.stdout = porcelain_out
            result.returncode = 0
            result.stderr = ""
            return result

        with patch("subprocess_dispatch.subprocess.run", side_effect=fake_run):
            committed = _auto_commit_changes("dispatch-003", "T1", pre_dispatch_dirty=frozenset())

        assert committed is True
        add_calls = [c for c in git_calls if len(c) > 1 and c[1] == "add"]
        assert len(add_calls) == 1
        staged = add_calls[0]
        assert "scripts/a.py" in staged
        assert "scripts/b.py" in staged


# ---------------------------------------------------------------------------
# Finding 2: _auto_stash_changes isolation + untracked coverage
# ---------------------------------------------------------------------------

class TestAutoStashIsolation:
    """_auto_stash_changes must isolate to dispatch-new files and use --include-untracked."""

    def test_stash_skips_pre_existing_dirty_files(self):
        """No stash when all dirty files were pre-existing."""
        from subprocess_dispatch import _auto_stash_changes

        pre_existing = frozenset(["scripts/pre.py"])
        porcelain_out = " M scripts/pre.py\n"
        git_calls = []

        def fake_run(cmd, **kwargs):
            git_calls.append(list(cmd))
            result = MagicMock()
            if cmd[1] == "status":
                result.stdout = porcelain_out
            result.returncode = 0
            return result

        with patch("subprocess_dispatch.subprocess.run", side_effect=fake_run):
            stashed = _auto_stash_changes(
                "dispatch-010", "T1", pre_dispatch_dirty=pre_existing
            )

        assert stashed is False
        stash_calls = [c for c in git_calls if len(c) > 1 and c[1] == "stash"]
        assert not stash_calls

    def test_stash_uses_include_untracked_flag(self):
        """git stash command must include --include-untracked."""
        from subprocess_dispatch import _auto_stash_changes

        porcelain_out = "?? scripts/new_untracked.py\n"
        git_calls = []

        def fake_run(cmd, **kwargs):
            git_calls.append(list(cmd))
            result = MagicMock()
            if cmd[1] == "status":
                result.stdout = porcelain_out
            result.returncode = 0
            result.stderr = ""
            return result

        with patch("subprocess_dispatch.subprocess.run", side_effect=fake_run):
            stashed = _auto_stash_changes("dispatch-011", "T1", pre_dispatch_dirty=frozenset())

        assert stashed is True
        stash_calls = [c for c in git_calls if len(c) > 1 and c[1] == "stash"]
        assert len(stash_calls) == 1
        assert "--include-untracked" in stash_calls[0]

    def test_stash_only_paths_from_dispatch(self):
        """Only dispatch-new files appear in the stash -- path argument."""
        from subprocess_dispatch import _auto_stash_changes

        pre_existing = frozenset(["scripts/old.py"])
        porcelain_out = " M scripts/old.py\n?? scripts/dispatch_new.py\n"
        git_calls = []

        def fake_run(cmd, **kwargs):
            git_calls.append(list(cmd))
            result = MagicMock()
            if cmd[1] == "status":
                result.stdout = porcelain_out
            result.returncode = 0
            result.stderr = ""
            return result

        with patch("subprocess_dispatch.subprocess.run", side_effect=fake_run):
            stashed = _auto_stash_changes(
                "dispatch-012", "T1", pre_dispatch_dirty=pre_existing
            )

        assert stashed is True
        stash_calls = [c for c in git_calls if len(c) > 1 and c[1] == "stash"]
        assert len(stash_calls) == 1
        cmd = stash_calls[0]
        assert "scripts/dispatch_new.py" in cmd
        assert "scripts/old.py" not in cmd

    def test_stash_uses_push_subcommand_not_save(self):
        """git stash push is used (not the deprecated git stash save)."""
        from subprocess_dispatch import _auto_stash_changes

        porcelain_out = " M scripts/file.py\n"
        git_calls = []

        def fake_run(cmd, **kwargs):
            git_calls.append(list(cmd))
            result = MagicMock()
            if cmd[1] == "status":
                result.stdout = porcelain_out
            result.returncode = 0
            result.stderr = ""
            return result

        with patch("subprocess_dispatch.subprocess.run", side_effect=fake_run):
            _auto_stash_changes("dispatch-013", "T1", pre_dispatch_dirty=frozenset())

        stash_calls = [c for c in git_calls if len(c) > 1 and c[1] == "stash"]
        assert stash_calls, "stash call expected"
        assert stash_calls[0][2] == "push", (
            f"expected 'git stash push', got '{stash_calls[0][2]}' — 'save' is deprecated and not isolated"
        )


# ---------------------------------------------------------------------------
# Finding 3: dispatch_pattern_offered isolation
# ---------------------------------------------------------------------------

class TestDispatchPatternOfferedIsolation:
    """Pattern offered to multiple dispatches must not overwrite earlier dispatch's association."""

    def _make_db(self, tmp_path: Path) -> Path:
        """Create a minimal quality_intelligence.db with needed tables."""
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY,
                title TEXT,
                confidence_score REAL DEFAULT 0.7,
                usage_count INTEGER DEFAULT 0,
                last_used TEXT,
                source_dispatch_ids TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE pattern_usage (
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
                dispatch_id TEXT DEFAULT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE dispatch_pattern_offered (
                dispatch_id   TEXT NOT NULL,
                pattern_id    TEXT NOT NULL,
                pattern_title TEXT NOT NULL,
                offered_at    TEXT NOT NULL,
                PRIMARY KEY (dispatch_id, pattern_id)
            )
            """
        )
        conn.commit()
        conn.close()
        return db_path

    def test_concurrent_dispatches_do_not_steal_pattern_row(self, tmp_path):
        """If dispatch-A and dispatch-B both offer pattern-X, dispatch-A's row must
        survive in dispatch_pattern_offered even after dispatch-B records its offer."""
        db_path = self._make_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        now = "2026-04-29T00:00:00+00:00"

        # Simulate dispatch-A offering pattern-X
        conn.execute(
            "INSERT INTO dispatch_pattern_offered (dispatch_id, pattern_id, pattern_title, offered_at) "
            "VALUES (?, ?, ?, ?)",
            ("dispatch-A", "pattern-X", "Pattern X", now),
        )
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, last_offered, "
            "confidence, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("pattern-X", "Pattern X", "pattern-X", now, 0.8, now, now),
        )
        conn.commit()

        # Simulate dispatch-B offering the same pattern-X (new dispatch offer)
        conn.execute(
            "INSERT INTO dispatch_pattern_offered (dispatch_id, pattern_id, pattern_title, offered_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(dispatch_id, pattern_id) DO UPDATE SET offered_at = excluded.offered_at",
            ("dispatch-B", "pattern-X", "Pattern X", now),
        )
        conn.execute(
            "UPDATE pattern_usage SET last_offered = ?, updated_at = ? WHERE pattern_id = ?",
            (now, now, "pattern-X"),
        )
        conn.commit()

        # Both dispatch-A and dispatch-B should have their own rows
        rows = conn.execute(
            "SELECT dispatch_id FROM dispatch_pattern_offered WHERE pattern_id = ? ORDER BY dispatch_id",
            ("pattern-X",),
        ).fetchall()
        dispatch_ids = [r["dispatch_id"] for r in rows]
        assert "dispatch-A" in dispatch_ids, "dispatch-A row must survive after dispatch-B offer"
        assert "dispatch-B" in dispatch_ids
        conn.close()

    def test_update_pattern_confidence_uses_junction_table(self, tmp_path):
        """_update_pattern_confidence queries dispatch_pattern_offered when it exists."""
        db_path = self._make_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        now = "2026-04-29T00:00:00+00:00"

        # Insert a success pattern
        conn.execute(
            "INSERT INTO success_patterns (title, confidence_score, usage_count) VALUES (?, ?, ?)",
            ("Pattern Alpha", 0.7, 1),
        )
        # Insert pattern_usage row for pattern-alpha (dispatch_id intentionally wrong/stale)
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, used_count, "
            "success_count, failure_count, last_offered, confidence, created_at, updated_at, dispatch_id) "
            "VALUES (?, ?, ?, 0, 0, 0, ?, 0.7, ?, ?, ?)",
            ("pattern-alpha", "Pattern Alpha", "pattern-alpha", now, now, now, "dispatch-OTHER"),
        )
        # Junction table points to dispatch-A
        conn.execute(
            "INSERT INTO dispatch_pattern_offered (dispatch_id, pattern_id, pattern_title, offered_at) "
            "VALUES (?, ?, ?, ?)",
            ("dispatch-A", "pattern-alpha", "Pattern Alpha", now),
        )
        conn.commit()
        conn.close()

        from subprocess_dispatch import _update_pattern_confidence

        count = _update_pattern_confidence("dispatch-A", "success", db_path)
        assert count == 1, (
            f"_update_pattern_confidence should find pattern via dispatch_pattern_offered, got count={count}"
        )

        # Verify confidence was boosted
        conn2 = sqlite3.connect(str(db_path))
        row = conn2.execute(
            "SELECT confidence_score FROM success_patterns WHERE title = ?", ("Pattern Alpha",)
        ).fetchone()
        conn2.close()
        assert row is not None
        assert row[0] > 0.7, f"confidence should have been boosted: {row[0]}"

    def test_pattern_usage_dispatch_id_not_overwritten_on_conflict(self, tmp_path):
        """ON CONFLICT for pattern_usage must NOT update dispatch_id."""
        import sys
        sys.path.insert(0, str(SCRIPTS_LIB))
        from intelligence_selector import IntelligenceSelector, InjectionResult, IntelligenceItem

        db_path = self._make_db(tmp_path)

        selector = IntelligenceSelector(quality_db_path=db_path)

        from intelligence_selector import SuppressionRecord
        item = IntelligenceItem(
            item_id="pat-1",
            title="My Pattern",
            content="do the thing",
            item_class="proven_pattern",
            confidence=0.8,
            evidence_count=3,
            last_seen="2026-04-01",
            scope_tags=[],
        )
        result_a = InjectionResult(
            injection_point="dispatch_create",
            injected_at="2026-04-29T00:00:00+00:00",
            items=[item],
            suppressed=[],
            task_class="backend",
            dispatch_id="dispatch-AAA",
        )
        result_b = InjectionResult(
            injection_point="dispatch_create",
            injected_at="2026-04-29T00:00:00+00:00",
            items=[item],
            suppressed=[],
            task_class="backend",
            dispatch_id="dispatch-BBB",
        )

        selector._record_pattern_usage(result_a)
        selector._record_pattern_usage(result_b)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # dispatch_pattern_offered must have rows for BOTH dispatches
        rows = conn.execute(
            "SELECT dispatch_id FROM dispatch_pattern_offered WHERE pattern_id = ? ORDER BY dispatch_id",
            ("pat-1",),
        ).fetchall()
        dids = {r["dispatch_id"] for r in rows}
        assert "dispatch-AAA" in dids
        assert "dispatch-BBB" in dids

        # pattern_usage.dispatch_id must NOT be overwritten to dispatch-BBB
        pu = conn.execute(
            "SELECT dispatch_id FROM pattern_usage WHERE pattern_id = ?", ("pat-1",)
        ).fetchone()
        # dispatch_id column should remain NULL (not set by the new code path)
        # OR remain the original value — in either case it must NOT be 'dispatch-BBB'
        # because the ON CONFLICT clause no longer writes dispatch_id
        assert pu["dispatch_id"] != "dispatch-BBB", (
            "pattern_usage.dispatch_id must not be overwritten by a later dispatch's offer"
        )
        conn.close()


# ---------------------------------------------------------------------------
# Source-level guard: 'git add -A' must not appear in _auto_commit_changes
# ---------------------------------------------------------------------------

class TestSourceGuards:
    """Static checks ensuring banned patterns are absent from source."""

    def test_no_git_add_dash_A_in_auto_commit(self):
        src = (SCRIPTS_LIB / "subprocess_dispatch.py").read_text(encoding="utf-8")
        # Locate _auto_commit_changes function body and check for "git", "add", "-A"
        start = src.find("def _auto_commit_changes(")
        end = src.find("\ndef ", start + 1)
        func_body = src[start:end] if end != -1 else src[start:]
        assert '"-A"' not in func_body and "'-A'" not in func_body, (
            "_auto_commit_changes must not use 'git add -A' — it stages unrelated pre-existing files"
        )

    def test_no_git_stash_save_in_auto_stash(self):
        src = (SCRIPTS_LIB / "subprocess_dispatch.py").read_text(encoding="utf-8")
        start = src.find("def _auto_stash_changes(")
        end = src.find("\ndef ", start + 1)
        func_body = src[start:end] if end != -1 else src[start:]
        assert '"save"' not in func_body and "'save'" not in func_body, (
            "_auto_stash_changes must not use 'git stash save' — use 'git stash push'"
        )
        assert '"push"' in func_body or "'push'" in func_body, (
            "_auto_stash_changes must use 'git stash push'"
        )

    def test_dispatch_pattern_offered_not_dispatch_id_in_conflict(self):
        src = (SCRIPTS_LIB / "intelligence_selector.py").read_text(encoding="utf-8")
        # Confirm 'dispatch_id = excluded.dispatch_id' is gone from ON CONFLICT block
        assert "dispatch_id  = excluded.dispatch_id" not in src, (
            "intelligence_selector: ON CONFLICT must not overwrite dispatch_id in pattern_usage"
        )
        assert "dispatch_pattern_offered" in src, (
            "intelligence_selector: must write to dispatch_pattern_offered junction table"
        )
