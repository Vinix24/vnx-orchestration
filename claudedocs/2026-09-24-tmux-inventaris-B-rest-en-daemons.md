# Tmux-inventaris deel B: de rest van de scope en de 9 daemons

Track `retire-redundant-architecture`, deliverable `dlv-dc316f60611b`. Rol research-analyst, diepte deep.
Dispatch `20260924-tmux-inventaris-b-rest-daemons`. Gemeten op `main` 11df8ff8, 2026-09-24.
Read-only onderzoek: geen code, test of config gewijzigd. Dit bestand is het enige dat is geschreven.

Bronverwijzingen staan inline als `bestand:regel`. Wat een afleiding is staat als "Inferred:". Wat ik niet kon vaststellen staat als "Uncertain:". Waar twee bronnen elkaar tegenspreken staat "Conflicting evidence:".

## 1. Executive Summary

1. **71 bestanden, niet 78.** De scope-definitie geeft 177 bestanden in totaal. Buiten `scripts/lib/` zijn dat er 71 en binnen `scripts/lib/` 106. De totaalcijfers van de opdracht kloppen, de splitsing niet (paragraaf 2).
2. **Klassen van de 71:** LEVEND 44, ACHTER VLAG 3, ALLEEN TESTS 14, DOOD 7, ONBEPAALD 3.
3. **LEVEND betekent bijna overal "op code-niveau".** Twaalf van de 44 hangen aan de `vnx start`-keten (start, stop, jump, resume, supervisor, dispatcher, smart_tap, ACK-monitor, popup-scripts, `log_dispatch_metadata.py`). Die keten wordt op deze machine niet gebruikt: geen `vnx-*` tmux-sessie (0 van 16 sessies), geen supervisor-proces, laatste logregel van alle daemons 2026-06-27 16:13. De tmux-lane van de deur (`claude_tmux_subscription`) draaide wel door tot 2026-09-09 (164 dispatches), maar die liep niet via deze daemons.
4. **290 van de 523 matchende regels (55%)** staan in bestanden die dood zijn, alleen door tests bereikt worden, achter een vlag zitten of alleen in de ongebruikte `vnx start`-keten hangen.
5. **De enige tmux-code die aantoonbaar draait** is `pane_manager.sh` in de launchd-`receipt_processor`. De daemon-log toont `Some panes are not healthy, attempting setup...` bij zijn start van 2026-09-22 17:10:51 (`receipt_processor.sh:633-636`). Daarnaast leidt de SessionStart-hook af tot een `tmux list-panes -a` (Inferred, paragraaf 4.1).
6. **De 9 daemons:** 2 draaien (`receipt_processor`, `dashboard`, allebei via launchd), 7 niet. Oordeel: NODIG 2, WEG 5 (`dispatcher`, `smart_tap`, `heartbeat_ack_monitor`, `queue_watcher`, `state_manager`), VERVANGEN en GEPARKEERD 2 (`intelligence_daemon`, `recommendations_engine`).
7. **`daemon_liveness` staat permanent op `fail`.** Alle 9 zijn "verwacht", 7 zijn afwezig. Dat trekt `system_health.status` naar `degraded` en `producer_liveness` naar `fail` (gemeten in `t0_state.json`). De verwachte lijst hoort nog uit 2 daemons te bestaan, plus 2 als `parked`.
8. **Verborgen afhankelijkheden** die een retire stuk maakt als ze niet eerst worden losgekoppeld: `vnx resume` faalt gesloten zonder dispatcher, `receipt_processor.sh` hangt aan `pane_manager.sh`, `lease_sweep.py` en `runtime_supervise.py` hebben de dispatcher als enige host, en `start_all()` is de bron van het daemon-register (paragraaf 6).
9. **Twee latente bijwerkingen.** `queue_popup_watcher.sh` telt en raakt bij een herstart 4351 `.md`-bestanden in de bundelmappen aan (paragraaf 5.5). De dashboard-generator meldt nu al `supervisor: running` op het PID van deze dispatch, een onwaar getal (paragraaf 5.6).
10. **De tmux-hooks zijn inert.** `VNX_TMUX_SIGNAL_DIR` en `VNX_DISPATCH_ID` worden nergens meer gezet sinds 496e0fe5 (2026-09-19). Drie `tmux_signal_*`-hooks vuren op elke sessie en stoppen bij hun guard.

## 2. Background & Context

### 2.1 Scope en telling

Commando uit de opdracht, opnieuw gedraaid op 11df8ff8 in de worktree:

```
grep -rlI -iE "tmux|send-keys|tmux_adapter|dispatch_broker|dispatch_deliver|terminal[-_ ]pane|pane_id|pane_mapping" scripts bin hooks dashboard | grep -v "/tests\?/" | grep -vE "\.(bak|orig)(\.|$)|/node_modules/" | sort
```

| Meting | Aantal |
|---|---|
| Totaal | 177 (klopt met de opdracht) |
| Buiten `scripts/lib/` | 71 |
| Binnen `scripts/lib/` | 106 (95 in de map zelf, 11 in submappen) |
| Matchende regels in de 71 | 523 |

Conflicting evidence: de opdracht noemt 78 bestanden buiten `scripts/lib/` en 99 daarbinnen. Ik kom op 71 en 106. Het verschil is 7 bestanden. Inferred: het zijn de zeven shell-modules die `dispatcher_minimal.sh` sourcet (`scripts/lib/dispatch_create.sh`, `dispatch_deliver.sh`, `dispatch_lifecycle.sh`, `dispatch_metadata.sh`, `input_mode_guard.sh`, `model_routing.sh`, `provider_routing.sh`). Dat past op precies zeven van de acht `.sh`-bestanden direct in `scripts/lib/`, maar ik kan het niet bewijzen. Ik heb alle 71 behandeld. Deel A dekt `scripts/lib/`, dus die zeven blijven daar. Waar zij mijn ketens raken citeer ik ze.

### 2.2 Werkwijze

- De entrypoint-kaart, alle daemon-analyse en alle machinemetingen (`ps`, `launchctl`, `tmux`, de centrale store, het dispatch-register, `t0_state.json`, de receipt-log) zijn van mijzelf.
- De classificatie van 66 bestanden is verdeeld over vier read-only subagents (16, 17, 19 en 14 bestanden). De vijf daemon-scripts heb ik zelf gedaan.
- Ik heb hun oordelen die een besluit dragen tegen de code getoetst: `pane_manager` in `receipt_processor.sh`, de afwezige setters van `VNX_TMUX_SIGNAL_DIR`, de presets, de dashboard-starter, de DOOD-claims en de inhoud van `hooks/lib/`. Ze bleken te kloppen. Eén correctie op mijn eigen brief: `hooks/lib/` bevat alleen `_vnx_hook_common.sh` (`git ls-files hooks/lib`), geen hookkopieën.
- Niets is uitgevoerd uit de repo. Alleen `grep`, `sed`, `git log`, `ps`, `launchctl list`, `tmux ls`, `tmux list-keys` en read-only opens van de store.

### 2.3 Entrypoints die ik heb gebruikt

| Id | Entrypoint | Bewijs |
|---|---|---|
| E1 | `bin/vnx` en de loader | `_load_command` sourcet `scripts/commands/<cmd>.sh` voor elke subcommand-naam (`bin/vnx:254-273`, aanroep `:2393`). Case-tabel `:2399-2616`. |
| E2 | `vnx_cli/main.py` (pip-script `vnx`) | `pyproject.toml:53-54`. Subcommando's `doctor`, `init`, `dispatch`, `pool`, `migrate`, `dream` en meer. |
| E3 | launchd-sjablonen | `scripts/launchd/*.plist`: 12 stuks. |
| E4 | Hooks | `.claude/settings.json`, `templates/settings_vnx_keys.json.tmpl`, `hooks/sessionstart.sh`. |
| E5 | De deur | `scripts/lib/dispatch_cli.py` en zijn lanes. |
| E6 | `vnx start` en `vnx resume` | `scripts/commands/start.sh:309,437`, `resume.sh:44-106`. |
| E7 | CI | `.github/workflows/vnx-ci.yml`, `subsystems-drift.yml`, `Makefile`, `scripts/ci/`. |
| Gevonden | E8 per-daemon start | `vnx ps/cleanup/restart` via `MANAGED_PROCESSES` (`scripts/vnx_process_ux.py:42-53`, `cmd_restart` `:430-500`). Ook `POST /api/restart-process` in `dashboard/serve_dashboard.py:720-770`. |
| Gevonden | E9 consumer-hooks | `~/Development/SEOcrawler_v2/.claude/settings.json:94` en circa 15 worktrees wijzen naar `hooks/vnx_handover_detector.sh`. |
| Gevonden | E10 host-launchd buiten de repo | `~/Library/LaunchAgents/com.vnx.dashboard.plist` draait `serve_dashboard.py` uit `~/.vnx-system/current` (v1.6.2). |

### 2.4 Klassen en de extra as

De vijf klassen zijn die van de opdracht. Ik voeg per rij toe wat ik op deze machine gemeten heb, omdat "bereikbaar" en "gebruikt" hier ver uit elkaar liggen:

- `draait`: proces of taak op 2026-09-24 gemeten actief, of draait in CI.
- `vuurt, inert`: de hook vuurt, de tmux-tak stopt bij zijn guard.
- `niet gebruikt`: bereikbaar in code, maar de entrypoint wordt hier niet gebruikt (gemeten afwezig).
- `handmatig`: operator-CLI zonder automatische aanroeper.

Een operator-CLI die T0 of de operator rechtstreeks draait (`pr_merge.py`, `panel.py`) telt als eigen entrypoint, met het runtime-bewijs erbij.

### 2.5 De toestand van deze machine op 2026-09-24

