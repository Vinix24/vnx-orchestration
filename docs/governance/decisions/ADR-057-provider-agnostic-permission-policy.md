# ADR-057 - Eén permissiepolicy als bron van waarheid, gebonden aan elke dispatch, met een flip die een poort is

**Status:** Aangenomen 08-11-2026 (T0, track `provider-agnostic-permissions`, P1).
**Datum:** 2026-11-08
**Besloten door:** T0, op het plan `claudedocs/2026-11-07-naar-1-9/plannen/provider-agnostic-permissions.md` (revisie 2, plan-gate-bevindingen B1 tot en met B7). ADR-057 was vrij op `origin/main` `94ecddd1f`; de hoogste bestaande ADR is ADR-055. Nummer 056 is door T0 toegewezen aan `agent-skill-discovery-binding` R1 en 058 aan `deur-afgedwongen`.
**Cross-refs:** ADR-012 (worker-permissieprofielen), ADR-044 (de provider-schakelaar: twee lagen), ADR-050 (agent layout fase 1), ADR-054 (het governance-record van de envelope is geen eindregel), ADR-055 (processor-liveness).

Deze ADR legt vast. Hij ontwerpt niets nieuws: de vorm van de policy, de matrix en de keten staan in het plan, en de acht PR's van de track (P2 tot en met P8) bouwen ertegen.

## Context

Er zijn drie oppervlakken die iets over toestemming zeggen, en er is geen object dat ze samen draagt.

- `.vnx/worker_permissions.yaml` (9 profielen) wordt gelezen door `resolve_worker_profile` (`scripts/lib/worker_permissions.py:455`) en vertaald naar CLI-vlaggen door `build_claude_scope_args` (`scripts/lib/worker_permissions.py:621`). Dit is het enige oppervlak dat vandaag een grens bouwt.
- `agents/<rol>/config.yaml:isolation` wordt als rauwe dict geparseerd (`scripts/lib/agent_resolver.py:44` en `:252`). Geen enkele lezer gebruikt de sleutels `allowed_paths` en `denied_paths` om een grens te zetten.
- `.vnx/governance_profiles.yaml` bevat folder-globs en reviewgewicht. Dat is een andere vraag en geen grens.

De handhaving staat uit en dat staat nergens waar een operator het leest. `worker_permission_enforcement_enabled` (`scripts/lib/worker_permissions.py:388`) geeft `False` zolang `VNX_ENFORCE_WORKER_PERMISSIONS` niet gezet is (`scripts/lib/worker_permissions.py:421-423`). De PreToolUse-hook is wel geregistreerd (`.claude/settings.json:26-36`, matcher `Write|Edit|MultiEdit|Bash`) en doet niets zolang de handhaving uit staat: de kop van `scripts/hooks/pretooluse_worker_scope_enforce.sh` (regel 24) zegt "default OFF. Unset/0 → no-op". Twee vlootdocumenten zeiden het tegendeel, elk op een eigen manier:

- `scripts/lib/providers/provider_constraints.yaml`, regel `worker-permission-enforcement-on`, schreef dat de handhaving "defaults ON since 2026-08-15" staat. Dat is onwaar: de flip van 15-08 is teruggedraaid.
- `docs/core/LOCKS_AND_RELEASES.md` (rij over fijnmazige write-scope-handhaving) schreef dat de hook niet in `.claude/settings.json` staat. Dat is onwaar sinds de registratie.

Een bron van waarheid die over de vlootstandaard liegt is de duurste vorm van drift. Daar komt bij dat de identiteit van een dispatch niets weet van de grens waaronder hij liep. `canonical_dict()` (`scripts/lib/dispatch_plan.py:199`) bevat geen permissie-identiteit en `digest()` (`scripts/lib/dispatch_plan.py:239`) is een hash over precies die dict. De permit (`ExecutionPermit`, `scripts/lib/dispatch_internal.py:65`) vergelijkt alleen `plan_digest`, in-process. Een wijziging in `.vnx/worker_permissions.yaml` verandert daarom geen digest, geen spec en geen receipt. De merge-poort kent geen permissiescope: `run_gate_checks` (`scripts/pre_merge_gate.py:1282`) heeft geen enkele permissiecheck.

Per rijstrook betekent "een grens" iets anders. Claude headless, claude subprocess, deepseek-harness en glm-harness lopen via één naad (`_build_worker_scope_args`, `scripts/lib/subprocess_adapter.py:112`). Kimi heeft alleen een `^Shell$`-hook en `--yolo` (`scripts/lib/provider_spawns/kimi_spawn.py:517`). Codex kent een OS-sandbox of niets (`scripts/lib/provider_spawns/codex_spawn.py:375-379`). De harness-lanes kunnen per constructie geen hook dragen: `scripts/lib/provider_spawns/harness_config_dir.py:253-254` weigert een settings.json met `hooks`.

