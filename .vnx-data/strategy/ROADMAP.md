# VNX Roadmap — Committed Development Sequence

**Version:** 1.1 (renamed from MASTER_ROADMAP 2026-05-06)
**Date:** 2026-05-02
**Author:** T0
**Owner:** Vincent van Deth (operator)
**Companion machine-readable:** `.vnx-data/strategy/roadmap.yaml`
**Related:** `.vnx-data/strategy/backlog.yaml` (unscheduled queue), `.vnx-data/state/PROJECT_STATE_DESIGN.md` (the design that justifies this folder), `claudedocs/PRD-VNX-UH-001-universal-headless-orchestration-harness.md` (the strategic vision)

---

## Roadmap vs backlog — the distinction

| | `roadmap.yaml` (this) | `backlog.yaml` |
|---|---|---|
| **Holds** | Committed, sequenced waves with deps + LOC estimates + acceptance criteria | Unscheduled ideas, future projects, content missions, wishlist items |
| **Discipline** | Hard: dependencies enforced; status state machine; operator approval required to add | Loose: append-only inbox; no deps; any idea welcome |
| **Examples** | W7 Streaming drainer; W10 Cap tokens; Phase 16 Business-domain bootstrap | "Write LinkedIn series on multi-agent systems"; "GA4 deep-dive Q2"; "Try Sonnet 4.7 evals"; "Blog: how VNX caught 3 hallucinations in one day" |
| **Lifecycle** | Items move planned → in_progress → completed/deferred/cancelled | Items live until operator promotes them to roadmap (or marks `wontfix`) |

When in doubt: idea → backlog first. Promote to roadmap when operator commits and dependencies are clear.

---

## Reading this document

This is the **canonical roadmap** for VNX system development. All previously-scattered plans (PRD waves W6-W15, single-system migration P1-P6, F43 packaging W6A-E, design-doc strategy waves W-state-1..7, design-doc memory waves W-mem-1..4, the new operator-UX quick wins, plus business-domain bootstrap Phase 16) are sequenced here with explicit dependencies.

**If you are T0 after `/clear`:** read this file first, then `roadmap.yaml`. They tell you what's done, what's next, and what's blocked on what.

**If you are the operator:** the next-action recommendation is at the end (§ "Recommended next move"). Each phase has a "why now" + "LOC" + "weeks" so you can sequence by capacity.

---

## Visualisation — phases at a glance

```
Phase 0 [UX]      ─→  current_state.md + vnx status CLI + GC + vnx init bootstrap
                       ↓ (unblocks: easy /clear recovery + new-project setup)
Phase 1 [Cleanup] ─→  Merge PR #395, #396 + replay codex re-audits
                       ↓ (unblocks: state hygiene clean)
Phase 2 [State]   ─→  strategy/ foundation (roadmap.yaml + decisions.ndjson + projector)
                       ↓ (unblocks: machine-readable plan, survives /clear)
   ┌───────────────┴───────────────┐
   │                                │
Phase 3 [W7 Streaming]    Phase 4 [W6A F43 Revival]   ◄── parallel, no OD blockers
   │                                │
Phase 5 [W7.5 Failover]   Phase 6 [W6 Single-System P1-P6]
   │                                │
   └───────────────┬───────────────┘
                   ↓
Phase 7 [W8 Folder agents] ◄── needs W7 (canonical event schema)
   ↓
Phase 8 [W9 Provider IF]
   ↓
Phase 9 [W10 Cap tokens] ◄── needs OD-2 answered
   ↓
Phase 10 [W11 Workers=N]
   ↓
Phase 11 [W12 Sub-orchs]
   ↓
   ├── Phase 12 [W-mem Memory layer]
   └── Phase 13 [W13 LiteLLM + community PyPI live]
   ↓
Phase 14 [W14 Folder cutover]
   ↓
Phase 15 [W15 Roadmap autopilot] ◄── existing ROADMAP.yaml feature
   ↓
Phase 16 [Business-domain bootstrap] ◄── needs Phase 7+9+12; the USP
                                            (marketing-lead + blog/linkedin/seo/ga4 workers)
```

---

## Phase 0 — Operator UX quick wins (~340 LOC, 2 days, NO BLOCKERS)

**Goal:** make T0 know what's going on after `/clear` without operator intervention. Hide VNX's internal complexity behind one file + one CLI. Plus: every NEW project from `vnx init` gets the same setup.

