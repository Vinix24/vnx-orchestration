# FORGE_GATE.md — vnx-gate: van lokale review-poort naar afgedwongen check-run

Operator-runbook voor golf B (`gate-enforcement-to-forge`). Plan: `claudedocs/plans/2026-09-06-golf-B-forge-dwingt-af.md` (deliverables B4, OP-B3, B5). Doel: GitHub weigert elke merge naar `main` zonder een gepubliceerd poortoordeel op exact de PR-head, ook een kale `gh pr merge`, ook van de operator zelf.

## 0. Wat bestaat er vandaag, wat nog niet (gemeten 2026-09-08)

Dit runbook beschrijft twee lagen.

| Laag | Status op 2026-09-08 | Commando's in dit runbook |
|---|---|---|
| **1. Branch-protection als YAML** (B1, #1807, **gemerged**) — `scripts/forge/branch_protection.yaml`, `scripts/forge/apply_branch_protection.py`, `scripts/lib/forge_protection_drift.py`, de preflight in `scripts/pr_merge.py`, de doctor-check in `scripts/vnx_doctor.py` | **Op `main`.** | Geverifieerd tegen elk script zijn eigen `--help`, en tegen een echte `gh api`-meting op de repo (zie §4). |
| **2a. Forge-client** (`scripts/lib/forge_check_run.py`) | **Op `main`** (B2a, #1808, plus de keychain-fix #1813). Bracht ook het `app:`-blok uit §3 en de schema-aanpassing in `forge_protection_drift.py` mee. Het App ID staat inmiddels ingevuld: `4869217`, slug `vnx-gate`. | De client heeft **geen CLI**, alleen library-functies — de toets in §2 is dus een directe library-aanroep. |
| **2b. Publicatie + de samenvattende check** (B2b #1812, B3 #1814) — `scripts/lib/forge_gate_publisher.py`, aangeroepen door de gate-recorder na elk schijf-record | **Op `main`.** De afbeelding van poortstatus naar `conclusion`, de `--pr`-CLI (`publish`, `review`, `pending-preview`) en `vnx-gate/review` als samenvattend oordeel bestaan en zijn getest. | Geverifieerd tegen de broncode op `main` en tegen `pending-preview`, dat niets schrijft en niets opvraagt. |
| **3. `vnx-gate/review` als VEREISTE check** (OP-B3) | **Half.** De entry staat sinds OP-B3 in `required_status_checks.checks` van `branch_protection.yaml` op `main` (15 checks), gebonden aan app_id `4869217`. De **apply is nog niet gedraaid**: de live bescherming vereist nog 14 checks. Zie §5 — tussen die twee momenten staat de merge-deur dicht. | §5. |
| **4. Terugweg één keer live bewijzen** (B5) | **Niet gedaan.** | §7 beschrijft de stappen; B5 voert ze één keer uit en quoteert de API-feiten. |

**Meetcorrectie op de aanname achter §4:** de dispatch-instructie voor dit runbook noemt de eerste apply als het moment waarop de bescherming ontstaat. Gemeten op 2026-09-07 via `gh api repos/Vinix24/vnx-orchestration/branches/main/protection` (+ `.../required_signatures`, `repos/{owner}/{repo}`, `.../rulesets`) staat de bescherming al live en komt hij op elk veld overeen met `branch_protection.yaml` (14/14 checks, `enforce_admins: true`, `allow_auto_merge: false`, `required_signatures: false`, 0 rulesets) — precies zoals de YAML's eigen kopregel zegt ("Measured live ... on 2026-09-07"). Een `--dry-run` bevestigt dit: `verdict: DRY-RUN, changed: False, diffs: []`. De eerste apply-run is dus vandaag een **no-op**: hij schrijft de eerste receipt en bewijst idempotentie, hij legt de bescherming niet voor het eerst vast — die staat er al.

**Meetcorrectie op de aanname achter §3:** de dispatch-instructie gaat ervan uit dat het App ID op de `pending_checks`-entry zelf komt te staan, zoals bij `checks[]` (`{context, app_id}`). Gemeten in PR #1808: `pending_checks` blijft een lijst van kale context-strings (`parse_protection_config` eist `all(isinstance(p, str) ...)`), en het App ID komt in een **apart, nieuw top-level `app:`-blok** te staan dat de client leest, nooit toegepast en nooit vergeleken wordt voor drift. Zie §3.

Dat verschil verdwijnt bij de promotie van OP-B3, en dat is geen inconsistentie: `checks[]` **is** het PUT-object, en GitHub eist de app-binding daar per entry. Een `vnx-gate/*`-entry zonder eigen `app_id` zou elke app die status laten zetten, en precies daarom weigert de schemalezer hem (`null` of de "elke app"-sentinel `-1`). In `pending_checks` was die tweede kopie overbodig — die lijst gaat nooit de deur uit, en de proefdraai bindt zo'n entry op leestijd aan `app.app_id`.

## 1. App aanmaken **[operator]**

1. GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Naam: `vnx-gate`.
3. Permissions: **Checks: Read & write** — en niets meer. Geen andere repository- of account-permissie.
4. Webhook: **uit** (checkbox "Active" leeg laten). De publisher polt niet en ontvangt niets; een webhook-URL is een extra aanvalsvlak zonder functie.
5. "Where can this GitHub App be installed?": naar wens, maar installeer hem alleen op `Vinix24/vnx-orchestration`.
6. Na het aanmaken: noteer het **App ID** (bovenaan de App-instellingenpagina) en genereer een **private key** (.pem, eenmalig zichtbaar — direct downloaden).
7. Installeer de App op het account, alleen op `Vinix24/vnx-orchestration`. De installatie-URL bevat het **Installation ID** (`.../settings/installations/<installation-id>`).

Resultaat van deze stap: een App ID, een installation-ID, en een `.pem`-bestand op schijf dat na stap 2 weer verdwijnt.

## 2. Keychain **[operator]**

**Geverifieerd tegen `scripts/lib/forge_check_run.py` op `main`** (#1808, plus de keychain-fix #1813 — zie §0). De client leest twee keychain-items via exact hetzelfde patroon als de bestaande `vnx-smtp-pass`-lezer (`scripts/send_digest_email.py::_read_smtp_pass_from_keychain`), maar faalt — in tegenstelling tot die lezer — altijd LUID in plaats van een lege string terug te geven: een leeg keychain-item zou een check-run stil laten verdwijnen in plaats van de publicatie te laten falen, en dat is precies wat deze poort moet voorkomen (module-docstring van `forge_check_run.py`).

De twee itemnamen liggen al vast in de code (`KEYCHAIN_PRIVATE_KEY_SERVICE`, `KEYCHAIN_INSTALLATION_ID_SERVICE`):

```bash
security add-generic-password -s vnx-gate-app-key -a "$USER" -w "$(cat /pad/naar/vnx-gate.pem)"
security add-generic-password -s vnx-gate-installation-id -a "$USER" -w "<installation-id>"
rm /pad/naar/vnx-gate.pem
```

Verwijder de `.pem` van schijf zodra hij in de keychain staat — dat is de enige kopie die overblijft.

**Toets (de client heeft geen CLI, dus dit is een directe library-aanroep, geen subcommando):**

```bash
python3 -c "
import sys; sys.path.insert(0, 'scripts/lib')
from forge_check_run import read_private_key, read_installation_id
read_private_key(); read_installation_id(); print('keychain OK')
"
```

Vóór deze stap geeft dit letterlijk (gemeten in de PR-worktree op 2026-09-07, met een lege keychain):

```
ForgeKeychainError: keychain-item vnx-gate-app-key bestaat niet: security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.. Herstel: security add-generic-password -s vnx-gate-app-key -a "$USER" -w '<pad naar de .pem, via $(cat ...)>' (runbook: docs/operations/FORGE_GATE.md)
```

Na deze stap moet `keychain OK` verschijnen.

## 3. app_id in de YAML **[operator via PR]**

**Gedaan** — beschreven omdat de stap herhaald moet worden als de App ooit opnieuw wordt aangemaakt (§8). #1808 voegde een nieuw top-level blok toe aan `scripts/forge/branch_protection.yaml`, dat inmiddels het echte App ID draagt:

```yaml
app:
  slug: vnx-gate
  app_id: 4869217
```

Dit blok is bewust **geen** onderdeel van `checks[]` of `pending_checks:`. Het beschrijft WIE publiceert, niet wat `main` vereist: `forge_protection_drift.py` behandelt `app` als een expliciet toegestane maar genegeerde top-level sleutel (`_OPTIONAL_TOP_LEVEL_FIELDS = frozenset({"app"})`) — nooit onderdeel van `ProtectionConfig`, nooit van `to_normalized_dict`, dus een wijziging hier kan nooit als drift of als verzwakking geregistreerd worden. Elke andere onbekende top-level sleutel wordt nog steeds geweigerd; dit is een allowlist van precies één sleutel, geen opening van het schema.

Het invullen gaat via een kleine PR door de normale deur (codex-gate tekent). Zolang `app_id` `null` is weigert de client expliciet en luid in plaats van te gokken (`load_app_config`'s eigen foutmelding verwijst naar dit runbook).

Dit blok zegt alleen wie *publiceert*. Wat `main` vereist, staat in `required_status_checks.checks` — daar staat `vnx-gate/review` sinds OP-B3 in, met dit getal eraan gebonden. Zie §5.

De twee getallen gelijk houden is geen leesinstructie meer maar een schemaregel: `parse_protection_config` weigert een `vnx-gate/*`-entry waarvan het `app_id` niet gelijk is aan `app.app_id`, en weigert er ook een wanneer er helemaal geen `app`-blok is (dan is er geen identiteit om aan te binden). De melding noemt beide getallen. Zonder die wachter zou een verwisselde cijfervolgorde gewoon parsen, geen verzwakking zijn voor de merge-deur — `is_weakening` kijkt in de checks-tak alleen naar aanwezigheid, niet naar een gewijzigd `app_id` — en netjes applyen, waarna `main` een check vereist die geen enkele App kan vervullen. Een permanent dichte merge-deur, geïnstalleerd door een typefout.

## 4. Eerste apply **[operator]** — historisch, niet meer als no-op uit te voeren

> **Deze sectie beschrijft de apply zoals hij vóór OP-B3 was: een no-op tegen een 14-checks-YAML.** Dat is hij sinds de merge van OP-B3 niet meer. `branch_protection.yaml` draagt nu vijftien checks en de live poort er veertien, dus een kale `apply_branch_protection.py` **voert de OP-B3-mutatie uit** — de meest consequente wijziging van de golf. Wil je die uitvoeren, volg dan §5 inclusief de opus-leeszetel, niet deze sectie. Wat hieronder staat blijft geldig als beschrijving van de commando's en van de controles erna (receipt, `vnx doctor`).

Dit ging over de bestaande, gemergede 14-checks-YAML van B1, onafhankelijk van stap 1-3.

```bash
python3 scripts/forge/apply_branch_protection.py --dry-run
```

Toont het volledige PUT-object dat zou worden verstuurd. Per OP-B3 leest een tweede lezer (opus-leeszetel) dit object voordat een echt-wijzigende apply draait; die eerste run was een no-op (zie de meetcorrectie in §0), dus die tweede lezer was daar niet vereist. Voor de apply van §5 is hij dat wél.

```bash
python3 scripts/forge/apply_branch_protection.py
```

Verwacht bericht destijds: `nul wijzigingen: live branch-protection komt al overeen met de YAML`. Controleer de receipt:

```bash
grep branch_protection_applied "$VNX_DATA_DIR/state/t0_receipts.ndjson" | tail -1
```

En bevestig dat `vnx doctor` nul drift toont:

```bash
vnx doctor
```

`branch_protection_drift` moet `PASS` zijn. Let op: tussen de merge van OP-B3 en de apply van §5 staat deze check op `FAIL` (vijftien in de YAML, veertien live), en dat is dan de verwachte toestand — geen storing.

## 5. OP-B3 — entry van `pending_checks` naar `checks[]`, en migratie van openstaande PR's

**Operator-stap.** Stap 1 is gedaan; vanaf stap 2 is dit werk dat nog voor je ligt.

1. **De promotie in de YAML — gedaan.** `vnx-gate/review` is uit `pending_checks:` gehaald (die lijst is nu `[]`) en staat als vijftiende entry in `required_status_checks.checks`, met het App ID uit §3 als gebonden `app_id`:

   ```yaml
   - {context: "vnx-gate/review", app_id: 4869217}
   ```

   Het getal staat hier bewust wél per entry, anders dan in `pending_checks` — zie de meetcorrectie in §0. Het moet gelijk blijven aan `app.app_id` onderaan hetzelfde bestand: dat is de identiteit die tekent, en dus de enige App die deze check kan vervullen.

2. `python3 scripts/forge/apply_branch_protection.py --dry-run` — het exacte PUT-object, gequoteerd in de PR. Vooraf, zonder de apply aan te raken, leest hetzelfde object ook:

   ```bash
   python3 scripts/lib/forge_gate_publisher.py pending-preview
   ```

3. Opus-leeszetel leest dat object vóór de apply (dit is de meest consequente mutatie van de golf: vanaf hier faalt elke merge zonder gepubliceerde check).
4. `python3 scripts/forge/apply_branch_protection.py` (zonder `--dry-run`) — dit voegt een check toe, dus is per definitie geen verzwakking en heeft geen `--allow-weaken` nodig.
5. **Migratie van openstaande PR's**, zodat geen enkele open PR na deze apply plotseling onmergebaar wordt zonder duidelijke reden:

   ```bash
   gh pr list --state open --json number,headRefOid --jq '.[] | "\(.number) \(.headRefOid)"'
   ```

   Per head:

   ```bash
   python3 scripts/lib/forge_gate_publisher.py review --pr <n>
   ```

   Een head zonder eerdere poortrun (geen schijf-record voor die PR plus poort) levert terecht `action_required` op en blijft geblokkeerd — dat is geen bug in de migratie, dat is de poort die zijn werk doet. Draai de review-poort eerst op die head, publiceer daarna opnieuw.

### Tussen de merge en de apply staat de merge-deur dicht

Dit is geen risico maar een zekerheid, en het volgt uit de deur zelf. `pr_merge.py::_run_branch_protection_gate` stap (c) vergelijkt de **live** bescherming met de YAML **zoals die op `main` staat**. Zodra de promotie-PR gemerged is zegt `main` vijftien checks en zegt de live poort er veertien. Dat is drift, en op drift heeft de deur geen enkele override: `--allow-weaken` dekt alleen een verzwakking die de PR zelf introduceert ten opzichte van `main`, nooit een verschil tussen live state en `main`'s eigen YAML.

De melding is dan, gemeten door `compare` over precies deze twee toestanden:

```
branch-protection wijkt af van scripts/forge/branch_protection.yaml op main: required_status_checks.checks[vnx-gate/review]
```

Hij noemt het afwijkende veld en niet het herstel. Dat herstel is de apply uit stap 4 hierboven.

Gevolg voor de planning: tussen de merge van de promotie en de apply mag niets zitten. Geen andere PR, geen pauze, geen "morgen verder". Ook de PR die deze situatie zou repareren komt er niet doorheen. De apply is de enige uitweg, en de terugweg in §7.1 (de entry er weer uit) vereist zelf óók een merge — die dus ook geblokkeerd is.

Dezelfde redenering geldt ná de apply, en dat is niet dezelfde situatie. Hierboven is de blokkade tijdelijk en heft de apply hem op. Daarna vereist elke merge een gepubliceerde `vnx-gate/review`, dus zodra publiceren zélf onmogelijk wordt (§8: keychain blijvend dicht, App verwijderd) is de terugweg uit §7.1 een lus in precies die faalmodus: de terugdraai-PR heeft de check nodig die niemand nog kan zetten. Daarvoor bestaat **§7.2**, met één ongegovernde stap en een verplichte vastlegging ervan.

### Na de apply leest een ongepubliceerde head als `unverified`, niet als "geen oordeel"

Bekende leesbaarheidsgrens, en de melding wijst de verkeerde kant op. Geen enkele workflow in `.github/workflows/` maakt de context `vnx-gate/review` — dat doet de App, buiten CI om. `ci_contexts.classify_contexts` valt voor een head zonder gepubliceerd oordeel daarom in de tak "geen check run én geen workflow-job die deze context maakt" (`scripts/lib/ci_contexts.py:537-552`), en die tak zegt letterlijk:

```
no check run on this commit and no workflow job in the checkout produces this context — cannot tell 'not yet' from 'never'
```

`required_contexts_gate` telt die tak als `unverified` en maakt er `SKIPPED_UNVERIFIED` van, met als detailregel:

```
1 of 15 required contexts could not be classified: vnx-gate/review
```

Dat blokkeert, en dat is juist: een poort die niet kon kijken geeft geen toestemming.

Wat er niet aan klopt is de aanwijzing. De tekst leest als een kapotte workflow-graaf — alsof er een workflow-bestand ontbreekt of niet parset. Dat is hier niet aan de hand; er is niets stuk. Het echte herstel is één commando:

```bash
python3 scripts/lib/forge_gate_publisher.py review --pr <n>
```

Herken dus `vnx-gate/review` in een `SKIPPED_UNVERIFIED`-regel als "publiceer het review-oordeel", niet als "repareer CI". Voor de veertien Actions-checks blijft dezelfde melding wel gewoon betekenen wat hij zegt.

## 6. Werkgevolg voor iedereen, na OP-B3

Elke push naar een PR-branch **na** een poortrun maakt de PR onmergebaar totdat de poort opnieuw draait — ook een triviale docs-fixup na een groene poort. De volgorde wordt: `request` → `execute` → de standaardpoort `codex_gate` (of de opvolger in de overnameketen) → publicatie → merge. Wat vóór OP-B3 nog kon (een groene poort, dan nog een klein commit, dan mergen) kan daarna niet meer zonder de poort opnieuw te draaien.

### Wie publiceert wat, en wanneer

"Publicatie" in die volgorde is geen handmatige stap. De **gate-recorder** publiceert na elk schijf-record twee check-runs, in deze volgorde, op de kop die in dat record staat (`scripts/lib/gate_recorder.py::publish_forge_check_run` en `::publish_forge_review_summary`):

1. `vnx-gate/<poort>` — wat déze poort zei. Vereist door niets.
2. `vnx-gate/review` — of er op deze kop een geldig review-akkoord ligt, volgens dezelfde regel als de merge-deur. **Dit is de check die `main` vereist.**

De twee zijn onafhankelijk. De per-poort-publicatie gaat eerst en slaagt of faalt op eigen merites; de samenvatting is een aparte aanroep met een aparte foutafhandeling, dus een fout daar kan de eerste niet ongedaan maken en kan een geslaagde poortrun niet rood maken. Beide falen luid in de log, met het herstelcommando erin, en geen van beide raakt het record op schijf.

Twee poortruns op dezelfde kop publiceren de samenvatting twee keer; dat is een update van dezelfde check-run, geen conflict.

Het CLI-commando blijft bestaan als **handmatige terugvaloptie**, niet als de normale weg:

```bash
python3 scripts/lib/forge_gate_publisher.py review --pr <n>
```

Gebruik het voor een kop die nooit een poortrun heeft gehad (de migratie in §5 stap 5), en na een uitval waarbij de recorder de publicatie moest inslikken — een gesloten keychain, een GitHub-500, een launchd-runner zonder `gh` op zijn PATH (§8). De log-regel van die uitval draagt dit commando al.

Wat de recorder **niet** doorgeeft, omdat hij het niet weet: `project_id` en `project_root`. De samenvatting valt daarvoor terug op de eigen resolutie van de publisher (`origin` van de checkout waarin hij draait). `results_dir` en `branch` komen wél mee, uit het record dat zojuist geschreven is, zodat de check dezelfde records leest als de deur straks toetst.

## 7. Terugweg **[operator]**

De terugweg is een branch-brede actie op de bestaande, geverifieerde `apply_branch_protection.py` — geen nieuwe code nodig, ook niet zodra OP-B3 gedraaid heeft. B5 voert hem één keer uit en quoteert de API-feiten ervan en ervoor.

Er zijn **twee gevallen**, en ze verschillen op precies één vraag: kun je nog publiceren? De deur zelf kent het verschil niet en krijgt er ook geen ontsnappingsluik voor. §7.1 is de terugweg; §7.2 is wat er overblijft als publiceren onmogelijk is.

### 7.1 Het gewone geval — publiceren werkt, en dit is dus geen lus

Van toepassing wanneer de App bestaat, de keychain open is en `forge_gate_publisher.py review --pr <n>` gewoon slaagt. Dan is de terugdraai-PR een PR als elke andere: hij krijgt zijn eigen review-poortrun, de recorder publiceert `vnx-gate/review` op zijn kop (§6), en de deur laat hem door. De terugweg vereist een merge, die merge vereist een gepubliceerde check, en die check is te leveren. Geen cirkel.

1. Kleine PR: haal de `vnx-gate/review`-entry uit `checks[]` van `scripts/forge/branch_protection.yaml` (terug naar `pending_checks:`, of helemaal weg).
2. `python3 scripts/forge/apply_branch_protection.py --dry-run` — dit is nu een verzwakking (een check minder), dus de dry-run toont ook de waarschuwing `zou een verzwakking zijn zonder --allow-weaken`.
3. Draai de review-poort op de kop van die PR en merge hem via de deur, mét `--allow-weaken` en een reden: de PR verzwakt `main`'s YAML, en dat is precies wat stap (d) van de deur tegenhoudt zonder die vlag.
4. **Pas na de merge** de apply, met de reden verplicht ingevuld:

   ```bash
   python3 scripts/forge/apply_branch_protection.py --allow-weaken "vnx-gate/review teruggedraaid: <reden>"
   ```

   Een lege reden wordt geweigerd (`run_apply` toetst expliciet op een niet-lege string) — er is geen stille bypass. Tussen stap 3 en stap 4 zegt `main` veertien en zegt live er vijftien: dat is drift en de deur staat dicht, om dezelfde reden als in §5. Laat er dus niets tussen zitten.
5. Receipt controleren zoals in §4; het veld `weak_fields` in die receipt draagt de naam van de teruggedraaide check.
6. Zodra het probleem verholpen is: dezelfde entry via een nieuwe PR terug in `checks[]`, opnieuw apply (nu weer een aanscherping, geen `--allow-weaken` nodig) — zie §5.

### 7.2 Het noodgeval — publiceren is onmogelijk

**Alleen voor de twee faalmodi uit §8 waarin geen enkele publicatie meer kan slagen:** een blijvend ontoegankelijke keychain, en een verwijderde of gedeïnstalleerde App. In beide gevallen is §7.1 een lus in zijn eigen faalmodus. De terugdraai-PR heeft `vnx-gate/review` nodig om te mergen, en dat is nou juist de check die niemand meer kan zetten. De omweg via een lokale `apply --allow-weaken` vóór de merge helpt niet: die maakt live veertien terwijl `main`'s YAML vijftien zegt, en op dat verschil weigert `scripts/pr_merge.py::_run_branch_protection_gate` stap (c) zonder enige override.

Deze volgorde werkt wél, en hij bevat **precies één ongegovernde stap**: stap 3, een kale `gh pr merge` buiten de deur om. Dat is het enige punt in dit hele runbook waar dat mag, en het mag alleen hier.

1. **Kleine PR met alleen de terugdraai.** Haal de `vnx-gate/review`-entry uit `checks[]`. Niets anders in die PR — hoe kleiner de ongegovernde merge, hoe kleiner wat er ongereviewd binnenkomt.

   Draai er wél de review-poort op. Die schrijft zijn record op schijf en dat record is het bewijsstuk; alleen de publicatie ervan naar GitHub lukt niet. De poort is dus niet overgeslagen, alleen zijn check-run ontbreekt.
2. **Verwijder de vereiste live**, want anders komt stap 3 er ook niet doorheen:

   ```bash
   python3 scripts/forge/apply_branch_protection.py --allow-weaken "noodherstel: <keychain dicht|App weg>, vnx-gate/review tijdelijk uit de bescherming"
   ```

   Dit is de laatste gegovernde stap. Hij schrijft zijn eigen receipt (`branch_protection_applied`, met `weak_fields`), dus dit deel van de afwijking staat vanzelf in de audit trail.

   Let op de volgorde ten opzichte van §7.1: daar apply je ná de merge, hier ervóór. Dat is het hele verschil. Hier is `main`'s YAML nog vijftien en live veertien, dus stap (c) van de deur zou een normale merge alsnog weigeren — daarom is stap 3 een kale merge en geen deur-merge.
3. **Kale merge, buiten de deur om.** De ongegovernde stap:

   ```bash
   gh pr merge <n> --squash
   ```

   Waarom de gegovernde weg hier niet kan: `pr_merge.py` toetst live tegen `main`'s YAML (stap c). Die twee zijn na stap 2 met opzet verschillend, en dat verschil is geen verzwakking die de PR introduceert, dus `--allow-weaken` dekt hem niet. Er is geen vlag die dit opent, en die komt er ook niet — een ontsnappingsluik in de deur zou op elke andere dag ook openstaan.

   Na deze merge zeggen `main`'s YAML en de live bescherming allebei veertien en is de deur weer normaal bruikbaar.
4. **Leg de ongegovernde stap vast.** Verplicht, en niet te improviseren op de dag dat `main` dichtstaat. Twee dingen, in deze volgorde:

   ```bash
   python3 scripts/open_items_manager.py add \
     --dispatch "<dispatch-id van de terugdraai>" \
     --pr <n> \
     --severity blocker \
     --title "Ongegovernde merge van PR #<n>: vnx-gate/review noodterugweg (FORGE_GATE.md §7.2)" \
     --details "Reden: <keychain dicht|App weg>. Poortrecord op schijf: <pad naar het review_gates/results-record>. Live bescherming teruggebracht naar 14 checks via apply --allow-weaken (receipt: branch_protection_applied). Merge uitgevoerd met een kale gh pr merge, buiten pr_merge.py om. Sluiten zodra de App hersteld is en de entry via §5 terugstaat."
   ```

   ```bash
   grep branch_protection_applied "$VNX_DATA_DIR/state/t0_receipts.ndjson" | tail -1
   ```

   De receipt van stap 2 is het bewijs van de verzwakking; het open item is het bewijs van de merge zelf, want dáár schrijft niets een receipt — dat is precies wat "buiten de deur om" betekent. Zonder dit open item eindigt de stap als een gat in de audit trail.
5. **Herstel.** Keychain weer open of App opnieuw aangemaakt (§1 en §2, inclusief een nieuw App ID in de YAML via §3 als de App echt nieuw is). Daarna de entry terug in `checks[]` via §5, apply, en het open item uit stap 4 sluiten met een verwijzing naar de PR die de entry terugzette.

## 8. Faalmodi

- **Gesloten keychain in een niet-interactieve sessie.** Na OP-B3 vraagt elke merge een lokaal gedraaide publisher met keychain-toegang. Zonder die toegang: geen publicatie, dus geen merge — ook niet voor de PR die het repareert. Geverifieerd (PR #1808, §2): elke keychain-fout is luid en draagt het exacte herstelcommando (`security add-generic-password -s <item> -a "$USER" -w '<...>'`); geen stille lege string. Lukt dat herstel wél, dan is §7.1 de terugweg. Lukt het niet (keychain blijvend ontoegankelijk, bijvoorbeeld een niet-interactieve launchd-sessie zonder sessiesleutel — `security`'s exit 51, "wil geen niet-interactieve toegang"), dan kan er niets meer gepubliceerd worden en is **§7.2** de weg: §7.1 zou hier een lus zijn, want de terugdraai-PR heeft zelf de check nodig die niemand meer kan zetten.
- **Verwijderde of gedeinstalleerde App.** `vnx-gate/review` is dan permanent onvervulbaar — geen enkele publicatie kan nog slagen. Dat is de tweede faalmodus waarvoor **§7.2** bestaat, om dezelfde reden: §7.1 vraagt een publicatie die per definitie niet meer kan. Daarna de App opnieuw aanmaken (§1), het nieuwe App ID in de YAML zetten (§3, en de schemalezer weigert een `vnx-gate/*`-entry die niet aan dat getal gebonden is) en de keten opnieuw doorlopen via §5.
- **Fork-PR.** De repo is publiek en er is nog geen externe PR geweest (gemeten: `POST /check-runs` op de head-sha van een fork-PR in de base-repo is dus niet getoetst). Aanvaarde procedure: de operator publiceert vanaf het schijf-record zoals bij elke andere PR. Faalt dat met **422**: er is geen per-PR-override in `apply_branch_protection.py` (het werkt branch-breed, niet per PR) — het herstel is de branch-brede terugweg uit §7.1, die ene PR mergen, en de bescherming direct daarna weer aanzetten. Publiceren werkt in dit geval nog gewoon voor elke andere PR, dus §7.2 is hier niet aan de orde. De eerste externe PR is zo een meting, geen storing.
- **launchd-runner met kaal PATH (OI-1663).** Een runner zonder de interactieve-shell-PATH boekt bijvoorbeeld codex als niet-geïnstalleerd. Voor deze poort betekent dat: als de publicatie via launchd draait, controleer eerst of `gh` en `security` op het PATH van die launchd-sessie staan vóór je een publicatiefout aan de keychain of de App toeschrijft.
- **Drift in een week zonder merges.** Niemand hoeft te mergen om drift te veroorzaken — een handmatige wijziging in de GitHub-UI verandert de live protection zonder dat de YAML meebeweegt. `vnx doctor`'s `branch_protection_drift`-check (§4) vangt dit ook buiten de merge-deur om; draai hem periodiek, niet alleen rond een merge.

## 9. Geaccepteerd restrisico

Letterlijk uit het plan (`claudedocs/plans/2026-09-06-golf-B-forge-dwingt-af.md`, sectie "Geaccepteerd restrisico"): de bescherming is bewerkbaar door precies de operator die hem bindt, en op een persoonlijk account is er geen audit-log-API — een protection-wijziging gevolgd door een kale merge laat niets opvraagbaars achter. Wie de private key in de keychain heeft, kan bovendien met een paar regels `curl` een `success` op elke sha zetten, langs `forge_check_run.py` heen; dezelfde operator, hetzelfde risico. Compenserende maatregelen: de drift in de deur (`pr_merge.py`) en in `vnx doctor` (§4, §8), en de receipt die apply en terugweg zelf schrijven (§4, §7). Wat overblijft is een operator die bewust drie stappen zet; dat risico is aanvaard en staat hier opgeschreven, niet weggewerkt.

## 10. Contract: `branch_protection.yaml` uitbreiden gaat in twee PR's (OI-1672)

**Gemeten, twee keer live.** `scripts/pr_merge.py::_run_branch_protection_gate` stap (d) leest de YAML van de PR-head via de contents-API, maar parseert die met `parse_protection_config` uit `scripts/lib/forge_protection_drift.py` **zoals dat bestand in de lokale checkout staat** — en stap (a), vlak daarvoor in dezelfde poort, heeft net bewezen dat die lokale checkout byte-identiek is aan `main`. De lezer die over de YAML van de PR-head oordeelt is dus altijd `main`'s lezer, nooit die van de PR zelf.

Gevolg: een PR die tegelijk een nieuw veld in `branch_protection.yaml` toevoegt ÉN de lezer uitbreidt zodat die het veld kent, kan nooit door stap (d). `main`'s lezer — de enige die op dat moment draait — kent het nieuwe veld per definitie nog niet. Een PR mag zijn eigen bewaker niet leveren.

**De eis.** Een uitbreiding van het schema van `scripts/forge/branch_protection.yaml` (nieuw top-level veld, nieuw sub-veld, nieuwe toegestane waarde) gaat altijd in twee PR's, in deze volgorde:

1. **Eerst de lezer.** `forge_protection_drift.py` (en elke andere plek die het schema parseert) leert het nieuwe veld kennen. De YAML op `main` verandert in deze PR NIET.
2. **Dan pas het veld.** Een tweede PR voegt het veld daadwerkelijk toe aan `scripts/forge/branch_protection.yaml`. Stap (d) draait nu tegen een `main`-lezer die het veld al kent, en laat de PR door.

**De exacte weigerboodschap**, zodat een lezer die erop stuit hem herkent:

```
branch-protection-YAML op de PR-head ongeldig: branch_protection.yaml heeft onbekende velden: [<veld>]
```

**Twee gemeten gevallen:**

- **2026-09-07 23:52, PR #1808**: voegde in één PR een `app:`-blok toe aan `scripts/forge/branch_protection.yaml` én breidde `forge_protection_drift.py` uit zodat de lezer dat blok kende (172 tests groen op de PR-head). De deur weigerde alsnog met exact de bovenstaande boodschap (`[app]`). Alle andere poorten stonden groen; alleen stap (d) blokkeerde.
- **2026-09-08, OP-B3 (#1815)**: opnieuw bevestigd door T0. De promotie van `vnx-gate/review` van `pending_checks` naar `required_status_checks.checks` (§5) is daarom bewust gedaan **zonder** een nieuw schemaveld — die PR wijzigde alleen bestaande, al-bekende structuur.

Zie ook de docstring van `_run_branch_protection_gate` (stap d) in `scripts/pr_merge.py` voor de codekant van dit contract.

## 11. Per project: doelrepo, strengheid en CI-workflow (OI-1849)

Tot OI-1849 toetste de merge-deur elke merge tegen de repo waar hij uit geïnstalleerd is. Vanuit een centrale installatie (`~/.vnx-system/versions/<v>`, een clone van `Vinix24/vnx-orchestration`) betekende dat: de YAML op `main`, de YAML op de PR-head, de live protection en de ADR-nummers kwamen van vnx-orchestration, terwijl `gh pr merge` en de CI-poort naar de map gingen waar je stond. Een consumer-merge kreeg "geen drift" over een repo die hij niet mergde.

### 11.1 Welke repo de deur toetst

De deur bepaalt één keer, vóór de eerste poort, in welk project hij merget. Die bepaling is de bestaande resolver (`vnx_paths`): `VNX_PROJECT_ROOT` als die gezet is, anders de git-toplevel van de cwd. Die resolver leidt het project af van `VNX_HOME` als de omgeving die zet. Wijst `VNX_HOME` naar een andere map dan de code die draait, en noemt `VNX_PROJECT_ROOT` het project niet, dan weigert de deur: anders merget een deur uit een installatie in de checkout waar een oude shell `VNX_HOME` op liet staan (`merge_target.resolve_project_root`, ook in `apply_branch_protection.py`). De `vnx`-shim zet `VNX_HOME` zelf leeg en loopt hier niet tegenaan. De repo is wat `gh repo view` in die map zegt. Alles wat de merge beoordeelt draait in die map: de CI-run, de ADR-poort, de YAML op `main` en op de PR-head, de live protection, en `gh pr merge` zelf. De eerste regel van de uitvoer is `doelrepo: owner/name`. Onder `--json` staat die regel op stderr en zit hij als `target_repo` in het object.

Kan de deur de repo niet bepalen, dan weigert hij vóór de eerste poort. Een installatie is nooit zijn eigen doel: start je de merge vanuit de installatiemap zelf, dan weigert hij ook.

**De deur zelf** toets je tegen de fabric, nooit tegen het project. In een checkout van de fabric is dat `main`. In een installatie is dat de tag waar hij uit komt: `v<VERSION>` in de repo waar de installatie een clone van is. Een installatie vergelijken met `main` zou elke installatie weigeren die niet op de laatste commit staat, want `main` loopt na een release door. Alle acht de deurbestanden (`_DOOR_INTEGRITY_PATHS`, sinds OI-1849 inclusief `scripts/lib/merge_target.py`, `scripts/lib/vnx_paths.py` en `scripts/lib/ci_contexts.py`) moeten byte-identiek zijn aan die versie.

### 11.2 Het bestand en de leesvolgorde

De deur, `apply_branch_protection.py` en `vnx doctor` lezen hetzelfde bestand in dezelfde volgorde, in de projectmap:

1. `.vnx/branch_protection.yaml`
2. `scripts/forge/branch_protection.yaml` (zo staat het in vnx-orchestration zelf)

Het eerste bestand dat bestaat wint. De deur leest ze via de contents-API op `main` (en op de PR-head voor stap d), dus het bestand moet op `main` gecommit staan. `apply_branch_protection.py` en `vnx doctor` lezen de schijf.

### 11.3 De drie standen

`enforcement: enforce | warn | off` staat op het hoogste niveau van dat bestand.

| Stand | Wat de deur doet | Wat er in het record staat |
|---|---|---|
| `enforce` | Drift tussen `main`'s YAML en de live protection is een weigering. Dit is ook wat een bestand betekent dat er niets over zegt. | `mode: enforce` |
| `warn` | Drift wordt gemeld en de merge gaat door. Een repo zonder protection (GitHub antwoordt `Branch not protected`, HTTP 404) is dezelfde melding. Elke andere leesfout (auth, netwerk, rate-limit) is een weigering: die zegt niets over de repo. | `mode: warn`, `reason_code: enforcement_warn_findings`, de velden in het bericht |
| `off` | De deur kijkt niet naar live protection en toetst de PR-head niet op verzwakking. Alleen de CI-ondergrens blijft: de head wordt gelezen voor `ci_workflow` (§11.4). | `mode: off`, `reason_code: enforcement_off` |

Een project **zonder bestand** wordt behandeld als `warn`, en de deur zegt dat luid: `geen branch_protection.yaml in <owner/name>, niets te toetsen`. Op stderr staat die melding als `WARN:`, en het record heeft `reason_code: protection_file_missing`. Dit vervangt de oude bootstrap-GO, die dezelfde melding gaf.

`warn` verzacht wat de deur met **drift** doet. Het verzacht niet wat een PR met de declaratie mag doen: een PR die een vereiste check schrapt, blijft `--allow-weaken` vragen, in `warn` net als in `enforce`. Een PR die het bestand verwijdert wordt ook geweigerd. Wil je de toetsing bewust uitzetten, zet dan `enforcement: off`, met `--allow-weaken`.

**Verlagen is verzwakken.** `enforce` naar `warn`, `warn` naar `off` en `enforce` naar `off` gaan via `forge_protection_drift.is_weakening` en vragen `--allow-weaken "<reden>"`. Het veld weghalen is geen verlaging: zonder veld is het `enforce`, dus het kan alleen gelijk blijven of hoger worden.

De CI-poort blijft voor iedereen aan. `enforcement` raakt hem niet, en een PR kan hem ook niet verzachten door `ci_workflow` te verwisselen, in geen enkele stand en ook niet bij het inrichten (§11.4).

Let op bij `off`: PyYAML leest het kale woord `off` als het boolean `False`. De lezer neemt dat als `off`. `on`, `yes` en `true` noemen geen stand en worden geweigerd met een melding.

### 11.4 De CI-workflow

De CI-poort zoekt standaard de workflow `VNX CI`. Een consumer heet anders: mission-control en sales-copilot `CI`, SEOcrawler_v2 `CI/CD Pipeline`. Zet dat in hetzelfde bestand:

```yaml
ci_workflow: "CI"
```

Volgorde: expliciet argument, dan `ci_workflow` in het project-YAML, dan `VNX_CI_WORKFLOW_NAME`, dan `VNX CI`. De deur, `vnx pre-merge-gate` en de `merge_preflight_ci_check`-CLI lezen het veld alle drie uit `main`'s YAML van de doelrepo, over de contents-API, via `forge_protection_drift.fetch_ci_workflow_from_main`. Nooit uit een lokale kopie: de checkout naast de gate staat op gate-tijd op de branch van de PR, dus een lokale kopie laat de PR zelf kiezen welke workflow hem toetst. Bestaat het bestand maar is het onleesbaar (of is `main` zelf niet te lezen), dan weigert de CI-poort in plaats van een naam te raden: `NO-GO` bij de deur en de CLI, `SKIPPED_UNVERIFIED` bij `pre_merge_gate`. Een expliciet argument (`--ci-workflow-name`, `--workflow`) of een override-reden beslist de poort al, en dan wordt `main` niet gelezen. `pre_merge_gate._resolve_ci_workflow_name` en `merge_preflight_ci_check._resolve_workflow_name` volgen dezelfde volgorde, en `tests/test_ci_workflow_resolution_parity.py` houdt ze daaraan.

**Het veld wijzigen is verzwakken.** Na de merge vraagt de CI-poort van elke volgende PR de workflow die `main` noemt. Een PR die het veld verwisselt, toevoegt of weghaalt kiest dus zelf de check waarop hij de volgende keer wordt getoetst. Alle drie vragen `--allow-weaken "<reden>"`, in elke stand: `enforce`, `warn` en `off` (operatorbesluit 25-09-2026, de CI-controle is de ondergrens voor iedereen). Onder `enforce` en `warn` gaat dat via `forge_protection_drift.is_weakening`, onder `off` en zonder bestand op `main` via `forge_protection_drift.ci_workflow_change`, dezelfde vergelijking. De melding noemt beide waarden: `ci_workflow: 'CI' -> 'Always Green'`, of `niet gedeclareerd` waar het veld ontbreekt. Toevoegen en weghalen tellen mee omdat een ontbrekend veld de poort aan `VNX_CI_WORKFLOW_NAME` en `VNX CI` geeft. Een project zonder bestand op `main` richt vrij in via de missing-file-tak van §11.3, met één uitzondering: declareert de PR die het bestand toevoegt `ci_workflow`, dan kiest die PR de workflow waar elke volgende PR op getoetst wordt, en dat vraagt `--allow-weaken "<reden>"`. De reden komt zo in het record. Onder `off` en zonder bestand leest de deur de PR-head daarvoor; een head die niet te lezen is, is een weigering.

### 11.5 Een consumer inrichten

1. Zet `.vnx/branch_protection.yaml` in het project. Begin met de stand die past bij wat er op GitHub staat: heeft de repo geen protection (sales-copilot, SEOcrawler_v2), kies dan `enforcement: warn`. Het schema is dat van `scripts/forge/branch_protection.yaml`, inclusief alle verplichte velden. Een minimaal voorbeeld staat in `tests/merge_target_helpers.py::consumer_yaml`.
2. Voeg `ci_workflow` toe als de workflow niet `VNX CI` heet. Dat vraagt altijd `--allow-weaken "<reden>"` bij de merge, ook in de PR die het bestand voor het eerst op `main` zet (§11.4), bijvoorbeeld `--allow-weaken "inrichting: workflow heet CI"`.
3. Bekijk wat er zou gebeuren, vanuit de projectmap: `python3 <install>/scripts/forge/apply_branch_protection.py --dry-run`. Het toont het PUT-object uit het project-YAML en werkt tegen de repo van het project.
4. Pas toe met `python3 <install>/scripts/forge/apply_branch_protection.py`. Het schrijft `doelrepo: owner/name` op stderr voordat het iets wijzigt en weigert als het die repo niet kan noemen.
5. Merge via `python3 <install>/scripts/pr_merge.py --pr <n>`, vanuit de projectmap of met `VNX_PROJECT_ROOT` gezet. Controleer de eerste regel: `doelrepo:`.
6. `vnx doctor` toetst dezelfde drift: `FAIL` onder `enforce`, `WARN` onder `warn`, en onder `off` meldt hij dat de toetsing uit staat.

### 11.6 Twee PR's voor het veld in vnx-orchestration zelf

De lezer van `enforcement` en `ci_workflow` staat sinds OI-1849 op `main`. Het veld staat bewust nog **niet** in `scripts/forge/branch_protection.yaml` van vnx-orchestration: zonder veld is het `enforce`, precies wat het bestand nu is, en een PR die lezer en veld samen levert komt door de regel van §10 niet door stap d. Wil vnx-orchestration het veld expliciet vastleggen, dan is dat een tweede PR.

## Cross-references

- Plan: `claudedocs/plans/2026-09-06-golf-B-forge-dwingt-af.md`
- Merge-deur en zijn gates: `docs/core/DISPATCH_RULES.md` §2
- Code op `main`: `scripts/forge/branch_protection.yaml`, `scripts/forge/apply_branch_protection.py`, `scripts/lib/forge_protection_drift.py`, `scripts/pr_merge.py::_run_branch_protection_gate`, `scripts/vnx_doctor.py::check_branch_protection_drift`
- Publicatielaag op `main`: `scripts/lib/forge_check_run.py` (client), `scripts/lib/forge_gate_publisher.py` (afbeelding, samenvattende check, CLI), `tests/test_forge_check_run_client.py`, `tests/test_forge_review_summary.py`
- Lezer-voor-veld-contract (OI-1672): §10 hierboven, bewaakt door `tests/test_branch_protection_gate_reader_for_field_doc.py`
- Doelrepo, strengheid en CI-workflow per project (OI-1849): §11 hierboven. Code: `scripts/lib/merge_target.py`, `scripts/pr_merge.py`, `scripts/lib/forge_protection_drift.py`, `scripts/pre_merge_gate.py`, `scripts/lib/merge_preflight_ci_check.py`, `scripts/forge/apply_branch_protection.py`. Tests: `tests/test_merge_target.py`, `tests/test_pr_merge_target_repo.py`, `tests/test_forge_protection_enforcement.py`, `tests/test_branch_protection_project_root.py`, `tests/test_ci_workflow_resolution_parity.py`
