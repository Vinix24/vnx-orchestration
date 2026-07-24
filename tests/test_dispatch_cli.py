"""test_dispatch_cli.py — Tests for dispatch_cli.run_dispatch (PR-4 single-entry gate).

Tests the full gate: spec load -> validate -> snapshot -> compile_plan -> permit -> execute.
Covers both claude and provider lanes, constraint enforcement (including claude),
dry-run mode, permit fingerprint stability, and legacy bash flag behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import (
    _check_track_link_verdict,
    _DEFAULT_MODEL_PINS,
    _execute_claude,
    _execute_claude_headless,
    _has_col,
    _load_model_pins_from_yaml,
    _persist_track_id,
    _tracks_db_path,
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
    """Real constraint engine (no mock): instruction with `import anthropic` → PROCEEDS (warn only).
    SDK import is no longer a blocking verdict after PR-4e. Clean instruction → same outcome."""
    data_dir = tmp_path / "vnx-data"
    staging_id = "20260615-staging-real-test"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    # Part 1: instruction contains `import anthropic` → SDK is warn not blocking → dispatch proceeds
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
        "target_slot": "T0",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file_evil = bundle_dir / "dispatch-spec-evil.json"
    spec_file_evil.write_text(json.dumps(spec_dict), encoding="utf-8")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file_evil)

    assert rc == 0, "SDK instruction must PROCEED after PR-4e (warn only, not blocking)"
    mock_execute.assert_called_once()

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
        "target_slot": "T0",
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

def test_raw_md_legacy_under_default_on(tmp_path):
    """Post-flip (ADR-024): VNX_SINGLE_ENTRY_DISPATCH unset resolves to the door (default ON), but a
    raw .md still falls through to the legacy dry-run path (Option X1) — NOT the single-entry gate."""
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
        "target_slot": "T0",  # T1/T2/T3 unconditionally pin to kimi-k3 (workers-kimi-pinned);
        # T0 stays a valid Claude model so the ADR-006-binding reject under test isn't
        # masked by an unrelated kimi-via-cli-only reject firing first.
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
    target_slot: str = "T0",
    model: str | None = None,
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
        # T0 (not T1): these callers all use provider="claude" to exercise the
        # SDK-scan/claude-lane mechanics, unrelated to worker model routing. Since
        # worker-provider-kimi-flip (2026-07-23), T1/T2/T3 unconditionally pin the
        # claude lane's model to kimi-k3 (workers-kimi-pinned), which is not a valid
        # Claude model and hard-rejects (model-not-in-current-registry). T0's pin
        # (t0-opus-only -> claude-opus-4-8) stays a valid Claude model, so it's the
        # clean slot for a generic "claude dispatch that should proceed" fixture.
        "target_slot": target_slot,
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": provider,
        "model": model,
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
    """PR-4e: SDK forms (whitespace variants) are DETECTED by the scanner (warn) but no longer
    BLOCK the dispatch — the instruction proceeds. The scanner still catches every form."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text=f"# Dispatch\n\n{evil_line}\n",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0, f"SDK form must PROCEED (warn not blocked) after PR-4e: {evil_line!r}"
    mock_execute.assert_called_once()


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
        "target_slot": "T0",
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
def test_dispatch_gate_codex_deep_forms_proceed(form_id, snippet, tmp_path, monkeypatch):
    """PR-4e: every codex SDK deep form → dispatch PROCEEDS (warn only, not blocking).

    The scanner and constraint engine still DETECT the form (see test_scanner_blocks_codex_deep_forms
    and test_constraint_engine_blocks_codex_deep_forms); the change is that the dispatch no longer
    BLOCKS on it — the executor is reachable."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text=f"# Dispatch\n\n{snippet}\n",
        staging_id=f"20260615-codex-{form_id.replace('_', '-')}",
        dispatch_id=f"20260615-codex-{form_id.replace('_', '-')}",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0, f"SDK deep form must PROCEED (warn) after PR-4e: {form_id!r}"
    mock_execute.assert_called_once()


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
        "target_slot": "T0",
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


# ---------------------------------------------------------------------------
# PR-4e — SDK instruction-scan WARN (not blocking); routing constraints stay blocking
# ---------------------------------------------------------------------------

def test_sdk_instruction_proceeds_warn_not_block(tmp_path, monkeypatch):
    """PR-4e: actual `import anthropic` in instruction → dispatch PROCEEDS (warn only).
    The SDK scan is a signal, not a gate. rc=0 proves no blocking verdict was produced."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# Task\n\nimport anthropic\nclient = anthropic.Anthropic()\n",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_exec:
        rc = run_dispatch(spec_file)

    assert rc == 0, "SDK import in instruction must PROCEED after PR-4e"
    mock_exec.assert_called_once()


