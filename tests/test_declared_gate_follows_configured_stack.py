"""test_declared_gate_follows_configured_stack.py — the declared gate is the
operator's configured gate, not a name baked into the router.

Measured 2026-09-19 in mission-control. The project configuration said
``VNX_DEFAULT_REVIEW_STACK = "glm_gate,claude_github_optional"``, yet EVERY
review-gate obligation the door registered declared ``codex_gate``
(D-da07e641/PR #1149, D-894b16a1/PR #1153, D-4e453252/PR #1150, and older —
all ``status=pending``). codex sat on its quota most of that day, so five PRs
stalled for hours on an obligation that, per the configuration, should never
have been codex — while the glm gate had already passed all five.

Root cause: the spec is silent about its gate, so
``dispatch_cli._resolve_gate_via_router`` asked ``smart_router.resolve_gate``
for one, and that function answers with
``GOVERNANCE_VARIANT_GATE[derived_variant]`` — a table hardcoded in
``scripts/lib/smart_router.py`` whose "default" and "coding-strict" entries
both read ``codex_gate``. So the mechanism that decided the declared name
consulted the code, while the configuration the operator edits was read only by
``review_gate_manager``, as the stack for the gate REQUESTS. The obligation
named codex_gate and the runner requested glm_gate.

Measured on the records, not inferred. ``project_config_audit`` shows the head
of ``VNX_DEFAULT_REVIEW_STACK`` was ``glm_gate`` continuously from
2026-09-12T07:43:18Z (``glm_gate,kimi_gate,claude_github_optional``), narrowed
to ``glm_gate,claude_github_optional`` on 09-14, and it never named
``codex_gate``. All 31 obligations declared on 2026-09-19 carry
``gate=codex_gate`` — including ``D-f2429d7d``, the dispatch that produced this
fix.

The two producers are visibly different on disk, and that is the comparison
that pins it: the ``D-<slug>`` lane's specs carry ``"gate": "glm_gate"`` written
in (``D-agenda-fix-103542``, ``D-authority-083137``), so they agreed with the
configuration. The ``D-<hex>`` lane's specs carry ``"gate": ""`` (``D-da07e641``,
``D-894b16a1``), leaving the door to supply the name — and it supplied the
hardcoded one. Nothing was wrong with the configuration; a silent spec was.

The property pinned here is agreement itself, never a particular gate name.
Every case derives its expected value FROM the configuration it sets, so the
module keeps passing when the operator picks a different gate tomorrow. There
is deliberately no test that asserts ``gate == "codex_gate"`` or any other
literal: a gate name is an operator decision, and pinning one would recreate
the defect in the test suite.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import config_registry  # noqa: E402
import config_runtime  # noqa: E402
import dispatch_cli  # noqa: E402
import smart_router  # noqa: E402
from dispatch_cli import run_dispatch  # noqa: E402
from dispatch_spec import (  # noqa: E402
    DispatchPath,
    DispatchSpec,
    PathAccess,
    Provider,
    ValidatedSpec,
)

# Cross-test import, the pattern tests/test_oi1452_canonical_failure_reason.py
# already uses: the door-level case below must run through the SAME staged
# bundle and state-dir fixture the gate-obligation suite uses, so the two
# cannot drift into describing different doors.
from test_gate_obligations import _make_bundle, _make_state_dir, _read_obligation  # noqa: E402

# Stacks the operator could plausibly configure. None of these is "the" answer:
# each case reads the gate it expects out of the stack it just set.
_CONFIGURED_STACKS = [
    "glm_gate,claude_github_optional",
    "kimi_gate",
    "deepseek_gate,glm_gate",
    "codex_gate,glm_gate",
    "gemini_review,codex_gate,claude_github_optional",
]


def _isolate_config(monkeypatch) -> None:
    """Pin the config read to the plain-env layer for this test.

    ``config_registry.get`` resolves in four steps: ``VNX_OVERRIDE_*`` env, then
    the per-project ``project_config`` DB row, then the plain env, then the
    registry default. Step 2 beats step 3, so on a machine whose
    ``~/.vnx-data`` carries a row for this project a plain ``VNX_DEFAULT_REVIEW_STACK``
    would be silently outranked and a stack-parametrized test would assert
    against a value it never set.

    Neutralising the DB layer keeps every test below deterministic without
    stubbing the function under test: ``config_registry.get`` itself still runs,
    with the same precedence, one layer shorter.
    """
    monkeypatch.setattr(config_runtime, "autowire", lambda *a, **k: False)
    monkeypatch.setattr(config_registry, "_db_resolver", None)


def _validated_spec(
    tmp_path: Path,
    *,
    dispatch_id: str,
    gate: str = "",
    paths: tuple[DispatchPath, ...] = (),
) -> ValidatedSpec:
    instruction = tmp_path / f"{dispatch_id}-instruction.md"
    instruction.write_text("# Test\n\nDo the thing.\n", encoding="utf-8")
    spec = DispatchSpec(
        schema_version=1,
        project_id="mission-control",
        dispatch_id=dispatch_id,
        staging_id=dispatch_id,
        instruction_file=instruction,
        role="backend-developer",
        target_slot="T1",
        gate=gate,
        dispatch_paths=paths,
        provider=Provider.CLAUDE,
        model="sonnet",
    )
    return ValidatedSpec(
        spec=spec,
        instruction_text=instruction.read_text(encoding="utf-8"),
        normalized_paths=paths,
        instruction_sha256="0" * 64,
    )


def _writing_path() -> DispatchPath:
    return DispatchPath(
        path=PurePosixPath("src/module.py"),
        access=PathAccess.READ_WRITE,
        materialize_at_cwd=False,
    )


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    (state_dir / "review_gates" / "obligations").mkdir(parents=True, exist_ok=True)
    return state_dir


def _obligation_record(state_dir: Path, dispatch_id: str) -> dict:
    path = state_dir / "review_gates" / "obligations" / f"{dispatch_id}.json"
    assert path.exists(), f"the door must leave an obligation record at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _register(vspec: ValidatedSpec, state_dir: Path) -> dict:
    """Register the obligation exactly as the door does and read it back off disk."""
    dispatch_cli._register_gate_obligation(vspec.spec, state_dir=state_dir)
    return _obligation_record(state_dir, vspec.spec.dispatch_id)


def _expected_gate(stack: str) -> str:
    """The operator's primary review gate: the head of the stack, skipping ci_gate."""
    for item in stack.split(","):
        name = item.strip()
        if name and name != "ci_gate":
            return name
    return ""


