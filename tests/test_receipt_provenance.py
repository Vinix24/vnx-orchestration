#!/usr/bin/env python3
"""Tests for receipt provenance enrichment and bidirectional linkage (FP-D PR-2).

Covers:
  - Receipt enrichment with provenance fields
  - Provenance validation and gap detection
  - Bidirectional mapping helpers (dispatch <-> receipt <-> commit)
  - Provenance registry operations
  - Backward compatibility with cmd_id-based receipts
  - Mixed execution path provenance preservation
  - Operator-readable provenance summaries
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import receipt_provenance
from receipt_provenance import (
    CHAIN_STATUS_BROKEN,
    CHAIN_STATUS_COMPLETE,
    CHAIN_STATUS_INCOMPLETE,
    GAP_CMD_ID_FALLBACK,
    GAP_MISSING_DISPATCH_ID,
    GAP_MISSING_GIT_REF,
    ProvenanceGap,
    ProvenanceLink,
    ProvenanceValidation,
    batch_provenance_summary,
    emit_provenance_gap_event,
    enrich_receipt_provenance,
    find_dispatch_by_receipt,
    find_receipt_by_commit,
    find_receipts_by_dispatch,
    get_provenance_link,
    provenance_summary_for_dispatch,
    reconcile_commit_provenance,
    register_provenance_link,
    validate_receipt_provenance,
)
from runtime_coordination import get_connection, init_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state_dir(tmp_path):
    """Create a temporary state directory with schema initialized."""
    sd = tmp_path / "state"
    sd.mkdir()
    init_schema(sd)
    return sd


@pytest.fixture
def conn(state_dir):
    """Database connection with schema initialized (including v6 migration)."""
    with get_connection(state_dir) as c:
        yield c


@pytest.fixture
def receipts_path(tmp_path):
    """Path for temporary receipts NDJSON file."""
    return tmp_path / "t0_receipts.ndjson"


def _make_receipt(
    dispatch_id="20260329-180606-test-task-B",
    event_type="task_complete",
    terminal="T2",
    status="success",
    git_ref="abc123def456",
    branch="feature/test",
    **overrides,
):
    """Build a test receipt with sensible defaults."""
    receipt = {
        "timestamp": "2026-03-29T18:30:00Z",
        "event_type": event_type,
        "event": event_type,
        "dispatch_id": dispatch_id,
        "task_id": f"TASK-{dispatch_id[:8]}",
        "terminal": terminal,
        "track": "B",
        "status": status,
        "run_id": f"run-{dispatch_id[:8]}",
        "summary": "Test task completed",
        "provenance": {
            "git_ref": git_ref,
            "branch": branch,
            "is_dirty": False,
            "dirty_files": 0,
            "captured_at": "2026-03-29T18:30:00Z",
            "captured_by": "test",
        },
        "session": {
            "session_id": "test-session",
            "terminal": terminal,
            "model": "claude-sonnet-4.5",
            "provider": "claude_code",
        },
    }
    receipt.update(overrides)
    return receipt


def _write_receipts(receipts_path, receipts):
    """Write receipts to NDJSON file."""
    with receipts_path.open("w", encoding="utf-8") as fh:
        for r in receipts:
            fh.write(json.dumps(r) + "\n")


# ============================================================================
# Receipt enrichment tests
# ============================================================================

class TestEnrichReceiptProvenance:

    def test_enriches_dispatch_id_and_trace_token(self):
        receipt = _make_receipt()
        result = enrich_receipt_provenance(receipt)

        assert result["dispatch_id"] == "20260329-180606-test-task-B"
        assert result["trace_token"] == "Dispatch-ID: 20260329-180606-test-task-B"
        assert result is receipt  # modified in place

    def test_populates_cmd_id_for_backward_compat(self):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["cmd_id"] = "20260329-180606-test-task-B"

        result = enrich_receipt_provenance(receipt)

        assert result["dispatch_id"] == "20260329-180606-test-task-B"
        assert result["cmd_id"] == "20260329-180606-test-task-B"

    def test_preserves_existing_dispatch_id(self):
        receipt = _make_receipt(dispatch_id="custom-dispatch-id")
        result = enrich_receipt_provenance(receipt)

        assert result["dispatch_id"] == "custom-dispatch-id"

    def test_sets_cmd_id_when_missing(self):
        receipt = _make_receipt()
        assert "cmd_id" not in receipt
        enrich_receipt_provenance(receipt)
        assert receipt["cmd_id"] == receipt["dispatch_id"]

    def test_does_not_overwrite_existing_cmd_id(self):
        receipt = _make_receipt()
        receipt["cmd_id"] = "legacy-cmd-id"
        enrich_receipt_provenance(receipt)
        assert receipt["cmd_id"] == "legacy-cmd-id"

    def test_does_not_overwrite_existing_trace_token(self):
        receipt = _make_receipt()
        receipt["trace_token"] = "custom-token"
        enrich_receipt_provenance(receipt)
        assert receipt["trace_token"] == "custom-token"

    def test_sets_pr_number_to_none_when_missing(self):
        receipt = _make_receipt()
        enrich_receipt_provenance(receipt)
        assert receipt["pr_number"] is None

    def test_preserves_existing_pr_number(self):
        receipt = _make_receipt(pr_number=42)
        enrich_receipt_provenance(receipt)
        assert receipt["pr_number"] == 42

    def test_extracts_feature_plan_pr_from_summary(self):
        receipt = _make_receipt(summary="PR-2 receipt provenance enrichment")
        enrich_receipt_provenance(receipt)
        assert receipt["feature_plan_pr"] == "PR-2"

    def test_extracts_feature_plan_pr_from_metadata(self):
        receipt = _make_receipt(metadata={"feature_plan_pr": "PR-3"})
        enrich_receipt_provenance(receipt)
        assert receipt["feature_plan_pr"] == "PR-3"

    def test_resolves_dispatch_id_from_env(self, monkeypatch):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        monkeypatch.setenv("VNX_CURRENT_DISPATCH_ID", "env-dispatch-123-B")

        enrich_receipt_provenance(receipt)
        assert receipt["dispatch_id"] == "env-dispatch-123-B"

    def test_resolves_dispatch_id_from_metadata(self):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["metadata"] = {"dispatch_id": "meta-dispatch-456-B"}

        enrich_receipt_provenance(receipt)
        assert receipt["dispatch_id"] == "meta-dispatch-456-B"

    def test_handles_receipt_with_no_dispatch_id(self):
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "heartbeat",
            "event": "heartbeat",
            "terminal": "T1",
        }
        result = enrich_receipt_provenance(receipt)
        assert "dispatch_id" not in result or result.get("dispatch_id") is None


# ============================================================================
# Provenance validation tests
# ============================================================================

class TestValidateReceiptProvenance:

    def test_valid_receipt_with_all_fields(self):
        receipt = _make_receipt(
            trace_token="Dispatch-ID: 20260329-180606-test-task-B",
            feature_plan_pr="PR-2",
        )
        validation = validate_receipt_provenance(receipt)

        assert validation.valid is True
        assert validation.dispatch_id == "20260329-180606-test-task-B"
        assert validation.git_ref == "abc123def456"
        assert validation.chain_status == CHAIN_STATUS_COMPLETE

    def test_incomplete_chain_missing_trace_token(self):
        receipt = _make_receipt()
        validation = validate_receipt_provenance(receipt)

        assert validation.valid is True
        assert validation.chain_status == CHAIN_STATUS_INCOMPLETE

    def test_detects_missing_dispatch_id(self):
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "heartbeat",
            "event": "heartbeat",
            "terminal": "T1",
        }
        validation = validate_receipt_provenance(receipt)

        gap_types = [g.gap_type for g in validation.gaps]
        assert GAP_MISSING_DISPATCH_ID in gap_types

    def test_detects_cmd_id_fallback(self):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["cmd_id"] = "20260329-180606-test-task-B"

        validation = validate_receipt_provenance(receipt)

        gap_types = [g.gap_type for g in validation.gaps]
        assert GAP_CMD_ID_FALLBACK in gap_types
        assert validation.dispatch_id == "20260329-180606-test-task-B"

    def test_detects_missing_git_ref(self):
        receipt = _make_receipt(git_ref="unknown")
        validation = validate_receipt_provenance(receipt)

        gap_types = [g.gap_type for g in validation.gaps]
        assert GAP_MISSING_GIT_REF in gap_types

    def test_detects_missing_provenance_block(self):
        receipt = _make_receipt()
        del receipt["provenance"]
        validation = validate_receipt_provenance(receipt)

        gap_types = [g.gap_type for g in validation.gaps]
        assert GAP_MISSING_GIT_REF in gap_types

    def test_broken_chain_no_dispatch_no_git(self):
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "heartbeat",
            "event": "heartbeat",
            "terminal": "T1",
        }
        validation = validate_receipt_provenance(receipt)
        assert validation.chain_status == CHAIN_STATUS_BROKEN

    def test_validation_to_dict(self):
        receipt = _make_receipt()
        validation = validate_receipt_provenance(receipt)
        d = validation.to_dict()

        assert "valid" in d
        assert "dispatch_id" in d
        assert "chain_status" in d
        assert "gaps" in d
        assert isinstance(d["gaps"], list)


# ============================================================================
# Provenance registry tests
# ============================================================================

class TestProvenanceRegistry:

    def test_register_new_link(self, conn):
        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-test-B",
            receipt_id="run-001",
            commit_sha="abc123",
        )

        assert link.dispatch_id == "20260329-180606-test-B"
        assert link.receipt_id == "run-001"
        assert link.commit_sha == "abc123"
        assert link.chain_status == CHAIN_STATUS_COMPLETE
        conn.commit()

    def test_merge_updates_existing_link(self, conn):
        # First registration: receipt only
        register_provenance_link(
            conn,
            dispatch_id="20260329-180606-merge-B",
            receipt_id="run-002",
        )
        conn.commit()

        # Second registration: add commit
        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-merge-B",
            commit_sha="def456",
        )
        conn.commit()

        assert link.receipt_id == "run-002"  # preserved from first
        assert link.commit_sha == "def456"  # added from second
        assert link.chain_status == CHAIN_STATUS_COMPLETE

    def test_does_not_overwrite_existing_fields(self, conn):
        register_provenance_link(
            conn,
            dispatch_id="20260329-180606-noover-B",
            receipt_id="run-original",
        )
        conn.commit()

        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-noover-B",
            receipt_id="run-attempted-overwrite",
        )
        conn.commit()

        assert link.receipt_id == "run-original"

    def test_get_provenance_link(self, conn):
        register_provenance_link(
            conn,
            dispatch_id="20260329-180606-get-B",
            receipt_id="run-003",
            trace_token="Dispatch-ID: 20260329-180606-get-B",
        )
        conn.commit()

        link = get_provenance_link(conn, "20260329-180606-get-B")
        assert link is not None
        assert link.receipt_id == "run-003"
        assert link.trace_token == "Dispatch-ID: 20260329-180606-get-B"

    def test_get_nonexistent_link_returns_none(self, conn):
        link = get_provenance_link(conn, "nonexistent-dispatch")
        assert link is None

    def test_incomplete_chain_status(self, conn):
        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-incomplete-B",
            receipt_id="run-004",
            # no commit_sha
        )
        conn.commit()

        assert link.chain_status == CHAIN_STATUS_INCOMPLETE

    def test_registration_emits_coordination_event(self, conn):
        register_provenance_link(
            conn,
            dispatch_id="20260329-180606-event-B",
            receipt_id="run-005",
        )
        conn.commit()

        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'provenance_registered'"
        ).fetchall()
        assert len(events) >= 1
        assert events[0]["entity_id"] == "20260329-180606-event-B"

    def test_link_to_dict(self, conn):
        link = register_provenance_link(
            conn,
            dispatch_id="20260329-180606-dict-B",
            receipt_id="run-006",
            pr_number=42,
            feature_plan_pr="PR-2",
        )
        conn.commit()

        d = link.to_dict()
        assert d["dispatch_id"] == "20260329-180606-dict-B"
        assert d["pr_number"] == 42
        assert d["feature_plan_pr"] == "PR-2"


# ============================================================================
# Bidirectional mapping helper tests
# ============================================================================

class TestBidirectionalMapping:

    def test_find_receipts_by_dispatch(self, receipts_path):
        receipts = [
            _make_receipt(dispatch_id="DISP-001"),
            _make_receipt(dispatch_id="DISP-002"),
            _make_receipt(dispatch_id="DISP-001", event_type="task_started"),
        ]
        _write_receipts(receipts_path, receipts)

        matches = find_receipts_by_dispatch(receipts_path, "DISP-001")
        assert len(matches) == 2

    def test_find_receipts_by_dispatch_with_cmd_id_fallback(self, receipts_path):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["cmd_id"] = "LEGACY-001"
        _write_receipts(receipts_path, [receipt])

        matches = find_receipts_by_dispatch(receipts_path, "LEGACY-001")
        assert len(matches) == 1

    def test_find_receipts_empty_file(self, receipts_path):
        matches = find_receipts_by_dispatch(receipts_path, "DISP-999")
        assert matches == []

    def test_find_dispatch_by_receipt(self):
        receipt = _make_receipt(dispatch_id="DISP-ABC")
        assert find_dispatch_by_receipt(receipt) == "DISP-ABC"

    def test_find_dispatch_by_receipt_cmd_id_fallback(self):
        receipt = _make_receipt()
        del receipt["dispatch_id"]
        receipt["cmd_id"] = "CMD-FALLBACK"
        assert find_dispatch_by_receipt(receipt) == "CMD-FALLBACK"

    def test_find_receipt_by_commit(self, receipts_path):
        receipts = [
            _make_receipt(dispatch_id="DISP-A", git_ref="sha-111"),
            _make_receipt(dispatch_id="DISP-B", git_ref="sha-222"),
        ]
        _write_receipts(receipts_path, receipts)

        match = find_receipt_by_commit(receipts_path, "sha-222")
        assert match is not None
        assert match["dispatch_id"] == "DISP-B"

    def test_find_receipt_by_commit_not_found(self, receipts_path):
        _write_receipts(receipts_path, [_make_receipt()])
        assert find_receipt_by_commit(receipts_path, "nonexistent-sha") is None


# ============================================================================
# Provenance gap event tests
# ============================================================================

class TestProvenanceGapEvents:

    def test_emit_gap_event(self, conn):
        gap = ProvenanceGap(
            gap_type=GAP_MISSING_DISPATCH_ID,
            severity="warning",
            entity_type="receipt",
            entity_id="run-001",
            description="Receipt has no dispatch_id",
        )

        event_id = emit_provenance_gap_event(conn, gap)
        conn.commit()

        assert event_id is not None
        events = conn.execute(
            "SELECT * FROM coordination_events WHERE event_type = 'provenance_gap'"
        ).fetchall()
        assert len(events) == 1
        assert events[0]["entity_id"] == "run-001"

    def test_gap_to_dict(self):
        gap = ProvenanceGap(
            gap_type=GAP_MISSING_GIT_REF,
            severity="warning",
            entity_type="receipt",
            entity_id="run-002",
            description="No git_ref",
        )
        d = gap.to_dict()
        assert d["gap_type"] == GAP_MISSING_GIT_REF
        assert d["severity"] == "warning"


# ============================================================================
# Operator-readable provenance summary tests
# ============================================================================

class TestProvenanceSummary:

    def test_summary_with_receipts(self, receipts_path):
        receipts = [
            _make_receipt(
                dispatch_id="DISP-SUM",
                trace_token="Dispatch-ID: DISP-SUM",
                feature_plan_pr="PR-2",
            ),
        ]
        _write_receipts(receipts_path, receipts)

        summary = provenance_summary_for_dispatch("DISP-SUM", receipts_path)
        assert summary["dispatch_id"] == "DISP-SUM"
        assert summary["receipt_count"] == 1
        assert len(summary["receipts"]) == 1
        assert summary["receipts"][0]["trace_token"] == "Dispatch-ID: DISP-SUM"

    def test_summary_no_receipts(self, receipts_path):
        _write_receipts(receipts_path, [])

        summary = provenance_summary_for_dispatch("DISP-NONE", receipts_path)
        assert summary["receipt_count"] == 0
        assert summary["chain_status"] == CHAIN_STATUS_INCOMPLETE
        gap_types = [g["gap_type"] for g in summary["gaps"]]
        assert "missing_receipt" in gap_types

    def test_summary_with_registry(self, receipts_path, conn):
        register_provenance_link(
            conn,
            dispatch_id="DISP-REG",
            receipt_id="run-reg",
            commit_sha="sha-reg",
        )
        conn.commit()
        _write_receipts(receipts_path, [_make_receipt(dispatch_id="DISP-REG")])

        summary = provenance_summary_for_dispatch("DISP-REG", receipts_path, conn)
        assert summary["registry"] is not None
        assert summary["registry"]["commit_sha"] == "sha-reg"
        assert summary["chain_status"] == CHAIN_STATUS_COMPLETE

    def test_batch_summary(self, receipts_path):
        receipts = [
            _make_receipt(dispatch_id="DISP-B1"),
            _make_receipt(dispatch_id="DISP-B2",
                          trace_token="Dispatch-ID: DISP-B2",
                          feature_plan_pr="PR-2"),
        ]
        _write_receipts(receipts_path, receipts)

        batch = batch_provenance_summary(
            ["DISP-B1", "DISP-B2", "DISP-MISSING"],
            receipts_path,
        )
        assert batch["total_dispatches"] == 3
        assert batch["chain_status_counts"]["incomplete"] >= 1


# ============================================================================
# Mixed execution path tests
# ============================================================================

class TestMixedExecutionPaths:

    def test_headless_receipt_preserves_provenance(self):
        """Receipts from headless execution paths preserve provenance."""
        receipt = _make_receipt(
            terminal="headless_claude_cli",
            dispatch_id="20260329-180606-headless-B",
        )
        receipt["session"]["provider"] = "claude_code"

        enrich_receipt_provenance(receipt)
        validation = validate_receipt_provenance(receipt)

        assert validation.dispatch_id == "20260329-180606-headless-B"
        assert validation.valid is True

    def test_codex_receipt_preserves_provenance(self):
        """Receipts from codex CLI preserve provenance."""
        receipt = _make_receipt(
            terminal="T1",
            dispatch_id="20260329-180606-codex-A",
        )
        receipt["session"]["provider"] = "codex_cli"

        enrich_receipt_provenance(receipt)
        validation = validate_receipt_provenance(receipt)

        assert validation.dispatch_id == "20260329-180606-codex-A"
        assert validation.valid is True

    def test_mixed_provider_receipts_link_same_dispatch(self, receipts_path):
        """Receipts from different providers for the same dispatch are found."""
        receipts = [
            _make_receipt(dispatch_id="DISP-MIXED", terminal="T1"),
            _make_receipt(dispatch_id="DISP-MIXED", terminal="headless_codex_cli",
                          event_type="task_started"),
        ]
        receipts[0]["session"]["provider"] = "claude_code"
        receipts[1]["session"]["provider"] = "codex_cli"
        _write_receipts(receipts_path, receipts)

        matches = find_receipts_by_dispatch(receipts_path, "DISP-MIXED")
        assert len(matches) == 2
        providers = {m["session"]["provider"] for m in matches}
        assert providers == {"claude_code", "codex_cli"}


# ============================================================================
# Backward compatibility tests
# ============================================================================

class TestBackwardCompatibility:

    def test_legacy_receipt_with_cmd_id_only(self):
        """Pre-PR-2 receipts with only cmd_id are enriched correctly."""
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "task_complete",
            "event": "task_complete",
            "cmd_id": "LEGACY-CMD-001",
            "task_id": "TASK-001",
            "terminal": "T1",
            "status": "success",
            "provenance": {
                "git_ref": "abc123",
                "branch": "main",
                "is_dirty": False,
                "dirty_files": 0,
            },
        }
        enrich_receipt_provenance(receipt)

        assert receipt["dispatch_id"] == "LEGACY-CMD-001"
        assert receipt["cmd_id"] == "LEGACY-CMD-001"
        assert receipt["trace_token"] == "Dispatch-ID: LEGACY-CMD-001"

    def test_legacy_receipt_validation(self):
        """Legacy receipts with cmd_id produce info-level gap, not error."""
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "task_complete",
            "cmd_id": "LEGACY-CMD-002",
            "terminal": "T1",
            "provenance": {"git_ref": "def456", "branch": "main"},
        }
        validation = validate_receipt_provenance(receipt)

        assert validation.valid is True
        assert validation.dispatch_id == "LEGACY-CMD-002"
        severities = {g.severity for g in validation.gaps}
        assert "error" not in severities

    def test_receipt_without_provenance_fields_still_readable(self):
        """Receipts without new fields don't break validation."""
        receipt = {
            "timestamp": "2026-03-29T18:30:00Z",
            "event_type": "heartbeat",
            "terminal": "T0",
        }
        validation = validate_receipt_provenance(receipt)
        assert isinstance(validation, ProvenanceValidation)

    def test_existing_receipt_readers_unaffected(self, receipts_path):
        """New fields don't break existing receipt reading patterns."""
        receipt = _make_receipt()
        enrich_receipt_provenance(receipt)
        _write_receipts(receipts_path, [receipt])

        # Read back and verify old fields still present
        with receipts_path.open("r") as fh:
            stored = json.loads(fh.readline())

        assert "event_type" in stored
        assert "dispatch_id" in stored
        assert "provenance" in stored
        assert "session" in stored
        # New fields present but optional
        assert "trace_token" in stored
        assert "pr_number" in stored


