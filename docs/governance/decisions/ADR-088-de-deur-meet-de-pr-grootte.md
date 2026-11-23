# ADR-088 - De merge-deur meet de PR-grootte, met een uitzondering per PR als inbox-antwoord

**Status:** Voorgesteld 18-11-2026 (track `pr-grootte-afgedwongen`, levering ADR, dispatch `dlv-66da10ebcabd`). Amendeert ADR-076 besluit 4 en besluit 6 via cross-ref; dat bestand blijft ongewijzigd (ADR-079 § 83).
**Datum:** 2026-11-18
**Besloten door:** T0, op de plan-gate van 18-11 (ronde 2: codex REVISE, opus REVISE, tiebreaker START met één verplichte wijziging, verwerkt als V2.1). Vlootregel van de operator van 09-11 (PR's 300 tot 450 regels, beveiligingspaden 300 of minder); operatorbesluit B-20261118-c767df96 (variant A: uitzondering per PR met plafond, reden en citaat, afgedwongen door de deur).
**Cross-refs:** ADR-007 (project_id in elke centrale tabel), ADR-056 (`actor_class`), ADR-061 § 3 (`door` als laatste preflight), ADR-063 (onleesbaar is `unknown`), ADR-076 (besluit 4 en 6, de operator-wachtrij), ADR-079 (§ 83, bestaande ADR's zijn bevroren); plan `claudedocs/specs/pr-grootte-afgedwongen/plan.md` (gitignored, niet in de repo), versie 1 plus § Versie 2 (V2.1 tot en met V2.11); track `pr-grootte-afgedwongen`.

## Context

Op 09-11 legde de operator een vlootregel vast: een PR is 300 tot 450 gewijzigde regels, toetsen meegeteld, met een harde grens van 450; beveiligings-, permissie- en auth-paden blijven op 300 of minder. Gemeten op 67 merges van 08-11: boven 450 sprong het aantal reviewrondes naar gemiddeld 2,9.

De code dwingt die regel niet af. Gemeten op `origin/main` `4e34cd5f`:

- De deur heeft zeven preflights: `ci`, `review`, `adr`, `contract_invalid`, `branch_protection`, `main`, `door` (`PREFLIGHT_ORDER`, `scripts/pr_merge.py:330`). Geen ervan meet grootte. #3011 mergede op 18-11 met 804 regels door de deur; de uitzondering daarvoor (B-20261118-9c374561) bestond alleen als inboxpunt en besluitregel.
- De enige groottemeting is `pre_merge_gate.check_pr_size` (`scripts/pre_merge_gate.py:643`). Zonder `base_ref` probeert zij `origin/main` en dan `origin/master` (`scripts/pre_merge_gate.py:519`, `:663`), dus een PR op een andere base wordt tegen de verkeerde branch gemeten. De deur roept haar niet aan.
- De grenzen zijn `VNX_PR_SIZE_WARN` 450 en `VNX_PR_SIZE_HOLD` 600 (`scripts/lib/config_registry.py:291`, `:297`). Er is geen handhavingssleutel en geen beveiligingsgrens.
- In code bestaat alleen `GOVERNANCE_PATH_MARKERS` (`scripts/lib/auto_merge_policy.py:11`) en `smart_router.security_path_kind` (`scripts/lib/smart_router.py:1122`). De 300-grens bestaat alleen als tekst in skills en CLAUDE.md.
- `config_registry.resolve_with_source` (`scripts/lib/config_registry.py:813`) leest `VNX_OVERRIDE_<BARE>` vóór de `project_config`-rij (`:832`), voor elke sleutel. De T0 die de deur draait, kan een handhavingssleutel dus met één env-variabele uitzetten.
- Bouwers mogen de hele Bash-tool en `python3*` (`.vnx/worker_permissions.yaml:51`, `:56`). "Een bouwer heeft geen inboxpunt" is daarom geen grens.

Dit ADR legt de besluiten vast voordat er code landt.

## Besluit

**1. Een preflight `pr_size`, na `ci` en vóór `review`.** `PREFLIGHT_ORDER` gaat van zeven naar acht namen. `pr_size` staat na `ci` en vóór `review`, zodat een te grote PR geen reviewronde meer kost; `door` blijft de laatste (ADR-061 § 3). De sleutel `VNX_PR_SIZE_ENFORCE` heeft drie standen, standaard `warn`:

| Stand | Gedrag |
|---|---|
| `refuse` | NO-GO boven de grens zonder geldige uitzondering |
| `warn` | meting op het merge-receipt, de merge gaat door |
| `off` | geen meting, `waived: pr_size` op het receipt |

Een waarde buiten deze drie leest als `refuse`, met een ERROR die waarde en bron noemt: bij een handhavingssleutel is streng de veilige kant. De grens is `VNX_PR_SIZE_HOLD_SECURITY` (standaard 300) zodra één gewijzigd pad een beveiligingspad is (besluit 8), anders `VNX_PR_SIZE_HOLD`. Het preflight-record en het merge-receipt dragen altijd `lines`, `limit`, `limit_kind` (`hold` of `security`), `security_paths_hit` (de eerste drie) en `mode`, en de grant als er een geldt (besluit 5).

**2. De meting: één functie, de eigen base van de PR.** `pr_size_policy.measure(pr, head_sha, base_branch, cwd)` is de enige meting, voor de deur en voor `--grant` (besluit 7). Zo kunnen deur en uitzondering niet over een ander getal beslissen.

- **Doelrepo:** `_target_root()` van de deur (`scripts/pr_merge.py:522`), dezelfde als bij de andere preflights. `headRefOid` en `baseRefName` komen uit de `gh pr view` die de deur al doet (`scripts/pr_merge.py:563`).
- **Base:** `review_risk_class.base_ref_for(baseRefName)` (`scripts/lib/review_risk_class.py:142`) en `fetch_merge_refs` (`scripts/lib/review_risk_class.py:498`), een alleen lezende fetch van head en base. Daarna `git merge-base`, en over één bereik `merge_base..head` zowel `git diff --numstat` als `git diff --name-only --no-renames -z`. Er is geen standaardbase; de terugval van `check_pr_size` op `origin/main` gebruikt de deur niet.
- **Regels:** toegevoegd plus verwijderd, toetsen meegeteld. Een binair bestand telt 0 regels en staat wel in de padlijst.
- **Meetfout** is elk van: geen `baseRefName`, een ongeldige branchnaam, een falende fetch, merge-base, numstat of name-only, een lege diff, of een onleesbare `gh`. Onder `refuse` geeft dat NO-GO `pr_size_unmeasured` met de stap die faalde. Onder `warn` wordt het een regel op het receipt.

**3. Een override maakt alleen strenger.** De lagen van `resolve_with_source` splitsen in twee soorten:

- **Vloer:** de lagen met een goedgekeurde schrijver. Dat is de `project_config`-rij (de operator via `/operator/config` met een approval-id) en, zodra config-een-oppervlak haar levert, de globale laag met een approval-id. Zonder waarde daar geldt de registerstandaard.
- **Losse lagen:** `VNX_OVERRIDE_<BARE>` en env `VNX_<BARE>`. Die mogen alleen strenger maken dan de vloer.

| Sleutel | Strenger is |
|---|---|
| `VNX_PR_SIZE_ENFORCE` | `refuse` boven `warn` boven `off` |
| `VNX_PR_SIZE_HOLD` | een lager getal |
| `VNX_PR_SIZE_HOLD_SECURITY` | een lager getal |
| `VNX_SECURITY_PATHS` | alleen toevoegen; een kortere lijst haalt niets weg uit de vaste set (besluit 8) |

Onder een vloer `refuse` geeft een losser makende waarde uit een losse laag NO-GO `pr_size_config_loosened`, met sleutel, bron, waarde en vloer. Onder een vloer `warn` of `off` geldt de losse waarde: er valt niets af te dwingen. Een strenger makende override geldt, met `source: override` op het receipt. Alle vier de sleutels krijgen `requires_approval=True`.

Het mechanisme is een parameter `layers` op `resolve_with_source`. De standaard is de hele keten, zodat voor elke andere lezer niets verandert; een toets bewijst dat de standaard gelijk is aan de ketenconstante, zodat een latere globale laag in beide landt. `pr_size_policy` leest de vloer met alleen de goedgekeurde lagen en de effectieve waarde met de hele keten, en vergelijkt.

Er komt nu geen generiek registerveld (zoals `override_direction` op `ConfigEntry`). Alleen deze vier sleutels hebben het nodig; het handhavingsprofiel van config-een-oppervlak laat een override naar `shadow` bewust toe en is dus geen tweede gebruiker. Komt er wel een, dan wordt het een registerveld in de seriële keten van `config_registry.py`.

**4. Wie losser maakt, en een onleesbare bron weigert.** Losser maken kan op twee manieren, allebei met een spoor van de operator:

- **per PR:** de uitzondering (besluit 5);
- **voor het hele project:** de operator zet de rij op `warn` via `/operator/config` met een approval-id, wat landt in `project_config_audit`.

Een meetfout onder `refuse` heeft dezelfde twee terugwegen. De T0 heeft geen eigen klep.

`pr_size_policy` leest de vier sleutels altijd met `strict_resolver=True`, dat bij een falende DB `(None, "project_config_error")` geeft (`scripts/lib/config_registry.py:842`-`846`); een latere `global_config_error` hoort daar ook bij. Een `*_error` op een van de vier geeft NO-GO `pr_size_config_unreadable` met sleutel en fout, ongeacht de grootte: de stand zelf is onbekend. Een onleesbaar inbox-grootboek levert nooit een uitzondering op; onder `refuse` en boven de grens geeft het NO-GO `pr_size_exception_unreadable`.

Dit wijkt bewust af van het handhavingsprofiel van config-een-oppervlak. Daar valt een onleesbare store door met een WARNING, omdat een falende hook elk gereedschap blokkeert. Een deur die weigert, kost één herhaalde run.

**5. De uitzondering is de antwoordregel.** De uitzondering is een veld `grant` op de `answer`-regel in het inbox-grootboek van het project (ADR-076 besluit 1): `{plafond, reden, head_sha, base_ref, regels, gemeten_op}`. Er is één gezaghebbende bron; versie 1 van het plan had een tabel naast het antwoord, twee stores zonder herstelcontract.

- **Atomair:** één regel, één append. Een antwoord zonder `grant` is een nee.
- **Idempotent:** `answer` weigert op een gesloten punt, en die ene regel sluit het punt. Een tweede `--grant` op hetzelfde punt kan niet.
- **Herhaalbaar:** faalt de meting of de append, dan is er geen regel en blijft het punt open.
- **Welke telt:** de grant van het `pr:<n>`-punt met de hoogste revisie die een grant draagt. Een hogere revisie ontstaat alleen voor een hoger plafond (besluit 6), dus een nee op een verhoging trekt een eerder plafond niet in. Een oudere revisie is als `vervallen` gesloten en kan niet meer beantwoord worden.
- **Geldig:** het punt bestaat, de ref is `pr:<n>` van dit project en deze PR, de regel draagt een citaat, de reden komt uit de gesloten set (`splitting_removes_tests`, `one_working_unit`, `generated`, `mechanical_rename`) en het plafond is een geheel getal van minstens de meting bij het vastleggen.
- **Geen bovengrens** op het plafond: het is het getal van de operator in het kopieerblok.

Onder `refuse`, boven de grens: plafond ≥ `lines` geeft GO met de grant op het receipt; anders NO-GO `pr_size_above_exception` (er is een grant, maar te laag) of `pr_size_no_exception`. De melding noemt regels, grens, plafond en het exacte `vnx inbox ask`-commando. De meting draait bij elke deur-run, dus een latere ronde boven het plafond wordt opnieuw geweigerd.

Er komt geen tabel en geen migratie, dus ADR-007 is hier niet van toepassing.

**6. Het inboxpunt `pr:<n>`, amendement op ADR-076 besluit 4 en 6.** ADR-076 besluit 4 kent zes ref-vormen (`FORMS`, `scripts/lib/operator_queue_ask.py:29`); besluit 6 laat `answer` alleen toe op `antwoord:` (`scripts/lib/operator_queue_ask.py:82`). Dit ADR voegt een zevende vorm toe en een tweede uitzondering op besluit 6:

| `ref` | Beantwoord als | Vervallen als |
|---|---|---|
| `pr:<n>` | een `answer`-regel voor het punt, met of zonder grant | de PR staat op `MERGED` of `CLOSED` in `gh pr view`, of er is een hogere revisie geopend |

- `pr:<n>` gaat alleen met bron `besluit`. `ask` eist `--plafond` (geheel getal boven 0) en `--reden` (de gesloten set); beide staan op de `opened`-regel.
- Geen bronstaat sluit een `pr:`-punt als `beantwoord`; alleen een `answer`-regel doet dat. Dat is de les van OI-2995, waar een `dlv:`-punt 83 s na openen dichtging op de dispatchstaat.
- Een onleesbare `gh` geeft `unknown`, en het punt blijft open (ADR-063).
- **Heraanvraag:** `ask` voor `pr:<n>` met een hoger plafond dan het laatste punt opent revisie +1 en sluit de vorige als `vervallen` ("vervangen door revisie N"), zoals `operator_queue_steering_verdict` al doet. Hetzelfde of een lager plafond opent geen nieuw punt.

Het kopieerblok voor de operator is `uitzondering <n> akkoord, <plafond> regels`. ADR-076 blijft ongewijzigd; besluit 4 en 6 gelden voortaan met deze toevoeging.

**7. Wie `--grant` mag.** `vnx inbox answer <id> --quote "..." --grant` hergebruikt de grens van promote: `actor_class.derive()` (`scripts/lib/actor_class.py:72`, ADR-056). Toegestaan voor `operator` en `t0`; geweigerd voor `worker`, `automation` en `unknown`, met exit 2 en zonder schrijven. Daarnaast weigert `--grant`:

- een punt dat niet `pr:<n>` als ref heeft;
- een punt zonder `presented`-regel op een ander kanaal dan `cli`;
- een leeg citaat of een citaat boven 300 tekens (`MAX_QUOTE`, `scripts/lib/operator_queue_ask.py:37`);
- een PR die niet `OPEN` is;
- een plafond onder de meting op dat moment, of een meting die faalt (besluit 2). `head_sha` in de grant komt uit precies die meting.

*Grens, aanvaard:* `actor_class` leest omgevingsmarkers. Een bouwer die zijn markers vervalst, of het grootboek via Bash rechtstreeks beschrijft, kan ook een grant schrijven, zoals hij ook kan promoveren of elk ander operatorbesluit in de store kan nabootsen. Dit is de bestaande grens van promote, geen nieuwe. Het spoor (punt, presentatie, antwoord met citaat, receipt) blijft na te lopen. Een getekende uitzondering sluit de grens, als aparte track.

**8. De beveiligingspaden komen uit code.** Een pad is een beveiligingspad als een van deze vier geldt, in deze volgorde:

1. `smart_router.security_path_kind(p)` geeft een soort (`scripts/lib/smart_router.py:1122`, prefixen in `_SECURITY_PREFIXES`, `:973`);
2. het pad valt onder `auto_merge_policy.GOVERNANCE_PATH_MARKERS` (`scripts/lib/auto_merge_policy.py:11`);
3. het pad valt onder de vaste set van deze track, `PR_SIZE_SECURITY_EXTRA` in `scripts/lib/pr_size_policy.py`: `scripts/pr_merge.py`, `scripts/lib/pr_size_policy.py`, `scripts/inbox_cli.py`, het prefix `scripts/lib/operator_queue`, `scripts/lib/actor_class.py`, `scripts/lib/config_registry.py`, `scripts/lib/config_store_db.py`, `scripts/lib/config_global.py`, het prefix `scripts/lib/attest`, `scripts/lib/delegation_mandate.py`, `scripts/lib/worker_permissions.py` en `schemas/`;
4. het pad valt onder `VNX_SECURITY_PATHS`, een projectuitbreiding die alleen toevoegt.

De set wordt bij het lezen samengesteld uit die constanten, niet overgetypt, zodat er geen kopie is die afdrijft. De eigen schrijvers van de handhaving (deur, policy, inbox, actorklasse, config) staan erin. De classificatie gebruikt de padlijst van de meting van besluit 2, dus hetzelfde bereik.

**9. `VNX_PR_SIZE_HOLD` gaat van 600 naar 450.** De vlootregel is gemeten en niet operatorspecifiek, dus een buitenstaander krijgt hem ook. Gemeten wie het oordeel leest: de deur roept `check_pr_size` niet aan, en `codex_final_gate.py`, `required_contexts_gate.py`, `ledger_health.py` en `smart_router.py` noemen `pre_merge_gate` alleen in commentaar, een constante of een padtabel. De enige lezer van het HOLD-oordeel is `vnx gate-check` (`bin/vnx:2743`, `vnx_cli/commands/gate_check.py`). Een PR van 451 tot 600 regels krijgt daar voortaan HOLD in plaats van WARN. `VNX_PR_SIZE_WARN` blijft 450 en valt samen met HOLD, zodat gate-check geen zachte zone boven 450 meer kent.

## Gevolgen

**De leveringen bouwen en toetsen, elk hooguit 300 regels met een rode run op main:**

- PG-0: de vier sleutels met `requires_approval`, HOLD 450, de parameter `layers`; `tests/test_pre_merge_gate.py` groen.
- PG-1: `pr_size_policy.py` met `measure`, de vloer- en losser-regel en onleesbaar-weigert. Rode toetsen: rij `refuse` plus `VNX_OVERRIDE_PR_SIZE_ENFORCE=off` of `warn` geeft `pr_size_config_loosened`; rij `refuse` plus `VNX_OVERRIDE_PR_SIZE_HOLD=99999` of env `VNX_PR_SIZE_HOLD=99999` geeft NO-GO; `VNX_OVERRIDE_PR_SIZE_HOLD=400` geldt, en 420 regels geeft NO-GO.
- PG-1d: `pr_size` in `PREFLIGHT_ORDER`, de recordvelden en het receipt.
- PG-2: besluit 8, met een voorbeeldpad per prefix dat `limit_kind=security` geeft en de kalibratie over de laatste 67 merges in de PR-tekst. Valt meer dan de helft onder 300, dan legt de T0 dat aan de operator voor vóór het aanzetten.
- PG-3a, PG-3b, PG-3c: het veld `grant`, de ref-vorm `pr:<n>` met revisies, en `answer --grant` met de weigeringen van besluit 7.
- PG-4: de deur leest de grant.

Per tak die doorlaat of uitsluit een toets met het veld ontbrekend, `None`, misvormd en een onbekende waarde; elk blijft blokkerend.

**Aanzetten is een operatorhandeling, geen levering.** Een inboxpunt `config:VNX_PR_SIZE_ENFORCE`, de operator zet `refuse` via `/operator/config`, en de T0 meet met `pr_merge.py --dry-run` op een draft-PR boven 450 regels: eerst zonder grant (NO-GO), dan met (GO).

**Wat dit ADR niet toestaat:**

- een omgevingsvariabele die de handhaving onder een vloer `refuse` losser maakt;
- een uitzondering buiten de `answer`-regel van een `pr:<n>`-punt, of een grant door `worker`, `automation` of `unknown`;
- een meting tegen een andere base dan de `baseRefName` van de PR;
- een onleesbare bron of meetfout die onder `refuse` doorlaat.

**Amendementen.** ADR-076 besluit 4 (een zevende ref-vorm `pr:<n>`) en besluit 6 (`answer` ook op `pr:<n>`, en met `--grant`) gelden voortaan zo. Het bestand van ADR-076 verandert niet.

## Alternatieven

| Alternatief | Waarom niet |
|---|---|
| Een tabel `pr_size_exceptions` in `runtime_coordination.db` naast het inbox-antwoord (versie 1 van het plan) | Afgewezen: twee stores zonder herstelcontract. Valt de tabelschrijf weg na het antwoord, of andersom, dan zegt de ene store ja en de andere niets. Het trackdoel zegt "vastgelegd via een inbox-antwoord"; de antwoordregel is dat letterlijk |
| Een getekende uitzondering via `attest_override.py` | Voorlopig afgewezen: het attestatiemechanisme werkt maar is nooit in gebruik genomen, en variant A (B-20261118-c767df96) koos het citaat. Zodra getekende attestaties in gebruik zijn, kan `--grant` een handtekening eisen; dat sluit ook de grens van besluit 7 |
| Een noodklep `VNX_OVERRIDE_PR_SIZE_ENFORCE=warn` voor één deur-run | Afgewezen: de T0 die de deur draait, kan hem zelf zetten en zo de handhaving omzeilen. Losser maken loopt via de operator (besluit 4) |
| Het additions-veld van GitHub als meting | Afgewezen: het hoort bij wat GitHub op het moment van vragen als kop ziet, niet bij de vastgepinde `head_sha` van de deur, en het levert geen padlijst voor besluit 8 uit hetzelfde bereik. Een meting in de eigen checkout is na te rekenen |
| `pre_merge_gate.check_pr_size` hergebruiken zoals zij is | Afgewezen: zonder `base_ref` valt zij terug op `origin/main`, dus een PR op een andere base wordt tegen de verkeerde branch gemeten |
