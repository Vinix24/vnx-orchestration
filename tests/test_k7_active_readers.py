"""ADR-045 audit-spoor-kern K-7: the active readers decide on ledger plus liveness.

Dispatch-ID: dlv-1c1097abb62b

``check_active_drain`` and ``open_outcomes`` used to answer "is this still
running" from ``dispatches/active/`` alone. They now ask the one shared reader,
``ledger_attempt_state.attempt_verdict``: a dispatch with no ``dispatch_started``
line is ``legacy`` and keeps the old receipt-based path; a ``v3`` one is
decided on its ``dispatch_finished`` end line plus liveness.

Every store is real (a real ledger, a real ``runtime_coordination.db``, real
``active/<id>/`` directories) under ``tmp_path``; the lifecycle lines come from
the real ``dispatch_start.begin`` and the real ``dispatch_lifecycle.finalize``.
Only the liveness probes are pinned.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
for _p in (SCRIPT_DIR, SCRIPT_DIR / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import coordination_db  # noqa: E402
import dispatch_exit  # noqa: E402
import dispatch_lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import dispatch_worktree_isolation  # noqa: E402
import open_outcomes as oo  # noqa: E402
import project_id_migration  # noqa: E402
import receipt_outcome as ro  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from check_active_drain import drain_active  # noqa: E402
from coordination_db import get_connection  # noqa: E402
from ledger_attempt_state import ATTEMPT_KINDS, attempt_verdict, read_ledger  # noqa: E402

PROJECT = "proj-k7"
OTHER = "proj-k7-other"
ROLE = "backend-developer"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
DOOR_STARTED = "Mon Nov  3 10:00:00 2026"
OTHER_CLOCK = "Tue Nov  4 10:00:00 2026"
END_TS = "2026-11-03T12:00:00Z"
GOOD = {"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0}

_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k7", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real ledger layer, never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    # The lifecycle writers (dispatch_start / dispatch_lifecycle) resolve the
    # append through the registered facade, so the same wrapper goes there too.
    monkeypatch.setattr("governance_receipts.append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


@pytest.fixture(autouse=True)
def probe(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Pin only the liveness probes: the occupancy flock and the pid start time."""
    box: Dict[str, Any] = {"occupancy": "absent", "door_clock": DOOR_STARTED,
                           "unmeasurable": False}
    monkeypatch.setattr(dispatch_worktree_isolation, "probe_occupancy",
                        lambda claims, dispatch_id: box["occupancy"])

    def _started(pid: object) -> str:
        if box["unmeasurable"]:
            raise OSError("ps is unavailable")
        return str(box["door_clock"])

    monkeypatch.setattr(dispatch_start, "process_started_at", _started)
    return box


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "vnx-data"
    for sub in ("active", "completed", "dead_letter"):
        (data / "dispatches" / sub).mkdir(parents=True)
    (data / "receipts" / "processed").mkdir(parents=True)
    (data / "state").mkdir(parents=True)
    return data


@pytest.fixture()
def state_dir(data_dir: Path) -> Path:
    """A real coordination store at the production shape (composite UNIQUE)."""
    sd = data_dir / "state"
    coordination_db.init_schema(sd)
    db_path = coordination_db.db_path_from_state_dir(sd)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        _mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return sd


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "dispatch-spec.md"
    path.write_text("# staged spec\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Helpers: real hand-over, real finalizer, real directories
# ---------------------------------------------------------------------------

def _insert_row(state_dir: Path, dispatch_id: str, *, project_id: str = PROJECT,
                attempt_count: int = 1, state: str = "running") -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (dispatch_id, project_id, state, attempt_count, "{}"))
        conn.commit()


def _begin(state_dir: Path, spec_file: Path, dispatch_id: str,
           project_id: str = PROJECT) -> dispatch_start.Handover:
    return dispatch_start.begin(
        project_id=project_id, dispatch_id=dispatch_id, role=ROLE, base_sha=BASE_SHA,
        spec_file=spec_file, target_slot="T1", state_dir=state_dir)


