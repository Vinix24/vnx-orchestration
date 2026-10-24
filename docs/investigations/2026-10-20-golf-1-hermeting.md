# Golf 1: hermeting van inventaris A, B en deel C, met het manifest van cluster naar plak

Dispatch-ID: 20261020-g1-0-hermeting
**Model**: opus
**Provider**: claude

Track `retire-redundant-architecture`, golf 1 (opruimen). Read-only op code. Dit bestand en de CSV ernaast zijn de enige wijzigingen.

**Gemeten op:** kop van `main` `c8c55d23` (2026-10-20, "feat(voortgang): a VNX mod that shows VNX progress per project"). Deel C mat op `11df8ff8` (2026-09-24). Daartussen liggen 206 commits.

**Bronnen:** `claudedocs/2026-09-24-tmux-inventaris-A-scripts-lib.md` (A), `claudedocs/2026-09-24-tmux-inventaris-B-rest-en-daemons.md` (B), `claudedocs/2026-09-24-tmux-opruimplan.md` (deel C), het plan `golf-1-opruimen` revisie 2 van 17-10.

**Regel per pad of functie:** `docs/investigations/2026-10-20-golf-1-hermeting.csv`, 150 regels. Elke regel draagt de vier commando's die ik draaide, letterlijk, uit te voeren vanuit de repo-root.

## 1. Samenvatting

Alles wat deel C noemt bestaat nog. Geen plak is vervallen; één bewerking is overbodig geworden (`vnx_cli/commands/pool.py` bevat geen tmux meer).

De verwachting houdt voor de helft. P09, P11 en P12 blijven grotendeels golf 1. P05, P06, P10, P13, P15 en P16 vallen naar golf 2, omdat ze bij meting een grens raken die het plan niet zag: de pane-daemon zelf (P05, P06, P13), `append_receipt_internals/enrichment.py` (P10), het `terminals`-blok in `t0_state.json` (P15) en `crash_recovery_sweep` (P16). Uit P15 en P16 splits ik twee stukken af die wel binnen blijven.

| Indeling | Regels in de CSV |
|---|---|
| golf 1 | 39 |
| golf 2 | 98 |
| twijfel | 12 |
| vervallen | 1 |

De twijfelregels vallen in twee klassen. Vincent beslist per klasse (paragraaf 6).

Het manifest (paragraaf 7) heeft acht clusters: C1, C2, C3, C4a tot C4d en C5. Samen circa 4.700 verwijderde regels, geen cluster boven 1.500. Twee clusters vragen een migratiestap buiten deze repo: C1 in mission-control en C4 in vier projectstores.

## 2. Nulmeting van 17-10, opnieuw

Dezelfde vier commando's uit het plan. Ik controleerde eerst dat ze op `9f3f1e2c` de getallen van 17-10 reproduceren; dat doen ze alle vier.

```bash
git ls-files 'scripts/*.py' 'scripts/**/*.py' | wc -l
git ls-files 'scripts/*.py' 'scripts/**/*.py' | xargs cat | wc -l
git grep -l -i tmux -- scripts | wc -l
git grep -l -E '(^|[^A-Za-z0-9_])T[123]([^0-9A-Za-z_]|$)' -- scripts | wc -l
```

| Meting | 17-10 op `9f3f1e2c` | 20-10 op `c8c55d23` | Verschil |
|---|---|---|---|
| Python-bestanden onder `scripts/` | 822 | 836 | +14 |
| Regels in die bestanden | 327.602 | 336.909 | +9.307 |
| Bestanden onder `scripts/` met tmux | 163 | 162 | -1 |
| Bestanden onder `scripts/` met T1, T2 of T3 | 131 | 131 | 0 |

Op `9f3f1e2c` reproduceerde ik de regeltelling per bestand met `git show`, omdat die commit niet uitgecheckt is.

## 3. Werkwijze

Per regel vier kanten. Het commando staat in de CSV-kolom `kantN_cmd`, de uitkomst in `kantN_uitkomst`.

1. **Statisch in de repo:** `git grep -n -E` met een woordgrens als POSIX-klasse (`(^|[^A-Za-z0-9_])naam([^A-Za-z0-9_]|$)`), zonder `\b` of `\s`. Dat dekt `import`, `from ... import`, `importlib`-strings, `-m`, paden in `subprocess`, shell, yaml, json en plist-sjablonen. `claudedocs/` sluit ik uit, want daar staan de drie bronbestanden zelf. De uitkomst splitst code, tests en docs.
2. **Runtime op deze machine:** `grep` over `~/Library/LaunchAgents/com.vnx.*.plist` (41 agents) en de hook-commando's uit `~/.claude/settings.json` en `~/.claude-salesminds/settings.json` via `jq`. Alleen commando's, geen env-waarden.
3. **Geïnstalleerde vloot:** `grep -r` over `.claude/`, `scripts/` en `bin/` van de drie consumers, een `find -type l` naar symlinks met dezelfde naam, en de aanwezigheid in `~/.vnx-system/current` (v1.7.2) en `~/.vnx-system/versions/v1.7.1`.
4. **Alternatieve kiezers:** welke `VNX_`-variabelen het pad leest, en voor de echte kiezers een settercontrole in code, shellprofielen en de tmux-omgeving (alleen aanwezigheid).

Drie bevindingen over de meetbasis zelf:

