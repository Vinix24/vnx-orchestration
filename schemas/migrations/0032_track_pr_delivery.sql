-- 0032_track_pr_delivery.sql
-- OI-829: fail-closed auto-close on incomplete delivery.
--
-- Purpose: Record whether a PR linked to a track's pr_ref delivers a
--          'partial' or 'complete' slice of the track's plan. Without this,
--          close_track_if_done's evidence-path revalidation has no way to
--          distinguish "the whole plan shipped" from "PR 1 of 5 merged" —
--          both look identical to the merged-PR evidence check, so a track
--          auto-closes the moment ANY linked PR merges (the OI-829 bug:
--          worker-provider-free-choice closed after PR-1/5 merged).
--
-- ADR-007 binding: composite PRIMARY KEY (project_id, track_id, pr_number).
--          No index on this table may omit project_id.
--          See docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md
--
-- delivery_kind is a closed enum ('partial'|'complete') enforced by CHECK at
-- write time; readers (track_reconciler.close_track_if_done, planning_cli's
-- `objective show`) additionally raise loudly on any other value found in an
-- existing row — absence of a 'complete' marking must never read as evidence
-- of completion.
--
-- Target DB: runtime_coordination.db
-- Applied by: scripts/lib/migrations/apply_0032.py (via auto_apply.py)
-- Tested by:  tests/test_close_track_if_done.py, tests/test_track_pr_delivery.py
--
-- Idempotency: CREATE TABLE/INDEX IF NOT EXISTS; apply_script_if_below skips
--              the whole script when user_version >= 32.
--
-- Pre-migration state  (v31): no track_pr_delivery table.
-- Post-migration state (v32): track_pr_delivery exists, empty.

CREATE TABLE IF NOT EXISTS track_pr_delivery (
    project_id     TEXT    NOT NULL DEFAULT 'vnx-dev',
    track_id       TEXT    NOT NULL,
    pr_number      INTEGER NOT NULL,
    delivery_kind  TEXT    NOT NULL CHECK (delivery_kind IN ('partial', 'complete')),
    set_by         TEXT    NOT NULL,
    set_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (project_id, track_id, pr_number),
    FOREIGN KEY (track_id, project_id) REFERENCES tracks(track_id, project_id)
);

PRAGMA user_version = 32;
