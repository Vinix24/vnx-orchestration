# ADR-076 - De operator-wachtrij: een punt sluit op de staat van zijn bron, nooit op een vinkje

**Status:** Voorgesteld 14-11-2026 (T0, track operator-vragen-tot-antwoord, Q0); aangenomen zodra de T0 Q0 merget.
**Datum:** 2026-11-14
**Besloten door:** de operator, 14-11-2026 (alles wat op Vincent wacht staat op één plek, wordt opnieuw voorgelegd tot hij antwoordt en verdwijnt zodra het beantwoord is); het plan `claudedocs/specs/operator-vragen-tot-antwoord/plan.md` (versie 2, plan-gate van 14-11-2026, tiebreaker START, deepseek-v4-pro, ronde 2, met de verplichte wijziging dat `push-due` de push zelf reserveert). Het nummer 076 is door de T0 toegewezen; ADR-075 gaat naar een parallelle levering.
**Cross-refs:** ADR-005 (append-only NDJSON als waarheid), ADR-007 (composite sleutel over `project_id` voor elke centrale tabel), ADR-056 (wie mag promoveren, `actor_class`; de promote-poort verandert niet), ADR-063 (onbekend is nooit 0, nooit een geslaagde meting), ADR-065 (leren is voorstellen), ADR-066 (dream-liveness, `read_reviews`), ADR-072 (de steering-store en zijn besluitmodel; deze wachtrij leest hem alleen), het vooronderzoek `dlv-operator-vragen-oppervlak` (gemeten 14-11-2026 ~09:30 tot 09:40Z, read-only).

Dit ADR legt vast waartegen de leveringen Q1 tot en met Q6 bouwen: waar de wachtrij staat, welke regels hij kent, hoe een punt zijn identiteit houdt, welke vorm een verwijzing naar zijn bron heeft, wanneer een punt sluit, hoe het wordt voorgelegd en gepusht, hoe lang de ledger bewaard blijft en wat de audit wel en niet meet. Het bevat geen code.

Het hele pad is deterministisch. Een lezer per bron leest een veld, een rij of een ledgerregel en vergelijkt die met een vaste waarde. De identiteit is een hash, de `ref` een gesloten grammatica, de secretcontrole een vaste lijst patronen, de volgorde van de getoonde punten een sortering op drie sleutels en de pushlimiet een telling in de ledger. Er komt geen model aan te pas. De enige plek waar open taal binnenkomt, is een chatvraag (bron 8): daar besluit geen model of iets beantwoord is, maar legt de T0 Vincents eigen woorden vast als citaat (besluit 5 en 6). Een model dat in de chat raadt of een vraag beantwoord is, zou een punt op een gok sluiten; dat is precies de fout die deze wachtrij moet uitsluiten (zie Alternatieven, B).

## Context

Gemeten in het vooronderzoek van 14-11-2026:

- Wat op Vincent wacht, staat verspreid over acht bronnen: promote van een levering (`dispatches`, `state='proposed'`), een besluit dat een T0 vraagt (`open_outcomes`), dream-reviews (`state/dream/*-pending-review.json`), analyzer-voorstellen (`pending_edits.json`, `pending_archival.json`), steering-voorstellen (ADR-072), permissievoorstellen (track `permissieblokkades-leren`, nog niet gebouwd), cap-verdicts `mergen-met-OI` en vragen in een T0-chat. Mission Control leest er geen enkele van.
- Vijf bronnen hebben al een staat die "beantwoord" zegt: `dispatches.state` niet meer `proposed` plus een `deliverable_promoted`-event, `requires_operator_review` false plus een `dream_cycle_approved/rejected`-event, `status` niet meer `pending` in `pending_edits.json`, een `approved`-, `rejected`- of `applied`-record in de steering-store, en een akkoordregel in `operator_approvals.ndjson` op verdict, PR en kop. Een lezer per bron is genoeg; er hoeft geen nieuw besluitrecord bij.
- `proposed` betekent ook achterstand: vnx-dev 85 rijen (de oudste van 16-07), mission-control 94, sales-copilot 19, pacompany-engine 18, seocrawler-v2 4. Dat zijn geen vragen aan Vincent.
- Open outcomes zijn T0-werk: vnx-dev 31, seocrawler-v2 744, en geen enkel item heeft Vincent als geadresseerde.
- Een chatvraag laat niets na: 177 vraagberichten in 7 dagen, 38 blokkerend, 44 berichten met een promote-verzoek. Zonder record is "beantwoord" een gok.
- Om 08:35Z stonden 4 dream-reviews open (de oudste van 26-09) en 23 analyzer-voorstellen in vnx-dev, zonder dat iets ze opnieuw voorlegde. In seocrawler-v2 staan 39 analyzer-voorstellen open sinds 05-03.
- De fabric heeft geen pushpad voor operatorpunten. `PushNotification` is een tool van de sessie, geen codepad.
- `operating` en `website-vincentvandeth` hebben een store zonder `project_id`-kolom; `vinixD` en twee testprojecten hebben geen `state/`. Daar is de stand onbekend, niet 0.

