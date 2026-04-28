#!/usr/bin/env python3
"""T5-PR3: instruction_sha256 surfaced in receipt session metadata.

Covers:
  A. Receipt with valid manifest_path → instruction_sha256 in session metadata
  B. Receipt without manifest_path → field absent, no crash
  C. Manifest file missing → field absent, warning logged to stderr
  D. Malformed manifest JSON → field absent, warning logged to stderr
  E. Existing receipt enrichment (token usage etc.) unaffected
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
