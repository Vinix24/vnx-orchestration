"""R10 (dispatch dlv-38447134aeec): a gate obligation at the retry cap is an open point for the T0.

The obligation runner parks an obligation whose gate ran twice on one PR head
without a bookable verdict (``degenerate_retry_cap``). The T0 reads
``open_outcomes`` in ``t0_index.json`` every cycle, so the parked obligation is
listed there as its own kind, ``gate_retry_cap``: beside any dispatch item of
the same id, not closed by an outcome decision, and only for this project
(ADR-007).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "scripts", _REPO / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import open_outcomes as oo  # noqa: E402
from build_t0_state import _build_open_outcomes, _open_outcomes_index_summary  # noqa: E402

PROJECT = "proj-a"
OTHER = "proj-b"
HEAD = "e5" * 20
DETAIL = (
    f"degenerate_retry_cap: codex_gate PR #2151 head {HEAD[:8]} ran 2 times without a verdict "
    "(last: gate_execution_degenerate); the runner does not run it again on this head."
)


@pytest.fixture(autouse=True)
def _project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)


def _obligation(state_dir: Path, dispatch_id: str, **fields: Any) -> Path:
    directory = state_dir / "review_gates" / "obligations"
    directory.mkdir(parents=True, exist_ok=True)
    record: Dict[str, Any] = {
        "kind": "review_gate_obligation", "dispatch_id": dispatch_id, "gate": "codex_gate",
        "project_id": PROJECT, "pr_number": 2151, "status": "pending", "attempts": 2,
        "last_attempt_at": "2026-10-21T13:13:18Z", "reason": "degenerate_retry_cap",
        "reason_detail": DETAIL, "head_runs": {"head_sha": HEAD, "reviewer_runs": 2},
    }
    record.update(fields)
    path = directory / f"{dispatch_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _section(state_dir: Path) -> Dict[str, Any]:
    return oo.build_open_outcomes(state_dir, project_id=PROJECT, limit=None)


def test_a_capped_obligation_is_an_open_point_of_its_own_kind(tmp_path):
    state_dir = tmp_path / "state"
    _obligation(state_dir, "20261021-release-v1-7-3")

    section = _section(state_dir)

    assert section["available"] is True
    assert section["by_kind"] == {"gate_retry_cap": 1}
    item = section["items"][0]
    assert item["kind"] == "gate_retry_cap" and item["outcome"] == "retry_cap"
    assert item["dispatch_id"] == "20261021-release-v1-7-3"
    assert (item["gate"], item["pr_number"], item["head_sha"]) == ("codex_gate", 2151, HEAD)
    assert item["reason"] == DETAIL
    assert item["last_seen"] == "2026-10-21T13:13:18+00:00"


@pytest.mark.parametrize("fields", [
    {"project_id": OTHER},
    {"reason": "gate_run_unavailable"},
    {"status": "fulfilled"},
], ids=["other-project", "not-capped", "terminal"])
def test_only_this_projects_pending_obligation_at_the_cap_is_listed(tmp_path, fields):
    state_dir = tmp_path / "state"
    _obligation(state_dir, "20261021-other", **fields)

    assert _section(state_dir)["items"] == []


def test_an_obligation_without_a_project_id_is_the_stores_own(tmp_path):
    state_dir = tmp_path / "state"
    _obligation(state_dir, "20261021-unstamped", project_id="")

    assert [i["dispatch_id"] for i in _section(state_dir)["items"]] == ["20261021-unstamped"]


def test_a_decision_on_the_dispatch_does_not_close_the_gate_point(tmp_path):
    state_dir = tmp_path / "state"
    _obligation(state_dir, "20261021-decided")
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / oo.LEDGER_NAME).write_text(json.dumps({
        "event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": "20261021-decided",
        "status": "failure", "timestamp": "2026-10-21T13:00:00Z", "project_id": PROJECT,
    }) + "\n", encoding="utf-8")

    before = _section(state_dir)
    oo.record_outcome_decision(state_dir / oo.DECISION_LOG_NAME, dispatch_id="20261021-decided",
                               project_id=PROJECT, decision="accept", reason="reviewed")
    after = _section(state_dir)

    assert sorted(i["kind"] for i in before["items"]) == ["gate_retry_cap", "receipt_outcome"], (
        "the gate point is listed beside the dispatch point of the same id"
    )
    assert [i["kind"] for i in after["items"]] == ["gate_retry_cap"], "a decision closes the dispatch, not the gate"


def test_an_unreadable_obligation_is_skipped_and_the_section_stays_available(tmp_path):
    state_dir = tmp_path / "state"
    _obligation(state_dir, "20261021-good")
    (state_dir / "review_gates" / "obligations" / "20261021-torn.json").write_text("{", encoding="utf-8")

    section = _section(state_dir)

    assert section["available"] is True
    assert [i["dispatch_id"] for i in section["items"]] == ["20261021-good"]


def test_the_t0_index_carries_the_capped_obligation(tmp_path):
    state_dir = tmp_path / "state"
    _obligation(state_dir, "20261021-release-v1-7-3")

    summary = _open_outcomes_index_summary(_build_open_outcomes(state_dir, PROJECT))

    assert summary["by_kind"] == {"gate_retry_cap": 1}
    assert summary["items"][0]["dispatch_id"] == "20261021-release-v1-7-3"
    assert summary["items"][0]["reason"].startswith("degenerate_retry_cap: codex_gate PR #2151")
