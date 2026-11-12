# Provider Lanes

VNX drives AI coding CLIs as subprocess workers. It never imports a vendor SDK.
Every Claude, Codex, Gemini, Kimi, DeepSeek, and Ollama call goes through the
provider's own binary or a CLI process VNX spawns. The `no-anthropic-sdk`
constraint in `scripts/lib/providers/provider_constraints.yaml` enforces this in
CI: any `import anthropic`, `from anthropic import`, or `@anthropic-ai/sdk`
string fails the grep gate.

This is an account-safety choice, not a stylistic one. Running an OAuth
subscription token through a provider SDK has gotten accounts banned (the
opencode and openclaw precedent). VNX stays CLI-driven so my production Claude
account is never the thing that pays for an SDK shortcut.

The trade-off: subprocess workers are harder to instrument than in-process SDK
calls. I recover observability through receipts, event streams, and a captured
conversation log instead of SDK callbacks. The rest of this doc is the map of
which lane runs which work, and where the lanes do not yet behave identically.

Verified against `origin/main` at `c41411fea` on 2026-11-12. `VERSION` says 1.8.0.
Work merged after that release is marked "on main, not yet released".

## The lanes

| Lane | Binary / transport | Auth | Primary use | Module |
|---|---|---|---|---|
| claude-headless | `claude -p` headless, via the door | OAuth subscription (an own `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` switches billing) | the only Claude worker lane (code + commit) | `scripts/lib/dispatch_envelope.py` (`run_envelope_headless_plan`) |
| claude-subprocess | `claude -p` headless, terminal-pinned | same auth as claude-headless | terminal-pinned single-worker PRs; the worker terminals T1-T3 always take it, T0 opts in | `scripts/lib/subprocess_dispatch.py` |
| codex | `codex exec` CLI | OpenAI CLI auth | strict diff-mode review (first seat of the default review stack); also a build lane | `scripts/lib/provider_dispatch.py` (`_dispatch_codex`) |
| gemini | gemini CLI | Google CLI auth | analysis / non-review work (not a reviewer; the door refuses a build dispatch on it) | `scripts/lib/provider_dispatch.py` (`_dispatch_gemini`) |
| kimi | Kimi CLI (`kimi login` OAuth) | Kimi CLI OAuth | synthesis / operational review; an explicit choice (build lane, or a stack or chain that names `kimi_gate`), in no default review stack since 2026-11-11 | `scripts/lib/provider_dispatch.py` (`_dispatch_kimi`) |
| claude_gate | `claude -p` headless, read-only (`Read`, `Grep`, `Glob`), MCP off | OAuth subscription, no `ANTHROPIC_*` variable in its environment | review seat, last link of the default takeover chain (after `deepseek_gate`) | `scripts/lib/claude_gate_lane.py` |
| deepseek-harness | `claude` CLI pointed at DeepSeek's Anthropic-compatible endpoint | own `DEEPSEEK_API_KEY`, key-auth | analysis / implementation on a non-Claude model; second seat of the default review stack (`deepseek_gate`, `deepseek-v4-pro`) | `scripts/lib/provider_dispatch.py` (`_dispatch_deepseek_harness`) |
| glm-harness | `claude` CLI pointed at a local litellm proxy (`:4141`) fronting OpenRouter | own OpenRouter key, key-auth (`zai-via-openrouter-only`, `glm-via-harness-only`) | review seat reached through an explicit takeover chain or stack (not in the default chain) | `scripts/lib/provider_dispatch.py` (`_dispatch_glm_harness`) |
| ollama | local Ollama resolver | none (local) | privacy-sensitive work, resolver layer | routed via litellm `ollama` sub-provider |

### claude-headless

The only Claude worker lane. The tmux-spawn lane was removed on 2026-09-18
(`docs/operations/TMUX_SPAWN_LANE.md`), so the door routes every `provider=claude`
dispatch here: `dispatch_envelope.run_envelope_headless_plan` runs `claude -p` in a
fresh isolated worktree, and the report gate and the receipt bind before the
dispatch counts as done. Isolation and report-gate status: `DISPATCH_RULES.md` §8.

