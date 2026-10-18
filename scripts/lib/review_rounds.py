"""The round ledger: one line per review verdict, per seat, per PR head (R1).

Track ``review-stopregel-en-lichte-stoelen``, plan section 1 ("Tellen en
weigeren"). B3.1 and B3.2 (``docs/core/DISPATCH_RULES.md`` §3) stood in the T0
role and nowhere in code. This module is what makes them countable and
enforceable:

- **The ledger.** ``review_gates/rounds.ndjson`` per project, beside
  ``results/``, append-only. One line per verdict (PASS or FAIL) of every seat,
  keyed by (``project_id``, ``pr_number``, ``head_sha``, ``seat``) per ADR-007.
  The result writers (``gate_recorder.write_result_guarded`` and
  ``record_terminal_result``) append the line BEFORE the result record, under
  the ledger lock, with fsync: when the line cannot be written, the run fails
  and no verdict is published. A run without a verdict (unavailable, quota,
  crash, ``chain_exhausted``, a partial review) writes no line, and neither
  does a rewrite of a record that already carries its verdict (a takeover
  annotation, a re-anchor): that is no new verdict.
- **The count.** A round is a verdict in the codex seat on its own head. The
  seat counts, not the vendor: a takeover in the codex seat counts as codex. A
  blocker of the second seat does not count (plan, open question 2). B3.1 (three
  or more blockers in one round) is checked first, then B3.2 (the latest round
  has a blocker and the sum over all rounds is six or more). The cap is sticky:
  it falls at the first round where either rule holds, and only R2's tiebreaker
  (:func:`round_granted_past_cap`) or ``--override-b3`` gets a codex-seat round
  past it. No fingerprint: a returning blocker counts again.
- **No second verdict on a head.** A key holds one verdict. The request side
  refuses a run in a seat that already has a verdict on the head
  (``gate_request_handler``); the writer refuses a second, different verdict on
  the same key as the backstop for every path that starts a run without a
  request.

Readers: ``gate_request_handler`` (refusal where a run starts),
``review_seat_evaluation`` (the merge door, ``vnx-gate/review``,
``vnx pr-ready``), ``dispatch_cli`` (the early warning for a build on a capped
PR's branch), and ``review_rounds_rebuild`` (``vnx review rounds rebuild``).

BILLING SAFETY: no Anthropic SDK, no network, no subprocess.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LEDGER_NAME = "rounds.ndjson"
CODEX_SEAT = "codex_gate"

B3_1 = "B3.1"
B3_2 = "B3.2"
B3_1_ROUND_BLOCKERS = 3
B3_2_TOTAL_BLOCKERS = 6

OVERRIDE_FLAG = "--override-b3"
OVERRIDE_USAGE = '--override-b3 --reason "..."'
REBUILD_COMMAND = "vnx review rounds rebuild --pr {pr}"

SOURCE_GATE = "gate"
SOURCE_REBUILD = "rebuild"

# Request-time refusal reasons (gate_request_handler books them on the seat's payload).
REFUSED_VERDICT_ON_HEAD = "verdict_on_head"
REFUSED_B3_CAP = "b3_cap"
REFUSED_LEDGER_UNREADABLE = "round_ledger_unreadable"

_VERDICTS = ("pass", "fail")
_STRING_FIELDS = (
    "project_id", "head_sha", "seat", "reviewer", "takeover_from", "verdict",
    "report_path", "dispatch_id", "recorded_at", "branch", "source",
)


class RoundLedgerError(OSError):
    """The ledger could not be read or written: no verdict is published on it."""


class RoundLedgerUnreadable(RoundLedgerError):
    """A line of the ledger does not parse; the PR it may belong to is refused until rebuilt."""


class RoundRefusal(ValueError):
    """A verdict the ledger may not take: the key already holds another one, or the cap holds."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def rebuild_command(pr_number: Any) -> str:
    return REBUILD_COMMAND.format(pr=pr_number)


# ---------------------------------------------------------------------------
# Paths and identity
# ---------------------------------------------------------------------------


def ledger_path(review_gates_dir: Path) -> Path:
    """``review_gates/rounds.ndjson``: the ledger beside ``results/`` and ``requests/``."""
    return Path(review_gates_dir) / LEDGER_NAME


def ledger_path_for_results(results_dir: Path) -> Optional[Path]:
    """The ledger of a ``review_gates/results`` dir, or ``None`` outside that layout.

    The ledger lives in the canonical store layout only. A results dir that is
    not ``.../review_gates/results`` (an ad-hoc dir a script or a test writes
    into) has no ledger, and the merge door never reads one there either.
    """
    results_dir = Path(results_dir)
    if results_dir.name != "results" or results_dir.parent.name != "review_gates":
        return None
    return ledger_path(results_dir.parent)


