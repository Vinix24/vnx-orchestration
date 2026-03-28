# Session Intelligence & Human-in-the-Loop System Tuning

**Version**: 2.0.0
**Added**: 2026-03-03
**Updated**: 2026-03-28 (self-learning pipeline: session discovery fix, consolidated nightly pipeline)

## Overview

The Session Intelligence system mines Claude Code JSONL session logs, extracts model-based performance patterns, and generates suggested system improvements — all through a **human-in-the-loop** workflow where nothing is auto-applied.

### Design Principles

1. **Model-based, not terminal-based**: Terminals have no persistent state (`/clear` after each dispatch). The meaningful performance dimension is the **model** (claude-opus, claude-sonnet, codex, gemini).
2. **Nothing auto-applied**: All system changes flow through `pending_edits.json` as suggestions. The user reviews, accepts/rejects, then applies.
3. **Read-only auto outputs**: Only `t0_session_brief.json` is auto-generated — it's a read-only state file consumed by T0.

## Architecture

```
Nightly Pipeline (conversation_analyzer_nightly.sh)
├── Phase 1: conversation_analyzer.py
│   └── Parse JSONL → session_analytics DB (incl. session_model)
├── Phase 2: generate_t0_session_brief.py
│   └── Aggregate by model → t0_session_brief.json (auto, read-only)
└── Phase 3: generate_suggested_edits.py
    └── Analyze patterns → pending_edits.json (human review required)

Morning workflow:
  vnx suggest review          → See pending suggestions
  vnx suggest accept 1,3,5   → Accept specific edits
  vnx suggest reject 2,4     → Reject with optional reason
  vnx suggest apply           → Apply accepted edits to files
  vnx suggest history         → View previously applied edits
```

## Model Tracking

### How it works

The conversation analyzer extracts the model from the first assistant message in each JSONL session:

```
record["message"]["model"] → e.g. "claude-opus-4-1-20250805"
```

This is normalized to a canonical family name via `normalize_model()`:

| Raw Model ID | Normalized |
|---|---|
| `claude-opus-4-1-20250805` | `claude-opus` |
| `claude-sonnet-4-5-20250514` | `claude-sonnet` |
| `claude-haiku-4-5-20251001` | `claude-haiku` |
| `codex-mini-latest` | `codex` |
| `gemini-2.0-flash` | `gemini` |
| (unknown) | `unknown` |

### Database

The `session_model` column was added to `session_analytics` (schema v8.0.5):

```sql
ALTER TABLE session_analytics ADD COLUMN session_model TEXT DEFAULT 'unknown';
CREATE INDEX idx_session_model ON session_analytics (session_model, session_date DESC);
```

## T0 Session Brief (Auto, Read-Only)

**Script**: `generate_t0_session_brief.py`
**Output**: `$VNX_STATE_DIR/t0_session_brief.json`

This file is consumed by T0 for dispatch intelligence. It contains:

### model_performance

Aggregated 7-day metrics per model:

```json
{
  "claude-opus": {
    "sessions_7d": 18,
    "avg_tokens_per_session": 52000,
    "primary_activities": {"coding": 10, "debugging": 5, "research": 3},
    "error_recovery_rate": 0.15,
    "cache_hit_ratio": 0.94,
    "avg_duration_minutes": 28.5
  }
}
```

### model_routing_hints

Task-type recommendations based on model success patterns:

```json
[
  {
    "task_type": "refactoring",
    "recommended_model": "claude-opus",
    "confidence": 0.85,
    "evidence": "10/12 succesvol op claude-opus, 3/7 op claude-sonnet"
  }
]
```

Hints are only generated when:
- At least 2 models have data for the same activity
- At least 3 sessions per model-activity combination
- Success rate difference >= 15%

### active_concerns

Model-specific warnings when error rates exceed 30%:

```json
[
  {
    "model": "claude-sonnet",
    "concern": "Hoge error recovery rate bij storage taken (45%)",
    "recommendation": "Overweeg een ander model voor complexe storage taken"
  }
]
```

### Receipt Footer

The receipt processor includes a MODEL line from the session brief:

```
📈 MODEL: claude-opus avg=52K tok | err=15% | cache=94% | claude-sonnet avg=31K tok | err=25%
```

## Suggested Edits (Human-in-the-Loop)

**Script**: `generate_suggested_edits.py`
**Output**: `$VNX_STATE_DIR/pending_edits.json`

### Edit Categories

| Category | Target | Example |
|---|---|---|
| `memory` | Terminal MEMORY.md | Model success patterns, token profiles |
| `rule` | `.claude/rules/*.md` | Threshold tightening (p95, success rates) |
| `claude_md` | Terminal CLAUDE.md | New sections (session intelligence references) |
| `skill` | Skill template.md | Intelligence references for dispatch |

### Suggestion Rules

**Memory suggestions**:
- Only generated with >= 5 sessions evidence per model
- Confidence >= 0.70 required
- Max 5 suggestions per nightly run
- Model comparisons require >= 20% success rate difference
- Duplicates are fingerprinted and never re-suggested

