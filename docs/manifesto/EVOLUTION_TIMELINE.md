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

**Representative implementation**
- `.claude/vnx-system/scripts/smart_tap_v7_json_translator.sh`

**Outcome**
- More deterministic dispatch ingestion and fewer accidental replays.

---

## Phase 3: Terminal State Reliability

**Problem**
- "Working vs idle" state could drift between sources.

**What changed**
- Consolidated status model around canonical state + reconciliation.
- Explicit active dispatch ownership in state transitions.

**Representative implementation**
- `.claude/vnx-system/scripts/sync_progress_state_from_receipts.py`
- `.claude/vnx-system/scripts/lib/terminal_state_reconciler.py`

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
- `.claude/vnx-system/scripts/generate_t0_brief.sh`
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

## Language Evolution: From Bash to Python

VNX started as a single tmux `send-keys` command — the simplest possible way to deliver a dispatch to another terminal pane. Tmux scripting is inherently bash, so the early automation (dispatch delivery, pane management, terminal status) was all shell scripts. As the system grew, more complex logic (receipt processing, quality advisory, state reconciliation, intelligence injection) was written in Python for structured data handling and testability.

The result is a ~60/40 bash/python ratio that reflects this organic growth:

- **Bash (~60%)**: tmux session management, dispatch delivery, file watchers, hook scripts, CLI entrypoint (`vnx`), context rotation pipeline
- **Python (~40%)**: receipt processing, quality advisory, terminal state reconciliation, cost reporting, intelligence generation, CI test suites

This split is a direct consequence of the system's origin — not an architectural choice. Bash was the right tool for the first layer (terminal orchestration), but as governance logic grew more complex, the lack of structured error handling, testability, and type safety in bash became a maintenance burden.

**Active migration**: Critical-path components are being progressively rewritten in Python to improve reliability, enable unit testing, and reduce the surface area for subtle shell bugs. The governance logic (quality advisory, receipt processing) already runs in Python; the remaining bash scripts handle tmux orchestration and hook wiring where shell is the natural interface.

---

## What Is Mature Today

- Receipt-led governance and audit trail
- Human-gated dispatch flow (staging/promote and confirmation path)
- Improved terminal-state consistency and recovery behavior
- Multi-model operation with a model-agnostic orchestration core
- Public packaging and CI hygiene suitable for external evaluation

---

## What This Timeline Intentionally Excludes

- Private product internals
- Full private incubation commit history
- Internal cleanup tracks not needed for public understanding

This document is intentionally concise: it explains the architecture's evolution without exposing private project context.

