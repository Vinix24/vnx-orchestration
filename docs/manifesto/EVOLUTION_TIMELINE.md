# VNX Evolution Timeline (Condensed)

**Status**: Public summary for readers  
**Scope**: Architectural evolution through 2026-10-18  
**Audience**: People evaluating VNX concept and build quality

---

## Provenance Note

VNX was incubated inside a private product repository before becoming its own repository.

- Early evolution (first months): private, not publicly replayable commit-by-commit.
- Public repository: extraction, hardening, packaging, and operational stabilization.

This timeline is a concise reconstruction of the technical evolution, without private product details.

---

## Phase 1: Basic Multi-Terminal Dispatch

**Start point**
- Simple dispatch delivery from orchestrator to worker terminals in tmux.

**Main limitation discovered**
- Fast, but fragile under repeated/manual operations.

**Architecture direction**
- Keep direct, file-based orchestration flow.
- Add explicit control points instead of implicit chat-state assumptions.

---

## Phase 2: Duplicate Dispatch Prevention

**Problem**
- Duplicate or repeated block processing under noisy terminal output.

**What changed**
- Canonical block hashing and dedup tracking.
- Validation before queueing.

**Representative implementation** (removed in housekeeping/system-cleanup)
- `scripts/smart_tap_json_translator.sh` — superseded by pending/ dispatch flow

**Outcome**
- More deterministic dispatch ingestion and fewer accidental replays.

---

## Phase 3: Terminal State Reliability

**Problem**
- "Working vs idle" state could drift between sources.

**What changed**
- Consolidated status model around canonical state + reconciliation.
- Explicit active dispatch ownership in state transitions.

**Representative implementation** (removed in housekeeping/system-cleanup)
- `scripts/sync_progress_state_from_receipts.py` — replaced by runtime_core state machine
- `scripts/lib/terminal_state_reconciler.py` — replaced by runtime_reconciler.py

**Outcome**
- Dashboard and orchestration decisions align better with actual runtime behavior.

---

## Phase 4: Receipt-First Governance

**Problem**
- Chat transcripts are hard to audit and hard to replay.

**What changed**
- Append-only receipt path became canonical.
- Completion handling enriched with structured metadata and quality context.

**Representative implementation**
- `.claude/vnx-system/scripts/append_receipt.py`
- `.claude/vnx-system/scripts/receipt_processor.sh`

**Outcome**
- Better auditability and deterministic post-task processing.

---

## Phase 5: Intelligence Injection and Decision Support

**Problem**
- Orchestrator needed consistent context, not ad-hoc manual memory.

**What changed**
- Context/intelligence injection into dispatch flow.
- Open-items and advisory signals surfaced to T0.

**Representative implementation**
- `.claude/vnx-system/scripts/dispatcher_minimal.sh`
- `scripts/generate_t0_brief.sh` (removed — replaced by build_t0_state.py)
- `.claude/vnx-system/scripts/lib/quality_advisory.py`

**Outcome**
- T0 decisions moved from intuition-only to signal-assisted orchestration.

---

## Phase 6: Model-Agnostic Orchestration

**Problem**
- Different CLIs/providers have different capabilities and ergonomics.

**What changed**
- Provider-aware dispatch and terminal launch behavior.
- Watcher/receipt approach retained as core portability layer.

**Representative implementation**
- `.claude/vnx-system/bin/vnx`
- `.claude/vnx-system/scripts/setup_multi_model_skills.sh`

**Outcome**
- Practical cross-provider operation without making hooks a hard dependency.

---

## Phase 7: Hardening and Packaging

**Problem**
- Needed to be shareable and verifiable by others.

**What changed**
- Distribution install flow, doctor/smoke checks, CI guards, path hygiene.
- Runtime/deployment boundaries made explicit.

**Representative implementation**
- `.claude/vnx-system/install.sh`
- `.claude/vnx-system/scripts/vnx_doctor.sh`
- `.claude/vnx-system/scripts/vnx_package_check.sh`
- `.claude/vnx-system/.github/workflows/public-ci.yml`

**Outcome**
- Cleaner public baseline with reproducible checks and lower operational drift.

---

## Language Evolution: Why Python Now Dominates