def store_project_id(review_gates_dir: Path) -> str:
    """The project the store names itself as (ADR-007): ``""`` when it names none.

    Read from the state dir the ledger lives in, never from the environment, so
    the writer and every reader of one store derive the same key.
    """
    import vnx_paths

    return vnx_paths.project_id_from_state_dir(Path(review_gates_dir).parent) or ""


def ledger_seat_gates() -> frozenset:
    """The gates that can sit in a review seat: the review peers, minus the optional ones."""
    from review_seats import OPTIONAL_REVIEW_GATES, review_peer_gates

    return frozenset(review_peer_gates()) - OPTIONAL_REVIEW_GATES


# ---------------------------------------------------------------------------
# One line
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundLine:
    """One verdict in one seat on one head of one PR."""

    project_id: str
    pr_number: int
    head_sha: str
    seat: str
    reviewer: str
    takeover_from: str
    verdict: str
    blocking_count: int
    report_path: str
    dispatch_id: str
    recorded_at: str
    branch: str = ""
    source: str = SOURCE_GATE
    override: Optional[Dict[str, Any]] = None

    @property
    def key(self) -> Tuple[str, int, str, str]:
        return (self.project_id, self.pr_number, self.head_sha, self.seat)

    @property
    def report_name(self) -> str:
        return Path(self.report_path).name if self.report_path else ""

    def same_verdict(self, other: "RoundLine") -> bool:
        """The same verdict booked twice (a retried write), not a second run."""
        return (
            self.key == other.key and self.verdict == other.verdict
            and self.reviewer == other.reviewer and self.report_name == other.report_name
        )

    def to_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "project_id": self.project_id, "pr_number": self.pr_number,
            "head_sha": self.head_sha, "seat": self.seat, "reviewer": self.reviewer,
            "takeover_from": self.takeover_from, "verdict": self.verdict,
            "blocking_count": self.blocking_count, "report_path": self.report_path,
            "dispatch_id": self.dispatch_id, "recorded_at": self.recorded_at,
            "branch": self.branch, "source": self.source,
        }
        if self.override:
            record["override"] = dict(self.override)
        return record

    @classmethod
    def from_dict(cls, data: Any) -> "RoundLine":
        """Strict: a line that misses a field or carries a wrong type is not a line."""
        if not isinstance(data, dict):
            raise ValueError("a ledger line is a JSON object")
        values: Dict[str, Any] = {}
        for name in _STRING_FIELDS:
            value = data.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"field {name!r} is not a string")
            values[name] = value
        pr_number, blocking = data.get("pr_number"), data.get("blocking_count")
        if not _is_int(pr_number) or not _is_int(blocking) or blocking < 0:
            raise ValueError("pr_number and blocking_count must be non-negative integers")
        if values["verdict"] not in _VERDICTS or not values["head_sha"] or not values["seat"]:
            raise ValueError("verdict must be pass or fail, head_sha and seat must be set")
        override = data.get("override")
        if override is not None and not isinstance(override, dict):
            raise ValueError("override must be an object")
        return cls(pr_number=pr_number, blocking_count=blocking, override=override, **values)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Reading the ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BadLine:
    """A line that does not parse, and the PR it names when that can still be read off it."""

    number: int
    pr_number: Optional[int]
    error: str


@dataclass(frozen=True)
class Ledger:
    """The ledger as read: its lines in file order, and every line that did not parse."""

    path: Path
    exists: bool
    lines: Tuple[RoundLine, ...] = ()
    bad: Tuple[BadLine, ...] = ()

    def for_pr(self, project_id: str, pr_number: int) -> Tuple[RoundLine, ...]:
        """This PR's lines in verdict order (``recorded_at``, then file order)."""
        found = [
            (line.recorded_at, index, line) for index, line in enumerate(self.lines)
            if line.project_id == project_id and line.pr_number == pr_number
        ]
        return tuple(line for _at, _index, line in sorted(found, key=lambda item: item[:2]))

    def unreadable_for(self, pr_number: int) -> Tuple[BadLine, ...]:
        """Bad lines that may belong to this PR: its own, and every line that names no PR."""
        return tuple(bad for bad in self.bad if bad.pr_number in (None, pr_number))

    def line_for(self, key: Tuple[str, int, str, str]) -> Optional[RoundLine]:
        return next((line for line in self.lines if line.key == key), None)


