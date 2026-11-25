# ADR-098 - The impact shadow job in its own workflow

**Status:** Proposed 2026-11-25 (T0, track `shadow-job-off-the-merge-path`, deliverable S1). Amends ADR-074 decision 15; ADR-074 itself stays unchanged (ADR-079 section 83).
**Date:** 2026-11-25
**Decided by:** plan v1 `claudedocs/specs/shadow-job-off-the-merge-path/plan.md` (not in the repo). Plan-gate 2026-11-25: codex REVISE, opus REVISE, tiebreaker deepseek-v4-pro START, with this ADR as the required change.
**Cross-refs:** ADR-074 (impact selection and its shadow), ADR-063 (unknown is never zero).

## Context

The job `Impact shadow (runs only S, never required)` lives in `.github/workflows/vnx-ci.yml`. It decides nothing (`continue-on-error: true`, in no `needs`, in no required-check list), but merges wait for the conclusion of the whole `VNX CI` workflow. Measured 20-24 Nov 2026 over 59 PR runs: the median `VNX CI` wall clock is 13.4 min, and the shadow job was the longest job in 2 of the last 3 green runs (14.4 and 22.0 min against 7 to 12 min for the Profile A shards). The measurement the job feeds is still wanted for the follow-up track `impact-selection-tuning`, so the job stays; it only moves.

`vnx impact collect` (`scripts/lib/impact_ledger.py`) writes one append-only ledger line per `VNX CI` run attempt. Once the job is in another workflow, the collector has to find its run and record, and a line written before they exist loses them for good.

## Decision

**1. The move (amends ADR-074 decision 15).** The shadow job moves from `vnx-ci.yml` to its own workflow `Impact shadow` (`.github/workflows/impact-shadow.yml`, `on: pull_request` only, no concurrency group, so a cancelled run does not leave its pair missing). Job id, name, runner, `continue-on-error`, timeout and scripts are unchanged. The Profile A shadow STEP and its artifact `impact-shadow` stay in `vnx-ci.yml` on every event. The required checks of main do not change. Everything else in ADR-074 decision 15 stands.

**2. Pairing identity.** A `VNX CI` run is paired with an `Impact shadow` run in this order:
- the head sha (`gh run list --workflow "Impact shadow" --commit <sha> --event pull_request`);
- then the merge candidate: both records carry `base_sha` and `candidate_sha` (`record.json` of artifact `impact-shadow` on the VNX CI run, `job.json` of artifact `impact-shadow-job` on the shadow run). Among the shadow runs for the head sha, newest first, the first completed run whose job record has the same `base_sha` and `candidate_sha` wins. Two shadow runs on one head sha can test different candidates when main moved between them, so the head sha alone is not an identity.
- A VNX CI run without a shadow record (light profile, missing artifact) has no candidate to compare: the newest run wins.
- No run matches while at least one record was read: it counts as "no shadow run" and the line carries `artifact:impact-shadow-job:candidate_mismatch`.
- The VNX CI shadow record identifies a candidate but no candidate job artifact is readable: the attempt stays pending under 2 h or with an unreadable age. After 2 h the line carries `artifact:impact-shadow-job:candidate_unreadable`, with no `shadow_run_id` or `shadow_run_attempt`.

The pair is stamped on the line as `shadow_run_id` and `shadow_run_attempt`. The old shape (the VNX CI run's own jobs contain the shadow job) is read as before, so the collector can ship before the workflow moves.

**3. Lifecycle.**
- **Pending:** the newest shadow run is not completed, or there is no pairing run and the VNX CI run is under 2 h old (or its age cannot be read). No line is written; `collect` reports the attempt as failed with `shadow_run_pending` and tries again next time.
- **Finalization:** after 2 h without a pairing run the line is written with the job fields in `missing`. A line is never rewritten (the ledger is append-only, ADR-074 decision 7).
- **Late arrival:** a shadow rerun or a late run after a line is written is not collected. The line stays as it is.
- **Not a pull request:** a `VNX CI` run whose event is not `pull_request` (a push on main, `workflow_dispatch`) has no shadow job and never gets a paired run. It is written at once, with the job fields in `missing`, and no shadow run is looked up. An unreadable event is not positive evidence of this and stays pending.

## Consequences

- Merge order: the collector change merges before the workflow move. Otherwise `collect` writes permanent lines without the job record for every run in between.
- `VNX CI` stops waiting for the shadow job; the merge door still reads only `VNX CI`, which is intended.
- `SCHEMA` of the ledger is unchanged; entries gain `shadow_run_id` and `shadow_run_attempt`, and the report reads entries with or without them.
- A candidate mismatch or a missing run is a visible hole in `missing`, never a silent pass (ADR-063).

## Way back

Revert the workflow move. The job returns to `vnx-ci.yml` and the collector reads the old shape again.
