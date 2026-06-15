"""test_dispatch_cli.py — Tests for dispatch_cli.run_dispatch (PR-4 single-entry gate).

Tests the full gate: spec load -> validate -> snapshot -> compile_plan -> permit -> execute.
Covers both claude and provider lanes, constraint enforcement (including claude),
dry-run mode, permit fingerprint stability, and legacy bash flag behavior.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import (
    _execute_claude,
    build_runtime_snapshot,
    fingerprint,
    load_spec,
    run_dispatch,
)
from dispatch_internal import ExecutionPermit, issue_permit
from providers.constraint_enforcer import (
    ConstraintEnforcer,
    scan_anthropic_sdk_text,
)
from dispatch_plan import (
    ConstraintVerdict,
    ExecutionPlan,
    RuntimeSnapshot,
    compile_plan,
)
from dispatch_spec import (
    DispatchPath,
    DispatchSpec,
    Isolation,
    PathAccess,
    Provider,
    Reject,
    ValidatedSpec,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_instruction(tmp_path: Path) -> Path:
    f = tmp_path / "instruction.md"
    f.write_text(
        "# Test Dispatch\n\nRole: backend-developer\n\nDo something useful.\n",
        encoding="utf-8",
    )
    return f


def _make_spec_file(
    tmp_path: Path,
    *,
    provider: str = "claude",
    model: str | None = None,
    target_slot: str = "T1",
    staging_id: str = "test-stage",
    schema_version: int = 1,
    project_id: str = "vnx-dev",
    dispatch_id: str = "20260615-test-dispatch",
    extra: dict | None = None,
) -> Path:
    """Write a minimal dispatch-spec.json and return its path."""
    instruction_file = _make_instruction(tmp_path)
    spec: dict = {
        "schema_version": schema_version,
        "project_id": project_id,
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction_file),
        "role": "backend-developer",
        "target_slot": target_slot,
        "gate": "human-promoted",
        "dispatch_paths": [
            {"path": "scripts/test.py", "access": "read_write", "materialize_at_cwd": False}
        ],
        "provider": provider,
        "model": model,
        "deadline_seconds": 3600,
        "base_ref": "origin/main",
        "isolation": "worktree",
        "requires_mcp": False,
    }
    if extra:
        spec.update(extra)
    spec_file = tmp_path / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return spec_file


def _clean_snapshot(*, staging_promoted: bool = True) -> RuntimeSnapshot:
    """A snapshot with no constraint violations and healthy targets."""
    return RuntimeSnapshot(
        constraint_verdicts=(),
        staging_promoted=staging_promoted,
        target_health={"ephemeral": "healthy", "T1": "healthy"},
        target_capable={"ephemeral": True, "T1": True},
        model_pins={"T0": "opus", "T1": "sonnet", "T2": "sonnet", "T3": "sonnet"},
    )


def _make_minimal_plan(
    *,
    provider: Provider = Provider.CLAUDE,
    dispatch_id: str = "20260615-test-dispatch",
    instruction_file: Path | None = None,
) -> ExecutionPlan:
    """Build a minimal ExecutionPlan for permit / fingerprint tests."""
    if instruction_file is None:
        instruction_file = Path("/tmp/instruction.md")
    lane = "claude_tmux_subscription" if provider == Provider.CLAUDE else "provider"
    adapter = "tmux_claude" if provider == Provider.CLAUDE else "provider"
    billing = "subscription" if provider == Provider.CLAUDE else "provider_metered"
    target_id = "ephemeral" if provider == Provider.CLAUDE else "T1"
    serialization_class = "claude-tmux" if provider == Provider.CLAUDE else None
    warmup = "verify_strict" if provider == Provider.CLAUDE else "n/a"
    # Compute sha256 from file content if the file exists, else use zero-sentinel
    if instruction_file.exists():
        sha256 = hashlib.sha256(
            instruction_file.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
    else:
        sha256 = "0" * 64
    return ExecutionPlan(
        dispatch_id=dispatch_id,
        project_id="vnx-dev",
        provider=provider,
        model="sonnet",
        lane=lane,
        adapter=adapter,
        target_id=target_id,
        billing=billing,
        serialization_class=serialization_class,
        isolation=Isolation.WORKTREE,
        require_worktree=True,
        seed_materialize=True,
        instruction_delivery="file_ref",
        report_contract="required",
        warmup=warmup,
        deadline_seconds=3600,
        base_ref="origin/main",
        dispatch_paths=(),
        instruction_file=instruction_file,
        route_reason="D11,D3,D1,D2,D4,D5,D6,D7,D8,D9,D10,D12",
        instruction_sha256=sha256,
    )


# ---------------------------------------------------------------------------
# test_dry_run_prints_plan_no_spawn
# ---------------------------------------------------------------------------

@patch("dispatch_cli.build_runtime_snapshot")
@patch("dispatch_envelope.run_envelope_plan")
@patch("tmux_interactive_dispatch.TmuxInteractiveDispatch.dispatch")
def test_dry_run_prints_plan_no_spawn(mock_tmux, mock_envelope, mock_snapshot, tmp_path, capsys):
    """--dry-run prints plan + fingerprint and calls NO executor."""
    mock_snapshot.return_value = _clean_snapshot()
    spec_file = _make_spec_file(tmp_path, provider="claude")

    rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 0
    mock_envelope.assert_not_called()
    mock_tmux.assert_not_called()

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "fingerprint" in out
    # dispatch_id and lane must appear in the printed plan
    assert "20260615-test-dispatch" in out
    assert "claude" in out


# ---------------------------------------------------------------------------
# test_claude_runs_compile_plan_and_constraints
# ---------------------------------------------------------------------------

def test_claude_runs_compile_plan_and_constraints(tmp_path, monkeypatch):
    """Real constraint engine (no mock): instruction with `import anthropic` → Reject.
    Clean instruction inside bundle → routes to _execute_claude."""
    data_dir = tmp_path / "vnx-data"
    staging_id = "20260615-staging-real-test"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    # Part 1: instruction contains `import anthropic` → blocking no-anthropic-sdk → Reject
    evil_inst = bundle_dir / "evil_instruction.md"
    evil_inst.write_text(
        "# Evil dispatch\n\nimport anthropic\nclient = anthropic.Anthropic()\n",
        encoding="utf-8",
    )
    spec_dict = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20260615-test-evil",
        "staging_id": staging_id,
        "instruction_file": str(evil_inst),
        "role": "backend-developer",
        "target_slot": "T1",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file_evil = bundle_dir / "dispatch-spec-evil.json"
    spec_file_evil.write_text(json.dumps(spec_dict), encoding="utf-8")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file_evil)

    assert rc == 1, "Expected Reject from no-anthropic-sdk constraint"
    mock_execute.assert_not_called()

    # Part 2: clean instruction inside bundle → routes to _execute_claude
    clean_inst = bundle_dir / "clean_instruction.md"
    clean_inst.write_text(
        "# Clean dispatch\n\nDo something safe and useful.\n",
        encoding="utf-8",
    )
    spec_dict2 = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20260615-test-clean",
        "staging_id": staging_id,
        "instruction_file": str(clean_inst),
        "role": "backend-developer",
        "target_slot": "T1",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file_clean = bundle_dir / "dispatch-spec-clean.json"
    spec_file_clean.write_text(json.dumps(spec_dict2), encoding="utf-8")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        mock_execute.return_value = 0
        rc = run_dispatch(spec_file_clean)

    assert rc == 0
    mock_execute.assert_called_once()
    plan_arg = mock_execute.call_args[0][0]
    assert plan_arg.lane == "claude_tmux_subscription"
    assert plan_arg.provider == Provider.CLAUDE


# ---------------------------------------------------------------------------
# test_provider_routes_to_envelope
# ---------------------------------------------------------------------------

@patch("dispatch_cli.build_runtime_snapshot")
@patch("dispatch_cli.run_envelope_plan")
def test_provider_routes_to_envelope(mock_envelope, mock_snapshot, tmp_path):
    """Codex spec → run_envelope_plan called with the plan and a valid permit."""
    mock_snapshot.return_value = _clean_snapshot()

    # run_envelope_plan returns an EnvelopeResult-like object
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_envelope.return_value = mock_result

    spec_file = _make_spec_file(tmp_path, provider="codex", target_slot="T1")

    rc = run_dispatch(spec_file)
    assert rc == 0
    mock_envelope.assert_called_once()

    args, kwargs = mock_envelope.call_args
    plan_arg, permit_arg = args[0], args[1]

    # Plan must be a provider-lane plan for codex
    assert plan_arg.lane == "provider"
    assert plan_arg.provider == Provider.CODEX

    # Permit must be valid (issued by issue_permit for this plan)
    from dispatch_internal import require_permit
    # Should not raise — a valid permit for the correct plan
    require_permit(plan_arg, permit_arg)


# ---------------------------------------------------------------------------
# test_claude_routes_to_tmux_with_permit
# ---------------------------------------------------------------------------

@patch("dispatch_cli.build_runtime_snapshot")
def test_claude_routes_to_tmux_with_permit(mock_snapshot, tmp_path):
    """Claude spec → _execute_claude calls require_permit then TmuxInteractiveDispatch.dispatch.
    Tampered permit → PermissionError before tmux dispatch is reached.
    """
    mock_snapshot.return_value = _clean_snapshot()
    instruction_file = _make_instruction(tmp_path)
    spec_file = _make_spec_file(tmp_path, provider="claude")

    # Part A: valid permit → TmuxInteractiveDispatch.dispatch called
    mock_dispatch_result = MagicMock()
    mock_dispatch_result.success = True

    with patch("tmux_interactive_dispatch.TmuxInteractiveDispatch.dispatch",
               return_value=mock_dispatch_result) as mock_tmux_dispatch:
        with patch("dispatch_cli.require_permit") as mock_require:
            rc = run_dispatch(spec_file)

    assert rc == 0
    # P1-#6 adds require_permit in run_dispatch; _execute_claude also calls it → 2 total
    assert mock_require.call_count == 2
    mock_tmux_dispatch.assert_called_once()

    # Part B: tampered permit → PermissionError raised before tmux dispatch
    plan = _make_minimal_plan(instruction_file=instruction_file)
    valid_permit = issue_permit(plan)

    # Build a tampered permit: wrong plan_digest, sentinel is None (default)
    tampered_permit = ExecutionPermit(
        dispatch_id=plan.dispatch_id,
        plan_digest="deadbeef" * 8,  # wrong digest
    )
    # _sentinel defaults to None, not _PERMIT_SENTINEL — require_permit will reject

    with patch("tmux_interactive_dispatch.TmuxInteractiveDispatch.dispatch") as mock_tmux:
        with pytest.raises(PermissionError):
            _execute_claude(
                plan,
                tampered_permit,
                state_dir=tmp_path / "state",
                data_dir=tmp_path,
            )
        mock_tmux.assert_not_called()


# ---------------------------------------------------------------------------
# test_reject_on_validate_failure
# ---------------------------------------------------------------------------

def test_reject_on_validate_failure(tmp_path):
    """Bad spec (wrong schema_version) → validate() returns Reject → return 1."""
    spec_file = _make_spec_file(tmp_path, schema_version=99)

    with patch("dispatch_cli.build_runtime_snapshot") as mock_snapshot:
        rc = run_dispatch(spec_file)

    assert rc == 1
    mock_snapshot.assert_not_called()  # gate fires before snapshot


# ---------------------------------------------------------------------------
# test_reject_on_unpromoted_staging
# ---------------------------------------------------------------------------

@patch("dispatch_cli.build_runtime_snapshot")
def test_reject_on_unpromoted_staging(mock_snapshot, tmp_path):
    """Unpromoted staging_id → compile_plan rejects (D11 gate) → return 1; no executor."""
    mock_snapshot.return_value = _clean_snapshot(staging_promoted=False)
    spec_file = _make_spec_file(tmp_path, provider="claude")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1
    mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# test_flag_off_legacy_unchanged
# ---------------------------------------------------------------------------

def test_flag_off_legacy_unchanged(tmp_path):
    """With VNX_SINGLE_ENTRY_DISPATCH unset, cmd_dispatch uses the legacy dry-run path."""
    dispatch_md = tmp_path / "test-dispatch.md"
    dispatch_md.write_text(
        "[[TARGET:T1]]\nRole: backend-developer\nGate: human-promoted\n\nTest dispatch.\n",
        encoding="utf-8",
    )

    dispatch_sh = _REPO_ROOT / "scripts" / "commands" / "dispatch.sh"
    dispatches_dir = tmp_path / "dispatches"
    dispatches_dir.mkdir()

    bash_cmd = f"""