| Meting | Uitkomst | Bron |
|---|---|---|
| tmux-sessies | 16, geen enkele met prefix `vnx-` (het patroon van `start.sh:56`) | `tmux ls` |
| Supervisor, dispatcher, smart_tap, ACK-monitor, queue_watcher, state_manager, intelligence, recommendations | geen proces | `ps aux` |
| Draaiend | `receipt_processor.sh` PID 82744 onder `receipt_processor_supervisor.sh` PID 82602, en `generate_valid_dashboard.sh` PID 12593 | `ps aux` |
| Laatste schrijfactie daemon-logs (vnx-dev store) | 2026-06-27 16:12 tot 16:13 | `~/.vnx-data/vnx-dev/logs/` |
| `panes.json`, `t0_conversation.log`, `session_profile.json` | 2026-06-27 | `state/` |
| `.md` in `pending/`, `queue/`, `active/` | 0, 0, 0 | `dispatches/` |
| Bundelmappen in `pending/` (de deur) | 3179 | `dispatches/pending/` |
| `.md` in `staging/`, nieuwste | 14, 2026-08-02 | `dispatches/staging/` |
| `dispatch_promoted`-events in het register | 0 van 3362 | `state/dispatch_register.ndjson` |
| `dispatch_created` per lane (deur, `extra.lane`) | aug: `provider` 191, `claude_tmux_subscription` 160, `claude_headless` 65, zonder lane 35. sep t/m 09-24: `claude_headless` 176, `provider` 75, `claude_tmux_subscription` 4. Laatste `claude_tmux_subscription`: 2026-09-09 18:54Z (164 in totaal, eerste 2026-08-11) | idem |
| Ticks van de unified supervisor | nooit gedraaid: geen `.last_*_ts` en geen `runtime_supervise.log`, `lease_sweep.log`, `oi_bridge.log`, `objective_reconcile.log`, `learning_cycle.log` | `state/`, `logs/` |
| `terminal_leases` | 3 rijen, alle `idle` | `runtime_coordination.db` (read-only, immutable) |

Uncertain: "niet gebruikt" is gemeten voor het `vnx-dev` project en een bredere zoektocht (diepte 6 over `~/.vnx-data`, `~/Development`, `~/Desktop/BUSINESS`, die de nieuwste supervisor-log op 2026-07-01 vond). Andere projecten met een eigen `vnx start` kan ik niet uitsluiten.

## 3. Key Findings: Deel 1, 71 bestanden buiten `scripts/lib/`

Kolommen: bestand, klasse (met gebruik), aantal matchende regels en hun aard, bewijs (keten of zoekopdracht), noot.

### 3.1 Commando's, popup-UI en pane-scripts (16)

| Bestand | Klasse | Tmux-regels | Bewijs | Noot |
|---|---|---|---|---|
| `bin/vnx` | LEVEND, draait | 3, tekst | Entrypoint. Case `start :2434`, `stop :2437`, `doctor :2440`, `resume :2506`, `recover :2509`, `dispatch :2551`, `jump :2575`. | Alleen usage-regels `:292,296,381`. Mode-gate `VNX_MODE_GATING_ENABLED` default 1 (`:852`). Mode in de store is `operator` (`mode.json`, 2026-08-02). Kill-lijst `vnx_kill_all_orchestration` `:927-960` noemt de tmux-daemons. |
| `scripts/commands/start.sh` | LEVEND, niet gebruikt | 73, actief | `bin/vnx:2434` naar `cmd_start` `:55`. Sessie `vnx-<basename>` `:387`. T0-start met `send-keys` `:599`. `pipe-pane` `:296,431`. Popup-binds `:561-564`. Supervisor `:309,437`. | `command -v tmux` is verplicht (`:220-223`). Geen vlag zet tmux uit. Laatste `vnx start` in de vnx-dev store: 2026-06-27. |
| `scripts/commands/stop.sh` | LEVEND, niet gebruikt | 4, 2 actief | `bin/vnx:2437`. `tmux has-session` `:16`, `kill-session` `:17`. | Rest is `vnx_kill_all_orchestration` (`:13`). |
| `scripts/commands/jump.sh` | LEVEND, niet gebruikt | 23, actief | `bin/vnx:2575` naar `cmd_jump` `:11-170`. | Enige aanroeper. T0-only layout (`start.sh:400-407`, `tracks: {}`) laat T1 tot T3 niet bestaan. |
| `scripts/commands/recover.sh` | LEVEND, draait mee | 2, tekst | `bin/vnx:2509`. | Tmux zit indirect in `vnx_recovery_phases.py` (fase `_phase_tmux_reconciliation`). Die slaat over zonder `session_profile.json`, en dat schrijft alleen `vnx start` (`start.sh:299,417`). `--aggressive` `:162`. |
| `scripts/commands/resume.sh` | LEVEND, niet gebruikt | 1, tekst | `bin/vnx:2506`. Dispatcher `:44-52`. Queue-watcher `:90-106`. | `_vnx_resume_verify_readiness` faalt gesloten zonder levende dispatcher (paragraaf 6). |
| `scripts/commands/dispatch.sh` | LEVEND, draait | 11, tekst en weigerpad | `bin/vnx:2551` naar `cmd_dispatch`. `ps` toont `bin/vnx dispatch 20260924-tmux-inventaris-b-rest-daemons`. | `:550-551` weigert `--adapter tmux`. `claude-tmux` leeft door als lock-class (`:201,218,278`, `dispatch_cli.py:3552`, `dispatch_plan.py:401`, `dispatch_serialization.py:4`, `config_registry.py:313-323`, `vnx_cli/commands/subsystems.py:98`). |
| `scripts/commands/doctor.sh` | LEVEND | 2, tekst | `bin/vnx:2440`. Delegatie naar `vnx_doctor.py` `:212-216`. | De tmux-regels `:260,270` zitten in een fallback die niet draait zolang `vnx_doctor.py` bestaat. |
| `scripts/commands/t0_role_audit.sh` | ALLEEN TESTS, handmatig | 10, waarvan actief `:345` en `:401` (`tmux list-panes -a`) | `tests/test_t0_role_audit_static.py:68` draait `--static`. Als operator-commando genoemd in `.claude/terminals/T0/role-orchestrator.md:21,116`. | Skip-pad `:361-364` verwijst naar `tmux_interactive_dispatch.py`, dat niet meer bestaat. |
| `hooks/vnx_rotate.sh` | ACHTER VLAG, alleen extern bedraad | 14, actief | Niet in `.claude/settings.json`, niet in `templates/settings_vnx_keys.json.tmpl`, niet in `cmd_bootstrap_hooks` (`bin/vnx:1909-1955`). Alleen consumer-settings (E9) naar `vnx_handover_detector.sh:74-82`. | Vlag `VNX_CONTEXT_ROTATION_ENABLED`: `vnx_rotate.sh:15` default 0, detector `:7` default 1, template `:8` staat op "1". Headless worktrees matchen niet op `*/T1` tot `*/T3` (`hooks/lib/_vnx_hook_common.sh:7-15`). Geen `vnx_rotate_T*.log` gevonden onder `~/.vnx-data`. Uncertain of hij ooit draaide. |
| `scripts/pane_config.sh` | ACHTER VLAG (via `vnx_rotate.sh`) | 8, lezen van `panes.json` | Supervisor sourcet hem (`vnx_supervisor_simple.sh:21`), maar `get_t0_pane` (`:23`) wordt nergens aangeroepen. Andere aanroeper: `hooks/vnx_rotate.sh:20,37`. | Een `source` zonder aanroep telt niet als aanroep. |
| `scripts/pane_manager.sh` | LEVEND, draait | 83, actief | `com.vnx.receipt-processor.plist:49` naar `receipt_processor_supervisor.sh` naar `receipt_processor.sh:43` (source) en `:633-636` (`check_pane_health`, `setup_pane_titles`). Ook `rp_dedup.sh:139-142`, `dispatcher_minimal.sh:153`, `smart_tap_json_translator.sh:49`. | Log: `Some panes are not healthy, attempting setup...` op 2026-09-22 17:10:51, de start van PID 82744. `setup_pane_titles` (`pane_manager.sh:260-276`) zet `select-pane -T` en `@vnx_label` op de gevonden panes. Inferred: dat kan een T0-pane van de operator hertitelen. |
| `scripts/update_pane_mapping.sh` | DOOD | 18 | `grep -rnI update_pane_mapping` geeft alleen `tests/test_process_control_safety_sweep.sh:23`. Die zet het pad in een lijst en zoekt met `rg` naar kill-patronen. Hij voert het script niet uit, het pad `.claude/vnx-system/scripts/` bestaat in deze layout niet en de test draait niet in CI. | Schrijft een vaste 2x2-`panes.json`, achterhaald t.o.v. de T0-only start. |
| `scripts/popup_editor.sh` | LEVEND, niet gebruikt | 1, tekst | Enige aanroeper `queue_ui_enhanced.sh:121-122` (`edit_in_pane :111`, toets `e :375`). | Valt weg met `queue_ui_enhanced.sh`. |
| `scripts/queue_popup_watcher.sh` | LEVEND, niet gebruikt | 25, actief | Supervisor `:212` (monitor `:346`), `resume.sh:90-95`, `vnx restart` (`vnx_process_ux.py:47`), `serve_dashboard.py:66`. | Paragraaf 5.5. |
| `scripts/queue_ui_enhanced.sh` | LEVEND, niet gebruikt | 1, tekst | `start.sh:472-484` (`@vnx_popup_cmd`) en binds `:561-564`. `queue_popup_watcher.sh:22,132`. | Menselijke poort voor `queue/`: `mv` naar pending `:342`, naar rejected `:356`. Restant gemeten: `tmux list-keys -T root` toont nog `C-g` naar de kopie in `SEOcrawler_v2/.claude/vnx-system/scripts/queue_ui_enhanced.sh`. |

### 3.2 Hooks, launchd en dashboard (17)

