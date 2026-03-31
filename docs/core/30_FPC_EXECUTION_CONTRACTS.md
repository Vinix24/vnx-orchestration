# FP-C Execution Contracts — Task Classes, Execution Targets, And Routing Invariants

**Feature**: FP-C — Execution Modes, Headless Routing, And Intelligence Quality
**PR**: PR-0
**Status**: Canonical
**Purpose**: Defines the contract surface that all FP-C implementation PRs build against. Task classes, execution target types, routing invariants, and fallback rules are frozen here.

---

## 1. Canonical Task Classes

A **task class** describes the nature of work a dispatch carries. Task classes drive execution target selection — they are not inferred from terminal IDs or skill names.

| Task Class | Description | Default Execution Target | Interactive Required | Notes |
|---|---|---|---|---|
| `coding_interactive` | Code authoring, refactoring, debugging — work requiring live terminal feedback | `interactive_tmux` | **Yes** | Default for all coding skills. Never routes headless unless explicit policy override. |
| `research_structured` | Deep analysis, architecture review, code review — structured output, no live editing | `interactive_tmux` (current), `headless_cli` (eligible) | No | Primary candidate for headless routing in FP-C. |
| `docs_synthesis` | Documentation generation, report writing, specification authoring | `interactive_tmux` (current), `headless_cli` (eligible) | No | Structured output, bounded scope. Good headless candidate. |
| `ops_watchdog` | Runtime monitoring, health checks, process supervision | `interactive_tmux` | Preferred | Requires tmux for process observation. May route headless for pure data collection. |
| `channel_response` | Responses to inbound channel events (webhook, notification, external trigger) | `channel_adapter` | No | Always enters via inbound inbox. Must become a canonical dispatch before execution. |

### Task Class Invariants

1. **Exhaustive**: Every dispatch must map to exactly one task class. Unknown work defaults to `coding_interactive`.
2. **Non-overlapping**: A dispatch cannot carry two task classes simultaneously.
3. **Immutable per dispatch**: Task class is assigned at dispatch creation and cannot change during the dispatch lifecycle.
4. **Skill-derived default**: Task class is inferred from the dispatch skill using the mapping in Section 1.1. T0 may override at creation time.

### 1.1 Skill-To-Task-Class Mapping

| Skill | Default Task Class |
|---|---|
| `backend-developer` | `coding_interactive` |
| `frontend-developer` | `coding_interactive` |
| `api-developer` | `coding_interactive` |
| `python-optimizer` | `coding_interactive` |
| `supabase-expert` | `coding_interactive` |
| `monitoring-specialist` | `coding_interactive` |
| `vnx-manager` | `coding_interactive` |
| `debugger` | `coding_interactive` |
| `architect` | `research_structured` |
| `reviewer` | `research_structured` |
| `planner` | `research_structured` |
| `data-analyst` | `research_structured` |
| `performance-profiler` | `research_structured` |
| `security-engineer` | `research_structured` |
| `t0-orchestrator` | `research_structured` |
| `test-engineer` | `coding_interactive` |
| `quality-engineer` | `coding_interactive` |
| `excel-reporter` | `docs_synthesis` |
| `technical-writer` | `docs_synthesis` |

Skills not listed default to `coding_interactive`.

---

## 2. Canonical Execution Target Types

An **execution target** is a runtime entity that can execute dispatches. Each target has a type, capability set, and health state.

| Target Type | Description | Transport | Receipt Production | CLI Agnostic |
|---|---|---|---|---|
| `interactive_tmux_claude` | Claude Code running in a tmux pane | tmux send-keys / paste-buffer | Via receipt processor monitoring pane output | No (Claude CLI) |
| `interactive_tmux_codex` | Codex CLI running in a tmux pane | tmux send-keys / paste-buffer | Via receipt processor monitoring pane output | No (Codex CLI) |
| `headless_claude_cli` | Claude Code invoked as a subprocess with `--print` or piped input | CLI subprocess spawn | Via stdout/stderr capture, structured output parsing, and normalized report projection for review-grade runs | No (Claude CLI) |
| `headless_codex_cli` | Codex CLI invoked as a subprocess | CLI subprocess spawn | Via stdout/stderr capture, structured output parsing, and normalized report projection for review-grade runs | No (Codex CLI) |
| `channel_adapter` | Adapter that translates inbound channel events into dispatches | Inbox file/queue → broker registration | Via adapter completion callback | Yes (adapter-defined) |

