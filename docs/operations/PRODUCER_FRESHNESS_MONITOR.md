# Producer Freshness Monitor

**Doel:** stil falen van producenten binnen een dag zichtbaar maken. Aanleiding
(2026-07-31): de review-gate-laag lag er dagen stilletjes uit — nul requests sinds
25-07, nul results sinds 28-07 — terwijl dispatches met `gate=codex_gate` gewoon
doorgingen. De afwezigheid van een resultaat zag er identiek uit als "niets te doen".

Het kernprincipe: **groepeer op de eigen sleutel van elke producent, nooit op
tabel- of directory-niveau.** Een tabel kan levend ogen terwijl individuele
sleutels dood zijn (`governance_metrics` schreef `dispatch_count` dagelijks terwijl
`fpy`/`rework_rate` al zes weken dood waren; de `dispatches`-tabel had 35 rijen van
de `dlv-`-producent terwijl de dispatch-deur sinds 16-07 niets meer schreef).

## Onderdelen

| # | Onderdeel | Bestand |
|---|-----------|---------|
| 1 | Per-sleutel versheidsdiff met cadans-drempel per producent, NDJSON-output | `scripts/lib/producer_freshness.py` + `configs/producer_freshness.yaml` |
| 2 | Exit-status-capture launchd/cron/nohup → `job_exits.ndjson` | `scripts/lib/job_exit_capture.py` |
| 3 | PATH/interpreter-pariteitscheck (voorgrond vs achtergrond) bij SessionStart | `scripts/lib/path_parity.py` + `scripts/hooks/path_parity_check.sh` |
| 4 | Guard-fired-teller (observe-only) | `scripts/lib/guard_stats.py`, geïnstrumenteerd in `phantom_guard.py` en `plan_gate_enforcement.py` |
| — | Sweep-CLI + heartbeat | `scripts/producer_freshness_monitor.py` |
| — | Domme tripwire (bash + `find -mmin` only) | `hooks/monitor_tripwire.sh` |
| — | Dagelijkse scheduling | `scripts/launchd/com.vnx.producer-freshness-monitor.plist` |
| 5 | Gate-obligaties: declaratie ↔ bewijs (OI-876/OI-881) | `scripts/lib/gate_obligations.py` + `scripts/gate_obligation_runner.py` + `scripts/launchd/com.vnx.gate-obligation-runner.plist` |

## 1. Per-sleutel versheidsdiff

De registry (`configs/producer_freshness.yaml`) declareert producenten met een
`cadence_seconds`-drempel. Twee brontypes:

- `directory` — glob files, sleutel uit bestandsnaam via `(?P<key>...)`-regex,
  timestamp = mtime. (review_gates: `pr-<nr>-<gate>.json` → sleutel = gate-naam)
- `sqlite` — read-only query, sleutel/timestamp uit kolommen; `key_transform:
  prefix` groepeert op het deel vóór de eerste `-` (producent-prefix).
  `expected_keys` maakt een producent die **helemaal niets** schrijft zichtbaar:
  afwezigheid kan niet gevonden worden door bestaande rijen te groeperen.

Elke finding bevat optioneel `demand`-bewijs: het aantal events in een
demand-bron (bijv. `dispatch_register.ndjson`) dat nieuwer is dan de `last_seen`
van de sleutel — "er viel wél werk terwijl deze producent zweeg".

Output is NDJSON (`<state_dir>/producer_freshness.ndjson`): één
`producer_freshness_sweep`-samenvatting plus één `producer_freshness_finding` per
stille/missende sleutel. NDJSON houdt ADR-007 (composite UNIQUE/PK voor nieuwe
centrale-DB-tabellen) buiten schot — de monitor opent geen centrale-DB-schrijfpad.

## 1b. Gate-obligaties (OI-876/OI-881)

De derde producentgroep in de registry, `review_gate_obligations`
(type `gate_obligations`), toetst elke **gedeclareerde** gate aan een
**daadwerkelijk resultaat** — de duurzame fix voor het incident van 31-07:

1. **De deur registreert.** `dispatch_cli.run_dispatch` schrijft voor elke
   geaccepteerde dispatch met `gate=<naam>` één obligatie
   (`<state_dir>/review_gates/obligations/<dispatch_id>.json`, status
   `pending`). Vóór deze fix overleefde `spec.gate` de `load_spec` niet — de
   declaratie verdween zonder dat iets hem las.
