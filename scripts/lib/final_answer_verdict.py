"""A harness seat's verdict from its final answer, when its report carries none (R6, OI-2039).

Measured 2026-10-15 on seven deepseek_gate runs (PRs 2065, 2066, 2069, 2070 and
2071): the model wrote ``unified_reports/<dispatch_id>.md`` itself, without a
verdict block, and gave its complete verdict in its final answer.
provider_dispatch keeps a report the worker wrote, so the runner read that file
and booked ``unavailable / validation_failed / no_verdict_block``. The plan-gate
tiebreaker lost a START the same way on 2026-10-12. The final answer was never
lost: it is the ``result`` event of the run's archived event stream,
``events/archive/plan-gate/<dispatch_id>.ndjson``, whose ``data.text`` ends in
the verdict block.

This module reads that event. It decides nothing about the verdict: each
surface hands in its own parser (PR-review seats
``codex_parser.extract_verdict_block``, plan-gate seats
``plan_gate_panel.parse_verdict``, the tiebreaker
``plan_gate_tiebreaker.parse_tiebreaker``), so a PR-review reader never reads a
plan verdict and the reverse. There is no free-text extraction.

What it checks, in this order:

1. **Binding.** The archive is the one named after the run's dispatch id
   (``event_store.resolve_archive_file``: an exact name, never a substring
   search or a glob). Every event in it carries that dispatch id and the seat
   terminal, and it holds exactly one ``result`` event. A PR-review seat also
   names the head sha of its run, and a run without one is refused. The archive
   carries no head sha itself (measured), so the head comes from the run's own
   request, the same in-memory request that holds the dispatch id the runner
   minted for that run. A report text that already carries a recovered-verdict
   marker is refused too: the runner writes that marker only into the evidence
   report it composes, never into a lane's own report, so such a text is not
   this run's lane report.
2. **Failed-run precedence.** Only a run that ended normally gets the fallback.
   A failed, deadline-cut or timed-out run keeps its booking, whatever its
   final answer says. The signals, and where each comes from:

   - the lane's report text: no ``SYNTHESIZED_REPORT_MARKER`` (governance wrote
     the body because the worker never did), no deadline mark
     (``gate_lane_contract.lane_deadline_detail``, which provider_dispatch puts
     on a report the worker already wrote since R7), no ``failure_reason`` in its
     frontmatter, and an ``exit_code`` in its frontmatter that is 0 or absent. A
     report the model wrote itself has no frontmatter, so absent is the
     measured shape of a normal run there;
   - the ``result`` event: ``subtype`` is ``"success"`` and ``is_error`` is not
     true. The normalized event carries ``is_error`` only from this change on
     (``subprocess_adapter``); archives written before it have none;
   - the runner: a dispatch that raised, a timeout included, is booked before
     any caller reaches this module.
3. **The surface's parser.** It reads a verdict from the final answer, or
   nothing is recovered and the booking stays.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import event_store
from gate_lane_contract import lane_deadline_detail
from unified_report_schema import parse_frontmatter

# The terminal every harness seat is dispatched under: provider_dispatch gets
# ``--terminal-id plan-gate`` from plan_gate_panel._make_default_dispatcher, and
# the claude seat runs with plan_gate_panel._PANEL_TERMINAL_ID. A test pins the
# two to this name.
HARNESS_SEAT_TERMINAL = "plan-gate"

VERDICT_SOURCE_REPORT = "report"
VERDICT_SOURCE_FINAL_ANSWER = "final_answer"

RECOVERED = "recovered"
UNAVAILABLE = "unavailable"   # no final answer to read: no archive, no result event, empty text
NO_VERDICT = "no_verdict"     # the run ended normally, its final answer holds no verdict the parser reads
REFUSED = "refused"           # the run is refused: failed-run precedence or binding

_MARKER_PREFIX = "<!-- vnx-final-answer-verdict"
_MARKER_LINE_RE = re.compile(r"^<!-- vnx-final-answer-verdict\b.*-->[ \t]*$", re.MULTILINE)
RECOVERED_SECTION_HEADING = "## Verdict recovered from the final answer"


@dataclass(frozen=True)
class FinalAnswerVerdict:
    """What the fallback found for one run.

    ``verdict`` is what the surface's parser returned, set only when
    ``outcome`` is ``recovered``. ``detail`` says why, in one line, for every
    outcome; it never carries the final answer's own text.
    """

    outcome: str
    detail: str
    verdict: Any = None
    dispatch_id: str = ""
    head_sha: str = ""
    archive_path: Optional[Path] = None

    @property
    def recovered(self) -> bool:
        return self.outcome == RECOVERED

    @property
    def refused(self) -> bool:
        return self.outcome == REFUSED

    def note(self) -> str:
        return f"final answer {self.outcome}: {self.detail}"


def recover_verdict(
    data_dir: Optional[Path],
    dispatch_id: str,
    *,
    report_text: str,
    parse: Callable[[str], Any],
    head_sha: Optional[str] = None,
    terminal: str = HARNESS_SEAT_TERMINAL,
) -> FinalAnswerVerdict:
    """Read the verdict of *dispatch_id*'s final answer with *parse*, or say why not.

    *data_dir* is the data dir the run's lane wrote under; the archive is
    ``<data_dir>/events/archive/<terminal>/<dispatch_id>.ndjson`` (or
    ``.ndjson.gz``). *report_text* is the report the lane handed back for this
    run. *parse* returns the surface's verdict or None. *head_sha* is None for a
    surface that reviews no commit (a plan seat, the tiebreaker); a PR-review
    seat passes the head of its run, and an empty one is refused.
    """
    def _result(outcome: str, detail: str, **extra: Any) -> FinalAnswerVerdict:
        return FinalAnswerVerdict(
            outcome=outcome, detail=detail, dispatch_id=dispatch_id,
            head_sha=(head_sha or "").strip(), **extra,
        )

    if data_dir is None or not _is_single_name(dispatch_id):
        return _result(UNAVAILABLE, f"no archive can be located for dispatch id {dispatch_id!r}")
    if head_sha is not None and not head_sha.strip():
        return _result(REFUSED, "the run names no head sha, so a recovered verdict would bind to no commit")
    refusal = _report_refusal(report_text)
    if refusal:
        return _result(REFUSED, refusal)
    archive = event_store.resolve_archive_file(Path(data_dir) / "events" / "archive" / terminal, dispatch_id)
    if archive is None:
        return _result(UNAVAILABLE, f"no archived event stream for {dispatch_id}")
    data, outcome, detail = _read_result_event(archive, dispatch_id, terminal)
    if data is None:
        return _result(outcome, detail, archive_path=archive)
    refusal = _event_refusal(data)
    if refusal:
        return _result(REFUSED, refusal, archive_path=archive)
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return _result(UNAVAILABLE, "the result event carries an empty final answer", archive_path=archive)
    verdict = parse(text)
    if verdict is None:
        return _result(
            NO_VERDICT, "the final answer carries no verdict block this surface's parser reads",
            archive_path=archive,
        )
    return _result(RECOVERED, f"verdict read from the result event in {archive.name}",
                   verdict=verdict, archive_path=archive)


def _is_single_name(dispatch_id: str) -> bool:
    """A dispatch id that names one file in the archive directory and nothing else."""
    value = (dispatch_id or "").strip()
    return bool(value) and value == dispatch_id and value not in (".", "..") \
        and "/" not in value and "\\" not in value and "\x00" not in value


def _report_refusal(report_text: str) -> str:
    """Why the lane's own report says this run did not end normally, or ""."""
    text = report_text or ""
    if _MARKER_LINE_RE.search(text):
        return (
            "the report already carries a recovered-verdict marker; the runner never writes one "
            "into a lane's own report, so this text is not this run's report"
        )
    if _synthesized_marker() in text:
        return "governance synthesized the report: the worker never wrote one"
    # Unreadable frontmatter carries no mark: the single reader's own rule
    # (lane_deadline_detail). It catches SchemaViolation only, and yaml can
    # raise a plain ValueError (a timestamp such as 2026-13-45).
    try:
        deadline = lane_deadline_detail(text)
    except (ValueError, RecursionError):
        deadline = None
    if deadline is not None:
        return f"the lane's own deadline cut the run off: {deadline}"
    try:
        frontmatter = parse_frontmatter(text)
    except (ValueError, RecursionError):
        return ""
    reason = str(frontmatter.get("failure_reason") or "").strip()
    if reason:
        return "the report's frontmatter carries a failure_reason"
    exit_code = frontmatter.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return f"the report's frontmatter carries exit_code={exit_code}"
    return ""


