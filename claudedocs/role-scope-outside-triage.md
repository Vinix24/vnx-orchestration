# Role-scope-outside triage — nulmeting en hermeting na scope-reparatie

Datum: 2026-08-15. Dispatch `20260815-opsch-w2-rolescope-fix`, track `role-scope-parity`,
punten 11+12 van het OPSCHALING-cluster plus de restschuld van punt 10 (dit rapport).

Dit document is de restschuld van #1516: de triage in drie buckets bestond al als script,
maar het rapport met de tellingen was nooit geschreven. Hier staan de nulmeting, de
scope-reparatie met de per-regel onderbouwing, en de hermeting na de reparatie.

De telling is gegenereerd uit script-output, nooit uit het hoofd. Beide commando's en de
ruwe output staan onderaan.

---

## De drie buckets

`role_scope_outside_triage.py` splitst de dispatches die buiten hun eigen rol-scope
schreven (`role_scope_only__outside`) in drie elkaar uitsluitende buckets, op de ownership-map
(`OWNERSHIP_RULES`, eerste match wint, fnmatch):

| bucket | betekenis | te fixen via scope? |
|---|---|---|
| `rol-te-smal` | elk buiten-pad is het EIGEN werk van de rol; de scope is te smal, routing was goed | ja |
| `verkeerd-gerouteerd` | elk buiten-pad hoort bij één ANDERE rol; de dispatch zat op de verkeerde rol | nee, routing |
| `onbeslisbaar` | mix van eigen en ander werk, meerdere andere rollen, of geen ownership-regel | nee |

Hard control: de drie buckets moeten de gemeten populatie exact partitioneren.
`sum(3 buckets) == role_scope_only__outside`, bij mismatch exit 1.

---

## Resultaat in één tabel

| maat | nulmeting (main YAML) | hermeting (gerepareerde YAML) | delta |
|---|---:|---:|---:|
| `role_scope_only__outside` | 144 | 86 | **-58** |
| `rol-te-smal` | 57 | 0 | -57 |
| `verkeerd-gerouteerd` | 42 | 61 | +19 |
| `onbeslisbaar` | 45 | 25 | -20 |
| `in_scope` | 211 | 269 | +58 |
| `unlinked` | 372 | 372 | 0 |
| `sum_check_ok` | true | true | |

De daling 144 → 86 is het beoogde resultaat. De dispatch noemde een daling van 142 naar
~90; de nulmeting op de huidige pending-dir is 144, niet 142. Dat verschil van 2 komt doordat
er sinds #1516 (die "140" in zijn docstring noemde) nieuwe dispatches in de pending-dir zijn
bijgekomen. De gemeten 144 is leidend.

De daling naar 86 is GEEN alarmsignaal: `role_scope_only__outside` ging naar 86, niet naar 0.
Alleen de `rol-te-smal`-bucket ging naar 0, en dat is precies de bedoeling: al het eigen werk
dat de scope vergat, zit nu binnen de scope.

De schuif binnen de buiten-populatie is verklaarbaar en verwacht. `verkeerd-gerouteerd` steeg
42 → 61 en `onbeslisbaar` daalde 45 → 25. Dat is herclassificatie, geen nieuw fout-routen.
Een dispatch die voorheen "eigen werk + andermans werk" buiten zijn scope schreef
(onbeslisbaar), verliest zijn eigen-werk-helft aan de verruimde scope. Wat overblijft is een
puur verkeerd-gerouteerd pad. Dus: onbeslisbaar → verkeerd-gerouteerd.

---

## Per-role verdeling

### Nulmeting

| rol | rol-te-smal | verkeerd-gerouteerd | onbeslisbaar |
|---|---:|---:|---:|
| backend-developer | 48 | 34 | 37 |
| security-engineer | 5 | 4 | 5 |
| quality-engineer | 4 | 2 | 2 |
| research-analyst | 0 | 1 | 1 |
| system-architect | 0 | 1 | 0 |

### Hermeting

| rol | rol-te-smal | verkeerd-gerouteerd | onbeslisbaar |
|---|---:|---:|---:|
| backend-developer | 0 | 52 | 18 |
| security-engineer | 0 | 5 | 4 |
| quality-engineer | 0 | 2 | 2 |
| research-analyst | 0 | 1 | 1 |
| system-architect | 0 | 1 | 0 |

Drie rollen hadden `rol-te-smal`-eigen werk (backend 48, security 5, quality 4).
`system-architect` had 0 `rol-te-smal`: die schreef nooit zijn eigen werk buiten zijn scope,
dus zijn scope is NIET gewijzigd. `research-analyst` evenmin (0).

