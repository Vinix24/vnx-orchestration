# Nameting — gate-weight-and-worker-posture-cluster (elf punten op main)

**Dispatch-ID**: dlv-c79dcb5a4ca2
**Measured on**: `origin/main` = `7877dbea36a1ab33e9ca6cf26bca8679cc49a02d`
**Date**: 2026-10-27
**Role**: research-analyst · **Model**: deepseek-flash · **Provider**: deepseek-harness
**Source plan**: `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/gate-weight-and-worker-posture-cluster.md` (sections Doel and the measurement table)
**Inventory**: `20261025-inv-gate-weight` (read-only, measured on `62d64145`)

This document changes no code. It re-runs the evidence command the plan and its
inventory give for each of the eleven points, on today's main, and records the
command, the proving output, the exit code and a verdict. PR-A to PR-D of this
track are on main: #2231 (PR-A), #2230 (PR-B), #2236 (PR-C), #2229 (PR-D).

## Table — the eleven points on main

| # | Point (plan's Doel) | Evidence command | Proving output | Exit | Verdict | Owner |
|---|---|---|---|---|---|---|
| 1 | Scoped default is the worker posture | `pytest tests/test_worker_scoped_default.py tests/test_worker_permission_enforcement_flag.py` | 28 passed | 0 | **met** | delivered (#1503) |
| 2 | Fallback keeps the `mcp__*` deny; `requires_mcp=True` survives to the claude-headless lane | `pytest tests/test_subprocess_adapter_scope_fallback.py` + `pytest tests/test_dispatch_envelope_field_propagation.py tests/test_requires_mcp_propagation.py` | 4 passed; 14 passed | 0 | **met** (acceptance probe inconclusive — see caveat) | PR-A (#2231) |
| 3 | Docs state the filesystem/network boundary | read `docs/operations/WORKER_PERMISSIONS.md:27-31` and `:61-62` | text says "worktree isolation bounds the filesystem, not the network" twice | 0 | **met** | delivered |
| 4 | Calibration measurement of the panel | `python3 scripts/analysis/plan_gate_panel_effectiveness.py --data-dir "$VNX_DATA_DIR"` | 13 of 167 complete rounds diverged (7.8%); corpus 1110 | 0 | **met** | delivered (#1504) |
| 5 | The plan gate derives its weight via `derive_governance_variant` | `grep` of `_derive_panel_weight` + `compose_panel` signature | variant derived and recorded, but `compose_panel` decides seats; unchanged | 0 | **not met** | Vincent (plan: text is Vincent's) |
| 6 | An irreversible path gets the second seat from the start; docs-only keeps one | `pytest tests/test_plan_gate_irreversible_second_seat.py` | 29 passed; direct call: receipt/fleet paths → `[codex, opus]` quorum 2, docs → `[codex]` quorum 1 | 0 | **met** | PR-B (#2230) |
| 7 | The operator's seat choice wins, visible in `gov_trace` | `pytest tests/test_plan_gate_one_seat.py` + ledger grep | 25 passed; 9 ledger records carry `OVERRIDES the default seat` | 0 | **met** | delivered |
| 8 | Two seats on one provider are refused at load; shipped config (5 seats/5 providers) loads | `pytest tests/test_plan_gate_panel_seat_family.py` + direct load | 22 passed; duplicate `claude` seat raises `ValueError` naming `'opus'`, `'sonnet'`, `'anthropic'`; shipped config 5/5 | 0 | **met** | PR-C (#2236) |
| 9 | `resolution_reason` is mandatory | `vnx horizon plan-gate missing-reasons` + guard in `scripts/lib/tracks.py:1272-1273` | `no resolved plan-gate blockers without a resolution_reason` (0) | 0 | **met** (cited test file red on main under OI-946 — see caveat) | delivered |
| 10 | Operator disposition of the nine OI-1198 tracks | read-only SQLite on `track_open_items` for the nine tracks | all nine carry a `resolution_reason`; 7 `done`, 2 still `blocked` | 0 | **met** | OI-1198 (closed 15-08) |
| 11 | Fewer blocked tracks than the 76 baseline | `vnx objective list --count-blocked` | `102 blocked (93 plan-gate, 9 dependency, 0 other)` — 26 **more** than 76 | 0 | **partly met** | Vincent (measuring half shipped; decline is other tracks) |

## Point 5 — not met

**What the plan says** (`Doel`/inventory, point 5): the plan gate "leidt zijn
zwaarte af via `derive_governance_variant`" (derives its weight via the
governance variant). The plan itself already records that the operator decision
of 17-10 (P2, #2104) superseded this: the variant is derived and recorded, but
`compose_panel` chooses the seats.

**What main measures today.** Nothing changed since the inventory.

- `_derive_panel_weight` (`scripts/planning_cli.py:4550`) calls
  `smart_router.derive_variant(...)` and records `variant` in the trace and the
  round record.
- `plan_gate_panel.compose_panel` (`scripts/lib/plan_gate_panel.py:620`) takes
  only `requested_labels`, `config_path` and `dispatch_paths` — no variant
  parameter. It picks the configured `default_seat`, the `escalation_seat` on a
  BLOCK, or both from the start via `second_seat_from_start`.
- PR-B (#2230) widened `second_seat_from_start` and PR-C (#2236) added the
  provider-family check; neither re-wired the variant to the seat count.

**Verdict**: `not met` as written — the sentence is literally false on main.
The plan deliberately does not reverse P2; the text is the operator's to reword
or the decision to re-wire. Owner: Vincent.

## Point 11 — partly met

**What the plan says** (`Doel`, point 11): the blocked-track count is lower than
the baseline of 76. The plan splits the point: PR-D ships the repeatable count;
the decline itself is an outcome of other tracks (triage, closures) and is
reserved for the operator.

**What main measures today.**

```
$ vnx objective list --count-blocked
102 blocked (93 plan-gate, 9 dependency, 0 other)
$ echo $?
0
```

- Baseline of 14-08: **76**.
- Today: **102** — 26 more, not fewer. The measuring half works and uses the
  same definition as the baseline (`derived_status == "blocked"` over all
  tracks), so the two numbers are comparable.
- The split: 93 plan-gate, 9 dependency, 0 other. A track blocked for more than
  one reason counts once, in the first category of the fixed order.

**Verdict**: `partly met` — the measurement half is delivered; the decline is
not (102 vs 76) and no code in this track can make it true. Owner: Vincent (the
plan parks the closing-condition rewrite as an operator decision).

## Caveats on points marked `met`

These do not change the verdicts; they bound the evidence.

### Point 2 — the MCP acceptance probe is inconclusive in this worktree

`python3 scripts/analysis/mcp_namespace_probe.py` exited **2**:

```
CONTROL (requires_mcp=True) ... mcp__ tools seen: NONE
SCOPED (fabric default)   ... --disallowedTools WebSearch,WebFetch,mcp__*
                              mcp__ tools seen: NONE
VERDICT: control is empty — no ambient mcp__ tools were detected, so the
scoped NONE is not independently meaningful.
```

Exit 2 means the control saw no ambient MCP tools, so the scoped `NONE` cannot
be attributed to the deny in this environment (no MCP servers are configured in
this worktree's harness config). What the run *does* show is that the scoped
argv carries `--disallowedTools ...,mcp__*`. The behavioural halves of point 2
are proven by the two test files above (import-fault fallback and
`requires_mcp` propagation), which pass.

### Point 9 — the cited regression test file is red on main and CI-excluded

The inventory cites `tests/test_planning_cli.py:1334`
(`test_resolve_plan_blocker_empty_reason_fails_closed`). Run on `origin/main`:

```
$ python3 -m pytest "tests/test_planning_cli.py::test_resolve_plan_blocker_empty_reason_fails_closed" -q
RuntimeError: Migration 0030 requires migration 0029: tracks is missing
column(s) ['track_type', 'next_action_owner']. Apply migration 0029 before 0030 (OI-1213).
3 failed
```

The whole file is on the CI exclusion list
(`scripts/ci/test_exclusions.txt:168`, reason "OI-946: DB/migration
precondition chain ... red on main too"), so this is a known pre-existing
exclusion, not a regression of this track. The guard itself is present at
`scripts/lib/tracks.py:1272-1273` (`if not reason or not reason.strip(): raise
ValueError(...)`), and the live measurement above reports 0 reasonless resolved
plan-gate blockers.

## Measurement environment

- Worktree: `.vnx-data/worktrees/dispatch-dlv-c79dcb5a4ca2`, HEAD `7877dbea`,
  verified equal to `origin/main` (`git rev-parse HEAD origin/main` both
  `7877dbea36a1ab33e9ca6cf26bca8679cc49a02d`).
- `VNX_DATA_DIR=/Users/vincentvandeth/.vnx-data/vnx-dev`.
- Combined targeted pytest of the six evidence files: **74 passed**, JUnit XML
  written to `unified_reports/dlv-c79dcb5a4ca2.junit.xml`.
- VNX CI at `7877dbea` was `in_progress` when read (read-only `gh run list`);
  this run did not act on it.

## Concluding count

Of the eleven points on main: **nine `met`**, **one `partly met`** (point 11,
measurement half only) and **one `not met`** (point 5, the plan's own text is
false and is reserved for the operator). Points 2 and 9 carry evidence caveats
that bound, but do not overturn, their verdicts.
