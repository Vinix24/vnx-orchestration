# VNX Documentation

**Status**: Active
**Last Updated**: 2026-04-08
**Owner**: VNX Maintainer
**Purpose**: Describe how VNX documentation is organized and where to find the current source of truth.

---

## Start Here

Use `DOCS_INDEX.md` for the canonical "one place to look" navigation.

**Most Used**:
- Index: `DOCS_INDEX.md`
- Architecture: `core/00_VNX_ARCHITECTURE.md`
- Getting started: `core/00_GETTING_STARTED.md`
- Dispatch workflow: `DISPATCH_GUIDE.md`
- Monitoring: `operations/MONITORING_GUIDE.md`
- Product modes: `contracts/PRODUCTIZATION_CONTRACT.md`
- Roadmap: `manifesto/ROADMAP.md`

## Directory Overview

| Directory | Purpose |
|-----------|---------|
| `core/` | System architecture, file formats, technical references, and core contracts |
| `contracts/` | Feature-level and platform contracts that govern runtime behavior |
| `operations/` | Monitoring, multi-model usage, receipts, rollback, and runtime operations |
| `intelligence/` | Public intelligence reference docs such as tag taxonomy and cost tracking |
| `manifesto/` | Public narrative docs: architecture story, roadmap, limitations, open method |
| `onboarding/` | New-user orientation and first-run setup guidance |
| `examples/` | Example orchestration flows for coding, research, and content work |
| `comparisons/` | Positioning docs that compare VNX to direct CLI use and frameworks |
| `_archive/` | Historical or superseded docs kept for reference only |

## Documentation Rules (Source of Truth)

- One active doc per topic; overlapping/older docs are archived.
- Active docs use a consistent header:
  - `**Status**: Active | Draft | Deprecated`
  - `**Last Updated**: YYYY-MM-DD`
  - `**Owner**: Team/Role`
  - `**Purpose**: one line`
- Private planning, business strategy, and internal research do not live in this repo.
- Do not delete content unless it is duplicated in the canonical doc or moved to the private BUSINESS workspace.
- Every active doc must be listed in `DOCS_INDEX.md`.

## Archive

Historical and superseded docs live in `_archive/`.

- Archive only docs that still have traceability value.
- If a document is private rather than merely historical, move it out of the repo instead of archiving it here.
