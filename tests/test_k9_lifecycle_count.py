"""ADR-045 audit-spoor-kern K-9: the lifecycle count, its beacon, its briefing line.

Dispatch-ID: dlv-321a4fa83c55

The count reports five defects over the ``v3`` lines of one store — a start
without an end past its deadline plus the sweep's grace, an end without a
start, a double end, a projection conflict and an unknown provenance — and it
never moves ``ledger_health``'s exit code. Every store here is real (a real
ledger written by ``dispatch_start.begin`` and the real finalizer, a real
``runtime_coordination.db``); only the clock and the writer's provenance are
pinned. The provenance repo is a throwaway git repository with two commits, so
the cohort boundary is a real ancestor test.

The red run of this file on the pre-K-9 base is in the completion report: no
``lifecycle_count`` module, no ``compute_health`` key, no briefing line.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import build_t0_state  # noqa: E402
import coordination_db  # noqa: E402
import dispatch_exit  # noqa: E402
import dispatch_lifecycle as lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import ledger_health as lh  # noqa: E402
import lifecycle_count  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import lifecycle as append_lifecycle  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402
from coordination_db import get_connection  # noqa: E402
from migrations.auto_apply import (  # noqa: E402
    _DEFAULT_MIGRATIONS_DIR,
    _RUNNERS_DIR,
    highest_auto_applicable_migration,
)

PROJECT = "vnx-dev"
ROLE = "backend-developer"
DEADLINE = 300  # dispatch_spec.DEADLINE_SECONDS_MIN: the smallest legal deadline
GRACE = 30 * 60  # ADR-045 §6's 30-minute decision, pinned literally
START = "2026-11-01T10:00:00Z"
START_DT = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
DOOR_START = "Mon Jan  1 00:00:00 2024"
SIX_HOURS = 6 * 60 * 60


def _at(seconds: float) -> datetime:
    return START_DT + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Real store fixtures (the shape tests/test_k6_sweep.py proved)
# ---------------------------------------------------------------------------

_spec_0017 = _ilu.spec_from_file_location(
    "apply_0017_k9", SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py")
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    # The lifecycle writers (dispatch_start / dispatch_lifecycle) resolve the
    # append through the registered facade, so the same wrapper goes there too.
    monkeypatch.setattr("governance_receipts.append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "vnx-data"


@pytest.fixture()
def state_dir(data_dir: Path) -> Path:
    sd = data_dir / "state"
    sd.mkdir(parents=True)
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
def lifecycle_on(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture()
def git_repo(tmp_path: Path) -> dict:
    """A real repository with two commits: the cohort boundary is a real ancestor."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init",), ("config", "user.email", "k9@example.invalid"),
                 ("config", "user.name", "K9")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "one"], check=True, capture_output=True)
    old = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    (repo / "b.txt").write_text("b", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "b.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "two"], check=True, capture_output=True)
    new = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    assert old and new and old != new
    return {"path": repo, "old": old, "new": new}


@pytest.fixture()
def writer_sha(monkeypatch, git_repo) -> dict:
    """Pin the provenance the real append layer stamps; the repo is the fixture's."""
    state = {"value": git_repo["new"]}
    monkeypatch.setattr(append_lifecycle, "writer_sha", lambda: state["value"])
    return state


# ---------------------------------------------------------------------------
# Helpers: real lines from the real writers
# ---------------------------------------------------------------------------

def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _append_raw(state_dir: Path, line: dict) -> None:
    """Append a line the way a corrupted/duplicated ledger would carry it."""
    with _ledger(state_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _begin(state_dir: Path, dispatch_id: str) -> dispatch_start.Handover:
    """A real ``dispatch_start.begin`` line, clock and door identity pinned."""
    spec = state_dir.parent / "dispatches" / "completed" / dispatch_id / "dispatch-spec.json"
    spec.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(spec, {"dispatch_id": dispatch_id, "deadline_seconds": DEADLINE})
    with patch.object(dispatch_start, "process_started_at", lambda pid: DOOR_START), \
            patch.object(dispatch_start, "_now", lambda: START):
        return dispatch_start.begin(project_id=PROJECT, dispatch_id=dispatch_id, role=ROLE,
                                    base_sha="a" * 40, spec_file=spec, target_slot="T1",
                                    state_dir=state_dir)


def _handover(dispatch_id: str, attempt: int = 1) -> dispatch_start.Handover:
    return dispatch_start.Handover(project_id=PROJECT, dispatch_id=dispatch_id, attempt=attempt,
                                   lifecycle_mode="v3", door_pid=0, door_started_at="",
                                   started_at=START, role=ROLE)


def _finish(state_dir: Path, handover, end_state: str = "completed", **extra):
    """A real finalizer end line (K-3c), projected like the lanes project it."""
    extra.setdefault("head_sha", "c" * 40)
    return lifecycle.finalize(
        state_dir=state_dir, project_id=handover.project_id,
        dispatch_id=handover.dispatch_id, attempt=handover.attempt, end_state=end_state,
        end_line=dispatch_exit.build_end_line(handover, role=handover.role,
                                              end_state=end_state, **extra),
    )


def _insert_row(state_dir: Path, dispatch_id: str, *, state: str = "running",
                attempt_count: int = 1) -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (dispatch_id, PROJECT, state, attempt_count, json.dumps({"lifecycle_mode": "v3"})))
        conn.commit()


def _sweep_heartbeat(state_dir: Path, written: datetime) -> None:
    path = state_dir / lifecycle.END_INTENT_DIRNAME / lifecycle.SWEEP_HEARTBEAT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"project_id": PROJECT,
                             "finished_at": written.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "counts": {"abandoned": 0}})