set -e
VNX_HOME='{_REPO_ROOT}'
VNX_DATA_DIR='{tmp_path}'
VNX_DISPATCH_DIR='{dispatches_dir}'
VNX_STATE_DIR='{tmp_path}/state'
log() {{ echo "[LOG] $*"; }}
err() {{ echo "[ERR] $*" >&2; }}
source '{dispatch_sh}'
unset VNX_SINGLE_ENTRY_DISPATCH
cmd_dispatch '{dispatch_md}' --dry-run
"""
    result = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)

    assert result.returncode == 0, (
        f"Expected legacy dry-run to succeed; rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "DRY RUN" in combined
    # Confirms NOT the single-entry gate path
    assert "single-entry gate" not in combined.lower()


# ---------------------------------------------------------------------------
# test_staging_binding_required (P0-2)
# ---------------------------------------------------------------------------

def test_staging_binding_required(tmp_path, monkeypatch, capsys):
    """spec_file or instruction_file outside bundle dir → Reject(ADR-006-binding), no spawn."""
    data_dir = tmp_path / "vnx-data"
    staging_id = "20260615-staging-bind-test"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    # Instruction file is OUTSIDE the bundle
    instruction_file = tmp_path / "outside_instruction.md"
    instruction_file.write_text("# Dispatch outside bundle\n", encoding="utf-8")

    # spec_file is also OUTSIDE the bundle
    spec_data = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20260615-binding-test",
        "staging_id": staging_id,
        "instruction_file": str(instruction_file),
        "role": "backend-developer",
        "target_slot": "T1",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = tmp_path / "dispatch-spec-outside.json"
    spec_file.write_text(json.dumps(spec_data), encoding="utf-8")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1
    mock_execute.assert_not_called()
    err = capsys.readouterr().err
    assert "ADR-006-binding" in err


# ---------------------------------------------------------------------------
# test_instruction_mutation_rejected (P0-3)
# ---------------------------------------------------------------------------

def test_instruction_mutation_rejected(tmp_path):
    """Mutating instruction file after permit issuance → PermissionError (sha256 mismatch), no spawn."""
    original_content = "# Clean dispatch\n\nDo something safe.\n"
    instruction_file = tmp_path / "instruction.md"
    instruction_file.write_text(original_content, encoding="utf-8")

    plan = _make_minimal_plan(instruction_file=instruction_file)
    expected_sha = hashlib.sha256(original_content.encode("utf-8")).hexdigest()
    assert plan.instruction_sha256 == expected_sha

    permit = issue_permit(plan)

    # Mutate the instruction file AFTER permit is issued
    instruction_file.write_text(
        "import anthropic\n# Evil override injected after permit",
        encoding="utf-8",
    )

    # _execute_claude must fail-closed: sha256 mismatch → PermissionError, no tmux spawn
    with patch("tmux_interactive_dispatch.TmuxInteractiveDispatch.dispatch") as mock_tmux:
        with pytest.raises(PermissionError, match="sha256 mismatch"):
            _execute_claude(
                plan,
                permit,
                state_dir=tmp_path / "state",
                data_dir=tmp_path,
            )
        mock_tmux.assert_not_called()


# ---------------------------------------------------------------------------
# test_legacy_rollback
# ---------------------------------------------------------------------------

def test_legacy_rollback(tmp_path):
    """VNX_DISPATCH_LEGACY=1 forces legacy path even when VNX_SINGLE_ENTRY_DISPATCH=1."""
    dispatch_md = tmp_path / "test-rollback.md"
    dispatch_md.write_text(
        "[[TARGET:T1]]\nRole: backend-developer\nGate: human-promoted\n\nTest rollback.\n",
        encoding="utf-8",
    )

    dispatch_sh = _REPO_ROOT / "scripts" / "commands" / "dispatch.sh"
    dispatches_dir = tmp_path / "dispatches"
    dispatches_dir.mkdir()

    bash_cmd = f"""
