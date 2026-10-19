# Dispatch & Intelligence Architecture (current)

**Status:** describes `main` as of 2026-10-18 (single-entry door default, ADR-024; N-slot concurrency #1017; scoped worker permissions by default since 2026-08-14; tmux-spawn lane removed 2026-09-18 #1868; `sonnet` build-worker default reaffirmed #1890). What is on `main` and in no release yet is marked "on main, not yet released".
**Scope:** how a dispatch flows from T0's intent to a governed receipt, and how intelligence is injected and learned. This is the end-to-end picture; the enforced ruleset T0 follows is `DISPATCH_RULES.md`, the lane detail is `PROVIDER_LANES.md`, the receipt shape is `11_RECEIPT_FORMAT.md`. The review gates that read a finished PR (seats, takeover chain, merge door) are in `DISPATCH_RULES.md` section 2; the provider switch is in `PROVIDER_LANES.md`.

---

## 1. The model in one line

A dispatch is not "paste text into a worker." It is: **stage an intent → route it through the single door → assemble it (skill + intelligence + report-contract) → deliver it to an isolated worker → execute → emit a receipt → govern it (phantom-guard) → advance gates.** Every step is on disk and produces evidence. Bypassing any step (hand-delivering a raw instruction) produces *ungoverned* work — no intelligence, no report-contract, no receipt.

## 2. End-to-end flow

```
┌─ T0 (orchestrator) ────────────────────────────────────────────────┐
│  composes a spec: instruction + role + dispatch_id + paths          │
└───────────────────────────────┬────────────────────────────────────┘
                                 ▼
            .vnx-data/dispatches/pending/<id>/      ← staged bundle ("/pending")
                                 │   (human gate where required: promote)
                                 ▼
   ┌─ THE DOOR — dispatch_cli.run_dispatch (single entry) ────────────┐
   │  reads the bundle → compile_plan → picks the lane:               │
   │     provider=claude              → claude headless lane           │
   │     provider=kimi|glm|deepseek   → provider envelope lane         │
   │  Flag: VNX_SINGLE_ENTRY_DISPATCH (default ON, ADR-024 2026-06-24);│
   │  VNX_DISPATCH_LEGACY=1 = hard rollback to legacy routing.         │
   └───────────────────────────┬─────────────────────────────────────┘
                                ▼
   ┌─ ASSEMBLY — dispatch_prepare.prepare / _assemble_context ────────┐
   │  1. repo-map (prepended)                                         │
   │  2. skill body            (the role's SKILL.md)                  │
   │  3. INTELLIGENCE INJECTION — _build_intelligence_section:        │
   │       "## Relevant Intelligence (from past dispatches)"          │
   │       "### Antipatterns to avoid"   (patterns + antipatterns)    │
   │  4. the actual instruction                                       │
   │  5. FOOTER — report-contract directive (## Summary / ## Changes  │
   │       / ## Verification / ## Open Items) + END-OF-INSTRUCTION     │
   │       sentinel                                                   │
   │  (+ smart_context prepended)                                     │
   └───────────────────────────┬─────────────────────────────────────┘
                                ▼  = the assembled "body"
   ┌─ DELIVERY ──────────────────────────────────────────────────────┐
   │  claude lane (headless): fresh isolated worktree, `claude -p`,   │
   │    report gate and receipt bound before the dispatch counts      │
   │    as done (DISPATCH_RULES §8).                                  │
   │  provider lane (envelope): spawn kimi / glm-harness / deepseek-  │
   │    harness, instruction passed in-process.                       │
   └───────────────────────────┬─────────────────────────────────────┘
                                ▼
   worker executes → writes a unified report (because the FOOTER
                     required it) → report on disk
                                ▼
   receipt processor → NDJSON receipt → t0_receipts.ndjson
                                ▼
   GOVERN — dispatch_govern.govern(): phantom-guard checks the receipt
            vs the worker's real worktree/branch diff (§6)
                                ▼
   T0 reviews receipts + review-gate evidence + closure → advances gates
```

## 3. The two lanes (provider → lane is a hard rule)

| Provider | Lane | Mechanism | Billing |
|---|---|---|---|
| `claude` (Opus/Sonnet) | **claude headless** (`dispatch_envelope.run_envelope_headless_plan`) | `claude -p` in an ephemeral isolated worktree; leaseless | subscription |
| `claude` (terminal-pinned, opt-in) | **subprocess** (`subprocess_dispatch.py`) | `claude -p` headless, lease + Wave-5 smart-context + triple-gate | subscription |
| `kimi` / `glm`(harness) / `deepseek`(harness) | **provider dispatch** (`scripts/lib/provider_dispatch.py`) | provider CLI / harness spawn | provider-metered |

The review gates (`kimi_gate`, `deepseek_gate`, `glm_gate`) run the same harness lanes through `gate_runner`; `codex_gate` runs `codex exec`. Which gate fills which seat, and who takes over when a seat's reviewer is out, is not repeated here: `PROVIDER_LANES.md` ("Takeover chain") and `DISPATCH_RULES.md` section 2.

Hard rules: **claude workers route via the headless lane, never `provider_dispatch`.** `glm` always via the claude-CLI harness (`:4141` litellm→OpenRouter proxy), never plain `litellm:zai`. Everything dispatches through the single door (`vnx dispatch`); calling a lane script directly is a side door (the `dispatch_sidedoor_audit.py` gate enforces this).

The headless lane is the default for parallel/independent feature work (the tmux-spawn lane was removed on 2026-09-18). The subprocess lane is opt-in (`VNX_ADAPTER_T{n}=subprocess`) for terminal-pinned PRs, >30-min workers, and burn-in measurement — it uniquely carries Wave-5 smart-context, lease management, triple-gate `contract_hash` binding, and prior-round findings.

## 4. Assembly — where governance value is added

Assembly runs in the lane **before** delivery (`_assemble_context` → `dispatch_prepare.prepare`, or the `_inject_skill_context` fallback). It wraps the raw instruction with three things the worker cannot produce on its own:

- **Skill body** — the role's `SKILL.md` (the specialist's operating instructions).
- **Intelligence injection** — `## Relevant Intelligence (from past dispatches)` + `### Antipatterns to avoid`, selected per-dispatch from the quality-intelligence store (§7).
- **Report-contract footer** — the mandatory `## Summary / ## Changes / ## Verification / ## Open Items` headings + the `<!-- VNX-END-OF-INSTRUCTION -->` sentinel. This is *why* the worker writes a report — and the report is what becomes a receipt.

