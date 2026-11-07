# Worker lane stash posture (OI-2588)

**Status**: Active
**Last Updated**: 2026-11-07
**Owner**: T0 / T-MANAGER
**Purpose**: Record, per worker lane, whether a dispatch can build on it and how
that lane is stopped from touching the repository-wide `refs/stash`.

`refs/stash` belongs to the repository, not to a worktree. Every dispatch
worktree shares ONE stash, so a `git stash drop`, `clear` or `pop` of the wrong
entry is irreversible outside the worker's isolation boundary (OI-1447). The
claude lanes refuse it with `--disallowedTools` (`worker_permissions.
WORKER_CLI_DENIED_TOOLS`), the kimi lane with a PreToolUse hook
(`scripts/kimi_deny_git_stash.py`, OI-2587). This file covers the remaining
lanes.

## Measurement

Door lane resolution: every non-claude provider goes to `lane = "provider"`
(`scripts/lib/dispatch_plan.py:359`); the provider envelope then picks the
spawner per provider (`scripts/lib/envelope_adapters_provider.py`).

- **codex** — `codex-cli 0.164.0` (`codex --version`). `codex exec --help`
  exposes `-c/--config`, `-p/--profile`, `-s/--sandbox` and `--ignore-rules`;
  it has **no command denylist and no flag that accepts a rules file**. Execpolicy
  `.rules` (a file with `prefix_rule`) are loaded from `$CODEX_HOME` or the
  project only — `codex execpolicy check --rules <PATH>` is a *check tool*, not
  an `exec` input. `workspace-write` blocks writes outside the workspace root,
  and a worktree's shared `.git` common dir is outside that root, so it would
  block `git stash push` (a write) while allowing `git stash list` (a read) —
  but it equally blocks the `git commit`/`git push` a build worker must deliver.
  It is therefore not a stash-only deny.
- **gemini** — not installed on this host. `_build_gemini_cmd` passes only
  `--model` and `--output-format` (`scripts/lib/provider_spawns/gemini_spawn.py:347-349`);
  there is no denylist or rules-file mechanism in the argv builder.
- **litellm** — the door routes this lane to `spawn_litellm`
  (`scripts/lib/envelope_adapters_provider.py:335-357`), the one-shot HTTP
  runner (`scripts/lib/adapters/_litellm_runner.py`) that sends `model` +
  `messages` to `litellm.completion` and gives the model **no tools**. The
  tool-use agentic runner is a separate, `VNX_LITELLM_AGENTIC=1`-only path that
  the door never selects.
- **local-gemma** — the door routes this lane to `spawn_local_gemma`
  (`scripts/lib/envelope_adapters_provider.py:419-426`), which runs
  `ollama run <model> <prompt>` (or mlx-lm) and returns text: **no tools**.

## Posture

| Lane | Can it build? (proof) | Mechanism | Decision | How T0 verifies with a real run |
|---|---|---|---|---|
| claude (headless) | yes — `dispatch_plan.resolve_claude_lane` | `--disallowedTools Bash(git stash:*)` (`worker_permissions.WORKER_CLI_DENIED_TOOLS`) | already denied (OI-1447) | dispatch a claude build in a throwaway repo with one stash entry; `git stash` is denied, `git stash list` still works |
| kimi | yes — lane `provider` | PreToolUse hook `scripts/kimi_deny_git_stash.py` reading `WORKER_SHELL_DENIED_PATTERNS` | already denied (OI-2587) | same, on the kimi lane |
| codex | **yes** — `envelope_adapters_provider.py:298` + `codex_spawn.py:360-380` (full bypass by default); live availability fallback in `wave7_models.yaml:785-818` (`openai`→`codex`, `gpt-5.5`, lane `provider`) | **none** per run (see Measurement) | **not refused**: refusing a codex build would break a routing default (the tier fallback + `routing_recommendations.yaml:102` `codex-gpt-5-5` in `01_code_generation`). Left to T0. | not applicable — a codex build may run; T0 must weigh it |
| gemini | **yes** — `envelope_adapters_provider.py:308` + `gemini_spawn.py:347-349` | none; no denylist / rules file | **refused by the door** for a writing dispatch: `REJECT [stash-unrefused-build-lane]` (`dispatch_cli._stash_build_reject`), override `VNX_OVERRIDE_STASH_BUILD_LANE=1` | stage a `--provider gemini` build spec and `vnx dispatch <id> --dry-run`: expect the refusal and an unchanged `git stash list`; re-run with the override: expect a plan, no refusal |
| litellm | no — door routes to the one-shot no-tool runner (`envelope_adapters_provider.py:335`) | none needed | no change: no stash surface | — |
| local-gemma | no — door routes to the prompt-only `ollama run` runner (`envelope_adapters_provider.py:419`) | none needed | no change: no stash surface | — |

Read-only dispatches on gemini (a role that denies `Edit`, no write paths, no
`pr_id`) are not refused: they never mutate the stash. The `gemini_review` gate
does not pass through the door and is unaffected (it is retired as a gate, per
`dispatch_spec.RETIRED_GATE_NAMES`).

The rule itself is `dispatch_cli._stash_build_reject`, and it is evaluated as the
first step of `dispatch_cli._lane_verdict` — the seam `run_dispatch` already
calls after `compile_plan` and before the permit, on the dry-run and the fire
path alike. It sits there, not inline in `run_dispatch`, because that function
may not grow past its `main` size (OI-2588, round 2); the order of checks is
unchanged.

## Open item for T0

Codex is a build lane with no per-run stash deny. It is not refused because it
is the live availability fallback; refusing it would make tier-zero/tier-low
fallbacks fail. Closing it needs a codex-side mechanism (an execpolicy
`.rules` file placed inside the isolated worktree, or a codex release that
accepts a rules file per run), not a change in the door.