set -e
VNX_HOME='{_REPO_ROOT}'
VNX_DATA_DIR='{tmp_path}'
VNX_DISPATCH_DIR='{dispatches_dir}'
VNX_STATE_DIR='{tmp_path}/state'
VNX_SINGLE_ENTRY_DISPATCH=1
VNX_DISPATCH_LEGACY=1
log() {{ echo "[LOG] $*"; }}
err() {{ echo "[ERR] $*" >&2; }}
source '{dispatch_sh}'
cmd_dispatch '{dispatch_md}' --dry-run
"""
    result = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)

    assert result.returncode == 0, (
        f"Expected legacy rollback to succeed; rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "DRY RUN" in combined
    # VNX_DISPATCH_LEGACY=1 must force the legacy path, not the single-entry gate
    assert "single-entry gate" not in combined.lower()


# ---------------------------------------------------------------------------
# test_permit_fingerprint_stable
# ---------------------------------------------------------------------------

def _make_bundle_spec(
    tmp_path: Path,
    *,
    instruction_text: str,
    staging_id: str = "20260615-staging-probe",
    dispatch_id: str = "20260615-probe-dispatch",
    provider: str = "claude",
) -> tuple[Path, Path]:
    """Build a promoted bundle under <tmp>/vnx-data with the given instruction.

    Returns (data_dir, spec_file). spec_file + instruction live inside the bundle so
    the staging-binding check passes and only the edge under test can reject.
    """
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)
    inst = bundle_dir / "instruction.md"
    inst.write_text(instruction_text, encoding="utf-8")
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(inst),
        "role": "backend-developer",
        "target_slot": "T1",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": provider,
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


# ---------------------------------------------------------------------------
# P0-1 — whitespace-bypassable SDK block is closed (fail-CLOSED)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("evil_line", [
    "import\tanthropic",            # codex probe: tab separator
    "import    anthropic",          # codex probe: multiple spaces
    "import anthropic as ant",      # aliased import
    "    import anthropic",         # leading indentation
    "from\tanthropic import Anthropic",
    "from  anthropic  import  Anthropic",
    "client = anthropic . Anthropic ( )",  # spaced attribute access + call
])
def test_sdk_block_is_whitespace_aware(tmp_path, monkeypatch, evil_line):
    """codex PROVED `import\\tanthropic` / `import   anthropic` slip past literal
    substring matching. The whitespace-aware gate must Reject all of these → no spawn."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text=f"# Dispatch\n\n{evil_line}\n",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, f"Expected fail-closed Reject for {evil_line!r}"
    mock_execute.assert_not_called()


