.PHONY: subsystems-check live-health-check

# Diff docs/core/SUBSYSTEMS.md's deterministic columns (subsystem/what/flag/status)
# against the live `vnx subsystems --md` generator. The dynamic `health` column is
# excluded (framework-status-audit-and-cockpit PR-3). Wired into
# .github/workflows/subsystems-drift.yml.
subsystems-check:
	python3 scripts/check_subsystems_drift.py

# Fail when a cockpit subsystem claims LIVE while its own health reads
# "unknown" -- LIVE + unmeasured is a self-contradiction, not a legal
# combination (D6b). Wired into .github/workflows/subsystems-drift.yml.
live-health-check:
	python3 scripts/check_live_requires_measured_health.py
