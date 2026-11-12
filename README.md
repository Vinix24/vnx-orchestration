<div align="center">

# VNX Orchestration

### Governance-first runtime for AI coding agents

**Glass-box governance · Multiple CLIs on subscription, not per-call API credit · Local NDJSON receipts**

[![PyPI version](https://img.shields.io/pypi/v/vnx-orchestration?color=1f6feb&label=pypi)](https://pypi.org/project/vnx-orchestration/)
&nbsp;![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
&nbsp;[![License: MIT](https://img.shields.io/github/license/Vinix24/vnx-orchestration?color=2ea043)](LICENSE)
&nbsp;[![CI](https://img.shields.io/github/actions/workflow/status/Vinix24/vnx-orchestration/public-ci.yml?branch=main&label=CI)](https://github.com/Vinix24/vnx-orchestration/actions)

[![Stars](https://img.shields.io/github/stars/Vinix24/vnx-orchestration?logo=github)](https://github.com/Vinix24/vnx-orchestration/stargazers)
&nbsp;![Forks](https://img.shields.io/github/forks/Vinix24/vnx-orchestration?logo=github)
&nbsp;![Vendor SDK](https://img.shields.io/badge/vendor%20SDK-none-24292f)

**[Docs](docs/) · [Architecture](docs/core/00_VNX_ARCHITECTURE.md) · [State Fabric](docs/core/STATE_FABRIC.md) · [ADRs](docs/governance/decisions/) · [Writing](https://vincentvandeth.nl) · [Changelog](CHANGELOG.md)**

</div>

---

VNX runs AI coding CLI workers in isolated git worktrees, through review gates, with an append-only NDJSON receipt per dispatch.

It is a local control plane for the AI coding CLIs that already sit on your machine. One orchestrator dispatches work to ephemeral workers; each worker runs in its own git worktree; review gates decide what merges; every dispatch leaves a receipt. VNX drives `claude`, `codex`, `gemini`, `kimi`, and local `ollama` with no vendor SDK. It calls the CLIs as subprocesses and never imports a provider library.

That has two distinct payoffs. First, governance: every dispatch is reconstructable, from what was assigned to what was reviewed to what merged and what each gate cost. Second, billing: because the workers are the CLIs themselves, the claude and codex lanes (and the kimi lane, where a subscription exists) bill the subscriptions already on the machine, not per-call API credit. That is measured, not assumed: `cost=$0.0000` receipts under a `subscriptionType: max` auth state (see "Billing follows auth, not the lane"). It does not hold for every lane. GLM, DeepSeek and any OpenRouter-routed model bill API credit, and only run where a project chose them or, for DeepSeek, as the second seat of the default review stack (see "Multi-provider architecture" below).

Most agent projects build SDK-native agents. I orchestrate the binaries instead.

I built this for my own work, an estimated 3,000+ hours of development since February 2025. It is open source because the architecture is portable. Source is at [github.com/Vinix24/vnx-orchestration](https://github.com/Vinix24/vnx-orchestration).

This is not a security sandbox; it isolates work with git worktrees. It is not compliance certification; it produces a local, append-only, inspectable audit trail. It is optimized for human-gated coding workflows, not fully autonomous merges.

## What changed recently

The released version is in [`VERSION`](VERSION) and every release has its entry in the [CHANGELOG](CHANGELOG.md). Everything below is on `main` and not yet released; the sources are `## [Unreleased]` in the CHANGELOG, `changelog.d/` and the git log since `v1.8.0`. Each line points at the document that owns the subject.

**Learning and intelligence.** Learning proposes. A human decides ([ADR-065](docs/governance/decisions/ADR-065-leren-is-voorstellen.md)).

- **The learning loop stays a proposal queue.** A failure is classified and counted once per dispatch, proposals go through one locked queue store, and the operator records the decision with `vnx learning decide-rule`. The queue fills only with `VNX_LEARNING_LOOP_QUEUE=1` or persist mode, both off by default ([SELF_LEARNING_LOOP.md](docs/intelligence/SELF_LEARNING_LOOP.md)).
- **A refused ledger verdict stops the loop.** In queue and persist mode the cycle stops with a stated reason, and every verdict is appended to an NDJSON ledger before the JSON snapshot.
- **An approved rule is activated, nothing else.** The ingest activates only a rule with a recorded operator decision for this project, writes a `pattern_activation` record, and `scripts/deactivate_pattern_activation.py` switches it off for good.
- **Lessons have one home and one owner.** Capture routing and its audit are in [ADR-067](docs/governance/decisions/ADR-067-learning-capture-routing.md). Attribution of a learned pattern votes over three signals, transfers rows from a journal and can undo them ([ADR-068](docs/governance/decisions/ADR-068-learning-signaal-attributie.md)).
- **The effect of injection is measurable, and not yet measured.** The A/B arm is decided inside a frozen window, `scripts/intel_injection_join.py effect` refuses a verdict below 200 resolved dispatches per arm, and no window has produced a verdict ([ADR-069](docs/governance/decisions/ADR-069-intelligence-effectmeting.md)).
- **A corpus can be quarantined and restored.** `scripts/vnx_intelligence_corpus_quarantine.py` moves a corpus out of the live store with a manifest, a verify step against nine permit requirements and a `restore`.
- **Operator steering, first parts.** A bound transcript source, a closed class table and a proposal store with revisions and reopen records. Analysis and proposals on top of them are not on main yet ([ADR-072](docs/governance/decisions/ADR-072-operator-bijsturing-leert.md)).

**Dreaming.**

- **Auto-dream has a liveness watch.** `vnx dream install-scheduler` installs the nightly job and `com.vnx.dream-watch` at 04:00 local; `scripts/dream_liveness_probe.py` and `vnx doctor` read the same `dream_liveness/1` contract, so a missed or disabled night shows up instead of passing ([ADR-066](docs/governance/decisions/ADR-066-dream-liveness-watch.md)). It is opt-in and still burning in.

**Harness and providers.**

- **Agents as folders.** A role declares its skills in `agents/<role>/config.yaml`; with `VNX_AGENT_LAYOUT=on` (default `off`, `shadow` available) they are materialised where the provider discovers them ([ADR-050](docs/governance/decisions/ADR-050-agent-layout-fase-1.md), [ADR-059](docs/governance/decisions/ADR-059-agent-skill-native-materialisation.md)).
- **One orchestrator role, three instruction files.** `vnx role sync` refreshes `role-orchestrator.md` and the role block in `AGENTS.md` and `GEMINI.md` from the canonical role, and the T0 playbook has one source ([ADR-058](docs/governance/decisions/ADR-058-t0-playbook-source-en-verwijzing.md)).
- **One permission policy.** A single `PermissionPolicy` with a digest is the source for every lane ([ADR-057](docs/governance/decisions/ADR-057-provider-agnostic-permission-policy.md)).
- **A review provider is a registry row.** `GATE_LANE_ROWS` is the only place a gate is registered, and a test blocks new literal gate-name tables ([ADR-046](docs/governance/decisions/ADR-046-een-poortmotor.md), amended).

**Governance.**

- **The gate profile is per project.** Review stack, round cap, takeover chain and plan-gate seats come from one fail-closed read, with a safety floor no profile can lower ([ADR-060](docs/governance/decisions/ADR-060-poortprofiel-per-project.md)).
- **The door publishes a permit.** A merge needs a permit with identity, rights and a receipt owner, and a stale or orphaned permit is a finding ([ADR-061](docs/governance/decisions/ADR-061-deur-afgedwongen.md), [ENFORCEMENT_CHAIN.md](docs/core/ENFORCEMENT_CHAIN.md)).
- **The second review seat can follow signals.** After a codex PASS the door asks the second seat only when a signal holds. It is a per-project setting and active for this repository only ([DISPATCH_RULES.md](docs/core/DISPATCH_RULES.md) §2, [ADR-008](docs/governance/decisions/ADR-008-dual-llm-adversarial-review.md)).
- **Autonomy and lead time are measured.** A norm, period and threshold for autonomy ([ADR-056](docs/governance/decisions/ADR-056-zelfstandigheid-meten.md)), a daily lead-time store (`scripts/daily_metrics.py`, [ADR-063](docs/governance/decisions/ADR-063-dagelijkse-doorloopmeting.md)) and a receipt-processor heartbeat ([ADR-055](docs/governance/decisions/ADR-055-processor-liveness-heartbeat.md)).
- **A rework round is attributed to a builder model and review seat** ([ADR-071](docs/governance/decisions/ADR-071-herwerk-per-model-en-stoel.md)).
- **CHANGELOG fragments.** A PR adds `changelog.d/<name>.md` and never edits `CHANGELOG.md`; the release PR folds the fragments in ([changelog.d/README.md](changelog.d/README.md)).

Full history for every minor and patch back to 0.1.0 is in the [CHANGELOG](CHANGELOG.md).

## Writing

I wrote the architecture down as I built it. The full series is on [vincentvandeth.nl](https://vincentvandeth.nl). Start here.

**Governance and trust**
- [Glass-box governance for multi-agent AI](https://vincentvandeth.nl/blog/glass-box-governance-multi-agent-ai)
- [Governance scoring: agent trust and autonomy](https://vincentvandeth.nl/blog/governance-scoring-agent-trust-autonomy)
- [Autonomous agents do not exist](https://vincentvandeth.nl/blog/autonome-ai-agents-bestaan-niet)
- [ISA 62443: AI governance and industrial safety](https://vincentvandeth.nl/blog/isa-62443-ai-governance-industrial-safety)

**Receipts, audit, traceability**
- [The NDJSON receipt ledger for AI audit trails](https://vincentvandeth.nl/blog/ndjson-receipt-ledger-ai-audit-trail)
- [Traceability architecture: the AI decision receipt](https://vincentvandeth.nl/blog/traceability-architecture-ai-decision-receipt)
- [The external watcher pattern for AI agent observation](https://vincentvandeth.nl/blog/external-watcher-pattern-ai-agent-observation)

**Orchestration architecture**
- [What is AI orchestration: terminal dispatch](https://vincentvandeth.nl/blog/wat-is-ai-orchestration-terminal-dispatch)
- [Architecture beats models in AI agent dispatches](https://vincentvandeth.nl/blog/architecture-beats-models-ai-agent-dispatches)
- [Multi-model AI orchestration from a single terminal](https://vincentvandeth.nl/blog/multi-model-ai-orchestration-single-terminal)
- [Why no subagents in AI orchestration](https://vincentvandeth.nl/blog/waarom-geen-subagents-ai-orchestration)
- [Routing is not orchestration](https://vincentvandeth.nl/blog/routing-not-orchestration-openclaw-governance)

**Cost, context, production**
- [The real cost of AI agents in production](https://vincentvandeth.nl/blog/real-cost-ai-agents-production)
- [Zero-LLM context injection with VNX intelligence](https://vincentvandeth.nl/blog/zero-llm-context-injection-vnx-intelligence)
- [Context rotation at scale](https://vincentvandeth.nl/blog/context-rotation-scale-vnx-implementation)
- [Async quality gates for AI agent workflows](https://vincentvandeth.nl/blog/async-quality-gates-ai-agent-workflows)

## What works today vs what is opt-in

The audit trail is the whole point, so this section states maturity per feature. Verified against `origin/main` at `f0c113253` on 2026-11-12. `VERSION` is 1.8.0; items marked "on main" are merged and not yet released.

**Tier 1: in production.** Append-only NDJSON receipts, with hash-chain verification tooling (`audit_chain`) available but opt-in per project (`VNX_CHAIN_RECEIPTS`, off by default) and designed as epoch-rotation ([ADR-029](docs/governance/decisions/ADR-029-hashchain-epoch-rotation.md)); the main ledger is unchained today. Multi-CLI provider hub, no vendor SDK. Review gates run a subscription reader first (codex) and DeepSeek on API credit second, with deterministic CI as the third gate. The single-entry dispatch door (`dispatch_cli.py`), default-on since 2026-06-24 ([ADR-024](docs/governance/decisions/ADR-024-single-entry-door-default-dispatch-lane.md)), normalizing every lane and running a phantom-guard that rejects evidence-free GATE-GREEN receipts. Per-worker git worktree isolation, default-on since #1449, unconditional for the provider and subprocess lanes, with teardown classification for clean, committed, or dirty state. Headless `claude -p` is the only Claude lane since the interactive tmux dispatch lane was removed on 2026-09-18 (#1868); it bills the subscription, not API credits (see "Billing" below). Zero-LLM context injection and repo map. Cost tracking per gate. Governed memory (past + current). The gate profile per project: review stack, round cap, takeover chain and plan-gate seats come from one fail-closed read ([ADR-060](docs/governance/decisions/ADR-060-poortprofiel-per-project.md)), on main. A review provider is one row in `GATE_LANE_ROWS` ([ADR-046](docs/governance/decisions/ADR-046-een-poortmotor.md)), on main.

**Tier 2: shipped, opt-in, burning in.** Smart routing (`VNX_AUTO_ROUTE`, honoured only on the raw-file legacy path and ignored by the single-entry door), elastic worker pool (`bin/vnx pool`), track layer + roadmap autopilot (`VNX_ROADMAP_AUTOPILOT=1`), auto-dream consolidation (`vnx dream install-scheduler` installs the nightly job and a 04:00 liveness watch, [ADR-066](docs/governance/decisions/ADR-066-dream-liveness-watch.md); still burning in), and a self-learning loop that only proposes. The loop mines the receipt stream for recurring failures; with `VNX_LEARNING_LOOP_QUEUE=1` (or persist mode, `VNX_LEARNING_LOOP_PERSIST=1`) the proposals reach `pending_rules.json`, and an operator records the decision with `vnx learning decide-rule`. Nothing is applied without that approval, and the loop stops with a stated reason when its ledger verdict is refused. The injection A/B window is an operator step and no window has produced a verdict, so no effect on outcomes has been measured. Agents as folders (`VNX_AGENT_LAYOUT=shadow` or `on`, default `off`, [ADR-059](docs/governance/decisions/ADR-059-agent-skill-native-materialisation.md)) and the signal-based second review seat (a per-project setting, active for this repository only) are on main and in the same state. These default off and are not yet proven at the Tier 1 bar. Roll back the dispatch door per terminal with `VNX_DISPATCH_LEGACY=1`.

**Tier 3: designed, not built.** Parallel multi-track execution, wave scheduler, merge lock, file-scope derivation. This is architecture, not a shipped feature ([ADR-020](docs/governance/decisions/ADR-020-parallel-track-execution.md)): worktree isolation is not yet guaranteed race-free under parallel dispatch. Also designed, not built: operator steering beyond its first parts (source binding, class table, proposal store) has no analysis or proposals on main yet ([ADR-072](docs/governance/decisions/ADR-072-operator-bijsturing-leert.md)), and a live T0 orchestrator on a non-Claude harness is not proven on this project.

## Install

```bash
pip install vnx-orchestration
vnx init                                  # scaffold a VNX project in the current dir
vnx migrate                               # apply runtime DB migrations
vnx doctor                                # environment and dependency checks
```

For an actual dispatch, worker, report, and receipt, see "Your first dispatch" below. It covers two prerequisites this four-line snippet does not show.

### Prerequisites

VNX does not run models itself: it drives existing coding CLIs as subprocesses and governs the
result. The default dispatch lane needs an **installed + authenticated `claude` CLI** on your PATH
(other lanes: `codex`, `gemini`, `kimi`), and using it incurs that provider's subscription/credit
usage. `vnx dispatch-agent` fails at spawn if no worker CLI is present; `vnx doctor` flags this with
a `tool:worker-cli` warning. (Zero-key exploration of the governance flow is not currently shipped;
the old replay demo was retired.)

There are two binaries on purpose. The pip `vnx` covers the essentials: `init`, `migrate`, `doctor`, `status`, `dispatch-agent`, `track`, `pool`, `dream`, and as of `#1462` also `gate-check`, the same deterministic pre-merge GO/HOLD check the fabric's own CI runs, now usable without a checkout. It also covers `horizon`/`objective`, `deliverable`, `attest`, `handoff`, `role`, `update`, `release`, `learning`, `fabric-audit`, `subsystems`, `version`, `pr-ready` and `worktree-release`. `providers`, `guard` and `preflight` ship in 1.8.0. Checkout-only operator commands still live behind `./bin/vnx`: `new-worktree` and the rest of the operator surface (worktree lifecycle, snapshot/restore, staging). For those, clone the repository and run `pip install -e .` from the checkout.

For a shared central install, measure a rollout before it happens. The [install-central runbook](docs/operations/install-central-runbook.md) section "Before a rollout" covers the read-only preflight and the four classes it blocks on, release verification, the `release:main-drift` doctor check, and the three steps that stay manual: the release itself, the `current` flip and reloading the launchd jobs.

## Your first dispatch

I ran this end to end in a scratch directory on 2026-08-12 to write this section. Two things broke on a completely fresh project, and the Install snippet above does not warn about either.

**A fresh `vnx init` project is not a git repository.** `vnx init` scaffolds config, databases, and `agents/`, but it does not run `git init`. Dispatching before you do fails at the worktree-creation step, and the error is unhelpful: a permission error against the read-only central install path, not a message pointing at "run git init."

**The default lane needs a GitHub-hosted `origin` remote, with `gh` authenticated.** Worktree isolation is unconditional on this lane (see "What works today vs what is opt-in" above), and that path calls `gh pr create` before it will record the dispatch as done. A local-only remote is not enough; without one the worker still does real work, but the receipt reads `failed`.

The commands, run for real:

```bash
mkdir readme-walkthrough-demo && cd readme-walkthrough-demo
vnx init
git init -q && git add -A && git commit -q -m "initial scaffold"
git remote add origin git@github.com:<you>/<repo>.git && git push -u origin main
vnx migrate
vnx doctor
```

`vnx doctor` lists `examples/hello-world/` as a resolvable agent alongside the real ones. It does not actually work: the single-entry door validates the role against its own `agents/` registry (this repository's thirteen built-in fleet roles: `backend-developer`, `blog-writer`, `code-reviewer`, `deliberation-panelist`, `frontend-developer`, `linkedin-writer`, `orchestrator`, `plan-reviewer`, `quality-engineer`, `research-analyst`, `review-gate`, `security-engineer`, `system-architect`), not `examples/`. `doctor` discovers `hello-world`; the dispatch door rejects it with `role 'hello-world' is not a known agent role`. Use one of the thirteen role names instead:

```bash
vnx dispatch-agent --agent research-analyst \
  --instruction "Quick depth research: what does 'orchestration' mean in software systems? 3 bullet points."
```

```
Dispatching to agent 'research-analyst' (dispatch_id=D-4d7b2a30, project_id=readme-walkthrough-demo) ...
```

The worker runs headless (`claude -p`, the only Claude lane since the tmux dispatch lane was removed on 2026-09-18) in an isolated git worktree on branch `dispatch/D-4d7b2a30`. In my run it wrote `findings.md`, committed it as `712280f`, and left a report at `$VNX_DATA_DIR/unified_reports/D-4d7b2a30.md`. Real output, trimmed:

```yaml
---
dispatch_id: D-4d7b2a30
provider: claude
model: sonnet
role: research-analyst
duration_seconds: 42.94
exit_code: 0
token_usage: {input: 0, output: 369, cache_read: 0}
cost_usd: 0.0
lane: claude_headless
---
## Summary
Quick-depth research task: define what "orchestration" means in software systems...
## Changes
- Added `findings.md` (new file) ...
- Committed as `712280f` on branch `dispatch/D-4d7b2a30`.
## Verification
...
## Open Items
None
```

The receipt lands in `$VNX_DATA_DIR/state/t0_receipts.ndjson`, one JSON line per dispatch. Query it rather than grep the file directly:

```bash
$ python3 scripts/receipt_query.py by-dispatch D-4d7b2a30 --state-dir $VNX_DATA_DIR/state
1 receipt(s) for dispatch D-4d7b2a30:
  ? D-4d7b2a30 [failed] schema_version=2 pr=-
```

That receipt reads `[failed]`, and it is correct: I pointed `origin` at a local bare repository instead of a real GitHub remote, `gh pr create` had nothing to talk to, and the receipt records exactly that (`autopr_reason: "gh pr create failed for branch ... (no open PR found on retry)"`). The worker's report, commit, and `findings.md` are real and complete; the receipt still refuses to call the dispatch done without a PR in front of it. That refusal is the governance model working, not a bug I hit while writing this. Point `origin` at a real GitHub remote and this same sequence ends with `status: done`.

After a dispatch the T0 still has to record whether the outcome is accepted: `receipt_query.py open-outcomes` lists the dispatches whose outcome still needs a decision (a reject, investigate or unknown outcome with none recorded; accepted and superseded outcomes are not listed) and `receipt_query.py decide <dispatch-id> accept|reject --reason ... --state-dir <state-dir>` records it. Before merging, `vnx pr-ready <PR>` shows what the merge door will check on the PR head, and the merge itself goes through `scripts/pr_merge.py` ([DISPATCH_RULES.md](docs/core/DISPATCH_RULES.md) §2.1 and §13).

## Architecture

VNX uses a T0 orchestrator and ephemeral workers. The old fixed T1-T3 mental model is no longer the core; workers spawn per dispatch and leave behind receipts, reports, and worktree state. (A fixed, terminal-pinned T0-T3 model still exists for the opt-in subprocess lane; the ephemeral-per-dispatch model is the default.)

The end-to-end deep dive (intent → single-entry door → bundle assembly → delivery → receipt → governance → review gates, plus the intelligence-injection contract) is in [docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md](docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md). The full component/data-flow reference is [docs/core/00_VNX_ARCHITECTURE.md](docs/core/00_VNX_ARCHITECTURE.md).

```
   T0 orchestrator  (plans, dispatches, reviews; does not write code)
        |
        |  one dispatch
        v
   +----------------+   +----------------+   +----------------+
   | ephemeral      |   | ephemeral      |   | ephemeral      |
   | worker         |   | worker         |   | worker         |
   | (git worktree) |   | (git worktree) |   | (git worktree) |
   +-------+--------+   +-------+--------+   +-------+--------+
           |                    |                    |
           +---------+----------+----------+---------+
                     v                     v
              review gates          worktree teardown
          (codex / deepseek / CI)    (clean / pushed / dirty)
                     |
                     v
   append-only NDJSON receipts  (one per dispatch; hash-chain verify via audit_chain, opt-in)
```

Claude has one lane: headless `claude -p` (`claude_headless`). It was blocked by default until an operator directive opened it on 2026-08-11 (#1455, `lane_safety.headless_block.enabled: false` in [`routing_policy.yaml`](scripts/lib/providers/routing_policy.yaml)), became the default on 2026-08-26, and became the only Claude lane when the interactive tmux lane was removed on 2026-09-18 (see [TMUX_SPAWN_LANE.md](docs/operations/TMUX_SPAWN_LANE.md)). It bills the Claude subscription, not API credits. A `cost=$0.0000` receipt confirms it; billing only changes if the environment carries its own `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL`, which routes outside the subscription entirely. Isolation and report-gate status of the lane are in [DISPATCH_RULES.md](docs/core/DISPATCH_RULES.md) §8. `VNX_OVERRIDE_CLAUDE_HEADLESS=1` re-lifts the block if it is ever re-enabled.

Per-worker git worktree isolation lives in `scripts/lib/tmux_worktree.py`, including teardown classification for clean, committed or pushed, and dirty worktrees. Isolation is unconditional on the provider and subprocess lanes; for the headless lane see [DISPATCH_RULES.md](docs/core/DISPATCH_RULES.md) §8.

### Governed memory: past, current, future

Memory is where most agent systems are weakest. Most bolt a vector store onto a stateless model and call it memory. I treat memory as a governed state machine with three tenses, each with its own store and its own audit guarantees.

The PAST is append-only NDJSON receipts: a forensic ledger of every dispatch, gate, and merge, with hash-chain verification tooling (`audit_chain`) available over it. The chain is opt-in per project (`VNX_CHAIN_RECEIPTS`) and off by default; the main ledger is unchained today, designed for epoch rotation once enabled ([ADR-029](docs/governance/decisions/ADR-029-hashchain-epoch-rotation.md)). The ledger itself is forensic, not lossy, and is in production now. Learning and dreaming read it; they change nothing without an operator approval.

The CURRENT is `runtime_coordination.db` (SQLite WAL): real-time orchestration state, leases, tracks, and dispatch status that any terminal can read for situational awareness. The `dispatches` table is ADR-007 tenant-scoped on a composite `UNIQUE(dispatch_id, project_id)`, rebuilt in place by a crash-safe migration (#859).

The FUTURE is the track layer and roadmap autopilot: planned features modeled as project-scoped tracks with a dependency graph, which the system can advance under human approval gates. The FUT-1 track schema, DAL, and CLI and the FUT-2 ADR-007 tenant-scoping have shipped, and the tracks layer is now activated for forward-state planning. The future-state reconciliation keeps it consistent automatically: an open-item → track bridge syncs `track_open_items` through the single-writer `tracks.py` primitives, then a reconciler derives each track's status. A track is `done` only when it has no unresolved blocking open-items, every dependency track is done, all of its dispatches are in terminal states, and any linked PR is confirmed merged. Both run inside the autopilot tick, which refuses to advance on a failed sync. The autopilot stays opt-in: it plans, but a human still approves the last step. A deliverable is promoted and closed by a human (`vnx deliverable promote` and `close`); when a track is declared done, the reconciler also completes its deliverable stubs (`deliverable_auto_completed`). A PR is linked to a track with `vnx objective link-pr` ([HORIZON_LIFECYCLE.md](docs/core/HORIZON_LIFECYCLE.md)).

A learning layer (`quality_intelligence.db`) consolidates the past into patterns and antipatterns that get injected into future dispatch context. Two parts feed it, and both only propose.

**Learning loop.** It mines the receipts for recurring failures and queues rule proposals. The queue fills only with `VNX_LEARNING_LOOP_QUEUE=1` (or persist mode, `VNX_LEARNING_LOOP_PERSIST=1`), both off by default, and an operator records the decision with `vnx learning decide-rule` ([ADR-065](docs/governance/decisions/ADR-065-leren-is-voorstellen.md), [SELF_LEARNING_LOOP.md](docs/intelligence/SELF_LEARNING_LOOP.md)). Every reader of `prevention_rules` keeps a loop rule out unless it has an active activation and the ledger verdict is fresh and `ok` or `pending`. In persist mode `ingest_approved_rules` activates only a rule whose operator decision is recorded for this project and whose proposal is unchanged since that decision, and `scripts/deactivate_pattern_activation.py` switches an activation off for good. Where a lesson belongs is fixed in [ADR-067](docs/governance/decisions/ADR-067-learning-capture-routing.md), and who owns a learned pattern in [ADR-068](docs/governance/decisions/ADR-068-learning-signaal-attributie.md). Whether injected intelligence changes outcomes is measured per arm by `scripts/intel_injection_join.py effect`, which refuses a verdict below 200 resolved dispatches per arm ([ADR-069](docs/governance/decisions/ADR-069-intelligence-effectmeting.md)). I have not measured an effect yet.

**Dreaming.** Auto-dream consolidates the store at night. It is opt-in: `vnx dream install-scheduler` installs the nightly job (launchd label `com.vnx.auto-dream`) and a watch job (`com.vnx.dream-watch`) that runs `scripts/dream_liveness_probe.py`. `vnx doctor` reports a missed or disabled night from the same reader ([ADR-066](docs/governance/decisions/ADR-066-dream-liveness-watch.md); the consolidation itself is [ADR-019](docs/governance/decisions/ADR-019-auto-dream-memory-consolidation.md), and it runs on DeepSeek unless `VNX_DREAM_PROVIDER` names `kimi`). It stays burning in until a run of measured nights says otherwise.

The point is not that the AI remembers. The point is that what it remembers is governed: every memory has a receipt, every plan has a gate, and a human owns the boundary.

## Architecture decisions

The decisions behind VNX are written down, not implied. The Architecture Decision Records under [docs/governance/decisions/](docs/governance/decisions/) cover the system end to end. The ones that shape it most:

- [ADR-005](docs/governance/decisions/ADR-005-ndjson-audit-ledger-primary.md): append-only NDJSON ledger as the primary observability surface
- [ADR-006](docs/governance/decisions/ADR-006-staging-promote-human-gate.md): staging then promote, with a mandatory human approval gate
- [ADR-008](docs/governance/decisions/ADR-008-dual-llm-adversarial-review.md): cross-vendor adversarial review (codex plus deepseek, amended 2026-11-11) bound by a contract hash
- [ADR-011](docs/governance/decisions/ADR-011-manager-worker-hierarchy.md): manager plus worker hierarchy with explicit depth, not depth-1 subagents
- [ADR-012](docs/governance/decisions/ADR-012-hybrid-interactive-headless.md): hybrid interactive and headless execution, no retire-interactive
- [ADR-014](docs/governance/decisions/ADR-014-autonomous-chain-dispatch.md): autonomous mode is pre-approved chain dispatch, never gate bypass
- [ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md): one structured plain-text skill prompt for every provider lane, no per-CLI mechanisms
- [ADR-030](docs/governance/decisions/ADR-030-plan-first-gate-enforcement.md): the plan-first gate enforced at the dispatch door and the merge gate, advisory-first: plan before work, structurally

For how the architecture got here, [docs/manifesto/EVOLUTION_TIMELINE.md](docs/manifesto/EVOLUTION_TIMELINE.md) reconstructs the technical evolution over roughly six months, including the private incubation provenance. The public repository is the extraction, hardening, and packaging of work that started inside a private product.

## Multi-provider architecture

VNX is not a thin "supports many models" wrapper. The provider layer is governed by [`provider_constraints.yaml`](scripts/lib/providers/provider_constraints.yaml), a machine-readable source of truth for constraints such as `kimi-via-cli-only`, `no-anthropic-sdk`, and `deepseek-harness-subscription-blocked`.

The default review stack is `codex_gate,deepseek_gate`. Seat 1 is a subscription reader (codex). Seat 2 is `deepseek_gate` on `deepseek-v4-pro`, which bills API credit, because the Kimi subscription was cancelled on 2026-11-11. The default takeover chain is `codex_gate,deepseek_gate,claude_gate`. `kimi_gate` and `glm_gate` stay registered gates but are in no default stack or chain. `vnx providers list` shows the state of each provider family. The operator can switch a family off with `vnx providers off`, and every launch function honors it ([PROVIDER_SWITCH.md](docs/core/PROVIDER_SWITCH.md)). Gemini is retired as a reviewer everywhere a review gate is selected; the gemini CLI still runs as a worker lane.

| Provider | How VNX drives it | Billing / constraint |
|---|---|---|
| **claude** | headless `claude -p` (only lane since the interactive tmux dispatch lane was removed on 2026-09-18, #1868) | subscription (own `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` routes outside) |
| **codex** | CLI subprocess | subscription · review gate + worker |
| **gemini** | CLI subprocess | provider sub/credits · worker (not a reviewer) |
| **kimi** | Kimi CLI over OAuth | subscription, while one exists · worker lane and explicit review gate, in no default review stack since 2026-11-11 · `kimi-via-cli-only`, no Moonshot SDK |
| **GLM** (Zhipu, default `glm-5.2`; `glm-5.3`/`glm-5.3-flash` admitted) | OpenRouter (`litellm:zai`) or the `glm-harness` proxy | API credit · registered gate, not in the default takeover chain · `zai-via-openrouter-only` |
| **any OpenRouter / OpenAI-compatible model** | claude-CLI harness or the local litellm proxy lane | API credit · routed generically via harness/proxy |
| **DeepSeek v4** | Claude harness + your own DeepSeek key, hardened | API credit (own key) · second seat of the default review stack · `deepseek-harness-subscription-blocked` |
| **ollama** (local, e.g. Gemma 4 E4B) | local runtime, no network | free/local · resolver + privacy-sensitive work |

**How far model- and harness-agnostic goes today.** The orchestrator role lives in one canonical file and `vnx role sync` carries it into `CLAUDE.md`, `AGENTS.md` and `GEMINI.md`, so Claude, Codex and Gemini start from the same role ([ADR-058](docs/governance/decisions/ADR-058-t0-playbook-source-en-verwijzing.md)). Workers run on whichever lane the dispatch names ([PROVIDER_LANES.md](docs/core/PROVIDER_LANES.md)), one `PermissionPolicy` governs every lane ([ADR-057](docs/governance/decisions/ADR-057-provider-agnostic-permission-policy.md)), and a review provider is one registry row. What I have not proven is a live T0 orchestrator session on a harness other than Claude Code. The T0 on this project runs on Claude.

No vendor SDK: VNX calls each CLI as a subprocess and never imports a provider library. One source-of-truth skill folder composes into a structured plain-text prompt (role, assignment, then an on-demand index of reference and script files) applied uniformly to every lane, so a single skill edit propagates to all providers with no per-CLI sync ([ADR-022](docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md)).

**The non-obvious lane: DeepSeek v4 through the Claude harness.** With my own DeepSeek key plus hardening (`ANTHROPIC_BASE_URL` redirect, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, telemetry and updater off, MCP off), DeepSeek v4 runs inside the Claude harness. Operator measurement on Claude Code 2.1.150 (2026-05-26) showed this beats a bare DeepSeek API call on coding and tool tasks (internal measurement, not a published benchmark): the harness adds tool-use loops, context injection, and structured diff output the raw API lacks.

### Billing follows auth, not the lane

VNX treats AI coding tools as interactive CLI workers, not SDK calls, and that choice raised a real billing question: does the headless lane cost more than the interactive one. I measured it instead of assuming. On 2026-08-11 the interactive tmux lane (removed since 2026-09-18) and the headless `claude -p` lane ran the same CLI under the same OAuth session, and both posted `cost=$0.0000` receipts, confirmed via auth state (no `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` in the environment, keychain `subscriptionType: max`). The billing boundary is the auth method, not which lane dispatched the work. Set your own `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` and that changes, since the lane then bills through that key instead of the subscription.

An earlier version of this README assumed a June 15, 2026 Anthropic billing change that would push headless usage to API credits by default. That change was never carried out. The headless lane was opened by direct operator decision (#1455) once subscription billing was confirmed by measurement, not by reading vendor announcements. This describes what this repository measures today; Anthropic's billing terms are the vendor's to change, and I have verified nothing beyond what a `cost=$0.0000` receipt tells me.

## Context rotation for any Claude Code session

`packages/claude-code-toolkit/context-rotation/` is a Claude Code plugin, separate from the VNX runtime and released in the public toolkit repository ([Vinix24/claude-code-toolkit](https://github.com/Vinix24/claude-code-toolkit)). Install it with `claude plugin marketplace add Vinix24/claude-code-toolkit`, then `claude plugin install context-rotation@claude-code-toolkit`. It measures how full the context is, asks Claude for a handover note at set points, checks that note, and gives it to the fresh session after `/clear`. Inside tmux it types the `/clear` itself. The bands are a share of `window_tokens`, which defaults to 200,000, or the absolute thresholds `warn_tokens`, `force_tokens` and `hard_tokens`; on a larger window set them in `.claude/context-rotation.json`. Install, settings and the hook layout are in the [plugin README](packages/claude-code-toolkit/context-rotation/README.md). The T0 rotation VNX enforces for its own orchestrator is a different mechanism: [T0_CONTEXT_ROTATION.md](docs/operations/T0_CONTEXT_ROTATION.md).

## Compared to dmux

The closest spiritual cousin is [dmux](https://github.com/standardagents/dmux), which also pairs tmux with per-pane git worktrees. My choices differ on ephemeral-per-dispatch workers instead of long-lived panes, NDJSON receipts instead of interactive merge as the main record, and a teardown classifier that preserves dirty or pushed work instead of treating cleanup as one state.

## Status

The released version is in [`VERSION`](VERSION), tagged with a `v` in front of it. `VERSION` is 1.8.0, released 2026-11-07. Work merged after it is not released: it sits in `changelog.d/` as fragments that the release PR folds into `## [Unreleased]` of [CHANGELOG.md](CHANGELOG.md), and this README describes it as on main, not yet released. Every release back to 0.1.0 is in the CHANGELOG. Open governance and release items are tracked in the tracks database (`vnx horizon`, alias `vnx objective`) and the open-items tooling under [scripts/open_items_manager.py](scripts/open_items_manager.py).

I built this for my own work. Use at your own discretion.

## Credits

Anthropic Claude Code is the foundation. I add receipts, provider routing, multi-CLI dispatch, and worktree isolation around it.
