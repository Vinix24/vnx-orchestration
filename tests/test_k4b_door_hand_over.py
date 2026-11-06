"""ADR-045 audit-spoor-kern K-4b: the door calls the hand-over.

Dispatch-ID: dlv-ddbfffc4b9fb

K-4a added ``dispatch_start.begin``; nothing called it. K-4b wires it into the
single-entry door (``dispatch_cli.run_dispatch``): one ``begin`` call per lane,
after the plan is validated and before the serialization slot and the spawn,
with ONE base sha resolved in the consumer repo and handed both to the start
line and to worktree creation. Every test drives the real door against real
stores under ``tmp_path``; the lane executors and the slot are spied on.

Red on the K-4a head: nothing there calls ``begin`` (no ``dispatch_started``
line, no start-line refusal, no consumer-repo resolution).

OI-2549: a test that drives the door against the REAL repository must neutralise
the door's own ``fetch_remote_base`` (``_patched_fetch`` below). Otherwise the
door runs a live ``git fetch origin main`` in this checkout, and a merge landing
on main while the suite runs moves the ref between the test's expectation and
the hand-over — main-CI run 41996195419 failed exactly that way (expected
``cf377391``, recorded ``0c28ca7d``, PR #2389 merged mid-run). The tests that
build their own clone in ``tmp_path`` and prove the fetch on purpose (§6) keep
the real fetch: their origin is a disposable local bare repo.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import dispatch_cli  # noqa: E402
import dispatch_start  # noqa: E402
import project_id_migration  # noqa: E402
import runtime_coordination as rc  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from dispatch_plan import ModelPin, RuntimeSnapshot  # noqa: E402
from dispatch_worktree_isolation import (  # noqa: E402
    create_dispatch_worktree,
    resolve_consumer_project_root,
)
from receipt_schema import DISPATCH_STARTED_EVENT_TYPE  # noqa: E402

PROJECT = "vnx-dev"
STAGED = "k4b"
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "vnx-test",
    "GIT_AUTHOR_EMAIL": "vnx-test@example.invalid",
    "GIT_COMMITTER_NAME": "vnx-test",
    "GIT_COMMITTER_EMAIL": "vnx-test@example.invalid",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    """Keep the append layer real, but never fire its process-spawning hooks."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


def _clean_snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        constraint_verdicts=(),
        staging_promoted=True,
        target_health={"ephemeral": "healthy", "T1": "healthy"},
        target_capable={"ephemeral": True, "T1": True},
        model_pins={
            "T0": ModelPin(model="opus", semantics="floor"),
            "T1": ModelPin(model="sonnet", semantics="floor"),
            "T2": ModelPin(model="sonnet", semantics="floor"),
            "T3": ModelPin(model="sonnet", semantics="floor"),
        },
    )