Fail-closed op bewijs en default-OFF op handhaving spraken elkaar tegen in revisie 1. `resolve_worker_profile` geeft voor een rol die wel in het agentregister staat maar niet in `worker_permissions.yaml` een fallbackprofiel met `is_fallback=True`. `UnknownRoleError` (`scripts/lib/worker_permissions.py:219`) volgt alleen als de rol in geen van beide registers staat. Fail-closed op "geen profiel" breekt bestaande rollen. Fail-open maakt een flip op bewijs onmogelijk.

## Besluit

**Er komt één `PermissionPolicy` met één `policy_digest`. Die digest gaat vier keer mee in de keten van dispatch tot merge. De handhaving blijft default OFF en wordt alleen door een poort met een menselijk besluit omgezet.** Er zit geen model in dit pad: het is parsen, hashen en vergelijken.

### 1. Eén policy, twee bronnen

`PermissionPolicy` wordt samengesteld uit precies twee bronnen:

| Bron | Waar | Verplicht |
|---|---|---|
| `role_profile` | `.vnx/worker_permissions.yaml` | ja |
| `agent_isolation` | `agents/<rol>/config.yaml:isolation` | nee, `{}` is geldig |

`policy_digest` is een hash over de samengestelde policy en over niets anders. `governance_profile` (`.vnx/governance_profiles.yaml`) is een **uitgesloten bron** en wordt als `excluded_source` vastgelegd. Een digest over precies de bronnen die een grens zetten is falsifieerbaar. Reviewgewicht is geen grens, en opnemen maakt de digest onfalsifieerbaar. Heroverwegen kan alleen als een reviewgewichtsregel ooit een tool blokkeert.

### 2. De digest gaat vier keer mee

1. De spec: veld `permission_policy_digest`.
2. De plan-digest: via `canonical_dict()`, dus ook in de permit.
3. Het materialisatierecord per rijstrook: welke vlaggen, welke bron, welke digest.
4. De merge-poort: herberekening op de merge-base en vergelijking.

Eén plek is een bewering. Vier plekken is een keten die je kunt breken en dus kunt testen. De spec is de autoriteit en het CFX-1-manifest is een restant: de merge-poort resolvet de spec op Dispatch-ID, niet op een mapglob (`_find_dispatch_for_pr`, `scripts/pre_merge_gate.py:1105`, globt platte `*.md`).

### 3. Geen `role` in de permit-identiteit

`role` blijft buiten `canonical_dict()`. Dat is gepind door `tests/test_dispatch_plan.py` (`test_canonical_dict_excludes_role`, regel 507). Een rolnaam is een label. De digest is de grens: twee rollen met hetzelfde profiel delen de digest, en een profielwijziging verandert de identiteit van elke dispatch eronder.

### 4. Twee lagen, elk met een eigen artefact

| Laag | Wat | Bewijs |
|---|---|---|
| Laag 0 | agnostische vloer: git-grens, zandbak, cwd | het materialisatierecord van de rijstrook (codex `--sandbox`, kimi `^Shell$`-hook, claude `--permission-mode`) |
| Laag 1 | per-harness vlaggenset | de uitkomst van `build_claude_scope_args` |

De lagen falen onafhankelijk. In één veld gepropt wordt een groene laag 0 een alibi voor een gatenkaas-laag 1.

### 5. Capaciteitsmatrix met drie waarden

Per (rijstrook, capability): `supported`, `degraded` of `unsupported`. `degraded` is: de rijstrook kan het met een andere mechaniek en ander bewijs (codex filesystem via `--sandbox`). `unsupported` is: de rijstrook kan het niet uitdrukken (kimi per-tool deny, hooks op de harness-lanes). Een onbekende harness is `unsupported`, nooit "allow". De opzoeking heeft een gesloten enum en een onbekende sleutel faalt.

Een policy die een restrictie eist die de rijstrook `unsupported` heeft, weigert de bouw met `permission-capability-unsupported` zodra `worker_permission_enforcement_enabled()` waar is. Onder OFF loopt de dispatch en wordt het gat als `unsupported` geboekt. Het wordt nooit stil ingeslikt. Of kimi een native tools-deny accepteert is niet gemeten en staat daarom als `unsupported` in de matrix.

### 6. Twee assen: bewijs is fail-closed, handhaving is default OFF