# ---------------------------------------------------------------------------
# The property: obligation and configuration never disagree.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stack", _CONFIGURED_STACKS)
def test_declared_gate_is_the_configured_primary_gate(tmp_path, monkeypatch, stack):
    """The door fills a silent spec from VNX_DEFAULT_REVIEW_STACK."""
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", stack)

    vspec = _validated_spec(
        tmp_path, dispatch_id="D-config-stack", paths=(_writing_path(),),
    )
    resolved, _reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert resolved.spec.gate == _expected_gate(stack), (
        f"a silent spec must take its gate from the configured stack {stack!r}; "
        f"got {resolved.spec.gate!r}"
    )


@pytest.mark.parametrize("stack", _CONFIGURED_STACKS)
def test_obligation_and_configuration_never_disagree(tmp_path, monkeypatch, stack):
    """The incident's property, asserted on the record the merge door reads.

    If an obligation names a gate the operator did not configure, a merge stalls
    on a seat nobody chose — which is exactly what happened to five PRs on one
    day. This compares the obligation record against the configuration, never
    against a literal gate name.
    """
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", stack)

    vspec = _validated_spec(
        tmp_path, dispatch_id="D-config-obligation", paths=(_writing_path(),),
    )
    resolved, _reason = dispatch_cli._resolve_gate_via_router(vspec)
    record = _register(resolved, _state_dir(tmp_path))

    assert record["gate"] == _expected_gate(stack), (
        f"obligation declares {record['gate']!r} while the configured stack "
        f"{stack!r} names {_expected_gate(stack)!r}"
    )
    assert record["status"] == "pending", (
        "the gate is still owed review — following the configuration must not "
        f"turn the obligation into a no-op; got status={record['status']!r}"
    )