| Bestand | Klasse | Tmux-regels | Bewijs | Noot |
|---|---|---|---|---|
| `scripts/hooks/pretooluse_block_raw_claude_spawn.sh` | LEVEND, vuurt, tmux-staart dood | 3 | `.claude/settings.json:16-20` (PreToolUse, Bash) naar `pretooluse_spawn_detector.py:37,60`. Ook `templates/settings_vnx_keys.json.tmpl:64` en `templates/init/{default,minimal}/settings.json.j2`. | Het enige hookbestand in de vlootconfig. Staart `:74-87` schrijft naar `$VNX_TMUX_SIGNAL_DIR` (`:81`), nergens gezet. Inferred: de lezers `provider_dispatch.py:1094`, `envelope_govern.py:396`, `enrichment.py:285` krijgen daardoor altijd `None`. |
| `scripts/hooks/pretooluse_block_subagent.sh` | LEVEND, vuurt, alleen deze repo | 4 | `.claude/settings.json:26-30` (matcher `Task`) naar `pretooluse_subagent_guard.py:28,46`. Niet in de templates. | Zelfde dode staart (`:78`). Buiten tmux: de guard toetst `tool_name == "Task"` (`pretooluse_subagent_guard.py:187`). In deze sessie heet het tool `Agent` en vier subagents startten zonder blokkade. Uncertain wat de payload is. |
| `scripts/hooks/pretooluse_worker_scope_enforce.py` | ALLEEN TESTS | 5, tekst | Nergens geregistreerd: niet in `.claude/settings.json`, templates, `regen_settings.sh`, `regen_worker_permissions.sh`. Scan van `~/.claude/settings.json` en `~/Development/*/.claude/settings*.json`: 0 treffers. Tests `tests/test_pretooluse_worker_scope_enforce.py:44,102` (CI). | Vlag `VNX_ENFORCE_WORKER_PERMISSIONS` default 0 (`worker_permissions.py:372,406`, hook `:180`). Enige registratie stond in `tmux_interactive_dispatch.py`, verwijderd in 496e0fe5. Conflicting evidence: `docs/operations/WORKER_PERMISSIONS.md:93,133,207` beschrijft hem als bedraad. |
| `scripts/hooks/session_reconcile_autoclose.sh` | LEVEND, draait | 2, tekst | `.claude/settings.json:52-58` (SessionStart) naar `planning_cli.py:71`. `objective_reconcile.log` 1,1 MB, mtime 2026-09-23 20:40. | Guard `:66` op `VNX_DISPATCH_ID` is een relikwie die altijd doorlaat. Echte schakelaar `VNX_AUTO_CLOSE` default 1 (`:202`). |
| `scripts/hooks/session_reconcile_cleanup.sh` | LEVEND, draait | 1, tekst | `.claude/settings.json:36-42` (SessionEnd). | Guard `:27` dood, zelfde reden. |
| `scripts/hooks/session_stop_rotation.py` | ACHTER VLAG | 5 (4 docstring, 1 leest `VNX_TMUX_SIGNAL_DIR` `:66`) | `.claude/settings.json:135,156-160` (Stop). Lichaam achter `VNX_T0_ROTATION=1` (`:72`), roept `context_rotation.write_t0_handoff` (`:94-98`). | Geen setter in de repo. Een 0-byte `session_stop_rotation.err` van 2026-07-13 suggereert dat de vlag minstens eens aan stond. |
| `scripts/hooks/tmux_signal_prompt_received.sh` | LEVEND, vuurt, inert | 15 | `.claude/settings.json:123-129` (UserPromptSubmit). Guard `:44-46`: beide variabelen leeg leidt tot `exit 0`. | Schrijft `prompt_received` en `dispatch_id_mismatch` (`:74-86`). De enige lezer, `tmux_interactive_dispatch.py`, is weg. Test `tests/test_tmux_lane_signals.py` (CI). |
| `scripts/hooks/tmux_signal_session_ready.sh` | LEVEND, vuurt, inert | 10 | `.claude/settings.json:73-77` (SessionStart). Guard `:30-32`. | Leest stdin en draait `jq` vóór de guard (`:21-27`), dus bij elke SessionStart. |
| `scripts/hooks/tmux_signal_stop_receipt.sh` | LEVEND, vuurt, inert | 12 | `.claude/settings.json:146-150` (Stop). Guard `:30-32`. | Python-heredoc `:81-118` zet een rapport om in een receipt: het enige echte bijeffect, en het is gated. Levende zus: `stop_report_hook.sh` (`.claude/settings.json:141`). |
| `scripts/launchd/com.vnx.dashboard-generator.plist` | LEVEND, draait | 2, tekst (`:12,22`) | Handmatig geïnstalleerd (`scripts/launchd/reload_plist.sh`; het plist zegt dat `vnx init` het niet installeert). Geladen als `com.vnx.dashboard-generator.vnx-dev`, PID 12593. `ProgramArguments :59-64`. Gelezen als verwachte job door `build_t0_state._scan_launchd_dir`. | Bij retire gaat alleen het woord "tmux" uit twee commentaarregels. |
| `dashboard/api_agent_stream.py` | LEVEND (dashboard-server) | 2, tekst | `serve_dashboard.py:150`. Routes `:474-496`. Frontend `app/agent-stream/page.tsx:207,236`. | Leest de EventStore, geen tmux. |
| `dashboard/api_operator.py` | LEVEND (server, hostkopie) | 34, actief in `_jump_terminal :574-632` en `_list_tmux_sessions :704-768`, plus parse `:781-874` | `GET /api/operator/sessions` (`serve_dashboard.py:381-383`) vanaf `hooks.ts:280` en `sessions/page.tsx:75`. `POST /api/jump/*` (`serve_dashboard.py:608-626`) alleen vanaf `dashboard/index.html:1541`. Start, stop en attach via `scripts/lib/dashboard_actions.py:123-165,273,342`. | De draaiende server is de hostkopie v1.6.2. Inferred: `_jump_terminal` zoekt daar sessie `vnx-v1.6.2` (`:579`) en geeft altijd 503. Tests van deze handlers staan op `scripts/ci/test_exclusions.txt:126,192`. |
| `dashboard/serve_dashboard.py` | LEVEND, extern gestart | 2, tekst | Geen repo-entrypoint start hem (`dashboard/launch-dashboard.sh:11` is handmatig). Host: `~/Library/LaunchAgents/com.vnx.dashboard.plist` draait `~/.vnx-system/current/dashboard/serve_dashboard.py` (byte-identiek aan de repo), PID 88670, `127.0.0.1:4173`, `VNX_DATA_DIR=~/.vnx-data/seocrawler-v2`. | `POST /api/restart-process` (`:720-770`, tabel `:63-73`) start `smart_tap`, `dispatcher`, `queue_watcher`, `receipt_processor`, `supervisor` en `intelligence_daemon` op naam. Drie namen in die tabel hebben geen bestand. Dynamische daemon-starter. |
| `dashboard/token-dashboard/__tests__/session-control-buttons.test.tsx` | ALLEEN TESTS (zelf een test) | 2, mock-tekst | `vnx-ci.yml:610-634` (job `dashboard-lint`) draait `npm run test:lint` (`tsc --noEmit`). `npm test` (jest) staat nergens in `.github`, `scripts/ci` of `Makefile`. | Doel: `components/operator/project-card.tsx`, de Start/Stop/Attach-knoppen naar de tmux-routes. |
| `dashboard/token-dashboard/app/agent-stream/page.tsx` | ONBEPAALD | 2, tekst | Pagina in `sidebar.tsx:24`. Niets start de Next.js-app (poort 3100 dicht op meetmoment). | Zijn routes raken geen tmux. Meten: toegangslog van de dashboard-server. |
| `dashboard/token-dashboard/app/operator/sessions/page.tsx` | ONBEPAALD | 1, tekst | Idem. `useLiveSessions` (`hooks.ts:280-285`) naar `_list_tmux_sessions`, refresh 15 s. | Inhoudelijk volledig tmux-afgeleid. Gaat samen weg met route, `fetchLiveSessions` en sidebar-link. |
| `dashboard/token-dashboard/components/operator/agent-selector.tsx` | ONBEPAALD | 2, label | `sidebar.tsx:22`, `app/operator/reports/page.tsx:390`, `api_operator.py:1585-1586` (default `"tmux"`). | Alleen een adapterlabel en kleurmap. |

### 3.3 Benchmark en losse scripts, eerste helft (19)

