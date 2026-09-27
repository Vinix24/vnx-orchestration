# OI-Triage 20260801-T6 — bestaansrecht OI-105/212/213/223/224/225/546/547/557/558/559/560

Dispatch-ID: 20260801-t6-oi-triage

## Summary

Twaalf open items (2026-06-03 t/m 2026-07-09) getoetst tegen origin/main op een verse worktree. Meet-opdracht: per item het genoemde bestand/de functie/de tabel opzoeken en het beschreven gedrag NU reproduceren, niet redeneren vanuit de tekst. Uitkomst: 3 ACHTERHAALD (OI-105 onbeoordeelbaar als runtime-incident, OI-224 betekenis gewijzigd), 2 BESTAAT-KLEIN met fix (OI-547, OI-558 restant), 7 BESTAAT-GROOT (OI-212, OI-213, OI-223 deels, OI-225, OI-546, OI-557, OI-559, OI-560). Twee kleine function-size fixes geleverd: `_check_hook_paths` (89→57 regels) in doctor.py en `_operator_get_sessions` (83→42 regels) in api_operator.py, beide met AST-grootte-guard die op de oude code rood staat. De vier function-size-advisories (OI-547/558/559/560) zijn allemaal nog levend als advisory-class; alleen de twee kleinste zijn nu binnen de 70-regel-norm. De codex-gate-runners, bench-harness en ADR-005-artifact-kwesties zijn onverminderd aanwezig maar vereisen ontwerpkeuzes, geen regeltjes-fix.

## Changes

- `vnx_cli/commands/doctor.py` — `_check_hook_paths` gesplitst: de per-event path-extractie + existentie-check is nu `_collect_dead_hook_paths(hooks, project_root)` (37 regels); `_check_hook_paths` zelf is 57 regels (was 89). Extern contract (Check name/status/detail) ongewijzigd.
- `tests/unit/vnx_cli/commands/test_doctor.py` — AST-grootte-guards voor `_check_hook_paths` en `_collect_dead_hook_paths` (≤70, faalt op oude code: 89) + gedragstest voor `_collect_dead_hook_paths` (relative/absolute dead-path split).
- `dashboard/api_operator.py` — `_operator_get_sessions` gesplitst: `_tmux_sessions_to_map` (15) + `_merge_session_store_entries` (33) + `_operator_get_sessions` (42, was 83). Gedrag identiek.
- `tests/test_serve_dashboard_api.py` — AST-grootte-guards voor de drie sessobs-functies (≤70, faalt op oude code: 83).

## Verification

