# ADR-072 - De operator stuurt bij en het systeem leert ervan: een gebonden bron, een gesloten klassetabel, een voorstelwachtrij met eigen identiteit en een oorzaakanalyse die geen tekst bewaart

**Status:** Voorgesteld 11-11-2026 (T0, track `operator-bijsturing-leert`, rij OB-0); aangenomen zodra de T0 OB-0 merget.
**Datum:** 2026-11-11
**Besloten door:** de operator, opdracht van 10-11-2026 ~08:45Z (learning en dreaming vangen de bijsturing uit prompts, doen bij complexe gevallen een oorzaakanalyse en stellen een skill, een instructie of een feature voor); het plan `claudedocs/2026-11-07-naar-1-9/plannen/operator-bijsturing-leert.md` (revisie 2, plan-gate van 10-11-2026 met de verplichte wijziging van de tiebreaker) voor de besluiten 1 tot en met 18. Het nummer 072 is door de T0 toegewezen; ADR-071 is het hoogste eerder toegewezen nummer.
**Cross-refs:** ADR-007 (bindingsplicht per project), ADR-056 (zelfstandigheid meten), ADR-063 (onbekend is nooit 0), ADR-065 (leren is voorstellen; het besluitmodel van de voorstelstore), ADR-066 (dream-liveness; blijft ongewijzigd, zie besluit 17), ADR-067 (dezelfde bindingsregel als besluit 9 daar).

Dit ADR legt vast waartegen de rijen OB-1 tot en met OB-7 bouwen: welke bron gelezen wordt, hoe hij gebonden is, wat een operatorprompt is, welke klassen er zijn, hoe herhaling wordt geteld, hoe een oorzaakanalyse zonder bewaarde tekst werkt, hoe een voorstel zijn identiteit houdt en wie wat besluit. Het bevat geen code. Het hele lees- en tellingspad is deterministisch: filter, woordenlijsten, union-find en vaste drempels. Een model komt op één plek: de oorzaakanalyse van een geval dat de drempel haalt, omdat "waarom stuurde de operator bij en welk artefact had dat voorkomen" open taal is zonder vaste regel. Die stap loopt als gewone dispatch door de deur en kent een uitweg (`insufficient_evidence`), een validatie bij het innemen en een grootboekregel.

## Context

Vincent stuurt de T0 bij via prompts en niets in VNX leest die prompts. Wat het systeem ervan leert, schrijft de T0 met de hand op als memory-notitie. Gemeten op 10-11-2026 (`origin/main` `146b2306`):

- De T0-map van vnx-dev heeft 235 transcripts (1,6 GB). Over 7 dagen: 63 bestanden, 35 sessies met een operatorprompt, 118 operatorprompts met mediaan 63 tekens. Daarnaast 764 `task-notification`-berichten, 69 `command-message` en 55 `local-command-stdout`, die ook als `type: user` staan en geen bijsturing zijn.
- Zes regex-regels delen 71 procent van de 118 prompts in (26 vraag, 18 akkoord of vrijgave, 15 nieuw besluit, 11 statusvraag, 10 correctie, 4 handmatige actie, 34 ongeclassificeerd). Deterministisch indelen draagt dus het grootste deel.
- 477 memory-notities, waarvan 71 met `OPERATOR` en 53 met `EIGEN MISSER`: de handmatige versie van wat dit spoor automatiseert. Het systeem leest ze niet terug.
- Herhaling is zichtbaar en wordt niet geteld: drie varianten van "hoe ver zijn we tot 1.9" in verschillende sessies, twee keer "je mag promoten". Een herhaalde statusvraag wijst op een ontbrekend overzicht, een herhaalde vrijgave op een poort die de T0 zelf mag nemen. Dat zijn voorstellen voor een feature of instructie, geen preventieregels.
- ADR-065 leert van receipts en eindigt in `prevention_rules`. ADR-067 controleert verwijzingen tussen bakken. Geen van beide leest een operatorprompt of eindigt in een voorstel voor een skill, `CLAUDE.md`, rolbestand of tool.
- `~/.claude-salesminds/projects` is een symlink naar `~/.claude/projects`. Klantsessies schrijven dus in dezelfde boom onder een andere slug. Een lezer die op een glob vertrouwt, leest klantdata.

