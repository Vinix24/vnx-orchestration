"""The deciding delta round of the codex seat under ``VNX_REVIEW_SCOPE=delta`` (ADR-064 besluit 1 to 3, deltareview DR-6 part 2).

:func:`plan_round` runs in the gate worktree before the model run: the delta prompt for an eligible follow-up head, else the
full prompt and the ``full_review_reason``. After the run :meth:`RoundPlan.stdout_for` closes the delta answer into one ordinary
codex verdict for the artifact path and :meth:`RoundPlan.request_for` carries ``scope`` and the parent chain onto the result.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import logging
from itertools import takewhile
from pathlib import Path
from typing import Any, Dict, Mapping, NamedTuple, Optional, Tuple

import review_delta_shadow as shadow
import review_rounds
import review_scope
from codex_parser import extract_final_agent_message
from gate_artifacts import _classify_findings
from review_delta import PASS, DeltaInput, DeltaRefused, DeltaVerdict, build_delta_prompt, parse_delta_verdict
from review_findings import blocks, id_for_finding

logger = logging.getLogger(__name__)

SCOPE_DELTA, SCOPE_FULL = "delta", "full"


class DeltaContext(NamedTuple):
    record: DeltaInput
    blockers: Tuple[str, ...]
    parent: review_rounds.RoundLine
    lines: Mapping[str, int] = {}  # blocker id -> the line in this head's source that gives back the same id and context


class RoundPlan(NamedTuple):
    """The prompt to send and what to book after the run; ``delta`` is ``None`` for a full round."""
    prompt: str
    fields: Mapping[str, str] = {}
    delta: Optional[DeltaContext] = None

    def request_for(self, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        """The request the result is stamped from."""
        return {**request_payload, **self.fields} if self.fields else request_payload

    def stdout_for(self, stdout: str) -> str:
        """The run's stream, closed by the one ordinary verdict a delta answer stands for; a full round is returned as it is."""
        if self.delta is None:
            return stdout
        parsed = parse_delta_verdict(extract_final_agent_message(stdout), [f.id for f in self.delta.record.open_findings],
                                     self.delta.blockers)
        return stdout.rstrip("\n") + "\n" + json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": _verdict_text(self.delta, parsed)}}) + "\n"


def _verdict_text(ctx: DeltaContext, parsed: DeltaVerdict) -> str:
    """Prose naming the delta verdict, the per-id statuses and the parent round, then the normal verdict object."""
    held = {f.id: f for f in ctx.record.open_findings}
    # identity round-trips: original path and message, and the line that resolves to the same context; the status stays in the prose
    findings = [{"severity": "error", "file_path": held[i].path, "line": ctx.lines[i], "message": held[i].text}
                for i in ctx.blockers if i in held and blocks(parsed.statuses.get(i))]
    # a new finding the delta parser blocks on, but the artifact classifier would not, is raised to ``error``
    findings += [dict(f, severity="error") if shadow._blocking_new(f) and not _classify_findings([dict(f)])[0] else dict(f)
                 for f in parsed.new_findings]
    prose = (f"Delta round on head {ctx.record.head}: verdict {parsed.verdict}; parent round {ctx.record.parent_round_ref} "
             f"(head {ctx.parent.head_sha}). Statuses (the finding objects below keep their original identity): {json.dumps(dict(parsed.statuses), sort_keys=True)}. Reasons: {"; ".join(parsed.reasons) or "none"}.")
    verdict = {"verdict": parsed.verdict if parsed.verdict == PASS else "fail", "findings": findings, "residual_risk": ""}
    return prose + "\n" + json.dumps(verdict)