def _finalize(state_dir: Path, handover: dispatch_start.Handover, end_state: str
              ) -> dispatch_lifecycle.LifecycleResult:
    line = dispatch_exit.build_end_line(handover, role=handover.role, end_state=end_state,
                                        head_sha=HEAD_SHA, timestamp=END_TS)
    return dispatch_lifecycle.finalize(
        state_dir=state_dir, project_id=handover.project_id,
        dispatch_id=handover.dispatch_id, attempt=handover.attempt,
        end_state=end_state, end_line=line)


def _v3(state_dir: Path, spec_file: Path, dispatch_id: str, *,
        project_id: str = PROJECT) -> dispatch_start.Handover:
    _insert_row(state_dir, dispatch_id, project_id=project_id)
    return _begin(state_dir, spec_file, dispatch_id, project_id=project_id)


def _active(data_dir: Path, dispatch_id: str, hours_old: float = 5.0) -> Path:
    entry = data_dir / "dispatches" / "active" / dispatch_id
    entry.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(tz=timezone.utc) - timedelta(hours=hours_old)).isoformat()
    (entry / "manifest.json").write_text(
        json.dumps({"dispatch_id": dispatch_id, "timestamp": ts}), encoding="utf-8")
    return entry


def _receipt(data_dir: Path, dispatch_id: str, status: str = "success") -> None:
    name = f"1780000000-{dispatch_id[:10]}-1.json"
    (data_dir / "receipts" / "processed" / name).write_text(json.dumps({
        "dispatch_id": dispatch_id, "event_type": "task_complete", "status": status,
        "verification": GOOD, "project_id": PROJECT,
        "timestamp": "2026-09-29T10:00:00Z"}), encoding="utf-8")


def _drain(data_dir: Path) -> Dict[str, Any]:
    return {r.dispatch_id: r for r in drain_active(data_dir, older_than_hours=1.0)}


def _items(data_dir: Path, project_id: str = PROJECT) -> Dict[str, Dict[str, Any]]:
    section = oo.build_open_outcomes(data_dir / "state", project_id=project_id, limit=None,
                                     data_dir=data_dir)
    assert section["available"], section
    return {i["dispatch_id"]: i for i in section["items"]}


def _decide(data_dir: Path, dispatch_id: str, decision: str) -> None:
    oo.record_outcome_decision(data_dir / "state" / oo.DECISION_LOG_NAME,
                               dispatch_id=dispatch_id, project_id=PROJECT,
                               decision=decision, reason="reviewed")


# ---------------------------------------------------------------------------
# 1. The shared reader's five kinds
# ---------------------------------------------------------------------------

def test_attempt_verdict_has_exactly_the_five_kinds() -> None:
    assert ATTEMPT_KINDS == {"legacy", "ended", "live", "unknown", "dead_no_end"}


def test_a_dispatch_without_a_start_line_is_legacy(state_dir: Path) -> None:
    verdict = attempt_verdict(state_dir, PROJECT, "d-legacy")
    assert verdict.kind == "legacy" and verdict.is_legacy


def test_an_ended_attempt_carries_its_end_line_and_status(
        state_dir: Path, spec_file: Path) -> None:
    handover = _v3(state_dir, spec_file, "d-ended")
    _finalize(state_dir, handover, "completed")
    verdict = attempt_verdict(state_dir, PROJECT, "d-ended")
    assert verdict.kind == "ended"
    assert verdict.end_status == "success" and verdict.end_state == "completed"
    assert verdict.end_line is not None and verdict.end_line["event_type"] == "dispatch_finished"


def test_liveness_decides_a_v3_start_without_an_end(
        state_dir: Path, spec_file: Path, probe: Dict[str, Any]) -> None:
    _v3(state_dir, spec_file, "d-flight")
    probe.update(occupancy="held")
    assert attempt_verdict(state_dir, PROJECT, "d-flight").kind == "live"
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    assert attempt_verdict(state_dir, PROJECT, "d-flight").kind == "dead_no_end"
    probe.update(unmeasurable=True)
    assert attempt_verdict(state_dir, PROJECT, "d-flight").kind == "unknown"


