"""OI-645: _enforce_pr_exists return annotation must resolve.

The F821 undefined-name finding on ``PrEnforcementResult`` (ruff,
scripts/lib/tmux_interactive_dispatch.py) was not just lint noise:
``typing.get_type_hints`` on the affected signature raised ``NameError``
because the name was only imported on a deferred exception path inside the
function body, never bound at module scope. Any introspection of the
signature (docs, dispatch-door type checks) would crash.

Fails on the pre-fix code (get_type_hints -> NameError) and passes once the
module-level import of PrEnforcementResult lands in
scripts/lib/tmux_interactive_dispatch.py.
"""

from __future__ import annotations

import typing

import pytest

import tmux_interactive_dispatch  # type: ignore[import-not-found]


def test_enforce_pr_exists_return_annotation_resolves():
    """get_type_hints on the OI-645-flagged signature must resolve."""
    func = tmux_interactive_dispatch.TmuxInteractiveDispatch._enforce_pr_exists
    try:
        hints = typing.get_type_hints(func)
    except NameError as exc:  # unresolved annotation name (pre-fix failure)
        pytest.fail(f"_enforce_pr_exists: unresolved annotation name: {exc}")
    assert hints["return"] is not None
