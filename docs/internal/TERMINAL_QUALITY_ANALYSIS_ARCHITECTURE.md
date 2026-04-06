# Terminal Quality Analysis Architecture

## 1. Current State

### Self-Learning Pipeline (Feature 18)

The F18 pipeline has three layers, orchestrated by `scripts/intelligence_daemon.py`:

**Layer 1: Signal Extraction** (`scripts/lib/governance_signal_extractor.py`)
Extracts 7 signal types from governance artifacts:
- `session_failure` — headless session_failed or session_timed_out
- `session_artifact` — artifact_materialized (execution evidence)
- `gate_failure` — quality gate did not pass
- `gate_success` — quality gate passed
- `queue_anomaly` — delivery failure, reconcile error, dead letter, queue stall
- `open_item_transition` — open-item status change (new/escalated/resolved)
- `defect_family` — normalized recurring defect pattern (N occurrences)

Signals carry full correlation context: feature_id, pr_id, session_id, dispatch_id, provider_id, terminal_id, branch.

**Layer 2: Recurrence Detection & Digest** (`scripts/lib/retrospective_digest.py`)
- Groups signals by `defect_family` key (MD5 of normalized content)
- Produces `RecurrenceRecord` for families with count >= 2
- Generates advisory-only `Recommendation` objects (4 categories: review_required, runtime_fix, policy_change, prompt_tuning)
- Assembles `RetroDigest` for T0 consumption

**Layer 3: Optional LLM Hook** (`scripts/lib/retrospective_model_hook.py`)
- Protocol-based: `LocalModelHook.analyze()` receives digest, returns `RetroAnalysisSummary`
- Always non-authoritative (`authoritative=False` enforced)
- Fallback: rule-based summary when no model configured
- Proposes candidate guardrails; T0 decides

**Orchestration** (`scripts/intelligence_daemon.py`)
- `GovernanceDigestRunner`: 5-min cadence, reads gate results + queue anomalies from `t0_receipts.ndjson`, writes `governance_digest.json`
- `IntelligenceDaemon`: hourly extraction, daily hygiene at 18:00, learning cycle

### Conversation Analyzer (`scripts/conversation_analyzer.py`)
- Parses Claude Code JSONL session logs for token/tool/message metrics
- Heuristic pattern detection (token thresholds, tool counts)
- Optional LLM deep analysis for high-cost sessions
- Stores in `session_analytics` table
- **Limitation**: Analyzes session metadata only, NOT output quality or correctness

## 2. Gap Analysis

| Aspect | Current Coverage | Missing |
|--------|-----------------|---------|
| Gate pass/fail | Signals extracted | No root-cause from output |
| Session failures | Timeout/crash detected | No thinking-loop or stall detection |
| Code quality | Not analyzed | Function/file size violations introduced by worker |
| Instruction adherence | Not analyzed | Deviation from dispatch instructions |
| Search efficiency | Not analyzed | Excessive grep/glob before finding target |
| Rework patterns | Not analyzed | Multiple commits on same file in one dispatch |
| Test outcomes | Not analyzed | Worker code failing tests |
| Output correctness | Not analyzed | Wrong instructions, hallucinated paths |

The pipeline knows WHAT happened (pass/fail) but not WHY. Terminal output is the missing link between dispatch instructions and outcomes.

## 3. Proposed Signal Types

Six new signal types for `governance_signal_extractor.py`:

### `terminal_rework`
- **Extraction**: Parse git log for dispatch branch — count commits touching same file >2 times
- **Source**: Git history on dispatch branch
- **Severity**: warn (3+ touches), info (2 touches)
- **Correlation**: dispatch_id, terminal_id, file paths

### `terminal_timeout`
- **Extraction**: Worker ran out of context window without producing final report
- **Source**: Unified reports directory — dispatch_id present in receipt but no corresponding report file
- **Severity**: blocker
- **Correlation**: dispatch_id, terminal_id, provider_id

