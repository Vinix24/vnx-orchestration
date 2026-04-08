# Headless T0 Orchestrator — Feasibility Report

**Dispatch ID**: 20260407-040001-f36-headless-t0-arch-C
**PR**: F36
**Track**: C
**Gate**: planning
**Status**: success
**Date**: 2026-04-07
**Author**: T3 (Architecture Review)

---

## Executive Summary

Running T0 as a headless `claude -p` process is **technically feasible** but requires significant adapter work. The VNX state layer is already ~60% externalized to files/DB, and the existing subprocess adapter (F28/F32) proves the pattern works for workers. However, T0's orchestration role demands multi-turn reasoning, accumulated context, and judgment persistence that single-shot `-p` cannot replicate without a state management layer.

**Recommendation: CONDITIONAL GO** — viable under Option B (polling daemon) or Option C (hybrid), not Option A (pure single-shot).

---

## 1. Capability Matrix

| T0 Capability | Interactive (tmux) | Headless (`claude -p`) | Gap |
|---|---|---|---|
| **Read state files** (receipts, queue, items, progress) | works | works | None — Read/Bash available in -p |
| **Write state files** (via Python CLI tools) | works | works | None — Bash available to call CLI |
| **Create dispatches** (`pr_queue_manager.py dispatch`) | works | works | None — pure Python CLI |
| **Patch dispatches** (`pr_queue_manager.py patch`) | works | works | None — pure Python CLI |
| **Promote dispatches** (`pr_queue_manager.py promote`) | works | works | None — atomic file move |
| **Receive receipts** (tmux paste from receipt_processor) | works | broken | Needs file-poll adapter |
| **Verify worker claims** (Grep/Read/git log) | works | works | None — tools available |
| **Close/add open items** (`open_items_manager.py`) | works | works | None — pure Python CLI |
| **Review gate evidence** (Read gate JSON + report) | works | works | None — Read available |
| **Quality advisory interpretation** | works | partial | Advisory files exist; T0 judgment is conversation-only |
| **Multi-turn reasoning** (hold context across receipts) | works | broken | Each -p call is fresh unless --resume used |
| **Decision rationale persistence** | partial (conversation) | broken | No durable decision log exists |
| **Pattern memory** (cross-dispatch learning) | partial (conversation) | broken | No externalized pattern store |
| **Escalation to operator** | works (tmux visible) | needs-adapter | Must write escalation file or webhook |
| **Smart tap** (capture manager blocks) | works | broken | Smart tap monitors T0 pane |
| **Context window management** (rotation/handover) | works (hooks) | partial | Hooks fire in -p mode but rotation is different |
| **Skill loading** (.claude/skills/) | works | partial | Skills injected into prompt text, not native |
| **MCP tools** (GitHub, Notion, etc.) | works | broken | -p mode only has 6 core tools |
| **Session resume** (--resume flag) | N/A | works | Can chain -p calls with session continuity |

**Summary**: 11 capabilities work, 3 are partial, 4 are broken.

---

## 2. Gap Analysis

### Critical Gaps (Must Fix)

**G1: Receipt delivery channel**
- Current: receipt_processor_v4.sh pastes receipts into T0's tmux pane
- Headless: No pane exists. Receipts accumulate in `receipts/pending/` forever
- Fix: T0 must poll `t0_receipts.ndjson` or `receipts/pending/` directory instead of receiving paste events

**G2: Multi-turn context loss**
- Current: T0 accumulates understanding across 50+ receipt reviews in one session
- Headless: Each `-p` call starts fresh (unless `--resume` used)
- Fix: Either use `--resume` for session continuity, or externalize all decision context to files
- Risk: `--resume` may hit context limits after extended orchestration

**G3: Escalation channel**
- Current: T0 writes text visible in tmux pane; operator reads it
- Headless: No visible output surface
- Fix: Write escalation events to a file (e.g., `.vnx-data/state/escalations/`) or webhook

**G4: Smart tap elimination**
- Current: smart_tap_v7 monitors T0 pane for manager blocks
- Headless: No pane to monitor
- Fix: T0 writes dispatch files directly via CLI (already supported by `pr_queue_manager.py`)

### Moderate Gaps (Should Fix)

