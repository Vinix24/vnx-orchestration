"""Rebuild a PR's round-ledger lines from what its review runs left on disk (R1).

``vnx review rounds rebuild --pr N`` (``scripts/review_rounds_cli.py``). For a PR
reviewed before the ledger existed, and for a ledger that became unreadable.

What a line needs and where it comes from, all in the project's own store:

- the headless reports ``unified_reports/headless/*-HEADLESS-<gate>-pr-N-*.md``:
  every review run that wrote a report, with its gate, branch, time and
  blocker count. A report names neither the head it reviewed nor the seat it
  filled, so it is joined, on its file name, to:
- the ``review_gate_request`` receipts in ``state/t0_receipts.ndjson``: the
  head the run was requested against (``request.commit_sha``), the seat it
  filled (``request.seat``) and the takeover it was (``takeover_from``);
- the ``review_gate_result`` receipts: whether a verdict was published, PASS
  (``completed``) or FAIL (``failed``), and its ``blocking_count``; since R1
  also its ``head_sha``, ``seat``, ``takeover_from`` and ``override_b3``;
- the result records ``review_gates/results/pr-N-<gate>.json``: the same facts
  for the latest run of each gate, for a run whose receipts never landed.

Each fact is taken from the first source that has it, in this order: the head
from the result receipt, then the request, then the record; the seat from the
request, then the result receipt, then the record; the takeover from the
request, then the result receipt; the override from the request, then the
result receipt, then the record. The first receipt per report counts: a later
re-anchor writes a receipt for the same report with a new head, and that does
not move the line.

A report gets no line when no verdict was published for it (a write the guard
refused, a degenerate run), and when its seat already has an earlier verdict
on that head: the first verdict on a head stands, exactly as the gate now
refuses the second run. A published codex-seat verdict whose head cannot be
established is never dropped: it is booked on the head
``unresolved:<report name>``, so its blockers count toward B3 as a round of
their own, while it can never fill a seat or block a rerun, because no real
head matches it. A second-seat verdict without a head neither counts nor
fills, so it gets no line. Every report left out is named with its reason.

Idempotent: a line already in the ledger is kept as it is (a live line knows
more than a rebuild can, such as an override), only keys that are missing are
added, so a second rebuild adds nothing. Every line that does not parse and
may belong to this PR (its own, and every line that names no PR) is removed
from the ledger and moved to ``rounds.ndjson.quarantine`` beside it, so it
stops refusing and stays on disk as evidence. Last, the PR is opened in the
ledger (``basis`` ``rebuild``): from then on the gate and the merge door count
it.

BILLING SAFETY: no Anthropic SDK, no network, no subprocess.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import review_rounds
from review_rounds import RoundLine

QUARANTINE_SUFFIX = ".quarantine"
_HEADER_LINES = 60
_FIELD_RE = re.compile(r"^\*\*(PR|Branch|Gate|Generated)\*\*:\s*(.*?)\s*$")
_BLOCKING_RE = re.compile(r"^Blocking count:\s*(\d+)\s*$")
_RECEIPT_EVENTS = ("review_gate_request", "review_gate_result")


@dataclass(frozen=True)
class ReportRun:
    """One headless report: a review run that wrote one."""

    name: str
    gate: str
    pr_number: int
    branch: str
    generated: str
    blocking_count: Optional[int]


@dataclass(frozen=True)
class Skipped:
    report: str
    reason: str


@dataclass(frozen=True)
class StoreSources:
    """What the rebuild reads, keyed by report file name."""

    reports: Tuple[ReportRun, ...]
    requests: Dict[str, Dict[str, Any]]
    results: Dict[str, Dict[str, Any]]
    records: Dict[str, Dict[str, Any]]


@dataclass(frozen=True)
class RebuildResult:
    """What a rebuild did. ``dropped`` counts this PR's own bad lines, ``quarantined`` the ones
    that named no PR; both were removed from the ledger and moved to the quarantine file.
    ``opened`` is ``"opened"`` when the rebuild opened the PR, ``"present"`` when it already was."""

    pr_number: int
    ledger: Path
    added: Tuple[RoundLine, ...]
    kept: int
    skipped: Tuple[Skipped, ...]
    dropped: int
    quarantined: int
    cap: review_rounds.CapState
    opened: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pr_number": self.pr_number, "ledger": str(self.ledger),
            "added": [line.to_dict() for line in self.added], "kept": self.kept,
            "skipped": [{"report": item.report, "reason": item.reason} for item in self.skipped],
            "dropped_bad_lines": self.dropped, "quarantined_lines": self.quarantined,
            "opened": self.opened,
            "cap": {**self.cap.to_record(), "summary": self.cap.summary()},
        }


# ---------------------------------------------------------------------------
# Reading the sources
# ---------------------------------------------------------------------------


def parse_report_header(path: Path) -> Optional[ReportRun]:
    """The header facts of a headless report; ``None`` when it is not one."""
    fields: Dict[str, str] = {}
    blocking: Optional[int] = None
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle):
            if number >= _HEADER_LINES:
                break
            line = raw.rstrip("\n")
            match = _FIELD_RE.match(line)
            if match:
                fields.setdefault(match.group(1), match.group(2))
            count = _BLOCKING_RE.match(line)
            if count and blocking is None:
                blocking = int(count.group(1))
    pr_text = fields.get("PR", "")
    if not fields.get("Gate") or not pr_text.isdigit():
        return None
    return ReportRun(
        name=Path(path).name, gate=fields["Gate"], pr_number=int(pr_text),
        branch=fields.get("Branch", ""), generated=fields.get("Generated", ""), blocking_count=blocking,
    )


def list_reports(headless_dir: Path, pr_number: int, gates: Iterable[str]) -> Tuple[ReportRun, ...]:
    runs: List[ReportRun] = []
    for gate in sorted(gates):
        for path in sorted(Path(headless_dir).glob(f"*-HEADLESS-{gate}-pr-{pr_number}-*.md")):
            run = parse_report_header(path)
            if run is not None and run.gate == gate and run.pr_number == pr_number:
                runs.append(run)
    return tuple(runs)


def read_receipt_facts(receipts_path: Path, pr_number: int) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """``(requests, results)``: this PR's review-gate receipts, keyed by report file name."""
    requests: Dict[str, Dict[str, Any]] = {}
    results: Dict[str, Dict[str, Any]] = {}
    marker = f"-pr-{pr_number}-"
    if not Path(receipts_path).is_file():
        return requests, results
    with Path(receipts_path).open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if marker not in raw or "review_gate_re" not in raw:
                continue
            receipt = _json_object(raw)
            event = str(receipt.get("event_type") or receipt.get("event") or "")
            if event not in _RECEIPT_EVENTS:
                continue
            request = receipt.get("request") if isinstance(receipt.get("request"), dict) else {}
            name = Path(str(receipt.get("report_path") or request.get("report_path") or "")).name
            if not name or marker not in name:
                continue
            if event == "review_gate_request":
                requests.setdefault(name, {**request, "takeover_from": receipt.get("takeover_from") or request.get("takeover_from")})
            else:
                results.setdefault(name, receipt)
    return requests, results


