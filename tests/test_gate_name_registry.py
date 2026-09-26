"""One answer to "which gate names exist" — every reader agrees on it.

The property under test, stated once
--------------------------------------
A gate name is either legal EVERYWHERE a gate name is checked or legal nowhere.
Whoever declares, stages, validates, chains or routes a gate reads
``dispatch_spec.REGISTERED_GATE_NAMES``; none of them keeps a private copy.

What was broken (measured 2026-09-24 on head 2c47038e)
------------------------------------------------------
deepseek_gate is a registered, runnable gate: ``gate_recorder.GATE_PROVIDERS``
holds it, ``gate_request_handler`` requests it, ``vnx gate <pr> --only
deepseek_gate`` runs it, and the takeover chain ends on it. It was never a
member of the ``Gate`` enum, and four readers each rebuilt the legal set from
that bare enum:

  * ``smart_router._primary_review_gate``: a stack naming only deepseek_gate
    raised ReviewGateConfigError ("names no gate that exists"); a stack of
    ``deepseek_gate,claude_github_optional`` declared claude_github_optional
    (weight 1) while the executor requested deepseek_gate (weight 3).
  * ``dispatch_bridge._canonical_gate``: ``stage_spec_bundle(gate="deepseek_gate")``
    refused with "valid gates are: ci_gate, claude_github_optional, ...".
  * ``dispatch_spec.validate`` Rule 16: a staged spec naming deepseek_gate
    would have been refused again at fire time.
  * ``gate_request_handler``: the one reader that already added deepseek_gate,
    by hand, in its own private helper.

So the tests below are parameterized over the REAL registry, not over a list
copied into this file: a gate added tomorrow is covered on the next run.
"""
from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_bridge
import dispatch_cli
import dispatch_spec
import gate_recorder
import smart_router
from dispatch_spec import (
    GATES_OUTSIDE_ENUM,
    REGISTERED_GATE_NAMES,
    RETIRED_GATE_NAMES,
    DispatchPath,
    DispatchSpec,
    Gate,
    PathAccess,
    Reject,
    ReviewGateConfigError,
    ValidatedSpec,
    validate,
)
from gate_request_handler import (
    ReviewGateTakeoverConfigError,
    _parse_review_gate_takeover_chain,
)

_GOOD_ID = "20260924-120000-registry"
_ALL_GATES = sorted(REGISTERED_GATE_NAMES)