## Besluit

### 1. Eén ADR, nummer 072

De regels van het plan staan in dit ADR: bron, binding, klassen, drempel, voorstelsoorten, besluitrecord en privacy. Rij OB-0 schrijft ze op. Rijen OB-1 tot en met OB-7 bouwen ertegen en nemen niets over dat hier niet staat.

### 2. De bron is gebonden, nooit gegokt

Lezen gebeurt alleen via `state/steering_sources.json` in de store van het project, schema `steering_sources/1`, met `schema`, `project_id`, `transcript_dirs` (een lijst slugs), `memory_dir` en `bound_at`. Dezelfde regel als ADR-067 besluit 9: `project_id` is gelijk aan `project_id_from_state_dir` en elk pad ligt na `resolve()` onder `~/.claude/projects/`, anders staat de bron op `unbound`. Een slug die `clients`, `Business` of `salesminds` bevat (kleine letters) weigert `bind` met exit 2. Er is geen glob over `projects/`. De staat per bron is `ok`, `absent`, `unbound` of `error`.

**Containment per bestand:** elk transcript- en notitiebestand wordt na `resolve(strict=True)` opnieuw getoetst. Ligt het opgeloste pad buiten zijn gebonden map, ook via een symlink of een symlink-lus, dan is die bron `error` met de bestandsnaam en wordt er niets uit gelezen. Eigenaar: OB-1.

### 3. Een operatorprompt is een gesloten filter

Een regel telt als operatorprompt als hij `type: user` is, niet `isMeta`, geen `isCompactSummary`, geen `tool_result` bevat en de tekst niet begint met een systeemtag (`<task-notification`, `<command-`, `<local-command-`, `<system-reminder`, `<bash-`). De rotatieprompt en een `/goal`-hervatting tellen apart als `automatisch`. `[Request interrupted by user` is de klasse `onderbreking`. Elke uitgesloten regel wordt per reden geteld, zodat de dekking zichtbaar blijft. Een prompt-id is `sha256(sessie | uuid van de regel)`, ingekort tot 16 hex. Eigenaar: OB-1.

### 4. De klassen zijn één geordende tabel

`STEERING_CLASSES`, bovenaan eerst: `handmatige_actie` (begint met `!`), `onderbreking`, `correctie`, `nieuw_besluit`, `vrijgave`, `statusvraag`, `vraag`, `ongeclassificeerd`. De voorwaarden zijn woordenlijsten en vormregels in `scripts/lib/operator_steering_class.py`, Nederlands en Engels. Een onbekende vorm valt door naar `ongeclassificeerd` en sluit nooit een prompt uit. Het rapport toont het aandeel `ongeclassificeerd`; boven 30 procent is de status `low_coverage`, geen fout. De tabel groeit per PR met een fixturetest, nooit met een model. Eigenaar: OB-2.

### 5. Herhaling is deterministisch (vervangen door besluit 15)

Het oorspronkelijke besluit koppelde twee prompts in verschillende sessies met dezelfde klasse en een Jaccard van minstens 0,5 op woord-bigrammen, met een `group_id` uit `sha256` over de gesorteerde prompt-id's. **Besluit 15 vervangt besluit 5**: de verwantschapsregel en de groepsvorm staan daar. Besluit 5 blijft alleen genoemd omdat het `group_id` en de prompt-id van besluit 3 gelden.

### 6. Een memory-notitie terugvinden is een tijdvenster plus woorden

