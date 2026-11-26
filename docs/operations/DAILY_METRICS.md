# Daily Metrics

**Status**: Active
**Last Updated**: 2026-11-26
**Owner**: track `doorloop-en-effectiviteit-dagelijks`
**Purpose**: Install, read and check the daily lead-time measurement of one project.

The contract (ten metric ids, unknown is never 0, the settled day, proven coverage, the approval gate, required nightly jobs) is [ADR-063](../governance/decisions/ADR-063-dagelijkse-doorloopmeting.md). This page is the runbook. `vnx daily-metrics install` installs the job; other verbs pass through to `python3 scripts/daily_metrics.py <verb>`. The reporting examples below use that script form.

## Install (required per project, ADR-063 amendment 2026-11-25)

```
vnx daily-metrics install --project-id <id>
# Optional manual collection through the project's pin:
bash scripts/launchd/vnx_pinned_launcher.sh <id> scripts/daily_metrics.py daily
python3 scripts/daily_metrics.py report --check --project-id <id>; echo "exit=$?"
```

`install` writes and loads `~/Library/LaunchAgents/com.vnx.daily-metrics.<id>.plist` for that one registered project. The job runs on load and at 04:10 local through the pinned launcher. `vnx init` prepares `<project store>/launchd/com.vnx.daily-metrics.<id>.plist` with the validated project id and resolved engine path. It never calls launchctl for this family or writes its definition to LaunchAgents. It prints one request for Vincent's operating session: what, why, the install command above, rollback, verification and class `reversible`. `vnx doctor` reports a missing job as FAIL with that exact install command. The other four required families retain their existing init installation behavior. `report --check` can report `stale` when the job is loaded but the newest settled day has no fresh file.

The first run collects missing final days from the last 14 settled days, oldest first. Collections later than `day end + 30 h` are `backfilled`: they count in the baseline, never in freshness. `report --check` needs seven consecutive fresh days and all the checks below; a catch-up run alone does not prove that.

The repository's launchd template guard checks XML and interpreter compatibility.
Its shell scanner ignores full-line comments when selecting an interpreter:
historical interpreter paths in comments are not executable commands.

The day files live in `~/.vnx-data/<id>/state/daily_metrics/<id>/`: one `<YYYY-MM-DD>.json` per UTC day, `<YYYY-MM-DD>.r<N>.json` per revision, and `proposals.ndjson` as the append-only proposal ledger.

## Proposal inbox lifecycle

`propose` opens one `besluit` point per proposal, with ref `proposal:<id>`, the project, metric, value, baseline and metric day. Its copy block is `vnx daily-metrics approve <id> --action "<intervention>"`; the operator fills in the intervention and runs it in their own terminal. Inbox answers never approve. Notification failures leave proposals in the ledger, report an inbox error on stderr and exit 1; rerunning retries open proposals without duplicate points.

Before proposing, even when the newest day is not final, `propose` appends one `expired` event for each proposal at least seven full days past `recorded_at`, unless already approved, expired, rejected or judged. Expired and rejected proposals cannot be approved and release their metric key for future proposals. The inbox reads the project's ledger: `approved` closes as `beantwoord`, `expired` as `vervallen`, using `recorded_at` as evidence. Missing, unreadable or invalid timestamp evidence keeps the point open. Existing `afgewezen` and `later` answers still close their points. Approval now records `recorded_at`; older approvals without that field remain open with a reason.

After expiry and before new proposals, `propose` creates one deliverable for each approved proposal naming `deliverable: {track, title, output_kind, task_class}` in its `proposed` event. All four fields must be nonempty strings; `output_kind` is `pr` or `doc`, and `task_class` uses the planning/smart-router vocabulary. Invalid objects (including null) are named ledger refusals. Existing proposals without the field remain valid: an approval produces `no deliverable named` in the JSON output and writes nothing. Current rules name no deliverables; M4b supplies them.

