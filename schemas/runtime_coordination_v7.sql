-- VNX Runtime Coordination Schema — Migration v7 (FP-D PR-4)
-- Purpose: Provenance verification audit trail — tracks verification runs,
--          findings, and advisory guardrail outputs.
-- Applies on top of: v6 (FP-D PR-2) — provenance registry
--
-- Design notes:
--   - provenance_verifications logs each verification run with findings
--   - Advisory guardrails emit recommendations as coordination_events
--   - Verification can be run per-dispatch or as a batch sweep
--   - Findings are stored as JSON for flexible gap/issue tracking
--
-- Governance:
--   G-R7: Dispatch, receipt, commit, and PR must be bidirectionally traceable
--   A-R9: No silent policy mutation from recommendation logic

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- PROVENANCE VERIFICATIONS
-- ============================================================================
-- Append-only audit log for provenance verification runs.
-- Each run inspects one dispatch's provenance chain and records findings.
--
-- Verdict values:
--   pass     — chain is complete and consistent
--   warning  — chain has non-blocking gaps (incomplete but usable)
--   fail     — chain has blocking gaps or contradictions (broken)
--
-- Invariants:
--   - One row per verification run (dispatch_id + verified_at is unique in practice)
--   - findings_json records structured gap/issue details
--   - advisory_json records non-mutating recommendations

CREATE TABLE IF NOT EXISTS provenance_verifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id TEXT    NOT NULL UNIQUE,
    dispatch_id     TEXT    NOT NULL,
    verdict         TEXT    NOT NULL,
    chain_status    TEXT    NOT NULL,
    findings_json   TEXT    DEFAULT '[]',
    advisory_json   TEXT    DEFAULT '[]',
    verified_by     TEXT    NOT NULL DEFAULT 'provenance_verification',
    verified_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metadata_json   TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_verification_dispatch
    ON provenance_verifications (dispatch_id, verified_at DESC);
CREATE INDEX IF NOT EXISTS idx_verification_verdict
    ON provenance_verifications (verdict, verified_at DESC);

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (7, 'FP-D PR-4: provenance verification audit trail — verification runs and findings');
