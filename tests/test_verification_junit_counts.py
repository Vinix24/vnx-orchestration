"""Receipt test counts from the builder's JUnit XML (deterministische-testverificatie PR-4).

Path 2 used to take its counts from a regex over the Verification prose: a
builder could write "12 passed" and the receipt said 12. ``from_junit`` reads
the XML left beside the report; ``_build_enhanced_receipt`` prefers it when
``VNX_JUNIT_COUNTS`` is on. Off is today's receipt, byte for byte.

The red run ran on origin/main with this file copied in: the "12 passed beside
the XML" test failed as ``12 == 3``. PR-4 imports are local to each test, so
the old code fails on the assertion, not on a missing import.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO = Path(__file__).resolve().parent.parent
for _path in (REPO / "scripts", REPO / "scripts" / "lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from report_parser import ReportParser  # noqa: E402

DISPATCH = "dlv-junit-counts-test"
TEXT_12_PASSED = "ran `pytest tests/test_x.py`; 12 passed"
TEXT_2_FAILED = "ran `pytest tests/test_x.py`; 2 failed, 8 passed"


def _xml_body(*, name: str = DISPATCH, tests: int = 5, failures: int = 1,
              errors: int = 0, skipped: int = 1) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            f'<testsuite name="{name}" tests="{tests}" failures="{failures}" '
            f'errors="{errors}" skipped="{skipped}">\n</testsuite>\n')


def _report_body(verification: str, dispatch_id: str = DISPATCH) -> str:
    return f"""# Completion Report
**Status**: success
**Dispatch-ID**: {dispatch_id}
**Model**: deepseek-flash
**Provider**: deepseek

## Summary
A completion report long enough for the body contract, whose verification text and JUnit
sidecar disagree on purpose so the merge rule can be pinned.

## Changes
- touched the reader

## Verification
{verification}