def _json_object(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def read_result_records(results_dir: Path, pr_number: int, gates: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """The current result record of each gate for this PR, keyed by the report it names."""
    records: Dict[str, Dict[str, Any]] = {}
    for gate in sorted(gates):
        path = Path(results_dir) / f"pr-{pr_number}-{gate}.json"
        record = _json_object(path.read_text(encoding="utf-8")) if path.is_file() else {}
        name = Path(str(record.get("report_path") or "")).name
        if name:
            records[name] = record
    return records


def read_sources(data_dir: Path, pr_number: int) -> StoreSources:
    """Everything the rebuild reads for one PR from one project store (``.vnx-data``)."""
    gates = review_rounds.ledger_seat_gates()
    state = Path(data_dir) / "state"
    requests, results = read_receipt_facts(state / "t0_receipts.ndjson", pr_number)
    return StoreSources(
        reports=list_reports(Path(data_dir) / "unified_reports" / "headless", pr_number, gates),
        requests=requests, results=results,
        records=read_result_records(state / "review_gates" / "results", pr_number, gates),
    )


# ---------------------------------------------------------------------------
# One report as a line
# ---------------------------------------------------------------------------


def _published_verdict(run: ReportRun, result: Dict[str, Any], record: Dict[str, Any]) -> Tuple[str, Optional[int], str]:
    """``(verdict, blocking_count, recorded_at)`` of the verdict published for ``run``; verdict ``""`` when none was."""
    status = str(result.get("status") or "")
    if status in ("completed", "failed"):
        count = result.get("blocking_count")
        count = count if isinstance(count, int) and not isinstance(count, bool) else run.blocking_count
        return ("pass" if status == "completed" else "fail"), count, str(result.get("timestamp") or "")
    verdict = review_rounds.ledger_verdict(record) if record else ""
    if verdict:
        return verdict, review_rounds.blocking_count_of(record), str(record.get("recorded_at") or "")
    return "", None, ""


def _first_text(*values: Any) -> str:
    """The first value that is a non-empty text, stripped; ``""`` when none is."""
    return next((str(value).strip() for value in values if isinstance(value, str) and value.strip()), "")


def _takeover_from(source: Dict[str, Any]) -> str:
    """The seat a request or record took over, only when it says it is a takeover."""
    return str(source.get("takeover_from") or "") if source.get("takeover") is True else ""


def _seat_and_takeover(
    run: ReportRun, request: Dict[str, Any], result: Dict[str, Any], record: Dict[str, Any],
) -> Tuple[str, str]:
    """The seat (request, result receipt, record) and the takeover origin (request, result receipt, record)."""
    from review_seat_evaluation import seat_for

    source = request or record
    stated = _first_text(request.get("seat"), result.get("seat"), record.get("seat"))
    seat = seat_for(run.gate, stated=stated, takeover_path=source.get("takeover_path"))
    takeover_from = _first_text(_takeover_from(request), result.get("takeover_from"), _takeover_from(record))
    return seat, takeover_from


def _override(*sources: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The first ``override_b3`` record among ``sources`` (request, result receipt, record)."""
    return next(
        (source["override_b3"] for source in sources if isinstance(source.get("override_b3"), dict) and source["override_b3"]),
        None,
    )


def line_for_run(run: ReportRun, sources: StoreSources, *, project_id: str) -> Tuple[Optional[RoundLine], str]:
    """The ledger line of one report, or ``(None, why)`` when it gets none.

    A published codex-seat verdict always gets a line: without an established
    head it is booked on ``unresolved:<report name>`` (it counts toward B3 and
    never fills a seat). A second-seat verdict without a head gets none.
    """
    request = sources.requests.get(run.name, {})
    result = sources.results.get(run.name, {})
    record = sources.records.get(run.name, {})
    verdict, blocking, recorded_at = _published_verdict(run, result, record)
    if not verdict:
        return None, "no verdict was published for this report (no review_gate_result receipt, no result record)"
    seat, takeover_from = _seat_and_takeover(run, request, result, record)
    head_sha = _first_text(result.get("head_sha"), request.get("commit_sha"), record.get("commit_sha"))
    if not head_sha:
        if seat != review_rounds.CODEX_SEAT:
            return None, (
                f"its head cannot be established (no review_gate_result or review_gate_request receipt or result "
                f"record names it) and it sat in seat {seat}: a second-seat verdict without a head neither counts nor fills"
            )
        head_sha = review_rounds.unresolved_head(run.name)
    override = _override(request, result, record)
    return RoundLine(
        project_id=project_id, pr_number=run.pr_number, head_sha=head_sha, seat=seat, reviewer=run.gate,
        takeover_from=takeover_from, verdict=verdict,
        blocking_count=blocking if blocking is not None else 0,
        report_path=str(result.get("report_path") or record.get("report_path") or request.get("report_path") or run.name),
        dispatch_id=str(request.get("dispatch_id") or result.get("dispatch_id") or record.get("dispatch_id") or ""),
        recorded_at=recorded_at or run.generated, branch=str(request.get("branch") or run.branch),
        source=review_rounds.SOURCE_REBUILD, override=override,
    ), ""


def rebuilt_lines(sources: StoreSources, *, project_id: str) -> Tuple[Tuple[RoundLine, ...], Tuple[Skipped, ...]]:
    """One line per (head, seat), the first verdict in time; every report left out, with why."""
    candidates: List[Tuple[str, str, RoundLine]] = []
    skipped: List[Skipped] = []
    for run in sources.reports:
        line, why = line_for_run(run, sources, project_id=project_id)
        if line is None:
            skipped.append(Skipped(run.name, why))
        else:
            candidates.append((line.recorded_at, run.name, line))
    lines: Dict[Tuple[str, int, str, str], RoundLine] = {}
    for _at, name, line in sorted(candidates, key=lambda item: item[:2]):
        first = lines.get(line.key)
        if first is None:
            lines[line.key] = line
            continue
        skipped.append(Skipped(name, (
            f"seat {line.seat} already has a verdict on head {review_rounds.short_head(line.head_sha)} "
            f"({first.report_name}): the first verdict on a head stands"
        )))
    return tuple(lines.values()), tuple(skipped)


# ---------------------------------------------------------------------------
# Into the ledger
# ---------------------------------------------------------------------------


def quarantine_path(path: Path) -> Path:
    """``rounds.ndjson.quarantine`` beside the ledger: where every bad line the rebuild removes goes."""
    return Path(path).with_name(Path(path).name + QUARANTINE_SUFFIX)


def merge_into_ledger(path: Path, pr_number: int, project_id: str, lines: Iterable[RoundLine]) -> Tuple[Tuple[RoundLine, ...], int, int, int]:
    """Add the missing ``lines`` under the ledger lock: ``(added, kept, dropped, quarantined)``.

    A line is missing when neither its key nor its report is in the ledger yet:
    one report is one verdict, so a report the gate already booked on its real
    head is never booked a second time (on ``unresolved:<report>``, when the
    receipt that names its head never landed).

    Without a bad line for this PR the new lines are appended at a record
    boundary (``review_rounds._append_text``); with one, every bad line that
    may belong to this PR (its own and every line that names no PR) is moved
    to the quarantine file first, and the ledger is rewritten atomically
    without them. ``dropped`` counts the PR's own, ``quarantined`` the ones
    that named no PR; both are in the quarantine file.
    """
    from atomic_io import atomic_write_text, slot_lock

    path = Path(path)
    with slot_lock(path):
        text = review_rounds._ledger_text(path)
        ledger = review_rounds.parse_ledger(path, text)
        present = {line.key for line in ledger.lines}
        booked = {line.report_name for line in ledger.for_pr(project_id, pr_number) if line.report_name}
        added = tuple(line for line in lines if line.key not in present and line.report_name not in booked)
        kept = len(ledger.for_pr(project_id, pr_number))
        bad = ledger.unreadable_for(pr_number)
        new_text = "".join(review_rounds.ndjson(line.to_dict()) for line in added)
        if not bad:
            review_rounds._append_text(path, new_text)
            return added, kept, 0, 0
        raw = text.splitlines()
        removed = {item.number for item in bad}
        review_rounds._append_text(quarantine_path(path), "".join(f"{raw[number - 1]}\n" for number in sorted(removed)))
        kept_text = "".join(f"{line}\n" for number, line in enumerate(raw, start=1) if number not in removed and line.strip())
        atomic_write_text(path, kept_text + new_text)
    orphans = sum(1 for item in bad if item.pr_number is None)
    return added, kept, len(removed) - orphans, orphans


def rebuild(pr_number: int, *, data_dir: Path) -> RebuildResult:
    """Rebuild ``pr_number``'s lines in the ledger of the store at ``data_dir`` (its ``.vnx-data``), then open the PR."""
    review_gates = Path(data_dir) / "state" / "review_gates"
    path = review_rounds.ledger_path(review_gates)
    project_id = review_rounds.store_project_id(review_gates)
    sources = read_sources(data_dir, pr_number)
    lines, skipped = rebuilt_lines(sources, project_id=project_id)
    added, kept, dropped, quarantined = merge_into_ledger(path, pr_number, project_id, lines)
    opened = review_rounds.open_pr(
        path, project_id, pr_number, basis=review_rounds.BASIS_REBUILD, evidence=len(sources.reports),
    )
    ledger = review_rounds.read_ledger(path)
    cap = review_rounds.cap_state(ledger.for_pr(project_id, pr_number), pr_number)
    return RebuildResult(pr_number, path, added, kept, skipped, dropped, quarantined, cap, opened)
