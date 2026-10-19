-- The seven 0015 runtime tables in the shape measured on 2026-10-15 on a
-- read-only copy of the sales-copilot store at user_version 33: the
-- sqlite_master SQL of each table and its explicit indexes, verbatim.
-- project_id is NOT NULL without a default and part of composite natural
-- keys. Used by tests/test_migration_0034_project_id_completion.py.

CREATE TABLE "recommendations" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "recommendation_id" TEXT NOT NULL,
  "recommendation_class" TEXT NOT NULL,
  "title" TEXT NOT NULL,
  "description" TEXT NOT NULL,
  "evidence_summary" TEXT NOT NULL,
  "confidence" REAL NOT NULL DEFAULT (0.0),
  "scope_tags_json" TEXT NOT NULL DEFAULT ('[]'),
  "acceptance_state" TEXT NOT NULL DEFAULT ('proposed'),
  "proposed_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "accepted_at" TEXT,
  "rejected_at" TEXT,
  "rejection_reason" TEXT,
  "expired_at" TEXT,
  "superseded_by" TEXT,
  "outcome_window_start" TEXT,
  "outcome_window_end" TEXT,
  "metadata_json" TEXT DEFAULT ('{}'),
  "project_id" TEXT NOT NULL,
  UNIQUE ("recommendation_id", "project_id")
);
CREATE INDEX idx_rec_class
    ON recommendations (recommendation_class, acceptance_state);
CREATE INDEX idx_rec_scope
    ON recommendations (acceptance_state);
CREATE INDEX idx_rec_state
    ON recommendations (acceptance_state, proposed_at DESC);
CREATE INDEX idx_recommendations_project         ON recommendations(project_id);

CREATE TABLE "recommendation_outcomes" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "recommendation_id" TEXT NOT NULL,
  "metric_name" TEXT NOT NULL,
  "baseline_value" REAL,
  "baseline_sample_size" INTEGER NOT NULL DEFAULT (0),
  "outcome_value" REAL,
  "outcome_sample_size" INTEGER NOT NULL DEFAULT (0),
  "delta" REAL,
  "direction" TEXT,
  "comparison_status" TEXT NOT NULL DEFAULT ('pending'),
  "computed_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "project_id" TEXT NOT NULL,
  UNIQUE ("recommendation_id", "metric_name", "project_id"),
  FOREIGN KEY ("recommendation_id", "project_id") REFERENCES "recommendations" ("recommendation_id", "project_id") ON UPDATE NO ACTION ON DELETE NO ACTION
);
CREATE INDEX idx_outcome_metric
    ON recommendation_outcomes (metric_name, comparison_status);
CREATE INDEX idx_outcome_rec
    ON recommendation_outcomes (recommendation_id);
CREATE INDEX idx_recommendation_outcomes_project ON recommendation_outcomes(project_id);

CREATE TABLE "retry_budgets" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "budget_key" TEXT NOT NULL,
  "entity_type" TEXT NOT NULL,
  "entity_id" TEXT NOT NULL,
  "incident_class" TEXT NOT NULL,
  "attempts_used" INTEGER NOT NULL DEFAULT (0),
  "max_retries" INTEGER NOT NULL,
  "cooldown_seconds" INTEGER NOT NULL DEFAULT (0),
  "backoff_factor" REAL NOT NULL DEFAULT (1.0),
  "max_cooldown_seconds" INTEGER NOT NULL DEFAULT (600),
  "last_attempt_at" TEXT,
  "next_allowed_at" TEXT,
  "escalated_at" TEXT,
  "auto_recovery_halted" INTEGER NOT NULL DEFAULT (0),
  "created_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "updated_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "project_id" TEXT NOT NULL,
  UNIQUE ("budget_key", "project_id")
);
CREATE INDEX idx_budget_class
    ON retry_budgets (incident_class);
CREATE INDEX idx_budget_entity
    ON retry_budgets (entity_type, entity_id);
CREATE INDEX idx_budget_halted
    ON retry_budgets (auto_recovery_halted);
CREATE INDEX idx_budget_next_allowed
    ON retry_budgets (next_allowed_at);
CREATE INDEX idx_retry_budgets_project           ON retry_budgets(project_id);

