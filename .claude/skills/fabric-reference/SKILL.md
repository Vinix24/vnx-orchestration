---
name: fabric-reference
description: Read-only runbook for how the VNX fabric actually works — state resolution, the single-entry dispatch door, gate invocation, the horizon planning layer, the plan-gate panel, and the hard gotchas that repeatedly bite (PATH-break, dual-CLI, codex-cert, classifier-protected actions). Use when you need to look up a fabric operation instead of rediscovering it, or when a fabric command behaves unexpectedly. Companion to the t0-orchestrator skill (judgment) and vnx-manager (infra maintenance); this one is pure reference.
allowed-tools: [Read, Grep, Glob, Bash]
---

# VNX Fabric Reference

How the fabric works, as runbooks. This is lookup, not judgment — the `t0-orchestrator` skill owns orchestration decisions; `vnx-manager` owns infra maintenance. When a T0 rediscovers the same mechanism a third time, it belongs here.

Master-rule (see `role-orchestrator.md`): project files describe the project; the fabric describes itself. Fabric mechanism lives in the canonical role and here, never copied into a consumer's `CLAUDE.md`.

## Runbook: state resolution

Tracks, objectives, receipts, and coordination state live in the **central** store, never repo-local.

- Central store: `~/.vnx-data/<project_id>/state/` (resolved by the vnx runtime, not hardcoded).
- Never pin `VNX_STATE_DIR` to a repo-local `.vnx-data/state` path in a role or script — a repo-local pin forks state from central = split-brain.
- Resolution helpers: Python `scripts/lib/project_root.py`; Bash `scripts/lib/vnx_resolve_root.sh`.
- `project_id` never silently defaults to `vnx-dev` (ADR-007) — it resolves from `VNX_PROJECT_ID` / `.vnx-project-id` / git remote, else it rejects.

```bash
vnx status                 # situational awareness from central state
cat .vnx-data/state/t0_state.json | python3 -m json.tool   # SessionStart projection
vnx fabric-audit           # store-hygiene: split-brain stores, per-project ledgers, hash-chain
```

## Runbook: the single-entry dispatch door

Every dispatch goes through the one door, which decides the lane. Calling a lane script directly is a side door.

```bash
vnx dispatch <pending-id>          # the door; decides lane, runs phantom-guard
```

- **Provider→lane (hard):** `claude`/Opus/Sonnet route via the headless lane (`dispatch_envelope.run_envelope_headless_plan`, `claude -p`, subscription-preserving); the tmux-spawn lane was removed on 2026-09-18 — NEVER `provider_dispatch`. `kimi`/`glm`/`deepseek` route via `provider_dispatch.py`.
- Default build-worker model: sonnet (`workers-kimi-pinned`, pin_semantics=default; operator decision 2026-09-23). kimi-k3 is an explicit per-dispatch choice, not the default. A spec that names its own model or provider wins. `VNX_OVERRIDE_WORKERS_KIMI_PINNED=1` only downgrades the warning, it does not change routing.
- Rollback to legacy routing: `VNX_DISPATCH_LEGACY=1` (per terminal).
- Autonomous staging flow (no template): track → central `stage_spec_bundle` → dry-run → fire → post-merge `link-pr`. Full rule: `docs/core/DISPATCH_RULES.md` (§12 for autonomous).
- No Claude Code subagents (Task tool) for dispatch work — governed lanes only.

## Runbook: gate invocation

Three gates decide what merges: codex + kimi (adversarial review) + deterministic CI.

- Provider gates WRITE to the working tree. After a gate: stage the good changes, `git checkout --` any stray edits.
- codex = strict diff-mode; kimi = synthesis/operational angle (proven 3x parallel).
- Phantom-guard (`scripts/lib/phantom_guard.py`) rejects evidence-free GATE-GREEN receipts. Read-only review roles (`REVIEW_ROLES`) are exempt — a verdict, not a diff, is expected.
- Required headless review gate is not complete until BOTH the result record and the normalized headless report exist.
- Self-merge rule: allowed when local CI is green AND the codex-gate passed — but confirm the FULL GitHub CI is green first, not just the gate (a literal in a CHANGELOG once tripped the state-pin gate and broke main).

## Runbook: horizon (planning / future-state)

```bash
vnx horizon list                    # actionable-by-default: done hidden
vnx horizon list --all              # include done tracks
vnx horizon list --horizon now --phase queued   # the real open NOW work
vnx horizon show <track_id>
vnx horizon reconcile               # git-grounded auto-close CHECK (no writes)
vnx horizon reconcile --apply       # close CONFIRMED tracks (PR merge verified via gh)
vnx horizon close <track_id> --apply --approval-id <token>   # human-gated single close
vnx horizon drift                   # advisory declared-vs-derived divergence
```

