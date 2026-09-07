# FORGE_GATE.md — vnx-gate: van lokale review-poort naar afgedwongen check-run

Operator-runbook voor golf B (`gate-enforcement-to-forge`). Plan: `claudedocs/plans/2026-09-06-golf-B-forge-dwingt-af.md` (deliverables B4, OP-B3, B5). Doel: GitHub weigert elke merge naar `main` zonder een gepubliceerd poortoordeel op exact de PR-head, ook een kale `gh pr merge`, ook van de operator zelf.

## 0. Wat bestaat er vandaag, wat nog niet (gemeten 2026-09-07)

Dit runbook beschrijft twee lagen.

| Laag | Status op 2026-09-07 | Commando's in dit runbook |
|---|---|---|
| **1. Branch-protection als YAML** (B1, #1807, **gemerged**) — `scripts/forge/branch_protection.yaml`, `scripts/forge/apply_branch_protection.py`, `scripts/lib/forge_protection_drift.py`, de preflight in `scripts/pr_merge.py`, de doctor-check in `scripts/vnx_doctor.py` | **Op `main`.** | Geverifieerd tegen elk script zijn eigen `--help`, en tegen een echte `gh api`-meting op de repo (zie §4). |
| **2a. Forge-client** (`scripts/lib/forge_check_run.py`) | **Nog niet op `main` — open als PR #1808** (`dispatch/20260907-golfb-b2a-forge-client`, tijdens het schrijven van dit runbook geopend; Profile A stond nog rood). Deze PR bevat óók de YAML-uitbreiding uit §3 en een aanpassing van `forge_protection_drift.py`'s schema. | Geverifieerd tegen de broncode van PR #1808 zelf (niet tegen `--help`: de client heeft **geen CLI**, alleen library-functies — zie §2 en §5). Herlees de PR bij twijfel, want hij is nog niet gereviewd. |
| **2b. Integratie in de gate-recorder + `vnx-gate/review` als verplichte check** (B2b, B3, OP-B3, B5) | **Niet gebouwd.** Geen `--pr`-CLI, geen allowlist-afbeelding van poortstatus naar `conclusion`, geen `pending_checks`-entry voor `vnx-gate/review`. | **Ontwerp uit het plan, niet uit code.** Elke vlag hierop in §5 en §8 is gemarkeerd **(ontwerp)** en moet herverifieerd worden zodra B2b/B3 mergen — vóór uitvoering. |

**Meetcorrectie op de aanname achter §4:** de dispatch-instructie voor dit runbook noemt de eerste apply als het moment waarop de bescherming ontstaat. Gemeten op 2026-09-07 via `gh api repos/Vinix24/vnx-orchestration/branches/main/protection` (+ `.../required_signatures`, `repos/{owner}/{repo}`, `.../rulesets`) staat de bescherming al live en komt hij op elk veld overeen met `branch_protection.yaml` (14/14 checks, `enforce_admins: true`, `allow_auto_merge: false`, `required_signatures: false`, 0 rulesets) — precies zoals de YAML's eigen kopregel zegt ("Measured live ... on 2026-09-07"). Een `--dry-run` bevestigt dit: `verdict: DRY-RUN, changed: False, diffs: []`. De eerste apply-run is dus vandaag een **no-op**: hij schrijft de eerste receipt en bewijst idempotentie, hij legt de bescherming niet voor het eerst vast — die staat er al.

**Meetcorrectie op de aanname achter §3:** de dispatch-instructie gaat ervan uit dat het App ID op de `pending_checks`-entry zelf komt te staan, zoals bij `checks[]` (`{context, app_id}`). Gemeten in PR #1808: `pending_checks` blijft een lijst van kale context-strings (`parse_protection_config` eist `all(isinstance(p, str) ...)`), en het App ID komt in een **apart, nieuw top-level `app:`-blok** te staan dat de client leest, nooit toegepast en nooit vergeleken wordt voor drift. Zie §3.

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

**Geverifieerd tegen de broncode van PR #1808** (`scripts/lib/forge_check_run.py`, nog niet gemerged — zie §0). De client leest twee keychain-items via exact hetzelfde patroon als de bestaande `vnx-smtp-pass`-lezer (`scripts/send_digest_email.py::_read_smtp_pass_from_keychain`), maar faalt — in tegenstelling tot die lezer — altijd LUID in plaats van een lege string terug te geven: een leeg keychain-item zou een check-run stil laten verdwijnen in plaats van de publicatie te laten falen, en dat is precies wat deze poort moet voorkomen (module-docstring van `forge_check_run.py`).

De twee itemnamen liggen al vast in de code (`KEYCHAIN_PRIVATE_KEY_SERVICE`, `KEYCHAIN_INSTALLATION_ID_SERVICE`):

```bash
security add-generic-password -s vnx-gate-app-key -a "$USER" -w "$(cat /pad/naar/vnx-gate.pem)"
security add-generic-password -s vnx-gate-installation-id -a "$USER" -w "<installation-id>"
rm /pad/naar/vnx-gate.pem
```

Verwijder de `.pem` van schijf zodra hij in de keychain staat — dat is de enige kopie die overblijft.

**Toets (werkt zodra PR #1808 op `main` staat — de client heeft geen CLI, dus dit is een directe library-aanroep, geen subcommando):**

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

**Geverifieerd tegen PR #1808**, niet tegen B3 (B3 bestaat nog niet — zie de meetcorrectie in §0). PR #1808 voegt zelf al een nieuw top-level blok toe aan `scripts/forge/branch_protection.yaml`:

```yaml
app:
  slug: vnx-gate
  app_id: null
```

Dit blok is bewust **geen** onderdeel van `checks[]` of `pending_checks:`. Het beschrijft WIE publiceert, niet wat `main` vereist: `forge_protection_drift.py` behandelt `app` als een expliciet toegestane maar genegeerde top-level sleutel (`_OPTIONAL_TOP_LEVEL_FIELDS = frozenset({"app"})`) — nooit onderdeel van `ProtectionConfig`, nooit van `to_normalized_dict`, dus een wijziging hier kan nooit als drift of als verzwakking geregistreerd worden. Elke andere onbekende top-level sleutel wordt nog steeds geweigerd; dit is een allowlist van precies één sleutel, geen opening van het schema.

Zodra PR #1808 op `main` staat: vervang `app_id: null` door het echte App ID uit stap 1, via een kleine PR door de normale deur (glm-gate tekent). `app_id` blijft `null` totdat dit gebeurt — de client weigert expliciet en luid op `null` in plaats van te gokken (`load_app_config`'s eigen foutmelding verwijst naar dit runbook).

Dit is nog niet de `pending_checks`-entry voor `vnx-gate/review` zelf (die komt uit B3, nog niet gebouwd) — dit is alleen de identiteit die straks *publiceert*. Zie §0.

## 4. Eerste apply **[operator]**

Onafhankelijk van stap 1-3 en van PR #1808: dit gaat over de bestaande, gemergede 14-checks-YAML van B1.

```bash
python3 scripts/forge/apply_branch_protection.py --dry-run
```

Toont het volledige PUT-object dat zou worden verstuurd. Per OP-B3 leest een tweede lezer (opus-leeszetel) dit object voordat een toekomstige, echt-wijzigende apply draait; deze eerste run is een no-op (zie de meetcorrectie in §0), dus die tweede lezer is hier niet vereist.

```bash
python3 scripts/forge/apply_branch_protection.py
```

Verwacht bericht vandaag: `nul wijzigingen: live branch-protection komt al overeen met de YAML`. Controleer de receipt:

```bash
grep branch_protection_applied "$VNX_DATA_DIR/state/t0_receipts.ndjson" | tail -1
```

En bevestig dat `vnx doctor` nul drift toont:

```bash
vnx doctor
```

`branch_protection_drift` moet `PASS` zijn.

## 5. OP-B3 — entry van `pending_checks` naar `checks[]`, en migratie van openstaande PR's

**Operator-stap met eigen poort — nog niet uitvoerbaar (B2b en B3 ontbreken, zie §0).** Verwacht verloop zodra ze er zijn:

1. Kleine PR: `vnx-gate/review` toevoegen aan `pending_checks:` (als kale string, zie §0) met het App ID uit §3 elders vastgelegd (via B3's eigen mechanisme — nog niet ontworpen op het niveau van broncode).
2. `python3 scripts/forge/apply_branch_protection.py --dry-run` — het exacte PUT-object, gequoteerd in de PR.
3. Opus-leeszetel leest dat object vóór de apply (dit is de meest consequente mutatie van de golf: vanaf hier faalt elke merge zonder gepubliceerde check).
4. `python3 scripts/forge/apply_branch_protection.py` (zonder `--dry-run`) — dit voegt een check toe, dus is per definitie geen verzwakking en heeft geen `--allow-weaken` nodig.
5. **Migratie van openstaande PR's**, zodat geen enkele open PR na deze apply plotseling onmergebaar wordt zonder duidelijke reden:

   ```bash
   gh pr list --state open --json number,headRefOid --jq '.[] | "\(.number) \(.headRefOid)"'
   ```

   Per head **(ontwerp — B2b levert de `--pr`-resolutie en de allowlist-afbeelding; `forge_check_run.py` van vandaag (PR #1808) is uitsluitend `publish_check_run(head_sha, name, conclusion, summary)` als primitief, zonder CLI, zonder kennis van PR-nummers of poortstatus — zie module-docstring "The CLIENT layer, and deliberately nothing more")**:

   ```
   <het commando dat B2b levert> --pr <n>
   ```

   Een head zonder eerdere poortrun (geen schijf-record voor die PR plus poort) levert volgens het plan terecht `action_required` op en blijft geblokkeerd — dat is geen bug in de migratie, dat is de poort die zijn werk doet. Draai de review-poort eerst op die head, publiceer daarna opnieuw.

## 6. Werkgevolg voor iedereen, na OP-B3

Elke push naar een PR-branch **na** een poortrun maakt de PR onmergebaar totdat de poort opnieuw draait — ook een triviale docs-fixup na een groene poort. De volgorde wordt: `request` → `execute` → `glm_gate.py` (of de opvolger in de overnameketen) → publicatie → merge. Wat vóór OP-B3 nog kon (een groene poort, dan nog een klein commit, dan mergen) kan daarna niet meer zonder de poort opnieuw te draaien.

## 7. Terugweg **[operator]**

De terugweg is een branch-brede actie op de bestaande, geverifieerde `apply_branch_protection.py` — geen nieuwe code nodig, ook niet zodra OP-B3 gedraaid heeft. B5 voert hem één keer uit en quoteert de API-feiten ervan en ervoor.

1. Kleine PR: haal de `vnx-gate/review`-entry uit `checks[]` van `scripts/forge/branch_protection.yaml` (terug naar `pending_checks:`, of helemaal weg).
2. `python3 scripts/forge/apply_branch_protection.py --dry-run` — dit is nu een verzwakking (een check minder), dus de dry-run toont ook de waarschuwing `zou een verzwakking zijn zonder --allow-weaken`.
3. Apply met de reden verplicht ingevuld:

   ```bash
   python3 scripts/forge/apply_branch_protection.py --allow-weaken "vnx-gate/review teruggedraaid: <reden>"
   ```

   Een lege reden wordt geweigerd (`run_apply` toetst expliciet op een niet-lege string) — er is geen stille bypass.
4. Receipt controleren zoals in §4; het veld `weak_fields` in die receipt draagt de naam van de teruggedraaide check.
5. Zodra het probleem verholpen is: dezelfde entry via een nieuwe PR terug in `checks[]`, opnieuw apply (nu weer een aanscherping, geen `--allow-weaken` nodig) — zie §5.

## 8. Faalmodi

- **Gesloten keychain in een niet-interactieve sessie.** Na OP-B3 vraagt elke merge een lokaal gedraaide publisher met keychain-toegang. Zonder die toegang: geen publicatie, dus geen merge — ook niet voor de PR die het repareert. Geverifieerd (PR #1808, §2): elke keychain-fout is luid en draagt het exacte herstelcommando (`security add-generic-password -s <item> -a "$USER" -w '<...>'`); geen stille lege string. Als dat herstel zelf niet lukt (keychain blijvend ontoegankelijk, bijvoorbeeld een niet-interactieve launchd-sessie zonder sessiesleutel — `security`'s exit 51, "wil geen niet-interactieve toegang"), is de terugweg uit §7 de enige echte uitweg: haal `vnx-gate/review` uit `checks[]`, merge, zet daarna terug.
- **Verwijderde of gedeinstalleerde App.** `vnx-gate/review` is dan permanent onvervulbaar — geen enkele publicatie kan nog slagen. De terugweg uit §7 is het enige herstel; daarna de App opnieuw aanmaken (§1) en de hele keten opnieuw doorlopen.
- **Fork-PR.** De repo is publiek en er is nog geen externe PR geweest (gemeten: `POST /check-runs` op de head-sha van een fork-PR in de base-repo is dus niet getoetst). Aanvaarde procedure: de operator publiceert vanaf het schijf-record zoals bij elke andere PR. Faalt dat met **422**: er is geen per-PR-override in `apply_branch_protection.py` (het werkt branch-breed, niet per PR) — het herstel is de branch-brede terugweg uit §7, die ene PR mergen, en de bescherming direct daarna weer aanzetten. De eerste externe PR is zo een meting, geen storing.
- **launchd-runner met kaal PATH (OI-1663).** Een runner zonder de interactieve-shell-PATH boekt bijvoorbeeld codex als niet-geïnstalleerd. Voor deze poort betekent dat: als de publicatie via launchd draait, controleer eerst of `gh` en `security` op het PATH van die launchd-sessie staan vóór je een publicatiefout aan de keychain of de App toeschrijft.
- **Drift in een week zonder merges.** Niemand hoeft te mergen om drift te veroorzaken — een handmatige wijziging in de GitHub-UI verandert de live protection zonder dat de YAML meebeweegt. `vnx doctor`'s `branch_protection_drift`-check (§4) vangt dit ook buiten de merge-deur om; draai hem periodiek, niet alleen rond een merge.

## 9. Geaccepteerd restrisico

Letterlijk uit het plan (`claudedocs/plans/2026-09-06-golf-B-forge-dwingt-af.md`, sectie "Geaccepteerd restrisico"): de bescherming is bewerkbaar door precies de operator die hem bindt, en op een persoonlijk account is er geen audit-log-API — een protection-wijziging gevolgd door een kale merge laat niets opvraagbaars achter. Wie de private key in de keychain heeft, kan bovendien met een paar regels `curl` een `success` op elke sha zetten, langs `forge_check_run.py` heen; dezelfde operator, hetzelfde risico. Compenserende maatregelen: de drift in de deur (`pr_merge.py`) en in `vnx doctor` (§4, §8), en de receipt die apply en terugweg zelf schrijven (§4, §7). Wat overblijft is een operator die bewust drie stappen zet; dat risico is aanvaard en staat hier opgeschreven, niet weggewerkt.

## Cross-references

- Plan: `claudedocs/plans/2026-09-06-golf-B-forge-dwingt-af.md`
- Merge-deur en zijn gates: `docs/core/DISPATCH_RULES.md` §2
- Code op `main`: `scripts/forge/branch_protection.yaml`, `scripts/forge/apply_branch_protection.py`, `scripts/lib/forge_protection_drift.py`, `scripts/pr_merge.py::_run_branch_protection_gate`, `scripts/vnx_doctor.py::check_branch_protection_drift`
- Code in open PR #1808 (nog niet gereviewd, kan veranderen): `scripts/lib/forge_check_run.py`, `tests/test_forge_check_run_client.py`
