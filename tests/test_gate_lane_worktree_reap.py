"""A gate seat's provider worktree must not outlive its seat — nor take a sibling's.

Field evidence 2026-10-24: eleven ``dispatch-{glm,kimi,deepseek}-gate-*``
worktrees left behind under ``.vnx-data/worktrees/``.  The provider-lane child
(``provider_dispatch.py``) creates its own isolated worktree and removes it in
its own ``finally``; a child SIGKILLed by ``subprocess.run(timeout=...)`` never
reaches that ``finally``, and the parent holds no handle to the tree.  The
parent-side fix is ``plan_gate_panel._run_provider_lane_child`` ->
``dispatch_worktree_isolation.reap_child_worktree_on_exit`` ->
``remove_dispatch_worktree_if_present``.

That reap must never reach a LIVE sibling's tree (codex, PR #2213 round 1): a
duplicate seat for the same dispatch id that exits on ``WorktreeOccupied`` (or
times out) leaves the parent's ``finally`` facing the original worker's tree.
Two guards: a tree that existed before the child ran is never reaped, and a
tree that appeared during the run is reaped only while the parent holds its
occupancy lock exclusively.

The workers here are real processes: ``_spawn_worker`` runs the real
``create_dispatch_worktree`` in a child of its own session, so its occupancy
lock is a real flock held by another process, and a SIGKILL releases it the way
the kernel does for a seat killed on timeout.

``test_timed_out_provider_seat_worktree_is_reaped`` is the round-1 red-run test.
The round-2 red-run tests are the two sibling tests and
``test_reap_reads_the_claim_registry_under_the_dispatchers_data_dir``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_worktree_isolation as dwi
import plan_gate_panel

# A worker that lives in its dispatch worktree: it creates the tree with the real
# allocator (claim + occupancy lock), leaves an untracked file in it — or commits
# it — moves into the tree like a real worker, reports the path and blocks on
# stdin until the test closes it.
_WORKER_SCRIPT = r"""
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import dispatch_worktree_isolation as dwi
root, dispatch_id, mode = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
wt = dwi.create_dispatch_worktree(dispatch_id, project_root=root)
(wt / "work.txt").write_text("real work\n")
if mode == "commit":
    for args in (("add", "work.txt"), ("commit", "-m", "real local work")):
        subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True)
os.chdir(wt)
print(wt, flush=True)
sys.stdin.read()
"""


def _init_git_repo_with_origin(tmp_path: Path) -> Path:
    """Bare origin + local clone with one commit; returns the local clone.

    Mirrors ``test_provider_dispatch_worktree_isolation``'s fixture so the real
    ``create_dispatch_worktree`` can resolve ``origin/main``.
    """
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
    (local / "README.md").write_text("init\n")
    for args in (
        ("checkout", "-b", "main"),
        ("config", "user.email", "test@test.local"),
        ("config", "user.name", "Test"),
        ("add", "README.md"),
        ("commit", "-m", "initial"),
        ("push", "-u", "origin", "main"),
    ):
        subprocess.run(["git", "-C", str(local), *args], check=True, capture_output=True)
    return local


def _point_state_at_tmp(tmp_path: Path, monkeypatch, name: str = "vnx-data") -> Path:
    """Pin the shared claim registry at a temp state root (never the real one)."""
    data_dir = tmp_path / name
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(data_dir / "state"))
    return data_dir


def _claims_dir(data_dir: Path) -> Path:
    return dwi.claims_dir_for_state_dir(data_dir.resolve() / "state")


def _claim_file(data_dir: Path, dispatch_id: str) -> Path:
    return _claims_dir(data_dir) / f"{dwi._sanitize_dispatch_id(dispatch_id)}.json"


def _dispatch_id_from_cmd(cmd) -> str:
    """The ``--dispatch-id`` the real provider_dispatch child would receive."""
    return cmd[cmd.index("--dispatch-id") + 1]


def _install_fake_seat(monkeypatch, fake_run) -> None:
    """Replace only plan_gate_panel's ``subprocess``, so the fake child's own
    git calls (``create_dispatch_worktree``) still run for real."""
    monkeypatch.setattr(
        plan_gate_panel,
        "subprocess",
        SimpleNamespace(run=fake_run, TimeoutExpired=subprocess.TimeoutExpired),
    )


def _dispatcher(data_dir: Path):
    return plan_gate_panel._make_default_dispatcher(str(data_dir), 1)


def _git(local: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(local), *args], capture_output=True, text=True, check=False,
    )


def _spawn_worker(local: Path, dispatch_id: str, *, mode: str = "hold", env=None):
    """Start a real worker process holding *dispatch_id*'s worktree; returns (proc, path).

    ``start_new_session`` gives it a process group of its own, as a sibling
    dispatch started by another gate run has — ``kill_worktree_processes``
    skips only the caller's own group.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _WORKER_SCRIPT, str(SCRIPTS_LIB), str(local), dispatch_id, mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True,
    )
    line = proc.stdout.readline().strip()
    if not line:
        proc.kill()
        _, err = proc.communicate()
        raise AssertionError(f"worker for {dispatch_id} never came up: {err}")
    return proc, Path(line)


