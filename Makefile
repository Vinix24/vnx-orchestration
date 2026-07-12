.PHONY: subsystems-check

# Diff docs/core/SUBSYSTEMS.md's deterministic columns (subsystem/what/flag/status)
# against the live `vnx subsystems --md` generator. The dynamic `health` column is
# excluded (framework-status-audit-and-cockpit PR-3). Wired into
# .github/workflows/subsystems-drift.yml.
subsystems-check:
	python3 scripts/check_subsystems_drift.py
