# ADR-077 - Het besluitenregister: één append-only register per context, schema `besluit/1` met een stabiel id per besluit

**Status:** Aangenomen 2026-11-14 (PR #2864, 46bbd0946). Voorgesteld 14-11-2026 (T0, track besluitenregister, B0).
**Datum:** 2026-11-14
**Besloten door:** de operator voor de track (besluit van 14-11-2026: per context één plek waar besluiten staan, los van de voortgang, generiek genoeg voor zakelijk gebruik en niet gebonden aan VNX); de T0 voor de rest, op het plan `claudedocs/specs/besluitenregister/plan.md` (versie 2) en de plan-gate van 14-11-2026 (codex REVISE, tiebreaker START, deepseek-v4-pro, ronde 2) met de verplichte wijziging: een stabiel `besluit_id` per besluit en een veld `betreft` op elke gebeurtenis. Het nummer 077 is het eerste vrije op `origin/main` (ADR-076 is het hoogste).
**Cross-refs:** ADR-005 (append-only NDJSON als waarheid), ADR-007 (composite sleutel voor een centrale tabel), ADR-063 (onbekend is nooit 0), ADR-075 besluit 3 (de gedeelde geheimpatronen), ADR-076 besluit 6 en 9 (een antwoord legt een besluit vast; weigeren, niet maskeren).

Dit ADR legt vast waartegen de leveringen B1 tot en met B5 bouwen: de plek, het schema, de levenscyclus, het gedrag bij een afgescheurde staart, de vertrouwelijkheid, de geheimen, de grens met andere besluitplekken en de terugweg. Het bevat geen code.

**Deterministisch of model.** Alles in dit ADR is deterministisch: het schema en de gesloten waardenlijsten, de ids (uuid4 en sha256), de afgeleide stand (een overgangstabel en een datumvergelijking met een expliciete `nu`), de lezer van de staart, het exportfilter en de geheimweigering (een vaste patronenlijst). Een model komt alleen voor in B5, dat uit vrije tekst in memory-bestanden en daily-logs voorstellen maakt; dat is open taal zonder vaste vorm, en daar is een model nodig. Het vangnet: een voorstel gaat nooit vanzelf het register in, de T0 legt het vast na eigen controle, en de schrijver valideert elke regel alsof hij met de hand komt.

## Context

Besluiten staan nu op vier plekken: `## Beslissingen` in elke daily-log, 80+ memory-bestanden `besluiten-*.md`, `t0_decision_log.jsonl` en de ADR's. Geen ervan heeft een stand per besluit, geen ervan kan een besluit vervangen of intrekken, en in een klantmap bestaat niets. Een besluit van vorige week terugvinden is grep-werk, en een vervangen besluit ziet er even gezaghebbend uit als het nieuwe.

## Besluit

### 1. Plek: naast de daily-log van de context

Elke context (dev-project, klant, content-kanaal, onderzoek, personal) heeft al een daily-log-map, `<LOGDIR>`, die de `daily-log`- en `kickoff`-skills met één tabel oplossen. Het register staat in die map:

| Bestand | Rol |
|---|---|
| `<LOGDIR>/besluiten.ndjson` | de bron, append-only, één regel per gebeurtenis |
| `<LOGDIR>/besluiten.ndjson.lock` | het slotbestand voor de schrijver (`fcntl.flock`, exclusief) |
| `<LOGDIR>/besluiten.ndjson.afgescheurd` | het quarantainebestand voor een afgescheurde staart (besluit 4), append-only |
| `<LOGDIR>/BESLUITEN.md` | leesbare weergave, altijd gegenereerd door `render`, nooit met de hand bewerkt |

Eén bestand hoort bij precies één context. Het pakket krijgt `<LOGDIR>` als argument (`--context`) en lost zelf geen pad op; het kent geen VNX-paden en geen `~/.vnx-data`. Elke regel draagt `context`; de schrijver weigert een regel waarvan `context` afwijkt van de `context` van de eerste geldige regel in het bestand. De map is privé (`daily-log/` staat in `.gitignore`), er is geen centrale tabel en niets gaat naar Supabase. Komt er ooit een centrale tabel, dan geldt ADR-007: een composite sleutel over `context` plus `event_id`.

### 2. Schema `besluit/1`

Eén regel per gebeurtenis, UTF-8, één JSON-object per regel, afgesloten met `\n`. De stand van een besluit wordt afgeleid, nooit overschreven.

**Gemeenschappelijk, op elke regel verplicht:**

| Veld | Inhoud |
|---|---|
| `schema` | exact `besluit/1` |
| `event` | een van `vastgelegd`, `vervangen`, `ingetrokken`, `herzien`, `bevestigd` |
| `betreft` | het `besluit_id` waar de gebeurtenis over gaat; op `vastgelegd` het eigen nieuwe id |
| `event_id` | `E-` plus 64 hex: sha256 over de canonieke regel (zie onder) |
| `context` | slug van de context: `vnx-orchestration`, `klant:renewance`, `content:linkedin` |
| `besloten_op` | wanneer deze gebeurtenis besloten is (niet wanneer ze werd vastgelegd): `JJJJ-MM-DD` of `JJJJ-MM-DDTHH:MM:SSZ`, UTC |
| `vastgelegd_op` | tijd van het schrijven van deze regel, `JJJJ-MM-DDTHH:MM:SSZ`, UTC |
| `vastgelegd_door` | `operator`, `t0`, `skill:<naam>` of `agent:<naam>` |

**Alleen op `vastgelegd`:**

| Veld | Verplicht | Inhoud |
|---|---|---|
| `besluit_id` | ja | `B-<JJJJMMDD>-<8 hex>`, regex `^B-[0-9]{8}-[0-9a-f]{8}$`. De datum is de UTC-datum van `besloten_op`; de hex zijn de eerste 8 tekens van `uuid4().hex`, eenmalig getrokken bij de vastlegging. Nooit afgeleid uit de inhoud, verandert nooit. Bestaat het id al in het bestand, dan trekt de schrijver onder het slot opnieuw. Gelijk aan `betreft` |
| `herkomst` | ja | 64 hex: sha256 over de canonieke JSON-lijst `[context, bron.soort, bron.ref, titel]` |
| `titel` | ja | hooguit 100 tekens, niet leeg |
| `besluit` | ja | het besluit in één tot drie zinnen |
| `waarom` | ja | de reden, niet leeg |
| `alternatieven` | ja | lijst van `{optie, waarom_niet}`, mag leeg zijn |
| `besloten_door` | ja | `operator`, `T0`, `klant`, `team` of een rol |
| `bron` | ja | `{soort, ref, citaat}`; `soort` is een van `chat`, `mail`, `meeting`, `pr`, `oi`, `adr`, `document`, `daily-log`. `citaat` is verplicht en niet leeg als `besloten_door` een mens is en `soort` een van `chat`, `mail`, `meeting`: de eigen woorden van wie besliste |
| `reikwijdte` | ja | `context`, `vloot` of `klant` |
| `vertrouwelijkheid` | ja | `publiek`, `intern` of `klant` (besluit 5) |
| `terugweg` | ja | hoe het besluit terug te draaien is; `geen: <reden>` mag |
| `herzien_op` | nee | `JJJJ-MM-DD`, niet vóór de datum van `besloten_op`, of `null` |
| `vervangt` | nee | het `besluit_id` dat dit besluit vervangt, of `null` |
| `links` | ja | lijst van tekst (track, OI, PR, taak), mag leeg zijn |
| `tags` | ja | lijst, kleine letters, `[a-z0-9-]+`, mag leeg zijn |

**Velden per andere soort gebeurtenis:**

| `event` | Verplicht naast de gemeenschappelijke velden |
|---|---|
| `vervangen` | `door` (het nieuwe `besluit_id`), `reden` |
| `ingetrokken` | `reden`, `bron` |
| `herzien` | `waarom` (wat er bekeken is), `herzien_op` (nieuwe datum of `null`, het veld zelf is verplicht) |
| `bevestigd` | `bron` (met `citaat` volgens dezelfde regel als op `vastgelegd`), `herzien_op` (nieuwe datum of `null`, het veld zelf is verplicht) |

Een onbekend veld, een ontbrekend verplicht veld of een waarde buiten een gesloten lijst maakt de regel ongeldig: de schrijver weigert hem met de veldnaam, de lezer telt hem als `ongeldig` (besluit 4).

**`event_id`.** De canonieke regel is de regel zonder `event_id` en zonder `vastgelegd_op`, geserialiseerd met `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` en als UTF-8 gehasht. Dezelfde gebeurtenis een tweede keer schrijven (een herhaalde poging, een dubbele aanroep) geeft dus hetzelfde `event_id`, en de schrijver schrijft niets. Een `bevestigd` op een andere `besloten_op` of met een ander citaat is een andere gebeurtenis en wordt wel geschreven: herhaalde bevestigingen blijven allemaal zichtbaar.

**`herkomst` en een import.** Een tweede `vastgelegd` met een `herkomst` die al bestaat, geeft geen nieuw besluit. Is `besluit` byte voor byte gelijk aan dat van het bestaande, dan schrijft de schrijver niets en geeft het bestaande `besluit_id` terug, ook als dat besluit inmiddels `vervangen` of `ingetrokken` is (een import haalt niets terug). Verschilt `besluit`, dan weigert de schrijver met exit 3 en de twee `besluit_id`'s in de melding; hij voegt niets stil samen.

### 3. Levenscyclus: vier afgeleide standen en een overgangstabel

De stand van een besluit volgt uit de geldige regels met dat `betreft` en de `vastgelegd`-regels met `vervangt` gelijk aan dat id, in bestandsvolgorde (niet op `besloten_op`), en een expliciet meegegeven `nu` (UTC-datum). Er zijn vier standen: `geldig`, `vervangen` (met `door`), `ingetrokken` (met `reden`) en `te_herzien`.

| Van | Gebeurtenis of tijd | Naar | Regel |
|---|---|---|---|
| (geen) | `vastgelegd` | `geldig` | of direct `te_herzien` als `herzien_op` al bereikt is |
| `geldig` | `nu` ≥ geldende `herzien_op` | `te_herzien` | geen regel, alleen afgeleid |
| `geldig`, `te_herzien` | `herzien` | `geldig` | de geldende `herzien_op` wordt de nieuwe waarde van de regel |
| `geldig`, `te_herzien` | `bevestigd` | `geldig` | idem; de bevestiging telt mee in de geschiedenis |
| `geldig`, `te_herzien` | `vastgelegd` van een nieuw besluit met `vervangt: <dit id>` | `vervangen` (open) | eindstand; `door` is het nieuwe id; de vervanging is open tot de afsluitende regel |
| `vervangen` (open) | `vervangen` met `betreft: <dit id>` en `door` gelijk aan het id van die `vastgelegd` | `vervangen` (afgesloten) | de enige gebeurtenis die na een eindstand mag komen, precies één keer |
| `geldig`, `te_herzien` | `vervangen` zonder eerdere `vastgelegd` met `vervangt: <dit id>` | geweigerd | een vervanging begint altijd met de nieuwe `vastgelegd` |
| `geldig`, `te_herzien` | `ingetrokken` | `ingetrokken` | eindstand |
| `vervangen`, `ingetrokken` | elke andere gebeurtenis: een tweede `vervangen`, een `vervangen` met een andere `door`, een `vastgelegd` met `vervangt: <dit id>`, `herzien`, `bevestigd`, `ingetrokken` | geweigerd | de schrijver weigert met exit 3, `check` meldt een bestaande regel als fout |

**`herzien_op`.** De geldende `herzien_op` is de waarde van de laatste regel die het veld draagt (`vastgelegd`, `herzien` of `bevestigd`); `null` betekent: geen herziening gepland. Een besluit is `te_herzien` vanaf de dag van de geldende `herzien_op` zelf, zolang er geen latere `herzien` of `bevestigd` is. `herzien` is de geplande herziening (er is gekeken, het besluit blijft); `bevestigd` is een bevestiging uit een bron, gepland of niet. Wie de inhoud wil wijzigen, vervangt het besluit.

**Vervangen.** `vervang` schrijft twee regels in één `write` onder het slot: eerst de nieuwe `vastgelegd` met `vervangt: <oud>`, dan de afsluitende `vervangen` met `betreft: <oud>` en `door: <nieuw>`. Het oude besluit staat op `vervangen` (open) zodra de nieuwe `vastgelegd` geldig in het bestand staat, en op `vervangen` (afgesloten) na de afsluitende regel; dat is de enige uitzondering op "geen gebeurtenis na een eindstand". Ontbreekt de afsluitende regel na een crash (de staart is afgescheurd en bij de volgende schrijfactie in quarantaine gezet, besluit 4), dan herstelt dezelfde `vervang`-aanroep het: de `vastgelegd` ontdubbelt op `herkomst` en wordt niet opnieuw geschreven, en alleen de afsluitende `vervangen` wordt toegevoegd. Dezelfde afsluitende regel nog eens schrijven geeft hetzelfde `event_id` en schrijft niets; een tweede `vervangen` met een andere inhoud wordt geweigerd. Een keten van `vervangt` mag geen cirkel bevatten en een besluit kan maar één keer vervangen worden.

**Controle (`check`).** Exit 0 alleen als: elke regel geldig is; elk `besluit_id` op precies één `vastgelegd` staat; elke `betreft`, `vervangt` en `door` naar een bestaand `besluit_id` wijst; er geen cirkel is en na een eindstand geen gebeurtenis staat behalve precies één afsluitende `vervangen` volgens de tabel; elke vervanging afgesloten is (een open vervanging is een fout met de melding dat `vervang` opnieuw moet draaien); en `BESLUITEN.md` byte voor byte gelijk is aan wat `render` zou schrijven.

### 4. Een afgescheurde staart

**De regel.** Een regel is vastgelegd als en alleen als hij eindigt op `\n`: de newline is de commitmarkering. De schrijver schrijft een regel plus `\n` in één `write` met `O_APPEND`, gevolgd door `fsync`, onder het slot, en meldt pas daarna succes. Een laatste stuk zonder `\n` is dus een schrijfactie die nooit succes heeft gemeld, en het telt nooit als gebeurtenis, wat de inhoud ook is, ook als het geldige JSON is.

**De lezer** leest het bestand als bytes en splitst op `\n`. Een laatste stuk zonder `\n` telt hij als `afgescheurd` (met de byte-offset) en hij slaat het over. Een afgesloten regel die geen geldige JSON is of het schema schendt, telt hij als `ongeldig` met het regelnummer: die kan niet uit een afgescheurde schrijfactie komen. De lezer crasht niet en schrijft, herschrijft of kort niets in. Beide tellingen staan in de uitvoer van `list`, `check` en `render`; nul gelezen regels is geen leeg register maar `0 regels`, met de tellingen erbij.

**De schrijver** doet onder het slot, vóór elke nieuwe regel, in deze volgorde:

1. Hij zoekt de laatste `\n`. Volgen daarna nog bytes, dan is dat de afgescheurde staart.
2. Hij voegt aan `besluiten.ndjson.afgescheurd` één regel toe met `offset`, `sha256` en de bytes van de staart (base64), plus `\n`, en doet `fsync`. Staat een regel met dezelfde `offset` en `sha256` er al, dan slaat hij deze stap over. Het quarantainebestand volgt dezelfde lezerregel: een eigen laatste stuk zonder `\n` telt niet als vermelding en wordt vóór de volgende vermelding op dezelfde manier ingekort.
3. Hij kort `besluiten.ndjson` in tot direct na de laatste `\n` (`ftruncate`), en doet `fsync`.
4. Pas dan schrijft hij de nieuwe regel.

Een crash na stap 2 en vóór stap 3 laat de staart in het bestand en in quarantaine staan; de volgende schrijver vindt dezelfde staart, slaat stap 2 over op `offset` plus `sha256` en kort in. Een crash na stap 3 laat een bestand achter dat op `\n` eindigt. In beide gevallen telt de staart nooit als gebeurtenis. De schrijver wijzigt of verwijdert nooit een byte van een vastgelegde regel; alleen een nooit gemelde staart verhuist, en die blijft in quarantaine bewaard.

**Melding.** `check` toont een `afgescheurd` stuk in het bestand en elke regel in `besluiten.ndjson.afgescheurd` als waarschuwing (exit 0 mits de rest klopt) en `ongeldig` als fout (exit 1). `export` weigert bij een `ongeldig` met exit 1, omdat die regel een intrekking kan zijn; een `afgescheurd` stuk blokkeert de export niet, omdat het een gebeurtenis is die nooit plaatsvond, en de export noemt het aantal.

### 5. Vertrouwelijkheid en export

`vertrouwelijkheid` staat op `vastgelegd` en geldt voor elke latere gebeurtenis van dat besluit, citaten inbegrepen. `klant` is alleen toegestaan in een context waarvan de slug met `klant:` begint, en `reikwijdte: klant` ook; de klant is die slug. De schrijver weigert elke andere combinatie.

`export` vraagt een verplicht publiek `--voor`; zonder `--voor` of met een andere waarde dan hieronder weigert hij met exit 2 en schrijft hij niets.

| `--voor` | Toont | Toont nooit |
|---|---|---|
| `intern` | `publiek`, `intern`, `klant` | - |
| `publiek` | `publiek` | `intern`, `klant` |
| `klant:<slug>` | `publiek`; `klant` alleen als `context` van het register gelijk is aan `klant:<slug>` | `intern`; `klant` uit elke andere context |

`render` schrijft `BESLUITEN.md` in de privémap en gebruikt altijd `intern`. Een export toont van een vervangen besluit het id van de opvolger alleen als die opvolger voor hetzelfde publiek zichtbaar is, anders alleen de stand `vervangen`.

**Testbare regel (B2, mergeplicht):** voor elk publiek A en elk besluit L in `export(A)` geldt `zichtbaar(A, L.vertrouwelijkheid, register.context)` volgens de tabel. De fixture zet in elk `intern`-besluit en in een `klant`-besluit van `klant:a` een unieke tekenreeks; een grep op die reeksen in `export --voor klant:b` en in `export --voor publiek` geeft nul treffers, en in `export --voor klant:a` alleen de reeks van `klant:a`.

### 6. Geheimen: weigeren met een gecontroleerde kopie van de patronen

Elk tekstveld van een regel, ook genest (`bron.citaat`, `alternatieven`, `links`, `tags`), gaat vóór het schrijven door alle patronen. Een treffer weigert de hele regel met exit 3 en de naam van het patroon, nooit de gevonden waarde; er wordt niets geschreven. Maskeren mag niet: een besluit met een gemaskeerde waarde is geen besluit meer (ADR-076 besluit 9).

De lijst is die van ADR-075 besluit 3, in `scripts/lib/secret_patterns.py` (`PATTERNS`, tien benoemde patronen). Het pakket importeert niet uit `scripts/`, dus het draagt een eigen kopie in `besluitenregister.geheimpatronen` met dezelfde namen, in dezelfde volgorde. Een bewuste kopie is hier de uitzondering op "één lijst in de repo" uit ADR-075, met deze bewaking:

- **De test** `tests/test_besluitenregister_patronen_gelijk.py` vergelijkt per positie `(name, regex.pattern, regex.flags)` van beide lijsten. Ontbreekt `scripts/lib/secret_patterns.py`, dan faalt de test; hij slaat niet over.
- **Wie bijwerkt:** de PR die `scripts/lib/secret_patterns.py` wijzigt, wijzigt de kopie in dezelfde PR; de gelijkheidstest wordt anders rood. Buiten deze repo is de kopie in de geïnstalleerde versie van het pakket de lijst; een nieuw patroon komt daar alleen met een nieuwe pakketversie uit deze repo, die de track besluitenregister uitbrengt.

### 7. De grens met andere besluitplekken

| Plek | Wat er blijft | Verhouding tot het register |
|---|---|---|
| ADR | elk architectuurbesluit, met context, gevolgen en alternatieven | een ADR blijft verplicht; het register legt het besluit vast met `bron.soort: adr` en `bron.ref: ADR-0NN` |
| memory-bestand | de werkregel ("Why", "How to apply") voor een agent | het operatorbesluit zelf gaat in het register met het citaat; de memory verwijst naar het `besluit_id` |
| `t0_decision_log.jsonl` | accept en reject van dispatches | blijft apart; een dispatch-uitkomst is geen besluit voor het register |
| daily-log `## Beslissingen` | wat er in de sessie besloten is, tussen de voortgang | de `daily-log`-skill legt elk punt ook vast (B3), ontdubbeld op `herkomst` |

Het register leidt geen besluit af uit tekst: elke `vastgelegd` komt van een mens, de T0 of een skill die een expliciet besluitpunt overneemt, met een bron.

### 8. Terugweg

Stoppen met het register is de skills en de VNX-adapter niet meer laten schrijven (de tekst uit de `daily-log`- en `kickoff`-skill halen, het blok `besluiten` uit `t0_index.json` halen). De bestanden `besluiten.ndjson` blijven staan: ze zijn append-only, platte NDJSON, leesbaar met elke JSON-lezer en met `python3 -m besluitenregister list` van elke uitgebrachte versie. `BESLUITEN.md` blijft als laatste weergave staan. Geen andere bron wordt gewijzigd, dus er is geen datamigratie terug.

## Gevolgen

- Per context staat elk besluit op één plek, met een stand; een vervangen of ingetrokken besluit ziet er ook zo uit.
- Een tweede bevestiging, een herhaalde poging en een dubbele import zijn drie verschillende gevallen met een eigen, testbaar gedrag.
- Een klantexport kan geen intern besluit en geen besluit van een andere klant tonen; dat is een test, geen afspraak.

| Levering | Testplicht uit dit ADR |
|---|---|
| B1 | twee bevestigingen op verschillende data geven twee regels; dezelfde bevestiging twee keer geeft één; dubbele import geeft één besluit; import met afwijkende tekst geeft exit 3; afgescheurde staart (lezen, tellen, ook als geldige JSON zonder `\n`; in quarantaine zetten en inkorten vóór de volgende regel; een crash tussen quarantaine en inkorten); een vervanging met een afgescheurde afsluitende regel en het herstel door `vervang`; elke overgang uit de tabel, ook de geweigerde; `te_herzien` op en na de dag van `herzien_op`; weigering per geheimpatroon zonder de waarde in de melding; gelijkheidstest van de patronen |
| B2 | `check` per foutsoort van besluit 3; de vertrouwelijkheidsregel van besluit 5; `export` zonder `--voor` geeft exit 2; `export` met een `ongeldig` regel geeft exit 1 |

## Alternatieven

**A. Het id afleiden uit de inhoud** (`sha256(context|besloten_op|titel)[:6]`, plan versie 1). Afgewezen door de plan-gate: dan zijn een tweede bevestiging en een herhaalde poging niet te onderscheiden, en een titelcorrectie geeft een ander besluit.

**B. Eén regel per besluit, bijgewerkt bij elke wijziging.** Afgewezen: dat overschrijft de geschiedenis en is geen audit. Een gebeurtenis per regel met een afgeleide stand volgt ADR-005.

**C. Een centrale tabel of de VNX-store.** Afgewezen: een klantmap of contentkanaal heeft geen VNX-store, en besluiten van een klant horen de machine en de map niet te verlaten.

**D. Importeren uit `secret_patterns.py`.** Afgewezen: dan is het pakket niet los te installeren. Een kopie met een gelijkheidstest houdt beide gelijk zolang ze in één repo staan.