**Worker model pin (operator decision 2026-09-23):** T1/T2/T3 default to `sonnet`
(`workers-kimi-pinned` in `provider_constraints.yaml`, `pin_semantics: default`; the id
keeps the name from the 2026-07-23 kimi flip). T0 stays on Opus. The pin is advisory:
`kimi-k3` on the provider lane is an explicit per-dispatch choice, and a build-worker
dispatch with `provider=claude` and no explicit model resolves to `sonnet` on the
headless lane without an override.

**Worker permissions:** see `docs/operations/WORKER_PERMISSIONS.md`.

**Concurrency (#1017):** the claude serial lock is an N-slot semaphore, not a
single mutex. Default `N=10` (operator directive 2026-08-21, the
subscription-safe default); `VNX_TMUX_MAX_CONCURRENT` (env var, or the
registry-backed config-store value) raises or lowers it as an explicit
operator opt-in. See `DISPATCH_RULES.md` §6.

### claude-subprocess

The terminal-pinned headless lane. `claude -p` runs via `subprocess_dispatch.py`,
enriched with skill context, intelligence injection, and the repo map. The worker
terminals T1, T2 and T3 always take this path: their `VNX_ADAPTER_T{n}` selector
is gone since T-G1 (`scripts/lib/terminal_lane.py`). Only T0 still opts in, with
`VNX_ADAPTER_T0=subprocess`, until T-B2 removes that.

Billing follows the auth, not the lane: it runs on the subscription unless the
environment carries an own `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL`
(`DISPATCH_RULES.md` §7 and §8).

### codex

`codex exec` for strict diff-mode review. Codex reads a diff and reports
findings against it. This is the first review gate in the cross-vendor adversarial
pattern (ADR-008, amended 2026-09-26). It wires the event store as its audit sink so a codex
dispatch leaves the same NDJSON trail as a Claude dispatch.

### gemini

A provider lane for non-review work. Gemini is not a reviewer: the operator
retired it as one on 2026-09-26, after a measurement of one `gemini_review`
record ever and no verdict among them (its binary was never on PATH). The
review stack is codex first and deepseek_gate on API credit second, with claude as the
last link of the takeover chain (see "Review stack and takeover chain" below). `gemini_review` is no longer a registered gate name
(`dispatch_spec.RETIRED_GATE_NAMES`): a `VNX_DEFAULT_REVIEW_STACK` that still
carries it drops the name with a warning, while a takeover chain or a
dispatch spec's declared gate that names it is refused outright. Existing
`gemini_review` result records stay readable as history.

### kimi

Kimi runs through the Kimi CLI with `kimi login` OAuth. VNX does not call the
Moonshot API for this lane (`kimi-via-cli-only`, blocking). Using the CLI keeps
cost attribution and rate-limit behavior in one place instead of split across an
API key and a CLI session. Kimi is the synthesis and operational-angle lane:
where codex finds diff-level defects, kimi reasons about whether the change
makes operational sense. The Kimi subscription was cancelled on 2026-11-11, so
no default review stack, takeover chain or gate profile names `kimi_gate`. The
lane and the gate stay registered for an explicit choice.

### deepseek-harness

DeepSeek run through the Claude harness. The `claude` CLI is pointed at
DeepSeek's Anthropic-compatible endpoint with `ANTHROPIC_BASE_URL`, authenticated
with my own `DEEPSEEK_API_KEY` in key-auth mode, with telemetry and the updater
disabled and MCP off. This is an execution lane, not a review lane: it reuses the
governed Claude spawn path so it emits a receipt and is not the raw `claude -p`
receipt-bypass.

The hard line: this lane requires the own DeepSeek key. The dispatch fast-fails
before any subprocess spawn when `DEEPSEEK_API_KEY` is absent. Routing DeepSeek
through the production OAuth subscription is blocked
(`deepseek-harness-subscription-blocked`), because that would redirect the
protected account identity to a third-party endpoint, which is the same ban risk
as importing the SDK. The keyed lane routes via `claude_harness_keyed` and clears
the pre-flight; the subscription lane (`claude_harness_subscription`) does not.

Measured on Claude Code 2.1.150 (2026-05-26): with the hardening above, zero
calls reached `api.anthropic.com` and all inference went to the DeepSeek
endpoint. That measurement is the basis for allowing the keyed lane at all.

### Harness lanes: a fabric-owned config dir

The glm and deepseek harness lanes start a `claude` child against a redirected
endpoint. Left alone the child would inherit the operator's `CLAUDE_CONFIG_DIR`
and send the user `CLAUDE.md` with its imports, the user memory and the personal
skills to a third-party provider. `ensure_harness_config_dir(lane)`
(`scripts/lib/provider_spawns/harness_config_dir.py`) gives each lane its own
directory, `<VNX_DATA_DIR>/harness-config/<lane>/` (mode 0700). The only file the
fabric writes there is a `settings.json` with the destructive Bash `ask` rules.
The overlay is applied last in the glm and deepseek spawns, together with
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, and every harness child carries `--bare` so it
does not walk up the tree for ancestor `CLAUDE.md` files. The helper fails closed
(`HarnessConfigDirError`): it never falls back to inheritance. A `plugins/`
directory may hold exactly the two plugin-catalog cache files the CLI writes on
its own (`PLUGIN_CACHE_ALLOWLIST`); any other entry keeps the refusal.

These lanes authenticate by key, so a fresh directory costs them no login. The
claude-headless lane does not use this helper: its subscription login is bound to
its config-dir path, so it keeps the `CLAUDE_CONFIG_DIR` of the process that fires
it. This is code behaviour; no ADR records it.

### ollama

Local Ollama for the resolver layer and privacy-sensitive work, where no data
leaves the machine. Routed through the litellm `ollama` sub-provider.

## Review stack and takeover chain

The default review stack is `codex_gate,deepseek_gate` (`VNX_DEFAULT_REVIEW_STACK`,
`scripts/lib/config_registry.py`). Seat 1 is a subscription reviewer (codex); seat 2
is `deepseek_gate` on API credit with `deepseek-v4-pro`. A project picks a gate
profile with `vnx gate-profile` (`scripts/lib/gate_profile.py`, ADR-060, on main,
not yet released):

| Profile | Review stack | Round cap | Takeover chain |
|---|---|---|---|
| `light` | `codex_gate` | 2 | codex, deepseek, claude |
| `standard` (default) | `codex_gate,deepseek_gate` | 3 | codex, deepseek, claude |
| `heavy` | `codex_gate,deepseek_gate,claude_gate` | 5 | codex, deepseek, claude |

How many seats a merge needs depends on the rule the project runs. By default the
risk class of the PR head decides: the first seat at `low` and `medium`, every
seat at `high` and `critical`. A project can switch to the signal rule
(`VNX_REVIEW_SEAT_RULE=first_seat_or_every_seat_on_signal`, off by default), which
asks the second seat only when a signal holds. Only vnx-dev runs the signal rule
today; every other project keeps the risk rule. Both rules and their fail-closed
inputs are in `DISPATCH_RULES.md` section 2.

A review seat that cannot read a PR hands it to the next gate in the takeover
chain. The default is `codex_gate,deepseek_gate,claude_gate`
(`gate_request_handler._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN`, equal to the registry
default at `config_registry.py:141-143`). The API-credit gate `deepseek_gate` comes
second and the subscription gate `claude_gate` ends the chain. `kimi_gate` and
`glm_gate` are not in the default chain: both stay registered gates, and an
explicit `VNX_REVIEW_GATE_TAKEOVER_CHAIN` or review stack that names them still
reaches them. A reviewer never runs the builder's model: such a gate is passed over
like an unavailable one (`scripts/lib/review_vendor.py`). A takeover record of
`deepseek_gate` signs the seat it took over at the merge door, because
`deepseek_gate` is a `Gate` enum member and a review peer in the closure verifier.
A chain that runs out ends as `chain_exhausted`, never as a silent re-dispatch.

The chain, the seat rules and the merge-door checks are described once, in
`DISPATCH_RULES.md` (section 2) and `docs/core/45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md`.
The decisions behind them: ADR-008 (amended 2026-09-26 and 2026-10-30) and ADR-037.

## Provider switch

`vnx providers` is the operator's one place to turn model providers on and off
(`scripts/providers_cli.py`, `scripts/lib/provider_switch.py`). One file,
`~/.vnx/providers.json` (`VNX_PROVIDER_SWITCH_FILE` moves it), holds a switch per
provider family, globally and per project. Families are `deepseek`, `glm`, `kimi`,
`gemini`, `local` and `openrouter`; one family covers every route to the same
model maker, so `deepseek` covers deepseek-harness, `deepseek_gate`, the
classifier provider and DeepSeek through OpenRouter. `claude`, `codex` and the
`gh`-based tooling gates are never switchable.

```
vnx providers list [--json]
vnx providers off|on <family> [--project <pid>] [--reason <text>]
vnx providers reset <family> --project <pid>
vnx providers repair [--empty]
vnx providers check <identifier>      # exit 0 allowed, 4 switched off
```

The switch is read fresh on every decision, so a change reaches the next call in
every process. It governs calls, never evidence: a refusal is booked as
`provider_switched_off`, never as an auth, credit or quota failure. Every function
that launches a non-claude provider process must call `provider_switch.require`
first. `LAUNCH_FUNCTIONS` in `provider_switch.py` is that census, and every entry in
it is enforced: each one calls `require` before its first launch call, which
`tests/test_provider_switch_coverage.py` asserts against an AST detector and
`tests/test_provider_switch_launch_functions.py` shows per function with its family
switched off. A claude launch whose env carries a non-Anthropic
`ANTHROPIC_BASE_URL` is decided on the family a harness lane names for that
redirect, else on the family of the host (an OpenRouter host on the model's maker);
a host the map does not know is refused. The write verbs are live: they refuse only
from a linked worktree or from inside a dispatch. `vnx providers list` prints the
enforcement state. The door books a refused fire as a `provider_switched_off`
receipt; the dry-run writes nothing. Review gates and the intelligence
callers honor the switch (see `DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`). The
switch and its census-enforced form have shipped since v1.7.2 and v1.7.4. The
full reference is `PROVIDER_SWITCH.md`; the decision is ADR-044.

## Report-writing divergence

This is the most important nuance to get right, because the receipt and report
are the whole point of the system and the lanes do not produce them the same
way.

**Removed lane, for reference.** The tmux-spawn lane (removed on 2026-09-18) had its
worker author its own report through a completion protocol, with `govern()` as the
backstop that emitted a minimal `contract_status="synthesized"` body when no usable
report existed. `govern()` itself stays in `dispatch_govern.py`.

**claude-subprocess lane: `govern()` wiring is opt-in (`VNX_SHARED_GOVERN=1`,
default off).** `dispatch_govern.py`'s own module docstring names the tmux lane
(removed) and the subprocess lane as its intended callers. Behind the flag,
`deliver_with_recovery` routes both the success and the
budget-exhausted-failure path through `govern()`, including
git-diff synthesis (`base_sha` = the pre-dispatch commit SHA) and schema-complete
frontmatter, instead of the legacy stub-only `_ensure_unified_report` (success) or
no report at all (final failure). `govern()`'s authored-report lookup also checks
the legacy subprocess worker filename (`<dispatch_id>_report.md`, from
`receipt_writer._ensure_unified_report`'s docstring convention) as a fallback
alongside the canonical `<dispatch_id>.md`. Receipt writing (`_write_receipt`) is
unchanged in both flag states. Flag default is off pending a burn-in period;
flipping it on is a follow-up, not part of this slice.

**provider_dispatch lanes (codex, gemini, kimi, deepseek-harness, litellm): the
report is synthesized.** These lanes do not author a report. `_emit_governance`
builds the unified report from the captured `completion_text` of the spawn
result. The report body is a synthesis of what the process printed, not a
document the worker chose to write. This lane (and the `claude_headless` lane
in `dispatch_envelope.py`, which prepares and governs through `envelope_prepare.py`
and `envelope_govern.py` rather than the shared `dispatch_prepare`/`dispatch_govern`
modules) remain
un-migrated: tracked as follow-up slices of the same Option B parity work.

**The known gap.** An analysis-only dispatch on a provider_dispatch lane (review,
audit, no commit) can yield an empty report body. The synthesized report is
built from `completion_text`, and for some lanes the substantive output lands in
the event stream rather than in a single completion string. When that happens the
text is recoverable from `.vnx-data/events/` (live or archived under
`.vnx-data/events/archive/`), but the unified report itself reads thin. This is a
real gap, not a feature. Closing it is the Option B report-parity work, still open: bring the synthesized-report path up to the same evidence quality as the
worker-authored path.

If you are debugging a thin report on a review lane, look in the event stream
before concluding the dispatch did nothing.

## When to use which lane

| Work | Lane | Why |
|---|---|---|
| Code change that commits | claude-headless (the only Claude lane) | Subscription-billed; report gate binds before the receipt |
| Terminal-pinned single-worker PR | claude-subprocess (opt-in per terminal) | Lease management, Wave-5 smart-context |
| Strict diff review | codex (`codex exec`) | Reads the diff, reports defects against it |
| Synthesis / operational review (explicit choice) | kimi | Reasons about whether the change makes sense, not just diff defects; in no default stack |
| Subscription reviewer, last link of the chain | claude_gate | Read-only `claude -p`; passed over when the builder ran a claude model |
| Analysis or implementation on a non-Claude model | deepseek-harness | Governed, own-key, account-safe; never on the OAuth subscription |
| Privacy-sensitive work, resolver layer | ollama | Local; no data leaves the machine |

Code-and-commit work goes to a Claude lane because that is where report
authorship and receipt quality are strongest. The only Claude lane is
claude-headless (subscription-billed). Review work goes to codex first and deepseek_gate second, with
claude_gate as the last link of the takeover chain.
Analysis work also goes to gemini. The report-divergence note above applies to
analysis-only dispatches.

## Lane maturity

I do not claim parity that is not measured.

- **claude-headless** is the only Claude lane since 2026-09-18. Its isolation and
  report-gate status is in `DISPATCH_RULES.md` §8.
- **claude-subprocess** is the terminal-pinned path for T1-T3.
- **codex / deepseek_gate / claude_gate** are the default review lanes; kimi stays a review lane for an explicit choice. **gemini** is a non-review
  provider lane. They emit receipts, reports, and an event trail. The synthesized-report
  thinness on analysis-only dispatches is the open gap.
- **deepseek-harness** is governed and account-safe with the own key. Its
  effectiveness was operator-measured on coding and tool tasks; that measurement
  is internal, not a published benchmark.
- **ollama** covers the resolver layer and local privacy work.

The receipt format and the intelligence layer are uniform across all lanes
today. Per-lane parity on the full PREPARE/GOVERN envelope, and the synthesized-
report parity for analysis-only dispatches, is the dispatch-unification work
that is still open.

## The orchestrator role on other harnesses

The T0 orchestrator is a role file, not a Claude-only feature. The canonical role
is `.claude/terminals/T0/role-orchestrator.md`. `vnx role sync --apply`
(`bin/vnx`, `cmd_role_sync`) refreshes it in a project and writes the same text as
a marked block into `AGENTS.md` (Codex) and `GEMINI.md` (Gemini) in the
same T0 folder. The block is byte-identical to the canonical role, a second sync
changes nothing, and `scripts/fleet_role_drift.py` fails when a mirror diverges
(measured 2026-10-27: `docs/investigations/2026-10-27-provider-agnostic-role-nameting.md`).
Agents are folders: each role under `agents/<role>/` carries its own `CLAUDE.md`
and `config.yaml`, resolved by `scripts/lib/agent_resolver.py`. The skills of a
role land at the provider's native destination under `VNX_AGENT_LAYOUT=on`
(off by default, ADR-050 and ADR-059, on main, not yet released).

That is the extent of it. The role text reaches another harness, and the lanes
above run workers on codex, kimi, gemini, deepseek and glm. A live T0 session on
another harness has not been run on this project, so I do not claim that VNX works
with any harness. The coupling points that remain are mapped in
`docs/investigations/2026-10-28-oss-harness-orchestrator.md` and decided in ADR-042.

## Single-entry door (default lane)

Lane selection runs behind one entry point, the dispatch door
(`scripts/lib/dispatch_cli.py`): a spec is validated, a plan is compiled, a
permit is issued, and only then does a lane execute. The door is **default-ON**
since 2026-06-24 (ADR-024). The single-source routing predicate
(`scripts/lib/dispatch_flags.py`) resolves `VNX_SINGLE_ENTRY_DISPATCH` on
(`_DEFAULT_ENABLED = True`); `VNX_DISPATCH_LEGACY=1` is the absolute per-terminal
rollback to the legacy per-lane paths described above. Every dispatch now goes
through the door.

At the door, GLM is normalized to a claude-CLI harness lane (`glm-harness`, the
local litellm proxy in front of OpenRouter): the plain `litellm:zai` runner is
normalized to `glm-harness` at the bridge, backed by the `glm-via-harness-only`
constraint. A phantom-guard rejects evidence-free GATE-GREEN receipts. All of
this is on `main`.