**G5: MCP tools unavailable**
- Current: Interactive T0 can use GitHub MCP, Notion, etc.
- Headless: Only Read/Write/Bash/Grep/Glob/Edit
- Fix: All external operations must go through Bash (e.g., `gh pr create`)
- Impact: Low — T0 rarely uses MCP directly; workers handle most external ops

**G6: Decision rationale not persisted**
- Current: T0's reasoning about "why approve this receipt" lives in conversation
- Headless: Lost between invocations
- Fix: Add a `t0_decision_log.jsonl` file for durable decision records
- Impact: Medium — affects audit trail and operator understanding

**G7: Hook behavior in -p mode**
- Current: SessionStart, context monitor, handover detector hooks fire in interactive
- Headless: Hooks may fire but their effects (tmux operations, rotation) need adaptation
- Fix: Audit each hook for -p compatibility; likely need stub replacements

### Low-Priority Gaps

**G8: Operator visibility**
- Current: Operator sees T0 reasoning in real-time via tmux
- Headless: Output goes to NDJSON stream, not human-readable
- Fix: Dashboard integration or log tailing
- Impact: Operator experience degraded but not blocked

---

## 3. Architecture Options

### Option A: Pure Single-Shot (Event-Driven)

```
Receipt arrives → trigger script → claude -p "Review this receipt: {receipt_json}" → T0 action
```

**How it works:**
1. Receipt processor detects new report, generates receipt
2. Instead of tmux paste, calls: `claude -p --model opus "Given state: {state_snapshot}. Review receipt: {receipt}. Decide next action."`
3. T0 outputs structured JSON decision (approve/reject/dispatch/escalate)
4. Wrapper script executes the decision (call CLI tools, write files)

**Pros:**
- Simplest architecture — no daemon, no state machine
- Each invocation is self-contained and debuggable
- No context window pressure (fresh each time)
- Natural retry/idempotency (re-run same receipt)

**Cons:**
- **No accumulated context** — T0 cannot learn from previous receipts in the session
- **Expensive** — full context reconstruction on every call (load all state files)
- **Slow** — each decision takes 30-60s cold start
- **Quality regression risk** — T0's judgment quality depends on accumulated reasoning
- **Token cost** — loading full state snapshot per call is expensive (~50K tokens/call)

**Verdict: NOT RECOMMENDED** — T0's value is in accumulated judgment, which single-shot destroys.

---

### Option B: Polling Daemon (Python Orchestration Loop)

```
Python daemon → polls state → detects events → claude -p --resume {session} "New event: ..." → action
```

**How it works:**
1. Python daemon (`t0_headless_daemon.py`) runs continuously
2. Polls `t0_receipts.ndjson`, `receipts/pending/`, state changes every 5-10s
3. On new event: calls `claude -p --resume {session_id}` with event context
4. T0 maintains session continuity via `--resume`
5. Daemon executes T0's decisions via Python CLI tools
6. Daemon handles escalation (write to file, send webhook)

**Pros:**
- **Session continuity** via `--resume` — T0 accumulates context
- **Controlled invocation** — daemon decides when to call T0
- **Structured I/O** — daemon can parse T0's JSON output and act on it
- **Resilient** — daemon restarts T0 session if it fails
- **Observable** — daemon logs every invocation and decision

**Cons:**
- **Session drift** — `--resume` may hit context limits after many rounds
- **Complexity** — needs daemon lifecycle management (PID, health checks)
- **Latency** — polling interval adds 5-10s delay to event response
- **Session management** — must handle session expiry, rotation, crash recovery
- **Two processes** — daemon + Claude process need coordination

**Estimated effort:** 3-4 PRs (daemon, receipt adapter, escalation channel, integration tests)

**Verdict: RECOMMENDED for full headless T0** — best balance of continuity and control.

---

### Option C: Hybrid (Interactive T0 with Headless Delegation)

```
Interactive T0 (tmux) → delegates heavy analysis to claude -p subprocesses
```

**How it works:**
1. T0 remains in interactive tmux session (current architecture)
2. For expensive operations (deep code review, multi-file verification), T0 spawns `claude -p` subprocess
3. Subprocess results flow back to T0 via file (shared state directory)
4. T0 retains orchestration control and accumulated context

**Pros:**
- **Minimal change** — T0 stays interactive, only delegates computation
- **Best of both worlds** — context accumulation + parallel analysis
- **No receipt adapter needed** — existing tmux paste flow works
- **No smart tap changes** — manager blocks work as-is
- **Incremental adoption** — can start with one delegation use case

