# How VNX Dispatches Work

> You don't annotate a plan and wait. You break work into dispatches and send them to separate terminals.

This document explains how the VNX orchestration system manages work across multiple AI agents — from feature plan to parallel execution, without bottlenecks.

---

## The Problem

You have a plan with 6 tasks. You spot issues in 3 of them simultaneously. But Claude processes one thing at a time. So you're stuck:

```
You: "Fix the auth flow"          → wait 4 min
You: "Also fix the scoring logic" → blocked
You: "And the WebSocket handler"  → still blocked
```

VNX solves this by separating **what to do** (the plan) from **who does it** (the terminals).

---

## 1. Feature Plan Init

Everything starts with a structured feature plan. T0 (the orchestrator) breaks a feature into numbered PRs with explicit dependencies:

```
vnx init → T0 creates FEATURE_PLAN.md
```

```markdown
## PR-1: AI Config & Model Registry (Track B)
Gate: implementation | Priority: P1 | Dependencies: none

## PR-2: Lead Scoring Engine (Track A)
Gate: implementation | Priority: P1 | Dependencies: PR-1

## PR-3: WebSocket Real-time Updates (Track C)
Gate: implementation | Priority: P2 | Dependencies: PR-2

## PR-4: Scoring Dashboard API (Track A)
Gate: implementation | Priority: P2 | Dependencies: PR-2

## PR-5: Multi-Provider Dispatch (Track B)
Gate: implementation | Priority: P2 | Dependencies: PR-1

## PR-6: Email Campaign Integration (Track C)
Gate: planning | Priority: P3 | Dependencies: PR-2, PR-4
```

Each PR is scoped to 150–300 lines — small enough to fit in a single context window. The dependency graph ensures nothing runs out of order:

```
PR-1 ──→ PR-2 ──→ PR-3
  │         │
  │         └──→ PR-4 ──→ PR-6
  │
  └──→ PR-5
```

PRs without mutual dependencies can run in parallel on separate terminals.

---

## 2. Dispatching: Feature Plan vs Loose

T0 has two ways to assign work. Both go through the same queue.

### Path A: Feature Plan Promotion

When a feature plan exists, PRs are staged as dispatch templates. T0 reviews and promotes them:

```
dispatches/staging/   →  T0 reviews  →  dispatches/queue/  →  human approves  →  terminal
```

T0 promotes with:
```bash
pr_queue_manager.py promote <dispatch-id>
```

### Path B: Loose Dispatch (Ad-hoc)

No feature plan needed. T0 writes a Manager Block directly — a scoped instruction set for a specific terminal:

```markdown
[[TARGET:A]]
Manager Block

Role: developer
Track: A
Terminal: T1
Gate: implementation
Priority: P0
Dispatch-ID: 20260224-fix-auth-validation

Instruction:
- Fix JWT validation in auth middleware
- Add expiry check for refresh tokens
- Run existing auth test suite
- Report findings

[[DONE]]
```

This is how you handle quick fixes, research tasks, or follow-ups that don't need a full feature plan. T0 writes it, the system queues it, a human approves it, and the terminal picks it up.

**Key rule: everything flows through T0.** Workers (T1–T3) never dispatch to each other. T0 is the single source of truth for what's happening where.

---

## 3. Terminal Locking

Each terminal can only work on one dispatch at a time. The system enforces this:

```json
{
  "T1": { "status": "working",  "claimed_by": "20260224-fix-auth" },
  "T2": { "status": "idle",     "claimed_by": null },
  "T3": { "status": "working",  "claimed_by": "20260224-ws-handler" }
}
```

What this means in practice:

- **T1 is locked** — no new dispatch can be sent until it reports back
- **T2 is available** — next queued dispatch for Track B goes here
- **T3 is locked** — working on its own task independently

Locks auto-expire after 15 minutes (stale lock protection). When a worker finishes and writes its report, the receipt processor releases the lock automatically.

This prevents the classic multi-agent problem: two agents editing the same files, or work arriving at a terminal that's mid-task.

---

## 4. Bringing Your Own Ideas

You don't need to wait for a feature plan. If you have specific tasks in mind, tell T0 what you want and let it organize the work.

### How it works

1. **Describe what you want to T0**: "I need auth validation fixed, scoring logic updated, and the WebSocket handler refactored."

2. **T0 breaks it into dispatches**: It creates scoped Manager Blocks for each terminal — each small enough for one context window, with clear instructions and success criteria.

3. **T0 creates open items first**: For complex work, T0 registers open items (blockers, warnings, info) before dispatching. This creates a checklist that must be resolved before a PR is considered done.

4. **Dispatches go to the queue**: Each dispatch enters the approval pipeline. You review and approve via the queue popup (Ctrl+G).

5. **Terminals work in parallel**:
```
T1 → auth validation fix        (working)
T2 → scoring logic update       (working)
T3 → WebSocket refactor          (working)
T0 → monitors receipts, decides next steps
```

### The report-back rule

When a terminal finishes, the worker writes a report with:
- What was done
- Files modified
- Test results
- Open items (blockers / warnings / info)

