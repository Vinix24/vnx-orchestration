#!/usr/bin/env python3
"""verification_runs.py — which test run in a report's Verification section counts.

fabric-state-herstel D1b. ``ReportParser.extract_validation`` used to take the
FIRST ``N pass`` and the FIRST ``N fail`` it found in the section. The report
contract asks a worker to show the red run on the old code before the green
run on its head, so for exactly the reports that follow the contract the red
run's failures became the receipt's count: measured on 29-09, 37 pytest
receipts in three days carried ``status=success`` with ``tests_failed > 0``.

A run is one test-runner summary:

- pytest: a clause of ``N passed`` / ``N failed`` terms on one line, optionally
  with ``errors``/``skipped``/``deselected``/``xfailed``/``xpassed``/``warnings``
  (``4 failed, 47 deselected in 0.71s``, ``12 failed, 8 passed``,
  ``124 passed / 1 skipped``, the older ``5 tests passed``). Errors count as
  failed. Matching is case-sensitive: a noun (``2 failures``) or ``2 PASSes``
  is prose, not a run.
- unittest: ``Ran N tests in Xs`` followed within a few lines by ``OK`` or
  ``FAILED (failures=a, errors=b, skipped=c)``. ``Ran N tests`` without a
  result line is no run.

The count on the receipt is the LAST run that is not marked red. A run is red
when the text before it on its own line, or else the label it sits under,
says so: red/rood/rode, old code/oude code, old head/oude kop, unfixed,
before/without the fix, before the change, baseline, mutation, on/op main.
A red marker later in the run's own sentence marks that run too. A label is a heading or a bold
lead line inside the section, or the first line of a paragraph or list item
(with no run on it) that carries a red or green marker; a heading or bold
lead without a marker resets to neutral. A continuation line in the middle
of a paragraph ("... re-ran to confirm green again") and code-fence content
never relabel. When every run is red, the last
red run is the count, so a report with only a red run keeps
``tests_failed > 0`` and can never be accepted.

``verification_record`` turns that reading into the ADR-035 ``verification{}``
shape. Both write paths (``report_parser._build_enhanced_receipt`` and
``envelope_govern_support._verification_from_report``) call it, so one report
yields one verification on either path. A gate-runner dispatch
(``kimi-gate-pr<N>-<ts>``) is review evidence for another dispatch's PR, not a
work run: its reviewer's own test runs are not that PR's verification, so it
gets ``method="gate_evidence"`` and no counts. ``compute_verdict`` and
``receipt_outcome`` are unchanged; the ledger is append-only (ADR-005) and old
receipts keep what they were written with.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from receipt_outcome import GATE_DISPATCH_RE

GATE_EVIDENCE_METHOD = "gate_evidence"

_RED_MARKER = re.compile(
    r"\b(?:red|rood|rode|old code|oude code|old head|oude kop|unfixed|pre-fix"
    r"|before the fix|voor de fix|without the fix|zonder de fix"
    r"|before the change|voor de wijziging|baseline"
    r"|mutation|mutatie|on main|op main|on origin/main|op origin/main)\b",
    re.IGNORECASE,
)
_GREEN_MARKER = re.compile(
    r"\b(?:green|groen|groene|after the fix|na de fix|new code|nieuwe code"
    r"|new head|nieuwe kop|final|finale)\b",
    re.IGNORECASE,
)

# Case-sensitive on purpose: runners print lowercase counts, while prose like
# "the 2 PASSes on old code" is not a run.
_TERM = re.compile(
    r"(\d+)\s+(?:tests?\s+)?"
    r"(passed|pass|failed|fail|errors|error"
    r"|skipped|deselected|xfailed|xpassed|warnings|warning)\b",
)
# What may sit between two terms of one summary clause: separators, emphasis,
# "and"/"en". Anything else ends the clause.
_TERM_GAP = re.compile(r"^[\s,;/*`+]*(?:(?:and|en)\b[\s,;/*`+]*)?$", re.IGNORECASE)
_PASS_WORDS = frozenset({"passed", "pass"})
_FAIL_WORDS = frozenset({"failed", "fail", "errors", "error"})
_SENTENCE_END = re.compile(r"\.(?:\s|$)|$")

_HEADING = re.compile(r"^\s*#{1,6}\s")
_BOLD_LEAD = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)?\*\*")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_UNITTEST_RAN = re.compile(r"\bRan (\d+) tests? in [\d.]+\s*s\b")
_UNITTEST_OK = re.compile(r"^\s*OK\b(?:\s*\(([^)]*)\))?")
_UNITTEST_FAILED = re.compile(r"^\s*FAILED\s*\(([^)]*)\)")
_UNITTEST_RESULT_WINDOW = 5


def _marker_color(text: str) -> Optional[str]:
    """``red``/``green`` for the LAST marker in ``text``, None without one."""
    plain = text.replace("`", "").replace("*", "")
    last_red = max((m.end() for m in _RED_MARKER.finditer(plain)), default=-1)
    last_green = max((m.end() for m in _GREEN_MARKER.finditer(plain)), default=-1)
    if last_red < 0 and last_green < 0:
        return None
    return "red" if last_red > last_green else "green"


def _pytest_clauses(line: str) -> List[Dict[str, Any]]:
    """Summary clauses on one line, each with its start offset and counts."""
    clauses: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for m in _TERM.finditer(line):
        word = m.group(2).lower()
        if current is not None and _TERM_GAP.match(line[current["end"]:m.start()]):
            current["end"] = m.end()
        else:
            current = {"start": m.start(), "end": m.end(), "passed": 0, "failed": 0,
                       "has_outcome": False}
            clauses.append(current)
        if word in _PASS_WORDS:
            current["passed"] += int(m.group(1))
            current["has_outcome"] = True
        elif word in _FAIL_WORDS:
            current["failed"] += int(m.group(1))
            current["has_outcome"] = current["has_outcome"] or word not in ("errors", "error")
    return [c for c in clauses if c["has_outcome"]]


def _unittest_counts(ran: int, result: str, failed_form: bool) -> Dict[str, int]:
    fields = {k.strip().lower(): int(v) for k, v in re.findall(r"(\w+)\s*=\s*(\d+)", result or "")}
    failed = fields.get("failures", 0) + fields.get("errors", 0) if failed_form else 0
    passed = max(0, ran - failed - fields.get("skipped", 0) - fields.get("expected failures", 0))
    return {"passed": passed, "failed": failed}


def extract_runs(section: str) -> List[Dict[str, Any]]:
    """Every test run in ``section``, in order, with ``passed``, ``failed``,
    ``method`` (``pytest``/``unittest``) and ``color`` (``red``/``green``/None)."""
    lines = section.splitlines()
    runs: List[Dict[str, Any]] = []
    context: Optional[str] = None
    in_fence = False
    paragraph_start = True
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            paragraph_start = not in_fence
            continue
        is_lead = paragraph_start or bool(_LIST_ITEM.match(line))
        paragraph_start = not line.strip() and not in_fence
        found: List[Dict[str, Any]] = [
            {**c, "method": "pytest"} for c in _pytest_clauses(line)
        ]
        ran = _UNITTEST_RAN.search(line)
        if ran:
            for follow in lines[index + 1:index + 1 + _UNITTEST_RESULT_WINDOW]:
                ok, failed = _UNITTEST_OK.match(follow), _UNITTEST_FAILED.match(follow)
                if ok or failed:
                    counts = _unittest_counts(int(ran.group(1)), (ok or failed).group(1),
                                              failed_form=failed is not None)
                    found.append({"start": ran.start(), "end": ran.end(), **counts,
                                  "method": "unittest"})
                    break
        if not found:
            if not in_fence:
                color = _marker_color(line)
                if _HEADING.match(line) or _BOLD_LEAD.match(line):
                    context = color
                elif color is not None and is_lead:
                    context = color
            continue
        found.sort(key=lambda r: r["start"])
        line_color: Optional[str] = None
        previous_end = 0
        for run in found:
            color = _marker_color(line[previous_end:run["start"]])
            if color is not None:
                line_color = color
            # "7 failed, 6 passed on the old code": a red marker in the rest
            # of the run's own sentence marks that run, and only that run.
            tail = line[run["end"]:]
            tail = tail[:_SENTENCE_END.search(tail).start()]
            own = "red" if _marker_color(tail) == "red" else None
            runs.append({"passed": run["passed"], "failed": run["failed"],
                         "method": run["method"], "color": own or line_color or context})
            previous_end = run["end"]
    return runs


def final_run(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The last run not marked red; the last red run when every run is red."""
    not_red = [r for r in runs if r["color"] != "red"]
    if not_red:
        return not_red[-1]
    return runs[-1] if runs else None


def is_gate_runner(dispatch_id: Any) -> bool:
    return bool(GATE_DISPATCH_RE.match(str(dispatch_id or "").strip()))


def verification_record(validation: Dict[str, Any],
                        dispatch_id: Any = None) -> Dict[str, Any]:
    """ADR-035 ``verification{}`` from ``extract_validation`` output."""
    tests_passed = int(validation.get("tests_passed") or 0)
    tests_failed = int(validation.get("tests_failed") or 0)
    tests_run = tests_passed + tests_failed
    if is_gate_runner(dispatch_id):
        method = GATE_EVIDENCE_METHOD
        tests_run = 0
    elif tests_run > 0:
        method = validation.get("test_method") or "pytest"
    elif validation.get("quality_gates"):
        method = "manual"
    else:
        method = "unknown"
    return {
        "method": method,
        "tests_run": tests_run if tests_run > 0 else None,
        "tests_passed": tests_passed if tests_run > 0 else None,
        "tests_failed": tests_failed if tests_run > 0 else None,
        "command": None,
        "pr_ref": None,
        "push_verified": None,
        "spec_deviation": None,
    }
