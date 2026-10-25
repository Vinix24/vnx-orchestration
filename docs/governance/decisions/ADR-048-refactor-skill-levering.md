# ADR-048 - Een skill is geen rol: één `skill`-veld en één naad

**Status:** Proposed (dispatch dlv-bb5dcfc182bd, track refactor-skill-en-levering)
**Datum:** 2026-10-25
**Besloten door:** T0 in plan-revisie 2 van de track, na de plan-gate van 24-10. Dit ADR legt de zes besluiten vast en opent ze niet opnieuw. Alle metingen staan op main `7762a44c`.
**Cross-refs:** ADR-022 (provider-agnostische skill-injectie — dit ADR haakt daar op in en wijzigt één punt); ADR-024 (de deur); ADR-035 (receipt v2); ADR-042 (het harness-contract); ADR-044 (de provider-schakelaar); ADR-046 en ADR-047 (de vorm van dit besluit).

## Context

Twee rollen doen het overwegend gedragsbehoudende refactorwerk: `backend-developer` en `quality-engineer`. Er is geen skill die dat werk als methode beschrijft, en er is geen weg om een skill aan één dispatch mee te geven. De spagaat is niet "de skill ontbreekt" maar "de bedrading ontbreekt", en dat is meetbaar op vier punten.

**Het veld bestaat en is een dode draad.** `DispatchSpec` declareert `skill` (`scripts/lib/dispatch_spec.py:242`), maar `stage_spec_bundle` schrijft geen `skill`-sleutel in `dispatch-spec.json` (`git grep -n skill scripts/lib/dispatch_bridge.py` geeft nul treffers). Een aanroeper kan het veld dus niet zetten via de brug die elk bundle schrijft, en niets stroomafwaarts leest het. Een gedeclareerd veld zonder schrijver is geen interface.

**De injector die de fabric gebruikt, leest rollen en geen skills.** `skill_context._inject_skill_context` (`scripts/lib/skill_context.py:139`) is de naad die elke lane al passeert, en wat hij oplost is een rolbon: `agents/<role>/CLAUDE.md`, dan `.claude/skills/<role>/CLAUDE.md`, dan de terminal (`scripts/lib/skill_context.py:343`; dezelfde volgorde in `scripts/lib/role_application.py:154`). Een skillmap bevat `SKILL.md`, geen `CLAUDE.md`, dus die tweede tier matcht nooit: 0 van de 38.756 receipts in het grootboek van dit project draagt `role_tier: "skills"` (`grep -c '"role_tier": *"skills"'` op `t0_receipts.ndjson`). Hem leren om `SKILL.md` te lezen zou een skill tot rolbon maken, en dat is precies de verwarring die dit ADR weghaalt.

**De methode van ADR-022 bestaat, maar alleen in de benchmark.** `build_structured_prompt` (`scripts/lib/skill_prefix.py:156`) doet wat ADR-022 besloot: frontmatter eraf (`:35`), één platte tekst, geen CLI-flag. Hij heeft één aanroeper buiten de tests, de benchmarkadapter (`scripts/benchmark/field-tests/runners/lane_adapter.py:285`). Geen dispatch-lane roept hem. ADR-022 had over de methode gelijk en over de naad niet.

**Wat de fabric al meet, gaat over rollen.** `role_applied` en `role_source_path` worden op de prompt gemeten (`scripts/lib/role_application.py:211`) en op het receipt gestempeld (`scripts/lib/governance_emit.py:111`). Voor een reviewer is die meting waar-door-onderdrukking: `role_applied=True` met `tier="suppressed_reviewer"` (`scripts/lib/role_application.py:234-237`). Er is vandaag geen veld dat zegt of een skillinhoud de worker bereikte.

## Besluit

### 1. Een skill, geen nieuwe rol

Een rol kost vier registers: `agents/<role>/CLAUDE.md`, een permissieprofiel, een routeringsregel en een `skills.yaml`-sleutel. Een skill die in de dispatch-spec gebonden wordt kost één veld en één naad, en bereikt beide rollen die refactoren. De track voegt daarom geen `refactorer`-rol toe.

### 2. Eén gedeelde naad

