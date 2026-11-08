# ADR-056 - Zelfstandigheid als meetbaar begrip: norm, sleutel, periode, drempel en wie mag promoveren

**Status:** Voorgesteld 08-11-2026 (track `zelfstandigheid-meten`, PR-1). De waarden onder "Periode en noemer" en "Drempel" worden bevestigd door de operator in stap 1 van het plan; tot dan is de status Voorgesteld.
**Datum:** 2026-11-08
**Besloten door:** de operator voor de bevoegdheid (beslissing 2 van de 1.9-sprint, 07-11-2026 ~14:30Z, hieronder letterlijk); T0 voor de rest, op het plan `claudedocs/2026-11-07-naar-1-9/plannen/zelfstandigheid-meten.md` (revisie 2, plan-gate-bevindingen F1 tot en met F7 en de tiebreaker-voorwaarde T). ADR-056 was vrij op `origin/main` `8c96dc306`.
**Cross-refs:** ADR-005 (eerst het grootboek), ADR-007 (`project_id` in elke sleutel), ADR-045 (het grootboek legt elke dispatch vast), ADR-047 (de tracks-DB is de enige planbron), ADR-055 (een falsifieerbaar grootboek).

## Context

De vraag "werkt de T0 zelfstandig" wordt nu met een gevoel beantwoord. Er is geen norm, geen telperiode en geen sleutel waarop de bronnen te koppelen zijn. Gemeten op de dev-state op 08-11-2026:

- `cmd_deliverable_promote` (`scripts/planning_cli.py:3195`) schrijft `actor="operator"` (`scripts/planning_cli.py:3257`). `cmd_deliverable_close` (`scripts/planning_cli.py:3269`) doet hetzelfde (`scripts/planning_cli.py:3373`). Dat is 474 promote- en 52 close-events met een literal, geen meting. Wie `actor='operator'` leest, leest een naam en geen bewijs.
- De kolom `requires_operator_promotion` (`schemas/migrations/0022_track_layer.sql:40`) suggereert een beleid dat niets afdwingt.
- De merge-receipt (`scripts/pr_merge.py:1892`) heeft geen `project_id`. In 736 `pr_merged`-receipts staat geen `actor` en geen `project_id`. De prompt-log kent alleen `session_id` en `cwd`.
- `verify_plan_gate_pass` (`scripts/lib/plan_gate_evidence.py:108`) plakt een gezakt signatuur op `PRESENT_UNSIGNED`, en van 163 plan-gate-records is er nul ondertekend.
- De telperiode was niet begrensd en de noemer niet gedefinieerd. Een teller zonder die twee levert altijd "te weinig", wat de operator ook doet.

Dit ADR legt de norm vast waartegen PR-2 tot en met PR-8b bouwen. Het bevat geen code. Het is deterministisch van begin tot eind: actorklassen volgen uit signalen, de sleutel is een tuple, de noemer is een telling, de drempel is een getal. Er zit geen model in de meting. Een model mag een oordeel voorstellen, nooit een bron tellen of een klasse kiezen.

## Besluit

De operator heeft op 07-11-2026 (beslissing 2 van de 1.9-sprint) bepaald:

> "zelfstandigheid-meten: promote ja, leren voorstellen. De T0 promoveert zelf de leveringen van een plan dat de plan-gate passeerde. Learning- en dreaming-uitkomsten blijven voorstellen die Vincent goedkeurt."

Zelfstandigheid wordt gemeten als het aandeel leveringen binnen een begrensde periode waarvoor geen enkele bron een handeling van de operator laat zien, over een noemer die vooraf is gedefinieerd, met een sleutel die per tenant klopt. Wat niet te meten is, heet `unknown` en telt nooit als nul. De zes secties hieronder zijn het besluit.

## Norm

Een handeling is autonoom als de afgeleide actorklasse niet `operator` is en niet `unknown`. De klasse wordt afgeleid en nooit letterlijk geschreven (F1). De naam in de bestaande kolom `actor` blijft vrij en blijft gevuld voor bestaande lezers. De klasse staat als `metadata.actor_class` op het event of de receipt, omdat `coordination_events.actor` al `NOT NULL` met een default is en een nieuwe kolom een fleet-brede migratie zou vragen.

| Klasse | Wanneer | Telt als |
|---|---|---|
| `operator` | De handeling komt uit een TTY en er is geen agentmarker | handeling van de operator |
| `t0` | Een agentmarker met de rol orchestrator (`CLAUDE_ROLE`, terminal T0) | autonoom |
| `worker` | Een dispatchmarker (`VNX_DISPATCH_ID` of `VNX_CURRENT_DISPATCH_ID`) met een andere rol dan orchestrator | autonoom |
| `automation` | Geen TTY, geen agentmarker, wel een bekende proceseigenaar (daemon, reconciler, geplande taak) | autonoom |
| `unknown` | Alles wat in geen van de vier past, ook een agent zonder marker en zonder TTY | niet te meten |