## Besluit

### 1. Eén append-only ledger per project, lokaal

De waarheid is `~/.vnx-data/<project_id>/state/operator_queue.ndjson`, append-only (ADR-005). De afgeleide stand is `state/operator_queue.json`; die wordt altijd uit de ledger herbouwd en is nooit zelf waarheid. Elke schrijfactie loopt onder een exclusieve `fcntl.flock` op een lockbestand naast de ledger, schrijft de regel en doet `fsync` vóór de afgeleide stand wordt bijgewerkt, zoals `steering_proposal_store`. Een onleesbare ledgerregel maakt de stand `unknown` met het regelnummer, nooit een overgeslagen regel.

De ledger gaat nooit naar Supabase en heeft geen centrale tabel. Komt er ooit een centrale tabel, dan geldt ADR-007: `project_id TEXT NOT NULL` en elke UNIQUE of PK composite over `project_id` (bijvoorbeeld `(project_id, point_id, seq)`). Eigenaar: Q1.

### 2. Zes soorten regels, een gesloten lijst

Elke regel draagt `kind`, `project_id` en `ts` (UTC, ISO 8601). Een regel met een andere `kind` of zonder een verplicht veld weigert de schrijver met de reden; de lezer van de stand behandelt zo'n regel als onleesbaar (besluit 1).

| `kind` | Velden |
|---|---|
| `opened` | `point_id`, `bron` (1 tot en met 8, de lijst van Context), `ref`, `revisie`, `vraag` (één zin, hooguit 200 tekens), `sluit_bij` (het antwoord dat het punt sluit, in woorden), `kopieerblok` (als het kan, anders leeg), `asked_at` (de eerste keer gevraagd), `opened_by` (`lezer` of de T0-sessie) |
| `presented` | `point_id`, `kanaal` (`kickoff`, `rotatie`, `status`, `push`, `cli`, `mc`), `sessie` |
| `closed` | `point_id`, `uitkomst` (`beantwoord` of `vervallen`, geen andere waarde), `bewijs` (de bronstaat: veld, waarde, event-id of ledgerregel), `beantwoord_door` (uit de bron, anders `onbekend`) |
| `unknown` | `point_id`, `reden` (bron onleesbaar, rij weg, bestand weg, `ref` niet leesbaar) |
| `answer` | `point_id`, `citaat` (Vincents antwoord, hooguit 300 tekens), `vastgelegd_door` (de T0-sessie). Alleen voor een punt met `ref` `antwoord:<hash>` |
| `push_reserved` | `point_ids` (lijst), `sessie`. Alleen geschreven door `push-due` (besluit 10) |

`beantwoord` betekent: de bron zegt dat het antwoord gegeven is. `vervallen` betekent: de vraag doet er niet meer toe (de rij is weg omdat de track `done` is, er is een nieuwer verdict, de PR is gemerged of gesloten). `vervallen` telt nooit als `beantwoord`, en een punt dat niet meer te lezen is, wordt `unknown`, geen van beide. Eigenaar: Q1.

### 3. De identiteit van een punt en de revisieregel

`point_id = sha256(project_id | bron | ref | revisie)`, ingekort tot 16 hex. Dezelfde vraag twee keer gesteld geeft hetzelfde punt: een tweede `opened` met een bestaande `point_id` schrijft niets. Een lezer schrijft alleen bij een verandering, dus twee keer lezen geeft geen extra regels.