def test_sdk_instruction_url_mention_proceeds(tmp_path, monkeypatch):
    """PR-4e: a URL mentioning 'anthropic.com' is not an SDK import → no violation at all."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# Task\n\nSee https://anthropic.com/docs for the routing policy.\n",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_exec:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_exec.assert_called_once()


def test_sdk_violation_is_warn_not_blocking_in_constraint_engine():
    """PR-4e: forbid_import (no-anthropic-sdk) must emit WARN severity, never blocking."""
    violations = ConstraintEnforcer().check_constraints(
        instruction_text="import anthropic\nclient = anthropic.Anthropic()\n"
    )
    sdk_violations = [v for v in violations if v.code == "no-anthropic-sdk"]
    assert sdk_violations, "Expected a no-anthropic-sdk violation from the constraint engine"
    assert all(v.severity == "warn" for v in sdk_violations), (
        f"SDK violation must be warn, got {sdk_violations[0].severity!r}"
    )


def test_forbid_route_kimi_is_blocking_in_constraint_engine():
    """PR-4e: forbid_route kimi-via-cli-only must remain BLOCKING severity."""
    violations = ConstraintEnforcer().check_constraints(
        provider="litellm:moonshot",
        sub_provider="moonshot",
        via="moonshot",
    )
    kimi_v = next((v for v in violations if v.code == "kimi-via-cli-only"), None)
    assert kimi_v is not None, "Expected kimi-via-cli-only violation for litellm:moonshot"
    assert kimi_v.severity == "blocking"


def test_kimi_model_under_claude_provider_is_blocking_in_constraint_engine():
    """dispatch-agent-lane-coercion (20260713-LANECOERCE): the hardcoded kimi-model-substring
    guard (constraint_enforcer.py ~502-507) is distinct from the litellm:moonshot forbid_route
    rule above — it must flag ANY non-kimi provider carrying a kimi-branded model string, which
    is exactly the raw (pre-pin) shape a --model kimi request has under provider=claude."""
    violations = ConstraintEnforcer().check_constraints(
        provider="claude",
        model="kimi",
    )
    kimi_v = next((v for v in violations if v.code == "kimi-via-cli-only"), None)
    assert kimi_v is not None, "Expected kimi-via-cli-only violation for provider=claude, model=kimi"
    assert kimi_v.severity == "blocking"


def test_forbid_route_deprecated_glm_is_blocking_in_constraint_engine():
    """PR-4e: deprecated-glm-models forbid_route must remain BLOCKING severity."""
    violations = ConstraintEnforcer().check_constraints(
        provider="litellm:zai",
        sub_provider="zai",
        model="glm-4.5",
    )
    glm_v = next((v for v in violations if v.code == "deprecated-glm-models"), None)
    assert glm_v is not None, "Expected deprecated-glm-models violation for glm-4.5"
    assert glm_v.severity == "blocking"


def test_require_route_wrong_model_is_warn_not_block():
    """PR-4e: require_route (t0-opus-only) with wrong model → WARN, not blocking."""
    violations = ConstraintEnforcer().check_constraints(
        provider="claude",
        terminal_id="T0",
        role="T0",
        model="sonnet",
    )
    t0_v = next((v for v in violations if v.code == "t0-opus-only"), None)
    assert t0_v is not None, "Expected t0-opus-only violation when T0 runs with sonnet"
    assert t0_v.severity == "warn"


def test_forbid_route_blocking_verdict_rejects_dispatch(tmp_path):
    """PR-4e: a blocking verdict from forbid_route in the snapshot → compile_plan Rejects → rc=1."""
    spec_file = _make_spec_file(tmp_path, provider="claude")
    blocking_snapshot = RuntimeSnapshot(
        constraint_verdicts=(ConstraintVerdict(
            code="kimi-via-cli-only",
            severity="blocking",
            message="Route forbidden: Kimi must use the kimi CLI lane",
        ),),
        staging_promoted=True,
        target_health={"ephemeral": "healthy"},
        target_capable={"ephemeral": True},
        model_pins={"T0": "opus", "T1": "sonnet", "T2": "sonnet", "T3": "sonnet"},
    )
    with patch("dispatch_cli.build_runtime_snapshot", return_value=blocking_snapshot):
        with patch("dispatch_cli._execute_claude") as mock_exec:
            rc = run_dispatch(spec_file)

    assert rc == 1, "Blocking forbid_route verdict must cause a Reject"
    mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# worker-provider-kimi-flip (2026-07-23) — pin-loader id rename regression tests
# ---------------------------------------------------------------------------

def test_default_model_pins_flip_workers_to_kimi_k3():
    """_DEFAULT_MODEL_PINS must pin T1/T2/T3 to kimi-k3 post-flip; T0 stays opus."""
    assert _DEFAULT_MODEL_PINS == {
        "T0": "opus",
        "T1": "kimi-k3",
        "T2": "kimi-k3",
        "T3": "kimi-k3",
    }


def test_load_model_pins_from_yaml_reads_workers_kimi_pinned():
    """_load_model_pins_from_yaml() matches the RENAMED constraint id
    (workers-kimi-pinned, not workers-sonnet-pinned) and loads its
    required_route.model (kimi-k3) for T1/T2/T3 from the real
    provider_constraints.yaml SSOT. T0 still resolves via t0-opus-only."""
    pins = _load_model_pins_from_yaml()
    assert pins["T0"] == "claude-opus-4-8"
    assert pins["T1"] == "kimi-k3"
    assert pins["T2"] == "kimi-k3"
    assert pins["T3"] == "kimi-k3"


def test_load_model_pins_from_yaml_ignores_stale_sonnet_pinned_id(tmp_path):
    """A constraints file that still uses the OLD id (workers-sonnet-pinned) with an
    old-style model must NOT be matched by the renamed loader's id check — it silently
    falls through to the hardcoded _DEFAULT_MODEL_PINS (kimi-k3), proving the id-string
    match was actually updated and isn't accidentally matching on role/model shape alone."""
    import yaml as _yaml
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "provider_constraints.yaml").write_text(_yaml.safe_dump({
        "version": 1,
        "constraints": [
            {
                "id": "workers-sonnet-pinned",
                "rule": "require_route",
                "required_route": {"role": ["T1", "T2", "T3"], "model": "claude-sonnet-5"},
            },
        ],
    }))
    with patch("dispatch_cli._LIB_DIR", tmp_path):
        pins = _load_model_pins_from_yaml()
    assert pins["T1"] == "kimi-k3", "stale workers-sonnet-pinned id must not override the default pin"
    assert pins["T2"] == "kimi-k3"
    assert pins["T3"] == "kimi-k3"


def test_raw_kimi_model_rejected_despite_workers_sonnet_pin(tmp_path, monkeypatch, capsys):
    """dispatch-agent-lane-coercion (20260713-LANECOERCE), defense-in-depth: a REAL staged spec
    with provider=claude, model=kimi, target_slot=T1 must be REJECTED by build_runtime_snapshot's
    raw-model guard, not silently pinned to the claude-lane model (workers-kimi-pinned since
    worker-provider-kimi-flip, 2026-07-23) before the kimi-via-cli-only check ever inspects the
    requested model. Uses the real constraint engine end-to-end (no snapshot mocking) so the fix
    is exercised exactly as dispatch_cli runs it."""
    data_dir = tmp_path / "vnx-data"
    staging_id = "20260713-staging-kimi-coercion"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    inst = bundle_dir / "instruction.md"
    inst.write_text("# Kimi-labelled dispatch\n\nDo something useful.\n", encoding="utf-8")
    spec_dict = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20260713-test-kimi-coercion",
        "staging_id": staging_id,
        "instruction_file": str(inst),
        "role": "backend-developer",
        "target_slot": "T1",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "model": "kimi",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec_dict), encoding="utf-8")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "provider=claude + model=kimi must be rejected, not silently pinned to sonnet"
    mock_execute.assert_not_called()
    err = capsys.readouterr().err
    assert "kimi-via-cli-only" in err