# ---------------------------------------------------------------------------
# 2. The two plan cases
# ---------------------------------------------------------------------------

def test_an_end_line_without_a_directory_counts_as_done(
        data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    handover = _v3(state_dir, spec_file, "d-done")
    _finalize(state_dir, handover, "completed")
    assert "d-done" not in _items(data_dir)
    assert _drain(data_dir) == {}


def test_a_directory_without_an_end_and_without_liveness_is_an_abandoned_candidate(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any]) -> None:
    _v3(state_dir, spec_file, "d-dead")
    # the hand-over recorded DOOR_STARTED; the probe now sees another start time
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    entry = _active(data_dir, "d-dead", hours_old=50.0)

    result = _drain(data_dir)["d-dead"]
    assert result.action == "skipped" and "abandoned candidate" in result.reason
    assert entry.is_dir()
    item = _items(data_dir)["d-dead"]
    # the ledger kind wins over the bucket (one open point per dispatch)
    assert item["kind"] == oo.KIND_RECEIPT_OUTCOME
    assert item["outcome"] == oo.OUTCOME_ABANDONED_CANDIDATE
    assert "abandoned candidate" in item["reason"]


# ---------------------------------------------------------------------------
# 3. Each end state through the drain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("end_state", ["failed", "timed_out", "abandoned"])
def test_an_open_end_state_stays_in_active_as_that_outcome(
        data_dir: Path, state_dir: Path, spec_file: Path, end_state: str) -> None:
    handover = _v3(state_dir, spec_file, f"d-{end_state}")
    _finalize(state_dir, handover, end_state)
    entry = _active(data_dir, f"d-{end_state}")

    result = _drain(data_dir)[f"d-{end_state}"]
    assert result.action == "skipped"
    assert f"end state '{end_state}'" in result.reason
    assert entry.is_dir()
    item = _items(data_dir)[f"d-{end_state}"]
    assert item["outcome"] == end_state
    assert f"end state '{end_state}'" in item["reason"]


def test_a_completed_end_moves_to_completed(
        data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    handover = _v3(state_dir, spec_file, "d-completed")
    _finalize(state_dir, handover, "completed")
    _active(data_dir, "d-completed")

    result = _drain(data_dir)["d-completed"]
    assert result.action == "completed" and "end line: completed" in result.reason
    assert (data_dir / "dispatches" / "completed" / "d-completed").is_dir()


@pytest.mark.parametrize("decision,bucket", [("accept", "completed"), ("reject", "dead_letter")])
def test_a_t0_decision_still_moves_an_open_end_state(
        data_dir: Path, state_dir: Path, spec_file: Path, decision: str, bucket: str) -> None:
    handover = _v3(state_dir, spec_file, "d-decided")
    _finalize(state_dir, handover, "abandoned")
    _active(data_dir, "d-decided")
    _decide(data_dir, "d-decided", decision)

    result = _drain(data_dir)["d-decided"]
    assert result.action == bucket and "T0 decision" in result.reason
    assert (data_dir / "dispatches" / bucket / "d-decided").is_dir()


# ---------------------------------------------------------------------------
# 4. Live and unknown liveness are never an open point, whatever the age
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("liveness,reason", [("live", "still running"), ("unknown", "unknown")])
def test_live_and_unknown_liveness_stay_and_are_no_open_point(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any],
        liveness: str, reason: str) -> None:
    _v3(state_dir, spec_file, f"d-{liveness}")
    probe.update(occupancy="held" if liveness == "live" else "absent",
                 door_clock=OTHER_CLOCK, unmeasurable=liveness == "unknown")
    entry = _active(data_dir, f"d-{liveness}", hours_old=50.0)

    result = _drain(data_dir)[f"d-{liveness}"]
    assert result.action == "skipped" and reason in result.reason
    assert entry.is_dir()
    assert f"d-{liveness}" not in _items(data_dir)