**Revisie.** Heropent een bron hetzelfde ding (een steering-voorstel dat terugkomt als `reopened`, een dream-review die opnieuw `requires_operator_review` true krijgt), dan stijgt `revisie` en is het een nieuw punt. Het oude punt blijft gesloten. `revisie` begint bij 1.

**Een chatvraag.** Voor bron 8 is de `ref` `antwoord:<hash>`, met `hash = sha256(genormaliseerde vraag)[:12]`. Normaliseren is: kleine letters, witruimte samengevoegd tot één spatie, en niets anders. Dispatch-ids, PR-nummers, OI-nummers en andere getallen blijven staan zoals ze er staan. Dezelfde chatvraag in een andere sessie is dan hetzelfde punt; dezelfde vraag over een andere PR, OI, levering of een ander bedrag is een ander punt. Een dubbele vraag kost een herhaling, een samengevoegde vraag sluit zonder antwoord. Eigenaar: Q1 (`point_id`, revisie), Q3 (de normalisatie van `antwoord:`).

### 4. De grammatica van `ref`, met een sluitregel per soort

`ref` heeft precies een van deze zes vormen. Elke andere vorm weigert `vnx inbox ask` met exit 2 en een foutmelding die de toegestane vormen noemt; er wordt dan niets geschreven.

| `ref` | Beantwoord als | Vervallen als |
|---|---|---|
| `dlv:<id>` | `dispatches.state` is niet meer `proposed` | de track van de levering staat op `done`, met of zonder rij |
| `oi:<id>` | de OI is `closed`, `deferred` of `wontfix` | n.v.t. |
| `track:<id>:<fase>` | de track staat op `<fase>` of verder | de track staat op `done` zonder die fase |
| `config:<sleutel>` | de sleutel staat in `project_config`, met een auditregel na `asked_at` | n.v.t. |
| `verdict:<id>` | een akkoordregel met dat verdict, die PR en die kop | een nieuwer verdict op dezelfde PR, of de PR is gemerged of gesloten |
| `antwoord:<hash>` | een `answer`-regel voor het punt | n.v.t. |

Een `dlv:`-rij die weg is terwijl de track niet op `done` staat, is `unknown`, niet `vervallen`. Het plan noemt een weggevallen rij in de ene tabel `vervallen` en in de andere `onbekend`; dit ADR kiest de lezing die ADR-063 volgt: alleen de track op `done` is positief bewijs dat de vraag er niet meer toe doet, een rij die zonder die staat verdwijnt is een onleesbare bron. Een sluitregel leest alleen de staat van de bron; hij leest nooit een `presented`-regel, een chatbericht of een vinkje. Eigenaar: Q3 (grammatica, weigering, de sluitregels van deze tabel).

### 5. Een punt sluit op de staat van zijn bron, nooit op een vinkje

Er is geen commando dat een punt dichtzet. `closed` wordt alleen geschreven door een lezer die de bronstaat van de tabel in besluit 4 of 7 leest, met die staat als `bewijs`. Ook bij `antwoord:<hash>` sluit het punt niet op het commando zelf: `vnx inbox answer` schrijft een `answer`-regel, en de lezer sluit het punt daarna op die regel als bronstaat, met `beantwoord_door: t0_namens_operator`. Een punt sluit nooit op een chatbericht zonder vastgelegd citaat.

**Onleesbaar is `unknown`, nooit `beantwoord` (ADR-063).** Een bron die niet te lezen is, een rij of bestand die weg is, een store zonder `project_id`-kolom of een project zonder `state/` geeft een `unknown`-regel met de reden. Het punt blijft dan open en wordt gewoon voorgelegd, met de reden erbij. Een lezer die een veld mist, `None` leest, een onbekende enumwaarde ziet of een lege lezing krijgt, sluit niets.

**Een gesloten punt komt nooit terug in een herinnering.** De herinneringslijst, `push-due` en het blok `operator_inbox` lezen alleen punten die in de afgeleide stand open zijn. Dat geldt ook na een rotatie en in een andere sessie (klaar-criterium 4). Een bron die hetzelfde ding heropent, geeft een nieuw punt via de revisieregel van besluit 3, niet het oude punt terug. Eigenaar: Q1 (de stand), Q2a, Q2b en Q3 (de lezers).

### 6. Een `answer` legt een besluit vast, met Vincents eigen woorden

