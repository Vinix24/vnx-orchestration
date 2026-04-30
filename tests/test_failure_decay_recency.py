#!/usr/bin/env python3
"""Recency canary: failure decay is producing real signal in production.

Reads ``confidence_events`` from the project's live quality_intelligence.db
(via ``project_root``) and asserts that **at least one** failure outcome in
the last 7 days produced a non-zero ``confidence_change`` (i.e. patterns_decayed
>= 1). This catches silent regressions where the chain compiles but the
linkage breaks (e.g. injection-time stamping stops, LIKE pattern mismatch,
schema migration drops a column).

Skip semantics:
- Skip if the live DB is missing (CI / fresh worktree).
- Skip if there are zero failure events in the last 7 days (acceptable on
  quiet weeks — we cannot manufacture a failure to assert against).
- Skip if no failure dispatch in the window has a stamped pattern
  (``source_dispatch_ids LIKE %dispatch_id%``). Without an injection-time
  stamp the decay path can never fire, so the canary has no signal.

If failures **with** stamped patterns exist in the window and **none** of them
recorded ``patterns_decayed > 0`` or ``confidence_change != 0``, fail loudly:
the chain is silently broken.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from project_root import resolve_project_root  # noqa: E402


def _live_db_path() -> Path | None:
    override = os.environ.get("VNX_QUALITY_DB")
    if override:
        p = Path(override)
        return p if p.exists() else None
    try:
        root = resolve_project_root(__file__)
    except RuntimeError:
        return None
    candidate = root / ".vnx-data" / "state" / "quality_intelligence.db"
    return candidate if candidate.exists() else None


class FailureDecayRecencyCanary(unittest.TestCase):
    """Live-DB canary — skipped on CI when the DB is absent."""

    def test_recent_failure_produced_decay(self) -> None:
        db_path = _live_db_path()
        if db_path is None:
            self.skipTest("live quality_intelligence.db not available")

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            failures = conn.execute(
                "SELECT dispatch_id, patterns_decayed, confidence_change, occurred_at "
                "FROM confidence_events "
                "WHERE outcome = 'failure' AND occurred_at >= ?",
                (cutoff,),
            ).fetchall()

            if not failures:
                self.skipTest("no failure receipts in the last 7d (quiet week)")

            # Only failures whose dispatch_id is actually stamped on at least
            # one success_patterns row should produce decay. Failures from the
            # pre-#326 era (or for dispatches that received no patterns) carry
            # no signal here — they're filtered out so the canary doesn't fire
            # on historical breakage.
            stamped_failures = []
            for f in failures:
                hit = conn.execute(
                    "SELECT 1 FROM success_patterns "
                    "WHERE source_dispatch_ids LIKE ? LIMIT 1",
                    (f"%{f['dispatch_id']}%",),
                ).fetchone()
                if hit:
                    stamped_failures.append(f)

            if not stamped_failures:
                self.skipTest(
                    "no failure dispatch in last 7d has a stamped pattern — "
                    "either the window predates #326 or no patterns were "
                    "offered to failing dispatches"
                )

            with_decay = [
                f for f in stamped_failures
                if (f["patterns_decayed"] or 0) > 0
                or (f["confidence_change"] or 0.0) != 0.0
            ]

            self.assertGreater(
                len(with_decay),
                0,
                "Failure-decay regression: "
                f"{len(stamped_failures)} failure event(s) in last 7d had "
                "stamped source_dispatch_ids on success_patterns, but 0 events "
                "recorded any patterns_decayed or confidence_change. The chain "
                "is silently broken — investigate record_injection stamping and "
                "update_confidence_from_outcome LIKE matching.",
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
