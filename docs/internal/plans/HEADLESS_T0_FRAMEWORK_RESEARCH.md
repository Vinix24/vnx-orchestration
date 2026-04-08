# Headless T0 Orchestrator — External Framework Research

**Dispatch ID**: 20260407-050001-f36-headless-t0-frameworks-A
**PR**: F36
**Track**: A
**Gate**: planning
**Date**: 2026-04-07

---

## Executive Summary

This document surveys how leading agent orchestration frameworks (LangGraph, CrewAI, OpenAI Agents SDK, AutoGen/AG2, Mastra, Claude Agent SDK, n8n) solve stateful multi-agent orchestration without persistent interactive sessions. The findings directly inform VNX's headless T0 design.

**Key finding**: Every mature framework converges on the same three principles:
1. **Externalize state** — all decision context lives in a durable store, not in-process memory
2. **Thread/session identity** — a persistent ID ties disparate invocations into a logical session
3. **Snapshot-resume, not replay** — resumption loads the latest state snapshot; it does not re-execute prior steps

VNX's existing architecture is well-aligned with these principles. The primary gap is a polling/event loop and a decision log — not a fundamental redesign.

---

## Framework Comparison Table

| Framework | State Pattern | Session Model | Event Trigger | Token Strategy |
|---|---|---|---|---|
| **LangGraph** | Thread-keyed checkpoints (SQLite/Postgres/Redis) | Persistent logical thread via `thread_id` | Caller drives: `graph.invoke(config)` with same `thread_id` | Manual: `trim_messages`, summarize-then-checkpoint nodes |
| **CrewAI** | `@persist` + SQLite Flow state; in-memory within `kickoff()` | Flow state UUID; stateless by default within individual Crew | `kickoff()` call; event-based via Flows `@listen()` decorator | Structured output (`output_pydantic`) for compact task results |
| **OpenAI Agents SDK** | `RunState` serialization OR `Session` object (InMemory/OpenAI Conversations API) | Client-managed `Session` or server-managed `conversation_id` | Caller invokes `Runner.run()` with same `Session` | `handoff_history_mapper` compresses history at agent boundaries; Session auto-compaction |
| **AutoGen / AG2** | Manual JSON export of `GroupChat.messages` + `resume()` | No native persistence; DIY JSON serialization | Caller calls `manager.resume(messages=json.dumps(history))` | `TransformMessages`: `MessageHistoryLimiter(N)`, `MessageTokenLimiter(T)` |
| **Mastra** | Storage-backed `Memory` (LibSQL/Postgres/Redis); workflow `suspend()` snapshots | `resourceId` + `threadId` in Memory API | Workflow `resume(runId, payload)` call | Auto-compression at ~30k tokens; semantic recall (top-K retrieval) |
| **Claude Agent SDK** | JSONL session files on disk (`~/.claude/projects/<cwd>/*.jsonl`) | Disk-persisted session with UUID; `--resume <id>` for headless | `claude -p --resume <id>` call | Session compaction (`/compact`); `--max-turns N` guard |
| **n8n AI Agents** | External DB required (Postgres/Redis); in-process only within single execution | No native session — stateless execution model | Webhook trigger / schedule trigger / sub-workflow return | Window Buffer Memory (`contextWindowLength`); Summary Memory (auto-summarize) |

---

## Framework Deep-Dives

### 1. LangGraph — Checkpoint-First Durability

**State pattern**: Every node transition writes a `StateSnapshot` to the configured checkpointer backend. The snapshot contains: full state dict, pending node list, interrupt data, step metadata.

**Resume mechanics**:
```python
config = {"configurable": {"thread_id": "f36-orchestrator-session-1"}}
# Initial invocation
result = graph.invoke({"messages": [...]}, config)

# Later — fresh process, same thread_id resumes from last checkpoint
result = graph.invoke(None, config)  # None = resume from where we left off
```

**Key insight for VNX**: LangGraph's `thread_id` is functionally identical to VNX's session ID concept. The checkpointer is the "state directory". VNX already has an external state directory (`.vnx-data/`); adding a `thread_id` key to that store completes the pattern.

