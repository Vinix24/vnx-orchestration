"""ADR-045 audit-spoor-kern K-8: a v3 dispatch's report is booked as ``report_ingested``.

Dispatch-ID: dlv-304f581cc664

The converter and the Bash receipt processor's parser both turn a worker report
into a ledger line. K-8 makes that line depend on ONE decision
(``report_lifecycle_mode.decide``): a ``legacy`` dispatch keeps its
task-shaped receipt byte for byte, a ``v3`` dispatch gets exactly one
``report_ingested`` and never a ``task_complete`` — on any path, including the
contract-invalid and kill paths.

Both stores are real (``runtime_coordination.db`` + ``t0_receipts.ndjson``) and
live in ``tmp_path``; the store fixtures and the hermetic append hook are the
K-4a ones, imported not copied, so the row and the start line are seeded exactly
as the hand-over writes them. End lines are written only by
``dispatch_lifecycle.finalize`` — never by this test's own hands, and never by
the code under test.

One block per K-8 acceptance point:

1. a v3 report before the end line carries ``end_pending: true`` and keeps every
   task-shaped payload field; the later end line does not rewrite it;
2. a v3 report after the end line carries that end line's ``outcome_id``;
3. the same two shapes through ``report_parser.py``;
4. the decision: a v3 start line wins over a legacy row, a v3 row needs no start
   line, and a v3 answer survives the setting being turned off;
5. the fail direction: no receipt at all, a loud refusal naming the dispatch,
   and the report left retryable;
6. one ``report_ingested`` per report, also through the converter's hot-path
   guard;
7. legacy dispatches keep the K-4c shape, field for field;
8. an unreadable source refuses (`lifecycle_mode_undecidable`) even when the
   other one reads, because either can hide a v3 start behind a legacy answer;
9. a late report keeps the attempt that was running when it was WRITTEN (its
   file mtime, never the scan time), and an ambiguous attribution refuses;
10. the same two answers through ``report_parser.py``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import dispatch_lifecycle
import dispatch_start
import report_parser
import vnx_paths
from append_receipt_internals import payload as payload_module
from coordination_db import db_path_from_state_dir, get_connection
from report_lifecycle_mode import ReportPathRefused, decide, report_line
from report_to_receipt_converter import (
    _convert_one_detailed,
    build_receipt_from_report,
)

# The K-4a store: same schema, same row/start-line seeding, same hermetic append
# hook (the append layer stays real; only its process-spawning hooks are muted).
from test_k4a_dispatch_start_core import (  # noqa: F401
    PROJECT,
    _hermetic_append,
    _insert_row,
    _lines,
    legacy,
    spec_file,
    state_dir,
    v3,
)

DISPATCH = "d-k8"
ROLE = "backend-developer"
BASE_SHA = "a" * 40
ATTEMPT = 1

REPORT_NAME = "d-k8.md"

_END_LINE = {
    "timestamp": "2026-11-03T12:00:00Z",
    "event_type": "dispatch_finished",
    "project_id": PROJECT,
    "dispatch_id": DISPATCH,
    "attempt": ATTEMPT,
    "end_state": "completed",
    "head_sha": "c" * 40,
    "model": "deepseek-flash",
}

_BODY = """# K-8 report

**Dispatch-ID**: {dispatch_id}
**Model**: deepseek-flash
**Provider**: deepseek-harness
**Status**: {status}
**Timestamp**: 2026-11-03T09:00:00Z

## Summary

Booked the worker report as one report_ingested line for a v3 dispatch, with a
payload long enough to satisfy the mandatory report body contract.

## Changes

- scripts/lib/report_lifecycle_mode.py: the one path decision

## Verification

pytest tests/test_k8_report_ingested.py: passing

## Open Items

None
"""


@pytest.fixture(autouse=True)
def _tenant(monkeypatch):
    """The store's tenant, as the door resolves it in production (ADR-007)."""
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)


@pytest.fixture()
def report(tmp_path: Path) -> Path:
    path = tmp_path / "reports" / REPORT_NAME
    path.parent.mkdir()
    path.write_text(_BODY.format(dispatch_id=DISPATCH, status="unknown"), encoding="utf-8")
    return path


