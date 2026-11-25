# ops-per-project-garantie: Per-project health jobs and compressed archives

Each project's ledger-health job has its own launchd label and logs. The installed Python CLI's doctor reports a missing job, a colliding bare label or a missing health beacon, with the affected project identified. Event archive readers accept both plain and gzip-compressed NDJSON.

## How

Run `vnx doctor` from the project with the installed Python CLI; see `vnx doctor --help` for flags. From a source checkout, invoke those checks with `python3 -m vnx_cli.main doctor`. Follow the [event-stream runbook](../operations/EVENT_STREAMS.md) to inspect archived events.

## Modules

- [Ledger lifecycle](../modules/ledger-lifecycle.md)
- [Event streams](../modules/event-streams.md)
- [Central installation](../modules/central-install-cutover.md)

## Pull requests

Track closed: 2026-10-15 (UTC).

[#2030](https://github.com/Vinix24/vnx-orchestration/pull/2030), [#2029](https://github.com/Vinix24/vnx-orchestration/pull/2029).
