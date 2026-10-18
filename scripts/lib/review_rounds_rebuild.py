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
  (``completed``) or FAIL (``failed``), and its ``blocking_count``;
- the result records ``review_gates/results/pr-N-<gate>.json``: the same facts
  for the latest run of each gate, for a run whose receipts never landed.

A report gets no line when no verdict was published for it (a write the guard
refused, a degenerate run), when its head cannot be established, and when its
seat already has an earlier verdict on that head: the first verdict on a head
stands, exactly as the gate now refuses the second run. Every report left out
is named with its reason.

Idempotent: a line already in the ledger is kept as it is (a live line knows
more than a rebuild can, such as an override), only keys that are missing are
added, so a second rebuild adds nothing. A line that does not parse and names
this PR is dropped; one that names no PR is moved to
``rounds.ndjson.quarantine`` beside the ledger, so it stops refusing every PR
and stays on disk as evidence.

BILLING SAFETY: no Anthropic SDK, no network, no subprocess.
"""
from __future__ import annotations

import json
import os
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
    pr_number: int
    ledger: Path
    added: Tuple[RoundLine, ...]
    kept: int
    skipped: Tuple[Skipped, ...]
    dropped: int
    quarantined: int
    cap: review_rounds.CapState

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pr_number": self.pr_number, "ledger": str(self.ledger),
            "added": [line.to_dict() for line in self.added], "kept": self.kept,
            "skipped": [{"report": item.report, "reason": item.reason} for item in self.skipped],
            "dropped_bad_lines": self.dropped, "quarantined_lines": self.quarantined,
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


def _seat_and_takeover(run: ReportRun, request: Dict[str, Any], record: Dict[str, Any]) -> Tuple[str, str]:
    from review_seat_evaluation import seat_for

    source = request or record
    stated = str(request.get("seat") or record.get("seat") or "")
    seat = seat_for(run.gate, stated=stated, takeover_path=source.get("takeover_path"))
    takeover_from = str(source.get("takeover_from") or "") if source.get("takeover") is True else ""
    return seat, takeover_from


def line_for_run(run: ReportRun, sources: StoreSources, *, project_id: str) -> Tuple[Optional[RoundLine], str]:
    """The ledger line of one report, or ``(None, why)`` when it gets none."""
    request = sources.requests.get(run.name, {})
    result = sources.results.get(run.name, {})
    record = sources.records.get(run.name, {})
    verdict, blocking, recorded_at = _published_verdict(run, result, record)
    if not verdict:
        return None, "no verdict was published for this report (no review_gate_result receipt, no result record)"
    head_sha = str(request.get("commit_sha") or record.get("commit_sha") or "").strip()
    if not head_sha:
        return None, "its head cannot be established (no review_gate_request receipt or result record names it)"
    seat, takeover_from = _seat_and_takeover(run, request, record)
    override = request.get("override_b3") or record.get("override_b3")
    return RoundLine(
        project_id=project_id, pr_number=run.pr_number, head_sha=head_sha, seat=seat, reviewer=run.gate,
        takeover_from=takeover_from, verdict=verdict,
        blocking_count=blocking if blocking is not None else 0,
        report_path=str(result.get("report_path") or record.get("report_path") or request.get("report_path") or run.name),
        dispatch_id=str(request.get("dispatch_id") or result.get("dispatch_id") or record.get("dispatch_id") or ""),
        recorded_at=recorded_at or run.generated, branch=str(request.get("branch") or run.branch),
        source=review_rounds.SOURCE_REBUILD, override=override if isinstance(override, dict) and override else None,
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
            f"seat {line.seat} already has a verdict on head {line.head_sha[:12]} ({first.report_name}): "
            "the first verdict on a head stands"
        )))
    return tuple(lines.values()), tuple(skipped)


# ---------------------------------------------------------------------------
# Into the ledger
# ---------------------------------------------------------------------------


def merge_into_ledger(path: Path, pr_number: int, project_id: str, lines: Iterable[RoundLine]) -> Tuple[Tuple[RoundLine, ...], int, int, int]:
    """Add the missing ``lines`` under the ledger lock: ``(added, kept, dropped, quarantined)``.

    Without a bad line the new lines are appended; with one, the ledger is
    rewritten atomically without it (this PR's bad lines dropped, lines that
    name no PR moved to the quarantine file).
    """
    from atomic_io import atomic_write_text, slot_lock

    path = Path(path)
    with slot_lock(path):
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        ledger = review_rounds.parse_ledger(path, text)
        present = {line.key for line in ledger.lines}
        added = tuple(line for line in lines if line.key not in present)
        kept = len(ledger.for_pr(project_id, pr_number))
        bad = ledger.unreadable_for(pr_number)
        if not bad:
            with path.open("a", encoding="utf-8") as handle:
                handle.write("".join(_ndjson(line) for line in added))
                handle.flush()
                os.fsync(handle.fileno())
            return added, kept, 0, 0
        raw = text.splitlines()
        dropped = {item.number for item in bad}
        orphans = [raw[item.number - 1] for item in bad if item.pr_number is None]
        if orphans:
            quarantine = path.with_name(path.name + QUARANTINE_SUFFIX)
            with quarantine.open("a", encoding="utf-8") as handle:
                handle.write("".join(f"{line}\n" for line in orphans))
                handle.flush()
                os.fsync(handle.fileno())
        kept_text = "".join(f"{line}\n" for number, line in enumerate(raw, start=1) if number not in dropped and line.strip())
        atomic_write_text(path, kept_text + "".join(_ndjson(line) for line in added))
    return added, kept, len(dropped) - len(orphans), len(orphans)


def _ndjson(line: RoundLine) -> str:
    return json.dumps(line.to_dict(), separators=(",", ":")) + "\n"


def rebuild(pr_number: int, *, data_dir: Path) -> RebuildResult:
    """Rebuild ``pr_number``'s lines in the ledger of the store at ``data_dir`` (its ``.vnx-data``)."""
    review_gates = Path(data_dir) / "state" / "review_gates"
    path = review_rounds.ledger_path(review_gates)
    project_id = review_rounds.store_project_id(review_gates)
    lines, skipped = rebuilt_lines(read_sources(data_dir, pr_number), project_id=project_id)
    added, kept, dropped, quarantined = merge_into_ledger(path, pr_number, project_id, lines)
    ledger = review_rounds.read_ledger(path)
    cap = review_rounds.cap_state(ledger.for_pr(project_id, pr_number), pr_number)
    return RebuildResult(pr_number, path, added, kept, skipped, dropped, quarantined, cap)