OI-105 | ONBEOORDEELBAAR | Runtime-incident (runner pids 75513/82267, PR #811, 2026-06-03) is niet reproduceerbaar en de root-cause is nooit vastgesteld; pids/PR-context is weg. De exit_nonzero-classificatie bestaat nog bij design (scripts/gate_runner.py:513-515), maar de gemini_review-runner heeft sindsdien stall-detection (GATE_STALL_DEFAULTS gemini_review=180s, scripts/lib/headless_adapter.py:89-92), vertex-routing (VNX_GEMINI_ROUTING=vertex, gate_runner.py:91) en cwd-pinning op een PR-branch-worktree (OI-708). Een stille 275s-exit-1 zou nu als 'stall' op ~180s worden geclassificeerd, niet als kale exit_nonzero.

OI-212 | BESTAAT-GROOT | `_llm_judge_code_quality` in scripts/benchmark/field-tests/runners/scorer.py:180-221 draait nog claude(Opus)+kimi; er is geen `_judge_deepseek`. Bij kimi-quota-falen valt de panel terug op claude-only (scorer.py:212-213), dus het doel 'tweede onafhankelijke judge zonder kimi-quota-afhankelijkheid' is niet bereikt. Oplossing vereist een provider-constraint-bewuste keuze (deepseek-harness lane, eigen key + hardening-flag per deepseek-harness-subscription-blocked), geen paar regels.

OI-213 | BESTAAT-GROOT | Er is geen scorer-replay/re-judge-mode: `--retry-from` (run_field_tests.py:418) her-draait DNF-cellen, geen re-judge over bestaande raw.csv. `scripts/benchmark/judge_quality.py` re-judget wel bestaande results/*.json maar met één judge (`--model`, default opus), geen 4-judge-panel (opus+kimi+deepseek+codex). Feature-ontwerp (replay-mode + multi-judge panel) ontbreekt nog.

OI-223 | BESTAAT-GROOT | De loader is LIVE: `load_lane_safety()` (routing_policy.py:56) + `is_claude_headless_blocked()` (routing_policy.py:71) worden afgedwongen in dispatch_bridge.py:263-265 en dispatch_cli.py:1261-1263. `headless_block` is dus enforced, en `HEADLESS_FORCED_MODELS` is leeg (hang reproduceerde niet post-cutover). De overige 5 regels (force_headless, claude_serial_under_load, codex_retry_once, kimi_quota_fail_fast, tmux_spawn_max_runtime, haiku_receipt_nondeterminism) blijven declaratief — routing_policy.yaml:79-82 zegt expliciet "Do not rely on them as live guards". Die per regel wél afdwingen is een ontwerpkeuze per regel.

OI-224 | ACHTERHAALD | Betekenis gewijzigd: 2 van 3 sub-punten opgelost, de derde bewust gedeferred. F4 (retry reset shared checkout niet): opgelost door fail-loud isolatie — lane_adapter.py zet VNX_BENCH_REQUIRE_ISOLATION=1 + VNX_ISOLATED_WORKTREE=1 voor headless én provider-lanes, downstream afgedwongen in provider_dispatch.py:1376-1379 en 1807-1810 (werkworktree per cel, seed-contaminatie-check pre/post via _main_seed_status). F5 (report-match op prefix ipv dispatch_id): gefixt in #831, run_field_tests.py:199-215 matcht nu dispatch_id exact. F8 (worktree-removal-fout niet structured): bestaat nog als gedocumenteerde known-limitation, bewust naar 1.0.1 gedeferred (run_field_tests.py:138-139) — geen live defect.

OI-225 | BESTAAT-GROOT | De bench-harness staat nog steeds buiten de wheel: pyproject.toml:115-117 excludeert scripts/benchmark/**, scripts/benchmarks/** en scripts/llm_benchmark.py. Deels gegeneraliseerd (judge_quality.py heeft --tasks-dir bring-your-own-tasks), maar 'terug in de wheel als feature' is een roadmap-ontwerpkeuze (1.1), geen regeltjes-fix.

OI-546 | BESTAAT-GROOT | Schrijft nog steeds het JSON-artifact naar `<state-dir>/scout_effectiveness.json` (scripts/scout_effectiveness.py:56-57 + write_artifact, regel 506-511) zonder NDJSON-ledger-event. Alleen de atomic-write is gefixt (#1072). De keuze 'naar een non-state locatie' óf 'ledger-event emitten' óf 'ADR-005-exempt codificeren' is een audit-contract-beslissing.

OI-547 | BESTAAT-KLEIN | Gefixt: `_check_hook_paths` was 89 regels (vnx_cli/commands/doctor.py:660-748), nu gesplitst in `_collect_dead_hook_paths` (37) + `_check_hook_paths` (57). AST-grootte-tests in tests/unit/vnx_cli/commands/test_doctor.py faalden op oude code (89>70) en slagen nu (12/12 doctor-tests groen).

OI-557 | BESTAAT-GROOT | Geen systemische resolutie: geen ADR-005-exemptie gecodificeerd, geen non-state analysis-locatie afgesproken. scout_effectiveness blijft state-dir-artifacts schrijven; de codex-advisory-class blijft dus terugkomen. Ontwerpbeslissing nodig, geen regeltjes-fix.

OI-558 | BESTAAT-KLEIN | Twee van drie sub-punten al opgelost: LiveSessionsPage bestaat niet meer (dashboard is JSON-API, api_operator.py), en de panes_result-degradation is gefixt in #1295 (api_operator.py:731-738, degraded_reasons). Restant `_operator_get_sessions` was 83 regels → nu gesplitst in `_tmux_sessions_to_map` (15) + `_merge_session_store_entries` (33) + `_operator_get_sessions` (42). AST-grootte-tests in tests/test_serve_dashboard_api.py faalden op oude code (83>70), slagen nu (sessobs-tests 7/7). NB: 6 pre-bestaande failures in test_serve_dashboard_api.py (sd._read_gate_config AttributeError, module serve_dashboard heeft die functie niet) zijn niet door deze dispatch veroorzaakt.

OI-559 | BESTAAT-GROOT | Beide delen nog aanwezig: (1) receipt_schema.py:247 + 256-257 schrijft nog steeds zowel `permission_enforcement` als `worker_permission_enforcement` (bewuste collision-avoidance, inconsistent); reconciliatie is een schema/consumer-ontwerpkeuze. (2) `_build_completion_protocol` is 111 regels (scripts/lib/tmux_interactive_dispatch.py:965-1075) — een substantiële refactor, geen paar regels.

OI-560 | BESTAAT-GROOT | `vnx_dispatch_agent` is nog 180 regels (vnx_cli/commands/dispatch_agent.py:184-363). De CLI-entry verweeft resolver + preflight + lane-coercion + deadline-passthrough; opsplitsen is een refactor die gedrag-drift riskeert zonder toegewijde refactor-budget. Boven de 'enkele regels'-grens.

## Open Items

- T0 kan op basis van dit bewijs overwegen: OI-105 (ONBEOORDEELBAAR, sluiten op mitigatie), OI-224 (ACHTERHAALD, F8-deferral is bewust) en de twee function-size-items (OI-547, OI-558) te sluiten; OI-212/213/223/225/546/557/559/560 blijven open als ontwerp-/roadmap-items.
- Pre-bestaande (niet door deze dispatch veroorzaakte) test-breuken op main: tests/test_serve_dashboard_api.py::TestOperatorKanban + TestGateTogglePost (6 tests, sd._read_gate_config AttributeError), tests/test_api_operator_dedup.py::test_no_false_dedup (kanban-dedup telt 67 i.p.v. 3). Beide apart te triagen.