| Bestand | Klasse | Tmux-regels | Bewijs | Noot |
|---|---|---|---|---|
| `scripts/benchmark/field-tests/METHODOLOGY.md` | DOOD (documentatie) | 1, tekst (`:147`) | `grep -rnI METHODOLOGY scripts tests vnx_cli bin hooks .github Makefile pyproject.toml`: alleen `scripts/lib/skill_prefix.py:173,213` met andere betekenis. | `:147` noemt een tmux-pad dat niet meer bestaat. |
| `scripts/benchmark/field-tests/lane_calibration.yaml` | ALLEEN TESTS | 6, datalabels `expected_lane: tmux_interactive` | Loader `runners/lane_calibration.py:65`. `tests/test_lane_calibration_field_test.py:26` (CI). Label komt uit `scripts/lib/providers/wave7_models.yaml:566-588`. | Een registry-naam van een verwijderde lane. Hernoemen raakt ook `tests/smart_router/test_tier_routing.py:69` en meer tests. |
| `scripts/benchmark/field-tests/runners/lane_adapter.py` | ALLEEN TESTS, handmatig | 2, tekst (`:13,330`) | Importeurs `run_field_tests.py:32`, `skill_smoke.py:44`. Tests `test_bench_equal_context.py:21`, `test_bench_worker_isolation.py:26`, `test_bench_headless_routing.py:18` (die laatste staat op `test_exclusions.txt:110`). | Geen tmux-aanroep meer. `HEADLESS_FORCED_MODELS = set()` (`:53`) maakt `_claude_subprocess_headless` (`:84-123`) in productie onbereikbaar. |
| `scripts/benchmark/field-tests/runners/run_field_tests.py` | ALLEEN TESTS, handmatig | 3, tekst | `tests/test_bench_retry_stale_report_prefix.py:29` (CI). Handmatig `README.md:16,21`, `monthly_runner.sh:19`. | `:140-150` (`scoring_worktree`-cleanup) is dode code sinds 496e0fe5. |
| `scripts/benchmark/field-tests/runners/scorer.py` | ALLEEN TESTS, handmatig | 2, tekst | `tests/test_bench_worker_isolation.py:32`. `dispatch_sidedoor_audit.py:126` noemt hem alleen in een allowlist. | Verwijderen vraagt ook `dispatch_sidedoor_audit.py:126-127` op te schonen. |
| `scripts/benchmark/field-tests/runners/skill_smoke.py` | DOOD (strikt), handmatige dev-smoke | 1, tekst (`:8`) | `grep -rnI skill_smoke tests` geeft niets. Genoemd in `README.md:29-30`. | Docstring-tabel is achterhaald. |
| `scripts/benchmark/field-tests/tasks.yaml` | ALLEEN TESTS, handmatig | 1, tekst (`:62`) | Lezers `run_field_tests.py:40`, `runners/lane_calibration.py:59`. `tests/test_lane_calibration_field_test.py:29`. | Comment "in the tmux lane" is historisch. |
| `scripts/build_t0_state.py` | LEVEND, draait | 1, tekst (`:900`) | `.claude/settings.json:68` (SessionStart) naar `scripts/hooks/build_t0_state_hook.sh:97`. | Zie paragraaf 4.1: `_build_terminals` (`:445-449`) leidt tot een tmux-probe. |
| `scripts/check_env_isolation.sh` | ALLEEN TESTS | 1, tekst (`:97`) | `tests/test_migrate_env_isolation.py:43`. `migrate_to_central_vnx.py:1773` noemt hem in een logtekst. | Handmatige diagnose. |
| `scripts/check_live_requires_measured_health.py` | LEVEND, draait in CI | 1, tekst (`:10`) | `.github/workflows/subsystems-drift.yml:26` naar `Makefile:13-14`. | Subsysteem `claude-tmux-serialization` is een echte rij (`docs/core/SUBSYSTEMS.md:32`, `vnx_cli/commands/subsystems.py:98,145`). `tests/test_live_requires_measured_health.py:55-75` pint de naam. |
| `scripts/check_no_file_derived_data_paths.py` | LEVEND, draait in CI en doctor | 3, allowlist-sleutels `scripts/lib/tmux_worktree.py` (`:177,271`) en een comment | `vnx-ci.yml:249`. `vnx_cli/main.py:1500-1501` naar `vnx_cli/commands/doctor.py:1070,1095,997`. | `tests/test_central_mode_path_gate.py:384-391` faalt als `tmux_worktree.py` verdwijnt terwijl de sleutels blijven. `tmux_worktree.py` zelf is levend (`dispatch_envelope.py:190`, `dispatch_worktree_isolation.py:1052`). |
| `scripts/ci/test_exclusions.txt` | LEVEND (data), draait in CI | 3, testpaden (`:206-208`) | Lezers `vnx-ci.yml:209`, `scripts/ci/check_test_exclusions.py:21` (via `scripts/local-ci.sh:66`), `tests/test_ci_check_test_exclusions.py`. | Sluit `test_tmux_adapter.py`, `test_tmux_adapter_interface.py` en `test_tmux_worktree.py` uit. Verwijderen van die tests vraagt deze regels mee, anders wordt `test_current_tree_is_green` rood. |
| `scripts/cleanup_reviewed_worktrees.py` | LEVEND, draait | 1, tekst (`:4`) | `vnx init` naar `init_cmd.py:1079,904,728`. `com.vnx.cleanup-reviewed-worktrees.plist:49`. Beide plists geladen (`launchctl list`), status 0. | Roept alleen git aan. Zijn producent van de lock-reden `vnx preserve` is `tmux_worktree.py:522`, die levend blijft. |
| `scripts/cleanup_stale_vnx_sessions.sh` | ALLEEN TESTS | 11, actief (`tmux list-sessions :31`, `kill-session :100`) | Enige verwijzingen: `tests/test_cleanup_stale_vnx_sessions.py:22` en een archiefplan. De test staat op `test_exclusions.txt:117`. | Puur handmatig gereedschap. Valt weg met tmux. |
| `scripts/codex_final_gate.py` | LEVEND | 1, padstring (`:40`) | `bin/vnx:2557` naar `roadmap_manager.py:488` naar `closure_verifier.py:2409,1872,1735,1558` naar `codex_final_gate.py:176,203`. | De regel is een prefix `scripts/lib/dispatch_broker` in `RUNTIME_PATH_MARKERS`. Zelfde marker in `auto_merge_policy.py:22`. |
| `scripts/dispatch_broker_cli.py` | DOOD | 9, usage-tekst en import (`:38`) | `grep -rnI dispatch_broker_cli`: 0 treffers buiten het bestand zelf. `git log -S"dispatch_broker_cli"`: alleen 195aed30. | Verwijderen breekt niets. `scripts/lib/dispatch_broker.py` bestaat nog (deel A). |
| `scripts/log_dispatch_metadata.py` | LEVEND (code, dispatcher niet gebruikt) | 1, tekst (`:88`) | `vnx start` naar supervisor `:198` naar `dispatcher_minimal.sh:143` naar `dispatch_lifecycle.sh:591,518`. Tests `test_pr4_role_capture_backfill.py:311`. | Loopt ook bij subprocess-levering (T1 tot T3), niet alleen bij tmux. Zonder draaiende dispatcher geen aanroep. |
| `scripts/migrate_dispatch_metadata_provider.py` | ALLEEN TESTS | 1, tekst (`:4`) | `tests/test_dispatch_metadata_provider_migration.py:33`. | `quality_db_init.py:975-990` draagt dezelfde migratie inline. |
| `scripts/migrate_to_central_vnx.py` | ALLEEN TESTS, eenmalige CLI | 1, tekst (`:1763`) | `vnx migrate` gaat niet hierheen (`bin/vnx:2605-2606` naar `vnx_cli.commands.migrate`). 16 testbestanden `tests/test_migrate_*`. | 2302 regels met dry-run-partner, geen productie-aanroeper. |

### 3.4 Losse scripts, tweede helft (14)

| Bestand | Klasse | Tmux-regels | Bewijs | Noot |
|---|---|---|---|---|
| `scripts/orphan_sweep.py` | ALLEEN TESTS | 4, tekst | `grep -rnIiE "orphan[-_ ]sweep"` buiten tests en docs: geen aanroeper. `tests/test_orphan_sweep.py:44-49` laadt hem (CI). | De `kill-session` zit in `scripts/lib/orphan_sweep.py:145`, levend via `worker_permission_relay.py:926`. |
| `scripts/panel.py` | LEVEND, handmatig, draait | 1, tekst (`:89`) | `.claude/skills/panel/SKILL.md:73-87`. 556 `panel-*.md` in `unified_reports`, nieuwste 2026-09-22 13:20. | Docstring noemt `vnx panel`. Dat subcommando bestaat niet. De tmux-regel is een verouderde comment. |
| `scripts/permission_relay_cli.py` | LEVEND, geen effect | 16, actief in `scan` en `approve` | `bin/vnx:2611-2612`. `scan :195` naar `worker_permission_relay.py:944,758,795`. `approve :269` naar `:623-636`. | Beide paden hangen aan `state/tmux_interactive/<id>.json`. Niemand schrijft die nog (30 handles, nieuwste 2026-08-26). `scan` slaat alles over, `approve` eindigt op `no_session`. |
| `scripts/pr_merge.py` | LEVEND, handmatig, draait | 1, tekst (`:205`) | Geen script roept hem aan. Runtime: 202 `pr_merged`-events met `source":"pr_merge"`, laatste PR #1904 op 2026-09-24. | Tmux-vrij. Retire raakt hem niet. |
| `scripts/quality_db_init.py` | LEVEND | 1, tekst (`:978`) | `bin/vnx:2402` (`init-db`), `vnx_init.py:519-535`, `nightly_intelligence_pipeline.sh:161`, `init_cmd.py:598`. | Tmux-neutraal. |
| `scripts/reconcile_terminal_state.py` | DOOD | 3, argparse-vlaggen | `grep -rnI "reconcile_terminal_state\.py"`: alleen `build_t0_state.py:6` ("replaces"). Tests importeren de lib, niet het script. | De lib erachter is levend (paragraaf 4.1). Dit CLI weghalen verwijdert de tmux-probe niet. |
| `scripts/rollback_runtime_core.py` | ALLEEN TESTS | 2, data | `tests/test_runtime_cutover.py:417,442,461`. Operator-hints `recover.sh:56,174`, `vnx_recovery_phases.py:808`. | Toont de vlag `VNX_TMUX_ADAPTER_ENABLED` (`scripts/lib/tmux_adapter.py:52`, default 1) alleen in `status`. |
| `scripts/runtime_coordination_init.py` | DOOD | 2, print-tekst | `grep -rnI "coordination_init"`: alleen comments en `tracks.py:213` (foutmelding). Geen test. | `tracks.py:213` noemt hem als handmatige remedie. Die melding moet mee. |
| `scripts/vnx_doctor.py` | LEVEND | 3, geen aanroep | `bin/vnx:2440` naar `doctor.sh:210-213`. `vnx_setup.py:185-200`. | `RECOMMENDED_TOOLS` (`:94`) geeft alleen een WARN "Optional not found: tmux". Het pip-`vnx doctor` (`vnx_cli/commands/doctor.py`) is een aparte implementatie met 0 tmux-regels. |
| `scripts/vnx_init.py` | LEVEND | 2, help-tekst | `bin/vnx:2399` naar `cmd_init :787` naar `:804-806`. `vnx_setup.py:128-140`. | Tekst "full tmux grid" (`:706,708`). |
| `scripts/vnx_install.py` | LEVEND | 2 | `bin/vnx:2599,2602`. `install.sh:61-63`. `vnx_setup.py:90-100`. | `RECOMMENDED_TOOLS` (`:112`) is een WARN. Regel `:547` is usage voor een vlag die niets meegeeft. |
| `scripts/vnx_process_ux.py` | LEVEND (`ps`, `cleanup`, `restart`) | 5, actief alleen in `cmd_status` (`:189-208`) | `bin/vnx:2566-2573` routeert `ps`, `cleanup`, `restart`. `bin/vnx:2560` (`status`) gaat naar `status.sh`, niet hierheen. | `cmd_status` is dood (handmatig en 4 tests). `restart` start de 10 daemon-scripts dynamisch (E8). |
| `scripts/vnx_setup.py` | LEVEND | 3, tekst | `bin/vnx:2596-2597`. | Alleen operator-mode tekst. |
| `scripts/vulture_whitelist.py` | DOOD (effectief) | 1, tekst (`:14`) | Enige consument `governance_enforcer.py:611-617`, alleen via de handmatige `check`. Geen vulture-stap in CI. `ast.parse`: 0 uitvoerbare statements. | Whitelist niets. Weghalen verandert de vulture-uitkomst niet. |

### 3.5 Daemon-scripts (5, uitgewerkt in deel 2)

| Bestand | Klasse | Tmux-regels | Kern |
|---|---|---|---|
| `scripts/vnx_supervisor_simple.sh` | LEVEND (code), niet gebruikt | 5 (`:24,25,30` pane-lookup, `:284-285` echo) | Starter van 8 van de 9 daemons. Alleen gestart door `start.sh:309,437`. |
| `scripts/dispatcher_minimal.sh` | LEVEND (code), niet gebruikt | 8, comment en `get_pane_ids` (`:648,653`) | Paragraaf 5.1. |
| `scripts/smart_tap_json_translator.sh` | LEVEND (code), niet gebruikt | 6, actief (`tmux capture-pane :584`, `list-panes :664`) | Paragraaf 5.2. |
| `scripts/heartbeat_ack_monitor.py` | LEVEND (code), niet gebruikt | 24, actief (`:493`, `:635-648`) | Paragraaf 5.4. |
| `scripts/receipt_processor.sh` | LEVEND, draait (launchd) | 1, comment (`:74`) | Paragraaf 5.3. Zijn tmux-koppeling zit in `pane_manager.sh`. |

