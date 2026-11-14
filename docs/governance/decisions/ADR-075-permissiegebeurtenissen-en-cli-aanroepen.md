# ADR-075 - Permissiegebeurtenissen en CLI-aanroepen: privacy, maskering, bewaring en levenscyclus van twee nieuwe ledgers

**Status:** Voorgesteld 14-11-2026 (T0, track permissieblokkades-leren, L0); aangenomen zodra de T0 L0 merget.
**Datum:** 2026-11-14
**Besloten door:** de operator voor de track (besluit van 14-11-2026: elk moment waarop Vincent zelf een commando uitvoert omdat een agent het niet mag, wordt gemeten en komt in het learning-stelsel); de T0 voor de rest, op het plan `claudedocs/specs/permissieblokkades-leren/plan.md` (versie 1) en de plan-gate van 14-11-2026 (tiebreaker START, deepseek-v4-pro, ronde 2) met de verplichte wijziging: een bindend besluit over privacy, maskering, bewaring en levenscyclus van `permission_events.ndjson` en `cli_invocations.ndjson` vóór L1 en L4 shippen. Het nummer 075 is door de T0 toegewezen; ADR-074 is het hoogste nummer op `origin/main`.
**Cross-refs:** ADR-007 (composite sleutel over `project_id`), ADR-056 (`actor_class` en wie mag promoveren), ADR-063 (onbekend is nooit 0), ADR-065 (leren is voorstellen), ADR-072 (operatorbijsturing; besluit 2 voor de binding, besluiten 11 en 12 voor privacy als testplicht).

Dit ADR is geen amendement op ADR-072. ADR-072 gaat over operatorprompts en hun tekst. Deze track voegt twee nieuwe ledgers toe met eigen bronnen: geweigerde en goedgekeurde tool-aanroepen uit transcripts, kopieerblokken van agents, en aanroepen van de vnx-CLI. Die krijgen hun eigen besluit, zodat de leveringen L1 tot en met L7 tegen één tekst bouwen en elk besluit hieronder door een test te controleren is.

**Deterministisch of model.** Alles in dit ADR is deterministisch: het schema, de maskering (een vaste patronenlijst), de normalisatie (vaste vervangingsregels), de bewaring (een datumgrens op `ts`), de dekking en de classificatie van harde stops (een vaste lijst vormen). Er komt nergens een model aan te pas. Een model zou hier alleen onzekerheid toevoegen op precies de plekken waar een fout een lek of een verkeerde allow-regel betekent; een vaste lijst die een patroon mist, is met een test te vinden en met een PR te repareren.

## Context

Gemeten in het vooronderzoek `dlv-permissieblokkades-oppervlak` over 7 dagen tot 14-11-2026 08:40Z, 735 transcripts van vnx-orchestration:

- 317 geweigerde tool-aanroepen (B1), waarvan 305 Bash en 279 van headless bouwers. 183 daarvan hebben één oorzaak (`rule:cliArg`, een PATH- of env-prefix).
- 70 permissievragen die Vincent beantwoordde (B2): 62 tijdelijk, 7 permanent, 1 weigering. 95 Bash-gevallen met `user-rejected` hebben `source: config` en zijn dus geen klik van Vincent.
- 28 T0-berichten met een kopieerblok waarin een agent Vincent vraagt iets te draaien (B4a). Wat Vincent daarna in een gewone terminal draait (B4b), laat geen spoor na; daarvoor komt een nieuwe CLI-ledger.

De ruwe bron draagt meer dan nodig is: volledige commando's met paden, tokens in env-prefixen, `Authorization`-headers, de `accountUuid` van de `session-owner`-regel en via `~/.claude.json` het e-mailadres. Een ledger die die regels ongefilterd overneemt, zet geheimen en persoonsgegevens op een nieuwe plek die elke dag groeit. De plan-gate eiste daarom vooraf een bindend besluit, zoals ADR-072 besluit 11 en 12 privacy tot een testplicht maakten.

## Besluit

### 1. Bereik en plek: drie bestanden per project, lokaal, append-only

De track schrijft precies drie bestanden, alle in de store van het project:

