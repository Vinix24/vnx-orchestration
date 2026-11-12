# Known Limitations & Scope

**Verified against `origin/main` at c41411fea on 2026-11-12.** `VERSION` says 1.8.0. Work merged after it is marked "on main, not yet released" (`CHANGELOG.md`, `changelog.d/`). Live per-feature status lives in the tracks database (`vnx objective list`, ADR-047), not in `ROADMAP.yaml`.

## Tested

**Default lane** (single-entry dispatch door, ephemeral per-dispatch workers):
- The dispatch door (`vnx dispatch`) as the single entry point for every dispatch (ADR-024).
- `claude_headless` (`claude -p` via envelope, own git worktree per dispatch) as the only Claude worker lane, subscription-preserving.
- Review seats: the default stack `codex_gate,deepseek_gate` (since 2026-11-11), one distinct reviewer per seat, a rejection from any reviewer blocks the merge (ADR-008, ADR-037). Takeover chain `codex_gate,deepseek_gate,claude_gate`, with DeepSeek on API credit; `glm_gate` is outside the default chain.
- Horizon (`vnx horizon`) as the roadmap, tracks and deliverables planning layer, with the plan-first gate: one seat, a second only on BLOCK (`docs/core/HORIZON_LIFECYCLE.md`).
- Open outcomes: a dispatch is finished only with an accept or reject from a T0 (`docs/core/DISPATCH_RULES.md` section 13).
- Receipt-based cost observability with git provenance per receipt, and a start line and end line per dispatch (ADR-043, ADR-045).
- T0 context guard and rotation (`docs/operations/T0_CONTEXT_ROTATION.md`) and the context-rotation plugin (`packages/claude-code-toolkit/context-rotation/`, its README names its own limits).
- The provider switch (`vnx providers`, `docs/core/PROVIDER_SWITCH.md`, ADR-044). `claude` and `codex` cannot be switched off, and the switch does not cover a project's own application code (`vnx providers check` is the hook for that).

**Terminal-pinned path** (`scripts/lib/subprocess_dispatch.py`, the historical layout and the rollback route):
- T0 plus Track A / B / C workers and T-MANAGER. The per-terminal selector `VNX_ADAPTER_T{n}` is gone for T1-T3 since 2026-10-27; only T0 still opts in with `VNX_ADAPTER_T0=subprocess`.
- Single-repository workflows, one VNX instance per project.

## Limits of learning and dreaming

- **Proposal-only.** The learning loop is shadow by default and a queue at most (`VNX_LEARNING_LOOP_PERSIST`, `VNX_LEARNING_LOOP_QUEUE`). Nothing it finds becomes a rule without my decision (ADR-065). Operator steering and dream consolidation follow the same boundary (ADR-072, ADR-019). This work is on main, not yet released.
- **The loop is dormant unless switched on.** The dream scheduler is off by default (`VNX_DREAM_SCHEDULER_ENABLED=0`). The dream-liveness probe reports whether a night produced output; it does not make one happen (ADR-066).
- **The effect window is not closed.** The A/B instrument for intelligence injection exists (`VNX_INTEL_AB_TEST`, off by default, ADR-069). Until a window has run to its sample size, I have no measured effect of injection and make no claim about one.
- **Failure classes are text rules.** The classifier matches prefixes and substrings that other modules write. A new wording falls into `failed` or `unclassified` and shows up as lower coverage (ADR-065).
- **The content boundary** (`content_class`) is read by the conversation analyzer and the dream consolidation only, not by the receipt classifier, the tagger or the scout (OI-2172). It is not a general guarantee about what reaches a non-claude provider.

## Limits of harness portability

- **A live T0 on another harness is not proven.** `vnx role sync --apply` mirrors the canonical orchestrator role into `.claude/terminals/T0/role-orchestrator.md`, `AGENTS.md` (Codex) and `GEMINI.md` (Gemini, Kimi) (`tests/test_role_sync.py`). That a Codex or Kimi session then orchestrates a project to the end has not been shown on this project.
- **T0 runs Claude Opus** via Claude Code. A hook blocks T0 from spawning subagents (`scripts/hooks/pretooluse_t0_no_subagents.py`), and that hook is a Claude Code mechanism.
- **Workers differ per provider.** Claude is the most used lane. Codex and Gemini are run through their CLIs with skills and a context reset. Gemini is a worker lane only and no longer a review gate. Kimi runs through its CLI OAuth path only (`kimi-via-cli-only`). Which provider may serve which lane is `scripts/lib/providers/provider_constraints.yaml`.
- **Per-command denial differs.** `git stash` is denied as a command on the claude and kimi worker lanes. Gemini has no per-command denial, so the door refuses a build dispatch on the gemini lane. Codex has none either and is not refused (`CHANGELOG.md` 1.8.0).

## Not yet tested

- T0 orchestrator with other models (see above).
- Gemini and Kimi at scale.
- Multi-repository orchestration (one T0 coordinating workers across repositories).
- Parallel multi-track execution (Tier 3, designed and not shipped, ADR-020).
- Full public commit history from the earliest iterations (the system started inside a private product repo and the history was squashed when it was split out).
- A tamper-evident ledger by default: the receipt hash chain is available per project and off by default, and the main ledger is unchained (ADR-023).

## By design

- **File-based and local-first.** The filesystem is the message bus. Not designed for distributed networks, and no cloud dependency for orchestration state.
- **Bash/Python prototype (about 8/92 ratio).** About 8% bash and 92% Python (measured 2026-10-18 by line count of tracked `.py` and `.sh` files under `scripts/` and `bin/`). The ratio reflects origin, not preference: VNX started as tmux `send-keys` scripts. New components are written in Python. See [EVOLUTION_TIMELINE.md, Language Evolution](EVOLUTION_TIMELINE.md#language-evolution-why-python-now-dominates).
- **Tmux dependency.** T0 management and the injection path for interactive codex/kimi/gemini terminals use tmux. The tmux-spawn worker lane for Claude was removed on 2026-09-18.
- **Hook-agnostic.** Quality intelligence and usage tracking do not require provider hooks.
- **T0 is a coordinator.** The orchestrator role never writes code; the work goes to workers through dispatches.
- **Checkpoint control.** Workers run provider sessions; mid-session interruption is best-effort, so VNX prefers short dispatches and human review between checkpoints.
- **No inter-agent communication.** Workers do not message each other. All coordination flows through T0 via dispatches and receipts, so that peer-to-peer traffic cannot bypass gates or break the audit trail. A worker may use subagents for supporting research, never for the primary implementation.
