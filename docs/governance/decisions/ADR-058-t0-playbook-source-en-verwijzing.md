# ADR-058 - De bron van het T0-playbook, het verwijzingsblok, aanwezig tegen geladen, de budgetscope en het bewijscontract

**Status:** Aangenomen 08-11-2026 (T0, track `orchestrator-skills-port-codex-kimi`, SP-0).
**Datum:** 2026-11-08
**Besloten door:** T0, op het plan `claudedocs/2026-11-07-naar-1-9/plannen/orchestrator-skills-port-codex-kimi.md` (revisie 2, besluiten 1 tot en met 7) en de plan-gate van 08-11, die de tiebreaker met `START` heeft vrijgegeven op één bindende voorwaarde: dit ADR specificeert het broncontract volledig en geeft de toestand `loaded` een concrete bewijsbron met een benoemde lezer. ADR-058 was vrij op `origin/main` `94ecddd1f`: de hoogste bestaande ADR is ADR-055. 056 en 057 zijn door open plannen gereserveerd.
**Cross-refs:** ADR-007 (project_id in elke sleutel), ADR-044 (de provider-schakelaar: fail-closed), ADR-050 (agent-layout fase 1), ADR-051 (pin op runtime en releaseverificatie), ADR-055 (heartbeat-contract met vijf toestanden).

## Context

De verplichte T0-handleiding, het playbook, hoort uit één provider-neutrale, versiegebonden bron te komen. Vandaag is dat niet zo. Alle regels hieronder zijn gemeten op `origin/main` `94ecddd1f`.

**De hook injecteert de verkeerde kopie.** `hooks/sessionstart.sh:432` zet `_T0_SKILL_MD` op `$PROJECT_ROOT/.claude/skills/t0-orchestrator/SKILL.md`. `hooks/sessionstart.sh:433-437` leest die kopie of meldt "Playbook file NOT FOUND". De geladen kopie is 6.841 bytes. De kopie die de fabric meelevert, `skills/t0-orchestrator/SKILL.md`, is 8.061 bytes. De geïnstalleerde release `~/.vnx-system/current/skills/t0-orchestrator/SKILL.md` bestaat en is de bron waar een consumer op kan leunen. De hook heeft de resolver voor de scripts-root al in huis: `hooks/sessionstart.sh:96-105` probeert hook-relatief, dan `$VNX_HOME/scripts`, dan `$HOME/.vnx-system/current/scripts`.

**De rol heeft geen bronverwijzing op de provider-oppervlakken.** `bin/vnx:1525` (`cmd_role_sync`) schrijft het blok `<!-- VNX:BEGIN T0-ROLE -->` (`bin/vnx:1687-1688`) in `AGENTS.md` en `GEMINI.md` (lus `bin/vnx:1692`, write `bin/vnx:1716`) en verder niets. `git grep T0-PLAYBOOK origin/main` geeft nul treffers. De rol wijst zelf op `.claude/skills/t0-orchestrator/SKILL.md` (`.claude/terminals/T0/role-orchestrator.md:13` en `:18`) en noemt Kimi onjuist bij `GEMINI.md` (`:154`). Het geleverde playbook zegt "T0 itself uses `CLAUDE.md`" (`skills/t0-orchestrator/SKILL.md:58`). De rol is 33.659 bytes. Het Kimi-plafond uit de kimi-cli-changelog is 32.768.

**Aanwezig en geladen zijn niet te scheiden.** `scripts/fleet_role_drift.py:105` pint `SKILL_REL` op de `.claude/skills`-kopie. `scripts/fleet_role_drift.py:209` (`_hook_injects_skill`) greppt op een substring die beide bomen matcht. `scripts/fleet_role_drift.py:265` (`measure_project`) kent geen as voor het playbook en geen budget. `scripts/commands/t0_role_audit.sh:344-351` zet `PLAYBOOK-MECHANISM-GAP` onvoorwaardelijk zodra een providerbestand de rolmarker draagt. Er is geen toestand "verwijzing aanwezig, laden ongemeten" en geen toestand "bron ontbreekt". Een meter die een verwijzing als opgelost leest, sluit het gat op aanwezigheid.

