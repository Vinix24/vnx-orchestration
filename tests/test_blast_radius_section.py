"""ADR-096 §15 (B4b-c): the blast-radius check and the dispatch claim run in one
serialised section, so a second fire in the same second sees the first as
``starting`` instead of reading "clear" too."""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "scripts", ROOT / "scripts" / "lib"):
    sys.path.insert(0, str(_p))

import blast_radius_gate as brg
import blast_radius_sources as brs
import config_runtime as crt
import coordination_db
import dispatch_cli
import project_id_migration
from dispatch_spec import DispatchPath

PID = "vnx-dev"
FIRST, SECOND = "dlv-sectionaa01", "dlv-sectionbb02"
SHARED = ("scripts/a.py",)


@dataclasses.dataclass(frozen=True)
class _Plan:
    dispatch_id: str
    route_reason: str = ""
    warnings: tuple = ()


def _spec(dispatch_id: str, *, paths=SHARED) -> SimpleNamespace:
    return SimpleNamespace(
        dispatch_id=dispatch_id, project_id=PID, track_id=None, gate=None, target_slot="T1", pr_id="1",
        role="backend-developer", dispatch_paths=tuple(DispatchPath(PurePosixPath(p)) for p in paths))


def _stage_spec(data_dir: Path, dispatch_id: str, paths=SHARED) -> None:
    """Stage the first fire's spec where blast_radius_sources reads a live dispatch's paths."""
    spec_dir = data_dir / "dispatches" / "trk" / dispatch_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "dispatch-spec.json").write_text(
        json.dumps({"dispatch_paths": [{"path": p} for p in paths]}), encoding="utf-8")


def _door(monkeypatch, tmp_path: Path, first_holds, release):
    """run_dispatch with every collaborator but the check, the claim and the section replaced.

    The gate, the claim (``_persist_dispatch_row``) and ``blast_radius_gate.section`` are
    the real ones: the first fire parks between its check and its claim on *first_holds*
    while it holds the section, and continues on *release*.
    """
    repo_root, data_dir = tmp_path, tmp_path / "data"
    state_dir = data_dir / "state"
    (tmp_path / "scripts").mkdir()
    _stage_spec(data_dir, FIRST)
    specs = {"/x/first.json": _spec(FIRST), "/x/second.json": _spec(SECOND)}
    files = {p: SimpleNamespace(spec=s) for p, s in specs.items()}
    monkeypatch.setattr(crt, "get", lambda key: "refuse" if key == brg.MODE_KEY else None)
    monkeypatch.setattr(brs, "open_pr_subjects", lambda *a, **k: brs.Sources())
    # A store with the ADR-007 project_id column and no in-flight rows: the first fire
    # must read "clear" so it can reach its claim.
    dispatch_cli._rc_init_schema(state_dir)
    project_id_migration.run_runtime_coordination_migration(
        coordination_db.db_path_from_state_dir(state_dir), default_project_id=PID)

    real_persist = dispatch_cli._persist_dispatch_row
    real_reject = dispatch_cli._blast_radius_reject
    checks: list = []

    def reject(spec, *a):
        out = real_reject(spec, *a)
        checks.append((spec.dispatch_id, out))
        return out

    def persist(spec, **kw):
        if spec.dispatch_id == FIRST:
            first_holds.set()
            if not release.wait(10):
                raise AssertionError("the test never released the first fire")
        return real_persist(spec, **kw)

    @contextlib.contextmanager
    def serialize_lane(cls, *, dispatch_id):
        yield

    def no_op(*a, **k):
        return None

    patches = {
        "_resolve_door_authority": lambda spec_file: (PID, repo_root, data_dir, state_dir),
        "_load_spec_with_track_override": lambda spec_file, track_id_override: specs[str(spec_file)],
        "_refire_guard_reject": lambda *a, **k: None,
        "_route_provider_pre_validate": lambda spec: (None, spec, "door"),
        "validate": lambda spec, **k: files[next(p for p, s in specs.items() if s is spec)],
        "_fill_gate_and_check_required": lambda vspec, reason: (None, vspec, "door2"),
        "build_runtime_snapshot": lambda *a, **k: SimpleNamespace(worker_claude_override_reason=None),
        "_compile_plan_checked": lambda vspec, snapshot: _Plan(vspec.spec.dispatch_id),
        "_headless_isolation_guard": lambda plan: None,
        "_lane_verdict": lambda *a, **k: None,
        "_run_scout_prepass": no_op,
        "_blast_radius_reject": reject,
        "_persist_dispatch_row": persist,
        "_register_gate_obligation": lambda *a, **k: None,
        "_export_worker_env": no_op,
        "issue_permit": lambda plan: "P",
        "require_permit": no_op,
        "fingerprint": lambda permit: "fp-1",
        "_commit_fire": lambda *a, **k: False,
        "_door_lifecycle_sweep": no_op,
        "_owner_hand_over": lambda *a, **k: ("H", "base"),
        "serialize_lane": serialize_lane,
        "_run_lane": lambda *a, **k: 0,
        "_owner_finish": no_op,
        "_finalize_door_exit": no_op,
    }
    for name, fn in patches.items():
        monkeypatch.setattr(dispatch_cli, name, fn)
    return state_dir, checks