### 3.6 Vlaggen

| Vlag | Default | Gelezen op | Schakelt | Gezet? |
|---|---|---|---|---|
| `VNX_TMUX_SIGNAL_DIR` | leeg | `pretooluse_block_raw_claude_spawn.sh:81`, `pretooluse_block_subagent.sh:78`, `tmux_signal_*.sh:30-32,44-46`, `session_stop_rotation.py:66` | signaal-staart en alle drie `tmux_signal_*`-hooks | Nee. `grep` op toewijzingen in `scripts hooks vnx_cli bin` levert 0 op. |
| `VNX_DISPATCH_ID` | leeg | dezelfde guards, `session_reconcile_autoclose.sh:66`, `session_reconcile_cleanup.sh:27` | guards | Nee. De headless lane zet `VNX_CURRENT_DISPATCH_ID` (`subprocess_adapter.py:401`), een andere naam. |
| `VNX_T0_ROTATION` | uit | `session_stop_rotation.py:72` | T0-handoff bij Stop | Geen setter in de repo. |
| `VNX_CONTEXT_ROTATION_ENABLED` | 0 in `vnx_rotate.sh:15`, 1 in `vnx_handover_detector.sh:7` | `hooks/lib/_vnx_hook_common.sh:85-87` | rotatie via tmux | Template `:8` zet "1". |
| `VNX_ENFORCE_WORKER_PERMISSIONS` | 0 | `worker_permissions.py:372,406`, hook `:180` | worker-scope hook | Niet bedraad. |
| `VNX_RECEIPT_T0_PUSH` | 0 | `rp_delivery.sh:118,189` | pane-paste van receipts | Plist zet hem niet. Gemeten: 67 keer `delivery_mode=suppressed` in de laatste 3 MB van `receipt_processing.log`, laatst 2026-09-24 09:33. |
| `VNX_QUEUE_POPUP_ENABLED` | 1 | `start.sh:197`, supervisor `:345`, `resume.sh:90`, `pr_queue_manager.py:1119` | popup-watcher of auto-accept | Drie presets zetten 0 (`bin/vnx:510,526,542`), het menu bij "n" (`:707`). |
| `VNX_ACK_DIRECT_NOTIFY` | 1 | `heartbeat_ack_monitor.py:599` | tmux-paste van ACK naar T0 | Nee. |
| `VNX_ADAPTER_T{n}` | T0 tmux, T1 tot T3 subprocess | `dispatch_deliver.sh:580-584`. Monitor `heartbeat_ack_monitor.py:221`. | leverkanaal | Nee. |
| `VNX_SUPERVISOR_MODE` | legacy | `dispatcher_supervisor_ticks.sh:27,79,138,181,206` | 5 ticks in de dispatcher | Nergens: repo, profielen, presets en shell-bestanden gegrept. |

Inferred: `VNX_ADAPTER_T{n}` heeft twee verschillende defaults. De dispatcher leidt T1 tot T3 af als subprocess (`dispatch_deliver.sh:583-585`), de ACK-monitor leest ontbrekend als `tmux` (`heartbeat_ack_monitor.py:221-222`). Een subprocess-dispatch zou de monitor dus toch tmux-panes laten volgen.

## 4. Key Findings: patronen over de 71 heen

### 4.1 Wat aan tmux daadwerkelijk draait

1. **`pane_manager.sh` in de launchd-`receipt_processor`.** Gemeten in de log. Bij elke start: `check_pane_health` faalt (T1 tot T3 bestaan niet), `setup_pane_titles` draait (`receipt_processor.sh:633-636`). In het flood-pad (queue groter dan 50) plakt `rp_dedup.sh:139-142` een melding in de T0-pane, zonder vlag.
2. **SessionStart naar tmux-probe. Inferred, niet gemeten.** `build_t0_state.py:445-449` roept `build_terminal_snapshot` aan met `allow_tmux_probe=True` (`canonical_state_views.py:216-222`). Is `terminal_state.json` ouder dan 180 s, dan draait de reconciler en daarmee `tmux list-panes -a` (`terminal_state_reconciler.py:167-179`). Het bestand is op het meetmoment 1 uur oud (mtime 09:33), en `panes.json` is van 2026-06-27. Uncertain: een `strace` of hooklog heb ik niet.
3. **Drie `tmux_signal_*`-hooks.** Vuren op elke SessionStart, prompt en Stop. Alleen `tmux_signal_session_ready.sh` doet werk (`jq`) vóór de guard.
4. **Server-restant.** `bind-key -T root C-g` wijst nog naar `SEOcrawler_v2` (uit een oude `vnx start`).

### 4.2 De `vnx start`-keten is als geheel ongebruikt

De keten `start.sh` naar supervisor naar 9 daemons plus popup-UI beslaat 12 bestanden en 172 matchende regels. Drie onafhankelijke aanwijzingen:

- geen `vnx-*` tmux-sessie en geen daemon-proces (`ps`, `tmux ls`);
- alle daemon-logs eindigen op 2026-06-27 16:13, `panes.json` en `t0_conversation.log` op 2026-06-27;
- de `.md`-stroom is leeg: `queue/`, `active/` en `pending/` bevatten 0 `.md`, `staging/` heeft 14 (nieuwste 2026-08-02), en het register bevat 0 `dispatch_promoted`-events.

De operator-T0's draaien niet in `vnx-*`-sessies. Ze staan in sessies als `orch-t0`, `mc-t0`, `seo-t0`, `pa-engine` en `sc-t0-sm`, die de shell-wrappers starten (`tmux ls`). Uncertain: de definitie van die wrappers vond ik niet in `~/.zshrc`.

Onderscheid dat in de rest van dit rapport telt: de **daemon-keten** (supervisor, dispatcher, popup) is sinds 2026-06-27 stil, de **tmux-lane van de deur** (`claude_tmux_subscription`, `tmux_interactive_dispatch.py`) draaide tot 2026-09-09 en is op 2026-09-19 verwijderd. Ze delen tmux, maar niet hun aanroepketen.

### 4.3 De tmux-signaalinfrastructuur is losgeknipt van zijn lezers

Commit 496e0fe5 (besluit 2026-09-18, commit 2026-09-19) verwijderde `scripts/lib/tmux_interactive_dispatch.py`. Dat was de enige plek die `VNX_TMUX_SIGNAL_DIR` en `VNX_DISPATCH_ID` zette, de worker-scope-hook registreerde en de sentinels las. Gevolg:

- de drie `tmux_signal_*`-hooks en de staarten in de twee `pretooluse_block_*`-hooks kunnen nooit meer iets schrijven;
- `pretooluse_worker_scope_enforce.py` is nergens meer geregistreerd;
- `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md:86` noemt de hooks zelf al "inert".

Conflicting evidence: `docs/operations/WORKER_PERMISSIONS.md:93,133,207` beschrijft de worker-scope-hook nog als bedraad.

### 4.4 Wat een retire per bestand meetrekt

Een verwijdering raakt vier soorten vaste punten buiten het bestand zelf:

- **Testpennen:** `tests/test_architecture_doc_drift.py:110` (eist `tmux_signal_prompt_received.sh`), `tests/test_central_mode_path_gate.py:384-391`, `tests/test_live_requires_measured_health.py:55-75`, `tests/test_init_migrate_bootstrap.py:560`, `tests/test_context_rotation.py:449-451`.
- **Uitsluitingen:** `scripts/ci/test_exclusions.txt:206-208` (bestaan van het pad wordt gecontroleerd).
- **Gegenereerde docs:** `docs/core/00_VNX_ARCHITECTURE.md:875-879` (hooklijst), `docs/core/DAEMON_LIVENESS.md`. Regenereren met `scripts/generate_architecture_doc.py --write` en `scripts/generate_daemon_liveness_md.py`.
- **Poortwachters:** `hookpin_check.sh` (SessionStart) en `vnx doctor` falen op een geconfigureerd hookpad dat niet resolveert.
- **Settings-merge:** `vnx_settings_merge.py:271-273` vervangt `hooks` bij `regen-settings --merge` volledig door de template. Alleen `pretooluse_block_raw_claude_spawn.sh` staat daarin, dus een regen schrapt de andere hooks van deze repo.

### 4.5 Klasse-verdeling

| Klasse | Aantal | Matchende regels |
|---|---|---|
| LEVEND, in de ongebruikte `vnx start`-keten | 12 | 172 |
| LEVEND, overig | 32 | 233 |
| ACHTER VLAG | 3 | 27 |
| ALLEEN TESTS | 14 | 51 |
| DOOD | 7 | 35 |
| ONBEPAALD | 3 | 5 |
| **Totaal** | **71** | **523** |

Van de 233 regels in de overige 32 LEVEND-bestanden zit 83 in `pane_manager.sh` en 34 in `dashboard/api_operator.py`. Het grootste deel van de rest is inert (37 regels in de drie `tmux_signal_*`-hooks) of tekst.

## 5. Key Findings: Deel 2, de 9 daemons

`read_daemon_register()` levert de 9 uit `start_all()` (`scripts/vnx_supervisor_simple.sh:195-221`). Alles hieronder is gemeten op 2026-09-24.

### 5.0 Overzicht

| Daemon | Script | Draait | Laatste logregel | Oordeel |
|---|---|---|---|---|
| dispatcher | `dispatcher_minimal.sh` | nee | 2026-06-27 16:13 | WEG |
| smart_tap | `smart_tap_json_translator.sh` | nee | 2026-06-27 16:13 | WEG |
| receipt_processor | `receipt_processor.sh` | ja, PID 82744, sinds 2026-09-22 17:10 | loopt | NODIG |
| heartbeat_ack_monitor | `heartbeat_ack_monitor.py` | nee | 2026-06-27 16:13 | WEG |
| queue_watcher | `queue_popup_watcher.sh` of `queue_auto_accept.sh` | nee | 2026-06-27 | WEG |
| dashboard | `generate_valid_dashboard.sh` | ja, PID 12593, sinds 06:21 | loopt | NODIG, inkrimpen |
| state_manager | `unified_state_manager.py` | nee | 2026-06-27 16:13 | WEG |
| intelligence_daemon | `intelligence_daemon.py` | nee | 2026-06-27 16:12 | VERVANGEN en GEPARKEERD |
| recommendations_engine | `recommendations_engine_daemon.sh` | nee | 2026-06-27 16:13 | VERVANGEN en GEPARKEERD |