def _count(state_dir: Path, git_repo: dict, now: datetime = None, **kwargs) -> dict:
    return lifecycle_count.compute_lifecycle_count(
        state_dir, project_id=PROJECT, repo=git_repo["path"],
        now=now or _at(SIX_HOURS), **kwargs)


def _five_defects(state_dir: Path, writer_sha: dict) -> None:
    """One key per defect, each written by the real start line and the real finalizer."""
    _begin(state_dir, "d-k9-swe")                       # start, no end (past deadline+grace)
    _finish(state_dir, _handover("d-k9-ews"))           # end, no start
    handover = _begin(state_dir, "d-k9-dual")
    _finish(state_dir, handover)                        # one real end...
    _append_raw(state_dir, {"timestamp": START, "event_type": "dispatch_finished",
                            "schema_version": 3, "project_id": PROJECT, "dispatch_id": "d-k9-dual",
                            "attempt": 1, "end_state": "completed", "head_sha": "d" * 40,
                            "writer_sha": writer_sha["value"]})   # ...and a second one
    _insert_row(state_dir, "d-k9-conf", state="expired")
    handover = _begin(state_dir, "d-k9-conf")
    _finish(state_dir, handover)                        # completed vs an expired row: no path
    _begin(state_dir, "d-k9-prov")
    _append_raw(state_dir, {"timestamp": START, "event_type": "dispatch_finished",
                            "schema_version": 3, "project_id": PROJECT, "dispatch_id": "d-k9-prov",
                            "attempt": 1, "end_state": "completed", "head_sha": "d" * 40,
                            "writer_sha": "0123456789abcdef0123456789abcdef01234567"})


# ---------------------------------------------------------------------------
# 1. five defects, one finding each, with the right key
# ---------------------------------------------------------------------------

def test_five_defects_give_five_findings(state_dir, git_repo, writer_sha, lifecycle_on):
    _five_defects(state_dir, writer_sha)

    count = _count(state_dir, git_repo)

    assert count["status"] == "measured", count
    assert count["cohort"] == "all_v3"
    assert count["counts"] == {"start_without_end": 1, "end_without_start": 1, "double_end": 1,
                               "projection_conflict": 1, "provenance_unknown": 1, "total": 5}
    # rule 3's shape: a list of keys per defect; the list's length is the count
    assert {name: [(k[1], k[2]) for k in count["findings"][name]]
            for name in lifecycle_count.DEFECTS} == {
        "start_without_end": [("d-k9-swe", 1)],
        "end_without_start": [("d-k9-ews", 1)],
        "double_end": [("d-k9-dual", 1)],
        "projection_conflict": [("d-k9-conf", 1)],
        "provenance_unknown": [("d-k9-prov", 1)],
    }
    assert count["findings"]["double_end"] == [("vnx-dev", "d-k9-dual", 1)]
    assert count["problems"] == []


