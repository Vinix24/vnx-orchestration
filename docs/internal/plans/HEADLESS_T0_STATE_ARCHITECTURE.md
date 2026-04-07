# Headless T0 State Architecture

**Dispatch ID**: 20260407-050002-f36-headless-t0-state-arch-C
**PR**: F36
**Track**: C
**Gate**: planning
**Date**: 2026-04-07
**Author**: T3 (Architecture Design)
**Builds on**: HEADLESS_T0_FEASIBILITY_REPORT.md (Option B: Polling Daemon)

---

## Executive Summary

This document specifies the concrete state architecture for running T0 as fresh `claude -p` invocations via a polling daemon. The design centers on three principles:

1. **Agent = Directory** — each agent has a directory with CLAUDE.md; subprocess `cwd` activates it
2. **File-based state reconstruction** — a context assembler builds minimal snapshots from existing state files
3. **Decision memory via stream archive** — T0's previous outputs serve as rolling decision context

**Token budget target**: <30% of context window (~60K tokens of 200K) for state reconstruction.
**Actual estimate**: ~18K–25K tokens (9–12.5% of context), well within budget.

---

## 1. Agent Directory Model

### Design Decision

`claude -p` does NOT auto-load CLAUDE.md from its `cwd`. The existing `_inject_skill_context()` in `subprocess_dispatch.py` (lines 27–53) explicitly prepends CLAUDE.md content to the instruction. This is more reliable than relying on auto-discovery and continues to work.

The `agents/{role}/` directory serves two purposes:
1. **Context source**: `_inject_skill_context()` checks it as tier-1 priority (line 44)
2. **Working directory**: `subprocess_dispatch.py` sets `cwd=agents/{role}/` when the directory exists (lines 115–120)

### Proposed Directory Layout

```
agents/
├── orchestrator/                   # T0 agent
│   ├── CLAUDE.md                   # Orchestrator role, rules, tool constraints (~2K tokens)
│   ├── references/
│   │   ├── dispatch-patterns.md    # Dispatch creation patterns
│   │   ├── gate-discipline.md      # Gate rules and evidence requirements
│   │   └── decision-output.md      # Manager block format, JSON schemas
│   └── scripts/                    # T0-callable scripts (symlinks or copies)
│       ├── queue_status.sh
│       ├── dispatch_guard.sh
│       └── deliverable_review.sh
├── backend-developer/              # T1 worker agent
│   └── CLAUDE.md
├── test-engineer/                  # T2 worker agent
│   └── CLAUDE.md
├── reviewer/                       # T3 worker agent
│   └── CLAUDE.md
└── security-engineer/              # T3 alt agent
    └── CLAUDE.md
```

### CLAUDE.md Sizing Strategy

The current T0 skill (SKILL.md: ~4,300 tokens + template.md: ~1,635 tokens) is too large for headless T0's context budget. The `agents/orchestrator/CLAUDE.md` should be a **condensed** version:

| Content | Current tokens | Target tokens |
|---|---|---|
| Role definition & constraints | 1,927 (T0/CLAUDE.md) | 800 |
| Decision workflow (condensed SKILL.md) | 4,306 | 1,500 |
| Output format (condensed template.md) | 1,635 | 500 |
| **Total** | **7,868** | **2,800** |

The condensation removes examples, verbose explanations, and interactive-mode instructions. The references directory retains full docs for when T0 needs to `Read` them.

### How It Activates

```python
# subprocess_dispatch.py already does this:
def _inject_skill_context(terminal_id, instruction, role=None):
    # Tier 1: agents/{role}/CLAUDE.md  ← orchestrator CLAUDE.md
    # Tier 2: .claude/skills/{role}/CLAUDE.md
    # Tier 3: .claude/terminals/{terminal}/CLAUDE.md
    ...

# For T0 headless:
deliver_headless(
    terminal_id="T0",
    instruction=assembled_context + "\n\n" + event_payload,
    role="orchestrator",   # → loads agents/orchestrator/CLAUDE.md
    model="opus",
)
```

