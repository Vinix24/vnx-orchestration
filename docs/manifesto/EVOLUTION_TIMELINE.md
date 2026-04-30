# VNX Evolution Timeline (Condensed)

**Status**: Public summary for readers  
**Scope**: Architectural evolution over ~6 months  
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
- `scripts/smart_tap_v7_json_translator.sh` — superseded by pending/ dispatch flow

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
- `.claude/vnx-system/scripts/receipt_processor_v4.sh`

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
- `.claude/vnx-system/scripts/dispatcher_v8_minimal.sh`
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

## Language Evolution: Why ~60% Bash / ~40% Python

VNX started as tmux `send-keys` scripts — the most direct way to control terminal panes programmatically. This means the codebase grew organically from bash, not as a planned language choice.

**Why bash persists:**
- Tmux orchestration (`send-keys`, pane management, session control) is inherently shell-native.
- File-bus operations (watch, move, append) are one-liners in bash but verbose in Python.
- Supervisor, dispatcher, and smart-tap were written first and work reliably.

**Why Python is growing:**
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
- `scripts/lib/haiku_classifier.py`
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
- Code pre-filter unconditionally blocks `COMPLETE` when any lock file exists.
- LLM never receives gate state — it cannot argue, reason around, or override a lock.
- Domain-agnostic: same mechanism works for `codex_review`, `gemini_review`, CI green, and future business compliance gates.

**Benchmark harness**
- Context assembler builds ~5K token snapshots from 8 state files.
- Replay harness executes scenarios at three complexity levels.
- Benchmark scores: Level-1 100%, Level-2 73–87%, Level-3 67–78%.
- Fixture-mode tagging separates benchmark runs from production execution.

**Representative implementations**
- `scripts/lib/t0_decision_framework.py`
- `scripts/lib/t0_gate_locks.py`
- `scripts/lib/t0_context_assembler.py`
- `scripts/benchmark/t0_replay_harness.py`

**Outcome**
- T0 can make correct decisions autonomously in the majority of cases.
- Gates are enforced deterministically — human override is possible, but agent bypass is not.
- Benchmark baseline established for future headless T0 production cutover.

---

## Phase 11: Governance Hardening Chain and Intelligence Closure (v0.10.0)

**Problem**
- Headless audit trail still had gaps (~40% parity): tokens uncounted, STUCK events not archived, no cryptographic reproducibility.
- Supervisor daemons (dispatcher, receipt processor) would die silently under load — no auto-respawn.
- The intelligence loop was open-circuit: pattern confidence stores for the selector and learner diverged independently, and `dispatch_id` was never stamped at injection time, so failure decay never propagated.
- Frontend regressions were caught manually — no automated visual baseline.
- Codex gate severity was too noisy (100% blocking rate on chain regate).

**What changed (27 PRs, 2026-04-28 → 2026-04-30)**

**Headless audit parity (40% → 90%)**
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
- Headless audit trail approaches interactive parity.
- Daemons self-heal without operator intervention.
- Intelligence loop is closed: patterns can actually learn from failures.
- Codex gate noise reduced ~75%, making gate enforcement sustainable at scale.

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
Codex and Gemini review gates moved from "operator reads the review output and decides" to headless `codex exec` / `gemini` subprocesses running autonomously. Triple-gate enforcement (codex pass + gemini pass + CI green = merge) became machine-enforced rather than human-remembered.

**The experience**
The operator's role shifted from decision-maker to exception handler. Gates ran without prompting; the operator only intervened on failures. A chain of 10 PRs could land while the operator was doing other work.

**The cost**
Gate false positives became a real problem. When codex flagged everything as `error`-severity, the operator had to manually override or retune prompts. The #323/#324 severity tightening in v0.10.0 was a direct response to this: blocking rates dropped ~75%.

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

### Phase F — Cryptographic Audit Chain (v0.10.0)

**What changed**
`instruction_sha256` stamps the dispatch instruction hash in the manifest and receipt. Token usage (codex, gemini) is tracked per gate invocation. The audit trail is now cryptographically complete: every dispatch can be reproduced, every gate invocation costed, every worker event counted.

**The experience**
The operator can answer questions that previously required guesswork: "Did this receipt come from this exact instruction?" and "How many tokens did the codex gate consume on this chain?". The audit trail transitioned from "good enough for debugging" to "good enough for billing and compliance audit".

---

## What Is Mature Today

- Receipt-led governance and append-only audit trail
- Human-gated dispatch flow (staging/promote and confirmation path)
- Headless worker execution via SubprocessAdapter (T1/T2 fully headless)
- Auto-report pipeline: stop hook → extraction → haiku classification → markdown
- Unified T0 state builder: 0.2s startup, single source of truth
- Gate locks: deterministic, LLM-bypass-proof governance enforcement
- T0 decision framework benchmarked at 73–100% accuracy by scenario tier
- Dashboard with unified domain visibility and reports browser
- Multi-model operation with model-agnostic orchestration core
- Supervisor pack: daemons auto-respawn, state auto-rotates nightly
- Cryptographic audit trail: instruction_sha256, token tracking, canonical gate schema
- Headless audit parity at ~90% (was 40% at v0.9.0)
- Intelligence loop closed: selector-learner reconciled, T0 decision log active
- Public packaging and CI hygiene suitable for external evaluation

---

## What This Timeline Intentionally Excludes

- Private product internals
- Full private incubation commit history
- Internal cleanup tracks not needed for public understanding

This document is intentionally concise: it explains the architecture's evolution without exposing private project context.

