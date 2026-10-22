"""tests/test_dispatch_guard_live_measurement.py — OI-2268.

``dispatch_guard.sh`` decided GO/WAIT from ``vnx status --json``, and that read
``t0_state.json`` as it was built: a pending bundle or an in-flight dispatch row that
appeared after the last build read as GO. The guard now asks ``vnx status --json --live``,
which measures ``live_work`` and ``queues`` at the moment of the call from the sources
``build_t0_state.py`` reads (the ``dispatches`` table plus the occupancy flock,
``dispatches/pending`` and ``dispatches/conflicts``), and GO requires that the answer
proves it was measured live and recently.

The end-to-end tests run the real guard and the real ``bin/vnx`` of this checkout against
an isolated store (tmp ``VNX_DATA_DIR``, own ``HOME``): the snapshot is built first, the
work appears after it, and the snapshot file is left untouched. A CLI older than the live
measurement is simulated with a stub that answers the 1.7.4 shape.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "skills" / "t0-orchestrator" / "scripts" / "dispatch_guard.sh"
INTEL = ROOT / "skills" / "t0-orchestrator" / "scripts" / "intelligence.sh"

for _p in (str(ROOT / "scripts"), str(ROOT / "scripts" / "lib"), str(ROOT / "scripts" / "cli"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_t0_state as bts
import vnx_status as vs
from fixtures.dispatches_schema_fixture import dispatches_ddl

PROJECT = "guardlive-alpha"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Isolated store: build the snapshot first, add the work after it.
# ---------------------------------------------------------------------------


def _store_with_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An idle, healthy store of PROJECT with t0_state.json built by build_t0_state."""
    store = tmp_path / "store"
    state_dir = store / "state"
    dispatch_dir = store / "dispatches"
    state_dir.mkdir(parents=True)
    for sub in ("pending", "active", "conflicts"):
        (dispatch_dir / sub).mkdir(parents=True)
    (store / ".vnx-project-id").write_text(PROJECT + "\n", encoding="utf-8")
    (state_dir / "t0_receipts.ndjson").write_text("", encoding="utf-8")
    with sqlite3.connect(str(state_dir / "runtime_coordination.db")) as conn:
        conn.execute(dispatches_ddl())
        conn.execute("ALTER TABLE dispatches ADD COLUMN claimed_at TEXT")

    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    # gh, the DB migrations and the terminals are not under test.
    monkeypatch.setattr(bts, "_build_pr_queue_section", lambda _sd: {"open_prs": []})
    monkeypatch.setattr(bts, "_init_and_check_db", lambda _sd, _skip=None: True)
    monkeypatch.setattr(bts, "_build_terminals", lambda _sd: {})
    real_health = bts._build_system_health

    def _isolated_health(sd, db_initialized, **kwargs):
        # Real process/launchd/beacon state of this machine is not under test.
        kwargs["daemon_liveness"] = {"overall": "ok"}
        kwargs["launchd_liveness"] = {"overall": "ok", "jobs": {}}
        kwargs["expected_beacon_components"] = ()
        return real_health(sd, db_initialized, **kwargs)

    monkeypatch.setattr(bts, "_build_system_health", _isolated_health)
    state = bts.build_t0_state(state_dir, dispatch_dir)
    assert state["queues"]["pending_count"] == 0
    assert state["live_work"]["available"] is True
    (state_dir / "t0_state.json").write_text(json.dumps(state), encoding="utf-8")
    return store


def _insert_running(store: Path, dispatch_id: str, project_id: str = PROJECT) -> None:
    started = _iso(datetime.now(timezone.utc) - timedelta(minutes=10))
    with sqlite3.connect(str(store / "state" / "runtime_coordination.db")) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count,"
            " created_at, updated_at, claimed_at) VALUES (?, ?, 'running', 0, ?, ?, ?)",
            (dispatch_id, project_id, started, started, started),
        )


class _HeldFlock:
    """The occupancy flock as the envelope holds it: LOCK_EX on an open description."""

    def __init__(self, store: Path, dispatch_id: str) -> None:
        claims = store / "state" / "dispatch_worktree_claims"
        claims.mkdir(parents=True, exist_ok=True)
        self._fh = open(claims / f"{dispatch_id}.occupancy", "a")
        fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


