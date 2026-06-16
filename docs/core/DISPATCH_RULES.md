# DISPATCH_RULES.md — the enforced dispatch ruleset (SSOT for T0)

> Canonical, machine-checkable dispatch rules extracted from the t0-orchestrator skill (PR-11).
> The skill stays slim and points here. Each rule is `id → condition → action`. Where a rule is
> enforced in code, the enforcer is named — the doc describes intent, the code is authoritative.
>
> SSOT cross-refs: provider constraints → `scripts/lib/providers/provider_constraints.yaml`;
> routing policy → `scripts/lib/providers/routing_policy.yaml`; pricing/registry →
> `scripts/lib/providers/wave7_models.yaml`. The single dispatch entry is the door
> (`vnx dispatch` / `scripts/lib/dispatch_cli.py`); it runs `compile_plan` + a permit for every lane.

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

All PR/gate work routes through the door (`vnx dispatch`). `tmux_interactive_dispatch.py` defaults: `--isolated-worktree` on, `--model sonnet`, `--base-ref origin/main`; staging gate via `--from-staging-id` (ADR-006). Required: `--dispatch-id`, `--instruction`.

**Known gap (OI-188):** no lane reliably edits files under `.claude/skills/` — Claude treats the loaded skill dir as read-only. Edit skill files manually from T0/operator.

## 6. Concurrency — claude-tmux is subscription-session-capped (serialize)

`claude-tmux` runs on Claude **subscription** sessions, which have a concurrent-session cap **shared with every other Claude agent on the account** (production agents, other terminals). Exceeding it = the dispatch immediate-exits in ~0.1s (`rc=1`), NOT a code error.

- **Rule: serialize claude-tmux — one at a time.** Provider lanes (codex/kimi/glm/deepseek) do NOT use the Claude subscription and stay parallel.
- **Enforced** for door-routed dispatches by PR-6's account-level `flock` (`serialize_lane`, `plan.serialization_class == "claude-tmux"`). Direct lane callers (e.g. the benchmark) bypass the door and MUST self-serialize: benchmark flag `--claude-serial`; policy `routing_policy.yaml: claude_serial_under_load`.
- **Diagnostic:** a Claude cell that DNFs at ~0.1s with `rc=1` = session-cap/rate-limit (capacity), not a bug. A Claude cell with `cost=$0.0000` confirms the subscription lane (headless `claude -p` would bill API). Remedy: wait for the usage-window reset or free a session; re-run with `--retry-from`.

## 7. Common dispatch failure modes (scan before any multi-lane / parallel dispatch)

These recur — observed, not hypothetical. A dispatch that "did nothing" or "billed wrong" is almost always one of these.

| Symptom | Root cause | Guardrail |
|---|---|---|
| Worker spawns but sits idle; instruction never runs, no start-receipt | tmux warmup/submit handshake missed on cold start (hook race) — common on claude/opus | Confirm the worker received the instruction (pane shows it running / start-receipt). If idle, hand-deliver: `tmux send-keys -t <pane> -l "<instr>"` then a **separate** `tmux send-keys -t <pane> Enter`. Enter is ALWAYS its own keystroke. |
| Parallel claude: warmup-misses + ~0.1s exits | subscription session-cap under concurrent claude load | Serialize claude-tmux (§6). |
| The whole Bash dispatch command is blocked by the PreToolUse hook | the spawn-guard greps the whole command; an inline `--instruction`/heredoc containing literal `claude -p`/`--print`, `kimi --print`/`-p`, `codex exec`, or `--dangerously-skip-permissions` trips it | Write the instruction to a file and pass `--instruction "$(cat /abs/path.md)"` — the hook sees the literal `$(cat …)` (no spawn token); the shell expands at run time. Keep the inline command free of those tokens. |
| Worker dirties the shared checkout / parallel cells collide | lane ran in the shared checkout; provider lanes don't isolate unless `VNX_ISOLATED_WORKTREE=1`, and (pre-PR-7) creation could silently fall back to shared | Provider/parallel work: `VNX_ISOLATED_WORKTREE=1` + verify a worktree was created (PR-7 makes this fail-loud). Never two writers in one checkout. |
| Claude silently bills API instead of subscription | claude routed headless (`claude -p` / stale `HEADLESS_FORCED_MODELS`) | Route claude via the tmux lane (subscription), never headless. `cost=$0.0000` = subscription; nonzero = API. |
| Provider dispatch rejected / wrong endpoint | wrong provider string | Use §8. `litellm:zai` is CORRECT for GLM (resolves to OpenRouter, satisfies `zai-via-openrouter-only`). `kimi` (CLI OAuth) is prod, NOT `litellm:moonshot`. |
| Worker can't find named files → no change → unscorable | seed/target paths not materialized at the worker's CWD | Pass `--dispatch-paths` for the touched paths; make the instruction's paths match the worker's CWD. Benchmark cells materialize the seed at the worktree root. |
| Lane reports `done` but worktree dirty / no push | lane auto-commit ran before a post-commit edit, OR long worker hit the receipt deadline | Verify the branch was pushed (`worktree_state: pushed`); recover by committing+pushing the verified worktree output (advance on verified evidence, never a fabricated receipt). |

