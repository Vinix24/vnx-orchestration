"""OI-288: dispatch_envelope signature annotations must resolve.

The F821 undefined-name findings (ExecutionPlan x4, ExecutionPermit x2) were
not just lint noise: typing.get_type_hints() on the affected signatures
raised NameError because the names were never imported into the module.
Any introspection of these signatures (docs, pydantic-style validation,
dispatch-door type checks) would crash.

Fails on the pre-fix code (get_type_hints -> NameError) and passes once the
runtime imports of ExecutionPlan / ExecutionPermit land in
scripts/lib/dispatch_envelope.py.
"""

from __future__ import annotations

import typing

import pytest

import dispatch_envelope


@pytest.mark.parametrize(
    "owner, attr",
    [
        (dispatch_envelope.ProviderAdapter, "run"),
        (dispatch_envelope.ProviderAdapter, "_run_kimi"),
        (dispatch_envelope, "run_envelope_plan"),
        (dispatch_envelope, "run_envelope_headless_plan"),
    ],
)
def test_signature_annotations_resolve(owner, attr):
    """get_type_hints on every F821-flagged signature must resolve."""
    func = getattr(owner, attr)
    try:
        typing.get_type_hints(func)
    except NameError as exc:  # unresolved annotation name (pre-fix failure)
        pytest.fail(f"{attr}: unresolved annotation name: {exc}")