| Bestand | Vorm |
|---|---|
| `~/.vnx-data/<project_id>/state/permission_events.ndjson` | append-only ledger, één regel per gebeurtenis, schema `permission_event/1` |
| `~/.vnx-data/<project_id>/state/cli_invocations.ndjson` | append-only ledger, één regel per vnx-CLI-aanroep, schema `cli_invocation/1` |
| `~/.vnx-data/<project_id>/state/permission_coverage.json` | projectie: de laatste dekkingsregel per bron en per bewaarronde, schema `permission_coverage/1` |

Het pad wordt opgelost via `scripts/lib/project_root.py` en de store van het project, nooit hard gecodeerd. Elke regel draagt `project_id`. Append-only betekent: een schrijver voegt alleen regels toe met `O_APPEND` en fsync en wijzigt of verwijdert nooit een bestaande regel; de enige uitzondering is de bewaarronde van besluit 6, die hele regels ongewijzigd verplaatst. Niets van deze bestanden gaat naar Supabase of een andere dienst buiten de machine. Komt er ooit een centrale tabel, dan is dat een nieuw besluit en geldt ADR-007: een composite UNIQUE- of primaire sleutel over `project_id` plus `event_id` (of `invocation_id`).

**Test (L1):** de schrijver weigert een regel zonder `project_id` of met een `project_id` die niet gelijk is aan dat van de store, en een bestaande regel is na een tweede schrijfronde byte voor byte gelijk.

### 2. Wat nooit wordt opgeslagen

In geen van de drie bestanden, en niet in een rapport, een gestagede spec of een PR-tekst die uit deze bestanden is afgeleid, komen:

- prompttekst van de operator of een agent;
- uitvoer van een tool (`tool_result`-inhoud, stdout, stderr);
- argumentwaarden van een CLI-aanroep;
- de `accountUuid`, een e-mailadres of een andere waarde uit het `oauthAccount`-blok;
- ruwe commandotekst vóór de normalisatie van besluit 4;
- het pad van het transcriptbestand en het pad van de worktree (die bepalen `origin`, maar worden niet bewaard).

`account` is uitsluitend een van de drie slots `vnx`, `sm` of `unknown`. Het slot volgt uit het vergelijken van de `accountUuid` van de `session-owner`-regel met het `oauthAccount`-blok van `~/.claude.json` en `~/.claude-salesminds/.claude.json`; die vergelijking gebeurt in geheugen en de uuid verlaat de functie niet. Geen treffer, een onleesbaar bestand of een ontbrekende regel geeft `unknown`. `reden` bevat de `reasonCode` plus hooguit 120 tekens van de weigeringstekst, en die 120 tekens gaan eerst door besluit 3 en 4.

**Test (L1, L2):** een fixture met een bekende uuid, een e-mailadres, een prompttekst en een tool-uitvoer van 40 unieke tekens; na een scan geeft een grep op elk van die vier waarden over de drie bestanden nul treffers, en `account` heeft geen andere waarde dan `vnx`, `sm` of `unknown`.

### 3. Maskeren vóór het schrijven, met één gedeelde patronenlijst

Elk tekstveld (`vorm`, `reden`) gaat door de maskering voordat een regel naar schijf gaat. Een treffer wordt `<secret>`. De patronen:

1. `sk-` gevolgd door een sleutel (OpenAI, Anthropic en vergelijkbaar);
2. `gh[pousr]_` gevolgd door een token (GitHub);
3. een AWS-sleutel-id (`AKIA` of `ASIA` gevolgd door 16 hoofdletters of cijfers);
4. een Slack-token (`xox[abprs]-`);
5. `Bearer` gevolgd door een token;
6. een toekenning `<naam>=<waarde>` waarin de naam `KEY`, `TOKEN`, `SECRET` of `PASSWORD` bevat (hoofdletterongevoelig): de waarde wordt gemaskeerd, de naam blijft;
7. `Authorization:` met alles erna tot het einde van de header;
8. een PEM-blok (`-----BEGIN ... -----` tot en met `-----END ... -----`);
9. een JWT (drie base64url-delen gescheiden door punten, het eerste begint met `eyJ`);
10. het generieke patroon van 40 of meer tekens uit `[A-Za-z0-9_+/=-]`, **alleen** in een `=`-positie of als waarde van een header. Elders maskeert het niet, anders verdwijnen sha's en ids, die besluit 4 al als plaatshouder behandelt.