**Human-in-the-loop** via `interrupt()`:
```python
def review_node(state):
    decision = interrupt({
        "question": "Approve this dispatch?",
        "dispatch": state["pending_dispatch"],
    })
    return {"approved": decision["approved"]}

# Resume after human acts:
result = graph.invoke(Command(resume={"approved": True}), config)
```

The `interrupt()` → `Command(resume=...)` cycle is a direct analogue for VNX's human approval gate. The checkpoint stores the interrupted state; resumption passes the operator's decision.

**Context overflow**: No auto-handling. Recommended pattern is a "summarize" node that compresses `messages` into a running summary and saves that summary to the checkpoint instead of full history.

**Best practice for VNX**: Add a `t0_context_summary` field to VNX state that T0 compresses after each N receipts. This field becomes the rolling context injection for headless invocations.

---

### 2. CrewAI — Flow Persistence at Method Boundaries

**State pattern**: CrewAI `Flows` add SQLite-backed persistence on top of stateless `Crew.kickoff()` calls. The `@persist` decorator serializes `self.state` (a Pydantic model) after each decorated method completes.

```python
@persist
class OrchestratorFlow(Flow):
    @start()
    def process_receipt(self):
        receipt = self.state.pending_receipt
        result = analysis_crew.kickoff(inputs={"receipt": receipt})
        self.state.decisions.append(result.raw)
        return result.raw

    @listen(process_receipt)
    def dispatch_next(self):
        # State persisted from process_receipt is available here
        decision = self.state.decisions[-1]
        create_dispatch(decision)
```

**Key limitation**: Persistence is at method boundaries only. If a `kickoff()` call crashes mid-task, CrewAI cannot resume — it re-runs the whole method.

**Best practice for VNX**: CrewAI's method-boundary persistence maps well to VNX's dispatch lifecycle phases (analyze-receipt → create-dispatch → verify-gate → close-items). Each phase can be a `@listen` method with its own checkpoint. This is coarser than LangGraph but sufficient for VNX's decision frequency.

---

### 3. OpenAI Agents SDK — Session Objects + RunState

**State pattern**: Two orthogonal mechanisms:

**(A) Session object** (for multi-turn continuity):
```python
session = InMemorySession()  # or OpenAIConversationsSession("conv_abc123")
result = await Runner.run(agent, "Review receipt 47", session=session)
result = await Runner.run(agent, "Now dispatch T1", session=session)
# Session auto-accumulates history across calls
```

**(B) RunState serialization** (for interrupt/resume within a single run):
```python
# Interrupted mid-run for human approval
serialized = result.state.model_dump_json()

# Later, with human's decision:
state = RunState.model_validate_json(serialized)
result = await Runner.run(agent, None, state=state)
```

**Context passing**: Two completely separate channels:
- `RunContextWrapper.context` — typed Python object for app state (DB handles, config, accumulated data). This is a side-channel that does NOT go through the LLM. Zero token cost.
- Conversation history — the message thread. Filtered at handoff boundaries via `input_filter`.

**Key insight for VNX**: The `RunContextWrapper.context` pattern maps directly to VNX's externalized state files. T0 can load `.vnx-data/` state into a typed Python dataclass before each invocation and pass it as context — the LLM reasons over a compact state representation, not raw file dumps.

**Production durability**: OpenAI + Temporal integration (GA March 2026) wraps agent loops in Temporal Workflows. Each tool call is a Temporal Activity. State persists in Temporal's event log. Auto-retry + crash recovery from the failure point. **Most relevant for enterprise VNX deployments.**

---

### 4. AutoGen / AG2 — Minimal Native Persistence

**State pattern**: No built-in persistence between process invocations. State is serialized manually:
```python
# Save
history = chat_result.chat_history  # list of dicts, JSON-serializable

# Restore
manager.resume(messages=json.dumps(history))
```

**Context overflow** via `TransformMessages`:
```python
transforms = TransformMessages(transforms=[
    MessageHistoryLimiter(max_messages=20),
    MessageTokenLimiter(max_tokens=4000, model="gpt-4o")
])
transforms.add_to_agent(orchestrator_agent)
```

**Key insight for VNX**: AG2's `TransformMessages` is a clean primitive for implementing the rolling-window context injection VNX needs. Apply `MessageHistoryLimiter` to keep T0's context bounded, regardless of how long the headless session runs.