def test_raw_opus_model_pin_now_rejects_explicit_claude_override(tmp_path, monkeypatch):
    """worker-provider-kimi-flip (2026-07-23): the workers-kimi-pinned SSOT now resolves T1's
    pin to "kimi-k3" regardless of the requested model. An explicit provider=claude override on
    T1 (e.g. --model opus) is pinned to that same "kimi-k3" label, which is not a valid Claude
    model — the registry gate (check_registry=True) correctly REJECTS it (blocking,
    model-not-in-current-registry) instead of silently proceeding on a claude lane. This is the
    intended "kimi-only, no fallback" behavior: there is no warn-and-proceed escape hatch left
    for a claude override on a build-worker role. (Formerly this scenario pinned to
    claude-sonnet-5 and proceeded under workers-sonnet-pinned — see git history.)"""
    data_dir = tmp_path / "vnx-data"
    staging_id = "20260713-staging-opus-pin"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    inst = bundle_dir / "instruction.md"
    inst.write_text("# Opus-requested dispatch\n\nDo something useful.\n", encoding="utf-8")
    spec_dict = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20260713-test-opus-pin",
        "staging_id": staging_id,
        "instruction_file": str(inst),
        "role": "backend-developer",
        "target_slot": "T1",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "model": "opus",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec_dict), encoding="utf-8")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, (
        "requested opus on T1 pins to kimi-k3 (workers-kimi-pinned) and must be rejected — "
        "no silent claude fallback for a build-worker role"
    )
    mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# PR-5 — headless opt-in lane
# ---------------------------------------------------------------------------

@patch("dispatch_cli.build_runtime_snapshot")
@patch("dispatch_cli.run_envelope_headless_plan")
def test_headless_optin_routes_to_subprocess_adapter(mock_headless, mock_snapshot, tmp_path):
    """allow_headless=True + non-empty reason + VNX_OVERRIDE_CLAUDE_HEADLESS=1 →
    routes to run_envelope_headless_plan. tmux NOT called; permit passed to the envelope.

    The headless lane is blocked fail-closed by default (claude-headless constraint +
    _execute_claude_headless guard); the override flag is the explicit opt-in, so the
    routing path is only reachable with it set.
    """
    mock_snapshot.return_value = _clean_snapshot()
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_headless.return_value = mock_result

    spec_file = _make_spec_file(tmp_path, provider="claude", extra={
        "allow_headless": True,
        "headless_reason": "burst benchmark run",
    })

    with patch("tmux_interactive_dispatch.TmuxInteractiveDispatch.dispatch") as mock_tmux, \
         patch.dict(os.environ, {"VNX_OVERRIDE_CLAUDE_HEADLESS": "1"}):
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_headless.assert_called_once()
    mock_tmux.assert_not_called()

    plan_arg = mock_headless.call_args[0][0]
    assert plan_arg.lane == "claude_headless"
    assert plan_arg.billing == "api_metered"
    assert plan_arg.adapter == "claude_subprocess"


@patch("dispatch_cli.build_runtime_snapshot")
def test_headless_empty_reason_rejected(mock_snapshot, tmp_path, capsys):
    """allow_headless=True + empty headless_reason → Reject at validate() → rc=1, no spawn."""
    mock_snapshot.return_value = _clean_snapshot()

    spec_file = _make_spec_file(tmp_path, provider="claude", extra={
        "allow_headless": True,
        "headless_reason": "",
    })

    with patch("dispatch_cli._execute_claude_headless") as mock_headless:
        with patch("dispatch_cli._execute_claude") as mock_tmux:
            rc = run_dispatch(spec_file)

    assert rc == 1
    mock_headless.assert_not_called()
    mock_tmux.assert_not_called()
    err = capsys.readouterr().err
    assert "headless-reason-required" in err


def test_default_claude_still_routes_tmux(tmp_path, monkeypatch):
    """allow_headless absent/false → claude_tmux_subscription (unchanged default behavior)."""
    data_dir = tmp_path / "vnx-data"
    staging_id = "20260615-staging-default-tmux"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    inst = bundle_dir / "instruction.md"
    inst.write_text("# Default claude test\n\nDo something safe.\n", encoding="utf-8")
    spec_data = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20260615-default-claude-tmux",
        "staging_id": staging_id,
        "instruction_file": str(inst),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec_data), encoding="utf-8")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_tmux:
        with patch("dispatch_cli._execute_claude_headless") as mock_headless:
            rc = run_dispatch(spec_file)

    assert rc == 0
    mock_tmux.assert_called_once()
    mock_headless.assert_not_called()
    plan_arg = mock_tmux.call_args[0][0]
    assert plan_arg.lane == "claude_tmux_subscription"


def test_legacy_env_vars_do_not_bypass_headless_gate(tmp_path, monkeypatch):
    """VNX_AUTO_ROUTE=1 + VNX_ADAPTER=subprocess env vars have no effect through the door.
    Without allow_headless=true in the spec, the plan is always claude_tmux_subscription.
    """
    data_dir = tmp_path / "vnx-data"
    staging_id = "20260615-legacy-env-probe"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)

    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_AUTO_ROUTE", "1")
    monkeypatch.setenv("VNX_ADAPTER", "subprocess")

    inst = bundle_dir / "instruction.md"
    inst.write_text("# Legacy env test\n\nDo something safe.\n", encoding="utf-8")
    spec_data = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": "20260615-legacy-env-probe",
        "staging_id": staging_id,
        "instruction_file": str(inst),
        "role": "backend-developer",
        "target_slot": "T0",
        "gate": "human-promoted",
        "dispatch_paths": [],
        "provider": "claude",
        "deadline_seconds": 3600,
        "isolation": "worktree",
    }
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec_data), encoding="utf-8")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_tmux:
        with patch("dispatch_cli._execute_claude_headless") as mock_headless:
            rc = run_dispatch(spec_file)

    assert rc == 0
    mock_tmux.assert_called_once()
    mock_headless.assert_not_called()
    plan_arg = mock_tmux.call_args[0][0]
    assert plan_arg.lane == "claude_tmux_subscription"