def _make_coordination_db(state_dir: Path) -> Path:
    """The production-shaped coordination store (project_id + composite UNIQUE)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    coordination_db.init_schema(state_dir)
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    return db_path


def _make_bundle(
    tmp_path: Path, *, dispatch_id: str, base_ref: str = "origin/main",
) -> "tuple[Path, Path]":
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / f"staging-{dispatch_id}"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text("Do something useful.", encoding="utf-8")
    spec = {
        "schema_version": 1,
        "project_id": PROJECT,
        "dispatch_id": dispatch_id,
        "staging_id": f"staging-{dispatch_id}",
        "instruction_file": str(instruction),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
        "base_ref": base_ref,
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _start_lines(state_dir: Path) -> list:
    ledger = state_dir / "t0_receipts.ndjson"
    if not ledger.is_file():
        return []
    lines = []
    for raw in ledger.read_text(encoding="utf-8").splitlines():
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(line, dict) and line.get("event_type") == DISPATCH_STARTED_EVENT_TYPE:
            lines.append(line)
    return lines


def _seed_live_attempt(state_dir: Path, *, dispatch_id: str, attempt_number: int = 1) -> str:
    """A pre-existing live attempt of the same dispatch, owned by nobody in this door run."""
    with rc.get_connection(state_dir) as conn:
        rc.register_dispatch(conn, dispatch_id=dispatch_id, project_id=PROJECT, actor="test")
        rc.increment_attempt_count(conn, dispatch_id)
        row = rc.create_attempt(
            conn, dispatch_id=dispatch_id, project_id=PROJECT, terminal_id="T0",
            attempt_number=attempt_number, actor="test",
        )
        rc.update_attempt(conn, attempt_id=row["attempt_id"], state="running", actor="test")
        conn.commit()
    return row["attempt_id"]


def _attempt_rows(state_dir: Path, dispatch_id: str) -> "dict[int, dict]":
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM dispatch_attempts WHERE dispatch_id = ? ORDER BY attempt_number",
        (dispatch_id,),
    ).fetchall()
    conn.close()
    return {row["attempt_number"]: dict(row) for row in rows}


def _dispatch_row(state_dir: Path, dispatch_id: str) -> dict:
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM dispatches WHERE dispatch_id = ?", (dispatch_id,)).fetchone()
    conn.close()
    assert row is not None, f"no dispatches row for {dispatch_id!r}"
    return dict(row)


@contextlib.contextmanager
def _slot_spy(state_dir: Path, seen: dict):
    seen["start_lines_at_slot"] = _start_lines(state_dir)
    yield


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, env=_GIT_ENV)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=path, check=True, env=_GIT_ENV)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True, env=_GIT_ENV)


def _rev_parse(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True, capture_output=True, text=True, env=_GIT_ENV,
    ).stdout.strip()


def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True, env=_GIT_ENV)
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=repo, check=True, env=_GIT_ENV)
    return _rev_parse(repo, "HEAD")


def _init_origin_and_clone(tmp_path: Path) -> "tuple[Path, Path]":
    """A real bare ``origin`` plus a consumer clone of it (fetchable, unpatchable)."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed-repo"
    consumer = tmp_path / "consumer-repo"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=_GIT_ENV)
    _init_repo(seed)
    subprocess.run(["git", "-C", str(seed), "branch", "-M", "main"], check=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)],
                   check=True, env=_GIT_ENV)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"],
                   check=True, env=_GIT_ENV)
    # Point the bare HEAD at main so the clone checks out a real branch instead
    # of warning about an unborn HEAD.
    subprocess.run(["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
                   check=True, env=_GIT_ENV)
    subprocess.run(["git", "clone", "-q", str(bare), str(consumer)], check=True, env=_GIT_ENV)
    return seed, consumer


def _push_main(seed: Path, name: str) -> str:
    """Advance ``main`` in the seed repo and push it to the bare origin."""
    sha = _commit(seed, name, f"{name} upstream\n")
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"],
                   check=True, env=_GIT_ENV)
    return sha


def _door_env(monkeypatch, data_dir: Path, *, lifecycle: bool) -> Path:
    state_dir = data_dir / "state"
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    if lifecycle:
        monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")
    else:
        monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")
    return state_dir


@contextlib.contextmanager
def _consumer_root(path: Path):
    with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=path):
        yield


@contextlib.contextmanager
def _patched_fetch():
    """OI-2549: keep the door's base resolution off the REAL origin.

    ``_resolve_start_base_sha`` calls ``dispatch_worktree_isolation``'s
    ``fetch_remote_base`` (imported inside the function, so patching the module
    attribute is where the door looks it up). Against the real checkout that is
    a live ``git fetch origin main``: a merge landing on main while the suite
    runs moves the ref between the test's own ``rev-parse`` and the hand-over,
    and the start line records a sha the test never expected (main-CI
    41996195419: ``cf377391`` expected, ``0c28ca7d`` recorded — PR #2389
    merged mid-run). Every caller asserts on the returned spy: ``called once
    with`` the consumer root and ``origin/main`` proves the patch landed on the
    call the door actually makes. §6's clone-based tests deliberately keep the
    real fetch — their origin is a disposable ``tmp_path`` bare repo.
    """
    with patch("dispatch_worktree_isolation.fetch_remote_base", return_value=False) as spy:
        yield spy


# ---------------------------------------------------------------------------
# 1. lifecycle on: one line, before the slot, with the consumer repo's base sha
# ---------------------------------------------------------------------------