**Side-channel state**: AG2's `context_variables` (v0.8+) passes structured state between agents without going through the LLM message thread — analogous to OpenAI SDK's `RunContextWrapper.context`. Zero token cost, typed access.

---

### 5. Mastra — Automatic Memory Compression

**State pattern**: Memory-backed by default. Three layers:
- **Conversation history** (per `resourceId`/`threadId`) — message thread with configurable window
- **Semantic memory** — vector-embedded past interactions; top-K retrieval at call time
- **Working memory** — structured key/value injected into system prompt every turn

```typescript
const memory = new Memory({
  storage: new LibSQLStore({ url: "file:./mastra.db" }),
  options: {
    lastMessages: 20,           // rolling window
    semanticRecall: { topK: 5 } // retrieve 5 most relevant past memories
  }
});
```

**Auto-compression**: Mastra automatically compresses memory at ~30,000 tokens with no configuration. This is the only framework surveyed with zero-config overflow handling.

**Suspend/resume** (workflow engine):
```typescript
const workflow = mastra.workflow("t0-receipt-review")
  .step("analyze", async ({ context }) => {
    if (needsHumanApproval(context.receipt)) {
      await suspend({ receipt: context.receipt });  // saves snapshot
    }
    return analyzeReceipt(context.receipt);
  });

// Operator provides approval:
await workflow.resume(runId, { approvedBy: "operator@vnx" });
```

**Key insight for VNX**: Mastra's working memory (structured key/value in system prompt) is the cleanest model for T0's "state briefing" pattern. T0 needs a compact summary of current feature state injected every invocation — working memory does this automatically without prompt engineering.

---

### 6. Claude Agent SDK — Native JSONL Persistence

**State pattern**: Sessions are persistent by default. JSONL files at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. Each session event (message, tool call, tool result) is a JSONL line.

**Headless resumption**:
```bash
# Capture session ID from first run
session_id=$(claude -p --output-format json "Review receipt X" | jq -r '.session_id')

# Resume that session for the next receipt
claude -p --resume "$session_id" --output-format stream-json "Review receipt Y"

# Or resume most recent session
claude --continue
```

**Key flags**:
| Flag | Purpose |
|---|---|
| `-p` / `--print` | Non-interactive, exits on completion |
| `--resume <id>` | Resume specific session by ID |
| `--continue` / `-c` | Resume most recent session |
| `--output-format stream-json` | NDJSON event stream for programmatic parsing |
| `--max-turns N` | Guard against runaway loops |
| `--bare` | Skip CLAUDE.md, skills, hooks, MCP (clean SDK invocation) |

**File checkpointing** (SDK feature):
- Creates backups before any Write/Edit tool modifies files
- Each user message gets a checkpoint UUID
- `SDKFilesPersistedEvent` tracks all checkpointed files
- Full rollback to any prior checkpoint UUID

**Agent Teams** (experimental, v2.1.32+) — disk inbox pattern:
- Each agent has `~/.claude/<teamName>/inboxes/<agentName>.json`
- Agents write JSON messages to peer inbox files
- Leader polls inboxes on an interval for new messages
- Pure filesystem, zero infrastructure

**CLAUDE.md agent identity injection**:
- Files loaded from `~/.claude/CLAUDE.md` (user-global) + `./<path>/CLAUDE.md` (project)
- Injected into system prompt context automatically at every invocation
- `--bare` mode skips all CLAUDE.md loading for clean programmatic use
- `.claude/terminals/T0/CLAUDE.md` is a first-class identity injection mechanism — no prompt injection risk because it's a local filesystem privilege

**Direct comparison for VNX T0**:
- VNX already uses `--output-format stream-json` via `SubprocessAdapter`
- `--resume <session_id>` enables stateful T0 headless invocations
- `CLAUDE.md` at `.claude/terminals/T0/CLAUDE.md` provides T0 role identity
- The polling daemon needs to capture `session_id` from the first invocation and thread it through subsequent calls

---

### 7. n8n AI Agents — Stateless Triggers + External DB

**State pattern**: Stateless execution model. Each workflow execution is an isolated unit. All state that must survive across executions must be written to external DB (Postgres, Redis) at the end of each run and loaded at the start of the next.

```
Trigger → [Load State from Postgres] → [AI Agent] → [Save State to Postgres] → Done
```

