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


def test_red_words_mid_line_are_prose_and_the_failure_counts():
    body = "Rood op de oude code: 3 failed, 10 passed. Groen op de kop: 13 passed in 0.4s."
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (13, 3)


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


def test_an_unlabelled_mutation_check_keeps_its_failure():
    body = (
        "- `pytest tests/test_a.py`: 16 passed.\n"
        "- Mutation (swap b and c, then restore): 1 failed / 7 passed.\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (7, 1)


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


def test_prose_failures_count_fail_closed():
    body = "The 2 failures in the neighbour file are pre-existing.\n\n`pytest tests/test_a.py` -> 30 passed\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (30, 2)


@pytest.mark.parametrize("line, failed", [
    ("- Volle unit-suite: **7417 passed**; 10 failures in `tests/deploy/test_backup.py` zijn pre-existing", 10),
    ("Resultaat: 4 FAIL, 1 WARN", 4),
    ("Result: 1 FAILED (test_migration_file_exists)", 1),
    ("**921 passed** after fixing the singleton test (5 failing before that fix).", 5),
    ("op PRE-FIX code -> **5 van de 7 tests FAILEN** (staging, loading).", 7),
    ("FAIL: 21 ongedekte route(s) > baseline 20. De baseline mag nooit stijgen", 21),
    ("The contract test 3 fails on the current head.", 3),
])
def test_a_documented_failure_in_any_case_or_as_a_noun_counts(line, failed):
    body = f"{line}\n\n`pytest tests/test_a.py` -> 38 passed\n"
    receipt = _receipt(body)
    assert receipt["verification"]["tests_failed"] == failed
    assert compute_verdict({**receipt, "status": "success"})["decision"] != "accept"


def test_zero_failures_in_prose_is_no_run():
    body = "`pytest tests/test_a.py` -> 38 passed\n\nThere were 0 failures in the neighbour files.\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (38, 0)


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


def test_a_red_word_later_in_the_sentence_is_no_label():
    body = (
        "- `pytest tests/test_a.py` -> 40 passed in 2.1s\n"
        "- `pytest tests/test_a.py` -> 7 failed, 6 passed on the old code, as expected.\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (6, 7)


def test_a_baseline_run_in_prose_keeps_its_failure():
    body = (
        "Groen na de fix:\n\n-> 145 passed in 12.37s\n\n"
        "Vooraf, ter baseline: de worker-tests gaven 8 failed, 20 passed.\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (20, 8)


def test_a_later_sentence_with_a_marker_does_not_relabel_the_run_before_it():
    body = "Combined set: **392 passed, 3 failed**. The 3 fail identically on `main`.\n"
    result = _validation(body)
    assert result["tests_failed"] == 3
    assert compute_verdict({**_receipt(body), "status": "success"})["decision"] != "accept"


def test_a_marker_in_the_middle_of_a_paragraph_does_not_relabel():
    body = (
        "Green run:\n\n```\n162 passed in 7.10s\n```\n\n"
        "### Red run - same test file against the pre-fix code\n\n"
        "Method: replaced the scripts with the old versions, ran the target file,\n"
        "then restored the fixed files and re-ran to confirm green again (shown above).\n\n"
        "```\n5 failed, 2 passed in 1.64s\n```\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (162, 0)


# ---------------------------------------------------------------------------
# D1b fix-forward 1: only an explicit contract label discounts a run.
#
# deepseek_gate (PR #2000) found the "last green run counts" reading
# fail-open: an unlabelled failing run vanished behind a later, narrower
# green run. The two fixtures below reproduce the two reports it named, in
# shape, not as copies.
# ---------------------------------------------------------------------------

from receipt_verdict import INCOMPLETE_EVIDENCE_METHODS
from report_body_contract import GREEN_RUN_LABELS, RED_RUN_LABELS, build_directive

# 20260118-151300-T3-VALIDATION-quality-validation-report.md: a failure in the
# middle of three separate commands, the last one narrower and green.
SEPARATE_COMMANDS_MIDDLE_FAILS = """### Tests Executed
```bash
pytest tests/storage/test_factory_singleton.py -v
============= 11 passed in 0.36s =============

pytest tests/integration/test_memory_usage.py -v
============= 4 passed, 1 failed in 0.47s =============

pytest tests/services/test_orchestrator_service.py -v
============= 1 passed in 0.91s =============
```
"""

# 20260226-111638-A-pr1-crawl-policy-schema-handoff.md: 10 failures on the PR
# branch, then a green contract-test run of 6.
PR_BRANCH_FAILS_THEN_CONTRACT_GREEN = """- Lint
  - Command: `ruff check src/`
  - Result: `All checks passed!`
- Unit-tests (PR branch, after fixing `.claude/vnx-system`)
  - Command: `python -m pytest -q tests/unit`
  - Result: `10 failed, 517 passed, 20 warnings in 10.63s`
- Contract-tests (exact)
  - Command: `pytest tests/api/test_schema_contract.py -q`
  - Result: `6 passed, 16 warnings in 0.80s`
"""


@pytest.mark.parametrize("body, expected", [
    (SEPARATE_COMMANDS_MIDDLE_FAILS, (1, 1)),
    (PR_BRANCH_FAILS_THEN_CONTRACT_GREEN, (6, 10)),
])
def test_deepseek_reports_keep_their_failure_and_are_not_accepted(tmp_path, body, expected):
    result = _validation(body, heading="## Tests")
    assert (result["tests_passed"], result["tests_failed"]) == expected
    receipt = _receipt(body, heading="## Tests")
    assert receipt["verification"]["tests_failed"] > 0
    assert compute_verdict({**receipt, "status": "success"})["decision"] != "accept"
    report = tmp_path / "20260929-d1b-test.md"
    report.write_text(_report("## Tests", body).replace("d1-test", "20260929-d1b-test"),
                      encoding="utf-8")
    assert _verification_from_report(report) == receipt["verification"]


def test_a_green_label_does_not_replace_an_earlier_unlabelled_failure():
    body = (
        "`pytest tests/test_a.py tests/test_b.py` -> 3 failed, 40 passed\n\n"
        "**Green run** on the head: `pytest tests/test_a.py` -> 12 passed\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (12, 3)


def test_the_same_failure_under_a_red_label_is_discounted():
    body = (
        "**Red run** on the old head: `pytest tests/test_a.py tests/test_b.py` -> 3 failed, 40 passed\n\n"
        "**Green run** on the head: `pytest tests/test_a.py tests/test_b.py` -> 43 passed\n"
    )
    receipt = _receipt(body)
    assert (receipt["verification"]["tests_passed"], receipt["verification"]["tests_failed"]) == (43, 0)
    assert compute_verdict({**receipt, "status": "success"})["decision"] == "accept"


@pytest.mark.parametrize("label", RED_RUN_LABELS)
def test_every_contract_red_label_discounts_the_run_under_it(label):
    body = f"{label}: `pytest tests/test_a.py` -> 2 failed, 5 passed\n\n`pytest tests/test_a.py` -> 7 passed\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (7, 0)


@pytest.mark.parametrize("label", GREEN_RUN_LABELS)
def test_no_green_label_discounts_a_failure(label):
    body = f"{label}: `pytest tests/test_a.py` -> 2 failed, 5 passed\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (5, 2)


@pytest.mark.parametrize("prefix", ["### ", "- ", "1. ", "**", "- **", "_"])
def test_a_label_may_sit_behind_heading_bullet_or_emphasis(prefix):
    body = f"{prefix}Rode run op kop abc123: 4 failed, 47 deselected in 0.71s\n\n### Groene run\n\n96 passed\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (96, 0)


def test_a_red_list_item_reaches_only_its_own_item():
    body = (
        "- Red run: `pytest tests/test_a.py` -> 4 failed\n"
        "- `pytest tests/test_b.py` -> 10 passed, 2 failed\n"
        "- `pytest tests/test_a.py` -> 4 passed\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (4, 2)


def test_a_red_paragraph_reaches_the_fence_right_after_it():
    body = "**Red run** (old code), all four fail on behaviour:\n\n```\n4 failed, 47 deselected in 0.71s\n```\n\n```\n51 passed\n```\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (51, 0)


def test_a_red_paragraph_does_not_reach_a_later_paragraph():
    body = (
        "Red run: `pytest tests/test_a.py` -> 4 failed\n\n"
        "Then the neighbour file: `pytest tests/test_b.py` -> 20 passed, 1 failed\n\n"
        "`pytest tests/test_a.py` -> 4 passed\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (4, 1)


def test_a_red_heading_reaches_deeper_headings_but_not_a_sibling():
    body = (
        "### Red runs\n\n#### (a) the first test\n\n```\n1 failed, 3 passed\n```\n\n"
        "### Neighbour files\n\n```\n2 failed, 30 passed\n```\n\n"
        "### Green runs\n\n```\n34 passed\n```\n"
    )
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (34, 2)


def test_a_label_inside_a_code_fence_is_no_label():
    body = "```\nRed run: 3 failed, 9 passed\n```\n\n12 passed\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (12, 3)


def test_unittest_expected_failures_do_not_overwrite_failures():
    body = "Ran 10 tests in 0.2s\n\nFAILED (failures=1, expected failures=3)\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (6, 1)


def test_an_errors_only_clause_counts_as_failed():
    body = "`pytest tests/test_a.py` -> 20 passed\n\n`pytest tests/test_b.py` -> 2 errors in 0.31s\n"
    result = _validation(body)
    assert result["tests_failed"] == 2
    assert compute_verdict({**_receipt(body), "status": "success"})["decision"] != "accept"


def test_zero_errors_alone_is_no_run():
    body = "`pytest tests/test_a.py` -> 20 passed\n\nruff: 0 errors\n"
    result = _validation(body)
    assert (result["tests_passed"], result["tests_failed"]) == (20, 0)


@pytest.mark.parametrize("body", ["12/12 passing", "All tests pass.", "Tests: all green"])
def test_a_claim_without_a_runner_summary_stays_unknown(body):
    assert _receipt(body)["verification"]["method"] == "unknown"


def test_gate_evidence_is_incomplete_evidence():
    assert "gate_evidence" in INCOMPLETE_EVIDENCE_METHODS
    verdict = compute_verdict({"status": "success", "verification": {
        "method": "gate_evidence", "tests_run": 5, "tests_passed": 5, "tests_failed": 0}})
    assert verdict["evidence_complete"] is False
    assert verdict["decision"] == "investigate"


def test_the_directive_tells_workers_the_red_label():
    directive = build_directive("disp-x")
    for label in ("Red run", "Rode run", "Before the fix", "Voor de fix"):
        assert f"`{label}`" in directive


# ---------------------------------------------------------------------------
# D1b ff2: a unittest run in FAILED form is always a failed run. unittest
# prints FAILED whenever wasSuccessful() is False, which includes unexpected
# successes; a FAILED line whose fields the extractor does not know still
# counts as at least one failure (fail-closed, ADR-035).
# ---------------------------------------------------------------------------

_UNEXPECTED = "Ran 5 tests in 0.1s\n\nFAILED (unexpected successes=1)\n"
_EXPECTED_ONLY = "Ran 5 tests in 0.1s\n\nFAILED (expected failures=1)\n"


@pytest.mark.parametrize("prefix", ["", "**Green run** on the head:\n\n", "Groene run op de kop:\n\n"])
def test_unittest_unexpected_success_is_a_failed_run(prefix):
    body = prefix + "```\n" + _UNEXPECTED + "```\n"
    receipt = _receipt(body)
    assert receipt["verification"]["method"] == "unittest"
    assert (receipt["verification"]["tests_passed"], receipt["verification"]["tests_failed"]) == (4, 1)
    assert compute_verdict({**receipt, "status": "success"})["decision"] != "accept"


@pytest.mark.parametrize("prefix", ["", "**Green run** on the head:\n\n", "Groene run op de kop:\n\n"])
def test_unittest_failed_form_with_only_expected_failures_is_a_failed_run(prefix):
    body = prefix + "```\n" + _EXPECTED_ONLY + "```\n"
    receipt = _receipt(body)
    assert receipt["verification"]["tests_failed"] == 1
    assert receipt["verification"]["tests_passed"] == 3
    assert compute_verdict({**receipt, "status": "success"})["decision"] != "accept"


@pytest.mark.parametrize("result", ["FAILED (skipped=1)", "FAILED ()", "FAILED (flaky reruns=2)", "FAILED"])
def test_unittest_failed_form_without_a_known_failure_field_counts_one(result):
    body = f"Ran 5 tests in 0.1s\n\n{result}\n"
    receipt = _receipt(body)
    assert receipt["verification"]["method"] == "unittest"
    assert receipt["verification"]["tests_failed"] >= 1
    assert compute_verdict({**receipt, "status": "success"})["decision"] != "accept"


def test_a_red_labelled_unexpected_success_is_discounted_by_a_later_ok_run():
    body = (
        "**Red run** on the old head:\n\n```\n" + _UNEXPECTED + "```\n\n"
        "**Green run** on the head:\n\n```\nRan 5 tests in 0.1s\n\nOK (expected failures=1)\n```\n"
    )
    receipt = _receipt(body)
    assert (receipt["verification"]["tests_passed"], receipt["verification"]["tests_failed"]) == (4, 0)
    assert compute_verdict({**receipt, "status": "success"})["decision"] == "accept"


@pytest.mark.parametrize("result, expected", [
    ("OK (expected failures=1)", (4, 0)),
    ("OK (skipped=2, expected failures=1)", (2, 0)),
    ("FAILED (failures=2, errors=1)", (2, 3)),
    ("FAILED (failures=1, unexpected successes=2)", (2, 3)),
    ("FAILED (unexpected successes=9)", (0, 9)),
])
def test_unittest_result_counts(result, expected):
    result_counts = _validation(f"Ran 5 tests in 0.1s\n\n{result}\n")
    assert (result_counts["tests_passed"], result_counts["tests_failed"]) == expected
