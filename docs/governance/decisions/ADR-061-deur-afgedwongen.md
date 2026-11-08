# ADR-061 - De garantie van de deur is een permit: één identiteit, één schrijver per artefact, een receipt dat het weet, een levensloop en een activering

**Status:** Aangenomen 08-11-2026 (T0, track `deur-afgedwongen`, rij D1).
**Datum:** 2026-11-08
**Besloten door:** de operator voor besluit 1 (beslissing 1 van de 1.9-sprint, 07-11-2026, hieronder letterlijk); de T0 voor alle overige besluiten, op het plan `claudedocs/2026-11-07-naar-1-9/plannen/deur-afgedwongen.md` (revisie 2, plan-gate-bevindingen 1 tot en met 7). ADR-061 was vrij op `origin/main` `391839fa9` (08-11-2026): `docs/governance/decisions/` telt daar 58 bestanden, de hoogste is ADR-059, en ADR-060 is geclaimd door `gate-profiel-per-project` P9 (in vlucht).
**Cross-refs:** ADR-005 (het grootboek is de bron), ADR-023 (de hash-keten), ADR-024 (de enige dispatchdeur), ADR-027 (ondertekende attestatie), ADR-035 (receipt v2), ADR-038 (uitkomstidentiteit van een receipt), ADR-046 (één poortmotor, vier uitkomsten, één schrijver), ADR-051 (een geplande taak volgt de pin), ADR-054 (het governance-record is geen eindregel), ADR-057 (dezelfde vorm: één bron van waarheid, fail-closed bewijs, default-OFF handhaving).

De operator heeft op 07-11-2026 (beslissing 1 van de 1.9-sprint) bepaald:

> "deur-afgedwongen: permit-check (verplichte status van de deur op de exacte kop; noodroute als override met reden)."

Dit ADR legt vast wat die ene regel betekent, wie de toestemming mag maken, hoe lang zij leeft, wat een lezer ermee doet en in welke volgorde de deur wordt aangezet. Het bevat geen code. Alles erin is een regel over data: een YAML-entry, een check-run, een exitcode, een lijst PR's, een paginagrens. Er zit geen model in dit besluit en er is dus ook geen modelgestuurd deel met een veiligheidsnet. De rijen D2 tot en met D7c van het plan bouwen ertegen.

## Context

Gemeten op `origin/main` `391839fa9` (08-11-2026), tenzij een andere stand genoemd wordt.

Er is geen ADR over de deursemantiek. `docs/governance/decisions/` telt 58 bestanden en de hoogste is ADR-059; `vnx-gate/door` komt in de hele getrackte boom niet voor. Wat er wel is, is een runbook dat de kale merge legaliseert in plaats van verbiedt: `docs/operations/FORGE_GATE.md` §7.2 noemt zijn stap 3 "precies één ongegovernde stap".

De drie deelvragen uit het doel hebben elk een gemeten stand:

- **Vertrouwde status-identiteit: die bestaat.** `vnx-gate/review` is gebonden aan `app_id` `4869217` (`scripts/forge/branch_protection.yaml:65`), en het `app:`-blok met dezelfde identiteit staat op `scripts/forge/branch_protection.yaml:120-122`. Waarom een App en niet het operator-token staat in `scripts/lib/forge_check_run.py:13-17`: een check van een mens met een token is voor branch protection niet te onderscheiden van "de poort zei ja".
- **Gescheiden rechten: die zijn er niet.** Er is één prefix voor álle `vnx-gate/*`-checks (`scripts/lib/forge_gate_publisher.py:133`) en één App die ze allemaal publiceert. De review-recorder publiceert de samenvatting met diezelfde identiteit (`scripts/lib/gate_recorder.py:1709`, functie `publish_forge_review_summary` op `:1714`). De App is op één repo geïnstalleerd (`scripts/forge/branch_protection.yaml:116-122`).
- **Eigenaar van het receipt: afleesbaar, niet vastgelegd.** `pr_merged` wordt geschreven door `_emit_receipt` (`scripts/pr_merge.py:1892`) met `source="pr_merge"` (`scripts/pr_merge.py:1964-1968`). De receipt draagt `preflight_gates` altijd (`scripts/pr_merge.py:1961-1963`), maar geen top-level `head_sha` of `merge_sha` en geen deuridentiteit. De deur toetst zijn eigen integriteit wel — `_door_blob_hash_gate` (`scripts/pr_merge.py:1392`) over de acht paden van `_DOOR_INTEGRITY_PATHS` (`scripts/pr_merge.py:1380-1389`) — en schrijft die uitkomst nergens in.

