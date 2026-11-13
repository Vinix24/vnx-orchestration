# ADR-074 - Impactselectie in schaduw: de verzamelingen, het bewijscontract en de haak VNX_CI_IMPACT_CMD

**Status:** Voorgesteld 13-11-2026 (T0, track `impactselectie-in-schaduw`, PR 1 van 6); aangenomen zodra de T0 deze PR merget.
**Datum:** 2026-11-13
**Besloten door:** de operator, 13-11-2026 ~05:50Z (de open vragen van het plan, langs het T0-advies); het plan `claudedocs/specs/impactselectie-in-schaduw/plan.md` (plan-gate 13-11-2026, versie 85525c3187ea, met de verplichte wijziging van de tiebreaker). Het nummer 074 is door de T0 toegewezen.
**Cross-refs:** ADR-007 (bindingsplicht per project; zie het slot van Context), ADR-053 (dezelfde controle in dezelfde blokkerende job op dezelfde boom), ADR-064 (deltareview, dezelfde schaduw-eerst-volgorde), ADR-073 (vensterpoort `main`, het vangnet bij activering), `docs/governance/decisions/ADR-072-operator-bijsturing-leert.md` (opmaak), `claudedocs/2026-11-08-graafpoorten-en-audittrail.md` (uitgangspunten), het panelrapport ontwikkelflow van 08-11-2026 (fase 7, vraag 3).

Dit ADR legt vast waartegen de PR's 2 tot en met 5 van de track bouwen: welke verzamelingen een selectie beschrijven, wat een bewijsstuk moet dragen, hoe de haak per project eruitziet en onder welke voorwaarden een verkleinde PR-run ooit een merge mag dragen. Het bevat geen selector. De berekening van de VNX-selector is deterministisch (Python-AST, padliteralen, `git diff --name-only`). Er komt geen model aan te pas: een afhankelijkheidsgraaf op primitieven dekt dit zonder open taal, en een misser is meetbaar in plaats van een oordeel.

## Context

Elke PR draait de hele suite van Profile A. Gemeten op 13-11-2026: mediaan 15,8 min per run over 40 runs, en 90 PR-CI-minuten per 1000 opgeleverde regels in de klasse 300-450. Een PR die één module raakt draait toch alle testbestanden.