**Cons:**
- **Doesn't solve the headless T0 goal** — T0 still needs tmux
- **Added complexity** — T0 must manage subprocess lifecycle
- **Context splitting** — T0 must synthesize its own context with subprocess findings
- **Limited gain** — main bottleneck is T0 decision speed, not analysis depth

**Estimated effort:** 1-2 PRs (subprocess delegation helper, result integration)

**Verdict: RECOMMENDED as stepping stone** — low risk, immediate value, but doesn't achieve full headless.

---

### Comparison Matrix

| Criterion | Option A (Single-Shot) | Option B (Polling Daemon) | Option C (Hybrid) |
|---|---|---|---|
| Implementation effort | Low (1-2 PRs) | Medium (3-4 PRs) | Low (1-2 PRs) |
| Context continuity | None | Good (--resume) | Full (interactive) |
| Operational complexity | Low | Medium (daemon mgmt) | Low |
| Token cost per decision | High (~50K/call) | Low (incremental) | Baseline |
| Decision quality | Degraded | Near-baseline | Baseline |
| Achieves headless T0 | Yes (with quality loss) | Yes | No |
| Scalability | Good | Good | Limited |
| Operator visibility | Poor | Medium (logs) | Full (tmux) |

---

## 4. Receipt Feedback Architecture (for Option B)

### Current Flow
```
Worker report → receipt_processor_v4.sh → tmux paste → T0 pane → T0 reads
```

### Proposed Headless Flow
```
Worker report → receipt_processor_v4.sh → t0_receipts.ndjson (already happens)
                                        → receipts/pending/ (outbox, already happens)
                                        → NEW: t0_event_queue/ (structured event file)

t0_headless_daemon.py:
  1. Poll t0_event_queue/ for new events (receipts, gate completions, escalations)
  2. Build context: load recent receipts + open items + progress state + recommendations
  3. Call: claude -p --resume {session} --model opus "{context}\n\nNew event: {event}"
  4. Parse structured output (JSON decision)
  5. Execute decision via CLI tools
  6. Log decision to t0_decision_log.jsonl
  7. Move event from pending/ to processed/
```

### Event Queue Schema
```json
{
  "event_id": "evt-20260407-123456",
  "type": "receipt|gate_complete|escalation|timer",
  "timestamp": "2026-04-07T12:34:56Z",
  "payload": {
    "dispatch_id": "...",
    "terminal": "T1",
    "status": "success",
    "report_path": "..."
  }
}
```

### Decision Output Schema (T0 → Daemon)
```json
{
  "action": "approve|reject|dispatch|escalate|wait",
  "dispatch_id": "...",
  "reasoning": "Receipt shows all tests pass, code verified at 3 locations",
  "open_items_actions": [
    {"action": "close", "item_id": "OI-047", "reason": "Fixed in commit abc123"}
  ],
  "next_dispatch": {
    "pr_id": "PR-5",
    "track": "A",
    "role": "backend-developer"
  },
  "escalation": null
}
```

### Adapter Required in receipt_processor_v4.sh
```bash
# Replace tmux paste with event file write
if is_headless_t0; then
    write_event_to_queue "$receipt_json" "$terminal" "$dispatch_id"
else
    send_receipt_to_t0_pane "$receipt_json" "$terminal"  # existing path
fi
```

The `is_headless_t0()` check reads `VNX_ADAPTER_T0=subprocess` environment variable, consistent with existing adapter pattern.

---

## 5. Risk Assessment

### High Risk

| Risk | Impact | Mitigation |
|---|---|---|
| **Quality regression** — headless T0 makes worse decisions without accumulated context | Gate failures, bad merges | Use --resume for session continuity; add decision logging; run in shadow mode first |
| **Session context overflow** — --resume sessions hit token limits after many rounds | T0 stops working mid-feature | Implement session rotation with state snapshot handover |
| **Silent failures** — T0 daemon crashes without operator noticing | Dispatches stall, queue blocks | Health check endpoint, heartbeat file, alerting |

### Medium Risk

| Risk | Impact | Mitigation |
|---|---|---|
| **Decision latency** — polling + Claude inference adds 15-30s per decision | Slower feature delivery | Acceptable for governance; not on critical path |
| **Escalation missed** — operator doesn't see headless T0 escalation | Blockers unresolved | Webhook/Slack integration, dashboard alert |
| **Hook incompatibility** — existing hooks assume tmux environment | Startup failures, context issues | Audit and stub hooks for headless mode |
| **Audit gap** — decision rationale not captured | Governance traceability lost | Add t0_decision_log.jsonl from day one |

