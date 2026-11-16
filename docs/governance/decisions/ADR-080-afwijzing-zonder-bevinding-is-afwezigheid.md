# ADR-080 - Een afwijzing zonder bevinding is afwezigheid: amendement op ADR-046 en ADR-037

**Status:** Voorgesteld 15-11-2026 (T0, track `reviews-zonder-uitkomst`, levering D0)
**Datum:** 2026-11-15
**Besloten door:** de operator, met zijn akkoord van 15-11-2026 (inbox 28ad40bfaf74e4db), en de plan-gate op het plan `claudedocs/specs/reviews-zonder-uitkomst/plan.md`: codex gaf op versie 1 en versie 2 REVISE, de tiebreaker koos beide keren START (`decision_ref: tiebreak:START`). Versie 3 draagt de verplichte wijziging van de tiebreaker (het herhalingscontract, besluit 3) en de wijziging uit het eerste codex-punt (de uitkomst vooraf in een ADR vastleggen). Het nummer 080 is gekozen omdat ADR-079 gelijktijdig door de docs-track (A0) wordt geschreven.
**Cross-refs:** ADR-046 (§1 de vier uitkomsten, §3 `attempt_seq`), ADR-037 (amendement R5b: elke seat een eigen reviewer, het rapport op schijf blokkeert de kop), ADR-007 (geen nieuwe centrale tabel), ADR-063 (onbekend is nooit 0), ADR-079 (een bestaand bestand onder `docs/governance/decisions/` verandert niet).

Dit ADR legt vast waartegen D2, D3a, D3b, D4 en D5 van de track bouwen. Het bevat geen code. Het amendeert ADR-046 en ADR-037 met een verwijzing en wijzigt die bestanden niet (regel 6 van de track `documentatie-volgt-de-code`, vastgelegd in ADR-079). Waar dit ADR "moet" of "nooit" zegt, is dat een eis die een test kan controleren.

**Deterministisch of model.** Alles in dit ADR is deterministisch: het tellen van bevindingen, de herclassificatie, de vier voorwaarden van de rapportuitzondering, het budget per kop, het slot bij de start en de E6-bakken. Er is geen modelpad. Het enige model in het geheel is de reviewer zelf, en diens uitvoer wordt alleen gelezen op een vast veld: het verdict en de lijst met bevindingen.

## Context

Een codex-review die blokkeert zonder dat er iets te herstellen valt, kost een ronde, houdt de merge tegen en voedt de stopregel E6. Op mission-control vraagt daardoor elke dispatch een override met akkoord van de operator. Er zijn twee oorzaken, allebei in de code aangewezen.

1. **Codex test buiten de projectvenv.** `resolve_review_interpreter` in `scripts/gate_runner.py` (OI-2591) kiest de eerste interpreter die `import pytest` haalt, en dat is die van de fabric, niet die van het project. Op MC #1390 draaide codex met mcp 1.27.1 terwijl het project mcp 2.3 eist. De tegenproef met `.venv/bin/python` en `PYTHONPATH=src` gaf 241 passed. Deze oorzaak is werk van levering D1 en valt buiten dit ADR.
2. **Een afwijzing zonder bevinding wordt een blokkerende bevinding.** `_with_verdict_entry` in `scripts/lib/gate_artifacts.py` (OI-1938) voegt bij `fail` of `blocked` zonder blokkerende bevinding één regel toe met de prefix `verdict_without_blocking_finding` (`VERDICT_ENTRY_PREFIX`) en `file_path: ""`. `_gate_result_cause` in `scripts/lib/stop_conditions.py` maakt van een blokkerende bevinding zonder `file_path` de oorzaak `blocking_findings`. Elke PR met zo'n regel heeft dus dezelfde oorzaak, en drie op rij laten E6 afgaan als "hetzelfde terugkerende probleem", ook als de PR's niets met elkaar te maken hebben.

**Nulmeting** (1 tot en met 15 november, gemeten 15-11 ~15:35Z met `claudedocs/specs/reviews-zonder-uitkomst/nulmeting.py`; het script reproduceert de MC-telling exact):

| project | codex-blokkades | klasse nul ("the reviewer wrote no findings") | klasse advisory ("N finding(s), none at a blocking severity") |
|---|---|---|---|
| vnx-dev | 147 | 3 (2%) | 41 |
| mission-control | 30 | 14 (47%) | 8 |
| newsflirt | 11 | 4 (36%) | 5 |