## Open Items
None
"""


def _parse(tmp_path: Path, verification: str, **xml_kwargs: Any) -> Dict[str, Any]:
    """Parse a report with a JUnit sidecar beside it; returns the receipt."""
    report = tmp_path / f"{DISPATCH}.md"
    report.write_text(_report_body(verification), encoding="utf-8")
    (tmp_path / f"{DISPATCH}.junit.xml").write_text(_xml_body(**xml_kwargs), encoding="utf-8")
    return ReportParser().parse_report(str(report))


# ---------------------------------------------------------------------------
# from_junit: valid, every testsuite, and the refusals (never raises)
# ---------------------------------------------------------------------------

def test_from_junit_counts_a_valid_file(tmp_path: Path) -> None:
    from verification_runs import from_junit
    path = tmp_path / "r.junit.xml"
    path.write_text(_xml_body(), encoding="utf-8")
    # 5 tests = 3 passed + 1 failed + 1 skipped
    assert from_junit(path, DISPATCH) == ({"passed": 3, "failed": 1, "skipped": 1}, None)


def test_from_junit_sums_every_testsuite(tmp_path: Path) -> None:
    from verification_runs import from_junit
    path = tmp_path / "r.junit.xml"
    path.write_text(
        "<testsuites>\n"
        f'  <testsuite name="{DISPATCH}" tests="2" failures="0" errors="0" skipped="0"/>\n'
        f'  <testsuite name="{DISPATCH}" tests="3" failures="1" errors="1" skipped="1"/>\n'
        "</testsuites>\n", encoding="utf-8")
    assert from_junit(path, DISPATCH) == ({"passed": 2, "failed": 2, "skipped": 1}, None)


@pytest.mark.parametrize("content,reason", [
    (_xml_body(name="pytest"), "does not equal"),
    ("not xml at all <", "does not parse"),
    ("<testsuites/>", "no testsuite"),
    (f'<testsuite name="{DISPATCH}" failures="0" errors="0" skipped="0"/>', "tests count"),
    (f'<testsuite name="{DISPATCH}" tests="five" failures="0" errors="0" skipped="0"/>',
     "tests count"),
    (f'<testsuite name="{DISPATCH}" tests="1" failures="0" errors="0"/>', "skipped count"),
], ids=["wrong-suite", "broken", "no-testsuite", "missing-count", "malformed-count",
        "missing-skipped"])
def test_from_junit_refuses_with_a_reason(tmp_path: Path, content: str, reason: str) -> None:
    from verification_runs import from_junit
    path = tmp_path / "r.junit.xml"
    path.write_text(content, encoding="utf-8")
    counts, why = from_junit(path, DISPATCH)
    assert counts is None
    assert reason in why


def test_from_junit_refuses_a_missing_file_and_an_empty_suite_name(tmp_path: Path) -> None:
    from verification_runs import from_junit
    counts, why = from_junit(tmp_path / "absent.junit.xml", DISPATCH)
    assert counts is None and "no JUnit file" in why
    path = tmp_path / "r.junit.xml"
    path.write_text(_xml_body(), encoding="utf-8")
    counts, why = from_junit(path, "")
    assert counts is None and "does not equal" in why


def test_junit_sidecar_path_is_the_report_without_md() -> None:
    from verification_runs import junit_sidecar_path
    assert junit_sidecar_path("/reports/dlv-abc.md") == Path("/reports/dlv-abc.junit.xml")
    assert junit_sidecar_path(Path("/reports/dlv-abc.txt")) == Path("/reports/dlv-abc.txt.junit.xml")


# ---------------------------------------------------------------------------
# The merge rule in report_parser, with VNX_JUNIT_COUNTS on
# ---------------------------------------------------------------------------

def test_text_12_passed_beside_xml_3_passed_1_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "1")
    verification = _parse(tmp_path, TEXT_12_PASSED)["verification"]
    assert verification["tests_passed"] == 3
    assert verification["tests_failed"] == 1
    assert verification["count_source"] == "junit_worker"
    assert verification["count_mismatch"] is True


def test_a_text_failure_is_never_erased_by_a_clean_xml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "1")
    verification = _parse(
        tmp_path, TEXT_2_FAILED, tests=8, failures=0, errors=0, skipped=0)["verification"]
    assert verification["tests_passed"] == 8
    assert verification["tests_failed"] == 2
    assert verification["count_mismatch"] is True
    assert verification["count_source"] == "junit_worker"


def test_agreeing_text_and_xml_is_no_mismatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "1")
    verification = _parse(
        tmp_path, "3 passed, 1 skipped", tests=4, failures=0, errors=0, skipped=1)["verification"]
    assert (verification["tests_passed"], verification["tests_failed"]) == (3, 0)
    assert verification["count_source"] == "junit_worker"
    assert "count_mismatch" not in verification


def test_a_refused_sidecar_keeps_the_text_counts_and_names_the_reason(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "1")
    verification = _parse(tmp_path, TEXT_12_PASSED, name="pytest")["verification"]
    assert verification["tests_passed"] == 12
    assert verification["count_source"] == "report_text"
    assert "does not equal" in verification["junit_rejected"]


def test_a_missing_sidecar_keeps_the_text_counts_without_a_rejection(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "1")
    report = tmp_path / f"{DISPATCH}.md"
    report.write_text(_report_body(TEXT_12_PASSED), encoding="utf-8")
    verification = ReportParser().parse_report(str(report))["verification"]
    assert verification["tests_passed"] == 12
    assert verification["count_source"] == "report_text"
    assert "junit_rejected" not in verification


def test_setting_off_is_todays_receipt_byte_for_byte(tmp_path: Path, monkeypatch) -> None:
    from verification_runs import verification_record
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "0")
    body = _report_body(TEXT_12_PASSED)
    report = tmp_path / f"{DISPATCH}.md"
    report.write_text(body, encoding="utf-8")
    (tmp_path / f"{DISPATCH}.junit.xml").write_text(_xml_body(), encoding="utf-8")
    verification = ReportParser().parse_report(str(report))["verification"]
    assert verification == verification_record(ReportParser().extract_validation(body), DISPATCH)
    assert verification["tests_passed"] == 12
    for key in ("count_source", "count_mismatch", "junit_rejected"):
        assert key not in verification


# ---------------------------------------------------------------------------
# PR-5: the envelope sub-path reads the same sidecar with the same rule
# ---------------------------------------------------------------------------

def _path1_verification(report: Path) -> Dict[str, Any]:
    """Path 1 exactly as ``envelope_govern`` calls it."""
    from envelope_govern_support import _verification_from_report
    return _verification_from_report(report)


def _write_report_and_sidecar(tmp_path: Path, verification: str,
                              **xml_kwargs: Any) -> Path:
    report = tmp_path / f"{DISPATCH}.md"
    report.write_text(_report_body(verification), encoding="utf-8")
    (tmp_path / f"{DISPATCH}.junit.xml").write_text(_xml_body(**xml_kwargs), encoding="utf-8")
    return report


def test_envelope_subpath_prefers_xml_over_the_text_counts(tmp_path: Path,
                                                           monkeypatch) -> None:
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "1")
    report = _write_report_and_sidecar(
        tmp_path, TEXT_12_PASSED, tests=5, failures=1, errors=0, skipped=1)
    verification = _path1_verification(report)
    assert verification["count_source"] == "junit_worker"
    assert verification["tests_passed"] == 3
    assert verification["tests_failed"] >= 1
    assert verification["count_mismatch"] is True


def test_envelope_subpath_without_a_sidecar_keeps_the_text_counts(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "1")
    report = tmp_path / f"{DISPATCH}.md"
    report.write_text(_report_body(TEXT_12_PASSED), encoding="utf-8")
    verification = _path1_verification(report)
    assert verification["tests_passed"] == 12
    assert verification["count_source"] == "report_text"
    assert "junit_rejected" not in verification


def test_envelope_subpath_refuses_a_sidecar_named_for_another_suite(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "1")
    report = _write_report_and_sidecar(tmp_path, TEXT_12_PASSED, name="pytest")
    verification = _path1_verification(report)
    assert verification["tests_passed"] == 12
    assert verification["count_source"] == "report_text"
    assert "does not equal" in verification["junit_rejected"]


def test_both_write_paths_yield_one_verification(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "1")
    report = _write_report_and_sidecar(
        tmp_path, TEXT_2_FAILED, tests=8, failures=0, errors=0, skipped=0)
    path1 = _path1_verification(report)
    path2 = ReportParser().parse_report(str(report))["verification"]
    assert path1 == path2
    assert path1["count_source"] == "junit_worker"
    assert path1["count_mismatch"] is True


def test_setting_off_both_paths_add_no_new_key(tmp_path: Path, monkeypatch) -> None:
    from verification_runs import verification_record
    monkeypatch.setenv("VNX_JUNIT_COUNTS", "0")
    body = _report_body(TEXT_12_PASSED)
    report = tmp_path / f"{DISPATCH}.md"
    report.write_text(body, encoding="utf-8")
    (tmp_path / f"{DISPATCH}.junit.xml").write_text(_xml_body(), encoding="utf-8")
    expected = verification_record(ReportParser().extract_validation(body), DISPATCH)
    for verification in (_path1_verification(report),
                         ReportParser().parse_report(str(report))["verification"]):
        assert verification == expected
        for key in ("count_source", "count_mismatch", "junit_rejected"):
            assert key not in verification


# ---------------------------------------------------------------------------
# Compatibility: the new keys change no verdict and no state reading
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra", [
    {"count_source": "junit_worker", "count_mismatch": True},
    {"count_source": "report_text", "junit_rejected": "testsuite name 'pytest' does not equal"},
], ids=["junit_worker", "report_text_rejected"])
def test_new_count_keys_change_no_verdict_and_no_state_reading(extra: Dict[str, Any]) -> None:
    from receipt_outcome import summarize
    from receipt_verdict import compute_verdict
    base = {"method": "pytest", "tests_run": 5, "tests_passed": 3, "tests_failed": 1,
            "command": None, "pr_ref": None, "push_verified": None, "spec_deviation": None}
    with_keys = {**base, **extra}

    plain = compute_verdict({"status": "success", "verification": base})
    assert compute_verdict({"status": "success", "verification": with_keys}) == plain

    writer_a = {"dispatch_id": "d1", "event_type": "task_complete", "status": "success",
                "receipt_kind": "dispatch", "timestamp": "2026-10-25T10:00:00Z"}
    plain_state = summarize([writer_a, dict(writer_a, status="done", report_file="d1.md",
                                            timestamp="2026-10-25T10:01:00Z",
                                            verification=base)], project_id="proj-a")
    keyed_state = summarize([writer_a, dict(writer_a, status="done", report_file="d1.md",
                                            timestamp="2026-10-25T10:01:00Z",
                                            verification=with_keys)], project_id="proj-a")
    assert keyed_state["verdict_counts"] == plain_state["verdict_counts"]
    assert (keyed_state["outcomes"][0]["decision"]
            == plain_state["outcomes"][0]["decision"])
