#!/usr/bin/env python3
"""T5-PR3 / CFX-10: instruction_sha256 surfaced in receipt session metadata.

Covers:
  A. Receipt with valid manifest_path (legacy 16-char) → instruction_sha256 in metadata (backward-compat: reader accepts any length)
  B. Receipt without manifest_path → field absent, no crash
  C. Manifest file missing → field absent, warning logged to stderr
  D. Malformed manifest JSON → field absent, warning logged to stderr
  E. Existing receipt enrichment (token usage etc.) unaffected
  F. subprocess_completion event → instruction_sha256 surfaces via full append path
  G. subprocess_completion must NOT overwrite quality_advisory_json/cqs
  H. real task_complete still triggers quality_advisory + CQS persistence
  I. New manifests with 64-char sha256 are read correctly
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"


def _load_append_receipt():
    env_patch = {
        "PROJECT_ROOT": str(VNX_ROOT),
        "VNX_DATA_DIR": str(VNX_ROOT / ".vnx-data"),
        "VNX_STATE_DIR": str(VNX_ROOT / ".vnx-data" / "state"),
        "VNX_HOME": str(VNX_ROOT),
    }
    mod_name = "append_receipt_ih_testmodule"
    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(
            mod_name, SCRIPTS_DIR / "append_receipt.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            del sys.modules[mod_name]
            raise
    return mod


@pytest.fixture(scope="module")
def ar():
    return _load_append_receipt()


def _make_receipt(terminal: str = "T1", **extra) -> dict:
    base = {
        "timestamp": "2026-04-28T12:00:00Z",
        "event_type": "task_complete",
        "event": "task_complete",
        "dispatch_id": "DISP-IH-001",
        "terminal": terminal,
        "status": "success",
        "source": "pytest",
    }
    base.update(extra)
    return base


def _call_build_session(ar_mod, receipt: dict, state_dir: Path) -> tuple[dict, str]:
    """Call _build_session_metadata and capture stderr output."""
    buf = io.StringIO()
    with patch.object(ar_mod, "_resolve_model_provider",
                      return_value={"model": "claude-sonnet-4-6", "provider": "anthropic"}):
        with patch.object(ar_mod, "_resolve_session_id", return_value="sess-test-0001"):
            with patch.object(ar_mod, "_extract_session_token_usage", return_value=None):
                with patch("sys.stderr", buf):
                    result = ar_mod._build_session_metadata(receipt, state_dir)
    return result, buf.getvalue()


# ── Case A: valid manifest_path → instruction_sha256 in metadata ──────────────

def test_valid_manifest_surfaces_instruction_sha256(ar, tmp_path):
    manifest = {
        "dispatch_id": "DISP-IH-001",
        "instruction_sha256": "abcdef1234567890",
        "instruction_chars": 42,
    }
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest))

    receipt = _make_receipt(manifest_path=str(manifest_file))
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    metadata, _ = _call_build_session(ar, receipt, state_dir)

    assert "instruction_sha256" in metadata
    assert metadata["instruction_sha256"] == "abcdef1234567890"


# ── Case I: new manifest with full 64-char sha256 → surfaces correctly ───────

def test_full_64char_sha256_surfaces_in_session_metadata(ar, tmp_path):
    """CFX-10: reader handles full 64-char sha256 from new manifests."""
    import hashlib as _hashlib
    instruction = "Full SHA-256 instruction text"
    full_sha = _hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    assert len(full_sha) == 64

    manifest = {
        "dispatch_id": "DISP-IH-I01",
        "instruction_sha256": full_sha,
        "instruction_chars": len(instruction),
    }
    manifest_file = tmp_path / "manifest_full.json"
    manifest_file.write_text(json.dumps(manifest))

    receipt = _make_receipt(manifest_path=str(manifest_file))
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    metadata, _ = _call_build_session(ar, receipt, state_dir)

    assert "instruction_sha256" in metadata
    assert metadata["instruction_sha256"] == full_sha
    assert len(metadata["instruction_sha256"]) == 64


# ── Case B: no manifest_path → field absent, no crash ────────────────────────

def test_no_manifest_path_field_absent_no_crash(ar, tmp_path):
    receipt = _make_receipt()  # no manifest_path key
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    metadata, _ = _call_build_session(ar, receipt, state_dir)

    assert "instruction_sha256" not in metadata
    # core fields still present
    assert metadata["session_id"] == "sess-test-0001"
    assert metadata["terminal"] == "T1"


# ── Case C: manifest file missing → field absent, warning logged ──────────────

def test_missing_manifest_file_field_absent_warning_logged(ar, tmp_path):
    receipt = _make_receipt(manifest_path=str(tmp_path / "nonexistent_manifest.json"))
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    metadata, stderr = _call_build_session(ar, receipt, state_dir)

    assert "instruction_sha256" not in metadata
    assert "warning" in stderr.lower()


# ── Case D: malformed manifest JSON → field absent, warning logged ─────────────

def test_malformed_manifest_json_field_absent_warning_logged(ar, tmp_path):
    bad_manifest = tmp_path / "manifest_bad.json"
    bad_manifest.write_text("{not: valid json,,,}")

    receipt = _make_receipt(manifest_path=str(bad_manifest))
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    metadata, stderr = _call_build_session(ar, receipt, state_dir)

    assert "instruction_sha256" not in metadata
    assert "warning" in stderr.lower()


# ── Case E: existing enrichment (session_id, model) unaffected ───────────────

def test_existing_session_enrichment_unaffected(ar, tmp_path):
    manifest = {
        "dispatch_id": "DISP-IH-002",
        "instruction_sha256": "cafebabe12345678",
        "instruction_chars": 100,
    }
    manifest_file = tmp_path / "manifest2.json"
    manifest_file.write_text(json.dumps(manifest))

    receipt = _make_receipt(
        manifest_path=str(manifest_file),
        terminal="T2",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    buf = io.StringIO()
    token_usage = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    with patch.object(ar, "_resolve_model_provider",
                      return_value={"model": "claude-sonnet-4-6", "provider": "anthropic"}):
        with patch.object(ar, "_resolve_session_id", return_value="sess-enrich-9999"):
            with patch.object(ar, "_extract_session_token_usage", return_value=token_usage):
                with patch("sys.stderr", buf):
                    metadata = ar._build_session_metadata(receipt, state_dir)

    assert metadata["session_id"] == "sess-enrich-9999"
    assert metadata["model"] == "claude-sonnet-4-6"
    assert metadata["provider"] == "anthropic"
    assert metadata["terminal"] == "T2"
    assert metadata["token_usage"] == token_usage
    assert metadata["instruction_sha256"] == "cafebabe12345678"
    assert "captured_at" in metadata


# ── Case F: subprocess_completion event → instruction_sha256 surfaces via full append path ──

def test_subprocess_completion_surfaces_sha256_via_append_path(ar, tmp_path):
    """Regression: subprocess_completion must reach _build_session_metadata.

    Before the fix, _is_completion_event excluded 'subprocess_completion', so
    _enrich_completion_receipt returned early and instruction_sha256 was never
    written to the persisted receipt's session metadata.
    """
    manifest = {
        "dispatch_id": "DISP-IH-F01",
        "instruction_sha256": "deadbeef00112233",
        "instruction_chars": 64,
    }
    manifest_file = tmp_path / "manifest_f.json"
    manifest_file.write_text(json.dumps(manifest))

    receipt = {
        "timestamp": "2026-04-29T00:00:00Z",
        "event_type": "subprocess_completion",
        "dispatch_id": "DISP-IH-F01",
        "terminal": "T1",
        "status": "success",
        "source": "subprocess",
        "manifest_path": str(manifest_file),
    }

    receipts_file = str(tmp_path / "receipts.ndjson")

    with patch.dict(os.environ, {
        "PROJECT_ROOT": str(VNX_ROOT),
        "VNX_DATA_DIR": str(tmp_path),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(tmp_path / "state"),
        "VNX_HOME": str(VNX_ROOT),
    }):
        (tmp_path / "state").mkdir(exist_ok=True)
        with patch.object(ar, "_resolve_model_provider",
                          return_value={"model": "claude-sonnet-4-6", "provider": "anthropic"}):
            with patch.object(ar, "_resolve_session_id", return_value="sess-f-0001"):
                with patch.object(ar, "_extract_session_token_usage", return_value=None):
                    with patch.object(ar, "collect_terminal_snapshot") as mock_snap:
                        snap = MagicMock()
                        snap.to_dict.return_value = {"status": "ok"}
                        mock_snap.return_value = snap
                        with patch.object(ar, "enrich_receipt_provenance", return_value=None):
                            with patch.object(ar, "validate_receipt_provenance") as mock_val:
                                mock_val.return_value = MagicMock(gaps=[], chain_status="ok")
                                with patch.object(ar, "_build_git_provenance",
                                                  return_value={"git_ref": "HEAD", "branch": "test"}):
                                    result = ar.append_receipt_payload(
                                        receipt,
                                        receipts_file=receipts_file,
                                    )

    assert result.status == "appended", f"append failed: {result}"

    written = (tmp_path / "receipts.ndjson").read_text().strip()
    persisted = json.loads(written)

    session = persisted.get("session", {})
    assert "instruction_sha256" in session, (
        "instruction_sha256 must be present in session metadata for subprocess_completion receipts"
    )
    assert session["instruction_sha256"] == "deadbeef00112233"


# ── Case G: subprocess_completion must NOT overwrite quality_advisory_json/cqs ──
#
# Codex regate finding (PR #309 round 2): treating subprocess_completion as a
# full completion event made _enrich_completion_receipt run quality-advisory +
# CQS persistence for receipts written before the real report exists. With no
# changed files visible at that point, the synthetic "No changed files
# detected" advisory and a zero-CQS row would overwrite any later, report-
# driven enrichment in dispatch_metadata.

def _seed_dispatch_metadata(db_path: Path, dispatch_id: str, advisory_json: str, cqs_value: float) -> None:
    """Seed quality_intelligence.db with a populated dispatch_metadata row."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dispatch_metadata (
            dispatch_id TEXT PRIMARY KEY,
            cqs REAL,
            normalized_status TEXT,
            cqs_components TEXT,
            open_items_created INTEGER,
            open_items_resolved INTEGER,
            quality_advisory_json TEXT,
            target_open_items TEXT
        )"""
    )
    conn.execute(
        """INSERT OR REPLACE INTO dispatch_metadata
           (dispatch_id, cqs, normalized_status, cqs_components,
            open_items_created, open_items_resolved, quality_advisory_json, target_open_items)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (dispatch_id, cqs_value, "passed", json.dumps({"base": cqs_value}),
         3, 1, advisory_json, json.dumps([])),
    )
    conn.commit()
    conn.close()