**Memory nodes** (within single execution):
| Type | Persistence | Use Case |
|---|---|---|
| Window Buffer Memory | Session only | Rolling window of last N messages |
| Summary Memory | Session only | Auto-summarize when window fills |
| PostgreSQL Memory | Persistent | Cross-execution history |
| Redis Memory | Persistent + TTL | Hot context, TTL-expiring sessions |

**Multi-agent coordination** via sub-workflows:
- AI Agent Tool node calls another workflow as a tool
- Sub-workflows are reusable across parent agents
- Pattern: orchestrator agent delegates to sub-workflow agents via tool calls

**MCP Trigger + MCP Client** (newer pattern):
- MCP Trigger acts as server; MCP Client acts as consumer
- Avoids webhook overhead for internal agent-to-agent calls
- Direct relevance to VNX's headless worker communication

**Key insight for VNX**: n8n's "load state at start / save state at end" pattern is the most portable model — it works with any storage backend and any invocation mechanism. VNX already does this for most operations. The gap is the T0 decision rationale, which currently lives only in conversation memory.

---

## Cross-Framework Best Practices

### BP-1: External State as First-Class Citizen

Every production-grade framework externalizes state to a durable store. The orchestrator's in-process context is ephemeral; the only durable truth is the store.

**VNX alignment**: `.vnx-data/` is the durable truth. T0's conversation is ephemeral. Currently T0's decision rationale is conversation-only — this must move to `t0_decision_log.jsonl`.

---

### BP-2: Thread/Session Identity Enables Stateless-by-Default

LangGraph's `thread_id`, OpenAI SDK's `Session`, Mastra's `resourceId`+`threadId`, Claude CLI's `session_id` all solve the same problem: binding disparate invocations into a logical session without requiring a persistent process.

**VNX application**:
```bash
# First T0 headless invocation — capture session ID
VNX_T0_SESSION=$(claude -p \
  --resume-if-exists .vnx-data/state/t0_session_id \
  --output-format json \
  "Review receipt: $(cat .vnx-data/receipts/pending/latest.json)" \
  | jq -r '.session_id')

echo "$VNX_T0_SESSION" > .vnx-data/state/t0_session_id
```

Each subsequent invocation uses `--resume $(cat .vnx-data/state/t0_session_id)` to maintain continuity.

---

### BP-3: Compact Context Injection, Not Full History

All frameworks provide a mechanism to limit context injection:
- LangGraph: `trim_messages`, summarize nodes
- OpenAI SDK: `handoff_history_mapper`, `MessageHistoryLimiter`
- AG2: `TransformMessages`
- Mastra: `lastMessages: N` + auto-compression
- n8n: `contextWindowLength` on Window Buffer Memory

**VNX application**: T0 does not need full conversation history per invocation. It needs:
1. Current feature state (PR, track, gate) — ~500 tokens
2. Last 3-5 receipts — ~2,000 tokens
3. Open items — ~500 tokens
4. Decision instructions (CLAUDE.md) — ~1,000 tokens
5. The current event — ~500 tokens

**Total: ~4,500 tokens per invocation** vs. ~50,000 tokens for full state reconstruction. This is the "rolling briefing" pattern.

---

### BP-4: Side-Channel State for Zero-Token Application Data

OpenAI SDK's `RunContextWrapper.context` and AG2's `context_variables` both demonstrate that application state (DB handles, config, feature metadata) should travel as a typed side-channel, not through the LLM message thread.

**VNX application**: The Python daemon wrapper that invokes T0 should:
1. Load `.vnx-data/` state files
2. Build a compact briefing struct
3. Serialize it as a structured JSON string (not free-form)
4. Inject into the `-p` prompt as a structured block

```
## State Briefing
```json
{
  "active_pr": "F36",
  "active_gate": "implementation",
  "pending_receipts": 2,
  "open_items": [{"id": "OI-047", "severity": "warn", "title": "..."}],
  "last_dispatch": "20260407-050001-f36-A",
  "last_decision": "approved"
}
```

## Event
```json
{"type": "receipt", "terminal": "T1", "status": "success", ...}
```
```

---

### BP-5: Checkpoint at Decision Boundaries, Not Every Step

