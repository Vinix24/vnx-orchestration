# field-tests — realistic-bench

Real-world benchmark suite for VNX provider lanes. Tasks are derived from actual production work in Mission Control, SEOcrawler v2, and VNX-orchestration itself. Each task has programmatic verification (tests must pass, files must exist, SQL must validate) plus an LLM-judge fallback for non-mechanical scoring.

This is the **realistic-bench** mode. It complements the **micro-bench** in `scripts/benchmark/prompts/` (7 short synthetic prompts for daily smoke runs). Field-tests are heavier, slower, and meant for monthly cadence or whenever a new model lane is added.

## Quick start

Single full run:
```bash
bash scripts/benchmark/field-tests/monthly_runner.sh
```

Single tier:
```bash
python3 scripts/benchmark/field-tests/runners/run_field_tests.py --tier t1_trivial --n 3
```

Single lane against single task (smoke):
```bash
python3 scripts/benchmark/field-tests/runners/run_field_tests.py \
  --lane claude-sonnet-4-6 \
  --task 01_yaml_config \
  --n 1
```

## What this measures

For each (lane, task, replication) cell:

| Metric | How |
|---|---|
| Wallclock seconds | Dispatch start to first receipt |
| Tool-call count | Parsed from session jsonl |
| Cost USD | From `models.yaml` rates × dispatch usage |
| Verify pass/fail | Programmatic check per task (`verify.py`) |
| Quality score 0-5 | LLM-judge (Opus) for non-programmatic dimensions |
| Errors logged | Captured from worker session |

## Task taxonomy

| Tier | What it tests | Source-inspiratie |
|---|---|---|
| **T1 trivial** | Mechanical refactors, config edits, single-file changes (~5 min) | MC PR #236, SEOcrawler PR #119/#125 |
| **T2 medium** | Multi-file refactor + tests + migration, bounded scope (~15-25 min) | MC PR #237/#244-249, SEOcrawler #123 |
| **T3 complex** | Cross-module state-machines, security boundaries, mock-introspection traps (>1 hour) | MC PR #239, SEOcrawler PR #100/#118 |

Each task has its own folder with `instruction.md`, `seed/` (starting files), `verify.py` (programmatic check), and `expected.json` (LLM-judge rubric).

## Lane coverage

Defined in `scripts/benchmark/models.yaml` (root). 10 lanes:

- `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`
- `claude-sonnet-4-6`, `claude-haiku-4-5`
- `deepseek-v4-pro`, `deepseek-v4-flash`
- `kimi-k2-6`, `kimi-k2-0905`
- `local-gemma-4b` (free, MLX on Mac)

T1 runs all 10. T2 runs 8 (drop haiku + local-gemma — known too thin). T3 runs 4 (opus + sonnet + ds-pro + kimi-k2-6 only — proven cost-tier threshold).

## Output

Results go to `results/<ISO-timestamp>/`:
```
results/2026-06-04T08-30Z/
├── raw.csv                 # per-cell row
├── summary.md              # lane × tier matrix
├── per-lane.md             # narrative per lane
├── cost-per-quality.csv    # $/quality-point earned
└── methodology.md          # what ran, N, scorer, limitations
```

`results/` is gitignored. Run-archive is kept locally and copied to `claudedocs/` on demand for marketing snapshots.

## Monthly cadence

Run on the 1st of each month and after any new model lane is added. Diff against prior month's summary to detect:
- Model regression (provider silently swapped weights)
- Cost drift (token-usage shifts after prompt changes)
- New lane fit (where does the new model land in the matrix)

Re-run the same task definitions for reproducibility — only change `models.yaml` lane-set, not the task `seed/` or `verify.py`.

## Adding a new lane

1. Add entry to `scripts/benchmark/models.yaml` with provider/model_arg/cost
2. Run `python3 runners/run_field_tests.py --lane <new-id> --tier t1_trivial --n 1` smoke
3. If smoke passes, full run: `python3 runners/run_field_tests.py --lane <new-id> --n 3`
4. Append results to next monthly summary

## Adding a new task

1. Choose tier folder under `tasks/`
2. Create `<NN>_<slug>/` folder with `instruction.md`, optional `seed/`, mandatory `verify.py`, optional `expected.json`
3. Register in `tasks.yaml`
4. Smoke-test against one lane before full run

## Anti-patterns

- **No mock workers.** Every lane spawns a real `claude`/`deepseek`/`kimi`/etc subprocess. If a lane can't be invoked (auth missing, binary absent), it's skipped — not faked.
- **No partial verification.** `verify.py` must return pass/fail with concrete evidence (file exists, test exits 0, SQL constraint validates). LLM-judge is fallback for dimensions that can't be programmatically checked, not a substitute for missing rigor.
- **No model-by-name interpretation.** Marketing-output cites composite scores per (lane, tier, task) cell. "Model X is best" is not a claim this suite supports.
