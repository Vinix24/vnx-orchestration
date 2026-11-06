# ADR-053 - Een test mag uit Profile A als een blokkerende CI-stap hem al draait

**Status:** Accepted (dispatch dlv-627e34828110, track test-suite-kosten, TSK-5)
**Datum:** 2026-11-06
**Besloten door:** T0 op 06-11 (plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/test-suite-kosten.md`, rij PR-5, besluit 1 en plan-gate-eis 3), op de inventaris `20261106-inv-test-suite-kosten` (groepen G2/G3).
**Cross-refs:** ADR-046 (één poortmotor); OI-906 / OI-1420 (`scripts/ci/test_exclusions.txt`); `.github/workflows/vnx-ci.yml`; `tests/test_ci_gates_still_exist.py`.

## Context

Profile A draait twee fasen in dezelfde job: blokkerende stappen die elk één boombrede controle
doen, en daarna de parallelle pytest-fase over de hele tests-boom. Vier controles liepen op elke
PR in beide fasen — side-door-audit, central-mode-padpoort, architectuur-doc-drift en
status-vocabulaire-drift — samen ~40 s, met dezelfde code op dezelfde boom: pure duplicatie.

Tegelijk is de parallelle fase niet vrijblijvend: de test is daar de enige reden dat de controle
draait. Een test uit de fase halen en de stap later laten sneuvelen maakt van twee onafhankelijke
bewakers nul. De vraag is dus niet "mag deze test weg", maar "wat blijft er over als iemand de
stap weghaalt". Gemeten op `54b2bc05` (de basis van deze PR) is het antwoord voor de vier:

| Stap in `profile-a` | Regel | Commando | `continue-on-error` | `set -euo pipefail` |
|---|---|---|---|---|
| Side-door exhaustiveness audit | `.github/workflows/vnx-ci.yml:207` | `:214` | nee | ja |
| Central-mode path gate | `.github/workflows/vnx-ci.yml:216` | `:224` | nee | ja |
| Run architecture doc drift guard unit tests | `.github/workflows/vnx-ci.yml:292` | `:296` | nee | ja |
| Run status vocabulary drift guard unit tests | `.github/workflows/vnx-ci.yml:310` | `:314` | nee | ja |

De job `profile-a` draagt zelf geen `continue-on-error`, dus een rode stap maakt de job rood en de
job is de verplichte check.

## Besluit

**Een test mag alleen uit de parallelle Profile A-fase verdwijnen als een blokkerende stap in
diezelfde job dezelfde controle op dezelfde boom al uitvoert.** Die uitzondering kent drie
verplichte voorwaarden:

1. **De stap is blokkerend:** geen `continue-on-error` op stap of job, en de stap begint met
   `set -euo pipefail`.
2. **De PR noemt stap en test met `file:line`:** per verwijderde test de test-id plus de naam van
   de CI-stap en zijn regel in `.github/workflows/vnx-ci.yml`, zodat een lezer zelf kan nagaan dat
   de dekking niet weg is.
3. **`tests/test_ci_gates_still_exist.py` bewaakt de stap:** die leest de workflow als YAML, zoekt
   de stap in de job `profile-a` en faalt — met de verwijderde test in de foutregel — wanneer de
   stap, zijn commando, zijn `set -euo pipefail` of zijn blokkerende karakter verdwijnt. Wie stap
   en test samen schrapt, krijgt een rode test in plaats van een stille halvering.

Voor een hele testfile geldt hetzelfde, plus één aanvulling: de file komt in
`scripts/ci/test_exclusions.txt` met een reden die met `ADR-053` begint en de stap plus zijn
`file:line` noemt. `scripts/ci/check_test_exclusions.py` eist die reden al; de naam van de stap
maakt hem controleerbaar.

Wat de uitzondering **niet** is: een test die rood staat, een test die iets anders meet dan de
stap, of een controle waarvan de stap maar een deel doet. De vier hierboven voldoen omdat de stap
hetzelfde script of dezelfde pytest-file op dezelfde boom draait; de stap gebruikt de gevendorde
kopie `.claude/vnx-system`, die de prelude met `rsync -a` uit de checkout vult.

## Gevolgen

- **~40 s minder per PR** zonder dekkingverlies: de vier controles draaien nog, één keer, in een
  stap die de job rood maakt.
- **De bewaking is nu een test over de workflow.** Wie de CI-structuur wijzigt, moet
  `tests/test_ci_gates_still_exist.py` bijwerken. Dat is bewust: de workflow is de bron van de
  dekking geworden en verdient daarom een test.
- **`test_exclusions.txt` heeft een tweede klasse** naast "rood op main" en "kan niet in het
  CI-image": "draait al als blokkerende stap". De kop van dat bestand noemt die klasse en de reden
  begint met `ADR-053`, zodat het verschil zichtbaar is zonder de PR te openen.
- **Fail-closed op de workflow als bewijs:** een onleesbare of misvormde workflow, een ontbrekende
  job, een ontbrekende of dubbele stap, een ontbrekende, lege of niet-string `run`, een ontbrekend
  commando, een ontbrekende `set -euo pipefail` en elke `continue-on-error` die niet aantoonbaar
  `false` is, zijn allemaal een bevinding — nooit een lege pass.
- **Wat niet meeverhuist:** de unittesten in de twee getrimde bestanden. `scan_dir` op een
  geplante boom, de docstring-uitsluiting, de grandfathered segmenten: gedragingen die de stap
  niet kan rapporteren. Ze blijven in de parallelle fase.
