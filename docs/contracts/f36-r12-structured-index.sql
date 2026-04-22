-- F36-R12 Structured Index — Reference Schema
-- Contract version: 1.0
-- Date: 2026-04-22
-- Status: Reference-only (not run against production DB in this PR)
--
-- Purpose: Define the single SQLite schema that both legacy VNX per-project
-- installs and the central daemon (W2+) will agree on. Every table carries
-- project_id (TEXT NOT NULL) as the mandatory tenancy boundary so that a
-- single DB instance can host multiple projects without cross-contamination.
--
-- Backward-compat guarantee: legacy JSON projections (open_items.json,
-- pr_queue_state.json, t0_receipts.ndjson, t0_decision_log.jsonl) can be
-- regenerated from these tables via SELECT ... WHERE project_id = ?.
--
-- Usage (schema-validity check only):
--   sqlite3 :memory: ".read docs/contracts/f36-r12-structured-index.sql" && echo OK
--
-- PRAGMA order matters: set before table creation.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA strict = ON;

-- ============================================================
-- SCHEMA VERSION
-- ============================================================
CREATE TABLE IF NOT EXISTS structured_index_schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT    NOT NULL
);

INSERT INTO structured_index_schema_version (version, description)
VALUES (1, 'F36-R12 initial schema — open_items, pr_queue, decisions, review_gates, receipts_index');

-- ============================================================
-- PROJECTS  (registry — populated by daemon; not present in legacy per-project mode)
-- ============================================================
-- In legacy mode every per-project install has its own DB file; the projects
-- table remains empty (or has a single self-row).  In daemon mode the projects
-- table is the authoritative registry; project_id FK is enforced.
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT NOT NULL PRIMARY KEY,   -- e.g. "vnx-roadmap-autopilot-wt"
    root_path     TEXT NOT NULL,               -- absolute path on host filesystem
    git_origin    TEXT,                        -- git remote origin URL
    token_hash    TEXT,                        -- HMAC-SHA256 of client token (hex)
    registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
) STRICT;