Een merge buiten de deur wordt niet geweigerd. `main` eist 15 contexten (`scripts/forge/branch_protection.yaml:29-65`), en geen daarvan zegt iets over de deur als actor; `enforce_admins: true` helpt daar niet tegen, want de checks zijn vervulbaar door iedereen met schrijfrechten. De merge-queue is vandaag `null`, maar niets bewaakt dat: `fetch_live_protection` (`scripts/lib/forge_protection_drift.py:583`) leest protection, repo en rulesets (`:614`), en rulesets worden alleen op naam vergeleken (`:965-968`).

Er is geen dagelijkse controle. `compute_health` (`scripts/ledger_health.py:527`) kent vier checks (`:541-546`) en geen daarvan leest GitHub. Wat het gat kán vinden, schrijft het dicht: `backfill()` (`scripts/backfill_pr_merged_receipts.py:249`) vergelijkt gemergede PR's met het grootboek en émít dan een receipt (`:189`) — en zijn lezer is fail-open: `_gh_list_merged_prs` geeft `return []` bij elke gh-fout (`:170`, `:174`, `:183`, `:186`), en een lege lijst leest als "geen gat".

Twee meetbare gevolgen van de bestaande lezers bepalen de vorm van dit besluit:

- Een intrekking als `failure` is onomkeerbaar. `_classify_present` (`scripts/lib/ci_contexts.py:315`) is bewust conservatief op duplicaten: "any terminal non-passing conclusion makes it `failed`, even when a sibling run of the same name passed" (`:319-323`). Een tweede publicatie op dezelfde kop kan dat niet meer rechtzetten.
- Een verplichte deurcheck maakt `vnx pr-ready` permanent rood. `classify_contexts` (`scripts/lib/ci_contexts.py:491`) geeft `STATE_UNVERIFIED` voor een context zonder check-run en zonder workflow-job (`:537-551`), en `required_contexts_gate` maakt daar `SKIPPED_UNVERIFIED` van (`scripts/lib/required_contexts_gate.py:99`). Voor `vnx-gate/review` is dat juist; voor de deurcheck geldt het omgekeerde, want alleen de deur kan hem zetten en pas wanneer hij merget.

## Besluit

### 1. De garantie betekent "door de deur toegestaan", niet "door de deur uitgevoerd"

De verplichte status `vnx-gate/door` is een **permit**: een uitspraak over één commit die alleen de deur op de exacte kop zet, waarna de merge aan die kop gepind is (`--match-head-commit`, `scripts/pr_merge.py:1879`).

Een statuscheck kan toestemming dragen, geen uitvoering. Een check-run is een uitspraak over één commit — `_require_head` zegt dat met zoveel woorden (`scripts/lib/forge_gate_publisher.py:220-240`: "een conclusie hangt aan een kop") — en niets in GitHub verbiedt dat dezelfde commit daarna via de API gemerged wordt. Wie de garantie "uitgevoerd door de deur" noemt, verkoopt een bewijs dat er niet is; wie haar "toegestaan door de deur" noemt, kan het verschil benoemen tussen wat de check bewijst en wat hij niet bewijst. Dat verschil staat in §5 (blootstelling) en niet weggeschreven.

### 2. Eén identiteit: de bestaande `vnx-gate`-App, en dit is géén credential-scheiding

De deur publiceert `vnx-gate/door` met de bestaande App `vnx-gate`, `app_id` `4869217` (`scripts/forge/branch_protection.yaml:120-122`). Er komt geen tweede App.

Dat is niet alleen een voorkeur. `parse_protection_config` weigert een `vnx-gate/*`-entry zonder gebonden `app_id`, zonder `app.app_id`, of met een ander `app_id` dan de tekenende App (`scripts/lib/forge_protection_drift.py:388-422`, binnen `parse_protection_config` op `:351`). Een tweede identiteit is dus een schemawijziging van twee PR's (het contract in `docs/operations/FORGE_GATE.md` §10) en geen configregeltje. Eén App betekent ook één faalmodus en één terugweg.

