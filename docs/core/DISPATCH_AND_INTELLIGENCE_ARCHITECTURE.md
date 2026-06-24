# Dispatch & Intelligence Architecture (current)

**Status:** current as of 2026-06-23 (post single-entry-door flip, PR #896).
**Scope:** how a dispatch flows from T0's intent to a governed receipt, and how intelligence is injected and learned. This is the end-to-end picture; the enforced ruleset T0 follows is `DISPATCH_RULES.md`, the lane detail is `PROVIDER_LANES.md`, the receipt shape is `11_RECEIPT_FORMAT.md`.

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
   │     provider=claude              → claude tmux-spawn lane         │
   │     provider=kimi|glm|deepseek   → provider envelope lane         │
   │  Flag: VNX_SINGLE_ENTRY_DISPATCH (default OFF = byte-identical    │
   │  legacy routing); VNX_DISPATCH_LEGACY=1 = hard rollback.          │
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
   │  claude lane (tmux-spawn): fresh isolated worktree + tmux pane,  │
   │    launch interactive `claude` (NEVER `claude -p`), bracketed-   │
   │    paste(body), settle, Enter as a SEPARATE keystroke, staged-   │
   │    paste retry. Readiness + submit detection ride on hook        │
   │    sentinels (§5).                                               │
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
| `claude` (Opus/Sonnet) | **claude tmux-spawn** (`tmux_interactive_dispatch.py`) | interactive `claude` in an ephemeral isolated worktree; leaseless | subscription (June-15 escape) |
| `claude` (terminal-pinned, opt-in) | **subprocess** (`subprocess_dispatch.py`) | `claude -p` headless, lease + Wave-5 smart-context + triple-gate | subscription |
| `kimi` / `glm`(harness) / `deepseek`(harness) | **provider envelope** (`dispatch_envelope.py`) | provider CLI / harness spawn | provider-metered |

Hard rules: **claude workers route via tmux-spawn, never `provider_dispatch`, never headless `claude -p` (post-cutover = API credits).** `glm` always via the claude-CLI harness (`:4141` litellm→OpenRouter proxy), never plain `litellm:zai`. Everything dispatches through the single door (`vnx dispatch`); calling a lane script directly is a side door (the `dispatch_sidedoor_audit.py` gate enforces this).

The tmux-spawn lane is the default for parallel/independent feature work. The subprocess lane is opt-in (`VNX_ADAPTER_T{n}=subprocess`) for terminal-pinned PRs, >30-min workers, and burn-in measurement — it uniquely carries Wave-5 smart-context, lease management, triple-gate `contract_hash` binding, and prior-round findings.

## 4. Assembly — where governance value is added

Assembly runs in the lane **before** delivery (`_assemble_context` → `dispatch_prepare.prepare`, or the `_inject_skill_context` fallback). It wraps the raw instruction with three things the worker cannot produce on its own:

- **Skill body** — the role's `SKILL.md` (the specialist's operating instructions).
- **Intelligence injection** — `## Relevant Intelligence (from past dispatches)` + `### Antipatterns to avoid`, selected per-dispatch from the quality-intelligence store (§7).
- **Report-contract footer** — the mandatory `## Summary / ## Changes / ## Verification / ## Open Items` headings + the `<!-- VNX-END-OF-INSTRUCTION -->` sentinel. This is *why* the worker writes a report — and the report is what becomes a receipt.

**Consequence:** a hand-delivered raw instruction (bypassing the lane) skips assembly entirely → no intelligence, no report-contract → the worker may write no report → no receipt → the work is invisible to governance, and the phantom-guard has nothing to check. Delivery problems must be fixed in the lane, never worked around by raw delivery.

## 5. Delivery reliability — the tmux-signal hook contract

The claude tmux-spawn lane's readiness and submit detection ride on two hook sentinels, dropped by hooks the worker's project must wire and guarded by `VNX_TMUX_SIGNAL_DIR` + `VNX_DISPATCH_ID` (which the lane exports into the worker env):

- **SessionStart** → `scripts/hooks/tmux_signal_session_ready.sh` writes `<signal_dir>/session_ready` → the lane knows the worker's input box is ready before it pastes.
- **UserPromptSubmit** → `scripts/hooks/tmux_signal_prompt_received.sh` writes `<signal_dir>/prompt_received` → the lane knows the paste was actually submitted.

