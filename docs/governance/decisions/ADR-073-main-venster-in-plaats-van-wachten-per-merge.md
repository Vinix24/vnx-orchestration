# ADR-073 - Een venster op main in plaats van wachten per merge: de deur bewaakt de niet-geverifieerde merges, elke merge houdt zijn eigen run

**Status:** Voorgesteld 13-11-2026 (T0, track `ci-cost-and-merge-cadence`, PR 1 van 6); aangenomen zodra de T0 deze PR merget.
**Datum:** 2026-11-13
**Besloten door:** de operator, 13-11-2026 ~05:50Z (N=1 eerst, N=3 pas na een week meten en een apart besluit; onmeetbaar is NO-GO met een override; andere projecten pas na de meting op vnx-dev); het plan `claudedocs/specs/ci-cost-and-merge-cadence/plan.md` met de sectie "Wijzigingen na de plan-gate" (versie d209d769f0cc). Het nummer 073 is door de T0 toegewezen; ADR-074 en ADR-075 zijn gereserveerd voor `impactselectie-in-schaduw` en `integratiecandidate-en-merge-trein`.
**Cross-refs:** ADR-061 (de deur is de enige merge-weg; §3 de volgorde van de poorten, hier aangevuld), ADR-063 (onbekend is nooit 0), ADR-064 (deltareview; DR-7 levert `merge_sha` en `tested_head` op het `pr_merged`-receipt, #2745), ADR-007 (zie "Gevolgen").

Dit ADR legt vast waartegen PR 2 tot en met 4b bouwen: wat het venster is, wanneer een run als groene basis telt, welke uitkomst elke status krijgt, hoe toelating en merge serialiseren, hoe de poort `main` zich in de deur voegt en waarom een native merge queue niet het antwoord is. Het bevat geen code. Alles hieronder is deterministisch: statussen, leeftijdsgrenzen, een telling en een lock. Er komt geen model aan te pas.

## Context

Het doel van de track dateert van 15-08. Toen kostte een VNX CI-run 15 tot 19 minuten en draaiden er twee per merge. De nulmeting van 13-11 (`docs/operations/CI_COST_METING.md`) zegt wat er nog van waar is:

- Het profiel `light` voor docs-only wijzigingen bestaat (`scripts/ci/classify_ci_profile.py`, #1514). Een lichte run duurt mediaan 2,5 min tegen 18,1 min voor een zware.
- De CI-duur is geen kostenpost meer: PR-CI mediaan 17 min, main-CI 19 min, een runner wacht mediaan 0,03 min en de repo is publiek.
- De kostenpost is serieel wachten. De mergeketen wachten na elke merge op een groene main-run: mediaan 19,4 min per merge over 86 merges sinds 11-11.
- Dat wachten zit in T0-gereedschap (`keten_f.sh`, `wacht_main`), niet in de fabric. `PREFLIGHT_ORDER` in `scripts/pr_merge.py` kijkt niet naar main, en `stop_conditions.check_main_ci_red` leest alleen de laatste run.

Main-runs delen bespaart dus niets en kost de bisectie: welke merge maakte main rood.

## Besluit

### 1. Elke merge houdt zijn eigen commit, run en receipt

Elke merge houdt zijn eigen squash-commit, zijn eigen main-run en zijn eigen `pr_merged`-receipt. Er komt geen gedeelde branch en geen gedeelde run. Wat verandert is dat de deur een venster bewaakt in plaats van dat de keten per merge wacht.

### 2. Het venster

Het venster zijn de merge-commits op main na de groene basis, elk met zijn run. Main is lineair, dus de eerste rode run in het venster wijst de **vermoedelijke** oorzaak aan. De uitkomst zegt nooit "bewezen" en noemt het run-id, zodat een lezer het kan herhalen. Een flake (gemeten 10-11: dezelfde 8 injection-tests rood op 3 van 5 main-runs) maakt het venster rood tot een herhaling groen is. De koppeling van merge-commit naar PR en dispatch loopt via `merge_sha` op het `pr_merged`-receipt (DR-7, #2745).

### 3. Het toelatingscontract

- **De basis.** Alleen een groene run met het zware profiel is groene basis. Het profiel komt uit `classify_ci_profile.py` op de merge-commit. Een lichte run schuift de basis nooit op en haalt dus geen rode of lopende zware run uit het venster.
- **Elke status een begrensde uitkomst:**

| Toestand van de run | Uitkomst |
|---|---|
| groen, zwaar profiel | basis |
| `queued` of `in_progress`, jonger dan de leeftijdsgrens | telt als lopend |
| `queued` of `in_progress`, ouder dan de leeftijdsgrens | NO-GO `stale_run` |
| `cancelled`, `timed_out`, `startup_failure`, `skipped` | NO-GO, met de naam van de status als reden |
| `failure` | NO-GO, rode run |
| een merge-commit zonder run, ouder dan de leeftijdsgrens | NO-GO `no_run` |
| een historie die niet terug te lezen is tot een groene zware basis | NO-GO `history_incomplete` |
| een venster dat niet te lezen is (gh weg, time-out, onleesbaar) | NO-GO `unmeasurable`, nooit groen |

- **De leeftijdsgrens** is de CI-duur p95 plus een marge, standaard 40 minuten, per project instelbaar. Een onbekende of onleesbare status valt onder NO-GO onder zijn eigen naam. Zo blijft de deur nooit eindeloos wachten: elke weigering is te verhelpen met een herhaalde run, een revert of een override.

### 4. Serialisatie

Toelating en merge gaan onder één lock per project (flock, hetzelfde patroon als `dispatch_serialization`). De deur leest het venster opnieuw onder de lock, vlak voor `gh pr merge`. Twee parallelle merge-lussen kunnen N zo niet overschrijden.

### 5. De poort `main` in de deur: een aanvulling op ADR-061 §3

De poort `main` staat in `PREFLIGHT_ORDER` van `scripts/pr_merge.py`, vóór `door`, altijd. `PREFLIGHT_ORDER` blijft een vaste constante die `forge_gate_publisher.door_evidence_gates()` leest. Zonder de sleutel `VNX_MAIN_UNVERIFIED_MAX` schrijft de poort een record met `mode: off`, verdict GO en de reden "niet geconfigureerd". Het mergegedrag blijft zonder sleutel gelijk. De poortenlijst en het record veranderen wel: dat is de aanvulling op ADR-061 §3. De permit ligt nog steeds het dichtst bij de merge, en een NO-GO van `main` laat geen permit achter.

### 6. N komt uit de configuratie

`VNX_MAIN_UNVERIFIED_MAX` staat in `config_registry`, per project, standaard niet gezet. De poort weigert bij N of meer merges in het venster waarvan de run nog loopt, bij een rode run en bij elke NO-GO uit besluit 3. Het receipt legt vast wat de poort zag: de groene basis, de lopende SHA's, N en de leeftijdsgrens. **Eerst N=1 op vnx-dev**, dat is de praktijk van de keten, nu door de deur afgedwongen. **N=3 pas na een week meten en een apart besluit van de operator**, met die meting erbij. Andere projecten (mission-control, SEOcrawler) pas na de meting op vnx-dev, via hun eigen projectconfig.

### 7. Een eigen override

`--override-main <reden>` dekt alleen deze poort. De CI-poort houdt `--override-reason`. Het receipt draagt de reden. Een revert-PR of een fix-forward-PR mag door de poort met `--override-main` en de reden `revert_or_fix_forward`: dat is de terugweg bij een rode main.

### 8. De keten vraagt de lezer

De keten vraagt het venster als alleen-lezen probe (`vnx main-window status`) en start pas een merge bij GO. De deur blijft de vangrail. Zo worden lopende runs geen reeks `pr_merge_refused`-receipts.

### 9. Een native merge queue is niet de weg

Het eerste argument is beschikbaarheid. De pagina "Managing a merge queue" op docs.github.com zegt (https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue, geraadpleegd 13-11-2026): "Pull request merge queues are available in any public repository owned by an organization, or in private repositories owned by organizations using GitHub Enterprise Cloud." `gh api repos/Vinix24/vnx-orchestration --jq .owner.type` geeft `User`, en de repo is publiek. De repo staat bij een persoonlijk account, dus de queue is hier niet beschikbaar.

Het tweede argument geldt onafhankelijk daarvan. Een native queue merget zelf. Dan zou de deur de `merge_group`-SHA als permit-doel moeten kennen, en die bestaat niet. ADR-061 schrijft voor dat een merge buiten de deur geweigerd wordt; het zegt niet dat alleen de deur een merge uitvoert, maar de weigering heeft een permit-doel nodig dat bij de queue ontbreekt. Een gefaalde groep wordt bovendien gesplitst: dat is bisectie op groepsniveau, precies wat besluit 1 voorkomt. De toets van de kop die main wordt hoort bij het spoor `integratiecandidate-en-merge-trein`.

## Gevolgen

- **Flaky main-runs** maken het venster rood tot een herhaling groen is. De poort noemt de eerste rode run "vermoedelijk".
- **Bij een GitHub-storing** weigert de deur: onmeetbaar is NO-GO. Stopconditie E4 geldt dan toch. De uitweg is een override met reden.
- **Quotum.** Per merge komt één `gh run list` bij, plus een receiptlezing, op het vlootbrede quotum van 5000 per uur. Het aantal calls staat in het receipt.
- **Per project.** De sleutel staat alleen bij vnx-dev aan. Elders merkt niemand iets tot de eigen projectconfig hem zet.
- **De winst is een bovengrens.** De 19,4 minuten zijn de duur van een main-run. De meting bij N=1 en N=3 volgt in PR 4b.
- ADR-007 speelt niet: er komt geen nieuwe tabel in de centrale DB. De nieuwe velden staan op het bestaande `pr_merged`- en `pr_merge_refused`-receipt.

## Terugweg

Haal de sleutel `VNX_MAIN_UNVERIFIED_MAX` weg. De poort `main` valt terug op `mode: off` en het mergegedrag is dat van vóór de poort. Er is geen data om terug te draaien.
