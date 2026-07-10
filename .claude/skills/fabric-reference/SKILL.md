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
- Never pin `VNX_STATE_DIR=.vnx-data/state` in a role or script — a repo-local pin forks state from central = split-brain.
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

- **Provider→lane (hard):** `claude`/Opus/Sonnet route via the tmux-spawn lane (`scripts/lib/tmux_interactive_dispatch.py`, interactive, subscription-preserving) — NEVER `provider_dispatch`, NEVER headless `claude -p` (API-metered after 2026-06-15). `kimi`/`glm`/`deepseek` route via `provider_dispatch.py`.
- Default worker model: sonnet. Opus only with `--model opus` + `VNX_OVERRIDE_WORKERS_SONNET_PINNED=1`.
- Rollback to legacy routing: `VNX_DISPATCH_LEGACY=1` (per terminal).
- Autonomous staging flow (no template): track → central `stage_spec_bundle` → dry-run → fire → post-merge `link-pr`. Full rule: `docs/core/DISPATCH_RULES.md` (§12 for autonomous).
- No Claude Code subagents (Task tool) for dispatch work — governed lanes only.

## Runbook: gate invocation

Three gates decide what merges: codex + gemini (adversarial review) + deterministic CI.

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

## Gotchas (the ones that repeatedly bite)

- **PATH-break in a bash loop:** do not run `head`/`cmp`/`git` right after `vnx` in the same loop iteration — split into separate calls.
- **Dual-CLI:** the real `vnx` is the pip `vnx_cli` editable install (`vnx_cli/commands/*`), NOT the bash `bin/vnx` (`scripts/*`). Verify operator/cutover claims against `vnx_cli`, or you check the wrong code path. New commands go in BOTH.
- **codex-cert:** a dead codex-gate is usually a macOS cert-revoke → Trash the binary; fix with `npm i -g @openai/codex@latest` (node v22). `glm-harness` needs the local proxy on :4141 (restart per session, not persistent).
- **kimi-gate edits the working tree:** stage the good, revert the stray.
- **classifier-protected actions:** "fully autonomous" does NOT cover `.claude/settings.json` self-modification, git tag-push, or PyPI-publish — those stay human-gated.
- **`vnx start --help` runs start:** it spawns the daemon set (footgun); SIGKILL the supervisor root if triggered.
- **Central-mode paths:** embedded-layout path assumptions can break central-mode resolution (fleet burn-in) — resolve via the helpers, never hardcode `.vnx-data/` literals.

### Known issues (tracked, active)

- **Provider dispatches through the door return empty (`envelope-provider-lane-empty-completion`):** `bin/vnx dispatch <id>` for a provider lane (kimi/glm/deepseek — and codex/gemini *gates*) routes via `run_envelope_plan` → `ProviderAdapter` and lands an empty completion + silent failure receipt. The kimi CLI, `spawn_kimi`, and `provider_dispatch.py` main all work standalone — only the door's envelope path is broken. Interim: run provider gates/dispatches via `provider_dispatch.py` directly (side-door), or use the claude/sonnet tmux lane through the door (which works). Do NOT re-diagnose as a kimi-CLI/adapter break.
- **Plan-gate panel scores 2/5 seats (`plan-gate-panel-seat-robustness`):** in `plan_gate_panel.py` (NOT the `/panel` skill) the opus seat NO-VERDICTs on a `data_dir=None` report-resolution miss (#1102 class) and the codex + glm seats abstain on unparseable verdict JSON. A PASS may rest on only kimi + deepseek — confirm real seat count before trusting a plan-gate verdict.

## Where the source of truth lives

- Dispatch mechanics, lanes, failure modes: `docs/core/DISPATCH_RULES.md`
- Architecture + data flow: `docs/core/00_VNX_ARCHITECTURE.md`, `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`
- State fabric: `docs/core/STATE_FABRIC.md`
- Provider constraints (machine-readable SSOT): `scripts/lib/providers/provider_constraints.yaml`
- ADRs: `docs/governance/decisions/`
