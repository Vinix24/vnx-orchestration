# Glass Box Governance: An Append-Only Audit Architecture for Multi-Agent AI Workflows

**Author**: Vincent van Deth
**Date**: February 2026
**Status**: Reference Architecture — released to PyPI 2026-07-02 (`pip install vnx-orchestration`)
**Usage**: extended daily use on a local system · governed receipts accumulate in the audit trail · ephemeral per-dispatch workers (the old fixed 4-terminal model is now opt-in)

---

## The Problem Nobody Talks About

The multi-agent AI space is obsessed with orchestration — how to make agents work together. But orchestration without governance is just coordinated chaos.

After running multi-agent workflows daily (Claude Code, Codex CLI, 3-4 parallel terminals), I kept hitting the same wall: **I couldn't audit what my agents actually did.** In the early months, the system ran with auto-accept enabled — dispatches flowed without manual confirmation. That autonomy taught me the hard way that orchestration without governance is just coordinated chaos. I switched to mandatory human confirmation for every dispatch (and have used that ever since), but even with manual approval, chat logs are unstructured, session transcripts are opaque, and git history shows *what* changed but not *why* an agent decided to change it at that specific moment, or *which* agent was responsible.

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

### 2. Governance Gates: One Door, One Human Step

VNX enforces human approval on **every** dispatch through one entry point, `vnx dispatch` (ADR-024):

1. **Stage**: `vnx dispatch stage` writes a staged bundle (instruction plus dispatch spec) into the central pending directory. It never fires.
2. **Review**: A human reads the compiled plan and permit with `vnx dispatch <dispatch-id> --dry-run`, which spawns nothing.
3. **Fire**: `vnx dispatch <dispatch-id>` starts one ephemeral worker in its own git worktree. The door picks the lane from the provider (`docs/core/DISPATCH_RULES.md` section 5).

The staged bundle is the staging gate (ADR-006). An older PR-queue path (`FEATURE_PLAN.md` to staging to promote) is still live for the roadmap autopilot; `docs/core/00_VNX_ARCHITECTURE.md` (Staging Workflow) describes both paths.

Both paths enforce the same principle: **nothing executes without human approval**. The gate prevents the **"Cascade of Doom"**: one hallucinated output triggering a chain of downstream agent actions, each compounding the error.

Review is gated the same way. A pull request needs one distinct reviewer per **review seat** of the project's review stack (default `codex_gate,kimi_gate`), and a rejection from any reviewer blocks the merge. A reviewer that is out is absence, not a vote (ADR-037), and the seat passes along the takeover chain (`codex_gate,kimi_gate,deepseek_gate`). The merge happens only through `scripts/pr_merge.py`. See `docs/core/00_VNX_ARCHITECTURE.md` (Review Seats) and `docs/core/PROVIDER_LANES.md` (Takeover chain). The seat rule and the takeover chain are on main, not yet released.

**A critical design constraint**: T0 (the orchestrator) plans, reviews and dispatches, and does not write code. Workers (T1/T2/T3 are role labels on dispatches) do the file changes in isolated worktrees under the governance layer's oversight. A hook also blocks T0 from spawning subagents, and `vnx guard install` registers a context guard for T0 sessions (`docs/operations/T0_CONTEXT_ROTATION.md`). This separation keeps the orchestrator from bypassing its own gates.

Screenshot pointer: `SCREENSHOTS.md` (S3: Queue Manager UI, S4: Worker Refusal).

---

### 3. Decoupled Observability: The External Watcher Pattern

Most orchestration frameworks depend on provider-specific hooks to track agent activity. This creates two problems: vendor lock-in, and fragility when hooks fail or aren't available.

VNX uses a **dual-input bridge**:

- **Push (Optional Hooks)**: If a provider supports hooks, they can emit receipts directly.
- **Pull (External Watcher)**: For providers without hooks (Codex, Gemini, future models), VNX watches the filesystem for agent output reports and generates receipts from them.

This **External Watcher Pattern** means that even if an agent process crashes, the orchestration layer remains alive and aware. It also means you can swap models per task, for example Claude for architecture and Codex for review, without changing any orchestration logic. Kimi runs through its CLI OAuth path (the `kimi-via-cli-only` constraint). Which provider may serve which lane is one machine-readable file, `scripts/lib/providers/provider_constraints.yaml`, and `vnx providers list` shows the operator's provider switches. The switch cannot turn a provider off yet: its write verbs refuse until every launch function calls `provider_switch.require` (`docs/core/PROVIDER_LANES.md`, Provider switch).

