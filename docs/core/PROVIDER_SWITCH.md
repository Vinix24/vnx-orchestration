# The provider switch

The operator's one place to turn model providers on and off. It covers every call the VNX engine makes to a non-claude, non-codex provider, in every project whose engine knows the switch. Code: `scripts/lib/provider_switch.py` (the library), `scripts/providers_cli.py` (the `vnx providers` command). Decision record: `docs/governance/decisions/ADR-044-provider-schakelaar.md`.

The switch decides whether a provider may be called at all. The content boundary (`scripts/lib/content_class.py`) decides whether given content may go to it. Both must allow; neither widens the other.

## Operator use

One file, `~/.vnx/providers.json`. `VNX_PROVIDER_SWITCH_FILE` moves it, and with it the audit log, the lock file and every quarantined copy. A change takes effect on the next call in every process, launchd jobs included: the file is read fresh on every decision, never cached.

```
$ vnx providers list                       # also: vnx providers, or --json
$ vnx providers off deepseek --reason "tegoed sparen"
deepseek switched OFF globally (was unset). Audit: ~/.vnx/providers_audit.ndjson
$ vnx providers off kimi --project mission-control --reason "weeklimiet"
$ vnx providers on deepseek --project mission-control    # override for one project
$ vnx providers reset kimi --project mission-control     # drop the override, follow global again
$ vnx providers check deepseek-harness     # exit 0 allowed, exit 4 switched off
$ vnx providers repair                     # rebuild an unreadable file from the audit log
$ vnx providers repair --empty             # or start empty: every family on
$ vnx providers usage [--json]             # usage and balance per provider, no model call
```

- `list` prints per family the global state, the per-project overrides and the effective state for the current project, with when, by whom and why. It also prints the enforcement line, each registered project with whether its engine knows the switch, the `VNX_OVERRIDE_PROVIDER_*` variables in its own environment and in the `com.vnx.*` launchd plists, and the consequences of the current state (no second review seat, a tiebreaker that will refuse, a panel below its floor, a tagger or scout that will skip).
- `check` is the hook for shell callers and for a project's own application code, which the switch does not cover (see "What is outside the switch").
- `usage` reads subscription usage and API balances. It is not part of the switch and never changes it.
- Exit codes: `0` ok, `1` write failure, `2` usage (unknown family, or a class that cannot be switched), `3` unreadable file, `4` refused by `check`, `5` write refused.

The pip `vnx providers` and `bin/vnx providers` run the same module.

## Families

A family names the model maker, so one switch covers every route to it. Switchable: `deepseek`, `glm`, `kimi`, `gemini`, `local`, `openrouter`.

| Family | Identifiers that map to it |
|---|---|
| `deepseek` | `deepseek`, `deepseek-harness`, `deepseek_harness`, `deepseek_gate`, `deepseek-*`, `litellm:deepseek*`, OpenRouter makers `deepseek` |
| `glm` | `glm`, `glm-harness`, `glm_harness`, `glm_gate`, `zai`, `glm-*`, `z-ai/*`, `litellm:zai*`, OpenRouter maker `z-ai` |
| `kimi` | `kimi`, `kimi_cli`, `kimi_gate`, `kimi-*`, `moonshot*`, `litellm:moonshot*`, OpenRouter maker `moonshotai` |
| `gemini` | `gemini`, `gemini_review`, `google`, `gemini-*`, `litellm:google/*`, `litellm:gemini/*`, OpenRouter maker `google` |
| `local` | `local`, `ollama`, `ollama:*`, `local-gemma`, `local_gemma`, `gemma-*-local`, `litellm:ollama/*` |
| `openrouter` | `openrouter`, and any `litellm:openrouter/<maker>/*` or `openrouter/<maker>/*` whose maker is not named above |

Not switchable, always allowed: `claude` (`claude`, `claude_headless`, `anthropic`, `claude-*`, `opus*`, `sonnet*`, `haiku*`, `fable*`), `codex` (`codex`, `codex_gate`, `openai`, `gpt-*`), `tooling` (`gh`, `ci_gate`, `wiring_gate`, `claude_github_optional`) and `unresolved` (`auto`, which the router resolves to a lane that is then checked). `vnx providers off claude` exits 2 with the reason.

A harness lane that redirects a claude launch (`deepseek-harness`, `glm-harness`) is decided on the family it reaches, not on `claude`. Hosts map the same way: `api.deepseek.com` is `deepseek`, `api.z.ai` and `open.bigmodel.cn` are `glm`, `api.moonshot.*` is `kimi`, the Google AI hosts are `gemini`, `openrouter.ai` is the maker of the model when known else `openrouter`. On a loopback host, port 11434 is `local` and port 4141 is `glm` (the litellm proxy that fronts GLM). The same ports on any other host are not in the map.

## Precedence

Per family, first match wins:

1. **The environment override.** `VNX_OVERRIDE_PROVIDER_<FAMILY>` can only turn a family off, for that process. `off`, `0`, `false` and `no` switch it off. `on`, `1`, `true` and `yes` are ignored with a WARNING that names the CLI as the way to re-enable. Any other value refuses the family with source `override-invalid`. Case-insensitive, trimmed. Every decision made by an override logs at WARNING with consumer and pid.
2. **The project section** of the file, for the project the decision is for.
3. **The global section.**
4. **The default, on.**

The project is resolved in this order: the explicit `project_id` argument, `VNX_PROJECT_ID` in the environment, the state directory of the process, none (then only the global section decides, and the decision says `scope=global`). The launchers stamp `VNX_PROJECT_ID` into every worker they start, so the door and the launch function decide for the same project. The explicit project `global` forces the global section, which a batch job uses for an item whose project it cannot resolve.

## The file and its version policy

```json
{
  "version": 1,
  "global":   {"deepseek": {"enabled": false, "changed_at": "...", "changed_by": "...", "reason": "..."}},
  "projects": {"mission-control": {"deepseek": {"enabled": true, "changed_at": "...", "changed_by": "...", "reason": "..."}}}
}
```

- `version` is an integer major. A reader accepts every major up to its own. A higher major is unreadable.
- A family name the reader does not know is ignored and logged once at INFO. An unknown field is ignored. A writer preserves both verbatim.
- Only these make the file unreadable: invalid JSON, a missing or non-integer `version`, a higher major (or one below 1), a section that is not an object, and an `enabled` that is not a boolean on a family the reader knows.
- A family for a provider that older engines have no route to is an additive change. A family that takes identifiers away from an existing family (for example splitting `mistral` out of `openrouter`) is a major bump, so older readers refuse instead of calling it under the old family.

Writes take an `fcntl` lock on `providers.json.lock`, re-read the file inside the lock, append one line to `providers_audit.ndjson` (actor, cwd, pid, parent command, argv, scope, family, old, new, reason) and only then replace the file atomically. A failed replace is marked with an `abort` record, so a replay never applies a change that did not land.

## Fail-closed and repair

- A missing file means no switches: everything is on.
- An unreadable file refuses every switchable family with source `unreadable` and the parse error as detail.
- An identifier or host the family map does not know is refused with source `unknown-identifier`.
- Non-switchable classes are never refused by the switch.

An operator who wrote the file meant something. A silent re-enable after a corrupt write would bring back exactly the calls that were switched off.

`vnx providers repair` handles an unreadable file. It keeps the file as `providers.json.corrupt-<UTC timestamp>`, rebuilds the state by replaying the audit log, writes it atomically and records the repair in the audit log. When the audit log is missing or unreadable as well it refuses, unless `--empty` is given: that writes an empty version 1 file, everything on. Repair refuses a readable or missing file.

### What fail-closed costs

Two refusals have no override. Both stay until `scripts/lib/provider_switch.py` learns the identifier or host.

1. **An `ANTHROPIC_BASE_URL` host the host map does not know.** `spawn_claude` maps a redirect in the environment its child receives to a family by host. A host that is not in the map raises `ProviderDisabledError` with source `unknown-identifier`, before anything is launched.
2. **A litellm model whose maker the family map does not know.** Both litellm runners call `require("litellm:<model>")` first. The runner emits `provider_switched_off` and exits with code 4.

To add one: put the host in `_HOSTS` (or a port on a loopback host in `_PORTS`), or the identifier in `_EXACT`, `_PREFIXES` or `_OPENROUTER_MAKERS`, in `scripts/lib/provider_switch.py`. Map it to the family of its maker, or to a non-switchable class when it is claude, codex or tooling. `test_every_routed_identifier_maps_to_a_family` in `tests/test_provider_switch.py` reads every identifier the fabric routes on; run it and it names what is unmapped. A new provider value fails CI, not production.

## What is not switchable, and why

`claude` and `codex` are outside the switch in version 1 (operator decision of 2026-10-12, 2b). They are the two seats every review and every worker lane rests on. Switching them off would stop the fabric, not save a cost. `tooling` calls no model. `unresolved` is `auto`, which the router resolves to a lane before anything is called.

## What is outside the switch

The switch covers the calls the VNX engine makes. A project's own application code is outside it: mission-control, for example, has its own DeepSeek lane and its own panel script. `vnx providers list` says so per project, and `vnx providers check <identifier> --project <pid>` is the hook for such code (OI-2048 tracks mission-control). A project whose engine predates the switch (a pinned older version) keeps calling every provider; `list` shows per project whether its engine knows it, and warns when a family that is off is unknown to that engine.

## Write protection

A worker must not turn providers on again. The write verbs (`off`, `on`, `reset`, `repair`) refuse with exit 5 when:

- the caller's working directory lies in a linked git worktree (walking up from the cwd to the first `.git`: a file whose `gitdir` points into `/worktrees/`; an unreadable `.git` file counts as linked), or
- the environment carries a dispatch marker: `VNX_CURRENT_DISPATCH_ID`, `VNX_DISPATCH_PATHS` or `VNX_WORKER_DISPATCH_ID`.

The check follows the caller, not the location of the script. No variable, flag or config key bypasses it. `VNX_WORKER_DISPATCH_ID` is stamped into every worker environment the launchers build, so the marker survives a change of directory. `templates/worker_permissions.yaml.tmpl` denies the verbs for claude workers as a third layer. `list`, `check` and `usage` work everywhere.

## How a refusal shows up

A refusal is never a reachability fact. It is not booked as `auth_rejected`, `credit_exhausted` or quota, and not written to the door's known-rejected check, the lane cooldown or the gate reachability record. Where it is recorded, its class and reason are `provider_switched_off`, so switching the family back on lets the next call through at once. The switch governs calls, never evidence: a run in flight is not killed, and a verdict already on disk keeps counting.

| Consumer | What the operator sees |
|---|---|
| The door (`vnx dispatch`) | `REJECT [provider-disabled]` with the decision detail, on dry-run and on fire, before reachability is probed. A fire books one dispatch receipt (status `blocked`, `delivery_refused`, failure class `provider_switched_off`); the dry-run writes nothing. |
| `provider_dispatch` | Prints `REJECT [provider_switched_off]` with the detail, writes a refusal receipt, exits 4 before any process starts. A refusal raised by the spawn itself is handled the same way. |
| The smart router | `lane_available` returns false with the decision detail for a disabled lane and does not record a lane failure. A spec with provider AUTO routes past it. |
| Review takeover chain | A switched-off candidate is passed over with a `provider_switched_off` hop in the same round. When every successor is off, the chain ends in `chain_exhausted` with each hop named. A seat with no successor is booked `not_executable` / `provider_switched_off`, a reason a later live result on the same head replaces. A decided verdict already on disk is not re-dispatched. |
| Gate runner and `vnx gate --only` | The runner calls `require` with the gate name before launch and books the refusal instead of a dispatch error. `--only <gate>` refuses before any launch and writes a skip-rationale line. |
| Plan-gate panel | `apply_provider_switch` removes every switched-off seat before dispatch and names it. The floor is `panel_floor(entitled)`, which is `min(2, entitled)` over the seats the run was entitled to, not over the shrunken panel. Below it the run refuses before dispatching anything and books no round. A panel entitled to two or more seats never certifies on one voice. Nothing substitutes for a removed seat. |
| Plan-gate tiebreaker | `TiebreakerSwitchedOffError` before any call. The track stays blocked; the message names re-enabling and operator attest. |
| Deliberation panel (`scripts/panel.py`) | The roster drops switched-off seats with a named line. The seats that remain are counted against the existing `synthesis_min_seats` floor (`configs/plan_gate_panel.yaml`); below it the run stops unless `--allow-degraded` is given. |
| Tagger, scout, receipt classifier | `get_provider` raises `ProviderDisabledError`. The call is skipped with one WARNING and counted where stats are kept. The receipt classifier decides per receipt by the receipt's own project. |
| Nightly deep analysis | A switched-off lane makes no attempt and is counted as outcome `provider_switched_off` (`deep_disabled_skips`); the beacon is `degraded` with that reason when every non-restricted attempt was skipped. |
| Dream | The consolidation cycle is skipped with reason `provider_switched_off` when kimi is off. |
| Any launch function | `ProviderDisabledError` before a command or request is built. |

Nothing is rerouted behind the operator's back. An explicit request for a switched-off provider is refused. Rerouting happens only where the operator already configured an order: the review takeover chain, and the router for provider AUTO.

## The switch and the takeover chain

`VNX_REVIEW_GATE_TAKEOVER_CHAIN` says which seat takes over when another cannot answer. The switch says which seats may be called. The chain walk asks the switch for every candidate, fresh each time. Switching off kimi, deepseek and glm leaves codex alone, so a PR cannot get a second review seat under the T0 rule. `vnx providers off` prints that consequence at write time, and the `chain_exhausted` record names `provider_switched_off` per hop so it reads differently from quota exhaustion. The merge door is not changed.

## The census and the guard

Every function that starts a non-claude provider process or request is in `LAUNCH_FUNCTIONS` (`scripts/lib/provider_switch.py`) and calls `provider_switch.require` before its first launch call. `tests/test_provider_switch_coverage.py` runs an AST detector over `scripts/` and `vnx_cli/` (spikes excepted) and fails when a launch function is neither in the census nor exempt with a reason, when `require` comes after the launch, or when an exempt entry matches nothing. There is no pending state: every entry is enforced, which is why the write verbs are live. A new consumer that calls a provider without `require` fails CI.
