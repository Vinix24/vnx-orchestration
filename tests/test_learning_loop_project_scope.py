#!/usr/bin/env python3
"""Project scoping of the learning loop's failure-pattern reader (ADR-007).

``extract_failure_patterns`` read every failure receipt in the governed ledger
and turned it into a failure record for ``generate_prevention_rules`` /
``persist_to_intelligence_db`` — including lines stamped with another project's
``project_id``. Dispatch ids collide across projects, so a foreign failure under
this project's dispatch id could become this project's prevention rule and
antipattern row. These tests pin the reader to the same project test every other
ledger reader uses: a line without ``project_id`` is this ledger's own; a line
stamped with another project's id is left out and counted.

Real code paths against tmp stores only. Nothing touches ``~/.vnx-data``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import learning_loop as ll


def _now_iso(offset_hours: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()


def _write_receipts(path: Path, receipts: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in receipts) + "\n", encoding="utf-8"
    )


def _failure(dispatch_id: str, reason: str, project_id=None, **extra) -> dict:
    receipt = {
        "status": "failed",
        "dispatch_id": dispatch_id,
        "failure_reason": reason,
        "provider": "claude",
        "terminal": "T1",
        "timestamp": _now_iso(-1),
    }
    if project_id is not None:
        receipt["project_id"] = project_id
    receipt.update(extra)
    return receipt


def _loop_with_db(tmp_path: Path, monkeypatch, project_id: str = "vnx-dev"):
    """A loop resolving its own project through the same ``db_path`` layout the
    loop's other project-scoped work uses (ADR-007)."""
    monkeypatch.setenv("VNX_PROJECT_ID", project_id)
    state_dir = tmp_path / ".vnx-data" / project_id / "state"
    state_dir.mkdir(parents=True)
    loop = ll.LearningLoop.__new__(ll.LearningLoop)
    loop.receipts_path = state_dir / "t0_receipts.ndjson"
    loop.db_path = state_dir / "quality_intelligence.db"
    return loop, state_dir


def _window():
    return datetime.now(timezone.utc) - timedelta(days=2)


# ---------------------------------------------------------------------------
# (a) the reported defect: colliding dispatch ids across two projects
# ---------------------------------------------------------------------------

def test_foreign_project_failure_with_colliding_dispatch_id_is_left_out(
    tmp_path, monkeypatch, capsys
):
    loop, _ = _loop_with_db(tmp_path, monkeypatch, "vnx-dev")
    _write_receipts(
        loop.receipts_path,
        [
            _failure("20260930-shared", "own failure", project_id="vnx-dev"),
            _failure("20260930-shared", "FOREIGN failure", project_id="seocrawler-v2"),
        ],
    )

    failures = loop.extract_failure_patterns(start_time=_window())

    assert [f["error"] for f in failures] == ["own failure"], (
        "another project's failure under a colliding dispatch id must not "
        "become this project's failure pattern"
    )
    assert loop.receipt_stats["foreign_project"] == 1

    out, _ = capsys.readouterr()
    assert "1 foreign-project filtered" in out, "foreign count must reach the corpus log"


def test_only_own_project_failures_reach_prevention_rules(tmp_path, monkeypatch):
    loop, _ = _loop_with_db(tmp_path, monkeypatch, "vnx-dev")
    _write_receipts(
        loop.receipts_path,
        [
            _failure("d1", "own failure", project_id="vnx-dev"),
            _failure("d2", "own failure", project_id="vnx-dev"),
            _failure("d1", "FOREIGN failure", project_id="seocrawler-v2"),
            _failure("d2", "FOREIGN failure", project_id="seocrawler-v2"),
        ],
    )

    rules = loop.generate_prevention_rules(
        loop.extract_failure_patterns(start_time=_window())
    )

    assert len(rules) == 1
    assert "own failure" in rules[0]["pattern"]
    assert rules[0]["occurrence_count"] == 2


# ---------------------------------------------------------------------------
# (b) a receipt without project_id is this ledger's own
# ---------------------------------------------------------------------------

def test_receipt_without_project_id_counts_as_own(tmp_path, monkeypatch):
    loop, _ = _loop_with_db(tmp_path, monkeypatch, "vnx-dev")
    _write_receipts(
        loop.receipts_path,
        [
            _failure("d-unstamped", "unstamped failure"),
            _failure("d-foreign", "FOREIGN failure", project_id="seocrawler-v2"),
        ],
    )

    failures = loop.extract_failure_patterns(start_time=_window())

    assert [f["error"] for f in failures] == ["unstamped failure"]
    assert loop.receipt_stats["foreign_project"] == 1


def test_own_project_stamp_is_kept(tmp_path, monkeypatch):
    loop, _ = _loop_with_db(tmp_path, monkeypatch, "vnx-dev")
    _write_receipts(loop.receipts_path, [_failure("d", "own failure", project_id="vnx-dev")])

    failures = loop.extract_failure_patterns(start_time=_window())

    assert [f["error"] for f in failures] == ["own failure"]
    assert loop.receipt_stats["foreign_project"] == 0


def test_new_only_receipts_path_resolves_own_project_from_env(tmp_path, monkeypatch):
    """The ``LearningLoop.__new__`` shape that sets only ``receipts_path`` has no
    db_path anchor; the resolver's ``VNX_PROJECT_ID`` source still identifies the
    project, exactly as the reproduction sets it (``VNX_PROJECT_ID=vnx-dev``)."""
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    receipts_path = tmp_path / "t0_receipts.ndjson"
    loop = ll.LearningLoop.__new__(ll.LearningLoop)
    loop.receipts_path = receipts_path
    _write_receipts(
        receipts_path,
        [
            _failure("d", "own failure", project_id="vnx-dev"),
            _failure("d", "FOREIGN failure", project_id="seocrawler-v2"),
        ],
    )

    failures = loop.extract_failure_patterns(start_time=_window())

    assert [f["error"] for f in failures] == ["own failure"]
    assert loop.receipt_stats["foreign_project"] == 1


# ---------------------------------------------------------------------------
# (c) unresolvable own project: no stamped line is attributable to this project
# ---------------------------------------------------------------------------

def test_unresolved_project_excludes_stamped_but_keeps_unstamped(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    receipts_path = tmp_path / "t0_receipts.ndjson"
    # Only receipts_path set and no env source: the db_path anchor is absent and
    # VNX_PROJECT_ID is gone, so the own project is unresolved.
    loop = ll.LearningLoop.__new__(ll.LearningLoop)
    loop.receipts_path = receipts_path
    _write_receipts(
        receipts_path,
        [
            _failure("d-stamped", "stamped failure", project_id="vnx-dev"),
            _failure("d-unstamped", "unstamped failure"),
        ],
    )

    failures = loop.extract_failure_patterns(start_time=_window())

    assert [f["error"] for f in failures] == ["unstamped failure"], (
        "when the own project cannot be resolved, a stamped receipt must not be "
        "counted as this project's; an unstamped one still belongs to the ledger"
    )
    assert loop.receipt_stats["foreign_project"] == 1

    out, _ = capsys.readouterr()
    assert "1 foreign-project filtered" in out