# ---------------------------------------------------------------------------
# HIGH-1 — load_spec strict bool coercion for allow_headless / requires_mcp
# ---------------------------------------------------------------------------

class TestLoadSpecStrictBoolCoercion:
    """load_spec must parse allow_headless strictly: only JSON boolean true enables it.

    Closes the bool("false") == True coercion trap where a JSON string "false"
    would have been interpreted as True, silently enabling headless billing.
    """

    def _write_coercion_spec(self, tmp_path, allow_headless_raw, provider="claude"):
        instruction = _make_instruction(tmp_path)
        spec = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260615-coercion-test",
            "staging_id": "test-stage",
            "instruction_file": str(instruction),
            "role": "backend-developer",
            "target_slot": "T1",
            "gate": "human-promoted",
            "dispatch_paths": [],
            "provider": provider,
            "deadline_seconds": 3600,
            "isolation": "worktree",
            "allow_headless": allow_headless_raw,
        }
        spec_file = tmp_path / "dispatch-spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        return spec_file

    def test_string_false_does_not_enable_headless(self, tmp_path):
        """JSON string 'false' → allow_headless=False (was bool('false')==True before fix)."""
        spec_file = self._write_coercion_spec(tmp_path, "false")
        loaded = load_spec(spec_file)
        assert loaded.allow_headless is False, (
            "allow_headless='false' (string) must NOT enable headless; bool('false') is the old bug"
        )

    def test_string_true_does_not_enable_headless(self, tmp_path):
        """JSON string 'true' → allow_headless=False (only JSON boolean true enables)."""
        spec_file = self._write_coercion_spec(tmp_path, "true")
        loaded = load_spec(spec_file)
        assert loaded.allow_headless is False

    def test_integer_0_does_not_enable_headless(self, tmp_path):
        """JSON integer 0 → allow_headless=False."""
        spec_file = self._write_coercion_spec(tmp_path, 0)
        loaded = load_spec(spec_file)
        assert loaded.allow_headless is False

    def test_integer_1_does_not_enable_headless(self, tmp_path):
        """JSON integer 1 → allow_headless=False (only JSON boolean true, not numeric 1)."""
        spec_file = self._write_coercion_spec(tmp_path, 1)
        loaded = load_spec(spec_file)
        assert loaded.allow_headless is False

    def test_empty_string_does_not_enable_headless(self, tmp_path):
        """JSON empty string '' → allow_headless=False."""
        spec_file = self._write_coercion_spec(tmp_path, "")
        loaded = load_spec(spec_file)
        assert loaded.allow_headless is False

    def test_json_bool_true_enables_headless(self, tmp_path):
        """JSON boolean true → allow_headless=True (the ONLY accepted value)."""
        spec_file = self._write_coercion_spec(tmp_path, True)
        loaded = load_spec(spec_file)
        assert loaded.allow_headless is True

    def test_json_bool_false_does_not_enable_headless(self, tmp_path):
        """JSON boolean false → allow_headless=False."""
        spec_file = self._write_coercion_spec(tmp_path, False)
        loaded = load_spec(spec_file)
        assert loaded.allow_headless is False

    def test_string_false_on_non_claude_does_not_trigger_headless_claude_only_reject(self, tmp_path):
        """Regression: string 'false' on a codex spec must not enable headless.

        Before the fix, bool('false')==True would have enabled headless on a codex spec,
        causing validate() to reject it with headless-claude-only instead of passing cleanly.
        """
        spec_file = self._write_coercion_spec(tmp_path, "false", provider="codex")
        loaded = load_spec(spec_file)
        assert loaded.allow_headless is False


# ---------------------------------------------------------------------------
# LOW — headless_reason sanitization in load_spec
# ---------------------------------------------------------------------------

class TestLoadSpecHeadlessReasonSanitization:

    def _write_spec_with_reason(self, tmp_path, headless_reason_raw):
        instruction = _make_instruction(tmp_path)
        spec = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260615-reason-sanitize-test",
            "staging_id": "test-stage",
            "instruction_file": str(instruction),
            "role": "backend-developer",
            "target_slot": "T1",
            "gate": "human-promoted",
            "dispatch_paths": [],
            "provider": "claude",
            "deadline_seconds": 3600,
            "isolation": "worktree",
            "allow_headless": True,
            "headless_reason": headless_reason_raw,
        }
        spec_file = tmp_path / "dispatch-spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        return spec_file

    def test_multiline_reason_has_newlines_removed(self, tmp_path):
        """Newlines in headless_reason are replaced so the reason stays on one log line."""
        spec_file = self._write_spec_with_reason(tmp_path, "line one\nline two\nline three")
        loaded = load_spec(spec_file)
        assert "\n" not in (loaded.headless_reason or "")
        assert "line one" in (loaded.headless_reason or "")

    def test_control_chars_stripped(self, tmp_path):
        """Control chars (\\r, \\x00, etc.) are stripped from headless_reason."""
        spec_file = self._write_spec_with_reason(tmp_path, "benchmark\x00run\r\nfast")
        loaded = load_spec(spec_file)
        reason = loaded.headless_reason or ""
        assert "\x00" not in reason
        assert "\r" not in reason
        assert "\n" not in reason

    def test_clean_reason_unchanged(self, tmp_path):
        """A clean single-line reason passes through unchanged."""
        spec_file = self._write_spec_with_reason(tmp_path, "burst benchmark run")
        loaded = load_spec(spec_file)
        assert loaded.headless_reason == "burst benchmark run"

    def test_none_reason_stays_none(self, tmp_path):
        """Missing headless_reason stays None."""
        instruction = _make_instruction(tmp_path)
        spec = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260615-no-reason-test",
            "staging_id": "test-stage",
            "instruction_file": str(instruction),
            "role": "backend-developer",
            "target_slot": "T1",
            "gate": "human-promoted",
            "dispatch_paths": [],
            "provider": "claude",
            "deadline_seconds": 3600,
            "isolation": "worktree",
            "allow_headless": False,
        }
        spec_file = tmp_path / "dispatch-spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        loaded = load_spec(spec_file)
        assert loaded.headless_reason is None


