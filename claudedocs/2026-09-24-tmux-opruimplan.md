# Tmux-opruimplan (deel C)

Dispatch-ID: 20260924-tmux-opruimplan-c
**Model**: sonnet-5
**Provider**: claude

Track: retire-redundant-architecture. Deliverable: dlv-dc316f60611b. Rol: research-analyst. Modus: alleen een plan, geen code, test of config gewijzigd.
Meetpunt: main 11df8ff8, 2026-09-24. Invoer: inventaris A (106 bestanden onder `scripts/lib`) en inventaris B (71 bestanden daarbuiten plus 9 daemons), gelezen van hun branches `dispatch/20260924-tmux-inventaris-a-lib` en `dispatch/20260924-tmux-inventaris-b-rest-daemons`.

## Executive Summary

Het plan bestaat uit 23 plakken in vier fasen. Fase 1 knipt de verborgen afhankelijkheden los (P01 tot P04). Fase 2 maakt het tmux-injectiepad naar kimi, codex en gemini expliciet en beschermt het met tests (P05 tot P07). Fase 3 haalt de oude claude-pane-architectuur en de `vnx start`-daemons weg (P08 tot P20). Fase 4 ruimt documentatie en namen op (P21 tot P23). Totaal ongeveer 1.750 regels toegevoegd en 16.000 regels verwijderd. Daarvan is circa 12.000 productiecode (gemeten aan bestandsgroottes en functiegrenzen). De rest is testcode en documentatie (schatting per plak, paragraaf 4).

Zeven bevindingen sturen het plan:

1. **Het injectiepad (b) is een berg onderdelen zonder werkende keten.** De launchcommando's worden alleen door tests geladen. De adressering wordt sinds 2026-06-27 niet meer geschreven. De injectie hangt aan een dispatcher die niet draait en aan een variabele die niets zet. De deur kent geen interactieve lane voor niet-claude. Bewust houden betekent nu een berg onderdelen houden (paragraaf 1.3).
2. **De tests die (b) vastpinnen beschermen niets.** Drie pytest-bestanden staan op de CI-uitsluitlijst (gemeten: 6 falen, 115 slagen) en de 60 `tests/*.sh` hebben geen runner. P07 herstelt dat.
3. **De dispatcher-daemon host (b)-helpers.** `dispatcher_minimal.sh` definieert `get_terminal_provider` en `get_context_reset_command` en de libs roepen ze aan. Weghalen van de daemon zonder P05 laat het codex-pad zonder resetcommando en zonder providerlookup achter.
4. **`VNX_DISPATCH_LEGACY` is geen tmux-vlag.** Het is het terugvalluik van de deur naar het rauwe `.md`-pad. Het valt onder ADR-025 en niet onder dit plan.
5. **61 van de 177 modules (35%) hebben geen tmux-gedrag.** De scope-regex matcht op `dispatch_broker`, `dispatch_deliver` en `pane_id`. Die modules dragen een naam, label of commentaar en geen tmux-code.
6. **De daemon-verwachting wordt 2 verwacht, 2 geparkeerd en 5 verwijderd.** Daarmee kan `daemon_liveness` op `ok` (paragraaf 7).
7. **Eén besluit kan het plan niet nemen.** Waar (b) landt na de opruiming (O-1). ADR-025 noemt het tmux-send-keys-pad van de daemon zelf een zijdeur van de deur. Opinion: (b) hoort een lane van de deur te worden, zodat register, receipt en zijdeur-audit het zien.

Waar te beginnen: P01, P02 en P08 zijn onafhankelijk en risicoarm en kunnen tegelijk. P05, P06 en P07 moeten vóór elke verwijdering die (b)-code raakt. P17 haalt de enige runtime-aanroeper van (b) weg. Dat is toegestaan onder het operatorbesluit (ALLEEN TESTS blijft staan), maar de operator moet het weten voordat P17 start.

## 1. Uitgangspunten

### 1.1 Wat (a) en (b) is

**(b)** is code die een instructie in een interactieve niet-claude CLI (kimi, codex, gemini) in een tmux-pane zet, of dat mogelijk maakt. Vijf rollen tellen mee: de pane adresseren, de CLI erin starten, weten hoe die CLI zijn invoer verwacht, controleren of de pane invoer aanneemt en bevestigen dat de CLI aan het werk gaat.

**(a)** is tmux-code voor de oude claude T0 tot T3-panes en de `vnx start`-daemons.

De regel per module: draagt minstens één functie een providertak voor kimi, codex of gemini of een van die vijf rollen, dan is de module (b) of ONBEPAALD. Twijfel is ONBEPAALD met reden en nooit "weg". Een module heet alleen (a) als ik de claude-specificiteit kan aanwijzen. Voorbeeld: `worker_pane_classifier.py` herkent Claude Code-prompts ("do you want to proceed", "don't ask again for") en geen prompt van codex, gemini of kimi.

### 1.2 Hoe ik regels tel

Een plak geeft `+` (toegevoegde regels in de diff) en `-` (verwijderde regels). De grens van 150 tot 300 geldt voor `+`. Verwijderplakken hebben een grote `-`. Die beoordeel je op bewijs dat er geen aanroeper is en niet op de leesbaarheid van de diff. Elke plak noemt het aanroeperbewijs als commando dat je kunt draaien.

Basis per getal: **gemeten** is de som van bestandsgroottes of functiegrenzen op main 11df8ff8. **Schatting** is mijn inschatting voor testaanpassingen en randgevallen. Elke plak noemt de tests die groen moeten blijven, met bestandsnaam.

### 1.3 (b) op main: onderdelen zonder werkende keten

| Rol | Waar | Toestand op main | Bron |
|---|---|---|---|
| CLI starten | `vnx_start_runtime.build_launch_command` (codex, gemini, kimi) | Alleen door tests geladen, behalve de constante `VNX_VARS`. `start.sh` bouwt een T0-only layout in shell (`tracks: {}`), dus T1 tot T3 bestaan niet. | A (`vnx_start_runtime.py`), B §3.1 (`jump.sh`) |
| Pane adresseren | `pane_manager.sh`, `panes.json` | `panes.json` is sinds 2026-06-27 niet geschreven. `get_pane_id_smart` ontdekt panes op pad, titel en venster en heeft dat bestand niet nodig (`pane_manager.sh:232-258`). | B §2.5, eigen lezing |
| Injecteren | `dispatch_deliver.sh` (`_ddt_send_content` e.a.) | Bereikbaar via `dispatcher_minimal.sh` en alleen als `VNX_ADAPTER_T{n}` ongelijk aan `subprocess` staat. Niets zet die variabele en de dispatcher draait niet. | A (`dispatch_deliver.sh`), B §5.1 |
| Gereedheid | `input_mode_guard.sh` | Zelfde dispatcher-pad. | A |
| Bevestigen | `heartbeat_ack_monitor._check_terminal_activity` (42 regels) | Daemon draait niet. | B §5.4 |
| Via de deur | geen | De deur kent geen interactieve lane voor niet-claude. Kimi, codex en gemini gaan headless via de `provider`-lane. | `dispatch_cli.py:180` |
| Testbescherming | tests | `test_tmux_adapter.py`, `test_tmux_adapter_interface.py` en `test_vnx_start_runtime.py` staan op `scripts/ci/test_exclusions.txt:206,207,212`. De 60 `tests/*.sh` hebben geen runner. | eigen meting hieronder |

Mijn meting van 2026-09-24 in de worktree: `python -m pytest tests/test_tmux_adapter.py tests/test_tmux_adapter_interface.py tests/test_vnx_start_runtime.py` geeft **6 gefaald, 115 geslaagd** in 1,55 seconde. De zes:

| Test | Oorzaak |
|---|---|
| `TestAdapterCliResolveCommand` (3 tests, `test_tmux_adapter.py:561,573,578`) | importeren de niet-bestaande module `tmux_adapter_cli` |
| `TestResultDataclasses::test_shutdown_is_noop` | `TmuxAdapter.shutdown()` kent de parameter `graceful` niet |
| `TestDirectCouplingFreeze::test_no_direct_tmux_in_protected_modules` | `pool_manager.py:371` en `pool_reaper.py:32,53,126` roepen tmux rechtstreeks aan |
| `TestVNXVars::test_contains_all_known_vars` | de verwachte set mist `VNX_SOCKETS_DIR` |

De hele bestanden zijn uitgesloten. Daardoor draait ook de test die de codex-tak vastlegt (`test_legacy_codex_provider_uses_single_paste`, `test_tmux_adapter.py:325`) niet in CI. Voor de shelltests zocht ik een runner in `.github`, `scripts/ci`, `scripts/local-ci.sh`, `Makefile`, `pyproject.toml` en `tests/conftest.py` en vond er geen.

Kimi heeft nergens een eigen injectietak. Hij valt door naar de claude-tak (skill via send-keys, prompt via paste, `/clear` als reset). `vnx_start_runtime.py:174-181` zegt zelf dat er voor kimi geen vastgestelde interactieve launch-syntaxis is. Uncertain: of de kimi-REPL die invoer overleeft, is nooit gemeten (O-7).

Consequentie voor het plan: (b) eerst expliciet maken en beschermen (P05 tot P07) en pas daarna verwijderen wat eraan grenst.

## 2. Wat (b) draagt: oordeel per module

Alle 177 modules uit A en B hebben precies één oordeel. Ik controleerde de dekking met een script: 106 uit A, 71 uit B, geen dubbele en geen ontbrekende.

| Oordeel | Modules |
|---|---|
| (b), houden | 9 |
| ONBEPAALD, met reden | 21 |
| Geen (b), (a) of daemon-erfenis, met plak | 73 |
| Geen (b), geen tmux-gedrag (naam, label, commentaar) | 61 |
| Geen (b), geen tmux-code, buiten dit plan | 13 |

In 2.3 tot 2.5 staat `lib/` voor `scripts/lib/`.

### 2.1 Modules die (b) dragen (houden, gesplitst op functieniveau waar nodig)