- **sales-copilot staat niet op het opgegeven pad.** `ls -d ~/Desktop/BUSINESS/development/sales-copilot` geeft "No such file or directory". De LaunchAgent `com.vnx.cleanup-reviewed-worktrees.sales-copilot` noemt `--repo-root ~/Development/sales-copilot`. Ik mat daar. Nul regels staan daardoor op "niet gemeten".
- **mission-control draait v1.7.1.** Zijn `.claude/vnx-system` wijst naar `~/.vnx-system/versions/v1.7.1`, de andere twee naar `current` (v1.7.2). Kant 3 meet daarom beide versies.
- **Een inhoudsgrep ziet geen symlinks.** mission-control heeft dertien symlinks onder `scripts/` naar de install; `scripts/dispatch_broker_cli.py` staat in git met mode 120000. Vier wijzen naar een pad uit een plak: `dispatch_broker_cli.py`, `dispatcher_v8_minimal.sh`, `runtime_coordination_init.py` en `pr_queue_manager.py`. Kant 3 zoekt daarom ook op symlinknaam.

Aanvullend gemeten, buiten de vier kanten van de opdracht: de crontab (0 treffers op de golf-1-namen over 2 actieve regels) en een vierde consument, `~/Development/pa-engine` (v1.7.2, hooks `build_t0_state_hook.sh` en `pretooluse_block_raw_claude_spawn.sh`, geen golf-1-pad).

**Kant 4, vlootbreed.** Van de oude-padvlaggen is er op deze machine één gezet: `VNX_TMUX_MAX_CONCURRENT` (in `~/.zshenv` en in de tmux-omgeving, raakt alleen P23). `VNX_CONTEXT_ROTATION_ENABLED` staat in de settings van SEOcrawler_v2 en sales-copilot (O-3, geen plak). `VNX_TMUX_SIGNAL_DIR`, `VNX_DISPATCH_ID`, `VNX_RECEIPT_T0_PUSH`, `VNX_QUEUE_POPUP_ENABLED`, `VNX_ACK_DIRECT_NOTIFY`, `VNX_ADAPTER_T0` tot `T3`, `VNX_SUPERVISOR_MODE`, `VNX_DISPATCH_LEGACY`, `VNX_SINGLE_ENTRY_DISPATCH`, `VNX_TMUX_ADAPTER_ENABLED` en `VNX_POOL_TASK_CONSUMER` staan nergens: niet in `~/.zshrc`, `~/.zprofile`, `~/.zshenv`, niet als sleutel in een LaunchAgent of in de settings van beide accounts en de consumers, niet in de tmux-omgeving.

**De (b)-toets.** De negen (b)-modules uit deel C §2.1 bestaan nog. Geen live injectiepad op main gebruikt ze. Wat vandaag via tmux in een interactieve sessie typt, doet dat met eigen tmux-aanroepen: `scripts/t0_rotate_spawn.sh`, `scripts/lib/t0_rotation_state.py`, `hooks/vnx_rotate.sh` en de plugin `packages/claude-code-toolkit/context-rotation/lib/tmux_clear.py`. Geen van die vier staat in een plak. Geen golf-1-cluster raakt (b)-code. `pane_manager_v2.sh` in C1 is een shim die naar `pane_manager.sh` doorverwijst; `pane_manager.sh` zelf blijft.

**Ruimte en opruiming.** Mijn meetscript stond in `/tmp/g1-hermeting-20261020/`, buiten de repo, en is na afloop verwijderd. Ik heb geen test gedraaid, geen hook uitgevoerd en niets onder `.vnx-data/` of de projectstores geschreven.

## 4. De metingen M-1 tot M-7

| Meting | Status | Uitkomst |
|---|---|---|
| M-1 `vnx start` elders | gedraaid | Geen proces van dispatcher, supervisor, smart_tap, popup, ACK-monitor of state manager (`ps -axo pid=,command=`). 0 van 9 tmux-sessies heeft prefix `vnx-` (`tmux ls`). Laatste supervisorlog: mission-control 2026-07-01; laatste dispatcherlog: vnx-dev 2026-06-27. Niet nodig voor golf 1; bevestigt P17 tot P19. |
| M-2 tmux-probe in SessionStart | niet gedraaid | Een trace vraagt de hook te draaien, en die schrijft `t0_state.json` in de store. Statisch staat het pad er nog: `build_t0_state.py:634-635` roept `build_terminal_snapshot` met de standaard `allow_tmux_probe=True` (`canonical_state_views.py:222`). Alleen nodig voor P15, dat golf 2 is. |
| M-3 dashboardgebruik | niet gedraaid | Hoort bij deel C §8, geen plak. Wel gemeten: `serve_dashboard.py` draait als LaunchAgent `com.vnx.dashboard` uit `~/.vnx-system/current`. |
| M-4 T2/T3-pre-flight | overbodig | P18 is golf 2. |
| M-5 shelltests van (b) | niet gedraaid | Alleen nodig als P07 in golf 1 valt (twijfelklasse B), en deze dispatch draait geen tests. |
| M-6 lezers van `terminal_state.json` | gedraaid | `feature_state_machine.py:286-296` leest `lease_state` uit het `terminals`-blok van `t0_state.json`; `commands/dispatch.sh:79` en `commands/status.sh:104` lezen `lease_state`; `terminal_snapshot.py:54` en `userpromptsubmit_worker_intelligence_inject.sh:23` lezen `terminal_state.json`. P14 is dus golf 2. |
| M-7 receipt-pull in de T0-cyclus | niet gedraaid | Alleen nodig voor P03, dat golf 2 is. |

