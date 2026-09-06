# ADR-039 — De claude-lane stopt met zelf-genezende synthese; nooit-succes wordt een gedeelde invariant

**Status:** Accepted (dispatch 20260906-oi1637-synthese-asymmetrie)
**Date:** 2026-09-06
**Decided by:** system-architect worker, op basis van de golf-2-meting van 05-09 en een eigen hermeting op de centrale store van 06-09.
**Resolves / Cross-refs:** OI-1637 (deze ADR + de code), OI-1638 deel 2 (het smalle "26 wrapper-successen"-defect, deels bevestigd, deels weerlegd — zie sectie 4).

## Context

`claudedocs/2026-09-05-golf2-contractpercentage-per-lane.md` (T0, golf-2) wees een asymmetrie aan tussen de twee governance-seams:

- `scripts/lib/dispatch_govern.py` (de tmux/claude-lane): als er geen geldig, worker-geschreven rapport bestaat, **fabriceert** `_synthesize()` er zelf een — vóór deze ADR met de volledige `## Summary/## Changes/## Verification/## Open Items`-kopstructuur, gevuld met git-bewijs (commit-boodschap, `diff --stat`). Die body haalde `validate_body()` altijd, ook al had de worker niets afgeleverd.
- `scripts/lib/envelope_govern.py` (kimi/glm-harness/deepseek-harness/codex/claude_headless): geen synthese. Ontbreekt een rapport, dan schrijft `governance_emit.emit_unified_report` de generieke `## Response`-wrapper, en die haalt `validate_body()` nooit.

Gemeten effect (golf-2, alleen bouw-dispatches): claude tmux-mét-synthese 100,0% contractvolledig tegen claude envelope-zonder 90,9% — een verschil van 9,1 punt op een populatie van 41 gesynthetiseerde rapporten. **Het contractpercentage van de claude-lane meette dus "heeft governance het gerepareerd", niet "heeft de worker het afgeleverd"** — een andere claim dan elke andere lane maakt.

## Twee richtingen, en waarom (b)

**Optie (a) — trek de synthese naar de envelope**, zodat alle lanes hetzelfde zelf-genezende gedrag krijgen. Bij onderzoek blijkt dit niet haalbaar binnen deze dispatch se eigen grens, ongeacht bestandsgrens-toewijzing: kimi, deepseek-harness en glm-harness lopen NIET via `envelope_govern._govern()`. Ze routeren via `provider_dispatch.py`'s `_dispatch_kimi`/`_dispatch_deepseek_harness`/`_dispatch_glm_harness` naar `_emit_governance()` — een derde, eigen governance-functie die `validate_body()` nooit aanroept. Optie (a) zou dus **ook** `provider_dispatch.py` moeten wijzigen, een bestand dat in deze dispatch aan niemand is toegewezen (niet aan mij, niet aan een van de genoemde parallelle workers). Git-diff-synthese vereist bovendien een worktree/base_sha die `EnvelopeSpec` voor de provider-lanes niet draagt.

**Optie (b) — schaf de zelf-genezende vorm af.** Behoud de git-bewijsverzameling (commit-boodschap, diff-stat, delivery-verdict) — die heeft audit-waarde — maar stop met hem verpakken in een vorm die de contractcheck voor de gek houdt. Straal: 41 historische rapporten (golf-2), en gemeten consumenten van de huidige vorm (`grep -rn SYNTHESIZED_REPORT_MARKER scripts/` en `grep -rn "contract_status.*synthesized" scripts/`, buiten tests/ en dispatch_govern.py zelf): `plan_gate_panel.py`, `plan_gate_tiebreaker.py` (marker-detectie, kop-vorm-onafhankelijk), `plan_gate_panel_effectiveness.py` en `tmux_interactive_dispatch.py` (beide lezen alleen de `contract_status`-STRING, niet de kopstructuur). Geen van de vier consumenten breekt op een kopwijziging.

**Besluit: optie (b).** `_synthesize()` behoudt zijn git-afgeleide inhoud maar verpakt die nu onder één `## Response`-kop (dezelfde vorm als de envelope-wrapper) in plaats van de vier contractkoppen. Een gefabriceerd rapport faalt nu `validate_body()` net als de wrapper van elke andere lane — het bestand meet weer wat het belooft.

### Welke statuswaarde: `contract_invalid`, niet `failed`

