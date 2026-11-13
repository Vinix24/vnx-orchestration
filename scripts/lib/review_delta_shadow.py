"""Shadow delta run next to the full codex round (ADR-064 besluit 1, 2, 4, 5, plan DR-4b).

After the full codex round judged a follow-up head, the codex seat also answers the delta prompt; the answer is
booked in ``review_gates/shadow/delta.ndjson``. The full round decides: nothing here writes to ``rounds.ndjson``, a
result or an obligation, and no reader of those reads the shadow folder (C4). A failure is a warning, never raised.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from itertools import takewhile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import review_rounds
from codex_parser import extract_final_agent_message, extract_verdict_block
from gate_worktree import create_gate_worktree, remove_gate_worktree
from review_changekind import ChangeKindError, Interdiff, anchors_present, classify_change, interdiff, seat_input_set
from review_delta import (Caller, DeltaInput, DeltaRefused, OpenFinding, _blocking_new, build_delta_prompt,
                          parse_delta_verdict)
from review_findings import id_for_finding, symbolic_context

logger = logging.getLogger(__name__)

SHADOW_DIR, SHADOW_NAME = "shadow", "delta.ndjson"
MAX_CHAIN_KEY, MAX_CHAIN_DEFAULT, PAIR_LIMIT = "VNX_REVIEW_DELTA_MAX_CHAIN", 2, 20
REASON_ERROR = "shadow_error"
Runner = Callable[[str, Path], Mapping[str, Any]]

def shadow_path(review_gates_dir: Path) -> Path:
    return Path(review_gates_dir) / SHADOW_DIR / SHADOW_NAME


def read_shadow(path: Path) -> List[Dict[str, Any]]:
    """Every readable shadow line; a corrupt or missing file yields what parses."""
    try:
        rows = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for raw in filter(str.strip, rows):
        try:
            row = json.loads(raw)
        except ValueError:
            row = None
        if isinstance(row, dict):
            out.append(row)
    skipped = len([r for r in rows if r.strip()]) - len(out)
    if skipped:  # DR-5 counts pairs from this file: a skipped line is a pair that may be missing
        logger.warning("review_delta_shadow: %d unreadable line(s) skipped in %s", skipped, path)
    return out


def max_chain() -> int:
    try:
        return int(os.environ.get(MAX_CHAIN_KEY, ""))
    except ValueError:
        return MAX_CHAIN_DEFAULT  # unreadable or non-integer


def book(path: Path, record: Mapping[str, Any]) -> None:
    """One line, ``O_APPEND`` plus fsync; the shadow folder is created on first use."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


class Eligibility(NamedTuple):
    reason: str  # empty when eligible
    parent: Optional[review_rounds.RoundLine] = None
    full: Optional[review_rounds.RoundLine] = None


def shadow_eligible(*, seat: str, risk_class: Any, rounds: Sequence[review_rounds.RoundLine], head: str,
                    shadow_lines: Sequence[Mapping[str, Any]], pr_number: int, takeover: bool = False) -> Eligibility:
    """Eligible only: codex first seat, medium PR, follow-up head, chain and pair limits open; a false answer names why."""
    if seat != review_rounds.CODEX_SEAT or takeover:
        return Eligibility("not_codex_first_seat")
    if risk_class != "medium":
        return Eligibility("risk_not_medium")
    heads = [r.head_sha for r in rounds]
    if head not in heads:
        return Eligibility("head_not_booked")
    if head == heads[0]:
        return Eligibility("first_head")
    mine = [s for s in shadow_lines if s.get("pr_number") == pr_number]
    if len(list(takewhile(lambda s: s.get("delta_verdict"), reversed(mine)))) >= max_chain():
        return Eligibility("max_chain_reached")
    if sum(1 for s in shadow_lines if s.get("delta_verdict")) >= PAIR_LIMIT:
        return Eligibility("pair_limit_reached")
    at = heads.index(head)
    return Eligibility("", rounds[at - 1], rounds[at])


def _git(repo: Path, *args: str) -> Optional[str]:
    done = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=30)
    return done.stdout if done.returncode == 0 else None


