#!/usr/bin/env python3
"""verification_runs.py — which test runs in a report's Verification section count.

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
  failed, also in a clause that has nothing but errors (``2 errors in 0.3s``);
  ``0 errors`` alone is no run. Failure terms are read in any case and as a
  noun or a Dutch verb (``10 failures in test_x.py``, ``4 FAIL``,
  ``5 tests falen``), and a checker line ``FAIL: 21 ...`` counts its number:
  a failure the report documents always counts. Pass terms are case-sensitive runner words only:
  ``2 PASSes`` is prose, and so are ``12/12 passing`` and ``All tests pass``,
  which leave the receipt at ``method=unknown``.
- unittest: ``Ran N tests in Xs`` followed within a few lines by ``OK`` or
  ``FAILED (failures=a, errors=b, skipped=c, expected failures=d, unexpected successes=e)``.
  A FAILED line is always a failed run: failed is ``a + b + e`` and at least 1,
  also for ``FAILED ()`` or a field the reader does not know. ``OK`` stays 0
  failed, whatever its fields. ``Ran N tests`` without a result line is no run.

Which runs count (fix-forward 1, fail-closed). A run is red ONLY when it sits
under an explicit red label from the report contract
(``report_body_contract.RED_RUN_LABELS``: ``Red run``, ``Rode run``,
``Before the fix``, ``Voor de fix``) at the start of a line or heading. No word
elsewhere in a sentence marks a run: "baseline", "mutation", "on main" or
"old code" in prose is not a label, and a failure next to it counts.

- A heading label reaches until the next heading of the same or a higher
  level. A label on a paragraph, bold lead or list item reaches over its own
  paragraph (for a list item: until the next item) and a fenced block that
  follows it with only blank lines in between. A new label ends the reach of
  the one before it, so a green label (``GREEN_RUN_LABELS``) only ends a red
  reach early. It discounts nothing.
- ``tests_passed`` and ``method`` come from the LAST run that is not red.
  ``tests_failed`` is the HIGHEST failure count among all runs that are not
  red, so an unlabelled failing run is never hidden by a later, narrower
  green run (deepseek_gate, PR #2000). A green label does not lift that: it
  says a run is on the new code, not that it covers what an earlier run
  selected. Only a red label removes a failure.
- When every run is red, ``tests_passed`` and ``method`` still come from the
  last red run, but ``tests_failed`` is the HIGHEST failure count among the red
  runs: a report with only red runs keeps ``tests_failed > 0`` whenever any of
  them failed, so a later red run with 0 failures cannot erase an earlier one
  and the report can never be accepted on that evidence.

``verification_record`` turns that reading into the ADR-035 ``verification{}``
shape. Both write paths (``report_parser._build_enhanced_receipt`` and
``envelope_govern_support._verification_from_report``) call it, so one report
yields one verification on either path. A gate-runner dispatch
(``kimi-gate-pr<N>-<ts>``) is review evidence for another dispatch's PR, not a
work run: its reviewer's own test runs are not that PR's verification, so it
gets ``method="gate_evidence"``, no counts, and ``receipt_verdict`` treats
that method as incomplete evidence. The ledger is append-only (ADR-005) and
old receipts keep what they were written with.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from xml.etree import ElementTree

from receipt_outcome import GATE_DISPATCH_RE
from report_body_contract import GREEN_COLON_LABELS, GREEN_RUN_LABELS, RED_RUN_LABELS

GATE_EVIDENCE_METHOD = "gate_evidence"


def _label_alternation(labels) -> str:
    return "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))


# A label is the first words of the line: optional heading hashes, list
# bullet and emphasis may precede it, nothing else.
_LABEL = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:(?:[-*+]|\d+\.)\s+)?(?:\*\*|__|\*|_)?\s*"
    rf"(?:(?P<red>{_label_alternation(RED_RUN_LABELS)})\b"
    rf"|(?P<green>{_label_alternation(GREEN_RUN_LABELS)})\b"
    rf"|(?P<green_colon>{_label_alternation(GREEN_COLON_LABELS)})(?:\*\*|__|\*|_)?\s*:)",
    re.IGNORECASE,
)

# Asymmetric on purpose (fail-closed). Pass terms are case-sensitive: runners
# print lowercase counts, while prose like "the 2 PASSes on old code" is not a
# run. Failure terms are read in any case and as a noun too: "10 failures in
# test_backup.py are pre-existing", "4 FAIL" and "1 FAILED" document a failure,
# and a documented failure must never vanish from the receipt.
_TERM = re.compile(
    r"(\d+)\s+(?:(?i:tests?)\s+)?"
    r"(passed|pass|(?i:failed|failures|failure|failing|fails|fail|failen|falen|faalden"
    r"|errors|error)"
    r"|skipped|deselected|xfailed|xpassed|warnings|warning)\b",
)
# What may sit between two terms of one summary clause: separators, emphasis,
# "and"/"en". Anything else ends the clause.
_TERM_GAP = re.compile(r"^[\s,;/*`+]*(?:(?:and|en)\b[\s,;/*`+]*)?$", re.IGNORECASE)
_PASS_WORDS = frozenset({"passed", "pass"})
# A runner's own `failed`/`fail` is an outcome even at 0 ("0 failed"); a noun
# or an error count is one only when it is above 0 ("ruff: 0 errors" is no run).
_RUNNER_FAIL_WORDS = frozenset({"failed", "fail"})
_FAIL_WORDS = frozenset({"failed", "fail", "fails", "failures", "failure", "failing",
                         "failen", "falen", "faalden", "errors", "error"})
# A checker's own verdict line ("FAIL: 21 ongedekte route(s) > baseline 20")
# is a documented failure too: its number counts as failed.
_CHECK_FAIL = re.compile(r"^\s*(?:FAIL|FAILED)\s*:\s*(\d+)\b")

_HEADING = re.compile(r"^\s*(#{1,6})\s")
_BOLD_LEAD = re.compile(r"^\s*(?:(?:[-*+]|\d+\.)\s+)?\*\*")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_UNITTEST_RAN = re.compile(r"\bRan (\d+) tests? in [\d.]+\s*s\b")
_UNITTEST_OK = re.compile(r"^\s*OK\b(?:\s*\(([^)]*)\))?")
# unittest writes a bare `FAILED` when it has no counts to add; the fields are optional.
_UNITTEST_FAILED = re.compile(r"^\s*FAILED\s*(?:\(([^)]*)\)|$)")
# `expected failures=3` is one key; `\w+` would read it as a second `failures`.
_UNITTEST_FIELD = re.compile(r"([A-Za-z][A-Za-z ]*?)\s*=\s*(\d+)")
_UNITTEST_RESULT_WINDOW = 5


def label_color(line: str) -> Optional[str]:
    """``red``/``green`` when ``line`` opens with a contract run label, else None."""
    m = _LABEL.match(line)
    if m is None:
        return None
    return "red" if m.group("red") else "green"


def _pytest_clauses(line: str) -> List[Dict[str, Any]]:
    """Summary clauses on one line, each with its start offset and counts."""
    clauses: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for m in _TERM.finditer(line):
        word = m.group(2).lower()
        count = int(m.group(1))
        if current is not None and _TERM_GAP.match(line[current["end"]:m.start()]):
            current["end"] = m.end()
        else:
            current = {"start": m.start(), "end": m.end(), "passed": 0, "failed": 0,
                       "has_outcome": False}
            clauses.append(current)
        if word in _PASS_WORDS:
            current["passed"] += count
            current["has_outcome"] = True
        elif word in _FAIL_WORDS:
            current["failed"] += count
            current["has_outcome"] = (current["has_outcome"] or count > 0
                                      or m.group(2) in _RUNNER_FAIL_WORDS)
    return [c for c in clauses if c["has_outcome"]]


def _unittest_counts(ran: int, result: str, failed_form: bool) -> Dict[str, int]:
    fields = {k.strip().lower(): int(v) for k, v in _UNITTEST_FIELD.findall(result or "")}
    failed = 0
    if failed_form:
        # unittest prints FAILED whenever wasSuccessful() is False, unexpected
        # successes included; a FAILED line is a failed run even when no field
        # the extractor knows carries the count (fail-closed, ADR-035).
        failed = max(1, fields.get("failures", 0) + fields.get("errors", 0)
                     + fields.get("unexpected successes", 0))
    passed = max(0, ran - failed - fields.get("skipped", 0) - fields.get("expected failures", 0))
    return {"passed": passed, "failed": failed}


def _runs_on_line(lines: List[str], index: int) -> List[Dict[str, Any]]:
    line = lines[index]
    found: List[Dict[str, Any]] = [
        {**c, "method": "pytest"} for c in _pytest_clauses(line)
    ]
    check = _CHECK_FAIL.match(line)
    if check and int(check.group(1)) > 0:
        found.append({"start": check.start(1), "end": check.end(), "passed": 0,
                      "failed": int(check.group(1)), "method": "pytest"})
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
    found.sort(key=lambda r: r["start"])
    return found


def extract_runs(section: str) -> List[Dict[str, Any]]:
    """Every test run in ``section``, in order, with ``passed``, ``failed``,
    ``method`` (``pytest``/``unittest``) and ``color`` (``red``/``green``/None,
    from the contract label the run sits under)."""
    lines = section.splitlines()
    runs: List[Dict[str, Any]] = []
    heading_scope: Optional[Dict[str, Any]] = None   # {"color", "level"}
    block_scope: Optional[Dict[str, Any]] = None     # {"color", "list_item", "open"}
    in_fence = False
    fence_color: Optional[str] = None
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            if in_fence:
                in_fence, fence_color = False, None
            else:
                in_fence = True
                fence_color = block_scope["color"] if block_scope else None
                block_scope = None
            continue
        if in_fence:
            color = fence_color or (heading_scope or {}).get("color")
        elif not line.strip():
            if block_scope is not None:
                block_scope["open"] = False
            continue
        else:
            label = label_color(line)
            heading = _HEADING.match(line)
            if heading:
                block_scope = None
                level = len(heading.group(1))
                if label is not None:
                    heading_scope = {"color": label, "level": level}
                elif heading_scope is not None and level <= heading_scope["level"]:
                    heading_scope = None
            else:
                is_item = bool(_LIST_ITEM.match(line))
                if block_scope is not None and (
                    not block_scope["open"]
                    or (block_scope["list_item"] and is_item)
                    or (label is None and _BOLD_LEAD.match(line))
                ):
                    block_scope = None
                if label is not None:
                    block_scope = {"color": label, "list_item": is_item, "open": True}
            color = ((block_scope or {}).get("color")
                     or (heading_scope or {}).get("color"))
        for run in _runs_on_line(lines, index):
            runs.append({"passed": run["passed"], "failed": run["failed"],
                         "method": run["method"], "color": color})
    return runs


def counted_run(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The figures a receipt carries for ``runs`` (see the module docstring).

    ``passed``/``method`` from the last run that is not red, ``failed`` the
    highest failure count among the runs that are not red. When every run is
    red the last run still gives ``passed``/``method``, but ``failed`` is the
    highest failure count among the red runs: a later red run with 0 failures
    may not erase the failure an earlier red run documented, so a report with
    only red runs always keeps ``tests_failed > 0`` when any of them failed.
    None without runs.
    """
    not_red = [r for r in runs if r["color"] != "red"]
    if not not_red:
        if not runs:
            return None
        return {**runs[-1], "failed": max(r["failed"] for r in runs)}
    last = not_red[-1]
    return {**last, "failed": max(r["failed"] for r in not_red)}


