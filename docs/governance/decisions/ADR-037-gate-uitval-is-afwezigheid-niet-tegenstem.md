# ADR-037 — Een poort-uitval is afwezigheid, geen tegenstem

**Status:** Accepted (dispatch 20260906-oi1624-uitval-is-afwezigheid)
**Date:** 2026-09-06
**Decided by:** T0, op basis van een live blokkade op vijf PR's (#1777-#1781) op main `36ee70a3`.
**Resolves / Cross-refs:** OI-1624. Bouwt voort op OI-1576 (`_find_takeover_successor_results`, ADR nog niet apart vastgelegd) en OI-1469/OI-1470 (de overschrijfwachter in `gate_recorder._check_overwrite_guard`, die dit ADR ongemoeid laat).

## Context

Vijf PR's met CI-conclusie `success` en een volwaardig `glm_gate` PASS-record (nul blocking findings, `commit_sha` gelijk aan de PR-head, `contract_hash` gevuld, rapport op schijf) konden niet mergen. Gemeten op PR #1777 (`~/.vnx-data/vnx-dev/state/review_gates/results/`):

```
pr-1777-codex_gate.json: status=not_executable, reason=provider_not_installed,
                          contract_hash="", report_path=""
pr-1777-glm_gate.json:   status=pass, contract_hash gevuld, report_path bestaat,
                          commit_sha == head
pr-1777-kimi_gate.json:  status=unavailable, reason=dispatch_error
```

De gedeclareerde poort (`gate_obligations.declared_gates_for_pr`) is `codex_gate`. Geen van de drie records draagt een `takeover`/`takeover_path`-annotatie — de drie poorten zijn onafhankelijk van elkaar gedispatcht in dezelfde review-ronde, niet via een sequentiële overname-keten.

`closure_verifier.check_review_gate_for_merge` resolveerde dit als volgt: `codex_gate`'s eigen record heeft geen beslist verdict (`not_executable` zit niet in `_DECIDED_VERDICT_STATES`), dus de functie zoekt een OI-1576-overname-opvolger. Die vindt niets (geen enkel record draagt `takeover_path`). Bij gebrek daaraan viel de functie terug op `_merge_door_record_verdict(result, gate, pr_id)` — **hetzelfde bewijs-invariantenblok dat een ECHTE, beslissen verdict-poging beoordeelt** — toegepast op een record dat nooit een verdict heeft afgeleverd. Dat blok las de lege `contract_hash`/`report_path` als "bewijs onvolledig" en gaf NO-GO, met geen enkele uitweg: de overname-route (OI-1576) vereist een formele `takeover_path`-annotatie die hier nooit is gezet, en de overschrijfwachter (OI-1469/OI-1470) weigert terecht een terminaal record te overschrijven met een niet-terminale herbeoordeling.

**Netto: een provider die niet geïnstalleerd is, produceert een terminaal record dat de merge blokkeert en tegelijk de enige gedocumenteerde uitweg (OI-1576) dichthoudt**, omdat die uitweg een formele koppeling eist die in de praktijk niet altijd ontstaat (meerdere poorten los gedispatcht in één ronde, niet altijd een sequentiële keten-overname).

## Beslissing

**Een poort-status die uitdrukt dat de poort niet gedraaid heeft, is AFWEZIGHEID — nooit een tegenstem — ongeacht of het record terminaal is opgeslagen.**

Dit wordt vastgelegd als een expliciet onderscheid tussen twee onafhankelijke assen, nooit als een uitzondering op een lijstje statusnamen:

- **Terminaal** (`gate_status.is_terminal`): is deze aanvraagpoging afgerond? `not_executable` is terminaal — de poort is definitief geclassificeerd als "kan niet draaien" — ook al is er niets uitgevoerd.
- **Een uitspraak gedaan** (`_DECIDED_VERDICT_STATES` = pass ∪ fail): is deze poging afgerond MET een oordeel? Terminaal betekent "deze poging is voorbij", nooit "er is geoordeeld". Alleen een pass en een echte afkeuring zijn uitspraken.

Absentie is dus: geen beslist verdict, ÉN (de poging is afgerond zonder oordeel — `is_terminal` — OF het is een expliciete uitval-status — `gate_status.UNAVAILABLE_STATES`, zelf bewust NIET terminaal omdat een herkansing nog kan beslissen). Beide routes lezen af uit `gate_status.py`'s eigen categorieën, nooit uit een tweede, met de hand gekozen lijst in `closure_verifier.py` — een uitvalstatus die morgen aan een van die twee categorieën wordt toegevoegd, valt hier automatisch goed (`closure_verifier._is_absent_without_verdict`).

