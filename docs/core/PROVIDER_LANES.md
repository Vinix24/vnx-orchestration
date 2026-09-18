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

The trade is real and worth stating up front: subprocess workers are harder to
instrument than in-process SDK calls. I recover observability through receipts,
event streams, and a captured conversation log instead of SDK callbacks. The
rest of this doc is the map of which lane runs which work, and where the lanes
do not yet behave identically.

## The lanes

| Lane | Binary / transport | Auth | Primary use | Module |
|---|---|---|---|---|
| claude-headless | `claude -p` headless, via the door | OAuth subscription (an own `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` switches billing) | the only Claude worker lane (code + commit) | `scripts/lib/dispatch_envelope.py` (`run_envelope_headless_plan`) |
| claude-subprocess | `claude -p` headless, terminal-pinned | same auth as claude-headless | terminal-pinned single-worker PRs, opt-in per terminal | `scripts/lib/subprocess_dispatch.py` |
| codex | `codex exec` CLI | OpenAI CLI auth | strict diff-mode review | `scripts/lib/provider_dispatch.py` (`_dispatch_codex`) |
| gemini | gemini CLI | Google CLI auth | review | `scripts/lib/provider_dispatch.py` (`_dispatch_gemini`) |
| kimi | Kimi CLI (`kimi login` OAuth) | Kimi CLI OAuth | synthesis / operational review | `scripts/lib/provider_dispatch.py` (`_dispatch_kimi`) |
| deepseek-harness | `claude` CLI pointed at DeepSeek's Anthropic-compatible endpoint | own `DEEPSEEK_API_KEY`, key-auth | analysis / implementation on a non-Claude model | `scripts/lib/provider_dispatch.py` (`_dispatch_deepseek_harness`) |
| ollama | local Ollama resolver | none (local) | privacy-sensitive work, resolver layer | routed via litellm `ollama` sub-provider |

### claude-headless

The only Claude worker lane. The tmux-spawn lane was removed on 2026-09-18
(`docs/operations/TMUX_SPAWN_LANE.md`), so the door routes every `provider=claude`
dispatch here: `dispatch_envelope.run_envelope_headless_plan` runs `claude -p` in a
fresh isolated worktree, and the report gate and the receipt bind before the
dispatch counts as done. Isolation and report-gate status: `DISPATCH_RULES.md` §8.

**Worker model pin (worker-provider-kimi-flip, 2026-07-23):** T1/T2/T3 default to
`kimi-k3` on the provider lane (`workers-kimi-pinned` in `provider_constraints.yaml`,
renamed from `workers-sonnet-pinned`); T0 stays on Opus. Since the pin now resolves
to a non-Claude model, an explicit `provider=claude` override for a T1/T2/T3 build
worker on the claude lane is rejected (fail-loud, no silent claude/sonnet fallback) rather
than resolving to `claude-sonnet-5`.

**Worker permissions:** see `docs/operations/WORKER_PERMISSIONS.md`.

**Concurrency (#1017):** the claude serial lock is an N-slot semaphore, not a
single mutex. Default `N=10` (operator directive 2026-08-21, the
subscription-safe default); `VNX_TMUX_MAX_CONCURRENT` (env var, or the
registry-backed config-store value) raises or lowers it as an explicit
operator opt-in. See `DISPATCH_RULES.md` §6.

### claude-subprocess

The terminal-pinned headless lane. `claude -p` runs via `subprocess_dispatch.py`,
enriched with skill context, intelligence injection, and the repo map. It has the
most receipts behind it and is the bar the other lanes are measured against.

Billing follows the auth, not the lane: it runs on the subscription unless the
environment carries an own `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL`
(`DISPATCH_RULES.md` §7 and §8). Opt in per terminal with `VNX_ADAPTER_T{n}=subprocess`.

### codex

`codex exec` for strict diff-mode review. Codex reads a diff and reports
findings against it. This is the first review gate in the dual-LLM adversarial
pattern (ADR-008). It wires the event store as its audit sink so a codex
dispatch leaves the same NDJSON trail as a Claude dispatch.

### gemini

The second review gate. Gemini reviews from a different angle than codex; the
two together plus deterministic CI form the three-gate review stack. Bound to a
review contract hash like the codex gate.

### kimi

Kimi runs through the Kimi CLI with `kimi login` OAuth. VNX does not call the
Moonshot API for this lane (`kimi-via-cli-only`, blocking). Using the CLI keeps
cost attribution and rate-limit behavior in one place instead of split across an
API key and a CLI session. Kimi is the synthesis and operational-angle lane:
where codex finds diff-level defects, kimi reasons about whether the change
makes operational sense.

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

### ollama

Local Ollama for the resolver layer and privacy-sensitive work, where no data
leaves the machine. Routed through the litellm `ollama` sub-provider.

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
budget-exhausted-failure path through `govern()` — including
git-diff synthesis (`base_sha` = the pre-dispatch commit SHA) and schema-complete
frontmatter — instead of the legacy stub-only `_ensure_unified_report` (success) or
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
in `dispatch_envelope.py`, which still has its own local `_prepare()`/`_govern()`
rather than the shared `dispatch_prepare`/`dispatch_govern` modules) remain
un-migrated — tracked as follow-up slices of the same Option B parity work.

