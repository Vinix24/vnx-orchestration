# Example: Feature Development with Parallel Agents

> Build a user authentication system across three parallel tracks in under an hour.

This walkthrough shows VNX coordinating three AI agents on a real coding task: implementing JWT authentication with login endpoints, test coverage, and a security review — all running simultaneously.

---

## Prerequisites

- VNX installed and initialized in operator mode (`vnx init --operator`)
- `vnx doctor` passes cleanly
- At least one AI CLI installed (Claude Code, Codex CLI, or Gemini CLI)

---

## 1. Create a Feature Worktree

Isolate the work from `main` so all agents work in the same feature branch:

```bash
vnx worktree create auth-feature --ref main
cd ../your-project-wt-auth-feature/
```

VNX creates an isolated `.vnx-data/` directory, bootstraps skills and terminal configs, and runs `vnx doctor` automatically.

## 2. Launch the Terminal Grid

```bash
vnx start                    # Default: Claude Code on all terminals
# or
vnx start claude-codex       # T1: Codex CLI, T2: Claude Code
```

You now have four terminals:
- **T0** (top-left): Orchestrator — plans work, reviews receipts, never writes code
- **T1** (top-right): Worker Track A — implementation
- **T2** (bottom-left): Worker Track B — tests and integration
- **T3** (bottom-right): Worker Track C — review and security analysis

## 3. Describe the Feature to T0

In the T0 terminal, describe what you want:

```
I need JWT authentication for our Express API:
- Login endpoint with email/password
- Token generation and refresh flow
- Auth middleware for protected routes
- Full test coverage
- Security review of the token handling
```

T0 breaks this into a feature plan with scoped PRs and explicit dependencies:

```markdown
## PR-1: JWT Auth Middleware and Token Service (Track A)
Gate: implementation | Priority: P1 | Dependencies: none
Scope: Token generation, validation, refresh logic, auth middleware

## PR-2: Auth Test Suite (Track B)
Gate: implementation | Priority: P1 | Dependencies: PR-1
Scope: Unit tests for token service, integration tests for middleware

## PR-3: Security Review of Token Handling (Track C)
Gate: review | Priority: P1 | Dependencies: PR-1
Scope: Token storage, expiry, refresh rotation, common JWT pitfalls
```

## 4. Promote and Approve Dispatches

T0 stages dispatches for each PR. You review and promote:

```bash
vnx staging-list              # See what's queued
```

Press `Ctrl+G` to open the dispatch queue popup. Each dispatch shows:
- **Track**: Which terminal gets it (A → T1, B → T2, C → T3)
- **Priority**: P0 (critical) through P3 (low)
- **Gate**: What quality standard applies
- **Scope**: Exactly what the agent should do

Press `A` to accept a dispatch. The dispatcher routes it to the assigned terminal.

## 5. Parallel Execution

All three terminals work simultaneously:

```
T1 (Track A): Implementing JWT token service and auth middleware
              Creating src/auth/token-service.ts, src/middleware/auth.ts
              Writing login endpoint at POST /api/auth/login

T2 (Track B): Waiting for PR-1 to complete (dependency)
              T0 will dispatch when PR-1 passes its gate

T3 (Track C): Waiting for PR-1 to complete (dependency)
              T0 will dispatch when PR-1 passes its gate
```

While agents work, you monitor from T0:

```bash
vnx status                    # Terminal states, queue depth, open items
vnx cost-report               # API spend per agent
```

## 6. Receipt Processing

When T1 finishes, it writes a structured report. The receipt processor automatically:
1. Parses the report into an NDJSON receipt
2. Runs quality advisory checks (file sizes, complexity)
3. Delivers the receipt to T0

T0 sees:

```json
{
  "event": "task_receipt",
  "track": "A",
  "status": "success",
  "summary": "JWT token service, auth middleware, and login endpoint implemented",
  "files_changed": ["src/auth/token-service.ts", "src/middleware/auth.ts", "src/routes/auth.ts"],
  "metrics": { "lines_added": 247, "files_created": 3 }
}
```

## 7. Quality Gate Check

Before dispatching dependent work, T0 runs the gate:

```bash
vnx gate-check --pr PR-1
```

The gate evaluates deterministically (no LLM judgment):
- File size limits
- Shell syntax (`bash -n`)
- Open blocker count
- Contract assertions (if defined in the dispatch)

Verdict: `APPROVE`, `HOLD`, or `ESCALATE`.

On `APPROVE`, T0 promotes the PR-2 and PR-3 dispatches. Now T2 and T3 start working in parallel.

## 8. Context Rotation (If Needed)

If an agent hits 65% context usage mid-task, VNX handles it automatically:

```
T1 context at 67% → agent blocked from tool calls
  → Agent writes ROTATION-HANDOVER.md with progress summary
    → VNX sends /clear to T1
      → Fresh session resumes with handover context + original dispatch
```

The receipt chain links rotation steps. No work is lost.

## 9. Review and Merge

After all tracks complete:

```bash
vnx merge-preflight auth-feature
```

Returns GO or NO-GO based on:
- Git cleanliness (no uncommitted changes)
- Open items resolved (no blockers remaining)
- PR queue status (all PRs gated)
- Quality advisory results

On GO:

```bash
vnx finish-worktree auth-feature --delete-branch
```

This merges intelligence data back to the main repo, removes the worktree, and cleans up.

## 10. What You Get

After the session, the receipt ledger contains a complete audit trail:

```bash
# Every action traced
cat .vnx-data/state/t0_receipts.ndjson | jq '.event'

# Cost per task
vnx cost-report

# Session patterns
vnx analyze-sessions
```

Each code change traces back to: a dispatch (what was requested), a human approval (who authorized it), an agent execution (what was produced), and a quality gate verdict (whether it passed).

---

## Key Takeaways

| What happened | Why it matters |
|---------------|---------------|
| T0 broke work into 150-300 line tasks | Each task fits in one context window |
| Three agents worked simultaneously | 3x throughput vs sequential execution |
| Dependencies were enforced | T2 and T3 didn't start until T1's gate passed |
| Every action produced a receipt | Full audit trail for compliance and debugging |
| Quality gates ran deterministically | No LLM judging its own work |
| Worktree isolated the feature | `main` stayed clean throughout |

---

## Starter Mode Alternative

Don't have tmux or want to start simpler? The same feature can be built in starter mode — just sequentially:

```bash
vnx init --starter
# T0 dispatches one task at a time
# Same receipts, same provenance, same audit trail
# Just one agent instead of three
```

When you're ready for parallel execution: `vnx init --operator`.