De rode-testeis in de dispatch liet twee waarden open, mits "een waarde die de rest van de keten al kent". `contract_invalid` is al canoniek in `event_outcome_semantics._STATUS_VOCABULARY` (categorie `failure`), al geschreven door `report_to_receipt_converter.py` en al gelezen door `stop_conditions.py`, `contract_invalid_window.py`, `receipt_classifier.py`. `envelope_govern.py` gebruikt hem al sinds c28b7ac4 (2026-08-09, OI-1017/OI-1048). Dispatch_govern.py's oude `"failed"` conflateert twee verschillende beweringen ("er is gewerkt en het is misgegaan" vs. "er is geen rapport, dus geen bewijs") onder één label. `contract_invalid` isoleert de tweede bewering en sluit meteen aan bij het label dat de rest van de vloot al voor precies dit geval gebruikt — dit is de vocabulaireconvergentie die de asymmetrie ooit veroorzaakte.

`"violated"` (contract_status) blijft gereserveerd voor een WORKER-geschreven rapport dat het contract niet haalt — nooit voor een gefabriceerd rapport. Zonder die knip zou elke synthese voortaan "violated" heten (want een gefabriceerd rapport haalt de contractcheck nu per ontwerp nooit meer), en dat wist het "er is geen rapport"-signaal waar de marker-consumenten op leunen.

## Wat golf-2's eigen "26" echt zijn (gemeten 06-09, niet aangenomen)

De dispatch droeg de aanname dat het smalle 26-defect (12 kimi, 10 deepseek-harness, 4 claude — wrapper-rapport met receipt-status `success`) in `envelope_govern.py` zit. Hermeting op de levende centrale store (`~/.vnx-data/vnx-dev/unified_reports/` + `state/t0_receipts.ndjson`, dezelfde methodiek als golf-2: alleen bouw-dispatches, wrapper-vorm, laatste receipt-record) geeft **37 van de 38** gevonden gevallen (kimi 16, deepseek-harness 14, claude 6, glm-harness 1) van vóór 2026-08-09 — de commit die de bindende contractcheck op de envelope-lanes zette (c28b7ac4). Vier van de zes "claude"-gevallen zijn bovendien `test-*`-dispatch-ID's: testfixture-vervuiling in de gedeelde store, geen echte dispatches. **Eén** geval (`20260904-kimi-saldo-probe`, 04-09) is recent — en loopt, net als alle kimi/deepseek-harness-gevallen, via `_emit_governance()`/`provider_dispatch.py`, niet via `envelope_govern.py`.

Dit is een derde aanname-fout in dezelfde keten als golf-2's eigen twee (sectie 6 van dat document): **de aanname was gebaseerd op WAAR de contract-check hoort te zitten, niet op waar hij bewezen draait.** Voor kimi/deepseek-harness/glm-harness is de enige correctiemechanisme dat wél werkt de asynchrone `report_to_receipt_converter.py`-scan (`event_type=report_contract_invalid`), niet `envelope_govern.py:256`. Dat bestand ligt buiten deze dispatch se bestandsgrens (niet aan mij toegewezen, niet aan een genoemde sibling-worker) en buiten deze ADR se scope — zie Open Items.

**Wat wél binnen scope leefde, en gefixt is:** `envelope_govern._govern()`'s eigen idempotentie-wachter (`_receipt_exists_for_dispatch`, bedoeld om `deliver_with_recovery`'s legacy safety-net-receipt niet dubbel te schrijven) sloeg de zojuist berekende contract-invalid-downgrade helemaal over zodra er al een receipt-regel voor dat dispatch_id bestond — ongeacht of die eerdere regel `success` beweerde en de zojuist gelezen rapport-body dat weersprak. Een reproduceerbare (zij het in de huidige levende data niet aangetroffen — de enige lane die dit pad vandaag daadwerkelijk gebruikt is `claude-subprocess`, en die routeert in productie niet via `provider_dispatch.main()`) maar structurele hard-invariant-lek. Gefixt: bij een bestaande receipt ÉN een gedetecteerde contract-downgrade wordt nu alsnog een correctieve regel toegevoegd in plaats van de skip.

## Beslissing (samengevat)