def test_a_start_inside_deadline_plus_grace_is_not_a_finding(state_dir, git_repo, writer_sha,
                                                             lifecycle_on):
    _begin(state_dir, "d-k9-fresh")

    count = _count(state_dir, git_repo, now=_at(DEADLINE + GRACE - 60))

    assert count["counts"]["total"] == 0, count


def test_the_projection_target_comes_from_the_finalizer_table(state_dir, git_repo, writer_sha,
                                                              lifecycle_on):
    """``failed`` projects to ``failed_delivery``; a row already there is no conflict."""
    _insert_row(state_dir, "d-k9-failed", state="delivering")
    _finish(state_dir, _begin(state_dir, "d-k9-failed"), end_state="failed")

    count = _count(state_dir, git_repo)

    assert count["counts"]["projection_conflict"] == 0, count


# ---------------------------------------------------------------------------
# 2. the exit code of ledger_health never moves with the count
# ---------------------------------------------------------------------------

def test_exit_code_is_equal_with_and_without_the_count(state_dir, data_dir, git_repo, writer_sha,
                                                       lifecycle_on, monkeypatch):
    _five_defects(state_dir, writer_sha)
    (data_dir / "state" / "dispatch_register.ndjson").write_text("", encoding="utf-8")
    # The tmp store carries no derivable project id; the other checks read the
    # same env fallback, so the count does too (never a silently empty project).
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)

    with_count = lh.compute_health(data_dir, state_dir, repo=git_repo["path"])
    assert with_count["lifecycle_count"]["counts"]["total"] == 5

    real_count = lh.compute_lifecycle_count
    no_count = {"status": lifecycle_count.STATUS_UNMEASURED,
                "counts": {name: None for name in lifecycle_count.DEFECTS}}
    monkeypatch.setattr(lh, "compute_lifecycle_count", lambda *a, **k: no_count)
    without_count = lh.compute_health(data_dir, state_dir, repo=git_repo["path"])

    assert with_count["exit_code"] == without_count["exit_code"]
    assert with_count["overall_status"] == without_count["overall_status"]

    monkeypatch.setattr(lh, "compute_lifecycle_count", real_count)
    rc_with = lh.main(["--data-dir", str(data_dir), "--state-dir", str(state_dir), "--no-write"])
    monkeypatch.setattr(lh, "compute_lifecycle_count", lambda *a, **k: no_count)
    rc_without = lh.main(["--data-dir", str(data_dir), "--state-dir", str(state_dir), "--no-write"])

    assert rc_with == rc_without == with_count["exit_code"]


# ---------------------------------------------------------------------------
# 3. provenance and the activation cohort
# ---------------------------------------------------------------------------

def test_an_unknown_writer_sha_is_provenance_unknown(state_dir, git_repo, writer_sha, lifecycle_on):
    _begin(state_dir, "d-k9-ghost")
    _append_raw(state_dir, {"timestamp": START, "event_type": "dispatch_finished",
                            "schema_version": 3, "project_id": PROJECT, "dispatch_id": "d-k9-ghost",
                            "attempt": 1, "end_state": "completed", "head_sha": "d" * 40,
                            "writer_sha": "f" * 40})

    count = _count(state_dir, git_repo)

    assert count["counts"]["provenance_unknown"] == 1, count
    assert count["findings"]["provenance_unknown"] == [("vnx-dev", "d-k9-ghost", 1)]