## 5. Indeling per plak

Afwijkingen van de verwachting staan vet.

| Plak | Indeling | Reden |
|---|---|---|
| P01 | golf 2 | Nieuw gedrag: het register wordt een tabel met de nieuwe toestand `parked`. Leest `start_all()` uit de `vnx start`-keten. Geen golf-1-plak hangt ervan af. |
| P02 | golf 2 | `resume.sh` start en eist `dispatcher_minimal.sh` (pane-daemon) en `queue_popup_watcher.sh` (`:90-106`). De enige afhankelijke plak, P13, is golf 2. |
| P03 | golf 2 | `rp_delivery.sh` wordt gesourced door `receipt_processor.sh`, de schrijver van het grootboek. Geen golf-1-plak hangt ervan af. |
| P04 | golf 2, met een afsplitsing | Bewerkt `receipt_processor.sh`, de launchd-daemon die `t0_receipts.ndjson` schrijft. Afgesplitst: de shim `scripts/pane_manager_v2.sh` (niet in deel C, geen aanroeper) is golf 1. |
| P05 | **golf 2** | Bewerkt `dispatcher_minimal.sh` zelf, een pane-daemon, en `dispatch_deliver.sh` en `dispatch_create.sh`, die alleen die daemon sourcet (`:144`, `:145`). Het doel, P17 en P18 voorbereiden, ligt in golf 2. |
| P06 | **golf 2** | Zelfde reden, hangt aan P05. |
| P07 | **twijfel (klasse B)** | Alleen tests, maar zonder P05 en P06 verliest de plak zijn doel en zijn stappen 5 en 6. De shutdown-fix raakt de test of `tmux_adapter.py`, dat `runtime_core.load_runtime_core` bij elke lease-operatie laadt. De uitsluitregels staan nu op `:209`, `:210` en `:215`, niet op 206, 207 en 212. |
| P08 | **gesplitst** | Golf 1: `dispatch_broker_cli.py`, `vulture_whitelist.py` en de tekstbewerkingen in de benchmark-runners. Twijfel klasse A: `update_pane_mapping.sh` (schrijft `panes.json` en herstart smart_tap) en `reconcile_terminal_state.py` (schrijft met `--repair` de projectie van `terminal_leases`). **Golf 2:** `runtime_coordination_init.py`, die `terminal_leases` leest (`:111`) en DDL toepast (`:244`); `tracks.py:253` gaat mee. |
| P09 | golf 1, op één bestand na | De twee signaalhooks, de staarten en de guards vuren nergens (kant 4). **Twijfel klasse A:** `tmux_signal_stop_receipt.sh`, die achter zijn guard `t0_receipts.ndjson` schrijft (`:75-121`). |
| P10 | **golf 2** | `toolcall_signals.py` wordt gelezen door `enrichment.py:274-306`, een expliciet genoemd grensbestand, en door de receipt-schrijvers `envelope_govern.py:388-406` en `provider_dispatch.py:1529-1544`. |
| P11 | golf 1 | De tmux-reap in de pool. `pool_reaper.py` wordt een inkorting, geen verwijdering: `identify_reap_targets` en twee andere functies blijven. `reap_dead` blijft de lease vrijgeven via `release_pool_lease` (`:308`). `pool.py` vervalt als bewerking. |
| P12 | golf 1 met migratiestap, op één bestand na | De permissie-relay van de verwijderde claude-lane. **Golf 2:** `worker_pane_classifier.py`, want `orphan_sweep.py:147` importeert hem op moduleniveau. Migratie: 39 open escalaties in vier stores. |
| P13 | **golf 2** | smart_tap is een pane-daemon (leest het T0-scherm). Verwijderen vraagt een bewerking van `start.sh`, de supervisor en `start_all()`. De shim `smart_tap_v7_json_translator.sh` is twijfel klasse A. |
| P14 | golf 2 | De ACK-monitor is een pane-daemon, en M-6 vindt lezers die op `lease_state` leunen. De shim `unified_state_manager_v2.py` is twijfel klasse A. |
| P15 | **golf 2**, met een afsplitsing | `canonical_state_views.build_terminal_snapshot` voedt het `terminals`-blok (`build_t0_state.py:634-668`); `terminal_snapshot.get_terminal_state_from_tmux` is de terugval in `enrichment.py:155`. Afgesplitst en golf 1: `worker_heartbeat.build_process_gone_failure_report`, die alleen een test aanroept. |
| P16 | **golf 2**, met een afsplitsing | De bewerking zit in `orphan_sweep.sweep()`, die soort 3 aan `crash_recovery_sweep` geeft (`:717`, `:979`). Soort 1 stuurt bovendien de worktree-beslissing van soort 2 (`surviving_ids`, `:790-915`). Afgesplitst en golf 1: `scripts/cleanup_stale_vnx_sessions.sh`. |
| P17 | golf 2 | Pane-daemons en de supervisor. De shim `dispatcher_v8_minimal.sh` is twijfel klasse A. |
| P18 | golf 2 | `dispatch_lifecycle.sh` neemt de lease (`rc_acquire_lease`, `:152`, `:417`) en verplaatst naar `active/` (`:595`). `dispatch_deliver.sh:540` is het terugvalluik. |
| P19 | golf 2 | `vnx start`, besluit 08-10, O-2. |
| P20 | golf 2 | De legacy `.md`-stroom, ADR-025. De T0-regel staat nu op `role-orchestrator.md:222`. |
| P21 | per cluster | In golf 1: de hooklijst in `00_VNX_ARCHITECTURE.md`, de alinea in `DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` §5 en `panel.py:89` (C2), de relay-delen van `WORKER_PERMISSIONS.md` (C4d), `METHODOLOGY.md` (C1). De rest gaat over golf-2-paden. `CLAUDE.md`, `DISPATCH_RULES.md`, de T0-rol en de fabric-reference-skill noemen geen golf-1-pad. |
| P22 | golf 2 | Hernoemen is geen opruimwerk. |
| P23 | golf 2 | Hernoemen; `VNX_TMUX_MAX_CONCURRENT` is op deze machine gezet, dus een nieuwe naam breekt een bestaande instelling. |