**Wat "één schrijver per artefact" hier wél en niet betekent.** Wel: de deurcheck wordt door precies één codepad gepubliceerd (`_run_door`, `scripts/pr_merge.py:2438`), de YAML bindt de check aan de App, en D3 reserveert de slug `door` op dezelfde manier waarop `review` vandaag geweigerd wordt (`check_run_name`, `scripts/lib/forge_gate_publisher.py:180-200`). Niet: gescheiden credentials. Dezelfde App-sleutel die `vnx-gate/door` maakt, kan ook `vnx-gate/review` maken. Dat is een **aanvaard restrisico** op besluit 1 van de operator, met een prijs die erbij hoort: een echte scheiding vraagt twee App-registraties, twee keychain-items, een schemawijziging van twee PR's en een operator-stap per consumer-repo. Die prijs wordt hier niet betaald en dus ook niet geclaimd.

### 3. `door` is de zesde en laatste preflight, maar haar bewijs zijn de vijf poorten daarvóór

`PREFLIGHT_ORDER` (`scripts/pr_merge.py:303-309`, nu `ci, review, adr, contract_invalid, branch_protection`) krijgt `door` als zesde en laatste naam. De preflight publiceert de check op de kop, leest hem terug van GitHub, en weigert als wat er staat geen `success` van de eigen App is of niet de run is die deze aanroep publiceerde.

**Het bewijs is de poorten vóór de deur, niet de deur zelf.** De deurpoort kan geen `GO` zijn voordat zij gepubliceerd heeft: `_preflight_ledger` (`scripts/pr_merge.py:394`) merkt een poort die nog geen oordeel had als `not_evaluated`, en de deur krijgt haar oordeel pas uit de teruglezing van de check-run. Een contract dat alle zes namen van `PREFLIGHT_ORDER` met `verdict: "GO"` eist vóórdat de deur publiceert, is dus cirkelredenerend en kan nooit slagen. Daarom draagt het evidence-bestand precies de namen van `PREFLIGHT_ORDER` **zonder** `door`, in die volgorde — op `main` van vandaag de vijf `ci, review, adr, contract_invalid, branch_protection` — elk met `verdict` exact `"GO"`. De uitgever valideert de namen en hun volgorde, niet alleen het aantal: een ontbrekende, een extra of een verkeerd geordende naam is elk een eigen weigeringstekst. Het eigen record van de deur (`verdict` uit de teruglezing) wordt pas aan de preflight-ledger toegevoegd nadat de teruglezing is geslaagd; die volledige lijst van zes is wat in de `pr_merged`-receipt naast `door_check` landt.

- **Laatst, omdat** de toestemming zo dicht mogelijk bij de merge moet liggen; omdat hij afhangt van de branch-protection-preflight, die `main`'s `checks[]` leest (`main_config`, `scripts/pr_merge.py:1552`, uit `_fetch_protection_yaml` op `:524`, aangeroepen op `:1544`); en omdat een groene check op een kop die daarna alsnog door een eerdere poort geweigerd wordt, een valse toestemming is.
- **Teruglezen, omdat** een POST die 201 antwoordt geen bewijs is dat de context op de kop groen staat, en een verloren antwoord een bestaand faaltype is (`_api_request`, `scripts/lib/forge_check_run.py:464-497`). De deur eist daarom drie dingen tegelijk: `conclusion == "success"`, de eigen `app.id`, en `id == <de run die deze aanroep maakte>`. Kan hij die run niet aanwijzen, dan weigert hij.

De eigen `check_run_id` is daarmee de identiteit van de toestemming: `permit_id` is `<app_id>:<head_sha>:<check_run_id>`.

### 4. De deur publiceert alleen als `main` de check eist — dat is de ene configregel per project

De deur publiceert `vnx-gate/door` dan en alleen dan wanneer `vnx-gate/door` in de `checks[]` van `main`'s `branch_protection.yaml` van het doelproject staat. Die lijst leest hij al in de branch-protection-preflight (`scripts/pr_merge.py:1552`). Staat de entry er niet, dan publiceert de deur niets en schrijft hij `door_check: {"schema": 1, "state": "not_required"}` in de receipt.

Eén bron van waarheid, geen tweede vlag die met de eerste kan divergeren — en "niet gedekt" wordt **meetbaar** in plaats van stil: een project zonder entry draagt dat in elke receipt en de dagelijkse controle telt het.