2. **De runner vervult.** `scripts/gate_obligation_runner.py` (launchd, elke
   15 min) resolveert de PR van de dispatch (obligatie → `dispatch_metadata`
   → `gh pr list --head dispatch/<id>`) en draait exact de gedeclareerde gate
   via `review_gate_manager.request_and_execute`. Kan de gate niet draaien,
   dan is dat een **luide, geregistreerde uitkomst**: request- én
   result-record met status `not_executable` plus skip-rationale-audit.
   Stilte is geen eindtoestand meer.
3. **De monitor toetst per sleutel.** De scanner groepeert obligaties op
   gate-naam: `last_seen` = de oudste nog-pendende declaratie (of, als alles
   vervuld is, de nieuwste resolutie). Een declaratie zonder resultaat binnen
   cadans → stale-finding voor díé gate — een levende zuster-gate maskeert een
   dode niet meer (de les uit OI-881).

## 1c. De verplichtingslevenscyclus — drie toestanden, twee grenzen (OI-1532)

Een `awaiting`-obligatie (repo resolveert, `gh` werkt, geen PR) kent twee
beslissingen die een CONTRACT zijn, niet een implementatiedetail:

**Tak A — `branch_exists is False` is drieledig.** Dat ene signaal vouwt
"branch bestond en is verwijderd" samen met "branch is nog nooit aangemaakt
omdat de dispatch NOG DRAAIT". De discriminator die ze scheidt is de
occupancy-lock van de dispatch
(`<state_dir>/dispatch_worktree_claims/<safe_id>.occupancy`): een `fcntl.flock`
op een open file description die de KERNEL vrijgeeft zodra de houder eindigt.
Drie antwoorden, nooit een stille keuze voor een van de twee:

| `dispatch_live` | Betekenis | Beslissing |
|---|---|---|
| `False` | dispatch dood, lock vrijgegeven | `retired` (`no_pr_branch_gone`) |
| `True`  | dispatch draait nog, niet gepusht | `pending` (`no_pr_branch_gone_live`) — nooit retired |
| `None`  | liveness niet vast te stellen | `pending` (`no_pr_branch_gone_unmeasured`) — niet retired, zichtbaar |

De `None`-tak is een DERDE antwoord, geen stille default: een levende dispatch
mag nooit op ambigu bewijs worden afgeboekt (OI-1388), en een onmeetbare wordt
zichtbaar gemarkeerd zodat hij niet als normale wacht wordt weggelezen.

**Tak B — `stay_pending` is begrensd.** Een echte wacht (branch bestaat of
onduidelijk) probeert niet eeuwig opnieuw. Na
`_STAY_PENDING_ESCALATION_ATTEMPTS` pogingen (== `_UNRESOLVABLE_ESCALATION_ATTEMPTS`,
96 × 900s ≈ 24u — dezelfde constante als de twee andere begrensde takken,
nooit een tweede grens ernaast) escaleert de wacht luid naar `not_executable`
(`stay_pending_timeout`).

**Waarom de monitor dit niet alleen oppakt.** De docstring bij de runner
verwijst naar de producer-freshness-monitor als vangnet voor eeuwig wachtende
verplichtingen. Dat vangnet werkt alleen op stores waar de monitor draait.
Gemeten 30-08: op `mission-control` draait de monitor niet (geen heartbeat,
geen NDJSON — de launchd-plist resolveert naar `vnx-dev`); op `vnx-dev` draait
hij wel (10 findings, status stale). De 11 obligaties op 776 pogingen op
mission-control hebben daarom acht dagen niets gealarmeerd. De grens in de
runner is niet overbodig: hij is het vangnet dat werkt ongeacht of de monitor
draait, en hij sluit de wachtlus die de monitor veronderstelt te vangen.

## 2. Exit-status-capture

Twee capture-paden naar `<state_dir>/job_exits.ndjson`:

```bash
# wrapper (cron/nohup/launchd ProgramArguments) — transparant: de exit-code van
# het kind wordt ongemoeid teruggegeven.
python3 scripts/lib/job_exit_capture.py --state-dir ... --job nightly-pipeline -- \
    /bin/bash scripts/nightly_intelligence_pipeline.sh

# harvest (draait automatisch mee in elke sweep) — leest launchctl list en
# legt LastExitStatus van elke com.vnx.*-job vast. Vereist geen plist-wijziging;
# zo had OI-850 (maanden exit 127) zichtbaar geweest.
python3 scripts/lib/job_exit_capture.py --state-dir ... --harvest-launchd
```

