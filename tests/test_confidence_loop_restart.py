"""The confidence loop books outcomes again.

Measured 2026-09-23 on the central store ``~/.vnx-data/vnx-dev/state``:
``confidence_events`` held one row, dated 13-06. Since 10-09 the ledger took in
164 success and 571 failure ``task_complete`` receipts and none of them reached
pattern confidence. Three separate causes, each pinned here:

1. ``append_receipt_payload(skip_enrichment=True)`` skipped EVERY post-append
   hook, the confidence update included. The report converter passes that flag
   to skip enrichment, so its receipts never updated confidence.
2. Where the update did run it resolved its store with
   ``resolve_state_dir(__file__)``, the checkout's own ``.vnx-data/state``. That
   store has no intelligence tables, so the insert died on "no such table",
   which is skipped silently. The receipts themselves live in the central
   per-project store (ADR-026).
3. The lane's own outcome receipt (``governance_emit.emit_dispatch_receipt``,
   the writer of the bulk of those 735 receipts) appends through the locked
   primitive directly and never ran a post-append hook at all.

The two stores the tests use: ``central`` (where the receipts are written, real
schema) and ``local`` (what ``project_root.resolve_state_dir`` answers under the
suite's isolation, real schema too, so a misrouted write is visible as a row).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import append_receipt  # registers the facade
import quality_db_init
import report_to_receipt_converter as rtc
from governance_emit import emit_dispatch_receipt
from test_report_to_receipt_converter import _write_frontmatter_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The tenant column the central store carries on the tables the confidence
# update touches (ADR-007). ``bootstrap_qi_db`` alone leaves it off; the
# production store has it, and the update takes a different path with it.
_TENANT_TABLES = ("success_patterns", "pattern_usage", "confidence_events", "dispatch_pattern_offered")


def _bootstrap_like_production(state_dir: Path) -> None:
    db = state_dir / "quality_intelligence.db"
    assert quality_db_init.bootstrap_qi_db(db)
    conn = sqlite3.connect(str(db))
    try:
        for table in _TENANT_TABLES:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "project_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN project_id TEXT DEFAULT 'vnx-dev'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def stores(tmp_path, monkeypatch) -> SimpleNamespace:
    """A central store and the checkout-local store, both with the real schema."""
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    central = tmp_path / "home" / ".vnx-data" / "vnx-dev" / "state"
    local = Path(os.environ["VNX_STATE_DIR"])
    assert central != local
    for state in (central, local):
        _bootstrap_like_production(state)
    return SimpleNamespace(central=central, local=local)


def _events(state_dir: Path, dispatch_id: str) -> list:
    conn = sqlite3.connect(str(state_dir / "quality_intelligence.db"))
    try:
        return conn.execute(
            "SELECT outcome, patterns_boosted, patterns_decayed, terminal "
            "FROM confidence_events WHERE dispatch_id = ? ORDER BY id",
            (dispatch_id,),
        ).fetchall()
    finally:
        conn.close()


def _seed_pattern(state_dir: Path, dispatch_id: str) -> int:
    """A success pattern grounded in ``dispatch_id`` (legacy source_dispatch_ids join)."""
    conn = sqlite3.connect(str(state_dir / "quality_intelligence.db"))
    try:
        cur = conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, source_dispatch_ids) "
            "VALUES ('approach', 'governance', 'seed', 'seed', '{}', 0.5, 0, ?)",
            (f'["{dispatch_id}"]',),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _hook(receipt: dict, state_dir: Path) -> None:
    """Run the confidence hook against ``state_dir``."""
    append_receipt._update_confidence_from_receipt(receipt, state_dir=state_dir)


def _outcome_receipt(dispatch_id: str, status: str = "success", **extra) -> dict:
    return {
        "event_type": "task_complete",
        "status": status,
        "dispatch_id": dispatch_id,
        "terminal": "T1",
        **extra,
    }


# ---------------------------------------------------------------------------
# Cause 1 + 2, the way it happens in production: the report converter
# ---------------------------------------------------------------------------

def test_converter_success_report_books_one_confidence_event_in_the_central_store(
    stores, tmp_path, monkeypatch,
):
    dispatch_id = "20260923-conf-converter"
    _seed_pattern(stores.central, dispatch_id)
    monkeypatch.setattr(rtc, "_check_branch_on_origin", lambda _did: True)
    report = _write_frontmatter_report(
        tmp_path / f"{dispatch_id}.md", dispatch_id, status="success",
    )

    result = rtc.convert_report_to_receipt(
        report, receipts_file=str(stores.central / "t0_receipts.ndjson"),
    )

    assert result is not None and result.status == "appended"
    assert _events(stores.central, dispatch_id) == [("success", 1, 0, "T1")], (
        "the converter booked a success receipt: exactly one confidence_events "
        "row belongs in the store the receipt was written to"
    )
    assert _events(stores.local, dispatch_id) == [], (
        "nothing may land in the checkout-local store"
    )


def test_converter_failure_report_books_a_failure_event_in_the_central_store(
    stores, tmp_path,
):
    dispatch_id = "20260923-conf-converter-fail"
    _seed_pattern(stores.central, dispatch_id)
    report = _write_frontmatter_report(
        tmp_path / f"{dispatch_id}.md", dispatch_id, status="failed",
    )

    result = rtc.convert_report_to_receipt(
        report, receipts_file=str(stores.central / "t0_receipts.ndjson"),
    )

    assert result is not None and result.status == "appended"
    assert _events(stores.central, dispatch_id) == [("failure", 0, 1, "T1")]
    assert _events(stores.local, dispatch_id) == []


# ---------------------------------------------------------------------------
# Cause 2 on its own: the store the hook writes to
# ---------------------------------------------------------------------------

def test_payload_append_updates_the_store_the_receipt_was_written_to(stores):
    dispatch_id = "20260923-conf-payload"
    receipt = _outcome_receipt(
        dispatch_id, model="sonnet", provider="claude", receipt_kind="test",
        timestamp="2026-09-23T10:00:00Z",
    )

    result = append_receipt.append_receipt_payload(
        receipt, receipts_file=str(stores.central / "t0_receipts.ndjson"),
    )

    assert result.status == "appended"
    assert len(_events(stores.central, dispatch_id)) == 1
    assert _events(stores.local, dispatch_id) == []


def test_hook_without_an_explicit_store_resolves_like_the_receipt_writer(
    stores, monkeypatch,
):
    """No state_dir passed: the store is VNX_STATE_DIR (the receipt writer's own
    first choice), never the checkout's ``.vnx-data``."""
    monkeypatch.setenv("VNX_STATE_DIR", str(stores.central))

    append_receipt._update_confidence_from_receipt(_outcome_receipt("20260923-conf-env"))

    assert len(_events(stores.central, "20260923-conf-env")) == 1
    assert _events(stores.local, "20260923-conf-env") == []


