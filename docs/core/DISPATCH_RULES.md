# DISPATCH_RULES.md — the enforced dispatch ruleset (SSOT for T0)

> Canonical, machine-checkable dispatch rules extracted from the t0-orchestrator skill (PR-11).
> The skill stays slim and points here. Each rule is `id → condition → action`. Where a rule is
> enforced in code, the enforcer is named — the doc describes intent, the code is authoritative.
>
> SSOT cross-refs: provider constraints → `scripts/lib/providers/provider_constraints.yaml`;
> routing policy → `scripts/lib/providers/routing_policy.yaml`; pricing/registry →
> `scripts/lib/providers/wave7_models.yaml`. The single dispatch entry is the door
> (`vnx dispatch` / `scripts/lib/dispatch_cli.py`); it runs `compile_plan` + a permit for every lane.
> The door is merged (PR #896) and **default-ON** since 2026-06-24 (ADR-024;
> `VNX_SINGLE_ENTRY_DISPATCH` resolves on via `scripts/lib/dispatch_flags.py` `_DEFAULT_ENABLED = True`;
> `VNX_DISPATCH_LEGACY=1` is the absolute per-terminal rollback). Every dispatch routes through the
> door, which selects the per-lane path in §5/§8.
>
> End-to-end architecture (door → assembly → delivery → govern → intelligence): see
> **`DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`**.

## 1. Decision tree (first matching rule wins)