1. `dispatch_govern._synthesize()` verpakt zijn git-bewijs onder `## Response`, niet meer onder de vier contractkoppen. `validate_body()` faalt er nu altijd op, symmetrisch met de envelope-wrapper.
2. `contract_status` blijft `"synthesized"` voor een gefabriceerd rapport, ook als het de contractcheck faalt — alleen een WORKER-geschreven rapport dat faalt wordt `"violated"`.
3. De receipt-status voor een niet-`authored` dispatch_govern-rapport wordt `report_body_contract.CONTRACT_INVALID_STATUS` (`"contract_invalid"`) — gedeeld met envelope_govern.py, niet langer het eigen `"failed"`.
4. `dispatch_govern._AUTHORITATIVE_STATUSES` krijgt `"contract_invalid"` erbij, zodat de dedup-prioriteit van een niet-geleverd rapport niet daalt door de naamswijziging.
5. `envelope_govern._govern()`'s idempotentie-skip persisteert een contract-invalid-downgrade ook wanneer er al een receipt bestaat, in plaats van hem stilzwijgend te laten vallen.
6. `_govern_error_fallback` (het interne-fout-pad) blijft ONGEWIJZIGD — bewust: dat pad stampt al `"failed"` (nooit `"done"`/`"success"`) via `ensure_receipt`'s eigen fallback-afleiding, draagt geen gemeten aandeel in de 9,1-punts-inflatie, en is een zeldzaam defensief pad. De hard-invariant hield daar al.

## Consequenties

- **Positief.** Het claude-tmux-lane contractpercentage meet voortaan hetzelfde als elke andere lane: "heeft de worker afgeleverd", niet "heeft governance het opgelapt". Een toekomstige golf-3-meting kan claude-tmux en claude-envelope zonder correctie naast elkaar leggen.
- **Positief.** De harde `nooit-success-op-een-ongeldige-body`-invariant is nu getest (`tests/test_govern_wrapper_never_success.py`) op de plek waar hij structureel kon lekken (idempotentie-skip), niet alleen op het hoofdpad.
- **Risico, expliciet aanvaard.** De marker-consumenten (`plan_gate_panel.py`, `plan_gate_tiebreaker.py`) hebben eigen testfixtures die de OUDE (vier-koppen) synthese-vorm hand-matig reproduceren (`tests/test_plan_gate_panel.py::_make_synthesized_report`, `tests/test_plan_gate_tiebreaker.py::_synthesized_report`). Die tests blijven groen (classificatie is marker-string-gebaseerd, niet kop-gebaseerd — geverifieerd door lezing, niet aangenomen), maar de fixtures documenteren nu een vorm die `_synthesize()` niet meer produceert. Geen breuk, wel gedateerde documentatie — een opruimtaak voor de eigenaar van die bestanden (buiten deze dispatch se grens), niet een blocker hier.
- **Open, buiten deze dispatch se bestandsgrens.** Het kimi/deepseek-harness/glm-harness-deel van de oorspronkelijke 26 loopt via `provider_dispatch.py::_emit_governance` (geen `validate_body()`-aanroep) en wordt alleen achteraf gecorrigeerd door `report_to_receipt_converter.py`'s asynchrone scan. Waarom die scan het merendeel (golf-2: 170/199 kimi-wrappers) wel corrigeert maar niet alles, is niet onderzocht binnen deze dispatch — geen van beide bestanden staat op de bestandsgrens van deze of een parallelle worker in dit dispatch-cluster. Aanbevolen vervolg-OI: breng `_emit_governance()`'s statusbepaling in lijn met `envelope_govern._govern()`'s synchrone `validate_body()`-check, zodat de correctie niet van een asynchrone achterstand afhangt.

## References

- OI-1637 — deze ADR, `scripts/lib/dispatch_govern.py::_synthesize`/`_govern_impl`, `scripts/lib/envelope_govern.py::_govern`, `scripts/lib/report_body_contract.py::CONTRACT_INVALID_STATUS`.
- `claudedocs/2026-09-05-golf2-contractpercentage-per-lane.md` — de golf-2-meting waarop deze ADR voortbouwt (niet in git; lokaal claudedocs-artefact).
- `tests/test_govern_wrapper_never_success.py` — de rode-naar-groene regressietest voor de nooit-succes-invariant, op beide seams.
- `scripts/lib/event_outcome_semantics.py` — de canonieke statusvocabulaire waar `contract_invalid` al deel van was.
- Open vervolgvraag: `scripts/lib/provider_dispatch.py::_emit_governance` en `scripts/lib/report_to_receipt_converter.py` — het kimi/deepseek-harness/glm-harness-governancepad, buiten deze dispatch se scope.