Het specveld `skill` (`scripts/lib/dispatch_spec.py:242`) is de enige invoer. `dispatch_bridge.stage_spec_bundle` schrijft het (RS-1) en weigert bij het stagen een skill die niet onder `skills/` of `.claude/skills/` bestaat — dezelfde grens die daar al een buiten-bereik-deadline weigert (`scripts/lib/dispatch_bridge.py:293`). Vanaf RS-2 injecteert elke lane het veld door `scripts/lib/skill_context.py` vóór het rolblok: de envelope-lane (`scripts/lib/envelope_prepare.py:83`), de provider-lane (`scripts/lib/provider_dispatch.py:369`) en de pool, die `subprocess_dispatch` start (`scripts/lib/pool_manager.py:121`) en dus dezelfde naad passeert. Oplossingsvolgorde: `skills/<slug>/SKILL.md` eerst, dan `.claude/skills/<slug>/SKILL.md`; beide mappen zijn tracked en RS-5 houdt de twee kopieën van een skill byte-gelijk.

Een reviewer krijgt hem nooit: `enrichment_allowed(role)` beslist (`scripts/lib/reviewer_roles.py:32`), en elke lane vraagt het al (`scripts/lib/envelope_prepare.py:58`, `scripts/lib/subprocess_dispatch.py:212`, `scripts/lib/provider_dispatch.py:344`).

### 3. Receiptsemantiek van `skill_applied`

Deze tabel is het contract, woordelijk:

| Geval | `skill_applied` | `skill_source_path` | `skill_unapplied_reason` |
|---|---|---|---|
| spec zonder `skill` | afwezig | afwezig | afwezig |
| skill gevonden, en de bytes van de gevonden `SKILL.md` (zonder frontmatter) staan letterlijk in de prompt die de lane de worker gaf | `true` | absoluut pad van de gevonden `SKILL.md` | afwezig |
| skill in de spec, oplossing faalt op levermoment | `false` | afwezig | `resolution_failed: <reden>` |
| skill in de spec, dispatch gebonden aan een reviewer | `false` | afwezig | `reviewer_bound` |

`true` is een meting op de prompt zelf, nooit een vlag die de naad zet. Een leverroute die de naad niet passeert stempelt `false` met `resolution_failed: lane_without_seam`, zodat een dekkingsgat op het receipt staat en niet in een logregel verdwijnt. Dat is ook de terugval voor een spec die buiten de brug om geschreven is: `validate()` laat skill-aanwezigheid bewust aan de planlaag (`scripts/lib/dispatch_spec.py:404`), dus voor die route is de meting de vanger, niet een tweede weigering.

`role_applied` wordt niet opgerekt om skills te dekken. Het zou het receipt laten liegen over welke bron aankomst bewees: voor een reviewer staat `role_applied` al `true` terwijl er geen bron is (`scripts/lib/role_application.py:234-237`), en een gebonden skill zou dan als toegepast lezen terwijl hij per besluit niet meegaat.

### 4. De dode `.claude/skills/<role>/CLAUDE.md`-tier gaat weg, niet in reparatie

RS-4 haalt de `skills`-kandidaat uit `scripts/lib/role_application.py:154` en uit `scripts/lib/skill_context.py:264` en `:343`. 0 van de 38.756 receipts op deze checkout loste die tier ooit op, en hem `SKILL.md` leren lezen zou een skill tot rolbon maken. Verwijderen is kleiner dan repareren en laat één rolvolgorde over: `agents/<role>/CLAUDE.md` is de bron.

### 5. `.agents/` blijft gegenereerde scaffolding

`.agents/` is bewust gitignored sinds #1382 (`.gitignore:142`). De rolbon die een worker krijgt is `agents/<role>/CLAUDE.md` — tracked — of de rolprompt onder `scripts/lib/prompts/roles/`, dat ook tracked is; geen van beide zit in het gitignorde `.agents/`. Dit ADR verandert daar niets aan en voegt geen tweede pad toe.

### 6. Terugdraaiorde

RS-3 eerst (de receiptvelden stoppen met stempelen), dan RS-2 (de injectie), dan RS-1 (de stagingweigering). RS-5 en dit ADR draaien onafhankelijk terug. De drie receiptvelden zijn optioneel: een afwezig veld leest als "geen skill gevraagd", dus oude receipts blijven geldig.

## Relatie tot ADR-022

ADR-022 besloot het mechanisme en noemde de naad. Dit ADR houdt het mechanisme en wijzigt de naad, de promptvolgorde, de bronmap en de bewijsvorm. Vier regels uit dat ADR, geciteerd met bestand en regel:

- `docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md:27`: "All worker lanes receive skills through **one provider-agnostic mechanism**: a structured plain-text prompt built by `scripts/lib/skill_prefix.py` and applied uniformly in `lane_adapter.dispatch()`, regardless of provider."
  De uniformiteit blijft het uitgangspunt; de naad verschuift. `lane_adapter.dispatch()` is de benchmarkadapter (`scripts/benchmark/field-tests/runners/lane_adapter.py:285`) en geen dispatch-lane. De naad die elke lane wél passeert is `skill_context._inject_skill_context`. `build_structured_prompt` blijft de opbouwer van de skilltekst — frontmatter eraf, platte tekst, geen `--skills-dir`, geen `Skill()` — en de fabric roept hem vanuit de ene naad aan in plaats van ernaast.
- `docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md:36-37`: "`# YOUR ROLE AND METHODOLOGY` / `<SKILL.md body — full markdown, frontmatter stripped>`".
  Daar *is* de skilltekst de rolsectie: skill en rol zijn hetzelfde ding. Dat houdt stand zolang de specialist de rol is. Voor `backend-developer` en `quality-engineer` is hij dat niet; die hebben een eigen rolbon. Daarom krijgt een gebonden skill een eigen blok **vóór** het rolblok, en blijft de instructie in het midden zoals ADR-022 hem plaatste. Geen van de twee vervangt de ander, en de volgorde is vast: skill (methode) → rol (rolbon) → opdracht → resources → closing.
- `docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md:29`: "The skill content is read from one SSOT folder (`.claude/skills/`), frontmatter stripped, and composed into the prompt." en `:62`: "**One SSOT folder, one function.**"
  Dat wordt één oplossingsorde over twee tracked mappen, `skills/` eerst. Niet twee SSOT's: de kopieën zijn byte-gelijk en een inhoudspin-test in RS-5 houdt ze dat. De drift tussen de overige gedeelde skills is een eigen track, niet deze.
- `docs/governance/decisions/ADR-022-provider-agnostic-skill-injection.md:64`: "**Deterministic + auditable.** The operator sees exactly what was injected."
  Dat is een uitspraak over de invoer. Dit ADR maakt er een meting op de uitvoer van: `skill_applied` toetst de bytes van de gevonden `SKILL.md` tegen de prompt die de lane de worker gaf (besluit 3). Zien wat je injecteerde is niet hetzelfde als bewijzen dat het aankwam; de tweede is testbaar en de eerste niet.

Daarmee vervalt ook de openstaande afweging uit ADR-022 dat een claude-lane de skill dubbel zou injecteren (`:70`): er is één injector, en een skillblok is geen rolbon, dus er valt niets te dubbelen. De fase-2-detectie uit de sunset-tabel (`:91`) is niet nodig.

Twee andere ADR's raken de lanes en worden niet hernummerd. ADR-042 noemt de dispatch-spec fabric-eigendom en harness-onafhankelijk (punt 3) en eist dat buiten de adapter geen harness-specifieke code staat (punt 4): een skill is prompttekst, dus geen adapter krijgt werk en elke harness erft hem. ADR-044 §5 zegt dat een uitgezette provider zijn lane niet start; dan is er geen levering en geen receipt dat een skill claimt, en de weigering leest als `provider_switched_off`, niet als een skill die faalde.

## Gevolgen

- **Eén veld, één naad, drie lanes.** Een skill binden is een specveld zetten. De envelope-, provider- en poollane geven het door aan dezelfde functie; een vierde lane voegt zich door dezelfde functie te bellen.
- **De kosten zijn prompttokens, geen modelcall.** De skilltekst gaat mee in de prompt die de lane toch al bouwt. Op de claude-lane zijn dat abonnementstokens, op een provider-lane tokens tegen dat tarief; er komt geen extra aanroep bij. Een reviewer betaalt niets, want die krijgt hem niet.
- **Een gemiste levering is zichtbaar.** Een route zonder naad stempelt `false` met `lane_without_seam` in plaats van het veld weg te laten. Zo leest een dekkingsgat als een meting, niet als "geen skill gevraagd".
- **Oude receipts blijven geldig.** De velden zijn optioneel en een afwezig veld betekent "geen skill gevraagd". Er is geen migratie en geen schemaversie-bump.
- **De rol-applicatie wordt kleiner.** Na RS-4 zijn er drie kandidaten in plaats van vier, en `role_applied` verwijst naar één bron per rol.
- **Consumerprojecten merken het pas na een release.** De naad zit in de engine; een project op een oudere pin kent het veld niet en gedraagt zich als vandaag.

