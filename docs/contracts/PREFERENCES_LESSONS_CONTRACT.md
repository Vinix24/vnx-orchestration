# Preferences And Lessons Surface Contract

**Feature**: Feature 22 — Preferences And Lessons Surface Generalization
**Contract-ID**: preferences-lessons-v1
**Status**: Canonical
**Last Updated**: 2026-04-03

---

## 1. Purpose

This contract defines how VNX stores, scopes, and surfaces preferences and lessons
across governance profiles so learning-loop outputs become durable, queryable inputs
without cross-profile contamination or unauthorized policy mutation.

The contract exists so that:

- Preferences and lessons are first-class entities with provenance and scoping
- Cross-profile contamination is structurally prevented
- Authority boundaries are unambiguous (no automatic instruction edits)
- Stale guidance retires rather than silently persisting

**Relationship to existing contracts**:
- `GOVERNANCE_FEEDBACK_CONTRACT.md` defines signal enrichment and recurrence detection. Preferences and lessons are downstream consumers of those signals.
- `AGENT_OS_LIFT_IN_CONTRACT.md` defines capability profiles. Preferences are scoped by profile.
- `BUSINESS_LIGHT_GOVERNANCE_CONTRACT.md` and `REGULATED_STRICT_GOVERNANCE_CONTRACT.md` define profile-specific governance. Preferences respect their authority boundaries.

---

## 2. Entity Model

### 2.1 Preference

A **preference** is an explicit operator or system statement about how work should be done within a specific scope.

```json
{
  "preference_id": "pref-<uuid4>",
  "scope": {
    "profile": "coding_strict|business_light|regulated_strict",
    "domain": "coding|business|regulated",
    "folder_scope": "/path/or/null",
    "feature_id": "Feature 22|null",
    "pr_id": "PR-2|null"
  },
  "category": "style|process|tooling|quality|communication",
  "content": "Always run pytest with -q flag in this project",
  "source": "operator|retrospective|signal_enrichment",
  "source_id": "sig-xxx|rec-xxx|manual",
  "confidence": "high|medium|low",
  "created_at": "ISO8601",
  "created_by": "operator|T0",
  "last_validated_at": "ISO8601",
  "status": "active|retired|superseded",
  "retirement_reason": "null|stale|superseded_by_pref-yyy|operator_removed"
}
```

### 2.2 Lesson

A **lesson** is an evidence-backed observation about what worked or didn't work, derived from execution history.

```json
{
  "lesson_id": "les-<uuid4>",
  "scope": {
    "profile": "coding_strict|business_light|regulated_strict",
    "domain": "coding|business|regulated",
    "folder_scope": "/path/or/null",
    "feature_id": "Feature 22|null"
  },
  "category": "success_pattern|failure_pattern|efficiency|quality",
  "content": "Codex gate timeouts correlate with prompts > 2000 tokens",
  "evidence_ids": ["sig-xxx", "sig-yyy"],
  "recurrence_key": "TIMEOUT:headless_codex_cli:research_structured",
  "recurrence_count": 4,
  "source": "retrospective|signal_enrichment|operator",
  "confidence": "high|medium|low",
  "created_at": "ISO8601",
  "last_validated_at": "ISO8601",
  "status": "active|retired|superseded"
}
```

### 2.3 Key Differences

| Aspect | Preference | Lesson |
|--------|-----------|--------|
| Origin | Operator statement or approved recommendation | Evidence-derived observation |
| Evidence required | No (operator authority) | Yes (evidence_ids mandatory) |
| Recurrence link | No | Optional (from governance feedback signals) |
| Mutability | Content immutable; status changeable | Content immutable; status changeable |
| Action authority | Guidance only — no auto-mutation | Guidance only — no auto-mutation |

---

## 3. Scoping Rules

### 3.1 Scope Keys

Every preference and lesson is scoped by these keys (most specific wins):

| Key | Required | Values | Purpose |
|-----|----------|--------|---------|
| `profile` | Yes | `coding_strict`, `business_light`, `regulated_strict` | Governance profile boundary |
| `domain` | Yes | `coding`, `business`, `regulated` | Domain isolation |
| `folder_scope` | No | Absolute path or null | Folder-specific (business/regulated) |
| `feature_id` | No | Feature identifier or null | Feature-specific |
| `pr_id` | No | PR identifier or null | PR-specific (most narrow) |

