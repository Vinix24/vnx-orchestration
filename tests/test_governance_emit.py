"""test_governance_emit.py — Unit tests for governance_emit module (Wave 7 PR-7.6).

Tests cover provider validation, atomic write patterns, concurrent safety,
and all public function contracts.
"""

from __future__ import annotations

import json
import multiprocessing as mp
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
from ndjson_hash_chain import (
    GENESIS_HASH,
    compute_entry_hash,
    verify_chain,
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
# Durability (fsync) tests
# ---------------------------------------------------------------------------

def test_emit_is_durable_and_fsyncs_the_append(tmp_state, monkeypatch):
    """The append is fsync'd before the lock releases, and the record is on disk."""
    import governance_emit as ge

    contexts = []
    real_fsync = ge.fsync_fileno

    def spy(fh, **kwargs):
        contexts.append(kwargs.get("context"))
        return real_fsync(fh, **kwargs)

    monkeypatch.setattr(ge, "fsync_fileno", spy)

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


# ---------------------------------------------------------------------------
# Hash-chain receipt flag (VNX_RECEIPT_HASH_CHAIN)
# ---------------------------------------------------------------------------


def _emit_worker(state_dir_str: str, worker_id: int) -> None:
    """Subprocess body: enable receipt chaining and append one receipt."""
    import os as _os
    import sys as _sys

    _os.environ["VNX_RECEIPT_HASH_CHAIN"] = "1"
    scripts_lib = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
    if scripts_lib not in _sys.path:
        _sys.path.insert(0, scripts_lib)
    from governance_emit import emit_dispatch_receipt

    state_dir = Path(state_dir_str)
    emit_dispatch_receipt(
        dispatch_id=f"worker-{worker_id:03d}",
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-4-6",
        pr_id=None,
        status="success",
        completion_pct=100,
        risk=0.0,
        findings=[],
        duration_seconds=1.0,
        token_usage={"input": 10, "output": 5},
        cost_usd=None,
        state_dir=state_dir,
    )


def _read_receipt_entries(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_hash_chain_flag_off_no_prev_hash(tmp_state, monkeypatch):
    """Default OFF: emit must not add prev_hash and must remain byte-identical
    to the pre-feature output shape."""
    monkeypatch.delenv("VNX_RECEIPT_HASH_CHAIN", raising=False)
    emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))

    receipt_path = tmp_state / "t0_receipts.ndjson"
    content = receipt_path.read_text()
    entry = json.loads(content.strip())
    assert "prev_hash" not in entry
    # Re-serializing the parsed entry with the same compact separators must
    # reproduce the written bytes exactly.
    assert content == json.dumps(entry, separators=(",", ":")) + "\n"


def test_hash_chain_flag_off_explicit_false(tmp_state, monkeypatch):
    monkeypatch.setenv("VNX_RECEIPT_HASH_CHAIN", "0")
    emit_dispatch_receipt(**_base_receipt_kwargs(tmp_state))
    entry = json.loads((tmp_state / "t0_receipts.ndjson").read_text().strip())
    assert "prev_hash" not in entry


def test_hash_chain_flag_on_links_and_verifies(tmp_state, monkeypatch):
    """Flag ON: each receipt carries prev_hash; the ledger verifies intact."""
    monkeypatch.setenv("VNX_RECEIPT_HASH_CHAIN", "1")
    receipt_path = tmp_state / "t0_receipts.ndjson"

    for i in range(6):
        kwargs = _base_receipt_kwargs(tmp_state)
        kwargs["dispatch_id"] = f"chain-{i:03d}"
        emit_dispatch_receipt(**kwargs)

    entries = _read_receipt_entries(receipt_path)
    assert len(entries) == 6
    assert entries[0]["prev_hash"] == GENESIS_HASH
    for idx in range(1, len(entries)):
        assert entries[idx]["prev_hash"] == compute_entry_hash(entries[idx - 1])

    is_valid, violations, status = verify_chain(receipt_path)
    assert is_valid, f"chain should verify, violations: {violations}"
    assert status == "verified"