# ---------------------------------------------------------------------------
# D2 — track.pr_ref propagation from merge-side provenance reconciliation
# ---------------------------------------------------------------------------


def _has_col(conn, table, col):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def _add_track_layer(state_dir):
    with get_connection(state_dir) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                track_id TEXT NOT NULL PRIMARY KEY,
                title TEXT NOT NULL,
                goal_state TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'queued',
                next_up INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                priority TEXT,
                requires_operator_promotion INTEGER NOT NULL DEFAULT 1,
                instruction_template TEXT,
                context_composer_rules TEXT,
                pr_ref TEXT,
                trigger_condition TEXT,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                phase_changed_at TEXT,
                completed_at TEXT,
                metadata_json TEXT DEFAULT '{}'
            )
        """)
        if not _has_col(conn, "dispatches", "project_id"):
            conn.execute(
                "ALTER TABLE dispatches ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'"
            )
        if not _has_col(conn, "dispatches", "track_id"):
            conn.execute("ALTER TABLE dispatches ADD COLUMN track_id TEXT")
        conn.commit()


def _make_project(tmp_path, *, with_track_layer=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(tmp_path),
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    state_dir = repo / ".vnx-data" / "state"
    init_schema(state_dir)
    with get_connection(state_dir) as c:
        if not _has_col(c, "dispatches", "project_id"):
            c.execute(
                "ALTER TABLE dispatches ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'"
            )
        c.commit()
    if with_track_layer:
        _add_track_layer(state_dir)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.row_factory = sqlite3.Row
    return repo, state_dir, conn, env


def _seed_track(conn, track_id, project_id, pr_ref=None):
    conn.execute(
        """
        INSERT INTO tracks (track_id, project_id, title, goal_state, phase, pr_ref)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (track_id, project_id, f"Track {track_id}", f"ship {track_id}", "active", pr_ref),
    )
    conn.commit()