def _read_dispatch_metadata(db_path: Path, dispatch_id: str) -> dict:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        """SELECT cqs, normalized_status, cqs_components,
                  open_items_created, open_items_resolved, quality_advisory_json
           FROM dispatch_metadata WHERE dispatch_id=?""",
        (dispatch_id,),
    ).fetchone()
    conn.close()
    assert row is not None, f"no dispatch_metadata row for {dispatch_id}"
    return {
        "cqs": row[0],
        "normalized_status": row[1],
        "cqs_components": row[2],
        "open_items_created": row[3],
        "open_items_resolved": row[4],
        "quality_advisory_json": row[5],
    }


def test_subprocess_completion_does_not_overwrite_dispatch_metadata(ar, tmp_path):
    """Regression: subprocess_completion must NOT touch quality_advisory_json/cqs.

    The intermediate subprocess receipt is appended before the real report
    exists. Running quality advisory + CQS persistence for it would write a
    synthetic "No changed files detected" advisory and zero-CQS row, then
    permanently corrupt dispatch_metadata if the report-driven enrichment is
    delayed or fails (codex finding 1, PR #309 r2).
    """
    dispatch_id = "DISP-IH-G01"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "quality_intelligence.db"

    real_advisory = json.dumps({
        "version": "1.0",
        "summary": {"warning_count": 2, "blocking_count": 1, "risk_score": 75},
        "t0_recommendation": {"decision": "review", "reason": "real findings"},
    })
    seeded_cqs = 0.82
    _seed_dispatch_metadata(db_path, dispatch_id, real_advisory, seeded_cqs)

    receipt = {
        "timestamp": "2026-04-29T01:00:00Z",
        "event_type": "subprocess_completion",
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "status": "success",
        "source": "subprocess",
    }

    with patch.dict(os.environ, {
        "PROJECT_ROOT": str(VNX_ROOT),
        "VNX_DATA_DIR": str(tmp_path),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(VNX_ROOT),
    }):
        with patch.object(ar, "_resolve_model_provider",
                          return_value={"model": "claude-sonnet-4-6", "provider": "anthropic"}):
            with patch.object(ar, "_resolve_session_id", return_value="sess-g-0001"):
                with patch.object(ar, "_extract_session_token_usage", return_value=None):
                    with patch.object(ar, "collect_terminal_snapshot") as mock_snap:
                        snap = MagicMock()
                        snap.to_dict.return_value = {"status": "ok"}
                        mock_snap.return_value = snap
                        with patch.object(ar, "enrich_receipt_provenance", return_value=None):
                            with patch.object(ar, "validate_receipt_provenance") as mock_val:
                                mock_val.return_value = MagicMock(gaps=[], chain_status="ok")
                                with patch.object(ar, "_build_git_provenance",
                                                  return_value={"git_ref": "HEAD", "branch": "test"}):
                                    with patch.object(ar, "get_changed_files", return_value=[]):
                                        with patch.object(ar, "calculate_cqs",
                                                          side_effect=AssertionError(
                                                              "calculate_cqs must NOT be called for subprocess_completion"
                                                          )):
                                            with patch.object(ar, "generate_quality_advisory",
                                                              side_effect=AssertionError(
                                                                  "generate_quality_advisory must NOT be called for subprocess_completion"
                                                              )):
                                                enriched = ar._enrich_completion_receipt(receipt)

    # Receipt itself must not carry a synthetic advisory or CQS payload.
    assert "quality_advisory" not in enriched, (
        "subprocess_completion must not generate a quality_advisory on the receipt"
    )
    assert "cqs" not in enriched, (
        "subprocess_completion must not compute cqs"
    )

    # Most importantly: dispatch_metadata row must be untouched.
    after = _read_dispatch_metadata(db_path, dispatch_id)
    assert after["quality_advisory_json"] == real_advisory, (
        "dispatch_metadata.quality_advisory_json was overwritten by subprocess_completion"
    )
    assert after["cqs"] == seeded_cqs, (
        "dispatch_metadata.cqs was overwritten by subprocess_completion"
    )
    assert after["normalized_status"] == "passed"
    assert after["open_items_created"] == 3
    assert after["open_items_resolved"] == 1


