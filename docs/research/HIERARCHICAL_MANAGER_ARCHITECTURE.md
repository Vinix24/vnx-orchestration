# Hierarchical Multi-Manager Architecture for VNX

**Date:** 2026-04-11  
**Status:** Research / Design  
**Audience:** VNX core contributors, system architects  
**Scope:** Evolution from flat T0→T1/T2/T3 to multi-level manager hierarchy

---

## Table of Contents

1. [Research Findings — What Already Exists](#1-research-findings)
2. [Failure Modes — What Goes Wrong at Scale](#2-failure-modes)
3. [Proposed VNX Architecture](#3-proposed-vnx-architecture)
4. [Manager State Design](#4-manager-state-design)
5. [Communication Protocol](#5-communication-protocol)
6. [Parallel Execution Model](#6-parallel-execution-model)
7. [Delegation Loop Prevention](#7-delegation-loop-prevention)
8. [Concrete Working Example](#8-concrete-working-example)
9. [Comparison with Existing Approaches](#9-comparison-with-existing-approaches)
10. [Implementation Roadmap](#10-implementation-roadmap)
11. [Risk Analysis](#11-risk-analysis)

---

## 1. Research Findings

### 1.1 Framework Survey

#### CrewAI — Hierarchical Process

CrewAI is the closest prior art. Its "hierarchical process" mode introduces a **manager LLM** that sits above a crew of worker agents. The manager receives the overall goal and decides which agent to call next via tool use.

**How it works:**
- A `manager_llm` parameter designates a model to orchestrate task decomposition
- The manager calls workers through a `Delegate Work to Coworker` tool
- Workers return text output; the manager routes to the next worker or synthesizes a final answer
- Nesting is possible: a crew can itself be a tool called by a higher-level manager

**State management:** Stateless per invocation. The manager has no persistent memory between crew runs. CrewAI's `Memory` module optionally writes to a local vector store (Chroma/Qdrant) for cross-run recall, but this is optional and the state store is shared, not per-manager.

**Feedback loops:** Linear. Worker output is returned as a string to the manager. There is no structured escalation path; the manager must parse unstructured text to detect failure.

**Scaling ceiling:** Context window. Every intermediate result is concatenated into the manager's context. In practice, crews of 5+ agents with long tasks overflow GPT-4 at 128k tokens within a single run. The framework has no context eviction strategy.

---

#### AutoGen — Group Chat and Nested Chat

Microsoft's AutoGen uses a "group chat" metaphor where multiple agents participate in a shared conversation thread. A `GroupChatManager` LLM decides who speaks next.

**How it works:**
- All agents share a single `GroupChat` conversation history
- The manager selects the next speaker by generating the agent's name as a token
- Nested chats allow one agent to spawn a sub-conversation with other agents and return the result

**State management:** The conversation history IS the state. It is held in memory and optionally written to disk. Each nested chat has its own history object, giving sub-conversations isolation, but the parent conversation accumulates all nested results.

**Feedback loops:** Implicit. Agents can "reply" to each other in the shared thread. Escalation is modeled as a special reply message ("I cannot complete this, escalating to..."). The manager LLM must recognize escalation intent from text.

**Scaling ceiling:** Group chat state grows O(n × message_length). The manager's attention degrades as the shared context fills. AutoGen 0.4 (Magentic-One) addresses this with a single orchestrator and individual agent contexts — each worker sees only its own recent history plus the orchestrator's current task assignment.

---

#### LangGraph — Subgraphs

LangGraph models agents as nodes in a directed graph. Hierarchical delegation is implemented as **subgraph nodes**: a node in the parent graph is itself a compiled graph that runs independently and returns its output as a single node result.

**How it works:**
- Parent graph: `[planner] → [feature_subgraph] → [review_subgraph] → [synthesizer]`
- `feature_subgraph` is its own compiled graph: `[decomposer] → [worker_A] → [worker_B] → [aggregator]`
- Communication: parent passes state dict to subgraph; subgraph returns updated state dict
- Interrupts and human-in-the-loop checkpoints can be placed at any node boundary

**State management:** Each graph has a `StateAnnotation` (typed dict) that persists across all node executions via a checkpointer (SQLite, Postgres, or Redis). The parent and child graphs have **separate checkpointers** — the child writes its own state; only the return value crosses the boundary.

**Feedback loops:** Explicit and typed. Subgraph return values are declared in the parent state schema. Escalation is a specific key in the return dict (e.g., `escalation: {reason, severity, context}`). The parent's routing logic reads this key to decide the next node.

**Scaling ceiling:** Graph complexity. Deeply nested subgraphs become difficult to debug because the full execution trace spans multiple checkpointer databases. LangGraph's Studio visualization tool helps but requires hosted infrastructure.

---

#### OpenAI Swarm / Agents SDK

OpenAI's Agents SDK (formerly Swarm) uses "handoffs" as the primitive for delegation. An agent can call a `transfer_to_X` function which hands control to agent X. The SDK maintains a single context that is passed from agent to agent.

**How it works:**
- Agents are Python functions with `@agent` decorator
- Handoff: calling `transfer_to_agent_B()` returns control to B with the current context
- Orchestrator pattern: a coordinator agent dispatches tasks and collects results via tool calls
- Parallel: `Runner.run()` supports concurrent agent execution with result aggregation

**State management:** Context is a Python object (dataclass or dict) mutated by each agent and passed by reference. No built-in persistence — must implement external checkpointing manually.

**Feedback loops:** Tool calls. Workers return structured tool results; the calling agent inspects the result and decides next action. Escalation requires explicit tool design.

---

### 1.2 Key Insights from the Survey

| Dimension | CrewAI | AutoGen | LangGraph | OpenAI SDK | VNX (current) |
|-----------|--------|---------|-----------|------------|----------------|
| Manager state | Stateless | Shared history | Per-graph checkpointer | In-memory object | SQLite + NDJSON |
| Feedback granularity | Unstructured text | Unstructured text | Typed dict | Tool result JSON | Structured receipt JSON |
| Parallel workers | Via crew processes | Group chat | Subgraph parallel nodes | Runner.run() | Subprocess + tmux |
| Human gates | Not built-in | Interrupt handler | Built-in checkpoint | Not built-in | Mandatory gate system |
| Delegation depth | 2 levels | Unlimited (risky) | Configurable | Unlimited | 1 level (T0→T1/T2/T3) |
| Governance | None | None | None | None | Full (codex+gemini+CI) |
| Audit trail | None | Conversation log | Checkpoint DB | None | NDJSON append-only |

**The VNX insight:** VNX already has the most mature governance, audit, and state infrastructure of any framework listed. The gap is purely structural: a flat, single-manager topology that cannot be nested.

---

## 2. Failure Modes

Every multi-manager system fails for the same 5 reasons. VNX must design against all of them from the start.

### 2.1 Context Explosion

**Symptom:** The top manager accumulates all intermediate results from all sub-managers into its context window. After 3-4 sub-manager completions, the context overflows and coherence degrades.

**Root cause:** Treating the manager's LLM context as the state store. The manager "knows" about workers by remembering their outputs in the prompt, not in a database.

**VNX mitigation:** State lives in files and SQLite, not in the manager's prompt. The manager's prompt at any given moment is constructed fresh from the state database. Manager context is bounded by the intelligence injection limit (≤3 items by default, same G-R5 rule as today).

---

### 2.2 Decision Latency Compounding

**Symptom:** A 3-level hierarchy where each level takes 30s to make a dispatch decision creates a minimum 90s latency before any worker starts. Add retry cycles and this becomes 5-10 minutes before a single line of code is written.

**Root cause:** Sequential, synchronous decision-making at each layer. The top manager waits for the feature manager, who waits for the worker.

**VNX mitigation:** Dispatch promotion is asynchronous. The top manager creates sub-manager dispatches and exits. Sub-managers run independently and report back via the receipt ledger. The top manager is only woken when a receipt arrives (trigger system). No level blocks waiting for a lower level.

---

### 2.3 Infinite Delegation Loops

**Symptom:** Manager A creates a dispatch for Manager B. Manager B, unable to complete the task, creates a dispatch back for Manager A. The system enters an infinite loop consuming tokens and producing no output.

**Root cause:** No depth tracking, no delegation budget, no cycle detection.

**VNX mitigation:** See §7. Every dispatch carries a `depth` field (0 = top manager, 1 = sub-manager, 2 = worker). Dispatch creation at depth ≥ MAX_DEPTH (configurable, default 3) is rejected. Cycle detection via parent dispatch ID chain.

---

### 2.4 State Inconsistency Under Crashes

**Symptom:** A manager subprocess crashes mid-execution. Its in-memory state is lost. The top manager has no record of what the crashed manager had already dispatched. Workers may be running orphaned tasks with no one to collect their receipts.

**Root cause:** State held in manager subprocess memory, not in a shared persistent store.

**VNX mitigation:** Manager state is externalised to files before any subprocess starts. The runtime_coordination.db records every dispatch transition. On manager restart, the reconciler reads the DB to reconstruct what was running, complete, or orphaned. Orphaned workers are detected by heartbeat expiry.

---

### 2.5 Governance Erosion at Lower Levels

**Symptom:** The top manager enforces full code review (codex + gemini + CI). Sub-managers, under time pressure or to avoid complexity, skip gates or use "minimal" governance profiles. The system ships unreviewed code.

**Root cause:** Governance is opt-in at each level rather than inherited from the parent.

**VNX mitigation:** Governance profiles are inherited downward and can only be tightened, not loosened. A sub-manager dispatching with profile "minimal" when its parent uses "default" is rejected by the dispatch broker. The governance profile is stamped on the dispatch at creation and cannot be overridden by the receiving manager.

---

## 3. Proposed VNX Architecture

### 3.1 Core Topology

```
Top Manager (T0 — strategic layer)
  │  manages: domain decomposition, PR queue, cross-domain coordination
  │  owns:    agents/top-manager/state/
  │
  ├── Feature Manager (FM-A — tactical layer)
  │     manages: engineering tasks for feature X
  │     owns:    agents/feature-manager-a/state/
  │     runs:    as headless claude -p subprocess
  │     │
  │     ├── Worker A1 (backend-developer)
  │     ├── Worker A2 (frontend-developer)
  │     └── Worker A3 (test-engineer)
  │
  ├── Content Manager (FM-B — tactical layer)
  │     manages: content production pipeline
  │     owns:    agents/content-manager/state/
  │     │
  │     ├── Worker B1 (blog-writer)
  │     └── Worker B2 (linkedin-writer)
  │
  └── Quality Manager (FM-C — tactical layer)
        manages: review gate pipeline
        owns:    agents/quality-manager/state/
        │
        ├── Worker C1 (reviewer)
        └── Worker C2 (security-engineer)
```

### 3.2 Dispatch Taxonomy

VNX dispatches now fall into three classes:

| Class | Sender | Receiver | Format |
|-------|--------|----------|--------|
| **Strategic dispatch** | Top Manager | Sub-Manager | dispatch.json with `type: "manager"` |
| **Tactical dispatch** | Sub-Manager | Worker | dispatch.json with `type: "worker"` (existing) |
| **Escalation receipt** | Sub-Manager | Top Manager | receipt.json with `escalation: {...}` |

The dispatch format is backward-compatible. Worker dispatches are unchanged. Manager dispatches add:

```json
{
  "type": "manager",
  "depth": 1,
  "parent_dispatch": "20260411-090000-top-feature-x",
  "delegation_budget": 5,
  "governance_floor": "default",
  "manager_role": "feature-manager",
  "manager_agent": "agents/feature-manager-a",
  "sub_tasks": [
    {"id": "t1", "role": "backend-developer", "description": "..."},
    {"id": "t2", "role": "frontend-developer", "description": "..."}
  ]
}
```

### 3.3 Folder Layout

```
agents/
├── top-manager/
│   ├── CLAUDE.md              # Strategic orchestration role definition
│   ├── config.yaml            # governance_profile: default, delegation_depth: 0
│   └── state/
│       ├── active_managers.json    # Which sub-managers are running
│       ├── cross_domain_oi.json    # Open items spanning multiple managers
│       └── strategic_brief.md     # Current sprint/cycle context
│
├── feature-manager-a/
│   ├── CLAUDE.md              # Tactical engineering management role
│   ├── config.yaml            # governance_profile: default, delegation_depth: 1
│   └── state/
│       ├── task_queue.json         # Pending + active worker tasks
│       ├── worker_status.json      # Last known state per worker
│       ├── decision_log.ndjson     # Append-only: each dispatch decision
│       └── open_items.json         # Feature-scoped open items
│
├── content-manager/
│   ├── CLAUDE.md
│   ├── config.yaml            # governance_profile: light
│   └── state/
│       ├── content_queue.json
│       ├── worker_status.json
│       └── decision_log.ndjson
│
├── quality-manager/
│   ├── CLAUDE.md
│   ├── config.yaml            # governance_profile: default
│   └── state/
│       ├── review_queue.json
│       ├── gate_verdicts.json
│       └── decision_log.ndjson
│
├── blog-writer/               # Existing F40 business agents (unchanged)
│   ├── CLAUDE.md
│   └── config.yaml
│
├── linkedin-writer/
│   ├── CLAUDE.md
│   └── config.yaml
│
└── orchestrator/              # Existing T0 orchestrator (unchanged for now)
    ├── CLAUDE.md
    └── config.yaml
```

---

## 4. Manager State Design

### 4.1 State Files vs SQLite

**Decision: hybrid approach.**

- **SQLite** (`runtime_coordination.db`) — already tracks all dispatch states, attempts, leases, and coordination events. Add a `manager_hierarchy` table to record parent-child relationships.
- **JSON files** (`agents/{manager}/state/`) — manager-local working state, readable by the manager's own process. Not shared with other managers.
- **NDJSON** (`decision_log.ndjson`) — append-only audit trail of every manager decision. Never overwritten.

Rationale: SQLite is the source of truth for dispatch coordination. JSON files are the manager's scratchpad, constructed from SQLite on startup and updated on every decision. If a manager crashes, the JSON files may be stale — the SQLite DB is authoritative for recovery.

### 4.2 Manager State Schema (task_queue.json)

```json
{
  "manager_id": "feature-manager-a",
  "parent_dispatch": "20260411-090000-top-feature-x",
  "depth": 1,
  "delegation_budget_remaining": 4,
  "governance_floor": "default",
  "tasks": [
    {
      "task_id": "t1",
      "role": "backend-developer",
      "description": "Implement API endpoint /api/v1/projects",
      "dispatch_id": "20260411-091500-fm-a-backend",
      "status": "running",
      "started_at": "2026-04-11T09:15:00Z",
      "receipt_path": null
    },
    {
      "task_id": "t2",
      "role": "frontend-developer",
      "description": "Build ProjectList component",
      "dispatch_id": null,
      "status": "pending",
      "started_at": null,
      "receipt_path": null
    }
  ],
  "open_items": [],
  "completion_criteria": "All tasks complete with quality gates passed",
  "last_updated": "2026-04-11T09:15:30Z"
}
```

### 4.3 Decision Log Schema (NDJSON, one record per line)

```json
{
  "ts": "2026-04-11T09:14:00Z",
  "manager_id": "feature-manager-a",
  "decision_type": "dispatch_worker",
  "task_id": "t1",
  "dispatch_id": "20260411-091500-fm-a-backend",
  "rationale": "Backend API must be complete before frontend can integrate",
  "alternatives_considered": ["parallel with frontend", "defer to next cycle"],
  "delegation_budget_before": 5,
  "delegation_budget_after": 4
}
```

### 4.4 Manager Hierarchy Table (runtime_coordination.db)

```sql
CREATE TABLE manager_hierarchy (
  manager_id TEXT PRIMARY KEY,
  parent_manager_id TEXT,
  parent_dispatch_id TEXT NOT NULL,
  depth INTEGER NOT NULL CHECK (depth BETWEEN 0 AND 3),
  governance_floor TEXT NOT NULL DEFAULT 'default',
  delegation_budget INTEGER NOT NULL DEFAULT 5,
  delegation_used INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'completed', 'failed', 'crashed')),
  created_at TEXT NOT NULL,
  completed_at TEXT,
  FOREIGN KEY (parent_dispatch_id) REFERENCES dispatches(dispatch_id)
);

CREATE TABLE dispatch_parentage (
  dispatch_id TEXT PRIMARY KEY,
  parent_dispatch_id TEXT,
  depth INTEGER NOT NULL,
  manager_id TEXT,
  FOREIGN KEY (dispatch_id) REFERENCES dispatches(dispatch_id),
  FOREIGN KEY (parent_dispatch_id) REFERENCES dispatches(dispatch_id)
);
```

### 4.5 State Recovery Protocol

When a manager subprocess is restarted after a crash:

1. **Read `runtime_coordination.db`** — query all dispatches with `parent_dispatch_id = my_parent_dispatch`. This gives the authoritative list of what was running at crash time.
2. **Reconstruct task_queue.json** — from DB dispatch states (running, completed, failed).
3. **Check heartbeats** — for dispatches marked "running", check if the worker lease is still alive. If not, mark as `timed_out`.
4. **Continue** — re-dispatch any tasks that were pending or timed out. Skip completed tasks.

The manager never trusts its own JSON files for recovery; it always re-derives state from the coordination DB.

---

## 5. Communication Protocol

### 5.1 Top Manager → Sub-Manager (Strategic Dispatch)

The top manager writes a dispatch to `.vnx-data/dispatches/pending/`. The dispatch `type` is `"manager"`. The pending queue watcher promotes it, which:

1. Registers the dispatch in `runtime_coordination.db`
2. Acquires a slot (manager slots are separate from worker slots)
3. Spawns the sub-manager subprocess: `claude -p --output-format stream-json`
4. The sub-manager process's CWD = `agents/{manager-role}/`
5. The sub-manager process reads the dispatch, updates `state/task_queue.json`, and begins issuing worker dispatches

```
Top Manager (process)
  ↓ writes
.vnx-data/dispatches/pending/20260411-090000-top-feature-x/dispatch.json
  ↓ promoted by pending watcher
.vnx-data/dispatches/active/20260411-090000-top-feature-x/dispatch.json
  ↓ spawns subprocess
subprocess: claude -p ... [agents/feature-manager-a/CLAUDE.md injected as system context]
  ↓ sub-manager reads dispatch, creates worker dispatches
.vnx-data/dispatches/pending/20260411-091500-fm-a-backend/dispatch.json
```

### 5.2 Sub-Manager → Worker (Tactical Dispatch)

Identical to today's T0 → T1/T2/T3 dispatch. The sub-manager writes a `type: "worker"` dispatch to `pending/`. The existing dispatch broker handles delivery. The only addition is that the worker dispatch carries:

```json
{
  "parent_dispatch": "20260411-090000-top-feature-x",
  "depth": 2,
  "manager_id": "feature-manager-a"
}
```

This parentage is recorded in `dispatch_parentage` table and enables the receipt to be routed back to the correct manager.

### 5.3 Worker → Sub-Manager (Receipt)

Workers write receipts exactly as today — to `.vnx-data/unified_reports/` and the NDJSON ledger. The addition: the headless trigger system checks the receipt's `manager_id` field and notifies the parent manager's inbox rather than T0's inbox.

**Manager inbox:** `agents/{manager-name}/state/inbox/`

```
.vnx-data/unified_reports/20260411-094500-A-backend.md
  → trigger detects receipt with manager_id=feature-manager-a
  → writes notification to agents/feature-manager-a/state/inbox/
      20260411-094500-receipt-notification.json
  → if sub-manager is alive: sends SIGUSR1 to trigger re-evaluation
  → if sub-manager is not alive: notification waits for next startup
```

### 5.4 Sub-Manager → Top Manager (Escalation / Completion)

When a sub-manager finishes all its tasks (or encounters a blocking issue), it writes a **manager receipt** to `.vnx-data/unified_reports/headless/`. This receipt has the same structure as a worker receipt but carries:

```json
{
  "event_type": "manager_complete",
  "manager_id": "feature-manager-a",
  "parent_dispatch": "20260411-090000-top-feature-x",
  "depth": 1,
  "summary": {
    "tasks_total": 3,
    "tasks_completed": 3,
    "tasks_failed": 0,
    "gate_status": "all_passed",
    "pr_ids": ["PR-42", "PR-43"]
  },
  "open_items": [],
  "escalation": null
}
```

If an escalation is needed:

```json
{
  "event_type": "manager_escalation",
  "escalation": {
    "severity": "blocking",
    "reason": "Backend API incompatible with existing database schema — needs architectural decision",
    "context_files": ["scripts/lib/db_schema.py"],
    "suggested_resolution": "Either migrate schema (PR-44) or change API contract"
  }
}
```

The top manager's trigger watches for `manager_complete` and `manager_escalation` events and is re-invoked to process them. This makes the top manager event-driven, not polling-driven.

### 5.5 Message Flow Diagram

```
Top Manager                Feature Manager             Worker (backend-dev)
     │                           │                           │
     │ write strategic dispatch  │                           │
     │──────────────────────────►│                           │
     │                           │ write tactical dispatch   │
     │                           │──────────────────────────►│
     │                           │                           │ [executes task]
     │                           │                           │ write report + receipt
     │                           │◄──────────────────────────│
     │                           │ [evaluates receipt]       │
     │                           │ [issues next dispatch if  │
     │                           │  more tasks remain]       │
     │                           │ write manager receipt     │
     │◄──────────────────────────│                           │
     │ [evaluates manager        │                           │
     │  receipt]                 │                           │
     │ [dispatches next          │                           │
     │  sub-manager if needed]   │                           │
```

---

## 6. Parallel Execution Model

### 6.1 Manager Slots

Today's terminal system has named slots: T1, T2, T3 (interactive) and subprocess slots keyed by dispatch_id. The hierarchical system adds **manager slots** as a separate pool.

**Manager slot pool:**
- Named by manager role: `FM-engineering`, `FM-content`, `FM-quality`
- No fixed count — the top manager decides how many sub-managers to spawn
- Each slot is a row in `manager_hierarchy` table with a subprocess PID
- Governed by `max_concurrent_managers` config (default: 4)

**Why separate from worker slots?** A manager subprocess is long-lived (minutes to hours), while a worker subprocess is task-scoped (typically 5-30 minutes). Mixing them in the same lease pool would cause starvation.

### 6.2 Concurrent Manager Execution

```
Top Manager
  ↓ spawns simultaneously
Feature Manager A ─── Worker A1 (running)
                  └── Worker A2 (running)

Content Manager B ─── Worker B1 (running)
                  └── Worker B2 (pending)

Quality Manager C ─── Worker C1 (running)
```

All three sub-managers and their workers run concurrently. The top manager is dormant between strategic dispatch creation and manager completion receipts.

### 6.3 Trigger System Extension

The existing `headless_trigger.py` watches `unified_reports/` for new receipts and wakes T0. This must be extended to:

1. Read the `manager_id` field from each new receipt
2. If `manager_id` is a sub-manager: notify that sub-manager's inbox
3. If `manager_id` is null or `top-manager`: notify T0 (existing behavior)
4. If receipt type is `manager_complete` or `manager_escalation`: always notify top manager

The trigger system becomes a **message router**, not just a T0 waker.

### 6.4 Top Manager Aggregation

The top manager maintains `state/active_managers.json`:

```json
{
  "cycle_id": "cycle-2026-04-11",
  "active_managers": [
    {
      "manager_id": "feature-manager-a",
      "dispatch_id": "20260411-090000-top-feature-x",
      "status": "running",
      "tasks_completed": 2,
      "tasks_total": 3
    },
    {
      "manager_id": "content-manager",
      "dispatch_id": "20260411-090100-top-content-q2",
      "status": "completed",
      "pr_ids": ["PR-45"]
    }
  ],
  "blocking_escalations": [],
  "ready_to_advance": false
}
```

The top manager only re-runs its decision loop when:
1. A sub-manager sends a completion receipt
2. A sub-manager sends an escalation
3. A governance gate produces a verdict that affects cross-manager dependencies
4. A manual trigger from the operator (human gate)

---

## 7. Delegation Loop Prevention

### 7.1 Depth Limit

Every dispatch carries a `depth` integer:
- 0 = issued by top manager to sub-manager
- 1 = issued by sub-manager to worker
- 2 = issued by worker to sub-worker (if permitted in future)
- MAX_DEPTH = 3 (configurable in `.vnx/config.yml`)

The dispatch broker rejects any dispatch where `depth >= MAX_DEPTH`. No exceptions.

```python
# dispatch_broker.py addition
if dispatch.get("depth", 0) >= config.MAX_DELEGATION_DEPTH:
    raise DelegationDepthExceeded(
        f"Dispatch {dispatch_id} at depth {depth} exceeds MAX_DELEGATION_DEPTH={config.MAX_DELEGATION_DEPTH}"
    )
```

### 7.2 Delegation Budget

Each manager is given a `delegation_budget` on creation (default: 10 dispatches). This is a hard ceiling on how many sub-dispatches a single manager can create in its lifetime.

```sql
UPDATE manager_hierarchy
SET delegation_used = delegation_used + 1
WHERE manager_id = ?;

-- Reject if delegation_used >= delegation_budget
```

Budget is inherited and bounded: if the top manager allocates budget 10 to sub-manager A, sub-manager A cannot allocate more than 10 dispatches total across all its workers.

### 7.3 Cycle Detection

Parent dispatch IDs form a chain. Before creating a new dispatch, the broker walks the chain:

```
new dispatch → parent_dispatch_id=X → X.parent_dispatch_id=Y → Y.parent_dispatch_id=Z
```

If any ancestor's `manager_id` equals the current manager's ID, a cycle is detected and the dispatch is rejected.

**Performance:** The chain is at most `MAX_DEPTH` (3) hops. This is a O(MAX_DEPTH) lookup — negligible overhead.

### 7.4 Time Budget

Each manager has a `deadline` set at creation (default: 2 hours). If a manager's process is still running after the deadline:
1. The trigger system sends a soft SIGUSR1 (checkpoint signal)
2. If not completed within 15 minutes of the soft signal, the manager receives SIGTERM
3. An escalation receipt is auto-generated and sent to the parent manager
4. Any running workers under this manager are allowed to complete their current task but receive no new dispatches

### 7.5 Budget Summary

| Safeguard | Mechanism | Default |
|-----------|-----------|---------|
| Depth limit | Dispatch broker rejects `depth >= MAX_DEPTH` | 3 |
| Delegation budget | Manager-level counter in coordination DB | 10 dispatches |
| Cycle detection | Parent-chain walk before dispatch creation | Any cycle |
| Time budget | Deadline + SIGTERM escalation | 2 hours |
| Context budget | Intelligence injection bounded | ≤3 items |

---

## 8. Concrete Working Example

### 8.1 Use Case: SEO Crawler v3 Feature Sprint

**Goal:** The Product Manager (top manager) needs to ship three things in parallel:
1. A new crawl scheduler (engineering)
2. A launch blog post (content)
3. A security audit of the scheduler (quality)

### 8.2 Folder Structure

```
agents/
├── product-manager/
│   ├── CLAUDE.md
│   ├── config.yaml
│   └── state/
│       ├── active_managers.json
│       ├── strategic_brief.md
│       └── decision_log.ndjson
│
├── engineering-manager/
│   ├── CLAUDE.md
│   ├── config.yaml                  # depth: 1, governance_floor: default
│   └── state/
│       ├── task_queue.json
│       ├── worker_status.json
│       ├── open_items.json
│       └── decision_log.ndjson
│       └── inbox/                   # receipt notifications from trigger
│
├── content-manager/
│   ├── CLAUDE.md
│   ├── config.yaml                  # depth: 1, governance_floor: light
│   └── state/
│       ├── task_queue.json
│       └── decision_log.ndjson
│       └── inbox/
│
├── quality-manager/
│   ├── CLAUDE.md
│   ├── config.yaml                  # depth: 1, governance_floor: default
│   └── state/
│       ├── review_queue.json
│       └── decision_log.ndjson
│       └── inbox/
│
├── blog-writer/           # existing
│   └── CLAUDE.md
├── linkedin-writer/       # existing
│   └── CLAUDE.md
├── backend-developer/     # existing (symlinked from .claude/skills/)
│   └── CLAUDE.md
├── test-engineer/         # existing
│   └── CLAUDE.md
├── reviewer/              # existing
│   └── CLAUDE.md
└── security-engineer/     # existing
    └── CLAUDE.md
```

### 8.3 CLAUDE.md Patterns

#### product-manager/CLAUDE.md
```markdown
# Product Manager — Strategic Orchestrator

You are the top-level orchestrator for the SEO Crawler sprint.

## Your Role
- Decompose sprint goals into parallel sub-manager dispatches
- Monitor sub-manager completion receipts
- Resolve cross-domain escalations
- Do NOT write code. Do NOT create worker dispatches directly.
- All implementation goes through sub-managers.

## Available Sub-Manager Roles
- engineering-manager: backend, frontend, test workers
- content-manager: blog-writer, linkedin-writer workers
- quality-manager: reviewer, security-engineer workers

## Dispatch Protocol
Write sub-manager dispatches to .vnx-data/dispatches/pending/
Use type="manager", depth=0.

## Receipt Protocol
Read receipts from agents/product-manager/state/inbox/
Update agents/product-manager/state/active_managers.json after each receipt.

## Escalation Protocol
If a sub-manager escalates a blocking issue, write it to state/blocking_escalations.json
and notify the human operator via a governance signal.

## Governance
governance_floor: default — sub-managers may NOT use a lighter profile than "default"
```

#### engineering-manager/CLAUDE.md
```markdown
# Engineering Manager — Tactical Coordinator

You manage engineering workers for a specific feature or PR.

## Your Role
- Decompose the feature plan into worker tasks (backend, frontend, test)
- Dispatch tasks in dependency order (backend first, then frontend, then test)
- Review worker receipts and decide: approve / request-revision / escalate
- Write completion receipt when all tasks pass quality gates

## Available Worker Roles
- backend-developer: Python/TypeScript API implementation
- frontend-developer: React/Next.js component implementation
- test-engineer: pytest + Playwright test suites

## Dispatch Protocol
Write worker dispatches to .vnx-data/dispatches/pending/
Use type="worker", depth=1, manager_id="engineering-manager".

## Receipt Protocol
Read receipts from agents/engineering-manager/state/inbox/
For each receipt:
  - If status=success AND gate=passed: mark task complete
  - If status=failed: increment retry count; re-dispatch with amended instruction if retry < 2
  - If retry >= 2: escalate to product-manager

## State Files
- task_queue.json: current task states
- decision_log.ndjson: append-only decision audit

## Completion Criteria
All tasks complete + all governance gates passed.
Write manager receipt to .vnx-data/unified_reports/headless/
```

#### config.yaml pattern
```yaml
# agents/engineering-manager/config.yaml
agent_id: engineering-manager
depth: 1
governance_profile: default      # inherits from parent; cannot be loosened
governance_floor: default        # minimum profile for any sub-dispatch
delegation_budget: 10            # max worker dispatches this manager can create
delegation_deadline_hours: 2
allowed_paths:
  - src/
  - tests/
  - scripts/lib/
denied_paths:
  - .vnx/
  - .claude/
  - agents/
```

### 8.4 Step-by-Step Message Flow

**Step 1: Operator triggers Product Manager**
```
operator → manual trigger
  → spawns: claude -p [agents/product-manager/CLAUDE.md injected]
  → product-manager reads: state/strategic_brief.md (sprint goals)
```

**Step 2: Product Manager creates three parallel strategic dispatches**
```
.vnx-data/dispatches/pending/
  20260411-090000-pm-engineering-scheduler/dispatch.json
  20260411-090001-pm-content-blog/dispatch.json
  20260411-090002-pm-quality-scheduler/dispatch.json
```

Each dispatch has `type: "manager"`, `depth: 0`.

Product Manager updates:
```
agents/product-manager/state/active_managers.json
agents/product-manager/state/decision_log.ndjson (3 entries)
```

Product Manager subprocess exits.

**Step 3: Pending watcher promotes all three dispatches simultaneously**
```
→ spawns: engineering-manager subprocess (reads strategic dispatch)
→ spawns: content-manager subprocess (reads strategic dispatch)
→ spawns: quality-manager subprocess (reads strategic dispatch)
```

All three run concurrently as separate `claude -p` processes.

**Step 4: Engineering Manager decomposes tasks**

Engineering Manager reads its strategic dispatch, creates:
```
.vnx-data/dispatches/pending/
  20260411-091000-em-backend-scheduler/dispatch.json   # type: worker, depth: 1
```

Updates `agents/engineering-manager/state/task_queue.json`:
```json
{
  "tasks": [
    {"task_id": "backend", "status": "dispatched", "dispatch_id": "20260411-091000-em-backend-scheduler"},
    {"task_id": "frontend", "status": "pending"},
    {"task_id": "test", "status": "pending"}
  ]
}
```

Engineering Manager subprocess pauses (waits for inbox notification via SIGUSR1 or periodic check).

**Step 5: Backend worker executes**
```
backend-developer subprocess: reads dispatch, implements crawl scheduler API
  → writes: .vnx-data/unified_reports/20260411-093000-A-backend.md
  → writes: receipt to t0_receipts.ndjson
  → codex gate + gemini review run
  → receipt updated with gate verdicts
```

**Step 6: Trigger routes receipt to Engineering Manager**
```
headless_trigger.py detects new receipt with manager_id="engineering-manager"
  → writes: agents/engineering-manager/state/inbox/20260411-093000-receipt-notification.json
  → sends SIGUSR1 to engineering-manager subprocess (if still alive)
  → if not alive: engineering-manager is re-spawned with recovery context
```

**Step 7: Engineering Manager processes receipt**
```
Engineering Manager reads inbox:
  - backend task: success, gates passed
  → marks backend task complete in task_queue.json
  → creates frontend dispatch: .vnx-data/dispatches/pending/20260411-093100-em-frontend/
  → logs decision to decision_log.ndjson
```

**Step 8: (Simultaneously) Content Manager executes blog task**
```
content-manager subprocess creates:
  .vnx-data/dispatches/pending/20260411-091100-cm-blog/dispatch.json
  
blog-writer subprocess executes:
  → writes blog post to agents/blog-writer/output/
  → writes receipt to unified_reports/
  
headless_trigger routes receipt to content-manager inbox
content-manager marks blog complete, creates linkedin dispatch
```

**Step 9: Engineering Manager reaches completion**
```
All 3 tasks (backend, frontend, test) complete with gates passed.
Engineering Manager writes:
  .vnx-data/unified_reports/headless/20260411-101500-manager-engineering.md
  
  {
    "event_type": "manager_complete",
    "manager_id": "engineering-manager",
    "parent_dispatch": "20260411-090000-pm-engineering-scheduler",
    "summary": {"tasks_total": 3, "tasks_completed": 3, "tasks_failed": 0},
    "pr_ids": ["PR-51"]
  }
```

**Step 10: Trigger routes manager receipt to Product Manager**
```
headless_trigger detects manager_complete event
  → writes to agents/product-manager/state/inbox/
  → re-spawns product-manager subprocess with recovery context
  
Product Manager:
  → reads active_managers.json (engineering: complete, content: running, quality: running)
  → updates state
  → waits for remaining manager completions
```

**Step 11: Final synthesis**
```
All three managers complete.
Product Manager:
  → reads all manager receipts from inbox
  → synthesizes sprint summary in state/strategic_brief.md
  → writes final governance signal to .vnx-data/state/t0_brief.json
  → exits
```

---

## 9. Comparison with Existing Approaches

### 9.1 What VNX Hierarchical Does Differently

**vs. CrewAI Hierarchical Process:**
- CrewAI manager is stateless; VNX manager persists state to files + DB
- CrewAI has no governance gates; VNX mandates codex + gemini + CI
- CrewAI accumulates all results in manager context; VNX uses bounded injection (≤3 items)
- CrewAI has no human gates; VNX requires promotion approval at every level
- CrewAI uses Python classes; VNX uses file-based dispatch (language-agnostic)

**vs. AutoGen Group Chat:**
- AutoGen shares one history across all agents; VNX gives each manager isolated state
- AutoGen has no structured escalation; VNX has typed `escalation` receipts
- AutoGen's manager selection is LLM text generation; VNX routing is deterministic file-based
- AutoGen has no governance; VNX enforces governance floor inheritance

**vs. LangGraph Subgraphs:**
- LangGraph requires Python graph compilation; VNX is pure file + subprocess
- LangGraph subgraphs are synchronous within a run; VNX sub-managers are long-lived processes
- LangGraph checkpointing is per-run; VNX persistence is per-coordination-event (NDJSON append)
- LangGraph has strong graph visualization; VNX has the dashboard (TODO: extend for hierarchy)

**vs. OpenAI Agents SDK:**
- OpenAI SDK requires SDK; VNX uses `claude -p` CLI only (no SDK dependency)
- OpenAI SDK has no governance; VNX mandates gates
- OpenAI SDK context is in-process object; VNX state is external files

### 9.2 What VNX Inherits from Prior Art

- **Subgraph isolation** (LangGraph): each manager has isolated state, only return values cross boundaries
- **Parallel execution** (OpenAI Swarm): multiple managers run as concurrent subprocesses
- **Typed escalation** (LangGraph): escalation is a structured field, not free text
- **Budget management** (None — VNX innovation): delegation budget and deadline enforcement not found in any surveyed framework
- **Governance inheritance** (None — VNX innovation): governance floor inherited from parent, cannot be loosened

---

## 10. Implementation Roadmap

### Phase 1: Infrastructure (Estimated: 3 PRs, ~150 LOC each)

**PR 1 — Dispatch taxonomy extension**
- Add `type` field to dispatch schema (`"worker"` default, `"manager"` new)
- Add `depth`, `parent_dispatch`, `manager_id`, `delegation_budget`, `governance_floor` fields
- Update dispatch broker to enforce depth limit and cycle detection
- Update `dispatch_parentage` table in coordination DB
- Files: `scripts/lib/dispatch_broker.py`, `schemas/runtime_coordination.sql`

**PR 2 — Manager hierarchy table**
- Add `manager_hierarchy` table to coordination DB
- Add `dispatch_parentage` table
- Add `LeaseManager` extension for manager-type leases (separate from T1/T2/T3 worker slots)
- Add `max_concurrent_managers` config to `.vnx/config.yml`
- Files: `schemas/runtime_coordination.sql`, `scripts/lib/lease_manager.py`, `scripts/lib/coordination_db.py`

**PR 3 — Manager inbox + trigger routing**
- Extend `headless_trigger.py` to route receipts to `agents/{manager}/state/inbox/` based on `manager_id` field
- Implement SIGUSR1 delivery to running manager subprocess
- Implement manager receipt types: `manager_complete`, `manager_escalation`
- Files: `scripts/headless_trigger.py`, `scripts/lib/subprocess_adapter.py`

### Phase 2: Manager Runtime (Estimated: 2 PRs, ~200 LOC each)

**PR 4 — Manager subprocess lifecycle**
- Extend `subprocess_dispatch.py` to handle `type: "manager"` dispatches
- Implement manager spawn with CWD = `agents/{manager-role}/`
- Implement manager heartbeat (longer interval: 600s for long-lived managers)
- Implement manager receipt writing on completion/escalation
- Implement state recovery (read coordination DB on startup)
- Files: `scripts/lib/subprocess_dispatch.py`, `scripts/lib/subprocess_adapter.py`

**PR 5 — Manager agent templates**
- Create `agents/engineering-manager/CLAUDE.md` with full role definition
- Create `agents/content-manager/CLAUDE.md`
- Create `agents/quality-manager/CLAUDE.md`
- Create `config.yaml` for each with governance constraints
- Create state directory structure (`state/`, `state/inbox/`)
- Files: `agents/*/CLAUDE.md`, `agents/*/config.yaml`

### Phase 3: Top Manager and Integration (Estimated: 2 PRs, ~150 LOC each)

**PR 6 — Top manager (product-manager) upgrade**
- Upgrade `agents/orchestrator/` (or create `agents/product-manager/`) to strategic role
- Integrate with active_managers.json state
- Implement cross-domain open item tracking
- Add strategic brief generation from sub-manager receipts
- Files: `agents/product-manager/CLAUDE.md`, `scripts/generate_t0_brief.sh` extension

**PR 7 — Dashboard extension for hierarchy**
- Add manager hierarchy view to dashboard
- Show: top manager → sub-managers → workers as tree
- Show: delegation budget consumption, depth indicators
- Add escalation panel (blocking escalations visible to operator)
- Files: `dashboard/` (new component)

### Phase 4: Hardening (Estimated: 1 PR, ~100 LOC)

**PR 8 — Delegation budget enforcement + dead-letter queue**
- Implement hard delegation budget enforcement in broker
- Add manager deadline enforcement with auto-escalation
- Add dead-letter queue for failed manager dispatches
- Add reconciler extension for crashed managers
- Files: `scripts/lib/dispatch_broker.py`, `scripts/lib/runtime_reconciler.py`

### Ordering Rationale

The roadmap is sequenced so that:
1. Phases 1-2 can be developed independently on separate branches
2. Each PR is below 300 LOC (existing governance constraint)
3. No PR depends on a PR from the same phase (within-phase parallelism)
4. Phase 3 requires Phase 1+2 complete (integration depends on infrastructure)
5. Phase 4 is polish and can begin once Phase 2 is merged

---

## 11. Risk Analysis

### 11.1 Context Accumulation in Top Manager

**Risk:** The top manager aggregates receipts from all sub-managers. Over a large sprint (10+ sub-managers), the top manager's context at decision time could be very large.

**Mitigation:** Apply the same ≤3 item intelligence injection rule to manager receipts. The top manager sees: (a) the 3 most recent manager receipts, (b) any blocking escalations, (c) the current strategic brief. It does NOT receive the full text of all sub-manager work. Sub-manager detail is in the report files on disk — the top manager reads those files only when needed via tool call, not injected automatically.

**Residual risk:** Low. The pattern is proven by the existing G-R5 bounded intelligence rule.

---

### 11.2 Manager Subprocess Lifecycle Complexity

**Risk:** Long-lived manager subprocesses (2+ hours) are harder to manage than short-lived worker subprocesses (5-30 minutes). The heartbeat system was designed for short workers; a manager heartbeat at 300s would generate excessive renewal overhead.

**Mitigation:** Manager heartbeat interval = 600s (configurable, separate from worker heartbeat). Manager lease TTL = 4 hours (configurable). The manager is not blocked waiting for workers — it pauses after dispatching and resumes on SIGUSR1 or periodic inbox check (every 120s).

**Residual risk:** Medium. Long-lived processes increase the probability of encountering system-level interruptions (OOM, network loss, host restart). The crash recovery protocol (§4.5) must be exercised in staging before production use.

---

### 11.3 Governance Floor Bypass

**Risk:** A sub-manager, when writing worker dispatches, could omit the `governance_floor` field or set it to `"minimal"`, bypassing the inherited governance constraint.

**Mitigation:** The dispatch broker enforces: if a dispatch has `parent_dispatch` → look up the parent's `governance_floor` in `manager_hierarchy` → reject if the new dispatch's profile is lighter than the floor. This is not an advisory — it is a hard rejection. The only exception is if the operator explicitly sets `governance_floor: "minimal"` on the top-level manager (which is a conscious choice, not a bypass).

**Residual risk:** Low. The broker is the choke point for all dispatches; bypassing it is not possible without modifying core infrastructure.

---

### 11.4 Deadlock Between Co-Dependent Managers

**Risk:** Engineering Manager waits for a shared library that Content Manager is supposed to document. Content Manager waits for the Engineering Manager to finish the library before writing the documentation. Neither can proceed.

**Mitigation:** The top manager is responsible for identifying cross-manager dependencies at dispatch creation time. If A depends on B, the top manager dispatches A only after B sends a completion receipt. The top manager does NOT dispatch A and B simultaneously if A depends on B. Cross-manager dependency graphs are recorded in `state/strategic_brief.md`.

**Residual risk:** Medium. Dependency detection requires the top manager's CLAUDE.md to explicitly reason about cross-domain dependencies. If the reasoning is wrong, a deadlock can occur. The time budget (§7.4) ensures this deadlock is caught and escalated to the operator within the configured deadline.

---

### 11.5 Sub-Manager Autonomy Drift

**Risk:** Sub-managers, making tactical decisions independently, may drift from the strategic direction. A feature manager might decide to completely redesign a module that the product manager intended to be a minor patch.

**Mitigation:** The strategic dispatch from the top manager includes explicit `completion_criteria` and `scope_constraints`. The sub-manager's CLAUDE.md instructs it to escalate if a task would require changes outside the declared scope. Scope is enforced by the `allowed_paths` / `denied_paths` in the manager's `config.yaml`.

**Residual risk:** Low. The existing folder scope isolation (already proven in business agent work) prevents out-of-scope file access at the filesystem level.

---

### 11.6 Receipt Routing Ambiguity

**Risk:** A worker produces a receipt with an incorrect or missing `manager_id`. The trigger system cannot route it; the receipt is delivered to T0 instead of the parent sub-manager. The sub-manager waits indefinitely.

**Mitigation:** Worker dispatches created by a sub-manager MUST include `manager_id`. The dispatch broker validates this field on creation (not optionally). The trigger system has a fallback: if `manager_id` is invalid (no matching agent directory), the receipt is routed to T0 with a `routing_error` flag. T0 can then inspect and manually re-route or cancel.

**Residual risk:** Low. Broker-level validation at dispatch creation prevents the most common source of this error.

---

### 11.7 Tooling Debt

**Risk:** Hierarchical execution is significantly more complex to debug than the current flat model. When a sprint fails, the operator must trace: top manager decision log → sub-manager decision log → worker receipt → gate verdict. This is 4 layers of logs across 4+ directories.

**Mitigation:** Phase 3 PR 7 (dashboard extension) adds a unified trace view: for any dispatch, show the full ancestor chain and all descendant dispatches and receipts. The NDJSON audit trail already exists; the dashboard just needs to index it by parent_dispatch chain.

**Residual risk:** Medium. The dashboard extension is Phase 3, meaning there will be a period where the hierarchy is operational but the trace tooling is not yet complete. Document this gap clearly in the operator runbook.

---

## Appendix A: Dispatch Schema v2

Full JSON schema for hierarchical dispatches:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "VNX Dispatch v2",
  "type": "object",
  "required": ["dispatch_id", "type", "depth", "role", "instruction", "created_at"],
  "properties": {
    "dispatch_id": { "type": "string", "pattern": "^[0-9]{8}-[0-9]{6}-[a-z0-9-]+$" },
    "type": { "type": "string", "enum": ["worker", "manager"] },
    "depth": { "type": "integer", "minimum": 0, "maximum": 3 },
    "parent_dispatch": { "type": ["string", "null"] },
    "manager_id": { "type": ["string", "null"] },
    "terminal": { "type": "string" },
    "track": { "type": "string" },
    "role": { "type": "string" },
    "skill_name": { "type": "string" },
    "gate": { "type": "string" },
    "cognition": { "type": "string", "enum": ["high", "medium", "low"] },
    "priority": { "type": "string", "enum": ["P0", "P1", "P2", "P3"] },
    "pr_id": { "type": ["string", "null"] },
    "feature": { "type": ["string", "null"] },
    "branch": { "type": ["string", "null"] },
    "reason": { "type": "string" },
    "instruction": { "type": "string" },
    "context_files": { "type": "array", "items": { "type": "string" } },
    "delegation_budget": { "type": ["integer", "null"], "minimum": 1, "maximum": 20 },
    "governance_floor": { "type": ["string", "null"], "enum": ["default", "light", "minimal", null] },
    "sub_tasks": {
      "type": ["array", "null"],
      "items": {
        "type": "object",
        "properties": {
          "id": { "type": "string" },
          "role": { "type": "string" },
          "description": { "type": "string" },
          "depends_on": { "type": "array", "items": { "type": "string" } }
        }
      }
    },
    "created_at": { "type": "string", "format": "date-time" }
  }
}
```

---

## Appendix B: Manager Receipt Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "VNX Manager Receipt",
  "type": "object",
  "required": ["event_type", "manager_id", "parent_dispatch", "depth", "timestamp"],
  "properties": {
    "event_type": { "type": "string", "enum": ["manager_complete", "manager_escalation"] },
    "timestamp": { "type": "string", "format": "date-time" },
    "manager_id": { "type": "string" },
    "parent_dispatch": { "type": "string" },
    "depth": { "type": "integer" },
    "summary": {
      "type": "object",
      "properties": {
        "tasks_total": { "type": "integer" },
        "tasks_completed": { "type": "integer" },
        "tasks_failed": { "type": "integer" },
        "gate_status": { "type": "string" },
        "pr_ids": { "type": "array", "items": { "type": "string" } },
        "delegation_budget_used": { "type": "integer" }
      }
    },
    "open_items": { "type": "array" },
    "escalation": {
      "type": ["object", "null"],
      "properties": {
        "severity": { "type": "string", "enum": ["blocking", "warning", "info"] },
        "reason": { "type": "string" },
        "context_files": { "type": "array", "items": { "type": "string" } },
        "suggested_resolution": { "type": "string" }
      }
    }
  }
}
```

---

*End of document. Total: ~750 lines.*
