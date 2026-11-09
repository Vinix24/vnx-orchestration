# ADR-055 - Het heartbeat-contract van de receipt processor, vijf toestanden en de uitrolvolgorde

**Status:** Aangenomen 07-11-2026 (T0, track `ledger-is-falsifiable`, PR 0).
**Datum:** 2026-11-07
**Besloten door:** T0, op het plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/ledger-is-falsifiable.md` (revisie 2, plan-gate-bevindingen OI-2607 tot en met OI-2612) en de inventaris `20261107-inv-ledger-falsifiable`. ADR-055 was vrij op `origin/main` `532813fce`; de hoogste bestaande ADR is ADR-054.
**Cross-refs:** ADR-005 (eerst het grootboek), ADR-043 (het kostengrootboek), ADR-045 (audit-spoor groep 1), ADR-051 (pin op runtime en releaseverificatie), ADR-054 (het governance-record van de envelope is geen eindregel).

## Context

Het grootboek moet een bron worden waarop een besluit mag leunen. De learning loop en de smart router leren straks uit dit grootboek, dus een vals groen signaal moet eruit. Van de vier gaten in het trackdoel zijn er drie dicht op main van vandaag: `audit_chain verify` meldt bij een ongekoppeld grootboek exit 2 en `unchained` (#1753); pytest-ruis wordt centraal herkend door `scripts/lib/receipt_outcome.py::noise_reason`; de converter-stderr staat in het processorlog en in de rejection-beacon.

Het vierde gat is open: de zichtbaarheid van de processor-liveness in `t0_state`. Er is alleen een PID. Een processor die vastloopt maar nog leeft leest daardoor "running". Een proces dat leeft is geen proces dat werkt, en dat verschil is precies wat een falsifieerbaar grootboek moet kunnen tonen: de receipts die niet binnenkomen laten geen spoor na.

Dit ADR legt het contract vast dat de drie volgende PR's van de track implementeren: een heartbeatbestand (de schrijver, PR 2), een lezer met vijf toestanden (PR 1) en de uitrol- en terugdraairoute. De ADR komt eerst, zodat schrijver en lezer tegen dezelfde tekst gebouwd worden en niet tegen elkaars code.

Het moet een eigen bestand met een eigen lezer zijn. `configs/producer_freshness.yaml` is geen optie: de scanner kent alleen de typen `directory`, `sqlite`, `gate_obligations` en `newest` (`scripts/lib/producer_freshness.py::_SCANNERS`), en elk type leidt versheid af uit een mtime of een tabel. Versheid uit de mtime van het watermerk of van het heartbeatbestand zelf is verboden: een `touch`, een restore of een back-up verschuift die tijd zonder dat de processor werkt.

## Besluit

**De receipt processor schrijft een heartbeatbestand. Een deterministische lezer (`scripts/lib/processor_liveness.py`) leidt daaruit een van vijf toestanden af. Het overall-oordeel van `system_health` neemt die toestand mee met de orde `fail` > `unknown` > `ok`.** Er zit geen model in dit pad: het is een schema, een klok en een procescontrole.

### 1. Het bestand en het schema

`$VNX_STATE_DIR/receipt_processor_heartbeat.json`, atomisch herschreven (tmp-bestand in dezelfde map, dan `mv`):

```json
{"schema": 1, "pid": 0, "cycle": 0, "started_at": "Z", "updated_at": "Z", "last_progress_at": "Z",
 "last_cycle_seconds": 0.0, "watermark_hash": "",
 "converter": {"rejected": 0, "malformed": 0, "errors": 0}}
