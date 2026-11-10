# ADR-069 - De effectmeting van intelligence-injectie: een hash-arm in een bevroren venster, een harde grens van 200 per arm en vier verdictklassen onder een plafond

**Status:** Voorgesteld 10-11-2026 (T0, track `intelligence-effect-measurable`, rij PR-11); aangenomen zodra de T0 PR-11 merget.
**Datum:** 2026-11-10
**Besloten door:** de operator op 07-11-2026 (besluit 2 en 3 hieronder letterlijk) en het plan `claudedocs/2026-11-07-naar-1-9/plannen/intelligence-effect-measurable.md` (revisie 2, 08-11-2026, plan-gate van 08-11 en de bindende voorwaarde van de tiebreaker) voor alle besluiten. ADR-069 was vrij op `origin/main` `b0bbe3da7` (10-11-2026): de hoogste bestaande is ADR-065, en 064 en 066 tot en met 068 en 070 zijn aan andere leveringen toegewezen. De plan gebruikte het werknummer ADR-056; dat nummer is door vijf andere plannen geclaimd en hier is het 069.
**Cross-refs:** ADR-007 (samengestelde sleutel over `project_id`), ADR-049 (de terminale dispatch-toestanden en hun overgangen), ADR-052 (geen functie boven 200 uitvoerbare regels), ADR-063 (dagelijkse doorloopmeting: dezelfde scheiding tussen meten, voorstel en oordeel), ADR-065 (leren is voorstellen).

De operator heeft op 07-11-2026 besloten, onder meer:

> **2.** zelfstandigheid-meten: T0 promoveert leveringen van een cleared plan; leren en dreaming blijven voorstellen.
>
> **3.** Learning-tracks ontparkeren zodra ledger-is-falsifiable gemerged is; corpus-quarantaine omkeerbaar (verplaatsen, niet wissen).

Dit ADR legt het meetprotocol van de effectmeting vast vóór het venster opent: welke arm een dispatch krijgt en hoe die arm wordt berekend, welke configuratie en welk cohort bevroren zijn, welke uitkomsten als succes, mislukking, uitgesloten of onopgelost tellen en wanneer een dispatch meetelt, welke schatter en strata het verschil dragen, de ene harde drempel, de vier verdictklassen onder het plafond van het eindpunt, de falsificaties en secundaire maten met hun dekkingscontract, het v38-herstelcontract van de migratie, de vensterdatums en de sluitregel. Het bevat geen code; alles erin is een regel over data en over het commando dat die data leest. Dit ADR is een eigen levering (PR-11): alle besluiten staan in het plan en dit document transcribeert ze, het voegt er geen besluit aan toe.

**Deterministisch, en waarom er geen model in zit.** Alles wat dit ADR vastlegt is deterministisch: de arm is een SHA-256 over vier strings, de configuratieparser is een gesloten veldenlijst, de uitkomstklassen zijn een vaste tabel over `DISPATCH_STATES`, de rijping is een vergelijking met `injected_at + 72 uur`, de schatter is een Mantel-Haenszel-gewogen verschil met een gesloten variantieformule, de verdictklassen zijn vier intervallen op één getal, en de controles zijn gelijkheids- en drempeltoetsen. Elk van die delen is een pure functie of een vaste tabel en daarom testbaar en reproduceerbaar op fixtures. Een model heeft hier geen rol: er is geen open natuurlijke taal in de meetweg, geen oordeel onder ambiguïteit en geen synthese over ongestructureerde bronnen. De enige vrije tekst is de reden die de operator bij het openen en sluiten van het venster typt; die wordt opgeslagen en getoond, nooit geïnterpreteerd. De veiligheidsnetten zijn daarom gesloten: een ontbrekende, ongeldige of onbekende waarde valt naar `not_evaluable`, `unknown` of `geen verdict`, nooit naar succes. Waar het plan een deterministische uitkomst boven een duurdere vraagt, is dat dezelfde regel: er is geen model dat 5 procentpunt extra had kunnen geven dat de reden van dit protocol is.