Gemeten op verzoek en niet in golf 1 ingedeeld: `scripts/lib/headless_dispatch_daemon.py` (schrijft `active/`, leest `lease_state` op `:252-253`), `scripts/headless_orchestrator.py`, en de luiken `VNX_DISPATCH_LEGACY` en `VNX_SINGLE_ENTRY_DISPATCH`. Alle vier golf 2.

Nieuw sinds deel C en meegenomen: vier compat-shims met de opmerking "remove in 1.0.1" (`pane_manager_v2.sh`, `smart_tap_v7_json_translator.sh`, `unified_state_manager_v2.py`, `dispatcher_v8_minimal.sh`). Geen ervan heeft een aanroeper.

## 6. Twijfelregels

Vincent beslist per klasse. Elke regel staat met `indeling=twijfel` in de CSV.

### Klasse A: dode code die zelf een grensobject zou raken

Geen van deze paden heeft een aanroeper in repo, runtime of vloot, of de guard ervoor vuurt nooit (kant 4). Als ze zouden draaien, raakten ze een grensobject.

| Pad | Wat het zou raken |
|---|---|
| `scripts/update_pane_mapping.sh` | schrijft `panes.json` voor T0 tot T3 en herstart smart_tap (`:116-123`) |
| `tests/test_process_control_safety_sweep.sh` (`:23`) | volgt `update_pane_mapping.sh` |
| `scripts/reconcile_terminal_state.py` | `--repair` schrijft `terminal_state.json` met `lease_expires_at` |
| `scripts/hooks/tmux_signal_stop_receipt.sh` | schrijft `t0_receipts.ndjson` via `convert_report_to_receipt` (`:75-121`) |
| `scripts/smart_tap_v7_json_translator.sh` | exec van de pane-daemon smart_tap |
| `scripts/unified_state_manager_v2.py` | start de `vnx start`-daemon state_manager |
| `scripts/dispatcher_v8_minimal.sh` | exec van de pane-daemon; mission-control trackt er een symlink naar |

**Vraag:** geldt "niets wat leest of schrijft" ook voor code die nooit draait?

**Mijn advies: golf 1.** De grenzen beschermen gedrag dat golf 2 na het audit-spoor herbouwt. Een pad zonder aanroeper heeft geen gedrag om te beschermen, en weghalen verandert geen schrijf- of leespad dat vandaag bestaat. Bij golf 1 schuiven ze naar C1 en C2 (paragraaf 7); bij golf 2 gaan ze met hun doel mee.

### Klasse B: P07, alleen tests

| Pad | Wat |
|---|---|
| `scripts/ci/test_exclusions.txt` `:209`, `:215` | uitsluiting van `test_tmux_adapter.py` en `test_vnx_start_runtime.py` |
| `tests/test_tmux_adapter.py::TestAdapterCliResolveCommand` | drie tests op de niet-bestaande module `tmux_adapter_cli` |
| `tests/test_vnx_start_runtime.py::TestVNXVars` | verwachte set mist `VNX_SOCKETS_DIR` |
| `scripts/lib/tmux_adapter.py::shutdown` | de parameter heet `_graceful` (`:416`); alleen tests roepen `shutdown(graceful=...)` aan |
| `tests/test_tmux_adapter_interface.py` (`:210`) | de freeze-test slaagt na C3, maar het bestand blijft uitgesloten door de shutdown-fout |

**Vraag:** P07 stap 1 tot 4 als voorbereiding in golf 1, of samen met P05 en P06 in golf 2?

**Mijn advies: golf 2.** P07 beschermt (b) voordat P17 en P18 eraan komen, en die zijn golf 2. Lost Vincent de shutdown-fout in golf 1 op, dan alleen in de test en niet in `tmux_adapter.py`. In golf 1 zou het een cluster van circa 45 regels vóór C3 worden.

## 7. Manifest van cluster naar plak

Volgorde volgens deel C §5: P08, P09, P07 en P11, P12, dan de afsplitsingen van P15 en P16. Clusters mogen parallel gebouwd worden en mergen na elkaar. Elk cluster draagt de clusterpunten 1 tot 7 uit het plan; hieronder staat per cluster wat specifiek is.

```
C1 ──┬── C2
     ├── C3
     ├── C4a ── C4b ── [migratie escalaties] ── C4c ── C4d
     └── C5
```