| Module | Oordeel | Wat (b) is en wat (a) |
|---|---|---|
| `scripts/heartbeat_ack_monitor.py` | (b) | deels: `_check_terminal_activity` bevestigt dat een geïnjecteerde pane werkt; de rest (T0-ACK-paste) is (a) |
| `scripts/lib/dispatch_create.sh` | (b) | deels: skill-prefix per provider (`$`, `@`, `/`) en providerlookup |
| `scripts/lib/dispatch_deliver.sh` | (b) | deels: primitives, codex-paste, niet-claude modus, contextreset, foutcodes |
| `scripts/lib/failure_classifier.py` | (b) | deels: `TMUX_TRANSPORT_FAILURE` geldt ook voor (b)-transportfouten |
| `scripts/lib/input_mode_guard.sh` | (b) | ja: pane-gereedheid, provider-neutraal |
| `scripts/lib/model_routing.sh` | (b) | deels: modelwissel-beslissing voor codex (gemini: unsupported) |
| `scripts/lib/tmux_adapter.py` | (b) | ja: getypeerde tweeling van de shell-injectie (codex-tak in `_deliver_legacy`) |
| `scripts/lib/vnx_start_runtime.py` | (b) | ja: launchcommando voor codex, gemini en kimi (`build_launch_command`) |
| `scripts/pane_manager.sh` | (b) | deels: pane-ontdekking en adressering (`get_pane_id_smart`, `discover_pane_*`); `setup_pane_titles` en `check_pane_health` zijn (a) |

### 2.2 ONBEPAALD, met reden (niet in de verwijderplakken)

| Module | Reden |
|---|---|
| `dashboard/api_operator.py` | tmux-routes voor start, stop en attach; gebruik onbekend (M-3) |
| `dashboard/token-dashboard/__tests__/session-control-buttons.test.tsx` | hoort bij de tmux-knoppen van het dashboard (M-3) |
| `dashboard/token-dashboard/app/operator/sessions/page.tsx` | volledig afgeleid van `_list_tmux_sessions`; gebruik onbekend (M-3) |
| `hooks/vnx_rotate.sh` | consumer-bedrading buiten de repo (SEOcrawler_v2 en circa 15 worktrees); O-3 |
| `scripts/commands/start.sh` | T0 wordt per provider gelanceerd (claude, codex, gemini); lot hangt aan O-2 |
| `scripts/hooks/pretooluse_worker_scope_enforce.py` | veiligheidshook zonder registratie; herbedraden of weg is een governancebesluit (O-6), geen tmux-code |
| `scripts/hooks/session_stop_rotation.py` | VNX_T0_ROTATION heeft geen setter maar de vlag stond minstens eens aan; O-3 |
| `scripts/lib/adapter_types.py` | adaptercontract (`pane_id`, `tmux_returncode`); wordt door de deur geladen |
| `scripts/lib/dashboard_actions.py` | handmatig gestarte dashboardserver; gebruik onbekend (M-3) |
| `scripts/lib/dispatch_prepare.py` | migratievlag zonder tmux-gedrag; de aan-tak draagt promptverrijking die de live tak mogelijk mist |
| `scripts/lib/dispatch_router.py` | cluster modelleert `interactive_tmux_codex` als doeltype; niet aan de deur gekoppeld |
| `scripts/lib/execution_target_registry.py` | idem: bevat `interactive_tmux_codex` |
| `scripts/lib/fpc_certification.py` | idem: zaait interactive_tmux-doelen |
| `scripts/lib/fpc_certification_checks.py` | idem |
| `scripts/lib/headless_transport_adapter.py` | adapterlaag naast de tmux-adapter; hoort bij O-1 |
| `scripts/lib/local_session_adapter.py` | adapterlaag naast de tmux-adapter; hoort bij O-1 |
| `scripts/lib/mixed_execution_router.py` | idem: `rollback_to_interactive` |
| `scripts/lib/runtime_core.py` | bewaart de tmux-adapter; wijzigt pas met O-1 |
| `scripts/lib/runtime_facade.py` | adapterlaag: `get_adapter` bouwt de TmuxAdapter; hoort bij de keuze in O-1 |
| `scripts/lib/tmux_session_profile.py` | schrijft `panes.json` voor elke pane; lot hangt aan O-2 |
| `scripts/rollback_runtime_core.py` | toont `VNX_TMUX_ADAPTER_ENABLED`; hoort bij de adapterkeuze (O-1) |

### 2.3 Geen (b): tmux voor de oude claude-pane-architectuur en de `vnx start`-daemons, per plak

- **P02** (1): `scripts/commands/resume.sh`
- **P03** (1): `lib/receipt_processor/rp_delivery.sh`
- **P04** (2): `lib/receipt_processor/rp_dedup.sh`, `scripts/receipt_processor.sh`
- **P07** (1): `scripts/ci/test_exclusions.txt`
- **P08** (8): `scripts/benchmark/field-tests/METHODOLOGY.md`, `scripts/benchmark/field-tests/runners/run_field_tests.py`, `scripts/benchmark/field-tests/runners/skill_smoke.py`, `scripts/dispatch_broker_cli.py`, `scripts/reconcile_terminal_state.py`, `scripts/runtime_coordination_init.py`, `scripts/update_pane_mapping.sh`, `scripts/vulture_whitelist.py`
- **P09** (7): `scripts/hooks/pretooluse_block_raw_claude_spawn.sh`, `scripts/hooks/pretooluse_block_subagent.sh`, `scripts/hooks/session_reconcile_autoclose.sh`, `scripts/hooks/session_reconcile_cleanup.sh`, `scripts/hooks/tmux_signal_prompt_received.sh`, `scripts/hooks/tmux_signal_session_ready.sh`, `scripts/hooks/tmux_signal_stop_receipt.sh`
- **P10** (4): `lib/append_receipt_internals/enrichment.py`, `lib/envelope_govern.py`, `lib/provider_dispatch.py`, `lib/toolcall_signals.py`
- **P11** (2): `lib/pool_manager.py`, `lib/pool_reaper.py`
- **P12** (5): `scripts/build_t0_state.py`, `lib/tmux_command_runner.py`, `lib/worker_pane_classifier.py`, `lib/worker_permission_relay.py`, `scripts/permission_relay_cli.py`
- **P13** (4): `scripts/popup_editor.sh`, `scripts/queue_popup_watcher.sh`, `scripts/queue_ui_enhanced.sh`, `scripts/smart_tap_json_translator.sh`
- **P15** (4): `lib/canonical_state_views.py`, `lib/terminal_snapshot.py`, `lib/terminal_state_reconciler.py`, `lib/worker_heartbeat.py`
- **P16** (3): `scripts/cleanup_stale_vnx_sessions.sh`, `lib/orphan_sweep.py`, `scripts/orphan_sweep.py`
- **P17** (5): `bin/vnx`, `dashboard/serve_dashboard.py`, `scripts/dispatcher_minimal.sh`, `scripts/vnx_process_ux.py`, `scripts/vnx_supervisor_simple.sh`
- **P18** (4): `lib/dispatch_lifecycle.sh`, `lib/dispatch_metadata.sh`, `lib/provider_routing.sh`, `scripts/log_dispatch_metadata.py`
- **P19** (9): `scripts/commands/jump.sh`, `scripts/commands/recover.sh`, `scripts/commands/stop.sh`, `lib/tmux_conversation_normalizer.py`, `lib/vnx_doctor_checks.py`, `lib/vnx_doctor_runtime.py`, `lib/vnx_recover_runtime.py`, `lib/vnx_recovery_phases.py`, `scripts/pane_config.sh`
- **P20** (1): `scripts/commands/t0_role_audit.sh`
- **P22** (5): `scripts/benchmark/field-tests/lane_calibration.yaml`, `scripts/benchmark/field-tests/tasks.yaml`, `lib/dispatch_govern.py`, `lib/providers/routing_policy.yaml`, `lib/providers/wave7_models.yaml`
- **P23** (7): `scripts/check_live_requires_measured_health.py`, `scripts/check_no_file_derived_data_paths.py`, `scripts/commands/dispatch.sh`, `lib/config_registry.py`, `lib/dispatch_plan.py`, `lib/dispatch_serialization.py`, `lib/tmux_worktree.py`

### 2.4 Geen (b) en geen tmux-gedrag: alleen naam, label of commentaar (blijven staan)

61 modules: `dashboard/api_agent_stream.py`, `dashboard/token-dashboard/app/agent-stream/page.tsx`, `dashboard/token-dashboard/components/operator/agent-selector.tsx`, `scripts/benchmark/field-tests/runners/lane_adapter.py`, `scripts/benchmark/field-tests/runners/scorer.py`, `scripts/check_env_isolation.sh`, `scripts/cleanup_reviewed_worktrees.py`, `scripts/codex_final_gate.py`, `scripts/commands/doctor.sh`, `scripts/launchd/com.vnx.dashboard-generator.plist`, `lib/_streaming_drainer.py`, `lib/append_receipt_internals/payload.py`, `lib/append_receipt_internals/register_emit.py`, `lib/atomic_io.py`, `lib/auto_merge_policy.py`, `lib/cleanup_worker_exit.py`, `lib/dispatch_broker.py`, `lib/dispatch_envelope.py`, `lib/dispatch_flags.py`, `lib/dispatch_metadata_db.py`, `lib/dispatch_outcome_classifier.py`, `lib/dispatch_process_registry.py`, `lib/dispatch_worktree_isolation.py`, `lib/envelope_govern_support.py`, `lib/event_store.py`, `lib/failure_classification.py`, `lib/final_prompt_integrity.py`, `lib/gate_obligations.py`, `lib/gate_worktree.py`, `lib/gh_pr_ensure.py`, `lib/git_common.py`, `lib/git_target_guard.py`, `lib/governance_emit.py`, `lib/headless_adapter.py`, `lib/launchd_receipt_processor_guard.sh`, `lib/merge_preflight_ci_check.py`, `lib/phantom_guard.py`, `lib/plan_gate_enforcement.py`, `lib/plan_gate_panel.py`, `lib/plan_gate_tiebreaker.py`, `lib/pr_enforcement.py`, `lib/provider_spawns/kimi_spawn.py`, `lib/receipt_processor/rp_extract.sh`, `lib/receipt_provenance.py`, `lib/receipt_schema.py`, `lib/report_path.py`, `lib/report_to_receipt_converter.py`, `lib/role_application.py`, `lib/subprocess_dispatch_internals/recovery.py`, `lib/token_harvest.py`, `lib/vnx_tag_vocabulary.py`, `lib/worker_permissions.py`, `scripts/migrate_dispatch_metadata_provider.py`, `scripts/migrate_to_central_vnx.py`, `scripts/panel.py`, `scripts/pr_merge.py`, `scripts/quality_db_init.py`, `scripts/vnx_doctor.py`, `scripts/vnx_init.py`, `scripts/vnx_install.py`, `scripts/vnx_setup.py`.

### 2.5 Geen (b), geen tmux-code, buiten dit plan