When these hooks are wired, the lane uses the sentinels (reliable). When they are missing, it falls back to TUI-marker heuristics — which mis-fire across Claude Code TUI revisions (e.g. under 2.1.186 the lane can paste before the input is ready → the body never stages → the worker idles → reaped on `interactive_no_progress`). **Wiring these two hooks is a hard prerequisite for the claude tmux-spawn lane in any project.** The hook scripts also exist under `.vnx/scripts/hooks/` for vendored installs; they are no-ops without `VNX_TMUX_SIGNAL_DIR`, so they are safe to register globally.

Other invariants: **Enter is ALWAYS a separate tmux keystroke** (a combined send-keys misses delivery); the worker launches scoped (`--permission-mode acceptEdits` + role allow-list) in the detached path, not blanket `--dangerously-skip-permissions`.

## 6. Govern + the phantom-guard

After a worker emits its completion receipt, `dispatch_govern.govern()` runs an inline **phantom-guard** (`phantom_guard.record_phantom_if_any`) for every dispatch — independent of the door flag. The rule: a *delivery-role* worker that claims completion (a GATE-GREEN status) but produced **no worktree/branch diff** is a phantom — a receipt not backed by a deliverable. Tokens spent do not exempt it (an LLM thinking is not a deliverable). Review roles are exempt (a verdict, not a diff, is expected); an unresolvable/torn-down ref ABSTAINS (never a false reject).

On a phantom verdict the guard appends a corrective `failed` receipt carrying `phantom_rejected=True` (the worker's original `done` is preserved — the contradiction is recorded, not overwritten). `dedup_completion_receipts` honours that as a **Tier-0 override**: the dispatch resolves FAILED even on a same-second timestamp tie. Operator escape: `VNX_OVERRIDE_PHANTOM_GUARD=1` for a legitimate no-op delivery.

This is the anti-fabrication backstop: it makes "claimed done with nothing to show" a first-class governance failure rather than a silent green.

## 7. Intelligence — injection and the self-learning loop

The quality-intelligence store (`quality_intelligence.db`, per-project under `~/.vnx-data/<project>/`) holds success patterns, antipatterns, prevention rules, and prior-round findings. Two flows:

- **Injection (read path, per dispatch):** `IntelligenceSelector.select()` chooses the most relevant patterns/antipatterns for the dispatch's role + paths + pr_id and `_build_intelligence_section` renders them into the assembled body (§4). Prior-round findings (`prior_round_injector.py`, Wave-5) carry a `contract_hash` so a re-dispatch sees what the last round found.
- **Learning (write path):** the intelligence daemon (`intelligence_daemon.py` + `learning_loop.py`) runs two tiers. Tier-1 auto-tunes pattern *confidence* from receipt outcomes. Tier-2 proposes new rules into an operator-gated `pending_rules.json` — the system proposes, the human approves. Nothing self-promotes a rule without a gate.

Tenant isolation (ADR-007): every intelligence table is keyed by a composite over `project_id`; the store resolves its owning project fail-closed (`.vnx-project-id` marker → `VNX_PROJECT_ID` → the `~/.vnx-data/<pid>/` path), so a fresh store never silently inherits another tenant's rows.

> **Deep reference:** the full engine — `IntelligenceSelector.select()` internals, confidence/evidence gates, recency suppression, payload cap, the exact rendered markdown, the `intelligence_injections` audit, and the learning-loop write path — is documented in `docs/core/technical/INTELLIGENCE_SYSTEM.md` (this section is the overview it links back to).

## 8. Where the evidence lives

- Staged intents: `.vnx-data/dispatches/pending/<id>/`
- Reports: `$VNX_DATA_DIR/unified_reports/<dispatch-id>.md` (interactive), `…/headless/` (review gates)
- Receipts (the audit trail): `t0_receipts.ndjson`
- Review-gate results: `.vnx-data/state/review_gates/results/`
- Per-dispatch event ring buffer (subprocess lanes only): `.vnx-data/events/T{n}.ndjson` → archived per dispatch

Without a report there is no receipt, and without a receipt the work is invisible to governance. The report contract (`scripts/lib/report_body_contract.py`) is the bridge.

---

## Cross-references

- Enforced ruleset (T0 SSOT): `DISPATCH_RULES.md`
- Lane detail + billing: `PROVIDER_LANES.md`, `docs/operations/TMUX_SPAWN_LANE.md`
- Receipt shape: `11_RECEIPT_FORMAT.md`
- Intelligence internals: `docs/core/technical/INTELLIGENCE_SYSTEM.md`, `docs/internal/intelligence/INTELLIGENCE_INJECTION_V1.1.md`
- Tenant isolation: `docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md`
- System boundaries (what stays local): `VNX_SYSTEM_BOUNDARIES.md`
