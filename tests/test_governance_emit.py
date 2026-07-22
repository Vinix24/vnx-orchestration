"""test_governance_emit.py — Unit tests for governance_emit module (Wave 7 PR-7.6).

Tests cover provider validation, atomic write patterns, concurrent safety,
and all public function contracts.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from governance_emit import (
    _validate_provider,
    emit_dispatch_receipt,
    emit_unified_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_state(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture()
def tmp_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


def _base_receipt_kwargs(state_dir):
    return dict(
        dispatch_id="test-dispatch-001",
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-4-6",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=3.5,
        token_usage={"input": 100, "output": 50, "cache_hit": 0},
        cost_usd=None,
        state_dir=state_dir,
    )


# ---------------------------------------------------------------------------
# Provider validation tests
# ---------------------------------------------------------------------------

def test_provider_field_validates_claude():
    _validate_provider("claude")


def test_provider_field_validates_codex():
    _validate_provider("codex")


def test_provider_field_validates_gemini():
    _validate_provider("gemini")


def test_provider_field_validates_litellm_deepseek():
    _validate_provider("litellm:deepseek")


def test_provider_field_validates_deepseek_harness():
    # Regression: the governed deepseek-harness lane must pass provider
    # validation so _emit_governance can write a receipt. Before the regex was
    # extended, every governed deepseek-harness dispatch raised
    # ValueError: Invalid provider 'deepseek-harness' at receipt-emit.
    _validate_provider("deepseek-harness")


def test_provider_field_validates_litellm_moonshot():
    _validate_provider("litellm:moonshot")


def test_provider_field_validates_litellm_zai():
    _validate_provider("litellm:zai")


def test_provider_field_validates_litellm_with_hyphen():
    _validate_provider("litellm:my-provider")


def test_provider_field_rejects_unknown_openai():
    with pytest.raises(ValueError, match="Invalid provider"):
        _validate_provider("openai")


def test_provider_field_rejects_litellm_with_space():
    with pytest.raises(ValueError, match="Invalid provider"):
        _validate_provider("litellm:foo bar")


def test_provider_field_rejects_empty():
    with pytest.raises(ValueError, match="Invalid provider"):
        _validate_provider("")


def test_provider_field_rejects_uppercase():
    with pytest.raises(ValueError, match="Invalid provider"):
        _validate_provider("Claude")


# ---------------------------------------------------------------------------
# Receipt emit tests
# ---------------------------------------------------------------------------

def test_emit_returns_path(tmp_state):
    path = emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
    assert isinstance(path, Path)
    assert path.exists()


def test_receipt_written_to_correct_file(tmp_state):
    emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
    receipt_path = tmp_state / "t0_receipts.ndjson"
    assert receipt_path.exists()


def test_deepseek_harness_dispatch_emits_receipt(tmp_state):
    # A governed deepseek-harness dispatch must pass provider-validation AND
    # write a receipt — this is the gap that made #765's earlier validation
    # incomplete (provider rejected at receipt-emit, no audit trail).
    kwargs = _base_receipt_kwargs(tmp_state)
    kwargs["provider"] = "deepseek-harness"
    kwargs["model"] = "deepseek-v4-pro"
    path = emit_dispatch_receipt(**kwargs)
    assert path.exists()
    data = json.loads((tmp_state / "t0_receipts.ndjson").read_text().strip().splitlines()[-1])
    assert data["provider"] == "deepseek-harness"
    assert data["model"] == "deepseek-v4-pro"


def test_receipt_json_structure(tmp_state):
    emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
    receipt_path = tmp_state / "t0_receipts.ndjson"
    line = receipt_path.read_text().strip()
    data = json.loads(line)
    assert data["dispatch_id"] == "test-dispatch-001"
    assert data["terminal_id"] == "T1"
    assert data["provider"] == "claude"
    assert data["model"] == "claude-sonnet-4-6"
    assert data["status"] == "success"
    assert data["completion_pct"] == 100


def test_receipt_includes_token_usage_when_provided(tmp_state):
    kwargs = _base_receipt_kwargs(tmp_state)
    kwargs["token_usage"] = {"input": 215, "output": 47, "cache_hit": 0}
    emit_dispatch_receipt(**kwargs)
    data = json.loads((tmp_state / "t0_receipts.ndjson").read_text().strip())
    assert data["token_usage"] == {"input": 215, "output": 47, "cache_hit": 0}


def test_receipt_cost_usd_nullable_when_provider_does_not_report(tmp_state):
    kwargs = _base_receipt_kwargs(tmp_state)
    kwargs["cost_usd"] = None
    emit_dispatch_receipt(**kwargs)
    data = json.loads((tmp_state / "t0_receipts.ndjson").read_text().strip())
    assert data["cost_usd"] is None


def test_recorded_at_timestamp_present(tmp_state):
    emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
    data = json.loads((tmp_state / "t0_receipts.ndjson").read_text().strip())
    assert "recorded_at" in data
    assert data["recorded_at"].endswith("Z")
    assert "timestamp" in data


def test_receipt_written_atomically(tmp_state, monkeypatch):
    """Verify that the NDJSON append does not write a partial line (flock held)."""
    lines_written = []

    original_open = open

    def patched_open(path, mode="r", **kwargs):
        fh = original_open(path, mode, **kwargs)
        return fh

    emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
    receipt_path = tmp_state / "t0_receipts.ndjson"
    content = receipt_path.read_text()
    for line in content.splitlines():
        if line.strip():
            obj = json.loads(line)
            lines_written.append(obj)
    assert len(lines_written) == 1
    assert lines_written[0]["dispatch_id"] == "test-dispatch-001"


def test_receipt_append_concurrent_safe(tmp_state):
    """Multiple threads appending concurrently should each write exactly one valid line."""
    errors = []
    results = []

    def write_one(i):
        try:
            kwargs = _base_receipt_kwargs(tmp_state)
            kwargs["dispatch_id"] = f"concurrent-{i:03d}"
            path = emit_dispatch_receipt(**kwargs)
            results.append(path)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_one, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent writes raised: {errors}"
    receipt_path = tmp_state / "t0_receipts.ndjson"
    lines = [l for l in receipt_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 10
    ids = {json.loads(l)["dispatch_id"] for l in lines}
    assert len(ids) == 10


def test_write_failure_raises_runtimeerror(tmp_state):
    """If the receipt file can't be written, RuntimeError is raised."""
    receipt_path = tmp_state / "t0_receipts.ndjson"
    receipt_path.write_text("existing\n")
    receipt_path.chmod(0o444)
    try:
        with pytest.raises(RuntimeError, match="receipt write failed"):
            emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
    finally:
        receipt_path.chmod(0o644)


