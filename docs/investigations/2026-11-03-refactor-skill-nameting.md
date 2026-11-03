# Nameting — refactor-skill-en-levering: de zeven doelpunten op main

**Dispatch-ID**: dlv-c4bf5ed70ec7
**Model**: deepseek-flash
**Provider**: deepseek-harness
**Measured on**: `origin/main` = `72eef76e` (`feat(rs3): the receipt proves the skill reached the worker (#2322)`, merged 2026-11-03T12:38:54+01:00), branch `dispatch/dlv-c4bf5ed70ec7`
**Date**: 2026-11-03
**Source plan**: `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/refactor-skill-en-levering.md`, sections "Doel" (lines 5-17), "Receiptsemantiek `skill_applied`" (lines 42-57) and the amendment on DoD 1 (line 17). `claudedocs/` is gitignored (`.gitignore:121`), so the plan is cited by section name and not shipped in this PR.
**Store**: `~/.vnx-data/vnx-dev/state`, read-only (ledger `t0_receipts.ndjson`, 40,952 lines at measurement time)

This document measures each of the seven DoD points against main after RS-3 merged. It changes no code: the only changes are this file and its changelog fragment. Every verdict is backed by a command in this document with its exit code.

## The track's merged PRs (the ground for every verdict)

| PR | Delivery | Merge | What it delivered for this measurement |
|---|---|---|---|
| #2227 | RS-1 `dlv-3be21970fcda` | merged before `72eef76e` | `stage_spec_bundle(skill=...)` writes the slug and refuses an unknown one |
| #2243 | RS-2 `dlv-42d6245b3056` | merged before `72eef76e` | `skill_context` seam; every lane passes `skill` to it |
| #2322 | RS-3 `dlv-75df89432a80` | `72eef76e` | the receipt fields `skill_applied` / `skill_source_path` / `skill_unapplied_reason` |
| #2258 | RS-4 `dlv-0101f84b635d` | merged before `72eef76e` | the dead `.claude/skills/<role>/CLAUDE.md` role tier removed |
| #2226 | RS-5 `dlv-77f09ad29fee` | merged before `72eef76e` | `skills/refactor-safe/SKILL.md`, its fleet copy, the registry entry |
| #2225 | RS-6 `dlv-bb5dcfc182bd` | merged before `72eef76e` | ADR-048 and `docs/core/DISPATCH_RULES.md` §15 |

## Table — the seven points