@pytest.fixture
def held_locks():
    locks: list[_HeldFlock] = []
    yield locks
    for lock in locks:
        lock.release()


def _store_env(tmp_path: Path, store: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_") and k != "PROJECT_ROOT"}
    env["HOME"] = str(home)
    env["VNX_DATA_DIR_EXPLICIT"] = "1"
    env["VNX_DATA_DIR"] = str(store)
    return env


def _guard(tmp_path: Path, store: Path, *args: str) -> subprocess.CompletedProcess:
    """The real guard of this checkout, so the real bin/vnx answers."""
    return subprocess.run(
        ["bash", str(GUARD), *args],
        capture_output=True, text=True, env=_store_env(tmp_path, store), cwd=tmp_path,
    )


class TestWorkAfterTheSnapshot:
    """Red on 6fc30baa: the snapshot stays idle, so the old guard answered GO."""

    def test_pending_bundle_after_the_snapshot_is_wait(self, tmp_path, monkeypatch):
        store = _store_with_snapshot(tmp_path, monkeypatch)
        snapshot = (store / "state" / "t0_state.json").read_bytes()
        idle = _guard(tmp_path, store, "json")
        assert idle.returncode == 0, idle.stdout + idle.stderr

        bundle = store / "dispatches" / "pending" / "20261022-after-snapshot"
        bundle.mkdir()
        (bundle / "dispatch-spec.json").write_text("{}", encoding="utf-8")
        result = _guard(tmp_path, store, "json")

        assert result.returncode == 2, f"pending bundle after the snapshot gave GO: {result.stdout}"
        payload = json.loads(result.stdout)
        assert payload["decision"] == "WAIT"
        assert payload["reason"] == "queue: pending=1"
        assert (store / "state" / "t0_state.json").read_bytes() == snapshot

    def test_in_flight_row_with_held_flock_after_the_snapshot_is_wait(
        self, tmp_path, monkeypatch, held_locks
    ):
        store = _store_with_snapshot(tmp_path, monkeypatch)
        snapshot = (store / "state" / "t0_state.json").read_bytes()
        idle = _guard(tmp_path, store, "json")
        assert idle.returncode == 0, idle.stdout + idle.stderr

        _insert_running(store, "20261022-in-flight")
        held_locks.append(_HeldFlock(store, "20261022-in-flight"))
        result = _guard(tmp_path, store, "json")

        assert result.returncode == 2, f"in-flight row after the snapshot gave GO: {result.stdout}"
        payload = json.loads(result.stdout)
        assert payload["decision"] == "WAIT"
        assert payload["reason"] == "busy: live_work live=1 starting=0 stale=0 unmeasured=0"
        assert [i["dispatch_id"] for i in payload["live_work"]["live"]] == ["20261022-in-flight"]
        assert (store / "state" / "t0_state.json").read_bytes() == snapshot

    def test_another_projects_row_after_the_snapshot_stays_go(self, tmp_path, monkeypatch, held_locks):
        """ADR-007: the live read is scoped to the project of the store."""
        store = _store_with_snapshot(tmp_path, monkeypatch)
        _insert_running(store, "20261022-other", project_id="guardlive-bravo")
        result = _guard(tmp_path, store, "json")
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["decision"] == "GO"

    def test_measurement_failure_is_wait_not_go(self, tmp_path, monkeypatch):
        """A live read that raises (here: the DB is no database) is WAIT, never GO."""
        store = _store_with_snapshot(tmp_path, monkeypatch)
        (store / "state" / "runtime_coordination.db").write_bytes(b"not a sqlite database" * 64)
        result = _guard(tmp_path, store, "json")

        assert result.returncode == 2, f"a failed live measurement gave rc={result.returncode}: {result.stdout}{result.stderr}"
        payload = json.loads(result.stdout)
        assert payload["decision"] == "WAIT"
        assert payload["reason"].startswith("live_unmeasured: measurement failed: live_work unavailable: ")
        assert "DatabaseError" in payload["reason"]

    def test_human_view_shows_live_measurement_time(self, tmp_path, monkeypatch):
        store = _store_with_snapshot(tmp_path, monkeypatch)
        result = _guard(tmp_path, store)
        assert result.returncode == 0, result.stdout + result.stderr
        measured = [line for line in result.stdout.splitlines() if line.startswith("Measured: ")]
        assert len(measured) == 1, result.stdout
        assert measured[0].startswith("Measured: live at 20")
        assert "age " in measured[0]


# ---------------------------------------------------------------------------
# vnx status --json [--live]: the marker and the failure shapes, in process.
# ---------------------------------------------------------------------------


def _status_json(argv: list[str], store: Path, capsys) -> dict:
    capsys.readouterr()
    assert vs.main(argv=argv, data_dir=store) == 0
    return json.loads(capsys.readouterr().out)


class TestStatusLiveMarker:
    def test_live_answer_carries_the_marker_and_the_live_sections(
        self, tmp_path, monkeypatch, held_locks, capsys
    ):
        store = _store_with_snapshot(tmp_path, monkeypatch)
        _insert_running(store, "20261022-marker")
        held_locks.append(_HeldFlock(store, "20261022-marker"))
        before = datetime.now(timezone.utc).replace(microsecond=0)
        out = _status_json(["--json", "--live"], store, capsys)

        assert out["schema"] == "vnx_status/1.0"
        measured_at = datetime.fromisoformat(out["live_measured_at"].replace("Z", "+00:00"))
        assert before <= measured_at <= datetime.now(timezone.utc)
        assert out["live_measurement"]["mode"] == "live"
        assert out["live_measurement"]["sections"] == ["live_work", "queues"]
        assert out["live_measurement"]["unmeasured"] == []
        assert [i["dispatch_id"] for i in out["live_work"]["live"]] == ["20261022-marker"]
        assert out["queues"]["pending_count"] == 0
        assert out["system_health"]["status"] == "healthy"

    def test_plain_answer_is_marked_as_snapshot(self, tmp_path, monkeypatch, held_locks, capsys):
        store = _store_with_snapshot(tmp_path, monkeypatch)
        _insert_running(store, "20261022-not-in-snapshot")
        held_locks.append(_HeldFlock(store, "20261022-not-in-snapshot"))
        out = _status_json(["--json"], store, capsys)

        assert out["live_measured_at"] is None
        assert out["live_measurement"]["mode"] == "snapshot"
        assert out["live_work"]["live"] == []

    def test_raising_live_work_measurement_is_unavailable_never_empty(
        self, tmp_path, monkeypatch, capsys
    ):
        store = _store_with_snapshot(tmp_path, monkeypatch)

        def _raise(*_args, **_kwargs):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(bts, "_build_live_work", _raise)
        out = _status_json(["--json", "--live"], store, capsys)

        assert out["live_work"]["available"] is False
        assert "RuntimeError: probe exploded" in out["live_work"]["reason"]
        assert "live" not in out["live_work"]
        assert any("probe exploded" in r for r in out["live_measurement"]["unmeasured"])
        assert out["system_health"]["status"] != "healthy"
        assert out["queues"]["pending_count"] == 0

    def test_raising_queue_measurement_is_unmeasured_never_zero(self, tmp_path, monkeypatch, capsys):
        store = _store_with_snapshot(tmp_path, monkeypatch)

        def _raise(*_args, **_kwargs):
            raise OSError("pending vanished")

        monkeypatch.setattr(bts, "_build_queues", _raise)
        out = _status_json(["--json", "--live"], store, capsys)

        assert out["queues"]["pending_count"] is None
        assert out["queues"]["conflict_count"] is None
        assert "pending vanished" in out["queues"]["pending_unmeasured_reason"]
        assert len(out["live_measurement"]["unmeasured"]) == 2
        assert out["live_work"]["available"] is True

    def test_builder_that_does_not_import_is_unmeasured(self, tmp_path, monkeypatch, capsys):
        store = _store_with_snapshot(tmp_path, monkeypatch)
        monkeypatch.setitem(sys.modules, "build_t0_state", None)
        out = _status_json(["--json", "--live"], store, capsys)

        assert out["live_work"]["available"] is False
        assert "import of build_t0_state halted" in out["live_work"]["reason"]
        assert out["queues"]["pending_count"] is None
        assert out["live_measurement"]["mode"] == "live"
        assert out["live_measurement"]["unmeasured"]


# ---------------------------------------------------------------------------
# A CLI without a fresh live measurement: WAIT, never GO.
# ---------------------------------------------------------------------------


def _v174_idle() -> dict:
    """The idle answer of a 1.7.4 CLI: vnx_status/1.0 without any live marker."""
    return {
        "schema": "vnx_status/1.0",
        "t0_state_available": True,
        "system_health": {"status": "healthy"},
        "queues": {"pending_count": 0, "conflict_count": 0},
        "live_work": {"available": True, "live": [], "starting": [], "stale": [], "unmeasured": []},
    }


def _live_idle(measured_at: str = "__NOW__") -> dict:
    payload = _v174_idle()
    payload["live_measured_at"] = measured_at
    payload["live_measurement"] = {
        "mode": "live", "sections": ["live_work", "queues"], "duration_seconds": 0.01, "unmeasured": [],
    }
    return payload


def _stub_repo(tmp_path: Path, payload: dict, *, live_flag: str) -> Path:
    """A git repo with the guard, intelligence.sh and a stub bin/vnx.

    ``live_flag``: ``refuse`` exits 1 on ``--live`` like the 1.7.4 wrapper
    ("Unknown option: --live"), ``answer`` gives the same JSON with or without it
    (a live CLI for a live payload, a CLI that ignores the flag for the 1.7.4 one).
    ``__NOW__`` in *payload* becomes the time of the call.
    """
    repo = tmp_path / "repo"
    scripts = repo / "skills" / "t0-orchestrator" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(GUARD, scripts / "dispatch_guard.sh")
    shutil.copy(INTEL, scripts / "intelligence.sh")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps(payload), encoding="utf-8")
    refuse = (
        'if [ "${3:-}" = "--live" ]; then echo "ERROR: [status] Unknown option: --live" >&2; exit 1; fi\n'
        if live_flag == "refuse" else ""
    )
    stub = repo / "bin" / "vnx"
    stub.parent.mkdir()
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "status --json" ]; then\n'
        f"{refuse}"
        f'  sed "s/__NOW__/$(date -u +%Y-%m-%dT%H:%M:%SZ)/" "{answer}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return repo


