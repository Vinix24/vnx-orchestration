# Nameting — gate-weight-and-worker-posture-cluster, eleven points on main (renegotiated 5 and 11)

**Dispatch-ID**: dlv-27c55cde9acc
**Measured on**: `origin/main` = `d80d748f` (`feat(ge4c-2): one request writer, one request emitter, a guarded annotation (#2271)`), HEAD of worktree `.vnx-data/worktrees/dispatch-dlv-27c55cde9acc`
**Date**: 2026-10-27 · **Role**: quality-engineer · **Model**: deepseek-flash · **Provider**: deepseek-harness
**Source goal**: `vnx objective show gate-weight-and-worker-posture-cluster` (eleven points)
**Source plan**: `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/gate-weight-and-worker-posture-cluster.md`
**Prior measurement**: `docs/investigations/2026-10-27-gate-weight-and-worker-posture-nameting.md` (#2250, on `7877dbea`) — the original wording
**T0 decisions**: `claudedocs/2026-10-24-nachtrun-golf-1-3/draaiboek.md` §0, table "Besluiten van de T0 op de open punten (27-10)": OI-2363 (point 5) and OI-2362 (point 11)

This document is the closing measurement. It re-runs the evidence command of every point on
today's main and records command, counted output and verdict. Points 5 and 11 are measured
against the T0's renegotiated text, not the original goal sentence. One code fix was necessary
and is part of this PR: the point-4 evidence command did not run on main at all (§"The one fix").

## Table — the eleven points

| # | Point | Command | Counted output | Verdict | Delivered by |
|---|---|---|---|---|---|
| 1 | Scoped worker-mode is the fabric default; blanket skip is explicit opt-in | `python3 -m pytest tests/test_worker_scoped_default.py tests/test_worker_permission_enforcement_flag.py -q` | `28 passed` | **met** | #1503 (`worker_permissions.py:339`, `:357`), #1511 |
| 2 | A worker without opt-in has zero `mcp__` tools; `requires_mcp` reaches the claude-headless lane | `python3 -m pytest tests/test_subprocess_adapter_scope_fallback.py -q` and `python3 -m pytest tests/test_dispatch_envelope_field_propagation.py tests/test_requires_mcp_propagation.py -q`; `python3 scripts/analysis/mcp_namespace_probe.py` | `4 passed`; `14 passed`; probe `exit 2` (control empty — see caveat) | **met** (probe inconclusive) | #2231 (PR-A), #1506 (`worker_permissions.py:106`), #1503 |
| 3 | The docs state the filesystem/network boundary with the reason | read `docs/operations/WORKER_PERMISSIONS.md:29` and `:62` | both lines read "bounds the filesystem, not the network" | **met** | delivered with #1506/#1503 |
| 4 | Calibration: how often does the panel diverge from seat 1 | `python3 scripts/analysis/plan_gate_panel_effectiveness.py --data-dir "$VNX_DATA_DIR"` | `complete rounds: 167`, `equal: 154 (92.2%)`, `diverged: 13 (7.8%)`; corpus `1074 reports, 136 tracks, 286 rounds` | **met** after this PR's fix; **not met on origin/main** (command exited 1 with `ModuleNotFoundError: append_receipt`) | #1504 (script), measured in #2250 |
| 5 | The plan gate derives its weight and records it; `compose_panel` chooses the seats (renegotiated) | read `scripts/planning_cli.py:4636-4641` + `scripts/lib/plan_gate_panel.py:628`; parse `.vnx-attest/plan-gates.ndjson` | `43` records carry `governance_variant` + `gov_trace`; `16` start `weight=<variant> -> N seat(s)`; seat count does not follow the variant | **met** | #2104 (P2); renegotiated by OI-2363 |
| 6 | Both ends of the ladder: docs-only gets no second seat, an irreversible path does | `python3 -m pytest tests/test_plan_gate_irreversible_second_seat.py -q`; direct `second_seat_from_start`/`compose_panel` call | `29 passed`; receipt path → `[codex, opus] quorum 2`; docs path → `[codex] quorum 1` | **met** | #2230 (PR-B) |
| 7 | An explicit operator seat choice wins, visible in `gov_trace` | `python3 -m pytest tests/test_plan_gate_one_seat.py -q`; count `OVERRIDES the default seat` in the attest ledger | `25 passed`; `9` records carry `OVERRIDES the default seat` | **met** | #2104 (`planning_cli.py:4641`) |
| 8 | N seats means N provider families; the shipped config loads | `python3 -m pytest tests/test_plan_gate_panel_seat_family.py -q`; direct `load_panel_seats` on the shipped config and on a duplicate-family copy | `22 passed`; shipped config `5 seats / 5 providers`; duplicate raises `ValueError: seats 'opus' and 'opus2' share provider family 'anthropic'` | **met** | #2236 (PR-C) |
| 9 | `resolution_reason` is mandatory when clearing an OI-PLAN blocker | `vnx horizon plan-gate missing-reasons`; guard at `scripts/lib/tracks.py:1304-1305` | `no resolved plan-gate blockers without a resolution_reason` (0) | **met** | #849 (guard), second path #1981 |
| 10 | Operator disposition of the nine OI-1198 tracks | read-only SQLite on `track_open_items` for the nine tracks | `9/9` carry a `resolution_reason`; `7 done`, `2 blocked` (`fabric-self-reporting-truthful`, `gate-finding-disposition`) | **met** | OI-1198 disposition, 15-08 |
| 11 | Blocked-track count is repeatable and every blocked track is triaged (renegotiated) | `vnx objective list --count-blocked`; triage join over `claudedocs/blocked-track-triage-{a,b}.md` | `104 blocked (93 plan-gate, 11 dependency, 0 other)`; `54 of 104` have a triage row, `50` untriaged | **not met** (repeatable half met, triage half not) | #2229 (PR-D) for the count; triage is a new delivery |

Ten of eleven `met`. Point 11 is `not met` on the T0's renegotiated condition because the triage
half is incomplete; the fix is larger than a measurement PR (§Point 11).

## Point 5 — renegotiated (OI-2363)

**T0 decision** (`draaiboek.md` §0, "Besluiten van de T0", row OI-2363): *"Punttekst volgt P2
(17-10): compose_panel kiest de zetels, de variant staat in gov_trace"*. The older goal wording
("de plan-gate leidt zijn zwaarte af via derive_governance_variant") is superseded and is not
what this measurement tests.

**What main does.**

- `compose_panel` (`scripts/lib/plan_gate_panel.py:628`) chooses the seats: the configured
  `default_seat`, the `escalation_seat` on a BLOCK, both from the start for an irreversible
  path (`second_seat_from_start`, `:2238`), or exactly the seats an operator names. Its
  signature takes `requested_labels`, `config_path`, `dispatch_paths` — no variant.
- `_derive_panel_weight` (`scripts/planning_cli.py:4588`) calls `smart_router.derive_variant`
  (`:4620`) and records the result in `gov_trace` (`:4636-4641`):
  `weight=<variant> -> N seat(s) by <chosen_by> (<labels>)`, plus
  `; OVERRIDES the default seat <label>` or `; both seats from the start (<category>)`.
- The variant and trace are durably recorded: `governance_variant=variant,
  gov_trace=weight["gov_trace"]` at `scripts/planning_cli.py:4890` (and `:4985`).

**Counted output** on `.vnx-attest/plan-gates.ndjson` (main checkout):

```
records: 148
with governance_variant: 43
with gov_trace: 43
gov_trace carrying 'weight=': 43
weight= derived from variant: 16
OVERRIDES the default seat: 9
variant distribution: {'tiebreaker': 27, 'default': 15, 'minimal': 1}
```

Example (last record): `weight=coding-strict -> 1 seat(s) by operator (codex); OVERRIDES the
default seat codex` — the variant is recorded, the seat count came from `compose_panel`.
The 27 `tiebreaker` records are the plan-gate tiebreaker rounds, whose `gov_trace` also carries
a `weight=` value.

**Verdict**: **met** under the renegotiated text.

## Point 11 — renegotiated (OI-2362)

**T0 decision** (`draaiboek.md` §0, "Besluiten van de T0", row OI-2362): *"Sluitvoorwaarde punt
11 wordt: telling herhaalbaar (PR-D) en elke geblokkeerde track getrieerd. Niet: het aantal
daalt"*. The original "fewer than 76" condition is withdrawn. The plan (`Besluiten`, first
bullet) already split the point the same way: PR-D makes the count repeatable; the decline is
an outcome of other tracks.