### Execution Target Registry Entry Schema

Each registered execution target in the runtime state has this canonical shape:

```json
{
  "target_id": "interactive_tmux_claude_T1",
  "target_type": "interactive_tmux_claude",
  "terminal_id": "T1",
  "capabilities": ["coding_interactive", "research_structured", "docs_synthesis", "ops_watchdog"],
  "health": "healthy",
  "health_checked_at": "2026-03-29T16:00:00.000Z",
  "model": "sonnet",
  "metadata": {}
}
```

### 2.1 Target Type Invariants

1. **One active target per terminal**: A terminal (T1/T2/T3) has at most one active execution target at any time.
2. **Target type is fixed per registration**: Changing a terminal from `interactive_tmux_claude` to `headless_claude_cli` requires deregistration and re-registration.
3. **Capabilities are declared, not inferred**: Each target declares which task classes it can execute. The router trusts declared capabilities.
4. **Health is queryable**: Every target must support health checks. Unhealthy targets are excluded from routing.
5. **Channel adapters are not terminals**: Channel adapters do not occupy T1/T2/T3 slots. They are registered separately.
6. **Review-grade headless runs emit report evidence**: When a headless run is used for governance review evidence, it must also emit a normalized markdown report under `$VNX_DATA_DIR/unified_reports/headless/`.

### 2.2 Target Health States

| Health State | Meaning | Routing Eligible |
|---|---|---|
| `healthy` | Target is responsive and capable | Yes |
| `degraded` | Target is responsive but may have issues (high context pressure, slow response) | Yes (with lower priority) |
| `unhealthy` | Target failed health check or is unresponsive | No |
| `offline` | Target is explicitly deregistered or shutdown | No |

---

## 3. Routing Invariants And Fallback Rules

### 3.1 Routing Decision Flow

```
dispatch.task_class
  → filter targets by capability (target.capabilities includes task_class)
  → filter by health (healthy or degraded only)
  → filter by terminal assignment (if dispatch specifies terminal_id)
  → select best target (prefer healthy over degraded, prefer interactive for coding)
  → if no target: apply fallback rules
```

### 3.2 Core Routing Invariants

| # | Invariant | Enforcement |
|---|---|---|
| R-1 | `coding_interactive` dispatches MUST route to an `interactive_tmux_*` target | Hard constraint. No fallback to headless. |
| R-2 | `research_structured` and `docs_synthesis` MAY route to `headless_*_cli` targets when available and eligible | Soft preference. Falls back to interactive if no headless target is healthy. |
| R-3 | `channel_response` dispatches MUST have entered via the inbound inbox before routing | Hard constraint. Direct routing without inbox registration is rejected. |
| R-4 | `ops_watchdog` dispatches prefer `interactive_tmux_*` targets | Soft preference. May route headless for pure data collection subtasks. |
| R-5 | A dispatch MUST NOT route to a target whose declared capabilities do not include the dispatch task class | Hard constraint. |
| R-6 | A dispatch MUST NOT route to a target with health `unhealthy` or `offline` | Hard constraint. |
| R-7 | Routing decisions MUST emit a `coordination_event` with event_type `routing_decision` | Audit requirement. |
| R-8 | T0 may override routing by specifying a `target_id` in the dispatch metadata | Override takes precedence over automatic routing, but still respects R-5 and R-6. |

### 3.3 Fallback Rules

| Condition | Fallback Behavior |
|---|---|
| No headless target available for eligible non-coding dispatch | Route to interactive tmux target (existing behavior) |
| No interactive target available for coding dispatch | Dispatch remains queued; T0 escalation after timeout |
| All targets unhealthy for a task class | Dispatch remains queued; T0 escalation emitted immediately |
| Channel adapter offline for `channel_response` | Event dead-lettered in inbox; T0 escalation |
| Target becomes unhealthy during dispatch execution | Existing dispatch continues (no mid-execution reroute); next dispatch uses updated health |

