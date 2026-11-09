# Process-tree load measurement (RS-1)

**Status**: Active · **Owner**: T0 (track `rekenslot-per-machine`) · **Tool**: `scripts/measure_process_tree_load.py`

The host slot budget needs one number per workload profile: how many cores a
full SEO gate really takes, including the subprocesses beside the xdist workers
(browsers, codex, claude). This runbook produces that number for one run. The
script only reads; it never signals or kills a process.

## Baseline of one full SEO gate

1. Start the gate as usual (`local_ci_gate.sh`, full profile) and note what else
   the machine does at that moment.
2. Find the gate's root pid from the wrapper you launched, or
   `ps -A -o pid=,command= | grep -E "local_ci_gate|pytest"`.
3. Attach the sampler and keep both artefacts:

   ```bash
   mkdir -p claudedocs/measurements/process-tree-load
   python3 scripts/measure_process_tree_load.py \
     --root-pid <PID> --interval 2 --duration 1800 \
     --out claudedocs/measurements/process-tree-load/seo-gate-<date>.ndjson \
     --summary > claudedocs/measurements/process-tree-load/seo-gate-<date>.summary.json
   ```

   Without `--duration` it samples until the root is gone, which a gate run
   wants; `--match REGEX` resolves the root by command line instead of pid.
4. One NDJSON line per sample: `ts`, `root_pid`, `n_xdist`, `n_other`,
   `xdist_cpu`, `other_cpu`, `rss`, `swap_used`, `compressor`. An unreadable
   value is `null` with a `reasons` entry, never `0`.

## Reading the number

`cores_p95` is `p95(xdist_cpu + other_cpu) / 100`: the tree's CPU load at the
95th percentile in cores. `xdist_cpu` is the workers; `other_cpu` is everything
else under the root and includes the root's own CPU. Compare with `n_xdist` to
see how much load ran outside the worker count. A run with failed snapshots is
still usable, but say how many in the notes next to `cores_p95`: the summary's
`failed_snapshots` counts the `ps_failed:` lines that contributed no values.
Note next to every run, because the number is only comparable
under the same conditions: the `-n` / `LOCAL_CI_GATE_WORKERS` value the gate
chose, what else ran on the machine (other gates, runner jobs, interactive
sessions), and the swap and compressor values from the summary.

## The slot number

Decide the slot number for a full SEO gate after **at least three runs**, not
from one: the summary's `note` field says a run is a measurement, not a budget.
Spread the runs over quiet and busy moments, keep every NDJSON file and summary
under `claudedocs/measurements/process-tree-load/`, and give the median
`cores_p95` to the slot budget with the three summaries as evidence.