---

## Vijf zwaarste cases per bucket (nulmeting)

### rol-te-smal

| dispatch | rol | # | paden |
|---|---|---:|---|
| 20260814q-a-gate-tests-fixforward | quality-engineer | 5 | scripts/commands/gate.sh, scripts/lib/gate_executor.py, scripts/lib/gate_recorder.py, scripts/lib/gate_status.py, scripts/review_gate_manager.py |
| 20260709-085020-otel-retire | backend-developer | 4 | docs/operations/OTEL_EXPORT.md, docs/operations/PACKAGE_BUILD.md, pyproject.toml, requirements.txt |
| 20260711-163120-bench-v2-smart-lanes | quality-engineer | 3 | scripts/benchmark/field-tests/README.md, scripts/benchmark/field-tests/lane_calibration.yaml, scripts/benchmark/field-tests/runners/lane_calibration.py |
| 20260713-085536-docs-applications | backend-developer | 3 | docs/applications/README.md, docs/applications/coding-agents.md, docs/applications/finance.md |
| 20260717-vnx-pin-robustness | backend-developer | 3 | vnx_cli/_engine.py, vnx_cli/commands/init_cmd.py, vnx_cli/main.py |

### verkeerd-gerouteerd

| dispatch | rol | # | paden |
|---|---|---:|---|
| 20260730-skillscope-paths | backend-developer | 4 | .claude/skills/control-centre/SKILL.md, .claude/skills/horizon/SKILL.md, .claude/skills/planner/SKILL.md, skills/horizon/SKILL.md |
| D-horizon-d2-skillrename | backend-developer | 4 | .claude/skills/featureplan-kickoff/SKILL.md, .claude/skills/horizon/SKILL.md, .claude/skills/planner/SKILL.md, .claude/skills/pm/SKILL.md |
| 20260709-080633-fabricdoor-consumer | backend-developer | 3 | .claude/terminals/T0/role-orchestrator.md, examples/backend-developer/CLAUDE.md, examples/backend-developer/config.yaml |
| 20260802-w19c-test-store-isolation | quality-engineer | 3 | scripts/lib/vnx_mode.py, scripts/lib/vnx_paths.py, scripts/pr_queue_manager.py |
| 20260807-102859-state-writer-install-artifact | backend-developer | 3 | templates/init/default/settings.json.j2, templates/init/minimal/settings.json.j2, templates/settings_vnx_keys.json.tmpl |

### onbeslisbaar

