# ADR-043 - Het kostengrootboek: één regel per run, met een bron bij elk bedrag

**Status:** Accepted (dispatch 20261020-kp-k2a1-grootboek, track kosten-per-model-in-het-grootboek, deliverable K2a, PR K2a-1). §6 is verplaatst naar de tweede K2a-PR (uitspraak `cap-pr2141-73c685f656862925ef20ef81d2a09090b9b08337`, dispatch 20261021-kp-k2a1-r6).
**Date:** 2026-10-20
**Decided by:** operator via het besluit van 19-10 ("iedere cent die wordt uitgegeven moet worden getrackt") en de plan-gate van 19-10 (codex REVISE met zeven punten, tiebreaker START met één verplichte wijziging), uitgewerkt door de bouwer van K2a-1.
**Cross-refs:** plan `claudedocs/specs/kosten-per-model-in-het-grootboek/plan.md` (secties "Het grootboek", "De prijsbron", "Als schrijven mislukt", "ADR"); ADR-005 (eerst schrijven, dan de toestand wijzigen); ADR-007 (`project_id` in de natuurlijke sleutel); ADR-036 (modelidentiteit uit het register); ADR-040 (prijsversies en `price_run`).

## Context

Modellen zijn niet op kosten te vergelijken, en het dagplafond voor betaalde lanes ziet het grootste deel van de uitgaven niet. Gemeten op 19-10:

- `provider_costs.ndjson` boekt voor de DeepSeek-bouw van R1b 102.385 input- en 64.922 output-tokens. Het transcript van dezelfde run draagt daarnaast 7.218.944 cache-hits. Die staan nergens.
- De geslaagde refire van P1c heeft geen regel. Alleen de geweigerde eerste poging staat er, met `0/0` en `cost_usd_estimate: null`.
- Het DeepSeek-saldo zakte in een venster $4,97. Het bestand boekt in dat venster $1,05, en twee regels daarvan zonder bedrag.
- `paid_lane_budget` telt `cost_usd` uit de receipts en slaat `null` over. De DeepSeek-bouwlane boekt daar `null`.

Een dispatch heeft één receipt, maar doet vaak meerdere betaalde aanroepen: bouwer, stoelen, tiebreaker, tagger. De receipt is dus de verkeerde eenheid voor kosten. Het oude regelformaat van `emit_provider_cost` kent geen run, geen correctie en geen onderscheid tussen onbekend en nul.

## Besluit

### 1. Eén regel per run, in één bestand

`<centrale store>/events/provider_costs.ndjson` is het kostengrootboek. `provider_costs._resolve_costs_path` blijft de enige resolver. Er komt geen tweede bestand en geen centrale DB-tabel.

De rekeneenheid is de run: één start van één model voor één doel. Een dispatchpoging, een poortrun, een plan-gate-stoel, een panelpoot, een intelligence-aanroep. Een refire is een nieuwe poging en dus een nieuwe run. Berichten zijn nooit een regel; een run telt zijn berichten op.

Elke regel heeft de vorm `provider_cost_run/1` met precies deze velden:

| Veld | Betekenis |
|---|---|
| `schema` | `provider_cost_run/1` |
| `record_id` | sha256 (32 tekens) over de regel zonder dit veld |
| `run_id` | `<project_id>:<producer>:<dispatch_id\|gate_run_id>:<attempt>`, poging vanaf 1 |
| `project_id` | gelijk aan het projectdeel van `run_id` (ADR-007) |
| `kind` | `charge`, of `correction` met `supersedes` (een `record_id`) en `reason` |
| `occurred_from`, `occurred_to`, `recorded_at` | begin en eind van de run, en het schrijfmoment |
| `role` | `builder`, `review`, `plan_gate`, `tiebreaker`, `panel`, `intelligence` |
| `provider`, `model` | van de runner; `model` is de registersleutel (ADR-036) |
| `tokens` | `input_uncached`, `cache_hit`, `cache_write_5m`, `cache_write_1h`, `output`: een geheel getal of `null` |
| `usage_completeness` | `complete`, `partial:<velden>` of `unknown:<reden>` |
| `segments` | optioneel, per prijsvenster de tokens met `peak` of `ts` |
| `cost_usd`, `cost_source` | `provider_reported`, `price_table@<prijsversie>` of `unavailable:<reden>` |
| `billing` | `metered` (eigen sleutel, echt geld) of `subscription` met `api_equivalent_usd` |
| `backfill` | optioneel `true`, voor regels uit de backfill |

`null` is onbekend en `0` is bevestigd nul. Een abonnementsrun boekt `cost_usd: 0` (marginaal) en zijn lijstprijs in `api_equivalent_usd`. Een geweigerde spawn is een regel met nul tokens, `complete`, `cost_usd: 0` en `unavailable:spawn_refused`.