def test_one_start_line_written_before_the_slot(tmp_path, monkeypatch):
    dispatch_id = "20261103-k4b-line"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    state_dir = data_dir / "state"
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")

    seen: dict = {}
    # The door resolves its base in the CONSUMER repo (OI-2510), not in the
    # engine checkout this file lives in — read the expectation from the same
    # root, so the test and the door can never name two different repos.
    consumer_root = resolve_consumer_project_root()
    expected_sha = _rev_parse(consumer_root, "origin/main")

    with _patched_fetch() as fetch_spy, \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli.serialize_lane", side_effect=lambda *a, **k: _slot_spy(state_dir, seen)), \
         patch("dispatch_cli._execute_claude_headless", return_value=0):
        assert dispatch_cli.run_dispatch(spec_file) == 0

    # OI-2549: the patch landed on the call the door really makes (consumer
    # root + origin/main) — and that call can no longer move the ref the
    # expectation above was read from.
    fetch_spy.assert_called_once_with(consumer_root, "origin/main")

    lines = _start_lines(state_dir)
    assert len(lines) == 1, lines
    assert lines[0]["dispatch_id"] == dispatch_id
    assert lines[0]["base_sha"] == expected_sha
    # The spy proves ORDER: the line was already durable when the slot opened.
    assert [line["dispatch_id"] for line in seen["start_lines_at_slot"]] == [dispatch_id]


# ---------------------------------------------------------------------------
# 2. one base, resolved in the consumer repo, handed to begin and the worktree
# ---------------------------------------------------------------------------

def test_base_resolves_in_the_consumer_repo_and_the_worktree_uses_it(tmp_path, monkeypatch):
    dispatch_id = "20261103-k4b-consumer"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, base_ref="consumer-only")
    state_dir = data_dir / "state"
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")

    consumer = tmp_path / "consumer-repo"
    _init_repo(consumer)
    consumer_sha = _commit(consumer, "consumer.txt", "consumer branch\n")
    subprocess.run(["git", "-C", str(consumer), "branch", "consumer-only", consumer_sha],
                   check=True, env=_GIT_ENV)
    # Advance the branch the worktree would otherwise drift to.
    _commit(consumer, "later.txt", "later\n")

    # The engine checkout (dispatch_cli.__file__) must NOT know this branch.
    engine_root = Path(dispatch_cli.__file__).resolve().parents[2]
    probe = subprocess.run(
        ["git", "-C", str(engine_root), "rev-parse", "--verify", "-q", "consumer-only^{commit}"],
        capture_output=True, text=True,
    )
    assert probe.returncode != 0, "fixture: the engine checkout must not carry the consumer branch"

    with patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=consumer), \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless:
        assert dispatch_cli.run_dispatch(spec_file) == 0

    # (a) the hand-over recorded the consumer repo's commit, not the engine's
    lines = _start_lines(state_dir)
    assert len(lines) == 1, lines
    assert lines[0]["base_sha"] == consumer_sha
    # (b) the SAME sha travelled to the lane
    assert headless.call_args.kwargs["base_sha"] == consumer_sha

    # (c) the worktree is built on exactly that commit when handed the sha
    wt = create_dispatch_worktree(dispatch_id, project_root=consumer, base_ref=consumer_sha)
    assert _rev_parse(wt, "HEAD") == consumer_sha