| Wave | Scope | LOC | PR strategy |
|------|-------|-----|-------------|
| **W-UX-1** | Bootstrap `.vnx-data/strategy/` folder + `roadmap.yaml` machine-readable + this document committed | ~50 | Single PR (`docs/strategy-bootstrap`). Adds `.gitignore` exception so `strategy/` is tracked while rest of `.vnx-data/` stays gitignored. |
| **W-UX-2** | `current_state.md` auto-projector. Reads roadmap.yaml + open PRs + receipts + open items + recent decisions → renders one-pager. Runs at SessionEnd hook + post-PR-merge. Retires vestigial `STATE.md` / `PROJECT_STATUS.md` / `HANDOVER_*.md` (move to `state/_archive/`). | ~150 | Single PR (`feat/strategy-current-state-projector`). Includes the projector script + hook wiring + 3 archive moves. |
| **W-UX-3** | `vnx status` CLI dashboard. Combines `current_state.md` (strategic) + live runtime data (open PRs, idle terminals, blocking OIs) into a terminal-friendly summary. Prints in <2 seconds. | ~80 | Single PR (`feat/vnx-status-cli`). Wraps existing `t0_state.json` reader + adds strategic projection. |
| **W-UX-4** | GC retention policy in `build_t0_state.py`. Prune `feature_state.dispatches` older than 14 days unless still in any open PR or open mission. Same for `pr_status` entries with no recent activity. | ~30 | Single PR (`fix/t0-state-gc-retention`). Default cutoff configurable; preserves anything operator-pinned. |
| **W-UX-5** | `vnx init` extended: bootstrap `strategy/` folder + agent folder templates + initial roadmap stub. Prompt: "What kind of project? [code / content / sales / mixed / custom]" → seeds appropriate agent folders + governance variant. | ~80 | Single PR (`feat/vnx-init-strategy-bootstrap`). New project from any operator workspace gets full strategic-state stack. |

**Phase 0 acceptance:**
- After `/clear` of T0, the operator can run `vnx status` and immediately see: current phase, in-flight waves, open PRs, blocking decisions.
- No ad-hoc state markdown files in `.vnx-data/state/` root anymore — all archived or replaced.
- `t0_state.json` is <500 KB (was bloating with history).

---

## Phase 1 — Open work cleanup (~0 new LOC, dependency on quotas/services)

**Goal:** clear the in-flight backlog so we start the next phase with empty active queue.

| Item | Status | Action |
|------|--------|--------|
| **PR #395** ADRs (No-Redis + F43 packaging) + threshold-OI cleanup script | Open | Retry gemini gate when quota recovers (today/tomorrow). Merge. |
| **PR #396** UR-001 dead duplicate `_maybe_reroute_ghost_receipt` removal | Open | Same — gemini gate retry + merge. |
| **PR #232** Pane-manager cross-project leak | Open since 2026-04-20 | Investigate: still relevant after W4G? Close if superseded. |
| **~75 codex re-audit OIs** | Pending May 5 quota reset | Batch-replay via `vnx codex replay-audits`. Will reduce open-OI count significantly. |

**Phase 1 acceptance:**
- 0 open PRs from prior sprints.
- Codex re-audit OIs either closed (no findings) or re-classified as legitimate work items.

---

## Phase 2 — Strategic state foundation (~740 LOC, 1 week, NO BLOCKERS)

**Goal:** implement Layer 1 of `PROJECT_STATE_DESIGN.md` — the file-based strategic state surface. Retires ad-hoc files. Makes the chained-feature execution context-clear-survivable.

| Wave | Scope | LOC | Notes |
|------|-------|-----|-------|
| **W-state-1** | `roadmap.yaml` schema + reader/writer Python module (`scripts/lib/strategy_roadmap.py`). Pydantic-style validation. Status state machine (planned → in_progress → completed → deferred → cancelled). | ~180 | Foundation for everything below. |
| **W-state-2** | `decisions.ndjson` append-only log + writer (`scripts/lib/strategy_decisions.py`). One entry per operator OD answer, T0 decision, ADR finalisation. Schema: `{id, scope, ts, rationale, supersedes, evidence_path}`. | ~140 | Replaces the in-conversation tracking that evaporates on /clear. |
| **W-state-3** | `current_state.md` auto-projector (`scripts/build_current_state.py`). Renders from roadmap.yaml + decisions.ndjson + open PRs + open items + recent receipts. Hook: SessionEnd + post-PR-merge. | ~210 | The user-facing one-pager. Already started in Phase 0; this is the formal version. |
| **W-state-4** | `prd_index.json` + `adr_index.json`. Lightweight pointers to documents in `claudedocs/` (PRDs, internal) and `docs/governance/decisions/` (ADRs, public). Frontmatter parsed for metadata. | ~110 | Lets queries answer "where is the active PRD for X?". |
| **W-state-5** | Extend `build_t0_state.py` to load `strategy/` files into `t0_state.json` under a `strategy` key. SessionStart hook keeps working unchanged. | ~100 | The bridge between strategic-state files and runtime-state JSON. |

