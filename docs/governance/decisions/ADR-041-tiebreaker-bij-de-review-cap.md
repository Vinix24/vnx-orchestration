# ADR-041 - De tiebreaker bij de review-cap: één registry-model kiest één uitkomst

**Status:** Accepted (dispatch 20261020-rs-r2a-tiebreaker, track review-stopregel-en-lichte-stoelen, deliverable R2, PR R2a; `terug-naar-plan` uitgewerkt in R2b, dispatch 20261020-rs-r2b-terug-naar-plan)
**Date:** 2026-10-20
**Decided by:** operator via de plan-gate van 18-10 (tiebreaker START, ronde 3 en ronde 5), uitgewerkt door de bouwer van R2a.
**Cross-refs:** plan `claudedocs/specs/review-stopregel-en-lichte-stoelen/plan.md` (sectie 2, "Gate-uitslag", "Risks"); ADR-007 (samengestelde sleutels over `project_id`); ADR-027 (getekende attestaties); ADR-036 (modelidentiteit uit de registry); R1 in `scripts/lib/review_rounds.py`; OI-2143.

## Context

R1 telt de rondes van de codex-stoel per kop in een eigen grootboek (`review_gates/rounds.ndjson`) en zet een PR op de cap zodra B3.1 of B3.2 geldt. Vanaf daar weigert de gate elke codex-run en telt de merge-deur geen PASS van de codex-stoel meer. Alleen `--override-b3` komt erlangs.

Dat stopt de lus, maar het geeft geen volgende stap. De T0 staat dan tussen een override en een admin-merge door de operator. Gemeten op #2089: B3.1 viel in ronde 1 en de PR eindigde in een admin-merge. Op #2080 viel B3.2 na ronde 5, twee runs vóór de PASS.

De plan-gate van 18-10 liet R2 door met twee bindende voorwaarden. Eén: zonder besluit over het tekenmechanisme blijft `mergen-met-OI` dicht. Twee: vóór de bouw komt er een toestandscontract voor cap en gate, ook in deze ADR.

## Besluit

### 1. Twee caps, twee betekenissen, elk twee uitkomsten

| Cap | Betekenis (DISPATCH_RULES §3) | Uitkomsten |
|---|---|---|
| B3.1 | Een ontwerpprobleem: drie of meer blockers in één ronde | `verkleinen` of `terug-naar-plan` |
| B3.2 | Geen convergentie: de laatste ronde heeft een blocker en de som is zes of meer | `verkleinen` of `mergen-met-OI` |

R2a voert `verkleinen` uit, R2b `terug-naar-plan`. `mergen-met-OI` komt met R2c: kiest het model die, dan wordt hij vastgelegd als `outcome_unavailable_in_r2a`, met de keuze van het model in `chosen_outcome`. De cap blijft dan dicht en de T0 handelt het af zoals nu: uitstellen met een OI.

**`terug-naar-plan` (R2b, alleen B3.1).** De uitspraak staat eerst, receipt vóór rij (OI-2201), met `terug-naar-plan` als uitkomst. Kiest het model hem, of laat de regel na een mislukte extra ronde alleen hem over, dan volgen drie stappen in deze volgorde (`scripts/lib/review_cap_aftermath.py`):

1. **OI's.** Eén OI per resterende codex-blocker op de kop van de uitspraak, met de ernst die codex gaf (op de schaal blocker, warn, info), de titel en `file:line`. Ze komen uit het resultaatrecord van die kop, en alleen als dat record de uitspraak is die het rondegrootboek telde (zelfde kop, zelfde rapport). Nooit uit het model. De OI-id's gaan in `oi_ids` van de rij.
2. **De deliverable terug naar `proposed`.** De branch `dispatch/<id>` van de PR noemt de dispatch, de `output_ref` van die dispatch de deliverable: dezelfde sleutel waarop de view `deliverables` groepeert en `dispatch_count` en `derived_status` telt. Een deliverable in `ready` gaat terug naar `proposed`, met `operator_approved_at` leeg en een coordination event `deliverable_returned_to_plan` (van, naar, actor is het verdict-id, reden met de cap-regel). Hij moet dus opnieuw door de promotie van de operator. Dat is de enige nieuwe overgang (`planning_cli.return_deliverable_to_plan`). Heeft de dispatch geen `output_ref`, dan is er geen gekoppelde deliverable: niets wordt uit de track afgeleid (OI-1969). Dat staat luid in de uitvoer en in de receipt, met de open deliverables van de track erbij, en de andere stappen lopen door.
3. **De PR gaat dicht**, met één commentaar dat het verdict-id, de cap-regel, de OI-id's en de deliverable noemt (`gate_recorder.close_pr`). Een PR die al dicht of gemerged is, blijft zoals hij is. De staat lezen en sluiten gebeurt in de repo van het project van de store, nooit in die van de cwd: een PR-nummer wijst pas een PR aan samen met zijn repo. De repo komt uit `forge_project_target.resolve_forge_target` (store, project-id, geregistreerde checkout, origin) en gaat als `--repo` mee naar `gh pr view` en `gh pr close`. Is die repo niet vast te stellen, dan gaat er niets dicht en zegt de stap waarom. Het commentaar draagt het verdict-id in een markeerregel. `gh` plaatst het commentaar vóór het sluiten, dus een sluiting die faalt kan het commentaar achterlaten. Daarom leest de stap eerst de commentaren van de PR: staat dat van deze uitspraak er al, dan sluit hij zonder tweede commentaar. Eén commentaar per verdict-id, ook na een gedeeltelijke fout en een herhaling.