# ---------------------------------------------------------------------------
# skip_enrichment keeps skipping the quality advisory
# ---------------------------------------------------------------------------

def _append_with_hooks_mocked(tmp_path: Path, skip_enrichment: bool) -> dict:
    receipts_file = tmp_path / "state" / "t0_receipts.ndjson"
    receipt = _outcome_receipt(
        f"20260923-skip-{int(skip_enrichment)}", model="sonnet", provider="claude",
        receipt_kind="test", timestamp="2026-09-23T10:00:00Z",
    )
    mocks = {
        name: MagicMock(name=name)
        for name in (
            "_enrich_completion_receipt",
            "_register_quality_open_items",
            "_update_confidence_from_receipt",
            "_maybe_trigger_state_rebuild",
            "_trigger_receipt_classifier",
        )
    }
    mocks["_enrich_completion_receipt"].side_effect = lambda r: r
    patches = [patch.object(append_receipt, name, m) for name, m in mocks.items()]
    for p in patches:
        p.start()
    try:
        append_receipt.append_receipt_payload(
            receipt, receipts_file=str(receipts_file), skip_enrichment=skip_enrichment,
        )
    finally:
        for p in patches:
            p.stop()
    mocks["receipts_file"] = receipts_file
    return mocks


def test_skip_enrichment_skips_the_quality_advisory_but_not_the_outcome_hook(tmp_path):
    mocks = _append_with_hooks_mocked(tmp_path, skip_enrichment=True)

    mocks["_enrich_completion_receipt"].assert_not_called()
    mocks["_register_quality_open_items"].assert_not_called()
    mocks["_maybe_trigger_state_rebuild"].assert_not_called()
    mocks["_trigger_receipt_classifier"].assert_not_called()
    mocks["_update_confidence_from_receipt"].assert_called_once()
    kwargs = mocks["_update_confidence_from_receipt"].call_args.kwargs
    assert Path(kwargs["state_dir"]) == mocks["receipts_file"].parent


def test_without_skip_enrichment_every_hook_still_runs(tmp_path):
    mocks = _append_with_hooks_mocked(tmp_path, skip_enrichment=False)

    for name in (
        "_enrich_completion_receipt",
        "_register_quality_open_items",
        "_update_confidence_from_receipt",
        "_maybe_trigger_state_rebuild",
        "_trigger_receipt_classifier",
    ):
        mocks[name].assert_called_once()


# ---------------------------------------------------------------------------
# One outcome counts once, however many writers book it
# ---------------------------------------------------------------------------

def test_two_writers_booking_the_same_outcome_count_it_once(stores):
    dispatch_id = "20260923-conf-two-writers"
    _seed_pattern(stores.central, dispatch_id)

    # The report parser books ``done``, then the lane books ``success``: two
    # receipts, one outcome.
    _hook(_outcome_receipt(dispatch_id, "done"), stores.central)
    _hook(_outcome_receipt(dispatch_id, "success"), stores.central)

    assert _events(stores.central, dispatch_id) == [("success", 1, 0, "T1")]