Bewust UITGESLOTEN van "afwezigheid": `INCOMPLETE_STATES` (pending/running/queued/requested). Die betekenen "dit exemplaar van de poging kan nog steeds tot een oordeel rijpen" — een heel andere bewering dan "deze poging is over en er komt nooit een oordeel". Een in-flight status blijft daarom via het bestaande "niet terminaal"-pad NO-GO, ongewijzigd.

### De voorrangsregel (waarom dit ertoe doet)

Zodra de gedeclareerde poort een bevestigde afwezigheid is (record bestaat, geen uitspraak gedaan) én geen formele OI-1576-overnamesuccessor haar noemt, raadpleegt de merge-poort elke ANDERE bekende poort se eigen, onafhankelijk afgeleverde uitspraak voor exact dezelfde PR/branch/sha (`closure_verifier._find_peer_gate_results`):

1. **Een echte afkeuring blokkeert altijd** — ook naast een pass van een andere poort. Als een peer-poort een beslist `fail` draagt, is het resultaat NO-GO, ongeacht of een andere peer passeert.
2. **Een volledig bewezen pass van een peer-poort telt als ondertekenaar** — onder DEZELFDE zeven bewijs-invarianten die elke andere verdict-check al afdwingt (record bestaat, terminaal, contract_hash, report_path, rapport bestaat op schijf, verdict spreekt zichzelf niet tegen, sha gelijk aan de head).
3. **Nul geldige ondertekenaars blijft NO-GO** — geen peer met een bruikbaar oordeel, geen successor, geen eigen uitspraak: de merge wordt geweigerd, nu eerlijk gerapporteerd als afwezigheid ("nul geldige ondertekenaars") in plaats van als "bewijs onvolledig" (een formulering die hoort bij een mislukte poging, niet bij een poort die nooit heeft gesproken).

### Wat dit NIET verandert

- **De OI-1576-overnameroute blijft primair en ongewijzigd.** Een formele `takeover_path`-koppeling wordt, wanneer aanwezig, nog steeds eerst geraadpleegd en beslist zelfstandig — de nieuwe peer-route is uitsluitend het vangnet voor wanneer die koppeling er niet is.
- **Een gedeclareerde poort zonder ENIG record (`result is None`) krijgt geen peer-fallback.** Dat blijft het bestaande, ongewijzigde contract (`TestUnrelatedRecordsNeverTakeOver`, OI-1576): een poort die nooit is aangevraagd is een onbekende toestand — misschien gewoon nog niet aan de beurt in deze ronde — niet een bevestigde afwezigheid. Een vreemde pass mag daar nooit voor invallen. Alleen een poort die WEL is aangevraagd en terugkwam zonder oordeel (record bestaat) is een bevestigde afwezigheid.
- **De OI-1469/OI-1470-overschrijfwachter (`gate_recorder._check_overwrite_guard`) is niet aangeraakt.** Die beschermt terecht tegen het overschrijven van een terminaal record met een niet-terminale herbeoordeling. Het probleem zat nooit in die bescherming, maar in de LEESKANT die een niet-gedraaide poort als een beoordeelde-maar-onvolledige uitspraak las.

## Consequenties

- **Positief — de blokkade lost op zonder een invariant te verzwakken.** PR #1777 (en de vier zusters) gaan van NO-GO naar GO zodra minimaal één andere poort in dezelfde ronde een volledig bewezen pass aflevert; een echte afkeuring blijft even hard blokkeren als voorheen.
- **Eerlijker foutmelding.** Een merge-weigering op een afwezige poort zegt nu letterlijk "afwezig, nul geldige ondertekenaars" in plaats van "bewijs onvolledig" — een operator die het logboek leest, ziet meteen of een provider ontbreekt of een echte review is mislukt.
- **Nieuw risico — een bredere leesomvang bij afwezigheid.** Waar de merge-poort voorheen alleen de gedeclareerde poort en formele opvolgers las, leest hij nu (uitsluitend bij bevestigde afwezigheid) elke bekende poort in `_KNOWN_GATES` voor dezelfde PR/branch/sha. Dat blijft begrensd: dezelfde scope-matcher (`_record_matches_scope`) en dezelfde zeven bewijs-invarianten gelden onverkort, en `_KNOWN_GATES` is de bestaande, enum-afgeleide verzameling — geen ongebonden scan.
- **Nog open:** de vraag WAAROM meerdere poorten (codex, kimi, glm) los gedispatcht worden voor dezelfde PR zonder dat een sequentiële overname-keten dat vastlegt, is een aparte kwestie in `gate_request_handler._dispatch_review_seat`/`_stamp_takeover_annotations` — dit ADR verandert daar niets aan en verzwakt de OI-1576-keten niet; het voegt alleen een leeskant-vangnet toe voor het geval die keten (om welke reden dan ook) geen formele koppeling aflevert.