---

## 2. Dispatch Footer / Metadata Strategy

### Decision: Dynamic append, NOT in CLAUDE.md

Dispatch metadata (dispatch ID, completion guidelines, report format) is **per-dispatch** and must be appended to the instruction, not baked into CLAUDE.md.

| Approach | Token cost | Flexibility | Recommended |
|---|---|---|---|
| In CLAUDE.md (static) | Wasted on every invocation even when not dispatching | Cannot vary per dispatch | No |
| Appended to instruction (dynamic) | Only when needed | Full flexibility | **Yes** |
| Separate dispatch_context.json | Requires T0 to Read an extra file | Adds a Read tool call | No |

### Implementation

The polling daemon constructs the instruction in three layers:

```
[Layer 1: CLAUDE.md]        — injected by _inject_skill_context() (~2,800 tokens)
[Layer 2: State snapshot]   — assembled by t0_context_assembler.py (~3,000-5,000 tokens)
[Layer 3: Event + metadata] — the actual task/receipt/decision request (~500-1,500 tokens)
```

Layer 3 includes dispatch metadata when T0 is being asked to create a dispatch:
```
---
Dispatch-ID: 20260407-060001-f36-impl-A
Track: A
Gate: implementation
PR-ID: F36
Report to: $VNX_DATA_DIR/unified_reports/
---
```

This matches the existing footer pattern from `dispatch_metadata.sh` but is only included when relevant.

---

## 3. State Reconstruction Specification

### What T0 Needs Per Invocation

A fresh T0 session needs to answer: **"What just happened, what's the current state, and what should I do next?"**

The context assembler builds a snapshot from these sources:

| Source | File | What it provides | Token estimate | Always include? |
|---|---|---|---|---|
| Terminal status | `t0_brief.json` | Who's idle/working/blocked, queue stats | ~940 | Yes |
| Track progress | `progress_state.yaml` | Current gate per track, history | ~400 (extract) | Yes |
| Open items | `open_items.json` | Blockers and warnings (filtered) | ~500 (top 10) | Yes |
| PR queue | `pr_queue.json` | Feature execution state | ~200 | Yes |
| Recommendations | `t0_recommendations.json` | Actionable suggestions | ~650 | Yes |
| Recent receipts | `t0_receipts.ndjson` | Last 5 receipts (tail) | ~750 | Yes |
| Decision memory | `t0_decision_log.jsonl` | Last 3-5 T0 decisions | ~1,000 | Yes |
| Review gate results | `state/review_gates/results/` | Latest gate verdicts | ~400 (last 3) | If gates pending |
| **Subtotal** | | | **~4,840** | |

### What NOT to Include

| Source | Size | Why exclude |
|---|---|---|
| Full open_items.json | ~22K tokens | 1,003 items; only open blockers matter |
| Full t0_receipts.ndjson | ~60K tokens | 687 records; last 5 suffice |
| Conversation logs | ~500K+ tokens | Not actionable for decisions |
| Intelligence DB | ~750K tokens | Workers use this, not T0 |
| Dispatch archive | ~50K+ tokens | Historical, not decision-relevant |

### Snapshot Format

The assembler outputs a single markdown document:

```markdown
# T0 Context Snapshot
Generated: 2026-04-07T16:50:00Z

## Terminal Status
| Terminal | Status | Track | Current Task |
|---|---|---|---|
| T1 | working | A | 20260407-050001-f36-headless-t0-frameworks-A |
| T2 | idle | B | — |
| T3 | working | C | 20260407-050002-f36-headless-t0-state-arch-C |

## Queue: 0 pending, 2 active, 0 conflicts

## Track Progress
- A: planning gate, working (dispatch 20260407-050001)
- B: implementation gate, idle
- C: planning gate, working (dispatch 20260407-050002)

## Open Blockers (2 of 52 open)
- OI-238: File exceeds blocking threshold: 538 lines (max 500)
- OI-276: File exceeds blocking threshold: 813 lines (max 800)

## Recent Receipts (last 5)
1. [success] T3 20260407-040001-f36-headless-t0-arch-C (planning)
2. [success] T1 20260407-030001-f36-refactor-A (implementation)
...

## Recent Decisions (last 3)
1. [dispatch] Sent f36-headless-t0-frameworks to T1/A — framework analysis needed
2. [dispatch] Sent f36-headless-t0-state-arch to T3/C — state architecture design
3. [approve] Closed receipt for f36-refactor — all evidence verified
```