def _scope_line(tree: ast.AST, qualified: str, prefix: str = "") -> Optional[int]:
    """The ``lineno`` of the ``def``/``class`` named ``qualified`` (dotted, as ``symbolic_context`` writes it), else ``None``."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = prefix + node.name
            if name == qualified:
                return node.lineno
            found = _scope_line(node, qualified, name + ".")
        else:
            found = _scope_line(node, qualified, prefix)
        if found is not None:
            return found
    return None


def _blocker_lines(worktree: Path, head: str, seat: str, held: Mapping[str, Mapping[str, Any]],
                   blockers: Tuple[str, ...]) -> Optional[Dict[str, int]]:
    """The head line per blocker whose finding, written back with it, recomputes the same id; ``None`` when one cannot be found."""
    lines: Dict[str, int] = {}
    for fid in blockers:
        finding = held.get(fid)
        path = str((finding or {}).get("file_path") or "")
        source = shadow._git(worktree, "show", f"{head}:{path}") if path else None
        if finding is None or source is None:
            return None
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return None
        context = str(finding.get("symbolic_context") or "")
        line = _scope_line(tree, context) if context else 0  # module-level code has no scope; line 0 resolves to ""
        if line is None or id_for_finding(seat, {**finding, "line": line}, source) != fid:
            return None
        lines[fid] = line
    return lines


def _full(prompt: str, reason: str) -> RoundPlan:
    return RoundPlan(prompt, {"scope": SCOPE_FULL, "full_review_reason": reason})


def _own_paths(worktree: Path, base_ref: str, head: str) -> Tuple[str, ...]:
    out = shadow._git(worktree, "diff", "--name-only", f"{base_ref}...{head}")
    if out is None:
        raise RuntimeError("the own diff cannot be read")
    return tuple(out.split())


def _eligible_parent(request_payload: Mapping[str, Any], gates_dir: Path, head: str) -> Tuple[str, Optional[review_rounds.RoundLine]]:
    """``(reason, parent)``: the last codex round on another head, or why there is none (ADR-064 besluit 2)."""
    if review_rounds.payload_seat(review_rounds.CODEX_SEAT, dict(request_payload)) != review_rounds.CODEX_SEAT \
            or request_payload.get("takeover") is True:
        return "not_codex_first_seat", None
    if request_payload.get("risk_class") != "medium":
        return "risk_not_medium", None
    pr_number = request_payload.get("pr_number")
    ledger = review_rounds.read_ledger(review_rounds.ledger_path(gates_dir))
    project_id = review_rounds.store_project_id(gates_dir)
    rounds = review_rounds.codex_rounds(ledger.for_pr(project_id, pr_number)) if isinstance(pr_number, int) else ()
    prior = [r for r in rounds if r.head_sha != head]
    if not prior:
        return "no_parent_round", None
    chain = len(list(takewhile(lambda r: r.scope == SCOPE_DELTA, reversed(prior))))
    return ("max_chain_reached", None) if chain >= shadow.max_chain() else ("", prior[-1])


def _decide(request_payload: Mapping[str, Any], gates_dir: Path, worktree: Path, prompt: str) -> RoundPlan:
    head = str(request_payload.get("commit_sha") or "")
    reason, parent = _eligible_parent(request_payload, gates_dir, head)
    if parent is None:
        return _full(prompt, reason)
    if (shadow._git(worktree, "rev-parse", "HEAD") or "").strip() != head:
        raise RuntimeError("worktree is not on the exact head")
    index = shadow.parent_index(worktree, parent)
    held = tuple(i for i, f in (index or {}).items() if _classify_findings([f])[0])
    if index is None or len(held) != parent.blocking_count:  # the blockers must be provable from the parent's own report
        return _full(prompt, "parent_findings_unresolvable")
    parent = dataclasses.replace(parent, finding_ids=held)
    base_ref = str(request_payload.get("base_ref") or "origin/main")
    record, blockers = shadow.assemble_delta_input(worktree, base_ref, int(request_payload["pr_number"]), head, parent)
    paths = (*_own_paths(worktree, base_ref, head), *record.interdiff.paths)
    if any(not p.endswith(".py") for p in paths):  # symbols and callers are computed for Python only (OI-2784)
        return _full(prompt, "non_python_path")
    try:
        delta_prompt = build_delta_prompt(record)
    except DeltaRefused as refused:
        return _full(prompt, refused.full_review_reason)
    lines = _blocker_lines(worktree, head, parent.seat, index, blockers)
    if lines is None:  # the retained blocker cannot be written back with its own identity: the delta is not provable
        return _full(prompt, "blocker_anchor_line_unresolvable")
    fields = {"scope": SCOPE_DELTA, "parent_head_sha": parent.head_sha, "parent_round_ref": record.parent_round_ref,
              "changekind": record.changekind.changekind, "delta_base": base_ref}
    return RoundPlan(delta_prompt, fields, DeltaContext(record, blockers, parent, lines))


def plan_round(*, gate: str, prompt: str, request_payload: Mapping[str, Any], state_dir: Path, worktree: Path) -> RoundPlan:
    """The round to run in ``worktree``; only the codex gate under ``delta`` may leave the full prompt."""
    if gate != review_rounds.CODEX_SEAT or review_scope.active_review_scope(state_dir).value != SCOPE_DELTA:
        return RoundPlan(prompt)
    try:
        return _decide(request_payload, Path(state_dir) / "review_gates", Path(worktree), prompt)
    except Exception as exc:  # fail-closed: a delta that cannot be proven is a full round that says so
        logger.warning("review_delta_decide: %s: %s", type(exc).__name__, exc)
        return _full(prompt, "delta_input_error")