def _kill_worker(proc: subprocess.Popen) -> None:
    """SIGKILL the worker: its finally never runs, the kernel drops its flock."""
    proc.kill()
    proc.communicate()


def _stop_worker(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.communicate(input="", timeout=30)


def _assert_sibling_untouched(proc, wt: Path, local: Path, data_dir: Path, dispatch_id: str, claim_before: str):
    assert proc.poll() is None, "the live sibling worker was killed"
    assert wt.exists(), f"the live sibling's worktree was reaped: {wt}"
    assert (wt / "work.txt").read_text() == "real work\n", "the sibling's file was reset away"
    assert dwi.probe_occupancy(_claims_dir(data_dir), dispatch_id) == "held"
    assert _claim_file(data_dir, dispatch_id).read_text() == claim_before
    assert str(wt) in _git(local, "worktree", "list", "--porcelain").stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    local = _init_git_repo_with_origin(tmp_path)
    data_dir = _point_state_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(dwi, "resolve_consumer_project_root", lambda: local)
    return SimpleNamespace(local=local, data_dir=data_dir)


def test_timed_out_provider_seat_worktree_is_reaped(repo, monkeypatch, caplog):
    created = {}

    def fake_run(cmd, **kwargs):
        # Like provider_dispatch: create the isolated worktree from the
        # --dispatch-id handed over, then be SIGKILLed by the parent's timeout.
        proc, created["wt"] = _spawn_worker(repo.local, _dispatch_id_from_cmd(cmd))
        _kill_worker(proc)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    _install_fake_seat(monkeypatch, fake_run)

    with caplog.at_level(logging.INFO, logger="dispatch_worktree_isolation"):
        with pytest.raises(subprocess.TimeoutExpired):
            _dispatcher(repo.data_dir)("glm", "glm-4.6", "instruction", "glm-gate-timeout-1")

    wt_path = created["wt"]
    assert not wt_path.exists(), f"gate seat's worktree survived its own kill: {wt_path}"
    listed = _git(repo.local, "worktree", "list", "--porcelain").stdout
    assert str(wt_path) not in listed
    assert not _claim_file(repo.data_dir, "glm-gate-timeout-1").exists()
    assert (
        f"parent-side reap of dispatch glm-gate-timeout-1 (worktree {wt_path}): classified clean"
        in caplog.text
    )


@pytest.mark.parametrize("child_outcome", ["exits_on_worktree_occupied", "times_out"])
def test_duplicate_seat_never_reaps_a_live_siblings_existing_tree(repo, monkeypatch, child_outcome):
    dispatch_id = "glm-gate-dup-1"
    sibling, wt = _spawn_worker(repo.local, dispatch_id)
    try:
        claim_before = _claim_file(repo.data_dir, dispatch_id).read_text()

        def fake_run(cmd, **kwargs):
            # The duplicate child: the real allocator refuses it the live tree.
            with pytest.raises(dwi.WorktreeOccupied):
                dwi.create_dispatch_worktree(_dispatch_id_from_cmd(cmd), project_root=repo.local)
            if child_outcome == "times_out":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 1, "", "WorktreeOccupied")

        _install_fake_seat(monkeypatch, fake_run)
        expected = subprocess.TimeoutExpired if child_outcome == "times_out" else RuntimeError
        with pytest.raises(expected):
            _dispatcher(repo.data_dir)("glm", "glm-4.6", "instruction", dispatch_id)

        _assert_sibling_untouched(sibling, wt, repo.local, repo.data_dir, dispatch_id, claim_before)
    finally:
        _stop_worker(sibling)