| Module | Reden |
|---|---|
| `lib/append_receipt_internals/common.py` | leest `VNX_ADAPTER_T0` om `terminal_snapshot` te stempelen; blijft |
| `lib/dispatch_bridge.py` | commentaar |
| `lib/dispatch_cli.py` | de deur; weigert de verwijderde `force_tmux`-sleutels bewust |
| `lib/dispatch_sidedoor_audit.py` | CI-poort op zijdeuren; geen tmux-code |
| `lib/governance_evaluator.py` | geen tmux-code; dode-code-beslissing hoort niet bij dit plan |
| `lib/incident_taxonomy.py` | vocabulaire voor storingsklassen; tmux-transportfouten blijven bestaan voor (b) |
| `lib/outcome_signals.py` | geen tmux-code |
| `lib/pool_worker_runner.py` | VNX_POOL_TASK_CONSUMER; tmux-treffer is alleen `DispatchBroker` |
| `lib/report_body_contract.py` | herkent de placeholder-body van de oude lane als ongeldig; blijft als bewaker |
| `lib/subprocess_adapter.py` | governed headless lane; tmux-treffers zijn veldnamen |
| `lib/subprocess_dispatch.py` | commentaar; raw-form valt onder ADR-025 |
| `lib/vnx_mode.py` | tekstbeschrijving van modi |
| `lib/vnx_starter.py` | no-tmux startmodus; een functie schrijft `panes.json` |

## 3. Verborgen afhankelijkheden eerst losknippen

De eerste vier komen uit de opdracht. De rest vond ik bij het lezen van de code. Een plak die iets weghaalt waar een van deze aan hangt, komt na de plak in de derde kolom.

| # | Afhankelijkheid | Bewijs | Losgeknipt door |
|---|---|---|---|
| H1 | `vnx resume` faalt gesloten zonder levende dispatcher. `vnx pause` stopt die als verplichte daemon. | `resume.sh:44-52`, `pause.sh:119-121`, B §6.1 | P02 |
| H2 | `receipt_processor.sh` hangt aan `pane_manager.sh`. Bij elke start draait `check_pane_health` en `setup_pane_titles` (de enige aantoonbaar draaiende tmux-code). | `receipt_processor.sh:43,633-636`, `rp_dedup.sh:139-145`, log 2026-09-22 17:10:51 | P03 en P04 |
| H3 | SEOcrawler_v2 en circa 15 worktrees wijzen naar `hooks/vnx_handover_detector.sh`. | B §2.3 (E9) | Geen plak raakt die bestanden. Ze blijven tot O-3. |
| H4 | `queue_popup_watcher.sh` raakt bij herstart 4351 bundelbestanden aan. `vnx resume` start hem. | B §5.5, `resume.sh:90-106` | P02 neutraliseert het herstartpad, P13 verwijdert de watcher |
| H5 | `daemon_register.read_daemon_register()` leest `start_all()` uit `vnx_supervisor_simple.sh`. | `daemon_register.py:108-190`, `tests/test_daemon_register.py:157` | P01 |
| H6 | `dispatcher_minimal.sh` host 15 functies waar libs van afhangen. Twee zijn (b): `get_terminal_provider` (`:171`) en `get_context_reset_command` (`:192`). | eigen scan van alle `scripts/lib/*.sh` | P05 |
| H7 | De skill-prefix per provider (`$skill` voor codex, `@skill` voor gemini, `/skill` voor de rest) staat in `dispatch_create.sh`, dat met de dispatcher verdwijnt. | `dispatch_create.sh:455-480` | P05 |
| H8 | Andere starters van dezelfde scripts: `vnx restart`, dashboard-restart en de kill-lijst. Twee namen in de dashboardtabel wijzen al naar niet-bestaande bestanden. | `vnx_process_ux.py:42-53`, `serve_dashboard.py:63-73`, `bin/vnx:927-960` | P13, P14, P17 |
| H9 | De legacy `.md`-stroom staat nog in de canonieke T0-rol. `VNX_QUEUE_POPUP_ENABLED=0` promoveert direct naar `pending/` zonder menselijke poort. | `role-orchestrator.md:195-209`, `pr_queue_manager.py:1119` | P20 |
| H10 | `pool_manager.py` en `pool_reaper.py` roepen tmux rechtstreeks aan en breken de freeze-test. | `test_tmux_adapter_interface.py` (gemeten) | P11 |
| H11 | De SessionStart-hook draait `list_escalations` en bouwt terminals, dat via `allow_tmux_probe=True` een `tmux list-panes -a` kan starten. | `build_t0_state.py:938,445-449`, B §4.1 | P12 en P15 |
| H12 | De zijdeur-audit noemt `dispatch_deliver.sh` in `DOOR_ROUTED_CALLERS`. | `dispatch_sidedoor_audit.py:179` | P18 |
| H13 | `lease_sweep.py` en `runtime_supervise.py` hebben de dispatcher als enige host. | B §6.1 nr 3 | O-4, dan P17 |
| H14 | `worker_permission_relay.py:926` importeert `_SESSION_RE` uit `orphan_sweep.py`. | A (`orphan_sweep.py`) | P12 vóór P16 |
| H15 | `scripts/ci/check_test_exclusions.py` faalt op een uitsluitregel waarvan het pad niet meer bestaat. | `test_exclusions.txt:206-208`, B §4.4 | elke plak die een uitgesloten test verwijdert |
| H16 | De receipt-pull is beschreven in `docs/core/DISPATCH_RULES.md:218-222` maar staat niet in `.claude`. `grep -rln receipt_query .claude` levert niets. De vlag `VNX_RECEIPT_T0_PUSH` blijft volgens ADR-035 §5.3 tot pull bevestigd is. | eigen meting | M-7 vóór P03 |

## 4. De plakken

Titels van de PR's staan onderaan (paragraaf 11). Elke plak noemt: bestanden, regels, wat verandert, de check vooraf, de tests die groen blijven, de afhankelijkheid en het (b)-oordeel.

### Fase 1: losknippen

#### P01. Daemon-register los van `start_all()`
- **Bestanden:** `scripts/lib/daemon_register.py` (284), `scripts/generate_daemon_liveness_md.py` (119), `docs/core/DAEMON_LIVENESS.md` (21, gegenereerd), `tests/test_daemon_register.py` (283), `tests/test_generate_daemon_liveness_md.py` (133), `tests/test_architecture_doc_drift.py` (141), `tests/test_build_t0_state_register_reader.py`.
- **Regels:** +150 / -110 [schatting; `read_daemon_register` is zelf 85 regels, gemeten].
- **Wat:** het register wordt een expliciete tabel. Verwacht: `receipt_processor` en `dashboard`, elk met hun launchd-label. Geparkeerd met reden uit `beacon_register._INTELLIGENCE_LAYER_PARKED`: `intelligence_daemon` en `recommendations_engine`. De vijf overige namen verdwijnen. `measure_daemon_liveness` krijgt een toestand `parked`. Een test eist dat het launchd-sjabloon van elke verwachte daemon in `scripts/launchd/` bestaat.
- **Check vooraf:** `grep -rnE "read_daemon_register|_SUPERVISOR_RELATIVE_PATH" scripts tests dashboard vnx_cli`. Alle treffers staan in de bestandenlijst.
- **Tests groen:** de vier hierboven plus `tests/test_build_t0_state*.py`.
- **Resultaat op deze host:** `daemon_liveness.overall` wordt `ok` (2 van 2 draaien, gemeten in B §5.0) en `producer_liveness` wordt `ok`.
- **Afhankelijk van:** niets. **(b):** raakt (b) niet.

#### P02. `vnx pause` en `vnx resume` eisen alleen de receipt-processor
- **Bestanden:** `scripts/commands/resume.sh` (179), `scripts/commands/pause.sh` (178), `tests/test_vnx_pause_resume.py` (376).
- **Regels:** +50 / -95 [schatting].
- **Wat:** `_vnx_pause_stop_daemons` stopt alleen `receipt_processor`. `_vnx_resume_start_daemons` start alleen die daemon (de launchd-guard blijft). `_vnx_resume_verify_readiness` eist alleen die ene PID. Het starten van `queue_popup_watcher.sh` (`resume.sh:90-106`) vervalt. Daarmee is H4 al neutraal voordat P13 het script verwijdert.
- **Check vooraf:** `grep -nE "dispatcher|queue_watcher|queue_popup" scripts/commands/pause.sh scripts/commands/resume.sh`. Voer `vnx pause` of `vnx resume` niet uit op de live store. De tests draaien op tijdelijke mappen.
- **Tests groen:** `tests/test_vnx_pause_resume.py`.
- **Afhankelijk van:** niets. **(b):** nee.

#### P03. Receipt-push naar de T0-pane weg (`VNX_RECEIPT_T0_PUSH`)
- **Bestanden:** `scripts/lib/receipt_processor/rp_delivery.sh` (472), `tests/test_rp_delivery_gates.sh` (439), `docs/operations/RECEIPT_PIPELINE.md:92,170`, `docs/core/DISPATCH_RULES.md:220`, statusnotitie onderaan `ADR-035-receipt-v2.md` (§8 beloofde deze verwijdering als "documented follow-up").
- **Regels:** +40 / -191 [gemeten] en nog -254 als de outbox meegaat en -250 testcode [schatting].
- **Wat eruit:** `_rpd_verify_submit` (28-47), `_rpd_paste_and_verify` (48-94), `_deliver_receipt_to_t0_pane` (95-160), `_rpd_build_id_list` (161-183), `_rpd_deliver_digest` (184-218) en de vlag-guards op `:118` en `:189`. De outbox (`send_receipt_to_t0` 259-310, `_retry_pending_receipts` 311-472 en de foutentellers 219-258, samen 254 regels) gaat mee als de check bevestigt dat alleen de paste ze gebruikt. Anders blijft de outbox als duurzame schrijfkant.
- **Check vooraf:** (1) `grep -rIn "VNX_RECEIPT_T0_PUSH" ~/.zshrc ~/Library/LaunchAgents ~/.vnx-data/*/ ~/Development/*/.env* 2>/dev/null` moet leeg zijn. Staat er een `=1`, dan blijft de vlag als rollback voor die omgeving. (2) M-7: bevestig dat de T0-cyclus `receipt_query.py pull` echt draait.
- **Tests groen:** `tests/test_rp_delivery_gates.sh` (herschreven op alleen `suppressed` en de outbox, met de hand draaien tot P07) en `tests/test_receipt_processor_*.py`.
- **Afhankelijk van:** M-7. **(b):** nee. T0 draait Opus 5.5 volgens `t0-opus-only`. ADR-035 §5.3 noemt pull de vervanger.