Dit ADR legt het contract vast dat de volgende PR's van de track implementeren: SP-3 (de module en de sync), SP-8 (rol en playbook), SP-2 (de hook), SP-4 (de driftmeter), SP-5 (de audit) en SP-7 (de budgetmeter). Het ADR komt eerst. Elke lezer wordt dan tegen dezelfde tekst gebouwd en niet tegen elkaars code.

## Besluit

**Het playbook komt uit één vaste resolutievolgorde van vijf bronnen. Een verwijzingsblok noemt de gekozen bron met pad, `source_id` en `sha256`. Elke lezer berekent de versheid opnieuw. Alleen een geldig bewijsrecord mag `loaded` geven.** Er zit geen model in dit pad: het zijn paden, een hash, een toestandsmachine en een byte-telling. Elke uitkomst is op elke consumer reproduceerbaar.

### 1. Het ADR-nummer

Het nummer wordt op het vuurmoment toegewezen als het laagste nummer dat op geen enkele ref gemerged is en in geen enkel plan staat. Op `94ecddd1f` is dat 058. Het plan koos in revisie 1 nog 056, maar elf open plannen claimen 056 en één plan claimt 057. Hergebruik van die nummers botst op de dag dat een van die plannen vuurt.

### 2. De resolutievolgorde en de identiteit van elke bron

De canonieke bron is de `skills/`-boom die de draaiende fabric meelevert. De volgorde is vast. De eerste stap waarvan het bestand bestaat wint.

| stap | bron | `source_id` | `degraded` |
|---|---|---|---|
| (a) | `$_VNX_SCRIPTS_ROOT/../skills/t0-orchestrator/SKILL.md` | `fabric` | `false` |
| (b) | `$VNX_HOME/skills/t0-orchestrator/SKILL.md` | `vnx_home` | `false` |
| (c) | `$HOME/.vnx-system/current/skills/t0-orchestrator/SKILL.md` | `installed` | `false` |
| (d) | `$PROJECT_ROOT/skills/t0-orchestrator/SKILL.md` | `project` | `true` |
| (e) | `$PROJECT_ROOT/.claude/skills/t0-orchestrator/SKILL.md` | `legacy` | `true` |

Stap (a) dekt twee gevallen. In de fabric-repo is dit `<repo>/skills`. Op een geïnstalleerde consumer is het `~/.vnx-system/current/skills`, dezelfde boom als de release. Alleen (d) en (e) zijn `degraded: true`. Het project-eigen exemplaar is niet bewezen uit de draaiende release en de Claude Code-detectiekopie (e) mag driften. Een consumer zonder eigen `skills/` resolveert dus via (a) naar de geïnstalleerde boom en meldt `degraded: false` met `source_id: fabric`. Een project met een eigen `skills/` meldt `degraded: true` met `source_id: project`.

**Identiteit.** Het verwijzingsblok bewaart het volledig geresolveerde echte pad (symlinks opgelost), de `source_id` van de stap die matchte en de `sha256` van de bytes van dat bestand. De alias `current` wordt nooit bewaard. Een flip van `current` verandert de resolutie maar niet het bewaarde pad, en dat verschil is precies wat een lezer moet kunnen zien.

### 3. Versheid: drie toestanden

Elke lezer herberekent de `sha256` van het bewaarde bestand en resolveert de volgorde van vandaag. Daarna vergelijkt hij.

| toestand | voorwaarde |
|---|---|
| `present` | bewaard pad en `source_id` zijn gelijk aan de resolutie van vandaag en de `sha256` klopt |
| `stale_sha` | de `sha256` van het bestand wijkt af van de bewaarde waarde |
| `stale_source` | het bewaarde pad of de `source_id` wijkt af van de resolutie van vandaag |

Een oude versie waarvan pad en hash onderling kloppen is na een `current`-flip nooit vers. Het bewaarde pad wijst dan naar een oudere release terwijl de resolutie naar de nieuwe wijst, en dat is `stale_source`. Een ontbrekend, onleesbaar of misvormd veld in het blok is nooit `present`. Een afwezig blok is `absent`.

### 4. Alle vijf bronnen ontbreken

Dit is de bindende voorwaarde van de plan-gate. Het gedrag is voor elke consumer vastgelegd.

