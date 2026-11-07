# ADR-050 - Agent-layout fase 1: de vlag `VNX_AGENT_LAYOUT`, de schaduwkopie en het context-manifest

**Status:** Aangenomen 07-11-2026 (T0, track `agents-als-map-fase-1`, PR-0, dispatch `dlv-a2a28c87d4f4`).
**Datum:** 2026-11-07
**Besloten door:** T0, op het plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/agents-als-map-fase-1.md` (revisie 2, na de codex-stoel op revisie 1), besluiten 1, 2, 3, 4 en 8, en de twee contracten onder "Contracten". De plan-gate van de track is `cleared`. Metingen staan op main `49e3112e2`; ADR-050 was daar vrij (`ls docs/governance/decisions/ | grep ADR-050` gaf niets; de hoogste was ADR-054).
**Cross-refs:** ADR-005 (eerst het grootboek; de ledgerlijst krijgt in PR-4b een amendement), ADR-007 (project_id op elke centrale rij), ADR-031 (agent-folders en `agents/<rol>/config.yaml` als SSOT), ADR-035 (receipt-v2), ADR-048 (skill-levering via de prompt), ADR-054 (het governance-record is geen eindregel). Inventaris `20261027-inv-agents-fase1` (gemeten op `164d48ad`).

## Context

De track `agents-als-map-fase-1` wil dat een worker alleen de skills van zijn rol ziet, en dat de fabric kan aantonen wat een worker werkelijk te zien kreeg. Fase 1 bouwt de meting en de declaratie. Fase 2 maakt de keuze bindend. Fase 3 voegt per-harness materializers en een canary toe.

Wat vandaag vaststaat, gemeten op `49e3112e2`:

- Niets van de vijf doelpunten is gebouwd. `VNX_AGENT_LAYOUT` en `context_manifest` komen in de getrackte boom niet voor. Geen van de 9 `agents/*/config.yaml` heeft een `skills:`-sleutel. `AgentConfig.skills` parseert hem wel (`scripts/lib/agent_resolver.py:46` en `:254`) en niemand leest hem.
- Skills komen alleen als prompttekst binnen: `skill_prefix.resolve_skill_dir` (`scripts/lib/skill_prefix.py:72`) en `build_skill_prefix` (`:147`). Er wordt niets naar de worktree geschreven.
- `bin/vnx:1328` zegt dat Claude Code geen symlinks volgt bij skill-ontdekking en dat het fabric daarom kopieert. Het symlink-ontwerp van de oude track `agent-skill-discovery-binding` is voor claude daarmee weerlegd.
- `.claude/skills/` heeft 27 getrackte skills, `skills/` 25. Een kopie in een ontdekte map kan een getrackt bestand overschrijven. `.git/info/exclude` wordt gedeeld tussen een linked worktree en de hoofdrepo, dus een uitsluiting daar is niet per worktree.
- De bruikbare naden bestaan: `ClaudeSubprocessAdapter.run` (`scripts/lib/envelope_adapters_claude.py:129`, `spawn_claude` op `:171`), `emit_dispatch_receipt` (`scripts/lib/governance_emit.py:78`, `ReceiptV2(` op `:301`, schrijfpad naar `t0_receipts.ndjson` op `:349`) en `harness_projects_dirs` (`scripts/lib/provider_spawns/harness_config_dir.py:270`). Er zijn vier aanroepers van `emit_dispatch_receipt`: `envelope_govern.py:777`, `provider_dispatch.py:1854`, `dispatch_cli.py:683` en `claude_gate_lane.py:418`.
- Er is al een vlag met bijna dezelfde naam. `VNX_AGENT_FOLDERS` schakelt de ADR-031-resolver (`scripts/lib/agent_resolver.py:22` en `:49-52`, default aan, rollback `=0`). De nieuwe vlag mag die niet raken en niet erop lijken.
- De harness-lanes (`deepseek-harness`, `glm`) starten `claude` met `--bare` (`BARE_FLAG` in `scripts/lib/provider_spawns/harness_config_dir.py:63`, de vlaggenlijst in `scripts/lib/provider_spawns/deepseek_harness_spawn.py:142`). Onder `--bare` slaat de CLI hooks over en leest hij geen OAuth. Daarom is elke afdwinging die op een hook leunt op die lanes een meetbaar nee.

Dit ADR legt vast wat fase 1 doet, wat het bewust niet doet en welke regels de twee nieuwe oppervlakken (de schaduwkopie en het context-manifest) bij elke latere wijziging houden.

Alles in dit ADR is deterministisch. Het zijn bestandskopieën, hashes, een vlagwaarde en een append naar een ledger. Er is geen modelaanroep, dus er is ook geen modelveiligheidsnet nodig. De enige delen die een model of een live CLI nodig hebben zijn de metingen, en die draait de T0 (zie besluit 8).

## Besluit

### 1. De vlag `VNX_AGENT_LAYOUT` en zijn twee waarden

`VNX_AGENT_LAYOUT` is een projectvlag. Fase 1 kent twee werkende waarden:

| Waarde | Betekenis in fase 1 |
|---|---|
| `off` (default) | Er verandert niets. Geen schaduwkopie, geen sidecar, geen veld in de receipt. De receipt is byte-gelijk aan die van vandaag. |
| `shadow` | De fabric materialiseert de skills van de rol als schaduwkopie en schrijft een context-manifest. Alleen waarneming: geen harness ontdekt de kopie en geen prompt verandert. |

- **De default staat in `scripts/lib/config_registry.py`** als `off`. Een `shadow`-default zou op elke dispatch in elke store een sidecar en een receiptveld schrijven. Dat is een gedragswijziging en geen schaduw. De operator zet `shadow` per project aan en draait het per project terug.
- **De resolver is `scripts/lib/agent_layout.py`** met `layout_mode()` en `is_shadow()`. Hij is de enige plek die de vlag leest.
- **Een andere waarde dan `off` of `shadow` wordt `off`.** Dat geldt voor `on` en voor elke onbekende, lege of verkeerd gespelde waarde. De resolver logt dan één WARNING en gaat door. Voor `on` luidt die: `VNX_AGENT_LAYOUT=on is fase 2; nu off`. Fail-closed zit hier in de richting van de vlag: wat de resolver niet positief herkent als `shadow` is `off`.
- **De vlag staat los van `VNX_AGENT_FOLDERS`.** Met `VNX_AGENT_LAYOUT=off` blijft `agent_folders_enabled()` `True`. Een test legt dat vast.

### 2. Waarom `on` in fase 1 als `off` leest

`on` is in fase 2 de waarde die de kopie in een door de harness ontdekte map zet, zodat een worker alleen zijn eigen skills ziet. Dat is een gedragswijziging in elke dispatch: de skill-lijst en de tokenkosten van de worker veranderen. Fase 1 heeft de meting nog niet die bepaalt of die wijziging veilig is (zie besluit 8). Zou `on` in fase 1 werken, dan kon iemand de ontdekte kopie aanzetten voordat bekend is wat een worker onder een klantmap nu al ziet.

Daarom bestaat `on` in fase 1 alleen als een waarde die veilig wordt afgewezen. Een operator die hem toch zet, krijgt `off` en één WARNING die zegt waarom. Er is geen stille downgrade.

### 3. Waar de rol-naar-skills-bron woont

De declaratie staat in `agents/<rol>/config.yaml:skills`. Dat bestand is al de SSOT van ADR-031 en `AgentConfig.skills` parseert de sleutel al. Er komt geen `skills:`-as in `.vnx/worker_permissions.yaml`. Dat zou een tweede waarheid naast de eerste zijn.

Fase 1 declareert skills voor `backend-developer`, `quality-engineer` en `research-analyst`. De spec van die levering (PR-2a) draagt `irreversible: true`, want `agents/` valt in `smart_router._IRREVERSIBLE_PREFIXES` onder `fleet-default` (`scripts/lib/smart_router.py:909`). Een wijziging in `agents/<rol>/config.yaml` bereikt andere projecten pas na een release.

### 4. De vouwing van `agent-skill-discovery-binding`

Fase 1 levert de declaratie, de schaduwkopie en de meting in de receipt. Dat dekt het DONE van de oude track niet. Die eist dat claude en codex de skills native ontdekken. Die helft hoort bij fase 2: `on`, de ontdekte kopie en de codex-indeling `.agents/skills`.

De oude track `agent-skill-discovery-binding` sluit dus op het bewijs van fase 2 en niet op dat van fase 1. Uit fase 1 is ook de codex-materialisatie gehaald (die was PR-2c). De kopie is bovendien bewust een kopie en geen symlink, om de reden in de context.

### 5. Contract: de schaduwkopie

Dit zijn de regels. Een wijziging aan de materializer die een van deze regels loslaat, vraagt een nieuw ADR.

- **Bestemming.** Alleen in de worktree van de dispatch, onder `.vnx-agent-skills/<rol>/<slug>/`. Dat is een map die geen harness ontdekt. Nooit in de hoofdcheckout. Nooit in `.claude/skills/` of `.agents/skills/`.
- **Uitsluiting uit git.** PR-2a zet `.vnx-agent-skills/` in de getrackte `.gitignore`. Die geldt in elke worktree. De materializer toetst vóór het schrijven met `git check-ignore -q .vnx-agent-skills/x`. Faalt die toets, dan schrijft hij niets en meldt hij `status: error, reason: not_ignored`. Een test eist dat de autocommit-scope van de subprocess-lane (`dispatch_paths.filter_paths`, `scripts/lib/dispatch_paths.py:90`) de map nooit meeneemt.
- **Levensduur.** De map ontstaat bij de start van de dispatch en verdwijnt met de worktree. Er is geen eigen opruiming en geen hergebruik tussen dispatches. Een bestaande map van een eerdere poging wordt eerst weggehaald. Alleen die map, en met een pad-toets dat hij binnen de worktree ligt.
- **Falen.** De schaduw faalt altijd open. Een fout geeft één WARNING en `status: error` met de reden in `manifest.json`, en de dispatch loopt door. Er is nooit een geweigerde dispatch door de schaduw.
- **Inhoud.** Per bestand staan pad, lengte en sha256 in `manifest.json`, plus de rol, de slug en de bronmap. Een slug die buiten de skillbomen wijst wordt geweigerd. Dat geldt voor `..`, voor een absoluut pad en voor een symlink naar buiten. De skillbomen zijn die van `skill_prefix.SKILL_TREES` (`scripts/lib/skill_prefix.py:39`).
- **Aanroep.** De materializer wordt één keer aangeroepen in `ClaudeSubprocessAdapter.run`, vóór `spawn_claude`. Niet in `dispatch_envelope.py`: dat is terrein van andere tracks.
- **Onder `off`** bestaat de map niet en is er geen aanroep.

### 6. Contract: het context-manifest in de receipt

Ook dit contract wijzigt alleen via een nieuw ADR.

- **Volgorde.** Eerst de sidecarregel in `<state_dir>/context_manifests.ndjson`: één `write` met `O_APPEND` en een `fsync`. Dan de receipt met `{"schema":1,"status":"ok|partial","sha256":"<hash van die regel>"}`. Faalt de sidecar, dan krijgt de receipt `{"schema":1,"status":"error","reason":"ledger_write_failed:<Class>"}` en geen hash. Een receipt noemt dus nooit een hash die niet in de sidecar staat. Dit volgt ADR-005: eerst het grootboek, dan het afgeleide.
- **Projectidentiteit.** `project_id` komt uit `project_scope.resolve_stamp_project_id`, hetzelfde als in de receipt. Een regel met een ander `project_id` dan de store is een fout die de lezer (PR-4c) telt. Er is geen SQLite-tabel voor het manifest, dus de composite-key-eis van ADR-007 is hier niet aan de orde.
- **Herhalingen.** De sidecar is idempotent op `(dispatch_id, session_id, sha256)`. Een tweede emit met dezelfde drie schrijft niets. Een nieuwe poging van dezelfde dispatch heeft een andere sessie en krijgt een eigen regel.
- **Nooit inhoud en nooit een e-mailadres.** Alleen paden, typen, lengtes en hashes.
- **Onder `off`** is de receipt byte-gelijk aan die van vandaag en bestaat het sidecarbestand niet.
- **Het veld** heet `context_manifest` in `scripts/lib/receipt_schema.py`. Het wordt alleen gestempeld als het niet `None` is en staat niet in `IDEMPOTENCY_FIELDS`.
- **Een eigen bestand, nooit een regel in `t0_receipts.ndjson`.** Een manifestregel daar zou door `_receipt_exists_for_dispatch` als "receipt bestaat al" gelezen worden. Dan onderdrukt hij de echte ReceiptV2. Het origineel weegt bovendien 3 tot 5 KB per regel, en elke ledger-lezer betaalt dat per receipt.
- **Statussen.** De receipt kent `ok`, `partial` en `error`. De sidecar kent daarnaast `unavailable` en `unsupported`. Een gesloten set redenen dekt de rest: `no_session_id`, `transcript_not_found`, `provider_not_supported:<provider>` en `extract_failed:<Class>`. De gate-seats van `dispatch_cli` en `claude_gate_lane` geven geen `session_id` mee en komen dus op `unavailable/no_session_id`. Dat is een meting en geen fout. `provider=kimi` geeft `unsupported` zonder het bestandssysteem te raken.

### 7. Wat `on` in fase 2 wordt

In fase 2 wordt `on` een derde, werkende waarde. Dan zet de materializer de kopie in een map die de harness ontdekt (de ontdekte kopie), en voor codex komt de indeling `.agents/skills` erbij. De gekozen set wordt dan bindend: de binding komt in de permit-digest naast rol, account of klant en manifest-hash, en de deur kan een dispatch weigeren die buiten zijn set valt.

Dat vraagt dingen die fase 1 niet heeft. Het vraagt het besluit tussen een vaste set en een catalogus. Het vraagt de behandeling van de 27 getrackte `.claude/skills/`-mappen en de gedeelde `info/exclude`. En het vraagt de metingen van besluit 8. Pas als die er zijn, hoort `on` te bestaan. Tot dan blijft de resolver `on` afwijzen.

### 8. De metingen blijven bij de T0

Een bouwer kan geen `claude -p` starten: `scripts/hooks/pretooluse_block_raw_claude_spawn.sh` blokkeert het. Daarom draait de T0 de metingen met de probe-scripts uit PR-5 en PR-6, na de merge van die PR's. Een bouwer draait ze nooit.

Elke cel heeft drie mogelijke uitkomsten: ja, nee, of niet gemeten met de reden. Een doelpunt is pas gemeten als zijn beslissende cel ja of nee gaf. Blijft een beslissende cel op "niet gemeten", dan sluit de track niet. De uitkomsten landen getrackt in `docs/investigations/` en niet in `claudedocs/`, want `claudedocs/` staat in `.gitignore` en de evidentie moet op een commit bestaan.

De drie metingen zijn:

- **Punt 3:** ziet een worker onder een klantmap een bovenliggende `CLAUDE.md` of de map `projects`?
- **Punt 4:** wordt `allowed_paths` echt afgedwongen? Vandaag is het antwoord nee. De hook `scripts/hooks/pretooluse_worker_scope_enforce.py` is niet geregistreerd in `.claude/settings.json`, geen PreToolUse-matcher dekt `Write|Edit|MultiEdit`, `VNX_ENFORCE_WORKER_PERMISSIONS` staat default uit (`worker_permission_enforcement_enabled`, `scripts/lib/worker_permissions.py:388`) en `agents/<rol>/config.yaml:isolation.allowed_paths` heeft geen lezer. Op de `--bare`-lanes vuurt een hook bovendien nooit.
- **Punt 5:** de `--bare`-vlag wordt hermeten bij elke CLI-update. Een getrackt record in `docs/operations/CLAUDE_CLI_MEASUREMENTS.md` en een `vnx doctor`-check dwingen dat af. Alleen een regel met de hash van de probe-uitvoer telt als meting.

De probes draaien op het eigen account (`~/.claude`), want een wegwerp-`CLAUDE_CONFIG_DIR` heeft geen login. Ze draaien met de cwd in een probe-boom onder `$TMPDIR` en met een wegwerpstore. Sentinels staan alleen in die probe-boom. Uit het transcript worden alleen paden, typen, lengtes en hashes gelezen.

## Gevolgen

- **Met de vlag uit verandert er niets.** Dat is de eis van elke PR in de track en een test per PR bewijst het.
- **Twee oppervlakken zijn nu vastgelegd.** De map `.vnx-agent-skills/` en het bestand `context_manifests.ndjson` hebben hun regels. Een PR die er een loslaat, wijzigt dit ADR.
- **`.claude/settings.json` en de hook-timeouts.** Sinds #2458 staan alle hook-timeouts in `.claude/settings.json` in seconden en de T0-contextguard is daaruit verdwenen. De hook-registratie van PR-3 schrijft dus `timeout: 5` (seconden) en niet `5000`, en `tests/test_fabric_settings_hooks.py` pint die eenheid. De matcher is `Write|Edit|MultiEdit|Bash`. Met de vlag uit moet `evaluate()` `allow` geven zonder bestandstoegang, ook zonder dispatch-omgeving, zodat een T0-sessie niet geraakt wordt.
- **Volgorde op het receipt-pad.** `receipt_schema.py` en `governance_emit.py` krijgen nooit twee bouwers tegelijk: eerst K-1 `dlv-164797402583`, dan RS-3 `dlv-75df89432a80`, dan PR-4b. De ADR-005-amendement op de ledgerlijst (`docs/governance/decisions/ADR-005-ndjson-audit-ledger-primary.md:28`) en `docs/core/11_RECEIPT_FORMAT.md` volgen in PR-4b. Dit ADR wijzigt ze zelf niet.
- **Risico op de lezers.** Een nieuwe lezer van `context_manifests.ndjson` moet regels met een vreemd `project_id` tellen en nooit stil negeren, en moet een receipt-hash zonder sidecarregel als fout lezen.
- **Wat dit ADR niet doet.** Het bouwt niets. Het wijzigt geen code, geen config en geen hook. De leveringen PR-1 tot en met PR-6 doen dat, elk met een eigen test die op de oude code op gedrag faalt.

## Verworpen alternatieven

- **Default `shadow`.** Verworpen: schrijft overal een sidecar en een receiptveld zonder dat iemand het koos. Zie besluit 1.
- **`on` laten werken in fase 1.** Verworpen: de ontdekte kopie verandert elke dispatch, en de meting die bepaalt of dat veilig is, bestaat nog niet. Zie besluit 2.
- **Symlinks in plaats van kopieën.** Verworpen: Claude Code volgt ze niet bij skill-ontdekking (`bin/vnx:1328`).
- **De kopie in `.claude/skills/`.** Verworpen voor fase 1: 27 getrackte skills kunnen overschreven worden en `.git/info/exclude` is gedeeld met de hoofdrepo. De ontdekte kopie hoort bij `on` in fase 2.
- **Een `skills:`-as in `.vnx/worker_permissions.yaml`.** Verworpen: een tweede waarheid naast `agents/<rol>/config.yaml`. Zie besluit 3.
- **Het manifest inline in de receipt.** Verworpen: 3 tot 5 KB per regel voor elke lezer, en een eigen regel in `t0_receipts.ndjson` onderdrukt de echte receipt. Zie contract 6.
- **Een SQLite-tabel voor het manifest.** Verworpen: het ledger is NDJSON (ADR-005) en een tabel zou ADR-007 invoeren zonder dat er een query is die het vraagt.