**Important**: if you've asked a follow-up question in T1–T3 after the worker already reported, always ask the worker to "report to T0" one more time. This ensures T0 gets a fresh report that includes the updates from your follow-up — otherwise T0 is working with stale information.

```
You (in T1): "Can you also handle the edge case for expired tokens?"
T1 (Claude): [does the work]
You (in T1): "Great. Now report to T0."
T1 (Claude): [writes updated report to unified_reports/]
T0: [receives receipt, reviews, decides next action]
```

---

## 5. The Complete Flow

Here's what a typical session looks like:

```
┌─────────────────────────────────────────────────────────────┐
│  1. YOU describe the feature/task to T0                     │
│  2. T0 creates FEATURE_PLAN.md (or loose dispatches)        │
│  3. T0 promotes dispatches to the queue                     │
│  4. YOU approve each dispatch (Ctrl+G popup)                │
│  5. Dispatcher routes to terminal, locks it                 │
│  6. Worker executes, writes report                          │
│  7. Receipt processor captures report, releases lock        │
│  8. T0 reviews receipt + quality advisory                   │
│  9. T0 decides: close open items / complete PR / redispatch │
│ 10. Next dispatch gets promoted → cycle repeats             │
└─────────────────────────────────────────────────────────────┘
```

The human stays in control at two points: approving dispatches (step 4) and the final decision is always T0's (step 9). Everything else is automated.

---

## 6. Contract Blocks (Deterministic Verification)

Dispatches can include a `## Contract` section that defines verifiable assertions about expected outcomes. Contracts enable automated receipt-time and pre-merge verification, reducing T0's review scope to semantic review.

### Contract Block Format

Add a `## Contract` section to any dispatch:

```markdown
## Contract
- file_exists: scripts/pre_merge_gate.py
- file_changed: bin/vnx
- pattern_match: "def run_gate_checks" in scripts/pre_merge_gate.py
- no_pattern: "hardcoded_path" in scripts/lib/vnx_paths.sh
- bash_check: `bash -n bin/vnx`
```

### Claim Types

| Type | Syntax | What It Checks |
|------|--------|----------------|
| `file_exists` | `- file_exists: <path>` | File exists at path |
| `file_changed` | `- file_changed: <path>` | File appears in `git diff` |
| `pattern_match` | `- pattern_match: "<regex>" in <path>` | Regex found in file |
| `no_pattern` | `- no_pattern: "<regex>" in <path>` | Regex NOT found in file |
| `bash_check` | `- bash_check: \`<command>\`` | Shell command exits 0 |

### Two Verification Stages

**Stage 1: Receipt-time (lightweight)** — Runs automatically via `verify_claims.py` when the receipt processor ingests a completion report. Fast checks only (no test suites). Non-fatal: failures are logged but don't block receipt processing. Results stored in `.vnx-data/state/verification_results/`.

**Stage 2: Pre-merge gate (heavy)** — Runs on demand via `vnx gate-check --pr <PR-ID>`. Includes everything from Stage 1 plus: pytest execution, AST/quality advisory checks, PR size validation, artifact verification (PDF/XLSX), and shell syntax checks. Produces a deterministic GO or HOLD verdict.

### Phase Rollout

- **Phase 2a** (current): Contract blocks are optional. Verification runs only when a contract block is present in the dispatch. No enforcement.
- **Phase 2b** (next): Contracts required for high-risk and implementation dispatches. Lightweight verification failures create open items.
- **Phase 2c** (target): Contracts are the default for all relevant PRs. Explicit opt-out reason required for dispatches without contracts.

### Related Scripts
- `scripts/verify_claims.py` — Lightweight receipt-time verifier
- `scripts/pre_merge_gate.py` — Heavy pre-merge gate engine
- `scripts/lib/contract_parser.py` — Contract block parser

---

## Why This Beats "Comment and Wait"

| Annotating a plan | VNX dispatches |
|---|---|
| One context window, one task at a time | 3 terminals working in parallel |
| Blocked while Claude processes | You review receipts while workers execute |
| Comments pile up, context gets polluted | Each dispatch is scoped and isolated |
| Hard to track what's done | Receipt ledger + open items = audit trail |
| No quality gate | Automated quality advisory on every completion |

The core insight: **your feedback isn't annotations on a plan — it's separate work items that can run independently.** Break them apart, dispatch them, and let the terminals work while you think about what's next.

---

## Getting Started

```bash
git clone https://github.com/Vinix24/vnx-orchestration.git
cd vnx-orchestration
./install.sh /path/to/your/project

# Try the dry-run demo (no LLM needed)
cd demo/dry-run
bash replay.sh --fast
```

The replay walks through a complete 6-PR governance lifecycle with real evidence data — dispatch promotion, parallel execution, quality gates, and blocker resolution — in under 2 minutes.

→ [Full repo](https://github.com/Vinix24/vnx-orchestration)
→ [Architecture docs](https://github.com/Vinix24/vnx-orchestration/blob/main/docs/manifesto/ARCHITECTURE.md)
→ [Limitations (what's tested, what's not)](https://github.com/Vinix24/vnx-orchestration/blob/main/docs/manifesto/LIMITATIONS.md)
