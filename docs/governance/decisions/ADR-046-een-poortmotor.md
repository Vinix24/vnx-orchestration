# ADR-046 - Eén poortmotor: één uitkomst, één schrijver, drie adaptersoorten

**Status:** Accepted (dispatch dlv-0ffac24a3e57, track gate-engine-consolidation)
**Date:** 2026-10-24
**Decided by:** T0, onder het mandaat van de operator voor golf 1-3 van 24-10. De plan-gate van 24-10 gaf START met één vereiste wijziging: GE-6 loopt ná GE-4b, omdat het `gate_recorder.py` en `gate_executor.py` met serie A deelt.
**Cross-refs:** plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/gate-engine-consolidation.md` (revisie 2); inventaris `20261024-inv-poortmotor`; ADR-005 (eerst schrijven, dan de toestand wijzigen); ADR-035 (receipt v2); ADR-036 (modelidentiteit uit het register); ADR-037 (uitval is afwezigheid, geen tegenstem); ADR-041 (tiebreaker bij de review-cap); ADR-044 (de vorm van dit besluit); ADR-045 (het grootboek, concept van 15-10, nog niet op main).

## Context

Elke poort heeft vandaag zijn eigen weg naar buiten, en voor de harness-lanes is dat een eigen script. Op main (`3e55e06d`) telt de poortlaag 33.515 regels over 46 bestanden; op 3 september waren dat 19.918 regels over 37. `glm_gate.py` is 1.100 regels, `kimi_gate.py` 1.012 en `codex_final_gate.py` 826. De laag groeide dus, maar de vorm bleef: één motor zou het moeten zijn, met dunne adapters eromheen.

Het register (`GATE_PROVIDERS`, `scripts/lib/gate_recorder.py:87`) kent negen poorten, elk met een eigen weg naar buiten:

| Poort | Register | Weg van vandaag |
|---|---|---|
| `codex_gate` | `path_binary` `codex` | `GateRunner.run`, prompt op stdin |
| `gemini_review` | `path_binary` `gemini` | `GateRunner.run`, optioneel via Vertex |
| `claude_github_optional` | `path_binary` `gh` | `GateRunner.run` |
| `ci_gate` | `path_binary` `gh` | inline in `gate_executor.execute_gate` |
| `wiring_gate` | `path_binary` `gh` | `GateRunner.run` |
| `kimi_gate` | `harness_lane` `kimi` | `_make_default_dispatcher` via `provider_dispatch` |
| `glm_gate` | `harness_lane` `glm-harness` | idem |
| `deepseek_gate` | `harness_lane` `deepseek-harness` | idem |
| `claude_gate` | `harness_lane` `claude` | `claude_gate_lane`, niet `provider_dispatch` |

Het verschil tussen glm en kimi is groter dan een tabel met strings. Van de 13 gedeelde functienamen zijn er 2 byte-gelijk (`_get_diff` en `_frontmatter_duration_seconds`). De andere 11 wijken af, waaronder `_build_prompt`, `_extract_verdict` en `main`. Een mechanische samenvoeging levert geen shim op. De adapter moet die verschillen dragen en per provider één regel in het register hebben.

Eén poortuitkomst wordt op veel plekken geschreven. De inventaris telt 26 verschillende schrijfplekken over vijf artefacten:

| Artefact | Schrijvers | Waarvan onder een slot |
|---|---|---|
| result-record | 11 (8 bewaakt, 3 raw) | 8 |
| aanvraag | 5 (1 atomair) | 0 |
| `review_gate_result` | 3 emitters | append-lock alleen |
| verplichting | 1 | 0 |
| forge check-run | 1 | 0, bewust |

De drie ruwe result-schrijvers staan op `scripts/lib/gate_result_parser.py:97`, `scripts/lib/gate_result_parser.py:225` en `scripts/lib/gate_request_handler.py:2373`. Aan de aanvraagkant schrijven `scripts/lib/gate_recorder.py:545` (`persist_request`) en `scripts/lib/gate_request_handler.py:2248`, `:2361` en `:2889` met een kale `write_text`; `:1077` schrijft atomair maar zonder slot. De drie `review_gate_result`-emitters zijn `_emit_terminal_result_receipt` (`scripts/lib/gate_recorder.py:1704`), `_emit_claude_github_receipt` (`scripts/lib/gate_result_parser.py:112`) en `_emit_result_receipt` (`:234`). Na GE-4c-1 schrijven beide parser-schrijvers via `commit_gate_outcome` (`scripts/lib/gate_result_parser.py:128` en `:271`) en bestaan de twee parser-emitters niet meer: hun eigen velden gaan als `receipt_fields` mee met `_emit_terminal_result_receipt` (`scripts/lib/gate_recorder.py:1746`). De verplichting kent 1 schrijver, `scripts/lib/gate_obligations.py:270`.

Alleen de result-records hebben een slot en een overschrijfwacht. `write_result_guarded` (`scripts/lib/gate_recorder.py:1921`) houdt `slot_lock` (`scripts/lib/atomic_io.py:46`) en `_check_overwrite_guard` (`scripts/lib/gate_recorder.py:1085`). De test die een nieuwe ruwe schrijver moet vangen, kent 3 modules (`tests/test_oi1472_residual_guard_writers.py:357`); `gate_result_parser.py` en `gate_request_handler.py` staan er niet in. De drie ruwe schrijvers leveren bovendien geen `review_gate_result` op, dus een uitkomst die langs die weg binnenkomt staat niet in het grootboek.

De idempotentiesleutel draagt sinds #1767 (4 september) `status` en `commit_sha` (`scripts/lib/append_receipt_internals/idempotency.py:64` en `:65`). Wat open is, is de andere kant: de enige poort-emitter stempelt `head_sha` (`scripts/lib/gate_recorder.py:1781`) en geen enkele plek geeft `commit_sha` mee. De commit-as van de sleutel staat dus leeg, en twee uitkomsten met dezelfde status op twee commits vallen samen. Dat is geen sleutelprobleem meer maar een schrijversprobleem.

`codex_final_gate.py` heeft één productiefunctie: `enforce_codex_gate` (`scripts/codex_final_gate.py:176`), met één aanroeper (`scripts/closure_verifier.py:27`). De 569 regels daarna (258 tot 826) zijn alleen vanuit tests bereikbaar. `glm_gate.py:153` en `kimi_gate.py:165` geven allebei `governed_lane.review_dispatcher` terug, dat de fabriek als argument krijgt; sinds GE-5a staat `_make_default_dispatcher` in `scripts/lib/governed_lane.py:184` (INTERIM-markering op `:191`, daarvoor `scripts/lib/plan_gate_panel.py:1659`/`:1666`). `claude_gate` staat als harness-lane in het register maar loopt via `scripts/lib/claude_gate_lane.py:159`, niet via `provider_dispatch`, dat claude weigert. `ci_gate` en `wiring_gate` staan als `path_binary` op `gh`; alleen hun lokale helft is deterministisch.

ADR-037 legde de leeskant al vast: een poort die niet gedraaid heeft is afwezigheid, geen tegenstem. Op vijf PR's met een volledig glm-PASS kon de merge niet door omdat een andere poort `not_executable` was. De schrijfkant heeft die twee soorten afwezigheid nog niet als aparte uitkomsten. Daar gaat dit ADR over.

## Besluit

Er komt één schrijver van een poortuitkomst, met één gesloten set uitkomsten, en per adaptersoort één vorm. De motor bezit wanneer en wat er geboekt wordt; de adapters bezitten alleen hoe een provider wordt aangeroepen en gelezen.

### 1. `GateOutcome` kent vier statussen

| Uitkomst | Betekenis | Terminaal | Uitspraak |
|---|---|---|---|
| `pass` | de poort keek en keurde goed | ja | ja |
| `fail` | de poort keek en keurde af | ja | ja |
| `unavailable` | de poort kon niet kijken: storing, quota, auth, lege uitvoer | nee | nee |
| `not_executable` | de poort kan hier niet draaien: niet geïnstalleerd, verkeerde omgeving | ja | nee |

Vier en niet twee, omdat twee statussen een storing als tegenstem moeten boeken. ADR-037 splitste dat al in twee assen: is de poging voorbij, en is er een uitspraak gedaan. `not_executable` is terminaal zonder uitspraak. `unavailable` is niet terminaal, want een herkansing op een gezonde provider kan nog beslissen. Alleen `pass` en `fail` zijn uitspraken.

De lezer `scripts/lib/gate_status.py` kent deze categorieën al (`PASS_STATES`, `FAIL_STATES`, `UNAVAILABLE_STATES`, `PARTIAL_REVIEW_STATES`, `is_terminal`). `GateOutcome` is de schrijversnaam van dezelfde vier; het is geen tweede lijst. Een lezer verandert dus niet mee.

Twee bestaande neerwaarderingen blijven binnen die vier:

- Een degenerate `pass` boekt de schrijver als `unavailable` (`scripts/lib/gate_recorder.py:2081`).
- Een `pass` op een afgekapte diff boekt hij als `partial_review` (`:2098`).

De aanroeper kiest dus altijd een van de vier. De schrijver verlaagt een `pass` als het bewijs dat eist, met een status die de lezer al kent. Dat is geen vijfde uitkomst in `GateOutcome`, maar een herclassificatie op de grens van het record.

### 2. `commit_gate_outcome()` is de enige schrijver

Eén aanroep neemt het slot per `(pr, gate)`, via `slot_lock` op het result-pad (`scripts/lib/atomic_io.py:46`, gebruikt op `scripts/lib/gate_recorder.py:2118`). Onder dat slot doet dezelfde aanroep vier dingen in deze volgorde:

1. de overschrijfwacht (`_check_overwrite_guard`, `scripts/lib/gate_recorder.py:1085`);
2. het rondegrootboek (`_book_round`, `:874`);
3. het record atomair (`_write_result_atomic`, `:1374`);
4. één `review_gate_result`-receipt (`_emit_terminal_result_receipt`, `:1704`).

Vanaf GE-4b boekt dezelfde aanroep ook de aanvraag en de verplichting. Eén aanroep levert van elk precies één. Een geweigerde schrijfactie levert er nul. De forge-publicatie (`publish_forge_check_run`, `:1507`) blijft buiten het slot, zoals vandaag: een publicatiefout mag een geland record niet terugdraaien.

De reden is meting, geen smaak. Drie emitters betekent dat elk nieuw veld drie keer geschreven wordt of uiteenloopt. Elf result-schrijvers waarvan drie raw betekent dat een uitkomst langs een weg binnenkomt die geen slot, geen wachter en geen grootboekregel kent. Vijf aanvraagschrijvers zonder slot betekent dat een lezer een half JSON kan zien, de klasse van OI-1486. Eén schrijver maakt de uitkomst atomair waar dat telt en laat één plek over voor een veld.

### 3. Contract voor deelfalen en poging-identiteit

Dit contract staat vóór de code van GE-4a vast.

- **Volgorde:** eerst het record, atomair (tijdelijk bestand plus rename, `_write_result_atomic`), dan het receipt.
- **Deelfout:** faalt het receipt, dan blijft het record staan met `receipt_pending: true`.
- **Herstel:** de volgende `commit_gate_outcome` op hetzelfde slot boekt het ontbrekende receipt eerst. Dat is idempotent via de bestaande sleutel, die `commit_sha` en `status` al draagt (`scripts/lib/append_receipt_internals/idempotency.py:64` en `:65`), dus een herhaling na een crash levert geen tweede regel.
- **Poging-identiteit:** `(pr, gate, commit_sha, attempt_seq)`. `attempt_seq` is nieuw en telt binnen het slot op.
- **Weigering:** een geweigerde schrijfactie laat geen record en geen receipt achter, en zet dus ook geen `receipt_pending`.

Het record gaat eerst omdat elke lezer en de merge-deur het record lezen; het receipt is de grootboekregel. Een verloren grootboekregel is uit het record te herstellen met de volgende aanroep. Een verloren record maakt de run onzichtbaar voor iedereen behalve het grootboek, en dat is de verkeerde kant om blind te zijn. `receipt_pending` wordt alleen gezet als het record werkelijk landde.

### 4. Drie adaptersoorten, één eigen transport, en een kleinere codex-module

| Soort | Poorten | Wat het is |
|---|---|---|
| `GovernedLane` | `glm_gate`, `kimi_gate`, `deepseek_gate` | één gedeelde adapter en parser onder `scripts/lib/`; glm en kimi worden shims van ongeveer 60 regels met dezelfde CLI |
| `Deterministic` | de lokale helft van `ci_gate` en `wiring_gate` | de lokale checks als uitvoerder; de `gh`-lezing van GitHub-checks blijft |
| `PathBinary` | `codex_gate`, `gemini_review`, `claude_github_optional` | een binary met stall-detectie, zoals vandaag |

- **`GovernedLane`.** De gedeelde parser (`_extract_verdict`, `_read_report`) en de dispatcher-bedrading verhuizen uit `plan_gate_panel._make_default_dispatcher` (na GE-5a: `scripts/lib/governed_lane.py:148`; daarvoor `scripts/lib/plan_gate_panel.py:1659`). De plan-gate importeert ze terug en verandert zelf niet. De CLI van beide shims blijft `--pr --diff-file --data-dir --model --timeout --json --reprocess`.
- **`Deterministic`.** Alleen de lokale helft van `ci_gate` en `wiring_gate` krijgt deze vorm. De `gh`-lezing van GitHub-checks blijft een subproces. Dit is een gedragswijziging, geen etiket: vandaag staan beide poorten als `path_binary` op `gh`.
- **`PathBinary`.** `codex_gate` houdt zijn binary en stall-detectie. `gemini_review` en `claude_github_optional` blijven in de stapel en houden dezelfde vorm.
- **`codex_final_gate.py` is geen adapter maar een controle.** Het bestand houdt `enforce_codex_gate` (`scripts/codex_final_gate.py:176`) en de helpers die die functie gebruikt. De 569 dode regels (258 tot 826) gaan in GE-2a en GE-2b weg. De aanroeper `scripts/closure_verifier.py:27` verandert niet.
- **`claude_gate` houdt een eigen transport.** Het staat als harness-lane in het register maar loopt via `claude_gate_lane.make_claude_gate_dispatcher` (`scripts/lib/claude_gate_lane.py:159`). `provider_dispatch` weigert claude, dus dit is een vierde transport en geen `GovernedLane`. Het register noemt hem apart.

Een nieuwe provider is na dit besluit een registerregel plus een parser. De motor verandert niet mee.

### 5. De naad met ADR-045

Dit ADR bezit wanneer en wat er geboekt wordt: `GateOutcome`, `commit_gate_outcome` en het terugbrengen van drie `review_gate_result`-emitters naar één. ADR-045 bezit wat elke grootboekregel draagt: het identiteitsblok en de schemaversie in de append-laag (`scripts/lib/append_receipt_internals/payload.py:416` en `:455`).

Het veld heet `commit_sha`, precies zoals punt 4 van ADR-045 het definieert. GE-1 schrijft het op het receipt en houdt `head_sha` als alias. Geen van beide tracks bouwt een tweede `review_gate_result`-schrijver. Deze volgorde is bewust: eerst de drie emitters naar één, dan het identiteitsblok van ADR-045 erop.

### 6. Wat dit ADR niet verandert

- **De stoelregel en de merge-deur.** Welke stoelen de deur eist is `scripts/lib/review_seat_evaluation.py` (R3a #2191, R3b #2193) en track `deterministische-testverificatie`.
- **De standaardstapel.** `gemini_review` en `claude_github_optional` blijven staan. Of ze eruit gaan is een apart besluit van de operator, niet een code-PR van deze track.
- **De idempotentiesleutel.** Die draagt `commit_sha` en `status` al sinds #1767. Dit ADR wijzigt hem niet.

## Gevolgen

- **De schrijverwachter dekt elke module die een result- of request-record schrijft.** `tests/test_oi1472_residual_guard_writers.py` breidt zijn lijst op `:357` uit; een nieuwe ruwe `write_text` op zo'n record laat een test falen. Vandaag kent de wachter 3 modules en passeren de drie raw schrijvers.
- **Het rondegrootboek valt onder hetzelfde slot.** `_book_round` (`scripts/lib/gate_recorder.py:874`) boekt de ronde in dezelfde aanroep als het record, zodat de review-cap niet anders kan tellen dan het record zegt.
- **Eén receipt-emitter.** Een veld toevoegen gebeurt op één plek. Dat is de voorwaarde waaronder ADR-045 zijn identiteitsblok kan opleggen zonder drie schrijvers bij te houden.
- **Geen migratie.** Het receipt krijgt een veld erbij en `head_sha` blijft als alias staan, dus oude lezers blijven werken. Terugdraaien is `git revert` per PR.
- **Consumers merken het pas na een release.** Een project op een oudere engine houdt het oude gedrag. De release is aan de operator.
- **De vier statussen zijn geen nieuwe lezerslijst.** `scripts/lib/gate_status.py` kent ze al. Een lezer die vandaag `unavailable` als afwezigheid behandelt, blijft dat doen.

**Uitrol.** Elke PR is een squash-commit op main en apart terug te draaien. De volgorde volgt uit de gedeelde bestanden:

1. GE-1 schrijft `commit_sha` op het receipt en houdt `head_sha` als alias.
2. GE-3 breidt de schrijverwachter uit en leidt de drie ruwe result-schrijvers door `write_result_guarded`.
3. GE-4a voert `GateOutcome` en `commit_gate_outcome` in en leidt `record_terminal_result` en `record_not_executable` erdoorheen.
4. GE-4b boekt ook de aanvraag en de verplichting in dezelfde aanroep.
5. GE-5a en GE-5b maken glm en kimi shims boven één `GovernedLane`-adapter.
6. GE-2a en GE-2b verwijderen de dode helft van `codex_final_gate.py`.
7. GE-6 doet de lokale helft van `ci_gate` en `wiring_gate`, ná GE-4b.

**Hoe we meten dat het gehaald is.** Na GE-4b: twintig poortruns op verschillende PR's, met per run precies één record, één aanvraag, één `review_gate_result` met `commit_sha` en één verplichting. Daarnaast `grep -rn 'write_text' scripts/*gate*.py scripts/lib/*gate*.py`, gekruist met de wachterlijst: geen ruwe schrijver van een result- of request-record buiten `commit_gate_outcome`. Tot slot `wc -l scripts/glm_gate.py scripts/kimi_gate.py scripts/codex_final_gate.py` voor en na, in het eindrapport van de track.

## Alternatieven die zijn afgewezen

**(a) Eén motor per provider houden en alleen de wachter verbreden.**
Afgewezen. De wachter uitbreiden dekt de drie ruwe result-schrijvers en de aanvraagkant, maar laat vijf artefacten en drie `review_gate_result`-emitters staan. De inventaris telt 26 verschillende schrijfplekken over die vijf artefacten. De laag groeide in zeven weken van 19.918 over 37 naar 33.515 over 46, dus per-provider-motoren blijven groeien. De wachter maakt één record veilig, niet één uitkomst atomair over alle artefacten.

**(b) `claude_gate` in de `GovernedLane`-adapter vouwen.**
Afgewezen. `claude_gate` loopt via `claude_gate_lane.make_claude_gate_dispatcher` (`scripts/lib/claude_gate_lane.py:159`) en `provider_dispatch` weigert claude. Vouwen brengt die weigering terug en zet de claude-stoel op een lane die hem niet kan draaien. Daarom een eigen transport, apart in het register.

**(c) De idempotentiesleutel wijzigen.**
Afgewezen. Sinds #1767 (4 september) draagt de sleutel `status` (`scripts/lib/append_receipt_internals/idempotency.py:64`) en `commit_sha` (`:65`). Een wijziging zou de dedupe van bestaande receipts ongeldig maken zonder iets te winnen. Wat open stond is de schrijfkant: de emitter stempelt `head_sha` (`scripts/lib/gate_recorder.py:1781`) en geeft geen `commit_sha` mee. GE-1 schrijft het veld; de sleutel blijft.

**(d) De drie emitters laten staan en het identiteitsblok van ADR-045 op alle drie zetten.**
Afgewezen. Drie emitters is er al één te veel. Elk veld dat ADR-045 toevoegt zou dan drie keer geschreven moeten worden, met drie kansen om uiteen te lopen. Eerst naar één emitter, dan het veld.
