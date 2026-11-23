# ADR-096 - Dependency per deliverable and file-level blast radius

**Status:** Proposed 2026-11-19 (T0, track `blast-radius-en-afhankelijkheden`, deliverable B0). Builds on ADR-007, ADR-026, ADR-063, ADR-087 and ADR-095 by cross-reference; no existing ADR file is changed (ADR-079 section 83).
**Date:** 2026-11-19
**Decided by:** the operator's request of 2026-11-19 (inbox point `0d361f391838b1ea`: pick up more work in parallel, for example by blast radius); plan v2 `claudedocs/specs/blast-radius-en-afhankelijkheden/plan.md` (not in the repo). v2 carries out the mandatory change of the plan-gate tiebreaker on v1 (`tiebreak:START`, 2026-11-19 13:38Z): one authoritative source for "predecessor merged", per deliverable, before B2 enforces it.
**Cross-refs:** ADR-007 (composite key over `project_id`), ADR-026 (central receipt store), ADR-063 (unknown is never zero), ADR-087 (one configuration surface), ADR-095 (the morning round; same stop-condition seams in the door and `pr_merge.py`).

## Context

The T0 guards order and overlap by hand. That, not the number of builders, is the brake on parallel work. Measured on 2026-11-19:

- Order lives only in plan text. Every plan has a "depends on" column, but the store knows dependencies only per track (`tracks.add_dependency`, `scripts/lib/tracks.py`), not per deliverable. Nothing stops a fire whose predecessor is not yet merged.
- Nobody sees overlap. No check in the door (`scripts/lib/stop_conditions.py`, `dispatch_cli`) or in `pr_merge.py` compares the paths of a new fire with what is already running. A deliverable row and the staged spec do carry `dispatch_paths`.
- Cost: 23 rebase and stacking rounds since 2026-11-01 and 19 docs rounds (research `dlv-herwerk-bronnen-onderzoek`). On 2026-11-19 config PR-1 (#3049) should have waited for G3 (#3046) because both touch the same registry file; only the handoff knew.
- The T0 picks from 55 deliverables on `ready` without a list saying which can safely run side by side.

This ADR is the contract that deliverables B1 to B6 build against. It contains no code.

## Decision

**1. Dependency per deliverable.** A new table `deliverable_dependencies` in `runtime_coordination.db` with columns `(project_id, deliverable_id, depends_on_id, kind, created_at, created_by, removed_at, removed_reason)` and composite PRIMARY KEY `(project_id, deliverable_id, depends_on_id)` (ADR-007). `kind` is `hard` (the fire is refused) or `soft` (reported only). Owner: B1.

**2. Recording and removing.** A dependency is recorded at creation (`vnx deliverable add --depends-on dlv-a,dlv-b`) or later (`vnx deliverable depend <dlv> --on <dlv> [--kind soft]`). It is removed with `vnx deliverable undepend <dlv> --on <dlv> --reason "<why>"`: the row stays, with `removed_at` and `removed_reason` set, so the trail does not disappear. A cycle is refused at `add` and at `depend`. Owner: B1.

**3. Satisfied: one source, per deliverable.** A predecessor `P` is **satisfied** if and only if the project's receipt ledger (`t0_receipts.ndjson` in the central store, ADR-026) holds a line with:

- `event_type = "pr_merged"` and `conclusion = "merged"`;
- `project_id` equal to the project of the dependency;
- `dispatch_id = P`, as `scripts/pr_merge.py --dispatch-id` writes it;
- `branch = "dispatch/P"` as second key: a PR belongs to exactly one deliverable because its branch points at exactly one deliverable id. A follow-up round (`P-r2`, `P-c`) works on the same branch and merges under `--dispatch-id P`, so it counts as `P`.

This is the only source. Not `link-pr` at track level (one track has several deliverables and cannot tie a PR to one of them) and not the dispatch-register event apart from the receipt. Measured 2026-11-19: the receipts of #3050 and #3051 carry exactly these four fields. Owner: B2.

**Amendment to decision 3, 2026-11-21 (OI-3054, operator decision B-20261120-d4aaaffb, option A).** A deliverable shipped in parts on its own branches (`dispatch/P-a`, `dispatch/P-b`, merged under `--dispatch-id P-a`, `P-b`) never matches the rule above. Measured 2026-11-20: N1 (`dlv-fd62af6a1f6f`) shipped as #3118, #3117 and #3119 on `dispatch/dlv-fd62af6a1f6f-a`, `-b` and `-c`, so the door refused its successor N8. The dispatch state is no substitute (PR-5 read `completed` while #3030 was open). The T0 therefore closes `P` with its PR list: `vnx deliverable close P --evidence 3118,3117,3119` writes the ordered list `#3118,#3117,#3119` to the `pr_ref` column of P's own dispatch row, the same column a single-PR close already wrote. `P` is then also satisfied when its row is `completed` or `expired`, `output_kind` is `pr` (or unset), and **every** listed PR has a `pr_merged` receipt (`conclusion = "merged"`, the dependency's `project_id`, matched on the receipt's integer `pr_number`) whose `branch` is `dispatch/P` or `dispatch/P-<part>`, with the part and round suffix stripped the way `blast_radius._round_base` strips it. A listed PR without such a receipt is `deliverable_predecessor_unmerged`, and the refusal names the PR. A listed PR whose receipt branch points at another deliverable is `predecessor_unknown`, naming the PR. So is a list that cannot be parsed or an unreadable ledger line (ADR-063). A deliverable closed without a PR list (`pr_ref` empty) keeps the original rule unchanged, and the original rule alone still satisfies. Owner: B2b.

**4. Exceptions, each explicit.**

- A deliverable without a PR (`output_kind` `doc` or `post`) is satisfied on an `accept` for `P` in `t0_decision_log.jsonl` (`receipt_query.py decide P accept`), and only then.
- A predecessor merged outside the door (old merge, no receipt) is not satisfied. The T0 resolves it with `--override-predecessor "<reason with PR number and merge sha>"` on the fire, or removes the dependency with `undepend`. There is no silent fallback to another source.
- A predecessor that is `cancelled`, `superseded` or `retired` is not satisfied, with its own reason `predecessor_retired`, so the message says what is going on. Resolve with `undepend` (or a dependency on the successor).
- An unreadable ledger or an unknown status is not satisfied, reason `predecessor_unknown` (ADR-063).

**5. Blast radius comes from paths.** The blast radius of a fire is the path set of its staged spec (`dispatch_paths`), normalised to repo-relative POSIX paths: absolute paths inside the repo become relative; `..` and paths outside the repo are refused (message `scope_invalid`); a trailing `/` means directory. Owner: B3.

**6. Generic directories do not count.** `tests`, `changelog.d` and `docs` as a whole directory are ignored. A named file inside them (`tests/test_x.py`) does count. The list is a registry key, `VNX_BLAST_RADIUS_GENERIC_DIRS` (ADR-087), not a constant. Owner: B3.

**7. Incomplete scope.** A spec without paths, or with only generic directories, has scope `incomplete`. That means "unknown", never "no overlap". Owner: B3.

**8. Overlap rule.** Two path sets overlap when they share a file, or when one path is a directory that contains the other. Owner: B3.

**9. Sources of comparison.** (a) The dispatches in `live_work` with status `live` or `starting` (their spec paths), and (b) the project's open PRs (changed files via `gh pr view --json files`). `stale` does not count (zombie). `unmeasured`, or a PR whose files cannot be read, gives "unknown". Owner: B4a.

**10. Self-exclusion.** The candidate is not compared with its own deliverable. Own means the same deliverable id, a follow-up round of it (same branch `dispatch/<id>`), or the open PR on that branch. An open PR or running dispatch of a **declared predecessor** (hard or soft) does not count as overlap either: that order is recorded on purpose and the predecessor rule guards it. Owner: B3 (rule), B4b (application).

**11. PR cache.** Key `(project_id, pr_number, head_sha)`. A different head invalidates the entry. A gh error is not cached and gives "unknown"; there is no second attempt in the same call. Owner: B4a.

**12. Mode keys.** Two registry keys with scope project (ADR-087), `allowed_values` `off|warn|refuse`, each with its own subsystem row in `SUBSYSTEMS.md`: `VNX_BLAST_RADIUS_MODE` (default `warn`) and `VNX_DELIVERABLE_PREDECESSOR_MODE` (default `refuse`, with `warn` and `off` as a rollback without code). Owner: B2, B4b.

**13. Unknown is local to the candidate.** If the door cannot establish the overlap or the predecessor of a candidate, the outcome applies to that candidate only: in `warn` a message, in `refuse` a refusal with reason `*_unknown`. There is no fleet-wide fire stop. Owner: B2, B4b.

**14. Overrides.** `--override-predecessor "<reason>"` and `--override-overlap "<reason>"`. An empty reason is refused. Each override is a line in the ledger, like `--override-main`. An override applies to that one fire. Owner: B2, B4b.

**15. The lock.** Check and claim of the dispatch happen in one serialised section of the door. A claimed dispatch then stands as `starting` in `live_work`, so a second fire in the same second sees it. Owner: B4b.

**16. Measurement rule.** Every hit (warn, refuse, unknown, override) is a line in `state/blast_radius.ndjson`, key `(project_id, ts, dispatch_id, seq)`, with candidate, source, path and outcome. Owner: B4b.

**17. Criterion for `refuse`.** After seven days of `warn`, B6 counts per hit whether the two deliverables later got a rebase, conflict or stacking round (`round_cause` and the round titles). If fewer than 30% of the hits are idle (no round, both merged cleanly), the advice is `refuse`; otherwise first adjust the generic directories or the overlap rule. The operator decides. Owner: B6.

**18. The "fireable now" list.** `vnx deliverable fireable --project-id X` lists the deliverables on `ready` whose every hard predecessor is satisfied and that have no overlap with running work or open PRs. Within that, it greedily picks a mutually overlap-free set, by track priority and then age. Output as table and JSON, with the reason per dropped deliverable (`predecessor_open: dlv-...`, `predecessor_retired`, `overlap: <path> with #<pr>`, `scope_incomplete`). It reads `ready` deliverables across **all tracks of the project**; `--horizon now|next|later` is an optional filter that narrows the list to that band (B5, decided 2026-11-23). Owner: B5.

## Open questions

The plan does not decide these; they are left to the owning deliverable or the operator.

- Whether `--override-predecessor` and `--override-overlap` lines go into an existing ledger file or a new one (the plan only says "a line in the ledger, like `--override-main`").
- The default content of `VNX_BLAST_RADIUS_GENERIC_DIRS` beyond `tests`, `changelog.d` and `docs`.
- How a soft dependency is reported (door message only, or also a ledger line).

## Consequences

- B1 to B6 build against this numbering; a deviation is an amendment to this ADR, not a silent choice in code.
- B2 comes after B0 and B1. Until B2 is merged, nothing enforces predecessors; the T0 keeps guarding them by hand.
- Every branch that clears a predecessor or an overlap decides on positive evidence: a missing, `None`, malformed or unknown field is not satisfied and not "no overlap", and names why. Every build deliverable ships per clearing branch a test with the field missing, `None`, malformed and an unknown value.
- Too coarse an overlap rule would hold back work that can run side by side. Hence `warn` first, generic directories excluded, self-exclusion, and a fixed criterion (decision 17).
- A spec with too narrow `dispatch_paths` hides overlap; the door cannot know what a builder touches extra. The open-PR comparison catches it afterwards, on the real files.
- A hard predecessor that never merges blocks its successors. `predecessor_retired`, `undepend` and the mode key exist for that; each is named in the message.
- The migration touches `runtime_coordination.db` of every project. It is additive and is tested on a copy of the live schema.
- The backlog of dependencies of the open tracks is recorded by the T0 after the merge of B1.

## Boundaries

- No CI path profile (that follows `ci-cost-and-merge-cadence` C5).
- No automatic derivation of dependencies from imports or the test graph (`impact_select` misses the edge via `tests/conftest.py`, OI-2788). Record by hand first and measure.
- No promotion logic; that follows the operator decision of 2026-11-19 (one promote per plan) and ADR-056.
- No backfill of `pr_merged` receipts for old merges; use the override with PR and sha.
- This ADR changes no other ADR file and no code.