De lijst staat in één module: `scripts/lib/secret_patterns.py`. Die module wordt ook gebruikt door de track `operator-vragen-tot-antwoord`; er is één lijst in de repo, en wie een patroon toevoegt, voegt het daar toe met een test. Welke van de twee tracks de module als eerste merget, maakt hem aan; de ander importeert hem en kopieert geen patroon. Een tweede patronenlijst in een van beide tracks is een reviewbevinding.

Maskeren is fail-closed: lukt het maskeren van een veld niet (een uitzondering, een niet-tekstwaarde), dan wordt het veld `<unmaskable>` en de regel wordt toch geschreven zonder de oorspronkelijke waarde; de ruwe waarde gaat nooit als terugval naar schijf.

**Test (L1, mergeplicht):** per patroon één fixture met een realistische maar verzonnen waarde, plus voor patroon 10 één geval in `=`-positie (gemaskeerd) en één sha van 40 tekens als los argument (niet gemaskeerd door dit patroon, wel een plaatshouder door besluit 4). Na het schrijven geeft een grep op elke verzonnen waarde in de ledger nul treffers. L1 merget niet zonder deze tests.

### 4. Normalisatie hoort bij de privacy

`vorm` is het genormaliseerde commando, nooit het ruwe. De regels, in deze volgorde, na de maskering:

1. een leidende `cd <pad> &&` en leidende env-toekenningen (`NAAM=waarde`) gaan eraf;
2. ids worden plaatshouders: `dlv-...` wordt `<dlv>`, een dispatch-id wordt `<dispatch>`, een PR-nummer wordt `<pr>`, een sha van 7 tot 40 hex wordt `<sha>`;
3. paden onder `/private/tmp` en `/tmp`, onder de scratchpad van de sessie en onder `~/.vnx-data` worden `<tmp>`, `<scratchpad>` en `<vnx-data>`; een absoluut pad onder de home-map van de gebruiker wordt `~/...` en een naam van een klantmap komt er nooit in (besluit 5);
4. losse getallen worden `<n>`;
5. een heredoc wordt `<cmd> <<heredoc`, zonder de inhoud.

Voorbeeld: `cd /Users/x/repo && VNX_X=1 bin/vnx deliverable promote dlv-4856e810fb14` wordt `bin/vnx deliverable promote <dlv>`.

Normaliseren is deterministisch en idempotent: de vorm van een vorm is dezelfde vorm. `event_id = sha256(project_id | bron | sessie | ts | vorm)[:16]` rekent met de genormaliseerde vorm, zodat dezelfde gebeurtenis twee keer gelezen één regel geeft.

**Test (L1):** de 10 vormen uit het vooronderzoek gaan naar hun verwachte vorm; normaliseren van de uitkomst geeft dezelfde uitkomst; dezelfde gebeurtenis twee keer gescand geeft één regel.

### 5. Transcripts buiten het bereik

De lezer van L2 en L3 leest alleen gebonden transcriptmappen, met de binding en de containment per bestand van ADR-072 besluit 2. Er is geen glob over `~/.claude/projects/`. Een slug die `clients`, `business` of `salesminds` bevat (kleine letters vergeleken) weigert de binding, zoals ADR-072 al doet. De track leest nooit:

- een klantmap of een SalesMinds-map (ook niet via de symlink `~/.claude-salesminds/projects`);
- `~/.zsh_history` of een andere shellgeschiedenis;
- een bestand buiten de gebonden mappen, ook niet via een symlink.

Een commando dat Vincent buiten de vnx-CLI in een gewone terminal draait, blijft onzichtbaar en heet in de dekking `unknown`, niet 0.

**Test (L2):** een binding met elk van de drie verboden slugs geeft exit 2; een transcript dat via een symlink buiten de gebonden map wijst, maakt de bron `error` en levert geen enkele regel.

### 6. Bewaring: 90 dagen live, daarna 365 dagen in een archief, dan weg

Voor `permission_events.ndjson` en `cli_invocations.ndjson` geldt dezelfde termijn:

- **Live:** regels waarvan `ts` hooguit 90 dagen oud is, blijven in het live bestand.
- **Archief:** een regel ouder dan 90 dagen verplaatst ongewijzigd naar `state/archive/permission_events/<ledger>-<JJJJ-MM>.ndjson` in dezelfde store, gegroepeerd op de maand van zijn `ts`.
- **Wissen:** een archiefbestand wordt verwijderd zodra de laatste dag van zijn maand meer dan 365 dagen achter ligt. Daarna is de gebeurtenis weg.

`permission_coverage.json` is een projectie en heeft geen eigen bewaring: elke lezing en elke bewaarronde overschrijft hun eigen regel.

**Waarom deze termijn.** De effectmeting (plan § Effectmeting, L7) telt een vorm in de 7 dagen vóór en de 7 dagen ná `decided_at` en geeft `pending` tot er 7 dagen na het besluit zijn. Een voorstel kan weken open staan voordat Vincent beslist. 90 dagen live laat een voorstel tot 76 dagen na de vroegste gebeurtenis in het voorvenster beslist worden, met de hele meting nog in het live bestand. Het archief van 365 dagen dekt een besluit dat later valt en een jaarvergelijking, zonder dat het live bestand ongebonden groeit: bij de gemeten 317 plus 70 plus 28 gebeurtenissen per week zijn 90 dagen ruim 5.000 regels.

**Hoe.** Het commando is `python3 scripts/permission_events_scan.py prune --project-id <id>`. Het neemt het slot `state/permission_events.lock`, dat ook elke schrijver neemt; schrijft eerst de archiefregels met fsync, schrijft dan het nieuwe live bestand naar een tijdelijk bestand en zet het met een atomaire rename op zijn plaats. Een regel zonder leesbare `ts` wordt nooit verplaatst of gewist: hij blijft live en telt als `unparseable_ts`. Elke ronde schrijft een dekkingsregel `prune` in `permission_coverage.json` met het aantal verplaatste regels per ledger, de verwijderde archiefbestanden, het aantal `unparseable_ts` en de oudste live `ts`. Een ronde die faalt, laat beide bestanden ongewijzigd en schrijft `status: "error"` met de reden.

**Lezen over de grens.** L7 leest het live bestand plus de archiefbestanden. Valt een venster van de effectmeting geheel of gedeeltelijk na het wissen, dan is de uitkomst `unknown` met reden `retention_expired`, nooit een daling en nooit 0.

**Test (de levering die `prune` toevoegt, zie Gevolgen):** een fixture met regels van 10, 89, 91 en 400 dagen oud en één regel zonder `ts`; na `prune` staan 10, 89 en de regel zonder `ts` live, 91 in het archief, 400 nergens, en de dekkingsregel noemt die aantallen. Een tweede `prune` direct erna verplaatst niets.

### 7. Levenscyclus: wie schrijft, wie leest, wat bij ontbinden

| Bestand | Schrijver | Lezer |
|---|---|---|
| `permission_events.ndjson` | L2 (`permission_events_scan.py`, bronnen B1, B2, B4a) en L3 (B3), via de schrijver van L1; L4 voegt de `executed`-paren toe | L5 (groeperen per vorm, drempel, doelgroep, harde stops), L7 (effectmeting) |
| `cli_invocations.ndjson` | L4 (de haak in `bin/vnx` en het vnx-CLI-entrypoint) | L4 (koppeling `asked` naar `executed` binnen 30 minuten), L5, L7 |
| `permission_coverage.json` | elke lezing van L2, L3 en L4, en `prune` | L5, L7 en `build_t0_state.py` (L6), die de dekking naast `permission_proposals` in `t0_index.json` zet |

`build_t0_state.py` leest geen ruwe gebeurtenisregels: het leest de voorstellen uit `steering_proposal_store` en de dekking uit `permission_coverage.json`. Geen lezer schrijft terug in een ledger.

De haak van L4 verandert nooit de exitcode, de uitvoer of de duur van een vnx-commando merkbaar: faalt het schrijven, dan draait het commando gewoon door en blijft de regel weg. Een ontbrekende `executed`-regel betekent daarom "geen paar gevonden" (`unpaired`), nooit "Vincent deed het niet".

