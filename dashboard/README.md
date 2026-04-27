---
title: "VNX Token Usage Dashboard"
status: draft
last_updated: 2026-03-05
owner: T-MANAGER
summary: Interactive dashboard for Claude Code session analytics and token usage monitoring
---

# VNX Token Usage Dashboard

Interactive dashboard for monitoring Claude Code token usage, cache efficiency, and terminal workload across the VNX orchestration system.

Built on [Claud-ometer](https://github.com/deshraj/Claud-ometer) | Next.js 15 | Recharts | shadcn/ui | Tailwind CSS v4

## Features

- **Configurable periods** — view by day, week, or month with date range picker
- **Per-terminal breakdown** — T0, T1, T2, T3, T-MANAGER workload comparison
- **Model performance** — Opus vs Sonnet efficiency metrics
- **Cache efficiency** — hit ratios, context utilization, optimization trends
- **Per-call metrics** — correct token accounting (not cumulative totals)
- **Session drill-down** — individual session details with activity classification

## Quick Start

```bash
# From vnx-system root:
cd dashboard/token-dashboard/

# Install dependencies
npm install

# Development (hot reload)
npm run dev
# -> http://localhost:3100

# Production build
npm run build
# -> .next/ (Next.js output)
```

**Backend API server** (separate process): `python dashboard/serve_dashboard.py` — runs on port 4173.

**Prerequisites**: Node.js 18+, running VNX dashboard server (`launch-dashboard.sh`), populated `quality_intelligence.db`.

## Data Source

Reads from `quality_intelligence.db` → `session_analytics` table, populated nightly by `conversation_analyzer_nightly.sh` (launchd, 02:00).

Currently tracking **861 sessions** across 5 terminals from 2026-02-03 to present.

## Documentation

| Document | Description |
|----------|-------------|
| [PRD.md](PRD.md) | Product requirements, views, acceptance criteria |
| [TTD.md](TTD.md) | Technical design, API contract, token metrics specification |

## Architecture

```
quality_intelligence.db (SQLite)
        |
        v
serve_dashboard.py ---------- /api/token-stats
   port 4173                  /api/token-stats/sessions
        |
        v
React app (Next.js 15)
   port 5173 (dev)
   dist/ (production)
```

## Status

- [x] Data collection pipeline (`conversation_analyzer_nightly.sh`, launchd 02:00)
- [x] Token metrics specification (TTD)
- [x] Product requirements (PRD)
- [x] API endpoints (`/api/token-stats`, `/api/token-stats/sessions`, `/api/health` — `serve_dashboard.py`)
- [x] React frontend (`dashboard/token-dashboard/` — Next.js 15, port 3100)
- [ ] Production deployment
