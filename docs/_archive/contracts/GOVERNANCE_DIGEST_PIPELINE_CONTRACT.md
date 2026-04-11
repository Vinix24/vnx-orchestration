# Governance Digest Pipeline Contract

**Feature**: Feature 25 — Governance Digest Pipeline And Dashboard Surface
**Contract-ID**: governance-digest-pipeline-v1
**Status**: Canonical
**Last Updated**: 2026-04-03

---

## 1. Purpose

This contract defines how the governance feedback loop's signals, recurrences,
and recommendations are materialized into a digest file and surfaced on the
operator dashboard as S7 (governance digest panel).

---

## 2. Digest Generation

### 2.1 Schedule And Trigger

| Aspect | Value |
|--------|-------|
| **Generator** | `intelligence_daemon.py` (existing daemon, extended) |
| **Interval** | Every 5 minutes |
| **Trigger** | Timer-based within daemon loop |
| **On-demand** | `vnx digest --refresh` CLI command for immediate regeneration |
| **Output** | `$VNX_STATE_DIR/governance_digest.json` |

### 2.2 Generation Pipeline

```
Signal store (feedback/signals.ndjson)
  → Recurrence index (feedback/recurrence_index.json)
  → Retrospective digest (per-feature, from retrospective_digest.py)
  → Recommendation surface (governance_recommendations.json)
  → governance_digest.json (materialized for dashboard)
```

The generator reads from existing sources — it does not recompute signals.
It aggregates and formats for dashboard consumption.

---

## 3. Digest JSON Shape

### 3.1 File Location

```
$VNX_STATE_DIR/governance_digest.json
```

### 3.2 Schema

```json
{
  "generated_at": "ISO8601",
  "generation_number": 42,
  "freshness": "fresh|aging|stale",
  "summary": {
    "total_signals": 120,
    "recurring_patterns": 8,
    "active_recommendations": 3,
    "dismissed_recommendations": 1,
    "signal_classes": {
      "RUNTIME_FAILURE": 35,
      "GATE_FAILURE": 20,
      "DELIVERY_FAILURE": 15,
      "ORCH_FAILURE": 10,
      "CHAIN_FAILURE": 5,
      "OI_RECURRENCE": 25,
      "QUALITY_REGRESSION": 10
    }
  },
  "recurrences": [
    {
      "recurrence_key": "TIMEOUT:headless_codex_cli:research_structured",
      "signal_class": "RUNTIME_FAILURE",
      "count": 4,
      "severity": "warn",
      "first_seen": "ISO8601",
      "last_seen": "ISO8601",
      "trend": "stable|increasing|decreasing",
      "impacted_features": ["Feature 18", "Feature 19"]
    }
  ],
  "recommendations": [
    {
      "id": "rec-<uuid4>",
      "priority": "P0|P1|P2",
      "signal_class": "RUNTIME_FAILURE",
      "recurrence_key": "TIMEOUT:headless_codex_cli:research_structured",
      "recurrence_count": 4,
      "recommendation": "Increase VNX_CODEX_GATE_TIMEOUT from 600s to 900s",
      "status": "pending|acknowledged|applied|dismissed",
      "created_at": "ISO8601"
    }
  ],
  "recent_signals": [
    {
      "signal_id": "sig-xxx",
      "signal_class": "GATE_FAILURE",
      "content": "Codex gate timed out on PR-2",
      "severity": "blocker",
      "timestamp": "ISO8601"
    }
  ]
}
```

### 3.3 Field Descriptions

| Section | Purpose | Max Items |
|---------|---------|-----------|
| `summary` | Aggregate counts for quick overview | N/A |
| `recurrences` | Recurring patterns sorted by count desc | 20 |
| `recommendations` | Active and recent recommendations | 10 |
| `recent_signals` | Most recent signals for timeline | 25 |

### 3.4 Freshness

| Age | Freshness |
|-----|-----------|
| < 5 minutes | `fresh` |
| 5-15 minutes | `aging` |
| > 15 minutes | `stale` |

---

## 4. Dashboard Surface S7

### 4.1 Layout

The governance digest panel (S7) displays:

