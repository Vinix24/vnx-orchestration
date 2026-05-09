# ADR-011 — Manager+Worker Hierarchy with Explicit Depth>1 (vs Subagents Depth-1)

**Status:** Accepted (primary architecture decision) — with a **Conditional / Pilot** sub-decision (2026-05-09 amendment) for tactical subagent use inside VNX workers, narrowed to read-only parallel-fanout via `VNX_T1_TACTICAL_SUBAGENTS=1` flag. The original Tentative section has been resolved by the Wave 0.5 subagent research dispatch (`claudedocs/2026-05-09-vnx-subagent-tactical-research.md`).
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

### Conditional sub-decision — Tactical subagents inside VNX workers (Status: **Conditional / Pilot**, 2026-05-09)

VNX permits the tactical use of Claude Code subagents *inside a single VNX worker dispatch* under the following constraints. The May 2026 platform shift (v2.1.117 added `agent_id`/`agent_type` to all hook events; v2.1.121 made `PostToolUse.hookSpecificOutput.updatedToolOutput` honor every tool, not only MCP) closed the primary objection — that subagent activity was opaque to the parent worker and therefore broke the glass-box audit property. With deliberate hook instrumentation, the worker can observe and redact every subagent tool call, fold subagent provenance into its own receipt, and retain VNX's "one dispatch = one receipt" contract.

#### Permitted shape: read-only parallel-fanout only

A worker MAY spawn subagents only when ALL of the following hold:

1. **Read-only tool budget.** The subagent's `allowed-tools` list contains only `Read`, `Grep`, `Glob`, `Bash` (read-only commands like `rg`, `grep`, `find`, `cat`, `wc`). It MUST NOT include `Edit`, `Write`, `NotebookEdit`, MCP write tools, or any command that mutates `.vnx-data/`, `runtime_coordination.db`, `quality_intelligence.db`, `dispatch_tracker.db`, the worktree filesystem outside ephemeral scratch, or any git ref.
2. **Independent fanout (≥4 branches recommended, ≥2 minimum).** Subagent N+1 MUST NOT depend on subagent N's output. Sequential dependent searches do not qualify; the worker performs them with direct tool use.
3. **Bounded summary contract.** Each subagent returns a structured summary (≤2000 tokens) — list of matches, count, file paths, line ranges — never raw tool output passed through.
4. **Worker-side hook instrumentation MUST be active.** The worker's process hook configuration (provided by `subprocess_dispatch.py` via injected `--settings` or equivalent) installs:
   - a `PreToolUse` hook that blocks any disallowed tool with exit code 2 (defense-in-depth against an out-of-band subagent role definition);
   - a `PostToolUse` hook that forwards `{agent_id, agent_type, tool_name, duration_ms, input_hash, output_hash}` to `.vnx-data/events/T{n}.ndjson` — same NDJSON the worker already uses;
   - a `PostToolUse` hook that applies secret redaction via `hookSpecificOutput.updatedToolOutput` (strips API keys, OAuth tokens, customer business content patterns).
5. **Receipt provenance.** The worker's final receipt MUST include a `subagents` array with one entry per spawned subagent: `{agent_type, tool_call_count, total_duration_ms, summary_hash}`. The worker's existing `t0_receipts.ndjson` line gains this field; downstream readers ignore unknown fields, so this is forward-compatible.
6. **No subagent-of-subagent.** The worker MUST NOT spawn a subagent that itself spawns subagents. Claude Code enforces this at the platform level (depth-1 limit per v1.0.64), but the worker's own role spec also asserts it for clarity.
7. **No cross-dispatch state writes.** A subagent MUST NOT call `dispatch_register`, `append_receipt`, `open_items_manager`, or any VNX state-mutating CLI. Its role definition omits these.

#### Forbidden shapes (use a separate VNX dispatch instead)