## Context

**1. Er wordt aankomst gemeten, geen effect.** Het systeem telt hoeveel intelligence er wordt aangeboden (`dispatch_pattern_offered`, 3909 rijen op 08-11) en hoeveel injecties er zijn (`intelligence_injections`, 8252 rijen over 5503 dispatch-id's), maar geen enkel commando vergelijkt de uitkomst van een dispatch met en zonder injectie. Er is geen arm, geen cohort en geen uitkomstmaat in één lezing.

**2. De arm bereikt de levende injectieweg niet, en de kolom bestaat niet.** De levende keten loopt `envelope_prepare._prepare` naar `skill_context._inject_skill_context`, `intelligence_injection.fetch_intelligence_section` en `selector.select(...)`; in die keten komt de armfunctie niet voor. `InjectionResult.ab_arm` heeft default `"treatment"`, dus elke echte dispatch boekt "treatment" ongeacht enige vlag, en `ab_arm='control'` staat 0 keer in de store. `intelligence_injections` in `runtime_coordination.db` heeft geen `ab_arm`; het SQL-bestand van 2026_05 heeft de `ALTER TABLE` in commentaar en de Python-guard bestaat niet. De schrijver verbergt dat falen: ontbreekt de kolom, dan valt hij terug op een INSERT zonder arm, zonder fout en zonder waarschuwing.

**3. Het venster is niet begrensd en de geschiedenis zou stil herlabelen.** Een vrije vlag zonder bevroren configuratie laat elke dispatch toetreden tot een cohort dat nergens is vastgelegd. De oude notitie stelt `DEFAULT 'treatment'` voor en `intelligence_ab_report.py` leest `COALESCE(i.ab_arm, 'treatment')`: bij 8252 bestaande rijen zou elke rij zonder arm als behandeling tellen, en elk effectcijfer daarna is onbruikbaar.

**4. Het plafond van het eindpunt is 97,7 procent.** Het ontwerp van 20-09 rekende met ~30 procent herwerk en een effect van 5 procentpunt. Voor `completed` op de deursleutel is de baseline 543 van 556 (97,7 procent): een winst van +5 procentpunt is onmogelijk, het plafond is 2,3 procentpunt. Een protocol dat alleen "winst van 5 punten" als succes kent, kan dus alleen falen. Daarom zijn er vier verdictklassen, waarvan gelijkwaardigheid en onbeslist geldige uitkomsten zijn.

**5. Zonder vaste drempel is de verleiding groter dan de regel.** Een vlag `--min-per-arm` of een deadline die een verdict afdwingt, maakt een tussenstand tot een uitspraak. De tiebreaker van 08-11 maakte de drempel van 200 opgeloste dispatches per arm tot een bindende voorwaarde: die grens mag nooit omlaag, en een deadline sluit niets.

## Besluit

### 1. De arm is een hash, geen `random()` (besluit 1)

`arm_for_dispatch(dispatch_id, project_id, window)` berekent `sha256(f"{window_id}:{salt}:{project_id}:{dispatch_id}")`, neemt de eerste 8 hex-tekens als geheel getal, deelt door 2^32 en geeft `control` als de uitkomst onder `control_fraction` ligt, anders `treatment`. De functie geeft `(arm, ab_window)` terug, met `ab_window = f"{window_id}:{config_sha}"` en `config_sha` de eerste 16 hex-tekens van de SHA-256 over de canonieke JSON van het venster. Reden: alleen een hash is reconstrueerbaar en geeft in twee processen dezelfde uitkomst; `project_id` in de invoer houdt tenants gescheiden (ADR-007). `_ab_arm()` en `AB_CONTROL_FRACTION` vervallen; beide aanroepers gaan naar de nieuwe functie.

### 2. De fractie is 50/50, niet 10 procent (besluit 2)

De gemeten aanwas is 140, 192 en 226 deur-dispatches per week. Bij 50/50 is dat 70 tot 113 per arm per week; het venster duurt dan twee tot drie weken plus de rijpingstijd van 72 uur voor 200 opgeloste per arm. Bij 10 procent duurt de controlearm alleen al 8 tot 10 weken. De prijs van 50/50 is dat de helft van de dispatches tijdens het venster geen intelligence krijgt. Die keuze is tijdelijk en met één configuratieregel terug te draaien. De fractie staat in de bevroren configuratie (besluit 3 hieronder).

### 3. Het bevroren venster en het cohort (besluit 14)

- Eén sleutel `VNX_INTEL_AB_WINDOW` in `config_registry.py` (type string, default leeg) draagt een JSON-object met precies de velden `window_id`, `salt`, `control_fraction`, `opened_utc` en `hash_version` (waarde 1). De parser is strikt: een ontbrekend veld, een onbekend veld, een fractie buiten (0,1), een ongeldige `hash_version` of ongeldige JSON is `invalid`.
- De sleutel wordt per `window_id` één keer geschreven en nooit gewijzigd. Een andere fractie of salt is een nieuw venster met een nieuw `window_id` en dus een nieuw cohort. De audit-tabel `project_config_audit` bewijst dat: voor de sleutel mag er per `window_id` precies één regel zijn.
- Bij `VNX_INTEL_AB_TEST != 1`, een ontbrekende of een ongeldige configuratie is het resultaat `(None, None)`: de dispatch krijgt zijn gewone injectie, de rij blijft ongestempeld en het cohort telt hem niet mee. Er wordt nooit stil `treatment` toegewezen.
- Het cohort is exact: rijen in `intelligence_injections` van het project met `ab_window` beginnend op `<window_id>:`, binnen `[opened_utc, as_of]`. Elke rij na `opened_utc` zonder `ab_window` is `unstamped_after_open` en het commando weigert bij één zo'n rij.
- Herberekenbaarheid: het commando haalt de configuratie uit `project_config`, herberekent de arm van elke cohortrij en vergelijkt. Eén afwijking is `assignment_not_reproducible` (exit 3). Meer dan één verschillende `ab_window` binnen het cohort is `config_diverged` (exit 3). Een armwaarde buiten `control` en `treatment` (ook `NULL` of `placebo`) is `arm_invalid` (exit 3). De placeboarm staat uit in het venster.

### 4. De uitkomstklassen volgen ADR-049 en zijn fail-closed (besluit 5)

De klassen zijn vast en volledig:

- `success`: `completed`.
- `failure`: `expired`, `dead_letter`. Dit zijn de terminale mislukkingen uit `TERMINAL_DISPATCH_STATES` (`scripts/lib/coordination_db.py:50`).
- `excluded`: `withdrawn`. Terminaal, maar nooit gebouwd: geen succes en geen mislukking. Wordt geteld en gerapporteerd.
- `unresolved`: elke andere staat uit `DISPATCH_STATES`: `proposed`, `ready`, `queued`, `claimed`, `delivering`, `accepted`, `running`, `timed_out`, `failed_delivery`, `recovered`. `failed_delivery` en `timed_out` kunnen nog herstellen (`DISPATCH_TRANSITIONS`) en zijn daarom nooit een mislukking.
- `unknown`: een staat buiten `DISPATCH_STATES`, een `NULL`-staat of een ontbrekende `dispatches`-rij. Telt nooit als succes, nooit als mislukking.

De bouwer importeert `TERMINAL_DISPATCH_STATES` en `DISPATCH_STATES` uit `coordination_db`; er komt geen tweede kopie. Een nieuwe staat in die module valt automatisch in `unknown` tot hij hier is ingedeeld, en het cohortcommando weigert dan (`exit 3`, reden `unknown_states`).

### 5. De rijping, de noemers en de differentiële uitval (besluit 16)

- **Rijping.** Een dispatch telt mee zodra `injected_at + 72 uur <= as_of`. De horizon van 72 uur volgt uit de gemeten laten: `completed` maximaal 15,6 uur, `expired` maximaal 11,6 uur. `dead_letter` loopt tot 159 uur; zo'n late mislukking wordt meegenomen als ze op `as_of` al in de store staat. De overige late dead-letters blijven zichtbaar als `unresolved` en vallen onder de uitvalcontrole hieronder.
- **Noemer per arm (`n`).** Gerijpte dispatches in de klassen `success` en `failure`. `excluded`, `unresolved` en `unknown` vallen buiten `n` en worden per arm gerapporteerd, evenals `immature` (nog niet gerijpt).
- **Differentiële uitval.** Het aandeel `unresolved` plus `unknown` onder de gerijpte dispatches per arm. Verschilt dat tussen de armen meer dan 5 procentpunt, dan is de uitkomst `geen verdict` (reden `differential_attrition`, `exit 3`): een arm waarin dispatches vaker blijven hangen, scheeft de opgeloste groep.
- **Schaduwtelling.** Een deur-dispatch zonder `dispatches`-rij of zonder injectierij staat in de dekkingsregel en wordt nooit weggelaten.

### 6. Eén rij per dispatch (besluit 6)

De ontdubbeling is één rij per `(project_id, dispatch_id)`: de eerste injectierij, `min(injected_at)` met tiebreak `min(id)`. Gemeten sinds 2026-10-01 hebben 14 van de 940 dispatch-id's meer dan één injectierij. Het aantal ontdubbelde rijen staat naast het verdict.

### 7. De schatter en de strata (besluit 15)

Een deterministische gestratificeerde risicoverschil-schatter, alleen met de standaardbibliotheek.

- **Eenheid.** Een opgeloste, gerijpte dispatch. Het verschil is `d = p_behandeling - p_controle`; positief is gunstig voor injectie.
- **Strata.** `task_class` van de eerste injectierij, gekruist met de ISO-week van `injected_at`. De ISO-week wordt in Python berekend (`date.isocalendar()`), niet in SQL: een drukke week of een andere taakklasse mag het beeld niet scheeftrekken. Rol is geen stratum.
- **Per stratum `s`:** `n_C,s`, `n_T,s`, `p_C,s`, `p_T,s`. Gewicht `w_s = n_C,s * n_T,s / (n_C,s + n_T,s)` (Mantel-Haenszel). Schatting `d = sum(w_s * d_s) / sum(w_s)`.
- **Variantie:** `sum(w_s^2 * (v_T,s + v_C,s)) / (sum(w_s))^2` met `v = p~(1 - p~) / n` en `p~ = (a + 2) / (n + 4)` (Agresti-Coull-correctie, om een nulvariantie bij 100 procent succes in een stratum te voorkomen). Het interval is `d ± 1,96 * sqrt(variantie)`. Bij `n = 200` per arm en `p = 0,977` is de halve breedte 2,96 procentpunt.
- **Vervallen strata.** Een stratum met een lege arm valt uit de schatting. Het aantal en het aandeel dispatches in vervallen strata staat in de uitvoer. Valt meer dan 10 procent van de opgeloste dispatches in vervallen strata, dan is de uitkomst `geen verdict` (reden `strata_coverage`, `exit 3`).
- **Controlegetal.** De ongestratificeerde ruwe verschil-schatting staat ernaast en verandert het verdict nooit.

### 8. De ene harde regel (besluit 7, woordelijk)

Deze tekst staat woordelijk gelijk in PR-6, PR-7, dit ADR en `## Done measurement` van het plan:

> **HARDE GRENS: een verdict bestaat alleen bij n ≥ 200 opgeloste dispatches per arm. De grens is de constante `MIN_PER_ARM = 200` in de code en is door geen vlag, omgevingsvariabele of configuratierij te verlagen. De deadline 01-12-2026 verlengt het venster en houdt de track open. Onder de grens is de uitkomst altijd `geen verdict` (exit 3, reden `below_floor`), nooit een verdict bij lagere n.**

De gevolgen vallen allemaal binnen die ene regel: het commando kent geen `--min-per-arm` (de vlag is verwijderd; opgeven geeft argparse `exit 2`); `MIN_PER_ARM` mag omhoog door een codewijziging met ADR-wijziging, nooit omlaag; er is geen tweede getal voor "een tussenstand" (de tussenstand van 17-11 is het commando `cohort`, dat alleen aantallen toont); een deadline sluit niets, alleen het verdict sluit de track. `n` is het aantal opgeloste dispatches per arm.

### 9. De vier verdictklassen en het plafond (besluit 21)

Bij `n >= 200` per arm en alle controles groen is de uitkomst één van vier klassen op het interval van besluit 7, met marge `M = 5 procentpunt` (het getal uit het ontwerp van 20-09, vastgelegd vóór het venster):

- `treatment_better`: ondergrens > 0.
- `control_better`: bovengrens < 0 (falsificatie F3).
- `equivalent_within_5pp`: het interval ligt volledig binnen (-5, +5) procentpunt en bevat 0.
- `inconclusive`: alles anders.

Vallen ze samen, dan geldt de volgorde `treatment_better`, `control_better`, `equivalent_within_5pp`, `inconclusive`; de uitvoer toont alle ware vlaggen. Elke klasse is een geldig verdict en sluit de track. Een `inconclusive` is een gemeten uitkomst ("geen aantoonbaar effect van 5 procentpunt of meer"), geen falen.

**Het plafond.** De baseline op `completed` is 543 van 556 (97,7 procent), gemeten op 08-11. Een winst van 5 procentpunt is dus onmogelijk; de maximale winst is 2,3 procentpunt. Het protocol kiest daarom 5 procentpunt als marge voor schade en gelijkwaardigheid, niet als winstdrempel. Met `n = 200` per arm is de halve breedte van het 95 procent-interval 2,96 procentpunt: het eindpunt kan schade van 5 procentpunt of meer opsporen (power 0,65) en gelijkwaardigheid binnen ±5 procentpunt aantonen (kans 0,82 bij een waar verschil van 0). Een winstdrempel zou een ander eindpunt met ruimte vragen, zoals de duur van opgeloste dispatches; dat valt buiten dit protocol.

### 10. De falsificaties en de secundaire maten (besluit 19)

Een subcommando `secondary` rekent ze. Ze wijzigen het verdict nooit en de exitcode nooit; het zijn gerapporteerde regels met een eigen status `fired`, `clear` of `not_evaluable`.

| Maat | Bron en sleutel | Noemer en aggregatie | Minimale dekking | Bij ontbrekende data |
|---|---|---|---|---|
| F1: adoptie stijgt zonder effect | QI `pattern_injection_outcome` (`used`, UNIQUE `(project_id, dispatch_id, pattern_id)`) gejoind op `dispatch_pattern_offered` (PK `(dispatch_id, pattern_id, project_id)`), alleen voor cohort-dispatches uit RC met arm `treatment`; de id-lijst komt uit RC, geen ATTACH | per ISO-week `sum(used) / count(offers met een outcome-rij)` | een week is evalueerbaar bij >= 30 offers met outcome en een dekking van >= 50 procent | `not_evaluable` met de dekking per week; bij minder dan 2 evalueerbare weken geen F1 |
| | | `fired` als `adoptie(laatste week) - adoptie(eerste week) >= +5 procentpunt` en het primaire verdict niet `treatment_better` is | | |
| F2: adoptie stijgt door verwatering | RC `intelligence_injections` (`payload_chars`, `items_injected`), cohort, arm `treatment`, rijen met `items_injected > 0` | per ISO-week `sum(payload_chars) / sum(items_injected)` (tekens per item) | zelfde weekregel als F1 | `not_evaluable` |
| | | `fired` als F1 `fired` is en tekens per item van de eerste naar de laatste evalueerbare week met >= 10 procent daalt | | |
| F3: controle presteert beter | het primaire interval uit besluit 7 | `fired` als de bovengrens van het interval < 0 is (verdictklasse `control_better`) | n >= 200 per arm | geen verdict, dus `not_evaluable` |
| CQS | QI `dispatch_metadata` (`cqs`, `project_id`), gejoind op `(project_id, dispatch_id)` met opgeloste cohort-dispatches | gemiddelde `cqs` per arm; eenheid is de dispatch; rijen zonder `cqs` of zonder `dispatch_metadata`-rij tellen mee in de dekking en niet in het gemiddelde | dekking >= 80 procent van de opgeloste dispatches in beide armen | `not_evaluable (dekking x procent)`: vandaag 0 `dlv-*` van 5764 |
| Open items geopend en gesloten | QI `dispatch_metadata` (`open_items_created`, `open_items_resolved`) | gemiddelde per dispatch per arm | idem | idem |

CQS en open items zijn voor de deursleutel nu `not_evaluable`: `dispatch_metadata` heeft `cqs` in 3113 van 5764 rijen en 0 `dlv-*`. Dat is een gemeten dekking en geen falen; het secundaire commando meldt het percentage en wijzigt het verdict niet.

### 11. Het v38-herstelcontract (besluit 20)

- **Paired runner, geen pure SQL.** `scripts/lib/migrations/apply_0038.py` controleert per kolom `PRAGMA table_info` en voegt `ab_arm TEXT` en `ab_window TEXT` alleen toe als ze ontbreken, maakt het index met `IF NOT EXISTS` en stempelt `user_version` 38 via `schema_migration.apply_if_below`, in `BEGIN IMMEDIATE`, zoals `apply_0037.py`. Reden: de pure-SQL-poort kijkt alleen naar `user_version`; een store die de kolom al heeft, zou `duplicate column name` geven en elke T0-bootstrap valt daarna opnieuw om.
- **Geen `DEFAULT`.** Beide kolommen zijn nullable zonder default. `NULL` betekent `niet toegewezen`. De `DEFAULT 'treatment'` uit de oude notitie zou 8252 historische rijen herlabelen.
- **`runtime_schema_version` blijft onaangeroerd** (max 15). De legacy-teller kent geen v16 en 0038 stempelt hem niet; een test legt dat vast, zodat up en down elkaars spiegel zijn.
- **Echte terugweg.** `0038_intelligence_injections_ab_arm_down.sql` en `_revert()` in de runner: eis `user_version == 38` (anders `Migration0038Error`), drop het index, drop `ab_window` en `ab_arm`, stempel 37. Geen rij verplaatst. De armgeschiedenis gaat bij een down verloren; het nulpunt is een verse back-up vlak voor de up.
- **De 2026_05-paar blijft staan en is gemarkeerd als SUPERSEDED.** Alleen de kop verwijst naar 0038; geen uitvoerbare regel wijzigt. Het contract: `2026_05_intelligence_ab_arm_down.sql` wordt nooit op een store met `user_version >= 38` uitgevoerd.
- **De probe.** `scripts/lib/schema_manifest.py` krijgt `manifest[38]`: de tabellen van v37 met `intelligence_injections` uitgebreid met `ab_arm` en `ab_window` (TEXT, nullable) en het index `idx_injection_window_arm`. Daarmee ziet `migration_effectiveness_probe` een store die 38 claimt zonder de kolommen.
- **Rondrit.** Een test loopt 37 naar 38 naar 37 naar 38 op een store met bestaande rijen: elke andere kolom en rij blijft gelijk, `runtime_schema_version` blijft 15, het manifest meldt nul schendingen op 38.

### 12. Eén store, projectgescoped (besluiten 4 en 17, ADR-007)

De arm (`intelligence_injections`) en de uitkomst (`dispatches`) staan samen in `runtime_coordination.db`, op `(dispatch_id, project_id)`. Een ATTACH naar `quality_intelligence.db` is voor het effect niet nodig; de adoptie leest QI met een id-lijst uit RC, zonder ATTACH. De migratie voegt geen tabel toe, dus geen nieuwe UNIQUE of PK. Het nieuwe index is `idx_injection_window_arm ON intelligence_injections (project_id, ab_window, ab_arm)`: `project_id` eerst, zoals ADR-007 voor tenant-scoped indexes vraagt. Elke leesquery filtert `project_id = ?` en joint op `(dispatch_id, project_id)`; `--project-id` krijgt als default `project_root.resolve_project_id()`, nooit een hardgecodeerde `vnx-dev`. Elke rij met een schema- of leescontract heeft een tweeprojecten-test: twee projecten met dezelfde `dispatch_id` mogen elkaars cohort, strata en tellingen niet beïnvloeden.

### 13. De vensterdatums en de sluitregel

- **Openen:** 10-11-2026, na de corpus-quarantaine en na de release, met geauditeerde configuratierijen: eerst `VNX_INTEL_AB_WINDOW` eenmalig met een nieuw `window_id` en `salt`, daarna `VNX_INJECTION_WHY_ENABLED=1` en `VNX_INTEL_AB_TEST=1`. Elke rij krijgt een `project_config_audit`-regel.
- **Tussenstand:** 17-11-2026, alleen het commando `cohort` (aantallen en controles, geen verdict).
- **Eerste toetsmoment:** 24-11-2026 met `effect`. Haalt een arm dan de 200 opgeloste niet, dan loopt het venster door en geeft `effect` `geen verdict`. Van dag 14 af toetst de T0 wekelijks.
- **Deadline:** 01-12-2026. De deadline verlengt het venster en houdt de track open; ze sluit niets en geeft geen verdict.
- **Sluiten:** zodra `effect` een verdict geeft, gaat `VNX_INTEL_AB_TEST` en `VNX_INJECTION_WHY_ENABLED` op `0`, met een audit-regel erbij. Een datum sluit het venster niet.
- **Sluitregel.** De track sluit op het verdict, met `window_id`, de vensterdatums, het commit-bereik en de uitvoer van `cohort`, `effect` en `secondary` erbij, en niet op het feit dat de code gemerged is. Elke van de vier verdictklassen telt als done. Exit 3 (`below_floor` of een andere reden) betekent dat de track niet sluit en het venster doorloopt, ook na 24-11 en ook na 01-12. Wordt het venster op een latere dag geopend, dan schuiven alle data mee.

## Consequenties

- Het venster is begrensd en reconstrueerbaar: elke cohortrij draagt zijn `ab_window`, de configuratie is één keer geschreven en de audit-tabel bewijst het. Een verkeerde of gewijzigde arm is een `exit 3` met een reden, geen plausibel cijfer.
- De helft van de dispatches krijgt tijdens het venster geen intelligence. Dat is tijdelijk en met één configuratierij te beëindigen; de prijs is bewust genomen omdat 10 procent de controlearm 8 tot 10 weken kost.
- Een onvolledige meting leest nooit als succes. Ontbrekende kolommen, ongestempelde rijen na het openen, onbekende staten, vervallen strata en differentiële uitval zijn elk een blokkade met een reden.
- `inconclusive` en `equivalent_within_5pp` zijn geldige, sluitende uitkomsten op een eindpunt met een plafond van 2,3 procentpunt. Zonder die klassen zou het protocol op deze baseline alleen kunnen falen.
- De migratie is omkeerbaar zonder rijverlies; alleen de armgeschiedenis gaat bij een down verloren, en het nulpunt is een verse back-up vlak voor de up.
- De armgeschiedenis van vóór het venster blijft `NULL` (`niet toegewezen`) en valt buiten het cohort. Er is geen default en geen `COALESCE` die dat verbergt.

## Wat dit ADR niet doet

Het wijzigt geen code, geen vloot-default en geen receipt-formaat. `VNX_INJECTION_WHY_ENABLED`, `VNX_INTEL_AB_TEST` en `VNX_INTEL_AB_WINDOW` blijven standaard uit of leeg; het openen en sluiten van het venster is een operatorstap met een audit-regel. Het opent het venster niet en draait de meting niet: de uitvoering staat in PR-1 tot en met PR-12 en in `## Operator steps` van het plan. Het vervangt de code-rijen die het protocol dragen niet, en het voegt geen vijfde verdictklasse, geen tweede drempel en geen model toe.