def parse_ledger(path: Path, text: str) -> Ledger:
    lines: List[RoundLine] = []
    bad: List[BadLine] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        data: Any = None
        try:
            data = json.loads(raw)
            lines.append(RoundLine.from_dict(data))
        except ValueError as exc:
            bad.append(BadLine(number, _pr_named(data, raw), str(exc)))
    return Ledger(Path(path), True, tuple(lines), tuple(bad))


_PR_NUMBER_IN_TEXT = re.compile(r'"pr_number"\s*:\s*(\d+)\s*[,}]')


def _pr_named(data: Any, raw: str) -> Optional[int]:
    """The PR a bad line names, when it still names one; ``None`` refuses every PR.

    A torn append still carries its PR near the start (``to_dict`` writes
    ``pr_number`` second), so a cut line refuses its own PR, not every PR.
    """
    if isinstance(data, dict) and _is_int(data.get("pr_number")):
        return data["pr_number"]
    match = _PR_NUMBER_IN_TEXT.search(raw)
    return int(match.group(1)) if match else None


def read_ledger(path: Path) -> Ledger:
    """The ledger at ``path``, read under its lock. Absent is a ledger that does not exist yet.

    Raises :class:`RoundLedgerError` when the file exists and cannot be read.
    """
    from atomic_io import slot_lock

    path = Path(path)
    if not path.is_file():
        return Ledger(path, False)
    try:
        with slot_lock(path):
            text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RoundLedgerError(f"round ledger {path} could not be read: {exc}") from exc
    return parse_ledger(path, text)


def unreadable_text(ledger: Ledger, pr_number: int) -> str:
    """Why this PR's rounds cannot be counted, naming the rebuild; ``""`` when they can."""
    bad = ledger.unreadable_for(pr_number)
    if not bad:
        return ""
    where = ", ".join(f"line {item.number} ({item.error})" for item in bad[:3])
    return (
        f"the round ledger {ledger.path} is unreadable for PR #{pr_number}: {where}. "
        f"Rebuild it with `{rebuild_command(pr_number)}`"
    )


# ---------------------------------------------------------------------------
# The count
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapState:
    """The codex-seat rounds of one PR and whether, and where, B3 put it at the cap."""

    pr_number: int
    rounds: Tuple[RoundLine, ...] = ()
    rule: str = ""
    cap_round: int = 0

    @property
    def capped(self) -> bool:
        return bool(self.rule)

    @property
    def blockers(self) -> Tuple[int, ...]:
        return tuple(line.blocking_count for line in self.rounds)

    @property
    def total(self) -> int:
        return sum(self.blockers)

    def summary(self) -> str:
        """The rule, the round it fell in and the counts it fell on."""
        if not self.capped:
            return (
                f"PR #{self.pr_number} is below the cap: {len(self.rounds)} codex-seat round(s), "
                f"blockers {_counts(self.blockers)}, sum {self.total}"
            )
        at_cap = self.blockers[: self.cap_round]
        line = self.rounds[self.cap_round - 1]
        if self.rule == B3_1:
            why = f"round {self.cap_round} had {at_cap[-1]} blockers (B3.1: three or more in one round)"
        else:
            why = (
                f"round {self.cap_round} had {at_cap[-1]} blocker(s) and the sum over "
                f"{self.cap_round} rounds is {sum(at_cap)} (B3.2: six or more, latest round blocking)"
            )
        return (
            f"PR #{self.pr_number} is at the cap under {self.rule}: {why}; codex-seat blockers per "
            f"round {_counts(self.blockers)} on {len(self.rounds)} head(s), cap fell on head "
            f"{line.head_sha[:12]}"
        )

    def to_record(self) -> Dict[str, Any]:
        return {
            "rule": self.rule, "cap_round": self.cap_round, "rounds": len(self.rounds),
            "blockers": list(self.blockers), "total": self.total,
        }


def _counts(blockers: Sequence[int]) -> str:
    return "(" + ", ".join(str(count) for count in blockers) + ")" if blockers else "(none)"


def codex_rounds(lines: Iterable[RoundLine]) -> Tuple[RoundLine, ...]:
    """The rounds: codex-seat verdicts in verdict order, one per head (the first)."""
    seen: set = set()
    rounds: List[RoundLine] = []
    for line in lines:
        if line.seat != CODEX_SEAT or line.head_sha in seen:
            continue
        seen.add(line.head_sha)
        rounds.append(line)
    return tuple(rounds)