CREATE TABLE "retry_state" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "dispatch_id" TEXT NOT NULL,
  "incident_class" TEXT NOT NULL,
  "attempts_used" INTEGER NOT NULL DEFAULT (0),
  "last_attempt_at" TEXT,
  "next_eligible_at" TEXT,
  "budget_exhausted" INTEGER NOT NULL DEFAULT (0),
  "escalated" INTEGER NOT NULL DEFAULT (0),
  "halted" INTEGER NOT NULL DEFAULT (0),
  "metadata_json" TEXT DEFAULT ('{}'),
  "created_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "updated_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "project_id" TEXT NOT NULL,
  UNIQUE ("dispatch_id", "incident_class", "project_id")
);
CREATE INDEX idx_retry_dispatch
    ON retry_state (dispatch_id);
CREATE INDEX idx_retry_exhausted
    ON retry_state (budget_exhausted, dispatch_id);
CREATE INDEX idx_retry_state_project             ON retry_state(project_id);

CREATE TABLE "escalation_log" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "escalation_id" TEXT NOT NULL,
  "incident_id" TEXT NOT NULL,
  "dispatch_id" TEXT,
  "terminal_id" TEXT,
  "incident_class" TEXT NOT NULL,
  "severity" TEXT NOT NULL,
  "escalated_to" TEXT NOT NULL DEFAULT ('T0'),
  "reason" TEXT NOT NULL,
  "retry_count" INTEGER NOT NULL DEFAULT (0),
  "budget_exhausted" INTEGER NOT NULL DEFAULT (0),
  "auto_recovery_halted" INTEGER NOT NULL DEFAULT (0),
  "acknowledged" INTEGER NOT NULL DEFAULT (0),
  "acknowledged_at" TEXT,
  "metadata_json" TEXT DEFAULT ('{}'),
  "created_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "project_id" TEXT NOT NULL,
  UNIQUE ("escalation_id", "project_id")
);
CREATE INDEX idx_escalation_dispatch
    ON escalation_log (dispatch_id, created_at DESC);
CREATE INDEX idx_escalation_log_project          ON escalation_log(project_id);
CREATE INDEX idx_escalation_unacked
    ON escalation_log (acknowledged, created_at DESC);

CREATE TABLE "execution_targets" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "target_id" TEXT NOT NULL,
  "target_type" TEXT NOT NULL,
  "terminal_id" TEXT,
  "capabilities_json" TEXT NOT NULL DEFAULT ('[]'),
  "health" TEXT NOT NULL DEFAULT ('offline'),
  "health_checked_at" TEXT,
  "model" TEXT,
  "registered_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "updated_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "metadata_json" TEXT DEFAULT ('{}'),
  "project_id" TEXT NOT NULL,
  UNIQUE ("project_id", "target_id")
);
CREATE INDEX idx_execution_targets_project ON execution_targets (project_id);
CREATE INDEX idx_target_health ON execution_targets (health);
CREATE INDEX idx_target_terminal ON execution_targets (terminal_id);
CREATE INDEX idx_target_type ON execution_targets (target_type);

CREATE TABLE "inbound_inbox" (
  "id" INTEGER PRIMARY KEY AUTOINCREMENT,
  "event_id" TEXT NOT NULL,
  "channel_id" TEXT NOT NULL,
  "dedupe_key" TEXT NOT NULL,
  "state" TEXT NOT NULL DEFAULT ('received'),
  "payload_json" TEXT NOT NULL,
  "routing_hints_json" TEXT DEFAULT ('{}'),
  "dispatch_id" TEXT,
  "attempt_count" INTEGER NOT NULL DEFAULT (0),
  "max_retries" INTEGER NOT NULL DEFAULT (3),
  "failure_reason" TEXT,
  "received_at" TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  "processed_at" TEXT,
  "metadata_json" TEXT DEFAULT ('{}'),
  "project_id" TEXT NOT NULL,
  UNIQUE ("event_id", "project_id")
);
CREATE INDEX idx_inbound_inbox_project           ON inbound_inbox(project_id);
CREATE UNIQUE INDEX idx_inbox_dedupe
    ON inbound_inbox (channel_id, dedupe_key);
CREATE INDEX idx_inbox_dispatch
    ON inbound_inbox (dispatch_id);
CREATE INDEX idx_inbox_state
    ON inbound_inbox (state, received_at DESC);