**Phase 2 acceptance:**
- `vnx status` shows current wave + next planned wave + recent decisions + open OD count.
- T0 after `/clear` reads `t0_state.json.strategy` and knows the full plan.
- Old `STATE.md` / `PROJECT_STATUS.md` / `HANDOVER_*.md` files moved to `state/_archive/` with deprecation notice.

---

## Phase 3 — Universal harness W7 streaming drainer (~640 LOC, 1 week, NO OD BLOCKERS)

**Goal:** close the codex+gemini observability gap. Closes governance hole. Reused by every later wave.

(See PRD §5 FR-2 + FR-3 + §8.2 for full sub-PR breakdown.)

| Sub-wave | Scope | LOC |
|----------|-------|-----|
| W7-A | `CanonicalEvent` schema + EventStore API + `observability_tier` field | ~80 |
| W7-B | `_streaming_drainer.py` mixin (shared by all adapters) | ~120 |
| W7-C | Codex adapter migration to streaming (closes the buffered-stdout gap) | ~115 |
| W7-D | Gemini-CLI adapter migration (gated behind `VNX_GEMINI_STREAM=1` until v0.11+ proven) | ~55 |
| W7-E | `LiteLLMAdapter` proof-of-concept (Bedrock/Mistral reachable) | ~150 |
| W7-F | `OllamaAdapter` audit + streaming refactor | ~70 |
| W7-G | Tier-1/2/3 labeling in receipt + governance-variant gating | ~50 |

**Phase 3 acceptance:** every provider's per-event stream lands in `events/T{n}.ndjson` and `events/archive/{worker_id}/{dispatch_id}.ndjson`. No silent buffering. Receipts carry `observability_tier`.

---

## Phase 4 — F43 context rotation revival + community giveaway prep (~750 LOC, 2 weeks, PARALLEL with Phase 3)

**Goal:** revive F43 in main, then carve out as standalone PyPI package per ADR-002.

| Wave | Scope | LOC | Notes |
|------|-------|-----|-------|
| **W6A** | Revive `feat/f43-context-rotation-headless` into main. Rebase onto post-W3J state. Re-apply F43 edits over W1A subprocess_dispatch facade. Run all tests + 501-LOC F43 test suite. | ~750 | Conflicts likely; budget extra time for rebase against post-refactor structure. |
| **W6B** | Carve out `scripts/lib/context_rotation/` package within VNX. stdlib-only. Public API frozen: `Tracker.update(event)`, `should_rotate(tracker)`, `build_handover(tracker, last_user_message)`. | ~400 | Dependency-free. |
| **W6C** | Create separate GitHub repo (`Vinix24/headless-context-rotation`) + sync script (`scripts/maintenance/sync_context_rotation_module.py`). MIT license. | ~200 | Operator owns the GitHub repo creation. |
| **W6D** | PyPI publish. `pyproject.toml` + GitHub Actions release workflow + first version tag. | ~100 | Operator owns PyPI account. |
| **W6E** | Reddit r/LocalLLaMA + r/ClaudeAI post + Hacker News "Show HN" + LinkedIn announcement. | content only | Operator-driven; T0 can draft. |

**Phase 4 acceptance:**
- F43 active in main; headless workers handle context rotation.
- Standalone PyPI package live, downloadable.
- First community feedback issue opened on the standalone repo.

---

## Phase 5 — Provider failover at orchestrator level W7.5 (~480 LOC, 1-2 weeks, AFTER W7)

**Goal:** when primary provider (Opus) is down, orchestrator restarts on next available fallback (Codex → Gemini). Trust chain survives via persistent ed25519 keys.

(See PRD §5 FR-11 for full design.)

