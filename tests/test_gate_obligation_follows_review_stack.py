"""De gedeclareerde review-gate-verplichting en de ingestelde review-stack
mogen elkaar niet tegenspreken. Dat is de eigenschap die dit bestand vastlegt —
geen gate-naam, geen momentopname.

Aanleiding, gemeten 2026-09-19 in mission-control: de projectconfig droeg
``VNX_DEFAULT_REVIEW_STACK=glm_gate,claude_github_optional`` terwijl ELKE
verplichting die de dispatch-deur aanmaakte ``codex_gate`` noemde (route_reason
in ``state/route_decisions/D-da07e641.json``: ``smart-router:governance_variant=
default gate=codex_gate``). Vijf PR's stonden daardoor uren stil op een codex-
quotum dat op was, achter een poort die de operator niet had gevraagd — terwijl
de glm-poort die het project WEL had ingesteld op alle vijf al had goedgekeurd.

Waarom de verwachte gate hier nooit als literal staat: de operator kiest de
stack, dus de test leest zijn verwachting uit de configuratie zelf. Morgen
``kimi_gate`` kiezen houdt deze suite groen zonder dat er één regel verandert;
hem hier vastpinnen zou precies de fout zijn die dit bestand bewaakt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from dispatch_cli import _register_gate_obligation, _resolve_gate_via_router
from dispatch_spec import DispatchPath, DispatchSpec, PathAccess, ValidatedSpec
from smart_router import (
    DEFAULT_REVIEW_STACK_KEY,
    ReviewGateConfigError,
    _GATE_WEIGHT,
    _primary_review_gate,
)

# Stacks the tests drive the config with. None of these is "the" right answer:
# each is an operator choice the code must follow, including the ones whose
# heaviest member is not the conventional codex_gate.
OPERATOR_CHOICES = [
    "glm_gate,claude_github_optional",
    "kimi_gate,glm_gate",
    "claude_github_optional,codex_gate",
    "codex_gate",
    "glm_gate",
]


def _set_stack(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Point this project's review stack at *raw*, deterministically.

    Uses the ``VNX_OVERRIDE_<BARE>`` channel (config_registry precedence 1)
    rather than the plain env var: a runtime that has its DB resolver wired
    resolves ``project_config`` ABOVE a plain env var, so a test that set only
    ``VNX_DEFAULT_REVIEW_STACK`` would silently measure the machine's real
    project config instead of the value it asked for. VNX_CI_GATE_REQUIRED is
    pinned off because it appends ci_gate and would otherwise widen the stack
    the assertions compare against.
    """
    monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", raw)
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")


def _clear_operator_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the stack to whatever this project resolves on its own."""
    monkeypatch.delenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", raising=False)
    monkeypatch.delenv("VNX_DEFAULT_REVIEW_STACK", raising=False)
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")


def _spec(dispatch_id: str, *, paths: tuple[DispatchPath, ...] = (), gate: str = "") -> DispatchSpec:
    return DispatchSpec(
        schema_version=1,
        project_id="mission-control",
        dispatch_id=dispatch_id,
        staging_id=dispatch_id,
        instruction_file=Path("/tmp") / f"{dispatch_id}-instruction.md",
        role="quality-engineer",
        target_slot="T1",
        gate=gate,
        dispatch_paths=paths,
    )


def _validated(spec: DispatchSpec) -> ValidatedSpec:
    return ValidatedSpec(
        spec=spec,
        instruction_text="werk",
        normalized_paths=spec.dispatch_paths,
        instruction_sha256="0" * 64,
    )


def _declare(
    state_dir: Path,
    dispatch_id: str,
    *,
    paths: tuple[DispatchPath, ...] = (),
) -> dict:
    """Run the door's own two steps for a silent spec and return the record.

    Step 1 is the router filling the empty gate; step 2 is the door writing the
    obligation. Both are the production functions, so this measures the real
    path rather than a re-implementation of it.
    """
    vspec, _reason = _resolve_gate_via_router(_validated(_spec(dispatch_id, paths=paths)))
    assert _register_gate_obligation(vspec.spec, state_dir=state_dir) is None, (
        "registering the obligation must succeed for a writing dispatch"
    )
    record_path = state_dir / "review_gates" / "obligations" / f"{dispatch_id}.json"
    assert record_path.exists(), f"no obligation declared at {record_path}"
    return json.loads(record_path.read_text(encoding="utf-8"))


def _stack_names(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@pytest.mark.parametrize("stack", OPERATOR_CHOICES)
def test_declared_obligation_names_a_gate_from_the_configured_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: str,
) -> None:
    """The obligation names a gate the operator actually configured."""
    _set_stack(monkeypatch, stack)
    record = _declare(tmp_path, "D-stackchoice01")

    assert record["gate"] in _stack_names(stack), (
        f"obligation declares {record['gate']!r}, which is not in the configured "
        f"stack {stack!r} — configuration and obligation contradict each other"
    )
    assert record["status"] == "pending", "a fresh obligation starts pending"


@pytest.mark.parametrize("stack", OPERATOR_CHOICES)
def test_declared_obligation_picks_the_heaviest_seat_in_the_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: str,
) -> None:
    """Not just any member: the heaviest one the stack offers.

    A stack that offers a full diff review and an optional review must not
    resolve to the optional one — that would drop the review the operator
    asked for while still looking like it followed the config.
    """
    _set_stack(monkeypatch, stack)
    record = _declare(tmp_path, "D-stackchoice02")

    declared = record["gate"]
    heaviest = max(_GATE_WEIGHT.get(name, 0) for name in _stack_names(stack))
    assert _GATE_WEIGHT.get(declared, 0) == heaviest, (
        f"obligation declares {declared!r} (weight "
        f"{_GATE_WEIGHT.get(declared, 0)}) while stack {stack!r} offers a seat of "
        f"weight {heaviest}"
    )


@pytest.mark.parametrize(
    "paths",
    [
        (),
        (DispatchPath(path=PurePosixPath("scripts/lib/dispatch_cli.py"), access=PathAccess.READ_WRITE),),
    ],
    ids=["no-paths-default-variant", "governance-core-coding-strict"],
)
def test_both_heavy_variants_follow_the_configured_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, paths: tuple[DispatchPath, ...],
) -> None:
    """`default` AND `coding-strict` both mean "full diff review", so both follow
    the operator's choice. Pinning only one of them would leave the other
    hardcoded and reintroduce the 2026-09-19 mismatch for that half."""
    stack = "glm_gate,claude_github_optional"
    _set_stack(monkeypatch, stack)
    record = _declare(tmp_path, "D-stackchoice03", paths=paths)

    assert record["gate"] == "glm_gate"


def test_obligation_gate_is_a_member_of_the_stack_the_executor_will_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The strongest form: the declared gate is one the executor will request.

    The obligation and the executed seats are two ends of one configuration. If
    the gate the door declares is not in the stack the executor actually fires,
    the obligation can only ever be fulfilled by a takeover — which is how this
    reached production (D-894b16a1 was booked ``fulfilled_by_takeover_evidence``
    from glm_gate against a codex_gate obligation).
    """
    stack = "glm_gate,claude_github_optional"
    _set_stack(monkeypatch, stack)

    import review_gate_manager as rgm

    executor_stack = rgm._build_default_review_stack()
    record = _declare(tmp_path, "D-stackchoice04")

    assert record["gate"] in executor_stack, (
        f"obligation declares {record['gate']!r} but the executor will request "
        f"{executor_stack!r} — the two ends of one config disagree"
    )