De harvest dedupt via `job_exits_launchd_state.json`: een ongewijzigde status
wordt niet opnieuw vastgelegd, behalve dat een aanhoudende non-zero status één
keer per 24 uur herhaald wordt (een falende job mag niet één keer vuren en dan
weer stil worden). Een niet-startbaar commando (missende interpreter, de
OI-852-klasse) wordt als exit 127 vastgelegd in plaats van te raisen.

## 3. PATH/interpreter-pariteitscheck

OI-852 (Homebrew-relink brak de PATH-resolved `python3` voor achtergrond-jobs
terwijl de voorgrond-shell bleef werken) vervuilde de diagnose van elk ander
geval. De check is gesloten maar blijft bestaan: de relink kan opnieuw gebeuren.

Bij SessionStart draait `scripts/hooks/path_parity_check.sh`, die
`scripts/lib/path_parity.py` aanroept. Die probt `python3` tweemaal: met de
ambient environment (voorgrond) en met een gescrubde environment met de
launchd/cron-default `PATH=/usr/bin:/bin:/usr/sbin:/sbin` (achtergrond).
Pariteit faalt bij een niet-draaiende achtergrond-interpreter of een
major.minor-versieverschil; een executable-padverschil is informatief.
Resultaat naar `<state>/path_parity.json`; bij breuk een SessionStart-warning.
De hook faalt nooit de sessie (altijd exit 0).

## 4. Guard-fired-teller

"Deze guard staat al 30 dagen 100% op False" was onzichtbaar — een niet-vurende
guard verdwijnt in `logger.debug`. `guard_stats.record_guard_evaluation(guard,
fired)` appendt elke evaluatie naar `<state_dir>/guard_evaluations.ndjson`.

**Harde eis: observe-only.** De teller wrapt de returnwaarde van een guard en
verandert de beslissing nooit; een teller-fout wordt gelogd en geslikt, nooit
gepropageerd. Geïnstrumenteerd: `phantom_guard.phantom_guard` (fired =
`is_phantom`) en `plan_gate_enforcement.plan_gate_state` (fired = `UNRESOLVED`).

```bash
python3 scripts/lib/guard_stats.py --summary   # per guard: evaluations, fired_pct,
                                               # suspect_silent (>=30d span, 0 firings)
```

## De ironie: de monitor kan zelf stil falen

Twee verdedigingen:

1. **Heartbeat bij elke run.** De sweep schrijft via `HealthBeacon` altijd
   `<data_dir>/health/producer_freshness_monitor.json`, óók bij nul bevindingen.
   Een sweep die niets vindt én niets schrijft is niet te onderscheiden van een
   sweep die niet draaide.
2. **Domme tripwire.** `hooks/monitor_tripwire.sh` (SessionStart, dus draait al
   bij elke sessie) toetst alleen de leeftijd van dat heartbeat-bestand met
   `find -mmin +1560` (26u). De tripwire deelt **geen code, geen DB-verbinding
   en geen Python-interpreter** met de monitor: puur bash + find + jq/sed. Een
   breuk als OI-852 mag niet tegelijk de monitor en zijn eigen alarm stilleggen.
   Deze onafhankelijkheid is als test vastgelegd
   (`tests/test_monitor_tripwire.py::test_tripwire_shares_no_code_db_or_interpreter_with_monitor`).

## Bediening

```bash
# Handmatige sweep (schrijft report + heartbeat + launchd-harvest)
python3 scripts/producer_freshness_monitor.py

# Read-only tegen een willekeurige store (acceptatie/diagnose; schrijft niets)
python3 scripts/producer_freshness_monitor.py \
    --state-dir ~/.vnx-data/vnx-dev/state --no-write --human

# Exit 0 always means the sweep ran; findings are in the health file + NDJSON.
# OI-1039: exit 11 removed — launchd reads non-zero as permanent failure.
```

Dagelijkse scheduling via `scripts/launchd/com.vnx.producer-freshness-monitor.plist`
(06:00; vul `__VNX_REPO_ROOT__` in en `launchctl load -w`). De exit-status van de
sweep zelf wordt door de volgende harvest in `job_exits.ndjson` vastgelegd, en de
tripwire bewaakt de heartbeat — de monitor faalt nooit onzichtbaar.

## Tests

`tests/test_producer_freshness.py`, `tests/test_job_exit_capture.py`,
`tests/test_path_parity.py`, `tests/test_guard_stats.py`,
`tests/test_monitor_tripwire.py` — allemaal rood op `origin/main` (modules/
scripts bestaan daar niet), groen op de branch. De acceptatietest draait de
monitor read-only tegen de live store en eist de review-gate-laag als finding.