### 3.2 Scope Resolution

When a dispatch requests applicable preferences/lessons, resolution follows:

1. Match `profile` exactly (required — no cross-profile)
2. Match `domain` exactly
3. Match `folder_scope` if set (null = applies to all folders in domain)
4. Match `feature_id` if set (null = applies to all features in domain)
5. Match `pr_id` if set (null = applies to all PRs in feature)

More specific scopes override less specific ones for the same `category`.

### 3.3 Cross-Profile Isolation

| Rule | Description |
|------|-------------|
| **PL-1** | A preference scoped to `coding_strict` is invisible to `business_light` and `regulated_strict` dispatches |
| **PL-2** | A lesson from `business_light` execution cannot appear in `coding_strict` context injection |
| **PL-3** | Cross-profile preference creation is forbidden — `created_by` must have authority for the target profile |
| **PL-4** | The preferences/lessons store is shared infrastructure, but all queries filter by profile |
| **PL-5** | A `global` scope (all profiles) is NOT supported. Every entry must have an explicit profile. |

---

## 4. Authority Boundaries

### 4.1 Who May Create

| Source | May Create Preferences | May Create Lessons |
|--------|----------------------|-------------------|
| Operator | Yes (any profile they manage) | Yes (with evidence) |
| T0 orchestrator | Yes (for coding_strict) | Yes (from retrospective) |
| Retrospective digest | No (proposes; operator approves) | Yes (auto-created from recurrence signals) |
| Signal enrichment | No | Yes (auto-created from 3+ recurrence) |
| Local model helper | No | No (proposes; never creates) |

### 4.2 Who May Retire

| Action | Authority |
|--------|-----------|
| Retire preference | Operator or T0 (with audit record) |
| Retire lesson | Operator, T0, or automatic retirement (Section 5) |
| Supersede preference | Operator creates new preference with `superseded_by` link |
| Supersede lesson | New lesson with stronger evidence auto-supersedes weaker |

### 4.3 Authority Invariants

- **PA-1**: No preference or lesson may modify dispatch prompts, CLAUDE.md, or skill instructions automatically. They are context inputs only — injected at P6 priority per context assembler.
- **PA-2**: No preference or lesson may override gate results, closure decisions, or approval records.
- **PA-3**: Lessons auto-created from signal enrichment must carry `source: "signal_enrichment"` and `confidence: "low"` until operator validates them.
- **PA-4**: Preferences require explicit `created_by` — no anonymous or automated creation.
- **PA-5**: Retirement is audited. Every status change records `retired_by`, `retirement_reason`, and `retired_at`.

---

## 5. Ingestion And Retirement Criteria

### 5.1 Ingestion

| Source | Ingestion Rule | Confidence |
|--------|---------------|------------|
| Operator manual entry | Direct creation | `high` |
| Retrospective recommendation (acknowledged) | Operator acknowledges -> creates preference | `high` |
| Signal enrichment (3+ recurrence) | Auto-create lesson | `low` (until validated) |
| Retrospective recommendation (pending) | NOT ingested — stays in recommendation surface | N/A |
| Local model suggestion | NOT ingested — stays in model output | N/A |

### 5.2 Retirement Criteria

A preference or lesson is retired when any of these conditions are met:

| Criterion | Check Frequency | Action |
|-----------|----------------|--------|
| **Age without validation** | Per-dispatch | Retire if `last_validated_at` > 90 days ago |
| **Superseded** | On creation | When a new entry with same scope + category is created, old one is superseded |
| **Evidence invalidated** | Per-feature | If all `evidence_ids` point to resolved/expired signals, retire the lesson |
| **Operator removed** | On demand | Operator explicitly retires with reason |
| **Profile deactivated** | On profile change | If domain is disabled, all its entries become inactive (not deleted) |

### 5.3 Retirement Audit

Every retirement produces a record:

```json
{
  "entity_id": "pref-xxx|les-xxx",
  "entity_type": "preference|lesson",
  "retired_by": "operator|system|T0",
  "retired_at": "ISO8601",
  "retirement_reason": "stale|superseded|evidence_invalidated|operator_removed|profile_deactivated",
  "superseded_by": "pref-yyy|null"
}
```

