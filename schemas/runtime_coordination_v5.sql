-- VNX Runtime Coordination Schema — Migration v5 (FP-D PR-1)
-- Purpose: Governance evaluation engine — escalation state tracking,
--          governance overrides, and policy evaluation audit trail.
-- Applies on top of: v4 (FP-C PR-0) — execution targets, intelligence, recommendations
--
-- Design notes:
--   - escalation_state tracks the current governance attention level per entity
--   - governance_overrides logs every override request and outcome
--   - Policy evaluation events go to coordination_events (existing table)
--   - All tables use CREATE TABLE IF NOT EXISTS for idempotency
--
-- Governance:
--   G-R1: Every automatic action maps to a policy class (enforced in governance_evaluator.py)
--   G-R2: High-risk actions are always gated (policy matrix classification)
--   G-R3: Repeated failure loops escalate to hold or escalate states
--   A-R2: Escalation states are explicit (this schema)
--   A-R3: Autonomy evaluation emits runtime events (coordination_events)
--   A-R7: Policy overrides are durable governance events (governance_overrides)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- ESCALATION STATE
-- ============================================================================
-- Tracks the current governance attention level for an entity.
-- One row per (entity_type, entity_id) — updated in-place on transitions.
--
-- Escalation levels:
--   info             — normal operations, no attention needed
--   review_required  — flagged for operator review, non-blocking
--   hold             — action paused, operator must release
--   escalate         — requires T0 intervention
--
-- Invariants:
--   - Exclusive: one level per entity at any time
--   - Monotonically increasing: runtime can only increase severity
--   - Only operator/T0 can de-escalate
--   - hold and escalate are blocking; info and review_required are not

CREATE TABLE IF NOT EXISTS escalation_state (
    entity_type         TEXT NOT NULL,
    entity_id           TEXT NOT NULL,
    escalation_level    TEXT NOT NULL DEFAULT 'info',
    trigger_category    TEXT,
    trigger_description TEXT,
    policy_class        TEXT,
    decision_type       TEXT,
    retry_count         INTEGER,
    budget_remaining    INTEGER,
    escalated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at         TEXT,
    resolved_by         TEXT,
    resolution_note     TEXT,
    PRIMARY KEY (entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_escalation_level
    ON escalation_state (escalation_level, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_escalation_unresolved
    ON escalation_state (resolved_at, escalation_level);

-- ============================================================================
-- GOVERNANCE OVERRIDES
-- ============================================================================
-- Append-only audit log for every governance override request.
-- Each override is scope-limited to one entity instance (never global).
--
-- Override types:
--   gate_bypass | invariant_override | dispatch_force_promote
--   dead_letter_override | hold_release | escalation_resolve
--
-- Outcomes:
--   granted | denied

CREATE TABLE IF NOT EXISTS governance_overrides (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    override_id         TEXT    NOT NULL UNIQUE,
    entity_type         TEXT    NOT NULL,
    entity_id           TEXT    NOT NULL,
    actor               TEXT    NOT NULL,
    override_type       TEXT    NOT NULL,
    justification       TEXT    NOT NULL,
    outcome             TEXT    NOT NULL,
    previous_level      TEXT,
    new_level           TEXT,
    policy_class        TEXT,
    decision_type       TEXT,
    override_scope      TEXT,
    occurred_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    metadata_json       TEXT    DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_override_entity
    ON governance_overrides (entity_type, entity_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_override_actor
    ON governance_overrides (actor, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_override_outcome
    ON governance_overrides (outcome, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_override_type
    ON governance_overrides (override_type, occurred_at DESC);

-- ============================================================================
-- SCHEMA VERSION
-- ============================================================================

INSERT OR IGNORE INTO runtime_schema_version (version, description)
VALUES (5, 'FP-D PR-1: governance evaluation engine — escalation state, governance overrides');
