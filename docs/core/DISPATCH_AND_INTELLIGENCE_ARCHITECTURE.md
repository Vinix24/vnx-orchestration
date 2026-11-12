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
   │  3. INTELLIGENCE INJECTION — arm first, then injection:              │
   │       arm = arm_for_dispatch(dispatch_id, project_id, window)        │
   │         control: placebo/empty, selector.select() never runs         │
   │       _build_intelligence_section:                                   │
   │       "## Relevant Intelligence (from past dispatches)"              │
   │       "### Antipatterns to avoid"   (patterns + antipatterns)        │
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
Claude Code TUI revisions) when a project hadn't wired them. Those hooks and
the Stop hook that wrote a receipt from the signal directory are gone
(golf 1, cluster C2); the full contract is preserved as history in
`docs/operations/TMUX_SPAWN_LANE.md`.

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

- **Injection (read path, per dispatch):** the A/B arm is decided **before** selection: `arm_for_dispatch(dispatch_id, project_id, window)` hashes the dispatch against the frozen window and returns `control`, `treatment`, or `(None, None)` when no window is open. A `control` dispatch gets no `selector.select()` call (a placebo or an empty section); a `treatment` dispatch and a dispatch with no open window select as before. Every injection row carries `ab_arm` and `ab_window` — `NULL` where no window is open means "not assigned", never a silent `treatment`. `IntelligenceSelector.select()` then chooses the most relevant patterns/antipatterns for the dispatch's role + paths + pr_id and `_build_intelligence_section` renders them into the assembled body (§4). Prior-round findings (`prior_round_injector.py`, Wave-5) carry a `contract_hash` so a re-dispatch sees what the last round found.
- **Measurement (harness, never the verdict):** `scripts/intel_injection_join.py cohort` reports the per-arm counts and the fail-closed controls and never a verdict; `effect` adds the stratified risk difference, its 95% interval and one of four verdict classes, and refuses below `MIN_PER_ARM = 200` resolved dispatches per arm (exit 3) or on a missing/`None`/malformed/unknown value. The measured arms are decided and frozen at window-open time; the full commands, exits and fail-closed table are `claudedocs/specs/intelligence-effect-measurable/protocol.md`, and the decisions are [ADR-069](../governance/decisions/ADR-069-intelligence-effectmeting.md). `secondary` adds the falsifications F1-F3 plus mean CQS and open items per arm, each `not_evaluable` with its reason when its evidence is missing. A `placebo` section for the control arm is off unless `VNX_INTEL_PLACEBO_ARM=1`. No window has produced a verdict yet: no effect of injection on outcomes has been measured. Opening and closing the window is an operator step, not a code change.
- **Learning (write path):** the intelligence daemon (`intelligence_daemon.py` + `learning_loop.py`) runs two tiers once a day. Tier-1 auto-tunes pattern *confidence* from receipt outcomes, and only with `VNX_LEARNING_LOOP_PERSIST=1`; without it the cycle is a shadow run that writes a report and changes no row. Tier-2 only proposes: with `VNX_LEARNING_LOOP_QUEUE=1` (off by default) or persist mode, proposals go to the operator queue `pending_rules.json` through `prevention_rule_store`, and the operator records a decision with `vnx learning decide-rule`. In queue and persist mode the loop first reads the ledger verdict (`ledger_verdict/1`, `learning_ledger_gate.py`) and stops with `ledger_source_check: <reason_code>` when it is `refused`. A loop rule is read by an injector only while it has an active row in `pattern_activation` and that verdict is fresh and `ok` or `pending` (`pattern_activation.injectable_clause`); `deactivate_pattern_activation.py` switches an activation off and it stays off. In persist mode `ingest_approved_rules` (`scripts/learning_loop.py`) activates only a rule whose operator decision is recorded for this project and whose proposal is unchanged since that decision, and only under a fresh `ok` or `pending` ledger verdict of this project. It writes an activation record (`pattern_activation`), reuses an existing rule row only when its content equals the approved proposal, and never switches a repeated activation back on. The D3 receipt filter skips a failure receipt with a sentinel `provider` only when its `model` is also a sentinel and it carries no `report_path`. One home per lesson: [ADR-067](../governance/decisions/ADR-067-learning-capture-routing.md) and [LEARNING_CAPTURE_ROUTING.md](LEARNING_CAPTURE_ROUTING.md). Pattern ownership across projects: [ADR-068](../governance/decisions/ADR-068-learning-signaal-attributie.md). Nothing self-promotes a rule without a gate.


**Dream (nightly consolidation).** `vnx dream install-scheduler` installs the nightly job (launchd label `com.vnx.auto-dream`) and a watch job (`com.vnx.dream-watch`, `scripts/dream_liveness_probe.py --append`). `vnx doctor` projects the same `dream_liveness/1` reader (`scripts/lib/dream_liveness.py`), so a missed or disabled night is a finding and not a pass ([ADR-066](../governance/decisions/ADR-066-dream-liveness-watch.md)). Auto-dream stays burning in until a run of measured nights.

**Corpus quarantine.** `scripts/vnx_intelligence_corpus_quarantine.py` takes `baseline`, `quarantine`, `rehearse`, `verify`, `restore` and `purge`. `quarantine` writes the corpus to a sealed artifact with a manifest and leaves the source untouched; `restore` puts it back.

The success-pattern and antipattern tables carry the natural key `(project_id, pattern_type, title)`. An import writes them through `pattern_upsert` (`scripts/intelligence_import.py`), so it merges into the local row and never deletes one on an id match.

**Intelligence follows the provider switch and the content boundary (on main, not yet released).** Every intelligence caller that sends text to a model provider asks `provider_switch` first and treats a refusal as `provider_switched_off`, not as an outage: the receipt classifier and its classifier providers, the tagger, the scout pre-pass, the conversation analyzer's deep pass and the nightly dream consolidation. The operator's content boundary (`scripts/lib/content_class.py`, the file `~/.vnx/content_boundary.json`, which lives outside the repository) is read only by the conversation analyzer (`scripts/conversation_analyzer/{runner,deep_analyzer,intelligence_bridge}.py`) and the dream consolidation (`scripts/dream/consolidator.py`). In those paths, restricted content (class `client` or `personal`) may go to Claude, codex and local models and never to GLM, DeepSeek, kimi or an openrouter/litellm lane, unless a project is named in `provider_exceptions` for content that is restricted through that project id alone. A missing or unreadable boundary file is an explicit `unconfigured` state, and the analyzer then treats every session as restricted. The receipt classifier, its classifier providers, the tagger and the scout pre-pass do not read the boundary file, so the restriction does not apply to them. The switch itself is described in `PROVIDER_SWITCH.md`, and ADR-044 records its design (one operator file, fail-closed, the two layers and the census guard). The content boundary and the `provider_switched_off` handling in each intelligence caller are code behaviour; no ADR records them.

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
