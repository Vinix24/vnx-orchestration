# Dreaming: nightly memory consolidation

**Status**: shipped, opt-in, off by default (`VNX_DREAM_SCHEDULER_ENABLED=0`). The liveness watch is on main and not yet released (`VERSION` says 1.8.0).
**Decisions**: [ADR-019](../governance/decisions/ADR-019-auto-dream-memory-consolidation.md) (the cycle), [ADR-066](../governance/decisions/ADR-066-dream-liveness-watch.md) (the liveness watch).
**Sources**: `scripts/dream/consolidator.py`, `scripts/dream/review_gate.py`, `scripts/dream/scheduler.py`, `scripts/lib/dream_liveness.py`.

---

## What a cycle does

A dream cycle reads the `success_patterns`, `antipatterns` and `intelligence_injections` of one project from `quality_intelligence.db`. It asks one model to sort every pattern into merge, drop, archive or flag. The answer is written to a review file. Nothing else changes.

The model is the provider `VNX_DREAM_PROVIDER` names (`_PROVIDER_KEY`, `scripts/dream/consolidator.py:48`). The default is `deepseek`: the key-auth DeepSeek provider with an output ceiling of 8,000 tokens (`_dispatch_deepseek_consolidation`). `kimi` still works behind the same key, through the kimi CLI (`_dispatch_kimi_consolidation`). There is no Anthropic SDK in the path. The prompt carries at most 50,000 characters of pattern JSON; larger input is split over several prompts, never cut off (`_PROMPT_PAYLOAD_BUDGET`).

## What a cycle does not do

A cycle never changes a pattern. It writes `<data root>/state/dream/<cycle_id>-pending-review.json` with `requires_operator_review: true` and a `dream_cycles` row with `operator_reviewed=0` (`_write_pending_review`, `run_dream_cycle`).

Patterns are archived only when an operator approves the review (`review_gate.approve_cycle`). Even then the original rows stay: the approval writes `dream_pattern_archives` records. Dreaming proposes. A human decides.

There is no auto-apply. [ADR-019](../governance/decisions/ADR-019-auto-dream-memory-consolidation.md) describes a post-burn-in auto-apply for exact deduplication. The code on main does not implement it, and ADR-066 states that this track introduces none.

## Guards before the model is called

A cycle skips, with a `dream_cycle_skipped` event and a named reason, when:

| Reason | Cause |
|---|---|
| `scheduler_disabled` | `VNX_DREAM_SCHEDULER_ENABLED` is not `1` |
| `content_boundary_unconfigured`, `content_restricted_project`, `content_restricted_all_rows` | the content boundary forbids the project or every row |
| `probe_not_ok` | the injection-effectiveness probe is neither `ok` nor `degraded` |
| `incomplete_data` | the receipt ledger is absent, empty or has no receipt in the last 48 hours |
| `insufficient_data` | no core patterns to consolidate |
| `provider_switched_off` | the operator switched the family of the chosen provider off (`vnx providers`; `deepseek` by default) |
| `dream_provider_unknown` | `VNX_DREAM_PROVIDER` is empty or neither `deepseek` nor `kimi`; no provider is called and kimi is not the fallback |

A `degraded` probe runs the cycle in proposal-only mode (`mode=proposal_only` in the review file). Anything else outside `ok` and `degraded` skips (`_PROBE_HEALTH_RUNNABLE`).

## Scheduling

On macOS `vnx dream install-scheduler` writes and loads two LaunchAgents in `~/Library/LaunchAgents/` (`scheduler._install_macos`):

- `com.vnx.auto-dream`: runs `vnx dream run --project-id <id>` daily at 03:00 local time.
- `com.vnx.dream-watch`: runs the liveness probe daily at 04:00 local time.

On Linux the installer adds one crontab entry at 03:00 and no watch job. `vnx dream uninstall-scheduler` removes what the installer wrote.

The installed job template sets `VNX_DREAM_SCHEDULER_ENABLED=1` (`com.vnx.auto-dream.plist.j2`). The flag in the process environment is the arm switch inside the cycle, so a manual `vnx dream run` with the flag unset skips with `scheduler_disabled`. The registry default is `0` (`config_registry.py`, category `intelligence`).

## Liveness: how silence is caught

Before ADR-066 a disabled or silent scheduler could still read as a pass. The watch now builds one document, `dream_liveness/1`, in `scripts/lib/dream_liveness.py`. `vnx doctor` (`check_dream_cycle`) and the probe `scripts/dream_liveness_probe.py` are projections of that same document.

It answers four questions from deterministic sources (the installed plist, `launchctl`, the dream NDJSON events, the job log, a read-only view of the database):

1. **Is the job loaded?** `job_not_loaded` is an error finding.
2. **Did the last night write anything?** An attempt counts only when the job log and a dated event carry the same `cycle_id`. No attempt within 26 hours is `no_attempt_26h`.
3. **Is the skip reason known?** The reasons above are classified as `allowed_skip` or `blocked`. An unknown or empty reason is `blocked`, never a pass (`classify_skip`).
4. **Does a waiting proposal go unread?** A review older than 30 days is `review_overdue`, a warning (`REVIEW_OVERDUE_SECONDS`). Its age comes from `created_at` in the review file, then from the completion event, and only then from the file time. The source is reported as `created_at_source`.

Progress is measured over a sliding window of seven closed nights. A completed cycle counts only when it produced output (merged, dropped, archived or flagged above zero). The state is `ok`, `deviation` or `unmeasured`, and `unmeasured` is not a pass. A review file that does not parse is counted in `unreadable_count` and raises `reviews_unreadable`.

The probe never changes dream state. With `--append` it writes one line to `<data root>/state/dream_liveness.ndjson` and a beacon (`dream_liveness`, expected interval 25 hours). A probe that stops running turns its own beacon stale. The doctor check returns WARN, not PASS, whenever a finding is measured or the state is not `ok`.

## Operator commands

```
vnx dream run [--project-id ID] [--dry-run]    run one cycle now
vnx dream status                                latest cycle and pending reviews
vnx dream history [--limit N]                   recent cycles
vnx dream review <cycle_id> [--approve|--reject --reason TEXT]
vnx dream install-scheduler | uninstall-scheduler
python3 scripts/dream_liveness_probe.py --project-id ID --json [--strict]
```

## Where the output goes

| Output | Location |
|---|---|
| Events (`dream_cycle_started`, `_completed`, `_skipped`, `_failed`, `_timeout`, `_approved`, `_rejected`) | `<data root>/events/dream/<UTC date>.ndjson`, written before the database row |
| Review proposal | `<data root>/state/dream/<cycle_id>-pending-review.json` |
| Cycle row and archive records | `dream_cycles`, `dream_pattern_archives` (migration `0025_dream_consolidation.sql`) |
| Job logs | `<data root>/events/dream/launchd.out.log`, `launchd.err.log` |
| Kimi call cost (only when `VNX_DREAM_PROVIDER=kimi`) | the cost ledger, producer `dream` (see [COST_TRACKING_GUIDE.md](COST_TRACKING_GUIDE.md)) |

The data root is the central store of the project (`resolve_central_data_dir`), never the repository's `.vnx-data`.

## Related

- [SELF_LEARNING_LOOP.md](SELF_LEARNING_LOOP.md): the receipt-driven learning loop, which also only proposes.
- [INTELLIGENCE_SYSTEM.md](../core/technical/INTELLIGENCE_SYSTEM.md): the intelligence store the cycle consolidates.
