# ADR-060 - Het poortprofiel per project: één sleutel, één tabel, één leesfunctie en een ondergrens die geen profiel zakt

**Status:** Aanvaard 08-11-2026 (T0, track `gate-profiel-per-project`, levering P9). De tabel, de precedentie, de fail-closed-regel, de twee views en het wijzigingscontract zijn de besluiten B1 tot en met B16 van het plan; dit ADR legt ze duurzaam vast. Elke wijziging aan de tabel, de ondergrens of de fail-closed-regel is een amendement op dit ADR en nooit een bouwkeuze.
**Datum:** 2026-11-08
**Besloten door:** T0, op het plan `claudedocs/2026-11-07-naar-1-9/plannen/gate-profiel-per-project.md` (revisie 2 van 08-11-2026). De plan-gate van 08-11 gaf REVISE (codex-seat) en werd door de tiebreaker (deepseek-v4-pro) op START gezet met één bindende voorwaarde: een onbekende of malformed profielwaarde faalt dicht. Die voorwaarde staat hier letterlijk. Het nummer is **060**: 058 en 059 zijn op `origin/main` bezet en het plan noemde nog 058.
**Gemeten op:** `origin/main` `2eceaaef5` (2026-11-08). Elk regelnummer hieronder is op die commit gemeten; een bewering zonder regelnummer is een meting die ik niet heb gedaan en die er dus niet staat.
**Cross-refs:** ADR-008 amendement 4 van 08-11-2026 (de veiligheidsondergrens en de `gate-merge`-uitzondering), ADR-030 amendement 2026-10-25 (de tweede plan-gate-zetel vanaf de start), ADR-041 (de tiebreaker bij de review-cap), ADR-046 (één poortmotor), ADR-047 (de tracks-DB is de enige planbron).

## Context

Een project kon zijn eigen zwaarte niet in één beweging kiezen. De vier waarden die de zwaarte bepalen kwamen uit vier bronnen, en drie lezers lazen ze onafhankelijk: de reviewstapel uit `VNX_DEFAULT_REVIEW_STACK`, de rondegrens uit `VNX_REVIEW_ROUND_CAP`, de overnameketen uit `VNX_REVIEW_GATE_TAKEOVER_CHAIN`, en de plan-gate-zetels uit `configs/plan_gate_panel.yaml`. `VNX_GOVERNANCE_PROFILE` bestond niet. Daarnaast schreef `vnx init` een `.vnx/governance_profiles.yaml` die de loader nooit las: een consumer dacht een profiel te hebben en de poort las het niet.

De acht leveringen P0 tot en met P8 hebben dat verholpen. P0 legde de veiligheidsondergrens vast in het amendement op ADR-008, P1 bouwde de ene leesfunctie, P2 liet de bron van de zwaarte zelf onder de zwaarte vallen, P3 tot en met P5 sloten de lezers aan, P6 bracht het spoor in de receipt, P7 gaf `vnx gate` en `vnx gate-profile` dezelfde bron en P8 stopte met het schrijven van het inerte profiel. Wat ontbrak is het duurzame contract: één ADR dat zegt wat het profiel is, waar het boven of onder gaat, wat er gebeurt bij een onleesbare waarde, en wat een profielwijziging met openstaand werk doet.

"Profile" betekent in deze repo al vijf dingen: providerprofielen in `$VNX_DATA_DIR/profiles/*.env`, startprofielen van `vnx start --profile`, de glob-governanceprofielen, `coding_strict`/`business_light`, en CI Profile A. Dit ADR gaat over geen van die vijf. Het gaat over het **poortprofiel**: de zwaarte van de reviewdeur en de plan-gate, gekozen met `VNX_GOVERNANCE_PROFILE` en gelezen door `scripts/lib/gate_profile.py`.

Alles aan dit contract is deterministisch: de invoer is een gesloten enum van drie namen, de uitvoer een gesloten dict van vier waarden, en de regels zijn een tabel, een precedentieketen, een validatie en een weigering. Er zit geen model in dit pad en er komt er ook geen bij. De enige "model-achtige" beslissing is *welke* waarden `light` en `heavy` krijgen; die staat als genummerd besluit in het plan en hier in de tabel, niet in code die een oordeel vraagt.

## Besluit

### 1. Eén sleutel, drie namen, vier waarden