---

## 6. Context Injection

### 6.1 Integration With Context Assembler

Preferences and lessons are injected at **P6 priority** (between P5 dispatch-specific context and P7 reusable signals):

| Priority | Component | Source |
|----------|-----------|--------|
| P5 | Dispatch-specific | Current task instructions |
| **P6** | **Preferences + lessons** | **Scoped active entries for this profile/domain/scope** |
| P7 | Reusable signals | From governance feedback carry-forward |

### 6.2 Injection Rules

1. Only `status: "active"` entries are injected
2. Entries are filtered by scope (Section 3.2) before injection
3. Most-specific scope wins when entries conflict
4. Maximum 10 preferences + 10 lessons per dispatch (budget-limited)
5. Entries are injected as structured context, not raw text

### 6.3 Injection Format

```json
{
  "preferences": [
    {
      "preference_id": "pref-xxx",
      "category": "quality",
      "content": "Always run pytest with -q flag",
      "confidence": "high",
      "scope_summary": "coding_strict / all features"
    }
  ],
  "lessons": [
    {
      "lesson_id": "les-xxx",
      "category": "failure_pattern",
      "content": "Codex gate timeouts correlate with prompts > 2000 tokens",
      "confidence": "medium",
      "recurrence_count": 4
    }
  ]
}
```

---

## 7. Storage

### 7.1 Store Location

```
$VNX_STATE_DIR/preferences_lessons/
  preferences.ndjson          # Append-only preference records
  lessons.ndjson              # Append-only lesson records
  retirement_audit.ndjson     # Retirement audit trail
  index.json                  # Active entry index (rebuilt from NDJSON)
```

### 7.2 Index

The `index.json` is a derived projection from the NDJSON files. It contains only `active` entries grouped by profile, and is rebuilt on startup or after any mutation.

---

## 8. Testing Contract

### 8.1 Entity Model Tests

1. Preference requires non-empty content and valid profile
2. Lesson requires non-empty evidence_ids
3. Both entities are content-immutable after creation
4. Status transitions are valid (active->retired, active->superseded)

### 8.2 Scoping Tests

1. Cross-profile query returns empty (PL-1, PL-2)
2. Cross-profile creation rejected (PL-3)
3. Global scope (no profile) rejected (PL-5)
4. More specific scope overrides less specific
5. Null folder_scope matches all folders in domain

### 8.3 Authority Tests

1. Signal-enrichment lessons have confidence=low (PA-3)
2. Anonymous creation rejected (PA-4)
3. Retirement is audited (PA-5)
4. Preferences cannot modify prompts/instructions (PA-1)
5. Lessons cannot override gates (PA-2)

### 8.4 Retirement Tests

1. Age > 90 days without validation triggers retirement
2. Superseded entry gets `superseded` status
3. Evidence-invalidated lesson retired
4. Operator removal records reason
5. Retirement audit record created for every status change

### 8.5 Injection Tests

1. Only active entries injected
2. Scope filtering applied
3. Budget limit (10+10) enforced
4. Most-specific scope wins

---

## 9. Migration Path

### Phase 1: Contract Lock (This PR)
### Phase 2: Preference/Lesson Store (PR-1)
### Phase 3: Scoped Query And Injection (PR-2)
### Phase 4: Retirement And Dashboard (PR-3)
### Phase 5: Certification (PR-4)

---

## 10. Open Questions (Resolved)

| Question | Resolution |
|----------|-----------|
| Should there be a global scope (all profiles)? | No. PL-5 prohibits it. Every entry must have an explicit profile to prevent contamination. |
| Should lessons auto-upgrade confidence? | Not automatically. Operator validates -> confidence manually upgraded. |
| Should preferences expire? | Yes, via age-without-validation (90 days). This prevents stale guidance persisting indefinitely. |
| Should the store use a database? | No. NDJSON + index.json is consistent with existing state file patterns. Database deferred to future scale needs. |
| Priority P6 vs P7? | P6. Preferences/lessons are more specific than P7 carry-forward signals, so they get higher priority. |
