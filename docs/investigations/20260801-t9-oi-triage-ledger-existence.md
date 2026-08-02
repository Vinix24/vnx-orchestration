# OI-Triage 20260801-T9 — bestaansrecht OI-561, OI-562, OI-563, OI-564, OI-579, OI-621, OI-628, OI-629, OI-636, OI-645, OI-653, OI-655

Dispatch-ID: 20260801-t9-oi-triage

## Summary

Twaalf open items getoetst op een verse worktree vanaf origin/main, elk gereproduceerd tegen de huidige code. Uitkomst: 2 ACHTERHAALD met betekeniswijziging (OI-628, OI-655), 1 BESTAAT-KLEIN (OI-645, gefixt), 9 BESTAAT-GROOT. Eén code-wijziging: OI-645 module-level import + rood-op-main-test. Volledige bewijslast per item staat in het unified report (`unified_reports/20260801-t9-oi-triage.md`); deze doc is de repo-side samenvatting.

## Verdicts

- **OI-628 | ACHTERHAALD** — lokale sidecar-anchor vervangen door ADR-034 git-anchor (implementatie aanwezig, nog niet geactiveerd: VNX_CHAIN_RECEIPTS default-off, governance/ nog niet gevuld). De 3 codex-holes (deletion-fallback, append-forgery-last-match, seal-race) zijn structureel dicht.
- **OI-655 | ACHTERHAALD** — contract-injectie live sinds 2026-06-23; post-OI (07-17 window, 07-27/28 govern-synthese + SynthesizedLaneReceipt) dicht de resterende gap. De 36x-telling zelf is alleen in de SEOcrawler-v2 store te her-meten (buiten scope).
- **OI-645 | BESTAAT-KLEIN** — F821 `PrEnforcementResult` gereproduceerd (ruff + `get_type_hints` NameError); gefixt met module-level import; test `tests/test_tmux_pr_enforcement_annotation_resolves.py` rood op main, groen na fix.
- **OI-561, OI-562 | BESTAAT-GROOT** — evidence-bound gate blijft advisory-default, signed-delegation blijft default-off (beide gemeten). Review-bij-flip-moment staat nog uit; operatorbeslissing.
- **OI-563 | BESTAAT-GROOT** — gereproduceerd: `apply_if_below(27, _migrate_v27)` stamt user_version=27 terwijl `ux_success_patterns_pid` ontbreekt; geen durable marker voor de quality-DB (probe leest alleen runtime_coordination.db).
- **OI-564 | BESTAAT-GROOT** — `_seed_default_pool_config` (nu init_cmd.py:433-505) gebruikt nog impliciete transactie; explicit-BEGIN is optionele design-wijziging.
- **OI-579, OI-636, OI-653 | BESTAAT-GROOT** — planning_cli.py 2593L, tmux_interactive_dispatch.py 2865L, test_tmux_interactive_dispatch.py 4648L; alle drie nog over de advisory-grens; monoliet-splitsing is grote refactor.
- **OI-621 | BESTAAT-GROOT** — geen continue content-convergentie-assertie voor role-orchestrator.md; bouwstenen (registry-iteratie in t0_role_audit.sh, cmp in cmd_role_sync) bestaan wel.
- **OI-629 | BESTAAT-GROOT** — merge-flow-codificatie + role escape-hatch afwezig in de canonieke role; MC/SC-delen in andere repos, hier niet te verifiëren.

## Changes

- `scripts/lib/tmux_interactive_dispatch.py`: module-level `from pr_enforcement import PrEnforcementResult`; redundante deferred import in `_enforce_pr_exists` verwijderd.
- `tests/test_tmux_pr_enforcement_annotation_resolves.py`: nieuw, OI-288-patroon.

## Open Items

- OI-561, OI-562, OI-563, OI-564, OI-579, OI-621, OI-629, OI-636, OI-653 blijven open als BESTAAT-GROOT.
- Pre-bestaande test-failures op main (niet door deze dispatch): `test_open_items_gate_certification.py::test_read_gate_config_returns_dict` (T8), `test_tmux_adapter_interface.py::test_shutdown_is_noop` + `test_no_direct_tmux_in_protected_modules` (geverifieerd pre-existent via stash-revert).