## Uitrol en meting

Zeven PR's, elk een squash-commit op main en apart terug te draaien. RS-1, RS-5 en RS-6 lopen parallel. RS-2 volgt, want hij leest het veld dat RS-1 schrijft. RS-4 volgt op RS-2, want hij haalt de tier weg die RS-2 heeft vervangen. RS-3 wacht op audit-spoor L-6a: `governance_emit.py` en `receipt_schema.py` zijn daar ook in beweging, en twee bouwers op hetzelfde bestand is de botsing die de volgorde voorkomt. Daarom landt de meting (RS-3) als laatste en draait hij bij een terugdraai als eerste terug.

**Hoe we meten dat het gehaald is.** Vijf dispatches met `skill=refactor-safe` over de drie lanen, plus één zonder skill; daarna de receipts gelezen. De vijf dragen `skill_applied: true` met een `skill_source_path` dat naar een bestaande `SKILL.md` wijst, en de zesde draagt geen van de drie velden. Twee negatieve controles horen erbij: een dispatch met een onbekende skill weigert bij het stagen en schrijft geen bundle, en een reviewer-dispatch met een skill in de spec boekt `false` met `reviewer_bound`. Tot slot de grep die de verwijdering bewijst: na RS-4 noemt `git grep -n "skills/" scripts/lib/role_application.py scripts/lib/skill_context.py` geen `.claude/skills/<role>/CLAUDE.md` meer als rolkandidaat.

## Wat dit ADR niet verandert

- **De rolvolgorde en `role_applied`.** Welke rolbon wint blijft `scripts/lib/role_application.py`; dit ADR voegt geen rolmeting toe en verandert geen tier.
- **De reviewer-onderdrukking.** `enrichment_allowed` blijft de ene beslissing; dit ADR voegt daar alleen een skill aan toe die dezelfde poort passeert.
- **ADR-042's adaptergrens en ADR-044's schakelaar.** Beide blijven staan; dit ADR leunt erop.
- **De resource-index uit ADR-022.** Een skillmap heeft naast `SKILL.md` ook `references/` en `scripts/`. RS-2 levert de `SKILL.md`-body zonder frontmatter, en dat is precies wat `skill_applied` meet; de map wordt niet geïndexeerd en de worker leest een referentie op aanvraag, zoals bij elke andere bron in zijn werkmap. Een resource-index per skill is een eigen besluit, niet een bijproduct van dit veld.
- **De planlaag en de deur.** `validate()` blijft skill-aanwezigheid aan de planlaag laten (`scripts/lib/dispatch_spec.py:404`); de stagingweigering komt daarvoor in de plaats, niet ernaast.

## Alternatieven die zijn afgewezen

**(a) Een nieuwe rol `refactorer`.** Afgewezen. Vier registers voor één methode, en het werk valt al onder twee bestaande rollen. Een rol toevoegen omdat een skill ontbreekt, zet de kosten op de verkeerde laag.

**(b) De `skills`-tier repareren zodat hij `SKILL.md` leest.** Afgewezen. Dan wordt een skill een rolbon: de rol-applicatie zou een skill als rol bewijzen en `role_applied` zou over een skill gaan. Bovendien is de tier in 38.756 receipts nooit opgelost; er is niets om te repareren.

**(c) `role_applied` uitbreiden zodat het ook skills dekt.** Afgewezen. Voor een reviewer staat `role_applied` al `true` zonder bron (`scripts/lib/role_application.py:234-237`); een skill eronder schuiven maakt het receipt onwaar over welke bron aankomst bewees. Twee metingen, twee velden.

**(d) Een lijst skills per dispatch (`skills: [a, b]`).** Afgewezen voor deze ronde. Eén slug dekt het geval dat de track oplost, en een lijst vraagt een ordeningsregel en een resource-index per skill die niemand vandaag nodig heeft. `skill_prefix.build_structured_prompt` kan er meerdere aan; het specveld blijft één.

**(e) Per lane een eigen injectie.** Afgewezen. Dat is de drievoudige divergentie die ADR-022 al afwees: envelope, provider en pool hebben vandaag elk hun eigen verrijkingspad, en één gedeelde functie is de reden dat een reviewer-uitzondering op één plek kan staan.