- `bin/vnx` exposes the same verbs under the older `objective` name (`bin/vnx objective list`). pip `vnx` uses `horizon`.
- A track is `done` when reconcile confirms its PR merged. Done tracks stay in their band + the ledger; `list` hides them by default so a reconciled-but-unarchived track does not read as live drift.
- `VNX_AUTO_CLOSE` gates whether the SessionStart tick auto-closes; when unset, reconcile is manual.

## Runbook: the plan-first gate (panel)

```bash
vnx horizon plan-gate ...           # multi-model panel reviews a plan before any build
```

- Panel = Opus + Kimi + GLM-5.2-via-harness (three families → real disagreement).
- One flaky panelist (unparseable JSON, empty output) must not force a REVISE — quorum/abstain handles it (#910). Never treat a codex/glm flake as PASS; retry works.
- The panel has no closeout mode — Tier-2 closeouts can get category-error REVISEs; judge accordingly.

## Runbook: staging and firing a dispatch from a script

`dispatch_bridge.stage_spec_bundle` (`scripts/lib/dispatch_bridge.py:229`) writes `<data_dir>/dispatches/pending/<dispatch_id>/{instruction.md,dispatch-spec.json}` and never fires; `bridge_dispatch` (`scripts/lib/dispatch_bridge.py:498`) stages and drives the door in one call.

- **`data_dir` is a `Path`, never a string** (`scripts/lib/dispatch_bridge.py:253`); the stage CLI's `--data-dir` is `type=Path` for the same reason (`scripts/lib/dispatch_bridge.py:710`).
- **The stage-only CLI has no `--gate`** — its flags stop at `--task-class`, `--deadline`, `--tag`, `--track`, `--data-dir` (`scripts/lib/dispatch_bridge.py:689-710`), and it has no `--pr-id`/`--work-ref`/`--base-ref` either. Stage from Python when the round needs any of those: `gate=` is `scripts/lib/dispatch_bridge.py:238`, validated against `REGISTERED_GATE_NAMES` (`scripts/lib/dispatch_spec.py:87`). The legacy bridge CLI does have `--gate/--pr-id/--work-ref` (`scripts/lib/dispatch_bridge.py:770-772`) but it fires on staging, so it cannot be dry-run first.
- **A follow-up round** stages `pr_id=`, `work_ref=<branch>` and `base_ref="origin/<branch>"` (`scripts/lib/dispatch_bridge.py:241,248,251`). `pr_id` is the PR join key: the obligation is written with `pr_number`/`pr_id` (`scripts/lib/gate_obligations.py:346-347`) and `obligation_matches_pr` joins on exactly those (`scripts/lib/gate_obligations.py:224-239`) — without `pr_id` the round reads as owing no gate for that PR. `work_ref=dispatch/<parent>` also derives `parent_dispatch` (`scripts/lib/dispatch_bridge.py:178-200`).
- **Fire through the door in its own tmux window.** The door holds its caller for the lane's whole run (`deadline_seconds`, default 3600 — `scripts/lib/dispatch_bridge.py:240`), so keep it out of the T0 pane. Dry-run in the pane first (`vnx dispatch <dispatch-id> --dry-run` prints the compiled lane/model/billing plan and spawns nothing), then:

```bash
tmux new-window -d -n "dlv-<dispatch-id>" \
  "cd '${VNX_HOME:-$(git rev-parse --show-toplevel)}' && bin/vnx dispatch <dispatch-id>"
```

  A bare dispatch-id whose bundle sits at `pending/<id>/dispatch-spec.json` routes to the door, never the legacy lane (`docs/core/DISPATCH_RULES.md:258-259`).

## Runbook: context rotation

A T0 rotates at 500K context (`scripts/hooks/t0_context_guard.py:2-5`). Measurement, enforcement and execution are three pieces: `scripts/lib/t0_context_budget.py` measures, `scripts/hooks/t0_context_guard.py` enforces, `scripts/t0_rotate_spawn.sh` spawns the successor.

- **The bands** (`scripts/lib/t0_context_budget.py:39-44`, operator decision 2026-09-25): WARN 400K = prepare the rotation; FORCE 500K = start nothing new, rotate at the next boundary; HARD 600K = rotate now, work in flight included. Read the current band with `python3 scripts/lib/t0_context_budget.py <transcript>`; unmeasurable context is always `ok` (`scripts/lib/t0_context_budget.py:207-210`).
- **Enforcement**: at FORCE and above, PreToolUse denies starting new work (a dispatch, a new `/goal`, `gh pr create`) and UserPromptSubmit refuses a new `/goal`; at HARD the Stop hook demands the rotation now, work in flight included (`scripts/hooks/t0_context_guard.py:33-43`).
- **The rotation**: `/build-log wrap` writes `daily-log/handoff.md` — numbered next steps, plus a `## Actief /goal` section with the directive and the remainder when a `/goal` is running (`docs/operations/T0_CONTEXT_ROTATION.md:331-350`; writer `scripts/lib/context_rotation.py:208`). Then `scripts/t0_rotate_spawn.sh` opens the successor in a new tmux window (`scripts/t0_rotate_spawn.sh:460`) in the T0 folder when that folder is healthy (`scripts/t0_rotate_spawn.sh:418-442`).
- **A `/goal` directive longer than about 650 characters arrives as a pasted block after a rotation and is not executed (OI-2314).** The follow-up accepts only a real `<command-name>/goal</command-name>` user record; a prompt that carries the directive as pasted `<pasted_content>` text is no arrival (`scripts/lib/t0_rotation_delivery.py:181-187`; `docs/operations/T0_CONTEXT_ROTATION.md:593-595`). Keep the handed-over `/goal` under that length or the successor starts without a goal.

## Gotchas (the ones that repeatedly bite)

- **Bash starts every call in the session's start folder, not where the last one ended:** the cwd resets after each call, and a T0 starts in `.claude/terminals/T0`, so write every command from `${VNX_HOME:-$(git rev-parse --show-toplevel)}` or an absolute path — never from a relative path.
- **Do not "fix" that by moving the T0's start folder to the project root:** role-file discovery walks up from cwd, so a root start silently drops `role-orchestrator.md` with no error (`.claude/terminals/T0/role-orchestrator.md:137`); the rotation's start-folder logic is `scripts/t0_rotate_spawn.sh:418-442`.
- **PATH-break in a bash loop:** do not run `head`/`cmp`/`git` right after `vnx` in the same loop iteration — split into separate calls.
- **Dual-CLI:** the real `vnx` is the pip `vnx_cli` editable install (`vnx_cli/commands/*`), NOT the bash `bin/vnx` (`scripts/*`). Verify operator/cutover claims against `vnx_cli`, or you check the wrong code path. New commands go in BOTH.
- **codex-cert:** a dead codex-gate is usually a macOS cert-revoke → Trash the binary; fix with `npm i -g @openai/codex@latest` (node v22). `glm-harness` needs the local proxy on :4141 (restart per session, not persistent).
- **kimi-gate edits the working tree:** stage the good, revert the stray.
- **classifier-protected actions:** "fully autonomous" does NOT cover `.claude/settings.json` self-modification, git tag-push, or PyPI-publish — those stay human-gated.
- **`vnx start --help` runs start:** it spawns the daemon set (footgun); SIGKILL the supervisor root if triggered.
- **Central-mode paths:** embedded-layout path assumptions can break central-mode resolution (fleet burn-in) — resolve via the helpers, never hardcode `.vnx-data/` literals.

### Known issues (tracked, active)

- **An empty provider completion is usually a TRANSIENT lane issue, not a structural door bug:** if `bin/vnx dispatch <id>` for a provider lane (kimi/glm/deepseek) lands an empty completion, check the lane's health FIRST — kimi OAuth/quota, glm proxy on `:4141` — not the envelope. The door's provider path works end-to-end; a blank completion is now downgraded to a loud `failure` (embedding the raw spawn result) for ALL provider spawns as of #1107, and the door surfaces `EnvelopeResult.error` instead of a bare exit code, so the real cause is visible.
- **The plan-gate runs one seat (P2, 2026-10-17):** in `plan_gate_panel.py` (NOT the `/panel` skill) a run has the default seat (codex), plus opus only when codex returns BLOCK (`configs/plan_gate_panel.yaml`). A codex seat that abstains on unparseable verdict JSON or never delivers leaves zero readable verdicts, which is `INFRA_FAIL`, not a REVISE; the version stays unread and the next run gives it its seat round again. **Read the scoring-seat count in the log before trusting a plan-gate PASS/REVISE**.

## Where the source of truth lives

- Dispatch mechanics, lanes, failure modes: `docs/core/DISPATCH_RULES.md`
- Architecture + data flow: `docs/core/00_VNX_ARCHITECTURE.md`, `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`
- State fabric: `docs/core/STATE_FABRIC.md`
- Provider constraints (machine-readable SSOT): `scripts/lib/providers/provider_constraints.yaml`
- ADRs: `docs/governance/decisions/`
