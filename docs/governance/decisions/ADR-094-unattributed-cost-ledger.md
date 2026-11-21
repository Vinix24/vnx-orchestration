# ADR-094 - A cost line without a resolved tenant goes to one unattributed ledger

**Status:** Proposed 19-11-2026 (track `agnostisch-standaard`, deliverable ADR-094, dispatch `dlv-1598a6064fee`). Follows up ADR-007 amendment 2026-06-24 point 4 and ADR-043 via cross-ref; their files stay unchanged (ADR-079 § 83).
**Date:** 2026-11-19
**Decided by:** T0, on the plan-gate of the `agnostisch-standaard` plan, round 9 (section V9.1, open item OI-3024). The number 094 is reserved by the plan: 087 to 092 are on main, 093 belongs to track `config-een-oppervlak`.
**Cross-refs:** ADR-005 (the decision lands before the state change), ADR-007 (amendment 2026-06-24, points 2 and 4; Decision rule 2, the composite key over `project_id`), ADR-043 (§ 1 the `run_id` with its project part, § 2 `RunIdProjectMismatch`, § 6 one lock file next to the ledger), ADR-079 (§ 83, existing ADRs are frozen), ADR-090 and ADR-091 (sibling decisions of the same track; ADR-091 established that the door writes what a builder runs as into the spec and the bundle, and this ADR puts the project id in that same carrier); plan `claudedocs/specs/agnostisch-standaard/plan.md` (gitignored, not in the repo), V9.1 and V9.3; deliverables AG-1f1, AG-1f3 and AG-1o.

**Deterministic or model.** Everything in this ADR is deterministic: a refusal at the door on a missing id, a fixed file path, a fixed set of fields, a lock rule and a counter. No model is involved. A cost line booked on the wrong project, or lost, has to be findable with a test.

## Context

ADR-007 amendment 2026-06-24 made the QI-write-tier fail closed: an unresolvable tenant raises `TenantUnresolved` and the writer skips the write (point 2). It kept the NDJSON cost log out of that rule (point 4): `provider_costs.emit_provider_cost` "must never skip an event (cost-audit = no data-loss) and stamps a store-derived pid best-effort". Point 4 does not say what "best effort" does when nothing resolves. The code answers with `vnx-dev`.

Measured on `b0c92f1c` (plan) and re-checked on `f5ea294dc` (`origin/main` at the time of writing); none of the cited lines moved:

| Location | What it does |
|---|---|
| `scripts/lib/dispatch_cli.py:122` | the door's data-dir fallback: `project_id or os.environ.get("VNX_PROJECT_ID", "vnx-dev")` |
| `scripts/lib/subprocess_dispatch.py:648` | the terminal-pinned lane: `_project_id = os.environ.get("VNX_PROJECT_ID", "vnx-dev")` |
| `scripts/lib/provider_costs.py:220` | `costs_path()` picks `~/.vnx-data/<VNX_PROJECT_ID or vnx-dev>` when no explicit data dir is set |
| `scripts/lib/provider_costs.py:299` | `emit_provider_cost` stamps `project_id or VNX_PROJECT_ID or vnx-dev` |
| `scripts/lib/harness_run_cost.py:282` | after the store (`project_id_from_store_path`, then `resolve_stamp_project_id`) it falls back to the env and then to `vnx-dev` |

ADR-043 puts the project in the natural key of a ledger line: `run_id` is `<project_id>:<producer>:<dispatch_id|gate_run_id>:<attempt>`, and `project_id` equals that project part. A `run_id` of another project is refused (`RunIdProjectMismatch`, § 2), and a refused line writes nothing.

So a cost line whose tenant does not resolve has two outcomes today, and neither is allowed. A refusal after the run loses the line, which breaks point 4. A guessed `vnx-dev` books it on the wrong project, which is the contamination the 2026-06-24 amendment was written against.

## Decision

**1. Identity is fixed before execution.** The door refuses a dispatch without a resolved project id, before any model runs (AG-1f1). The door writes the id into the spec and the bundle. Every cost writer after a door dispatch takes the id of its caller (the bundle, the `state_dir`), never from the env and never from a default.

**2. A cost line that still arrives without a tenant is never skipped and never booked on `vnx-dev`.** This is the remaining case, a run outside the door. Such a line:

- goes to one machine-wide ledger, `~/.vnx-data/provider_costs.unattributed.ndjson`. It is a file, not a directory, so a glob over `~/.vnx-data/*/state/` never sees it as a project;
- has its own lock file next to it, following the one-lock rule of ADR-043 § 6 (created with `O_CREAT`, never removed, taken before the file itself);
- carries everything of the run except the project part, plus `reason` (why no tenant resolved) and `cwd`;
- counts in no project total;
- makes the writer log at ERROR and call the counter of AG-1o (`record_project_id_refusal`, anchored at `~/.vnx-data/` when no store is known). `vnx doctor` shows the count and the amount of the last 24 hours.

**3. Recovery.** Every line carries `dispatch_id`, provider, model, tokens and `cwd`, which is enough to find the run again. Assigning a line to a project, as a line in that project's ledger under ADR-043, is a separate step in phase 2. In phase 1 nothing is lost: the line exists, is counted and is visible, it is only not attributed.

**4. The work splits in two deliverables.**

- **AG-1f1** keeps the door: `scripts/lib/dispatch_cli.py:122` and `scripts/lib/subprocess_dispatch.py:648`.
- **AG-1f3 (new)** takes the cost writers, `scripts/lib/provider_costs.py:220,299` and `scripts/lib/harness_run_cost.py:282`, and implements Decision 2 and 3. It lands after AG-1o, whose counter it calls.

**5. No new table.** The unattributed ledger is an NDJSON file, not a central-DB table, so the ADR-007 key rule for central tables (composite UNIQUE/PK over `project_id`) does not apply.

## Relation to ADR-007 and ADR-043

- This ADR follows up ADR-007 amendment 2026-06-24 point 4 for what "best effort" does when nothing resolves: the event is still written, to the unattributed ledger, and never stamped with a guessed id. Every other clause of ADR-007 and of the amendment stays in force.
- This ADR follows up ADR-043 for a ledger line without a project part: such a line never enters a project ledger, so the `run_id` rule and `RunIdProjectMismatch` of § 2 are not weakened. It lives in its own file under its own lock. Every other clause of ADR-043 stays in force.
- Neither file is edited (ADR-079 § 83).

## Consequences

- A door dispatch can no longer reach a cost writer without a project id, so the `vnx-dev` fallbacks in the five locations above become dead for that path and are removed by AG-1f1 and AG-1f3.
- A run outside the door that cannot resolve a tenant still costs money, and that money is visible: one file, one ERROR line, one counter entry and one `vnx doctor` section. It is never silently added to `vnx-dev`'s totals.
- Per-project totals become smaller wherever `vnx-dev` absorbed foreign lines before. A sum over all projects plus the unattributed ledger is the machine total.
- Every branch that stamps a project id decides on positive evidence. A missing, `None`, malformed or unknown id never resolves to a project; it routes the line to the unattributed ledger. AG-1f3 adds one test case per branch for each of those four values.
- Until phase 2 there is no attribution tool. A line stays in the unattributed ledger until that step exists.

## Limits

- This ADR does not specify the phase 2 assignment step, its CLI or its receipt.
- This ADR does not change the door's refusal reasons or the AG-1o counter beyond the call named in Decision 2; those belong to AG-1f1 and AG-1o.
- This ADR changes no other ADR file.
