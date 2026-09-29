"""D1 fabric-state-herstel — the report parser reads the contract's `## Verification`.

The report contract prescribes `## Verification` (aliases `## Test Results`,
`## Evidence`, `## Tests`). `ReportParser.extract_validation` used to look only
for `Validation` or `Test`, so a contract-conformant report yielded
`method=unknown` and zero tests on every receipt.

No central DB is touched here: the parser and the envelope reader are pure
functions over a report text/file, so there is no project_id to filter on and
no store to leak between two projects.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(SCRIPTS_DIR))

from report_parser import ReportParser
from envelope_govern_support import _verification_from_report


def _report(heading: str, body: str) -> str:
    return (
        "**Dispatch-ID**: d1-test\n\n"
        "## Summary\n\nDid the work and wrote the tests for it, at some length.\n\n"
        "## Changes\n\n- scripts/x.py\n\n"
        f"{heading}\n\n{body}\n\n"
        "## Open Items\n\nNone\n"
    )


PYTEST_LINE = "`pytest tests/test_x.py -x` -> 12 passed, 2 failed"


def test_contract_aliases_come_from_the_contract():
    from report_body_contract import section_heading_names

    assert section_heading_names("## Verification") == (
        "Verification", "Test Results", "Evidence", "Tests",
    )


@pytest.mark.parametrize("heading", [
    "## Verification", "## Test Results", "## Evidence", "## Tests",
])
def test_every_contract_heading_yields_counts(heading):
    result = ReportParser().extract_validation(_report(heading, PYTEST_LINE))
    assert result["tests_passed"] == 12
    assert result["tests_failed"] == 2


def test_legacy_validation_heading_still_read():
    result = ReportParser().extract_validation(_report("## Validation", "5 tests passed"))
    assert result["tests_passed"] == 5


def test_verification_wins_over_a_later_legacy_section():
    content = _report("## Verification", "7 passed") + "\n## Validation\n\n99 passed\n"
    assert ReportParser().extract_validation(content)["tests_passed"] == 7


def test_verification_section_stops_at_next_heading():
    content = _report("## Verification", "3 passed")
    result = ReportParser().extract_validation(content)
    assert result["tests_passed"] == 3
    assert result["tests_failed"] == 0


def test_no_verification_section_stays_empty():
    content = "## Summary\n\nSomething.\n\n## Open Items\n\nNone\n"
    result = ReportParser().extract_validation(content)
    assert result["tests_passed"] == 0 and result["tests_failed"] == 0


def test_envelope_reader_gives_pytest_method_for_verification_only_report(tmp_path):
    report = tmp_path / "d1-test.md"
    report.write_text(_report("## Verification", PYTEST_LINE), encoding="utf-8")
    verification = _verification_from_report(report)
    assert verification["method"] == "pytest"
    assert verification["tests_run"] == 14
    assert verification["tests_passed"] == 12
    assert verification["tests_failed"] == 2


def test_envelope_reader_unknown_without_evidence(tmp_path):
    report = tmp_path / "d1-none.md"
    report.write_text(_report("## Verification", "Not run."), encoding="utf-8")
    assert _verification_from_report(report)["method"] == "unknown"