- **Bewijs is altijd fail-closed.** Onleesbare bron, kapotte YAML, onbekende bronsleutel, rol in geen enkel register, onbekende harness en een mislukte digest-herberekening weigeren. Ontbrekend, `None` of leeg is nooit een pass.
- **Handhaving blijft default OFF** en volgt alleen de flip-poort van punt 7.
- **`is_fallback=True` is een geregistreerde degradatie**, geen ontbrekende lezing. Het is toegestaan onder OFF, blokkeert de flip voor die rol en blokkeert de merge (`SKIPPED_UNVERIFIED`, `scripts/pre_merge_gate.py:82`) onder ON. Een lege maar aanwezige `isolation` en een afwezige `governance_profile` blokkeren niet.

### 7. De flip is een poort, geen codepad

Niets in de code zet `VNX_ENFORCE_WORKER_PERMISSIONS`: geen env-schrijver, geen aanpassing van `.claude/settings.json`, geen autoflip in de T0-rol. Het postuurrapport (P8) is read-only. De flip vereist allemaal tegelijk:

1. per rijstrook een groene kwalificatie;
2. nul fallback-rollen;
3. nul `unsupported` restricties die een actieve policy eist;
4. N opeenvolgende dispatches met digest-overeenkomst bij merge (aanbeveling: 20, minstens 3 per rijstrook);
5. een expliciet besluit van Vincent, vastgelegd in de track.

Besluit 4 van 07-11 geeft T0 de uitrol van een release. Het geeft T0 niet de bevoegdheid om de permissiegrens zonder expliciete stap aan te zetten.

### 8. Scope van de track

SSOT, binding, bewijs en flip-poort. De flip zelf valt erbuiten. `agents-als-map-fase-3` is **geen voorwaarde meer**. Reden: de profielbron bestaat al en `isolation` is optioneel (`scripts/lib/agent_resolver.py:252` accepteert `{}`), dus de policy is ook zonder fase 3 volledig te binden. De `depends`-regel op de track vervalt (T0-actie).

Buiten scope: `envelope_adapters_claude.py`, `governance_emit.py`, `receipt_schema.py`, `provider_dispatch.py` (erft de naad), `dispatch_envelope.py`, `agents/*/config.yaml` en `.vnx/worker_permissions.yaml` (wordt alleen gelezen). Het wijzigen van de codex-sandboxdefault is een flip-stap met Vincent en geen PR in deze track.

### 9. De twee onware regels verdwijnen, de geschiedenis blijft

`worker-permission-enforcement-on` in `provider_constraints.yaml` behoudt zijn `id` (de loader keyt op die string) en beschrijft de gemeten werkelijkheid: default OFF, hook geregistreerd en inert, flip alleen via de poort van deze ADR. Een `history`-sleutel bewaart de oude bewering en de datum van de correctie (2026-11-08). De rij in `docs/core/LOCKS_AND_RELEASES.md` zegt dat de hook geregistreerd is en inert onder OFF. `tests/test_permission_ssot_truthfulness.py` pint beide kanten.

## Gevolgen

- **Positief:** de grens die een mens leest is aantoonbaar de grens die een worker krijgt, per dispatch en per merge. Een profielwijziging is zichtbaar als verandering van de digest. Een SSOT die zijn eigen fout stilzwijgend wist, verliest het bewijs dat de fout bestond: dat blijft nu staan.
- **Positief:** default-OFF-compatibiliteit blijft. Specs van vóór P5 blijven geldig met een leeg digestveld. De merge-poort geeft onder OFF `info` en onder ON `SKIPPED_UNVERIFIED`, met een coulancevenster voor specs van vóór P5 dat op het flipmoment sluit. Het specveld is optioneel tot dat moment en verplicht erna.
- **Negatief:** twee identiteiten naast elkaar (`plan_digest` en `policy_digest`) kunnen uiteenlopen. De digest wordt op één plek berekend (`compose_policy`), zit als veld in `canonical_dict()` en de poort herberekent met dezelfde functie. Dat wordt in P4 getest.
- **Negatief:** `provider_constraints.yaml` en `LOCKS_AND_RELEASES.md` zijn beleid dat mensen lezen. Dit is een eenmalige correctie; de truthfulness-test houdt ze eerlijk tot de flip.
- **Open:** wat de handhaving bij de flip blokkeert is nog niet opnieuw gemeten. De voorgaande meting telde 17 matcher-artefacten tussen de 42 uitschieters (zie de docstring van `worker_permission_enforcement_enabled`). Het postuurrapport van P8 moet dat meten voor de flip kan.
- **Opvolging:** P2 tot en met P8 bouwen deze beslissing. Geen van die PR's wijzigt de keten van deze ADR zonder een opvolger-ADR.