### Low Risk

| Risk | Impact | Mitigation |
|---|---|---|
| **MCP tool loss** — no GitHub/Notion MCP in -p mode | Minor functionality gap | Route through Bash (gh CLI, etc.) |
| **Smart tap removal** — dispatch creation flow changes | Workflow adjustment | Already handled by CLI tools |

---

## 6. State Externalization Gaps

For headless T0 to work reliably, these currently conversation-only data points must be externalized:

| Data | Current Location | Proposed Externalization |
|---|---|---|
| Decision rationale | Conversation memory | `t0_decision_log.jsonl` |
| Evidence verification steps | Conversation memory | Include in decision log `checks_performed` field |
| Quality confidence level | Conversation memory | Include in decision log `confidence` field |
| Cross-dispatch patterns | Conversation memory | `quality_intelligence.db` pattern table (already partially exists) |
| Terminal capability assessment | Conversation memory | Static config or learned via intelligence DB |
| Feature-level strategy | Conversation memory | `progress_state.yaml` strategy section |

---

## 7. Recommendation

### CONDITIONAL GO

**Recommended path: Option C → Option B (incremental)**

1. **Phase 1 (F36-F37)**: Implement Option C (Hybrid)
   - Keep T0 interactive
   - Add subprocess delegation for heavy analysis
   - Build `t0_decision_log.jsonl` infrastructure
   - Build event queue infrastructure
   - Validate decision logging captures sufficient context

2. **Phase 2 (F38-F39)**: Implement Option B (Polling Daemon)
   - Build `t0_headless_daemon.py`
   - Adapt receipt processor for headless delivery
   - Implement session rotation with context handover
   - Run headless T0 in shadow mode (parallel to interactive T0)
   - Compare decision quality: headless vs interactive

3. **Phase 3 (F40+)**: Full headless T0
   - Remove tmux dependency for T0
   - All 4 terminals headless
   - Full autonomous chain execution

### Conditions for GO

- [ ] `t0_decision_log.jsonl` captures sufficient rationale for audit
- [ ] `--resume` session continuity validated over 20+ receipt cycles
- [ ] Session rotation handles context overflow gracefully
- [ ] Shadow-mode decision quality within 90% of interactive baseline
- [ ] Operator escalation channel tested and reliable
- [ ] Receipt processor adapter for headless delivery tested

### Conditions for NO-GO

- `--resume` sessions degrade after <10 rounds (insufficient for feature lifecycle)
- Decision quality drops >20% in shadow mode comparison
- Session rotation loses critical context (open items, active blockers)

---

## 8. Key Technical Findings

1. **The substrate is ready**: All T0 state operations go through Python CLI tools (pr_queue_manager, open_items_manager, runtime_core_cli). These work identically from `-p` mode.

2. **The existing subprocess adapter (F28/F32) proves the pattern**: T1 already runs headless via `subprocess_adapter.py`. The same infrastructure extends to T0 with an orchestration wrapper.

3. **Receipt processor outbox pattern is critical**: `receipt_processor_v4.sh` already queues receipts in `pending/` when T0 pane is unavailable. This is the natural integration point for headless T0.

4. **Smart tap is eliminable**: T0 can create and promote dispatches entirely through `pr_queue_manager.py` CLI commands. The smart tap was an interactive convenience, not a necessity.

5. **Session resume is the key enabler**: `claude -p --resume {session_id}` allows multi-turn orchestration within a single logical session. Without this, headless T0 would require full state reconstruction per call (~50K tokens).

6. **MCP tools are non-critical for T0**: T0's role is governance (read state, make decisions, write state). It doesn't need GitHub MCP, Notion, or other external integrations — those are worker concerns.

---

## Open Items

| ID | Severity | Title | Notes |
|---|---|---|---|
| — | info | `--resume` session limit unknown | Need to validate how many rounds --resume supports before context overflow |
| — | info | Hook audit needed | Each hook must be tested in -p mode before Phase 2 |
| — | warn | No structured decision log exists | Must be built before headless T0 can be audited |
| — | info | Dashboard integration for headless T0 | Operator visibility needs design work |
