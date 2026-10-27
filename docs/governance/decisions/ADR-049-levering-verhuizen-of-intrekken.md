# ADR-049 - Een levering verhuist of wordt ingetrokken, nooit afgeboekt met bewijs dat haar niet leverde

**Status:** Proposed (dispatch dlv-6370c2e33cce, track deliverable-retarget-withdraw, PR-0)
**Datum:** 2026-10-25
**Besloten door:** T0, op het operatorbesluit van 25-10 naar aanleiding van OI-2325. Dit ADR legt de regel en de twee verben vast vóór PR-1 en PR-2 ze bouwen. Alle metingen staan op main `1ce78b68`.
**Cross-refs:** ADR-005 (eerst schrijven, dan de toestand wijzigen); ADR-007 (`project_id` op elke tabel en elke sleutel); ADR-030 (de plan-first-gate); ADR-046 en ADR-047 (de vorm van dit besluit); OI-1969 (een receipt zegt niet dat een levering verscheen); OI-2325 (de aanleiding).

## Context

`vnx deliverable close <dlv> --evidence #PR` (`scripts/planning_cli.py:3366`) is vandaag de enige weg om een levering van een track af te halen. Het commando schrijft `completed` en legt de PR vast als **operatorattestatie**: VNX controleert de merge niet (`cmd_deliverable_close`, docstring). Voor werk dat geleverd is, is dat de juiste vorm. Voor werk dat niet geleverd is, is het valse evidence.

De aanleiding is meetbaar. Op `retire-redundant-architecture` staan 21 van de 26 leveringen op `proposed`: 16 horen volgens de T0-meting van 25-10 op `terminal-binding-afbouwen`, 1 op `retire-feature-plan-second-planning-layer`, en P22 en P23 zijn optioneel en niet meer gewenst. Geen PR leverde ze. Ze met een PR-nummer afboeken zou een levering als geleverd laten tellen die nooit bestond: de deliverables-view telt de rij daarna in `completed_count` en leest haar als `done`, en de nameting kan haar niet meer als openstaand werk onderscheiden (OI-2325).

Twee dingen ontbreken daardoor: een weg om een levering naar een andere track te **verhuizen**, en een eindstatus voor een levering die **niet meer gebouwd wordt**. `expired` is daar geen antwoord op. Die staat al in `TERMINAL_DISPATCH_STATES` (`scripts/lib/coordination_db.py:45`) en leest in de view als `failed` (`_DELIVERABLES_VIEW`, `scripts/lib/schema_manifest.py:329`). Een ingetrokken levering is geen mislukking: er is niets geprobeerd.

De toestandsmachine maakt een nieuwe staat goedkoop. `DISPATCH_STATES` en `DISPATCH_TRANSITIONS` staan in `scripts/lib/coordination_db.py`; geen live store die de T0 mat (6 van 6) draagt nog een CHECK op `dispatches.state`, en de store op deze checkout heeft er geen. Een nieuwe staat vraagt dus geen tabelrebuild, alleen een migratie voor de view.

De terminale verzameling bestaat vandaag vier keer: canoniek in `coordination_db.TERMINAL_DISPATCH_STATES` (`scripts/lib/coordination_db.py:45`), als losse kopie in `track_reconciler.py` (`scripts/lib/track_reconciler.py:41`) en in `runtime_supervisor.py` (`scripts/lib/runtime_supervisor.py:67`), en als letterlijke `NOT IN ('completed', 'expired', 'dead_letter')` in `_deliverable_stubs` (`scripts/lib/track_reconciler.py:899`). Vier kopieën van één verzameling is de reden dat dit ADR één canonieke set eist.

## Besluit

### 1. De regel

**Een levering wordt nooit afgeboekt met een PR die het werk niet leverde.** `vnx deliverable close --evidence` blijft, en blijft uitsluitend voor werk dat een PR daadwerkelijk opleverde. Wie het werk niet levert, heeft twee geldige uitkomsten: verhuizen (het werk hoort ergens anders) of intrekken (het werk komt er niet). Beide laten een spoor na; geen van beide claimt een levering. Een ontbrekende reden, een onbekende track of een onbekende toestand is geen geldige uitkomst en weigert zonder wijziging.

### 2. Verhuizen: `vnx deliverable retarget`

