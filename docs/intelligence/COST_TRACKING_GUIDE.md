# VNX Cost Tracking Guide

**Last Updated**: 2026-02-15
**Status**: Active (Phase 2 Complete)
**Owner**: T-MANAGER

## Overview

The VNX cost tracking system aggregates token usage and estimates costs across all worker terminals (T1, T2, T3) by analyzing receipt data and session transcripts. This enables:

- **Budget Management**: Track AI usage costs across the project
- **Resource Optimization**: Identify high-cost operations and optimize workflows
- **Audit Trail**: Complete usage history correlated with git commits via receipts
- **Provider Agnostic**: Works with Claude Code, Gemini CLI, and Codex CLI

## Architecture

### Data Flow

```
Worker Report → Receipt Processor → Receipt (with session metadata)
                                            ↓
                                    cost_tracker.py
                                            ↓
                         ┌──────────────────┴──────────────────┐
                         ↓                                      ↓
                 Phase 2: Transcript Resolution        Fallback: Receipt Extraction
                 (session_id → transcript.jsonl)       (token fields in receipt payload)
                         ↓                                      ↓
                    Usage Data                             Usage Data
                         ↓                                      ↓
                         └──────────────────┬──────────────────┘
                                            ↓
                                    Cost Estimation
                                    (model pricing)
                                            ↓
                                    Metrics Output
                                (JSON + Human Report)
```

### Phase 2: Transcript Resolution

**Key Innovation**: Instead of storing usage data in receipts, `cost_tracker.py` reads session transcripts on-demand based on `session_id` embedded in receipts.

**Benefits**:
- Model-agnostic (works with Claude/Gemini/Codex)
- No receipt format changes required
- Access to complete session usage history
- Backwards compatible with old receipts

## Usage

### Basic Cost Report

```bash
# Generate cost report from default receipts file
python3 .claude/vnx-system/scripts/cost_tracker.py

# Specify custom receipts file
python3 .claude/vnx-system/scripts/cost_tracker.py \
    --receipts $VNX_STATE_DIR/t0_receipts.ndjson

# Save metrics to custom output file
python3 .claude/vnx-system/scripts/cost_tracker.py \
    --output /tmp/my_metrics.json
```

### Output Format

#### Human-Readable Report

```
VNX Cost Report
Generated: 2026-02-15T10:30:00Z
Receipts source: $VNX_STATE_DIR/t0_receipts.ndjson

Totals
- Task complete events analyzed: 42
- Estimated cost (USD): $0.012345
- Receipts with estimated cost: 40
- Receipts with unknown cost: 2
- Input tokens (known): 123456
- Output tokens (known): 45678
- Total tokens (known): 169134
- Receipts with unknown tokens: 2

Resolution Statistics (Phase 2 - Transcript Usage)
- Successfully resolved from transcripts: 38 (90.5%)
- No session_id in receipt: 2
- Transcript not found: 1
- No usage data in transcript: 0
- Resolution errors: 0
- Fallback to receipt extraction: 1

By Model
- claude-sonnet-4.6: events=35, cost=$0.010500, known_tokens=35, unknown_tokens=0
- claude-opus-4.6: events=5, cost=$0.001500, known_tokens=5, unknown_tokens=0
- unknown: events=2, cost=$0.000000, known_tokens=0, unknown_tokens=2

By Worker (terminal|provider)
- T1|claude_code: events=20, cost=$0.006000, unknown_cost=0
- T2|claude_code: events=15, cost=$0.004500, unknown_cost=1
- T3|claude_code: events=7, cost=$0.001845, unknown_cost=1

Limitations
- Cost estimates are approximate based on standard pricing
- Actual costs may vary with provider-specific pricing
```

#### JSON Metrics

```json
{
  "generated_at_utc": "2026-02-15T10:30:00Z",
  "source": {
    "receipts_path": "$VNX_STATE_DIR/t0_receipts.ndjson"
  },
  "totals": {
    "events_analyzed": 42,
    "estimated_cost_usd": 0.012345,
    "receipts_with_estimated_cost": 40,
    "receipts_with_unknown_cost": 2,
    "input_tokens": 123456,
    "output_tokens": 45678,
    "total_tokens": 169134,
    "receipts_with_unknown_tokens": 2,
    "resolution_stats": {
      "resolved": 38,
      "no_session_id": 2,
      "transcript_not_found": 1,
      "no_usage_data": 0,
      "error": 0,
      "fallback_to_receipt": 1
    }
  },
  "by_model": {
    "claude-sonnet-4.6": {
      "events": 35,
      "estimated_cost_usd": 0.010500,
      "receipts_with_known_tokens": 35,
      "receipts_with_unknown_tokens": 0
    }
  },
  "by_worker": {
    "T1|claude_code": {
      "events": 20,
      "estimated_cost_usd": 0.006000,
      "receipts_with_unknown_cost": 0
    }
  },
  "notes": [
    "Cost estimates are approximate based on standard pricing"
  ]
}
```

