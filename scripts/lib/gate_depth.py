"""How much investigation a gate run actually did (OI-1485).

A gate result record says the gate ran. It did not say whether the gate
LOOKED. Measured on PR #1707: two codex_gate runs on the same head sha
(00e64792) under the same contract_hash (088a30754169bb91) — the same
instruction over the same diff — produced

    run A   14s    0 shell calls    18219 input tokens   0 findings
    run B  227s   16 shell calls   239992 input tokens   1 real defect

and run A's own residual_risk said what had happened: "The review is limited
to the provided diff." Across the twelve gate runs of that day the normal
range is 5-42 shell calls and 86k-2.5M input tokens, so run A is two orders
below the floor of the distribution, not the low end of it.

The acceptance gap is that all seven headless-review invariants hold for run A
exactly as they hold for run B: request record present, execution completed,
result record present, contract_hash non-empty and matching, report_path
non-empty, report file on disk, and JSON and report agreeing. Nothing in that
list asks whether the gate did any work, so a PASS from a run that read only
the diff is indistinguishable from a PASS from a run that read the tree and
ran the tests. It was caught only because 14 seconds stood out beside 150.

The shape of the two runs, from the event stream both of them emitted::

    degenerate  item.completed: {agent_message: 1}
    real        item.completed: {command_execution: 16, agent_message: 1}

An ``agent_message`` is the verdict itself; emitting one is not evidence of
anything. What separates the two is whether the run took a single
investigative action. That is the floor this module measures against, and it
is deliberately the weakest possible one: not "enough" investigation, just
some.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict

logger = logging.getLogger(__name__)

# The floor is 1, not a percentile of the observed distribution. A threshold
# tuned to look like normal runs would need re-tuning whenever the fleet's
# habits change, and would start rejecting honest small reviews. "Took at
# least one action" needs no tuning and rejects only the degenerate shape.
MIN_INVESTIGATIVE_ACTIONS = 1

# item.completed types that are the model talking rather than the model
# working. Everything else counts as an investigative action, so a new tool
# type in the stream counts on the day it appears instead of the day someone
# remembers to add it here.
_NON_INVESTIGATIVE_ITEM_TYPES = frozenset({"agent_message", "reasoning", "todo_list"})

# Commands whose point is to get file content in front of the model.
_FILE_READ_RE = re.compile(r"\b(sed|cat|head|tail|less|rg|grep|awk|find|ls)\b")


@dataclass(frozen=True)
class ExecutionDepth:
    """What a gate run did, as opposed to what it concluded.

    ``parsed`` is the field that decides whether the rest means anything. A
    stream this module does not recognise yields ``parsed=False`` and zeros,
    and zeros that mean "not measured" must never be read as zeros that mean
    "did nothing" — that is the difference between an unmeasured gate and a
    degenerate one, and collapsing it would fail every gate whose lane emits
    no event stream. Only codex_gate emits one today; kimi_gate and glm_gate
    reports carry no events at all.
    """

    parsed: bool = False
    investigative_actions: int = 0
    shell_calls: int = 0
    files_read: int = 0
    agent_messages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def measure_execution_depth(stdout: str) -> ExecutionDepth:
    """Count what the run did, from the event stream it emitted.

    Never raises: an unparseable or absent stream is a measurement this
    module could not make, not a gate failure. A malformed line is skipped
    rather than aborting the count, because a truncated stream still carries
    evidence about the part that did arrive.
    """
    if not stdout:
        return ExecutionDepth()

    recognised = False
    investigative = shell = reads = messages = 0
    input_tokens = output_tokens = 0
    seen_items: set = set()

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type in {"thread.started", "turn.started", "item.started",
                          "item.completed", "turn.completed"}:
            recognised = True

        if event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id is not None and item_id in seen_items:
                continue
            if item_id is not None:
                seen_items.add(item_id)
            item_type = item.get("type")
            if item_type in _NON_INVESTIGATIVE_ITEM_TYPES:
                messages += 1
                continue
            investigative += 1
            if item_type == "command_execution":
                shell += 1
                if _FILE_READ_RE.search(str(item.get("command") or "")):
                    reads += 1
        elif event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)

    if not recognised:
        return ExecutionDepth()

    return ExecutionDepth(
        parsed=True,
        investigative_actions=investigative,
        shell_calls=shell,
        files_read=reads,
        agent_messages=messages,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def is_degenerate(depth: ExecutionDepth) -> bool:
    """True when the run reached a verdict without taking a single action.

    False whenever the depth was not measured. A gate whose lane emits no
    event stream must keep working exactly as before: this check exists to
    stop a measured emptiness from being accepted, not to demand that every
    lane become measurable first.
    """
    if not depth.parsed:
        return False
    return depth.investigative_actions < MIN_INVESTIGATIVE_ACTIONS


def degenerate_detail(depth: ExecutionDepth) -> str:
    """The human-readable why, carried into the result record's reason_detail."""
    return (
        f"the run produced {depth.agent_messages} message(s) and "
        f"{depth.investigative_actions} investigative action(s) "
        f"(minimum {MIN_INVESTIGATIVE_ACTIONS}); "
        f"{depth.shell_calls} shell call(s), {depth.files_read} file read(s), "
        f"{depth.input_tokens} input tokens — a verdict reached without "
        f"looking at anything beyond the prompt is not gate evidence"
    )


__all__ = [
    "ExecutionDepth",
    "MIN_INVESTIGATIVE_ACTIONS",
    "degenerate_detail",
    "is_degenerate",
    "measure_execution_depth",
]