LangGraph checkpoints after every node (maximally durable). CrewAI checkpoints at method boundaries. Both are valid; the right granularity depends on decision frequency.

For T0, the natural checkpoint boundary is: after each receipt review decision. T0 processes ~5-15 receipts per feature. Checkpointing per decision is appropriate — not per tool call.

**VNX application**: The `t0_decision_log.jsonl` record IS the checkpoint. Write it immediately after T0 generates a decision, before executing the decision's actions. This enables replay: re-run the actions from the log if the execution step fails.

---

### BP-6: Event Queue Decouples Producer from Consumer

LangGraph uses a message-passing model between graph nodes. n8n uses webhook triggers. CrewAI uses `@listen` decorators. The common pattern: **the orchestrator is woken by an event, not by a timer or a process**.

**VNX application**: The `.vnx-data/state/t0_event_queue/` directory (proposed in HEADLESS_T0_FEASIBILITY_REPORT.md) implements this pattern. Receipt processor writes events; daemon polls for events; T0 processes one event per invocation.

Event isolation ensures:
- Each T0 invocation has exactly one task (one receipt, one gate completion)
- Failed invocations can be retried against the same event file
- Events are durable across daemon restarts

---

### BP-7: Agent Identity via Config File, Not Prompt

The "agent = directory with config" model is used by:
- Claude Code: `CLAUDE.md` files auto-loaded from directory hierarchy
- Mastra: `agent.yaml` config in agent directory
- CrewAI: `agents.yaml` in Crew configuration
- OpenAI Agents SDK: Agent objects with `instructions` string (typically externalized to a file)

**VNX alignment**: `.claude/terminals/T0/CLAUDE.md` is the VNX implementation of this pattern. The T0 role instructions are injected at the filesystem-privilege level, not via prompt parameter. This is superior to prompt injection because:
1. It loads before any user message content
2. It cannot be overridden by untrusted content in receipts or reports
3. It is version-controlled alongside the terminal configuration

No changes needed here — VNX already uses best practice.

---

### BP-8: Token Budget as a First-Class Design Constraint

Every framework documents token overflow handling separately from the happy path. Token overflow is not an edge case — it is a design constraint.

For VNX headless T0 using `--resume`:
- Claude's context window: 1M tokens
- JSONL session growth: ~2,000-5,000 tokens per receipt review cycle
- **Theoretical limit**: 200-500 receipt reviews per session before rotation needed
- **Practical limit**: Lower due to context dilution reducing decision quality before hitting hard limit