| dispatch | rol | # | paden |
|---|---|---:|---|
| D-retire-featureplan-prqueue | backend-developer | 12 | .claude/skills/featureplan-kickoff, planner, pm/SKILL.md; .claude/terminals/T0/CLAUDE.md; FEATURE_PLAN.md; PR_QUEUE.md; agents/orchestrator/CLAUDE.md; docs/contracts/f36-r12-rpc-schemas/query_pr_queue-{request,response}.json; templates/{DOMAIN_FEATURE_PLAN_TEMPLATE,FEATURE_PLAN_TEMPLATE,PR_QUEUE_TEMPLATE}.md |
| 20260712-190957-cockpit-pr11 | backend-developer | 10 | docs/DOCS_INDEX.md; docs/_archive/*; docs/comparisons/*; docs/manifesto/HEADLESS_TRANSITION.md |
| 20260716-f1-t0-startup-import | backend-developer | 9 | .claude/settings.json; .claude/skills/featureplan-kickoff/SKILL.md; .claude/terminals/T0/{CLAUDE.md,role-orchestrator.md}; bin/vnx; hooks/sessionstart.sh; templates/init/default/{claude_md.j2,terminals/T0_claude_md.j2}; templates/terminals/T0.md |
| 20260716-ff-1174-r3-wireup | backend-developer | 9 | idem |
| 20260716-ff-1174-r4-testfix | backend-developer | 9 | idem |

Na de reparatie is `rol-te-smal` leeg. De `verkeerd-gerouteerd`-top-5 is ongewijzigd (routing,
geen scope). In `onbeslisbaar` krimpt de `t0-startup-import`/`ff-1174`-familie van 9 naar 7
paden: `bin/vnx` en `hooks/sessionstart.sh` vallen nu binnen de backend-scope en verdwijnen
uit de buiten-paden. Concreet bewijs dat de scope-reparatie precies de eigen-werk-paden vangt.

---

## Scope-reparatie — wat er per rol is toegevoegd en waarom

Onderbouwd, nooit blanket. Elke regel noemt het pad en het aantal buiten-pad-voorkomens in de
`rol-te-smal`-bucket dat hij oplost. Een regel die niets oplost hoort er niet in.

### backend-developer (48 rol-te-smal)

| regel | # | waarom |
|---|---:|---|
| `vnx_cli/**` | 51 | CLI-package, runtime-implementatie |
| `bin/**` | 5 | runtime-entrypoints (bin/vnx) |
| `hooks/**` | 1 | runtime-hooks (monitor_tripwire.sh) |
| `configs/**` | 3 | runtime-config (plan_gate_panel, producer_freshness) |
| `docs/applications/**` | 3 | feature-docs |
| `docs/operations/**` | 4 | ops-docs (overlapt security's WORKER_PERMISSIONS.md) |
| `docs/MIGRATION_GUIDE.md` | 1 | root feature-doc, letterlijk pad |
| `VERSION` | 5 | root build/release |
| `CHANGELOG.md` | 4 | root build/release |
| `pyproject.toml` | 1 | root build/release |
| `uv.lock` | 1 | root build/release |
| `requirements.txt` | 1 | root build/release |

`docs/MIGRATION_GUIDE.md` is bewust een letterlijk pad in plaats van een `docs/*.md`-glob.
fnmatch's `*` matcht `/` op Unix, dus `docs/*.md` zou ook `docs/core/DISPATCH_RULES.md`,
`docs/governance/decisions/ADR-*.md` en `docs/manifesto/*` vangen. Dat is system-architect-
terrein. Het letterlijke pad sluit alleen het eigen werk.

### quality-engineer (4 rol-te-smal)

| regel | # | waarom |
|---|---:|---|
| `scripts/benchmark/**` | 5 | benchmark-runners |
| `scripts/refactor_*.py` | 2 | equivalentie-tooling |
| `scripts/lib/gate_*.py` | 3 | review-gate-infra |
| `scripts/commands/gate.sh` | 1 | review-gate-infra |
| `scripts/review_gate_manager.py` | 1 | review-gate-infra |

### security-engineer (5 rol-te-smal)

| regel | # | waarom |
|---|---:|---|
| `docs/operations/WORKER_PERMISSIONS.md` | 4 | permission-docs |
| `docs/governance/KEY_PROVISIONING.md` | 1 | key-provisioning-doc |

Twee smalle bestands-grants, geen `docs/**` of `scripts/lib/**`. De test
`test_scope_additions_are_narrow_doc_grants_not_broad_surface` pint dit af.

### Niet gewijzigd

`system-architect` en `research-analyst` hadden 0 `rol-te-smal`, dus hun scope is onaangeraakt.
`Makefile` is bewust NIET toegevoegd aan backend-developer, ondanks dat het eigen werk is: het
kwam in de `rol-te-smal`-bucket niet als buiten-pad voor (0 voorkomens). Een regel die niets
oplost hoort er niet in.

---

## Verkeerd-gerouteerd: het routing-patroon (niet gefixt via scope)

42 (nu 61) dispatches zaten op de verkeerde rol. Dat is geen scope-probleem maar een
routing-probleem, en een scope-verruiming lost het niet op. Het patroon is specifiek:

De dominante vorm is **backend-developer die system-architect-werk doet** op `.claude/skills/**`,
`.claude/terminals/**`, `templates/**`, `agents/**`, `examples/**` en `docs/manifesto/**`.
De ownership-map wijst die paden toe aan system-architect. De reden dat juist backend-developer
deze paden zo vaak raakt, is de sentinel-default: wanneer de rol niet oplost, valt de worker
terug op `_FAKE_DEFAULT_ROLE = _IDENTITY_UNRESOLVED = "identity_unresolved"`
(`scripts/lib/dispatch_identity.py:40`), en die rol valt via `resolve_worker_profile` terug op
het code-worker-fallbackprofiel. In de spec-administratie leest zo'n onopgeloste dispatch als
"backend-developer", terwijl de inhoud system-architect-werk is.

De tweede vorm is kwaliteit-werk (`tests/**`, `scripts/check_*`, `.github/**`) dat bij
backend-developer of een andere rol terechtkomt, en omgekeerd backend-werk
(`scripts/lib/vnx_mode.py`, `scripts/lib/vnx_paths.py`, `scripts/pr_queue_manager.py`) dat bij
quality-engineer terechtkomt.

Deze bucket wordt niet opgelost door scope te verruimen. De verruiming die hier is gedaan
(backend's eigen runtime-paden) verandert het aantal verkeerd-gerouteerde dispatches niet
inhoudelijk; het getal stijgt alleen door de herclassificatie van voorheen-onbeslisbare
dispatches.

---

## Onbeslisbaar: blijft onbeslisbaar

25 dispatches (was 45) blijven onbeslisbaar. De zwaarste is `D-retire-featureplan-prqueue`
met 12 buiten-paden die eigen werk, andermans werk en paden zonder ownership-regel mengen
(`FEATURE_PLAN.md`, `PR_QUEUE.md`, `CLAUDE.md` hebben bewust geen eigenaar). Die kunnen per
constructie niet uit spec + changed files worden beslist, en horen ook niet in een scope-regel.

---

## Conclusie: OI-1209 — de scope-reparatie wordt pas actief na role-propagatie

De aangepaste scopes zijn correct en getest, maar ze worden pas actief zodra de rol daadwerkelijk
bij de enforcement-hook aankomt. Dat is vandaag niet het geval op de provider-lane.

De enforcement-hook leest de rol uit `VNX_WORKER_ROLE`
(`scripts/hooks/pretooluse_worker_scope_enforce.py:177`). Die variabele wordt alleen gezet in
`scripts/lib/tmux_interactive_dispatch.py:1439`, de claude/tmux-lane. De provider-lane
(`provider_dispatch.py`, dominant sinds de kimi-flip) zet hem NIET. Met
`VNX_ENFORCE_WORKER_PERMISSIONS` aan krijgt elke provider-lane-worker daardoor het restrictieve
code-worker-fallbackprofiel, ongeacht zijn echte rol.

De consequentie is dubbel:

1. Vandaag (enforcement uit) heeft deze scope-reparatie geen runtime-effect. De meting hier is
   een voorspelling van wat er gebeurt zodra de rol goed doorkomt.
2. Enforcement aanzetten vóór de role-propagatie is gerepareerd, blokkeert de hele
   provider-lane. Dat is punt 13 van het OPSCHALING-cluster en een T0-beslissing; deze dispatch
   zet de flag NIET aan.

Het voorbeeld uit de dispatch-opdracht bevestigt de richting: een system-architect die
`docs/core/**` schrijft is zijn eigen werk (scope te smal), geen verkeerde routing. Dat geval
is de reden dat `docs/core/**` in `OWNERSHIP_RULES` aan system-architect hangt en dat de
backend-scope NIET met een `docs/*.md`-glob is verruimd.

---

## Commando's en ruwe output

Beide metingen draaien beide scripts. Het verschil zit in de YAML die de resolver leest.
De resolver (`_resolve_permissions_yaml`) geeft voorrang aan `$VNX_PROJECT_ROOT`/`$PROJECT_ROOT`
boven de sibling-YAML. In deze shell wijzen die env-vars naar de MAIN-repo, dus:

- nulmeting = default env (leest main's ongewijzigde YAML);
- hermeting = `env -u PROJECT_ROOT -u VNX_PROJECT_ROOT -u VNX_HOME` (leest de worktree-YAML).

```
# nulmeting
python3 scripts/analysis/worker_scope_enforcement_measure.py
python3 scripts/analysis/role_scope_outside_triage.py

# hermeting
env -u PROJECT_ROOT -u VNX_PROJECT_ROOT -u VNX_HOME \
  python3 scripts/analysis/worker_scope_enforcement_measure.py
env -u PROJECT_ROOT -u VNX_PROJECT_ROOT -u VNX_HOME \
  python3 scripts/analysis/role_scope_outside_triage.py
```

### role_scope_outside_triage.py — summary (nulmeting)

```json
{
  "role_scope_only__outside": 144,
  "unlinked": 372,
  "linked_no_files": 0,
  "in_scope": 211,
  "rol_te_smal": 57,
  "verkeerd_gerouteerd": 42,
  "onbeslisbaar": 45,
  "sum_check_ok": true
}
```

### role_scope_outside_triage.py — summary (hermeting)

```json
{
  "role_scope_only__outside": 86,
  "unlinked": 372,
  "linked_no_files": 0,
  "in_scope": 269,
  "rol_te_smal": 0,
  "verkeerd_gerouteerd": 61,
  "onbeslisbaar": 25,
  "sum_check_ok": true
}
```

### worker_scope_enforcement_measure.py — kernvelden

| veld | nulmeting | hermeting |
|---|---:|---:|
| dispatch_specs_total | 727 | 727 |
| linked_to_commit | 355 | 355 |
| would_be_blocked_by_flip | 155 | 112 |
| blocked_no_dispatch_paths__role_scope_only | 110 | 73 |
| blocked_with_paths__also_outside_role_scope | 34 | 13 |
| role_scope_only__outside | 144 | 86 |
| role_scope_only__inside | 211 | 269 |