@pytest.fixture()
def v3_store(state_dir, spec_file, v3):
    """A v3 hand-over as K-4a writes it: the row, then the start line."""
    _insert_row(state_dir, dispatch_id=DISPATCH)
    dispatch_start.begin(
        project_id=PROJECT, dispatch_id=DISPATCH, role=ROLE, base_sha=BASE_SHA,
        spec_file=spec_file, target_slot="T1", state_dir=state_dir,
    )
    return state_dir


def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _append(state_dir: Path, line: dict):
    """Book a line the way the converter does: real append layer, no enrichment."""
    return payload_module.append_receipt_payload(
        dict(line), receipts_file=str(_ledger(state_dir)), skip_enrichment=True)


def _events(state_dir: Path, event_type: str) -> list:
    return [line for line in _lines(state_dir) if line.get("event_type") == event_type]


def _finalize(state_dir: Path) -> dict:
    """End attempt 1 via the real finalizer; return the end line it appended."""
    dispatch_lifecycle.finalize(
        state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH, attempt=ATTEMPT,
        end_state="completed", end_line=dict(_END_LINE),
    )
    endings = _events(state_dir, "dispatch_finished")
    assert len(endings) == 1
    return endings[0]


def _set_metadata(state_dir: Path, metadata: dict) -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "UPDATE dispatches SET metadata_json = ? WHERE dispatch_id = ? AND project_id = ?",
            (json.dumps(metadata), DISPATCH, PROJECT))
        conn.commit()


def _expected(task_shaped: dict, **deltas) -> dict:
    """The task-shaped receipt with only the lifecycle deltas applied."""
    expected = dict(task_shaped)
    expected["event_type"] = "report_ingested"
    if "event" in expected:
        expected["event"] = "report_ingested"
    expected.update(deltas)
    return expected


def _set_mtime(path: Path, iso: str) -> None:
    """Pin the report's write time: the clock the attempt rule reads (never "now")."""
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    os.utime(path, (stamp, stamp))


def _write_report(directory: Path, name: str) -> Path:
    """A second contract-valid report for the same dispatch (another attempt's file)."""
    path = directory / name
    path.write_text(_BODY.format(dispatch_id=DISPATCH, status="unknown"), encoding="utf-8")
    return path


def _booked(state_dir: Path, attempt: int) -> list:
    return [line for line in _events(state_dir, "report_ingested")
            if line.get("attempt") == attempt]


def _ended(state_dir: Path, attempt: int) -> dict:
    endings = [line for line in _events(state_dir, "dispatch_finished")
               if line.get("attempt") == attempt]
    assert len(endings) == 1, endings
    return endings[0]


@pytest.fixture()
def two_attempts(state_dir, spec_file, v3, monkeypatch):
    """Attempt 1 started 08:00 and ended; attempt 2 started 10:00 (still running).

    The real hand-over and the real finalizer write both lines; only the clock
    they stamp them with is pinned, so the fixture does not hand-write a single
    lifecycle line.
    """
    _insert_row(state_dir, dispatch_id=DISPATCH)

    def _begin(clock: str) -> None:
        monkeypatch.setattr(dispatch_start, "_now", lambda: clock)
        dispatch_start.begin(
            project_id=PROJECT, dispatch_id=DISPATCH, role=ROLE, base_sha=BASE_SHA,
            spec_file=spec_file, target_slot="T1", state_dir=state_dir,
        )

    _begin("2026-11-03T08:00:00Z")
    dispatch_lifecycle.finalize(
        state_dir=state_dir, project_id=PROJECT, dispatch_id=DISPATCH, attempt=1,
        end_state="completed", end_line=dict(_END_LINE),
    )
    _begin("2026-11-03T10:00:00Z")
    return state_dir


# ---------------------------------------------------------------------------
# 1. a v3 report before the end line
# ---------------------------------------------------------------------------

def test_v3_report_before_the_end_line_is_end_pending_and_keeps_the_payload(
        report, state_dir, spec_file, v3):
    """The same report, the same store: task_complete before the hand-over, one
    report_ingested after it, and no payload field lost in between."""
    _insert_row(state_dir, dispatch_id=DISPATCH)
    text = report.read_text(encoding="utf-8")
    legacy_line = build_receipt_from_report(report, text, state_dir=state_dir)
    assert legacy_line["event_type"] == "task_complete"

    dispatch_start.begin(
        project_id=PROJECT, dispatch_id=DISPATCH, role=ROLE, base_sha=BASE_SHA,
        spec_file=spec_file, target_slot="T1", state_dir=state_dir,
    )
    line = build_receipt_from_report(report, text, state_dir=state_dir)

    assert line == _expected(legacy_line, project_id=PROJECT, attempt=ATTEMPT,
                             end_pending=True)
    assert "end_outcome_id" not in line
    assert set(line) - set(legacy_line) == {"project_id", "attempt", "end_pending"}


