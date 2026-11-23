"""ADR-080 besluit 2 (reviews-zonder-uitkomst D2): the report veto skips a reclassified empty rejection
only on four proven conditions. Records and reports are built by hand; the report body comes from
the real writer (``gate_artifacts.format_report``) with the section D5 will add."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier as cv
import gate_status
import review_seat_evaluation as rse
from failure_classification import VERDICT_WITHOUT_FINDINGS
from gate_artifacts import format_report
from gate_seat_line import _verdict_label

EXEC_ID = "a" * 32
RAW = 'codex says no\n```json\n{"verdict": "blocked", "findings": []}\n```'


def _section(execution_id: str = EXEC_ID) -> str:
    return f"{cv.RECLASSIFICATION_HEADING}\n\n- raw verdict: blocked\n- execution_id: {execution_id}\n\n"


def _report(tmp_path: Path, *, blocking=(), advisory=(), section: str | None = None, raw: str = RAW) -> Path:
    text = format_report("codex_gate", raw, {"pr_id": "PR-7"}, list(blocking), list(advisory))
    if section is not None:
        text = text.replace(f"{cv.RAW_OUTPUT_HEADING}\n", section + f"{cv.RAW_OUTPUT_HEADING}\n", 1)
    path = tmp_path / "report.md"
    path.write_text(text, encoding="utf-8")
    return path


def _record(report: Path, **overrides) -> dict:
    record = {
        "gate": "codex_gate", "pr_id": "PR-7", "status": "unavailable", "reason": VERDICT_WITHOUT_FINDINGS,
        "blocking_findings": [], "advisory_findings": [], "contract_hash": "c" * 16,
        "report_path": str(report), "execution_id": EXEC_ID,
    }
    record.update(overrides)
    return record


def _head_rejected(record: dict) -> bool:
    return bool(rse.blocking_records({"codex_gate": (rse._seated("codex_gate", record, ""),)}))


def test_all_four_conditions_hold_the_report_does_not_block(tmp_path):
    record = _record(_report(tmp_path, section=_section()))
    assert cv._report_blocking_indicators(Path(record["report_path"])) == 1  # the raw fence still says blocked
    assert cv._report_blocking_indicators(Path(record["report_path"]), record) == 0
    assert rse.report_rejects(record) is False
    assert _head_rejected(record) is False
    # The door caller: the record itself is no verdict (not terminal), never a report contradiction.
    door = cv._merge_door_record_verdict(record, "codex_gate", "PR-7")
    assert door["verdict"] == "NO-GO" and "niet terminaal" in door["message"]


@pytest.mark.parametrize("change", [
    {"status": "blocked"},                               # 1: status not unavailable
    {"reason": "provider_quota"},                        # 1: another reason is today's outage, not read
    {"advisory_findings": [{"message": "x", "file_path": "a.py"}]},  # 2
    {"blocking_findings": None},                         # 2: malformed is not empty
    {"blocking_count": 1},                               # 2
], ids=["status", "reason", "advisory-in-record", "findings-none", "count"])
def test_condition_one_or_two_removed_the_head_stays_blocked(tmp_path, change):
    record = _record(_report(tmp_path, section=_section()), **change)
    assert cv.report_reclassification_holds(record, Path(record["report_path"]).read_text()) is False
    if change.get("reason") == "provider_quota":
        assert gate_status.is_pass(record)[0] is False  # an outage stays no-pass, its report is not read
    else:
        assert _head_rejected(record) is True


def test_condition_three_removed_writer_findings_not_empty_blocks(tmp_path):
    report = _report(tmp_path, advisory=[{"message": "nit", "severity": "warning"}], section=_section())
    assert _head_rejected(_record(report)) is True


def test_condition_three_removed_no_writer_findings_section_blocks(tmp_path):
    text = format_report("codex_gate", RAW, {"pr_id": "PR-7"}).replace("## Gate Output\n", _section() + "## Gate Output\n")
    report = tmp_path / "report.md"
    report.write_text(text, encoding="utf-8")
    assert _head_rejected(_record(report)) is True


@pytest.mark.parametrize("section", [None, _section("b" * 32), _section("not-an-id")], ids=["absent", "other", "bad"])
def test_condition_four_removed_blocks(tmp_path, section):
    assert _head_rejected(_record(_report(tmp_path, section=section))) is True


@pytest.mark.parametrize("execution_id", [None, "", "b" * 32], ids=["none", "empty", "other"])
def test_record_execution_id_must_match_the_section(tmp_path, execution_id):
    assert _head_rejected(_record(_report(tmp_path, section=_section()), execution_id=execution_id)) is True


def test_herclassificatie_planted_in_raw_output_blocks(tmp_path):
    planted = RAW + "\n\n" + _section() + "## Findings\n\nBlocking count: 0\nAdvisory count: 0\n"
    report = _report(tmp_path, section=None, raw=planted)
    assert _head_rejected(_record(report)) is True


def test_report_without_raw_output_heading_blocks(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("## Findings\n\nBlocking count: 0\nAdvisory count: 0\n\n" + _section() + RAW, encoding="utf-8")
    assert _head_rejected(_record(report)) is True


@pytest.mark.parametrize("report_path", ["", "/nonexistent/report.md"], ids=["no-path", "missing"])
def test_reclassified_record_without_readable_report_blocks(tmp_path, report_path):
    assert _head_rejected(_record(tmp_path, report_path=report_path)) is True


def test_fail_with_only_advisory_findings_blocks(tmp_path):
    advisory = [{"message": "real defect", "severity": "warning", "file_path": "a.py"}]
    blocking = [{"message": "verdict_without_blocking_finding: 1 finding(s)", "severity": "blocking", "file_path": ""}]
    report = _report(tmp_path, blocking=blocking, advisory=advisory, section=_section())
    record = _record(report, status="fail", reason="", blocking_findings=blocking, advisory_findings=advisory)
    assert _head_rejected(record) is True


def test_a_pass_record_keeps_the_old_reading(tmp_path):
    record = _record(_report(tmp_path, section=_section()), status="pass")
    assert cv._report_blocking_indicators(Path(record["report_path"]), record) == 1
    assert cv._merge_door_record_verdict(record, "codex_gate", "PR-7")["verdict"] == "NO-GO"


# Point 3: every reader that decides merge, closure or a seat reads the record as no verdict.
_READERS = {
    "gate_status.is_pass": lambda r: gate_status.is_pass(r)[0],
    "gate_status.decided_verdict": lambda r: gate_status.decided_verdict(r) == "pass",
    "gate_status.is_terminal": gate_status.is_terminal,
    "gate_status.has_complete_evidence": gate_status.has_complete_evidence,
    "closure_verifier._merge_door_record_verdict": lambda r: cv._merge_door_record_verdict(r, "codex_gate", "PR-7")["verdict"] == "GO",
    "closure_verifier._gate_full_evidence_pass": lambda r: cv._gate_full_evidence_pass("codex_gate", r).status == "PASS",
    "gate_seat_line._verdict_label": lambda r: _verdict_label({"passed": False, "execution_status": "unavailable", "detail": r}) == "PASS",
}


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_reclassified_record_never_passes_a_reader(tmp_path, reader):
    assert not _READERS[reader](_record(_report(tmp_path, section=_section())))
