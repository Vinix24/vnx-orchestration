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
| `renumbered_from` | optioneel, alleen door de replay gezet: de oorspronkelijke `run_id` van een tweede uitvoering die onder een volgende poging is geboekt (§6) |

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

> **Gefaseerd.** Deze paragraaf landt in drie PR's (plan `claudedocs/specs/kosten-per-model-in-het-grootboek/k2a3-spool-een-lock.md`, plan-gate 21-10). Gedaan: K2a-3a (#2150) met het lockbestand, het schrijfpad met de spool als terugval en de aggregatie in één scope. Gedaan: K2a-3b (#2155) met de replay, quarantaine, hernummering en `vnx costs replay`. Gedaan: K2a-3c met eerst de zes adviezen van #2155 (OI-2250), daarna de tik in de prelude, het beacon `cost-ledger` en de eigen bestanden van het grootboek in `vnx doctor` en de footprint-probe. Het eerste ontwerp met twee locks (grootboek-`flock` plus spool-lock) haalde #2141 niet. Vijf van de zes codex-blockers zaten in de momenten waarop het ene lock vrij was en het andere niet. Dat ontwerp en die bevindingen staan in `claudedocs/specs/kosten-per-model-in-het-grootboek/k2a-spool-replay-uitgesteld.md`.

**Eén lock.** `events/provider_costs.lock` staat naast het grootboek (`provider_costs.ledger_lock`). Het wordt aangemaakt met `O_CREAT` en nooit verwijderd. Elke actor neemt het als eerste lock:

| Actor | Lockbestand | Daaronder |
|---|---|---|
| `record_run`, `record_next_attempt` | `LOCK_EX` | grootboek openen en `flock`; grens herstellen; wacht de run al in de spool, of is het een correctie van een hernummerde regel, dan in de spool; anders append en fsync |
| `emit_provider_cost` (oud formaat) | `LOCK_EX` | grootboek-`flock`, append zoals voorheen |
| replay (`replay_spool`) | `LOCK_EX` met `LOCK_NB`, per spoolbestand | grootboek-`flock` met `LOCK_NB`, begrensd; grens; ontbrekende regels schrijven, altijd fsync, elke regel teruglezen en veld voor veld vergelijken; receipts met een begrensde receipt-lock; pas daarna verwijderen |
| `aggregate_runs` | `LOCK_SH` | grootboek lezen en spool tellen in deze ene scope; herstelt niets |
| beacon (`report_spool_health`) | `LOCK_SH` met `LOCK_NB`, begrensd | spool tellen en het beacon schrijven in deze ene scope; staat de lock langer vast dan `VNX_COST_LEDGER_LOCK_WAIT_SEC`, dan schrijft hij deze keer niets |

De `flock` op het grootboek zelf blijft, genest onder het lockbestand. Hij beschermt de bytes tegen een proces van een oudere fabric-versie dat het lockbestand niet kent. Een 1.7.2-proces schrijft alleen het oude formaat. Dat telt in geen som (`legacy_lines`). Een proces van fabric f2bef053 (K2a-2) schrijft wel regels in de vorm `provider_cost_run/1`, en die tellen mee. Het kent de spool niet en telt de pogingen alleen in het grootboek. Wacht een poging in de spool, dan kan het die poging opnieuw boeken als een andere uitvoering onder dezelfde `run_id`. De aggregatie meldt dat als `spool_conflicts` en de replay hernummert de uitvoering uit de spool (zie "Twee uitvoeringen onder één `run_id`"). De volgorde is altijd eerst het lockbestand en dan het grootboek. Oude actoren nemen het lockbestand nooit, dus er is geen cyclus.

**Het faalcontract.** Wat de schrijver doet, volgt uit wat er op schijf kan staan:

| Wat faalt | Kan de regel in het grootboek staan? | Wat de schrijver doet | `landed` |
|---|---|---|---|
| openen, `flock` of de grens | nee | spool | `spool` |
| `write` (ook een halve) | een deel | eigen regel afsluiten als dat kan, dan spool | `spool` |
| `fsync` of `close` na een volledige `write` | ja | ook naar de spool | `ledger+spool` |

