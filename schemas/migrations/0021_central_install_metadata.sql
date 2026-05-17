-- VNX Migration 0021 — central install metadata + project pin tracking
-- Purpose: Bookkeeping for central install pinning and event history.
--
-- Design: claudedocs/roadmap/centralization.md
--
-- Pre-migration state (v20/v14): elastic worker pool tables.
-- Post-migration state (v21): adds central_install_pins, central_install_events.
--
-- Atomicity: single implicit transaction per statement (SQLite default).
-- Idempotency: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
--
-- Applied by: scripts/lib/central_install_db.py (implicit on first use)
-- Tested by: tests/test_central_install_db.py

CREATE TABLE IF NOT EXISTS central_install_pins (
    project_id  TEXT    NOT NULL,
    project_root TEXT   NOT NULL,
    pin_version TEXT    NOT NULL,
    pinned_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    pinned_by   TEXT,
    pin_source  TEXT    CHECK (pin_source IN ('vnx-version-file', 'env-VNX_PIN', 'auto')),
    notes       TEXT,
    PRIMARY KEY (project_id, project_root)
);

CREATE TABLE IF NOT EXISTS central_install_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT    NOT NULL,
    event_type    TEXT    NOT NULL CHECK (event_type IN ('install', 'update', 'rollback', 'verify', 'remove')),
    from_version  TEXT,
    to_version    TEXT,
    occurred_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    success       INTEGER NOT NULL DEFAULT 1,
    error_message TEXT,
    actor         TEXT,
    details_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_install_events_project ON central_install_events(project_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_install_events_type ON central_install_events(event_type);

-- Bump schema version
PRAGMA user_version = 21;
