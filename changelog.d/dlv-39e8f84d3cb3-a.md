- Removed four plan-gate panel environment variables; each is now a fixed value, the same one the code used when the variable was unset (config-een-oppervlak PR-2a, ADR-087):

- `VNX_PANEL_RETRY`: a flaked panelist is retried once (`PANEL_RETRY_COUNT = 1`).
- `VNX_PLAN_GATE_MAX_DOC_CHARS`: the plan document is cut at 400000 characters.
- `VNX_PLAN_GATE_REEXTRACT_TIMEOUT`: the verdict re-extraction deadline is 120 seconds.
- `VNX_PLAN_GATE_SEAT_TIMEOUT`: the per-seat deadline is 900 seconds; an explicit `--seat-timeout` still wins.

The only behaviour difference with today is that setting one of these variables no longer has an effect.
