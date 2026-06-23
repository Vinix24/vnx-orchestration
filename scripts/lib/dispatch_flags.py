"""dispatch_flags.py — single source of truth for the single-entry dispatch routing flag.

Every reader (python AND bash, via vnx_dispatch_flags.sh) MUST go through this one predicate
so the default lives in ONE place and the VNX_DISPATCH_LEGACY rollback is honored UNIFORMLY.

Background (the bug this fixes): before this helper, the top-level router (dispatch.sh
cmd_dispatch) checked BOTH flags, but the downstream delivery readers (dispatch.sh
dispatch_deliver, dispatch_deliver.sh, dispatch-agent.sh, dispatch_bridge.deliver_via_door,
pool_worker_runner, headless_dispatch_daemon, claude_adapter) checked only
VNX_SINGLE_ENTRY_DISPATCH=="1". So VNX_DISPATCH_LEGACY=1 routed to "legacy" at the top but the
legacy delivery still funneled through the door downstream — an INCOMPLETE rollback. Routing
every reader through single_entry_enabled() closes that.

Truth table (env value → enabled):
    VNX_DISPATCH_LEGACY == "1"   -> False   (rollback always wins, regardless of the other flag)
    VNX_SINGLE_ENTRY_DISPATCH:
        unset / ""               -> _DEFAULT_ENABLED   (the flip default)
        "0"                      -> False
        "1" / any other non-"0"  -> True   (NOTE: semantics widened from the old ==\"1\" to !=\"0\")

E (the flip) flips _DEFAULT_ENABLED to True, gated on the burn-in + the operator's explicit go.
Until then the default is False — pre-flip behavior is byte-identical for the canonical
unset/"0"/"1" values; only non-canonical truthy values ("2", "yes") now enable (documented).
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

# Pre-flip default. Item E flips this to True (after burn-in + operator go).
_DEFAULT_ENABLED = False

_LEGACY_ENV = "VNX_DISPATCH_LEGACY"
_SINGLE_ENTRY_ENV = "VNX_SINGLE_ENTRY_DISPATCH"


def single_entry_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return True iff a dispatch should route through the single-entry door.

    Honors VNX_DISPATCH_LEGACY=1 as an absolute rollback (wins over everything).
    """
    env = env if env is not None else os.environ
    if env.get(_LEGACY_ENV) == "1":
        return False
    raw = env.get(_SINGLE_ENTRY_ENV)
    if raw is None or raw == "":
        return _DEFAULT_ENABLED
    return raw != "0"


def default_enabled() -> bool:
    """Expose the compiled-in default (what unset resolves to). For tests/diagnostics."""
    return _DEFAULT_ENABLED