#### P04. `receipt_processor` los van `pane_manager.sh`
- **Bestanden:** `scripts/receipt_processor.sh` (658), `scripts/lib/receipt_processor/rp_dedup.sh` (153), `scripts/pane_manager.sh` (458), `tests/test_pane_manager_scope.sh` (53).
- **Regels:** +10 / -75 [gemeten voor de 41 regels in `pane_manager.sh`, schatting voor de rest].
- **Wat:** `receipt_processor.sh:41-43` (source) en `:633-636` (pane-health) weg. In `rp_dedup.sh:139-145` vervalt de paste naar de T0-pane. De bestaande `log "ERROR" "FLOOD DETECTED"` blijft de melding. In `pane_manager.sh` verdwijnen `setup_pane_titles` (260-278) en `check_pane_health` (279-300). De adresseerfuncties blijven, want dat is (b).
- **Check vooraf:** `grep -nE "get_pane_id|discover_pane|setup_pane_titles|check_pane_health" scripts/receipt_processor.sh scripts/lib/receipt_processor/*.sh` is leeg na P03.
- **Meting na afloop:** `launchctl kickstart` van de receipt-processor logt geen "Some panes are not healthy" meer in `receipt_processing.log`.
- **Tests groen:** `bash tests/test_pane_manager_scope.sh` en `tests/test_receipt_processor_*.py`.
- **Afhankelijk van:** P03. **(b):** `pane_manager.sh` blijft als (b)-adressering. Klein met opzet: het is de enige draaiende tmux-code en moet apart terugdraaibaar zijn.

### Fase 2: (b) expliciet maken en beschermen

#### P05. (b)-naad, deel 1: `scripts/lib/pane_injection.sh`
- **Bestanden:** nieuw `scripts/lib/pane_injection.sh`. Bron: `scripts/lib/dispatch_deliver.sh` (612), `scripts/dispatcher_minimal.sh` (664), `scripts/lib/dispatch_create.sh` (537).
- **Regels:** +230 / -200 [gemeten aan regelnummers, plus 30 regels kop en source-regels].
- **Wat:** verplaatsen zonder gedragswijziging. Uit `dispatch_deliver.sh`: `tmux_send_best_effort`, `_ddt_buffer_name`, `tmux_load_buffer_safe`, `tmux_retry` (samen 51 regels), `_clear_terminal_context` (39), `_activate_non_claude_mode` (27) en `_ddt_send_content` (33). Uit `dispatcher_minimal.sh`: `get_terminal_provider` en `get_context_reset_command` (27). Uit `dispatch_create.sh`: `_pdp_build_skill_command` (24). De drie bronbestanden sourcen het nieuwe bestand. Dit lost H6 en H7 op.
- **Check vooraf:** `grep -rnE "get_terminal_provider|get_context_reset_command|_pdp_build_skill_command" scripts bin hooks vnx_cli tests`. Alleen de nieuwe plek, de aanroepers en tests komen terug.
- **Tests groen (met de hand, in de voorgrond, want geen CI-runner):** `bash tests/test_dispatch_deliver_named_buffer.sh`, `bash tests/test_dispatcher_mode_enhancement.sh`, `bash tests/test_dispatcher_subprocess_preflight_skip.sh`, `bash tests/test_dispatcher_role_alias.sh`, `python -m pytest tests/test_pr2_requeue_clear_context.py`.
- **Afhankelijk van:** niets. Moet vóór P17 en P18. **(b):** dit is de (b)-plak.

#### P06. (b)-naad, deel 2: modelwissel
- **Bestanden:** `scripts/lib/dispatch_deliver.sh`, `scripts/lib/pane_injection.sh`, `scripts/lib/model_routing.sh` (187, blijft).
- **Regels:** +95 / -81 [gemeten: `_stm_send_switch_command` 48, `switch_terminal_model` 33].
- **Wat:** beide functies naar `pane_injection.sh`, zonder wijziging. `model_routing.sh` beslist per provider of wisselen kan (claude en codex ja, gemini `unsupported`). De claude-normalisatie `opus` naar `default` reist mee als onderdeel van de functie.
- **Tests groen:** `bash tests/test_model_routing_verification.sh` (350 regels, 5 codex-, 10 gemini-verwijzingen).
- **Afhankelijk van:** P05. **(b):** ja. Uncertain: of de codex-tak van het modelwissel ooit tegen een echte codex-pane is gemeten.

#### P07. (b) beschermen: uitgesloten tests herstellen, shelltests laten draaien, contracttest
- **Bestanden:** `scripts/ci/test_exclusions.txt` (regels 206 en 212), `tests/test_tmux_adapter.py` (699), `tests/test_vnx_start_runtime.py` (437), nieuw `tests/test_shell_pane_suites.py`, nieuw `tests/test_pane_injection.py`. `tests/test_tmux_adapter_interface.py` blijft uitgesloten tot P11.
- **Regels:** +290 / -50 [schatting].
- **Wat:** (1) de drie `tmux_adapter_cli`-tests verwijderen (de module bestaat niet meer). (2) `VNX_SOCKETS_DIR` in de verwachte set van `test_vnx_start_runtime.py:53`. (3) `shutdown(graceful=...)` in lijn brengen. `HeadlessAdapter` heeft dezelfde fout (`test_adapter_conformance.py`, `test_exclusions.txt:102`), dus dat wordt één beslissing voor beide. (4) twee uitsluitregels schrappen. (5) `test_shell_pane_suites.py` draait `test_dispatch_deliver_named_buffer.sh`, `test_model_routing_verification.sh`, `test_input_mode_guard.sh` en `test_pane_manager_scope.sh` via `bash` en eist exit 0. Een test die echte tmux nodig heeft krijgt `skipif` zonder `tmux` op PATH. (6) `test_pane_injection.py` bronnen `pane_injection.sh` met een nep-`tmux` op PATH dat argv opneemt. Het pint per provider: codex krijgt één `load-buffer` en `paste-buffer` met skill en prompt samen, daarna Enter als aparte keystroke. Gemini en kimi krijgen de skill via `send-keys -l` en dan de paste. De reset is `/new` voor codex en `/clear` voor de rest. `/plan` gaat alleen naar codex. Gemini krijgt geen toetsaanslagen voor een modus. Foutcode `tx_load_buffer_codex`. De buffer wordt op elk pad verwijderd.
- **Let op:** de kimi-test legt het huidige gedrag vast (generieke tak). Ze bewijst niet dat een echte kimi-REPL dat aanneemt (O-7).
- **Check vooraf:** M-5, de vier shelltests exit 0 solo.
- **Tests groen:** alles hierboven plus `tests/test_adapter_conformance.py`.
- **Afhankelijk van:** P05 en P06. **(b):** ja. Dit maakt (b) expliciet en getest, zoals het operatorbesluit vraagt.

### Fase 3: (a) verwijderen, kleinste risico eerst

#### P08. Dode bestanden zonder aanroeper
- **Bestanden:** `scripts/update_pane_mapping.sh` (122), `scripts/dispatch_broker_cli.py` (368), `scripts/reconcile_terminal_state.py` (47), `scripts/runtime_coordination_init.py` (301), `scripts/vulture_whitelist.py` (19). Edits: `scripts/lib/tracks.py:213` (foutmelding noemt `runtime_coordination_init.py`), `governance_enforcer.py:611-617` (leest de whitelist), `scripts/benchmark/field-tests/METHODOLOGY.md:147`, `runners/skill_smoke.py:8`, `runners/run_field_tests.py:140-150`, `tests/test_process_control_safety_sweep.sh:23`.
- **Regels:** +25 / -857 [gemeten] en -11 in `run_field_tests.py`.
- **Check vooraf:** per bestand `grep -rnI "<naam>" scripts bin hooks vnx_cli tests .github Makefile pyproject.toml`. Alleen de edits hierboven mogen terugkomen. De echte remedie voor `tracks.py:213` bepaal je bij de PR.
- **Tests groen:** `tests/test_governance_enforcer*.py`, `tests/test_tracks*.py`.
- **Afhankelijk van:** niets. **(b):** nee. `update_pane_mapping.sh` schrijft een vaste 2x2 `panes.json` en is achterhaald ten opzichte van de T0-only start (B §3.1).

#### P09. tmux-signaalhooks en dode staarten
- **Bestanden:** `scripts/hooks/tmux_signal_prompt_received.sh` (89), `tmux_signal_session_ready.sh` (51), `tmux_signal_stop_receipt.sh` (123). Staarten in `pretooluse_block_raw_claude_spawn.sh:74-87` en `pretooluse_block_subagent.sh:72-86`. Guards in `session_reconcile_autoclose.sh:66` en `session_reconcile_cleanup.sh:27`. `.claude/settings.json:73-77,123-129,146-150`, `templates/settings_vnx_keys.json.tmpl`, `docs/core/00_VNX_ARCHITECTURE.md:875-879` (regenereren met `scripts/generate_architecture_doc.py --write`), tests `tests/test_tmux_lane_signals.py` (443), `tests/test_architecture_doc_drift.py:110`, `tests/test_init_migrate_bootstrap.py:560`, `tests/test_context_rotation.py:449-451`.
- **Regels:** +45 / -780 [gemeten: 263 hooks, 443 test; rest schatting].
- **Wat:** `VNX_TMUX_SIGNAL_DIR` en `VNX_DISPATCH_ID` worden nergens gezet sinds 496e0fe5, dus de drie hooks vuren op elke sessie en stoppen bij hun guard. Alleen `tmux_signal_session_ready.sh` draait vóór die guard een `jq`.
- **Check vooraf:** `grep -rnE "(export )?VNX_TMUX_SIGNAL_DIR=|VNX_DISPATCH_ID=" scripts hooks vnx_cli bin` levert 0 toewijzingen. Mijn meting van consumentbestanden: alleen `.claude/settings.json` van deze repo en van de worktrees `vnx-wt-alpha-oi1453` en `vnx-wt-t0-verify` noemen de hooks. SEOcrawler_v2 niet. Draai daarna `vnx doctor` en `hookpin_check.sh`.
- **Waarschuwing:** gebruik niet `regen-settings --merge`. Dat vervangt `hooks` door de template en schrapt de repo-eigen hooks (`vnx_settings_merge.py:271-273`). Wijzig de settings met de hand. Een wijziging aan `.claude/settings.json` kan door de permissie-classifier worden geblokkeerd en vraagt dan expliciete toestemming van de operator.
- **Tests groen:** `tests/test_hookpin*.py`, `tests/test_architecture_doc_drift.py`, `tests/test_init_migrate_bootstrap.py`, `tests/test_context_rotation.py`.
- **Afhankelijk van:** niets. **(b):** nee. De signalen ontstonden in de verwijderde claude-lane.