C1 gaat eerst, want het bouwt `scripts/ci/check_retired_paths.py` met de lijst in `scripts/ci/retired_paths.d/`. Elk cluster voegt daar zijn eigen fragment toe (één `*.txt` per cluster, genoemd naar de dispatch); de check leest alle fragmenten in sorteervolgorde als één lijst en weigert een regel die in twee fragmenten staat. Clusters kunnen dus in elke volgorde mergen. Geen cluster raakt de test-uitsluitlijst behalve C5 (één regel).

### C1: P08 (golf-1-deel) en de shim uit P04

| | |
|---|---|
| Plakken | P08 golf 1; `pane_manager_v2.sh` (uit P04) |
| Weg | `scripts/dispatch_broker_cli.py` (368), `scripts/vulture_whitelist.py` (19), `scripts/pane_manager_v2.sh` (3) |
| Bewerkt | `scripts/lib/governance_enforcer.py:837` (argument `scripts/vulture_whitelist.py` uit het vulture-commando); `scripts/benchmark/field-tests/METHODOLOGY.md:145-147`; `runners/skill_smoke.py:8`; `runners/run_field_tests.py:137-150` (dode `scoring_worktree`-tak: `lane_adapter.py:404` zet hem op `None` en niets wijst hem toe) |
| Aanroepers die de verwijdering raakt | `governance_enforcer.py:837`. Vloot: mission-control `scripts/dispatch_broker_cli.py` (getrackte symlink) en `scripts/bootstrap-mission-control-repo.sh:100` |
| Hangt af van | niets |
| Tests weg | geen. Geen regel in `test_exclusions.txt`, `serial_tests.txt` of de `-k`-quarantaine |
| Regels weg | circa 410. Met klasse A in golf 1 erbij: plus `update_pane_mapping.sh` (124), `reconcile_terminal_state.py` (47), `test_process_control_safety_sweep.sh:23` (1), drie shims (12) en `tests/test_unified_state_manager_shim.py` (68, 2 tests): circa 660 |
| Migratiestap | In mission-control de symlink `scripts/dispatch_broker_cli.py` en `bootstrap-mission-control-repo.sh:100` weghalen, vóór of met de release. Met klasse A ook `scripts/dispatcher_v8_minimal.sh` en `:97`. |

Toets van P08 (golf-1-deel) per grens:

- **Schema:** geen DDL. `runtime_coordination_init.py` zit niet in C1.
- **Grootboek:** niets leest of schrijft `active/`, `crash_recovery_sweep`, het `terminals`-blok of `enrichment.py`. `dispatch_broker_cli.py` schrijft via `DispatchBroker` de tabel `dispatches` en bundels onder `dispatches/<id>/`, niet `active/`. Rekent Vincent die tabel tot het grootboek, dan schuift deze regel naar klasse A.
- **Terminalbinding:** geen `terminal_leases` of `lease_state`, geen pane-daemon, geen T1 tot T3, geen `vnx start`.
- **Luik:** geen `VNX_DISPATCH_LEGACY` of `VNX_SINGLE_ENTRY_DISPATCH`.
- **`subprocess_dispatch`:** niet geraakt.
- **Nieuw gedrag:** geen. `vulture_whitelist.py` heeft 0 uitvoerbare regels, dus de vulture-uitkomst blijft gelijk.

### C2: P09 (golf-1-deel) en tekst uit P21

| | |
|---|---|
| Plakken | P09 golf 1; P21 voor deze paden |
| Weg | `scripts/hooks/tmux_signal_prompt_received.sh` (89), `scripts/hooks/tmux_signal_session_ready.sh` (51) |
| Bewerkt | staart `pretooluse_block_raw_claude_spawn.sh:74-86`; staart `pretooluse_block_subagent.sh:71-85`; guard `session_reconcile_autoclose.sh:65-68`; guard `session_reconcile_cleanup.sh:26-31`; `.claude/settings.json` de regels op `:87` en `:139`; `docs/core/00_VNX_ARCHITECTURE.md:621-623` (regenereren met `scripts/generate_architecture_doc.py --write`); `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md:90-96`; `scripts/panel.py:89` |
| Aanroepers die de verwijdering raakt | `.claude/settings.json:87` (SessionStart) en `:139` (UserPromptSubmit). De twee pretooluse-hooks blijven; ze staan in `.claude/settings.json:20,30` en, via `~/.vnx-system/current`, in de settings van mission-control, sales-copilot en pa-engine. Daar is de staart even dood. |
| Hangt af van | C1 (de check) |
| Tests weg | `tests/test_tmux_lane_signals.py::TestHookGuards`, 15 tests: `test_session_ready_noop_when_env_unset`, `test_session_ready_writes_sentinel_when_guarded`, `test_session_ready_does_not_write_when_only_one_var_set`, `test_prompt_received_writes_sentinel_when_guarded`, `test_prompt_received_noop_when_env_unset`, `test_prompt_received_writes_mismatch_sentinel_when_dispatch_id_disagrees`, `test_prompt_received_no_mismatch_sentinel_when_dispatch_id_agrees`, `test_prompt_received_no_mismatch_sentinel_when_prompt_has_no_dispatch_id`, `test_prompt_received_ignores_foreign_id_quoted_earlier_in_body`, `test_prompt_received_detects_real_mismatch_despite_foreign_id_in_body`, `test_prompt_received_no_mismatch_when_protocol_id_agrees_with_env`, `test_session_ready_captures_session_id_from_stdin`, `test_session_ready_empty_stdin_still_writes_sentinel`, `test_session_ready_garbage_stdin_still_writes_sentinel`, `test_session_ready_no_jq_still_writes_sentinel`. En `tests/test_pretooluse_block_subagent.py::TestToolcallSignalAggregation::test_deny_is_recorded_as_blocked` en `::test_allow_is_recorded_as_not_blocked`. Geen CI-lijstregel. |
| Tests herschreven | `tests/test_architecture_doc_drift.py::test_hook_rows_include_the_real_userpromptsubmit_hook`: de assert op `:111` eist de verwijderde hook; na C2 is `t0_context_guard.py` de UserPromptSubmit-hook |
| Regels weg | circa 535. Met klasse A in golf 1 erbij: plus `tmux_signal_stop_receipt.sh` (123), de settingsregel `:170` (9) en de rest van het testbestand met `TestStopReceiptGuarantee` (3 tests, 141): circa 810 |
| Let op | `.claude/settings.json` met de hand wijzigen, niet met `regen-settings --merge` (dat vervangt de hooks door de template). De permissie-classifier kan de wijziging blokkeren. `env:VNX_TMUX_SIGNAL_DIR` kan nog geen regel in het eigen fragment onder `scripts/ci/retired_paths.d/` worden: `enrichment.py:295`, `envelope_govern.py:396` en `provider_dispatch.py:1534` lezen hem (golf 2). |