This format is:
- Human-readable (operator can inspect)
- Token-efficient (~3,000–5,000 tokens depending on state volume)
- Self-contained (T0 doesn't need to Read additional files for routine decisions)

---

## 4. T0 Decision Memory Design

### Problem

When T0 runs as fresh sessions, it loses:
- Why it dispatched work to specific tracks
- What evidence it verified in previous receipts
- What patterns it noticed across dispatches
- What it's waiting for before advancing gates

### Solution: Decision Log + Stream Archive

#### 4a. Structured Decision Log (`t0_decision_log.jsonl`)

Written by the daemon after each T0 invocation:

```jsonl
{"timestamp":"2026-04-07T16:50:00Z","session_id":"abc123","action":"dispatch","dispatch_id":"20260407-050001-f36-A","track":"A","reasoning":"Framework analysis needed before state architecture can be designed. T1 idle.","confidence":0.9,"checks_performed":["queue_status","terminal_readiness","gate_prereqs"],"open_items_actions":[]}
{"timestamp":"2026-04-07T16:55:00Z","session_id":"abc123","action":"approve","dispatch_id":"20260407-040001-f36-C","reasoning":"Report covers all 6 areas. Evidence verified at 3 file locations. No regressions found.","confidence":0.85,"checks_performed":["report_read","file_verification","gate_evidence"],"open_items_actions":[{"action":"close","id":"OI-301","reason":"Addressed in feasibility report"}]}
```

**Schema:**
```json
{
  "timestamp": "ISO-8601",
  "session_id": "string",
  "action": "dispatch|approve|reject|escalate|wait|close_oi|advance_gate",
  "dispatch_id": "string|null",
  "track": "A|B|C|null",
  "reasoning": "string (1-2 sentences)",
  "confidence": 0.0-1.0,
  "checks_performed": ["string"],
  "open_items_actions": [{"action": "close|add|defer", "id": "string", "reason": "string"}],
  "context_tokens_used": 12500
}
```

**Rolling window**: Keep last 50 decisions (~5K tokens). Archive older entries to `t0_decision_log.archive.jsonl`.

#### 4b. Stream Archive Reuse

The SubprocessAdapter archives events to `.vnx-data/events/archive/{terminal}/`. For T0, this creates a stream-json record of every tool call, thought, and output.

**Can this replace the decision log?** No — but it supplements it:

| Purpose | Decision log | Stream archive |
|---|---|---|
| Quick context (what did T0 decide?) | Yes — structured, scannable | No — too verbose |
| Deep debugging (why did T0 fail?) | Partial — reasoning field | Yes — full thought process |
| Token cost in context | ~1,000 tokens (last 5 entries) | ~10,000+ tokens (too expensive) |
| Written by | Daemon (post-processing) | SubprocessAdapter (automatic) |

**Recommendation**: Use the decision log for context reconstruction (included in snapshot). Use stream archive only for post-hoc debugging by operators.

#### 4c. Extract Script

A lightweight extractor pulls key decisions from stream archive when deeper context is needed:

```python
# t0_decision_extractor.py
def extract_decisions(archive_path: Path, last_n: int = 3) -> list[dict]:
    """Extract T0 decision summaries from stream-json archive.
    
    Filters for 'result' events (T0's final output per invocation)
    and 'text' events containing 'DISPATCH' or 'APPROVE' keywords.
    """
    decisions = []
    for line in reversed(archive_path.read_text().splitlines()):
        event = json.loads(line)
        if event.get("type") == "result":
            decisions.append({
                "timestamp": event["timestamp"],
                "output_preview": event.get("result", "")[:200],
            })
        if len(decisions) >= last_n:
            break
    return decisions
```

This is a fallback — the primary decision memory is `t0_decision_log.jsonl`.

---

## 5. Context Budget Breakdown

### Target: <30% of 200K context window = 60K tokens

| Component | Tokens | % of 200K | Notes |
|---|---|---|---|
| **agents/orchestrator/CLAUDE.md** | 2,800 | 1.4% | Condensed from 7,868 |
| **Root CLAUDE.md** | 600 | 0.3% | Injected by claude -p from project root |
| **State snapshot** | 4,840 | 2.4% | Assembled by t0_context_assembler.py |
| **Decision memory (last 5)** | 1,000 | 0.5% | From t0_decision_log.jsonl |
| **Event payload (receipt/trigger)** | 1,500 | 0.75% | The actual task for T0 |
| **Dispatch metadata footer** | 300 | 0.15% | When creating a dispatch |
| **System prompt overhead** | 2,000 | 1.0% | Claude's system framing |
| **TOTAL** | **~13,040** | **~6.5%** | |

**Headroom for T0 reasoning**: ~187K tokens (93.5%) — more than sufficient.

Even with generous estimates (larger state, more decisions, bigger event payloads):

| Scenario | Total context tokens | % of 200K |
|---|---|---|
| Minimal (routine check) | ~10,000 | 5% |
| Typical (receipt + dispatch) | ~13,000 | 6.5% |
| Heavy (complex gate review, 10 decisions) | ~20,000 | 10% |
| Worst case (all state + deep history) | ~25,000 | 12.5% |

All scenarios are well under the 30% target.

---

## 6. Context Assembler Specification

### `scripts/lib/t0_context_assembler.py`

```python
"""Build minimal T0 context snapshot from VNX state files.

Usage:
    python scripts/lib/t0_context_assembler.py --format markdown
    python scripts/lib/t0_context_assembler.py --format json --max-tokens 5000

Output: Markdown or JSON snapshot to stdout.
"""

class T0ContextAssembler:
    """Reads state files and produces a minimal context snapshot."""

    def __init__(self, state_dir: Path, max_tokens: int = 5000):
        self.state_dir = state_dir
        self.max_tokens = max_tokens

    def assemble(self) -> str:
        """Build the context snapshot."""
        sections = [
            self._terminal_status(),    # t0_brief.json
            self._track_progress(),     # progress_state.yaml
            self._open_blockers(),      # open_items.json (filtered)
            self._queue_state(),        # pr_queue.json
            self._recent_receipts(),    # t0_receipts.ndjson (tail -5)
            self._recent_decisions(),   # t0_decision_log.jsonl (tail -5)
            self._recommendations(),   # t0_recommendations.json
            self._pending_gates(),     # review_gates/results/ (if pending)
        ]
        return self._render_markdown(sections)

    def _terminal_status(self) -> dict:
        brief = json.loads((self.state_dir / "t0_brief.json").read_text())
        return {
            "title": "Terminal Status",
            "data": brief["terminals"],
            "queue": brief["queues"],
        }

    def _open_blockers(self) -> dict:
        items = json.loads((self.state_dir / "open_items.json").read_text())
        blockers = [i for i in items if i.get("severity") == "blocker" and i.get("status") == "open"]
        return {
            "title": f"Open Blockers ({len(blockers)} of {len([i for i in items if i.get('status')=='open'])} open)",
            "data": blockers[:10],  # Cap at 10 to control tokens
        }

    def _recent_receipts(self, n: int = 5) -> dict:
        path = self.state_dir / "t0_receipts.ndjson"
        lines = path.read_text().strip().splitlines()[-n:]
        receipts = [json.loads(l) for l in lines]
        return {"title": "Recent Receipts", "data": receipts}

    def _recent_decisions(self, n: int = 5) -> dict:
        path = self.state_dir / "t0_decision_log.jsonl"
        if not path.exists():
            return {"title": "Recent Decisions", "data": []}
        lines = path.read_text().strip().splitlines()[-n:]
        return {"title": "Recent Decisions", "data": [json.loads(l) for l in lines]}
```

### Integration with Polling Daemon

```python
# t0_headless_daemon.py (conceptual)

def handle_event(event: dict):
    assembler = T0ContextAssembler(state_dir=VNX_STATE_DIR)
    context = assembler.assemble()
    
    instruction = f"{context}\n\n---\n\nNEW EVENT:\n{json.dumps(event, indent=2)}"
    
    result = deliver_headless(
        terminal_id="T0",
        instruction=instruction,
        role="orchestrator",
        model="opus",
    )
    
    # Parse T0's structured output
    decision = parse_t0_decision(result.output)
    
    # Log decision
    log_decision(decision)
    
    # Execute decision
    execute_decision(decision)
```

---

## 7. Comparison: Interactive vs Headless T0

| Dimension | Interactive T0 (current) | Headless T0 (proposed) |
|---|---|---|
| **Context source** | Conversation memory (accumulated) | State snapshot + decision log (reconstructed) |
| **Context size** | Grows unbounded → rotates at ~180K | Fixed ~13K per invocation |
| **Decision quality** | High (accumulated judgment) | Near-high (structured memory compensates) |
| **Token cost per decision** | ~0 (already in context) | ~13K (reconstruction) |
| **Token cost per session** | ~200K (full conversation) | ~13K × N decisions |
| **Crash recovery** | Complex (find session, resume, reconcile) | Trivial (fresh invocation reads state) |
| **Audit trail** | Conversation log (unstructured, 17MB) | Decision log (structured, compact) |
| **Operator visibility** | Full (tmux pane) | Log tailing / dashboard |
| **Startup time** | 30-60s (skill load, reconciliation) | 10-20s (state read, Claude cold start) |
| **Session management** | Manual rotation, crash recovery | Stateless — no session to manage |
| **Scalability** | Single pane, serial | Can run N parallel T0 invocations |

### Key Advantage of Headless

The interactive T0 accumulates a massive conversation (17MB+) but only ~5K tokens of it are decision-relevant at any point. The headless model inverts this: it starts with only the relevant state and has 93% of context available for reasoning.

### Key Risk of Headless

The ~40% of T0 knowledge that currently lives only in conversation memory (cross-dispatch patterns, terminal capability assessments, quality confidence trends) must be captured in the decision log. If the decision log is too sparse, T0 will make worse decisions.

**Mitigation**: The `reasoning` and `checks_performed` fields in each decision log entry capture the why, not just the what. The assembler includes the last 5 decisions, providing a rolling window of judgment context.

---

## 8. New State Files Required

| File | Location | Purpose | Written by | Read by |
|---|---|---|---|---|
| `t0_decision_log.jsonl` | `.vnx-data/state/` | Structured record of every T0 decision | Daemon (post T0 invocation) | Context assembler |
| `t0_decision_log.archive.jsonl` | `.vnx-data/state/` | Archived decisions (>50 entries old) | Daemon (rotation) | Operators only |
| `t0_escalations.jsonl` | `.vnx-data/state/` | Escalation events for operator attention | Daemon (from T0 output) | Dashboard / alerting |
| `t0_event_queue/` | `.vnx-data/state/` | Pending events for T0 to process | Receipt processor, gate runner | Daemon (polls) |

### Event Queue Structure

```
.vnx-data/state/t0_event_queue/
├── pending/
│   ├── evt-20260407-165000-receipt.json
│   └── evt-20260407-165100-gate.json
└── processed/
    ├── evt-20260407-164500-receipt.json
    └── ...
```

Each event file:
```json
{
  "event_id": "evt-20260407-165000",
  "type": "receipt",
  "timestamp": "2026-04-07T16:50:00Z",
  "source": "receipt_processor",
  "payload": {
    "dispatch_id": "20260407-050001-f36-headless-t0-frameworks-A",
    "terminal": "T1",
    "status": "success",
    "report_path": ".vnx-data/unified_reports/20260407-170000-A-frameworks.md"
  }
}
```

---

## 9. Implementation Sequence

### Phase 1: State Infrastructure (F36-F37)

1. **Create `t0_decision_log.jsonl` writer** — add to receipt processor or as standalone daemon module
2. **Build `t0_context_assembler.py`** — reads all state files, outputs markdown snapshot
3. **Create `agents/orchestrator/CLAUDE.md`** — condensed T0 role from current SKILL.md
4. **Create event queue directory** — `t0_event_queue/{pending,processed}/`
5. **Add event queue writer to receipt_processor_v4.sh** — write event files alongside tmux paste

### Phase 2: Daemon Core (F38-F39)

6. **Build `t0_headless_daemon.py`** — poll loop, Claude invocation, decision parsing
7. **Add `--headless` flag to T0 startup** — switches between tmux and daemon mode
8. **Shadow mode** — run headless T0 in parallel with interactive, compare decisions

### Phase 3: Cutover (F40+)

9. **Remove tmux dependency for T0**
10. **Dashboard integration for headless monitoring**

---

## 10. Open Questions

| Question | Impact | Recommended resolution |
|---|---|---|
| Does `claude -p` load `.claude/CLAUDE.md` from project root when cwd is `agents/orchestrator/`? | If not, root CLAUDE.md rules won't apply | Test empirically; if not, include root CLAUDE.md in assembler output |
| How many `--resume` rounds before context degrades? | Affects Option B viability | Not relevant for proposed design (fresh invocations, not --resume) |
| Should T0 output structured JSON or free-form text? | Affects daemon parsing complexity | Start with structured JSON schema; T0's CLAUDE.md enforces format |
| Maximum event queue depth before T0 falls behind? | Could cause decision staleness | Set alarm at depth >5; batch events if >3 pending |

---

## Appendix A: Token Estimation Methodology

Token estimates use the approximation: **1 token ≈ 4 characters** (conservative for English text with JSON/code).

Verified against actual file sizes:
- `t0_brief.json`: 3,751 bytes → ~937 tokens (matches estimate)
- `t0_recommendations.json`: 2,615 bytes → ~653 tokens (matches estimate)
- T0 SKILL.md: 17,227 bytes → ~4,306 tokens

## Appendix B: Decision Log Example Session

```jsonl
{"timestamp":"2026-04-07T16:50:00Z","action":"wait","reasoning":"T1 and T3 both working on planning dispatches. No pending receipts. Wait for completion.","confidence":0.95,"checks_performed":["terminal_status","pending_receipts"],"context_tokens_used":10200}
{"timestamp":"2026-04-07T17:15:00Z","action":"approve","dispatch_id":"20260407-050001-f36-A","reasoning":"Framework analysis report covers all 3 candidates with clear comparison matrix. Evidence verified in report file.","confidence":0.85,"checks_performed":["report_read","evidence_verification"],"context_tokens_used":13500}
{"timestamp":"2026-04-07T17:16:00Z","action":"dispatch","dispatch_id":"20260407-060001-f36-impl-A","track":"A","reasoning":"Framework decision made. T1 idle after approval. Next: implement context assembler using chosen approach.","confidence":0.9,"checks_performed":["gate_prereqs","terminal_readiness","queue_status"],"context_tokens_used":14200}
```