### `terminal_search_excessive`
- **Extraction**: Count Grep/Glob/Read tool calls before first Edit/Write in stream-json events
- **Source**: F28 stream-json event stream (future), or conversation JSONL (current)
- **Severity**: warn (>10 search ops), info (>5)
- **Correlation**: dispatch_id, terminal_id, session_id

### `terminal_test_failure`
- **Extraction**: Detect test runner exit codes != 0 in Bash tool results
- **Source**: Stream-json events or unified reports with test evidence
- **Severity**: warn (flaky), blocker (consistent failure)
- **Correlation**: dispatch_id, terminal_id, test file paths

### `terminal_code_quality`
- **Extraction**: Post-commit static analysis — function length >50 lines, file >500 lines introduced by worker
- **Source**: Git diff on dispatch branch + simple line-count heuristic
- **Severity**: info (function >50L), warn (file >500L)
- **Correlation**: dispatch_id, terminal_id, file paths

### `terminal_instruction_mismatch`
- **Extraction**: Compare dispatch instruction scope (files mentioned, task type) vs actual files modified
- **Source**: Dispatch JSON + git diff
- **Severity**: warn (modified files not in scope), blocker (no overlap with scope)
- **Correlation**: dispatch_id, terminal_id

## 4. Integration with Existing Pipeline

### Extraction Phase
New extractors plug into `collect_governance_signals()` via additional keyword arguments:

```python
def collect_governance_signals(
    *,
    # existing sources
    session_timeline=None,
    gate_results=None,
    queue_anomalies=None,
    open_item_transitions=None,
    # new terminal quality sources
    terminal_quality_events=None,   # List[Dict] from git/report analysis
    stream_json_events=None,        # List[Dict] from F28 subprocess streams
    correlation=None,
    ...
)
```

### Digest Phase
`retrospective_digest.py` needs no changes — `detect_recurrences()` and `generate_recommendations()` operate on any signal with a `defect_family` key. New signal types will naturally cluster into families.

### New recommendation category
Add `worker_coaching` to `RECOMMENDATION_CATEGORIES`:
- Triggered by recurring `terminal_search_excessive` or `terminal_rework`
- Advisory: "Worker on T1 repeatedly exceeds 10 search operations. Consider providing more specific file paths in dispatch instructions."

## 5. F28/F29 Streaming Analysis Design

### Stream-JSON Event Model (F28)
When `SubprocessAdapter` emits `--output-format stream-json`, each event contains:
- `type`: tool_use, tool_result, text, error, system
- `tool`: tool name (Read, Grep, Edit, Bash, etc.)
- `timestamp`: event time
- `content`: truncated output

### Real-Time Analysis Hooks
A `StreamAnalyzer` class processes the event stream in-flight:

1. **Thinking loop detector**: If >5 consecutive text events without tool_use, flag as potential reasoning loop
2. **Search spiral detector**: Count sequential Grep/Glob/Read without Edit — threshold at 10
3. **Stall detector**: No events for >60s while process still alive
4. **Error cascade detector**: >3 consecutive tool_result with error status

### Integration Point
`StreamAnalyzer` emits `terminal_quality_events` in real-time, which the `GovernanceDigestRunner` can pick up on its next 5-min cycle. For dashboard integration, events also write to a ring buffer file (`stream_quality_events.ndjson`, max 1000 lines).

### Dashboard Panel (F29)
- New panel: "Worker Quality" showing per-terminal quality scores
- Metrics: search efficiency ratio, rework rate, test pass rate
- Sourced from `governance_digest.json` terminal quality section

## 6. Recommendation

**Do NOT bundle with F28.** Terminal quality analysis should be a separate feature (F30 or similar) because:

1. F28 SubprocessAdapter is infrastructure — it should ship lean
2. Quality signals from git/reports work TODAY without stream-json
3. Stream-json analysis is an incremental enhancement once F28 lands
4. Separate feature allows independent testing and rollback

**Phased delivery**:
- Phase 1 (now): Git-based signals (rework, code quality, instruction mismatch) — no F28 dependency
- Phase 2 (post-F28): Stream-json signals (search excessive, thinking loops, stall detection)
- Phase 3 (post-F29): Dashboard integration with quality scoring panel