De afleiding is één pure functie `derive_actor_class(env, isatty)` zonder I/O, in `scripts/lib/actor_class.py` (PR-2). Twee onafhankelijke signalen (TTY en marker) bepalen de klasse. Een agent zonder marker wordt dus `unknown` en nooit `operator`, anders zou de zelfstandigheid te laag gemeten worden. Een tweede implementatie bij een aanroeper is niet toegestaan.

Een `t0`-handeling onder een gedelegeerde bevoegdheid draagt `metadata.grant` met de verwijzing naar het plan-gate-record dat haar dekt (F1, zie "Wie mag promoveren"). Zonder die verwijzing is het geen gedelegeerde promotie en weigert PR-5b haar.

Een levering heeft nul operatorhandelingen als geen van de zes bronnen (promote, close, decide, merge, override, prompt) voor haar sleutel een rij met klasse `operator` of `unknown` heeft. `close` is een eigen bron (F4): een operator die zelf afrondt heeft gekeken, en een teller die close mist leest alles-zelf-sluiten als nul zelfstandigheid.

## Tenant-veilige sleutel

De leveringsidentiteit is `(project_id, delivery_id)` (F7, voorwaarde T). `delivery_id` is `dispatches.id` van de leveringsrij (`dlv-...`), `project_id` is de tenant-as uit ADR-007. Elke getelde bron schrijft beide velden. Joinen op `session_id`, `cwd` of de actornaam is niet toegestaan, omdat die niet tenant-veilig zijn en een sessie meerdere leveringen kan bevatten.

| Bron | Plaats | Moet dragen | Rij |
|---|---|---|---|
| promote | `cmd_deliverable_promote` | `project_id`, `delivery_id`, `actor_class` | PR-2 |
| close | `cmd_deliverable_close` | `project_id`, `delivery_id`, `actor_class` | PR-2 |
| decide | `t0_decision_log` / `receipt_query.py decide` | `project_id` (bestaat), `delivery_id` erbij | PR-7a |
| merge | merge-receipt via `pr_merge.py:1892` | `project_id` (nu nergens bepaald), `delivery_id` uit de dispatch | PR-3 |
| override | `governance_overrides` | `project_id` (kolom ontbreekt), `delivery_id` | PR-4 |
| delivery add | `cmd_deliverable_add`, `register_proposed_track_dispatch` | `project_id` en `delivery_id` via één gedeelde helper `delivery_identity.py`, geen tweede implementatie naast `register_dispatch` (`scripts/lib/runtime_state_machine.py:116`) | PR-8a |
| prompt attribution | de `UserPromptSubmit`-hook | `project_id` en `delivery_id`, alleen als precies één levering de sessie deelt; anders `unknown: ambiguous_session` | PR-8b |

Een bron die geen `project_id` kan bepalen schrijft `project_id=unknown` en telt als `uncovered`. Zij wordt nooit stil als `vnx-dev` geboekt. De teller (PR-7a) joint op niets anders dan dit tuple en aggregeert niet over projecten.

## Periode en noemer

Een cohort is de verzameling leveringen die in de periode een terminale toestand bereikte (F5, D9 tot D11). De periode is begrensd en staat vast vóór de meting. Voorgesteld: `2026-10-30T00:00:00Z` tot `2026-11-30T00:00:00Z` (einde exclusief). De ingangsdatum is de datum waarop de dev-state 183 afgeronde `dlv-`-leveringen telde. De operator bevestigt of verschuift beide data in stap 1 van het plan. Een andere periode is een nieuwe marker, geen aanpassing.

- Een levering komt in aanmerking als `id LIKE 'dlv-%'` en `created_at` op of na de start van de periode ligt.
- `completed` telt als geleverd. Alleen dan is de levering echt de deur uit.
- `withdrawn`, `expired` en `dead_letter` zijn niet geleverd. Ze staan met hun reden in de uitvoer en niet in de noemer (PR-6a). Meetellen zou de zelfstandigheid opblazen, want er is niets geleverd om zelfstandig te laten.
- Een `completed` levering met `pr_ref` maar zonder merge-receipt is `unknown: receipt_missing`. Dat is niet nul.
- `N` is het aantal in aanmerking komende, geleverde leveringen. `zero_rate` is het aantal met nul operatorhandelingen gedeeld door `N`. `unknown_rate` is het aantal met minstens één bron op `unknown` gedeeld door `N`. Beide gaan over leveringen en nooit over events.
- Per project is de dekking `covered` (in elke bron minstens één geldige sleutel), `uncovered` (minstens één bron ontbreekt) of `no_data` (geen enkele levering in de periode). Een leeg project is `no_data` en nooit `covered` (PR-6b). Het telt niet mee in een rate tot het data heeft.

## Drempel

Het cohort is groot genoeg als `N >= 50`. Dit getal is bevroren in dit ADR en geldt voor de meetperiode hierboven. Reden: bij 50 leveringen is een rate van 0,1 nog leesbaar, en 30 maakt het oordeel te ruisgevoelig. De teller toont `N` altijd, zodat de lezer de steekproef ziet.

Het oordeel heeft drie waarden:

