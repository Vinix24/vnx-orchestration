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
- Getting started: `core/00_GETTING_STARTED.md`
- Monitoring: `operations/MONITORING_GUIDE.md`
- Design manifesto: `manifesto/ARCHITECTURE.md`

## Directory Overview

| Directory | Purpose |
|-----------|---------|
| `core/` | System fundamentals, dispatch/receipt formats, permissions |
| `core/technical/` | Deep technical references (dispatcher, intelligence, state) |
| `operations/` | Monitoring, restart, receipt pipeline, daemon ops |
| `intelligence/` | Intelligence system, tag taxonomy, token optimization |
| `manifesto/` | Design philosophy, architecture overview, limitations |
| `images/` | Screenshots and diagrams used in README |

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
