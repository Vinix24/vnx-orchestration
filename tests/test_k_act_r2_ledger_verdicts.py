"""ADR-045 audit-spoor-kern K-act round 2 (codex gate on #2386 @ 2c319c78).

Dispatch-ID: dlv-110f73fe28f8-r2

Three reproduced blockers:

1. ``attempt_verdict`` let a legacy completion record stand in for a missing
   ``dispatch_finished``. A v3 attempt now ends only on the
   ``dispatch_finished`` line of its highest attempt; anything else falls
   through to the liveness branch as "no end" does.
2. ``read_ledger`` skipped a corrupt interior line, so a dispatch whose own
   ``dispatch_started`` line was damaged read as ``legacy``. ``read_ledger``
   now keeps the raw text of every interior line that did not parse, and
   ``attempt_verdict`` raises ``NdjsonCorruptionError`` when one of them
   carries this dispatch id. Every caller treats that as "not done, not
   abandoned, report it" for that dispatch only.
3. ``dispatch_lifecycle.liveness`` trusted whatever ``json.loads`` returned
   from the spawn file: ``{}`` read as a dead child and ``[]`` raised. It now
   goes through ``dispatch_start.spawn_child_verdict`` like the door does.

Every ledger is written by the real append writer, every store is a real
``runtime_coordination.db`` under ``tmp_path``; no reader, verdict or probe
seam is patched.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import sqlite3
import subprocess
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

import check_active_drain  # noqa: E402
import coordination_db  # noqa: E402
import crash_recovery_sweep  # noqa: E402
import dispatch_exit  # noqa: E402
import dispatch_lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import ndjson_io  # noqa: E402
import open_outcomes as oo  # noqa: E402
import orphan_sweep  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from ledger_attempt_state import (KIND_DEAD_NO_END, KIND_ENDED, KIND_LEGACY,  # noqa: E402
                                  attempt_verdict, read_ledger)

PROJECT = "proj-kact2"
OTHER_PROJECT = "proj-kact2-other"
ROLE = "backend-developer"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
T0 = "2026-11-03T10:00:00Z"
END_TS = "2026-11-03T12:00:00Z"
DEADLINE = 300
GOOD = {"method": "pytest", "tests_run": 1, "tests_passed": 1, "tests_failed": 0}

_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_kact2", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
    monkeypatch.delenv("VNX_CURRENT_DISPATCH_ID", raising=False)


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the ledger layer real, never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


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


# ---------------------------------------------------------------------------
# Helpers: real writers, real directories
# ---------------------------------------------------------------------------

def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _append(receipt: Dict[str, Any], state_dir: Path) -> None:
    result = payload_module.append_receipt_payload(
        receipt, receipts_file=str(_ledger(state_dir)))
    assert result.status == "appended", result


def _dead_pid() -> tuple[int, str]:
    """A real process that has exited, with the kernel start time it had while alive."""
    proc = subprocess.Popen(["sleep", "0.3"])
    started = dispatch_start.process_started_at(proc.pid)
    proc.wait(timeout=30)
    assert started, "the kernel must report the child's start time for this fixture"
    return proc.pid, started


def _v3_start(state_dir: Path, dispatch_id: str, *, attempt: int = 1,
              door_pid: int | None = None, door_started_at: str | None = None,
              deadline_seconds: int | None = None,
              project_id: str = PROJECT) -> None:
    """A v3 start line through the real append writer (v3 needs no DB row)."""
    if door_pid is None or door_started_at is None:
        door_pid, door_started_at = _dead_pid()
    line: Dict[str, Any] = {
        "timestamp": T0, "event_type": "dispatch_started", "project_id": project_id,
        "dispatch_id": dispatch_id, "attempt": attempt, "role": ROLE,
        "base_sha": BASE_SHA, "spec_sha256": "b" * 64, "lifecycle_mode": "v3",
        "door_pid": door_pid, "door_started_at": door_started_at,
        "requested_by": "terminal:T1",
    }
    if deadline_seconds is not None:
        line["deadline_seconds"] = deadline_seconds
    _append(line, state_dir)


def _legacy_complete(state_dir: Path, dispatch_id: str, *, status: str = "success",
                     project_id: str = PROJECT) -> None:
    _append({"timestamp": T0, "event_type": "task_complete", "project_id": project_id,
             "dispatch_id": dispatch_id, "terminal": "T1", "status": status}, state_dir)


def _finish(state_dir: Path, dispatch_id: str, *, attempt: int = 1,
            end_state: str = "completed") -> None:
    """A real ``dispatch_finished``: the real intent writer, the real append rule."""
    handover = dispatch_start.Handover(
        project_id=PROJECT, dispatch_id=dispatch_id, attempt=attempt,
        lifecycle_mode="v3", door_pid=12345, door_started_at="Mon Nov  3 10:00:00 2026",
        started_at=T0)
    line = dispatch_exit.build_end_line(handover, end_state=end_state,
                                        head_sha=HEAD_SHA, timestamp=END_TS)
    result = dispatch_lifecycle.finalize(
        state_dir=state_dir, project_id=PROJECT, dispatch_id=dispatch_id, attempt=attempt,
        end_state=end_state, end_line=line)
    assert result.status in ("finalized", "already_published"), result


def _truncate_line_carrying(state_dir: Path, dispatch_id: str) -> None:
    """Cut the ledger line carrying *dispatch_id* mid-JSON, keeping the id in the
    raw text (the raw text is what the reader scopes the refusal on)."""
    path = _ledger(state_dir)
    lines = path.read_bytes().split(b"\n")
    index = next(i for i, line in enumerate(lines) if dispatch_id.encode() in line)
    marker = dispatch_id.encode()
    lines[index] = lines[index][: lines[index].find(marker) + len(marker)]
    path.write_bytes(b"\n".join(lines))


def _active(data_dir: Path, dispatch_id: str, hours_old: float = 5.0) -> Path:
    entry = data_dir / "dispatches" / "active" / dispatch_id
    entry.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(tz=timezone.utc) - timedelta(hours=hours_old)).isoformat()
    (entry / "manifest.json").write_text(
        json.dumps({"dispatch_id": dispatch_id, "timestamp": ts}), encoding="utf-8")
    return entry


def _receipt(data_dir: Path, dispatch_id: str, status: str = "failed") -> None:
    name = f"1780000000-{dispatch_id[:10]}-1.json"
    (data_dir / "receipts" / "processed" / name).write_text(json.dumps({
        "dispatch_id": dispatch_id, "event_type": "task_complete", "status": status,
        "verification": GOOD, "project_id": PROJECT,
        "timestamp": "2026-09-29T10:00:00Z"}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Rule 1: a v3 attempt ends only on its own dispatch_finished
# ---------------------------------------------------------------------------

def test_v3_start_with_a_legacy_complete_and_no_finish_is_not_ended(state_dir: Path) -> None:
    _v3_start(state_dir, "d-legacy-end")
    _legacy_complete(state_dir, "d-legacy-end", status="success")

    verdict = attempt_verdict(state_dir, PROJECT, "d-legacy-end")

    assert verdict.kind == KIND_DEAD_NO_END
    assert verdict.kind != KIND_ENDED
    assert verdict.end_line is None
    assert verdict.attempt == 1


def test_a_dispatch_finished_of_the_highest_attempt_ends_it(state_dir: Path) -> None:
    _v3_start(state_dir, "d-really-ended")
    _finish(state_dir, "d-really-ended", attempt=1, end_state="completed")

    verdict = attempt_verdict(state_dir, PROJECT, "d-really-ended")

    assert verdict.kind == KIND_ENDED
    assert verdict.end_status == "success" and verdict.end_state == "completed"
    assert verdict.end_line is not None
    assert verdict.end_line["event_type"] == "dispatch_finished"


def test_a_dispatch_finished_of_a_lower_attempt_only_is_not_ended(state_dir: Path) -> None:
    _v3_start(state_dir, "d-lower-end", attempt=1)
    _finish(state_dir, "d-lower-end", attempt=1, end_state="completed")
    _v3_start(state_dir, "d-lower-end", attempt=2)

    verdict = attempt_verdict(state_dir, PROJECT, "d-lower-end")

    assert verdict.kind == KIND_DEAD_NO_END
    assert verdict.attempt == 2
    assert verdict.end_line is None


# ---------------------------------------------------------------------------
# Rule 2: a broken line about this dispatch is unreadable, never skipped
# ---------------------------------------------------------------------------

def test_a_corrupt_line_carrying_this_dispatch_id_raises(state_dir: Path) -> None:
    _legacy_complete(state_dir, "d-before")
    _v3_start(state_dir, "d-broken")
    _truncate_line_carrying(state_dir, "d-broken")
    _legacy_complete(state_dir, "d-after")

    with pytest.raises(ndjson_io.NdjsonCorruptionError) as caught:
        attempt_verdict(state_dir, PROJECT, "d-broken")

    assert "d-broken" in str(caught.value) or caught.value.line_number > 0


def test_the_same_refusal_when_records_come_from_read_ledger(state_dir: Path) -> None:
    _legacy_complete(state_dir, "d-before")
    _v3_start(state_dir, "d-broken")
    _truncate_line_carrying(state_dir, "d-broken")
    _legacy_complete(state_dir, "d-after")

    records = read_ledger(state_dir)

    assert isinstance(records, list)
    with pytest.raises(ndjson_io.NdjsonCorruptionError):
        attempt_verdict(state_dir, PROJECT, "d-broken", records=records)


def test_a_corrupt_line_for_another_dispatch_leaves_this_verdict_alone(
        state_dir: Path) -> None:
    _legacy_complete(state_dir, "d-unrelated")
    _v3_start(state_dir, "d-damaged-elsewhere")
    _truncate_line_carrying(state_dir, "d-damaged-elsewhere")
    _legacy_complete(state_dir, "d-after")

    verdict = attempt_verdict(state_dir, PROJECT, "d-unrelated")

    assert verdict.kind == KIND_LEGACY
    assert verdict.is_legacy


def test_check_active_drain_leaves_the_corrupt_dispatch_and_drains_the_clean_one(
        data_dir: Path, state_dir: Path) -> None:
    _v3_start(state_dir, "d-clean")
    _finish(state_dir, "d-clean", end_state="completed")
    _v3_start(state_dir, "d-broken")
    _truncate_line_carrying(state_dir, "d-broken")
    _legacy_complete(state_dir, "d-after")
    _active(data_dir, "d-clean")
    broken_entry = _active(data_dir, "d-broken")

    results = {r.dispatch_id: r for r in check_active_drain.drain_active(
        data_dir, older_than_hours=1.0)}

    assert results["d-clean"].action == "completed"
    assert (data_dir / "dispatches" / "completed" / "d-clean").is_dir()
    assert results["d-broken"].action == "skipped"
    assert "unreadable" in results["d-broken"].reason
    assert broken_entry.is_dir(), "a dispatch with a broken ledger line is never moved"


def test_crash_recovery_leaves_the_corrupt_orphan_and_recovers_the_clean_one(
        data_dir: Path, state_dir: Path) -> None:
    _v3_start(state_dir, "d-broken")
    _truncate_line_carrying(state_dir, "d-broken")
    _legacy_complete(state_dir, "d-after")
    _active(data_dir, "d-broken")
    _active(data_dir, "d-clean-legacy")

    result = crash_recovery_sweep.sweep(
        data_dir, state_dir=state_dir, project_id=PROJECT, dry_run=True,
        pid_alive=lambda pid: False)

    assert "d-broken" not in result.recovered
    assert any(error.get("dispatch_id") == "d-broken" for error in result.errors), result.errors
    assert "d-clean-legacy" in result.recovered


def test_orphan_sweep_keeps_the_corrupt_worktree_and_handles_the_clean_one(
        tmp_path: Path, data_dir: Path, state_dir: Path) -> None:
    repo_root = tmp_path / "repo"
    worktrees = repo_root / ".vnx-data" / "worktrees"
    broken_tree = worktrees / "dispatch-d-broken"
    clean_tree = worktrees / "dispatch-d-clean"
    for tree in (broken_tree, clean_tree):
        tree.mkdir(parents=True)
    _v3_start(state_dir, "d-broken")
    _truncate_line_carrying(state_dir, "d-broken")
    _legacy_complete(state_dir, "d-after")

    result = orphan_sweep.sweep(
        repo_root=repo_root, data_dir=data_dir, state_dir=state_dir, project_id=PROJECT,
        dry_run=True, list_sessions=lambda: [], pid_alive=lambda pid: False,
        capture_pane=lambda session: "", account_data_root=tmp_path / "account")

    assert str(broken_tree) in result.worktrees_skipped_live
    assert str(clean_tree) not in result.worktrees_skipped_live
    assert str(clean_tree) in result.worktrees_removed + result.worktrees_preserved


def test_open_outcomes_reports_the_corrupt_dispatch_and_closes_the_clean_one(
        data_dir: Path, state_dir: Path) -> None:
    _v3_start(state_dir, "d-clean")
    _finish(state_dir, "d-clean", end_state="completed")
    _v3_start(state_dir, "d-broken")
    _truncate_line_carrying(state_dir, "d-broken")
    _legacy_complete(state_dir, "d-after")
    _active(data_dir, "d-clean")
    _active(data_dir, "d-broken")
    _receipt(data_dir, "d-broken", status="failed")

    section = oo.build_open_outcomes(
        data_dir / "state", project_id=PROJECT, limit=None, data_dir=data_dir)

    assert section["available"], section
    items = {item["dispatch_id"]: item for item in section["items"]}
    assert items["d-broken"]["outcome"] == oo.OUTCOME_LEDGER_UNREADABLE
    assert "unreadable" in items["d-broken"]["reason"]
    assert "d-clean" not in items


# ---------------------------------------------------------------------------
# Rule 3: the sweep trusts no malformed spawn file
# ---------------------------------------------------------------------------

def _inflight(state_dir: Path, dispatch_id: str, *, door: tuple[int, str]) -> Any:
    door_pid, door_started_at = door
    return dispatch_lifecycle.InFlightAttempt(
        PROJECT, dispatch_id, 1,
        {"timestamp": T0, "attempt": 1, "deadline_seconds": DEADLINE,
         "project_id": PROJECT, "dispatch_id": dispatch_id,
         "door_pid": door_pid, "door_started_at": door_started_at})


def _write_spawn(state_dir: Path, dispatch_id: str, raw: str) -> Path:
    path = dispatch_start.spawn_file_path(state_dir, PROJECT, dispatch_id, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return path


@pytest.mark.parametrize("raw", ["{}", "[]", '{"child_pid": 123}'])
def test_a_malformed_spawn_file_is_liveness_unknown_and_never_abandons(
        state_dir: Path, raw: str) -> None:
    door = _dead_pid()
    dispatch_id = "d-spawn-unknown"
    _write_spawn(state_dir, dispatch_id, raw)
    attempt = _inflight(state_dir, dispatch_id, door=door)

    verdict, _signal = dispatch_lifecycle.liveness(state_dir, attempt)

    assert verdict == dispatch_lifecycle.LIVENESS_UNKNOWN
    now = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
    counter, _source, _error = dispatch_lifecycle.sweep_attempt(state_dir, attempt, now)
    assert counter == "unknown_liveness"
    assert not _ledger(state_dir).exists() or b"dispatch_finished" not in _ledger(state_dir).read_bytes()


def test_no_spawn_file_stays_dead(state_dir: Path) -> None:
    door = _dead_pid()
    attempt = _inflight(state_dir, "d-no-spawn", door=door)

    verdict, signal = dispatch_lifecycle.liveness(state_dir, attempt)

    assert verdict == dispatch_lifecycle.LIVENESS_DEAD
    assert signal is None
