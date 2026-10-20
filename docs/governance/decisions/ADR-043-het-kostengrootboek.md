# ADR-043 - Het kostengrootboek: één regel per run, met een bron bij elk bedrag

**Status:** Accepted (dispatch 20261020-kp-k2a1-grootboek, track kosten-per-model-in-het-grootboek, deliverable K2a, PR K2a-1)
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

Elk ander misvormd veld is `InvalidRecord`. In alle gevallen staat er daarna niets in het grootboek en niets in de spool.

Geeft de aanroeper tokens zonder bedrag, dan rekent de schrijver via `price_run` (ADR-040). Zonder segmenten is dat het hoogste tarief, nooit een onderschatting. Zijn alle tokens onbekend, dan wordt de bron `unavailable:usage_unknown` en het bedrag geen nul.

ADR-005 geldt: de regel staat in het grootboek of in de spool wanneer `record_run` terugkeert. De aanroeper wijzigt zijn toestand pas daarna. De schrijver neemt dezelfde `flock` op het bestand als `emit_provider_cost`.

### 3. Correctie in plaats van wijziging

Het bestand blijft append-only. Een correctie is een nieuwe regel van dezelfde run. Per `run_id` telt alleen de laatste regel. Elke lezer telt via één functie, `cost_ledger.aggregate_runs`: receipts, rapport en plafond. Die functie geeft per run de geldende regel, de sommen van tokens en bedrag, de runs zonder prijs apart, het aantal regels in het oude formaat (`legacy_lines`, niet opgeteld) en het aantal runs in de spool.

Een backfillregel voor een run die al een regel heeft, is een `correction`. Een run zonder regel krijgt een `charge` met `backfill: true`. Zo telt een run nooit twee keer.

### 4. De prijs komt alleen uit het register

Er is één prijsbron: de prijsversies in `wave7_models.yaml`, gelezen via `price_run` (ADR-040). De bron van een berekend bedrag noemt de versie: `price_table@<datum van de versie>`. Een prijs die de provider zelf rapporteert is `provider_reported`.

### 5. De bron is verplicht en `unavailable` is nooit nul

Een bedrag zonder bron bestaat niet. Een run met `unavailable:*` telt in geen enkele som. De aggregatie noemt hem apart, zodat een lezer ziet hoeveel runs hij mist in plaats van een te laag totaal te tonen.

### 6. Spool met replay is het herstelpad

Mislukt het schrijven, dan gaat dezelfde regel naar `events/provider_costs.spool/<run_id>.json`: een JSON-lijst van regels, atomair geschreven. Het subsysteem `cost-ledger` meldt dan degraded via het bestaande beaconmechanisme (`<data_dir>/health/cost-ledger.json`, status `PROBE_TO_BEACON["degraded"]`, event-driven). Mislukt ook de spool, dan faalt de aanroep (`CostLedgerSpoolError`). Nooit stil.

Wacht er een regel van een run in de spool, dan gaat een latere regel van dezelfde run er ook in. Zo komt een correctie nooit vóór de regel die ze vervangt in het grootboek, en betekent "laatste regel" ook "laatst geschreven".

`vnx costs replay` brengt de spool in het grootboek: één keer per `run_id` + `record_id`. Elke append, in `record_run` en in de replay, sluit onder de `flock` eerst een afgebroken laatste regel af met `\n`. Er verdwijnt geen byte: het afgebroken stuk telt als één onleesbare regel. Een spoolbestand verdwijnt pas als de replay het grootboek na de fsync terugleest en elke regel eruit terugvindt als eigen regel, met de nieuwste regel van elke run als laatste. Anders blijft het bestand staan, blijft het beacon degraded en eindigt `vnx costs replay` met exit 2. Een onleesbaar spoolbestand blijft staan en wordt gemeld. De dispatcher-prelude roept de replay elke tik aan, met dezelfde drempel als `lease_sweep` (`VNX_COST_LEDGER_REPLAY_INTERVAL_SEC`, standaard 30 seconden). Een mislukte replay stopt de prelude niet. Hij wordt gelogd en de spool blijft.

De tik hangt niet aan `VNX_SUPERVISOR_MODE`. De spool is het herstelpad van het grootboek en geen supervisie. Een regel in de spool ontbreekt in elke som tot hij is teruggezet.

### 7. Kalibratie is een eigen recordsoort

Een saldometing of een lijst generations van een provider is geen uitgave. Die krijgt een eigen bestand, `events/provider_account_observations.ndjson`, met recordsoort `account_observation`. Geen uitgave- of tokensom leest dat bestand. Dat is K5b en wordt hier niet gebouwd.

### 8. Het plafond schakelt pas om na de meetpoort

`paid_lane_budget` blijft `cost_usd` uit de receipts lezen. Het plafond leest het grootboek pas als de meetpoort het toelaat: K2a, K2b, K3, K4a en K4b uitgerold, en zeven dagen geen `metered`-regel met `unavailable:*` behalve de gedocumenteerde kimi-uitzondering. Dat is K5c. Daarna telt een `unavailable`-regel op een `metered` lane niet als nul: het plafond meldt hem en de deur waarschuwt, zonder te weigeren.

### 9. Geen centrale DB-tabel

Het grootboek is een NDJSON-bestand per project-store. Komt er ooit een projectie in een database, dan krijgt die `project_id` in de natuurlijke sleutel (ADR-007).

## Gevolgen

- Een lezer die het grootboek optelt, ziet wat hij mist: runs zonder prijs, regels in het oude formaat en runs in de spool. Een te laag totaal zonder melding kan niet meer.
- Het oude formaat blijft bestaan tot de lanes over zijn. `emit_provider_cost` en zijn aanroepers zijn ongewijzigd. De harness-lanes gaan in K2a-2 over op `record_run`; codex en kimi in K2b.
- De identiteit is strenger dan de prijsopzoeking. Een model dat het register niet kent, levert geen regel op maar een fout. Een lane die zo'n model draait, moet eerst in het register. Dat is de bedoeling: een geraden identiteit is precies de fout van OI-2186.
- Een run zonder segmenten op een lane met piek en dal wordt tegen het piektarief geboekt. Dat is een bovengrens. K2a-2 levert de segmenten uit het transcript.
- De replay leest het hele grootboek twee keer: vooraf om te zien wat al geland is, achteraf om terug te lezen wat hij schreef. Dat gebeurt alleen als er iets in de spool staat.
- De prelude start elke 30 seconden een Python-proces voor de replay, ook in legacy-modus. Met een lege spool schrijft dat proces niets.