def is_gate_runner(dispatch_id: Any) -> bool:
    return bool(GATE_DISPATCH_RE.match(str(dispatch_id or "").strip()))


def junit_sidecar_path(report_path: Union[str, Path]) -> Path:
    """The XML a builder writes beside its report: ``<report without .md>.junit.xml``.

    One helper for every reader (PR-4's report path and PR-5's envelope path).
    """
    path = Path(report_path)
    stem = path.with_suffix("") if path.suffix.lower() == ".md" else path
    return stem.with_name(stem.name + ".junit.xml")


# A testsuite without these cannot be counted: absence is no measurement, not 0.
_JUNIT_COUNT_ATTRS = ("tests", "failures", "errors")


def _junit_int(suite: ElementTree.Element, attr: str) -> Optional[int]:
    raw = suite.get(attr)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def from_junit(xml_path: Union[str, Path],
               expected_suite: Any) -> Tuple[Optional[Dict[str, int]], Optional[str]]:
    """Counts from the builder's JUnit XML, or ``(None, reason)`` when it is refused.

    Success is ``({"passed", "failed", "skipped"}, None)``, summed over every
    ``testsuite``. A missing or unparseable file, no testsuite, a ``name`` that
    is not ``expected_suite`` (the dispatch id the builder passes as
    ``pytest -o junit_suite_name=<dispatch_id>``), or a count that is not a
    whole number is refused with a reason: a receipt that cannot prove its
    numbers keeps the text counts (fail-closed on evidence). Never raises.
    """
    try:
        path = Path(xml_path)
        try:
            root = ElementTree.parse(path).getroot()
        except FileNotFoundError:
            return None, f"no JUnit file at {path}"
        except ElementTree.ParseError as exc:
            return None, f"JUnit file does not parse: {exc}"
        except OSError as exc:
            return None, f"JUnit file cannot be read: {exc}"
        suites = list(root.iter("testsuite"))
        if not suites:
            return None, "JUnit file carries no testsuite"
        suite_name = str(expected_suite or "").strip()
        passed = failed = skipped = 0
        for suite in suites:
            name = (suite.get("name") or "").strip()
            # The builder stamps the dispatch id as the suite name; a file that
            # names another suite is another run's evidence, not this report's.
            if not suite_name or name != suite_name:
                return None, (f"testsuite name {name!r} does not equal the dispatch id "
                              f"{suite_name!r}")
            counts = {attr: _junit_int(suite, attr) for attr in _JUNIT_COUNT_ATTRS}
            for attr, value in counts.items():
                if value is None:
                    return None, f"testsuite {name!r} has no usable {attr} count"
            tests, failures, errors = (counts[attr] for attr in _JUNIT_COUNT_ATTRS)
            skips = _junit_int(suite, "skipped")
            if skips is None:
                # pytest always writes the attribute; a missing one is not a 0
                # we may silently assume. Refuse instead of guessing.
                return None, f"testsuite {name!r} has no usable skipped count"
            failed += failures + errors
            skipped += skips
            passed += max(tests - failures - errors - skips, 0)
        return {"passed": passed, "failed": failed, "skipped": skipped}, None
    except Exception as exc:  # the reader's contract is "never raise", and the reason is returned
        return None, f"JUnit file could not be read: {type(exc).__name__}: {exc}"


def verification_record(validation: Dict[str, Any],
                        dispatch_id: Any = None, *,
                        count_source: Optional[str] = None,
                        count_mismatch: bool = False,
                        junit_rejected: Optional[str] = None) -> Dict[str, Any]:
    """ADR-035 ``verification{}`` from ``extract_validation`` output.

    ``count_source``/``count_mismatch``/``junit_rejected`` are the
    deterministische-testverificatie fields, OMITTED unless a caller passes
    them: an existing caller writes exactly the receipt it wrote before.
    """
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
    record = {
        "method": method,
        "tests_run": tests_run if tests_run > 0 else None,
        "tests_passed": tests_passed if tests_run > 0 else None,
        "tests_failed": tests_failed if tests_run > 0 else None,
        "command": None,
        "pr_ref": None,
        "push_verified": None,
        "spec_deviation": None,
    }
    if count_source is not None:
        record["count_source"] = count_source
    if count_mismatch:
        record["count_mismatch"] = True
    if junit_rejected:
        record["junit_rejected"] = junit_rejected
    return record