def test_provider_validation_raises_before_write(tmp_state):
    kwargs = _base_receipt_kwargs(tmp_state)
    kwargs["provider"] = "bad-provider"
    with pytest.raises(ValueError, match="Invalid provider"):
        emit_dispatch_receipt(**kwargs)
    assert not (tmp_state / "t0_receipts.ndjson").exists()


# ---------------------------------------------------------------------------
# ADR-035 §7.1/§9 PR-3 — shared append primitive
# ---------------------------------------------------------------------------

def test_t30_stamps_event_type_task_complete_on_success(tmp_state):
    """T30: emit_dispatch_receipt (Path 1) stamps event_type='task_complete'
    for a status='success' receipt — never a different event_type per outcome."""
    kwargs = _base_receipt_kwargs(tmp_state)
    kwargs["status"] = "success"
    emit_dispatch_receipt(**kwargs)
    data = json.loads((tmp_state / "t0_receipts.ndjson").read_text().strip())
    assert data["event_type"] == "task_complete"
    assert data["status"] == "success"


def test_t30_stamps_event_type_task_complete_on_failure(tmp_state):
    """T30: emit_dispatch_receipt (Path 1) stamps event_type='task_complete'
    for a status='failed' receipt too — status carries the outcome, event_type
    only marks that a terminal outcome was reached (outcome_signals.py convention)."""
    kwargs = _base_receipt_kwargs(tmp_state)
    kwargs["dispatch_id"] = "test-dispatch-failed-001"
    kwargs["status"] = "failed"
    emit_dispatch_receipt(**kwargs)
    data = json.loads((tmp_state / "t0_receipts.ndjson").read_text().strip())
    assert data["event_type"] == "task_complete"
    assert data["status"] == "failed"


def test_emit_dispatch_receipt_uses_shared_append_lock_file(tmp_state):
    """ADR-035 §7.1 point 1: Path 1 now takes the same append_receipt.lock
    Path 2 uses, not a separate lock on the receipts file handle directly —
    the mechanism that makes a concurrent Path-1/Path-2 write genuinely
    serialize against each other (closes BLOCKING-1's dual-lock race)."""
    emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
    assert (tmp_state / "append_receipt.lock").exists()


def test_emit_dispatch_receipt_no_longer_has_inline_write(tmp_state):
    """ADR-035 §9 PR-3 acceptance criterion 1: emit_dispatch_receipt has no
    inline open()/flock() on t0_receipts.ndjson of its own anymore."""
    import inspect
    import governance_emit as ge
    source = inspect.getsource(ge.emit_dispatch_receipt)
    assert "fcntl" not in source
    assert "flock" not in source


