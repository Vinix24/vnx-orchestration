# ADR-064 - De deltareview: een vers oordeel op de exacte kop, de ouderketen in het grootboek, schaduw eerst en één profielschakelaar terug

**Status:** Voorgesteld 10-11-2026 (T0, track `deltareview-met-ouderketen`, levering DR-0); aangenomen zodra de T0 DR-0 merget.
**Datum:** 2026-11-10
**Besloten door:** de operator ("zo snel mogelijk, want daar profiteren alle volgende tracks van", 09-11-2026); het plan `claudedocs/2026-11-07-naar-1-9/plannen/deltareview-met-ouderketen.md` (revisie 2) voor de besluiten 1 tot en met 6 en de tien eisen; de plan-gate van 09-11 (codex REVISE, tiebreaker deepseek-v4-pro START) voor de eisen V en C1 tot en met C8. ADR-064 was vrij op `origin/main` (10-11-2026): ADR-065 en ADR-066 bestaan al, 064 was overgeslagen.
**Cross-refs:** ADR-060 (poortprofiel; dit ADR voegt daar de sleutel `VNX_REVIEW_SCOPE` aan toe, zie het amendement in dat ADR), ADR-007 (project_id in elke sleutel), ADR-046 (één poortmotor), ADR-061 (de deur is afgedwongen), ADR-065 (leren is voorstellen).

## Context

Een vervolgronde kost vandaag evenveel als de eerste. Na een fix krijgt de codex-stoel de volle diff van `origin/main...branch` opnieuw, zonder te weten wat hij in de vorige ronde vond en wat er sindsdien veranderde. Hij leest alles opnieuw en vindt soms iets nieuws in code die al goedgekeurd was. Dat geeft een nieuwe ronde.