**Rule suggestions**:
- Only with >= 10 data points
- Only tighten thresholds (never loosen — safer)
- Must exceed current threshold by >= 20%

**One-time suggestions**:
- Session brief references for T0 CLAUDE.md and T0 skill
- Auto-detected: if section doesn't exist, suggest adding it
- After first apply, never re-suggested

### pending_edits.json Format

```json
{
  "generated_at": "2026-03-03T02:15:00Z",
  "edits": [
    {
      "id": 1,
      "category": "memory",
      "target": "MEMORY.md",
      "section": "## Geleerde Patronen",
      "action": "append",
      "content": "- claude-opus: coding taken 83% first-try success vs claude-sonnet 40%",
      "evidence": "5/6 opus vs 2/5 sonnet (afgelopen 7d)",
      "confidence": 0.85,
      "status": "pending",
      "_fingerprint": "a1b2c3d4e5f6g7h8",
      "suggested_at": "2026-03-03T02:15:00Z"
    }
  ]
}
```

### Status Flow

```
pending → accepted → applied (archived to edit_history.json)
pending → rejected (archived with reason)
```

## CLI Reference

### vnx suggest review

Show all pending edits with category, confidence, evidence, and status.

### vnx suggest accept \<ids\>

Mark specific edits as accepted. Comma-separated IDs: `vnx suggest accept 1,3,5`

### vnx suggest reject \<ids\> [--reason "..."]

Mark edits as rejected with optional reason: `vnx suggest reject 2 --reason "te agressief"`

### vnx suggest apply

Apply all accepted edits to their target files:
- `memory` → Appends to section in MEMORY.md
- `rule` → Replaces content in rules file
- `claude_md` → Appends section to CLAUDE.md
- `skill` → Appends section to skill template

Applied edits are archived to `$VNX_STATE_DIR/edit_history.json`.

### vnx suggest history

Show the last 20 applied edits with timestamps and content.

## Session Discovery & Idempotency (PR-1 Fix)

The session analyzer previously failed to populate `session_analytics` due to a `CLAUDE_PROJECTS_DIR` path mismatch. PR-1 fixed this by:

1. **Path validation**: Added fallback discovery for Claude Code session logs across both main repo and worktree contexts
2. **Dry-run diagnostics**: `vnx analyze-sessions --dry-run` reports found sessions and estimated processing time without writing to DB
3. **Idempotency**: Re-running the analyzer skips already-imported sessions (keyed by `session_id UNIQUE`)
4. **Downstream consumers**: Once populated, `session_analytics` feeds: session brief, suggested edits, governance metrics, model routing, and session-dispatch linkage

## Consolidated Nightly Intelligence Pipeline (11 Phases)

The `nightly_intelligence_pipeline.sh` consolidates the previously overlapping daily (18:00) and nightly (02:00) pipelines into a single ordered pipeline. Each phase runs independently — a failure in one phase does not block subsequent phases.

**File**: `scripts/nightly_intelligence_pipeline.sh`
**Schedule**: Daily at 02:00 via launchd plist
**Lock**: PID-based singleton with stale lock recovery

| Phase | Script | Purpose |
|-------|--------|---------|
| 0 | `quality_db_init.py` | Schema migrations |
| 1a | `code_quality_scanner.py` | Code complexity analysis |
| 1b | `code_snippet_extractor.py` | Extract patterns from code |
| 1c | `doc_section_extractor.py` | Mine documentation |
| 2 | `conversation_analyzer.py --max-sessions 50 --deep-budget 20` | Session parsing + LLM analysis |
| 3 | `link_sessions_dispatches.py` | Correlate sessions with dispatches |
| 4 | `learning_loop.py run` | Update pattern confidence, queue archival |
| 5 | `tag_intelligence.py stale` | Mark 7+ day old edits stale |
| 6 | `generate_t0_session_brief.py` | Generate session summary |
| 7 | `governance_aggregator.py --backfill` | Compute metrics, SPC control limits |
| 8 | `generate_suggested_edits.py` | Generate human-in-loop recommendations |
| 9 | `build_t0_quality_digest.py` | 3-section quality digest (NDJSON + JSON) |
| 10 | `generate_t0_recommendations.py --lookback 1440` | 24h-window recommendations |
| 11 | `send_digest_email.py` | Email digest (optional, env-gated) |

### Pipeline Health Tracking

```bash
# Per-phase results (append-only NDJSON)
$VNX_STATE_DIR/nightly_pipeline_phases.ndjson

# Summary health file
$VNX_STATE_DIR/nightly_pipeline_health.json
```

### Health Gates

Health checks run between pipeline phases. Each phase result is logged with status, duration, and error (if any). The pipeline continues past failures, recording the failure for operator review.

### Phase 7: Governance Measurement

Governance aggregation (now Phase 7 in the consolidated pipeline) is documented in detail in `GOVERNANCE_MEASUREMENT.md`. It computes:
- **CQS backfill** for dispatches that completed before the governance system was installed
- **FPY, rework rate, gate velocity, mean CQS** per scope (system/terminal/role/gate/model)
- **SPC control limits** (X-bar +/- 3 sigma from 30-day baseline)
- **Anomaly detection** via Western Electric rules (out-of-control, trend, shift, run)

