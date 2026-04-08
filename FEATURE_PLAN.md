# VNX Feature Plan

**Last Updated**: 2026-04-08
**Status**: Active
**Completed**: F1-F36 (see CHANGELOG.md)

---

## Next Features

### F37: Auto-Report Pipeline
**Priority**: P1 | **Status**: Planned

Stop hook + deterministic checks + haiku summary. Workers only provide a short exit summary. Everything else is auto-assembled from stream archive, git diff, and deterministic parsing.

- Worker Stop hook with provider-agnostic execution
- Deterministic extraction: git diff, commit hash, pytest output parsing
- Haiku classification: content type, quality score, risk level
- Tag flow: dispatch tags → auto-derived tags → classified tags → receipt
- Auto-assembled report replaces manual unified reports

### F38: Dashboard Unified
**Priority**: P1 | **Status**: Planned

Single dashboard for both coding and business domains.

- Domain filter tabs (Coding, Business, All)
- Session history browser (view past dispatch outputs)
- Agent selector by name instead of terminal ID
- Business-light governance visibility

### F39: Headless T0 Benchmark
**Priority**: P2 | **Status**: Planned

Parallel validation: same feature executed in 2 worktrees (headless T0 vs interactive T0).

- Context assembler (8 state files → ~5K token snapshot)
- 3-tier benchmark: low/medium/high complexity features
- 6 comparison metrics: decision quality, speed, token cost, OI tracking, dispatch quality, gate discipline
- Shadow mode validation before production cutover

### F40: Business Agent Integration
**Priority**: P2 | **Status**: Planned

SubprocessAdapter on GCP VM for VNX Digital workers.

- Replace fragile n8n → SSH → MacBook → claude -p pipeline
- Business-light governance profile activation (folder-scoped, review-by-exception)
- Agent directories: agents/blog-writer/, agents/linkedin-writer/
- 24/7 headless content worker execution