**Klasse advisory bevat geen loze blokkades.** De acht jongste op vnx-dev (#2786, #2793, #2864, #2890, #2919, #2930, #2949) dragen elk een gereproduceerd defect met een te lage severity. Een rejection met alleen advisory-bevindingen is een echt defect en blijft `fail` (OI-1938). Alleen klasse nul is afwezigheid.

Tokens: codex draait op het abonnement, dus de geldkosten zijn nul. In API-equivalent kostte klasse nul 21 runs (mediaan 20.000 tot 26.000 verse tokens per run, samen ongeveer $2,90) en had geen enkele een herstelronde. De winst zit in de doorlooptijd (mediaan van klasse nul tot de volgende codex-run: 28 minuten op vnx-dev, 124 minuten op MC) en in de akkoorden van de operator op MC en newsflirt.

## Besluit

### 1. Herclassificatie naar `unavailable` met reden `verdict_without_findings`

*(Plan: § Approach, "De uitkomst: herclassificatie naar `unavailable`".)*

ADR-046 §1 kent vier uitkomsten, en `unavailable` betekent daar "de poort kon niet kijken": niet terminaal en zonder uitspraak. ADR-046 staat al een herclassificatie op de grens van het record toe: een degenerate `pass` boekt de schrijver in `scripts/lib/gate_recorder.py` als `unavailable` (reden `gate_execution_degenerate`). Dit ADR breidt dat uit met een tweede geval, binnen dezelfde vier uitkomsten. Er komt geen vijfde uitkomst bij.

- Een `blocked` of `fail` met **nul bevindingen van welke severity ook** moet de schrijver boeken als `unavailable` met reden `verdict_without_findings`. Er is dan geen uitspraak waarop iemand kan handelen.
- Een `blocked` of `fail` met minstens één bevinding, ook als alle bevindingen advisory zijn, moet de synthetische regel van `_with_verdict_entry` houden en `fail` blijven. De herclassificatie raakt dit geval nooit.
- De ruwe uitvoer van de reviewer moet in het rapport blijven staan. De schrijver moet in het rapport een sectie "Herclassificatie" zetten met het ruwe verdict, de reden en het `execution_id`.
- Een lezer die `verdict_without_findings` niet kent, leest `unavailable` (zie Gevolgen).

Testbaar: `verdict: blocked` met `findings: []` en `verdict: fail` met `findings: []` worden `unavailable`; een `fail` met alleen advisory blijft `fail` met de synthetische regel; het rapport draagt de ruwe uitvoer en de sectie Herclassificatie.

### 2. De uitzondering op de rapportveto van ADR-037 R5b

*(Plan: § Approach, "Het rapport en de voorrangsregel van ADR-037".)*

Het R5b-amendement van ADR-037 zegt dat een rapport op schijf met een FAIL-verdict de kop blokkeert, wat de JSON ook zegt (`_report_blocking_indicators` in `scripts/closure_verifier.py`). Zonder aanpassing blokkeert het ruwe `blocked` in het rapport van een geherclassificeerde run de kop dus nog, ook na een overname. De regel van R5b blijft de regel. Dit ADR maakt één smalle uitzondering.

De lezer mag het FAIL-verdict van een rapport alleen overslaan als **alle vier** deze dingen waar zijn:

1. het record staat op `unavailable` met reden `verdict_without_findings`;
2. de lijst met bevindingen in het record is leeg;
3. de genormaliseerde sectie Findings van het rapport, die de schrijver opstelt, telt 0 blokkerend en 0 advisory;
4. de sectie Herclassificatie noemt hetzelfde `execution_id` als het record.

Is er één niet waar, dan blokkeert het rapport de kop zoals nu. Een markering die alleen in de ruwe uitvoer van de reviewer staat, telt nooit: de lezer moet de sectie Herclassificatie en de genormaliseerde sectie Findings uit het door de schrijver opgestelde deel van het rapport halen. Dezelfde regel geldt voor elke andere lezer van een gate-uitkomst: ontbreekt een veld, is het `None`, misvormd of een onbekende waarde, dan blijft de uitkomst blokkerend.

Testbaar: een record met de vier voorwaarden blokkeert de kop niet op het rapport; elk van de vier apart weggenomen blokkeert wel; een geplante "Herclassificatie"-tekst in de ruwe codex-uitvoer blokkeert wel; een `fail` met alleen advisory blokkeert wel.

### 3. Herhaling en overname

*(Plan: § Approach, "Herhaling en overname (het contract voor D4)".)*

Het contract gebruikt `scripts/lib/gate_run_guard.py`, dat nu al de herkansing na een `gate_execution_degenerate`, een timeout of een stall begrenst. Er komt geen nieuwe opslag en geen centrale tabel bij, dus ADR-007 wordt niet geraakt.

- **`rounds.ndjson`: niets.** Een poging met `verdict_without_findings` is geen uitspraak, en een poging die zonder verdict eindigt evenmin. De schrijver mag voor beide geen regel in `rounds.ndjson` zetten. `review_rounds.book_result_verdict` slaat beide al over ("no review-seat verdict") en dat moet zo blijven. Het grootboek blijft het grootboek van uitspraken, voor de B3.2-cap en de merge-deur.
- **Waar de poging wel staat:** in `head_runs` op het verplichtingsrecord van de poort. De telling is per uitvoering, met `reviewer_ran` als voorwaarde: een run die geen reviewer startte (quota, capaciteit, een mislukte setup) telt niet. Het verplichtingsrecord boekt `commit_gate_outcome` bij elke aanvraag (ADR-046 §2). D4 moet eerst toetsen dat dit ook geldt voor een aanvraag via `review_gate_manager request-and-execute` buiten de verplichtingsrunner. Geldt het niet, dan stopt de levering en meldt de bouwer het, in plaats van een tweede telling te bouwen.
- **`attempt_seq`** houdt de betekenis van ADR-046 §3: een teller van schrijfacties binnen het slot per `(pr, gate)`. De herkansing mag er nooit op leunen.
- **Ontdubbeling bij de start** doet het slot tijdens de run van `scripts/lib/gate_run_guard.py`: een exclusief, niet-blokkerend `fcntl.flock` op één bestand per (project, PR, poort, kop) onder `review_gates/run_locks/`. Een tweede start op dezelfde sleutel moet niet draaien en logt de houder. Een crash laat geen slot achter dat een kop blokkeert.
- **Budget:** `MAX_REVIEWER_RUNS_PER_HEAD`, dus de eerste run plus één herkansing. Na de eerste `verdict_without_findings` op een kop leest dezelfde poort bij de volgende aanvraag opnieuw.
- **Na de herkansing nog `verdict_without_findings`:** het verplichtingsrecord krijgt reden `verdict_without_findings_retry_cap`, naast de bestaande `REASON_DEGENERATE_RETRY_CAP`. `_walk_takeover_chain` in `scripts/lib/gate_request_handler.py` leest die reden voor deze kop en slaat de poort over met een hop van dezelfde naam. De bestaande overnamevolgorde vult dan de stoel, met een eigen reviewer per stoel (ADR-037 R5b). Dat is een hop bij de aanvraag, geen nieuw record, dus er ontstaat geen `not_executable` dat E6 kan voeden.
- **Een nieuwe kop begint opnieuw**, want `head_runs` telt per kop. Een herstart verandert de telling niet, want het verplichtingsrecord staat op schijf.
- **Uitkomst van de herkansing:** een PASS of een fail is een gewone uitspraak. Een andere `unavailable` (quota, storing) volgt de bestaande regels.
- **Is de overnameketen op**, dan geldt de bestaande benoemde eindtoestand (`_chain_exhausted_result`). Er is geen terugval op codex.
- **B3.2-cap:** een `unavailable`-record schrijft geen regel in `rounds.ndjson`, dus het telt niet mee voor `VNX_REVIEW_ROUND_CAP`. De grens is het budget per kop plus de overname. Die cap blijft ongewijzigd.

Testbaar: één `verdict_without_findings` op een kop geeft een tweede run van dezelfde poort; een tweede geeft `verdict_without_findings_retry_cap` en de overnamehop; een nieuwe kop begint opnieuw; een tweede start terwijl het slot vastgehouden wordt draait niet; een run zonder `reviewer_ran` telt niet; een aanvraag via `request-and-execute` telt even goed als een via de verplichtingsrunner; een geherclassificeerde run schrijft geen regel in `rounds.ndjson`.

### 4. E6 telt alleen echte oorzaken

*(Plan: § Approach, "E6 telt alleen echte oorzaken".)*

In `scripts/lib/stop_conditions.py`, waar `_classify_gate_record` de records in bakken zet (`unbuilt`, `exhausted`, `counted`):

- Een record `unavailable` met reden `verdict_without_findings` moet een eigen bak krijgen naast `unbuilt` en `exhausted`. Hij telt nooit mee in de reeks en wordt luid gemeld.
- Is de enige blokkerende regel van een record de synthetische `verdict_without_blocking_finding`, dan moet `_gate_result_cause` de oorzaak halen uit de `file_path` van de advisory-bevindingen van dat record. Drie advisory-fails op drie verschillende bestanden geven dan geen E6. Drie op hetzelfde bestand wel, want dat is een terugkerend probleem. Heeft zo'n record geen advisory-bevindingen met een bruikbaar pad, dan houdt het de oude oorzaak `blocking_findings`.
- De E6-historie op de receipts (`review_gate_result` in `t0_receipts.ndjson`) gebruikt dezelfde twee regels.

Testbaar: drie records `verdict_without_findings` op rij geven geen E6 en één luide melding; drie advisory-fails op verschillende bestanden geven geen E6, op hetzelfde bestand wel; een synthetische regel zonder advisory-bevindingen houdt de oude oorzaak.

### 5. Volgorde van de activatie

*(Plan: § Deliverables, "Volgorde" en "Wat er per levering op main verandert".)*

De lezers komen vóór de schrijver. Pas als elke lezer `verdict_without_findings` kent, mag de schrijver hem boeken.

| Levering | Wat verandert er op main |
|---|---|
| D2 | `_report_blocking_indicators` met de vier voorwaarden van besluit 2, plus een fail-closed-toets per lezer van een gate-uitkomst. Verandert niets tot D5: tot dan schrijft niets die reden. |
| D3a | E6: de oorzaak van een record met alleen de synthetische regel komt uit de advisory-paden. **Verandert het gedrag meteen en dat is de bedoeling:** het repareert een valse E6-reeks die vandaag al bestaat, en hangt niet af van D5. |
| D3b | E6: de eigen bak `verdict_without_findings` en de E6-historie op de receipts. Verandert niets tot D5. |
| D4 | `verdict_without_findings` in de herkansingsredenen van `gate_run_guard`, reden `verdict_without_findings_retry_cap` en de hop in `_walk_takeover_chain`. Verandert niets tot D5. |
| D5 | De schrijver: `_with_verdict_entry` boekt nul bevindingen als `unavailable` via `commit_gate_outcome`, met een `review_gate_result`-receipt (`gate_status: unavailable`, `reason: verdict_without_findings`) en de rapportsectie Herclassificatie. Komt pas als D2, D3b en D4 op main staan. |

D2, D3b en D4 hebben elk een toets met een met de hand gebouwd record. D2, D3b en D4 raken elkaar niet.

## Gevolgen

- Een lezer die `verdict_without_findings` niet kent, leest het record als `unavailable`: de status die elke lezer kent (`scripts/lib/gate_status.py`, ADR-046 §1). Een lezer die `unavailable` als afwezigheid behandelt, blijft dat doen. Niets wordt hierdoor een PASS: `unavailable` is geen uitspraak en geeft nooit vrij.
- Het rapport draagt de ruwe uitvoer van de reviewer, dus het bewijs blijft bewaard. Tot D2 op main staat, blokkeert dat rapport de kop nog. Daarom komt D5 pas na D2.
- Onveranderd blijven: de vier uitkomsten van ADR-046 §1 en de betekenis van `attempt_seq` (§3); de regel van ADR-037 R5b dat een rapport met een FAIL-verdict de kop blokkeert, behalve in het ene geval van besluit 2; OI-1938 (een afwijzing met minstens één bevinding blijft `fail`); de B3.2-cap `VNX_REVIEW_ROUND_CAP`; `rounds.ndjson` als grootboek van uitspraken; het slot van `gate_run_guard`; de severities van codex-bevindingen.
- ADR-046 en ADR-037 zelf veranderen niet. Wie ze leest, vindt dit ADR via de cross-refs hierboven.
- Risico en vangnet: de uitzondering van besluit 2 is fail-open als een lezer haar te ruim leest. Daarom heeft elk van de vier voorwaarden een eigen negatieve toets, plus een injectietoets op de ruwe uitvoer.