```

| Veld | Betekenis |
|---|---|
| `schema` | Altijd `1`. Een andere waarde maakt de lezer `unknown`. |
| `pid` | PID van de processor-lus die schrijft. |
| `cycle` | Teller van de poll-cyclus, 1-gebaseerd. |
| `started_at` | Starttijd van de processor, UTC (ISO-8601 met `Z`). |
| `updated_at` | Tijd van de laatste schrijfactie. Schuift bij elke schrijfactie. |
| `last_progress_at` | Tijd waarop voor het laatst een rapport is ingelezen. Schuift alleen bij een ingelezen rapport. |
| `last_cycle_seconds` | Duur van de vorige volledige cyclus in seconden. |
| `watermark_hash` | Hash van de inhoud van het watermerk. Alleen ter vergelijking, nooit als versheidsbron. |
| `converter` | Tellers van de laatste converter-scan: `rejected`, `malformed`, `errors`. |

### 2. De twee schrijfmomenten

De schrijver zit in `scripts/receipt_processor.sh` en schrijft op twee plekken:

1. **Bovenaan elke `_poll_new_reports_cycle`.**
2. **Direct na `_run_receipt_converter_scan`.** Die scan draait elke zesde cyclus (ongeveer elke 30 s bij de standaard `POLL_INTERVAL` van 5 s).

`last_progress_at` beweegt alleen mee wanneer in die cyclus een rapport is ingelezen. Een processor die elke 5 s schrijft maar nooit iets verwerkt, houdt `updated_at` vers en laat `last_progress_at` oud staan. Dat verschil is bewust zichtbaar in het bestand, maar de lezer beslist erop niet: een stille map is geen storing.

**Een schrijffout is geen cyclusfout.** De schrijver logt de fout en de cyclus loopt door. De processor mag niet falen doordat zijn eigen levensteken niet geschreven kan worden. De lezer ziet dan een bestand dat veroudert en meldt `stalled` of `dead`, nooit stil `ok`.

### 3. De vijf toestanden, in beslisvolgorde

De lezer beslist in precies deze volgorde. De eerste regel die past wint.

| # | Toestand | Wanneer | Telt als |
|---|---|---|---|
| 1 | `absent` | het bestand bestaat niet | `unknown` |
| 2 | `unknown` | onleesbaar, geen geldige JSON, `schema` onbekend, `pid` ontbreekt of is geen positief geheel getal, een tijdveld niet te parsen, of `updated_at` meer dan 30 s in de toekomst (klokscheefstand) | `unknown` |
| 3 | `dead` | `pid` bestaat niet meer (`os.kill(pid, 0)` geeft `ProcessLookupError`), ongeacht de leeftijd van `updated_at` | `fail` |
| 4 | `stalled` | het proces bestaat en `now - updated_at > WINDOW_SECONDS` | `fail` |
| 5 | `fresh` | het proces bestaat en `now - updated_at <= WINDOW_SECONDS` | `ok` |

- Een `PermissionError` op `os.kill(pid, 0)` betekent dat het proces bestaat onder een andere eigenaar. Dat telt als "bestaat" en gaat door naar de versheidstoets. Elke andere `OSError` is een niet te beantwoorden vraag en geeft `unknown`.
- `dead` gaat vóór `stalled`: een bestand met een verse `updated_at` en een dode PID blijft `dead`. Een PID kan binnen het venster zijn gestorven.
- Onbekend of ontbrekend is nooit `fresh`. De lezer beslist alleen op positief bewijs: een leesbaar bestand, een geldig schema, geparste tijden, een levend proces en een leeftijd binnen het venster. Alles wat daar niet aan voldoet blijft `unknown`, `stalled` of `dead` en noemt waarom in het resultaat.
- Nooit versheid afleiden uit de mtime van het watermerk of van het heartbeatbestand.

### 4. De aggregatie

`processor_liveness` voedt `_combine_liveness_overall` in `scripts/build_t0_state.py` als een **eigen component**, niet via `configs/producer_freshness.yaml`. Het overall-oordeel is het slechtste van de componenten met de orde `fail` > `unknown` > `ok`:

- elke component `fail` geeft overall `fail`;
- anders geeft elke component `unknown` overall `unknown`;
- alleen als alle componenten `ok` zijn, is het overall `ok`.

`system_health.status` wordt `degraded` bij overall `fail` of `unknown`. Een bestaande sleutel van `system_health` verandert niet van betekenis; er komt er één bij (`processor_liveness`, met `state`, de leeftijd van `updated_at` en de reden).

De huidige `_combine_liveness_overall` geeft bij een mengsel van `ok` en `unknown` `ok`. Voor de processor is dat te mild: "niet gemeten" mag geen "gezond" worden doordat een ander component wel `ok` is. PR 1 past de combinatie daarom aan op de orde hierboven en legt die vast met een test in `tests/test_system_health_monotonic.py` (`fail` > `unknown` > `ok`), inclusief de uitkomst voor de bestaande daemon- en launchd-componenten.

### 5. Het venster

`WINDOW_SECONDS = 942`, één constante in `scripts/lib/processor_liveness.py`.

**Dit is een meting, geen aanname.** De poll-lus schrijft twee keer per cyclus: aan het begin en aan het eind. Het grootste gat tussen twee schrijfacties is dus één cyclus. De regel was: ligt de p99 van de cyclusduur boven 30 s, dan wordt het venster drie keer de gemeten p99, met de meting erbij.

De meting op de draaiende processor (PID 75922, gestart 2026-11-08T19:57:39Z), van 2026-11-08 21:29Z tot 2026-11-09 08:51Z. Methode: de tijdstempels van een regel die de processor precies één keer per cyclus logt in `receipt_processor_supervisor.log` (`Already processed: ...`). Uitkomst: n = 171 cycli, min 221 s, p50 231 s, p90 274 s, p99 314 s, max 350 s. Alle 171 liggen boven 90 s.

Het heartbeatbestand, elke 5 s bemonsterd op 2026-11-09: `updated_at` 08:42:59Z (cyclus 189), 08:47:02Z (189), 08:47:07Z (190), 08:51:28Z (190), 08:51:34Z (191). De langste gaten tussen twee schrijfacties waren 243 s en 261 s. `last_cycle_seconds` las 247,69, 243,159 en 261,382.

De vorige processor (PID 2021, 2026-11-06 23:48Z tot 2026-11-08 19:56Z, andere marker per cyclus): n = 895, p50 153 s, p99 441 s, max 661 s. Ook die lag overal boven 90 s.

Met 90 s noemde de lezer een gezonde processor `stalled` voor het grootste deel van elke cyclus. Drie keer 314 s is 942 s. Een latere meting vervangt dit getal op dezelfde manier, in een eigen PR en met de meting erbij.

### 6. Wat "zichtbaar" betekent

`t0_state.json` wordt gebouwd door `scripts/build_t0_state.py` (SessionStart-hook of handmatig). "Zichtbaar binnen het venster" (942 s, ruim 15 minuten) betekent daarom: **een bouw van de state die meer dan `WINDOW_SECONDS` na de laatste heartbeat start, meldt `stalled` of `dead`.** Het is geen push en geen alarm. Er draait niets dat tussen twee bouwen kijkt.

De meting voor "klaar" roept de bouw daarom zelf aan op vaste tijden: na een `SIGSTOP` op de PID uit het heartbeatbestand (gecontroleerd met `ps -o stat= -p <pid>`, verwacht `T`) staat op T+975 s (het venster plus 30 s marge) de toestand op `stalled` en `system_health.status` op `degraded`; na `SIGCONT` is hij binnen 30 s weer `fresh`.

### 7. Mergevolgorde: schrijver vóór lezer

Mergen in deze volgorde: PR 0 (deze ADR), PR 4, PR 3, **PR 2 (schrijver), dan PR 1 (lezer)**.

Een heartbeatbestand zonder lezer is onschadelijk. Een lezer zonder schrijver meldt `absent`, en dat zou main tussen twee merges op `degraded` zetten. Bouwen mag parallel; PR 2 start zodra deze ADR ligt.

### 8. Live gaan

De receipt-processor van `vnx-dev` draait onder de pinned launcher uit `~/.vnx-system/versions/v1.8.0` (plist `com.vnx.receipt-processor.vnx-dev`, zie ADR-051). De schrijver is dus **pas live na de release van 1.9 en `scripts/launchd/reload_plist.sh com.vnx.receipt-processor vnx-dev`**. De lezer draait uit de checkout en is wel meteen live na zijn merge.

### 9. Het `absent`-venster

Tussen de merge van PR 1 en die herstart meldt `t0_state` `processor_liveness: absent` en daarmee `system_health: degraded`. Dat is een juiste melding: liveness is in dat venster niet gemeten. Het venster staat met begin- en einddatum in de handoff, zodat niemand het als storing leest. Wil de T0 het venster vermijden, dan merget PR 1 pas na de uitrol van 1.9; die keuze staat in de G2-handoff.

### 10. Terugdraaien

Elke PR is een squash-commit, dus `git revert` is de route. De volgorde is **lezer eerst, dan schrijver**.

- `git revert` van PR 1 haalt de lezer weg. `system_health` valt terug op de bestaande componenten.
- `git revert` van PR 2 haalt de schrijver weg. De lezer meldt dan `absent` en `degraded`, nooit stil `ok`.

Een schrijver terugdraaien terwijl de lezer blijft staan is dus veilig, maar het zet main op `degraded` tot ook de lezer weg is. Daarom staat de lezer eerst.

## Gevolgen

- De processor-liveness is falsifieerbaar: een `SIGSTOP` op de PID uit het bestand moet binnen één state-bouw na het venster `stalled` geven, en dat is te meten (zie §6).
- Een vastgelopen maar levende processor leest niet meer "running". Een dode processor leest `dead`, ook binnen het venster.
- `system_health.status` kan voor het eerst `degraded` worden om een reden die geen daemon of launchd-job is. Een lezer van `system_health` die alleen die twee componenten kent, ziet een nieuwe sleutel en moet de bestaande niet anders lezen.
- Het contract kost de processor twee kleine atomische schrijfacties per cyclus. Een schrijffout stopt de processor niet.
- `last_progress_at` en `watermark_hash` worden geschreven maar beslissen niets in deze versie. Ze staan er zodat een latere track "leeft maar verwerkt niets" kan meten zonder het schema te wijzigen (`schema` blijft dan 1, tenzij een veld van betekenis verandert).
- Het venster van 942 s is gemeten: drie keer de p99 van 314 s over 171 cycli (§5). Een stilgevallen processor wordt daardoor pas na ruim 15 minuten `stalled`. Dat is de prijs van een venster dat een gezonde cyclus van 350 s niet vals afkeurt.
- Buiten dit ADR: de PR-koppeling op het eind-receipt (track `audit-spoor-volledig`), de 32 foutbestanden van de converter en de 18 huidige afwijzingen (operatorbeoordeling, datawijziging), en het aansluiten van `unlinked_gate_evidence` op `system_health`.