def test_obligation_agrees_with_whatever_config_resolves_in_this_environment(
    tmp_path, monkeypatch
):
    """Agreement with the REAL resolution chain, not with a stack this test chose.

    No config layer is neutralised here on purpose: the test asks
    ``config_runtime.get`` what this environment resolves, and requires the door
    to declare exactly that. It therefore holds whether the value comes from an
    operator's ``project_config`` row, an env var or the registry default — and
    it is the literal form of the dispatch requirement "an obligation names the
    same gate as VNX_DEFAULT_REVIEW_STACK for that project" (D-f2429d7d).
    """
    resolved_stack = config_runtime.get("VNX_DEFAULT_REVIEW_STACK") or ""
    expected = _expected_gate(resolved_stack)
    if not expected:
        pytest.skip(
            "no review gate configured in this environment; the loud-failure "
            "cases below cover that state"
        )

    vspec = _validated_spec(
        tmp_path, dispatch_id="D-config-live", paths=(_writing_path(),),
    )
    resolved, _reason = dispatch_cli._resolve_gate_via_router(vspec)
    record = _register(resolved, _state_dir(tmp_path))

    assert record["gate"] == expected, (
        f"this environment resolves VNX_DEFAULT_REVIEW_STACK={resolved_stack!r} "
        f"(gate {expected!r}) but the obligation declares {record['gate']!r}"
    )


# ---------------------------------------------------------------------------
# ci_gate is not a review seat.
# ---------------------------------------------------------------------------

def test_ci_gate_is_never_the_declared_review_gate(tmp_path, monkeypatch):
    """ci_gate is appended to the review stack separately (VNX_CI_GATE_REQUIRED);
    it is not a review seat, so it is never what the door declares.

    The rule is positional, so it holds for any stack: the declared gate is the
    first entry that is not ci_gate.
    """
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "ci_gate,glm_gate")

    vspec = _validated_spec(
        tmp_path, dispatch_id="D-config-ci-skip", paths=(_writing_path(),),
    )
    resolved, _reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert resolved.spec.gate == "glm_gate"


# ---------------------------------------------------------------------------
# Loud failure: an unreadable configuration is amber, never a silent default.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stack", ["", "  ", "ci_gate", "ci_gate,ci_gate", ",,"])
def test_unreadable_stack_leaves_the_gate_empty_never_a_hardcoded_fallback(
    tmp_path, monkeypatch, stack
):
    """A configuration that yields no review gate is amber, never silently green.

    The door must NOT fall back to ``smart_router.GOVERNANCE_VARIANT_GATE``.
    Doing so would declare a gate the operator did not choose, and the failure
    would be invisible: the dispatch would look governed while running on a name
    nobody configured. That silent fallback is the 2026-09-19 defect itself.

    The empty gate is not the outcome — it is the input to the door's existing
    ``gate-required`` refusal for a writing dispatch, pinned in the next test.
    """
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", stack)

    vspec = _validated_spec(
        tmp_path, dispatch_id="D-config-empty", paths=(_writing_path(),),
    )
    resolved, reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert resolved.spec.gate == "", (
        f"stack {stack!r} names no review gate; expected no declared gate, "
        f"got {resolved.spec.gate!r}"
    )
    assert reason is None
    assert resolved.spec.gate not in set(smart_router.GOVERNANCE_VARIANT_GATE.values()), (
        "an unreadable configuration must never resolve to a name from the router's "
        f"hardcoded table {smart_router.GOVERNANCE_VARIANT_GATE!r}"
    )