Het teruggegeven record draagt `landed` als attribuut (`WrittenRecord`). De regel zelf krijgt dat veld niet. De spool is één bestand per run, `events/provider_costs.spool/<leesbaar>~<digest>.json`: een JSON-lijst van regels, atomair geschreven en gevolgd door een fsync van de map. De naam is de run_id met elk teken buiten `[A-Za-z0-9._:@+-]` als `_` (hoogstens 128 tekens), dan `~` en de eerste 16 hextekens van de sha256 van de exacte run_id, zodat twee run_id's die gelijk ontsmet worden (`review/a` en `review_a`) nooit één bestand delen. Een regel die in het grootboek én in de spool staat, telt één keer. De aggregatie neemt per `run_id` de laatste regel en de replay ziet aan `record_id` en inhoud dat hij al geland is. Een grootboekregel met hetzelfde `record_id` en andere inhoud telt nooit als geland.

Wacht er een regel van een run in de spool, dan gaat een latere regel van dezelfde run er ook in. Dat geldt ook als het grootboek werkt. Zo komt een correctie nooit vóór de regel die ze vervangt. `record_next_attempt` telt de pogingen in het grootboek en in de spool onder hetzelfde exclusieve lock als de schrijfactie. Een refire terwijl poging 1 in de spool wacht, wordt dus poging 2.

`CostLedgerWriteError` heeft één betekenis: ook de spool faalde, of het lockbestand was niet te nemen. De uitkomst is dan onbekend, want de regel kan in het grootboek staan. Faalde de spool tijdens of na het vervangen van het bestand van de run, dan kan hij ook in de spool staan en zegt de melding dat. De aanroeper faalt, wijzigt niets en boekt geen nieuwe poging. `record_next_attempt` zou er anders een tweede run van maken. De aanroeper meldt de fout (op de envelope het OI-1179-pad) en de replay of een correctie lost het op. Kan de schrijver de spool niet lezen om te zien of de run er wacht, of kan hij de pogingen niet tellen, dan schrijft hij niets en geeft hij dezelfde fout.

**Grensherstel.** Alleen een houder van `LOCK_EX` sluit een laatste regel zonder `\n` af. Een lezer onder `LOCK_SH` verandert niets. Hij telt een gescheurde staart als één onleesbare regel en meldt `torn_tail`. Een 1.7.2-schrijver herstelt geen grens en kan zijn regel aan een gescheurde staart plakken. Wat dan verloren gaat, is altijd een oude-formaatregel plus het fragment. De nieuwe regel staat volgens het faalcontract ook in de spool.

