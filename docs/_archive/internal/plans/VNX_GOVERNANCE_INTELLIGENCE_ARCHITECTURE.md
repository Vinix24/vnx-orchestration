# VNX Governance & Intelligence Layer Architecture

**Author**: T3 (Track C) — Architecture Research  
**Dispatch-ID**: 20260408-010001-f36-governance-intelligence-arch-C  
**Date**: 2026-04-08  
**Status**: Planning Document — No Code Implementation

---

## Table of Contents

1. [Current State Assessment](#1-current-state-assessment)
2. [Stream-Based Report Assembly](#2-stream-based-report-assembly)
3. [Tag Schema & Data Model](#3-tag-schema--data-model)
4. [Context Injection Architecture](#4-context-injection-architecture)
5. [Quality Check Pipeline](#5-quality-check-pipeline)
6. [Self-Learning Loop Design](#6-self-learning-loop-design)
7. [Benchmark Plan](#7-benchmark-plan)
8. [Implementation Roadmap](#8-implementation-roadmap)
9. [Framework Comparison Table](#9-framework-comparison-table)

---

## 1. Current State Assessment

### 1.1 What We Have

The VNX intelligence system is a closed-loop learning and governance system with five main modules:

| Module | File | Status | Purpose |
|--------|------|--------|---------|
| Intelligence Selector | `scripts/lib/intelligence_selector.py` | Working | Bounded injection of 0-3 items at dispatch_create/resume |
| Intelligence Persist | `scripts/lib/intelligence_persist.py` | Working | Upserts patterns/antipatterns/metadata from governance signals |
| Intelligence Backfill | `scripts/intelligence_backfill.py` | Working | One-time backfill from historical receipts |
| Recommendation Tracker | `scripts/lib/recommendation_tracker.py` | Working | Full lifecycle: propose → accept/reject → measure outcomes |
| Recommendation Metrics | `scripts/lib/recommendation_metrics.py` | Working | FPY, rework rate, timeout rate, override rate computation |
| Governance Signal Extractor | `scripts/lib/governance_signal_extractor.py` | Partial | Signal classes defined, `collect_governance_signals()` not connected |
| Intelligence Daemon | `scripts/intelligence_daemon.py` | Partial | `GovernanceDigestRunner` operational, awaiting signal enrichment |

**Database**: `quality_intelligence.db` with well-designed schema:
- `success_patterns` — proven approaches with confidence scoring
- `antipatterns` — failure patterns with severity classification
- `dispatch_metadata` — per-dispatch analytics (terminal, track, role, skill, gate, outcome)
- `prevention_rules` — generated from tag combination mining
- `tag_combinations` — 2-3 tag pattern tracking with outcome correlation
- `code_snippets` — FTS5 full-text search virtual table
- `pattern_usage` — feedback loop tracking (used/ignored/success/failure counts)

**Documentation**: 9 files in `docs/intelligence/`, feature plan for F18 (learning-loop signal enrichment) with PRs 0-4 planned.

### 1.2 What Works

1. **Intelligence injection at dispatch time** — `IntelligenceSelector.select()` queries three candidate classes (proven_pattern ≥0.6, failure_prevention ≥0.5, recent_comparable ≥0.4), enforces payload limits (max 3 items, max 2000 chars), and records injection/suppression decisions.

2. **Receipt-driven persistence** — `intelligence_persist.py` upserts patterns from governance signals with idempotent logic. Confidence grows with evidence: `min(1.0, 0.5 + (usage_count * 0.05))`.

3. **Recommendation lifecycle** — Full state machine (proposed → accepted → rejected/expired/superseded) with 7-day measurement windows and baseline/outcome delta computation.

4. **Quality advisory pipeline** — 5 deterministic checks (file size, function size, linting via ruff/shellcheck, dead code via vulture, test coverage hygiene) producing risk scores and T0 recommendations.

5. **Multi-gate review system** — Three-gate stack (gemini_review, codex_gate, claude_github_optional) with subprocess stall detection, timeout management, and structured receipt schema.

### 1.3 What's Broken or Incomplete

1. **Learning loop not closing** — Patterns persist but don't automatically improve confidence based on outcome measurements. Recommendations are measured but not fed back to pattern confidence scores.

2. **Signal extraction not connected** — `governance_signal_extractor.py` defines signal types and correlation context but `collect_governance_signals()` is not wired into the daemon's main loop.

3. **Prevention rules not auto-generated** — `tag_combinations` table tracks patterns but no automatic rule generation from recurring failure patterns.

4. **No stop hooks** — Only a `sessionstart.sh` hook exists. No `sessionend` or `post_stop` hook to run deterministic checks when a worker session ends. This is the critical gap this architecture must fill.

5. **Reports are manual** — Workers write full markdown reports (~500-2000 tokens each). No auto-assembly from stream events. Report quality varies and metadata extraction requires regex parsing of multiple formats (YAML, tables, plain text).

### 1.4 Key File References

```
scripts/lib/intelligence_selector.py    — Injection logic + scope tag matching
scripts/lib/intelligence_persist.py     — Signal → DB upsert bridge
scripts/lib/governance_signal_extractor.py — Signal normalization (F18)
scripts/lib/recommendation_tracker.py   — Recommendation lifecycle
scripts/lib/recommendation_metrics.py   — Dispatch-level metric computation
scripts/lib/quality_advisory.py         — 5 deterministic checks + risk scoring
scripts/lib/subprocess_adapter.py       — Stream event capture from Claude CLI
scripts/lib/event_store.py              — NDJSON event persistence per terminal
scripts/lib/headless_event_stream.py    — Structured session timeline
scripts/lib/headless_review_receipt.py  — Canonical receipt schema + validation
scripts/lib/dispatch_broker.py          — Dispatch lifecycle management
scripts/lib/receipt_provenance.py       — Provenance enrichment + gap detection
scripts/review_gate_manager.py          — Gate orchestration (request/execute/parse/report)
scripts/codex_final_gate.py             — Codex enforcement + prompt rendering
scripts/pre_merge_gate.py               — 6 pre-merge checks
scripts/verify_claims.py                — Lightweight contract verification
scripts/append_receipt.py               — Receipt appending with git provenance
scripts/report_parser.py                — Markdown report extraction
schemas/quality_intelligence.sql        — Intelligence DB schema
schemas/runtime_coordination.sql        — Coordination DB schema (v9)
```

---

## 2. Stream-Based Report Assembly

### 2.1 Design Rationale

Workers currently write full markdown reports to `$VNX_DATA_DIR/unified_reports/`. This creates:
- Variable quality and completeness
- Wasted worker tokens on formatting/metadata
- Report parsing fragility (3 different metadata formats)
- No guarantee the report matches what actually happened

**Target**: Workers provide only a short exit summary (~50 tokens). Everything else is auto-assembled from:
- Stream archive (tool_use events → files touched, test output)
- Git diff (actual files changed, line delta)
- Deterministic checks (syntax validation, test counts)
- Haiku classification (semantic summary, quality score)

### 2.2 Stop Hook Pipeline

The stop hook fires when a worker session ends (provider-agnostic). It runs a deterministic-first pipeline:

```
Worker Session Ends
    │
    ▼
[Stop Hook: scripts/hooks/worker_stop.py]
    │
    ├─ Phase 1: Deterministic Extraction (0 LLM cost)
    │   ├─ git diff HEAD~1..HEAD → files_changed, insertions, deletions
    │   ├─ git log -1 --format='%H %s' → commit_hash, commit_message
    │   ├─ Parse dispatch file → dispatch_id, pr_id, track, gate, tags
    │   ├─ Scan stream archive → tool_use count, text block count, error count
    │   ├─ Extract test output → pytest results (passed/failed/errors/skipped)
    │   ├─ py_compile on changed .py files → syntax_valid bool
    │   ├─ bash -n on changed .sh files → syntax_valid bool
    │   ├─ Duration calculation → session_duration_seconds
    │   └─ Worker exit summary → last text block from stream (≤50 tokens)
    │
    ├─ Phase 2: Deterministic Quality Checks (0 LLM cost)
    │   ├─ File exists for all claimed deliverables
    │   ├─ Test count matches claimed count
    │   ├─ No merge conflict markers in changed files
    │   ├─ No secrets in changed files (basic pattern match)
    │   ├─ PR size check (line delta vs thresholds)
    │   └─ Import check on new Python files
    │
    ├─ Phase 3: Haiku Classification (low LLM cost, ~200 tokens)
    │   ├─ Input: exit summary + files_changed + test_results + check_results
    │   ├─ Output: content_type, quality_score (1-5), complexity (low/med/high)
    │   ├─ Semantic: does exit summary match git diff? (consistency_score)
    │   └─ Only runs if Phase 1+2 produce no blocking failures
    │
    └─ Phase 4: Report Assembly (0 LLM cost)
        ├─ Merge all phases into structured report
        ├─ Write to $VNX_DATA_DIR/unified_reports/{timestamp}-{track}-auto.md
        ├─ Write structured JSON to $VNX_DATA_DIR/auto_reports/{dispatch_id}.json
        └─ Append receipt to t0_receipts.ndjson
```

### 2.3 Module Design

```python
# scripts/lib/auto_report_assembler.py

@dataclass
class DeterministicExtraction:
    dispatch_id: str
    terminal: str
    track: str
    gate: str
    dispatch_tags: Dict[str, str]       # From dispatch file
    files_changed: List[str]            # From git diff
    insertions: int
    deletions: int
    commit_hash: Optional[str]
    commit_message: Optional[str]
    test_results: Optional[TestResults]  # From pytest output parsing
    syntax_checks: List[SyntaxCheck]    # py_compile / bash -n results
    tool_use_count: int                 # From stream archive
    error_count: int                    # From stream archive
    session_duration_seconds: int
    exit_summary: str                   # Last text block, ≤50 tokens
    worker_model: Optional[str]         # From stream init event

@dataclass
class TestResults:
    passed: int
    failed: int
    errors: int
    skipped: int
    duration_seconds: float
    raw_output: str                     # Truncated to 500 chars

@dataclass
class SyntaxCheck:
    file_path: str
    language: str                       # python | shell
    valid: bool
    error_message: Optional[str]

@dataclass
class QualityCheckResult:
    check_name: str                     # file_exists | test_count | conflicts | secrets | pr_size | imports
    passed: bool
    severity: str                       # info | warning | blocking
    detail: str

@dataclass
class HaikuClassification:
    content_type: str                   # implementation | test | refactor | docs | config | review
    quality_score: int                  # 1-5
    complexity: str                     # low | medium | high
    consistency_score: float            # 0.0-1.0 (exit summary vs git diff alignment)
    summary: str                        # ≤100 token semantic summary

@dataclass
class AssembledReport:
    extraction: DeterministicExtraction
    quality_checks: List[QualityCheckResult]
    classification: Optional[HaikuClassification]  # None if Phase 2 had blockers
    assembled_at: str                   # ISO 8601
    report_version: str                 # "2.0"

class AutoReportAssembler:
    def __init__(self, state_dir: Path, project_root: Path):
        self._state_dir = state_dir
        self._project_root = project_root

    def extract(self, dispatch_id: str, stream_archive_path: Path) -> DeterministicExtraction:
        """Phase 1: Deterministic extraction from git + stream + dispatch file."""
        ...

    def check(self, extraction: DeterministicExtraction) -> List[QualityCheckResult]:
        """Phase 2: Deterministic quality checks."""
        ...

    def classify(self, extraction: DeterministicExtraction,
                 checks: List[QualityCheckResult]) -> Optional[HaikuClassification]:
        """Phase 3: Haiku classification. Returns None if blocking checks found."""
        ...

    def assemble(self, dispatch_id: str, stream_archive_path: Path) -> AssembledReport:
        """Full pipeline: extract → check → classify → assemble."""
        extraction = self.extract(dispatch_id, stream_archive_path)
        checks = self.check(extraction)
        blocking = [c for c in checks if c.severity == "blocking" and not c.passed]
        classification = self.classify(extraction, checks) if not blocking else None
        return AssembledReport(
            extraction=extraction,
            quality_checks=checks,
            classification=classification,
            assembled_at=datetime.utcnow().isoformat() + "Z",
            report_version="2.0",
        )

    def write_report(self, report: AssembledReport) -> Path:
        """Write markdown + JSON versions of the assembled report."""
        ...

    def append_receipt(self, report: AssembledReport) -> None:
        """Append structured receipt to t0_receipts.ndjson."""
        ...
```

### 2.4 Stream Event Extraction

The `SubprocessAdapter` already captures and normalizes stream events via `_normalize_cli_event()`. The `EventStore` persists these as NDJSON at `.vnx-data/events/{terminal}.ndjson`. The auto-report assembler reads from the event archive:

| Event Type | Extraction |
|-----------|------------|
| `init` | session_id, model |
| `tool_use` | tool name → count file operations, test runs, git commands |
| `tool_result` | test output (grep for pytest markers), error messages |
| `text` | Last text block → exit summary (truncated to 50 tokens) |
| `result` | Final result text, subtype (end_turn, tool_use) |

### 2.5 Handling Edge Cases

| Scenario | Handling |
|----------|---------|
| Truncated stream (crash) | Phase 1 uses git diff as ground truth; exit_summary = "SESSION_CRASHED" |
| No git changes | Valid for review/analysis tasks; files_changed=[], commit_hash=None |
| Worker never started | No stream archive; report status="no_execution", all checks skip |
| Missing exit summary | Use commit message as fallback; if no commit, use "NO_EXIT_SUMMARY" |
| Test timeout | TestResults.raw_output captures timeout message; failed=0, errors=1 |

---

## 3. Tag Schema & Data Model

### 3.1 Tag Flow: Dispatch → Receipt → Intelligence

```
T0 Creates Dispatch
    │
    ├─ dispatch_tags (set by T0):
    │   type: implementation | test | review | refactor | docs | config
    │   risk: low | medium | high
    │   scope: single_file | multi_file | cross_module | infrastructure
    │   expected_ois: 0-N (predicted open items)
    │   depends_on: [dispatch_id, ...]
    │
    ▼
Worker Executes
    │
    ├─ auto_derived_tags (set by stop hook):
    │   files_changed: [path, ...]
    │   test_count: N
    │   line_delta: +N/-M
    │   duration_seconds: N
    │   model_used: string
    │   commit_hash: string
    │   syntax_valid: bool
    │   tool_use_count: N
    │   error_count: N
    │
    ▼
Haiku Classification
    │
    ├─ classified_tags (set by haiku):
    │   content_type: implementation | test | refactor | docs | review
    │   quality_score: 1-5
    │   complexity: low | medium | high
    │   consistency_score: 0.0-1.0
    │
    ▼
Assembled Report + Receipt
    │
    ├─ All tags merged into receipt JSON
    │
    ▼
Intelligence DB
    │
    ├─ dispatch_metadata row with all tags
    ├─ Tag combinations mined for patterns
    ├─ Success/failure patterns updated
    └─ Prevention rules generated
```

### 3.2 Unified Tag Schema

```python
@dataclass
class DispatchTags:
    """Tags set by T0 at dispatch creation."""
    type: str          # implementation | test | review | refactor | docs | config
    risk: str          # low | medium | high
    scope: str         # single_file | multi_file | cross_module | infrastructure
    expected_ois: int  # Predicted open item count
    depends_on: List[str]  # Upstream dispatch IDs

@dataclass
class AutoDerivedTags:
    """Tags derived deterministically by the stop hook."""
    files_changed: List[str]
    test_count: int
    line_delta_add: int
    line_delta_del: int
    duration_seconds: int
    model_used: str
    commit_hash: Optional[str]
    syntax_valid: bool
    tool_use_count: int
    error_count: int

@dataclass
class ClassifiedTags:
    """Tags set by haiku classification."""
    content_type: str      # implementation | test | refactor | docs | review
    quality_score: int     # 1-5
    complexity: str        # low | medium | high
    consistency_score: float  # 0.0-1.0

@dataclass
class UnifiedTagSet:
    """Complete tag set for a dispatch execution."""
    dispatch_tags: DispatchTags
    auto_derived: AutoDerivedTags
    classified: Optional[ClassifiedTags]  # None if haiku skipped
    outcome: str           # success | failure | partial | crashed
```

### 3.3 Intelligence DB Schema Extensions

The existing `dispatch_metadata` table needs extension to support the full tag set:

```sql
-- New columns for dispatch_metadata
ALTER TABLE dispatch_metadata ADD COLUMN dispatch_type TEXT;        -- from dispatch_tags.type
ALTER TABLE dispatch_metadata ADD COLUMN risk_level TEXT;           -- from dispatch_tags.risk
ALTER TABLE dispatch_metadata ADD COLUMN scope TEXT;                -- from dispatch_tags.scope
ALTER TABLE dispatch_metadata ADD COLUMN expected_ois INTEGER;      -- from dispatch_tags.expected_ois
ALTER TABLE dispatch_metadata ADD COLUMN depends_on TEXT;           -- JSON array of dispatch IDs
ALTER TABLE dispatch_metadata ADD COLUMN files_changed TEXT;        -- JSON array of file paths
ALTER TABLE dispatch_metadata ADD COLUMN test_count INTEGER;        -- from auto_derived
ALTER TABLE dispatch_metadata ADD COLUMN line_delta_add INTEGER;    -- from auto_derived
ALTER TABLE dispatch_metadata ADD COLUMN line_delta_del INTEGER;    -- from auto_derived
ALTER TABLE dispatch_metadata ADD COLUMN duration_seconds INTEGER;  -- from auto_derived
ALTER TABLE dispatch_metadata ADD COLUMN model_used TEXT;           -- from auto_derived
ALTER TABLE dispatch_metadata ADD COLUMN commit_hash TEXT;          -- from auto_derived
ALTER TABLE dispatch_metadata ADD COLUMN content_type TEXT;         -- from classified
ALTER TABLE dispatch_metadata ADD COLUMN quality_score INTEGER;     -- from classified (1-5)
ALTER TABLE dispatch_metadata ADD COLUMN complexity TEXT;           -- from classified
ALTER TABLE dispatch_metadata ADD COLUMN consistency_score REAL;    -- from classified (0.0-1.0)
ALTER TABLE dispatch_metadata ADD COLUMN auto_report_path TEXT;     -- path to assembled report
ALTER TABLE dispatch_metadata ADD COLUMN report_version TEXT;       -- "1.0" (manual) or "2.0" (auto)
```

### 3.4 Tag-Enabled Queries

The enriched schema enables queries that drive intelligence:

```sql
-- Success rate by risk level
SELECT risk_level, 
       COUNT(*) as total,
       SUM(CASE WHEN outcome_status = 'success' THEN 1 ELSE 0 END) as successes,
       ROUND(100.0 * SUM(CASE WHEN outcome_status = 'success' THEN 1 ELSE 0 END) / COUNT(*), 1) as success_rate
FROM dispatch_metadata
WHERE risk_level IS NOT NULL
GROUP BY risk_level;

-- Common failure patterns by scope
SELECT scope, dispatch_type, COUNT(*) as failures,
       AVG(duration_seconds) as avg_duration
FROM dispatch_metadata
WHERE outcome_status = 'failure'
GROUP BY scope, dispatch_type
ORDER BY failures DESC;

-- OI prediction accuracy
SELECT expected_ois, 
       AVG(actual_oi_count) as avg_actual,
       ABS(expected_ois - AVG(actual_oi_count)) as avg_error
FROM dispatch_metadata dm
LEFT JOIN (
    SELECT dispatch_id, COUNT(*) as actual_oi_count
    FROM open_items
    GROUP BY dispatch_id
) oi ON dm.dispatch_id = oi.dispatch_id
WHERE expected_ois IS NOT NULL
GROUP BY expected_ois;

-- Quality score trends over time (last 30 days)
SELECT date(dispatched_at) as day,
       AVG(quality_score) as avg_quality,
       COUNT(*) as dispatch_count
FROM dispatch_metadata
WHERE quality_score IS NOT NULL
  AND dispatched_at > datetime('now', '-30 days')
GROUP BY date(dispatched_at);

-- Model effectiveness comparison
SELECT model_used,
       AVG(quality_score) as avg_quality,
       AVG(duration_seconds) as avg_duration,
       ROUND(100.0 * SUM(CASE WHEN outcome_status = 'success' THEN 1 ELSE 0 END) / COUNT(*), 1) as success_rate
FROM dispatch_metadata
WHERE model_used IS NOT NULL
GROUP BY model_used;
```

---

## 4. Context Injection Architecture

### 4.1 Current System

`IntelligenceSelector.select()` queries three candidate classes from `quality_intelligence.db`:

1. **Proven patterns** (≥0.6 confidence, ≥2 evidence) — from `success_patterns` table
2. **Failure prevention** (≥0.5 confidence, ≥1 evidence) — from `antipatterns` + `prevention_rules`
3. **Recent comparable** (≥0.4 confidence, ≥1 evidence) — from `dispatch_metadata` (last 14 days)

Selection is scoped by task class (mapped from skill name): `coding_interactive`, `research_structured`, `docs_synthesis`, `ops_watchdog`, `channel_response`.

**Payload limits**: Max 3 items, max 500 chars/item, max 2000 chars total (~500 tokens).

### 4.2 Target Architecture

The current system works but retrieval is limited to tag-based filtering with simple confidence thresholds. The target adds two retrieval strategies:

```
┌─────────────────────────────────────────────────────────┐
│              Context Injection Pipeline                  │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  1. Tag-Based Retrieval (current, keep)                │
│     ├─ Proven patterns by task class                    │
│     ├─ Failure prevention by scope tags                 │
│     └─ Recent comparable by track + gate                │
│                                                         │
│  2. Feature-Scoped Working Memory (new)                │
│     ├─ Per-feature summary of decisions and risks       │
│     ├─ Maintained by T0 decision summarizer             │
│     └─ Injected when dispatch is for same feature       │
│                                                         │
│  3. Failure-Pattern Matching (new)                      │
│     ├─ Defect family normalization (strip IDs/dates)    │
│     ├─ Recurring failures across dispatches             │
│     └─ Injected as prevention advice                    │
│                                                         │
│  Budget: ≤10% of context window                         │
│  200K window → 20K tokens → ~8K chars                   │
│  Allocation: tags 2K + working memory 4K + failures 2K  │
└─────────────────────────────────────────────────────────┘
```

### 4.3 Feature-Scoped Working Memory

A new concept inspired by Mastra's working memory pattern. Each feature maintains a structured summary that T0 actively updates:

```python
@dataclass
class FeatureWorkingMemory:
    feature_id: str                    # e.g., "F36"
    last_updated: str                  # ISO 8601
    current_gate: str                  # planning | implementation | review | testing | validation
    key_decisions: List[str]           # ≤5 decisions, ≤100 chars each
    active_risks: List[str]            # ≤3 risks, ≤100 chars each
    open_items_summary: str            # ≤200 chars
    completed_dispatches: int
    failed_dispatches: int
    total_dispatches: int
```

**Storage**: `.vnx-data/state/feature_memory/{feature_id}.json`  
**Updated by**: T0 decision summarizer (haiku) after each T0 session  
**Injected into**: All dispatches for the same feature  
**Token cost**: ~400-650 tokens per feature

### 4.4 Retrieval Strategy Decision

| Strategy | Pros | Cons | VNX Fit |
|----------|------|------|---------|
| Full-text search | Simple, no infra | Poor semantic matching | Current (FTS5 on code_snippets) |
| Embedding similarity | Finds semantic matches | Requires embedding model + vector DB | Too much infra for local-first |
| Tag-based filtering | Deterministic, fast, auditable | Misses semantic connections | Current + enhanced |
| Hybrid (tags + FTS) | Best of both, no new infra | Slightly more complex queries | **Recommended** |
| Graph-based (Mem0-style) | Rich entity relationships | Neo4j dependency, complexity | Future consideration |

**Recommendation**: Extend current tag-based filtering with SQLite FTS5 search over extracted facts from reports. No embedding infrastructure needed. The existing `code_snippets` FTS5 table proves the pattern works.

### 4.5 Token Budget

For a 200K context window with target ≤10% intelligence context:

| Component | Max Tokens | Source |
|-----------|-----------|--------|
| CLAUDE.md (terminal role) | 2,800 | Static file |
| State snapshot (T0 brief, progress, OIs) | 4,840 | State files |
| Intelligence injection (patterns + prevention) | 500 | quality_intelligence.db |
| Feature working memory | 650 | feature_memory/{id}.json |
| Failure pattern warnings | 500 | defect_family matching |
| Dispatch instructions | 1,500 | dispatch prompt |
| **Total** | **~10,790** | **5.4% of 200K** |

Well within the 10% target with room for growth.

---

## 5. Quality Check Pipeline

### 5.1 Current Check Inventory

**Deterministic checks (0 LLM cost):**

| Check | Source | When | Blocking? |
|-------|--------|------|-----------|
| File size (lines) | `quality_advisory.py` | Post-receipt | Warn/Block at thresholds |
| Function size | `quality_advisory.py` | Post-receipt | Warn/Block at thresholds |
| Ruff linting | `quality_advisory.py` | Post-receipt | Warning |
| Shellcheck | `quality_advisory.py` | Post-receipt | Warning |
| Dead code (vulture) | `quality_advisory.py` | Post-receipt | Warning |
| Test coverage hygiene | `quality_advisory.py` | Post-receipt | Warning |
| Open items check | `pre_merge_gate.py` | Pre-merge | Blocking if blockers exist |
| CQS threshold | `pre_merge_gate.py` | Pre-merge | Blocking if CQS < 50 |
| Git cleanliness | `pre_merge_gate.py` | Pre-merge | Blocking if conflicts |
| Contract verification | `pre_merge_gate.py` | Pre-merge | Blocking |
| PR size | `pre_merge_gate.py` | Pre-merge | Warn 300 / Hold 600 lines |
| Pytest | `pre_merge_gate.py` | Pre-merge | Blocking if failures |
| File exists (claims) | `verify_claims.py` | Post-receipt | Info |
| File changed (claims) | `verify_claims.py` | Post-receipt | Info |
| Pattern match (claims) | `verify_claims.py` | Post-receipt | Info |
| Bash check (claims) | `verify_claims.py` | Post-receipt | Info |

**LLM-based checks (variable cost):**

| Check | Provider | When | Cost |
|-------|----------|------|------|
| Gemini review | Gemini 2.5 Flash | Review gate | Medium |
| Codex gate | GPT-5.2-Codex | Final gate (high-risk) | High |
| Claude GitHub review | Claude | Optional gate | High |
| Haiku classification | Claude Haiku | Stop hook Phase 3 | Low (~200 tokens) |

### 5.2 Proposed Pipeline Order

The pipeline runs in cost order — cheapest and most reliable first. Each phase gates the next:

```
Phase 0: Syntax (instant, 0 cost)
  ├─ py_compile on changed .py files
  ├─ bash -n on changed .sh files
  └─ GATE: If syntax fails → BLOCK, skip all subsequent phases

Phase 1: Deterministic Quality (fast, 0 cost)
  ├─ File size / function size checks
  ├─ Linting (ruff + shellcheck)
  ├─ Dead code detection (vulture)
  ├─ Test coverage hygiene
  ├─ Import verification on new files
  └─ GATE: Blocking findings → report but continue

Phase 2: Contract Verification (fast, 0 cost)
  ├─ Claimed files exist
  ├─ Claimed changes present in git diff
  ├─ Test count matches claim
  ├─ Commit hash matches claim
  └─ GATE: Verification failures → WARN

Phase 3: Test Execution (medium speed, 0 LLM cost)
  ├─ pytest with timeout
  ├─ Parse results (passed/failed/errors/skipped)
  └─ GATE: Test failures → BLOCK

Phase 4: Haiku Classification (fast, low cost)
  ├─ Semantic summary of changes
  ├─ Quality score (1-5)
  ├─ Consistency check (exit summary vs git diff)
  └─ GATE: consistency_score < 0.3 → WARN (claims don't match reality)

Phase 5: External Review Gates (slow, high cost)
  ├─ Gemini review (if enabled)
  ├─ Codex gate (if required by risk/path)
  ├─ Claude GitHub review (if enabled)
  └─ GATE: Blocking findings from any gate → HOLD

Phase 6: Pre-Merge Decision
  ├─ Aggregate all phase results
  ├─ Open items check
  ├─ CQS threshold check
  └─ DECISION: GO | HOLD | REJECT
```

### 5.3 False Positive / Negative Handling

| Phase | False Positive Risk | Mitigation |
|-------|-------------------|------------|
| Syntax | Near zero | `py_compile` and `bash -n` are authoritative |
| Quality | Medium (vulture, size limits) | Advisory-only for edge cases; T0 can override |
| Contract | Low | Check is binary (exists or doesn't) |
| Tests | Near zero (when tests exist) | Missing tests are a coverage gap, not false positive |
| Haiku | Medium | Consistency score is advisory; never auto-blocks |
| External | Variable by provider | Blocking findings require human review before action |

**Key principle**: Deterministic phases are authoritative. LLM phases are advisory. Only deterministic failures can auto-block without human review.

---

## 6. Self-Learning Loop Design

### 6.1 The Learning Cycle

```
                    ┌──────────────────┐
                    │  T0 Creates      │
                    │  Dispatch with   │
                    │  Intelligence    │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Worker Executes │
                    │  (with injected  │
                    │   patterns)      │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Stop Hook       │
                    │  Auto-assembles  │
                    │  Report + Tags   │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Receipt with    │
                    │  Full Tag Set    │
                    │  (dispatch +     │
                    │   auto + haiku)  │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
    ┌─────────▼────┐  ┌─────▼──────┐  ┌───▼───────────┐
    │ Pattern      │  │ Defect     │  │ Tag           │
    │ Update       │  │ Family     │  │ Combination   │
    │ (confidence  │  │ Detection  │  │ Mining        │
    │  + evidence) │  │ (normalize │  │ (2-3 tag      │
    │              │  │  + cluster)│  │  correlations) │
    └──────┬───────┘  └─────┬──────┘  └───┬───────────┘
           │                │              │
           └────────────────┼──────────────┘
                            │
                   ┌────────▼─────────┐
                   │  Intelligence DB │
                   │  Updated with    │
                   │  New Evidence    │
                   └────────┬─────────┘
                            │
                   ┌────────▼─────────┐
                   │  Recommendation  │
                   │  Engine          │
                   │  (propose based  │
                   │   on trends)     │
                   └────────┬─────────┘
                            │
                   ┌────────▼─────────┐
                   │  Next Dispatch   │
                   │  Gets Smarter    │
                   │  Injection       │
                   └──────────────────┘
```

### 6.2 Confidence Evolution

Pattern confidence grows with evidence and decays without it:

```python
def update_confidence(pattern: SuccessPattern, outcome: str) -> float:
    """Update pattern confidence based on dispatch outcome."""
    if outcome == "success":
        pattern.usage_count += 1
        pattern.success_count += 1
    elif outcome == "failure":
        pattern.usage_count += 1
        pattern.failure_count += 1

    # Base confidence from success rate
    if pattern.usage_count > 0:
        success_rate = pattern.success_count / pattern.usage_count
        base = 0.3 + (success_rate * 0.5)  # Range: 0.3-0.8
    else:
        base = 0.5

    # Evidence bonus (more evidence = more confidence, capped)
    evidence_bonus = min(0.2, pattern.usage_count * 0.02)

    # Recency decay (patterns not seen in 30 days lose confidence)
    days_since_last = (datetime.utcnow() - pattern.last_used).days
    recency_factor = max(0.5, 1.0 - (days_since_last / 60.0))

    return min(1.0, (base + evidence_bonus) * recency_factor)
```

### 6.3 Defect Family Detection

From `governance_signal_extractor.py`, defect families are normalized by stripping variable content:

```python
def normalize_defect_family(error_text: str) -> str:
    """Strip UUIDs, dispatch IDs, timestamps, numbers to create family key."""
    text = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>', text)
    text = re.sub(r'\d{8}-\d{6}-[a-zA-Z0-9-]+-[A-C]', '<DISPATCH_ID>', text)
    text = re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', '<TIMESTAMP>', text)
    text = re.sub(r'\b\d+\b', '<N>', text)
    return text.strip()
```

When the same family key appears ≥3 times across different dispatches, it becomes a prevention rule candidate. This is the key mechanism for turning recurring failures into preventive intelligence.

### 6.4 Recommendation Generation

The recommendation engine (existing `recommendation_tracker.py`) proposes improvements in four classes:

| Class | Example | Measurement |
|-------|---------|-------------|
| `prompt_patch` | "Add explicit test count claim to dispatch instructions" | First-pass success rate change |
| `routing_preference` | "Route infrastructure tasks to T3 instead of T1" | Rework rate change |
| `guardrail_adjustment` | "Increase stall timeout for review tasks from 90s to 180s" | Timeout rate change |
| `process_improvement` | "Split multi-file refactors into max 3 files per dispatch" | OI carry-over rate change |

Each recommendation enters a 7-day measurement window after acceptance. Before/after metrics are computed automatically from `dispatch_metadata`. Recommendations with negative outcomes are flagged for review.

---

## 7. Benchmark Plan

### 7.1 Benchmark Design: Headless vs Interactive T0

**Objective**: Compare decision quality, speed, and cost between headless T0 (polling daemon with fresh sessions) and interactive T0 (persistent tmux session).

**Setup**:
- Two git worktrees from the same commit
- Same feature plan, same workers (T1/T2/T3), same model (Claude Opus)
- Same dispatch sequence (predetermined, not T0-generated)
- T0 makes decisions only — dispatch creation is from a fixed queue

### 7.2 Metrics

| Metric | How Measured | Weight |
|--------|------------|--------|
| Decision correctness | Manual review: was approve/reject/dispatch the right call? | 30% |
| Decision speed | Time from receipt arrival to T0 decision | 15% |
| Token cost | Total tokens consumed by T0 across all decisions | 15% |
| OI tracking accuracy | OIs opened/closed correctly vs ground truth | 15% |
| Dispatch quality | Quality scores of T0-generated dispatches (rated by T3) | 15% |
| Context coherence | Does T0 reference prior decisions correctly? | 10% |

### 7.3 Scoring

Each decision is scored by a human reviewer on a 1-5 scale:
- **5**: Optimal decision with correct reasoning
- **4**: Correct decision, minor reasoning gaps
- **3**: Acceptable decision, significant reasoning gaps
- **2**: Suboptimal decision (didn't catch an issue or missed an opportunity)
- **1**: Wrong decision (approved bad work or rejected good work)

Final score = weighted average across all metrics. A score difference of <0.5 between headless and interactive is considered equivalent.

### 7.4 Fairness Controls

- **Same receipt corpus**: Identical receipts fed to both T0 variants in the same order
- **Same model**: Both use Claude Opus (or the same configured model)
- **Same state initialization**: Both start with the same `.vnx-data/state/` snapshot
- **No cross-contamination**: Separate worktrees, separate state directories
- **Blind evaluation**: Reviewer scores decisions without knowing which T0 variant produced them

### 7.5 Proposed Benchmark Features

Three features of increasing complexity:

| Feature | Complexity | Dispatches | Tracks | Expected Decisions |
|---------|-----------|-----------|--------|-------------------|
| **B1**: Add a new CLI flag to `bin/vnx` | Low | 3 (implement, test, review) | A+B+C | 3-4 receipt reviews, 0-1 OIs |
| **B2**: Refactor receipt processor from shell to Python | Medium | 6-8 (plan, implement×3, test, review, gate) | A+B+C | 6-8 receipt reviews, 2-3 OIs, 1 rejection expected |
| **B3**: Add new dashboard widget with SSE streaming | High | 10-12 (plan, implement×5, test×2, review×2, gate×2) | A+B+C | 10-12 reviews, 3-5 OIs, multi-track coordination |

### 7.6 Test Infrastructure

Existing infrastructure in `/tests/headless_t0/`:
- `fake_data.py` — Receipt and report generators
- `setup_sandbox.py` — State directory creation and reset
- `assertions.py` — Decision validation helpers

For the benchmark, extend with:
- `benchmark_runner.py` — Feeds same receipt sequence to both T0 variants
- `benchmark_scorer.py` — Collects human scores, computes weighted averages
- `benchmark_report.py` — Generates comparison report

---

## 8. Implementation Roadmap

### Phase 1: Stop Hook Foundation (1-2 PRs)

**Dependencies**: None  
**Goal**: Auto-report assembly replaces manual worker reports

1. **PR: Stop hook + deterministic extraction**
   - Create `scripts/hooks/worker_stop.py`
   - Implement Phase 1 (git extraction) + Phase 2 (quality checks)
   - Write assembled report to `unified_reports/`
   - Append structured receipt to `t0_receipts.ndjson`
   - Hook registration in `.claude/settings.json`

2. **PR: Haiku classification + tag flow**
   - Implement Phase 3 (haiku classification)
   - Extend `dispatch_metadata` table with new tag columns
   - Persist full `UnifiedTagSet` in intelligence DB
   - Update `dispatch_create.sh` to set `dispatch_tags` on new dispatches

### Phase 2: Intelligence Enhancement (2-3 PRs)

**Dependencies**: Phase 1  
**Goal**: Close the learning loop

3. **PR: Confidence evolution + defect family detection**
   - Implement `update_confidence()` with evidence + recency
   - Wire `normalize_defect_family()` into signal extraction
   - Connect `governance_signal_extractor.collect_governance_signals()` to daemon
   - Generate prevention rules from ≥3 occurrence families

4. **PR: Feature-scoped working memory**
   - Create `FeatureWorkingMemory` dataclass and storage
   - T0 decision summarizer writes/updates feature memory after each session
   - `IntelligenceSelector` injects feature memory into same-feature dispatches
   - Extend payload limit from 2000 to 4000 chars (for working memory)

5. **PR: Enhanced tag-based queries + FTS integration**
   - Build tag combination mining from enriched `dispatch_metadata`
   - Connect `tag_intelligence.py` to new columns
   - Add FTS5 indexing over extracted report facts
   - Dashboard integration: quality trends by tag

### Phase 3: Quality Pipeline Standardization (1-2 PRs)

**Dependencies**: Phase 1  
**Goal**: Ordered, configurable quality check pipeline

6. **PR: Unified quality pipeline runner**
   - Consolidate `quality_advisory.py` + `pre_merge_gate.py` + `verify_claims.py` checks
   - Implement phase-gated execution (syntax → quality → contract → tests → haiku → gates)
   - Configurable per-dispatch: which phases run, which are blocking
   - Structured pipeline result that feeds into assembled report

### Phase 4: Benchmark & Validation (1-2 PRs)

**Dependencies**: Phases 1-2  
**Goal**: Validate headless T0 against interactive T0

7. **PR: Benchmark infrastructure**
   - `benchmark_runner.py` — dual-worktree execution
   - `benchmark_scorer.py` — human evaluation framework
   - Execute B1 (low complexity) as proof of concept

8. **PR: Full benchmark execution**
   - Execute B2 + B3 benchmarks
   - Generate comparison report
   - Decision on headless T0 readiness for production

### Phase 5: F18 Integration (2-3 PRs)

**Dependencies**: Phase 2  
**Goal**: Complete the learning-loop signal enrichment feature

9. **PR: F18 PR-1 — Signal enrichment from runtime, gates, and OIs**
10. **PR: F18 PR-2 — Recurrence detection and retrospective digests**
11. **PR: F18 PR-3 — Local-model retrospective analysis hook (optional)**

### Dependency Graph

```
Phase 1 (Stop Hook) ──┬── Phase 2 (Intelligence) ── Phase 5 (F18)
                      │
                      ├── Phase 3 (Quality Pipeline)
                      │
                      └── Phase 4 (Benchmark)
```

---

## 9. Framework Comparison Table

| Dimension | VNX (Current) | VNX (Target) | LangGraph | Mastra | CrewAI | AG2 | Devin | Cursor | Copilot Workspace |
|-----------|--------------|-------------|-----------|--------|--------|-----|-------|--------|-------------------|
| **Memory Model** | File-based (reports + DB) | Tag-driven + feature memory + FTS | Checkpoint-based (full state per step) | Vector + working memory | LLM-encoded cognitive | Dict side-channel | Playbooks + wiki (no cross-session) | Vector embeddings | None |
| **Persistence** | SQLite + NDJSON + files | Same + structured tags | Postgres/Redis/DynamoDB | LibSQL/Postgres + vector DB | Vector DB + LLM encoding | In-memory only | Per-project knowledge | Server-side vectors | None |
| **Retrieval** | Tag filtering + confidence thresholds | Tag + FTS5 hybrid | Checkpoint resume by thread | Semantic top-K + message range | Adaptive-depth (similarity + recency + importance) | Dictionary lookup | Codebase search | Semantic nearest-neighbor | N/A |
| **Cross-Session** | Yes (intelligence DB) | Yes (enhanced) | Yes (via checkpointer) | Yes (resource scope) | Yes (persistent memory) | No | No (session-only) | No | No |
| **Quality Gates** | Multi-gate (Gemini + Codex + Claude) | Phased pipeline (6 phases) | None (custom conditional edges) | None | None | None | In-session test loop | None | Multi-tool (CodeQL + tests + secrets + review) |
| **Human Gates** | Mandatory (dispatch approval) | Mandatory (preserved) | Optional (pause at checkpoint) | None | None | None | PR review | None | None (automated) |
| **Audit Trail** | NDJSON receipts + coordination events | Same + auto-assembled reports | Checkpoint history | None | None | None | Session logs | None | PR history |
| **Learning Loop** | Partial (inject patterns, no confidence update) | Full (inject → execute → measure → update confidence) | None | None | Contradiction detection | None | In-session correction | None | None |
| **Context Budget** | ~500 tokens injection | ~2K tags + ~650 working memory + ~500 failures | Full state per step (unbounded) | Top-K messages (~4K) | Adaptive (LLM-controlled) | Full dict (unbounded) | Context window limited | ~8K code chunks | N/A |
| **Infrastructure** | SQLite + filesystem (local-first) | Same (no new infra) | Requires DB server | Requires embedding model + vector DB | Requires LLM for memory ops | None | Cloud-hosted | Cloud-hosted | GitHub-hosted |
| **Provider Lock-in** | None (CLI-only, provider-agnostic) | None (preserved) | LangChain ecosystem | Mastra SDK | CrewAI SDK | AG2/AutoGen SDK | Cognition (proprietary) | Cursor (proprietary) | GitHub (proprietary) |

### Key Differentiators

**VNX's architectural advantages**:
1. **Human-gated audit trail** — No other framework has mandatory human approval with NDJSON receipt trails
2. **Provider-agnostic** — Works with Claude, Codex, Ollama via CLI subprocess; no SDK lock-in
3. **Local-first, zero-infra** — SQLite + filesystem; no servers, no cloud dependencies
4. **Deterministic-first quality** — Cost-ordered pipeline: syntax → lint → tests → LLM (only when needed)
5. **Closed learning loop** — Patterns flow dispatch → execution → receipt → intelligence → next dispatch

**VNX's gaps vs competitors**:
1. **No semantic retrieval** — Tag filtering misses semantic connections (vs Mastra/Cursor vector search)
2. **No automatic fact extraction** — Reports are unstructured blobs (vs CrewAI's extract → remember)
3. **No working memory per feature** — Context lost between dispatches for same feature (vs Mastra's resource scope)
4. **Learning loop not yet closing** — Confidence doesn't update from outcomes (F18 dependency)

All four gaps are addressed in the target architecture (Phases 1-2 of the roadmap).

### Patterns Adopted from Research

| Pattern | Source | VNX Adaptation |
|---------|--------|----------------|
| Feature-scoped working memory | Mastra resource scope | `FeatureWorkingMemory` JSON per feature |
| Atomic fact extraction | CrewAI extract → remember, Mem0 | Stop hook extracts structured tags from reports |
| Phased quality pipeline | Copilot Workspace multi-tool | 6-phase cost-ordered pipeline with gating |
| Defect family normalization | Academic (self-improving agents) | Strip IDs/dates → cluster recurring failures |
| Confidence with recency decay | Mem0 temporal relevance | `update_confidence()` with 60-day decay window |
| Scope hierarchy | CrewAI filesystem paths | Tag schema: type/risk/scope/feature scoping |

### Patterns Explicitly Rejected

| Pattern | Source | Reason |
|---------|--------|--------|
| Implicit checkpointing | LangGraph | VNX's explicit human gates are a strength; implicit saves create checkpoint bloat |
| LLM-heavy memory ops | CrewAI encode/consolidate | Cost and latency prohibitive; use LLM only at report boundaries |
| Server-side embeddings | Cursor | Privacy concerns; VNX operates on potentially sensitive codebases |
| Full graph database | Mem0 Neo4j | Infrastructure overhead; SQLite FTS5 provides 80% of the benefit |
| Autonomous policy rewrites | Academic self-improving | VNX is advisory-only by design; operator authority must be preserved |

---

*End of Architecture Document*
