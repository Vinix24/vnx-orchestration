# ADR-051 - Een geplande taak volgt de pin op runtime, en een release bewijst zichzelf

**Status:** Accepted (dispatch dlv-079d383ac4cd, track fleet-release-and-rollout, OI-2371)
**Datum:** 2026-10-28
**Besloten door:** T0 op 27-10 (OI-2371), op het operatorbesluit van 25-10 en op de tiebreaker `plan-tiebreak-fleet-release-and-rollout-5c594af1`. De besluiten zijn daar genomen en PR-2, PR-3, PR-6 en PR-7 hebben ze al gebouwd; dit ADR legt ze vast en heropent ze niet. Alle metingen en regelverwijzingen staan op main `12994639`.
**Cross-refs:** plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/fleet-release-and-rollout.md` (revisie 1b, de secties "Besluiten", "Leveringen" en "Uitrol en terugdraaien"); OI-2371 (dit deliverable); OI-1663 (`PATH`); OI-2415 (een `VNX_HOME=`-toewijzing in de opdrachtregel); OI-2057 (settings-merge, PR-1 van deze track); ADR-030 (de plan-first-gate die het open punt achterliet); ADR-046 (de vorm van dit besluit).

## Context

De track `fleet-release-and-rollout` heeft één doel: een release-versie is uniek en verifieerbaar, en de uitgerolde boom en de repo lopen niet uit elkaar zonder dat een driftcheck faalt. De meting van 25-10 liet zien dat geen van beide helften dat waarmaakte.

| Claim uit het doel | Meting 25-10 | Oordeel |
|---|---|---|
| Uitgerolde boom en repo zijn dezelfde | `VERSION` 1.7.6, `current` -> `v1.7.6`, main 29 commits en 67 `scripts/`-entries voor op de tag | liep uiteen, niets mat het |
| De projecten draaien die versie | 42 plists, 0 van 6 jobs op gepinde projecten volgde de pin; `mission-control` draaide drie versies tegelijk (1.7.1, 1.7.5, 1.7.6) | geen enkele job volgde zijn pin |
| Een driftcheck faalt bij afwijking | `install:mode` vergelijkt pin met `current`; `vnx update` meet alleen bij een cutover | er was geen check op main-drift |

**De taken.** Een plist bakt zijn engine-root (`VNX_HOME`) bij installatie in, dus een job bleef de boom draaien waaruit `reload_plist.sh` ooit was uitgevoerd — wat het `.vnx-version` van het project ook zei. Dat is geen configuratiefout van één project; het is de vorm van de start.

**De release.** `VERSION` en `current` heetten gelijk, maar de tag en main waren dat niet. Niets mat dat verschil, dus een release die niet meer is wat de repo is, werd niet als afwijking gezien zolang niemand keek.

De twee helften hebben dezelfde faalvorm: **stil doorgaan op de verkeerde boom**. Een job met een dode pin draait de oude boom zonder klacht, en een afgeweken release-dir publiceert zichzelf als versie. Beide zijn vandaag dicht: de launcher weigert (sectie 1 en 2), `vnx release verify` en `vnx doctor` meten (sectie 3). Wat nog niet gebouwd is, staat er als gepland bij.

De CLI-start (`vnx_cli/_reexec.py`) blijft buiten dit besluit. Die is met opzet fail-open — `maybe_reexec_pinned` (`vnx_cli/_reexec.py:295`) waarschuwt en draait de huidige versie, want een pinprobleem mag een interactieve CLI niet breken (`vnx_cli/_reexec.py:306`). Een *geplande* taak is de andere consument: niemand leest zijn exitcode, dus dezelfde keuze zou hier juist het falen onzichtbaar maken. Dat verschil in consument is de reden dat er twee paden zijn en niet één.

## Besluit

### 1. De pin wordt op runtime gevolgd

Elke geplande taak start via één launcher, één keer per jobstart. De plist noemt niet langer een script in de engine-boom maar de launcher, met het project en het relatieve script als argumenten: `${VNX_HOME}/scripts/launchd/vnx_pinned_launcher.sh ${VNX_PROJECT_ID} scripts/gate_obligation_runner.py` (`scripts/launchd/com.vnx.gate-obligation-runner.plist:103-105`).

- **De resolver komt uit de boom waar de launcher woont.** De launcher pakt `$(dirname "$0")/../lib/pinned_engine.py` (`scripts/launchd/vnx_pinned_launcher.sh:31`), en `resolve_engine` (`scripts/lib/pinned_engine.py:91`) bepaalt de boom. Een nieuwere launcher kan dus een oudere pin starten, en de doelboom hoeft alleen `<relative-script>` te dragen: er wordt niets uit geïmporteerd (`scripts/lib/pinned_engine.py:119`, getest in `tests/test_launchd_pinned_start.py:103`).
- **De volgorde is vast.** Het project komt uit het register (`launchd_project_root.resolve_project_root`), de pin uit `.vnx-version` via `_find_pin_dir` en `_read_pin` (`vnx_cli/_reexec.py:91`, `vnx_cli/_reexec.py:137` — dezelfde lezers als de CLI, dus geen tweede kopie van de pinregels), en de map uit `<declared>/versions/` via `_resolve_pinned_dir` (`vnx_cli/_reexec.py:263`).
- **Geen pin: de gedeclareerde boom.** Dat houdt het dev-checkout-gedrag ongewijzigd: `EngineChoice(declared, SOURCE_DECLARED, None)` (`scripts/lib/pinned_engine.py:103`).

De uitkomsten zijn eindig en elke weigering is dezelfde:

| Uitkomst | Wat de start doet | Bron |
|---|---|---|
| geen pin | de gedeclareerde `VNX_HOME` | `scripts/lib/pinned_engine.py:103` |
| pin resolveert onder `versions/` | de gepinde boom | `scripts/lib/pinned_engine.py:116` |
| dode pin (niet geïnstalleerd) | exit 78, één regel met project, pin en gezochte map | `scripts/lib/pinned_engine.py:112` |
| onleesbare of misvormde pin | exit 78, idem | `scripts/lib/pinned_engine.py:106` |
| pin ontsnapt uit de versions-root | exit 78, idem | `vnx_cli/_reexec.py:283` |
| niet-geregistreerd project | exit 78, idem | `scripts/lib/pinned_engine.py:97` |
| gekozen boom mist `<relative-script>` | exit 78, idem | `scripts/lib/pinned_engine.py:119` |

`EX_CONFIG = 78` staat in `scripts/lib/pinned_engine.py:54`. De tests dekken de dode pin (`tests/test_launchd_pinned_start.py:131`) en de pin zonder script (`tests/test_launchd_pinned_start.py:145`).

- **Er is nooit een terugval naar een andere boom.** De weigering zegt dat letterlijk (`scripts/lib/pinned_engine.py:119-127`). Een stille terugval is precies de faalvorm die de meting van 25-10 liet zien.

**Waarom niet bij installatie ingebakken.** Een pin die in de plist gebakken zit, veroudert bij de eerste pinwijziging die geen `reload_plist.sh` krijgt: de job blijft dan stil een oude versie draaien en niemand ziet het. Resolveren bij elke start maakt de pin de enige bron en de plist een constante.

**Waarom fail-closed hier wel.** De CLI waarschuwt en gaat door; `_quiet_pin_reader` (`scripts/lib/pinned_engine.py:75`) maakt dat onderscheid expliciet: dezelfde conditie is hier fail-closed, dus de waarschuwing van `_read_pin` moet wijken voor de weigering. Een job die na een geslaagde start niets doet, valt op; een job die op de verkeerde boom "slaagt", valt niet op.

### 2. Launcher-compatibiliteit en een lopende job

- **Een lopende job wordt nooit omgeschakeld.** Elke start resolveert opnieuw (`scripts/launchd/vnx_pinned_launcher.sh:41`); een run die al loopt eindigt op de boom waarmee hij begon, en pas de volgende launchd-start ziet een gewijzigde pin. Getest met twee starts en een pinwissel ertussenin (`tests/test_launchd_pinned_start.py:159`).
- **Het contract van de launcher is het compatibiliteitsoppervlak tussen twee bomen.** Wie een nieuwe launcher naast een oude pin zet, vertrouwt op precies deze vijf punten:

| Deel van het contract | Regel | Bron |
|---|---|---|
| argumenten | `<project_id> <relative-script> [args...]`; minder dan twee is exit 78 | `scripts/launchd/vnx_pinned_launcher.sh:22-27` |
| weigering | exit 78, één regel op stderr, niets op stdout | `scripts/launchd/vnx_pinned_launcher.sh:41-43` |
| omgeving | `VNX_HOME` gezet op de gekozen boom | `scripts/launchd/vnx_pinned_launcher.sh:49` |
| `PATH` | **achteraan** aangevuld, nooit vooraan (OI-1663) | `scripts/launchd/vnx_pinned_launcher.sh:52` |
| uitvoering | `exec python3 <relative-script>` met de argumenten doorgegeven | `scripts/launchd/vnx_pinned_launcher.sh:54` |

- **Een wijziging daaraan is een breuk voor elk gepind project.** Daarom importeert de launcher niets uit de doelboom, en daarom staat het contract in de kop van het script en in de plist-comment (`scripts/launchd/com.vnx.gate-obligation-runner.plist:89-98`) in plaats van in een test alleen: een oudere pin moet met een nieuwere launcher kunnen starten zonder meegelift te worden.
- **`PATH` achteraan is geen detail.** Een `~/.local/bin/python3` die vóór de systeem-`python3` komt, neemt de `exec python3` van de launcher zelf over. Aanvullen achteraan vindt `codex` (dat daar staat) even betrouwbaar en raakt geen andere binary op de gedeclareerde `PATH`.

### 3. Releaseverificatie

Drie metingen, alle drie deterministisch en read-only.

| Meting | Wat ze vaststelt | Waar |
|---|---|---|
| `vnx release verify <version>` | de geïnstalleerde map is de boom van de tag | `vnx_cli/commands/release.py:553` |
| `vnx release publish` | de tag ligt op de default branch | `vnx_cli/commands/release.py:260` |
| `vnx doctor` `release:main-drift` | de draaiende release mist geen commits die main heeft | `vnx_cli/commands/doctor.py:353` |

- **`verify` vergelijkt met de tag-boom, niet met de index.** `HEAD` moet de commit van de tag zijn en elk bestand moet byte-gelijk zijn: blobs voor gewone bestanden, doelen voor symlinks, het executable-bit tegen de mode van de tag (`vnx_cli/commands/release.py:516`). Er zijn **precies twee** toegestane afwijkingen: ` D .vnx-project-id` (het bestand is gestript door `install-central.sh`) en `?? .vnx-install-mode` (door diezelfde installatie geschreven) — `VERIFY_ALLOWED_DELETED` en `VERIFY_ALLOWED_UNTRACKED` in `vnx_cli/commands/release.py:43-44`. Elke derde afwijking is een probleemregel en exit 1; `_compare_entries` noemt hem bijvoorbeeld `changed`, `missing`, `extra` of `mode` (`vnx_cli/commands/release.py:473`).
- **`publish` weigert een tag buiten de default branch.** `_unmerged_tag_message` (`vnx_cli/commands/release.py:260`) toetst met `git merge-base --is-ancestor`; een niet-lokale repo, een onvindbare default branch en een mislukte `merge-base` zijn "niet bewezen" en dus een weigering, geen pass. `--allow-unmerged-tag "<reden>"` is de enige uitweg, de reden moet niet-leeg zijn (`vnx_cli/commands/release.py:297`) en de waarschuwing noemt hem. De controle draait ook in `--dry-run` (`vnx_cli/commands/release.py:287`).
- **`release:main-drift` blokkeert.** In een git-checkout waarin de draaiende release commits mist die main heeft, is de uitkomst FAIL en de exitcode niet-nul, met de commits en hun paden. Een gedocumenteerde afwezigheid (geen git-checkout, geen main-ref, een release-ref die de checkout niet kent) is "skipped: <reden>" (`scripts/lib/release_drift.py:36`); een *mislukte* meting blokkeert. De check fetcht nooit (`scripts/lib/release_drift.py:226`).

De whitelist is een lijst van twee en geen patroon: een derde afwijking moet falen, en de test bewijst dat (`tests/test_vnx_release_verify.py`). De vergelijking leest de index, `.gitignore` en `skip-worktree` niet — een eerdere versie die `git status --porcelain` las, meldde een gewijzigd `skip-worktree`-bestand en een gitignored extra bestand als schoon.

### 4. Wat bij de operator blijft

De code doet deze vier dingen niet, met opzet: ze zijn vlootbreed, niet terug te draaien door één revert, of ze vereisen een oordeel dat geen meting is.

1. Een release snijden en taggen.
2. `vnx release publish`, inclusief `--set-current`.
3. Pins zetten of bijwerken in de consumers.
4. `bash scripts/launchd/reload_plist.sh <template> [project_id]` per familie per project (`scripts/launchd/reload_plist.sh:1-12`).

Punt 4 is geen formaliteit: de launcher bereikt een geïnstalleerde job pas na een release én een reload. Een gemergede PR verandert geen draaiende taak.

## Terugdraaien

- **De pin volgen.** Revert de commit die een plist naar de launcher liet wijzen (PR-3 voor de obligation-runner, PR-4 voor de overige families) en voer `bash scripts/launchd/reload_plist.sh <template> [project_id]` opnieuw uit. Die reload schrijft de directe aanroep terug. De launcher noemt deze weg zelf (`scripts/launchd/vnx_pinned_launcher.sh:17-18`).
- **Een revert haalt geen geladen job weg.** De job blijft geladen tot de reload; een run die al loopt eindigt op de boom waarmee hij begon. De nameting van de track noemt dat.
- **De releasekant.** PR-6 en PR-7 zijn met `git revert` terug te draaien. Geen migratie, geen verwijderd bestand, geen data: de checks verdwijnen en de metingen die ze deden verdwijnen mee. Wat een revert niet terugdraait, is een al gepubliceerde tag; die hoort bij de operator.

## Gevolgen

- **Fail-closed waar het fail-open was.** Een geplande taak met een dode of onleesbare pin stopt en zegt waarom, in plaats van door te draaien op de boom waaruit de plist ooit gerenderd is. De prijs is reëel: de taak staat stil tot de operator de pin of de plist repareert. Dat is de bedoelde ruil — een luidruchtige stop is beter dan een stille oude versie, en de meting van 25-10 is het bewijs dat de stille variant de normale uitkomst was.
- **Eén launcherregel per familie.** De stand per familie:

| Familie | Start vandaag | Status |
|---|---|---|
| `com.vnx.gate-obligation-runner` | via de launcher (`scripts/launchd/com.vnx.gate-obligation-runner.plist:103`) | gebouwd (PR-3) |
| `com.vnx.receipt-processor` | direct uit `${VNX_HOME}` (`scripts/launchd/com.vnx.receipt-processor.plist:49`) | gepland (PR-4) |
| `com.vnx.cleanup-reviewed-worktrees` | direct uit `${VNX_HOME}` (`scripts/launchd/com.vnx.cleanup-reviewed-worktrees.plist:50`) | gepland (PR-4) |
| `com.vnx.ledger-health` | direct uit `${VNX_HOME}` (`scripts/launchd/com.vnx.ledger-health.plist:66`) | gepland (PR-4) |

- **Gepland, nog niet gebouwd:** PR-4 zet de drie resterende families op dezelfde launcher; PR-5 laat `regen_settings.sh` een gepind project uit zijn eigen pin renderen (tot dan rendert een consumer vanuit de boom die de operator draait); PR-8 (`vnx preflight`) bundelt pin, plists, hooks en release-verify in drie blokkerende klassen en is niet gemerged. Dit ADR noemt ze als plan, niet als toestand.
- **De shim volgt de pin niet in project-scope.** Er komt nu geen PR-10. Vandaag routeert geen consumer zijn project-hooks via de shim, en `VNX_HOME` zetten in een re-exec lekt naar elk kindproces — dat is de reden dat `_reexec` ongemoeid blijft. PR-8 meldt het als de situatie verandert; dan wordt het een losse PR.
- **De releasekant is strenger geworden.** Een tag buiten de default branch, een release-dir die niet de tag is en een main die voorloopt op de release zijn alle drie een weigering of een blokkade, geen waarschuwing. De prijs: een bewuste hotfix-tag op een zijbranch moet `--allow-unmerged-tag "<reden>"` krijgen, en die reden staat in het log.
- **Dit ADR legt een genomen besluit vast.** De T0 nam het op 27-10 (OI-2371) en de track bouwde het al deels; dit document is de vastlegging, geen startsein. Wat het toevoegt is de samenhang tussen de twee helften en de grens met de CLI-start: die stond per opdracht (PR-3, PR-6, PR-7) beschreven en nergens als één regel.

## Wat deterministisch is en wat een model vraagt

Alles in dit besluit is deterministisch: de resolutieregels en hun volgorde, de exitcode 78, de whitelist van twee, de ancestry-toets, de blobvergelijking en de driftmeting zijn regels, queries en vergelijkingen. Er is geen modelcall nodig en er komt er geen bij. De enige vrije tekst is de reden bij `--allow-unmerged-tag`; die wordt opgeslagen en getoond, nooit geïnterpreteerd. Waar de invoer niet klopt — een dode pin, een onbekend project, een ontbrekende tag, een lege reden — weigert de code zonder wijziging. Dat is de fail-closed kant van dezelfde regel.

## Wat dit ADR niet verandert

- **De pinregels in `_reexec`.** `_find_pin_dir`, `_read_pin` en `_resolve_pinned_dir` blijven zoals ze zijn; de launcher hergebruikt ze alleen met de waarschuwing stilgezet (`scripts/lib/pinned_engine.py:75`). De CLI blijft fail-open.
- **Het gedrag zonder pin.** Een project zonder `.vnx-version` houdt de gedeclareerde boom; dat is het dev-checkout-pad en het is ongewijzigd.
- **De tag/VERSION-guard van `publish`.** Die blijft en is niet verzwakt; dit ADR voegt alleen de default-branch-toets toe als reden om te weigeren.
- **De installatiepaden zelf.** `vnx init` en `install-central.sh` blijven de plists renderen; alleen de regel die ze renderen is een andere.

## Alternatieven die zijn afgewezen

**(a) De pin bij installatie in de plist bakken.** Afgewezen. Elke pinwijziging vraagt dan een reload, en een vergeten reload draait stil een oude versie — precies de meting van 25-10. Resolutie bij elke start maakt de fout luid.

**(b) Ook voor geplande taken fail-open blijven.** Afgewezen. Niemand leest de exitcode van een launchd-job; "waarschuwen en de huidige versie draaien" is daar geen waarschuwing maar een stille verkeerde uitvoering. De CLI mag fail-open zijn omdat een mens de waarschuwing ziet.

**(c) `vnx release verify` op `git status --porcelain` baseren.** Afgewezen na een reproductie: een `skip-worktree`-bestand verbergt zijn eigen wijziging en een gitignored extra bestand verschijnt nooit als untracked, dus verify meldde "gelijk" met een gewijzigd bestand op schijf. De tag-boom is nu de referentie en de bytes op schijf het bewijs.

**(d) `release:main-drift` als WARN.** Afgewezen door de tiebreaker van 25-10. Een WARN laat `vnx doctor` exit 0 geven terwijl de vloot op een oude release draait; dan is de check een mededeling en geen check.

**(e) Een tag buiten de default branch publiceren met een waarschuwing.** Afgewezen. Dan scheept een zijbranch code die nooit op main kwam, en de release ziet er identiek uit als een echte. De weigering met een expliciete, genoemde reden houdt de uitzondering zichtbaar in het log.
