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


# ---------------------------------------------------------------------------
# D1b fabric-state-herstel: the write side counts the LAST green run.
#
# A report that shows a red run first and a green run after it used to land
# on the receipt with the red run's counts (first regex hit), so the receipt
# said `tests_failed > 0` for work whose own green run was clean. These tests
# pin the new reading. Like the tests above they touch no DB and no ledger:
# the parser and the envelope reader are pure functions over report text, so
# there is no project_id to filter on (ADR-007) and nothing to leak.
# ---------------------------------------------------------------------------

from receipt_verdict import compute_verdict


def _validation(body: str, heading: str = "## Verification") -> dict:
    return ReportParser().extract_validation(_report(heading, body))


def _receipt(body: str, dispatch_id: str = "20260929-d1b-test",
             heading: str = "## Verification") -> dict:
    parser = ReportParser()
    content = _report(heading, body).replace(
        "**Dispatch-ID**: d1-test", f"**Dispatch-ID**: {dispatch_id}\n**Date**: 2026-09-29T10:00:00Z")
    extracted = {
        "metadata": parser.extract_metadata(content),
        "recommendations": parser.extract_recommendations(content),
        "validation": parser.extract_validation(content),
        "intelligence": {},
    }
    return parser._build_enhanced_receipt(extracted, f"/reports/{dispatch_id}.md")


RED_THEN_GREEN_NL = """**Rode run op kop 13907fe8** (nieuwe tests, oude code). Alle vier falen op gedrag:

```
python3 -m pytest tests/test_x.py -q
(a) AssertionError: premigration
4 failed, 47 deselected in 0.71s
```

**Groene run op 37c5e034**:

```
python3 -m pytest tests/test_x.py -q
96 passed in 37.62s   (voor de extra no-table-test)

python3 -m pytest tests/test_x.py tests/test_y.py -q
198 passed in 51.21s
```

De bestaande test blijft groen.
"""

RED_THEN_GREEN_EN = """### Red runs (old head c4c9d580, signatures intact)

Command: `python3 -m pytest -q tests/test_a.py`. Result: **8 failed, 8 passed**.

- (a) `test_one`: `assert {} == {}`.

### Green runs (c2c60327)

- `python3 -m pytest -q tests/test_a.py`: 23 passed
- `python3 -m pytest -q tests/test_b.py`: 42 passed
"""


def test_red_then_green_dutch_counts_the_last_green_run():
    result = _validation(RED_THEN_GREEN_NL)
    assert (result["tests_passed"], result["tests_failed"]) == (198, 0)


def test_red_then_green_english_headings_count_the_last_green_run():
    result = _validation(RED_THEN_GREEN_EN)
    assert (result["tests_passed"], result["tests_failed"]) == (42, 0)


def test_red_and_green_on_one_line_counts_the_green_one():
    body = "Rood op de oude code: 3 failed, 10 passed. Groen op de kop: 13 passed in 0.4s."
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (13, 0)


def test_only_green_runs_take_the_last_one():
    body = "- `pytest tests/test_a.py` -> 17 passed in 0.3s\n- `pytest tests/test_b.py` -> 52 passed in 1.81s\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (52, 0)


def test_only_a_red_run_keeps_its_failures_and_never_accepts():
    body = "Rode run op de oude code: `pytest tests/test_a.py` -> 5 failed, 33 passed in 1.2s\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (33, 5)
    receipt = _receipt(body)
    assert receipt["verification"]["tests_failed"] == 5
    assert compute_verdict({**receipt, "status": "success"})["decision"] != "accept"


def test_green_run_with_a_real_failure_keeps_the_failure():
    body = "Groene run: `pytest tests/` -> 12 failed, 8 passed in 2.49s\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (8, 12)