Toets van P09 (golf-1-deel) per grens: geen schema. De twee hooks schrijven alleen sentinels in `VNX_TMUX_SIGNAL_DIR`; de hook die het grootboek schrijft zit alleen bij klasse A in C2. Geen terminalbinding, geen luik, geen `subprocess_dispatch`. Nieuw gedrag: nee, want de guards laten altijd door en de hooks en staarten stoppen bij hun guard (kant 4: `VNX_TMUX_SIGNAL_DIR` en `VNX_DISPATCH_ID` nergens gezet). `VNX_DISPATCH_ID` zelf blijft: `git_target_guard.py:56` en `hooks/git/pre-push:11` lezen hem.

### C3: P11

| | |
|---|---|
| Plakken | P11 |
| Bewerkt | `scripts/lib/pool_reaper.py:24-152` (`_sanitize_session_name`, `list_tmux_sessions`, `session_has_living_children`, `identify_orphan_tmux_sessions`, `sweep_orphan_tmux_sessions`); `scripts/lib/pool_manager.py`: import op `:43`, de blokken in `reap_dead` op `:281-286` en `:326-335`, `_kill_tmux_session` (`:365-376`), `_is_dispatch_terminal_in_db` (`:378-394`), `_sweep_orphan_tmux` (`:396-400`) |
| Aanroepers die de verwijdering raakt | `pool_manager.py:43`, `:398`. `reap_dead` draait alleen via `vnx pool reap --force` (`vnx_cli/commands/pool.py:191`) of `PoolManager.tick`, dat geen aanroeper buiten tests heeft |
| Hangt af van | C1. Deel C zette P07 vóór P11; dat is niet nodig, de freeze-test blijft uitgesloten tot klasse B beslist is |
| Tests weg | `tests/test_pool_reaper.py`: `test_orphan_sweep_kills_session_without_children_terminal_dispatch`, `test_orphan_sweep_preserves_session_with_living_children`, `test_orphan_sweep_preserves_session_non_terminal_dispatch`, `test_orphan_sweep_skips_non_vnx_sessions`, `test_orphan_sweep_empty_prefix_skip`, `test_orphan_sweep_multiple_sessions_mixed`. `tests/test_pool_manager_integration.py::test_reap_dead_kills_tmux_session`. Geen CI-lijstregel. |
| Tests herschreven | `tests/pool/test_pool_consumer.py::TestPoolManagerConsumerIntegration::test_tick_with_6_dispatches_spawns_runners` en `::test_tick_flag_off_unchanged_with_dispatches` (patch op `:261`, `:298`); `tests/test_pool_tick_reap_decide_execute.py::test_kill_subprocess_pid_validation_in_reap_flow` (patch op `:429`) |
| Regels weg | circa 320 |

Toets van P11 per grens: geen schema. `_is_dispatch_terminal_in_db` leest de tabel `dispatches`, niet `active/`. Terminalbinding: `reap_dead` schrijft `terminal_leases` via `release_pool_lease` (`:308`); die regel en zijn `try`-blok blijven byte-gelijk, en de reviewer toetst dat de diff in `reap_dead` alleen de twee tmux-blokken raakt. `subprocess_dispatch` start in `_spawn_via_provider_dispatch` (`:79-157`), dat de plak niet raakt. Geen luik. Nieuw gedrag: nee, beide blokken zijn fail-soft en staan los van de lease-vrijgave.

### C4: P12 in vier delen

P12 is samen circa 2.980 regels en wordt gesplitst langs de subcommando's van `vnx permission`. Elk deel haalt een subcommando volledig weg, dus geen tussentoestand verandert van gedrag.

**Migratiestap vóór C4c.** Open escalaties per store, gemeten met
`for d in ~/.vnx-data/*/state/permission_escalations; do echo "$d $(cat $d/*.json | jq -s 'map(select((.status // "pending")=="pending"))|length')"; done`:
seocrawler-v2 21, mission-control 16, sales-copilot 1, pacompany-engine 1, vnx-dev 0. In vnx-dev is op 2026-10-07 nog een `resolve-stale` gedaan. Eerst per store `vnx permission resolve-stale`, dan C4c en C4d. Anders verdwijnen open records uit `t0_state.json` zonder dat iemand ze heeft afgehandeld.