**Dit is de terugweg.** De entry is letterlijk één regel per project, in het bestand dat de deur al leest en al vergelijkt. Weghalen is een verzwakking: `is_weakening` (`scripts/lib/forge_protection_drift.py:973`) eist dan `--allow-weaken "<reden>"` (`scripts/forge/apply_branch_protection.py:282-287`, de vlag op `:371`), en de apply schrijft `branch_protection_applied` met `weak_fields` (`:214`, `:231`). Dat is precies "de noodroute als override met reden in het grootboek" uit besluit 1 van de operator.

**Waarom géén nieuw `door:`-veld in de YAML.** `FORGE_GATE.md` §7 zegt van de deur: "De deur zelf kent het verschil niet en krijgt er ook geen ontsnappingsluik voor", en §7.2 stap 3: "Er is geen vlag die dit opent, en die komt er ook niet." Een `door: {enforcement: off}` die een merge doorlaat zou precies dat luik zijn, en het zou een schemawijziging van twee PR's vragen (§10) voor iets wat in de bestaande vorm één regel in een bestaande lijst is.

### 5. De toestemming wordt **verwijderd**, niet rood gezet; de blootstelling is begrensd

De deur trekt de toestemming in door **de check-run te verwijderen** (`DELETE /repos/{owner}/{repo}/check-runs/{id}`). Lukt dat niet, dan publiceert hij als fallback `conclusion: failure` — dat blokkeert GitHub hoe dan ook — en meldt dat met een eigen merker (`revoke_outcome: failed_delete_published_failure`).

**Waarom verwijderen en niet rood zetten.** `_classify_present` (`scripts/lib/ci_contexts.py:315`) houdt de context `failed` zolang één run op die naam niet-slagend is (`:319-323`). Een `failure` als intrekking is dus een eenrichtingsdeur op die kop: een tweede publicatie kan het niet meer rechtzetten. Verwijderen brengt de context terug naar "geen run" — de toestand die §6 al kent — waarna de volgende deurrun schoon publiceert. De bewijswaarde blijft in het grootboek: `check_run_id`, `permit_id`, `revoked: true`.

**De drie triggers, alle drie expliciet.**

1. `_do_merge` (`scripts/pr_merge.py:1861`) geeft een niet-gemergede uitkomst terug (kop verschoven, gh-fout, weigering).
2. De uitkomst is **onbekend**: `gh` meldt succes maar `_pr_actually_merged` (`scripts/pr_merge.py:1836`) kan de PR-state niet lezen of leest geen `MERGED`. Dan intrekken, `merge_outcome: "unknown"`, exit non-zero.
3. Een uitzondering na publicatie en vóór een bevestigde merge.

**Wat een crash niet kan.** Een SIGKILL, segfault of stroomuitval tussen publicatie en merge laat de toestemming staan; geen enkel in-process mechanisme dekt dat. Daarvoor is de **verzoening met intrekking** (rij **D7c**, nieuwe rij na D7b en D3 — niet D7b): zij leest elke **open** PR, en een groene `vnx-gate/door` op de huidige kop die ouder is dan `--permit-ttl` (standaard 30 min), of waarvan de `permit_id` in geen enkele `pr_merged`/`pr_merge_refused`-regel voorkomt, is een bevinding **en** wordt verwijderd via hetzelfde publisher-delete-pad als de in-process intrekking: drie pogingen met backoff, daarna de `failure`-fallback met merker en een blocker-open-item.

**De grens is de job-cadans plus de TTL, niet de TTL.** Melden alleen is geen grens: niets op GitHub laat een check-run verlopen, dus tussen de publicatie en de volgende verzoeningsrun blijft de merge-toestemming staan, hoe oud zij ook is. Met de dagelijkse job is de grens daarom hooguit **24 uur plus 30 minuten**. Een kortere cadans is een keus van de operator en de enige manier om die grens te verkorten.

**De blootstelling, in drie gevallen met een grens.** Dit is de tabel die "seconden" uit de eerste planrevisie vervangt; zij is de claim.

| Geval | Wat er overblijft | Grens |
|---|---|---|
| merge lukt | niets: de kop staat in `main`, de PR is dicht, een kale merge op die PR kan niet meer | 0 |
| merge faalt, deur leeft | de toestemming tot de verwijdering terugkomt | seconde tot tientallen seconden; 3 pogingen met backoff, daarna fallback + blocker |
| crash of verloren antwoord | de toestemming tot de verzoening haar **intrekt** (zelfde delete-pad, daarna fallback + blocker) | **job-cadans + `permit-TTL`**: bij de dagelijkse job ≤ 24 u + 30 min; niets op GitHub laat haar vanzelf verlopen |

