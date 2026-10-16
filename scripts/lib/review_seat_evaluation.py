"""Which reviewer fills which review seat on one PR head (OI-2058 merge side, reparatiegolf R5b).

R5a (``review_seats``) decides which seats a review needs and gives every seat
its own reviewer when the review is requested. This module is the other half:
for one PR head it answers which record fills each seat, which seats are still
unfilled (and which reviewers could fill them), and whether any reviewer on the
head blocks. Every evaluator asks this module and none of them derives a seat on
its own:

- the merge door: ``closure_verifier.check_review_gate_for_merge``, called by
  ``pr_merge._run_review_gate``;
- the summary check ``vnx-gate/review``: ``forge_gate_publisher.review_verdict``;
- ``vnx pr-ready``: ``pr_readiness.collect_gate_evidence``;
- track closure of a PR merged under this rule:
  ``closure_verifier._validate_review_evidence``;
- ``t0_gate_enforcement`` (``gate_enforcement_verify``) and the governance
  enforcer, through :func:`seat_for`; the enforcer also asks
  :func:`rejections_on_head` before it credits a seat.

**Which seats are required.** The project's configured stack: the
``review_seats`` that a request on this head recorded with ``source=config``
(the latest one), else a fresh ``review_seats.resolve_review_seats()`` for the
project. A request with ``source=explicit`` never narrows that set. The T0
requests one seat at a time (``--review-stack codex_gate``, then
``--review-stack kimi_gate``), and each of those records names one seat. When
the recorded stack and a fresh resolution differ, the recorded one wins (plan
acceptance (b)) and the evaluation says so in ``note``.

**Which seat a record answers for** (:func:`seat_for`). The ``seat`` its own
request on this head states (stamped since R5a), else ``takeover_path[0]``
(the seat the takeover walk started from), else its own gate.
``takeover_from`` is never read: it names the last hop, not the seat. One
record answers for one seat, so a takeover record never also fills its own
gate's seat.

**When a seat is filled.** A completed record on this exact head with zero
blocking findings and complete evidence (``contract_hash``, ``report_path``,
the report on disk, no contradiction), judged by the merge door's own chain
(``closure_verifier._merge_door_record_verdict``). The evidence routes are the
closure verifier's own: the seat's own record, a successor that answers for
the seat, the OI-1719 obligation booking, and the OI-1624/OI-1642 independent
peer for a confirmed absence (also after an OI-1851 partial review, which
itself fills no seat). A reviewer fills at most one seat: the booking and the
peer route only accept a reviewer that no other seat counts. Every route hands
back the exact record it accepted (``evidence_record``), and that record is
the seat's evidence (:class:`SeatFill`): track closure, ``vnx-gate/review`` and
``vnx pr-ready`` never look the reviewer up a second time.

**Blocking always blocks.** A record on the head from a review peer rejects
it when its decided verdict is FAIL, or when its report on disk carries a FAIL
verdict or a blocking finding, whatever its JSON says (:func:`report_rejects`:
invariant 7 of the merge door, read with the door's own reader). That holds for
every review peer, inside or outside the required seats, the declared gate
included, and a rejection refuses the merge whatever the other seats say.
Every in-scope record of a peer on the head is read for it, not only the one
that fills a seat (OI-2113: an outage in the contract-named file hid a FAIL in
the legacy file beside it). A record that is only incomplete (no report on
disk) is no rejection: it fills no seat and does not block on its own.

BILLING SAFETY: no Anthropic SDK, no network, no subprocess. Only the standard
library is imported at module level: ``gate_enforcement_verify`` is copied into
the enforcement sandbox with this one module beside it. Everything the merge
side needs (``closure_verifier``, ``gate_status``, ``review_seats``,
``config_runtime``) is imported inside the function that uses it.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REVIEW_STACK_KEY = "VNX_DEFAULT_REVIEW_STACK"
REVIEW_STACK_OVERRIDE_KEY = "VNX_OVERRIDE_DEFAULT_REVIEW_STACK"
TAKEOVER_CHAIN_KEY = "VNX_REVIEW_GATE_TAKEOVER_CHAIN"

#: The rule a merge record was judged under. Its presence on a ``pr_merged``
#: receipt is how track closure tells a PR merged under the seat rule from one
#: merged before it (``closure_verifier._validate_review_evidence``).
SEAT_RULE = "every_seat_distinct_reviewer"

SOURCE_RECORDED = "config"
SOURCE_FRESH = "config_fresh"
SOURCE_EXPLICIT = "explicit"
SOURCE_MERGE_RECORD = "merge_record"

ROUTE_DIRECT = "direct"
ROUTE_TAKEOVER = "takeover"
ROUTE_BOOKING = "takeover_booking"
ROUTE_PEER = "independent_peer"

HOW_STATED = "stated"
HOW_TAKEOVER = "takeover_path"
HOW_OWN = "own_gate"
HOW_UNKNOWN = "unknown"

_GLOB_UNSAFE_CHARS = frozenset("*?[]")


# ---------------------------------------------------------------------------
# Record -> seat. Standard library only: gate_enforcement_verify uses these.
# ---------------------------------------------------------------------------


def takeover_origin(takeover_path: Any) -> str:
    """The seat a takeover walk started from: the gate of its first hop, or ``""``.

    The first hop that names a gate, the same hop list ``gate_enforcement_verify``
    reads. A later hop is a reviewer the walk passed over, never the seat.
    """
    if not isinstance(takeover_path, list):
        return ""
    for hop in takeover_path:
        if isinstance(hop, dict) and hop.get("gate"):
            return str(hop["gate"]).strip()
    return ""


def seat_for(gate: str, *, stated: str = "", takeover_path: Any = None) -> str:
    """The one seat a reviewer's record answers for.

    ``stated`` is the ``seat`` its own request states (R5a); it wins. Without
    one, ``takeover_path[0]`` names the seat a takeover read, and a record that
    is neither answers for its own gate.
    """
    stated = (stated or "").strip()
    if stated:
        return stated
    return takeover_origin(takeover_path) or gate


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def requests_dir_for(results_dir: Path) -> Path:
    """``review_gates/requests`` beside ``review_gates/results``: the slot the request handler writes."""
    return Path(results_dir).parent / "requests"


def on_head(record: Dict[str, Any], head_sha: Optional[str]) -> bool:
    """Three answers, as ``closure_verifier._matches_required_field``: ``None`` asks for no head,
    ``""`` is a head nobody could resolve (nothing matches), a value must be the record's ``commit_sha``."""
    if head_sha is None:
        return True
    return bool(head_sha) and str(record.get("commit_sha") or "").strip() == head_sha


