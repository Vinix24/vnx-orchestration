"""ADR-045 (audit-spoor-kern K-8b): the two writers that book a worker report.

Dispatch-ID: dlv-69af85a4952c

K-8a decided the path; nothing called it. This file drives the two writers that
do now — ``scripts/lib/report_to_receipt_converter.py`` and
``scripts/report_parser.py`` — plus the restore's guard, against real stores:

* the ledger and the row are written by the real ``dispatch_start.begin`` and
  ``dispatch_lifecycle.finalize`` (only ``_now`` is pinned for distinct start
  times) — no hand-written lifecycle line;
* reports are real files, and the write time that attributes an attempt is the
  file's own mtime set on disk;
* ``report_line`` / ``decide`` are never patched.

One block per acceptance rule of the K-8b dispatch.

Red run: written against the K-8a head (4528d08e), where both writers still book
``task_complete`` and ``ReportPathRefused`` does not exist yet; every test below
that expects ``report_ingested`` or a refusal fails there.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

VNX_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

import coordination_db  # noqa: E402
import dispatch_lifecycle as lifecycle  # noqa: E402
import dispatch_start  # noqa: E402
import project_id_migration  # noqa: E402
import report_parser  # noqa: E402
import report_to_receipt_converter as rtc  # noqa: E402
import restore_stranded_reports as restore  # noqa: E402
from append_receipt_internals import payload as payload_module  # noqa: E402
from coordination_db import get_connection  # noqa: E402

PROJECT = "proj-alpha"
DISPATCH = "d-k8b"
OTHER_DISPATCH = "d-k8b-other"
BASE_SHA = "a" * 40
ROLE = "backend-developer"
T0 = "2026-11-03T10:00:00Z"
T1 = "2026-11-03T11:00:00Z"
T2 = "2026-11-03T12:00:00Z"
T3 = "2026-11-03T13:00:00Z"

_RUNNER_PATH_0017 = SCRIPT_DIR / "lib" / "migrations" / "apply_0017.py"
_spec_0017 = _ilu.spec_from_file_location("apply_0017_k8b", _RUNNER_PATH_0017)
_mod_0017 = _ilu.module_from_spec(_spec_0017)
_spec_0017.loader.exec_module(_mod_0017)


def _epoch(zulu: str) -> float:
    return datetime.fromisoformat(zulu.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    """A real coordination store at the production shape (project_id + composite UNIQUE)."""
    sd = tmp_path / "state"
    sd.mkdir()
    coordination_db.init_schema(sd)
    db_path = coordination_db.db_path_from_state_dir(sd)
    project_id_migration.run_runtime_coordination_migration(db_path, default_project_id=PROJECT)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN")
        _mod_0017._rebuild_dispatches(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return sd


@pytest.fixture()
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "SPEC.md"
    path.write_text("# staged spec\n\nbody\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _tenant(monkeypatch):
    """The door exports VNX_PROJECT_ID; both writers resolve the tenant the same way."""
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)


@pytest.fixture()
def v3(monkeypatch):
    monkeypatch.setenv("VNX_LEDGER_LIFECYCLE", "1")


@pytest.fixture(autouse=True)
def _branch(monkeypatch):
    """The success-claim delivery check shells out to git; the report is always delivered here."""
    monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: True)


@pytest.fixture(autouse=True)
def _hermetic_append(monkeypatch):
    """Keep the append layer real, but never fire its process-spawning hooks (K-8a's rule)."""
    real = payload_module.append_receipt_payload

    def _append(receipt, **kwargs):
        kwargs.setdefault("skip_enrichment", True)
        return real(receipt, **kwargs)

    monkeypatch.setattr(payload_module, "append_receipt_payload", _append)
    monkeypatch.setattr(payload_module.facade, "_emit_dispatch_register", lambda receipt: False)


@pytest.fixture()
def clock(monkeypatch):
    """Only ``_now`` is pinned; every line is still written by ``begin``."""
    state = {"now": T0}
    monkeypatch.setattr(dispatch_start, "_now", lambda: state["now"])
    return state


# ---------------------------------------------------------------------------
# Report, lifecycle and ledger helpers
# ---------------------------------------------------------------------------

def _report_text(dispatch_id: str, *, project_id: str | None = PROJECT,
                 model: str = "claude-sonnet-4-6",
                 attempt: int | str | None = None) -> str:
    fm = {
        "schema_version": 1,
        "dispatch_id": dispatch_id,
        "provider": "claude",
        "sub_provider": "anthropic",
        "model": model,
        "terminal_id": "T1",
        "pool_id": "headless",
        "role": ROLE,
        "task_class": "implementation",
        "pr_id": "none",
        "duration_seconds": 12.5,
        "exit_code": 0,
        "token_usage": {"input": 1234, "output": 567, "cache_read": 89},
        "cost_usd": 0.0421,
        "route_decision": {"strategy": "default", "selected_provider": "claude",
                           "selected_model": "claude-sonnet-4-6", "reason": "primary route"},
        "status": "success",
        "terminal": "T1",
        "timestamp": "2026-11-03T09:00:00Z",
    }
    # Omitted rather than nulled: a report that names no tenant must reach the
    # writers exactly as legacy reports do (rule 4 of the K-8b round 2 dispatch).
    if project_id is not None:
        fm["project_id"] = project_id
    if attempt is not None:
        fm["attempt"] = attempt
    return (
        f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 42 passed\n\n"
        "## Open Items\n\nNone\n"
    )


def _write_report(path: Path, dispatch_id: str, *, project_id: str | None = PROJECT,
                  mtime: str | float | None = None, model: str = "claude-sonnet-4-6",
                  attempt: int | str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_report_text(dispatch_id, project_id=project_id, model=model,
                                 attempt=attempt),
                    encoding="utf-8")
    if mtime is not None:
        stamp = mtime if isinstance(mtime, float) else _epoch(mtime)
        os.utime(path, (stamp, stamp))
    return path


def _begin(state_dir: Path, spec_file: Path, *, dispatch_id: str = DISPATCH, **overrides):
    kwargs = dict(project_id=PROJECT, dispatch_id=dispatch_id, role=ROLE, base_sha=BASE_SHA,
                  spec_file=spec_file, target_slot="T1", state_dir=state_dir)
    kwargs.update(overrides)
    return dispatch_start.begin(**kwargs)


def _end_line(dispatch_id: str = DISPATCH, *, attempt: int = 1,
              timestamp: str = "2026-11-03T10:30:00Z") -> dict:
    return {
        "timestamp": timestamp, "event_type": "dispatch_finished", "project_id": PROJECT,
        "dispatch_id": dispatch_id, "attempt": attempt, "end_state": "completed",
        "head_sha": "c" * 40, "model": "deepseek-flash",
    }


def _finalize(state_dir: Path, dispatch_id: str = DISPATCH, **overrides):
    kwargs = dict(state_dir=state_dir, project_id=PROJECT, dispatch_id=dispatch_id,
                  attempt=1, end_state="completed", end_line=_end_line(dispatch_id))
    kwargs.update(overrides)
    return lifecycle.finalize(**kwargs)


def _ledger(state_dir: Path) -> Path:
    return state_dir / "t0_receipts.ndjson"


def _lines(state_dir: Path) -> list:
    ledger = _ledger(state_dir)
    if not ledger.exists():
        return []
    return [json.loads(raw) for raw in ledger.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _of_type(state_dir: Path, event_type: str, dispatch_id: str = DISPATCH) -> list:
    return [line for line in _lines(state_dir)
            if line.get("event_type") == event_type and line.get("dispatch_id") == dispatch_id]


def _insert_row(state_dir: Path, *, dispatch_id: str = DISPATCH, project_id: str = PROJECT,
                attempt_count: int = 1, metadata: str = "{}", state: str = "running") -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "INSERT INTO dispatches (dispatch_id, project_id, state, attempt_count, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (dispatch_id, project_id, state, attempt_count, metadata),
        )
        conn.commit()


def _set_row_metadata(state_dir: Path, metadata: dict, *, dispatch_id: str = DISPATCH,
                      project_id: str = PROJECT) -> None:
    with get_connection(state_dir) as conn:
        conn.execute(
            "UPDATE dispatches SET metadata_json = ? WHERE dispatch_id = ? AND project_id = ?",
            (json.dumps(metadata, sort_keys=True), dispatch_id, project_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Rule 1 / plan acceptance: the converter books one report_ingested
# ---------------------------------------------------------------------------

def test_v3_report_before_the_end_line_is_one_report_ingested_end_pending(
    state_dir, spec_file, v3, clock, tmp_path
):
    clock["now"] = T0
    _begin(state_dir, spec_file)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH, mtime=T1)

    _result, outcome = rtc._convert_one_detailed(
        report, receipts_file=str(_ledger(state_dir)))

    assert outcome == "appended"
    ingested = _of_type(state_dir, "report_ingested")
    assert len(ingested) == 1
    assert ingested[0]["end_pending"] is True
    assert "end_outcome_id" not in ingested[0]
    assert ingested[0]["project_id"] == PROJECT
    assert ingested[0]["attempt"] == 1
    assert _of_type(state_dir, "task_complete") == []
    assert _of_type(state_dir, "dispatch_finished") == []


def test_v3_report_after_the_end_line_links_that_dispatch_finished(
    state_dir, spec_file, v3, clock, tmp_path
):
    clock["now"] = T0
    _begin(state_dir, spec_file)
    _finalize(state_dir)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH, mtime=T1)

    _result, outcome = rtc._convert_one_detailed(
        report, receipts_file=str(_ledger(state_dir)))

    assert outcome == "appended"
    finished = _of_type(state_dir, "dispatch_finished")
    ingested = _of_type(state_dir, "report_ingested")
    assert len(finished) == 1
    assert len(ingested) == 1
    assert ingested[0]["end_outcome_id"] == finished[0]["outcome_id"]
    assert "end_pending" not in ingested[0]
    assert _of_type(state_dir, "task_complete") == []


# ---------------------------------------------------------------------------
# Legacy: byte-for-byte the K-8a line
# ---------------------------------------------------------------------------

def test_a_legacy_dispatch_books_the_same_task_complete_line(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()  # no coordination DB and no lifecycle line: legacy
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH, mtime=T1)
    text = report.read_text(encoding="utf-8")
    core = rtc._build_receipt_from_report_core(report, text, state_dir=sd)

    # The public builder's ONE lifecycle shaping point hands a legacy dispatch's
    # receipt back untouched: every core field survives, no lifecycle key is added.
    built = rtc.build_receipt_from_report(report, text, state_dir=sd)
    assert built["event_type"] == "task_complete"
    for key, value in core.items():
        assert built[key] == value, key
    for lifecycle_key in ("attempt", "end_pending", "end_outcome_id", "project_id"):
        assert lifecycle_key not in built

    _result, outcome = rtc._convert_one_detailed(
        report, receipts_file=str(_ledger(sd)))

    assert outcome == "appended"
    line = _lines(sd)[0]
    assert line["event_type"] == "task_complete"
    assert line["receipt_kind"] == "dispatch"
    assert line["dispatch_id"] == DISPATCH
    assert line["status"] == core["status"]
    for lifecycle_key in ("attempt", "end_pending", "end_outcome_id", "project_id"):
        assert lifecycle_key not in line


# ---------------------------------------------------------------------------
# Rule 2: the real destination
# ---------------------------------------------------------------------------

def test_convert_report_to_receipt_without_receipts_file_uses_vnx_state_dir(
    state_dir, spec_file, v3, clock, monkeypatch, tmp_path
):
    clock["now"] = T0
    _begin(state_dir, spec_file)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH, mtime=T1)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

    result = rtc.convert_report_to_receipt(report)

    assert result is not None and result.status == "appended"
    ingested = _of_type(state_dir, "report_ingested")
    assert len(ingested) == 1
    assert _of_type(state_dir, "task_complete") == []