# ---------------------------------------------------------------------------
# 5. Legacy: a dispatch without a start line keeps today's destination and reason
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case,expected", [
    ("success", ("completed", "receipt found with success status")),
    ("failure", ("skipped", "open point: outcome 'reject' needs a human look "
                            "(a T0 decides: receipt_query.py decide)")),
    ("young", ("skipped", None)),
    ("old_no_receipt", ("skipped", None)),
    ("no_timestamp", ("skipped", "open point: no receipt, no timestamp")),
])
def test_a_legacy_dispatch_keeps_the_receipt_path(
        data_dir: Path, state_dir: Path, case: str, expected: tuple) -> None:
    did = f"d-legacy-{case}"
    if case == "no_timestamp":
        (data_dir / "dispatches" / "active" / did).mkdir()
    else:
        _active(data_dir, did, hours_old=0.1 if case == "young" else 5.0)
    if case == "success":
        _receipt(data_dir, did, "success")
    elif case == "failure":
        _receipt(data_dir, did, "failed")

    assert attempt_verdict(state_dir, PROJECT, did).is_legacy
    result = _drain(data_dir)[did]
    assert result.action == expected[0]
    if expected[1] is not None:
        assert result.reason == expected[1]
    elif case == "young":
        assert result.reason.startswith("no receipt yet, age")
    else:
        assert result.reason.startswith("open point: no receipt, age")


# ---------------------------------------------------------------------------
# 6. An unreadable ledger is never "done"
# ---------------------------------------------------------------------------

def test_an_unreadable_ledger_is_an_open_point_that_is_never_moved(data_dir: Path) -> None:
    (data_dir / "state" / "t0_receipts.ndjson").mkdir()  # a directory: open() raises
    entry = _active(data_dir, "d-unreadable")
    _receipt(data_dir, "d-unreadable", "success")

    result = _drain(data_dir)["d-unreadable"]
    assert result.action == "skipped" and "cannot read t0_receipts.ndjson" in result.reason
    assert entry.is_dir()
    section = oo.build_open_outcomes(data_dir / "state", project_id=PROJECT, limit=None,
                                     data_dir=data_dir)
    assert section["available"] is False and "t0_receipts.ndjson" in section["reason"]


# ---------------------------------------------------------------------------
# 7. open_outcomes decides a v3 dispatch on its end line
# ---------------------------------------------------------------------------

def test_abandoned_and_timed_out_ends_are_open_until_a_decision(
        data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    for end_state in ("abandoned", "timed_out", "completed"):
        handover = _v3(state_dir, spec_file, f"d-end-{end_state}")
        _finalize(state_dir, handover, end_state)

    items = _items(data_dir)
    assert items["d-end-abandoned"]["outcome"] == "abandoned"
    assert items["d-end-timed_out"]["outcome"] == "timed_out"
    assert "d-end-completed" not in items
    assert all(items[d]["kind"] == oo.KIND_RECEIPT_OUTCOME
               for d in ("d-end-abandoned", "d-end-timed_out"))

    _decide(data_dir, "d-end-abandoned", "reject")
    assert "d-end-abandoned" not in _items(data_dir)


def test_a_malformed_end_line_is_an_open_point_that_names_the_failure(
        data_dir: Path) -> None:
    ledger = data_dir / "state" / "t0_receipts.ndjson"
    lines = [
        {"timestamp": "2026-11-03T09:00:00Z", "event_type": "dispatch_started",
         "project_id": PROJECT, "dispatch_id": "d-bad-end", "attempt": 1, "role": ROLE,
         "base_sha": BASE_SHA, "spec_sha256": "0" * 64, "lifecycle_mode": "v3",
         "door_pid": 1, "door_started_at": DOOR_STARTED, "requested_by": "terminal:T1"},
        {"timestamp": "2026-11-03T12:00:00Z", "event_type": "dispatch_finished",
         "project_id": PROJECT, "dispatch_id": "d-bad-end", "attempt": 1,
         "end_state": "not-an-end-state"},
    ]
    ledger.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    # the write side refuses this line, so it is only ever read from history
    verdict = attempt_verdict(data_dir / "state", PROJECT, "d-bad-end")
    assert verdict.kind == "ended" and verdict.end_status is None
    _active(data_dir, "d-bad-end", hours_old=5.0)
    result = _drain(data_dir)["d-bad-end"]
    assert result.action == "skipped" and "malformed" in result.reason


# ---------------------------------------------------------------------------
# 8. ADR-007: two projects with one dispatch_id never decide each other
# ---------------------------------------------------------------------------

def test_only_the_own_projects_lines_decide(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any]) -> None:
    mine = _v3(state_dir, spec_file, "d-shared")
    _v3(state_dir, spec_file, "d-shared", project_id=OTHER)
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    _finalize(state_dir, mine, "completed")
    _active(data_dir, "d-shared")

    assert attempt_verdict(state_dir, PROJECT, "d-shared").kind == "ended"
    assert attempt_verdict(state_dir, OTHER, "d-shared").kind == "dead_no_end"
    assert _drain(data_dir)["d-shared"].action == "completed"
    assert (data_dir / "dispatches" / "completed" / "d-shared").is_dir()
    assert read_ledger(state_dir), "the shared ledger must carry both projects' lines"


