"""Actor class of a governed handeling (ADR-056, section Norm).

One pure derivation, no I/O besides the optional TTY probe. Two independent
signals decide the class: the TTY and the agent marker. The class is derived
here and never written as a literal by a caller.

Classes (exactly five):

* ``operator``   - a TTY and no agent marker.
* ``t0``         - an agent marker with the orchestrator role (``CLAUDE_ROLE``
                   or terminal T0). A delegated promote is ``t0`` plus
                   ``metadata.grant`` (PR-5b); it is not a sixth class.
* ``worker``     - a dispatch marker with another role than orchestrator.
* ``automation`` - no TTY, no agent marker, a known process owner.
* ``unknown``    - everything else, including an agent without a marker and
                   without a TTY. Never counted as autonomous, never as nil.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, Optional

OPERATOR = "operator"
T0 = "t0"
WORKER = "worker"
AUTOMATION = "automation"
UNKNOWN = "unknown"
ACTOR_CLASSES = (OPERATOR, T0, WORKER, AUTOMATION, UNKNOWN)

ORCHESTRATOR_ROLE = "orchestrator"
ORCHESTRATOR_TERMINAL = "T0"
DISPATCH_MARKERS = ("VNX_DISPATCH_ID", "VNX_CURRENT_DISPATCH_ID")
ROLE_MARKER = "CLAUDE_ROLE"
TERMINAL_MARKERS = ("VNX_TERMINAL", "VNX_TERMINAL_ID")
#: Env var naming the owner of a non-interactive process; only these values count.
PROCESS_OWNER_MARKER = "VNX_PROCESS_OWNER"
KNOWN_PROCESS_OWNERS = frozenset({"daemon", "reconciler", "scheduled"})


def _value(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _stdin_isatty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        # No usable stdin means no TTY: the fail-closed answer is "not interactive".
        return False


def _has_dispatch_marker(env: Mapping[str, str]) -> bool:
    return any(_value(env, key) for key in DISPATCH_MARKERS)


def _is_orchestrator(env: Mapping[str, str]) -> bool:
    if _value(env, ROLE_MARKER).lower() == ORCHESTRATOR_ROLE:
        return True
    return any(_value(env, key).upper() == ORCHESTRATOR_TERMINAL for key in TERMINAL_MARKERS)


def _has_agent_marker(env: Mapping[str, str]) -> bool:
    return (
        bool(_value(env, ROLE_MARKER))
        or any(_value(env, key) for key in TERMINAL_MARKERS)
        or _has_dispatch_marker(env)
    )


def derive(
    env: Optional[Mapping[str, str]] = None,
    isatty: Optional[bool] = None,
) -> str:
    """Return the actor class for *env* and the TTY signal (defaults: process env, stdin)."""
    env = os.environ if env is None else env
    tty = _stdin_isatty() if isatty is None else bool(isatty)
    if _has_agent_marker(env):
        if _is_orchestrator(env):
            return T0
        return WORKER if _has_dispatch_marker(env) else UNKNOWN
    if tty:
        return OPERATOR
    if _value(env, PROCESS_OWNER_MARKER).lower() in KNOWN_PROCESS_OWNERS:
        return AUTOMATION
    return UNKNOWN


derive_actor_class = derive
