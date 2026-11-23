# ADR-085 - Een blokkerende controle mag draaien in een job die de verplichte Profile A-check nodig heeft

**Status:** Accepted. Verbreedt de woorden "in diezelfde job" uit ADR-053; ADR-053 zelf blijft ongewijzigd (bestaande ADR's zijn bevroren). (dispatch dlv-0cde1fdeae88, track pr-ci-onder-zes-minuten, C3a)
**Datum:** 2026-11-17
**Besloten door:** T0 op 17-11, op plan versie 2 van de track (§ "C3a: ADR die ADR-053 aanpast", punt 1 van "Wat versie 2 verandert"), na codex REVISE punt 1 in de plan-gate.
**Cross-refs:** ADR-053 (een test mag uit Profile A als een blokkerende CI-stap hem draait); ADR-046 (één poortmotor); plan `claudedocs/specs/pr-ci-onder-zes-minuten/plan.md` (gitignored, niet in de repo); track `pr-ci-onder-zes-minuten`, leveringen C3a en C3; `.github/workflows/vnx-ci.yml`; `tests/test_ci_gates_still_exist.py`.

## Context

ADR-053 laat een test alleen uit de parallelle Profile A-fase verdwijnen als een blokkerende stap
**in diezelfde job** dezelfde controle op dezelfde boom al uitvoert. Dat werkte omdat Profile A één
job was: de stap en de parallelle fase deelden één uitkomst.

De track `pr-ci-onder-zes-minuten` splitst die job. Volgens het plan (baseline van het plan, niet
hier gemeten: Profile A 17,1 min van 19,7 min, 199 groene PR-runs, 14 tot 16 november) is Profile A
de langste stap van de PR-CI. Levering C3 maakt er vier shard-jobs van, een job `Profile A guards`
met de blokkerende controles, en een aggregator. De aggregator draagt de verplichte checknaam
`Profile A (doctor + core tests)`; branch protection en de lezers op naam blijven daardoor
ongewijzigd.

Na die splitsing is geen enkele guard meer "in diezelfde job" als de pytest-fase. Letterlijk
gelezen verbiedt ADR-053 dan elke test die een guard dupliceert te verwijderen, en zou een guard in
een andere job zonder regel kunnen draaien: een rode guards-job die de verplichte check niet raakt
dekt niets. Codex wees hierop in de plan-gate (REVISE punt 1). De regel moet dus eerst op `main`
staan, vóór C3 merget.

## Besluit

**Een test mag uit de parallelle Profile A-fase verdwijnen als een blokkerende stap dezelfde
controle op dezelfde boom uitvoert in een job die de verplichte check `Profile A (doctor + core
tests)` nodig heeft (`needs`) en waarvan die check de `success` eist.**

Beide helften zijn nodig. De `needs`-kant zorgt dat de job meedraait en klaar is voordat de
verplichte check beslist. De `success`-kant zorgt dat een rode of ontbrekende uitkomst de
verplichte check rood maakt.

- **Wat "eist de success" waar maakt:** de fail-closed regel van het plan. De aggregator draait met
  `if: always()` en is rood tenzij de guards-job precies `success` is. `failure`, `cancelled`,
  `skipped`, een ontbrekende of onbekende uitkomst: allemaal rood, met de naam van wat ontbrak.
  Alleen de positieve waarde `success` telt.
- **De drie voorwaarden van ADR-053 blijven gelden, ongewijzigd, voor die stap:**
  1. de stap is blokkerend: geen `continue-on-error` op stap of job, en de stap begint met
     `set -euo pipefail`;
  2. de PR noemt stap en test met `file:line`;
  3. `tests/test_ci_gates_still_exist.py` bewaakt de stap.

## Gevolgen

- **`tests/test_ci_gates_still_exist.py` moet in C3 meeleren.** Het zoekt de stap nu in de job
  `profile-a`. Straks zoekt het de stap in de guards-job en toetst het ook (a) de `needs`-kant:
  de aggregator heeft de guards-job in zijn `needs`, en (b) de aggregatorvoorwaarde: de aggregator
  faalt als de guards-job niet `success` is. Het blijft fail-closed op de workflow als bewijs: een
  ontbrekende job, stap, `needs`-regel of voorwaarde is een bevinding, nooit een lege pass.
- **`VNX_CI_XDIST=0` (de weg terug) draait weer alles in één job.** Shards slaan dan over en de
  aggregator draait de seriële sweep. ADR-053 geldt dan letterlijk; deze ADR voegt daar niets aan
  toe en haalt er niets af.
- **Wat deze ADR niet toestaat:**
  - een guard in een job die de verplichte check niet nodig heeft: hij kan rood worden zonder dat
    de check het ziet;
  - een guard waarvan de uitkomst de aggregator niet leest: ook een job in `needs` telt pas als de
    aggregator zijn `success` eist;
  - een guard die niet blokkerend is, of waarvan de PR stap en test niet met `file:line` noemt.
- **ADR-053 wordt niet bewerkt.** Wie ADR-053 leest, vindt hier de verbreding via de cross-ref.
