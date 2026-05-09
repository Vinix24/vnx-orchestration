# ADR-011 — Manager+Worker Hierarchy with Explicit Depth>1 (vs Subagents Depth-1)

**Status:** Accepted (primary architecture decision) — with a **Tentative** section labeled "When to tactically use subagents (FUTURE / RESEARCH)" that documents an OPEN QUESTION rather than a recommendation.
**Date:** 2026-05-09
**Decided by:** Operator (Vincent van Deth)
**Cross-references:** ADR-003 (OAuth-only Claude routing, no SDK), ADR-010 (subprocess adapter), ADR-012 (hybrid interactive + headless)

## Context

VNX runs as a multi-terminal orchestration system with one master orchestrator (T0) and N workers (T1, T2, T3, …). Workers are **separate Claude Code processes** with their own `CLAUDE.md`, their own tool budget, their own dispatch envelope, and their own receipt — they are not subagents in the Anthropic Task-tool sense. Workers can themselves dispatch follow-up work (e.g., T1 emitting a follow-up dispatch for T3 review, or a hypothetical sub-orchestrator pool spawning short-lived sub-workers). Hierarchy depth in VNX is therefore explicitly **greater than 1**.

Claude Code natively offers a Task-tool subagent pattern. Per Perplexity verification on 2026-05-09 (`claudedocs/2026-05-09-vnx-platform-features-verification.md`, Claim 1):

- v1.0.64 release note (2025-07-30: "Fixed unintended access to the recursive agent tool") establishes **depth-1 as the explicit, intentional architecture**.
- Subagents return only a condensed summary (1,000–2,000 tokens) to the parent — no recursion path.
- v2.1.117 (April 2026) added `CLAUDE_CODE_FORK_SUBAGENT=1` for forked subagents that inherit full parent conversation context, but forking is still **flat from the parent**, not from a subagent.
- Anthropic's Multiagent Sessions (Managed Agents API beta `managed-agents-2026-04-01`, released 2026-04-01) extends this with a **flat coordinator + 20-agent roster**; depth > 1 is **explicitly ignored** (Claim 3 verification).

A separate platform shift on **2026-05-08** added subagent-level observability events and hook-intervention points (PreToolUse / PostToolUse / Subagent lifecycle hooks) — see WebSearch verification cited below. This shift changes what subagents *expose* to the parent, but does not change their depth-1 nature.

The strategic question for VNX: should T0..Tn be flattened to native subagents (giving up depth>1 but gaining first-class platform tooling), or should the manager+worker-with-depth>1 primitive remain canonical?

## Decision

### Primary decision (Accepted)

VNX's architecture primitive is **separate-agent-per-task** orchestrated through a manager (T0) and worker terminals (T1, T2, T3, …Tn). Workers are first-class processes — each has its own dispatch lifecycle, its own receipt, its own NDJSON event stream (when subprocess-routed), its own lease, and its own audit trail in `t0_receipts.ndjson`. Workers may themselves dispatch sub-tasks (depth>1). This is the canonical primitive. New orchestration patterns extend hierarchy depth — they do not flatten it.

### Tentative section — When to tactically use subagents (FUTURE / RESEARCH)

As of **2026-05-08**, Claude Code subagents now expose:

- Real-time hook events for subagent lifecycle (Subagent start/stop, PreToolUse, PostToolUse) — verified via `code.claude.com/docs/en/hooks` and the May 2026 changelog (Releasebot, Developers Digest "Agent Teams, Subagents, and MCP: The 2026 Playbook").
- `PostToolUse.hookSpecificOutput.updatedToolOutput` — hooks can rewrite tool output for **all tools** (not only MCP).
- `duration_ms` on `PostToolUse` / `PostToolUseFailure` for tool-performance observability.
- Parallel subagent + SDK MCP-server reconnection.
- 27 hook events across 5 categories, with 4 execution types (shell, LLM-evaluated, webhook, subagent-verifier).

This means subagents are no longer the opaque depth-1 black boxes that originally justified their wholesale replacement by VNX workers. Subagents *may* be tactically useful in narrow cases — for example, a quick parallel grep/search inside a single VNX worker dispatch where spawning another full T-terminal dispatch would be wasteful. Such tactical use does **not** compromise VNX's separate-agent-per-task primitive because the subagent runs **inside** a single VNX worker dispatch and reports back to that worker (the VNX dispatch boundary is preserved).

**However, when exactly to use a tactical subagent vs. spawn another VNX worker dispatch needs further research before any guidance can be Accepted.** Open questions:

- What triggers (LOC, parallelism, latency budget, evidence-needed) tip the choice toward a tactical subagent vs. a new VNX dispatch?
- Do subagent observability events (PreToolUse, PostToolUse) get captured in VNX's NDJSON ledger or do they vanish when the parent worker exits?
- Does using subagents inside a worker break the "one dispatch = one receipt" provenance contract, or can the worker fold subagent evidence into its own receipt cleanly?
- Does the depth-1 limit matter when the subagent is one level *under* a VNX worker (effective system depth = 2+), or is the depth-1 floor itself a problem?