def cap_state(lines: Iterable[RoundLine], pr_number: int) -> CapState:
    """Walk the rounds in order; the first round where B3.1, else B3.2, holds is the cap.

    ``lines`` are one PR's lines in verdict order (:meth:`Ledger.for_pr`).
    """
    rounds = codex_rounds(lines)
    total = 0
    for index, line in enumerate(rounds, start=1):
        total += line.blocking_count
        if line.blocking_count >= B3_1_ROUND_BLOCKERS:
            return CapState(pr_number, rounds, B3_1, index)
        if line.blocking_count >= 1 and total >= B3_2_TOTAL_BLOCKERS:
            return CapState(pr_number, rounds, B3_2, index)
    return CapState(pr_number, rounds)


def round_granted_past_cap(cap: CapState, *, head_sha: str) -> bool:
    """Has a tiebreaker granted the codex seat one round on ``head_sha`` past the cap?

    This is where R2 plugs in (plan section 2): a ``verkleinen`` verdict grants
    one extra round, bound to the dispatch that carries out the named
    intervention, consumed by the first codex-seat verdict on a head with a
    commit of that dispatch. R1 has no tiebreaker and grants nothing: past the
    cap, only ``--override-b3`` lets a codex-seat round run (at the gate) or
    count (at the merge door).
    """
    return False


def cap_refusal_text(cap: CapState, *, where: str) -> str:
    """The refusal a capped PR gets, naming the rule, the counts and the override."""
    return (
        f"{where}: {cap.summary()}. Every codex-seat run on this PR is refused on every head; "
        f"the tiebreaker that decides the next step arrives with R2. To go on anyway, pass "
        f"{OVERRIDE_USAGE}: the override wins over the cap and is recorded with its reason"
    )


def override_record(reason: str, cap: CapState) -> Dict[str, Any]:
    """What an applied ``--override-b3`` records: the flag, the reason and the cap it won over."""
    return {"flag": OVERRIDE_FLAG, "reason": reason, **cap.to_record()}


# ---------------------------------------------------------------------------
# Writing a verdict
# ---------------------------------------------------------------------------


def append_verdict(path: Path, line: RoundLine) -> str:
    """Append ``line`` under the ledger lock, fsynced: ``"appended"`` or ``"present"``.

    ``"present"`` is the same verdict booked again (a retried write). A key that
    holds another verdict raises :class:`RoundRefusal` (``verdict_on_head``); a
    codex-seat verdict on a PR at the cap without an override or a granted round
    raises it too (``b3_cap``). A ledger that is unreadable for this PR raises
    :class:`RoundLedgerUnreadable`, any I/O failure :class:`RoundLedgerError`.
    """
    from atomic_io import slot_lock

    path = Path(path)
    try:
        with slot_lock(path):
            created = not path.exists()
            ledger = parse_ledger(path, path.read_text(encoding="utf-8") if not created else "")
            _check_append(ledger, line)
            if ledger.line_for(line.key) is not None:
                return "present"
            _append_line(path, line, created=created)
    except RoundLedgerError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise RoundLedgerError(f"round ledger {path} could not be written: {exc}") from exc
    return "appended"


def _check_append(ledger: Ledger, line: RoundLine) -> None:
    text = unreadable_text(ledger, line.pr_number)
    if text:
        raise RoundLedgerUnreadable(text)
    existing = ledger.line_for(line.key)
    if existing is not None:
        if existing.same_verdict(line):
            return
        raise RoundRefusal(REFUSED_VERDICT_ON_HEAD, (
            f"seat {line.seat} of PR #{line.pr_number} already has a {existing.verdict.upper()} "
            f"on head {line.head_sha[:12]} ({existing.reviewer}, {existing.report_name}); a second "
            f"verdict on the same head is refused"
        ))
    if line.seat != CODEX_SEAT or line.override:
        return
    cap = cap_state(ledger.for_pr(line.project_id, line.pr_number), line.pr_number)
    if cap.capped and not round_granted_past_cap(cap, head_sha=line.head_sha):
        raise RoundRefusal(REFUSED_B3_CAP, cap_refusal_text(cap, where="verdict not booked"))


