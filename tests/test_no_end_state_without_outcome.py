"""fabric-state-herstel D4b2: no end state without an outcome.

Operator decision 29-09-2026: a dispatch is only finished with an outcome,
accept or reject. Without one it is an open point in ``open_outcomes`` that a
T0 investigates; dead_letter is only a T0 decision in t0_decision_log.jsonl.

Every reader that moves a dispatch out of a live bucket uses the same
predicate (``open_outcomes.receipt_presence`` and the ``receipt_outcome``
index): only an accept goes to completed, a failure receipt without a T0
decision does not (OI-1907), and a receipt of another project under a
colliding id counts for nothing (ADR-007).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
for _p in (_SCRIPTS, _SCRIPTS / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import active_dispatch_janitor as janitor  # noqa: E402
import contract_invalid_ledger as cil  # noqa: E402
import dispatch_cleanup as dc  # noqa: E402
import open_outcomes as oo  # noqa: E402
from check_active_drain import drain_active  # noqa: E402
from report_body_contract import validate_body  # noqa: E402

# scripts/ and scripts/lib both hold a crash_recovery_sweep module; load the lib one by path
_crs_spec = importlib.util.spec_from_file_location(
    "crash_recovery_sweep_lib", _SCRIPTS / "lib" / "crash_recovery_sweep.py")
crs = importlib.util.module_from_spec(_crs_spec)
sys.modules[_crs_spec.name] = crs
_crs_spec.loader.exec_module(crs)

PROJECT = "proj-a"
OTHER = "proj-b"
GOOD = {"method": "pytest", "tests_run": 3, "tests_passed": 3, "tests_failed": 0}
TS = "2026-09-29T10:00:00Z"


@pytest.fixture(autouse=True)
def _project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)


def _a(did: str, status: str, project: str = PROJECT, **kw: Any) -> Dict[str, Any]:
    return {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
            "status": status, "timestamp": TS, "project_id": project, **kw}


def _b(did: str, status: str = "done", project: str = PROJECT, **kw: Any) -> Dict[str, Any]:
    return _a(did, status, project, report_file=f"{did}.md", verification=GOOD, **kw)


def _accept(did: str, project: str = PROJECT) -> List[Dict[str, Any]]:
    return [_a(did, "success", project), _b(did, project=project)]


def _gate_result(did: str, project: str = PROJECT) -> Dict[str, Any]:
    return {"event_type": "review_gate_result", "dispatch_id": did, "gate": "codex_gate",
            "pr_number": 42, "status": "pass", "project_id": project, "timestamp": TS}


def _store(root: Path) -> Path:
    data = root / ".vnx-data"
    for sub in ("active", "completed", "dead_letter"):
        (data / "dispatches" / sub).mkdir(parents=True)
    (data / "receipts" / "processed").mkdir(parents=True)
    (data / "state").mkdir()
    return data


def _processed(data: Path, receipts: List[Dict[str, Any]]) -> None:
    for seq, receipt in enumerate(receipts):
        name = f"{1780000000 + seq}-{receipt['dispatch_id']}-{seq}.json"
        (data / "receipts" / "processed" / name).write_text(json.dumps(receipt), encoding="utf-8")


def _ledger(data: Path, receipts: List[Dict[str, Any]]) -> None:
    path = data / "state" / oo.LEDGER_NAME
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in receipts))


def _active(data: Path, did: str, hours_old: float = 5.0) -> None:
    entry = data / "dispatches" / "active" / did
    entry.mkdir()
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (entry / "manifest.json").write_text(json.dumps({"dispatch_id": did, "timestamp": ts}),
                                         encoding="utf-8")


def _where(data: Path, did: str) -> str:
    for sub in ("active", "completed", "dead_letter"):
        if (data / "dispatches" / sub / did).exists():
            return sub
    return "missing"


def _decide(data: Path, did: str, decision: str, project: str = PROJECT) -> None:
    oo.record_outcome_decision(data / "state" / oo.DECISION_LOG_NAME, dispatch_id=did,
                               project_id=project, decision=decision, reason="reviewed")


def _open(data: Path, project: str = PROJECT) -> Dict[str, Dict[str, Any]]:
    section = oo.build_open_outcomes(data / "state", project_id=project, limit=None)
    assert section["available"], section
    return {i["dispatch_id"]: i for i in section["items"]}


def _item(data: Path, did: str) -> Dict[str, Any]:
    items = _open(data)
    assert did in items, f"{did} is not an open point: {sorted(items)}"
    return items[did]


# ---------------------------------------------------------------------------
# Point 2: the age rule no longer dead-letters; the dispatch is an open point
# ---------------------------------------------------------------------------

def test_active_dispatch_without_receipt_past_the_threshold_is_an_open_point(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d-silent", hours_old=5.0)
    results = {r.dispatch_id: r for r in drain_active(data, older_than_hours=1.0)}
    assert _where(data, "d-silent") == "active"
    assert results["d-silent"].action == "skipped"
    item = _item(data, "d-silent")
    assert (item["kind"], item["outcome"]) == ("active_dispatch", "no_receipt")


def test_active_dispatch_without_receipt_under_the_threshold_is_not_open_yet(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _active(data, "d-young", hours_old=0.1)
    drain_active(data, older_than_hours=1.0)
    assert _where(data, "d-young") == "active"
    assert "d-young" not in _open(data)


def test_active_dispatch_without_timestamp_is_an_open_point_not_dead_letter(tmp_path: Path) -> None:
    data = _store(tmp_path)
    (data / "dispatches" / "active" / "d-bare").mkdir()
    drain_active(data, older_than_hours=1.0)
    assert _where(data, "d-bare") == "active"
    assert _item(data, "d-bare")["outcome"] == "no_receipt"


# ---------------------------------------------------------------------------
# Point 1: a failure without a T0 judgment is an open point, not dead_letter
# ---------------------------------------------------------------------------

def test_failure_receipt_without_decision_stays_open_and_a_t0_reject_dead_letters_it(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _processed(data, [_a("d-fail", "failure")])
    _active(data, "d-fail")
    drain_active(data, older_than_hours=1.0)
    assert _where(data, "d-fail") == "active"
    assert _item(data, "d-fail")["outcome"] == "reject"

    _decide(data, "d-fail", "reject")
    results = {r.dispatch_id: r for r in drain_active(data, older_than_hours=1.0)}
    assert _where(data, "d-fail") == "dead_letter"
    assert "T0 decision" in results["d-fail"].reason
    assert "d-fail" not in _open(data)


def test_evidence_only_dispatch_after_the_epoch_is_an_open_point(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _ledger(data, [_gate_result("d-evidence")])
    item = _item(data, "d-evidence")
    assert (item["kind"], item["outcome"]) == ("receipt_outcome", "unknown")


def test_active_receipt_without_an_outcome_of_its_own_is_an_open_point(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _processed(data, [_gate_result("d-gate-only")])
    _active(data, "d-gate-only")
    drain_active(data, older_than_hours=1.0)
    assert _where(data, "d-gate-only") == "active"
    assert _item(data, "d-gate-only")["outcome"] in ("no_outcome", "unknown")


def test_accept_leaves_no_open_point(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _processed(data, _accept("d-ok"))
    _ledger(data, _accept("d-ok"))
    _active(data, "d-ok")
    drain_active(data, older_than_hours=1.0)
    assert _where(data, "d-ok") == "completed"
    assert "d-ok" not in _open(data)


# ---------------------------------------------------------------------------
# ADR-007: a second project with a colliding id moves nothing
# ---------------------------------------------------------------------------

def test_second_project_with_a_colliding_id_moves_nothing(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _processed(data, _accept("d-shared", project=OTHER) + [_a("d-mine", "failure")])
    _active(data, "d-shared")
    _active(data, "d-mine")
    _decide(data, "d-mine", "reject", project=OTHER)
    _decide(data, "d-shared", "accept", project=OTHER)
    drain_active(data, older_than_hours=1.0)
    assert (_where(data, "d-shared"), _where(data, "d-mine")) == ("active", "active")
    assert _item(data, "d-shared")["outcome"] == "no_receipt"
    assert _item(data, "d-mine")["outcome"] == "reject"
    # the other project's view never carries this project's active dispatches
    assert _open(data, project=OTHER) == {}


# ---------------------------------------------------------------------------
# Point 4b: active_dispatch_janitor promotes only an accept
# ---------------------------------------------------------------------------

def _janitor_layout(tmp_path: Path) -> tuple:
    data = _store(tmp_path)
    active = data / "dispatches" / "active"
    completed = data / "dispatches" / "completed"
    return data, active, completed, data / "receipts" / "processed"


def _md(active: Path, did: str, age_hours: float = 0.0) -> None:
    f = active / f"{did}.md"
    f.write_text(f"# {did}\n", encoding="utf-8")
    if age_hours:
        ts = time.time() - age_hours * 3600.0
        os.utime(f, (ts, ts))


def test_janitor_does_not_promote_a_failure_receipt(tmp_path: Path) -> None:
    data, active, completed, processed = _janitor_layout(tmp_path)
    _processed(data, [_a("d-fail", "failure")])
    _md(active, "d-fail")
    results = {r.dispatch_id: r for r in janitor.reconcile_active(active, completed, processed)}
    assert (active / "d-fail.md").exists()
    assert not (completed / "d-fail.md").exists()
    assert results["d-fail"].action != "completed"


def test_janitor_does_not_promote_on_another_projects_receipt(tmp_path: Path) -> None:
    data, active, completed, processed = _janitor_layout(tmp_path)
    _processed(data, _accept("d-shared", project=OTHER)
               + [{"event_type": "state_mutation", "dispatch_id": "d-book", "project_id": PROJECT}])
    _md(active, "d-shared")
    _md(active, "d-book")
    janitor.reconcile_active(active, completed, processed)
    assert (active / "d-shared.md").exists() and (active / "d-book.md").exists()
    assert list(completed.iterdir()) == []


def test_janitor_promotes_an_accept(tmp_path: Path) -> None:
    data, active, completed, processed = _janitor_layout(tmp_path)
    _processed(data, _accept("d-ok"))
    _md(active, "d-ok")
    results = {r.dispatch_id: r for r in janitor.reconcile_active(active, completed, processed)}
    assert results["d-ok"].action == "completed"
    assert (completed / "d-ok.md").exists()


# ---------------------------------------------------------------------------
# OI-1907: dispatch_cleanup sends only an accept to completed/
# ---------------------------------------------------------------------------

def _bundle(data: Path, did: str, age_days: float = 10.0) -> Path:
    bundle = data / "dispatches" / "pending" / did
    bundle.mkdir(parents=True)
    (bundle / "dispatch-spec.json").write_text(json.dumps({"dispatch_id": did, "project_id": PROJECT}))
    (bundle / "instruction.md").write_text("# instruction\n")
    old = time.time() - age_days * 86400
    os.utime(str(bundle), (old, old))
    return bundle


def test_cleanup_does_not_complete_a_failure_only_bundle(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _bundle(data, "d-fail")
    _ledger(data, [_a("d-fail", "failure")])
    entries = {e.dispatch_id: e for e in dc.scan_pending(data, data / "state")}
    assert entries["d-fail"].action != "move-to-completed"
    dc.execute_cleanup(list(entries.values()), data, dry_run=False)
    assert not (data / "dispatches" / "completed" / "d-fail").exists()


def test_cleanup_ignores_another_projects_success_under_a_colliding_id(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _bundle(data, "d-shared")
    _ledger(data, _accept("d-shared", project=OTHER))
    entry = dc.scan_pending(data, data / "state")[0]
    assert entry.action != "move-to-completed"
    assert entry.has_receipt is False


def test_cleanup_completes_an_accept(tmp_path: Path) -> None:
    data = _store(tmp_path)
    _bundle(data, "d-ok")
    _ledger(data, _accept("d-ok"))
    entry = dc.scan_pending(data, data / "state")[0]
    assert (entry.classification, entry.action) == ("receipt-found", "move-to-completed")


# ---------------------------------------------------------------------------
# Point 4c (OI-1917): the merge acceptance gate fails closed on an
# event_type=contract_invalid as the last outcome receipt
# ---------------------------------------------------------------------------

def test_merge_gate_refuses_a_last_contract_invalid_event_before_pr_merged(tmp_path: Path) -> None:
    ledger = tmp_path / "t0_receipts.ndjson"
    rows = [
        {"event_type": "contract_invalid", "dispatch_id": "d-ci", "status": "contract_invalid",
         "timestamp": "2026-09-29T10:00:00Z", "ingested_at": "2026-09-29T10:00:00Z",
         "project_id": PROJECT},
        {"event_type": "pr_merged", "dispatch_id": "d-ci", "pr_number": 7, "status": "merged",
         "timestamp": "2026-09-29T10:05:00Z", "project_id": PROJECT},
        # another project's success under the same id must not heal it (ADR-007)
        {"event_type": "task_complete", "dispatch_id": "d-ci", "status": "success",
         "timestamp": "2026-09-29T10:10:00Z", "ingested_at": "2026-09-29T10:10:00Z",
         "project_id": OTHER},
    ]
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    result = cil.evaluate_deliverable_acceptance("d-ci", ledger, project_id=PROJECT)
    assert result.acceptable is False
    assert result.code == cil.CODE_CONTRACT_INVALID


# ---------------------------------------------------------------------------
# Point 4d: a worker that dies before _govern still leaves a report
# ---------------------------------------------------------------------------

class _CrashEnv:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.data = root / ".vnx-data"
        (self.data / "dispatches" / "active").mkdir(parents=True)
        (self.data / "state").mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(self.data))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_STATE_DIR", str(self.data / "state"))
        monkeypatch.delenv("VNX_REPORTS_DIR", raising=False)

    def orphan(self, did: str) -> None:
        d = self.data / "dispatches" / "active" / did
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({
            "dispatch_id": did, "terminal": "T1", "model": "sonnet",
            "timestamp": "2026-09-29T09:00:00+00:00", "worker_pid": 999999,
        }), encoding="utf-8")

    def report(self, did: str) -> Path:
        return self.data / "unified_reports" / f"{did}.md"


def test_worker_that_died_before_govern_leaves_a_failure_report(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    mine = _CrashEnv(tmp_path / "a", monkeypatch)
    theirs_root = tmp_path / "b" / ".vnx-data"
    (theirs_root / "unified_reports").mkdir(parents=True)
    mine.orphan("d-shared")

    crs.sweep(mine.data, state_dir=mine.data / "state", project_id=PROJECT,
              pid_alive=lambda _pid: False)

    report = mine.report("d-shared")
    assert report.exists(), "a dead worker must leave a unified report"
    text = report.read_text(encoding="utf-8")
    assert validate_body(text).valid
    assert "**Dispatch-ID**: d-shared" in text
    assert "**Model**: sonnet" in text
    assert "**Provider**: claude" in text
    assert "**Status**: failure" in text
    assert "killed" in text
    # ADR-007: the other project's store under the same id is untouched
    assert not (theirs_root / "unified_reports" / "d-shared.md").exists()


def test_fallback_report_keeps_a_contract_valid_worker_report(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    env = _CrashEnv(tmp_path, monkeypatch)
    env.orphan("d-wrote")
    env.report("d-wrote").parent.mkdir(parents=True)
    own = ("**Dispatch-ID**: d-wrote\n**Model**: sonnet\n**Provider**: claude\n\n"
           "## Summary\nThe worker wrote its own complete report before the crash happened.\n\n"
           "## Changes\n- scripts/x.py\n\n## Verification\npytest tests/x.py: 3 passed\n\n"
           "## Open Items\nNone\n")
    env.report("d-wrote").write_text(own, encoding="utf-8")
    crs.sweep(env.data, state_dir=env.data / "state", project_id=PROJECT,
              pid_alive=lambda _pid: False)
    assert env.report("d-wrote").read_text(encoding="utf-8") == own


# ---------------------------------------------------------------------------
# dead_letter writers: the headless daemon keeps a failed delivery in active/
# ---------------------------------------------------------------------------

def test_headless_daemon_keeps_a_failed_delivery_in_active(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    import headless_dispatch_daemon as hdd

    data = tmp_path
    for sub in ("pending", "active", "completed", "dead_letter"):
        (data / "dispatches" / sub).mkdir(parents=True)
    (data / "state").mkdir()
    (data / "state" / "t0_state.json").write_text(json.dumps(
        {"schema_version": "2.0", "terminals": {"T1": {"lease_state": "idle", "status": "idle"}}}))
    (data / "dispatches" / "pending" / "20260929-failing-A.md").write_text(
        "[[TARGET:T1]]\nTrack: A\nRole: backend-developer\nGate: g\n\n---\n\n## Instruction\n\nx\n")
    monkeypatch.setenv("VNX_ADAPTER_T1", "subprocess")
    monkeypatch.setattr(hdd, "_acquire_lease", lambda t, d: 1)
    monkeypatch.setattr(hdd, "_deliver", lambda *a, **k: (False, "T1", 1))
    monkeypatch.setattr(hdd, "_release_lease", lambda t, g: True)
    monkeypatch.setattr(hdd, "_run_governance_pre_check", lambda *a, **k: (False, [], None))

    hdd.DispatchDaemon(data_dir=data, state_dir=data / "state").run_once()

    assert [p.name for p in (data / "dispatches" / "active").iterdir()] == ["20260929-failing-A.md"]
    assert list((data / "dispatches" / "dead_letter").iterdir()) == []
