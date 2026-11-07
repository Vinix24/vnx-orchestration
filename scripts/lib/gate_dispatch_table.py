"""The dispatch table: which dispatcher carries a harness-lane review gate.

ADR-046 Amendment 1, section A2 (track provider-agnostic-review-lane, P1).
``GateRunner`` used to compare the registered lane with one provider's name and
fall back, for every other lane, on the governed dispatcher. A second own
transport meant a second ``if`` in the engine. The engine now asks this table.

Two maps, one resolver:

* ``DISPATCHER_FACTORIES`` maps a *dispatcher key* to a factory with the one
  signature ``(data_dir, base_ref, timeouts, project_root) -> dispatcher``.
  ``timeouts`` is what ``gate_lane_contract.harness_dispatcher_timeouts``
  returns (``timeout_seconds`` and ``total_deadline_seconds``). A factory that
  needs fewer arguments ignores the rest.
* ``LANE_DISPATCHER_KEYS`` maps the lane a ``harness_lane`` row registers
  (``GATE_PROVIDERS[gate][1]``) to its dispatcher key. Once the gate row carries
  a ``dispatcher`` column (A3, P2) this map is replaced by that column; the
  factories and the resolver stay as they are.

It fails closed. A lane without a key and a key without a factory give
:class:`UnknownDispatcher`; there is no default dispatcher. A typo in a new row
therefore shows up as a booked ``not_executable``, not as a gate that quietly
runs on another provider's transport.

The table chooses the transport. It does not choose the provider-specific parser
of the answer (that stays in ``governed_lane``) and it changes neither
``provider_dispatch`` nor ``subprocess_adapter``.

Standard library only: the dispatch door reads ``scripts/lib`` alone. The
factories import their heavy modules when called, and look the factory up on the
module at call time, so a test that patches the module attribute is honoured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

DISPATCHER_GOVERNED = "governed"
DISPATCHER_CLAUDE_LANE = "claude_lane"

DispatcherFn = Callable[[str, str, str, str], str]
DispatcherFactory = Callable[[Path, str, Mapping[str, int], Optional[Path]], DispatcherFn]


class UnknownDispatcher(LookupError):
    """A harness-lane row names no dispatcher the table can build."""


def _governed_factory(
    data_dir: Path, base_ref: str, timeouts: Mapping[str, int], project_root: Optional[Path],
) -> DispatcherFn:
    """The governed lane: ``provider_dispatch``, constraint-safe per provider."""
    # Lazy: plan_gate_panel pulls in the whole governed dispatch graph.
    import plan_gate_panel

    return plan_gate_panel._make_default_dispatcher(
        str(data_dir), timeouts["timeout_seconds"], role="review-gate", base_ref=base_ref,
        total_deadline_seconds=timeouts["total_deadline_seconds"],
    )


def _claude_lane_factory(
    data_dir: Path, base_ref: str, timeouts: Mapping[str, int], project_root: Optional[Path],
) -> DispatcherFn:
    """The subscription lane: ``claude -p`` in a worktree, never ``provider_dispatch``."""
    import claude_gate_lane

    return claude_gate_lane.make_claude_gate_dispatcher(
        data_dir, base_ref=base_ref,
        total_deadline_seconds=timeouts["total_deadline_seconds"], project_root=project_root,
    )


DISPATCHER_FACTORIES: Dict[str, DispatcherFactory] = {
    DISPATCHER_GOVERNED: _governed_factory,
    DISPATCHER_CLAUDE_LANE: _claude_lane_factory,
}

LANE_DISPATCHER_KEYS: Dict[str, str] = {
    "kimi": DISPATCHER_GOVERNED,
    "glm-harness": DISPATCHER_GOVERNED,
    "deepseek-harness": DISPATCHER_GOVERNED,
    "claude": DISPATCHER_CLAUDE_LANE,
}


def unresolved_detail(gate: str, lane: Any) -> Optional[str]:
    """Why ``gate`` on ``lane`` has no dispatcher, or ``None`` when it has one.

    Fail-closed on every shape: a lane that is not a non-empty string, a lane
    with no key, a key that is not a non-empty string and a key without a
    factory each name what is missing.
    """
    if not isinstance(lane, str) or not lane:
        return f"{gate} registers no lane ({lane!r}), so the dispatch table has no row for it"
    key = LANE_DISPATCHER_KEYS.get(lane)
    if not isinstance(key, str) or not key:
        return f"{gate} runs on lane {lane!r}, which names no dispatcher in the dispatch table"
    if not callable(DISPATCHER_FACTORIES.get(key)):
        return f"{gate} runs on lane {lane!r} with dispatcher {key!r}, which the dispatch table does not hold"
    return None


def build_dispatcher(
    gate: str, lane: Any, data_dir: Path, base_ref: str,
    timeouts: Mapping[str, int], project_root: Optional[Path] = None,
) -> DispatcherFn:
    """The dispatcher for ``gate`` on ``lane``; :class:`UnknownDispatcher` when there is none."""
    detail = unresolved_detail(gate, lane)
    if detail is not None:
        raise UnknownDispatcher(detail)
    factory = DISPATCHER_FACTORIES[LANE_DISPATCHER_KEYS[lane]]
    return factory(Path(data_dir), base_ref, timeouts, project_root)