# ---------------------------------------------------------------------------
# 9. A v3 end that is not completed stays open, superseded or not. The explicit
#    T0 decision closes it; an implicit supersession (a child naming it as
#    ``parent_dispatch``) does not (operator decision 03-11-2026, K-7 round 2).
# ---------------------------------------------------------------------------

def _ledger_line(state_dir: Path, **fields: Any) -> None:
    line = {"timestamp": "2026-11-03T13:00:00Z", "project_id": PROJECT, **fields}
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _child(state_dir: Path, parent: str, child: str) -> None:
    """A later dispatch whose receipt names ``parent`` as its ``parent_dispatch``."""
    _ledger_line(state_dir, event_type="task_complete", dispatch_id=child,
                 parent_dispatch=parent, status="done", verification=GOOD)


def _fold(state_dir: Path, dispatch_id: str) -> str:
    summary = ro.summarize(read_ledger(state_dir), project_id=PROJECT)
    return next(o for o in summary["outcomes"] if o["dispatch_id"] == dispatch_id)["decision"]


def test_a_superseded_failed_end_stays_open_until_a_t0_decision(
        data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    """Codex's reproduction: the failed end vanished once a child named it."""
    _finalize(state_dir, _v3(state_dir, spec_file, "d-parent"), "failed")
    _child(state_dir, "d-parent", "d-child")
    assert _fold(state_dir, "d-parent") == "superseded"

    item = _items(data_dir)["d-parent"]
    assert item["outcome"] == "failed"

    _decide(data_dir, "d-parent", "reject")
    assert "d-parent" not in _items(data_dir)


@pytest.mark.parametrize("end_state", ["abandoned", "timed_out"])
def test_another_superseded_open_end_is_listed_too(
        data_dir: Path, state_dir: Path, spec_file: Path, end_state: str) -> None:
    _finalize(state_dir, _v3(state_dir, spec_file, f"d-parent-{end_state}"), end_state)
    _child(state_dir, f"d-parent-{end_state}", f"d-child-{end_state}")
    assert _fold(state_dir, f"d-parent-{end_state}") == "superseded"

    assert _items(data_dir)[f"d-parent-{end_state}"]["outcome"] == end_state


def test_a_superseded_completed_end_is_no_open_point(
        data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    _finalize(state_dir, _v3(state_dir, spec_file, "d-done"), "completed")
    _child(state_dir, "d-done", "d-done-child")
    assert _fold(state_dir, "d-done") == "superseded"
    assert "d-done" not in _items(data_dir)


def test_an_accept_fold_does_not_hide_a_failed_end(
        data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    _finalize(state_dir, _v3(state_dir, spec_file, "d-late-accept"), "failed")
    _ledger_line(state_dir, event_type="task_complete", dispatch_id="d-late-accept",
                 status="success")
    _ledger_line(state_dir, event_type="task_complete", dispatch_id="d-late-accept",
                 status="done", report_file="d-late-accept.md", verification=GOOD)
    assert _fold(state_dir, "d-late-accept") == "accept"

    assert _items(data_dir)["d-late-accept"]["outcome"] == "failed"


def test_a_legacy_superseded_dispatch_is_still_no_open_point(
        data_dir: Path, state_dir: Path) -> None:
    _ledger_line(state_dir, event_type="task_complete", dispatch_id="d-legacy-super",
                 status="failure")
    _child(state_dir, "d-legacy-super", "d-legacy-child")
    assert attempt_verdict(state_dir, PROJECT, "d-legacy-super").is_legacy
    assert _fold(state_dir, "d-legacy-super") == "superseded"
    assert "d-legacy-super" not in _items(data_dir)


# ---------------------------------------------------------------------------
# 10. Only a ``dispatch_finished`` ends a v3 attempt (K-7 round 3, codex #2380)
# ---------------------------------------------------------------------------

def _legacy_completion(state_dir: Path, dispatch_id: str, status: str = "success", *,
                       attempt: int = 1) -> None:
    """The legacy completion family's line for this key's attempt (no end)."""
    _ledger_line(state_dir, event_type="task_complete", dispatch_id=dispatch_id,
                 attempt=attempt, status=status)


def test_a_legacy_completion_is_no_end_for_a_dead_v3_attempt(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any]) -> None:
    """Codex's reproduction: a success receipt ended the attempt the sweep saw in flight."""
    _v3(state_dir, spec_file, "d-legacy-end")
    _legacy_completion(state_dir, "d-legacy-end")
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    entry = _active(data_dir, "d-legacy-end", hours_old=5.0)

    verdict = attempt_verdict(state_dir, PROJECT, "d-legacy-end")
    assert verdict.kind == "dead_no_end"
    assert verdict.end_line is None and verdict.attempt == 1
    assert _items(data_dir)["d-legacy-end"]["outcome"] == oo.OUTCOME_ABANDONED_CANDIDATE
    result = _drain(data_dir)["d-legacy-end"]
    assert result.action == "skipped" and "abandoned candidate" in result.reason
    assert entry.is_dir()
    assert not (data_dir / "dispatches" / "completed" / "d-legacy-end").exists()


def test_a_legacy_completion_is_no_end_for_a_live_v3_attempt(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any]) -> None:
    _v3(state_dir, spec_file, "d-legacy-live")
    _legacy_completion(state_dir, "d-legacy-live")
    probe.update(occupancy="held")
    entry = _active(data_dir, "d-legacy-live", hours_old=50.0)

    verdict = attempt_verdict(state_dir, PROJECT, "d-legacy-live")
    assert verdict.kind == "live" and verdict.end_line is None
    result = _drain(data_dir)["d-legacy-live"]
    assert result.action == "skipped" and "still running" in result.reason
    assert entry.is_dir()
    assert "d-legacy-live" not in _items(data_dir)


def test_a_dispatch_finished_ends_the_v3_attempt_as_before(
        data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    handover = _v3(state_dir, spec_file, "d-finished")
    _legacy_completion(state_dir, "d-finished")  # beside it, the legacy line must not win
    _finalize(state_dir, handover, "completed")

    verdict = attempt_verdict(state_dir, PROJECT, "d-finished")
    assert verdict.kind == "ended" and verdict.attempt == 1
    assert verdict.end_state == "completed" and verdict.end_status == "success"
    assert verdict.end_line is not None
    assert verdict.end_line["event_type"] == "dispatch_finished"
    _active(data_dir, "d-finished")
    assert _drain(data_dir)["d-finished"].action == "completed"


def test_an_older_attempts_dispatch_finished_never_ends_a_newer_start(
        state_dir: Path, spec_file: Path, probe: Dict[str, Any]) -> None:
    first = _v3(state_dir, spec_file, "d-refire")
    _finalize(state_dir, first, "completed")
    second = _begin(state_dir, spec_file, "d-refire")
    assert second.attempt == 2
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)

    verdict = attempt_verdict(state_dir, PROJECT, "d-refire")
    assert verdict.kind == "dead_no_end" and verdict.attempt == 2
    assert verdict.end_line is None
    probe.update(occupancy="held")
    assert attempt_verdict(state_dir, PROJECT, "d-refire").kind == "live"


def test_a_legacy_completion_still_ends_a_dispatch_without_a_v3_start(
        data_dir: Path, state_dir: Path) -> None:
    _legacy_completion(state_dir, "d-legacy-only")
    _receipt(data_dir, "d-legacy-only", "success")
    _active(data_dir, "d-legacy-only", hours_old=5.0)

    verdict = attempt_verdict(state_dir, PROJECT, "d-legacy-only")
    assert verdict.kind == "legacy" and verdict.is_legacy
    assert verdict.end_line is None
    result = _drain(data_dir)["d-legacy-only"]
    assert result.action == "completed"
    assert result.reason == "receipt found with success status"


# ---------------------------------------------------------------------------
# 11. The end line must carry the exact key ``v3_in_flight`` ends on (K-7
#     round 4, codex #2380): a ``dispatch_finished`` missing a key field, or
#     with a malformed attempt, is no end -- never a defaulted one.
# ---------------------------------------------------------------------------

def _finished_line(state_dir: Path, dispatch_id: str, **fields: Any) -> None:
    """A ``dispatch_finished`` line; a field passed as ``None`` is left out."""
    line = {"timestamp": "2026-11-03T13:30:00Z", "event_type": "dispatch_finished",
            "project_id": PROJECT, "dispatch_id": dispatch_id, "attempt": 1,
            "end_state": "completed", **fields}
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: v for k, v in line.items() if v is not None}) + "\n")


