# ADR-063 - De dagelijkse doorloopmeting: tien metriek-ids, onbekend is nooit 0, een afgeronde dag en een structurele goedkeuringspoort

**Status:** Voorgesteld 09-11-2026 (T0, track `doorloop-en-effectiviteit-dagelijks`, rij PR-0); aangenomen zodra de T0 PR-0 merget.
**Datum:** 2026-11-09
**Besloten door:** de operator voor besluiten 1 tot en met 4 van de 1.9-sprint (07-11-2026, besluit 2 en 3 hieronder letterlijk); het plan `claudedocs/2026-11-07-naar-1-9/plannen/doorloop-en-effectiviteit-dagelijks.md` (revisie 2, 08-11-2026, plan-gate van 08-11 en de tiebreaker-voorwaarden) voor de besluiten 20 tot en met 29. ADR-063 was vrij op `origin/main` `1a7776a40` (09-11-2026): de hoogste is ADR-061 en ADR-062 is gereserveerd voor `rekenslot-per-machine`.
**Cross-refs:** ADR-007 (samengestelde sleutel over `project_id`; hier niet van toepassing, zie §9), ADR-046 (één poortmotor), ADR-051 (een geplande taak volgt de pin), ADR-056 (zelfstandigheid als meetbaar begrip), ADR-061 (deur-afgedwongen: zijn D7-lezer vervangt op termijn de grootboekbron en zijn override-administratie de wachttijd).

De operator heeft op 07-11-2026 besloten, onder meer:

> **2.** zelfstandigheid-meten: T0 promoveert leveringen van een cleared plan; leren en dreaming blijven voorstellen.
>
> **3.** Learning-tracks ontparkeren zodra ledger-is-falsifiable gemerged is; corpus-quarantaine omkeerbaar (verplaatsen, niet wissen).

Dit ADR legt vast waartegen de veertien code-rijen PR-1 tot en met PR-14 bouwen: welke tien getallen de dagelijkse job meet, wat "onbekend" betekent, wanneer een dag af is, hoe dekking wordt bewezen, hoe een voorstel en een oordeel werken, wie goedkeurt, wat "klaarstaand werk" niet is, dat de job opt-in is en dat er geen schemawijziging komt. Het bevat geen code; alles erin is een regel over data. Er zit in het hele dagpad geen model: de enige open natuurlijke taal is het bedenken van de interventie, en dat is de handeling van de operator bij de goedkeuring. Alle andere delen zijn deterministisch — een mapping van metriek naar drempel, een dagnummer, een bestandsvorm — en daarom testbaar en reproduceerbaar.

## Context

Er is geen dagelijkse meting: `scripts/daily_metrics.py` en `scripts/lib/daily_metrics/` bestaan niet, er zijn nul `daily-metrics`-launchd-labels, en de nachtelijke drager (`com.vnx.nightly-intelligence-pipeline`) draait niet. De enige lezer van gemergede PR's is `_get_merged_today` (`scripts/lib/pr_queue_state.py:107`) met een vaste `--limit 30` en zonder `createdAt`, waardoor de telling op drukke dagen stil fout is: 68 merges op 10-27 en 50 op 11-06 tegen een limiet van 30. Een grotere vaste limiet bewijst geen dekking: `gh run list --created` over drie dagen gaf exact 1000 rijen (afgekapt). Een dagcijfer is de enige eenheid die de vensterafhankelijke kopgetallen niet vervormt: de mediaan over de laatste 45 merges was 0,402 u, over 300 merges 1,008 u, en per dag 0,41 u tot 93,85 u.

## Besluit

### 1. De tien metriek-ids

Tien ids, niet meer en niet minder. "5 merges per uur" wordt geen id: `merges` is de dagtelling en `details.hour_histogram` geeft de piek.

