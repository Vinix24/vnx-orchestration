# ADR-028 — Target orchestration architecture: folder-per-agent + two-tier ephemeral judge

**Status:** Accepted (target architecture adopted; migration gated per-phase — see Ratification)
**Date:** 2026-07-07
**Decided by:** Operator (Vincent van Deth), ratified in an autonomous session under an explicit overnight operator mandate. Informed by a deep-research pass (110 agents, 27 sources, 21/25 claims confirmed) and a two-round provider panel. **Three usable panelists — deepseek (design), codex (repo-grounding/corrections), kimi-r2 (implementation-grade detail) — converge on the same core architecture (high confidence); glm flaked empty both rounds.**
**Resolves / Cross-refs:** Extends ADR-022 (provider-agnostic skill injection), ADR-011 (manager–worker hierarchy), ADR-012 (hybrid interactive + headless). Reuses the primitives of ADR-005 (NDJSON ledger), ADR-006 (staging→promote human gate), ADR-023 (receipt hash-chain, currently PARTIAL), ADR-026 (per-project store). Supersedes `claudedocs/20260707-ADR-DRAFT-orchestration-target-architecture.md`.

## Context

VNX runs a fixed T0–T3 tmux terminal topology. The target is a **folder-per-agent, provider-agnostic** fabric coordinated by a **two-tier orchestration**: a thin persistent supervisor plus ephemeral, stateless, per-receipt judges. Research confirms the model has real prior art (ESAA event-sourcing single-author pattern), that **tamper-evident AUDIT is VNX's differentiator** (the field ships no tamper-evidence; VNX's hash-chain does), that central coordination dampens error propagation (17.2× → 4.4×), and that the fusion of *folder-per-agent* + *cross-vendor provider-agnostic* exists nowhere else (VNX would be first to build it). Hard caveat: the **migration mechanics have zero external evidence**, so the path is reasoned, not battle-tested — hence phased, reversible, and shadow-validated.

## Decision (target architecture)

### 1. Folder-per-agent (the fusion)
Each agent = `agents/<name>/` with a `CLAUDE.md` (role) + `config.yaml` (provider binding + scoped permissions). Builds on the EXISTING `agents/` folder. Reuses the skill-frontmatter permission primitive + the business-folder isolation code (allowed/denied paths + scope + runtime guard), promoted from pilot to the registry contract. Delivery stays ADR-022 injection (provider-agnostic), plus a LiteLLM-style `provider.default` field the door reads at dispatch. **No parallel dispatch path: the existing door (`compile_plan` + `ExecutionPermit` + lane) is EXTENDED** to read agent definitions and emit decision-receipts.

`config.yaml` (kimi-r2, concrete): `agent_id`, `name`, `role`, `governance_profile`, `provider`, `model`, `isolation` (scope_type + allowed/denied_paths), `permissions` (allowed/denied_tools + bash_allow/deny_patterns), `skills[]`. Non-claude CLIs (codex/kimi/glm) honour no Claude permission blocks → permissions enforced as a plain-text guard + a `PreToolUse` hook enforcing `denied_tools` / `bash_deny_patterns`.

### 2. Two-tier orchestration
- **Thin persistent Opus supervisor** — interactive (auto-loads CLAUDE.md, hence lean + canonical, see phase 1). Registers work and escalates only on exception.
- **Ephemeral stateless judges** — opus-tmux-spawn / codex / kimi (NEVER `claude -p`). Spawn per receipt, read the fabric, decide APPROVE / REJECT / DISPATCH-NEXT / REWORK, write a hash-chained decision-receipt (`decided_by`, `receipt_under_review`, `verdict`, `evidence`), then die.
- **Cheap DETERMINISTIC fast-path** — risk ≤ 0.3 and no blockers → no judge spawn (multi-agent ≈ 15× tokens; do not spawn a judge for a one-liner). Opt-in resume token for continuity threads.
- **Governance reconciliation (codex, critical):** the fast-path + judges are GOVERNED LOCAL DECISIONS with human-in-the-loop at the final ACTION boundary (ADR-006 promote gate stays), NOT an autonomous execution bypass. Hard constraints stay deterministic / pre-LLM; LLM judges make only soft choices AFTER a fresh snapshot.

