# ADR-087 - Eén configuratie-oppervlak: de globale laag, vijf lagen voorrang, scope, de lezersgrens en het handhavingsprofiel

**Status:** Voorgesteld 18-11-2026 (track `config-een-oppervlak`, dispatch `dlv-253486b12a21`). Amendeert ADR-023 via cross-ref; dat bestand blijft ongewijzigd (ADR-079 § 83).
**Datum:** 2026-11-18
**Besloten door:** T0, op de plan-gate van 18-11 (rondes 3 en 4: codex REVISE twee keer, opus REVISE één keer, tiebreaker START twee keer). Operatorbesluit B-20261118-e0417fd9 (Q3: één handhavingssleutel).
**Cross-refs:** ADR-007 (project_id in elke centrale sleutel), ADR-023 (receipt-hashketen; geamendeerd door besluit 9), ADR-026 (een store per project), ADR-044 (de providerschakelaar als globaal bestand), ADR-079 (§ 83, bestaande ADR's zijn bevroren); plan `claudedocs/specs/config-een-oppervlak/plan.md` (gitignored, niet in de repo), § V2.1, V2.2 en V3.1 tot en met V3.6; track `config-een-oppervlak`, leveringen G1, G2, G3, PR-6, PR-10a, PR-Q3a en PR-Q3c.

## Context

Een instelling van VNX leeft vandaag op twee plekken die elkaar niet kennen: het register met zijn opgeslagen `project_config`-rij, en de omgeving van het proces. De inventaris van 18-11 telt 217 `VNX_*`-namen die buiten het register letterlijk uit de omgeving gelezen worden, op 624 plekken, en tien registersleutels die ook rechtstreeks uit de omgeving gelezen worden.

Die tien lezen in een eigen volgorde. `VNX_PLAN_GATE_ENFORCE` leest eerst de omgeving en pas daarna de opgeslagen rij (`scripts/lib/plan_gate_enforcement.py:60`): een geëxporteerde `advisory` verslaat een geaudite `required`. De keten in het register zelf heeft vier lagen, zonder globale laag (`scripts/lib/config_registry.py:20-24`, `scripts/lib/config_registry.py:813`).

Er is ook geen plek voor een waarde die voor alle projecten op deze machine geldt, zoals de Ollama-host of het mailadres van de nachtelijke digest. Wie dat vandaag wil, exporteert het in een plist of een rc-bestand, buiten elke audit.

Tot slot hangt handhaving aan zes losse schakelaars, elk met eigen waarden en een eigen lezer. De operator koos bij Q3 voor één sleutel.

De drie bouwleveringen G2 (de keten), PR-6 (handhavingslezers en de hashketen-vlag) en PR-Q3a (het profiel) bouwen tegen het contract dat deze ADR vastlegt.

## Besluit

**1. De globale laag is één bestand: `~/.vnx/config.json`, met de audit in `~/.vnx/config_audit.ndjson`.** Een globale waarde geldt voor elk project van dit gebruikersaccount op deze machine.

- Vorm: `{"version": 1, "values": {"<KEY>": {"value", "changed_at", "changed_by", "approval_id", "reason"}}}`.
- Versiebeleid: een lezer accepteert elke major tot en met de eigen. Onleesbaar zijn alleen: ongeldige JSON, een ontbrekende of niet-gehele `version`, een hogere major, of `values` dat geen object is. Een onbekende sleutel wordt genegeerd en één keer gelogd; een schrijver laat hem staan.
- Schrijven: `fcntl`-lock op `config.json.lock`, binnen de lock opnieuw lezen, eerst de auditregel toevoegen (actor, cwd, pid, argv, sleutel, oud, nieuw, approval_id, reden), dan atomisch vervangen. Een mislukte vervanging krijgt een `abort`-regel in de audit.
- Een onleesbaar bestand wordt nooit overschreven: `vnx config set --global` weigert en noemt het bestand en de auditlog.
- `VNX_GLOBAL_CONFIG_FILE` verplaatst het bestand, en audit en lock gaan mee. De naam is intern, voor toetsen en installatie. Het is bewust niet `VNX_CONFIG_FILE`: die naam is al van `config.yml` (`bin/vnx:187`).

Reden: er is geen gedeelde database om een globale tabel in te zetten. Elk project heeft een eigen store (ADR-026), en een job zonder project, zoals de nachtelijke analyzer, moet de waarde ook kunnen lezen. De providerschakelaar draait al zo, als bestand onder `~/.vnx/` (ADR-044, `scripts/lib/provider_switch.py:242-246`). Er komt geen nieuwe centrale tabel bij, dus ADR-007 is op deze laag niet van toepassing.

*Grens, aanvaard:* het bestand hoort bij één gebruikersaccount op één machine. Twee accounts op dezelfde Mac hebben elk een eigen globale laag. De Mac mini deelt niets met deze Mac.

**2. Scope en toegestane waarden per sleutel.** `ConfigEntry` (`scripts/lib/config_registry.py:48`) krijgt twee velden:

- `scope`:
  - `project` (de standaard, en de stand van alle bestaande sleutels): de globale waarde is de standaard voor elk project, en een projectrij wint;
  - `global`: alleen een globale waarde telt. Het schrijven van een projectrij wordt geweigerd. Een projectrij die er toch staat, negeert de resolver met een WARNING.
- `allowed_values` voor enum-sleutels. Het schrijven weigert een waarde buiten die set. Zonder `allowed_values` blijft het gedrag van vandaag: geen controle.

Reden: een machinewaarde (Ollama-host, analyzer-lane, digestmail, SMTP-host) heeft geen betekenis per project. Een projectrij daarvoor is een fout die stil verkeerd zou lezen.

**3. De voorrangsketen heeft vijf lagen**, hoogste eerst, in `config_registry.resolve_with_source`:

| # | Bron | `source` | Geldt voor |
|---|---|---|---|
| 1 | `VNX_OVERRIDE_<BARE>` in de omgeving van het proces | `override` | elke sleutel |
| 2 | `project_config`-rij van het project | `project_config` | alleen scope `project` |
| 3 | waarde in het globale bestand | `global_config` | elke sleutel |
| 4 | `VNX_<BARE>` in de omgeving bij de start | `env` | elke sleutel |
| 5 | registerstandaard | `default` | elke bekende sleutel |

- Een onbekende sleutel zonder waarde geeft `unset`.
- Met `strict_resolver=True` stopt de keten bij een onleesbare laag op `project_config_error` of `global_config_error`. Zonder strict valt hij door naar de volgende laag, met een WARNING. Dat is het bestaande gedrag van de projectlaag (`scripts/lib/config_registry.py:838-847`), nu ook voor de globale laag.
- De globale laag heeft geen projectkoppeling nodig: `config_registry.get` leest hem ook in een proces waarin `config_runtime.autowire` geen store vond.

Reden: een opgeslagen waarde, project of globaal, is een bewuste, geaudite keuze. Een geëxporteerde env-waarde kan een vergeten regel uit een plist zijn. Daarom wint opgeslagen van env. Env blijft werken waar niets is opgeslagen: CI, een verse installatie, een plist-job. Wie één proces wil overrulen, gebruikt `VNX_OVERRIDE_<BARE>`; dat is de rem per proces.

*Grens, aanvaard:* wie vandaag een env-waarde exporteert om een opgeslagen rij te overrulen, verliest dat. PR-6 noemt die gedragswijziging in de changelog en de PR-tekst.

**4. De lezersgrens.** Alleen deze modules lezen een `VNX_*`-configuratiewaarde uit `os.environ`:

- `config_registry.py` (lagen 1 en 4);
- `config_runtime.py` (`VNX_STATE_DIR` en `VNX_PROJECT_ID`, om de store te vinden);
- de globale laag (`VNX_GLOBAL_CONFIG_FILE`);
- de padresolvers `vnx_paths.py` en `project_root.py`;
- de namen op de allowlist van de leestoets (PR-9), elk met reden.

Al het andere leest via `config_runtime.get`/`get_bool`, of voor een sleutel met scope `global` via `config_registry.get`.

Bash leest een registersleutel via `vnx config get --raw <KEY> [<KEY>...]`, met dit contract:

- Alleen bash buiten de bootstrapbestanden (besluit 5), buiten hooks en buiten lussen. Een shell-hook laat de Python die hij aanroept de waarde bepalen.
- De binary komt uit `vnx_resolve_root.sh` (`"$VNX_ROOT/bin/vnx"`), nooit uit `PATH`, zodat een tweede geïnstalleerde CLI niet stil een andere fabric leest.
- Uitvoer: één regel per gevraagde sleutel, in volgorde. Een lege regel is: geen waarde.
- Exitcodes: 0 gelukt, 2 onbekende sleutel, 3 een opslaglaag onleesbaar (de lezing is strict). Elke andere code betekent dat de CLI ontbreekt of crasht.
- Bij een code ongelijk aan 0 gebruikt een lezer op een handhavingspad de strenge waarde en meldt dat. Elke andere lezer stopt met een melding die de sleutel noemt. Nooit stil een standaard.

Reden: één lezer is de enige manier waarop de voorrang op elke plek dezelfde is. Het geval van `VNX_PLAN_GATE_ENFORCE` bewijst wat er gebeurt als elke lezer zijn eigen volgorde kiest.

**5. Bootstrap: eerst paden, dan beleid.** `project_root` roept `data_dir_guard` aan tijdens de padbepaling (`scripts/lib/project_root.py:64`, aanroep op `scripts/lib/project_root.py:110`). Zou de guard het profiel via `config_runtime` lezen, dan ontstaat een lus: `config_runtime.autowire` → `vnx_paths.resolve_paths` → `project_root` → `data_dir_guard` → `config_runtime`.

- Padbepaling (`vnx_paths`, `project_root`, `vnx_resolve_root.sh`) leest nooit beleid via `config_runtime` en roept nooit `vnx config` aan.
- `data_dir_guard` werkt in twee fasen:
  - tijdens de padbepaling **legt hij vast**. Hij leest het profiel alleen uit de lagen zonder store (override, globaal, env, standaard). Geven die `enforced`, dan breekt hij meteen af; anders schrijft hij de bevinding weg;
  - na een geslaagde `autowire` **dwingt hij af**: `config_runtime.autowire` roept `data_dir_guard.enforce_recorded()` aan, die het volledige profiel leest en bij `enforced` met een vastgelegde bevinding afbreekt.
- De bash-bootstrapbestanden (`bin/vnx` tot de overdracht aan Python, `scripts/lib/vnx_paths.sh`, `scripts/lib/vnx_resolve_root.sh` en alles wat zij sourcen) en de hooks roepen nooit `vnx config` aan. Een registerwaarde die zij nodig hebben, bepaalt het Python-proces dat haar gebruikt. De export op `scripts/lib/vnx_paths.sh:305` (`VNX_AUTO_REPORT`) verdwijnt daarom in PR-7.

Reden: de store vinden vraagt de paden, dus de paden mogen niet van de store afhangen. Twee fasen houden de guard streng waar dat zonder store kan, en volledig zodra de store er is.

**6. Het handhavingsprofiel `VNX_ENFORCEMENT_PROFILE`.** Enum `shadow` of `enforced`, standaard `shadow`, scope `project`, `requires_approval`. Eén lezer: `scripts/lib/enforcement_profile.py` met `enforced() -> bool`, via `config_runtime`. Het vervangt zes schakelaars, zonder alias (Q10 mat dat niemand ze zet):

| Oude variabele | Lezer | `shadow` (gedrag van vandaag) | `enforced` |
|---|---|---|---|
| `VNX_HOOK_ENFORCE` | `hooks/pretooluse_spawn_detector.py` | toestaan en loggen | blokkeren |
| `VNX_SCHEMA_STRICT` | `governance_emit.py`, `report_assembler.py`, `dispatch_govern.py` | schemafout loggen | schemafout werpt |
| `VNX_CONTRACT_VALIDATE` | `dispatch_govern.py` | `shadow` | `enforce` |
| `VNX_CONSTRAINTS_STRICT` | `provider_dispatch.py` | ontbrekende `provider_constraints.yaml` loggen | weigeren |
| `VNX_AUTONOMY_EVALUATION` | `governance_evaluator.py`, `safe_autonomy_cutover.py` | evaluatie in schaduw | evaluatie handhaaft |
| `VNX_DATA_DIR_GUARD` | `data_dir_guard.py` | `warn` | start afbreken |

- Een waarde buiten de set (alleen mogelijk via env, override of een met de hand bewerkt bestand, want het schrijven weigert hem) leest als `enforced`, met een ERROR die waarde en bron noemt. Wie iets anders typt dan de standaard, wil strenger of vergiste zich; in beide gevallen is streng de veilige kant.
- Gooit de hook-lezer een fout, dan geldt `enforced` met een ERROR. Een kapotte lezer zet de controle niet uit.
- De harde blokkades in `pretooluse_spawn_detector.py` blijven onvoorwaardelijk, in beide standen.
- Eén proces terug naar schaduw: `VNX_OVERRIDE_ENFORCEMENT_PROFILE=shadow`.
- Registersleutel `VNX_GOVERNANCE_ENFORCED` (gedeclareerd, zonder effect) verdwijnt in PR-Q3b.

**7. Het profiel als de store niet gevonden wordt.** `config_runtime` legt vast waarom `autowire` mislukt, op te vragen met `config_runtime.wire_status()`: `no_project` (geen project-id te bepalen), `store_missing` (project-id bekend, `runtime_coordination.db` ontbreekt) of `resolver_failed` (de padresolver gaf een fout). `enforcement_profile.enforced()` leest met `strict_resolver=True`:

- **Store gekoppeld en leesbaar:** de gewone keten.
- **Store gekoppeld, lezen faalt:** `enforced`, bron `project_config_error`, en een ERROR die sleutel, oorzaak en terugval noemt. Er bestaat een store die mogelijk `enforced` zegt en die niet te lezen is.
- **`store_missing` of `resolver_failed`:** de lagen 1, 3, 4 en 5, bron `project_config_error` met de oorzaak. Eén ERROR per proces noemt de sleutel, het project, de oorzaak en de gebruikte terugval met waarde en bron, en zegt dat een opgeslagen projectwaarde niet is toegepast.
- **`no_project`:** de lagen 1, 3, 4 en 5, met één WARNING. Zonder project bestaat er ook geen projectwaarde die zou kunnen ontbreken.

Reden: een opgeslagen `enforced` mag nooit stil `shadow` worden. Elk geval waarin dat zou kunnen, krijgt een ERROR met een foutbron en is te toetsen. Een vergrendelde of ontbrekende store zet anderzijds niet alles ineens op `enforced`: CI heeft geen store.

*Grens, aanvaard:* bij `store_missing` geldt de terugval uit de lagen zonder store, dus meestal `shadow`. Dat is luid (ERROR) en gemeten, niet streng. PR-Q3a meet de productie-ingangen waar `store_missing` optreedt (hook in een worker-worktree, plist-job, CI) en herstelt de resolutie voor een productie-ingang die de store van ADR-026 niet vindt.

**8. De gedeelde schrijver en `unset`.** Projectrijen worden geschreven door `config_store_db.set_config` (`scripts/lib/config_store_db.py:147`), dezelfde functie die het dashboard gebruikt (`dashboard/api_config.py:118`). De controles horen daarom daar, niet in het register:

- `set_config` weigert een projectrij voor een sleutel met scope `global`;
- `_coerce` (`scripts/lib/config_store_db.py:101`) weigert een waarde buiten `allowed_values`, dus ook vanuit het dashboard;
- een nieuwe `unset_config` verwijdert de projectrij en schrijft één auditrij met `operation = 'unset'`, de vorige waarde als `old_value` en `new_value = ''`. Een lege waarde zetten blijft `operation = 'set'`. Een sleutel met `requires_approval` vraagt ook bij `unset` een approval-id;
- `--reason` komt in een nieuwe kolom `reason` (project) en in de auditregel (globaal).

Eén migratie, alleen toevoegend: `ALTER TABLE project_config_audit ADD COLUMN operation TEXT NOT NULL DEFAULT 'set'` en `ADD COLUMN reason TEXT`. `new_value` blijft `TEXT NOT NULL` (`scripts/lib/config_store_db.py:52`). Geen tabel herbouwd, geen kolom gewijzigd; bestaande rijen lezen als `set` zonder reden. De ADR-007-sleutels blijven onaangeroerd: `PRIMARY KEY (project_id, config_key)` en `UNIQUE (project_id, event_id)` (`scripts/lib/config_store_db.py:44`, `scripts/lib/config_store_db.py:57`).

Reden: een verwijdering zonder auditrij is een wijziging die niemand kan terugvinden. Een toevoegende migratie kan niets verliezen.

**9. Amendement op ADR-023: `VNX_CHAIN_RECEIPTS` gaat op in `VNX_HASH_CHAIN_REQUIRED`.** Vandaag leest de hashketen een losse env-vlag (`scripts/lib/ndjson_hash_chain.py:341`), terwijl de registersleutel `VNX_HASH_CHAIN_REQUIRED` bestaat zonder lezer (`scripts/lib/config_registry.py:459-463`). Na PR-6 is er één sleutel: `VNX_HASH_CHAIN_REQUIRED`, gelezen via de keten van besluit 3, met de huidige standaard (uit). `VNX_CHAIN_RECEIPTS` verdwijnt. Waar ADR-023 en zijn amendement van 2026-09-27 `VNX_CHAIN_RECEIPTS` noemen, geldt voortaan `VNX_HASH_CHAIN_REQUIRED`. Het mechanisme van ADR-023, ADR-029 en ADR-034 verandert niet. Het bestand `ADR-023-receipt-hash-chain.md` blijft ongewijzigd (ADR-079 § 83).

Reden: twee namen voor één keuze laten het register iets tonen wat de code niet doet.

## Gevolgen

**De leveringen bouwen en toetsen:**

- G1: het bestandscontract van besluit 1, zonder lezer, gedragsneutraal;
- G2: `scope`, `allowed_values` en de vijf lagen in `config_registry.py`, met een toets per laag en per scope, en een autouse-fixture in `tests/conftest.py` die `VNX_GLOBAL_CONFIG_FILE` per toets op een leeg tijdelijk pad zet. Laag 3 wint van laag 4, dus zonder die fixture zou een globaal bestand op de machine van een ontwikkelaar een gemonkeypatchte env-waarde verslaan;
- G3: besluit 8;
- PR-10a: `vnx config get --raw` volgens besluit 4;
- PR-6: de omgedraaide voorrang voor de env-first-lezers en besluit 9;
- PR-Q3a: besluit 6 en 7, eerst de rode toets (projectrij `enforced`, store niet gevonden: ERROR, bron `project_config_error`, oorzaak `store_missing`);
- PR-Q3c: besluit 5.

**Wat deze ADR niet toestaat:**

- een nieuwe directe `os.environ`-lezing van een `VNX_*`-instelling buiten de grens van besluit 4;
- `vnx config` aanroepen vanuit padbepaling, bootstrap-bash of een hook;
- een projectrij voor een sleutel met scope `global`;
- een `unset` zonder auditrij, of een migratie die een kolom of de ADR-007-sleutels wijzigt;
- een profiellezing die bij een onleesbare of ontbrekende store stil `shadow` geeft.

**Gedragswijzigingen:** een opgeslagen waarde verslaat een geëxporteerde env-waarde (besluit 3); een globale `VNX_ANALYZER_LLM` verslaat de waarde uit de plist van de analyzer.

**Amendement.** ADR-023 geldt voortaan met `VNX_HASH_CHAIN_REQUIRED` in plaats van `VNX_CHAIN_RECEIPTS` (besluit 9). Het bestand verandert niet.

## Alternatieven

- **Een centrale DB-tabel voor globale waarden.** Afgewezen: onder ADR-026 is er geen gedeelde store. Een tabel in één projectstore zou een project tot eigenaar van de machine maken, en een job zonder project kan hem niet lezen.
- **Globaal onder env.** Afgewezen: dan verslaat een vergeten export in een plist een bewuste, geaudite keuze. Dat is precies de fout van `VNX_PLAN_GATE_ENFORCE`.
- **Eén schakelaar per controle in plaats van een profiel.** Afgewezen door operatorbesluit B-20261118-e0417fd9 (Q3). Zes schakelaars met zes waardensets geven combinaties die niemand toetst.
- **Fail-closed `enforced` bij een ontbrekende store.** Voor nu afgewezen: CI heeft geen store en zou dan op elke controle afbreken. Besluit 7 maakt het geval luid (ERROR met foutbron) en PR-Q3a meet het per ingang. Blijkt een productie-ingang de store structureel te missen, dan herstelt PR-Q3a de resolutie, niet het profiel.