def _seed_dispatch(conn, dispatch_id, project_id, track_id):
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track_id) VALUES (?, ?, ?, ?)",
        (dispatch_id, project_id, "completed", track_id),
    )
    conn.commit()


def _commit(repo, message, env):
    i = len(list(repo.glob("*.txt")))
    f = repo / f"f{i}.txt"
    f.write_text(str(i))
    subprocess.run(["git", "add", str(f)], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, env=env, check=True)


class TestReconcileCommitProvenanceTrackLinkage:
    DISPATCH_ID = "20260706-tl-d2-provenance-prref"

    def _track_ref(self, conn, track_id, project_id):
        row = conn.execute(
            "SELECT pr_ref FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        return row["pr_ref"] if row else None

    def test_links_pr_ref_from_squash_merge(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["linked"] == 1
        assert result["pr_ref_linked"] == 1
        assert self._track_ref(conn, "T-001", "test-proj") == "#412"
        reg = conn.execute(
            "SELECT commit_sha FROM provenance_registry WHERE dispatch_id = ?",
            (self.DISPATCH_ID,),
        ).fetchone()
        assert reg["commit_sha"] is not None
        conn.close()

    def test_idempotent_dedup(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        first = reconcile_commit_provenance(repo, conn, max_commits=10)
        second = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert first["pr_ref_linked"] == 1
        assert second["pr_ref_linked"] == 0
        assert self._track_ref(conn, "T-001", "test-proj") == "#412"
        conn.close()

    def test_preserves_existing_pr_ref(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj", pr_ref="#800")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["pr_ref_linked"] == 1
        assert self._track_ref(conn, "T-001", "test-proj") == "#800,#412"
        conn.close()

    def test_track_id_column_absent_is_noop(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path, with_track_layer=False)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
            (self.DISPATCH_ID, "test-proj", "completed"),
        )
        conn.commit()
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["linked"] == 1
        assert result["pr_ref_linked"] == 0
        reg = conn.execute(
            "SELECT commit_sha FROM provenance_registry WHERE dispatch_id = ?",
            (self.DISPATCH_ID,),
        ).fetchone()
        assert reg["commit_sha"] is not None
        conn.close()

    def test_null_track_id_is_noop(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, track_id) VALUES (?, ?, ?, ?)",
            (self.DISPATCH_ID, "test-proj", "completed", None),
        )
        conn.commit()
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["pr_ref_linked"] == 0
        assert self._track_ref(conn, "T-001", "test-proj") is None
        conn.close()

    def test_unknown_track_id_is_noop(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-missing")
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["pr_ref_linked"] == 0
        assert self._track_ref(conn, "T-001", "test-proj") is None
        conn.close()

    def test_wrong_project_id_is_noop(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "other-proj", "T-001")
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["pr_ref_linked"] == 0
        assert self._track_ref(conn, "T-001", "test-proj") is None
        conn.close()

    def test_no_pr_number_records_commit_only(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _commit(repo, f"feat(x): do thing\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["linked"] == 1
        assert result["pr_ref_linked"] == 0
        assert self._track_ref(conn, "T-001", "test-proj") is None
        reg = conn.execute(
            "SELECT commit_sha FROM provenance_registry WHERE dispatch_id = ?",
            (self.DISPATCH_ID,),
        ).fetchone()
        assert reg["commit_sha"] is not None

    def test_pr_number_registered_on_registry_row(self, tmp_path):
        """Seam 1 (provenance seams PR-B, 2026-07-29): pr_number was extracted
        from the commit body but never passed to register_provenance_link,
        so provenance_registry.pr_number stayed NULL on 0/536 rows even
        though tracks.pr_ref and dispatch_metadata.pr_id (separate
        downstream consumers of the same extraction) were correctly
        populated. This is the only path that can ever supply a real
        pr_number -- the append-time receipt path always sees None, because
        the PR doesn't exist yet when the receipt is written."""
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["linked"] == 1
        reg = conn.execute(
            "SELECT pr_number FROM provenance_registry WHERE dispatch_id = ?",
            (self.DISPATCH_ID,),
        ).fetchone()
        assert reg["pr_number"] == 412
        conn.close()


class TestReconcileCommitProvenanceCentralStateDir:
    """Seam 2 (provenance seams PR-B, 2026-07-29): reconcile_commit_provenance's
    internal state_dir resolution (used only for the pr_ref/pr_id linking
    steps) hardcoded vnx_paths.resolve_state_dir(repo_root) -- the repo-local
    ``.vnx-data/state``. On a central-store deployment (ADR-026) that
    directory is never created; the real state lives at
    ``~/.vnx-data/<project_id>/state``. The pre-fix code silently dropped
    the pr_ref/pr_id linking steps on every central-store project, which is
    the deployment this fabric actually runs in.
    """

    DISPATCH_ID = "20260729-seam2-central-store"

    def _make_project_with_central_home(self, tmp_path, project_id, monkeypatch):
        """Mirrors _make_project, but the state dir lives at the central
        per-project location under an isolated HOME, and the repo gets no
        .vnx-data/state at all -- proving the fix actually resolved the
        central store rather than the fallback silently no-op'ing against a
        nonexistent repo-local path."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        env = {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(home),
        }

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)

        state_dir = home / ".vnx-data" / project_id / "state"
        init_schema(state_dir)
        with get_connection(state_dir) as c:
            if not _has_col(c, "dispatches", "project_id"):
                c.execute(
                    "ALTER TABLE dispatches ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'"
                )
            c.commit()
        _add_track_layer(state_dir)

        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        conn.row_factory = sqlite3.Row
        return repo, state_dir, conn, env

    def test_prefers_central_store_when_it_exists(self, tmp_path, monkeypatch):
        project_id = "seam2-central-proj"
        repo, state_dir, conn, env = self._make_project_with_central_home(
            tmp_path, project_id, monkeypatch,
        )
        _seed_track(conn, "T-001", project_id)
        _seed_dispatch(conn, self.DISPATCH_ID, project_id, "T-001")
        _make_qi_dispatch_metadata(state_dir, project_id, self.DISPATCH_ID)
        _commit(repo, f"feat(x): do thing (#900)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        # No .vnx-data/state anywhere under the repo: the pre-fix
        # resolve_state_dir(repo_root) call resolves to a directory that was
        # never created, so opening a connection against it fails and both
        # linking steps silently no-op.
        assert not (repo / ".vnx-data").exists()

        result = reconcile_commit_provenance(repo, conn, max_commits=10, project_id=project_id)
        conn.commit()

        assert result["linked"] == 1
        assert result["pr_ref_linked"] == 1
        assert result["pr_id_linked"] == 1
        assert _read_pr_id(state_dir, project_id, self.DISPATCH_ID) == "900"
        conn.close()

    def test_ignores_central_store_without_project_id(self, tmp_path, monkeypatch):
        """No project_id supplied: must behave exactly as before the fix
        (repo-local resolution only), even though a central store exists."""
        project_id = "seam2-central-proj-2"
        repo, state_dir, conn, env = self._make_project_with_central_home(
            tmp_path, project_id, monkeypatch,
        )
        _seed_track(conn, "T-001", project_id)
        _seed_dispatch(conn, self.DISPATCH_ID, project_id, "T-001")
        _make_qi_dispatch_metadata(state_dir, project_id, self.DISPATCH_ID)
        _commit(repo, f"feat(x): do thing (#901)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)  # no project_id
        conn.commit()

        assert result["linked"] == 1
        assert result["pr_ref_linked"] == 0
        assert result["pr_id_linked"] == 0
        conn.close()


class TestReconcileCommitProvenanceTransactionNarrowing:
    """OI-851 (PR-3): reconcile_commit_provenance used to open qi_conn once
    and only commit it in the `finally` block after the ENTIRE git-log scan
    completed -- holding qi_conn's write transaction open across every
    remaining commit in the scan, not just the write that opened it. The fix
    commits qi_conn (and state_conn, when it is not the caller's borrowed
    `conn`) after each git-log entry's writes instead.

    This spies on `_link_pr_to_dispatch_metadata` and records
    `qi_conn.in_transaction` at the moment each call STARTS (before it does
    its own write). Under the pre-fix code, commit 0's write leaves
    qi_conn.in_transaction True until the whole scan finishes, so the second
    and third calls would observe True. Under the fix, the first commit's
    transaction is closed out before the second commit is even scanned, so
    every call observes False.
    """

    def _make_qi_db_with_rows(self, state_dir, project_id, dispatch_ids):
        qi_db = state_dir / "quality_intelligence.db"
        qi_conn = sqlite3.connect(str(qi_db))
        qi_conn.execute(
            """
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                pr_id TEXT,
                UNIQUE(project_id, dispatch_id)
            )
            """
        )
        for dispatch_id in dispatch_ids:
            qi_conn.execute(
                "INSERT INTO dispatch_metadata (dispatch_id, project_id, pr_id) "
                "VALUES (?, ?, NULL)",
                (dispatch_id, project_id),
            )
        qi_conn.commit()
        qi_conn.close()

    def test_qi_conn_transaction_not_held_across_scan(self, tmp_path, monkeypatch):
        repo, state_dir, conn, env = _make_project(tmp_path)
        project_id = "test-proj"
        _seed_track(conn, "T-001", project_id)
        dispatch_ids = [f"20260730-txn-narrow-{i}" for i in range(3)]
        for dispatch_id in dispatch_ids:
            _seed_dispatch(conn, dispatch_id, project_id, "T-001")
        self._make_qi_db_with_rows(state_dir, project_id, dispatch_ids)
        for i, dispatch_id in enumerate(dispatch_ids):
            _commit(
                repo,
                f"feat(x): thing {i} (#{500 + i})\n\nDispatch-ID: {dispatch_id}",
                env,
            )

        seen_in_transaction = []
        real_fn = receipt_provenance._link_pr_to_dispatch_metadata

        def _spy(qi_conn, pid, dispatch_id, pr_number):
            seen_in_transaction.append(qi_conn.in_transaction)
            return real_fn(qi_conn, pid, dispatch_id, pr_number)

        monkeypatch.setattr(receipt_provenance, "_link_pr_to_dispatch_metadata", _spy)

        result = reconcile_commit_provenance(
            repo, conn, max_commits=10, project_id=project_id,
        )
        conn.commit()

        assert result["pr_id_linked"] == 3
        assert len(seen_in_transaction) == 3
        assert seen_in_transaction == [False, False, False], (
            "qi_conn's write transaction from an earlier commit was still "
            "open when a later commit's write started -- the transaction is "
            "being held across the scan instead of committed per entry"
        )
        conn.close()


class TestReconcileCommitProvenanceLinkedPendingCommit:
    """Provenance-chain PR-A (2026-07-29): ``linked`` must not claim durability
    it hasn't earned. ``conn`` is caller-owned — this function never commits
    it — so ``linked`` alone only means "the register call didn't raise", not
    "this row survives conn.close()". ``linked_pending_commit`` makes the gap
    visible instead of letting a caller (or a log line) read ``linked`` as
    already-durable."""

    DISPATCH_ID = "20260729-pending-commit-check"

    def test_mirrors_linked_before_caller_commits(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _commit(repo, f"feat(x): do thing\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)

        assert result["linked"] == 1
        # conn has NOT been committed yet: sqlite3's implicit transaction is
        # still open, so the write is one dropped connection away from
        # vanishing exactly like the historical objective_reconcile.py bug.
        assert conn.in_transaction is True
        assert result["linked_pending_commit"] == 1
        conn.commit()
        conn.close()

    def test_zero_when_nothing_written(self, tmp_path):
        repo, _state_dir, conn, env = _make_project(tmp_path)
        _commit(repo, "chore: unrelated commit with no trace token", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)

        assert result["linked"] == 0
        assert result["linked_pending_commit"] == 0
        conn.close()

    def test_zero_under_autocommit_because_nothing_is_actually_pending(self, tmp_path):
        """linked_pending_commit must track real durability risk, not just
        linked > 0. Under autocommit (isolation_level=None) every statement
        commits immediately, so nothing is pending even though this function
        itself never calls .commit()."""
        repo, state_dir, conn, env = _make_project(tmp_path)
        conn.close()
        auto_conn = sqlite3.connect(
            str(state_dir / "runtime_coordination.db"), isolation_level=None
        )
        auto_conn.row_factory = sqlite3.Row
        _seed_track(auto_conn, "T-002", "test-proj")
        _seed_dispatch(auto_conn, self.DISPATCH_ID, "test-proj", "T-002")
        _commit(repo, f"feat(y): do thing\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, auto_conn, max_commits=10)

        assert result["linked"] == 1
        assert auto_conn.in_transaction is False
        assert result["linked_pending_commit"] == 0

        # Prove it: close WITHOUT ever calling .commit() and the row survives.
        auto_conn.close()
        verify_conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        row = verify_conn.execute(
            "SELECT commit_sha FROM provenance_registry WHERE dispatch_id = ?",
            (self.DISPATCH_ID,),
        ).fetchone()
        assert row is not None and row[0] is not None
        verify_conn.close()


# ---------------------------------------------------------------------------
# receipt-quality PR-B2 — dispatch_metadata.pr_id backfill (sibling of
# tracks.pr_ref, a different database: quality_intelligence.db)
# ---------------------------------------------------------------------------


def _make_qi_dispatch_metadata(state_dir, project_id, dispatch_id, pr_id=None):
    """Minimal quality_intelligence.db with just the dispatch_metadata columns
    reconcile_commit_provenance's pr_id backfill touches (mirrors the
    hand-rolled _add_track_layer helper's minimal-schema style above)."""
    qi_db = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(qi_db))
    conn.execute(
        """
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            pr_id TEXT,
            UNIQUE(project_id, dispatch_id)
        )
        """
    )
    conn.execute(
        "INSERT INTO dispatch_metadata (dispatch_id, project_id, pr_id) VALUES (?, ?, ?)",
        (dispatch_id, project_id, pr_id),
    )
    conn.commit()
    conn.close()
    return qi_db


def _read_pr_id(state_dir, project_id, dispatch_id):
    conn = sqlite3.connect(str(state_dir / "quality_intelligence.db"))
    row = conn.execute(
        "SELECT pr_id FROM dispatch_metadata WHERE project_id = ? AND dispatch_id = ?",
        (project_id, dispatch_id),
    ).fetchone()
    conn.close()
    return row[0] if row else None


class TestReconcileCommitProvenanceDispatchMetadataPrId:
    DISPATCH_ID = "20260728-rq-b2-provenance-prid"

    def test_fills_empty_pr_id_from_commit(self, tmp_path):
        repo, state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _make_qi_dispatch_metadata(state_dir, "test-proj", self.DISPATCH_ID)
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["pr_id_linked"] == 1
        assert _read_pr_id(state_dir, "test-proj", self.DISPATCH_ID) == "412"
        conn.close()

    def test_stores_bare_numeric_pr_id_not_hash_prefixed(self, tmp_path):
        """Codex-gate Finding B (fix-forward, PR #1235): dispatch_metadata.pr_id
        must be the BARE numeric PR id, not "#412" -- prior-round-intelligence
        consumers (prior_round_injector.py) build
        review_gates/results/pr-{pr_id}-{gate}.json paths from this value
        verbatim, and a "#"-prefixed value never matches a bare-numeric-keyed
        path. Distinct from tracks.pr_ref (test_links_pr_ref_from_squash_merge
        above), which legitimately keeps the "#"-prefixed display format."""
        repo, state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _make_qi_dispatch_metadata(state_dir, "test-proj", self.DISPATCH_ID)
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        stored = _read_pr_id(state_dir, "test-proj", self.DISPATCH_ID)
        assert stored == "412"
        assert not stored.startswith("#")
        conn.close()

    def test_idempotent_second_reconcile_is_noop(self, tmp_path):
        repo, state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _make_qi_dispatch_metadata(state_dir, "test-proj", self.DISPATCH_ID)
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        first = reconcile_commit_provenance(repo, conn, max_commits=10)
        second = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert first["pr_id_linked"] == 1
        assert second["pr_id_linked"] == 0
        assert _read_pr_id(state_dir, "test-proj", self.DISPATCH_ID) == "412"
        conn.close()

    def test_never_overwrites_existing_pr_id(self, tmp_path):
        """Fill-once: a pr_id already present is never clobbered by a later
        commit referencing a different PR number."""
        repo, state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _make_qi_dispatch_metadata(state_dir, "test-proj", self.DISPATCH_ID, pr_id="#100")
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["pr_id_linked"] == 0
        assert _read_pr_id(state_dir, "test-proj", self.DISPATCH_ID) == "#100"
        conn.close()

    def test_no_pr_number_leaves_pr_id_null(self, tmp_path):
        repo, state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _make_qi_dispatch_metadata(state_dir, "test-proj", self.DISPATCH_ID)
        _commit(repo, f"feat(x): do thing\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["pr_id_linked"] == 0
        assert _read_pr_id(state_dir, "test-proj", self.DISPATCH_ID) is None
        conn.close()

    def test_missing_qi_db_is_noop_not_fatal(self, tmp_path):
        """No quality_intelligence.db at all (project never bootstrapped it) --
        the RC-side pr_ref linkage must keep working exactly as before."""
        repo, state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        assert not (state_dir / "quality_intelligence.db").exists()
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["pr_id_linked"] == 0
        assert result["pr_ref_linked"] == 1
        conn.close()

    def test_wrong_project_id_row_is_noop(self, tmp_path):
        repo, state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _make_qi_dispatch_metadata(state_dir, "other-proj", self.DISPATCH_ID)
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        result = reconcile_commit_provenance(repo, conn, max_commits=10)
        conn.commit()

        assert result["pr_id_linked"] == 0
        assert _read_pr_id(state_dir, "other-proj", self.DISPATCH_ID) is None
        conn.close()
        conn.close()

    def test_explicit_project_id_bypasses_ambiguous_lookup(self, tmp_path, monkeypatch):
        """Codex-gate Finding A (fix-forward, PR #1235): when the caller already
        knows its own project_id (the reconciliation loop operates on a
        per-project store), reconcile_commit_provenance uses it directly for
        the dispatch_metadata.pr_id backfill instead of re-resolving by
        dispatch_id alone -- a lookup that cannot disambiguate a same-
        dispatch_id row under a different project_id (ADR-007 composite key).
        Proven here by making the fallback resolver raise: if it were called
        despite project_id being supplied, this test would fail loudly rather
        than silently passing on the resolver's own (also-correct) answer."""
        repo, state_dir, conn, env = _make_project(tmp_path)
        _seed_track(conn, "T-001", "test-proj")
        _seed_dispatch(conn, self.DISPATCH_ID, "test-proj", "T-001")
        _make_qi_dispatch_metadata(state_dir, "test-proj", self.DISPATCH_ID)
        _commit(repo, f"feat(x): do thing (#412)\n\nDispatch-ID: {self.DISPATCH_ID}", env)

        def _must_not_be_called(*_args, **_kwargs):
            raise AssertionError(
                "_resolve_dispatch_project_id must not be called when the "
                "caller already supplied project_id"
            )
        monkeypatch.setattr(receipt_provenance, "_resolve_dispatch_project_id", _must_not_be_called)

        result = reconcile_commit_provenance(repo, conn, max_commits=10, project_id="test-proj")
        conn.commit()

        assert result["pr_id_linked"] == 1
        assert _read_pr_id(state_dir, "test-proj", self.DISPATCH_ID) == "412"
        conn.close()


# ---------------------------------------------------------------------------
# receipt-quality PR-B2 fix-forward (Finding A) — _resolve_dispatch_project_id
# must be ADR-007 composite-safe: a dispatch_id colliding across more than one
# project_id (dispatches is UNIQUE(dispatch_id, project_id), so this is a
# legitimate multi-tenant state, not corruption) must abstain (None) rather
# than resolve an arbitrary/first row and let a downstream composite-scoped
# UPDATE stamp the wrong tenant's dispatch_metadata.pr_id.
# ---------------------------------------------------------------------------


class TestResolveDispatchProjectIdCompositeSafety:
    def _make_conn(self, tmp_path):
        """Minimal dispatches table with the ADR-007 composite-unique key
        (mirrors schemas/runtime_coordination_v10.sql), independent of
        _make_project's legacy single-column-UNIQUE(dispatch_id) table (which
        cannot represent the collision this test exercises at all)."""
        db_path = tmp_path / "rc.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'queued',
                UNIQUE(dispatch_id, project_id)
            )
            """
        )
        conn.commit()
        return conn

    def test_unambiguous_single_project_resolves(self, tmp_path):
        conn = self._make_conn(tmp_path)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id) VALUES (?, ?)",
            ("dispatch-x", "proj-a"),
        )
        conn.commit()

        assert receipt_provenance._resolve_dispatch_project_id(conn, "dispatch-x") == "proj-a"
        conn.close()

    def test_cross_tenant_collision_abstains(self, tmp_path):
        conn = self._make_conn(tmp_path)
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id) VALUES (?, ?)",
            ("dispatch-x", "proj-a"),
        )
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id) VALUES (?, ?)",
            ("dispatch-x", "proj-b"),
        )
        conn.commit()

        assert receipt_provenance._resolve_dispatch_project_id(conn, "dispatch-x") is None
        conn.close()

    def test_no_matching_row_returns_none(self, tmp_path):
        conn = self._make_conn(tmp_path)
        assert receipt_provenance._resolve_dispatch_project_id(conn, "nonexistent") is None
        conn.close()

    def test_missing_project_id_column_returns_none(self, tmp_path):
        db_path = tmp_path / "rc-legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE dispatches (id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL UNIQUE)"
        )
        conn.execute("INSERT INTO dispatches (dispatch_id) VALUES (?)", ("dispatch-x",))
        conn.commit()

        assert receipt_provenance._resolve_dispatch_project_id(conn, "dispatch-x") is None
        conn.close()


# ============================================================================
# OI-830: chain_status rename verification
# ============================================================================

class TestChainStatusRename:
    """Verify the old 'complete' value is never produced and the new
    'receipt_and_commit' value is produced in its place."""

    def test_old_value_not_produced_by_validate(self):
        """validate_receipt_provenance never returns 'complete' as chain_status."""
        receipt = _make_receipt(
            trace_token="Dispatch-ID: 20260329-180606-test-task-B",
            feature_plan_pr="PR-2",
        )
        validation = validate_receipt_provenance(receipt)
        assert validation.chain_status != "complete"
        assert validation.chain_status == CHAIN_STATUS_COMPLETE
        assert validation.chain_status == "receipt_and_commit"

    def test_old_value_not_produced_by_calculate(self):
        """_calculate_chain_status never returns 'complete'."""
        from receipt_provenance import _calculate_chain_status

        fields = {"receipt_id": "run-001", "commit_sha": "abc123"}
        status = _calculate_chain_status(fields)
        assert status != "complete"
        assert status == CHAIN_STATUS_COMPLETE
        assert status == "receipt_and_commit"

    def test_old_value_not_produced_by_register(self, conn):
        """register_provenance_link never sets chain_status to 'complete'."""
        link = register_provenance_link(
            conn,
            dispatch_id="20260804-oi830-register-verify",
            receipt_id="run-oi830",
            commit_sha="sha-oi830",
        )
        conn.commit()
        assert link.chain_status != "complete"
        assert link.chain_status == CHAIN_STATUS_COMPLETE
        assert link.chain_status == "receipt_and_commit"

    def test_old_value_not_produced_by_batch_summary(self, receipts_path):
        """batch_provenance_summary never counts 'complete' category."""
        receipts = [
            _make_receipt(
                dispatch_id="DISP-OI830-A",
                trace_token="Dispatch-ID: DISP-OI830-A",
                feature_plan_pr="PR-2",
            ),
        ]
        _write_receipts(receipts_path, receipts)
        batch = batch_provenance_summary(["DISP-OI830-A"], receipts_path)
        assert "complete" not in batch["chain_status_counts"]

    def test_has_pr_has_fp_do_not_exist(self):
        """The dead variables has_pr and has_fp are removed from the module."""
        import inspect
        from receipt_provenance import _calculate_chain_status

        source = inspect.getsource(_calculate_chain_status)
        assert "has_pr" not in source
        assert "has_fp" not in source