### 3. Fleet-addressable agents
Central registry `~/.vnx-data/agent-registry/agents.json` (logical name → physical home dir + binding) + `vnx agent register` + dispatch-into-home (cross-project: vnx-dev → business-os for a linkedin-writer). The terminal/pool worker-registry stays for execution slots; the agent-registry is additive. Structurally fixes the "skill not invocable from another project" gap.

### 4. Reliability
SQLite stays a PROJECTION under the NDJSON ledger, NOT the SSOT. "Fabric 100% reliable" = a narrow synchronous decision-write transaction + ledger append, read-your-writes on the decision path (a Jepsen stale-read is the primary threat). Idempotency via `receipt_under_review` as the dedup key; the hash-chain detects lost / double / out-of-order. **Codex catch: make an "unknown" identity INVALID on the decision path** (quarantine legacy/ghost events) — otherwise a judge could decide under an unknown identity.

### 5. Audit
Every decision is a first-class receipt via the canonical append facade (`append_receipt_payload`), hash-chained UNIFORMLY. Closes decision-diffusion (`decided_by`) + evidence-fragmentation (full fabric read per spawn) + responsibility-ambiguity (hash-chain over depth > 1). **Codex honesty: ADR-023's hash-chain is currently PARTIAL** — the target REQUIRES completing it, not pretending it is already universal.

### 6. Migration (6 phases, each feature-flagged + reversible, ADR-012-compliant)
- **Phase 0 — fabric hardening (start NOW, PREREQUISITE):** retire the orphaned shared `~/.vnx-data/state/` (to `.pre-retirement`, 30-day hold), remove dual-write paths (`VNX_USE_CENTRAL_DB`, `VNX_STATE_DUAL_WRITE_LEGACY`), enforce the `VNX_DATA_DIR_EXPLICIT=1` guard globally + a startup check that aborts when the resolved data-dir does not match `.vnx-project-id`, complete the hash-chain (judge decisions exclusively via `append_chained_entry`, because `emit_dispatch_receipt` does not chain), make unknown-identity invalid. New command **`vnx fabric-audit`** (checks: no shared-store rows, all ledgers per-project, hash-chain integrity, no projection contradictions) run green before every T0 session. This is a correctness bug NOW, not just a future blocker. Rollback: `VNX_DISPATCH_LEGACY=1` + dual-write back.
- **Phase 1 — agent-folder fusion (backward-compatible):** extend `config.yaml` with provider/model/permissions/skills; old configs keep working with defaults. `agent_resolver.py` (local `agents/` or registry). The phase-1 role canonicalization is step 1 here. Rollback: `VNX_AGENT_FOLDERS=0`.
- **Phase 2 — decision-judge SHADOW mode (the safety valve):** `VNX_DECISION_JUDGE_SHADOW=1` spawns a judge that writes its decision as a `decision_advisory` receipt; T0 reads but does NOT act on it and decides itself. A comparator logs divergence. Validates the judge against the human before trusting it, at zero risk. Rollback: flag off, advisories ignored.
- **Phase 3 — judge fast-path:** deterministic classifier handles trivial receipts; non-trivial go to the judge. Rollback: `VNX_DECISION_FAST_PATH=0` → all to T0.
- **Phase 4 — human-on-the-last-set:** `VNX_DECISION_JUDGE_ENABLED=1` makes judge decisions binding for routine work; T0 reviews exceptions; an explicit `operator_approval` receipt for sensitive actions (merge, close track, override). Rollback: flag off, T0 resumes full decision authority.
- **Phase 5 — fleet-addressable agents:** central registry + cross-project dispatch on. Rollback: `VNX_AGENT_REGISTRY_ENABLED=0` → local only.
- No phase retires interactive tmux (ADR-012).

### 7. Adopt / Avoid (per framework, against VNX constraints)
- **ADOPT (patterns, not products):** Claude Code subagent-folder convention (constrained), OpenAI Agents SDK + LiteLLM ROUTING pattern (not the SDK), ESAA read-model/projection pattern, AgentOrchestra external-plan-state.
- **AVOID:** LangGraph (in-memory SSOT), AutoGen (SDK-coupled, conflicts ADR-003), CrewAI (weak audit), managed-agents-as-substrate (cloud state, conflicts local-first), "everything via subagents" (depth-1, no receipts).

