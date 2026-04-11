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
- Public packaging and CI hygiene suitable for external evaluation

---

## What This Timeline Intentionally Excludes

- Private product internals
- Full private incubation commit history
- Internal cleanup tracks not needed for public understanding

This document is intentionally concise: it explains the architecture's evolution without exposing private project context.