## Resolution Statistics Explained

### Resolution Status Values

| Status | Meaning | Action |
|--------|---------|--------|
| `resolved` | Successfully extracted usage from transcript | ✅ Optimal path |
| `no_session_id` | Receipt has no `session.session_id` field | Workers should include session_id in reports |
| `transcript_not_found` | Session transcript file doesn't exist | Check transcript retention, session may be old |
| `no_usage_data` | Transcript exists but has no usage metadata | Transcript corrupted or incomplete |
| `error` | Exception during transcript parsing | Check logs, may indicate format incompatibility |
| `fallback_to_receipt` | Used token fields already present in receipt | Old receipts or non-transcript-compatible flows |

### Improving Resolution Rate

**Target**: ≥90% resolution from transcripts

**Common Issues**:

1. **Low resolution rate (<50%)**
   - **Cause**: Workers not including `session_id` in reports
   - **Fix Option A** (Recommended): Update worker report templates to include `**Session**: <id>`
   - **Fix Option B**: Set `CLAUDE_SESSION_ID` environment variable at session start
   - **Fix Option C** (Future): Implement VNX session manager to write `current_session` file

2. **High `transcript_not_found` rate**
   - **Cause**: Transcripts deleted or session IDs incorrect
   - **Fix**: Check transcript retention policy, verify session ID extraction in `report_parser.py`

3. **`no_usage_data` errors**
   - **Cause**: Transcript format incompatibility or corruption
   - **Fix**: Verify transcript JSONL format matches expected schema (see Transcript Format section)

## Transcript Format

### Claude Code Transcript

```jsonl
{"role":"user","content":"Hello"}
{"role":"assistant","content":"Hi!","usage":{"input_tokens":10,"output_tokens":5}}
{"role":"user","content":"What time is it?"}
{"role":"assistant","content":"I don't have access to the current time.","usage":{"input_tokens":15,"output_tokens":12}}
```

**Key Fields**: `usage.input_tokens`, `usage.output_tokens`

### Gemini CLI Transcript

```jsonl
{"role":"user","parts":[{"text":"Hello"}]}
{"role":"model","parts":[{"text":"Hi!"}],"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}}
```

**Key Fields**: `usageMetadata.promptTokenCount`, `usageMetadata.candidatesTokenCount`

### Codex CLI Transcript

```jsonl
{"role":"user","content":"Hello"}
{"role":"assistant","content":"Hi!","usage":{"prompt_tokens":10,"completion_tokens":5}}
```

**Key Fields**: `usage.prompt_tokens`, `usage.completion_tokens`

## Model Pricing

Default pricing (USD per 1M tokens) mirrors `MODEL_PRICING` in `cost_tracker.py`:

| Model | Input | Output |
|-------|-------|--------|
| claude-opus-4.6 | $15.00 | $75.00 |
| claude-sonnet-4.6 | $3.00 | $15.00 |
| claude-opus-4.5 (historical) | $15.00 | $75.00 |
| claude-sonnet-4.5 (historical) | $3.00 | $15.00 |
| gpt-5.2-codex | $1.75 | $14.00 |
| gemini-pro | $0.50 | $1.50 |
| gemini-flash | $0.10 | $0.30 |

**Note**: Prices are estimates and may vary. Update `MODEL_PRICING` in `cost_tracker.py` for accurate costs.

## Session Resolution Strategy

**Design Principle**: DETERMINISTIC ONLY - No heuristics to ensure correct attribution in parallel terminal scenarios.

### Resolution Priority Chain

**Priority Order** (first available wins):
1. **Worker Report** → Explicit `**Session**: <id>` in markdown (RECOMMENDED)
2. **Environment Variable** → `$CLAUDE_SESSION_ID`, `$GEMINI_SESSION_ID`, `$CODEX_SESSION_ID`
3. **"Current" Session Files** → `~/.codex/sessions/current`, `~/.gemini/sessions/current`
4. **Fallback** → "unknown"

