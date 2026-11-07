"""ADR-045 audit-spoor-kern K-7b: both remaining readers of ``dispatches/active/``
decide a ``v3`` dispatch on the ledger plus liveness.

Dispatch-ID: dlv-06ab1f47535e

``crash_recovery_sweep`` and ``orphan_sweep`` used to answer "is this dead" from
the manifest bucket (and the orchestrator PID) alone. They now ask the K-7
reader, ``ledger_attempt_state.attempt_verdict``: a dispatch with no
``dispatch_started`` line is ``legacy`` and keeps every old path byte for byte; a
``v3`` one is decided on its ``dispatch_finished`` end line plus liveness. For a
``v3`` dispatch these two sweeps never write a receipt, a report or a move
(ADR-045 §7: the finalizer or the K-6 sweep ends it, the K-7 drain moves it).

Every store is real (a real ledger, real ``active/<id>/manifest.json`` files)
under ``tmp_path``; the lifecycle lines come from the real ``dispatch_start.begin``
and the real ``dispatch_lifecycle.finalize``. Only clocks and the liveness probes
are pinned — the same seams ``tests/test_k7_active_readers.py`` patches.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
# Unconditional inserts (mirrors test_orphan_sweep.py / test_crash_recovery_sweep.py):
# scripts/lib is frequently ALREADY on sys.path, so a `not in sys.path` guard would
# skip the front-insert and leave scripts/ ahead — resolving crash_recovery_sweep
# and orphan_sweep to their CLIs instead of the lib modules under test.
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import crash_recovery_sweep as crs  # noqa: E402
import dispatch_exit  # noqa: E402
import dispatch_lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import dispatch_worktree_isolation  # noqa: E402
import orphan_sweep as osweep  # noqa: E402
import tmux_worktree  # noqa: E402
from ledger_attempt_state import attempt_verdict  # noqa: E402

PROJECT = "proj-k7b"
ROLE = "backend-developer"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
DOOR_STARTED = "Mon Nov  3 10:00:00 2026"
OTHER_CLOCK = "Tue Nov  4 10:00:00 2026"
END_TS = "2026-11-03T12:00:00Z"
DEAD_PID = 999999


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
    monkeypatch.delenv("VNX_CURRENT_DISPATCH_ID", raising=False)


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
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real store: active/completed/dead_letter buckets, state and reports."""
    data = tmp_path / ".vnx-data"
    for sub in ("active", "completed", "dead_letter"):
        (data / "dispatches" / sub).mkdir(parents=True)
    (data / "receipts" / "processed").mkdir(parents=True)
    (data / "state").mkdir(parents=True)
    (data / "unified_reports").mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(data / "state"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data / "unified_reports"))
    # Every other variable the path resolver reads is pinned inside this store too, so
    # nothing an earlier test on the same worker left behind can name another store
    # (OI-2590). The ones that change behaviour rather than a path are removed.
    for name in ("VNX_DISPATCH_DIR", "VNX_DB_DIR", "VNX_LOCKS_DIR", "VNX_LOGS_DIR",
                 "VNX_PIDS_DIR", "VNX_HEADLESS_REPORTS_DIR", "VNX_INTELLIGENCE_DIR",
                 "VNX_SOCKETS_DIR", "VNX_TMUX_SIGNAL_DIR"):
        monkeypatch.setenv(name, str(data / name.removeprefix("VNX_").removesuffix("_DIR").lower()))
    monkeypatch.setenv("VNX_DATA_HOME", str(data / "account-home"))
    for name in ("VNX_PROJECT_ROOT", "VNX_PROJECT_FILTER", "VNX_CHAIN_RECEIPTS",
                 "VNX_REQUESTED_BY", "VNX_PARENT_DISPATCH", "VNX_TASK_CLASS",
                 "VNX_CANONICAL_ROOT", "VNX_HOME"):
        monkeypatch.delenv(name, raising=False)
    return data


