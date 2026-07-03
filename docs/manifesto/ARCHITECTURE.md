# Glass Box Governance: An Append-Only Audit Architecture for Multi-Agent AI Workflows

**Author**: Vincent van Deth
**Date**: February 2026
**Status**: Reference Architecture — 1.0.0, released to PyPI 2026-07-02 (`pip install vnx-orchestration`)
**Usage**: 6+ months daily use on a local system · 14,000+ governed receipts in the audit trail · ephemeral per-dispatch workers (the old fixed 4-terminal model is now opt-in)

---

## The Problem Nobody Talks About

The multi-agent AI space is obsessed with orchestration — how to make agents work together. But orchestration without governance is just coordinated chaos.

After running multi-agent workflows daily for six months (Claude Code, Codex CLI, 3-4 parallel terminals), I kept hitting the same wall: **I couldn't audit what my agents actually did.** In the early months, the system ran with auto-accept enabled — dispatches flowed without manual confirmation. That autonomy taught me the hard way that orchestration without governance is just coordinated chaos. I switched to mandatory human confirmation for every dispatch (and have used that ever since), but even with manual approval, chat logs are unstructured, session transcripts are opaque, and git history shows *what* changed but not *why* an agent decided to change it at that specific moment, or *which* agent was responsible.

The industry treats the chat window as the system's state. This is a fundamental engineering mistake:

- **No rollback**: You cannot undo a conversation.
- **No grep**: You cannot query a chat log for "cost per feature" or "failed tests by terminal."
- **No replay**: When a system crashes mid-session, you cannot reconstruct what happened from a proprietary context window.
- **No governance**: There is no point between "agent decides" and "agent executes" where a human or quality gate can intervene.

Every multi-agent framework I evaluated — LangGraph, CrewAI, AutoGen, Claude Agent Teams, OpenAI Swarm — solves the orchestration problem well. What I couldn't find was a framework that treated governance — audit trails, quality gates, explicit human approval — as a first-class architectural concern rather than something you bolt on later.

---

## The Architecture: Four Pillars of Glass Box Governance

I built **VNX** to solve this. Not as a product, but as an architecture I needed to trust my own agent workflows. It operates on four principles:

If you want the visual proof pack (what screenshots to capture, and where they fit), see `SCREENSHOTS.md`.

### 1. The Ledger: Receipts as the Canonical Audit Trail

VNX replaces opaque chat logs with an **append-only NDJSON ledger**. Every agent action — task start, task completion, failure, acknowledgment — generates a structured receipt:

```json
{
  "event_type": "task_complete",
  "timestamp": "2026-02-07T14:30:00Z",
  "dispatch_id": "dispatch-20260207-001",
  "terminal": "T2",
  "status": "success",
  "metadata": {
    "cost_est": 0.04,
    "duration_sec": 124
  },
  "provenance": {
    "git_ref": "a1b2c3d4",
    "is_dirty": false,
    "captured_by": "append_receipt"
  }
}
```