### Recommended Production Setup

**Option 1: Explicit in Reports** (Best)
Workers include session_id in report metadata:

```markdown
# Task Report

**Session**: abc-def-123-456
**Terminal**: T1
**Status**: success
...
```

**Option 2: Environment Variables** (Good)
Set at terminal/session initialization:

```bash
export CLAUDE_SESSION_ID="abc-def-123-456"
export GEMINI_SESSION_ID="xyz-789-012-345"
```

**Option 3: VNX Session Manager** (Future)
If you need a deterministic fallback without provider-managed "current" files, implement a VNX-managed per-terminal current file (avoid a single global file to prevent parallel mis-attribution):

```bash
# On session start
echo "abc-def-123-456" > $VNX_STATE_DIR/current_session_T1

# Session resolver can read it (if you implement this hook in your terminal wrapper/session resolver)
```

### Why No Auto-Detect?

**Problem**: Heuristic methods (e.g., "latest transcript") can fail in parallel terminal scenarios:
- Terminal T1 finishes task at 10:00:00
- Terminal T2 finishes task at 10:00:01
- T1's receipt processor runs at 10:00:02
- Finds T2's transcript as "latest" → **WRONG ATTRIBUTION!**

**Solution**: Use only deterministic methods that can't be confused by parallel execution.

## Integration with Receipt System

### Receipt Schema (Phase 1B)

Receipts include session metadata for usage resolution:

```json
{
  "event_type": "task_complete",
  "task_id": "A3-2_receipt_upgrade",
  "status": "success",
  "session": {
    "session_id": "abc-def-123-456",
    "terminal": "T1",
    "model": "claude-sonnet-4.6",
    "provider": "claude_code",
    "captured_at": "2026-02-15T10:30:00Z"
  }
}
```

### Worker Report Template

Workers should include session metadata in markdown reports:

```markdown
# Task A3-2: Receipt Upgrade

**Session**: abc-def-123-456
**Status**: success
**Terminal**: T1

## Summary
Implemented git provenance and session metadata capture...
```

The `report_parser.py` extracts `session_id` from these patterns:
- `**Session**: <session_id>` (markdown bold)
- `Session: <session_id>` (plain text)

## Troubleshooting

### Cost Tracker Errors

**Error**: `FileNotFoundError: receipts file not found`
- **Cause**: Receipt file doesn't exist or wrong path
- **Fix**: Check `$VNX_STATE_DIR/t0_receipts.ndjson` exists, or specify `--receipts` path

**Error**: `JSONDecodeError: Expecting value`
- **Cause**: Corrupted NDJSON in receipts file
- **Fix**: Validate receipts file format, check for malformed JSON lines

**Error**: `KeyError: 'session'`
- **Cause**: Receipt missing session metadata (old format)
- **Fix**: Expected for old receipts, will use fallback extraction (normal)

### Low Resolution Rate

**Symptom**: Resolution rate <50%, high `no_session_id` count

**Diagnosis**:
```bash
# Check if receipts have session metadata
jq -c 'select(.session) | {task_id, session_id: .session.session_id}' \
    $VNX_STATE_DIR/t0_receipts.ndjson | head -5
```

**Fix**: Update workers to include session_id in reports (see Worker Report Template)

**Symptom**: High `transcript_not_found` rate

**Diagnosis**:
```bash
# Check transcript directory
ls -lh ~/.claude/sessions/*.jsonl | wc -l

# Check specific session
cat ~/.claude/sessions/abc-def-123-456.jsonl | head -5
```

**Fix**: Verify transcripts exist and session_id extraction is correct

## Future Enhancements (Phases 3-4)

**Phase 3**: Context Monitoring
- Real-time session context size tracking
- Proactive alerts when approaching limits
- Auto-refresh triggers based on thresholds

**Phase 4**: Auto Context Refresh
- Mid-session summarization
- Clean context restoration
- Seamless session continuation

See `RECEIPT_UPGRADE_PLAN.md` for complete roadmap.

## Related Documentation

- **RECEIPT_UPGRADE_PLAN.md** - Complete implementation roadmap
- **11_RECEIPT_FORMAT.md** - Receipt schema specification
- **receipt_processor_v4.sh** - Receipt processing implementation
- **report_parser.py** - Report parsing and session extraction

---

*For questions or issues, consult T-MANAGER terminal.*