def _pr_matches(record: Dict[str, Any], pr_id: str) -> bool:
    """A request names its PR by ``pr_id`` or ``pr_number``; the file name already does.

    A record that names a PR must name this one by at least one of the two.
    """
    named = [str(record[key]) for key in ("pr_id", "pr_number") if record.get(key) not in (None, "")]
    return not named or pr_id in named


def _request_paths(requests_dir: Path, pr_id: str, gate: str) -> List[Path]:
    slug = pr_id.lower().replace("-", "")
    return [requests_dir / f"{slug}-{gate}-contract.json", requests_dir / f"pr-{pr_id}-{gate}.json"]


def read_request(requests_dir: Path, pr_id: str, gate: str) -> Optional[Dict[str, Any]]:
    """The request record of ``gate`` for ``pr_id`` (either file convention), or ``None``.

    An offline test run is no request on this PR, read with the fabric's one
    test-run predicate (``gate_status.is_test_run_record``, OI-2113).
    """
    from gate_status import is_test_run_record

    for path in _request_paths(Path(requests_dir), pr_id, gate):
        if not path.is_file():
            continue
        record = _load_json(path)
        if record is None or is_test_run_record(record):
            continue
        if (record.get("gate") or gate) == gate and _pr_matches(record, pr_id):
            return record
    return None


def stated_seat(requests_dir: Path, pr_id: str, gate: str, head_sha: Optional[str]) -> str:
    """The ``seat`` ``gate``'s own request states, when that request is about this head; else ``""``."""
    request = read_request(requests_dir, pr_id, gate)
    if request is None or not on_head(request, head_sha):
        return ""
    return str(request.get("seat") or "").strip()


# ---------------------------------------------------------------------------
# Which seats a merge needs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequiredSeats:
    """The seats a merge of this head needs, where they came from, and why when there are none.

    ``fresh`` is a fresh resolution next to a recorded stack (only filled in when
    a recorded stack was found), so the merge record can say that the two differ.
    ``error`` is the refusal text when no seat list can be established at all.
    """

    seats: Tuple[str, ...]
    source: str
    value: str = ""
    fresh: Optional[Tuple[str, ...]] = None
    note: str = ""
    error: str = ""