**Repeatable half — met.**

```
$ vnx objective list --count-blocked
104 blocked (93 plan-gate, 11 dependency, 0 other)
$ echo $?
0
```

The definition is the one behind the 14-08 baseline of 76 (`derived_status == "blocked"` over
all tracks, one count per track, fixed category order plan-gate > dependency > other;
`scripts/planning_cli.py:416` `blocked_track_counts`, `:447` `_cmd_list_count_blocked`). The
number is not asked to fall; 104 versus 76 says nothing about this point any more.

**Triage half — not met.** The only recorded triage dispositions for blocked tracks are the two
tables `claudedocs/blocked-track-triage-a.md` and `-b.md` (81 unique track ids, measured on
`45ace207`, 15-08). Joining today's blocked set against them:

```
$ vnx objective list --count-blocked --json | python3 -c '
import json,sys,re,pathlib
ids=[t["track_id"] for t in json.load(sys.stdin)["tracks"]]
docs=[pathlib.Path(f"claudedocs/blocked-track-triage-{x}.md") for x in "ab"]
covered={i for i in ids if any(re.search(r"(?<![A-Za-z0-9_-])"+re.escape(i)+r"(?![A-Za-z0-9_-])", d.read_text()) for d in docs)}
print(f"{len(covered)} of {len(ids)} blocked tracks have a triage row; {len(ids)-len(covered)} untriaged")
'
54 of 104 blocked tracks have a triage row; 50 untriaged
```

