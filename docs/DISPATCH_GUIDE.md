# How VNX Dispatches Work

> You don't annotate a plan and wait. You break work into dispatches and send them to separate terminals.

This document explains how the VNX orchestration system manages work across multiple AI agents — from tracks and deliverables to parallel execution.

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

## 1. Tracks and Deliverables

T0 records the plan in the tracks DB and breaks work into deliverables. Inspect
the plan and its proposed dispatches with:

```bash
vnx objective list
vnx deliverable list --objective <track-id>
```

`FEATURE_PLAN.md` is a retired generated view; any surviving copy is an example,
not a live plan source. `vnx init` no longer creates it. The readers are
`scripts/planning_cli.py::cmd_objective_list` and
`scripts/planning_cli.py::cmd_deliverable_list`; see
[ADR-047](governance/decisions/ADR-047-tracks-db-enige-planbron.md) and the
[planning reference](core/HORIZON_PLANNING.md).

T0 scopes each delivery and records dependencies before promotion. Deliverables
can carry explicit dependency edges (`vnx deliverable depend`); the implementation
is `scripts/lib/deliverable_dependencies.py`. Independent work can run in parallel.

---

## 2. Dispatching: Deliverables and Manager Blocks

T0 can promote a planned deliverable or write a Manager Block for the dispatch pipeline.

### Path A: Deliverable Promotion

`vnx deliverable add` records a proposed dispatch for an objective. After review,
the authorized caller promotes it:

```bash
vnx deliverable promote <dispatch-id>
```

`scripts/planning_cli.py::cmd_deliverable_promote` checks the track's blockers
and promotion authority before changing `proposed` to `ready`. T0 promotion
requires the track's opt-in and a verified grant; see the
[horizon lifecycle](core/HORIZON_LIFECYCLE.md).
The current queue is available through `vnx objective queue`.

### Path B: Loose Dispatch (Ad-hoc)

T0 can also write a Manager Block directly — a scoped instruction set for a specific terminal:

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

This path handles quick fixes, research tasks, or follow-ups. T0 writes the block,
then the dispatch pipeline validates and stages it for promotion; see the
[dispatch rules](core/DISPATCH_RULES.md).

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

## 3b. Claude Workers: Concurrency and Permissions

Every Claude worker runs in its own fresh, isolated worktree: `git worktree
add` gives each dispatch its own checked-out files and branch while sharing
the repository's object database (not a full clone, and not a shared working
tree). The tmux-spawn lane was removed on 2026-09-18; `claude_headless`
(`claude -p` via envelope) is the only claude lane, and it runs on the Claude
subscription, not metered API credits. Two consequences follow from the
worktree isolation:

**Concurrency defaults to 10 concurrent Claude workers.** Claude subscription
sessions share a concurrency cap across everything on the account, so VNX
serializes claude-lane dispatches through an account-level slot lock. The
default is 10 slots (`VNX_TMUX_MAX_CONCURRENT`, historically named for the
retired tmux lane but still the knob for `claude_headless`). Dial it down
(e.g. `=1` for fully serial) or up as an explicit, informed choice, not
something the system escalates to on its own. Provider workers (Codex, Kimi,
GLM, DeepSeek) don't share this cap and always run in parallel.

**Workers get a scoped, role-based tool allow-list by default.** Since
2026-08-14, headless workers spawn with capabilities scoped to their role
rather than the old blanket `--dangerously-skip-permissions` posture. Worktree
isolation bounds the filesystem, not the network, and a blanket-skip worker
still reaches ambient MCP servers outside the checkout. To opt back into the
blanket posture for a specific dispatch, set `VNX_WORKER_BLANKET_SKIP=1`.

---

## 4. Bringing Your Own Ideas

If you have specific tasks in mind, tell T0 what you want and let it organize the work.

### How it works

1. **Describe what you want to T0**: "I need auth validation fixed, scoring logic updated, and the WebSocket handler refactored."

2. **T0 breaks it into dispatches**: It creates scoped Manager Blocks for each terminal — each small enough for one context window, with clear instructions and success criteria.

3. **T0 creates open items first**: For complex work, T0 registers open items (blockers, warnings, info) before dispatching. This creates a checklist that must be resolved before a PR is considered done.

4. **Dispatches enter the approval pipeline**: Proposed deliverables require promotion as described above; Manager Blocks follow the dispatch rules.

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
│  2. T0 records tracks and deliverables (or Manager Blocks) │
│  3. T0 reviews the scope and dependencies                  │
│  4. Authorized promotion makes a deliverable ready         │
│  5. Dispatcher routes to terminal, locks it                 │
│  6. Worker executes, writes report                          │
│  7. Receipt processor captures report, releases lock        │
│  8. T0 reviews receipt + quality advisory                   │
│  9. T0 decides: close open items / complete PR / redispatch │
│ 10. Next dispatch gets promoted → cycle repeats             │
└─────────────────────────────────────────────────────────────┘
```

Promotion authority is checked at step 4. T0 reviews the worker's evidence and
decides the next step; a completion receipt alone does not close the work.

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
```

The replay walks through a complete governance lifecycle with real evidence data — dispatch promotion, parallel execution, quality gates, and blocker resolution.

→ [Full repo](https://github.com/Vinix24/vnx-orchestration)
→ [Architecture docs](https://github.com/Vinix24/vnx-orchestration/blob/main/docs/manifesto/ARCHITECTURE.md)
→ [Limitations (what's tested, what's not)](https://github.com/Vinix24/vnx-orchestration/blob/main/docs/manifesto/LIMITATIONS.md)