def test_sibling_that_creates_the_tree_mid_run_keeps_it(repo, monkeypatch, caplog):
    dispatch_id = "glm-gate-race-1"
    holder = {}

    def fake_run(cmd, **kwargs):
        # The tree is absent when the parent looks; a sibling wins the race to
        # create it while this duplicate child runs, and still holds it.
        holder["proc"], holder["wt"] = _spawn_worker(repo.local, dispatch_id)
        holder["claim"] = _claim_file(repo.data_dir, dispatch_id).read_text()
        with pytest.raises(dwi.WorktreeOccupied):
            dwi.create_dispatch_worktree(_dispatch_id_from_cmd(cmd), project_root=repo.local)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    _install_fake_seat(monkeypatch, fake_run)
    try:
        with caplog.at_level(logging.INFO, logger="dispatch_worktree_isolation"):
            with pytest.raises(subprocess.TimeoutExpired):
                _dispatcher(repo.data_dir)("glm", "glm-4.6", "instruction", dispatch_id)

        _assert_sibling_untouched(
            holder["proc"], holder["wt"], repo.local, repo.data_dir, dispatch_id, holder["claim"],
        )
        assert f"parent-side reap skipped for dispatch {dispatch_id}" in caplog.text
        assert "is held by a live process" in caplog.text
    finally:
        if "proc" in holder:
            _stop_worker(holder["proc"])


def test_tree_that_existed_before_the_run_is_never_reaped(repo, monkeypatch, caplog):
    dispatch_id = "glm-gate-preexisting-1"
    worker, wt = _spawn_worker(repo.local, dispatch_id)
    _kill_worker(worker)
    # Nobody holds the lock any more and the claim still names this dispatch:
    # only the before-the-run record keeps the parent's hands off.
    assert dwi.probe_occupancy(_claims_dir(repo.data_dir), dispatch_id) == "released"

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    _install_fake_seat(monkeypatch, fake_run)
    with caplog.at_level(logging.INFO, logger="dispatch_worktree_isolation"):
        with pytest.raises(subprocess.TimeoutExpired):
            _dispatcher(repo.data_dir)("glm", "glm-4.6", "instruction", dispatch_id)

    assert wt.exists(), f"a tree this run did not create was reaped: {wt}"
    assert (wt / "work.txt").read_text() == "real work\n"
    assert _claim_file(repo.data_dir, dispatch_id).exists()
    assert "existed before the run" in caplog.text