De sleutel is `VNX_GOVERNANCE_PROFILE`, geregistreerd als `enum` met default `standard` (`scripts/lib/config_registry.py:205-215`). De drie namen staan in `gate_profile.PROFILES` (`scripts/lib/gate_profile.py:52`) en de volledige tabel in `gate_profile.PROFILE_TABLE` (`scripts/lib/gate_profile.py:66`). `standard` is geen ontwerp: het is de gemeten "vandaag"-kolom, en een test pint die aan de registry-defaults.

| Waarde (veld) | Registry-key / bron | `light` | `standard` (= vandaag) | `heavy` |
|---|---|---|---|---|
| `review_stack` (wat de deur vraagt) | `VNX_DEFAULT_REVIEW_STACK` (`scripts/lib/config_registry.py:116-117`) | `codex_gate` (+ `ci_gate` als `VNX_CI_GATE_REQUIRED`) | `codex_gate,kimi_gate` (+ `ci_gate`) | `codex_gate,kimi_gate,claude_gate` (+ `ci_gate`) |
| `round_cap` | `VNX_REVIEW_ROUND_CAP` (`scripts/lib/config_registry.py:190-191`) | `2` | `3` | `5` |
| `takeover_chain` | `VNX_REVIEW_GATE_TAKEOVER_CHAIN` (`scripts/lib/config_registry.py:141-143`) | `codex_gate,kimi_gate,claude_gate` | `codex_gate,kimi_gate,claude_gate,deepseek_gate` | `codex_gate,kimi_gate,claude_gate,deepseek_gate` |
| `plan_gate_seats` (vanaf de start) | `configs/plan_gate_panel.yaml:47-48` via `compose_panel` | `1` | `1` | `2` |
| `required_stack` (wat `vnx gate` verifieert) | de enforcement-laag op `review_stack`, zie §5 | uit `review_stack` + enforcement | idem | idem |
| ondergrens (mag nooit omlaag) | `light_floor_refusal` + `second_seat_from_start` | `light` weigert op een signaalpad, zie §4 | ongewijzigd | ongewijzigd |

`heavy` houdt vier ketennamen: een vijfde schakel (`glm_gate`, op tegoed) zou "zwaar" tot "meer tegoed" maken (B7). `standard` draagt niets bij: `_contribution` (`scripts/lib/gate_profile.py:149`) geeft voor `standard` op elk veld `None`, dus elke lezer valt door naar zijn eigen keten en "standaard is het gedrag van vandaag" is een constructie-eigenschap, geen belofte (B3). `ci_gate` is orthogonaal: de bestaande keten voegt hem toe wanneer `VNX_CI_GATE_REQUIRED` waar is, het profiel nooit.

De lezers, elk met één expliciete aanroep en hun eigen terugvalketen (B12):

| Veld | Lezer(s) |
|---|---|
| `review_stack` | `review_seats.resolve_review_seats` (`scripts/lib/review_seats.py:259`) |
| `round_cap` | `review_rounds.round_cap` (`scripts/lib/review_rounds.py:818`; een malformed cap faalt luid in `parse_round_cap`, `:757`) |
| `takeover_chain` | `gate_request_handler._build_review_gate_takeover_chain` (`scripts/lib/gate_request_handler.py:178`); `providers_cli._review_chain_consequence` (`scripts/providers_cli.py:223`) |
| `plan_gate_seats` | `planning_cli._profile_panel_inputs` (`scripts/planning_cli.py:4851`) door `_derive_panel_weight` (`:4887`) naar `plan_gate_panel.compose_panel` (`scripts/lib/plan_gate_panel.py:647`) |
| `required_stack` | `gate_profile.required_stack` (`scripts/lib/gate_profile.py:317`), via `scripts/gate_profile_cli.py` en `_g_required_gates` (`scripts/commands/gate.sh:33`) |

### 2. De precedentie (B4)

Per waarde geldt één keten:

```
VNX_OVERRIDE_<BARE>  >  project_config-rij  >  [enforcement-laag: alleen review/required stack]
                     >  profiel  >  VNX_<BARE>-env  >  registry-default
```

De keten zelf is `config_registry.resolve_with_source` (`scripts/lib/config_registry.py:664`), die `_resolve` (`:698`) niet verandert: elke bestaande aanroeper leest byte-gelijk wat hij las (B1). `gate_profile.resolve` (`scripts/lib/gate_profile.py:155`) is de enige lezer van de profielsleutel. `gate_profile.value` (`:173`) geeft de profielbijdrage alleen wanneer de veldeigen sleutel níet uit `override` of `project_config` kwam: een operator die de stapel expliciet zette, wint van het profiel. Een per ongeluk geëxporteerde env-var verliest van het profiel, want een governance-instelling hoort niet stil door de omgeving overruled te worden.