The 50 without a triage row are listed in Open Items of the dispatch report. They include the
three wave tracks (`golf-1-opruimen`, `golf-2-hardenen`, `golf-3-harness-agnostisch`), the
`agents-als-map-fase-1..4` series, `audit-spoor-kern`/`audit-spoor-volledig`,
`terminal-binding-afbouwen`, `fleet-release-and-rollout` and `archify-verified-architecture-maps`.
Triaging 50 tracks with a per-track verdict and evidence is a delivery of its own, not a
≤40-line fix.

**Alternative reading, recorded so the T0 can decide.** If "getrieerd" means "attributed by the
count itself" — every blocked track lands in exactly one category with at least one recorded
reason, and `other` is the fail-closed bucket for a track with no reason left
(`scripts/planning_cli.py:378-405`) — then the half is met: 104 = 93 + 11 + 0, no track dropped
out. This document does not adopt that reading: the T0's "getrieerd" matches the per-track
disposition in the blocked-track triage tables (`GESHIPT`/`DEELS`/`OPEN`/`ONTOETSBAAR`), not the
category split.

**Verdict**: **not met**.

## The one fix this measurement forced

The plan's own evidence command for point 4 did not run on `origin/main`:

```
$ python3 scripts/analysis/plan_gate_panel_effectiveness.py --data-dir "$VNX_DATA_DIR"
ModuleNotFoundError: No module named 'append_receipt'
```

`tests/test_plan_gate_panel_effectiveness.py`, the test that pins that script, was red the same
way when run on its own (it passed in the full suite only because an alphabetically earlier
sibling test, `tests/test_plan_gate_irreversible_second_seat.py` or
`tests/test_plan_gate_one_seat.py`, had already put `scripts/` on `sys.path` — an import-order
dependency, not isolation).

Cause: #2266 (`d0139cd3`) added `from gate_artifacts import _compute_contract_hash` to
`scripts/lib/governed_lane.py:66`; `gate_artifacts` imports `governance_receipts`
(`scripts/lib/gate_artifacts.py:16`); `governance_receipts` imported the bare facade
`append_receipt` (at `scripts/append_receipt.py`), which only resolves when `scripts/` is on
`sys.path`. Entry points that put only `scripts/lib` on the path — `scripts/analysis/*` and a
standalone test — failed at import time.

Fix (`scripts/lib/governance_receipts.py`, +22/−11): import the facade through a small
`_load_append_receipt()` that puts the facade's own directory on `sys.path` first — the same
self-bootstrap `scripts/lib/context_rotation.py:45-46` does, without an `E402` `# noqa`. The
facade itself must stay the import — importing `append_receipt_internals` alone is not
equivalent, because `append_receipt_payload` reads the `common.facade` proxy, which is only
populated when the facade module registers (`scripts/lib/append_receipt_internals/common.py:63-97`).

Regression test: `tests/test_governance_receipts_import_path.py` — one subprocess that puts only
`scripts/lib` on `sys.path`, and one identity check that the re-exported
`append_receipt_payload`/`AppendReceiptError` are the facade's own objects.

## Caveats

- **Point 2 — the acceptance probe is inconclusive in this worktree, as in #2250.** Run from
  this real dispatch worktree, `python3 scripts/analysis/mcp_namespace_probe.py --timeout 120`
  exited **2**: the control run (`requires_mcp=True`) saw `NONE`, so there is no ambient
  `mcp__` surface in this harness configuration to compare the scoped run against. What the run
  does show is the scoped argv carrying `--disallowedTools WebSearch,WebFetch,mcp__*` and the
  control not carrying it. The behavioural halves of point 2 are pinned by the two test files
  above (fallback parity and `requires_mcp` propagation, both green).
- **Point 9 — the cited regression test is red on main and CI-excluded.**
  `tests/test_planning_cli.py:1333` (`test_resolve_plan_blocker_empty_reason_fails_closed`) is
  in `scripts/ci/test_exclusions.txt:168` for OI-946 (DB/migration precondition chain, red on
  main too). The guard itself is at `scripts/lib/tracks.py:1304-1305` and the live measurement
  reports 0 reasonless resolved plan-gate blockers.

## Environment

- Worktree `.vnx-data/worktrees/dispatch-dlv-27c55cde9acc`; `git rev-parse HEAD origin/main`
  both `d80d748f`. No state in `~/.vnx-data/vnx-dev` was written: every store read is read-only
  (SQLite `SELECT`s, the attest ledger, the open-items JSON), and the analysis scripts write
  only to stdout.
- `VNX_DATA_DIR=/Users/vincentvandeth/.vnx-data/vnx-dev`.
- Targeted pytest runs for the evidence files: 28 + 4 + 14 + 25 + 29 + 22 + 32 (effectiveness)
  + 2 (new regression) = 156 passed after the fix; the red run of the new test on the old
  `governance_receipts.py` is `1 failed, 1 passed`.