def test_unresolvable_ref_refuses_before_the_hand_over(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261103-k4b-noref"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, base_ref="not-a-ref-anywhere")
    state_dir = data_dir / "state"
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")

    seen: dict = {}
    with _patched_fetch() as fetch_spy, \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli.serialize_lane", side_effect=lambda *a, **k: _slot_spy(state_dir, seen)), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless:
        assert dispatch_cli.run_dispatch(spec_file) == 1

    # A non-``origin/`` ref is never fetched (OI-2549: the door is on the real
    # checkout here, so this also proves no live fetch can fire behind it).
    fetch_spy.assert_not_called()
    assert "start_lines_at_slot" not in seen, "a refused base must never reach the slot"
    headless.assert_not_called()
    assert _start_lines(state_dir) == []
    err = capsys.readouterr().err
    assert "REJECT [start-line-refused]: base_ref_unresolved" in err, err


# ---------------------------------------------------------------------------
# 3. a forced StartRefused: nothing in flight, this door's attempt closed
# ---------------------------------------------------------------------------

def test_refused_start_closes_only_this_doors_attempt(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261103-k4b-refused"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    state_dir = data_dir / "state"
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")

    live_attempt_id = _seed_live_attempt(state_dir, dispatch_id=dispatch_id, attempt_number=1)

    entered = {"slot": False}

    @contextlib.contextmanager
    def _slot(serialization_class, *, dispatch_id):
        entered["slot"] = True
        yield

    with _patched_fetch() as fetch_spy, \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli.serialize_lane", _slot), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless, \
         patch("dispatch_start.begin",
               side_effect=dispatch_start.StartRefused("start_test_forced", "forced by the test")):
        assert dispatch_cli.run_dispatch(spec_file) == 1

    # OI-2549: the base is resolved (and fetched) before ``begin`` is forced to
    # refuse — patch that fetch, never the real origin.
    fetch_spy.assert_called_once_with(resolve_consumer_project_root(), "origin/main")
    assert entered["slot"] is False, "a refused start must not take the slot"
    headless.assert_not_called()
    assert "REJECT [start-line-refused]: start_test_forced: forced by the test" in capsys.readouterr().err

    attempts = _attempt_rows(state_dir, dispatch_id)
    # The door created attempt 2 (attempt 1 was already live) and closed exactly it.
    assert attempts[2]["state"] == "failed"
    assert attempts[2]["failure_reason"] == "start_test_forced"
    # The other live attempt of the same dispatch is untouched.
    assert attempts[1]["attempt_id"] == live_attempt_id
    assert attempts[1]["state"] == "running"
    assert attempts[1]["failure_reason"] is None
    assert _dispatch_row(state_dir, dispatch_id)["state"] == "failed_delivery"


# ---------------------------------------------------------------------------
# 4. mode_write: write_failed is named, the dispatch goes on
# ---------------------------------------------------------------------------

def test_failed_projection_is_named_and_the_dispatch_proceeds(tmp_path, monkeypatch, capsys):
    dispatch_id = "20261103-k4b-writefail"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    state_dir = data_dir / "state"
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")

    failed = dispatch_start.Handover(
        project_id=PROJECT, dispatch_id=dispatch_id, attempt=1,
        lifecycle_mode=dispatch_start.MODE_LEGACY, door_pid=os.getpid(),
        door_started_at="", started_at="2026-11-03T00:00:00Z",
        mode_write=dispatch_start.MODE_WRITE_FAILED,
    )
    entered = {"slot": False}

    @contextlib.contextmanager
    def _slot(serialization_class, *, dispatch_id):
        entered["slot"] = True
        yield

    with _patched_fetch() as fetch_spy, \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli.serialize_lane", _slot), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless, \
         patch("dispatch_start.begin", return_value=failed):
        assert dispatch_cli.run_dispatch(spec_file) == 0

    fetch_spy.assert_called_once_with(resolve_consumer_project_root(), "origin/main")
    assert entered["slot"] is True
    headless.assert_called_once()
    err = capsys.readouterr().err
    assert "hand-over row projection write_failed" in err, err
    assert "fire continues" in err, err


# ---------------------------------------------------------------------------
# 5. ledger_lifecycle off: nothing on the ledger, decision/receipts/rc unchanged
# ---------------------------------------------------------------------------

def test_legacy_writes_no_line_and_keeps_main_s_decision(tmp_path, monkeypatch):
    dispatch_id = "20261103-k4b-legacy"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id)
    state_dir = data_dir / "state"
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")

    with _patched_fetch() as fetch_spy, \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless:
        assert dispatch_cli.run_dispatch(spec_file) == 0

    # The legacy path resolves (and so fetches) the base too — off the real origin.
    fetch_spy.assert_called_once_with(resolve_consumer_project_root(), "origin/main")
    headless.assert_called_once()
    # No lifecycle line and no lifecycle receipt anywhere on the ledger.
    assert _start_lines(state_dir) == []

    # The decision object the door writes is the plan's canonical dict — the
    # hand-over adds no field to it (K-4a-head parity).
    decision_path = state_dir / "route_decisions" / f"{dispatch_id}.json"
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    assert decision["dispatch_id"] == dispatch_id
    assert decision["decision"]["dispatch_id"] == dispatch_id
    assert decision["decision"]["lane"] == "claude_headless"
    assert decision["decision"]["base_ref"] == "origin/main"
    assert "lifecycle_mode" not in json.dumps(decision["decision"]), (
        "the hand-over must never leak into the route decision"
    )

    # The legacy hand-over still projects lifecycle_mode: legacy into the row.
    metadata = json.loads(_dispatch_row(state_dir, dispatch_id)["metadata_json"] or "{}")
    assert metadata.get("lifecycle_mode") == "legacy"


# ---------------------------------------------------------------------------
# 6. base freshness — the fetch that create_dispatch_worktree used to run
#    (codex on 30b21e0f: the door resolved the base before any fetch, but it
#    now hands the worktree a full sha, so the worktree's own fetch never runs)
# ---------------------------------------------------------------------------