### 2. De schrijver weigert, en schrijft dan niets

`cost_ledger.record_run(RunCost)` weigert met een eigen fouttype:

- een bedrag zonder `cost_source` (`CostWithoutSource`);
- `unavailable:*` met een bedrag dat geen nul is (`UnavailableWithAmount`);
- een `correction` zonder `supersedes` of `reason` (`CorrectionWithoutSupersedes`);
- `complete` terwijl een tokenveld `null` is (`IncompleteUsageMarkedComplete`);
- een `run_id` van een ander project (`RunIdProjectMismatch`);
- een provider of model dat het register niet kent (`UnknownModel`). De resolver is strenger dan die van de prijsopzoeking: een exacte sleutel, de sleutel na het bekende voorvoegsel van de sectie, de ene `cli_model_arg` die gelijk is, of een gedateerd snapshot-achtervoegsel. `sonnet-6` wordt geen `sonnet`.

Elk ander misvormd veld is `InvalidRecord`. In alle gevallen staat er daarna niets in het grootboek.

Geeft de aanroeper tokens zonder bedrag, dan rekent de schrijver via `price_run` (ADR-040). Zonder segmenten is dat het hoogste tarief, nooit een onderschatting. Is een telling onbekend die de prijs nodig heeft, dan wordt de bron `unavailable:usage_unknown` en het bedrag `null`, geen nul. De prijs heeft altijd `input_uncached` en `output` nodig, en een cache-telling alleen als de laatste prijsversie van het model daar een tarief voor heeft. Een onbekende telling wordt nooit als 0 aan `price_run` gegeven. Een telling die echt 0 is, blijft een berekende nul.

ADR-005 geldt: de regel staat in het grootboek wanneer `record_run` terugkeert. De aanroeper wijzigt zijn toestand pas daarna. De schrijver neemt dezelfde `flock` op het bestand als `emit_provider_cost` en geeft hem vrij door het bestand te sluiten.

Mislukt de append (openen, locken, schrijven of sluiten), dan geeft `record_run` `CostLedgerWriteError`, een `OSError` met de errno van de oorzaak. Niets bewaart de regel elders. De aanroep faalt en de aanroeper wijzigt niets. Een write of close die halverwege faalde kan de regel op schijf hebben gezet: een hele regel telt dan als de regel van de run, een afgebroken regel als één onleesbare regel. Dit geldt tot §6 landt; daarna neemt de spool dit pad over.

### 3. Correctie in plaats van wijziging

Het bestand blijft append-only. Een correctie is een nieuwe regel van dezelfde run. Per `run_id` telt alleen de laatste regel. Elke lezer telt via één functie, `cost_ledger.aggregate_runs`: receipts, rapport en plafond. Die functie geeft per run de geldende regel, de sommen van tokens en bedrag, de runs zonder prijs apart en het aantal regels in het oude formaat (`legacy_lines`, niet opgeteld). Ze leest het grootboek onder een gedeelde `flock`. Een ontbrekend grootboek is leeg; een grootboek dat niet te lezen is, is een fout en nooit een leeg totaal. Het aantal runs in de spool komt erbij met §6.

Een backfillregel voor een run die al een regel heeft, is een `correction`. Een run zonder regel krijgt een `charge` met `backfill: true`. Zo telt een run nooit twee keer.

### 4. De prijs komt alleen uit het register

Er is één prijsbron: de prijsversies in `wave7_models.yaml`, gelezen via `price_run` (ADR-040). De bron van een berekend bedrag noemt de versie: `price_table@<datum van de versie>`. Een prijs die de provider zelf rapporteert is `provider_reported`.

### 5. De bron is verplicht en `unavailable` is nooit nul

Een bedrag zonder bron bestaat niet. Een run met `unavailable:*` telt in geen enkele som. De aggregatie noemt hem apart, zodat een lezer ziet hoeveel runs hij mist in plaats van een te laag totaal te tonen.

### 6. Eén lockbestand, met de spool als terugval

> **Gefaseerd.** Deze paragraaf landt in drie PR's (plan `claudedocs/specs/kosten-per-model-in-het-grootboek/k2a3-spool-een-lock.md`, plan-gate 21-10). K2a-3a: het lockbestand, het schrijfpad met de spool als terugval en de aggregatie in één scope. K2a-3b: de replay en `vnx costs replay`. K2a-3c: de tik in de prelude, het beacon `cost-ledger` en het lockbestand in `vnx doctor` en de footprint-probe. Het eerste ontwerp met twee locks (grootboek-`flock` plus spool-lock) haalde #2141 niet. Vijf van de zes codex-blockers zaten in de momenten waarop het ene lock vrij was en het andere niet. Dat ontwerp en die bevindingen staan in `claudedocs/specs/kosten-per-model-in-het-grootboek/k2a-spool-replay-uitgesteld.md`.

