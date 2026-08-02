"""OI-645: tmux_interactive_dispatch PR-enforcement annotation must resolve.

The F821 undefined-name finding (PrEnforcementResult in the return annotation
of TmuxInteractiveDispatch._enforce_pr_exists) was not just lint noise:
typing.get_type_hints() on the signature raised NameError because the name
was only imported locally inside the function body, never into module scope.
Any introspection of the signature (docs, dispatch-door type checks, gate
instrumentation) would crash.

Fails on the pre-fix code (get_type_hints -> NameError) and passes once the
module-level import of PrEnforcementResult lands in
scripts/lib/tmux_interactive_dispatch.py.
"""

from __future__ import annotations

import typing

import pytest

import tmux_interactive_dispatch


def test_pr_enforcement_signature_annotation_resolves():
    """get_type_hints on the F821-flagged signature must resolve."""
    func = tmux_interactive_dispatch.TmuxInteractiveDispatch._enforce_pr_exists
    try:
        hints = typing.get_type_hints(func)
    except NameError as exc:  # unresolved annotation name (pre-fix failure)
        pytest.fail(f"_enforce_pr_exists: unresolved annotation name: {exc}")
    assert hints["return"].__name__ == "PrEnforcementResult"