Het register meet hetzelfde: `t0_state.json` (`system_health.daemon_liveness`, gebouwd 10:38) toont `receipt_processor` en `dashboard` als `running` en de overige zeven als `absent`.

### 5.1 dispatcher: `scripts/dispatcher_minimal.sh` (664 regels). Oordeel WEG

**Wie start hem nog.**
- Supervisor `vnx_supervisor_simple.sh:198`, herstart bij crash via de monitor (`:344`).
- `vnx start` zonder supervisor (`start.sh:321-323,449-451`).
- `vnx resume` (`resume.sh:44-52`, via `dispatcher_supervisor.sh`, of direct).
- Handmatig `vnx restart dispatcher` (`vnx_process_ux.py:43,430-500`) en het dashboard (`serve_dashboard.py:65,720-770`).
- Launchd: nee. Draait: nee.

**Wat hij produceert.**
- Scant `dispatches/pending/*.md` (`:609`), valideert, claimt een terminal en levert af (`dispatch_deliver.sh:572-611`). T0 gaat via tmux, T1 tot T3 via `subprocess_dispatch.py` (`_ddt_subprocess_delivery :500`, adapter-keuze `:580-585`).
- Verplaatst naar `rejected/` en `stuck/` (`_cleanup_stuck_dispatches :537-597`), schrijft `dispatcher_v8.log` (`:78`).
- Roept `notify_dispatch.py` (`dispatch_lifecycle.sh:587`, voedt de ACK-monitor) en `log_dispatch_metadata.py` (`:518`).
- Host van vijf ticks (`dispatcher_supervisor_ticks.sh`): `lease_sweep`, `runtime_supervise`, OI-bridge, objective-reconcile en learning-cycle. Alle vijf zijn een no-op tenzij `VNX_SUPERVISOR_MODE=unified`, en die staat nergens (paragraaf 3.6). In de vnx-dev store bestaat geen enkel bewijs dat een tick ooit draaide.

**Wie leest dat.** Worker-terminals, de ACK-monitor, het dashboard (`queues`), `vnx status`.

**Overname.**
- De deur (`vnx dispatch <pending-id>`, `dispatch_cli.py`) is sinds 2026-06-24 default aan (`docs/core/DISPATCH_RULES.md:3-10`). `pending/` bevat 3179 bundelmappen en 0 `.md`. De dispatcher-glob `pending/*.md` ziet ze niet (`:609-610`). Het register telt vanaf augustus 705 `dispatch_created`-events van de deur (451 in augustus, 254 in september). De tmux-lane van de deur (`claude_tmux_subscription`) was daarvan 164, tot 2026-09-09. Die lane spawnde zelf panes en liep niet via `dispatcher_minimal.sh`. Sinds 2026-09-19 zijn er alleen `claude_headless` en `provider`.
- Ticks: `objective reconcile` draait via de SessionStart-hook (`session_reconcile_autoclose.sh`, log 2026-09-23). OI-bridge heeft `roadmap_manager.py:827` als tweede aanroeper. Het learning-cycle staat in de (geparkeerde) nachtpipeline (`nightly_intelligence_pipeline.sh:202`). `lease_sweep.py` en `runtime_supervise.py` hebben **geen** andere aanroeper: `grep` geeft alleen `dispatcher_supervisor_ticks.sh:219,42`.

**Afhankelijkheden bij verwijderen.**
1. `vnx resume` faalt gesloten: `_vnx_resume_verify_readiness` eist een levende dispatcher en laat anders de `PAUSED`-marker staan. `vnx pause` stopt hem als een van drie verplichte daemons (`pause.sh:119-121`).
2. `lease_sweep` en `runtime_supervise` verliezen hun enige host. Gemeten effect tot nu toe: nul (drie leases, alle `idle`).
3. De legacy `.md`-stroom (`vnx promote`, `queue/`) stopt. Laatste staging 2026-08-02.
4. De T0-rolinstructie `Promote vs Manager-Block Rule` (`.claude/terminals/T0/role-orchestrator.md:195-209`) beschrijft die stroom nog.

### 5.2 smart_tap: `scripts/smart_tap_json_translator.sh` (677 regels). Oordeel WEG

**Start.** Supervisor `:204`, `start.sh:315-317,443-445`, `vnx restart smart_tap`, dashboard-restart (`serve_dashboard.py:64`). Draait niet.

**Produceert.** Leest de scherminhoud van de T0-pane (`tmux capture-pane -t "$T0_PANE" -p -J -S -300`, `:584`), extraheert Manager Blocks en schrijft `dispatches/queue/*.md` (`:453`), plus `processed_block_hashes.txt` (0 B, 2026-06-27) en `tap.log`.

**Lezers.** `queue_popup_watcher.sh`, `queue_ui_enhanced.sh`, `queue_auto_accept.sh`.

**Overname.** T0 dispatcht via de deur: "OUTPUT: dispatch via the single-entry door `vnx dispatch`" en "Manager blocks to terminal are ONLY for accidental dispatches or operator-requested manual delivery" (`.claude/terminals/T0/role-orchestrator.md:143-144`). De deur schrijft bundels rechtstreeks, zonder een scherm te lezen.

**Oordeel.** WEG. Dit is de enige daemon met een tmux-scherm als invoer. Opinion: hij is bij verwijderen het schoonst, want er hangt geen lezer aan behalve de `.md`-queue die zelf weggaat.

### 5.3 receipt_processor: `scripts/receipt_processor.sh` (658 regels). Oordeel NODIG

**Start.**
- Launchd `com.vnx.receipt-processor.vnx-dev` naar `scripts/receipt_processor_supervisor.sh` naar `receipt_processor.sh` (PID 82602 en 82744, `KeepAlive SuccessfulExit=false`). Dit is de productiestart.
- Supervisor `:205`. `start.sh:44` en `resume.sh:69-72` slaan een handmatige start over als launchd het project al beheert (`launchd_receipt_processor_guard.sh`).

**Produceert.** `state/t0_receipts.ndjson` (165 MB, mtime 10:38), `receipt_processing.log`, de outbox `receipts/pending/`, en triggert `generate_t0_brief.sh` (`rp_append.sh:48`). `t0_brief.json` is vers (10:38).

**Lezers.** `build_t0_state.py`, T0-pull via `receipt_query.py`, de closure-verifier en de poorten, de dashboards.

**Overname.** Niet van toepassing: dit is de bron van het audit-spoor. Het levende alternatief voor tmux-push is al ingebouwd: T0 haalt receipts op (pull) en de push staat uit (`VNX_RECEIPT_T0_PUSH=0`, gemeten 67 keer suppressed).

**Oordeel.** NODIG. Ontkoppel hem van tmux op vier plekken: `receipt_processor.sh:43` (source `pane_manager.sh`), `:633-636` (pane-health), `rp_dedup.sh:139-142` (flood-paste, ongated) en `rp_delivery.sh:124,195` (achter de vlag, dus reeds inert).

### 5.4 heartbeat_ack_monitor: `scripts/heartbeat_ack_monitor.py` (936 regels). Oordeel WEG

**Start.** Supervisor `:210` (monitor `:344`), `vnx restart` (`vnx_process_ux.py:46`). `start.sh` heeft hem niet in zijn fallbacklijst en het dashboard ook niet. Draait niet.

**Produceert.** Een unix-socket `heartbeat_ack_monitor.sock` (`:731-744`), ACK-signalen naar de terminal-state-shadow (`update_terminal_state`) en een ACK-melding in de T0-pane via `tmux load-buffer`, `paste-buffer` en `send-keys` (`:603-648`, `VNX_ACK_DIRECT_NOTIFY` default 1). Pollt `tmux list-panes -a` (`:493`).

**Enige voeding.** `notify_dispatch.py`, aangeroepen op één plek: `dispatch_lifecycle.sh:587`, in het dispatcher-pad. Zonder dispatcher komt er nooit iets binnen.

**Overname.** De headless lane heeft een eigen eventpijplijn (dat zegt de monitor zelf, `:213-222`). In het register staan `worker_exited` (173) en `dispatch_started` als afgeleide events van receipts (`register_emit.py:75`).

**Oordeel.** WEG, samen met de dispatcher. Writes naar `terminal_state.json` via `terminal_state_shadow` zijn een tweede producent. Uncertain: welke lezers van `terminal_state.json` (o.a. `build_t0_state.py:445`) na verwijdering op alleen de reconciler leunen. Dat moet bij de implementatie gemeten worden.

### 5.5 queue_watcher: `scripts/queue_popup_watcher.sh` (251 regels) of `queue_auto_accept.sh` (68). Oordeel WEG

**Start.** Supervisor `:212` (popup) of `:215` (auto-accept), monitor alleen voor de popup-variant (`:345-347`, dus de auto-accept-variant krijgt geen crash-herstart). `resume.sh:90-106`, `vnx restart queue_watcher`, dashboard. Draait niet.

**Produceert.** Tmux-meldingen (`display-message`, statusbalk-flash, `rename-window`), het venster `VNX-Queue` met `queue_ui_enhanced.sh` (`:132`), en `_stale_pending_catchup` (`:158-171`). De auto-accept-variant verplaatst `queue/` naar `pending/` zonder mens.

**Lezers.** De operator, en via `pending/` de dispatcher.

**Overname.** De menselijke poort voor een dispatch zit nu in `vnx dispatch <pending-id>` en de kolom `dispatches.operator_approved_at`, niet meer in een popup.

**Latente bijwerking, afgeleid uit code en gemeten telling.** `count_files` (`:39-42`) en `_stale_pending_catchup` (`:172`) zoeken recursief naar `*.md`. De bundelmappen van de deur bevatten `instruction.md` en `final_prompt.md`. Er zijn nu 4351 zulke bestanden onder `pending/`, en die zijn allemaal ouder dan drie minuten of worden het. Bij een herstart zou de watcher bij zijn start en daarna elke 150 cycli (circa 5 minuten) een `touch` doen op elk ervan en elke 2 seconden `Note: 4351 dispatch(es) in pending (auto-processing)` loggen. Inferred: `dispatch_cleanup.py:180-183` veroudert bundels op de mtime van de bundelmap, en die verandert niet door een `touch` van een bestand erin. Uncertain of iets anders op de mtime van bundelbestanden leunt. Ik heb dit niet uitgevoerd.

