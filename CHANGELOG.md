# Changelog

All notable changes to VNX Orchestration are documented here.

Format: [keep-a-changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [semver](https://semver.org/).

## [Unreleased]

## [1.8.0] - 2026-11-07

This release carries six themes. The dispatch ledger lifecycle of the `audit-spoor-kern` track (ADR-045): start and end lines, a sweep that recovers end intents, readers that decide a `v3` dispatch on its ledger attempt, and `VNX_LEDGER_LIFECYCLE` on by default. The gate engine consolidation (ADR-046): one `GateOutcome`, one `GovernedLane` adapter under `glm_gate` and `kimi_gate`, and obligation outcomes booked through one writer. The per-model cost ledger (`vnx costs report` and `backfill`, the daily paid-lane ceiling). The planning layer with the tracks DB as the only plan source (ADR-047) and `vnx deliverable retarget` and `withdraw`. The fleet rollout tooling: `vnx preflight`, the fleet pin audit, `vnx release verify`, the pinned launchd launcher, `vnx regen-settings --merge` and the T0 context guard at user level. Worker isolation and CI hardening: `git stash` denied as a command on the claude and kimi worker lanes, while the door refuses a build dispatch on the gemini lane (which has no per-command denial) and leaves codex, which has none either, un-refused, the function-size ratchet with its whole-tree measurement, and the Profile A xdist cutover with the `test-suite-kosten` fixes.

Two migrations ship, both on the `deliverables` view of `runtime_coordination.db` and neither with a data change on the way up. `0036` (`scripts/lib/migrations/apply_0036.py`) drops and recreates the view so that `withdrawn` is a terminal `derived_status` with its own `withdrawn_count`; it stamps `user_version` 36. `0037` (`apply_0037.py`) drops and recreates the view again so that a base whose own row ended `expired` and names its delivering round `<id>-rK` in `metadata_json.superseded_by` derives `done` when that round is `completed`; it stamps `user_version` 37. Both build the view from `schema_manifest` and run in one transaction. The down path is `apply_0037.revert_migration(db_path)` (v37 to v36, no row changes, nothing booked) and then `apply_0036.revert_migration(db_path)` (v36 to v35; each `withdrawn` dispatch becomes `expired` with a `deliverable_withdrawn_reverted` event, after a `state_mutation` receipt in the `t0_receipts.ndjson` beside the database). Re-pinning a project from 1.8.0 back to 1.7.6 is not established as safe, and it is not safe to do on a store that already sits at 36 or 37. 1.7.6 knows no `withdrawn` state, and its `schema_manifest` holds one deliverables view for every version up to 35: a store at 36 or 37 carries another view, fails its v35 invariant, and `reconcile_user_version` (called from `migrate_future_system.py`) then lowers `user_version` to the highest version that still holds or raises `SchemaReconciliationError`. I read that path in the v1.7.6 code and did not run it. What is established: revert 0037 and then 0036 with the 1.8.0 tree before the pin goes back, and expect a `superseded` base to read `failed` again after the 0037 revert. After the tag the operator runs `vnx release publish --tag v1.8.0 --set-current`, `vnx release verify v1.8.0`, raises the pins, runs `vnx regen-settings --merge` per project and `reload_plist.sh` per launchd family, and ends on `vnx preflight` with exit 0 (`docs/operations/install-central-runbook.md`, "Before a rollout").

- Golf 1, C4c (track `golf-1-opruimen`): removed the permission-escalation section of `t0_state.json`.
  `build_t0_state.py` lost `_build_permission_escalations`, its module docstring paragraph (schema 2.2 addition
  OI-1414), the call in `build_t0_state`, the `permission_escalations` key in the returned document and its
  `_DETAIL_SECTION_MAP` entry, so `t0_detail/permission_escalations.json` is no longer written. The escalations
  store and the `vnx permission` CLI that write and resolve it are unchanged (C4d); the `terminals` block stays
  (ADR-042: it moves to `live_work` in golf 2). The four `TestBuildPermissionEscalations` tests went with the
  reader. Both retired names are on `scripts/ci/retired_paths.d/dlv-62b0441cc025.txt`.

- Golf 1, C4d (track `golf-1-opruimen`): `vnx permission` is gone entirely. Removed the worker-permission
  relay (`scripts/lib/worker_permission_relay.py`: the escalation store, `_safe_dispatch_id`, `_send_approval`
  and `PROMPT_MARKERS`), the operator CLI `scripts/permission_relay_cli.py` (`escalations`, `approve`, `deny`,
  `resolve-stale`) with its `bin/vnx` arm and usage lines, and the injectable tmux transport
  `scripts/lib/tmux_command_runner.py`, which nothing outside the relay imported. The relay had no living
  caller outside these files: `scripts/check_no_file_derived_data_paths.py` no longer grandfathers it and no
  module writes the escalation store. `docs/operations/WORKER_PERMISSIONS.md` stays, because it describes
  `worker_permissions.py` and the subprocess lane and never covered the relay.
  35 test nodeids went: the whole of `tests/test_worker_permission_relay.py` (`TestEscalationLifecycle`,
  `TestResolveStaleEscalation`, `TestSafeDispatchId`, `TestManualApproveSurfacesSendFailure`,
  `TestResolveStaleCli`) and the relay-parity test
  `tests/test_worker_pane_classifier.py::TestMarkerParity`; `worker_pane_classifier.py` keeps its own
  `PROMPT_MARKERS` until golf 2. `tests/test_runtime_adapter_certification.py` loses only the relay name from
  its two tmux-coupling sets and `tests/test_tmux_adapter_interface.py` only from its allowlist — no test in
  either goes. The escalation records under `~/.vnx-data/*/state/permission_escalations` stay on disk.
  Everything removed is on `scripts/ci/retired_paths.d/dlv-ba9994b6e438.txt`.

- xdist cutover rule (track `ci-profile-a-xdist`, D4): option A, operator decision of 2026-10-22.
  `scripts/ci/xdist_parity_history.py` now classifies a run whose serial reference job failed, or
  in which either Profile A job received a runner shutdown signal, as `not_measurable`: it is set
  aside from every condition of the window (clean, heads, days, wall time) and listed with its
  reason. A shutdown is proven by the runner's own line in the job log
  (`##[error]The runner has received a shutdown signal`), read per job from
  `actions/jobs/<id>/logs` when the check-run annotations do not carry that text themselves; the
  generic `The operation was canceled.` annotation names no cause and is never proof. The
  annotations themselves are read from `check-runs/<check-run id>/annotations`, keyed by the
  check-run id taken from the job's `check_run_url` rather than by the Actions job id; a job row
  without that id is a problem, and a failed annotation read still reads the job log (codex round
  4 on #2184). A failed annotation or log read is never a shutdown, and when both fail the
  problem names both. Only positive evidence sets a run aside: the
  reference job's conclusion, or the verdict's `reference.job_result`, is exactly `failure`, or
  its reference result is present with a whole-number exit other than 0. A `cancelled`,
  `timed_out`, `skipped`, missing or unknown conclusion or job result, and a missing or malformed
  exit, are no cause and never set a run aside (codex round 3 on #2184); a cancelled reference
  job that a runner shutdown cut short is still set aside through the shutdown evidence. A real
  parity difference still blocks, and so does an xdist shadow job that is not known to have
  succeeded or proven shut down: a failed shadow blocks, and an unknown one does too.

- Build roles fail-closed (track `review-stopregel-en-lichte-stoelen`): one shared block, `scripts/lib/prompts/fail_closed_evidence.md`
  (1.1 KB), now reaches every build worker on every lane. Code that reads evidence decides only on positive evidence: missing,
  `None`, malformed or unknown never reads as a pass; match the positive value exactly; fix sibling paths in the same round;
  one blocking test case per missing/`None`/malformed/unknown value. `scripts/lib/fail_closed_evidence.py` appends it through
  `PromptAssembler.assemble` and the legacy `agents/<role>/CLAUDE.md` route of `skill_context`, so `quality-engineer` and
  `system-architect`, which never saw `base_worker.md`, get it too. Exactly once per prompt; reviewer roles get nothing (D1).
  The "Null guards" checklist item of `backend-developer` (prompt role file and both `SKILL.md` copies) is now limited to display
  and formatting code, and `agents/quality-engineer/CLAUDE.md` gained a fail-closed tests section.

- Fail-closed block (OI-2318, track `review-stopregel-en-lichte-stoelen`): the shared rule no longer drops out silently
  when prompt injection fails. Both lane fallbacks — `envelope_prepare._prepare` (headless) and
  `provider_dispatch._enrich_instruction` (provider) — now run a build role's fallback header through
  `fail_closed_evidence.with_block`, so a build worker keeps the rule even when skill injection breaks; a reviewer
  role stays bare (D1). A missing or empty block file raises `FailClosedBlockError` (role, dispatch id and block path
  in the message) instead of being caught into a bare header, before any worker is spawned. The error escapes
  `_try_prompt_assembler`, `_prepare` and `_enrich_instruction`; what happens above them differs per lane. On the
  headless lane it also escapes `run_envelope_headless_plan` (no handler around its `_prepare` call) into the door's
  outer `except Exception`, which prints `REJECT [runtime-error]` and exits 1 — the raise precedes GOVERN, so that
  dispatch gets no envelope receipt. On the provider lane it does not escape `provider_dispatch.main`: that function's
  generic `except Exception` logs it, writes a `deliver_failed` refusal receipt (status failure) and returns 1. The
  legacy CLAUDE.md route is the opposite case: it now applies the block on every return path, so it raises only when
  the block file itself cannot be read. The block test parametrizes over `sorted(BUILD_ROLES)` (14 members,
  asserted) so the five skill-only roles (`api-developer`, `debugger`, `python-optimizer`, `supabase-expert`,
  `performance-profiler`) are covered on both lanes, plus fallback-failure and missing-block cases per lane.

- Review risk class from the diff (R3a, track `review-stopregel-en-lichte-stoelen`): every review
  round now derives one risk class from the PR diff against its merge base
  (`scripts/lib/review_risk_class.py`), through the same path table that makes a track architecture
  work (`smart_router.plan_scope_path_category`). Every category (outside-repo, schema, security,
  gate-merge, store-resolution) is `high`, a spec with `irreversible: true` is `high`, anything else
  is `medium`. The caller's `--risk-class` only raises it; an unreadable diff, an empty file list, a
  path that cannot be normalised, an unknown caller value and an unreadable spec are each `high`.
  The class, the head, the merge base, the rule version and the deciding files are recorded as
  `review_risk` on every seat's request record; the seats are requested with the derived class (so
  the codex reasoning effort follows it), and `vnx gate` prints `RISK CLASS <class>: <why>`.
  `review_risk_class.head_risk_class` reads the class of a merge head from its records and recomputes
  it when the merge base or the rule version differs. The merge door does not use it yet (R3b).
  - The path table is widened: the base schemas under `schemas/` are `schema`, and the gate and merge
    modules (`scripts/gate_*`, `scripts/lib/gate_*`, `scripts/review_gate_manager.py`,
    `scripts/closure_verifier.py`, the xdist parity checker and history, and the other modules that
    write or read a gate verdict or decide a merge) are `gate-merge`. A build dispatch on one of these
    paths now raises its track to architecture, so it gets a plan-gate.

- T0 context trim (track `t0-context-trim`, F2): the fabric-reference skill now names the
  absolute-path convention in Gotchas — every Bash call starts in the session's start folder, so a
  T0 writes from `${VNX_HOME:-$(git rev-parse --show-toplevel)}` or an absolute path, and the start
  folder is not moved to the project root because role-file discovery walks up from cwd — and adds
  two runbooks: staging and firing a dispatch from a script (`stage_spec_bundle` with a `Path`
  `data_dir`, no `--gate` on the stage-only CLI, `pr_id`/`work_ref`/`base_ref` for a follow-up
  round, the door fired in its own tmux window) and context rotation (the 400K/500K/600K bands and
  the hook that enforces them, `/build-log wrap` writing `daily-log/handoff.md` with
  `## Actief /goal`, `scripts/t0_rotate_spawn.sh`, and a handed-over `/goal` longer than about 650
  characters arriving as a pasted block that is not executed — OI-2314). The shipped
  `skills/fabric-reference/SKILL.md` and the checked-in `.claude/skills/fabric-reference/SKILL.md`
  are byte-identical again; the latter was one line behind on the `VNX_STATE_DIR` pin.

- The merge door acts on the review risk class (R3b, track `review-stopregel-en-lichte-stoelen`;
  operator decision 2026-10-17, ADR-008 amendment 2026-10-23): at `low` and `medium` only the first
  seat of the review stack is required, at `high` and `critical` every seat, each with a PASS on the
  head being merged. One function decides it (`review_seat_evaluation.seats_for_risk`), and the class
  is looked up inside the shared seat evaluation, so the merge door, `vnx-gate/review`,
  `vnx pr-ready` and track closure read the same class. The door fetches the PR head and the PR's
  own base branch before it reads the diff (`review_risk_class.merge_head_risk_class`); a failed
  fetch, an unreadable diff and a checkout that cannot be established are `high`. No flag lowers the
  class. A seat list the caller names keeps all its seats.
  - The class is read against the base the PR names (`baseRefName`), never `origin/main` by
    default. A PR for `release` that carries merge logic already on `main` plus a doc change read as
    medium against `origin/main` and merged on its first seat alone; against `origin/release` it is
    high. The door, `vnx pr-ready` and `vnx-gate/review` each pass the base they read from the PR,
    the request side looks it up the same way, and the merge record carries it as `base_branch`
    (also after `--override-reason`, for track closure). A base that is missing, no plain branch
    name or cannot be fetched is `high`. A request record read against another base than the
    merge's is recomputed.
  - Refusals, `vnx gate` and the `pr_merged` record name the class, its reason and the required
    seats. The record's rule is now `first_seat_or_every_seat_by_risk` and carries `stack`,
    `required_seats`, `risk_class`, `risk_reason`, `risk_source` and `risk`. A merge recorded under
    `every_seat_distinct_reviewer` keeps being judged by that rule at track closure.
  - Track closure demands what the door demanded. A merge recorded under the new rule is closed on
    its `required_seats`: a stack seat its class did not require (kimi at `medium`) gets no per-gate
    check, so a medium PR merged on codex alone no longer fails closure on "no kimi_gate result
    found". A FAIL by that seat on the merged head still fails closure, as it refuses the door; a
    PASS, or a record on another head, changes nothing. The check `review_seats_not_required` names
    the class and the seats that were left out. A record whose `stack`, `seats`, `required_seats`
    and `risk_class` do not agree leaves nothing out.
  - The receipt conversion is `gate-merge` in the path table and so high (operator decision
    2026-10-23): `report_to_receipt_converter.py`, `append_receipt_internals/`, `append_receipt.py`,
    `report_body_contract.py`, `receipt_outcome.py`, `receipt_verdict.py`, `scripts/lib/receipt_processor/`
    and `scripts/receipt_processor.sh`. A track that touches one of them now also needs a plan-gate.
    The dispatch door, the lanes, `scripts/lib/providers/` and the fleet defaults stay medium.
  - The obligation runner does not request the second seat yet; the T0 requests it by hand after a
    first-seat PASS and CI `success` on a high head.

- Review gates, GE-1 (track `gate-engine-consolidation`): every `review_gate_result` receipt now carries
  `commit_sha`, the head the verdict is about (ADR-045 point 4), with `head_sha` kept as its equal-valued
  alias for the round ledger. `gate_recorder._emit_terminal_result_receipt` and both `gate_result_parser`
  emit sites (`record_result`, `record_claude_github_result`) stamp it from the payload or contract request
  they already hold; a path with no head stamps neither key rather than an empty string. The receipt
  idempotency key already had a `commit_sha` dimension (PR #1767), so two verdicts for the same gate+PR on
  two heads now book two receipts instead of deduping into one. No key, result-record, obligation or reader
  changed.

- Review gates (track `gate-engine-consolidation`, GE-0): ADR-046 records one gate engine. `GateOutcome` has four statuses (`pass`, `fail`, `unavailable`, `not_executable`), because an outage is absence and not a vote against (ADR-037). `commit_gate_outcome` becomes the only writer of the result record, the request, the `review_gate_result` receipt and the obligation, under one slot per PR and gate; the ADR also fixes the partial-failure contract (`receipt_pending`, atomic record first, attempt identity) and names three adapter kinds plus `claude_gate`'s own transport. The seam with ADR-045 is written into the decision: this track owns when and what is recorded, ADR-045 owns what every ledger line carries. Docs only; GE-1 to GE-6 follow.

- Gate engine (GE-2a): `scripts/codex_final_gate.py` now carries only `enforce_codex_gate`,
  the helpers it calls, and the prompt renderer that GE-2b removes. The final-gate receipt
  (`CodexFinalGateReceipt`, `evaluate_and_record`, `_persist_result`), the clearance check
  (`check_gate_clearance`) and the CLI (`main`, the `_cmd_*` arms) are gone — nothing outside
  tests imported or ran them, and the module's one production caller
  (`scripts/closure_verifier.py`) imports `enforce_codex_gate` only. 826 lines became 435;
  the removed symbols are listed in `scripts/ci/retired_paths.d/dlv-73cd853f646d.txt`.

- xdist cutover rule (track `ci-profile-a-xdist`, D3-9): the throwaway git repos the tests build no longer spawn a
  detached `git maintenance run --auto --quiet --detach` child. `git commit` ends with `run_auto_maintenance()`,
  which starts that child unless `maintenance.auto` is false or `gc.auto` is 0; the child creates and removes
  `.git/objects/maintenance.lock` a few milliseconds later, and a `TemporaryDirectory` deleting the repo inside
  that window failed VNX CI run 39971696706 with `OSError: [Errno 39] Directory not empty: 'objects'`
  (`tests/test_attest_d2.py::TestDiffBinding::test_diff_bind_forged_manifest_fails`, serial Profile A only), which
  blocked the D4 cutover rule. The cleanup failure is not papered over (no `ignore_cleanup_errors`, no retry):
  the new shared helper `tests/git_maintenance_guard.py` writes both settings into a repo's own config, and
  `tests/conftest.py` injects them once per session as git command-scope config, so every repo-creating helper in
  `tests/` — including helpers added later, and the production git commands these tests drive — never starts the
  child. `tests/test_git_maintenance_guard.py` pins the helper, its fail-closed reader and the absence of the
  spawned child in git's own trace.

- Planning (track `retire-feature-plan-second-planning-layer`): ADR-047 records that the tracks DB is the only plan source. `FEATURE_PLAN.md`, `PR_QUEUE.md` and `ROADMAP.md` are retired as inputs; `ROADMAP.yaml` becomes an example that no script reads from a fixed path, and `seed_tracks_from_roadmap.py` only runs with an explicit `--file` (C2). The ADR writes the replacement semantics for per-PR closure (A5), whole-feature closure (A2 and B2), queue reconciliation (A2), deliverable-to-dispatch aggregation (C1) and the review contract (B4), names every default `ROADMAP.yaml` reader and every automatic seeder caller, and corrects two orderings: A6 waits for B2's last caller, B4 waits for the last `PR_QUEUE.md` writer, and A4 also removes the Profile C closure step, which makes A4 high risk. The queue truth contract carries a status block pointing at the PR that removes each file's last reader. Docs only; A1 to C2 follow.

- Golf 1, C2 (track `golf-1-opruimen`): removed the three `tmux_signal_*` hooks of the retired tmux-spawn lane
  (`tmux_signal_prompt_received.sh`, `tmux_signal_session_ready.sh`, `tmux_signal_stop_receipt.sh`), the
  tool-call signal tails of `pretooluse_block_raw_claude_spawn.sh` and `pretooluse_block_subagent.sh`, and the
  `VNX_DISPATCH_ID` guards of `session_reconcile_autoclose.sh` and `session_reconcile_cleanup.sh`. Nothing exports
  `VNX_TMUX_SIGNAL_DIR` or sets `VNX_DISPATCH_ID` in a headless dispatch, so none of them ran. The hooks stay
  registered in a project's `.claude/settings.json` until its SessionStart, UserPromptSubmit and Stop entries for
  `tmux_signal_*` are removed by hand; a project that pins this release must remove them, or every prompt reports
  a hook error. `VNX_TMUX_SIGNAL_DIR` itself stays: `enrichment.py`, `envelope_govern.py` and `provider_dispatch.py`
  still read it. The four removed paths are guarded by `scripts/ci/retired_paths.d/dlv-209d2bcb21d2.txt`.

- Planning layer (track `retire-feature-plan-second-planning-layer`, A4): removed the three modules that existed only for the FEATURE_PLAN/PR_QUEUE layer — `scripts/verify_closure.py` (its six checks parsed FEATURE_PLAN.md/PR_QUEUE.md and ran on an empty PR list), `scripts/validate_feature_plan.py` and `scripts/review_contract_materializer.py` — together with the Profile C CI step `Run closure verification` that invoked the first one. The tests that went through the materializer are gone and the two skill reference lists no longer point at the retired validator. `scripts/lib/review_contract.py` itself stays.

- Gate engine (GE-2b): `scripts/codex_final_gate.py` no longer carries the Codex prompt
  renderer. `render_codex_prompt` and its four `_format_*` helpers are gone; only tests
  called them, and no script, hook or workflow did. The module keeps `enforce_codex_gate`
  and the helpers it calls, which is all `scripts/closure_verifier.py` imports. 435 lines
  became 248; the removed symbol is listed in
  `scripts/ci/retired_paths.d/dlv-8c5b32ca31ad.txt`.

- De state-build (`scripts/build_t0_state.py`) herschrijft `FEATURE_PLAN.md` niet meer: elke build liet dat bestand in de project-root achter, terwijl geen enkele parser er nog uit las.

- Receipts (track `deterministische-testverificatie`, PR-0): ADR-035 gains an amendment for machine-measured test evidence, before the track's code lands. `verification{}` gains three optional keys, all absent while `VNX_JUNIT_COUNTS` is off: `count_source` (`junit_worker` from the builder's JUnit XML, `report_text` as today), `count_mismatch` (the XML and the report text disagree, so failed is the higher of the two and passed comes from the XML) and `junit_rejected` (why a present XML was refused). The amendment also states the trust level (builder evidence, not independent evidence), the CI red-on-base verdict set (`behavior`, `not_behavior`, `green_on_base`, `fails_on_head`, `skipped`, `incomplete`; only `behavior` satisfies a red-run claim), the one setting that switches both receipt write paths together, and that `compute_verdict` does not read the new keys yet. `docs/core/11_RECEIPT_FORMAT.md` documents the same shape and rules. Docs only; the code follows.

- Path resolution (track `vnx-init-store-path-parity`, PR-1): the inline fallback
  resolver in `bin/vnx` no longer ends in an XDG data dir. A fresh install now
  resolves to the central per-project store (`~/.vnx-data/<project-id>`), the same
  answer as `_vnx_resolve_state_root` in `scripts/lib/vnx_paths.sh` and
  `_resolve_state_root` in `scripts/lib/vnx_paths.py`. The XDG path stayed behind
  when #1437 moved those two copies, so a `bin/vnx` with no shell lib beside it
  put state where the data-dir guard refuses it (OI-1055). A new branch-parity
  test slices the real inline block out of `bin/vnx` and compares it with the bash
  resolver for every branch, and a new static guard fails any tracked production
  file under `bin/`, `scripts/`, `vnx_cli/` or `hooks/` that builds a store path
  from `XDG_DATA_HOME` or `.local/share/vnx`.

- Receipt test counts (track `deterministische-testverificatie`, PR-4): `verification_runs.from_junit`
  reads the JUnit XML a builder leaves beside its report (`<report without .md>.junit.xml`, one helper
  `junit_sidecar_path`) and counts `tests`, `failures + errors` and `skipped` over every `testsuite`;
  a missing file, an unparseable file, a `name` that is not the dispatch id, or a suite without a
  whole-number count is refused with a reason and no counts. The new setting `VNX_JUNIT_COUNTS`
  (off by default) makes Path 2 (`report_parser._build_enhanced_receipt`) prefer those counts:
  `verification{}` then carries `count_source` (`junit_worker`, or `report_text` when the sidecar is
  missing/refused, with `junit_rejected` naming the reason), and a sidecar that disagrees with the
  text keeps the higher `tests_failed` with `count_mismatch: true`, so a documented failure is never
  erased. With the setting off the receipt is byte-for-byte today's; PR-5 wires the envelope path.

- Test selection (track deterministische-testverificatie): `classify_ci_profile.py --tests-only` prints the pytest files a PR adds or changes as JSON (`selected`, `excluded_touched`, `deleted`, `conftest_changed`), read from the Profile A prelude's changed-paths file and `test_exclusions.txt`. Input it cannot read is refused with exit 2 instead of reporting a smaller selection. The profile classification is byte-identical.

- Worktree inventory (worktree-opruiming-structureel PR-1): a new read-only
  `scripts/lib/worktree_inventory.py` records every worktree git knows about — dispatch,
  gate, pool, bench and external — with its lock state, dirty-path count, commits no
  `origin/*` ref reaches and age in days. `cleanup_reviewed_worktrees.py --all` prints it
  (table, or `--json`) and evaluates, locks and releases nothing; the default run is
  byte-identical to before.

- Planning layer (track `retire-feature-plan-second-planning-layer`, C1): `vnx horizon queue` (alias `vnx objective queue`) replaces the FEATURE_PLAN.md/PR_QUEUE.md overview with a read-only command over the tracks DB. One row per deliverable: track, deliverable id, state (the deliverables VIEW's derived_status), the delivering PR from `dispatches.pr_ref` of the deliverable or any `-r<n>` round, and its track's `track_dependencies` edges with kind; `--json` emits the same fields with `pr` as an int or null. Done/closed/cancelled rows are hidden unless `--all`. No schema change and no `gh` call.
- `vnx horizon queue` / `vnx objective queue` are now exposed through the pip CLI (`vnx_cli`) as well as `bin/vnx`: the same `--objective`/`--all`/`--json` flags delegate to `planning_cli.cmd_objective_queue`, closing the engine↔CLI parity gap the parity suite caught.

- Feature planning (track `retire-feature-plan-second-planning-layer`, B1): the tracks DB is the only plan source (ADR-047), so `scripts/lib/auto_gate_trigger.py` and `scripts/lib/feature_state_machine.py` are gone, together with `HeadlessOrchestrator._check_feature_completion_and_trigger`. The trigger parsed `FEATURE_PLAN.md` for section shapes the generated plan never had, and its feature id came from an `f<n>-pr` dispatch-id shape that no current dispatch id matches, so it never fired on real input.

- `vnx doctor` (track `vnx-fleet-pin-audit`): a consumer linked to an older
  release dir inside the same central store (`versions/v1.7.5` while
  `current` -> `versions/v1.7.6`) is reported as a WARN `lagging pin: linked
  to v1.7.5, current is v1.7.6 (not a dual install)` instead of a `dual
  install conflict` FAIL that advised removing the working link. A real
  embedded install and a link outside the store still FAIL.

- Pin audit (track `vnx-fleet-pin-audit`, PR-3): new read-only `scripts/lib/pin_drift.py` answers how far a project's `.vnx-version` pin sits behind the store's `current` — `current`, `lagging` with the number of releases in between, `non-release` (a rolling `edge`/`latest` or non-version dir), or `dead` (no such dir). The store is read in semver order with `.vnx-data` and rolling dirs excluded and a `-rc9` pre-release sorting before its release; `current` is read from the symlink target, never inferred from the highest dir. When `current` does not resolve to a release the distance is `None`, never `0`. No caller and no CLI in this PR.

- Plan gates (track `worktree-opruiming-structureel`, PR-3): a provider-lane gate seat can no longer outlive its gate. `plan_gate_panel` now runs the `provider_dispatch.py` seat inside `dispatch_worktree_isolation.reap_child_worktree_on_exit`, which on exit calls the new `remove_dispatch_worktree_if_present`, so a seat killed for exceeding its timeout (SIGKILL — its own `finally` never runs) no longer leaks a `dispatch-*-gate-*` worktree under `.vnx-data/worktrees/`. Measured 2026-10-24: 11 such trees left on disk.
  - The reap resolves the project root with the same `resolve_consumer_project_root()` the child used (never a hard-coded path), makes no git call when the tree is absent, and reuses `remove_dispatch_worktree`'s `review_only` reset — which still fires only while `HEAD` equals the recorded base, so a real local commit's branch is kept, never discarded.
  - It never raises: a failing teardown is logged with the dispatch id and path and swallowed, so the caller's `TimeoutExpired` propagates unchanged.
  - The reap never touches a live sibling's tree. Whether the worktree path exists is recorded before the child runs, and a tree that existed before is never reaped. A tree that appeared during the run is reaped only while the parent holds its occupancy lock exclusively (non-blocking; a live holder means skip), held through the whole teardown and released in a `finally`. The reap also demands positive evidence: the lock file and a claim naming this dispatch for exactly this path, under the child's registry. Before this, a duplicate seat that exited on `WorktreeOccupied` could reset, kill and remove the original worker's tree.
  - The reap reads the claim registry and the occupancy lock under the data dir the dispatcher pinned into the child (`VNX_DATA_DIR`), not the parent's own environment. `remove_dispatch_worktree` takes a new optional `data_dir` for that and now returns the classification it reaped under (None when the tree was absent).
  - Every reap decision is logged at INFO with dispatch id and path: the classification, or the reason it was skipped. The existing `provider_teardown_worktree` event now fires for a parent-side reap, unchanged in shape, under the terminal `plan-gate-parent` in `<data_dir>/events/`.

- `vnx doctor` (track `vnx-init-store-path-parity`): the `store:legacy-xdg` check names a pre-#1437 store under `${XDG_DATA_HOME:-~/.local/share}/vnx/<project_id>` when it is still the project's only copy, so an operator who upgraded learns the state there is not read any more. A legacy copy that coexists with the central `~/.vnx-data/<id>` store, an empty skeleton, and a project with no legacy store are all PASS. Read-only: nothing is moved, rewritten or deleted; `vnx migrate --relocate-legacy` lands in a later PR.
- `vnx doctor` (track `vnx-init-store-path-parity`, round 2): the same PR now carries `tests/test_no_xdg_store_defaults.py`
  onto its merged tree — the static guard that flags any production line building a VNX store path from XDG. The
  read-only legacy detector `scripts/lib/legacy_store_path.py` is its single allowlist entry (it stats one directory and
  opens no store); the `store:legacy-xdg` docstring in `vnx_cli/commands/doctor.py` names the pre-#1437 XDG data-home
  store through `legacy_store_path` instead of spelling the path out, so the doctor file stays scanned rather than
  allowlisted. No behaviour change: the check reports exactly what it reported before.

- Receipt test counts (track `deterministische-testverificatie`, PR-5): the envelope write path now reads the builder's JUnit sidecar with the same rule as the receipt write path. The sidecar decision lives in one function both paths call (`verification_runs.verification_record_for_report`), so they cannot drift, and `VNX_JUNIT_COUNTS` now defaults to on for both at once — the single switch is also the rollback. The Worker Dispatch Standards footer asks the builder to run its targeted pytest with `--junitxml=<report without .md>.junit.xml -o junit_suite_name=<dispatch_id>` and leave that file beside the report.

- Review gates, GE-3 (track `gate-engine-consolidation`): the overwrite guard now sees every module that writes a
  `review_gates/results/` record. `gate_result_parser.record_claude_github_result`,
  `gate_result_parser._validate_and_persist_result` (the `record-result` CLI) and
  `gate_request_handler._persist_claude_github_files` wrote their record with a bare `write_text`; all three now
  route through `gate_recorder.write_result_guarded` (slot lock, overwrite guard, atomic write), keeping each
  record's path and content shape. A `record-result` for the same PR, gate and head that would downgrade a
  decided, evidenced verdict is refused instead of erasing it, and books no receipt; on a new head it lands as
  before. `tests/test_oi1472_residual_guard_writers.py` now opens both modules, so a future raw writer fails CI.
  Routing must not book one outcome twice: `write_result_guarded` gained `emit_outcome_receipt=False`, used by
  the two callers that already emit their own richer, contract-linked `review_gate_result` receipt — that
  gate-specific receipt stays, because it carries findings and (for the Claude GitHub path) the head read off
  the contract request, which the result record itself does not carry.
- GE-3 follow-up (same track): `tests/test_w3j_followup_cleanups.py` no longer treats the guarded writer's
  empty `.{name}.lock` sidecar as a result record. The record glob pins the `.json` suffix and asserts exactly
  one match, and a regression test proves the slot-lock sidecar can never be read as the record (VNX CI red
  twice on Linux, where ext4 listed the sidecar first and `json.loads('')` raised `JSONDecodeError`).

- `vnx providers` (track `vnx-fleet-pin-audit`, PR-1): `engine_root_for` no longer reports a dead `<project>/.claude/vnx-system` link as an engine. A symlink whose target is missing or is not a directory now returns `(None, "dead .claude/vnx-system link -> <target> (target missing|target not a directory)")`, so a project pinned to a reaped `~/.vnx-system/versions/<tag>` is listed with an unresolved engine root instead of a provider list read from a directory that does not exist. Live links, the fabric-checkout branch and the "no link" branch are unchanged; a project row with no readable engine now renders "engine root unresolved (<reason>)".

- Pin audit (track `vnx-fleet-pin-audit`, PR-6): `pin_drift.mislabeled(store_root)` names every release
  dir whose `VERSION` file disagrees with the dir name (`v1.0.1` holding `1.0.0`), and
  `pin_drift.writable(store_root)` names every release dir whose write bit is still set -- a WARN, not a
  broken store, since an operator may unlock one on purpose for a repair. A dir with no readable
  `VERSION` is never guessed into a mislabel: `unlabeled` reports it apart. All three are pure reads that
  share `load_store`'s release-dir skip list (dot-dirs, rolling dirs, non-dirs, non-version names), and
  the read-only test is `vnx_version_ro.is_readonly`, imported rather than copied. No CLI in this PR
  (PR-7 wires them into the audit); on today's store all three report nothing.

- Fleet pin audit (track `vnx-fleet-pin-audit`): `scripts/fleet_pin_audit.py` gives one read-only verdict per registered project over both engine surfaces — the `.vnx-version` pin (`pin_drift` distance) and the `.claude/vnx-system` link — plus the checkout lag. `dead` means the pinned release is absent or the link dangles, which `vnx doctor` reported as `install:mode PASS` for `mc-wt-seclock`; `lagging`, `pin-without-link` and `link-without-pin` are WARN lines and keep exit 0, so a deliberate pin never turns the fleet red. `--json` emits the same rows; `--project-dir` audits one project without the registry; `--store-root` points the store at a tmp tree in tests. Nothing is written.

- Dispatch fabric (track `refactor-skill-en-levering`, RS-6): ADR-048 records that a refactoring method is a skill bound in the dispatch spec, not a new role. One seam — the already-declared `skill` field on the spec, written by `stage_spec_bundle` and injected through `skill_context` before the role block on every lane — replaces a role register that would have cost four files. The receipt contract is fixed as a measurement: `skill_applied` is `true` only when the bytes of the resolved `SKILL.md` are literally in the prompt the lane handed the worker, `false` with a named reason otherwise (`resolution_failed: …`, `reviewer_bound`, `lane_without_seam`), and `role_applied` is deliberately not reused for it. The ADR also records that the dead `.claude/skills/<role>/CLAUDE.md` tier is removed rather than repaired (0 of 38,756 receipts ever resolved it), that `.agents/` stays generated scaffolding, and the rollback order RS-3 → RS-2 → RS-1. `docs/core/DISPATCH_RULES.md` gains §15 as the operating half. Docs only; RS-1 to RS-5 follow.

- Refactor-safe skill (track `refactor-skill-en-levering`, RS-5): a behaviour-preserving refactor contract shipped as `skills/refactor-safe/SKILL.md` plus the byte-identical `.claude/skills/` copy and a `skills/skills.yaml` entry. It requires characterization before any move (import surface, patch binding, path resolution, output shape), lists the eleven seams where a move silently changes behaviour, and carries the two rules this repo learned the hard way: patch the consumer namespace (and assert the mock was called), and treat a partial test sweep as a different environment. Docs only.

- Dispatch skills (track `refactor-skill-en-levering`, RS-1): a staged spec can now carry a skill. `dispatch_bridge.stage_spec_bundle(..., skill=...)` writes the verified slug to `dispatch-spec.json` under `skill` (null when no skill is named — every other field is unchanged), and refuses before writing anything when the name is malformed (`ValueError`) or exists in neither `<project_root>/skills/<name>/SKILL.md` nor `<project_root>/.claude/skills/<name>/SKILL.md` (`UnknownSkillError`, both searched paths in the message). `dispatch_spec.validate()` Rule 17 checks the slug format only (the id convention plus a ban on `..`); existence stays a staging/compile-time rule. `DispatchSpec.skill` was already declared and parsed — nothing could write it before this.

- Refactor census (track `slop-refactor-panel-review`, S1): `scripts/lib/refactor_census.py` is one repeatable count of long functions (raw span plus the CI ratchet's executable unit), exact duplicates, doubled step banners, version-suffixed paths, defensive markers, dead module functions and restating comments. It reads a commit through `git ls-tree`/`git show` so a number cited by a later refactor PR can be reproduced without checking anything out. It reports and ranks; it does not gate.

- Planning (track `gate-weight-and-worker-posture-cluster`, point 11): `vnx objective list --count-blocked` prints one repeatable number of blocked tracks — every track whose stored `derived_status` is `blocked` over ALL tracks, the same definition as the 14-08 baseline of 76 — and splits it once per track as `plan-gate` (open `OI-PLAN-*` blocker) > `dependency` (unmet hard dependency) > `other`. `--json` lists every reason per track, so overlap stays visible. Read-only: it reuses `track_reconciler`'s blocking detail and writes nothing.

- Plan-gate (track `gate-weight-and-worker-posture-cluster`, PR-B; ADR-030 amendment 2026-10-25): a plan whose dispatch paths fall in an irreversible category now gets both plan-gate seats from its first round, quorum 2, as schema, security and outside-repo paths already did. The categories are `schema-migration`, `receipt-format` (`append_receipt_internals/`, `ndjson_hash_chain.py`, `ndjson_io.py`, `receipt_schema.py`) and `fleet-default` (`agents/`, `skills/`, `.claude/skills/`, `.claude/terminals/`), named in that order after schema and security. The new public `smart_router.irreversible_path_category` normalises the path first, so `.claude/skills/` keeps its dot and an absolute path is read relative to the repository. A plan that touches only `docs/` keeps one seat, and `--panel-seats` still runs exactly the seats the operator names.

- Planning layer (track `retire-feature-plan-second-planning-layer`, A2, ADR-047 §3 and §4): the PR queue and the whole-feature closure no longer read `FEATURE_PLAN.md` or `PR_QUEUE.md`. `QueueReconciler` takes no plan: a PR is a queue row when a dispatch names it, so the "not in FEATURE_PLAN.md" drift warning is gone and a PR has no dependencies of its own. `scripts/reconcile_queue_state.py` has no `--feature-plan`, no exit 2 on a missing plan and no longer writes `PR_QUEUE.md`; the JSON/YAML projection stays a cache. `closure_verifier.verify_closure` reads the feature as a track from the tracks DB (`--track-id`, else the track of the dispatch behind a `dispatch/<id>` branch): `feature_plan_status` became `track_derived_status` (`tracks.derived_status = 'done'`), `pr_queue_complete` became `track_prs_delivered` (every PR in `tracks.pr_ref` merged and no delivery hold, naming the PR that is not done), and `metadata_sync` is gone. A track that cannot be read fails both checks. `closure_verifier.py` lost `--feature-plan` and `--pr-queue`.

- Worker permissions (track `gate-weight-and-worker-posture-cluster`, PR-A): the subprocess adapter's import-fault fallback now carries the full scoped code-worker posture — `acceptEdits`, the empty `--mcp-config` with `--strict-mcp-config`, the code-worker allow-list and the `mcp__*` namespace deny — and never the legacy skip flag, so a failed `worker_permissions` import can no longer silently reopen `--dangerously-skip-permissions`. `requires_mcp` also travels the claude-headless lane: `EnvelopeSpec` carries it from the plan, and a `Requires-MCP` dispatch keeps its ambient MCP config instead of being force-emptied.

- Planning (track `deliverable-retarget-withdraw`, PR-0): ADR-049 records that a deliverable is never closed on a PR that did not deliver it. `vnx deliverable close --evidence` stays, and stays only for work a PR actually shipped. The two missing outcomes get their own verbs and one rule: `vnx deliverable retarget` moves a `proposed` or `ready` deliverable to another track in the same project (never a `done` target, `parked` only with an explicit flag, a `ready` row returns to `proposed` because the approval belonged to the old track) under a compare-and-swap in one transaction with a `deliverable_retargeted` event, and no retarget exists without that event; `vnx deliverable withdraw` gives a deliverable the terminal state `withdrawn` instead of `expired`, because withdrawing is not a failure. `withdrawn` joins the one canonical `coordination_db.TERMINAL_DISPATCH_STATES`, the loose copies in `track_reconciler.py` and `runtime_supervisor.py` read that set, and the deliverables view returns `withdrawn` as its own `derived_status` so no reader counts a withdrawn deliverable as open, planned, delivered or failed. The ADR names the nine readers PR-2 walks, requires `done` to rest on positive delivery evidence rather than terminal-ness, and fixes the rollback: migration 0036's down migration sets every `withdrawn` row to `expired` with a `deliverable_withdrawn_reverted` event. The reason is measured: 21 of the 26 deliverables on `retire-redundant-architecture` are `proposed` and none was delivered (OI-2325). Docs only; PR-1 and PR-2 follow.

- Fleet pin audit (track `vnx-fleet-pin-audit`, PR-5): `fleet_pin_audit.py --scan-root PATH` (repeatable) walks the disk with the same `_iter_pin_files` walk `vnx update` prunes against and adds one row per pin directory the registry does not cover. A git worktree of a registered project keeps its own pin verdict and names its `owner`; a stranger directory is the new `unregistered` verdict (LEVEL `FIND`, next to `dead`, exit 1) with the pin judgment kept in `pin_verdict`. An unlistable scan root exits 2. Without `--scan-root` the report is byte-for-byte unchanged.

- Refactor (track `slop-refactor-panel-review`): close-time revalidation moves out of `close_track_if_done` into `scripts/lib/track_reconciler_revalidation.py::revalidate_close_evidence`, unchanged and still before any DB write. The function drops from 349 to 165 raw lines; the nine near-identical rejection payloads share one `_rejection` helper. The 26 close tests and the new raw-line target test pin the behaviour and the size.

- Reconcile (slop-refactor S4): the derived refresh, the dispatch-outcome recompute and the nomination sweep leave `run_reconcile` for `scripts/lib/objective_reconcile_steps.py` (`refresh_derived`, `recompute_dispatch_outcomes`, `nominate_candidates`). Behaviour is unchanged, including the error-summary exit 2, the best-effort classifier and the three fail-closed nomination guards. `run_reconcile` loses its four doubled step banners (one banner per step now), and the moved lines carry plain marker comments instead of `# noqa:`.

- Feature planning (track `retire-feature-plan-second-planning-layer`, A3): `scripts/kickoff_preflight.py` and its tests are gone, and `scripts/routing_preflight.py` no longer reads a plan file. Readiness now comes only from an explicit `--terminal` with `--provider`/`--model`, from `--dispatch`, or from `--check-pinned`; `--feature-plan`, `--pr-id` and `extract_requirements_from_feature_plan` are removed, and a call with no requirement source reports that and exits 2 instead of reporting a vacuous ready. The plan parser behind it understood section shapes the ADR-047 generated plan never has, so it always returned an empty list.

- Review gates (track `gate-engine-consolidation`, GE-4a, ADR-046): `scripts/lib/gate_outcome.py` adds `GateOutcome`
  (one of `pass`, `fail`, `unavailable`, `not_executable`) and `commit_gate_outcome`, the one writer of a gate's
  result record and its `review_gate_result` receipt. Under the slot lock it writes the record atomically, then the
  receipt. A failed receipt append leaves the record with `receipt_pending: true`, and the next commit on that slot
  books it once through the existing idempotency key. If that booking fails again, the commit is refused
  (`ReceiptPendingRefused`, a `ResultOverwriteRefused`) and the pending record stays byte for byte, so its
  receipt can never be lost to a later head. Each landed record carries `attempt_seq`, counting up within the
  slot; repeating the decided outcome already on the slot (same `commit_sha`, status and pass/fail decision) is a
  no-op that returns the stored record, without a new attempt or receipt. `record_terminal_result` and
  `record_not_executable` route through it with unchanged signatures and return values; `record_terminal_result`
  now refuses an in-flight status (`pending`, `running`) before it writes.

- Plan-gate panel: `load_panel_seats` refuses two seats from one provider family
  at load time, naming both seats, both provider strings and the family. The
  family is the wave7 registry key the constraint enforcer already assigns
  (ADR-036), with the lane-only `_harness` suffix folded away, so
  `deepseek-harness` and `litellm:deepseek` count as one family and
  `glm-harness`/`litellm:zai` as another. The registry splits one vendor over
  two lane keys, so the lane key `kimi_cli` (`provider: kimi`) also folds to its
  vendor key `moonshot`, seating `kimi` and `litellm:moonshot` as one family.
  `default_panel()` holds the same rule, the shipped five-seat config still
  loads, and an exhaustive provider-family table test makes a new provider
  member a deliberate placement.

- T0 context guard (OI-2119 (d)): rule 1 now follows the session's project. The guard and
  `scripts/hooks/vnx_hook_shim.sh` resolve it with one order and one set of exclusions — the
  cwd first, and only when that yields no rule-1 root `CLAUDE_PROJECT_DIR` (Claude Code sets
  it to the folder the session started in). A T0 whose shell drifted to `$HOME` (3 of 143
  sessions measured) no longer loses the guard at 520K tokens, and the shim no longer stops a
  run the guard would have made. A candidate under `.vnx-data/worktrees/` still yields
  nothing and stops the resolution, even when `CLAUDE_PROJECT_DIR` names the main checkout.
  `hook_coverage.session_project_root` is the one Python implementation; the shim's
  `rule1_root` takes the same two steps.

- T0 guard (track `t0-guard-gebruikersniveau`, PR-B): `vnx guard check --require-user` makes the
  user level the only registration that counts. For every project that passes rule 1, an event
  answered by the project's own settings fails as `covered by the project only, not at user
  level`, named per project and per event; without the flag the report and the exit code are
  unchanged. The report also says what it did not see: without `--scan-root` it prints
  `scope: registry only (N projects); pass --scan-root DIR to include unregistered projects`,
  and with `--scan-root` that line names the roots. A scan now also finds a folder holding
  `.claude/terminals/T0/CLAUDE.md` without a `.vnx-project-id`; such a folder is listed as
  `not guardable: no .vnx-project-id`, with one closing line that counts them, and it never
  fails the check (rule 1 itself is unchanged). The `check` help documents the exit codes:
  0 when every rule-1 project passes the active rule, 1 when any project fails it, 2 for usage.

- Track closure (track `slop-refactor-panel-review`, S3): `close_track_if_done` in
  `scripts/lib/track_reconciler_closure.py` hands its three close gates to `_terminal_gate` and its
  phase walk to `_walk_to_done`, with the order of checks and every value written to the returned
  payload unchanged. The function drops from 165 to 147 raw lines and from 77 to 62 executable
  lines; two seam tests pin each gate's full payload and the walk's `path`/`declared_phase`/`error`
  contract, and `test_close_track_if_done_after_s3` holds the 150/70 ratchet.

- `closure_verifier.py --pr-id` (track `retire-feature-plan-second-planning-layer`, A5): the existence check is renamed
  from `pr_exists_in_plan` to `pr_has_dispatch` and now also reports a PASS row naming the dispatch, because no plan is
  read: a PR exists when a dispatch names it (ADR-047 §2). `pr_completed` now fails closed on a dispatch in
  `completed/` without a terminal receipt (it used to pass with an "unconfirmed" note); only `receipt_confirmed is True`
  counts. A plan file or the `pr_queue_state.json` projection that names a PR still adds or removes nothing.

- Fleet pin audit (track `vnx-fleet-pin-audit`, PR-7): the audit now reports the
  store as well as the pins. A release dir whose VERSION disagrees with its name,
  or whose write bit is still set, is a finding (exit 1); a release dir with no
  readable VERSION is a WARN. A project pin more than
  `VNX_FLEET_PIN_MAX_LAG_RELEASES` releases behind the store's current is now a
  finding instead of a WARN (default 3, read through the config registry); an
  unknown `behind` count always stays a WARN. `--write-state` writes the
  `fleet_pin_audit` health beacon (daily) and refuses to run without
  `--scan-root`, so the beacon can never call a partial fleet healthy. A
  machine-wide launchd template runs it over `$HOME/Development` every six hours;
  loading it is the operator's act.

- Refactor census (track `slop-refactor-panel-review`, S5): steps 4a and 4b of `objective_reconcile.run_reconcile` move to `objective_reconcile_steps` as `gh_unavailable_result` and `verify_candidates` (plus `_verify_one_candidate` for the per-candidate loop body). Behaviour is unchanged — same gh probe, same MERGED cache write order, same deferred cap, same counts — and a new golden (`tests/test_objective_reconcile_summary_golden.py`) freezes the summary key sets, counts and per-track verdicts of the gh-absent, confirmed-plus-deferred and no-candidate runs. `run_reconcile` drops from 272 to 196 raw lines and joins the census targets in `tests/test_refactor_targets.py`.

- `vnx deliverable retarget <id> --objective <track> --reason "..."` (track `deliverable-retarget-withdraw`): a proposed or
  ready deliverable moves to another track with one `deliverable_retargeted` event, instead of a `close --evidence` that
  attests a PR which never delivered the work. A ready one goes back to proposed; started or ended ones are refused.
  The decision lands first as a `state_mutation` receipt (`trigger: deliverable_retarget`) in the store's
  `t0_receipts.ndjson` (ADR-005); a ledger that does not take it is exit 1 and nothing moves. A row that changed in
  between, or a store error after that receipt, adds a `deliverable_retarget_aborted` receipt with the re-read state
  and track. Each decision carries its own `retarget_id` in `status`, so the ledger's outcome dedup (ADR-038) never
  drops the aborted line or a second move of the same deliverable as a duplicate.

- Harness lanes (OI-2373): `ensure_harness_config_dir` accepts `plugins/store/cc-plugin-*_builtin-*.json`, the marker file claude 2.1.311 writes at every session start. Before this, the second DeepSeek or GLM spawn into the same config dir refused with `plugins/store is not an allowed CLI cache file`. Only plain files of that name in a plain `store` directory pass; any other entry is refused and named.

- Dispatch door and prompt seam (track `refactor-skill-en-levering`, RS-2): a spec's `skill` is now refused at the door when it names a folder in neither `skills/` nor `.claude/skills/` (`compile_plan` rejects with `unknown-skill` and both searched paths), carried on `ExecutionPlan` and `EnvelopeSpec`, and injected by the one prompt seam (`skill_context._inject_skill_context`) ahead of the role block. A reviewer role still gets nothing (`enrichment_allowed`), a spec without a skill produces a byte-identical prompt, and a skill that stops resolving at delivery logs one WARNING and continues. Staging and the door now share a single existence resolver (`skill_prefix.resolve_skill_dir`), so the two cannot disagree on where a skill lives. The pool delivery route reads only the spec's deadline and carries no skill — recorded for RS-3's `lane_without_seam`.
- RS-2 round 2 (codex finding on `07c11454`): the shared resolver validates the slug itself — `skill_prefix.resolve_skill_dir` returns `None` and `skill_search_paths` an empty tuple for a slug that is a path (an absolute path, `..`, `a/b`, an empty string), before any path is joined — and the provider lane's `--skill` refuses such a value at the argparse boundary with exit 2, so a standalone provider dispatch can no longer read a `SKILL.md` from outside `skills/` and `.claude/skills/` into the worker prompt.

- Planning layer (track `retire-feature-plan`, A6): `scripts/pr_queue_manager.py` no longer parses a feature plan. Removed `validate_feature_plan`, `_parse_pr_from_feature_plan`, `_parse_all_prs_from_plan`, `create_dispatch_from_pr`, `init_feature_batch`, `load_feature_plan`, `_parse_feature_metadata` and `_classify_gate_severity` together with the legacy self-tests and the `init-feature`, `dispatch` and `test*` CLI verbs (`vnx init-feature` is gone too). `check_pr_dependencies` now answers from `track_dependencies` in the tracks DB via the new read-only `tracks.list_dependencies` — advisory kinds (`soft`, `overlap`) do not block, an unknown kind does, and missing evidence (no DB, no track, no `track` on the PR) is not-ready rather than a pass. `get_execution_order` lost its `feature_plan` argument. Callers: `scripts/roadmap_manager.py` copies the plan as a reference artifact only and the RA-5 step fails closed instead of staging a dispatch (the track-driven step is B2), `scripts/generate_t0_recommendations.py` drops the per-PR `pr_blocked` recommendation, and the planner/kickoff/horizon skill text no longer documents `init-feature`. The `pr_ready` recommendation no longer carries a `command` field either — that command was `pr_queue_manager.py dispatch <PR>`, a verb this PR removes — and its `suggested_instruction` names `vnx dispatch` as the surviving route for staging a dispatch.

- Gate outcomes (track `gate-engine-consolidation`, GE-4b, ADR-046): `gate_artifacts`, the ci_gate verdict in `gate_executor` and both `gate_report_generator` writers now write through `commit_gate_outcome`, so their records carry `attempt_seq` and book their receipt through the one emitter. A request now ends in the same commit as its record (`completed`, `unavailable` or `not_executable`); a refused write leaves the request as it was. `persist_request` writes atomically under the request path's lock. The other writers and the obligation follow in GE-4c.

- Gate engine (GE-5a, ADR-046 §4): `glm_gate` and `kimi_gate` now share one `GovernedLane` adapter, `scripts/lib/governed_lane.py`. The dispatcher wiring moves out of `plan_gate_panel._make_default_dispatcher`, the report reader out of `plan_gate_panel._read_report`, and both gates call `extract_verdict(text, provider)` through a per-provider register instead of carrying their own copies. `plan_gate_panel` re-exports both under their old names, so no caller changes, and behaviour is proven unchanged against a golden corpus of real gate reports (12 fixtures: PASS, FAIL with a blocking finding, advisory-only and truncated per provider). `deepseek_gate` has no parser of its own and stays out of the register.

- `vnx deliverable withdraw <id> --reason "..."` (track `deliverable-retarget-withdraw`, ADR-049): a proposed or ready
  deliverable that will not be built ends in its own terminal state `withdrawn`, with one `deliverable_withdrawn`
  event, instead of `expired` (which reads as failed) or a `close --evidence` for a PR that never delivered it. Same
  ledger-first order as `retarget`: a `state_mutation` receipt, then a compare-and-swap in one transaction with its
  event; a lost swap or a store error books `deliverable_withdraw_aborted`. The swap holds on the track it read as well
  as the state, so a `retarget` that moved the row in between leaves it untouched. A store whose `dispatches.state` still
  carries the CHECK of migration 0022 (a store built by `vnx init`) is refused with that reason.
  - `withdrawn` is in the one canonical terminal set (`coordination_db.TERMINAL_DISPATCH_STATES`), reachable from
    `proposed` and `ready` only; `track_reconciler` and `runtime_supervisor` read that set instead of a copy. A withdrawn
    row is never a deliverable stub, so a track declared done never completes it, and it is no delivery signal: a
    track whose rows are all withdrawn is not derived `done`.
  - Migration 0036 replaces the `deliverables` view (schema v36): `withdrawn` is its own `derived_status` and
    `withdrawn_count`, counted as neither planned, in flight, done nor failed. The down path
    (`apply_0036.revert_migration`) turns each withdrawn row `expired` with a `deliverable_withdrawn_reverted` event,
    ledger first: one `migration_0036_revert` receipt naming every row before the transaction, a refused receipt
    refuses the revert, and a transaction that fails after it books `migration_0036_revert_aborted`. An earlier manifest version accepts the view of a later one, as it accepts extra columns.
  - `deliverable list` and `horizon queue` hide withdrawn deliverables unless `--all`; the scope rule and the plan-gate
    paths never count them.
  - `retarget`: an error that is not a store error after the intent receipt now books the aborted receipt before it is
    raised (kimi on #2241).

- Skills (track `refactor-skill-en-levering`, RS-4 / ADR-048): the dead `skills` tier is gone from the role-candidate list, and the pool route now carries the spec's skill to the worker.
  `role_application._candidate_sources` no longer offers `.claude/skills/<role>/CLAUDE.md` (a skill folder holds `SKILL.md`, so the tier resolved 0 of 38,380 receipts), and `skill_context` no longer probes that path as a role source. A skill reaches a worker only through the spec's `skill` field.
  `pool_worker_runner.run()` now reads `skill` from the claimed dispatch's `dispatch-spec.json` (OI-2372) and passes it through `_deliver_claude`/`_deliver_provider` to `deliver_via_door`, which gained a `skill` keyword (default `None`) and hands it to the same prompt seam RS-2 built. The claude subprocess lane threads it to `dispatch_metadata["skill"]`; the provider lane passes `--skill`. A reviewer role still receives no skill.

- Planning layer (track `retire-feature-plan`, B4, ADR-047): `PR_QUEUE.md` and `scripts/build_pr_queue.py` are gone, and so is the last writer, `PRQueueManager.update_markdown` in `scripts/pr_queue_manager.py`. The review contract no longer reads a queue file: `closure_stage` comes from the dispatch register through the new `review_contract.derive_closure_stage` (`merged` on a `pr_merged` event, `in_review` on a `gate_passed`/`gate_failed` event, otherwise `open`; a GitHub number joins `pr_number`, a label joins only through the dispatch id). `materialize_review_contract` takes `register_events` instead of `pr_queue_content`, and `materialize_from_files` takes `state_dir` instead of `pr_queue_path`. `tests/test_roadmap_consistency.py` is back in the CI sweep without its `PR_QUEUE.md` check, and `docs/core/70_QUEUE_TRUTH_CONTRACT.md` now names the tracks DB and the dispatch state as the truth.

- Feature planning (track `retire-feature-plan-second-planning-layer`, B2): the tracks DB is the only plan source (ADR-047), so the last code that read or materialized a `FEATURE_PLAN.md` is gone. `RoadmapManager.load_feature` no longer copies the feature plan to a root `FEATURE_PLAN.md`, an inserted drift fix-up is a state record with its drift items instead of a generated plan file, `_gates_incomplete` takes the PR ids it verifies from `tracks.pr_ref` plus the linked dispatches' `pr_ref` (no PR evidence still blocks, never passes), the f39 prompt's SECTION 4 is now the dispatch track's goal from the tracks DB (omitted when no track resolves, as in every replay fixture), and `init_chain_state` takes no `feature_plan` argument. The removed symbols are pinned in `scripts/ci/retired_paths.d/dlv-edb850ac4260.txt`.

- Deliverable retarget/withdraw (track `deliverable-retarget-withdraw`): the track's nameting,
  measured read-only on `vnx-dev` (`user_version` 36), is in
  `docs/investigations/2026-10-27-deliverable-retarget-withdraw-nameting.md`. All three goal
  points are `met`: both verbs are on main with a red run on the old head (#2241, #2255); the two
  withdrawn deliverables count as neither delivered nor failed and keep no track open (P09 and
  P21 do); and the OI-2325 reclassification moved 17 deliverables and withdrew 2, with 17 + 2
  `coordination_events` and 17 + 2 `state_mutation` ledger lines, each with its reason.
  The ADR-049 down-migration section and the `-- Down:` comment of
  `schemas/migrations/0036_deliverable_withdrawn.sql` now describe the ledger-first revert of
  #2255 round 2: one `migration_0036_revert` receipt before the transaction, a refusal when that
  append is not `appended`, and the aborted line when the transaction fails after the intent line.

- Review gates (GE-5b): every helper `glm_gate` and `kimi_gate` still carried a copy of now lives in `governed_lane` as one public function taking the provider — the diff reader, the prompt builder, the dispatcher construction, the frontmatter readers, the window parser, the reprocess identity lookup, the verdict-to-status mapping, the unread-run outcome, the lane-log lift and the summary line. The provider's gate name and the messages it prints come from the provider argument or its register row, so `if provider == ...` branches never appear. `main` and its CLI stay per script, `_get_diff` and `_make_default_dispatcher` stay bound in each script's namespace so the existing test patches still reach the call, and `_validate_model`/`_lift_report_failure_reason` stay glm-only. Golden records produced by the unmoved gates prove the result record is unchanged field by field.

- Planning layer (track `retire-feature-plan-second-planning-layer`, B5, ADR-047): the last plan-file scaffolds are gone. `vnx new-worktree` no longer writes `FEATURE_PLAN.md` or `PR_QUEUE.md` into a new worktree (step 6 and its templates are removed), and `vnx init` no longer writes `FEATURE_PLAN.md`; `_write_feature_plan` is deleted, so a new project's first planning step is `vnx track new`. `templates/FEATURE_PLAN_TEMPLATE.md`, `templates/PR_QUEUE_TEMPLATE.md` and `templates/DOMAIN_FEATURE_PLAN_TEMPLATE.md` are deleted, and so is the repo-root `ROADMAP.md` (README, `docs/README.md` and `docs/manifesto/ROADMAP.md` now point at the tracks DB). The `reconcile_queue_state.py --repair` line leaves the canonical T0 role's Quick Commands block and is replaced by the read-only `vnx horizon queue`. `featureplan-kickoff` and `docs/core/120_PROJECTION_CONSISTENCY_CONTRACT.md` stop pointing a reader at a root `PR_QUEUE.md`: projection P-4 is retired, the queue is read from the dispatch register and `vnx horizon queue`. New tests drive the real `new_worktree.sh` and `vnx_init` into temporary trees and assert neither plan file appears.

- Cost ledger (K5a): `vnx costs report [--by model|role|day|provider] [--since YYYY-MM-DD] [--json] [--ledger PATH]`, on `bin/vnx` and on the pip CLI. One read of the ledger (`cost_ledger.aggregate_groups`, one `_ledger_and_spool` scope, the sums from the same loop as `aggregate_runs`) gives a row per group and the totals row: runs, metered `cost_usd`, `api_equivalent_usd`, the five token sums, the runs with an unknown token count, the `unavailable` runs and completeness. `day` is the UTC date of `occurred_from`, and a record whose key field is missing is grouped in `(unknown)`. The read-level counts (spool backlog, legacy and unreadable lines, torn tail, unlocked read) are printed once below the totals and never folded into a group. `vnx costs replay` is unchanged.

- Gate engine (track `gate-engine-consolidation`, GE-4c-1): the failure record (`gate_recorder.record_failure` and
  `record_failure_simple`), the not-executable request and both `gate_result_parser` writers (`record_result` and
  `record_claude_github_result`) now go through `gate_outcome.commit_gate_outcome`. One call writes the record with an
  `attempt_seq`, books at most one `review_gate_result` receipt under the slot lock, and ends its request in the same
  commit; a refused write books no receipt and leaves the request as it was. The two parser receipts are booked by the
  one emitter and keep every field they carried (findings, summary, residual risk, required reruns, the claude_github
  head off its request), passed as `receipt_fields`. Their `status` now uses the receipt vocabulary (`completed` /
  `failed`) with the raw word under `gate_status`, like every other gate receipt. Only a decided pass or fail books a
  receipt: a `record-result` for `unavailable`, `not_executable` or an in-flight status no longer adds one, and a
  status that is neither an outcome nor in flight is refused before anything is written. The unused
  `write_result_guarded(emit_outcome_receipt=...)` opt-out is gone.
  A write on the same head, status and decision that changes `blocking_findings`, `advisory_findings`,
  `required_reruns` or `residual_risk` is no repeat: it lands as the next attempt with its own receipt, and is
  refused (`DuplicateReceiptRefused`, naming the changed fields) when the receipt key would return that receipt as
  a duplicate of the one already booked for the record on the slot.

- `scripts/build_feature_plan.py` and its tests are gone (track `retire-feature-plan-second-planning-layer`, B3; ADR-047): the tracks DB is the only plan source, so `FEATURE_PLAN.md` has no generator left. The OI-1846 dirty-worktree classifier keeps setting a stale marked `FEATURE_PLAN.md` aside, now from a marker literal instead of importing the deleted module. `tests/test_roadmap_consistency.py` keeps its `ROADMAP.yaml` shape checks and drops the in-memory plan comparison; `scripts/ci/retired_paths.d/dlv-8dfb55f41551.txt` keeps the generator retired.

- Classifier cost ledger (K4b-1): every `DeepSeekProvider.classify` and `HaikuProvider.classify` call now starts `claude --print` with a fresh `--session-id` and books one `cost_ledger` line (role `intelligence`, producer `classifier`) from that session's transcript. `ClassifierResult.cost_usd` is the written record's amount instead of a flat constant, with `extra["cost_source"]`/`extra["cost_run_id"]`; a ledger refusal returns `cost_usd` `None` plus `extra["cost_error"]` and a WARNING, and `receipt_classifier`/`scout_prepass` treat that as unknown rather than 0. `record_builder_run` takes a validated `role` (default `builder`, so every K2a call site is unchanged).

- Review gates (track `gate-engine-consolidation`, GE-5b-2): the end of a `glm_gate`/`kimi_gate` run — the unread-outcome/verdict read, the `--reprocess` head check and evidence pair, the recorder write and the printed outcome — is now one body, `governed_lane.run_gate_tail`, called by both `main`s with a `GateRunState`. The provider string, the dispatch-id prefix and glm's report-frontmatter failure lift are register-row fields (`glm` vs `kimi`), not branches; `glm_gate.py` and `kimi_gate.py` keep only their arguments, request record and diff/report acquisition until GE-5b turns them into shims. Records are byte-identical to the pre-move gates on the GE-5b-1 goldens and on new fail / dispatch-error / stale-reprocess goldens per provider.

- Feature planning (track `retire-feature-plan-second-planning-layer`, C2): `ROADMAP.yaml` is now an example of the import format, and no code reads it from a path the caller did not give. `track_reconciler` loses Source 3 (its merged-PR evidence is the NDJSON ledger, `t0_receipts.ndjson` and `gh`), `vnx objective sync` requires `--roadmap <file>` and exits 2 without it, the flag-gated auto-seed hook (`maybe_auto_seed`, `_maybe_auto_seed_tracks` and `VNX_AUTO_SEED_TRACKS`) is gone, and `seed_tracks_from_roadmap.py`, `backfill_dispatch_track_linkage.py` and `build_strategy_projection.py` take the file only as a required argument. `repo_root` keeps its one remaining job: the working directory for the `gh` lookup.

- Refactor census (track `slop-refactor-panel-review`, S1 follow-up): `dead_module_functions` now reads production and test references from one pass over the same corpus, so its definition and numbers are unchanged. Two new classes report what it structurally cannot: `test_only_functions` and `test_only_methods` name a module function or class-body method whose only references outside its own `def` line live under `tests/`. Both are labelled "not proven dead", carry `path`, `line`, `name`, `test_refs` and `in_seed`, and are never folded into the dead class. Methods skip dunders, names another class in the corpus also defines, and names of three characters or fewer.

- Dashboard `/state/*` traversal (slop-sweep fix 2): `DashboardHandler.translate_path` no longer hands back the canonical candidate after its own containment loop rejected it — that path could be a symlink pointing outside the state dir, which `SimpleHTTPRequestHandler` then opened and served. A candidate that resolves inside a state dir but does not exist is still returned under its requested name (a genuine miss keeps its 404), and a canonical miss still falls through to a real file in the legacy dir. Only when every candidate escapes do we return a reserved non-existent `.vnx-state-miss` path inside the canonical state dir, which the handler answers 404 for and which is not derived from the request.

- Deterministic test verification (track `deterministische-testverificatie`, PR-2): `scripts/ci/red_on_base.py` runs the head's selected tests on a base worktree and on a head worktree, copies HEAD's test files and changed conftests into the base tree and deletes the conftests the PR deleted from it, so the base tree differs from the head tree only in the code under test, and rules per test: `behavior` (a base failure whose JUnit record names an `AssertionError`, or shows pytest's rewritten assert, with a green head), `error_on_base` (any other exception on base — it confirms no claim), `not_behavior` (base error or collection error), `green_on_base`, `fails_on_head`, `skipped`, `incomplete`. Evidence is JUnit XML plus a collection manifest per side, keyed by `check_xdist_parity.junit_key_function()`; missing or unparsable evidence, a collection failure or skip, an unrecognised result child and a test neither side collected never pass. A side's evidence counts only when pytest exited 0 or 1 and its manifest names that exit status, a finished collection and a JUnit result for every collected test; an interrupted, internally failed or killed run, a timeout or no tests collected makes every compared test `incomplete` with the reason. A copy or a mirrored conftest deletion is refused, and the file `incomplete` with the path, when the source or destination is a symlink or resolves outside its worktree; a refused conftest copy or deletion leaves no file comparable. It exits 1 on any `fails_on_head`, `incomplete` or false claim (a claimed nodeid whose verdict is not `behavior`) and 2 on unusable input, and writes no verdict then.

- Merge gate (`verify_pr`, track `slop-refactor-panel-review`): the base-branch
  `.vnx-attest/allowed_signers` copy now stays alive through the evidence-bound
  phase instead of being deleted at the end of the provenance phase. A single
  context manager materialises the file once and unlinks it on exit — normal
  return, early return and exception alike — so `required` mode stops refusing
  a valid evidence record as an "invalid signature" and `advisory` mode stops
  reporting false signature diagnostics for the base-branch path.

- Store path parity (track `vnx-init-store-path-parity`, PR-3): `vnx migrate --relocate-legacy --legacy-path <dir> --project-id <pid> [--assume-owner "<reason>"]` copies a pre-#1437 XDG store into the central `~/.vnx-data/<pid>` store and deletes nothing. Before writing it refuses an ambient `VNX_DATA_DIR`/`VNX_STATE_DIR` (or a mismatched `VNX_PROJECT_ID`), a repo-local store that would shadow the target, an existing target, a source with no database, and a missing `--legacy-path`/`--project-id` (no auto-discovery). Ownership comes from the `project_id` stamps in the source DBs: a foreign stamp refuses, no stamp at all (the schema-v14 shape) needs `--assume-owner "<reason>"`, and the assumption is recorded in `state/relocate-legacy.json` inside the published store. The copy stages into `~/.vnx-data/.staging-<pid>-<ts>/<pid>/`: a database is recognised by its header (`SQLite format 3\0`), whatever the file name, so an extensionless `state` database is copied through the backup API with its committed WAL frames and never raw; a `*.db` file without the header is a plain copy, and a header that cannot be read refuses the relocation with the path. Before the upgrade rewrites the copy, every detected database is verified twice: `PRAGMA integrity_check` must answer `ok` and its row count per table must equal the source's (a raw copy loses WAL rows while integrity still answers `ok`). It then runs `_bootstrap_runtime_dbs` plus the whole future-system pipeline there with every step fatal, and publishes with one `os.rename`; a failure leaves the staging dir in place (path printed) and no target. The ownership decision is booked as a `state_mutation` receipt in the published store's `state/t0_receipts.ndjson` (ADR-005) through the shared append writer, before success is reported; anything but `appended` un-publishes the store, so there is no receipt and no store. The source is opened read-only and never modified — the removal command is printed as text for the operator. A store that holds a symlink anywhere in its tree (the store root itself may be a link) is refused before anything is read: `copytree` follows a linked directory while the database walk does not, so its WAL frames would be dropped from the copy.

- Review gates (track `gate-engine-consolidation`, GE-4c-2): `gate_request_handler`'s request writes and its four
  `review_gate_request` emitters now go through one path each. Every request record the module writes —
  `_stamp_takeover_annotations` (Q5), `_request_seat_reader`, `_book_switched_off_seat`, `_request_gemini`,
  `_request_codex`, `_request_kimi`, `_request_glm`, `_request_harness_seat`, `_request_claude_github`,
  `_request_ci_gate`, `request_gemini_with_contract`, `request_claude_github_with_contract`,
  `request_ci_gate_with_contract` and `_request_wiring_gate` (Q6/Q7) — goes through `_write_request_record`
  (`atomic_write_json` inside `slot_lock` on the request path, the pair GE-4b gave
  `gate_recorder.persist_request`), so the bare `write_text` writes and the unlocked `atomic_write_json` writes are
  gone and a reader never sees half a request. The four request receipts (stack, gemini contract, optional Claude
  GitHub contract, ci_gate contract) come from `_emit_review_request_receipt` with one named field set,
  `REVIEW_REQUEST_RECEIPT_FIELDS`; each path still carries exactly the fields it carried before, golden-compared.
- Review gates (track `gate-engine-consolidation`, GE-4c-2): `gate_recorder.annotate_result_guarded` is the write for
  a takeover annotation (R11, `_stamp_takeover_annotations`): read, merge and write now happen inside one `slot_lock`
  behind the overwrite guard the commit uses, so a decided verdict landing in the gap can no longer be replaced by an
  annotated copy of the record it superseded (OI-1630). It is not `commit_gate_outcome` and does not become one — an
  annotation is no outcome (ADR-046 §1 has no outcome for a `requested` record or a ci_gate `running` snapshot) and
  must not stamp a second `attempt_seq` for a decided verdict. The optional-gate result mirror (R10) stays on
  `write_result_guarded` for the same reason, now stated on the function.
- Review gates (track `gate-engine-consolidation`, GE-4c-2): `gate_outcome._finish_request` updates the caller's
  payload only after the request write has landed. Until now it mutated the dict first and only logged an `OSError`,
  so a caller read back a request status the request on disk never reached.

- Review gates (GE-5b): `glm_gate.py` and `kimi_gate.py` are now shims of 77 lines each. Their phases 1-2 — the argument parser, the request record written before any dispatch, and the diff/report acquisition for a live run and for `--reprocess` — moved into `governed_lane.run_gate_head`, which reads every provider difference off the register row (`cli_description`, `model_pin`, `lane_provider`, `dispatch_id_prefix`, `gate`) and carries no `if provider ==` branch. With GE-5b-2's `run_gate_tail` the two gates are one engine on one path: `run_gate_head` -> `GateRunState` -> `run_gate_tail`.
  The CLI is unchanged: the `--help` text and the exit code of a missing `--pr` match a golden recorded on the old head byte for byte, and the option set is still `--pr --diff-file --data-dir --model --timeout --json --reprocess`. Every seam a test patches on a script module (`_get_diff`, `_make_default_dispatcher`, `_resolve_data_dir`, `persist_request`, `max_diff_chars`, `get_pr_head_*`, `record_terminal_result`) is still reached through that module's binding at call time and called exactly as often as before. The unused `import re` is gone from both scripts (kimi advisory on #2253); the fixture texts under `tests/fixtures/governed_lane/` are untouched, so every golden record reproduces field for field.

- Feature planning (track `retire-feature-plan-second-planning-layer`, C-last): the five done points re-measured on main after #2267, with the command and the count per point — `docs/investigations/2026-10-27-retire-feature-plan-nameting.md`. Points 1-4 are met; point 5 is not as written: `PR_QUEUE.md` is in no retired-paths fragment (the reason is recorded in the fragment and reproduced in the document), so the check cannot catch a re-created plan file. The document also judges `dlv-46f7f927d1a1` per part of its title and finds it fully delivered by #2202, #2201, #2242, #2256, #2263, #2257 and #2261, so the T0 can withdraw it (ADR-049). The measurement proved one production line wrong: `vnx roadmap`'s help still promised that `load <feature_id>` materialises `FEATURE_PLAN.md`, which `RoadmapManager.load_feature` stopped doing; the help now names the branch and worktree it actually stages.

- Store path parity, PR-4 (track `vnx-init-store-path-parity`): a new operator runbook
  `docs/operations/LEGACY-XDG-STORE-MIGRATION.md` measures a project whose only store
  still lives under `${XDG_DATA_HOME:-~/.local/share}/vnx/<project_id>` (the
  `store:legacy-xdg` WARN in `vnx doctor`), copies it with
  `vnx migrate --relocate-legacy` (the real flags, every refusal and why, and the
  staging failure path), checks the copy (`vnx doctor` PASS, `PRAGMA integrity_check`,
  `PRAGMA user_version`, `relocate-legacy.json`, the ownership receipt, source hashes)
  and leaves removal of the old store as the operator's own step.
  `docs/MIGRATION_GUIDE.md` gains a short section 7 pointing at the runbook, and the
  operations index lists it. Documentation only; no code or behaviour change.

- T0 role audit (track `orchestrator-provider-agnostic-role`, golf 3): `t0_role_audit.sh --static` now fails when the `<!-- VNX:BEGIN T0-ROLE -->` block in `AGENTS.md` or `GEMINI.md` is not the text of the project's `role-orchestrator.md` (`PROVIDER-ROLE-DRIFT`). Before this the check only looked for the markers, so a hand-edited mirror audited exactly like a synced one. A block that cannot be delimited, or a missing role file to compare against, is a finding too — an unmeasurable mirror is never current. A provider file with no block stays unflagged: `vnx role sync` generates those files, and their absence is `fleet_role_drift.py`'s coverage warning.

- `vnx doctor` (track `vnx-init-store-path-parity`): the `store:legacy-xdg` WARN no longer claims that
  relocating a legacy XDG store is unwired. It now names the shipped command with the exact flags —
  `vnx migrate --relocate-legacy --legacy-path <legacy-store> --project-id <project_id>` (the run copies and
  deletes nothing) — and points at `docs/operations/LEGACY-XDG-STORE-MIGRATION.md`. `tests/test_doctor_legacy_store.py`
  pins the new sentence and asserts the stale "will be available in a later release" phrase is gone.

- Blocked-track triage deel c (OI-2362, punt 11): de 50 geblokkeerde tracks zonder rij in triage a/b
  zijn op hun goal getoetst — 4 GESHIPT, 22 DEELS, 24 OPEN, 0 ONTOETSBAAR — met een join over
  triage a/b/c die 0 geblokkeerde tracks zonder triage-rij laat zien (`claudedocs/blocked-track-triage-c.md`).

- OI-2262 (track `deliverable-retarget-withdraw`): a rejected or superseded attempt now ends in a terminal state, and a base deliverable closes on the evidence of its own round.
  - `receipt_query.py decide <id> reject` ends the attempt it rejected: a dispatch row in `failed_delivery` or `timed_out` moves to `dead_letter` through `runtime_state_machine.transition_dispatch` (actor `t0`, the decide reason, the decision-log timestamp in the event metadata, the row's `project_id` on the event). Any other state is untouched, a second reject is a no-op, and a refused transition exits non-zero with the decision line already on disk. One such row used to keep a whole track `queued`/`in_progress` forever.
  - `vnx deliverable close <id> --evidence N` accepts a base in `failed_delivery` or `timed_out` when a `<id>-rK` row of the same project is `completed`: the base ends `expired` (terminal, no delivery claim) with `superseded_by` and the evidence in its metadata, one `deliverable_superseded` event, and a compare-and-swap on the state it read. Without a completed round — or with a round of another project, an id that is not a round, or unreadable metadata — the close is refused with nothing changed. `ready` behaves exactly as before.
  - Migration 0037 (schema v37) replaces the `deliverables` view: a superseded base whose round is `completed` derives `done` instead of `failed`; with no superseded row it derives what v36 derives. The down path restores the v36 view and stamps 36 without touching a row, so ADR-005 asks for no ledger line. `_compute_derived_status` reads the `deliverable_superseded` event as a positive delivery signal next to `pr_merged`. ADR-049 carries the amendment.
  - The round id itself is validated (OI-2403). `superseded_by` counts as evidence only when it is exactly `<base-id>-rK` with a positive integer `K` and the row with that id of the same project is `completed`: the v37 view's join now requires the `-r` prefix, a non-empty all-digit suffix and no leading zero, so an expired base naming any other completed dispatch of the project no longer derives `done`, and `-r0`/`-r03`/`-r`/`-rX` are not rounds. `schema_manifest.is_round_of(base_id, round_id)` is the one Python copy of that rule; `vnx deliverable close` uses it for the round it records and `horizon queue` uses it for the `-rK` row it reads a PR from, so the round the close writes is exactly the round the view reads back.
  - The supersede write no longer loses a concurrent metadata change. `metadata_json` used to be read before the write transaction began while the compare-and-swap covered only the state, so another connection could commit a metadata change in between and the close would overwrite it silently. The close now opens `BEGIN IMMEDIATE` before the row is read and keeps the read, the merge of the `superseded_*` keys, the `UPDATE` and the event in that one transaction; the `UPDATE` also compares against the `metadata_json` text it read (`AND metadata_json IS ?`), so a writer that bypasses the lock gets `row_changed` and nothing is written. `end_attempt_on_reject` reads its row under the same lock (its transition had no compare-and-swap at all); it never reads or writes metadata. Tests drive the codex reproduction with two real sqlite connections on one store file.

- Research note (OH-1, `oss-harness-orchestrator-interchange`): `docs/investigations/2026-10-28-oss-harness-orchestrator.md` maps the eight places the T0 role still couples to Claude Code (SessionStart injection, Skill namespace, `/goal` + rotation spawner, status line, role/tri-file, `.claude/settings.json` hooks, auto-mode classifier, tmux pane semantics), with `file:line` on head `164d48ad`.
  The door measurement stages one spec into a throwaway store with `VNX_DATA_DIR_EXPLICIT=1` and diffs `vnx dispatch --dry-run` from the ambient shell against `env -i` (no `CLAUDE_*`, no `CLAUDECODE`): the lane plan is identical, and the two differing lines are explained by `VNX_PROJECT_ID` and Anthropic auth identity (`ANTHROPIC_BASE_URL`), with a control run that re-adds only those two reproducing the ambient output byte-for-byte.
  The candidate matrix scores codex CLI and kimi CLI against the six T0 capabilities ADR-042 lists (11 of 24 cells measured; OpenCode and Aider have no local source and are "niet gemeten"). The recommendation orders the golf-3 adapters kimi, then codex, then OpenCode, and names the missing measurements. The note is internal and makes no claim that VNX works with any harness.

- Governance receipts (track `gate-weight-and-worker-posture-cluster`, nameting): `scripts/lib/governance_receipts.py` makes the `append_receipt` facade's own directory importable before importing it, so a lib-only entry point (`scripts/analysis/*`, a standalone test) no longer dies with `ModuleNotFoundError: append_receipt` after #2266 put the facade on the import chain of `plan_gate_panel`. The point-4 evidence command runs again and `tests/test_plan_gate_panel_effectiveness.py` no longer depends on a sibling test's `sys.path` side effect. New regression test `tests/test_governance_receipts_import_path.py`.

- Worktree removal guard (PR-4a): one `guarded_remove` holds the worktree's slot — the gate seat's lifetime flock for a gate checkout, the dispatch occupancy lock otherwise — through the removal, probes process-group liveness as alive/dead/unknown and keeps the tree on unknown, and revalidates `HEAD` and a clean `git status` under the slot before removing. A gate checkout now carries a lifetime flock (`<checkout>.live`) from creation to its teardown, so a running reviewer's tree can no longer be deleted under it. `dry_run` is the default and removes nothing.
- Worktree removal guard (PR-4a round 2): a process group is classified dead only when `killpg(pgid, 0)` says the group itself is gone — a worker's child that outlived the registry snapshot, or a reused PGID, keeps the tree instead of being deleted under it (teardown still signals only groups with a recorded member carrying the PGID). The clean/HEAD evidence is revalidated under the repository lock on every removal, not only when a test hook is supplied, and the removal decision is persisted with `emit_state_mutation` before `git worktree remove`; if that ledger write does not land, the tree is kept with reason `decision_unrecorded`.

- Cost ledger calibration (track `kosten-per-model-in-het-grootboek`, K5b, ADR-043 §7): a balance measurement is now its own record kind. `vnx costs observe` writes one `account_observation` per provider it can measure (deepseek: total, granted and topped-up balance; openrouter: total credits and total usage) to `events/provider_account_observations.ndjson`, appended atomically under its own lock file in the ledger's style (ADR-043 §6), with `unavailable:<reason>` when a probe fails — never a silent skip. `calibration_delta` reads the drop in the total balance as the spend of a window; a rise of the topped-up balance is a top-up, so a window that holds one says `unavailable:topup_in_window` instead of a number, and the windows around it are unaffected. `vnx costs report --by provider --json` carries that delta next to the ledger's metered sum with the deviation in percent. `openrouter_coverage` reports a generation without a ledger line as a missed catch when the API can list generations, and otherwise sets the `total_usage` delta against the ledger's OpenRouter sum and says that generation-level detection has no source. No spend or token sum reads the observations file. Round 2 tightens the calibration: the ledger sum in `--by provider --json` covers exactly the interval the observations measured (the first to the last observation used), so a run before or after it no longer fakes a deviation; a DeepSeek balance drop counts only when both observations are in USD (`unavailable:currency_<code>` otherwise, `currency_mismatch` across currencies, `currency_unknown` without one); a valid-JSON line that is not an object counts as unreadable instead of crashing the reader; and a listed OpenRouter generation without a readable `created_at` makes coverage say `unavailable:no_generation_source: generation_without_created_at` instead of comparing a moment with `None`.

- Gate obligations (GE-4c, ADR-046 step 8): an obligation OUTCOME — the record marked fulfilled/failed/not_executable because a gate run ended — is now booked through `gate_outcome.commit_gate_outcome` (or its obligation-only sibling `commit_obligation_outcome`) inside the result slot's lock, next to the record and its `review_gate_result` receipt. The record carries `obligation_pending`/`obligation_booking` before the obligation write is tried, so a crash in between leaves a durable work order that the next commit on the slot books exactly once (idempotent by value); a debt whose booking cannot be read or re-booked is a refusal (`ObligationPendingRefused`), never a silent replacement. Scheduling state (claims, retry counters, `dispatch_still_running`) stays with `gate_obligations.update_obligation`.
- Refused gate writes (GE-4c, kimi advisory (a)): a refused result write now ends its request — the writer's own terminal end when it named one (`record_failure`, `record_not_executable`), else `failed` with `failure_reason: write_refused` — instead of leaving it on `executing` for the next `execute_gate` to re-run into the same refusal.
- Request writes (GE-4c, kimi advisory (b)): the request end is built from a copy of the caller's payload and only written back once the write landed; a failed write raises `RequestWriteFailed` from the commit after the bundle archive, instead of being logged and dropped.
- Request statuses (GE-4c, kimi advisory (c)): `gate_status.REQUEST_TERMINAL_STATUSES` is now the one list `gate_outcome.REQUEST_STATUS_OF` and `gate_executor.execute_gate` share. `unavailable` was in the mapping but not in the executor's short-circuit list, so a request ended `unavailable` was re-run.
- Repeat commits (GE-4c round 2, codex finding 1): a repeated outcome that carries an obligation now persists `obligation_pending`/`obligation_booking` on the record already on the slot BEFORE it books, exactly like the first commit does. A booking that raises therefore leaves a durable work order the next commit on the slot pays exactly once, instead of a commit that returned success with neither a booking nor a work order.
- Refused writers (GE-4c round 2, codex finding 2): step 9 reads the request record on the slot under that slot's lock and ends only the request the caller holds. A request that is already terminal (its completion metadata is kept), or that belongs to another attempt — a newer retry in the same `(gate, PR)` slot — is left untouched and the refusal is named (`RequestLeftAlone`: `request_already_terminal`, `request_belongs_to_another_attempt`, `request_not_on_disk`, `request_unreadable_on_disk`). The caller's stale in-memory status no longer decides.

- Audit trail (track `audit-spoor-kern`, group 1): ADR-045 records the ledger lifecycle of a dispatch. The hand-off to the lane is the boundary: the door writes one `dispatch_started` first, then `lifecycle_mode` in the dispatch row, then spawns; a failed start line fails closed. Each attempt `(project_id, dispatch_id, attempt)` gets exactly one `dispatch_finished` with an `end_state` from `completed`/`failed`/`timed_out`/`abandoned`, and the report becomes `report_ingested`, never a second outcome. `abandoned` is a ledger fact and a `timed_out` row with `metadata_json.end_state`, not a new DB state. Liveness is the occupancy flock, the door process or the child process, each with its start time; the sweep books `abandoned` after deadline plus 30 minutes and runs on the existing runner tick, at the door and at every `build_t0_state`. `schema_version: 3` and `writer_sha` land in the append layer, `ledger_lifecycle` ships off, no backfill, and `track` keeps its meaning. Identity block, per-dispatch event stream and hash chain stay with `audit-spoor-volledig`. Docs only; K-1 to K-meet follow.

- Golf 1 (track `golf-1-opruimen`): the closing measurement of the four goal points on main — ADR-042 is accepted with per-part stays/goes, the three child tracks are done, the 17-10 numbers re-run to 847 files / 347268 lines / 149 tmux / 126 T1-T3, and every removal PR is checked for a guard fragment, a caller measurement and a merge (one open item: #2173's kimi seat has no PASS).

- Pool route (T-D0): the elastic pool now starts `pool_worker_runner` by default, so a pool worker delivers through the single-entry door; `VNX_SINGLE_ENTRY_DISPATCH=0` (or `VNX_DISPATCH_LEGACY=1`) still starts the legacy `subprocess_dispatch` worker.
  The pool worker owns its terminal lease: it renews `terminal_leases.last_heartbeat_at` while it runs and releases the row at its end.
  ADR-042 besluit 6 is amended accordingly; `terminal_leases` stays a pool table with `project_id` in its key (ADR-007).

- Pane injection (track `terminal-binding-afbouwen`, T-A4): the provider-aware tmux injection now lives in `scripts/lib/pane_injection.sh` — `pane_inject_prompt` plus the transport helpers (`tmux_send_best_effort`, `tmux_load_buffer_safe`, `tmux_retry`, the named-buffer `_ddt_buffer_name`) — and `scripts/lib/dispatch_deliver.sh` sources it. Behaviour is unchanged: the same argv, the same OI-1144 named buffer and the same failure substeps, which `dispatch_deliver.sh` maps back onto `_DDT_FAILED_SUBSTEP`. The model switch and the `VNX_ADAPTER_T{n}` selector stay where they are (T-A5, T-G1).

- Release (PR-6): `vnx release verify <version>` fails when a published version dir drifted from its tag, naming every differing path (only the stripped `.vnx-project-id` and the written `.vnx-install-mode` are allowed); `vnx release publish` refuses a tag whose commit is not on the default branch, with `--allow-unmerged-tag <reason>` as the only escape.
- Release (PR-6 round 2): `vnx release verify` no longer reads `git status` — a skip-worktree file that changed on disk and an extra file hidden by `.gitignore` both slipped through as "byte-identical". It now compares the published dir with the tag's tree by content: one read-only `git hash-object --no-filters --stdin-paths` batch for the regular files, the link target bytes against the tag's `120000` blob, and the executable bit against the tag's mode, naming every difference as `changed`/`missing`/`extra`/`mode <path>` (a gitlink is `submodule <path>: not verified`; a failed git read is a problem line, never a pass). The only exceptions are still the stripped `.vnx-project-id` and the written `.vnx-install-mode`.

- Red-run claims (track deterministische-testverificatie, PR-3a): `scripts/ci/red_run_claims.py` turns a PR body's red-run claims into `red_on_base.py --claims` input, labels imported from `report_body_contract.RED_RUN_LABELS`.
- A claim the parser cannot read exits 3 and writes no claims file instead of silently becoming "no claim"; whitespace inside a parametrize part such as `test_always[hello world]` is now read exactly.
- A bare path a space cut in two (`tests/my test.py::test_one`, `tests/x.py ::test`) is refused as one unreadable fragment instead of being dropped as two words no claim detector reads.
- The nodeids of a claim line are read by one left-to-right scan of its backticked and bare tokens, so the claims keep the order the body writes them in instead of listing every backticked id first.

- Doctor (release:main-drift): `vnx doctor` BLOCKS (FAIL, non-zero exit) when the release the
  machine runs lags `origin/main`, naming the missing commits and the paths they touch
  (`scripts/lib/release_drift.py`). Read-only, fetch-free; without a release, a git checkout or
  a local main ref it reports `skipped: <reason>`.
- Doctor (release:main-drift): a git measurement that FAILS — `rev-list`, `git log`, an
  unlaunchable git — now blocks as FAIL naming the command and its stderr, instead of
  passing as a `skipped:` line; only a legitimately absent ref still skips.

- RG-5: the B3 override also runs through `VNX_OVERRIDE_B3=1` plus `VNX_OVERRIDE_B3_REASON` at the merge door (`pr_merge.py`) and the review gate CLI (`review_gate_manager.py`); the flag wins when both are given, a carrier without a reason is still refused, and the record names the carrier (`flag`/`env`) next to the reason.

- Review cap (RG-3): `mergen-met-OI` is carried out at the B3.2 cap instead of being stored as
  `outcome_unavailable_in_r2a`. The remaining codex blockers become open items with the
  `review-cap:<project>:<verdict>:blocker-<n>` dedup key; the deliverable and the PR stay as they
  are, and a closing record carries `open_items_written`. The merge itself needs an
  `operator_approval` receipt bound to that verdict id, its PR and its head — one per use, no
  `review_cap_approvals` table (ADR-041 amendment R2c).

- Panel cost ledger (K4a, ADR-043 §1): every leg of a panel run — each diverge seat and each contrarian/verify/synthesis attempt — books one `panel` line under the panel run's id, with the stage and the seat as its producer. A leg whose response reported its own amount (OpenRouter `usage.cost`, or ADR-040's `provider_reported` pair) books that amount with source `provider_reported`; a leg with tokens only is priced from the register; a leg with no usage books `unavailable:<reason>`, never a zero. A leg its lane already booked (a receipt with `cost_ledger_run_ids`) is not booked twice, and a refused write logs one WARNING and leaves the panel's result intact.

- Gate engine (track `gate-engine-consolidation`, GE-6): the local half of `ci_gate` and `wiring_gate` is now the `deterministic` adapter kind (ADR-046 §4), one row per gate in `gate_recorder.GATE_PROVIDERS` and one register table in the new `scripts/lib/deterministic_gate.py`. `ci_gate`'s classification of the rows `gh pr checks` returned, and `wiring_gate`'s AST+grep analysis of a PR diff, now run in-process with no `gh` subprocess; the GitHub read of each gate stays where it was (`gate_executor._execute_ci_gate`, `wiring_gate.check_pr_wiring`). `wiring_gate.analyse_diff` is the extracted local half (it can also derive the diff from a local checkout with `git` alone, `deterministic_gate._local_diff`) and greps an explicit `root`; a check that cannot run is `not_executable` with the reason, never a pass. The outcome record of both gates is built once, in the adapter, and still lands through `commit_gate_outcome` — identical field for field (plus `kind`) to what the old writers produced. `gate_runner` refuses a `deterministic` gate by name (`gate_not_subprocess_routable`) instead of looking up a binary named after the kind, and `gate_recorder.gate_is_available` answers it by registration.

- Settings merge (OI-2057, track `fleet-release-and-rollout` PR-1): `vnx regen-settings --merge` refreshes the VNX-owned hooks and leaves every project hook in place. A hook handler is VNX-owned when its command equals one the template renders for the same event, or names a path in a VNX install's hook tree (a `scripts/hooks/` segment, or anything under the central install's `/.vnx-system/`); a handler whose command cannot be read is kept, because keeping loses nothing and refreshing on a guess deletes a project hook. Before this a merge replaced the whole `hooks` block, and step 4 of the per-project cutover made that the documented procedure: a pinned consumer measured 2026-10-27 carried 10 handlers, 3 VNX-owned and 7 project-owned, one of them its `t0-readonly-enforcer.sh` security hook. `--merge --replace-hooks` restores the old replace-everything behaviour explicitly, and `--merge --dry-run --json` reports per event what is kept, added and replaced under `_vnx_hook_report` — dry-run output that is never written to `settings.json`. `--full` is unchanged and still rewrites the file from the template alone.
- Settings merge, round 3 (OI-2410): the `vnx regen-settings` wrapper now forwards `--replace-hooks` to the merge engine instead of rejecting it with `Unknown option` (exit 1), so the restore-old-behaviour path both this entry and `docs/core/12_PERMISSION_SETTINGS.md` document actually runs; its `--help` names both behaviours and no longer claims a `--merge` replaces hooks entirely. A matcher group whose `hooks` list holds a non-object entry, or a `type: command` handler with no string `command`, is now refused by the merge before anything is written — exit non-zero, file untouched — because an unreadable handler is not evidence that it is VNX-owned.

- Pane injection (track `terminal-binding-afbouwen`, T-A5): the model switch now lives in `scripts/lib/pane_injection.sh` — `switch_terminal_model`, `_stm_send_switch_command` and `_clear_terminal_context`, bodies byte-identical — and `scripts/lib/dispatch_deliver.sh` reaches them through the sourced lib it already loads. Behaviour is unchanged: one `/model <name>` paste per switch with Enter as its own keystroke, the same `opus` → `default` normalization, the same `C-u` line clear before a context reset, and no paste when the routing pre-check says no switch is needed. `tests/test_pane_injection.py` pins both step sequences through a recording `tmux` stub, directly on the lib and through `configure_terminal_mode`, so the move is proven behaviour-equal.

- Terminal-binding-afbouwen T-B1 (P15a): the T0 state build no longer probes tmux. The `terminals` block of `t0_state.json` is now filled from the `terminal_leases` rows only (state, dispatch, last heartbeat), so `scripts/build_t0_state.py` never runs `tmux list-panes -a` and never depends on a live tmux server; a host without tmux still produces a complete `live_work` section. The block itself stays until T-B2 removes it together with its reader, and `live_work` is unchanged. A lease read that fails is `unknown` with a `lease_error`, never idle.

- launchd scope guard (fleet-release-and-rollout PR-2): `check_pin_alignment` judges every installed
  `com.vnx.*` plist against the pin of the project it serves and reports `pin_followed`,
  `pin_ignored_current`, `pin_ignored_other_version` or `pin_dead`, so a job on the rolling `current`
  link of a pinned project is no longer `ok`. Read-only; `main --json` carries the section and exits
  non-zero when a pinned project's job ignores its pin.
- follow-up (round 2): `pin_followed` now requires the job's `VNX_HOME` to exist and to `resolve()` to the
  installed pinned release directory; a missing path or a same-named symlink into another release is
  `pin_ignored_other_version` with the missing/resolved directory in the detail (was a basename-only match).
- follow-up (round 3, OI-2415): a `VNX_HOME=` assignment in `ProgramArguments` (bare, `export`, `env VNX_HOME=...`,
  inside `bash -c`/`sh -c`) now takes precedence over `EnvironmentVariables`, so a job whose environment names the
  pinned release but whose command says `VNX_HOME=<current>` is no longer `pin_followed`. An assignment the parser
  cannot read with certainty (another variable, a command substitution, conflicting literals, an empty or
  unterminated value) is reported `pin_unmeasurable` instead of falling back to the environment.

- Dispatch queue (track `terminal-binding-afbouwen`, T-A10): the legacy `.md` promote flow is gone. A delivered `.md` is no longer moved from `dispatches/pending/` into `dispatches/active/` and no `dispatch_promoted` event is emitted; the delivered file is consumed instead, so `pending/` cannot hand it to the dispatcher a second time. `PRQueueManager.promote_dispatch` is deleted, `bin/vnx` dispatches no `promote` and `python3 scripts/pr_queue_manager.py promote` refuses, naming the door: a staged spec bundle (`pending/<id>/dispatch-spec.json`, fired with `vnx dispatch <id>`) is the only queue. `show`, `patch`, `reject` and the staging listing still serve staging bundles. The retired entry is pinned in `scripts/ci/retired_paths.d/` and by `tests/test_md_promote_retired.py`. No document still sends the reader to the retired command: the T0 role, `docs/core/DISPATCH_RULES.md`, the architecture doc, the kickoff skill, the onboarding guide and both example walkthroughs now name `vnx dispatch <id>` (or the retired flow in prose, without a command to run).

- Cost ledger (K2b-a): codex and kimi builder runs now write their own ledger line instead of the old cost event (ADR-043). `harness_run_cost` gains a fail-open CLI booker per provider (`record_codex_run`, `record_kimi_run`, behind `record_cli_run`), and token usage is normalised per provider before it reaches the ledger: codex's `input_tokens` includes the cached part (40939 in with 33536 cached books 7403 uncached), kimi's `wire.jsonl input_other` excludes it. A cached count larger than the input books `input_uncached: null` with `input_cache_split_invalid`; a provider whose split was never measured gets `input_cache_split_unmeasured`, never an "input means uncached" default (OI-2407).
- Cost ledger (K2b-a): `CodexSpawnResult.spawn_refused` names a codex launch refusal explicitly (unresolvable model, missing binary, a failed `Popen`), so a builder that started and ended without usage books `unknown:no_usage_fields` rather than a zero-token refusal; both builders bill `subscription` with the list price in `api_equivalent_usd`, and a failed booking is logged with the ref, provider and reason and never fails the build.

- CI (deterministische-testverificatie PR-3b): the claimed red-on-base signal lives in its own workflow, `.github/workflows/red-on-base.yml` (`name: Red on base`), triggered by `pull_request` with `types: [opened, synchronize, reopened, edited]`; `.github/workflows/vnx-ci.yml` stays identical to main, so editing a PR body starts no `VNX CI` run that could turn a red head green.
- The new job keeps the merge-base selection (one `merge_base` step, `sha` output) and reads the claimed nodeids through `red_run_claims.py`; the claims step is not `continue-on-error` and runs under `set -euo pipefail`, so an unreadable claim fails the job, and `red_on_base.py --claims` reads only that step's persisted output.

- Review cap (RG-2, OI-2143): `vnx-gate/review` takes the merge door's `--override-b3` reason — a capped head the door merges publishes `success` with "B3 override" and the reason on the summary, `gate_recorder` hands the record's `override_b3.reason` through, and `refresh_review_summary` republishes the check for one head without writing a gate record (nothing for a head that moved).

- Tests (track `terminal-binding-afbouwen`, T-A7): `tests/test_dispatch_deliver_contract.py` sources `scripts/lib/dispatch_deliver.sh` in a real bash, stubs both branch entry points as recorders and counts their calls, so the `VNX_ADAPTER_T{n}` default is pinned on behaviour (T1/T2/T3 unset → subprocess, `=tmux` → tmux, T0 unset → tmux) instead of on the inline copy in `test_t2_t3_subprocess_default.sh`, which never reads the shipped selector.
  An inverted-selector scratch copy turns four of the eight cases red on the call counts, which is the failure T-G1 will produce when the selector goes.
- Tests (OI-1227): `tests/test_subprocess_adapter_pr3.py` and `tests/test_vnx_start_runtime.py` leave `scripts/ci/test_exclusions.txt`. Their two red assertions were stale, not broken code: the session-id case still expected the pre-OI-1125 "first init wins" while that fix deliberately refreshes on every init, and the VNX_VARS expectation lacked `VNX_SOCKETS_DIR` (added in W4G). Both files are green solo; each fix stayed inside the test.

- Review round cap (RG-1): B3.2 now also caps a PR once three codex FAIL rounds (default `VNX_REVIEW_ROUND_CAP`, per project) fell on distinct heads since the last opened line; the record and the refusal name the threshold (`blockers` or `rounds`), and a malformed setting raises instead of falling back to 3.
- Review round cap (RG-1 round 2): the cap resolves from the ledger's own state dir and project (`cap_state(..., project_id=, state_dir=)` / `round_cap`), so `cap-verdict --data-dir` and every door/writer/request reader judge another store by that store's project threshold, not the launching process's.

- Worktree cleanup sweep (PR-4b): `scripts/cleanup_reviewed_worktrees.py` now takes its candidates from `scripts/lib/worktree_inventory.py` — every fabric-owned worktree (dispatch, gate, pool, bench) except the running checkout and trees of another repo — instead of only `vnx preserve`-locked `dispatch-*` trees, so a leaked gate, pool or bench tree is finally visible to it. Every removal goes through `worktree_removal_guard.guarded_remove`, which owns liveness, occupancy and HEAD; a preserve lock is dropped only inside that guard, right before its removal, so a tree the guard keeps stays locked. Locked trees keep the reviewed-worktree evidence; a tree without a lock additionally needs a clean status, no OPEN pull request (unmeasurable without `gh` keeps the tree) and no commit outside origin. The default stays a dry run. `orphan_sweep`'s worktree kind keeps its own tmux/register-driven removal.
- Worktree cleanup sweep (PR-4b round 2): `evaluate_candidate` returns the HEAD sha it judged and `guarded_remove` takes it as a required `expected_head`, refusing with `head_moved` (naming both shas) while holding the slot when the tree no longer sits on it. A clean commit made between evidence evaluation and guard entry therefore keeps the worktree and its branch: `git branch -D` runs only after a removal that passed that check.
- Worktree cleanup sweep (PR-4b round 3): the preserve lock is now dropped as the guard's last step — after the under-lock revalidation and after the removal decision is written, immediately before `git worktree remove` — so a late write (`dirty`), a late commit (`head_moved`) or an unrecorded decision (`decision_unrecorded`) keeps the tree AND its lock, and a removal that fails re-locks the tree. `_dispatch_slot`'s docstring now names that a dry run still holds a real occupancy slot.

- Receipt ledger (track `audit-spoor-kern`, K-1, ADR-045): the ledger knows three dispatch lifecycle line types — `dispatch_started`, `dispatch_finished` and `report_ingested` — in `receipt_schema` (one required-field set per type, each missing field refused under its own name), in `event_outcome_semantics` (`dispatch_finished` is a completion type whose `end_state` maps to the projection status; a start line and a report line are never an outcome) and in `receipt_outcome` (`report_ingested` is a report type and never a second outcome). A second `dispatch_finished` for the same `(project_id, dispatch_id, attempt)` is refused, found through the ADR-038 index with a bounded ledger tail scan as the fallback; byte-equality with the intent is one named hook that accepts today (K-3). The three types carry `schema_version: 3` and `writer_sha`, stamped at one place in the append layer; every other type keeps `schema_version: 2`, and readers accept both. `VNX_LEDGER_LIFECYCLE` (`ledger-lifecycle`) is off by default, and while it is off the append layer refuses a lifecycle line, so no writer emits one.
- Receipt ledger (track `audit-spoor-kern`, K-1, ADR-045, round 2): the attempt triple now scopes every lifecycle line, not just the end line — `dispatch_started`, `dispatch_finished` and `report_ingested` all carry `project_id` and `attempt` in BOTH identity calculations (the short-window idempotency key and the durable outcome key), decided by one helper (`outcome_identity.lifecycle_identity_fields`), so attempt 2 of a dispatch and the same `dispatch_id` in another project are never discarded as attempt 1's duplicate. The correction key is scoped the same way, so a line only supersedes an earlier line of the same project, dispatch and attempt instead of inventing a supersedes relation across attempts or projects. Legacy receipt types keep their identity, correction and idempotency keys byte-identical.

- launchd pinned start (fleet-release-and-rollout PR-3): `scripts/lib/pinned_engine.py` resolves a
  project's `.vnx-version` through `~/.vnx/projects.json` to `<store>/versions/<pin>`, and
  `scripts/launchd/vnx_pinned_launcher.sh` starts the job from that tree with cwd and `VNX_HOME` on the
  pin; `scripts/launchd/com.vnx.gate-obligation-runner.plist` now starts through the launcher instead of
  running `python3 scripts/gate_obligation_runner.py` from whatever tree the plist was rendered from.
- no pin means the declared `VNX_HOME`, unchanged for a dev checkout; a pin that is not installed, is
  unreadable or malformed, or escapes the versions root exits 78 (EX_CONFIG) with one stderr line naming
  project, pin and the directory searched — never a silent fallback to another tree.
- the launcher only relies on the target tree carrying `<relative-script>` (nothing is imported from it),
  so a newer tree starts an older pin; a pinned tree without the script is refused the same way. Every
  start resolves again and a run in progress finishes on the tree it started from.
- rollback: revert this commit and run `bash scripts/launchd/reload_plist.sh com.vnx.gate-obligation-runner`
  again; that reload restores the direct call in the installed plist.

- Cost ledger (K2b-b): the codex and kimi review gates now book their own ledger line when the run ends — role `review`, one line per attempt, never a builder line for the same seat. The codex gate books from the `execution_depth` its `codex exec --json` stream leaves (`gate_runner._book_gate_run_cost`), the kimi gate from the seat's report frontmatter in the governed lane's tail (`governed_lane._book_gate_cost`); a `--reprocess` run calls no model and books nothing. Both bill `subscription` with the list price in `api_equivalent_usd`, and a failed booking is a WARNING naming the ref, provider and reason that never changes the verdict.
- Gate depth (K2b-b): `ExecutionDepth` carries `cache_read_tokens` — the cached part of the reported input when the stream carried it (`turn.completed.cached_input_tokens`), `None` when it did not — so the codex gate books `input_uncached = input - cache_hit` instead of pricing the total input as uncached (OI-2407). A stream with no cached count books `input_uncached: null` with `input_cache_split_unmeasured`; a cached count larger than the input books `null` with `input_cache_split_invalid`, never a negative. `harness_run_cost.cli_run` gains the gate's two usage sources (`depth`, `report_text`); a kimi report not stamped `token_usage_measured: true` books `unavailable:kimi_cli_no_usage`, never three zeros.
- Gate goldens (K2b-b): `tests/fixtures/governed_lane/records/` regenerated — the only change in each of the 8 records is the new `execution_depth.cache_read_tokens` key.

- launchd (fleet PR-4): `com.vnx.receipt-processor`, `com.vnx.cleanup-reviewed-worktrees` and `com.vnx.ledger-health` now start through `scripts/launchd/vnx_pinned_launcher.sh` from both install paths (`vnx init`, `reload_plist.sh`) instead of `bash -c "cd ${VNX_HOME} && exec ..."`, and the launcher picks the interpreter from the script's extension (`*.sh` -> bash, `*.py` -> python3, anything else exit 78), so the bash receipt-processor supervisor can run on its project's pin.

- Cost ledger (K6a, track `kosten-per-model-in-het-grootboek`, ADR-049): a harness run that knows its session now books `transcript_identity` on its line at write time, the string `"<provider>:<session_id>"` built by the one `harness_run_cost.transcript_identity`, so a later backfill matches a line to its transcript instead of guessing which line belongs to which session (OI-2419). A run without a session writes the byte-identical record it wrote before, `record_id` included.
  Migration decision (OI-2426): no migration. Existing ledger lines are never rewritten — the absent field does not enter their `record_id` — and a legacy line receives an identity only through the K6b-2 backfill correction that supersedes it.

- Worktree salvage (WT-2a1a rebuild of #2291): `scripts/lib/worktree_salvage.py` bundles only
  the commits a worktree holds off `origin`, publishes each bundle by hard link, and appends one
  MANIFEST line; its `verify()` returns `[]` only when `git status` names no entry at all, every
  gitlink whose checkout exists is named even when the submodule is clean (its commits ride in no
  superproject bundle; an uninitialised gitlink is not named), HEAD is still the salvaged commit
  and every listed bundle unpacks to that commit in a scratch repo. The bundle is staged in a
  `tempfile.mkdtemp` directory of its own and hard-linked into place, so two writers of one
  destination never share a scratch path (OI-1486).

- ADR-051 legt de twee besluiten van track `fleet-release-and-rollout` vast: een geplande taak volgt zijn projectpin op runtime via één launcher die bij elke start opnieuw resolveert en met exit 78 weigert in plaats van stil naar een andere boom terug te vallen, en een release bewijst zichzelf met `vnx release verify` (twee toegestane afwijkingen), de default-branch-weigering in `publish` en de blokkerende doctor-check `release:main-drift`.

- Gate outcomes (track `gate-engine-consolidation`, GE-7, ADR-046 §2): the `wiring_gate` request is the last writer that stayed outside the one writer. It now books its outcome through `commit_gate_outcome` — one result record, one `review_gate_result` receipt carrying the head it judged, and the request's end — all under the result slot's lock. All three branches land there: no `gh` books `not_executable` (nothing decided, so ADR-046 books no outcome receipt; the request still ends terminal, so a retry is a new request), a `WiringGateError` books `fail` with the subprocess failure as the blocking finding, and a completed analysis books the deterministic adapter's own record. The request record now carries the request's lifecycle word (`completed`/`not_executable`); the payload returned to the executor still carries the gate's verdict.
- Wiring gate advisory mapping (GE-7): the record's status word is one of ADR-046 §1's four, so shadow mode (`VNX_WIRING_GATE_REQUIRED=0`) books `fail`, not a fifth word `advisory`. The reasoning: `advisory` is the merge policy's decision about what to do with a finding, and that decision already travels on the record as `required: false`, which is what `gate_executor`'s pre-booked branch reads, so the executor's required-failure count stays 0; the forge publisher maps `fail` to `failure` on `vnx-gate/wiring_gate`, a context no branch protection requires, and `wiring_gate` is not a review peer, so it can neither fill nor block `vnx-gate/review`. `pass` was rejected as a green verdict on a diff with findings (the fail-open reading the four statuses exist to prevent); `unavailable`/`not_executable` were rejected as untrue (the check ran) and as booking no receipt. An unknown verdict word fails closed to `fail`. Consequence: a shadow-mode finding now leaves a receipt and an `attempt_seq` on its slot where it used to leave none.

- regen-settings (fleet-release-and-rollout PR-5): `vnx regen-settings` now renders from the tree the project's `.vnx-version` pins (through `scripts/lib/pinned_engine.py`, the resolver launchd starts already use), not from whichever tree the CLI happens to run from; a pin nothing answers refuses the render with the resolver's one line instead of rendering from the running tree, and the worker-permissions merge follows the same tree.

- Cost ledger (track `kosten-per-model-in-het-grootboek`, K6b-1, ADR-049): the reading half of the backfill is back as `scripts/cost_ledger_backfill.py`, with one entry `scan(roots, since)` that turns the harness session transcripts into deduplicated `TranscriptRun`s (one per resolved path and per `<provider>:<session id>`), ordered on the run's end timestamp and then its path. The provider is the lane directory, the dispatch id comes from the transcript's `cwd`, and only tokens and timestamps leave a transcript. The module writes no ledger and has no CLI; K6b-2 plans and writes the backfill on top of it.

- Ledger readers (track `audit-spoor-kern`, K-2a / ADR-045 §2): the Python state and outcome readers now read `dispatch_finished` next to `task_complete`, through one helper.
  New `scripts/lib/ledger_end_line.py`: `end_line(records, project_id, dispatch_id, attempt=None)` returns the one end record of an attempt — `dispatch_finished` first, a legacy completion record as fallback, the highest attempt when `attempt` is `None`. `dispatch_started` and `report_ingested` are never an end; a report without an end line stays "report in, dispatch not ended". `end_status` projects a `dispatch_finished` status from `end_state` and never reads that line's raw `status`; `end_outcome` classifies through `event_outcome_semantics.classify_event_outcome`, so an absent, malformed or unknown `end_state` is never a success.
  The stream readers that filtered on a literal `"task_complete"` now use `event_outcome_semantics.COMPLETION_EVENT_TYPES`: `build_t0_state._build_queues`, `receipt_classifier.should_classify`, `state_rebuild_trigger.CRITICAL_EVENTS`, `stop_conditions._ATTEMPT_EVENT_TYPES`, `terminal_state_reconciler.COMPLETE_EVENTS` (and `dispatch_started` as a working signal; a late `report_ingested` no longer unfinishes a terminal), `pr_discovery`, `unified_state_manager` and `queue_reconciler.RECEIPT_TERMINAL_EVENTS`. Every legacy branch is kept: a ledger with only `task_complete` gives exactly the picture it gives today.
- Ledger readers, round 2 (K-2a / ADR-045 §2): `pr_discovery` now reads a `dispatch_finished` line's `receipt_done` from its `end_state` outcome (`ledger_end_line.end_outcome`), never the raw `status` — a lifecycle line without an `end_state` no longer marks a PR done, and the legacy `task_complete`/`"unknown"` branch is unchanged; `stop_conditions.check_provider_exhausted` resolves one end record per `(project, dispatch, attempt)` before taking its last-`threshold` window (the lifecycle line wins, a group of legacy-only lines stays separate, since the legacy vocabulary does not track attempts), so two failed attempts written as both a `task_failed` and a `dispatch_finished` line no longer fill a three-attempt exhaustion window.

- Dispatch selector (track `terminal-binding-afbouwen`, T-G1): the worker terminals (T1, T2, T3) lost their `VNX_ADAPTER_T{n}` selector. `deliver_dispatch_to_terminal` in `scripts/lib/dispatch_deliver.sh` sends them to `_ddt_subprocess_delivery` unconditionally, `_pdp_resolve_target` in `scripts/lib/dispatch_create.sh` always skips the tmux pre-flight for them, and the three Python readers (`headless_dispatch_daemon._is_terminal_headless`, `heartbeat_ack_monitor._is_subprocess_terminal`, `dashboard/api_operator._operator_get_agents`) take the rule from the new `scripts/lib/terminal_lane.py`. `VNX_ADAPTER_T1/T2/T3=tmux` is no longer read; `VNX_ADAPTER_T0` is untouched (T-B2 removes it).
- Rollback net kept (plan-gate point 1, measured): `subprocess_dispatch.py`'s `main`/`__main__` entry point and `_pool_heartbeat_loop` stay, because the hard-parked rollback route uses them — `pool_manager._pool_worker_command` starts `python -m scripts.lib.subprocess_dispatch` and `_ddt_subprocess_delivery` calls `scripts/lib/subprocess_dispatch.py` under `VNX_SINGLE_ENTRY_DISPATCH=0` or `VNX_DISPATCH_LEGACY=1`. `tests/test_dispatch_rollback_flags.py` pins one case per flag. Goal 1 of the track closes with that reason.
- Tests: `tests/test_dispatch_deliver_contract.py` is flipped (`=tmux` on a worker now counts one subprocess call and zero tmux calls) and gained the T0 `tmux`/unknown cases, `tests/test_t2_t3_subprocess_default.sh` is rewritten to pin "no opt-out" (its cases are a subset of the contract test, which is the behavioural pin), `tests/test_dispatch_rollback_flags.py` is new, and the suites that set the worker variable were retargeted at the new truth.
- Rollback import repair (round 2): `scripts/lib/subprocess_dispatch.py` puts its own directory on `sys.path` before the first sibling import, in the `pool_worker_runner.py` form, so `python3 -m scripts.lib.subprocess_dispatch` starts again with `PYTHONPATH` unset — that is exactly how `pool_manager._pool_worker_command` starts it under `VNX_SINGLE_ENTRY_DISPATCH=0` / `VNX_DISPATCH_LEGACY=1`. `tests/test_dispatch_rollback_flags.py` now drops `PYTHONPATH` from the subprocess environment so a builder's or CI's path cannot hide the break again.

- Receipt counts (track `deterministische-testverificatie`, nameting): a JUnit attribute listing echoed in a report is no longer read as a failing pytest run. `verification_runs._pytest_clauses` skips a count whose number is glued to a `key=`, so `tests=2593 failures=76 errors=0 skipped=3` no longer produces a run with 2593 failures and the highest-failure rule cannot promote the suite's test count into `tests_failed`. `dlv-0caf1cccbcc4` carried `5107/2514/2593` beside an XML of 2593 tests / 2514 passed / 76 failed; the same report now converts to `2590/2514/76`. A runner never writes `=N failed`, so no real summary is lost; the rule that a text-documented failure is never erased by the XML is unchanged.
- Nameting (track `deterministische-testverificatie`): `docs/investigations/2026-10-28-deterministische-testverificatie-nameting.md` measures the four goal points on three real PRs after #2310 (runs 40617587369, 40611781755, 40619858595): verdicts per test, the cause of both red runs, the three dispatch receipts against their JUnit XML, and the second-seat refusal of `#2319`.

- Kosten-per-model-in-het-grootboek K6b-2: `scripts/cost_ledger_backfill.py` now plans the harness-transcript backfill without writing — per transcript a correction on its live ledger line, a charge beside it, or nothing. A line is skipped only when it carries the run's `transcript_identity` and says the same thing; an identity-less legacy line is corrected so the write persists the match, and it is claimed only on exact tokens or an overlapping window, never as "the nearest line".

- Worktree salvage (WT-2a1b, OI-2417 points 2 and 4): `verify()` still refuses every tree state the
  salvage did not preserve, except the caches a worker's tree fills on its own. An IGNORED status
  entry is now covered when one of its path components is in `THROWAWAY_IGNORED_DIRS`
  (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `node_modules`) and no `.vnx-data/`
  or `.git/` component precedes it. Both shapes count — the directory entry (`pkg/__pycache__/`) and
  the file entry inside it (`pkg/__pycache__/m.cpython-312.pyc`). Nothing else widens: a tracked,
  staged, untracked or unmerged entry blocks whatever its path, an ignored `.vnx-data/` entry blocks
  (its own note file or a cache directory inside it), and so does an ignored `build/`. No caller, CLI
  flag or removal is added; submodule bundling (OI-2411) is still open.
- Worktree salvage (WT-2a1b, round 2): `verify()` now matches `THROWAWAY_IGNORED_DIRS` only against
  DIRECTORY components of an ignored status entry — every component but the last, plus the last only
  when git marks it a directory with a trailing `/`. An ignored regular FILE that merely shares a cache
  directory name (`node_modules`, `pkg/__pycache__`) stays uncovered and is named, because no bundle
  carries an ignored file and approving its loss would lose unpreserved data.

- Worktree-opruiming PR-6: `scripts/lib/branch_gc.py` ruimt lokale `dispatch/*`-branches op die geen werk meer dragen (gemeten 27-10: 497 branches tegen 41 worktrees). Een branch gaat alleen weg als geen enkele worktree hem uitcheckt — de main checkout inbegrepen, gelezen via `worktree_inventory.inventory` — en hij een ancestor van `origin/main` is of zijn PR `MERGED` is met precies de lokale kop in `headRefOid`, en er geen `OPEN` PR op staat. Zonder bruikbare `gh` verwijdert alleen het ancestor-bewijs; al het andere blijft met reden `gh_unavailable`. De CLI is dry-run tenzij `--apply`, hereleest elke kop vlak voor `git branch -D` (verschoven kop: `head_moved`) en raakt nooit een remote branch. Niets is aan launchd of `init_cmd.py` gekoppeld.
- Worktree-opruiming PR-6 ronde 2: `branch_gc.apply` verwijdert een branch met een compare-and-delete (`git update-ref -d refs/heads/<branch> <geverifieerde kop>`), dus een kop die tussen plan en delete verschoof wordt geweigerd en blijft staan met reden `head_moved` in plaats van mee verwijderd te worden; de checked-out-set wordt vlak voor elke delete opnieuw gelezen (update-ref kent geen worktrees) en een branch die een worktree dan uitcheckt blijft met reden `checked_out`, terwijl een onleesbare inventaris `BranchGcError` blijft.
- Worktree-opruiming PR-6 ronde 3: `branch_gc.main` bepaalt de standaard repo-root via `git -C <map van dit script> rev-parse --show-toplevel` in plaats van `resolve_project_root(__file__)` (OI-1145 vorm 3a, dezelfde route die ADR-045 K-1 r3 voor `writer_sha` koos); een mislukte git-vraag blijft een nette exit 2 met één regel op stderr en `--repo-root` werkt onveranderd.

- Dispatch state machine (track `audit-spoor-kern`, K-3a, ADR-049): `transition_dispatch` and `transition_dispatch_idempotent` take an optional `project_id`. When it is given, the read, the `UPDATE`, the returned row, the no-op event and the transition event are all scoped to `dispatch_id = ? AND project_id = ?`, so a transition never changes another project's row that carries the same dispatch id; an `UPDATE` that touches no row raises the existing not-found error instead of reporting success. A `project_id` that is `None`, empty or not a string is refused rather than read as "no project". Callers that do not pass it keep the dispatch_id-only behaviour; `end_attempt_on_reject` is the one that already passes its project.

- Cost ledger (K3-1a, ADR-043/ADR-049): a ledger line is now booked on an explicit execution identity. `cost_ledger.record_once(project_id, producer, execution_id, make_run, ...)` writes one line for `(project_id, producer, execution_id)` under the ledger's own lock and returns the line already booked instead of writing a second one; the line's `run_id` is derived from that key alone, so no slot counter can duplicate a run or swallow one (the K3 round-4 failure). A missing, empty or non-`uuid4().hex` `execution_id` is refused with `InvalidExecutionId` before any lock is taken. `harness_run_cost.AttemptRun` now carries the required `execution_id`; `record_attempt_run` books through it, a door refusal books `unavailable:<reason>` at amount 0, and a `never_started` attempt books nothing. `ledger_project_id` reads the tenant from the store path first, so an ambient `VNX_PROJECT_ID` never overrides a store that names its own project.

- Fleet rollout (PR-8, ADR-051): `vnx preflight [--project <id>]... [--json]` measures per project whether a rollout of the pinned release can go ahead, and changes nothing — no install, no reload, no write to a settings file, a plist or a store. Four blocking classes: `pin` (a dead pin or a pinned release dir that fails `vnx release verify`), `launchd` (every `check_pin_alignment` violation, the state name kept literally), `hooks` (hook commands a `regen_settings.sh --merge --dry-run` render would change, old and new named; a failed dry-run is itself a finding) and `main-drift` (the doctor's `release:main-drift` check, skipped outside a git checkout). Exit 0/1/2; a project without a pin lists `pin` and `launchd` as unmeasured, and shim-routed project hooks are one info line, not a blocker.
- `vnx preflight` verifies the release dir the pin actually resolves to, not the raw pin string: a pin written `1.0.0` installed at `versions/v1.0.0` (OI-2270) now passes `vnx release verify` instead of being reported as a missing dir, while a pin with no installed dir stays one `dead` finding.
- `vnx preflight` now finds its engine through its owner (`vnx_cli._engine.engine_root()`): a pip install looks for the `regen_settings.sh` wrapper under `vnx_orchestration/` instead of `site-packages/`, so no project reports a missing wrapper; `--project` resolves exact project ids first, and an alias two projects share is refused naming both instead of measuring the first.

- Release and rollout docs (fleet-release-and-rollout PR-9): the install-central runbook now uses `vnx regen-settings --merge` for an existing project and gains a "Before a rollout" section for `vnx preflight`, `vnx release verify` and the `release:main-drift` doctor check; `docs/core/LOCKS_AND_RELEASES.md` gains rows for those controls and the pinned launcher refusal.

- Fleet release nameting (fleet-release-and-rollout, dispatch dlv-ffb65a8df377): the ten claims are measured again on `origin/main` at `71e357c0` — five `geleverd`, four `operatorhandeling`, one `open`; `vnx doctor` fails only on `release:main-drift` (137 commits), and `vnx preflight` exits 1 with 62 blocking findings (0 pin, 7 launchd, 51 hooks, 4 main_drift), each mapped to the operator action that clears it.

- Worktree salvage PR-2a2 (`scripts/lib/worktree_salvage.py`): `salvage()` now also preserves a worktree's staged and unstaged changes to tracked files as `<name>.staged.patch` and `<name>.unstaged.patch`, staged in a `tempfile.mkdtemp` directory and hard-linked into place like the bundle, and listed on the MANIFEST line. `verify()` proves each patch by applying it in a scratch checkout of the head that borrows no object store: the staged patch must reproduce the tree's own index tree (`git write-tree` through a temporary `GIT_INDEX_FILE`, so the worktree's index is never written) and the unstaged patch must reproduce every changed file's bytes, type and exec bit. A failed apply, a mismatch or a change with no patch listed is a named problem; untracked files, unmerged paths and populated submodules still fail closed.
- Worktree salvage verify (PR-2a2 follow-up): `verify()` now compares every path the worktree itself changed against its index as well as every path the applied unstaged patch changes, so a tracked change made after the salvage that the saved patch never mentions (e.g. an edited `.gitignore`, a deleted file) is a named problem instead of escaping verification; an unreadable worktree diff is a named problem, never a pass.

- Fleet release and rollout docs: the README names `preflight` next to `providers` and `guard` as on main but not yet released, and the central-install runbook says which binary carries these commands — the fabric shim `~/.vnx-system/bin/vnx` or `./bin/vnx` for `regen-settings` and `bootstrap-hooks`, a pip CLI installed from a main checkout for `preflight` and `release verify` — that the deployed v1.7.6 pip CLI has neither, and how to check with `command -v vnx` and `vnx --help`.

- Cost ledger (track `kosten-per-model-in-het-grootboek`, K6b-3, ADR-049): `vnx costs backfill` executes K6b-2's plan — a new charge as one line through the ledger's writer, a correction through the correction path (`supersedes` plus the reason, ADR-038), a skip as nothing — so a second `--apply` over the same transcripts writes nothing. A run whose dispatch row names no purpose books under the closed set of `unknown:<reason>` roles (`unknown:no_dispatch_row`, `unknown:no_ledger_role`; any other suffix is refused by `cost_ledger`). The dry run is the default and prints one summary per lane (runs, new lines, corrections, skips, tokens); a refused line exits non-zero with the reason and leaves the lines already written as they were.
- Cost ledger (K6b-3 round 2, ADR-049): a backfill charge's attempt number is re-checked under the writer's own lock, so two backfills of one dispatch can no longer write two transcripts' spend under one `run_id` (which the aggregate collapsed to the last line). `cost_ledger.record_run` refuses a `backfill=True` charge whose `run_id` already has a line — in the ledger or in its spool — with the new `BackfillRunIdTaken`, nothing written; live producers and corrections keep their behaviour. `cost_ledger_backfill.apply_runs` re-reads the ledger, renumbers the charge past every attempt the ledger and the plan's own reservations hold, and writes again (at most `BACKFILL_RETRY_LIMIT` times; past that it is a failure), and a correction is never renumbered. A lock file that cannot be taken stops the run with nothing written, now covered by a test.
- Cost ledger (K6b-3 round 3, ADR-049): the same transcript is booked once. A backfill charge is refused with the new `cost_ledger.BackfillTranscriptBooked` (carrying the line that holds it) when a line with its `transcript_identity` already exists in the ledger or in the spool waiting for the replay — checked under the writer's own lock, before the `run_id` check. `cost_ledger_backfill.apply_runs` compares usage and amount with that line: equal counts as already booked (a skip, not a failure), different is written as one correction on that line's `run_id` so the aggregate shows the new usage once; a charge is never renumbered for it. A backfill charge that names no transcript identity is never renumbered either: on a taken `run_id` it fails with one stderr line naming the run and writes nothing. Live producers and corrections are unchanged.

- Fleet rollout (OI-2456): `bin/vnx preflight` now delegates to the pip CLI's `vnx_cli.main preflight`, like `release`, and `preflight` joins the shared starter/operator mode tier, so the central shim runs the read-only rollout measurement instead of refusing it with `Error: 'preflight' requires a different mode`.

- Worktree salvage PR-2b (`scripts/lib/worktree_salvage.py`): `salvage()` now also preserves the
  work a tree holds outside the index as `<name>.untracked.tgz` plus `<name>.untracked.list`, and
  `verify()` proves both. The archive carries every untracked (`?`) and every non-throwaway ignored
  (`!`) status entry; entries below `.vnx-data/` or `.git/` stay uncovered and keep blocking removal
  as evidence, an ignored throwaway cache stays excused, and an untracked symlink into the checkout
  (`worktree_inventory.is_internal_symlink`) is covered without being carried. A directory entry —
  git reports a nested repository as one `nested/` record — is walked, so every file and symlink
  inside it, a nested `.git` included, lands in the archive; an empty directory is a `d` member of
  its own and a symlink is archived as a symlink, never followed. The list holds one NUL-terminated
  `kind size sha256 path` record per member, NUL-safe for any filename. `verify()` parses the list,
  opens the archive, proves both hold the same members with the same kind, size and sha256, and
  re-walks the tree: a listed file edited or deleted after the salvage, a member the tree gained and
  the list never carried, an archive without its list or a list without its archive are each a named
  problem. This is the half of OI-2361 the closed #2214 failed: the archive held only the directory
  record, so a removal would have deleted `nested/precious.txt` while verify passed.
- Worktree salvage PR-2b round 2 (`scripts/lib/worktree_salvage.py`): the untracked archive keeps the
  permission bits of every regular file as they are on disk (`stat.S_IMODE` of the `lstat`, setuid,
  setgid and sticky masked out), so an untracked `run.sh` at 0755 restores executable instead of
  being flattened to 0644; directories and symlinks keep their fixed modes. The list record gains a
  mode field (`kind size sha256 mode path`) and `verify()` names a member whose archive mode or
  on-disk mode differs from the recorded one, and names a list written before modes were kept
  ("list without modes") instead of passing it. An archive truncated to half its bytes is a named
  problem too: `EOFError` and `zlib.error` while reading it are translated like the other read
  failures, never raised at the caller.

- Backfill ledger (K6b-3b, OI-2455): `BackfillTranscriptBooked` compares with the effective line of the `supersedes` chain, in the ledger and in the spool, so a repeated backfill of a grown transcript adds no second correction and a transcript back at an older usage is corrected back instead of reported as booked.

- Receipt ledger (track `audit-spoor-kern`, K-3b, ADR-045 §3): the end-line contract now lives in the append layer. `append_receipt_internals.lifecycle.prepare_end_line(line, ledger_path=...)` runs the caller's line through the append layer's own stamps and validation — one extracted `payload._prepare_receipt` path, not a copy — and returns the canonical line an end intent may freeze, with the append layer's derived stamps (`DERIVED_STAMP_FIELDS`) removed and the caller's `model` already normalised; a line the append would refuse raises with the same code and writes nothing, so an intent is never claimed for a payload that could not be written. `decide_end_line` replaces K-1's "first line wins" with the intent rule: accepted only when `state/dispatch_end/<project_id>/<dispatch_id>.<attempt>.json` exists, is well-formed, and its canonical line (sha256 included) is byte-equal to the receipt on every field outside the derived stamps — an extra caller decision such as `no_push` is `end_line_mismatch`, while a caller-supplied `ingested_at` or a normalised model is not. `end_intent_path` refuses an identifier that is not one safe path component, or a path resolving outside `dispatch_end/`, before anything is created. `write_end_intent` freezes the intent exclusively (temp file plus `os.link`); K-3c's finalizer only calls it.
- Receipt ledger (K-3b round 2): a frozen end line now stays frozen through the final write. The three warning keys the pre-write hook rewrites from lock-time state — `destination`, `oi_id`, `reason`, set by `warning_destination.commit_destination` — are `DERIVED_WARNING_FIELDS`: `project_end_line` drops them from both the frozen line and the comparison, while `code`, `severity`, `message` and `requires_tracking` stay compared. `idempotency._write_receipt_under_lock` re-checks the final `dispatch_finished` line against its intent after `pre_write_hook` runs (`lifecycle.verify_final_end_line`), so a hook that changes a non-derived field refuses with `end_line_mismatch` and writes nothing — what is persisted is what was frozen.

- Kosten-per-model K3-1b (ADR-049): every plan-gate seat dispatch, every verdict re-extraction and every plan-gate / review-cap tiebreaker call now books exactly one cost-ledger line through K3-1a's `record_attempt_run`, on an execution id (`uuid.uuid4().hex`) minted where the run starts — so a report read again, a re-parsed verdict or a re-render books nothing new, while a retry or a second tiebreaker attempt is a new line. A tiebreaker refused at the door (`delivery_refused`, `paid_lane_daily_budget_exceeded`) books a confirmed zero with the door's reason; any other raised lane books `unknown`. The seat's name rides the ledger producer (`plan_gate-<label>`); the tenant is the store's own (ADR-007), never the ambient project, and an unnamed `project_id` now resolves from the data dir. A refused booking logs one WARNING and never changes the seat's or the tiebreaker's verdict.

- Review gates (K3-2a, track `kosten-per-model-in-het-grootboek`, OI-2444): every review-gate seat run now gets an `execution_id` at the moment it starts, and every record of that run carries it. `gate_status.new_execution_id()` mints the id (32 lower-case hex, `uuid4().hex`) and is the only minter; `gate_status.EXECUTION_ID_FIELD` names the record field next to `EVIDENCE_SOURCE_FIELD`. The gate runner mints it in `GateRunner._mark_executing` — after every "this gate will not run" refusal and before the first model call of the subprocess, vertex and harness lanes — and persists it on the request, so the success record, a failure record and a not-executable one all carry it through `gate_recorder.stamp_request_identity`. The standalone governed gates (`governed_lane.run_gate_head`) mint it before the dispatcher is built and carry it on `GateRunState` into the record they build. A takeover run is a run of its own and gets its own id; nothing derives an id from the result slot, `attempt_seq`, a timestamp, the PR, the head or the report — deriving it from the slot is what made K3 book three cost lines for two executions. `--reprocess` calls no model and is no run: it carries the id the original run recorded with the report it rebuilt (`reprocess_original_execution_id`, addressed by `dispatch_id`) and none when that run recorded none. K2b's line for a live CLI run writes the same id (`harness_run_cost.CliRun.execution_id`, `cost_ledger.RunCost.execution_id`), so K3-2b can see the run already has its line. This PR books no cost line and changes no amount.
- Review gates (K3-2a round 3, track `kosten-per-model-in-het-grootboek`, OI-2444): a refused retry no longer inherits the previous run's `execution_id`. A request record left over from an earlier run (an `unavailable` attempt) still names that run's id, and `GateRunner.run` used to copy it onto the `not_executable` record of a run that never started — so a refusal could have been booked as the earlier execution in a ledger keyed by `execution_id`. `GateRunner.run` now drops the inherited id from the request payload before its first refusal check; `GateRunner._mark_executing` stays the only minter, at the transition to `executing`, and every record after that transition still carries the run's own id. The governed lane already guarded this shape (`_build_record`/`_stamp_and_persist` carry only `GateRunState.execution_id`), and `--reprocess`'s deliberate reuse of the original run's id is unchanged. No record field, status or amount changes.

- Review gates (OI-2453): a second `request_reviews` of a seat whose takeover reviewer already decided on the head no longer dies with `KeyError: 'failure_reason'`. The `review_gate_request` receipt copies `failure_reason`/`takeover_from` from the standing takeover record only when the record carries them, so a decided PASS/FAIL is reported and its receipt written instead of crashing the round.

- Review gates (track `gate-engine-consolidation`, GE-8): the orphan-request cleanup now books its outcome through
  the one gate writer. `scripts/cleanup_orphan_gates.py` resolves a `review_gates/requests/` record older than 24 h
  with no result as `unavailable` — there is no `abandoned` outcome (ADR-046 §1) — through
  `gate_outcome.commit_gate_outcome`: one record under the slot's lock, its `review_gate_result` receipt and the
  request's terminal end. The record carries `reason: abandoned_orphan_request`, `abandoned_by`, the age in hours and
  the source request stem. A slot that already holds a record is reported as already resolved and left alone, and a
  dry run writes nothing. The store is resolved through `project_root.resolve_data_dir` (`VNX_DATA_DIR` needs
  `VNX_DATA_DIR_EXPLICIT=1`), never a repo-relative `.vnx-data` fallback. The writer guard
  (`tests/test_oi1472_residual_guard_writers.py`) now covers the module, and `commit_gate_outcome` gained one opt-in,
  `book_undecided_receipt`, so an `unavailable` record can book its receipt in the same commit; every other caller
  keeps ADR-046 §1 (only a decided pass or fail books an outcome receipt).

- Review gates (track `gate-engine-consolidation`, GE-8 round 2): the orphan-request cleanup revalidates the request
  under its own lock, inside the result slot's lock, before it commits — the identity (`dispatch_id`, `commit_sha`,
  `requested_at`, `pr_number`, `pr_id`) and the age are compared with what the scan saw, and a request a retry
  replaced is reported as `skipped_replaced` with no record and no request end, the request written back being the one
  read under the lock rather than the scan's copy. A scan also finds a request a failed step-7 write left `executing`
  next to the cleanup's own `unavailable` record and finishes that same request through the same writer: the record
  is handed back unchanged, so step 6 reads it as a repeat and no second record or receipt is written. To make the
  two-lock critical section possible, `atomic_io.slot_lock` is now reentrant within one process (a `fork` resets it).

- Review gates (track `gate-engine-consolidation`, GE-8 round 4): the orphan cleanup's record carries the request's
  `dispatch_id` and `commit_sha` on its top level, so the `review_gate_result` it books dedupes on a stable key
  instead of the emission timestamp — a crash after the append and before clearing `receipt_pending` now yields one
  receipt after recovery, not two. The recovery scan also picks up a request that has ended while its record still
  carries `receipt_pending: true`, so a transient append failure is paid by the next run instead of being stranded
  between the orphan scan (the slot has a result) and the ended request.

- CI (track `ci-profile-a-xdist`, OI "Main-CI rood door runner-image"): all 12 jobs in `.github/workflows/vnx-ci.yml` and both jobs in `.github/workflows/public-ci.yml` now pin `runs-on: ubuntu-24.04` instead of the moving `latest` runner label, which was bumped to the 26.04 image and turned main red on run 41079080194 — four profile-a suites died inside `install-central` with `BASH_SOURCE[0]: unbound variable`, and `setup-python` has no 3.9 build for 26.04 ("The version '3.9' with architecture 'x64' was not found for Ubuntu 26.04"). No test or script changed; the pull request's own VNX CI run is the proof. The pin comment above the first job names the way back.

- xdist cutover rule, wall-time bound (track `ci-profile-a-xdist`, D4a; T0 decision OI-2442): the shadow's suite wall time is now measured against the serial reference of the same run instead of a fixed 15 minutes. `scripts/ci/xdist_parity_history.py` replaces `MAX_SUITE_WALL_S` with `MAX_SHADOW_SHARE = (2, 5)`: a run is within the bound when the shadow took at most 40 percent of the reference's `duration_s`, compared in whole numbers (`5 * shadow <= 2 * reference`), both read from the run's own verdict. The per-run reason reads `shadow suite <s> s > 40% of reference <r> s`. A verdict without a whole-number `reference.duration_s` blocks with `no reference duration`, exactly as a missing shadow wall time blocks with `no shadow suite wall time`, and the `suite_wall_time` condition names the share it applies. The old bound was measured against a 35.4-minute serial Profile A; the suite has since passed 30,000 tests and the serial reference takes 42 to 50 minutes, where 15 minutes meant 42 percent and now meant 34 percent.
- xdist cutover rule, wall-time bound round 2 (track `ci-profile-a-xdist`, D4a; T0 decision OI-2442, amended
  2026-10-28): the bound is one half of the run's own serial reference. `MAX_SHADOW_SHARE` in
  `scripts/ci/xdist_parity_history.py` moves from `(2, 5)` to `(1, 2)` (`2 * shadow <= reference`), so a run is
  within the bound when its shadow's `shadow.suite_wall_s` is at most 50 percent of its own
  `reference.duration_s`. The per-run reason reads `shadow suite <s> s > 50% of reference <r> s`, the
  `suite_wall_time` condition names `(1/2)`, and a shadow at exactly one half is within. Round 1 measured the
  40 percent bound and six runs of the window sat between 40.6 and 44.5 percent of their own reference, so that
  bound could never hold on runs the window keeps; one half means the parallel shape at least halves Profile A,
  a plain, explainable target that is not tuned to the 44.5 percent maximum. 1300 s beside 3000 s (43 percent)
  is now within; 1600 s beside 3000 s (53 percent) blocks.

- Cost ledger (K4b): the conversation analyzer's `claude` and `deepseek-harness` lanes and the dream
  cycle's kimi call now write one ledger line per call (role `intelligence`, ADR-043). The analyzer
  books through `harness_run_cost.record_classifier_run` with producer `conversation_analyzer`; the
  dream cycle books through `record_kimi_run` with producer `dream`. `record_classifier_run` gains a
  `producer` keyword (default `classifier`) so the two intelligence producers stay apart.
- Cost ledger (K4b): the line follows the call, never the hope. Both analyzer lanes pass a fresh
  `--session-id <uuid4>`, so the tokens come from that call's own transcript, and the model is the one
  the CLI reports (`model`/`modelUsage`), falling back to `VNX_ANALYZER_CLAUDE_MODEL` (default
  `sonnet`) only when the child named none. A child that never started books
  `cost_source=unavailable:spawn_refused` with five confirmed zeros (`missing_cli`, or the
  deepseek-harness pre-spawn statuses `key_missing`/`balance_low`/`balance_unavailable` and an unsafe
  harness config dir); a call that ran and left no transcript books `unknown:no_transcript`, never a
  zero. dream's kimi stream carries no usage accounting, so it books `unknown:kimi_cli_no_usage`.
- Cost ledger (K4b): ollama is not booked. A local model is neither `metered` nor `subscription`, and a
  third billing class is out of scope here; the analyzer's local lane writes no line.
- Cost ledger (K4b): the analyzer's line follows the session, not the process. `_book_analysis_call`
  forwards the project of the session being analysed to `record_classifier_run`, so a session of
  another project is never attributed to this process's `VNX_PROJECT_ID`/`vnx-dev`. A session that
  names no project is not booked at all — the ledger has no unknown-project field under ADR-007 —
  and one WARNING names it.
- Cost ledger (K4b): a refused ledger write never changes the analysis or the consolidation result —
  the booker logs its WARNING and the caller continues with the outcome it already has.

- Review gates (OI-2476, track `tweede-stoel-op-signaal`): every verdict line in `review_gates/rounds.ndjson` and every `review_gate_result` receipt now carries `warning_count` and `prescan_notice_count`, read from that verdict's own report. The pre-scan's `instruction-like text in diff` findings count as notices, never as warnings. A missing count (an old line, a missing or unreadable report, a takeover) stays unknown and is not written as zero. No merge decision changes.

- Analysis readers (track `audit-spoor-kern`, K-2c, ADR-045): `cost_tracker`, `traceability_audit`, `cqs_calculator`, `dispatch_lifecycle_tracker`, `t0_intelligence_aggregator`, `intelligence_daemon`, `memory_consolidator`, `generate_t0_recommendations`, `link_sessions_dispatches` and the dashboard token feed now read a `dispatch_finished` line next to `task_complete`, through the new `scripts/lib/completion_reading.py`: the completion set is the canonical `COMPLETION_EVENT_TYPES`, and a v3 end line's outcome is `classify_event_outcome`'s answer over its `end_state`, never its projected status field. An end line whose `end_state` is missing, `None`, malformed or unknown is no success anywhere. `dispatch_started` and `report_ingested` are never a completion and never a cost line. A ledger of legacy lines reads exactly the numbers it read before. The `backfill` scripts are deliberately untouched: history stays what it was.
- K-2c round 2: the lifecycle tracker classifies every canonical completion type through `completion_outcome` before it counts (a `blocked` completion is FAILED, a `timeout`/unknown/missing status is no outcome and no longer swallows the next terminal receipt), and the completion readers count one completion per `(dispatch_id, attempt)` via `completion_reading.count_completions`/`dedupe_completions`, so a legacy `task_complete` plus its `subprocess_completion` lane line reads as one.

- Receipt processor, shell readers (track `audit-spoor-kern`, K-2b, ADR-045): every shell filter that accepted `task_complete` as the end of a dispatch now accepts `dispatch_finished` next to it, and keeps its legacy branch byte-for-byte — the move from `dispatches/active/` to `completed/`, the `terminal_state.json` shadow write, the dispatch outcome/CQS update, the lease release, the pattern-usage success credit, the track close in `progress_state.yaml`, the contract-claim verification, the pattern-adoption recording, and the T0 brief's completed-last-hour counter. `dispatch_started` and `report_ingested` stay outside every filter: neither is an outcome.
- Receipt processor (K-2b): one helper, `scripts/lib/receipt_processor/rp_end_line.sh`, is the single shell read of an end line. It asks `event_outcome_semantics` (one `python3 -c` call, module imported from the resolved project root via `lib/vnx_resolve_root.sh`) for the status an `end_state` projects to, so shell and Python cannot drift into two mapping tables. A `dispatch_finished` line's own `status` field is never believed: an `end_state` that is missing, empty or outside the closed set yields no status and no success, so pattern learning, the track close and the brief's counter all decline it, and the dispatch outcome update records nothing rather than an invented status.
- Receipt processor, end-line helper (`scripts/lib/receipt_processor/rp_end_line.sh`): a failing `python3` helper is now a failure instead of an empty status. Its stderr and exit status are captured (`end_line_helper_failed`, helper exit status and first stderr line in the processor log and on stderr), `rp_end_status`/`rp_end_outcome` return 2 with nothing on stdout when it fails, and every caller (`extract_receipt_fields`, the pattern success credit, the track close, the dispatch-outcome update) handles that return explicitly — a `dispatch_finished` whose helper died has no status and no outcome, so no completion hook runs and it is never counted as a success. A legacy `task_complete`/`task_failed`/`task_timeout` keeps its exact behaviour, and a valid line whose `end_state` simply does not resolve still returns `none` at exit 0.

- Terminal-binding-afbouwen T-B2 (P15b): a receipt no longer carries `terminal_snapshot`, and `t0_state.json` no longer has a `terminals` block. `_enrich_terminal_snapshot` and its call leave `append_receipt_internals/enrichment.py`, `is_headless_t0()` leaves `append_receipt_internals/common.py` and the `append_receipt.py` re-exports together with `collect_terminal_snapshot`, and `build_t0_state.py` loses `_build_terminals`/`_terminal_record_from_lease`/`_TERMINAL_TRACKS` plus the brief adapter's `terminals` reader. A receipt's identity comes from its dispatch row and "what is running" is `live_work`; ADR-042 records that decision as its 2026-10-28 amendment. `vnx status --terminals` now prints live_work instead of the removed block.

- Cost ledger ceiling (track `kosten-per-model-in-het-grootboek`, K5c, ADR-043 §8): the daily paid-lane ceiling now switches from the receipts to the cost ledger only once a measurement gate over the ledger itself passes. `scripts/lib/cost_ledger_meetpoort.py` answers `evaluate(ledger_path, today)`: the five producer roles (`builder`, `review`, `plan_gate`, `panel`, `intelligence`) each have a dated line, the last one arrived at least seven full UTC days before today, and the window of those seven days holds no `metered` line with an `unavailable:*` cost source — except the documented kimi exception (no usage source) and a refused spawn (a measured zero); `unavailable:no_transcript` is never an exception. No state file and nothing to flip: the gate is re-evaluated per call and its answer is the evidence. Once it passes, `paid_lane_budget` sums today's effective metered paid-lane runs from `cost_ledger.aggregate_runs` (a correction counts once at its new amount) and counts an `unavailable` run as unknown, never as 0; while it has not passed, the receipts stay the answer and the door warns once per call with the gate's reason. Fail-closed means the old path, never a lower number: an unreadable ledger, an evaluation error or an incomplete read (a run waiting in the spool) keeps the receipts sum and says why.

- Gate obligation runner (track `gate-runner-github-quotum`, OI-2459): a tick now reads every PR and `dispatch/*` branch fact from one lazily built snapshot — at most one `gh pr list --state all` and one paginated `gh api .../git/matching-refs/heads/dispatch/` — instead of one call per obligation. A failed or truncated snapshot degrades to the per-call paths unchanged, and a failed branch listing still answers `None` ("unknown, wait"), never `False`. The head AFTER a gate run is always read live (`gh pr view --json headRefOid`), never from the pre-run snapshot, so a fix-forward push that lands during a run can no longer book a verdict about superseded code; the pre-run baseline uses the snapshot only while its PR data is younger than 60 s. The per-obligation fallback is capped at 20 calls per tick (past the cap, and after a rate-limit refusal, every lookup answers unknown = a wait) and the module docstring states the tick's worst-case call formula. The snapshot only FILTERS: every decision that books a terminal status on a snapshot-derived fact — evidence rescue, retire on `MERGED`/`CLOSED`, retire on a branch gone from the listing — confirms that fact with exactly one live read immediately before the booking, bounded at 20 live confirmations per tick; a live answer that differs (or cannot be read) books nothing. A lookup the tick's budget skipped (fallback cap, rate-limit stop, live-confirm cap) is now a distinct `DEFERRED` outcome instead of the `None` that means "could not be determined": a deferred obligation gets no write, `attempts` does not move, and it can never escalate to `not_executable` through deferrals, while a genuine failure (gh missing, timed out, unrecognised reply) keeps the existing unresolvable/escalate path.

- Reviewed-worktree sweep maakt oud werk echt opruimbaar (worktree-opruiming-structureel, dispatch dlv-6fb36d30388e): `cleanup_reviewed_worktrees.py` krijgt `--max-age-days N` (leeftijdsgrens via dezelfde maat als `worktree_inventory`) en `--include-dirty`, dat eerst `salvage` draait, daarna `verify` op wat salvage schreef, en de boom alleen verlaat als die verificatie slaagt — nooit andersom. Die controle loopt op elk meetmoment opnieuw, ook bij een schone `git status`: ignored bestanden archiveert de salvage wél en een gewijzigd ignored bestand ziet die status niet, dus een schone status bewijst niets over wat de salvage vasthield. Bomen met een open PR, een levend (of onmeetbaar) proces of een onmeetbare gh-lookup blijven staan met hun reden; elke verwijdering schrijft een rapportregel plus het bestaande `provider_teardown_worktree`-event. Salvage-artefacten en hun map worden expliciet owner-only (0600/0700) aangemaakt, en een afgekapt list-record is een benoemd `SalvageError`-probleem in plaats van een crash. De launchd-template houdt geen van beide vlaggen.

- Skill delivery is measured on the receipt (track `refactor-skill-en-levering`, RS-3, ADR-048 §3): `skill_applied`, `skill_source_path` and `skill_unapplied_reason` are optional v2 receipt fields stamped by the envelope and provider lanes. `skill_application.verify_skill_applied` reads the resolved `SKILL.md` at delivery time and proves its body (frontmatter stripped) occurs literally in the final prompt; a spec without a skill keeps all three fields absent, an unresolvable skill records `resolution_failed: <reason>`, and a reviewer binding records `reviewer_bound`. No schema-version bump and no migration: an absent field reads as "no skill requested".

- Cost ledger (track `kosten-per-model-in-het-grootboek`, K3-2b): `gate_outcome.commit_gate_outcome` now books the one
  cost-ledger line of a gate run at the moment the run ends, whatever the slot write did — a new record, a repeat and a
  refused overwrite alike — so a run that called a model is never invisible and never booked twice. The identity is the
  record's own `execution_id` (`gate_status.EXECUTION_ID_FIELD`), not the slot's `attempt_seq`: the closed K3 booked at
  `attempt_seq` and produced three lines for two executions in its round-4 reproduction (#2329, OI-2444). A run that
  provably never started books nothing (no id is by design there, so no warning either), `--reprocess` evidence books
  nothing, a run K2b already measured at its lane's own end gets no second line, and a record that names no model run
  (`ci_gate`, a `gh` read) books nothing. A run that did start and names no valid execution id books nothing and is
  never guessed: one WARNING naming the dispatch and one count under
  `harness_run_cost.UNBOOKED_NO_EXECUTION_ID`. A refused booking logs and the verdict stands.
  `gate_status.new_execution_id`/`is_execution_id` is now the one minter and one format check — `cost_ledger` asks it,
  and the plan-gate and tiebreaker mints ask it instead of `uuid.uuid4().hex`.

- Review seats (track `tweede-stoel-op-signaal`, PR 2): `review_seat_evaluation.seats_for_signals` computes, from one typed input, whether a head needs only seat 1 or every seat — on a real codex warning, a security or schema path, `irreversible`, or a deterministic sample of the head, and fail-closed to every seat on any `UNKNOWN` field, no seat-1 outcome, a seat-1 FAIL or a seat-1 takeover. The merge door computes it in shadow on every dry run and every merge and records `shadow_seat_rule` (rule name, seats, signals, unknown fields, `head_sha`) in the dry-run JSON and the `pr_merged` receipt; the door's decision stays the one of `RISK_SEAT_RULE`, unchanged.

- Analysis readers (OI-2468, track `audit-spoor-kern`, K-2c fix-forward): `cost_tracker.build_metrics` and the T0 recommendation engine now run their completion lines through `completion_reading.dedupe_completions`, so one dispatch attempt whose `task_complete` and `dispatch_finished` both carry the same 150 tokens is booked once, not twice, and the engine emits one recommendation per attempt instead of one per completion line. The cost census (`event_counts`) still counts every raw line, and `subprocess_completion` stays excluded from booking where it only witnesses its own attempt, so a legacy ledger keeps exactly the numbers it had.

- Review gates (review-cap tiebreaker, RG-4): a standing `mergen-met-OI` verdict now counts at the merge door on the head it was decided on, and nowhere else. It fills the codex seat only when an `operator_approval` names that verdict id, the PR number and the head (one approval per use), and only when the aftermath is booked complete: every remaining blocker is an open item under `review-cap:<project>:<verdict>:blocker-<n>` and the `merge_ready` record carries `open_items_written`. A half-written aftermath, a missing approval, a moved head or a half-named approval is a NO-GO whose refusal says which of the three is missing. The verdict lifts the codex seat's veto and nothing else: on a high PR the kimi seat still needs its own PASS and every other FAIL stays a veto. Before a merge under the verdict (or under `--override-b3`) the door republishes `vnx-gate/review` for that head through RG-2's `refresh_review_summary` and refuses, naming the check, when that publication does not go out. `docs/core/DISPATCH_RULES.md` §3 records the env carrier (`VNX_OVERRIDE_B3`/`VNX_OVERRIDE_B3_REASON`, reason required) and these conditions.
- Review gates (RG-4 round 2, codex findings on `571d75c3`): a verdict only stands inside the current count — a `review_cap_verdicts` database row created before the PR's last attested `opened` line no longer counts at the merge door, exactly as a receipt-only verdict never did; and the door clears only when the `vnx-gate/review` check it republished actually concluded `success` (the publisher now returns the payload with its conclusion; a published `failure`/`action_required` keeps the door NO-GO).
- Review gates (RG-4 round 3, Kimi gate on `642017a7`, OI-2506): track closure now judges a merge record that carries the door's `mergen-met-OI` verdict the way the door did — the same evaluation fills the codex seat and lifts its veto, only on the head the verdict names. A verdict naming another head fails closure with a message naming both heads, and a record without the block judges exactly as before. A `--dry-run` merge no longer publishes the refreshed `vnx-gate/review` check-run, and a verdict-driven refresh labels its evidence with the verdict id instead of calling it a B3 override.

- Ledger lifecycle (ADR-045 K-3c, track `audit-spoor-kern`): `scripts/lib/dispatch_lifecycle.py` adds `finalize` and `recover`, the two callers the end-line contract was waiting for. Nothing calls them yet — K-5 wires the lanes, K-6 the sweep.
  - `finalize` runs the plan's steps 1 to 5 literally: the validated canonical line is frozen exclusively as `state/dispatch_end/<project_id>/<dispatch_id>.<attempt>.json` through a temp file plus `os.link` (the loser reads the winner's intent and writes nothing), an exclusive flock on the intent's `.lock` is held through publication with the intent re-read under it, the ledger is appended only when no `dispatch_finished` for the key sits at or after the intent's byte offset, the projection runs, and `published_at` enters the intent through a temp file plus `rename`. `recover` is steps 2 to 5 for an intent that never published, and it re-applies the projection even when the line is already in the ledger.
  - The key is `(project_id, dispatch_id, attempt)`. The projection runs under `BEGIN IMMEDIATE` and only applies while the row's `attempt_count` is still the intent's attempt, so a late finalize of attempt 1 can neither write attempt 2's line nor project onto its row (OI-2443).
  - The projection is ADR-045 §4's table, literally: `completed` → `completed`, `failed` → `failed_delivery`, `timed_out`/`abandoned` → `timed_out`, each along the hop sequences the table lists. A row in a state the table does not list, or in another terminal state, stays where it is and the call reports a projection conflict; the ledger line still lands, because the ledger is the source. `abandoned` adds `metadata_json.end_state = abandoned` to the row — no new DB state, and the marker is merged, never replaced.
  - Two append-layer guards the contract was missing. The receipt-v2 warning classification is decided once, when the intent is frozen: the append re-classifies against the recurrence counter, which can cross its threshold in between, and the frozen decision now wins, so the line is written against the classification the intent holds and the deferred commit acts on it (OI-2452). The event type is frozen before the pre-write hook and compared after it, so a hook can no longer decide whether the post-hook end-line guard runs by rewriting `dispatch_finished` into `task_complete` or back (OI-2470).
  - A corrupt intent (invalid UTF-8, an unreadable file) and a NaN or Infinity float in an end line are named `AppendReceiptError` refusals (`invalid_end_intent`, `invalid_end_line`) instead of a raw `UnicodeDecodeError`, `OSError` or `ValueError` escaping into a caller that only knows the append vocabulary.
  - Round 3 fixes the race test only (K-3c r3): the two-process case no longer sorts the two outcome strings and requires position 0 to be `finalized`. The statuses are strings and `already_published` sorts before `finalized`, so the test failed on CI whenever the loser lost to a winner that had already published — a legal outcome the next line of the same test already allowed. One helper now counts: exactly one `finalized` beside one `(intent_lost, already_published)` in either order, and anything else — a missing outcome, `None`, an unknown status, two winners, two losers — stays a failure.

- T0 rotation (/goal follow-up): a follow-up line over 780 UTF-8 bytes was typed as one
  `tmux send-keys -l` burst, which Claude Code stores as `<pasted_content>`; no /goal runs (03-11
  13:17Z, 913 bytes). The threshold comes from the receipts: every confirmed follow-up carried at
  most 785 bytes, every unconfirmed one at least 841. `goal_followup_text` now prefers a line that
  fits one burst (`GOAL_TYPED_MAX_BYTES`), and `type_line` types a longer line in pieces of at most
  200 bytes, split in Python so no character is cut, with `VNX_T0_ROTATE_CHUNK_PAUSE` between the
  pieces and one Enter at the end. The `t0_context_rotation_goal_followup` receipt carries
  `goal_bytes` and `typed_in_pieces`, so the next rotations measure the fix themselves.

- CI Profile A (track `ci-profile-a-xdist`, D3c, risk R9): `profile-a` gets
  `timeout-minutes: 80`, up from 55. R9 came true on 2026-11-03: the last three
  `main` runs finished the job in 52, 52 and 54.8 min (the last 12 s under the
  limit) while all 13 stacked dispatch branches since 17:28Z were cancelled at
  exactly 55 min, so no PR of the stack could get a green VNX CI and the D4
  cutover rule could not hold (`every_run_clean` fails on
  `reference.job=cancelled`). D4 (#2361) runs the same suite in the xdist shape
  in 17-24 min and sets the bound again from that measured parallel time. The
  workflow comment now carries the measurements, and
  `test_profile_a_keeps_its_required_name_and_gets_80_minutes` pins 80 and that
  every step timeout still fires before the job's.

- Ledger lifecycle (ADR-045 K-4a, track `audit-spoor-kern`): `scripts/lib/dispatch_start.py` adds `begin` and `record_child`, the core of the door's hand-over to a lane. Nothing calls it yet — K-4b wires the door, K-4c the spawn file — and `begin` takes `base_sha` as an already resolved, immutable value, so the module resolves no ref and knows no repo.
  - `begin` returns a frozen `Handover` (`project_id`, `dispatch_id`, `attempt`, `lifecycle_mode`, `door_pid`, `door_started_at`, `started_at`, `mode_write`, `is_v3`) and refuses through `StartRefused(code, detail)` with named codes (`refire_attempt_live`, `start_line_duplicate`, `start_attempt_unreadable`, `door_identity_unresolved`, `handover_lock_timeout`, `start_line_unconfirmed`, `ledger_unreadable`, `spec_unreadable`).
  - With `ledger_lifecycle` on it is one locked operation: an exclusive `fcntl.flock` per dispatch on `state/dispatch_end/<project_id>/<dispatch_id>.handover.lock` (parent created, bounded wait of 5 s, else `handover_lock_timeout`) wraps the attempt choice, the liveness check and the append. A previous attempt that has a `dispatch_started` without a `dispatch_finished` and is still live — same door pid and start time, same child pid and start time from the spawn file, or a held occupancy flock — refuses the refire, and the append layer's `duplicate` verdict is `start_line_duplicate`.
  - Only the exact `(project_id, dispatch_id)` key counts: a `job-extra` start never blocks `job`, and a finish line for `job-extra` never ends `job`.
  - Stored count, no row yet and a failed count read stay distinct. A failed read never becomes "attempt 1": legacy projects only what it can (`mode_write: write_failed`), v3 refuses `start_attempt_unreadable`; no row yet is attempt 1. A failed `lifecycle_mode` write logs a WARNING with dispatch id, attempt and exception, reports `mode_write: write_failed`, and the hand-over continues because the ledger is the source.
  - Legacy (the default) takes no lock, writes no line and needs no door identity; with `ledger_lifecycle` on, an unreadable door start time refuses `door_identity_unresolved` rather than writing a line whose liveness cannot be checked. `record_child` writes `state/dispatch_end/<project_id>/<dispatch_id>.<attempt>.spawn.json` atomically for v3 only.

- Ledger readers (ADR-045 K-7a, track `audit-spoor-kern`, OI-2524): a door refusal no longer ends a started attempt and no longer outranks the real completion. A refusal is booked as a `task_complete` with `status: blocked`, `delivery_state: delivery_refused` and no `attempt`; `completion_reading.is_refusal` is the one predicate for that exact value. `ledger_end_line.end_line` never returns a refusal as the end of an attempt that has a `dispatch_started` line for the same `(project_id, dispatch_id, attempt)`, so an attempt still running no longer reads as ended `blocked`; a refusal in legacy history without a start line stays that attempt's end exactly as before. `completion_reading.dedupe_completions` ranks a refusal below every non-refusal completion line of its attempt, in either order, so the later real success is the witness; two refusals still collapse to the first and `count_completions` returns the same numbers as before. No ledger line is rewritten.

- Review seats (track `tweede-stoel-op-signaal`, PR 4): the merge door now reads `VNX_REVIEW_SEAT_RULE` through one helper (`review_seat_evaluation.active_seat_rule`, over the same `config_runtime` path as the review stack). Unset — the default for every project — and `first_seat_or_every_seat_by_risk` keep the risk rule unchanged; any other value fails closed to it and is recorded by name on the seat record; `first_seat_or_every_seat_on_signal` activates the signal rule, so a clean codex PASS needs codex only and every signal, `UNKNOWN` field or fail-closed case asks every seat. The `pr_merged` receipt's seat record carries the rule name, the signals and every `UNKNOWN` field, and the shadow record now reuses that one evaluation instead of running a second. Track closure and the enforcement check judge a receipt under the rule its own record names, never under the current key. The runner transition (a `pending` second-seat obligation booked `retired`/`no_signal`) is a follow-up.

- Ledger lifecycle (ADR-045 K-4b, track `audit-spoor-kern`): the single-entry door now calls `dispatch_start.begin` — its ONE hand-over call, for both lanes, after the plan is validated and before the serialization slot and the spawn.
  - OI-2510 point 5: the base ref is resolved to an immutable SHA ONCE, in the same consumer repo the lanes build in (`resolve_consumer_project_root`), never in the engine checkout `dispatch_cli.__file__` lives in. That one SHA goes to `begin` and down to `create_dispatch_worktree`, which uses a full commit id verbatim instead of re-resolving a ref (a moved `origin/main` can no longer put the worktree on a different commit than the start line names). A ref that resolves nowhere refuses `base_ref_unresolved` before the hand-over.
  - Base freshness (codex on `30b21e0f`): the door now hands `create_dispatch_worktree` a full SHA, so that function's own `git fetch origin <branch>` no longer runs. The fetch comes back into the door, BEFORE the base is resolved, through the SAME helper the worktree path uses (`dispatch_worktree_isolation.fetch_remote_base`) — same command, same best-effort semantics: a stale local `origin/main` no longer starts a worker (legacy path included) on an old commit, and a branch that exists upstream but was not fetched yet resolves instead of being refused. A full SHA or a local ref is not fetched, as on main.
  - OI-2510 point 6: a `StartRefused` prints `REJECT [start-line-refused]: <code>: <detail>`, closes the attempt THIS door owns through the existing pre-spawn close path (`_owner_finish`, the same one the lock-wait timeout uses) with the refusal code as its reason, touches no other live attempt of the same dispatch, and never reaches the slot or the spawn.
  - A `mode_write: write_failed` projection is named on the door's output and the fire continues — the slot and the spawn depend only on the returned `Handover`, never on the projection.
  - The `Handover` travels to both lane entry points as `handover=`, default `None`; K-4b only carries it (K-4c reads it at the spawn, K-5 at the exit). With `ledger_lifecycle` off the door writes no lifecycle line and the route decision, receipts and exit code are unchanged.

- Dispatch start line (ADR-045 K-4c, stacked on #2369): the claude headless lane now records the spawn file. One optional `on_spawn` hook is threaded from `run_envelope_headless_plan` through `ClaudeSubprocessAdapter.run`, `spawn_claude` and `SubprocessAdapter.deliver` to the `Popen` seam, and the lane passes it only for a v3 hand-over, where it calls `dispatch_start.record_child` with the child's pid. The file lands atomically at `state/dispatch_end/<project_id>/<dispatch_id>.<attempt>.spawn.json` with the child's pid and kernel start time. A raising hook is logged with the dispatch id and attempt and never fails the spawned worker; a legacy hand-over and the provider lane pass no hook and write no file.
- A refused start books its own receipt (ADR-045 K-4c round 2): every `StartRefused` in the door's hand-over now writes one `blocked`/`delivery_refused` dispatch receipt through the same writer the provider-switch refusal uses, owned attempt or not, with `start-line-refused` and the refusal code in the failure reason. The live attempt of a refire is left untouched, the refusal never cools the lane or blocks the refire, and with the ledger lifecycle off nothing changes.
- A refusal is never a lane completion (ADR-045 K-4c round 3): `envelope_govern_support._is_lane_completion_receipt` returns False for a `delivery_refused` receipt, so a refused start (or a switched-off provider family) followed by a successful refire no longer makes GOVERN skip its own receipt — the ledger holds the refusal and one real completion. The `source: subprocess` safety net, the ReceiptV2 completion shape, and every record that already did not count keep their verdict.

- Dispatch lifecycle (K-5, track `audit-spoor-kern`, ADR-045 §1/§2/§5): every lane's exit now publishes the attempt's one `dispatch_finished`, and it does so BEFORE the lane releases its occupancy flock. `scripts/lib/dispatch_exit.py` is the single call-site shape: it maps a lane's own outcome onto the closed end-state set (`success` → `completed`, a deadline → `timed_out`, everything else → `failed`), reads the worktree HEAD as the `head_sha` evidence, writes `no_push:<reason>` when there is no head yet, and carries `spawned_at`/`session_id` when they exist. The claude-headless lane finalizes in its `finally`, just before `remove_dispatch_worktree` (whose first action releases occupancy) — covering its success path, a non-zero child, a deadline, the pre-spawn returns (invalid TOCTOU, worktree creation) and the OI-1743 close-out raise; the provider lane does the same for its four pre-spawn returns and its terminal exit; the door finalizes its own three post-hand-over exits (`slot_timeout`, `pre_spawn_error`, `door_error`) before it closes the door-owned row. Only a v3 hand-over is finalized: with `ledger_lifecycle` off — and for every caller without a hand-over — no line is written and the behaviour, receipts and decision objects are exactly the K-4c ones. A finalizer that raises is logged with the dispatch id and swallowed, so a successful dispatch is never booked `failed` and the flock is still released. One finalizer, one key: a second call on the same `(project_id, dispatch_id, attempt)` adds no second line (`already_published`/`intent_lost`).

- ADR-045 K-5b (OI-2519): a report that governance rejects no longer freezes a `completed` end line. `envelope_govern._govern` publishes its governed verdict as `_AdapterResult.governed_status` (never rewriting `status`), and `dispatch_envelope._LaneExit.govern` maps `contract_invalid` onto the end line via a new `GOVERNED_END_STATES` row, so the audit trail says `failed` where the report body contract was rejected. The caller-visible `EnvelopeResult` is unchanged, and any non-success lane outcome still wins.
- ADR-045 K-5b r2 (OI-2519): a delivery the phantom guard rejects no longer freezes a `completed` end line either. `_govern` keeps the `PhantomVerdict` `record_phantom_if_any` returns and publishes `phantom_rejected` as the governed status (it wins over `contract_invalid`; a guard that raised publishes nothing), `GOVERNED_END_STATES` gets the matching `failed` row, and `_LaneExit.govern` now takes any governed downgrade of a worker success instead of one named value. The guard sees the worker's own status, so an empty diff under an invalid report is still a phantom. The corrective receipt, `phantom_guard.py`, the finalizer and the readers are unchanged.

- Report path (ADR-045 K-8a, track `audit-spoor-kern`, OI-2516): `scripts/lib/report_lifecycle_mode.py` adds the one decision that says whether a worker report is booked as the legacy task-shaped receipt or as a single `report_ingested`. Nothing calls it yet — K-8b wires the converter and `report_parser`, K-8c the restore — so no receipt changes in this delivery; every writer, reader, the append layer and the schema stay untouched.
  - `decide` answers v3 as soon as one readable source says so (a `dispatch_started` of this dispatch with `lifecycle_mode: v3`, or `dispatches.metadata_json.lifecycle_mode == v3`). Either source unreadable — the ledger, or the row, including a store whose tenant cannot be addressed — now refuses `lifecycle_mode_undecidable` and names both; the previous build stayed legacy unless BOTH were unreadable, which could book a second end behind a v3 row or a v3 start. An absent DB file or row still counts as read.
  - The tenant is fixed before the row is read: the given `project_id`, else the one tenant the start lines name; two tenants refuse `project_ambiguous` instead of letting the last start line win.
  - The attempt is the receipt's own (it must have its own start line, else `attempt_unknown`), else the only started attempt, else the start latest at or before `report_written_at` — the write time of the file the caller actually read, with the canonical `report_path` mtime only as a fallback; no usable time, or a time before every start, refuses `attempt_ambiguous`. A row-only v3 dispatch uses `attempt_count`.
  - The end link comes only from a `dispatch_finished` with the exact `(project_id, dispatch_id, attempt)`. `ledger_end_line.end_line` accepts the legacy completion family by design, so it is not used here: a `task_complete` (even `delivery_refused`) or a line without an attempt never links, and a running attempt stays `end_pending: true`.
  - The store is explicit: `state_dir=None` is a `TypeError`, never "legacy", and `append_destination_state_dir()` reuses the append layer's own `_resolve_state_dir_env_first` so K-8b can pass the real destination.
  - Round 2 (OI-2520): the tenant rule lives here once. A caller with no `project_id` and no start-line tenant now resolves it with `project_root.resolve_project_id()` — the resolver behind `dispatch_cli._resolve_project_id`, reused not copied — so a legacy report is no longer refused for lacking a tenant while `runtime_coordination.db` exists. Order: the given/receipt `project_id`, then the single tenant on the start lines (two still `project_ambiguous`), then the resolver; a resolver that raises leaves the tenant unresolved and the existing refusal applies.
  - Round 2 (OI-2515): a tenant that finds no v3 evidence while ANOTHER tenant holds v3 evidence of the same `dispatch_id` (a v3 `dispatch_started`, or a v3 `dispatches` row) refuses `project_mismatch` and names both, instead of silently answering `legacy`. The look is one read-only query on the same connection rules; an unparseable other-tenant row refuses rather than clearing the mismatch. A legacy row under another tenant still answers `legacy`.
  - Round 3 (OI-2528): `scripts/lib/ndjson_io.py` gains `read_ndjson_strict` / `NdjsonCorruptionError`, which keep the torn-tail tolerance (blank lines skipped, a missing file empty, a complete final record without a newline kept) but raise for an unparseable line anywhere else, naming the 1-based line and the path. `iter_ndjson` / `read_ndjson` keep their lenient mid-file skip byte for byte, and the ledger is decoded per line, so an undecodable byte is unparseable evidence rather than U+FFFD. `_read_ledger` drops its own `continue`-on-every-`JSONDecodeError` loop and reads through the strict reader: a ledger with an interior break refuses `lifecycle_mode_undecidable` with `one lifecycle source is unreadable (ledger: ledger_corrupt: …, the line and path; row: …)` — the ledger's reason in full, naming the line — so a corrupted v3 start line can no longer vanish into a booked `task_complete`, while a crash-torn final line is still read past.
  - Round 5 (OI-2528, OI-2529): torn means unterminated. `read_ndjson_strict` skips an unparseable final fragment only when the file does not end in a newline after it; a newline-terminated final break (JSON or invalid UTF-8) was written completely and raises with its line number. It takes an optional predicate on the raw line: an interior break raises only when the predicate holds for it (otherwise skipped with a WARNING), so the refusal is scoped to lines that can be the caller's evidence. `dispatch_start._lifecycle_lines` now reads the ledger through this reader with `dispatch_id in raw`, turning a broken own line into `StartRefused("ledger_corrupt", <line>)` instead of silently continuing past it; the intact ledger still answers `refire_attempt_live`, another dispatch's break is skipped, and a torn final fragment is tolerated. `report_lifecycle_mode._read_ledger` passes the same predicate for the dispatch it decides, so another dispatch's broken line no longer refuses this one while this dispatch's own still refuses `lifecycle_mode_undecidable` in round 4's `one lifecycle source is unreadable (ledger: …; row: …)` wording. A `runtime_coordination.db` path that exists but is not a regular file is now `ROW_UNREADABLE` (never `ROW_ABSENT`/legacy).

- Audit trail (ADR-045, audit-spoor-kern K-8b): the report converter and `report_parser.py` now book a v3 dispatch's worker report as one `report_ingested` (linked to the `dispatch_finished` when it exists, else `end_pending: true`) instead of a second `task_complete`; a legacy dispatch keeps its byte-identical line, a refusal writes nothing, names the reason and leaves the report for the next scan, and `restore_stranded_reports` reports the refusal instead of crashing.
- Audit trail (K-8b round 2): both writers now hand the decision the attempt the report's own metadata names (`attempt: 1` survives a later attempt 2 start instead of being booked as attempt 2), and neither writer resolves a tenant itself any more — K-8a resolves it once and refuses an unresolvable one.
- Audit trail (K-8b round 3): the attempt reader lives once, in `report_to_receipt_converter.py`, and `report_parser.py` imports it — the parser used to keep the frontmatter's quotes, fail `int("'1'")` and book the later attempt, while the converter booked the earlier one for the same report. The one reader strips one matching pair of quotes, so `1`, `'1'`, `"1"` and ` 1 ` all book the attempt the report names.
- Audit trail (K-8b round 4): the lifecycle hint now has one builder, `report_hint_fields` in `report_to_receipt_converter.py`, read from the report text through the converter's own quote-stripping metadata readers; `report_parser.py` calls it and drops its own hint code — the parser used to keep `project_id: 'proj-alpha'` quoted, refuse `project_mismatch` for a valid v3 report and retry it forever while the converter booked it.
- Audit trail (K-8b round 5): `report_parser.py` no longer names the receipt appender in its text — the docstring names "the store the receipt appender writes to" instead, so the receipt-writer CI guard stops counting the parser as a writer while it still writes no receipt.

- Restore scan (track `audit-spoor-kern`, K-8c, ADR-045): `find_stranded_reports` routes every report receipt through `report_lifecycle_mode.report_line` before judging it, so this third writer books a `v3` dispatch as `report_ingested`. A `ReportPathRefused` is now caught before the bare `except`, logged once per report with its dispatch id, file, code and detail, and counted in `ScanResult.refused`; `--list` and `--apply` name the refused count and print one `REFUSED` line per report. The report is not booked and stays on disk untouched.
- Restore scan (K-8c round 2): `find_stranded_reports` now hands the lifecycle decision the report's own attempt, read through the converter's single reader (`rc._named_attempt`) on the merged frontmatter and body, so a report naming `attempt: '1'` is booked against attempt 1 even after attempt 2 has started. `apply_restoration` books the same routed line the converter does — one `report_ingested` with `attempt` and `end_pending`/`end_outcome_id` for a v3 dispatch, never a second `task_complete` — and a refusal returns `(False, "<code>: ...")` leaving the file byte-for-byte untouched. Legacy reports that name no tenant stay candidates with the DB present because K-8a resolves the tenant itself.
- Restore scan (K-8c round 3): `find_stranded_reports` now takes the report's own tenant and attempt from the converter's one hint builder (`rc.report_hint_fields`) instead of a local copy that carried only the attempt — with `dispatch_started` lines under two tenants, a report naming `project_id: proj-alpha` (bare, single- or double-quoted) is routed to `proj-alpha` and `--apply` books one `report_ingested` there, where the scan previously refused `project_ambiguous` while the converter booked the same report.
- Restore scan (K-8c round 4): `apply_restoration` now captures the report's `os.stat` before anything is booked and, after appending its audit annotation, restores the access and modification times with `os.utime(..., ns=...)`. The write time is the attempt signal for a report that names no `attempt` (ADR-045 K-8a), so the annotation no longer moves an attempt-1 report past the start of attempt 2 and the next converter scan no longer books a second `report_ingested` under attempt 2. A failing `utime` logs exactly one warning naming the file and the error, names the failure in the returned message, and keeps the booking, which already stands.

- CI Profile A (track `ci-profile-a-xdist`, D4): the per-PR suite now runs the two-phase pytest-xdist shape instead of one serial sweep. `scripts/ci/profile_a_pytest_args.sh` emits `serial.args` (the entries of `scripts/ci/serial_tests.txt`, run with `-p no:xdist`) and `parallel.args` (the rest, under `-n 4 --dist loadfile`), and a new required step runs `check_xdist_parity.py completeness` over the two collection manifests, failing the job on a test that did not run, ran twice or was skipped without a recorded key. The accepted skip set is `scripts/ci/xdist_expected_skips.txt`, written from the last measurable run of the cutover window (run 41119890499, head 75011469, 50 skips, no collection-time skip), each key with the reason it carried. Profile A's `timeout-minutes` is 80 for the serial way back and 38 for the two-phase shape: 38 is twice the longest shadow suite wall time of the merge window it landed on (1134 s, run 41827919472; 2 x 1134 s = 37.8 min, rounded up), and the serial branch keeps D3c's 80, its measured 52 to 54.8 minute sweep (OI-2523).
- `vars.VNX_CI_XDIST=0` puts Profile A back to the fully serial `full.args` sweep with no other change; unset or `1` is the two-phase shape. The step is one expression in `vnx-ci.yml`, so the way back is a repository variable, not a revert.
- The per-PR `profile-a-xdist-shadow` and `xdist-parity` jobs are removed from `.github/workflows/vnx-ci.yml`. `check_xdist_parity.py`, `record_xdist_parity_verdict.py` and `xdist_parity_history.py` stay: the new nightly `.github/workflows/vnx-ci-serial-truth.yml` runs the first two, and `xdist_parity_history.py` applies the cutover rule to the pre-D4 per-PR verdicts. It runs a full serial sweep on main plus the xdist-shaped run of the same commit, compares the two, records the verdict and fails on any difference, which is what makes the cutover rule measurable. Its name is deliberately not "VNX CI": the merge readers select their runs by that exact name.
- Tests: `tests/test_ci_xdist_d4_switch.py` is new (the skip set passes a recorded key and fails an unrecorded one, the selection fingerprint of the default emission is the pre-D4 one, `VNX_CI_XDIST=0` emits the serial vector and not the xdist phase, the per-PR workflow has no shadow/parity job and a completeness step that can fail the job, the nightly keeps the serial truth and the comparison and is not named "VNX CI"). `tests/test_ci_profile_a_pytest_args.py` passes `VNX_CI_XDIST=0` where it means serial; the workflow tests of the removed jobs are gone and `tests/test_ci_record_xdist_parity_verdict.py` now runs the nightly's parity steps.
- OI-2518 (D4 round 2): `tests/test_report_to_receipt_converter.py::TestFailClosedBranchNotOnOrigin::test_existing_branch_passes_check` no longer fails the xdist phase when its precondition times out. The listing that finds a positive-control branch is narrowed to `git ls-remote --heads origin "dispatch/*"`, and a `TimeoutExpired` or a non-zero exit of that listing now skips with a reason that names the timeout — the branch listing is a precondition, not the thing under test, and `_check_branch_on_origin` keeps its real call, its real 10 s timeout and its real assertion. The skip key is recorded in `scripts/ci/xdist_expected_skips.txt` with its OI-2518 reason so the completeness step accepts it. Measured on `82c74083`: the three network tests serially 5/5 green (2.0 s per run; every `git ls-remote` call 0.37–0.53 s), the whole file under `-n 4` 3/3 green (208 passed, 5.3–6.5 s), so no `serial_tests.txt` entry: the call that timed out in run 41622189660 was the precondition listing, not the call under test.
- OI-2521 + OI-2523 (D4 round 3): the nightly's `report` step takes its clean branch from `record_xdist_parity_verdict.py --report-clean` (new `report_decision`): only a verdict with `exit` 0 and an empty `step_failures` counts, so a compare step that failed after writing exit 0 now opens the issue and exits 1 instead of printing "parity holds"; every job of `vnx-ci-serial-truth.yml` carries `if: github.ref == 'refs/heads/main'` (the parity job combined with its `!cancelled()`), because a manual dispatch on a PR head would attach check runs to that SHA and the merge readers read every check run; and `serial-truth` gets `timeout-minutes: 80` (main measured 52, 52 and 54.8 minutes on 03-11; the 55 cutoff cancelled every stacked run).
- OI-2523 + OI-2525 (D4 round 4): `profile-a`'s `timeout-minutes` now follows the same `vars.VNX_CI_XDIST` the suite step reads for its switch — `${{ vars.VNX_CI_XDIST == '0' && 80 || 35 }}`. The serial rollback gets the 80 minutes its measured 52 to 54.8 minute wall time needs (OI-2523) instead of being cancelled at 35 on every run; the two-phase shape keeps 35. `tests/test_ci_xdist_d4_switch.py` evaluates that expression with a small parser at both variable values instead of pinning the string, and holds the timeout to the switch's variable and its comment to the serial measurement.
- D4 round 5 (merge of main, #2378): the branch merges `origin/main` and `profile-a`'s xdist bound moves 35 -> 38, sized from the merge window it lands on (`xdist_parity_history.py --last-d3-merge 983c944f`: 23 runs, all exit 0, longest shadow suite wall time 1134 s on run 41827919472, 2 x 1134 s = 37.8 min, rounded up). The serial branch keeps 80, now described as D3c's bridge (#2378, 2026-11-03). `vnx-ci.yml` resolves to D4's two-phase shape, the shadow-workflow test of the removed job stays deleted, and D3c's step-sum rule moves into `tests/test_ci_xdist_d4_switch.py` (every step timeout is an integer, at least one exists, and their sum is below both branch values read out of the expression).

- CI Profile A (track `ci-profile-a-xdist`, D4 follow-up; OI-2543, OI-2544): the record step's bash
  fallback is held equal to `scripts/ci/record_xdist_parity_verdict.py` again. A new test extracts the
  step's bash from `.github/workflows/vnx-ci-serial-truth.yml`, runs it without a checkout, and compares
  its verdict (every key and value) and its summary text with what the script writes for the same
  inputs; the two had drifted on the summary line. The script now says "Nightly serial truth." (its only
  caller is the nightly) and its docstring names the nightly's parity job and the surviving test file.
- The selection fingerprint test no longer pins one literal sha256 of the parallel vector: it holds the
  value deterministic for the same inputs and moving when `test_exclusions.txt`, the `-k` quarantine or
  the constraints change, so a PR that repairs an excluded test or bumps a pin no longer turns Profile A
  red until someone edits a hash. The D4 cutover value stays on record in
  `claudedocs/2026-10-24-nachtrun-golf-1-3/evidence/d4-overstapregel-20261105-0251.json`.
- The nightly's main-guard comment now says what the mechanism does: a `workflow_dispatch` on another ref
  skips the jobs but GitHub still creates check runs with conclusion `skipped` on that SHA, and
  `closure_verifier._rollup_all_green` reads that as not green, so it fails closed. A new test holds every
  job in the workflow to the guard, not only the three named ones.
- `scripts/ci/xdist_parity_history.py` gains `--candidate-branch BRANCH`: a run whose head branch matches
  is placed `candidate`, outside the window, listed with its reason, and never a measurement or a
  blocker. Without the flag nothing changes; only an exact branch match is placed this way, so the
  candidate's own run (whose workflow has no shadow job by design) no longer blocks the rule as
  `no_verdict`. Its docstring no longer says the nightly runs it (the nightly runs the comparison and the
  record step).
- Small truths: `vnx-ci.yml` no longer writes `heavy=true`/`heavy=false` to `$GITHUB_OUTPUT` (no step
  reads `outputs.heavy`), the D4 changelog bullet states the final bounds (80 serial, 38 xdist, from
  1134 s), and `docs/DOCS_INDEX.md` lists `docs/core/CI_PROFILE_A.md`.
- The fingerprint test's constraint-pin case now derives its mutation from the constraints text — the first
  `==` pin, its last version component bumped — instead of replacing a literal `pytest==9.1.1`. A bump
  of that pin therefore moves the mutated text with it instead of turning the mutation into a silent no-op
  (OI-2543 point 3). A parametrized case runs the same mutation on `pytest==8.0.0` and `pytest==9.2.0`
  texts and holds that only the pinned line changes and still parses as the same distribution.

- Dispatch sweep (ADR-045, K-6): `dispatch_lifecycle.sweep` recovers every end intent that never published and books `abandoned` for a `v3` attempt that is past its deadline plus the 30-minute grace and has no liveness (occupancy flock, living `door_pid`/`child_pid` with the recorded kernel start time). A reused pid counts as dead and an unmeasurable probe as unknown; neither the sweep nor its heartbeat ever changes the gate-obligation runner's exit code, and with `ledger_lifecycle` off the sweep writes nothing and reports the in-flight attempts instead. `scripts/dispatch_lifecycle_count.py` reports the rollback precondition: `v3` attempts in flight and open intents.
- The K-6 sweep closes two audit holes (codex round 2): a `v3` attempt ends only on a `dispatch_finished` for its exact `(project_id, dispatch_id, attempt)`, so a legacy completion, a refusal receipt or a line without an `attempt` no longer hides an unfinished attempt from the sweep and the rollback count; and the lifecycle-mode stamp is one `UPDATE` that merges the field under the row's own JSON, so metadata another writer commits after the sweep read the row is never lost.
- The sweep now tells three outcomes apart (codex round 3): no store and no row stay "no row", and a `runtime_coordination.db` that cannot be opened or queried raises `AppendReceiptError` instead of reading as an absent row. That attempt is booked nothing — no end line, no projection, no mode stamp — the sweep names the dispatch, the attempt and the exception in `SweepResult.errors` and the heartbeat's `counts.errors`, the rollback count reports the store among its `unreadable` sources (exit 2), and the next sweep tries again. An absent store is still an absent row and the attempt is abandoned as before.

- Audit trail (plan row 6, OI-2522): the lifecycle sweep now also runs at the door on every real fire and in every `build_t0_state`, each bounded at 2 seconds between items (`budget_seconds`, reported as `budget_exhausted`/`budget_left` in the heartbeat and the state). A sweep that raises or runs out of budget never refuses a fire or changes a build's exit code. A bounded sweep that stops early now records the `(project_id, dispatch_id, attempt)` of the first attempt it did not take as `cursor` in the same heartbeat, and the next bounded sweep resumes there and wraps around — so consecutive bounded sweeps visit every attempt instead of restarting on a live prefix. An unbounded sweep ignores and clears the cursor, and an absent or malformed one starts at the front. A cursor is read back only when `project_id` and `dispatch_id` are non-empty strings, `attempt` is an integer of at least 1 and `offset` an integer of at least 0 (bools rejected); anything else — a cursor left by an older run without a `project_id`, say — is the front for both a scoped and an unscoped sweep, and `_resume_at` never raises on a cursor it was handed. A sweep that fails for any other reason writes the front to its heartbeat instead of carrying a cursor the next run would reject. The `dispatch_started` line now carries the staged spec's own `deadline_seconds` when it is a legal integer, and `deadline_for` reads it first — so a spec staged under `pending/<staging_id>/` with a staging id different from the dispatch id no longer falls back to 14400 seconds.

- Active readers decide a `v3` dispatch on its ledger attempt (track `audit-spoor-kern`, K-7, ADR-045 §7): the new `scripts/lib/ledger_attempt_state.py` holds one `attempt_verdict(state_dir, project_id, dispatch_id, *, records=None)` over the ledger end line and `dispatch_lifecycle.liveness`, and both readers now ask it instead of re-deriving "done" from receipts and age. The end line is the first `dispatch_finished` whose `project_id`, `dispatch_id` and integer `attempt` are exactly this attempt's key — the same key `dispatch_lifecycle.v3_in_flight` ends on (ADR-045 §2), so the reader and the sweep agree; a line missing a key field, naming another project or attempt, or carrying a malformed attempt (a string, a bool) is no end and leaves the attempt to liveness, never a defaulted one. `check_active_drain.py` moves an ended `completed` attempt to `completed/`, leaves any other end state as an open point with that end state, leaves a `live`/`unknown` attempt alone whatever its age, and books a `dead_no_end` attempt as `abandoned_candidate` (the K-6 sweep writes the end; the drain never writes ledger lines). The ledger is read once per drain run; an unreadable one makes every entry an open point, never a silent move.
- `scripts/lib/open_outcomes.py` takes the same verdict: a `v3` end line decides the item's outcome (`completed` closes it, `abandoned`/`timed_out`/`failed` stay listed until a T0 decision in `t0_decision_log.jsonl`), an end line without a readable `end_state` is a `malformed_end_line` open point, and a live/unknown attempt is no open point. The verdict is asked before the legacy outcome filter (K-7 round 2): a `v3` end state other than `completed` stays an open point until a T0 `accept`/`reject`, even when the receipts fold to `superseded` (a child naming it as `parent_dispatch`) or to `accept`. Legacy dispatches (no `dispatch_started` line with `lifecycle_mode: v3`) keep the receipt-based path byte for byte.
- Tests: new `tests/test_k7_active_readers.py` drives real `dispatch_start.begin`, `dispatch_exit.build_end_line` and `dispatch_lifecycle.finalize` against a real ledger and `runtime_coordination.db` in `tmp_path` (only clocks and liveness probes are patched), covering both plan cases, every end state, T0 accept/reject, live/unknown past the threshold, the legacy reasons, an unreadable ledger, two projects sharing a dispatch id (ADR-007), and a `dispatch_finished` that misses the exact key (no `attempt`, no `project_id`, another project, a string or a bool attempt).

- Audit trail (track `audit-spoor-kern`, K-6 follow-up): compaction no longer archives an attempt that is still running. `compact_receipts` keeps every `dispatch_started` line whose exact `(project_id, dispatch_id, attempt)` has no `dispatch_finished` in the live ledger, because `v3_in_flight` reads only the live file and an archived start would read as no attempt at all; the rule is one shared predicate factored out of `v3_in_flight` (`lifecycle_key`/`end_keys`/`is_unfinished_v3_start`), never a copy, and kept starts keep their order ahead of the newest 10,000 lines. `receipts_rotating` reports `kept_unfinished_v3_starts` with the same numbers in a dry run and a real one. `open_intents` now lists the intent directories with `os.scandir` instead of `Path.glob`, so an unreadable project directory is reported and fails the in-flight count instead of silently reading as zero intents; a missing root or project dir is still no intents and no error. OI-2539, the start line carrying its own `deadline_seconds`, landed with K-6b (PR #2381, OI-2522) and is unchanged here.

- OI-2549: the K-4b door tests no longer fetch the real `origin/main`. Every test in `tests/test_k4b_door_hand_over.py` that drives the door against the real repository now neutralises `dispatch_worktree_isolation.fetch_remote_base` and asserts the patch landed on the call the door makes, so a merge landing on main while a CI run is in flight can no longer move the base sha between the test's read and the hand-over (main-CI run 41996195419: expected `cf377391`, recorded `0c28ca7d`). The clone-based freshness tests in §6 still fetch on purpose against a disposable bare origin; two new §7 tests reproduce the race there and pin the fix.

- Sweep (OI-2527, OI-2530): a bounded run walks one sequence — the open intents in their current order, then the in-flight attempts — starting at the heartbeat cursor's item and wrapping, so a `recover` that hangs on the publication lock costs one item of one run instead of the whole budget of every bounded run at the door and in the build. The cursor now carries its item's `kind` (`intent`/`attempt`) and combined offset; a K-6b cursor without a `kind` is read as an attempt, and an unreadable cursor still means the front. An unbounded run keeps the old order and clears the cursor.

- Receipt conversion (track `audit-spoor-kern`, K-act pre-work, OI-2542 / OI-2541): a ledger-lifecycle refusal that can never resolve is dead-lettered once the REFUSAL ITSELF has lasted 24 hours, and every lifecycle refusal is counted by the rejection beacon. `report_to_receipt_converter.PERMANENT_LIFECYCLE_REFUSALS` names the `ReportPathRefused` codes no later read of the append-only ledger can resolve — that is exactly why they are dead-lettered rather than retried (`attempt_unknown`, `attempt_ambiguous`, `project_ambiguous`, `missing_dispatch_id`); once the converter's own rejection history has carried that exact `(dispatch_id, file, reason)` for `LIFECYCLE_REFUSAL_DEADLETTER_SECONDS` (measured on `first_rejected_at`, never on the report's mtime), the refusal takes the model-refusal path — the `REJECTED (fail-closed)` line, the history entry and the `receipt_deadletter/` move with reason `lifecycle_<code>`. A processor returning from a long outage therefore refuses for a full day before anything moves, however old the report is. `TRANSIENT_LIFECYCLE_REFUSALS` names what is always retried (`ledger_unreadable`, `lifecycle_mode_undecidable`, `end_outcome_id_missing`, and `project_mismatch` / `project_unknown`, which hinge on `resolve_project_id()` and the environment rather than the ledger alone), and a test pins both sets against every code `report_lifecycle_mode` raises. The converter beacon's `details.rejected` now keeps ONE entry per refusal key — `rejected_at` the newest sighting, `first_rejected_at` the earliest — deduplicated before the 200-entry cap, so 300 scans of one hanging refusal no longer evict every model rejection from the history. `receipt_conversion_rejection_beacon.parse_rejections` also matches `REFUSED (ledger lifecycle) ...`, and a `REJECTED (fail-closed)` line whose reason starts with `ledger lifecycle ` gets `kind: lifecycle_refusal`, so both beacons give one verdict and one name for one refusal.

- Audit spoor (track `audit-spoor-kern`, K-7b, ADR-045 §7): `crash_recovery_sweep` and `orphan_sweep` now decide a `v3` dispatch on the shared `ledger_attempt_state` reader instead of on liveness alone. `crash_recovery_sweep` reads the ledger once per run and asks `attempt_verdict` per orphan: a `v3` orphan is counted under `v3_ended`/`v3_live`/`v3_unknown`/`v3_dead_no_end` and left alone — no fallback report, no receipt, no manifest move, no worker-exit cleanup — while a `legacy` orphan keeps the recovery path unchanged; an unreadable ledger recovers nothing and names the reason. In `orphan_sweep` kind 1b counts only an `ended` attempt as completed, kind 2 reads `live`/`unknown` as in flight, kind 3 delegates to `crash_recovery_sweep` with the ledger read it already made, and a `legacy` dispatch keeps today's register-and-receipts logic.

- Audit trail (K-9, ADR-045 lifecycle): `ledger_health` now reports a `lifecycle_count` next to its four checks — the five lifecycle defects over this project's `v3` ledger lines (`start_without_end` past `deadline_for` plus the sweep's `ABANDON_GRACE_SECONDS`, `end_without_start`, `double_end`, `projection_conflict` against the finalizer's `END_STATE_TARGET`/`PROJECTION_ROUTES`, and `provenance_unknown` for a `writer_sha` the repository cannot resolve), each a list of `(project_id, dispatch_id, attempt)` keys with its count. It is deliberately OUTSIDE the exit-code fold: `overall_status` and `exit_code` are computed from the four checks exactly as before, and a test proves them equal with and without the count on a store where the count finds every defect. `--activation-commit <sha>` (also `compute_health(activation_commit=...)`) narrows the count to lines whose `writer_sha` descends from that commit via `git merge-base --is-ancestor`; without it the cohort says `all_v3`. Every git probe runs through one helper with a timeout, and a git that cannot run makes the result `unmeasured` with a reason — never a zero. The count writes its own atomic beacon, `health/lifecycle_count.json`, with the counts and their timestamp, and the sweep heartbeat's age is reported as `sweep_heartbeat_age_seconds` (or `null` with the reason it is absent). The SessionStart briefing gains one line off that beacon — `lifecycle: <n> start-without-end, <n> end-without-start, <n> double, <n> conflict, <n> provenance-unknown; sweep <age>` — carried through `build_t0_state`'s `t0_index.json` and printed by `hooks/sessionstart.sh`; when the beacon was never written the line says `unmeasured`, never a row of zeros.
  - Round 2 (OI-2528, codex on the round-1 head): what the count cannot measure is `unmeasured`, never a clean zero. The ledger is read through K-8a's `read_ndjson_strict`, so an unparseable line in the middle makes the whole count `unmeasured` naming the line while a torn final line stays no record and no defect. Git answers yes, no or `unmeasured`, never a guessed no: `_git` returns the exit code, an `activation_commit` that `rev-parse --verify` cannot resolve to a commit makes the count `unmeasured` (reason names the commit) instead of emptying the cohort, and a `merge-base --is-ancestor` exit other than 0/1 is `unmeasured`. A recorded measurement problem (an unparseable timestamp, an unkeyable start or end) is `unmeasured` with the problems as the reason, so the beacon fails and the briefing line says `unmeasured` instead of zeros. `provenance_unknown` now covers every `schema_version == 3` line of this project whatever its `event_type` — a `report_ingested` stamped with an unknown `writer_sha` is a finding — keyed on `(project_id, dispatch_id, attempt)` with `attempt: null` when the line names none; pairing stays start/end only.
  - Round 3 (codex on `c0c92a8e`): the end of an attempt is picked only from the cohort. `_records` now groups by dispatch id only the lines the `activation_commit` keeps, so the end `projection_conflict`, `double_end` and `start_without_end` read through `ledger_end_line.end_line` can no longer be a line the activation excluded; without an activation commit every `v3` line stays in and the count is unchanged.

- Audit trail (K-9e, OI-2556): `lifecycle_count`'s `projection_conflict` no longer counts a row that legally moved on after its projection. The check compared the row's state against `PROJECTION_ROUTES[target]` only — the *from*-states a row may be in before the projection — so an attempt that ended `timed_out`, was projected to `timed_out`, and was later expired by `runtime_reconciler._expire_over_attempted` (which does not change `attempt_count`) read as a conflict: a false defect in the K-meet acceptance measurement. A new helper, `_projection_successors`, walks `DISPATCH_TRANSITIONS` (imported from `coordination_db`, never copied) from the target and stops at `claimed`, because `increment_attempt_count` runs on each claim path and a claim starts a new attempt. From `timed_out`/`failed_delivery` that yields `{recovered, expired, dead_letter, queued}`; from `completed`, nothing. A row still in a `PROJECTION_ROUTES[target]` from-state (not projected yet) is unchanged, and every other state of the same attempt stays a conflict (`completed` vs `timed_out`, `timed_out` vs `completed`). New tests in `tests/test_k9e_projection_successors.py` pin both directions and the walk's boundary at `claimed`.

- Receipt conversion (track `audit-spoor-kern`, K-act pre-work, OI-2552 / OI-2541): the 24 h quarantine deadline now survives the rejection-history cap and needs an unbroken day. `report_to_receipt_converter._write_scan_heartbeat` never evicts an entry whose `(dispatch_id, file, reason)` this scan just refused — the 200-entry cap bounds only the entries not sighted in the scan, oldest first, so a refusal pending beside 200 others keeps its `first_rejected_at` instead of coming back forever young (the number of sighted entries is bounded by the reports on disk). `_merge_rejected_history` restarts `first_rejected_at` at a sighting more than `_REJECTION_CONTINUITY_GAP_SECONDS` (twice the beacon's 1 h expected interval) after the held one: a converter returning from an outage longer than that gives the refusal its promised full day of alarm, while hourly sightings still reach the deadline exactly as before, and `_lifecycle_refusal_outcome` applies the same continuity rule when it decides to dead-letter. The `REFUSED (ledger lifecycle)` line now carries the same `ledger lifecycle <code>: <detail>` reason text as the converter's rejection-history entry (the rejection beacon parses that line, so both name one refusal with one reason; `kind` is unchanged).

- Ledger corruption (ADR-045 K-9c, track `audit-spoor-kern`, OI-2528): a broken line inside `t0_receipts.ndjson` is a named refusal on every reader that decides whether one attempt already started or already ended — but only where that line is this dispatch's own evidence. The torn tail (a crash mid-append: the final fragment, unterminated) stays no record and no evidence.
  - `scripts/lib/ndjson_io.py` keeps one torn-tail rule: `scan_ndjson_lines` decides it per line, and both `read_ndjson_strict` (K-8a round 5's signature and predicate: an optional `Callable[[str], bool]` on the raw line) and the blob form `read_ndjson_strict_bytes` (for readers that hold a window, not the whole file) are built on it. A broken line is torn only if it is the final fragment of what was read AND not newline-terminated: a final broken line that does end in a newline is committed corruption, including one carrying invalid UTF-8. With a predicate, only a matching broken line raises and the others are skipped with a WARNING; without one, the strict reader keeps K-8a's whole-file refusal.
  - The door (`dispatch_start._lifecycle_lines`) refuses `StartRefused("ledger_corrupt", line N)` when an interior broken line carries this `dispatch_id`; the prefilter and exact-key check stay (K-8a round 5's own code — this round keeps it, not a copy). The finalizer (`dispatch_lifecycle._end_lines_from_offset`) refuses `AppendReceiptError("ledger_corrupt")` on the same scope, appends nothing and leaves the end intent for recovery. The append layer's tail scan (`find_prior_end_line`) keeps dropping a window cut and tolerating a torn final line, and refuses `lifecycle_end_lookup_failed` with a reason starting `ledger_corrupt` for any other broken line naming the receipt's dispatch id.
  - The sweep's scan (`v3_in_flight`) reports every interior broken line as `ledger_corrupt line N` in the error list the count already surfaces, and a run whose scan saw one writes no `abandoned` end: it fails through the existing heartbeat path, so the ledger stays byte-identical. The K-9 lifecycle count keeps its whole-ledger `unmeasured` answer.
  - The report decision (`report_lifecycle_mode._read_ledger`) refuses through the same dispatch scope, keeping `ledger_corrupt` with the line number inside K-8a round 4's `one lifecycle source is unreadable (ledger: …; row: …)` detail the restore scan reads. A store path that exists but is not a regular database file is now `ROW_UNREADABLE` and refuses, never `legacy` (OI-2529); only a path that does not exist stays absent.
  - Round 2: K-9 round 4 (which carries K-8a round 5) is merged in, and K-8a's version of the shared reader, the door and the report decision wins outright — one reader, one predicate (`predicate`, on the raw `str` line), one `NdjsonCorruptionError`, no second copy of the torn-tail rule. The three readers only this delivery adds (the finalizer's `_end_lines_from_offset`, the append layer's `find_prior_end_line`, the sweep's `v3_in_flight`) keep their round-1 behaviour but read through that one reader and its predicate.
  - Round 3 (codex on K-8a `25b79c17`, three warnings): a fragment the append closer sealed is debris, not corruption. `_close_a_torn_tail` now writes `ndjson_io.TORN_TAIL_CLOSED` (declared once, imported by the closer) before its one newline, and every reader — strict, blob, tolerant and the sweep's direct scan — skips a broken line ending in exactly that sentinel with a WARNING, whatever the predicate says; the fragment's own bytes stay on disk. Torn is now decided on the last element of `data.split(b"\n")`, never the last non-blank line, so a broken record followed by a newline and whitespace is corruption. The door's attempt read treats only a missing `runtime_coordination.db` as "no row": an existing path that is not a regular file is `ATTEMPT_UNREADABLE` and refuses as `start_attempt_unreadable`. The K-9 count answers `unmeasured` with a reason when that path exists but is not a regular file or cannot be opened; a missing database stays round 1's answer.
  - Round 4 (codex on #2384, three warnings): `_close_a_torn_tail` now seals only a fragment that does not parse. It reads just the tail window: a complete JSON record missing its final newline stands and gets only the `\n`, while a fragment cut mid-JSON (or one longer than the 64 KiB window) is sealed with `TORN_TAIL_CLOSED` exactly as round 3 did. One rule now backs every database-path read — `coordination_db.store_state` answers absent / present / unreadable — at all seven call sites: the finalizer's step 4 fails `store_unreadable` and leaves the end intent unpublished for `recover` (never `no_store` for a directory), the sweep raises `store_unreadable` and that run writes no `abandoned`, `_record_lifecycle_mode` takes its loud `write_failed` path, `_other_tenant_v3` refuses `lifecycle_mode_undecidable`, and the three sites fixed in round 3 keep their answers. K-5b (#2376) is merged in, so an end line follows the governed verdict: work that succeeded under a `contract_invalid` report ends `failed` with that reason, never `completed`.

- Report quarantine (track `audit-spoor-kern`, K-act, OI-2565/OI-2566): the converter's 24 h dead-letter deadline counts the alarm time actually observed instead of one unbroken run, so a host that sleeps through the night still retires a permanently refused report, a longer outage adds no alarm time, and a history entry written before `alarm_seconds` existed starts at zero instead of being quarantined on the first scan after deploy.

- Liveness and ledger-tail hardening (ADR-045 K-9d, track `audit-spoor-kern`, codex warnings on #2384):
  - The append closer reads back to the record boundary. `_close_a_torn_tail` no longer decides on a 64 KiB window: it walks backwards in 64 KiB chunks until the last newline or byte 0 and judges the whole final fragment. A complete JSON record of any length that is only missing its final newline stands and gets the newline; a fragment cut mid-JSON is sealed with `TORN_TAIL_CLOSED` and skipped. Measured on the `vnx-dev` ledger: 333 lines above 64 KiB, the largest 894,997 bytes — every one of them used to be sealed.
  - The pid probe answers alive, dead or unknown. `dispatch_start.probe_process_started_at` separates "ps ran and the pid does not exist" (measured on macOS: exit 1 with both streams empty) from "ps could not run, timed out or failed otherwise" (unknown). `process_started_at` keeps its `Optional[str]` meaning, `pid_verdict` maps the probe to the door's liveness verdict.
  - The door refuses a refire on unknown: `_liveness` reports `PROBE_UNKNOWN` when no signal holds and a probe could not answer, and `_plan_attempt` refuses `refire_liveness_unknown` instead of choosing the next attempt. A dead previous door still allows the refire.
  - The sweep keeps its unknown: `dispatch_lifecycle._pid_state` now maps the probe through `pid_verdict`, so an unavailable `ps` is `LIVENESS_UNKNOWN` and never reads as dead — the sweep writes no `abandoned` for that attempt.

- Lifecycle sweep (OI-2559 point 2): `v3_in_flight` now streams the ledger line by line through the new `ndjson_io.scan_ndjson_stream` and keeps only the end keys and the first v3 start per key, instead of reading the whole ledger and every parsed record. On a 20 MB ledger the traced peak of one `dispatch_lifecycle.sweep` drops from ~60 MB to ~0.3 MB; the attempts, their order and the error text are unchanged.

- Audit trail (track `audit-spoor-kern`, OI-2532 part 1, K-act rule 5): the spawn evidence now survives a failed probe, and an unreadable spawn file reads unknown instead of absent. `dispatch_start.record_child` used to return before writing when `process_started_at(child_pid)` came back `None`, so once the door was gone and its occupancy flock was free the only proof that a live child existed was lost: the refire picked attempt 2 and the sweep abandoned a running child. It now writes the same spawn file with `"child_started_at": null`; `pid_verdict` already answers unknown for a live pid with no recorded start (K-9d rule 3) and dead for a dead one, so a dead child still ends. On the read side, `_liveness` folded every failure to read the spawn file into "no spawn file"; only `FileNotFoundError` now means no child was recorded, while a directory at the path, unreadable bytes, invalid JSON, a non-object value or a `child_pid` that is not a positive `int` (a `bool` is no int) makes the child signal unknown, so `_plan_attempt` refuses the refire with `refire_liveness_unknown` and the sweep keeps it as `unknown_liveness`. The provider lane still gets no spawn file (ADR-045 §8), and the probe, sweep and `_plan_attempt` are unchanged.

- Function size (track `golf-2-hardenen`, G2-0): `scripts/ci/check_function_size_tree.py` measures every in-scope Python file at a git ref with the same counting rule as the per-PR ratchet (`measure_source`, `_in_scope`, `git show <ref>:<path>`), taken from `git ls-tree -r` so the working tree is never measured. It prints `function size tree: <A> above <max>, <B> above 70, <F> functions in <N> files at <ref> (<sha8>)` and one line per function above `--max`, longest first, or one JSON object with `--json`. A file that does not parse exits 1 as `unparsed <path>`. No CI wiring yet (G2-15), because the tree still has 12 functions above 200.
  `scripts/lib/function_size_gate.py` gains thin public names — `in_scope`, `measure_at`, `paths_at`, `resolve_ref` — over the existing `_in_scope`, `_measure_at` and `_git`; the ratchet's behaviour is unchanged.

- Audit spoor (track `audit-spoor-kern`, K-act, ADR-045 §9/§10): `VNX_LEDGER_LIFECYCLE` is on by default. Every dispatch through the door now books `dispatch_started`, one `dispatch_finished` per `(project_id, dispatch_id, attempt)` and one `report_ingested` without any environment override; `VNX_LEDGER_LIFECYCLE=0` is the emergency brake that restores the legacy `task_complete` shape and writes no lifecycle line at all. The registry entry and the append layer's `lifecycle_enabled()` say so, and `docs/core/SUBSYSTEMS.md` moves `ledger-lifecycle` to LIVE with a measured health cell.

- Dispatch ledger (track `audit-spoor-kern`, K-act regression): the lifecycle start line and end line load the `append_receipt` facade before they append. `dispatch_start._append_start_line`, `dispatch_lifecycle._append_end_line` and `append_receipt_internals.lifecycle.prepare_end_line` used the internals module alone, which leaves the `common.facade` proxy empty in a lib-only process (the door has only `scripts/lib` on `sys.path`), so every real dispatch was refused before the spawn with `REJECT [runtime-error]: facade not registered (looking for '_enrich_completion_receipt')`.

- Wiring gate (OI-1411): a public definition added in a test file is no longer reported as an unwired symbol. The gate decides with one predicate over path components (`tests`/`test`) and basenames (`test_*.py`, `*_test.py`, `conftest.py`), applied to both the AST and the regex-fallback extraction, so pytest classes, fixtures and test functions stop blocking every PR that adds tests. `WiringGateResult` gains `test_file_skipped`, so a diff whose definitions were all in test files reports how many it left out instead of a bare zero. Production files under a `testing_tools/` directory or `attest.py` are unaffected.

- Worker isolation (OI-1447, track `golf-2-restblockers`): every claude-CLI worker lane now refuses `git stash`. `refs/stash` belongs to the repository, not to a worktree, so all dispatch worktrees share one stash and a `drop`, `clear` or a `pop` of the wrong entry is irreversible outside the worker's isolation boundary. `worker_permissions.WORKER_CLI_DENIED_TOOLS` holds `Bash(git stash:*)`; it is merged into the role profile's `--disallowedTools`, the shared harness tail (`bare_harness_cli_args`, so DeepSeek and glm inherit it) and the subprocess adapter's import-failure fallback. The kimi, codex, gemini, litellm and local-gemma CLIs have no equivalent deny and follow separately.

- Provider dispatch: `_emit_governance` is 192 executable lines (was 232) — the receipt-role resolution, the tool-call signal aggregation and the post-receipt cooldown/reachability/clear tail each moved into a private helper, unchanged and still fail-open (golf 2, G2-11).

- Refactor (track `functies-onder-200`, golf 2 G2-10): `orphan_sweep.sweep` drops from 245 to 188 executable lines. The orphan-worktree pass (kind 2) moves into `_sweep_orphan_worktrees` (the walk and the lazy `tmux_worktree` import) and `_sweep_worktree_entry` (match, in-flight check, classify, dry-run bucket, reap), both under the 70-line ratchet. Behaviour is unchanged: the same `OrphanSweepResult` entries in the same order, the same log lines, dry-run still mutates nothing, and a classify or reap exception still lands in `errors` with the same keys while the loop continues.

- Dispatch door (golf 2, G2-8): `run_dispatch` drops from 264 to 195 executable lines. The pre-validate routing, the gate fill plus gate-required refusal, and the opening authority/spec-load/refire prologue move into private helpers that keep every reject code, printed line, exit code and spec mutation identical.

- Refactor (golf 2, G2-14): the schema-complete report frontmatter of `dispatch_govern._govern_impl` moved into `_govern_frontmatter(spec, raw, *, dispatch_id, lane, contract_status, permission_enforcement)`. The dict's keys, values and key order are unchanged; `_govern_impl` drops from 211 to 169 executable lines (limit 200) and the new helper is 59 (limit 70). `tests/test_dispatch_govern_frontmatter.py` pins the full dict and `list(frontmatter)` for a worker receipt, a missing receipt, chain-link keys from the spec, from the environment, and unset.

- Function size (track `functies-onder-200`, G2-6a of `golf-2-hardenen`): `envelope_govern._govern` drops from 330 to 260 executable lines. Its three-way closing status log becomes `_log_govern_outcome`, the inline phantom guard (lazy import, fix-forward diff resolution, verdict publish and both fail-open backstops) becomes `_run_phantom_guard`, and the end-of-dispatch event-stream clear becomes `_clear_archived_event_stream`. Pure move: same log lines at the same levels with the same arguments, the guard gets the same inputs, every guard failure stays non-fatal. The stray-report adoption and the body-contract check follow in G2-6b.

- Gate obligations (track `functies-onder-200`, golf 2 G2-1a): three decision branches of `gate_obligation_runner.fulfill_obligation` — `sha_unverifiable`, `unresolvable`/`escalate` and `retire` — now live in `_fulfill_sha_unverifiable`, `_fulfill_unresolvable_or_escalate` and `_fulfill_retire`. Behaviour-neutral: same outcome dicts, same obligation records, same order of branch checks. The function drops from 568 to 485 executable lines (G2-1b..d follow).

- Function size (track `functies-onder-200`, G2-7): `scripts/migrate_to_central_vnx.py::_run_apply` drops from 281 to 189 executable lines, below the track's 200-line ceiling. The env-isolation preflight, the `--test-apply` redirect, the `--verify-only`/plan-only modes, the source backup and the pre-import freshness/stub gates now live in `_warn_on_env_leak`, `_redirect_for_test_apply`, `_run_non_apply_mode`, `_backup_before_apply` and `_apply_pre_import_gates` (all ≤ 28 lines). Behaviour is unchanged on every path — same exit codes 0/1/2/3/4, same order of side effects, and the `--test-apply` temp dir is still removed on every exit path; `tests/test_migrate_to_central_vnx_run_apply_paths.py` pins each path before and after the move.

- CI Profile A (OI-1420): the worker instruction that says Profile A "sweeps the whole tree" now names `scripts/ci/test_exclusions.txt` and tells a worker whose change touches an excluded file (or the code it tests) to run that file and report the result. `check_test_exclusions.py --summary` prints `test exclusions: <files> files, <functions> test functions outside the Profile A sweep`, then the `-k` quarantine expression read from `scripts/ci/profile_a_pytest_args.sh` and every extra `--ignore` / `--deselect` that script builds into `serial.args` / `parallel.args` (from the script's literal flags and `scripts/ci/serial_tests.txt`), then any `-m` / `--ignore` / `--deselect` that a `pytest` command in the profile-a job carries (a `-m` outside a pytest command is not a selection); Profile A appends it to `$GITHUB_STEP_SUMMARY` non-fatally; a missing or unreadable input reads `unmeasured` rather than a partial number. No exclusion was added or removed: the 113 files stay excluded under OI-1420's follow-up.

- Planning CLI (`functies-onder-200`, golf 2 G2-3a): the first half of the `plan-gate` argument surface moved out of `planning_cli._build_parser` into its own builders. `_build_parser` calls `_add_plan_gate_parsers(sub, common)`, which builds the `plan-gate` parser with its action subparsers and the `seed`, `run`, `status` and `attest` actions (one helper per action, `_add_plan_gate_{seed,run,status,attest}_parser`) and returns the subparsers object; `_build_parser` keeps building `missing-reasons`, `backfill-reason`, `reblock` and `batch` on that object, unchanged and in the same order (its second half follows in G2-3b). `_build_parser` drops from 424 to 345 executable lines, measured with `function_size_gate.measure_source`; every new builder is at or below 52. No parser changes: same subcommands, aliases, arguments, `dest` names, defaults, `choices`, `required`, `help` texts, mutually exclusive groups and order — `tests/test_planning_cli_parser_shape.py` freezes that surface (and four real command-line `Namespace`s) in `tests/fixtures/planning_cli_parser_shape.json` and compares it on every run.

- Refactor (golf 2, G2-13): the report-to-receipt core keeps its two self-contained steps in private helpers, `_reject_unknown_filename_id` (the OI-1102 register cross-check) and `_resolve_lane_identity` (lane identity). `_build_receipt_from_report_core` drops from 212 to 191 executable lines; receipt bytes, log lines, dead-letter moves and contract-violation order are unchanged.

- Function size (golf 2, G2-12): the opt-in `--auto-route` smart-router step of `provider_dispatch.main` moves into `_apply_auto_route(args, provider)`, which returns the provider `main` continues with. `main` drops from 202 to 172 executable lines; the helper is 33. Behaviour is unchanged: without `--auto-route` nothing runs and nothing is imported, and with it the same decision is written, the same provider/model land on `args`, `VNX_ROUTE_STRATEGY`/`VNX_TASK_CLASS` are set only when there is a primary, and a failure still falls back to `--provider`/`--model` with the same warning. Pinned by `tests/test_provider_dispatch_auto_route_step.py`, written green on the old code.

- Delivery (test-suite-kosten TSK-1): `deliver_via_subprocess` now stops its silence heartbeat instead of only joining it. `_start_heartbeat` returns the silence stop event alongside the thread (four values: lease stop, lease thread, silence stop, silence thread), and the teardown sets it before the join, which keeps the 5 s timeout as the upper bound for a thread that does not stop. Previously the thread waited in `stop_event.wait(30)` and every delivery — and the 52 tests that drive one — paid the full join. The lease heartbeat and the silence-detection logic are unchanged.

- Function size (track `functies-onder-200`, G2-6b of `golf-2-hardenen`): `envelope_govern._govern` drops from 260 to 184 executable lines. The stray-report adoption becomes `_adopt_stray_report`, the unified-report emit becomes `_emit_govern_report` (the emitter is passed in, so the tests that patch `governance_emit.emit_unified_report` keep binding), and the body-contract check with its OI-2519 `governed_status` override becomes `_check_report_contract`. Pure move: same report at the same path, same log lines at the same levels with the same arguments, the same `report_relocated`-then-`report_contract_violated` warning order, the same effective status and receipt, and every failure (adoption, emit, unreadable report) stays non-fatal. Characterised first by `tests/test_envelope_govern_report_steps.py`.

- Review gates (track `functies-onder-200`, golf 2, G2-1b): the four remaining stay-pending branches of `fulfill_obligation` in `scripts/gate_obligation_runner.py` move into private helpers — `_fulfill_stay_pending_live`, `_fulfill_stay_pending_dispatch_live`, `_fulfill_stay_pending_unmeasured` and `_fulfill_escalate_stay_pending_unmeasured`. The branch checks, the obligation records they write and their log lines are unchanged; `fulfill_obligation` falls from 485 to under 396 executable lines.

- Door: `dispatch_cli.build_runtime_snapshot`'s raw-model guard (OI-LANECOERCE defence in depth) moves into `_apply_raw_model_guard`; the constraint verdict tuple is unchanged, in the same order (golf 2, G2-9).

- CI profiling (track `test-suite-kosten`, TSK-0): `scripts/ci/profile_a_slow_tests.py` turns a Profile A JUnit XML into the numbers the track reports its gain against — total test time, test count, and the count and summed time of the tests above `--threshold` (default 2 s), with the median over several files, the N slowest modules from `--top`, a fixed baseline set from `--baseline` (missing tests count 0 s and are listed) and `--exclude-module` for modules that leave every sum and baseline set and are reported separately. Standard library only; exit 2 with a message on stderr for a missing file, an unreadable file or XML without `testcase` elements.

- Planning CLI (`functies-onder-200`, golf 2 G2-3b): the second half of the `plan-gate` argument surface moved out of `planning_cli._build_parser` into one builder per action. `_add_plan_gate_parsers(sub, common)` now builds all eight plan-gate actions — `seed`, `run`, `status`, `attest`, `missing-reasons`, `backfill-reason`, `reblock`, `batch` — via `_add_plan_gate_{seed,run,status,attest,missing_reasons,backfill_reason,reblock,batch}_parser`, in the original order, and no longer returns the subparsers object (nothing read it). `_build_parser` drops from 345 to 252 executable lines, measured with `function_size_gate.measure_source`; every new builder is at or below 64. No parser changes: same subcommands, aliases, arguments, `dest` names, defaults, `choices`, `required`, `help` texts, mutually exclusive groups and order — `tests/test_planning_cli_parser_shape.py` freezes that surface in `tests/fixtures/planning_cli_parser_shape.json` (now with parsed `Namespace`s for the four moved actions too) and compares it on every run.

- Role audit (golf 2, PA-1): the static argparse walk interprets each function once per (call site, argument identity) instead of once per call, the per-module function/import index is computed once per parsed tree, and `tests/test_refactor_targets.py` measures only the file it targets. On this repo one `t0_role_sources_audit.py` run drops from 271,316 to 20,319 `_stmts` calls; `tests/test_t0_role_audit_static.py` falls from ~107 s to ~40 s and `tests/test_refactor_targets.py` from 17.5 s to 0.3 s. Audit output and findings are byte-identical. A memoized call is now stored only when its body created no root parser and no nested call was cut short by the recursion guard, so a parser factory never hands two callers the same mutable parser and a guard-cut result is never reused.

- Plan gate (track `functies-onder-200`, G2-5): `run_plan_gate_for_track` in `scripts/planning_cli.py` drops from 265 to 194 executable lines. The plan-source resolution (`_resolve_gate_plan_source`: `--doc` vs goal_state + deliverables, the two `_error` exits and the `PlanRefusal` result), the verdict text (`_append_verdict_text`: decision, tally, truncation warning and the per-seat lines) and the durable pass record (`_record_plan_gate_pass`) are now their own functions. Behaviour is unchanged: same exit codes, same outcomes, same `stderr_lines`/`stdout_text`/`stdout_json`, same panel, tiebreaker and pass-record calls, pinned by a new characterisation suite.

- `scripts/planning_cli.py` (track `functies-onder-200`, G2-4): the eight write actions of the `objective` domain — `close`, `link-pr`, `unlink-pr`, `remove-dependency`, `unmark-delivery`, `reopen`, `add` and `set-goal` — each get their own argument builder (`_add_objective_close_parser` and siblings), called in their original order by `_add_objective_write_parsers`. `_build_parser` drops from 252 to 101 executable lines, under the 200-line limit of the track, and every new builder is 39 lines or fewer. Behaviour-neutral: the argparse tree is byte-identical (the unchanged `tests/fixtures/planning_cli_parser_shape.json` snapshot proves it), and a new parametrized test parses one full command line per write action and pins the resulting `Namespace`, `func` included.

- Nameting (track `audit-spoor-kern`, K-meet): `docs/investigations/2026-11-06-audit-spoor-kern-nameting.md` meet de vijf doelen op het cohort vanaf activeringscommit `a7a2d7c4` — 30 `dispatch_started`-regels met `lifecycle_mode: v3`, een steekproef van twintig, de harde kill (`abandoned` in 28 s na het eerste sweep-moment), de vier lezers van `dispatches/active/` en de dagelijkse telling (0/0/0). Twee doelen houden niet: de eindregel draagt geen `role` (0/20), en `task_complete` wordt voor een `v3`-dispatch nog wél geschreven door `envelope_govern._govern` (20/20), ook al schrijft de converter hem niet meer (0/20). Als bevinding: 28 `door_bookkeeping_failed`-regels met een `DuplicateTransitionError` op 28 van de 30 cohortdispatches.

- Review gates (track `functies-onder-200`, golf 2, G2-1c): the `escalate_stay_pending` and `stay_pending` branches of `fulfill_obligation` in `scripts/gate_obligation_runner.py` move into private helpers — `_fulfill_escalate_stay_pending` and `_fulfill_stay_pending`. The branch checks and their order, the obligation records they write and their log lines are unchanged; `fulfill_obligation` falls from 392 to 362 executable lines.

- Doctor (track `test-suite-kosten`, TSK-2): the embedded-path census (`_check_embedded_path_assumptions` → `scripts/check_no_file_derived_data_paths.scan_dir`) is memoized per process in `vnx_cli/commands/doctor.py`, keyed on the resolved engine root plus `(path, size, mtime_ns)` for every file it reads — no time limit, and a tree change is always a miss. A second `vnx doctor` run on an unchanged tree skips the 9-10 s AST sweep; `scan_dir` stays a pure function and the new public `clear_embedded_path_cache()` forces a rescan. Locally `tests/test_vnx_doctor_strict.py` + `tests/test_doctor_release_drift.py` drop from 33.2 s to 6.2 s (61 tests both sides). `tests/test_vnx_doctor_strict.py::test_strict_flag_exit_0_on_clean_project` now builds a genuinely clean tree and asserts `exit_code == 0` instead of the assertion `in (0, 1)` that could not fail.

- Test suite (test-suite-kosten TSK-3): the three call-site contracts (spawn scrub-env keys, sessionstart beacons, phantom guard) take their file trees from one session-scoped `repo_python_census()` in `tests/conftest.py` instead of each running `git ls-files` plus `ast.parse` over the repo. Every tracked `.py` file is read and parsed once per pytest worker instead of once per test file (935 parses per contract file before, 935 in total after), and every assertion and failure message is unchanged.

- Test suite cost (track `test-suite-kosten`, TSK-6): the provider-lane tests stop building a real git worktree and stop running a real CLI. Isolation is default-on (OI-1090), so all seven files built and removed a worktree per dispatch while asserting only on receipt fields, prompts and report bodies; `tests/provider_worktree_support.py` now stubs `provider_dispatch._prepare_provider_workdir` (same `(isolation_worktree, worker_cwd)` tuple shape) and records the `_finish_provider_worktree` teardown for every test in them, and each file gains an assertion that `create_dispatch_worktree` is not reached. `tests/test_provider_dispatch_governance_integration.py::test_codex_dispatch_produces_receipt` keeps the real path as the one integration case (`@pytest.mark.real_provider_worktree`, registered in `pyproject.toml`). The two kimi files keep patching `spawn_kimi`, the seam the other kimi tests already use. Measured locally: 152 passed in 83.57 s before, 159 passed in 9.63 s after. No production file changed; `tests/test_provider_dispatch_worktree_isolation.py` still pins the real create/remove behaviour, unchanged.

- Een test verlaat de parallelle Profile A-fase alleen nog als een blokkerende stap in dezelfde job dezelfde controle op dezelfde boom al draait, en `tests/test_ci_gates_still_exist.py` bewaakt die stap: de side-door-audit, de central-mode-padpoort en de twee drift-unittestfiles leveren zo ~40 s per PR op zonder dekking te verliezen (ADR-053, test-suite-kosten TSK-5).

- Test suite (track `test-suite-kosten`, TSK-7): `tests/test_t0_rotate_spawn.py` runs the real
  spawner once per script behaviour instead of once per test. Two module-scoped rigs — a plain
  spawn and a spawn with an active `/goal` delivered into the transcript — carry ten test
  functions, each keeping its own assertions on `calls.ndjson`, the receipts and the files, and
  each group gets its own `tmp_path_factory` directory. Script runs during the module drop from
  97 to 90; no test is deleted and no assertion is weakened. A test that depends on a stub switch
  (`STUB_EAT_FIRST_GOAL_ENTER`) keeps a run with exactly that switch and now also proves, on a
  monkeypatched rig without the branch, that the enter-only assertion fails there.

- Test costs (test-suite-kosten TSK-8): three waits that were the assertion now stop at the measured bound. The T0 rollback tests set `_sigkill_wait_timeout_seconds` to 0.1 s through the attribute production reads and assert the kill finished inside it (10.2 s → 0.3 s per test); the locked live-work test injects a 50 ms busy timeout through `_LIVE_WORK_DB_BUSY_TIMEOUT_MS` and asserts the build gave up inside 1.5 s (18.4 s → 0.4 s); the state-writer concurrent test runs 25 threads × 100 writes (2,500, floor pinned) instead of 100 × 100, and the OI-1005 slow-runner test scales its injected delay to keep its serialized sleep above the old 10 s join. No assertion is weakened and no production file changes.
  The ~15.9 s that the locked test actually spent was the track reconcile's hard-coded busy timeouts (`tracks._get_conn` 10 s plus `qi_db_health.count_tables` 5 s), not the live-work read (2 s); that unrelated wait is isolated like schema init already was, and the lines are named in the report.

- Lifecycle end lines: `dispatch_finished` now carries `role` on every write path. `Handover` gained a required `role` field, `build_end_line` takes it as a keyword-only argument with no default, the lane exit passes `handover.role`, and the sweep copies the start line's `role` onto its `abandoned` line. A pre-`role` frozen intent still recovers unchanged.

- Gate obligation runner (golf 2, track `functies-onder-200`, G2-1d): the takeover booking, the `provider_not_installed` cleanup, the temporary refusal and the runner-error handler of the `attempt_gate` path in `fulfill_obligation` move into four private helpers (`_fulfill_by_takeover`, `_drop_provider_not_installed_result`, `_fulfill_temporary_refusal`, `_fulfill_runner_error`). No behaviour changes: every outcome dict, booked obligation field, log line, ordering and the launch-lock release are identical; `fulfill_obligation` drops from 362 to the measured size in the report.

- Test suite cost (track `test-suite-kosten`, TSK-9): the hash-chain fork test runs eight spawned writers instead of eight and sixteen, the cost-ledger replay tests hold the lock for 1 s instead of 3 s (the wait is two polls of `cost_ledger._LOCK_POLL_SEC`, not a copy), and the chunk/deadline tests use 0.2 s/0.4 s instead of 2.5 s/2.5 s. Every assertion on the chain, on `files_replayed`/`files_remaining`/`stopped` and on "the timeout fired and the process is gone" is unchanged. `VNX_TEST_NO_APPEND_LOCK=1` runs the fork test without the append lock, so the race the lock prevents stays demonstrated. The timeout tests no longer inherit `VNX_CHUNK_TIMEOUT`/`VNX_TOTAL_DEADLINE` from the caller's shell.

- Receipts (ADR-054): the envelope's governance record of a v3 dispatch is written as the non-completion `dispatch_governed` instead of `task_complete`, so a v3 dispatch has exactly one end line. The record keeps its 17 unique fields (role/skill evidence and per-dispatch cost); legacy dispatches are unchanged.

- Handover-helper in test_envelope_governed_record.py geeft de verplichte role mee (audit-spoor-kern E1-fix, OI-2598).

- Gate runner (golf 2, G2-1e): the three hold branches (`_fulfill_declared_sha_mismatch`, `_fulfill_declared_unknown_sha`, `_fulfill_result_head_moved`) and the final booking (`_fulfill_book_declared_result`) of the `attempt_gate` path in `fulfill_obligation` move into private helpers. No behaviour change; `fulfill_obligation` is now below the 200-executable-line limit and `scripts/gate_obligation_runner.py` has no size offender left.

- Function size (G2-15, track `functies-onder-200`): the whole-tree measurement is now a blocking step in the required `Lint Patterns` job, directly after the function-size ratchet. `scripts/ci/check_function_size_tree.py --ref HEAD --prefix scripts/ --max 200` runs on every pull request head and every push to main, so the 200-executable-line limit holds for every function under `scripts/`, not only for the ones a diff touches. The step has no `continue-on-error` and no `if`, and `tests/test_function_size_ratchet.py` fails if it disappears. An empty measurement now fails too (OI-2570): a `--prefix` that matches no file exits 2 with a message on stderr instead of reporting "0 functions in 0 files" and exiting 0. The JSON output keeps `unparsed` as the count and adds `unparsed_paths`, the sorted list of files that did not parse, and the text output names each one. ADR-052 records the limit, the counting rule, the relation to the ratchet of 70, and the rollback (remove the step).

- fix(scripts): orphan_sweep, crash_recovery_sweep and scout_effectiveness import their scripts/lib module first, also with scripts/lib on PYTHONPATH (OI-2593).

- Confidence loop (OI-2597): `_update_confidence_from_receipt` reads the outcome of a v3 dispatch from its `dispatch_governed` record, so v3 dispatches feed pattern confidence again (K-fix-3 had made the hook skip them).

- Door (OI-2592): `_owner_finish` leaves a dispatch row the lifecycle finalizer already closed (completed, failed_delivery, timed_out) alone, so the door no longer books a false `door_bookkeeping_failed` (`_owner_transition:accepted`) on every v3 dispatch.

- Review gates (OI-2591): the codex review seat is told, and handed, a Python interpreter that really imports pytest. The runner resolves the first of `VNX_REVIEW_PYTHON`, `VNX_PYTHON`, `sys.executable`, `python3.12`, `python3` that exists and imports pytest within 10 s, once per runner process; the codex prompt names it in a `Run the PR's tests with <interp> -m pytest <files> -q -p no:cacheprovider` block, and says in `residual_risk` that the tests did not run when none has pytest, and the codex spawn gets `VNX_REVIEW_PYTHON` in its inherited environment (PATH untouched). Measured 06-11: 10–15 s codex reviews on PRs #2415–#2418 booked a PASS on a read of the diff, because the inherited `python3` (3.15) has no pytest; the kimi, glm and deepseek prompts are unchanged.

- Three test files (`test_k_act_activation`, `test_k9e_projection_successors`, `test_k4c_spawn_file`) now also wrap the `governance_receipts.append_receipt_payload` route in their hermetic append, and a guide test fails for any file that wraps only the internal route (track `audit-spoor-kern`, OI-2586). Test-only.

- Briefing (track `audit-spoor-kern`, OI-2594): the SessionStart lifecycle line names the cohort its count was taken over (`lifecycle [cohort all_v3]:` or `[cohort since <8-char sha>]:`, `unknown` for a beacon without one), so a reader can tell the all-v3 count from the one since the activation commit.

- test_dispatch_register is hermetic under an operator VNX_PROJECT_ID: the fixture removes the ambient project id (OI-2584).

- Tests (track `audit-spoor-kern`, OI-2590): the `data_dir` fixture of `tests/test_k7b_manifest_readers.py` pins every path variable the resolver reads inside its own store and removes the behaviour variables, and a test pre-pollutes the environment with a foreign store to prove the v3 lifecycle stays in its own.

- Cost ledger (OI-2595): `cost_tracker` now books the tokens of a v3 attempt from its governance record (`task_complete` or `dispatch_governed`, receipt-v2 `token_usage.input`/`output`) instead of from the token-less `dispatch_finished`; an attempt without such a record still books unknown tokens.

- The lifecycle briefing line now carries the age of its count (`count 14h old`, or `count age unknown`), caps an unmeasured reason at 160 characters with `(+N more)`, and `_lifecycle_count_index_summary` reports a malformed beacon as unmeasured instead of raising (audit-spoor-kern, OI-2557).

- Event archives (track `audit-spoor-kern`, OI-2096, OI-2098): an archive is extended without repeating lines. A retry after a move that died between the archive write and the truncate no longer doubles the events, and the copy into a terminal-pinned archive takes the live file's lock and extends the archive instead of replacing it.

- Test suite (OI-2599): no test signals its own process group any more. `tests/test_codex_spawn_fail_closed.py` patches `getpgid`/`killpg` where a fake pid reaches `_kill_proc`, and an autouse tripwire in `tests/conftest.py` raises instead of sending `killpg`/`kill` to the test's own group or wider. On a Linux runner pid 99 can resolve to pgid 0, which signalled pytest and the runner and cancelled Profile A at ~15% four times on 2026-11-06.

- Investigatie (audit-spoor-kern K-meet-2): de nameting van 2026-11-06 opnieuw gedaan op het cohort ná K-fix-1 (#2433) en K-fix-3 (#2436). `docs/investigations/2026-11-07-audit-spoor-kern-hermeting.md` legt het cohort, de commando's en de tellingen vast. Punt 1 houdt nu op alle velden (20/20 `role` op de eindregel, 20/20 gelijk aan de startregel); punt 3 houdt (0/20 `task_complete`, 20/20 `dispatch_governed`, één `dispatch_finished` per dispatch). De rapportregel was 15/20 op het eerste meetmoment (2026-11-07T00:02:37Z) en is bij hermeting 20/20 (05:01:40Z): de vijf rapporten stonden wel op schijf en kregen hun `report_ingested` tussen 00:08:07Z en 00:08:25Z. Ook gecorrigeerd in het document: de conversie-achterstand is 23 minuten 42 seconden (niet 83) en het pad van de converter-health-beacon is exact benoemd (`/Users/vincentvandeth/.vnx-data/vnx-dev/health/report_to_receipt_converter.json`; de variant onder `state/health/` is een verouderd artefact dat niemand leest). Alleen documentatie.

- Test suite cost (track `test-suite-kosten`, OI-2589 D1): eleven files come off `scripts/ci/test_exclusions.txt` and back into the Profile A sweep. Each measured green solo, green with `VNX_PROJECT_ID=vnx-dev` and green in the Profile A parallel phase of CI run 42318233090: `test_auto_commit_stash_isolation`, `test_context_rotation`, `test_dispatch_daemon`, `test_future_state_reconciliation`, `test_learning_feature`, `test_provider_dispatch_deepseek_harness`, `test_queue_reconciliation_certification`, `test_routing_policy`, `test_runtime_coordination_schema_drift`, `test_tmux_worktree`, `test_worker_state_dir`. Three more (`test_repo_map`, `test_t0_decision_log`, `test_t0_escalations_log`) failed in that sweep (16 failed) and stay on the list with the measured reason.

- Launchd jobs start an interpreter inside `requires-python` (OI-2564): the pinned launcher and the fleet-role-drift, fleet-pin-audit and subsystem-probe templates pick it with the new `scripts/launchd/vnx_python.sh` (engine venv, else Homebrew 3.12, else `python3`) and exit 78 when it is outside the range, so a relinked `/opt/homebrew/bin/python3` can no longer fail them in the background.
- CI Profile A accepts the skip of `test_without_a_venv_the_pinned_312_is_taken` on a runner without the pinned Homebrew 3.12 (`scripts/ci/xdist_expected_skips.txt`), so the xdist completeness step no longer reports `unexpected_skip` for it (OI-2564).
- The pinned skip-set test (`tests/test_ci_xdist_d4_switch.py`) now expects 52 entries and the header of `scripts/ci/xdist_expected_skips.txt` names OI-2564 next to OI-2518 as the second key added after the measured run (OI-2564).

- Kimi worker lanes (`scripts/lib/provider_spawns/kimi_spawn.py`): the per-run `--config` now carries a `PreToolUse` hook backed by `scripts/kimi_deny_git_stash.py`, so `git stash` is refused with the same OI-1447 reason the claude lanes get from `--disallowedTools`; `~/.kimi` is read only, never written.
- The stash deny hook (`scripts/kimi_deny_git_stash.py`) tokenizes on shell boundaries and also looks inside `sh -c`/`eval`/substitutions, so `git stash; true`, `true;git stash pop` and `bash -c 'git stash clear'` no longer walk past it; a command shlex cannot parse is denied when it mentions stash.

- Test session (track `test-suite-kosten`, OI-2589 D2): `tests/conftest.py` removes an ambient `VNX_PROJECT_ID` at import time, so the operator's project no longer outranks the one a test fixture derives. Files that were red only because of it leave `scripts/ci/test_exclusions.txt`.
- Test session (track `test-suite-kosten`, OI-2589 D2 round 2): a function-scoped autouse fixture in `tests/conftest.py` removes `VNX_PROJECT_ID` before every test, so a module that writes it at import (`test_link_sessions_dispatches_cleanup.py`) or a test that leaves it behind (`test_token_harvest.py`) no longer re-poisons a pytest-xdist worker; both now use `monkeypatch`. `test_stub_modules_cleaned_up_after_load_daemon_module` restores the real `learning_loop`, `gather_intelligence` and `cached_intelligence` modules it popped (five `TestConfidenceLogging` tests wrote to the real state dir after it). The Profile A exclusion-list check compares with the parsed list instead of a fixed floor of 100.

- Worker isolation (OI-2588, follow-up of OI-1447): the door refuses a **build** on the gemini worker lane (`REJECT [stash-unrefused-build-lane]`, override `VNX_OVERRIDE_STASH_BUILD_LANE=1`) because that lane has no per-run way to deny the repository-wide `git stash`; codex is deliberately left un-refused (it is the live availability fallback) and litellm/local-gemma are proven to run no builders — see `docs/operations/WORKER_LANE_STASH_POSTURE.md`.
- Worker isolation (OI-2588, round 2): that refusal now rides the door's existing pre-permit lane seam (`_lane_verdict`) instead of growing `run_dispatch`, which stays at its `main` size of 195 executable lines and no longer crosses the "no function above 200" line. The behaviour, its place in the order of checks (after `compile_plan`, before the permit, on the dry-run and the fire path alike) and the override are unchanged.

- Hooks (G3): the T0 context guard leaves this repo's `.claude/settings.json` (it runs at user level via `vnx guard install`), and every remaining hook timeout is now in seconds instead of 5000 (83 minutes).

## [1.7.6] - 2026-10-22

This release carries the T0 rotation follow-up that waits for the end of the kickoff turn in the transcript before it types `/goal`, verbruik 0.1.2 with the kimi countdown to its upper bound, the golf-1 removals of C1, C3, C4a, C4b and C5 with the `retired_paths.d` fragments, the review seat that is passed over for the builder's model (R7b), and the xdist isolation of the model normalizer tests.

No migration ships: no file under `schemas/migrations` or `scripts/lib/migrations` changed since v1.7.5, so every store keeps its `user_version`. Re-pinning a project from 1.7.6 back to 1.7.5 is safe for what this release writes: 1.7.5 reads result records with `.get()` and skips the new `reasoning_effort` and `force_rerun` fields, the `builder_vendor` seat record stays on the request record, an obligation parked at `degenerate_retry_cap` reads as any pending one and keeps its `head_runs` through an update, the `permission_escalations` record shape is unchanged, and nothing in 1.7.5 reads `retired_paths.d`. The one thing that does not travel back is a codex_gate or deepseek_gate verdict, whose prompt changed in this release, so 1.7.5's re-anchor recomputes another hash and refuses it.

- `vnx providers usage` and the `verbruik` mod (0.1.1): a full kimi no longer shows only `vol tot reset`. The kimi row gets `resets_at_latest`, an upper bound for the weekly reset: the first weekly-limit failure of the current unbroken streak in VNX's own kimi receipts, plus 7 days. A kimi answer in any store breaks the streak; without records, or when the bound lies in the past, it stays `onbekend`. The mod shows `kimi reset uiterlijk <dd-mm HH:MM>` in local time. It is a bound, never the exact reset, which the kimi CLI does not name.

- Horizon skill (`skills/horizon/SKILL.md`): the delivery rule (a deliverable carries the work, `dlv-...-rN` for follow-up rounds, `vnx deliverable close` after the merge), the done condition per deliverable in `goal_state`, the close rule per PR (`_delivery_hold`: every linked PR must be `complete`), the missing horizon verb (OI-2272) and a goal that survives a rotation. The `--delivery` help of `link-pr` in `planning_cli.py` and `vnx_cli/main.py` now says the same.

- Review gates (R9): every gate worktree now gets a fixed, gate-authored `AGENTS.md` at its root, written by `create_gate_worktree` for every reviewer seat. It tells the reviewer that all instructions are in the prompt and not to search for AGENTS.md, CLAUDE.md or other instruction files, in the worktree or outside it. codex loads that file at start; the repo's own `AGENTS.md` is gitignored, so the worktree had none and codex spent its first turn searching for one outside the repo, a search whose output it resent on every later turn.
  The file never comes from the tree under review. A PR's own `AGENTS.md` and `AGENTS.override.md` (the name codex prefers over `AGENTS.md`) are replaced or removed in the worktree only, and `git status --porcelain` there stays empty: skip-worktree in the worktree's own index for a tracked name, an ignore rule in the worktree's own root `.gitignore` for a name that is neither tracked nor ignored. Nothing is written to the shared `info/exclude` or any config. A write that fails removes the worktree and is booked as `worktree_checkout_failed`, never a review without the file.

- Review gates no longer send the reviewer looking for instructions (OI-2287): every review prompt now says, after the diff block, that all instructions are in the prompt, so no search for AGENTS.md, CLAUDE.md or other instruction files and nothing read outside the working directory.
  `codex_gate` passes `model_reasoning_effort` next to the model (OI-2288), so `~/.codex/config.toml` no longer decides it: `VNX_CODEX_REVIEW_EFFORT` when set (low, medium, high, xhigh or max; any other value books `unavailable` as `codex_review_effort_invalid`), else `high` for risk class high or critical and `medium` for the rest. The result record carries `model` and `reasoning_effort`, and so does the record of a codex run that timed out, stalled or was refused as degenerate, so an effort comparison counts the failed runs too.

- Review gates (R10, track `review-stopregel-en-lichte-stoelen`): one run per PR head. Every review-gate run now launches under an exclusive, non-blocking lock per (project, PR, gate, head) in `state/review_gates/run_locks/` (`scripts/lib/gate_run_guard.py`), held for the whole run, in `execute_gate` and in the obligation runner. A second launch on a head that is being reviewed does not run: it logs the key and the holder's pid, answers `in_flight` (never a pass) and counts no attempt. A lock dies with its holder; the runner sweeps the files of dead holders.
  - Under the lock the launch reads again: a verdict that landed on the head meanwhile is the answer, not a second run.
  - Retry cap: an obligation gets at most two runs per head in which the reviewer ran without a bookable verdict (`gate_execution_degenerate`, timeout, stall, partial review). Then it stays pending as `degenerate_retry_cap`, the runner reports `retry_capped`, and `open_outcomes` in `t0_index.json` lists it as kind `gate_retry_cap`. A quota or setup refusal does not count; a new head starts at zero.
  - `gate_obligation_runner.py --dispatch-prefix <id> --force-rerun --reason "..."` is the one operator way past the verdict rescue and the cap; the reason is logged and lands on the obligation, the request and the result record.

- Golf 1, C1 (track `golf-1-opruimen`): removed dead code without a caller in repo, runtime or fleet:
  `scripts/dispatch_broker_cli.py`, `scripts/vulture_whitelist.py`, `scripts/update_pane_mapping.sh`,
  `scripts/reconcile_terminal_state.py`, the compat shims `pane_manager_v2.sh`, `smart_tap_v7_json_translator.sh`,
  `unified_state_manager_v2.py` and `dispatcher_v8_minimal.sh`, and the two tests of the `unified_state_manager_v2.py`
  shim. The libraries behind them (`dispatch_broker.py`, `terminal_state_reconciler.py`, `unified_state_manager.py`,
  `pane_manager.sh`, `smart_tap_json_translator.sh`, `dispatcher_minimal.sh`) stay. The dead-code check runs vulture
  on `scripts/` without the whitelist, which had no executable lines; the field-test runner lost its always-`None`
  `scoring_worktree` cleanup.
  - New CI check `scripts/ci/check_retired_paths.py` with the list `scripts/ci/retired_paths.txt`: a retired path,
    `path::symbol` or `env:NAME` fails CI when it comes back or when anything still calls it in an executable way
    (import, `-m`, a command in shell, yaml, json, plist or a settings template, an env read). Comments, docstrings,
    markdown and frozen history do not count. Every later golf-1 cluster adds its removals to the list.
  - A tracked symlink counts when its target, resolved lexically against the link's directory (a dangling link
    too), is a retired path or lies under one; an absolute target inside the repo counts the same way.
  - For a `path::Class.method` entry the class stays importable: what fails is the retired method on the class or
    an instance of it (`K.method`, `K().method`, an instance bound from `K(...)`, `getattr`/`patch.object` on
    either, a `patch("module.K.method")` target). The docstring of the check lists the forms it does not follow.
  - A consumer that tracks a symlink to `scripts/dispatch_broker_cli.py` or `scripts/dispatcher_v8_minimal.sh`
    (mission-control does, via `scripts/bootstrap-mission-control-repo.sh`) keeps a dangling link after it pins
    this release; remove the link and its bootstrap line.

- Golf 1, C3 (track `golf-1-opruimen`): removed the tmux orphan sweep of the pool. `pool_reaper.py` lost
  `_sanitize_session_name`, `list_tmux_sessions`, `session_has_living_children`, `identify_orphan_tmux_sessions` and
  `sweep_orphan_tmux_sessions`; `PoolManager` lost `_kill_tmux_session`, `_is_dispatch_terminal_in_db` and
  `_sweep_orphan_tmux`, and `reap_dead` no longer kills a `vnx-<terminal>` session or sweeps `vnx-*` sessions
  (the tmux lane they belonged to was removed on 2026-09-18). The lease release, membership release and worktree
  cleanup in `reap_dead` are unchanged. The eight names are on `scripts/ci/retired_paths.txt`.

- Review gates (R7, operator decision 2026-10-22): a new review seat, `claude_gate`, runs `claude -p` headless with model `sonnet` on the operator's own claude subscription. It goes through gate_runner's harness-lane path like `deepseek_gate`, with its own dispatcher (`scripts/lib/claude_gate_lane.py`), never provider_dispatch. The seat runs in its own worktree on the PR head with MCP off and is read-only by allow-list: only `Read`, `Grep` and `Glob` exist and are allowed, `--permission-mode dontAsk` and `--permission-prompts none` refuse every other call, and the argv never carries `--dangerously-skip-permissions` or the adapter's role scope, whatever `VNX_WORKER_SCOPED` says. A verdict counts only from a run that ended in a completion event with subtype `success`, `is_error` false and exit 0. A run that hit its turn limit, ended in an error, emitted no completion event or exited non-zero is booked as an execution failure with the cause in `reason_detail`, and its answer stays out of the report, so a PASS in it is never read. No `ANTHROPIC_*` variable reaches the seat, `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` included, even when the process that starts it carries them. Its cost line is 0 with source `subscription`.
  The default takeover chain is now `codex_gate,kimi_gate,claude_gate,deepseek_gate`: every subscription reviewer before the API-credit fallback. With kimi out, the kimi seat goes to claude, no longer to deepseek.
- Review gates (R7): a seat never gets a reviewer of the vendor that built the PR head (`scripts/lib/review_vendor.py`). The builder's vendor comes from the `provider` and `model` of the dispatch spec the review request names. A same-vendor reviewer is passed over like an unavailable one, with `takeover_reason: vendor_matches_builder`. A DeepSeek-built head with kimi out reads `claude_gate`, and a claude-built head reads `deepseek_gate`. When the builder's vendor cannot be determined, no rule applies and every seat records why in `builder_vendor`. The merge door is unchanged: a seat filled by `claude_gate` counts like any takeover, on the exact head only.

- Review gates (R12): a review seat with tools is told to look at its checkout. After the no-search rule of R8 and the AGENTS.md of R9 a small diff gave codex nothing to do, and PR #2173 was refused as `gate_execution_degenerate` four times with 0 shell calls and 0 file reads. The prompt of `codex_gate` and of `deepseek_gate` on the harness lane now says, right after the no-search rule, to check at least one claim of the diff against the working directory before the verdict, and that a verdict reached without looking there is refused as degenerate. The single-shot `glm_gate` and `kimi_gate` do not get the rule on any path, and neither does `gemini_review`, whose builder also serves the vertex route.
  `build_review_prompt` takes a required `agentic` argument: each caller states its lane. The harness-lane fact is `gate_lane_contract.SINGLE_SHOT_GATES` and `reviews_with_tools`, read by the standalone glm and kimi scripts, the harness lane and the re-anchor hash. The gate-authored AGENTS.md keeps its text and ends with one sentence more: inside the directory, the files are there to check the diff against before the verdict. The degenerate floor is unchanged.
  The contract hash of every prompt that gains the rule changes (the codex_gate and deepseek_gate prompts), so a verdict recorded under the old prompt cannot be re-anchored onto a new head. The glm_gate and kimi_gate prompts are byte-identical to before, and their hashes still reproduce.

- Golf 1, C4a (track `golf-1-opruimen`): `vnx permission scan` is gone. Removed the live-pane scan
  `scan_live_awaiting_permission` and its helpers from `scripts/lib/worker_permission_relay.py`, the `scan`
  subcommand and its worktree-risk rendering from `scripts/permission_relay_cli.py`, the usage line in `bin/vnx`
  and the 19 tests of the scan. Every other `vnx permission` subcommand is unchanged.

- T0 rotation (track `rotatie-goal-overdracht`): the `/goal` follow-up after a context rotation waits for the end
  of the successor's kickoff turn in its transcript (a `system` / `turn_duration` record after the kickoff entry)
  instead of on screen text, and is `confirmed` only on a `/goal` user entry in that transcript. On claude 2.1.308
  the spinner no longer shows `esc to interrupt` and the footer stays during a turn, so the screen check typed
  `/goal` into the kickoff turn, where claude queued it as plain text and never ran it (two SEOcrawler rotations on
  2026-10-22).
  - The delivery job passes the transcript it read (`delivery.json`) to the follow-up with `--transcript`; the
    follow-up refuses to run without one. The retry and the LET OP fallback go in on an idle turn only; a turn that
    does not end within `VNX_T0_ROTATE_TURN_TIMEOUT` books `turn_timeout` and types nothing.
  - `VNX_T0_ROTATE_BUSY_PATTERN` is a second opinion now, default `esc to interrupt|… \([0-9]+[hms]` (the 2.1.308
    spinner and the older one). `/goal active` on screen never confirms on its own. The
    `t0_context_rotation_goal_followup` receipt gains `fallback` and `screen_goal_active`.
  - A consumer project gets this with the first release it pins after the merge; until then its rotations keep the
    screen check.

- Golf 1, C5 (track `golf-1-opruimen`): removed `scripts/cleanup_stale_vnx_sessions.sh`, a manual tool for the removed
  tmux lane with no caller in repo, runtime or fleet, and `build_process_gone_failure_report` from
  `scripts/lib/worker_heartbeat.py`, which only a test called. `worker_heartbeat.py` stays for `kimi_spawn.py`.
  Both are on `scripts/ci/retired_paths.txt`.

- Verbruik mod 0.1.2 (`packages/vnx-mods/verbruik`): a full kimi with a known upper bound shows the time left, `kimi terug over 1d 19u (uiterlijk 24-10 13:05) !!`, rounded up so it never promises kimi back too early. At or after the bound it reads `kimi laatst vol, grens 24-10 13:05 voorbij !` (warning) instead of claiming kimi is full now. The display reads the engine's clock, with no new timer.

- Tests (track `ci-profile-a-xdist`, D3-7): `test_normalizer_keeps_5_and_5_5_apart[anthropic/claude-sonnet-5-5-sonnet-5-5]`
  failed only under xdist. `tests/test_codex_model_registry.py` points `provider_registry` at a tmp registry with only
  `openai`; the first `normalize_model_name` call in that worker built its alias tables from it and kept them. Under
  that table `claude-sonnet-5-5` still normalized through the `claude-<family>` fallback, while
  `anthropic/claude-sonnet-5-5` came back unchanged. The file now resets and restores the normalizer caches around
  every test; `tests/test_model_normalizer_registry_isolation.py` runs polluter and victim in one process.

- Review gates (R7b, operator decision 2026-10-22): a seat is passed over for the builder's model, no longer for its vendor (`scripts/lib/review_vendor.py`). A reviewer of the builder's vendor on a different model may take the seat, so a `deepseek-flash` build can be read by `deepseek_gate` on `deepseek-v4-pro`. The reviewer's model is `gate_lane_contract.gate_model` (env var at decision time, else the default); the builder's is the spec's `model`, a placeholder meaning the lane default. Both compare through `model_normalizer.comparable_model_name`, which reads the phased-out `deepseek-v4-flash` as `deepseek-flash`. Same vendor with an unknown model fails closed. The hop reason `vendor_matches_builder` is now `model_matches_builder` (or `model_unknown_for_same_vendor`); records written before keep the old string. A same-vendor reviewer that is let through shows `builder_vendor.rule: model_differs` with `builder_model` and `reviewer_model`.

- Golf 1 (track `golf-1-opruimen`): `scripts/ci/retired_paths.txt` becomes one fragment per cluster under
  `scripts/ci/retired_paths.d/`, named after the dispatch, so parallel clusters merge in any order instead
  of conflicting on the shared file. Every fragment holds its cluster's block unchanged; the header (the
  three line kinds and the rule that a line stays) moved to `scripts/ci/retired_paths.d/README.md`.
  `scripts/ci/check_retired_paths.py` reads every `*.txt` fragment in sorted order as one list, refuses a
  line that appears in two fragments (naming both files), and treats an empty or missing directory as an
  error. `--list` still reads a single file and `--list-dir` reads a directory, which is the default. C4a
  gains its two helper entries (`tests/test_worker_permission_relay.py::_ScanArgs` and `::_make_git_repo`)
  and C5 gains the `Source:` line its neighbours carry. The old single file is itself listed as retired.

- Golf 1, C4b (track `golf-1-opruimen`): the `vnx permission window-*` subcommands and the relay tick loop are gone.
  Removed `PermissionWindow`, `is_catastrophic`, `parse_pending_command`/`_strip_box`, `decide`, the fingerprint
  helpers, `_capture_pane`, `relay_tick`, `run_relay_loop` and `RelayHandle` from
  `scripts/lib/worker_permission_relay.py`, the three `window-*` handlers and parser entries from
  `scripts/permission_relay_cli.py`, the usage lines in `bin/vnx`, and the 55 tests of those paths.
  `escalations`, `approve`, `deny` and `resolve-stale` are unchanged.

## [1.7.5] - 2026-10-22

The dispatch guard now decides GO only on live work and a pending queue measured at the call (`vnx status --json --live`). It gives WAIT when that measurement is missing, stale, partial or failed (OI-2268, #2166). The pin shim now finds `versions/v<pin>` when a project pins the bare version (OI-2270).

Move a project's pin to 1.7.5 before it runs the new guard. A project that stays on 1.7.4 gets WAIT, because the 1.7.4 `vnx status` refuses `--live`. This release has no migration: no file under `schemas/migrations` or `scripts/lib/migrations` changed since v1.7.4, so every store keeps its `user_version`.

- Fixed (OI-2268): the dispatch guard measures live work and the pending queue at the moment of the call.
  `dispatch_guard.sh` read GO/WAIT from `t0_state.json` as it was last built, so a dispatch row in flight or a
  pending bundle that appeared after that build read as GO. The guard now asks `vnx status --json --live`, which
  measures `live_work` (the `dispatches` table plus the occupancy flock) and `queues` (`dispatches/pending` and
  `conflicts`) with the functions `build_t0_state.py` uses, in about half a second on vnx-dev instead of a 15 s build.
  The answer carries `live_measured_at` and `live_measurement`; GO requires a live measurement of both sections, at
  most 10 s old, with no read that failed. A read that fails is WAIT with its reason, and a queue count that is not a
  number is WAIT instead of zero.
  - A CLI older than this release gives WAIT (`live_unmeasured: cli gave no live measurement`): the 1.7.4 wrapper
    refuses `--live`, the guard then reads the plain answer and finds no live marker. Pin a project to this release
    or later before relying on GO.
  - `vnx status --json` without `--live` is unchanged apart from two added fields: `live_measured_at: null` and
    `live_measurement.mode: "snapshot"`. The human view of the guard prints a `Measured:` line, and
    `intelligence.sh` reads the same live answer, shows an unmeasured count as `unmeasured` and marks a snapshot answer.
- Fixed (OI-2270): the pin shim now finds `versions/v<pin>` when a project pins the bare version (`1.7.5`), so the guard works in a consumer.

## [1.7.4] - 2026-10-22

This release mainly enforces the provider switch (ADR-044). Every launch function in the census now calls `provider_switch.require`, the `vnx providers` write verbs (`off`, `on`, `reset`, `repair`) are live, the door refuses a switched-off provider, and the plan-gate and the deliberation panel leave a switched-off seat out and refuse below their floor. Claude and codex are outside the switch. It also adds `vnx providers usage` and the `verbruik` mod 0.1.0 that shows its figures, the `context-meter` plugin 1.0.0 with `context-rotation` 1.0.2, the cost ledger's spool replay (K2a-3b) with its dispatcher tick, the `cost-ledger` beacon and the ledger files in `vnx doctor` (K2a-3c) together with the OI-2250 repairs, `voortgang` 0.4.0 where the deliverable is the unit of a track row, and a fix for the launchd reload under `pipefail` (D3-6).

No migration ships: no file under `schemas/migrations` or `scripts/lib/migrations` changed since v1.7.3, so the store schema stays as it was. Re-pinning a project to 1.7.3 leaves the files 1.7.4 writes readable. 1.7.3 lists only the top-level `*.json` files of the cost ledger spool and ignores `quarantine/`, names spool files by the same rule, and checks a spool record more loosely than 1.7.4 does, so it accepts every record 1.7.4 writes to the spool, while 1.7.4 quarantines records that 1.7.3 accepted. It reads the `cost-ledger` beacon as any other beacon and reads the switch file, which stays at format version 1. Nothing in 1.7.3 replays the spool or refreshes that beacon, so a degraded one keeps the health summary at `stale` and reads `unknown` after seven days. The switch itself does not travel back. 1.7.3 honors it in the gate runner, the gate recorder, the deep analyzer, the dream cycle and `classifier_providers.get_provider`, and not at the door, in `provider_dispatch`, in the panels, in the spawn functions or in the adapters. Its write verbs refuse while launch functions are pending there, so turn a family back on with 1.7.4 before pinning back. The `verbruik` mod checks no version string and runs the `vnx` that `~/.vnx-system/current` points to. With 1.7.2 or 1.7.3 there it shows only the Claude part, because `providers usage` is refused with `invalid choice`. With 1.7.1 or older there the `providers` command is unknown (exit 1) and the mod reports a failed measurement instead. The content boundary at the door and the panel and the nightly content-boundary check are still not in this release.

- Added: the spool replay of the cost ledger (ADR-043 §6, K2a-3b): `cost_ledger.replay_spool` and `vnx costs replay [--json]`
  (script, pip CLI and `bin/vnx`) bring spooled lines back into the ledger, one spool file per exclusive hold of the lock
  file, at most `VNX_COST_LEDGER_REPLAY_BATCH` files in `VNX_COST_LEDGER_REPLAY_BUDGET_SEC` seconds. Under the lock file
  the ledger `flock` is taken with `LOCK_NB` as well, bounded by what is left of the wait and the budget, so a writer of an
  older fabric version cannot hold the replay, and every current writer behind it. The receipt the replay writes is
  bounded by the same deadline: `emit_governance_receipt` and `append_receipt_payload` take an optional `lock_timeout`
  (default unchanged), a bounded append queues its central mirror for the next unbounded one, and a receipt lock still
  taken makes the call `lock_busy` with the spool file intact. A file is deleted only after the ledger is fsynced, also
  when nothing was appended, and each of its records, written now or landed before, reads back as its own complete line
  with the same content, field by field, so a killed replay leaves it and writes no line twice. A ledger line with a
  spooled `record_id` and other content keeps the file, writes nothing for it and ends the call with
  `readback_mismatch`; the replay goes on with the next file.
  Exit 0 done, 1 an unreadable spool directory, a write failure or a mismatch, 3 a lock busy.
  - A second execution under one `run_id` (an f2bef053 writer booked an attempt again while it waited in the spool) is
    booked under the next free attempt with `renumbered_from` and a `cost_ledger_renumbered` receipt. A correction of a
    renumbered record follows it under the renumbered run id, with `supersedes` naming the renumbered line, also when it
    comes in a later call; the writer leaves such a correction to the replay (`landed == "spool"`).
  - A spool file that is not one run's goes to `provider_costs.spool/quarantine/` (OI-2243), each one a file of the
    batch and the budget; its receipt is booked before the move, and a move that failed after it is finished without a
    second receipt. It counts in the new `LedgerTotals.spool_quarantined`; it no longer blocks `record_next_attempt`.
  - The aggregation (OI-2244, OI-2241): a spooled record already in the ledger with the same content is no backlog, any
    unreadable line makes the total incomplete, a reader of a store it may not write reads without creating the lock
    file (`unlocked_read`), and the text says `1 run wacht op replay`.

- Launchd reload (D3-6, track ci-profile-a-xdist): `reload_plist.sh` no longer tests for its placeholders through `printf | grep -q`.
  Under `pipefail` that pipeline ends on 141 when grep exits at the first match while printf is still writing, and on a loaded Linux runner the project-id substitution was skipped (measured 14 of 6000 runs under CPU load, 0 unloaded).
  The three checks now match in the shell, so the two reload tests that failed only under the xdist shape stop flaking.

- Added: `vnx providers usage [--json]` (script, pip CLI and `bin/vnx`), the usage and balance of every provider in one read-only command and the data source of the usage mod (`scripts/lib/provider_usage.py`, schema `vnx.providers.usage/1`). One independent row per source: codex from the last `rate_limits` line of the newest rollout, kimi from the reachability records of every project store (a status, never a percentage; a weekly limit booked as `auth_401` reads as `vol tot reset`), deepseek and openrouter from their balance endpoints, claude as unavailable outside a session. A failing source gives `available: false` with a reason and never touches another row; each HTTP call has a 5 second timeout; no key is printed. The balance probe sits on `provider_switch.EXEMPT`.

- voortgang 0.4.0 (OI-2248): the deliverable is the unit of a track row (`3/12`, then `bezig:` and `volgt:` with `(+N)`), a row has segments with their own tone (a new `ok` for the filled part of a bar, `ci groen` and `klaar voor merge`; `warn` for what waits on the operator; `error` for `ci rood` and `onbeschikbaar`), and the open PRs moved from their own section and from the track rows into `Nu bezig`.
  - `Wacht op een besluit` counts `N te promoveren (now)`, only the proposed deliverables on tracks with horizon `now`, with a dim `backlog` row for `next` and `later`. Orphans on a track with phase `done` are not counted (OI-2249).
  - `/voortgang plan` shows the tracks on `next` and `later` with their `volgt:` rows. The 0.3.1 changelog line about `dispatch_id: null` is corrected, `openPrsOf` is removed and the "whatever the index lists" test now gives the index a PR the queue misses.

- Added: the `context-meter` plugin (`packages/claude-code-toolkit/context-meter`, 1.0.0), a Claude Code mod that draws one line above the prompt with the context fill in the bands of the context-rotation guard: green below warn, yellow from warn, red from force, with what the guard does there and the next threshold. It reads the guard's settings and its per-session `start_size` (the `min_work_tokens` hold), notes a window that does not match the percentage bands, and hides itself with `show_meter: false`. It is a plugin of its own because 2.1.303 loads a hooks module too and 2.1.300 to 2.1.302 were not measured (Part 0).
  - `context-rotation` 1.0.2: the `show_meter` setting (a known boolean key in `DEFAULTS`) and `tests/test_meter_parity.py`; the settings loader and `band_guard._loop_level` are held to one fixture (43 settings cases, 14 min-work cases) that the meter's TypeScript port reads as well.
  - `scripts/maintenance/export_toolkit_plugin.sh` takes `--plugin <context-rotation|context-meter>` (default `context-rotation`, the tag prefix and branch follow the plugin).

- Added: the dispatcher prelude replays the cost ledger's spool (ADR-043 §6, K2a-3c). `_maybe_cost_ledger_replay` starts
  `scripts/cost_ledger_replay.py` at most once per `VNX_COST_LEDGER_REPLAY_INTERVAL_SEC` (60) seconds, in every supervisor
  mode, as a subprocess with a hard timeout of 20 seconds (`VNX_COST_LEDGER_TICK_TIMEOUT_SEC` may only lower it) and kills
  it after that. It logs every failure (a non-zero exit, a kill, a log or threshold file that cannot be written) and always
  returns 0; a threshold file that cannot be written still throttles the tick in memory. With an empty spool the replay
  writes nothing.
  - The `cost-ledger` beacon (`<data_dir>/health/cost-ledger.json`, event-driven) is written after every replay: degraded
    (`stale` in the beacon file, `details.state: degraded`) while the spool holds a file, an unreadable file or a file in
    quarantine, ok once it holds none; the details carry `spool_files` and the spool counts of `LedgerTotals`. Its shared
    lock, the ledger `flock` and the beacon's own lock end at one deadline, `VNX_COST_LEDGER_LOCK_WAIT_SEC` after the
    call began, so it never hangs behind a writer and never keeps one out longer, also in `vnx costs replay` run by
    hand; a beacon that cannot be written in time is logged and skipped, and the replay's result and exit code stay.
    An ok beacon older than a day (`BEACON_REFRESH_SECONDS`) is written again on an empty spool, so a healthy store no
    longer reads `unknown` from day 8; a tick that stopped running still does.
  - `vnx doctor` (both entrances) and the `vnx-data-footprint` probe know the cost ledger's own files: the ledger,
    `events/provider_costs.lock`, the spool and its quarantine (`cost_ledger.ledger_files`, read-only, nothing created).
    The doctor warns on a waiting or quarantined spool file and names an entry of the spool that is none of its own;
    one file reads `1 spool file waits for the replay` and `spool 1 file`.
- Fixed (OI-2250, the advisories of #2155): a renumbered line carries the spooled `record_id` in
  `renumbered_from_record_id`, so a correction of a correction of a renumbered run follows it ($2.60, not $1.35) and a
  replay killed between them writes no duplicate. A spool record without a splittable `run_id`, its `project_id`, a `kind`
  or a correction's `supersedes` string goes to quarantine instead of ending the replay in a traceback. The lock file wait
  is clamped to the replay's budget. A bounded receipt append records its mirror debt under the append lock it wrote the
  receipt with, so contention can no longer drop it. The replay's receipts land in the project's resolved `VNX_STATE_DIR`.

- Provider switch, panels (track provider-schakelaar, deliverable D): the plan-gate and the deliberation panel honor `vnx providers off`.
  A plan-gate seat whose provider family is switched off for the track's project is left out before anything is dispatched.
  The run output, the round's decision text (`gov_trace` and the rationale) and `--json` (`switched_off_seats`) name it, it is never booked as a seat, and nothing takes its place.
  - One floor helper, `plan_gate_panel.panel_floor`, holds the rule `apply_panel_rule` already applied (a multi-seat panel keeps two voices). The seat assembly applies it to the seats the run was entitled to before removal: below it the run refuses before any dispatch, books no round and exits 4 (`REFUSED_PROVIDER_SWITCHED_OFF`), naming the seats, `vnx providers on <family>` and operator attest. A panel entitled to two or more seats never certifies on one voice.
  - `--panel-seats` that names a switched-off seat refuses the same way. A switched-off escalation seat is left out and the run goes without one.
  - A switched-off tiebreaker refuses before any call (`TiebreakerSwitchedOffError`, exit 4); the track stays blocked. The seat refusal applies only when a seat round would run, so a run that goes straight to the tiebreaker is not stopped by it.
  - `plan-gate batch` tallies the refusal under its own bucket (`GEWEIGERD-provider-uit`) and does not mark the track done, so the next batch runs it once the provider is back on. The `--dry-run` preview names the left-out seats.
  - `vnx panel` (`scripts/panel.py`) decides the switch for the current project: the default roster drops each switched-off seat with one named line, and below `synthesis_min_seats` the run refuses before any dispatch (exit 4) unless `--allow-degraded`. `--seats` that names a switched-off seat refuses.

- verbruik 0.1.0 (V2): a new mod in `packages/vnx-mods/verbruik`, one status line with the usage of every subscription and the balance of the API providers. Claude comes from the session (`claude 5u 34% · week 61%`), codex, kimi, deepseek and openrouter from `vnx providers usage --json`, refreshed every `refreshMinutes` and kept as `(oud)` when a run fails.
  - Needs VNX 1.7.4 for the other providers; an older vnx shows only the Claude part and `vnx te oud voor providers usage`. `/verbruik` lists the full figures. The status line takes no style, so a warning ends in `!` and an error in `!!`.

- Provider switch, layer 1 and the dispatch path (provider-schakelaar B): every launch function in the census now
  calls `provider_switch.require` before its first launch, so `PENDING_B` is gone, `LAUNCH_FUNCTIONS` is the enforced
  set and the `vnx providers` write verbs (`off`, `on`, `reset`, `repair`) are live. A switched-off family raises
  `ProviderDisabledError` before any process, request or `litellm.completion` starts.
  - `spawn_claude` checks the `ANTHROPIC_BASE_URL` its child receives (extra env over the process env): a
    non-Anthropic host is decided as that host's family, an OpenRouter host on the maker of the model, an unmapped
    host is refused. The glm and deepseek harness spawns keep their own check and name the family of their redirect
    (`base_url_family`), so a `VNX_GLM_PROXY_URL` on any host or port is decided as glm.
  - `spawn_local_gemma` decides for the `project_id` it is given, for MLX and the Ollama fallback alike; the envelope
    passes the plan's project.
  - The door refuses a switched-off provider with `REJECT [provider-disabled]` and the decision detail, on the dry-run
    and on the fire path, before the reachability probe. A refused fire lands one `provider_switched_off` dispatch
    receipt; the dry-run writes nothing. `provider_dispatch` refuses by name with exit code 4, before
    the route constraints and the daily budget. The router's `lane_available` skips a switched-off lane, names the
    skip in the route reason and never writes a cooldown for it.
  - `provider_dispatch`, `envelope_adapters_claude` and `subprocess_adapter` stamp `VNX_PROJECT_ID` and
    `VNX_WORKER_DISPATCH_ID` into every worker env (`provider_switch.worker_env_stamps`), so the door, the launch
    function and the worker decide for one project, and the write verbs refuse inside any worker (exit 5).
  - A refusal is never a reachability fact: `failure_classification` has the class `provider_switched_off` (matched
    before every keyword bag, escalation `no_climb`), and `provider_reachability.classify_failure_text` ignores a
    refusal text, so switching a family back on lets the next call through at once.

- Provider switch (provider-schakelaar F): `docs/core/PROVIDER_SWITCH.md` is the operator reference for `vnx providers` (families, precedence, version policy, fail-closed and `repair`, write protection, and how a refusal shows up per consumer, including what fail-closed costs), and ADR-044 records the decision. `docs/core/DISPATCH_RULES.md` points to it. The "not enforced yet" pending mechanism is gone: `pending_launch_functions()` is removed and the coverage guard asserts that every launch function in `LAUNCH_FUNCTIONS` calls `provider_switch.require`. `vnx providers list --json` reports `enforcement` as `{"launch_functions": N}`; the `enforced` and `pending` keys are gone because nothing is pending. No other change in behavior; the write verbs were already live.

## [1.7.3] - 2026-10-21

This release carries the tiebreaker at the B3 review cap with its verdict table (migration 0035, ADR-041, R2a and R2b). It also carries the cost ledger: one line per run with a mandatory source and dated price versions (ADR-043, K2a-1 and K2a-2, #2126), one ordering lock file with the spool as the write fallback, and `spool_conflicts` (K2a-3a). The `voortgang` mod in `packages/vnx-mods` now shows the progress of one project per session, with a bar per track and a status line (0.3.0 and 0.3.1). The T0 rotation hands the first message over as a start argument (OI-2102). The shipped skills are consumer-safe and resolve the vnx CLI in one order (#2146, #2147, OI-2226), and a release PR gets a review task that sends the reviewer into the tree (OI-2242).

The first state build on 1.7.3 moves a store to `user_version` 35. Migration 0035 only creates the table `review_cap_verdicts` and one index on it, and alters no existing table; 1.7.2 never reads or writes that table, so re-pinning a store at 35 to 1.7.2 is safe. The content boundary at the door and the panel (#2063) and the nightly content-boundary check (#2061) are still not in this release.

- Model-pricing grootboek (ADR-040): prijzen zijn nu gedateerde versies in `wave7_models.yaml` in plaats van één platte paarwaarde plus een hard-coded `_PROVIDER_RATES`-tabel in `provider_costs.py`. Elke geprijsde model draagt `price_versions` (in/uit/cache-read/cache-write per Mtok, plus piek/dal waar de provider die kent), en elke alias van een model lost op naar dezelfde versies — het `opus`-tegen-`opus-4-8`-conflict is weg. Eén functie `price_run(provider, model, segments)` is de prijsingang: segmenten splitsen een run op een prijsgrens (het DeepSeek-piekvenster 01:00-04:00 en 06:00-10:00 UTC op werkdagen, dal is de helft), zodat een run die de grens kruist met twee tarieven wordt geprijsd. Een model zonder prijs retourneert `unavailable:no_price:<model>` — nooit een stille 0. De tarieventabel is verwijderd; `codex_wrapper`, `gemini_wrapper` en de `provider_dispatch`-fallback prijzen allemaal via `price_run`. DeepSeek- en OpenRouter-prijzen zijn opgenomen zoals gemeten op 2026-09-14 (`deepseek-flash` 0.30/1.20 piek en 0.15/0.60 dal, `deepseek-v4-pro` 1.32/3.96 piek en 0.66/1.98 dal, z-ai/glm ongewijzigd), met die meetdatum — geen verzonnen prijs.

- Cost provenance no longer collapses to a silent $0 (dispatch 20261019-kp-k1-r2, fix-forward on #2126): `provider_dispatch._compute_cost` and `_compute_kimi_cost` now return `(cost_usd, cost_source)` and every caller of `price_run` carries the source through to the written record — the cost event (`emit_provider_cost`), the receipt (`ReceiptV2`/`emit_dispatch_receipt`/`receipt_writer`) and the report frontmatter (`_build_frontmatter`). An unavailable price stays `cost_usd=None` with a `cost_source` of `unavailable:no_price:<model>`, so a billable run with no measured price is never recorded as free; a genuinely free run (measured $0, e.g. local inference) keeps `0.0` with its own `<model>@<version-date>` source. `_build_frontmatter` no longer rewrites `None` to `0.0`.
- Price-version parsing validates rates: `provider_registry._parse_price_version` rejects a NaN, infinite, or negative `input_per_mtok`/`output_per_mtok` (and every optional rate) before it becomes a price.

- An unknown price is a valid report again (dispatch 20261020-kp-k1-r3, fix-forward on #2126, OI-2194): `schemas/unified_report_v1.json` accepts `cost_usd: null` only next to a `cost_source` that starts with `unavailable:`, and refuses an `unavailable:` source next to a number, so an unknown price can never be written as `0.0` (both enforced in the schema's `allOf` by the Draft 2020-12 validator). Before this, `_build_frontmatter`'s null failed with `invalid type: cost_usd (expected number)`, the converter booked the report as a fail-closed `task_failed`, and `VNX_SCHEMA_STRICT=1` aborted the report write. One helper, `unified_report_schema.report_cost_fields`, now builds the pair for the provider lane: a null cost always carries its reason, `unavailable:no_usage` when usage was missing. A failure inside pricing (`_compute_cost`, `_compute_kimi_cost`) now returns `unavailable:price_error` instead of an empty source, so it is never stamped as `no_usage`. The govern step (`dispatch_govern`) writes an unavailable price as null with its source instead of `float(None or 0.0)`. The converter copies the report's `cost_usd`/`cost_source` to the receipt unchanged.
- Readers that sum or average `cost_usd` count an unknown price and never read it as 0: `cost_tracker.recent_costs` and `paid_lane_budget.spent_today` return and log the unknown count, `scout_effectiveness` reports it per cohort and for scout receipts, and the benchmark harnesses (`run_lane_bench`, the field-test scorer and reporter, `export_routing_matrix`) show it next to the known total. `run_lane_bench` no longer crashes on `cost_usd: null`, and the field-test scorer no longer gives an unknown price the free-run cost-efficiency score.

- Review stop rule (R2a, ADR-041): a PR at the B3 cap gets a next step. `vnx review cap-verdict --pr N --for-dispatch <id>`
  asks one registry model for one outcome of the cap rule and records it in `review_cap_verdicts` (migration 0035,
  ADR-007 key). Never a findings list. `verkleinen` grants one extra codex-seat round to the named dispatch,
  used by the first codex verdict on a head with a commit of that dispatch; the merge door counts only that round's PASS.
  `terug-naar-plan` and `mergen-met-OI` are recorded as `outcome_unavailable_in_r2a` and leave the cap closed.
  - `vnx review rounds open` on an opened PR releases a head whose verdict reached the round ledger while its
    result record did not (an interrupted publication).

- **Store-guard tests hold under xdist (D3-1).** Two tests swapped `vnx_paths`
  for later tests in the same process. `test_resolve_project_id_with_env_override`
  reloaded it and `test_append_receipt_stamps_project_id_from_env` re-imported it
  after a `sys.modules` pop. Either one left a second `TestIsolationGuardError`
  class, so the isolation-guard tests in `test_test_runner_store_guard.py` and
  `test_receipt_mirror_isolation.py` failed depending on worker order. The reload
  is gone, the re-import puts the original modules back, and the guard tests
  look up the guard class when they assert instead of at import. The guard itself
  is unchanged.

- Receipt-processor launchd start-skip tests poll (10 s deadline) for the stub's marker instead of a fixed 0.3 s sleep, and the skip cases hold an explicit 1 s absence window (D3-5).

- Fixed: three kimi heartbeat tests no longer fail under xdist load: the silent-worker test polls for the failure report, every test in the file writes under its own temp data dir with a unique dispatch id, and the two timing tests keep at least 25x the poll interval between activity and threshold.

- Review stop rule (R2a follow-up): the cap verdict, its grant lookup and the tiebreaker's input are hardened.
  - OI-2201: `vnx review cap-verdict` writes the `review_cap_verdict` receipt before the verdict row (ADR-005).
    A receipt that does not land leaves no row and no granted round, and the command exits 1. A receipt that landed
    while its row did not is booked from the receipt on the next run, without asking the model again.
  - OI-2202: a git call in the grant lookup that fails or times out is a logged "no grant", never an exception.
    The lookup shares one 30 s budget, and the round-ledger writer resolves it before it takes the ledger lock.
  - OI-2203: a `vnx-review-cap-tiebreak` fence in the tiebreaker brief's input is disarmed, and a value that is no
    commit SHA (`[0-9a-f]{7,40}`) never reaches git; SHAs go after `--end-of-options` and `--`.

- Plan-gate tiebreaker STOP (OI-2197): de open items van de STOP-afhandeling landen nu in de centrale state-dir die `plan-gate run` zelf al heeft opgelost (volgt `VNX_DATA_DIR`, `VNX_DATA_DIR_EXPLICIT` en `VNX_STATE_DIR`), niet meer in `<repo>/state/`. `open_items_state_dir_for(seat_ledger_path)` is vervangen door `open_items_state_dir_from(central_state_dir)`; de seat-ledger blijft in de repo. Zonder centrale state-dir legt de STOP niets vast, meldt dat op stderr en blijft het gate-resultaat staan.

- Tests: the once-per-process warning test no longer depends on test order. Its fixture resets the sentinel on every module object the validation consults, resolved from the validation function the test actually calls, so it also holds under xdist and after an earlier test re-imports `append_receipt_internals` (D3-2).

- The t0-orchestrator skill scripts answer the same from any cwd (dispatch 20261020-t0skill-guard-fix, OI-2208): `dispatch_guard.sh` runs every `vnx` call bound to its own repo root, `intelligence.sh` resolves its state and dispatch dirs through the central `vnx_paths.py` (an explicit `VNX_STATE_DIR`/`VNX_DISPATCH_DIR` wins; unresolvable means `Error: VNX state dir unresolved`, never `/state`), `can_dispatch` delegates to the guard (YES/NO/UNKNOWN, never YES on missing state) and no longer reads `t0_brief.json` (`check_terminals`/`check_blocked` are gone because `vnx status --json` carries no terminals, `check_live_work` replaces them), and both `SKILL.md` copies name `claude_headless` as the only claude lane instead of the removed tmux-spawn lane.

- voortgang mod (`packages/vnx-mods/voortgang/`): a VNX mod for Claude Code that shows VNX progress per project in a pane (`/voortgang`) and a status line: live work, open outcomes, open PRs with their review seats, now-tracks with their deliverables and the deliverables waiting on a promote. It ships with a VNX release (a local marketplace `vnx-mods` in `packages/vnx-mods/`, installed from `~/.vnx-system/current/packages/vnx-mods`) and is not exported to the public toolkit. It finds every project store under the data root itself (a child holding `state/t0_index.json` or `state/t0_state.json`, ordered by id, `exclude` to leave ids out) and carries no project names. It reads one directory listing of the data root, two files per project and runs two `vnx ... list --json --state-dir` calls, writes nothing, and shows a failed read as `onbeschikbaar` or `geen store`, never as zero.

- Review cap (R2b round 2, ADR-041): the merge door now reads a `terug-naar-plan` verdict from its receipt as well as its row, so a receipt whose row commit failed refuses the PR exactly like the row, `--override-b3` included. The deliverable's move back to `proposed` writes its `review_cap_aftermath` receipt inside the transaction, before the commit (ADR-005). A rerun of `vnx review cap-verdict` restores every receipted aftermath step from its receipt instead of running it again, so a replaced result record no longer blocks a retry of the PR close.

- Review cap (R2b round 3, ADR-041): the `terug-naar-plan` aftermath looks up and closes the PR in the repo of the store's project (`resolve_forge_target`, passed as `--repo` to `gh pr view` and `gh pr close`), never in the repo of the cwd. `vnx review cap-verdict --data-dir` of project A run from a checkout of project B no longer closes B's PR of the same number; a store whose repo cannot be established closes nothing and says why. The close comment carries the verdict id in a marker line and the PR's comments are read first, so a close that failed after its comment is retried without a second one.

- Review cap (R2b, ADR-041): `vnx review cap-verdict` now carries out `terug-naar-plan` at B3.1. The remaining codex blockers become open items, the deliverable goes back to `proposed` and the PR closes with one comment. Every step is idempotent on the verdict id and leaves its own `review_cap_aftermath` receipt; exit 3 means "verdict stands, run again". The merge door counts no PASS of a PR sent back to plan, `--override-b3` included. Receipt recovery now matches `project_id` and the PR's current count (OI-2207).

- T0 rotation: the successor's first message is a start argument, not keystrokes (OI-2102).
  - `t0_rotate_spawn.sh` starts `claude --model opus ... --session-id <uuid> "/kickoff rotatie <run_id>"`. Nothing is typed into the TUI input box, so a redraw during startup (Remote Control connecting) can no longer wipe the instruction.
  - A server-side job (`scripts/lib/t0_rotation_delivery.py`) reads the successor's transcript, compares its first real user record to the first message, writes `delivery.json` and a `t0_context_rotation_delivery` receipt. The old window closes only on `confirmed`; on `mismatch` or `timeout` both windows stay open. The last output line gains `close=on_delivery_confirmed`.
  - `t0_rotation_state.py` gains `run-dir <run_id>` and `kickoff-done --run-dir <dir> [--catch-up-done]` (exit 3 without a newer wrap); `compose` writes `continuation.json` and `prompt.txt` no longer carries the kill-window sentence.

- T0 rotation delivery, round 2 (OI-2218, OI-2102): the delivery job no longer breaks the direct-tmux freeze and its ordering is receipt first.
  - The tmux-driving job moved to `scripts/t0_rotation_delivery_job.py`; `scripts/lib/t0_rotation_delivery.py` keeps only the transcript logic. One job now waits for the verdict, books the receipt (the ledger must take it), writes `delivery.json`, runs the `/goal` follow-up on `confirmed` and only then closes the old window. A mismatch, a timeout or a refused receipt types nothing and closes nothing. The job starts after the spawner's last stdout line.
  - `t0_rotation_state.py event` and `kickoff-done` exit 1 when the ledger append failed; `kickoff-done` books the receipt before it writes the marker. `continuation.json` stores the handoff as an absolute path.
  - The spawner checks the start folder with a plain `cd --` before claude is started, so the first message never reaches a successor in the wrong folder. A failing session-id generation is exit 5. The delivery check accepts the expanded slash-command form and the plain text of the start argument.

- Review gates (dispatch 20261021-adr010-rolprompt): the reviewer and security-engineer role prompts summarise ADR-010 as ADR-010 says it.
  Claude is reached only through the `claude` CLI subprocess; `severity: error` covers an Anthropic SDK import, direct HTTP to `api.anthropic.com` or a REST shim around it, LiteLLM for Claude, and an Anthropic API key or OAuth token in code or config.
  A non-Claude provider called over HTTP or LiteLLM with its own key is outside ADR-010 and no longer a finding under it; the entry cites the ADR line that permits it.
  The old "LLM delivery happens exclusively via a CLI subprocess" made codex block a DeepSeek HTTP lane on one run and pass the same question on another.
  `tests/test_adr010_scope_prompts.py` pins the wording through `PromptAssembler`, the path the gates read.

- Added: the cost ledger per run (ADR-043, K2a-1): `cost_ledger.record_run` writes one `provider_cost_run/1` line per run and refuses an amount without a source, and `aggregate_runs` counts the last line per run and never sums `unavailable` as 0.
  An append that fails raises `CostLedgerWriteError` (an `OSError`): the call fails and nothing is kept elsewhere. The spool and replay of ADR-043 §6 land in the second K2a PR.

- A cost-ledger line always starts on its own line (dispatch 20261020-kp-k2a1-r2, round 2 on #2141, ADR-043).
  Every append in `cost_ledger.record_run` first ends a torn last line with `\n` under the ledger lock.
  No byte is removed: the torn prefix stays and counts as one unreadable line, and the next record starts on its own line.

- The cost ledger no longer leaves a line open for the old writer, and no longer prices unknown usage as zero (dispatch 20261020-kp-k2a1-r3, round 3 on #2141, ADR-043).
  An append that fails halfway ends its own line at once when the disk has room, so `emit_provider_cost` cannot glue its
  next line onto it.
  A metered run whose `input_uncached`, `output` or a cache count its price charges is `null` gets `cost_usd: null` and
  `unavailable:usage_unknown`, and `aggregate_runs` lists it under `unavailable`. A count that is a real 0 stays a priced zero.

- Skills that ship to consumers (dispatch 20261021-skills-consumer-fixes, SEOcrawler #778 codex review): `vnx skills sync` ships from `skills/` only (`bin/vnx`, `cmd_skills_sync`), so the fixes live there.
  - Every shipped template and `@vnx-manager.md` tell the worker to write its report to `$VNX_DATA_DIR/unified_reports/`, never the repo-relative `.vnx-data/unified_reports/`; `tests/test_skills_consumer_contract.py` fails when a skill file names the relative form.
  - `dispatch_guard.sh` finds a CLI that speaks `vnx_status/1.0` in a consumer (`$REPO_ROOT/bin/vnx`, `$VNX_HOME/bin/vnx`, `~/.vnx-system/current/bin/vnx`, `VNX_BIN`, then PATH), fails closed with the CLI path and the schema it got when the schema differs, and runs `vnx status --json` with cwd and `VNX_PROJECT_ROOT` set to its own repo root.
  - `t0-orchestrator`: `template.md` drops the Manager-Block-via-smart-tap method for the single-entry door; `SKILL.md` points at `docs/core/DISPATCH_RULES.md` instead of restating the tmux-spawn lane; `intelligence.sh` finds `vnx_paths.py`, `bin/vnx` and the python scripts in the repo, `$VNX_HOME` or `~/.vnx-system/current` instead of cwd-relative `scripts/`.
  - Five skills no longer tell an agent to read user-global Claude memory; they point at ADRs and `claudedocs/`. `runtime-coordination-schema.md` lists v2 to v10 plus the v11 code step, the real dispatch and lease states, and `init_schema(state_dir)`.
  - `featureplan-kickoff` reads the tracks DB and never initializes or promotes from `FEATURE_PLAN.md`/`PR_QUEUE.md`; it and the `pm` alias now ship from `skills/` and are registered in `skills.yaml`, with `repo_local` naming the directories that are not shipped.

- `docs/core/DISPATCH_RULES.md` §6 no longer presents a claude tmux lane as live (dispatch 20261021-dispatch-rules-r3-land, PR #2147, cherry-picked from round 3 of #2146): the section is titled after the subscription session cap, says every `claude` dispatch runs on `claude_headless`, and keeps the 2026-09-18 removal as history. A test fails when this file or the shipped T0 skill names a claude tmux lane as current, and checks the skill's `DISPATCH_RULES §N` references.

- Changed: builder runs on the claude harness lanes (`claude`, `deepseek-harness`, `glm-harness`) write one cost-ledger line per attempt through `record_run` and no longer write the old `emit_provider_cost` event (ADR-043, K2a-2).
  - Tokens come from the session transcript, per assistant message deduplicated on `message.id`, with cache hits and the 5m/1h cache writes (`token_harvest.harvest_session_messages`). A count the transcript does not carry is `null` and the run `partial`, never 0. The R1b DeepSeek build now books its 7,218,944 cache hits.
  - DeepSeek runs are split into a peak and an off-peak segment from the message timestamps and priced per window. `claude` books as `subscription`, the two harness lanes as `metered`.
  - A refire is a new attempt: `cost_ledger.record_next_attempt` takes the attempt from the ledger under the append lock. A refused spawn writes a zero line with `unavailable:spawn_refused`; a run without a transcript writes `unknown:no_transcript` and `unavailable:no_transcript`.
  - The receipt carries `cost_ledger_run_ids` and takes `cost_usd`/`cost_source` from the ledger line. A ledger write that fails or is refused writes no receipt: `provider_dispatch` and the subprocess lane raise, the envelope records the proof-chain gap (OI-1179).
  - codex, kimi and the other providers, and gate seats, plan-gate seats and panel legs on the harness lanes, keep the old event (K2b, K3, K4a).
- Fixed: `claude-haiku-4-5-20251001` resolves to the registry key `haiku` through a new registry alias `haiku-4-5` (OI-2224 advice 1). An alias that names a key or a second model fails the registry load.

- T0 skill scripts: `dispatch_guard.sh` and `intelligence.sh` resolve the vnx CLI in one order (repo, `VNX_HOME`, `VNX_BIN`, the project-pin shim, `~/.vnx-system/current`, PATH), skip a non-executable candidate, and `intelligence.sh` pins `VNX_PROJECT_ROOT` when it resolves the state dirs (OI-2226).

- Changed: the cost ledger has one ordering lock, `events/provider_costs.lock`, taken first by every actor (ADR-043 §6, K2a-3a). `record_run`, `record_next_attempt` and `emit_provider_cost` take it exclusively, `aggregate_runs` shared; the ledger's own `flock` stays nested under it for writers of an older fabric version.
  - A failed ledger write no longer fails the call: the line goes to its run's spool file in `events/provider_costs.spool/` before the lock is released. A failed open, lock, boundary or append lands it in the spool; a failed fsync or close after a whole append lands it in the ledger and the spool. The returned record says where in `landed` (`ledger`, `spool` or `ledger+spool`).
  - The spool file name is `<readable>~<digest>.json`: the run id sanitised and cut at 128 characters, then the first 16 hex characters of the sha256 of the exact run id, so two run ids that sanitise alike never share a file. Readers take the run from the records, and a file holding a record of another run counts as unreadable.
  - A run with lines waiting in the spool queues behind them, so a correction never lands before the line it replaces. `record_next_attempt` counts attempts in the ledger and the spool: a refire while attempt 1 waits in the spool is attempt 2.
  - `CostLedgerWriteError` now means the spool failed too, or the lock file could not be taken: the outcome is unknown and the caller books no new attempt. The envelope, the subprocess lane and `provider_dispatch` write no receipt and do not retry on it.
  - `aggregate_runs` reads the ledger and counts the spool in one lock scope and changes nothing. `LedgerTotals` gains `spool_pending`, `spool_unreadable`, `torn_tail` and `incomplete`; a spool directory that cannot be listed is never an empty spool, and a torn last line counts as unreadable until a writer ends it.
  - `LedgerTotals.spool_conflicts` counts the spooled runs whose `run_id` meets another execution in the ledger (an f2bef053 writer that booked the attempt again); `incomplete` is set, the spooled amount is in no sum and `record_next_attempt` counts past both with a warning.
  - Not yet: the replay that brings the spool back into the ledger (K2a-3b) and the prelude tick and `cost-ledger` beacon (K2a-3c). Until then a spooled run is missing from every total and the aggregation reports the total as incomplete.

- voortgang mod 0.3.0 (`packages/vnx-mods/voortgang/`): the pane and the status line show the one VNX project the session is in (the first `.vnx-project-id` above the working directory) instead of every project on the machine, with the builders at work, what waits on a decision, per track `X van Y klaar` with a bar (or `geen leveringen gepland`), and the open PRs with their seats. The fleet (option `exclude`, data-root listing, per-project row split) is removed; a fleet view comes later as a separate product. It still reads metadata only and writes nothing. A PR counts as open when the state's PR queue or the index's `live_work.open_prs` lists it; when neither can be read the PRs of a track show as `#N ?` with `PR-status onbekend: <reason>` instead of `dicht`. The project id follows `bin/vnx` (all whitespace removed, `^[a-z][a-z0-9-]{1,31}$`).

- Review gates (OI-2242): a release PR now gets a review task that sends the reviewer into the tree. Release PR #2151 drew five `gate_execution_degenerate` codex runs on 2026-10-21, each one message with zero investigative actions, because the whole diff was in the prompt and the prompt asked for nothing beyond it. `changelog_fragments.is_release_pr` is the one rule: the PR changes `VERSION` and nothing outside `VERSION`, `CHANGELOG.md`, `changelog.d/` and `scripts/ci/changelog_legacy_prs.txt`. For such a PR `gate_prompt.build_review_prompt`, the path codex_gate, kimi_gate, deepseek_gate and glm_gate all take, adds `RELEASE_REVIEW_TASK` after the untrusted-diff block.
  The reviewer opens `VERSION` and the new section, runs `check_changelog_fragments.py` on the merge base and quotes its last line. It checks every claim of the intro in the tree: a named migration under `schemas/migrations/`, a re-pin claim against the previous version's code via `git show v<previous>:<path>`, and a named PR or OI number against the folded bullets. An unconfirmed claim is advisory and a contradicted claim blocks. The degenerate guard is unchanged and a normal PR's prompt is byte-identical to before.

- voortgang mod 0.3.1 (`packages/vnx-mods/voortgang/`): the status line shows how far the project is (the first two tracks of the pane with the 8-cell bar and `X/Y`, `: geen plan` or `: ?`, then `N bouwer(s)` when a builder runs) instead of PR, `te promoveren` and `uitkomsten` counters. `live_work.open_prs` is positive evidence only: a PR is `dicht` only when the readable PR queue of the state does not list it, else `#N ?`. An index entry without `dispatch_id` still counts as an open PR, a spec without `model` still links its track, and a track whose deliverables cannot be read keeps its PR and builder row. The Tracks order (pane and status line) is: a builder on the track first (most builders first), then an open PR, then a deliverable active in the last 7 days, then no activity; within a tier by priority, then id.

## [1.7.2] - 2026-10-19

This release carries migration 0034 (OI-2026), which gives the seven runtime
tables a `project_id`, every composite-key table its `ux_<table>_pid` index, and
stamps the store id from the nine writers. The state builder no longer migrates
the central store from a linked worktree (OI-2027). It also ships the review
round ledger with the B3 cap and a durable override, the per-project worktree
sweeper (OI-1629 B), and context-rotation plugin 1.0.1 in
`packages/claude-code-toolkit/`.

A store moves to `user_version` 34 at its first state build on 1.7.2; re-pinning
a project to 1.7.1 afterwards is safe because the 1.7.1 writers name their
columns. The content boundary at the door and the panel (#2063) and the nightly
content-boundary check (#2061) are not in this release and move to 1.7.3.

- Event streams (OI-2115, R6 fix-forward): every harness seat run now writes a live stream of its own, `events/plan-gate/<dispatch_id>.ndjson`, instead of the one `events/plan-gate.ndjson` all review, plan-gate and deliberation seats shared. Two seats that ran at once destroyed each other's stream: on 2026-10-16 deepseek_gate for #2076 booked `unavailable` because its archive held only its `provider_teardown_worktree` event. A run's teardown moves only its own stream into the unchanged archive path `events/archive/plan-gate/<dispatch_id>.ndjson`, extending an archive it already has instead of replacing it, so archives written before this change read as before. Seats can run in parallel and each keeps its own final answer. Terminal-pinned streams (`T{n}`) are unchanged. Docs: `docs/operations/EVENT_STREAMS.md`

- Event streams (OI-1869, OI-1895, OI-1930): every headless builder run now writes a live stream of its own, `events/ephemeral/<dispatch_id>.ndjson`, instead of the one `events/ephemeral.ndjson` every claude dispatch on the door's default lane shared. A builder that started, appended its first event or tore down archived and emptied the stream of a builder that was still running: on 2026-10-16 the fire logs of three parallel opus builders showed it three times (`dispatch boundary 20261016-cr-b1-r2 -> 20261016-xd-d1-shadow` and two more). `ephemeral` is now a per-run terminal like `plan-gate`, named once as `event_store.HEADLESS_LANE_TERMINAL` and used by the door's plan. A run's teardown moves only its own stream into the unchanged archive path `events/archive/ephemeral/<dispatch_id>.ndjson`, extending an archive it already has, so archives written before this change read as before. The old `events/ephemeral.ndjson` is never truncated by a run and is left for the operator to remove. The dashboard shows a per-run terminal as one lane over all its live runs, lists each run under `runs`, and streams one run with `?dispatch_id=`; `/api/events/stream` needs that id on a per-run terminal. Terminal-pinned streams (`T{n}`) are unchanged. Docs: `docs/operations/EVENT_STREAMS.md`

- T0 context guard, G1 fix-forward (OI-2117, three of the four OI-2119 items). `hook_coverage.coverage` counted registrations as covering where nothing decides, so the user-level guard skipped and no guard ran (codex and deepseek on #2075). A registration now covers only when it decides. (a) A direct entry covers only when the command runs the guard: the guard is the script of a `python3` call or stands executable in command position, also inside `if`/`then`, `( )`, a `;`/`&&`/`||` list or `sh -c`, not piped, backgrounded, negated or with stdout or stdin redirected. `test -f <guard>`, `grep` and `cat` no longer cover, nor does a command that mentions `VNX_HOOK_SCOPE` other than `=project` or runs `.`, `source` or `eval`. A leading variable resolves only from the environment or from what the command assigns before the call (`ROOT=$(git rev-parse --show-toplevel ...)` reads as the project dir); `$NOT_SET/...` no longer reads as the project dir. (b) A new command reader replaces `shlex.split`, so quoting survives: a `$HOME` in single quotes or behind a backslash stays literal and does not cover, nor does a bare one the shell would split. (c) A user-scope shim call runs the entry beside the shim it was started as and ignores an inherited `VNX_HOME`, which `bin/vnx` exports as the launching copy (a version dir or a dispatch worktree) and a project's settings `env` block can set; a project-scope call still follows `VNX_HOME`. (d) A shim registration covers only when the entry the shim would start exists (`shim_entry_file`). (f) A hook that is `async`, `asyncRewake` or `once`, carries an `if` condition or exec-form `args`, or names another `shell` does not cover. (g) A registration covers only where Claude Code recorded trust: measured in 2.1.301, `projects[<folder>].hasTrustDialogAccepted` in `$CLAUDE_CONFIG_DIR/.claude.json` or `~/.claude.json` (legacy `.config.json` first), for the session folder or a parent up to its git root. Untrusted reports `missing`; an unreadable trust file `unknown`. (h) Rule 1 finds the root by walking to the nearest `.git` instead of starting `git rev-parse` (10 s timeout in Python, none in the shim); the guard resolves its rotation state from that root; the shim's registry read ends itself after 2 s and the Python twin opens only a regular file. The fourth OI-2119 item (rule 1 reads the payload `cwd`) goes to G6. Docs: `docs/operations/T0_CONTEXT_ROTATION.md` § "Who is guarded".

- Context-rotation plugin, B1 of track `context-rotation-plugin` (`packages/claude-code-toolkit/context-rotation/`): the handover loop up to the manual clear. A PostToolUse hook on Write|Edit checks the session's handover (`## Running`, a step numbered 1 under `## Next steps`, at most `handover_max_chars`) and writes `ready-<session>.json` under a project lock with the sha256, the `claude` process identity (`CLAUDE_PID` plus its start time), the transcript position and the write's tool-use id, or tells the model exactly what is missing. A prompt (except `/clear`), any tool call other than Read, Glob, Grep or the handover write, work in the transcript after the position, or age past `handover_max_age_minutes` renames it to `outdated-<session>.json`; Stop also gives a valid handover written by another route its marker, and with a current marker tells the user once: "Handover saved. Type /clear, then any message, to continue in a fresh context." SessionStart after `/clear` injects the one current handover of the same process: claim by atomic rename, archive copy, a line in `injections.jsonl`, then the emit (a pointer to the archive copy above 9,000 characters), the claimed marker removed last; otherwise it lists the waiting handovers with the `resume` hint, and every start trims the archive to `archive_keep`. New `rotate` skill (`write`, `resume`); typing `/context-rotation:rotate` puts the handover path in context, `resume` lists pending and archived handovers and `resume <n>` injects one. No force or hard band and no tmux delivery yet (B2, C) The files under `.claude/rotation/` are inputs, not facts (codex and deepseek on #2080): every name and path a marker gives (session id, handover, archive copy, transcript position) must be the one its own session id and kind give, or the marker is reported once (stderr and `errors.log`), left on disk and never used. Markers, handovers, archive copies, transcripts and the plugin's own state files are read only as regular files (no symlink, FIFO or device), opened without blocking. A claim records the bytes it hands over (`claimed_sha256`); cleanup removes the original only while it still holds them, so a handover the origin writes again meanwhile stays with its own marker. Recovery after a crash reads the archive copy first and uses the original only while it still matches; an archive copy is never overwritten. `resume` names each entry's `claude` process (this one or another) and lists markers that cannot be resumed without a number.

- CI Profile A under pytest-xdist, D1 of track `ci-profile-a-xdist`: the shadow comes first, nothing switches yet. Profile A's selection (the `--ignore` list from `scripts/ci/test_exclusions.txt`, the `-k` quarantine, the per-test timeout) moved out of `vnx-ci.yml` into `scripts/ci/profile_a_pytest_args.sh`. It emits the vector the inline block emitted (golden test). With `VNX_CI_XDIST=1` it emits the two-phase shape: the entries of `scripts/ci/serial_tests.txt` in one process with xdist off, then the rest under `-n 4 --dist loadfile --no-loadscope-reorder --max-worker-restart=0`. The serial list starts empty and refuses (exit 2) an entry that does not match exactly one file or one collected nodeid. Profile A's setup steps moved into the composite action `.github/actions/profile-a-prelude`. `scripts/ci/profile_a_constraints.txt` pins pytest 9.1.1, pytest-xdist 3.8.0 and pytest-timeout 2.4.0. Every phase runs through `scripts/ci/profile_a_run_phase.sh` and writes a JUnit XML and a collection manifest from the same run (`scripts/ci/pytest_collection_manifest.py`, which also records collection-time skips and errors). Profile A uploads both with `if: always()`. A new job that is not required, `Profile A (xdist shadow)`, runs the two-phase shape on heavy runs and records its outcome in `xdist-shadow.json`. Its CheckRun always concludes success: every step is non-fatal, every step has its own timeout inside the job's, and the record and the upload always run. Profile A's timeout goes from 45 to 55 minutes for the shadow period.

- Review gates (OI-2114): kimi's weekly-limit 403 is now read as an exhausted lane. `_LANE_EXHAUSTED_MARKERS` gains kimi's reset sentence (`your quota will reset when the current 7-day window ends`), measured on `pr-2074-kimi_gate.json`. Before, that record read as `no_response`, so it held the seat and kimi was dispatched again on every request for that head. Now the takeover walk passes kimi over to deepseek while the record is fresh, and the write path books such a failure as `lane_exhausted`. The recency gate and the reachability rules are unchanged

- T0 rotation, wrap proof (G5a; operator decisions 2026-10-16 a-c): `/build-log wrap` leaves proof that the spawner checks. `t0_rotation_state.py wrap-start` (first step of the wrap) records the start time and the month bucket's headings; `wrap-finish` (last step) writes a `wrap:` block into the handoff frontmatter with the system clock, counting only this slug's new nugget headings. `scripts/t0_rotate_spawn.sh` refuses with the new exit 6 ("run /build-log wrap") unless that block binds to this wrap: `wrapped_at` fresh, the daily log carries `handoff: true` and was written inside `[started_at, wrapped_at + 60 s]`, every nugget heading is in the bucket. Emergency path `--skip-wrap-reason "<text>" [--transcript PATH]`: skips the proof and the age check (never the first-step check), needs the predecessor's transcript as readable JSONL, books a `t0_context_rotation_wrap_skipped` receipt before any window opens (a refusing ledger refuses the spawn), and puts a catch-up wrap at the head of the prompt. New helpers `transcript-digest` (post-boundary user/assistant text in a 200,000-byte budget that the newest entries win, printed oldest first) and `skips [--since DAYS]`. One wrap-start proves one wrap: a passing `wrap-finish` writes `consumed_at` into the start record, so a second finish without a new `wrap-start` refuses ("run wrap-start first") and cannot stamp a fresh `wrapped_at` on an older wrap; a failed finish leaves the start usable. A relative `--transcript` (or marker path) is made absolute against the caller's folder before the check, the receipt and the prompt, because the successor starts in the T0 folder. The block names the start record's `start_id`, and `wrap-finish` checks the block before it writes, consumes the start record, then publishes the block; the spawner accepts a block only when the record with that `start_id` was consumed by exactly that block (check `wrap_start_binding`), so a crash between the two writes leaves a failed wrap, never a reusable start beside a valid proof (OI-2116). An earlier `wrap:` entry is removed from its key line up to the next top-level key, blank lines and comments included, and the rewritten frontmatter is parsed again strictly: a result that would not parse is refused (exit 3) and the handoff stays as it was. The spawner reads the handoff frontmatter strictly too; the daily log stays lenient. Behaviour change: a handoff without a `wrap:` block no longer spawns, so the build-log skill must run the two helper steps (G7a) before this reaches a T0

- T0 context guard, G1 of track `t0-guard-gebruikersniveau` (OI-2071, OI-2072, part of OI-2056). `scripts/hooks/vnx_hook_shim.sh` (POSIX sh) starts a hook entry so that a missing file never exits 2, which Claude Code treats as blocking: it exits 0 outside a VNX project with a T0, names a missing entry with exit 1, and maps an entry's exit 2 to 1. The fabric repo runs its own code only when its real path is the `vnx-dev` path in the user-owned registry `~/.vnx/projects.json`, never on the repo's own `.vnx-project-id` alone; every other root runs `current`. `scripts/lib/hook_coverage.py` renders the inline registered command (it tests the shim first and maps every non-zero exit to 1, so a script that vanishes between test and start exits 1 under bash and under dash; the VNX home is an allowlist of documented forms, so a command substitution is refused) and holds the one coverage function: does a project's own settings run the guard for this event and PreToolUse tool. A shim registration covers only when its shim exists and the shim accepts its arguments at project scope. The guard now needs all three rules: a project with a `.vnx-project-id` and a T0 folder, the T0 by convention, and an attended session (not `CLAUDE_CODE_ENTRYPOINT=sdk-*`, not `VNX_WORKER_SCOPED=1`). A `claude -p` child of the T0 is no longer rotation-pressured. With `VNX_HOOK_SCOPE=user` it stays silent where the project covers the guard itself. After a compaction the measurement returns `compactMetadata.postTokens`, a lower bound, instead of the size before the compaction. No settings or template changes yet: `vnx guard install` (G2) registers the command

- Review takeover chain is now `codex_gate,kimi_gate,deepseek_gate,glm_gate` (operator decision 2026-10-04, OI-1984): deepseek is the API-credit fallback after kimi and glm is the last link. Both API-credit gates stay behind both subscription gates. `deepseek_gate` is a member of the `Gate` enum with a `closure_verifier` handler, so a deepseek takeover pass now signs the seat it took over at the merge door and counts in the `vnx-gate/review` summary (before, the record was skipped as a non-peer and the seat stayed unsigned). `GATES_OUTSIDE_ENUM` is empty. A consumer project follows after a release and a repin, or through a per-project value of `VNX_REVIEW_GATE_TAKEOVER_CHAIN`

- Review seats, R5b fix-forward (OI-2113, OI-2110): the rejection scan reads every in-scope record of a review peer on the head, not only the one that fills a seat. An outage in `<n>-<gate>-contract.json` hid a FAIL in `pr-<n>-<gate>.json` beside it, and the merge door, `vnx-gate/review` and `vnx pr-ready` said GO (codex reproduced it on `46b52973`). `closure_verifier._find_gate_results` is the one reader: its first record may fill a seat, and any of them rejects the head. The governance enforcer's `<gate>_required` checks credit no seat while a review peer rejects the head. The test-run marker is read with `gate_status.is_test_run_record` everywhere (`review_seat_evaluation`, `pr_readiness.observed_gates_for_pr`), so a request with `test_run: "TRUE"` no longer narrows the seats from two to one. The canonical T0 role (`.claude/terminals/T0/role-orchestrator.md`) states the R5b seat rule instead of the pre-R5a "unless codex and kimi are both unavailable"; a consumer gets it through `vnx role sync` after a release.

- Review seats (OI-2058, R5a): one seat resolver, `scripts/lib/review_seats.py`, reads `VNX_DEFAULT_REVIEW_STACK` fresh and refuses an unknown or retired gate by name. Every seat gets its own reviewer: with codex alone out, kimi reads codex's seat and deepseek reads kimi's; with both out, deepseek and glm read them. A seat with no distinct reviewer left is booked unfillable by name (`chain_exhausted`, `reason=no_distinct_reviewer`). Every seat's request record carries `seat` and `review_seats`. A reviewer that runs out during `request-and-execute` hands its seat on in the same call, forward along the chain from that reviewer. A decided verdict, PASS or FAIL, is never handed on, so a substitute's FAIL can no longer be superseded by the reviewer it replaced. Verification credits each entry to the seat its request states, before any takeover annotation. Behaviour change: an API-credit gate now also reads when only one subscription reviewer is out

- Merge seats (OI-2058, OI-2064, OI-2107, R5b): every evaluator counts review seats with one evaluation, `scripts/lib/review_seat_evaluation.py`. The merge door (`check_review_gate_for_merge`), `vnx-gate/review`, `vnx pr-ready` and track closure now require every seat of the project's review stack, each filled by a distinct reviewer on the PR head; the declared gate no longer decides the seats. The seats are the config stack a request on the head recorded (that one wins over a fresh resolution, and the merge record says so), else a fresh `resolve_review_seats()`; an explicit single-seat request never narrows them. A record answers for the seat its own request states, else `takeover_path[0]`, else its own gate; `takeover_from` is never read, and a successor that only walked past a seat no longer fills it (also in the governance enforcer, OI-2107). The OI-1719 booking and the OI-1624/OI-1642 peer route only take a reviewer no other seat counts. Any review peer's rejection on the head blocks, whatever the other seats say: a FAIL verdict or a blocking finding in its JSON, or in its report on disk whatever the JSON says (the door's invariant 7, read with the door's own reader for every review peer, the declared gate and reviewers outside the seats included). A record that only lacks its report is no rejection and fills no seat. `vnx-gate/review` is `failure` then, `action_required` while a seat has not spoken, `success` only when every seat is filled; `vnx pr-ready` gives every rejecting reviewer its own NO-GO line, also when another reviewer filled the seat its gate name stands for, so it says READY exactly when the door says GO. A switched-off seat without a successor is refused with the switch and both ways out. The `pr_merged` receipt carries `review_seats`; closure applies the seat rule to a merge with it, checks the contract hash and the report of the exact record the door's route accepted for each seat (`hash_<seat>`, `report_<seat>`; `vnx-gate/review` and `vnx pr-ready` read that same record), and keeps older merges under the rule they were merged under. `gate_enforcement_verify` resolves its default seats fresh (or from the report's own seat list) instead of the import-time `DEFAULT_REVIEW_STACK`. Behaviour change: a head with only a codex PASS no longer merges in vnx-dev; consumers get this after a release with R5a and R5b. ADR-037 is amended.

- Docs: `docs/core/LOCKS_AND_RELEASES.md` lists every lock from dispatch to merge with its releases, who may use them, the trail and the test. New accounts of the merge door and of what a green CI does not prove (`DISPATCH_RULES.md` 2.2 and 2.3), `live_work` (`STATE_FABRIC.md`), `takeover_from` (`45_HEADLESS_REVIEW_EVIDENCE_CONTRACT.md`), the plan-gate evidence file and the reconciler's automatic closing of deliverables (`HORIZON_LIFECYCLE.md`), and `open-outcomes --recount` (`11_RECEIPT_FORMAT.md`, `DISPATCH_RULES.md` section 13)

- T0 context guard, G2 of track `t0-guard-gebruikersniveau` (part of OI-2056, OI-2117 item e). `vnx guard install [--dry-run] [--config-dir DIR]...` registers the guard once per Claude Code account: it discovers `~/.claude` and every `~/.claude-*` that holds a `settings.json` or `.claude.json`, and writes three user-scope entries (UserPromptSubmit, PreToolUse with matcher `Bash|Skill|SlashCommand`, Stop) through `registered_command`, `"timeout": 10` seconds. Each file gets a fresh read, a JSON check that keeps every top-level key, a check of the whole `hooks` tree (a malformed node refuses the install and is named by its path, such as `hooks.Stop[0].hooks is not a list`), a timestamped backup, a `mkstemp` scratch file and a size, mtime and hash comparison just before the replace (a change starts over once and removes the abandoned attempt's backup, a second change refuses). The replace is an atomic exchange (`renamex_np` with `RENAME_SWAP` on macOS, `renameat2` with `RENAME_EXCHANGE` on Linux, through `ctypes`, new `scripts/lib/atomic_swap.py`): the file that sat at `settings.json` at that instant is compared with the backup and, when it differs, kept as `settings.json.vnx-displaced-<id>` and reported with exit 1, so a write that landed before the replace is never lost. A platform or filesystem without the exchange refuses the install instead of falling back to `os.replace`. It prints what is and is not covered, is idempotent, and refuses when the shim or the guard is missing in `~/.vnx-system/current`. `vnx guard check` (also a `vnx doctor` check) reports per account the three entries, the shim and the guard in `current` and the root the registry trusts to run repo code, and per project with a T0 folder, per event and PreToolUse tool, whether the guard runs from the `user` level, the `project` (with its form), is `missing` or `unknown`; its inventory is the registry plus `--scan-root` (depth 3). Coverage is computed per account with that account's environment (`CLAUDE_CONFIG_DIR` unset for `~/.claude`, set to the folder for every other account), because Claude Code records project trust per account. An account whose settings cannot be read or hold a malformed `hooks` tree is `unknown` with the reason. `templates/settings_vnx_keys.json.tmpl` and `templates/init/{default,minimal}/settings.json.j2` no longer register the guard and carry every `timeout` in seconds. They start `pretooluse_t0_no_subagents.py` without the shim, whose cwd rule would undo the hook's own transcript-based T0 detection when a T0's cwd drifts out of the project: an inline command rendered by new `scripts/lib/t0_subagent_block.py` tests that the entry exists, runs it with `python3` and maps every non-zero exit to 1 (a missing entry exits 1 with its path, never 2). The VNX home that `vnx regen-settings` and `vnx init` substitute into a hook command is rendered through that function at render time, so a path outside the `registered_command` allowlist (a quote, a `$(`) is refused. A release check asserts the shim and the new modules are tracked and shipped. `guard` is a command of the starter and operator tiers in `vnx_mode.py`, like the other commands that write the operator's own configuration. An event that holds more than one entry of ours is normalised to exactly one by the install (a correct entry no longer hides an older registration such as `"timeout": 5000`), and `vnx guard check` reports it as `duplicate` by event name and exits 1. `check` also applies the shim's rule 1 (`hook_coverage.t0_project_root`): a registered project with a T0 folder but no `.vnx-project-id`, no `.git` entry at or above it, or a place under `.vnx-data/worktrees/` is `not guarded` with the reason, because the installed command exits there without running the guard. `vnx init` and `vnx regen-settings` print one line when no account on the machine has the user-level guard, and `vnx doctor` warns with `vnx guard install` in that case; nothing is installed automatically. The template timeouts (5, 3 and 8 seconds) were measured against the slowest run of every hook they register (10 runs each on a fixture project, at most 0.354 s) and all sit above three times that maximum.

- CHANGELOG fragments, F1 of track `changelog-fragmenten` (OI-2118): a PR no longer edits `CHANGELOG.md`. It adds `changelog.d/<dispatch-id>.md` with its bullets, so two open PRs no longer conflict on the line under `## [Unreleased]`. The release PR folds them in with `scripts/release/assemble_changelog.py --version X.Y.Z [--check]`: a new `## [X.Y.Z] - <date>` heading under an empty `## [Unreleased]`, the bullets already there first, then the fragments in the order they landed on the first-parent history of `--rev` (default `origin/main`), and the fragments deleted, all or nothing. Every fragment must be in `--rev` with the same bytes, and every line under `## [Unreleased]` must be a bullet, a continuation or blank. One run at a time: a second run refuses and names its lock in the git dir, so it can never restore the old file over the first run's result. A failed write or delete restores the original files; after a killed run the assembler refuses the dirty tree and names `git checkout -- CHANGELOG.md changelog.d`. A new `Changelog fragments` step in the required `Lint Patterns` job (pull requests only) reads both trees from git objects, decodes every file as strict UTF-8 (an invalid byte is refused by file, line and byte offset), and refuses any change to `CHANGELOG.md` outside a release PR (the file stays byte-identical to the merge base, every released section included, and the message names the changed section), a `## [...]` heading that appears twice, an invalid or symlinked fragment, a non-fragment file in `changelog.d/`, and a release PR that differs from the fold of its merge base beyond a prose intro, leaves a fragment behind, adds one, or carries a heading that is not a version `--version` accepts or not `VERSION`. Every added heading occurrence makes a release PR, also a second heading for a version that already exists. PRs that were open with a `CHANGELOG.md` bullet are listed with a hash in `scripts/ci/changelog_legacy_prs.txt` and pass while those lines are unchanged and nothing else in `CHANGELOG.md` changes. A PR that changes only fragments runs the light CI profile. `dispatch_create.sh` and the `vnx-manager` skill tell workers to write the fragment. Rule: `changelog.d/README.md`

- Context-rotation plugin, B1 round 6 (PR #2080; codex and deepseek on `a14f38b2`, OI-2126, OI-2136, OI-2138, OI-2083): no handover is deleted under its writer, and no text from disk reaches the model. After an injection the cleanup first renames the original handover into the archive under a new name, so a write to the original path from then on makes a new file that the cleanup never touches. It removes the moved file only when its inode, size, modification time and sha256 are the ones the claim recorded (`claimed_file`, `claimed_sha256`). Otherwise it puts the file back, listed through its own ready-marker or a new outdated one, or leaves it in the archive with a `kept` line when a newer file took the path meanwhile. Values from a marker or from `injections.jsonl` are used only in the form the plugin writes them: times in its own format, an outdated reason from a closed list (a reason no longer names a tool from the transcript), paths as the plugin builds them, marker file names of plain file-name characters. Anything else rejects the marker or drops the line. The transcript cursor of a ready-marker sits just after the transcript entry of the handover write itself, so work that lands while the hook runs lies after it. `resume` shows a stable id per entry (`[id ready-<session>]`, `[id <archive name>]`) and accepts `resume <id>`. `resume <number>` picks the entry shown under that number in the session's last list, and is refused with the current list when that entry is gone or no list was shown yet; a number of any length gets the normal refusal, and an unknown action gets the usage line. Every transition (ready, outdated, claimed, injected, kept) has its line in `injections.jsonl` before the marker changes, and the marker on disk stays the truth. `injections.jsonl` is kept to 16 lines per `archive_keep` (10 in this round; C raised it for the steps of the automatic `/clear`), `errors.log` lines are appended under a lock of their own so failures at the same moment lose none, a whole-file read stops at 1 MiB (a larger file is invalid), and the list shows claims, then ready, then outdated handovers, each newest first.
- Context-rotation plugin, corrections to the B1 entry under `## [Unreleased]` (PR #2080), which stays as it was when the fragment rule landed. In that line "No force or hard band and no tmux delivery yet (B2, C)" has no closing period: it runs on into "The files under `.claude/rotation/` are inputs, not facts". Read a period after "(B2, C)": those are two sentences. Every name and path a marker gives must be the one its own session id and kind give: its file name, the handover, the archive copy, and a transcript position in the session's own `<session id>.jsonl`. Since round 7 a claim moves the marker to `claimed-<origin>-<generation>.json` by a hard link, not by an atomic rename, and the cleanup never removes the original handover: the original becomes the archive copy when it still holds the claimed bytes.
- Context-rotation plugin, B1 round 7 (PR #2080; codex and deepseek on `e8fba0d8`): every handover generation has its own identity, and nothing in `.claude/rotation` is overwritten or deleted under its writer. A ready-marker gets a random `generation` id when it is written, which stays with it through outdated and claimed and is in every `ready`, `outdated`, `claimed` and `injected` line of `injections.jsonl`. A claim is named `claimed-<origin>-<generation>.json`, so a second claim of the same session no longer replaces an unfinished one, and the archive trim keeps the copy each claim names. A generation is claimed at most once: a claim that stopped between its link and its unlink is completed by the next attempt, and anything else at the claim name refuses the claim. Markers move by a hard link to the new name, which fails when that name exists, so a move never replaces a file; a file at the target refuses the move and is logged. The original handover moves into the archive before the emit, under a fresh name checked under the project lock that every archive writer now holds. The cleanup no longer removes that file: when it still holds the claimed bytes it becomes the claim's archive copy and the separately written copy goes, otherwise it goes back to the handover path and keeps its archive name with a `kept` line. A write through a descriptor opened before the move is kept. Only the age-based trim removes archive copies, and never one a marker names. A `resume` id is `<kind>-<session>-<generation>` (`[id ready-<session>-<generation>]` replaces round 6's `[id ready-<session>]`), so a number or id saved from a list never picks a later handover of the same session; it is refused with the current list. An injected handover says whether it still holds the text its marker was checked against: after `/clear` it always does, and a handover the user picks is injected as the file is now, with that sentence. On macOS an open that creates a lock file or `.gitignore` can fail with ENOENT while another hook process creates the same name; it is tried again, at most 5 times.

- CI Profile A under pytest-xdist, D2 of track `ci-profile-a-xdist` (PR #2087): the parity check. `scripts/ci/check_xdist_parity.py compare` compares the serial Profile A (reference) with the shadow's two phases (candidate) and writes `xdist-parity-verdict.json` plus a step summary. A test is the `(classname, name)` pair JUnit writes, derived from each manifest nodeid with the pinned pytest's own `mangle_test_address` and `bin_xml_escape`. A call failure plus a teardown error is one execution. A file skipped or erroring at collection is keyed by its file, and a collection error that arrives once per xdist worker is one outcome. The script never reads pytest's summary line. Skip reasons are kept for triage but not compared: on run 38951854817, 6 of 51 differed between the shapes only in a temporary path.
  Exit codes. 0 is parity: the candidate executed exactly the reference set with the same outcome classes. 1 lists the differences in seven classes: not run in the candidate, extra in the candidate, run more than once, outcome class (pass, fail, xfail), skip set, collection outcome, and phases that do not partition the reference selection. 2 is invalid evidence: a missing, empty or unparsable file, a key collision, a record outside the manifest, workers that disagree, tool versions off `scripts/ci/profile_a_constraints.txt`, constraints that are not UTF-8. A light run records `not_applicable` and exits 0.
  Every input proves the run and the attempt it is counted for, with its own stamp: the manifests and `result-<phase>.json` (Profile A's pytest exit and wall time, written by `profile_a_run_phase.sh`) as `run_id`/`run_attempt` fields, each JUnit XML as the testsuite properties `vnx_run_id`/`vnx_run_attempt`, each args emission as `args/run.json`, the shadow record as fields. One rule decides, `stale_evidence_reason`. A stale input is exit 2 (`stale_evidence`) and nothing is read from it: a stale phase is not compared, a stale shadow record gives no profile and no wall time, stale args give no fingerprint.
  The verdict records `step_failures` next to the blocks they come from (`steps`, `reference`, `shadow`): every parity-job step that is not success, Profile A's job result (an audit step can fail it after its evidence was uploaded green), its phase result and its pytest exit, the shadow's record, verdict and job result. Step failures never change the exit. The verdict also carries a `selection_fingerprint`: the sha256 of the emitted xdist-path vectors with the workspace root stripped, plus the constraints file. `check_xdist_parity.py fingerprint` prints it for an args directory.
  Every field of a collection outcome (`nodeid`, `outcome`, `reason`, `reports`) and every nodeid is checked where the manifest is read. A manifest that fails is `malformed_manifest` and its phase is not compared. Bad input exits 2 with a reason on stderr, never a traceback: a `--step` or `--upstream` that is not `NAME=VALUE`, a verdict or a summary that cannot be written.
  A new job that is not required, `xdist parity`, needs Profile A and the shadow with `if: !cancelled()`, downloads both artifacts, runs the check and uploads the verdict. It never fails a check, by the shadow job's rules: every step non-fatal and bounded inside the job's timeout, the record and the upload with `if: always()`. When the script wrote no verdict, the record step writes an exit-2 verdict (`no_verdict`). A verdict that exists is kept as the script wrote it, also when the parity step failed after writing it (OI-2145), so no reader uses the verdict for a decision yet.
  The cutover history (the window of verdicts that decides D4's switch) moved out of D2 into its own deliverable, D2b.

- Context-rotation plugin, B1 fix-forward (OI-2144; codex on the last head of #2080): the archive copy a claim names never changes, and `resume` claims only the generation it found. The copy written from the claimed bytes stays the claim's archive copy. The injected context, the pointer above 9,000 characters, the `injected` line and the recovery after a crash all name it. The original handover no longer becomes that copy and the copy is no longer removed. The cleanup moves the original to `archive/<time>-<origin>.original`, so a write through a descriptor opened before the claim lands there and changes nothing a claim or a delivery reads. `resume` does not list that file. The trim keeps the newest `archive_keep` of them, counted apart from the archive copies. A moved file that turns out to be another generation gets an archive copy name and is kept as in round 7.
  For a ready or outdated entry, `resume <number>` and `resume <id>` pass the generation of the entry they found to the claim. The claim compares it under the project lock before anything is written. When the origin marked a new generation ready in between, nothing is claimed and the refusal names both generations. The claim after `/clear` already looked up and claimed under one hold of the project lock, and is unchanged.

- Context-rotation plugin, B2: the force and hard bands. At force (65% of `window_tokens` by default) every prompt asks the model to write the handover at its path now, and the user gets one line per crossing that names the path; a Stop without a current handover is held back once per crossing with "write the handover at <path> with the rotate skill as your last action, then stop". PreToolUse denies nothing at force, unless the new setting `force_deny_tools` (tool names or fnmatch patterns, empty by default) names the tool. At hard (75%) PreToolUse allows only Read, Glob, Grep, Skill, AskUserQuestion, TodoWrite and a Write or Edit of the session's own handover, and denies everything else with the reason and the path. A denial is `permissionDecision: deny` and a held-back Stop is `decision: block`, both JSON with exit 0.
  A crossing is a move into force or hard from a lower band; falling back after a compaction and rising again is a new one. Every PreToolUse records the band, also for a call the hard band allows, so a fall seen only at a Read still makes the next rise a crossing. No Stop is held back while `stop_hook_active` is set. An unknown size, a hook with `agent_id` and plan mode (`permission_mode: plan`) never deny or hold back. After `hard_max_denials` denials without a handover write (default 3) the session only informs and the user is told once; a handover write resets the count. The user lifts both bands for the session by typing `/context-rotation:rotate release`: the `UserPromptExpansion` hook records it, and a release the model starts through the Skill tool changes nothing. A symlink at the handover path opens no write at hard and resets no denials; a handover path that cannot be checked lets the call through, resets no denials and is logged.
  The state per session lives in `.claude/rotation/state/<session>.enforcement.json` behind B1's storage gate, every change under the project lock with its line in `.claude/rotation/enforcement.jsonl` written first. A state file that is not exactly what the plugin writes counts as a fresh one, so no text from it reaches the model or the user. A failure of that state (OSError, lock timeout) is logged and the call goes ahead: it never denies or holds back. A read of the state that fails (an I/O error at its lstat, open or read) is such a failure and never a state to replace: nothing is written on it, so a recorded release survives it.

- Context-rotation plugin, B2 fix-forward (OI-2151; deepseek on #2090): Claude's file tools can no longer write the plugin's state directory. Below the hard band a model could Write `.claude/rotation/state/<session>.enforcement.json` with `inform_only: "release"`, and every band message gives it the session id through the handover path, so it could release its own guard before the hard band. Now PreToolUse denies a Write, Edit, MultiEdit or NotebookEdit that lands in the project's `.claude/rotation/`, other than of the session's own handover, in every band: also below warn, in plan mode, after a release, at an unknown size and inside a subagent (a subagent hook now runs this one check and nothing else). The reason names that directory and says the plugin keeps it. Such a denial never counts toward `hard_max_denials`, so it cannot be used to reach the cap. A write at `.claude/rotation` itself or at the `.claude` above it is denied the same way, with or without those directories: a regular file there made the storage gate refuse the project, after which no hook enforced anything.
  Where a write lands is judged by the real directories against the rotation directory B1's storage gate verifies, in two forms of the path: as the kernel opens it, a symlink followed before a `..` after it (`foo/../state/...` with `foo` a symlink into the plugin's directory is caught), and with `..` collapsed by name first, as a caller that normalises the path does. A `..`, a symlink elsewhere that leads in, a symlink inside that leads out, a `~` path and a name in another case (the macOS default file system) all count, also before the directory exists. The session's own handover is recognised the same way: a `~` path and, on a case-insensitive file system, a case variant of the existing handover are that handover, compared by file identity where it exists. A symlinked rotation directory is never followed: the storage gate refuses it and nothing is decided on it. A check that fails (an `OSError`) lets the call through and is logged.
  The rotate skill and the new plugin README say what the guard does not do: a shell command runs with the user's rights and can change any file there, and the plugin parses no shell command. The skill no longer claims that no tool call can change anything. This change left the plugin's settings files writable by the file tools; the fix-forward for OI-2156 closes that.

- Context-rotation plugin, fix-forward on #2092 (OI-2156; deepseek and codex on #2092): the two settings files fall under the same write rule as `.claude/rotation/`. Every hook reads `<project>/.claude/context-rotation.json`, else `~/.claude/context-rotation.json`, and does nothing when that file says `"enabled": false`. Below the hard band a model could Write it and so switch the plugin off. Now PreToolUse denies a Write, Edit, MultiEdit or NotebookEdit that lands on either settings file, existing or not, in every band: also below warn, in plan mode, after a release, at an unknown size and inside a subagent. The reason names the settings file and says the user changes it themselves. Such a denial never counts toward `hard_max_denials`. The guard takes both paths from `context_budget.settings_paths`, through which `load_settings` now reads them too, so the guard and the reader never name different files. Where the write lands is judged as for the rotation directory: a `..`, a symlink, a `~` path and a name in another case all count. A file next to a settings file, such as `.claude/settings.json` or `.claude/context-rotation.json.bak`, stays writable.
  One way of reading the path that cannot be judged no longer hides a denial. Each spelling of the path (as the kernel opens it, with `..` collapsed by name, and through its real directories) is judged on its own against each kept place on its own, and one that lands on a kept place denies the call. Before, the first `OSError` ended the whole check: `<project>/<a 300-character name>/../.claude/rotation/state/<session>.enforcement.json` failed with `ENAMETOOLONG` on its raw spelling and was let through, after which a writer that collapses `..` first could write `inform_only: "release"`. Only when no spelling lands on a kept place and one could not be judged is that failure logged and the call let through. The session's own handover stays writable when every spelling that can be judged is that handover. The step in `errors.log` is now called `check write of the plugin's own files`.
  The plugin README, the rotate skill and the #2092 entry now say where the guard stops. It stops a direct write by Claude's file tools to the plugin's own files, and it is no lock against a model that sets out to switch the plugin off: a shell command or a script runs with the user's rights and can change those files, a change to Claude Code's own settings can remove the plugin's hooks, and the tools of an MCP server are not looked at. Typing `/context-rotation:rotate release` is the only release the plugin itself offers, and the user can always also edit the settings file.

- Context-rotation plugin, C: the automatic /clear in tmux. At Stop with a valid, current handover, inside tmux (`TMUX` and `TMUX_PANE`, the pane the handover was written in) and with `auto_clear` on, the plugin starts `scripts/tmux_clear.sh` detached through `tmux run-shell -b` and tells the user that /clear is typed for them once the session is idle. A running entry in `background_tasks` (or a Stop without that list) starts nothing: the user is told what still runs, by type and count only. A job that cannot be started falls back to the manual message.
  The job waits until the Stop hook has exited and the pane shows two equal captures, reads the ready-marker again (it must still be current, for that generation, pane and `claude` process), then checks the pane: alive, in no mode, not synchronized, the input box empty (`❯` and a no-break space with nothing or only dim text after it, read with `capture-pane -e`), no spinner above it, and the session's `claude` (CLAUDE_PID and its start time) in the process tree of `#{pane_pid}`, owning the input of the pane's terminal: `#{pane_tty}` is its controlling terminal, its process group is that terminal's foreground group, and it is not stopped (`ps -o pid=,pgid=,tpgid=,stat=,tty=`, the same columns in BSD ps and procps). A `claude` that is stopped (Ctrl-Z, SIGSTOP) or runs in a background group while another program reads the keys gets no key (`process_stopped`, `process_background`; a ps that cannot tell is `process_unreadable`). It types `/clear` with `send-keys -l` and reads it back until the input line holds exactly `/clear`, writes the step's journal line and record, reads the ready-marker again (that write can wait for the project lock, and a prompt in that wait makes the handover outdated or replaces it; then no Enter goes), then takes a fresh look: the session's `claude` still runs below the pane and owns its terminal's input, then one tmux call (`display-message ; capture-pane`) gives the pane's state, its terminal, its cursor and its screen at one moment. Enter goes as a separate call right after that look, with no lock wait, sleep or other read in between, and only while the input line still reads exactly `/clear` with no spinner above it. It waits up to `ack_timeout_seconds` (at most 120) for the fresh session's `delivered` line, then types and sends `Continue from the handover in your context, step 1.` the same way, without the marker read: the fresh session has taken the handover by then. Those two fixed lines are the only text it types. It never removes text it cannot tell is its own: only when a look shows exactly the line it typed, with the cursor right after it, does it send one BSpace per character. In any other state (more text, other text, the cursor elsewhere, a line that cannot be read) it sends no key, and the status line and the repeat at the next prompt say the text it typed was left in the input line (`left_in_line` in the record and in the `clear_aborted` line). The default `prompt_pattern` is now `^❯` plus a no-break space (A0 rule 6), which a prompt in the history above does not match.
  Any failed check, and any read that fails (marker, handover, transcript, lock, pane, process table, journal), stops the job: nothing more is typed, `tmux display-message` shows what to do, and the next prompt of that `claude` process repeats it once. The run-shell line holds only the shell-quoted script path and ids checked against fixed patterns, every `#` doubled, its output to /dev/null and `|| true`, because tmux shows a failing or printing run-shell job in view mode over the pane; the project path travels in a job file in `~/.claude/context-rotation/jobs/` (mode 0700). One job per pane: `clear-<n>.lock` under `.claude/rotation/`, stale after 300 s, its state in `clear-<n>.json`, and every step journaled first in `injections.jsonl` (`clear_started`, `clear_entered`, `clear_acknowledged`, `clear_continued`, `clear_aborted`, `clear_told`, `clear_lock_broken`). The first automatic clear for a user says, in the fresh session's first line, how to turn `auto_clear` off. B1's delivery now writes a `delivered` line to `injections.jsonl` once the emit of a claimed handover returned (never raising: a failure to write it is logged); `injected` stays the line written before the emit. A handover whose emit fails leaves its claim and no `delivered` line, so the job types no continuation and shows the message to resume by hand. `tests/live/smoke_tmux.sh` is the live smoke a person runs in a private tmux server: it starts `claude --plugin-dir`, captures the pane and checks the evidence of four runs (the automatic clear, text in the input line, working on after a stopped clear, a running background shell); `capture` also saves where tmux has the cursor, and `check input` checks that Claude Code keeps it right after the typed text; it types nothing itself.

- Context-rotation plugin D (track context-rotation-plugin): the plugin README now opens with the problem, the two-command install (and the project-scope variant), the loop with the real file names, the settings table with every key and default, the automatic clear and its choices, the limits and the files the plugin writes. Limits named there: tmux for the automatic clear, a 1M-window user on defaults rotates early, Windows unsupported, `/resume` restores the old session, and a fresh session that starts close to the force band rotates again after one turn (OI-2158). The plugin gets its own `CHANGELOG.md` with the `0.1.0` entry, and `tests/live/install_test.sh` installs the plugin from an exported marketplace in a throwaway git repo with `--scope project`; it is run by hand, never by a worker. Two docstrings in `hooks/context_guard.py` now say the subagent check also covers the two settings files, as `lib/band_guard.py` does. No code changes.

- Release 1.7.2 preparation (OI-2141): `vnx release publish --set-current` now prints the `assemble_changelog.py` steps (`--check`, then the real run) and the pin consequence as intro prose, instead of telling the operator to add bullets by hand that the fragment check refuses.
  The assembler also deletes `scripts/ci/changelog_legacy_prs.txt` in the first release PR (restoring it if a write fails), and the check refuses a release PR that keeps it.

- Context-rotation plugin, the open B1 advisories before the export (OI-2083, OI-2126, OI-2136, OI-2138): each was reproduced on `main` first. Two were still there and are fixed. The others were already fixed by B1 rounds 6 and 7 (#2080) and its fix-forward (#2091), and are unchanged.
  A claimed marker is removed only once its `delivered` line is in `injections.jsonl`. Before, a `delivered` line that could not be written (a full disk, a project lock held too long) was logged and the claim was removed anyway, so the claim ended without a line. Now the claim stays, and `resume` lists it as a claim that did not finish.
  `handover_max_chars` accepts at most 262,144: a quarter of the 1 MiB read limit, because UTF-8 takes at most four bytes per character. A higher value falls back to the default with a warning. Before, a higher limit let the contract accept a handover that the plugin then refused to read.
  The B1 fragment said `injections.jsonl` keeps 10 lines per `archive_keep`. It now says 16, which is what the code has kept since C.

- Harness lanes (OI-2163): the CLI's own plugin catalog cache no longer closes the deepseek and glm seats. On 18-10 a long deepseek_gate run made claude 2.1.302 write `plugins/plugin-catalog-cache.json` and `plugins/plugin-directory-cache-v2.json` into the lane's config dir, with nothing installed. The next spawn refused with `harness config dir holds user-level context (plugins)`, and every deepseek and glm seat stayed closed until the directory was removed by hand. `ensure_harness_config_dir` now allows `plugins/` only as a plain directory (no symlink) whose entries are plain files from a closed list of those two cache files. Both files only feed the `/plugin` browser (install counts, token costs); they enable nothing and reach no session context. Any other entry under `plugins/` (a subdirectory, `installed_plugins.json`, `known_marketplaces.json`, another file, a symlink) keeps the refusal, and the message now names it. Every other forbidden name is refused as before. No env variable or setting stops the fetch, so there is nothing to add to the spawn env.

- Release intro (OI-2161): the paragraph `vnx release` suggests for the new `## [X.Y.Z]` heading has no leading whitespace and carries the pin command inline in backticks, so pasting it passes the changelog check.
  A test builds the release with the real assembler and runs `compare_release()` on it.
- Release steps (OI-2162): `changelog.d/README.md` names the deletion of `scripts/ci/changelog_legacy_prs.txt` in the commit step and in the restore command after an interrupted run.

- Review gates (R4, operator decision 2026-10-17): `glm_gate` leaves the default takeover chain, which is now `codex_gate,kimi_gate,deepseek_gate` (`VNX_REVIEW_GATE_TAKEOVER_CHAIN` and `gate_request_handler._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN`).
  glm_gate stays a registered gate and an explicit chain or stack can still name it.
  With codex and kimi both out, deepseek reads one seat and the other ends as `chain_exhausted`: no merge until a subscription reviewer is back.

- Streaming drainer (OI-2128, xdist D3c): a provider child that crashes without a complete event now always gets its error event. The drainer read `process.poll()` right after EOF on stdout, but the kernel closes a dying child's stdout before the child can be reaped, so the exit code could still be missing at that moment. A SIGKILL or a non-zero exit then ended without any error event. Measured under CPU load on the old code: the first poll after EOF saw no exit code in 9 of 100 SIGKILLed children, and in 1 of 300 the second one missed it too. That run produced exactly what the xdist shadow showed (`['init', 'text']`, no error event). After the stream ends without a complete event, the drainer now waits up to 5 seconds for the exit code. A non-zero code gives the same `subprocess exited with code N before complete event` error as before. A child that is still running after the wait closed its output and kept going: the drainer kills it with the same process-group kill as its stall and deadline paths and emits an error event saying so. A stream with a complete event is not waited for, so a child that lingers after reporting its result stays the caller's to reap. Fixes `test_gemini_crash_negative.py::test_sigkill_mid_run_emits_synthetic_error` and `test_streaming_drainer.py::test_nonzero_exit_without_complete_emits_synthetic_error` under xdist. New tests hold the window open on purpose: a child that closes stdout first and then exits or keeps running.

- Plan-gate (P2 of plan-gate-alleen-voor-architectuur): one seat reads the plan, `default_seat`
  (codex) in `configs/plan_gate_panel.yaml`, and its one readable PASS clears the gate. When it
  returns BLOCK, `escalation_seat` (opus) reads the same plan in the same run and the rule decides
  over both. The governance variant no longer sizes the panel and a new feature no longer gets all
  five seats; the variant stays in the trace and on the round record. `--panel-seats` still
  overrides, runs exactly the seats it names and keeps the quorum of two for several seats.
  - Rounds count per plan version: the `plan_gate_round` record carries the plan's sha256 and the
    seats' verdicts. A revised plan always gets its seat round before the tiebreaker sees it, and
    the tiebreaker reads the latest version with the seat's verdict on it. `max_rounds` is 1: one
    seat, one round, then the tiebreaker, in the same run. A read version below the threshold
    must change first. Records written before this count as one version each.
  - The plan reviewer is asked for scope, PR split, decisions and ADRs, and feasibility, and not
    for line numbers or test details. A tiebreaker STOP now files the seat's real findings as
    open items.

- Headless worker exit code (xdist D3a, track `ci-profile-a-xdist`): `SubprocessAdapter` read the worker's returncode with a bare `poll()` the moment its stdout reached EOF. EOF comes a moment before the worker is reapable, so on a loaded machine `poll()` returned `None` and a worker that exited 0 came back from `spawn_claude` as returncode 1 with no error, status `failure`. It showed as `test_two_envelopes_started_in_the_same_second_each_keep_their_stream` failing in the xdist shadow (4 workers on 4 vCPUs) in 3 of 4 heavy CI runs of 18-10 while the serial Profile A passed, and it reproduced serially under CPU load. Both EOF sites (`read_events`, `read_events_with_timeout`) now reap the worker for at most 5 seconds before caching the returncode. A worker that closes stdout and keeps running is not waited for longer than that and caches no returncode, as before. The product defect, not a test defect: any headless dispatch on a busy host could be marked failed after a clean exit. Tests: `tests/test_subprocess_adapter.py::TestReturncodeWhenStdoutClosesBeforeTheWorkerExits`.

- CI xdist shadow (D3b): `test_smart_router_e2e.py` and `test_smart_router_multiprovider.py` no longer overwrite or rename the shipped `routing_recommendations.yaml` on disk to swap a fixture registry in.
  They patch `smart_router._RECOMMENDATIONS_PATH` per test instead, so a parallel worker reading the real registry (`test_valid_task_classes_matches_real_registry` and its neighbours) can no longer see a fixture or a missing file.

- CI xdist cutover history (D2b, OI-2145, OI-2147): `scripts/ci/xdist_parity_history.py` is back and applies the cutover rule of track ci-profile-a-xdist to the parity verdicts of VNX CI runs.
  The last D3 merge commit (`--last-d3-merge`) and the watch-list OIs (`--watch-list` or `--no-watch-list`) are required flags; the help text says where the operator gets them.
  The parity job's record step now runs `scripts/ci/record_xdist_parity_verdict.py`. It adds the comparison step's own outcome to the verdict, so a step that failed after writing exit 0 leaves `parity.parity=failure` and never a clean verdict. When no verdict was written, it writes an exit-2 verdict that carries every field of a normal one, and the step's bash fallback for a run without a checkout writes the same verdict.
  A light verdict now names its skipped steps in `step_failures`. The history tool counts a light run neither as clean nor as blocking. A verdict that lacks a field reads as no verdict, and one that never passed through the record step is not clean.
  The parity job's selection step gets the shadow's `VNX_HOME` and `VNX_DATA_DIR`, so collecting a serial entry cannot fail there alone.

- Architecture docs (track documentatie-bij-op-main, D1): `PROVIDER_LANES.md` gains the harness config dir, the takeover chain without `glm_gate` and the `vnx providers` switch. `DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` records that reviewers are not enriched and that intelligence follows the provider switch and the content boundary. `STATE_FABRIC.md` names the tracks database as the future layer. `SUBSYSTEMS.md` describes the xdist shadow job, the parity check and the function-size ratchet, and drops stale numbers from the health column.

- Architecture overview (track documentatie-bij-op-main, D1): `00_VNX_ARCHITECTURE.md` gains a Review Seats section and corrects the attention model, the supervisor interval, the nightly pipeline order, the dashboard routes and the data-flow steps. The File Formats section now points to the owner document of each format instead of carrying 2025 sample records, and version numbers are gone from the prose.

- Public manifesto (track documentatie-bij-op-main, D4b): `GOVERNANCE_ARCHITECTURE.md` gains a section on the governance chain as main runs it (merge door, review seats, takeover chain, provider switch, open outcomes, plan-gate with one seat, T0 guard, context-rotation plugin, changelog fragments) and corrects the pre-filter and gate-lock description. `ROADMAP.md` corrects statuses and file names that main contradicts. `EVOLUTION_TIMELINE.md` adds the period since 2026-09-28. Documentation only.

- Docs: the public manifesto (`docs/manifesto/README.md`, `ARCHITECTURE.md`, `ARCHITECTURAL_DECISIONS.md`, `LIMITATIONS.md`) describes main: the staged-bundle door, review seats and the takeover chain, the plan-gate with one seat, open outcomes, the provider switch with its enforcement state, the T0 guard and rotation, and the context-rotation plugin. Corrected the quality thresholds, the hash-chain limit, the worker default and the removed unmeasured claims. Documentation only.

- Docs (track documentatie-bij-op-main, D2): the README, the docs README, the docs and scripts indexes, the quickstart and the getting-started guide describe main again. The QUICKSTART first dispatch uses a built-in role instead of `hello-world`, which the dispatch door refuses. The README no longer lists `glm_gate` in the default takeover chain, names the current diff-char default by pointing at `DISPATCH_RULES.md` §2, and says a spec with `force_tmux: true` runs on `claude_headless` with a warning. New: the T0 context guard and rotation spawner (`vnx guard install/check`) in `00_GETTING_STARTED.md`, the context-rotation plugin in the README, and the new CI, release and guard scripts in `SCRIPTS_INDEX.md`. Docs: `README.md`, `docs/core/00_GETTING_STARTED.md`

- Plan scope per track (P1, track `plan-gate-alleen-voor-architectuur`): a track now carries one plan scope, `architecture` or `light`, as `plan_scope` in `tracks.metadata_json` next to `lane_hint`. No migration.
  `vnx horizon add` requires `--plan-scope`: `architecture` seeds `OI-PLAN-<track>`, `light` seeds no plan-gate. Without the flag the command refuses and creates nothing.
  The scope moves up by itself and never down: the fourth deliverable on a light track, or a build dispatch at the door whose paths are schema, security, gate/merge or store resolution, whose variant is `coding-strict`, or that names no paths, makes the track `architecture` and seeds `OI-PLAN` (a resolved one is never reopened). The scope and the seed commit together or not at all; a stored `architecture` track without any `OI-PLAN` row gets one on the next build dispatch at the door or `set-plan-scope`. The door refuses nothing yet; it writes, seeds and prints one line.
  Down only by hand: `vnx horizon set-plan-scope <track> light --reason "..."`. Every change writes one `track_plan_scope_set` line with old, new and reason, the scope chosen at `horizon add` included. `objective sync` no longer snapshots and restores `lane_hint`: the seeder's write keeps `lane_hint` and `plan_scope` as stored, and `set-lane-hint` merges into the stored metadata, so neither can put back an older scope. `vnx horizon show` names the scope and the rule that set it, `list` adds a `scope:` tag. Tracks from before this read as `architecture` with an open `OI-PLAN` and as `light` without one.
  New security path category in `smart_router` (provider spawns, `.claude/settings*`, `hooks/`, permission and sandbox modules, auth/secret/token/credential names, `.github/workflows/`). A plan-gate run whose paths, or its deliverables' new `--dispatch-paths`, are schema or security now runs both seats from the start and needs both readable verdicts.
  `plan_gate_enforcement.plan_gate_scope()` and `LIGHT_PANEL_LABELS` are removed: they had no caller outside their tests.

- Review gates (R1, track review-stopregel-en-lichte-stoelen): B3.1 and B3.2 are enforced in code.
  Every PASS and FAIL of every review seat is a line in `state/review_gates/rounds.ndjson`, keyed by project, PR, head and seat, written with fsync before the result record; when that write fails, the run fails and no verdict is published.
  The gate refuses a second run on a head that has a verdict in that seat, and every codex-seat run on a PR at the cap (three or more codex-seat blockers in one round, or a blocking latest round with six or more in total; a takeover in the codex seat counts, the second seat does not).
  The merge door counts a PASS only when its line is in the ledger and never a codex-seat PASS at the cap; the dispatch door refuses a build on the branch of a capped PR.
  `--override-b3 --reason "..."` on the gate run, `pr_merge.py` and `dispatch_cli.py` wins over the cap and is recorded in the ledger line, the `review_gate_result` receipt (which now also carries `head_sha`) and the `pr_merged` receipt.
  A store the gate has run in keeps a ledger from then on. A PR counts at the door only once the ledger has an `opened` line for it. The gate writes that line for a PR without earlier reviews; the evidence check fails closed: one headless report, decided result record (`pr-N-<gate>.json` or `N-<gate>-contract.json`) or `review_gate_result` receipt of a seat gate is enough, and a source that exists but cannot be read counts as evidence too (`unreadable:<path>`).
  Such a PR is refused at every seat as `round_history_missing` (`--override-b3` does not lift that) and at the door, until the operator opens it with `vnx review rounds open --pr N --reason "..."`: an `opened` line with `basis: attested`, actor and reason, plus a `review_round_opened` receipt. Earlier verdicts are not reconstructed; the cap starts at zero from that line.
  A ledger path that is not a regular file, or a line that does not parse, stops every PR it can touch (the PR it names, every PR when it names none) at the gate, the door and the writer alike, until the operator sets the file aside. Nothing removes, moves or rewrites a ledger line, and every append starts on a record boundary, so a torn line never swallows the next PR's verdict.

- Docs D3 (track `documentatie-bij-op-main`): the control chain on main is described. The receipt format covers open outcomes with `receipt_query.py decide` and `takeover_from`. The locks register covers the merge door's dry-run lines, the round ledger with `--override-b3`, the changelog fragment check and `vnx pr-ready`. The horizon docs cover plan scope with the `outside-repo` class, the reconciler's `deliverable_auto_completed`, the `plan-gate seats:` trace line and how to verify `.vnx-attest/plan-gates.ndjson`.

- Documentation track D5 (corrections after D1 to D4, docs only). `DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` names the content-boundary readers precisely: the conversation analyzer and the dream consolidation only, with the receipt classifier, its classifier providers, the tagger and the scout pre-pass listed as not reading it. `PROVIDER_LANES.md` distinguishes the enforced switch call sites (`ENFORCED`) from the pending ones (`PENDING_B`). `QUICKSTART.md` names the store root via `$DATA` from `vnx doctor` and records that `receipt_query.py` ships in the pip package but has no console entry point. `README.md` corrects the reviewer count, spells out what `open-outcomes` lists, and notes the deliverable reconciler's `deliverable_auto_completed` event. The onboarding guide walks through the built-in `research-analyst` role instead of the removed `examples/hello-world` path. `SCRIPTS_INDEX.md` drops `dispatch-agent.sh` from the `bin/vnx` list and notes `vnx dispatch-agent` is a pip CLI command. `DISPATCH_RULES.md` corrects the build-worker default (sonnet on the claude lane) and refreshes the CI-sweep numbers. The manifesto decision example gains `--state-dir`, and the timeline gets corrected phase dates, a per-phase release note and dated point releases.

- Plan-gate health surface (P1c, track `plan-gate-alleen-voor-architectuur`): the effectiveness probe, the config registry and the subsystem ledger no longer describe `VNX_PLAN_GATE_COMPLEX_ONLY` as a live scope-skip read-site. The flag is read by `complex_only_active()` but nothing consults that result to change a plan-gate run since P1 removed `plan_gate_scope` and its reduced 2-seat panel. The probe now reports `scope_skip_read_site: "absent"`; the flag, its read and the registry entry stay until P4 reworks the probe.

- Round ledger (OI-2180/OI-2181): `vnx review rounds open` now resolves its store from `VNX_STATE_DIR` the way the gate and the merge door do, through one helper both sites share, so with a state dir outside `<VNX_DATA_DIR>/state` the command writes the `opened` line and its receipt into the ledger the gate actually reads instead of one nobody sees.
  A spawn failure when the pip CLI starts `review_rounds_cli.py` is now caught, printed to stderr and returned as the module's error exit code, not an escaping `OSError`.

- Context-rotation plugin, deliverable E: `scripts/maintenance/export_toolkit_plugin.sh` exports a merged commit's tracked `packages/claude-code-toolkit/context-rotation` files into a local clone of the public toolkit repo, as one commit on the branch `export/context-rotation-<version>` with the exact bytes and modes of the source. The script never checks out, writes into, resets or cleans the clone's working tree or index. It builds the commit in a temporary worktree of its own outside the clone, with none of the clone's hooks, on the existing export branch or else on the commit the clone's checkout is on. The branch is the only ref it writes in the clone: last, as a compare-and-swap, on the branch itself and never through a symbolic ref (every `update-ref` it runs, the write and the swap back, uses `--no-deref`; the commit is written with `commit-tree`, without a ref). The run counts the branch as its own only once its own swap succeeded: a lost swap leaves the other writer's branch as it is and names what it holds now. It syncs deletions inside the plugin folder only, merges only the `context-rotation` entry into `.claude-plugin/marketplace.json` (other entries stay byte-for-byte) and adds the README row to the `## Tools` table once. It refuses a commit that is not on `origin/main`, a symbolic `origin/main` in the source, a source folder with a link, an operation in progress in the clone's checkout, an export branch that is a symbolic ref (the message names what it points at), an export branch that a worktree has checked out or is rebasing or bisecting, also under a symbolic ref that points at it, a link on the base where it writes, a version that differs between `plugin.json`, the marketplace entry and `--tag`, and a content change without a version bump (a mode change counts). Before the branch is written it runs the plugin's leak patterns over the committed blobs, also when there is nothing to export, and names a hit the base's marketplace file or README already had as pre-existing. A failure removes the temporary worktree and swaps back a branch this run wrote; when it cannot, it exits 3 and lists what is left. It never pushes or tags. POSIX sh, excluded from the wheel. The plugin gets a `README.md` with the maintainer runbook.

- Harness lanes (kosten-per-model-in-het-grootboek K0): the fabric-owned `settings.json` in `<VNX_DATA_DIR>/harness-config/<lane>/` now also carries `cleanupPeriodDays: 365`, beside the destructive Bash `ask` rules. The Claude Code default transcript retention of 30 days would otherwise start deleting the session transcripts under `<lane>/projects/` from early November, and those transcripts are the only source for the cost backfill (K6). The value is the fabric constant `HARNESS_TRANSCRIPT_RETENTION_DAYS`, never read from the operator's settings at runtime, and `ensure_harness_config_dir` already rewrites `settings.json` on every spawn, so existing lane dirs pick it up on their next spawn with no migration.

- Round ledger (OI-2180/OI-2181/OI-2185): an attested `opened` line (`vnx review rounds open`) is now the boundary every reader draws. What was booked for the PR before it counts for nothing at the gate, the writer, the merge door and the dispatch door, so the cap really starts at zero there.
  A re-anchored verdict (`gate_reanchor_cli.py --apply`) gets a ledger line for its new head that names the head it came from, instead of being taken for a rewrite and leaving the door NO-GO. It is booked only when that head's line is counted, and it is no new round.
  A seat handed to the next reviewer in the same call passes the round ledger again before the successor is requested; a refused advance books nothing and spends no run.
  The gate's history check reads the store the ledger lives in, not the one the environment names. `vnx review rounds open --data-dir` refuses with both paths when `VNX_STATE_DIR` overrides the state dir with another one.

- Test flake fix (OI-2130): `test_state_writer_compact_backfill_migration.py::test_rewrite_locked_atomic` waited for its `spawn` child with a 5 s timeout on both the result queue and `process.join`, but the spawn itself takes 3.0-5.1 s on CI, so the queue read could hit `_queue.Empty` before the child was up. The probe now reports it started via a new `started` Event set at the top of `_probe_lock`; the test waits on it with a 60 s timeout before calling `rewrite_locked`, so the short 5 s timeouts only measure the lock behaviour the test is actually proving (the rewrite holds the lock across `os.replace` and the child sees it blocked). No shipped code changed.

- DeepSeek harness key loading (OI-2182, #2118): the deepseek-harness lane now resolves its own key through the new `load_deepseek_key` loader (argument → `DEEPSEEK_API_KEY` → provider env file), and a key missing from every source is classified as the new `local_key_missing` failure class instead of `auth_rejected`, so a local config error no longer cools the lane down for everyone.

- DeepSeek harness key loading, fix-forward on #2118 (OI-2182, OI-2187): the smart router's deepseek-harness lane gate now resolves the key through the same `load_deepseek_key` loader as the spawn and classifier paths, so auto-route and tier escalation select the lane when the key lives only in the provider env file. An undecodable provider env file reads as a missing key instead of raising, and the last `DEEPSEEK_API_KEY=` assignment wins even when empty (matching the bash loader), so an empty final assignment no longer resurrects an earlier key. The test suite pins `VNX_PROVIDER_ENV_FILE` to an absent path per test so the "no own key => unavailable" property stays pinned on a host whose operator file carries a key.

- Context-rotation plugin 1.0.0 (track context-rotation-plugin): absolute thresholds and a loop guard.
  `warn_tokens`, `force_tokens` and `hard_tokens` (positive integers) give the three bands in tokens instead of the share of `window_tokens`. None set: percentage mode, exactly as today. All three set with `0 < warn < force < hard`: the bands come from the token values, and the percentages and `window_tokens` no longer decide one. One or two set, a bad value or the wrong order: one stderr line names the rule and the percentages stay, never a mix. Both the percentages and the tokens configured and valid: the tokens win. The three thresholds are computed in one function in `lib/context_budget.py` and used everywhere a band or a threshold is computed or shown; every guard message that names a threshold names it in tokens.
  A loop guard for a session that starts heavy from a handover injected after `/clear`: the start size (the first known measurement) is recorded in the per-session enforcement state (`VERSION` bumped, an older state counts as fresh), and the force and hard levels are reached only when the size is at or above the threshold and at least `min_work_tokens` (default 20000) above the start size. The warn band is unchanged and a compaction does not reset the start size. When start size + `min_work_tokens` is at or above the force threshold, the user gets one line at the first prompt where it is known, naming the start size and the force threshold, saying rotating will not make the next session smaller, and the two remedies (a higher force threshold, a smaller start context such as fewer CLAUDE.md imports). A fresh `startup` and a `/resume` are enforced as before.
  `plugin.json` goes to `1.0.0`, the plugin `CHANGELOG.md` folds in the two additions and rewrites the loop line under its limits, and the README gains the four settings rows, the rewritten loop paragraph and a large-window section with the 1M absolute-mode example `{"warn_tokens": 400000, "force_tokens": 500000, "hard_tokens": 600000}`.

- Plan-gate P1 follow-ups (#2117, OI-2179): `plan_scope.describe()` renders a scope set at creation as such, via a `CREATED_SCOPE_RULE` marker `create_track` now stores in `plan_scope_rule`; the roadmap seeder's `_row_matches` compares only the authored metadata fields, so `plan_scope`/`lane_hint` no longer read as an authored change on every apply; and `deliverable add` keeps its committed row and exits 0 even when the scope-raise cannot run, warning with `set-plan-scope` instead of exit 1.

- Plan scope reconcile after a failed projection write (P1e, track `plan-gate-alleen-voor-architectuur`): a repeated scope change now re-derives a track's `derived_status` whenever the store has an open `OI-PLAN-<track>` — newly seeded, already open, or a retry that changed nothing. Before, only the first seed reconciled, so a reconcile that failed once left `derived_status` stale and no repeat repaired it. `vnx deliverable promote` now also refuses while `OI-PLAN-<track>` is open in the open-items store, not only when the `derived_status` projection reads `blocked`, so the plan-first lock holds even when the projection lags.

- Context-rotation plugin 1.0.1 (track context-rotation-plugin): documentation only. The plugin README now names the absolute thresholds (`warn_tokens`, `force_tokens`, `hard_tokens`) next to `window_tokens` in "Check your window first" and points at "Large windows"; says the warn/force/hard bands are the percentages or, when set, the three token thresholds; notes under "Limits" that the plugin is untested in non-interactive `claude -p` runs and that a user-scope install also reaches automated runs on that account, so install with `--scope project` where you work interactively; records that the user settings file is always `~/.claude/context-rotation.json` regardless of `CLAUDE_CONFIG_DIR`; and adds the 1.0.0 live smoke result on Claude Code 2.1.303 to "Claude Code versions". The plugin version is 1.0.1 with a `[1.0.1] - 2026-10-20` CHANGELOG entry, and the VNX README now says the plugin is released with the two install commands.

- Round ledger (OI-2180 point 3, R1d): an applied `--override-b3` on the build door (`dispatch_cli.py`) now books one `override` line per capped PR in `state/review_gates/rounds.ndjson`, instead of only a `logger.warning` and a plan warning outside the canonical dict.
  The line carries the project, PR number, the head the cap fell on, the override reason, the env var that carried it (`VNX_OVERRIDE_B3_REASON`) and the cumulative blocker count. A failure to write the line refuses the override (fail-closed, `b3-override-ledger-error`); it never silently proceeds.
  The dispatch receipt (the `dispatch_created` register event and the `route_decisions` record) carries the override as an `override_b3` field with its reason; a dispatch without an override has no such field.
  `vnx review rounds show --pr N` lists a PR's ledger, including its `override` lines.

- B3 override booking, R1d fix round (fix-forward on PR #2125): the `--override-b3` line is booked in the round ledger only when the door actually commits to firing, not during `build_runtime_snapshot`.
  `_check_b3_cap_verdict` now only decides and carries the decision (which capped PRs, the reason) forward in memory on the `b3-cap-override-applied` verdict; the door books the `override` line(s) at the commit-to-fire point, after stop-conditions, lane reachability and every other refusal have passed, and before the lane is invoked. A `--dry-run --override-b3 --reason ...` writes zero ledger lines and zero receipt fields while still showing in the dry-run plan that the override would apply and for which PRs. A ledger write failure at the booking point refuses the fire with `b3-override-ledger-error` and nothing is dispatched. The `override_b3` receipt fields (`dispatch_created` and `route_decisions`) are written only on a real fire.

## [1.7.1] - 2026-10-03

A review verdict is now evidence in both directions. A rejection books as a
rejection: `REVISE` counts as `fail`, an executed gate that does not PASS fails
`request-and-execute`, and a `fail` without a blocking finding still blocks the
merge. A verdict is bound to the PR head it was given on, so a result about an
older head is neither booked nor accepted. A reviewer reads the whole diff
instead of the first 50,000 characters: `kimi_gate`, `glm_gate` and
`deepseek_gate` share one default cap of 800,000 characters. The second theme is
per-project guarantees: `ledger-health` is one launchd job per project, and
receipt lookups and the learning loop no longer mix receipts of projects that
share a dispatch id.

### What's new

- Reviewers read the whole diff: one default cap of 800,000 chars for `kimi_gate`, `glm_gate` and `deepseek_gate` (#2034)
- A rejection is a rejection: `REVISE` counts as fail and `deepseek_gate` defaults to `deepseek-flash` (#2025); an executed gate that does not PASS fails `request-and-execute` (#2013); the review-gate check requires a PASS by the shared rule (#2016)
- A verdict belongs to one PR head: no verdict about an older head is booked (#2019), verified results are bound to the head and an unreadable result counts as missing (#2021)
- `ledger-health` is one launchd job per project, guarded at install and at run (#2029)
- Abandoned bundles and failed worker exits stay open outcomes for the T0 (#2018)
- `project_id` scoping in `receipt_query` (#2024) and in the learning loop (#2023)
- Event archives may be compressed: readers open `.ndjson.gz`, and a footprint probe reports archive, salvage and free disk (#2030)
- Content boundary, part 1: the analyzer routes deep analysis by content and labels the session origin (#2032); glm and deepseek harness lanes get a clean `CLAUDE_CONFIG_DIR` (#2026); the dispatch role reaches the receipt (#2028)

### Added

- **Reviewer diff cap: one default of 800,000 characters (OI-1961 step 1; #2034).**
  `gate_lane_contract.DEFAULT_MAX_DIFF_CHARS` is derived from named constants
  (the 500,000 token target, a measured 3.8 characters per token and
  `ARGV_SAFE_MAX_DIFF_CHARS`) and is the default for `kimi_gate`, `glm_gate`,
  `deepseek_gate` and an unknown gate. A configured value above the ceiling is
  clamped with a warning that names the key and OI-1961. The config registry
  rows for the three `VNX_*_GATE_MAX_DIFF_CHARS` keys carry the same default.
- **Review verdict vocabulary: `canonical_review_verdict` (OI-1938; #2025).**
  One closed alias map turns `revise` into `fail` next to `VALID_VERDICTS`
  (unchanged). `extract_verdict_block` accepts a verdict through it, the codex
  register emit uses it, and `vnx gate` prints FAIL for a decided fail record.
  `materialize_artifacts` books one `verdict_without_blocking_finding` entry
  when a fail or blocked verdict carries no blocking-severity finding, so the
  verdict value decides and the status stays `completed`.
- **One install guard for launchd (OI-1942; #2029).**
  `scripts/lib/launchd_install_guard.py` is the single refusal rule for
  `vnx init` and `scripts/launchd/reload_plist.sh`. `com.vnx.ledger-health` gets a
  per-project Label, per-project logs and RunAtLoad, and joins
  `REQUIRED_PER_PROJECT_FAMILIES`. `vnx doctor` reads the job per project and
  warns about an installed plist that still holds a `${...}` placeholder.
- **Open outcomes for abandoned bundles and failed worker exits (OI-1934, OI-1935; #2018).**
  `open_outcomes` gets the kind `abandoned_dispatch` for bundles in
  `dispatches/abandoned/` (closed by a decision of the same project, deduped, an
  unreadable bundle still listed). `rejected/<reason>/` becomes a live bucket:
  a failed worker exit stays open until a T0 decision, with or without a
  receipt. `DISPATCH_RULES` section 13 is updated.
- **Compressed event archives and a footprint probe (OI-1944 part C1; #2030).**
  `event_store` gets `resolve_archive_file`, `open_archive_text`,
  `list_archive_files` and `find_archive_file`; agent-stream, `api_intelligence`,
  `event_analyzer` and `retroactive_backfill` read `.ndjson.gz` through them. The
  `vnx-data-footprint` probe (`scripts/lib/vnx_data_footprint_probe.py`)
  reports archive, salvage and free-disk footprint per store. Retention is not
  part of this release.
- **Content classifier and session origin (fase 0 F2 part 1; #2032).**
  `scripts/lib/content_class.py` classifies a session by boundary file, path
  and text. The analyzer stores `origin_class`, `origin_project_id` and
  `origin_source` per session and `improvement_suggestions.origin_class`.
  Client, personal and unknown sessions, a summary that names a restricted path
  and an unconfigured boundary get deep analysis on Claude only, capped at
  `VNX_ANALYZER_RESTRICTED_CLAUDE_CAP` (default 20). Over the cap, or with
  Claude unavailable, a session is `restricted_deferred` and gets the budget
  first the next night. `pacompany-engine` keeps its DeepSeek exception through
  `provider_exceptions`, never when the cwd or summary also hits a restricted
  root. Restricted and legacy NULL-origin suggestions no longer bridge into
  `antipatterns`, and `deep_analysis_model` names the lane.
- **Clean `CLAUDE_CONFIG_DIR` for harness lanes (fase 0 F1; #2026).**
  glm-harness, deepseek-harness, the deep analyzer and the deepseek classifier
  run under `<VNX_DATA_DIR>/harness-config/<lane>` with
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, so the operator's `CLAUDE.md`, imports,
  memory and personal skills no longer reach the provider. The lane fails
  closed. Transcript readers also search the lane directories.
- **Four build rules in the project `CLAUDE.md` (fase 0; #2031).**
  A `## Build rules` section outside the bootstrap block, so harness-lane, kimi
  and codex workers see them. The bootstrap block is unchanged.

### Changed

- **A dispatch role reaches the receipt (fase 0 F3; #2028).**
  `dispatch_identity.resolve_effective_role` puts the role of the spec between
  the caller's role and `dispatch_metadata`, and `report_parser` and
  `receipt_writer` stamp `role` through it. `_receipt_exists_for_dispatch` in
  `envelope_govern_support` only lets a lane completion receipt suppress the
  envelope receipt, decided on parsed JSON.
- **Corrective receipts carry the real provider and terminal (OI-1898; #2020).**
  The phantom guard and the PR enforcement receive provider and terminal from
  the lanes (the door's env export as fallback), and enrichment no longer stamps
  the sentinel provider.
- **`deepseek_gate` defaults to `deepseek-flash` (OI-1939; #2025).**
  `VNX_DEEPSEEK_GATE_MODEL` still overrides it. The plan gate and the build-lane
  default are untouched.

### Fixed

- **A rejection is a rejection (OI-1921, OI-1892, OI-1938; #2013, #2016, #2025).**
  `_execute_requested_gates` no longer reads the merge-policy `required` flag
  for an executed gate: a decided non-pass fails the call (#2013).
  `_check_review_gate_required` (direct and takeover) requires
  `gate_status.is_pass` on top of contract hash and head binding, with a failure
  message that names the reason (#2016). `REVISE` and a `fail` without a
  blocking finding book as a rejection (#2025).
- **A verdict is bound to the PR head (OI-1914, OI-1893, OI-1877; #2019, #2021).**
  The gate-obligation runner re-resolves the PR head after the gate ran: a
  provable mismatch stays pending (`stale_evidence_sha_mismatch`) and a PR with a
  positively live dispatch stays pending (`dispatch_still_running`) (#2019).
  `verify_report` binds each gate that is not chain-exhausted to the PR head
  (`SHA_MISMATCH`, exit 1) and a result file that is not a JSON object is
  `MISSING_ARTIFACT`, also for the chain-exhausted record (#2021).
- **Receipt lookups scope by `project_id` (OI-1925; #2024).**
  `by-pr`, `since`, `by-dispatch`, `by-track` and `reconcile-oi-pending` filter
  through `receipt_outcome.is_foreign_project` (ADR-007). The functions take
  `project_id`, the CLI gets `--project-id` and the JSON output names the
  project. Reconcile no longer files or counts foreign warnings. The `digest`
  default without a project resolver is OI-1928 and is not part of this fix.
- **The learning loop scopes failure receipts by `project_id` (OI-1909; #2023).**
  `extract_failure_patterns` no longer turns another project's failure receipts
  (colliding dispatch ids) into patterns of this project. An unresolved project
  fails closed for stamped lines. The foreign count shows in `receipt_stats`,
  the corpus log and `receipts_skipped`.
- **Pattern import merges on the natural key (OI-1899; #2017).**
  `success_patterns` and `antipatterns` import goes through `pattern_upsert`
  (counter is the max, exported id dropped, rows without `project_id` skipped
  and counted) and the result carries `pattern_import {inserted, merged, skipped}`.
  Other tables keep `INSERT OR REPLACE`.
- **The subprocess success path writes a contract-valid report (OI-1933; #2015).**
  `_handle_success` no longer writes the legacy stub. The close-out report has
  Summary, Changes, Verification and Open Items plus Dispatch-ID, Model and
  Provider, is written without `VNX_REPORTS_DIR`, and never overwrites a report
  the worker wrote.
- **All-red verification keeps the highest failure count (OI-1922; #2012).**
  The all-red branch of `counted_run` returns the maximum failed count over the
  red runs, with passed and method taken from the last run.
- **The plan-gate probe reports an unreadable blocker table as degraded (OI-1840; #2014).**
  `PlanGateEffectivenessProbe` records `oi_plan_source: unreadable` with a
  reason, reports the three `oi_plan_*` counters as `None` instead of 0 and
  returns `degraded`.
- **`vnx_paths.sh` no longer turns off errexit in its callers (OI-1904; #2022).**
  The option snapshot ran inside `$(...)`, where bash clears errexit, so the
  trailing `eval` switched `set -e` off in every caller. Options are captured
  with `[[ -o ... ]]` in the current shell and restored explicitly. The same fix
  is in `process_lifecycle.sh` and `ops_process_control.sh`.
- **Forge check-runs go to the project's repo, and no test mutates launchd (OI-1789, OI-1891; #2027).**
  The target repo comes from the store the record was written to. An
  unattributable store, an id mismatch, a central install or a non-GitHub origin
  is refused without a POST. A `launchctl` shim in `tests/conftest.py` refuses
  every mutating verb under the test runner.

### Upgrade notes

- **ledger-health (#2029).** After the update every project reinstalls its job
  with `bash scripts/launchd/reload_plist.sh com.vnx.ledger-health <project-id>`,
  then unloads and removes the legacy bare install
  (`launchctl unload ~/Library/LaunchAgents/com.vnx.ledger-health.plist`, then
  remove that file). Until then the bare job stops without writing a beacon,
  because `scripts/ledger_health.py` rejects a `VNX_PROJECT_ID` that is not a
  project id (the unsubstituted `${VNX_PROJECT_ID}` of the old plist). `vnx doctor`
  fails on a missing per-project job and warns about a bare label and about an
  installed plist that holds a placeholder. The install guard refuses a
  worktree, a linked worktree and an unregistered clone.
- **Review diff cap (#2034).** Defaults at v1.7.0: `kimi_gate` 400,000,
  `glm_gate` 50,000, `deepseek_gate` 50,000, unknown gate 50,000. From 1.7.1 all
  four are 800,000. A project value in `VNX_KIMI_GATE_MAX_DIFF_CHARS`,
  `VNX_GLM_GATE_MAX_DIFF_CHARS` or `VNX_DEEPSEEK_GATE_MAX_DIFF_CHARS` still wins;
  a value above 800,000 is clamped with a warning. A large diff on an API-billed
  seat costs more per review than before.
- **Review verdicts (#2025, #2013, #2016).** `REVISE` counts as fail. A `fail`
  verdict without a blocking finding books as a rejection. A project that relied
  on such a review passing now sees a blocked merge.
- **`deepseek_gate` model (#2025).** The default is `deepseek-flash`;
  `VNX_DEEPSEEK_GATE_MODEL` still overrides it.
- **Schema (#2032).** `schemas/quality_intelligence.sql` gains the origin
  columns marked v34 (`origin_class`, `origin_project_id`, `origin_source` on
  `session_analytics`, `origin_class` on `improvement_suggestions`). This is the
  quality intelligence database (`quality_intelligence.db`), not the runtime
  coordination store. A fresh store gets the columns from the schema file. An
  existing store gets them from `_migrate_v34` in `scripts/quality_db_init.py`
  (additive, nullable TEXT, no default), which also adds
  `session_analytics.deep_deferred_reason`, and ends on `user_version` 34
  (`HIGHEST_QI_VERSION`). Rows written before v34 stay NULL and the readers treat
  them as restricted. `scripts/lib/migrations/` is unchanged since v1.7.0, so
  the runtime coordination store stays on `user_version` 33. The installed
  conversation-analyzer plist must be re-rendered to pick up
  `VNX_ANALYZER_RESTRICTED_CLAUDE_CAP`.

### Known issues

Known properties of 1.7.1:

- The reading budget is 800,000 chars, not the decided 500,000 tokens. The
  review prompt travels as one command-line argument and macOS allows about
  1 MB for argv plus environment. Delivery by stdin or file follows (OI-1961).
- A review that returns `fail` with only warning-level findings blocks the
  merge. The first case was a function-size warning on #2029. A deterministic
  function-size check in CI replaces the reviewer rule in a following release.
- Receipt history sits at about 0 accept. Before D1b (#2000, merge `70574f5d`)
  the receipt read the first, often red, run. Old receipts are not corrected
  (operator decision 5, no epoch correction). From `70574f5d` the last green
  run counts. Accept grows as writers use the red-run label.
- Attribution (an outcome on a different id, `unlinked_gate`) follows as its own track.
- 0015 column drift. On existing stores (vnx-dev, seocrawler-v2,
  pacompany-engine) `project_id` is missing on 7 runtime tables:
  `retry_budgets`, `retry_state`, `escalation_log`, `execution_targets`,
  `inbound_inbox`, `recommendations` and `recommendation_outcomes`. A fresh
  store has them. No code reads that column. The repair migration did not make
  1.7.1.
- Excluded tests, as in `scripts/ci/test_exclusions.txt` on this release (OI-1227,
  since 15-08): `test_build_t0_state_exception_handling::TestRunsClean::test_build_t0_state_runs_clean_on_main`
  (exit 1 is health degraded because beacons are absent on a fresh store);
  `test_dispatcher_drain_lifecycle`: `test_dispatch_paths_written_when_provided`,
  `test_classify_completion_no_dead_letter_on_failure`,
  `test_transient_fail_then_success_only_completed`;
  `test_wiring_completeness::test_provider_dispatch_auto_route_calls_smart_router_route`.
  `tests/test_conversation_analyzer.py` is excluded too and now points to OI-1958
  (#2033): two tests assert behaviour from before the inference guard of #805,
  which emits no hint. Measured solo: 2 failed, 90 passed.

Delivered from the "Follows in 1.7.1" list of 1.7.0:

- OI-1933: the subprocess success report is contract-valid (#2015).
- OI-1934 and OI-1935: abandoned bundles and failed worker exits stay open outcomes (#2018).
- OI-1925: `receipt_query` lookups scope by `project_id` (#2024). The `digest`
  default without a project resolver (OI-1928) is not part of it.

Follows in 1.7.2:

- What 1.7.0 does and does not guarantee. It guarantees that a dispatch
  without an outcome never closes silently. It does NOT guarantee that every
  dispatch has a report. The headless envelope lane, the default lane, has no
  manifest in `active/` and no safety net if the orchestrator dies before
  `_govern` (OI-1932, high).
- `recovery.py:399` puts a dispatch in `dead_letter` after an exhausted retry
  budget. That must become an `open_outcome` (OI-1931).
- `ledger_health check_receipt_coverage` does not filter on `project_id`. That
  is fail-open under ADR-007 and a blocker candidate (OI-1924).
- The `receipt_query` `digest` default runs without a project resolver
  (OI-1928).

## [1.7.0] - 2026-09-30

Every dispatch now has exactly one computed outcome, and a dispatch without an
outcome never closes silently again. The outcome is folded from the ledger
(`receipt_outcome`) instead of counted per verdict stamp, and the digest, the
recent-receipts view, the weekly digest and the drain all read it. A dispatch
that ends without an accept or a reject surfaces to the T0 as an
`open_outcome`, and `dead_letter` is only reached through a T0 reject in the
decision log. This release closes the `fabric-state-herstel` track (13 points),
runs the learning loop in shadow mode, adds the Sonnet 5.5 registry entry and
ships a series of fixes. The learning loop persists nothing in 1.7.0.

### What's new

- One outcome per dispatch: `receipt_outcome`, computed rather than claimed (#1980, #1990, #1992)
- No end state without an outcome: `open_outcomes` surface to the T0 instead of a silent close (operator decision 6) (#2008, #2010)
- The last green verification run counts, with an explicit red-run label (#2000, plus the T0 role rule in #2010)
- A fresh store reaches `user_version` 33 again: `auto_apply` fails closed (#2009)
- The nightly conversation analyzer runs again (claude found under the launchd PATH) (#2007)
- No duplicate PR on a fix-forward (OI-1906) (#2003)
- `t0_state` shrinks from 578 KB to 29 KB (#1999)
- Live work comes from the `dispatches` table and the occupancy flock (#1984)
- The test leak that wrote `PR_QUEUE.md` into the real checkout is closed (OI-1919) (#2006)

### Added

- **`receipt_outcome`: one outcome per dispatch (fsh D3a, D3b, D4a; #1980, #1990, #1992).**
  `scripts/lib/receipt_outcome.py` folds a dispatch's ledger lines into one of
  accept, investigate, reject, superseded or unknown. Noise (pytest sources,
  temp report paths, foreign `project_id`) is dropped, gate-runner dispatches
  count as evidence linked to the work dispatch via the PR, and the ledger
  itself is untouched (`OUTCOME_READER_EPOCH` dates the reader-side switch).
  `receipt_query.py digest` now reports per dispatch and keeps the old per-line
  tally as `line_verdict_counts`. `_build_recent_receipts`, `weekly_digest` and
  `check_active_drain` read the same module, so a later failure after an
  earlier success shows as failure and a dispatch with two `task_complete`
  receipts counts once.
- **`open_outcomes` and `decide` (fsh D4b1, D4b2; #2008, #2010).**
  `scripts/lib/open_outcomes.py` lists every dispatch of the project whose
  outcome is reject or investigate and that has no `outcome_decision`.
  `receipt_query.py decide <id> accept|reject --reason` records the decision
  through `t0_decision_log.write_decision`, and the byte-cursor `pull` is
  removed. `t0_index.json` carries `open_outcomes` (schema `t0_index/1.2`). A
  new kind `active_dispatch` covers dispatches still in `active/` without a
  receipt or outcome. `governance_emit.emit_fallback_report` writes a report
  for a worker the crash sweep finds dead. The drain, the janitor and
  `dispatch_cleanup` share one predicate and only move an accept to
  `completed/`. The headless daemon keeps a failed delivery in `active/`, and
  `vnx doctor` reads the `open_outcomes` check.
- **`verification_runs`: the last green run counts (fsh D1b; #2000).**
  `scripts/lib/verification_runs.py` reads every pytest or unittest summary in
  the Verification section and counts the last run that is not marked red
  (`Red run`, `Rode run`, `Before the fix`, `old code`, `baseline`). Both
  receipt write paths build `verification{}` through one helper, and
  gate-runner dispatches get `method="gate_evidence"` without counts.
  `## Verificatie` is read as well.
- **Learning loop in shadow mode (#1961, #1963, #1965, #1966, #1968, #1969, #1972).**
  The loop runs as phase 2.6 of `conversation_analyzer_nightly.sh` with an
  in-memory pattern DB and writes every would-be change to
  `learning_loop_shadow_report.json`. Nothing is persisted: `VNX_LEARNING_LOOP_PERSIST=1`
  is not part of 1.7.0. Receipts before `VNX_LEARNING_LOOP_CUTOFF` are ignored,
  a `learning_loop_nightly_beacon.json` is written on every run (`vnx_doctor`
  fails on a missing or 36 hour old beacon), and unresolved open items of the
  project are a second signal source (a title class over 3 origin dispatches
  becomes a proposal). The D3a filter keeps real failures with an unresolved
  provider (895 of 931 receipts were dropped before). Pattern tables get the
  natural key `(project_id, pattern_type, title)` with one write path
  (`pattern_upsert.py`) and a dry-run-by-default migration
  (`migrate_pattern_natural_key.py`). `pattern_reattribution.py` moves
  mis-stamped rows to their real `project_id`. The nightly analyzer defaults to
  `deepseek-flash`, checks `/user/balance` before it starts and bills cache-hit tokens.
- **Sonnet 5.5 in the registry, and receipts record the model that ran (#1973).**
  `sonnet-5-5` is added (alias `sonnet` resolves to `claude-sonnet-5-5`,
  2.00/10.00 per Mtok) and `sonnet-5` stays for comparison. The headless lane
  reads the real model from the claude-stream init event and stamps it as
  `model_resolved` on the `task_complete` receipt.
- **Reconciler honours dependency kind, plus `vnx horizon remove-dependency` (fsh D9; #1981).**
  Only `hard` edges in `track_dependencies` block a track. `soft` and `overlap`
  edges show as `advisory_deps`, and an unknown kind counts as `hard`. The new
  verb deletes one edge by its full key and emits `track_dep_removed` first.
- **`t0_role_audit.sh --static` checks the role against the repo (fsh D10; #1987).**
  `scripts/lib/t0_role_sources_audit.py` tests every script and subcommand the
  role and `DISPATCH_RULES` name, and every state path against the writers
  manifest `t0_role_state_writers.txt`.

### Changed

- **`t0_state.json` goes from 578 KB to 29 KB (fsh D8; #1999).** Eight sections
  without a reader outside the builder are gone (`feature_state`, `pr_progress`,
  `canonical_tracks`, `human_gate_queue`, `dispatch_register_events`,
  `recent_dispatches`, `intelligence_brief`, `dispatch_insights`), as is
  `pr_queue.queued_features`. A T0 asks for what it needs with `vnx objective`,
  `vnx deliverable list` and `receipt_query.py`.
- **Live work comes from the `dispatches` table (fsh D5; #1984).** `live_work`
  replaces `active_work`, `queues.active` and the terminals in `t0_index.json`.
  It reads `runtime_coordination.db` through a read-only connection, and
  liveness is "lock held" via `dispatch_worktree_isolation.probe_occupancy`
  (`live`, `starting`, `stale` or `unmeasured`). A failed read degrades
  `system_health` with a reason.
- **Gate bundles leave `dispatches/pending/` (fsh D7; #1979).** The gate result
  writers in `gate_recorder` move the gate's own `final_prompt` bundle to
  `completed/` or `failed/`, and `dispatch_cleanup` only moves a bundle when a
  matching result or `review_gate_result` receipt proves it. `_move_bundle`
  refuses an existing destination.
- **Reviewer prompts are no longer enriched (OI-1444; #1964).** A single
  decision point, `reviewer_roles.enrichment_allowed`, keeps repo-map, skill
  and intelligence context out of review-gate and plan-reviewer prompts on all
  four injection paths.
- **Health expects only headless-era daemons (fsh D6; #1983).** Only
  `receipt_processor` is expected as a daemon. `beacon_summary` reports status,
  age and reason per beacon (a beacon with 5,000 rejected rows went from 1.16 MB
  to under 8 KB in `system_health`), and the quality DB probe is read-only and
  reports `db_reason`.

### Fixed

- **`auto_apply` fails closed, a fresh store reaches `user_version` 33 (#2009).**
  A fresh `runtime_coordination.db` stuck at 10 because `auto_apply` skipped
  every number without an `apply_NNNN.py`, so 0015 never ran and 0022 raised
  `no such column: project_id`. Every pending number now resolves to a runner,
  the pure-SQL runner (0028, 0029, 0030) or a declared `APPLIED_ELSEWHERE`
  entry, and anything else raises `UnhandledMigrationError`. `apply_0015.py`
  carries the runtime partition of 0015 with column-existence guards.
- **Nightly conversation analyzer finds claude under launchd (#2007).**
  `claude_cli.resolve_claude_cli()` looks on PATH and then in `~/.local/bin`
  and `~/.claude/local`, and is used at both call sites in `deep_analyzer.py`.
- **Nightly analyzer fails on an error (D4a; #1966).**
  `conversation_analyzer_nightly.sh` called the non-existent `ensure_env` and
  lost `set -e` after sourcing `vnx_paths.sh`, so every phase logged complete.
  Both are fixed and a failing phase now exits with its code.
- **Dreaming reads its full input (OI-1821, OI-1258, OI-1822; #1967).** The
  consolidation prompt is split into batches under 50,000 characters, failed
  deep analyses are counted with a reason, and `check_dream_cycle` warns on a
  degraded or proposal-only cycle.
- **No duplicate PR on a fix-forward (OI-1906; #2003).** `_enforce_push_pr`
  passes `work_ref` to `enforce_pr_exists`, so the OI-1392 path handles it.
- **`parent_dispatch` is derived from `work_ref` on stage (#1962).** Only 37 of
  1,365 receipts carried it. `resolve_parent_dispatch` derives it from
  `dispatch/<X>` and refuses to stage a bundle with a conflicting lineage.
- **`extract_validation` reads `## Verification` (fsh D1; #1975).** Contract
  reports produced `method=unknown` and zero tests before.
- **Three errors in the T0 index (fsh D2; #1974).** `recent_receipts` took the
  oldest three instead of the newest, `queue.open_prs` counted an empty table,
  and the dead `lease_expires` is removed.
- **The PR queue test no longer writes into the real checkout (OI-1919; #2006).**
  An autouse fixture points `VNX_PROJECT_ROOT`, `PROJECT_ROOT` and `VNX_HOME`
  at `tmp_path`, and a module guard compares the real `PR_QUEUE.md` before and after.

### Upgrade notes

The migration to `user_version` 33 runs at rollout. A store below 33 (known:
`website-vincentvandeth` at uv=10, OI-1927) gets a backup first, `user_version`
and schema hash are recorded before and after, and the `dispatches` table must
stay readable. If the migration fails, the backup is restored and that store
stays on 1.6.x; the rest of the fleet does not wait for it.

### Known issues

Known properties of 1.7.0:

- Receipt history sits at about 0 accept. Before D1b (#2000, merge `70574f5d`)
  the receipt read the first, often red, run. Old receipts are not corrected
  (operator decision 5, no epoch correction). From `70574f5d` the last green
  run counts. Accept grows as writers use the red-run label.
- Attribution (an outcome on a different id, `unlinked_gate`) follows as its own track.
- 0015 column drift. On existing stores (vnx-dev, seocrawler-v2,
  pacompany-engine) `project_id` is missing on 7 runtime tables:
  `retry_budgets`, `retry_state`, `escalation_log`, `execution_targets`,
  `inbound_inbox`, `recommendations` and `recommendation_outcomes`. A fresh
  store has them. No code reads that column. A repair migration with an
  idempotency test follows in 1.7.1.
- Excluded tests (OI-1227, since 15-08), older than this release and unchanged:
  `test_build_t0_state_exception_handling::TestRunsClean::test_build_t0_state_runs_clean_on_main`
  (exit 1 is health degraded because beacons are absent on a fresh store);
  `test_dispatcher_drain_lifecycle`: `test_dispatch_paths_written_when_provided`,
  `test_classify_completion_no_dead_letter_on_failure`,
  `test_transient_fail_then_success_only_completed`;
  `test_wiring_completeness::test_provider_dispatch_auto_route_calls_smart_router_route`.

Follows in 1.7.1:

- What 1.7.0 does and does not guarantee. It guarantees that a dispatch
  without an outcome never closes silently. It does NOT guarantee that every
  dispatch has a report. The headless envelope lane, the default lane, has no
  manifest in `active/` and no safety net if the orchestrator dies before
  `_govern` (OI-1932, high).
- `recovery.py:399` puts a dispatch in `dead_letter` after an exhausted retry
  budget. That must become an `open_outcome` (OI-1931).
- `recovery.py:281` writes a report stub without a contract (OI-1933).
- A bundle in `abandoned/` also becomes an `open_outcome`. The bucket stays (OI-1934).
- `cleanup_worker_exit` moves a failed dispatch to `rejected/<reason>/` without
  a T0 decision. The bucket stays, the `open_outcome` decides (OI-1935).
- `ledger_health check_receipt_coverage` does not filter on `project_id`. That
  is fail-open under ADR-007 and a blocker candidate (OI-1924).
- `receipt_query` readers and the digest default run without a project
  resolver (OI-1925, OI-1928).

## [1.6.7] - 2026-09-28

Closes the "poorten en workers in hun hok" goal. Harness-lane review gates
(`glm_gate`/`kimi_gate`/`deepseek_gate`) now review in isolation on the PR
branch, with a vangnet on the orchestrator's own checkout if one still tries
to touch it. `VNX_DISPATCH_PATHS` is set again, so
`VNX_ENFORCE_WORKER_PERMISSIONS=1` actually narrows a dispatch's worker to
its declared paths. A gate PASS only counts on the PR's current head — an
older head's PASS no longer satisfies the check after a fix-forward push.
`vnx gate`'s slot-line names whoever actually read the seat, including a
takeover. `kimi_gate` is now named in the gate execution and evidence
contracts. And `vnx dispatch <file.md>` is refused under the door default
(rollback: `VNX_DISPATCH_LEGACY=1`).

### Fixed

- **`VNX_DISPATCH_PATHS` regains a setter (OI-1886, #1958).** The only reader
  (`pretooluse_worker_scope_enforce.py`'s worker-scope hook) had its one setter,
  `TmuxInteractiveDispatch._spawn_session`, removed along with the tmux lane itself
  (#1868). A dispatch declaring `dispatch_paths` was silently no longer narrowed on
  ANY lane — only the coarse role scope still applied when enforcement was on. The
  headless lane (`ClaudeSubprocessAdapter`), the terminal-pinned subprocess lane
  (`deliver_via_subprocess`), and the provider lanes (kimi/glm/deepseek/codex/gemini,
  both via `provider_dispatch`'s own CLI dispatch functions and via
  `envelope_adapters_provider.ProviderAdapter`) now all export `VNX_DISPATCH_PATHS`
  (and `VNX_WORKER_ROLE`, same gap) through one shared encoder,
  `worker_permissions.dispatch_paths_env_value`. `VNX_ENFORCE_WORKER_PERMISSIONS`
  stays default OFF (operator decision, unchanged) — this only makes the narrowing
  work once enforcement is turned on.
- **`vnx gate`'s slot-line names whoever actually read the seat (OI-1888, #1957).**
  `scripts/commands/gate.sh` printed `Gate '<seat>': PASS` from the literal
  `--only`/required-gates argument and `request-and-execute`'s exit code
  alone. When a seat's own reader is unavailable and the takeover chain hands
  it to a different gate (`kimi_gate` unavailable -> `glm_gate` reads
  instead), the exit code already reflected the successor's real verdict, but
  the line kept naming the original seat as if it had read — PR #1952 merged
  on 2026-09-27 on exactly that misreading. A new helper,
  `scripts/lib/gate_seat_line.py`, resolves each requested seat against the
  `request-and-execute` report — reusing
  `gate_enforcement_verify.resolve_seat_entries`, the same takeover
  interpretation `scripts/t0_gate_enforcement.sh` already relies on, instead
  of a second one — and gate.sh now prints one line per seat in that shape:
  unchanged (`Gate 'kimi_gate': PASS`) when the seat answered itself, with the
  successor named on a takeover (`Gate 'kimi_gate' -> glm_gate (takeover,
  lane_exhausted): PASS`), and never a PASS for an unavailable seat with no
  successor (`Gate 'kimi_gate': UNAVAILABLE (<reason>)`, non-zero exit). A
  truncated diff is marked on the line (`(diff truncated)`).
- **`codex_gate_required`/`kimi_gate_required` now bind to the PR's current head
  (OI-1884, #1956).** `governance_enforcer.py`'s `_check_review_gate_required` used to accept
  any `pr-<N>-<gate>.json` result (direct or takeover-successor) that carried a
  non-empty `contract_hash`, without comparing the result's `commit_sha` to the PR's
  actual head. After a fix-forward push, a PASS recorded against the OLD commit kept
  satisfying the check forever — the new code on the head was never reviewed. The
  check now resolves the PR's head once per invocation (context-supplied
  `head_sha`/`commit_sha`/`headRefOid`, else `gh pr view --json headRefOid`), refuses
  when the head cannot be determined, and only counts a record whose own `commit_sha`
  equals that head. A record with no `commit_sha` at all is treated as stale evidence,
  never a wildcard.

### Removed

- **Raw-file `vnx dispatch <file.md>` refused under the door default (ADR-025, #1954).** The
  form that used to fall through to the legacy delivery lane with a one-time DEPRECATED
  stderr warning now exits non-zero and tells the caller to stage first
  (`vnx dispatch stage --instruction <file.md> ...` / `dispatch_bridge.py stage ...`) and run
  `vnx dispatch <pending-id>`. `VNX_DISPATCH_LEGACY=1` (or `VNX_SINGLE_ENTRY_DISPATCH=0`)
  still reaches the unchanged legacy lane, byte-identical, for when the door itself is broken.

### Fixed

- **Harness-lane review gates (`glm_gate`/`kimi_gate`/`deepseek_gate`) no longer isolate
  on `origin/main` when reviewing a PR (OI-1887, #1959).** Measured 27-09 on PR #1950: the
  provider worktree `_prepare_provider_workdir` created had no PR content of its own, so
  the dispatched agent fetched and checked out the PR branch itself — 96 times, IN THE
  ORCHESTRATOR'S OWN CHECKOUT (a stray branch appeared in the operator's repo). The
  harness-lane runner (`gate_runner._run_harness_lane_path`) now validates and fetches
  `origin/<branch>` up front (`gate_worktree.fetch_branch`, extracted from
  `create_gate_worktree`) and threads it into the governed dispatcher
  (`plan_gate_panel._make_default_dispatcher`'s new `base_ref` parameter, `provider_dispatch.py
  --base-ref`) so the provider's own isolated worktree already has the PR branch. An
  unfetchable branch books `unavailable`/`harness_lane_branch_unavailable` before any
  dispatch — never a silent fallback to main. A deterministic vangnet snapshots the
  orchestrator's own checkout (HEAD, branch list, working-tree status) before and after
  the dispatch; a mismatch books `unavailable`/`harness_lane_touched_main_checkout` and
  overrides whatever the dispatch itself returned, including a PASS. The standalone
  `scripts/glm_gate.py`/`scripts/kimi_gate.py` CLIs (deepseek has no standalone CLI, only
  the harness-lane path above) carried the identical missing-`base_ref` pattern at their
  own `_make_default_dispatcher` call sites and are fixed the same way.
- **The OI-1887 vangnet no longer trips on the fabric's own dispatch-branch
  churn (#1959).** `refs/heads` is shared across every worktree of a repo, and a
  correct harness-lane run mints `dispatch/<id>` branches as a side effect
  — the gate's own provider worktree (`git worktree add ... -b
  dispatch/<safe_id>`, left behind after teardown) and any sibling dispatch
  running concurrently. The first vangnet compared the full `git branch
  --list` output, so every real run booked
  `unavailable`/`harness_lane_touched_main_checkout`. The comparison now
  excludes the `dispatch/` prefix and compares every other branch by name
  AND commit sha (`git for-each-ref`, not `git branch --list`), so a branch
  reset to a different commit under an unchanged name is still caught — as
  is any branch outside that prefix (the exact `pr1950` shape from PR
  #1950).

### Documentation

- **`kimi_gate` and the harness-lane fallback documented in the gate execution and evidence
  contracts (OI-1885, #1955).** Doc 180's NG-1, timeout table, and stall table, and Doc 130's verdict
  table and GE-7, now name `kimi_gate`/`glm_gate`/`deepseek_gate` and record that they carry no
  per-gate subprocess timeout override, no stall detection, and a `status` field
  (`completed`/`partial_review`) that GE-7's literal `status or verdict` check cannot alone turn
  into a pass/fail verdict — `gate_status.is_pass()` also needs `blocking_findings`.
- **Docs refreshed to v1.6.6 reality across the board (#1939, #1940, #1941, #1942, #1943,
  #1944, #1945, #1946, #1947, #1948, #1949, #1950, #1951, #1952).** Fourteen PRs: archived
  completed/dead docs and the tmux-pane delivery contract in `docs/core`, `docs/operations`,
  and `docs/investigations`; refreshed ADR status lines, the public manifesto, lane/permission
  truth in active reference docs, operations runbooks, `00_VNX_ARCHITECTURE.md`, contracts,
  intelligence and scripts-index, and the onboarding entry points (merged, pip/bash CLI drift
  fixed); merged roadmaps and regenerated `PR_QUEUE`; rebuilt `DOCS_INDEX`/README/operations-README;
  aligned the top-level README.
- **Operator's transcript-backup workstation doc removed from the repo (#1953).**
  `docs/operations/TRANSCRIPT_BACKUP_ARCHIVE.md` described the operator's own Mac
  Mini/launchd backup setup, not VNX runtime, and the operator holds a local copy — dropped
  entirely rather than archived, with the link in `docs/operations/README.md` and the
  `docs/_archive/ARCHIVED_MANIFEST.md` entry updated to match.

## [1.6.6] - 2026-09-27

Feature release (16 commits since v1.6.5). The default review stack is
`codex_gate,kimi_gate`: subscription readers first, `glm_gate` and
`deepseek_gate` run only as takeover-chain fallback when both are
unavailable (#1928). Gemini is retired as a reviewer everywhere a review
gate is selected — default stacks, profiles, enforcement yaml, the
dashboard toggle, docs and the T0 role — while an existing `gemini_review`
result stays readable in history (#1932, #1934, #1935, #1936). Every
harness-lane review gate now has its own diff-char cap instead of one
shared 50000-char constant: kimi 400000, glm 50000, deepseek 50000
(OI-1874, #1937). A PASS booked on a diff the gate could not fully read is
`partial_review`, not a pass, and the merge door refuses it (OI-1851,
#1920).

Behaviour change for projects: a project that already sets its own review
stack or takeover chain keeps it unchanged. A project whose stack or chain
names `gemini_review` must remove it — the name is refused everywhere a
review gate is selected, though an existing `gemini_review` record in
history still reads as a verdict.

### Added

- **`vnx objective unmark-delivery <track> <pr> [<pr> ...] --reason ...`
  (OI-1872, #1925).** `link-pr` always writes a `track_pr_delivery` row (default
  `partial`, fail-closed) and nothing could take one back, so a PR could
  never return to unmarked. The new verb deletes the row for each PR on that
  track and project, leaves `pr_ref` alone, and prints which PRs had a marker
  and which had none. Like `unlink-pr` it requires a non-empty `--reason` and
  records a `track_delivery_unmarked` audit event; a run that removes nothing
  writes no event. On a store without migration 0032 it says the table is
  absent instead of failing. Available as `vnx horizon unmark-delivery` and
  through the `vnx objective` alias.
- **T0 context rotation is enforced at 500K, not just recommended (#1923).**
  A PreToolUse/Stop/UserPromptSubmit guard hook
  (`scripts/hooks/t0_context_guard.py`) and `scripts/lib/t0_context_budget.py`
  measure a T0 session's context budget; crossing 500K lets work in flight
  finish, starts nothing new, and carries a running `/goal` over to the next
  T0 through `scripts/t0_rotate_spawn.sh` and
  `scripts/lib/t0_rotation_state.py`. The pending marker and latch write
  through `atomic_io.atomic_write_json` and change only on a transition, so a
  repeat crossing in the same band leaves the file byte for byte untouched.
  Docs: `docs/operations/T0_CONTEXT_ROTATION.md`.
- **A placebo arm gives the adoption metric a negative control (#1883).**
  `pattern_injection_outcome.used` decides adoption from a 25% token-overlap
  threshold between a pattern's content and a worker's report, which cannot
  tell "worked on the same topic" apart from "actually applied the pattern".
  `VNX_INTEL_PLACEBO_ARM` (default off) makes `intelligence_selector` inject,
  instead of the real candidate, the offered pattern with the lowest token
  overlap against the dispatch context — tie-broken to a different scope-tag
  group, and never a direct-injection class (code anchors, ADRs, scout
  sketches), since those share the dispatch topic by definition. Migration
  v32 adds `ab_arm` to `pattern_injection_outcome` and
  `dispatch_pattern_offered`; `intel_injection_join.py placebo` compares the
  adoption rate per arm side by side. Build-out only: the flag defaults off
  and nothing runs differently until an operator turns it on.
- **Each harness-lane review gate has its own diff-char cap (OI-1874,
  #1937).** `MAX_DIFF_CHARS` was one fixed 50000-char constant shared by
  `glm_gate`, `kimi_gate` and `deepseek_gate`, dating from the first kimi
  gate and never chosen per model. `gate_lane_contract.max_diff_chars(gate)`
  is now the one resolver, read at runtime through `config_runtime.get()`:
  kimi 400000 (its 1M-token context and kimi-CLI-OAuth subscription cost
  nothing extra), glm 50000 and deepseek 50000 (API-credit fallback seats,
  kept conservative). A missing or invalid config value falls back to that
  gate's own default, never to unlimited; an unknown gate name gets the
  conservative 50000. `kimi_gate.py`, `glm_gate.py`, `gate_runner.py`'s
  harness-lane path and `gate_reanchor_cli.py`'s offline hash recompute all
  read the same resolver; `MAX_DIFF_CHARS` itself is gone, no alias left
  behind.

### Changed

- **The default review stack is `codex_gate,kimi_gate`: subscription reviewers
  first, API-credit reviewers only as a fallback (#1928).** Operator decision of
  2026-09-26. `VNX_DEFAULT_REVIEW_STACK` was `codex_gate,glm_gate` (#1852), which
  made glm (OpenRouter credit) a standing second seat on every PR. codex (codex
  CLI) and kimi (kimi CLI OAuth) both run on a subscription. glm and deepseek
  only read a PR when both are unavailable, which is what the takeover chain
  `codex_gate,kimi_gate,glm_gate,deepseek_gate` already does and keeps doing.
  `gate_recorder.GATE_BILLING` classifies every registered gate as
  subscription, provider-metered or model-free, and a guard test fails when the
  default stack or the takeover chain puts an API-credit gate before a
  subscription gate. The obligation the door declares follows the same order:
  of the full-diff seats in a stack, `_primary_review_gate` now prefers a
  subscription gate over an API-credit gate when they tie on weight, so
  `glm_gate,codex_gate` declares `codex_gate`. A stack with a single full-diff
  gate (mission-control: `glm_gate,claude_github_optional`) resolves as before.
  A project that set its own stack or chain keeps it; `vnx gate --only kimi`
  (and `glm`, `deepseek`) now resolve to the gate names. kimi_gate has run
  through `gate_runner`'s harness lane since v1.6.2 (#1837), so it no longer
  books `not_executable` / `gate_not_subprocess_routable`. The records of that
  kind that remain in the stores were written by an engine older than that
  version. Tests now pin the path from `vnx gate --only kimi_gate` to a booked
  pass, and the merge door's acceptance of it. The PR readiness hint for a PR
  with no review obligation lists codex_gate and kimi_gate first and names
  glm_gate last, as the fallback. It used to offer glm_gate ahead of codex_gate.
- **Docs and defaults name sonnet as the standard build worker, not kimi-k3
  (#1930, #1935).** Operator decision of 2026-09-23, confirmed 2026-09-26: the
  routing code already pinned T1/T2/T3 to sonnet by default
  (`workers-kimi-pinned`, `pin_semantics: default`); this brought the text in
  line. `DISPATCH_RULES.md`, `PROVIDER_LANES.md`, the `fabric-reference` and
  `t0-orchestrator` skills, `.vnx/vnx_workers.default.yaml`, the
  `worker_registry` fallback, `pool_worker_runner`, the headless dispatch
  daemon's `VNX_DISPATCH_MODEL` default, `canonical_state_views` and
  `dispatch_prepare` all now read sonnet where they read kimi-k3 before.
  kimi-k3 stays an explicit, available choice. The protected
  `.claude/terminals/T0/role-orchestrator.md` and the two skill copies inside
  `.claude/` needed a separate operator-applied edit (#1935), since a worker
  may not write those paths.

### Removed

- **Gemini is no longer a reviewer (operator decision 2026-09-26; #1932,
  #1934, #1935, #1936).** `Gate.GEMINI_REVIEW` is out of the closed
  gate-name enum, so every reader that picks a review gate — dispatch spec
  Rule 16, the staging bridge, the takeover-chain parser, the primary-seat
  pick in `smart_router` — refuses it. It survives once, as
  `dispatch_spec.RETIRED_GATE_NAMES`, so a refusal names the reason instead
  of reading like a typo, and `closure_verifier` still reads an existing
  `gemini_review` result as a verdict rather than "not implemented".
  `review_gate_manager` drops a retired name from a stale project's own
  stack with a warning. Also dropped: gemini from default governance
  profiles and enforcement yaml, `auto_gate_trigger`, the auto-merge policy,
  stop conditions, plan-gate enforcement, `vnx gate --only gemini`, and the
  dashboard gate toggle (#1936). Docs, skills copies, `PROVIDER_LANES.md`,
  gate contracts, the README, an ADR-008 amendment and the T0 role file
  follow the same decision (#1934, #1935, #1929). The gemini **worker** lane
  (`gemini_cli` provider, `claude-gemini` profile) is untouched — this is
  the reviewer role only.

### Fixed

- **A takeover no longer requests the same reader twice (#1933).** With the stack
  `codex_gate,kimi_gate` and codex at its limit, the codex seat is taken over by
  kimi and the second seat names kimi as well. `request_reviews` requested it
  again, which rewrote the request record without the takeover path and made the
  executor run the same reader twice. It also ran glm twice when codex and kimi
  were both exhausted. A seat that resolves to a gate an earlier seat of the same
  round already requested now requests nothing, and the first request keeps its
  takeover path.
- **`vnx objective close --attest --pr` no longer replaces `pr_ref`
  (OI-1872, #1925).** On track `absence-is-loud` a close with `--pr 1922 --pr 1924`
  cut `pr_ref` from 17 refs to `#1922,#1924`, and the 17 earlier refs were gone
  from the record. `--pr` now appends to the existing refs with the same merge
  `link-pr` uses (deduplicated, order kept). The `ops-attest:<date>` fail-open
  without `--pr` also keeps the existing refs and adds the stamp, and an
  earlier `ops-attest` stamp survives a later `--pr`. The audit event carries
  `pr_ref_before`. The help text says what happens to the existing list.
- **`vnx objective unlink-pr` no longer leaves an orphan delivery marker
  (OI-1872, #1925).** It now deletes the `track_pr_delivery` row of every PR it
  removes from `pr_ref`, in the same transaction as the `pr_ref` update. The
  output and the `track_pr_unlinked` event list the markers removed.
- **`is_available()` measures reachability, not presence (OI-1454, #1924).** On
  2026-08-23 three of four reader adapters reported available while none could
  answer a call: codex had its quota spent and litellm's key was rejected, but
  both were on PATH and importable. A fallback that chose on that picked a dead
  seat with full confidence. `scripts/lib/provider_reachability.py` now keeps one
  record per provider with three states: `reachable`, `unreachable` with a reason
  (`quota_exhausted`, `insufficient_balance`, `auth_401`, `not_present`) and
  `unmeasured`. The record is written by outcomes the fabric already sees (gate
  results, provider-lane dispatches, codex and litellm adapter calls) and expires
  on its own, so no paid call is made per check. Classifier providers only read
  it. `unmeasured` is never reported as
  `reachable`. Adapters and classifier providers split `is_present()` from
  `is_available()`. The review-gate takeover walk and the smart router's
  `lane_available` skip a provider recorded unreachable and name the reason,
  instead of asking a seat that cannot give a verdict. OI-1507's second half, a
  check that a running process holds a different key than the one on disk, is not
  part of this change.
- **A PASS booked on a diff the gate could not fully read is
  `partial_review`, and the merge door refuses it (OI-1851, #1920).** PR
  #1915 had a 176,860-char diff against a 50,000-char cap; `glm_gate` booked
  `completed` with 0 blocking findings while its own residual_risk said the
  `pr_merge.py` part was cut off, and the record read `diff_chars: 0,
  diff_truncated: false`. Two full reviews then found a real blocker in the
  part glm never saw; the same happened on #1916 and #1917. The record now
  carries the real diff size on every mode (`gate_depth.diff_coverage`), and
  a booked result outside PASS/FAIL — `partial_review` — closes only on a
  measured re-read: an agentic run with a parsed tool stream and at least
  one file read per cut file. The merge door returns NO-GO with the cut
  size, the cut files and the way out (a peer's full pass on the same head,
  or split the PR); `is_pass`/`has_complete_evidence` refuse any `completed`
  record whose `execution_depth` shows an unread truncation. A fail stays a
  fail, and a diff under the cap behaves exactly as before.

### Documentation

- **Every checkmark in `docs/core/00_VNX_ARCHITECTURE.md` now resolves to a
  generated measurement or is gone (#1922).** The four hand-typed checkmarks
  in the Staging Workflow section, and two "NO ..." bullets (one of them
  wrong: the roadmap autopilot does call `create_dispatch_from_pr` and
  `promote_dispatch` on its own), are replaced by a section rewritten
  against the code: the dispatch door as canonical for the described path,
  and the PR-queue path (`pr_queue_manager.py`, `dispatcher_minimal.sh`)
  with its real popup condition and ADR-025 status.
  `tests/test_architecture_doc_drift.py` now fails when a checkmark glyph
  appears outside the generated sections.
- **ADR-005 is scoped to decisions and transitions, not derived state
  (#1927, #1929).** Three codex gates warned on the same day (OI-1867,
  OI-1870, OI-1871) that a write "must be recorded as NDJSON events in
  `.vnx-data/events/`", stricter than the ADR itself. The amendment states
  the reviewer test — does this write drive a decision recorded in no
  canonical ledger — and exempts re-derivable caches (reachability JSON,
  pending/latch markers, digests) as long as the decision they drive is
  logged elsewhere. Reviewer-role prompts, `STATE_FABRIC.md` and three
  docstrings follow the amended wording. A follow-up (#1929) strikes the
  `incident_log.ndjson` line from the canonical ledger list (no writer
  exists) and updates `FORGE_GATE.md` and `agents/review-gate/CLAUDE.md` to
  name `codex_gate`/`kimi_gate` instead of `glm_gate` as the default-stack
  reviewers.

## [1.6.5] - 2026-09-25

Patch release (2 commits since v1.6.4). The merge door now judges a merge
against the repo of the project it merges into, not the repo it is installed
from. Strictness is set per project (`enforce`, `warn` or `off`), and the CI
check stays as the floor under every setting (OI-1849, #1917). Workers also get
the same report headings on every lane as the validator requires, so consumer
dispatches no longer end on `contract_invalid` (OI-1850, #1918).

Behaviour change: `pr_merge.py` refuses when `VNX_HOME` points at a different
checkout than the door that is running. `VNX_PROJECT_ROOT` names the project
explicitly and is the way to point a door at one. Through the `vnx` shim you
notice nothing, because it unsets `VNX_HOME` before it resolves.

### Fixed

- **A worker is told one thing about its report headings, on every lane
  (OI-1850, #1918).** The validator wants `## Summary`, `## Changes`,
  `## Verification` and `## Open Items`. The fabric prompt
  (`prompts/base_worker.md`) asked for "What changed", commands, test totals
  and "Known limitations", and workers followed the prompt: 175
  `contract_invalid` receipts in mission-control, probe D-86244666. The
  directive that states the real headings was only appended by
  `dispatch_prepare.prepare`, so the headless lane, the provider lanes and the
  terminal-pinned subprocess lane on its default flag never carried it.
  `skill_context._inject_skill_context`, the seam every lane's worker prompt
  passes through, now closes the prompt with `report_body_contract.with_directive`
  (once, idempotent, read from the same `_REQUIRED_SECTIONS` the validator
  checks), and the fallback prompts of the headless and provider lanes carry it
  too. `base_worker.md` names no heading list of its own any more: it points at
  the directive and says which heading each kind of content goes under. The
  directive now also asks for the identity block (`**Dispatch-ID**`,
  `**Model**`, `**Provider**`), because a dispatch report without a real model
  never becomes a receipt. A role file that lists other report headings makes
  the door log a warning with its path and the differing headings; the
  directive stands after the role text and wins, nothing is blocked. Gate
  reviewers answer with a verdict and stay directive-free.
- **The merge door judges the project it merges into, not the repo it is
  installed from (OI-1849, #1917).** Run from a central install with a consumer
  project as the target, `pr_merge.py` read the YAML on main, the YAML on the PR
  head, the live branch protection and the ADR numbers from
  `Vinix24/vnx-orchestration`, while `gh pr merge` and the CI gate went to the
  directory the caller stood in. A consumer merge got "no drift" about a repo it
  was not merging into. The target is now decided once before the first gate
  (`VNX_PROJECT_ROOT`, else the git toplevel of the cwd, through `vnx_paths`),
  named on the first line of the output (`doelrepo: owner/name`, on stderr under
  `--json` and as `target_repo` in the object), and every git and gh call of the
  merge runs in it. A target that cannot be named is a refusal. The running
  door is proven against the fabric instead: `main` for a checkout, the tag
  `v<VERSION>` for a central install (`scripts/lib/merge_target.py`, now one of
  the hashed door files). `apply_branch_protection.py` and `vnx doctor` use the
  same project root and reading order; a bare `apply_branch_protection.py` from
  an install no longer applies the fabric's YAML to whichever repo the install's
  remote names.

- **A PR cannot lower the CI gate through `ci_workflow` (OI-1849, #1917).** The
  CI gate asks main's `ci_workflow` of every later PR, so a PR that changed,
  added or removed it chose the check it was judged on next time, and the
  preflight said "PR verzwakt niets". `forge_protection_drift.is_weakening` now judges it
  and any change needs `--allow-weaken "<reason>"`; the message names the old
  and the new value. `pre_merge_gate` and the `merge_preflight_ci_check` CLI
  read it from main of the target repo over the contents API
  (`fetch_ci_workflow_from_main`, one reader shared with the door), not from the
  checkout, which at gate time is the PR's own branch. `apply_branch_protection.py`
  refuses a central install as its project, with the test the door already used
  (`merge_target.ensure_project_is_not_the_install`).

### Added

- **Strictness per project (OI-1849, #1917).** `branch_protection.yaml` accepts
  `enforcement: enforce | warn | off` (absent means `enforce`) and
  `ci_workflow: "<name>"`. The file is `.vnx/branch_protection.yaml`, else
  `scripts/forge/branch_protection.yaml`. `warn` reports drift and merges, `off`
  does not check, and the preflight record carries `mode` and a `reason_code` so a
  GO that was not a check reads differently from one that was. A project with no
  file is treated as `warn`, loudly, replacing the bootstrap GO. Lowering the
  enforcement is a weakening and needs `--allow-weaken`. The CI gate is not
  softened by any of it, and looks for the project's own workflow name (both
  resolvers: explicit argument, project YAML, `VNX_CI_WORKFLOW_NAME`, `VNX CI`).
  Only the reader lands here: the fabric's own YAML does not carry the fields
  yet (two-PR contract, `docs/operations/FORGE_GATE.md` §10 and §11).

## [1.6.4] - 2026-09-24

Patch release (3 commits since v1.6.3). A consumer dispatch no longer books
successful work as failed. Two causes are fixed: the receipt's warning counter
no longer writes into the read-only install (OI-1788, #1912), and the salvage
step no longer fails a dispatch whose PR was already delivered (OI-1846,
#1913). The only other change is a research document on subagent
observability (#1910).

### Added

- **Documentation (#1910).** A read-only study of what Claude Code 2.1.281
  offers for subagent observability (hooks, transcripts, OTel, isolation),
  measured against 70 subagents in one SEOcrawler_v2 T0 session and against the
  headless lane per property, in
  `claudedocs/2026-09-24-subagent-observability-onderzoek.md`. None of the 70
  carried a dispatch id, report or receipt while 67 pushed code. Three options
  are laid out (forbid subagents for governed work, read-only research with a
  receipt via a hook, replace the headless lane) and nine points stay open with
  the measurement that decides each. Analysis only: no code changed.

### Fixed

- **A finished dispatch no longer fails because of its own cleanup (OI-1846,
  #1913).** Three defects in the post-dispatch salvage (`pr_enforcement`), measured in
  sales-copilot where two dispatches whose PR already existed ended on
  `failure`. `git push` and every `gh` call now run from the dispatch worktree
  when it is known, not from the main checkout, so the operator's pre-push
  hooks no longer time the push out at 120 s. A worktree that is dirty only
  through the generated `FEATURE_PLAN.md` is not forgotten work: it is never
  salvage-committed, and its real commit state (unpushed worker commits, an
  existing PR) is enforced instead. `write_feature_plan` also stops writing
  inside a dispatch worktree, which is where the regen dirtied a tracked copy.
  When the salvage push fails after the worker had already pushed with a PR,
  the outcome stays a success and a `pr_enforcement_warning` receipt records
  the reason. `classify_path` gained `ignore_working_tree` for the commit-state
  read.
- **A consumer dispatch no longer books "failed" for work that succeeded
  because the receipt's warning counter sat in the read-only install (OI-1788,
  #1912).**
  On mission-control (v1.6.3, probe D-615f4dc5) `envelope._govern` logged
  `VNX_RECEIPT_EMIT_FAILURE work_status=success: Failed to acquire append lock:
  [Errno 13] Permission denied: '~/.vnx-system/versions/v1.6.3/.vnx-data'`,
  75 times since 2026-09-15. The lock was never the problem: `spec.state_dir`
  was the project's central store and the ledger path was right. The failing
  call was the receipt's pre-write hook. A `warnings[]` entry that resolves to
  `counted` (every worker report that misses a contract heading stamps one)
  bumps a recurrence counter, and `warning_destination._default_counter_path`
  built that path with `resolve_state_dir(__file__)`. Inside an install that is
  the version directory (`dr-xr-xr-x`), so the counter's `mkdir` raised. Both
  receipt writers (`governance_emit.emit_dispatch_receipt` and
  `append_receipt_payload`) now keep the counter beside the ledger they append
  to. The no-ledger default and `session_resolver`'s fallback use
  `vnx_paths.resolve_state_dir()`, where an explicit `VNX_STATE_DIR` still
  wins. Both files left the grandfather list of the central-mode path gate.
  `_write_receipt_under_lock` also stopped reporting every `OSError` from the
  pre-write hook as "Failed to acquire append lock": that label sent the first
  diagnosis after the lock path while the lock was held. It now says
  `pre_write_hook_failed` and names the real path.

## [1.6.3] - 2026-09-24

Patch release (49 commits since v1.6.2). Headline: consumers no longer stall
on an exhausted codex quota. The dispatch door declares the reviewer from
`VNX_DEFAULT_REVIEW_STACK` instead of a hardcoded `codex_gate` (#1872), and a
provider quota refusal counts as a reviewer being absent, not as a gate cause
that trips E6 (#1863). Around that sit the absence-is-loud run (beacons,
receipt gaps, quarantine, parked components, launchd drivers), a T0 that
blocks subagents, and the removal of the tmux dispatch lane.

### Added

- **T0 blocks subagents and warns when its role is not loaded (#1907).** A
  PreToolUse hook (`scripts/hooks/pretooluse_t0_no_subagents.py`) denies the
  `Agent` and `Task` tools in a T0 session and points to `vnx dispatch`. T0 is
  recognised by the session cwd or by the launch directory encoded in
  `transcript_path`. Workers and every other tool are untouched. The hook ships
  through the settings templates. SessionStart puts `ROLE NOT LOADED` at the top
  of the T0 injection when the terminal `CLAUDE.md` does not import the
  canonical role, measured with `fleet_role_drift --reach`. The rule used to
  live only in the role file: one consumer project logged 280 `Agent` calls in
  13 T0 transcripts.
- **An explained receipt gap can be acknowledged (#1897).**
  `ledger_health.py acknowledge --dispatch-id ID --reason "..."` appends to
  `<state_dir>/ledger_coverage_acknowledged.ndjson`. `receipt_coverage`
  subtracts acknowledged ids and stays `finding` only for an unacknowledged
  gap, so the check can go green and a new gap is visible again. A blank reason
  is refused. An unreadable acknowledgement file reads as unverified and
  silences nothing. The two lane events that reuse `dispatch_id` for a lane
  label no longer count as gaps.
- **One parked register, with a reason per item (#1900, #1904).**
  `beacon_register` gains `PARKED_ARTIFACTS` and `PARKED_LAUNCHD_JOBS` next to
  `PARKED_COMPONENTS`. The intelligence layer was parked on 2026-09-09 but only
  the beacon reader knew, so two other readers reported the consequence as a
  fault. SessionStart now reads a parked `t0_recommendations.json` as
  `[PARKED] <name>: age N days` plus the reason instead of STALE and BLOCKED.
  `launchd_liveness` reads the three never-installed jobs (nightly intelligence
  pipeline, receipt classifier batch, F41 headless trigger) as `parked` and no
  longer pins `overall` on fail. The `intelligence-self-learning-loop` beacon
  joined the register on 2026-09-24. Parking covers absence only: a fresh
  artifact reads fresh, a beacon that says `status: fail` stays `fail`, and a
  parked job that is loaded and exiting non-zero stays `loaded` with its exit
  status.
- **Three launchd drivers for producers that only ran by hand (#1900, #1902).**
  Templates under `scripts/launchd/`, installed with `reload_plist.sh`.
  `com.vnx.dashboard-generator` keeps `generate_valid_dashboard.sh` running (it
  only ran as a child of `vnx start`, so `dashboard_status.json` was 88 days
  old). `com.vnx.fleet-role-drift` runs `fleet_role_drift.py --write-state`
  every 6 hours. `com.vnx.subsystem-probe` runs
  `python3 -m vnx_cli.main subsystems --probe` every 6 hours and once on
  install. The `governance-enforcement-stack` and `plan-gate-panel` beacons had
  read stale from 12 July to 24 September, because
  `subsystem_health.aggregate()` only ran when someone typed the command.
  Install the probe driver from the primary checkout: the probes read the
  git-ignored `.vnx-attest/` ledger of the checkout they run from, so a
  worktree measures nothing and writes no beacon.
- **Gate findings carry a structured address (#1859, #1866).** Findings gain a
  `file_path`/`line` address, and every gate gets its findings, not just
  `codex_gate`.
- **The provider registry admits glm-5.3 and glm-5.3-flash (#1855)**, alongside
  glm-5.2.
- **`scripts/intel_injection_join.py` (#1882).** A per-dispatch join of offered,
  used and ignored-with-reason patterns over the injection offers, plus an
  adoption-rate report (used against offered, per pattern and over time).
- **Finding-triage spike scripts (#1884, #1888, #1889).**
  `scripts/spikes/laya_finding_triage/` measures a regex baseline against
  classifier arms (Laya on CoreML and MLX, JEV via OpenRouter) on gate findings.
  Nothing in the runtime imports them.
- **Documentation (#1885, #1905, #1908, #1909).** The smart-router design doc
  gains section 5 on benchmark provenance and the self-learning loop design. The
  tmux clean-up is inventoried (106 files under `scripts/lib`, 71 files outside
  it and 9 daemons) and planned in 23 slices over four phases, in
  `claudedocs/2026-09-24-tmux-inventaris-A-scripts-lib.md`,
  `claudedocs/2026-09-24-tmux-inventaris-B-rest-en-daemons.md` and
  `claudedocs/2026-09-24-tmux-opruimplan.md`. The plan keeps the tmux injection
  path into interactive kimi, codex and gemini terminals and removes the old
  claude-pane architecture. Analysis only: no code changed.

### Changed

- **Build-workers default to sonnet and T0 is pinned to Opus 5.5 (#1890).**
  Operator decisions of 2026-09-23. A claude-lane build-worker dispatch without
  an explicit model now resolves to sonnet and passes. Before it resolved to
  kimi-k3 and was rejected by `kimi-via-cli-only`. kimi-k3 stays an explicit
  per-dispatch choice. `opus-5-5` (`claude-opus-5-5`) is registered and the
  `t0-opus-only` pin points at it. `opus-5` stays registered for the side-by-side
  measurement. The constraint id stays `workers-kimi-pinned` because the pin
  loader keys on that exact string.
- **The default review stack is `codex_gate,glm_gate` (#1852).** The door now
  follows the configured stack (#1872, see Fixed).
- **README and CHANGELOG aligned with the code at v1.6.2 (#1877).** The README
  no longer mentions the removed tmux dispatch lane, opens with subscription
  billing next to governance, and carries re-measured numbers.
- **`vnx init` help text matches the repin gate (#1878).** Repinning an
  initialised project takes `--force --set-version`. `--set-version` alone still
  aborts. Text only, no behaviour change.

### Fixed

- **The review obligation follows the configured review stack, and a quota
  refusal no longer trips E6 (#1872, #1863).** The door filled a silent
  `spec.gate` from a hardcoded table that named `codex_gate` for the default and
  coding-strict variants and never read `VNX_DEFAULT_REVIEW_STACK`. On
  2026-09-19 in mission-control the project config carried
  `glm_gate,claude_github_optional`. Every new obligation still named
  `codex_gate`, and five PRs sat for hours on an exhausted codex quota while the
  glm gate the project had configured had already approved all five. The door now
  declares the heaviest seat of the configured stack, so a project that never
  changed its stack sees no difference. An unreadable, empty or unknown stack is
  refused with `gate-config-unreadable` instead of falling back to a default.
  `deepseek_gate` is now in the one registry of legal gate names. On the other
  side `gate_recorder` booked a provider quota refusal as `exit_nonzero`, so
  `stop_conditions` read three of them as one systemic cause and E6 halted every
  dispatch in mission-control on 2026-09-17. The refusal is now classified once,
  at write time, and booked as a quota refusal. E6 excludes it from its streak
  the way it already excludes `gate_runner_missing`, with its own loud notice.
  The threshold is unchanged and E6 still triggers on any cause the fabric owns.
  Together the two lift the E6 block that left consumers stalled on a codex
  quota.
- **The SessionStart hook finds the central store from a consumer project
  (#1906).** The v1.6.2 hook located `vnx_paths.py` at `$_HOOK_DIR/../scripts`,
  which is the repo's own `scripts/` only in the fabric repo. A consumer's
  deployed hook sits in `<project>/.claude/hooks/`, so it looked in a
  `.claude/scripts/` that does not exist. It reported "VNX STATE STORE NOT FOUND"
  with terminal states, open items, receipts, beacon health, producer freshness
  and state freshness all UNMEASURED. The hook now picks one scripts root, once:
  `../scripts` when it holds `lib/vnx_paths.py`, else `$VNX_HOME/scripts`, else
  `~/.vnx-system/current/scripts`. `resolve_paths()` stays the only resolver.
  When no root exists the UNMEASURED line names the three places tried. A
  consumer gets the fix when its hook is redeployed (`bootstrap_hooks`).
- **Hook hygiene (#1894, #1891).** The UserPromptSubmit fallback in the settings
  template ended on `echo '{"decision": "allow"}'`. It fires wherever `$PWD` does
  not end in T0 to T3, so in every worktree, and the literal text reached every
  worker's context (OI-1816). The fallback is now `exit 0`, the two inject scripts
  exit 0 with empty stdout on their no-op paths, and context goes out as
  `hookSpecificOutput.additionalContext`. A new test holds every settings
  template to "no top-level decision". Separately, a path fragment inside a word
  is no longer read as an absolute path: `/hooks/build_t0_state_hook.sh` inside a
  printf error text was reported as a dead absolute pin in every T0 session.
- **Beacon readers expect what the writers do (#1896).** t0_state and the
  dashboard health API now pass the parked component names, so `learning_loop`
  and `intelligence_daemon` no longer read `stale` where SessionStart says
  `parked`. An event-driven writer (`expected_interval_seconds=None`) owes no
  beacon between two events, so its silence no longer reads `absent` and counts
  as fail. A beacon on disk with `status: fail` stays `fail`.
  `fleet_role_drift --write-state` writes its beacon to `<data_dir>/health/`
  where the readers look.
- **A report refused for its model is quarantined, and the refusal stays loud
  for 24 hours (#1898, #1901).** The converter refused a report with no real
  model and left it in `unified_reports/`. Every scan retried it, so
  `report_to_receipt_converter` stayed at fail and 15 reports hid the next new
  refusal behind an alarm that was already on. `missing_model` and
  `invalid_model_shape` (a `**Model:** glm-5.2 · **Provider:** claude` line
  made the parser read the rest of the line as the model) now move to
  `receipt_deadletter/` with an INDEX line. Nothing is deleted. Both beacons
  stay at fail while a refusal younger than 24 hours is in the history, then
  return to ok with the history kept in `details.rejected`.
  `receipt_conversion_rejections` applies the same rule, so the two beacons no
  longer contradict each other on one scan. The fail-closed model check itself
  is unchanged.
- **Lane identity reaches the receipt (#1886, OI-1546, OI-1547).** The
  converter read only the legacy `selected_model` (1 of 765 route decisions) and
  ignored the `decision` block that 764 use. It therefore fell back to the
  report's own claim almost every time. On a harness lane that claim is wrong:
  the CLI says sonnet/claude while the lane runs glm-5.2. The converter now reads
  both shapes and treats "no usable route decision" as an explicit third source.
  Provider prose in the provider field is refused as a visible
  `unrecognized_provider` contract violation.
- **The receipt converter dry-run is side-effect-free and faithful (#1879,
  OI-1744).** A dry run moved reports into `receipt_deadletter/` and wrote
  gate-obligation `pr_link`s. It also counted a report as `would_append` that
  the fail-closed model check would refuse (45 would-book, 0 rejected against 28
  booked and 16 rejected on a copy of the live store). Both are fixed. A
  staleness tripwire test fails when the live converter beacon is older than the
  interval it promises: the converter had stood still for more than 17 days because the
  per-project launchd job of its host loop was never installed for vnx-dev.
- **A successful dispatch writes its own receipt (#1880, OI-1743).** The
  worker run and `_enforce_push_pr` had a `finally` and no `except`, so an
  exception after a successful worker skipped `_govern` and left neither report
  nor receipt. The exception is now turned into a failure result that keeps what
  the worker produced, and `_govern` runs regardless. `run_envelope` has the same
  shape.
- **The effectiveness probes read the central store, and a failed `--probe`
  exits non-zero (#1903).** Without an env pin the probes resolved the
  checkout-local state, a stale copy. On 2026-09-24 the `plan-gate-panel` beacon
  said ok while the live store held 90 unresolved OI-PLAN blockers.
  `vnx subsystems --probe` now hands its data dir to every probe that takes a
  state dir and fails with a message on stderr and exit code 1, instead of
  rendering every row as `unknown` and exiting 0.
- **A test run outside pytest could write the real central store (#1899).** The
  #1333 guard only recognised pytest, so `python -m unittest
  test_auto_commit_stash_isolation` put a `fail` beacon, register events and a
  `runtime_coordination.db` into `~/.vnx-data/vnx-dev`. The guard is now
  `vnx_paths.refuse_real_central_store_write_under_test_runner` (the pytest-only
  name is gone, no alias). It reads the runner itself, a `unittest` frame on any
  thread's call stack, so a production process that merely imports `unittest`
  is untouched. It sits on `state_writer`, `HealthBeacon`,
  `cleanup_worker_exit` and the writers of `intelligence_usage.ndjson`, next to
  the receipt append that already had it. The test that leaked is hermetic now:
  it patched the wrong `SubprocessAdapter`, so each delivery test spawned a real
  `claude -p`.
- **The confidence loop writes outcomes again (#1893).** On 2026-09-23
  `confidence_events` held 1 row (from 13 June) and 239 of 241 dispatch receipts
  with a cqs field read `unknown` in the quality score. `skip_enrichment=True`
  had skipped every post-append hook, the confidence update included. Now only
  the advisory hooks stay behind the flag. The update also resolved its store
  from its own checkout and now writes to the store the receipt went to, and the
  lane's own outcome receipt calls it too. An outcome counts once per project,
  dispatch and outcome. A lane or provider failure (`empty_completion`,
  `credit_exhausted`) is not a signal about the patterns and does not count. A
  report that passes the contract and declares no status becomes `done`, with
  `status_source` on the receipt. The identity block is also read at the bottom
  of a report.
- **Injection outcome rows land (#1882).** `_pattern_usage_signals` subtracted a
  naive `datetime.now()` from an offset-aware `last_offered`. The `TypeError`
  escaped the instrument's own catch, so `pattern_injection_outcome` held 0 rows
  after 6919 injections. The subtraction is tz-aware now.
- **The dream cycle runs as a proposal when the injection probe is degraded
  (#1892).** `com.vnx.auto-dream` ran 55 times since 2026-07-31 and skipped every
  run on `probe_not_ok`, so `dream_cycles` has 0 rows: the gate blocked the
  consolidation that remedies the ignored patterns. A `degraded` probe now lets
  the cycle run with `mode=proposal_only`. Archiving still happens only in
  `review_gate.approve_cycle`, an operator action. `produces_crap`, `unknown` and
  any unrecognised status still skip.
- **Harness-lane failure honesty (#1857, #1858, #1864, #1871, #1874, #1875).** A
  spawn-failure report is refused as `completed`. API-error failures with no
  `failure_reason` are caught. A verdict-block that was never written is
  refused. The shared verdict reader reads a bare verdict object. The verdict
  guard covers `codex_gate`, with the decision taken on the provider. The codex
  booking reads the same verdict the guard reads.
- **Obligation hygiene (#1861, #1862, #1865, #1881).** PRs that carry no
  review-gate obligation are reported loudly. A refused overwrite-guard write
  gets a durable audit line. Terminal obligations whose evidence went stale after
  closing are detected. `gate_obligation_retire_backlog.py` refuses (exit 20)
  when its PR source and its branch source resolve to different repositories,
  when `--project-root` is not a git repository, or when there is no GitHub
  origin. Before, a mixed source would have retired 40 obligations with a
  fabricated `no_pr_branch_gone` reason (OI-1795).
- **The router refuses an unknown `task_class` (#1860)** instead of silently
  routing nothing.
- **A generated MCP config no longer carries a credential (#1895).** `vnx init`
  and `vnx bootstrap-terminals` copied every global server, `env` and all, into
  each terminal `.mcp.json`. Claude Code ignores the `disabled` flag they added,
  so the copy started with frozen keys and outranked `~/.claude.json`. Both now
  share `scripts/lib/mcp_server_config.py` and write a name-only mask
  (`{"command": "true"}`) per global server, derived again on every run. The
  role-scoped worker `--mcp-config` keeps `env` and `headers` only as `${VAR}`
  references, so no literal reaches the process list. The dead
  `scripts/mcp_profile_manager.sh` is removed.

### Removed

- **The tmux dispatch lane (#1868, #1869).**
  `scripts/lib/tmux_interactive_dispatch.py`, the `force_tmux` spec fields and
  flags, and the `VNX_ALLOW_TMUX_LANE` emergency brake are gone.
  `claude_headless` is the only claude lane. Tmux session management
  (`tmux_adapter.py`, `tmux_worktree.py`, the signalling hooks) is untouched. See
  `docs/operations/TMUX_SPAWN_LANE.md`.

## [1.6.2] — 2026-09-12

Patch release (57 commits since v1.6.1). Headline: the gate-evidence
chain was closed end-to-end — gate outcomes are booked as result
receipts, branch protection is declared as code with a summarizing
gate check, the plan-gate reads its seats honestly, and the interactive
tmux dispatch lane was retired.

### Added

- **Gate outcomes are booked, not assumed (#1836, #1846, #1842, #1837,
  #1847)** — every gate outcome lands as a result receipt with verdict,
  provider and model; the harness-lane verdict contract is consolidated
  to one source; harness-lane gates sign with their own dispatch-id; the
  harness-lane strategy delegates glm/kimi to the governed dispatcher;
  and a test binds `MODEL_DEFAULTS` to the provider registry.
- **Branch protection as code and a summarizing gate check (golf B,
  #1807, #1808, #1812, #1814, #1815, #1809, #1820)** — branch protection
  is declared as YAML with apply, a drift preflight and a doctor check; a
  GitHub-App client publishes check-runs with the key from the keychain,
  after the verdict record and under an allowlist and sha states; a
  summarizing `vnx-gate/review` check is derived from the
  closure-verifier rules and moved from `pending_checks` to the required
  checks; the FORGE_GATE runbook and the reader-per-field requirement of
  the branch-protection preflight are documented.
- **Fail-closed merge door (golf B/Bx, #1806, #1824, #1805, #1823)** —
  `contract_invalid` is a fail-closed merge check with its own override
  that works only on its own reason code; an ADR-number preflight runs
  against main with a uniqueness test; and the five preflight verdicts
  land in the ledger, also on a refusal.
- **Receipt processing and worktree cleanup as per-project launchd jobs
  (#1830, #1851)** — the receipt-processor and
  cleanup-reviewed-worktrees run as per-project jobs instead of central
  ones.
- **Beacon reader lifecycle (#1832)** — reader-register, park and dedupe.
- **`vnx objective unlink-pr` (#1821)** — operator-gated, with a receipt
  and tenancy isolation.

### Changed

- **The interactive tmux dispatch lane is retired (#1845, #1803)** —
  `force_tmux` is refused at validation and `claude_headless` is the
  only claude lane (the lane itself was removed after this release, in
  #1868); plan-gate claude panel seats now run on the headless lane
  because tmux delivery lost the prompt.
- **Update and publish cutover guards (#1840, #1841, #1825)** — publish
  and `update --to <tag>` work repeatedly; the cutover names its
  behindness and refuses stale targets; and `vnx doctor`'s install:mode
  warns when the pin differs from the active version.

### Fixed

- **Harness-lane verdict integrity (#1834, #1848, #1835)** — spawn-layer
  diagnosis surfaces in the report body, the glm gate `failure_reason`
  and the unified report; and a runner refusal no longer overwrites a
  provider outcome.
- **Obligation evidence lifecycle (#1844, #1843, #1839, #1838, #1810,
  #1811, #1804, #1819)** — terminally parked obligations with missing
  evidence, and obligations whose evidence file vanished, reopen as a
  valid reason; the merge door reads the takeover booking from the
  obligation as a third evidence route; availability is measured via
  registration and child, not file existence; gate evidence binds to the
  head so an old verdict does not block a fresh one; a routing notice is
  not a verdict and does not hold the gate closed; takeover fields are
  order-independent on the result; and a takeover record without a
  verdict no longer displaces the independent signer.
- **The plan-gate reads its seats honestly (#1799, #1801, #1800, #1796)**
  — empty and unreadable seats are distinguished, verdicts are
  re-extracted, and the tiebreaker runs only after a read round; the
  tiebreaker outcome lands in `decision_ref` and backfill recognises
  tiebreak reports; `execution_depth` is recorded on every lane so a
  verdict without investigation is no verdict; and a test pins the three
  signer classes per Gate value.
- **Stop conditions and hooks (#1828, #1827, #1795, #1816, #1826,
  #1817)** — the door's blocking verdict is scoped to this dispatch's own
  provider/gate; `gate_runner_missing` no longer counts toward E6's
  repeat streak; the worker-scope hook emits the `hookSpecificOutput`
  deny form; the t0_state hook no longer hangs on a `VNX_HOME` a
  SessionStart hook never inherits; the dead SessionStart matcher is out
  of the three install templates with a matcher-watcher in
  t0_state_health; and the gate-runner plist gains `~/.local/bin` with
  the `provider_not_installed` cleanup extended.
- **Dispatch door and prompt safety (#1798, #1797)** — the door refuses
  the fire when the gate obligation cannot be registered, with
  bookkeeping failures reaching stderr and the receipt; and
  glm/kimi/codex/gemini review prompts carry the untrusted-diff
  sandwich.
- **Worktree, forge and gate-prompt correctness (#1833, #1850, #1829,
  #1813, #1822, #1818, #1802)** — the git common dir is resolved instead
  of assuming `.git` is a directory; the review-gate dispatch worktree is
  reset at teardown; `publish_check_run` refuses to post in test mode
  without opt-in; a multiline keychain secret is hex-decoded; the PR
  number comes from the squash suffix, not the first hash-number; the
  neutralization of json-fences is reported as a rendering; and the
  headless worker gets the absolute report path while govern adopts a
  stray worker report before synthesising.
- **Docs (#1831)** — the triage report for the augustus-blockers (golf C,
  OI-1259 through OI-1580) is recorded.

## [1.6.1] — 2026-09-06

- **The nightly digest sender resolves its own SMTP credential (#1751,
  2026-09-04)** — `send_digest_email.py` falls back to the macOS keychain
  item `vnx-smtp-pass` via `_read_smtp_pass_from_keychain()` when
  `VNX_SMTP_PASS` is empty, and fails loudly when both are empty.
- **Phase 11 of the nightly pipeline no longer pre-empts that fallback** —
  the phase gate checked `VNX_SMTP_PASS` itself, so removing the variable
  from the operator's shell profile (the point of the keychain fallback)
  made phase 11 skip outright instead of reaching the sender.

## [1.6.0] — 2026-08-20

Minor release since v1.5.0. The user-visible shape: a review gate can no
longer be skipped — an ungated write or an ungated merge is now refused
outright — the door refuses more before it ever spawns a worker (a
known-rejected lane, a local-path project_id, a constraint-blocked model),
failure classification reads structured signals (a bisect ref, tmux's real
stderr, a measured length floor) instead of guessing from prose, dispatch
report wrappers carry a machine-readable identity block, and CI no longer
stalls on an external apt mirror.

### Added

- **A review gate can no longer be skipped (#1588, #1589, #1590, #1591)** —
  a writing dispatch with an empty gate, and a merge without a gate
  verdict, are now refused outright instead of merging silently — closing
  the gap where 95 PRs merged over four days with zero review-gate runs.
  The CI gate now binds to the PR head instead of falling back to whatever
  is checked out locally, `ci_green_required` resolves to the actual
  `ci_gate` check instead of an unrecognized gate name, and a merge is
  pinned to the exact head SHA the gates approved so a late push during
  the auto-merge wait can't slip an unreviewed commit through.
- **The door refuses earlier and louder (#1586, #1584, #1587, #1600,
  #1603)** — a dispatch now refuses to fire on a lane with a recent
  quota/auth rejection before any worker spawns, instead of firing blind
  into the same dead lane; a local filesystem git origin (e.g. a
  release-time temp checkout) can no longer resolve as a project_id; a
  constraint-blocked model (a deprecated GLM variant) in the routing
  recommendations is now a hard load-time error instead of a silent
  filter; and the `irreversible` flag and per-path `access` (read/write) a
  dispatch declares now actually reach the router and the worker-scope
  enforcement instead of dying partway down the door path.
- **The dispatch report wrapper is machine-readable (D1+D2, #1619)** — the
  generic report wrapper used to paste the full dispatch instruction under
  `## Instruction`, which broke parsing for plan-gate seats (the
  instruction itself carries a verdict-template fence the parser then
  mistook for the seat's answer). The instruction echo is gone, and the
  wrapper now stamps its own bold `**Model**`/`**Provider**` identity
  block instead of a bullet list, so a wrapper report keeps a recoverable
  identity even without frontmatter.

### Fixed

- **Failure classification is measured, not guessed (#1585, #1604, #1605,
  #1606, #1607, #1609, #1617)** — a deep-analysis digest now distinguishes
  "nothing was tried" from "every attempt failed" instead of collapsing
  both to a bare zero; a near-empty completion (17 characters) is
  classified `empty_completion` instead of falling to `unknown` and
  missing its retry; ambiguous quota-or-auth error strings are told
  apart; provider pricing now carries a `price_source`/`price_checked_at`
  pair so a stale inherited price is visible; model-name resolution for
  cost lookups is a deterministic three-tier match instead of a
  dict-order guess that could fabricate a price; a regression's first-bad
  commit is read structurally off the bisect ref instead of parsed from
  git's version-dependent prose; and the orphan-sweep tmux session check
  now recognizes tmux's real "no socket" stderr instead of a stubbed
  string, so it no longer skips a worktree reap on an unmeasurable
  listing.
- **CI is more robust (#1608, #1616, #1622)** — the ripgrep install step no
  longer runs `apt-get` against an external Ubuntu mirror that had twice
  stalled the pipeline for hours; it is now a sha256-verified static
  binary download, with every CI job carrying a timeout as a backstop.
  The `ci_gate` check now speaks gh 2.76.2's current `pr-checks` JSON
  schema (`name,bucket,state`) instead of the retired `status`/
  `conclusion` fields, which had gone silently unrecognized and stalled
  the gate at "running" forever.
- **A quota/auth failure marks its lane out for the next dispatch
  (#1592)** — `record_lane_failure` had no production caller, so a lane
  that hit a quota or auth failure was never marked unavailable and the
  next dispatch could re-enter the same dead lane, measured as 9
  dispatches dying on the same kimi 403 within eight minutes. The
  cooldown write now happens where the failure is actually observed, in
  the provider-lane governance emit, gated on the two unambiguous
  provider-failure classes.
- **PR enforcement resolves the branch that was actually pushed (#1623)**
  — PR enforcement only ever checked `dispatch/<id>` for a push and a PR.
  A dispatch instruction that prescribes its own branch name made the
  worker push a different branch, so the guard found nothing and booked a
  completed, PR'd dispatch as `failure`. It now resolves the worktree's
  actual checked-out branch for both the tmux and envelope lanes.
- **Docstring typo corrected (#1498)** — a duplicated word ("Light light
  caps decisions at 5") in a module docstring is fixed to match the
  sibling item's phrasing.

## [1.5.0] — 2026-08-16

Minor release since v1.4.7. The user-visible shape: the plan-gate
now judges a track on its goal AND its deliverables and refuses a
deliverable-less track loud, new CLI verbs manage deliverables and track
goals directly, the provider cost ladder is an ordered tier list with
quality escalation, and worker permissions and roles fail loud instead of
resolving to a silent default.

### Added

- **`vnx deliverable close` and `vnx objective set-goal` (#1553, #1547)** —
  `vnx deliverable close` settles (afboekt) a ready deliverable from the
  CLI instead of hand-editing the tracks DB. `vnx objective set-goal` (and its track
  alias) repairs a goal the plan-gate refused as too thin, so a blocked
  track can be unblocked without SQL.
- **Provider cost ladder as ordered tiers with quality escalation (#1540,
  #1544)** — the ladder is no longer a flat list: tiers are ordered, a
  delivery that fails quality escalates to a higher tier, and the
  escalation is wired into the spec receipt.
- **Smart-router tier machinery (#1494, #1500, #1495, #1484, #1486, #1530,
  #1513, #1531)** — registry-driven tier map with a walked fallback chain
  and per-class cooldown; per-tier AUTO-staging with a deterministic
  canary and nulmeting; review-gate weight derived from
  `governance_variant`; the tier-mid/high enum gap closed; kimi, glm and
  deepseek registered as Tier-1 adapters; zai constraint allowlists
  enforced by registry key with a glm-5.2 allowlist (not blocklist);
  auth-derived claude billing with headless passthrough via the pip CLI.
- **Plan-gate panel operations (#1520, #1507, #1504, #1505, #1526)** — a
  stop-rule plus tiebreaker for the panel; panel size derived from the
  governance variant (zwaarte); panel effectiveness measured against the
  first seat (ijkmeting); `backfill-reason` and `reblock` disposition
  verbs; and a batch command over OI-PLAN-blocked tracks.
- **`vnx deliverable set` tags an existing deliverable's `task_class`/
  `routing_floor` (#1562, OI-1560-p2)** — the write-side half of what #1560
  shipped read-only: `vnx deliverable add` gains optional
  `--task-class`/`--routing-floor`, and a new `vnx deliverable set
  <dispatch_id>` verb patches those same two fields onto a deliverable that
  already exists. `task_class` is validated against the smart-router closed
  set (`01_code_generation` .. `07_translation`); `routing_floor` is free
  text. `vnx deliverable list` now surfaces both fields. See
  `docs/core/HORIZON_PLANNING.md`.
- **Escalation ladder climbs from failure_class (#1574)** — the smart-router
  escalation ladder no longer climbs on a bare boolean: `model_error` and
  `credit_exhausted` climb one tier (the latter also notifies the operator),
  `auth_rejected` does not climb (a higher tier has the same auth problem),
  and `timeout`/`empty_completion` retry the same tier once before climbing.
  `stage_escalation_bundle` gains its first production caller in the
  provider-lane failure branch, so `tier_from`/`tier_to`/`parent_dispatch`
  land on the staged follow-up spec and reach the receipt.
- **Blessed merge wrapper gated on VNX CI conclusion (#1575)** — `pr_merge.py`
  refuses a merge unless a VNX CI run with `conclusion=success` exists for
  the exact PR head SHA. A run on an older head does not count, zero runs is
  a refusal, and any unverifiable state is a refusal rather than a silent
  pass. `--override-reason` (or `VNX_MERGE_OVERRIDE_REASON`) is the single
  visible escape hatch, and an empty reason is refused.
- **Blocked review state split into unread vs refused (#1576)** — the single
  `blocked` state hid two facts under one name: tracks the plan-gate never
  reviewed (queue depth) and tracks it reviewed and refused. The disposition
  is now derived from the open OI-PLAN blocker plus the durable
  `tracks.decision_ref`, so it cannot drift from the gate records.
  `vnx objective list` badges them `~blocked:unread` / `~blocked:refused`
  and `vnx objective show` adds a `plan-gate:` disposition line.

### Fixed

- **Plan-gate reads deliverables, not just the goal field (#1560, OI-1148)**
  — without `--doc`, the plan text reviewed by the plan-gate panel is now
  composed from the track's `goal_state` PLUS its deliverables, so rubric
  axes 3 (deliverables/task_class) and 5 (routing FLOOR) have something to
  judge. A track with zero deliverables and no `--doc` is now refused loud
  (`REFUSED_NO_DELIVERABLES`, distinct from the existing `REFUSED_THIN`)
  before it burns a panel round it cannot pass — including a track whose
  goal is otherwise thick enough. See `docs/core/HORIZON_PLANNING.md`.
- **Plan-gate bookkeeping and correctness (#1518, #1538, #1542, #1532,
  #1502, #1493, #1539, #1527)** — `goal_state` is accepted as the plan
  with a loud refusal of thin goals; `governance_variant` and `gov_trace`
  are persisted into plan-gate records; empty or dropped tiebreaker lanes
  are treated as no-answer rather than a parse failure, with deepseek
  routing; `resolution_reason` is persisted when a plan blocker is
  cleared and the gate's `decision_ref` lands on the track; post-merge
  verification is refused on a stale local checkout; panel seats receive
  `VNX_DATA_DIR` and a thin synthesis is refused.
- **Plan-gate panel role registered (#1571)** — the claude/tmux-lane
  plan-gate panel seats were spawned with `--role plan-reviewer`, but
  that role was in no register: not in `worker_permissions.yaml`, and no
  `agents/plan-reviewer/CLAUDE.md` existed. Since the fail-closed role
  resolution in #1563, the lane refused every claude seat in 0.06
  seconds without a report. A two-seat panel was left with one seat,
  below the liveness quorum of `min(2, panelgrootte)`, so every
  plan-gate fell over on liveness rather than on the plan's content. The
  role is now registered with a read-only profile.
- **Dispatch lanes: role, isolation, and failure truth (#1522, #1537,
  #1488, #1529, #1569, #1568, #1558, #1543, #1579, #1581)** — `VNX_WORKER_ROLE` is
  threaded into the provider-lane spawn environment; `base_ref` is
  honored through provider-lane worktree isolation; tmux buffers are
  named per delivery and main-checkout permissions carry into the
  worktree; a worktree-occupancy lock, an unpushed-commit warning, and a
  fabric-version freeze protect concurrent lanes; `classify_failure`
  reads `completion_text` instead of discarding the failure reason; the
  unbound `model` name on the worker-gone/heartbeat paths is bound; an
  oversize event-store stream now rotates at threshold instead of warning
  forever; provider-lane delivery and reporting are separately observable
  through a `delivery_state` receipt field, and a dispatch that dies on a
  quota- or auth-failure leaves a post-hoc readable marker without the
  door log (the tmux lane already had this); the dispatch spec's role
  reaches the provider-lane spawn instead of arriving as `None` and
  silently resolving every worker to the restrictive code-worker
  fallback.
- **Permissions and roles fail loud (#1557, #1534, #1563, #1523, #1503,
  #1511, #1509, #1506, #1545)** — multi-token `bash_allow_patterns` are
  translated into `--allowedTools`; canonical role names get
  `identity_unresolved` as the no-role-resolved sentinel; a role that is
  in no register refuses loud instead of silently defaulting; the
  role-scope gap for backend/quality/security is closed; scoped
  worker-mode is the fabric default; the worker write-boundary is
  enforced by default; `dispatch_paths` is enforced as a real
  file-write scope, not decoration; the `mcp__` namespace is denied to
  close the extension-bridge leak; generated AGENTS.md/GEMINI.md mirrors
  are ignored in terminals/T0.
- **Governance and receipts (#1559, #1554, #1555, #1552, #1491, #1497,
  #1489, #1490, #1515, #1577, #1582)** — event_type+status governed-outcome semantics
  are pinned; a synthesized receipt's status is derived from the declared
  outcome; the reconciler now sees a bare `gh pr merge` (default-ON gh
  merge source); the phantom-guard weighs the pushed branch diff on a
  fix-forward and warns on dispatch-file overlap; receipts no longer
  default `model` to "unknown" — an undeterminable model fails loud,
  prose model values are rejected, and the kimi-for-coding alias
  resolves; work fate is decoupled from receipt fate with a loud failure
  on a read-only version store; gate infrastructure failures are booked
  as unavailable and non-run gates never roll up as PASS; push-verified
  delivery is enforced for PR dispatches; the ledger's `schema_version`
  reader is direction-aware — a schema newer than the reader refuses
  loud, an older one converts in memory and is written back only on
  continuation, and the append-only history is never rewritten; the
  ledger's status vocabulary now comes from one generated source instead
  of two hand-written copies that disagreed on five values, with a CI
  check that fails a second hardcoded copy.
- **CI and doctor (#1536, #1535, #1514, #1546, #1549, #1524, #1551,
  #1533)** — workflow files are validated with actionlint in
  `local-ci.sh`; the merge-preflight VNX CI-run check is fail-closed;
  Profile A is scoped to touched paths and fails closed; every reasonless
  test exclusion gets a measured disposition; `vnx doctor` gains
  t0_state freshness and a current-flip pin warning; `pre-merge-gate
  --pr` gates the resolved PR head rather than the working copy; the docs
  line-ref guard is scoped to tracked docs; argparse `help=` prose is no
  longer flagged as a raw claude spawn.
- **Paths and store (#1550, #1556, #1501)** — an explicit `VNX_STATE_DIR`
  pin survives the VNX_HOME cross-project guard; an orphan-dispatch
  teardown sweep runs under an unconditional guard; migration `apply_0033`
  is idempotent against a version downgrade.

Plus smaller fixes in docs, skills, and tests.

## [1.4.7] — 2026-08-12

Patch release (10 commits since v1.4.6). Four of the ten fixes are for behavior
consumers were demonstrably blocked by before this release shipped: the
`.vnx-version` pin was read from the wrong directory, headless dispatches
could share one worktree with zero warning, `vnx migrate` left every central
store but one stuck below the current schema, and a missing migration table
silently released a reconciler hold that should have blocked the close.

### Added

- **Ledger-health check: every fired dispatch must have a receipt (#1471)** —
  `vnx doctor` now includes a per-dispatch reconciliation between the
  dispatch register and the receipts ledger, plus pull-cursor age and chain
  status. A dispatch that fires with no receipt is now a finding, not
  silence.

### Fixed

- **Dispatch-lane isolation is honored, not silently skipped (#1481, #1478,
  #1476)** — `base_ref` is now honored when a headless or provider-lane
  dispatch creates its worktree, instead of always building on
  `origin/main`, with a loud failure on an unresolvable ref. The door now
  warns and stamps a field when a lane's isolation guarantee cannot be
  verified structurally, instead of staying silent (OI-1158). Worktree
  creation now asserts the result lands on its own branch, catching a
  cross-dispatch identity mismatch at creation time instead of surfacing it
  later as one dispatch's diff under another's name (OI-1124).
- **Worker liveness distinguishes silence from death (#1473)** — a
  deterministic check (tmux session, pane, process) now runs on every poll,
  independent of heartbeat silence. A deep-thinking worker with no output is
  never killed on silence alone, and a confirmed-dead process fails within
  one poll interval under its own reason instead of waiting out the silence
  threshold (OI-1130).
- **`vnx migrate` applies the full chain, staleness goes loud (#1477)** — the
  hardcoded 0022-0031 walk and the generic migration discovery were two
  separate mechanisms. `vnx migrate` now ends with a generic sweep on the
  same store, so a store can no longer get stuck below the current schema
  version, and a store that falls behind is now a `vnx doctor` finding
  instead of silence (OI-1169).
- **A missing `track_pr_delivery` table now holds the close, never releases
  it (#1479)** — the reconciler's delivery hold treated "the table doesn't
  exist yet" the same as "checked and found nothing to object to", which let
  a track close on unguarded PR evidence on any pre-migration store. A
  missing table now returns a hold, and `link-pr --delivery` surfaces the
  failure as an error instead of a silently swallowed warning (OI-1167).
- **CLI surface: `worktree-release` is loadable, the version pin resolves
  from the project root (#1470, #1482)** — the command loader looked for
  `worktree-release.sh`/`worktree_release.sh` while the implementation lived
  under a different file name, so the command was never invocable since it
  shipped (OI-1052). A new test derives every advertised `bin/vnx` command
  from the script itself and asserts it has a working implementation.
  Separately, the `.vnx-version` pin lookup now walks up to the project root
  instead of only checking the working directory, so a T0 orchestrator
  running from `.claude/terminals/T0` reads the same version the project
  root does (OI-1170).

### Changed

- **README corrected on version, billing/headless claims, ADR count; real
  "Your first dispatch" walkthrough added (#1475)** — the Status section
  pointed at 1.2.0, the billing and headless sections still described a lane
  boundary that stopped being the billing boundary once headless opened by
  default, and the ADR count was stale. The new walkthrough is real,
  trimmed output from an actual install-to-receipt run.

## [1.4.6] — 2026-08-12

Patch release (64 commits since v1.4.5). Headline: the dispatch lanes, worker
receipt semantics, merge gates, and release/install guardrails were hardened so
consumer fleets stop hitting defects already fixed on main.

### Added

- **Documented gate-check CLI surface (#1462)** — `gate-check` is now exposed on
  the pip CLI, so consumers can run the documented pre-merge gate without relying
  on an internal entry point.
- **Worktree release path and learning dispositions (#1425, #1430)** — locked
  worktrees gained a governed release path, and pending archival records now have
  explicit approve/dismiss verbs.
- **T0 state builder install artefact (#1404)** — the t0_state builder ships as
  an install artefact instead of remaining repo-local tooling.

### Changed

- **Dispatch-lane capacity and isolation (#1455, #1451, #1456, #1449, #1446,
  #1463, #1448, #1442, #1416, #1417, #1419, #1411, #1413)** — the claude
  headless lane is open, tmux concurrency is raised to 5, paste buffers are
  scoped per dispatch/call, worktree isolation is default-on, dirty worktrees
  salvage substantive changes including untracked non-gitignored files, AUTO
  fallback pairs provider+model, worktree launchd installs fail loud, provider
  lanes preserve unpushed work before reap, push+PR obligations bind the envelope
  lanes, permission-menu panes are treated as awaiting workers, and fabric hook
  artefacts anchor at the fabric install root.
- **Heartbeat, workers and routing (#1464, #1460, #1465, #1427, #1429)** — the
  default silence threshold moved from 600s to 1800s; heartbeat kills now land as
  failure-shaped receipts; `kimi_exec` routes through the model resolver; kimi-k2
  and codex candidates have explicit smart-router pricing; worker permission
  profiles align with canonical dispatched roles; and three noisy monitor alarms
  were damped.
- **CI and release verification coverage (#1458, #1467, #1438, #1434, #1432)** —
  Profile A regained 23 measured-green files / 491 tests, codex gate-clearance
  tests are hermetic, register isolation tests and leak measurement were added,
  receipt mirror isolation was de-quarantined, and critical-rules footer /
  receipt-resolution tests were pinned.
- **Governance and delivery contracts (#1435, #1424, #1420, #1418, #1403,
  #1436)** — push containment and single-destination PR logic were tightened,
  envelope classification now uses the worktree's recorded base, report
  contracts are binding on envelope lanes, delivery markings create a fail-closed
  hold with visible reason, merged-PR evidence threads into the close verb, and
  four stale config truths were corrected.
- **Documentation and measurement state (#1423, #1414, #1335)** — the lane
  conformity matrix was corrected after the lane-conformity series, the L1 matrix
  measurement report landed, and the daemon-driven T0 context-rotation plan was
  recorded for follow-up verification.

### Fixed

- **Gates fail closed on unavailable or unverified checks (#1461, #1468, #1444,
  #1440, #1433, #1422, #1415, #1412, #1410, #1408, #1406)** — kimi provider
  outages book as `unavailable` instead of review `fail`; `pre_merge_gate` no
  longer reports GO on checks that could not run and now has
  `SKIPPED_UNVERIFIED`; plan-gate seat timeout/unreadable-config handling was
  split; pip CLI and engine flags were realigned; gate dispatch is table-driven
  from the Gate enum; unknown gates require producer identity; envelope
  body-contract violations are observed; run-resolver passes are emitted; and
  no-verdict is separate from abstain.
- **Receipts, register and identity extraction (#1457, #1445, #1447, #1459,
  #1443, #1439, #1426, #1421, #1409)** — Dispatch-ID parsing is line-anchored
  for both bold forms; phantom guards are scoped to dispatch reports; the
  dispatch register is filled at fire time; idempotency is checked inside the
  write lock; phantom ID exclusions were cleaned up; lane identity wins over
  body-provided model/provider; appended receipts always emit register rows;
  echoed instructions and phantom filenames are ignored during identity
  extraction; and report-derived receipts lift model/provider deterministically.
- **Installer, doctor and runtime roots (#1454, #1450, #1453, #1437, #1428,
  #1405)** — `install.sh` no longer corrupts the literal `VNX_HOME` token in
  `vnx_settings_merge.py`; hook-pin checks run from SessionStart and include
  fabric-deployed pins; runtime state resolves to the canonical central dir
  instead of XDG; `vnx horizon` works through both entrances; and doctor symlink
  conflict checks resolve paths before comparison.

### Removed

- **Dead context-rotation control-plane (#1466)** — the unused
  `context_rotation` checkpoint control-plane was removed while the live handoff
  surface was kept.

## [1.4.3] — 2026-08-03

Patch release (6 commits since 1.4.2). Headline: **CI was measuring 2% of the suite**, and
**tests could write to the production store**. Profile A ran 18 of 933 test files; it now runs
~763 (#1310). Widening it immediately surfaced a test that had never been CI-safe and left main
red from 13:12 until the evening — fixed by giving the Profile A checkout full history so
`check_pr_size` can resolve pinned SHAs (#1337). Separately, two tests wrote straight into the
live central store: one left 14 files in `dispatches/staging/`, the other could flip `mode.json`
from `operator` to `starter` and close the governance door for `dispatch`, `gate` and `dream`
fleet-wide. A fail-closed guard now refuses a real-central-store write under pytest (#1333).

### Fixed

- **CI measured 18 of 933 test files (#1310, OI-908, OI-906)** — Profile A ran a hardcoded
  handful while the suite grew to 933 files, so a green merge gate said almost nothing. The full
  suite now runs in CI with the F39 replay gated behind an explicit marker (it drives `claude -p`
  and does not belong in an unattended lane). The 213 reds this exposed were all pre-existing;
  they are tracked as eight open items grouped by cause, not one per file.
- **Profile A checkout could not resolve pinned SHAs (#1337, OI-838)** — the checkout ran at the
  default shallow depth, so `check_pr_size` could not resolve the SHAs it was pinned to and the
  workflow concluded `failure` while the individual job names read as green. Now `fetch-depth: 0`.
  Two dead mocks repaired in the same pass.
- **Tests could write to the production central store (#1333)** — `test_pr_dispatch_integration`
  seeded staging dispatches into the live store, and `vnx_mode.write_mode()` could flip the live
  `mode.json` through its resolver fallback.
  `vnx_paths.refuse_real_central_store_write_under_pytest()` now fails loud when code under
  pytest is about to write to the real central store. Deliberately called from write surfaces
  rather than from path resolvers: pure resolution must keep pointing at the real store
  (read-only tests assert that), only an imminent write is the hazard. Adopted in `vnx_mode` and
  `pr_queue_manager`, plus an autouse fixture in `tests/conftest.py`.
- **Merge gate blocked the whole refactor series (#1332, OI-937)** — every PR in the extraction
  series failed the file-size gate on `dispatch_cli`, including one that made the file smaller.
  A temporary `FILE_SIZE_ALLOWLIST` entry unblocks the series; it comes out when the extraction
  lands.
- **Track-reconciler test asserted a dead command form (#1331, OI-934)** — the test pinned the
  plan-gate hint to a literal command string that no longer exists, so it verified spelling
  instead of behaviour. It now asserts the hint's meaning.

### Changed

- **Per-seat distillate budget in the plan-gate panel (#1334, OI-820)** — one shared budget
  starved the later seats, so most of the panel never reached the synthesis stage. Each seat now
  carries its own budget: 6,000 characters reached synthesis before, 60,258 after.

## [1.4.2] — 2026-08-02

Patch release (42 commits since 1.4.1). Headline: the **reconcile chain and the cleanup of the open-item administration**. Open items went **from 773 to 53 across eight triage rounds** (#1304, #1305, #1307, #1315, #1316, #1317, #1318, #1319, #1320, #1321, #1323) — not a list-tidying exercise but the repair of an administration that was reporting itself green. The automatic close-out reconciler **stood still for 31 hours on a broken `gh` lookup path** and booked that as "does not exist" instead of as an outage (#1314). The bridge between findings and planning **would have wiped all 70 plan-gate blockers the moment it was switched on**; that only surfaced because it was measured against a copy of the production database (#1322). Workers on the provider lane **ran without their role instructions** (#1313, OI-926), and role validation was a silent default instead of a check (#1312, OI-921). The **refactor program has started**: the two proof tools landed in `scripts/` with their own tests (#1325), and the first verified moves are in (#1328, #1329).

### Fixed

Reconcile chain:

- **Auto-close reconciler dead for 31 hours on a bare `gh` lookup (#1314)** — `_detect_gh`/`_gh_pr_view` shelled out to bare `gh`; on a non-interactive PATH (launchd/cron) that is `FileNotFoundError` → "absent", so every nominated track became unverified, nothing closed, and the outage read as "no merged PR". The binary is now resolved once per run to an absolute path (`shutil.which`, then `/opt/homebrew/bin` etc.), and `track_freshness` gained the real autoclose health read: `reconciled` renamed to `derived_refreshed`, plus `autoclose_degraded` from `reconcile_summary.json` with a 24h staleness threshold (measured cadence p90 ~3.2h — ordinary idle does not trip it, the 31h outage does).
- **OI-bridge obsolete-sweep would have wiped all 70 plan-gate blockers (#1322, OI-929)** — the R4.2 sweep closed every active link whose oi_id was not in `open_items.json`; synthetic `OI-PLAN-<track>` blockers never appear there, so one bridge run resolved every plan-first gate blocker (measured 70→0 in a single run against a production-DB copy — and the supervisor tick runs this bridge automatically). The OI-PLAN namespace is now excluded from the sweep: the bridge has no authority over the plan-gate lifecycle.
- **Reconcile worker bound to its own session lifetime (#1292, OI-873, OI-877)** — the SessionStart hook spawned a detached reconcile worker that nothing ever killed; the SessionEnd hook now kills the worker of its own session via a per-session marker under the central `$VNX_STATE_DIR` (ADR-026), not a repo-local `.vnx-data/state`.
- **End-of-dispatch event teardown in the envelope path (#1291, OI-878, OI-902)** — the door's provider-lane envelope path never archived/cleared the per-dispatch event ringbuffer at end-of-dispatch; rotation only happened on the *next* dispatch's write-side boundary guard, so the last dispatch in a series leaked its events into the live `T{n}.ndjson`. `_govern` now archives under the dispatch's own id before the receipt and clears after it in a `finally`; a live file holding a *different* dispatch's events is left for the boundary guard.
- **Stale `_archive_dispatch_events` mock aligned to the tuple signature (#1327, OI-933)** — the helper returns `(events_path, clear_ok)` since #1291; the mock still returned `None` and failed receipt-stamping tests with `TypeError`.

Role governance:

- **Role validation is enforced, not a silent default (#1312, OI-921)** — the bridge silently filled the backend-developer sentinel for any unset role and the deferred `compile_plan` registry check was never built, so nobody was ever forced to choose a role (20/20 dispatches on 2026-08-01 carried the default). An unset role is now staged as explicit `""` and rejected loud at the door; `compile_plan` rejects roles outside the registry discovered from `agents/` (fail-closed on an empty registry); `agents/**` ships in the wheel so the check works in pip installs too. A consciously chosen backend-developer stays valid.
- **Provider-lane workers ran without their role instructions (#1313, OI-926)** — the provider/envelope lanes assembled the final prompt *without* the role's own CLAUDE.md context: a dispatch staged as quality-engineer got byte-identical context to one staged as system-architect. Both lanes now route through the same lane-neutral injector (`scripts/lib/skill_context.py`), and a deterministic whitespace-normalized containment check (no LLM) stamps `role_applied`/`role_tier`/`role_source_path` on the receipt — true only when the resolved role source's content actually reached the prompt, so the ledger records what was *used*, not what was requested.
- **Staged `spec.role` propagated into envelope/provider receipts (#1303)** — the GOVERN paths resolved the receipt role from `dispatch_metadata` only, so a genuinely-roled dispatch landed as `identity_unresolved` whenever the DB join came up empty (21,095 of 21,779 receipts carried no role). One canonical resolver (caller role → dispatch_metadata → `identity_unresolved`; never the fake default) is wired into all three paths.

Dispatch, paths, CLI:

- **`vnx --version` reads VERSION before pip metadata (#1284)** — `_read_version()` asked `importlib.metadata` first, so the dist-info stamp (written once at editable-install time) always won over the VERSION file that moves with the code: `--version` reported a version unrelated to the running code, and `vnx init` pinned brand-new projects to the stale, already-pruned version.
- **Gate worktree lock dir + PR head branch resolved correctly (#1285, OI-904, OI-905)** — the lock dir assumed `<root>/.git` is a directory and crashed with `NotADirectoryError` in a linked worktree before any gate ran (now derived from `git rev-parse --git-common-dir`); and `gate.sh` derived `--branch` from the local checkout, so T0 gating from main silently gated `origin/main` instead of the PR branch — the gate agent's reads missed the diff under review (now resolved from `gh pr view --json headRefName`, with a loud warning fallback).
- **Reachability probe authenticated + HTTP status propagated to receipts (#1286, OI-893, OI-866)** — the dry-run probe sent unauthenticated requests to auth-gated endpoints, so it returned 401 by construction and every healthy keyed route looked dead; the probe now carries the lane's own key and reports 401 as AUTH REJECTED with a key hint, never as unreachable. And a litellm HTTP error status was dropped in normalization, collapsing receipts to "(no error captured)"; the status code now survives the normalize → consume → finalize chain into `failure_reason`/`failure_class`.
- **`mode.json` resolved through the fabric resolver + fail-loud write guard (#1290, OI-911)** — `_mode_file_path` read `VNX_DATA_DIR` raw — a fourth resolver without the two-key contract; a cleaned-env subprocess lost the flag and its write silently fell back to `~/.vnx-data/vnx-dev`, flipping mode operator→starter and closing the governance door for `vnx dispatch`. Now resolved via `vnx_paths` like every other consumer, with a write guard that refuses divergent or cross-project targets.
- **Pin honored by install identity; doctor separates pin from active (#1294, OI-892, OI-914)** — reexec compared VERSION strings, so a rolling dir like `versions/edge` satisfied a version pin while carrying arbitrary commits; now compared by resolved install dir (path identity), and the doctor reports the project's `.vnx-version` pin and the active install dir as separate fields.
- **Receipts routed to the active project store + reference-safe version prune (#1308, OI-900, OI-912)** — two receipt writers resolved the runtime root without the active project context (a hardcoded `vnx-dev` fallback; a `__file__`-anchored state dir inside the shared versions tree), landing 21 plan-gate receipts in the wrong tenant store and 29 in the version dir; both now follow the ADR-007 resolution chain, failing closed when no project is resolvable. And `vnx update` pruned version dirs the console script still pointed at, killing `vnx --version`; the prune now scans candidates for live pip/symlink references and protects them with a message + audit event.
- **Central-first state readers (#1309, OI-859, OI-897b)** — `dispatch_guard.sh` read a repo-local `t0_brief.json` that does not exist in a fresh checkout; it now reads runtime state via `vnx status --json` (degraded-check preserved, one source so the divergence-check is gone). The T0 SessionStart hook resolves the central state/logs dirs via `vnx_paths` with an explicit interpreter — no more repo-local-write / central-read split-brain. And in a bare git repo with only an origin remote, `project_id` resolved but the data dir fell back repo-local, crashing `vnx horizon list`; the ADR-007 git-remote fallback is now scoped to the project root.
- **Central claim registry + `awaiting_permission` detection (#1311, OI-861, OI-863)** — the dispatch-id claim registry lived in repo-local state, forking the map exactly as far apart as the racing worktrees; it now lives in the canonical central state root and simultaneous claims from different project roots serialize on one shared map. And a detached tmux worker stuck on a Claude Code permission prompt showed no event, no receipt, no exit — only the hours-long dispatch deadline would ever fire; a pure pane classifier now labels `awaiting_permission` (recoverable — one relayed answer saves the worker — and explicitly NOT a dead worker), so the lane relays instead of fast-aborting.
- **SC2155/F821 lint fixes in the dispatch path (#1297, OI-268, OI-288)** — `local x="$(cmd)"` declare-and-assigns (which mask the command's exit code) split in `dispatch.sh`; runtime import of `ExecutionPlan`/`ExecutionPermit` so `get_type_hints` resolves the string annotations in `dispatch_envelope`.

Analytics and observability:

- **Token rows linked to dispatches + provider-lane receipts priced (#1293, OI-872, OI-882)** — the conversation analyzer inspected only the first Dispatch-ID match per message, hit the `<dispatch_id>` template placeholder, and stopped: 96.7% of token rows stayed unlinkable. It now scans every mention for the first valid ID plus a backfill phase for already-analyzed rows (measured on a scratch copy: linked 40→621). And the envelope hardcoded `cost_usd=None` — provider-lane receipts carried real token_usage but no price; the chain broke on conversion, not measurement. The envelope now resolves the actual spawned model, computes cost, and emits the ADR-005 cost event.
- **tmux `list-panes` failure surfaced via `degraded_reasons` (#1295, OI-558)** — the panes subprocess-failure path degraded busy/idle classification silently (sessions still listed, every pane command treated as absent); a failing probe is now logged and visible on the operator dashboard.
- **Dream reviews surfaced in t0_state + `dream_cycles` freshness producer (#1300, OI-896)** — two silent producers gained readers: a `dream_reviews` section in `build_t0_state` (pending count, oldest age) and a producer-freshness key, so an unreviewed completed cycle goes stale and silence becomes a signal within a day.

Triage rounds — measurement reports (no code change, verdicts with file:line evidence):

- **T2: OI-105..128 re-tested (#1296)** — twelve items from 2026-06-03; the large majority proven outdated against main, OI-105 unjudgeable (non-reproducible runtime incident).
- **OI-005..560 batch re-tested (#1304)** — 4 outdated, 1 unjudgeable, 7 still-standing advisory/design items; side-observation: ADR-005 doc-drift on the dispatch_register path.
- **T12: OI-699..766 re-tested (#1307)** — 9 outdated with evidence, 3 still-standing design items; the kimi/phantom-guard/report-contract items are fixed on main or their mechanism is removed.
- **R1: OI-005..629 re-tested (#1315)** — 1 outdated (the gemini runner failure mode was structurally replaced), 11 still-standing advisory/design items.
- **R5: OI-818..833 re-tested (#1319)** — 3 outdated (worker-provider free choice landed; the refuted codex-round-3 claim), 9 still-standing design items.
- **T9 report landed (#1324)** — the OI-561..655 verdicts from an earlier triage branch whose code fix already reached main via #1301; without this PR the evidence trail for that batch was missing.

Triage rounds — small fixes, each with a test red on main:

- **Five month-old OI-ledger findings closed (#1299, OI-003, OI-004, OI-015, OI-017)** — `traceability_audit` strategy 2b now matches normalized event types (legacy `event: pr_merged` receipts traced) and strategy 4 requires ≥2 significant branch tokens so a lone `feat` token cannot attach an unrelated dispatch; `index_adrs` record hash includes `project_id` (same-content cross-project collision ended); `claim_next_queued_dispatch` restores the caller's `isolation_level` in a `finally`; `pool_manager` spawn redirects child stdout/stderr to DEVNULL instead of undrained PIPE buffers that can deadlock.
- **F821 `PrEnforcementResult` in tmux dispatch (#1301, OI-645)** — the string annotation was an undefined name (imported only inside an except-handler), so `get_type_hints()` failed with `NameError`; module-level import + regression test.
- **Function-size splits with AST guards (#1302, OI-547, OI-558)** — `_check_hook_paths` (89→57 lines) and `_operator_get_sessions` (83→42); both carry ≤70-line AST guards that were red on the old code.
- **Shell lint in receipt/session hooks (#1305, OI-678, OI-679, OI-684)** — `receipt_processor.sh` tests the exit status directly instead of `$?` (SC2181) and splits four declare-and-assigns (SC2155); `sessionstart.sh` drops the dead ROLE/TRACK override block that was never read or exported.
- **`hook_settings_written` event + ReceiptV2 contract constants (#1316, OI-804, OI-817)** — the tmux lane now emits the event after a successful `settings.local.json` write (ADR-005 audit gap closed), and `ReceiptV2.__post_init__` forces `schema_version=2`/`event_type=task_complete` so the constants can no longer be bypassed.
- **R3: four small fixes (#1317, OI-695, OI-775, OI-796, OI-798)** — `vnx init --force` clobbered the project-owned `.claude/settings.json` (now always preserved, same contract as the `.vnx-version` pin); `terminal_snapshot` enriches `claimed_by`/`lease_expires_at` from `dashboard_status.json`; receipt enrichment used `setdefault` for `session_id`, preserving null/blank (now a truthiness guard); the five timestamp markers wrote with truncating `echo >` (now atomic tmp+rename, same as the freshness write).
- **R2: five small fixes (#1318, OI-656, OI-657, OI-660, OI-669, OI-670)** — `sessionstart.sh` splits model-invocable vs operator-only skills (architect/t0-orchestrator no longer advertised as invocable); tmux-protocol test helpers select completion blocks by content instead of position; a zero-byte `skills.yaml` is re-seeded and `validate_skill` raises a clear `ValueError`; shellcheck SC2154/SC2155 closed in `rp_delivery.sh`.
- **R7: analyzer fail-closed exit code + marker footer (#1320, OI-862, OI-890)** — `conversation_analyzer` ignored its own RunStats return, so a night in which *all* sessions failed ended with exit 0; it now exits 1 with a "fail" heartbeat when errors>0 and nothing was analyzed. The marker-syntax + noqa-rejection rule is codified in the canonical T0 role footer so every dispatch carries it.
- **R8: five small fixes (#1321, OI-908, OI-916, OI-918, OI-919, OI-925)** — the f39-replay tests (31 real headless `claude -p` calls) are marked `live` and deselected by default, with the opt-in restricted so `-m "not integration"` no longer silently collects them; `test_session_reconcile_lifetime.sh` exports `VNX_STATE_DIR` (a bare assignment does not reach child processes); the envelope clears live events only after a *successful* archive (clear-on-archive-fail wiped exactly the events that had to be kept); four permanently-red stale flag-gate tests rewritten to the current door contract; `_fail_loud_on_empty_success` gives a descriptive message instead of "(no error captured)".
- **R6: raw lane output on parse-error + kimi test staleness (#1323, OI-839, OI-846)** — the plan-gate panel persists the raw lane output of parse-error seats into the hash-chained seat ledger, so parser hardening can be built against the real failure mode instead of a guessed one; the five red-on-main kimi tests (plus a hidden codex variant) were test-vs-code drift after the kimi-lane hardening — stale model keys and mock signatures updated.

Also on main, preserved for T0 verification: two salvaged worker change sets whose lanes were killed externally mid-flight without a receipt (#1287 drain-thread-leak, #1288 chunk-timeout-deadline) — unreviewed, not merged on that basis.

### Changed

- **Refactor proof tools landed in `scripts/` with their own tests (#1325, phase 0)** — `refactor_equivalence.py` and `refactor_surface.py` moved out of `claudedocs/refactor-tools/`, fixing three T0 findings on the way: `_find` searched *all* scopes via `ast.walk` and could silently match a nested closure with the same name (now top-level/class body only, with a loud ambiguity failure); a dead module-level `REPO` constant removed; and the cwd-relative `--lib-dir` default replaced by the module bootstrap as the single mechanism, with a test hardened to actually be able to go red (PYTHONPATH scrubbed).
- **Deterministic-first design principle + role routing table (#1298, docs)** — the system-architect agent must name the deterministic vs model-backed parts of a design and justify every model use; the T0 worker dispatch policy now states that backend-developer is the sentinel default and must never be chosen out of convenience — the role follows the work, not the terminal.
- **First moves of the `track_reconciler` split (#1328, #1329)** — `_compute_derived_status` → `scripts/lib/track_reconciler_status.py` and `close_track_if_done` → `scripts/lib/track_reconciler_closure.py`: pure moves, AST-identical as proven by the new equivalence tool, re-exported at the old location so no consumer changed. `track_reconciler.py` drops 1143 → 667 lines, and its silently-broken ADVISORY ONLY contract is verifiably restored: the remaining file provably writes only `tracks.derived_status`.

## [1.4.1] — 2026-08-01

Patch release (8 commits since 1.4.0). Headline: the **fleet-wide plan-gate unblock** — in a central install the plan-gate resolved its data dir from the module's own location inside the read-only pinned version dir, so every plan-gate on the machine died with `PermissionError` on `~/.vnx-system/versions/<v>/.vnx-data` across all five provider lanes and every project. The data dir is now resolved from the central store for the active `project_id`, plus seven hardening fixes across the dispatch, gate, audit, and scheduler paths.

### Fixed

- **Plan-gate data-dir resolution (#1280)** — the headline fix. `resolve_data_dir(caller_file=__file__)` anchored to the module's own location, which in a central install sits inside the read-only pinned version dir; now resolved from the central store for the active `project_id`. Same anchor fixed in `provider_costs._resolve_costs_path`, which mkdir's `events/` on every provider-lane dispatch and is why all five lanes failed identically. Also: 0-of-N readable verdicts now returns `INFRA_FAIL` instead of `REVISE` — a plan that was never reviewed must not read as a plan judgment.
- **Plan-gate probe degraded semantics + per-seat verdicts (#1275, OI-888)** — `degraded` fires on an all-attest ledger, and per-seat verdicts are persisted.
- **Dispatch-boundary ringbuffer rotation enforced on the write side (#1276, OI-878)**.
- **Dream scheduler job runnable after install (#1277, OI-895)** — the installed job now actually runs under launchd/cron.
- **Adoption reader reads the DB offer junction (#1278, OI-894)** — zero-row updates become visible.
- **Declared review gates wired to evidence (#1279, OI-876/OI-881)** — gates connected via obligations.
- **Side-door delivery scan skips pattern-definition guards (#1281, OI-898)** — those guards hold lane literals as detection patterns and never deliver.
- **Project-id guard on `project_root.resolve_data_dir`'s explicit branch (#1282, OI-899)** — the `VNX_DATA_DIR` + `VNX_DATA_DIR_EXPLICIT=1` branch was completely unchecked while `vnx_paths` was already guarded. Advisory by default (`VNX_DATA_DIR_GUARD=warn`).

## [1.4.0] — 2026-07-31

The fourth minor (47 commits since 1.3.1). Headlines: the **ReceiptV2 schema contract with measured token capture** (claude-harness transcript harvest for all providers and a session `wire.jsonl` harvest for kimi), **worker-provider free choice shipped end-to-end** (ModelPin floor-vs-default semantics, workers default-pinned), the **producer-freshness monitor** that makes silent producer death visible within a day, the **door writing a dispatches row on acceptance**, provider-lane **failure classification with a dead-route registry**, **ringbuffer truncate on every teardown path**, phantom-guard **worktree-branch resolution**, and **coordination-lock hardening** (bounded retry + process-group cleanup).

### Added

- **ReceiptV2 schema contract + role/receipt_kind propagation (#1229–#1235, #1237)** — ReceiptV2 and SynthesizedLaneReceipt contracts codified; role + receipt_kind propagated into v2 emit and govern-synthesized receipts; receipt_kind stamped on all remaining emitters with lint flipped to raise; converter role resolver + write-time capture-gap backfill; token-capture from claude transcripts; tool-call signals + dispatch→PR/rework columns; closed-outcome-status via recompute.
- **Measured token capture for the harness lanes (#1269, #1270)** — `token_usage` harvested for all claude-harness providers (OI-884) and measured kimi tokens harvested from the session `wire.jsonl`.
- **Worker-provider free choice (#1239, #1240, #1244, #1245)** — ModelPin contract (behavior-neutral), door coercion honors `ModelPin.semantics`, D4 floor-vs-default branching, and the worker pin_semantics flip from floor to default.
- **Producer-freshness monitor (#1267)** — silent producer failure becomes visible within a day.
- **Door writes a dispatches row on acceptance (#1266, OI-847)** — accepted dispatches enter the register from the door.
- **Provider-lane failure classification (#1264, OI-866/867)** — classified failure reasons + dead-route registry + dry-run reachability check.
- **Ringbuffer truncate on all teardown paths (#1263, OI-858)** — with a hard upper bound.
- **Worker-scope PreToolUse enforcement hook (#1223)** — worktree-wired, default OFF.
- **Gated, audited operator escape-hatch (#1220)** — routes one build task back to claude.
- **`deadline_seconds` threaded spec → spawn (#1259)**; **`requires_mcp` threaded through the plan boundary (#1268, OI-865)**.
- **CI out-of-repo symlink guard (#1256)** — tests referencing code outside the repo are caught.
- **Pinned version dirs read-only after install (#1255)**; **premigrate backup skipped on no-op runs + rotation on three mechanisms (#1258)**.

### Changed

- **Build-worker default stays kimi-k3** — a quota-exhaustion flip to sonnet (#1221) was reverted (#1222); the audited escape-hatch (#1220) is the supported claude route.

### Fixed

- **Phantom-guard worktree-branch resolution (#1253, #1262)** — prefers the pushed branch over the worktree diff in the provider lane and detects a self-referencing `base_ref`.
- **Coordination-lock hardening (#1260)** — bounded lock retry + process-group cleanup on worktree teardown.
- **Plan-gate doc truncation surfaced in the verdict (#1265)** — with the ARG_MAX-derived cap raised.
- **Analyzer correctness (#1248, #1261)** — project_id, ollama fast-fail, billing guard, atomic writes; placeholder dispatch_id rejected and the token_usage chain closed.
- **Billing classifier classifies the real dispatch population (#1243, OI-824)**; **fail-closed close-gate on incomplete delivery (#1242, OI-829)**.
- **Provenance-chain seams closed (#1238, #1241, #1246)** — the automated sweep commits instead of silently rolling back; the four data-fill seams blocking the chain are closed; the receipt-provenance write transaction is narrowed.
- **Deliberation-panel reliability (#1236, OI-809/810/811)**.
- **kimi-spawn fabrication guard made commit-aware (#1225)** — tree-diff supersedes the earlier blind guard.
- **T0 role modernised (#1257)** — free-per-dispatch pins, horizon planning, serial lane.
- **OI acceptance-criterion guard landed in the source repo (#1254)**; **reconcile-hook interpreter anchor + singleton guard (#1247)**.
- **Diagnostics hardening (#1249)** — gate-name validation, merge-base `pr_size`, holder metadata, classified failures.
- **Skills paths: scoping dropped from horizon/planner/control-centre (#1250)**.
- **Docs corrected (#1251)** — stale references for #1246/#1248/#1249/#1250 replaced; runtime-liveness inventory added.

## [1.3.1] — 2026-07-23

Patch release. Headlines: **kimi-k3 is the default build-worker** for T1/T2/T3, the pip CLI **honors the `.vnx-version` pin via startup re-exec** (hardened against cwd-local `vnx_cli` shadowing), **`vnx release publish --tag`** cuts immutable central-store versions with pin-safe GC, the **deepseek-v4-flash migration** off the discontinued deepseek-chat, and **symlink-safe `vnx init` scaffold writes**.

### Added

- **`vnx release publish --tag` + pin-safe GC (#1218)** — immutable `versions/v<X.Y.Z>/` materialization from a git tag; GC prune protects pinned/running versions and fails closed when the protected set is undeterminable.
- **Pin-honoring re-exec (#1213, #1216)** — the pip-installed CLI re-execs into the pinned central install at startup; re-exec hardened against cwd-local `vnx_cli` shadowing.

### Changed

- **kimi-k3 default build-workers (#1211, #1210, #1209)** — T1/T2/T3 default from sonnet to kimi-k3; the kimi worker lane made agentic (`--yolo` + `-w`); provider dispatch resolves the consumer project root, never the central install.
- **deepseek-v4-flash migration (#1215)** — smart_router moves off the discontinued deepseek-chat.
- **kimi spawn hardening (#1212, #1214)** — plain-string assistant content extracted; per-chunk stall-timeout default raised 600s → 1200s.
- **Symlink-safe init scaffold (#1217)** — all `vnx init` scaffold writes are symlink-safe and atomic.

## [1.3.0] — 2026-07-16

The third minor (70 commits since 1.2.0). Headlines: the **plan-first gate enforced at the dispatch door** (ADR-030, advisory-first with a config-plane flip), the **T0 playbook hook-injected at SessionStart** (F1 — no more trust-prompt/invocation gap), **auto-PR enforcement for build workers**, **fail-loud lanes** (empty provider completion or empty tmux extraction can no longer report success), the **effectiveness-probe registry** that gates the learning loop on measured health, and the **subsystem cockpit SSOT** (`SUBSYSTEMS.md` + `vnx subsystems`). Enforcement machinery stays default-off behind operator knobs.

### Added — Governance & gates

- **Plan-first gate at the dispatch door (ADR-030, #1111)** — advisory-first enforcement; enforce mode honors the persisted config plane (#1115), `VNX_PLAN_GATE_ENFORCE` registered for an audited flip (#1119); plan-gate-pass evidence as merge-gate primitive (#1121); hints emit the real `vnx horizon plan-gate` command (#1120).
- **ADR-031 ratified (#1114, #1118)** — operator-facing orchestration-target architecture, supersedes ADR-028.
- **ADR-032 (#1162)** — skills-in-consumers install-artifact model promoted to ADR.
- **ADR-034 design (#1172)** — external chain-origin anchor for the receipt hash-chain, GOOD after 3 codex rounds (implementation lands post-1.3.0).
- **Auto-close ON by default in the reconcile tick (#1110)** — merged PRs close their tracks without operator sweeps.

### Added — Dispatch & lanes

- **Auto-PR enforcement (#1170, #1175)** — tmux-spawn build workers create their own PR; `autopr_rejected` mirrored in the shell processor.
- **Fail-loud lanes (#1107, #1127)** — empty provider-lane completion downgrades to loud `failure` with the raw spawn result; tmux lane refuses `done` on empty extraction.
- **Worker-output salvage after lane timeout (#1176)** — a 3600s deadline no longer strands finished-but-unreported work.
- **`--deadline-seconds` passthrough (#1180)** — consumer door overrides the 3600s receipt-wait end-to-end (300-14400).
- **Final-prompt integrity persisted in both lanes (#1116, #1155)** — the enriched prompt a worker actually received is now audit-addressable.
- **Subprocess lane on shared `dispatch_govern.govern()` (#1131)**; dispatch-agent honors the requested provider lane (#1158); per-role `mcp_servers` capability-binding (#1128); OpenRouter arbitrary-model lane skeleton (#1130).
- **rm-rf worker-hang fix (#1169)** — scoped build-toolchain allow-list replaces the interactive dangerous-rm prompt that hung headless dispatches.

### Added — Orchestrator (T0)

- **F1: SessionStart hook-injects the t0-orchestrator playbook (#1174)** — role/audit/templates coherent; kills the trust-prompt + skill-invocation gap.
- **Context rotation as T0-initiated control-plane (#1149, #1150, #1154)** — non-destructive, default OFF; respawned successor pinned to Opus (`t0-opus-only`).
- **Receipt-delivery hardening (#1178)** — submit-verify + dedupe + digest + `VNX_RECEIPT_T0_PUSH` kill-switch; stale notifications can no longer flush into a T0 prompt as input.
- **Proposed deliverables surfaced as `human_gate_queue` in t0-state (#1167)**; per-track `lane_hint` (#1168).

### Added — Intelligence & observability

- **Effectiveness-probe registry (#1137, #1139, #1141, #1146)** — probes for governance stack, plan-gate, migration, and injection; the learning loop activates only when its probe is healthy (#1143).
- **Injection-eval PR-A/PR-B (#1157, #1160)** — delivery-time WHY instrumentation + reason-aware evaluator with measure-only tuning proposals.
- **Subsystem cockpit SSOT (#1135, #1140, #1144)** — `SUBSYSTEMS.md` + config-registry metadata, `vnx subsystems` CLI over the live SSOT, cockpit tile on the observability page.
- **Regression-attribution primitive (#1165)** — names the commit that broke a check.
- **Bench generalization (#1122, #1123, #1132)** — bring-your-own-tasks/models, lane-calibration field-test, `--retry-from` DNF matching fixed.

### Fixed

- **Fleet keystone: `vnx init` seeds `skills.yaml` copy-if-missing (#1173)** and refreshes per-skill, mixed-dir-safe (#1163) — the fleet-wide consumer CI-breaker.
- **Plan-gate-panel seat robustness (#1106, #1161)** — opus-seat data_dir resolution + codex/glm verdict-parser backward scan; scoped-spawn env.
- **Phantom-guard (#1104, #1164)** — read-only reviews exempt; fix-forward dispatches resolve the pushed PR branch.
- **Central-mode hygiene (#1152, #1159, #1166)** — embedded-layout path-assumption sweep, install-mode marker on `vnx update`, roadmap anchored on project root.
- **Security batch (#1125)** — Anthropic credentials scrubbed from litellm/harness subprocess env (S1-S3).
- **Kimi role-gate content-block text extraction (#1129)**; intelligence UPSERT conflict targets (#1147); dashboard light-theme migration + contrast (#1148, #1151).

## [1.2.0] — 2026-07-11

The second minor. Headlines: **ADR-028 orchestration-target Phases 1–4** (agent-folder fusion + a decision-judge that shadows, fast-paths, then binds — all default-off, human-on-the-last-set), a **central-store project_id authority** fix that closes a week of multi-tenant store bugs at the root, **ADR-029 hash-chain epoch-rotation**, a **multi-provider deliberation panel** (`/panel`), an **evidence-bound merge gate**, and the **canonical provider-agnostic orchestrator role** with `vnx role sync`. All new decision/enforcement machinery ships default-off behind operator knobs.

### Added — Orchestration target (ADR-028)

- **Agent-folder fusion, Phase 1 (#1079)** — config extension + resolver for folder-per-agent, provider-agnostic, backward-compatible.
- **Decision-judge, Phases 2–4 (#1096, #1098, #1099, #1100)** — shadow mode (safety valve, default-off) → wired `DecisionRouter.decide` → conservative fast-path → Phase-4 judge-binding policy (human-on-the-last-set). Every phase default-off; nothing auto-activates.
- **ADR-028 P5 cutover verified-complete + hash-chain done (#1092, docs).**

### Added — Governance & audit

- **ADR-029 hash-chain epoch-rotation (#1090)** — verify + seal + audit across chain epochs.
- **Evidence-bound merge gate, D3 bootstrap (#1080)** — the merge gate verifies requirement-completion (test/gate receipts), not just provenance.
- **Signed batch-delegation mandate (#1081)** — one-time batch mandate instead of Touch ID per dispatch, default off.
- **ADR-007 composite UNIQUE indexes over project_id (#1082)** — non-destructive, defensive multi-tenant isolation.
- **Blocking gate findings become track_open_items (#1054)** — ppb #1039 gap closed.
- **`headless_block` wired live in the routing-policy loader (#1068)** and **worker-permission enforcement, feature-flagged default-OFF, both lanes (#1078).**

### Added — Panel, skills, role

- **Multi-provider deliberation panel skill `/panel` (#1101, #1102)** — 4-stage deliberation across the provider fleet; real data_dir + synthesis provider-fallback.
- **Canonical orchestrator role + `vnx role sync` (#1056, #1057, #1061)** — fleet-identical, provider-agnostic role; dual-CLI gap + project resolution fixed; file-layout master-rule codified.
- **Directory-based skills sync + `fabric-reference` runbook skill (#1062, #1070)** — registers `horizon` & `fabric-reference`; skills-manifest gap 1 closed.
- **Fleet-wide dev-worker agent library resolvable from any project (#1089)** + **packaged backend-developer example agent (#1067).**

### Added — Dashboard, intelligence, durability

- **Live-sessions observability tile (#1076)** and **self-learning proposals surfaced on operator/improvements (#1071).**
- **Scout-effectiveness measurement harness (#1072)** — observational A/B on receipts.
- **Whole-repo advisory backlog scanner (#1075)** and **advisory pre-flight FK/integrity check before each store migration (#1094).**
- **quality_intelligence backup rotation, keep last N (#1087, `VNX_DB_BACKUP_KEEP=3`).**

### Changed

- **Auto-close ON by default in the SessionStart tick (#1055, #1097)** — the future-state fabric closes merged tracks without a manual sweep.
- **Horizon `list` hides done tracks by default (#1060)** — `--all` to show them.
- **Retired dead OTel wiring + fixed kimi silent-zero token usage (#1069).**
- **Thinned the injected CLAUDE snippet to a pointer, WP1 (#1064).**
- **`VNX_DATA_DIR_GUARD` startup guard, warn by default (#1084).**

### Fixed

- **Central-store project_id authority (#1091, #1093)** — resolve project_id from the target project (not a hardcoded `vnx-dev`); door authority = physical staged-bundle location. Root cause behind a week of multi-tenant store bugs.
- **Tenant-stamping (#1083, #1095)** — widen child FKs, dedupe the dual-seed `pool_config`, re-enable W1 in `vnx migrate`.
- **`objective sync` roadmap anchor (#1108)** — anchor on `--project-dir`, not the central example-template.
- **`vnx version` reads the resolved-engine VERSION (#1088)**, not a stale pip dist-info.
- **Billing classifier (#1063)** — kimi=subscription, local-gemma=local.
- **GLM constraint text reconciled to GLM-5.2 SSOT (#1059).**
- **Security (#1065)** — close CGNAT bypass + DNS-rebind TOCTOU in `url_policy`.

## [1.1.0] — 2026-07-08

The first minor since 1.0.0. Headlines: the **Horizon planning module** (`vnx horizon`), **signed attestation enforcement** (ADR-027), **track-linkage + git-grounded backward closure** (the future-state fabric now closes itself against merged PRs), **`vnx fabric-audit`** (ADR-028 Phase-0 store-hygiene), and an **operator-gated self-learning proposal tier**. The 1.0.1 future-state reconciliation batch is folded in below — it landed on `main` but was never tagged separately.

### Added — Horizon planning module

- **`vnx horizon` command group (#1014, #1015, #1018, #1022)** — the future-state layer gets a named, tenant-safe command surface: `list / show / add / sync / drift / reconcile / close / reopen / plan-gate / deliverable`. The `pm` skill was renamed to `horizon` (pm alias kept), with parity + ADR-007 cross-project isolation test coverage. Documented in `docs/core/` (Horizon planning module).
- **plan-gate attest + link-pr / close --attest escape-hatches (#1033, #1038, #1046)** — an operator can attest a plan-gate as passed without re-running the panel, link a PR to a track, and close a track with an evidence attestation; wired into the canonical `vnx horizon` surface.

### Added — Governance: signed attestation enforcement (ADR-027)

- **D1–D5 attestation gate (#1004, #1007, #1009, #1011, #1012)** — SSH-key signing + verification and an attestation manifest; an in-repo, content-keyed, diff-bound (squash-safe) attest record; a server-side verify gate (staged advisory + CODEOWNERS trust-root); a signed, budgeted, audited gate-override (recorded deviation, never silent); and `vnx init` provisioning of the attest trust-root + shipping the gate workflow.

### Added — Track-linkage + git-grounded backward closure

- **Track-linkage TL-D1–D5 (#1032, #1034, #1035)** — `track_id` on the dispatch spec + door validation + persistence; auto-population of `track.pr_ref` from `dispatch.track_id` on merge; and a reconcile hint that names the blocking open-item + the exact `oi-close` command on a blocked derivation.
- **Git-grounded batch auto-close (#994–#1000)** — `vnx horizon reconcile` verifies PR merge state via `gh` and closes CONFIRMED tracks (system actor, no human approval-id); an audited `done → active` reopen valve + re-close guard; advisory-first continuous wiring (tick, review log, flip streak); and ALL-merged multi-PR derivation. The flip to auto-apply requires 7 consecutive clean runs plus an operator review (#1000).

### Added — Self-learning proposal tier (operator-gated)

- **Intelligence D1–D7 (#1001–#1010)** — an explicit confidence-range contract; a reversible drop of the dead `success_rate` column; an operator-gated proposal tier that supersedes stale patterns; a tagger A/B harness (opt-in, no default-on); outcome-grounding shadow-verify (V2 vs V1); operator-gated skill-refinement proposals from rework attribution; and the philosophy write-up (operator-gated tiers, off-switches). Nothing auto-activates: proposals land for a human to accept.

### Added — fabric-audit, durability, quality, dispatch

- **`vnx fabric-audit` (#1045)** — Phase-0 fabric hardening check (split-brain stores, per-project ledgers, receipt hash-chain integrity), ADR-028.
- **NDJSON durability (#1031, #1041)** — `fsync` on the audit append + a shared torn-tail read guard.
- **Configurable tmux-spawn concurrency (#1017)** — `VNX_TMUX_MAX_CONCURRENT` (N-slot semaphore, default 1).
- **Async scout pre-pass (#1027)** — pending-sweep + discovery + dispatch-linked receipts.
- **Global process-hygiene scan (#1029)** — violation / idle / protected classification.
- **Full provider-family plan-gate panel (#991)** and **plan-gate bounded single-retry before abstain (#1030, #1042)**.

### Changed

- **Worker model pin Sonnet 4.6 → Sonnet 5 (#1013).**
- **`gemini_review` retired as a required gate (#1028)** — codex is the required review gate; gemini is opt-in.
- **tmux-spawn workers default to `--dangerously-skip-permissions` in an isolated worktree (#1016)** — `VNX_WORKER_SCOPED=1` opts into scoped permissions.
- **Central-mode path correctness (#1023, #1025)** — `__file__`-anchored data-dir/roadmap paths route through the canonical resolvers (+ a CI grep-gate), and workers spawn in the project rather than the keystone.
- **ADR-028 ratified (#1044)** — target orchestration architecture (folder-per-agent + two-tier ephemeral judge).

---

**Folded-in: the 1.0.1 future-state reconciliation batch** (`adr007-composite-keys-batch`). It makes the track ↔ dispatch ↔ open-item future state reflect reality *automatically* and brings the `dispatches` table into ADR-007 composite-key tenancy. Driven by `claudedocs/PRD-future-state-reconciliation-v1.1.md` (database-engineer skill under T0 governance). Cite ADR-007.

### Fabric + quality hardening (2026-07-08)

Phase-0 fabric hardening (ADR-028) and a code-health gate, from the autonomous horizon run.

- **fabric-audit `-wal`/`-shm` awareness (#1047)** — `fabric_audit.py` check A read only `.db` mtimes, so a Jun-20 `.db` with a same-day `.db-wal` reported as a safe 17-day stale relic while a connection had just opened the store. mtime alone cannot tell a leftover sidecar from a live handle, so a fresh sidecar now drives the active/stale decision and escalates to RED with a "verify with `lsof` before retiring" note. Proven empirically during the store retirement below (the audit gave a false-clean signal).
- **Repo-local state-pin gate (#1047)** — a standalone `state-pin-gate` CI job bans a repo-local `VNX_STATE_DIR=.vnx-data` pin across every shipped surface (templates, skills, docs, the T0 role — not just `scripts/`, which the pre-existing "Legacy path gate" covered). Durability guard for the #1043 footgun.
- **CI apt-flake fix (#1047)** — strip the flaky `packages.microsoft.com` apt source before `apt-get update` in every ripgrep-install step; it broke the install ~4×/night with a NOSPLIT/hash-sum mismatch.
- **Legacy shared-store retirement** — the orphaned `~/.vnx-data/state/` (last real write 2026-06-20) was moved to `~/.vnx-data/state.pre-retirement` (reversible; ADR-028 Phase-0 30-day hold) after confirming no live writer via `lsof`. `vnx fabric-audit` → **GREEN**.
- **File-size gate escalates to BLOCKING (#1048)** — `quality_advisory.py` had a "blocking" file-size threshold that only ever emitted `severity="warning"`, so monoliths (up to 3357 lines) grew unchecked. The Python hard ceiling is now 1200 (warn stays 500) and over it emits `severity="blocking"` (HOLDs `pre_merge_gate`). A `FILE_SIZE_ALLOWLIST` grandfathers every current over-ceiling source file (surfaced as a standing advisory, not a block); test files are exempt. A genuinely new monolith blocks.

### Pre-ship hardening sprint (2026-06-26)

A pre-PyPI-ship pass: five real production bugs and the future-state drift, then a stale-test and docs sweep. None change the 1.0 feature surface; they make it tip-top before publish.

- **exit_classifier audit-trail restore (#913)** — a dead-code purge had gutted `_STDERR_PATTERNS`, broken the decision-tree order, and inverted `_RETRYABLE` for INTERRUPTED/UNKNOWN, corrupting the governed coordination-events trail + retry behavior. Restored to HEADLESS_RUN_CONTRACT §4 (+ context-limit → non-retryable, auth 401/403 → non-retryable to save tokens).
- **future-state git-grounded reconcile (#914)** — the track reconciler never advanced a track to `done` from a merged PR (it read three dead-for-central-store sources). Added a 4th source (`gh pr list --state merged`, opt-in `VNX_RECONCILE_GIT`, cache-first, silent-on-failure) + multi-PR `pr_ref` parsing.
- **self-learning proposal tier revived (#915)** — `learning_loop.extract_failure_patterns` scanned a directory that never existed under the central store; pointed it at the real `t0_receipts.ndjson` so the operator-gated proposal tier (`pending_rules.json`) finally runs.
- **schema SSOT for `dispatches.output_ref`/`output_kind` (#916)** — columns referenced by code but declared in no `.sql`; declared in the canonical table (closes the schema-drift guard).
- **`report_findings` self-heal (#917)** — `ALTER ADD COLUMN` for missing columns before the `CREATE INDEX (extracted_at DESC)` so a drifted table self-heals instead of crashing.
- **`vnx_doctor` partial-setup tolerance (#918)** — `.get()` fallback for `VNX_INTELLIGENCE_DIR` so the doctor reports failures instead of `KeyError`-ing on a bare project.
- **code-anchor injection as pointers (#919)** — code anchors were silently evicted whole (item + suppression list exceeded the payload budget); now inject compact `file:line` pointers, not full bodies — cheaper, richer, and the item survives the budget. `MAX_PAYLOAD_CHARS` unchanged.
- **stale-test clusters (#920)** — test-only: aligned fixtures/expectations to current production (project_id-scoped fixtures, F54 temporal columns, the cb174793 CLI rename, dynamic ADR count).

### Added

- **ADR-007 composite-key `dispatches` rebuild (PR-A1, #859)** — schema-preserving in-place repair of the `dispatches` table to `UNIQUE(dispatch_id, project_id)`, removing every uniqueness keyed solely on `dispatch_id` (inline column, table-level, standalone index, partial index, and `lower(dispatch_id)` expression index). Canonical 12-step crash-safe rebuild: capture/restore `PRAGMA foreign_keys`, `BEGIN IMMEDIATE` with bounded retry on `SQLITE_BUSY/LOCKED`, drop+recreate dependent views/triggers verbatim, preserve the `sqlite_sequence` high-water mark, and run `foreign_key_check` + `integrity_check` before commit (abort/rollback on any violation). Tenant `project_id` is resolved **fail-closed** from a precedence chain (resolved DB path → `.vnx-project-id` marker → `VNX_PROJECT_ID`); conflicting or unknown sources abort, and existing NULL/empty/conflicting `project_id` values abort before any mutation. Never a silent `vnx-dev` default. (`scripts/migrate_future_system.py`)
- **Version reconciliation via a declarative invariant manifest (PR-A2, #861)** — a per-version (v22–v30) invariant manifest (tables; columns with type + nullability; PK ordinals; FK actions; index definitions; views) in `scripts/lib/schema_manifest.py`. A DB whose claimed `user_version` fails its invariant is downgraded to the highest version whose invariants actually hold and the missing migrations re-run; on no safe target it raises rather than guess (ADR-009).
- **Tenant-scoped canonical tracks in `build_t0_state` (PR-B, #863, R3.2)** — the canonical-track and `track_open_items` reads always carry a `WHERE project_id = ?` predicate. On unavailable tenant identity the builder returns a documented degraded fallback (`available: false`, `tenant_unavailable: true`, empty `tracks`/`open_items`) and never returns cross-tenant rows.
- **Open-item → track bridge through `tracks.py` (PR-C, #862, R4.1–R4.4)** — `scripts/import_open_items_to_tracks.py`, a thin orchestrator over the single-writer primitives `tracks.link_open_item` / `tracks.unlink_open_item` (no second SQL writer; decision D1). One run-level `BEGIN IMMEDIATE` transaction makes the read-then-write window serialized (TOCTOU closed) and the run atomic. It fails loud on an absent/unreadable/wrong-shape source (never coerced to an empty store that would close every active link), requires the migration 0030 resolution schema (`resolved_at` / `resolution_reason`) and fails closed on a pre-0030 DB, and is idempotent (`INSERT OR REPLACE` upsert — re-running yields identical rows). **D3 event semantics, documented honestly:** the DB is authoritative and the ADR-005 ledger events are emitted *after* a successful commit (at-most-once, never orphaned). A post-commit emit failure is logged loudly and is non-fatal — the DB mutation persists and the reconciler re-derives status — surfacing as CLI exit 4. Exactly-once via a transactional outbox is deferred to 1.x (#867).
- **Bridge + reconcile wired into the autopilot loop (PR-D, #871, R5/D2/D4)** — `RoadmapManager.autopilot_tick()` runs the open-item → track bridge and then `reconcile_tracks()` synchronously, under the `VNX_ROADMAP_AUTOPILOT=1` gate, before any feature-step dispatch or advance. If the track sync fails the tick returns `status: degraded` (`reason: track_sync_failed`) and refuses to advance on stale state — the downstream advance is gated on a clean sync. (`scripts/roadmap_manager.py`)
- **Bridge CLI exit codes** in `docs/EXIT_CODES.md`: `3` source missing/malformed, `4` ledger-emit failure (DB already committed), `5` resolution-schema (0030) precondition, `6` DB error.
- **Operator runtime-migration runbook** in `docs/MIGRATION_GUIDE.md` (PRD §7.2, human-gated): quiesce → WAL-safe verified backup → preflight + dry-run → migrate → backfill linkage → bridge-import → reconcile → row/schema/checksum/`integrity_check` postflight; restore-from-verified-backup and re-run on any phase failure (each phase idempotent).

### Changed

- **Test-isolation guard enforced (PR-0, #857)** — migration test modules pin `VNX_DATA_DIR` to a tmp dir; a guard refuses to open the canonical `$HOME/.vnx-data` DB in test mode, and a CI canary asserts the live DB file hash is unchanged after the full suite.
- **Kanban / state-builder honesty (PR-E, #858)** — `build_t0_state` catches only enumerated pre-migration missing-table/column cases; any other `OperationalError` (locked/malformed) sets a `health=degraded|failed` field and a non-zero exit instead of a silent legacy fallback. Artifact-read failures are recorded with the dispatch id (work is not dropped) and flag the build degraded; the active-dispatch count is de-duplicated across dir and `.md` forms.
- Kimi default per-chunk stall threshold raised 300s → 600s (#860).
- Worker-role skills pre-approved in settings so detached lanes don't stall on skill-permission prompts (#872).
- Roadmap updated with the local-model PM-gate-automation plan and an honest future-state batch status (#873).

### Known issues / roadmap (1.x)

Filed and tracked in `ROADMAP.yaml`; not part of this batch:

- **#864** — broader ADR-007 composite-key batch across the SPC / intelligence tables (separate from this dispatches/tracks migration).
- **#866** — event-stream-primary measurement (normalized NDJSON of all tool-calls as the primary measurement substrate).
- **#867** — open-item bridge exactly-once via a transactional outbox (supersedes the current at-most-once post-commit events).
- **#868** — governance observability.
- **#869** — operator-runbook automation (automating the §7.2 runtime-migration runbook).
- Additional follow-ups **#865**, **#870**, **#874** are filed against the 1.x line.

## [1.0.0] — 2026-07-02

The 1.0.0 release, published to PyPI (`pip install vnx-orchestration`) and tagged
`v1.0.0`. Everything below is the rc9 → 1.0.0 delta; the rc-series entries that
follow document the road there.

### Added

- **Realistic benchmark methodology (repo-only)** — field-tests harness with production-derived tasks, programmatic verification per task, LLM-judge fallback, and cost per quality-point; codex lane added and provider-agnostic skill injection verified end-to-end on all 6 lanes (#828, #830, #831). Lives under `scripts/benchmark/field-tests/`; deliberately **excluded from the wheel** (#832) — task seeds are repo-specific, a generalised bring-your-own-tasks version is planned for 1.1 (OI-225).
- **Smart Lanes foundation** — local Gemma e4b via MLX with package structure (`[local-gemma]` extra), Smart Router cost-tier classifier (flag-gated, default-off) (#813), `quality_tier` discriminator with per-task min/max gates (#822).
- **Planning / future-state layer (ships dark)** — tracks seeder + horizon views + `vnx objective list` (#787), deliverable plane with proposed→ready human gate (#790), planning kanban in the dashboard (#791), advisory rollup reconciler that never auto-writes ROADMAP (#793), dispatch→track linkage backfill (#801), human-gated objective sync (#800), `track_type` + `next_action_owner` discriminator (#803).
- **Governance hardening** — `/pending` dispatch-path enforcement closes the T0 direct-call bypass (#811), profile-gate resolver active in `request_reviews()` (#804), worker-permission relay with operator auto-accept window + catastrophic hard-list (#799), OI bulk pattern subcommands + 1.0 closing sprint (96→48 open items) (#812).
- **Digest architecture V2** — `atomic_io.py` + ADR-021 exception discipline (#816), progress-table + minimal digest skeleton (#817).
- **OI-lifecycle closure** — `vnx track done`, `vnx oi-close`, dispatch-to-track linkage backfill, and `vnx status --tracks` added; coordinates track completion with open-item closure in a single governed action (#849).
- **dispatch_metadata backfill tool** — `vnx dispatch_metadata` subcommand backfills `outcome`, `model`, `provider`, `tokens`, and `cost_usd` from receipts into the dispatch register; `contract_invalid` vocabulary synced at gate-F2 (#847).

### Fixed

- **Bench seed decontamination** (#831) — task seeds no longer contain solutions and the scorer no longer reads repo-root state; `tests/test_bench_seed_integrity.py` guards that every verifier fails on the bare repo.
- **Wheel hygiene** (#832) — benchmark dev-tooling (incl. a planted-flaw `sk-live` fixture string and a binary DB fixture) excluded from the artifact: 0 benchmark files, 2.3 MB, fresh-venv install verified.
- Receipt dedup per dispatch_id keeps best status (#808); dispatcher survives scans that reject all dispatches (`set -e` leak) with observable rejection (#806); self-learning loop controls for task difficulty in model inference (#805); claude-spawn captures `completion_text` from stream-json (#821); smart-router null-cost sort collapse (#818); `_dispatch_gemini` respects `--model` (OI-155, #823); uniform central-path resolution (OI-126, #819); four regressed nightly intelligence phases repaired (OI-2331, #792); hook-driven version-agnostic tmux lane signals (#798); reconciler derives done from `track.pr_ref` instead of the legacy A/B/C join (#802).
- **Audit-chain verify** now correctly distinguishes an unchained (virgin) ledger from a broken/corrupt one; previously both returned the same error code (LB-5, #840).
- **Schema-init view-ordering** on legacy DBs unblocked v22/v23 migration failures — SQLite view dependency order now enforced during schema bootstrap (nightly phase-0 failure mode 2, #842).
- **Observability path resolution** unified across `state/` and `events/`; `events_path` field added to receipt pointer so consumers locate the correct per-dispatch event archive (H2, #843).
- **Kimi lane/constraint conflict** resolved — constraint file no longer marks the kimi CLI lane as violating; raw-spawn guard generalized to protect against uncontrolled provider CLI spawns on all non-claude lanes (#844).
- **tmux-lane receipts** now emit truthful completion status and timestamps; extra-flags argument handling rewritten with `shlex.split` to eliminate quoting edge cases (H3/H5, #845).
- **Dispatch broker atomicity** — orphan dispatch window and `claim_next` TOCTOU races closed; adapter pipe hygiene ensures `SIGPIPE` does not silently swallow worker output (H1/H6, #848).
- **Self-learning duplicate-dominance** — injection history now suppresses patterns that dominated past injections even when their raw score is high; root cause of 93% duplicate injection rate resolved (#850).

### Changed

- tmux-spawn documented as the default dispatch lane for parallel independent work; subprocess-dispatch reserved for terminal-pinned work (#824, #825).
- README benchmark claim rewritten from package feature to repo methodology (#832); roadmap privacy trim moved operational detail to private state (#827).
- **Docs truth-pass for 1.0 launch** — version labels corrected, ADR provenance added, surface sync between README/ROADMAP/CHANGELOG and shipped code completed (#846).

## [1.0.0-rc9] — 2026-05-26

### Added

- **feat(governance) GOV-3 (#655)** `scripts/traceability_audit.py` — re-runnable observability tool that cross-references PRs/commits/dispatches/receipts and reports traceability gaps. Four gap categories (A–D): dispatches without completion receipt, receipts with unresolvable dispatch_id, merged PRs without receipt cross-reference, completion receipts missing both pr_id and dispatch_id link. Supports `--since`/`--until` date range, `--repo PATH` override, atomic markdown output. 44 unit tests. First run against vnx-dev (2026-01-01 → 2026-05-26): Category C gaps at 6.0% (most PRs linked via branch-slug heuristic); Categories A/B/D reflect tmux-era receipt schema predating current linkage fields.
- **feat(governance) GOV-2 (#654)** `scripts/pr_merge.py` canonical T0 merge path: instead of raw `gh pr merge`, T0 calls this script which merges the PR and atomically emits a `pr_merged` receipt (pr_number, dispatch_id, conclusion, merge_method) to `t0_receipts.ndjson` + `dispatch_register.ndjson`. `scripts/backfill_pr_merged_receipts.py` reconciles already-merged PRs against existing receipts; idempotent, supports `--dry-run` / `--limit` / `--since`. Fixes FPY/history gap: previously merged PRs left no governance trail.

### Fixed

- **fix(provider-dispatch) CL1 (#644)** Non-claude provider dispatch (kimi/codex) now captures correct `status`, `output`, and `tokens` in completion receipts. Receipt fields were being dropped for non-claude cheap lanes.

### Changed

- **feat(subprocess-dispatch) CL2 (#652)** Cheap-lane execution routes through `provider_dispatch` instead of Claude fallback. Subprocess dispatch now uses the provider-agnostic entry-point for cheap-lane tasks — removes the hardcoded Claude-only path and enables kimi/codex/gemini as cheap-lane workers.
- **refactor(providers) CL3 (#643)** Constraint renamed: `deepseek-path-d-blocked` → `deepseek-harness-subscription-blocked`. Semantics expanded: own-key + hardening path (`ANTHROPIC_API_KEY` + `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` + MCP off) is now explicitly **allowed**; subscription-redirect (no own key, rides OAuth subscription) remains blocked. Measured on claude v2.1.150: 0 calls to `api.anthropic.com` with own-key + hardening.

### Refactored

- **refactor(quality-db) OI #645** `bootstrap_qi_db` extracts migration registry (OI-1542/1544/1541) — migration blocks now indexed, removes inline migration sprawl.
- **refactor(benchmark) OI #646** Extract source-info + report-writers from benchmark main (OI-1510) — cuts benchmark script below size threshold.
- **refactor(dispatcher) OI #647** Extract stuck-cleanup python + supervisor-ticks from dispatcher (OI-1521/1523).
- **refactor(receipt-proc) OI #648** Extract mtime-calc python from bootstrap-protection (OI-1525/1524).
- **refactor(doctor) OI #649** Extract worktree + settings checks from `cmd_doctor` (OI-1573).
- **refactor(install-central) OI #650** Move shim content to template file (OI-1562) — shim generator no longer embeds multi-line heredoc inline.
- **refactor(migrate-central) OI #653** Extract `migrate_import` module from `migrate_to_central_vnx.py` (OI-1537/1539, part 1/3).
- **refactor(migrate-central) OI #657** Extract `migrate_schema` module from `migrate_to_central_vnx.py` (OI-1536/1533, part 2/3).

## [1.0.0-rc3] — 2026-05-20

### Added
- chore: bump version to 1.0.0-rc3; Wave 2a centralisation milestone
- feat(env): Wave 2a feature flag block in `vnx.env.example` (VNX_USE_CENTRAL_DB, VNX_RUNTIME_PRIMARY, VNX_CANONICAL_LEASE_ACTIVE, dormant Wave 5/6 flags)
- docs: dry-run manifest + rapport voor 4-project centralisatie (`claudedocs/wave2a-dag1-dry-run-2026-05-20.md`); 1,891,733 rijen gescand, 0 read errors, risico-classificatie per project

## [1.0.0-rc1+wave7] - 2026-05-17

Multi-provider milestone. 5 providers in production with provider-agnostic governance, intelligence injection, and end-to-end token + cost tracking. Reproducible 49-dispatch benchmark suite ships with routing recommendations.

### Added — Wave 7: Multi-Provider via LiteLLM (PR #515-#520, #531, #536, #545, #550-#552)

- **PR-7.0 (#515)** ADR-015 LiteLLM Path B for DeepSeek/Kimi/GLM integration freeze
- **PR-7.1 (#516)** DeepSeek V4 lane via LiteLLM subprocess bridge (V4-Pro + V4-Flash)
- **PR-7.2 (#517)** Kimi K2.6 + K2-0905 lane via LiteLLM Moonshot endpoint
- **PR-7.3 (#518)** GLM-5.1 lane via OpenRouter (z.AI direct deferred)
- **PR-7.4 (#519)** Cost-routing policy engine (feature-flag gated)
- **PR-7.5 (#520)** Provider behavior contracts (capabilities + tool-shape + cache-control)
- **PR-7.6 (#536)** Provider governance unification — uniform receipt + unified report shape for all 5 providers (claude/codex/gemini/litellm/kimi)
- **PR-7.7 (#550)** Kimi CLI as 5th provider — OAuth via `kimi login`, no API key required (Anthropic-compatible stream-json output)
- **PR #531** `vnx.env` loader + DeepSeek V4-Pro/V4-Flash model registry
- **PR #545** OI cleanup group 2 — LiteLLM usage stream + unified report `.md` suffix
- **PR #551 (P0-A)** Intelligence injection unification — codex/gemini/litellm equal first-class with claude
- **PR #552 (P0-B)** Token usage + cost tracking end-to-end for all 5 providers; OI-1489 streaming drainer accepts `usage_complete` event

### Added — Wave 6: Workers=N Elastic Pool (PR #534-#544, #546)

- **PR-6.0 (#534)** ADR-018 elastic worker pool design freeze
- **PR-6.1 (#535)** `vnx_workers.yaml` + `WORKER_REGISTRY` (ADR-013 implementation)
- **PR-6.2 (#538)** Schema v14 elastic worker pool tables + migration scripts
- **PR-6.3 (#539)** `PoolManager` core (decision engine + state repo + manager)
- **PR-6.4 (#540)** Pluggable scaling policies (`queue_depth_v1` + `cost_aware_v1`)
- **PR-6.5 (#541)** Provider-mix per pool with lowest-share-first allocation
- **PR-6.6 (#542)** Health monitoring + dead-worker reap (tick cycle: reap → decide → execute)
- **PR-6.7 (#543)** `vnx pool` CLI (`status`/`scale`/`config`/`reap` subcommands)
- **PR-6.8 (#544)** Control Centre pool integration (cross-project pool view + supervisor)
- **PR #546** OI cleanup group 1 — idempotency + regex + ledger + audit fixes
- **PR #537** OI-1479 — token_usage extraction + cost_usd computation per provider

### Added — Wave 5: Control Centre + Multi-Project (PR #521-#532)

- **PR-5.0 (#521)** ADR-017 Control Centre product-shape architecture
- **PR-5.1 (#522)** Multi-project state aggregator write-pad
- **PR-5.2 (#525)** Per-project T0 lifecycle management (spawn/heartbeat/kill/reap)
- **PR-5.3 (#523)** Multi-tenant lease isolation (schema v12)
- **PR-5.4 (#524)** Cross-project intelligence aggregator (global + per-project facets)
- **PR-5.5 (#528)** Control Centre CLI shell skill + operator commands
- **PR-5.6 (#530)** Hybrid dispatch routing with receipt-tail lifecycle tracker
- **PR-5.7 (#532)** Operator demo runbook + Control Centre docs + completion report
- **PR #533** OI-1476 — align `project_id` regex + YAML placeholder substitution

### Added — Benchmark Infrastructure (PR #547, #548)

- **PR #547** Benchmark suite infrastructure — 9 models × 7 task-classes orchestrator + judge + analyzer (`scripts/benchmark/`)
- **PR #548** 56-dispatch model comparison results + routing recommendations (`scripts/lib/providers/routing_recommendations.yaml`)

Result summary (49 valid dispatches):
- DeepSeek V4-Flash: $0.0006/dispatch, 7.3/10 — cost+speed winner (198× cheaper than Opus 4.6)
- Kimi K2.6: 8.1/10 — top-tier quality, 21× cheaper than Opus
- GLM-5.1: 8.0/10 — top-tier quality, 24× cheaper, fastest top-tier (100s vs Kimi's 215s)
- Opus 4.6: 8.2/10 — highest cost, marginal quality lead

### Added — Wave 4.6: Provider Dispatch Generalization (PR #488, #490, #510-#513)

- **PR-4.6.1 (#488)** `scripts/lib/provider_dispatch.py` — provider-agnostic dispatch entry-point (`--provider {claude,codex,gemini,litellm:<model>}`)
- **PR-4.6.2 (#490)** `claude_spawn` extracted from `subprocess_dispatch` (byte-identical)
- **PR-4.6.3 (#511)** `codex_spawn` handler extracted from `codex_adapter`
- **PR-4.6.4 (#510)** `gemini_spawn` handler extracted from `gemini_adapter`
- **PR-4.6.5 (#512)** `litellm_spawn` handler extracted from `litellm_adapter`
- **PR-4.6.6 (#513)** Unified event shape via `CanonicalEvent` + `EventStore` enforcement

### Refactored

- **intelligence_selector.py** (2026-05-17, in flight) — 2511 LOC monolith split into `intelligence_sources/` package (9 modules, target ~321 LOC main + sources)
- **conversation_analyzer (#504)** — modularized into package; closes OI-1438/1439/1440/1441/1442
- **replay_harness (#506)** — modularized into package; closes OI-1443/1444/1445/1446/1447
- **cleanup_worker_exit (#507)** — decompose 104-line function; closes OI-1448

### Hardened — Silent-except narrowing (OI-1437, PR #491-#500, #508, #509)

- 14 PRs converting bare `except:` and overly broad `except Exception:` patterns to specific exception types with `logger.warning` across hot files (build_t0_state, intelligence_selector, gather_intelligence, learning_loop, api_intelligence, dispatch_register, append_receipt payload, api_operator, session_resolver, conversation_analyzer, replay_harness, cleanup_worker_exit, plus 13 singleton files, plus 7 hot files). Total ~120 silent-except sites converted to instrumented warnings.

### Fixed

- **OI-1489** (in flight) — Streaming drainer drops `usage_complete` event; 1-line fix re-enables provider-agnostic token telemetry end-to-end
- **OI-1450/1451/1452 (#503)** — Receipt processor bootstrap audit ordering + test infra hardening
- **dispatcher (#502)** — log stderr, fix `script_dir` leak, receipt processor bootstrap-mode
- **ADR-003 (#505)** — clarify API-key + CLI permitted; SDK still banned

### Added — CONTRIBUTING (#489)

- `CONTRIBUTING.md` + CI lint gate enforcing atomic-write and silent-except policies

## [1.0.0-rc1] - 2026-05-09

Architectural stabilization milestone. 14 ADRs locked. Central VNX state proven on real production data (855k snippets across 4 projects, 0 verifier discrepancies). CI gate enforces OAuth-only Claude routing. Smart context injection validated at +30 percentage-point dispatch quality lift on 658 outcome-tagged dispatches.

From this release forward, dispatch envelope, receipt schema, NDJSON ledger format, and ADR-locked invariants are backwards-compatibility-honoring.

### Added — ADR backfill (10 new ADRs, 003-014)

- ADR-003 OAuth-only Claude routing via `claude -p` subprocess (no SDK, no API key)
- ADR-004 VNX positioning: self-hosted alternative to Anthropic Managed Agents
- ADR-005 Append-only NDJSON audit ledger as primary orchestration substrate
- ADR-006 Mandatory staging→promote with human approval gate
- ADR-007 Multi-tenant `project_id` stamping with composite UNIQUE rebuilds
- ADR-008 Dual-LLM adversarial review (codex_gate + gemini_review) with `contract_hash` evidence binding
- ADR-009 Schema-first migrations via PRAGMA introspection
- ADR-010 Subprocess adapter (`claude -p`) as canonical Claude routing
- ADR-011 Manager+worker hierarchy with explicit depth>1
- ADR-012 Hybrid interactive+headless (no retire-interactive)
- ADR-013 Worker pool size as configuration (workers = N)
- ADR-014 Autonomous mode = pre-approved chain dispatch with SHA-256 chain-spec hash as consent token

### Added — Structural enforcement

- CI gate `ADR-003: No Anthropic SDK Imports` blocks any `import anthropic` / `from anthropic` / `import claude_agent_sdk` in `scripts/`, `dashboard/`, `tests/`

### Added — Wave 1 shadow-mode read cutover (PR #450-#454)

- `shadow_verifier.py` — independent comparator with 6 zero-tolerance divergence metrics
- `shadow_logger.py` NDJSON writer + CLI + flock-rotation
- T0 state-builder + IntelligenceSelector + DispatchRegister + Dashboard shadow wiring across 13 read sites
- Canary divergence test pack (14+ fixtures) + operator-readable rollback procedure

### Added — Wave 5 smart-context injection (PR #455-#461)

- Prior-round-findings injection (W5.0)
- ADR injection by file-touch (W5.1)
- Code anchor injection (W5.2)
- Operator memory injection (W5.3)
- Schema introspection injection (W5.4)
- Production plumbing for P0-P4 context-bundle classes (W5.5)

### Added — Wave 4 OTel observability foundation (PR #468)

- Opt-in OpenTelemetry export wired into `subprocess_dispatch` completion. Emits `dispatch_completion_count` metric + spans. Env-gated via `OTEL_EXPORTER_OTLP_ENDPOINT`; no-op when unset.

### Added — Wave 4.5 provider parity (PR #471, #472, #477, #479)

- `PromptAssembler` provider-agnostic methods (claude/codex/gemini/litellm)
- Codex + Gemini adapters use `PromptAssembler`; `AGENTS.md` + `GEMINI.md` tri-file activated by `vnx init` bootstrap
- Gate reviewer prompts use `gh pr diff` authoritative source
- Intelligence injection per-provider with empty-`dispatch_id` guard (audit-safe)

### Added — Wave 2 package extraction foundation (PR #469, #478)

- `pyproject.toml` + `vnx_core` + `vnx_cli` package skeleton with smoke tests
- First module migration: `function_size_gate.py` → `vnx_core` with `sys.path`-fallback shim

### Fixed — OI-1370 systemic locking refactor (PR #482-#486)

- Original `migrate_phase3_envelope` race (writer pre-rename appends to unlinked inode) required system-wide locking refactor across all writer paths
- `scripts/lib/state_writer.append_locked()` helper with sentinel registry; 100-thread × 100-write concurrency test passes
- 4-PR migration of all envelope/state writers to helper
- All four implementation PRs (#483-#486) implemented by **Codex CLI workers** — first production codex-worker dispatches in this codebase

### Fixed — Security + governance

- **OI-1369 (#465)** Path traversal in `vnx_paths.resolve_central_data_dir` — strict regex `^[a-z][a-z0-9-]{1,31}$`
- **OI-1294 (#467)** `compact_open_items_digest` function-size 76→34 via mechanical helper extraction
- **OI-1415 (#462)** `review_contract.content_hash` backward-compat for empty `deleted_files`

### Added — Repo hygiene (OI-1373 cleanup)

- 5-tier OI-1373 cleanup: 49 strategic/business docs moved from public `roadmap/`+`docs/internal/` to gitignored `claudedocs/`
- Pattern: filesystem `mv` + `git add -u` (NOT `git mv`) — preserves files locally on disk while removing from git tracking

## [0.10.0] - 2026-04-30

Chain summary: 27 PRs landed across governance hardening, headless audit parity, supervisor pack, CFX thematic refactors, P0 intelligence loop fixes.

### Added — State self-maintenance

- `compact_state.py` + `install_nightly_crons.sh` (#299, #313): auto-rotate intelligence_archive (7d), receipts cap (10k), open_items_digest (>30d evict)

### Added — Headless audit parity (40% → 90%)

- `instruction_sha256` in manifest + receipt (#309): cryptographic reproducibility
- `WorkerHealthMonitor` STUCK → EventStore + receipt `stuck_event_count` (#310)
- Codex+Gemini token tracking via `adapter.get_token_usage()` (#307)
- Canonical gate result schema with `gate_status.is_pass()` (#322)

### Added — Real-time observability

- `/api/register-stream` SSE endpoint (#304): dispatch lifecycle stream

### Added — Supervisor pack (auto-respawn)

- `cleanup_worker_exit` single-owner exit cleanup (#315)
- `receipt_processor_supervisor.sh` wrapper-respawn (#319)
- `lease_sweep` + dispatcher prelude tick (#316)
- `runtime_supervise` + 60s tick (#317)
- Operator guide `docs/operations/UNIFIED_SUPERVISOR.md` (#318)

### Added — Frontend regression protection

- Playwright visual regression suite (#312)
- `tsc` strict + `npm typecheck` (#306)
- Playwright network failure scenarios (#308)
- Console error detection per route (#305)

### Improved — Codex review intelligence

- Severity prompt tightening (#323, #324): `error` reserved for data loss / false closure / security; ~75% reduction in blocking findings noise

## [0.9.0] - 2026-04-11

Streaming + autonomous loop + A/B test milestone.

### Added

- **F42 PR-1** Restore EventStore from git history + dashboard archive endpoints for historical dispatch event retrieval
- **F42 PR-2** Headless T0 decision loop — decision parser extracted from replay harness, decision executor with 5 decision types and loop guards, trigger wiring for closed autonomous loop
- **A/B Test** First systematic comparison of interactive vs headless execution across F40 (moderate) and F42 (complex). Finding: headless produces functionally equivalent output with ~4% less LOC and ~18% fewer tests. Conclusion: execution mode does not determine quality — instruction quality does.

## [0.8.0] - 2026-04-11

Headless intelligence + governance profiles milestone.

### Added

- **F39** Headless T0 benchmark — decision framework with deterministic pre-filter (Level-1: 100%, Level-2: 73-87%, Level-3: 67-78%), context assembler, replay harness, file-based gate locks (#204)
- **F41** Intelligence pipeline activation — governance aggregator backfill (722 metrics, 58 SPC control limits), nightly pipeline scheduling via launchd, quality digest with real SPC data (#206)
- **F41** 3-layer headless trigger system — file watcher on unified_reports, silence watchdog (10-min stale lease/dispatch detection), optional haiku LLM triage (#206)
- Headless dispatch writer — programmatic dispatch creation for autonomous T0 orchestration (#207)
- Governance profiles — config-driven review profiles (default/light/minimal) replacing hardcoded business/coding split, configurable via `.vnx/governance_profiles.yaml` (#207)

## [0.5.0] - 2026-03-30

Governance Runtime Upgrade. Largest upgrade since initial public preview. One-command worktree lifecycle with deterministic gates, governance-aware finish flow, hardened dispatcher/tmux delivery, intelligence export/import + self-learning loop, token/model tracking in receipts, dashboard attention model + event timeline, Codex CLI + multi-model orchestration improvements, configurable per-terminal models, Opus 4.6 1M default.

## [0.1.0] - 2026-02-22

Initial public preview release of VNX.
