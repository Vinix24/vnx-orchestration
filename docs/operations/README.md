# Operations

**Status**: Active
**Last Updated**: 2026-04-08
**Owner**: T-MANAGER
**Purpose**: Entry point for running, monitoring, and troubleshooting the VNX system.

---

## Canonical Docs

- Event streams: `EVENT_STREAMS.md`
- Receipt pipeline: `RECEIPT_PIPELINE.md`
- Receipt processing flow: `RECEIPT_PROCESSING_FLOW.md` *(historical — `report_watcher.sh` deprecated)*
- Runtime rollback: `RUNTIME_CORE_ROLLBACK.md`
- Subprocess adapter flag: `SUBPROCESS_ADAPTER_FEATURE_FLAG.md`
- Multi-model guide: `MULTI_MODEL_GUIDE.md`
- Autonomous production guide: `AUTONOMOUS_PRODUCTION_GUIDE.md`

> **Note**: `MONITORING_GUIDE.md` was retired. Runtime monitoring is now available
> via the dashboard server (`dashboard/serve_dashboard.py`) at `/api/health`.

## Public Operations Scope

Dispatch policy, queue behavior, and governance flow are documented in:

- `../DISPATCH_GUIDE.md`
- `../core/00_VNX_ARCHITECTURE.md`
- `../contracts/`

## Archive

Historical operational notes and one-off fix reports are archived under `../_archive/`.
