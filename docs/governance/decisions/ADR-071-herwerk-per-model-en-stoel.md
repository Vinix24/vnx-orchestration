# ADR-071 - Herwerk per bouwermodel en per reviewstoel: een randtabel als waarheid, vier randsoorten, drie maten met een rijpheidsregel en een rapport dat alleen voorstelt

**Status:** Voorgesteld 11-11-2026 (T0, track `herwerk-per-model-en-reviewer`, rij H0); aangenomen zodra de T0 H0 merget.
**Datum:** 2026-11-11
**Besloten door:** de operator op 10-11-2026 (herwerk terug naar de oorspronkelijke dispatch, per bouwermodel en per reviewstoel) en het plan `claudedocs/2026-11-07-naar-1-9/plannen/herwerk-per-model-en-reviewer.md` (revisie 2, plan-gate van 10-11) voor besluit 1 tot en met 14. Het nummer 071 is door de T0 toegewezen.
**Cross-refs:** ADR-007 (samengestelde sleutel over `project_id`), ADR-063 (dezelfde scheiding tussen meten, voorstel en oordeel), ADR-065 (leren is voorstellen), ADR-069 (effectmeting: het patroon van n-regel en rijpheid).

Dit ADR legt vast wat de rijen H1 tot en met H7 bouwen. Het bevat geen code en voegt geen besluit toe aan het plan: elk besluit hieronder is dat van het plan, in dezelfde nummering.

**Deterministisch, en waarom er geen model in zit.** De randen komen uit git blame, uit `work_ref` en uit twee stagingvlaggen. De maten zijn tellingen over receipts, `rounds.ndjson` en merge-feiten. De voorstellen zijn drempelregels. Er is geen open taal, geen oordeel onder ambiguïteit en geen synthese over ongestructureerde bronnen, dus geen model. Ontbrekende of gebrekkige invoer valt naar `unattributed`, `unknown`, `immature` of `incomplete`, nooit naar een telling als herwerk of als succes.

## Context

