"""Tests for scripts/migrate_phase3_envelope.py (Phase 6 P3).

Covers:
- Dry-run mode prints without writing
- Live run re-stamps missing envelope fields
- Idempotency: running twice produces identical output
- Records with existing envelope fields are not overwritten
- Malformed JSON lines are preserved unchanged
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from migrate_phase3_envelope import migrate, _stamp_line, _needs_stamp, _build_envelope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ndjson(state_dir: Path, filename: str, records: list[dict]) -> Path:
    path = state_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, separators=(",", ":")) for r in records]
    path.write_text("\n".join(lines) + "\n")
    return path


def _read_ndjson(path: Path) -> list[dict]:
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# _needs_stamp
# ---------------------------------------------------------------------------

class TestNeedsStamp:
    def test_returns_true_when_all_missing(self):
        assert _needs_stamp({"event": "x"}) is True

    def test_returns_false_when_all_present(self):
        assert _needs_stamp({
            "project_id": "vnx-dev",
            "operator_id": "vince",
            "orchestrator_id": "dev-t0",
            "agent_id": "t1",
        }) is False

    def test_returns_true_when_partial(self):
        assert _needs_stamp({"project_id": "vnx-dev"}) is True


# ---------------------------------------------------------------------------
# _stamp_line
# ---------------------------------------------------------------------------

class TestStampLine:
    def test_adds_missing_fields(self):
        record = {"event": "dispatch_created", "dispatch_id": "d1"}
        envelope = {"project_id": "vnx-dev", "operator_id": "vince"}
        out = _stamp_line(record, envelope)
        assert out["project_id"] == "vnx-dev"
        assert out["operator_id"] == "vince"
        assert out["event"] == "dispatch_created"

    def test_does_not_overwrite_existing_fields(self):
        record = {"project_id": "mc", "event": "x"}
        envelope = {"project_id": "vnx-dev"}
        out = _stamp_line(record, envelope)
        assert out["project_id"] == "mc"

    def test_original_not_mutated(self):
        record = {"event": "x"}
        envelope = {"project_id": "vnx-dev"}
        _stamp_line(record, envelope)
        assert "project_id" not in record


# ---------------------------------------------------------------------------
# migrate() — dry run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_does_not_modify_file(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        records = [{"event": "x", "dispatch_id": "d1"}]
        ndjson = _make_ndjson(state, "t0_receipts.ndjson", records)
        original = ndjson.read_text()

        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")
        migrate("vnx-dev", state_dir=str(state), dry_run=True)

        assert ndjson.read_text() == original

    def test_dry_run_returns_summary(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        _make_ndjson(state, "t0_receipts.ndjson", [{"event": "x", "dispatch_id": "d1"}])
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")
        summary = migrate("vnx-dev", state_dir=str(state), dry_run=True)
        assert "t0_receipts.ndjson" in summary
        assert summary["t0_receipts.ndjson"] == 1


# ---------------------------------------------------------------------------
# migrate() — live run
# ---------------------------------------------------------------------------

class TestLiveRun:
    def test_stamps_missing_envelope(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        records = [{"event": "dispatch_created", "dispatch_id": "d1"}]
        ndjson = _make_ndjson(state, "dispatch_register.ndjson", records)
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")

        migrate("vnx-dev", state_dir=str(state))

        out = _read_ndjson(ndjson)
        assert out[0]["project_id"] == "vnx-dev"
        assert out[0]["operator_id"] == "vince"

    def test_does_not_overwrite_existing_project_id(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        records = [{"event": "x", "dispatch_id": "d1", "project_id": "mc"}]
        ndjson = _make_ndjson(state, "dispatch_register.ndjson", records)
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")

        migrate("vnx-dev", state_dir=str(state))

        out = _read_ndjson(ndjson)
        assert out[0]["project_id"] == "mc"

    def test_idempotent_second_run_produces_identical_output(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        records = [{"event": "x", "dispatch_id": "d1"}]
        ndjson = _make_ndjson(state, "t0_receipts.ndjson", records)
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")

        migrate("vnx-dev", state_dir=str(state))
        after_first = ndjson.read_text()
        migrate("vnx-dev", state_dir=str(state))
        after_second = ndjson.read_text()

        assert after_first == after_second

    def test_preserves_all_existing_fields(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        records = [{"event": "x", "dispatch_id": "d1", "terminal": "T2", "status": "ok"}]
        ndjson = _make_ndjson(state, "t0_receipts.ndjson", records)
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")

        migrate("vnx-dev", state_dir=str(state))

        out = _read_ndjson(ndjson)
        assert out[0]["terminal"] == "T2"
        assert out[0]["status"] == "ok"
        assert out[0]["event"] == "x"
        assert out[0]["dispatch_id"] == "d1"

    def test_already_stamped_record_not_counted(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        fully_stamped = {
            "event": "x",
            "dispatch_id": "d1",
            "project_id": "vnx-dev",
            "operator_id": "vince",
            "orchestrator_id": "dev-t0",
            "agent_id": "t1",
        }
        _make_ndjson(state, "t0_receipts.ndjson", [fully_stamped])
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")

        summary = migrate("vnx-dev", state_dir=str(state))
        assert summary["t0_receipts.ndjson"] == 0

    def test_multiple_files_processed(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        _make_ndjson(state, "t0_receipts.ndjson", [{"event": "x", "dispatch_id": "d1"}])
        _make_ndjson(state, "dispatch_register.ndjson", [{"event": "y", "dispatch_id": "d2"}])
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")

        summary = migrate("vnx-dev", state_dir=str(state))

        assert summary["t0_receipts.ndjson"] == 1
        assert summary["dispatch_register.ndjson"] == 1

    def test_malformed_json_line_preserved(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir(parents=True)
        ndjson = state / "t0_receipts.ndjson"
        ndjson.write_text('{"event":"x","dispatch_id":"d1"}\nNOT_JSON\n')
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")

        migrate("vnx-dev", state_dir=str(state))

        lines = [l for l in ndjson.read_text().splitlines() if l.strip()]
        assert lines[1] == "NOT_JSON"

    def test_empty_state_dir_returns_empty_summary(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")
        summary = migrate("vnx-dev", state_dir=str(state))
        assert summary == {}

    def test_nonexistent_state_dir_returns_empty_summary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_OPERATOR_ID", "vince")
        summary = migrate("vnx-dev", state_dir=str(tmp_path / "no-such-dir"))
        assert summary == {}