#### P10. Lezers van de dode signaalmap
- **Bestanden:** `scripts/lib/toolcall_signals.py` (176), `envelope_govern.py:396`, `provider_dispatch.py:1094`, `append_receipt_internals/enrichment.py:285` (`_enrich_toolcall_signals`), `tests/test_toolcall_signals.py`, `tests/test_toolcall_signals_tmux_lane_wiring.py` (162).
- **Regels:** +30 / -380 [schatting].
- **Risico:** dit raakt het govern-pad van de deur en de receipt-verrijking.
- **Check vooraf:** (1) lees `toolcall_signals.py` en bevestig dat de signaalmap de enige bron is. (2) meet in de centrale ledger of het veld na 496e0fe5 ooit gevuld is: `grep -c '"toolcall_signals"' ~/.vnx-data/vnx-dev/state/t0_receipts.ndjson` en kijk naar de laatste 200 receipts. Ga alleen door als het veld sindsdien leeg is.
- **Tests groen:** `tests/test_envelope_govern*.py`, `tests/test_append_receipt*.py`, `tests/test_provider_dispatch*.py`.
- **Afhankelijk van:** P09. **(b):** nee.

#### P11. Pool: tmux-reap eruit
- **Bestanden:** `scripts/lib/pool_reaper.py` (252), `scripts/lib/pool_manager.py` (`_kill_tmux_session` 11 regels, `_sweep_orphan_tmux` 5 regels, aanroep op `:396-399`, directe tmux-aanroep op `:371`), `vnx_cli/commands/pool.py:175-191`, `tests/test_pool_reaper.py`, `tests/test_pool_manager_integration.py`, `scripts/ci/test_exclusions.txt:207`.
- **Regels:** +30 / -330 [gemeten 268, rest schatting].
- **Wat:** `vnx pool reap` reapt daarna alleen dode workerprocessen, zonder tmux. De freeze-test slaagt dan (nu 4 overtredingen) en `tests/test_tmux_adapter_interface.py` kan van de uitsluitlijst na één fix voor `shutdown`.
- **Check vooraf:** lees `pool_reaper.py` en bevestig dat er niets in staat dat geen tmux is.
- **Tests groen:** `tests/test_tmux_adapter_interface.py` (nu ook in CI), `tests/test_pool_manager_integration.py`.
- **Afhankelijk van:** P07. **(b):** nee. Het reapt `vnx-*`-sessies van de verwijderde lane.

#### P12. Permissie-relay voor de verwijderde claude-lane
- **Bestanden:** `worker_permission_relay.py` (1014), `worker_pane_classifier.py` (127), `permission_relay_cli.py` (452), `tmux_command_runner.py` (50), de arm `permission` in `bin/vnx:2611-2612`, `scripts/build_t0_state.py:938`, `tests/test_worker_pane_classifier.py` (149), `tests/test_worker_permission_relay.py`, `docs/operations/WORKER_PERMISSIONS.md`.
- **Regels:** +30 / -1950 [gemeten 1643 productiecode, rest schatting].
- **Bewijs voor (a):** de `PROMPT_MARKERS` zijn Claude Code-prompts. `vnx permission scan` en `approve` doen niets sinds niemand `state/tmux_interactive/<id>.json` schrijft (B: 30 handles, nieuwste 2026-08-26).
- **Check vooraf:** `grep -rn "write_escalation" scripts vnx_cli`. Schrijft alleen de relay escalaties, dan is `list_escalations` in `build_t0_state.py:938` altijd leeg en kan de aanroep weg. Anders blijft een kleine lezer.
- **Tests groen:** `tests/test_build_t0_state*.py`, `tests/test_tmux_adapter_interface.py`.
- **Afhankelijk van:** niets. Moet vóór P16. **(b):** nee. Codex-, kimi- en gemini-prompts worden niet herkend.

#### P13. Queue- en popup-laag met `smart_tap`
- **Bestanden:** `queue_popup_watcher.sh` (251), `queue_auto_accept.sh` (68), `queue_ui_enhanced.sh` (465), `popup_editor.sh` (14), `smart_tap_json_translator.sh` (677). Edits: `start.sh:472-484,561-564` (popup-binds), supervisor-regels `:204,212,215`, `bin/vnx:510,526,542,707` (`VNX_QUEUE_POPUP_ENABLED`), `bin/vnx:927-960` (kill-lijst), `vnx_process_ux.py:46-47`, `serve_dashboard.py:64,66`, `tests/test_queue_auto_accept_no_local_outside_function.py`.
- **Regels:** +25 / -1550 [gemeten 1475].
- **Wat:** `smart_tap` leest het T0-scherm en schrijft `queue/*.md`. De popup was de menselijke poort voor die map. Die poort zit nu in `vnx dispatch <pending-id>` en `dispatches.operator_approved_at` (B §5.5). `pr_queue_manager.py:1119` blijft in deze plak ongemoeid (dat is P20).
- **Na afloop, door de operator:** `tmux unbind-key -T root C-g`. De tmux-server heeft nog een binding naar een kopie in `SEOcrawler_v2` (B §3.1).
- **Check vooraf:** `ls dispatches/queue dispatches/pending dispatches/active` bevat 0 `.md`-bestanden (B mat 0, 0, 0 op 2026-09-24). P02 is gemerged.
- **Tests groen:** `tests/test_queue_reconciler.py`, `tests/test_pr_queue_*.py`.
- **Afhankelijk van:** P02. **(b):** nee. Het scherm dat `smart_tap` leest is dat van de claude-T0.

#### P14. ACK-monitor en state manager
- **Bestanden:** `heartbeat_ack_monitor.py` (936), `notify_dispatch.py` (73), `unified_state_manager.py` (486), `dispatch_lifecycle.sh:587` (aanroep), lezers `t0_query.py`, `query_quality_intelligence.py:288`, `check_intelligence_health.py:347`, `templates/footers/t0_action_request_autonomous.md:11,62`. Tests: `test_ack_monitor_stdin_mode.py`, `test_heartbeat_ack_monitor_exception_handling.py`, `test_heartbeat_ack_named_buffer.py`, `test_unified_state_manager_shim.py`.
- **Regels:** +130 / -1700 [gemeten 1495, rest schatting].
- **(b)-deel eerst:** `_check_terminal_activity` (42 regels, `:483-524`) bevestigt dat de CLI in een geïnjecteerde pane aan het werk gaat. Die verhuist naar een nieuw `scripts/lib/pane_activity.py` (circa 60 regels, met test). De rest is (a): de T0-ACK-paste (`_notify_t0_ack`, 55 regels) en de socket.
- **Check vooraf:** M-6, wie leest `terminal_state.json` behalve de reconciler: `grep -rn "terminal_state.json\|terminal_state_shadow" scripts dashboard vnx_cli`.
- **Tests groen:** nieuwe `tests/test_pane_activity.py`, `tests/test_terminal_state*.py`.
- **Afhankelijk van:** P05 en P13. **(b):** deels, zie boven.

#### P15. Terminal-probes en tmux-tekst in gedeelde code
- **Bestanden:** `terminal_state_reconciler.py::_probe_tmux` (74), `terminal_snapshot.py::get_terminal_state_from_tmux` (56, fallback `:233-239`), `canonical_state_views.py` (parameter `allow_tmux_probe`, aanroepers `:500` en `:540`), `worker_heartbeat.py::build_process_gone_failure_report` (58, alleen een test roept hem aan), `tests/test_model_canonicity.py`.
- **Regels:** +30 / -230 [gemeten 188, rest schatting].
- **Check vooraf:** M-2. Bevestig met een trace dat de SessionStart-hook nog `tmux list-panes -a` draait (B §4.1 leidde dit alleen af).
- **Tests groen:** `tests/test_terminal_state_reconciler*.py`, `tests/test_canonical_state_views*.py`, `tests/test_model_canonicity.py`.
- **Afhankelijk van:** P14, M-2. **(b):** nee. Het is een statusbord voor T0 tot T3. Injectiegereedheid komt uit `input_mode_guard.sh`.

#### P16. `orphan_sweep`: tmux-soorten eruit, `cleanup_stale_vnx_sessions.sh` weg
- **Bestanden:** `scripts/lib/orphan_sweep.py` (1058), `scripts/orphan_sweep.py` (167, blijft als wrapper), `scripts/cleanup_stale_vnx_sessions.sh` (114), `tests/test_orphan_sweep.py` (1372), `tests/test_orphan_sweep_fail_open.py` (299), `tests/test_cleanup_stale_vnx_sessions.py`, `test_exclusions.txt:117`.
- **Regels:** +150 / -550 [gemeten 87 (zes hulpfuncties) plus 114, rest schatting]. Komt `+` boven 300 uit, splits dan in `cleanup_stale_vnx_sessions.sh` en `orphan_sweep.py`.
- **Wat:** soort 1 en 1b (tmux-sessies `vnx-<dispatch_id>` van de verwijderde lane) eruit. Soort 2 en 3 (worktrees en `dispatches/active/`-manifesten) blijven, want de deur heeft ze nodig. Het bestand is dus een inkorting en geen verwijdering.
- **Tests groen:** `tests/test_orphan_sweep.py` en `tests/test_orphan_sweep_fail_open.py`, ingekort.
- **Afhankelijk van:** P12. **(b):** nee.

#### P17. Daemon-hoofdlussen weg: dispatcher, supervisors, tick-host
- **Bestanden:** `dispatcher_minimal.sh` (664), `dispatcher_supervisor.sh` (193), `scripts/lib/dispatcher_supervisor_ticks.sh` (223), `vnx_supervisor_simple.sh` (437). Edits: `vnx_process_ux.py:42-53`, `serve_dashboard.py:63-73,720-770`, `bin/vnx:927-960`, `start.sh:309,321-336,437-464`. Tests: `test_dispatcher_supervisor.py` (344), `test_dispatcher_resilience.py`, `test_dispatcher_drain_lifecycle.py`, `test_oi_bridge_supervisor_tick.py`.
- **Regels:** +40 / -1950 [gemeten 1517].
- **Wat:** de vlag `VNX_SUPERVISOR_MODE` verdwijnt met de vijf tickplekken. De twee geparkeerde daemons blijven in `MANAGED_PROCESSES` en de dashboard-restarttabel, zodat ze bij het uit parkeren nog een startroute hebben. `lease_sweep.py` en `runtime_supervise.py` verliezen hun enige host en blijven staan tot O-4.
- **Check vooraf:** P01, P02, P05, P06, P13 en P14 zijn gemerged. M-1 bevestigt dat `vnx start` nergens anders draait. O-4 is beslist of bewust uitgesteld.
- **Tests groen:** `tests/test_daemon_register.py`, `tests/test_vnx_pause_resume.py`, de tests uit P05.
- **Afhankelijk van:** zie check. **(b):** de daemon is (a). De (b)-helpers zijn dan al verplaatst. Deze plak haalt de laatste runtime-aanroeper van (b)-injectie weg. Het operatorbesluit staat dat toe, maar O-1 bepaalt de nieuwe aanroeper.