`vnx inbox answer <point_id> --quote "..."` schrijft één `answer`-regel. Het commando weigert een punt dat niet bestaat, dat gesloten is of dat geen `ref` `antwoord:<hash>` heeft (voor elke andere `ref` beslist de bron, niet de T0), en een leeg citaat of een citaat van meer dan 300 tekens. Het citaat zijn Vincents eigen woorden; een samenvatting door de T0 is geen citaat. Eigenaar: Q3.

### 7. Welke bron een punt opent

Een punt is open zolang zijn bron open zegt. Per bron:

| Bron | Opent | Sluit | Onbekend |
|---|---|---|---|
| 1 promote | alleen via een vraag van een T0 (`vnx inbox ask --bron promote --ref dlv:<id>`), nooit uit de achterstand van `proposed`-rijen | `state` niet meer `proposed`; `beantwoord_door` uit `metadata.actor_class` van het `deliverable_promoted`-event | de rij is weg. De vraag toont ook waarom promoveren nu geweigerd zou worden (`_promotion_refusal`, `_track_blocks_promotion`, read-only) |
| 3 dream | elk bestand met `requires_operator_review` true, via `dream_liveness.read_reviews` | het veld is false; bewijs is het `dream_cycle_*`-event | bestand onleesbaar |
| 4 analyzer | elke `edits[]` en `pending_archival`-regel met `status` pending | `status` is iets anders; tijd uit `rejected_at` of `decided_at`. Een edit die na `apply` uit het bestand verdwijnt, sluit als beantwoord met de regel in `edit_history.json` als bewijs | bestand weg, of een edit die verdwijnt zonder regel in `edit_history.json` |
| 5, 6 steering en permissie | elk voorstel met `status` pending in `steering_proposal_store`, ook de soorten van de permissie-track | een `approved`-, `rejected`- of `applied`-record na de laatste revisie; `beantwoord_door` uit `decided_by` | project niet gebonden: de dekking zegt `niet_gebonden`, nooit 0 |
| 7 cap-verdict | `review_cap_verdicts` met `outcome='mergen-met-OI'` zonder akkoord via `merge_approval` | een akkoordregel met hetzelfde verdict, dezelfde PR en dezelfde kop; vervallen bij een nieuwer verdict op die PR, of als de PR gemerged of gesloten is | de PR-kop is na het verdict verschoven: het punt blijft open met de reden `kop_verschoven`, want een akkoord op een oude kop geldt niet |
| 2, 8 besluit en vraag | alleen via `vnx inbox ask --bron besluit\|vraag --ref <soort>:<id>` | de staat die `ref` noemt, volgens besluit 4 | `ref` niet leesbaar |

Waar de bron geen actor draagt (`pending_edits.json`, `operator_approvals.ndjson`), zegt `beantwoord_door` `onbekend`. Dit ADR voegt daar geen actor-klasse toe; dat is een uitbreiding van ADR-056 en een eigen OI. Eigenaar: Q2a (bron 3 en 4), Q2b (bron 5, 6 en 7), Q3 (bron 1, 2 en 8).

### 8. Open outcomes blijven T0-werk

`open_outcomes` komt niet in de operator-wachtrij. Geen lezer leest `open_outcomes` of `t0_decision_log.jsonl` als bron van punten. Alleen een T0 die een specifiek item zelf aan Vincent voorlegt, maakt er een punt van met `vnx inbox ask --bron besluit` en een `ref` van besluit 4. Eigenaar: Q3 (er is geen lezer voor bron 2; de test bewijst dat een open outcome zonder `ask` geen punt geeft).

### 9. Geen secrets: weigeren, niet maskeren

Een `vraag`, `kopieerblok` of `citaat` die op een patroon uit `scripts/lib/secret_patterns.py` past, wordt geweigerd met de naam van het patroon (nooit de gevonden waarde) en er wordt niets geschreven. Maskeren mag niet: een gemaskeerde regel laat zien dat er een secret was en verleidt tot een tweede poging met de waarde ernaast. De patronen: `sk-`, `gh[pousr]_`, AWS-sleutel-id, Slack-token, `Bearer`, `KEY|TOKEN|SECRET|PASSWORD=`, `Authorization:`, een PEM-blok en een JWT. Dat is één lijst, gedeeld met `permission_events.py` van de track `permissieblokkades-leren`; geen van beide tracks houdt een eigen kopie. Eigenaar: Q1 (de module en de weigering in de schrijver, met een test per patroon).

