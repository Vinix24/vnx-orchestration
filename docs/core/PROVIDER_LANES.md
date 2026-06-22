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
| claude-tmux-spawn | interactive `claude` in a tmux session | OAuth subscription (preserved) | default Claude worker (code + commit) | `scripts/lib/tmux_interactive_dispatch.py` |
| claude-subprocess | `claude -p` headless | API credits after June 15, 2026 | burst worker, opt-in (blocked by default) | `scripts/lib/subprocess_dispatch.py` |
| codex | `codex exec` CLI | OpenAI CLI auth | strict diff-mode review | `scripts/lib/provider_dispatch.py` (`_dispatch_codex`) |
| gemini | gemini CLI | Google CLI auth | review | `scripts/lib/provider_dispatch.py` (`_dispatch_gemini`) |
| kimi | Kimi CLI (`kimi login` OAuth) | Kimi CLI OAuth | synthesis / operational review | `scripts/lib/provider_dispatch.py` (`_dispatch_kimi`) |
| deepseek-harness | `claude` CLI pointed at DeepSeek's Anthropic-compatible endpoint | own `DEEPSEEK_API_KEY`, key-auth | analysis / implementation on a non-Claude model | `scripts/lib/provider_dispatch.py` (`_dispatch_deepseek_harness`) |
| ollama | local Ollama resolver | none (local) | privacy-sensitive work, resolver layer | routed via litellm `ollama` sub-provider |

### claude-tmux-spawn

The default Claude worker lane. `dispatch.sh` selects it unless a dispatch opts
into the headless burst lane. Interactive `claude` (never `claude -p`) is driven
inside a fresh, single-shot tmux session: spawn, deliver the instruction, wait
for the completion receipt, tear down. No session reuse, no leases, no fixed
terminal identity.

The point of this lane is billing. Interactive Claude Code stays on the
subscription after the June 15, 2026 billing change, while headless moves to API
credits. The lane guards that property: `_assert_no_headless_flags` rejects any
`-p`/`--print` flag in the assembled launch command, so the lane cannot silently
become a metered headless call.

The lane is subscription-preserving. Its structural work (`PREPARE`, `GOVERN`,
`RECEIPT`, `CAPTURE`) has shipped, which is what lets it emit a receipt and a
unified report and normalize the captured conversation into the event store. It
is still being hardened; I do not yet claim it matches the headless lane on
every surface (see Lane maturity).

### claude-subprocess

The headless burst lane. `claude -p` runs via `subprocess_dispatch.py`, enriched
with skill context, intelligence injection, and the repo map. It has the most
receipts behind it and is the bar the other lanes are measured against.

The June 15, 2026 billing change moves headless `claude -p` usage to API
credits, so this lane is now opt-in and blocked by default. The `claude-headless`
constraint refuses it unless `VNX_OVERRIDE_CLAUDE_HEADLESS=1` is set, to stop a
dispatch from silently billing API credits. Use it for burst/batch throughput
when the API cost is intended.

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
are the whole point of the system and the two Claude lanes do not produce them
the same way.

**tmux-spawn lane: the worker authors its own report.** The completion protocol
appended to every tmux dispatch instructs the worker to write its unified report
to `unified_reports/` and then emit the completion receipt as its last step. The
report body is what the worker actually wrote. `govern()` always runs as a
backstop: if the worker did not produce a usable report, it emits an honest
minimal body marked `contract_status="synthesized"` rather than leaving a gap.

**provider_dispatch lanes (codex, gemini, kimi, deepseek-harness, litellm): the
report is synthesized.** These lanes do not author a report. `_emit_governance`
builds the unified report from the captured `completion_text` of the spawn
result. The report body is a synthesis of what the process printed, not a
document the worker chose to write.

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
| Code change that commits | claude-tmux-spawn (default) | Subscription-preserving; worker authors its own report |
| Burst / batch implementation | claude-subprocess (opt-in, `VNX_OVERRIDE_CLAUDE_HEADLESS=1`) | Headless throughput; lowest overhead per dispatch; bills API credits |
| Strict diff review | codex (`codex exec`) | Reads the diff, reports defects against it |
| Second-angle review | gemini | Different reviewer, contract-bound, pairs with codex |
| Synthesis / operational review | kimi | Reasons about whether the change makes sense, not just diff defects |
| Analysis or implementation on a non-Claude model | deepseek-harness | Governed, own-key, account-safe; never on the OAuth subscription |
| Privacy-sensitive work, resolver layer | ollama | Local; no data leaves the machine |

Code-and-commit work goes to a Claude lane because that is where report
authorship and receipt quality are strongest. The default is claude-tmux-spawn
(subscription); the headless lane is the intentional, API-billed opt-in. Review
and analysis work goes to codex-exec, gemini, kimi, or the harness, with the
report-divergence caveat above in mind for analysis-only dispatches.

## Lane maturity

I do not claim parity that is not measured.

- **claude-subprocess** has the most receipts and is the bar the others are held
  to, but it now bills API credits and is opt-in (blocked by default).
- **claude-tmux-spawn** is the default and is subscription-preserving. Its
  structural PREPARE/GOVERN/RECEIPT/CAPTURE work has shipped. It is still being
  hardened; it is not yet proven equal to the subprocess lane on every surface.
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

## Single-entry door and the in-progress flip

Lane selection is moving behind one entry point, the dispatch door
(`scripts/lib/dispatch_cli.py`): a spec is validated, a plan is compiled, a
permit is issued, and only then does a lane execute. The door is built and
exercised under tests, but it is **default-OFF**. The single-source routing
predicate (`scripts/lib/dispatch_flags.py`) resolves `VNX_SINGLE_ENTRY_DISPATCH`
to disabled until the flip lands; `VNX_DISPATCH_LEGACY=1` is the absolute
rollback. Today, dispatches go through the existing per-lane paths described
above.

On the in-progress flip branch (`feat/dispatch-flip`), the door normalizes GLM
to a claude-CLI harness lane (`glm-harness`, the local litellm proxy in front of
OpenRouter) — the plain `litellm:zai` runner is normalized to `glm-harness` at
the bridge, backed by the `glm-via-harness-only` constraint. A phantom-guard
rejects evidence-free GATE-GREEN receipts. These are committed on the branch, not
on the released default path.
