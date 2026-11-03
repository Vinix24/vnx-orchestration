"""K3-1b: every plan-gate seat run and every tiebreaker run books one ledger line (ADR-049).

The unit under test is the real ledger in ``tmp_path``: only the model call (the
dispatcher) is injected and only the store path is chosen, so each test reads
back the line the module actually wrote. A line's identity is the run's own
``uuid.uuid4().hex`` (K3-1a's ``record_attempt_run``), minted before the call —
never the report's, never a slot counter's.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for p in (str(_LIB), str(_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import cost_ledger  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402
import plan_gate_tiebreaker as pgt  # noqa: E402

LEDGER_RELPATH = Path("events") / "provider_costs.ndjson"
TWO_SEATS = [
    {"label": "seat-a", "provider": "deepseek-harness", "model_arg": "deepseek-v4-pro"},
    {"label": "seat-b", "provider": "claude", "model_arg": "opus"},
]
DEEPSEEK_TIEBREAKER = ("deepseek-harness", "deepseek-v4-pro", "deepseek-v4-pro")


@pytest.fixture(autouse=True)
def _no_retry(monkeypatch):
    monkeypatch.setenv("VNX_PANEL_RETRY", "0")


def _ledger_lines(data_dir) -> list:
    path = Path(data_dir) / LEDGER_RELPATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _plan_report(verdict: str = "pass") -> str:
    return (
        "# review\n\nsome prose\n\n"
        f"```{pgp.VERDICT_FENCE}\n"
        f'{{"verdict": "{verdict}", "blocking_findings": [], "rationale": "ok"}}\n'
        "```\n"
    )


def _panel_dispatcher(verdict="pass", seen_texts=None):
    def _dispatch(provider, model_arg, instruction, dispatch_id):
        text = _plan_report(verdict)
        if seen_texts is not None:
            seen_texts.append(text)
        return text

    return _dispatch


def _tiebreaker_dispatcher():
    report = (
        "prose\n"
        f"```{pgt.TIEBREAKER_FENCE}\n"
        '{"outcome": "START", "required_change": "", "rationale": "ok"}\n'
        "```\n"
    )

    def _dispatch(provider, model_arg, instruction, dispatch_id):
        return report

    return _dispatch


def _run_panel(tmp_path, *, dispatcher, project_id="proj-a", panel=None, data_dir=None):
    return pgp.run_panel(
        doc_text="## Problem\nx\n", track_id="trk",
        project_id=project_id, panel=panel or TWO_SEATS, dispatcher=dispatcher,
        data_dir=str(data_dir or tmp_path), seat_ledger_path=tmp_path / "seats.ndjson",
    )


def _run_tiebreaker(tmp_path, *, dispatcher, project_id="proj-a", data_dir=None):
    return pgt.run_tiebreaker(
        doc_text="## Problem\n", track_id="trk", project_id=project_id, round_number=1,
        dispatcher=dispatcher, data_dir=str(data_dir or tmp_path),
    )


def _producer_and_execution(line):
    """``(producer, execution_id)`` of a line, from the run_id K3-1a derived from them."""
    _project, producer, execution_id, _attempt = cost_ledger.split_run_id(line["run_id"])
    return producer, execution_id


# ---------------------------------------------------------------------------
# Plan-gate seats
# ---------------------------------------------------------------------------


def test_a_two_seat_round_books_two_lines_each_naming_its_seat_and_model(tmp_path):
    out = _run_panel(tmp_path, dispatcher=_panel_dispatcher())

    assert out["decision"] == "PASS"
    lines = _ledger_lines(tmp_path)
    assert len(lines) == 2
    named = {_producer_and_execution(line)[0]: line for line in lines}
    assert set(named) == {"plan_gate-seat-a", "plan_gate-seat-b"}
    assert named["plan_gate-seat-a"]["model"] == "deepseek-v4-pro"
    assert named["plan_gate-seat-b"]["model"] == "opus"
    execution_ids = {_producer_and_execution(line)[1] for line in lines}
    assert len(execution_ids) == 2, "each seat run is its own execution"
    assert all(len(execution_id) == 32 for execution_id in execution_ids)
    assert all(line["role"] == "plan_gate" for line in lines)
    assert all(line["project_id"] == "proj-a" for line in lines)


def test_reading_the_same_report_twice_books_nothing_new(tmp_path):
    reports = []
    _run_panel(tmp_path, dispatcher=_panel_dispatcher(seen_texts=reports))
    assert len(_ledger_lines(tmp_path)) == 2

    # A retry of the parse reads the SAME report text again. The line belongs to
    # the run, not the report, so re-reading it can never book a second line.
    for _ in range(2):
        for text in reports:
            pgp.parse_verdict(text)

    assert len(_ledger_lines(tmp_path)) == 2


# ---------------------------------------------------------------------------
# Tiebreakers
# ---------------------------------------------------------------------------


def test_each_tiebreaker_attempt_is_its_own_line(tmp_path, monkeypatch):
    monkeypatch.setattr(pgt, "resolve_tiebreaker_model", lambda config=None: DEEPSEEK_TIEBREAKER)

    first = _run_tiebreaker(tmp_path, dispatcher=_tiebreaker_dispatcher())
    second = _run_tiebreaker(tmp_path, dispatcher=_tiebreaker_dispatcher())

    assert first.outcome == second.outcome == pgt.START
    lines = _ledger_lines(tmp_path)
    assert len(lines) == 2, "a second attempt is a new run, not a re-book"
    assert len({line["run_id"] for line in lines}) == 2
    assert all(line["role"] == "tiebreaker" for line in lines)
    assert all(line["provider"] == "deepseek-harness" for line in lines)
    assert all(line["model"] == "deepseek-v4-pro" for line in lines)


def test_a_door_refusal_books_a_zero_line(tmp_path, monkeypatch):
    monkeypatch.setattr(pgt, "resolve_tiebreaker_model", lambda config=None: DEEPSEEK_TIEBREAKER)

    def _refused(provider, model_arg, instruction, dispatch_id):
        raise RuntimeError("REJECT [paid_lane_daily_budget_exceeded] daily budget exhausted")

    with pytest.raises(pgt.TiebreakerNoAnswerError):
        _run_tiebreaker(tmp_path, dispatcher=_refused)

    lines = _ledger_lines(tmp_path)
    assert len(lines) == 1
    line = lines[0]
    assert line["cost_usd"] == 0.0
    assert line["cost_source"] == "unavailable:paid_lane_daily_budget_exceeded"
    assert line["usage_completeness"] == "complete"
    assert set(line["tokens"].values()) == {0}


# ---------------------------------------------------------------------------
# The tenant is the store's
# ---------------------------------------------------------------------------


def test_every_line_belongs_to_the_store_tenant_not_the_ambient_env(tmp_path, monkeypatch):
    tenant_store = tmp_path / ".vnx-data" / "tenant-b"
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    monkeypatch.setattr(pgt, "resolve_tiebreaker_model", lambda config=None: DEEPSEEK_TIEBREAKER)

    _run_panel(tmp_path, dispatcher=_panel_dispatcher(), project_id=None, data_dir=tenant_store)
    _run_tiebreaker(
        tmp_path, dispatcher=_tiebreaker_dispatcher(), project_id=None, data_dir=tenant_store,
    )

    lines = _ledger_lines(tenant_store)
    assert len(lines) == 3, "two seats and one tiebreaker, all in tenant-b's ledger"
    assert {line["project_id"] for line in lines} == {"tenant-b"}


# ---------------------------------------------------------------------------
# A booking never breaks a verdict
# ---------------------------------------------------------------------------


def test_a_refused_booking_leaves_the_verdict_unchanged_and_warns(
    tmp_path, monkeypatch, caplog,
):
    solo = [{"label": "solo", "provider": "claude", "model_arg": "opus"}]
    control = _run_panel(
        tmp_path, dispatcher=_panel_dispatcher("revise"), panel=solo, data_dir=tmp_path / "control",
    )

    def _refuse(*_args, **_kwargs):
        raise cost_ledger.CostLedgerRefusal("ledger says no")

    monkeypatch.setattr(cost_ledger, "record_once", _refuse)
    caplog.set_level(logging.WARNING, logger="harness_run_cost")
    refused = _run_panel(tmp_path, dispatcher=_panel_dispatcher("revise"), panel=solo)

    assert refused["decision"] == control["decision"] == "REVISE"
    assert not _ledger_lines(tmp_path), "the refused booking wrote nothing"
    warnings = [
        record for record in caplog.records
        if record.levelno >= logging.WARNING and "ledger says no" in record.getMessage()
    ]
    assert len(warnings) == 1, [record.getMessage() for record in caplog.records]