## References

- OI-1624 — dit ADR, `scripts/closure_verifier.py::_is_absent_without_verdict`, `::_find_peer_gate_results`, `::check_review_gate_for_merge`.
- OI-1576 — `_find_takeover_successor_results`, de primaire overnameroute die dit ADR ongewijzigd laat.
- OI-1469/OI-1470 — `gate_recorder._check_overwrite_guard`, de schrijfkant-wachter die dit ADR expliciet niet aanraakt.
- `scripts/lib/gate_status.py` — de canonieke statuscategorieën (`PASS_STATES`, `FAIL_STATES`, `INCOMPLETE_STATES`, `UNAVAILABLE_STATES`, `is_terminal`) waarop dit ADR's "terminaal ≠ uitspraak"-onderscheid leunt.
- `tests/test_oi1624_gate_absence_vs_rejection.py` — rode-naar-groene regressietests, inclusief een expliciete pin op het ongewijzigde OI-1576-contract (`TestNoRegressionOnZeroRecordAbsence`).

## Addendum (OI-1642, 2026-09-06) — afwezigheid geldt ook buiten scope

Bovenstaand ADR gaat ervan uit dat een bevestigde afwezigheid een IN-SCOPE record is:
`_find_gate_result` vond het, met matchende `branch`/`project_id`/`commit_sha`. In de
praktijk bleek dat een te sterke aanname. Zeven PR's (#1777-#1782, #1784) bleven na dit
ADR alsnog NO-GO: hun `codex_gate`-record dateert van vóór de fix hierboven en draagt
daarom geen `branch`/`commit_sha` — precies het veld dat `_record_matches_scope`
onvoorwaardelijk eist. `_find_gate_result` gaf dus `None` terug, niet omdat de poort nooit
gevraagd was, maar omdat het bestaande record de scope-toets niet haalde. De
peer-route hierboven wordt alleen bereikt via `result is not None and
_is_absent_without_verdict(result)` (regel "OI-1624" in
`check_review_gate_for_merge`), dus bij `None` werd hij nooit geraadpleegd — en omdat
`gate_recorder`'s overschrijfwachter (OI-1469/OI-1470) een terminaal record nooit
opnieuw laat schrijven, is dat record blijvend onzichtbaar voor de lezer.

De oplossing (`closure_verifier._find_gate_result_ignoring_scope` +
`_consult_peers_for_absence`) splitst wat `result is None` voorheen op een hoop gooide,
in drie gevallen:

- **(a) geen enkel record voor dit gate+pr_id.** Ongewijzigd NO-GO, geen peer-route.
  Dit blijft exact het contract van `TestUnrelatedRecordsNeverTakeOver`
  (`tests/test_oi1576_merge_door_takeover_evidence.py`) — gemeten: een peer-route die
  hier ONVOORWAARDELIJK zou lopen breekt die test twee keer (een losstaande pass en een
  takeover-claim zonder de gedeclareerde poort in het pad worden dan allebei ten
  onrechte een geldige ondertekenaar).
- **(b) een record bestaat, buiten scope, en is zelf een bevestigde afwezigheid**
  (`_is_absent_without_verdict`). Dezelfde peer-route als hierboven opent, via de
  gedeelde `_consult_peers_for_absence` — geen tweede, kunnen-uiteenlopen-kopie.
- **(c) een record bestaat buiten scope MET een uitspraak** (pass/fail op een andere
  branch/sha). Verouderd bewijs, geen afwezigheid — blijft NO-GO met de bestaande
  boodschap "geen review-gate resultaat gevonden".

De voorrangsregel en de zeven bewijs-invarianten uit het hoofd-ADR gelden onverkort voor
de ondertekenende peer in geval (b); er is niets verzwakt, alleen de vraag "bestaat er
een record" losgekoppeld van "is dat record in scope".