**The known gap.** An analysis-only dispatch on a provider_dispatch lane (review,
audit, no commit) can yield an empty report body. The synthesized report is
built from `completion_text`, and for some lanes the substantive output lands in
the event stream rather than in a single completion string. When that happens the
text is recoverable from `.vnx-data/events/` (live or archived under
`.vnx-data/events/archive/`), but the unified report itself reads thin. This is a
real gap, not a feature. Closing it is the Option B report-parity work targeted
for 1.1: bring the synthesized-report path up to the same evidence quality as the
worker-authored path.

If you are debugging a thin report on a review lane, look in the event stream
before concluding the dispatch did nothing.

## When to use which lane

| Work | Lane | Why |
|---|---|---|
| Code change that commits | claude-headless (the only Claude lane) | Subscription-billed; report gate binds before the receipt |
| Terminal-pinned single-worker PR | claude-subprocess (opt-in per terminal) | Lease management, Wave-5 smart-context |
| Strict diff review | codex (`codex exec`) | Reads the diff, reports defects against it |
| Second-angle review | gemini | Different reviewer, contract-bound, pairs with codex |
| Synthesis / operational review | kimi | Reasons about whether the change makes sense, not just diff defects |
| Analysis or implementation on a non-Claude model | deepseek-harness | Governed, own-key, account-safe; never on the OAuth subscription |
| Privacy-sensitive work, resolver layer | ollama | Local; no data leaves the machine |

Code-and-commit work goes to a Claude lane because that is where report
authorship and receipt quality are strongest. The only Claude lane is
claude-headless (subscription-billed). Review
and analysis work goes to codex-exec, gemini, kimi, or the harness, with the
report-divergence caveat above in mind for analysis-only dispatches.

## Lane maturity

I do not claim parity that is not measured.

- **claude-subprocess** has the most receipts and is the bar the others are held
  to. It is opt-in per terminal.
- **claude-headless** is the only Claude lane since 2026-09-18. Its isolation and
  report-gate status is in `DISPATCH_RULES.md` §8.
- **codex / gemini / kimi** are the review lanes. They emit receipts, reports,
  and an event trail. The synthesized-report thinness on analysis-only dispatches
  is the open gap (1.1).
- **deepseek-harness** is governed and account-safe with the own key. Its
  effectiveness was operator-measured on coding and tool tasks; that measurement
  is internal, not a published benchmark.
- **ollama** covers the resolver layer and local privacy work.

The receipt format and the intelligence layer are uniform across all lanes
today. Per-lane parity on the full PREPARE/GOVERN envelope, and the synthesized-
report parity for analysis-only dispatches, is the dispatch-unification work
targeted for the 1.x release.

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
local litellm proxy in front of OpenRouter) — the plain `litellm:zai` runner is
normalized to `glm-harness` at the bridge, backed by the `glm-via-harness-only`
constraint. A phantom-guard rejects evidence-free GATE-GREEN receipts. All of
this is on `main`.