def test_two_fires_in_the_same_second_serialise_the_check_and_the_claim(monkeypatch, tmp_path):
    first_holds, release = threading.Event(), threading.Event()
    state_dir, checks = _door(monkeypatch, tmp_path, first_holds, release)
    results: dict = {}

    def fire(spec_file: str, key: str) -> None:
        results[key] = dispatch_cli.run_dispatch(Path(spec_file))

    first = threading.Thread(target=fire, args=("/x/first.json", "first"), daemon=True)
    first.start()
    assert first_holds.wait(10), "the first fire never reached its claim"

    second = threading.Thread(target=fire, args=("/x/second.json", "second"), daemon=True)
    second.start()
    time.sleep(0.3)
    # The first fire holds the section between its check and its claim; the second
    # cannot have run its check yet, because the check is inside the same section.
    assert [c[0] for c in checks] == [FIRST]

    release.set()
    first.join(10), second.join(10)
    assert not first.is_alive() and not second.is_alive()

    assert results == {"first": 0, "second": 1}
    assert [c[0] for c in checks] == [FIRST, SECOND]
    second_reject = checks[1][1]
    assert second_reject is not None and second_reject.code == "blast-radius-overlap"
    assert FIRST in second_reject.reason  # the second fire saw the first as `starting`

    # The claim really happened, and the refusal of the second fire is booked.
    with dispatch_cli._rc_get_connection(state_dir) as conn:
        state = conn.execute("SELECT state FROM dispatches WHERE dispatch_id = ?", (FIRST,)).fetchone()[0]
    assert state == "claimed"
    ledger = [json.loads(line) for line in (state_dir / brg.LEDGER).read_text().splitlines()]
    assert [(e["dispatch_id"], e["verdict"], e["outcome"]) for e in ledger] == [
        (SECOND, brg.OVERLAP, "refused")]


def test_the_section_is_released_when_the_check_raises(tmp_path):
    state_dir = tmp_path / "nested" / "state"  # the parent dirs do not exist yet
    with pytest.raises(RuntimeError):
        with brg.section(state_dir):
            raise RuntimeError("the check blew up")
    lock = state_dir / brg.LOCK_FILENAME
    assert lock.exists()
    with open(lock, "a+", encoding="utf-8") as fh:
        # A short non-blocking exclusive lock proves nothing holds it any more.
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def test_the_section_blocks_a_second_holder_until_the_first_exits(tmp_path):
    order: list = []
    first_in, release = threading.Event(), threading.Event()

    def holder() -> None:
        with brg.section(tmp_path):
            order.append("first-in")
            first_in.set()
            release.wait(10)
            order.append("first-out")

    def follower() -> None:
        with brg.section(tmp_path):
            order.append("second-in")

    first = threading.Thread(target=holder, daemon=True)
    first.start()
    assert first_in.wait(10)
    second = threading.Thread(target=follower, daemon=True)
    second.start()
    time.sleep(0.2)
    assert order == ["first-in"]  # the follower waits on the same lock file
    release.set()
    first.join(10), second.join(10)
    assert order == ["first-in", "first-out", "second-in"]