VNX kent twee dingen die op impactselectie lijken en het niet zijn. `classify_ci_profile.py` heeft een doc-allowlist (`light` of `heavy`). `select_test_paths` (`--tests-only`, #2210) kiest alleen gewijzigde testbestanden, voor red-on-base. Een wijziging in productiecode selecteert daar niets: `scripts/lib/dispatch_serialization.py` gaf in het panel `selected: []`.

Een selectie die afhankelijkheden volgt kan de suite per ronde inkorten. Ze mag pas iets beslissen als gemeten is hoeveel failures ze mist. Daarom is dit eerst een meting.

ADR-007 geldt hier niet: er komt geen tabel in de centrale database. Het schaduwgrootboek is een NDJSON-bestand per project.

## Besluit

### 1. Het panelverdict is REFUTED: eerst schaduw, niets beslissen

De claim "ADR-053 plus de gewijzigde-testselector leveren al veilige impactselectie waarmee de volle suite naar later kan" is bij verificatie weerlegd (REFUTED):

- `select_test_paths` is geen afhankelijkheidsselectie. Het berekent geen pad van productiecode naar tests.
- ADR-053 dekt geen uitgestelde run. Het staat alleen dezelfde controle toe in dezelfde blokkerende job op dezelfde boom. Een nachtrun of een run na de merge voldoet daar niet aan.
- Achteraf testen is geen preventieve dekking. Een latere volle run of een revert bewijst niets over een merge die al is toegelaten.

Gevolg: de selectie draait in schaduw naast de volle run en beslist niets. De verplichte check `Profile A` blijft de volle run.

### 2. De verzamelingen

Per kandidaat (geteste SHA tegen een basis) zijn er zes verzamelingen:

| Naam | Inhoud |
|---|---|
| U | de ontdekte testbestanden |
| X | de via `scripts/ci/test_exclusions.txt` uitgezonderde bestanden |
| S | de door de selector gekozen testbestanden |
| E | wat in de run daadwerkelijk is uitgevoerd |
| F | de failures van de volle run |
| F − S | de missers |

De missers staan **bij naam** in het record, nooit alleen als aantal. Een aantal laat een auditor niets narekenen.

**Vergelijking op bestandsniveau.** S bestaat uit testbestanden, JUnit uit test-ids. Een misser is een falende test-id waarvan het bestand niet in S zit. De uitgezonderde bestanden (X) en de verwachte skips in `scripts/ci/xdist_expected_skips.txt` worden ook op bestandsniveau vergeleken. U − X − S blijft zichtbaar als niet getest.

### 3. Het scopeveld

Elk bewijsstuk draagt `scope: full|impact`. De suite-volledigheid en de completeness-controle (`check_xdist_parity.py completeness`) zijn alleen geldig op `full`. Op `impact` staat er "niet gedekt", nooit groen.

Bekende failures zijn scopebewust. K is de verzameling uitzonderingen en verwachte skips. De uitslag wordt vergeleken met K ∩ S. K − S heet `niet onderzocht`, nooit "verdwenen" of "opgelost". De completeness-controle krijgt in PR 3 een variant tegen S.

### 4. De graafversie

De graafversie is een hash over de selector en zijn invoerregels (de expansieregels van besluit 6 en de scanregels voor imports en padliteralen). Samen met de selectorversie maakt ze een selectie reproduceerbaar. Een wijziging aan een regel geeft een andere graafversie, dus ander bewijs.

### 5. De haak `VNX_CI_IMPACT_CMD`

De haak woont in config_registry, per project, niet in een bestand `.vnx/ci.yaml`. Eén bron.

- **Contract:** de geraakte paden gaan erin. De testbestanden plus de graafversie komen eruit, exit 0.
- **Elke andere uitkomst betekent de volle run:** een exit-code anders dan 0, uitvoer die niet te parsen is, een time-out, een lege of ontbrekende graafversie.
- **De VNX-waarde** wijst naar `scripts/ci/impact_select.py` (PR 2).
- **De fabric dwingt af wat eromheen gebeurt** (record, vergelijking, scope, receipt) en schrijft geen selector voor andere projecten. Mission-control en SEOcrawler leveren een eigen `impact_cmd` pas na de meting op vnx-dev, in een latere track.
- De sleutel is `approval=True`, standaard leeg, en heeft tot PR 3 geen leesplek in productiecode.

### 6. De expansieregels

De selector valt terug op de volle suite bij:

1. een geraakt pad buiten `scripts/`, `tests/` en de doc-allowlist van `classify_ci_profile.py`;
2. een geraakte `conftest.py`;
3. fixtures, `pyproject.toml`, constraints of `.github/**`;
4. een geraakt pad dat de graaf niet kent;
5. een parse-fout.

PR 2 test elke regel afzonderlijk. Een misser in de terugtoets is een bevinding. Het is geen reden om stil een regel bij te maken.

### 7. Het bewijscontract

**(a) Exacte reconstructie.** Selectorversie, graafversie, basis-SHA, kandidaat-SHA en de lijst geraakte paden reproduceren per run exact dezelfde U, X, S en E. Ontbreekt er één van de vijf, dan is de run `onreconstrueerbaar`. Hij telt niet mee en staat bij naam in het rapport.

**(b) Identiteit van een poging.** Elk record draagt `run_id` en `run_attempt`. Alleen een afgeronde poging met een JUnit die alle tests van E bevat telt mee. Een afgebroken poging, een time-out of een poging met verzamelfouten telt niet en wordt bij naam gemeld. Een herhaling is een nieuwe poging. Het oudere bewijs wordt nooit overschreven.

**(c) De besparingsmaat is kloktijd.** De besparing wordt gemeten in een aparte schaduwjob die alleen S draait en nooit een check faalt. Die kloktijd gaat naast de volle Profile A op dezelfde kandidaat. Opgetelde JUnit-duren zijn geen besparing: onder xdist lopen tests parallel.

### 8. Het record leeft in het lokale grootboek

Het record gaat naar `state/impact_shadow.ndjson` in de store van het project, omdat GitHub-artefacten na 90 dagen verlopen. Het bewaart namen, geen aantallen. Een CI-artefact is de aanvoerroute, het grootboek is het bewijs.

### 9. De herleidbaarheidstoets

Een auditor moet uit het grootboek alleen kunnen aflezen welke tests op de gemergede kop draaiden, op welke graaf, en welke niet. De `pr_merged`-receipt draagt sinds DR-7 (#2745) de merge-SHA en de geteste boom, waardoor een record daaraan te koppelen is. Slaagt de toets niet, dan wordt de selectie niet geactiveerd.

### 10. Activering is een apart besluit van de operator

Activering volgt pas na de meting en na een besluit van de operator. Een PR-run met `scope: impact` mag dan een merge dragen onder vier voorwaarden:

1. de volle run volgt op main;
2. een rode volle run blokkeert de volgende merge (de vensterpoort `main` van ADR-073);
3. de keuze staat in het receipt;
4. de schaduw liet geen misser boven de drempel zien.

**De drempel:** 0 missers over minstens 15 rode runs, terugtoets en live schaduw samen. Alleen echt verkleinde selecties tellen: runs met `|S| < |U − X|`. Een run waarin de selector terugviel op de volle suite bewijst niets over een kleinere selectie. Het rapport toont dat aandeel apart, en het aantal rode runs apart, zodat "0 missers" op 3 rode runs niet als bewijs leest.

### 11. De ADR-053-uitzondering staat hier als uitzondering

Een gereduceerde PR-run met een volle run na de merge is een beleidsuitzondering op ADR-053. Het is geen toepassing ervan. Hij geldt alleen na het besluit van de operator uit besluit 10.

### 12. Termijn en herstel

Volgt de volle run op main niet binnen de CI-duur plus een marge, dan behandelt de vensterpoort van ADR-073 dat als niet meetbaar. Dat is NO-GO voor de volgende merge. Het herstel is een herhaalde run of een revert.

### 13. Wat deze track niet doet

- Geen selector voor mission-control of SEOcrawler (besluit 5).
- Geen activering: PR 5 wordt pas gebouwd na de meting en het besluit.
- Geen overname van pytest-testmon, Bazel of Nx. De mechanismen zijn geleend (Bazel en Buck2: change pruning; Nx: gedeclareerde invoer propageert; Turborepo: bij te weinig zekerheid uitbreiden naar alles), de code niet.

### 14. De conftest-variant

De conftest-sluiting is de verzameling repo-modules die transitief door elke `conftest.py` onder `tests/` bereikt wordt, met dezelfde importresolutie als de graaf en functielichamen meegeteld. Autouse-fixtures importeren zulke modules (`pr_merge`, `gate_recorder`) binnen een fixture, dus elke test draait ze, terwijl de graaf er geen kant van een test naar trekt (OI-2788).

De schaduw meet beide varianten naast elkaar: de graaf-alleen-variant (S) en de conftest-variant (S wordt U wanneer een geraakt pad in de sluiting zit). Ze beslist niets. Activering, en met welke variant, blijft het besluit van de operator na de meting.

## Gevolgen

- Tijdens de schaduw wint de pijp niets. De stap kost seconden, de schaduwjob loopt naast de volle job. Runners wachten niet (0,03 min mediaan op 13-11), dus de job vertraagt niets.
- De meting voorspelt de winst per PR-run tegen de nulmeting van 15,8 min per Profile A-run (13-11). Na een eventuele activering volgt de werkelijke meting tegen 90 PR-CI-minuten per 1000 regels (klasse 300-450).
- Het ijkpunt van 50 PR's hangt aan de terugtoets (PR 4b) plus de live schaduw.
- De sleutel `VNX_CI_IMPACT_CMD` staat in het register zonder leesplek (`read_site_wired=False`). PR 3 voegt die toe.
- Zonder vensterpoort uit ADR-073 is er geen blokkade bij rood, en dan wordt PR 5 niet gebouwd.

## Terugweg

Zet de sleutel `VNX_CI_IMPACT_CMD` leeg of haal hem weg. Elke PR draait dan weer de volle suite. Het grootboek `state/impact_shadow.ndjson` blijft staan als bewijs en wordt niet verwijderd.