| Component | Scope | LOC |
|-----------|-------|-----|
| `scripts/lib/provider_health.py` | Health-check probe + ring buffer | ~120 |
| `scripts/lib/provider_chain.py` | Chain resolver + failover decision | ~100 |
| `scripts/lib/checkpoint_writer.py` | Summary checkpoint writer for orchestrators | ~80 |
| Receipt schema | `provider_chain_at_dispatch`, `failover_events` fields | ~30 |
| Tests + docs | | ~150 |

**Phase 5 acceptance:** kill Opus mid-mission → main orchestrator restarts on Codex with summary checkpoint, mission state preserved, cap-token chain unbroken.

---

## Phase 6 — Single-system migration (~1500 LOC, 2-3 weeks, PARALLEL with Phase 3-5)

**Goal:** consolidate 4 VNX deployments into one central `~/.vnx-data/`. Phase 0 already done (PRs #334 + #358).

(See `claudedocs/2026-04-30-single-vnx-migration-plan.md` for full design.)

| Sub-phase | Scope | LOC | Status |
|-----------|-------|-----|--------|
| P0 | `project_id` column on 7 hottest tables | ~120 | ✅ done (#334 + #358) |
| P1 | Read-only federation aggregator (~300 LOC) | ~300 | Pending |
| P2 | Identity layer (per-project worker registry, project_id in all writers) | ~250 | Pending |
| P3 | Receipt + register envelope + per-project paths (event envelopes carry project_id) | ~280 | Pending |
| P4 | One-shot data import script (`migrate_to_central.py`) | ~280 | Pending — high-risk; operator review before run |
| P5 | Reader cutover, central install symlinks | ~190 | Pending |
| P6 | Per-project DB retirement after verification | ~80 | Pending |

**Phase 6 acceptance:** all 4 projects (`vnx-roadmap-autopilot`, `mission-control`, `sales-copilot`, `SEOcrawler_v2`) read/write from `~/.vnx-data/` with their own `project_id`. Zero per-project DB after P6.

---

## Phase 7 — Folder-based agents W8 (~1470 LOC, 3 weeks, AFTER W7 + recommended after W-state-2)

**Goal:** replace prompt-injected skills with folder-loaded agents. Each agent has its own `BEHAVIOR.md` + provider symlinks + `permissions.yaml` + optional `skills/<task>.md` + `runtime.yaml` (provider chain).

(See PRD §5 FR-4 + FR-12 for full design.)

| Sub-wave | Scope | LOC |
|----------|-------|-----|
| W8 Phase A | `.claude/agents/` skeleton + migrate 8 existing `.claude/skills/<role>/` into `.claude/agents/workers/<role>/` with BEHAVIOR.md + symlinks | ~400 |
| W8 Phase B | `governance.yaml`, `guardrails.yaml`, `permissions.yaml` per agent + dispatcher reads these instead of `worker_permissions.yaml` + per-agent hooks fire from `hooks/` | ~500 |
| W8 Phase C (= W14) | Flip default to folder-based; injection path becomes legacy fallback. Remove `.claude/skills/` symlink. | ~200 |
| **W8 + FR-12** | Agent registry + dispatch validator + library renderer (orchestrators see worker library summary, not full BEHAVIOR.md) | ~370 |

**Phase 7 acceptance:** orchestrator dispatches a worker by name; VNX assembles `BEHAVIOR.md + (optional skill) + dispatch.instruction`. Validation rejects unknown_agent / missing_input / provider_unsupported_by_worker.

---

## Phase 8 — Universal `WorkerProvider` interface W9 (~400 LOC, 2 weeks, AFTER W8)

**Goal:** refactor `ProviderAdapter` → `WorkerProvider` Protocol with `models()`, `spawn()`, `stop()`, lifecycle methods. All providers (Claude, Codex, Gemini, Ollama, future Kimi/LiteLLM bridge) implement same interface.

**Phase 8 acceptance:** adding a new provider is ≤2 days for one developer (~80-150 LOC adapter).

---

## Phase 9 — Capability tokens + governance variants W10 (~600 LOC, 2-3 weeks, AFTER W9, NEEDS OD-2)

**Goal:** macaroon-style ed25519-signed capability tokens. Trust chain `operator → main orch → sub-orch → workers`. Per-orchestrator `governance.yaml` declares variant (coding-strict vs business-light).

(See PRD §5 FR-5 + FR-6 for full design.)

**BLOCKED ON:** OD-2 (signing-key location: laptop file / Keychain / YubiKey). Operator decision needed.

**Phase 9 acceptance:** worker forges dispatch → verifier rejects with structured error in receipt. Sub-orch attenuates token (narrows scope) → verifier accepts attenuated, rejects expansion.

---

## Phase 10 — Workers=N rename W11 (~600 LOC, 2 weeks, AFTER W10)

**Goal:** drop hardcoded `T0..T3` everywhere. Worker identity becomes `(orchestrator_id, worker_id, role)` triple. `worker_registry` SQLite sidecar table.

(See PRD §5 FR-9 for full design.)

**Phase 10 acceptance:** spawn 8th worker via `vnx pool add tech-lead/be-dev-5` — works without source-code change. T0..T3 still aliases for ≥6 months.

---

## Phase 11 — Sub-orchestrator pools + missions + Assistant orchestrator W12 (~800 LOC, 3 weeks, AFTER W11)

**Goal:** Tier-2 orchestrators with own worker pools. `.vnx-data/missions/<id>.json` describes top-level mission. Main "Assistant" orchestrator dispatches sub-orchs.

(See PRD §5 FR-7 + FR-8 + companion research for full design.)

**Phase 11 acceptance:** operator dispatches "ship F50 + announcement" → main → Tech Lead (4 workers) + Marketing Manager (2 workers). All 6 workers complete with archived events. Mission file shows state transitions.

---

## Phase 12 — Memory layer + cross-domain learning (~1340 LOC, 2-3 weeks, AFTER W11)

**Goal:** Layer 2 from `PROJECT_STATE_DESIGN.md`. Cross-domain artifact memory (blogs, marketing copy, sales emails, mission summaries) via `sqlite-vec` + `nomic-embed-text` (local Ollama, ADR-001 compliant).

| Wave | Scope | LOC |
|------|-------|-----|
| W-mem-1 | sqlite-vec + nomic-embed setup. Ollama model pull. Schema migration in `quality_intelligence.db`. | ~200 |
| W-mem-2 | `artifact_index` table + embedding pipeline. Boot-time + on-write indexer. | ~350 |
| W-mem-3 | Domain partitioning (hard, per-domain virtual tables). Retrieval API: CLI + programmatic. | ~400 |
| W-mem-4 | Auto-injection at dispatch (opt-in 2 weeks then default-on). One-way sync of `feedback_*.md` → `vec_operator_prefs`. | ~390 |

**Phase 12 acceptance:**
- `vnx memory search "blog langGraph orchestration" --domain marketing` returns top-k similar past artifacts.
- Tech Lead orchestrator at planning time gets injected: "Related past missions: ... Past decisions on similar topics: ... Relevant antipatterns: ...".

---

## Phase 13 — Provider expansion W13 (~400 LOC, 1 week, AFTER W9)

**Goal:** LiteLLM bridge + Ollama parity + first community module live (PyPI download stats).

**Phase 13 acceptance:** dispatch via `provider: litellm/anthropic/claude-sonnet-4-6` works. `headless-context-rotation` PyPI module has ≥100 downloads in first month.

---

## Phase 14 — Folder-based agents Phase C cutover W14 (~200 LOC, 1 week, AFTER W13)

**Goal:** remove legacy `_inject_skill_context()` path. Folder agents are the only path.

**Phase 14 acceptance:** `VNX_FOLDER_AGENTS=0` does nothing (legacy path removed). All dispatches use folder-loaded agents.

---

## Phase 15 — Roadmap autopilot W15 (~existing scope, 2 weeks, AFTER W14)

**Goal:** the existing `roadmap-autopilot` feature in `ROADMAP.yaml` (PR-0..PR-3). Now operates over the universal harness.

**Phase 15 acceptance:** new feature added to `roadmap.yaml` → main orchestrator auto-loads it after current feature merges.

---

## Phase 16 — Business-domain bootstrap (~1150 LOC, 2 weeks, AFTER Phase 7+9+12)

**Goal:** prove VNX's universal-harness vision by extending it from code-domain to content/marketing/sales. Same machinery, different agent folders + governance variant. Demonstrates the unique selling point: one operator running engineering AND content AND sales via one orchestration system with audit-grade governance per domain.

| Wave | Scope | LOC |
|------|-------|-----|
| **W16-1** | `business-light` governance variant + permissions templates (allowed: Read, Write, WebFetch; denied: Bash, code execution) | ~100 |
| **W16-2** | `marketing-lead` orchestrator agent folder (BEHAVIOR.md + governance.yaml + runtime.yaml claude→gemini chain + workers.yaml + skills/plan-content-calendar, kies-onderwerp, analyseer-performance) | ~200 |
| **W16-3** | `blog-writer` worker agent folder (BEHAVIOR.md operator-tone + skills/draft-post, edit-for-tone, add-seo-meta) | ~150 |
| **W16-4** | `linkedin-writer` worker agent folder (LinkedIn-specific tone + skills/draft-post, draft-carousel, respond-to-comment) | ~120 |
| **W16-5** | `seo-analyst` worker agent folder + brave-search MCP grant (skills/keyword-research, competitor-content-audit, on-page-audit) | ~130 |
| **W16-6** | `ga4-analyst` worker + custom GA4 MCP server wrapping GA4 Data API (~300 LOC server, separate concern, possibly own giveaway repo) | ~450 |
| **W16-7** | Per-domain memory partition: bootstrap `vec_artifacts_marketing` + `vec_artifacts_sales` tables. Hard partition; cross-domain only via `vec_operator_prefs`. | ~100 |
| **W16-8** | End-to-end smoke test mission "schrijf 1 blog van begin tot eind" — operator → main → marketing-lead → research (seo-analyst) → draft (blog-writer) → review → commit as markdown | ~50 |

**Phase 16 acceptance:**
- One full blog post drafted end-to-end via the dispatch flow with full audit trail.
- `vnx memory search "blog langGraph orchestration" --domain marketing` returns past content artifacts.
- Marketing-lead dispatches cannot reach code-domain workers (cap-token scope enforced).

**Phase 16 USP:**
This is what differentiates VNX from CrewAI / AutoGen / Aider / Cline:
- CrewAI does only agents-as-tools within one crew
- AutoGen does teams without governance variants
- Aider/Cline are code-only
- Mem0 is only memory

VNX becomes the only single-operator multi-domain orchestration framework with audit-grade governance per domain. That's worth a Show-HN.

---

## Recommended next move (if you have to pick ONE thing today)

**Phase 0** — operator-UX quick wins. Reasons:

1. **No OD blockers.** All 4 sub-PRs can land without operator decisions.
2. **Solves the immediate pain** ("T0 doesn't know what's going on after /clear") with ~260 LOC.
3. **Bootstraps the strategy/ folder pattern.** Every later phase benefits because the canonical roadmap location now exists.
4. **Validates the design** before committing 4000+ LOC to the universal harness.

If Phase 0 lands cleanly within 1-2 days, immediately start **Phase 3 (W7) and Phase 4 (W6A) in parallel** — both are no-OD-blocker pure implementation work that closes governance gaps and ships the F43 community module.

---

## Cross-cutting notes

- **Operator decision blockers:** Only OD-2 (signing-key location) blocks Phase 9. OD-1, OD-4, OD-5, OD-6 are recommended-defaults that can stand unless operator overrides.
- **Cost:** Total new code is ~13,320 LOC over ~27 weeks if sequential; ~15-17 weeks with maximum parallelism. (Phase 16 +1150 LOC, W-UX-5 +80 LOC vs v1.0.)
- **Public-vs-internal:** Strategy state (`.vnx-data/strategy/`) is git-tracked via `.gitignore` exception (recommended — audit trail). Memory artifacts (`.vnx-data/memory/`) stay gitignored (private operator content).
- **Backward compatibility:** Every wave is additive or has a flag-controlled cutover. T0..T3 aliases ≥6 months. `_inject_skill_context()` legacy path until W14.
- **Risk waves:** Phase 6 P4 (one-shot data import) is the highest-risk single PR. Operator MUST review the migration script + dry-run output before P4 executes.

---

## Update protocol

This file is **the master**. Update it when:
- A wave completes (set status, link merged PRs)
- A wave is deferred or cancelled (note reason)
- An operator decision (OD-N) is answered (link to `decisions.ndjson` entry)
- A new wave is added (rare; should be discussed first)

The companion `roadmap.yaml` should be updated **automatically** by `current_state.md` projector once Phase 2 lands. Until then, update both files in lockstep.

---

*End of Master Roadmap. ~12,000 LOC, ~14-25 weeks, 1 operator + 4 LLM workers + community giveaway as the deliverable shape.*