def _stub_guard(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    script = repo / "skills" / "t0-orchestrator" / "scripts" / "dispatch_guard.sh"
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=env, cwd=repo)


class TestNoFreshLiveMeasurement:
    @pytest.mark.parametrize("live_flag", ["refuse", "answer"])
    def test_cli_1_7_4_shape_is_wait(self, tmp_path, live_flag):
        repo = _stub_repo(tmp_path, _v174_idle(), live_flag=live_flag)
        result = _stub_guard(repo, "json")
        assert result.returncode == 2, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["decision"] == "WAIT"
        assert payload["reason"].startswith("live_unmeasured: cli gave no live measurement")
        if live_flag == "refuse":
            assert "refused 'status --json --live'" in payload["reason"]

    def test_cli_1_7_4_shape_human_view_names_the_snapshot(self, tmp_path):
        repo = _stub_repo(tmp_path, _v174_idle(), live_flag="refuse")
        result = _stub_guard(repo)
        assert result.returncode == 2, result.stdout + result.stderr
        assert result.stdout.startswith("WAIT: live_unmeasured: cli gave no live measurement")
        assert "Measured: snapshot" in result.stdout

    def test_fresh_live_marker_is_go(self, tmp_path):
        repo = _stub_repo(tmp_path, _live_idle(), live_flag="answer")
        result = _stub_guard(repo, "json")
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["decision"] == "GO"
        assert payload["live_measurement"]["mode"] == "live"
        assert 0 <= payload["live_age_seconds"] <= 10

    def test_stale_live_marker_is_wait(self, tmp_path):
        stale = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
        repo = _stub_repo(tmp_path, _live_idle(stale), live_flag="answer")
        result = _stub_guard(repo, "json")
        assert result.returncode == 2, result.stdout + result.stderr
        reason = json.loads(result.stdout)["reason"]
        assert reason.startswith("live_unmeasured: live measurement is ")
        assert "old (bound 10s)" in reason

    def test_future_live_marker_is_wait(self, tmp_path):
        future = _iso(datetime.now(timezone.utc) + timedelta(hours=1))
        repo = _stub_repo(tmp_path, _live_idle(future), live_flag="answer")
        result = _stub_guard(repo, "json")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "in the future" in json.loads(result.stdout)["reason"]

    def test_unparseable_live_marker_is_wait(self, tmp_path):
        repo = _stub_repo(tmp_path, _live_idle("yesterday"), live_flag="answer")
        result = _stub_guard(repo, "json")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "live_measured_at unparseable: yesterday" in json.loads(result.stdout)["reason"]

    def test_live_marker_missing_a_section_is_wait(self, tmp_path):
        payload = _live_idle()
        payload["live_measurement"]["sections"] = ["live_work"]
        repo = _stub_repo(tmp_path, payload, live_flag="answer")
        result = _stub_guard(repo, "json")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "queues" in json.loads(result.stdout)["reason"]

    def test_unmeasured_entry_is_wait(self, tmp_path):
        payload = _live_idle()
        payload["live_measurement"]["unmeasured"] = ["queues.pending_count unmeasured: PermissionError"]
        repo = _stub_repo(tmp_path, payload, live_flag="answer")
        result = _stub_guard(repo, "json")
        assert result.returncode == 2, result.stdout + result.stderr
        assert json.loads(result.stdout)["reason"] == (
            "live_unmeasured: measurement failed: queues.pending_count unmeasured: PermissionError"
        )

    def test_null_pending_count_is_wait_even_with_a_fresh_marker(self, tmp_path):
        payload = _live_idle()
        payload["queues"] = {"pending_count": None, "conflict_count": 0,
                             "pending_unmeasured_reason": "PermissionError: pending"}
        repo = _stub_repo(tmp_path, payload, live_flag="answer")
        result = _stub_guard(repo, "json")
        assert result.returncode == 2, result.stdout + result.stderr
        assert json.loads(result.stdout)["reason"] == "queue: pending unmeasured: PermissionError: pending"