# ---------------------------------------------------------------------------
# Rule 3: the write time of the file actually read
# ---------------------------------------------------------------------------

def test_the_read_files_mtime_attributes_the_attempt(state_dir, spec_file, v3, clock,
                                                     monkeypatch, tmp_path):
    clock["now"] = T0
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    clock["now"] = T2
    _begin(state_dir, spec_file)

    # The receipt's canonical report_path is an OLD file; the file actually read
    # was written between the two starts and belongs to attempt 1.
    data_dir = tmp_path / "data"
    (data_dir / "unified_reports").mkdir(parents=True)
    canonical = _write_report(data_dir / "unified_reports" / f"{DISPATCH}.md", DISPATCH,
                              mtime=_epoch(T0) - 3600)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    read = _write_report(tmp_path / "reports" / f"dispatch-{DISPATCH}.md", DISPATCH, mtime=T1)

    _result, outcome = rtc._convert_one_detailed(
        read, receipts_file=str(_ledger(state_dir)))

    assert outcome == "appended"
    ingested = _of_type(state_dir, "report_ingested")
    finished = _of_type(state_dir, "dispatch_finished")
    assert len(ingested) == 1
    assert ingested[0]["attempt"] == 1
    assert ingested[0]["end_outcome_id"] == finished[0]["outcome_id"]
    assert ingested[0]["report_path"] == str(canonical)
    assert _of_type(state_dir, "report_ingested")[0]["attempt"] != 2