## Email Digest (Opt-in)

**Script**: `send_digest_email.py`

The nightly pipeline can email a digest with model performance, routing hints, concerns, and pending suggested edits.

### Configuration

Set these environment variables (e.g. in `~/.zshrc`):

```bash
export VNX_DIGEST_EMAIL="user@gmail.com"   # Recipient address (required)
export VNX_SMTP_PASS="xxxx xxxx xxxx xxxx"  # Gmail App Password (required)
# Optional overrides:
# export VNX_SMTP_USER="sender@gmail.com"  # Defaults to VNX_DIGEST_EMAIL
# export VNX_SMTP_HOST="smtp.gmail.com"    # Default
# export VNX_SMTP_PORT="587"               # Default
```

Gmail requires an [App Password](https://myaccount.google.com/apppasswords) (2FA must be enabled).

### Digest Contents

The email includes:
- **Model Performance** — sessions, tokens, error rates, cache ratios per model
- **Routing Hints** — which model performs best for which task type
- **Warnings** — models with high error rates on specific activities
- **Pending Suggested Edits** — with `vnx suggest` CLI instructions
- **Analyzer Log** — last 15 lines for quick health check

### Manual Usage

```bash
# Dry run (print to stdout, no email sent)
python3 scripts/send_digest_email.py --dry-run

# Send email
VNX_DIGEST_EMAIL="user@gmail.com" VNX_SMTP_PASS="..." python3 scripts/send_digest_email.py
```

### Design Decisions

- **Opt-in**: Phase 4 is skipped when env vars are not set — zero impact for users who don't configure it
- **No hardcoded addresses**: All SMTP config via environment variables
- **Generic SMTP**: Works with any SMTP provider, not just Gmail
- **Non-fatal**: Email failure does not affect the rest of the nightly pipeline

## Nightly Digest Integration

The nightly digest includes a "Voorgestelde Wijzigingen" section:

```markdown
## Voorgestelde Wijzigingen (3 pending)

Review met: `vnx suggest review`
Accepteer: `vnx suggest accept 1,3`
Afwijzen:  `vnx suggest reject 2`
Toepassen: `vnx suggest apply`

### #1 [MEMORY] MEMORY.md
**Toevoegen**: "claude-opus: coding taken 83% first-try success..."
**Confidence**: 0.85 | **Bewijs**: 5/6 opus vs 2/5 sonnet
```

## Testing

Tests are in `tests/test_conversation_analyzer.py`:

- `TestNormalizeModel` — 8 tests for model normalization
- `TestModelExtraction` — 3 tests for JSONL model extraction
- `TestSessionBrief` — 2 tests for brief generation and concern detection
- `TestSuggestedEdits` — 7 tests for edit generation, dedup, digest rendering
- `TestApplySuggestedEdits` — 5 tests for accept/reject/apply flow

Run: `python3 -m pytest tests/test_conversation_analyzer.py -v`

## Files

| File | Purpose |
|---|---|
| `scripts/conversation_analyzer.py` | Session parser with model extraction and path discovery fix (PR-1) |
| `scripts/generate_t0_session_brief.py` | T0 read-only state file generator |
| `scripts/generate_suggested_edits.py` | Suggestion engine |
| `scripts/apply_suggested_edits.py` | Accept/reject/apply CLI |
| `scripts/send_digest_email.py` | Opt-in email digest sender (SMTP) |
| `scripts/nightly_intelligence_pipeline.sh` | Consolidated 11-phase nightly pipeline (PR-4) |
| `scripts/conversation_analyzer_nightly.sh` | Legacy nightly runner (superseded by above) |
| `scripts/governance_aggregator.py` | Phase 7: FPY/rework/SPC computation |
| `scripts/governance_weekly_report.py` | Weekly governance markdown report |
| `scripts/lib/cqs_calculator.py` | CQS calculation engine + status normalization |
| `scripts/update_dispatch_cqs.py` | Standalone CQS update CLI |
| `scripts/receipt_processor_v4.sh` | MODEL footer + CQS update (section C3b) |
| `scripts/gather_intelligence.py` | Pattern offer/adoption tracking (PR-0) |
| `scripts/learning_loop.py` | Confidence adjustment, pending archival (PR-0) |
| `scripts/tag_intelligence.py` | Pairwise/triple subsets, recommendation manager (PR-3) |
| `scripts/build_t0_quality_digest.py` | 3-section quality digest with NDJSON (PR-4) |
| `scripts/userpromptsubmit_worker_intelligence_inject.sh` | Worker intelligence injection (PR-2) |
| `schemas/quality_intelligence.sql` | Schema v8.1.0 with governance tables |
| `scripts/quality_db_init.py` | Migrations (CQS columns, governance tables) |
| `bin/vnx` | `suggest` and `analyze-sessions` subcommands |
| `tests/test_conversation_analyzer.py` | 44 tests |
| `tests/test_learning_feature.py` | Learning pipeline test suite (PR-4) |