| | C4a: `scan` | C4b: `window-*` en de tick-lus | C4c: de t0_state-sectie | C4d: `approve`, `deny`, `escalations`, `resolve-stale` en de store |
|---|---|---|---|---|
| Weg of bewerkt | relay `_list_dispatch_sessions` (`:758-792`), `_capture_pane_or_error` (`:795-811`), `_dispatch_worktree_path` (`:831-837`), `scan_live_awaiting_permission` (`:840-1014`), de imports op `:49` en `:926`; CLI `:132-208` en de parser-regels van `scan` | relay `is_catastrophic` (`:168-183`), `PermissionWindow` (`:245-330`), `_strip_box` en `parse_pending_command` (`:336-417`), `decide` (`:423-435`), de fingerprint-functies (`:579-609`), `_capture_pane` (`:615-620`), `relay_tick` (`:638-703`), `run_relay_loop` (`:709-729`), `RelayHandle` (`:733-737`); CLI `:78-110` en parser | `build_t0_state.py` docstring `:38-45`, `_build_permission_escalations` (`:916-992`), `:2673`, `:2705`, `:2806` | de rest van `worker_permission_relay.py`, heel `permission_relay_cli.py` en `tmux_command_runner.py`; `bin/vnx:2635-2636` en de usage op `:345`; `check_no_file_derived_data_paths.py:244-246`; `docs/operations/WORKER_PERMISSIONS.md` (relay-delen) |
| Aanroepers die de verwijdering raakt | `permission_relay_cli.py:195` | `permission_relay_cli.py:79`, `:93`, `:100` | `build_t0_state.py:2673` | `bin/vnx:2636`, `build_t0_state.py:954` (weg na C4c), `worker_permission_relay.py:48`, `:755` |
| Hangt af van | C1 | C4a (`parse_pending_command` deelt de scan) | C4b en de migratiestap | C4c |
| Tests weg | `TestScanLiveAwaitingPermission` (7), `TestCmdScanExitCodes` (4), `TestScanWorktreeRisk` (6), `TestScanHumanOutputRiskLabel` (2) met helpers `FakeScanRunner`, `_ScanArgs`, `_make_git_repo` | `TestPermissionWindow` (5), `TestIsCatastrophic` (2), `TestParsePendingCommand` (10), `TestDecide` (5), `TestRelayTick` (6), `TestAutoApproveGatesOnSendResult` (2) | `tests/test_build_t0_state.py::TestBuildPermissionEscalations` (4) | `TestEscalationLifecycle` (3), `TestResolveStaleEscalation` (4), `TestSafeDispatchId` (6), `TestManualApproveSurfacesSendFailure` (7), `TestResolveStaleCli` (4); daarmee gaat `tests/test_worker_permission_relay.py` heel weg |
| Tests bewerkt | geen | geen | geen | `tests/test_runtime_adapter_certification.py:308`, `:340` (naam uit beide sets, geen test weg); `tests/test_tmux_adapter_interface.py:250-261` (uitgesloten bestand) |
| CI-lijsten | geen | geen | geen | geen |
| Regels weg | circa 790 | circa 680 | circa 170 | circa 1.340 |

Toets van P12 (golf-1-deel) per grens: geen schema; `t0_state.json` is geen centrale DB en `permission_escalations` heeft geen JSON-schema en geen lezer buiten `build_t0_state.py` en zijn tests. Grootboek: het `terminals`-blok bouwt `_build_terminals` (`:634-668`), dat C4c niet raakt. Terminalbinding: de relay leest `state/tmux_interactive/<id>.json` van de verwijderde lane, geen `terminal_leases`; geen pane-daemon, want `run_relay_loop` heeft geen starter. Geen luik, geen `subprocess_dispatch`. Nieuw gedrag: nee, alleen subcommando's verdwijnen. `worker_pane_classifier.py` blijft tot het orphan_sweep-deel van P16 in golf 2.

### C5: de afsplitsingen van P15 en P16

| | |
|---|---|
| Plakken | P15 (één functie), P16 (één bestand) |
| Weg | `scripts/lib/worker_heartbeat.py::build_process_gone_failure_report` (`:338-395`); `scripts/cleanup_stale_vnx_sessions.sh` (114) |
| Aanroepers die de verwijdering raakt | geen buiten tests. De LaunchAgent `com.vnx.tmux-cleanup` draait `~/.local/bin/tmux-cleanup`, een eigen script buiten de repo dat dit bestand niet noemt |
| Hangt af van | C1. In §5 staan P15 en P16 achteraan, maar deze twee stukken hangen niet aan P14, M-2 of P12 (H14 geldt voor `orphan_sweep`). C5 mag dus ook direct na C1 |
| Tests weg | `tests/test_model_canonicity.py::TestHeartbeatReportNoUnknownDefault::test_process_gone_report_default_model_is_empty`; `tests/test_cleanup_stale_vnx_sessions.py` (10 tests: `test_script_exists`, `test_script_is_executable`, `test_script_syntax`, `test_no_kill_without_explicit_confirm`, `test_skip_message_on_default_no`, `test_fresh_session_not_listed_as_stale`, `test_non_vnx_session_not_listed`, `test_no_vnx_sessions_exits_clean`, `test_tmux_not_found_exits_clean`, `test_invalid_timestamp_skipped`) en zijn regel `scripts/ci/test_exclusions.txt:119` |
| Regels weg | circa 440 |