# ---------------------------------------------------------------------------
# Rule 4: a refusal is named and books nothing
# ---------------------------------------------------------------------------

def test_an_unreadable_row_refuses_and_a_second_scan_after_repair_books(
    state_dir, v3, caplog, tmp_path
):
    # No v3 start line: neither readable source says v3, and the row cannot be
    # read -> the mode is undecidable and the decision refuses (K-8a's rule).
    _insert_row(state_dir, metadata="this is not json", state="running")
    reports = tmp_path / "reports"
    report = _write_report(reports / f"{DISPATCH}.md", DISPATCH, mtime=T1)
    caplog.set_level(logging.WARNING, logger=rtc.logger.name)

    stats = rtc.scan_and_convert([reports], state_dir=state_dir)

    assert stats.error_count == 1
    assert stats.new_count == 0
    assert _of_type(state_dir, "report_ingested") == []
    assert _of_type(state_dir, "task_complete") == []
    refused = [rec.getMessage() for rec in caplog.records
               if "lifecycle_mode_undecidable" in rec.getMessage()]
    assert refused, caplog.text
    assert any(DISPATCH in message for message in refused)
    assert rtc._compute_sha256(report) not in rtc._load_watermark(
        state_dir / rtc._WATERMARK_FILENAME)

    # Repair the row: it is now readable and says nothing about v3 -> legacy.
    _set_row_metadata(state_dir, {})

    stats_after = rtc.scan_and_convert([reports], state_dir=state_dir)

    assert stats_after.new_count == 1
    assert len(_of_type(state_dir, "task_complete")) == 1