## Worked example (marketing-manager)
Orchestrator decides blog-vs-LinkedIn → research-analyst dispatch (cross-project, reads Supabase business data) → judge approves research → parallel fan-out to linkedin-writer + blog-writer (business folder, dispatch-into-home) → judges approve both → orchestrator closes the track. Six hash-chained receipts, fully auditable. Business data cloud (read), governance decisions local (write) — the two-tier data split works here exactly.

## Consequences
- VNX becomes the first system to fuse folder-per-agent AND cross-vendor provider-agnostic delivery, with tamper-evident audit the field leaves open (a brand differentiator: data sovereignty down to the evidence layer).
- Cost: hierarchy overhead + per-spawn state reconstruction. The deterministic fast-path is not optional but economically necessary.
- Risk: early adopter of a single-author pattern (ESAA); migration reasoned, not battle-tested. Hence phased + reversible + shadow.

## Ratification (2026-07-07, autonomous session)

Ratified under an explicit overnight operator mandate. The four open operator-decisions from the draft are resolved with best-effort defaults below. **Two carry a `[FLAG — morning review]` marker: they are defaults I chose, not operator-confirmed, and are cheap to revise.**

1. **Where do operator-policies A1–E6 live — canonical role vs project-local?**
   **Default (chosen):** the A1–E6 policies live in the **canonical** `role-orchestrator.md` (single SSOT, distributed by `vnx role sync`), not project-local — they are fleet-wide governance rules and a per-project copy is exactly the drift phase 1 removes. Project-specific deviations, when genuinely needed, go in a small project-local supplement file that the thin `terminals/T0/CLAUDE.md` `@import`s *after* the canonical role, so overrides are explicit and auditable rather than forked. **`[FLAG — morning review]`** — confirm the canonical-with-optional-supplement model vs a stricter canonical-only rule.

2. **Pinned-daemon structural friction → its own ADR?**
   **Default (chosen):** YES. Kept out of this ADR to preserve focus; recorded as a follow-up ADR candidate (ADR-029) covering the pinned-daemon-alongside-door structural friction (see memory `dispatch-structure-not-consolidated-friction`). **`[FLAG — morning review]`** — confirm splitting vs folding into this ADR.

3. **Approve starting Phase 0 NOW?**
   **Resolved: YES — approved and largely landed.** The overnight mandate is the approval. Phase-0 progress:
   - `VNX_STATE_DIR` repo-local footgun removed from the T0 role/template/skill/docs (PR #1043), plus a durable `state-pin-gate` CI job so it cannot creep back in any shipped surface (PR #1047).
   - `vnx fabric-audit` shipped (PR #1045) and hardened to weigh `.db-wal`/`.db-shm` sidecar mtimes, so a recent open on a stale `.db` no longer reads as safe-to-retire (PR #1047).
   - The orphaned shared `~/.vnx-data/state/` **retired** to `.pre-retirement` (reversible, 30-day hold) after an `lsof` no-live-writer check; `vnx fabric-audit` is **GREEN**.
   - **Remaining Phase-0 items (parked for a supervised governed dispatch, decision-path):** hash-chain completion default-on (judge decisions exclusively via `append_chained_entry`) — blast-radius on fabric-audit check C requires epoch rotation first — and unknown-identity invalidation. Phase 0 is a correctness fix independent of the future architecture.

4. **Is panel coverage complete?**
   **Resolved: YES — no re-run needed.** deepseek + codex + kimi-r2 converge (high confidence); glm is dead for long synthesis (flaked empty twice, persistent). The draft's own conclusion, carried forward.

### Ratification status
Target architecture (§1–§7) and the migration sequence (§6) are **Accepted**. Adoption remains **gated per-phase**: each phase ships behind its own feature flag with a rollback, and Phases 2→4 (which touch the decision path) each require an explicit operator go/no-go at the transition, per the human-in-the-loop principle. Nothing here authorizes an autonomous execution bypass.