VNX started as tmux `send-keys` scripts — the most direct way to control terminal panes programmatically. This means the codebase grew organically from bash, not as a planned language choice. It has since shifted heavily toward Python: a line count under `scripts/` (`.py`) against `scripts/`+`bin/` (`.sh`) puts the split at roughly 92%/8% Python/Bash (line count of tracked `scripts/**/*.py` against `scripts/**/*.sh` plus `bin/*`, measured 2026-10-18), not the ~60/40 Bash/Python this section originally claimed.

**Why bash persists in its remaining 8%:**
- Tmux orchestration (`send-keys`, pane management, session control) is inherently shell-native.
- File-bus operations (watch, move, append) are one-liners in bash but verbose in Python.
- Supervisor, dispatcher, and smart-tap were written first and work reliably.

**Why Python dominates:**
- Intelligence pipeline (FTS5 queries, pattern scoring, learning loop) needs structured data handling.
- Receipt processing moved from bash to Python for JSON parsing reliability.
- CI testing is pytest-based — Python scripts are directly testable, bash scripts require wrapper tests.
- New features are written in Python by default.

**Active migration policy:**
- New components: Python.
- Existing bash scripts: migrated when they need significant changes (not rewritten for its own sake).
- Target: critical-path components in Python, shell glue for tmux/filesystem operations.

Current ratio reflects origin, not preference. The system is moving toward Python for anything that benefits from testability, type safety, and structured error handling.

---

## Phase 8: Headless Worker Pipeline (F31–F35, March 2026)

**Problem**
- tmux `send-keys` delivery is fragile: no output capture, no stream, no automated retry.
- Workers required an interactive terminal session; automated batch execution was impossible.

**What changed**
- `SubprocessAdapter` spawns `claude -p --output-format stream-json` as a child process.
- Full stream capture via `EventStore` — every token, event, and error archived to disk.
- `HeartbeatMonitor` detects silence/hang and triggers auto-restart.
- Skills became pure prompt files (`SKILL.md`). No tool wiring, no code scaffolding.
- F35 certified the end-to-end headless pipeline: dispatch → execution → receipt → close.

**Key insight**
Skills are just prompts. Worker skills and manager skills use the identical mechanism — domain behavior comes from skill content, not special code paths.

**Representative implementations**
- `scripts/lib/subprocess_adapter.py`
- `scripts/lib/subprocess_dispatch.py`
- `scripts/lib/event_store.py` (removed — functionality merged into subprocess_adapter)
- `scripts/lib/heartbeat_monitor.py`

**Outcome**
- Workers can run in fully headless mode, with or without tmux.
- Stream output is capturable, archivable, and replayable.
- `VNX_ADAPTER_T1=subprocess` / `VNX_ADAPTER_T2=subprocess` feature flags enable per-terminal routing.

---

## Phase 9: Auto-Report Pipeline and State Unification (F37–F38, PRs #196–203, March–April 2026)

**Problem**
- Workers manually assembled reports from memory — inconsistent, expensive, error-prone.
- T0 startup required 8+ separate scripts to reconstruct session state.
- Heartbeat monitor falsely flagged subprocess-adapter terminals as ghost processes.

**What changed (F37: Auto-Report Pipeline)**
- Stop hook fires on session end and triggers extraction → classification → markdown assembly.
- Deterministic extraction: git diff, commit hash, pytest output parsed without LLM.
- Haiku classifier assigns content type, quality score, and risk level.
- `VNX_AUTO_REPORT=1` enables the pipeline; off by default to preserve backward compatibility.