def _set_stack(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Point the project's review stack at *raw* through the override channel.

    The override channel (config_registry precedence 1) beats project_config, so
    the test measures the value it set and not the machine's real config.
    """
    monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", raw)
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")


def _spec(instruction_file: Path, *, gate: str) -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="20260924-registry-dispatch",
        staging_id="20260924-registry-staging",
        instruction_file=instruction_file,
        role="backend-developer",
        target_slot="T1",
        gate=gate,
        dispatch_paths=(
            DispatchPath(PurePosixPath("scripts/lib/foo.py"), PathAccess.WRITE),
        ),
    )


def _validate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str):
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Do the work.", encoding="utf-8")
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    return validate(
        _spec(instruction, gate=gate), project_id="vnx-dev", repo_root=Path("/fake/repo"),
    )


def _stage(tmp_path: Path, *, gate: str) -> Path:
    return dispatch_bridge.stage_spec_bundle(
        instruction_text="do the thing", dispatch_id=_GOOD_ID, role="dev",
        target_slot="T1", project_id="p1", provider="claude", data_dir=tmp_path,
        gate=gate,
    )


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------

def test_registry_is_the_enum_plus_the_gates_outside_it() -> None:
    assert REGISTERED_GATE_NAMES == {g.value for g in Gate} | GATES_OUTSIDE_ENUM


def test_a_gate_lives_in_exactly_one_place() -> None:
    """A gate that joins the Gate enum must leave GATES_OUTSIDE_ENUM.

    Listed in both, the two would agree today and could drift apart the day one
    of them is edited; the overlap is the hand-copy this registry replaced.
    """
    overlap = GATES_OUTSIDE_ENUM & {g.value for g in Gate}
    assert not overlap, f"gates in both the Gate enum and GATES_OUTSIDE_ENUM: {sorted(overlap)}"


def test_registry_is_exactly_the_set_of_gates_that_can_run() -> None:
    """Legal name <=> registered provider route.

    ``GATE_PROVIDERS`` is what ``vnx gate --only <gate>`` actually resolves. A
    name legal in the registry but absent there is a gate that validates and
    then never runs; a runnable gate absent from the registry is the
    deepseek_gate defect itself.
    """
    assert REGISTERED_GATE_NAMES == set(gate_recorder.GATE_PROVIDERS) - RETIRED_GATE_NAMES


def test_every_weighted_gate_is_a_registered_gate() -> None:
    """The heaviness ladder may not weigh a name the legal set refuses.

    The PR that put deepseek_gate on rung 3 of ``_GATE_WEIGHT`` did so while
    ``_primary_review_gate`` refused the same name.
    """
    unregistered = set(smart_router._GATE_WEIGHT) - REGISTERED_GATE_NAMES
    assert not unregistered, f"_GATE_WEIGHT weighs unregistered gates: {sorted(unregistered)}"


def test_the_config_error_is_one_class_for_the_door_and_the_router() -> None:
    """The door catches the class smart_router raises: they must be identical."""
    assert smart_router.ReviewGateConfigError is dispatch_spec.ReviewGateConfigError
    assert dispatch_cli.ReviewGateConfigError is dispatch_spec.ReviewGateConfigError


# ---------------------------------------------------------------------------
# Every reader agrees, for every registered gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("gate", _ALL_GATES)
def test_staging_bridge_accepts_every_registered_gate(tmp_path: Path, gate: str) -> None:
    import json

    payload = json.loads(_stage(tmp_path, gate=gate).read_text(encoding="utf-8"))
    assert payload["gate"] == gate


@pytest.mark.parametrize("gate", _ALL_GATES)
def test_door_validation_accepts_every_registered_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str,
) -> None:
    """A spec the bridge staged is a spec the door accepts at fire time."""
    result = _validate(tmp_path, monkeypatch, gate)
    assert isinstance(result, ValidatedSpec), f"door refused registered gate {gate!r}: {result}"


@pytest.mark.parametrize("gate", _ALL_GATES)
def test_takeover_chain_accepts_every_registered_gate(gate: str) -> None:
    chain = _parse_review_gate_takeover_chain(f"codex_gate,{gate}" if gate != "codex_gate" else gate)
    if gate != "codex_gate":
        assert chain == {"codex_gate": gate}


@pytest.mark.parametrize("gate", _ALL_GATES)
def test_review_stack_router_accepts_every_registered_gate(
    monkeypatch: pytest.MonkeyPatch, gate: str,
) -> None:
    _set_stack(monkeypatch, gate)
    assert smart_router._primary_review_gate() == gate


# ---------------------------------------------------------------------------
# deepseek_gate, named: the three cases of the blocking finding
# ---------------------------------------------------------------------------

def test_stage_spec_bundle_accepts_deepseek_gate(tmp_path: Path) -> None:
    import json

    payload = json.loads(_stage(tmp_path, gate="deepseek_gate").read_text(encoding="utf-8"))
    assert payload["gate"] == "deepseek_gate"


def test_canonical_gate_lists_deepseek_gate_among_the_valid_names() -> None:
    with pytest.raises(ValueError) as excinfo:
        dispatch_bridge._canonical_gate("deepseek")
    assert "deepseek_gate" in str(excinfo.value)


def test_stack_of_deepseek_gate_alone_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_stack(monkeypatch, "deepseek_gate")
    assert smart_router._primary_review_gate() == "deepseek_gate"


@pytest.mark.parametrize(
    "stack", ["deepseek_gate,claude_github_optional", "claude_github_optional,deepseek_gate"],
)
def test_deepseek_gate_outranks_a_lighter_seat(monkeypatch: pytest.MonkeyPatch, stack: str) -> None:
    _set_stack(monkeypatch, stack)
    assert smart_router._primary_review_gate() == "deepseek_gate"


# ---------------------------------------------------------------------------
# A name that really does not exist is still refused, by every reader
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["deepseek", "codex", "codex_gat", "deepseek-gate", "Deepseek_Gate"])
def test_staging_bridge_still_refuses_an_unknown_name(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="not a recognized gate name"):
        _stage(tmp_path, gate=bad)


@pytest.mark.parametrize("bad", ["deepseek", "codex_gat", "totally_unknown_gate"])
def test_door_validation_still_refuses_an_unknown_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str,
) -> None:
    result = _validate(tmp_path, monkeypatch, bad)
    assert isinstance(result, Reject)
    assert result.code == "bad-gate"
    assert "deepseek_gate" in result.reason, "the refusal must list the registered names"


def test_takeover_chain_still_refuses_an_unknown_name() -> None:
    with pytest.raises(ReviewGateTakeoverConfigError, match="unknown gate 'deepseek'"):
        _parse_review_gate_takeover_chain("codex_gate,deepseek")


def test_review_stack_of_only_unknown_names_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_stack(monkeypatch, "deepseek,codex_gat")
    with pytest.raises(ReviewGateConfigError) as excinfo:
        smart_router._primary_review_gate()
    assert "deepseek_gate" in str(excinfo.value), "the refusal must list the registered names"


def test_legacy_lifecycle_phase_names_are_still_the_no_gate_sentinel(tmp_path: Path) -> None:
    """The registry must not turn "planning"/"implementation" into gates."""
    assert dispatch_bridge._canonical_gate("planning") == ""
    assert dispatch_bridge._canonical_gate("implementation") == ""
    assert not {"planning", "implementation"} & REGISTERED_GATE_NAMES


# ---------------------------------------------------------------------------
# The door stays fail-open when smart_router cannot be imported
# ---------------------------------------------------------------------------

def _door_spec(tmp_path: Path) -> ValidatedSpec:
    instruction = tmp_path / "instruction.md"
    instruction.write_text("Do the work.", encoding="utf-8")
    return ValidatedSpec(
        spec=_spec(instruction, gate=""),
        instruction_text="Do the work.",
        normalized_paths=(),
        instruction_sha256="0" * 64,
    )


def test_gate_resolution_is_fail_open_when_smart_router_cannot_be_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An import failure leaves the gate empty; it never aborts the dispatch."""
    monkeypatch.setitem(sys.modules, "smart_router", None)  # `import smart_router` -> ImportError
    vspec = _door_spec(tmp_path)

    resolved, reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert resolved is vspec
    assert reason is None


def _run_door_without_smart_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, gate: str,
) -> int:
    """Run the real door path in dry-run with smart_router unimportable."""
    import json

    from dispatch_plan import ModelPin, RuntimeSnapshot

    instruction = tmp_path / "instruction.md"
    instruction.write_text("# Test Dispatch\n\nDo something useful.\n", encoding="utf-8")
    spec_file = tmp_path / "dispatch-spec.json"
    spec_file.write_text(
        json.dumps({
            "schema_version": 1,
            "project_id": "vnx-dev",
            "dispatch_id": "20260924-registry-failopen",
            "staging_id": "test-stage",
            "instruction_file": str(instruction),
            "role": "backend-developer",
            "target_slot": "T1",
            "gate": gate,
            "dispatch_paths": [
                {"path": "scripts/test.py", "access": "read_write", "materialize_at_cwd": False},
            ],
            "provider": "claude",
            "model": None,
            "deadline_seconds": 3600,
            "base_ref": "origin/main",
            "isolation": "worktree",
            "requires_mcp": False,
        }),
        encoding="utf-8",
    )
    snapshot = RuntimeSnapshot(
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
    monkeypatch.setitem(sys.modules, "smart_router", None)

    with patch("dispatch_cli.build_runtime_snapshot", return_value=snapshot), \
         patch("dispatch_cli.run_envelope_plan"), \
         patch("dispatch_cli._execute_claude_headless"):
        return dispatch_cli.run_dispatch(spec_file, dry_run=True)


def test_run_dispatch_with_its_own_gate_does_not_need_smart_router(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A spec that names its own gate never consults the router, so a router
    that will not import must not stop it.

    ``from smart_router import ReviewGateConfigError`` sat unguarded in
    run_dispatch, ahead of the guarded import in ``_resolve_gate_via_router``:
    an unimportable smart_router raised out of the door for EVERY dispatch,
    including the ones that never needed it.
    """
    rc = _run_door_without_smart_router(tmp_path, monkeypatch, gate="codex_gate")

    assert rc == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_run_dispatch_without_a_gate_is_refused_by_name_when_smart_router_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The router import failure is fail-open at the router and closed at the
    door: the gate stays empty and the door refuses BY NAME (gate-required),
    instead of an ImportError escaping run_dispatch as a traceback."""
    rc = _run_door_without_smart_router(tmp_path, monkeypatch, gate="")

    assert rc == 1
    assert "gate-required" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The variant is derivable without reading the review-stack configuration
# ---------------------------------------------------------------------------

DERIVATION_INPUTS = [
    {},
    {"dispatch_paths": ["docs/foo.md"]},
    {"dispatch_paths": ["scripts/lib/dispatch_cli.py"]},
    {"dispatch_paths": ["schemas/migrations/0034_x.sql"]},
    {"task_class": "01_code_generation"},
    {"task_class": "04_documentation"},
    {"dispatch_paths": ["docs/foo.md"], "irreversible": True},
]


@pytest.mark.parametrize("args", DERIVATION_INPUTS, ids=lambda a: ",".join(sorted(a)) or "no-signals")
def test_variant_only_derivation_agrees_with_the_full_derivation(
    monkeypatch: pytest.MonkeyPatch, args: dict,
) -> None:
    """Splitting the derivation must not fork the rule."""
    _set_stack(monkeypatch, "glm_gate")
    paths = args.get("dispatch_paths")
    kwargs = {k: v for k, v in args.items() if k != "dispatch_paths"}

    variant_only = smart_router.derive_variant(paths, **kwargs)
    full = smart_router.derive_governance_variant(paths, **kwargs)

    assert variant_only.variant == full.variant
    assert variant_only.reason == full.reason
    assert variant_only.is_new_feature == full.is_new_feature


@pytest.mark.parametrize("stack", ["", "mijn_eigen_poort"])
def test_variant_only_derivation_ignores_an_unreadable_review_stack(
    monkeypatch: pytest.MonkeyPatch, stack: str,
) -> None:
    """A caller that sizes something from the variant alone is not broken by a
    review-gate setting it never asked about; the gate-carrying derivation still
    refuses, which is the door's contract."""
    _set_stack(monkeypatch, stack)

    derived = smart_router.derive_variant(["scripts/lib/dispatch_cli.py"])
    assert derived.variant == "coding-strict"

    with pytest.raises(ReviewGateConfigError):
        smart_router.derive_governance_variant(["scripts/lib/dispatch_cli.py"])


def test_plan_gate_panel_weight_is_derivable_with_an_unreadable_review_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """planning_cli._derive_panel_weight needs the variant and nothing else."""
    import planning_cli

    _set_stack(monkeypatch, "")

    weight = planning_cli._derive_panel_weight(
        panel_seats=None,
        dispatch_paths="scripts/lib/dispatch_cli.py",
        task_class=None,
        irreversible=False,
    )

    assert weight["variant"] == "coding-strict"
    assert weight["seats"] >= 1