def test_clean_import_mentioning_anthropic_word_not_blocked(tmp_path, monkeypatch):
    """Sanity: prose mentioning 'anthropic' without an import statement is not blocked
    (the regex requires an import/from/SDK-call shape, not the bare word)."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# Dispatch\n\nDocument the anthropic routing policy clearly.\n",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# P0-2 — symlinked pending ROOT escaping the data root is rejected (fail-CLOSED)
# ---------------------------------------------------------------------------

def test_symlinked_pending_root_rejected(tmp_path, monkeypatch, capsys):
    """codex PROVED: symlink <data_dir>/dispatches/pending → an external dir, drop a
    bundle there → rc=0 + staging_promoted + executor called. Anchor the pending root:
    if it resolves outside the data root → BLOCKING ADR-006-untrusted-root, no spawn."""
    data_dir = tmp_path / "vnx-data"
    (data_dir / "dispatches").mkdir(parents=True)

    # External bundle OUTSIDE the data root
    external = tmp_path / "external-pending"
    staging_id = "20260615-external-bundle"
    external_bundle = external / staging_id
    external_bundle.mkdir(parents=True)
    inst = external_bundle / "instruction.md"
    inst.write_text("# Looks clean\n\nDo something.\n", encoding="utf-8")

    # Symlink the pending ROOT to the external dir (the proven attack)
    pending_link = data_dir / "dispatches" / "pending"
    pending_link.symlink_to(external, target_is_directory=True)

    # Spec references paths THROUGH the symlinked pending root
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20260615-symlink-probe",
        "staging_id": staging_id,
        "instruction_file": str(pending_link / staging_id / "instruction.md"),
        "role": "backend-developer",
        "target_slot": "T1",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = pending_link / staging_id / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1
    mock_execute.assert_not_called()
    assert "ADR-006-untrusted-root" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# P0-3 — empty-hash plan never spawns on EITHER lane (fail-CLOSED)
# ---------------------------------------------------------------------------

def test_empty_hash_plan_no_spawn_either_lane(tmp_path):
    """codex PROVED an empty-hash plan + valid permit spawns mutated content on both
    lanes. Defense-in-depth: issue_permit refuses to mint for an empty hash, and both
    executors fail-closed before delivery even if require_permit were bypassed."""
    from dataclasses import replace

    instruction_file = _make_instruction(tmp_path)

    claude_plan = replace(
        _make_minimal_plan(provider=Provider.CLAUDE, instruction_file=instruction_file),
        instruction_sha256="",
    )
    provider_plan = replace(
        _make_minimal_plan(provider=Provider.CODEX, instruction_file=instruction_file),
        instruction_sha256="",
    )

    # Defense-in-depth #4: no permit can be minted for an empty-hash plan
    with pytest.raises(PermissionError):
        issue_permit(claude_plan)
    with pytest.raises(PermissionError):
        issue_permit(provider_plan)

    # Claude lane: even with require_permit bypassed, the executor refuses to spawn
    bare_claude = ExecutionPermit(dispatch_id=claude_plan.dispatch_id, plan_digest=claude_plan.digest())
    with patch("dispatch_cli.require_permit", lambda *a, **k: None):
        with patch("tmux_interactive_dispatch.TmuxInteractiveDispatch.dispatch") as mock_tmux:
            with pytest.raises(PermissionError):
                _execute_claude(
                    claude_plan, bare_claude,
                    state_dir=tmp_path / "state", data_dir=tmp_path,
                )
            mock_tmux.assert_not_called()

    # Provider lane: even with require_permit bypassed, the envelope fails closed
    from dispatch_envelope import run_envelope_plan
    bare_provider = ExecutionPermit(dispatch_id=provider_plan.dispatch_id, plan_digest=provider_plan.digest())
    with patch("dispatch_internal.require_permit", lambda *a, **k: None):
        with patch("dispatch_envelope.ProviderAdapter.run") as mock_run:
            result = run_envelope_plan(
                provider_plan, bare_provider,
                state_dir=tmp_path / "state", data_dir=tmp_path,
            )
            assert result.returncode != 0
            assert result.status == "failure"
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# P1 — dispatch.sh `--spec-file` with no value emits a clean error (not set -u abort)
# ---------------------------------------------------------------------------

def test_spec_file_flag_without_value_clean_error(tmp_path):
    """Under `set -u`, a trailing `--spec-file` with no path must produce a clean gate
    error (return 1), not an unbound-variable shell abort."""
    dispatch_sh = _REPO_ROOT / "scripts" / "commands" / "dispatch.sh"
    dispatches_dir = tmp_path / "dispatches"
    dispatches_dir.mkdir()

    bash_cmd = f"""