@pytest.fixture()
def state_dir(data_dir: Path) -> Path:
    return data_dir / "state"


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "dispatch-spec.md"
    path.write_text("# staged spec\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Helpers: real hand-over, real finalizer, real manifests, real git repo
# ---------------------------------------------------------------------------

def _begin(state_dir: Path, spec_file: Path, dispatch_id: str) -> dispatch_start.Handover:
    return dispatch_start.begin(
        project_id=PROJECT, dispatch_id=dispatch_id, role=ROLE, base_sha=BASE_SHA,
        spec_file=spec_file, target_slot="T1", state_dir=state_dir)


def _end(state_dir: Path, handover: dispatch_start.Handover, end_state: str) -> None:
    line = dispatch_exit.build_end_line(handover, role=handover.role, end_state=end_state,
                                        head_sha=HEAD_SHA, timestamp=END_TS)
    dispatch_lifecycle.finalize(
        state_dir=state_dir, project_id=handover.project_id,
        dispatch_id=handover.dispatch_id, attempt=handover.attempt,
        end_state=end_state, end_line=line)


def _v3(state_dir: Path, spec_file: Path, dispatch_id: str, *,
        end_state: Optional[str] = None) -> dispatch_start.Handover:
    """A real ``v3`` hand-over, optionally ended by the real finalizer."""
    handover = _begin(state_dir, spec_file, dispatch_id)
    if end_state is not None:
        _end(state_dir, handover, end_state)
    return handover


def _active(data_dir: Path, dispatch_id: str, *, worker_pid: Optional[int] = DEAD_PID) -> Path:
    entry = data_dir / "dispatches" / "active" / dispatch_id
    entry.mkdir(parents=True, exist_ok=True)
    manifest = {"dispatch_id": dispatch_id, "terminal": "T1", "model": "sonnet",
                "timestamp": "2026-11-03T09:00:00+00:00"}
    if worker_pid is not None:
        manifest["worker_pid"] = worker_pid
    (entry / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return entry


def _ledger(data_dir: Path) -> List[Dict[str, Any]]:
    path = data_dir / "state" / "t0_receipts.ndjson"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _report(data_dir: Path, dispatch_id: str) -> Path:
    return data_dir / "unified_reports" / f"{dispatch_id}.md"


def _register(data: Path, event: str, dispatch_id: str) -> None:
    """A raw register event under ``state/``: test setup, not the write API."""
    path = data / "state" / "dispatch_register.ndjson"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": "2026-11-03T09:00:00.000000Z",
                                 "event": event, "dispatch_id": dispatch_id}) + "\n")