def test_hash_chain_transition_tolerates_unchained_prefix(tmp_state, monkeypatch):
    """Enabling chaining on an existing unchained ledger must not rewrite
    history; it starts a fresh chain from GENESIS at the switch point."""
    receipt_path = tmp_state / "t0_receipts.ndjson"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "dispatch_id": "legacy-dispatch",
        "terminal_id": "T1",
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "status": "success",
        "completion_pct": 100,
        "risk": 0.0,
        "duration_seconds": 2.0,
        "token_usage": {"input": 1, "output": 1},
        "cost_usd": None,
        "findings": [],
        "pr_id": None,
        "report_path": None,
        "events_path": None,
        "timestamp": "2026-01-01T00:00:00Z",
        "recorded_at": "2026-01-01T00:00:00Z",
    }
    with receipt_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(legacy, separators=(",", ":")) + "\n")

    monkeypatch.setenv("VNX_RECEIPT_HASH_CHAIN", "1")
    kwargs = _base_receipt_kwargs(tmp_state)
    kwargs["dispatch_id"] = "chained-after-legacy"
    emit_dispatch_receipt(**kwargs)

    entries = _read_receipt_entries(receipt_path)
    assert len(entries) == 2
    assert "prev_hash" not in entries[0], "existing receipts must not be rewritten"
    assert entries[1]["prev_hash"] == GENESIS_HASH

    is_valid, violations, status = verify_chain(receipt_path)
    assert is_valid, f"transition ledger should verify: {violations}"
    assert status == "verified"


def test_hash_chain_flag_on_detects_tamper(tmp_state, monkeypatch):
    """A tampered chained receipt must cause verify_chain to fail."""
    monkeypatch.setenv("VNX_RECEIPT_HASH_CHAIN", "1")
    for i in range(2):
        kwargs = _base_receipt_kwargs(tmp_state)
        kwargs["dispatch_id"] = f"tamper-{i}"
        emit_dispatch_receipt(**kwargs)

    receipt_path = tmp_state / "t0_receipts.ndjson"
    lines = receipt_path.read_text().splitlines()
    first = json.loads(lines[0])
    first["status"] = "tampered"
    lines[0] = json.dumps(first, separators=(",", ":"))
    receipt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    is_valid, violations, status = verify_chain(receipt_path)
    assert is_valid is False
    assert status == "broken"
    assert len(violations) >= 1
    assert any(v.get("line_number") == 2 for v in violations)


@pytest.mark.parametrize("n_workers", [8, 16])
def test_hash_chain_concurrent_appends_no_fork(tmp_state, n_workers):
    """Concurrent writers with chaining ON must serialize read-tail + stamp +
    append under the one exclusive lock and produce a single unbroken chain."""
    receipt_path = tmp_state / "t0_receipts.ndjson"

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_emit_worker, args=(str(tmp_state), wid))
        for wid in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker exited non-zero: {p.exitcode}"

    entries = _read_receipt_entries(receipt_path)
    assert len(entries) == n_workers, "every worker must append exactly once"

    is_valid, violations, status = verify_chain(receipt_path)
    assert is_valid, f"CHAIN FORKED under concurrency: {violations}"
    assert status == "verified"

    # Exactly one GENESIS_HASH, on line 1.
    assert entries[0]["prev_hash"] == GENESIS_HASH
    genesis_count = sum(1 for e in entries if e.get("prev_hash") == GENESIS_HASH)
    assert genesis_count == 1, "fork: GENESIS_HASH appears more than once"

    # No duplicate prev_hash (a fork would share a parent).
    prev_hashes = [e["prev_hash"] for e in entries]
    assert len(prev_hashes) == len(set(prev_hashes)), "fork: duplicate prev_hash"

    # Each prev_hash matches the prior entry's canonical body hash.
    for idx in range(1, len(entries)):
        assert entries[idx]["prev_hash"] == compute_entry_hash(entries[idx - 1])
