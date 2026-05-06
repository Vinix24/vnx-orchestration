"""Tests for Phase 6 P3 dual-write in append_receipt_internals.payload.

Verifies that after a receipt is successfully appended to the per-project path,
a mirror copy is also written to ~/.vnx-data/<project_id>/state/<receipts_file>.
Per-project path remains source-of-truth; central write is best-effort.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from append_receipt_internals.payload import _write_central_receipt_mirror


MINIMAL_RECEIPT = {
    "event_type": "task_complete",
    "status": "success",
    "dispatch_id": "test-disp-001",
    "terminal": "T1",
    "timestamp": "2026-05-06T12:00:00Z",
}


@pytest.fixture()
def receipts_file(tmp_path):
    state_dir = tmp_path / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)
    return state_dir / "t0_receipts.ndjson"


class TestWriteCentralReceiptMirror:
    def test_writes_to_central_path_when_project_id_present(self, tmp_path, receipts_file, monkeypatch):
        central_home = tmp_path / "central_home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        receipt = {**MINIMAL_RECEIPT, "project_id": "vnx-dev"}
        _write_central_receipt_mirror(receipt, receipts_file)

        central_path = central_home / ".vnx-data" / "vnx-dev" / "state" / receipts_file.name
        assert central_path.exists()
        record = json.loads(central_path.read_text().strip())
        assert record["dispatch_id"] == "test-disp-001"
        assert record["project_id"] == "vnx-dev"

    def test_noop_when_no_project_id(self, tmp_path, receipts_file, monkeypatch):
        central_home = tmp_path / "central_home2"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

        receipt = {**MINIMAL_RECEIPT}  # no project_id
        _write_central_receipt_mirror(receipt, receipts_file)

        # No central dir created
        assert not (central_home / ".vnx-data").exists()

    def test_uses_env_project_id_when_receipt_has_none(self, tmp_path, receipts_file, monkeypatch):
        central_home = tmp_path / "central_home3"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))
        monkeypatch.setenv("VNX_PROJECT_ID", "mc")

        receipt = {**MINIMAL_RECEIPT}  # no project_id in receipt
        _write_central_receipt_mirror(receipt, receipts_file)

        central_path = central_home / ".vnx-data" / "mc" / "state" / receipts_file.name
        assert central_path.exists()

    def test_does_not_write_when_central_same_as_primary(self, tmp_path, receipts_file, monkeypatch):
        # If central path resolves to the same file as primary, skip writing
        central_home = tmp_path / "central_home4"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        # Create a receipt at the "central" path and pass that as the primary
        state = central_home / ".vnx-data" / "vnx-dev" / "state"
        state.mkdir(parents=True)
        central_receipts = state / "t0_receipts.ndjson"

        # Simulate the primary write having already happened (1 line on disk)
        receipt = {**MINIMAL_RECEIPT, "project_id": "vnx-dev"}
        import json
        central_receipts.write_text(json.dumps(receipt, separators=(",", ":")) + "\n")

        # Mirror call should detect same path and not double-write
        _write_central_receipt_mirror(receipt, central_receipts)

        # File still has exactly 1 line — no double-write
        lines = [l for l in central_receipts.read_text().splitlines() if l.strip()]
        assert len(lines) == 1

    def test_never_raises_on_write_failure(self, tmp_path, receipts_file, monkeypatch):
        unwriteable = tmp_path / "unwriteable_home"
        unwriteable.mkdir()
        unwriteable.chmod(0o555)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: unwriteable))

        receipt = {**MINIMAL_RECEIPT, "project_id": "vnx-dev"}
        try:
            _write_central_receipt_mirror(receipt, receipts_file)
        finally:
            unwriteable.chmod(0o755)

    def test_multiple_receipts_accumulate(self, tmp_path, receipts_file, monkeypatch):
        central_home = tmp_path / "central_home5"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        for i in range(3):
            receipt = {**MINIMAL_RECEIPT, "project_id": "vnx-dev", "dispatch_id": f"disp-{i}"}
            _write_central_receipt_mirror(receipt, receipts_file)

        central_path = central_home / ".vnx-data" / "vnx-dev" / "state" / receipts_file.name
        lines = [l for l in central_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 3

    def test_receipt_content_preserved_exactly(self, tmp_path, receipts_file, monkeypatch):
        central_home = tmp_path / "central_home6"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))

        receipt = {
            **MINIMAL_RECEIPT,
            "project_id": "vnx-dev",
            "operator_id": "vince",
            "orchestrator_id": "dev-t0",
            "agent_id": "t1",
        }
        _write_central_receipt_mirror(receipt, receipts_file)

        central_path = central_home / ".vnx-data" / "vnx-dev" / "state" / receipts_file.name
        record = json.loads(central_path.read_text().strip())
        assert record["operator_id"] == "vince"
        assert record["orchestrator_id"] == "dev-t0"
        assert record["agent_id"] == "t1"


class TestSchemaCompatibility:
    """Old-format receipts (no envelope) must be tolerated by the central mirror."""

    def test_old_format_receipt_written_without_error(self, tmp_path, receipts_file, monkeypatch):
        central_home = tmp_path / "central_home_compat"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: central_home))
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")

        # Old format: no project_id / identity fields
        old_receipt = {
            "event_type": "task_complete",
            "status": "success",
            "dispatch_id": "old-disp-001",
            "terminal": "T1",
        }
        _write_central_receipt_mirror(old_receipt, receipts_file)

        central_path = central_home / ".vnx-data" / "vnx-dev" / "state" / receipts_file.name
        assert central_path.exists()
        record = json.loads(central_path.read_text().strip())
        assert record["event_type"] == "task_complete"