# ---------------------------------------------------------------------------
# intelligence.sh: can_dispatch follows the guard, the displays follow the CLI.
# ---------------------------------------------------------------------------


def _intel(repo: Path, function: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    script = repo / "skills" / "t0-orchestrator" / "scripts" / "intelligence.sh"
    return subprocess.run(
        ["bash", "-c", f'source "{script}" >/dev/null; {function}'],
        capture_output=True, text=True, env=env, cwd=repo,
    )


class TestIntelligenceFollowsTheGuard:
    def test_can_dispatch_is_no_on_a_cli_without_live_measurement(self, tmp_path):
        repo = _stub_repo(tmp_path, _v174_idle(), live_flag="refuse")
        result = _intel(repo, "can_dispatch")
        assert result.stdout.startswith("NO - live_unmeasured: cli gave no live measurement"), result.stdout

    def test_check_queue_names_a_snapshot_answer(self, tmp_path):
        repo = _stub_repo(tmp_path, _v174_idle(), live_flag="refuse")
        result = _intel(repo, "check_queue")
        assert result.stdout.strip() == "pending=0 conflicts=0 (snapshot: cli gave no live measurement)", result.stdout

    def test_check_queue_live_answer_has_no_note(self, tmp_path):
        repo = _stub_repo(tmp_path, _live_idle(), live_flag="answer")
        result = _intel(repo, "check_queue")
        assert result.stdout.strip() == "pending=0 conflicts=0", result.stdout

    def test_check_queue_shows_an_unmeasured_count_as_unmeasured(self, tmp_path):
        payload = _live_idle()
        payload["queues"] = {"pending_count": None, "conflict_count": 0,
                             "pending_unmeasured_reason": "PermissionError: pending"}
        repo = _stub_repo(tmp_path, payload, live_flag="answer")
        result = _intel(repo, "check_queue")
        assert result.stdout.strip() == "pending=unmeasured conflicts=0", result.stdout

    def test_check_live_work_names_a_snapshot_answer(self, tmp_path):
        payload = _v174_idle()
        payload["live_work"]["live"] = [{"dispatch_id": "20261022-old", "state": "running", "age_seconds": 5}]
        repo = _stub_repo(tmp_path, payload, live_flag="refuse")
        result = _intel(repo, "check_live_work")
        assert "live 20261022-old state=running age=5s" in result.stdout, result.stdout
        assert "snapshot: cli gave no live measurement" in result.stdout