def test_reap_reads_the_claim_registry_under_the_dispatchers_data_dir(tmp_path, monkeypatch):
    local = _init_git_repo_with_origin(tmp_path)
    parent_data = _point_state_at_tmp(tmp_path, monkeypatch, name="parent-data")
    monkeypatch.setattr(dwi, "resolve_consumer_project_root", lambda: local)
    dispatcher_data = tmp_path / "dispatcher-data"
    dispatch_id = "kimi-gate-datadir-1"
    created = {}

    def fake_run(cmd, **kwargs):
        # The worker gets exactly the env the dispatcher hands its child: its
        # VNX_DATA_DIR is the dispatcher's data dir, not this process's.
        proc, created["wt"] = _spawn_worker(local, dispatch_id, env=kwargs["env"])
        created["claim"] = _claim_file(dispatcher_data, dispatch_id).exists()
        _kill_worker(proc)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    _install_fake_seat(monkeypatch, fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        _dispatcher(dispatcher_data)("kimi", "kimi-k2", "instruction", dispatch_id)

    assert created["claim"], "the worker did not claim under the dispatcher's data dir"
    assert not created["wt"].exists(), "the reap looked in the wrong registry and skipped"
    assert not _claim_file(dispatcher_data, dispatch_id).exists(), "the child's claim was left behind"
    assert not _claims_dir(parent_data).exists(), "the reap wrote into the parent's registry"

    # The existing teardown event fires, unchanged, under the documented
    # parent terminal in the dispatcher's data dir.
    stream = dispatcher_data / "events" / f"{plan_gate_panel._PARENT_REAP_TERMINAL_ID}.ndjson"
    events = [json.loads(line) for line in stream.read_text().splitlines()]
    assert [e["type"] for e in events] == ["provider_teardown_worktree"]
    assert events[0]["dispatch_id"] == dispatch_id
    assert events[0]["terminal"] == "plan-gate-parent"
    assert set(events[0]["data"]) == {
        "worktree_state", "branch_kept_local", "branch_kept_remote", "preserved_path",
        "fabric_version_at_create", "fabric_version_at_teardown",
        "unpushed_local_commits_at_create",
    }
    assert events[0]["data"]["worktree_state"] == "clean"
    assert not (parent_data / "events").exists()


def _abandon(repo, dispatch_id: str) -> Path:
    """A tree whose worker died: tree, claim and lock file on disk, lock free."""
    worker, wt = _spawn_worker(repo.local, dispatch_id)
    _kill_worker(worker)
    return wt


def _break_lock_file(repo, dispatch_id, wt):
    (_claims_dir(repo.data_dir) / f"{dwi._sanitize_dispatch_id(dispatch_id)}.occupancy").unlink()


def _break_claim_missing(repo, dispatch_id, wt):
    _claim_file(repo.data_dir, dispatch_id).unlink()


def _break_claim_malformed(repo, dispatch_id, wt):
    _claim_file(repo.data_dir, dispatch_id).write_text("{not json")


def _break_claim_other_identity(repo, dispatch_id, wt):
    path = _claim_file(repo.data_dir, dispatch_id)
    claim = json.loads(path.read_text())
    claim["dispatch_id"] = "someone-else"
    path.write_text(json.dumps(claim))


def _break_claim_other_path(repo, dispatch_id, wt):
    path = _claim_file(repo.data_dir, dispatch_id)
    claim = json.loads(path.read_text())
    claim["worktree_path"] = str(wt.parent / "dispatch-elsewhere")
    path.write_text(json.dumps(claim))


@pytest.mark.parametrize(
    "break_evidence, reason",
    [
        (_break_lock_file, "no occupancy record"),
        (_break_claim_missing, "no claim for this dispatch"),
        (_break_claim_malformed, "no claim for this dispatch"),
        (_break_claim_other_identity, "the claim refuses this identity"),
        (_break_claim_other_path, "the claim refuses this identity"),
    ],
    ids=["lock-file-missing", "claim-missing", "claim-malformed", "claim-other-id", "claim-other-path"],
)
def test_reap_without_positive_evidence_leaves_the_tree(repo, monkeypatch, caplog, break_evidence, reason):
    dispatch_id = "glm-gate-evidence-1"
    wt = _abandon(repo, dispatch_id)
    break_evidence(repo, dispatch_id, wt)
    teardowns = []
    monkeypatch.setattr(dwi, "remove_dispatch_worktree", lambda *a, **k: teardowns.append(a))

    with caplog.at_level(logging.INFO, logger="dispatch_worktree_isolation"):
        assert dwi.remove_dispatch_worktree_if_present(
            dispatch_id, project_root=repo.local, data_dir=repo.data_dir,
        ) is False

    assert teardowns == []
    assert wt.exists()
    assert reason in caplog.text
    assert dispatch_id in caplog.text and str(wt) in caplog.text


def test_reap_skips_a_tree_this_process_itself_occupies(repo, monkeypatch, caplog):
    dispatch_id = "glm-gate-self-1"
    wt = dwi.create_dispatch_worktree(dispatch_id, project_root=repo.local)
    teardowns = []
    monkeypatch.setattr(dwi, "remove_dispatch_worktree", lambda *a, **k: teardowns.append(a))
    try:
        with caplog.at_level(logging.INFO, logger="dispatch_worktree_isolation"):
            assert dwi.remove_dispatch_worktree_if_present(
                dispatch_id, project_root=repo.local, data_dir=repo.data_dir,
            ) is False
        assert teardowns == []
        assert wt.exists()
        assert "this process itself occupies the worktree" in caplog.text
    finally:
        dwi._release_occupancy(wt)


def test_reap_holds_the_lock_through_teardown_and_releases_it(repo, monkeypatch):
    dispatch_id = "glm-gate-hold-1"
    wt = _abandon(repo, dispatch_id)
    real_remove = dwi.remove_dispatch_worktree
    seen = {}

    def observing_remove(*a, **k):
        # A sibling re-entering the same dispatch id mid-teardown is refused.
        with pytest.raises(dwi.WorktreeOccupied):
            dwi.create_dispatch_worktree(dispatch_id, project_root=repo.local)
        seen["during"] = dwi.probe_occupancy(_claims_dir(repo.data_dir), dispatch_id)
        return real_remove(*a, **k)

    monkeypatch.setattr(dwi, "remove_dispatch_worktree", observing_remove)
    assert dwi.remove_dispatch_worktree_if_present(
        dispatch_id, project_root=repo.local, data_dir=repo.data_dir,
    ) is True

    assert seen["during"] == "held"
    assert not wt.exists()
    assert dwi.probe_occupancy(_claims_dir(repo.data_dir), dispatch_id) == "released"


def test_absent_worktree_returns_false_and_runs_no_git(tmp_path, monkeypatch):
    local = _init_git_repo_with_origin(tmp_path)
    _point_state_at_tmp(tmp_path, monkeypatch)

    git_calls = []
    real_run = subprocess.run

    def counting_run(args, *a, **kwargs):
        git_calls.append(list(args))
        return real_run(args, *a, **kwargs)

    monkeypatch.setattr(dwi.subprocess, "run", counting_run)
    teardowns = []
    monkeypatch.setattr(
        dwi, "remove_dispatch_worktree",
        lambda *a, **kwargs: teardowns.append((a, kwargs)),
    )

    assert dwi.remove_dispatch_worktree_if_present(
        "never-created-gate", project_root=local,
    ) is False
    assert git_calls == [], f"absent worktree must cost no git call: {git_calls}"
    assert teardowns == [], "teardown must not run for an absent worktree"


def test_local_commit_survives_a_timed_out_seat(repo, monkeypatch):
    created = {}

    def fake_run(cmd, **kwargs):
        proc, wt_path = _spawn_worker(repo.local, _dispatch_id_from_cmd(cmd), mode="commit")
        created["head"] = _git(wt_path, "rev-parse", "HEAD").stdout.strip()
        _kill_worker(proc)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    _install_fake_seat(monkeypatch, fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        _dispatcher(repo.data_dir)("kimi", "kimi-k2", "instruction", "kimi-gate-commit-1")

    branch = f"dispatch/{dwi._sanitize_dispatch_id('kimi-gate-commit-1')}"
    head = _git(repo.local, "rev-parse", branch)
    assert head.returncode == 0, f"committed work lost: {branch} does not resolve: {head.stderr}"
    assert head.stdout.strip() == created["head"]
    assert _git(repo.local, "show", f"{branch}:work.txt").stdout == "real work\n"


def test_helper_swallows_a_teardown_exception(repo, monkeypatch, caplog):
    dispatch_id = "glm-gate-boom"
    wt_path = _abandon(repo, dispatch_id)

    def boom(*_a, **_kwargs):
        raise RuntimeError("teardown exploded")

    monkeypatch.setattr(dwi, "remove_dispatch_worktree", boom)

    with caplog.at_level(logging.WARNING, logger="dispatch_worktree_isolation"):
        assert dwi.remove_dispatch_worktree_if_present(
            dispatch_id, project_root=repo.local, data_dir=repo.data_dir,
        ) is False
    assert dispatch_id in caplog.text
    assert str(wt_path) in caplog.text
    assert dwi.probe_occupancy(_claims_dir(repo.data_dir), dispatch_id) == "released"


def test_timeout_still_propagates_when_teardown_raises(repo, monkeypatch):
    created = {}

    def fake_run(cmd, **kwargs):
        proc, created["wt"] = _spawn_worker(repo.local, _dispatch_id_from_cmd(cmd))
        _kill_worker(proc)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    _install_fake_seat(monkeypatch, fake_run)
    teardowns = []

    def boom(*a, **_kwargs):
        teardowns.append(a)
        raise RuntimeError("teardown exploded")

    monkeypatch.setattr(dwi, "remove_dispatch_worktree", boom)

    with pytest.raises(subprocess.TimeoutExpired):
        _dispatcher(repo.data_dir)("glm", "glm-4.6", "instruction", "glm-gate-boom-2")
    assert teardowns, "the reap never reached the teardown, so its exception path is untested"
    assert dwi.probe_occupancy(_claims_dir(repo.data_dir), "glm-gate-boom-2") == "released"


def test_normally_exiting_seat_costs_no_second_teardown(repo, monkeypatch):
    monkeypatch.setattr(plan_gate_panel, "_read_report", lambda *a, **k: "panel seat analysis")
    real_remove = dwi.remove_dispatch_worktree

    def fake_run(cmd, **kwargs):
        dispatch_id = _dispatch_id_from_cmd(cmd)
        wt = dwi.create_dispatch_worktree(dispatch_id, project_root=repo.local)
        real_remove(dispatch_id, project_root=repo.local)  # the child's own finally
        assert not wt.exists()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    _install_fake_seat(monkeypatch, fake_run)
    teardowns = []
    monkeypatch.setattr(dwi, "remove_dispatch_worktree", lambda *a, **k: teardowns.append(a))

    assert _dispatcher(repo.data_dir)("glm", "glm-4.6", "prompt", "glm-gate-clean-1") == (
        "panel seat analysis"
    )
    assert teardowns == [], "the parent must not re-tear-down a tree the child already removed"


def test_unmeasurable_path_before_the_run_is_never_reaped(repo, monkeypatch, caplog):
    def unresolvable():
        raise RuntimeError("no project root")

    monkeypatch.setattr(dwi, "resolve_consumer_project_root", unresolvable)
    reaps = []
    monkeypatch.setattr(dwi, "remove_dispatch_worktree_if_present", lambda *a, **k: reaps.append(a))

    with caplog.at_level(logging.INFO, logger="dispatch_worktree_isolation"):
        with dwi.reap_child_worktree_on_exit(
            "glm-gate-unmeasured-1", data_dir=repo.data_dir, terminal_id="plan-gate-parent",
        ):
            pass

    assert reaps == []
    assert "could not be measured before the run" in caplog.text