## 8. Provider-string routing cheat-sheet

| Model(s) | Lane / provider string | Constraint |
|---|---|---|
| sonnet / opus (claude) | `tmux_interactive_dispatch.py` `--model sonnet`\|`opus` | Subscription. NEVER headless `claude -p` (= API). Serialize under load (§6). |
| codex (gpt-5.x) | `provider_dispatch.py --provider codex` | Has tools. Retry once on lane-launch DNF (`codex_retry_once`). |
| kimi (k2.x) | `provider_dispatch.py --provider kimi` | **kimi CLI OAuth only** (`kimi-via-cli-only`). The CLI OAuth serves the current coding model (K2.7-Code, the default — no `-m`). NOT `litellm:moonshot` (bare API; violates the constraint; baseline-only). |
| GLM-5.1 | `provider_dispatch.py --provider litellm:zai` | Resolves to `openrouter/z-ai/glm-5` + `OPENROUTER_API_KEY` → satisfies `zai-via-openrouter-only`. GLM-4.5/4.6 rejected (`deprecated-glm-models`). |
| DeepSeek (tools) | `provider_dispatch.py --provider deepseek-harness` | Anthropic-compat via harness, own `DEEPSEEK_API_KEY` + hardening. NOT on the prod OAuth subscription (`deepseek-harness-subscription-blocked`). |
| DeepSeek (bare) | `provider_dispatch.py --provider litellm:deepseek` | Chat-only, NO tools. Baseline only. |
| local gemma | `provider_dispatch.py --provider local-gemma` | Free, local; mechanical / cutoff-resilient checks. |

## 9. Manager-block contract

Every dispatch: `[[TARGET:A|B|C]]` … `[[DONE]]`, headers `Role/Track/Terminal/PR-ID/Priority/Cognition/Dispatch-ID/Parent-Dispatch/Reason`, `Workflow` + `Context`, explicit success criteria. A headless-gate dispatch must name the expected report path + receipt/result linkage. Report contract (every worker): `## Summary` (≥50 chars) / `## Changes` / `## Verification` / `## Open Items`, with the `Dispatch-ID`. Validate roles: `python3 scripts/validate_skill.py --list`.

## 10. Operational runbooks (not inlined — see scripts)

Startup reconciliation, post-crash lease recovery, orphaned-dispatch handling, OI lifecycle, and PR-queue ops are operational recipes, not always-loaded skill content. Use: `scripts/queue_status.sh`, `scripts/deliverable_review.sh`, `.claude/skills/t0-orchestrator/scripts/dispatch_guard.sh`, `scripts/provider_capabilities.sh`, `scripts/runtime_core_cli.py`, `bin/vnx pool {status,scale,config,reap}` (Wave 6 elastic pool, ADR-018).
