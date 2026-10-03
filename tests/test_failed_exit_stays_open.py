"""A failed worker exit must not silently end a dispatch (D4b2 defect).

``cleanup_worker_exit`` is the single post-exit cleanup for both lanes. On a
non-success exit its step 3 moves the dispatch file out of ``active/`` on its
own — ``failure``/``timeout``/``killed``/``stuck`` →
``dispatches/rejected/<reason>/`` — with no T0 decision behind it. That bucket
is storage, never an ending: ``open_outcomes`` reads it with the same predicate
as ``active/``, so the dispatch stays listed as an open point until a T0
records accept or reject for it, receipt or no receipt. ``completed/`` (the
success exit) stays out of the list, and one dispatch id is one item whatever
mix of ledger, ``active/`` and ``rejected/`` entries it has.

ADR-007: every scenario is read for one project, and a decision of another
project never closes this one's open point.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
for _p in (_SCRIPTS, _SCRIPTS / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cleanup_worker_exit as cwe  # noqa: E402
import open_outcomes as oo  # noqa: E402

PROJECT = "proj-a"
OTHER = "proj-b"
TS = "2026-09-29T10:00:00Z"
_DISPATCH_MD = "[[TARGET:T1]]\nTrack: A\nRole: backend-developer\n\n---\n\n## Instruction\n\nx\n"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch store, with the data/state env pinned to it so the cleanup's
    audit event and event archive land here, never in the real central store."""
    data = tmp_path / ".vnx-data"
    for sub in ("active", "completed", "dead_letter", "rejected"):
        (data / "dispatches" / sub).mkdir(parents=True)
    (data / "receipts" / "processed").mkdir(parents=True)
    (data / "state").mkdir()
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(data / "state"))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
    return data


def _active_md(data: Path, did: str) -> Path:
    path = data / "dispatches" / "active" / f"{did}.md"
    path.write_text(_DISPATCH_MD, encoding="utf-8")
    return path


def _processed(data: Path, did: str, status: str = "failure", project: str = PROJECT) -> None:
    (data / "receipts" / "processed" / f"1780000000-{did}-0.json").write_text(
        json.dumps({"event_type": "task_complete", "receipt_kind": "dispatch",
                    "dispatch_id": did, "status": status, "timestamp": TS,
                    "project_id": project}), encoding="utf-8")


def _cleanup(data: Path, did: str, exit_status: str, dispatch_file: Path) -> cwe.CleanupResult:
    return cwe.cleanup_worker_exit(
        terminal_id="T1", dispatch_id=did, exit_status=exit_status,
        dispatch_file=dispatch_file, state_dir=data / "state")


def _section(data: Path, project: str = PROJECT) -> Dict[str, Any]:
    section = oo.build_open_outcomes(data / "state", project_id=project, limit=None)
    assert section["available"], section
    return section


def _ids(data: Path, project: str = PROJECT) -> list:
    return [i["dispatch_id"] for i in _section(data, project)["items"]]


def _decide(data: Path, did: str, decision: str, project: str = PROJECT) -> None:
    oo.record_outcome_decision(data / "state" / oo.DECISION_LOG_NAME, dispatch_id=did,
                               project_id=project, decision=decision, reason="reviewed")


# ---------------------------------------------------------------------------
# A failed exit stays listed until a T0 decision; the decision closes it
# ---------------------------------------------------------------------------

def test_failed_exit_moves_to_rejected_and_stays_an_open_point(store: Path) -> None:
    did = "20260930-failed-A"
    src = _active_md(store, did)
    _processed(store, did, "failure")
    assert _ids(store) == [did], "an active dispatch with a failure receipt is an open point"

    result = _cleanup(store, did, "failure", src)

    moved = store / "dispatches" / "rejected" / "failure" / f"{did}.md"
    assert result.dispatch_moved == moved and moved.exists() and not src.exists()
    item = _section(store)["items"][0]
    assert (item["dispatch_id"], item["kind"], item["outcome"]) == (did, "active_dispatch", "reject")

    # a T0 decision closes the open point; the reason subdirectory keeps the file
    _decide(store, did, "reject")
    assert _ids(store) == []
    assert moved.exists(), "rejected/ stays as storage after the decision"


def test_failed_exit_without_a_receipt_is_an_open_point_at_once(store: Path) -> None:
    did = "20260930-silent-A"
    _cleanup(store, did, "timeout", _active_md(store, did))
    item = _section(store)["items"][0]
    assert (item["dispatch_id"], item["outcome"]) == (did, "no_receipt")


def test_decision_of_another_project_does_not_close_the_failed_exit(store: Path) -> None:
    did = "20260930-failed-B"
    _processed(store, did, "failure")
    _cleanup(store, did, "killed", _active_md(store, did))
    _decide(store, did, "reject", project=OTHER)
    assert _ids(store) == [did]


# ---------------------------------------------------------------------------
# A success exit is unchanged: completed/, no open point
# ---------------------------------------------------------------------------

def test_success_exit_goes_to_completed_and_is_no_open_point(store: Path) -> None:
    did = "20260930-ok-A"
    result = _cleanup(store, did, "success", _active_md(store, did))
    assert result.dispatch_moved == store / "dispatches" / "completed" / f"{did}.md"
    assert result.dispatch_moved.exists()
    assert _ids(store) == []


# ---------------------------------------------------------------------------
# One id is one item, whatever mix of buckets it has
# ---------------------------------------------------------------------------

def test_one_id_once_with_ledger_active_and_rejected_entries(store: Path) -> None:
    did = "20260930-mixed-A"
    (store / "state" / oo.LEDGER_NAME).write_text(json.dumps(
        {"event_type": "task_complete", "receipt_kind": "dispatch", "dispatch_id": did,
         "status": "failure", "timestamp": TS, "project_id": PROJECT}) + "\n", encoding="utf-8")
    _processed(store, did, "failure")
    _active_md(store, did)
    (store / "dispatches" / "rejected" / "failure").mkdir(exist_ok=True)
    (store / "dispatches" / "rejected" / "failure" / f"{did}.md").write_text(
        _DISPATCH_MD, encoding="utf-8")
    assert _ids(store) == [did]


def test_active_and_rejected_file_is_one_item(store: Path) -> None:
    did = "20260930-mixed-B"
    _processed(store, did, "failure")
    _active_md(store, did)
    (store / "dispatches" / "rejected" / "failure").mkdir(exist_ok=True)
    (store / "dispatches" / "rejected" / "failure" / f"{did}.md").write_text(
        _DISPATCH_MD, encoding="utf-8")
    assert _ids(store) == [did]


# ---------------------------------------------------------------------------
# A .md without a dispatch marker in rejected/ is no dispatch: ignored, counted
# ---------------------------------------------------------------------------

def test_rejected_md_without_a_dispatch_marker_is_ignored(store: Path) -> None:
    (store / "dispatches" / "rejected" / "failure").mkdir()
    (store / "dispatches" / "rejected" / "failure" / "README.md").write_text(
        "# what lives in rejected/\n", encoding="utf-8")
    section = _section(store)
    assert (section["items"], section["ignored"]) == ([], 1)