def test_advanced_origin_main_is_fetched_before_it_is_resolved(tmp_path, monkeypatch):
    """origin/main moves upstream after the clone: the door must start on the new commit."""
    dispatch_id = "20261103-k4b-fresh-main"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, base_ref="origin/main")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)

    seed, consumer = _init_origin_and_clone(tmp_path)
    stale_sha = _rev_parse(consumer, "origin/main")
    fresh_sha = _push_main(seed, "upstream-moved.txt")
    assert fresh_sha != stale_sha
    assert _rev_parse(consumer, "origin/main") == stale_sha, (
        "fixture: the clone's local origin/main must still be stale"
    )

    import dispatch_worktree_isolation as dwi

    real_run = dwi.subprocess.run
    seen: "list[list[str]]" = []

    def _record(cmd, **kwargs):
        seen.append([str(part) for part in cmd] if not isinstance(cmd, str) else [cmd])
        return real_run(cmd, **kwargs)

    with _consumer_root(consumer), \
         patch("dispatch_worktree_isolation.subprocess.run", side_effect=_record), \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless:
        assert dispatch_cli.run_dispatch(spec_file) == 0

    # The door fetched the branch, in the consumer repo, before resolving it.
    assert ["git", "fetch", "origin", "main"] in seen, seen

    lines = _start_lines(state_dir)
    assert len(lines) == 1, lines
    assert lines[0]["base_sha"] == fresh_sha, "the start line must name the fetched commit"
    assert headless.call_args.kwargs["base_sha"] == fresh_sha

    wt = create_dispatch_worktree(
        dispatch_id, project_root=consumer, base_ref=headless.call_args.kwargs["base_sha"],
    )
    assert _rev_parse(wt, "HEAD") == fresh_sha, "the worktree must be built on the fetched commit"


def test_branch_that_exists_only_upstream_is_fetched_and_resolved(tmp_path, monkeypatch):
    """A branch pushed after the clone exists only in the bare repo until the fetch."""
    dispatch_id = "20261103-k4b-fresh-branch"
    data_dir, spec_file = _make_bundle(
        tmp_path, dispatch_id=dispatch_id, base_ref="origin/upstream-only")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)

    seed, consumer = _init_origin_and_clone(tmp_path)
    subprocess.run(["git", "-C", str(seed), "checkout", "-q", "-b", "upstream-only"],
                   check=True, env=_GIT_ENV)
    branch_sha = _commit(seed, "upstream-only.txt", "only upstream\n")
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "upstream-only"],
                   check=True, env=_GIT_ENV)
    probe = subprocess.run(
        ["git", "-C", str(consumer), "rev-parse", "--verify", "-q", "origin/upstream-only^{commit}"],
        capture_output=True, text=True, env=_GIT_ENV,
    )
    assert probe.returncode != 0, "fixture: the clone must not know the branch before the fetch"

    with _consumer_root(consumer), \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless:
        assert dispatch_cli.run_dispatch(spec_file) == 0

    lines = _start_lines(state_dir)
    assert len(lines) == 1, lines
    assert lines[0]["base_sha"] == branch_sha
    assert headless.call_args.kwargs["base_sha"] == branch_sha

    wt = create_dispatch_worktree(dispatch_id, project_root=consumer, base_ref=branch_sha)
    assert _rev_parse(wt, "HEAD") == branch_sha


def test_full_sha_base_is_not_fetched(tmp_path, monkeypatch):
    """A full sha is already the answer: no fetch call, it is resolved verbatim."""
    dispatch_id = "20261103-k4b-fresh-sha"
    seed, consumer = _init_origin_and_clone(tmp_path)
    _push_main(seed, "moved-again.txt")
    sha = _rev_parse(consumer, "origin/main")
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, base_ref=sha)
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)

    with _consumer_root(consumer), \
         patch("dispatch_worktree_isolation.fetch_remote_base", create=True) as fetch_spy, \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless:
        assert dispatch_cli.run_dispatch(spec_file) == 0

    # create=True: the assertion also holds on the K-4b head, where the helper
    # does not exist yet (nothing there fetches at all).
    fetch_spy.assert_not_called()
    assert _start_lines(state_dir)[0]["base_sha"] == sha
    assert headless.call_args.kwargs["base_sha"] == sha