def test_a_dry_run_refusal_writes_nothing(state_dir, v3, tmp_path):
    _insert_row(state_dir, metadata="not json either")
    reports = tmp_path / "reports"
    report = _write_report(reports / f"{DISPATCH}.md", DISPATCH, mtime=T1)
    before = report.read_bytes()

    stats = rtc.scan_and_convert([reports], state_dir=state_dir, dry_run=True)

    assert stats.error_count == 1
    assert stats.would_append_count == 0
    assert _of_type(state_dir, "report_ingested") == []
    assert report.read_bytes() == before
    assert not (state_dir / rtc._WATERMARK_FILENAME).exists() or \
        rtc._compute_sha256(report) not in rtc._load_watermark(
            state_dir / rtc._WATERMARK_FILENAME)


# ---------------------------------------------------------------------------
# Rule 5: the duplicate guard is attempt-exact
# ---------------------------------------------------------------------------

def test_the_duplicate_guard_is_attempt_exact(state_dir, spec_file, v3, clock, tmp_path):
    clock["now"] = T0
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    clock["now"] = T2
    _begin(state_dir, spec_file)
    reports = tmp_path / "reports"
    first = _write_report(reports / "attempt-1.md", DISPATCH, mtime=T1)
    second = _write_report(reports / "attempt-2.md", DISPATCH, mtime=T3)
    ledger = str(_ledger(state_dir))

    _r, outcome_one = rtc._convert_one_detailed(first, receipts_file=ledger)
    _r, outcome_two = rtc._convert_one_detailed(second, receipts_file=ledger)

    assert outcome_one == "appended"
    assert outcome_two == "appended"
    assert [line["attempt"] for line in _of_type(state_dir, "report_ingested")] == [1, 2]

    count = len(_lines(state_dir))
    _r, outcome_again = rtc._convert_one_detailed(first, receipts_file=ledger)
    assert outcome_again == "duplicate"
    assert len(_lines(state_dir)) == count

    # A dispatch_finished for another dispatch must never suppress its report line.
    clock["now"] = T0
    _begin(state_dir, spec_file, dispatch_id=OTHER_DISPATCH)
    _finalize(state_dir, dispatch_id=OTHER_DISPATCH)
    other_report = _write_report(reports / f"{OTHER_DISPATCH}.md", OTHER_DISPATCH, mtime=T1)

    _r, outcome_other = rtc._convert_one_detailed(other_report, receipts_file=ledger)

    assert outcome_other == "appended"
    assert len(_of_type(state_dir, "report_ingested", OTHER_DISPATCH)) == 1


# ---------------------------------------------------------------------------
# Rule 6: report_parser takes the same decision
# ---------------------------------------------------------------------------

def _parser_env(state_dir: Path, data_dir: Path, *, v3: bool = False) -> dict:
    env = dict(os.environ)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_PROJECT_ID"] = PROJECT
    env["VNX_LEDGER_LIFECYCLE"] = "1" if v3 else "0"
    return env


def _run_parser(report: Path, env: dict,
                cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "report_parser.py"), str(report)],
        capture_output=True, text=True, env=env, timeout=120, cwd=cwd,
    )


def test_parser_prints_one_report_ingested_for_a_v3_dispatch(state_dir, spec_file, v3,
                                                             clock, tmp_path):
    clock["now"] = T0
    _begin(state_dir, spec_file)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH, mtime=T1)

    run = _run_parser(report, _parser_env(state_dir, tmp_path / "data", v3=True))

    assert run.returncode == 0, run.stderr
    receipt = json.loads(run.stdout)
    assert receipt["event_type"] == "report_ingested"
    assert receipt["event"] == "report_ingested"
    assert receipt["dispatch_id"] == DISPATCH
    assert receipt["project_id"] == PROJECT
    assert receipt["attempt"] == 1
    assert receipt["end_pending"] is True
    # The parser writes nothing itself; receipt_processor.sh appends its stdout.
    assert [line["event_type"] for line in _lines(state_dir)] == ["dispatch_started"]


def test_parser_refusal_exits_nonzero_with_no_stdout(state_dir, tmp_path):
    _insert_row(state_dir, metadata="still not json")
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH, mtime=T1)

    run = _run_parser(report, _parser_env(state_dir, tmp_path / "data"))

    assert run.returncode != 0
    assert run.stdout == ""
    assert DISPATCH in run.stderr
    assert "lifecycle_mode_undecidable" in run.stderr


def test_parser_keeps_the_legacy_task_complete_json(tmp_path):
    sd = tmp_path / "legacy-state"
    sd.mkdir()
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH, mtime=T1)

    run = _run_parser(report, _parser_env(sd, tmp_path / "data"))

    assert run.returncode == 0, run.stderr
    receipt = json.loads(run.stdout)
    assert receipt["event_type"] == "task_complete"
    assert receipt["event"] == "task_complete"
    assert receipt["dispatch_id"] == DISPATCH
    assert receipt["status"] == "success"
    for lifecycle_key in ("attempt", "end_pending", "end_outcome_id", "project_id"):
        assert lifecycle_key not in receipt


# ---------------------------------------------------------------------------
# Rule 7: the restore does not crash on the new exception
# ---------------------------------------------------------------------------