Creation uses the planning layer in the named project/track with status `proposed`; it never promotes, fires or calls the door. The proposal lock covers the check, creation and single `deliverable_created` append (project id, proposal id, deliverable id, recorded_at). A deterministic project/proposal ID and planning metadata make a retry after a failed append recover the existing deliverable; conflicting metadata is refused. An unknown track or unreadable planning store exits nonzero with a named stderr error and no creation event, so a later run retries. `--state-dir` selects the planning store explicitly; otherwise the project's central state directory is used. A JSON `deliverables` line reports creations or missing specifications before the usual proposal summary.

After deliverable creation, `propose` records `applied` once under the proposal lock from the project's native `t0_receipts.ndjson`: an exact deliverable dispatch (or its numbered rework round), `pr_merged` with `conclusion: merged`, PR number, merge SHA and UTC merge-receipt timestamp. An unusable match (missing SHA, invalid PR number, conclusion or timestamp) is skipped; a complete receipt wins over an incomplete one. Only disagreeing complete receipts conflict. With only unusable matches or a conflict, that proposal stays pending and its id and reason appear on stderr; other applications, new proposals and inbox notifications still run before exit 1. `--state-dir` selects this evidence store. No PR titles are matched.

A missing receipt file or no matching merge leaves proposals pending without error. Non-candidate lines, non-object lines and an unfinished last line are skipped, as in the predecessor reader. An unreadable file or corrupt complete `pr_merged` candidate remains a named error for pending proposals; the shared receipt log is never rewritten. The proposal ledger itself remains strict: damaged lines stop processing.

`daily` runs this application step before `verdict` and continues all steps after a nonzero exit. It reports `{"command_failed": "<step>", "exit_code": 1}` separately from `{"command_import_failed": "<module>", "exception": "<class>"}`; either makes `daily` exit 1. Without application, `verdict` lists `{proposal_id, reason: not_applied}` under `waiting`. Application day `p` starts the observation window `p+1..p+7`; it waits until `p+7` settles (next day at 02:00 UTC). The approval's frozen baseline, sample sufficiency and schema checks still apply; unknown yields `undetermined`. Each verdict opens one acknowledgment point through `inbox ask` (`bron: vraag`, stable `antwoord:` ref derived from `proposal verdict <id>` so display wording changes cannot reopen answered points). Judgment runs before notification replay: an unreadable inbox, refused ask or malformed old notification reports an error and exits 1 without blocking new due verdicts. Reruns recover failed notifications without reopening answered points.

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

The `daily_metrics` health beacon has a separate 36-hour deadline (ADR-097).
Readers measure the age of its last completed collection run (`last_run_ts`,
the epoch form of `last_run_iso`), including legacy beacons with a null interval.
After 36 hours it reads `fail`, reason `no_measurement_36h`, with `age_hours`.
The job is required for every registered project. Init seeds
`state/health/daily_metrics.json` with status `fail`, reason `never_measured`,
and the project id, so T0 session start names the project before its first run.
The seed timestamp marks preparation, not a measurement; the failure remains
until the collector writes a new beacon. Reinitialization preserves an existing
beacon under the same lock as the collector, including stale or corrupt evidence.
Preparation failures abort init with a visible error.
Health readers still discover participation through the project's beacon (or an
explicit expectation). Both supported paths are read directly; unreadable signals
stay named `corrupt` rows. Another project's beacon cannot satisfy this project.
A project never initialized, or whose beacon was removed, needs init to seed it;
doctor independently reports its missing required launchd job.
The existing content rule is unchanged: no final file in the last three settled
days gives `fail` (`no_final_settled_day`), even with a fresh heartbeat.
Readers include `state/health/daily_metrics.json` under the supplied project data
root. The T0 index keeps a failing `daily_metrics (<project-id>): <reason>` row
visible in its bounded list; project identity comes from that root, not the shell.

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

There is no `vnx daily-metrics uninstall` verb. To stop the installed job manually, ask operating to substitute the project id and boot out that one Label:

```
launchctl bootout gui/$(id -u)/com.vnx.daily-metrics.<id>
# Check after installation (the Label must appear):
launchctl list | grep com.vnx.daily-metrics.<id>
```

This leaves the plist, day files, ledger and health beacon in place. The beacon's age rule still applies; stopping the job does not clear its health signal. There is no schema change or migration to undo. Removing the store itself is deleting data, and that waits for the operator's approval.