Elke stap is idempotent op het verdict-id. Een OI heeft de sleutel `review-cap:<project>:<verdict-id>:blocker-<n>`, met `n` de plaats van de bevinding in het resultaatrecord van die kop. Dat record hoort bij één getelde uitspraak, dus de volgorde schuift niet. De deliverable kijkt eerst of er al een event van deze actor is, en de PR-stap leest eerst de staat van de PR. Een herhaling van `vnx review cap-verdict` na een crash doet daarom alleen wat ontbreekt.

Elke stap krijgt een eigen receipt `review_cap_aftermath` (veld `step`), alleen als er voor die stap van deze uitspraak en dit project nog geen is. De receipt gaat vóór de afgeleide schrijfactie (ADR-005). Stap 1 schrijft de receipt met de OI-id's en zet die id's daarna op de rij. Stap 2 schrijft de receipt binnen de transactie van de deliverable, vóór de commit: landt de receipt niet, dan rolt de overgang terug. Stap 3 sluit eerst de PR op GitHub en boekt daarna de receipt. Per stap en niet één voor het geheel: een crash tussen twee stappen laat dan elke wijziging die al gebeurde met zijn receipt achter. Een stap die blijft falen (de PR gaat niet dicht) houdt de receipts van de stappen ervoor dan niet tegen. Een onvolledige afhandeling geeft exit 3: de uitspraak staat, draai het commando opnieuw.

Een stap met een receipt draait niet opnieuw. Een herhaling haalt hem terug uit zijn receipt. Stap 1 zet de OI-id's uit de receipt op de rij als ze daar nog niet staan, en leest het resultaatrecord niet opnieuw: een latere gate-run op de PR vervangt dat bestand. Stap 2 voert de overgang uit die de receipt vastlegt als de commit niet landde. Staat de deliverable intussen ergens anders, dan gaat hij niet mee en meldt de stap `not_applied`, luid in de uitvoer en in het PR-commentaar. Stap 3 is klaar.

### 2. Eén model uit de registry beslist, nooit een bevindingenlijst

De cap bestaat omdat de bevindingen niet convergeerden. Een tiebreaker die weer bevindingen geeft, start de lus opnieuw die de cap net stopte. Daarom geeft de tiebreaker precies één uitkomst en bij `verkleinen` precies één benoemde ingreep.

- **Invoer:** de cap-regel, de blockers per kop uit het rondegrootboek en de diff-stat van de PR. Niet de tekst van de bevindingen.
- **Uitvoer:** één blok `vnx-review-cap-tiebreak` met `outcome`, `intervention` en `rationale`. Een bevindingenlijst, twee ingrepen, een uitkomst die de regel niet toelaat of `verkleinen` zonder ingreep faalt luid (`TiebreakerParseError`). Een lane zonder antwoord is `TiebreakerNoAnswerError`. In beide gevallen wordt niets vastgelegd.
- **Model:** uit de registry (ADR-036), via hetzelfde mechanisme als de plan-gate-tiebreaker (`plan_gate_tiebreaker.resolve_tiebreaker_model` en de governed dispatcher van het panel). Een eigen sleutel `review_cap_tiebreaker` in `configs/plan_gate_panel.yaml`, nu hetzelfde model (`deepseek_harness` / `deepseek-v4-pro`). Zo kunnen de twee los bewegen (open vraag 3 van het plan).
- **Geen tweede vraag.** Staat er al een uitspraak op de kop die beslist moet worden, dan geeft het commando die terug en vraagt het geen model. Blijft er na de extra ronde maar één uitkomst over, dan beslist de regel en vraagt het commando ook geen model. `tiebreaker_model` is dan leeg.

