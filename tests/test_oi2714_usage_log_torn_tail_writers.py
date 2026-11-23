"""OI-2714: every writer of intelligence_usage.ndjson closes a torn tail before it appends."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import qi_learning_approval as qla
import learning_loop as ll
from gather_intelligence import T0IntelligenceGatherer

FRAGMENT = b'{"event_type":"x","a"'
CLI = ROOT / "scripts" / "append_usage_event.py"
ID = "learn-" + "0" * 64


def _torn(tmp_path: Path) -> Path:
    log = tmp_path / "intelligence_usage.ndjson"
    log.write_bytes(b'{"event_type":"head"}\n' + FRAGMENT)
    return log


def _assert_repaired(tmp_path: Path, event_type: str) -> None:
    assert qla._claimed_in_log(tmp_path, ID) is False
    lines = (tmp_path / "intelligence_usage.ndjson").read_bytes().split(b"\n")
    assert lines[0] == b'{"event_type":"head"}'
    assert lines[1] == FRAGMENT
    marker = json.loads(lines[2])
    assert marker["event_type"] == qla.TORN_TAIL_MARKER
    assert marker["sha256"] == hashlib.sha256(FRAGMENT).hexdigest()
    assert marker["length"] == len(FRAGMENT)
    assert json.loads(lines[3])["event_type"] == event_type
    assert lines[4] == b""


def test_gather_intelligence_writer(tmp_path):
    log = _torn(tmp_path)
    obj = object.__new__(T0IntelligenceGatherer)
    with patch.object(T0IntelligenceGatherer, "_usage_log_path", return_value=log):
        obj._append_usage_event({"event_type": "pattern_offered"})
    _assert_repaired(tmp_path, "pattern_offered")


def test_learning_loop_confidence_change_writer(tmp_path):
    _torn(tmp_path)
    loop = ll.LearningLoop.__new__(ll.LearningLoop)
    loop._persist = True
    with patch("learning_loop.ensure_env", return_value={"VNX_STATE_DIR": str(tmp_path)}):
        loop._log_confidence_change("p1", "src", 0.5, 0.6)
    _assert_repaired(tmp_path, "confidence_change")


def test_learning_loop_archival_decision_writer(tmp_path):
    _torn(tmp_path)
    ll._emit_archival_decision_event(
        tmp_path, pattern_id="p1", decision="archive", approved_by="operator", approval_id="a1",
        reason="r", action="archive", source_table="success_patterns", db_applied=True)
    _assert_repaired(tmp_path, "archival_decision")


def test_cli_repairs_torn_tail(tmp_path):
    _torn(tmp_path)
    done = subprocess.run([sys.executable, str(CLI), str(tmp_path)], input='{"event_type":"injection"}',
                          text=True, capture_output=True)
    assert done.returncode == 0, done.stderr
    _assert_repaired(tmp_path, "injection")


@pytest.mark.parametrize("payload", ["[1,2]", "not json", "", '"str"', "null"])
def test_cli_refuses_non_object(tmp_path, payload):
    log = _torn(tmp_path)
    before = log.read_bytes()
    done = subprocess.run([sys.executable, str(CLI), str(tmp_path)], input=payload, text=True, capture_output=True)
    assert done.returncode == 2
    assert done.stderr
    assert log.read_bytes() == before


def test_cli_refuses_missing_state_dir_argument():
    done = subprocess.run([sys.executable, str(CLI)], input="{}", text=True, capture_output=True)
    assert done.returncode == 2
