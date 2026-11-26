# vnx-init-store-path-parity: Initialization and runtime agree on the store

A fresh project’s default store is the central per-project directory used by runtime resolution, including the shell fallback. The installed Python CLI’s doctor names a legacy XDG store whose data is no longer active, and the relocation command copies and upgrades it into the central store while preserving the source.

## How

Follow the [central installation runbook](../operations/install-central-runbook.md) for project setup, then run `vnx doctor` with the installed Python CLI; see `vnx doctor --help` for flags. From a source checkout, invoke those doctor checks with `python3 -m vnx_cli.main doctor`. For existing XDG stores, follow the [legacy-store migration runbook](../operations/LEGACY-XDG-STORE-MIGRATION.md) and consult `vnx migrate --help` before relocation.

## Modules

- [Central database routing](../modules/central-db-routing.md)
- [Migration mechanisms](../modules/migration-mechanisms.md)

## Pull requests

Track closed: 2026-10-27 (UTC).

[#2207](https://github.com/Vinix24/vnx-orchestration/pull/2207), [#2209](https://github.com/Vinix24/vnx-orchestration/pull/2209), [#2234](https://github.com/Vinix24/vnx-orchestration/pull/2234), [#2281](https://github.com/Vinix24/vnx-orchestration/pull/2281).