| id | eenheid | bronmodule | vorm |
|---|---|---|---|
| metric: merges | aantal gemergede PR's per UTC-dag | `scripts/lib/daily_metrics/sources/github_merges.py` | scalair |
| metric: pr_open_to_merge_median_hours | uren, mediaan van `mergedAt - createdAt` | `scripts/lib/daily_metrics/sources/github_merges.py` | scalair |
| metric: pr_open_to_merge_p75_hours | uren, p75 van `mergedAt - createdAt` | `scripts/lib/daily_metrics/sources/github_merges.py` | scalair |
| metric: rounds_per_pr | rondes per PR, mediaan over `opened`-regels | `scripts/lib/daily_metrics/sources/gates.py` | scalair |
| metric: ci_duration_seconds_median | seconden, mediaan van de PR-wandtijd over alle runs op de kop | `scripts/lib/daily_metrics/sources/ci.py` | scalair |
| metric: gate_duration_seconds_by_seat | seconden per stoel (mediaan), één record per sleutel | `scripts/lib/daily_metrics/sources/gates.py` | gekeyd, één volwaardig record per stoel |
| metric: silence_windows_over_30m | aantal gaten > 30 minuten dat op de dag sluit | `scripts/lib/daily_metrics/sources/silence.py` | scalair |
| metric: operator_wait_hours | uren wachttijd op de operator | `scripts/lib/daily_metrics/sources/approvals.py` | scalair |
| metric: cost_per_merged_pr_usd | USD per gemergede PR | `scripts/lib/daily_metrics/sources/costs.py` | scalair |
| metric: merges_without_receipt_count | aantal merges zonder receipt | `scripts/lib/daily_metrics/sources/ledger.py` | scalair |

Een scalair record is `{value: float|None, source, baseline_7d, unknown_reason, sample_size, details}`. `gate_duration_seconds_by_seat` is gekeyd (besluit 23 van het plan): `KeyedMetric(source, entries: dict[str, ScalarMetric])`, elke entry een volwaardig record met eigen `value`, `sample_size`, `source`, `baseline_7d` en `unknown_reason`, `p75` in `details`. Een gekeyde metriek heeft geen eigen `value`, want een mediaan over stoelen met elk een andere orde van grootte (ci_gate 0,8 s tegen deepseek_gate 332 s) zegt niets. De sleutel is de stoelnaam met patroon `^[a-z0-9]+_(gate|review)$`; elk ander bestand telt in `details.unrecognised_files` en valt niet stil weg. Voorstellen en oordelen adresseren `(metriek, sleutel)`, geschreven als `<id>:<sleutel>`.

### 2. Ontbrekend is unknown, nooit 0

Een waarde is óf een getal met zijn `source` en `sample_size`, óf `None` met een `unknown_reason`. De constructor weigert een record zonder `source` en weigert `value is None` zonder `unknown_reason`; er is geen pad dat `None` naar `0.0` dwingt. Een ontbrekende bron wordt `unknown` met een reden (`source_import_failed`), nooit overgeslagen.

**Nooit een partiële som als totaal.** Kostenpublicatie is `None` met `owner_track_kosten-per-gemergede-pr_not_landed` en de numerieke dekking (96 tot 98% per dag, 1.086 `null`-regels van de 5.861) in `details.numeric_coverage`; `api_equivalent_usd` is een schaduwprijs en wordt niet gebruikt. De grootboekhelft is `None` met de vaste reden `ledger-is-falsifiable not merged` tot die track en de lezer van `deur-afgedwongen` D7 landen (besluit 24). Zeven `unknown`-dagen tellen als onvervuld, niet als "geen data dus goed". `report --check` heeft `--scan-zero-for-unknown`: een record `{value: 0, unknown_reason: "x"}` is exit 1. `unknown` leest nooit als `not_helped` en nooit als een geslaagde meting — dat is de fail-open die dit ADR verbiedt.

### 3. De afgeronde dag (besluit 20)

