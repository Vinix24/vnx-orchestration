"""tests/test_irreversible_flag_transport.py — OI-1274: `irreversible` is dead
on the dispatch door's own path.

``DispatchSpec.irreversible`` (dispatch_spec.py) exists and ``smart_router``
honors it (``derive_governance_variant``/``resolve_gate`` already force
coding-strict on ``irreversible=True`` — see test_smart_router_governance_
variant.py::TestIrreversibility, which is NOT touched here because the router
itself is not the defect).

What IS the defect: nothing between an operator declaring the flag and the
router actually seeing it carries the value, on the DOOR path specifically
(the planning/panel side already wires it — planning_cli.py --irreversible,
vnx_cli/main.py --irreversible — this file is scoped to the door, not those):

  1. staging  — dispatch_bridge.stage_spec_bundle has no `irreversible` param
     at all, so the flag can't even be staged into dispatch-spec.json.
  2. loading  — dispatch_cli.load_spec never reads raw.get("irreversible"),
     so a spec file WITH the flag set still loads with the dataclass default
     (False).
  3. forwarding — dispatch_cli._resolve_gate_via_router calls
     smart_router.resolve_gate(...) without irreversible=, so even a
     correctly-loaded spec.irreversible=True never reaches the router.

Each leg is tested independently, at its own layer, on ORIGIN (read files
back off disk / capture actual call kwargs) rather than asserting on values
this same test constructed and fed straight to the router — that would test
the router, which already works.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_bridge  # noqa: E402
import dispatch_cli  # noqa: E402
import smart_router  # noqa: E402
from dispatch_cli import load_spec  # noqa: E402
from dispatch_spec import Reject, validate  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

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
    dispatch_id: str = "20260817-oi1274-test",
    staging_id: str = "test-stage",
    gate: str = "",
    dispatch_paths: "list[dict] | None" = None,
    extra: "dict | None" = None,
) -> Path:
    """Write a minimal dispatch-spec.json and return its path.

    Mirrors tests/test_dispatch_cli.py::_make_spec_file (same shape, same
    default role/provider/target_slot) so a spec built here validates cleanly
    through the real dispatch_spec.validate() the door uses.
    """
    instruction_file = _make_instruction(tmp_path)
    spec: dict = {
        "schema_version": 1,
        "project_id": "vnx-dev",
        "dispatch_id": dispatch_id,
        "staging_id": staging_id,
        "instruction_file": str(instruction_file),
        "role": "backend-developer",
        "target_slot": "T1",
        "gate": gate,
        "dispatch_paths": (
            dispatch_paths
            if dispatch_paths is not None
            else [{"path": "scripts/test.py", "access": "read_write", "materialize_at_cwd": False}]
        ),
        "provider": "claude",
        "model": None,
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


def _validated_spec(tmp_path: Path, **spec_kwargs):
    spec_file = _make_spec_file(tmp_path, **spec_kwargs)
    spec = load_spec(spec_file)
    vspec = validate(spec, project_id="vnx-dev", repo_root=_REPO_ROOT)
    assert not isinstance(vspec, Reject), f"fixture spec failed to validate: {vspec}"
    return vspec


# ---------------------------------------------------------------------------
# Leg 1 — staging: stage_spec_bundle(..., irreversible=True) must land in the
# written dispatch-spec.json.
# ---------------------------------------------------------------------------

def test_stage_spec_bundle_writes_irreversible_true(tmp_path):
    """dispatch_bridge.stage_spec_bundle has NO `irreversible` parameter today
    (OI-1274) — the flag cannot even be staged. Read the bundle back off disk
    and assert on its content, not on any in-memory value."""
    spec_path = dispatch_bridge.stage_spec_bundle(
        instruction_text="delete the legacy shim (irreversible)",
        dispatch_id="20260817-oi1274-stage-true",
        role="dev",
        target_slot="T1",
        project_id="p1",
        provider="claude",
        data_dir=tmp_path,
        irreversible=True,
    )
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    assert payload["irreversible"] is True


def test_stage_spec_bundle_defaults_irreversible_false(tmp_path):
    """An ordinary dispatch that never mentions the flag must stage as an
    explicit False, not merely absent — pins the default once staging honors
    the field, so a caller can rely on the key always being present."""
    spec_path = dispatch_bridge.stage_spec_bundle(
        instruction_text="ordinary reversible change",
        dispatch_id="20260817-oi1274-stage-default",
        role="dev",
        target_slot="T1",
        project_id="p1",
        provider="claude",
        data_dir=tmp_path,
    )
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    assert payload.get("irreversible") is False


# ---------------------------------------------------------------------------
# Leg 2 — loading: load_spec must read "irreversible" off disk into the spec.
# ---------------------------------------------------------------------------

def test_load_spec_parses_irreversible_true(tmp_path):
    """load_spec never reads raw.get("irreversible") (OI-1274) — the spec
    always carries the dataclass default False regardless of what is on disk.
    The spec file is written directly here (independent of stage_spec_bundle,
    which does not even write the key yet), so this pins the loading leg on
    its own."""
    spec_file = _make_spec_file(tmp_path, extra={"irreversible": True})
    spec = load_spec(spec_file)
    assert spec.irreversible is True


def test_load_spec_irreversible_absent_defaults_false(tmp_path):
    """Vangnet: an ordinary spec with no irreversible key at all must still
    load as False. Already true on main; must stay true after the fix."""
    spec_file = _make_spec_file(tmp_path)
    spec = load_spec(spec_file)
    assert spec.irreversible is False


# ---------------------------------------------------------------------------
# Leg 3 — forwarding: the door path (_resolve_gate_via_router) must pass
# spec.irreversible through to smart_router.resolve_gate.
# ---------------------------------------------------------------------------

def test_door_path_forwards_irreversible_true_to_resolve_gate(tmp_path, monkeypatch):
    """_resolve_gate_via_router calls resolve_gate(...) WITHOUT irreversible=
    (OI-1274), even though resolve_gate accepts it and the spec carries it.

    A ValidatedSpec with irreversible=True is built directly on the dataclass
    (dataclasses.replace) rather than through load_spec — that isolates this
    leg from the Leg-2 defect above, so a fix to loading alone can't
    accidentally make this test pass without the forwarding fix too.

    The real smart_router.resolve_gate is spied on (call-through), never
    replaced with a canned return: this exercises the actual router so the
    assertion on the resulting governance variant is genuine, not asserted
    against a value this test constructed. Never call resolve_gate directly
    with irreversible=True and assert on it — that tests the router, which
    already works (see test_smart_router_governance_variant.py).

    A docs/ path is used on purpose: docs alone derives 'minimal' -> ci_gate,
    so a resulting codex_gate can only be explained by irreversible=True
    actually reaching the router — not by the path category.
    """
    vspec = _validated_spec(
        tmp_path,
        dispatch_id="20260817-oi1274-forward-true",
        gate="",
        dispatch_paths=[{"path": "docs/README.md", "access": "read_write", "materialize_at_cwd": False}],
    )
    irreversible_spec = dataclasses.replace(vspec.spec, irreversible=True)
    vspec = dataclasses.replace(vspec, spec=irreversible_spec)

    real_resolve_gate = smart_router.resolve_gate
    captured: dict = {}

    def spy_resolve_gate(*args, **kwargs):
        captured.update(kwargs)
        return real_resolve_gate(*args, **kwargs)

    monkeypatch.setattr(smart_router, "resolve_gate", spy_resolve_gate)

    new_vspec, gate_reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert captured.get("irreversible") is True, (
        f"resolve_gate must receive irreversible=True from the door path; got kwargs={captured}"
    )
    assert new_vspec.spec.gate == "codex_gate", (
        "irreversible=True must force the strictest gate (codex_gate) over the "
        f"docs-derived minimal/ci_gate; got gate={new_vspec.spec.gate!r}"
    )
    assert gate_reason is not None and "irreversible=true" in gate_reason, (
        f"gate_reason must name the irreversible override in the trace; got {gate_reason!r}"
    )


def test_door_path_forwards_irreversible_false_by_default(tmp_path, monkeypatch):
    """Companion negative case: a spec that never declares the flag (loaded
    normally, no dataclasses.replace) must forward irreversible=False — the
    door path must not accidentally force strict on every dispatch once the
    forwarding leg is fixed."""
    vspec = _validated_spec(
        tmp_path,
        dispatch_id="20260817-oi1274-forward-false",
        gate="",
        dispatch_paths=[{"path": "docs/README.md", "access": "read_write", "materialize_at_cwd": False}],
    )
    assert vspec.spec.irreversible is False

    real_resolve_gate = smart_router.resolve_gate
    captured: dict = {}

    def spy_resolve_gate(*args, **kwargs):
        captured.update(kwargs)
        return real_resolve_gate(*args, **kwargs)

    monkeypatch.setattr(smart_router, "resolve_gate", spy_resolve_gate)

    new_vspec, _gate_reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert captured.get("irreversible") is False
    assert new_vspec.spec.gate == "ci_gate"


def test_door_path_forces_strict_for_irreversible_path_without_flag(tmp_path):
    """Vangnet (must stay green): a path already under an irreversible prefix
    forces coding-strict through the door's OWN resolve_gate call, with no
    flag needed at all — dispatch_paths already reach resolve_gate correctly
    today (OI-1274 is scoped to the missing `irreversible` kwarg only).

    Pinned at the _resolve_gate_via_router layer (not just the router layer,
    already covered by test_smart_router_governance_variant.py) so a fix that
    adds irreversible-forwarding cannot regress the already-working
    path-derived branch through the door.
    """
    vspec = _validated_spec(
        tmp_path,
        dispatch_id="20260817-oi1274-path-derived",
        gate="",
        dispatch_paths=[{"path": "scripts/migrations/0099_test.sql", "access": "read_write", "materialize_at_cwd": False}],
    )
    assert vspec.spec.irreversible is False

    new_vspec, gate_reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert new_vspec.spec.gate == "codex_gate"
    assert gate_reason is not None and "irreversible" in gate_reason