def test_restore_on_a_refused_report_returns_false_and_leaves_the_bytes(state_dir, tmp_path):
    _insert_row(state_dir, metadata="unreadable row")
    report = _write_report(tmp_path / "unified_reports" / f"{DISPATCH}.md", DISPATCH,
                           mtime=T1, model="unknown")
    before = report.read_bytes()
    candidate = restore.Candidate(path=report, dispatch_id=DISPATCH,
                                  reason_code="missing_model", receipt={})
    resolution = restore.Resolution(model="deepseek-flash", provider="deepseek-harness",
                                    source_kind="frontmatter", source_detail="model: x")

    restored, detail = restore.apply_restoration(
        candidate, resolution, data_dir=tmp_path, state_dir=state_dir)

    assert restored is False
    assert detail.startswith("lifecycle_mode_undecidable:")
    assert report.read_bytes() == before
    assert _lines(state_dir) == []


# ---------------------------------------------------------------------------
# Round 2, rule 3: the report's OWN attempt reaches the decision
# ---------------------------------------------------------------------------

def _two_started_attempts(state_dir: Path, spec_file: Path, clock) -> None:
    """Attempt 1 started and ended, attempt 2 started: write time alone says 2."""
    clock["now"] = T0
    _begin(state_dir, spec_file)
    _finalize(state_dir, attempt=1)
    clock["now"] = T2
    _begin(state_dir, spec_file)


def _finished_of_attempt(state_dir: Path, attempt: int) -> list:
    return [line for line in _of_type(state_dir, "dispatch_finished")
            if line.get("attempt") == attempt]


def test_the_reports_own_attempt_outranks_write_time(state_dir, spec_file, v3, clock,
                                                     tmp_path):
    """Codex's reproduction: attempt 1 named, file written after attempt 2 started."""
    _two_started_attempts(state_dir, spec_file, clock)
    reports = tmp_path / "reports"
    ledger = str(_ledger(state_dir))

    # An attempt-2 booking already exists, so a guard keyed on the dispatch alone
    # would suppress the attempt-1 report below as a duplicate.
    second = _write_report(reports / "attempt-2.md", DISPATCH, mtime=T2, attempt=2)
    _r, outcome_two = rtc._convert_one_detailed(second, receipts_file=ledger)
    assert outcome_two == "appended"
    assert _of_type(state_dir, "report_ingested")[0]["attempt"] == 2

    first = _write_report(reports / "attempt-1.md", DISPATCH, mtime=T3, attempt=1)
    _r, outcome_one = rtc._convert_one_detailed(first, receipts_file=ledger)

    assert outcome_one == "appended"
    ingested = _of_type(state_dir, "report_ingested")
    assert [line["attempt"] for line in ingested] == [2, 1]
    finished_one = _finished_of_attempt(state_dir, 1)
    assert len(finished_one) == 1
    assert ingested[1]["end_outcome_id"] == finished_one[0]["outcome_id"]
    assert "end_outcome_id" not in ingested[0]


def test_parser_books_the_reports_own_attempt(state_dir, spec_file, v3, clock, tmp_path):
    _two_started_attempts(state_dir, spec_file, clock)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH,
                           mtime=T3, attempt=1)

    run = _run_parser(report, _parser_env(state_dir, tmp_path / "data", v3=True))

    assert run.returncode == 0, run.stderr
    receipt = json.loads(run.stdout)
    assert receipt["event_type"] == "report_ingested"
    assert receipt["attempt"] == 1
    assert receipt["end_outcome_id"] == _finished_of_attempt(state_dir, 1)[0]["outcome_id"]


def test_a_named_attempt_with_no_start_refuses_the_converter(
    state_dir, spec_file, v3, clock, caplog, tmp_path
):
    clock["now"] = T0
    _begin(state_dir, spec_file)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH,
                           mtime=T1, attempt=3)
    caplog.set_level(logging.WARNING, logger=rtc.logger.name)

    _r, outcome = rtc._convert_one_detailed(report, receipts_file=str(_ledger(state_dir)))

    assert outcome == "error"
    assert [line["event_type"] for line in _lines(state_dir)] == ["dispatch_started"]
    assert "attempt_unknown" in caplog.text


def test_parser_refuses_a_named_attempt_with_no_start(state_dir, spec_file, v3, clock,
                                                      tmp_path):
    clock["now"] = T0
    _begin(state_dir, spec_file)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH,
                           mtime=T1, attempt=3)

    run = _run_parser(report, _parser_env(state_dir, tmp_path / "data", v3=True))

    assert run.returncode != 0
    assert run.stdout == ""
    assert "attempt_unknown" in run.stderr
    assert [line["event_type"] for line in _lines(state_dir)] == ["dispatch_started"]


@pytest.mark.parametrize("named", ["x", 0])
def test_a_named_attempt_that_is_not_a_positive_integer_is_ignored_by_the_converter(
    state_dir, spec_file, v3, clock, tmp_path, named
):
    _two_started_attempts(state_dir, spec_file, clock)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH,
                           mtime=T1, attempt=named)

    _r, outcome = rtc._convert_one_detailed(report, receipts_file=str(_ledger(state_dir)))

    assert outcome == "appended"
    ingested = _of_type(state_dir, "report_ingested")
    assert [line["attempt"] for line in ingested] == [1]
    assert ingested[0]["end_outcome_id"] == _finished_of_attempt(state_dir, 1)[0]["outcome_id"]