**Eén lock.** `events/provider_costs.lock` staat naast het grootboek (`provider_costs.ledger_lock`). Het wordt aangemaakt met `O_CREAT` en nooit verwijderd. Elke actor neemt het als eerste lock:

| Actor | Lockbestand | Daaronder |
|---|---|---|
| `record_run`, `record_next_attempt` | `LOCK_EX` | grootboek openen en `flock`; grens herstellen; wacht de run al in de spool, dan erachter in de spool; anders append en fsync |
| `emit_provider_cost` (oud formaat) | `LOCK_EX` | grootboek-`flock`, append zoals voorheen |
| replay (K2a-3b) | `LOCK_EX`, per spoolbestand | grens; ontbrekende regels schrijven, fsync, teruglezen, pas daarna verwijderen |
| `aggregate_runs` | `LOCK_SH` | grootboek lezen en spool tellen in deze ene scope; herstelt niets |

De `flock` op het grootboek zelf blijft, genest onder het lockbestand. Hij beschermt de bytes tegen een proces van een oudere fabric-versie dat het lockbestand niet kent. Zo'n proces schrijft alleen het oude formaat, en dat telt in geen som. De volgorde is altijd eerst het lockbestand en dan het grootboek. Oude actoren nemen het lockbestand nooit, dus er is geen cyclus.

**Het faalcontract.** Wat de schrijver doet, volgt uit wat er op schijf kan staan:

| Wat faalt | Kan de regel in het grootboek staan? | Wat de schrijver doet | `landed` |
|---|---|---|---|
| openen, `flock` of de grens | nee | spool | `spool` |
| `write` (ook een halve) | een deel | eigen regel afsluiten als dat kan, dan spool | `spool` |
| `fsync` of `close` na een volledige `write` | ja | ook naar de spool | `ledger+spool` |

Het teruggegeven record draagt `landed` als attribuut (`WrittenRecord`). De regel zelf krijgt dat veld niet. De spool is `events/provider_costs.spool/<run_id>.json`: een JSON-lijst van regels, atomair geschreven en gevolgd door een fsync van de map. Een regel die in het grootboek én in de spool staat, telt één keer. De aggregatie neemt per `run_id` de laatste regel en de replay ziet aan `record_id` dat hij al geland is.

Wacht er een regel van een run in de spool, dan gaat een latere regel van dezelfde run er ook in. Dat geldt ook als het grootboek werkt. Zo komt een correctie nooit vóór de regel die ze vervangt. `record_next_attempt` telt de pogingen in het grootboek en in de spool onder hetzelfde exclusieve lock als de schrijfactie. Een refire terwijl poging 1 in de spool wacht, wordt dus poging 2.

`CostLedgerWriteError` heeft één betekenis: ook de spool faalde, of het lockbestand was niet te nemen. De uitkomst is dan onbekend, want de regel kan in het grootboek staan. De aanroeper faalt, wijzigt niets en boekt geen nieuwe poging. `record_next_attempt` zou er anders een tweede run van maken. De aanroeper meldt de fout (op de envelope het OI-1179-pad) en de replay of een correctie lost het op. Kan de schrijver de spool niet lezen om te zien of de run er wacht, of kan hij de pogingen niet tellen, dan schrijft hij niets en geeft hij dezelfde fout.

**Grensherstel.** Alleen een houder van `LOCK_EX` sluit een laatste regel zonder `\n` af. Een lezer onder `LOCK_SH` verandert niets. Hij telt een gescheurde staart als één onleesbare regel en meldt `torn_tail`. Een 1.7.2-schrijver herstelt geen grens en kan zijn regel aan een gescheurde staart plakken. Wat dan verloren gaat, is altijd een oude-formaatregel plus het fragment. De nieuwe regel staat volgens het faalcontract ook in de spool.

