# ADR-092 - Een optionele feature staat aan en faalt dicht, of staat benoemd uit; wat wegvalt staat op het receipt

**Status:** Voorgesteld 18-11-2026 (track `optionele-features-expliciet`, levering ADR, dispatch `dlv-25bb2364ca7b`). Breidt ADR-060 § 5 uit en vervangt drie regels van ADR-030 via cross-ref; die bestanden blijven ongewijzigd (ADR-079 § 83).
**Datum:** 2026-11-18
**Besloten door:** de operator (B-20261118-bdb7faf4: deze track hoort bij 2.0) en T0 op de plan-gate van 18-11 (ronde 5, tiebreaker STOP onder de stopregel, plan versie 6 met OI-3004 tot en met OI-3007 opgelost).
**Cross-refs:** ADR-030 (plan-first-gate; regels 29, 30 en 38 vervangen door besluit 5), ADR-060 (§ 5, twee views met één bron; uitgebreid door besluit 4), ADR-079 (§ 83, bestaande ADR's zijn bevroren), ADR-087 (één configuratie-oppervlak; PR-6 levert de lezer van `VNX_HASH_CHAIN_REQUIRED`); plan `claudedocs/specs/optionele-features-expliciet/plan.md` (gitignored, niet in de repo), versie 1 § Ontwerp, V2.1 tot en met V2.3, V3.4, V5.1, V5.2 en V6.1 tot en met V6.5; track `optionele-features-expliciet`, leveringen OF-2c, OF-2, OF-4, OF-3, OF-5, OF-9 en OF-8.

## Context

Een feature die de garanties van VNX draagt, kan vandaag stil wegvallen. Gemeten op main `abd25c4e9`:

- `VNX_CI_GATE_REQUIRED=0` zet de CI-poort van de deur niet uit: `_run_door` roept `_run_ci_gate` altijd aan (`scripts/pr_merge.py:3317`).
- `_ci_gate_available` geeft `False` als `gh` ontbreekt, ook als de sleutel aan staat (`scripts/lib/gate_request_handler.py:702-704`). De poort valt dan stil uit de reviewstapel.
- De aanvraag van reviewstoelen loopt via `review_seats.resolve_review_seats` (`scripts/lib/review_seats.py:259`, aangeroepen in `gate_request_handler.py:1889`). Daarin voegt `_config_stack_entries` `ci_gate` alleen toe op `VNX_CI_GATE_REQUIRED` (`review_seats.py:201-210`); de dial en `branch_protection.yaml` tellen niet mee. De verificatie (`gate_profile.required_stack`, `scripts/lib/gate_profile.py:317`) telt de sleutel via `_ci_gate_source` (`:305-314`) alleen als hij gezet is: de registerstandaard `"1"` is daar geen verzoek. Aanvraag, verificatie en deur hanteren dus drie regels.
- Een onbekende waarde van `VNX_PLAN_GATE_ENFORCE` leest als `off`, een onleesbare store als `advisory` (`scripts/lib/plan_gate_enforcement.py:60-74`).
- Onder `required` laat `_check_track_link_verdict` een dispatch door bij een onleesbare tracks-DB (`scripts/lib/dispatch_cli.py:1285-1294`, severity `warn`) en bij een fout in `plan_gate_state`, die `UNSUPPORTED` wordt (`:1320-1324`).
- `branch_protection.yaml` kent `enforcement: enforce | warn | off` (`scripts/lib/forge_protection_drift.py:36-44`, `:95-106`). Onder `warn` merget een afwijking met een waarschuwing, onder `off` leest de deur de live bescherming niet (`pr_merge.py:1619-1624`, `_judge_enforcement_off` `:1775`), en zonder bestand geeft de deur een `warn` GO (`_judge_project_without_protection_file`, `:1808`).
- `VNX_HASH_CHAIN_REQUIRED` staat in het register met standaard `"0"` (`scripts/lib/config_registry.py:459-460`).
- Een weggevallen garantie wordt nergens vastgelegd. Alleen een override per merge laat een spoor achter (`OVERRIDE:` op het receipt, `pr_merge.py:870`).

## Besluit

**1. Het contract.** Een feature staat aan en faalt dicht, of staat benoemd uit. Afwezigheid beslist nooit.

- Eén sleutel per feature, met een gesloten set waarden. Het ontbreken van een binary of bestand zet een feature niet uit.
- Aan plus een ontbrekende afhankelijkheid weigert. De melding noemt de sleutel en wat ontbreekt.
- Uit wordt vastgelegd als `waived_guarantees: [{feature, key, value, source, guarantee}]`.
- Een onbekende of onleesbare waarde weigert. Zij leest nooit als uit.

Reden: een garantie die verdwijnt zonder spoor is erger dan een weigering. Een weigering is te herstellen, een stille merge zonder CI niet.

**2. De tabel van features** staat in `scripts/lib/optional_features.py`.

| Feature | Sleutel | Aan | Uit | Garantie die wegvalt |
|---|---|---|---|---|
| `ci` | `VNX_CI_GATE_REQUIRED` | `1` | `0` | gemergede code is getest op exact deze head |
| `plan_gate` | `VNX_PLAN_GATE_ENFORCE` | `required` | `advisory` (gedeeltelijk uit), `off` | `advisory`: een bouw zonder groen plan wordt niet geweigerd. `off`: het plan wordt niet beoordeeld vóór de bouw |
| `branch_protection` | het bestand, geen registersleutel | aanwezig met `enforce` | afwezig, `warn`, `off` | zie besluit 5 |
| `hash_chain` | `VNX_HASH_CHAIN_REQUIRED` | `1` | `0` | een gewijzigde of verwijderde receipt-regel valt niet op: het grootboek is niet tamper-evident |

De garantietekst staat in deze module, niet als veld in `ConfigEntry`. Dat wijkt bewust af van het onderzoek (§ B.2): een veld in `config_registry.py` zou deze track in de seriële keten van dat bestand zetten, achter config-een-oppervlak. Een bindtoets houdt de twee gelijk: elke sleutel uit de tabel bestaat in het register, elke garantietekst is niet leeg, en elke uit-waarde valt binnen `allowed_values` zodra het register dat veld geeft. `branch_protection` is de enige rij zonder registersleutel: het bestand is zelf de configuratie.

**3. Waar de lijst staat, en één momentopname per handeling.**

- Een veld `waived_guarantees` op `ExecutionPlan` (`scripts/lib/dispatch_plan.py:148`), dat meetelt in de identiteitshash.
- Het `pr_merged`-receipt (`_write_merged_receipt`, `pr_merge.py:2439`) en het preflight-record.
- De samenvatting van check-run `vnx-gate/door` (`scripts/lib/forge_gate_publisher.py`).
- Een sectie in `vnx doctor`, met als kop dat het de huidige stand is. Doctor leest vers en is geen bewijs van een eerdere handeling.

Eén bouwer: `optional_features.waived_guarantees(project_id)`. Er is geen tweede kopie. De deur neemt de momentopname één keer, direct na het vastpinnen van de kop; preflight-record, receipt en check-run krijgen díe momentopname. De dispatch-deur neemt hem bij het plannen; de permit neemt bij het vuren een nieuwe en weigert bij een verschil met `waived_changed_since_plan`.

Reden: twee lezingen binnen één handeling kunnen elkaar tegenspreken, en dan bewijst het receipt niets.

**4. CI: één regel voor elke handhaver. Uitbreiding van ADR-060 § 5.** `gate_profile.ci_requirement(project_id)` geeft `(vereist, bronnen)`. CI is vereist als minstens één van vier bronnen het eist:

1. `VNX_CI_GATE_REQUIRED` waar, **ook als registerstandaard**;
2. de dial `ci_green_required` >= 2;
3. een CI-context in de verplichte checks van `branch_protection.yaml` op main;
4. `ci_gate` in de eigen reviewketen van het project.

De aanvraag (`review_seats.resolve_review_seats` via `_config_stack_entries`, in beide takken), de verificatie (`gate_profile.required_stack`, waarin `_ci_gate_source` opgaat), de beschikbaarheid en de deur lezen deze functie. `_ci_gate_available` laat de poort niet meer vallen: vereist zonder `gh` weigert met `ci_gate_dependency_missing: gh`. De helper komt eerst zonder aanroeper (OF-2c); aanvraag, verificatie en beschikbaarheid schakelen samen om in één PR (OF-4), met een samenhangtoets over alle combinaties van de vier bronnen. Pas daarna slaat de deur `ci` over als `ci_requirement` onwaar is (OF-3); tot dan draait de deur CI altijd, wat strenger is en dus veilig.

ADR-060 § 5 (`docs/governance/decisions/ADR-060-poortprofiel-per-project.md:81`) noemt alleen de dial en de gezette sleutel. Deze ADR breidt dat uit in drie punten: bron 3, bron 4, en de registerstandaard telt als aan. Reden: met vlag `0`, dial 1 en een CI-context in de YAML zou de deur anders CI eisen die `vnx gate` niet verifieert en de aanvraag nooit plant.

*Grens, aanvaard:* een project zonder dial en zonder vlag krijgt na OF-4 `ci_gate` in aanvraag en verificatie. De deur draaide CI daar al; nu doen alle handhavers hetzelfde. Wie geen CI wil, zet `VNX_CI_GATE_REQUIRED=0` zonder andere bron, en dan staat `ci` op de lijst.

**5. Branch protection: verklaard tegenover geverifieerd.** Het bestand wordt gelezen met de bestaande zoekvolgorde en parser van `forge_protection_drift` (`PROTECTION_YAML_SEARCH_PATHS`, `forge_protection_drift.py:93`: `.vnx/branch_protection.yaml` vóór `scripts/forge/branch_protection.yaml`; `fetch_yaml_from_ref` `:798`, `load_protection_config` `:514`), op de vastgepinde revisie van main. Er komt geen tweede lezer. `file_absent` volgt alleen als beide paden ontbreken.

Per situatie van het bestand:

| Situatie | Gedrag |
|---|---|
| bestand afwezig op main | `branch_protection` op de lijst met `source: file_absent`: geen serverweigering voor een directe push, geen verplichte `vnx-gate/*`-check |
| bestand onleesbaar of ongeldig | weigeren met `branch_protection_unreadable`; nooit als afwezig lezen |
| bestand noemt `vnx-gate/door` of `vnx-gate/review` niet | die check op de lijst als `vnx_gate_door` of `vnx_gate_review`, `source: branch_protection.yaml` |
| check verplicht, publicatie faalt | de deur weigert met `check_run_publish_failed` en de naam van de check |
| check niet verplicht, publicatie faalt | een regel op het receipt; de garantie stond al op de lijst |

Per stand van `enforcement` op main:

| Stand | Op de lijst | Garantie die wegvalt |
|---|---|---|
| `enforce` (of het veld ontbreekt) | niets, tenzij een rij hierboven geldt | geen |
| `warn` | `{feature: branch_protection, key: enforcement, value: warn, source: <pad>@<main-sha>}` | de live serverbescherming wordt niet afgedwongen: een afwijking merget met een waarschuwing. De checks in het bestand zijn verklaard, niet geverifieerd |
| `off` | `{feature: branch_protection, key: enforcement, value: off, source: <pad>@<main-sha>}` | de deur leest de serverbescherming niet; de checks zijn verklaard, niet geverifieerd. Alleen de CI-workflow-vloer blijft |
| geen bestand | `source: file_absent` | zoals de eerste tabel |
| onbekende waarde | weigeren met `branch_protection_unreadable` | geen |

Een check in het bestand is een verklaring. Alleen de live vergelijking onder `enforce` maakt hem geverifieerd. De deur voegt aan de regel `server_verified` toe (waar alleen onder `enforce` na een vergelijking zonder afwijking) en onder `warn` `drift_fields`. Een dispatch-momentopname leest alleen de verklaarde stand en draagt `server_verified: null`, nooit `true`. Het verlagen van `enforcement` vraagt al `--allow-weaken` (`is_weakening`, `forge_protection_drift.py:1095-1097`); dat blijft.

Reden: een bestand met `warn` dat beide checks noemt gaf een lege lijst terwijl niemand de server toetste.

**6. Plan-gate. Vervangt drie regels van ADR-030.** Een onbekende stand weigert met `plan_gate_mode_unknown`, een onleesbare met `plan_gate_mode_unreadable`; beide noemen waarde en bron. Onder `required`, voor een dispatch met track:

| Bewijs | Uitkomst |
|---|---|
| `PASSED` | door, niets op de lijst |
| `UNRESOLVED` | weigeren, zoals vandaag |
| tracks-DB of `plan_gate_state` gooit | weigeren met `plan_gate_evidence_unreadable`, met de fout |
| schema zonder de kolommen (`UNSUPPORTED`) | weigeren met `plan_gate_evidence_unsupported` |
| een van de twee hierboven met `VNX_OVERRIDE_PLAN_GATE=1` | door; op de lijst als `{feature: plan_gate, key: VNX_OVERRIDE_PLAN_GATE, value: 1, source: override, reason: <weigercode>}` |

Een dispatch zonder track is een afbakening, geen onleesbaar bewijs. Het gedrag blijft zoals vandaag (`track_unlinked` of `no-track:<reden>`), met één regel op de lijst: `{feature: plan_gate, key: track_id, value: none, source: <track_unlinked of no-track:<reden>>, guarantee: deze bouw hangt aan geen plan}`. Onder `advisory` en `off` verandert het gedrag niet; de stand zelf staat op de lijst.

Dit vervangt in ADR-030 de regel "unknown value fails safe to `off`" (`ADR-030-plan-first-gate-enforcement.md:30`) en onder `required` de twee terugvallen "callers degrade to WARN" (`:29`) en "`unsupported` → clean" (`:38`). Reden: een geraden stand of een geraden pass is erger dan een nieuwe poging.

*Grens, aanvaard:* op een `required`-project weigert een tracks-DB die even bezet is nu een dispatch in plaats van hem door te laten. De verbinding wacht al 10 s (`dispatch_cli.py:1260`); de melding zegt dat opnieuw proberen de weg is.

**7. Hashketen.** `hash_chain` leest `VNX_HASH_CHAIN_REQUIRED` via de canonieke lezer van config-een-oppervlak PR-6 (ADR-087 besluit 9), nooit met een eigen lezer. De standaard is `0`, dus elk project zonder rij draagt de regel. Reden: dezelfde keuze als `advisory`; de lijst zegt wat niet afgedwongen wordt.

*Grens, aanvaard:* door `advisory` en `hash_chain` is de lijst op een standaardproject nooit leeg. Dat is bedoeld. Wie hem leeg wil, zet `required` en de hashketen aan.

*Grens, aanvaard:* de plan-identiteit wisselt als een stand wijzigt tussen plan en vuur. Dat is dicht falen; de prijs is een herhaalde dry-run.

**8. Buiten deze ADR.** De daemons (`VNX_DAEMONS`, `vnx init --daemons`, de foreground-ingang) horen bij track `daemons-expliciet` (B-20261118-3ced8444). De voorrang van `project_config` boven env en de hashketen-schakelaar zelf zijn config-een-oppervlak PR-6 (ADR-087). `vnx config list` is config PR-10a; die toont de lijst zodra hij bestaat. `--override-reason` per merge blijft zoals het is.

## Gevolgen

**Leveringen, in deze volgorde:**

- OF-2c: alleen de helper `gate_profile.ci_requirement` met de lezer van besluit 5; geen aanroeper wijzigt.
- OF-2: `optional_features.py` met de tabel, de bindtoets en de rijen per `enforcement`-stand.
- OF-4: aanvraag, verificatie en beschikbaarheid samen naar `ci_requirement`, met de samenhangtoets en een meting per project vooraf. Grens 300 regels (poortpaden).
- OF-3: de deur, met de momentopname, `server_verified`, `drift_fields` en het overslaan van `ci`. Na OF-4.
- OF-5: de plan-gate-standen en het bewijs onder `required` (besluit 6). Na config PR-6v.
- OF-9: `hash_chain` in de tabel. Na config PR-6.
- OF-8: de lijst in `vnx doctor` en de check-run.

**Wat deze ADR niet toestaat:**

- een feature die uitvalt omdat een binary, bestand of rij ontbreekt;
- een onbekende of onleesbare waarde die als uit of als `advisory` leest;
- een tweede bouwer van de lijst of een tweede lezer van `branch_protection.yaml`;
- `server_verified: true` zonder live vergelijking;
- een wijziging van ADR-030 of ADR-060 in hun eigen bestand.

**Terugweg:** per PR `git revert`. Elke nieuwe weigering heeft een sleutel die haar uitzet en vastlegt.

## Alternatieven

- **`--override-reason` per merge als enige klep.** Verworpen: dat legt één merge vast, geen staande keuze van een project.
- **De garantie als veld in `ConfigEntry`.** Verworpen voor nu: het zet deze track in de seriële keten van `config_registry.py`. De bindtoets van besluit 2 houdt de tekst en het register toch gelijk.
- **Een onbekende stand lezen als `advisory` in plaats van te weigeren.** Verworpen: een geraden stand is erger dan een nieuwe poging.