**Ontbinden of verwijderen.** Wordt een project ontbonden (`steering_sources.json` weg of een bron `unbound`), dan schrijft de scan niets meer voor dat project en leest geen enkele lezer de bestanden nog; ze blijven in de store van het project staan en de bewaring van besluit 6 blijft gelden zodra `prune` weer draait. Wordt een project verwijderd, dan gaan de bestanden mee met de store. Niets wordt ooit van de ene projectstore naar de andere gekopieerd, en een lezer van project A opent nooit een bestand onder de store van project B.

**Test (L2, L5):** met een ontbonden bron schrijft de scan nul regels en zet de bron op `unbound` in de dekking; een lezer met `project_id` A die een pad onder de store van B krijgt, weigert met een fout.

### 8. Dekking en `unknown`

Elke lezing schrijft per bron (`B1`, `B2`, `B3`, `B4a`, `B4b`) een dekkingsregel met de gelezen bestanden (als aantal), de onleesbare bestanden (aantal en reden), het venster en de staat `ok`, `absent`, `unbound`, `error` of `unknown`. Een bron die niet te lezen is, geeft `unknown` met een reden, nooit 0 (ADR-063 besluit 2). B4b is `unknown` zolang `cli_invocations.ndjson` ontbreekt of onleesbaar is. Een telling van 0 is alleen geldig met een bron in staat `ok` en minstens één gelezen bestand. L5 maakt geen voorstel en L7 geen oordeel op een bron die niet `ok` is.

**Test (L2):** een onleesbaar transcriptbestand geeft voor die bron `unknown` met reden; een rapport met `value: 0` naast een `unknown`-bron faalt de controle.

### 9. Harde stops worden nooit een allow-regel

De classificatie van plan § "Van gebeurtenis naar voorstel" is een vaste lijst in code, deterministisch en zonder model. Elke vorm die op operatorwerk of een harde stop uit `~/.claude/rules/operator-protocol.md` lijkt, krijgt de soort `alleen_melding` met de reden, nooit `permissie`, `bouwer_allowlist` of `bouwgewoonte`:

- verwijderen: `rm -rf` buiten de scratchpad, `git push --delete`, `gh ... delete`;
- secrets: elke vorm waarin de maskering van besluit 3 een `<secret>` zette, en commando's die sleutels lezen of schrijven (`security find-generic-password`, `gh secret`, `op`);
- Supabase-migraties en -rechten;
- `gcloud`, `hcloud` en `ssh` naar een server;
- `launchctl` en `systemctl`;
- deploys en herstarts.

`vnx deliverable promote` valt hier ook onder. ADR-056 houdt het promoveren bij de operator. Het voorstel is daarom een melding die de route van ADR-056 noemt: `metadata.autonomous_promotion_eligible` op de track plus een plan-gate-grant die de verifier als `VERIFIED` leest. Dat is de enige weg waarlangs een T0 dit werk legaal overneemt. Geen codepad in deze track past een permissieregel toe of schrijft in `settings.json`; leren blijft voorstellen (ADR-065).

Een vorm die de classificatie niet kent, valt door naar de gewone doelgroepregel; een vorm die ze wel herkent, kan niet door een later besluit, een hogere telling of een heropening alsnog `permissie` worden.

**Test (L5):** per categorie één vorm, en `bin/vnx deliverable promote <dlv>`, gaan naar `alleen_melding` met een reden die de categorie noemt; de melding voor promote noemt `autonomous_promotion_eligible` en `VERIFIED`; geen van die vormen geeft ooit een voorstel van soort `permissie`, ook niet na 10 nieuwe gebeurtenissen.

### 10. Geen argumentwaarden in de CLI-ledger

Een regel in `cli_invocations.ndjson` bevat `ts`, `invocation_id`, `project_id`, de subcommandovorm, `actor_class` (afgeleid door `scripts/lib/actor_class.py`, nooit een literal), de exitcode en de duur. De subcommandovorm is het pad van bekende subcommandonamen uit de parser (bijvoorbeeld `vnx deliverable promote`), plus de namen van gebruikte vlaggen gesorteerd en zonder waarde. Positionele argumenten worden plaatshouders volgens besluit 4; een positioneel argument dat geen bekende plaatshouder is, wordt `<arg>`. Een vlagwaarde komt er nooit in, ook niet gemaskeerd.