Commando: `vnx review cap-verdict --pr N --for-dispatch <dispatch-id>` (`scripts/review_rounds_cli.py`, ook via de pip-CLI).

### 3. `verkleinen`: één extra ronde, gebonden aan één dispatch

- De uitspraak noemt één ingreep en kent precies één extra codex-ronde toe, gebonden aan de dispatch die de T0 opgeeft (`--for-dispatch`).
- Een kop telt voor die ronde als hij een commit van die dispatch draagt die de cap-kop niet draagt: een commit met de trailer `Dispatch-ID: <id>` in `git log <cap-kop>..<kop>`. Kan git die reeks niet lezen, ook niet na één fetch van `origin`, dan telt de kop niet.
- De ronde is verbruikt bij de eerste uitspraak in de codex-stoel op zo'n kop. Een run zonder uitspraak verbruikt hem niet, want die schrijft geen regel. De schrijver boekt die uitspraak in het rondegrootboek met `granted_by: <verdict-id>`, onder het slot van het grootboek. De gate en de schrijver kennen de ronde alleen toe zolang zo'n regel er niet is. De merge-deur telt voorbij de cap alleen de PASS op de regel met `granted_by`, en alleen als die naar een opgeslagen `verkleinen`-uitspraak van deze PR wijst.
- De deur (`dispatch_cli`) laat precies die dispatch door op de branch van de PR zolang de ronde open is. Elke andere bouwdispatch op die branch krijgt de vroege melding van R1.
- Na de ronde: PASS is mergen. FAIL gaat terug naar de tiebreaker, die dan alleen nog de andere uitkomst van de regel kan kiezen. Twee keer `verkleinen` op één PR kan niet: de schrijver weigert het (`CapVerdictRefused`) en de store ook, met een partiële UNIQUE.

### 4. Opslag met ADR-007-sleutels

Migratie 0035 maakt `review_cap_verdicts` in de runtime-store van het project. Primaire sleutel (`project_id`, `pr_number`, `verdict_id`). Kolommen: kop, cap-regel, uitkomst, gekozen uitkomst, de ene ingreep, OI-id's (JSON), de dispatch van een toegekende ronde, het tiebreaker-model, de motivering en `created_at`. `project_id` krijgt als default het id van de store zelf, fail-closed opgelost zoals in 0034 (`resolve_init_project_id(strict=True)`). De schrijver stempelt het ook expliciet.

`verdict_id` is `cap-pr<N>-<kop>`: één uitspraak per kop waarop de cap beslist wordt. Elke stap is idempotent op die sleutel. Een herhaling na een crash vindt de rij en schrijft niets dubbel. Een uitspraak van vóór de laatste geattesteerde `opened`-regel van de PR hoort bij een telling die niet meer staat en telt niet mee. Dat geldt ook voor het herstel uit een receipt (OI-2207): alleen een receipt met het `project_id` van deze store (ADR-007) en van ná die regel wordt als rij geboekt. Een receipt van een ander project met dezelfde PR en hetzelfde verdict-id telt hier niet.

`review_cap_approvals` (het getekende akkoord) komt met R2c.

### 5. Toestandscontract van cap en gate

Drie regels, elk deterministisch en elk met een test (`tests/test_r2a_cap_tiebreaker.py::TestTheStateContract`).

