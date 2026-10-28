"""ADR-045 K-2c: the analysis readers read ``dispatch_finished`` next to ``task_complete``.

Every reader below is exercised on two ledgers that describe the SAME dispatches:
one writes the legacy completion line (``task_complete``/``subprocess_completion``),
the other writes the v3 end line (``dispatch_finished``). The reader has to answer
the same thing for both — the legacy answer IS the golden — and an end line whose
``end_state`` is missing, ``None``, malformed or unknown must never read as a
success in any of them.

Only filesystem paths are patched: each reader reads a ``tmp_path`` ledger and,
where it needs one, a ``tmp_path`` state dir.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))
sys.path.insert(0, str(_REPO / "dashboard"))


DISPATCH_ID = "20260101-test-dispatch"
NOW_Z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_ledger(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _legacy_line(**extra) -> dict:
    """The legacy shape: the completion type carries the outcome in ``status``."""
    line = {
        "event_type": "task_complete",
        "status": "success",
        "dispatch_id": DISPATCH_ID,
        "terminal": "T1",
        "timestamp": NOW_Z,
    }
    line.update(extra)
    return line


def _end_line(end_state: str = "completed", **extra) -> dict:
    """The v3 shape: ``end_state`` is the fact, ``status`` only its projection."""
    line = {
        "event_type": "dispatch_finished",
        "end_state": end_state,
        "status": "success",
        "dispatch_id": DISPATCH_ID,
        "terminal": "T1",
        "timestamp": NOW_Z,
    }
    line.update(extra)
    return line


def _end_line_without_end_state(**extra) -> dict:
    line = _end_line(**extra)
    del line["end_state"]
    return line


def _end_line_with(end_state, **extra) -> dict:
    return _end_line(end_state, **extra)


# The four ways an end line can fail to name a governed outcome. Each keeps
# ``status: "success"`` on purpose: the status field is a projection and must not
# be able to turn a line with no end_state into a success.
_NO_GOVERNED_OUTCOME = [
    pytest.param(_end_line_without_end_state, id="end-state-missing"),
    pytest.param(lambda **extra: _end_line_with(None, **extra), id="end-state-none"),
    pytest.param(lambda **extra: _end_line_with(42, **extra), id="end-state-malformed"),
    pytest.param(lambda **extra: _end_line_with("half_done", **extra), id="end-state-unknown"),
]


# ---------------------------------------------------------------------------
# The shared read: one place decides what a completion line says
# ---------------------------------------------------------------------------

def test_completion_reading_reads_the_end_state_not_the_status_field():
    from completion_reading import completion_outcome

    # A hand-written status that disagrees with the ledger fact loses.
    assert completion_outcome(_end_line("failed", status="success")) == "failure"
    assert completion_outcome(_end_line("completed", status="failure")) == "success"
    assert completion_outcome(_legacy_line(status="success")) == "success"


@pytest.mark.parametrize("garbage", _NO_GOVERNED_OUTCOME)
def test_completion_reading_never_reads_a_success_without_an_end_state(garbage):
    from completion_reading import completion_outcome, completion_status

    line = garbage()
    assert completion_status(line) is None
    assert completion_outcome(line) is None


def test_completion_reading_lifecycle_lines_are_never_completions():
    from completion_reading import completion_outcome, is_completion

    for event_type in ("dispatch_started", "report_ingested"):
        line = {"event_type": event_type, "status": "success", "end_state": "completed"}
        assert is_completion(line) is False
        assert completion_outcome(line) is None


# ---------------------------------------------------------------------------
# scripts/cost_tracker.py — cost lines
# ---------------------------------------------------------------------------

def _cost_tracker_cli():
    """Load scripts/cost_tracker.py by path.

    ``scripts/lib/cost_tracker.py`` is a different module with the same
    basename (the pool-scaling cost reader), and scripts/lib sits on sys.path
    ahead of scripts/ in this file, so a plain ``import cost_tracker`` gets the
    wrong one.
    """
    import importlib.util

    path = _REPO / "scripts" / "cost_tracker.py"
    spec = importlib.util.spec_from_file_location("k2c_cost_tracker_cli", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def test_cost_tracker_legacy_and_end_line_give_the_same_totals(tmp_path):
    cost_tracker = _cost_tracker_cli()

    usage = {"provider": "claude", "model": "claude-sonnet-4.5",
             "input_tokens": 100, "output_tokens": 50}
    legacy = cost_tracker.build_metrics(
        [_legacy_line(**usage)], 0, tmp_path, tmp_path / "legacy.ndjson")
    v3 = cost_tracker.build_metrics(
        [_end_line(**usage)], 0, tmp_path, tmp_path / "v3.ndjson")

    assert v3["totals"] == legacy["totals"]
    assert v3["by_model"] == legacy["by_model"]
    assert legacy["totals"]["events_analyzed"] == 1


def test_cost_tracker_start_and_report_lines_are_not_cost_lines(tmp_path):
    cost_tracker = _cost_tracker_cli()

    records = [
        {"event_type": "dispatch_started", "dispatch_id": DISPATCH_ID, "terminal": "T1",
         "input_tokens": 100, "output_tokens": 50, "timestamp": NOW_Z},
        {"event_type": "report_ingested", "dispatch_id": DISPATCH_ID, "terminal": "T1",
         "input_tokens": 100, "output_tokens": 50, "timestamp": NOW_Z},
    ]
    metrics = cost_tracker.build_metrics(records, 0, tmp_path, tmp_path / "l.ndjson")
    assert metrics["totals"]["events_analyzed"] == 0
    assert metrics["totals"]["total_tokens"] == 0


def test_cost_tracker_does_not_book_the_lane_line_twice(tmp_path):
    cost_tracker = _cost_tracker_cli()

    # The synthesized lane line for a dispatch that also writes its carrying
    # completion line must not add a second cost line (measured: 64 of 114
    # dispatch ids carry both).
    records = [
        _legacy_line(input_tokens=100, output_tokens=50, provider="claude"),
        {"event_type": "subprocess_completion", "status": "done", "dispatch_id": DISPATCH_ID,
         "terminal": "T1", "input_tokens": 100, "output_tokens": 50, "timestamp": NOW_Z},
    ]
    metrics = cost_tracker.build_metrics(records, 0, tmp_path, tmp_path / "l.ndjson")
    assert metrics["totals"]["events_analyzed"] == 1
    assert metrics["totals"]["total_tokens"] == 150


# ---------------------------------------------------------------------------
# scripts/traceability_audit.py — completion receipts per dispatch
# ---------------------------------------------------------------------------

def _trace_gaps(ledger: Path):
    import traceability_audit as ta

    receipts = list(ta.iter_receipts(ledger, None, None))
    dispatches = [ta.DispatchRecord(dispatch_id=DISPATCH_ID, state="completed", pr_id="",
                                    source_path=f"/fake/{DISPATCH_ID}.md")]
    return ta.gap_dispatches_without_completion_receipt(dispatches, receipts)


def test_traceability_audit_legacy_and_end_line_find_the_same_gaps(tmp_path):
    legacy = _trace_gaps(_write_ledger(tmp_path / "legacy.ndjson", [_legacy_line()]))
    v3 = _trace_gaps(_write_ledger(tmp_path / "v3.ndjson", [_end_line()]))

    assert v3.gap_count == legacy.gap_count == 0
    assert v3.traced == legacy.traced == 1


def test_traceability_audit_start_and_report_lines_are_not_completions(tmp_path):
    ledger = _write_ledger(tmp_path / "l.ndjson", [
        {"event_type": "dispatch_started", "dispatch_id": DISPATCH_ID, "timestamp": NOW_Z},
        {"event_type": "report_ingested", "dispatch_id": DISPATCH_ID, "timestamp": NOW_Z},
    ])
    assert _trace_gaps(ledger).gap_count == 1


# ---------------------------------------------------------------------------
# scripts/lib/cqs_calculator.py — the status category
# ---------------------------------------------------------------------------

def test_cqs_legacy_and_end_line_score_the_same():
    from cqs_calculator import calculate_cqs

    legacy = calculate_cqs(_legacy_line(report_path="/tmp/r.md"), session=None)
    v3 = calculate_cqs(_end_line(report_path="/tmp/r.md"), session=None)

    assert v3 == legacy
    assert legacy["normalized_status"] == "success"


def test_cqs_failed_end_line_is_a_failure_never_a_success():
    from cqs_calculator import calculate_cqs

    result = calculate_cqs(_end_line("failed", status="success"), session=None)
    assert result["normalized_status"] == "failure"
    assert result["cqs"] is not None


@pytest.mark.parametrize("garbage", _NO_GOVERNED_OUTCOME)
def test_cqs_end_line_without_end_state_is_never_a_success(garbage):
    from cqs_calculator import calculate_cqs

    result = calculate_cqs(garbage(), session=None)
    assert result["normalized_status"] != "success"
    assert result["cqs"] is None  # excluded, exactly like timeout/unknown


# ---------------------------------------------------------------------------
# scripts/control_centre/dispatch_lifecycle_tracker.py — the tracked status
# ---------------------------------------------------------------------------

def _track_one(tmp_path: Path, event: dict, dispatch_id: str = DISPATCH_ID):
    from scripts.control_centre.dispatch_lifecycle_tracker import (
        DispatchLifecycleTracker,
    )
    from scripts.control_centre.receipt_tail import ProjectConfig, ReceiptTail

    project_id = "proj-a"
    receipt_path = tmp_path / project_id / "t0_receipts.ndjson"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    tail = ReceiptTail(
        projects=[ProjectConfig(project_id=project_id, root=tmp_path / project_id,
                                ndjson_path=receipt_path)],
        poll_interval=0.02,
    )
    tracker = DispatchLifecycleTracker(receipt_tail=tail)
    tracker.register(dispatch_id, project_id)

    def _emit() -> None:
        time.sleep(0.05)
        _write_ledger(receipt_path, [{**event, "dispatch_id": dispatch_id}])

    threading.Thread(target=_emit, daemon=True).start()
    outcome = tracker.track(dispatch_id, project_id, timeout_seconds=0.8)
    tail.stop()
    return outcome


def test_lifecycle_tracker_legacy_and_end_line_complete_the_same(tmp_path):
    from scripts.control_centre.dispatch_lifecycle_tracker import DispatchStatus

    legacy = _track_one(tmp_path / "legacy", _legacy_line())
    v3 = _track_one(tmp_path / "v3", _end_line())

    assert legacy.status == v3.status == DispatchStatus.COMPLETED
    assert v3.success is True


def test_lifecycle_tracker_failed_end_line_fails_the_dispatch(tmp_path):
    from scripts.control_centre.dispatch_lifecycle_tracker import DispatchStatus

    outcome = _track_one(tmp_path, _end_line("failed", status="success"))
    assert outcome.status == DispatchStatus.FAILED
    assert outcome.success is False


@pytest.mark.parametrize("garbage", _NO_GOVERNED_OUTCOME)
def test_lifecycle_tracker_end_line_without_end_state_never_completes(tmp_path, garbage):
    from scripts.control_centre.dispatch_lifecycle_tracker import DispatchStatus

    outcome = _track_one(tmp_path, garbage())
    assert outcome.status != DispatchStatus.COMPLETED


def test_lifecycle_tracker_start_line_does_not_complete(tmp_path):
    from scripts.control_centre.dispatch_lifecycle_tracker import DispatchStatus

    outcome = _track_one(tmp_path, {"event_type": "dispatch_started", "status": "success"})
    assert outcome.status != DispatchStatus.COMPLETED


# ---------------------------------------------------------------------------
# scripts/t0_intelligence_aggregator.py — tasks_completed_1hr
# ---------------------------------------------------------------------------

@pytest.fixture()
def aggregator(tmp_path, monkeypatch):
    import t0_intelligence_aggregator as ag
    import vnx_paths

    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "unified_reports"
    state_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {
        "VNX_HOME": str(tmp_path),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_REPORTS_DIR": str(reports_dir),
    })
    return ag.T0IntelligenceAggregator()


def test_aggregator_legacy_and_end_line_count_the_same(aggregator):
    legacy = aggregator._compute_summary([_legacy_line()])
    v3 = aggregator._compute_summary([_end_line()])

    assert v3["tasks_completed_1hr"] == legacy["tasks_completed_1hr"] == 1


def test_aggregator_start_and_report_lines_are_not_completions(aggregator):
    summary = aggregator._compute_summary([
        {"event_type": "dispatch_started", "status": "success", "timestamp": NOW_Z},
        {"event_type": "report_ingested", "status": "success", "timestamp": NOW_Z},
    ])
    assert summary["tasks_completed_1hr"] == 0


# ---------------------------------------------------------------------------
# scripts/intelligence_daemon.py — gate results
# ---------------------------------------------------------------------------

def _gate_results(tmp_path: Path, records: list[dict]) -> list[dict]:
    from intelligence_daemon import GovernanceDigestRunner

    _write_ledger(tmp_path / "t0_receipts.ndjson", records)
    runner = GovernanceDigestRunner(state_dir=tmp_path)
    return [{k: r[k] for k in ("gate_id", "status", "dispatch_id")}
            for r in runner._load_gate_results()]


def test_intelligence_daemon_legacy_and_end_line_gate_alike(tmp_path):
    legacy = _gate_results(tmp_path / "legacy", [_legacy_line(gate="codex_gate")])
    v3 = _gate_results(tmp_path / "v3", [_end_line(gate="codex_gate")])

    assert v3 == legacy == [{"gate_id": "codex_gate", "status": "pass",
                             "dispatch_id": DISPATCH_ID}]


def test_intelligence_daemon_failed_end_line_fails_the_gate(tmp_path):
    results = _gate_results(tmp_path, [_end_line("failed", status="success", gate="codex_gate")])
    assert [r["status"] for r in results] == ["fail"]


@pytest.mark.parametrize("garbage", _NO_GOVERNED_OUTCOME)
def test_intelligence_daemon_end_line_without_end_state_is_never_a_pass(tmp_path, garbage):
    results = _gate_results(tmp_path, [garbage(gate="codex_gate")])
    assert [r["status"] for r in results] == []


# ---------------------------------------------------------------------------
# scripts/memory_consolidator.py — failure patterns
# ---------------------------------------------------------------------------

@pytest.fixture()
def consolidator(tmp_path, monkeypatch):
    import memory_consolidator as mc_module

    state_dir = tmp_path / "state"
    (tmp_path / ".vnx-intelligence").mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "VNX_HOME": str(tmp_path),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_DATA_DIR": str(tmp_path / ".vnx-data"),
        "VNX_INTELLIGENCE_DIR": str(tmp_path / ".vnx-intelligence"),
    }
    monkeypatch.setattr(mc_module, "ensure_env", lambda: env)
    return mc_module.MemoryConsolidator()


def _failure_patterns(consolidator, ledger: Path):
    from datetime import datetime as _dt

    consolidator.receipts_path = ledger
    receipts = consolidator._read_receipts(_dt(2020, 1, 1))
    return receipts, consolidator._extract_failure_patterns(receipts)


def test_memory_consolidator_legacy_and_end_line_extract_the_same_pattern(tmp_path, consolidator):
    legacy_ledger = _write_ledger(tmp_path / "legacy.ndjson", [
        _legacy_line(status="error", gate="codex_gate", dispatch_id="D-1"),
        _legacy_line(status="failure", gate="codex_gate", dispatch_id="D-2"),
    ])
    v3_ledger = _write_ledger(tmp_path / "v3.ndjson", [
        _end_line("failed", status="success", gate="codex_gate", dispatch_id="D-1"),
        _end_line("failed", status="success", gate="codex_gate", dispatch_id="D-2"),
    ])

    legacy_receipts, legacy = _failure_patterns(consolidator, legacy_ledger)
    v3_receipts, v3 = _failure_patterns(consolidator, v3_ledger)

    assert len(legacy_receipts) == len(v3_receipts) == 2
    assert v3 == legacy
    assert legacy and legacy[0].evidence_count == 2


def test_memory_consolidator_start_and_report_lines_are_not_read(tmp_path, consolidator):
    ledger = _write_ledger(tmp_path / "l.ndjson", [
        {"event_type": "dispatch_started", "status": "error", "gate": "codex_gate",
         "dispatch_id": "D-1", "timestamp": NOW_Z},
        {"event_type": "report_ingested", "status": "error", "gate": "codex_gate",
         "dispatch_id": "D-2", "timestamp": NOW_Z},
    ])
    receipts, patterns = _failure_patterns(consolidator, ledger)
    assert receipts == []
    assert patterns == []


@pytest.mark.parametrize("garbage", _NO_GOVERNED_OUTCOME)
def test_memory_consolidator_end_line_without_end_state_is_no_failure_success_claim(
        tmp_path, consolidator, garbage):
    ledger = _write_ledger(tmp_path / "l.ndjson", [
        garbage(gate="codex_gate", dispatch_id="D-1"),
        garbage(gate="codex_gate", dispatch_id="D-2"),
    ])
    receipts, patterns = _failure_patterns(consolidator, ledger)
    assert len(receipts) == 2      # the lines were read
    assert patterns == []          # but they claim no governed outcome


# ---------------------------------------------------------------------------
# scripts/generate_t0_recommendations.py — gate progression
# ---------------------------------------------------------------------------

@pytest.fixture()
def recommendation_engine(tmp_path, monkeypatch):
    import generate_t0_recommendations as gtr

    completed = tmp_path / "completed"
    completed.mkdir()
    (completed / f"{DISPATCH_ID}.md").write_text(
        "Program: test\nGate: implementation\nOn-Success: review\nOn-Failure: investigation\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gtr, "_dispatch_roots", lambda: [tmp_path])
    return gtr.RecommendationEngine()


def _recommendation_keys(engine, receipt: dict):
    engine.analyze_task_completion(receipt)
    return [(r.get("trigger"), r.get("dispatch_id"), r.get("gate"), r.get("action"))
            for r in engine.recommendations]


def test_recommendations_legacy_and_end_line_agree(recommendation_engine):
    legacy = _recommendation_keys(recommendation_engine, _legacy_line())
    v3 = _recommendation_keys(recommendation_engine, _end_line())

    assert v3 == legacy == [("task_success", DISPATCH_ID, "review", "create_dispatch")]


def test_recommendations_failed_end_line_uses_the_failure_branch(recommendation_engine):
    keys = _recommendation_keys(recommendation_engine, _end_line("failed", status="success"))
    assert keys == [("task_failure", DISPATCH_ID, "investigation", "create_dispatch")]


@pytest.mark.parametrize("garbage", _NO_GOVERNED_OUTCOME)
def test_recommendations_end_line_without_end_state_claims_nothing(
        recommendation_engine, garbage):
    assert _recommendation_keys(recommendation_engine, garbage()) == []


def test_recommendations_start_line_generates_nothing(recommendation_engine):
    start = {"event_type": "dispatch_started", "status": "success", "dispatch_id": DISPATCH_ID}
    assert _recommendation_keys(recommendation_engine, start) == []


# ---------------------------------------------------------------------------
# scripts/link_sessions_dispatches.py — outcome_status written per dispatch
# ---------------------------------------------------------------------------

def _linked_outcome(tmp_path: Path, record: dict):
    import link_sessions_dispatches as lsd

    ledger = _write_ledger(tmp_path / "t0_receipts.ndjson", [record])
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE dispatch_metadata (dispatch_id TEXT, outcome_status TEXT, "
                 "outcome_report_path TEXT, completed_at TEXT)")
    conn.execute("INSERT INTO dispatch_metadata (dispatch_id) VALUES (?)", (DISPATCH_ID,))
    original = lsd.RECEIPTS_FILE
    lsd.RECEIPTS_FILE = ledger
    try:
        linked = lsd.link_receipts_to_dispatches(conn)
    finally:
        lsd.RECEIPTS_FILE = original
    row = conn.execute(
        "SELECT outcome_status FROM dispatch_metadata WHERE dispatch_id = ?",
        (DISPATCH_ID,)).fetchone()
    conn.close()
    return linked, row[0]


def test_link_sessions_legacy_and_end_line_write_the_same_outcome(tmp_path):
    legacy = _linked_outcome(tmp_path / "legacy", _legacy_line())
    v3 = _linked_outcome(tmp_path / "v3", _end_line())

    assert v3 == legacy == (1, "success")


def test_link_sessions_failed_end_line_writes_a_failure(tmp_path):
    assert _linked_outcome(tmp_path, _end_line("failed", status="success")) == (1, "failure")


@pytest.mark.parametrize("garbage", _NO_GOVERNED_OUTCOME)
def test_link_sessions_end_line_without_end_state_is_never_a_success(tmp_path, garbage):
    linked, status = _linked_outcome(tmp_path, garbage())
    assert linked == 1
    assert status != "success"


# ---------------------------------------------------------------------------
# dashboard/api_token_stats.py — the receipts feed
# ---------------------------------------------------------------------------

def _feed_types(tmp_path: Path, monkeypatch, record: dict) -> list[str]:
    import api_token_stats

    ledger = _write_ledger(tmp_path / "t0_receipts.ndjson", [record])
    monkeypatch.setattr(api_token_stats, "_sd",
                        lambda: SimpleNamespace(RECEIPTS_PATH=ledger))
    return [event["type"] for event in api_token_stats._parse_receipts_events(limit=10)]


def test_token_stats_feed_legacy_and_end_line_look_the_same(tmp_path, monkeypatch):
    legacy = _feed_types(tmp_path / "legacy", monkeypatch, _legacy_line(gate="codex_gate"))
    v3 = _feed_types(tmp_path / "v3", monkeypatch, _end_line(gate="codex_gate"))

    assert v3 == legacy == ["gate_passed"]


def test_token_stats_feed_failed_end_line_is_a_failed_gate(tmp_path, monkeypatch):
    types = _feed_types(tmp_path, monkeypatch, _end_line("failed", status="success",
                                                         gate="codex_gate"))
    assert types == ["gate_failed"]


@pytest.mark.parametrize("garbage", _NO_GOVERNED_OUTCOME)
def test_token_stats_feed_end_line_without_end_state_is_never_a_pass(
        tmp_path, monkeypatch, garbage):
    types = _feed_types(tmp_path, monkeypatch, garbage(gate="codex_gate"))
    assert "gate_passed" not in types