**Rotation strategy** (informed by LangGraph's summarize-node pattern):
```
Session N approaching limit
  → T0 generates: context_handover = {active_pr, open_items, last_5_decisions, active_blockers}
  → Daemon saves context_handover to .vnx-data/state/t0_context_handover.json
  → Daemon starts new session: claude -p "Load handover: {context_handover}" → new session_id
  → New session continues with compact context
```

This is effectively the same as LangGraph's "summarize node" except triggered by a daemon-side token budget check rather than an in-graph decision.

---

## Specific Recommendations for VNX Headless T0

### R1: Use `--resume` for Session Continuity (Primary Path)

The `claude -p --resume <session_id>` mechanism provides the exact continuity VNX needs. Session state accumulates across invocations via JSONL on disk. This is equivalent to LangGraph's `thread_id`-based checkpoint resumption.

**Implementation**:
```python
# t0_headless_daemon.py
session_id_file = Path(".vnx-data/state/t0_session_id")

def get_or_create_session():
    if session_id_file.exists():
        return session_id_file.read_text().strip()
    return None  # first invocation will create a new session

def invoke_t0(event_json: dict, session_id: str | None) -> dict:
    cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "10"]
    if session_id:
        cmd.extend(["--resume", session_id])
    cmd.append(build_t0_prompt(event_json))

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = json.loads(result.stdout)

    # Persist session ID for next invocation
    new_session_id = output.get("session_id")
    if new_session_id:
        session_id_file.write_text(new_session_id)

    return output
```

### R2: Implement Rolling Context Briefing (Anti-Token-Bloat)

Instead of relying on full session history, inject a compact state briefing as the first message of every invocation. This mirrors AG2's `TransformMessages` and Mastra's working memory.

```python
def build_t0_prompt(event: dict) -> str:
    state = load_vnx_state()  # .vnx-data/ file reads
    briefing = {
        "active_pr": state.active_pr,
        "active_gate": state.active_gate,
        "pending_receipt_count": state.pending_count,
        "recent_decisions": state.last_decisions[-3:],
        "open_items": state.blocking_open_items,
    }
    return f"""## State Briefing
{json.dumps(briefing, indent=2)}

## Event
{json.dumps(event, indent=2)}

Review this event and take appropriate governance action."""
```

### R3: Write `t0_decision_log.jsonl` Before Executing Actions

Informed by CrewAI's `@persist` pattern and OpenAI SDK's `RunState` serialization. The decision log is the durable record. Write it first; execute second. This enables action replay if execution fails.

```jsonl
{"ts":"2026-04-07T12:34:56Z","dispatch_id":"20260407-050001-f36-A","action":"approve","reasoning":"Tests pass, 3 locations verified","session_id":"abc123","token_usage":{"input":4200,"output":310}}
{"ts":"2026-04-07T12:35:10Z","dispatch_id":"20260407-060001-f36-B","action":"dispatch","track":"B","role":"test-engineer","reasoning":"Track A complete, gate passed"}
```

### R4: Event Queue with Outbox Pattern

Informed by n8n's webhook-trigger model and LangGraph's node handoff pattern. Each receipt/gate event is an isolated unit of work.

```
receipt_processor_v4.sh
  └── (new) write_t0_event()
        └── .vnx-data/state/t0_event_queue/pending/<event_id>.json

t0_headless_daemon.py
  └── poll pending/ every 10s
  └── for each event: invoke_t0(event) → parse decision → execute → move to processed/
```

This is a clean, debuggable, retryable pipeline. Each event file contains its full payload; failures can be retried by moving the file back to `pending/`.

### R5: Session Rotation Triggered by Daemon (Not T0)

Informed by Mastra's auto-compression and LangGraph's summarize-node pattern. The daemon, not T0, is responsible for session hygiene.

```python
# In t0_headless_daemon.py
TOKEN_ROTATION_THRESHOLD = 700_000  # tokens

def should_rotate_session(session_id: str) -> bool:
    session_file = get_session_file(session_id)
    approx_tokens = estimate_session_tokens(session_file)
    return approx_tokens > TOKEN_ROTATION_THRESHOLD

def rotate_session(old_session_id: str) -> str:
    # 1. Ask T0 to summarize context for handover
    handover = invoke_t0({"type": "handover_request"}, old_session_id)
    # 2. Save handover to file
    Path(".vnx-data/state/t0_context_handover.json").write_text(handover["context_summary"])
    # 3. Start fresh session with handover injected
    new_session = invoke_t0({"type": "handover_load", "context": handover["context_summary"]}, None)
    return new_session["session_id"]
```

### R6: Keep CLAUDE.md as Primary Identity Mechanism

No change needed. `.claude/terminals/T0/CLAUDE.md` provides T0 role identity, governance rules, and output format instructions. This is already best practice (confirmed by survey of all frameworks). It is loaded at filesystem-privilege level, not through prompt parameters.

---

## Code Examples: VNX-Adapted Patterns

### Minimal Polling Daemon Skeleton

```python
#!/usr/bin/env python3
"""t0_headless_daemon.py — T0 orchestration daemon (minimal skeleton)"""

import json
import subprocess
import time
from pathlib import Path

VNX_DATA = Path(".vnx-data")
EVENT_QUEUE = VNX_DATA / "state" / "t0_event_queue"
SESSION_ID_FILE = VNX_DATA / "state" / "t0_session_id"
DECISION_LOG = VNX_DATA / "state" / "t0_decision_log.jsonl"

def poll_events() -> list[Path]:
    pending = EVENT_QUEUE / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    return sorted(pending.glob("*.json"))

def get_session_id() -> str | None:
    if SESSION_ID_FILE.exists():
        return SESSION_ID_FILE.read_text().strip()
    return None

def save_session_id(session_id: str) -> None:
    SESSION_ID_FILE.write_text(session_id)

def invoke_t0(event: dict, session_id: str | None) -> dict:
    cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "15"]
    if session_id:
        cmd.extend(["--resume", session_id])
    briefing = build_briefing(event)
    cmd.append(briefing)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"T0 invocation failed: {result.stderr}")

    output = json.loads(result.stdout)
    if new_sid := output.get("session_id"):
        save_session_id(new_sid)
    return output

def build_briefing(event: dict) -> str:
    # Load compact state snapshot
    state = load_compact_state()
    return (
        f"## State Briefing\n{json.dumps(state, indent=2)}\n\n"
        f"## Event\n{json.dumps(event, indent=2)}\n\n"
        "Review this event and respond with a governance decision as JSON."
    )

def load_compact_state() -> dict:
    # Read from .vnx-data/ — kept compact, not full file dumps
    # (implementation reads queue state, open items, recent decisions)
    return {}  # placeholder — implement against actual VNX CLI tools

def log_decision(decision: dict, event: dict) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, **decision}
    with open(DECISION_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")

def execute_decision(decision: dict) -> None:
    action = decision.get("action")
    if action == "approve":
        subprocess.run(["python3", "scripts/lib/pr_queue_manager.py",
                        "advance", decision["dispatch_id"]])
    elif action == "dispatch":
        subprocess.run(["python3", "scripts/lib/pr_queue_manager.py",
                        "dispatch", "--track", decision["track"],
                        "--role", decision["role"]])
    elif action == "escalate":
        Path(".vnx-data/state/escalations/").mkdir(parents=True, exist_ok=True)
        Path(f".vnx-data/state/escalations/{decision['event_id']}.json").write_text(
            json.dumps(decision)
        )

def process_event(event_file: Path) -> None:
    event = json.loads(event_file.read_text())
    session_id = get_session_id()

    output = invoke_t0(event, session_id)
    decision = parse_decision(output)

    log_decision(decision, event)
    execute_decision(decision)

    # Move event to processed
    processed = EVENT_QUEUE / "processed"
    processed.mkdir(exist_ok=True)
    event_file.rename(processed / event_file.name)

def parse_decision(output: dict) -> dict:
    # Parse structured JSON from T0's final message
    final_text = output.get("result", "")
    # T0 instructed to respond with JSON block
    import re
    match = re.search(r'```json\s*(\{.*?\})\s*```', final_text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return {"action": "wait", "reasoning": "Could not parse T0 output"}

def run():
    print("T0 headless daemon started")
    while True:
        events = poll_events()
        for event_file in events:
            try:
                process_event(event_file)
            except Exception as e:
                print(f"[ERROR] Failed to process {event_file}: {e}")
                # Leave file in pending/ for retry
        time.sleep(10)

if __name__ == "__main__":
    run()
```

### Event File Schema

```json
{
  "event_id": "evt-20260407-123456",
  "type": "receipt",
  "timestamp": "2026-04-07T12:34:56Z",
  "payload": {
    "dispatch_id": "20260407-050001-f36-A",
    "terminal": "T1",
    "track": "A",
    "pr": "F36",
    "gate": "planning",
    "status": "success",
    "report_path": ".vnx-data/unified_reports/20260407-164526-A-frameworks.md"
  }
}
```

### Decision Output Schema (T0 → Daemon)

```json
{
  "action": "approve",
  "dispatch_id": "20260407-050001-f36-A",
  "reasoning": "Track A report is complete, research document committed, all dispatch requirements met",
  "confidence": "high",
  "open_items_actions": [],
  "next_dispatch": {
    "pr": "F36",
    "track": "B",
    "role": "test-engineer",
    "gate": "implementation"
  },
  "escalation": null
}
```

---

## Open Items

| ID | Severity | Title | Notes |
|---|---|---|---|
| — | warn | `--resume` session durability unvalidated at scale | Need empirical test: how many T0 receipt cycles before context degrades? |
| — | info | Token estimation for session rotation trigger | Need to measure average tokens per T0 invocation cycle |
| — | info | `t0_event_queue` integration with receipt_processor_v4.sh | Dispatch F36-B or F37 scope |
| — | info | Daemon PID management and crash recovery | Out of scope for research dispatch; needed for Option B impl |

---

**Dispatch ID**: 20260407-050001-f36-headless-t0-frameworks-A
**PR**: F36
**Track**: A
**Gate**: planning
**Status**: success
