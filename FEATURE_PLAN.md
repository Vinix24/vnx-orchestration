# VNX Feature Plan

**Last Updated**: 2026-04-10
**Status**: Active
**Completed**: F1-F39 (see CHANGELOG.md)

---

## Next Features

### F37: Auto-Report Pipeline
**Priority**: P1 | **Status**: Completed (March 2026, PR #196, #197, #201)

Stop hook + deterministic extraction + haiku classification → auto-assembled markdown report. Workers no longer manually assemble reports.

- Stop hook with provider-agnostic execution (`hooks/stop_hook.py`)
- Deterministic extraction: git diff, commit hash, pytest output parsing
- Haiku classification: content type, quality score, risk level
- `VNX_AUTO_REPORT=1` feature flag (off by default)
- Auto-assembled report replaces manual unified reports

### F38: Dashboard Unified
**Priority**: P1 | **Status**: Completed (April 2026, PR #203)

Single dashboard for coding and business domains.

- Domain filter tabs (Coding, Business, All)
- Session history browser (view past dispatch outputs)
- Agent selector by name instead of terminal ID
- Reports browser surfaces auto-assembled reports in UI

### F39: Headless T0 Benchmark
**Priority**: P2 | **Status**: Completed (April 2026, PR-9 in progress)

Decision framework rewrite + gate locks + replay harness. Deterministic pre-filter handles ~70% of decisions. Taxonomy simplified to DISPATCH/COMPLETE/WAIT/REJECT/ESCALATE.

- Context assembler: 8 state files → ~5K token snapshot (`scripts/lib/t0_context_assembler.py`)
- Replay harness: 3-level fixture corpus (`scripts/benchmark/t0_replay_harness.py`)
- Gate locks: file-based, LLM-bypass-proof (`scripts/lib/t0_gate_locks.py`)
- Benchmark scores: Level-1 100%, Level-2 73–87%, Level-3 67–78%
- `--mode benchmark` CLI flag for fixture-tagged runs

### F40: Business Agent Integration
**Priority**: P2 | **Status**: Planned

SubprocessAdapter on GCP VM for VNX Digital workers.

- Replace fragile n8n → SSH → MacBook → claude -p pipeline
- Business-light governance profile activation (folder-scoped, review-by-exception)
- Agent directories: agents/blog-writer/, agents/linkedin-writer/
- 24/7 headless content worker execution