### 3.4 Routing Event Schema

Every routing decision emits a `coordination_event`:

```json
{
  "event_type": "routing_decision",
  "entity_type": "dispatch",
  "entity_id": "<dispatch_id>",
  "from_state": "queued",
  "to_state": "claimed",
  "actor": "router",
  "reason": "task_class=research_structured routed to headless_claude_cli_T2",
  "metadata_json": {
    "task_class": "research_structured",
    "selected_target_id": "headless_claude_cli_T2",
    "selected_target_type": "headless_claude_cli",
    "candidates_evaluated": 3,
    "fallback_used": false,
    "intelligence_items_attached": 2
  }
}
```

---

## 4. Dispatch Metadata Extensions

The `dispatches` table and bundle.json gain these FP-C fields:

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `task_class` | TEXT | Yes (at creation) | Derived from skill | Canonical task class from Section 1 |
| `target_type` | TEXT | No | NULL (router selects) | Explicit target type override by T0 |
| `target_id` | TEXT | No | NULL (router selects) | Explicit target ID override by T0 |
| `channel_origin` | TEXT | No | NULL | Channel identifier if dispatch originated from inbound event |
| `intelligence_payload` | TEXT (JSON) | No | NULL | Bounded intelligence items attached at creation/resume (see Intelligence Contract) |

These fields are added to the `dispatches` table via schema migration v4 and to `bundle.json` in the broker's bundle writer.

---

## 5. Cutover Strategy

### Phase 1: Shadow (PR-1, PR-2)
- Execution targets are registered in the database but routing still uses the existing tmux-only path.
- Routing decisions are logged as events but do not change delivery behavior.
- Headless targets are registered but not activated.

### Phase 2: Eligible Non-Coding (PR-3, PR-4)
- `research_structured` and `docs_synthesis` dispatches may route to headless targets when `VNX_HEADLESS_ROUTING=1`.
- Interactive fallback remains active.
- Intelligence injection is bounded and evidence-backed.

### Phase 3: Certified Cutover (PR-5)
- Mixed execution routing is the default.
- Coding stays interactive. Non-coding eligible work routes per task class rules.
- Rollback via `VNX_HEADLESS_ROUTING=0` returns to all-interactive behavior.

---

## Appendix A: Task Class JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "VNX Task Class",
  "type": "string",
  "enum": [
    "coding_interactive",
    "research_structured",
    "docs_synthesis",
    "ops_watchdog",
    "channel_response"
  ]
}
```

## Appendix B: Execution Target Registration Schema

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "title": "VNX Execution Target",
  "type": "object",
  "required": ["target_id", "target_type", "capabilities", "health"],
  "properties": {
    "target_id": {
      "type": "string",
      "description": "Unique target identifier, e.g. interactive_tmux_claude_T1"
    },
    "target_type": {
      "type": "string",
      "enum": [
        "interactive_tmux_claude",
        "interactive_tmux_codex",
        "headless_claude_cli",
        "headless_codex_cli",
        "channel_adapter"
      ]
    },
    "terminal_id": {
      "type": ["string", "null"],
      "description": "T1/T2/T3 for terminal-bound targets, null for channel adapters"
    },
    "capabilities": {
      "type": "array",
      "items": { "$ref": "#/definitions/task_class" },
      "minItems": 1,
      "description": "Task classes this target can execute"
    },
    "health": {
      "type": "string",
      "enum": ["healthy", "degraded", "unhealthy", "offline"]
    },
    "health_checked_at": {
      "type": "string",
      "format": "date-time"
    },
    "model": {
      "type": ["string", "null"],
      "description": "LLM model identifier (sonnet, opus, etc.)"
    },
    "metadata": {
      "type": "object"
    }
  },
  "definitions": {
    "task_class": {
      "type": "string",
      "enum": [
        "coding_interactive",
        "research_structured",
        "docs_synthesis",
        "ops_watchdog",
        "channel_response"
      ]
    }
  }
}
```