| # | Point (plan, "Doel") | Evidence command | Proving output | Exit | Verdict | Owner |
|---|---|---|---|---|---|---|
| 1 | A staged spec carries `skill`; an unknown skill is refused at staging | `python3 -m pytest tests/test_dispatch_skill_staging.py -q` | 16 passed. `test_skill_under_canonical_tree_lands_in_the_spec` and `test_skill_under_claude_tree_lands_in_the_spec` assert `payload["skill"] == _SKILL` on the written `dispatch-spec.json`; `test_unknown_skill_is_refused_before_anything_is_written` expects `UnknownSkillError` before a bundle exists | 0 | **met** | RS-1 (#2227) |
| 2 | Every lane (envelope, provider, pool) injects the bound skill through one shared seam, never for a reviewer | `python3 -m pytest tests/test_skill_injection_seam.py tests/test_pool_worker_runner.py tests/test_d1_reviewer_no_injection.py -q` | 19 + 20 + 20 passed. Seam `skill_context._inject_skill_context` (`scripts/lib/skill_context.py:139`); callers `scripts/lib/envelope_prepare.py:86` (envelope), `scripts/lib/provider_dispatch.py:375` (provider), `scripts/lib/pool_worker_runner.py:216` → `subprocess_dispatch_internals/delivery.py:132` (pool). Reviewer suppress: `test_reviewer_role_never_receives_the_skill` (both reviewer roles) and `test_d1_reviewer_no_injection` | 0 | **met** | RS-2 (#2243) |
| 3 | The receipt carries `skill_applied` and `skill_source_path`; a dispatch without a skill carries neither (absent, not `false`) | `python3 -m pytest tests/test_skill_applied_receipt.py -q`; `git grep -n "lane_without_seam" -- scripts/` | 13 passed: 4 contract rows, absent-fields-on-no-skill for both lanes, fail-closed `False` stamp, and the seam-coverage pin. `git grep` for `lane_without_seam` in production code: 0 matches, exit 1 | 0 / **1** | **partly met** — four rows met; `resolution_failed: lane_without_seam` is named in ADR-048 §3 and DISPATCH_RULES §15 but emitted by no code path; `skill_receipt_fields` has no direct unit test | RS-3 (#2322) — §3 |
| 4 | The dead `.claude/skills/<role>/CLAUDE.md` tier is gone from role resolution | `git grep -n "skills/" -- scripts/lib/role_application.py scripts/lib/skill_context.py`; `python3 -m pytest tests/test_role_applied_provider_lane.py tests/test_subprocess_dispatch_f34.py -q` | 5 matches, all comments saying the tier is gone; no candidate is yielded. `_candidate_sources` yields exactly `prompt_assembler`, `agents`, `terminal` (`test_candidate_sources_no_longer_offer_the_dead_skills_tier`); `test_inject_ignores_a_skills_role_file` proves a planted `.claude/skills/<role>/CLAUDE.md` never reaches the prompt. 24 + 14 passed | 0 | **met** | RS-4 (#2258) |
| 5 | `skills/refactor-safe/SKILL.md` and `.claude/skills/refactor-safe/SKILL.md` are byte-identical and listed in `skills/skills.yaml` | `cmp skills/refactor-safe/SKILL.md .claude/skills/refactor-safe/SKILL.md`; `shasum -a 256`; `sed -n 138,142p skills/skills.yaml`; `python3 -m pytest tests/test_refactor_safe_skill.py -q` | `cmp` exit 0; both 7033 bytes, sha256 `dcd6a2fd…f42944`; `skills.yaml:138-142` lists `refactor-safe` → `file: refactor-safe/SKILL.md`; 6 passed (`test_canonical_skill_ships`, `test_fleet_copy_is_byte_identical`, `test_registry_lists_refactor_safe`, `test_body_names_the_required_rule`) | 0 | **met** | RS-5 (#2226) |
| 6 | ADR-048 records skill not role, `.agents/` stays generated, and the `skill_applied` contract | `grep -n "^#" docs/governance/decisions/ADR-048-refactor-skill-levering.md`; `git ls-files .agents` | `22:### 1. Een skill, geen nieuwe rol`; `32:### 3. Receiptsemantiek van \`skill_applied\``; `51:### 5. \`.agents/\` blijft gegenereerde scaffolding`. `.agents/` is gitignored (`.gitignore:142`) and `git ls-files .agents` returns 0 files, while `agents/<role>/CLAUDE.md` is tracked | 0 | **met** | RS-6 (#2225) |
| 7 | Five dispatches with `skill=refactor-safe` over the lanes carry `skill_applied=true` with a real `SKILL.md` path and `role_applied=true` | `python3 scripts/receipt_query.py since "2026-11-03T12:38:54+01:00" --state-dir ~/.vnx-data/vnx-dev/state`; `python3 scripts/receipt_query.py by-dispatch <id> …` | 3 receipts since the merge, none carrying a spec `skill`; 0 of 5 measured. All 21 staged specs that carry `skill=refactor-safe` fired **before** `72eef76e`, so their receipts predate the fields — see §7 | 0 | **waits on cohort** (0 of 5) | measurement (T0), plan "meting" row |

## 7. Cohort — dispatches with `skill` in the spec since the RS-3 merge

### 7a. After the RS-3 merge (`72eef76e`, 2026-11-03T12:38:54+01:00)

```
$ python3 scripts/receipt_query.py since "2026-11-03T12:38:54+01:00" --state-dir ~/.vnx-data/vnx-dev/state
3 receipt(s) since 2026-11-03T12:38:54+01:00 (project vnx-dev):
  ? dlv-75df89432a80 [success] schema_version=2 pr=-
  ? ? [?] schema_version=2 pr=-
  ? ? [?] schema_version=2 pr=-
```

| Dispatch | Lane | `skill_applied` | `skill_source_path` | `skill_unapplied_reason` | `role_applied` | Path on disk? |
|---|---|---|---|---|---|---|
| `dlv-75df89432a80` | T0 `state_mutation` (RS-3's own merge receipt) | absent | absent | absent | absent | no `skill_source_path` |
| (anoniem, `state_mutation`) | T0 | absent | absent | absent | absent | no `skill_source_path` |
| (anoniem, `state_mutation`) | T0 | absent | absent | absent | absent | no `skill_source_path` |

All three are T0 governance/state-mutation receipts written by the merge. None is a dispatch, none carries `skill` in its spec, and none carries any of the three new fields.

**Count per lane after the merge:** provider 0, envelope 0, pool 0. **Total with `skill_applied=true`: 0 of the required 5.**

### 7b. Why the cohort is empty (the 21 staged specs)

`git`-independent scan of `~/.vnx-data/vnx-dev/dispatches/*/*/dispatch-spec.json` (read-only) finds **21** staged specs with `skill: "refactor-safe"`, every one `provider: deepseek-harness`, `model: deepseek-flash`, `role: backend-developer` — i.e. the **provider lane only**:

```
$ python3 - <<'EOF'   # read-only scan, prints the 21 spec paths + provider/model/role
... 21 specs with non-null skill
('pending/dlv-20e9ed688893/dispatch-spec.json', 'refactor-safe', ..., 'deepseek-harness', 'deepseek-flash', 'backend-developer')
... (20 more)
EOF
```

Cross-checking those 21 ids against the ledger (`receipt_query.py by-dispatch` per id, plus a read-only parse of `t0_receipts.ndjson`): every one has receipts, and **every receipt's last timestamp is before 2026-11-03T12:38:54+01:00** — the latest is `2026-11-03T10:53:15Z` (`dlv-83abb30569bd-r2`), 1h45m before RS-3 merged. Their receipts carry **none** of `skill_applied` / `skill_source_path` / `skill_unapplied_reason`:

| Dispatch | Last receipt | `skill_applied` | `skill_source_path` | `skill_unapplied_reason` | `role_applied` |
|---|---|---|---|---|---|
| `dlv-0caf1cccbcc4` / `-r2` | 2026-10-28T02:31:26Z / 2026-10-28T05:04:06Z | absent | absent | absent | absent |
| `dlv-20e9ed688893` / `-r2` | 2026-10-28T00:36:27Z / 2026-10-28T02:14:58Z | absent | absent | absent | absent |
| `dlv-279f054c2d3b` / `-r2` | 2026-10-28T01:40:13Z / 2026-10-28T02:32:16Z | absent | absent | absent | absent |
| `dlv-42de4cf2f1c7` / `-r2` | 2026-10-28T01:43:42Z / 2026-11-03T10:37:02Z | absent | absent | absent | absent |
| `dlv-5c477e89337e` / `-r2` | 2026-10-28T01:45:14Z / 2026-11-03T10:28:55Z | absent | absent | absent | absent |
| `dlv-6477c84d40bb` | 2026-10-27T21:35:07Z | absent | absent | absent | absent |
| `dlv-729397a63308` | 2026-10-27T20:46:15Z | absent | absent | absent | absent |
| `dlv-83abb30569bd-r2` | 2026-11-03T10:53:15Z | absent | absent | absent | absent |
| `dlv-9cae7e00a9c2` | 2026-11-03T10:51:09Z | absent | absent | absent | absent |
| `dlv-d8c303d77c53` | 2026-10-27T21:58:48Z | absent | absent | absent | absent |
| `dlv-f097b3b7f084` | 2026-10-28T04:19:35Z | absent | absent | absent | absent |
| `dlv-fa8064ffd4cf` / `-r2` | 2026-10-27T20:16:55Z / 2026-10-27T22:05:53Z | absent | absent | absent | absent |
| `dlv-fd2200f312e2` | 2026-10-28T06:05:04Z | absent | absent | absent | absent |
| `dlv-fd2f251033a3` / `-r2` | 2026-10-27T20:18:05Z / 2026-10-27T22:01:09Z | absent | absent | absent | absent |

**Count per lane over all staged specs:** provider 21 (all of the above), envelope 0, pool 0.

**Verdict: `waits on cohort`, 0 of 5.** This is not a failure of the seam: the fields did not exist when those dispatches ran. It is also not a failure of the lanes: per the operator rule for this sprint only the provider lane builds (DeepSeek; no claude builder), so the envelope and pool lanes have **no cohort to measure** at all. Five post-merge `skill=refactor-safe` dispatches — including at least one on envelope or pool, or an explicit operator decision to measure provider-only — are needed before point 7 can be `met`.

## 3. Point 3 — what is missing

The DoD sentence is met: the envelope and provider receipts stamp `skill_applied` / `skill_source_path`, and a spec without a skill keeps all three fields **absent** (`test_envelope_receipt_omits_all_three_fields_without_a_skill`, `test_provider_receipt_omits_all_three_fields_without_a_skill`, `test_receipt_schema_omits_all_three_when_none_but_stamps_a_false_verdict`). The four contract rows each have a passing test:

| Row | Test | `skill_applied` | `skill_unapplied_reason` |
|---|---|---|---|
| no `skill` | `test_no_skill_returns_none_and_stamps_nothing` | `None` (no verdict) | — |
| body literally in the prompt | `test_resolved_skill_body_in_prompt_is_true_with_absolute_path` | `true` | — |
| resolution fails at delivery | `test_skill_that_stops_resolving_at_delivery_is_a_resolution_failure`, `test_resolved_but_body_absent_is_a_resolution_failure` | `false` | `resolution_failed: <reason>` |
| reviewer-bound | `test_reviewer_with_a_skill_never_claims_arrival` | `false` | `reviewer_bound` |

Two gaps remain:

1. **`resolution_failed: lane_without_seam` is documented but not emitted.** ADR-048 §3 states "Een leverroute die de naad niet passeert stempelt `false` met `resolution_failed: lane_without_seam`" (`docs/governance/decisions/ADR-048-refactor-skill-levering.md:43`), and `docs/core/DISPATCH_RULES.md:398` repeats it. `git grep -n "lane_without_seam" -- scripts/` returns **0 matches (exit 1)**: `scripts/lib/skill_application.py` only emits `invalid_skill_slug`, `skill_not_found`, `skill_unreadable`, `empty_skill_body`, `body_not_in_prompt` and `reviewer_bound`. Today every delivery route passes the seam, so a seam-less route would fall back to `body_not_in_prompt` rather than naming the coverage gap. The test that stands in for it, `test_every_delivery_route_passes_the_skill_seam`, pins the *absence* of such a route by source inspection — it does not assert the `lane_without_seam` stamp.
2. **`skill_receipt_fields` has no direct unit test.** `git grep -rn "skill_receipt_fields" tests/` returns nothing. The helper is exercised indirectly by the two govern-level receipt tests (`**skill_receipt_fields(_skill_app)` in `scripts/lib/envelope_govern.py:611` and `scripts/lib/provider_dispatch.py:1848`), so a regression in the "absent when `None`" branch would surface only through those.

Owner named by the plan for these fields is **RS-3 (#2322)**; the plan names no open item for a seam-less route, so this is an open item for the track.

## Voorbehouden aan Vincent (open, not counted against any point)

The plan reserves three actions to Vincent ("Voorbehouden aan Vincent", lines 74-78). They stay open and are deliberately not scored:

1. `refactor-safe` naar consumers brengen — a release and a skills sync per consumer.
2. Everything under `~/.claude` (memory, the knowledge sources) — untouched by this track.
3. The audit-track design session (OI-2359) — RS-3 no longer blocks on it after the 27-10 decision (plan line 105-107), so this reservation no longer gates any point in this measurement.

## Open items

| # | Open item | Point | Owner |
|---|---|---|---|
| 1 | `resolution_failed: lane_without_seam` is recorded in ADR-048 §3 and DISPATCH_RULES §15 but emitted by no production code path; a seam-less route would currently read `resolution_failed: body_not_in_prompt` | 3 | RS-3 / track `refactor-skill-en-levering` |
| 2 | `skill_receipt_fields` has no direct unit test; only covered indirectly by the envelope/provider govern tests | 3 | RS-3 / track |
| 3 | The five-dispatch cohort is unmeasured: 0 of 5 after the RS-3 merge, because all 21 `skill=refactor-safe` specs fired before it, and the envelope/pool lanes have no cohort under this sprint's provider-only builder rule | 7 | measurement (T0) — needs five post-merge dispatches |