OB-3 leest de beschrijving van elke notitie met `OPERATOR` of `EIGEN MISSER`. Draagt die een tijd (`DD-MM ~HH:MMZ`), dan zoekt hij operatorprompts binnen 45 minuten ervoor. Zonder tijd: dezelfde dag. Een treffer vraagt ook minstens twee gedeelde inhoudswoorden. Het resultaat per notitie is `found` (met prompt-id's), `ambiguous` of `not_found`. De aanvulling voor notities zonder jaar staat in besluit 15. Eigenaar: OB-3.

### 7. De drempel voor een analyse

Een geval is een herhalingsgroep met minstens twee sessies, of een `correctie` of `handmatige_actie` waarbij OB-3 een `EIGEN MISSER`-notitie vond. `vraag` en `vrijgave` worden alleen een geval als groep van minstens drie. Alles daaronder blijft telling. Herhaling telt sessies, niet prompts: een groep binnen één sessie is geen herhaling.

### 8. De oorzaakanalyse is een dispatch door de deur

Geen eigen `claude -p`, geen API-aanroep, geen nieuwe provider. `scripts/operator_steering_rca.py stage --case <id>` staget een bundel met rol `research-analyst`, model sonnet, lane `claude_headless`. De T0 vuurt hem zoals elke bundel. Het rapport bevat één JSON-blok `steering_rca/1`: `case_id`, `oorzaak` (`ontbrekende_regel`, `ontbrekende_skill`, `ontbrekend_overzicht`, `poort_te_streng`, `tool_faalt_stil`, `anders` of `insufficient_evidence`), `artefact_soort` (`skill`, `claude_md`, `rolbestand`, `memory`, `tool`, `feature`), `doelpad`, `voorstel` en `bewijs` (prompt-id's, notities, receipts). `ingest` valideert het blok; een ongeldig blok is een weigering met de reden en geeft geen voorstel. Wat de opdracht draagt, staat in besluit 12. Eigenaar: OB-6a (`stage`) en OB-6b (`ingest`).

### 9. De voorstelwachtrij is een eigen store met het besluitmodel van ADR-065

`scripts/lib/steering_proposal_store.py` (`steering_proposal_store`): projectie `state/steering_proposals.json`, record `state/steering_proposal_decisions.ndjson` (append-only, eerst het record en fsync, dan de projectie, `reconcile()` bij elke lees- of schrijfroute). Een voorstel kent `pending`, `approved`, `rejected` en `applied`. `approved` zet niets door: de wijziging zelf is een gewone dispatch of een handeling van de operator, en `applied` wordt alleen gezet met een PR-nummer of een commit (`applied_ref`). De preventieregel-store van ADR-065 blijft ongewijzigd; dit is een tweede soort voorstel en geen nieuwe regel daarin. Eigenaar: OB-5a.

### 10. Afgewezen komt alleen terug met nieuw bewijs (de sleutel vervangen door besluit 14)

Het principe blijft: een afgewezen voorstel wordt alleen weer `pending` met bewijs van na de afwijzing, anders telt het als `suppressed_rejected` in het rapport. **Besluit 14 vervangt de sleutel van besluit 10** (`sha256(artefact_soort | doelpad | genormaliseerd voorstel)`, die aan gegenereerde tekst hing).

### 11. Privacy

**Prompttekst blijft lokaal.** Het rapport (`steering_report.json`) bevat tellingen, prompt-id's en klassen, nooit tekst. `--show` print tekst naar de terminal en slaat hem nooit op. Business-transcripts en klantdata vallen buiten deze track tot een apart besluit van de operator. Dit is een regel waartegen de code getest wordt: een test greept het rapport, de store en de gestagede spec op een fixtureprompt en verwacht nul treffers. **De zin van dit besluit dat de RCA-opdracht prompttekst draagt, is vervangen door besluit 12** (de opdracht draagt geen tekst).

### 12. De RCA-bundel draagt geen tekst; de dispatch leest live

Dit vervangt de zin in besluit 11 dat de RCA-opdracht prompttekst draagt. De gestagede spec en de opdracht bevatten alleen `case_id`, prompt-id's, de namen van de gevonden notities, de context-id's van besluit 13, de kandidaat-paden en `snapshot_sha256`. De RCA-bouwer haalt de tekst op met `python3 scripts/operator_steering.py show --case <case_id>`. Dat commando leest live uit de gebonden mappen, past de containment van besluit 2 op elk bestand toe, print naar stdout en schrijft niets. Het rapport en het `steering_rca/1`-blok noemen prompts alleen bij id. `ingest` weigert een rapport waarin een aaneengesloten stuk van 40 tekens of meer uit een prompt van de case letterlijk voorkomt. **Bewaring:** spec en rapport bevatten geen prompttekst, dus de gewone bewaring van dispatches en rapporten geldt. Het transcript van de RCA-bouwer zelf staat in dezelfde boom `~/.claude/projects/` als de bron, met dezelfde eigenaar en dezelfde bewaring. Er komt geen nieuwe plek bij waar prompttekst staat; dit ADR noemt dat met naam. Wissen hoort niet bij deze track. Eigenaar: OB-6a en OB-6b.

### 13. De case draagt context en "te weinig bewijs" is een geldige uitkomst

Per prompt van een case legt OB-3 vast: de id's van de tool-aanroepen in de assistent-beurt vóór de prompt (maximaal vijf, met de toolnaam en de eerste regel van een `Bash`-commando, nooit de uitvoer) en de dispatch-id's en PR-nummers die in die beurt of in de prompt genoemd worden. Dat is de begrensde context; `show --case` toont hem naast de prompt. `steering_rca/1` kent `oorzaak: insufficient_evidence`; dan zijn `artefact_soort`, `doelpad` en `voorstel` leeg en volgt er geen voorstel. `ingest` controleert dat elk prompt-id en elk context-id in `bewijs` bij de snapshot van de case hoort; een id daarbuiten is een weigering. Eigenaar: OB-3 (context) en OB-6b (controle).

### 14. Een voorstel heeft een identiteit los van zijn tekst

Dit vervangt de sleutel van besluit 10. `proposal_id = sha256(project_id | oorzaak | artefact_soort | doelpad)`, ingekort tot 16 hex. De tekst is een **revisie** (`revision` 1, 2, ...) onder die id. Een tweede RCA die hetzelfde doelpad met andere woorden voorstelt, wordt een nieuwe revisie van hetzelfde voorstel. Afwijzen geldt voor de id. Heropenen gebeurt alleen als een nieuwe revisie bewijs draagt met een prompt waarvan de `timestamp` uit het transcript (de enige gezaghebbende tijd) na `decided_at` van de afwijzing ligt. Dan schrijft de store eerst een record `decision: reopened` met de nieuwe prompt-id's en de revisie, en daarna de projectie `pending`. `reconcile()` herbouwt de projectie uit alle records in volgorde, dus afwijzing en heropening blijven allebei zichtbaar. Zonder nieuw bewijs wordt de revisie bewaard en telt hij als `suppressed_rejected`. Eigenaar: OB-5a.

### 15. Groeperen als samenhangende componenten, korte teksten exact

Dit vervangt besluit 5 en vult besluit 6 aan. Twee prompts zijn verwant als ze in verschillende sessies staan, dezelfde klasse hebben en een Jaccard-overlap van minstens 0,5 hebben op de verzameling woorden plus woord-bigrammen na normalisatie (kleine letters, zonder leestekens en getallen, stopwoorden eruit). Een herhalingsgroep is een samenhangende component in de graaf van verwante paren (union-find): uniek, ongeacht de volgorde. Een tekst met minder dan drie woorden na normalisatie ("go", "done", "akkoord") is alleen verwant aan een tekst met precies dezelfde genormaliseerde vorm. Een notitie zonder volledig jaar krijgt het jaar uit de `mtime` van het bestand; ligt `DD-MM` dan na de mtime-datum, dan het jaar ervoor.

**Nauwkeurigheid:** de T0 labelt in het gesprek 30 echte prompts met een klasse en 10 notities met de prompt die erbij hoort. Klaar is minstens 80 procent overeenstemming op `correctie`, `handmatige_actie` en `vrijgave` samen, en van de 10 notities minstens 7 `found` met de juiste prompt en 0 `found` met een verkeerde. De labels staan alleen in het gesprek, nooit in de repo; de tests gebruiken verzonnen parafrases. Eigenaar: OB-3.

### 16. Een case heeft een stabiele id en een grootboek

`state/steering_cases.ndjson` (append-only, `O_APPEND` en fsync) houdt per case `case_opened` (met `case_id`, klasse, anker-prompt-id), `case_members` (nieuwe prompt-id's), `rca_staged` (dispatch-id, `snapshot_sha256`, ISO-week) en `rca_ingested` of `rca_refused`. `case_id = sha256(project_id | klasse | anker-prompt-id)`, waarbij het anker de vroegste prompt is op het moment dat de case opent. Een latere component die het anker bevat, of een prompt die al lid is, valt onder dezelfde case, ook als het anker uit het venster schuift. Stagen is idempotent: een case met een `rca_staged` zonder `rca_ingested` of `rca_refused` wordt niet opnieuw gestaged. De week is de ISO-week in UTC; het plafond is vijf `rca_staged`-regels per project per week, geteld uit het grootboek, dus een herhaalde run haalt nooit een zesde. `ingest` vergelijkt het `snapshot_sha256` uit de spec met de snapshot die het rapport noemt; een latere verversing van het rapport maakt een openstaande RCA niet ongeldig. Eigenaar: OB-6a.

### 17. Dream host de stap; ADR-066 verandert niet

Het koppelpunt is het commando dat de launchd-job `com.vnx.auto-dream` draait (`vnx dream run`). OB-7 voegt daar één stap `steering` aan toe, na de consolidatie, ongeacht of die slaagde, oversloeg of faalde. De stap draait `report` (dagelijks venster: de laatste 7 dagen) en `stage` (weekplafond van besluit 16). Faalt de stap, dan faalt dream niet: de stap schrijft `steering_report.json` met `status: "error"` en de reden en schrijft **geen** dream-event.

**Producent-consumentregel.** Dream host de stap; dream is er niet de producent van. `dream_liveness/1` (ADR-066) heeft één producent en die verandert niet. De stap voegt geen eventtype toe aan de log die `assess()` leest. **ADR-066 verandert niet.** De versheid van de stap heeft een eigen consument: een doctorcheck op `steering_report.json`, ouder dan 36 uur of `status: "error"` is een WARN, nooit een FAIL. Eigenaar: OB-7. Dream vuurt geen RCA-bundels, alleen stagen (open vraag 2 van het plan, tot `zelfstandigheid-meten` een meting heeft).

### 18. Kleinere PR's met vaste grenzen en elk commando een eigenaar

De groottegrens is 300-450 regels (operatorbesluit 09-11-2026); de 150-300 uit `AGENTS.md` is ouder. De rijen zijn vooraf gesplitst langs de grenzen uit de plan-gate: OB-4 in bibliotheek en CLI, OB-5 in store en operator-CLI, OB-6 in stagen en innemen. De bouwer meet de grootte na elke ronde en splitst bij overschrijding in `-a` en `-b` op dezelfde grens. De externe afhankelijkheid op `self-learning-loop-reactivate` D3 (`decide-rule` in `vnx_cli/commands/learning.py`) staat in het plan: OB-4b start pas na de merge van D3.

## Contracten

Bestanden onder `state/` van de projectstore:

| Bestand | Schema of vorm | Schrijver | Eigenaar |
|---|---|---|---|
| `steering_sources.json` | `steering_sources/1`, zie besluit 2 | `operator_steering.py bind` | OB-1 |
| `steering_report.json` | `steering_report/1`: `project_id`, venster, `bronstaat`, `tellingen` per klasse, `uitgesloten` per reden, `dekking` (aandeel niet-`ongeclassificeerd`), `herhalingsgroepen` (`group_id`, klasse, sessies, prompt-id's), `notities` (naam, `found`/`ambiguous`/`not_found`, prompt-id's), `gevallen`, `suppressed_rejected`; geen tekst | `operator_steering.py report` | OB-4a |
| `steering_cases.ndjson` | grootboek van besluit 16 | `operator_steering_rca.py` | OB-6a |
| `steering_proposals.json` | projectie van de voorstelstore | `steering_proposal_store` | OB-5a |
| `steering_proposal_decisions.ndjson` | besluitrecord: `decision_id`, `proposal_id`, `revision`, `decision`, `reason`, `decided_by`, `decided_at`, `project_id`, `evidence_prompt_ids`; bij `applied` ook `applied_ref` | `steering_proposal_store` | OB-5a |

`steering_rca/1` is het JSON-blok van besluit 8 en 13. Het operatorprompt-record (`prompt_id`, `session_id`, `ts`, `klasse`, `tekst`, `uitgesloten_reden`) bestaat alleen in geheugen en wordt nooit op schijf gezet.

CLI-werkwoorden en hun eigenaar:

| Werkwoord | Eigenaar |
|---|---|
| `scripts/operator_steering.py bind` en `show --prompt` | OB-1 |
| `vnx learning steering report --json\|--md\|--show` en `show --case` | OB-4b |
| `vnx learning decide-steering` en `vnx learning steering proposals` | OB-5b |
| `scripts/operator_steering_rca.py stage` | OB-6a |
| `scripts/operator_steering_rca.py ingest` | OB-6b |
| de stap `steering` in `vnx dream run` en de doctorcheck | OB-7 |

## Consequenties

- Een bijsturing van de operator is te tellen, te groeperen en terug te vinden in memory, zonder dat tekst het project verlaat of in een state-bestand komt. De privacyregel van besluit 11 en 12 is een testplicht, geen afspraak.
- **Leren blijft voorstellen** (besluit 2 van 07-11-2026, ADR-065). Geen codepad past een voorstel toe zonder besluit in de store: `approved` zet niets door, en geen codepad in deze track schrijft een skill, `CLAUDE.md`, rolbestand of tool. Een voorstel eindigt in een gewone dispatch of een handeling van de operator; `applied` vraagt een PR-nummer of commit.
- De preventieregel-store van ADR-065 en `ingest_approved_rules` blijven onaangeroerd. Een tweede soort voorstel krijgt een eigen store, zodat een goedkeuring hier nooit als `failure_prevention` in `prevention_rules` belandt.
- De RCA kost een bouwplek per case. Het weekplafond van vijf bundels per project begrenst dat, en dream stagent alleen.
- Dream krijgt een stap erbij zonder dat ADR-066 of `dream_liveness/1` verandert. Een falende stap is zichtbaar als WARN in de doctor, niet als dream-fout.
- Een lezer zonder binding leest niets. Nieuwe slugs vragen een expliciet `bind`; klant- en businessmappen vallen buiten tot een apart besluit van de operator.
- Buiten deze track: T1-T3-transcripts (die prompts komen van de T0), de business-bron en het vuren van RCA-bundels door dream.

## Terugdraaien

Alles in deze track is data in `state/` en code die alleen leest of een voorstel opslaat. Terugdraaien is de stap `steering` uit `vnx dream run` halen en `steering_sources.json` verwijderen: zonder bindingsbestand staat elke bron op `unbound` en leest geen enkel pad nog een transcript. De stap leest dan niets meer en schrijft hooguit een `steering_report.json` met `status: "error"`, dat de doctor als WARN toont. Er is geen datamigratie: `steering_cases.ndjson`, `steering_proposals.json` en `steering_proposal_decisions.ndjson` zijn eigen bestanden die niemand anders leest en mogen blijven staan. De regel voor `prevention_rules` en ADR-065 hoeft niet terug, want daar is niets aan veranderd.