@pytest.mark.parametrize("named", ["x", 0])
def test_parser_ignores_a_named_attempt_that_is_not_a_positive_integer(
    state_dir, spec_file, v3, clock, tmp_path, named
):
    _two_started_attempts(state_dir, spec_file, clock)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH,
                           mtime=T1, attempt=named)

    run = _run_parser(report, _parser_env(state_dir, tmp_path / "data", v3=True))

    assert run.returncode == 0, run.stderr
    receipt = json.loads(run.stdout)
    assert receipt["attempt"] == 1
    assert receipt["end_outcome_id"] == _finished_of_attempt(state_dir, 1)[0]["outcome_id"]


# ---------------------------------------------------------------------------
# Round 3: one attempt reader for both writers, a quoted value included
# ---------------------------------------------------------------------------

def _write_report_naming_attempt_literal(path: Path, literal: str, *, mtime: str) -> Path:
    """A report whose frontmatter names ``attempt`` with *literal* verbatim ('1' / "1")."""
    text = _report_text(DISPATCH, attempt=1).replace("attempt: 1", f"attempt: {literal}", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = _epoch(mtime)
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("literal", ["'1'", '"1"'])
def test_parser_books_a_quoted_attempt_the_report_names(
    state_dir, spec_file, v3, clock, monkeypatch, tmp_path, literal
):
    """Codex's reproduction with the quote kept: attempt 1 named, file written after attempt 2.

    ``ReportParser.extract_metadata`` reads the frontmatter line raw, so this is
    the shape where the parser's own copy of the rule drifted from the
    converter's: ``int("'1'")`` failed and the file's write time booked attempt 2.
    """
    _two_started_attempts(state_dir, spec_file, clock)
    report = _write_report_naming_attempt_literal(
        tmp_path / "reports" / f"{DISPATCH}.md", literal, mtime=T3)
    assert f"attempt: {literal}" in report.read_text(encoding="utf-8")
    end_of_one = _finished_of_attempt(state_dir, 1)[0]["outcome_id"]
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))

    # The real public entry point, in process.
    receipt = report_parser.ReportParser().parse_report(str(report))
    assert receipt["event_type"] == "report_ingested"
    assert receipt["attempt"] == 1
    assert receipt["end_outcome_id"] == end_of_one

    # ... and the production entry point, as receipt_processor.sh runs it.
    run = _run_parser(report, _parser_env(state_dir, tmp_path / "data", v3=True))
    assert run.returncode == 0, run.stderr
    subprocess_receipt = json.loads(run.stdout)
    assert subprocess_receipt["attempt"] == 1
    assert subprocess_receipt["end_outcome_id"] == end_of_one


@pytest.mark.parametrize("literal", ["'1'", '"1"'])
def test_converter_books_the_same_attempt_for_the_quoted_report(
    state_dir, spec_file, v3, clock, tmp_path, literal
):
    _two_started_attempts(state_dir, spec_file, clock)
    report = _write_report_naming_attempt_literal(
        tmp_path / "reports" / f"{DISPATCH}.md", literal, mtime=T3)

    _r, outcome = rtc._convert_one_detailed(report, receipts_file=str(_ledger(state_dir)))

    assert outcome == "appended"
    ingested = _of_type(state_dir, "report_ingested")
    assert [line["attempt"] for line in ingested] == [1]
    assert ingested[0]["end_outcome_id"] == _finished_of_attempt(state_dir, 1)[0]["outcome_id"]


@pytest.mark.parametrize("named, expected", [
    (1, 1), ("1", 1), ("'1'", 1), ('"1"', 1), (" 1 ", 1),
    ("'x'", None), (0, None), (-1, None), ("", None), ("''", None),
    ("'1", None), ("1'", None), (True, None), (False, None), (None, None),
])
def test_the_one_attempt_reader_reads_the_value_table(named, expected):
    """Rule 2 of round 3, on the single reader both writers use."""
    assert rtc._named_attempt({"attempt": named}) == expected


def test_exactly_one_attempt_reader_is_defined_in_the_production_tree():
    """`git grep -n "def _named_attempt" scripts` — exactly one hit, the converter's."""
    defined = sorted(
        str(path.relative_to(VNX_ROOT))
        for path in SCRIPT_DIR.rglob("*.py")
        if "def _named_attempt" in path.read_text(encoding="utf-8")
    )
    assert defined == ["scripts/lib/report_to_receipt_converter.py"]


def test_the_parser_routes_its_attempt_through_the_converters_reader(
    state_dir, spec_file, v3, clock, monkeypatch, tmp_path
):
    """A spy on the one reader fires when the parser books a report: no second copy left."""
    _two_started_attempts(state_dir, spec_file, clock)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH, mtime=T3,
                           attempt=1)
    seen = []
    real = rtc._named_attempt

    def _spy(metadata):
        seen.append(real(metadata))
        return real(metadata)

    monkeypatch.setattr(rtc, "_named_attempt", _spy)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))

    receipt = report_parser.ReportParser().parse_report(str(report))

    assert seen == [1], "the parser never reached the converter's attempt reader"
    assert receipt["attempt"] == 1