## Addendum (OI-1645, 2026-09-06) — een peer is een review-poort; CI is een tweede, onafhankelijke eis, geen ondertekenaar

Bij de merge van PR #1781 zei de deur letterlijk: "ci_gate droeg op dezelfde head
zelfstandig een geldige, volledig bewezen pass en telt als ondertekenaar bij afwezigheid
van de gedeclareerde poort". Een droge simulatie tegen een tmp results-dir met UITSLUITEND
`pr-1781-codex_gate.json` (status `unavailable`) en `pr-1781-ci_gate.json` (status `pass`,
alle zeven bewijs-invarianten aanwezig) bevestigde dit: GO, `evidence_gate="ci_gate"`, geen
enkele review-poort aanwezig.

`_find_peer_gate_results` (het hoofd-ADR hierboven) en `_find_takeover_successor_results`
(OI-1576) begrensden de kandidatenverzameling allebei tot `_KNOWN_GATES` — de
enum-afgeleide verzameling van poorten die de closure-verifier kan INTERPRETEREN. Dat is
een andere bewering dan "dit is een review": `ci_gate` toetst CI-checks (tests, lint,
build), nooit de codewijziging zelf, maar levert wel een volledig bewezen pass/fail-record
op dat elke bewijs-invariant haalt die `_merge_door_record_verdict` toetst. Niets in de
bewijsketen ving dit — de ketting was sterk, maar aan het verkeerde ding vastgemaakt.

**Beslissing:** interpreteerbaar zijn en een review zijn zijn twee aparte beweringen.
`closure_verifier._REVIEW_PEER_GATES` is een nieuwe, striktere verzameling — ook afgeleid
van de `Gate`-enum met een uitsluitingslijst met reden per poort
(`_NON_REVIEW_SIGNER_REASONS`), dezelfde discipline als `_GATES_NOT_IMPLEMENTED_BY_CLOSURE`
— die BEIDE routes nu delen:

- `ci_gate` uitgesloten: toetst CI-checks, geen code-review. Uitgesloten in BEIDE
  richtingen — kan geen pass ondertekenen, en (nieuw) een `ci_gate`-record met een
  `takeover_path` die een andere poort noemt telt ook niet als opvolger op de OI-1576-route.
  Symmetrisch ook geen "echte afkeuring": een `ci_gate`-FAIL in de resultaatstore blokkeert
  de peer-route niet langer. Dat is geen verzwakking — `pr_merge.main` roept
  `_run_ci_gate` (een LIVE `gh`-statusCheckRollup-toets) vóór de review-poort aan en geeft
  `EXIT_ERROR` bij een niet-groene CI-conclusie, dus een echte CI-afkeuring blokkeert de
  merge al via die aparte, onafhankelijke weg. Een verouderd `ci_gate`-resultaatbestand ook
  hier laten blokkeren zou dezelfde afkeuring dubbel tellen, geen extra veiligheid
  toevoegen.
- `wiring_gate` uitgesloten: staat al buiten `_KNOWN_GATES` (geen interpreteerbaar
  bewijs), dus a fortiori geen ondertekenaar.
- `claude_github_optional` blijft WEL een geldige peer. Het is optioneel — mag nooit
  draaien — maar wanneer `state="completed"` bereikt wordt, is `result_status` een echte
  Claude-code-review van de diff, geen CI-signaal (`claude_github_receipt.EVIDENCE_STATES`).
  Optioneel-maar-echt telt; alleen niet-review-bewijs is uitgesloten. De eigen
  afwezigheidstoestanden (`not_configured`, `configured_dry_run`) coderen sowieso nooit
  naar een beslist verdict, dus een niet-gedraaide instantie draagt toch al niets bij.

Een nieuwe enum-waarde die morgen wordt toegevoegd is automatisch ONGECLASSIFICEERD tot
iemand hem in een van beide verzamelingen zet — vastgepind door de drift-test in
`tests/test_oi1645_peer_is_review_gate.py`, naast de bestaande
`test_closure_verifier_gate_enum_drift.py`.

**Wat dit niet verandert:** de voorrangsregel en de zeven bewijs-invarianten uit het
hoofd-ADR blijven ongewijzigd voor elke poort die WEL in `_REVIEW_PEER_GATES` zit. Alleen
de kandidatenverzameling is versmald van "interpreteerbaar" naar "een review".