1. **Gelijktijdige runs op één kop.** Uitspraken op dezelfde PR, kop en stoel worden geserialiseerd onder het slot van het grootboek. De eerste uitspraak die het grootboek haalt wint. Elke latere wordt geweigerd, ook als zijn run al liep (`_check_append`). Dat geldt ook voor de extra ronde op twee koppen tegelijk: de eerste verbruikt hem, de tweede krijgt `b3_cap`.
2. **Een onderbroken publicatie.** Staat de uitspraak in het grootboek maar landt het resultaatrecord niet, dan faalt de run. Er is geen PASS aan de deur, want de deur telt alleen een record dat de regel draagt. Een nieuwe run op die kop wordt geweigerd, met de naam van de uitweg. Vrijgeven kan alleen de operator, met `vnx review rounds open --pr N --reason "..."`: een geattesteerde regel in het grootboek en een receipt met `released`. Die regel is een nieuwe grens, dus de cap begint daar opnieuw bij nul, zoals bij elke geattesteerde opening. Geen stille reconstructie. De check-run op GitHub is een kopie van het antwoord van de deur (`gate_recorder.publish_forge_check_run`). Faalt alleen die kopie, dan herstelt het herstelcommando de kopie en verandert de deur niets.
3. **Tweede stoel en extra ronde tegelijk.** Vallen een blocker van de tweede stoel en een FAIL in de extra ronde op dezelfde kop samen, dan bindt de tweede stoel eerst: de volgende uitspraak valt onder B3.2. Een uitspraak van de tweede stoel verbruikt de extra ronde nooit. Alleen een uitspraak in de codex-stoel doet dat.

### 6. `mergen-met-OI` blijft dicht

`mergen-met-OI` gaat pas open als er een tekenmechanisme is besloten waarin de T0 niet zonder bevestiging per gebruik kan tekenen: een aparte sleutel buiten bereik van de T0, of een sleutel in `allowed_signers` die bij elk gebruik bevestiging vraagt (Touch ID, passphrase). Dat is open vraag 1 van het plan en een besluit van de operator. Tot dan legt de tiebreaker die keuze vast als `outcome_unavailable_in_r2a` en blijft de cap dicht. R2b verandert dat niet: de letterlijke waarde uit R2a blijft de opgeslagen uitkomst voor dit geval, en alleen `terug-naar-plan` krijgt zijn eigen waarde.

## Gevolgen

- Een PR op de cap heeft een volgende stap zonder override en zonder admin-merge, als één ronde volstaat. Dat is precies het geval van #2089.
- De merge-deur verandert van betekenis: voorbij de cap telt één PASS, die van de toegekende ronde. Daarom staat elke beslisregel van de deur in een eigen test, en de bestaande evaluator- en grootboektests zijn ongewijzigd groen.
- De schrijver leest commitberichten met git. Een gate die draait in een checkout zonder de kop van de PR haalt hem één keer op van `origin`. Lukt dat niet, dan blijft de cap dicht en zegt de weigering waarom.
- De vrijgave van een onderbroken publicatie zet ook de cap op nul. Dat is de semantiek van een geattesteerde opening uit R1. Het is een handeling van de operator met reden en receipt, en hij werkt alleen als er werkelijk een uitspraak zonder record is.
- Het model krijgt geen bevindingen en kan er geen geven. Een tiebreaker die toch een lijst geeft, kost een run en levert niets op. Dat is bewust: een stille interpretatie van een lijst zou de cap omzeilen.
- **De merge-deur weigert een PR die terug naar plan ging (R2b).** Staat er een `terug-naar-plan`-uitspraak (na de laatste geattesteerde `opened`-regel), dan telt de deur geen PASS meer van die PR: van geen enkele stoel, op de kop van de uitspraak en op elke andere kop, en ook niet met `--override-b3`. Die override heft de cap op, geen uitspraak. De deur leest de rijen en de receipts samen (`review_cap_verdicts.standing_verdicts`): een `terug-naar-plan`-receipt van dit project en deze telling waarvan de rij niet landde, weigert precies zoals de rij. Een onderbroken projectie wist de uitspraak niet. Voor die receipts geldt dezelfde regel als bij het herstel uit een receipt (OI-2207). Kan de deur de uitspraken van een PR op de cap niet lezen, of staan er receipts terwijl de store geen project noemt, dan telt hij ook geen PASS, want of de PR terug ging is dan niet vast te stellen. De regel staat in `review_seat_evaluation.door_rounds` en heeft zijn eigen test (`tests/test_r2b_terug_naar_plan.py::TestTheMergeDoorRefusesAPrSentBackToPlan`).
- **De deliverable-levenscyclus krijgt één overgang terug**: `ready` naar `proposed`, alleen via de uitspraak, met een event `deliverable_returned_to_plan` en een lege `operator_approved_at`. Het werk komt daarna in een nieuwe PR, na een nieuwe promotie.
- **De koppeling PR naar deliverable loopt alleen via `output_ref`.** Een bouwdispatch die de deur zonder `output_ref` start, hoort bij geen deliverable. In zo'n store gaat er niets terug naar plan, en de uitvoer zegt dat luid. De deliverable wordt dan met de hand verplaatst.