def test_the_later_end_line_does_not_rewrite_the_report_line(report, v3_store):
    text = report.read_text(encoding="utf-8")
    _append(v3_store, build_receipt_from_report(report, text, state_dir=v3_store))

    end = _finalize(v3_store)

    reports = _events(v3_store, "report_ingested")
    assert len(reports) == 1
    assert len(_events(v3_store, "dispatch_finished")) == 1
    # ADR-045 §2: the report line is never rewritten to add the link later
    assert reports[0]["end_pending"] is True
    assert "end_outcome_id" not in reports[0]
    assert reports[0]["attempt"] == end["attempt"] == ATTEMPT
    assert not _events(v3_store, "task_complete")


# ---------------------------------------------------------------------------
# 2. a v3 report after the end line
# ---------------------------------------------------------------------------

def test_v3_report_after_the_end_line_links_that_end_outcome(report, v3_store):
    text = report.read_text(encoding="utf-8")
    end = _finalize(v3_store)
    assert end.get("outcome_id"), end

    line = build_receipt_from_report(report, text, state_dir=v3_store)

    assert line["event_type"] == "report_ingested"
    assert line["end_outcome_id"] == end["outcome_id"]
    assert "end_pending" not in line
    assert line["project_id"] == PROJECT and line["attempt"] == ATTEMPT
    assert line["status"] == "unknown" and line["model"] == "deepseek-flash"
    _append(v3_store, line)
    reports = _events(v3_store, "report_ingested")
    assert len(reports) == 1
    assert reports[0]["end_outcome_id"] == end["outcome_id"]
    assert not _events(v3_store, "task_complete")


# ---------------------------------------------------------------------------
# 3. the same two shapes through report_parser
# ---------------------------------------------------------------------------

@pytest.fixture()
def parser(state_dir, tmp_path, monkeypatch):
    dispatches = tmp_path / "dispatches" / "completed"
    dispatches.mkdir(parents=True)
    monkeypatch.setattr(vnx_paths, "ensure_env", lambda: {
        "VNX_HOME": str(VNX_ROOT),
        "VNX_DISPATCH_DIR": str(tmp_path / "dispatches"),
        "VNX_DATA_DIR": str(tmp_path / "data"),
        "VNX_STATE_DIR": str(state_dir),
    })
    return report_parser.ReportParser()


def test_report_parser_books_end_pending_before_the_end_line(
        report, v3_store, parser):
    receipt = parser.parse_report(str(report))

    assert receipt["event_type"] == "report_ingested"
    assert receipt["event"] == "report_ingested"
    assert receipt["project_id"] == PROJECT
    assert receipt["attempt"] == ATTEMPT
    assert receipt["end_pending"] is True
    assert "end_outcome_id" not in receipt
    assert "status" in receipt  # the report's own payload survives the retype
    assert receipt["model"] == "deepseek-flash"
    assert receipt["receipt_kind"] == "dispatch"
    _append(v3_store, receipt)
    assert not _events(v3_store, "task_complete")


def test_report_parser_links_the_end_line_when_it_is_there(
        report, v3_store, parser):
    end = _finalize(v3_store)

    receipt = parser.parse_report(str(report))

    assert receipt["event_type"] == "report_ingested"
    assert receipt["end_outcome_id"] == end["outcome_id"]
    assert "end_pending" not in receipt


# ---------------------------------------------------------------------------
# 3b. every other task-shaped path is a report line too
# ---------------------------------------------------------------------------

_VARIANT_HEADER = """# K-8 variant

**Dispatch-ID**: d-k8
**Model**: deepseek-flash
**Provider**: deepseek-harness
**Status**: {status}
**Timestamp**: 2026-11-03T09:00:00Z

"""

_FULL_BODY = """## Summary

A summary long enough to satisfy the mandatory report body contract rules here.

## Changes

- a

## Verification

- b

## Open Items

None
"""

_SHORT_BODY = """## Changes

- a
"""


