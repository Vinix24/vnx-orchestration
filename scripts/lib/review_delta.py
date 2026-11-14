"""Delta prompt builder and verdict parser (ADR-064 besluit 1 and 3, plan C2, deltareview DR-4a).

Pure: no git, no file IO, no clock. The caller computes the record (DR-2 changekind and interdiff, DR-1 ids,
callers at depth 1); this module turns it into one prompt and reads the seat's one ``delta_verdict/1`` answer.
A refused build is never a prompt. A parse that cannot prove the answer is FAIL, never PASS. A sent blocker id
that is ``open``, ``regressed`` or ``unverifiable`` makes the verdict FAIL whatever the block says (C2).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from review_changekind import ANCHOR_PRESENT, NO_CODE_CHANGE, REWORK, TRIVIAL_REBASE, ChangeKindRecord, Interdiff
from review_findings import STATUS_FIXED, STATUS_OPEN, STATUS_REGRESSED, STATUS_UNVERIFIABLE, blocks

FENCE = "delta_verdict/1"
REASON_NO_PARENT = "no_parent_round"
REASON_CANNOT_PROVE = "cannot_prove"
REASON_ANCHOR = "anchor_unverifiable"
PASS, FAIL = "pass", "fail"
#: Answers a seat may give per id. ``waived`` is an operator decision, not a reviewer answer.
_ANSWERS = (STATUS_FIXED, STATUS_OPEN, STATUS_REGRESSED, STATUS_UNVERIFIABLE)
#: A new finding is advisory only on one of these; a missing or unknown severity blocks (fail-closed).
_ADVISORY_SEVERITIES = ("warning", "info", "advisory", "nit")
#: Every fenced block whatever its info string: the answer may hold exactly one, and it is the verdict block.
_BLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
REASON_EXTRA_BLOCK = "extra_block"
REASON_TRAILING = "trailing_content"
REASON_DUPLICATE_KEY = "duplicate_key"
REASON_INVALID_STATUS = "invalid_status"


class DeltaRefused(ValueError):
    """A full review is forced; ``full_review_reason`` says why. No prompt exists for this record."""

    def __init__(self, full_review_reason: str) -> None:
        super().__init__(f"delta review refused: {full_review_reason}")
        self.full_review_reason = full_review_reason


class _DuplicateKey(ValueError):
    """The verdict block names one JSON key twice in the same object."""


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """``object_pairs_hook`` for every object level: json.loads would keep the last value of a repeated key silently."""
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _DuplicateKey(REASON_DUPLICATE_KEY)
    return dict(pairs)


@dataclass(frozen=True)
class OpenFinding:
    id: str
    path: str
    symbolic_context: str
    severity: str
    text: str
    anchor: str = ANCHOR_PRESENT


@dataclass(frozen=True)
class Caller:
    path: str
    symbol: str


@dataclass(frozen=True)
class DeltaInput:
    pr_number: int
    seat: str
    parent_head: str
    parent_verdict: str
    parent_round_ref: str
    head: str
    changekind: Optional[ChangeKindRecord]
    interdiff: Interdiff
    open_findings: Tuple[OpenFinding, ...]
    callers: Tuple[Caller, ...]


@dataclass(frozen=True)
class DeltaVerdict:
    verdict: str
    statuses: Mapping[str, str]
    new_findings: Tuple[Dict[str, Any], ...]
    ignored_ids: Tuple[str, ...]
    reasons: Tuple[str, ...]


def full_review_reason(record: DeltaInput) -> Optional[str]:
    """Why this record forces a full review (besluit 2), or ``None`` when a delta may judge it."""
    if not (record.parent_head and record.parent_verdict and record.parent_round_ref):
        return REASON_NO_PARENT
    kind = record.changekind
    if kind is None or kind.changekind not in (NO_CODE_CHANGE, TRIVIAL_REBASE, REWORK):
        return REASON_CANNOT_PROVE
    if kind.full_review_reason:
        return kind.full_review_reason
    if any(f.anchor != ANCHOR_PRESENT for f in record.open_findings):
        return REASON_ANCHOR
    return None


def disarm(text: Any) -> str:
    """Input embedded in the prompt as one line with no two adjacent backticks, whatever the run length, so it cannot close or open an answer fence (OI-2203)."""
    return re.sub(r"`(?=`)", "` ", " ".join(str(text).split()))


def _interdiff_block(diff: Interdiff) -> List[str]:
    lines = []
    for path in sorted(diff.paths):
        symbols = sorted(name for _, name in diff.symbols.get(path, frozenset()))
        lines.append(f"  - {disarm(path)}: " + (", ".join(disarm(s) for s in symbols) or "no symbol-level change found"))
    return lines or ["  (no paths differ)"]


# (untrusted content, verdict contract, instruction) -> the prompt; the gate runner's reviewer assembly
Assemble = Callable[[str, str, str], str]


def _plain(content: str, contract: str, instruction: str) -> str:
    return "\n\n".join([instruction, content, contract])


def build_delta_prompt(record: DeltaInput, assemble: Assemble = _plain) -> str:
    """The delta brief through ``assemble``; deterministic. The deciding round and the shadow pass the reviewer assembly
    of the full review, so the role policy and the untrusted-data rules stay. Raises :class:`DeltaRefused` when a full review is forced."""
    reason = full_review_reason(record)
    if reason is not None:
        raise DeltaRefused(reason)
    findings = sorted(record.open_findings, key=lambda f: f.id)
    callers = sorted({(c.path, c.symbol) for c in record.callers})
    content = [
        "Parent chain:\n"
        f"  parent head: {disarm(record.parent_head)}\n  parent verdict: {disarm(record.parent_verdict)}\n"
        f"  parent round: {disarm(record.parent_round_ref)}\n  new head: {disarm(record.head)}",
        "Interdiff between the parent head's own diff and the new head's own diff:\n"
        + "\n".join(_interdiff_block(record.interdiff)),
        "Open findings, by id:\n" + ("\n".join(
            f"  - {disarm(f.id)} [{disarm(f.severity)}] {disarm(f.path)} :: {disarm(f.symbolic_context) or '<module>'}: "
            f"{disarm(f.text)}" for f in findings) or "  (none)"),
        "Callers at depth 1:\n" + ("\n".join(f"  - {disarm(p)} :: {disarm(s)}" for p, s in callers) or "  (none found)"),
    ]
    return assemble(
        "\n\n".join(content),
        "Answer with exactly one fenced block in the whole answer and nothing after it. Any other fenced block, "
        "before or after it, or any text after it, makes the verdict fail:\n"
        f"```{FENCE}\n"
        '{"verdict": "pass or fail", "findings": [{"id": "<sent id>", "status": "fixed|open|regressed"}], '
        '"new_findings": [{"file_path": "", "line": 0, "severity": "", "message": ""}]}\n```\n',
        f"You review a follow-up head of PR #{int(record.pr_number)} as seat {disarm(record.seat)}. "
        "This is a fresh verdict on the exact head, not a carried one. Read the repo as it is on that head. "
        "For every id in the untrusted-data block answer exactly one status: fixed, open or regressed. An id you leave "
        "out counts as open. Judge the delta and every caller of what the delta touches, not only the changed lines. "
        "A changed caller can misuse unchanged code.")


def _failed(reason: str, sent_ids: Sequence[str]) -> DeltaVerdict:
    return DeltaVerdict(FAIL, {i: STATUS_OPEN for i in sent_ids}, (), (), (reason,))


class _InvalidStatus(ValueError):
    """A sent id carries a status that is not one of the answers a seat may give."""


def _statuses(rows: Any, sent: FrozenSet[str]) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    given: Dict[str, str] = {}
    ignored: List[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            raise ValueError("findings entry without a string id")
        rid = row["id"]
        if rid not in sent:
            ignored.append(rid)
            continue
        answer = row.get("status")
        if not isinstance(answer, str) or answer not in _ANSWERS:  # missing, non-string or unknown: never read as an answer
            raise _InvalidStatus(f"{REASON_INVALID_STATUS}: {rid}")
        if given.get(rid, answer) != answer:
            given[rid] = STATUS_OPEN  # two different answers for one id: the finding stays open
        else:
            given[rid] = answer
    return given, tuple(ignored)


def _blocking_new(finding: Mapping[str, Any]) -> bool:
    return str(finding.get("severity") or "").lower() not in _ADVISORY_SEVERITIES


def parse_delta_verdict(text: str, sent_ids: Sequence[str], blocker_ids: Sequence[str]) -> DeltaVerdict:
    """The seat's verdict. Exactly one ``delta_verdict/1`` block or FAIL; C2 and new blocking findings force FAIL."""
    sent = tuple(sent_ids)
    raw = text if isinstance(text, str) else ""
    blocks_found = list(_BLOCK_RE.finditer(raw))
    if not blocks_found:
        return _failed(f"expected exactly one ```{FENCE}``` block in the answer", sent)
    if raw.count("```") != 2 * len(blocks_found):  # an unpaired fence is a block we cannot read
        return _failed(REASON_EXTRA_BLOCK, sent)
    if len(blocks_found) != 1 or blocks_found[0].group(1).strip() != FENCE:
        return _failed(REASON_EXTRA_BLOCK, sent)
    if raw[blocks_found[0].end():].strip():
        return _failed(REASON_TRAILING, sent)
    try:
        data = json.loads(blocks_found[0].group(2), object_pairs_hook=_no_duplicate_keys)
        if not isinstance(data, dict) or data.get("verdict") not in (PASS, FAIL):
            raise ValueError("verdict is not pass or fail")
        if not isinstance(data.get("findings"), list) or not isinstance(data.get("new_findings"), list):
            raise ValueError("findings and new_findings must be lists")
        given, ignored = _statuses(data["findings"], frozenset(sent))
        if not all(isinstance(f, dict) for f in data["new_findings"]):
            raise ValueError("new_findings entry is not an object")
    except (_DuplicateKey, _InvalidStatus) as exc:  # a named reason, no partial reading of the block
        return _failed(str(exc), sent)
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        return _failed(f"unreadable {FENCE} block: {exc}", sent)
    statuses = {i: given.get(i, STATUS_OPEN) for i in sent}
    new = tuple(data["new_findings"])
    reasons: List[str] = []
    if data["verdict"] == FAIL:
        reasons.append("the seat answered fail")
    stuck = sorted(i for i in set(blocker_ids) if blocks(statuses.get(i)))  # an unsent blocker has no answer: stuck
    if stuck:
        reasons.append("blocker ids not fixed: " + ", ".join(stuck))
    if any(_blocking_new(f) for f in new):
        reasons.append("a new finding at blocking severity")
    return DeltaVerdict(FAIL if reasons else PASS, statuses, new, ignored, tuple(reasons))