# ---------------------------------------------------------------------------
# Round 2, rule 4: a legacy report keeps its line, whatever it names
# ---------------------------------------------------------------------------

def test_a_legacy_report_naming_attempt_and_project_id_keeps_the_k8a_line(
    state_dir, tmp_path
):
    _insert_row(state_dir)  # a legacy row: no lifecycle_mode marker
    report = _write_report(tmp_path / "reports" / "named.md", DISPATCH, mtime=T1,
                           attempt=1)
    text = report.read_text(encoding="utf-8")
    core = rtc._build_receipt_from_report_core(report, text, state_dir=state_dir)

    # The one shaping point hands a legacy receipt back untouched: the very
    # same object, so no lifecycle key (and no project_id) can be added.
    assert rtc._lifecycle_report_line(core, text, state_dir=state_dir,
                                      report_path=report) is core

    built = rtc.build_receipt_from_report(report, text, state_dir=state_dir)
    assert built["event_type"] == "task_complete"
    for key, value in core.items():
        assert built[key] == value, key
    for lifecycle_key in ("attempt", "end_pending", "end_outcome_id", "project_id"):
        assert lifecycle_key not in built

    _r, outcome = rtc._convert_one_detailed(report, receipts_file=str(_ledger(state_dir)))

    assert outcome == "appended"
    line = _lines(state_dir)[0]
    assert line["event_type"] == "task_complete"
    assert line["dispatch_id"] == DISPATCH
    assert line["status"] == core["status"]
    for lifecycle_key in ("attempt", "end_pending", "end_outcome_id", "project_id"):
        assert lifecycle_key not in line


def test_a_legacy_report_without_project_id_books_the_same_line_in_the_parser(
    state_dir, tmp_path
):
    _insert_row(state_dir)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH,
                           project_id=None, mtime=T1, attempt=1)

    run = _run_parser(report, _parser_env(state_dir, tmp_path / "data"))

    assert run.returncode == 0, run.stderr
    receipt = json.loads(run.stdout)
    assert receipt["event_type"] == "task_complete"
    assert receipt["dispatch_id"] == DISPATCH
    for lifecycle_key in ("attempt", "end_pending", "end_outcome_id", "project_id"):
        assert lifecycle_key not in receipt


def test_the_parsers_legacy_shaping_point_returns_the_very_same_object(
    state_dir, monkeypatch, tmp_path
):
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    _insert_row(state_dir)
    report = _write_report(tmp_path / "reports" / "named.md", DISPATCH, mtime=T1,
                           attempt=1)
    receipt = {"dispatch_id": DISPATCH, "event_type": "task_complete",
               "receipt_kind": "dispatch"}

    shaped = report_parser.ReportParser()._book_report_line(
        receipt,
        report.read_text(encoding="utf-8"),
        str(report),
    )

    assert shaped is receipt


def test_an_unresolvable_tenant_refuses_both_writers(state_dir, monkeypatch, tmp_path):
    """No project_id in the report and no resolver: the named refusal, never a booking."""
    _insert_row(state_dir)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    reports = tmp_path / "reports"
    report = _write_report(reports / f"{DISPATCH}.md", DISPATCH, project_id=None, mtime=T1)

    _r, outcome = rtc._convert_one_detailed(report, receipts_file=str(_ledger(state_dir)))

    assert outcome == "error"
    assert _of_type(state_dir, "task_complete") == []
    assert _of_type(state_dir, "report_ingested") == []

    env = _parser_env(state_dir, tmp_path / "data")
    env.pop("VNX_PROJECT_ID", None)
    run = _run_parser(report, env, cwd=tmp_path)

    assert run.returncode != 0
    assert run.stdout == ""
    assert "lifecycle_mode_undecidable" in run.stderr
    assert _of_type(state_dir, "task_complete") == []
    assert _of_type(state_dir, "report_ingested") == []


# ---------------------------------------------------------------------------
# Round 2, rule 2: no tenant rule of their own in the writers
# ---------------------------------------------------------------------------

def test_no_local_tenant_rule_remains_in_the_writers():
    files = ["scripts/lib/report_to_receipt_converter.py", "scripts/report_parser.py"]
    run = subprocess.run(
        ["git", "grep", "-n", "_resolve_project_id\\|_lifecycle_project_id", *files],
        capture_output=True, text=True, cwd=VNX_ROOT,
    )
    assert run.returncode == 1, run.stdout  # 1 = no match
    assert run.stdout == ""


# ---------------------------------------------------------------------------
# Round 4: one hint builder, one metadata reader, for both writers
# ---------------------------------------------------------------------------