### 3. Fail-closed op een onbekende of malformed waarde (B13)

Dit is de bindende voorwaarde van de tiebreaker, en zij staat hier letterlijk als regel. `gate_profile.resolve()` leest de ruwe waarde en valideert **vóór** de tabelmapping:

- afwezig, `None`, leeg of alleen witruimte → "niet gezet" → `standard`, bron `default` of `unset`;
- na één `strip()` exact in `{light, standard, heavy}` → geldig;
- al het andere (`zwaar`, `Heavy`, `high`, `standard heavy`, een tikfout) → `GateProfileConfigError` met de **ruwe** waarde, de **bron** en de reden (`scripts/lib/gate_profile.py:99`).

Geen enkele lezer vangt die fout weg. De deur weigert: `resolve_review_seats` (`scripts/lib/review_seats.py:259`) gooit de fout, en `gate_request_handler.request_reviews` (`scripts/lib/gate_request_handler.py:1778`) roept hem aan (`:1837`) zonder de fout in te slikken, dus er wordt niets aangevraagd. `vnx gate` stopt met de fout van de CLI en kiest geen literal (`scripts/commands/gate.sh:33`, CLI op `:38`). De plan-gate weigert: `_profile_panel_inputs` (`scripts/planning_cli.py:4851`) laat de fout door. `vnx gate-profile show` print de fout en exit 2 (`EXIT_REFUSED`, `scripts/gate_profile_cli.py:35`; `error_report`, `:70`). Het record draagt `profile_raw`, `profile_source` en `profile_error` en schrijft **nooit** `profile: "standard"` op de fout. Een terugval op `standard` verzwakt de governance stil; dat is precies wat deze regel uitsluit. De normalisatie is bewust minimaal — één `strip()`, case-sensitive: `" heavy "` is geldig, `"Heavy"` niet.

### 4. De veiligheidsondergrens en de `gate-merge`-uitzondering

Het volledige ondergrenscontract staat in **ADR-008, amendement 4 van 08-11-2026**; dit ADR kopieert het niet, want een kopie drift en de bron is één. Wat hier vastligt is de verwijzing en de plaats in code:

- **Reviews.** Op een kop van klasse `high` of `critical` is elke stoel van de aanvraagstapel vereist (`seats_for_risk`, `scripts/lib/review_seat_evaluation.py:354`; alleen `low` en `medium` versmallen, `_FIRST_SEAT_CLASSES`, `:132`). `light` heeft één stoel en **weigert** daarom op zo'n kop (`light_floor_refusal`, `:791`), behalve wanneer de beslissende categorie **uitsluitend** `gate-merge` is (`_signal_risk`, `:770`). Een expliciete stack met twee stoelen blijft werken: expliciet wint, en de ontsnapping is een stack in de aanvraag, geen codewijziging.
- **Plan-gate.** `second_seat_from_start` (`scripts/lib/plan_gate_panel.py:2381`) is een vloer onder elk profiel: schema, security, outside-repo en elke irreversible categorie geven de tweede zetel vanaf de start. `compose_panel` (`:647`) gebruikt `max(profile_seats, vloer)`. `gate-merge` alleen geeft **geen** tweede zetel.
- **Fail-closed.** Kan de overnameketen een vereiste stoel niet vullen, dan blokkeert de deur met de reden in `RequiredSeats.error` (`scripts/lib/review_seat_evaluation.py:305`). Een `unknown`-uitkomst is nooit een pass.
- **Eén signaalset.** De vloer en de plan-gate lezen dezelfde categorieën uit `smart_router`: `PLAN_SCOPE_GATE_MERGE` (`scripts/lib/smart_router.py:967`), `_PLAN_SCOPE_PREFIXES` (`:995`, met `configs/` en `.vnx/` als `gate-merge` op `:1049-1050`) en `_IRREVERSIBLE_PREFIXES` (`:909`). Twee lezers, één tabel.

### 5. Twee views met één bron (B10)

`vnx gate` en de reviewdeur stellen verschillende vragen, dus er zijn twee views op dezelfde functie:

- `review_stack` (`scripts/lib/gate_profile.py:292`) is wat de deur vraagt: het profiel, of de keten die het project zelf zette;
- `required_stack` (`scripts/lib/gate_profile.py:317`) is wat `vnx gate` verifieert: de `checks.*_required`-dials uit `.vnx/governance_enforcement.yaml` toegepast op de reviewstapel. Een dial op level ≥ 2 maakt die poorten de stapel; level 1 is adviserend en verplicht niets; level 0 haalt een stoel weg. `ci_gate` komt erbij als `ci_green_required` ≥ 2 of `VNX_CI_GATE_REQUIRED` waar is.

`vnx gate-profile show` print beide views met per naam de bron, plus een waarschuwing zodra de nominale profielstapel en de effectieve verplichte stapel verschillen (`stack_difference`, `scripts/gate_profile_cli.py:77`; de leesfunctie `build_report`, `:53`). Gemeten in dit project: de dials zijn `codex_gate_required: 2`, `kimi_gate_required: 2`, `ci_green_required: 3` (`.vnx/governance_enforcement.yaml:15-28`), dus `required_stack` is `codex_gate,kimi_gate,ci_gate` — gelijk aan de aanvraagstapel van vandaag. Een project waar de dials afwijken volgt nu de dial in plaats van `vnx gate` tegen te spreken; dat is de correctie van bevinding 2 van de plan-gate.

### 6. Het spoor in de receipt (B8, B15)

Het profiel is additief op het bestaande record, zonder schemaversie-bump, en buiten het hash- en identiteitspad. De drie sleutels zijn `profile`, `profile_source` en `profile_error`:

- het aanvraagrecord: `ReviewSeats.to_record` (`scripts/lib/review_seats.py:128`) draagt de drie sleutels plus `stack_source` en de stapel als `value` met `seats`/`extra_gates`;
- het resultaat-receipt `review_gate_result`: `_profile_receipt_fields` (`scripts/lib/gate_recorder.py:1943`), aangeroepen uit `_outcome_receipt_fields` (`:1963`, aanroep `:2001`), leest de drie sleutels uit de payload of zijn `review_seats`-record;
- het merge-record: `SeatEvaluation.to_record` (`scripts/lib/review_seat_evaluation.py:1704`) voegt via `_profile_trail` (`:1684`) ook `stack_source`, `required_error` en (bij een geweigerde waarde) `profile_raw` toe; `stack` staat er wanneer de risicoregel de zetels bepaalde;
- de plan-gate: `plan_gate_evidence` schrijft `gate_profile` uit de `gov_trace` (`scripts/lib/plan_gate_evidence.py:41` en `:114`), die het label `profile=<naam> (<bron>)` levert (`scripts/planning_cli.py:4868`, uit `_profile_panel_inputs`, `:4851`).

Een receipt zonder de sleutels leest als **onbekend**, nooit als `standard`: een record van vóór P6 en een record op een geweigerde waarde zeggen allebei "ik weet het niet", en alleen een echte lezing zegt `standard`/`default`.

### 7. Het wijzigings- en terugdraaicontract (B14, zoals gebouwd)

Een profielwijziging is een gewone configwijziging; de gevolgen liggen vast. Per clausule staat er of zij vandaag **houdt** of een **grens** is, met de consequentie. Wie dit leest mag alleen de houdende clausules als garantie lezen.