def test_the_activation_commit_narrows_the_cohort(state_dir, git_repo, writer_sha, lifecycle_on):
    writer_sha["value"] = git_repo["old"]
    _begin(state_dir, "d-k9-before")          # before the activation commit: not in the cohort
    writer_sha["value"] = git_repo["new"]
    _begin(state_dir, "d-k9-at")              # at/after it: in the cohort

    everything = _count(state_dir, git_repo)
    cohort = _count(state_dir, git_repo, activation_commit=git_repo["new"])

    assert everything["cohort"] == "all_v3"
    assert {k[1] for k in everything["findings"]["start_without_end"]} == {"d-k9-before", "d-k9-at"}
    assert cohort["cohort"] == git_repo["new"]
    assert [k[1] for k in cohort["findings"]["start_without_end"]] == ["d-k9-at"]


def test_a_line_with_an_unknown_sha_stays_provenance_unknown_in_the_cohort(
        state_dir, git_repo, writer_sha, lifecycle_on):
    _begin(state_dir, "d-k9-ghost")
    _append_raw(state_dir, {"timestamp": START, "event_type": "dispatch_finished",
                            "schema_version": 3, "project_id": PROJECT,
                            "dispatch_id": "d-k9-ghost", "attempt": 1, "end_state": "completed",
                            "head_sha": "d" * 40, "writer_sha": "f" * 40})

    count = _count(state_dir, git_repo, activation_commit=git_repo["new"])

    assert count["counts"]["provenance_unknown"] == 1, count


# ---------------------------------------------------------------------------
# 4. legacy history and unmeasurable stores
# ---------------------------------------------------------------------------

def test_a_legacy_dispatch_is_never_paired(state_dir, git_repo, writer_sha, lifecycle_on):
    _append_raw(state_dir, {"timestamp": START, "event_type": "task_complete",
                            "schema_version": 2, "project_id": PROJECT,
                            "dispatch_id": "d-k9-legacy", "status": "success"})

    count = _count(state_dir, git_repo)

    assert count["counts"]["total"] == 0, count
    assert count["findings"]["end_without_start"] == []


def test_a_git_that_cannot_run_makes_the_count_unmeasured(state_dir, git_repo, writer_sha,
                                                          lifecycle_on, monkeypatch):
    _five_defects(state_dir, writer_sha)
    monkeypatch.setattr(lifecycle_count, "GIT_TIMEOUT_SECONDS", 0.0)  # every probe times out

    count = _count(state_dir, git_repo)

    assert count["status"] == "unmeasured", count
    assert count["counts"] == {name: None for name in lifecycle_count.DEFECTS}
    assert "git could not run" in count["reason"]
    assert "unmeasured" in lifecycle_count.briefing_line(count)


def test_a_directory_that_is_no_repository_is_unmeasured(tmp_path, state_dir, writer_sha,
                                                         lifecycle_on):
    _begin(state_dir, "d-k9-swe")
    count = lifecycle_count.compute_lifecycle_count(
        state_dir, project_id=PROJECT, repo=tmp_path / "not-a-repo")

    assert count["status"] == "unmeasured", count
    assert count["counts"]["start_without_end"] is None


# ---------------------------------------------------------------------------
# 5. the beacon and the one briefing line
# ---------------------------------------------------------------------------

