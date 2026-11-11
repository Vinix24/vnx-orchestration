"""dlv-d7-verdict-ledger: every verdict is in the append-only NDJSON ledger before the snapshot."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts/lib", "scripts"):
    sys.path.insert(0, str(VNX_ROOT / sub))

import learning_ledger_gate as gate  # noqa: E402

NDJSON = "learning_ledger_verdict.ndjson"
SNAPSHOT = gate.VERDICT_FILENAME
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)


def _make(status: str, code: str) -> dict:
    return gate._verdict(NOW, "proj", "a" * 40, status, code, f"reason {code}")


def _lines(state_dir: Path) -> list:
    return [json.loads(x) for x in (state_dir / NDJSON).read_text().splitlines()]


def _boom(*args, **kwargs):
    raise OSError("injected failure")


def test_two_verdicts_two_lines_and_snapshot_is_last(tmp_path):
    ok, refused = _make("ok", "ok"), _make("refused", "unmeasured")
    gate.write_verdict(tmp_path, ok)
    gate.write_verdict(tmp_path, refused)
    lines = _lines(tmp_path)
    assert [x["status"] for x in lines] == ["ok", "refused"]
    for line, verdict in zip(lines, (ok, refused)):
        assert line["event_type"] == "learning_ledger_verdict"
        assert {k: line[k] for k in verdict} == verdict
    stored = json.loads((tmp_path / SNAPSHOT).read_text())
    assert stored == refused and set(stored) == gate._FIELDS
    assert gate.read_verdict(tmp_path, now=NOW) == refused


def test_line_is_in_the_ledger_before_the_snapshot_write(tmp_path, monkeypatch):
    seen = []
    real = gate.atomic_write_json

    def spy(path, data):
        seen.append(len(_lines(tmp_path)) if (tmp_path / NDJSON).exists() else 0)
        return real(path, data)

    monkeypatch.setattr(gate, "atomic_write_json", spy)
    gate.write_verdict(tmp_path, _make("ok", "ok"))
    assert seen == [1]


def test_failing_append_leaves_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "audit_event_append", _boom, raising=False)
    with pytest.raises(OSError):
        gate.write_verdict(tmp_path, _make("ok", "ok"))
    assert not (tmp_path / SNAPSHOT).exists()


def test_failing_append_keeps_existing_snapshot_byte_identical(tmp_path, monkeypatch):
    gate.write_verdict(tmp_path, _make("ok", "ok"))
    before = (tmp_path / SNAPSHOT).read_bytes()
    monkeypatch.setattr(gate, "audit_event_append", _boom, raising=False)
    with pytest.raises(OSError):
        gate.write_verdict(tmp_path, _make("refused", "unmeasured"))
    assert (tmp_path / SNAPSHOT).read_bytes() == before


def test_failing_snapshot_write_keeps_the_ledger_line(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "atomic_write_json", _boom)
    with pytest.raises(OSError):
        gate.write_verdict(tmp_path, _make("ok", "ok"))
    assert [x["status"] for x in _lines(tmp_path)] == ["ok"]
    assert not (tmp_path / SNAPSHOT).exists()