**Test (L4):** een aanroep met een unieke vlagwaarde en een positioneel argument dat op een token lijkt; een grep op beide waarden in `cli_invocations.ndjson` geeft nul treffers, en de vorm is `vnx <sub> <arg> --<vlag>`.

## Gevolgen

- De privacyregels zijn testplichten per levering, geen afspraak. De verdeling:
  - **L1:** maskering per patroon (besluit 3, mergeplicht), normalisatie en idempotente `event_id` (besluit 4), schrijver weigert een vreemde `project_id` en wijzigt geen bestaande regel (besluit 1), de nooit-opslaan-grep (besluit 2).
  - **L2:** `account` alleen als slot en geen uuid of e-mailadres in de uitvoer, `origin` zonder pad (besluit 2); verboden slugs en containment (besluit 5); ontbinden en tenantgrens (besluit 7); `unknown` voor een onleesbare bron (besluit 8).
  - **L3:** B3 via dezelfde schrijver, dus dezelfde maskering en normalisatie; een `<bash-input>`-regel met een token geeft `<secret>` in de ledger.
  - **L4:** geen argumentwaarden (besluit 10); de haak verandert de exitcode niet; B4b `unknown` zonder ledger.
  - **L5:** classificatie van harde stops en de promote-melding (besluit 9).
  - **L7:** `retention_expired` geeft `unknown` (besluit 6). L7 voegt ook `prune` toe met zijn test, omdat L7 de lezer is die de termijn nodig heeft; past dat niet binnen 300 regels, dan splitst L7 in `L7a` (effectmeting) en `L7b` (`prune`), en L7b merget vóór de eerste `prune` op een live store.
- `scripts/lib/secret_patterns.py` wordt een gedeelde module van twee tracks. Een wijziging aan de lijst raakt beide; de test per patroon staat bij de module.
- De ledgers groeien gecontroleerd: hooguit 90 dagen live, daarna een jaar archief. Een effectmeting op een besluit van meer dan een jaar na de gebeurtenissen is `unknown`. Dat is aanvaard.
- B4b dekt alleen de vnx-CLI. Wat Vincent elders draait, blijft `unknown`; de track belooft geen volledig beeld van operatorwerk.
- Een tweede patronenlijst, een glob over `~/.claude/projects/`, een `permissie`-voorstel voor een harde stop of een ongemaskeerde sleutel in een ledger is een stopregel van het plan (E3, PR terug), geen randgeval.

## Alternatieven

- **ADR-072 amenderen.** Afgewezen: ADR-072 gaat over operatorprompts en hun tekst, met eigen bestanden en eigenaars (OB-0 tot en met OB-7). Twee nieuwe ledgers met andere bronnen in dat ADR zetten, maakt beide moeilijker te toetsen. Dit ADR leent de binding (ADR-072 besluit 2) en de vorm van privacy als testplicht (besluit 11 en 12) en noemt dat met naam.
- **Ruwe commando's bewaren en bij het lezen maskeren.** Afgewezen: dan staat het geheim al op schijf en hangt de privacy aan elke lezer. Maskeren en normaliseren vóór het schrijven houdt het op één plek.
- **Een model laten beslissen of een waarde een geheim is, of een vorm een harde stop.** Afgewezen: een vaste lijst haalt de gemeten patronen, is met een test per patroon te bewijzen en is reproduceerbaar. Een model zou een gemiste sleutel niet zichtbaar maken en een allow-regel kunnen voorstellen voor een vorm die nooit een allow-regel mag worden.
- **Geen bewaring, alles blijft.** Afgewezen: de ledgers groeien dan ongebonden met commandovormen die over een jaar niemand meer leest.
- **Een korte termijn van 30 dagen.** Afgewezen: een voorstel dat drie weken open staat, verliest dan het voorvenster van zijn effectmeting.
- **De shellgeschiedenis lezen voor B4b.** Afgewezen: te breed en er staat privéwerk in. De CLI-ledger dekt de vnx-CLI, de rest is `unknown`.
- **Een centrale tabel in Supabase.** Afgewezen voor nu: niets in deze track heeft gegevens over projecten heen nodig, en lokale bestanden houden de gegevens op de machine. Een latere tabel is een nieuw besluit onder ADR-007.
