# Who Should Use VNX

## Target Audience

### 1. Solo Developers Managing Multiple AI Agents

**Profile**: You already use Claude Code or Codex CLI daily. You've tried running 2-3 terminals on the same project and hit merge conflicts, duplicated work, or lost track of which agent did what.

**What VNX gives you**:
- Scoped dispatches so agents don't step on each other
- Receipt-based audit trail showing exactly what each agent produced
- Cost tracking per task, not just per session
- Context rotation so long tasks don't lose progress

**Start with**: Starter mode (single terminal with governance), then upgrade to operator mode when you want parallel agents.

### 2. Small Engineering Teams (2-5 people)

**Profile**: Your team uses AI coding agents individually. You want to coordinate AI-assisted work across branches, features, and team members without creating chaos in the codebase.

**What VNX gives you**:
- Feature-level orchestration with worktrees: each feature gets an isolated branch
- Cross-agent provenance: every code change traces to a dispatch, terminal, and approval
- Quality gates that enforce team standards regardless of which agent or model produced the code
- Session intelligence that reveals which models and task types perform best

**Start with**: Operator mode with a single project, then expand to worktree-based feature isolation.

### 3. Compliance-Aware Organizations

**Profile**: You work in regulated industries or on projects where you need to demonstrate that AI-generated code was reviewed, approved, and traceable. You need audit trails, not just git history.

**What VNX gives you**:
- Append-only NDJSON ledger with structured receipts for every agent action
- Provenance chain: dispatch → human approval → agent execution → quality gate → receipt
- Git provenance tracking (`CLEAN`, `DIRTY_LOW`, `DIRTY_HIGH`) per receipt
- Mode transparency: `vnx status` always shows what governance controls are active
- No silent degradation: if a governance check can't run, the command fails explicitly

**Start with**: Operator mode with full governance controls enabled.

## Use Cases

### Feature Development with Parallel Agents

**Scenario**: You need to implement a feature that touches frontend, backend, and tests. Instead of one agent doing everything sequentially, you dispatch scoped tasks to three agents working in parallel.

**VNX workflow**:
1. T0 (orchestrator) creates dispatches: Track A (implementation), Track B (tests), Track C (review)
2. Human reviews and promotes each dispatch
3. T1, T2, T3 execute their assigned tracks simultaneously
4. Quality gates validate each track's output before merge
5. Receipts capture the full story: what was planned, what was executed, what passed

**Governance benefit**: No agent can merge without passing gates. If T1's implementation breaks T2's tests, the gate catches it before merge.

### Long-Running Research and Refactoring

**Scenario**: You're refactoring a module with 50+ files. A single agent session will exhaust its context window before finishing.

**VNX workflow**:
1. Break the refactoring into 150-300 line dispatches
2. Each dispatch is a self-contained unit of work
3. When context fills up, VNX rotates automatically: handover → clear → resume
4. The receipt chain links all rotation steps into a continuous audit trail

**Governance benefit**: Progress is never lost. Each completed dispatch is a checkpoint with a receipt, a git commit, and a quality verdict.

### Multi-Model Evaluation

**Scenario**: You want to compare how Claude, Codex, and Gemini handle similar tasks to find the best model for different work types.

**VNX workflow**:
1. Configure terminals with different providers: `vnx start claude-codex` or `vnx start full-multi`
2. Dispatch comparable tasks to different terminals
3. Session intelligence aggregates performance data across models
4. `vnx cost-report` shows cost-per-task by provider

**Governance benefit**: Same governance controls apply regardless of model. Quality gates don't care which LLM produced the code — they evaluate the output.

### Onboarding a New Team Member to AI-Assisted Development

**Scenario**: A new developer joins your team and hasn't used multi-agent workflows before. You want them productive without the full operator complexity.

**VNX workflow**:
1. New developer starts with `vnx init --starter` — single terminal, sequential dispatch
2. They learn the dispatch → execute → receipt cycle with guardrails
3. `vnx demo` lets them see how operator mode works with sample data
4. When ready: `vnx init --operator` upgrades to the full multi-agent grid

**Governance benefit**: Starter mode isn't a toy — it runs the same runtime with real receipts and provenance. Skills learned in starter mode transfer directly to operator mode.

## Who Should NOT Use VNX

- **Teams that don't use AI coding agents** — VNX orchestrates AI agents; it doesn't replace them
- **Projects where a single agent terminal is always sufficient** — governance overhead isn't justified for simple, single-agent workflows
- **Teams looking for a general-purpose AI framework** — VNX is purpose-built for software engineering; use CrewAI or LangGraph for research, content, or data analysis workflows
- **Organizations that need cloud-native distributed agents** — VNX is local-first and file-based; it doesn't deploy to cloud infrastructure
