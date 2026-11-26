# vnx-fleet-pin-audit: Installation drift across the fleet

Operators can inspect each project’s engine pin, dead links, release distance and checkout lag, plus mislabeled or writable releases in the central store. Dead or unregistered installations, store findings and measured release lag above the configured threshold make the audit fail; a deliberate older pin within the threshold remains a warning.

## How

From the engine checkout, run `python3 scripts/fleet_pin_audit.py --scan-root /path/to/projects --json`; see `python3 scripts/fleet_pin_audit.py --help` for flags. Follow the [central installation runbook](../operations/install-central-runbook.md) to repair a pin or installation after inspecting the report.

## Modules

- [Fleet pin audit](../modules/fleet-pin-audit.md)
- [Central installation](../modules/central-install-cutover.md)

## Pull requests

Track closed: 2026-10-27 (UTC).

[#2219](https://github.com/Vinix24/vnx-orchestration/pull/2219), [#2218](https://github.com/Vinix24/vnx-orchestration/pull/2218), [#2216](https://github.com/Vinix24/vnx-orchestration/pull/2216), [#2224](https://github.com/Vinix24/vnx-orchestration/pull/2224), [#2223](https://github.com/Vinix24/vnx-orchestration/pull/2223), [#2233](https://github.com/Vinix24/vnx-orchestration/pull/2233), [#2247](https://github.com/Vinix24/vnx-orchestration/pull/2247).