def test_mutation_check_after_the_green_run_is_not_the_final_count():
    body = (
        "- `pytest tests/test_a.py`: 16 passed.\n"
        "- Mutation (swap b and c, then restore): 1 failed / 7 passed.\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (16, 0)


def test_red_then_green_receipt_is_accepted_on_success():
    receipt = _receipt(RED_THEN_GREEN_NL)
    assert receipt["verification"]["method"] == "pytest"
    assert receipt["verification"]["tests_run"] == 198
    assert receipt["verification"]["tests_failed"] == 0
    assert compute_verdict({**receipt, "status": "success"})["decision"] == "accept"


def test_unittest_ok_form_is_recognised():
    body = "```\n$ python3 -m unittest tests/test_a.py\n..........\n----------\nRan 12 tests in 0.031s\n\nOK\n```\n"
    receipt = _receipt(body)
    assert receipt["verification"]["method"] == "unittest"
    assert (receipt["verification"]["tests_passed"], receipt["verification"]["tests_failed"]) == (12, 0)


def test_unittest_failed_form_counts_failures_and_errors():
    body = "Ran 9 tests in 0.2s\n\nFAILED (failures=2, errors=1, skipped=1)\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (5, 3)


def test_unittest_ran_without_a_result_line_is_no_run():
    receipt = _receipt("Ran 3 tests in 0.1s and then the process was killed.")
    assert receipt["verification"]["method"] == "unknown"
    assert receipt["verification"]["tests_run"] is None


def test_dutch_verificatie_heading_is_read():
    receipt = _receipt("`pytest tests/test_a.py` -> 21 passed in 0.9s", heading="## Verificatie")
    assert receipt["verification"]["method"] == "pytest"
    assert receipt["verification"]["tests_passed"] == 21


def test_prose_failures_are_not_a_run():
    body = "The 2 failures in the neighbour file are pre-existing.\n\n`pytest tests/test_a.py` -> 30 passed\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (30, 0)


def test_gate_runner_report_is_marked_as_evidence_not_a_work_run():
    body = "- `python -m pytest tests/test_reconciler.py -q` -> 15 passed.\n"
    receipt = _receipt(body, dispatch_id="deepseek-gate-pr1981-1790662192")
    assert receipt["verification"]["method"] == "gate_evidence"
    assert receipt["verification"]["tests_run"] is None
    assert compute_verdict({**receipt, "status": "success"})["decision"] != "accept"


def test_envelope_reader_marks_a_gate_runner_report_as_evidence(tmp_path):
    report = tmp_path / "kimi-gate-pr1958-1790577475.md"
    content = _report("## Verification", "`pytest tests/test_x.py` -> **58 passed**").replace(
        "d1-test", "kimi-gate-pr1958-1790577475")
    report.write_text(content, encoding="utf-8")
    verification = _verification_from_report(report)
    assert verification["method"] == "gate_evidence"
    assert verification["tests_run"] is None


def test_work_dispatch_with_gate_in_its_name_is_not_a_gate_runner():
    receipt = _receipt("`pytest tests/` -> 9 passed", dispatch_id="20260929-fsh-d7-gate-bundles")
    assert receipt["verification"]["method"] == "pytest"


@pytest.mark.parametrize("body", [RED_THEN_GREEN_NL, RED_THEN_GREEN_EN, "Ran 4 tests in 0.1s\n\nOK\n"])
def test_both_write_paths_give_the_same_verification(tmp_path, body):
    report = tmp_path / "20260929-d1b-test.md"
    report.write_text(_report("## Verification", body).replace("d1-test", "20260929-d1b-test"),
                      encoding="utf-8")
    assert _verification_from_report(report) == _receipt(body)["verification"]


def test_capitalised_prose_is_not_a_run():
    body = "`pytest tests/test_a.py` -> 7 passed in 1.86s\n\nThe 2 PASSes on old code are guards, not red runs.\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (7, 0)


def test_red_marker_later_in_the_same_sentence_marks_that_run():
    body = (
        "- `pytest tests/test_a.py` -> 40 passed in 2.1s\n"
        "- `pytest tests/test_a.py` -> 7 failed, 6 passed on the old code, as expected.\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (40, 0)


def test_baseline_run_before_the_change_is_not_the_final_count():
    body = (
        "Groen na de fix:\n\n-> 145 passed in 12.37s\n\n"
        "Vooraf, ter baseline: de worker-tests gaven 8 failed, 20 passed.\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (145, 0)


def test_a_later_sentence_with_a_marker_does_not_relabel_the_run_before_it():
    body = "Combined set: **392 passed, 3 failed**. The 3 fail identically on `main`.\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (392, 3)
