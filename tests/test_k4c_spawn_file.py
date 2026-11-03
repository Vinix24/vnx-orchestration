"""ADR-045 audit-spoor-kern K-4c: the spawn file from the lanes.

Dispatch-ID: dlv-2326a0e02534

K-4a added ``dispatch_start.record_child``; K-4b handed the door's ``Handover``
to the lanes, which accepted and ignored it. K-4c makes the claude headless lane
call ``record_child`` the moment the spawn returns the child's pid: one optional
``on_spawn`` hook threaded through ``run_envelope_headless_plan`` →
``ClaudeSubprocessAdapter.run`` → ``spawn_claude`` → ``SubprocessAdapter.deliver``
to the ``Popen`` seam.

The lane tests drive the real door pieces (the real ``run_envelope_headless_plan``
with only worktree/GOVERN/prompt-assembly replaced) and a real short-lived child
process through the same ``Popen`` call the adapter makes. Nothing here writes to
the real store: every state/data dir is under ``tmp_path``.

Red on the K-4b head ``dcebb1ff``: the lane had no ``on_spawn`` keyword, so no
spawn file was written and the hook-failure test could not reach ``record_child``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import coordination_db  # noqa: E402
import dispatch_cli  # noqa: E402
import dispatch_start  # noqa: E402
import project_id_migration  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from dispatch_envelope import EnvelopeResult  # noqa: E402
from dispatch_internal import issue_permit  # noqa: E402
from dispatch_plan import ExecutionPlan, ModelPin, RuntimeSnapshot  # noqa: E402
from dispatch_spec import Isolation, Provider  # noqa: E402

PROJECT = "vnx-dev"
MODEL = "sonnet"


# ---------------------------------------------------------------------------
# Hermetic append: keep the real ledger layer, never fire its process hooks
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _snapshot() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        constraint_verdicts=(),
        staging_promoted=True,
        target_health={"ephemeral": "healthy", "T0": "healthy", "T1": "healthy"},
        target_capable={"ephemeral": True, "T0": True, "T1": True},
        model_pins={
            "T0": ModelPin(model="opus", semantics="floor"),
            "T1": ModelPin(model=MODEL, semantics="floor"),
        },
    )


def _make_coordination_db(state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    coordination_db.init_schema(state_dir)
    db_path = coordination_db.db_path_from_state_dir(state_dir)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    return db_path


def _make_bundle(tmp_path: Path, *, dispatch_id: str, provider: str) -> "tuple[Path, Path]":
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
        "target_slot": "T1",
        "gate": "codex_gate",
        "dispatch_paths": [],
        "provider": provider,
        "deadline_seconds": 3600,
        "isolation": "worktree",
        "base_ref": "origin/main",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


def _make_plan(tmp_path: Path, dispatch_id: str) -> ExecutionPlan:
    work = tmp_path / "work" / dispatch_id
    work.mkdir(parents=True, exist_ok=True)
    instruction = work / "instruction.md"
    instruction.write_text("# Build\nDo the work.\n", encoding="utf-8")
    return ExecutionPlan(
        dispatch_id=dispatch_id,
        project_id=PROJECT,
        provider=Provider.CLAUDE,
        model=MODEL,
        lane="claude_headless",
        adapter="claude_subprocess",
        target_id="ephemeral",
        billing="subscription",
        serialization_class="claude-tmux",
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=False,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup="n/a",
        deadline_seconds=60,
        base_ref="origin/main",
        dispatch_paths=(),
        instruction_file=instruction,
        route_reason="test",
        instruction_sha256=hashlib.sha256(instruction.read_bytes()).hexdigest(),
        role="backend-developer",
    )


def _v3_handover(dispatch_id: str, *, attempt: int = 1) -> dispatch_start.Handover:
    return dispatch_start.Handover(
        project_id=PROJECT,
        dispatch_id=dispatch_id,
        attempt=attempt,
        lifecycle_mode=dispatch_start.MODE_V3,
        door_pid=os.getpid(),
        door_started_at=dispatch_start.process_started_at(os.getpid()) or "",
        started_at="2026-11-03T00:00:00Z",
    )


def _legacy_handover(dispatch_id: str) -> dispatch_start.Handover:
    return dispatch_start.Handover(
        project_id=PROJECT,
        dispatch_id=dispatch_id,
        attempt=1,
        lifecycle_mode=dispatch_start.MODE_LEGACY,
        door_pid=os.getpid(),
        door_started_at="",
        started_at="2026-11-03T00:00:00Z",
    )


def _fake_claude(tmp_path: Path) -> Path:
    """A real, short-lived child that speaks just enough stream-json."""
    script = tmp_path / "fake-claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "def emit(obj):\n"
        "    sys.stdout.write(json.dumps(obj) + '\\n'); sys.stdout.flush()\n"
        "emit({'type': 'system', 'subtype': 'init', 'session_id': 's-k4c', 'model': 'claude-sonnet'})\n"
        "emit({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'k4c done'}]}})\n"
        "emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'k4c done',\n"
        "      'session_id': 's-k4c', 'usage': {'input_tokens': 3, 'output_tokens': 2}})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@contextlib.contextmanager
def _lane_harness(tmp_path: Path, state_dir: Path):
    """The real headless lane with only prompt assembly, worktree and GOVERN replaced.

    ``subprocess.Popen`` stays the adapter's own seam: the claude argv is handed to
    a real short-lived child (``_fake_claude``) and every other command (``ps``,
    ``git``) passes through untouched. The child's pid and kernel start time are
    captured at spawn time, while it is alive.

    GOVERN returns an existing receipt path so a successful worker is not degraded
    to ``receipt_missing``; the receipt's content is K-5's concern, not this lane's.
    """
    consumer = tmp_path / "consumer"
    consumer.mkdir(exist_ok=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    fake_claude = _fake_claude(tmp_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    receipt = state_dir / "t0_receipts.ndjson"
    receipt.touch()
    real_popen = subprocess.Popen
    captured: dict = {}

    def fake_popen(cmd, *args, **kwargs):
        if cmd and cmd[0] == "claude":
            proc = real_popen([str(fake_claude), *cmd[1:]], *args, **kwargs)
            captured["pid"] = proc.pid
            captured["started"] = dispatch_start.process_started_at(proc.pid)
            return proc
        return real_popen(cmd, *args, **kwargs)

    with patch("dispatch_envelope._prepare", side_effect=lambda spec: spec.instruction), \
         patch("dispatch_envelope._record_integrity", return_value=None), \
         patch("dispatch_envelope._govern", return_value=(None, receipt)), \
         patch("dispatch_envelope._enforce_push_pr", side_effect=lambda **kw: kw["result"]), \
         patch("dispatch_envelope._resolve_phantom_diff", return_value=None), \
         patch("dispatch_worktree_isolation.resolve_consumer_project_root", return_value=consumer), \
         patch("dispatch_worktree_isolation.create_dispatch_worktree", return_value=worktree), \
         patch("dispatch_worktree_isolation.remove_dispatch_worktree"), \
         patch("subprocess.Popen", side_effect=fake_popen):
        yield captured


def _run_lane(tmp_path: Path, plan: ExecutionPlan, handover, state_dir: Path, data_dir: Path) -> int:
    return dispatch_cli._execute_claude_headless(
        plan,
        issue_permit(plan),
        state_dir=state_dir,
        data_dir=data_dir,
        role="backend-developer",
        handover=handover,
        base_sha="0" * 40,
    )


def _spawn_path(state_dir: Path, dispatch_id: str, attempt: int = 1) -> Path:
    return dispatch_start.spawn_file_path(state_dir, PROJECT, dispatch_id, attempt)


def _leftover_tmp(path: Path) -> "list[str]":
    if not path.parent.exists():
        return []
    return [item.name for item in path.parent.iterdir() if ".tmp" in item.name]


# ---------------------------------------------------------------------------
# 1. v3: the hook fires once, right after the spawn, and the file is atomic
# ---------------------------------------------------------------------------

def test_v3_lane_writes_the_spawn_file_for_the_real_child(tmp_path):
    dispatch_id = "20261103-k4c-spawn"
    data_dir = tmp_path / "vnx-data"
    state_dir = data_dir / "state"
    data_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    plan = _make_plan(tmp_path, dispatch_id)
    handover = _v3_handover(dispatch_id)

    with _lane_harness(tmp_path, state_dir) as captured, \
         patch.object(dispatch_start, "record_child",
                      wraps=dispatch_start.record_child) as record:
        rc = _run_lane(tmp_path, plan, handover, state_dir, data_dir)

    assert rc == 0, "the worker reached its normal end"
    assert "pid" in captured, "the child was never spawned"
    record.assert_called_once()
    assert record.call_args.args[0] is handover

    path = _spawn_path(state_dir, dispatch_id)
    assert path.is_file(), f"no spawn file at {path}"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["project_id"] == PROJECT
    assert document["dispatch_id"] == dispatch_id
    assert document["attempt"] == 1
    assert document["child_pid"] == captured["pid"]
    assert document["child_started_at"] == captured["started"]
    assert document["child_started_at"], "a spawn file without a start time is no evidence"
    assert _leftover_tmp(path) == [], "the spawn file must be written atomically"


# ---------------------------------------------------------------------------
# 2. a raising hook never kills the worker; the warning names the dispatch
# ---------------------------------------------------------------------------

def test_hook_failure_is_logged_and_the_worker_runs_on(tmp_path, caplog):
    dispatch_id = "20261103-k4c-hookfail"
    data_dir = tmp_path / "vnx-data"
    state_dir = data_dir / "state"
    data_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    plan = _make_plan(tmp_path, dispatch_id)
    handover = _v3_handover(dispatch_id)

    with _lane_harness(tmp_path, state_dir) as captured, \
         patch.object(dispatch_start, "record_child",
                      side_effect=RuntimeError("spawn evidence unwritable")), \
         caplog.at_level(logging.WARNING, logger="dispatch_cli"):
        rc = _run_lane(tmp_path, plan, handover, state_dir, data_dir)

    assert rc == 0, "a failing record_child must not fail the dispatch"
    assert "pid" in captured, "the worker must have run"
    warnings = [record.getMessage() for record in caplog.records
                if record.levelno == logging.WARNING]
    assert any(dispatch_id in message and "attempt=1" in message
               and "spawn evidence unwritable" in message
               for message in warnings), warnings
    assert not _spawn_path(state_dir, dispatch_id).exists(), "no partial spawn file may be left"


# ---------------------------------------------------------------------------
# 3. legacy: no hook, no file, the same outcome as the K-4b head
# ---------------------------------------------------------------------------

def test_legacy_handover_passes_no_hook_and_writes_no_file(tmp_path):
    dispatch_id = "20261103-k4c-legacy"
    data_dir = tmp_path / "vnx-data"
    state_dir = data_dir / "state"
    data_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    plan = _make_plan(tmp_path, dispatch_id)
    handover = _legacy_handover(dispatch_id)
    seen: dict = {}
    real_lane = dispatch_cli.run_envelope_headless_plan

    def _spy(*args, **kwargs):
        seen.update(kwargs)
        return real_lane(*args, **kwargs)

    with _lane_harness(tmp_path, state_dir), \
         patch.object(dispatch_start, "record_child") as record, \
         patch("dispatch_cli.run_envelope_headless_plan", side_effect=_spy):
        rc = _run_lane(tmp_path, plan, handover, state_dir, data_dir)

    assert rc == 0
    # K-4b called the lane without on_spawn; this lane passes the keyword's own
    # default, None, so the effective call is identical.
    assert seen.get("on_spawn", "missing") is None
    assert seen["handover"] is handover
    record.assert_not_called()
    assert not _spawn_path(state_dir, dispatch_id).exists()


# ---------------------------------------------------------------------------
# 4. provider lane: no claude child, so no hook and no file (unchanged)
# ---------------------------------------------------------------------------

def test_provider_lane_writes_no_spawn_file(tmp_path, monkeypatch):
    dispatch_id = "20261103-k4c-provider"
    data_dir, spec_file = _make_bundle(tmp_path, dispatch_id=dispatch_id, provider="codex")
    state_dir = data_dir / "state"
    _make_coordination_db(state_dir)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")

    provider_result = EnvelopeResult(
        status="success", returncode=0, report_path=None, receipt_path=None,
        completion_text="provider done", error=None,
    )

    @contextlib.contextmanager
    def _slot(serialization_class, *, dispatch_id):
        yield

    with patch("dispatch_cli.build_runtime_snapshot", return_value=_snapshot()), \
         patch("dispatch_cli.serialize_lane", _slot), \
         patch("dispatch_cli.run_envelope_plan", return_value=provider_result) as provider, \
         patch.object(dispatch_start, "record_child") as record:
        assert dispatch_cli.run_dispatch(spec_file) == 0

    provider.assert_called_once()
    assert "on_spawn" not in provider.call_args.kwargs
    record.assert_not_called()
    assert not _spawn_path(state_dir, dispatch_id).exists(), "the provider lane spawns no claude child"