Toets per grens: geen schema, geen grootboek, geen terminalbinding (de tmux-sessies `vnx-*` horen bij de verwijderde lane), geen luik, geen `subprocess_dispatch`, geen nieuw gedrag. `worker_heartbeat.py` zelf blijft voor `kimi_spawn.py`.

### Totaal

| Cluster | Plakken | Regels weg (circa) |
|---|---|---|
| C1 | P08 deels, shim uit P04 | 410 (660 met klasse A) |
| C2 | P09 deels, P21 deels | 535 (810 met klasse A) |
| C3 | P11 | 320 |
| C4a | P12 | 790 |
| C4b | P12 | 680 |
| C4c | P12 | 170 |
| C4d | P12, P21 deels | 1.340 |
| C5 | P15 deels, P16 deels | 440 |
| | | **circa 4.685** (circa 5.235 met klasse A) |

Productiecode heb ik gemeten aan functie- en bestandsgrenzen. Docs- en testregels zijn waar het om delen van een bestand gaat een schatting op klassegrenzen.

## 8. Wat ik niet heb vastgesteld

1. **Uncertain:** of mission-control nog een eigen aanroeper van `scripts/dispatch_broker_cli.py` heeft buiten `.claude/`, `scripts/` en `bin/`. Een `git grep` over de hele mission-control-repo vond alleen `docs/archive/CHAIN-1-PR-PACK.md:501`, de bootstrapregel en een docstring in `ci_script_caller_check.py:18`.
2. **Uncertain:** het gebruik van de tmux-routes van het dashboard (M-3). Geen plak in golf 1 hangt eraan.
3. **Gap:** M-2 en M-5 heb ik niet gedraaid, omdat ze een hook of tests vragen. Beide zijn alleen nodig voor golf 2 of klasse B.
4. **Gap:** de regelschattingen voor docs (`WORKER_PERMISSIONS.md`, de alinea in `DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md`) zijn schattingen. De productiecode niet.
5. **Uncertain:** of de permissie-classifier een handwijziging van `.claude/settings.json` in C2 tegenhoudt. Deel C waarschuwde ervoor; ik heb het niet geprobeerd.

## 9. Besluiten van Vincent, 22-10

Op 22-10 koos Vincent op alle vijf de vragen van §6 en §7 voor het geadviseerde antwoord.

**Klasse A: golf 1.** De zeven paden van §6 klasse A gaan naar golf 1. `update_pane_mapping.sh`, `test_process_control_safety_sweep.sh:23`, `reconcile_terminal_state.py` en de shims `smart_tap_v7_json_translator.sh` en `unified_state_manager_v2.py` gaan naar C1. `tmux_signal_stop_receipt.sh` gaat met zijn settingsregel en `TestStopReceiptGuarantee` naar C2. `dispatcher_v8_minimal.sh` gaat naar C1, met de migratiestap in mission-control. De "met klasse A"-getallen van §7 gelden: C1 circa 660, C2 circa 810, samen circa 5.235 regels. De grensvraag over de tabel `dispatches` (§7, C1, Grootboek) verandert niets meer: `dispatch_broker_cli.py` is hoe dan ook golf 1.

**Klasse B: golf 2.** P07 gaat samen met P05 en P06 naar golf 2. De freeze-test blijft tot dan uitgesloten; C3 hangt niet van P07 af.

**Manifest: akkoord**, zoals beschreven in §7. Vóór C4c krijgen de vier stores met open escalaties (seocrawler-v2 21, mission-control 16, sales-copilot 1, pacompany-engine 1, gemeten op 20-10) eerst een backup, dan per store `vnx permission resolve-stale`, daarna C4c en C4d.

**ADR-042: aangenomen.** Het harness-contract is nu `docs/governance/decisions/ADR-042-harness-contract.md`. Het `terminals`-blok in `t0_state.json` wordt in golf 2 vervangen door `live_work`, niet zonder vervanging weggehaald. Zijn lezers (`feature_state_machine.py:286-296`, `append_receipt_internals/enrichment.py:166`, `commands/dispatch.sh:79`, `commands/status.sh:104`) gaan daar mee naar `live_work`.

**Model per cluster:** C1 op opus, omdat het de CI-check `scripts/ci/check_retired_paths.py` bouwt die beslist wat als bewijs telt. C2, C3, C4a tot C4d en C5 op DeepSeek. De clusters worden na C1 parallel gebouwd en één voor één gemerged.

### Natelling van 22-10

De T0 draaide de meetcommando's van elke CSV-regel met `indeling` "golf 1" en zonder aanroepers (12 regels) opnieuw, op origin/main `6cadd5a6`, 28 commits na de merge-base `c8c55d23` van deze PR. Uitslag: 12 van de 12 kloppen nog, plus 4 van de 4 voor de regels die alleen "pytest (CI)" als aanroeper hebben. De regelnummers in de kolom `functie` houden stand. Eén gemeten bestand is sindsdien veranderd: `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` op regel 128 (#2164), buiten de gemeten alinea `:90-96`. `current` wijst nu naar v1.7.5 waar de CSV v1.7.2 zegt; de daar gemeten bestanden bestaan nog.