def test_empty_gate_on_a_writing_dispatch_meets_the_doors_refusal(
    tmp_path, monkeypatch
):
    """The loud half: an empty gate on a writing dispatch is refused, not run.

    Re-evaluates the door's own condition after router resolution
    (``dispatch_cli.py``, the ``gate-required`` reject in ``run_dispatch``:
    ``not (spec.gate or "").strip() and _spec_is_writing(spec)``). Both halves
    are the real functions on the real spec, so this pins that the loud-failure
    path is reachable rather than merely intended.

    If this ever goes green with a gate present, the failure mode has changed
    and the previous test's empty-gate outcome is no longer harmless.
    """
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "ci_gate")

    vspec = _validated_spec(
        tmp_path, dispatch_id="D-config-refused", paths=(_writing_path(),),
    )
    resolved, _reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert not (resolved.spec.gate or "").strip()
    assert dispatch_cli._spec_is_writing(resolved.spec), (
        "this fixture must describe a writing dispatch, otherwise the door would "
        "legitimately accept it with no gate and this test would prove nothing"
    )


def test_a_silent_config_records_no_gate_rather_than_a_review_obligation(
    tmp_path, monkeypatch
):
    """Downstream half: the empty gate must not leave a pending review record.

    ``_register_gate_obligation`` writes the explicit no-gate sentinel for an
    empty gate, not a review gate. Asserted on the record so a reader cannot
    mistake it for a review seat nobody has run yet, and so the sentinel is
    checked against the name the merge door reads.
    """
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "ci_gate")

    vspec = _validated_spec(
        tmp_path, dispatch_id="D-config-no-gate", paths=(_writing_path(),),
    )
    resolved, _reason = dispatch_cli._resolve_gate_via_router(vspec)
    record = _register(resolved, _state_dir(tmp_path))

    assert record["gate"] == "__no_gate__"
    assert record.get("no_gate") is True
    assert record["status"] == "not_executable"
    assert record["gate"] not in set(smart_router.GOVERNANCE_VARIANT_GATE.values())


# ---------------------------------------------------------------------------
# The loud failure, end to end, through the real door.
# ---------------------------------------------------------------------------

def test_the_door_refuses_when_config_names_no_review_gate(tmp_path, monkeypatch, capsys):
    """End-to-end: the loud failure is the DOOR's refusal, not a log line.

    Drives ``run_dispatch`` on a real staged bundle — the same entry point the
    fabric uses — with a configuration that names no review seat. The door must
    exit non-zero with the ``gate-required`` reject, never reach execution, and
    leave no obligation behind: the dispatch never ran, so there is nothing to
    review.

    This is what makes the empty-gate outcome above safe rather than a hole. The
    dispatch asked for no obligation to be WEAKENED; the obligation survives,
    and a configuration that names no gate stops the dispatch instead of
    quietly nominating one.
    """
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "ci_gate")

    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260919-staging-config-nogate",
        dispatch_id="20260919-config-no-gate",
        gate="",
        dispatch_paths=["scripts/lib/dispatch_cli.py"],
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude_headless", return_value=0) as mock_execute:
        rc = run_dispatch(spec_file)

    assert rc == 1
    assert "gate-required" in capsys.readouterr().err, (
        "the refusal must name the rule that stopped it, so an operator reading "
        "the door's output knows to fix VNX_DEFAULT_REVIEW_STACK"
    )
    assert mock_execute.call_count == 0, "a refused dispatch must never reach execution"
    assert not (
        data_dir / "state" / "review_gates" / "obligations" / "20260919-config-no-gate.json"
    ).exists(), "a refused writing dispatch must not leave a review-gate obligation"


def test_the_door_declares_the_configured_gate_end_to_end(tmp_path, monkeypatch):
    """The positive half, through the real door: the obligation follow the config.

    Same entry point, a configuration that names a review seat. The obligation
    the door leaves must carry that seat — this is the incident's property
    measured on the artifact a merge door reads, not on an in-process value.
    """
    _isolate_config(monkeypatch)
    stack = "glm_gate,claude_github_optional"
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", stack)

    data_dir, spec_file = _make_bundle(
        tmp_path,
        staging_id="20260919-staging-config-gate",
        dispatch_id="20260919-config-gate",
        gate="",
        dispatch_paths=["scripts/lib/dispatch_cli.py"],
    )
    _make_state_dir(tmp_path)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with patch("dispatch_cli._execute_claude_headless", return_value=0):
        rc = run_dispatch(spec_file)

    assert rc == 0, "a writing dispatch with a configured gate must be accepted"
    record = _read_obligation(data_dir / "state", "20260919-config-gate")
    assert record["gate"] == _expected_gate(stack), (
        f"the door registered gate={record['gate']!r} while the configuration "
        f"names {_expected_gate(stack)!r}"
    )
    assert record["status"] == "pending", "the review is still owed"