`vnx deliverable retarget <dlv> --objective <track> --reason <tekst>` verplaatst een levering naar een andere track. De regels zijn deterministisch en staan hier vast:

- **Alleen `proposed` en `ready`.** Een `completed` levering is afgeboekt met bewijs en blijft staan; `expired`, `dead_letter` en `withdrawn` zijn eindstatussen en worden niet verhuisd.
- **Hetzelfde project.** De doeltrack moet in `tracks` bestaan voor dezelfde `project_id` (ADR-007). Er is geen verhuizing tussen projecten.
- **Geen doeltrack in fase `done`.** De fase-CHECK staat in `schemas/migrations/0022_track_layer.sql:36`. Een `parked` doeltrack vereist de expliciete vlag `--allow-parked`, omdat een geparkeerde track het werk niet gaat plannen.
- **Een `ready` levering gaat terug naar `proposed`.** Het akkoord bij promotie hoorde bij de oude track; de nieuwe track moet zijn eigen gate halen. De oude promotie wordt niet stilzwijgend meegenomen.
- **Compare-and-swap in één transactie.** De update is `UPDATE dispatches SET track = ?, state = ?, updated_at = ? WHERE dispatch_id = ? AND project_id = ? AND state = ?`, met de waargenomen staat in de `WHERE`. Wijkt de rij af, dan is `rowcount` 0 en stopt het commando zonder wijziging — een gelijktijdige verandering wordt niet overschreven.
- **Geen verhuizing zonder event.** In dezelfde transactie gaat `deliverable_retargeted` naar `coordination_events`, met `from_state`, `to_state`, `from_track`, `to_track` en `reason` in de metadata en `actor = operator`. Mislukt het event, dan rolt de update terug. Een verhuizing zonder event bestaat niet.
- **`--reason` is verplicht.** Een lege of alleen-witruimte reden weigert (exit 2) zonder wijziging, net als een onbekende doeltrack.

Een verhuizing naar dezelfde track met dezelfde staat is een no-op: niets verandert, dus er is geen event en geen schrijfactie.

### 3. Intrekken: `vnx deliverable withdraw`

`vnx deliverable withdraw <dlv> --reason <tekst>` zet een levering op de eigen eindstatus **`withdrawn`**. Niet `expired`: een ingetrokken levering is geen mislukking. Alleen `proposed` en `ready` komen in aanmerking; `completed` weigert (er is leveringsbewijs), `withdrawn` is een no-op, en elke andere staat weigert zonder wijziging. De reden is verplicht en komt in het event `deliverable_withdrawn`; dezelfde compare-and-swap-regel als bij verhuizen geldt (één transactie, `rowcount` 1, anders geen wijziging).

`withdrawn` komt in de **ene canonieke set** `coordination_db.TERMINAL_DISPATCH_STATES`, met `DISPATCH_TRANSITIONS['withdrawn'] = frozenset()` en een edge van `proposed` en `ready` naar `withdrawn`. De losse kopieën in `track_reconciler.py` en `runtime_supervisor.py` gaan die set lezen in plaats van hem te herhalen. De deliverables-view krijgt `withdrawn` als eigen `derived_status`; geen lezer telt een ingetrokken levering als open, gepland, geleverd of mislukt.

De negen lezers die PR-2 nalopen:

| Lezer | Wat hij vandaag doet | Wat PR-2 doet |
|---|---|---|
| `coordination_db.TERMINAL_DISPATCH_STATES` (`scripts/lib/coordination_db.py:45`) | canonieke verzameling zonder `withdrawn` | `withdrawn` erin; `DISPATCH_STATES`, `DISPATCH_TRANSITIONS` en de afgeleide `IN_FLIGHT_DISPATCH_STATES` kloppen vanzelf |
| `track_reconciler.py` (`scripts/lib/track_reconciler.py:41`) | losse kopie van de verzameling | leest de canonieke set |
| `runtime_supervisor.py` (`scripts/lib/runtime_supervisor.py:67`) | losse kopie voor de zombie-detectie | leest de canonieke set |
| `_deliverable_stubs` (`scripts/lib/track_reconciler.py:899`) | letterlijke `NOT IN ('completed', 'expired', 'dead_letter')`; een ingetrokken rij blijft een stub en kan door `_reconcile_deliverable_dispatches` alsnog `expired` of `completed` worden | leest de canonieke set; een ingetrokken levering is nooit een stub en wordt nooit afgeboekt |
| `_compute_derived_status` (`scripts/lib/track_reconciler_status.py:22`) | `done` op basis van "alles terminaal"; een rij `withdrawn` zou als geleverd lezen | `done` vereist een positief leveringssignaal (een `completed` rij, een `pr_merged`-event, een gemergede `pr_ref` of een gedeclareerde `done`); anders beslist de gedeclareerde fase |
| `_DELIVERABLES_VIEW` (`scripts/lib/schema_manifest.py:311`) | `planned_count` en `in_flight_count` kennen `withdrawn` niet; de `ELSE`-tak maakt er `proposed` van | `withdrawn_count` erbij, `withdrawn` als eigen `derived_status`, en `withdrawn` telt in geen van de drie tellingen en in geen enkele faal-tak |
| `scripts/planning_cli.py` (deliverable-verben) | `list`, `promote` en `close` kennen alleen `proposed`, `ready` en `completed` | `list` toont `withdrawn`, `promote` en `close` weigeren het met een genoemde reden, `withdraw` komt erbij |
| `is_terminal_dispatch_state` (`scripts/lib/runtime_state_machine.py:48`) | leest de canonieke set; `dispatch_broker` weigert er overgangen mee | een ingetrokken dispatch weigert elke volgende overgang, zonder extra code |
| `_REFIRE_RUNTIME_END_STATES` (`scripts/lib/dispatch_cli.py:3178`) | vereniging van de canonieke set met `failed_delivery` en `timed_out` | een ingetrokken dispatch is een runtime-eindstatus en wordt niet opnieuw gevuurd |

De volgorde binnen de view is vast: `done` (alles `completed`), dan `in_progress` (iets in behandeling), dan `withdrawn` (alles `withdrawn`), dan `failed` (alles in de faalset), dan `ready`, dan `proposed`. Een mengeling van een ingetrokken ronde en een open ronde leest als de open ronde: de deliverable is dan niet ingetrokken, en de ingetrokken rij telt in geen enkele telling mee.

### 4. Terugdraaien

De down-migratie van 0036 zet elke `withdrawn`-rij op `expired` en schrijft per rij het event `deliverable_withdrawn_reverted` in `coordination_events`, met de oude staat en de reden in de metadata. Zo ziet een oude lezer na een rollback een eindstatus die hij kent, en blijft de omzetting in het grootboek staan. Eerst boekt `apply_0036.revert_migration` één `state_mutation`-receipt (trigger `migration_0036_revert`, met de lijst `(dispatch_id, project_id)`) in de `t0_receipts.ndjson` naast de database; is het resultaat niet `appended`, dan weigert de revert en verandert er niets. Faalt de transactie daarna alsnog, dan boekt hij best-effort een `migration_0036_revert_aborted`-receipt voordat hij de fout doorgeeft. `completed` blijft ongemoeid; de down-migratie raakt alleen `withdrawn`. De voorkant (0036) maakt de view opnieuw en werkt de schema-manifest bij in dezelfde PR, zodat manifest en store niet uiteenlopen.

### 5. Gevolgen

- **De prijs.** `withdrawn` is een nieuwe staat die negen lezers raakt (tabel hierboven). PR-2 loopt ze één voor één na; een lezer die de staat niet kent, telt hem als open of als geleverd. Daarom is de canonieke set de enige bron en verdwijnen de losse kopieën in dezelfde PR.
- **De aanleiding.** OI-2325 en het operatorbesluit van 25-10: 21 leveringen op `retire-redundant-architecture` moeten verhuizen of worden ingetrokken, en geen ervan heeft een PR die het werk leverde.
- **Twee verben, één regel.** `retarget` en `withdraw` zijn elkaars spiegel: de eerste verplaatst de verantwoordelijkheid, de tweede beëindigt haar. Geen van beide schrijft `completed` en geen van beide raakt `pr_ref`.
- **`close` blijft scherp.** De bestaande eis `--evidence <pr>` wordt niet verzwakt. De regel staat in de docstring en in de weigering; PR-2 voegt alleen de twee ontbrekende uitkomsten toe.
- **Een ingetrokken levering blijft zichtbaar.** Ze verdwijnt niet uit de view en niet uit `vnx deliverable list`; ze leest alleen als `withdrawn`, zodat een operator ziet dat het besluit genomen is en niet dat de rij vergeten is.