def _variant(tmp_path: Path, name: str, status: str, body: str) -> Path:
    path = tmp_path / "reports" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(_VARIANT_HEADER.format(status=status) + body, encoding="utf-8")
    return path


def test_a_v3_kill_report_is_a_report_line_not_task_failed(tmp_path, v3_store):
    """The kill path (a declared failure) chooses task_failed for legacy."""
    report = _variant(tmp_path, "kill.md", "failed", _FULL_BODY)
    text = report.read_text(encoding="utf-8")
    legacy_line = build_receipt_from_report(report, text, state_dir=tmp_path / "no-store")
    assert legacy_line["event_type"] == "task_failed"

    line = build_receipt_from_report(report, text, state_dir=v3_store)

    assert line["event_type"] == "report_ingested"
    assert line["status"] == "failed"  # the kill signal is not lost
    assert line["end_pending"] is True
    assert not _events(v3_store, "task_failed")


def test_a_v3_contract_invalid_report_is_a_report_line(tmp_path, v3_store):
    """The contract-invalid path (a non-success claim plus a broken body)."""
    report = _variant(tmp_path, "contract.md", "unknown", _SHORT_BODY)
    text = report.read_text(encoding="utf-8")
    legacy_line = build_receipt_from_report(report, text, state_dir=tmp_path / "no-store")
    assert legacy_line["event_type"] == "report_contract_invalid"

    line = build_receipt_from_report(report, text, state_dir=v3_store)

    assert line["event_type"] == "report_ingested"
    assert line["status"] == "contract_invalid"
    assert line["contract_violations"] == legacy_line["contract_violations"]
    assert not _events(v3_store, "report_contract_invalid")


def test_a_v3_fail_closed_rejection_is_a_report_line(tmp_path, v3_store, monkeypatch):
    """A success claim that fails the fail-closed checks (task_failed today)."""
    report = _variant(tmp_path, "failclosed.md", "success", _SHORT_BODY)
    text = report.read_text(encoding="utf-8")
    # ``git ls-remote`` is an external process; the branch check is not what
    # this test measures (the frontmatter and body checks alone reject already).
    monkeypatch.setattr(
        "report_to_receipt_converter._check_branch_on_origin", lambda dispatch_id: True)
    legacy_line = build_receipt_from_report(report, text, state_dir=tmp_path / "no-store")
    assert legacy_line["event_type"] == "task_failed"

    line = build_receipt_from_report(report, text, state_dir=v3_store)

    assert line["event_type"] == "report_ingested"
    assert line["status"] == "failure"
    assert line["fail_closed_violations"] == legacy_line["fail_closed_violations"]


# ---------------------------------------------------------------------------
# 4. the one decision
# ---------------------------------------------------------------------------

def test_a_v3_start_line_wins_over_a_legacy_row(report, v3_store):
    _set_metadata(v3_store, {"lifecycle_mode": "legacy"})
    text = report.read_text(encoding="utf-8")

    line = build_receipt_from_report(report, text, state_dir=v3_store)

    assert line["event_type"] == "report_ingested"
    assert line["attempt"] == ATTEMPT


def test_a_v3_row_needs_no_start_line(report, state_dir, v3):
    _insert_row(state_dir, dispatch_id=DISPATCH, attempt_count=2,
                metadata=json.dumps({"lifecycle_mode": "v3"}))
    text = report.read_text(encoding="utf-8")

    line = build_receipt_from_report(report, text, state_dir=state_dir)

    assert line["event_type"] == "report_ingested"
    assert line["attempt"] == 2  # the row's attempt_count is the fallback
    assert line["end_pending"] is True
    assert not _lines(state_dir)  # building writes nothing


def test_a_v3_answer_survives_the_setting_being_turned_off(
        report, v3_store, monkeypatch):
    monkeypatch.delenv("VNX_LEDGER_LIFECYCLE", raising=False)
    text = report.read_text(encoding="utf-8")

    line = build_receipt_from_report(report, text, state_dir=v3_store)

    assert line["event_type"] == "report_ingested"


# ---------------------------------------------------------------------------
# 5. the fail direction: book nothing, loudly, retryable
# ---------------------------------------------------------------------------