def test_the_count_writes_its_own_beacon_with_a_timestamp(state_dir, data_dir, git_repo,
                                                          writer_sha, lifecycle_on, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    _five_defects(state_dir, writer_sha)
    rc = lh.main(["--data-dir", str(data_dir), "--state-dir", str(state_dir), "--json"])
    beacon = json.loads((data_dir / "health" / "lifecycle_count.json").read_text(encoding="utf-8"))

    assert beacon["component"] == lh.LIFECYCLE_COUNT_COMPONENT
    assert beacon["status"] == "fail"  # five defects are never a green heartbeat
    assert beacon["details"]["counts"]["total"] >= 1
    assert beacon["details"]["counted_at"]
    assert rc == lh.EXIT_UNMEASURABLE  # the four checks cannot certify this bare store


def test_the_briefing_line_carries_the_five_numbers_and_the_sweep_age(
        state_dir, data_dir, git_repo, writer_sha, lifecycle_on):
    _five_defects(state_dir, writer_sha)
    now = _at(SIX_HOURS)
    _sweep_heartbeat(state_dir, now - timedelta(seconds=600))
    count = _count(state_dir, git_repo, now=now)
    lh.write_count_beacon(data_dir, {"overall_status": "ok", "exit_code": 0,
                                     "lifecycle_count": count})

    index = build_t0_state._build_t0_index(
        {"lifecycle_count": build_t0_state._lifecycle_count_index_summary(state_dir)})

    assert index["lifecycle"]["line"].startswith(
        "lifecycle [cohort all_v3]: 1 start-without-end, 1 end-without-start, 1 double, 1 conflict, "
        "1 provenance-unknown; sweep 600s; count ")
    assert count["sweep_heartbeat_age_seconds"] == 600
    assert count["sweep_heartbeat_reason"] is None


def test_the_missing_sweep_heartbeat_is_null_with_a_reason(state_dir, git_repo, writer_sha,
                                                           lifecycle_on):
    count = _count(state_dir, git_repo)

    assert count["sweep_heartbeat_age_seconds"] is None
    assert "no sweep heartbeat" in count["sweep_heartbeat_reason"]
    assert "sweep unknown" in lifecycle_count.briefing_line(count)


def test_the_sessionstart_hook_prints_the_lifecycle_line(tmp_path, state_dir, data_dir, git_repo,
                                                         writer_sha, lifecycle_on):
    _five_defects(state_dir, writer_sha)
    now = _at(SIX_HOURS)
    _sweep_heartbeat(state_dir, now - timedelta(seconds=600))
    count = _count(state_dir, git_repo, now=now)
    lh.write_count_beacon(data_dir, {"overall_status": "ok", "exit_code": 0,
                                     "lifecycle_count": count})
    index = build_t0_state._build_t0_index(
        {"lifecycle_count": build_t0_state._lifecycle_count_index_summary(state_dir)})
    (state_dir / "t0_index.json").write_text(json.dumps(index), encoding="utf-8")

    terminal = tmp_path / "project" / ".claude" / "terminals" / "T0"
    terminal.mkdir(parents=True)
    (tmp_path / "project" / ".vnx").mkdir()
    env = dict(os.environ, VNX_STATE_DIR=str(state_dir), VNX_DATA_DIR=str(data_dir),
               VNX_DATA_DIR_EXPLICIT="1")
    for var in ("VNX_PROJECT_ID", "VNX_HOME", "VNX_BIN", "VNX_EXECUTABLE"):
        env.pop(var, None)
    run = subprocess.run(["bash", str(VNX_ROOT / "hooks" / "sessionstart.sh")],
                         capture_output=True, text=True, cwd=str(terminal), env=env, timeout=30)

    assert run.returncode == 0, run.stderr
    context = json.loads(run.stdout)["hookSpecificOutput"]["additionalContext"]
    assert ("lifecycle [cohort all_v3]: 1 start-without-end, 1 end-without-start, 1 double, 1 conflict, "
            "1 provenance-unknown; sweep 600s; count ") in context, context


# ---------------------------------------------------------------------------
# 6. round 2 (OI-2528): what the count cannot measure is unmeasured, never zero
# ---------------------------------------------------------------------------

def _append_text(state_dir: Path, text: str) -> None:
    with _ledger(state_dir).open("a", encoding="utf-8") as fh:
        fh.write(text)


def test_an_activation_commit_that_does_not_resolve_is_unmeasured(
        state_dir, git_repo, writer_sha, lifecycle_on):
    """Warning 1: a git that fails is never a "no", so nothing falls out of a false cohort."""
    _begin(state_dir, "d-k9-swe")
    missing = "0" * 40

    count = _count(state_dir, git_repo, activation_commit=missing)

    assert count["status"] == "unmeasured", count
    assert count["counts"] == {name: None for name in lifecycle_count.DEFECTS}
    assert missing in count["reason"] and "does not resolve" in count["reason"]


def test_an_unparseable_start_timestamp_is_unmeasured_and_the_beacon_fails(
        state_dir, data_dir, git_repo, writer_sha, lifecycle_on, monkeypatch):
    """Warning 2: a problem is not a measurement — no zero may stand for it."""
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    _append_raw(state_dir, {"timestamp": "not-a-date", "event_type": "dispatch_started",
                            "schema_version": 3, "project_id": PROJECT,
                            "dispatch_id": "d-k9-date", "attempt": 1, "base_sha": "a" * 40,
                            "deadline_seconds": DEADLINE, "writer_sha": writer_sha["value"]})

    lh.main(["--data-dir", str(data_dir), "--state-dir", str(state_dir), "--json"])
    beacon = json.loads((data_dir / "health" / "lifecycle_count.json").read_text(encoding="utf-8"))
    line = build_t0_state._lifecycle_count_index_summary(state_dir)["line"]

    assert beacon["status"] == "fail", beacon
    assert beacon["details"]["status"] == "unmeasured"
    assert beacon["details"]["counts"][lifecycle_count.DEFECTS[0]] is None, beacon
    assert line.startswith("lifecycle: unmeasured"), line


def test_an_unknown_writer_on_a_report_ingested_line_is_provenance_unknown(
        state_dir, git_repo, writer_sha, lifecycle_on):
    """Warning 3: every v3 line of this project carries provenance, whatever its event."""
    _begin(state_dir, "d-k9-ingest")
    _append_raw(state_dir, {"timestamp": START, "event_type": "report_ingested",
                            "schema_version": 3, "project_id": PROJECT,
                            "dispatch_id": "d-k9-ingest", "attempt": 1,
                            "writer_sha": "f" * 40})

    count = _count(state_dir, git_repo)

    assert count["counts"]["provenance_unknown"] == 1, count
    assert count["findings"]["provenance_unknown"] == [("vnx-dev", "d-k9-ingest", 1)]


def test_an_unknown_writer_on_a_line_without_an_attempt_reports_attempt_none(
        state_dir, git_repo, writer_sha, lifecycle_on):
    """Rule 4's key choice: the attempt the line names, else ``None`` — never a guess."""
    _append_raw(state_dir, {"timestamp": START, "event_type": "report_ingested",
                            "schema_version": 3, "project_id": PROJECT,
                            "dispatch_id": "d-k9-no-attempt", "writer_sha": "f" * 40})

    count = _count(state_dir, git_repo)

    assert count["findings"]["provenance_unknown"] == [("vnx-dev", "d-k9-no-attempt", None)]


def test_an_interior_broken_line_makes_the_count_unmeasured(state_dir, git_repo, writer_sha,
                                                            lifecycle_on):
    """Rule 1: a hole in the ledger is no evidence; only the torn tail is read past."""
    _begin(state_dir, "d-k9-hole")
    broken_at = len(_ledger(state_dir).read_text(encoding="utf-8").splitlines()) + 1
    _append_text(state_dir, '{"timestamp": "2026-11-01T10:00:00Z", "event_type"\n')
    _begin(state_dir, "d-k9-after")

    count = _count(state_dir, git_repo)

    assert count["status"] == "unmeasured", count
    assert count["counts"] == {name: None for name in lifecycle_count.DEFECTS}
    assert f"line {broken_at}" in count["reason"], count["reason"]


def test_a_torn_final_line_is_no_record_and_no_defect(state_dir, git_repo, writer_sha,
                                                      lifecycle_on):
    _finish(state_dir, _begin(state_dir, "d-k9-torn"))
    _append_text(state_dir, '{"event_type": "dispatch_finished", "schema_ver')

    count = _count(state_dir, git_repo)

    assert count["status"] == "measured", count
    assert count["counts"]["total"] == 0, count
    assert count["problems"] == []


# ---------------------------------------------------------------------------
# 7. round 3 (codex on c0c92a8e): the end of an attempt comes from the cohort
# ---------------------------------------------------------------------------

def _excluded_end(state_dir: Path, dispatch_id: str, old_sha: str) -> None:
    """An end for the same attempt, written before the activation commit."""
    _append_raw(state_dir, {"timestamp": START, "event_type": "dispatch_finished",
                            "schema_version": 3, "project_id": PROJECT,
                            "dispatch_id": dispatch_id, "attempt": 1,
                            "end_state": "timed_out", "head_sha": "d" * 40,
                            "writer_sha": old_sha})


def test_an_excluded_end_does_not_decide_the_end_the_cohort_counts(
        state_dir, git_repo, writer_sha, lifecycle_on):
    """Codex's reproduction: the excluded ``timed_out`` end must not clear the conflict."""
    _insert_row(state_dir, "d-k9-cohort", state="timed_out")
    writer_sha["value"] = git_repo["new"]
    _finish(state_dir, _begin(state_dir, "d-k9-cohort"))  # an included completed end

    without = _count(state_dir, git_repo, activation_commit=git_repo["new"])
    _excluded_end(state_dir, "d-k9-cohort", git_repo["old"])
    with_excluded = _count(state_dir, git_repo, activation_commit=git_repo["new"])

    assert without["counts"]["provenance_unknown"] == 0, without  # both real lines carry one
    assert without["counts"]["projection_conflict"] == 1, without
    assert with_excluded["counts"]["projection_conflict"] == 1, with_excluded
    assert with_excluded["findings"]["projection_conflict"] == [("vnx-dev", "d-k9-cohort", 1)]


def test_an_excluded_end_is_not_a_second_end_inside_the_cohort(state_dir, git_repo, writer_sha,
                                                               lifecycle_on):
    _insert_row(state_dir, "d-k9-cohort", state="timed_out")
    writer_sha["value"] = git_repo["new"]
    _finish(state_dir, _begin(state_dir, "d-k9-cohort"))
    _excluded_end(state_dir, "d-k9-cohort", git_repo["old"])

    count = _count(state_dir, git_repo, activation_commit=git_repo["new"])

    assert count["counts"]["double_end"] == 0, count
    assert count["findings"]["double_end"] == []


# ---------------------------------------------------------------------------
# 6b. round 3 (codex): a database path that exists is never "no database"
# ---------------------------------------------------------------------------

def test_a_database_that_is_a_directory_makes_the_count_unmeasured(
        state_dir, git_repo, writer_sha, lifecycle_on):
    """Codex's reproduction: a valid start/end pair and the db path a directory."""
    _finish(state_dir, _begin(state_dir, "d-k9-dbdir"))
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    db_path.unlink()
    db_path.mkdir()

    count = _count(state_dir, git_repo)

    assert count["status"] == lifecycle_count.STATUS_UNMEASURED, count
    assert count["counts"] == {name: None for name in lifecycle_count.DEFECTS}
    assert "exists but is not a regular file" in count["reason"], count


def test_a_missing_database_path_still_counts_the_ledger(
        state_dir, git_repo, writer_sha, lifecycle_on):
    """Round 1's decision stands: a store with no database yet has no rows to read."""
    _finish(state_dir, _begin(state_dir, "d-k9-nodb"))
    coordination_db.db_path_from_state_dir(state_dir).unlink()

    count = _count(state_dir, git_repo)

    assert count["status"] == lifecycle_count.STATUS_MEASURED, count
    assert count["counts"]["total"] == 0, count


# ---------------------------------------------------------------------------
# 7. OI-2594: the briefing line names the cohort the count was taken over
# ---------------------------------------------------------------------------

def _measured(**extra) -> dict:
    count = {"status": "measured", "counts": dict.fromkeys(lifecycle_count.DEFECTS, 0),
             "sweep_heartbeat_age_seconds": 412}
    count.update(extra)
    return count


def test_the_line_names_all_v3_for_a_count_without_activation_commit():
    line = lifecycle_count.briefing_line(_measured(cohort="all_v3"))

    assert line.startswith("lifecycle [cohort all_v3]: "), line


def test_the_line_names_the_short_sha_for_a_count_with_an_activation_commit():
    sha = "a7a2d7c4" + "0" * 32

    line = lifecycle_count.briefing_line(_measured(cohort=sha))

    assert line.startswith("lifecycle [cohort since a7a2d7c4]: "), line


@pytest.mark.parametrize("cohort", [None, "", "  ", 7])
def test_a_measured_count_without_a_usable_cohort_says_unknown(cohort):
    line = lifecycle_count.briefing_line(_measured(cohort=cohort))
    absent = lifecycle_count.briefing_line(_measured())

    assert line.startswith("lifecycle [cohort unknown]: "), line
    assert absent.startswith("lifecycle [cohort unknown]: "), absent


def test_the_unmeasured_line_has_no_cohort():
    line = lifecycle_count.briefing_line(
        {"status": "unmeasured", "reason": "no project id", "cohort": None})

    assert line == "lifecycle: unmeasured (no project id); sweep unknown"


# ---------------------------------------------------------------------------
# 8. OI-2557: the age of the count, a capped reason and a guarded summary
# ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("stamp, expected", [
    ("2026-11-07T11:15:00Z", "count 45m old"),
    ("2026-11-07T11:59:30Z", "count 0m old"),
    ("2026-11-06T22:00:00Z", "count 14h old"),
    ("2026-11-07T11:00:00Z", "count 1h old"),
])
def test_the_line_carries_the_age_of_the_count(stamp, expected):
    for field in ("counted_at", "evaluated_at"):
        line = lifecycle_count.briefing_line(_measured(cohort="all_v3", **{field: stamp}), now=NOW)

        assert line.endswith(f"; {expected}"), line