set -eu
VNX_HOME='{_REPO_ROOT}'
VNX_DATA_DIR='{tmp_path}'
VNX_DISPATCH_DIR='{dispatches_dir}'
VNX_STATE_DIR='{tmp_path}/state'
VNX_SINGLE_ENTRY_DISPATCH=1
log() {{ echo "[LOG] $*"; }}
err() {{ echo "[ERR] $*" >&2; }}
source '{dispatch_sh}'
cmd_dispatch --spec-file
rc=$?
echo "EXITCODE=$rc"
"""
    result = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True)
    combined = result.stdout + result.stderr
    # Clean gate error, not a bash 'unbound variable' abort
    assert "requires a path argument" in combined, combined
    assert "unbound variable" not in combined, combined


def test_permit_fingerprint_stable(tmp_path):
    """Same plan → same fingerprint; different plan → different fingerprint."""
    instruction = _make_instruction(tmp_path)

    plan_a = _make_minimal_plan(dispatch_id="dispatch-alpha", instruction_file=instruction)
    plan_b = _make_minimal_plan(dispatch_id="dispatch-alpha", instruction_file=instruction)
    plan_c = _make_minimal_plan(dispatch_id="dispatch-beta", instruction_file=instruction)

    permit_a1 = issue_permit(plan_a)
    permit_a2 = issue_permit(plan_b)  # same plan content, different object
    permit_c = issue_permit(plan_c)

    fp_a1 = fingerprint(permit_a1)
    fp_a2 = fingerprint(permit_a2)
    fp_c = fingerprint(permit_c)

    # Same plan → same fingerprint
    assert fp_a1 == fp_a2, "Identical plans must produce identical fingerprints"

    # Different dispatch_id → different fingerprint
    assert fp_a1 != fp_c, "Plans with different dispatch_ids must produce different fingerprints"

    # Fingerprint is <digest[:12]>-<dispatch_id>
    assert fp_a1.startswith(permit_a1.plan_digest[:12])
    assert fp_a1.endswith(plan_a.dispatch_id)
    assert "-" in fp_a1


# ---------------------------------------------------------------------------
# PR-4d P0 — tokenize-robust SDK scanner closes the codex-proven deep forms
# ---------------------------------------------------------------------------

# codex re-probe forms that the PR-4c regex scanner let through (rc=0). Each is
# stored inside a Python string literal (never a bare source-line import) so the
# ADR-003 file scanner does not flag this test file.
_CODEX_SDK_FORMS = [
    ("line_continuation_import", "import \\\n    anthropic as a"),
    ("line_continuation_from", "from anthropic \\\n    import Client"),
    ("submodule_from", "from anthropic.client import Client"),
    ("submodule_import", "import anthropic.client"),
    ("dunder_import", '__import__("anthropic")'),
    ("importlib_module", 'importlib.import_module("anthropic")'),
    ("spaced_attr_call", "client = anthropic . Anthropic ( )"),
    ("js_sdk_literal", 'const x = require("@anthropic-ai/sdk")'),
]


@pytest.mark.parametrize("form_id,snippet", _CODEX_SDK_FORMS, ids=[f[0] for f in _CODEX_SDK_FORMS])
def test_scanner_blocks_codex_deep_forms(form_id, snippet):
    """Gate #1 (shared scanner): every codex-proven deep form is detected."""
    text = f"# Dispatch\n\n{snippet}\n"
    assert scan_anthropic_sdk_text(text) is True, f"{form_id!r} slipped past the scanner"