def test_unreadable_row_and_ledger_refuses_and_books_nothing(
        report, state_dir, v3, tmp_path, caplog):
    ledger = _ledger(state_dir)
    ledger.mkdir()  # an unreadable ledger (I/O error, not an absent one)
    _insert_row(state_dir, dispatch_id=DISPATCH, metadata="{not json")

    with caplog.at_level("WARNING"):
        result, outcome = _convert_one_detailed(report, receipts_file=str(ledger))

    assert result is None and outcome == "error"  # "error" is never marked processed
    assert "REFUSED (ledger lifecycle)" in caplog.text
    assert DISPATCH in caplog.text and "lifecycle_mode_undecidable" in caplog.text
    assert ledger.is_dir() and not list(ledger.iterdir())
    assert report.exists()  # retryable: still where the next scan looks
    assert not (state_dir / "receipt_deadletter").exists()


def test_a_v3_dispatch_with_an_unreadable_ledger_refuses(
        report, state_dir, v3, caplog):
    _insert_row(state_dir, dispatch_id=DISPATCH,
                metadata=json.dumps({"lifecycle_mode": "v3"}))
    ledger = _ledger(state_dir)
    ledger.mkdir()

    with caplog.at_level("WARNING"):
        result, outcome = _convert_one_detailed(report, receipts_file=str(ledger))

    assert result is None and outcome == "error"
    assert "ledger_unreadable" in caplog.text and DISPATCH in caplog.text
    assert report.exists()


def test_a_v3_dispatch_with_an_unknown_attempt_refuses(report, state_dir, v3, caplog):
    _insert_row(state_dir, dispatch_id=DISPATCH, attempt_count=0,
                metadata=json.dumps({"lifecycle_mode": "v3"}))

    with caplog.at_level("WARNING"):
        result, outcome = _convert_one_detailed(
            report, receipts_file=str(_ledger(state_dir)))

    assert result is None and outcome == "error"
    assert "attempt_unknown" in caplog.text and DISPATCH in caplog.text
    assert _lines(state_dir) == []  # neither task_complete nor a partial line
    assert report.exists()


def test_report_parser_refuses_instead_of_returning_a_receipt(
        report, state_dir, v3, parser, capsys):
    _insert_row(state_dir, dispatch_id=DISPATCH, attempt_count=0,
                metadata=json.dumps({"lifecycle_mode": "v3"}))

    receipt = parser.parse_report(str(report))

    assert receipt["lifecycle_refusal"] == "attempt_unknown"
    assert receipt["dispatch_id"] == DISPATCH
    assert "REFUSED (ledger lifecycle)" in capsys.readouterr().err
    assert _lines(state_dir) == []


# ---------------------------------------------------------------------------
# 6. one report_ingested per report
# ---------------------------------------------------------------------------

def test_the_same_report_twice_books_one_report_ingested(report, v3_store):
    text = report.read_text(encoding="utf-8")
    first = _append(v3_store, build_receipt_from_report(report, text, state_dir=v3_store))
    second = _append(v3_store, build_receipt_from_report(report, text, state_dir=v3_store))

    assert first.status == "appended"
    assert second.status == "duplicate"
    assert len(_events(v3_store, "report_ingested")) == 1


def test_the_converter_does_not_book_a_second_report_line(report, v3_store):
    first, first_tag = _convert_one_detailed(report, receipts_file=str(_ledger(v3_store)))
    second, second_tag = _convert_one_detailed(report, receipts_file=str(_ledger(v3_store)))

    assert (first_tag, second_tag) == ("appended", "duplicate")
    assert first.status == "appended" and second.status == "duplicate"
    reports = _events(v3_store, "report_ingested")
    assert len(reports) == 1 and reports[0]["end_pending"] is True
    assert not _events(v3_store, "task_complete")


# ---------------------------------------------------------------------------
# 7. legacy keeps the K-4c shape
# ---------------------------------------------------------------------------

def test_a_legacy_row_changes_nothing(report, state_dir, legacy):
    """The K-4c head never read the row for this decision; neither does K-8."""
    text = report.read_text(encoding="utf-8")
    without_row = build_receipt_from_report(report, text, state_dir=state_dir)

    _insert_row(state_dir, dispatch_id=DISPATCH,
                metadata=json.dumps({"lifecycle_mode": "legacy"}))
    with_row = build_receipt_from_report(report, text, state_dir=state_dir)

    assert with_row == without_row
    assert with_row["event_type"] == "task_complete"
    assert with_row["status"] == "unknown"
    assert {"project_id", "attempt", "end_pending", "end_outcome_id"}.isdisjoint(with_row)