def _assert_no_end(state_dir: Path, did: str) -> None:
    assert attempt_verdict(state_dir, PROJECT, did).kind == "dead_no_end"


def test_a_finished_line_without_an_attempt_is_no_end(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any]) -> None:
    _v3(state_dir, spec_file, "d-no-attempt")
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    line = {"timestamp": "2026-11-03T13:30:00Z", "event_type": "dispatch_finished",
            "project_id": PROJECT, "dispatch_id": "d-no-attempt", "end_state": "completed"}
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    entry = _active(data_dir, "d-no-attempt", hours_old=5.0)

    _assert_no_end(state_dir, "d-no-attempt")
    assert _items(data_dir)["d-no-attempt"]["outcome"] == oo.OUTCOME_ABANDONED_CANDIDATE
    assert _drain(data_dir)["d-no-attempt"].action == "skipped"
    assert entry.is_dir()
    assert not (data_dir / "dispatches" / "completed" / "d-no-attempt").exists()


def test_a_finished_line_without_a_project_id_is_no_end(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any]) -> None:
    _v3(state_dir, spec_file, "d-no-project")
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    _finished_line(state_dir, "d-no-project", project_id=None)
    entry = _active(data_dir, "d-no-project", hours_old=5.0)

    _assert_no_end(state_dir, "d-no-project")
    assert _items(data_dir)["d-no-project"]["outcome"] == oo.OUTCOME_ABANDONED_CANDIDATE
    assert _drain(data_dir)["d-no-project"].action == "skipped"
    assert entry.is_dir()
    assert not (data_dir / "dispatches" / "completed" / "d-no-project").exists()


def test_a_finished_line_of_another_project_is_no_end(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any]) -> None:
    _v3(state_dir, spec_file, "d-other-project")
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    _finished_line(state_dir, "d-other-project", project_id=OTHER)

    _assert_no_end(state_dir, "d-other-project")


@pytest.mark.parametrize("attempt", ["1", True])
def test_a_finished_line_with_a_malformed_attempt_is_no_end(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any],
        attempt: Any) -> None:
    _v3(state_dir, spec_file, "d-bad-attempt")
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    _finished_line(state_dir, "d-bad-attempt", attempt=attempt)

    _assert_no_end(state_dir, "d-bad-attempt")
