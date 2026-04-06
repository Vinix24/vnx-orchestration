# Operator Feedback Architecture

## Overview

VNX operator feedback flows through 4 channels, each serving a distinct latency and audience need.

## Feedback Channels

### Channel 1: Real-Time Dashboard
- **Latency**: <5s (WebSocket/polling)
- **Source**: `dashboard_status.json` written by intelligence daemon
- **Content**: Terminal status, active dispatches, queue depth, health indicators
- **Consumer**: Operator during active development sessions

### Channel 2: Governance Digest (Automated)
- **Latency**: 5-min cadence
- **Source**: `governance_digest.json` written by `GovernanceDigestRunner`
- **Content**: Gate pass/fail signals, queue anomalies, defect family recurrence, advisory recommendations
- **Consumer**: T0 orchestrator for dispatch planning decisions

### Channel 3: Retrospective Digest (Daily)
- **Latency**: Daily at 18:00 (configurable via `daily_hygiene_hour`)
- **Source**: Learning loop output from `intelligence_daemon.py` daily hygiene
- **Content**: Pattern recurrence trends, quality intelligence refresh, tag auto-refresh, candidate guardrails
- **Consumer**: Operator for weekly planning; T0 for long-term pattern adjustment

### Channel 4: Session Analytics (Nightly)
- **Latency**: Nightly batch
- **Source**: `conversation_analyzer.py` parsing JSONL session logs
- **Content**: Token usage, tool call distribution, session cost, model utilization
- **Consumer**: Operator for cost monitoring and capacity planning
- **Limitation**: Metadata only — does not analyze output quality or correctness

## Signal Flow

```
Terminal Output (T1/T2/T3)
    |
    v
+-------------------+     +------------------------+
| Unified Reports   |---->| Governance Signal       |
| (markdown)        |     | Extractor (5-min)       |
+-------------------+     +------------------------+
    |                              |
    v                              v
+-------------------+     +------------------------+
| Receipt Processor |     | Retrospective Digest    |
| (NDJSON)          |---->| Builder (daily)         |
+-------------------+     +------------------------+
    |                              |
    v                              v
+-------------------+     +------------------------+
| T0 Receipt Review |     | Optional LLM Hook       |
| (manual gate)     |     | (candidate guardrails)  |
+-------------------+     +------------------------+
                                   |
                                   v
                          +------------------------+
                          | Governance Digest JSON  |
                          | (T0 + Dashboard)        |
                          +------------------------+
```

## Missing Channel: Terminal Quality Analysis

Currently absent — the gap between "what did the worker produce?" (Channel 2/3) and "how well did the worker work?" (not captured). See `TERMINAL_QUALITY_ANALYSIS_ARCHITECTURE.md` for the proposed design.

## Email Digest Activation

The intelligence daemon supports email digest delivery via environment variables:

```bash
# Enable email digest (requires SMTP configuration)
export VNX_EMAIL_DIGEST_ENABLED=1
export VNX_EMAIL_SMTP_HOST=smtp.example.com
export VNX_EMAIL_SMTP_PORT=587
export VNX_EMAIL_RECIPIENT=operator@example.com

# Digest schedule (default: daily at 18:00 with hygiene cycle)
export VNX_DIGEST_EMAIL_HOUR=18
```

**Current status**: Email delivery is not yet implemented. The digest infrastructure (`governance_digest.json`) is the structured data source that an email renderer would consume. Implementation would involve:
1. A `DigestEmailRenderer` that reads `governance_digest.json`
2. Template-based HTML email with recurring pattern highlights
3. Triggered from `IntelligenceDaemon.daily_hygiene()` after digest generation
4. Opt-in only — never auto-enabled

## Configuration Reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `VNX_DIGEST_INTERVAL` | 300 | Governance digest cycle (seconds) |
| `VNX_DAILY_INTEL_REFRESH` | 1 | Enable daily quality intelligence refresh |
| `VNX_INTELLIGENCE_DASHBOARD_WRITE` | 0 | Enable dashboard status file writes |
| `VNX_ANALYZER_LLM` | auto | Conversation analyzer LLM strategy |
| `VNX_OLLAMA_MODEL` | qwen2.5-coder:14b | Local model for deep analysis |