def test_without_a_store_the_public_builder_is_unchanged(report, legacy):
    text = report.read_text(encoding="utf-8")

    shipped = build_receipt_from_report(report, text)

    assert shipped["event_type"] == "task_complete"
    assert shipped["dispatch_id"] == DISPATCH
    assert {"project_id", "attempt", "end_pending", "end_outcome_id"}.isdisjoint(shipped)


def test_a_store_without_a_database_or_ledger_answers_legacy(report, tmp_path):
    """An absent DB file and an absent row both count as READ: nothing hides v3."""
    bare = tmp_path / "bare-state"
    bare.mkdir()
    text = report.read_text(encoding="utf-8")

    with_row_or_not = build_receipt_from_report(report, text, state_dir=bare)

    assert with_row_or_not["event_type"] == "task_complete"
    assert with_row_or_not["status"] == "unknown"
    assert {"project_id", "attempt", "end_pending", "end_outcome_id"}.isdisjoint(with_row_or_not)
    assert not (bare / "t0_receipts.ndjson").exists()


# ---------------------------------------------------------------------------
# 8. an unreadable source refuses even when the other one reads (finding 1)
# ---------------------------------------------------------------------------

def test_an_unreadable_row_with_a_readable_empty_ledger_refuses(
        report, state_dir, v3, caplog):
    """An empty ledger reads as legacy, but an unreadable row could hide v3."""
    _insert_row(state_dir, dispatch_id=DISPATCH, metadata="{not json")
    assert _lines(state_dir) == []  # the ledger is readable and empty

    with caplog.at_level("WARNING"):
        result, outcome = _convert_one_detailed(
            report, receipts_file=str(_ledger(state_dir)))

    assert result is None and outcome == "error"
    assert "lifecycle_mode_undecidable" in caplog.text and DISPATCH in caplog.text
    assert _lines(state_dir) == []  # not a task_complete, not a partial line
    assert report.exists()  # retryable


def test_a_database_file_that_is_not_a_database_refuses(report, state_dir, v3):
    """A present but corrupt store is an unreadable row, never a legacy answer."""
    Path(db_path_from_state_dir(state_dir)).write_bytes(b"not a sqlite database")

    with pytest.raises(ReportPathRefused) as exc:
        build_receipt_from_report(report, report.read_text(encoding="utf-8"),
                                  state_dir=state_dir)

    assert exc.value.code == "lifecycle_mode_undecidable"
    assert exc.value.dispatch_id == DISPATCH


def test_an_unreadable_ledger_with_a_readable_legacy_row_refuses(
        report, state_dir, v3, caplog):
    """A readable legacy row cannot outvote a ledger that might hold a v3 start."""
    _insert_row(state_dir, dispatch_id=DISPATCH,
                metadata=json.dumps({"lifecycle_mode": "legacy"}))
    ledger = _ledger(state_dir)
    ledger.mkdir()

    with caplog.at_level("WARNING"):
        result, outcome = _convert_one_detailed(report, receipts_file=str(ledger))

    assert result is None and outcome == "error"
    assert "lifecycle_mode_undecidable" in caplog.text and DISPATCH in caplog.text
    assert list(ledger.iterdir()) == []
    assert report.exists()


# ---------------------------------------------------------------------------
# 9. the report belongs to the attempt that was running when it was WRITTEN
# ---------------------------------------------------------------------------

def test_a_late_report_of_attempt_one_stays_attempt_one(report, two_attempts):
    """Attempt 1's report, ingested after attempt 2 started, is still attempt 1 —
    and attempt 2's own report is a second line, never a duplicate of it."""
    _set_mtime(report, "2026-11-03T09:00:00Z")  # between start 1 (08:00) and start 2 (10:00)
    first, first_tag = _convert_one_detailed(
        report, receipts_file=str(_ledger(two_attempts)))

    assert (first.status, first_tag) == ("appended", "appended")
    booked = _booked(two_attempts, 1)
    assert len(booked) == 1
    assert booked[0]["end_outcome_id"] == _ended(two_attempts, 1)["outcome_id"]
    assert "end_pending" not in booked[0]

    second_report = _write_report(report.parent, "d-k8-attempt2.md")
    _set_mtime(second_report, "2026-11-03T11:00:00Z")  # after start 2
    second, second_tag = _convert_one_detailed(
        second_report, receipts_file=str(_ledger(two_attempts)))

    assert (second.status, second_tag) == ("appended", "appended")
    assert [line["attempt"] for line in _events(two_attempts, "report_ingested")] == [1, 2]
    assert _booked(two_attempts, 2)[0]["end_pending"] is True