#### P18. Dispatcher-eigen libs
- **Bestanden (bovengrens, de check per functie bepaalt de rest):** `dispatch_create.sh` (513 na P05), `dispatch_lifecycle.sh` (600), `dispatch_metadata.sh` (198), `provider_routing.sh` (52), `scripts/log_dispatch_metadata.py` (207), `dispatch_deliver.sh` (381 na P05 en P06). Tests: `test_dispatch_metadata_lib.sh`, `test_dispatch_lifecycle_register_events.py`, `test_dispatcher_*.sh`, `test_pr4_role_capture_backfill.py:311`.
- **Regels:** +5 / -1950 productiecode [gemeten] en -350 test [schatting]. `dispatch_sidedoor_audit.py:179` verliest de entry `dispatch_deliver.sh`.
- **Blijft (b):** `pane_injection.sh`, `input_mode_guard.sh`, `model_routing.sh`.
- **Check vooraf per functie:** `for fn in $(grep -oE '^[a-zA-Z_][a-zA-Z0-9_]*\(\)' <bestand> | tr -d '()'); do grep -rn "$fn" scripts bin hooks vnx_cli tests | grep -v "<bestand>"; done`. Wat een niet-verwijderd bestand nog aanroept, blijft.
- **Tests groen:** `tests/test_dispatch_sidedoor_audit.py` en de tests uit P05 tot P07.
- **Afhankelijk van:** P17. **(b):** nee voor wat verdwijnt.

#### P19. `vnx start`, `stop`, `jump`, sessieprofiel en de tmux-fase van recover en doctor
- **Bestanden:** `commands/start.sh` (636), `stop.sh` (28), `jump.sh` (170), `lib/tmux_session_profile.py` (676), `pane_config.sh` (66), `lib/tmux_conversation_normalizer.py` (199), `vnx_recovery_phases._phase_tmux_reconciliation` (97), `vnx_doctor_checks.check_tmux_profile` (92, aangeroepen op `vnx_doctor_runtime.py:115`), `commands/recover.sh`, tests `test_tmux_session_profile.py` (612), `test_tmux_conversation_normalizer.py`.
- **Twee uitkomsten, O-2 beslist:** (A) `vnx start` verdwijnt volledig. `vnx_start_runtime.py` blijft voor de launchcommando's van (b). Regels +30 / -2100 [gemeten 1964 productiecode]. (B) `vnx start` wordt een T0-launcher zonder daemons, popup en `panes.json`. `start.sh` krimpt dan van 636 naar circa 150. Regels +60 / -1500 [schatting].
- **Opinion:** kies (A) tenzij O-1 voor `vnx start` een rol als launcher van interactieve niet-claude panes kiest. Reden: geen `vnx-*`-sessie, alle daemon-logs eindigen op 2026-06-27 (B §4.2) en de operator-T0's draaien in eigen wrapper-sessies.
- **Tests groen:** `tests/test_vnx_start_runtime.py` (uit P07) en `tests/test_recover*.py`.
- **Afhankelijk van:** P17, O-2. **(b):** `start.sh` en `tmux_session_profile.py` zijn ONBEPAALD (paragraaf 2.2). Geen van beide verdwijnt vóór O-2. `pane_manager.sh` ontdekt panes op titel of pad en heeft `panes.json` niet nodig.

#### P20. Legacy `.md`-stroom sluiten
- **Bestanden:** `bin/vnx:2515-2523` (`promote`), `scripts/pr_queue_manager.py:1065-1170` (promote naar `queue/`, `VNX_QUEUE_POPUP_ENABLED` op `:1119`), `.claude/terminals/T0/role-orchestrator.md:195-209`, `scripts/commands/t0_role_audit.sh` (tmux-paden `:345,:401`, skip-pad naar het verwijderde `tmux_interactive_dispatch.py`).
- **Regels:** +40 / -300 [schatting].
- **Waarom aparte plak:** na P13, P14 en P17 leest niemand `queue/`, dus `vnx promote` schrijft naar een map zonder lezer. `VNX_QUEUE_POPUP_ENABLED=0` is een auto-approve-schakelaar (queue naar pending zonder poort). Los verwijderen zou de menselijke poort stil verschuiven. De poort zit nu in `vnx dispatch stage` en `operator_approved_at`. ADR-025 (item B) plant dezelfde ontkoppeling voor het rauwe `.md`-pad.
- **Operatorstap:** de canonieke rol wijzigt. `vnx role sync --apply` per repo is een operatorbesluit. `fleet_role_drift` staat nu al op fail door echte drift.
- **Tests groen:** `tests/test_t0_role_audit_static.py`, `tests/test_pr_queue_*.py`.
- **Afhankelijk van:** P13, P17, O-5. **(b):** nee.

### Fase 4: documentatie en namen

#### P21. Documentatie en gegenereerde docs
- **Bestanden:** repo-`CLAUDE.md` (blok "Supervisor Mode", de aanbeveling `dispatcher_supervisor.sh` te wrappen, `<important if="working on tmux delivery or session hooks">`, de regel over TmuxAdapter-routed terminals bij Event Streams), `docs/operations/UNIFIED_SUPERVISOR.md`, `docs/core/00_VNX_ARCHITECTURE.md` (regenereren), `docs/operations/WORKER_PERMISSIONS.md:93,133,207`, `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`, `scripts/commands/doctor.sh:260,270`, `scripts/vnx_init.py:706,708` ("full tmux grid"), `scripts/vnx_setup.py`, `com.vnx.dashboard-generator.plist:12,22`, `scripts/panel.py:89`.
- **Bewust erbij:** één alinea in `docs/core/DISPATCH_RULES.md` die (b) als capability beschrijft (tmux als injectiepad naar interactieve kimi, codex en gemini) met een verwijzing naar `pane_injection.sh` en `tests/test_pane_injection.py`.
- **Regels:** +120 / -120 [schatting].
- **Afhankelijk van:** P17. **(b):** de alinea maakt (b) zichtbaar.

#### P22. Naamgeving: lane-labels (optioneel)
- **Bestanden:** `providers/wave7_models.yaml:566-588` (`lane: tmux_interactive`), `benchmark/field-tests/lane_calibration.yaml`, `tasks.yaml:62`, `dispatch_govern.py:360,864,976-985`, tests `tests/smart_router/test_tier_routing.py:69`, `tests/test_lane_calibration_field_test.py:26`.
- **Regels:** +40 / -40 [schatting]. Risico: de registerwaarde wordt door router en tests gelezen. Hernoemen zonder alias.
- **Afhankelijk van:** niets. **(b):** nee.

#### P23. Naamgeving: lock-klasse, omgevingsvariabele en worktreemodule (optioneel)
- **Bestanden:** `claude-tmux` in `dispatch_plan.py:401`, `dispatch_serialization.py:4,386,436,447`, `dispatch_cli.py:3552`, `config_registry.py:313-323`, `commands/dispatch.sh:201,218,278`, `vnx_cli/commands/subsystems.py:98,145`, `docs/core/SUBSYSTEMS.md:32`, `check_live_requires_measured_health.py`, `tests/test_live_requires_measured_health.py:55-75`. `VNX_TMUX_MAX_CONCURRENT` (`dispatch_serialization.py:148-158`, `config_registry.py:315-323`). `tmux_worktree.py` (543) met `check_no_file_derived_data_paths.py:177,271`, `tests/test_central_mode_path_gate.py:384-391`, `tests/test_tmux_worktree.py`, `test_exclusions.txt:208`.
- **Regels:** +120 / -120 [schatting], eventueel twee PR's.
- **Risico:** bestaande lockbestanden dragen de oude klassenaam. Voer uit als er geen dispatch loopt.
- **Afhankelijk van:** P22 en P17. **(b):** nee. `tmux_worktree.py` bevat geen tmux-aanroepen (A). Het is git-levenscyclus voor de deur.

## 5. Volgorde

```
P01, P02, P08                      onafhankelijk, kunnen tegelijk
P03 -> P04                         receipt_processor tmux-vrij (M-7 vóór P03)
P05 -> P06 -> P07                  (b) expliciet en beschermd, vóór alles wat (b) raakt
P09 -> P10
P07 -> P11
P12 -> P16
P02 -> P13 -> P14 -> P15           (P14 ook na P05; P15 na M-2)
P01, P02, P05, P06, P13, P14 -> P17 -> P18 -> P19 (O-2), P21
P13, P17 -> P20 (O-5)
P22, P23                           laatste, optioneel
```

De reeks vermijdt de valkuilen die B noemt: `receipt_processor` wordt eerst tmux-vrij (P03, P04), pause en resume worden herschreven vóór de dispatcher verdwijnt (P02 vóór P17) en het register leunt niet meer op de supervisor vóór die weggaat (P01 vóór P17).

## 6. Per vlag

