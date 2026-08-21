<!-- VNX:BEGIN BOOTSTRAP -->
## VNX Governance

This repository is governed by **VNX Glass Box Governance**: multi-agent orchestration with a human gate at every step and an append-only NDJSON receipt per dispatch.

The mechanism is not duplicated here. How the fabric works — the single-entry dispatch door and its lanes, review gates, the horizon planning layer, state resolution, and the report contract — lives in one canonical place so it can never drift out of a project file:

- **How the fabric works:** the canonical orchestrator role, `.claude/terminals/T0/role-orchestrator.md`, kept in sync fleet-wide by `vnx role sync`.
- **Runbooks + gotchas:** the `fabric-reference` skill.
- **Dispatch mechanics (lanes, provider routing, failure modes):** `docs/core/DISPATCH_RULES.md`.

Everything above this block describes *this project*. Everything the fabric does lives in the canonical role — never copy fabric mechanism back into this file, or the copy drifts the moment the fabric changes.
<!-- VNX:END BOOTSTRAP -->

<!-- The sections below live here because this repo IS the fabric. In a consumer
     project they do NOT belong in CLAUDE.md: the report contract reaches workers
     operationally via build_directive() at dispatch time, and lane detail lives in
     the canonical role + docs/core/DISPATCH_RULES.md. They stay outside the bootstrap
     block so `vnx role sync` / re-init never propagates them into consumer files. -->

## Mandatory Report Contract

**Every agent and worker MUST write a unified report on completing any task.**

This is how work enters the governed audit trail:
```
report on disk → receipt processor → t0_receipts.ndjson
```

Without a report, your work has no receipt and is invisible to governance.

Write to: `$VNX_DATA_DIR/unified_reports/<dispatch-id>.md`

Your report MUST contain these exact headings (aliases accepted):

| Required | Accepted aliases |
|---|---|
| `## Summary` | — |
| `## Changes` | `## Files Modified`, `## Work Completed` |
| `## Verification` | `## Test Results`, `## Evidence`, `## Tests` |
| `## Open Items` | — |

`## Summary` must be at least 50 non-whitespace characters. `## Open Items` may contain "None" explicitly. Include your dispatch ID as a plain-text or bold field (e.g. `Dispatch-ID: 20260601-213416-myfeature`). Full contract: `scripts/lib/report_body_contract.py`.

**A dispatch report also needs an identity block, or its receipt never lands.** The table above (`validate_body()`) does not require Model or Provider — but the receipt-converter fail-closed model check does (`scripts/lib/append_receipt_internals/validation.py::_validate_model_present`): any dispatch-lane report without a real Model is REFUSED at receipt-write time, silently as far as the report contract is concerned (it can still pass `validate_body()` cleanly). Include a `Model:`/`Provider:` bold-field or frontmatter pair (e.g. `**Model**: sonnet`, `**Provider**: claude`) alongside your Dispatch-ID so the receipt actually gets written — see the fail-closed check itself (`_validate_model_present`) for the rationale, and `scripts/lib/report_to_receipt_converter.py` for how the refusal is logged (WARNING, dispatch-id + reason) and surfaced (`health/report_to_receipt_converter.json`).

## Dispatch lanes

Two lanes ship on main; T0 picks per task. Full decision rule, provider strings, concurrency, and failure modes live in **`docs/core/DISPATCH_RULES.md`** (tmux-spawn lane detail: `docs/operations/TMUX_SPAWN_LANE.md`).

- **`scripts/lib/tmux_interactive_dispatch.py`** (default) — leaseless ephemeral, isolated worktree per dispatch, drives an interactive `claude` worker on the subscription. Use for parallel/independent feature work.
- **`scripts/lib/subprocess_dispatch.py`** — terminal-pinned (Wave 5 smart-context, lease, triple-gate). Opt in per terminal with `VNX_ADAPTER_T{n}=subprocess`. Use for single-worker PRs that benefit from prior-round findings, or work expected to run >30 min. **No Anthropic SDK** — only `subprocess.Popen(["claude", ...])`.

