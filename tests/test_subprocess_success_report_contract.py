#!/usr/bin/env python3
"""Regression: the subprocess lane's success path must end with a contract-valid report.

Measured defect: on success, ``_handle_success`` (recovery.py) called
``_ensure_unified_report(dispatch_id, terminal_id, "done")``, which wrote a
legacy stub with only ``## Summary`` + ``## Open Items``, no ``**Model**`` /
``**Provider**`` field, and — when ``VNX_REPORTS_DIR`` was unset — wrote nothing
at all. ``report_body_contract.validate_body()`` on that stub returns
``valid=False, missing=['## Changes', '## Verification']`` and the report-to-receipt
converter refuses a dispatch report without a real model, so a successful
dispatch ended with no usable report.

These tests drive the real ``_handle_success`` for a worker that wrote no report
of its own and assert:
  - the report on disk passes ``validate_body`` and names the model it ran with;
  - a report is written even when ``VNX_REPORTS_DIR`` is unset (no silent skip);
  - a worker-authored report (any filename form) is never overwritten;
  - the completion receipt is still written exactly once.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
for _p in (str(REPO_ROOT), str(SCRIPTS_LIB)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import subprocess_dispatch  # noqa: E402
from report_body_contract import validate_body  # noqa: E402
from subprocess_dispatch_internals.delivery_runtime import _SubprocessResult  # noqa: E402
from subprocess_dispatch_internals.recovery import _handle_success  # noqa: E402

DISPATCH_ID = "20260930-120000-closeout-report-A"


def _run_success(*, model: str = "sonnet", touched=frozenset(), dispatch_id: str = DISPATCH_ID):
    """Drive the real _handle_success with all external side effects stubbed out."""
    monitor = MagicMock()
    monitor.stuck_count = 0

    sub_result = _SubprocessResult(
        success=True,
        session_id="sess-closeout",
        event_count=2,
        manifest_path=None,
        touched_files=touched,
    )

    with patch.object(subprocess_dispatch, "_get_commit_hash", return_value="abc123"), \
         patch.object(subprocess_dispatch, "_check_commit_since", return_value=False), \
         patch.object(subprocess_dispatch, "_commit_belongs_to_dispatch", return_value=False), \
         patch.object(subprocess_dispatch, "_write_receipt") as mock_receipt, \
         patch.object(subprocess_dispatch, "_update_pattern_confidence", return_value=0), \
         patch.object(subprocess_dispatch, "_capture_dispatch_outcome"), \
         patch.object(subprocess_dispatch, "cleanup_worker_exit"), \
         patch.object(subprocess_dispatch, "_resolve_active_dispatch_file", return_value=None), \
         patch("provider_costs.emit_provider_cost"), \
         patch("project_scope.resolve_stamp_project_id", return_value=""):
        _handle_success(
            dispatch_id=dispatch_id,
            terminal_id="T1",
            attempt=0,
            sub_result=sub_result,
            monitor=monitor,
            auto_commit=False,
            gate="",
            pre_dispatch_dirty=frozenset(),
            manifest_paths=None,
            commit_hash_before="abc123",
            dispatch_start_ts="2026-09-30T00:00:00+00:00",
            pre_sha="abc123",
            lease_generation=None,
            model=model,
            pr_id=None,
            mandate_id=None,
            instruction="Do the thing.",
            role="backend-developer",
        )
    return mock_receipt


@pytest.fixture(autouse=True)
def _shared_govern_off(monkeypatch):
    """The defect lives on the default branch (VNX_SHARED_GOVERN off)."""
    monkeypatch.delenv("VNX_SHARED_GOVERN", raising=False)


def test_success_writes_contract_valid_report_naming_model(tmp_path, monkeypatch):
    reports_dir = tmp_path / "unified_reports"
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))

    mock_receipt = _run_success(model="sonnet")

    report_path = reports_dir / f"{DISPATCH_ID}.md"
    assert report_path.exists(), "the success path must leave a report on disk"
    text = report_path.read_text(encoding="utf-8")

    result = validate_body(text)
    assert result.valid, f"report fails the body contract: missing={result.missing}"
    assert result.status == "authored"
    assert "**Model**: sonnet" in text
    assert "**Provider**: claude" in text
    assert f"**Dispatch-ID**: {DISPATCH_ID}" in text
    # The legacy stub is gone.
    assert "Auto-Generated**: stub" not in text

    # Receipt writing is untouched: exactly one completion receipt.
    mock_receipt.assert_called_once()


def test_report_is_written_without_reports_dir_env(tmp_path, monkeypatch):
    """No silent skip: the report lands in the derived store when VNX_REPORTS_DIR is unset."""
    monkeypatch.delenv("VNX_REPORTS_DIR", raising=False)

    _run_success()

    # The autouse isolation fixture pins VNX_STATE_DIR to <tmp>/_vnx_test_data/state,
    # so the derived store is <tmp>/_vnx_test_data/unified_reports.
    derived = tmp_path / "_vnx_test_data" / "unified_reports" / f"{DISPATCH_ID}.md"
    assert derived.exists(), "success path skipped the report when VNX_REPORTS_DIR was unset"
    assert validate_body(derived.read_text(encoding="utf-8")).valid


def test_worker_authored_report_is_never_overwritten(tmp_path, monkeypatch):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))

    # A legacy filename form the worker may have used.
    worker_report = reports_dir / f"dispatch-{DISPATCH_ID}.md"
    original = "# Worker report\n\n**Model**: opus\n\n## Summary\n\nworker text\n"
    worker_report.write_text(original, encoding="utf-8")

    _run_success(model="sonnet")

    assert worker_report.read_text(encoding="utf-8") == original
    assert not (reports_dir / f"{DISPATCH_ID}.md").exists(), (
        "a second report must not be written alongside the worker's legacy-form report"
    )


def test_changes_section_lists_touched_files(tmp_path, monkeypatch):
    reports_dir = tmp_path / "unified_reports"
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))

    _run_success(touched=frozenset({"scripts/lib/foo.py"}))

    text = (reports_dir / f"{DISPATCH_ID}.md").read_text(encoding="utf-8")
    assert "scripts/lib/foo.py" in text
    assert validate_body(text).valid