**Why NDJSON?** It is crash-resilient (partial writes don't corrupt previous entries), streamable (`tail -f` works), unix-friendly (`grep`, `jq`, `awk` all work natively), and append-only by design. No database, no migrations, no schema versioning headaches.

The ledger is the single source of truth. Runtime state files exist for performance, but the ledger alone can reconstruct the system's complete history.

Screenshot pointer: `SCREENSHOTS.md` (S2: Chain of Custody).

### 2. Governance Gates: Two Dispatch Paths, One Rule

VNX enforces human approval on **every** dispatch — but through two distinct paths:

**Path A: Feature Plan Dispatches (Staging → Promote)**

For structured feature work, VNX introduces an explicit staging gate:

1. **Draft**: The orchestrator (T0) generates a dispatch plan in `dispatches/staging/`, often containing multiple tracks (e.g., Track A for implementation, Track B for testing).
2. **Review**: A human reviews the file paths, complexity, and scope.
3. **Promote**: Only after approval does the dispatch move to `dispatches/queue/`.
4. **Claim**: A worker terminal (T1/T2/T3) claims the task and emits a `task_ack` receipt.

**Path B: Ad-Hoc Dispatches (Popup Confirmation)**

For operational tasks — quick questions, hotpatch instructions, one-off commands — T0 can propose a dispatch directly. But every ad-hoc dispatch triggers an **explicit human confirmation popup** before anything is sent to a worker terminal. There is no silent dispatch path.

Both paths enforce the same principle: **nothing executes without human approval**. The staging gate catches architectural errors in complex plans; the popup catches drift in day-to-day operations. Together they prevent the **"Cascade of Doom"** — the failure mode where one hallucinated output triggers a chain of downstream agent actions, each compounding the error.

**A critical design constraint**: T0 (the orchestrator) **cannot write files directly**. It operates through hooks that restrict write actions, limiting T0 to coordination, planning, and dispatch creation. All file modifications happen in worker terminals (T1/T2/T3) under the governance layer's oversight. This separation of concerns ensures the orchestrator cannot bypass its own governance gates.

Screenshot pointer: `SCREENSHOTS.md` (S3: Queue Manager UI, S4: Worker Refusal).

### 3. Decoupled Observability: The External Watcher Pattern

Most orchestration frameworks depend on provider-specific hooks to track agent activity. This creates two problems: vendor lock-in, and fragility when hooks fail or aren't available.

VNX uses a **dual-input bridge**:

- **Push (Optional Hooks)**: If a provider supports hooks, they can emit receipts directly.
- **Pull (External Watcher)**: For providers without hooks (Codex, Gemini, future models), VNX watches the filesystem for agent output reports and generates receipts from them.

This **External Watcher Pattern** means that even if an agent process crashes, the orchestration layer remains alive and aware. It also means you can swap models per-task — use Claude for architecture, Codex for implementation, Gemini for review — without changing any orchestration logic. Kimi runs as a first-class production worker lane via its CLI OAuth path (the `kimi-via-cli-only` constraint), one of several providers the watcher pattern already validates across ecosystems.

Intelligence and usage signals are derived from receipts and watchers, not from provider hooks. Hooks can optionally enrich metadata, but they are never dependencies.

### 4. Quality Gates: The Strict Tech Lead

VNX enforces engineering thresholds that AI agents routinely ignore. These run as an **async intelligence layer** — post-ingestion from receipts, not as blocking hooks:

| Check | Warning | Blocking | Tool |
| :--- | :--- | :--- | :--- |
| File size (Python) | >500 lines | >800 lines | `wc -l` |
| File size (Shell) | >200 lines | >300 lines | `wc -l` |
| Function size | >40 lines | >70 lines | `radon` / AST |
| Dead code | `vulture` signals | Unused imports/vars | `ruff` |
| Secrets | — | Any detection | `gitleaks` |
| Risk patterns | `\|\| true` usage | Broad `kill` patterns | Regex |
| Test hygiene | — | src/ changes without tests | Git diff |

**Why async?** Running `vulture` or `gitleaks` inside a terminal hook adds latency and increases provider coupling. VNX captures the event via receipts, then runs validation in the background. If a gate fails, the orchestrator issues a **Refactor Dispatch** — not a rollback, but a structured follow-up task.

The quality advisory generates a risk score (0-100) and a deterministic decision: `approve`, `approve_with_followup`, or `hold`.

Screenshot pointer: `SCREENSHOTS.md` (S5: Quality Advisory).

---

## Keeping the Future State Honest

The four pillars govern work that has happened. The hardest part of a planning
layer is keeping the *intended* future state true without a human re-typing it.
The 1.0.1 future-state reconciliation is how VNX does that, and it follows the
same glass-box discipline as the rest of the system.

Open items (blockers, follow-ups) feed **tracks** (the planning unit), which feed
**dispatches** (the executable work). A bridge keeps the open-item → track links
current, and a deterministic reconciler computes each track's status from those
links, its dependency graph, and its dispatches. A track is `done` only when it
has no unresolved blocking open-items, every dependency track is done, all of its
dispatches reached a terminal state, and any linked PR is confirmed merged.
No LLM decides this; it is computed, so it is auditable.

Two design choices keep it trustworthy. First, the bridge is a **single writer**:
every `track_open_items` mutation goes through one set of primitives in one
transaction, so there is no second code path that can disagree with the first.
Second, it runs inside the autopilot tick under an explicit gate
(`VNX_ROADMAP_AUTOPILOT=1`), and if the sync fails the tick **refuses to advance**
on stale state rather than guessing. The ledger events for these mutations are
emitted after the DB commit (at-most-once, reconcile-compensated); exactly-once
delivery via an outbox is deliberately deferred, and that tradeoff is written
down rather than hidden. The whole linkage is tenant-scoped under the ADR-007
composite-key model so one project's planning state can never bleed into another.

For the precise rule, the lifecycle diagram, and the loop, see
`docs/core/00_VNX_ARCHITECTURE.md` (*Future-State Reconciliation*).

---

## How This Compares

| Feature | Native Agent Teams | Aura | SafeClaw | Camunda | **VNX** |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **State Format** | Chat context (opaque) | JSONL events | JSONL + SHA-256 chain | BPMN audit logs | **NDJSON ledger** |
| **Governance Gates** | None (lead-agent model) | Approval policies | Deny-by-default gating | BPMN/DMN gateways | **Staging → Promote** |
| **Model-Agnostic** | No (provider-locked) | Yes (via Agno) | Limited (OpenClaw) | Yes | **Yes (watcher pattern)** |
| **Quality Gates** | None | Not documented | Not documented | DMN rules | **Lint/size/dead code** |
| **External Watcher** | No | No | No | Service tasks | **Yes (first-class)** |
| **Recovery** | Session resume | Event replay | Hash chain replay | Process replay | **Ledger replay** |

**Key differentiator**: VNX is designed as **governance middleware** — it sits between your agents and your codebase, regardless of which orchestration framework or model powers those agents. Aura and SafeClaw are self-contained agent frameworks. Camunda is enterprise workflow infrastructure. VNX is the governance layer you add to whatever you already use.

---

## What This Is (and What It Isn't)

**This is:**
- A reference architecture validated by 6 months of daily use on a local 4-terminal system
- A working Python/Bash prototype used to build real software
- An opinionated stance: governance belongs in the architecture, not as an afterthought
- MIT-licensed and open for inspection

**This is not:**
- A hosted service
- A competitor to LangGraph/CrewAI/AutoGen (it's a governance layer that could complement them)
- A promise of feature parity across all providers

### Known Limitations

- The interactive tmux worker lane is available and subscription-preserving; full PREPARE/GOVERN envelope parity across all lanes is targeted for 1.x (see Tier 1/2 framing in the README).
- Gemini integration documented and validated; Kimi K2.6 is in production via CLI OAuth lane, 6/6 skill-injection verified.
- **T0 orchestrator tested with Claude Opus** (via Claude Code, which powers ~80% of the workflow). Other models may work as T0, but this is less tested.
- Per-worker git worktree isolation is available via `VNX_ISOLATED_WORKTREE=1` and off by default; isolation guarantees vary by lane. Parallel multi-track execution is Tier 3 — designed, not shipped (see README Tier framing).
- File-based, local-first — not designed for distributed networks
- Tmux dependency for terminal management
- Python/Bash prototype — a production deployment would benefit from Rust/Go
- Public git history starts at repo separation point (earlier work was in a private product repo)

See `LIMITATIONS.md` for the full scope declaration.

---

## The Open Method

I am not a traditional systems engineer. I am a product architect who saw the potential of AI agents but was frustrated by their lack of discipline. I didn't build VNX *with* AI to avoid coding — I built it *for* AI to enforce engineering discipline.

I used AI to build the guardrails I needed for AI. The architecture is the contribution — not the implementation language.

See `OPEN_METHOD.md` for the full transparency report.

---

*See LIMITATIONS.md for current scope and Tier 1/2/3 feature framing.*