1. `dispatch_metadata` kent geen enkele leveringsdispatch: 5.846 rijen, 0 met `dispatch_id like 'dlv-%'`. De enige schrijver is `upsert_dispatch_provider_row`, aangeroepen vanuit `provider_dispatch.py`. De claude-headless-lane en de subprocess-lane schrijven niets.
2. `parent_dispatch` bestaat in 1.170 van 3.821 spec-bundels en 1.321 van 45.421 receipts, maar staat in 0 van 5.846 rijen van `dispatch_metadata`. Een fix die uit een OI of een eerdere PR voortkomt kan zijn oorsprong niet noemen.
3. `scripts/attribute_rework.py` vond in een droge run 235 randen over de laatste 300 commits, maar draait nergens: er is geen launchd-job.
4. `review_gates/rounds.ndjson` heeft sinds 19-10 de stoelgeschiedenis per kop (1.621 regels over 494 PR's), maar zonder het model van de stoel.
5. Gate-, panel- en benchmarkdispatches staan in dezelfde tabellen als bouwwerk (4.846 benchmarkdispatches). Zonder uitsluiting vervuilt dat elke noemer.

Meetbasis: `origin/main` `d3c28569` en de centrale store, 10-11-2026.

## Besluit

**1. De randtabel is de waarheid, `parent_dispatch` is een projectie.** Een fix kan regels van twee dispatches raken, en een rand heeft een soort en bewijs. De randsoorten (`kind`) zijn `blame`, `round`, `oi_fix` en `pr_fix`. De tabel `rework_edges` staat in de per-projectstore `quality_intelligence.db`, naast `dispatch_metadata`. Kolommen: `project_id`, `rework_dispatch_id`, `origin_dispatch_id`, `kind`, `class` (`rework` of `churn`), `evidence_json` (commit, bestanden, aantal regels, OI-id of PR-nummer), `detected_at` en `source` (`spec`, `receipt`, `blame`). Schrijven is `INSERT OR IGNORE`: een rand verandert nooit meer. `dispatch_metadata.parent_dispatch` blijft fill-once de dominante oorsprong, zodat de self-join in `rework_attribution.rework_by_origin_role` blijft werken.

**2. Elke lane schrijft een `dispatch_metadata`-rij bij de start.** De claude-headless-lane en de subprocess-lane roepen `upsert_dispatch_provider_row` aan met provider, model, track, rol, PR en `parent_dispatch` uit het plan van de deur. `parent_dispatch` is fill-once (`COALESCE`, nooit overschrijven). Een bestaande rij, zoals van een gate-run, verandert niet van vorm.

**3. De oorsprong wordt bij het stagen genoemd, niet geraden.** `stage_spec_bundle` en de `dispatch_bridge`-CLI krijgen `origin_oi` en `origin_pr`. `origin_oi` leest het `dispatch`-veld van de OI uit `open_items_manager`. `origin_pr` leest de kopbranch van de PR en neemt `<id>` uit `dispatch/<id>`. Ze vullen `parent_dispatch` en leggen een verklaarde rand vast (`oi_fix` of `pr_fix`). Het stagen schrijft ook een `round`-rand uit `work_ref`, via `record_edge`. Spreken `origin_oi` of `origin_pr` een `work_ref` of een expliciete `parent_dispatch` tegen, dan weigert het stagen, met de regel van `resolve_parent_dispatch`. Een oorsprong die niet op te lossen is, is een weigering met de reden en nooit een lege rand.

**4. Herwerk en churn zijn verschillende dingen.** Een `blame`-rand R naar O zegt alleen dat R regels verving die O schreef. Het is herwerk als het commit-type van R `fix` of `revert` is, of als R een `05_debugging`-taak was. Bij `feat`, `refactor`, `docs`, `test` of `chore` is het churn: gerapporteerd, maar geen herwerk. Verklaarde randen `oi_fix` en `pr_fix` zijn altijd herwerk. Een `round`-rand is herwerk vóór de merge en telt voor first-pass yield, niet voor herwerk na de merge.

**5. De drie maten, het 14-dagenvenster en de n-regel.**
- *First-pass yield per bouwer* (provider plus model): het aandeel eerste dispatches (geen `-rN`, geen `-b`) waarvan de PR mergede zonder ronde en zonder split. Een PR die zonder merge sloot telt als niet in één keer goed.
- *Herwerk binnen 14 dagen per bouwer*: het aandeel gemergede dispatches O met minstens één herwerkrand R naar O waarvan R binnen 14 dagen na de merge van O mergede.
- *Gemist per stoel* (gate plus model): elke stoel die op de laatste kop van O's PR (`head_sha` gelijk aan de kop bij de merge) PASS met 0 blockers gaf, terwijl O daarna herwerk kreeg, heeft één gemiste bevinding. Noemer: het aantal van zulke PASS-uitslagen van die stoel op gemergede koppen in het venster. Een overname telt voor `reviewer`, niet voor `seat`.
- Elke cel draagt `n`. Onder `n=20` toont het rapport alleen tellingen, geen percentage.
- Het stoelmodel komt uit `reviewer_model` in de regel, anders uit de kop van het rapport in `report_path`, anders staat het in een eigen cel `model=unknown`, nooit bij een bekend model.

**6. De noemer is bouwwerk.** Uitgesloten: benchmarkdispatches (`rework_attribution.benchmark_excluded_count`, 4.846 op de meetbasis), gate-dispatches (`*-gate-pr*`), `panel-*`, `plan-gate-*` en `plan-tiebreak-*`, receipts die `receipt_outcome.noise_reason` als ruis aanmerkt, en dispatches zonder PR. Het rapport noemt per uitsluitingsreden het aantal, zodat de noemer na te rekenen is.

**7. De koppeling draait als eigen nachtjob.** De nachtelijke intelligence-pipeline staat niet ingepland, dus een fase daarin zou niet draaien. `com.vnx.rework-attribution` draait om 05:30 lokaal `attribute_rework.py --persist` en daarna het rapport naar `state/rework_report.json`, plus een `HealthBeacon` met `expected_interval_seconds=90000`. Het patroon is dat van `com.vnx.dream-watch`. De job is fail-open voor de rest van de vloot: een fout schrijft een beacon met de reden en raakt geen andere producent.

**8. Leren blijft voorstellen.** Het rapport heeft een sectie `## Voorstellen` met deterministische regels. Het schrijft nooit routering, vloeren of stoelketens: geen schrijfpad naar `routing_recommendations.yaml`, `project_config` of `provider_constraints.yaml`. Een test greept de module daarop. De voorsteldrempels staan onder "Voorsteldrempels".

**9. Geen receipt-formaatwijziging en geen poortpad.** De leesmodule importeert `receipt_outcome` en leest `rounds.ndjson`, maar wijzigt geen van beide. Het `reviewer_model`-veld komt uit #2607, niet uit dit track.

**10. Eén bron voor de merge-feiten.** Per PR zijn mergemoment en mergekop:
1. de `pr_merged`-receipt van de merge-deur: `timestamp` is het mergemoment, `review_seats.head_sha` de beoordeelde mergekop (10-11: 854 receipts, 503 met `head_sha`);
2. ontbreekt die receipt of zijn `head_sha`, dan GitHub (`gh pr view --json mergedAt,headRefOid,headRefName`), met `merge_source: "github"` op de rij;
3. lukt geen van beide, dan telt de PR niet mee en staat hij onder `unattributed` met de reden. Nooit een mergemoment uit een commitdatum of een receipt-tijd van de bouwer.

De koppeling dispatch-PR loopt via de kopbranch `dispatch/<id>`, met het `pr_number` in de receipts van de bouwer als controle. Spreken die twee elkaar tegen, dan is de rij `unattributed` met reden `pr_join_conflict`. Het 14-dagenvenster en de noemer van de stoelen lezen alleen deze merge-feiten.

**11. Rijpheid.** Een oorsprong O telt pas in een 14-dagenmaat als zijn mergemoment minstens 14 dagen voor het peilmoment van het rapport ligt. Jongere oorsprongen tellen in teller noch noemer en staan per cel als `immature: <n>`. Een gemiste bevinding op een stoel volgt dezelfde regel, op de oorsprong.

**12. Identiteit en ontdubbeling.**
- De eenheid voor first-pass yield is de levering: het basis-id `dlv-<hex>`. Rondes `-rN` en splits `-a`/`-b` vouwen erin. De levering telt voor het model van zijn eerste dispatch.
- Herwerk na de merge wordt toegerekend aan de dispatch die de vervangen regels schreef (de `blame`-oorsprong), met het model van die dispatch.
- Per dispatch-id telt één identiteit: de eerste receipt met een niet-lege provider en model. Twee receipts met verschillende modellen voor hetzelfde id geven `model=conflict` als eigen cel.
- Per `(pr_number, head_sha, reviewer)` telt één uitslag: de laatste `recorded_at`.

**13. Dekking, inhalen en gebrekkige invoer van de nachtjob.** De job scant elke commit op main sinds een watermerk (`state/rework_attribution_watermark.json`, de laatste volledig verwerkte commit), niet de laatste 300. Een gemiste nacht haalt de volgende run in vanaf het watermerk. Gebrekkige invoer (git faalt, `provenance_registry` onleesbaar, een ledger dat niet te lezen is) zet het watermerk niet verder, schrijft het beacon als `degraded` met de reden, en het rapport markeert het venster als `incomplete`. Een gedeeltelijke run schrijft alleen randen van volledig verwerkte commits.

**14. Stratificatie en de grens van "gemist".** Een voorstel vergelijkt alleen bouwers binnen dezelfde `task_class` en risicoklasse, en vraagt `n >= 20` in die laag. Voor stoelen heet de maat **doorgelaten herwerk**: een PASS op een kop waarvan de dispatch later herwerk kreeg. Het rapport noemt het een bovengrens, want de fout kan buiten de diff liggen die de stoel zag. Een stoelvoorstel vergelijkt stoelen binnen dezelfde risicoklasse, op dezelfde PR's waar mogelijk (gepaard), en vraagt `n >= 20` gepaarde koppen.

**De tabel `rework_edges` en ADR-007.** Elke nieuwe tabel in een centrale database vraagt een samengestelde UNIQUE of primaire sleutel over `project_id` (ADR-007). De primaire sleutel is `(project_id, rework_dispatch_id, origin_dispatch_id, kind)`. Twee `project_id`'s in één database zien elkaars randen niet. H3 voegt alleen deze tabel toe en raakt geen bestaande tabel.

**Voorsteldrempels.** Een bouwervoorstel (bijvoorbeeld een vloer voor een taakklasse naar sonnet) vraagt `n >= 20` in beide cellen van de laag van besluit 14 en 95%-Wilson-intervallen die elkaar niet overlappen: de bovengrens van de betere cel ligt onder de ondergrens van de andere. Een stoelvoorstel volgt dezelfde regel binnen één risicoklasse. De vaste drempel van 5 procentpunt is vervallen: gemeten op 13-11 geeft 10 van 20 een interval van 30 tot 70%, dus 5 punten verschil bij n=20 zegt niets. Onder `n = 20` toont het rapport tellingen en doet het geen voorstel.

**15. De oorzaak van een ronde.** Elke ronde (`-rN` of splitsing) heeft een van vier oorzaken: `review`, `ci`, `grens` of `operator`. De oorzaak wordt bij het stagen gedeclareerd (`--round-cause`) en staat in de `evidence_json` van de `round`-rand; de titelregel (`GRENS` en `CI` in `rework_metrics.py`) geldt alleen als terugval voor historie zonder declaratie, en een ronde zonder leesbare titel is `unknown`. First-pass yield telt alleen rondes met oorzaak `review` of `unknown`; `ci`, `grens` en `operator` verlagen de opbrengst van een bouwer niet.

## Consequenties

- H1 tot en met H7 bouwen tegen dit ADR. Code die een definitie hier wijzigt, wijzigt eerst dit ADR.
- Livegang is het moment waarop H5 gemerged en de job geïnstalleerd is, met H1 tot en met H7 op main. Het meetvenster begint daar. De periode ervóór is een apart gemarkeerde nulmeting en wordt nooit bij het venster opgeteld.
- Het rapport kan een voorstel doen, nooit een wijziging doorvoeren. Routering blijft een besluit van de operator.
- Cellen met `model=unknown` en `model=conflict` blijven zichtbaar en vervuilen geen bekend model.
- Doorgelaten herwerk per stoel is een bovengrens en mag niet als foutenpercentage van de stoel worden gelezen.

## Terugdraaien

Zet de nachtjob `com.vnx.rework-attribution` uit (`launchctl bootout`) en verwijder de plist: dan schrijft niets meer randen of rapport. De tabel `rework_edges` en de kolom-vulling van `parent_dispatch` blijven staan, want randen veranderen nooit en `parent_dispatch` is fill-once. Er is geen datamigratie nodig om terug te draaien: geen enkele bestaande tabel is gewijzigd, en het rapport en het watermerk zijn afgeleide bestanden die weg kunnen.