def test_a_different_outcome_for_the_same_dispatch_is_a_new_fact(stores):
    dispatch_id = "20260923-conf-fix-forward"
    _seed_pattern(stores.central, dispatch_id)

    _hook(_outcome_receipt(dispatch_id, "failed"), stores.central)
    _hook(_outcome_receipt(dispatch_id, "success"), stores.central)

    assert [row[0] for row in _events(stores.central, dispatch_id)] == ["failure", "success"]


def test_the_same_outcome_is_scoped_to_its_project(stores, monkeypatch):
    dispatch_id = "20260923-conf-tenants"
    _hook(_outcome_receipt(dispatch_id), stores.central)
    monkeypatch.setenv("VNX_PROJECT_ID", "other-project")
    _hook(_outcome_receipt(dispatch_id), stores.central)

    conn = sqlite3.connect(str(stores.central / "quality_intelligence.db"))
    try:
        projects = conn.execute(
            "SELECT project_id FROM confidence_events WHERE dispatch_id = ? ORDER BY id",
            (dispatch_id,),
        ).fetchall()
    finally:
        conn.close()
    assert projects == [("vnx-dev",), ("other-project",)]


# ---------------------------------------------------------------------------
# A failure of the lane or the provider is not a failure of the patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "failure_class",
    ["empty_completion", "auth_rejected", "credit_exhausted", "tool_missing",
     "model_error", "no_verdict", "timeout"],
)
def test_lane_and_provider_failures_carry_no_pattern_signal(stores, failure_class):
    dispatch_id = f"20260923-conf-infra-{failure_class}"
    _seed_pattern(stores.central, dispatch_id)

    _hook(_outcome_receipt(dispatch_id, "failure", failure_class=failure_class), stores.central)

    assert _events(stores.central, dispatch_id) == []


@pytest.mark.parametrize("failure_class", [None, "completion_without_execution", "unknown"])
def test_a_failure_of_the_work_decays_the_patterns(stores, failure_class):
    dispatch_id = f"20260923-conf-work-{failure_class}"
    _seed_pattern(stores.central, dispatch_id)

    _hook(_outcome_receipt(dispatch_id, "failure", failure_class=failure_class), stores.central)

    assert _events(stores.central, dispatch_id) == [("failure", 0, 1, "T1")]


def test_a_success_is_never_filtered_by_failure_class(stores):
    dispatch_id = "20260923-conf-success-class"
    _seed_pattern(stores.central, dispatch_id)

    _hook(_outcome_receipt(dispatch_id, "success", failure_class="empty_completion"), stores.central)

    assert len(_events(stores.central, dispatch_id)) == 1


# ---------------------------------------------------------------------------
# Cause 3: the lane's own receipt
# ---------------------------------------------------------------------------

def _lane_kwargs(state_dir: Path, dispatch_id: str, status: str, **extra) -> dict:
    return dict(
        dispatch_id=dispatch_id,
        terminal_id="T1",
        provider="claude",
        model="claude-sonnet-4-6",
        pr_id=None,
        status=status,
        completion_pct=100 if status == "success" else 0,
        risk=0.0,
        findings=[],
        duration_seconds=12.5,
        token_usage={"input": 100, "output": 50, "cache_hit": 0},
        cost_usd=None,
        state_dir=state_dir,
        receipt_kind="dispatch",
        **extra,
    )


def test_lane_success_receipt_books_a_confidence_event(stores):
    dispatch_id = "20260923-conf-lane-success"
    _seed_pattern(stores.central, dispatch_id)

    emit_dispatch_receipt(**_lane_kwargs(stores.central, dispatch_id, "success"))

    # The lane names its terminal ``terminal_id``, the report-derived
    # receipts ``terminal``.
    assert _events(stores.central, dispatch_id) == [("success", 1, 0, "T1")]
    assert _events(stores.local, dispatch_id) == []


def test_lane_receipt_emitted_twice_counts_once(stores):
    dispatch_id = "20260923-conf-lane-twice"

    emit_dispatch_receipt(**_lane_kwargs(stores.central, dispatch_id, "success"))
    emit_dispatch_receipt(**_lane_kwargs(stores.central, dispatch_id, "success"))

    assert len(_events(stores.central, dispatch_id)) == 1


def test_lane_receipt_of_a_dispatch_that_never_ran_is_not_counted(stores):
    dispatch_id = "20260923-conf-lane-never-ran"

    emit_dispatch_receipt(
        **_lane_kwargs(
            stores.central, dispatch_id, "failure",
            failure_class="empty_completion", failure_reason="model returned no text",
        )
    )

    assert _events(stores.central, dispatch_id) == []


def test_lane_receipt_of_a_failed_run_is_counted(stores):
    dispatch_id = "20260923-conf-lane-failed-run"
    _seed_pattern(stores.central, dispatch_id)

    emit_dispatch_receipt(
        **_lane_kwargs(
            stores.central, dispatch_id, "failure",
            failure_class="completion_without_execution",
            failure_reason="tool calls left no change",
        )
    )

    assert _events(stores.central, dispatch_id) == [("failure", 0, 1, "T1")]