# ── Case H: real task_complete still triggers quality_advisory + CQS persistence ──

def test_task_complete_still_persists_quality_advisory_and_cqs(ar, tmp_path):
    """Positive control: real completion events still persist advisory + CQS.

    Ensures the subprocess_completion guard does not regress the canonical
    completion path.
    """
    dispatch_id = "DISP-IH-H01"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "quality_intelligence.db"
    _seed_dispatch_metadata(db_path, dispatch_id, json.dumps({"placeholder": True}), 0.0)

    receipt = {
        "timestamp": "2026-04-29T02:00:00Z",
        "event_type": "task_complete",
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        "status": "success",
        "source": "pytest",
    }

    fake_cqs = {
        "cqs": 0.91,
        "normalized_status": "passed",
        "components": {"base": 0.91},
    }

    with patch.dict(os.environ, {
        "PROJECT_ROOT": str(VNX_ROOT),
        "VNX_DATA_DIR": str(tmp_path),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(state_dir),
        "VNX_HOME": str(VNX_ROOT),
    }):
        with patch.object(ar, "_resolve_model_provider",
                          return_value={"model": "claude-sonnet-4-6", "provider": "anthropic"}):
            with patch.object(ar, "_resolve_session_id", return_value="sess-h-0001"):
                with patch.object(ar, "_extract_session_token_usage", return_value=None):
                    with patch.object(ar, "collect_terminal_snapshot") as mock_snap:
                        snap = MagicMock()
                        snap.to_dict.return_value = {"status": "ok"}
                        mock_snap.return_value = snap
                        with patch.object(ar, "enrich_receipt_provenance", return_value=None):
                            with patch.object(ar, "validate_receipt_provenance") as mock_val:
                                mock_val.return_value = MagicMock(gaps=[], chain_status="ok")
                                with patch.object(ar, "_build_git_provenance",
                                                  return_value={"git_ref": "HEAD", "branch": "test"}):
                                    with patch.object(ar, "get_changed_files", return_value=[]):
                                        with patch.object(ar, "calculate_cqs", return_value=fake_cqs) as mock_cqs:
                                            enriched = ar._enrich_completion_receipt(receipt)

    assert mock_cqs.called, "calculate_cqs must run for task_complete events"
    assert enriched.get("cqs") == fake_cqs
    assert "quality_advisory" in enriched, "task_complete must produce a quality_advisory on receipt"

    after = _read_dispatch_metadata(db_path, dispatch_id)
    assert after["cqs"] == 0.91
    persisted_advisory = json.loads(after["quality_advisory_json"])
    assert persisted_advisory.get("t0_recommendation", {}).get("reason") == "No changed files detected"