| Oordeel | Voorwaarde |
|---|---|
| `onvoldoende_meting` | `N < 50`, of `unknown_rate` boven de afkapwaarde |
| `selfstandig` | `N >= 50` en `zero_rate` aan de goede kant van de afkapwaarde |
| `niet_selfstandig` | `N >= 50` en `zero_rate` aan de verkeerde kant van de afkapwaarde |

Een `N` onder de 50 geeft nooit `selfstandig`. De afkapwaarden voor `zero_rate` en `unknown_rate` stelt het plan niet vast. De operator zet ze in stap 1 van het plan, in een amendement op dit ADR, vóór PR-7b draait. Zolang ze ontbreken weigert PR-7b een oordeel te geven en meldt de reden (fail-closed). Het wijzigen van `50` of van een afkapwaarde is een nieuw amendement op dit ADR en nooit een codewijziging.

## Wie mag promoveren

De standaard blijft: de operator promoveert. Tot nu toe was dat een praktijk die niets afdwingt. Vanaf PR-5b is het een afgedwongen regel.

De operatorbeslissing van 07-11-2026 opent één uitzondering. De T0 mag zelf leveringen promoveren van een track waarvan de plan-gate gepasseerd is. Dat gaat alleen onder deze voorwaarden (D5, D6, F2):

1. De track staat expliciet op `metadata.autonomous_promotion_eligible` (standaard `false`). Die opt-in is een bewuste handeling, wordt vastgelegd en leeft in de track-metadata en niet in een nieuwe kolom (PR-5b).
2. De plan-gate van de track is gepasseerd en de grant-verifier leest het record als `VERIFIED`: een geldig signatuur en een `project_id` die exact gelijk is aan dat van de track (PR-5a). `PRESENT_UNSIGNED`, `SIGNATURE_INVALID` en `ABSENT` zijn niet genoeg. Een `project_id` van `None` in het record telt niet als match.
3. De promotie draagt `actor_class=t0` en `metadata.grant` met de verwijzing naar dat record.

Op de dag van dit ADR is geen enkel van de 163 plan-gate-records ondertekend. In de praktijk blijft promotie dus operator-only tot ondertekening in gebruik is. Dat is de veilige uitkomst en geen fout.

Learning- en dreaming-uitkomsten worden nooit door een agent gepromoveerd. Ze blijven voorstellen die de operator goedkeurt. De opt-in van een track geldt voor leveringen en niet voor dit soort uitkomsten.

`scripts/lib/plan_gate_evidence.py` wordt in deze track niet gewijzigd. De strikte verifier komt als nieuwe functie in `scripts/lib/plan_gate_enforcement.py` (PR-5a), omdat `plan_gate_evidence.py` van de track `gate-profiel-per-project` is.

## Write-once uitkomstmarker

De eerste echte uitkomst mag achteraf niet bijgesteld kunnen worden. PR-7b schrijft daarom een write-once marker met de vijf projecten (`vnx-dev`, `seocrawler`, `mission-control`, `sales-copilot`, `pacompany`), de periode, `N`, de drempels en het oordeel. Een tweede schrijfpoging naar dezelfde marker faalt. Een andere periode is een nieuwe marker met een nieuwe naam. De marker legt ook vast welke bron voor het einde van een levering is gebruikt (vandaag `dispatches.state`), zodat een latere wijziging van het effectieve einde de uitkomst niet stil verschuift.

De track is klaar als aan de volgende punten tegelijk voldaan is:

- deze ADR bestaat met de zes secties en een bevestigde periode en afkapwaarden;
- elke bron schrijft `(project_id, delivery_id)` of staat met reden als `uncovered` in de dekking;
- `receipt_query.py operator-cohort` toont `N`, `zero_rate` en `unknown_rate` voor een project, en per project `covered`, `uncovered` of `no_data`;
- de marker bestaat, bevat de vijf projecten, de periode, de drempels en het oordeel, en een tweede schrijfpoging faalt.

Een geslaagde meting zonder `N >= 50` bestaat niet. Een nul zonder bron bestaat niet.

## Consequenties

- PR-2 tot en met PR-8b bouwen tegen deze tekst. Een afwijking van de sleutel, de klassen of de noemer is een amendement en geen bouwkeuze.
- Bestaande `actor='operator'`-events blijven staan. Ze hebben geen `actor_class` en lezen als `unknown`, zodat historie nooit als zelfstandig geteld wordt.
- Zolang de prompt-hook niet in de repo-instellingen staat (PR-8b), telt de prompt-bron als `uncovered` en niet als nul.
- Zolang plan-gate-records niet ondertekend zijn, is de T0-promotie uit de operatorbeslissing wel toegestaan maar in de praktijk geblokkeerd. Ondertekening gebruiken is een aparte stap.
- De drempel `N >= 50` kan bij een korte periode onhaalbaar blijken. Dat is een zichtbare uitkomst (`onvoldoende_meting`) en geen stille nul.
- Als de track `audit-spoor-volledig` het effectieve einde van een levering verandert, leest de teller dat einde en legt de marker de versie vast.