**What changed (PR #200: Unified State Builder)**
- `SessionStart` hook builds `t0_state.json` in 0.2 seconds.
- Replaces 8+ individual startup scripts with a single Python module.
- State snapshot includes: open dispatches, active terminals, open items, recent receipts.

**What changed (PR #202: Daemon Compatibility)**
- Heartbeat monitor detects subprocess-adapter terminals and skips ghost-detection logic.
- Prevents false "terminal down" alerts for headless workers with infrequent stdout.

**What changed (F38: Dashboard Unified)**
- Single dashboard for coding and business domains.
- Domain filter tabs, session history browser, agent selector by name.
- Reports browser surfaces auto-assembled reports directly in UI.

**Representative implementations**
- `hooks/stop_hook.py`
- `scripts/lib/report_assembler.py`
- `scripts/lib/classifier_providers/haiku_provider.py`
- `scripts/lib/t0_state_builder.py`

**Outcome**
- T0 session startup is deterministic, fast, and single-source.
- Workers produce structured reports without manual effort.
- Dashboard gives unified visibility across all agent lanes.

---

## Phase 10: Headless T0 Benchmark and Governance Hardening (F39, April 2026)

**Problem**
- T0 orchestration was interactive-only: high quality, but required constant human attention.
- Review gates were enforced by LLM judgment — which could be argued or bypassed by prompt context.
- No benchmark existed to measure autonomous T0 decision quality against interactive T0.

**What changed**

**Decision framework rewrite**
- Taxonomy simplified: `ACCEPT`/`IGNORE` eliminated → only `DISPATCH`, `COMPLETE`, `WAIT`, `REJECT`, `ESCALATE`.
- Hybrid architecture: deterministic code pre-filter handles ~70% of decisions without LLM invocation.
- Pre-filter checks (ordered): gate locks → queue empty → no receipts → stale context → ambiguous receipt.
- Remaining 30% routed to LLM with 5 structured rules and constrained output format.

**Gate locks**
- File-based locks at `.vnx-data/state/gate_locks/<gate-id>.lock`.
- Designed so that a pre-filter blocks `COMPLETE` while any lock file exists and the LLM never receives gate state. **Correction (2026-10-18): this was not wired into a running caller.** `scripts/f39/gate_locks.py` has no caller outside the benchmark; the deterministic block on completion is the merge door (Phase 15 below).
- Domain-agnostic by design: the same mechanism would work for review gates, CI green and future business compliance gates.

**Benchmark harness**
- Context assembler builds ~5K token snapshots from 8 state files.
- Replay harness executes scenarios at three complexity levels.
- Replay scenarios at three complexity levels; the scores of April 2026 are not restated because they have not been re-measured on `main`.
- Fixture-mode tagging separates benchmark runs from production execution.

**Representative components**
- T0 decision pre-filter + taxonomy (the deterministic pre-LLM filter)
- Gate locks: `scripts/f39/gate_locks.py` (benchmark harness, no production caller)
- Context assembler: `scripts/lib/context_assembler.py`
- F39 replay harness: `scripts/f39/replay_harness/`

**Outcome**
- A benchmark baseline exists for a future headless T0 cutover, which has not happened.
- The decision taxonomy stayed; gates are enforced deterministically at the merge door, not through lock files.

---

## Phase 11: Governance Hardening Chain and Intelligence Closure (v0.10.0)

**Problem**
- Headless audit trail still had gaps in parity with the interactive path: tokens uncounted, STUCK events not archived, no cryptographic reproducibility.
- Supervisor daemons (dispatcher, receipt processor) would die silently under load — no auto-respawn.
- The intelligence loop was open-circuit: pattern confidence stores for the selector and learner diverged independently, and `dispatch_id` was never stamped at injection time, so failure decay never propagated.
- Frontend regressions were caught manually — no automated visual baseline.
- Codex gate severity was too noisy (100% blocking rate on chain regate).

**What changed (2026-04-28 → 2026-04-30)**

**Headless audit parity**
- `instruction_sha256` stamped in manifest + receipt: dispatches are cryptographically reproducible.
- `WorkerHealthMonitor` STUCK events archived to `EventStore` + `stuck_event_count` in receipt.
- Cross-provider token tracking (`codex_adapter`, `gemini_adapter`) via `adapter.get_token_usage()`.
- Canonical gate result schema: `gate_status.is_pass()` replaces ad-hoc string comparisons.

**Supervisor pack**
- `cleanup_worker_exit`: single-owner exit cleanup — lease release + state transition + audit event, idempotent.
- `receipt_processor_supervisor.sh`: auto-respawn with exponential backoff, stale-lock cleanup, SIGTERM/KILL escalation.
- `lease_sweep` and `runtime_supervise` ticking in dispatcher prelude (30s/60s intervals).
- `docs/operations/UNIFIED_SUPERVISOR.md`: operator guide for opt-in per-project cutover.

**State self-maintenance**
- `compact_state.py` + nightly cron: auto-rotate intelligence_archive (7d), receipts (cap 10k), open_items_digest (>30d evict).

**P0 intelligence loop fixes**
- Reconcile pattern confidence stores: closes open-circuit between `intelligence_selector` and `learning_loop`.
- Stamp `dispatch_id` at injection time: unblocks failure decay propagation.
- Activate T0 decision log + outcome reconciliation: T0 introspection wired end-to-end for the first time.

**Representative implementations**
- `scripts/compact_state.py`, `scripts/install_nightly_crons.sh`
- `scripts/lib/cleanup_worker_exit.py`
- `scripts/receipt_processor_supervisor.sh`
- `scripts/lib/gate_status.py`
- `dashboard/api_register_stream.py`

**Outcome**
- The headless audit trail moved toward interactive parity.
- Daemons self-heal without operator intervention.
- Intelligence loop is closed: patterns can actually learn from failures.
- Codex gate severity was tightened, which made gate enforcement sustainable at scale.

---

## Phase 12: The Road to 1.0 (Waves 5–8 → 1.0.0, May–June 2026)

**Problem**
- v0.10.0 was a single-operator, single-project system. Running it for more than one project meant the central state could cross-contaminate (`project_id` was not enforced end-to-end).
- Dispatch routing had grown several side doors (per-lane scripts called directly), so the audit trail of *how* a dispatch was routed was inconsistent.
- Install was checkout-only with no packaged surface.

**What changed**
- **Multi-provider control centre (Waves 5–7)** — the elastic worker pool (`bin/vnx pool`, ADR-018), five provider lanes (Claude subscription tmux + subprocess burst, Codex CLI, Gemini CLI, Kimi CLI OAuth, LiteLLM bridge for DeepSeek/GLM), universal cost tracking, and the provider/routing registries (`scripts/lib/providers/`).
- **Multitenancy (ADR-007)** — composite `UNIQUE(dispatch_id, project_id)` and fail-closed `project_id` resolution across the central store, so one machine can govern many projects without bleed. The `dispatches` table rebuild + the future-state reconciliation batch (track ↔ dispatch ↔ open-item) followed.
- **The single-entry door (ADR-024)** — every dispatch funnels through one entry point (`scripts/lib/dispatch_cli.py`): validate → snapshot → compile_plan → permit → execute. Flipped default-ON 2026-06-24, with a phantom-guard that rejects evidence-free GATE-GREEN receipts and GLM normalized to the harness lane at the door. Rollback via `VNX_DISPATCH_LEGACY=1`.
- **Packaging** — the wheel builds from the tree (`pip install -e .`, `vnx` console script); the human-gated PyPI publish followed on 2026-07-02.
- **Mission Control central-store cutover (2026-06-23)** and the receipt PULL-model groundwork.
- **Pre-ship hardening sprint (2026-06-26)** — five real production bugs (exit-classifier audit-trail, schema-SSOT, report-findings self-heal, doctor partial-setup, code-anchor payload eviction), the future-state git-grounded reconcile, the self-learning proposal-tier revival, and a stale-test + docs sweep. See `CHANGELOG.md`.

**Outcome**
- `VERSION` is `1.0.0`, published to PyPI on 2026-07-02 (`pip install vnx-orchestration`), tagged `v1.0.0` with a GitHub release.
- The audit trail now records routing through one door; multitenancy is enforced fail-closed; the self-learning proposal tier produces operator-gated proposals from the real receipt stream.

---

## Phase 13: Outcomes Instead of End States (2026-09-28 to 2026-10-04)

**Problem**
- A dispatch could end in a silent state (killed worker, abandoned bundle, a receipt without a verdict) and drop off every list. The byte cursor that handed a receipt to one reader took it away from every other reader.

**What changed**
- One outcome per dispatch, with its own identity rule ([ADR-038](../governance/decisions/ADR-038-receipt-outcome-identity.md)): `receipt_outcome` module (#1980), digest counting one outcome per dispatch (#1990), three readers on it (#1992).
- Open outcomes replace the byte cursor (#2008). A dispatch is finished only with an outcome, accept or reject; no end state without one (#2010). Abandoned bundles and failed worker exits stay open (#2018).
- `receipt_query.py open-outcomes --recount` shows a fresh, read-only reading of the test evidence (#2038). Verification counts the last green run, not the first red one (#2000, #2012). The report parser reads `## Verification` (#1975). The dispatch role reaches the receipt (#2028).

**Outcome**
- A T0 cannot lose a dispatch: it stays on the open list until a decision is written to the decision log. Mechanism: `docs/core/DISPATCH_RULES.md` §13, fields in `docs/core/11_RECEIPT_FORMAT.md`. Released: #1980 to #2010, #2018 and #2028. On main, not yet released: `--recount` (#2038).

---

## Phase 14: The T0 View of the Fabric (2026-09-28 to 2026-10-07)

**What changed**
- Live work in the T0 state comes from the dispatches table and the occupancy lock (#1984). Dead and bloated sections left the state file (#1999). The open-PR row carries head, conflict state and CI on the head (#2041).
- Gate bundles leave `dispatches/pending/` (#1979). Health checks expect only headless-era daemons (#1983). `parent_dispatch` is derived from `work_ref` on stage (#1962).
- The reconciler counts only hard dependency edges as blocking (#1981) and no longer completes a stub from track-level evidence (#2048, on main, not yet released).
- The T0 folder works for a T0: the role fallback reads the playbook (#2037). The rotation spawner starts the successor in that folder (#2042).
- The register of every lock between a dispatch and a merge, and every way each opens (#2039): `docs/core/LOCKS_AND_RELEASES.md`.

---

## Phase 15: Review Seats and the Merge Door (2026-09-28 to 2026-10-18)

**Problem**
- A review gate could pass on an older head, a reviewer outage could leave a seat silently empty, and two evaluators could read the same PR differently.

**What changed**
- Gates bind to the PR head: the gate-required check (#1956), no verdict about an older head (#2019), verified results bound to the head (#2021), a review-gate check requires a PASS by the shared rule (#2016). REVISE counts as fail (#2025). Reviewer prompts carry no intelligence or repo map (#1964).
- The takeover chain `codex_gate,kimi_gate,deepseek_gate`, with deepseek signing at the merge door (#2040). A seat resolver and one reviewer per seat (#2073, R5a). Every evaluator counts seats one way and blocking always blocks (#2074, #2084, R5b, [ADR-037](../governance/decisions/ADR-037-gate-uitval-is-afwezigheid-niet-tegenstem.md)). `glm_gate` left the default chain (#2100).
- Harness reviewer seats keep their final answer in parallel runs (#2081) and a weekly-limit refusal counts as an exhausted lane (#2077).
- A raw-file dispatch is refused under the door default ([ADR-025](../governance/decisions/ADR-025-raw-file-dispatch-deprecation.md), #1954, released). The harness lanes for glm and deepseek run on a clean `CLAUDE_CONFIG_DIR` (#2026, released). Every headless builder run gets its own event stream (#2086).
- The plan-gate runs one seat, a second seat on BLOCK, and counts rounds per plan version (#2104).

**Outcome**
- The merge door (`scripts/pr_merge.py`) runs five preflights and writes a `pr_merged` receipt that records who filled which seat. The seat rule, the takeover chain and the one-seat plan-gate are on main, not yet released. Summary: `docs/manifesto/GOVERNANCE_ARCHITECTURE.md` §10.

---

## Phase 16: Provider Switch and Content Boundary (2026-09-28 to 2026-10-15)

**What changed**
- `vnx providers`: the switch core, the CLI and a coverage guard (#2055). Review gates honor it (#2058). The intelligence tagger, scout, classifier and analyzer honor it, and the dream consolidation also honors the content boundary (#2057).
- Deep analysis is routed by content, and the session origin is labelled (#2032). The codex model comes from the registry in every launch path (#2059). Sonnet 5.5 entered the registry, and receipts record the model that actually ran (#1973).
- The learning loop: natural key on pattern tables (#1965), the nightly analyzer fails on an error instead of logging complete (#1966), a nightly shadow phase with a beacon (#1968), the analyzer on deepseek-flash with a balance pre-flight (#1969), open items as a second signal (#1972), project scoping of failure patterns (#2023).

**Outcome**
- The switch exists and does not enforce yet: its write verbs refuse until every launch function calls `provider_switch.require` (`vnx providers list`). The content boundary is read by the conversation analyzer and the dream consolidation only (OI-2172). `docs/core/PROVIDER_LANES.md`, "Provider switch".
- The learning-loop, registry and content-routing work (#1965, #1966, #1968, #1969, #1972, #1973, #2023, #2032) shipped in v1.7.0/v1.7.1; the switch and the codex registry launch (#2055, #2057, #2058, #2059) are on main, not yet released.

---

## Phase 17: T0 Guard, Rotation and the Context-Rotation Plugin (2026-10-15 to 2026-10-18)

**What changed**
- The T0 guard: a hook shim that cannot wedge a session (#2075, #2079) and `vnx guard install` and `vnx guard check` (#2078). The rotation spawner refuses a rotation without wrap proof and has an emergency path that leaves an audit line (#2076).
- The context-rotation plugin in `packages/claude-code-toolkit/context-rotation/`: skeleton, hook boundary and warn band (#2064), state, detector, injection and the rotate skill (#2080), force and hard bands (#2090), the automatic `/clear` in tmux (#2093), documentation and install test (#2095), with fix-forwards (#2091, #2092, #2094, #2096).

**Outcome**
- A long session rotates on a handover note that is checked, not on auto-compaction. On main, not yet released.

---

## Phase 18: CI Cost and Release Mechanics (2026-10-03 to 2026-10-18)

**What changed**
- A function-size ratchet in CI (#2036). One profile prelude, a collection manifest and an xdist shadow job that never fails a check (#2082), the parity check (#2087) and a cutover history that counts only verdicts a failed parity step cannot leave clean (#2105). Worker-safe fixes in the tests and the subprocess adapter that the parallel run exposed (#2101, #2102, #2103).
- One CHANGELOG fragment per PR under `changelog.d/`, folded into `CHANGELOG.md` by the release PR (#2085, #2097, #2099).
- Three point releases were cut across the period boundary: v1.6.7 (#1960) on 28 September, v1.7.0 (#2011) on 30 September and v1.7.1 (#2035) on 3 October. The project `CLAUDE.md` carries four build rules (#2031).
- The architecture documents under `docs/core/` were brought up to main (#2108, #2109).

---

## The Operator's Journey: From 4 tmux Panes to Headless Orchestrator

This section traces how the *experience of running VNX* changed for the human operator — independent of the technical phases above, which describe what was built. These phases describe what it felt like.

### Phase A — Manual Queue + 4 tmux Panes (Early)

**What the operator did**
Four tmux panes open simultaneously: T0 orchestrator, T1 primary worker, T2 testing, T3 review. The operator read each dispatch markdown file manually, evaluated it, and typed an approval command to promote it from `pending/` to active. Each step required a deliberate human action — there was no autonomous advancement.

**The experience**
High visibility. Every decision was explicit and operator-driven. The system had a "ChatGPT-as-orchestrator" feel: T0 was essentially a well-prompted interactive session, and the operator was the state machine connecting the pieces. Mistakes were caught early because the operator was in the loop at every step.

**The cost**
Exhausting for any chain longer than 3-4 PRs. The operator had to stay focused across all four panes, approve each dispatch, watch for completions, and manually trigger the next step. Context switching between panes was constant.

### Phase B — Headless Workers (T1, T2)

**What changed**
`SubprocessAdapter` (F32, PR #189) made T1 and T2 invisible: `claude -p --output-format stream-json` spawned as child processes. Workers no longer needed visible terminal panes. The operator still ran T0 interactively, but T1/T2 ran silently in the background, writing to a per-terminal NDJSON ring buffer.

**The experience**
The cognitive load dropped significantly. The operator no longer needed to watch T1/T2 panes — they just waited for receipts. The dashboard became the primary interface for monitoring worker progress instead of eyeballing tmux output.

**The cost**
Debugging got harder. When a headless worker stalled or failed, the operator had to read event archives (`events/archive/{terminal}/{dispatch_id}.ndjson`) instead of the live pane. The "silent failure" surface area increased.

### Phase C — Headless Review Gates

**What changed**
Codex and Gemini review gates moved from "operator reads the review output and decides" to headless `codex exec` / `gemini` subprocesses running autonomously. Triple-gate enforcement (codex pass + gemini pass + CI green = merge) became machine-enforced rather than human-remembered. Gemini has since left the review stack; the current merge door is Phase 15.

**The experience**
The operator's role shifted from decision-maker to exception handler. Gates ran without prompting; the operator only intervened on failures. A chain of 10 PRs could land while the operator was doing other work.

**The cost**
Gate false positives became a real problem. When codex flagged everything as `error`-severity, the operator had to manually override or retune prompts. The #323/#324 severity tightening in v0.10.0 was a direct response to this: blocking rates dropped.

### Phase D — Self-Improvement Loop Attempt

**What changed**
`learning_loop.py`, `intelligence_selector`, and a success-patterns SQLite DB were introduced. The idea: patterns from past dispatches would inject context into future ones. Confidence scores would rise for patterns that correlated with successful outcomes.

**The experience**
Theoretically transformative; practically inert. The loop was open-circuit until the v0.10.0 P0 fixes: the selector and learner maintained separate confidence stores that never reconciled, and `dispatch_id` was never stamped at injection time, so failure decay never propagated. From the operator's perspective, the intelligence panel showed data, but dispatch quality didn't observably improve.

**Note**: Per `claudedocs/2026-04-30-self-learning-loop-audit.md`, the loop was open-circuit until PRs #326–#328 in v0.10.0 addressed the root causes. Whether the closed loop produces measurable quality improvement remains to be evaluated.

### Phase E — Supervisor + State Self-Maintenance (v0.10.0)

**What changed**
Daemons became self-healing. `receipt_processor_supervisor.sh` auto-respawns the receipt processor on crash. `dispatcher_supervisor.sh` does the same for the dispatcher. `lease_sweep` runs every 30s to release stale leases before they block the queue. `compact_state.py` runs nightly to prevent state directories from growing unbounded.

**The experience**
The operator no longer needs to check "is the receipt processor still running?" at the start of each session. The system maintains itself. Long-running chains (20+ PRs over multiple days) became operationally viable without babysitting.

**The cost**
More moving parts. Diagnosing *why* a supervisor restarted requires reading the supervisor log, not just the dispatcher log. The operational surface area is wider even though the operator burden is lower.

### Phase F: Reproducible Audit Evidence (v0.10.0)

**What changed**
`instruction_sha256` stamps the dispatch instruction hash in the manifest and receipt. Token usage (codex, gemini) is tracked per gate invocation. Every dispatch can be reproduced from its recorded instruction, every gate invocation costed, every worker event counted.

**The experience**
The operator can answer questions that previously required guesswork: "Did this receipt come from this exact instruction?" and "How many tokens did the codex gate consume on this chain?". The audit trail moved from "good enough for debugging" to evidence an auditor can query. That is not tamper-evidence: the hash chain over the ledger is available per project and off by default.

---

## What Is Mature Today

- Receipt-led governance and append-only audit trail
- Human-gated dispatch flow (staging/promote and confirmation path)
- Headless worker execution via SubprocessAdapter (T1/T2 fully headless)
- Auto-report pipeline: stop hook → extraction → haiku classification → markdown
- Unified T0 state builder: 0.2s startup, single source of truth
- Merge-time fail-closed gates (`scripts/pr_merge.py`): CI, review gate with one reviewer per seat, ADR numbering, contract validity and branch protection all run in code before a merge is allowed (the file-lock-based gate design in `scripts/f39/gate_locks.py` remains unwired, see `GOVERNANCE_ARCHITECTURE.md` §3 and §10)
- Open outcomes: no dispatch ends without an accepted or rejected outcome
- T0 decision framework with a replayable benchmark harness
- Dashboard with unified domain visibility and reports browser
- Multi-model operation with model-agnostic orchestration core
- Supervisor pack: daemons auto-respawn, state auto-rotates nightly
- Audit evidence per dispatch: `instruction_sha256`, token tracking, canonical gate schema. The hash chain over the ledger is available per project and off by default
- Headless audit trail close to interactive parity
- Intelligence loop closed: selector-learner reconciled, T0 decision log active; the self-learning proposal tier produces operator-gated proposals from the receipt stream
- Multitenancy enforced fail-closed (ADR-007 composite `project_id` keys) — one machine governs many projects without bleed
- Single-entry dispatch door default-ON (ADR-024): every dispatch funnels through one validated, permitted, phantom-guarded entry point
- Elastic worker pool (`bin/vnx pool`, ADR-018) with queue- and cost-aware scaling and per-worker worktree isolation
- Public packaging and CI hygiene suitable for external evaluation

---

## What This Timeline Intentionally Excludes

- Private product internals
- Full private incubation commit history
- Internal cleanup tracks not needed for public understanding

This document is intentionally concise: it explains the architecture's evolution without exposing private project context.

