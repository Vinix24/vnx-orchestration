"""Tests for smart_router governance-variant derivation (gate-weight selection).

The router derives a ``governance_variant`` from dispatch_paths (and, when there
are none, task_class) and maps it to a review-gate weight. This suite pins the
three requirements of the change:

- a dispatch touching scripts/lib/ gets a STRICTER variant than one touching
  only docs/
- an explicit gate on the spec always wins over the derived gate
- the chosen variant + reason are visible in the trace (reason string), and a
  lighter-than-baseline gate is never silent (direction=down)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from smart_router import (
    GOVERNANCE_VARIANT_GATE,
    _GATE_WEIGHT,
    _GATE_BASELINE,
    derive_governance_variant,
    resolve_gate,
)
from observability_tier import GOVERNANCE_MIN_TIERS
from dispatch_spec import Gate


class TestDeriveVariant:
    def test_governance_core_path_is_coding_strict(self):
        result = derive_governance_variant(
            dispatch_paths=["scripts/lib/dispatch_cli.py"]
        )
        assert result.variant == "coding-strict"
        assert result.gate == "codex_gate"

    def test_providers_path_is_coding_strict(self):
        result = derive_governance_variant(
            dispatch_paths=["scripts/lib/providers/provider_registry.py"]
        )
        assert result.variant == "coding-strict"

    def test_docs_only_path_is_minimal(self):
        result = derive_governance_variant(
            dispatch_paths=["docs/operations/dispatch-rules.md"]
        )
        assert result.variant == "minimal"
        assert result.gate == "ci_gate"

    def test_general_code_path_is_default(self):
        result = derive_governance_variant(
            dispatch_paths=["scripts/lib/some_utility.py"]
        )
        assert result.variant == "default"
        assert result.gate == "codex_gate"

    def test_core_is_stricter_than_docs(self):
        core = derive_governance_variant(dispatch_paths=["scripts/lib/dispatch_cli.py"])
        docs = derive_governance_variant(dispatch_paths=["docs/foo.md"])
        assert _GATE_WEIGHT[core.gate] > _GATE_WEIGHT[docs.gate]
        assert core.variant == "coding-strict"
        assert docs.variant == "minimal"

    def test_mixed_core_and_docs_is_coding_strict(self):
        # Strictest path category wins across all touched paths — never a
        # silent downgrade to the docs class.
        result = derive_governance_variant(
            dispatch_paths=["scripts/lib/dispatch_cli.py", "docs/foo.md"]
        )
        assert result.variant == "coding-strict"

    def test_no_paths_docs_task_class_is_minimal(self):
        result = derive_governance_variant(task_class="04_documentation")
        assert result.variant == "minimal"

    def test_no_paths_defaults_to_default(self):
        result = derive_governance_variant()
        assert result.variant == "default"

    def test_derivation_is_deterministic(self):
        args = dict(dispatch_paths=["scripts/lib/smart_router.py"])
        first = derive_governance_variant(**args)
        second = derive_governance_variant(**args)
        assert first == second


class TestResolveGate:
    def test_explicit_gate_wins_over_lighter_derivation(self):
        result = resolve_gate(
            explicit_gate="codex_gate",
            dispatch_paths=["docs/operations/foo.md"],
        )
        assert result.gate == "codex_gate"
        assert result.source == "explicit"
        assert result.governance_variant == ""

    def test_explicit_gate_wins_even_when_router_would_be_stricter(self):
        result = resolve_gate(
            explicit_gate="wiring_gate",
            dispatch_paths=["scripts/lib/dispatch_cli.py"],
        )
        assert result.gate == "wiring_gate"
        assert result.source == "explicit"

    def test_silent_docs_derives_minimal_ci_gate(self):
        result = resolve_gate(dispatch_paths=["docs/operations/foo.md"])
        assert result.source == "derived"
        assert result.gate == "ci_gate"
        assert result.governance_variant == "minimal"

    def test_silent_core_derives_coding_strict(self):
        result = resolve_gate(dispatch_paths=["scripts/lib/dispatch_cli.py"])
        assert result.source == "derived"
        assert result.gate == "codex_gate"
        assert result.governance_variant == "coding-strict"

    def test_explicit_reason_says_not_overridden(self):
        result = resolve_gate(explicit_gate="codex_gate")
        assert "did not override" in result.reason


class TestTraceVisibility:
    def test_derived_reason_contains_variant_and_gate(self):
        result = resolve_gate(dispatch_paths=["scripts/lib/dispatch_cli.py"])
        assert "governance_variant=coding-strict" in result.reason
        assert "gate=codex_gate" in result.reason

    def test_light_gate_direction_is_down_in_reason(self):
        # A docs dispatch gets a LIGHTER gate than the codex_gate baseline; the
        # trace must say so explicitly (never a silent lighter gate).
        result = resolve_gate(dispatch_paths=["docs/operations/foo.md"])
        assert "direction=down" in result.reason

    def test_baseline_gate_direction_is_unchanged(self):
        result = resolve_gate(dispatch_paths=["scripts/lib/some_utility.py"])
        assert "direction=unchanged" in result.reason


class TestVocabularyGuard:
    def test_all_variant_gate_keys_are_governance_variants(self):
        unknown = set(GOVERNANCE_VARIANT_GATE) - set(GOVERNANCE_MIN_TIERS)
        assert unknown == set(), f"gate map declares unknown variants: {sorted(unknown)}"

    def test_every_mapped_gate_is_a_legal_gate_enum(self):
        legal = {g.value for g in Gate}
        for variant, gate in GOVERNANCE_VARIANT_GATE.items():
            assert gate in legal, f"variant {variant!r} maps to illegal gate {gate!r}"

    def test_baseline_gate_is_the_heaviest(self):
        assert _GATE_BASELINE == "codex_gate"
        assert _GATE_WEIGHT["codex_gate"] == max(_GATE_WEIGHT.values())
