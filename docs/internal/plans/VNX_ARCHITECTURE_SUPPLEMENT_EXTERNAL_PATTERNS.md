# VNX Architecture Supplement: External Pattern Analysis

**Author**: T3 (Track C) — Architecture Follow-up  
**Dispatch-ID**: 20260408-010001-f36-governance-intelligence-arch-C  
**Date**: 2026-04-08  
**Parent Document**: `docs/internal/plans/VNX_GOVERNANCE_INTELLIGENCE_ARCHITECTURE.md`  
**Status**: Planning Document — No Code Implementation

**Source Repositories**:
- https://github.com/DimitriGeelen/agentic-engineering-framework (Apache 2.0, Bash/Python)
- https://github.com/DimitriGeelen/termlink (Rust, 6 crates, 705 tests)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Source Analysis: Agentic Engineering Framework](#2-source-analysis-agentic-engineering-framework)
3. [Source Analysis: TermLink](#3-source-analysis-termlink)
4. [VNX Memory Architecture](#4-vnx-memory-architecture)
5. [Healing & Escalation Model](#5-healing--escalation-model)
6. [Component Topology & Blast Radius](#6-component-topology--blast-radius)
7. [Runtime & Session Control Primitives](#7-runtime--session-control-primitives)
8. [Operator Capability Model](#8-operator-capability-model)
9. [Adopt / Adapt / Ignore](#9-adopt--adapt--ignore)
10. [Implications for F37+](#10-implications-for-f37)

---

## 1. Executive Summary

Two external repositories were analyzed for patterns that strengthen VNX's intelligence, memory, control plane, and governance layers — without replacing VNX's governance-first, receipt-led, local-first architecture.

**Key findings**:

- **Agentic Engineering Framework (AEF)** is a single-agent governance layer for coding agents. Its strongest contributions to VNX are: a 3-layer memory taxonomy (working/project/episodic), a 4-step healing escalation ladder (A→D), per-file component cards with blast radius analysis, and self-audit independence principles. AEF operates at a different scale (single repo, single agent) but its governance concepts are directly transferable.

- **TermLink** is a Rust-based terminal session manager with physically separated control and data planes, 4-tier capability tokens (observe/interact/control/execute), and a stateless hub with file-based session registry. Its session primitives and capability model provide a concrete blueprint for VNX's future runtime API and operator permission model.

**What VNX already has that neither repo provides**: multi-agent dispatch orchestration, receipt-led audit trails, SQLite coordination with lease management, quality gates with multi-provider review, and intelligence injection with confidence scoring. These are VNX's differentiators and must not be diluted.

**What VNX gains from this analysis**: a formal memory taxonomy, structured healing loops, component-aware dispatch planning, cleaner runtime abstraction, and a capability-based operator security model.

---

## 2. Source Analysis: Agentic Engineering Framework

### 2.1 Architecture Overview

AEF is a governance layer that wraps a single AI coding agent (Claude Code, Cursor, etc.) within a single git repository. It enforces structural rules via git hooks, Claude Code PreToolUse/PostToolUse hooks, and periodic audits.

**Scope difference from VNX**: AEF governs one agent in one repo. VNX orchestrates 4 terminals (T0-T3) across multiple features with dispatch coordination, quality gates, and receipt-driven closure. AEF's concepts must be lifted from single-agent to multi-agent context.

### 2.2 Memory System ("Context Fabric")

AEF implements a 3-layer memory stored as YAML files under `.context/`:

| Layer | Storage | Scope | Contents |
|-------|---------|-------|----------|
| **Working Memory** | `.context/working/session.yaml`, `focus.yaml` | Per-session, ephemeral | Current task, priorities, blockers, pending decisions, reminders |
| **Project Memory** | `.context/project/learnings.yaml`, `patterns.yaml`, `decisions.yaml` | Per-project, persistent | Learnings (L-XXX), patterns (FP/SP/AF/WP-XXX), decisions (D-XXX) with rationale |
| **Episodic Memory** | `.context/episodic/T-XXX.yaml` | Per-task, persistent | Auto-generated from git (commits, files, timeline) + task file (acceptance criteria, decisions) |

**Semantic Recall** via `memory-recall.py`: hybrid search (vector embeddings via Ollama → fallback to keyword scoring) across all three layers, normalized to common `{type, id, text, context, task, application}` structure.

**Key design**: Episodic memory is auto-generated from git data (commit messages → challenges, file diffs → artifacts, timestamps → timeline). Human/agent enrichment is tracked separately (`enrichment_status: auto-complete | complete`).

### 2.3 Healing Loop

AEF's healing subsystem (`agents/healing/`) implements a 4-step escalation ladder for recurring failures:

| Step | Name | Action |
|------|------|--------|
| **A** | "Don't repeat the same failure" | Check patterns.yaml for known mitigations, review similar episodic summaries |
| **B** | "Improve technique" | Type-specific suggestions (add tests, pin versions, revisit design) |
| **C** | "Improve tooling" | Add automated check for this condition, update audit agent |
| **D** | "Change ways of working" | Add to pre-work checklist, create new practice |

Each pattern tracks `escalation_step` (current level) and `occurrences_at_step` (recurrence count at this level). The `resolve` command produces dual output: a failure pattern (FP-XXX) in `patterns.yaml` AND a learning (L-XXX) in `learnings.yaml`.

**Pattern types**: `failure_patterns` (FP), `success_patterns` (SP), `antifragile_patterns` (AF — capability improvements triggered by failures), `workflow_patterns` (WP).

**Weakness**: Escalation is advisory — no automated mechanism triggers A→B→C→D progression. The `occurrences_at_step` field exists but is not auto-incremented.

### 2.4 Component Fabric & Blast Radius

AEF maintains per-file component cards in `.fabric/components/` (YAML, committed to repo):

```yaml
id: agents/healing/lib/diagnose.sh
name: diagnose
type: script
subsystem: healing
depends_on:
  - { target: agents/context/lib/pattern.sh, type: calls }
  - { target: .context/project/patterns.yaml, type: reads }
depended_by: []
last_verified: 2025-01-15
```

**Blast radius**: `fw fabric blast-radius` takes a git ref, gets changed files via `git diff-tree`, looks up each file's card, and reports downstream write dependencies.

**Drift detection**: Three categories — unregistered (files without cards), orphaned (cards without files), stale edges (dependencies pointing to non-existent components).

**Subsystem inference**: Path patterns map files to 12 subsystems automatically. Override via `.fabric/subsystem-rules.yaml`.

### 2.5 Audit Independence

AEF's `self-audit.sh` verifies framework integrity **without depending on the framework itself** — a principle worth adopting. It checks 5 layers independently:

1. Foundation (executables exist, syntax valid)
2. Directory structure (state storage exists)
3. Claude Code hooks (PreToolUse/PostToolUse correctly configured)
4. Git hooks (commit-msg, post-commit, pre-push installed)
5. Version consistency

The `controls.yaml` register documents 27 formal controls, each with: type, implementation file, blocking status, mitigated risks, and **explicit failure mode** (how the control can break). This is rare even in enterprise governance.

---

## 3. Source Analysis: TermLink

### 3.1 Architecture Overview

TermLink is a Rust-based terminal session manager with 6 crates: protocol, session, hub, mcp, cli, test-utils. It manages terminal sessions via Unix domain sockets with a clean control/data plane separation.

### 3.2 Control Plane vs Data Plane

**Physical separation** — two Unix sockets per session:

| Plane | Socket | Wire Format | Purpose |
|-------|--------|-------------|---------|
| **Control** | `{id}.sock` | JSON-RPC 2.0 (newline-delimited) | Commands, queries, events, KV, session mgmt |
| **Data** | `{id}.sock.data` | Binary frames (22-byte header: magic, length, type, flags, sequence, channel) | PTY I/O streaming |

The control plane never carries terminal bytes. The data plane never carries RPC commands. A read-only observer can connect to the data plane without any ability to issue control commands.

**Data plane multiplexing**: Uses `tokio::sync::broadcast` — N clients observe the same PTY output with independent backpressure (lagging clients get `Lagged(n)`, not a block).

### 3.3 Session Primitives

| Primitive | RPC Method | What It Does |
|-----------|-----------|--------------|
| **list** | Directory scan | Reads `sessions/*.json`, filters by liveness (`kill(pid, 0)` + socket exists) |
| **ping** | `termlink.ping` | Returns session_id, state, display_name, uptime |
| **exec** | `command.execute` | Runs `sh -c <command>` with timeout, env vars, cwd, optional allowlist |
| **pty output** | `query.output` | Reads from scrollback ring buffer (last N lines/bytes, optional ANSI strip) |
| **pty inject** | `command.inject` | Writes keystrokes to PTY master fd (text, named keys, raw base64) |
| **attach** | Data plane connect | Full bidirectional TUI mirror via binary frame protocol |

**Session lifecycle**: `Initializing → Ready → Busy → Ready (cycle) → Draining → Gone`  
**Session identity**: ULID-based, filesystem-safe, prefixed with `tl-`  
**Registration**: JSON sidecar file per session (atomic write via tmp+rename)

### 3.4 Capability Model

4-tier permission scoping enforced per-connection:

```
Observe (0)  — ping, query.*, event.poll, kv.get (read-only)
Interact (1) — event.emit, session.update, kv.set (mutates state)
Control (2)  — command.inject, command.signal (affects processes)
Execute (3)  — command.execute (runs shell commands)
```

**Hierarchy**: Higher scopes grant access to all lower scopes. Unknown methods default to Execute (deny by default).

**Authentication**: 3 layers:
1. UID-based (`SO_PEERCRED` / `LOCAL_PEERCRED`) — reject connections from different users
2. Capability tokens (HMAC-SHA256) — scope + session_id + TTL + nonce
3. Command allowlist — prefix-based filter on `command.execute`

### 3.5 Hub Architecture

The Hub is **stateless** — holds no persistent state, reads session registrations from disk. Crash recovery = restart. Supervisor polls liveness every 30s, auto-cleans dead sessions, emits `session.exited` events. Circuit breaker opens after 3 consecutive failures to a session.

---

## 4. VNX Memory Architecture

### 4.1 Current State

VNX has intelligence storage but no formal memory taxonomy:

| What Exists | Where | Gap |
|-------------|-------|-----|
| Intelligence injection (proven patterns, failure prevention, recent comparable) | `quality_intelligence.db` via `IntelligenceSelector` | No structured working/project/episodic distinction |
| Decision memory (proposed) | `t0_decision_log.jsonl` (from headless T0 architecture) | Not yet implemented |
| Feature working memory (proposed) | `FeatureWorkingMemory` dataclass in parent architecture doc | Not yet implemented |
| Dispatch metadata + receipts | `dispatch_metadata` table + `t0_receipts.ndjson` | Flat — not organized by memory type |

### 4.2 Proposed VNX Memory Taxonomy

Adapt AEF's 3-layer model to VNX's multi-agent, dispatch-driven context:

```
┌─────────────────────────────────────────────────────────────────┐
│                    VNX Memory Architecture                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Layer 1: Working Memory (per-dispatch, ephemeral)             │
│  ├─ Scope: single dispatch execution                            │
│  ├─ Contents: current task focus, active blockers,              │
│  │   pending decisions, dispatch context                        │
│  ├─ Storage: injected into dispatch prompt at creation           │
│  ├─ Lifetime: dispatch start → receipt                          │
│  └─ VNX equivalent: dispatch_tags + intelligence_payload        │
│     in bundle.json (already exists, formalize as working memory)│
│                                                                 │
│  Layer 2: Feature Memory (per-feature, persistent)             │
│  ├─ Scope: all dispatches within one feature (e.g., F36)       │
│  ├─ Contents: key decisions, active risks, completed gates,     │
│  │   OI summary, dispatch outcomes, learned patterns            │
│  ├─ Storage: .vnx-data/state/feature_memory/{feature_id}.json  │
│  ├─ Updated by: T0 decision summarizer after each session       │
│  ├─ Injected into: all dispatches for the same feature          │
│  └─ Token cost: ~400-650 tokens                                │
│                                                                 │
│  Layer 3: Project Memory (cross-feature, persistent)           │
│  ├─ Scope: entire project, all features                         │
│  ├─ Contents: proven patterns, failure prevention rules,        │
│  │   architectural decisions, prevention rules from tag mining  │
│  ├─ Storage: quality_intelligence.db (already exists)           │
│  ├─ Updated by: intelligence_persist from governance signals    │
│  ├─ Injected via: IntelligenceSelector (max 3 items, 2K chars) │
│  └─ Token cost: ~500 tokens                                    │
│                                                                 │
│  Layer 4: Episodic Memory (per-dispatch, persistent)           │
│  ├─ Scope: historical record of each dispatch execution         │
│  ├─ Contents: auto-assembled from git diff + stream archive     │
│  │   + test results + haiku classification                      │
│  ├─ Storage: .vnx-data/auto_reports/{dispatch_id}.json          │
│  │   (structured) + unified_reports/ (markdown)                 │
│  ├─ Generated by: stop hook auto-report assembler               │
│  ├─ Queried by: T0 for decision context, intelligence daemon    │
│  │   for pattern mining                                         │
│  └─ Token cost: 0 at dispatch time (queried on demand)         │
│                                                                 │
│  Semantic Recall (future, deferred)                             │
│  ├─ FTS5 on code_snippets table (already exists in schema)      │
│  ├─ FTS5 on extracted facts from episodic memory (new)          │
│  ├─ No embedding infrastructure (consistent with local-first)   │
│  └─ Hybrid: tag-based filtering + FTS5 keyword search           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.3 How Memory Layers Map to VNX Lifecycle

```
T0 Creates Dispatch
  ├─ Layer 1 (Working): dispatch_tags + intelligence_payload → bundle.json
  ├─ Layer 2 (Feature): FeatureWorkingMemory → injected into prompt
  └─ Layer 3 (Project): IntelligenceSelector → 0-3 items

Worker Executes Dispatch
  └─ Layer 1 (Working): active during execution, ephemeral

Stop Hook Fires
  └─ Layer 4 (Episodic): auto-assembled from git + stream + checks

T0 Reviews Receipt
  ├─ Layer 4 (Episodic): reads auto-report for decision context
  ├─ Layer 2 (Feature): updates feature memory with decision + outcomes
  └─ Layer 3 (Project): intelligence_persist updates patterns

Next Dispatch
  └─ All layers are fresher, more accurate, better scoped
```

### 4.4 What VNX Already Has vs What's New

| Component | Status | Action |
|-----------|--------|--------|
| `intelligence_payload` in `bundle.json` | Exists | Formalize as "working memory injection" |
| `IntelligenceSelector` with 3-class injection | Exists | Formalize as "project memory retrieval" |
| `quality_intelligence.db` (patterns, antipatterns, prevention rules) | Exists | Formalize as "project memory store" |
| `FeatureWorkingMemory` dataclass | Proposed in parent doc | Implement as "feature memory" |
| Auto-report assembler (structured episodic records) | Proposed in parent doc | Implement as "episodic memory" |
| FTS5 on `code_snippets` | Schema exists, table created | Extend to episodic fact search |
| Semantic recall via embeddings | Not planned | Defer — FTS5 + tag filtering is sufficient for now |

**Key insight**: VNX already has ~70% of the memory architecture in scattered form. The value is in formalizing it as a taxonomy with explicit layer names, scoping rules, and injection points — not in building new infrastructure.

---

## 5. Healing & Escalation Model

### 5.1 Current State

VNX has partial healing capabilities:

| What Exists | Where | Gap |
|-------------|-------|-----|
| Defect family normalization | `governance_signal_extractor.py` | Not wired into daemon |
| Prevention rules table | `quality_intelligence.db` | Not auto-generated from patterns |
| Recommendation tracker | `recommendation_tracker.py` | Full lifecycle, but no escalation model |
| Rework index | `dispatch_metadata` | Tracked but not used for escalation |

### 5.2 Proposed VNX Healing Model

Adapt AEF's 4-step escalation ladder to VNX's multi-agent dispatch context:

```
┌───────────────────────────────────────────────────────────────┐
│              VNX Healing & Escalation Pipeline                │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│  DETECTION: Recurring failure family detected                 │
│  ├─ Source: defect family normalization (strip IDs/dates)     │
│  ├─ Trigger: same family key appears ≥3 times across         │
│  │   different dispatches                                     │
│  └─ Storage: antipatterns table with occurrence_count         │
│                                                               │
│  ESCALATION LADDER:                                           │
│                                                               │
│  Step A: "Inject Prevention" (automatic)                      │
│  ├─ Action: add failure_prevention item to IntelligenceSelector│
│  ├─ Injected into: future dispatches with matching scope tags │
│  ├─ Escalation trigger: recurs ≥2 more times at Step A       │
│  └─ VNX mechanism: intelligence_persist → selector retrieval  │
│                                                               │
│  Step B: "Strengthen Checks" (automatic)                      │
│  ├─ Action: generate prevention_rule from tag combination     │
│  ├─ Effect: quality check pipeline adds specific validation   │
│  ├─ Escalation trigger: recurs ≥2 more times at Step B       │
│  └─ VNX mechanism: prevention_rules table → quality_advisory  │
│                                                               │
│  Step C: "Scope Dispatch Differently" (T0 advisory)           │
│  ├─ Action: recommend routing/scoping change to T0            │
│  ├─ Examples: split multi-file into smaller dispatches,       │
│  │   route to different terminal, add explicit pre-checks     │
│  ├─ Escalation trigger: recurs ≥2 more times at Step C       │
│  └─ VNX mechanism: recommendation_tracker.propose()           │
│                                                               │
│  Step D: "Operator Escalation" (human required)               │
│  ├─ Action: escalate to operator with full evidence chain     │
│  ├─ Contents: family key, occurrence count, all dispatch IDs, │
│  │   what Steps A-C tried, why they didn't work               │
│  ├─ Storage: .vnx-data/state/escalations/{family_key}.json   │
│  └─ VNX mechanism: open item with severity=blocker            │
│                                                               │
└───────────────────────────────────────────────────────────────┘
```

### 5.3 Escalation Data Model

```python
@dataclass
class HealingRecord:
    family_key: str                    # Normalized defect family key
    escalation_step: str               # A | B | C | D
    occurrences_total: int             # Total across all steps
    occurrences_at_step: int           # Count at current step
    first_seen: str                    # ISO 8601
    last_seen: str                     # ISO 8601
    source_dispatch_ids: List[str]     # All dispatches where this family appeared
    mitigations_tried: List[Dict]      # What was tried at each step
    current_action: str                # What's currently active
    resolved: bool                     # Has the family stopped recurring?

class HealingEngine:
    ESCALATION_THRESHOLD = 2           # Recurrences before escalating to next step

    def record_occurrence(self, family_key: str, dispatch_id: str) -> str:
        """Record a new occurrence. Returns current escalation step."""
        ...

    def check_escalation(self, family_key: str) -> Optional[str]:
        """Check if occurrences_at_step >= threshold. Returns next step or None."""
        ...

    def execute_escalation(self, family_key: str, from_step: str, to_step: str) -> None:
        """Execute the escalation action for the new step."""
        ...

    def mark_resolved(self, family_key: str) -> None:
        """Mark a family as resolved (no recurrence for 30 days)."""
        ...
```

### 5.4 Confidence Evolution with Healing Feedback

Extend the confidence model from the parent architecture doc to incorporate healing outcomes:

```python
def update_pattern_confidence(pattern, outcome: str, healing_context: Optional[HealingRecord]) -> float:
    """Update confidence considering healing escalation state."""
    base = compute_base_confidence(pattern)  # From parent doc

    # Healing bonus: patterns that prevented a known failure family get extra confidence
    if healing_context and healing_context.resolved:
        healing_bonus = 0.1  # This pattern helped resolve a recurring failure
    elif healing_context and not healing_context.resolved:
        healing_penalty = -0.05  # Pattern was tried but family still recurs
    else:
        healing_bonus = 0.0

    return min(1.0, max(0.0, base + healing_bonus))
```

### 5.5 Difference from AEF

| Aspect | AEF | VNX Adaptation |
|--------|-----|----------------|
| Escalation trigger | Manual (advisory only) | Automatic (occurrence counting) |
| Step A action | Check patterns.yaml manually | Auto-inject via IntelligenceSelector |
| Step B action | Suggest improved technique | Auto-generate prevention_rule |
| Step C action | Suggest tooling improvement | T0 recommendation (routing/scoping) |
| Step D action | Change ways of working | Operator escalation as blocker OI |
| Scope | Single agent, single repo | Multi-agent, multi-feature, cross-dispatch |
| Storage | YAML files | SQLite (antipatterns + healing_records table) |

---

## 6. Component Topology & Blast Radius

### 6.1 Current State

VNX has no component topology system. Dispatch risk is tagged manually by T0 (`risk: low | medium | high`). There is no automated analysis of which components a dispatch touches or what downstream impact a change has.

### 6.2 Proposed Adaptation

AEF's per-file component cards are too granular for VNX's dispatch-level orchestration. Instead, adapt the concept to **module-level topology** with automatic derivation from git:

```
┌─────────────────────────────────────────────────────────────────┐
│              VNX Component Topology                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Granularity: module (directory), not file                      │
│  Storage: .vnx-data/state/topology/                             │
│  Update trigger: post-commit hook or stop hook                  │
│                                                                 │
│  Module Card (.vnx-data/state/topology/modules/{module}.json):  │
│  ├─ module_id: "scripts/lib"                                    │
│  ├─ subsystem: "coordination" | "intelligence" | "delivery"     │
│  │   | "quality" | "dashboard" | "governance"                   │
│  ├─ file_count: N                                               │
│  ├─ import_dependencies: ["scripts/lib/other_module", ...]      │
│  ├─ depended_by: ["scripts/other_dir", ...]                     │
│  ├─ change_frequency: N (commits in last 30 days)               │
│  ├─ failure_rate: float (from dispatch_metadata outcomes)        │
│  └─ last_updated: ISO 8601                                      │
│                                                                 │
│  Blast Radius Calculator:                                       │
│  ├─ Input: list of changed files (from git diff)                │
│  ├─ Process: map files → modules → downstream dependencies      │
│  ├─ Output: risk_score, affected_modules, suggested_test_scope  │
│  └─ Used by: dispatch creation (auto-set risk tag),             │
│     quality pipeline (scope test execution)                     │
│                                                                 │
│  Drift Detection:                                               │
│  ├─ Unregistered: new directories not in topology               │
│  ├─ Orphaned: topology entries for deleted directories          │
│  └─ Stale: dependency edges that no longer resolve              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 6.3 Integration with Dispatch Planning

The topology feeds into three VNX subsystems:

**1. Risk tagging at dispatch creation**:
```python
def compute_dispatch_risk(changed_files: List[str], topology: ModuleTopology) -> str:
    affected = topology.blast_radius(changed_files)
    if len(affected.modules) >= 4 or any(m.subsystem == "governance" for m in affected.modules):
        return "high"
    elif len(affected.modules) >= 2 or any(m.failure_rate > 0.3 for m in affected.modules):
        return "medium"
    return "low"
```

**2. Context injection**:
When creating a dispatch that touches a high-change-frequency module, inject recent failure patterns for that module from episodic memory.

**3. Quality check scoping**:
Blast radius determines which test suites to run — if only `scripts/lib/intelligence_*.py` changed, run intelligence tests, not dashboard tests.

### 6.4 What NOT to Adopt from AEF

- **Per-file cards**: Too granular for VNX. AEF is single-agent/single-repo; VNX dispatches are at feature/module level.
- **Manual card curation**: VNX topology must be auto-derived from git and Python imports. Manual YAML cards don't scale with VNX's dispatch velocity.
- **Subsystem inference from path patterns**: Adopt this part — VNX's directory structure already maps cleanly to subsystems.

---

## 7. Runtime & Session Control Primitives

### 7.1 Current State

VNX has three runtime adapters with no unified primitive set:

| Adapter | Transport | Control | Data | Limitations |
|---------|-----------|---------|------|-------------|
| TmuxAdapter | tmux send-keys | Entangled with data | Entangled with control | No clean separation |
| HeadlessAdapter | `claude -p` subprocess | Process lifecycle only | stdout/stderr | No multiplexing |
| SubprocessAdapter | `claude -p --output-format stream-json` | Process lifecycle + dispatch metadata | NDJSON events via EventStore | Best separation, but still process-coupled |

### 7.2 TermLink-Inspired Session Primitives

Adapt TermLink's primitives to VNX's existing RuntimeAdapter interface without replacing the transport layer:

```python
class SessionPrimitives(Protocol):
    """Unified session primitives for all VNX runtime adapters.
    Inspired by TermLink's clean primitive set, adapted to VNX's
    dispatch-driven model."""

    def list_sessions(self) -> List[SessionInfo]:
        """List active terminal sessions with liveness check.
        TermLink: directory scan + kill(pid,0) + socket exists.
        VNX: query terminal_leases + worker_states tables."""
        ...

    def ping(self, terminal_id: str) -> PingResult:
        """Health check for a terminal session.
        TermLink: returns session_id, state, uptime.
        VNX: returns terminal_id, worker_state, dispatch_id, last_heartbeat."""
        ...

    def query_output(self, terminal_id: str, lines: int = 50) -> str:
        """Read recent output from a terminal.
        TermLink: scrollback ring buffer, last N lines.
        VNX: EventStore.tail() for subprocess; tmux capture-pane for tmux."""
        ...

    def execute(self, terminal_id: str, dispatch_id: str, prompt: str) -> str:
        """Deliver a dispatch to a terminal.
        TermLink: command.execute with allowlist.
        VNX: adapter.deliver() with dispatch metadata."""
        ...

    def signal(self, terminal_id: str, signal: str) -> bool:
        """Send a signal to a terminal's process.
        TermLink: command.signal (SIGTERM, SIGINT, etc.).
        VNX: process group signal via SubprocessAdapter."""
        ...

    def status(self, terminal_id: str) -> TerminalStatus:
        """Full status snapshot.
        Combines: worker_state, lease_state, heartbeat, dispatch_id,
        event_count, last_output_timestamp."""
        ...
```

### 7.3 What This Enables

1. **Dashboard operator actions**: The dashboard can call `ping()`, `query_output()`, `status()` via SSE/HTTP without knowing whether the terminal is tmux or subprocess.

2. **Hybrid runtime**: Interactive T0 (tmux) + headless workers (subprocess) accessed through the same primitive set.

3. **Future extensibility**: If VNX ever adds a new adapter (e.g., Docker container, remote SSH), it implements the same primitives.

### 7.4 What NOT to Adopt from TermLink

- **Physical socket separation**: TermLink's dual-socket (control + data) design is elegant but requires a process-per-session server. VNX's file-based coordination with SQLite is simpler and sufficient.
- **Binary frame protocol**: VNX's NDJSON events are human-readable and debuggable. Binary frames optimize bandwidth at the cost of inspectability — wrong trade-off for a governance system.
- **Hub architecture**: VNX's T0 orchestrator IS the hub. Adding a separate stateless router layer adds complexity without benefit at VNX's scale (4 terminals).
- **TOFU certificate model**: VNX is local-first. No cross-machine trust needed currently.

---

## 8. Operator Capability Model

### 8.1 Current State

VNX has no explicit capability model. All terminals are trusted at the same level. Governance is enforced by convention (T0 orchestrates, T1-T3 execute) not by permission enforcement.

### 8.2 Proposed VNX Capability Model

Adapt TermLink's 4-tier model to VNX's governance context:

```
┌─────────────────────────────────────────────────────────────────┐
│              VNX Operator Capability Tiers                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Tier 0: Observe                                                │
│  ├─ Dashboard read access (kanban, agent stream, OI list)       │
│  ├─ Query terminal status (ping, list, output tail)             │
│  ├─ Read receipts, reports, dispatch metadata                   │
│  ├─ Read intelligence DB (patterns, recommendations)            │
│  └─ Who: any stakeholder, CI/CD systems, monitoring             │
│                                                                 │
│  Tier 1: Interact                                               │
│  ├─ Everything in Observe, plus:                                │
│  ├─ Update open items (add, close, change severity)             │
│  ├─ Accept/reject recommendations                               │
│  ├─ Add comments to dispatches/receipts                         │
│  ├─ Update feature memory (decisions, risks)                    │
│  └─ Who: project team members, reviewers                        │
│                                                                 │
│  Tier 2: Control                                                │
│  ├─ Everything in Interact, plus:                               │
│  ├─ Promote dispatches (pending → active)                       │
│  ├─ Override quality gate decisions                              │
│  ├─ Release terminal leases                                     │
│  ├─ Trigger review gates manually                               │
│  ├─ Pause/resume terminals                                      │
│  └─ Who: operators (the human running VNX)                      │
│                                                                 │
│  Tier 3: Execute                                                │
│  ├─ Everything in Control, plus:                                │
│  ├─ Create/modify dispatches                                    │
│  ├─ Merge PRs / advance feature gates                           │
│  ├─ Modify governance configuration                             │
│  ├─ Delete state (receipts, dispatches, intelligence)           │
│  ├─ Modify CLAUDE.md / terminal definitions                     │
│  └─ Who: T0 orchestrator, system administrators                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 8.3 Enforcement Strategy

**Phase 1 (convention, no enforcement)**: Document tiers, add tier labels to `bin/vnx` subcommands. Log which tier each action requires. No blocking.

**Phase 2 (soft enforcement)**: Dashboard actions check tier before execution. Warn if an Observe-only client attempts a Control action. Log violations.

**Phase 3 (hard enforcement, if needed)**: Capability tokens per TermLink model. Only relevant if VNX moves to multi-user or remote access. Not needed for single-operator local use.

### 8.4 Mapping to Existing VNX Actions

| VNX Action | Current Access | Proposed Tier |
|-----------|---------------|---------------|
| `vnx status` | Anyone | Observe |
| Dashboard view | Anyone | Observe |
| `vnx open-items add` | Anyone | Interact |
| `vnx promote` | Operator | Control |
| `vnx dispatch create` | T0 | Execute |
| `vnx gate override` | Operator | Control |
| `vnx merge` | Operator | Execute |
| Release stale lease | Operator | Control |
| Modify `.vnx/config.yml` | Admin | Execute |

---

## 9. Adopt / Adapt / Ignore

### 9.1 Adopt (direct adoption into VNX)

| Pattern | Source | Rationale | Where It Lands |
|---------|--------|-----------|----------------|
| **Memory taxonomy naming** (working/feature/project/episodic) | AEF | VNX already has the pieces; formalizing the names creates shared vocabulary and clearer injection points | Memory layer definitions in architecture docs + code comments |
| **Episodic memory auto-generation from git** | AEF | The stop hook auto-report assembler already does this; AEF confirms the pattern is sound (git → challenges/artifacts/timeline) | `auto_report_assembler.py` (already proposed) |
| **Self-audit independence principle** | AEF | VNX review gates should be verifiable without depending on the dispatch pipeline they audit | Future audit tooling design principle |
| **Explicit failure modes on controls** | AEF | Each review gate definition should document how it can break (timeout, false positive, stale model, etc.) | Gate configuration / documentation |
| **Session primitives protocol** (list, ping, status, query_output) | TermLink | Unifies tmux/subprocess/headless behind one interface for dashboard and operator tooling | `SessionPrimitives` protocol on RuntimeAdapter |

### 9.2 Adapt (concept preserved, redesigned for VNX)

| Pattern | Source | VNX Adaptation | Key Difference |
|---------|--------|----------------|----------------|
| **4-step healing escalation (A→D)** | AEF | Automated escalation with occurrence counting: A=inject prevention, B=generate rule, C=T0 recommendation, D=operator blocker OI | AEF is manual/advisory; VNX automates A+B, advises C, escalates D |
| **Component topology / blast radius** | AEF | Module-level (not file-level) topology auto-derived from git + Python imports. Feeds risk tags and test scoping. | AEF uses manually curated per-file YAML cards; VNX auto-derives at module granularity |
| **4-tier capability model** (observe/interact/control/execute) | TermLink | Map to VNX operator actions; enforce progressively (convention → soft → hard) | TermLink uses HMAC tokens per-connection; VNX starts with convention-based, adds enforcement only if multi-user |
| **Control/data plane concept** | TermLink | Conceptual separation in adapter interface: control methods (deliver, signal, status) vs data methods (query_output, stream_events) — same process, different method groups | TermLink physically separates sockets; VNX separates at interface level only |
| **Pattern escalation tracking** (occurrences_at_step) | AEF | Add `escalation_step` + `occurrences_at_step` to antipatterns table. Auto-increment on recurrence. | AEF tracks but doesn't auto-escalate; VNX implements automatic threshold-based escalation |
| **Drift detection** | AEF | Detect topology drift: new modules without cards, deleted modules with stale cards, broken dependency edges | AEF checks per-file; VNX checks per-module |

### 9.3 Ignore (not applicable to VNX)

| Pattern | Source | Why Not |
|---------|--------|---------|
| **YAML-on-disk for all state** | AEF | VNX uses SQLite for coordination (concurrent writes, transactions) and NDJSON for audit trails. YAML has no concurrent write safety. |
| **Single-agent focus session** (`fw context focus T-XXX`) | AEF | VNX dispatches are explicit work units delivered to specific terminals. There is no "focus" command — dispatches ARE the focus mechanism. |
| **Ollama-based semantic recall** | AEF | Adds local LLM infrastructure dependency. FTS5 + tag filtering is sufficient for VNX's retrieval needs. Defer until evidence shows keyword search is inadequate. |
| **Binary frame protocol** | TermLink | VNX's NDJSON events are human-readable and debuggable. Governance systems need inspectability over bandwidth optimization. |
| **Dual-socket physical separation** | TermLink | VNX's file-based coordination (SQLite + NDJSON) is simpler and sufficient for 4 terminals. Socket-per-session adds process management complexity. |
| **Stateless hub router** | TermLink | T0 IS the hub. Adding a separate router layer adds complexity without benefit at VNX's terminal count. |
| **TOFU certificate model** | TermLink | VNX is local-first. No cross-machine trust boundary exists. |
| **Claude Code hook interception** (PreToolUse/PostToolUse governance) | AEF | VNX uses dispatch-level governance, not tool-level. Intercepting individual tool calls would conflict with worker autonomy within a dispatch. |
| **Plugin audit for authority-claiming language** | AEF | Novel concept but not applicable — VNX controls agent context via CLAUDE.md and dispatch prompts, not third-party plugins. |
| **Manual pattern curation** (fw healing resolve) | AEF | VNX intelligence persistence is automated from governance signals. Manual curation doesn't scale with VNX's dispatch velocity. |

---

## 10. Implications for F37+

### 10.1 F37: Memory Taxonomy + Healing Foundation

**Scope**: Formalize VNX's memory layers and implement the healing escalation engine.

**PRs**:
1. **Memory layer formalization** — Add `FeatureMemory` implementation (Layer 2). Create `.vnx-data/state/feature_memory/` storage. Wire T0 decision summarizer to update feature memory after each session. Wire `IntelligenceSelector` to inject feature memory into same-feature dispatches.

2. **Healing engine** — Add `healing_records` table to `quality_intelligence.db`. Implement `HealingEngine` class with `record_occurrence()`, `check_escalation()`, `execute_escalation()`. Wire into `intelligence_persist.py` signal processing. Implement automatic Step A (inject prevention) and Step B (generate rule).

3. **Escalation-to-OI bridge** — When healing reaches Step D, auto-create blocker open item with full evidence chain (family key, all dispatch IDs, mitigations tried at A/B/C).

### 10.2 F38: Stop Hook + Episodic Memory

**Scope**: Implement the stop hook pipeline and auto-report assembly (episodic memory generation).

**PRs**: As defined in parent architecture doc Phase 1 (stop hook + deterministic extraction, haiku classification + tag flow).

**Enhancement from this supplement**: Structure the auto-report JSON as formal episodic memory records with fields matching the taxonomy (summary, outcomes, challenges from git, artifacts, timeline).

### 10.3 F39: Component Topology + Risk Intelligence

**Scope**: Auto-derive module topology from git/imports. Feed into dispatch risk tagging.

**PRs**:
1. **Topology builder** — `scripts/lib/topology_builder.py` scans Python imports and directory structure. Generates module cards in `.vnx-data/state/topology/`. Runs as post-commit hook or on-demand.

2. **Blast radius integration** — `compute_dispatch_risk()` uses topology for automatic risk tagging at dispatch creation. Quality pipeline uses blast radius to scope test execution.

### 10.4 F40: Session Primitives + Capability Model

**Scope**: Unify runtime adapter interface with TermLink-inspired primitives. Add capability tier labels.

**PRs**:
1. **SessionPrimitives protocol** — Add `list_sessions()`, `ping()`, `query_output()`, `status()` to RuntimeAdapter base class. Implement for SubprocessAdapter and TmuxAdapter.

2. **Capability tier labels** — Document tier requirements for each `bin/vnx` subcommand. Add tier logging (which tier each action uses). Dashboard actions tagged with required tier.

### 10.5 Sequencing & Dependencies

```
F37 (Memory + Healing)
  ├─ No external dependencies
  ├─ Builds on: intelligence_selector, intelligence_persist, recommendation_tracker
  └─ Enables: F38 (episodic memory needs feature memory context)

F38 (Stop Hook + Episodic)
  ├─ Depends on: F37 (memory taxonomy for episodic structure)
  ├─ Builds on: SubprocessAdapter, EventStore, auto_report_assembler proposal
  └─ Enables: F39 (topology uses episodic data for failure_rate per module)

F39 (Topology + Risk)
  ├─ Depends on: F38 (needs episodic records for failure rate computation)
  ├─ Independent: topology builder can start before F38
  └─ Enables: F40 (topology informs which session actions are risky)

F40 (Primitives + Capabilities)
  ├─ Partially independent: primitives protocol can start anytime
  ├─ Capability tiers benefit from: F39 (topology-aware risk scoping)
  └─ Dashboard integration: depends on primitives being available
```

### 10.6 What Changes in Existing Architecture

| Existing Component | Change | Reason |
|-------------------|--------|--------|
| `IntelligenceSelector.select()` | Add Layer 2 (feature memory) injection alongside existing Layer 3 (project) | Feature context was lost between dispatches |
| `intelligence_persist.py` | Wire healing engine: after upserting antipattern, call `HealingEngine.record_occurrence()` | Automate escalation tracking |
| `dispatch_broker.register()` | Add blast radius computation: call `topology.blast_radius(changed_files)` → set risk tag | Automated risk assessment |
| `quality_advisory.py` | Use blast radius to scope which checks/tests to run | Targeted quality checks, not blanket runs |
| RuntimeAdapter base class | Add SessionPrimitives methods | Unified interface for dashboard/operator actions |
| `bin/vnx` subcommands | Add tier label metadata | Foundation for progressive capability enforcement |
| `antipatterns` table | Add columns: `escalation_step TEXT DEFAULT 'A'`, `occurrences_at_step INTEGER DEFAULT 0` | Track healing state per defect family |

---

*End of Architecture Supplement*
