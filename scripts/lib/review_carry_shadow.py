"""Carried PASS in shadow (ADR-064, deltareview DR-8a).

A PASS that survives a trivial rebase is booked in ``review_gates/shadow/carry.ndjson``, never in the
real rounds. A carry is not a round (``codex_rounds`` never sees it), is replaced
by any fresh verdict on the same head, takes the seat and reviewer of its source line and is never
booked for a security path. The merge door still demands a fresh PASS per seat; nothing here is read by it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, NamedTuple, Optional, Tuple, Union
from review_changekind import NO_CODE_CHANGE, TRIVIAL_REBASE, ChangeKindRecord
from review_rounds import RoundLine

SHADOW_DIR = "shadow"
CARRY_NAME = "carry.ndjson"
CARRY_RULE_VERSION = "1"
VERDICT_ORIGIN = "carried"
_RULES = {NO_CODE_CHANGE: "no_code_change", TRIVIAL_REBASE: "trivial_rebase"}
_STRINGS = (
    "verdict_origin", "carried_from", "carry_rule", "carry_rule_version", "source_patch_id", "head_patch_id",
    "plan_scope_rule_version", "project_id", "seat", "reviewer", "head_sha", "source_head_sha", "recorded_at",
)


class CarryInvalid(ValueError):
    """A carry record that misses a provenance field, or names one it must not carry."""


@dataclass(frozen=True)
class CarryLine:
    verdict_origin: str
    carried_from: str
    carry_rule: str
    carry_rule_version: str
    source_patch_id: str
    head_patch_id: str
    security_paths_touched: Tuple[str, ...]
    plan_scope_rule_version: str
    project_id: str
    pr_number: int
    seat: str
    reviewer: str
    head_sha: str
    source_head_sha: str
    recorded_at: str

    def to_dict(self) -> Dict[str, Any]:
        record = dict(self.__dict__)
        record["security_paths_touched"] = list(self.security_paths_touched)
        return record


class CarryDecision(NamedTuple):
    line: Optional[CarryLine]
    reason: str = ""


def carry_path(review_gates_dir: Path) -> Path:
    return Path(review_gates_dir) / SHADOW_DIR / CARRY_NAME


def round_ref(line: RoundLine) -> str:
    return f"{line.pr_number}:{line.seat}:{line.head_sha}"


def validate_carry(record: Any) -> CarryLine:
    """The line, or :class:`CarryInvalid`. An empty ``security_paths_touched`` is valid; a missing key is not."""
    if not isinstance(record, Mapping):
        raise CarryInvalid("a carry record is a JSON object")
    values: Dict[str, Any] = {}
    for name in _STRINGS:
        value = record.get(name)
        if not isinstance(value, str) or not value.strip():
            raise CarryInvalid(f"field {name!r} is missing or empty")
        values[name] = value
    if values["verdict_origin"] != VERDICT_ORIGIN:
        raise CarryInvalid(f"verdict_origin must be {VERDICT_ORIGIN!r}")
    paths = record.get("security_paths_touched")
    if not isinstance(paths, (list, tuple)):
        raise CarryInvalid("field 'security_paths_touched' is missing")
    if paths:
        raise CarryInvalid("a carry never covers a security path")
    pr_number = record.get("pr_number")
    if type(pr_number) is not int:
        raise CarryInvalid("field 'pr_number' is missing or not an integer")
    return CarryLine(security_paths_touched=(), pr_number=pr_number, **values)


def decide_carry(source_line: RoundLine, changekind_record: ChangeKindRecord, *, pr: int, seat: str, head: str,
                 recorded_at: str = "") -> CarryDecision:
    """A carry only on positive evidence; anything missing or unprovable is no carry, with the reason."""
    if source_line.pr_number != pr:
        return CarryDecision(None, "other_pr")
    if source_line.seat != seat:
        return CarryDecision(None, "other_seat")
    if source_line.verdict != "pass":
        return CarryDecision(None, "source_not_pass")
    if source_line.takeover_from:
        return CarryDecision(None, "source_is_takeover")
    if not head or source_line.head_sha == head:
        return CarryDecision(None, "no_new_head")
    rule = _RULES.get(changekind_record.changekind)
    if rule is None:
        return CarryDecision(None, "changekind_" + str(changekind_record.changekind).lower())
    if changekind_record.full_review_reason:
        return CarryDecision(None, "cannot_prove_" + changekind_record.full_review_reason)
    if changekind_record.security_paths:
        return CarryDecision(None, "security_path")
    if not changekind_record.parent_patch_id or not changekind_record.head_patch_id:
        return CarryDecision(None, "cannot_prove_patch_id")
    record = {
        "verdict_origin": VERDICT_ORIGIN, "carried_from": round_ref(source_line), "carry_rule": rule,
        "carry_rule_version": CARRY_RULE_VERSION, "source_patch_id": changekind_record.parent_patch_id,
        "head_patch_id": changekind_record.head_patch_id, "security_paths_touched": [],
        "plan_scope_rule_version": changekind_record.plan_scope_rule_version, "project_id": source_line.project_id,
        "pr_number": pr, "seat": source_line.seat, "reviewer": source_line.reviewer, "head_sha": head,
        "source_head_sha": source_line.head_sha, "recorded_at": recorded_at,
    }
    try:
        return CarryDecision(validate_carry(record))
    except CarryInvalid as exc:
        return CarryDecision(None, f"invalid_provenance: {exc}")


def book_carry(review_gates_dir: Path, line: CarryLine) -> Path:
    """Append one validated line to the shadow file: ``O_APPEND``, one write, fsync. No other file is touched."""
    validate_carry(line.to_dict())
    path = carry_path(review_gates_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(line.to_dict(), sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if os.fstat(fd).st_size:
            with path.open("rb") as probe:
                probe.seek(-1, os.SEEK_END)
                if probe.read(1) != b"\n":
                    data = b"\n" + data  # a torn tail stays one skipped line, it never swallows this one
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def read_carry(review_gates_dir: Path) -> Tuple[Tuple[CarryLine, ...], int]:
    """(valid lines, skipped count). An invalid line is counted and never becomes a verdict."""
    path = carry_path(review_gates_dir)
    if not path.is_file():
        return (), 0
    lines, skipped = [], 0
    for raw in filter(str.strip, path.read_text(encoding="utf-8", errors="replace").splitlines()):
        try:
            lines.append(validate_carry(json.loads(raw)))
        except (ValueError, TypeError):  # CarryInvalid and JSONDecodeError are ValueErrors
            skipped += 1
    return tuple(lines), skipped


def effective_shadow_verdict(rounds_lines: Iterable[RoundLine], carry_lines: Iterable[CarryLine], pr: int,
                             seat: str, head: str) -> Optional[Union[RoundLine, CarryLine]]:
    """The latest fresh verdict on (pr, seat, head) wins over any carry, whatever arrived first; else the latest carry."""
    for group in (rounds_lines, carry_lines):
        hits = [x for x in group if (x.pr_number, x.seat, x.head_sha) == (pr, seat, head)]
        if hits:
            return hits[-1]
    return None