Gemeten op 09-11-2026 van 00:00Z tot 19:45Z (`scripts/pr_grootte_meting.py`, 42 gemergde PR's): 79 codex-rondes, 5,38 rondes per 1000 regels (nulmeting 08-11: 4,23). In de klasse 300-450 regels gemiddeld 1,59 rondes per PR, in de klasse 451 en hoger 4,20. Het panel van 08-11 zag hetzelfde: rondes en wachten domineren de uren, niet de poort zelf.

Een vervolgronde moet de vraag beantwoorden die er ligt: zijn de open bevindingen opgelost en heeft de fix iets gebroken? Dat kan met de delta als focus, op voorwaarde dat het oordeel een vers oordeel op de exacte kop blijft. Het deterministische deel (diff, patch-id, ankers, ids, boeking) staat in code. Het enige modeldeel is de beoordeling zelf door de codex-stoel: open natuurlijke taal over code, daar is geen regel voor. Dat deel krijgt een vangnet: de parser dwingt statussen af, een id zonder antwoord telt als `open`, en elke uitvoering staat in een request-record.

## Uitgangspunten

### U1. Het mechanisme overnemen, niet de code

- Handhaving blijft bij GitHub en de deur: branch protection, verplichte checks, `vnx-gate/door` en PASS per stoel op de exacte kop (`review_seat_evaluation.on_head`). Deze track verandert die eis niet.
- Een beslisregel over de geldigheid van een review wordt een declaratieve regel in het poortprofiel (`gate_profile.resolve`, ADR-060), met geleende semantiek en bron (zie "Geleende semantiek").
- Pure berekening gebruikt git-primitieven: `git patch-id --stable`, `git range-diff`, `git merge-tree`, `git merge-base`. Geen framework wordt geïmporteerd. Code van buiten komt er alleen na een licentiecheck.

### U2. De audit trail beslist

De harde toets: een auditor herleidt uit het grootboek alleen waarom de PR gemerged is, anders gaat het mechanisme er niet in. Per mechanisme:

| Mechanisme | Wat het receipt vastlegt | Waar |
|---|---|---|
| Deltareview (fase 1) | `scope: delta` of `full`, `parent_head_sha`, `parent_round_ref`, `delta_base`, de open finding-ids die meegingen, `changekind`, de patch-id van beide koppen, de reden van een gedwongen volle review | velden op de bestaande verdictregel in `review_gates/rounds.ndjson` plus het request-record per kop |
| Overgeërfde PASS (fase 2, schaduw) | `verdict_origin: carried`, `carried_from`, `carry_rule`, `carry_rule_version`, beide patch-ids, `security_paths_touched`, `plan_scope_rule_version` | velden op de verdictregel |
| Pruning (fase 2, schaduw) | `unchanged: true` plus de finding-set-hash | veld op de bestaande ronde |

Een overgeërfde PASS staat nooit als verse PASS in het receipt en een beveiligingspad erft nooit.

De audit-impactstap (`20261108-res-graafaudit`) oordeelde: `changekind` met input-set en beveiligingspadlijst (mechanisme 1) en pruning (mechanisme 3) zijn herleidbaar onder voorwaarden. De reuse-sleutel met ouderketen (mechanisme 2) is dat niet, want het grootboek kent geen stapel: nul regels noemen een lagere PR. Mechanisme 2 valt buiten deze track tot er een veld `stack_parents` bestaat. De reconstructietoets op #2457 beantwoordde vier van de zes vragen. De twee gaten: welke boom gemerged is (`pr_merged` draagt geen merge- of squash-sha) en wat de tweede ronde zag (geen request-record voor die kop). Besluit 4 en eis 9 dichten ze.

## Besluit

### Besluit 1. Een deltareview is een vers oordeel op de exacte kop

De codex-stoel krijgt op een vervolgkop dezelfde worktree op de exacte kop als vandaag, met volle leestoegang tot de repo. Wat verandert is de opdracht. Bovenaan staan de ouderketen (vorige kop, vorig verdict, open bevindingen met hun stabiele id), de interdiff tussen de eigen diff van de vorige kop en die van de nieuwe kop, en de vraag per open bevinding: opgelost, nog open of geregresseerd. Daarna beoordeelt hij de delta én elke aanroeper van wat de delta raakt, niet alleen de gewijzigde regels. Een gewijzigde aanroeper kan ongewijzigde code verkeerd gebruiken.

Het verdict geldt voor de kop, zoals nu. De deur erft niets en houdt kop-gelijkheid. Op het productiepad (fase 2) draait de codex-stoel in `gate_obligation_runner` de beslissende deltaronde zelf, via dezelfde opdrachtbouwer als de schaduwrun (eis V uit de plan-gate).

*Reden:* een verdict dat niet op de exacte kop is gevormd, is een geërfd verdict. Dat mag alleen onder besluit 6 en eis 2 en nooit via de leesruimte van de reviewer.

### Besluit 2. Wanneer de volle review gedwongen is

Een vervolgronde is alleen delta wanneer al deze voorwaarden gelden. Anders is het een volle review met de reden in het record (`full_review_reason`):

- er is een verdictregel van dezelfde stoel op een eerdere kop van dezelfde PR (de ouder), met een request-record;
- de merge-base is gelijk, of `changekind` van de rebase is `NO_CODE_CHANGE` of `TRIVIAL_REBASE` volgens eis 1 en 2;
- de interdiff raakt geen beveiligingspad (eis 3);
- elk anker van een open bevinding bestaat nog (pad plus symbool). Een verdwenen anker maakt die bevinding `unverifiable` en dwingt de volle review af;
- het aantal deltarondes op rij is kleiner dan `VNX_REVIEW_DELTA_MAX_CHAIN` (standaard 2). De derde vervolgronde is weer vol, zodat de ketting niet onbeperkt op de eerste volle lezing leunt.

De tweede stoel (tweede-stoel-op-signaal) blijft in fase 1 en 2 altijd een volle review.

*Reden:* bij elke twijfel kiest de regel de duurdere en veiligere uitkomst. Dat is dezelfde richting als fail-expand bij Turborepo en als `gate_reanchor`.

### Besluit 3. Stabiele finding-ids

`finding_id = sha256(seat, pad, symbolische context, genormaliseerde tekst)[:16]`, zonder regelnummers. De symbolische context is de omsluitende functie of klasse uit de bestaande parse van `gate_reanchor`, anders het pad alleen. Status per id: `open`, `fixed`, `waived`, `regressed`, `unverifiable`. De codex-uitvoer van een deltaronde noemt per meegegeven id zijn status. Een id zonder antwoord telt als `open`. Een bevinding sluit nooit stil.

De finding-ids van elke volle ronde staan op de verdictregel (C2). Een deltaverdict kan geen PASS zijn zolang een meegegeven blocker-id `open`, `regressed` of `unverifiable` is: de parser zet het verdict dan op FAIL.

*Reden:* een regelnummer verschuift bij elke fix, een tekst-hash zonder context botst. Pad plus symbool is het stabielste anker dat deterministisch te berekenen is.

### Besluit 4. De ouderketen leeft in het grootboek, niet ernaast

Alle nieuwe gegevens zijn optionele velden op de bestaande `RoundLine` (`scripts/lib/review_rounds.py`) en op het request-record. Een lezer die ze negeert gedraagt zich als vandaag. Geen nieuwe `kind`, geen nieuw bestand voor productieve gegevens (eis 6).

Per uitvoering schrijft de aanvraagkant een request-record dat nooit overschreven wordt (`requests/<pr>-<seat>-<head>-<n>.json`), op elk aanvraagpad: de runner, `gate_request_handler` en de overname (C1). Dat dicht het tweede gat van #2457. De schaduwronde van fase 1 boekt in `review_gates/shadow/delta.ndjson`; carry en pruning boeken alleen in `review_gates/shadow/`. De deur, `read_ledger` en `codex_rounds` lezen die map nooit (C4).

*Reden:* de reconstructie moet uit één grootboek kunnen. Een tweede bron is precies de drift die het grootboek moet voorkomen.

### Besluit 5. Schaduw eerst, dan de schakelaar

Fase 1 draait de deltaronde naast de volle ronde op dezelfde kop: de volle ronde beslist, de delta wordt alleen geboekt. Na minstens 20 paren vergelijkt `vnx review delta-compare` de finding-sets. De drempel staat onder "Drempel". Pas dan zet de profielsleutel `VNX_REVIEW_SCOPE` de delta aan, met een approval-id. De schakelaar staat onder "De schakelaar". Rollback is die sleutel op `full`.

De schaduw kost tijdelijk een extra codex-run per vervolgronde. Met codex op 65% van het weekquotum draait de schaduw alleen op vervolgrondes van medium-PR's en stopt hij zodra 20 paren geteld zijn.

*Reden:* een poortwijziging bewijst zichzelf niet. De meting naast de lopende poort kost quotum maar niets aan veiligheid.

### Besluit 6. Overname en overerving blijven gescheiden

Een overname vult een stoel op dezelfde kop met een andere reviewer (`takeover_from`). Een deltaronde is een nieuw oordeel van die stoel op een nieuwe kop. Een overgeërfde PASS (fase 2, schaduw) neemt stoel en reviewer van de bronregel over en vult nooit een andere stoel. Een overname vult nooit een stoel met een oordeel van een andere kop. Beide krijgen een test (DR-3 en DR-8a).

*Reden:* wie de twee vermengt, kan achteraf niet meer zeggen of een PASS is gevormd, overgenomen of geërfd.

## De tien eisen

Deze eisen komen uit de audit-impactstap en zijn bindend voor elke levering van de track.

1. **Eis 1.** Bewijs is `git patch-id --stable` per kop, door VNX berekend en opgelost vóór de lock op het grootboek (OI-2202). `contract_hash` is nooit het bewijs.
2. **Eis 2.** `changekind` in {`NO_CODE_CHANGE`, `TRIVIAL_REBASE`} op de eigen diff, plus een lege doorsnede met de input-set van de stoel, plus geen beveiligingspad. Al het andere is `REWORK`. De input-set is de eigen diff plus de bestanden die op diepte 1 een symbool uit die diff bereiken, berekend met de bestaande bereikbaarheid van `gate_reanchor` (C3). `cannot_prove` is `REWORK`.
3. **Eis 3.** De beveiligingspadlijst is de bestaande plan-scope-tabel (`smart_router.security_path_kind`). `plan_scope_rule_version()` staat op de regel.
4. **Eis 4.** Elke carry draagt de zeven carry-velden: `verdict_origin`, `carried_from`, `carry_rule`, `carry_rule_version`, de patch-id van bron en eigen kop, `security_paths_touched` en `plan_scope_rule_version`. Ontbreekt er één, dan is de regel ongeldig en weigert de deur.
5. **Eis 5.** Een carried regel telt niet als ronde en is door een verse PASS te vervangen (append-only, de laatste wint).
6. **Eis 6.** Geen nieuwe `kind`, alleen velden op de bestaande verdictregel. Een onbekende `kind` laat de lezer de hele PR weigeren. Dit gaat vóór advies 5 van het research-rapport, dat een nieuwe `kind` voorstelde.
7. **Eis 7.** `stack_parents` komt er pas als er een stapel is. Tot dan vervalt mechanisme 2.
8. **Eis 8.** `unchanged` is een veld op een bestaande ronde en mag alleen de volgende aanvraag overslaan.
9. **Eis 9.** De `pr_merged`-receipt draagt ook de merge- of squash-sha en de geteste boom (DR-7, met een reconstructietest die de zes vragen van #2457 beantwoordt).
10. **Eis 10.** Schaduw eerst: delta, carry en pruning boeken naast de volle ronde. De terugweg is één profielschakelaar die fail-closed naar volle review leest.

De failure modes F1 tot en met F5 van het audit-rapport zijn de testcases van DR-8a: een carry zonder herkomst, een carry die als ronde telt, een carry die een verse ronde op dezelfde kop blokkeert, een apart opgeslagen reuse-sleutel en een rebase die niet van een inhoudswijziging te onderscheiden is. De laatste telt op de patch-id en nooit op de `head_sha`.

## Geleende semantiek en wat we niet overnemen

| Bron | Wat is geleend | Licentie | Waar het landt |
|---|---|---|---|
| Gerrit `copyCondition` en `changekind` | een stem overleeft een triviale rebase; `changekind` onderscheidt `NO_CODE_CHANGE`, `TRIVIAL_REBASE` en `REWORK` | Apache-2.0 | eis 2, berekend op de eigen diff en niet op kop-gelijkheid |
| LUCI CV | een eigen stem per CL; reuse-sleutel op het laagste equivalente patchset | Apache-2.0 | besluit 2 en 6; de ouderketen in de sleutel valt weg tot `stack_parents` bestaat (eis 7) |
| Prow Tide | een resultaat geldt alleen voor de refs waarmee het gebouwd is | Apache-2.0 | kop-gelijkheid blijft bij de deur (besluit 1) |
| Turborepo | een te ondiepe historie maakt alles gewijzigd (fail-expand) | MIT | `cannot_prove` is `REWORK`; onbekend is volle review |
| Nx `affected` | gedeclareerde inputs bepalen wat geraakt is | MIT | de input-set van eis 2 |
| Bazel en Buck2 | change pruning: gelijke herberekende waarde stopt de golf | Apache-2.0 | `unchanged` in eis 8 |
| git | `patch-id --stable`, `range-diff`, `merge-tree`, `merge-base` | GPL-2.0, aangeroepen als proces | alle pure berekening |

Niet overgenomen: **Kodiak** (AGPL-3.0, en VNX rebaset de PR doelbewust niet) en **bors-ng** (gearchiveerd en deprecated; alleen het bisect-idee blijft genoteerd, buiten deze track). Merge queues als geheel ordenen en testen integratie, maar invalideren geen review-oordelen. Bron: `20261108-res-graafpoort`.

## Drempel

De delta gaat pas aan wanneer `vnx review delta-compare` (DR-5) over minstens 20 paren het volgende meldt (C6):

- **0 gemiste echte blockers in 20 paren.** Elk paar uit het venster telt, er is geen keuze. Ketens van diepte 2 tellen apart. Een paar is een volle en een deltaronde op dezelfde kop; een gemiste blocker is een blocker die de volle ronde vond en de delta niet.
- **10 van 10 geplante blockers gevonden** in een set van tien synthetische vervolgkoppen met een bekende geplante blocker.
- Het 95%-interval staat in het meetdocument. Bij 0 missers in 20 is de bovengrens van het tweezijdige exacte interval 16,8%. Dat is de onzekerheid die dit ADR aanvaardt (open vraag 1). Een ander resultaat dan 0 in 20 laat de schakelaar op `delta_shadow`.

## De schakelaar

`VNX_REVIEW_SCOPE` is een profielsleutel met drie waarden:

| Waarde | Gedrag |
|---|---|
| `full` | vandaag: elke ronde is een volle review. Standaard. |
| `delta_shadow` | de volle ronde beslist; de deltaronde draait ernaast en boekt alleen in `review_gates/shadow/delta.ndjson` |
| `delta` | de deltaronde is de beslissende ronde voor een vervolgkop die aan besluit 2 voldoet |

De lezing is fail-closed: een onbekende waarde, een lege waarde en elke leesfout geven `full`. Een `delta` zonder approval-id geeft ook `full`. Het amendement op ADR-060 legt vast wie de sleutel zet en onder welke voorwaarden.

## Rollback

De rollback is de sleutel `VNX_REVIEW_SCOPE` op `full`, zonder revert en zonder migratie. Open verplichtingen blijven staan en geen record wordt herschreven; de volgende aanvraag is weer een volle review. De velden op de verdictregel blijven staan en zijn voor een lezer die ze negeert onschadelijk (eis 6).

## Wat gelijk blijft

- PASS per stoel op de exacte kop is de eis van de deur. De deur erft in fase 1 en 2 niets.
- De tweede stoel is in fase 1 en 2 altijd een volle review.
- Een overname (`takeover_from`) en een overgeërfde PASS blijven gescheiden (besluit 6).
- Een deltaverdict is geen PASS zolang een meegegeven blocker niet gesloten is (besluit 3).

## Open vragen, als genomen besluit

Beantwoord volgens het advies van het plan. Alle drie zijn herzienbaar na de meting.

1. **Drempel:** 20 paren met het interval erbij, daarna meten na 50 actieve deltarondes. Niet 40 paren vooraf.
2. **Tweede stoel:** de delta geldt er niet voor, ook niet zodra tweede-stoel-op-signaal actief is, in fase 1 en 2.
3. **`VNX_REVIEW_DELTA_MAX_CHAIN`:** 2. Omhoog na de meting.

## Wat dit ADR niet doet

Het verandert geen code en geen gedrag. Het bouwt de tien leveringen DR-1 tot en met DR-8b niet; het legt vast waartegen ze bouwen. Het activeert `VNX_REVIEW_SCOPE` niet: de sleutel bestaat pas na DR-6.

## Consequenties

- **Positief:** een vervolgronde stelt de vraag die ligt in plaats van de hele diff opnieuw, en elke ronde is herleidbaar uit het grootboek met de reden van de scope.
- **Positief:** de terugweg is één sleutel. Een mislukte meting kost niets behalve het schaduwquotum.
- **Negatief:** de schaduw kost een extra codex-run per medium-vervolgronde totdat 20 paren geteld zijn.
- **Negatief:** de deltaruns vragen eerst DR-3 (request-record op elk pad). Tot dan is de ouderketen niet te reconstrueren en blijft de schakelaar op `full`.
- **Open:** mechanisme 2 (reuse-sleutel met ouderketen) wacht op `stack_parents`. Een stapel van PR's erft dus niets.