def _append_line(path: Path, line: RoundLine, *, created: bool) -> None:
    data = json.dumps(line.to_dict(), sort_keys=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_ledger(review_gates_dir: Path) -> Path:
    """Create an empty ledger when the store has none, so the store counts rounds from now on."""
    from atomic_io import slot_lock

    path = ledger_path(review_gates_dir)
    if path.exists():
        return path
    with slot_lock(path):
        if not path.exists():
            path.touch()
            _fsync_dir(path.parent)
    return path


# ---------------------------------------------------------------------------
# A result payload as a ledger line
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerdictBooking:
    """What :func:`book_result_verdict` did with a result payload, and why."""

    outcome: str
    detail: str = ""
    line: Optional[RoundLine] = field(default=None, compare=False)


def blocking_count_of(payload: Dict[str, Any]) -> int:
    """Blockers of a verdict: its ``blocking_findings``, as the result receipt counts them."""
    findings = payload.get("blocking_findings")
    if isinstance(findings, list):
        return len(findings)
    count = payload.get("blocking_count")
    return count if _is_int(count) and count > 0 else 0


def payload_pr_number(payload: Dict[str, Any]) -> Optional[int]:
    value = payload.get("pr_number")
    if _is_int(value):
        return value
    text = str(payload.get("pr_id") or "").strip()
    return int(text) if text.isdigit() else None


def payload_seat(gate: str, payload: Dict[str, Any]) -> str:
    """The seat a verdict was given in: the seat its request stated, else the takeover origin, else its gate."""
    from review_seat_evaluation import seat_for

    return seat_for(gate, stated=str(payload.get("seat") or ""), takeover_path=payload.get("takeover_path"))


def ledger_verdict(payload: Dict[str, Any]) -> str:
    """``pass``/``fail`` for a verdict the ledger books, ``""`` for anything that is no verdict.

    A verdict carries its evidence (contract hash and report): a crash booked
    ``failed`` with an empty report is no verdict, and neither is a test run.
    """
    from gate_status import decided_verdict, has_evidence_fields, is_test_run_record

    if is_test_run_record(payload) or not has_evidence_fields(payload):
        return ""
    verdict = decided_verdict(payload)
    return verdict if verdict in _VERDICTS else ""


def line_from_payload(
    payload: Dict[str, Any], *, gate: str, project_id: str, source: str = SOURCE_GATE,
) -> Optional[RoundLine]:
    """The ledger line of a decided result payload; ``None`` when it is no verdict or names no head."""
    verdict, pr_number = ledger_verdict(payload), payload_pr_number(payload)
    head_sha = str(payload.get("commit_sha") or "").strip()
    if not verdict or pr_number is None or not head_sha:
        return None
    takeover_from = str(payload.get("takeover_from") or "") if payload.get("takeover") is True else ""
    override = payload.get("override_b3")
    return RoundLine(
        project_id=project_id, pr_number=pr_number, head_sha=head_sha,
        seat=payload_seat(gate, payload), reviewer=gate, takeover_from=takeover_from,
        verdict=verdict, blocking_count=blocking_count_of(payload),
        report_path=str(payload.get("report_path") or ""),
        dispatch_id=str(payload.get("dispatch_id") or ""),
        recorded_at=str(payload.get("recorded_at") or "") or _utc_now(),
        branch=str(payload.get("branch") or ""), source=source,
        override=override if isinstance(override, dict) and override else None,
    )


def _is_rewrite(payload: Dict[str, Any], on_disk: Optional[Dict[str, Any]]) -> bool:
    """A rewrite of the record that already carries this verdict: same report, same verdict."""
    if not isinstance(on_disk, dict):
        return False
    report = str(payload.get("report_path") or "")
    return bool(report) and str(on_disk.get("report_path") or "") == report and (
        ledger_verdict(on_disk) == ledger_verdict(payload)
    )


def book_result_verdict(
    result_path: Path, payload: Dict[str, Any], *, gate: str, on_disk: Optional[Dict[str, Any]],
) -> VerdictBooking:
    """Book the verdict a result writer is about to land, before it lands.

    Called by ``gate_recorder`` inside the result slot's lock, after the
    overwrite guard and before the record is written. Raises
    :class:`RoundRefusal` (the writer refuses the record) or
    :class:`RoundLedgerError` (the run fails); anything else is booked or
    skipped with the reason in the returned :class:`VerdictBooking`.
    """
    path = ledger_path_for_results(Path(result_path).parent)
    if path is None:
        return VerdictBooking("skipped", "the result is not in a review_gates/results store")
    # The verdict first: an outage or a note never reads the seat gates at all.
    if not ledger_verdict(payload) or gate not in ledger_seat_gates():
        return VerdictBooking("skipped", "no review-seat verdict")
    if _is_rewrite(payload, on_disk):
        return VerdictBooking("skipped", "a rewrite of a record that already carries this verdict")
    line = line_from_payload(payload, gate=gate, project_id=store_project_id(path.parent))
    if line is None:
        logger.warning(
            "review_rounds: %s verdict for pr=%s names no head (commit_sha empty): it cannot be "
            "counted per head and gets no ledger line; the merge door cannot use it either",
            gate, payload.get("pr_number") or payload.get("pr_id"),
        )
        return VerdictBooking("headless", "the verdict names no head")
    return VerdictBooking(append_verdict(path, line), line=line)