def test_project_without_an_operator_choice_still_follows_its_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No operator override: whatever this project resolves IS the authority.

    Deliberately does NOT assert a literal gate. On CI that resolution is the
    registry default; on an operator machine it may be a project_config value.
    Both are legitimate, and the property — the obligation stays inside the
    stack — holds for either.
    """
    _clear_operator_choice(monkeypatch)

    import config_runtime

    resolved = _stack_names(str(config_runtime.get(DEFAULT_REVIEW_STACK_KEY) or ""))
    assert resolved, "this project must resolve a non-empty review stack"

    record = _declare(tmp_path, "D-stackchoice05")
    assert record["gate"] in resolved


@pytest.mark.parametrize("stack", OPERATOR_CHOICES)
def test_every_configured_stack_still_gets_an_obligation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stack: str,
) -> None:
    """Following the config may never degrade into declaring nothing.

    A dispatch without any obligation is not an acceptable outcome: the review
    requirement stays, only WHICH gate changes with the configuration.
    """
    _set_stack(monkeypatch, stack)
    record = _declare(tmp_path, "D-stackchoice06")

    assert record["gate"], "an obligation must name a gate"
    assert record["kind"] == "review_gate_obligation"


# ---------------------------------------------------------------------------
# Unreadable input is amber, never green
# ---------------------------------------------------------------------------

def test_empty_stack_refuses_and_names_the_config_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An empty stack fails loudly instead of falling back to a hardcoded gate.

    The silent fallback is the defect: it answered a project routed to glm_gate
    with codex_gate for a whole day without a single error.
    """
    _set_stack(monkeypatch, "")

    with pytest.raises(ReviewGateConfigError) as excinfo:
        _resolve_gate_via_router(_validated(_spec("D-stackchoice07")))

    assert DEFAULT_REVIEW_STACK_KEY in str(excinfo.value), (
        "the refusal must name the config key the operator has to fix"
    )
    obligations = tmp_path / "review_gates" / "obligations"
    assert not obligations.exists(), "a refused dispatch must not leave an obligation"


def test_stack_naming_only_unknown_gates_refuses_and_names_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A private label is not a gate; refuse it by name rather than guess."""
    _set_stack(monkeypatch, "mijn_eigen_poort")

    with pytest.raises(ReviewGateConfigError) as excinfo:
        _resolve_gate_via_router(_validated(_spec("D-stackchoice08")))

    message = str(excinfo.value)
    assert "mijn_eigen_poort" in message, "the refusal must name the offending value"
    assert DEFAULT_REVIEW_STACK_KEY in message
    assert not (tmp_path / "review_gates" / "obligations").exists()


def test_an_unknown_name_beside_a_real_gate_does_not_hide_the_real_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A typed-in label next to a real gate resolves to the real gate.

    Refusing the whole dispatch here would be the wrong kind of strict: the
    operator's instruction is still legible, and the real gate is what reviews.
    """
    _set_stack(monkeypatch, "glm_gate,typo_gate")
    record = _declare(tmp_path, "D-stackchoice09")

    assert record["gate"] == "glm_gate"


def test_the_router_and_the_gate_executor_read_the_same_config_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift guard: ONE key decides both what is declared and what is requested.

    Two keys (or a literal on one side) is how the two ends came apart. This
    asserts the key identity AND that moving the key moves both sides.
    """
    assert DEFAULT_REVIEW_STACK_KEY == "VNX_DEFAULT_REVIEW_STACK"

    import review_gate_manager as rgm

    _set_stack(monkeypatch, "glm_gate")
    declared_seat = _primary_review_gate()
    executor_stack = [g for g in rgm._build_default_review_stack() if g != "ci_gate"]

    assert declared_seat == "glm_gate"
    assert executor_stack == ["glm_gate"]
