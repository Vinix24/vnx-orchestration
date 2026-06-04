-- Seed script: builds scan_quota.db with the un-tenanted schema and 3 rows.
-- Re-runnable: drops the table first.

DROP TABLE IF EXISTS scan_quota;

CREATE TABLE scan_quota (
    id INTEGER PRIMARY KEY,
    scan_id TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    quota_limit INTEGER DEFAULT 100
);

INSERT INTO scan_quota (id, scan_id, used_count, quota_limit) VALUES
    (1, 'scan_a', 0, 100),
    (2, 'scan_b', 5, 100),
    (3, 'scan_c', 10, 100);