- **De hook** injecteert geen body en zet één expliciete regel: `T0 PLAYBOOK SOURCE MISSING: no playbook in sources (a)-(e); do not orchestrate from memory`, gevolgd door de geprobeerde paden. De exit blijft 0. De hook is fail-soft, zoals de huidige NOT FOUND-regel op `hooks/sessionstart.sh:437`.
- **De meter** (`scripts/fleet_role_drift.py`) meldt `playbook.status: "missing"`, `source_missing: true`, `probed: [...]` en `loading: "unverified"`. Het project telt als behind en de exit is 1.
- **De audit** (`scripts/commands/t0_role_audit.sh`) meldt `PLAYBOOK-SOURCE-MISSING` en houdt `PLAYBOOK-MECHANISM-GAP`. Hij meldt nooit `POINTER-ONLY` en is nooit schoon.

### 5. Eén implementatie en één spiegel

`scripts/lib/t0_playbook.py` (SP-3) is het contract: `resolve()`, `sha256_of()`, `render_pointer()`, `parse_pointer()` en `pointer_state()`. SP-4, SP-5 en SP-2 lezen die module. De hook is bash en mag niet van een Python-import afhangen. Hij houdt daarom een shell-spiegel van alleen de resolutie, hergebruikt `_vnx_try_scripts_root` (`hooks/sessionstart.sh:96-105`) en krijgt in SP-2 een pariteitstest over vijf fixtures: fabric, `vnx_home`, installed-only, project-only en alles-ontbreekt. Hook en module moeten in elke fixture hetzelfde pad kiezen. Twee resoluties die uit elkaar lopen zijn precies de drift die deze track wegneemt.

### 6. Het blok is een verwijzing en sluit het gat nooit

Het blok in `AGENTS.md` en `GEMINI.md` bevat nooit de body van het playbook. Het bevat de markers `<!-- VNX:BEGIN T0-PLAYBOOK -->` en `<!-- VNX:END T0-PLAYBOOK -->` met de velden `path`, `source_id` en `sha256`. Een kopie van de body zou de rol verder over het Kimi-plafond duwen en maakt van één bron twee kopieën.

Een verwijzing sluit `PLAYBOOK-MECHANISM-GAP` nooit. Een actuele verwijzing geeft de audit `POINTER-ONLY` naast het gat, en `loading: "unverified"` is een waarde en geen ontbrekend veld. Aanwezig betekent dat de bytes er staan. Geladen betekent dat de provider ze in de sessie heeft gelezen. Alleen het tweede bewijst dat de T0 het playbook heeft.

### 7. Het bewijscontract voor `loaded`

`loaded` heeft één bewijsbron en twee benoemde lezers.

**Het bestand.** `<VNX_STATE_DIR>/playbook_loading.ndjson`. Het is runtime-state, wordt nooit gecommit, is geen receiptveld en geen databasetabel.

**Het record.** Eén JSON-regel per waarneming:

```json
{"schema": 1, "provider": "claude", "project_id": "<id>", "source_id": "fabric",
 "path": "<abs>", "sha256": "<hex64>", "observed_at": "<ISO8601Z>", "evidence": "<wat de producent las>"}
```

`provider` is `claude`, `codex` of `kimi`. `source_id` is een van de vijf waarden uit besluit 2.

**De producent.** Een probe die een door de provider zelf geschreven artefact leest. Voor Claude is dat het sessietranscript via `locate_session_transcript` in `scripts/lib/token_harvest.py`. Voor Codex is het de C5-meting en voor Kimi een live sessie. De probe is de vervolgtrack **F1** (`playbook-loading-meting`). Deze track levert de lezer en de tests, niet de probe.

**De geldigheid.** Een record geldt alleen voor zijn eigen `provider`. Het geldt als `project_id` gelijk is aan het gemeten project en `source_id`, `path` en `sha256` gelijk zijn aan de resolutie van vandaag. Elke wijziging van bron of hash maakt het record ongeldig en zet `loaded` terug op `unverified`. Een tweede vervaldatum is niet nodig.

**De lezers.**

| lezer | PR | gebruik |
|---|---|---|
| `scripts/fleet_role_drift.py` | SP-4 | `playbook[provider].loading` in het rapport |
| `scripts/commands/t0_role_audit.sh` | SP-5 | het gat sluit alleen voor de provider met een geldig record |

De bewijslezer woont in `scripts/lib/playbook_loading_evidence.py` (SP-4). SP-5 leest die module.

