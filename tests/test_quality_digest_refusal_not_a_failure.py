#!/usr/bin/env python3
"""Golf Bx, D3 fix-forward: a refused merge is not a failed dispatch.

``build_t0_quality_digest._build_evidence_map`` classifies a dispatch as failed
on the receipt's ``status`` alone — ``failed``/``fail``/``error``/``blocked``,
with no ``event_type`` filter. The ``pr_merge_refused`` receipt introduced by
D3 carries ``status="blocked"`` (the canonical governed-failure literal; a new
literal like "refused" would be a fleet-wide vocabulary change) plus the
dispatch_id. From the merge door's first refusal onward, the nightly digest
would therefore count a dispatch whose door said "not yet" as a dispatch that
failed — a different population than it counted before this PR.

The concrete case: dispatch X is refused on the review preflight at 10:00, the
review verdict lands at 11:00, the PR merges at 11:30. All three receipts fall
inside the digest's 24h lookback and X is a success, yet it appears in
``failed_dispatch_ids``.

MEASURED, and why these tests drive ``_build_evidence_map`` directly rather
than through ``_load_recent_receipts``: that loader cannot parse the canonical
receipt timestamp. ``governance_receipts.utc_now_iso()`` emits whole seconds
with a ``Z`` suffix (``2026-09-08T12:00:00Z``), and the loader rewrites it to
``2026-09-08T12:00:00+00:00+00:00`` (``replace("Z", "+00:00")`` then
``split(".")[0] + "+00:00"``), which ``fromisoformat`` rejects into a
swallowed ``ValueError``. Over the live ledger on 2026-09-08 that silently
drops 22.598 of 29.536 lines; only the 6.934 sub-second timestamps survive.
A test that fed this classifier a canonically stamped refusal would therefore
pass no matter what the classifier did — it would never reach it. That loader
defect is a SEPARATE defect from the one this file pins, it is recorded as an
open item, and it is deliberately not fixed here: repairing it changes the
digest's input population fourfold, which is its own measurement and not a
rider on a one-line classification fix.

So the coupling under test is producer -> classifier: the refusal receipt is
built by the REAL ``pr_merge._emit_refusal_receipt`` and read back off disk,
so this file tracks the receipt shape ``pr_merge`` actually writes.

``scripts/learning_loop.py:424`` has the same status-only shape. It is dormant
and out of this PR's scope; it is recorded as an open item, not changed here.
"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pr_merge

digest = importlib.import_module("build_t0_quality_digest")


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _real_refusal(state_dir: Path, dispatch_id: str) -> Dict[str, Any]:
    """A refusal receipt as ``pr_merge`` actually writes it, read back off an
    isolated ledger — not a hand-copied literal that can drift from it."""
    ledger = state_dir / "emitted.ndjson"
    pr_merge._emit_refusal_receipt(
        pr_number=1823,
        dispatch_id=dispatch_id,
        preflight_gates=pr_merge._preflight_ledger([], refused_by="review"),
        refused_by="review",
        head_sha="d" * 40,
        receipts_file=str(ledger),
    )
    lines = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, "helper must produce exactly one refusal line"
    record = json.loads(lines[-1])
    assert record["status"] == "blocked", (
        "this test only means something while the refusal carries a "
        "failure-shaped status"
    )
    return record


def _merged(dispatch_id: str) -> Dict[str, Any]:
    return {
        "event_type": "pr_merged",
        "receipt_kind": "state_mutation",
        "status": "success",
        "terminal": "T0",
        "source": "pr_merge",
        "dispatch_id": dispatch_id,
        "pr_number": 1823,
        "conclusion": "merged",
    }


def _real_failure(dispatch_id: str) -> Dict[str, Any]:
    return {
        "event_type": "dispatch_completed",
        "receipt_kind": "dispatch",
        "status": "failed",
        "terminal": "T1",
        "source": "envelope_govern",
        "dispatch_id": dispatch_id,
    }


class TestRefusedMergeIsNotAFailedDispatch:
    def test_refusal_then_merge_is_not_a_failure(self, state_dir):
        """The dispatch's own failure scenario: refused at 10:00, merged at
        11:30, both inside the 24h window."""
        evidence = digest._build_evidence_map([
            _real_refusal(state_dir, "20260908-x"),
            _merged("20260908-x"),
        ])

        assert evidence["failed_dispatch_ids"] == []
        assert "20260908-x" in evidence["dispatch_ids"]

    def test_a_standing_refusal_is_still_not_a_failed_dispatch(self, state_dir):
        """Even with no merge behind it: the door refusing is a gate doing its
        job, not the dispatch failing. The refusal has its own event type and
        its own readers."""
        evidence = digest._build_evidence_map([_real_refusal(state_dir, "20260908-y")])

        assert evidence["failed_dispatch_ids"] == []
        assert evidence["dispatch_ids"] == ["20260908-y"]

    def test_a_real_failure_is_still_counted(self, state_dir):
        """The narrowing must not blunt the signal it was aimed around."""
        evidence = digest._build_evidence_map([
            _real_refusal(state_dir, "20260908-x"),
            _real_failure("20260908-z"),
        ])

        assert evidence["failed_dispatch_ids"] == ["20260908-z"]

    def test_a_dispatch_that_was_refused_and_also_really_failed_still_counts(
        self, state_dir,
    ):
        """The exclusion is scoped to the refusal RECEIPT, not to the dispatch:
        a dispatch carrying both a refusal and a genuine failure keeps its
        failure. This is why the fix is an event_type filter and not "a later
        success outranks an earlier failure" — the latter would silently drop
        real failures across every event type in the ledger, a far larger
        change to the metric than the defect being repaired."""
        evidence = digest._build_evidence_map([
            _real_refusal(state_dir, "20260908-w"),
            _real_failure("20260908-w"),
        ])

        assert evidence["failed_dispatch_ids"] == ["20260908-w"]

    def test_a_failure_status_without_a_readable_event_type_still_counts(self):
        """Negative path. The filter must not become a hole: a receipt whose
        event_type is missing or None is NOT exempt, it is unclassified, and an
        unclassified failure stays a failure. Also pins that the added
        ``event_type`` read cannot raise on ``None``."""
        evidence = digest._build_evidence_map([
            {"dispatch_id": "d1", "status": "blocked", "event_type": None},
            {"dispatch_id": "d2", "status": "error"},
        ])

        assert evidence["failed_dispatch_ids"] == ["d1", "d2"]

    def test_the_legacy_event_alias_is_honoured(self, state_dir):
        """Older ledger lines carry ``event`` rather than ``event_type``
        (``outcome_identity._resolve_event_name`` resolves both). A refusal
        written under the legacy key must be exempt too."""
        legacy = _real_refusal(state_dir, "20260908-legacy")
        legacy["event"] = legacy.pop("event_type")

        assert digest._build_evidence_map([legacy])["failed_dispatch_ids"] == []

    def test_the_excluded_event_type_is_named_not_inferred(self):
        """A reader of the digest must be able to see WHICH event types are
        exempt without re-deriving the rule from a status list."""
        assert "pr_merge_refused" in digest.NON_FAILURE_EVENT_TYPES


class TestEvidenceMapReachesTheClassifier:
    """One end-to-end pass through ``_load_recent_receipts`` so the wiring is
    covered too, using a sub-second timestamp — the only shape that loader
    accepts (see the module docstring's measurement)."""

    def _write(self, state_dir: Path, records: List[Dict[str, Any]]) -> None:
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "+00:00"
        with (state_dir / "t0_receipts.ndjson").open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(dict(r, timestamp=stamp)) + "\n")

    def test_a_refusal_loaded_from_disk_is_not_a_failure(self, state_dir):
        self._write(state_dir, [
            _real_refusal(state_dir, "20260908-e2e"),
            _real_failure("20260908-e2e-other"),
        ])

        loaded = digest._load_recent_receipts(state_dir)
        assert len(loaded) == 2, "loader precondition: both lines must arrive"

        evidence = digest._build_evidence_map(loaded)
        assert evidence["failed_dispatch_ids"] == ["20260908-e2e-other"]
        assert "20260908-e2e" in evidence["dispatch_ids"]