def _synthesized_marker() -> str:
    """dispatch_govern's marker, imported so the writer and this reader never drift."""
    try:
        from dispatch_govern import SYNTHESIZED_REPORT_MARKER  # noqa: PLC0415
    except Exception:  # vnx-silent-except: the same literal plan_gate_panel falls back to
        return "VNX_SYNTHESIZED_REPORT_BY_GOVERNANCE"
    return SYNTHESIZED_REPORT_MARKER


def _read_result_event(
    archive: Path, dispatch_id: str, terminal: str,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """``(data, outcome, detail)`` for the one ``result`` event in *archive*.

    ``data`` is the event's data object, set only when the archive is bound to
    *dispatch_id* and holds exactly one result event; ``outcome`` and
    ``detail`` then stay empty. Otherwise ``data`` is None and ``outcome``
    says why. A line that is not JSON is a torn write and carries no event; an
    event of another dispatch or terminal refuses the whole archive.
    """
    results = []
    try:
        with event_store.open_archive_text(archive) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                foreign = _foreign_binding(event, dispatch_id, terminal)
                if foreign:
                    return None, REFUSED, f"{archive.name}: {foreign}"
                if event.get("type") == "result":
                    results.append(event)
    except (OSError, EOFError) as exc:
        return None, UNAVAILABLE, f"{archive.name} is unreadable ({type(exc).__name__})"
    if not results:
        return None, UNAVAILABLE, f"{archive.name} holds no result event"
    if len(results) > 1:
        return None, REFUSED, f"{archive.name} holds {len(results)} result events; the final answer is ambiguous"
    event = results[0]
    if event.get("dispatch_id") != dispatch_id or event.get("terminal") != terminal:
        return None, REFUSED, f"{archive.name}: the result event is not bound to {dispatch_id} on {terminal}"
    data = event.get("data")
    if not isinstance(data, dict):
        return None, UNAVAILABLE, f"{archive.name}: the result event carries no data object"
    return data, "", ""


def _foreign_binding(event: Dict[str, Any], dispatch_id: str, terminal: str) -> str:
    """Why *event* belongs to another dispatch or terminal, or ""."""
    event_id = event.get("dispatch_id")
    if event_id is not None and event_id != dispatch_id:
        return f"an event of dispatch {str(event_id)[:120]!r}, not {dispatch_id}"
    event_terminal = event.get("terminal")
    if event_terminal is not None and event_terminal != terminal:
        return f"an event of terminal {str(event_terminal)[:60]!r}, not {terminal}"
    return ""


def _event_refusal(data: Dict[str, Any]) -> str:
    """Why the result event says this run did not end normally, or ""."""
    subtype = data.get("subtype")
    if subtype != "success":
        return f"the run did not end normally: result subtype {str(subtype)[:60]!r}"
    if data.get("is_error") is True:
        return "the run did not end normally: the result event has is_error true"
    return ""


# A fenced block with a label: the opening fence line, then everything up to the
# closing fence line or the end of the text (an unclosed fence runs to the end).
_LABELLED_FENCE_RE = re.compile(
    r"^[ \t]*```[ \t]*(?P<label>[^\s`]+)[^\n]*\n.*?(?:^[ \t]*```[^\n]*$|\Z)",
    re.DOTALL | re.MULTILINE,
)


def without_foreign_fences(text: str, keep: Tuple[str, ...] = ("json",)) -> str:
    """*text* with every fenced block whose label is not in *keep* left out.

    For the PR-review surface, whose reader (``extract_verdict_block``) reads a
    written JSON object anywhere, fenced or bare. A final answer that holds a
    ``vnx-plan-verdict`` or ``vnx-plan-tiebreak`` block carries an object with a
    ``"verdict"`` of ``pass`` in it; read as is, a PR-review seat would book a
    plan verdict. A block in a fence labelled anything but json is another
    surface's block or quoted code, so it is not read. An unlabelled fence and
    a json fence are kept, so the reader sees them exactly as before.
    """
    allowed = {label.lower() for label in keep}
    return _LABELLED_FENCE_RE.sub(
        lambda match: match.group(0) if match.group("label").lower() in allowed else "",
        text or "",
    )


def _marker_line(dispatch_id: str, head_sha: str) -> str:
    return f"{_MARKER_PREFIX} dispatch_id={dispatch_id} head={head_sha} -->"


def recovered_section(recovery: FinalAnswerVerdict, verdict_block: Dict[str, Any]) -> str:
    """The section a PR-review evidence report gains when its verdict came from the final answer.

    Its verdict is a fenced ```json block holding *verdict_block* as the
    surface's parser read it, so the merge door's agreement check and the forge
    publisher's re-read find the same verdict as the record. A backtick inside a
    string is written as ``\\u0060``: the same JSON, and no string can close the
    fence early.
    """
    body = json.dumps(verdict_block, indent=2, ensure_ascii=False).replace("`", "\\u0060")
    archive = recovery.archive_path.name if recovery.archive_path is not None else ""
    return "\n".join([
        RECOVERED_SECTION_HEADING,
        "",
        _marker_line(recovery.dispatch_id, recovery.head_sha),
        "",
        "The report this run wrote carries no verdict block. The block below is the verdict "
        "from the run's final answer, the `result` event of its archived event stream, "
        "recovered by the runner (R6, OI-2039). The run's own report above is unchanged.",
        "",
        f"- dispatch_id: `{recovery.dispatch_id}`",
        f"- head: `{recovery.head_sha}`",
        f"- archive: `{archive}`",
        "",
        "```json",
        body,
        "```",
        "",
    ])


def with_recovered_section(report_text: str, section: str) -> str:
    """*report_text* with *section* appended once.

    Idempotent: a text that already carries this exact section is returned
    unchanged, so a second booking of the same dispatch never appends a second
    block. The text before the section is never changed.
    """
    if section in report_text:
        return report_text
    return report_text.rstrip("\n") + "\n\n" + section