| Vlag | Default | Besluit | Reden | Plak |
|---|---|---|---|---|
| `VNX_RECEIPT_T0_PUSH` | 0 | Weghalen, na M-7 | ADR-035 §5.3 hield de vlag tot pull bevestigd is. Gemeten 67 keer `suppressed` in de laatste 3 MB van `receipt_processing.log` (B §3.6). De T0-skill bevat de pull-stap niet (OI-188), dus eerst M-7. Vindt de check een `=1`, dan blijft de vlag als rollback. | P03 |
| `VNX_DISPATCH_LEGACY` | ongezet | Houden, buiten dit plan | Geen tmux-vlag. Het is het terugvalluik van de deur naar het rauwe `.md`-pad op de subprocess-lane (`dispatch_flags.py:35`, `dispatch.sh:346-350`). Verwijdering staat gepland onder ADR-025 item B. De weigering van `--adapter tmux` (`dispatch.sh:548-552`) is een guard en blijft tot dan. | niet in plan |
| `VNX_ADAPTER_T{n}` | T0 tmux, T1 tot T3 subprocess in de dispatcher. De ACK-monitor leest ontbrekend als tmux. `dispatch_create.sh:253` behandelt alleen T1 zo. | Houden: dit is de schakelaar van (b) | Drie plekken spreken elkaar tegen en alle drie zitten in code die P14 en P18 verwijdert. Daarna leest alleen `append_receipt_internals/common.py:109` de variabele. Het nieuwe aanroeppad van (b) legt de default in één plek vast (O-1). | P14, P18, O-1 |
| `VNX_SHARED_PREPARE` | 0 | Houden (ONBEPAALD) | Geen tmux-vlag. De aan-tak (`dispatch_prepare.py`) draagt permissiepreambule, worker-rules-footer en report-contract-directief. De live tak mist ze mogelijk. Het is een besluit over een onafgemaakte migratie. De docstring "both tmux and subprocess Claude lanes" is de enige tmux-link. | niet in plan |
| `VNX_POOL_TASK_CONSUMER` | ongezet | Houden, buiten plan | Alleen de import van `DispatchBroker` matcht de scope-regex. | niet in plan |
| `VNX_TMUX_SIGNAL_DIR`, `VNX_DISPATCH_ID` | leeg | Weghalen | Nergens gezet sinds 496e0fe5. Drie hooks en twee staarten vuren zonder effect. | P09, P10 |
| `VNX_QUEUE_POPUP_ENABLED` | 1 | Weghalen in twee stappen | `=0` is een auto-approve-schakelaar. Start-, supervisor- en presetplekken gaan met de popup (P13). De leesplek in `pr_queue_manager.py:1119` gaat met de promote-stroom (P20). | P13, P20 |
| `VNX_ACK_DIRECT_NOTIFY` | 1 | Weghalen | Hoort bij de ACK-monitor. | P14 |
| `VNX_SUPERVISOR_MODE` | legacy | Weghalen | Nergens `unified` gezet. Vijf tickplekken in `dispatcher_supervisor_ticks.sh`. Repo-`CLAUDE.md` beschrijft hem nog. | P17, P21 |
| `VNX_RESUME_IN_PROGRESS` | 0 | Houden | Ook de receipt-processor-guards gebruiken de bypass (`receipt_processor.sh:15`, `receipt_processor_supervisor.sh:27`). Alleen de dispatcher-guard verdwijnt. | P17 |
| `VNX_CONTEXT_ROTATION_ENABLED` | 0 in `vnx_rotate.sh`, 1 in de detector | Houden tot O-3 | Consumerbedrading buiten de repo. | niet in plan |
| `VNX_T0_ROTATION` | uit | Houden tot O-3 | Een 0-byte `session_stop_rotation.err` van 2026-07-13 suggereert dat de vlag minstens eens aan stond. | niet in plan |
| `VNX_ENFORCE_WORKER_PERMISSIONS` | 0 | Houden, buiten plan | Governancebesluit (O-6). | niet in plan |
| `VNX_TMUX_ADAPTER_ENABLED` | 1 | Houden | Hoort bij de tmux-adapter (b) tot O-1 beslist welke tweeling blijft. | niet in plan |
| `VNX_TMUX_MAX_CONCURRENT` | 10 | Hernoemen | Is een concurrency-instelling voor claude-dispatches met een tmux-naam. Hernoemen zonder alias, na een grep op setters. | P23 |
| `VNX_RUNTIME_PRIMARY` | 1 | Houden, buiten plan | Schakelt `RuntimeCore` en dus ook het laden van de adapter. Niet tmux-specifiek. | niet in plan |
| `VNX_T{n}_PROVIDER`, `VNX_GEMINI_MODEL`, `VNX_CODEX_MODEL` | | Houden | Launchconfiguratie van (b) (`vnx_start_runtime.py:116-125`). | niet in plan |

## 7. Daemon-verwachting

Meting van B op 2026-09-24: `daemon_liveness.overall = fail` (7 van 9 afwezig), `producer_liveness` = `fail`, `launchd_liveness.overall = ok`.

| Daemon | Nu | Verwacht na P01 | Na P17 | Reden |
|---|---|---|---|---|
| `receipt_processor` | draait (launchd) | ja | ja | Bron van het audit-spoor. |
| `dashboard` | draait (launchd) | ja | ja | Producent van het versheidsartefact dat SessionStart leest. |
| `intelligence_daemon` | afwezig | `parked` met reden | `parked` | Operatorbesluit 2026-09-09 (`beacon_register.PARKED_COMPONENTS`). |
| `recommendations_engine` | afwezig | `parked` met reden | `parked` | Artefact staat in `PARKED_ARTIFACTS["t0_recommendations"]`. Fase 10 van de nachtpipeline draait dezelfde generator. |
| `dispatcher` | afwezig | niet in register | code weg | Vervangen door de deur (`vnx dispatch`). |
| `smart_tap` | afwezig | niet in register | code weg (P13) | Leest het T0-scherm. T0 dispatcht via de deur. |
| `heartbeat_ack_monitor` | afwezig | niet in register | code weg (P14) | De headless lane heeft een eigen eventpijplijn. |
| `queue_watcher` | afwezig | niet in register | code weg (P13) | De poort zit in `vnx dispatch <pending-id>`. |
| `state_manager` | afwezig | niet in register | code weg (P14) | `build_t0_state.py` en `rp_append.sh` vervangen hem. |

Verwachte uitkomst na P01 op deze host: `daemon_liveness.overall = ok` en `producer_liveness = ok`. `system_health.status` blijft `degraded` zolang `beacon_health.overall = fail` (`receipt_conversion_rejections` en `report_to_receipt_converter`). Dat staat los van de daemons.

De registerbron verandert van `start_all()` naar een expliciete tabel met launchd-labels (P01). Opinion: dat is robuuster dan de shell-parse, die stil op nul entries kan uitkomen (`daemon_register.py:108-190` faalt daar nu zelf op).

## 8. Wat niet in het plan zit en waarom