@pytest.mark.parametrize("extra", [{}, {"counted_at": None}, {"counted_at": "yesterday"},
                                   {"counted_at": 7}, {"counted_at": "2026-11-08T12:00:00Z"}])
def test_a_count_without_a_usable_timestamp_says_the_age_is_unknown(extra):
    line = lifecycle_count.briefing_line(_measured(cohort="all_v3", **extra), now=NOW)

    assert line.endswith("; count age unknown"), line


def test_the_age_does_not_change_the_cohort_or_the_five_numbers():
    line = lifecycle_count.briefing_line(
        _measured(cohort="all_v3", counted_at="2026-11-07T11:00:00Z"), now=NOW)

    assert line == ("lifecycle [cohort all_v3]: 0 start-without-end, 0 end-without-start, 0 double, "
                    "0 conflict, 0 provenance-unknown; sweep 412s; count 1h old")


def test_a_long_unmeasured_reason_is_capped_with_the_number_of_hidden_problems():
    problems = [f"problem {n}: " + "x" * 50 for n in range(4)]
    reason = "; ".join(problems)

    line = lifecycle_count.briefing_line(
        {"status": "unmeasured", "reason": reason, "problems": problems})

    assert line.startswith("lifecycle: unmeasured (problem 0: ")
    assert line.endswith("\u2026 (+1 more)); sweep unknown")
    assert "problem 3" not in line
    assert len(line) < len(reason)


