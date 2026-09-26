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
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, Dict

logger = logging.getLogger(__name__)

# The floor is 1, not a percentile of the observed distribution. A threshold
# tuned to look like normal runs would need re-tuning whenever the fleet's
# habits change, and would start rejecting honest small reviews. "Took at
# least one action" needs no tuning and rejects only the degenerate shape.
MIN_INVESTIGATIVE_ACTIONS = 1

# Item types are classified from two explicit sets, with a third bucket for
# anything in neither. An earlier version listed only the message types and
# counted EVERYTHING else as investigative, so that a new tool type would count
# on the day it appeared. That reasoning is right for tools and exactly wrong
# for messages: a message alias this module does not know — codex also emits
# `assistant_message` and `output_text` in some versions — would have been
# counted as work, and a run that took zero tools while emitting one would have
# cleared the floor. The permissive default has to sit where being wrong is
# safe, and for a degeneracy check that is nowhere.
#
# Measured across 348 headless reports in this store: `command_execution`
# (3854), `agent_message` (736), `file_change` (56), `web_search` (8). Nothing
# else appears, so the sets below are the observed vocabulary plus the aliases
# codex is known to emit elsewhere.
_INVESTIGATIVE_ITEM_TYPES = frozenset({
    "command_execution",
    "file_change",
    "web_search",
    "mcp_tool_call",
    "patch_apply",
})
_MESSAGE_ITEM_TYPES = frozenset({
    "agent_message",
    "assistant_message",
    "output_text",
    "reasoning",
    "todo_list",
    "error",
})

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
    no event stream. Only codex_gate/gemini_review emit one today.

    ``mode`` distinguishes the TWO shapes this dataclass now carries (OI-1618):
    ``"agentic"`` (the tool-call stream above, measured by
    :func:`measure_execution_depth`) and ``"single_shot"`` (a one-shot API
    lane — glm_gate/kimi_gate — measured by :func:`single_shot_depth`).
    ``investigative_actions``/``shell_calls``/``files_read``/
    ``agent_messages``/``unrecognised_item_types`` only ever mean anything in
    agentic mode. One dataclass, not two, so
    :func:`gate_recorder.record_terminal_result` can hold every terminal
    writer to the same requirement without knowing which lane produced the
    measurement.

    ``diff_chars``/``diff_truncated``/``diff_limit``/``truncated_files``
    describe the diff the gate was handed, on every mode (OI-1851, see
    :func:`with_diff_coverage`).
    """

    parsed: bool = False
    mode: str = "agentic"
    investigative_actions: int = 0
    shell_calls: int = 0
    files_read: int = 0
    agent_messages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unrecognised_item_types: tuple = ()
    diff_chars: int = 0
    diff_truncated: bool = False
    diff_limit: int = 0
    truncated_files: tuple = ()

    def to_dict(self) -> Dict[str, Any]:
        """JSON-shaped dict: ``unrecognised_item_types`` normalizes to a list.

        ``asdict()`` alone preserves the field's tuple type. JSON has no tuple
        type, so a round trip through disk (write, then ``json.loads`` it back)
        already turns it into a list -- normalizing here means the in-memory
        dict a caller stamps onto its own payload (record_terminal_result does
        exactly this) agrees with what a reader gets back from the file,
        instead of silently disagreeing only on this one field's type.
        """
        data = asdict(self)
        data["unrecognised_item_types"] = list(data["unrecognised_item_types"])
        data["truncated_files"] = list(data["truncated_files"])
        return data


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
    unrecognised: set = set()
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
            if item_type in _MESSAGE_ITEM_TYPES:
                messages += 1
                continue
            if item_type not in _INVESTIGATIVE_ITEM_TYPES:
                unrecognised.add(str(item_type))
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
        mode="agentic",
        investigative_actions=investigative,
        shell_calls=shell,
        files_read=reads,
        agent_messages=messages,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unrecognised_item_types=tuple(sorted(unrecognised)),
    )


_DIFF_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


def diff_coverage(diff_text: str, max_chars: int) -> Dict[str, Any]:
    """The coverage fields of :class:`ExecutionDepth` for a diff capped at
    ``max_chars`` (OI-1851): full post-strip size, whether the raw text exceeds
    the cap (the test ``gate_prompt.wrap_untrusted_diff`` cuts on), the cap
    (0 = uncapped), and every file whose section does not end inside the cap.
    """
    text = diff_text or ""
    truncated = max_chars > 0 and len(text) > max_chars
    files: list = []
    if truncated:
        headers = list(_DIFF_FILE_HEADER_RE.finditer(text))
        for index, match in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            if end > max_chars:
                files.append(match.group(2))
    return {
        "diff_chars": len(text.strip()),
        "diff_truncated": truncated,
        "diff_limit": max(max_chars, 0),
        "truncated_files": files,
    }


def with_diff_coverage(depth: ExecutionDepth, coverage: Any) -> ExecutionDepth:
    """Stamp a :func:`diff_coverage` result onto any depth (OI-1851). A
    non-dict ``coverage`` (no diff in hand) leaves ``depth`` unmeasured."""
    if not isinstance(coverage, dict):
        return depth
    return replace(
        depth,
        diff_chars=int(coverage.get("diff_chars") or 0),
        diff_truncated=bool(coverage.get("diff_truncated")),
        diff_limit=int(coverage.get("diff_limit") or 0),
        truncated_files=tuple(coverage.get("truncated_files") or ()),
    )


def coverage_gap(depth: ExecutionDepth) -> str:
    """Why this run's verdict does not cover the whole diff, or ``""`` (OI-1851).

    Only MEASURED reading closes the gap: an agentic run with a parsed tool
    stream (no unrecognised item types) and at least one file read per cut
    file. A count, not a path match: the stream does not name what a
    ``sed``/``rg`` touched in a form worth trusting. A single-shot run never
    closes it (the prompt is all it saw), nor does ``parsed: false``.
    """
    if not depth.diff_truncated:
        return ""
    needed = max(1, len(depth.truncated_files))
    if (
        depth.parsed
        and depth.mode == "agentic"
        and not depth.unrecognised_item_types
        and depth.files_read >= needed
    ):
        return ""
    shown = list(depth.truncated_files[:10])
    more = len(depth.truncated_files) - len(shown)
    files = ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")
    return (
        f"diff of {depth.diff_chars} chars was cut at {depth.diff_limit} chars in the "
        f"gate prompt; {len(depth.truncated_files)} file(s) seen partially or not at all"
        f"{': ' + files if files else ''}; the run did not demonstrably read the rest "
        f"itself (parsed={depth.parsed}, mode={depth.mode}, files_read={depth.files_read})"
    )


def single_shot_depth(
    diff_chars: int,
    diff_truncated: bool,
    *,
    diff_limit: int = 0,
    truncated_files: tuple = (),
) -> ExecutionDepth:
    """Depth for a single-shot review lane (glm_gate/kimi_gate): one API call
    against a diff, no agentic tool loop to count shell calls or file reads
    from (OI-1618). The diff IS the investigation — there is nothing else the
    run could have looked at — so the only question degeneracy can ask here is
    whether the diff carried anything at all.

    ``diff_chars`` is the caller's own post-strip length (``len(diff.strip())``),
    not the raw byte count — :func:`is_degenerate` trusts this value directly
    rather than re-deriving it, so the "0 chars after strip" rule lives in one
    place (the caller that already has the raw text) instead of two.
    ``diff_truncated`` records whether the gate's own ``MAX_DIFF_CHARS`` cap
    fired; it is never treated as degenerate on its own (see
    :func:`is_degenerate`), but a truncated single-shot run can never close
    its own :func:`coverage_gap` (OI-1851).
    """
    return ExecutionDepth(
        parsed=True,
        mode="single_shot",
        diff_chars=diff_chars,
        diff_truncated=diff_truncated,
        diff_limit=diff_limit,
        truncated_files=tuple(truncated_files),
    )


def from_dict(data: Any) -> ExecutionDepth:
    """Reconstruct an :class:`ExecutionDepth` from its own ``to_dict()`` output.

    Used to carry a depth measurement FORWARD without re-measuring — a
    re-anchored verdict (``gate_reanchor_cli.py``) reviewed nothing new, so it
    takes the ORIGINAL run's depth rather than manufacturing a fresh one
    (OI-1618), exactly as it already carries the original verdict forward.

    Anything that is not a dict, or a dict with no recognisable fields (a
    record written before this module tracked ``execution_depth`` at all),
    reconstructs to the default ``parsed=False`` depth — which
    :func:`is_degenerate` always reads as "not measured", never as
    "degenerate". That is the same never-guess-on-absence rule this module
    already applies to a stream it cannot parse; a stale record must not be
    retroactively held to a floor it was never measured against.
    """
    if not isinstance(data, dict):
        return ExecutionDepth()
    known = {f.name for f in fields(ExecutionDepth)}
    kwargs = {k: v for k, v in data.items() if k in known}
    for tuple_field in ("unrecognised_item_types", "truncated_files"):
        if kwargs.get(tuple_field) is not None:
            kwargs[tuple_field] = tuple(kwargs[tuple_field])
    try:
        return ExecutionDepth(**kwargs)
    except TypeError:
        return ExecutionDepth()


def is_degenerate(depth: ExecutionDepth) -> bool:
    """True when the run reached a verdict without taking a single action.

    False whenever the depth was not measured. A gate whose lane emits no
    event stream must keep working exactly as before: this check exists to
    stop a measured emptiness from being accepted, not to demand that every
    lane become measurable first.

    Single-shot mode (OI-1618) asks a different question than agentic mode:
    there are no tool calls to count, so degeneracy is EXCLUSIVELY an empty
    diff (0 characters after strip). Truncation is a fact about size, never
    about content, and is never degenerate on its own — a diff capped at
    ``MAX_DIFF_CHARS`` still handed the model real material to review.
    """
    if not depth.parsed:
        return False
    if depth.mode == "single_shot":
        return depth.diff_chars == 0
    if depth.unrecognised_item_types:
        # The stream carried an item type this module cannot classify, so the
        # action count is a floor and not a measurement. Refusing on it would
        # be refusing on something not measured, which is the one thing this
        # check must never do.
        return False
    return depth.investigative_actions < MIN_INVESTIGATIVE_ACTIONS


def degenerate_detail(depth: ExecutionDepth) -> str:
    """The human-readable why, carried into the result record's reason_detail."""
    if depth.mode == "single_shot":
        return (
            f"single-shot gate reviewed a diff of {depth.diff_chars} character(s) "
            f"after strip (diff_truncated={depth.diff_truncated}) — a verdict "
            "reached against an empty diff is not gate evidence"
        )
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
    "coverage_gap",
    "degenerate_detail",
    "diff_coverage",
    "from_dict",
    "with_diff_coverage",
    "is_degenerate",
    "measure_execution_depth",
    "single_shot_depth",
]