def _write_report_naming_literal(path: Path, field: str, literal: str, *, mtime: str = T1) -> Path:
    """The shared report with *field*'s frontmatter value replaced by *literal* verbatim.

    ``yaml.safe_dump`` writes ``project_id: proj-alpha`` and ``attempt: 1``
    unquoted; the replacement puts the reviewer's literal (quotes, padding,
    nothing) on the line the two writers read.
    """
    text = _report_text(DISPATCH, attempt=1)
    current = f"{field}: " + ("proj-alpha" if field == "project_id" else "1")
    assert current in text
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace(current, f"{field}: {literal}", 1), encoding="utf-8")
    stamp = _epoch(mtime)
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("literal", ["'proj-alpha'", '"proj-alpha"'])
def test_parser_books_a_quoted_tenant_the_report_names(
    state_dir, spec_file, v3, clock, monkeypatch, tmp_path, literal
):
    """Codex's reproduction: a v3 dispatch under proj-alpha, the report quoting the tenant.

    ``ReportParser.extract_metadata`` kept the YAML quotes, so the parser handed
    the decision ``'proj-alpha'``, refused ``project_mismatch`` and retried a
    valid report forever — while the converter booked it. Both writers must book
    this file as one ``report_ingested`` for ``proj-alpha``.
    """
    clock["now"] = T0
    _begin(state_dir, spec_file)
    report = _write_report_naming_literal(
        tmp_path / "reports" / f"{DISPATCH}.md", "project_id", literal)
    assert f"project_id: {literal}" in report.read_text(encoding="utf-8")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))

    receipt = report_parser.ReportParser().parse_report(str(report))
    assert "lifecycle_refusal" not in receipt
    assert receipt["event_type"] == "report_ingested"
    assert receipt["project_id"] == PROJECT

    run = _run_parser(report, _parser_env(state_dir, tmp_path / "data", v3=True))
    assert run.returncode == 0, run.stderr
    assert "project_mismatch" not in run.stderr
    subprocess_receipt = json.loads(run.stdout)
    assert subprocess_receipt["event_type"] == "report_ingested"
    assert subprocess_receipt["project_id"] == PROJECT


@pytest.mark.parametrize("literal", ["'proj-alpha'", '"proj-alpha"'])
def test_converter_books_the_same_tenant_for_the_quoted_report(
    state_dir, spec_file, v3, clock, tmp_path, literal
):
    clock["now"] = T0
    _begin(state_dir, spec_file)
    report = _write_report_naming_literal(
        tmp_path / "reports" / f"{DISPATCH}.md", "project_id", literal)

    _r, outcome = rtc._convert_one_detailed(report, receipts_file=str(_ledger(state_dir)))

    assert outcome == "appended"
    ingested = _of_type(state_dir, "report_ingested")
    assert [line["project_id"] for line in ingested] == [PROJECT]


@pytest.mark.parametrize("literal, expected", [
    ("proj-alpha", "proj-alpha"),
    ("'proj-alpha'", "proj-alpha"),
    ('"proj-alpha"', "proj-alpha"),
    ("  proj-alpha  ", "proj-alpha"),
    ("'proj-alpha", "'proj-alpha"),
    ("", None),
])
def test_the_one_hint_builder_reads_the_tenant_table(literal, expected):
    """Rule 1 of round 4, on the one builder both writers use."""
    text = _report_text(DISPATCH, attempt=1).replace(
        "project_id: proj-alpha", f"project_id: {literal}", 1)
    assert rtc.report_hint_fields(text, {}).get("project_id") == expected


@pytest.mark.parametrize("literal, expected", [
    ("1", 1), ("'1'", 1), ('"1"', 1), (" 1 ", 1), ("'1", None), ("", None),
])
def test_the_one_hint_builder_reads_the_attempt_table(literal, expected):
    text = _report_text(DISPATCH, attempt=1).replace("attempt: 1", f"attempt: {literal}", 1)
    assert rtc.report_hint_fields(text, {}).get("attempt") == expected


def test_the_one_hint_builder_leaves_a_receipts_own_tenant_alone():
    """A receipt that already carries ``project_id`` gains no hint for it (round 2's rule)."""
    named = rtc.report_hint_fields(_report_text(DISPATCH, attempt=1),
                                   {"project_id": "proj-other"})
    assert "project_id" not in named


def test_the_parser_routes_its_hint_through_the_one_builder(
    state_dir, spec_file, v3, clock, monkeypatch, tmp_path
):
    """A spy on the one builder fires when the parser books a report: no second copy left."""
    clock["now"] = T0
    _begin(state_dir, spec_file)
    report = _write_report(tmp_path / "reports" / f"{DISPATCH}.md", DISPATCH, mtime=T1)
    seen = []
    real = rtc.report_hint_fields

    def _spy(text, receipt):
        seen.append(real(text, receipt))
        return real(text, receipt)

    monkeypatch.setattr(rtc, "report_hint_fields", _spy)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))

    receipt = report_parser.ReportParser().parse_report(str(report))

    assert seen and seen[0]["project_id"] == PROJECT
    assert receipt["event_type"] == "report_ingested"


def test_the_parser_keeps_no_hint_code_of_its_own():
    r"""`git grep -n "named_attempt(metadata)\|metadata.get('project_id')" scripts/report_parser.py`."""
    run = subprocess.run(
        ["git", "grep", "-n", "named_attempt(metadata)\\|metadata.get('project_id')",
         "scripts/report_parser.py"],
        capture_output=True, text=True, cwd=VNX_ROOT,
    )
    assert run.returncode == 1, run.stdout + run.stderr  # 1 = no match
    assert run.stdout == ""