### Amendement bij PR-2 (dlv-c61cf36113cc, 2026-10-27)

Drie punten wijken af van de tekst hierboven. De opdracht van PR-2 en een meting gingen voor.

- **De CHECK op `dispatches.state` bestaat wel, alleen niet live.** De zes live stores dragen hem niet (read-only gemeten). Een store die `vnx init` bouwt loopt 0022 en draagt hem wel, zonder `withdrawn`. Migratie 0036 rebuildt die tabel niet: `withdraw` weigert zo'n store met exit 2 en noemt de CHECK. Een migratie die de CHECK verbreedt is een vervolgstap.
- **`deliverable list` verbergt `withdrawn` zonder `--all`**, net als `horizon queue`, en meldt hoeveel rijen verborgen zijn. Met `--all` blijft de levering zichtbaar.
- **Een al ingetrokken levering is een weigering (exit 2), geen no-op.** De opdracht noemt alleen `proposed` en `ready` als toegestaan.

Een eerdere manifestversie accepteert voortaan de view van een latere versie, zoals ze extra kolommen al accepteerde. Zonder dat faalt een v36-store op elke manifestversie van 27 tot 35, en zet de reconciler hem na één verloren index terug naar 24 in plaats van naar 33.

### Wat deterministisch is en wat een model vraagt

Alles in dit besluit is deterministisch: de toegestane staten, de fase-CHECK, de compare-and-swap, de transactie met het event, de view en de negen lezers zijn regels en queries. Er is geen modelcall nodig en er komt er geen bij. De enige vrije tekst is `--reason`, en die wordt opgeslagen en getoond, nooit geïnterpreteerd. Waar de invoer niet klopt (onbekende staat, onbekende track, lege reden, `rowcount` 0) weigert het commando zonder wijziging; dat is de fail-closed kant van dezelfde regel.

### Wat dit ADR niet verandert

- **`vnx deliverable close`.** De operatorattestatie blijft de vorm voor geleverd werk; dit ADR verzwakt de `--evidence`-eis niet.
- **`promote` en de plan-first-gate.** De gate leest de track en zijn blokkades (ADR-030); een verhuizing verandert alleen de track van de rij, niet de gate.
- **De reconciler als melder.** `deliverable_close_candidates` blijft alleen melden en sluit niet (OI-1969). Dit ADR voegt geen automatische afboeking toe.
- **De rollup-grens van de view.** Een werkdispatch zonder eigen `output_ref` telt nog steeds niet mee (OI-1969).

### Alternatieven die zijn afgewezen

**(a) De 21 leveringen met een PR-nummer afboeken.** Afgewezen. Dat is precies het valse bewijs dat OI-2325 beschrijft: de rij leest daarna als geleverd en de view en de reconciler tellen hem mee. Een attestatie die niet klopt is erger dan een open rij.

**(b) `expired` gebruiken voor een ingetrokken levering.** Afgewezen. `expired` leest als `failed` in de view en betekent "geprobeerd en niet gehaald". Een ingetrokken levering is een besluit vóór de poging; de twee mogen niet dezelfde status delen.

**(c) Een `withdrawn`-vlag in `metadata_json` in plaats van een staat.** Afgewezen. Een vlag naast de staat laat elke lezer zelf beslissen of hij hem respecteert; de staat, de canonieke verzameling en de view dwingen één antwoord af. Een vlag is ook niet te valideren met een CHECK of een overgangstabel.

**(d) Verhuizen en intrekken in één verb met een `--state`-parameter.** Afgewezen. Het zijn twee verschillende besluiten met verschillende invoer (doeltrack tegenover alleen een reden) en verschillende vooruitzichten. Twee verben maken de weigeringen scherp en het audit-event ondubbelzinnig.

**(e) De negen lezers laten staan en `withdrawn` alleen in de view kennen.** Afgewezen. Dan leest de staat op acht andere plekken als open of als terminaal-zonder-levering; `_deliverable_stubs` zou een ingetrokken rij alsnog afboeken. De canonieke set is de reden dat één toevoeging negen lezers goed zet.

### Amendement bij dlv-0a0ae4abdcb7 (OI-2262, 2026-10-27)

