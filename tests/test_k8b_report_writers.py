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

def _report_text(dispatch_id: str, *, project_id: str = PROJECT,
                 model: str = "claude-sonnet-4-6") -> str:
    fm = {
        "schema_version": 1,
        "dispatch_id": dispatch_id,
        "project_id": project_id,
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
    return (
        f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n"
        "## Summary\n\nImplemented the feature per dispatch specification. "
        "All tests pass and coverage is at target.\n\n"
        "## Changes\n\n- scripts/lib/example.py: added X\n\n"
        "## Verification\n\npytest tests/ -x: 42 passed\n\n"
        "## Open Items\n\nNone\n"
    )


def _write_report(path: Path, dispatch_id: str, *, project_id: str = PROJECT,
                  mtime: str | float | None = None, model: str = "claude-sonnet-4-6") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_report_text(dispatch_id, project_id=project_id, model=model),
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


def _run_parser(report: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "report_parser.py"), str(report)],
        capture_output=True, text=True, env=env, timeout=120,
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