def test_legacy_path_has_the_same_freshness(tmp_path, monkeypatch):
    """ledger_lifecycle off: the legacy path must equal main — stale ref refreshed too."""
    dispatch_id = "20261103-k4b-legacy-fresh"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, base_ref="origin/main")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=False)

    seed, consumer = _init_origin_and_clone(tmp_path)
    stale_sha = _rev_parse(consumer, "origin/main")
    fresh_sha = _push_main(seed, "legacy-moved.txt")
    assert _rev_parse(consumer, "origin/main") == stale_sha

    with _consumer_root(consumer), \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless:
        assert dispatch_cli.run_dispatch(spec_file) == 0

    assert headless.call_args.kwargs["base_sha"] == fresh_sha
    # Still no lifecycle line: only the freshness changed.
    assert _start_lines(state_dir) == []


# ---------------------------------------------------------------------------
# 7. OI-2549 — a merge on main while the run is in flight
#    (main-CI 41996195419 ran on cf377391; #2389 merged at 08:45Z, the door's
#    own fetch moved origin/main to 0c28ca7d between the test's read and the
#    hand-over. The control below reproduces that on a disposable origin; the
#    test after it pins what §1-§5 now rely on: a neutralised fetch cannot move
#    the ref the expectation was read from.)
# ---------------------------------------------------------------------------

def test_the_race_the_hermetic_tests_neutralise_is_real(tmp_path, monkeypatch):
    """Control: WITHOUT the patch, the upstream move lands in the start line.

    Proves the fixture can reproduce the CI failure, so the pass in the test
    below is caused by the neutralised fetch and not by a dead fixture.
    """
    dispatch_id = "20261103-k4b-upstream-race"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, base_ref="origin/main")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)

    seed, consumer = _init_origin_and_clone(tmp_path)
    expected_sha = _rev_parse(consumer, "origin/main")
    moved_sha = _push_main(seed, "merged-during-the-run.txt")
    assert moved_sha != expected_sha, "fixture: the upstream commit must be new"
    assert _rev_parse(consumer, "origin/main") == expected_sha, (
        "fixture: the clone's local origin/main must still be the pre-merge commit")

    with _consumer_root(consumer), \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli._execute_claude_headless", return_value=0):
        assert dispatch_cli.run_dispatch(spec_file) == 0

    lines = _start_lines(state_dir)
    assert len(lines) == 1, lines
    # The door's own fetch moved the ref under the test — exactly 41996195419.
    assert lines[0]["base_sha"] == moved_sha
    assert lines[0]["base_sha"] != expected_sha


def test_a_merge_during_the_run_keeps_the_line_on_the_expected_base(tmp_path, monkeypatch):
    """Same move, fetch neutralised: the line names the sha the test read.

    This is the shape §1-§5 use against the real checkout, proven on a repo
    whose origin this test can move: read the expectation, merge upstream, run
    the door. Nothing outside the test can move the ref in between.
    """
    dispatch_id = "20261103-k4b-upstream-hermetic"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, base_ref="origin/main")
    state_dir = _door_env(monkeypatch, data_dir, lifecycle=True)

    seed, consumer = _init_origin_and_clone(tmp_path)
    expected_sha = _rev_parse(consumer, "origin/main")
    moved_sha = _push_main(seed, "merged-during-the-run.txt")
    assert moved_sha != expected_sha, "fixture: the upstream commit must be new"

    seen: dict = {}
    with _consumer_root(consumer), \
         _patched_fetch() as fetch_spy, \
         patch("dispatch_cli.build_runtime_snapshot", return_value=_clean_snapshot()), \
         patch("dispatch_cli.serialize_lane", side_effect=lambda *a, **k: _slot_spy(state_dir, seen)), \
         patch("dispatch_cli._execute_claude_headless", return_value=0) as headless:
        assert dispatch_cli.run_dispatch(spec_file) == 0

    fetch_spy.assert_called_once_with(consumer, "origin/main")
    lines = _start_lines(state_dir)
    assert len(lines) == 1, lines
    assert lines[0]["base_sha"] == expected_sha, (
        f"the start line named {lines[0]['base_sha']!r}; the test read {expected_sha!r} "
        f"before the run and upstream had moved to {moved_sha!r}")
    assert headless.call_args.kwargs["base_sha"] == expected_sha
    assert [line["dispatch_id"] for line in seen["start_lines_at_slot"]] == [dispatch_id]