# ---------------------------------------------------------------------------
# TL-D1 — track_id: load_spec parsing
# ---------------------------------------------------------------------------

class TestLoadSpecTrackId:
    def test_track_id_parsed(self, tmp_path):
        spec_file = _make_spec_file(tmp_path, extra={"track_id": "track-linkage-enforcement"})
        loaded = load_spec(spec_file)
        assert loaded.track_id == "track-linkage-enforcement"

    def test_track_id_absent_is_none(self, tmp_path):
        spec_file = _make_spec_file(tmp_path)
        loaded = load_spec(spec_file)
        assert loaded.track_id is None

    def test_track_id_empty_string_is_none(self, tmp_path):
        spec_file = _make_spec_file(tmp_path, extra={"track_id": ""})
        loaded = load_spec(spec_file)
        assert loaded.track_id is None


# ---------------------------------------------------------------------------
# TL-D1 — track_id: tracks DB fixture helper
# ---------------------------------------------------------------------------

def _make_tracks_db(
    state_dir: Path,
    *,
    tracks: "dict[str, str] | None" = None,
    dispatches: "list[dict] | None" = None,
    dispatches_has_track_id_col: bool = False,
) -> Path:
    """Build a minimal runtime_coordination.db with `tracks` (+ optionally `dispatches`).

    tracks: {track_id: phase}. dispatches: list of {dispatch_id, project_id, state}.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tracks (
            track_id TEXT NOT NULL PRIMARY KEY,
            phase TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev'
        )
        """
    )
    for tid, phase in (tracks or {}).items():
        conn.execute(
            "INSERT INTO tracks (track_id, phase, project_id) VALUES (?, ?, 'vnx-dev')",
            (tid, phase),
        )
    if dispatches is not None:
        track_col = ", track_id TEXT" if dispatches_has_track_id_col else ""
        conn.execute(
            f"""
            CREATE TABLE dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT 'vnx-dev',
                state TEXT NOT NULL DEFAULT 'queued'{track_col},
                UNIQUE(dispatch_id, project_id)
            )
            """
        )
        for row in dispatches:
            conn.execute(
                "INSERT INTO dispatches (dispatch_id, project_id, state) VALUES (?, ?, ?)",
                (row["dispatch_id"], row.get("project_id", "vnx-dev"), row.get("state", "queued")),
            )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# TL-D1 — _check_track_link_verdict unit tests
# ---------------------------------------------------------------------------

class TestCheckTrackLinkVerdict:
    def test_valid_live_track_passes(self, tmp_path):
        state_dir = tmp_path / "state"
        _make_tracks_db(state_dir, tracks={"my-track": "active"})
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id="my-track",
        )
        assert _check_track_link_verdict(spec, state_dir=state_dir) is None

    def test_nonexistent_track_rejects(self, tmp_path):
        state_dir = tmp_path / "state"
        _make_tracks_db(state_dir, tracks={"other-track": "active"})
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id="does-not-exist",
        )
        verdict = _check_track_link_verdict(spec, state_dir=state_dir)
        assert verdict is not None
        assert verdict.severity == "blocking"
        assert verdict.code == "bad-track-link"

    def test_done_track_rejects(self, tmp_path):
        state_dir = tmp_path / "state"
        _make_tracks_db(state_dir, tracks={"finished-track": "done"})
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id="finished-track",
        )
        verdict = _check_track_link_verdict(spec, state_dir=state_dir)
        assert verdict is not None
        assert verdict.severity == "blocking"
        assert verdict.code == "bad-track-link"

    def test_wrong_project_id_treated_as_nonexistent(self, tmp_path):
        """A track that exists but under a different project_id must not leak across tenants."""
        state_dir = tmp_path / "state"
        db_path = _make_tracks_db(state_dir, tracks={})
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO tracks (track_id, phase, project_id) VALUES ('shared-name', 'active', 'other-project')"
        )
        conn.commit()
        conn.close()
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id="shared-name",
        )
        verdict = _check_track_link_verdict(spec, state_dir=state_dir)
        assert verdict is not None
        assert verdict.severity == "blocking"
        assert verdict.code == "bad-track-link"

    def test_missing_tracks_db_degrades_to_warn(self, tmp_path, monkeypatch):
        """Never crash: a present track_id with no tracks DB at all -> WARN, not an exception."""
        state_dir = tmp_path / "state-does-not-exist"
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id="my-track",
        )
        verdict = _check_track_link_verdict(spec, state_dir=state_dir)
        assert verdict is not None
        assert verdict.severity == "warn"
        assert verdict.code == "tracks-db-unavailable"

    def test_absent_flag_off_warns(self, tmp_path, monkeypatch):
        import config_runtime
        monkeypatch.setattr(config_runtime, "get_bool", lambda key: False)
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id=None,
        )
        verdict = _check_track_link_verdict(spec, state_dir=tmp_path / "state")
        assert verdict is not None
        assert verdict.severity == "warn"
        assert verdict.code == "track_unlinked"

    def test_absent_flag_on_no_escape_rejects(self, tmp_path, monkeypatch):
        import config_runtime
        monkeypatch.setattr(config_runtime, "get_bool", lambda key: True)
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id=None, tags=(),
        )
        verdict = _check_track_link_verdict(spec, state_dir=tmp_path / "state")
        assert verdict is not None
        assert verdict.severity == "blocking"
        assert verdict.code == "track-required"

    def test_absent_flag_on_with_escape_passes(self, tmp_path, monkeypatch):
        import config_runtime
        monkeypatch.setattr(config_runtime, "get_bool", lambda key: True)
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id=None,
            tags=("no-track:exploratory spike",),
        )
        verdict = _check_track_link_verdict(spec, state_dir=tmp_path / "state")
        assert verdict is None


