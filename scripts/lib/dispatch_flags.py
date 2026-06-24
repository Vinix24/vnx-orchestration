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
        unset / ""               -> _DEFAULT_ENABLED   (POST-FLIP: the single-entry door)
        "0"                      -> False   (explicit opt-out — legacy lane)
        "1" / any other non-"0"  -> True

FLIPPED 2026-06-24 (door-flip D2, ADR-024): _DEFAULT_ENABLED is now True — the single-entry door
is the DEFAULT dispatch lane. The routing-split (D1, ADR-025) keeps the documented raw
`vnx dispatch <file.md>` form working on the legacy lane (deprecated) so the flip is safe.
Rollback to the legacy lane: VNX_DISPATCH_LEGACY=1 (always wins) or VNX_SINGLE_ENTRY_DISPATCH=0.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

# Post-flip default (door-flip D2, ADR-024, 2026-06-24): the single-entry door is the default lane.
# Rollback: VNX_DISPATCH_LEGACY=1 or VNX_SINGLE_ENTRY_DISPATCH=0.
_DEFAULT_ENABLED = True

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
