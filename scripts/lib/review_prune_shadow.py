"""Change pruning in shadow (ADR-064 besluit 4, eisen 6, 8, 10; DR-8b): ``unchanged`` plus the finding-set hash,
booked in ``review_gates/shadow/prune.ndjson`` only (no ``rounds.ndjson`` line, no new ``kind``; the readers and the
door never open that folder, C4). ``would_skip`` names the one next request pruning would skip; nothing in the gate
path calls it yet."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple

from review_changekind import NO_CODE_CHANGE, TRIVIAL_REBASE, ChangeKindRecord
from review_findings import STATUS_FIXED, STATUS_OPEN, STATUS_REGRESSED, STATUS_UNVERIFIABLE, STATUS_WAIVED
from review_rounds import RoundLine

_STATUSES = frozenset({STATUS_OPEN, STATUS_FIXED, STATUS_WAIVED, STATUS_REGRESSED, STATUS_UNVERIFIABLE})
_PRUNABLE = frozenset({NO_CODE_CHANGE, TRIVIAL_REBASE})

#: The hash of an empty finding set: sha256 of its canonical encoding ``[]``.
Pairs = Optional[Iterable[Tuple[str, str]]]
EMPTY_SET_HASH = hashlib.sha256(b"[]").hexdigest()


def finding_set_hash(findings: Iterable[Tuple[str, str]]) -> str:
    """sha256 over the sorted ``(finding_id, status)`` pairs of one round; a bad pair raises ``ValueError``."""
    pairs = set()
    for item in findings:
        if not (isinstance(item, (tuple, list)) and len(item) == 2):
            raise ValueError(f"a finding is a (finding_id, status) pair, got {item!r}")
        fid, status = item
        if not (isinstance(fid, str) and fid and isinstance(status, str) and status in _STATUSES):
            raise ValueError(f"finding pair is no known id and status: {item!r}")
        pairs.add((fid, status))
    return hashlib.sha256(json.dumps(sorted(pairs), separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PruneDecision:  # ``round_ref``: the dispatch id of the round
    project_id: str
    pr_number: int
    seat: str
    head_sha: str
    parent_head_sha: str
    round_ref: str
    request_seq: int
    unchanged: bool
    finding_set_hash: str
    parent_finding_set_hash: str
    reason: str


def _hash_or_none(findings: Pairs) -> Optional[str]:
    try:
        return None if findings is None else finding_set_hash(findings)
    except (ValueError, TypeError):
        return None


def _refusal(rnd: RoundLine, parent: Optional[RoundLine], record: Optional[ChangeKindRecord],
             own: Optional[str], theirs: Optional[str]) -> str:
    """The first thing not proven, or ``""``."""
    if parent is None:
        return "no_parent_round"
    if not (rnd.parent_head_sha and parent.head_sha == rnd.parent_head_sha and parent.head_sha != rnd.head_sha
            and parent.key[:2] == rnd.key[:2] and parent.seat == rnd.seat):
        return "parent_not_of_this_round"
    if not isinstance(record, ChangeKindRecord):
        return "no_changekind"
    if not isinstance(record.security_paths, Mapping) or record.security_paths:
        return "security_path"
    if record.changekind not in _PRUNABLE or record.full_review_reason is not None:
        return "not_a_trivial_change"
    if rnd.changekind and rnd.changekind != record.changekind:
        return "changekind_disagrees_with_round"
    if own is None or theirs is None:
        return "finding_set_unreadable"
    return "" if own == theirs else "finding_set_changed"


def decide_unchanged(rnd: RoundLine, parent: Optional[RoundLine], record: Optional[ChangeKindRecord],
                     findings: Pairs, parent_findings: Pairs, request_seq: int) -> PruneDecision:
    """``unchanged`` only on a parent round, ``NO_CODE_CHANGE``/``TRIVIAL_REBASE`` without a security path and equal
    finding-set hashes. Anything missing or unprovable is not."""
    own, theirs = _hash_or_none(findings), _hash_or_none(parent_findings)
    reason = _refusal(rnd, parent, record, own, theirs)
    return PruneDecision(rnd.project_id, rnd.pr_number, rnd.seat, rnd.head_sha, rnd.parent_head_sha, rnd.dispatch_id,
                         request_seq, reason == "", own or "", theirs or "", reason or "finding_set_equal")


def shadow_path(state_dir: Path) -> Path:
    return Path(state_dir) / "review_gates" / "shadow" / "prune.ndjson"


def append_decision(state_dir: Path, decision: PruneDecision) -> Path:
    """One JSON line, ``O_APPEND`` plus fsync, into the shadow file only. ``rounds.ndjson`` is never opened."""
    path = shadow_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(asdict(decision), sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def book_decision(state_dir: Path, rnd: RoundLine, parent: Optional[RoundLine], record: Optional[ChangeKindRecord],
                  findings: Pairs, parent_findings: Pairs, request_seq: int) -> PruneDecision:
    decision = decide_unchanged(rnd, parent, record, findings, parent_findings, request_seq)
    append_decision(state_dir, decision)
    return decision


def read_decisions(state_dir: Path) -> list[dict[str, Any]]:
    """The booked lines; a bad one is left out (that can only remove a skip)."""
    path = shadow_path(state_dir)
    if not path.is_file():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return [item for item in out if isinstance(item, dict)]


def would_skip(lines: Iterable[Mapping[str, Any]], pr: int, seat: str, request_head: str, request_seq: int) -> bool:
    """True only for the request directly after an ``unchanged`` round: same PR, seat and head, seq one past it."""
    if not (type(pr) is int and type(request_seq) is int and isinstance(seat, str) and isinstance(request_head, str)):
        return False
    return any(
        isinstance(line, Mapping) and line.get("unchanged") is True and line.get("pr_number") == pr
        and line.get("seat") == seat and line.get("head_sha") == request_head and type(line.get("request_seq")) is int
        and line["request_seq"] + 1 == request_seq
        for line in lines)