**De aggregatie.** `LedgerTotals` krijgt `spool_pending`, `spool_unreadable`, `spool_conflicts`, `spool_quarantined`, `torn_tail` en `unlocked_read`. `incomplete` geeft de reden waarom een totaal niet volledig is, of niets. Elke onleesbare regel maakt het totaal onvolledig, ook een regel midden in het bestand. De spoolmap wordt opgesomd met `os.scandir` en alleen een ontbrekende map is een lege spool. Een map die niet te lezen is, geeft `spool_unreadable` en nooit een volledig totaal (blocker 6 uit #2141). Elke lezer neemt de run uit de regels van een spoolbestand en nooit uit de naam. Een bestand met een regel van een andere run is onleesbaar en telt voor geen van beide runs, ook niet bij het tellen van de pogingen. Een regel in de spool ontbreekt in elke som tot de replay hem terugzet. Een spoolbestand waarvan elke regel al met dezelfde inhoud in het grootboek staat (`ledger+spool`) wacht nergens op en telt niet in `spool_pending`. Staat een `record_id` er met andere inhoud, dan telt het bestand wel.

Een lezer heeft geen schrijfrecht op de store nodig. Bestaat het lockbestand, dan opent hij het alleen-lezen en neemt hij `LOCK_SH`. Bestaat het niet, dan maakt hij het aan zoals elke actor. Lukt ook dat niet, dan leest hij zonder lock en zet hij `unlocked_read`. Het totaal heet dan onvolledig. Onder een testrunner wordt het lockbestand nooit in de echte centrale store aangemaakt.

**Twee uitvoeringen onder één `run_id`.** Een schrijver van fabric f2bef053 (K2a-2 zonder spool) telt de pogingen alleen in het grootboek. Wacht poging 1 in de spool, dan boekt hij een andere uitvoering opnieuw als poging 1. Een regel in de spool waarvan de `run_id` in het grootboek een regel met een ander `record_id` tegenkomt, is daarom een tweede uitvoering. Een correctie die via `supersedes` een regel van die run vervangt, of een eerdere spoolregel die de run voortzet, hoort bij dezelfde uitvoering. Komen alle grootboekregels van de run uit dit spoolbestand zelf, dan is er geen tweede uitvoering maar een achterstand. De replay voegt een tweede uitvoering nooit bij die run. Hij boekt haar onder de eerstvolgende vrije poging van dezelfde `(project_id, producer, ref)`, geteld over grootboek en spool, bewaart de oorspronkelijke `run_id` in het veld `renumbered_from` en schrijft een receipt. Tot de replay haar hernummert, meldt de aggregatie haar als `spool_conflicts`. Het totaal is dan onvolledig en het bedrag uit de spool staat in geen som. `record_next_attempt` telt voorbij beide pogingen en logt een waarschuwing met de `run_id`. Een correctie van een regel die de replay al hernummerde, is geen tweede uitvoering en geen conflict: ze volgt die regel naar de hernummerde run (zie "De replay").

**De replay.** `cost_ledger.replay_spool` neemt per spoolbestand het lockbestand met `LOCK_NB`, herhaald tot `VNX_COST_LEDGER_LOCK_WAIT_SEC` (5) en nooit langer dan er over is van het budget (OI-2250). Per aanroep verwerkt hij hoogstens `VNX_COST_LEDGER_REPLAY_BATCH` (50) bestanden binnen `VNX_COST_LEDGER_REPLAY_BUDGET_SEC` (10) seconden. Onder het lockbestand neemt hij de `flock` op het grootboek ook met `LOCK_NB`. Hij herhaalt die zolang er iets over is van de wachttijd en van het budget. Een schrijver van een oudere fabric-versie neemt alleen die `flock`, en een blokkerende `flock` zou het lockbestand vasthouden zolang die schrijver het grootboek heeft. Elke huidige schrijver zou dan achter de replay wachten. Om dezelfde reden is de receipt begrensd. De append-lock van het receiptgrootboek (`append_receipt.lock`) wordt alleen geprobeerd binnen wat er over is van de wachttijd en het budget (`emit_governance_receipt(..., lock_timeout=...)`). Zo'n begrensde append schrijft zijn centrale mirror niet zelf, want die wacht op de lock van een andere store. Hij zet de mirror in de wachtrij `pending_mirrors.ndjson`, onder dezelfde append-lock waaronder hij de receipt schreef, en de volgende onbegrensde append zet hem door. Een tweede keer de lock nemen voor de wachtrij kon na een geschreven receipt verlopen en de schuld kwijtraken (OI-2250). Lukt een van de locks niet, dan geeft hij het lockbestand vrij, schrijft hij niets meer voor dat bestand, laat hij het staan en stopt hij met `lock_busy`.

Onder beide locks herstelt hij de grens, leest hij welke regels al geland zijn en schrijft hij alleen wat ontbreekt. Geland betekent: een grootboekregel met het `record_id` en dezelfde inhoud, veld voor veld. Staat een `record_id` er met andere inhoud, dan schrijft hij niets voor het bestand en laat hij het staan. Hij logt de run en de velden die afwijken en gaat door met het volgende bestand. De aanroep eindigt dan met `readback_mismatch` en exit 1. Dan doet hij een fsync van het grootboek en de map. Die fsync gebeurt altijd, ook als alle regels al geland waren en hij niets schreef. Zo'n regel kan ongesynchroniseerd in het grootboek staan: de fsync van de schrijver faalde (`ledger+spool`) of een eerdere replay werd na zijn append gedood. Tot een fsync slaagt, is het spoolbestand de enige duurzame kopie. Faalt de fsync, dan blijft het bestand staan en stopt de replay met `write_failed`. Daarna leest hij terug. Het bestand verdwijnt pas als elke regel ervan, nu geschreven of al geland, als eigen volledige regel terugkomt met dezelfde inhoud en elke hernummerde regel zijn receipt heeft. Voor een hernummerde regel vergelijkt hij met de regel die de replay ervan maakte, `record_id` inbegrepen. Een gedode replay laat het bestand dus staan, en de volgende ziet aan `record_id` en inhoud wat er al staat.

Een hernummering telt de pogingen voorbij elk spoolbestand. Staat er een bestand in de spool dat niet te lezen is, dan wacht de hernummering. De replay schrijft dan niets voor het bestand en zet eerst de onleesbare bestanden in quarantaine, elk als eigen bestand binnen de grenzen van de aanroep en met het lockbestand tussendoor vrij. Daarna probeert hij het bestand opnieuw. Houdt een grens hem eerder tegen, dan wacht de hernummering op een volgende aanroep. Hij telt nooit pogingen voorbij een bestand dat hij niet kon lezen.

Een hernummerde regel is de regel uit de spool met een nieuwe `run_id`, het veld `renumbered_from` met de oorspronkelijke `run_id`, het veld `renumbered_from_record_id` met het oorspronkelijke `record_id` en een nieuw `record_id`. Een correctie van een hernummerde regel volgt die regel, in hetzelfde spoolbestand of in een latere aanroep. De replay boekt haar onder de hernummerde `run_id`, met `supersedes` naar de hernummerde regel en `renumbered_from` op de oorspronkelijke `run_id`. Ze krijgt nooit een nieuwe poging en wel een receipt. Het bedrag van de hernummerde run is dan dat van de correctie. De schrijver boekt zo'n correctie niet zelf, ook niet als het grootboek werkt: onder de oorspronkelijke `run_id` zou ze de andere uitvoering vervangen. Hij zet haar in de spool (`landed` is `spool`) en de replay boekt haar. Aan `renumbered_from_record_id` ziet de replay ook na een onderbreking welke spoolregel al geland is. Het eerst gebouwde ontwerp rekende dat `record_id` terug uit de hernummerde regel. Dat ging mis bij een correctie die in `supersedes` de hernummerde regel zelf noemde: de hernummering laat die `supersedes` staan, dus terugrekenen kon niet zien wat ze in de spool noemde. Een correctie daarvan verving dan de andere uitvoering ($1,35 in plaats van $2,60), en een onderbroken replay schreef de eerste correctie twee keer (OI-2250). Een regel met maar één van beide velden, of waarvan `renumbered_from` geen andere poging van dezelfde `(project_id, producer, ref)` noemt, is onleesbaar. De receipt (`cost_ledger_renumbered`, `state_mutation`) gaat via de bestaande append-helper naar het receiptgrootboek van de store, één keer per hernummerde regel. Voor de store van het project zelf is dat `t0_receipts.ndjson` in de opgeloste `VNX_STATE_DIR`, ook als die buiten `<data_dir>/state` ligt, zodat hij naast elke andere `state_mutation`-receipt van het project staat (OI-2250). Voor het grootboek van een andere store is het `<data_dir>/state`.

**Quarantaine (OI-2243).** Een spoolbestand dat niet te lezen is, geen niet-lege JSON-lijst van runregels is of een regel van een andere run bevat, verplaatst de replay naar `events/provider_costs.spool/quarantine/`. Een runregel heeft de identiteit die de schrijver maakt: een `run_id` die zich laat splitsen, de `project_id` van die `run_id`, een `kind` en bij een correctie een `supersedes` als tekst. Een spoolregel zonder dat gaat ook in quarantaine. Eerder liep `a:b:1` als `InvalidRecord` en een regel zonder `project_id` als `KeyError` uit de replay, de tweede nadat de hernummerde regel al geland was. Elke weigering (`CostLedgerRefusal`) tijdens het plannen van een bestand zet het in quarantaine, voordat er iets van geschreven is, dus geen spoolinhoud eindigt de replay in een traceback (OI-2250). Hij verwijdert het nooit. Een naam die daar al staat krijgt een achtervoegsel `.<n>`. Elke quarantaine telt als één bestand in de batch en het budget van de aanroep. Eerst komt het besluit en dan de verplaatsing (ADR-005). De receipt (`cost_ledger_spool_quarantined`) gaat naar het receiptgrootboek vóór de `rename`, met de bestandsnaam, de sha256 van de inhoud en de doelnaam. Faalt de receipt, dan verplaatst hij niets: het bestand blijft in de spool en de aanroep stopt met `write_failed`. Faalt de `rename` na de receipt, dan vindt de volgende replay het besluit terug aan bestandsnaam en inhoud. Hij maakt alleen de verplaatsing af, naar de doelnaam uit de receipt en zonder tweede receipt. Na de verplaatsing volgt een logregel met bestand en reden. Daarna telt het bestand niet meer als onleesbare spool en houdt het geen `record_next_attempt` meer tegen. De aggregatie telt de quarantaine in `spool_quarantined`. Zolang daar iets staat, blijft het totaal onvolledig: wat er in staat, ontbreekt in de som. Een spoolmap die niet op te sommen is, gaat nooit in quarantaine. De replay stopt dan met exit 1.

**`vnx costs replay [--json]`** draait één begrensde replay op de store van het project, via `scripts/cost_ledger_replay.py`. De pip-CLI en `bin/vnx` starten hetzelfde script. Exit 0 is klaar of niets te doen, ook als een grens bestanden liet staan. Exit 1 is een spoolmap die niet te lezen is, een schrijffout of een regel die met andere inhoud terugkomt (`readback_mismatch`). Exit 3 is `lock_busy`: het lockbestand, de grootboek-`flock` of de receipt-lock.

**De tik (K2a-3c).** `_maybe_cost_ledger_replay` in de dispatcher-prelude (`dispatcher_supervisor_ticks.sh`) start `scripts/cost_ledger_replay.py` hoogstens één keer per `VNX_COST_LEDGER_REPLAY_INTERVAL_SEC` (60) seconden, als subproces met een harde timeout van 20 seconden, en doodt het daarna. `VNX_COST_LEDGER_TICK_TIMEOUT_SEC` mag die timeout alleen verlagen. De replay begrenst zichzelf op 10 seconden, dus de kill is de vangrail voor een replay die blijft hangen. De tik hangt niet aan `VNX_SUPERVISOR_MODE`: de spool is het herstelpad van het grootboek en geen supervisie. Hij logt elke fout (een exitcode anders dan 0, een kill, een log- of drempelbestand dat niet te schrijven is) en geeft altijd 0 terug. Het drempelbestand is `$STATE_DIR/.last_cost_ledger_replay_ts`. Is het niet te schrijven, dan houdt de dispatcher de drempel in zijn eigen proces bij. Met een lege spool schrijft het subproces niets.

**Het beacon.** Na elke replay schrijft het script het beacon `cost-ledger` via het bestaande beaconmechanisme (`<data_dir>/health/cost-ledger.json`, `cost_ledger.report_spool_health`). Het is degraded zolang de spool een bestand houdt, een bestand dat niet te lezen is of een bestand in quarantaine, en ok zodra er niets meer staat. In het beaconbestand staat de status van het probe-framework (`PROBE_TO_BEACON`: degraded wordt `stale`), en de details dragen `state`, `spool_files` en de spooltellingen van `LedgerTotals`. Het beacon is event-driven (`expected_interval_seconds=None`). Een lege spool met een beacon dat ok zegt, of zonder beacon, schrijft niets en neemt geen lock. Anders telt hij en schrijft hij in één scope onder het gedeelde lockbestand, begrensd door `VNX_COST_LEDGER_LOCK_WAIT_SEC`. Staat de lock langer vast, dan schrijft hij deze keer niets en logt hij dat. Zo hangt de tik nooit op het beacon.

**De eigen bestanden van het grootboek.** `vnx doctor` (het fabric-script en de pip-CLI) en de footprint-probe kennen `events/provider_costs.ndjson`, het lockbestand `events/provider_costs.lock` en de spool `events/provider_costs.spool/` met `quarantine/` als bekende bestanden (`cost_ledger.ledger_files`, alleen-lezen, maakt niets aan). In de spool kent hij spoolbestanden, het tijdelijke bestand van een onderbroken atomaire spoolschrijfactie en `quarantine/`. Al het andere daar heet een zwerfbestand: geen replay leest het en geen totaal telt het. De doctor waarschuwt bij een wachtend spoolbestand, een bestand in quarantaine, een zwerfbestand of een map die niet op te sommen is. De probe telt ze onder `cost_ledger` en verandert er zijn oordeel niet door.

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
- K2a-2 (#2148) en K2a-3a gaan in dezelfde release (1.7.3), dus geen uitgebrachte versie draait K2a-2 zonder de spool. Een proces uit de tijd van f2bef053 mag na K2a-3a (#2150) geen store meer beschrijven; `spool_conflicts` laat zien wanneer dat toch gebeurde.
- Een receipt over meerdere pogingen waarvan er één in de spool wacht, krijgt geen bedrag maar een `unavailable:`-bron. De aggregatie ziet die run pas na de replay. Een receipt over één poging neemt het bedrag van het teruggegeven record, ook als dat in de spool staat.
- Elke schrijver neemt het lockbestand, ook als het grootboek gewoon werkt. Is het niet te nemen, dan faalt de aanroep. Zonder dat lock is de volgorde van de run niet te garanderen en is ook de spool niet te gebruiken.
- Elke append krijgt een fsync. Dat kost één fsync per run en maakt het onderscheid tussen een halve regel en een hele regel die niet bevestigd is.
- Het oude formaat blijft bestaan tot de lanes over zijn. `emit_provider_cost` schrijft dezelfde regel als voorheen en neemt nu eerst het lockbestand. De harness-lanes gingen in K2a-2 over op `record_run`; codex en kimi volgen in K2b.
- Het lockbestand is een nieuw bestand in elke store. Sinds K2a-3c kennen `vnx doctor` en de footprint-probe het, net als het grootboek en de spool met quarantaine.
- De identiteit is strenger dan de prijsopzoeking. Een model dat het register niet kent, levert geen regel op maar een fout. Een lane die zo'n model draait, moet eerst in het register. Dat is de bedoeling: een geraden identiteit is precies de fout van OI-2186.
- Een run zonder segmenten op een lane met piek en dal wordt tegen het piektarief geboekt. Dat is een bovengrens. K2a-2 levert de segmenten uit het transcript.
- Met K2a-3b: de replay leest het hele grootboek twee keer, vooraf om te zien wat al geland is en achteraf om terug te lezen wat hij schreef. Per spoolbestand doet hij één fsync van het grootboek en de map, ook als hij niets schreef. Dat gebeurt alleen als er iets in de spool staat. Voor elke receipt van een quarantaine of een hernummering leest hij het receiptgrootboek één keer, zodat geen receipt dubbel komt. Een correctie leest bij het schrijven het grootboek één keer, om te zien of ze een hernummerde regel volgt. De centrale mirror van een receipt van de replay gaat via de wachtrij en komt pas aan bij de volgende onbegrensde append in die store.
- Met K2a-3c: de prelude start de replay als subproces met een harde timeout van 20 seconden, ook in legacy-modus, hoogstens één keer per minuut. Met een lege spool schrijft dat proces niets. Een dispatcher wacht per tik dus hoogstens 20 seconden op de spool, en een tik die faalt of hangt houdt hem nooit tegen.
- Met K2a-3c: de replay wordt pas door de tik aangezet nu de twee adviezen die het operatorbesluit noemde gerepareerd zijn (OI-2250, punt 1 en 4). Een hernummerde regel draagt sindsdien `renumbered_from_record_id`. Er bestond op dat moment nergens een hernummerde regel (gemeten op 21-10 over de zes stores onder `~/.vnx-data/`), dus geen oudere regel mist het veld.
- Met K2a-3c: het beacon `cost-ledger` leest het hele grootboek alleen als de spool iets houdt of het beacon iets te corrigeren heeft. Het beschrijft de spool zoals de laatste replay hem achterliet. Houdt de spool na een tik nog iets, dan staat het beacon op degraded. Een spool die in dezelfde tik leeg raakt, laat geen beacon achter.
- Met K2a-3c: het beacon wacht op zijn eigen lock tot dezelfde deadline als op het lockbestand en de `flock` van het grootboek, `VNX_COST_LEDGER_LOCK_WAIT_SEC` na het begin van de aanroep. Het houdt het lockbestand dus nooit langer gedeeld vast dan die wachttijd, ook niet bij `vnx costs replay` met de hand, waar geen watchdog omheen staat. Lukt het niet op tijd, dan volgt een waarschuwing en geen beacon; het resultaat en de exitcode van de replay blijven gelijk.
- Met K2a-3c: een beacon op `ok` dat ouder is dan een dag (`BEACON_REFRESH_SECONDS`) wordt bij een lege spool opnieuw geschreven. De lezer noemt een event-driven beacon na 7 dagen `unknown`, dus anders stond een gezonde store vanaf dag 8 onder NOT ok. Een draaiende tik houdt het beacon vers. Een tik die een week stilstaat blijft zichtbaar als `unknown`. Dat klopt. Het kost één leesronde van het grootboek per dag.
- Met K2a-3c: de doctor en de footprint-probe lezen de bestanden van het grootboek zonder lock. Hun telling is een momentopname; het totaal en het beacon blijven de lezers onder het lockbestand.