def _git_repo(tmp_path: Path) -> Path:
    """Bare origin plus a clone with one commit (mirrors test_orphan_sweep)."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    for args in (["init", "--bare", "--initial-branch=main", str(bare)],
                 ["clone", str(bare), str(tmp_path / "local")]):
        subprocess.run(["git", *args], check=True, capture_output=True)
    local = tmp_path / "local"
    for args in (["config", "user.email", "test@test.local"],
                 ["config", "user.name", "Test"]):
        subprocess.run(["git", "-C", str(local), *args], check=True, capture_output=True)
    (local / "README.md").write_text("init\n", encoding="utf-8")
    for args in (["add", "README.md"], ["commit", "-m", "initial"],
                 ["push", "-u", "origin", "main"]):
        subprocess.run(["git", "-C", str(local), *args], check=True, capture_output=True)
    return local


def _worker(data_dir: Path, repo_root: Optional[Path] = None,
            sessions: Optional[List[str]] = None, **kwargs: Any):
    """The unified sweep with the tmux listing pinned (no real tmux in a test)."""
    return osweep.sweep(repo_root=repo_root, data_dir=data_dir, project_id=PROJECT,
                        list_sessions=lambda: sessions or [], **kwargs)


def _crash(data_dir: Path, state_dir: Path, *, dead: bool = True, **kwargs: Any):
    return crs.sweep(data_dir, state_dir=state_dir, project_id=PROJECT,
                     pid_alive=lambda pid: not dead, **kwargs)


# ---------------------------------------------------------------------------
# 1. crash_recovery_sweep: a v3 orphan is counted, never recovered (rule 1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,box,field", [
    ("live", {"occupancy": "held"}, "v3_live"),
    ("unknown", {"unmeasurable": True}, "v3_unknown"),
    ("dead_no_end", {"occupancy": "absent", "door_clock": OTHER_CLOCK}, "v3_dead_no_end"),
])
def test_a_v3_orphan_is_counted_by_kind_and_never_recovered(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any],
        kind: str, box: Dict[str, Any], field: str) -> None:
    """The dead orchestrator PID is real evidence for a legacy dispatch and none
    at all for a v3 one: the verdict is asked before the liveness check, so even
    a "dead" v3 orphan is only counted — live, unknown and a start without an
    end are the K-6 sweep's and the K-7 drain's to settle."""
    dispatch_id = f"d-v3-{kind}"
    _v3(state_dir, spec_file, dispatch_id)
    probe.update(**box)
    entry = _active(data_dir, dispatch_id, worker_pid=DEAD_PID)
    before = _ledger(data_dir)
    assert attempt_verdict(state_dir, PROJECT, dispatch_id).kind == kind

    result = _crash(data_dir, state_dir, dead=True)

    assert getattr(result, field) == [dispatch_id]
    assert result.recovered == [] and result.skipped_alive == []
    assert entry.is_dir() and (entry / "manifest.json").is_file()
    assert not (data_dir / "dispatches" / "dead_letter" / dispatch_id).exists()
    assert not (data_dir / "dispatches" / "completed" / dispatch_id).exists()
    assert not _report(data_dir, dispatch_id).exists()
    assert _ledger(data_dir) == before, "a v3 orphan gets no receipt and no end line"
    assert result.to_dict()[field] == [dispatch_id]


def test_the_same_orphan_as_a_legacy_dispatch_is_recovered_as_today(
        data_dir: Path, state_dir: Path) -> None:
    """No start line -> legacy -> the report + failed receipt + dead_letter move
    + cleanup path the module shipped, byte for byte."""
    entry = _active(data_dir, "d-legacy-dead", worker_pid=DEAD_PID)
    assert attempt_verdict(state_dir, PROJECT, "d-legacy-dead").is_legacy

    result = _crash(data_dir, state_dir, dead=True)

    assert result.recovered == ["d-legacy-dead"]
    assert not entry.exists()
    assert (data_dir / "dispatches" / "dead_letter" / "d-legacy-dead" / "manifest.json").exists()
    receipts = [r for r in _ledger(data_dir) if r.get("dispatch_id") == "d-legacy-dead"]
    assert [r["status"] for r in receipts] == ["failed"]
    assert receipts[0]["failure_reason"] == crs.ORCHESTRATOR_DEATH_REASON
    assert _report(data_dir, "d-legacy-dead").exists()


def test_a_v3_orphan_with_an_end_line_is_counted_ended_and_left_alone(
        data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    """An ended v3 attempt is a proven terminal outcome: the sweep names it and
    writes nothing — the K-7 drain, not this sweep, moves the manifest."""
    _v3(state_dir, spec_file, "d-v3-ended", end_state="completed")
    entry = _active(data_dir, "d-v3-ended", worker_pid=DEAD_PID)
    before = _ledger(data_dir)
    assert attempt_verdict(state_dir, PROJECT, "d-v3-ended").kind == "ended"

    result = _crash(data_dir, state_dir, dead=True)

    assert result.v3_ended == ["d-v3-ended"]
    assert result.v3_dead_no_end == [] and result.recovered == []
    assert entry.is_dir() and (entry / "manifest.json").is_file()
    assert not _report(data_dir, "d-v3-ended").exists()
    assert _ledger(data_dir) == before


def test_an_unreadable_ledger_recovers_nothing_and_names_why(
        data_dir: Path, state_dir: Path) -> None:
    """Fail-closed: a sweep that cannot tell a v3 orphan from a legacy one must
    fail neither, so even a dead-PID orphan with no start line is left alone."""
    (state_dir / "t0_receipts.ndjson").mkdir()  # a directory: open() raises
    entry = _active(data_dir, "d-unknown-mode", worker_pid=DEAD_PID)

    result = _crash(data_dir, state_dir, dead=True)

    assert result.recovered == [] and result.v3_dead_no_end == []
    assert result.ledger_error is not None
    assert "t0_receipts.ndjson" in result.ledger_error
    assert result.errors and "t0_receipts.ndjson" in result.errors[0]["error"]
    assert result.to_dict()["ledger_error"] == result.ledger_error
    assert entry.is_dir()
    assert not (data_dir / "dispatches" / "dead_letter" / "d-unknown-mode").exists()
    assert not _report(data_dir, "d-unknown-mode").exists()


# ---------------------------------------------------------------------------
# 2. orphan_sweep kind 3: the delegation carries the ledger and rule 1
# ---------------------------------------------------------------------------

def test_kind_3_delegation_carries_the_v3_rule(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any],
        tmp_path: Path) -> None:
    _v3(state_dir, spec_file, "d-v3-active")
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    entry = _active(data_dir, "d-v3-active", worker_pid=DEAD_PID)

    result = _worker(data_dir, tmp_path / "repo")

    assert result.active_v3["v3_dead_no_end"] == ["d-v3-active"]
    assert result.active_recovered == []
    assert entry.is_dir()
    assert result.to_dict()["active_v3"]["v3_dead_no_end"] == ["d-v3-active"]


def test_an_unreadable_ledger_fails_the_unified_sweep_closed(
        data_dir: Path, state_dir: Path, tmp_path: Path) -> None:
    (state_dir / "t0_receipts.ndjson").mkdir()
    entry = _active(data_dir, "d-unreadable", worker_pid=DEAD_PID)

    result = _worker(data_dir, tmp_path / "repo")

    assert result.active_recovered == []
    assert any(e.get("kind") == "ledger" and "t0_receipts.ndjson" in e["error"]
               for e in result.errors)
    assert entry.is_dir()


# ---------------------------------------------------------------------------
# 3. orphan_sweep kind 2: the verdict outranks the register for a v3 dispatch
# ---------------------------------------------------------------------------

def test_kind_2_treats_a_live_v3_dispatch_as_in_flight(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any],
        tmp_path: Path) -> None:
    """The register says completed; the ledger says the attempt is live. For a
    v3 dispatch the ledger wins, so the worktree is never reaped."""
    local = _git_repo(tmp_path)
    handle = tmux_worktree.allocate("d-v3-live", repo_root=local)
    _register(data_dir, "dispatch_completed", "d-v3-live")
    _v3(state_dir, spec_file, "d-v3-live")
    probe.update(occupancy="held")

    result = _worker(data_dir, local)

    assert str(handle.path) in result.worktrees_skipped_live
    assert result.worktrees_removed == []
    assert handle.path.is_dir()


def test_kind_2_treats_an_ended_v3_dispatch_as_finished(
        data_dir: Path, state_dir: Path, spec_file: Path, tmp_path: Path) -> None:
    """The register still shows the dispatch in flight; its own end line is the
    proof, so the orphaned worktree is reaped."""
    local = _git_repo(tmp_path)
    handle = tmux_worktree.allocate("d-v3-ended", repo_root=local)
    _register(data_dir, "dispatch_created", "d-v3-ended")
    _v3(state_dir, spec_file, "d-v3-ended", end_state="completed")

    result = _worker(data_dir, local)

    assert str(handle.path) in result.worktrees_removed
    assert str(handle.path) not in result.worktrees_skipped_live
    assert not handle.path.exists()


@pytest.mark.parametrize("event,removed", [
    (None, True),                     # never registered: an ordinary orphan
    ("dispatch_created", False),      # registered, no terminal outcome: in flight
    ("dispatch_completed", True),     # registered and terminal: reaped
])
def test_kind_2_legacy_worktrees_are_unchanged(
        data_dir: Path, tmp_path: Path, event: Optional[str], removed: bool) -> None:
    local = _git_repo(tmp_path)
    dispatch_id = "d-legacy-wt"
    handle = tmux_worktree.allocate(dispatch_id, repo_root=local)
    if event is not None:
        _register(data_dir, event, dispatch_id)

    result = _worker(data_dir, local)

    if removed:
        assert str(handle.path) in result.worktrees_removed
        assert not handle.path.exists()
    else:
        assert str(handle.path) in result.worktrees_skipped_live
        assert handle.path.is_dir()


# ---------------------------------------------------------------------------
# 4. orphan_sweep kind 1b: only an end line proves a v3 dispatch completed
# ---------------------------------------------------------------------------

def test_kind_1b_counts_a_v3_dispatch_with_an_end_line_as_completed(
        data_dir: Path, state_dir: Path, spec_file: Path, tmp_path: Path) -> None:
    _register(data_dir, "dispatch_created", "d-v3-done")
    _v3(state_dir, spec_file, "d-v3-done", end_state="completed")
    killed: List[str] = []

    result = _worker(data_dir, tmp_path / "repo", sessions=["vnx-d-v3-done"],
                     probe_liveness=lambda session: True,
                     kill_session=lambda session: (killed.append(session), True)[1],
                     capture_pane=lambda session: "")

    assert killed == ["vnx-d-v3-done"]
    assert len(result.tmux_completed_orphans_killed) == 1
    assert result.tmux_completed_orphans_killed[0]["reason"].startswith(
        "ledger:dispatch_finished")


def test_kind_1b_does_not_count_a_v3_start_without_an_end_as_completed(
        data_dir: Path, state_dir: Path, spec_file: Path, probe: Dict[str, Any],
        tmp_path: Path) -> None:
    """The register calls it completed; the ledger shows a start without an end.
    Not in flight is not the same as completed, so the session stays."""
    _register(data_dir, "dispatch_completed", "d-v3-open")
    _v3(state_dir, spec_file, "d-v3-open")
    probe.update(occupancy="absent", door_clock=OTHER_CLOCK)
    killed: List[str] = []

    result = _worker(data_dir, tmp_path / "repo", sessions=["vnx-d-v3-open"],
                     probe_liveness=lambda session: True,
                     kill_session=lambda session: (killed.append(session), True)[1],
                     capture_pane=lambda session: "")

    assert killed == []
    assert result.tmux_completed_orphans_killed == []
    assert result.tmux_completed_orphans_preserved[0]["reason"] == "v3_dead_no_end"
    assert "vnx-d-v3-open" in result.tmux_skipped_alive


@pytest.mark.parametrize("event,killed", [
    ("dispatch_completed", True),     # register proves it terminal
    ("dispatch_created", False),      # known, no terminal outcome: preserved
])
def test_kind_1b_legacy_sessions_are_unchanged(
        data_dir: Path, tmp_path: Path, event: str, killed: bool) -> None:
    dispatch_id = "d-legacy-session"
    _register(data_dir, event, dispatch_id)
    killed_sessions: List[str] = []

    result = _worker(data_dir, tmp_path / "repo", sessions=[f"vnx-{dispatch_id}"],
                     probe_liveness=lambda session: True,
                     kill_session=lambda session: (killed_sessions.append(session), True)[1],
                     capture_pane=lambda session: "")

    if killed:
        assert killed_sessions == [f"vnx-{dispatch_id}"]
        assert result.tmux_completed_orphans_killed[0]["reason"].startswith(
            "register:dispatch_completed")
    else:
        assert killed_sessions == []
        assert result.tmux_completed_orphans_preserved[0]["reason"] == "not_completed"


# ---------------------------------------------------------------------------
# 6. Hermetic store: a foreign environment never reaches the v3 lifecycle (OI-2590)
# ---------------------------------------------------------------------------

FOREIGN_ENV = ("VNX_DATA_DIR", "VNX_STATE_DIR", "VNX_REPORTS_DIR", "VNX_DISPATCH_DIR",
               "VNX_DB_DIR", "VNX_LOCKS_DIR", "VNX_LOGS_DIR", "VNX_PIDS_DIR",
               "VNX_HEADLESS_REPORTS_DIR", "VNX_INTELLIGENCE_DIR", "VNX_SOCKETS_DIR",
               "VNX_TMUX_SIGNAL_DIR", "VNX_DATA_HOME")


@pytest.fixture()
def foreign_env(tmp_path_factory: pytest.TempPathFactory,
                monkeypatch: pytest.MonkeyPatch) -> Path:
    """What an earlier test on the same xdist worker can leave in ``os.environ``.

    Requested BEFORE ``data_dir`` in the test signature, so the store fixture has to
    win over every variable set here. The foreign store holds a real, empty
    ``runtime_coordination.db``: if the lifecycle resolved it, the finalizer would
    project into a store ``dispatch_start.begin`` never created.
    """
    foreign = tmp_path_factory.mktemp("foreign-store")
    for name in FOREIGN_ENV:
        monkeypatch.setenv(name, str(foreign / name.lower()))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "0")
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(foreign))
    monkeypatch.setenv("VNX_PROJECT_FILTER", "0")
    monkeypatch.setenv("VNX_CHAIN_RECEIPTS", "1")
    monkeypatch.setenv("VNX_REQUESTED_BY", "a-foreign-test")
    monkeypatch.setenv("VNX_PARENT_DISPATCH", "d-foreign-parent")
    monkeypatch.setenv("VNX_TASK_CLASS", "foreign-class")
    monkeypatch.setenv("VNX_CURRENT_DISPATCH_ID", "d-foreign-current")
    (foreign / "state").mkdir()
    (foreign / "state" / "runtime_coordination.db").write_bytes(b"")
    monkeypatch.chdir(foreign)
    return foreign


def test_a_foreign_environment_does_not_move_the_v3_lifecycle_out_of_its_own_store(
        foreign_env: Path, data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    before = sorted(str(p.relative_to(foreign_env)) for p in foreign_env.rglob("*"))

    _v3(state_dir, spec_file, "d-v3-hermetic", end_state="completed")

    assert attempt_verdict(state_dir, PROJECT, "d-v3-hermetic").kind == "ended"
    assert (state_dir / "t0_receipts.ndjson").is_file()
    assert not (state_dir / "runtime_coordination.db").exists()
    assert (foreign_env / "state" / "runtime_coordination.db").read_bytes() == b""
    assert sorted(str(p.relative_to(foreign_env)) for p in foreign_env.rglob("*")) == before


def test_a_present_but_schemaless_store_stays_blocking_for_the_finalizer(
        data_dir: Path, state_dir: Path, spec_file: Path) -> None:
    """Fail closed: a database file without ``dispatches`` is never read as "no store"."""
    handover = _begin(state_dir, spec_file, "d-v3-schemaless")
    (state_dir / "runtime_coordination.db").write_bytes(b"")

    with pytest.raises(Exception, match="no such table: dispatches"):
        _end(state_dir, handover, "completed")

    # The end line is already in the ledger (step 3); the projection (step 4) failed, so
    # the intent stays unpublished and the same finalize call completes it once the
    # store is sound again.
    assert list((state_dir / "dispatch_end").rglob("*.json"))
    (state_dir / "runtime_coordination.db").unlink()
    _end(state_dir, handover, "completed")
    assert attempt_verdict(state_dir, PROJECT, "d-v3-schemaless").kind == "ended"