Een afgewezen of achterhaalde poging eindigt in een eindstatus; een basislevering sluit op het bewijs van haar eigen ronde. Twee gaten hielden een track uit `done` en een geleverde basis onafboekbaar: op de vnx-dev-store stonden 14 zulke rijen op tracks van de 2.0-golven.

- **Een T0-`reject` beëindigt de poging.** `receipt_query.py decide <id> reject` schrijft de beslissing en verplaatst een rij in `failed_delivery` of `timed_out` daarna naar `dead_letter`, via `runtime_state_machine.transition_dispatch` (actor `t0`, de reden van de beslissing, de timestamp van de beslissingsregel in de metadata van het ene `dispatch_dead_letter`-event). Elke andere staat blijft ongemoeid: `completed` leverde, `dead_letter` en de andere eindstatussen zijn al geëindigd, een levende rij is geen mislukte poging. Een tweede reject is daarom een no-op. Een overgang die de store weigert maakt `decide` exit 1 en noemt de oorzaak; de beslissingsregel blijft staan, want die is het oordeel van de T0. Waarom dit in de staat moet en niet in een lezer: `_compute_derived_status` (`scripts/lib/track_reconciler_status.py`) kent alleen terminaal, in behandeling en gepland, dus één `failed_delivery`-rij liet de hele track voor altijd `queued`/`in_progress` (`dlv-ef18a14528ac-r3` op `deliverable-retarget-withdraw`).

- **Een achterhaalde basis sluit op haar eigen ronde.** `vnx deliverable close <id> --evidence N` accepteert een basis in `failed_delivery` of `timed_out` wanneer een rij `<id>-rK` van hetzelfde project `completed` is. De basis eindigt `expired`: terminaal, en geen leveringsclaim — haar eigen poging leverde niet. Wat leverde staat in `metadata_json.superseded_by` (`<id>-rK`), met de PR van de ronde in `pr_ref` en in de metadata, in één transactie met één `deliverable_superseded`-event en onder een compare-and-swap op de gelezen staat. Zonder `completed` ronde blijft de weigering staan (ADR-049: nooit afgeboekt met vreemd bewijs); een ronde van een ander project, een id dat geen `-rK` is, een ontbrekende of onleesbare `metadata_json`: geen bewijs, dus weigering zonder wijziging.

  De view (migratie 0037, schema v37) leest die combinatie: een `expired` rij waarvan `superseded_by` naar een `completed` rij van hetzelfde project wijst, telt als geleverd. `done` wordt daarmee "minstens één geleverde rij en elke rij geleverd of ingetrokken", en de faal-tak telt een achterhaalde rij niet meer mee. Zonder achterhaalde rij leidt v37 exact af wat v36 afleidt (per staat getest). De track-afleiding leest het `deliverable_superseded`-event als positief leveringssignaal naast `pr_merged`: de operator attesteerde bij het afboeken de PR van de ronde, en zonder dat signaal bleef een track met een gemergede `pr_ref` die niet in de lokale bronnen staat op `in_progress`.

- **De down-migratie van 0037 boekt niets.** Ze zet de v36-view terug en stempelt user_version 36 in één transactie; geen rij verandert, dus vraagt ADR-005 geen grootboekregel (0036's down boekt er een omdat ze `withdrawn`-rijen naar `expired` zet). Onder v36 leest een achterhaalde basis weer `failed` — fail-closed: niet als geleverd geteld, geen rij aangeraakt, en opnieuw toepassen van 0037 herstelt het beeld.

- **Wat dit niet verandert.** `ready` sluit zoals voorheen (`completed`, hetzelfde event, dezelfde uitvoer), en `--evidence` blijft verplicht. Een basis die al `dead_letter` is (een T0-reject ging voor) is geen `failed_delivery`/`timed_out` meer en sluit niet op rondebewijs: één richting, en de reject is dan de booking.

Dit amendement voegt geen modelcall toe. De staten, de ronde-herkenning (`-rK`), de compare-and-swap, de view en het leveringssignaal zijn regels en queries; de enige vrije tekst blijft de reden, die wordt opgeslagen en getoond, nooit geïnterpreteerd. Waar de invoer niet klopt — geen `completed` ronde, een vreemd project, onleesbare metadata, een geweigerde overgang — weigert het commando zonder wijziging of eindigt het non-zero met de oorzaak.