- Een UTC-dag `D` is **afgerond** als `now_utc >= D + 1 dag + SETTLE_HOURS` met `SETTLE_HOURS = 2`. Poortresultaten en receipts krijgen hun tijdstip na het feit; zonder die marge meet de job een dag die nog niet stil staat.
- De job draait om **04:10 lokaal**: 03:10 UTC in CET, 02:10 UTC in CEST, beide na de afronding.
- `collect` zonder `--day` vult de afgeronde dagen uit de laatste `CATCHUP_DAYS = 14` waarvoor geen `final` bestand bestaat, **oudste eerst**. De dag van vandaag wordt nooit geschreven. Een Mac die een dag uit stond haalt die dag bij de volgende run in; de eerste run vult veertien dagen terug.
- **`final` is onveranderlijk.** `--recollect DAY --reason "..."` schrijft `<DAY>.r<N>.json` (N vanaf 2) met `revision`, `collected_at`, `as_of` en `reason`; de lezer kiest de hoogste N en er wordt niets verwijderd. Een niet-`final` dag wordt bij elke run opnieuw geprobeerd: `incomplete` bij een voorbijgaande reden (`gh_failed`, `source_import_failed`, `truncated_fetch`, `ci_run_unreadable_for_<n>_prs`, `population_mismatch`), `final` bij alleen permanente redenen.
- **Versheid.** Een dag is vers als de laatste revisie `collected_at <= D + 1 dag + FRESH_HOURS` heeft met `FRESH_HOURS = 30` en `backfilled == false`. Een teruggevulde dag telt mee in de basislijn en niet in de 7-daagse versheid.
- **Gelijktijdigheid en klok.** Een `flock` op `state/daily_metrics/<project_id>/.lock`; een tweede run stopt met exit 0 en `skipped: locked`. Alle dagrekenen in UTC met een injecteerbare klok; de launchd-tijd is een uitvoermoment, geen dagdefinitie.

### 4. Dekking is bewezen, niet aangenomen (besluit 21)

- `fetch_merged(from_day, to_day)` leest met een datumbereik (`merged:<from>T00:00:00Z..<to>T23:59:59Z`) en `--limit 1000`. **Dekking is bewezen als het aantal rijen onder de limiet blijft** (`len < limit`). Bij gelijkheid halveert de lezer het bereik en leest beide helften (maximaal 6 niveaus diep); blijft een stuk op de limiet, dan is de uitkomst `truncated_fetch` en is elke metriek die erop leunt `None` met die reden. Een rij met `mergedAt` buiten het gevraagde bereik maakt de lezing `unknown`.
- **Eén populatie per run.** `collect` leest per dag één `MergedPopulation` (rijen, bereik, `fetched_at`, `truncated`) en vermeldt die in `details.population`. `merges`, de twee open-tot-merge-metrieken en `ci_duration_seconds_median` delen haar; wijkt het aantal rijen af van `merges.sample_size`, dan zijn beide `None` met `population_mismatch`.
- **Stiltevensters** lezen `[D - 14 dagen, nu]` met dezelfde lezer plus de open PR's. Een venster waarvan de voorgaande merge buiten de lookback valt, telt mee met `started_at: null`, `lookback_exhausted: true` en `duration_minutes_min`. Een venster dat aan het einde van de dag nog open is, staat in `details.open_window_at_day_end` en telt niet.
- **Afgewezen alternatief, met gemeten reden:** één bulk-lezing `gh run list --created <dag>` gaf over drie dagen precies 1000 rijen terug terwijl 11-07 alleen al 571 runs had. Een join daarop is stil onvolledig; de CI-bron leest daarom per PR-kop.

### 5. Voorstellen en oordelen (besluit 26)

`proposal_rules.py` bevat één tabel. Een kandidaat ontstaat alleen als **alle** voorwaarden gelden, alleen op de **nieuwste afgeronde dag**, en alleen als die dag `final` is; ontbreekt iets, dan staat de reden in `skipped`.

| sleutel | richting | trigger | minimum |
|---|---|---|---|
| `pr_open_to_merge_median_hours` | lager is beter | >= 1,5 x basislijn en verschil >= 0,5 u | n_dag >= 8, gepoolde basislijn n >= 30 |
| `pr_open_to_merge_p75_hours` | lager | >= 1,5 x en verschil >= 1,0 u | idem |
| `rounds_per_pr` | lager | >= basislijn + 0,5 en >= 1,25 x | n_dag >= 8 PR's |
| `ci_duration_seconds_median` | lager | >= 1,25 x en verschil >= 120 s | n_dag >= 8 PR's |
| `gate_duration_seconds_by_seat:<stoel>` | lager | >= 1,5 x en verschil >= 60 s | n >= 5 resultaten van die stoel |
| `silence_windows_over_30m` | lager | >= basislijn + 2 en >= 2 | basislijn uit >= 4 `final` dagen |
| `merges` | hoger is beter | <= 0,5 x basislijn | basislijn >= 10 |
| `operator_wait_hours`, `cost_per_merged_pr_usd`, `merges_without_receipt_count` | — | geen regel zolang de bron `unknown` is | — |

