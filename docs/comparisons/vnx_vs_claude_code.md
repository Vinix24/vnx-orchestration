# VNX vs Claude Code

An honest comparison for developers deciding whether VNX adds value on top of Claude Code.

## The Short Answer

Claude Code is an excellent AI coding agent. VNX is not a replacement — it's an orchestration layer that coordinates multiple Claude Code instances (and other AI CLIs) with governance controls.

If you use one Claude Code terminal for straightforward tasks, you probably don't need VNX. If you run multiple agents, need audit trails, or want quality gates between "agent proposes" and "code merges," VNX solves problems Claude Code doesn't try to solve.

## Feature Comparison

| Capability | Claude Code | VNX + Claude Code |
|-----------|-------------|-------------------|
| **AI coding assistance** | Full-featured agent with tool use, file editing, terminal access | Same — VNX doesn't modify what Claude Code can do |
| **Multi-agent coordination** | Manual: open multiple terminals yourself | Structured: T0 orchestrator dispatches scoped tasks to T1-T3 workers |
| **Audit trail** | Chat history in `~/.claude/` | Append-only NDJSON ledger with structured receipts per task |
| **Quality gates** | Per-tool permission prompts | Deterministic gates: file size, test coverage, blocker counts. LLM never judges its own work |
| **Human approval** | Tool-level accept/reject | Dispatch-level: every task requires explicit human promotion before any agent sees it |
| **Context rotation** | Manual `/clear` or `/compact` | Automatic: detects 65% context usage, writes handover, clears, resumes with zero human intervention |
| **Cost tracking** | Token counts in session | Structured cost-per-task, cost-per-agent, cost-per-feature reporting |
| **Multi-model** | Claude only | Claude Code, Codex CLI, Gemini CLI, Kimi CLI — mix models per terminal |
| **Git worktrees** | Not managed | Integrated worktree lifecycle: create, track, preflight, merge, cleanup |
| **Provenance** | Git blame shows Claude Code as author | Every code change traces to a specific dispatch, terminal, approval, and quality verdict |
| **Session intelligence** | Not available | Pattern mining across sessions: model performance, task-type efficiency, tuning suggestions |

## What VNX Adds

### 1. Governance over agent output

Claude Code's permission system controls what the agent *can do*. VNX controls what the agent *is allowed to merge*. These are different problems:

- Claude Code: "Allow this agent to edit files" (tool-level)
- VNX: "This code passes quality gates and was produced under a reviewed dispatch" (output-level)

### 2. Coordination without conflicts

Running three Claude Code terminals on the same repo means merge conflicts, duplicated work, and no visibility into who's doing what. VNX assigns scoped tasks (150-300 lines), routes them to specific terminals, and tracks completion through receipts.

### 3. Traceability you can query

Claude Code's chat history is useful for reviewing a single session. VNX's NDJSON ledger is queryable with standard Unix tools:

```bash
# Cost per terminal this week
jq 'select(.terminal=="T1") | .metadata.cost_est' .vnx-data/state/t0_receipts.ndjson | paste -sd+ | bc

# Failed tasks by type
jq 'select(.status=="failure") | .dispatch_id' .vnx-data/state/t0_receipts.ndjson | sort | uniq -c | sort -rn
```

### 4. Context rotation without lost work

When Claude Code fills its context window, you lose your working state. VNX detects high context usage, triggers a structured handover, clears the session, and resumes with the original task plus handover notes. The receipt chain maintains continuity across rotations.

## What VNX Does NOT Do

- **Does not replace Claude Code** — VNX orchestrates Claude Code instances, it doesn't compete with them
- **Does not modify Claude Code's behavior** — agents run their native CLIs unmodified
- **Does not add AI capabilities** — VNX's quality gates are deterministic, not LLM-based
- **Does not require Claude Code** — VNX works with any AI CLI that produces file output

## When to Use Just Claude Code

- Single-agent workflows where one terminal is enough
- Quick tasks where governance overhead isn't justified
- Exploration and prototyping where audit trails don't matter yet
- Teams that don't need cross-agent traceability

## When to Add VNX

- Running 2+ AI agents on the same codebase simultaneously
- Need to know which agent produced which code change, and under what task
- Want quality gates that prevent bad merges regardless of what the LLM thinks
- Need cost tracking per feature, not just per session
- Working in regulated or compliance-sensitive contexts where provenance matters
- Context windows keep filling up on long-running tasks

## Getting Started

If you already use Claude Code:

```bash
# Starter mode — add governance to a single Claude Code terminal
git clone https://github.com/Vinix24/vnx-orchestration.git
cd vnx-orchestration && ./install.sh /path/to/your/project
cd /path/to/your/project
vnx init --starter
vnx doctor

# Later, when you want parallel agents:
vnx init --operator
vnx start
```

VNX doesn't change how Claude Code works. It adds structure around it.