def parent_index(repo: Path, parent: review_rounds.RoundLine) -> Optional[Dict[str, Dict[str, Any]]]:
    """Every finding in the parent's report by id, with symbolic context; ``None`` when the report cannot be read."""
    try:
        text = Path(parent.report_path).read_text(encoding="utf-8", errors="replace")
        raw = extract_verdict_block(text).get("findings") or []
    except (OSError, ValueError, AttributeError):
        return None
    found: Dict[str, Dict[str, Any]] = {}
    for finding in raw:
        try:
            path = str(finding.get("file_path") or "")
            source = _git(repo, "show", f"{parent.head_sha}:{path}") if path else None
            fid = id_for_finding(parent.seat, finding, source)
            found[fid] = {**finding, "id": fid, "symbolic_context": symbolic_context(source, finding.get("line"))}
        except (ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            continue
    return found


def parent_findings(repo: Path, parent: review_rounds.RoundLine) -> Optional[List[Dict[str, Any]]]:
    """The parent's findings behind its recorded ids with id and symbolic context; ``None`` when an id is unresolved."""
    found = parent_index(repo, parent)
    if found is None:
        return None
    return [found[i] for i in parent.finding_ids] if all(i in found for i in parent.finding_ids) else None


def assemble_delta_input(repo: Path, base_ref: str, pr_number: int, head: str,
                         parent: review_rounds.RoundLine) -> Tuple[DeltaInput, Tuple[str, ...]]:
    """The :class:`DeltaInput` and the parent's blocker ids; what cannot be computed leaves ``changekind`` unset."""
    kind, diff, callers, open_findings, found = None, Interdiff(), (), (), parent_findings(repo, parent)
    if found is not None:  # unresolved parent findings: no changekind, so the full review is forced
        try:
            diff = interdiff(repo, base_ref, parent.head_sha, head)
            reach = seat_input_set(repo, head, diff.paths)
            kind = classify_change(repo, base_ref, parent.head_sha, head, reach)
            callers = tuple(Caller(p, "<module>") for p in sorted(reach - diff.paths))
            anchors = anchors_present(repo, head, found)["anchors"]
            open_findings = tuple(
                OpenFinding(f["id"], str(f.get("file_path") or ""), f["symbolic_context"], str(f.get("severity") or ""),
                            str(f.get("message") or ""), a["status"]) for f, a in zip(found, anchors))
        except ChangeKindError as exc:
            logger.warning("review_delta_shadow: input not computable: %s", exc)
            kind = None
    blockers = tuple(f["id"] for f in found or () if _blocking_new(f))
    record = DeltaInput(pr_number, parent.seat, parent.head_sha, parent.verdict, parent.report_name or parent.dispatch_id,
                        head, kind, diff, open_findings, callers)
    return record, blockers


def run_shadow(*, request_payload: Mapping[str, Any], state_dir: Path, project_root: Path, run: Runner,
               seat: str = review_rounds.CODEX_SEAT) -> Optional[Dict[str, Any]]:
    """Run and book the shadow delta for the head just judged; the booked line, ``None`` when not eligible."""
    gates_dir = Path(state_dir) / "review_gates"
    path = shadow_path(gates_dir)
    pr_number, head = request_payload.get("pr_number"), str(request_payload.get("commit_sha") or "")
    line: Dict[str, Any] = {"pr_number": pr_number, "seat": seat, "head": head}
    worktree: Optional[Path] = None
    try:
        ledger = review_rounds.read_ledger(review_rounds.ledger_path(gates_dir))
        project_id = review_rounds.store_project_id(gates_dir)
        rounds = review_rounds.codex_rounds(ledger.for_pr(project_id, pr_number)) if isinstance(pr_number, int) else ()
        verdict = shadow_eligible(seat=seat, risk_class=request_payload.get("risk_class"), rounds=rounds, head=head,
                                  shadow_lines=read_shadow(path), pr_number=pr_number)
        if verdict.reason:
            return None
        parent, full = verdict.parent, verdict.full
        line.update(parent_head=parent.head_sha, parent_round_ref=parent.report_name or parent.dispatch_id,
                    full_verdict=full.verdict, full_finding_ids=list(full.finding_ids))
        worktree = create_gate_worktree(branch=str(request_payload.get("branch") or ""), gate=seat,
                                        identifier=f"{pr_number}-delta", project_root=project_root)
        if (_git(worktree, "rev-parse", "HEAD") or "").strip() != head:
            raise RuntimeError("worktree is not on the exact head")
        record, blockers = assemble_delta_input(worktree, str(request_payload.get("base_ref") or "origin/main"),
                                                pr_number, head, parent)
        try:
            prompt = build_delta_prompt(record)
        except DeltaRefused as refused:
            line["full_review_reason"] = refused.full_review_reason
            book(path, line)
            return line
        result = run(prompt, worktree)
        if result.get("status") == "failed":
            raise RuntimeError(f"codex delta run failed: {result.get('reason')}")
        stdout = str(result.get("stdout") or "")
        parsed = parse_delta_verdict(extract_final_agent_message(stdout), [f.id for f in record.open_findings], blockers)
        line.update(delta_verdict=parsed.verdict, statuses=dict(parsed.statuses), new_findings=list(parsed.new_findings),
                    ignored_ids=list(parsed.ignored_ids), reasons=list(parsed.reasons), duration_seconds=result.get("duration_seconds"))
        book(path, line)
        return line
    except Exception as exc:  # fail-open: the full round's outcome never depends on the shadow step
        logger.warning("review_delta_shadow: %s", exc)
        try:
            book(path, {**line, "reasons": [REASON_ERROR], "error": str(exc)[:200]})
        except Exception as booking:  # fail-open: the full round never depends on the shadow booking
            logger.warning("review_delta_shadow: booking the error line in %s failed: %s: %s", path,
                           type(booking).__name__, booking)
        return None
    finally:
        if worktree is not None:
            remove_gate_worktree(worktree, project_root=project_root)