| Component | Content | Interaction |
|-----------|---------|-------------|
| **Summary bar** | Total signals, recurring patterns, active recommendations | Read-only |
| **Recurrence table** | Rows of recurring patterns with key, count, severity, trend | Sortable by count/severity |
| **Recommendation cards** | One card per recommendation with priority badge, action buttons | Acknowledge/Dismiss buttons |
| **Signal timeline** | Recent signals as chronological list | Scrollable |

### 4.2 Recurrence Table Columns

| Column | Source | Sortable |
|--------|--------|----------|
| Pattern | `recurrence_key` (formatted) | No |
| Class | `signal_class` (badge) | Yes |
| Count | `count` | Yes (default) |
| Severity | `severity` (color-coded) | Yes |
| Trend | `trend` (arrow icon) | No |
| Last Seen | `last_seen` (relative time) | Yes |

### 4.3 Recommendation Card

```
┌─────────────────────────────────────────┐
│ [P0] Increase Codex gate timeout        │
│                                         │
│ Pattern: TIMEOUT:headless_codex_cli     │
│ Seen: 4 times across 2 features        │
│                                         │
│ [Acknowledge]  [Dismiss]  [Details]     │
└─────────────────────────────────────────┘
```

### 4.4 Recommendation Actions

| Action | API | Effect |
|--------|-----|--------|
| **Acknowledge** | `POST /api/operator/recommendation/ack` | Sets `status: "acknowledged"` |
| **Dismiss** | `POST /api/operator/recommendation/dismiss` | Sets `status: "dismissed"` with reason |
| **Details** | Client-side | Expands card to show evidence IDs and recurrence history |

### 4.5 Advisory-Only Enforcement

- Recommendations are rendered as suggestions, not actions
- No "Apply" button — operator must manually implement the recommendation
- Dismissed recommendations record a `dismissed_reason` (required field)
- The digest panel never modifies dispatch state, gate config, or runtime settings

---

## 5. Signal-To-Recommendation Flow

```
Signal (from governance_signal_extractor)
  → Recurrence detection (detect_recurrences, threshold >= 3)
  → Recommendation generation (generate_recommendations)
  → governance_recommendations.json (pending status)
  → governance_digest.json (materialized for dashboard)
  → S7 panel (rendered for operator)
  → Operator acknowledges/dismisses
  → Status update in governance_recommendations.json
```

**Invariants**:
- **D-1**: Recommendations are generated only from recurrence count >= 3 (persistent threshold)
- **D-2**: The digest file is regenerated every 5 minutes — not on every signal
- **D-3**: The digest file is a projection — deleting it has no data loss (regenerated from sources)
- **D-4**: Recommendation status changes are written to `governance_recommendations.json`, not to the digest
- **D-5**: The dashboard reads from `governance_digest.json` only — it never reads signal/recurrence stores directly

---

## 6. API Endpoints

### 6.1 GET /api/operator/digest

Returns the current governance digest.

**Response**: Content of `governance_digest.json` wrapped in `FreshnessEnvelope`.

### 6.2 POST /api/operator/recommendation/ack

Acknowledge a recommendation.

**Request**: `{ "recommendation_id": "rec-xxx" }`
**Response**: `{ "success": true, "status": "acknowledged" }`

### 6.3 POST /api/operator/recommendation/dismiss

Dismiss a recommendation.

**Request**: `{ "recommendation_id": "rec-xxx", "reason": "Not applicable to current work" }`
**Response**: `{ "success": true, "status": "dismissed" }`

---

## 7. Testing Contract

1. Digest JSON validates against schema (all required fields present)
2. Recurrences sorted by count descending
3. Recommendations capped at 10
4. Recent signals capped at 25
5. Freshness derived correctly from `generated_at`
6. Acknowledge sets status to "acknowledged"
7. Dismiss requires non-empty reason
8. Dashboard reads only from digest file (D-5)
9. Digest is regenerable from sources (D-3)

---

## 8. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should the digest be generated on every signal? | No. Every 5 minutes is sufficient. Per-signal would create unnecessary I/O. |
| Should the dashboard read signal stores directly? | No. D-5 — dashboard reads only the materialized digest. This prevents coupling to internal store formats. |
| Should "Apply" be a recommendation action? | No. Advisory-only enforcement. Operator must implement manually. |