**De aggregatie.** `LedgerTotals` krijgt `spool_pending`, `spool_unreadable` en `torn_tail`. `incomplete` geeft de reden waarom een totaal niet volledig is, of niets. De spoolmap wordt opgesomd met `os.scandir` en alleen een ontbrekende map is een lege spool. Een map die niet te lezen is, geeft `spool_unreadable` en nooit een volledig totaal (blocker 6 uit #2141). Een regel in de spool ontbreekt in elke som tot de replay hem terugzet.

**Na K2a-3a.** De replay (K2a-3b) neemt het lock met `LOCK_NB` en een wachttijd, heeft een batch- en een tijdsgrens per aanroep en leest terug vóór hij verwijdert. De tik (K2a-3c) start hem met een harde timeout van 20 seconden en geeft altijd 0 terug. De tik hangt niet aan `VNX_SUPERVISOR_MODE`: de spool is het herstelpad van het grootboek en geen supervisie. Het subsysteem `cost-ledger` meldt degraded via het bestaande beaconmechanisme (`<data_dir>/health/cost-ledger.json`) zolang de spool niet leeg of niet te lezen is. Tussen K2a-3a en K2a-3b kan een spool ontstaan zonder replay. Dat gebeurt alleen als het grootboek faalt. De aggregatie meldt het totaal dan als onvolledig.

### 7. Kalibratie is een eigen recordsoort

Een saldometing of een lijst generations van een provider is geen uitgave. Die krijgt een eigen bestand, `events/provider_account_observations.ndjson`, met recordsoort `account_observation`. Geen uitgave- of tokensom leest dat bestand. Dat is K5b en wordt hier niet gebouwd.

### 8. Het plafond schakelt pas om na de meetpoort

`paid_lane_budget` blijft `cost_usd` uit de receipts lezen. Het plafond leest het grootboek pas als de meetpoort het toelaat: K2a, K2b, K3, K4a en K4b uitgerold, en zeven dagen geen `metered`-regel met `unavailable:*` behalve de gedocumenteerde kimi-uitzondering. Dat is K5c. Daarna telt een `unavailable`-regel op een `metered` lane niet als nul: het plafond meldt hem en de deur waarschuwt, zonder te weigeren.

### 9. Geen centrale DB-tabel

Het grootboek is een NDJSON-bestand per project-store. Komt er ooit een projectie in een database, dan krijgt die `project_id` in de natuurlijke sleutel (ADR-007).

## Gevolgen

- Een lezer die het grootboek optelt, ziet wat hij mist: runs zonder prijs, regels in het oude formaat en runs in de spool (`spool_pending`, `incomplete`). Een te laag totaal zonder melding kan niet meer.
- Een mislukte append is geen fout van de aanroep meer: de regel gaat naar de spool. `CostLedgerWriteError` betekent dat ook de spool faalde en dat de uitkomst onbekend is. Een lane op `record_run` of `record_next_attempt` laat die fout doorkomen, vangt hem niet stil af en boekt bij die fout geen nieuwe poging. De envelope, de subprocess-lane en `provider_dispatch` doen dat sinds K2a-3a.
- `record_next_attempt` (K2a-2) telt de pogingen in het grootboek en in de spool, onder hetzelfde exclusieve lock als de schrijfactie. Een refire is daardoor altijd een nieuwe poging, ook als een eerdere in de spool wacht.
- Een receipt over meerdere pogingen waarvan er één in de spool wacht, krijgt geen bedrag maar een `unavailable:`-bron. De aggregatie ziet die run pas na de replay. Een receipt over één poging neemt het bedrag van het teruggegeven record, ook als dat in de spool staat.
- Elke schrijver neemt het lockbestand, ook als het grootboek gewoon werkt. Is het niet te nemen, dan faalt de aanroep. Zonder dat lock is de volgorde van de run niet te garanderen en is ook de spool niet te gebruiken.
- Elke append krijgt een fsync. Dat kost één fsync per run en maakt het onderscheid tussen een halve regel en een hele regel die niet bevestigd is.
- Het oude formaat blijft bestaan tot de lanes over zijn. `emit_provider_cost` schrijft dezelfde regel als voorheen en neemt nu eerst het lockbestand. De harness-lanes gingen in K2a-2 over op `record_run`; codex en kimi volgen in K2b.
- Het lockbestand is een nieuw bestand in elke store. K2a-3c maakt het bekend voor `vnx doctor` en de footprint-probe.
- De identiteit is strenger dan de prijsopzoeking. Een model dat het register niet kent, levert geen regel op maar een fout. Een lane die zo'n model draait, moet eerst in het register. Dat is de bedoeling: een geraden identiteit is precies de fout van OI-2186.
- Een run zonder segmenten op een lane met piek en dal wordt tegen het piektarief geboekt. Dat is een bovengrens. K2a-2 levert de segmenten uit het transcript.
- Met K2a-3b: de replay leest het hele grootboek twee keer, vooraf om te zien wat al geland is en achteraf om terug te lezen wat hij schreef. Dat gebeurt alleen als er iets in de spool staat.
- Met K2a-3c: de prelude start de replay als subproces met een harde timeout van 20 seconden, ook in legacy-modus. Met een lege spool schrijft dat proces niets.