@pytest.mark.parametrize("form_id,snippet", _CODEX_SDK_FORMS, ids=[f[0] for f in _CODEX_SDK_FORMS])
def test_constraint_engine_blocks_codex_deep_forms(form_id, snippet):
    """Gate #2 (constraint engine): the forbid_import rule flags every deep form."""
    text = f"# Dispatch\n\n{snippet}\n"
    violations = ConstraintEnforcer().check_constraints(instruction_text=text)
    assert any(v.code == "no-anthropic-sdk" for v in violations), (
        f"constraint engine missed {form_id!r}"
    )


@pytest.mark.parametrize("form_id,snippet", _CODEX_SDK_FORMS, ids=[f[0] for f in _CODEX_SDK_FORMS])
def test_dispatch_gate_blocks_codex_deep_forms(form_id, snippet, tmp_path, monkeypatch):
    """End-to-end via run_dispatch: every codex deep form → fail-closed Reject, no spawn.

    run_dispatch exercises BOTH gates inside the single door (constraint engine in
    build_runtime_snapshot AND the blocking backstop)."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text=f"# Dispatch\n\n{snippet}\n",
        staging_id=f"20260615-codex-{form_id.replace('_', '-')}",
        dispatch_id=f"20260615-codex-{form_id.replace('_', '-')}",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, f"Expected fail-closed Reject for {form_id!r}"
    mock_execute.assert_not_called()


def test_scanner_allows_clean_prose_with_anthropic_word():
    """Sanity: prose mentioning 'anthropic' without an import shape is not flagged."""
    text = "# Dispatch\n\nDocument the anthropic routing policy clearly.\n"
    assert scan_anthropic_sdk_text(text) is False


def test_scanner_handles_garbled_non_python_text():
    """Garbled text that breaks tokenization (unbalanced brackets) must not crash the
    scanner and must still catch a hidden import via the regex fallback (fail-CLOSED)."""
    garbled = "Broken markdown with [an unbalanced bracket and (paren\nimport anthropic\n"
    assert scan_anthropic_sdk_text(garbled) is True
    benign_garbled = "Broken markdown with [an unbalanced bracket and (paren only\n"
    assert scan_anthropic_sdk_text(benign_garbled) is False


# ---------------------------------------------------------------------------
# PR-4d P2 — the SSOT (provider_constraints.yaml) patterns are authoritative
# ---------------------------------------------------------------------------

def test_ssot_forbid_import_pattern_is_authoritative(tmp_path):
    """A forbid_import pattern configured ONLY in the YAML must be enforced — proving
    the SSOT governs and the scanner is not limited to a hard-coded list."""
    custom = tmp_path / "constraints.yaml"
    custom.write_text(
        textwrap.dedent(
            """\
            version: 1
            constraints:
              - id: no-anthropic-sdk
                rule: forbid_import
                forbidden_import:
                  patterns:
                    - "vnx_probe_forbidden_marker"
                reason: SSOT-authoritative probe
                enforcement: ci_grep
                audit_severity: blocking
                override_allowed: false
            """
        ),
        encoding="utf-8",
    )
    probe = "benign instruction containing vnx_probe_forbidden_marker inline"

    # Configured-pattern enforcer flags the marker...
    violations = ConstraintEnforcer(path=custom).check_constraints(instruction_text=probe)
    assert any(v.code == "no-anthropic-sdk" for v in violations), (
        "configured SSOT pattern was not enforced"
    )

    # ...while the real SSOT (no such pattern) does NOT — confirming the match came
    # from the configured pattern, not a built-in default.
    control = ConstraintEnforcer().check_constraints(instruction_text=probe)
    assert not any(v.code == "no-anthropic-sdk" for v in control)


# ---------------------------------------------------------------------------
# PR-4d — dispatches-level symlink escape is rejected (kimi P2-2 regression)
# ---------------------------------------------------------------------------

def test_symlinked_dispatches_dir_rejected(tmp_path, monkeypatch, capsys):
    """kimi P2-2: symlink <data_dir>/dispatches (one level above pending) to an
    external dir, drop a bundle in it. The pending-root anchor resolves THROUGH the
    symlink, lands outside the data root → BLOCKING ADR-006-untrusted-root, no spawn."""
    data_dir = tmp_path / "vnx-data"
    data_dir.mkdir(parents=True)

    external = tmp_path / "external-dispatches"
    staging_id = "20260615-external-dispatches-bundle"
    external_bundle = external / "pending" / staging_id
    external_bundle.mkdir(parents=True)
    inst = external_bundle / "instruction.md"
    inst.write_text("# Looks clean\n\nDo something.\n", encoding="utf-8")

    # Symlink the WHOLE dispatches dir (not just pending) to the external location
    dispatches_link = data_dir / "dispatches"
    dispatches_link.symlink_to(external, target_is_directory=True)

    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20260615-dispatches-symlink-probe",
        "staging_id": staging_id,
        "instruction_file": str(dispatches_link / "pending" / staging_id / "instruction.md"),
        "role": "backend-developer",
        "target_slot": "T1",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = dispatches_link / "pending" / staging_id / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1
    mock_execute.assert_not_called()
    assert "ADR-006-untrusted-root" in capsys.readouterr().err
