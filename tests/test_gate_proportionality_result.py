"""R4c advisory route through the real artifact, receipt and round writers."""
import json
from functools import partial

import pytest

from test_gate_verdict_without_findings_writer import (
    _ledger_lines, _on_disk, _run, _stdout, _verdict_text,
)

import gate_recorder
import gate_status
from governance_receipts import emit_governance_receipt


@pytest.mark.parametrize("gate", ["codex_gate", "deepseek_gate"])
@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_proportionality_advisory_preserves_the_recorded_verdict(tmp_path, monkeypatch, gate, verdict):
    state_dir = tmp_path / "state"
    env = {
        "requests_dir": state_dir / "review_gates/requests",
        "results_dir": state_dir / "review_gates/results",
        "reports_dir": tmp_path / "reports",
    }
    for directory in env.values():
        directory.mkdir(parents=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    receipts = tmp_path / "receipts.ndjson"
    monkeypatch.setattr(gate_recorder, "emit_governance_receipt",
                        partial(emit_governance_receipt, receipts_file=str(receipts)))
    message = "unjustified_mechanism: O1 adapter has no current second caller or violated guarantee"
    finding = {"severity": "info", "file_path": "scripts/a.py", "line": 3, "message": message}
    result = _run(env, gate, _stdout(gate, _verdict_text(verdict, [finding])))

    stored = _on_disk(env, gate)
    assert stored == json.loads(json.dumps(result))
    assert stored["status"] == "completed"
    assert gate_status.is_pass(stored)[0] is (verdict == "pass")
    assert stored["advisory_findings"] == [finding]
    if verdict == "pass":
        assert stored["blocking_findings"] == []
        assert stored["findings"] == [finding]
        assert "verdict_without_blocking_finding" not in json.dumps(stored)
    else:
        [blocking] = stored["blocking_findings"]
        assert blocking["message"].startswith("verdict_without_blocking_finding")
        assert "1 finding(s)" in blocking["message"]
    [receipt] = [json.loads(line) for line in receipts.read_text().splitlines()
                 if json.loads(line)["event_type"] == "review_gate_result"]
    assert receipt["status"] == ("completed" if verdict == "pass" else "failed")
    [round_record] = [json.loads(line) for line in _ledger_lines(env)]
    assert round_record["verdict"] == verdict