| Onderdeel | Reden |
|---|---|
| De negen (b)-modules en de 21 ONBEPAALD-modules (paragraaf 2.1 en 2.2) | Operatorbesluit: bewust houden of twijfel. Alleen een beslissing kan ze verplaatsen. |
| `tmux_adapter.py` en de adapterlaag (`runtime_facade`, `headless_transport_adapter`, `local_session_adapter`, `adapter_types`) | Twee implementaties van dezelfde injectie bestaan naast elkaar: de shell-bibliotheek en `tmux_adapter.py`. Welke blijft hangt aan O-1. |
| Het dode routeringscluster (`dispatch_router`, `execution_target_registry`, `mixed_execution_router`, `fpc_certification`, `fpc_certification_checks`, samen 2.650 regels) | Het modelleert `interactive_tmux_codex` als doeltype. Niet aan de deur gekoppeld, maar het noemt precies het (b)-doel. Twijfel is ONBEPAALD. |
| De geparkeerde intelligence-laag (`intelligence_daemon.py`, `recommendations_engine_daemon.sh`, `nightly_intelligence_pipeline.sh`, `generate_t0_recommendations.py`) | Operatorbesluit 2026-09-09 (#1832). `intelligence_daemon` heeft geen enkele tmux-treffer (B §5.8). |
| Niet-tmux dode code uit A (`governance_evaluator.py` met `safe_autonomy_cutover` en `safe_autonomy_cli`, `outcome_signals.py`) | Geen tmux-code. Ze matchen de regex op `dispatch_deliver` als sleutel of op commentaar. Een dode-code-besluit hoort in een eigen plak. |
| `dispatch_sidedoor_audit.py` | CI-poort (`vnx-ci.yml:239`), geen tmux-code. |
| `scripts/lib/vnx_starter.py` (402) | No-tmux startmodus. Een functie schrijft `panes.json`. |
| `hooks/vnx_rotate.sh`, `hooks/vnx_handover_detector.sh`, `session_stop_rotation.py` | Consumerbedrading (SEOcrawler_v2 en circa 15 worktrees). O-3. |
| `pretooluse_worker_scope_enforce.py` | Veiligheidshook zonder registratie. Herbedraden of weg is een governancebesluit (O-6), geen tmux-opruiming. |
| `lease_sweep.py`, `runtime_supervise.py` | Verweesd na P17. O-4. |
| `dashboard_actions.py`, tmux-routes in `api_operator.py`, Next.js-pagina's | Gebruik onbekend (M-3). |
| `VNX_DISPATCH_LEGACY` en de rauwe `.md`-vorm van `vnx dispatch` | ADR-025 item B. |
| Het tmux-gebruik van de operator zelf: T0-sessies (`orch-t0`, `mc-t0`, `seo-t0`, `pa-engine`, `sc-t0-sm`), de shell-wrappers en de `/rotate`-skill | Staat buiten de repo. Dit plan raakt het niet. |
| Andere consumer-repo's (SEOcrawler_v2 en worktrees) | Niet aangeraakt. Ze krijgen de wijzigingen via hun eigen sync. |

## 9. Besluiten en metingen die eerst nodig zijn

**Besluiten voor de operator**

| # | Vraag | Aanbeveling |
|---|---|---|
| O-1 | Waar landt (b)? (i) een lane van de deur, (ii) een handmatige CLI, (iii) alleen bewaren als bibliotheek. Bepaalt ook welke tweeling blijft: de shell-naad of `tmux_adapter.py`. | (i). ADR-025 noemt het tmux-send-keys-pad van de daemon een zijdeur. `dispatch_sidedoor_audit.py` zoekt alleen op verwijzingen naar lane-scripts en ziet tmux-injectie dus niet. Een aanroeper buiten de deur levert een levering zonder registerregel op. |
| O-2 | Blijft `vnx start` bestaan, en zo ja als T0-launcher of als launcher van interactieve niet-claude panes? | Weghalen (P19-A). Geen `vnx-*`-sessie en alle logs eindigen op 2026-06-27. Kiest O-1 voor de launcher-rol, dan (B). |
| O-3 | Blijven de rotatiehooks voor consumers (`vnx_rotate.sh`, `vnx_handover_detector.sh`, `session_stop_rotation.py`)? | Meet eerst of `**/logs/vnx_rotate_T*.log` ooit bestond (B: niets gevonden). |
| O-4 | `lease_sweep.py` en `runtime_supervise.py`: eigen launchd-taak of weg? | Weg. Er draaide nooit een tick en `terminal_leases` heeft 3 rijen, allemaal `idle` (B §2.5). Blijft sweeping nodig, dan een eigen taak. |
| O-5 | De legacy `.md`-stroom (`vnx promote`, `queue/`) mee retiren in deze reeks? | Ja (P20). Anders schrijft `vnx promote` naar een map zonder lezer. |
| O-6 | `pretooluse_worker_scope_enforce.py`: herbedraden op de headless lane of weg? | Herbedraden onderzoeken. `WORKER_PERMISSIONS.md` zegt dat hij bedraad is, de code niet. |
| O-7 | Kimi interactief: valt de REPL onder de generieke tak (skill via send-keys, paste, `/clear`)? | Meet het met één echte kimi-pane vóór P07 de generieke tak als contract vastlegt. |

**Metingen**

| # | Meting | Nodig voor |
|---|---|---|
| M-1 | Draait `vnx start` in een ander project dan `vnx-dev`? (B zocht diepte 6 en vond de nieuwste supervisor-log op 2026-07-01.) | P17, P19 |
| M-2 | Trace of de SessionStart-hook `tmux list-panes -a` draait (`build_t0_state.py:445-449`). | P15 |
| M-3 | Wordt het Next.js-dashboard geopend en gebruikt de operator start, stop en attach? Kijk in de toegangslog van `serve_dashboard.py`. | § 8 |
| M-4 | Faalt de T2/T3-pre-flight in `dispatch_create.sh:253` in de praktijk (A, open punt)? | wordt overbodig met P18 |
| M-5 | Draaien de vier (b)-shelltests solo met exit 0, en welke vragen echte `tmux`? | P07 |
| M-6 | Welke lezers van `terminal_state.json` leunen na de ACK-monitor alleen op de reconciler? | P14 |
| M-7 | Draait `receipt_query.py pull` in de T0-cyclus? De repo bevat de stap alleen in `DISPATCH_RULES.md`. | P03 |

## 10. Uncertainty & Gaps

1. **Uncertain: regelaantallen van testaanpassingen.** Productiecode heb ik aan bestandsgroottes en functiegrenzen gemeten. Voor testbestanden (`test_orphan_sweep.py` 1372 regels, `test_rp_delivery_gates.sh` 439) is het aantal te wijzigen regels een schatting. Elke plak markeert dat.
2. **Uncertain: plakgrootte van P05, P16 en P19.** Verplaatsingen tellen in een diff dubbel (P05 is +230 en -200). P16 kan boven 300 uitkomen en krijgt daarom een splitsregel.
3. **Uncertain: of de codex-tak van `switch_terminal_model` en van `_activate_non_claude_mode` (`/plan`) ooit tegen een echte codex-pane is gemeten.** De tests pinnen de toetsaanslagen, niet het effect.
4. **Uncertain: kimi interactief** (O-7). Hij heeft nergens een eigen tak.
5. **Uncertain: gebruik buiten `vnx-dev`.** B mat één store. Andere projecten met een eigen `vnx start` sluit ik niet uit (M-1).
6. **Uncertain: wie de twee geparkeerde daemons start bij het uit parkeren.** De supervisor was hun enige automatische starter. Na P17 blijven `vnx restart` en de dashboard-restarttabel over. Een launchd-sjabloon zoals `com.vnx.nightly-intelligence-pipeline.plist` bestaat voor hen niet.
7. **Conflicting evidence: `WORKER_PERMISSIONS.md:93,133,207` beschrijft de worker-scope-hook als bedraad, de code niet** (B §4.3). Ik volg de code.
8. **Conflicting evidence: de datum van de lane-verwijdering.** `CLAUDE.md` zegt 2026-09-18, commit 496e0fe5 is van 2026-09-19 (B §7).
9. **Gap: ik voerde geen van de plakken uit.** De voorwaarde-checks zijn commando's die de uitvoerende worker draait. Ik draaide alleen de drie (b)-testbestanden solo, die alle drie de meting in paragraaf 1.3 opleverden.
10. **Gap: de inventarissen zelf.** A en B corrigeerden allebei de opdracht (106 en 71 bestanden in plaats van 99 en 78). Ik nam hun 177 als geheel en controleerde de dekking, maar telde de scope niet opnieuw.

## 11. Deliverables

Eén per plak. T0 zet ze voor. De titels volgen de commitstijl van de repo (Engels, conventional commits). Plakken met een voorwaarde staan erbij.

```
--output-kind pr --title "refactor(daemons): derive the daemon register from an explicit table with parked and retired states, not from start_all() (P01)"
--output-kind pr --title "refactor(pause-resume): vnx pause and vnx resume require only the receipt processor (P02)"
--output-kind pr --title "refactor(receipts): retire the T0 pane-paste push and VNX_RECEIPT_T0_PUSH (P03, after M-7)"
--output-kind pr --title "refactor(receipts): decouple the receipt processor from pane_manager and drop the flood paste (P04)"
--output-kind pr --title "refactor(tmux): move the provider-aware pane injection into scripts/lib/pane_injection.sh (P05)"
--output-kind pr --title "refactor(tmux): move the model switch into pane_injection.sh (P06)"
--output-kind pr --title "test(tmux): un-exclude the adapter and start-runtime tests, run the shell suites in CI, pin the injection contract (P07)"
--output-kind pr --title "chore(cleanup): remove five scripts with no caller (P08)"
--output-kind pr --title "refactor(hooks): remove the tmux signal hooks and their dead tails (P09)"
--output-kind pr --title "refactor(receipts): remove the readers of the dead tmux signal directory (P10, after P09)"
--output-kind pr --title "refactor(pool): drop the tmux reap from the pool and restore the direct-coupling freeze test (P11)"
--output-kind pr --title "refactor(permissions): remove the permission relay for the removed claude tmux lane (P12)"
--output-kind pr --title "refactor(queue): remove the queue popup layer and smart_tap (P13)"
--output-kind pr --title "refactor(daemons): remove the ACK monitor and the state manager, keep pane activity as a helper (P14)"
--output-kind pr --title "refactor(state): remove the tmux terminal probes and the process-gone tmux report (P15, after M-2)"
--output-kind pr --title "refactor(orphan-sweep): drop the tmux session kinds from the sweep and remove cleanup_stale_vnx_sessions (P16)"
--output-kind pr --title "refactor(daemons): remove the dispatcher, its supervisors and the tick host (P17)"
--output-kind pr --title "refactor(dispatcher): remove the dispatcher-private libraries (P18)"
--output-kind pr --title "refactor(start): remove vnx start, stop, jump and the tmux session profile (P19, after O-2)"
--output-kind pr --title "refactor(queue): retire the legacy .md promote flow and the T0 role paragraph (P20, after O-5)"
--output-kind pr --title "docs(tmux): document the retired daemons and state pane injection as a capability (P21)"
--output-kind pr --title "refactor(naming): rename the tmux_interactive lane label (P22, optional)"
--output-kind pr --title "refactor(naming): rename the claude-tmux lock class, VNX_TMUX_MAX_CONCURRENT and tmux_worktree (P23, optional)"
```

## 12. Sources

Primaire bronnen (code en meting):

1. Inventaris A: `git show origin/dispatch/20260924-tmux-inventaris-a-lib:claudedocs/2026-09-24-tmux-inventaris-A-scripts-lib.md` (227 regels).
2. Inventaris B: `git show origin/dispatch/20260924-tmux-inventaris-b-rest-daemons:claudedocs/2026-09-24-tmux-inventaris-B-rest-en-daemons.md` (536 regels).
3. Code op main 11df8ff8: `scripts/lib/dispatch_deliver.sh`, `dispatch_create.sh`, `input_mode_guard.sh`, `model_routing.sh`, `tmux_adapter.py`, `vnx_start_runtime.py`, `worker_pane_classifier.py`, `worker_permission_relay.py`, `orphan_sweep.py`, `daemon_register.py`, `beacon_register.py`, `dispatch_sidedoor_audit.py`, `scripts/dispatcher_minimal.sh`, `heartbeat_ack_monitor.py`, `pane_manager.sh`, `receipt_processor.sh`, `scripts/lib/receipt_processor/rp_delivery.sh`, `rp_dedup.sh`, `scripts/commands/resume.sh`, `pause.sh`, `bin/vnx`.
4. Eigen metingen op 2026-09-24: `python -m pytest` op de drie uitgesloten testbestanden (6 gefaald, 115 geslaagd); een scan van alle 30 functies in `dispatcher_minimal.sh` tegen `scripts/lib/*.sh` (15 gebruikt door libs); AST-functiegrenzen voor `orphan_sweep`, `pool_manager`, `vnx_recovery_phases`, `terminal_state_reconciler`, `terminal_snapshot`, `worker_heartbeat`, `vnx_doctor_checks` en `heartbeat_ack_monitor`; een grep naar consumerbedrading in `~/Development/*/.claude/settings.json`; een dekkingscontrole van alle 177 modules.
5. `scripts/ci/test_exclusions.txt`, `.github/workflows/vnx-ci.yml`, `scripts/local-ci.sh`, `Makefile`, `tests/conftest.py` (geen shell-runner).

Secundaire bronnen:

6. `docs/governance/decisions/ADR-025-raw-file-dispatch-deprecation.md` (redenering 3: de daemon tmux-send-keys is een zijdeur) en `ADR-035-receipt-v2.md` (§5.3, §8, PR-8).
7. `docs/core/DISPATCH_RULES.md:218-222` (receipt-pull als vervanger van de push).
8. Memory `tmux-is-een-injectiepad-geen-legacy` (operatorbesluit 2026-09-24).

## VNX Report
- research_question: welke plakken, in welke volgorde, halen de tmux-erfenis weg terwijl tmux als injectiepad naar interactieve niet-claude CLI's blijft bestaan
- scope: 177 modules uit inventaris A en B, main 11df8ff8, plus de 9 daemons; alleen een plan
- depth: deep
- source_count: 8
- uncertainty_flags: 10
- quality_self_assessment: alle 177 modules hebben een (b)-oordeel met dekkingscontrole en elke plak noemt een uitvoerbare voorwaarde-check; productieregels zijn gemeten en testregels geschat, en of het (b)-pad tegen een echte kimi-, codex- of gemini-pane werkt is niet gemeten.
- open_items: ["O-1 waar (b) landt", "O-2 lot van vnx start", "O-3 rotatiehooks", "O-4 lease_sweep en runtime_supervise", "O-5 legacy md-stroom", "O-6 worker-scope-hook", "O-7 kimi interactief", "M-1 tot M-7"]