# ---------------------------------------------------------------------------
# TL-D1 — _persist_track_id unit tests
# ---------------------------------------------------------------------------

class TestPersistTrackId:
    def test_persists_onto_existing_row(self, tmp_path):
        state_dir = tmp_path / "state"
        _make_tracks_db(
            state_dir,
            dispatches=[{"dispatch_id": "d1", "project_id": "vnx-dev", "state": "queued"}],
        )
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id="my-track",
        )
        _persist_track_id(spec, state_dir=state_dir)

        conn = sqlite3.connect(str(_tracks_db_path(state_dir)))
        row = conn.execute(
            "SELECT track_id, state FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
            ("d1", "vnx-dev"),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "my-track"
        assert row[1] == "queued", "persistence must never mutate the state column"

    def test_adds_track_id_column_when_missing(self, tmp_path):
        state_dir = tmp_path / "state"
        _make_tracks_db(
            state_dir,
            dispatches=[{"dispatch_id": "d1"}],
            dispatches_has_track_id_col=False,
        )
        conn = sqlite3.connect(str(_tracks_db_path(state_dir)))
        assert not _has_col(conn, "dispatches", "track_id")
        conn.close()

        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id="my-track",
        )
        _persist_track_id(spec, state_dir=state_dir)

        conn = sqlite3.connect(str(_tracks_db_path(state_dir)))
        assert _has_col(conn, "dispatches", "track_id")
        row = conn.execute("SELECT track_id FROM dispatches WHERE dispatch_id = 'd1'").fetchone()
        conn.close()
        assert row[0] == "my-track"

    def test_noop_when_no_matching_row(self, tmp_path):
        """UPDATE-only: no pre-existing dispatches row (the leaseless claude-tmux lane's
        normal case today) must be a safe no-op, never an INSERT, never a raise."""
        state_dir = tmp_path / "state"
        _make_tracks_db(state_dir, dispatches=[])
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="ghost-dispatch", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id="my-track",
        )
        _persist_track_id(spec, state_dir=state_dir)  # must not raise

        conn = sqlite3.connect(str(_tracks_db_path(state_dir)))
        count = conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
        conn.close()
        assert count == 0, "must never INSERT a synthetic dispatches row"

    def test_noop_when_track_id_absent(self, tmp_path):
        state_dir = tmp_path / "state"
        _make_tracks_db(
            state_dir,
            dispatches=[{"dispatch_id": "d1", "project_id": "vnx-dev", "state": "queued"}],
        )
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id=None,
        )
        _persist_track_id(spec, state_dir=state_dir)  # must not raise, must not touch the DB

    def test_noop_when_db_missing(self, tmp_path):
        """Never crash when the tracks DB file doesn't exist at all."""
        state_dir = tmp_path / "state-does-not-exist"
        spec = DispatchSpec(
            schema_version=1, project_id="vnx-dev", dispatch_id="d1", staging_id="s1",
            instruction_file=Path("/fake"), role="backend-developer", target_slot="T1",
            gate="human-promoted", dispatch_paths=(), track_id="my-track",
        )
        _persist_track_id(spec, state_dir=state_dir)  # must not raise


# ---------------------------------------------------------------------------
# TL-D1 — end-to-end run_dispatch tests
# ---------------------------------------------------------------------------