- Subagent that edits source files. (Use a new T1 dispatch.)
- Subagent that runs gates (codex_gate, gemini_review). (Use the existing review-gate flow; gates are siblings of the work, both children of T0.)
- Subagent that spans more than one worker dispatch lifecycle. (Subagents are ephemeral and tied to one parent worker; no cross-dispatch handoff.)
- Subagent that consumes a budget the worker has not pre-provisioned. (Subagent token cost counts against the worker's dispatch budget; if exceeded, the worker fails closed.)
- Subagent in T0. (T0 does not implement code; T0 dispatches workers. T0 may use direct Read/Grep but does not need parallel fanout — its work is dispatch-decision, not exploration.)

#### Pilot scope (Q3 2026)

The first deployment is a single pilot, not a general rollout:

- **Pilot terminal:** T1 only.
- **Pilot task:** parallel-search across `code_snippets` FTS5 table during open-item triage and during multi-file refactor research. Out of scope for the pilot: codex-fix-codex round loops (those stay sequential).
- **Pilot env flag:** `VNX_T1_TACTICAL_SUBAGENTS=1`. Default unset = forbidden.
- **Pilot duration:** 4 weeks (or 25 dispatches with subagent use, whichever comes first).
- **Pilot telemetry:** every dispatch using subagents emits a row to `.vnx-data/state/runtime_coordination.db` table `subagent_pilot_log` with columns `(dispatch_id, agent_count, total_subagent_tokens, wall_clock_savings_ms, summary_quality_score, audit_completeness_bool)`.
- **Success criteria for promotion to general rollout:**
  - ≥30% wall-clock reduction vs the same-task baseline (measured against direct tool use on matched tasks).
  - 100% audit completeness — every subagent's `agent_id`/`agent_type` and summary hash present in the worker's receipt.
  - Zero leaks: no subagent recorded a tool call to a forbidden tool (PreToolUse hook never fired with exit code 2 for that reason ≥1 time per dispatch).
  - Operator review approves at week 4.
- **Failure / rollback criteria:**
  - Any audit-completeness failure → roll back the pilot, re-publish ADR-011 with status downgraded to "Rejected" for that section.
  - Any secret leakage incident → immediate rollback, security review.
  - Wall-clock savings < 15% on pilot tasks → keep pilot in monitor mode but do not promote.

#### Reasoning for Conditional rather than Accepted (general)

A general "subagents are allowed inside workers" policy is too broad for VNX's audit constraints. The risk surface — receipt-provenance leakage, untracked token cost, evidence trails that vanish when the parent exits, operator confusion about whether a finding came from VNX governance or from a black-box subagent — is real and was the basis for the 2026-05-09 default ban. The pilot scope above narrows usage to the one shape (read-only parallel fanout) where the cost/benefit calculation is documented and the platform's new hook surface (v2.1.117 `agent_id`, v2.1.121 universal `updatedToolOutput`) makes audit instrumentation feasible. Promoting from Conditional to Accepted requires evidence from the pilot, not theoretical argument.

#### Reasoning for Conditional rather than Rejected

The Anthropic-documented Django parallel-search example (4 Explore subagents, 3m40s vs 14min sequential, better results — `code.claude.com/docs/en/sub-agents`) maps directly onto VNX's largest fanout-search workload: searching the 855k-snippet `code_snippets` FTS5 table for cross-cutting refactor evidence. A blanket Reject would forfeit a real wall-clock win on a real recurring task. The platform's hook instrumentation now lets us capture that win without sacrificing audit completeness, *if* the wiring is done right. The pilot tests whether "if the wiring is done right" holds in practice.

#### Citations

- Subagent depth-1 + condensed-summary return: `code.claude.com/docs/en/sub-agents`; v1.0.64 release note (Jul 30 2025); `claudedocs/2026-05-09-vnx-platform-features-verification.md` Claim 1.
- `agent_id`/`agent_type` in hook payloads: v2.1.117 changelog (April 2026); `code.claude.com/docs/en/hooks`; `claudefa.st/blog/guide/changelog`.
- Universal `updatedToolOutput` for all tools: v2.1.121 release notes (May 2026); `wotai.co/blog/claude-code-2-1-121`; `code.claude.com/docs/en/hooks`.
- Parallel Explore subagents wall-clock benchmark: `code.claude.com/docs/en/sub-agents` (Django 4-fanout example, 3m40s vs 14min sequential, surfaced via `nimbalyst.com/blog/claude-code-subagents-guide` 2026 guide).
- Companion research: `claudedocs/2026-05-09-vnx-subagent-tactical-research.md` (full Q1-Q4 analysis with sources).

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