### 6. De lezer kent drie toestanden, met een herstelpad in de derde

`ci_contexts` krijgt twee expliciete toestanden voor exact de context `vnx-gate/door`:

| Toestand | Voorwaarde | Blokkeert | Wat de lezer zegt |
|---|---|---|---|
| `published_at_merge` | verplicht, **geen** check-run op de kop, geen vreemde-App-run | nee | "de deur publiceert deze bij de merge" |
| `permit_revoked` | een run van **de deur-App** met `conclusion: failure` | ja, met hersteltekst | "de deur heeft de toestemming ingetrokken na een mislukte merge; herstel: `python3 scripts/pr_merge.py --pr N`; blijft dit blokkeren, push dan een nieuwe commit" |
| al het andere | — | bestaand gedrag | bestaand gedrag |

`required_contexts_gate` neemt `published_at_merge` op in een eigen veld en telt hem niet als `unverified`; bij `permit_revoked` blijft het verdict `HOLD` (blokkeren is juist) maar krijgt de payload een `recovery`-veld met het commando. `pr_readiness` toont dezelfde regel; `scripts/pre_merge_gate.py` erft dat via `check_required_contexts` (`:51` import, `:1360` aanroep).

**Dit is geen fail-open.** De uitzondering geldt voor een **gesloten verzameling van één naam**, alleen als de context verplicht is, en alleen als er géén check-run is. Staat er een run van de verkeerde App, dan valt de bestaande `wrong_app`-tak eerder in (`scripts/lib/ci_contexts.py:517-533`) en blijft het `UNVERIFIED` en blokkerend. Een onbekende of verkeerd gespelde context krijgt de uitzondering nooit. Een test bindt de constante aan `forge_gate_publisher.CHECK_RUN_NAME_PREFIX`.

**Waarom `permit_revoked` alleen op een `failure` van de deur-App mag afgaan.** Er is precies één producent van die context (§2 en §3), dus een `failure` betekent: de deur heeft geweigerd of kon niet intrekken. Een CI-job kan die naam niet zetten zonder de App-sleutel.

**Het herstelpad is getest, niet beloofd.** D6 levert vier proeven: (a) geen run → `published_at_merge`, niet-blokkerend; (b) een `failure` van de deur-App → `permit_revoked`, blokkerend, met `recovery` gevuld; (c) na een geslaagde verwijdering (runlijst zonder deur-run) → weer `published_at_merge`; (d) verwijdering mislukt en er staat een `failure` → blijft `permit_revoked`, en de hersteltekst zegt dat een nieuwe commit nodig is.

### 7. De merge-queue wordt gelezen, en een ingeschakelde queue is drift

`fetch_live_protection` (`scripts/lib/forge_protection_drift.py:583`) leest `mergeQueue(branch:)` via GraphQL en neemt het genormaliseerde veld mee in `to_normalized_dict` (`:517`) en `compare` (`:891`); een ingeschakelde queue is drift en dus een weigering.

Vandaag is `null` een observatie zonder bewaking. Een queue laat de checks op de merge-group-SHA lopen, niet op de PR-kop, dus de deurcheck zou daar per definitie ontbreken — en de deur zou dat niet merken. "Uitgesloten" zonder meting is dezelfde soort afspraak als de deur zelf was. Dit valt niet onder de twee-PR-regel van §10: dat contract gaat over het **schema van `branch_protection.yaml`**, en dit is een leesveld in de genormaliseerde live-state.

### 8. De dagelijkse controle is de report-only modus van de vergelijking die er al is