1. **Opgeslagen stapels — houdt.** Het aanvraagrecord (`scripts/lib/review_seats.py:128`) en het merge-record (`scripts/lib/review_seat_evaluation.py:1704`) dragen het profiel en de bron van het moment van de aanvraag; niets herschrijft een bestaand record bij een latere wijziging. *Grens:* het `review_gate_result`-receipt draagt alleen de drie profielsleutels (`scripts/lib/gate_recorder.py:1943`) en geen `stack`/`stack_source`, en het aanvraagrecord heeft geen `stack`-lijst (de stapel staat in `value` plus `seats`/`extra_gates`). *Consequentie:* een lezer van het resultaat-receipt weet onder welk profiel het oordeel viel, maar niet welke stapel; daarvoor leest hij het aanvraagrecord of het merge-record.
2. **Open verplichtingen — grens.** De status `retired` bestaat (`scripts/lib/gate_obligations.py:81`) en alleen de deur of de runner schrijft hem, maar **geen codepad koppelt die status aan een profielwijziging**: `scripts/gate_obligation_runner.py` noemt `gate_profile` nergens (grep, 0 treffers). *Consequentie:* een nog niet gestarte verplichting die een lichter profiel niet meer nodig heeft, wordt niet gepensioneerd; zij blijft open tot zij vervuld is of volgens de bestaande regels (dode dispatch, verdwenen PR) vervalt. Het plan belooft hier dus niets meer dan de status en de schrijfrechten.
3. **Herkomst — houdt.** `profile_source` komt uit `config_registry.resolve_with_source` (`scripts/lib/config_registry.py:664`): `override`, `project_config`, `env`, `default` (en `unset` voor een ongeregistreerde sleutel; deze sleutel is geregistreerd en geeft `default`). `stack_source` komt uit `_config_stack_source` (`scripts/lib/review_seats.py:235`): `profile`, `override`, `project_config`, `env` of `default`; `required_stack` voegt daar `enforcement` aan toe (`scripts/lib/gate_profile.py:317`). Dat zijn precies de twee sets uit het plan.
4. **Rollback — houdt.** De sleutel weghalen of op `standard` zetten maakt de bijdrage `None` (`scripts/lib/gate_profile.py:149`) voor elke **toekomstige** aanvraag; open verplichtingen blijven staan en geen record wordt herschreven. Geen migratie, geen dataverlies. *Grens:* de rollback kan een open verplichting ook niet pensioneren, want clausule 2 bestaat niet — dat is dezelfde grens, niet een tweede.
5. **Midden in de vlucht — grens.** De merge-deur toetst niet in het algemeen tegen het huidige profiel. Wat bestaat is `_recorded_below_current` (`scripts/lib/review_seat_evaluation.py:821`, aangeroepen op `:764`): een geregistreerde stapel van één stoel waarvan het record `stack_source == "profile"` zegt, wordt geweigerd wanneer de verse resolutie op een signaalkop twee of meer stoelen geeft. *Consequentie:* een stapel die een operator expliciet naam, wordt niet tegen een strenger geworden profiel hertoetst; de deur vraagt de geregistreerde zetels. Dat is de bedoeling (expliciet wint), maar het betekent dat de belofte "nooit op de lichtere eis van een ouder profiel" alleen de door het profiel geleverde stapels dekt. De andere richting houdt wel: een record onder een zwaarder profiel behoudt zijn zwaardere eis tot het vervuld of gepensioneerd is.

### 8. Wat dit ADR niet doet

Het verandert geen code en geen gedrag: P0 tot en met P8 zijn gemerged en dit is de duurzame vastlegging. Het activeert `VNX_REVIEW_SEAT_RULE` niet (die sleutel blijft onbezet; de regel van 30-10 beslist elke merge tot de schaduwtest haar aanzet), het voegt geen stoel toe aan of haalt er geen weg uit `configs/plan_gate_panel.yaml`, het voegt geen vijfde naam aan de overnameketen toe (B7), en het raakt `receipt_schema.py`, het hashpad en `append_receipt_internals/` niet aan (B8: additief, geen bump).

## Consequenties

- **Positief:** één sleutel zet de zwaarte van de deur en de plan-gate in één beweging, en `standard` laat het gedrag van vandaag aantoonbaar staan. Een project dat `light` of `heavy` kiest, verlaat de oude keten bewust en zichtbaar.
- **Positief:** een onleesbare waarde blokkeert luid met de ruwe waarde en de bron. Dat is duurder dan een stille terugval en precies de bedoeling: een typefout in een governance-instelling mag de deur niet stil verzwakken.
- **Negatief:** `light` weigert op een beveiligings-, schema-, irreversible-, outside-repo- of store-resolution-pad. Een project dat `light` kiest, merkt dat op zo'n kop als een blokkade, niet als een automatische optrekking; de uitweg is een expliciete twee-stoels stack in de aanvraag.
- **Negatief:** de twee views kunnen verschillen zodra de `checks.*_required`-dials afwijken van de profielstapel. Dat verschil is zichtbaar in `vnx gate-profile show` en niet stil: de dials gaan vóór, en de waarschuwing noemt wat er bij kwam en af ging.
- **Open:** clausule 2 (profielgedreven pensionering) en de brede vorm van clausule 5 (hertoetsing van een expliciet gezette stapel) zijn niet gebouwd. Wie een profiel verlicht, houdt openstaande verplichtingen; wie een stapel expliciet zette, houdt die stapel. Beide staan hier als grens, niet als garantie.
- **Opvolging:** een wijziging aan de tabel, de ondergrens, de fail-closed-regel of de herkomstsets is een amendement op dit ADR. De spiegelregel van ADR-008 ("Transition of obligations") geldt onverkort voor wat wél bestaat: een open verplichting wordt nooit stilzwijgend ingetrokken of vervuld.
