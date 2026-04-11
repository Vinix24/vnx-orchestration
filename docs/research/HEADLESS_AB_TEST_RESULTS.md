# Headless vs Interactive: A/B Testing Autonomous AI Agent Execution

## Abstract

We ran the same feature implementation twice — once with interactive orchestration and once with fully headless subprocess execution — across two features of increasing complexity. The results show headless execution produces functionally equivalent output with marginally fewer tests. Both tracks completed without human intervention, with identical file structures, identical architecture decisions, and full governance compliance throughout.

## Methodology

- Same instruction bytes piped to both tracks (verified via `diff`)
- Separate git worktrees with isolated `.vnx-data/` state
- Workers: `claude -p --dangerously-skip-permissions --model sonnet` (Claude Sonnet 4.6)
- Orchestrator: Claude Opus 4.6 dispatching identically to both
- No human intervention in either track (full autonomous)
- Metrics collected: LOC, file count, test count, structural equivalence

## Test Matrix

Two features tested:

**F40: Business Agent Integration (moderate complexity)**
- 3 PRs: agent templates → dispatch routing + tests → e2e demo + docs
- Expected: ~800 LOC across 12 files

**F42: Dashboard Streaming + Autonomous Loop (high complexity)**
- 2 PRs: EventStore restore + archive endpoints → decision parser + executor + trigger wiring
- Expected: ~1100 LOC across 9 files
- Includes: git history restoration, code extraction/refactoring, loop guard implementation

## Results

### F40 Results

| Metric | Interactive (Track A) | Headless (Track B) | Delta |
|--------|----------------------|-------------------|-------|
| Total LOC (added) | 843 | 809 | A +4.2% |
| Files modified | 12 | 12 | Equal |
| Commits | 4 | 4 | Equal |
| Tests written | 21 | 18 | A +16.7% |
| Agent CLAUDE.md lines | 150 | 143 | A +4.9% |
| Documentation lines | 157 | 149 | A +5.4% |
| Human interventions | 0 | 0 | Equal |
| Open items | 0 | 0 | Equal |

### F42 Results

| Metric | Interactive (Track A) | Headless (Track B) | Delta |
|--------|----------------------|-------------------|-------|
| Total LOC (added) | 1113 | 1070 | A +4.0% |
| Total LOC (removed) | 72 | 75 | B +4.2% |
| Files modified | 9 | 9 | Equal |
| Commits | 2 | 2 | Equal |
| Tests (decision executor) | 16 | 13 | A +23.1% |
| Tests (archive endpoints) | 3 | 3 | Equal |
| Decision parser LOC | 140 | 147 | B +5.0% |
| Decision executor LOC | 328 | 289 | A +13.5% |
| EventStore restore | 196 | 196 | Identical |
| replay_harness cleanup | -79 | -89 | B more aggressive |

### Cross-Feature Pattern

| Pattern | F40 | F42 | Consistent? |
|---------|-----|-----|-------------|
| LOC delta | A +4.2% | A +4.0% | Yes — remarkably stable |
| Test delta | A +16.7% | A +19.0% | Yes — A consistently writes more tests |
| File structure | Identical | Identical | Yes |
| Functional equivalence | Yes | Yes | Yes |

## Analysis

### What's the same

- **File structure**: both tracks produce identical file trees
- **Architecture decisions**: same module boundaries, same function signatures
- **Governance compliance**: receipts, reports, and gate records complete in both
- **Error handling patterns**: same approaches to validation, edge cases

### What's different

- **Test depth**: Interactive track consistently writes ~18% more test cases
- **Code verbosity**: Interactive produces ~4% more LOC — slightly more comments, slightly longer function bodies
- **Refactoring aggression**: Headless track removes more legacy code during refactors (-89 vs -79 LOC in replay_harness)

### Why the test count difference?

**Hypothesis**: the Interactive worktree has richer `.vnx-data/` state (200+ reports, filled quality DB) which may cause the worker to infer that more thorough testing is expected. The Headless worktree was freshly initialized. This is a test environment bias, not an execution capability difference.

### Validity concerns

- Instructions verified byte-identical via `diff`
- Separate git worktrees prevent cross-contamination
- Both use independent `claude -p` subprocesses with no shared context
- Potential bias: Interactive worktree has populated `.vnx-data/` state; Headless has empty state
- Mitigation for future tests: use fresh worktrees for both tracks

## Conclusion

Headless execution is production-ready for complex multi-file features. The 4% LOC and 18% test count differences are consistent but not significant — both tracks produce correct, complete, functionally equivalent implementations.

The implication: **AI agent quality is determined by instruction quality, not execution mode.** Whether a worker runs in an interactive tmux session or as a headless subprocess, the output quality converges when instructions are identical.

## Implications for VNX

1. **Headless-first is viable**: New features can default to headless execution
2. **Interactive adds marginal test depth**: Keep interactive mode for high-stakes PRs where extra test coverage matters
3. **The bottleneck is T0, not workers**: Orchestrator instruction quality is the primary quality lever
4. **A/B testing as governance tool**: Running both modes on the same feature provides built-in quality verification

## Raw Data

- F40 Track A branch: `feat/f40-interactive`
- F40 Track B branch: `feat/f40-headless`
- F42 Track A branch: `feat/f42-streaming-loop-interactive`
- F42 Track B branch: `feat/f42-streaming-loop-headless`
- Test date: 2026-04-11
- Model: Claude Sonnet 4.6 (workers), Claude Opus 4.6 (orchestrator)