**Oordeel.** WEG, beide varianten.

### 5.6 dashboard: `scripts/generate_valid_dashboard.sh` (445 regels). Oordeel NODIG, inkrimpen

**Start.**
- Launchd `com.vnx.dashboard-generator.vnx-dev`, handmatig geïnstalleerd, sinds #1900 (2026-09-24 06:21). `KeepAlive SuccessfulExit=false`, singleton via `enforce_singleton "dashboard"`.
- Supervisor `:217`, `start.sh:328,456`, `vnx restart dashboard`. Wie het eerst pakt wint het slot.

**Produceert.** `state/dashboard_status.json`, elke 2 seconden (`:439-444`).

**Lezers.** De SessionStart-vershedspoort (`session_state_freshness.py:100`: "STATE FRESHNESS: BLOCKED" als het bestand te oud is, reden van #1900), `intelligence_dashboard.py`, `quality_metrics_updater.sh:15`, `t0_intelligence_aggregator.py:228`, `terminal_snapshot.py:49`, de dashboardserver (`serve_dashboard.py:13`) en de legacy UI.

**Wat er tmux-tijdperk in zit, live gemeten in `dashboard_status.json` van 10:46:**
- `processes` doet `pgrep -f` op acht namen (`:361-374`), waarvan er zeven aan de dode daemons hangen. Gemeten: `supervisor` en `state_manager` staan op `running: true` met PID 90839. Dat PID is de `claude -p` van deze dispatch, waarvan de argv de scriptnamen bevat. `pgrep -f` matcht de hele commandoregel. `daemon_register.py:240-251` beschrijft dezelfde valkuil en lost hem op met exacte basename-vergelijking per argv-token.
- `queues.pending` is 4351. Het telt `*.md` recursief (`count_md :24-31`), dus bestanden in bundels, geen dispatches.
- `metrics.throughput` en `totalProcessed` lezen `dispatcher.log`, staan op 0.

**Overname.** Niet van toepassing: het bestand is zelf de producent van het artefact dat de vershedspoort leest. Beide levende daemons staan al in `launchd_liveness` als `loaded`, status 0.

**Oordeel.** NODIG. Snoei de `processes`-, `queues`- en `metrics`-secties of meet ze met dezelfde psutil-basenamemethode als `daemon_register.py`. Opinion: houd de sleutels bestaan zolang de dashboardserver en de legacy UI ze lezen, en meet eerst wie dat nog doet (Uncertain, paragraaf 7).

### 5.7 state_manager: `scripts/unified_state_manager.py` (486 regels). Oordeel WEG

**Start.** Supervisor `:218`, `start.sh:334-336,462-464`, `vnx restart`. Draait niet.

**Produceert.** `unified_state.ndjson` en `manager_cursors.json` (beide ontbreken in de store), voert `t0_intelligence.ndjson` via `T0IntelligenceAggregator` (0 B, 2026-06-27), en triggert `generate_t0_brief.sh` (`:417-418`).

**Lezers.** `t0_query.py` (leest `unified_state.db`, dat nergens bestaat: `database/` bevat alleen een lege `horizon.db`), `query_quality_intelligence.py:288`, `check_intelligence_health.py:347`, de footer-sjablonen `templates/footers/t0_action_request_autonomous.md:11,62`.

**Overname.** `build_t0_state.py` in de SessionStart-hook ("Replaces 8+ separate startup scripts", `:4`) en `rp_append.sh:48` voor de T0-brief (die is vers zonder de daemon).

**Oordeel.** WEG. De lezers van `t0_intelligence.ndjson` krijgen nu al een leeg bestand. Ruim ze op bij dezelfde wijziging.

### 5.8 intelligence_daemon: `scripts/intelligence_daemon.py` (535 regels). Oordeel VERVANGEN en GEPARKEERD

**Start.** Supervisor `:219`, `vnx restart`, dashboard-restart (`serve_dashboard.py:70`). `start.sh` heeft geen fallback. Draait niet. Geen scope-treffers voor tmux: hij hoort niet bij de tmux-uitfasering.

**Produceert.** `governance_digest.json` elke 300 s (`:73-100`, ontbreekt in de store), `intelligence_health.json` (2026-06-27 16:13), writes naar `quality_intelligence.db`, een heartbeat-beacon en om 18:00 een dagelijkse hygiene en learning-cycle (`:396-401`). De uurlijkse "extractie" is een telling van patronen (`:376-395`).

**Lezers.** `dashboard/api_operator.py:1215,1379` (governance-digest), `generate_valid_dashboard.sh:75`, `check_intelligence_health.py:340`.

**Overname.** `nightly_intelligence_pipeline.sh` vervangt de dagelijkse hygiene om 18:00 (kop `:3-5`). Die pipeline is zelf geparkeerd en niet geïnstalleerd (`launchd_liveness`: `parked`). De 5-minutendigest heeft geen opvolger.

**Operatorbesluit.** Geparkeerd op 2026-09-09 (`beacon_register.PARKED_COMPONENTS`, `:114-118`), `beacon_health` toont hem al als `parked`.

**Oordeel.** VERVANGEN door de nachtpipeline voor de dagelijkse hygiene, en GEPARKEERD. Niet aanraken in de tmux-uitfasering. Wel als `parked` in `daemon_liveness` opnemen.

### 5.9 recommendations_engine: `scripts/recommendations_engine_daemon.sh` (19 regels). Oordeel VERVANGEN en GEPARKEERD

**Start.** Supervisor `:220`, `vnx restart`. Geen fallback in `start.sh`. Draait niet.

**Produceert.** Een lus die elke 30 s `generate_t0_recommendations.py --lookback 60` draait (`:13-19`) en `t0_recommendations.json` schrijft (2026-06-17, 99 dagen oud).

**Lezers.** `userpromptsubmit_intelligence_inject.sh:30` (de T0-hook uit het settings-sjabloon), `.claude/skills/t0-orchestrator/scripts/intelligence.sh:38`, `dashboard/api_recommendations.py:17`, `generate_valid_dashboard.sh:303`, `tag_intelligence.py:719`.

**Overname.** Fase 10 van de nachtpipeline draait dezelfde generator (`nightly_intelligence_pipeline.sh:254`). De wrapper-lus voegt niets toe.

**Operatorbesluit.** Niet in `PARKED_COMPONENTS`, wel zijn artefact in `PARKED_ARTIFACTS["t0_recommendations"]` (`beacon_register.py:140-145`).

**Oordeel.** VERVANGEN door fase 10 van de nachtpipeline en GEPARKEERD. Zelfde behandeling als 5.8.

### 5.10 Welke daemons horen nog in de verwachte lijst van `daemon_liveness`

Nu: `daemon_register.read_daemon_register()` levert 9 entries, alle met `expected: True`. Het resultaat op 2026-09-24:

- `daemon_liveness.overall = fail` (7 van 9 afwezig);
- daaruit volgt `system_health.status = degraded` en `producer_liveness = {overall: fail, daemon_overall: fail, launchd_overall: ok}` (`build_t0_state.py:2349-2388`);
- `launchd_liveness.overall = ok`, en die dekt beide levende daemons al (`com.vnx.receipt-processor.vnx-dev` en `com.vnx.dashboard-generator.vnx-dev`, allebei `loaded`).

| Daemon | In de verwachte lijst? | Reden |
|---|---|---|
| receipt_processor | ja | Draait, audit-spoor. |
| dashboard | ja | Draait, producent van het vershedsartefact. |
| intelligence_daemon | nee, als `parked` met reden | Zelfde besluit dat `beacon_health` en `launchd_liveness` al eren. `daemon_liveness` kent geen `parked`-toestand. |
| recommendations_engine | nee, als `parked` met reden | Idem. |
| dispatcher | nee | WEG. |
| smart_tap | nee | WEG. |
| heartbeat_ack_monitor | nee | WEG. |
| queue_watcher | nee | WEG. |
| state_manager | nee | WEG. |

Na die wijziging zou `producer_liveness` op `ok` komen. `system_health.status` blijft `degraded` zolang `beacon_health.overall = fail` (twee falende beacons: `receipt_conversion_rejections` en `report_to_receipt_converter`). Dat staat los van de daemons.

Ontwerpvraag voor de implementatie: het register wordt uit `start_all()` gelezen. Vervalt de supervisor, dan verliest het zijn bron. Opinion: leid het register af van de launchd-sjablonen (`scripts/launchd/*.plist`, die `launchd_liveness` al scant in `_scan_launchd_dir`) plus een klein `parked`-register, in plaats van een `start_all()` te bewaren dat twee launchd-beheerde daemons start.

## 6. Analysis

### 6.1 Afhankelijkheden die eerst los moeten

| Nr | Vast punt | Bewijs | Gevolg als het blijft staan |
|---|---|---|---|
| 1 | `vnx resume` eist een levende dispatcher | `resume.sh` `_vnx_resume_verify_readiness`; `pause.sh:119-121`; `PAUSED`-guards `dispatcher_minimal.sh:18`, `dispatcher_supervisor.sh:27`, `receipt_processor.sh:15`, `receipt_processor_supervisor.sh:27` | Na een `vnx pause` kan `vnx resume` niet slagen. |
| 2 | `receipt_processor.sh` hangt aan `pane_manager.sh` | `receipt_processor.sh:43,633-636`, `rp_dedup.sh:139-142` | Verwijderen van `pane_manager.sh` breekt de levende audit-daemon bij zijn start. |
| 3 | `lease_sweep.py` en `runtime_supervise.py` hebben één host | `dispatcher_supervisor_ticks.sh:219,42`, comment `planning_cli.py:1797` | Twee mechanismen zonder aanroeper. Gemeten schade tot nu toe nul. |
| 4 | `start_all()` is bron van `daemon_liveness` en docs | `daemon_register.py:108-190`, `docs/core/DAEMON_LIVENESS.md`, `tests/test_daemon_register.py:157`, `tests/test_architecture_doc_drift.py` | Verwijderen zonder aanpassen geeft `ValueError` of drift in CI. |
| 5 | Andere starters van dezelfde scripts | `vnx_process_ux.py:42-53`, `serve_dashboard.py:63-73,720-770`, kill-lijst `bin/vnx:927-960` | Gaan naar een script dat niet bestaat. Twee namen in `serve_dashboard.py` wijzen al naar niet-bestaande bestanden. |
| 6 | Legacy `.md`-stroom in de T0-rol | `.claude/terminals/T0/role-orchestrator.md:195-209`, `bin/vnx:2515-2523`, `pr_queue_manager.py:1065-1170` | De rol (vlootbreed gesynchroniseerd met `vnx role sync`) beschrijft een levering die niet meer bestaat. |
| 7 | Consumer-bedrading buiten de repo | E9 (`SEOcrawler_v2`), root-keybinding `C-g` | Hooks en binds wijzen naar verwijderde scripts in andere projecten. |
| 8 | Hook- en testpennen | paragraaf 4.4 | Rode CI of rode `vnx doctor`. |

### 6.2 Volgorde

Opinion: van klein en risicoloos naar groot.

1. `daemon_liveness`: verwacht, geparkeerd en verwijderd expliciet maken. Klein, en het haalt de permanente `fail` weg.
2. `receipt_processor` van tmux losmaken (vier plekken). Het is de enige draaiende tmux-koppeling.
3. Dashboard-generator inkrimpen en de `pgrep -f`-telling vervangen.
4. `pause` en `resume` herschrijven zodat alleen `receipt_processor` verplicht is.
5. Dispatcher, `smart_tap`, ACK-monitor, `queue_watcher`, `state_manager`, supervisor en het tmux-deel van `start.sh` samen weghalen, met de popup-bestanden, en beslissen wat er met `lease_sweep` en `runtime_supervise` gebeurt. Opinion: weg met de dispatcher, want ze draaiden hier nooit. Blijft sweeping nodig, dan als eigen launchd-taak.
6. De drie `tmux_signal_*`-hooks en de dode staarten in twee `pretooluse_block_*`-hooks. Settings, templates, gegenereerde docs en de vier testpennen mee.
7. De 7 DOOD-bestanden en de ALLEEN TESTS-bestanden zonder handmatig gebruik.

`intelligence_daemon` en `recommendations_engine` blijven buiten deze reeks (geparkeerd).

### 6.3 Waar de tmux-code niet de bedoeling is te verwijderen

- `scripts/lib/tmux_worktree.py` blijft levend voor worktree-isolatie (`dispatch_envelope.py:190`, `dispatch_worktree_isolation.py:1052`, `orphan_sweep.py:905`). Zijn naam is misleidend, zijn inhoud is git.
- `claude-tmux` is een nog gebruikte lock-class en subsysteemnaam (paragraaf 3.1 en 3.3). Hernoemen raakt zes bestanden en de test die de naam pint (`tests/test_live_requires_measured_health.py:55-75`).

## 7. Uncertainty & Gaps

1. **Uncertain: gebruik buiten het `vnx-dev` project.** Ik heb één store gemeten. Een `vnx start` in een ander project heb ik niet uitgesloten.
2. **Uncertain: de SessionStart-tmux-probe.** Afgeleid uit `canonical_state_views.py:211-222` en de leeftijd van `terminal_state.json`. Niet met een trace bevestigd.
3. **Uncertain: het Next.js-dashboard.** Niets start het (poort 3100 dicht). Of iemand `agent-stream`, `operator/sessions` of `operator/reports` opent zie ik niet. Meten: toegangslog van de dashboardserver.
4. **Uncertain: het dashboard-poortprobleem.** `next.config.ts:8` proxyt naar 4174, waar een ander proces luistert, terwijl de server op 4173 staat. README en TTD spreken elkaar tegen. Conflicting evidence, niet uitgezocht.
5. **Uncertain: `hooks/vnx_rotate.sh` heeft ooit gedraaid.** Geen logs gevonden. Meten: `**/logs/vnx_rotate_T*.log` over alle consumer-repos.
6. **Uncertain: de matcher `Task` tegen het tool `Agent`.** Vier subagents startten zonder blokkade. Meten met `hooks/vnx_hook_payload_logger.sh` tijdens een subagent-aanroep.
7. **Uncertain: `VNX_T0_ROTATION` en `session_stop_rotation.py`.** Alleen een 0-byte errorlog van 2026-07-13.
8. **Uncertain: de benchmark-maandrun.** Of de operator hem nog draait zie ik niet. Er staat niets gecommit in `results/` na #828 (2026-06-04). Meten: mtimes van `scripts/benchmark/field-tests/results/*` op de Mac van de operator.
9. **Uncertain: de zeven ontbrekende bestanden** (78 tegen 71, paragraaf 2.1).
10. **Host-installatie.** De hostkopie draait v1.6.2, byte-identiek met de repo voor de 11 bestanden die ik vergeleek. Een wijziging in de repo raakt de host pas na een nieuwe release-installatie.
11. **Uncertain: welke lezers van `terminal_state.json`** na verwijdering van de ACK-monitor alleen op de reconciler leunen (paragraaf 5.4).
12. **Conflicting evidence: datum van de lane-verwijdering.** Docs zeggen 2026-09-18 (`CLAUDE.md`, `dispatch.sh:388`), de commit 496e0fe5 is van 2026-09-19 08:44.

## 8. Recommendations

Opinion: geprioriteerd, alle vooraf te toetsen met het operatorbesluit.

1. **Neem in `daemon_liveness` een `parked`- en een `retired`-toestand op** en zet de verwachte lijst op `receipt_processor` en `dashboard`, met `intelligence_daemon` en `recommendations_engine` als `parked` (reden uit `beacon_register.PARKED_*`). Dit is de kleinste stap en het haalt de permanente `fail`.
2. **Maak `receipt_processor.sh` tmux-vrij** (`:43`, `:633-636`, `rp_dedup.sh:139-142`). Het is de enige levende koppeling, en de `setup_pane_titles`-aanroep kan een pane van de operator hertitelen.
3. **Besluit of `vnx start` als T0-launcher blijft bestaan.** De operator-T0's draaien in sessies van de shell-wrappers. Blijft `vnx start` gewenst, dan gaat het om een aparte, kleine T0-launcher zonder daemons, popup-binds of `panes.json`.
4. **Haal de vijf WEG-daemons met de supervisor in één wijziging weg**, na `pause` en `resume` (aanbeveling 5), de kill-lijst, de starters E8, de T0-rol, en de gegenereerde docs.
5. **Herschrijf `pause` en `resume`** zodat alleen `receipt_processor` verplicht is.
6. **Verwijder de drie `tmux_signal_*`-hooks en de dode staarten**, met settings, templates, gegenereerde docs, `hookpin_check.sh`-verwachting en de vier testpennen.
7. **Ruim de DOOD-bestanden op** (paragraaf 3, klasse DOOD: 7) en bepaal per ALLEEN TESTS-bestand of het handmatige gebruik blijft.
8. **Los `pgrep -f` op in de dashboard-generator.** De huidige `supervisor: running` op een `claude -p`-PID is nu al een onwaar getal in een bestand dat de vershedspoort leest.
9. **Laat `intelligence_daemon` en `recommendations_engine` met rust** tot het operatorbesluit van 2026-09-09 wordt herzien.

Vragen aan de operator: (a) Blijft het Next.js-dashboard in gebruik. (b) Draait `vnx start` nog in een ander project. (c) Moet de rotatie-hook voor `SEOcrawler_v2` blijven.

## 9. Sources

Primaire bronnen (code en meting):

1. Git-tree op `main` 11df8ff8 (worktree `dispatch-20260924-tmux-inventaris-b-rest-daemons`): `git ls-files`, `git log`, `git show 496e0fe5`.
2. Scope-grep uit de opdracht en per-bestand tellingen (`grep -ciE`).
3. `bin/vnx` en `scripts/commands/*.sh`.
4. `scripts/vnx_supervisor_simple.sh`, `scripts/dispatcher_minimal.sh`, `scripts/lib/dispatch_deliver.sh`, `scripts/lib/dispatcher_supervisor_ticks.sh`, `scripts/lib/dispatch_lifecycle.sh`.
5. `scripts/smart_tap_json_translator.sh`, `scripts/heartbeat_ack_monitor.py`, `scripts/queue_popup_watcher.sh`, `scripts/queue_ui_enhanced.sh`.
6. `scripts/receipt_processor.sh`, `scripts/lib/receipt_processor/rp_delivery.sh`, `rp_dedup.sh`, `scripts/pane_manager.sh`.
7. `scripts/generate_valid_dashboard.sh`, `scripts/unified_state_manager.py`, `scripts/intelligence_daemon.py`, `scripts/recommendations_engine_daemon.sh`, `scripts/nightly_intelligence_pipeline.sh`.
8. `scripts/lib/daemon_register.py`, `scripts/lib/beacon_register.py`, `scripts/build_t0_state.py`, `scripts/lib/canonical_state_views.py`, `scripts/lib/terminal_state_reconciler.py`.
9. `.claude/settings.json`, `templates/settings_vnx_keys.json.tmpl`, `scripts/hooks/*`, `hooks/*`.
10. `scripts/launchd/*.plist` en de geïnstalleerde `~/Library/LaunchAgents/com.vnx.*.plist`.
11. `dashboard/*` en `dashboard/token-dashboard/*`.
12. `.github/workflows/vnx-ci.yml`, `subsystems-drift.yml`, `Makefile`, `scripts/ci/`.
13. `tests/` (verwijzingen en pennen, niet uitgevoerd).
14. Host op 2026-09-24: `ps aux`, `launchctl list`, `tmux ls`, `tmux list-keys -T root`.
15. Centrale store `~/.vnx-data/vnx-dev`: `logs/`, `state/` (o.a. `t0_state.json`, `dashboard_status.json`, `dispatch_register.ndjson`, `receipt_processing.log`), `dispatches/`, `mode.json`, en `runtime_coordination.db` (read-only, `immutable=1`).

Secundaire bronnen:

16. `docs/core/DISPATCH_RULES.md`, `docs/operations/WORKER_PERMISSIONS.md`, `docs/core/DISPATCH_AND_INTELLIGENCE_ARCHITECTURE.md` en `.claude/terminals/T0/role-orchestrator.md` (beschrijvend, waar dat botst met de code geldt de code).
17. Vier subagent-rapporten (read-only classificatie van 66 bestanden). Gecontroleerd op de beweringen die een oordeel dragen.
18. `CHANGELOG.md` en de commit-boodschappen (alleen voor data en context).