class TestTrackIdEndToEnd:
    def test_valid_track_id_persists_and_proceeds(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "vnx-data"
        state_dir = data_dir / "state"
        staging_id = "20260706-staging-tl-d1"
        bundle_dir = data_dir / "dispatches" / "pending" / staging_id
        bundle_dir.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.delenv("VNX_CURRENT_TRACK_ID", raising=False)

        _make_tracks_db(
            state_dir,
            tracks={"track-linkage-enforcement": "active"},
            dispatches=[{"dispatch_id": "20260706-tl-e2e-valid", "project_id": "vnx-dev", "state": "queued"}],
        )

        instruction = bundle_dir / "instruction.md"
        instruction.write_text("Do something useful.", encoding="utf-8")
        spec_dict = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260706-tl-e2e-valid",
            "staging_id": staging_id,
            "instruction_file": str(instruction),
            "role": "backend-developer",
            "target_slot": "T0",
            "gate": "human-promoted",
            "dispatch_paths": [],
            "provider": "claude",
            "deadline_seconds": 3600,
            "isolation": "worktree",
            "track_id": "track-linkage-enforcement",
        }
        spec_file = bundle_dir / "dispatch-spec.json"
        spec_file.write_text(json.dumps(spec_dict), encoding="utf-8")

        with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
            rc = run_dispatch(spec_file)

        assert rc == 0
        mock_execute.assert_called_once()
        assert os.environ.get("VNX_CURRENT_TRACK_ID") == "track-linkage-enforcement"

        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        row = conn.execute(
            "SELECT track_id FROM dispatches WHERE dispatch_id = ?", ("20260706-tl-e2e-valid",)
        ).fetchone()
        conn.close()
        assert row[0] == "track-linkage-enforcement"

    def test_nonexistent_track_id_rejects(self, tmp_path, monkeypatch, capsys):
        data_dir = tmp_path / "vnx-data"
        state_dir = data_dir / "state"
        staging_id = "20260706-staging-tl-d1-bad"
        bundle_dir = data_dir / "dispatches" / "pending" / staging_id
        bundle_dir.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        _make_tracks_db(state_dir, tracks={"some-other-track": "active"})

        instruction = bundle_dir / "instruction.md"
        instruction.write_text("Do something useful.", encoding="utf-8")
        spec_dict = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260706-tl-e2e-bad",
            "staging_id": staging_id,
            "instruction_file": str(instruction),
            "role": "backend-developer",
            "target_slot": "T0",
            "gate": "human-promoted",
            "dispatch_paths": [],
            "provider": "claude",
            "deadline_seconds": 3600,
            "isolation": "worktree",
            "track_id": "does-not-exist",
        }
        spec_file = bundle_dir / "dispatch-spec.json"
        spec_file.write_text(json.dumps(spec_dict), encoding="utf-8")

        with patch("dispatch_cli._execute_claude") as mock_execute:
            rc = run_dispatch(spec_file)

        assert rc == 1
        mock_execute.assert_not_called()
        err = capsys.readouterr().err
        assert "bad-track-link" in err

    def test_absent_track_id_default_off_warns_and_proceeds(self, tmp_path, monkeypatch, capsys):
        """Flag OFF (default): an unlinked dispatch WARNs (visible in dry-run) but proceeds."""
        data_dir = tmp_path / "vnx-data"
        staging_id = "20260706-staging-tl-d1-unlinked"
        bundle_dir = data_dir / "dispatches" / "pending" / staging_id
        bundle_dir.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        instruction = bundle_dir / "instruction.md"
        instruction.write_text("Do something useful.", encoding="utf-8")
        spec_dict = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260706-tl-e2e-unlinked",
            "staging_id": staging_id,
            "instruction_file": str(instruction),
            "role": "backend-developer",
            "target_slot": "T0",
            "gate": "human-promoted",
            "dispatch_paths": [],
            "provider": "claude",
            "deadline_seconds": 3600,
            "isolation": "worktree",
        }
        spec_file = bundle_dir / "dispatch-spec.json"
        spec_file.write_text(json.dumps(spec_dict), encoding="utf-8")

        rc = run_dispatch(spec_file, dry_run=True)

        assert rc == 0
        out = capsys.readouterr().out
        assert "track_unlinked" in out

    def test_absent_track_id_flag_on_rejects(self, tmp_path, monkeypatch, capsys):
        data_dir = tmp_path / "vnx-data"
        staging_id = "20260706-staging-tl-d1-required"
        bundle_dir = data_dir / "dispatches" / "pending" / staging_id
        bundle_dir.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        instruction = bundle_dir / "instruction.md"
        instruction.write_text("Do something useful.", encoding="utf-8")
        spec_dict = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260706-tl-e2e-required",
            "staging_id": staging_id,
            "instruction_file": str(instruction),
            "role": "backend-developer",
            "target_slot": "T0",
            "gate": "human-promoted",
            "dispatch_paths": [],
            "provider": "claude",
            "deadline_seconds": 3600,
            "isolation": "worktree",
        }
        spec_file = bundle_dir / "dispatch-spec.json"
        spec_file.write_text(json.dumps(spec_dict), encoding="utf-8")

        import config_runtime
        monkeypatch.setattr(config_runtime, "get_bool", lambda key: True)

        with patch("dispatch_cli._execute_claude") as mock_execute:
            rc = run_dispatch(spec_file)

        assert rc == 1
        mock_execute.assert_not_called()
        err = capsys.readouterr().err
        assert "track-required" in err

    def test_absent_track_id_flag_on_with_escape_proceeds(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "vnx-data"
        staging_id = "20260706-staging-tl-d1-escape"
        bundle_dir = data_dir / "dispatches" / "pending" / staging_id
        bundle_dir.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        instruction = bundle_dir / "instruction.md"
        instruction.write_text("Do something useful.", encoding="utf-8")
        spec_dict = {
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260706-tl-e2e-escape",
            "staging_id": staging_id,
            "instruction_file": str(instruction),
            "role": "backend-developer",
            "target_slot": "T0",
            "gate": "human-promoted",
            "dispatch_paths": [],
            "provider": "claude",
            "deadline_seconds": 3600,
            "isolation": "worktree",
            "tags": ["no-track:exploratory spike"],
        }
        spec_file = bundle_dir / "dispatch-spec.json"
        spec_file.write_text(json.dumps(spec_dict), encoding="utf-8")

        import config_runtime
        monkeypatch.setattr(config_runtime, "get_bool", lambda key: True)

        with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
            rc = run_dispatch(spec_file)

        assert rc == 0
        mock_execute.assert_called_once()


# ---------------------------------------------------------------------------
# worker-claude-override (escape-hatch-worker-claude, 2026-07-23) — gated,
# audited operator escape-hatch routing ONE build-worker dispatch back to claude
# ---------------------------------------------------------------------------

def _clear_worker_claude_override_env(monkeypatch) -> None:
    """Guarantee a clean override env regardless of the ambient operator env."""
    monkeypatch.delenv("VNX_OVERRIDE_WORKER_CLAUDE", raising=False)
    monkeypatch.delenv("VNX_OVERRIDE_WORKER_CLAUDE_REASON", raising=False)


def test_worker_claude_override_routes_build_worker_to_claude_tmux(tmp_path, monkeypatch):
    """Override env + reason + provider=claude + T1 => the kimi-k3 pin coercion is
    skipped for THIS dispatch: effective/plan model is a claude model, the
    constraint check passes, and the route is the claude tmux-subscription lane.
    An audited `worker-claude-override-applied` entry carrying the reason, the
    target_slot, and the resolved model lands on the plan's governed record."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# Escape hatch\n\nRoute this one build task to claude.\n",
        staging_id="20260723-staging-escape-hatch",
        dispatch_id="20260723-escape-hatch-apply",
        provider="claude",
        target_slot="T1",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_OVERRIDE_WORKER_CLAUDE", "1")
    monkeypatch.setenv(
        "VNX_OVERRIDE_WORKER_CLAUDE_REASON",
        "kimi failed 2x on this chain-critical task (C1 escalation)",
    )

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0, "gated override must route the dispatch, not reject it"
    mock_execute.assert_called_once()
    plan_arg = mock_execute.call_args[0][0]
    assert plan_arg.lane == "claude_tmux_subscription"
    assert plan_arg.provider == Provider.CLAUDE
    assert plan_arg.billing == "subscription"
    assert plan_arg.model == "sonnet", (
        "pin coercion skipped: no spec.model -> claude-lane default 'sonnet', "
        f"not the kimi-k3 pin (got {plan_arg.model!r})"
    )
    audit = [w for w in plan_arg.warnings if "worker-claude-override-applied" in w]
    assert audit, f"audited override verdict missing from plan warnings: {plan_arg.warnings}"
    assert "kimi failed 2x on this chain-critical task (C1 escalation)" in audit[0]
    assert "T1" in audit[0]
    assert "sonnet" in audit[0]


def test_worker_claude_override_honors_requested_claude_model(tmp_path, monkeypatch):
    """Override + explicit spec.model=opus => plan.model is opus (requested claude
    model), still via the tmux-subscription lane."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# Escape hatch\n\nRoute this one build task to opus.\n",
        staging_id="20260723-staging-escape-hatch-opus",
        dispatch_id="20260723-escape-hatch-opus",
        provider="claude",
        target_slot="T2",
        model="opus",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_OVERRIDE_WORKER_CLAUDE", "1")
    monkeypatch.setenv("VNX_OVERRIDE_WORKER_CLAUDE_REASON", "operator pre-assessed opus-depth task")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_execute.assert_called_once()
    plan_arg = mock_execute.call_args[0][0]
    assert plan_arg.lane == "claude_tmux_subscription"
    assert plan_arg.model == "opus"
    assert any("worker-claude-override-applied" in w for w in plan_arg.warnings)


@pytest.mark.parametrize("reason_value", [None, "", "   "])
def test_worker_claude_override_without_reason_is_blocking_refusal(tmp_path, monkeypatch, capsys, reason_value):
    """Override env set but reason absent/empty/whitespace => blocking refusal
    (worker-claude-override-reason-required). The override is inert without an
    audit reason; no executor runs."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# Escape hatch\n\nNo reason given.\n",
        staging_id="20260723-staging-escape-hatch-noreason",
        dispatch_id="20260723-escape-hatch-noreason",
        provider="claude",
        target_slot="T1",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_OVERRIDE_WORKER_CLAUDE", "1")
    if reason_value is None:
        monkeypatch.delenv("VNX_OVERRIDE_WORKER_CLAUDE_REASON", raising=False)
    else:
        monkeypatch.setenv("VNX_OVERRIDE_WORKER_CLAUDE_REASON", reason_value)

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "override without a reason must be a blocking refusal"
    mock_execute.assert_not_called()
    err = capsys.readouterr().err
    assert "worker-claude-override-reason-required" in err


def test_no_override_claude_on_build_worker_still_hard_rejects(tmp_path, monkeypatch, capsys):
    """DEFAULT INTACT: no override env + provider=claude + T1 => still hard-rejected
    via the kimi-k3 registry failure (model-not-in-current-registry, blocking).
    No silent claude fallback is ever introduced."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# No override\n\nThis must keep hard-rejecting.\n",
        staging_id="20260723-staging-no-override",
        dispatch_id="20260723-no-override-reject",
        provider="claude",
        target_slot="T1",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _clear_worker_claude_override_env(monkeypatch)

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "default kimi-k3 hard-reject must stand without the override env"
    mock_execute.assert_not_called()
    err = capsys.readouterr().err
    # The emergent reject surfaces as the first blocking verdict for a kimi-branded
    # model on the claude lane (kimi-via-cli-only fires ahead of the registry gate).
    assert "kimi-via-cli-only" in err or "model-not-in-current-registry" in err
    assert "worker-claude-override" not in err


def test_no_override_kimi_build_dispatch_unchanged(tmp_path, monkeypatch):
    """DEFAULT INTACT: a normal kimi build dispatch on T1 (no override env) routes
    to the provider lane exactly as before — no override artifacts on the plan."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# Normal kimi build dispatch\n\nUnchanged path.\n",
        staging_id="20260723-staging-kimi-default",
        dispatch_id="20260723-kimi-default",
        provider="kimi",
        target_slot="T1",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    _clear_worker_claude_override_env(monkeypatch)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.status = "success"
    with patch("dispatch_cli.run_envelope_plan", return_value=mock_result) as mock_envelope:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_envelope.assert_called_once()
    plan_arg = mock_envelope.call_args[0][0]
    assert plan_arg.lane == "provider"
    assert plan_arg.provider == Provider.KIMI
    assert not any("worker-claude-override" in w for w in plan_arg.warnings)


def test_worker_claude_override_cannot_smuggle_mismatched_model(tmp_path, monkeypatch, capsys):
    """Belt-and-suspenders intact: override + reason but spec.model is kimi-branded
    => the constraint engine still blocks (kimi-via-cli-only / registry). The
    override skips the pin coercion only; it is claude->claude, never a hole the
    kimi-via-cli-only guard can be smuggled through."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# Escape hatch abuse attempt\n\nkimi model on the claude lane.\n",
        staging_id="20260723-staging-escape-hatch-abuse",
        dispatch_id="20260723-escape-hatch-abuse",
        provider="claude",
        target_slot="T1",
        model="kimi-k3",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_OVERRIDE_WORKER_CLAUDE", "1")
    monkeypatch.setenv("VNX_OVERRIDE_WORKER_CLAUDE_REASON", "attempt to route kimi via the claude lane")

    with patch("dispatch_cli._execute_claude") as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1, "a kimi-branded model on the claude lane must still hard-reject"
    mock_execute.assert_not_called()
    err = capsys.readouterr().err
    assert "kimi-via-cli-only" in err or "model-not-in-current-registry" in err


def test_worker_claude_override_env_does_not_leak_into_t0_or_kimi(tmp_path, monkeypatch):
    """Scope guard: the override only applies to provider=claude on T1/T2/T3.
    Override env + provider=claude on T0 is unaffected (T0 pin path unchanged:
    claude-opus-4-8, NO override audit entry)."""
    data_dir, spec_file = _make_bundle_spec(
        tmp_path,
        instruction_text="# T0 dispatch with stray override env\n\nPin path unchanged.\n",
        staging_id="20260723-staging-override-t0",
        dispatch_id="20260723-override-t0",
        provider="claude",
        target_slot="T0",
    )
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_OVERRIDE_WORKER_CLAUDE", "1")
    monkeypatch.setenv("VNX_OVERRIDE_WORKER_CLAUDE_REASON", "stray env must not affect T0")

    with patch("dispatch_cli._execute_claude", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 0
    mock_execute.assert_called_once()
    plan_arg = mock_execute.call_args[0][0]
    assert plan_arg.model == "claude-opus-4-8", (
        f"T0 pin (t0-opus-only) must be unaffected by the worker override (got {plan_arg.model!r})"
    )
    assert not any("worker-claude-override" in w for w in plan_arg.warnings)