Het getal van een kandidaat is de richting-gecorrigeerde relatieve overschrijding `(waarde - basislijn) / max(basislijn, epsilon)`; rangschikking grootste eerst, gelijkspel alfabetisch op sleutel.

- **Hoogstens drie per metriekdag, retry-veilig.** `proposal_id = sha256(project_id | sleutel | metriekdag | regelversie)[:12]`. Het grootboek is `proposals.ndjson` (append-only, onder `flock`) met `proposed`, `approved` en `verdict`; `propose` telt de bestaande `proposed`-events voor de metriekdag, schrijft hoogstens `3 - bestaand` nieuwe ids en slaat bestaande over. Een herhaalde of afgebroken run kan de drie dus niet overschrijden en wijzigt geen bestaand event. Een inhaalslag over vijf dagen maakt geen vijftien voorstellen.
- **Hoogstens één open voorstel per sleutel**; na `undetermined` veertien dagen geen nieuw voorstel op die sleutel.
- **Evaluatie.** `approved_at` bepaalt de goedkeuringsdag `a` (UTC); het naverkort is `a+1` tot en met `a+7`, alle afgeronde dagen. `verdict` weigert zolang `a+7` niet afgerond is. De basislijn wordt **bij de goedkeuring bevroren** en staat in het `approved`-event.
- **Oordeel.** `helped` als de richting-gecorrigeerde verbetering >= 10% van de bevroren basislijn is en de absolute vloer uit de tabel haalt; `not_helped` als er genoeg data is en dat niet geldt (ook bij verslechtering); `undetermined` als de basislijn of de naverwaarde `unknown` is, minder dan 5 van de 7 naverkortdagen `final` zijn, de gepoolde n onder het minimum valt, of de metriekversie (`schema`) sinds de goedkeuring veranderde.

### 6. De goedkeuringspoort is structureel, geen beveiligingsgrens (besluit 27)

Een naamvlag kan "alleen Vincent" niet afdwingen: de enige operator-poort op main (`scripts/learning_loop.py:1740`) controleert alleen dat het token niet leeg is en `VNX_OPERATOR_ID` is een omgevingsvariabele. Wat wel kan, is de goedkeuring een handeling maken die een agent structureel niet kan uitvoeren:

1. `approve` weigert met exit 2 tenzij **stdin én stdout een TTY zijn**; een tool-aanroep, een headless worker en een launchd-job halen de poort dus niet.
2. `approve` weigert met exit 2 als `VNX_AGENT_ID` gezet is.
3. Het commando toont het voorstel (sleutel, waarde, basislijn, verwacht effect, meting achteraf) en eist dat de operator het volledige `proposal_id` terugtypt. Er is geen `--yes` en geen `--operator`.
4. `approved_by` is de vaste waarde `"operator"`; het event draagt `evidence = {tty: true, user, host, vnx_operator_id}`. `--action "<tekst>"` is verplicht.
5. `report --check` meldt elk `approved`-event zonder `evidence.tty == true` als schending (exit 1).

**Het restrisico, letterlijk (R6 uit het plan):** een actor onder dezelfde OS-gebruiker die in het terminalpaneel van de operator kan typen (bijvoorbeeld via `tmux send-keys`) kan goedkeuren. Een tweede drempel is dat de classifier typen in het eigen paneel weigert; dat is geen onderdeel van dit plan. Een harde garantie vraagt een credential die een agent niet kan lezen (keychain of `~/.claude`) en dat is operatorwerk (open vraag 13). Gevolg van een misbruik is een goedgekeurd voorstel dat zelf niets uitvoert. De T0 schrijft nooit `approved` en voert nooit een voorstel uit (besluit 2 van de operator); de job schrijft `approved` nooit zelf.