def test_a_receipt_that_names_its_attempt_wins_over_the_latest_start(report, two_attempts):
    """An explicit attempt on the receipt is used even when a later start exists."""
    _set_mtime(report, "2026-11-03T11:00:00Z")  # the time rule alone would say attempt 2
    receipt = {
        "dispatch_id": DISPATCH, "project_id": PROJECT, "attempt": 1,
        "event_type": "task_complete", "event": "task_complete", "status": "unknown",
        "report_path": str(report), "model": "deepseek-flash",
    }

    line = report_line(receipt, state_dir=two_attempts)

    assert line["event_type"] == "report_ingested"
    assert line["attempt"] == 1
    assert line["end_outcome_id"] == _ended(two_attempts, 1)["outcome_id"]


def test_a_receipt_attempt_without_a_start_line_refuses(report, state_dir, v3):
    """v3 from the row alone, but the receipt names an attempt nobody started."""
    _insert_row(state_dir, dispatch_id=DISPATCH, attempt_count=2,
                metadata=json.dumps({"lifecycle_mode": "v3"}))
    receipt = {
        "dispatch_id": DISPATCH, "project_id": PROJECT, "attempt": 2,
        "event_type": "task_complete", "report_path": str(report),
    }

    with pytest.raises(ReportPathRefused) as exc:
        report_line(receipt, state_dir=state_dir)

    assert exc.value.code == "attempt_unknown"
    assert _lines(state_dir) == []


def test_several_starts_without_a_usable_report_time_refuse(two_attempts, report):
    """Attempts 1 and 2 started; the report's own write time is unknown."""
    with pytest.raises(ReportPathRefused) as direct:
        decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=two_attempts)

    assert direct.value.code == "attempt_ambiguous"

    receipt = {
        "dispatch_id": DISPATCH, "project_id": PROJECT,
        "event_type": "task_complete", "report_path": str(report.with_name("gone.md")),
    }

    with pytest.raises(ReportPathRefused) as exc:
        report_line(receipt, state_dir=two_attempts)

    assert exc.value.code == "attempt_ambiguous"
    assert not _events(two_attempts, "report_ingested")


def test_a_report_written_before_every_start_refuses(two_attempts):
    """A write time earlier than the first start belongs to no attempt."""
    with pytest.raises(ReportPathRefused) as exc:
        decide(project_id=PROJECT, dispatch_id=DISPATCH, state_dir=two_attempts,
               report_time="2026-11-03T07:00:00Z")

    assert exc.value.code == "attempt_ambiguous"


def test_the_single_started_attempt_needs_no_report_time(report, v3_store):
    """One start: the answer is that attempt, whatever the report's time is."""
    _set_mtime(report, "2026-11-03T23:59:00Z")

    line = build_receipt_from_report(
        report, report.read_text(encoding="utf-8"), state_dir=v3_store)

    assert line["event_type"] == "report_ingested"
    assert line["attempt"] == ATTEMPT
    assert line["end_pending"] is True


# ---------------------------------------------------------------------------
# 10. the same two answers through report_parser
# ---------------------------------------------------------------------------

def test_report_parser_books_the_row_attempt_for_a_v3_dispatch(
        report, state_dir, v3, parser):
    _insert_row(state_dir, dispatch_id=DISPATCH, attempt_count=2,
                metadata=json.dumps({"lifecycle_mode": "v3"}))

    receipt = parser.parse_report(str(report))

    assert receipt["event_type"] == "report_ingested"
    assert receipt["attempt"] == 2
    assert receipt["end_pending"] is True


def test_report_parser_refuses_an_unreadable_row_with_a_readable_empty_ledger(
        report, state_dir, v3, parser, capsys):
    _insert_row(state_dir, dispatch_id=DISPATCH, metadata="{not json")
    assert _lines(state_dir) == []

    receipt = parser.parse_report(str(report))

    assert receipt["lifecycle_refusal"] == "lifecycle_mode_undecidable"
    assert receipt["dispatch_id"] == DISPATCH
    assert "REFUSED (ledger lifecycle)" in capsys.readouterr().err
    assert _lines(state_dir) == []
    assert report.exists()
