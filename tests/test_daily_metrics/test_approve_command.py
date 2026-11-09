"""Tests for ``daily_metrics.py approve`` (doorloop PR-12a, decisions 26 and 27).

Everything lives under ``tmp_path`` and the clock is fixed with ``--now``. The
terminal cases run the real CLI on a pty; the non-terminal case uses a pipe.
"""

from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "daily_metrics.py"
NOW = "2026-11-09T09:00:00Z"
APPROVAL_DAY = date(2026, 11, 9)
PID = "abc123def456"
LEDGER = "proposals.ndjson"


def _day_doc(day, value):
    record = {"value": value, "source": "t", "sample_size": 1}
    return {"schema": 3, "project_id": "p", "day": day.isoformat(), "status": "final",
            "metrics": {"merges": record}}


def _store(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    for offset in range(1, 9):
        day = APPROVAL_DAY - timedelta(days=offset)
        (root / f"{day.isoformat()}.json").write_text(
            json.dumps(_day_doc(day, float(offset))), encoding="utf-8")
    proposed = {"event": "proposed", "proposal_id": PID, "project_id": "p", "key": "merges:x",
                "metric_id": "merges", "seat": None, "metric_day": "2026-11-08",
                "value": 10.0, "baseline": 5.0}
    (root / LEDGER).write_text(json.dumps(proposed, sort_keys=True) + "\n", encoding="utf-8")
    return root


def _args(root, proposal_id=PID, *extra):
    return [sys.executable, str(CLI), "approve", proposal_id, "--action", "tune the gate",
            "--project-id", "p", "--store", str(root), "--now", NOW, *extra]


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("VNX_AGENT_ID", "VNX_PROJECT_ID")}
    env.update(extra)
    return env


def _pty_run(argv, typed, env=None):
    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                            env=env or _env(), text=True)
    os.close(slave)
    time.sleep(0.5)
    try:
        os.write(master, (typed + "\n").encode())
    except OSError:
        pass  # the child refused before it read a line and already closed the terminal
    try:
        _, err = proc.communicate(timeout=60)
    finally:
        os.close(master)
    return proc.returncode, err


def _events(root, kind):
    lines = (root / LEDGER).read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["event"] == kind]


def _ledger():
    from daily_metrics import proposal_ledger

    return proposal_ledger


def test_approve_command_is_registered():
    result = subprocess.run([sys.executable, str(CLI), "approve", "--help"],
                            capture_output=True, text=True, env=_env())
    assert result.returncode == 0


def test_refuses_without_a_terminal(tmp_path):
    root = _store(tmp_path)
    before = (root / LEDGER).read_bytes()
    result = subprocess.run(_args(root), input=PID + "\n", capture_output=True, text=True,
                            env=_env())
    assert result.returncode == 2
    assert (root / LEDGER).read_bytes() == before


def test_refuses_when_an_agent_id_is_set(tmp_path):
    root = _store(tmp_path)
    code, _ = _pty_run(_args(root), PID, _env(VNX_AGENT_ID="t1"))
    assert code == 2
    assert _events(root, "approved") == []


def test_wrong_typed_id_is_refused(tmp_path):
    root = _store(tmp_path)
    code, _ = _pty_run(_args(root), "not-the-id")
    assert code == 2
    assert _events(root, "approved") == []


def test_right_id_writes_one_approved_event(tmp_path):
    root = _store(tmp_path)
    code, err = _pty_run(_args(root), PID)
    assert code == 0, err
    (event,) = _events(root, "approved")
    assert event["approved_by"] == "operator"
    assert event["evidence"]["tty"] is True
    assert event["action"] == "tune the gate"
    assert event["approval_day"] == "2026-11-09"
    assert event["schema"] == 3
    from daily_metrics.baseline import baseline

    window = {APPROVAL_DAY - timedelta(days=o): _day_doc(APPROVAL_DAY - timedelta(days=o), float(o))
              for o in range(1, 8)}
    expected = baseline("merges", APPROVAL_DAY, window)
    assert event["frozen_baseline"]["value"] == expected.value
    assert event["frozen_baseline"]["sample_size"] == expected.sample_size


@pytest.mark.parametrize("flag", [["--yes"], ["--operator", "x"]])
def test_name_and_yes_flags_do_not_exist(tmp_path, flag):
    root = _store(tmp_path)
    result = subprocess.run(_args(root, PID, *flag), capture_output=True, text=True, env=_env())
    assert result.returncode == 2
    assert _events(root, "approved") == []


def test_second_approval_is_refused(tmp_path):
    root = _store(tmp_path)
    assert _pty_run(_args(root), PID)[0] == 0
    assert _pty_run(_args(root), PID)[0] == 2
    assert len(_events(root, "approved")) == 1


def test_unknown_proposal_writes_nothing(tmp_path):
    root = _store(tmp_path)
    before = (root / LEDGER).read_bytes()
    assert _pty_run(_args(root, "ffffffffffff"), "ffffffffffff")[0] == 2
    assert (root / LEDGER).read_bytes() == before


def test_append_approval_refusals_leave_the_file_untouched(tmp_path):
    ledger = _ledger()
    root = _store(tmp_path)
    path = root / LEDGER
    approved = {"event": "approved", "proposal_id": PID}

    def refused(event):
        before = path.read_bytes()
        with pytest.raises(ledger.ApprovalRefused) as info:
            ledger.append_approval(path, event)
        assert path.read_bytes() == before
        return info.value.reason

    assert refused({"event": "approved", "proposal_id": "other"}) == "unknown_proposal"
    assert refused({"event": "approved"}) == "unknown_proposal"
    ledger.append_approval(path, approved)
    assert refused(approved) == "already_approved"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "verdict", "proposal_id": PID}) + "\n")
    assert refused(approved) == "already_judged"
