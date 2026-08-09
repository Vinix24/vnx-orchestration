#!/usr/bin/env python3
"""Drift pin: _KNOWN_GATES must stay derived from the Gate enum (OI-1094).

Before this dispatch, closure_verifier carried a fourth hand-written list of
gate names (_KNOWN_GATES) that drifted from the canonical Gate enum in
scripts/lib/dispatch_spec.py: it omitted wiring_gate, which the enum, the
gate_recorder and gate_request_handler all know about. Five places holding the
same collection without talking to each other is drift-prone by design.

_KNOWN_GATES is now derived from Gate: every enum member is either implemented
by the closure verifier (in _KNOWN_GATES) or explicitly excluded with a reason
(in _GATES_NOT_IMPLEMENTED_BY_CLOSURE). These tests pin that invariant so the
two collections cannot silently drift again.

Against main (commit 60601c1f / d86f132d) these tests are RED: wiring_gate is
declared by the Gate enum but present in neither _KNOWN_GATES (it was simply
missing) nor _GATES_NOT_IMPLEMENTED_BY_CLOSURE (that set did not exist).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier as cv
from dispatch_spec import Gate


class TestKnownGatesDerivedFromEnum:
    def test_known_gates_is_a_subset_of_enum_members(self):
        """Every implemented gate must be a declared Gate enum value."""
        enum_values = {g.value for g in Gate}
        assert cv._KNOWN_GATES.issubset(enum_values), (
            f"_KNOWN_GATES has values not in the Gate enum: "
            f"{sorted(cv._KNOWN_GATES - enum_values)}"
        )

    def test_excluded_gates_is_a_subset_of_enum_members(self):
        """Every explicitly-excluded gate must be a declared Gate enum value."""
        enum_values = {g.value for g in Gate}
        assert cv._GATES_NOT_IMPLEMENTED_BY_CLOSURE.issubset(enum_values), (
            f"_GATES_NOT_IMPLEMENTED_BY_CLOSURE has values not in the Gate enum: "
            f"{sorted(cv._GATES_NOT_IMPLEMENTED_BY_CLOSURE - enum_values)}"
        )

    def test_known_and_excluded_are_disjoint(self):
        """A gate is either implemented or excluded, never both."""
        overlap = cv._KNOWN_GATES & cv._GATES_NOT_IMPLEMENTED_BY_CLOSURE
        assert not overlap, (
            f"gates in both _KNOWN_GATES and _GATES_NOT_IMPLEMENTED_BY_CLOSURE: "
            f"{sorted(overlap)}"
        )

    def test_every_enum_member_is_accounted_for(self):
        """No Gate enum member may fall through both collections.

        This is the drift pin. On main, wiring_gate is declared by the enum
        but in neither set, so this assertion fails there and passes here.
        """
        enum_values = {g.value for g in Gate}
        accounted = cv._KNOWN_GATES | cv._GATES_NOT_IMPLEMENTED_BY_CLOSURE
        missing = enum_values - accounted
        assert not missing, (
            f"Gate enum members with no closure-verifier disposition: {sorted(missing)}. "
            f"Add them to _KNOWN_GATES (if implemented) or to "
            f"_GATES_NOT_IMPLEMENTED_BY_CLOSURE (with a reason)."
        )

    def test_wiring_gate_is_explicitly_excluded_not_silently_missing(self):
        """wiring_gate must be excluded by name, with a reason, not absent.

        It emits no terminal result-record with contract_hash + report_path, so
        the generic known-gate handling cannot honestly attest it. Keeping it
        outside _KNOWN_GATES also preserves OI-1093 (#1422): records for gates
        not in _KNOWN_GATES must carry a producer identity (dispatch_id). When
        in doubt, the side with more verification wins.
        """
        assert "wiring_gate" in cv._GATES_NOT_IMPLEMENTED_BY_CLOSURE
        assert "wiring_gate" not in cv._KNOWN_GATES

    def test_every_implemented_gate_has_a_handler(self):
        """Every gate in _KNOWN_GATES must have a handler in the dispatch table.

        A gate marked implemented but with no handler would raise KeyError at
        runtime instead of producing a CheckResult — the table-driven design
        must stay complete.
        """
        missing_handlers = cv._KNOWN_GATES - set(cv._GATE_HANDLERS.keys())
        assert not missing_handlers, (
            f"_KNOWN_GATES without a _GATE_HANDLERS entry: {sorted(missing_handlers)}"
        )

    def test_no_handler_for_a_gate_outside_known_gates(self):
        """No handler may exist for a gate the verifier does not claim to implement."""
        stray = set(cv._GATE_HANDLERS.keys()) - cv._KNOWN_GATES
        assert not stray, (
            f"_GATE_HANDLERS entries for gates not in _KNOWN_GATES: {sorted(stray)}"
        )