This section therefore documents an **OPEN QUESTION** and the answer is deliberately deferred. A research dispatch (Wave 0 doc-hygiene track or Wave 4 of the strategic replan, see `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §8) will produce concrete guidance. Until that research lands, the default is: **do not use Claude Code subagents inside VNX workers**; spawn another VNX dispatch instead.

## Reasoning

1. **Depth>1 is the moat that survives the platform shift.** Per the strategic replan §4, M5: "T0..Tn manager+worker hierarchy" is one of seven moats. Subagents are depth-1 only; Multiagent Sessions are flat coordinator+20-roster (also depth-1). VNX is the only one shipping a >1 hierarchy primitive. Flattening to subagents would eliminate the moat for an undocumented gain.

2. **Separate processes give isolation that subagents structurally cannot.** Each VNX worker has its own subprocess, its own filesystem cwd, its own tool budget, its own lease in `runtime_coordination.db`, and its own receipt in `t0_receipts.ndjson`. A subagent shares the parent's context window (modulo summary compression) and cannot be killed independently. For long-running gate-fix-gate loops or for adversarial dual-LLM review (codex_gate + gemini_review), separate processes are the only correct primitive.

3. **The OAuth-only Claude routing constraint (ADR-003) reinforces depth>1.** VNX is forbidden from using the Anthropic SDK and routes Claude through `claude -p --output-format stream-json` subprocesses. A subagent invoked inside such a subprocess is fine; a VNX worker invoked as a subagent of another VNX worker would require either an SDK call or a nested CLI invocation — the latter is exactly what `subprocess_dispatch.py` already does, by design (ADR-010). The CLI-subprocess path *is* the depth>1 mechanism.

4. **Receipts and dispatch envelopes are the audit primitive.** Per `CLAUDE.md` (project root): "Every change goes through a dispatch. No cowboy commits." Subagents inside a parent dispatch are part of one dispatch, one receipt — they are not auditable as independent units of work. VNX's audit ledger (M1 in the replan) requires each work unit to have its own receipt; depth>1 dispatch chains preserve that, subagent-trees do not.

5. **The May 2026 platform shift is additive, not replacement.** The new hook surface (verified via WebSearch on 2026-05-09; sources: code.claude.com/docs/en/hooks, releasebot.io/updates/anthropic/claude-code, Developers Digest "Claude Code Agent Teams, Subagents, and MCP: The 2026 Playbook", ofox.ai "Claude Code: Hooks, Subagents, and Skills — Complete Guide", boringbot.substack.com "Claude Code: Skills, Subagents, Hooks, Plugins, and Harnesses") gives subagents new *tactical* surface — observable lifecycle, parallel MCP reconnect, output-rewriting hooks — but does not give them depth>1 forking. The moat is unchanged; only the tactical surface inside a VNX worker dispatch has expanded.

6. **Tactical-subagent guidance is not yet ready to be Accepted.** Until a research dispatch answers the four open questions above, premature guidance ("use subagents for parallel search inside T1") would risk: receipt-provenance leakage, cost untracked by `t0_receipts.ndjson`, evidence trails that vanish when the parent exits, and operator confusion about whether a finding came from VNX governance or from a black-box subagent. The Tentative status is the honest reflection of the current state.

## Consequences

### Accepted

- T0..Tn architecture with depth>1 stays canonical. New features extend hierarchy (e.g., sub-orchestrator pools, per-track managers) — they do not flatten to a subagent roster.
- The dispatch envelope, receipt, and lease primitives in `subprocess_dispatch.py` (ADR-010) remain the spawn mechanism for any depth>1 worker.
- Future "manager-of-managers" patterns (e.g., a track-A manager that itself dispatches T1a / T1b / T1c) are explicitly in scope and do not require a new primitive — they reuse the existing dispatch path.
- Cross-vendor adversarial gating (codex + gemini reviewing Claude work, M3 in the replan) remains a depth>1 use case: gate dispatches are siblings of the work dispatch, both children of T0.

### Rejected

- "Flatten T0..T3 to native subagents." Rejected because it eliminates moat M5, breaks receipt-per-dispatch provenance, and gives up cross-vendor gating that subagents cannot host (Anthropic will not ship a Codex-based subagent).
- "VNX is just a glorified subagent dispatcher." Rejected as a framing — VNX is a separate-agent-per-task orchestration platform; subagents are an internal tool within one such agent, not a substitute for the agent itself.
- "Adopt Multiagent Sessions coordinator pattern as the canonical T0 replacement." Rejected — coordinator is depth-1 and Anthropic-hosted; both violate VNX's local-first OAuth-only constraints (ADR-003).

### Open (Tentative — research required)

- **When (if ever) to tactically use Claude Code subagents inside a single VNX worker dispatch.** A research dispatch in Wave 0 or Wave 4 (per the 2026-05-09 strategic replan) will answer the four open questions and either:
  - publish a "tactical subagent playbook" ADR amendment (status → Accepted for that section), or
  - confirm "do not use subagents inside VNX workers" as the permanent default (status → Rejected for that section).
- Until that research lands, the default is **do not use subagents inside VNX workers**. Operators may experiment in throwaway branches but must not ship subagent-using code through the dispatch flow without a follow-up ADR amendment.

## See also

- `claudedocs/2026-05-09-vnx-platform-features-verification.md` — Claims 1 (subagent depth-1) and 3 (Multiagent Sessions flat coordinator)
- `claudedocs/2026-05-09-vnx-strategic-replan-proposal.md` §4 (moat M5) and §5 (no flatten-to-subagents adoption)
- ADR-003 — OAuth-only Claude routing (no SDK)
- ADR-010 — Subprocess adapter as the depth>1 spawn mechanism
- ADR-012 — Hybrid interactive + headless execution (every depth>1 worker can run in either mode)
- WebSearch verification 2026-05-09: code.claude.com/docs/en/hooks; releasebot.io/updates/anthropic/claude-code; Developers Digest "Claude Code Agent Teams, Subagents, and MCP: The 2026 Playbook"; ofox.ai "Claude Code: Hooks, Subagents, and Skills — Complete Guide (2026)"; boringbot.substack.com "Claude Code: Skills, Subagents, Hooks, Plugins, and Harnesses for Production Multi-Agent Workflows"
- `CLAUDE.md` (project root) — VNX governance system overview, dispatch-and-receipt rules