def test_emit_dispatch_receipt_rejects_empty_dispatch_id(tmp_state):
    """ADR-035 §7.1 point 3 / BLOCKING-3: emit_dispatch_receipt now runs
    through the shared validator (_validate_receipt) before append — an
    empty dispatch_id is rejected the same way Path 2 already rejects it,
    proving the validator binds to Path 1 too (never lands durably)."""
    kwargs = _base_receipt_kwargs(tmp_state)
    kwargs["dispatch_id"] = ""
    with pytest.raises(RuntimeError, match="dispatch_id"):
        emit_dispatch_receipt(**kwargs)
    assert not (tmp_state / "t0_receipts.ndjson").exists()


# ---------------------------------------------------------------------------
# Durability (fsync) tests
# ---------------------------------------------------------------------------

def test_emit_is_durable_and_fsyncs_the_append(tmp_state, monkeypatch):
    """The append is fsync'd before the lock releases, and the record is on disk.

    ADR-035 §7.1: the fsync now happens inside the shared append primitive
    (append_receipt_internals.idempotency._write_receipt_under_lock), which
    lazy-imports ndjson_io.fsync_fileno on each call — patch it there rather
    than on governance_emit, which no longer holds its own reference.
    """
    import ndjson_io

    contexts = []
    real_fsync = ndjson_io.fsync_fileno

    def spy(fh, **kwargs):
        contexts.append(kwargs.get("context"))
        return real_fsync(fh, **kwargs)

    monkeypatch.setattr(ndjson_io, "fsync_fileno", spy)

    emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))

    assert contexts, "emit must fsync the receipt append for durability"
    assert any("test-dispatch-001" in (c or "") for c in contexts)

    data = json.loads((tmp_state / "t0_receipts.ndjson").read_text().strip())
    assert data["dispatch_id"] == "test-dispatch-001"


def test_emit_survives_fsync_failure(tmp_state, monkeypatch):
    """An fsync failure (fs without fsync support) must degrade, not break the write."""
    import ndjson_io

    def boom(_fd):
        raise OSError("fsync not supported on this filesystem")

    monkeypatch.setattr(ndjson_io.os, "fsync", boom)

    path = emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
    assert path.exists()
    data = json.loads((tmp_state / "t0_receipts.ndjson").read_text().strip())
    assert data["dispatch_id"] == "test-dispatch-001"


# ---------------------------------------------------------------------------
# Unified report tests
# ---------------------------------------------------------------------------

def _base_report_kwargs(data_dir):
    return dict(
        dispatch_id="test-dispatch-002",
        terminal_id="T2",
        provider="litellm:deepseek",
        instruction="Do the thing",
        response_text="Done.",
        findings=[],
        duration_seconds=4.2,
        data_dir=data_dir,
    )


def test_unified_report_markdown_format(tmp_data):
    path = emit_unified_report(**_base_report_kwargs(tmp_data))
    content = path.read_text()
    assert "# Dispatch test-dispatch-002" in content
    assert "## Instruction" in content
    assert "## Response" in content
    assert "## Findings" in content


def test_unified_report_includes_provider_in_header(tmp_data):
    path = emit_unified_report(**_base_report_kwargs(tmp_data))
    content = path.read_text()
    assert "Provider: litellm:deepseek" in content


def test_unified_report_created_at_correct_path(tmp_data):
    emit_unified_report(**_base_report_kwargs(tmp_data))
    expected = tmp_data / "unified_reports" / "test-dispatch-002.md"
    assert expected.exists()


def test_unified_report_returns_path(tmp_data):
    path = emit_unified_report(**_base_report_kwargs(tmp_data))
    assert isinstance(path, Path)
    assert path.exists()


def test_unified_report_idempotent(tmp_data):
    kwargs = _base_report_kwargs(tmp_data)
    path1 = emit_unified_report(**kwargs)
    original_mtime = path1.stat().st_mtime
    kwargs["response_text"] = "Different response"
    path2 = emit_unified_report(**kwargs)
    assert path1 == path2
    assert path2.stat().st_mtime == original_mtime


def test_unified_report_includes_response_text(tmp_data):
    kwargs = _base_report_kwargs(tmp_data)
    kwargs["response_text"] = "My specific response"
    path = emit_unified_report(**kwargs)
    assert "My specific response" in path.read_text()


def test_unified_report_includes_findings(tmp_data):
    kwargs = _base_report_kwargs(tmp_data)
    kwargs["findings"] = [{"severity": "warning", "message": "Something smells"}]
    path = emit_unified_report(**kwargs)
    assert "Something smells" in path.read_text()
    assert "WARNING" in path.read_text()
