"""Characterisation of two door steps inside ``run_dispatch``.

Step A is the pre-validate routing: the smart router fills provider+model for
an AUTO spec, or the door falls back to CLAUDE with a deterministic reason.
Step B is the gate fill (and, after it, the gate-required refusal for a wholly
silent writing spec). golf-2-hardenen G2-8 extracts both steps into private
helpers; this file pins the behaviour those helpers must preserve — the printed
``REJECT [<code>]`` line with its exact text, the exit code, and the
provider/model/route_reason that lands on the dry-run plan.

The fixtures mirror ``tests/test_dispatch_cli.py`` (staged bundle + clean
snapshot + patched ``build_runtime_snapshot``) and
``tests/test_gate_obligation_follows_review_stack.py`` (the operator's review
stack). Router/gate resolvers are patched in the *consumer* namespace
(``dispatch_cli.<name>``) — the name the door looks up — never in the source
module, and every patched seam asserts it was actually called where the point
of the test is that it ran.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_cli
from dispatch_cli import run_dispatch
from dispatch_plan import ModelPin, RuntimeSnapshot
from dispatch_spec import Provider, ReviewGateConfigError
from providers.provider_registry import RegistryLookupError
from providers.smart_router.door_routing import DoorRouteResult


@pytest.fixture(autouse=True)
def _legacy_ledger_lifecycle(monkeypatch):
    """These door tests run a fresh, un-migrated store (init_schema only, no
    project_id on ``dispatches``), so they exercise the legacy lifecycle mode."""
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "0")


@pytest.fixture(autouse=True)
def _clear_operator_choice(monkeypatch):
    """Leave the review stack to whatever this project resolves on its own —
    the gate step under test patches the resolver directly, so the machine's
    real stack must not decide anything here."""
    monkeypatch.delenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", raising=False)
    monkeypatch.delenv("VNX_DEFAULT_REVIEW_STACK", raising=False)
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")


def _make_bundle_spec(
    tmp_path: Path,
    *,
    provider: str = "claude",
    model: "str | None" = None,
    gate: str = "codex_gate",
    role: str = "backend-developer",
    dispatch_id: str = "20261106-g28-dispatch",
    staging_id: str = "20261106-g28-staging",
) -> tuple[Path, Path]:
    """A promoted-style staged bundle: spec + instruction inside the bundle dir."""
    data_dir = tmp_path / "vnx-data"
    bundle_dir = data_dir / "dispatches" / "pending" / staging_id
    bundle_dir.mkdir(parents=True)
    instruction = bundle_dir / "instruction.md"
    instruction.write_text(
        "# Test Dispatch\n\nRole: backend-developer\n\nDo something useful.\n",
        encoding="utf-8",
    )
    spec = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction),
        "role": role,
        "target_slot": "T1",
        "gate": gate,
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
    spec_file = bundle_dir / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    return data_dir, spec_file


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


def _plan_field(out: str, name: str) -> str:
    """Value of a ``  <name>: <value>`` dry-run plan line, fail-loud when absent."""
    prefix = f"  {name}:"
    for line in out.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise AssertionError(f"dry-run output has no {name!r} line:\n{out}")


def _env(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")


def _gate_filling_resolver(gate: str, reason: str):
    """A stand-in for ``_resolve_gate_via_router`` that fills *gate* and returns *reason*."""
    def _resolve(vspec):
        filled = dataclasses.replace(vspec, spec=dataclasses.replace(vspec.spec, gate=gate))
        return filled, reason
    return _resolve


# ---------------------------------------------------------------------------
# Step A — the pre-validate routing
# ---------------------------------------------------------------------------

def test_explicit_provider_never_calls_the_router(tmp_path, monkeypatch, capsys):
    """An explicit provider on the spec is worker-provider-free-choice: the
    router must not be consulted and no smart-router reason may reach the plan."""
    data_dir, spec_file = _make_bundle_spec(tmp_path, provider="claude")
    _env(monkeypatch, data_dir)

    with patch("dispatch_cli.build_runtime_snapshot") as mock_snapshot, \
         patch("dispatch_cli._resolve_router_pre_validate") as mock_router:
        mock_snapshot.return_value = _clean_snapshot()
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 0, capsys.readouterr().err
    mock_router.assert_not_called()
    out = capsys.readouterr().out
    assert _plan_field(out, "provider") == "claude"
    assert "smart-router" not in _plan_field(out, "route_reason")


def test_auto_spec_router_route_fills_provider_and_model(tmp_path, monkeypatch, capsys):
    """provider=auto + a router route → the resolved provider/model land on the
    plan and the route reason is carried on route_reason."""
    data_dir, spec_file = _make_bundle_spec(tmp_path, provider="auto")
    _env(monkeypatch, data_dir)
    reason = "smart-router:governance_variant=default gate=codex_gate"

    with patch("dispatch_cli.build_runtime_snapshot") as mock_snapshot, \
         patch(
             "dispatch_cli._resolve_router_pre_validate",
             return_value=DoorRouteResult(route=(Provider.CLAUDE, "sonnet", reason)),
         ) as mock_router:
        mock_snapshot.return_value = _clean_snapshot()
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 0, capsys.readouterr().err
    assert mock_router.call_count == 1
    out = capsys.readouterr().out
    assert _plan_field(out, "provider") == "claude"
    assert _plan_field(out, "model") == "sonnet"
    assert _plan_field(out, "route_reason").startswith(reason + ";")


def test_auto_spec_router_decline_with_reason_names_it(tmp_path, monkeypatch, capsys):
    """A router decline carries its reason into the deterministic fallback text."""
    data_dir, spec_file = _make_bundle_spec(tmp_path, provider="auto")
    _env(monkeypatch, data_dir)

    with patch("dispatch_cli.build_runtime_snapshot") as mock_snapshot, \
         patch(
             "dispatch_cli._resolve_router_pre_validate",
             return_value=DoorRouteResult(decline_reason="provider-not-mapped"),
         ) as mock_router:
        mock_snapshot.return_value = _clean_snapshot()
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 0, capsys.readouterr().err
    assert mock_router.call_count == 1
    out = capsys.readouterr().out
    assert _plan_field(out, "provider") == "claude"
    assert _plan_field(out, "model") == "sonnet"
    assert _plan_field(out, "route_reason").startswith(
        "smart-router:no-route,reason=provider-not-mapped,fallback=sonnet;"
    )


def test_auto_spec_router_decline_without_reason_uses_bare_fallback_text(
    tmp_path, monkeypatch, capsys
):
    """No decline reason → the bare no-route text, still deterministic."""
    data_dir, spec_file = _make_bundle_spec(tmp_path, provider="auto")
    _env(monkeypatch, data_dir)

    with patch("dispatch_cli.build_runtime_snapshot") as mock_snapshot, \
         patch(
             "dispatch_cli._resolve_router_pre_validate",
             return_value=DoorRouteResult(),
         ):
        mock_snapshot.return_value = _clean_snapshot()
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert _plan_field(out, "provider") == "claude"
    assert _plan_field(out, "route_reason").startswith(
        "smart-router:no-route,fallback=sonnet;"
    )


def test_auto_spec_registry_drift_is_refused_by_name(tmp_path, monkeypatch, capsys):
    """ADR-036 §2: router/registry drift is rejected loudly, never a silent
    fallback to the default lane."""
    data_dir, spec_file = _make_bundle_spec(tmp_path, provider="auto")
    _env(monkeypatch, data_dir)

    with patch(
        "dispatch_cli._resolve_router_pre_validate",
        side_effect=RegistryLookupError("provider 'bogus' is not a known dispatch Provider"),
    ), patch("dispatch_cli._execute_claude_headless") as mock_exec:
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 1
    assert (
        "[dispatch_cli] REJECT [router-registry-drift]: provider 'bogus' is not a "
        "known dispatch Provider"
    ) in capsys.readouterr().err
    mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# Step B — the gate fill and the gate-required refusal
# ---------------------------------------------------------------------------

def test_declared_gate_is_kept_and_adds_no_gate_reason(tmp_path, monkeypatch, capsys):
    """An explicit gate on the spec always wins: the door passes it through
    unchanged and merges no router reason into route_reason."""
    data_dir, spec_file = _make_bundle_spec(tmp_path, gate="codex_gate")
    _env(monkeypatch, data_dir)
    seen: dict = {}

    def _resolve(vspec):
        seen["gate"] = vspec.spec.gate
        return vspec, None

    with patch("dispatch_cli.build_runtime_snapshot") as mock_snapshot, \
         patch("dispatch_cli._resolve_gate_via_router", side_effect=_resolve):
        mock_snapshot.return_value = _clean_snapshot()
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 0, capsys.readouterr().err
    assert seen["gate"] == "codex_gate", "the declared gate must reach the gate step intact"
    assert "smart-router:" not in _plan_field(capsys.readouterr().out, "route_reason")


def test_silent_gate_filled_by_router_merges_its_reason(tmp_path, monkeypatch, capsys):
    """A gate-silent spec gets its gate from the router; the reason is merged
    into route_reason so the dry-run proves what was derived (never silent)."""
    data_dir, spec_file = _make_bundle_spec(tmp_path, gate="")
    _env(monkeypatch, data_dir)
    reason = "smart-router:governance_variant=fix gate=glm_gate"
    seen: dict = {}

    def _resolve(vspec):
        seen["gate_before"] = vspec.spec.gate
        return _gate_filling_resolver("glm_gate", reason)(vspec)

    with patch("dispatch_cli.build_runtime_snapshot") as mock_snapshot, \
         patch("dispatch_cli._resolve_gate_via_router", side_effect=_resolve) as mock_gate:
        mock_snapshot.return_value = _clean_snapshot()
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 0, capsys.readouterr().err
    assert mock_gate.call_count == 1
    assert seen["gate_before"] == "", "the door must hand the silent spec to the gate router"
    assert _plan_field(capsys.readouterr().out, "route_reason").startswith(reason + ";")


def test_gate_config_unreadable_is_refused_by_name(tmp_path, monkeypatch, capsys):
    """An unreadable VNX_DEFAULT_REVIEW_STACK rejects as gate-config-unreadable —
    never a hardcoded fallback gate."""
    data_dir, spec_file = _make_bundle_spec(tmp_path, gate="")
    _env(monkeypatch, data_dir)

    with patch(
        "dispatch_cli._resolve_gate_via_router",
        side_effect=ReviewGateConfigError("VNX_DEFAULT_REVIEW_STACK unreadable: not-a-gate"),
    ), patch("dispatch_cli._execute_claude_headless") as mock_exec:
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 1
    assert (
        "[dispatch_cli] REJECT [gate-config-unreadable]: "
        "VNX_DEFAULT_REVIEW_STACK unreadable: not-a-gate"
    ) in capsys.readouterr().err
    mock_exec.assert_not_called()


def test_writing_spec_without_gate_is_refused(tmp_path, monkeypatch, capsys):
    """A writing dispatch whose gate is still empty after derivation (fail-open)
    is refused as gate-required; a build role with no paths is still writing."""
    data_dir, spec_file = _make_bundle_spec(tmp_path, gate="", role="backend-developer")
    _env(monkeypatch, data_dir)

    with patch(
        "dispatch_cli._resolve_gate_via_router",
        side_effect=lambda vspec: (vspec, None),
    ), patch("dispatch_cli._execute_claude_headless") as mock_exec:
        rc = run_dispatch(spec_file, dry_run=True)

    assert rc == 1
    assert "[dispatch_cli] REJECT [gate-required]:" in capsys.readouterr().err
    mock_exec.assert_not_called()