-- ============================================================
-- OPEN ITEMS
-- ============================================================
-- Mirrors the JSON schema from .vnx-data/state/open_items.json (schema_version 1.0).
-- evidence_refs and open_items_actions are stored as JSON arrays (TEXT column).
CREATE TABLE IF NOT EXISTS open_items (
    id                    TEXT NOT NULL,
    project_id            TEXT NOT NULL,
    severity              TEXT NOT NULL CHECK (severity IN ('blocker', 'warning', 'info')),
    status                TEXT NOT NULL CHECK (status  IN ('open', 'in_progress', 'done', 'wont_fix')),
    title                 TEXT NOT NULL,
    description           TEXT,
    pr_id                 TEXT,
    evidence_refs         TEXT,                -- JSON array of file paths / URLs
    resolution            TEXT,
    origin_dispatch_id    TEXT,
    origin_report_path    TEXT,
    closed_reason         TEXT,
    closed_by_dispatch_id TEXT,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    closed_at             TEXT,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects (id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_open_items_project_status
    ON open_items (project_id, status);

CREATE INDEX IF NOT EXISTS idx_open_items_pr
    ON open_items (project_id, pr_id);

CREATE INDEX IF NOT EXISTS idx_open_items_severity
    ON open_items (project_id, severity, status);

-- ============================================================
-- PR QUEUE
-- ============================================================
-- Mirrors pr_queue_state.json + FEATURE_PLAN.md PR metadata.
-- dependencies and gate_summary are stored as JSON (TEXT columns).
CREATE TABLE IF NOT EXISTS pr_queue (
    pr_id              TEXT NOT NULL,
    project_id         TEXT NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('queued', 'active', 'blocked', 'completed', 'skipped')),
    branch             TEXT,
    title              TEXT,
    skill              TEXT,                   -- e.g. "@backend-developer"
    size_estimate      INTEGER,               -- estimated LOC
    dependencies       TEXT,                  -- JSON array of pr_id strings, e.g. '["PR-1"]'
    active_dispatch_id TEXT,                  -- currently-executing dispatch
    gate_summary       TEXT,                  -- JSON object: {gate_type: verdict}
    github_pr_number   INTEGER,
    completed_at       TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (pr_id, project_id),
    FOREIGN KEY (project_id) REFERENCES projects (id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_pr_queue_project_state
    ON pr_queue (project_id, state);

-- ============================================================
-- DECISIONS  (T0 decision log — F36-R1 / R1b)
-- ============================================================
-- Provides structured storage for t0_decision_log.jsonl entries.
-- The JSONL file remains the primary write path in Phase 0/1; this table
-- is the canonical form from Phase 2 onward.
-- open_items_actions is stored as a JSON array.
CREATE TABLE IF NOT EXISTS decisions (
    id                 TEXT NOT NULL PRIMARY KEY,  -- ISO timestamp + random suffix
    project_id         TEXT NOT NULL,
    decision_type      TEXT NOT NULL CHECK (decision_type IN (
                           'dispatch', 'approve', 'reject', 'escalate',
                           'wait', 'close_oi', 'advance_gate')),
    dispatch_id        TEXT,
    track              TEXT CHECK (track IN ('A', 'B', 'C')),
    pr_id              TEXT,
    reasoning          TEXT NOT NULL,
    open_items_actions TEXT,                   -- JSON array of OI action strings
    next_expected      TEXT,                   -- free-text description of next expected event
    session_summary_at TEXT,                   -- ISO timestamp of originating T0 session
    source             TEXT CHECK (source IN ('haiku', 'direct', 'replay')),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (project_id) REFERENCES projects (id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_decisions_project_created
    ON decisions (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_decisions_dispatch
    ON decisions (dispatch_id);

CREATE INDEX IF NOT EXISTS idx_decisions_pr
    ON decisions (project_id, pr_id);

-- ============================================================
-- REVIEW GATES  (unified requests + results)
-- ============================================================
-- Merges .vnx-data/state/review_gates/requests/*.json and
-- .vnx-data/state/review_gates/results/*.json into one table.
-- findings, blocking_findings, advisory_findings, and changed_files
-- are JSON arrays stored as TEXT.
CREATE TABLE IF NOT EXISTS review_gates (
    id                TEXT NOT NULL PRIMARY KEY,   -- request_id from gate request
    project_id        TEXT NOT NULL,
    gate_type         TEXT NOT NULL CHECK (gate_type IN (
                          'codex', 'gemini_review', 'ci', 'burn_in', 'human')),
    pr_id             TEXT,
    pr_number         INTEGER,
    branch            TEXT,
    status            TEXT NOT NULL CHECK (status IN (
                          'pending', 'running', 'completed', 'failed', 'cancelled')),
    verdict           TEXT CHECK (verdict IN ('pass', 'fail', 'blocked')),
    contract_hash     TEXT,
    changed_files     TEXT,                    -- JSON array of file paths
    diff_stat         TEXT,
    report_path       TEXT,
    findings          TEXT,                    -- JSON array of {severity, message}
    blocking_findings TEXT,                    -- JSON array (subset of findings)
    advisory_findings TEXT,                    -- JSON array (subset of findings)
    required_reruns   INTEGER NOT NULL DEFAULT 0,
    residual_risk     TEXT,
    duration_seconds  REAL,
    requested_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at      TEXT,
    recorded_at       TEXT,
    FOREIGN KEY (project_id) REFERENCES projects (id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_review_gates_project_type
    ON review_gates (project_id, gate_type, status);

CREATE INDEX IF NOT EXISTS idx_review_gates_pr
    ON review_gates (project_id, pr_id);

CREATE INDEX IF NOT EXISTS idx_review_gates_status
    ON review_gates (project_id, status, requested_at DESC);

-- ============================================================
-- RECEIPTS INDEX
-- ============================================================
-- Structured form of .vnx-data/state/t0_receipts.ndjson.
-- raw_payload holds the full original JSON event for audit purposes.
CREATE TABLE IF NOT EXISTS receipts_index (
    id             TEXT NOT NULL PRIMARY KEY,   -- UUID or ISO+suffix
    project_id     TEXT NOT NULL,
    dispatch_id    TEXT NOT NULL,
    pr_id          TEXT,
    terminal       TEXT NOT NULL,              -- T1 / T2 / T3 / HEADLESS
    track          TEXT CHECK (track IN ('A', 'B', 'C')),
    gate           TEXT,
    status         TEXT NOT NULL CHECK (status IN (
                       'task_started', 'task_complete', 'task_failed',
                       'receipt_miss', 'delivery_miss')),
    confidence     REAL,
    report_path    TEXT,
    title          TEXT,
    auto_generated INTEGER NOT NULL DEFAULT 0,  -- 1 = synthetic receipt
    source         TEXT,                        -- e.g. "heartbeat_ack_monitor"
    raw_payload    TEXT,                        -- JSON of original receipt event
    sent_at        TEXT,
    confirmed_at   TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (project_id) REFERENCES projects (id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_receipts_dispatch
    ON receipts_index (project_id, dispatch_id);

CREATE INDEX IF NOT EXISTS idx_receipts_terminal_created
    ON receipts_index (project_id, terminal, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_receipts_status
    ON receipts_index (project_id, status, created_at DESC);