### 10. Voorleggen en pushen

**Altijd vers.** `vnx inbox show` en `build_t0_state.py` draaien eerst de lezers en tonen pas daarna. Een punt wordt nooit voorgelegd uit een afgeleide stand die ouder is dan die lezing.

**Welke vijf.** Het blok `operator_inbox` in `t0_index.json` bevat hooguit 5 open punten plus `more` (het aantal dat niet getoond is), per punt de vraag, de leeftijd sinds `asked_at` en het kopieerblok. De volgorde: eerst nooit voorgelegd, dan het minst vaak voorgelegd (aantal `presented`-regels), dan de oudste `asked_at`, en bij gelijkspel `point_id`. Zo zakt een vaak getoond punt niet weg achter nieuwe punten, en blijft een nieuw punt niet onder de oude hangen. Het SessionStart-hook toont dat blok. Elk getoond punt krijgt een `presented`-regel met zijn kanaal.

**De rolregel.** De canonieke rol `role-orchestrator.md` (gesynct met `vnx role sync`) krijgt twee regels: een T0 die Vincent iets vraagt, schrijft het eerst met `vnx inbox ask`, en een vraag die alleen in de chat staat, telt als niet gesteld; elke statusupdate eindigt met `vnx inbox show --present --channel status`.

**Push, afgedwongen door de ledger.** `vnx inbox push-due` kiest de punten waarvoor een push nu mag: een open punt zonder `push_reserved`, of een open punt waarvan de laatste `push_reserved` minstens 4 uur oud is (`VNX_INBOX_PUSH_INTERVAL_H`, standaard 4). Hooguit één push per project per 30 minuten, met alle gekozen punten in één bericht. Binnen één `flock` leest `push-due` de ledger, beslist, schrijft de `push_reserved`-regel, doet `fsync` en geeft pas daarna de punten terug. Een tweede aanroep, uit dezelfde of een andere sessie, ziet die reservering en krijgt niets. De limiet geldt dus over sessies heen en hangt niet af van een latere bevestiging. Een onleesbare ledger geeft geen push en een foutmelding, nooit een push zonder reservering. De T0 stuurt de push met de tool van zijn sessie; een beurt zonder nieuw of verlopen punt stuurt niets. Eigenaar: Q4 (`show`, de volgorde, `push-due`), Q5 (`operator_inbox`, het hook, de rolregels).

### 11. Vlootbreed ontdekken, met een dekkingsregel per project

`vnx inbox --all` stelt de lijst projecten samen uit elke map onder `~/.vnx-data/` plus elk project in het centrale projectregister, niet alleen uit mappen met een `state/`. Per project staat er één dekkingsregel: `gelezen`, `niet_gebonden` (de steering-store is niet gebonden), of `unknown` met de reden (geen `state/`, geen `project_id`-kolom, onleesbaar bestand). Een project zonder store verdwijnt nooit stil uit de lijst, en een project met `unknown` telt nooit als nul open punten. `--json` geeft dezelfde punten en dezelfde dekking als de tekstuitvoer. Eigenaar: Q4.

### 12. Bewaartermijn en levenscyclus van `operator_queue.ndjson`

**De termijn is 180 dagen na `closed`.** De regels van een punt dat langer dan 180 dagen gesloten is, gaan uit de ledger. Een open punt (ook een punt in `unknown`) verliest nooit een regel, hoe oud het ook is. Een `push_reserved`-regel gaat mee zodra al zijn `point_ids` weg zijn.

Waarom 180 dagen. De ledger bevat korte vragen (hooguit 200 tekens), citaten van hooguit 300 tekens, ids en tijden, en nooit transcripttekst. De omvang is klein: bij de gemeten 177 vragen per week en een paar `presented`-regels per punt zijn dat enkele tienduizenden regels per halfjaar, ruim onder een paar MB. Ruimte is dus niet de reden om in te korten. De reden is dat een gesloten vraag na een halfjaar niets meer toevoegt: het besluit zelf staat in zijn bron (de dispatch-rij, het dream-event, de steering-store, de akkoordregel) en die bronnen hebben hun eigen bewaring. 180 dagen dekt de 7-daagse audit van besluit 13 ruim, laat een kwartaalterugblik op vraag-tot-antwoordtijden toe en houdt geen vragen langer vast dan nodig. Een kortere termijn (30 dagen) zou de terugblik op een terugkerende vraag breken; zonder termijn groeit een bestand met operatortekst eindeloos.