Intelligence and usage signals are derived from receipts and watchers, not from provider hooks. Hooks can optionally enrich metadata, but they are never dependencies.

### 4. Quality Gates: The Strict Tech Lead

VNX measures engineering thresholds that AI agents routinely ignore. These run as an **async intelligence layer** (`scripts/lib/quality_advisory.py`), post-ingestion from receipts, not as blocking hooks:

| Check | Warning | Blocking | Tool |
| :--- | :--- | :--- | :--- |
| File size (Python) | >500 lines | >1200 lines | line count |
| File size (Shell) | >300 lines | >600 lines | line count |
| Function size (Python) | >40 lines | >70 lines | AST |
| Function size (Shell) | >30 lines | >60 lines | line count |
| Lint | `ruff` findings | `E`/`F` codes | `ruff` |
| Dead code | `vulture` findings | none | `vulture` |
| Test hygiene | src changes without test changes | none | git diff |

Secret scanning is a separate CI job (`gitleaks`, `.github/workflows/public-ci.yml`), not part of the advisory. In CI a function-size ratchet fails a pull request that makes a function larger (`docs/core/SUBSYSTEMS.md`).

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
| **Governance Gates** | None (lead-agent model) | Approval policies | Deny-by-default gating | BPMN/DMN gateways | **Staged bundle, human fires** |
| **Model-Agnostic** | No (provider-locked) | Yes (via Agno) | Limited (OpenClaw) | Yes | **Yes (watcher pattern)** |
| **Quality Gates** | None | Not documented | Not documented | DMN rules | **Lint/size/dead code** |
| **External Watcher** | No | No | No | Service tasks | **Yes (first-class)** |
| **Recovery** | Session resume | Event replay | Hash chain replay | Process replay | **Ledger replay** |

**Key differentiator**: VNX is designed as **governance middleware** — it sits between your agents and your codebase, regardless of which orchestration framework or model powers those agents. Aura and SafeClaw are self-contained agent frameworks. Camunda is enterprise workflow infrastructure. VNX is the governance layer you add to whatever you already use.

---

## What This Is (and What It Isn't)

**This is:**
- A reference architecture validated by extended daily use on a local system (the fixed 4-terminal model described above is now opt-in, not the default)
- A working Python/Bash prototype used to build real software
- An opinionated stance: governance belongs in the architecture, not as an afterthought
- MIT-licensed and open for inspection

**This is not:**
- A hosted service
- A competitor to LangGraph/CrewAI/AutoGen (it's a governance layer that could complement them)
- A promise of feature parity across all providers

### Known Limitations

- The tmux-spawn worker lane was removed on 2026-09-18; `claude_headless` (`claude -p` via envelope) is the only Claude worker lane and runs on the Max subscription, not API credits. Tmux remains only as an injection path for interactive codex/kimi/gemini terminals.
- Gemini is a non-review provider lane: `gemini_review` was retired from the review stack. Build workers default to Sonnet (`workers-kimi-pinned` in `provider_constraints.yaml`); a dispatch spec that names a model, kimi-k3 for example, wins over that default.
- **T0 runs on Claude Opus** (via Claude Code). Other models may work as T0, but this is less tested.
- Per-dispatch git worktree isolation is unconditional in the headless envelope lane; the older subprocess-adapter lane gates it behind `VNX_ISOLATED_WORKTREE=1`. Parallel multi-track execution is Tier 3, designed and not shipped (ADR-020).
- The receipt hash chain is available per project and off by default (ADR-023), so the ledger is append-only but not tamper-evident unless a project turns the chain on.
- The content boundary is read by the conversation analyzer and the dream consolidation only, not by the receipt classifier, the tagger or the scout (OI-2172), so it is not a general guarantee about what reaches a non-claude provider.
- File-based, local-first: not designed for distributed networks.
- Tmux dependency for terminal management.
- Python/Bash prototype: a production deployment would benefit from Rust/Go.
- Public git history starts at repo separation point (earlier work was in a private product repo).

See `LIMITATIONS.md` for the full scope declaration.

---

## The Open Method

I am not a traditional systems engineer. I am a product architect who saw the potential of AI agents but was frustrated by their lack of discipline. I didn't build VNX *with* AI to avoid coding — I built it *for* AI to enforce engineering discipline.

I used AI to build the guardrails I needed for AI. The architecture is the contribution — not the implementation language.

See `OPEN_METHOD.md` for the full transparency report.

---

*See LIMITATIONS.md for current scope and Tier 1/2/3 feature framing.*
