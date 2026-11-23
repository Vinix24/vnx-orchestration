# Daily Metrics

**Status**: Active
**Last Updated**: 2026-11-12
**Owner**: track `doorloop-en-effectiviteit-dagelijks`
**Purpose**: Install, read and check the daily lead-time measurement of one project.

The contract (ten metric ids, unknown is never 0, the settled day, proven coverage, the approval gate, opt-in) is [ADR-063](../governance/decisions/ADR-063-dagelijkse-doorloopmeting.md). This page is the runbook. The verbs run as `python3 scripts/daily_metrics.py <verb>`. The `vnx daily-metrics <verb>` wrapper, `install` and `uninstall` come with PR-13 of the track and are not merged yet. The commands below use the script form, except where marked PR-13.

## Install (opt-in per project)

```
# PR-13, not merged yet:
vnx daily-metrics install --project-id <id>
# on main:
bash scripts/launchd/vnx_pinned_launcher.sh <id> scripts/daily_metrics.py daily
python3 scripts/daily_metrics.py report --check --project-id <id>; echo "exit=$?"
```

`install` (PR-13) writes the job `com.vnx.daily-metrics.<id>` (04:10 local, through the pinned launcher) for that one project. `vnx init` never installs it and `vnx doctor` does not expect it. Until PR-13 merges there is no `install`: the job `com.vnx.daily-metrics.<id>` does not exist and `report --check` cannot report `stale`.

The `bash` line is the first manual run. It fills the last 14 settled days, oldest first. Those days are `backfilled`: they count in the baseline, never in freshness. The check therefore turns green only after seven nights of real runs.

The day files live in `~/.vnx-data/<id>/state/daily_metrics/<id>/`: one `<YYYY-MM-DD>.json` per UTC day, `<YYYY-MM-DD>.r<N>.json` per revision, and `proposals.ndjson` as the append-only proposal ledger.

## What `report --check` checks

Exit 0 only when every condition holds. Otherwise exit 1, and `unmet` names each failed condition. The window is the last seven settled UTC days, ending at the newest settled day (`now >= day + 1 day + 2 h`).

| `unmet` name | Meaning |
|---|---|
| `store_empty` | The store holds no day file, or does not exist. An empty store is never done. |
| `fresh_days` | Fewer than 7 consecutive fresh days ending at the newest settled day. Fresh means: this project's file, `status == "final"`, `backfilled == false`, and `collected_at` between the settle moment and `day end + 30 h`. A backfilled, incomplete, missing or unreadable day breaks the run. The newest revision counts, so a `--recollect` revision is not fresh. |
| `max_gap_hours` | More than 48 hours between two consecutive collections in the window, `now` counted as the last point, or no readable collection at all. |
| `missing_ids` | A window day lacks one of the ten ids. |
| `keyed_without_entries` | `gate_duration_seconds_by_seat` has no entries, or is not a record. |
| `malformed_records` | A record or entry that is not an object, or a value that is not a finite number. |
| `unknown_without_reason` | A `null` value without an `unknown_reason`. |
| `zero_for_unknown` | A value next to an `unknown_reason`, 0 included: in a window day or in any day file or revision of the store. |
| `unreadable_files` | A day file or revision that is not a JSON object with `metrics`. |
| `ledger_unreadable` | `proposals.ndjson` has a damaged or torn line. |
| `approval_without_tty` | An `approved` event whose `evidence.tty` is not exactly `true`. The names are in `approvals_without_tty`. |

## Reading `stale` and `unknown`

`status` is `ok`, `unmet` or `stale`. `stale` means `launchctl print gui/<uid>/com.vnx.daily-metrics.<id>` reports the job as loaded while the newest settled day has no fresh file: the job is installed but not measuring. Read the job log and the newest day file first. `job.loaded` is `false` when launchd does not know the label and `null` when `launchctl` could not be asked. A loaded job is supporting evidence only; freshness always comes from the day files.

`unknown` is a value of `null` with a reason. It is never 0, never "no data so fine", and never a measurement. `operator_wait_hours`, `cost_per_merged_pr_usd` and `merges_without_receipt_count` stay `unknown` with a fixed reason until their owner tracks land (ADR-063 besluit 2). Seven `unknown` days in a row are seven days without a measurement.

## Reading a window

```
python3 scripts/daily_metrics.py report --metric pr_open_to_merge_median_hours --last 7 --json --project-id <id>
python3 scripts/daily_metrics.py report --metric gate_duration_seconds_by_seat:codex_gate --last 7 --json --project-id <id>
```

The output carries `window.from`, `window.to`, `pooled_value` and `sample_size`. Only `final` days count. Quantile metrics pool the raw samples, so `sample_size` is the number of pooled samples. Count metrics are the mean of the day values, and `sample_size` is the number of days. `excluded_days` names every skipped day with its reason. Without one measured value, `pooled_value` is `null` with `unknown_reason: "no_measured_value_in_window"`.

## The zero-for-unknown scanner

```
python3 scripts/daily_metrics.py report --scan-zero-for-unknown --project-id <id>; echo "exit=$?"
```

It reads every day file and every revision. Exit 0 needs `violations: 0`, no `unreadable_files` and a non-empty store. Exit 1 lists each offending record under `records`, with file, metric and value.

## Rollback

```
# PR-13, not merged yet:
vnx daily-metrics uninstall --project-id <id>
```

`uninstall` (PR-13) removes only that project's job. The day files and the ledger stay: a `final` day is never overwritten and nothing in the measurement deletes data. There is no schema change and no migration to undo. Removing the store itself is deleting data, and that waits for the operator's approval.