**Het commando.** `vnx inbox compact [--project-id <id>|--all] [--dry-run]` past de termijn toe. Het draait onder dezelfde `flock` als de schrijver, schrijft de overblijvende regels naar een tijdelijk bestand naast de ledger, doet `fsync` en vervangt de ledger met een atomaire `rename`. Daarna herbouwt het de afgeleide stand. Verwijderde regels worden niet gearchiveerd. `--dry-run` toont per project hoeveel punten en regels zouden gaan. Een onleesbare ledger compacteert het niet: dan meldt het `unknown` met het regelnummer en blijft het bestand ongemoeid. Eigenaar: Q6.

**Een project dat verdwijnt.** De ledger staat in `~/.vnx-data/<project_id>/state/` en heeft geen kopie elders (besluit 1). Wordt die map verwijderd, dan gaat de ledger mee en blijft er geen wees achter. Staat het project nog in het centrale projectregister, dan geeft `vnx inbox --all` voor dat project `unknown` met de reden `geen state/`; het telt dus niet als schoon. Is het project ook uit het register, dan staat het niet meer in de lijst.

### 13. De grens van de audit

`vnx inbox audit --since 7d [--all]` meet per punt de tijd van `asked_at` tot de eerste `presented`. Hij telt twee soorten te laat: een punt dat na meer dan 48 uur voor het eerst is voorgelegd, en een open punt ouder dan 48 uur zonder één `presented`-regel. Een project met `unknown` telt niet als schoon.

De audit meet alleen vanaf de eerste regel van de ledger; die tijd staat als `ledger_since` in de uitvoer. Wat daarvoor wachtte, heet `niet gemeten`, niet schoon. Na `vnx inbox compact` schuift `ledger_since` naar de oudste overgebleven regel, zodat een gecompacteerd tijdvak nooit als schoon telt. De meetweek van klaar-criterium 7 begint pas als Q5 live is. Eigenaar: Q6.

### 14. Wat dit ADR niet doet

- Het verandert de promote-poort van ADR-056 niet en promoveert niets.
- Het voegt geen actor-klasse toe aan `operator_approvals.ndjson` of `pending_edits.json`.
- Het schrijft niet in de bronnen: elke lezer opent zijn bron read-only.
- Het stuurt zelf geen melding buiten de sessie (mail, Telegram); de digest-mail blijft zoals hij is.

## Gevolgen

- Wat op Vincent wacht, staat op één plek per project en vlootbreed onder `vnx inbox --all`, met een dekkingsregel waar een bron niet te lezen is.
- Een punt verdwijnt pas als de bron het antwoord laat zien. Een T0 kan een punt niet wegwerken door het af te vinken; hij kan alleen het besluit in de bron laten nemen of een citaat vastleggen.
- Een T0 die een vraag alleen in de chat stelt, heeft hem volgens de rol niet gesteld. De audit maakt een vergeten voorlegging zichtbaar.
- Mission Control leest alleen `vnx inbox --all --json`. Dat is het leescontract voor de track `wacht-op-vincent-scherm`; die track is apart, leest alleen en schrijft nooit in de ledger.

Testplicht per levering:

| Levering | Testplicht |
|---|---|
| Q1 | idempotente `point_id` (dezelfde vraag twee keer geeft één `opened`); een nieuwe revisie bij heropenen geeft een nieuw punt en laat het oude gesloten; weigering per secretpatroon zonder dat de waarde in de foutmelding staat; een regel met een onbekende `kind` of zonder verplicht veld wordt geweigerd; een onleesbare ledgerregel maakt de stand `unknown` |
| Q2a | lezers voor bron 3 en 4 op tijdelijke stores; de `edit_history.json`-regel (verdwenen met regel is `beantwoord`, zonder regel `unknown`); klaar-criterium 4: een punt sluiten en daarna drie herinneringsrondes draaien zonder dat het terugkomt |
| Q2b | lezers voor bron 5, 6 en 7 op tijdelijke stores; `niet_gebonden` is nooit 0; `kop_verschoven` houdt het punt open; een nieuwer verdict of een gemergede PR geeft `vervallen`, nooit `beantwoord` |
| Q3 | elke `ref`-vorm van besluit 4 met zijn sluitregel; elke andere vorm geeft exit 2 en schrijft niets; `answer` op een punt zonder `antwoord:`-ref wordt geweigerd; twee chatvragen die alleen in een PR-nummer verschillen geven twee verschillende `point_id`'s, en een `answer` op de ene laat de andere open; een open outcome zonder `ask` geeft geen punt; de lezer voor bron 1 toont de weigerreden |
| Q4 | `--all` toont een project zonder `state/` als `unknown`; de volgorde van de vijf; twee gelijktijdige `push-due` geven één `push_reserved`-regel; geen tweede push binnen 30 minuten, ook over twee sessies |
| Q5 | `operator_inbox` in `t0_index.json` bevat alleen open punten, hooguit 5 plus `more`; het SessionStart-hook toont het blok; de twee rolregels staan in `role-orchestrator.md` na `vnx role sync` |
| Q6 | beide soorten te laat; `ledger_since` en `niet gemeten` voor alles daarvoor; `compact` verwijdert alleen punten die langer dan 180 dagen gesloten zijn, laat elk open punt heel en weigert een onleesbare ledger |

Stopregels uit het plan blijven gelden: een levering waarin een punt sluit terwijl de bron nog open zegt, gaat terug; een secret in een ledger, rapport of PR is E3 en gaat terug; een test die meer dan één push per project per 30 minuten laat zien, gaat terug.

## Alternatieven

**A. Sluiten op een vinkje van de T0** (`vnx inbox done <point_id>`). Afgewezen. Een vinkje zegt dat de T0 denkt dat het beantwoord is, niet dat de bron het antwoord bevat. Het vooronderzoek laat zien dat vijf bronnen de staat al dragen; een vinkje ernaast kan van die staat afwijken (afgevinkt terwijl de levering nog `proposed` staat) en dan verdwijnt een open vraag uit de herinnering. Dat is de fout die Vincent met dit spoor wil uitsluiten.

**B. Een vectorstore of zoeken in de chat om "beantwoord" te bepalen.** Afgewezen. Een gelijkende zin in een later bericht is geen besluit: "ja" na een vraag kan op een andere vraag slaan, en een antwoord in een andere sessie of in een terminal is in de chat niet te zien. Het resultaat is een kans, geen staat, en een punt zou op een gok sluiten of open blijven. Het is ook niet reproduceerbaar in een test en zou transcripttekst in een index zetten. Bron 8 krijgt daarom een vastgelegd citaat (besluit 6) en de overige bronnen een lezer op hun staat.

**C. Pushen bij elke beurt.** Afgewezen. Een T0 heeft tientallen beurten per uur; een push per beurt maakt de melding ruis en Vincent leert hem te negeren. Een limiet die de sessie zelf bijhoudt, geldt niet over sessies heen: twee T0's of een rotatie pushen dan dubbel. Daarom reserveert `push-due` binnen één transactie in de ledger (besluit 10), met 4 uur per punt en één push per project per 30 minuten.

**D. Een centrale tabel in Supabase of in `runtime_coordination.db`.** Afgewezen voor nu. Alle bronnen zijn per project en lokaal, en de vragen bevatten operatortekst die het project niet hoeft te verlaten. Een lokaal bestand per project volgt `steering_proposal_store` en vraagt geen migratie. Komt er later een centrale tabel, dan geldt ADR-007 (besluit 1).

**E. Alle `proposed`-rijen en open outcomes als punt.** Afgewezen. 85 oude rijen in vnx-dev en 744 open outcomes in seocrawler-v2 zijn geen vragen aan Vincent. De wachtrij zou volstromen en een echte vraag verdwijnt eronder. Een promote of besluit wordt alleen een punt als een T0 het vraagt (besluit 7 en 8).

## Terugweg

De wachtrij leest alleen en schrijft alleen in eigen bestanden. Terugdraaien is de twee regels uit `role-orchestrator.md` halen, het blok `operator_inbox` uit `build_t0_state.py` halen en `operator_queue.ndjson` en `operator_queue.json` laten staan of verwijderen; niemand anders leest ze. Geen bron wordt gewijzigd, dus er is geen datamigratie terug.