def _seat_names(raw: Any) -> Tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    names: List[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _pr_request_files(requests_dir: Path, pr_id: str) -> List[Path]:
    if not pr_id or any(ch in _GLOB_UNSAFE_CHARS for ch in pr_id) or not requests_dir.is_dir():
        return []
    slug = pr_id.lower().replace("-", "")
    found = set(requests_dir.glob(f"pr-{pr_id}-*.json")) | set(requests_dir.glob(f"{slug}-*-contract.json"))
    return sorted(found)


def recorded_config_seats(requests_dir: Path, pr_id: str, head_sha: Optional[str]) -> Optional[Dict[str, Any]]:
    """The ``review_seats`` (source ``config``) a request on this head recorded; the latest wins.

    ``None`` when no request on this head recorded the project's stack, for
    instance when every request named its stack explicitly. An offline test
    run records nothing (``gate_status.is_test_run_record``, OI-2113): read
    with a narrower marker, ``test_run: "TRUE"`` narrowed two seats to one.
    """
    from gate_status import is_test_run_record

    best: Optional[Dict[str, Any]] = None
    best_at = ""
    for path in _pr_request_files(Path(requests_dir), pr_id):
        request = _load_json(path)
        if request is None or is_test_run_record(request) or not on_head(request, head_sha):
            continue
        record = request.get("review_seats")
        if not _pr_matches(request, pr_id) or not isinstance(record, dict):
            continue
        seats = _seat_names(record.get("seats"))
        if record.get("source") != SOURCE_RECORDED or not seats:
            continue
        requested_at = str(request.get("requested_at") or "")
        if best is None or requested_at > best_at:
            best, best_at = dict(record, seats=list(seats)), requested_at
    return best


def fresh_config_seats() -> Tuple[Optional[Tuple[str, ...]], str, str]:
    """``(seats, value, refusal)`` from a fresh ``resolve_review_seats()`` for this process's project."""
    from review_seats import ReviewSeatError, resolve_review_seats

    try:
        resolved = resolve_review_seats()
    except ReviewSeatError as exc:
        return None, "", str(exc)
    return resolved.seats, resolved.value, ""


def required_seats(
    requests_dir: Path,
    pr_id: str,
    head_sha: Optional[str],
    *,
    seats: Optional[Sequence[str]] = None,
    seats_source: str = SOURCE_EXPLICIT,
) -> RequiredSeats:
    """The seats a merge of this head needs (see the module docstring for the order)."""
    if seats is not None:
        named = _seat_names(list(seats))
        if not named:
            return RequiredSeats((), seats_source, error=f"de opgegeven seat-lijst ({seats_source}) is leeg")
        return RequiredSeats(named, seats_source, value=",".join(named))
    recorded = recorded_config_seats(Path(requests_dir), pr_id, head_sha)
    fresh, value, refusal = fresh_config_seats()
    if recorded is None:
        if fresh is None:
            return RequiredSeats((), SOURCE_FRESH, error=refusal)
        return RequiredSeats(fresh, SOURCE_FRESH, value=value)
    kept = tuple(recorded["seats"])
    return RequiredSeats(
        kept, SOURCE_RECORDED, value=str(recorded.get("value") or ""), fresh=fresh,
        note=_recorded_note(kept, fresh, refusal),
    )


def _recorded_note(kept: Tuple[str, ...], fresh: Optional[Tuple[str, ...]], refusal: str) -> str:
    if fresh is None:
        return (
            f"de geboekte stack ({', '.join(kept)}) geldt; een verse resolutie van "
            f"{REVIEW_STACK_KEY} weigert nu: {refusal}"
        )
    if fresh != kept:
        return (
            f"de geboekte stack ({', '.join(kept)}) geldt boven een verse resolutie "
            f"({', '.join(fresh)}): de review is aangevraagd tegen de geboekte stack (plan, acceptatie (b))"
        )
    return ""


# ---------------------------------------------------------------------------
# The reviewers on the head, each with its one seat
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeatScope:
    """The head the evaluation is about, in the closure verifier's own scope terms."""

    pr_id: str
    results_dir: Path
    branch: Optional[str] = None
    project_id: Optional[str] = None
    head_sha: Optional[str] = None

    @property
    def kwargs(self) -> Dict[str, Optional[str]]:
        return {"branch": self.branch, "project_id": self.project_id, "head_sha": self.head_sha}


@dataclass(frozen=True)
class SeatedRecord:
    """One review peer's record on the head and the seat it answers for (``""``: undeterminable)."""

    gate: str
    seat: str
    how: str
    record: Dict[str, Any]


def reviewer_gates(seats: Sequence[str]) -> Tuple[str, ...]:
    """The gates whose records the evaluation reads: every review peer, plus every required seat.

    A seat named explicitly (``seats=``) is read through its own record even
    when it is no review peer, so an explicit seat list means what it says.
    """
    from review_seats import review_peer_gates

    return tuple(sorted(set(review_peer_gates()) | set(seats)))


def stated_seats_on_head(scope: SeatScope, gates: Sequence[str]) -> Dict[str, str]:
    """Reviewer gate -> the seat its own request on this head states."""
    requests_dir = requests_dir_for(scope.results_dir)
    stated: Dict[str, str] = {}
    for gate in gates:
        seat = stated_seat(requests_dir, scope.pr_id, gate, scope.head_sha)
        if seat:
            stated[gate] = seat
    return stated


def _seated(gate: str, record: Dict[str, Any], stated: str) -> SeatedRecord:
    if stated:
        return SeatedRecord(gate, stated, HOW_STATED, record)
    origin = takeover_origin(record.get("takeover_path"))
    if origin:
        return SeatedRecord(gate, origin, HOW_TAKEOVER, record)
    if record.get("takeover") is True:
        # Claims a takeover and does not say of which seat: it answers for none.
        return SeatedRecord(gate, "", HOW_UNKNOWN, record)
    return SeatedRecord(gate, gate, HOW_OWN, record)


def records_on_head(
    scope: SeatScope, stated: Dict[str, str], gates: Sequence[str],
) -> Dict[str, Tuple[SeatedRecord, ...]]:
    """Every reviewer's in-scope records on the head, keyed by gate, each with its one seat.

    Read through ``closure_verifier._find_gate_results``, in the order the merge
    door has always read them (the contract record first). The first record of
    a gate is the one that may fill a seat (:func:`seated_records`); every one of
    them can reject the head (:func:`blocking_records`, OI-2113). One reader for
    both, so the scope rules (this PR, this head, no test run) cannot drift
    between the record that fills a seat and the records that reject.
    """
    import closure_verifier as cv

    found: Dict[str, Tuple[SeatedRecord, ...]] = {}
    for gate in gates:
        records = cv._find_gate_results(gate, scope.pr_id, scope.results_dir, **scope.kwargs)
        if records:
            found[gate] = tuple(_seated(gate, record, stated.get(gate, "")) for record in records)
    return found


def seated_records(on_head: Dict[str, Tuple[SeatedRecord, ...]]) -> Dict[str, SeatedRecord]:
    """Per gate, the record that may fill a seat: its first in-scope record.

    That is the record ``closure_verifier._find_gate_result`` returns for the
    gate: the slot the merge door has always judged.
    """
    return {gate: records[0] for gate, records in on_head.items()}


@dataclass(frozen=True)
class Rejection:
    """One review peer on the head that rejected it, the seat it answered for, and how it said so.

    ``contradicted`` is True when the record's JSON did not say FAIL and its
    report on disk did (a FAIL verdict or a blocking finding): invariant 7 of
    the merge door, read for every reviewer and not only for one that fills a seat.
    """

    gate: str
    seat: str
    status: str
    contradicted: bool = False

    def text(self) -> str:
        if self.contradicted:
            return (
                f"{self.gate} (reviewer voor seat {self.seat or 'onbekend'}) zegt {self.status!r} in zijn "
                "JSON, maar zijn rapport op schijf draagt een FAIL-verdict of een blokkerende bevinding: "
                "het rapport spreekt de JSON tegen, en een afkeuring blokkeert de merge, ook naast een "
                "pass op een andere seat"
            )
        where = f" (reviewer voor seat {self.seat})" if self.seat and self.seat != self.gate else ""
        return (
            f"{self.gate}{where} keurde deze kop af ({self.status!r}, blokkerende bevinding): een afkeuring "
            "blokkeert de merge, ook naast een pass op een andere seat"
        )


def report_rejects(record: Dict[str, Any]) -> bool:
    """Does the report on disk of a record that reviewed carry a FAIL verdict or a blocking finding?

    Read with the merge door's own reader (``closure_verifier._report_blocking_indicators``),
    whatever the record's JSON says. Only a record that reviewed is read (status
    pass, fail or partial review): an outage or a gate that never ran has no
    review to contradict. A record without a report on disk, or with one that
    cannot be read, is incomplete, never a rejection: it fills no seat and does
    not block on its own.
    """
    import closure_verifier as cv
    from gate_status import FAIL_STATES, PARTIAL_REVIEW_STATES, PASS_STATES, canonical_status

    if canonical_status(record) not in PASS_STATES | FAIL_STATES | PARTIAL_REVIEW_STATES:
        return False
    report_path = record.get("report_path")
    if not isinstance(report_path, str) or not report_path.strip():
        return False
    indicators = cv._report_blocking_indicators(Path(report_path.strip()))
    return bool(indicators)


def blocking_records(on_head: Dict[str, Tuple[SeatedRecord, ...]]) -> Tuple[Rejection, ...]:
    """Every review peer on the head that rejected it, in its JSON or in its report: blocking always blocks.

    Every in-scope record of a peer is read, not only the one that fills a
    seat (OI-2113): an outage in ``<n>-<gate>-contract.json`` hid a FAIL in
    ``pr-<n>-<gate>.json`` beside it, and the door said GO. A peer is named
    once, by the first of its records that rejects.
    """
    from review_seats import review_peer_gates

    peers = review_peer_gates()
    found: List[Rejection] = []
    for gate, records in sorted(on_head.items()):
        if gate not in peers:
            continue
        rejection = next((hit for hit in map(_rejection, records) if hit is not None), None)
        if rejection is not None:
            found.append(rejection)
    return tuple(found)


def _rejection(entry: SeatedRecord) -> Optional[Rejection]:
    """How one record rejects the head (its JSON, or its report against its JSON), or ``None``."""
    from gate_status import canonical_status, decided_verdict

    status = canonical_status(entry.record)
    if decided_verdict(entry.record) == "fail":
        return Rejection(entry.gate, entry.seat, status)
    if report_rejects(entry.record):
        return Rejection(entry.gate, entry.seat, status, contradicted=True)
    return None


def rejections_on_head(
    pr_id: str,
    results_dir: Path,
    *,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> Tuple[Rejection, ...]:
    """The rejection scan :func:`evaluate_review_seats` runs, for an evaluator that judges one seat.

    The governance enforcer's ``<gate>_required`` checks judge one seat, not
    the head. They ask this so they never credit a seat on a head the merge
    door refuses because a review peer rejected it (OI-2113). Same reader,
    same scope arguments, same peers as the door.
    """
    scope = SeatScope(str(pr_id), Path(results_dir), branch, project_id, head_sha)
    gates = reviewer_gates(())
    return blocking_records(records_on_head(scope, stated_seats_on_head(scope, gates), gates))


# ---------------------------------------------------------------------------
# Filling the seats
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeatFill:
    """One required seat: filled or not, by whom, through which route, and the route's own words.

    ``spoke`` says whether a reviewer answering for this seat rendered a
    decided verdict on the head. A seat that is unfilled and has not spoken is
    waiting (``action_required``); one that spoke and is still unfilled was
    judged and did not hold (``failure``).

    ``record`` is the seat's evidence. For a filled seat it is the exact record
    the route accepted, so track closure checks the contract hash and the
    report of that record (``closure_verifier._seat_rule_checks``), and
    ``vnx-gate/review`` and ``vnx pr-ready`` read it too. For an unfilled seat
    it is the record that answered for the seat and did not hold; ``None`` when
    no record answered for the seat at all.
    """

    seat: str
    filled: bool
    reviewer: str
    route: str
    message: str
    spoke: bool
    could_fill: Tuple[str, ...] = ()
    evidence_via: str = ""
    record: Optional[Dict[str, Any]] = field(default=None, compare=False, repr=False)


def _excluded(seat: str, seats: Sequence[str], seated: Dict[str, SeatedRecord], taken: Dict[str, str]) -> frozenset:
    """Reviewers that may not sign for ``seat``: one that fills, or answers for, another required seat."""
    counted = {reviewer for other, reviewer in taken.items() if other != seat}
    answering = {gate for gate, entry in seated.items() if entry.seat in seats and entry.seat != seat}
    return frozenset(counted | answering)


def _own_for_seat(seat: str, seated: Dict[str, SeatedRecord]) -> Optional[Dict[str, Any]]:
    entry = seated.get(seat)
    return entry.record if entry is not None and entry.seat == seat else None


def _reviewer_of(seat: str, verdict: Dict[str, Any]) -> str:
    return str(verdict.get("evidence_gate") or seat)


def _claimed_round(scope: SeatScope, seats: Sequence[str], seated, stated) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    """Phase 1: the seat's own record and the successors that answer for it."""
    import closure_verifier as cv

    settled: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for seat in seats:
        verdict = cv._seat_claimed_verdict(
            seat, scope.pr_id, scope.results_dir, own=_own_for_seat(seat, seated),
            stated_seats=stated, **scope.kwargs,
        )
        if verdict is not None:
            route = ROUTE_TAKEOVER if verdict.get("evidence_gate") else ROUTE_DIRECT
            settled[seat] = (route, verdict)
    return settled


def _booking_round(scope, seats, seated, settled) -> None:
    """Phase 2: the OI-1719 obligation booking, for a reviewer no other seat counts."""
    import closure_verifier as cv

    for seat in seats:
        if seat in settled:
            continue
        exclude = _excluded(seat, seats, seated, _taken(settled))
        verdict = cv._consult_obligation_takeover_booking(
            seat, scope.pr_id, scope.results_dir, exclude=exclude, **scope.kwargs,
        )
        if verdict is not None:
            settled[seat] = (ROUTE_BOOKING, verdict)


def _absence_round(scope, seats, seated, settled) -> None:
    """Phase 3: a partial review, a confirmed absence (independent peer), or nothing at all."""
    import closure_verifier as cv

    for seat in seats:
        if seat in settled:
            continue
        own = seated.get(seat)
        if own is not None and own.how == HOW_UNKNOWN:
            settled[seat] = (ROUTE_PEER, {
                "verdict": "NO-GO", "gate": seat,
                "message": (
                    f"{seat} claimt een overname maar draagt een lege of afwezige takeover_path: "
                    "voor welke seat het record spreekt is niet vast te stellen, dus het vult er geen"
                ),
            })
            continue
        verdict = cv._seat_absence_verdict(
            seat, scope.pr_id, scope.results_dir,
            own_record=own.record if own is not None and own.seat == seat else None,
            read_other_seat=own.seat if own is not None and own.seat != seat else "",
            exclude=_excluded(seat, seats, seated, _taken(settled)), **scope.kwargs,
        )
        settled[seat] = (ROUTE_PEER, verdict)


def _taken(settled: Dict[str, Tuple[str, Dict[str, Any]]]) -> Dict[str, str]:
    return {
        seat: _reviewer_of(seat, verdict)
        for seat, (_route, verdict) in settled.items()
        if verdict.get("verdict") == "GO"
    }


def _distinct(seats: Sequence[str], settled) -> None:
    """A reviewer that the routes counted for two seats fills only the first of them."""
    holder: Dict[str, str] = {}
    for seat in seats:
        route, verdict = settled[seat]
        if verdict.get("verdict") != "GO":
            continue
        reviewer = _reviewer_of(seat, verdict)
        if reviewer in holder:
            settled[seat] = (route, {
                "verdict": "NO-GO", "gate": seat,
                "message": (
                    f"{reviewer} vult al seat {holder[reviewer]}: een reviewer vult één seat, "
                    f"dus seat {seat} heeft geen eigen reviewer"
                ),
            })
            continue
        holder[reviewer] = seat


def _spoke(seat: str, seated: Dict[str, SeatedRecord], filled: bool) -> bool:
    from gate_status import decided_verdict

    if filled:
        return True
    return any(entry.seat == seat and decided_verdict(entry.record) for entry in seated.values())


def _answering_record(
    seat: str, verdict: Dict[str, Any], seated: Dict[str, SeatedRecord],
) -> Optional[Dict[str, Any]]:
    """The seat's evidence (see :class:`SeatFill`).

    A filled seat: the record its route accepted, carried beside the GO
    (``evidence_record``, ``closure_verifier._accepted``), never a second lookup
    of the reviewer's slot. That slot can hold another record than the one a
    booking pointed at. An unfilled seat: of the records that answer for it, one
    that rendered a verdict first, the seat gate's own first.
    """
    from gate_status import decided_verdict

    if verdict.get("verdict") == "GO":
        return verdict["evidence_record"]
    answering = sorted(
        (entry for entry in seated.values() if entry.seat == seat),
        key=lambda entry: (not decided_verdict(entry.record), entry.gate != seat, entry.gate),
    )
    return answering[0].record if answering else None


def fill_seats(scope: SeatScope, seats: Sequence[str], seated, stated) -> Tuple[SeatFill, ...]:
    """Fill every seat through the evidence routes, in three rounds, one reviewer per seat."""
    settled = _claimed_round(scope, seats, seated, stated)
    _booking_round(scope, seats, seated, settled)
    _absence_round(scope, seats, seated, settled)
    _distinct(seats, settled)
    taken = _taken(settled)
    fills: List[SeatFill] = []
    for seat in seats:
        route, verdict = settled[seat]
        filled = verdict.get("verdict") == "GO"
        reviewer = _reviewer_of(seat, verdict) if filled else ""
        fills.append(SeatFill(
            seat=seat, filled=filled, reviewer=reviewer,
            route=route if filled else "", message=str(verdict.get("message") or ""),
            spoke=_spoke(seat, seated, filled), evidence_via=str(verdict.get("evidence_via") or ""),
            could_fill=() if filled else could_fill(seat, _excluded(seat, seats, seated, taken)),
            record=_answering_record(seat, verdict, seated),
        ))
    return tuple(fills)


def _report_fallback(function: str, exc: Exception, fallback: str) -> None:
    """One stderr line for a failed read, before its fallback is used: a failure is never swallowed.

    The line names the function, the exception type and its message (folded
    onto the one line), and what the fallback means for the refusal text.
    """
    message = " ".join(str(exc).split())
    print(
        f"[review_seat_evaluation] {function}: {type(exc).__name__}: {message}. Terugval: {fallback}",
        file=sys.stderr,
    )


def _takeover_chain() -> List[str]:
    """The operator's takeover chain, in order (fresh, as the request side reads it).

    The chain only orders a hint in a refusal text; the refusal never depends
    on it. A failed read is reported on stderr and the chain is then empty.
    """
    try:
        import config_runtime

        raw = config_runtime.get(TAKEOVER_CHAIN_KEY) or ""
    except Exception as exc:
        _report_fallback("_takeover_chain", exc, (
            f"{TAKEOVER_CHAIN_KEY} niet gelezen; de weigering noemt per open seat alleen de seat "
            "zelf als mogelijke vuller, zonder opvolgers uit de overnameketen"
        ))
        raw = ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def could_fill(seat: str, excluded: frozenset) -> Tuple[str, ...]:
    """The reviewers that could still fill ``seat``: the seat itself, then its chain successors,
    minus every reviewer another seat already counts."""
    chain = _takeover_chain()
    order = [seat] + (chain[chain.index(seat) + 1:] if seat in chain else [])
    return tuple(gate for gate in order if gate not in excluded)


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeatEvaluation:
    """The seat answer for one PR head: required seats, their fills, and what blocks."""

    scope: SeatScope
    required: RequiredSeats
    fills: Tuple[SeatFill, ...]
    blocking: Tuple[Rejection, ...]
    hints: Dict[str, str]

    @property
    def seats(self) -> Tuple[str, ...]:
        return self.required.seats

    @property
    def unfilled(self) -> Tuple[SeatFill, ...]:
        return tuple(fill for fill in self.fills if not fill.filled)

    @property
    def go(self) -> bool:
        return bool(self.seats) and not self.required.error and not self.blocking and not self.unfilled

    def fill_for(self, seat: str) -> Optional[SeatFill]:
        return next((fill for fill in self.fills if fill.seat == seat), None)

    def message(self) -> str:
        if self.required.error:
            return f"de vereiste review-seats zijn niet vast te stellen: {self.required.error}"
        parts = [rejection.text() for rejection in self.blocking]
        parts += [_unfilled_text(fill, self.hints.get(fill.seat, "")) for fill in self.unfilled]
        if not parts:
            parts = [_filled_text(self)]
        if self.required.note:
            parts.append(self.required.note)
        return "; ".join(parts)

    def to_record(self) -> Dict[str, Any]:
        """The shape the merge record carries (``review_seats`` on the ``pr_merged`` receipt)."""
        return {
            "rule": SEAT_RULE,
            "pr_id": self.scope.pr_id,
            "head_sha": self.scope.head_sha or "",
            "seats": list(self.seats),
            "source": self.required.source,
            "value": self.required.value,
            "fresh_seats": list(self.required.fresh) if self.required.fresh is not None else None,
            "note": self.required.note,
            "filled_by": {fill.seat: fill.reviewer for fill in self.fills if fill.filled},
            "routes": {fill.seat: fill.route for fill in self.fills if fill.filled},
            "unfilled": [fill.seat for fill in self.unfilled],
            "blocking": [rejection.gate for rejection in self.blocking],
            "verdict": "GO" if self.go else "NO-GO",
        }


def _unfilled_text(fill: SeatFill, hint: str) -> str:
    text = f"seat {fill.seat}: {fill.message}"
    if hint:
        text += f" — {hint}"
    if fill.could_fill:
        text += f" — kan nog gevuld worden door: {', '.join(fill.could_fill)}"
    return text


def _filled_text(evaluation: SeatEvaluation) -> str:
    if len(evaluation.fills) == 1:
        return evaluation.fills[0].message
    filled = ", ".join(
        f"{fill.seat}={fill.reviewer}" + ("" if fill.route == ROUTE_DIRECT else f" ({fill.route})")
        for fill in evaluation.fills
    )
    return (
        f"elke seat van de review-stack ({evaluation.required.source}: {', '.join(evaluation.seats)}) "
        f"is gevuld door een eigen reviewer op deze kop: {filled}"
    )


def _family_of(gate: str) -> str:
    """The provider family the refusal's ``vnx providers on <family>`` names.

    A failed lookup is reported on stderr; the gate's bare name stands in.
    """
    try:
        import provider_switch

        return provider_switch.family_of(gate)
    except Exception as exc:
        family = gate[:-5] if gate.endswith("_gate") else gate
        _report_fallback("_family_of", exc, (
            f"de weigering noemt `vnx providers on {family}`, afgeleid uit de kale poortnaam {gate}"
        ))
        return family


def _switch_text(seat: str, gate: str, detail: str) -> str:
    family = _family_of(gate)
    return (
        f"{gate} staat uit via de provider-schakelaar ({detail or 'provider_switched_off'}) en telt "
        f"nooit als pass. Twee uitwegen: `vnx providers on {family}`, of een project-override van "
        f"{REVIEW_STACK_KEY} die seat {seat} niet noemt"
    )


def _chain_exhausted(scope: SeatScope, seat: str) -> Optional[Dict[str, Any]]:
    path = Path(scope.results_dir) / f"pr-{scope.pr_id}-{seat}-chain-exhausted.json"
    return _load_json(path) if path.is_file() else None


def _unfilled_hint(scope: SeatScope, seat: str, seated: Dict[str, SeatedRecord]) -> str:
    """The chain and switch facts a refusal for an unfilled seat names."""
    from provider_switch import REASON_SWITCHED_OFF as PROVIDER_SWITCHED_OFF_REASON

    parts: List[str] = []
    exhausted = _chain_exhausted(scope, seat)
    if exhausted is not None:
        tried = ", ".join(str(gate) for gate in exhausted.get("reviewers_tried") or []) or "onbekend"
        parts.append(f"de overnameketen is uitgeput ({exhausted.get('reason') or 'chain_exhausted'}), geprobeerd: {tried}")
    switched = []
    own = seated.get(seat)
    if own is not None and own.record.get("reason") == PROVIDER_SWITCHED_OFF_REASON:
        switched.append((seat, str(own.record.get("reason_detail") or "")))
    for hop in (exhausted or {}).get("takeover_path") or []:
        if isinstance(hop, dict) and hop.get("reason") == PROVIDER_SWITCHED_OFF_REASON and hop.get("gate"):
            switched.append((str(hop["gate"]), str(hop.get("detail") or "")))
    seen = set()
    for gate, detail in switched:
        if gate not in seen:
            seen.add(gate)
            parts.append(_switch_text(seat, gate, detail))
    return "; ".join(parts)


def evaluate_review_seats(
    pr_id: str,
    results_dir: Path,
    *,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    head_sha: Optional[str] = None,
    seats: Optional[Sequence[str]] = None,
    seats_source: str = SOURCE_EXPLICIT,
) -> SeatEvaluation:
    """The seat answer for ``pr_id`` on ``head_sha``: the one function every evaluator calls.

    ``seats`` names the required seats directly (track closure passes the merge
    record's list, ``seats_source`` says where it came from); ``None`` resolves
    them as the module docstring describes. ``branch``/``project_id``/``head_sha``
    are the closure verifier's scope arguments, with the same three-way meaning.
    """
    scope = SeatScope(str(pr_id), Path(results_dir), branch, project_id, head_sha)
    required = required_seats(
        requests_dir_for(scope.results_dir), scope.pr_id, head_sha, seats=seats, seats_source=seats_source,
    )
    gates = reviewer_gates(required.seats)
    stated = stated_seats_on_head(scope, gates)
    on_head = records_on_head(scope, stated, gates)
    seated = seated_records(on_head)
    fills = fill_seats(scope, required.seats, seated, stated) if required.seats else ()
    hints = {fill.seat: _unfilled_hint(scope, fill.seat, seated) for fill in fills if not fill.filled}
    return SeatEvaluation(scope, required, fills, blocking_records(on_head), hints)