**Fail-closed.** Elk van deze gevallen geeft `unverified` en het gat blijft open: het bestand ontbreekt, de regel ontbreekt, de regel is `None`, de JSON is misvormd, `provider` is onbekend, `sha256` is geen 64 hex-tekens, `observed_at` is niet te parsen, `project_id` wijkt af. Elk geval krijgt een eigen test.

### 8. Het budgetcontract

De budgetmeter (SP-7) is een referentiemeting en geen poort.

- **Traversal.** Van `git rev-parse --show-toplevel` tot en met `<repo>/.claude/terminals/T0` (de T0-cwd). Op elk niveau telt de meter `AGENTS.md` en `.kimi/AGENTS.md`. Dat zijn de twee namen die de kimi-cli 1.46.0-changelog noemt.
- **Sommen.** `files: [{path, bytes, present}]`, `per_file_max` en `aggregate_bytes` (de som van de aanwezige bestanden).
- **Status.** `exceeded` als `per_file_max` of `aggregate_bytes` boven 32.768 ligt. `ok` als beide op of onder 32.768 liggen. `unmeasured` als geen enkel bestand aanwezig is of een bestand onleesbaar is. `unmeasured` leest nooit als `ok`.
- **Herkomst.** `cap: 32768`, `cap_source: "kimi-cli 1.46.0 CHANGELOG.md:177"` en `claim: "reference_metric_only"`. Het regelnummer is de meting van het plan op het kimi-cli-pakket en geen meting in deze repo. De changelog belooft prioriteit per bestand onder een budget. Of het plafond per bestand of over het geheel geldt is niet gemeten. De meter telt daarom beide en noemt zichzelf een conservatieve referentie.
- **Exit.** De `budget`-sectie verandert de exitcode niet: 0 current, 1 behind, 2 onleesbare canon of registry. Een ontbrekend playbook is al behind via de `playbook`-as.

De rol zelf is 33.659 bytes en dus al boven 32.768 vóór er een verwijzingsregel bij komt. Dat is een feit over de bron en geen verwachting over het afkapgedrag van Kimi.

### 9. Wat de track claimt en wat niet

De track sluit op vier dingen: (i) het verplichte playbook komt uit een provider-neutrale, versiegebonden bron, (ii) de rol is op het `AGENTS.md`-oppervlak bereikbaar via een verwijzing, (iii) de meters scheiden aanwezig van geladen en (iv) het broncontract staat op schrift en is getest.

De track claimt **niet** dat Codex of Kimi het playbook ook echt laden. Hij claimt ook niet dat de rol onder het Kimi-plafond komt. Die twee uitkomsten hebben een eigenaar en een eigen meting:

| id | track | klaar als |
|---|---|---|
| F1 | `playbook-loading-meting` | per provider één geldig record in `playbook_loading.ndjson` uit een door de provider geschreven artefact, of per provider een expliciete "niet gemeten: reden". De meter meldt per provider `loaded` of `unverified`. |
| F2 | `t0-rol-onder-32kib` | de canonieke rol plus verwijzing past in 32.768 op de T0-`AGENTS.md`-keten: `budget.status == "ok"` na een verse sync, met het getal. |

De sluitrapportage van de track noemt F1 en F2 en herhaalt deze niet-claims. Zonder die registratie sluit de track niet.

## Gevolgen

- SP-3 en SP-8 wachten op dit ADR. SP-3 implementeert het pad-plus-hash-contract en SP-8 schrijft dezelfde bron in de rol en het playbook. Zonder ADR ontstaat de incoherentie die de plan-gate verwierp.
- SP-2 injecteert het bestand dat SP-8 corrigeert (`skills/t0-orchestrator/SKILL.md:58`). Een Claude-only regel in de geïnjecteerde body zou de lock terugbrengen die de track wegneemt. SP-2 hangt daarom aan SP-3 en SP-8.
- Een `current`-flip bij een release maakt elk bewaard pad in een verwijzingsblok `stale_source`. Daarna is een `vnx role sync --apply` nodig. Dat is operatorwerk op de consumers en geen onderdeel van een PR.
- Een lezer die het contract niet kan toepassen valt altijd terug op `unverified`, `missing` of behind. Een ontbrekend bewijs is nooit een groen oordeel.
- Wat dit ADR niet doet: geen code, geen wijziging van `~/.kimi/config.toml`, geen receiptveld, geen reparatie van `.claude/skills`.