`scripts/backfill_pr_merged_receipts.py` krijgt `--report-only`: hetzelfde samenstel (gemergede PR's tegen `pr_merged`-regels, `backfill()` op `:249`), maar het rapporteert in plaats van te helen — exit 1, één open item per ontbrekende PR, een beacon. De lezer wordt fail-closed en leest **beide** ledgerbestanden. `--report-only` bereikt `_emit_backfill_receipt` (`:189`) nooit.

- **Waarom niet een nieuw script.** De vergelijking bestaat al; een tweede script dupliceert haar en heeft geen gedragsrode run. Door de modus aan het bestaande CLI te hangen is de rode run echt: de oude CLI kent `--report-only` niet.
- **Waarom niet in `ledger_health.py`.** Dat is bezet terrein (de dagelijkse telling van `audit-spoor-kern`, de uitbreiding van `audit-spoor-volledig`). Het kanaal waar het gat landt is wel hetzelfde: de health-beacon, per project één `<state_dir>/health/door_audit.json`, zodat `vnx doctor` en het dashboard hem zonder nieuwe registratie zien.
- **Venster: 7 dagen**, `--since` overschrijfbaar. Ouder dan dat is een eenmalige backfill, geen dagelijkse taak.
- **De dekking wordt bewezen, niet afgekapt.** De audit leest `repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page=N` en pagineert **tot** de stopconditie: de oudste `updated_at` op een pagina is `< since`. Daarna filtert hij op `merged_at >= since`. Voor een gemergede PR geldt `updated_at ≥ merged_at` (gemeten: 0 schendingen op 129 rijen), dus zodra een pagina geen `updated_at ≥ since` meer bevat kan geen volgende pagina nog een merge binnen het venster bevatten. De bestaande lijstvorm sorteert op **aanmaakdatum** en is daarmee per definitie onvolledig: 12 van de 129 in-venster-merges staan vóór het venster aangemaakt, de diepste op positie 155.
- **Drie uitkomsten, scherp gescheiden.** Een geslaagde, volledige lezing met 0 merges is `clean` en exit 0 ("0 van 0") — een stil project is meetbaar, niet onmeetbaar. Een **mislukte** lezing (gh ontbreekt, auth, rate-limit, onparseerbare JSON, of de paginagrens van 50 pagina's bereikt vóór de stopconditie) is `unknown` en exit 2, nooit 0. Een lezing met gaten is exit 1.

### 9. De vlootrunner: enumeratie, isolatie, selectie, aggregatie, dedup

D7b draait per project in een **eigen proces**, met een expliciet contract:

- **Enumeratie** = de **vereniging** van `~/.vnx/projects.json` (gemeten: 4) en de geïnstalleerde `com.vnx.ledger-health.*`-labels (gemeten: 5). Een project dat in de ene lijst staat en niet in de andere is een **bevinding** (`unregistered_project` / `job_missing`), nooit een stille skip. Een project zonder lokaliseerbaar pad of repo is `unmeasurable`, nooit `clean`.
- **Isolatie** = één subprocess per project met `cwd=<project_root>` plus expliciete `VNX_PROJECT_ROOT`, `VNX_DATA_DIR`, `VNX_STATE_DIR`. De cwd is geen detail: `open_items_manager.py:33` rekent `STATE_DIR` uit `Path.cwd()`, dus een verkeerde cwd schrijft open items in de verkeerde store. Een crash of onleesbare store van project A laat B en C doorlopen en maakt van A een `unmeasurable`-rij.
- **Selectie** = `--project <id>` (herhaalbaar), standaard "alles wat geënumereerd is". Een onbekend id is exit 2. Een **lege** enumeratie is exit 2, nooit 0.
- **Aggregatie** = maximum van de ernst: 2 als één project onmeetbaar is, anders 1 als één project een gat heeft, anders 0.
- **Dedup** = `dedup_key = f"door-audit:{project_id}:{pr_number}"` (`add_item_programmatic`, `scripts/open_items_manager.py:212-238`), zodat een dagelijkse run geen 30 identieke items opent.

**De verzoening met intrekking is rij D7c, niet D7b.** D7b blijft de vlootrunner hierboven: enumereren, per project isoleren, selecteren, aggregeren, dedup. D7c bouwt daarop en voegt de pass toe die §5 beschrijft — elke open PR, een te oude of onverklaarde groene `vnx-gate/door`, en de verwijdering via hetzelfde publisher-delete-pad als de in-process intrekking. Zonder D7c is de verzoening report-only en is de crash-grens uit §5 niet gehaald.

Bewust **geen** nieuwe per-project launchd-familie: die zou `REQUIRED_PER_PROJECT_FAMILIES` raken (`scripts/launchd/launchd_project_scope.py:99-104`) en `vnx doctor` op elk project laten FAILen tot de sweep gedaan is. Eén machinebrede job met eigen enumeratie geeft hetzelfde bewijs zonder die koppeling.

### 10. De activering is een toestandsmachine A0 tot A3

| Toestand | Wanneer | Wat er waar is | Wat er moet kloppen |
|---|---|---|---|
| **A0** code op main | na D1-D4, D6, D7a/b/c | niets verandert aan de bescherming | niets; de dev-checkout is identiek aan `main` |
| **A1** YAML declareert, live nog niet | tussen de merge van D5 en de apply | `main` zegt 16, live 15 → drift → de deur weigert alles, zonder override | D3, D4 en D6 staan op `main`; de apply volgt direct, één T0-stap, niets ertussen |
| **A2** live eist de check | na de apply | vanaf hier moet de deur publiceren of niemand mergt | de draaiende deur is de hoofdcheckout op `main`, die D3/D4/D6 bevat |
| **A3** release + pins | na A2 op vnx-dev | consumers kunnen hetzelfde | **pas hierna** mag een consumer-project de entry krijgen; daar is de draaiende deur de gepinde installatie, en die moet 1.9.0 zijn (`door_reference`, `scripts/lib/merge_target.py:185-198`) |

De volgorde volgt uit A3: in een consumer-project merget de deur vanuit de pin, niet vanuit de hoofdcheckout. Een consumer met de entry maar zonder 1.9.0 heeft een verplichte check die zijn deur niet kan publiceren — een dicht project.

**De integriteitsregel, apart benoemd.** D2 en D4 raken `_DOOR_INTEGRITY_PATHS` (`scripts/pr_merge.py:1380-1389`). Na zo'n merge vergelijkt elke **dev-checkout** zich met `main`, dus een worktree die achterloopt weigert elke merge tot hij bij is (fetch + reset, of een verse worktree). Installaties vergelijken met hun eigen tag en blijven werken tot de release. Dat is geen risico dat je "kunt accepteren": het is een stap in de volgorde, en de melding noemt het pad en de hash.

### 11. De vier contracten

Deze vier staan hier letterlijk, omdat de rijen D3, D4 en D7a ze anders elk opnieuw zouden interpreteren.

**(a) GO-evidence — wat de publicatie moet meebrengen.**

```
evidence := {
  "schema": 1,
  "source": "pr_merge._run_door",
  "pr_number": <int>,
  "head_sha": "<40 hex>",
  "preflight_gates": [ <de records van PREFLIGHT_ORDER zonder "door", in die volgorde> ],
  "door_version": "<VERSION>",
  "integrity": {"paths": 8, "sha256": "<hash over pad+blob-hash-paren>"}
}
```

Geweigerd (elk met eigen tekst, exit 1) bij: ontbrekend of onparseerbaar bestand, `schema != 1` of onbekend, `source` anders dan `pr_merge._run_door`, `head_sha != --sha`, `pr_number` anders, een **ontbrekende, extra of verkeerd geordende naam** in `preflight_gates` (vergeleken met `PREFLIGHT_ORDER` zonder `door`, in die volgorde — elk van de drie heeft zijn eigen tekst), en **elk** record waarvan `verdict` niet exact `"GO"` is — ontbrekend, `null` en onbekend weigeren alle drie. De bron is `preflight_gates = _preflight_ledger(evaluated)` (`scripts/pr_merge.py:2518`): het evidence-bestand neemt daar de vijf records vóór `door` uit, en de receipt schrijft dezelfde lijst plus het deurrecord dat pas na een geslaagde teruglezing wordt toegevoegd. Eén bron, geen tweede.

**(b) Publicatie-autoriteit — wie mag een toestemming maken.**

Eén schrijver: `pr_merge._run_door` (`scripts/pr_merge.py:2438`). De CLI `forge_gate_publisher.py door` is een **herstel- en testpad**, geen tweede schrijver: hij eist hetzelfde evidence-bestand, weigert zonder, en kan geen `GO` verzinnen. De echte autoriteit is de **App-sleutel in de keychain** (`scripts/lib/forge_check_run.py:93-94` voor de twee keychain-items, `_read_keychain_secret` op `:355`, die luid faalt en nooit leeg teruggeeft). Het evidence-bestand is een tweede slot tegen vergissingen, geen beveiligingsgrens tegen wie de sleutel heeft. Dat staat hier zo, want het omgekeerde beweren zou een schijnzekerheid zijn. D3 reserveert de slug `door` zoals `review` vandaag geweigerd wordt (`check_run_name`, `scripts/lib/forge_gate_publisher.py:180-200`), zodat geen enkele poort ooit over de toestemming heen publiceert.

**(c) De receipt van een geslaagde en van een geweigerde deurmerge.**

`pr_merged` krijgt `door_check`, altijd geschreven door het deurpad — zoals `preflight_gates` altijd geschreven wordt (`scripts/pr_merge.py:1961-1963`):

```
"door_check": {
  "schema": 1,
  "state": "permitted" | "not_required" | "not_evaluated",
  "check_name": "vnx-gate/door", "head_sha": "<40 hex>", "app_id": 4869217,
  "check_run_id": <int|null>, "permit_id": "<app_id:head_sha:check_run_id>",
  "published_at": "<RFC3339|null>", "read_back": {"conclusion": "success", "id": <int>},
  "revoked": true|false, "revoke_outcome": "not_needed|deleted|failed_delete_published_failure|skipped_dry_run",
  "merge_outcome": "merged|not_merged|unknown",
  "door_version": "<VERSION>", "integrity_sha256": "<hash>"
}
```

**Fail-closed leesregel:** een `pr_merged`-regel **zonder** `door_check` leest als `unwitnessed` — een merge van vóór deze track — en **nooit** als "de deur stond het toe". De tegenhanger is `pr_merge_refused` (`_emit_refusal_receipt`, `scripts/pr_merge.py:1974`), dat hetzelfde blok krijgt; daarmee is een intrekking herkenbaar in het grootboek en niet alleen op GitHub.

**(d) Herkomst van een backfill.**

Een `pr_merged`-regel met `source != "pr_merge"` **of** `backfilled: true` telt als **backfilled**, nooit als deurbewijs. D7a rapporteert drie buckets: `door`, `backfilled`, `missing`. Gemeten vandaag: 735 `pr_merge`-regels in `state/t0_receipts.ndjson` en 2 `backfill_pr_merged_receipts`-regels met `backfilled: true` in `events/pr_merged.ndjson` — een lezer moet dus **beide** bestanden lezen. De backfill-writer houdt zijn bestaande merkers (`scripts/backfill_pr_merged_receipts.py:214`, `:221`).

## Consequenties

- **Positief:** een merge buiten de deur wordt vanaf A2 door GitHub geweigerd, niet door een afspraak. De weigering is dezelfde voor de operator, voor een agent en voor een kale `gh pr merge`.
- **Positief:** "niet gedekt" is per project en per receipt meetbaar (`door_check.state == "not_required"`), in plaats van een stilte. De terugweg is één regel in een bestaand bestand en een verzwakking die haar eigen receipt en blocker-open-item schrijft.
- **Negatief, en aanvaard:** dit is **geen** credential-scheiding. Dezelfde App-sleutel kan de toestemming ook namens iets anders zetten. De maatregelen zijn detectie en intrekking (de verzoening met intrekking, rij D7c, en de dagelijkse audit) en vastlegging (het `door_check`-blok), niet preventie. Wie preventie wil, betaalt de prijs uit §2.
- **Negatief:** een crash tussen publicatie en merge laat een toestemming staan tot de verzoening haar **intrekt**; niets op GitHub laat een check-run verlopen. De grens is daarom de job-cadans plus de `permit-TTL` (bij de dagelijkse job hooguit 24 uur plus 30 minuten, zie §5); de verzoening met intrekking (D7c) is het enige net dat dit geval dekt.
- **Negatief:** `permit_revoked` blokkeert een kop tot er een nieuwe commit is wanneer de verwijdering mislukt. Dat is de bewuste fallback: een blokkade met hersteltekst is beter dan een stil gat, en `revoke_outcome` maakt haar herkenbaar.
- **Volgorde:** niets van dit alles is actief voordat D5 de entry zet en de apply draait (A1 → A2). Tot dan staat de code op `main` en verandert er niets aan de bescherming.

## Wat dit ADR niet doet

Het lost de dubbele `pr_number` (PR 1695) niet op, het verhuist de dagelijkse telling niet naar `ledger_health.py`, en het installeert de App niet op consumer-repo's (operator, na parkeerpunt 3). Het verandert niets aan het schema van `branch_protection.yaml` — de entry in §4 is een regel in een bestaande lijst, geen nieuw veld. En het claimt geen uitvoering: zie §1.