### 7. Scopeverkleining van "klaarstaand werk" (besluit 25)

Het klaarpunt vraagt vensters "met klaarstaand werk". Dat is historisch nergens vastgelegd en een dagelijkse snapshot reconstrueert geen venster. Geleverd wordt de proxy `open_pr_through`: PR's met een `createdAt` voor de start van het venster die aan het einde nog open staan of daarna zijn gemerged. Elke stiltemetriek draagt `details.work_ready_coverage = {measured: 0, total: N, reason: "no_historical_readiness_snapshot"}` en `work_ready: null` per venster. Een tekst die de proxy "klaarstaand werk" noemt is een fout (D5). Open vraag 14 blijft open tot de operator de scopeverkleining bevestigt; een realtime snapshot hoort bij de aparte stiltewacht (doel punt 6).

### 8. Opt-in per project (besluit 29)

Er komt **geen** verplichte launchd-familie: `REQUIRED_PER_PROJECT_FAMILIES` blijft ongemoeid, want `vnx doctor` geeft FAIL per ontbrekende familie en elk project dat niet meedoet zou dan rood staan. Opt-in is de handeling `vnx daily-metrics install --project-id <id>`; `vnx init` installeert de job niet en `uninstall` verwijdert hem. De beacon is **event-driven** (`HealthBeacon(state_dir, "daily_metrics", None)`, `expected_interval_seconds=None`), zodat geen enkel project hem verwacht; de job schrijft zelf `health: fail` bij drie afgeronde dagen zonder `final` bestand. Versheid komt uit de dagbestanden (`report --check`), niet uit `launchctl`: een geladen job zonder verse dagbestanden is `stale`. Het restrisico van een stil gestorven job staat in R1 en open vraag 8.

### 9. Geen schemawijziging

Er is **geen schemawijziging**: geen migratie, geen nieuwe tabel, geen prune, geen `PRAGMA`. De meting is een verzameling bestanden onder `resolve_central_data_dir(project_id)/state/daily_metrics/<project_id>/` met `project_id` in pad én record. ADR-007 (samengestelde sleutel over `project_id`) is dus niet van toepassing; wie later een centrale tabel toevoegt, citeert ADR-007 eerst en doet dat in een eigen rij, niet in deze track. `--recollect` schrijft een nieuwe revisie en wist niets; er is geen datavolume-risico en geen terugweg nodig.

## Consequenties

- **Positief:** één contract voor veertien rijen, met de invariant in het type (`None` plus reden) in plaats van in een renderer. Een ontbrekende bron wordt luid, geen stille nul.
- **Positief:** dekking is meetbaar (`len < limit`, `details.population`), zodat een daling door een andere leesmethode niet als doorloopwinst leest.
- **Positief:** de goedkeuringspoort blokkeert elke tool-aanroep, headless lane en launchd-job en laat bewijs achter; de T0 kan een voorstel niet goedkeuren of uitvoeren.
- **Negatief, en aanvaard:** de goedkeuringspoort is geen beveiligingsgrens (R6, §6). Wie onder dezelfde OS-gebruiker in het operatorpaneel kan typen, kan goedkeuren.
- **Negatief, en aanvaard:** kosten en de grootboekhelft blijven `unknown` met reden tot `kosten-per-gemergede-pr` en `ledger-is-falsifiable` (met de lezer van `deur-afgedwongen` D7) landen. Er is geen `coverage == 1.0` in deze sprint.
- **Negatief, en aanvaard:** "met klaarstaand werk" is als proxy geleverd (§7), en een stil gestorven job zonder `--check` blijft onopgemerkt tot iemand kijkt (R1, open vraag 8).
- **Volgorde:** niets van dit alles is actief voordat de rijen PR-1 tot en met PR-14 gemerged en gerealiseerd zijn; de operator installeert per project (besluit 8).

## Wat dit ADR niet doet

Het lost de realtime stiltewacht niet op (die blijft in `scripts/headless_trigger.py:307`), het reconstrueert `work_ready` niet, het levert de kostenjoin en de grootboeklezer niet, en het repareert of plant de nachtelijke pipeline niet. Het bevat geen code en geen model: de veertien rijen van het plan bouwen ertegen.
