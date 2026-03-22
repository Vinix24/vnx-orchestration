---
title: "VNX Token Usage Dashboard — Product Requirements"
status: draft
last_updated: 2026-03-05
owner: T-MANAGER
summary: Product requirements for the VNX token usage analytics dashboard
---

# VNX Token Usage Dashboard — PRD

## Problem Statement

The VNX orchestration system runs 5 Claude Code terminals (T0, T1, T2, T3, T-MANAGER) with hundreds of sessions per month. There is currently no visibility into:

- How many tokens are consumed per terminal, model, or time period
- Whether prompt caching is working efficiently
- Which terminals are over- or underutilized
- How context window utilization changes over time
- Whether model selection (Opus vs Sonnet) aligns with task complexity

Operators must run ad-hoc SQL queries to get basic insights — a manual, error-prone process.

## Users

| User | Need |
|------|------|
| VNX Operator | Daily/weekly usage overview, cache health monitoring |
| System Architect | Model efficiency comparison, terminal workload balancing |
| Cost Analyst | Token consumption trends, optimization opportunities |

## Goals

1. **Configurable time view** — switch between day, week, month aggregation with date range picker
2. **Terminal-level insights** — workload, efficiency, and activity type per terminal
3. **Model comparison** — Opus vs Sonnet performance on identical metric set
4. **Cache health monitoring** — hit ratios, context utilization trends
5. **Correct metrics** — per-API-call averages, not misleading cumulative totals

## Non-Goals

- Multi-user authentication or authorization
- Cloud hosting or remote access
- Real-time streaming (polling refresh is sufficient)
- Cost calculations (operator uses Max subscription, no per-token billing)
- Conversation replay (Claud-ometer already provides this; we focus on analytics)

## Base

Fork of [Claud-ometer](https://github.com/deshraj/Claud-ometer) (Next.js 15 + Recharts + shadcn/ui + Tailwind CSS v4). Claud-ometer provides the UI shell, chart components, and session browser. We replace its JSONL file reader with a SQLite data source.

## Data Source

**Database**: `quality_intelligence.db` → `session_analytics` table
**Population**: Nightly via `conversation_analyzer_nightly.sh` (launchd, 02:00)
**Volume**: ~860 sessions, ~30 days history, growing at ~28 sessions/day

### Available Fields

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | TEXT | Unique session identifier |
| `terminal` | TEXT | T0, T1, T2, T3, T-MANAGER, unknown |
| `session_model` | TEXT | claude-opus, claude-sonnet, unknown |
| `session_date` | DATE | Session start date |
| `total_input_tokens` | INT | Non-cached input (cumulative over session) |
| `total_output_tokens` | INT | Claude output (cumulative over session) |
| `cache_creation_tokens` | INT | Newly cached input (cumulative over session) |
| `cache_read_tokens` | INT | Cache-served input (cumulative over session) |
| `assistant_message_count` | INT | Number of API calls in session |
| `tool_calls_total` | INT | Total tool invocations |
| `tool_read_count` | INT | Read tool calls |
| `tool_edit_count` | INT | Edit tool calls |
| `tool_bash_count` | INT | Bash tool calls |
| `primary_activity` | TEXT | debugging, research, coding, refactoring, mixed |
| `has_error_recovery` | BOOL | Whether error recovery was detected |
| `duration_minutes` | REAL | Session wall-clock duration |

## Dashboard Views

### View 1: Overview (Landing Page)

**Purpose**: At-a-glance system health and usage trends.

| Component | Metric | Description |
|-----------|--------|-------------|
| KPI Card | Total Sessions | Sessions in selected period |
| KPI Card | Total API Calls | Sum of assistant_message_count |
| KPI Card | Avg Context/Call | Average context window utilization (K tokens) |
| KPI Card | Cache Hit % | Percentage of context served from cache |
| Area Chart | API Calls Over Time | Stacked by terminal, grouped by period |
| Donut Chart | Model Distribution | Opus vs Sonnet session split |
| Heatmap | Activity Pattern | Sessions by day-of-week and hour |

**Period Selector**: Day / Week / Month toggle + custom date range picker.

### View 2: Token Analysis

**Purpose**: Deep dive into token consumption patterns.

| Component | Metric |
|-----------|--------|
| Line Chart | Avg context per call over time (per terminal) |
| Bar Chart | Cache hit % per terminal per week |
| Table | Top 10 heaviest sessions (by API calls or total tokens) |
| Trend Line | New tokens per call over time (measures context growth) |

### View 3: Terminal Comparison

**Purpose**: Compare workload and efficiency across terminals.

| Component | Metric |
|-----------|--------|
| Grouped Bar | Sessions, API calls, output tokens side-by-side |
| Radar Chart | Activity profile (% debugging, research, coding, refactoring) |
| Trend Lines | Per-terminal weekly trends |
| Table | Terminal summary with all KPIs |

### View 4: Model Performance

**Purpose**: Compare Opus vs Sonnet on identical metrics.

| Component | Metric |
|-----------|--------|
| Side-by-Side Cards | Same KPIs for each model |
| Efficiency Chart | Output tokens per API call (productivity measure) |
| Activity Distribution | Which model handles which task types |
| Bar Chart | Average session duration by model |

## Acceptance Criteria

### AC-1: Period Selection
- User can switch between day/week/month grouping
- Custom date range picker filters all views
- URL reflects selected period (shareable state)

### AC-2: Correct Token Metrics
- All token displays use per-API-call averages (see TTD for specification)
- Context per call values fall within 20K-200K range
- Cumulative totals are never presented as "session size"
- Cache hit percentage matches SQL validation query

### AC-3: Terminal Breakdown
- All 5 terminals (T0, T1, T2, T3, T-MANAGER) shown separately
- "unknown" terminal sessions grouped and visible
- Terminal filter works across all views

### AC-4: Data Freshness
- Dashboard shows data timestamp ("Last updated: ...")
- Stale data (>24h) shows warning indicator
- Manual refresh button available

### AC-5: Responsive Layout
- Dark theme consistent with VNX system branding
- Usable on 13" laptop screen (minimum viewport)
- Charts readable without horizontal scrolling