**Provider→lane rule (hard).** `claude`/Opus/Sonnet panelists and workers route via the **tmux-spawn lane** (subscription) by default — NEVER `provider_dispatch` (it refuses claude: claude is not a provider-lane provider). Headless `claude -p` is opened as of 2026-08-11 (operator directive): it runs on the Max subscription, not API credits — measured 2026-08-11 via auth state (no `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`, keychain `subscriptionType: max`); the "API-metered post-cutover" reason this line used to carry was never true (Anthropic never carried out that cutover). Isolation and report-gate status for the headless lane: DISPATCH_RULES §8 (do not duplicate the mechanism here — it drifts). `kimi`/`glm`(litellm:zai)/`deepseek` route via `provider_dispatch.py`. Everything dispatches through the **single-entry door** (`vnx dispatch`), which decides the lane; calling a lane script directly is a side door (PR-12 consolidates the remaining ones, incl. the plan-gate panel). The plan-first gate (`plan_gate_panel.py`) honors this split.

For full documentation: `docs/`

<important if="working on schemas/migrations">
ADR-007 binding: every new central-DB table requires composite UNIQUE/PK over project_id.
See `docs/governance/decisions/ADR-007-multitenant-project-id-stamping.md`.
T0 must cite this explicitly in review-gate prompts.
</important>

<important if="working on review-gates or codex/kimi/gemini providers">
Per CC-COMMUNITY-SYNTHESIS-2026-05-29.md: codex for strict diff-mode, kimi for synthesis/operational angle.
Parallel review pattern proven 3x. Raw vs gate-routed dispatch = different audit trail — audit concern applies.
</important>

<important if="working on dispatch infrastructure or subprocess adapter">
Wave 6 elastic pool shipped 2026-05-16 (ADR-018, 9 PRs). Use `bin/vnx pool {status,scale,config,reap}`.
Backward-compat: terminal-pin via subprocess_dispatch.py still works.
SubprocessAdapter path: `scripts/lib/subprocess_adapter.py` + `scripts/lib/subprocess_dispatch.py`.
Single dispatch entry is the door (`vnx dispatch`): decision-tree enforced in code + side-door blocking.
Dispatch mechanics, lanes, and failure modes: `docs/core/DISPATCH_RULES.md`.
No T0 role file loaded (autonomous/project-root T0)? The staging flow for a governed dispatch
(track → central `stage_spec_bundle` → dry-run → fire → post-merge `link-pr`) is
`docs/core/DISPATCH_RULES.md` §12 "Autonomous dispatch — the staging flow".
</important>

<important if="working on receipt processor or governance/audit trail">
GOV-1/2/3 receipt-gap: raw `claude -p` bypasses receipts. rc9 shipped cheap-lane fixes.
Self-learning loop is dormant. Receipt processor must be running for audit trail integrity.
`.vnx-data/` is runtime state — never commit it.
</important>

<important if="working on tmux delivery or session hooks">
Hard rule: Enter ALWAYS as a separate tmux keystroke — combined send-keys misses delivery.
Leaseless lane live on main (#663+#664). Known bugs: timestamp drift, env-not-inherited.
</important>

## Path Resolution

All scripts must resolve project root via helper libraries — never hardcode paths or rely solely on env vars. Python: `scripts/lib/project_root.py`. Bash: `scripts/lib/vnx_resolve_root.sh`. Background: issue #225.

## Event Streams

`.vnx-data/events/T{n}.ndjson` is a **per-dispatch ring buffer**, not a long-running log. At the end of each subprocess-adapter dispatch, the live file is archived to `.vnx-data/events/archive/{terminal}/{dispatch_id}.ndjson` and truncated to 0 bytes. If you're debugging "the live file is empty", look in the archive directory instead.

Only subprocess-routed terminals produce this stream. TmuxAdapter-routed terminals (T0 default; T2/T3 unless `VNX_ADAPTER_T{n}=subprocess`) produce no per-terminal NDJSON.

## Supervisor Mode

Set per-project to enable unified supervisor (auto-respawn, lease sweep, runtime supervision):

```
VNX_SUPERVISOR_MODE=unified   # opt in to supervisor
```

Default (unset or `legacy`): no behavior change.

When enabled:
- Dispatcher prelude ticks `lease_sweep` every 30s
- Dispatcher prelude ticks `runtime_supervise` every 60s
- Recommend wrapping daemons via `dispatcher_supervisor.sh` and `receipt_processor_supervisor.sh`

See `docs/operations/UNIFIED_SUPERVISOR.md` for full guide.

<!-- Local maintainer overrides (optional, gitignored): machine-specific VNX notes live in
     ~/.claude/vnx-local.md; repo-local private notes in CLAUDE.local.md. Both load after this
     file and win on conflict. Keep secrets and absolute local paths out of this tracked file. -->
@~/.claude/vnx-local.md
