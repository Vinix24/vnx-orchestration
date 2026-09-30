"""fabric-state-herstel D4b: open outcomes instead of the receipt byte cursor.

A dispatch whose outcome is reject or investigate is an open point until a T0
records a decision about it in t0_decision_log.jsonl. The index lists those
points; the active-drain moves a decided dispatch out of dispatches/active/.
Reading consumes nothing, so two T0 sessions see the same list.
ADR-007: every scenario carries a second project with a colliding dispatch id.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
for _p in (_SCRIPTS, _SCRIPTS / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import open_outcomes as oo
import receipt_query as rq
from build_t0_state import _build_open_outcomes, _build_t0_index
from check_active_drain import drain_active
from ledger_health import STATUS_FINDING, STATUS_OK, check_open_outcomes

PROJECT = "proj-a"
OTHER = "proj-b"
GOOD = {"method": "pytest", "tests_run": 3, "tests_passed": 3, "tests_failed": 0}
TS = "2026-09-29T10:00:00Z"


@pytest.fixture(autouse=True)
def _project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)


def _a(did: str, status: str, project: str = PROJECT, ts: str = TS, **kw: Any) -> Dict[str, Any]:
    return {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
            "status": status, "timestamp": ts, "project_id": project, **kw}


def _b(did: str, status: str = "done", verification: Any = None, **kw: Any) -> Dict[str, Any]:
    return _a(did, status, report_file=f"{did}.md", verification=verification or GOOD, **kw)


def _reject(did: str, **kw: Any) -> List[Dict[str, Any]]:
    return [_a(did, "failure", **kw)]


def _investigate(did: str, **kw: Any) -> List[Dict[str, Any]]:
    return [_a(did, "success", **kw), _b(did, "unknown", {"method": "unknown"}, **kw)]


def _accept(did: str, **kw: Any) -> List[Dict[str, Any]]:
    return [_a(did, "success", **kw), _b(did, **kw)]


def _ledger(state_dir: Path, receipts: List[Dict[str, Any]]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / oo.LEDGER_NAME
    path.write_text("".join(json.dumps(r) + "\n" for r in receipts), encoding="utf-8")
    return path


def _decide(state_dir: Path, did: str, decision: str, project: str = PROJECT) -> None:
    oo.record_outcome_decision(state_dir / oo.DECISION_LOG_NAME, dispatch_id=did,
                               project_id=project, decision=decision, reason="reviewed")


def _open_ids(state_dir: Path, project: str = PROJECT) -> List[str]:
    section = oo.build_open_outcomes(state_dir, project_id=project, limit=None)
    assert section["available"], section
    return sorted(i["dispatch_id"] for i in section["items"])


# ---------------------------------------------------------------------------
# Klaar 1: a reject without a decision is open, and leaves after a decision
# ---------------------------------------------------------------------------

def test_reject_and_investigate_without_decision_are_open(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-rej") + _investigate("d-inv") + _accept("d-ok"))
    assert _open_ids(state) == ["d-inv", "d-rej"]


def test_decision_takes_a_reject_off_the_list(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-rej") + _investigate("d-inv"))
    _decide(state, "d-rej", "reject")
    assert _open_ids(state) == ["d-inv"]
    _decide(state, "d-inv", "accept")
    assert _open_ids(state) == []


def test_index_carries_open_outcomes_and_drops_a_decided_one(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-rej"))
    index = _build_t0_index({"open_outcomes": _build_open_outcomes(state, PROJECT)})
    section = index.get("open_outcomes") or {}
    assert [i["dispatch_id"] for i in section.get("items", [])] == ["d-rej"]
    assert section.get("total") == 1
    _decide(state, "d-rej", "reject")
    index = _build_t0_index({"open_outcomes": _build_open_outcomes(state, PROJECT)})
    assert (index.get("open_outcomes") or {}).get("items") == []


def test_index_holds_at_most_ten_items_and_counts_the_rest(tmp_path: Path) -> None:
    state = tmp_path / "state"
    receipts: List[Dict[str, Any]] = []
    for n in range(25):
        receipts += _reject(f"20260929-long-dispatch-name-for-size-{n:02d}",
                            ts=f"2026-09-29T10:{n:02d}:00Z")
    _ledger(state, receipts)
    index = _build_t0_index({"open_outcomes": _build_open_outcomes(state, PROJECT)})
    section = index.get("open_outcomes") or {}
    assert section.get("total") == 25 and section.get("more") == 15
    assert len(section.get("items", [])) == 10
    # newest first: the latest receipt heads the list
    assert section["items"][0]["dispatch_id"].endswith("-24")
    assert len(json.dumps(section, separators=(",", ":"))) < 2048


def test_investigate_before_the_epoch_is_not_listed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _investigate("d-old", ts="2026-09-01T10:00:00Z") + _reject("d-new"))
    assert _open_ids(state) == ["d-new"]


def test_no_project_id_is_unavailable_not_empty(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-rej"))
    section = oo.build_open_outcomes(state, project_id="")
    assert section["available"] is False and "project_id" in section["reason"]


# ---------------------------------------------------------------------------
# Klaar 2: two processes writing 200 decisions each give 400 valid lines
# ---------------------------------------------------------------------------

def _write_many(log_path: str, prefix: str, count: int) -> None:
    for n in range(count):
        oo.record_outcome_decision(Path(log_path), dispatch_id=f"{prefix}-{n}",
                                   project_id=PROJECT, decision="accept",
                                   reason="x" * 2000)


def test_two_processes_writing_200_decisions_each_give_400_valid_lines(tmp_path: Path) -> None:
    log = tmp_path / "state" / oo.DECISION_LOG_NAME
    log.parent.mkdir(parents=True)
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_write_many, args=(str(log), p, 200)) for p in ("p1", "p2")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    lines = log.read_bytes().split(b"\n")
    assert lines[-1] == b""
    records = [json.loads(line) for line in lines[:-1]]
    assert len(records) == 400
    assert all(r.get("decision_type") == oo.OUTCOME_DECISION_TYPE for r in records)
    assert all(r.get("project_id") == PROJECT for r in records)
    assert len(oo.read_outcome_decisions(log, PROJECT)) == 400


# ---------------------------------------------------------------------------
# Klaar 3: a torn last line does not crash the reader
# ---------------------------------------------------------------------------

def test_torn_last_line_and_garbage_line_are_skipped(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-1") + _reject("d-2"))
    _decide(state, "d-1", "reject")
    with open(state / oo.DECISION_LOG_NAME, "a", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write('{"decision_type":"outcome_decision","dispatch_id":"d-2","project_id":"proj-a","dec')
    assert set(oo.read_outcome_decisions(state / oo.DECISION_LOG_NAME, PROJECT)) == {"d-1"}
    assert _open_ids(state) == ["d-2"]


def test_torn_last_ledger_line_does_not_crash_the_reader(tmp_path: Path) -> None:
    state = tmp_path / "state"
    ledger = _ledger(state, _reject("d-1"))
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write('{"event_type":"task_complete","dispatch_id":"d-2","sta')
    assert _open_ids(state) == ["d-1"]


def test_last_decision_in_file_order_counts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    log = state / oo.DECISION_LOG_NAME
    oo.record_outcome_decision(log, dispatch_id="d-1", project_id=PROJECT,
                               decision="accept", timestamp="2026-09-30T12:00:00Z")
    oo.record_outcome_decision(log, dispatch_id="d-1", project_id=PROJECT,
                               decision="reject", timestamp="2026-09-30T08:00:00Z")
    assert oo.read_outcome_decisions(log, PROJECT).get("d-1", {}).get("decision") == "reject"


def test_other_records_in_the_decision_log_are_no_outcome_decision(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-1"))
    from t0_decision_log import log_decision
    assert log_decision(decision_type="dispatch_created", dispatch_id="d-1",
                        log_file=state / oo.DECISION_LOG_NAME)
    assert _open_ids(state) == ["d-1"]


def test_record_refuses_missing_project_or_bad_decision(tmp_path: Path) -> None:
    log = tmp_path / oo.DECISION_LOG_NAME
    with pytest.raises(ValueError):
        oo.record_outcome_decision(log, dispatch_id="d-1", project_id="", decision="accept")
    with pytest.raises(ValueError):
        oo.record_outcome_decision(log, dispatch_id="d-1", project_id=PROJECT, decision="maybe")
    with pytest.raises(ValueError):
        oo.record_outcome_decision(log, dispatch_id=" ", project_id=PROJECT, decision="accept")
    assert not log.exists()


# ---------------------------------------------------------------------------
# Klaar 4: a decision of project B leaves project A's open outcome standing
# ---------------------------------------------------------------------------

def test_decision_of_another_project_does_not_close_this_one(tmp_path: Path) -> None:
    state = tmp_path / "state"
    # the colliding id also has a foreign line in A's ledger: it must not leak either
    _ledger(state, _reject("d-1") + _reject("d-foreign", project=OTHER))
    _decide(state, "d-1", "accept", project=OTHER)
    assert _open_ids(state) == ["d-1"]
    assert _open_ids(state, project=OTHER) == ["d-foreign"]


# ---------------------------------------------------------------------------
# Klaar 5: two T0 sessions see the same open outcome
# ---------------------------------------------------------------------------

def _cli(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "VNX_PROJECT_ID": PROJECT}
    return subprocess.run([sys.executable, str(_SCRIPTS / "receipt_query.py"), *args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_two_sessions_see_the_same_open_outcomes_and_reading_writes_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-1") + _investigate("d-2"))
    before = sorted(p.name for p in state.iterdir())
    first = _cli("open-outcomes", "--state-dir", str(state), "--json")
    second = _cli("open-outcomes", "--state-dir", str(state), "--json")
    assert first.returncode == 0 and second.returncode == 0, first.stderr + second.stderr
    items_1 = json.loads(first.stdout)["items"]
    items_2 = json.loads(second.stdout)["items"]
    assert [i["dispatch_id"] for i in items_1] == [i["dispatch_id"] for i in items_2]
    assert sorted(i["dispatch_id"] for i in items_1) == ["d-1", "d-2"]
    assert sorted(p.name for p in state.iterdir()) == before


def test_cli_decide_writes_one_line_and_closes_the_point(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-1"))
    done = _cli("decide", "d-1", "reject", "--reason", "lane failed, rework in d-1b",
                "--state-dir", str(state))
    assert done.returncode == 0, done.stderr
    lines = (state / oo.DECISION_LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert (record["dispatch_id"], record["project_id"], record["decision"]) == ("d-1", PROJECT, "reject")
    assert record["timestamp"]
    listed = _cli("open-outcomes", "--state-dir", str(state), "--json")
    assert json.loads(listed.stdout)["items"] == []


def test_cli_decide_refuses_blank_reason_and_missing_project(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-1"))
    blank = _cli("decide", "d-1", "accept", "--reason", "  ", "--state-dir", str(state))
    assert blank.returncode == 2 and "--reason must not be blank" in blank.stderr
    env = {k: v for k, v in os.environ.items() if k != "VNX_PROJECT_ID"}
    refused = subprocess.run([sys.executable, str(_SCRIPTS / "receipt_query.py"), "decide", "d-1",
                              "accept", "--reason", "ok", "--state-dir", str(state)],
                             capture_output=True, text=True, env=env, timeout=60)
    assert refused.returncode == 2 and "no project id" in refused.stderr
    assert not (state / oo.DECISION_LOG_NAME).exists()


def test_pull_subcommand_and_cursor_are_gone(tmp_path: Path) -> None:
    gone = _cli("pull", "--state-dir", str(tmp_path))
    assert gone.returncode == 2 and "invalid choice" in gone.stderr
    assert not hasattr(rq, "pull_new_receipts") and not hasattr(rq, "CURSOR_NAME")


# ---------------------------------------------------------------------------
# Sluitstuk: the drain moves a decided dispatch out of active/
# ---------------------------------------------------------------------------

def _store(tmp_path: Path) -> Path:
    data = tmp_path / ".vnx-data"
    for sub in ("active", "completed", "dead_letter"):
        (data / "dispatches" / sub).mkdir(parents=True)
    (data / "receipts" / "processed").mkdir(parents=True)
    (data / "state").mkdir()
    return data


def _processed(data: Path, receipts: List[Dict[str, Any]]) -> None:
    for seq, receipt in enumerate(receipts):
        name = f"{1780000000 + seq}-{receipt['dispatch_id']}-{seq}.json"
        (data / "receipts" / "processed" / name).write_text(json.dumps(receipt), encoding="utf-8")


def _active(data: Path, did: str) -> None:
    entry = data / "dispatches" / "active" / did
    entry.mkdir()
    ts = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (entry / "manifest.json").write_text(json.dumps({"dispatch_id": did, "timestamp": ts}),
                                         encoding="utf-8")


def _where(data: Path, did: str) -> str:
    for sub in ("active", "completed", "dead_letter"):
        if (data / "dispatches" / sub / did).exists():
            return sub
    return "missing"


def test_investigate_stays_in_active_until_a_decision_moves_it(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _processed(data, _investigate("d-acc") + _investigate("d-rej"))
    _active(data, "d-acc")
    _active(data, "d-rej")
    drain_active(data, older_than_hours=1.0)
    assert (_where(data, "d-acc"), _where(data, "d-rej")) == ("active", "active")

    _decide(data / "state", "d-acc", "accept")
    _decide(data / "state", "d-rej", "reject")
    results = {r.dispatch_id: r for r in drain_active(data, older_than_hours=1.0)}
    assert (_where(data, "d-acc"), _where(data, "d-rej")) == ("completed", "dead_letter")
    assert "T0 decision" in results["d-acc"].reason


def test_decision_of_another_project_moves_nothing(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _processed(data, _investigate("d-1"))
    _active(data, "d-1")
    _decide(data / "state", "d-1", "accept", project=OTHER)
    drain_active(data, older_than_hours=1.0)
    assert _where(data, "d-1") == "active"


def test_t0_decision_overrides_the_receipt_outcome(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _processed(data, _reject("d-1"))
    _active(data, "d-1")
    _decide(data / "state", "d-1", "accept")
    drain_active(data, older_than_hours=1.0)
    assert _where(data, "d-1") == "completed"


# ---------------------------------------------------------------------------
# ledger_health: an open outcome waiting past the threshold is a finding
# ---------------------------------------------------------------------------

def test_ledger_health_finds_an_open_outcome_waiting_too_long(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _ledger(state, _reject("d-1"))
    later = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    result = check_open_outcomes(state, stale_hours=24.0, now=later)
    assert result["status"] == STATUS_FINDING and result["stale_dispatch_ids"] == ["d-1"]
    assert check_open_outcomes(state, stale_hours=48.0, now=later)["status"] == STATUS_OK
    _decide(state, "d-1", "reject")
    result = check_open_outcomes(state, stale_hours=24.0, now=later)
    assert result["status"] == STATUS_OK and result["open_count"] == 0