# ---------------------------------------------------------------------------
# An explicit gate is the author's decision and outranks the default.
# ---------------------------------------------------------------------------

def test_an_explicit_gate_on_the_spec_still_wins(tmp_path, monkeypatch):
    """The configuration fills a silent spec; it never overrides an author.

    ``pin_semantics=default`` — the door fills in, it never overrides.
    """
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "glm_gate,claude_github_optional")

    vspec = _validated_spec(
        tmp_path,
        dispatch_id="D-config-explicit",
        gate="kimi_gate",
        paths=(_writing_path(),),
    )
    resolved, reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert resolved.spec.gate == "kimi_gate"
    assert reason is None, "an explicit spec gate needs no router trace — nothing was filled in"


# ---------------------------------------------------------------------------
# The trace must stay honest about where the gate came from, and about risk.
# ---------------------------------------------------------------------------

def test_trace_names_the_configuration_as_the_source(tmp_path, monkeypatch):
    """Without this, a later audit cannot tell an operator-set gate from an
    author-declared one, and the two have different owners."""
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "glm_gate,claude_github_optional")

    vspec = _validated_spec(
        tmp_path, dispatch_id="D-config-trace", paths=(_writing_path(),),
    )
    _resolved, reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert reason is not None
    assert "VNX_DEFAULT_REVIEW_STACK" in reason, (
        f"the route trace must name where the gate came from; got {reason!r}"
    )
    assert "declared on spec" not in reason, (
        f"the trace must not credit the spec with an operator-configured gate; got {reason!r}"
    )


def test_router_trace_still_names_the_derived_variant(tmp_path, monkeypatch):
    """Following the configuration must not blind the trace to risk.

    The variant is what says WHAT the change is. A schema migration is a
    PATH-DERIVABLE irreversible category (``_IRREVERSIBLE_PREFIXES``:
    ``scripts/migrations/``), so it forces coding-strict whatever reversible
    ladder the path would otherwise take. The variant stays in the trace, so a
    configured gate that sits lighter than the risk class still shows as a
    downgrade instead of happening quietly.
    """
    _isolate_config(monkeypatch)
    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "glm_gate,claude_github_optional")

    vspec = _validated_spec(
        tmp_path,
        dispatch_id="D-config-variant",
        paths=(
            DispatchPath(
                path=PurePosixPath("scripts/migrations/0099_x.sql"),
                access=PathAccess.READ_WRITE,
                materialize_at_cwd=False,
            ),
        ),
    )
    resolved, reason = dispatch_cli._resolve_gate_via_router(vspec)

    assert resolved.spec.gate == "glm_gate"
    assert "governance_variant='coding-strict'" in reason, (
        f"the migration path must still derive the strict variant; got {reason!r}"
    )


def test_configured_gate_is_read_fresh_not_frozen_at_import(tmp_path, monkeypatch):
    """The configuration is read per dispatch, not captured at module import.

    An operator who changes the stack must not have to restart the door before
    the next dispatch honours it.
    """
    _isolate_config(monkeypatch)

    vspec = _validated_spec(
        tmp_path, dispatch_id="D-config-fresh", paths=(_writing_path(),),
    )

    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "glm_gate")
    first, _reason = dispatch_cli._resolve_gate_via_router(vspec)

    monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "kimi_gate")
    second, _reason = dispatch_cli._resolve_gate_via_router(
        dataclasses.replace(vspec, spec=dataclasses.replace(vspec.spec, gate=""))
    )

    assert first.spec.gate == "glm_gate"
    assert second.spec.gate == "kimi_gate"