def test_a_short_unmeasured_reason_is_left_alone():
    line = lifecycle_count.briefing_line(
        {"status": "unmeasured", "reason": "a; b", "problems": ["a", "b"]})

    assert line == "lifecycle: unmeasured (a; b); sweep unknown"


def test_the_full_problem_list_stays_in_the_count(state_dir, git_repo, writer_sha, lifecycle_on,
                                                  monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    for n in range(3):
        _append_raw(state_dir, {"timestamp": "not-a-date", "event_type": "dispatch_started",
                                "schema_version": 3, "project_id": PROJECT,
                                "dispatch_id": f"d-k9-date-{n}", "attempt": 1, "base_sha": "a" * 40,
                                "deadline_seconds": DEADLINE, "writer_sha": writer_sha["value"]})

    count = _count(state_dir, git_repo)

    assert count["status"] == "unmeasured"
    assert len(count["problems"]) == 3, count["problems"]
    assert count["reason"] == "; ".join(count["problems"])


@pytest.mark.parametrize("counts", [["not", "a", "dict"], 7, "text"])
def test_a_beacon_with_malformed_counts_is_unmeasured_not_an_exception(data_dir, state_dir, counts):
    beacon = data_dir / "health" / "lifecycle_count.json"
    beacon.parent.mkdir(parents=True, exist_ok=True)
    beacon.write_text(json.dumps({"details": {"status": "measured", "counts": counts}}),
                      encoding="utf-8")

    line = build_t0_state._lifecycle_count_index_summary(state_dir)["line"]

    assert line.startswith("lifecycle: unmeasured (malformed beacon "), line
    assert line.endswith("); sweep unknown")