**Reviewers are not enriched.** The `review-gate` and `plan-reviewer` roles receive neither the repo-map, nor the skill or role `CLAUDE.md`, nor intelligence patterns. `reviewer_roles.enrichment_allowed(role)` is the one decision place, and every lane that enriches a prompt (`provider_dispatch._enrich_instruction`, `envelope_prepare._prepare`, `skill_context._inject_skill_context`, the direct-CLI path of `subprocess_dispatch.py`) asks it. A reviewer fed patterns from past dispatches is no longer independent, and any later measurement of what injection does to quality would be contaminated by an injected gate. The raw review instruction and the report-contract directive are not stripped. This is code behaviour; no ADR records it.

**Consequence:** a hand-delivered raw instruction (bypassing the lane) skips assembly entirely → no intelligence, no report-contract → the worker may write no report → no receipt → the work is invisible to governance, and the phantom-guard has nothing to check. Delivery problems must be fixed in the lane, never worked around by raw delivery.

## 5. Delivery reliability: history (removed lane)

The tmux-spawn lane (removed 2026-09-18, #1868) used to rely on two hook
sentinels (SessionStart and UserPromptSubmit), guarded by
`VNX_TMUX_SIGNAL_DIR` to detect that a paste actually landed in the worker's
input box, falling back to TUI-marker heuristics (which mis-fired across
Claude Code TUI revisions) when a project hadn't wired them. Nothing exports
`VNX_TMUX_SIGNAL_DIR` any more, so those hooks are inert; the full contract is
preserved as history in `docs/operations/TMUX_SPAWN_LANE.md`.

That lane also launched detached workers with blanket
`--dangerously-skip-permissions` by default. Worker permissions today are the
opposite: since 2026-08-14, headless workers spawn with a scoped, role-based
tool allow-list by default (worktree isolation bounds the filesystem, not the
network, and a blanket-skip worker still reaches ambient MCP servers). The
opt-out is `VNX_WORKER_BLANKET_SKIP=1` per dispatch. Detail:
`docs/operations/WORKER_PERMISSIONS.md`.

Invariant that outlived the lane: **Enter is ALWAYS a separate tmux
keystroke** (a combined send-keys misses delivery). This still applies to T0's
own tmux pane and any interactive non-claude CLI (codex, kimi, gemini)
terminal.

## 6. Govern + the phantom-guard

After a worker emits its completion receipt, `dispatch_govern.govern()` runs an inline **phantom-guard** (`phantom_guard.record_phantom_if_any`) for every dispatch — independent of the door flag. The rule: a *delivery-role* worker that claims completion (a GATE-GREEN status) but produced **no worktree/branch diff** is a phantom — a receipt not backed by a deliverable. Tokens spent do not exempt it (an LLM thinking is not a deliverable). Review roles are exempt (a verdict, not a diff, is expected); an unresolvable/torn-down ref ABSTAINS (never a false reject).

On a phantom verdict the guard appends a corrective `failed` receipt carrying `phantom_rejected=True` (the worker's original `done` is preserved — the contradiction is recorded, not overwritten). `dedup_completion_receipts` honours that as a **Tier-0 override**: the dispatch resolves FAILED even on a same-second timestamp tie. Operator escape: `VNX_OVERRIDE_PHANTOM_GUARD=1` for a legitimate no-op delivery.

This is the anti-fabrication backstop: it makes "claimed done with nothing to show" a first-class governance failure rather than a silent green.

## 7. Intelligence — injection and the self-learning loop

The quality-intelligence store (`quality_intelligence.db`, per-project under `~/.vnx-data/<project>/`) holds success patterns, antipatterns, prevention rules, and prior-round findings. Two flows:

- **Injection (read path, per dispatch):** `IntelligenceSelector.select()` chooses the most relevant patterns/antipatterns for the dispatch's role + paths + pr_id and `_build_intelligence_section` renders them into the assembled body (§4). Prior-round findings (`prior_round_injector.py`, Wave-5) carry a `contract_hash` so a re-dispatch sees what the last round found.
- **Learning (write path):** the intelligence daemon (`intelligence_daemon.py` + `learning_loop.py`) runs two tiers. Tier-1 auto-tunes pattern *confidence* from receipt outcomes. Tier-2 proposes new rules into an operator-gated `pending_rules.json` — the system proposes, the human approves. Nothing self-promotes a rule without a gate.

The success-pattern and antipattern tables carry the natural key `(project_id, pattern_type, title)`. An import writes them through `pattern_upsert` (`scripts/intelligence_import.py`), so it merges into the local row and never deletes one on an id match.

**Intelligence follows the provider switch and the content boundary (on main, not yet released).** Every intelligence caller that sends text to a model provider asks `provider_switch` first and treats a refusal as `provider_switched_off`, not as an outage: the receipt classifier and its classifier providers, the tagger, the scout pre-pass, the conversation analyzer's deep pass and the nightly dream consolidation. The operator's content boundary (`scripts/lib/content_class.py`, the file `~/.vnx/content_boundary.json`, which lives outside the repository) is read only by the conversation analyzer (`scripts/conversation_analyzer/{runner,deep_analyzer,intelligence_bridge}.py`) and the dream consolidation (`scripts/dream/consolidator.py`). In those paths, restricted content (class `client` or `personal`) may go to Claude, codex and local models and never to GLM, DeepSeek, kimi or an openrouter/litellm lane, unless a project is named in `provider_exceptions` for content that is restricted through that project id alone. A missing or unreadable boundary file is an explicit `unconfigured` state, and the analyzer then treats every session as restricted. The receipt classifier, its classifier providers, the tagger and the scout pre-pass do not read the boundary file, so the restriction does not apply to them. The switch itself is described in `PROVIDER_LANES.md`. This is code behaviour; no ADR records it.

Tenant isolation (ADR-007): every intelligence table is keyed by a composite over `project_id`; the store resolves its owning project fail-closed (`.vnx-project-id` marker → `VNX_PROJECT_ID` → the `~/.vnx-data/<pid>/` path), so a fresh store never silently inherits another tenant's rows.

## 8. Where the evidence lives

- Staged intents: `.vnx-data/dispatches/pending/<id>/`
- Reports: `$VNX_DATA_DIR/unified_reports/<dispatch-id>.md` (interactive), `…/headless/` (review gates)
- Receipts (the audit trail): `t0_receipts.ndjson`
- Review-gate results: `.vnx-data/state/review_gates/results/`
- Per-dispatch event ring buffer (subprocess lanes only): `.vnx-data/events/T{n}.ndjson` → archived per dispatch. The harness review seats and the headless lane are per-run terminals (`event_store.PER_RUN_TERMINALS`): each run writes its own stream, so parallel seats keep their final answer (`docs/operations/EVENT_STREAMS.md`).

Without a report there is no receipt, and without a receipt the work is invisible to governance. The report contract (`scripts/lib/report_body_contract.py`) is the bridge.

---

## Cross-references

- Enforced ruleset (T0 SSOT): `DISPATCH_RULES.md`
- Lane detail + billing: `PROVIDER_LANES.md`, `docs/operations/TMUX_SPAWN_LANE.md`
- Receipt shape: `11_RECEIPT_FORMAT.md`
- Intelligence internals: `docs/core/technical/INTELLIGENCE_SYSTEM.md`
- Tenant isolation: `docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md`
- System boundaries (what stays local, archived): `docs/_archive/core/VNX_SYSTEM_BOUNDARIES.md`. Superseded by ADR-026 (per-project store) and ADR-032 (fabric artifacts in consumers).