| id | condition | action |
|---|---|---|
| D1 GHOST | `receipt.dispatch_id` starts `unknown-` or empty | WAIT |
| D2 DUP | dispatch_id already in recent_receipts | WAIT |
| D3 REJECT | status=failure/failed OR risk>0.8 OR blocking findings | REJECT (don't hunt for reasons to approve) |
| D4 ESCALATE | architectural change OR new dependency OR policy question | ESCALATE |
| D5 INVESTIGATE | risk 0.3–0.8 OR advisory=hold | DISPATCH follow-up to T3 |
| D6 TERMINAL | all terminals busy / none ready | WAIT |
| D7 COMPLETE | completion_pct=100 AND no blockers AND no pending OIs AND gates verified | COMPLETE |
| D8 DEFAULT | receipt valid AND work pending | DISPATCH one block |

Efficiency: risk ≤ 0.3 + success + no blockers → fast path, skip deep verification. Verify (spot-check 3 claims) only when risk > 0.3. Evidence for verification: `git log --oneline -1 -- <file>`, grep the fix present, grep old pattern = 0, test pass-counts (automated counts are acceptable).

## 2. Gates before COMPLETE / merge

- **Review-gate evidence (3 surfaces, all required):** request in `.vnx-data/state/review_gates/requests/`, result in `.../results/`, normalized report in `$VNX_DATA_DIR/unified_reports/`. A result with empty `contract_hash` or empty `report_path`, or a report with no matching structured result, is **incomplete evidence → blocks completion**. `queued`/`requested` ≠ executing.
- **CI workflow conclusion (mandatory):** `gh run list --branch <head> --workflow "VNX CI" --limit 1 --json conclusion --jq '.[0].conclusion'` must equal `success`. `gh pr checks` listing green names is NOT sufficient — a multi-step job can still produce a `failure` conclusion.
- **Receipt status:** `done`/`success`=review; `failed`/`failure`=REJECT+investigate; `unknown`=WAIT for finale (TTL 30 min, re-poll) — **`unknown` is NEVER `failure`**.
- **Post-merge sequence (mandatory):** after `gh pr merge`, run `git pull --ff-only` then `vnx objective reconcile --project-id <pid>` to auto-close any track whose `pr_ref` points to the just-merged PR (advisory CHECK by default; add `--apply` to write).

## 3. PR size + iteration caps

- Target **150–200 LOC** delta; **hard cap 300** (override `--allow-large-pr` or split via track_dependencies). Exceptions (no cap): auto-generated migration SQL, single-bug-class test surface, mechanical renames. Put the LOC budget in the dispatch instruction.
- **B3.1** — if a review round finds ≥3 NEW blocking findings → stop fix-forward, dispatch `architect` for system-level review, decide rewrite-or-defer.
- **B3.2** — if a round has ≥1 NEW blocker AND cumulative blockers ≥6 → stop, scope-shrink + OI (override `--override-b3-cumulative` + reason).

## 4. Skill routing (route to the most specific specialist)

| Work | Skill |
|---|---|
| schema/migrations/SQLite/FTS5/multi-tenant/UPSERT/"rows missing" | **database-engineer** (MUST, not backend-developer, for `schemas/`, `scripts/migrate*`, `_import_table`, any SQLite schema touch) |
| VNX intelligence schema, central DBs, dispatch lifecycle, project_id propagation | intelligence-engineer |
| endpoints/scripts/refactor/general server-side | backend-developer |
| UI/dashboards | frontend-developer | review | reviewer (DB second-opinion when `schemas/` touched) |
| api design | api-developer | tests | test-engineer | perf | performance-profiler | security | security-engineer |
| architecture/planning (NOT implementation) | architect | skills | skill-creator |

## 5. Lane selection (which dispatch path)

| Task | Lane |
|---|---|
| Parallel / independent feature work (default) | `tmux_interactive_dispatch.py` — leaseless, isolated worktree, subscription-safe, fresh checkout/dispatch |
| Terminal-pinned single-worker PR (Wave 5 smart-context, lease, triple-gate) | `subprocess_dispatch.py` |
| Work expected to run >30 min | `subprocess_dispatch.py` (tmux-spawn has receipt-deadline failures on long workers) |
| PR review gate | `review_gate_manager.py` / `t0_gate_enforcement.sh` |
| Pure utility (no PR, no gate) | direct Bash |

PR/gate work is routed through `vnx dispatch`, which selects the lane; the single-entry door is the default single funnel for that selection (ADR-024, see header). `tmux_interactive_dispatch.py` defaults: `--isolated-worktree` on, `--model sonnet` (resolves to the pinned `claude-sonnet-5`, #1013 — see §8), `--base-ref origin/main`, `deadline_seconds` 3600 (receipt-wait ceiling, `stage_spec_bundle`); staging gate via `--from-staging-id` (ADR-006). Required: `--dispatch-id`, `--instruction`.

**Deadline override (20260716-deadline-passthrough):** the consumer-door `vnx dispatch-agent --deadline-seconds N` (300-14400, out-of-range hard-errors) overrides the 3600s default end-to-end via `dispatch_bridge.deliver_via_door(deadline_seconds=...)` -> `stage_spec_bundle(deadline_seconds=...)`. `bin/vnx dispatch` needs no separate flag — it only ever consumes an already-staged `dispatch-spec.json`, so a bundle staged with a custom `deadline_seconds` (via `stage_spec_bundle` directly, per §12) already carries it through.

**Worker permissions default (#1016):** detached tmux-spawn/subprocess workers launch with blanket `--dangerously-skip-permissions` by default — the spawn runs in an isolated per-dispatch worktree, so a scoped allow-list only stalls autonomous builds on prompts without adding real blast-radius protection. `VNX_WORKER_SCOPED=1` opts back into the scoped posture (`--permission-mode acceptEdits` + empty ambient MCP + role allow-list from `.vnx/worker_permissions.yaml`). A `working_tree_only` dispatch (plan-review/plan-write, no commit/push allowed) fail-closes: it refuses to run unscoped, because the commit/push deny only binds in the scoped code path (`scripts/lib/tmux_interactive_dispatch.py:1616-1625`). Full detail: `docs/operations/WORKER_PERMISSIONS.md`.

**Known gap (OI-188):** no lane reliably edits files under `.claude/skills/` — Claude treats the loaded skill dir as read-only. Edit skill files manually from T0/operator.

## 6. Concurrency — claude-tmux is subscription-session-capped (serialize, N-slot)

`claude-tmux` runs on Claude **subscription** sessions, which have a concurrent-session cap **shared with every other Claude agent on the account** (production agents, other terminals). Exceeding it = the dispatch immediate-exits in ~0.1s (`rc=1`), NOT a code error.

- **Default: serialize claude-tmux — one at a time.** Provider lanes (codex/kimi/glm/deepseek) do NOT use the Claude subscription and stay parallel.
- **Enforced** for door-routed dispatches by `serialize_lane()` (`scripts/lib/dispatch_serialization.py`, PR-6 + the concurrency-config follow-up, #1017), an account-level N-slot `flock` gated on `plan.serialization_class == "claude-tmux"`. Direct lane callers (e.g. the benchmark) bypass the door and MUST self-serialize: benchmark flag `--claude-serial`; policy `routing_policy.yaml: claude_serial_under_load`.
- **`VNX_TMUX_MAX_CONCURRENT` (N-slot semaphore, #1017)** — raises the cap above 1. `serialize_lane` acquires the first free slot among `N` independent `flock`s on `<lock_dir>/claude-tmux-slot-{0..N-1}.lock` (`dispatch_serialization.py:69-81,188-256`). Missing, `0`, negative, or unparseable values clamp to `1` (the subscription-safe default) — only a valid positive integer opts into more than one concurrent worker. This is an explicit, informed operator opt-in, not a default the code creeps towards: raising it trades serialization safety for throughput against the account's real session cap.
- **`--force-release-lock [<class>]` now releases every held slot for that class** (glob `<lock_dir>/<class>-slot-*.lock`, default class `claude-tmux`) — not a single lock file. Each matching slot is inspected independently: a dead holder is released silently, a live holder gets a loud double-run warning before its lock file is removed (the flock on the unlinked inode stays held by the original process; a fresh acquire on the new inode can then run genuinely in parallel with it). Only force-release a live holder that is hung and not progressing.
- **Diagnostic:** a Claude cell that DNFs at ~0.1s with `rc=1` = session-cap/rate-limit (capacity), not a bug. A Claude cell with `cost=$0.0000` confirms the subscription lane (headless `claude -p` would bill API). Remedy: wait for the usage-window reset, free a session, or (deliberately) raise `VNX_TMUX_MAX_CONCURRENT`; re-run with `--retry-from`.

## 7. Common dispatch failure modes (scan before any multi-lane / parallel dispatch)

These recur — observed, not hypothetical. A dispatch that "did nothing" or "billed wrong" is almost always one of these.

| Symptom | Root cause | Guardrail |
|---|---|---|
| Worker spawns but sits idle; instruction never runs, no start-receipt | tmux warmup/submit handshake missed on cold start (hook race) — common on claude/opus | Confirm the worker received the instruction (pane shows it running / start-receipt). If idle, hand-deliver: `tmux send-keys -t <pane> -l "<instr>"` then a **separate** `tmux send-keys -t <pane> Enter`. Enter is ALWAYS its own keystroke. |
| Parallel claude: warmup-misses + ~0.1s exits | subscription session-cap under concurrent claude load | Serialize claude-tmux (§6). |
| The whole Bash dispatch command is blocked by the PreToolUse hook | the spawn-guard greps T0's own Bash tool-call text for a raw spawn attempt; an inline `--instruction`/heredoc containing literal `claude -p`/`--print`, `kimi --print`/`-p`, `codex exec`, or `--dangerously-skip-permissions` trips it | Write the instruction to a file and pass `--instruction "$(cat /abs/path.md)"` — the hook sees the literal `$(cat …)` (no spawn token); the shell expands at run time. Keep the inline command free of those tokens. Note: since #1016 the tmux-spawn/subprocess lanes inject `--dangerously-skip-permissions` themselves inside the governed spawn — that internal use is not what the hook is guarding against; it blocks the *literal token appearing in T0's own Bash command*, i.e. a raw/manual spawn attempt. |
| Worker dirties the shared checkout / parallel cells collide | lane ran in the shared checkout; provider lanes don't isolate unless `VNX_ISOLATED_WORKTREE=1`, and (pre-PR-7) creation could silently fall back to shared | Provider/parallel work: `VNX_ISOLATED_WORKTREE=1` + verify a worktree was created (PR-7 makes this fail-loud). Never two writers in one checkout. |
| Claude silently bills API instead of subscription | claude routed headless (`claude -p` / stale `HEADLESS_FORCED_MODELS`) | Route claude via the tmux lane (subscription), never headless. `cost=$0.0000` = subscription; nonzero = API. |
| Provider dispatch rejected / wrong endpoint | wrong provider string | Use §8. `litellm:zai` is CORRECT for GLM (resolves to OpenRouter, satisfies `zai-via-openrouter-only`). `kimi` (CLI OAuth) is prod, NOT `litellm:moonshot`. |
| Worker can't find named files → no change → unscorable | seed/target paths not materialized at the worker's CWD | Pass `--dispatch-paths` for the touched paths; make the instruction's paths match the worker's CWD. Benchmark cells materialize the seed at the worktree root. |
| Lane reports `done` but worktree dirty / no push | lane auto-commit ran before a post-commit edit, OR long worker hit the receipt deadline | Verify the branch was pushed (`worktree_state: pushed`); recover by committing+pushing the verified worktree output (advance on verified evidence, never a fabricated receipt). |

## 8. Provider-string routing cheat-sheet

| Model(s) | Lane / provider string | Constraint |
|---|---|---|
| sonnet / opus (claude) | `tmux_interactive_dispatch.py` `--model sonnet`\|`opus` | Subscription. **T1/T2/T3 workers are pinned to `claude-sonnet-5`** (`workers-sonnet-pinned` constraint, bumped from 4.6 on 2026-07-05, #1013); T0 stays Opus. `_load_model_pins_from_yaml()` (`scripts/lib/dispatch_cli.py:190-216`) loads this unconditionally from `provider_constraints.yaml`, and the D4 model-tier step (`scripts/lib/dispatch_plan.py:182-190`) applies `model = pinned` unconditionally. The `VNX_OVERRIDE_WORKERS_SONNET_PINNED` env var *does* exist in the door code (`scripts/lib/providers/constraint_enforcer.py` `_override_key` at :70-71, applied at :468-469, honoring the constraint's `override_allowed: true`), but it only downgrades the `workers-sonnet-pinned` constraint *warning* (warn→overridden) — it does **not** re-route the worker, which still resolves to `claude-sonnet-5`. So a plan-floor above the pin must escalate to the operator rather than assume the override changes the routed model. Headless `claude -p` is opt-in and blocked by default (`claude-headless` constraint; `VNX_OVERRIDE_CLAUDE_HEADLESS=1` to open it, = API). Serialize under load (§6, N-slot via `VNX_TMUX_MAX_CONCURRENT`). |
| codex (gpt-5.x) | `provider_dispatch.py --provider codex` | Has tools. Retry once on lane-launch DNF (`codex_retry_once`). |
| kimi (k2.x) | `provider_dispatch.py --provider kimi` | **kimi CLI OAuth only** (`kimi-via-cli-only`). The CLI OAuth serves the current coding model (K2.7-Code, the default — no `-m`). NOT `litellm:moonshot` (bare API; violates the constraint; baseline-only). |
| GLM-5.1 | `provider_dispatch.py --provider litellm:zai` | Resolves to `openrouter/z-ai/glm-5` + `OPENROUTER_API_KEY` → satisfies `zai-via-openrouter-only`. The default single-entry door normalizes this to the `glm-harness` lane (`glm-via-harness-only`). GLM-4.5/4.6 rejected (`deprecated-glm-models`). |
| DeepSeek (tools) | `provider_dispatch.py --provider deepseek-harness` | Anthropic-compat via harness, own `DEEPSEEK_API_KEY` + hardening. NOT on the prod OAuth subscription (`deepseek-harness-subscription-blocked`). |
| DeepSeek (bare) | `provider_dispatch.py --provider litellm:deepseek` | Chat-only, NO tools. Baseline only. |
| local gemma | `provider_dispatch.py --provider local-gemma` | Free, local; mechanical / cutoff-resilient checks. |

## 9. Manager-block contract

Every dispatch: `[[TARGET:A|B|C]]` … `[[DONE]]`, headers `Role/Track/Terminal/PR-ID/Priority/Cognition/Dispatch-ID/Parent-Dispatch/Reason`, `Workflow` + `Context`, explicit success criteria. A headless-gate dispatch must name the expected report path + receipt/result linkage. Report contract (every worker): `## Summary` (≥50 chars) / `## Changes` / `## Verification` / `## Open Items`, with the `Dispatch-ID`. Validate roles: `python3 scripts/validate_skill.py --list`.

## 10. Operational runbooks (not inlined — see scripts)

Startup reconciliation, post-crash lease recovery, orphaned-dispatch handling, OI lifecycle, and PR-queue ops are operational recipes, not always-loaded skill content. Use: `vnx queue-status`, `vnx deliverable`, `.claude/skills/t0-orchestrator/scripts/dispatch_guard.sh`, the provider registry under `scripts/lib/providers/` (`wave7_models.yaml` + `routing_policy.yaml`), `scripts/runtime_core_cli.py`, `bin/vnx pool {status,scale,config,reap}` (Wave 6 elastic pool, ADR-018).

## 11. 1.0 transition-flag sunset list

The single-entry-door rollout ships behind transition flags. They are retired in a post-1.0 release (target 1.0.1), after the door has run stably as the default, so it becomes the one and only path (no dual-path branches left to drift). At 1.0.0 the flags below are still live — the `REMOVE` disposition is the plan, not a done deal. Disposition per flag:

| Flag | Disposition at 1.0 |
|---|---|
| `VNX_SINGLE_ENTRY_DISPATCH` | REMOVE — the door becomes the only path; the flag becomes a no-op, then deleted |
| `VNX_DISPATCH_LEGACY` | REMOVE — the legacy lane is deleted after one stable release on the door |
| `VNX_AUTO_ROUTE` | REMOVE — legacy-only smart-route; the door's compile_plan owns routing. `dispatch-agent.sh`'s `--auto-route` path is inert under the door and is removed with it |
| `VNX_USE_CENTRAL_DB` | CUTOVER VERIFIED 2026-07-10 — every consumer resolves state to central; the dual-write mirror is a proven no-op (`primary == central`). `vnx fabric-audit` GREEN. Flag + `dual_writer.py` scaffolding retained as the Phase-6-P6 rollback net; delete after Phase-0 burn-in |
| `VNX_STATE_DUAL_WRITE_LEGACY` | ROLLBACK NET — the `VNX_DISPATCH_LEGACY=1` + dual-write-back lever for Phase-0 (ADR-028); remove with P6, not before |
| `VNX_OVERRIDE_CLAUDE_HEADLESS` | KEEP — a real account-safety override, not transition scaffolding |
| `VNX_OVERRIDE_WORKER_PUSH_MAIN` | KEEP — a real governance override |
| `VNX_OVERRIDE_GLM_VIA_HARNESS_ONLY` | KEEP — the benchmark baseline escape for `glm-via-harness-only` |
| `VNX_OVERRIDE_PHANTOM_GUARD` | KEEP — operator escape for a legitimate no-op delivery |

KEEP = a genuine safety/operator override. REMOVE = transition scaffolding that only existed to make the flip reversible. The default-flip itself (`dispatch_flags._DEFAULT_ENABLED`) is the operator-gated cutover; the REMOVE flags are deleted only after it has run stably.

## 12. Autonomous dispatch — the staging flow

Proven end-to-end sequence for a T0 that has no role file loaded (no `.claude/terminals/T0/CLAUDE.md` context) to stage and fire one governed dispatch by hand.

1. **Create/confirm a track** — `python3 scripts/planning_cli.py objective add <track-id> "<title>" "<goal>" --horizon now`. A brand-new track is born plan-gated/blocked; that does **not** block dispatch — the door's track-link check (`_check_track_link_verdict`, `scripts/lib/dispatch_cli.py:368-431`) only rejects a `track_id` that is nonexistent, already `done`, or (when `VNX_REQUIRE_DISPATCH_TRACK=1`, default OFF) absent without a `no-track:<reason>` tag escape. An absent `track_id` under the default config is advisory-only (warn).
2. **Stage a spec bundle into the CENTRAL pending dir** — call `dispatch_bridge.stage_spec_bundle(instruction_text=..., dispatch_id=..., role=..., target_slot=..., provider=..., model=..., gate=..., dispatch_paths=..., tags=..., data_dir=~/.vnx-data/<project>)`. It resolves the data root via the same helper the door uses (`dispatch_cli._resolve_data_dir`) and writes `<data_dir>/dispatches/pending/<dispatch_id>/{instruction.md,dispatch-spec.json}` (`scripts/lib/dispatch_bridge.py:102-198`). Staging into a repo-local pending dir instead of the central one means `vnx dispatch <id>` won't find the bundle at bundle-existence check time (`_d_is_staged_form`, `scripts/commands/dispatch.sh:126-159`) and falls through to the deprecated legacy raw-file lane (ADR-025) — always stage central.
3. **Dry-run the door** — `bin/vnx dispatch <dispatch-id> --dry-run`. A bare dispatch-id with an existing bundle at `pending/<id>/dispatch-spec.json` routes to the door, not the legacy lane; `--dry-run` prints the compiled plan + permit fingerprint (lane/model/billing/route_reason) and spawns nothing.
4. **Fire** — `bin/vnx dispatch <dispatch-id>`. The single-entry door selects the lane per §5/§8: claude/Opus/Sonnet → tmux-subscription lane; kimi/glm/deepseek → `provider_dispatch.py`.
5. **After merge** — `python3 scripts/planning_cli.py objective link-pr <track-id> <pr-number>`. **Caveat:** `stage_spec_bundle`'s written spec payload has no `track_id` field (`scripts/lib/dispatch_bridge.py:166-195`), so a bridge-staged dispatch's `track_id` is always absent at the door. The TL-D2 auto-propagation that upserts `tracks.pr_ref` from `dispatch.track_id` on merge (`reconcile_commit_provenance`, #1034) therefore never fires for dispatches staged this way — `link-pr` is currently the only way to record the PR on the track for this path.

## 13. Receipt pull cadence — T0 cycle step 0 (ADR-035 §5/§5.3, §9 PR-8)

Receipts are no longer pushed into the T0 pane by default. **Step 0 of every T0 cycle, before reading any receipt, is a pull:**

```bash
python3 scripts/receipt_query.py pull --state-dir <state-dir> --json
```

- Reads everything appended to `t0_receipts.ndjson` since T0's own cursor (`receipt_pull_cursor.json` in the same state dir) and advances the cursor past what it read. A concurrent writer's not-yet-newline-terminated line is never consumed early (safe against a mid-append race).
- **First use on a given state dir:** run once with `--seed-now` to set the cursor to EOF and skip the historical backlog (the backlog stays on disk, still reachable via `by-dispatch`/`by-pr`/`since` — nothing is deleted).
- `--peek` reads without advancing the cursor, for a look-without-consuming check.
- Follow the pull with `python3 scripts/receipt_query.py digest --state-dir <state-dir> --json` for the accept/investigate/reject rollup, and periodically (same cadence, not a separate operational task) `python3 scripts/receipt_query.py reconcile-oi-pending --state-dir <state-dir> --json` to retry any `oi_pending` warnings — see ADR-035 §6.4. An entry that keeps failing past `--max-age-days` (default 7) shows up in both `reconcile-oi-pending`'s own `escalated`/`failed` counts and in `digest`'s `oi_pending_escalated_count` — a standing operator obligation, not a new alerting channel.
- This replaces waiting for `rp_delivery.sh`'s tmux pane-paste (`_deliver_receipt_to_t0_pane` / `_rpd_deliver_digest`), which is now suppressed by default (`VNX_RECEIPT_T0_PUSH` defaults to `0` — set it to `1` only as the transition escape hatch, e.g. a T0 setup that cannot run a pull cadence yet). The push code path and the flag are **kept**, not removed (§8/ADR-035 §5.3) — retiring them outright is a separate follow-up PR. The durability half (`send_receipt_to_t0`'s write-first-then-attempt-delivery) is untouched either way: the ledger line lands regardless of whether the pane notification fires.
- **OI-188 note:** this step belongs in the `t0-orchestrator` skill's cycle steps (`.claude/skills/t0-orchestrator/SKILL.md` §"2. Primary workflow"), mirroring the parked commit `24f71d22`'s intent. No lane can reliably write under `.claude/skills/` (Claude treats it read-only) — this section is the canonical, git-tracked source until an operator applies the equivalent edit to the skill file by hand. Track as an operator follow-up, not something to force through a lane edit.

See also: `docs/operations/RECEIPT_PIPELINE.md` (pipeline mechanics), ADR-035 §5 (pull interface design), §5.3 (push retirement), §6.4 (`oi_pending` lifecycle + escalation).
