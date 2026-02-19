# VNX Documentation

**Status**: Active
**Last Updated**: 2026-02-19
**Owner**: VNX Maintainer
**Purpose**: Describe how VNX documentation is organized and where to find the current source of truth.

---

## Start Here

Use `DOCS_INDEX.md` for the canonical "one place to look" navigation.

**Most Used**:
- Index: `DOCS_INDEX.md`
- Architecture: `core/00_VNX_ARCHITECTURE.md`
- PR workflow: `orchestration/README_PR_QUEUE.md`
- Monitoring: `operations/MONITORING_GUIDE.md`
- Dashboard: `core/00_GETTING_STARTED.md` (launch + URL)
- Roadmap: `roadmap/implementation/VNX_IMPLEMENTATION_ROADMAP.MD`

## Directory Overview

| Directory | Purpose |
|-----------|---------|
| `core/` | System fundamentals, dispatch/receipt formats, permissions |
| `core/technical/` | Deep technical references (dispatcher, intelligence, state) |
| `architecture/` | Strategy documents, design decisions, proposals |
| `operations/` | Monitoring, restart, receipt pipeline, daemon ops |
| `orchestration/` | PR workflow, T0 guides, skills, progress tracking |
| `intelligence/` | Intelligence system, tag taxonomy, token optimization |
| `testing/` | QA system, quality reviewer workflow |
| `roadmap/` | Project status, implementation roadmap, feature plans |
| `archive/` | Superseded/historical docs, organized by date |

## Documentation Rules (Source of Truth)

- One active doc per topic; overlapping/older docs are archived.
- Active docs use a consistent header:
  - `**Status**: Active | Draft | Deprecated`
  - `**Last Updated**: YYYY-MM-DD`
  - `**Owner**: Team/Role`
  - `**Purpose**: one line`
- Do not delete content unless it is duplicated in the canonical doc.
- Every active doc must be listed in `DOCS_INDEX.md`.

## Archive

Historical and superseded docs live in `archive/`.

- Each cleanup batch gets its own dated directory: `archive/YYYY-MM-DD-cleanup/`
- Each batch has an `ARCHIVE_README.md` explaining what was moved and why.
- Archived docs are tagged `**Status**: Archived`.
