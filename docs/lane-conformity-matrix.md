# L1: Lane-conformiteitsmatrix

> MEET-document, geen reparatie. Uitkomst van dispatch `20260808-ds-l1-lane-matrix`.
> Elke cel is getraceerd naar het daadwerkelijke codepad. Een label, configwaarde of
> docstring telt niet als bewijs. Cellen die niet met een codepad-trace konden worden
> vastgesteld staan op `ongemeten`.

**Gemeten op commit**: `6157a254` (main, 2026-08-08)
**Meetmethode**: codepad-trace van dispatch-deur tot mechanisme-aanroep
**Vier OI-gaten**: OI-1011, OI-1017, OI-1045, OI-1048 — alle vier terug te vinden als `bindt niet`

---

## 1. Lanes — enumeratie uit de code

De deur (`dispatch_cli.py:run_dispatch()`, regels 1671-1702) kent exact drie lanes,
bepaald door `dispatch_plan.py:compile_plan()` D1 (regels 226-245):

| Lane | String in code | Conditie | Adapter |
|---|---|---|---|
| `claude_tmux_subscription` | `"claude_tmux_subscription"` | `provider == CLAUDE` en `allow_headless == False` | `tmux_claude` |
| `claude_headless` | `"claude_headless"` | `provider == CLAUDE` en `allow_headless == True` | `claude_subprocess` |
| `provider` | `"provider"` | `provider != CLAUDE` (codex, gemini, kimi, deepseek-harness, glm-harness, local-gemma, litellm:*) | `provider` |

**Gesloten verzameling** (`dispatch_cli.py:1700`): elke andere lane-string raise
`_InvariantViolation`.

**Bevestiging/weerlegging van de vooraf-lijst**:
- `tmux`/`tmux_interactive` → bevestigd als `claude_tmux_subscription`
- `provider` → bevestigd
- `claude_headless` → bevestigd
- `subprocess` → **weerlegd als aparte lane**. `subprocess_dispatch.py` is een legacy
  pad dat alleen actief is bij `--adapter subprocess`; de deur routeert NIET naar
  `subprocess` als lane-string. Het is een alternatieve adapter binnen het legacy-pad,
  niet een vierde lane.

Bewijs: `dispatch_cli.py:1671-1702` (if/elif/else op `plan.lane`) en
`dispatch_plan.py:226-245` (D1 lane-resolutie).

---

## 2. Mechanismen — enumeratie uit aanroeppunten

De rijen zijn afgeleid uit een enumeratie van:
- Elke plek die een receipt schrijft (zoek op schrijvers van `t0_receipts.ndjson`)
- Elke plek die een worktree opzet of afbreekt
- Elke plek die een teardown of reap draait

### 2.1 Receipt-schrijvers

Twee canonieke paden (met lock, idempotency, validatie):

| Pad | Functie | Bestand | Validatie |
|---|---|---|---|
| A | `append_receipt_payload()` | `append_receipt_internals/payload.py:497` | `_validate_receipt()` incl. `_validate_model_present()` |
| B | `emit_dispatch_receipt()` | `governance_emit.py:321` | `_validate_provider()` + `_validate_receipt()` |

Twee bare-write paden (zonder lock, zonder validatie):

| Pad | Functie | Bestand | Gebruikt door |
|---|---|---|---|
| C | `write_starter_receipt()` | `vnx_starter.py:226-227` | Starter-mode only (geen dispatch-lane) |
| D | `_persist_receipt()` fallback | `subprocess_dispatch_internals/receipt_writer.py:251-252` | Recovery/sweep only (geen dispatch hot-path) |

Eén full-file rewrite (batch, geen append):

| Pad | Script | Bestand |
|---|---|---|
| E | `link_sessions_dispatches.py` | `scripts/link_sessions_dispatches.py:310` |

### 2.2 Worktree-operaties

| Operatie | Functie | Bestand | Gebruikt door |
|---|---|---|---|
| Aanmaken (OI-861 claims) | `create_dispatch_worktree()` | `dispatch_worktree_isolation.py:411` | provider, headless (envelope-pad) |
| Aanmaken (eigen lock) | `allocate()` | `tmux_worktree.py:109` | tmux |
| Verwijderen (unconditional) | `remove_dispatch_worktree()` | `dispatch_worktree_isolation.py:508` | provider, headless |
| Verwijderen (classify + reap) | `reap()` | `tmux_worktree.py:261` | tmux |

### 2.3 Teardown/reap-classificatie

| Operatie | Functie | Bestand | Gebruikt door |
|---|---|---|---|
| Classificatie (git ls-remote) | `classify()` | `tmux_worktree.py:195` | tmux |
| Teardown event emit | `_teardown()` | `tmux_interactive_dispatch.py:2291` | tmux |
| Geen classificatie | `remove_dispatch_worktree()` | `dispatch_worktree_isolation.py:508` | provider, headless |

### 2.4 Ringbuffer

| Operatie | Functie | Bestand | Gebruikt door |
|---|---|---|---|
| Archive at end-of-dispatch | `_archive_dispatch_events()` | `envelope_govern_support.py:296` | provider, headless (via `_govern()`) |
| Clear at end-of-dispatch | `_clear_dispatch_events()` | `envelope_govern_support.py` | provider, headless (via `_govern()` finally) |
| Pre-capture clear (previous dispatch) | `EventStore.clear()` | `tmux_interactive_dispatch.py:2424` | tmux |

### 2.5 PR enforcement

| Operatie | Functie | Bestand | Gebruikt door |
|---|---|---|---|
| Auto-PR check/create | `enforce_pr_exists()` | `pr_enforcement.py:63` | tmux (line 2907) |

### 2.6 Rijen toegevoegd aan de oorspronkelijke acht

De oorspronkelijke acht mechanismen uit de dispatch-opdracht waren een startpunt.
Uit de enumeratie hierboven zijn twee rijen toegevoegd:

| # | Toegevoegde rij | Reden |
|---|---|---|
| 9 | **Canonical receipt path (geen bare-write bypass)** | De enumeratie vond twee bare-write paden (C en D) die receipts schrijven zonder lock, idempotency, of validatie. Een lane die via zo'n pad schrijft heeft een governance-gat dat geen van de oorspronkelijke acht rijen dekt. |
| 10 | **OI-861 worktree identity guard (O_EXCL claims)** | De enumeratie vond twee verschillende worktree-aanmaakmechanismen: `create_dispatch_worktree()` met O_EXCL claims en `tmux_worktree.allocate()` met eigen locking. Het OI-861 mechanisme (worktree-identiteit gebonden aan dispatch_id via atomic claims) is een apart afdwingpunt dat geen van de oorspronkelijke rijen dekt. |

---

## 3. De matrix

| # | Mechanisme | tmux | headless | provider |
|---|---|---|---|---|
| 1 | `validate_body()` vóór receipt | **bindt** | **bindt niet** | **bindt niet** |
| 2 | Fail-closed model/provider-check | **bindt** | **bindt** | **bindt** |
| 3 | `isolation=worktree` gehonoreerd | **bindt** | **bindt niet** | **bindt** |
| 4 | Hoofdcheckout-guard (geen stille fallback) | **bindt** | **bindt niet** | **bindt** |
| 5 | Reap weigert bij ongepushte commits | **bindt** (branch blijft) | **n.v.t.** | **bindt niet** |
| 6 | Teardown meldt `worktree_state` | **bindt** | **n.v.t.** | **bindt niet** |
| 7 | Push+PR-verplichting afgedwongen | **bindt** (pushed + committed) | **bindt** | **bindt** |
| 8 | Ringbuffer-teardown (end-of-dispatch) | **bindt niet** | **bindt** | **bindt** |
| 9 | Canonical receipt path (geen bare-write) | **bindt** | **bindt** | **bindt** |
| 10 | OI-861 worktree identity guard | **bindt niet** | **n.v.t.** | **bindt** |

### 3.1 Celbewijzen — mechanisme 1: `validate_body()` vóór receipt

**tmux — bindt**
- `dispatch_govern._govern_impl()` roept `validate_body()` aan op regel 533 (workers rapport) en regel 562 (final body) vóór `emit_unified_report()` op regel 670 en `ensure_receipt()` op regel 699.
- Bestand: `scripts/lib/dispatch_govern.py:533`, `:562`
- Het workers-rapport wordt eerst gelezen uit `unified_reports/<dispatch_id>.md`. Als `validate_body()` faalt, valt de governance terug op synthese (`_synthesize()`, regel 548). Als het final body ook faalt, wordt de contract-schending gerapporteerd.

**headless — bindt niet**
- `run_envelope_headless_plan()` → `_govern()` roept `emit_unified_report()` aan op regel 107 zonder voorafgaande `validate_body()`.
- `emit_unified_report()` (`governance_emit.py:394-526`) valideert NIET het rapport-body-contract. Het controleert alleen frontmatter-pattern (`_validate_report_frontmatter()`, regel 505), niet de verplichte koppen.
- Bestand: `scripts/lib/envelope_govern.py:107`, `scripts/lib/governance_emit.py:394-526`
- **OI-1048**: het rapport had `validate_body() → valid=False` maar `emit_unified_report()` schreef het toch weg, en `emit_dispatch_receipt()` produceerde een receipt met `status=success`.

**provider — bindt niet**
- `run_envelope_plan()` → `_govern()` zelfde pad als headless: `emit_unified_report()` op regel 107 zonder `validate_body()`.
- Ook `provider_dispatch._emit_governance()` (`provider_dispatch.py:859,986`) roept `emit_dispatch_receipt()` en `emit_unified_report()` aan zonder `validate_body()`.
- Bestand: `scripts/lib/envelope_govern.py:107`, `scripts/lib/provider_dispatch.py:859,986`
- **OI-1017**: het rapportbestand was de geassembleerde prompt. Nul verplichte koppen. Toch `status=success` receipt.

**Noot over `report_to_receipt_converter.py`**:
- De converter (`report_to_receipt_converter.py:338`) roept WEL `validate_body()` aan via zijn fail-closed checks (regels 407-429). Maar de converter draait als cron-job (`receipt_processor.sh` elke 30s), niet in de dispatch hot-path. Een ongeldig rapport dat door de converter wordt opgepikt krijgt `status="failure"` of `status="contract_invalid"`, nooit `status="success"`. Maar dit gebeurt buiten de dispatch-executie — de deur is dan al lang `exit 0`.

### 3.2 Celbewijzen — mechanisme 2: Fail-closed model/provider-check

**Alle drie lanes — bindt**
- Alle drie lanes schrijven receipts via canonieke paden (A of B) die `_validate_receipt()` → `_validate_model_present()` afdwingen.
- tmux: worker receipt via `append_receipt.py` → `append_receipt_payload()` (Pad A, `payload.py:480`). Governance fallback via `ensure_receipt()` → `append_receipt_payload()` (Pad A).
- headless: `emit_dispatch_receipt()` (Pad B, `governance_emit.py:318`).
- provider: `emit_dispatch_receipt()` (Pad B), zowel via envelope (`envelope_govern.py:252`) als direct (`provider_dispatch.py:859`).
- `_validate_model_present()` (`validation.py:340-365`) weigert receipts met `model` = None, empty, "unknown", "null", "none", "n/a", "na", "unset", "-".
- **Geen bare-write bypass voor dispatch-lanes**: Paden C (`vnx_starter.py`) en D (`receipt_writer.py` fallback) worden niet door de drie dispatch-lanes gebruikt.

### 3.3 Celbewijzen — mechanisme 3: `isolation=worktree` gehonoreerd

**tmux — bindt**
- `_execute_claude()` (`dispatch_cli.py:1377`) hardcodeert `isolated_worktree=True`.
- `TmuxInteractiveDispatch.dispatch()` roept `allocate()` aan (`tmux_interactive_dispatch.py:2195`) en zet `cwd = worktree_handle.path` (`:2200`).
- De tmux-sessie start met deze cwd. De worker draait IN de worktree.
- Bewijs: `scripts/lib/tmux_interactive_dispatch.py:2193-2200`

**headless — bindt niet**
- `run_envelope_headless_plan()` roept `ClaudeSubprocessAdapter().run(enriched_spec)` aan (`dispatch_envelope.py:499`) **zonder** `cwd` parameter.
- `ClaudeSubprocessAdapter.run()` (`envelope_adapters_claude.py:116-134`) accepteert een optionele `cwd` parameter (regel 120) maar de headless lane geeft hem niet mee.
- `spawn_claude()` krijgt `cwd=None` → erft CWD van het parent-proces → **hoofdcheckout**.
- Er wordt GEEN worktree aangemaakt in het headless pad.
- **OI-1045**: spec zette `isolation=worktree`, de deur negeerde het, de worker werkte in de hoofdcheckout. 420 regels ongecommit.

**provider — bindt**
- `run_envelope_plan()` roept `create_dispatch_worktree()` aan (`dispatch_envelope.py:328`) en geeft `cwd=wt_path` mee aan `ProviderAdapter().run()` (`:357`).
- `create_dispatch_worktree()` (`dispatch_worktree_isolation.py:411-505`) maakt de worktree aan via `git worktree add`.
- Als worktree-aanmaak faalt, wordt de dispatch afgebroken (regels 329-352) — geen stille fallback naar de hoofdcheckout.
- Bewijs: `scripts/lib/dispatch_envelope.py:328,357` en `scripts/lib/dispatch_worktree_isolation.py:411-505`

### 3.4 Celbewijzen — mechanisme 4: Hoofdcheckout-guard

**tmux — bindt (by construction)**
- De tmux-lane zet `cwd = worktree_handle.path` vóór de tmux-sessie start (`tmux_interactive_dispatch.py:2200`).
- Er is geen aparte "guard" die detecteert of de worker in de hoofdcheckout werkt — de worker KAN niet in de hoofdcheckout werken omdat zijn cwd de worktree is.
- Bewijs: `scripts/lib/tmux_interactive_dispatch.py:2200`

**headless — bindt niet**
- Geen worktree-aanmaak, geen cwd-override, geen guard. De worker draait in de geërfde CWD.
- Bewijs: `scripts/lib/dispatch_envelope.py:499` (geen `cwd` parameter)

**provider — bindt**
- `create_dispatch_worktree()` faalt hard als worktree-aanmaak mislukt. Er is geen fallback naar de hoofdcheckout.
- De OI-861 identity check (regels 454-466) voorkomt dat twee dispatches dezelfde worktree delen.
- Bewijs: `scripts/lib/dispatch_worktree_isolation.py:411-505` en `scripts/lib/dispatch_envelope.py:329-352` (fail-path)

### 3.5 Celbewijzen — mechanisme 5: Reap weigert bij ongepushte commits

**tmux — bindt (branch blijft lokaal behouden)**
- `classify()` (`tmux_worktree.py:195-239`) bepaalt de worktree-status via `git status --porcelain`, `HEAD == base_sha`, en `git ls-remote origin dispatch/<id>`.
- `reap()` (`tmux_worktree.py:261-358`) handelt per classificatie:
  - `clean`: worktree verwijderd, branch verwijderd
  - `pushed`: worktree verwijderd, branch lokaal verwijderd (remote blijft)
  - `committed`: worktree verwijderd, **branch lokaal BEHOUDEN** — de commits overleven
  - `dirty`: worktree **gelockt** (niet verwijderd)
  - `unknown`: behandeld als dirty
- **Beperking**: de worktree-directory wordt altijd verwijderd bij "committed". De branch met commits blijft lokaal bestaan, maar er is geen actief werkbestand meer. De dispatch kan nog steeds `exit 0` terwijl er ongepushte commits zijn.
- Bewijs: `scripts/lib/tmux_worktree.py:195-358`

**headless — n.v.t.**
- De headless lane maakt geen worktree aan. Er valt niets te reapen.

**provider — bindt niet**
- `remove_dispatch_worktree()` (`dispatch_worktree_isolation.py:508-626`) verwijdert de worktree **onvoorwaardelijk** met `git worktree remove --force`.
- Geen `classify()` stap. Geen push-check. Geen ls-remote.
- De lokale branch wordt verwijderd met `git branch -D` (force, regel ~615) — geen check of de branch op origin staat.
- Bewijs: `scripts/lib/dispatch_worktree_isolation.py:508-626`
- **OI-1011 analogie**: als dit in de provider lane was gebeurd, waren de commits volledig verloren (branch verwijderd met `-D`).

### 3.6 Celbewijzen — mechanisme 6: Teardown meldt `worktree_state`

**tmux — bindt**
- `_teardown()` (`tmux_interactive_dispatch.py:2291-2365`) roept `classify()` aan (`:2324`) en `reap()` (`:2326`).
- Emit `interactive_teardown_worktree` event met metadata: `worktree_state`, `branch_kept_local`, `branch_kept_remote`, `preserved_path` (`:2328-2340`).
- Bij `dirty` status: extra `interactive_teardown_preserved` event (`:2341-2347`).
- Bewijs: `scripts/lib/tmux_interactive_dispatch.py:2321-2347`

**headless — n.v.t.**
- Geen worktree → geen teardown-classificatie.

**provider — bindt niet**
- `remove_dispatch_worktree()` emitteert geen classificatie-events. Het logt alleen "removed worktree".
- Geen `worktree_state` veld in enig event of log.
- Bewijs: `scripts/lib/dispatch_worktree_isolation.py:508-626`

### 3.7 Celbewijzen — mechanisme 7: Push+PR-verplichting

**tmux — bindt (voor `pushed` EN `committed`)**
- `_enforce_pr_exists()` (`tmux_interactive_dispatch.py:1031-1098`) wordt aangeroepen op regel 2982, vóór `_govern_report()` en vóór `_teardown()`.
- `enforce_pr_exists()` (`pr_enforcement.py:80-164`) bevat de ENIGE per-staat beslissing: `pushed` → PR afdwingen; `committed` → pushen dan PR afdwingen (rij-7 fix); `clean`/`dirty` → `applicable=False`.
- Een mislukte push of PR-creatie → `ok=False` → `worker_succeeded = False` (`tmux_interactive_dispatch.py:2989`) → `receipt["status"] = "failed"` + `failure_reason=dispatch_branch_no_pr (state=...)`. Geen `exit 0` met werk lokaal gestrand.
- De corrective receipt wordt door `pr_enforcement._record_corrective_receipt` zelf geschreven met `autopr_kind` (`push_failed`/`pr_failed`).
- Bewijs: `scripts/lib/pr_enforcement.py:80-164`, `scripts/lib/tmux_interactive_dispatch.py:2982-3013`

**headless — bindt**
- `run_envelope_headless_plan()` (`dispatch_envelope.py:665-672`) roept `_enforce_push_pr()` aan vóór `remove_dispatch_worktree()` (de worktree is de enige handle naar de lokale branch).
- `_enforce_push_pr()` (`dispatch_envelope.py:101-173`) hergebruikt `pr_enforcement.enforce_pr_exists` en de gedeelde `tmux_worktree.classify_path()` — geen tweede kopie van de beslissing (OI-1099).
- De headless-lane maakt sinds #1416 wél een worktree aan (`isolation=worktree` gehonoreerd, hard-fail bij creatie, `dispatch_envelope.py:641-660`); de matrix-tekst "maakt geen worktree aan" is daarmee achterhaald.
- Een mislukte push/PR → `result.status = "failure"` → `EnvelopeResult.returncode = 1`.
- Bewijs: `scripts/lib/dispatch_envelope.py:101-175,665-672`

**provider — bindt**
- `run_envelope_plan()` (`dispatch_envelope.py:481-488`) roept dezelfde `_enforce_push_pr()` aan, vóór `remove_dispatch_worktree()`.
- Bewijs: `scripts/lib/dispatch_envelope.py:101-175,481-488`

**Gedeelde classificatie**
- `classify_path()` (`tmux_worktree.py:211-271`) is de enige canonieke git-staat-classificatie; `classify()` (`tmux_worktree.py:195-208`) delegeert ernaar. De envelope-lanes gebruiken `classify_path()` direct (geen `WorktreeHandle`).
- Bewijs: `scripts/lib/tmux_worktree.py:195-271`

### 3.8 Celbewijzen — mechanisme 8: Ringbuffer-teardown

**tmux — bindt niet**
- De tmux lane archiveert events bij de START van de VOLGENDE dispatch, niet bij het EIND van de huidige.
- `EventStore.clear(label, archive_dispatch_id=_prev_did)` op regel 2424 — dit archiveert de VORIGE dispatch's events.
- Bij `_teardown()` wordt alleen `_run_capture_normalizer()` aangeroepen (`:2316`) — geen `_archive_dispatch_events()` of `_clear_dispatch_events()`.
- **Gevolg**: de laatste dispatch in een serie lekt events in de live file. Geen end-of-dispatch rotatie.
- Bewijs: `scripts/lib/tmux_interactive_dispatch.py:2417-2426` (pre-capture clear) en `:2291-2365` (teardown zonder archive)

**headless — bindt**
- `_govern()` roept `_archive_dispatch_events()` aan op regel 99 en `_clear_dispatch_events()` in de finally block.
- Bewijs: `scripts/lib/envelope_govern.py:99` en `envelope_govern_support.py:296-350`

**provider — bindt**
- Zelfde `_govern()` pad als headless.
- Ook `provider_dispatch._emit_governance()` (`provider_dispatch.py`) archiveert events — dit is het directe provider-pad, niet via envelope.
- Bewijs: `scripts/lib/envelope_govern.py:99` en `scripts/lib/provider_dispatch.py` (OI-878/OI-902 fix)

### 3.9 Celbewijzen — mechanisme 9: Canonical receipt path

**Alle drie lanes — bindt**
- tmux: worker receipt via `append_receipt.py` → `append_receipt_payload()` (Pad A). Governance fallback via `ensure_receipt()` → `append_receipt_payload()` (Pad A).
- headless: `_govern()` → `emit_dispatch_receipt()` (Pad B).
- provider: `_govern()` → `emit_dispatch_receipt()` (Pad B) of `provider_dispatch._emit_governance()` → `emit_dispatch_receipt()` (Pad B).
- Beide canonieke paden (A en B) hebben: `LOCK_EX` file lock, idempotency cache, `_validate_receipt()` inclusief `_validate_model_present()`.
- Geen van de drie dispatch-lanes gebruikt bare-write paden (C of D) in de hot-path.
- Bewijs: `scripts/lib/append_receipt_internals/payload.py:434-504` (Pad A validatie) en `scripts/lib/governance_emit.py:71-328` (Pad B validatie)

### 3.10 Celbewijzen — mechanisme 10: OI-861 worktree identity guard

**tmux — bindt niet**
- `tmux_worktree.allocate()` (`tmux_worktree.py:109-192`) heeft eigen idempotency (branch exists + SHA match → attach), maar gebruikt NIET het OI-861 O_EXCL claim-mechanisme.
- Geen `_write_claim_atomic()`, geen `_read_claim()`, geen `verify_worktree_identity()`.
- De OI-861 guard (`dispatch_worktree_isolation.py:179-214`) is alleen geïntegreerd in `create_dispatch_worktree()`.
- Bewijs: `scripts/lib/tmux_worktree.py:109-192` (geen OI-861 claims)

**headless — n.v.t.**
- Geen worktree-aanmaak → geen identity guard nodig.

**provider — bindt**
- `create_dispatch_worktree()` (`dispatch_worktree_isolation.py:454-502`):
  - Lees bestaande claim (`_read_claim`, regel 459)
  - Bij bestaande claim van andere dispatch: `_claim_belongs_to_or_raise()` (regel 461)
  - Bij race na `git worktree add`: herlees claim (`:485-487`)
  - Schrijf claim atomair via O_EXCL (`_write_claim_atomic`, regel 497)
- `remove_dispatch_worktree()` wist de claim (`_clear_claim`, regel 530).
- Bewijs: `scripts/lib/dispatch_worktree_isolation.py:411-505` (create) en `:508-626` (remove)

---

## 4. Store-vraag per mechanisme

Elk mechanisme schrijft naar de **per-project store** (`~/.vnx-data/<project_id>/` of `<repo>/.vnx-data/`). Geen enkel mechanisme schrijft naar een gedeelde locatie over projecten heen.

| # | Mechanisme | Store-locatie | ADR-026 compliant? | Bijzonderheden |
|---|---|---|---|---|
| 1 | `validate_body` | (geen write — validatiepoort) | n.v.t. | — |
| 2 | Model/provider-check | (geen write — validatiepoort) | n.v.t. | — |
| 3 | Worktree-aanmaak | `<project_root>/.vnx-data/worktrees/` (worktree) + `~/.vnx-data/<pid>/state/dispatch_worktree_claims/` (claims) | ja | Claims via `resolve_data_root()` |
| 4 | Hoofdcheckout-guard | Claims in `~/.vnx-data/<pid>/state/dispatch_worktree_claims/` | ja | — |
| 5 | Reap | (cleart alleen claims, geen writes) | n.v.t. | — |
| 6 | Teardown-rapportage | EventStore: `~/.vnx-data/<pid>/events/` | ja | — |
| 7 | PR-enforcement | `~/.vnx-data/<pid>/state/t0_receipts.ndjson` | ja | Corrective receipts |
| 8 | Ringbuffer | `~/.vnx-data/<pid>/events/` + `archive/<terminal>/` | deels | `_events_dir()` heeft eigen resolver die `VNX_DATA_HOME` en XDG overslaat; valt in praktijk samen met canonical path |
| 9 | Receipt-schrijver | `~/.vnx-data/<pid>/state/t0_receipts.ndjson` (paden A/B) | ja | `dispatch_cli._resolve_data_dir()` is simpelder maar ook per-project |
| 10 | OI-861 claims | `~/.vnx-data/<pid>/state/dispatch_worktree_claims/` | ja | Atomic O_EXCL |

**Enige ADR-026-partial**: mechanisme 8 (ringbuffer). `event_store.py:_events_dir()` (regels 39-75) heeft een eigen 3-staps resolver: explicit `VNX_DATA_DIR` → direct `~/.vnx-data/<pid>/events` → fallback naar `resolve_paths()["VNX_DATA_DIR"]`. Deze slaat `VNX_DATA_HOME` en XDG-resolutie over. In de praktijk convergeren beide paden op dezelfde per-project store, dus het is geen productieprobleem.

**Geen ADR-007-schendingen**: alle mechanismen die schrijven naar een centrale store resolven het project_id correct.

---

## 5. Toegevoegde rijen — verantwoording

### Rij 9: Canonical receipt path (geen bare-write bypass)

Toegevoegd omdat de receipt-schrijver-enumeratie twee bare-write paden vond (C: `vnx_starter.py`, D: `receipt_writer.py` fallback) die receipts schrijven zonder lock, idempotency, of validatie. De oorspronkelijke acht rijen bevatten wel `validate_body` (rij 1) en model/provider-check (rij 2), maar geen van beide dekt de vraag of de lane überhaupt het canonieke pad gebruikt. Een lane die via een bare-write pad schrijft, omzeilt zowel rij 1 als rij 2.

Uitkomst: alle drie dispatch-lanes gebruiken canonieke paden. De bare-write paden zijn beperkt tot starter-mode (C) en recovery/sweep (D). Dit is een geruststelling maar moet bewaakt blijven.

### Rij 10: OI-861 worktree identity guard (O_EXCL claims)

Toegevoegd omdat de worktree-enumeratie twee verschillende aanmaakmechanismen vond: `create_dispatch_worktree()` met O_EXCL atomic claims en `tmux_worktree.allocate()` met eigen fcntl-locking. Het OI-861 mechanisme voorkomt dat twee concurrente dispatches dezelfde worktree delen — een klasse van fouten die geen van de oorspronkelijke rijen dekt. Rij 4 (hoofdcheckout-guard) gaat over werk in de verkeerde checkout; rij 10 gaat over twee dispatches die in DEZELFDE worktree werken.

Uitkomst: de provider lane heeft OI-861 bescherming. De tmux lane heeft het niet — maar de kans op collisie is daar kleiner omdat `allocate()` de dispatch-id in het branch-pad verwerkt en fcntl-lockt.

---

## 6. Cellen op `ongemeten`

Geen. Alle 30 cellen (10 mechanismen × 3 lanes) zijn getraceerd naar het daadwerkelijke codepad. Elke cel heeft een bestandsregel als bewijs.

---

## 7. Dekking van de vier bekende OI-gaten

| OI | Lane | Mechanisme | Matrix-cel | Status |
|---|---|---|---|---|
| OI-1011 | tmux | Commit lokaal, niets gepusht, geen PR, worktree gereapt, exit 0 | Rij 7 (tmux) | `bindt` maar met beperking: PR enforcement dekt alleen `worktree_state == "pushed"`. "committed" valt erbuiten. |
| OI-1017 | provider | Rapport = prompt, nul koppen, status=success | Rij 1 (provider) | `bindt niet` — bevestigd |
| OI-1045 | headless | isolation=worktree genegeerd, worker in hoofdcheckout | Rij 3 (headless) | `bindt niet` — bevestigd |
| OI-1048 | headless | validate_body=False, receipt status=success | Rij 1 (headless) | `bindt niet` — bevestigd |

**OI-1011 nuancering**: de cel staat op `bindt` omdat het mechanisme bestaat en werkt voor het "pushed but no PR" scenario. Maar OI-1011 toont aan dat het "committed but not pushed" scenario niet gedekt wordt: de dispatch slaagt terwijl er niets op origin staat. Dit is een meetcorrectie ten opzichte van de oorspronkelijke OI-beschrijving: het mechanisme bestaat wel, maar dekt niet het volledige probleem. De vervolg-PR voor rij 7 moet de scope verbreden van "pushed" naar "committed".

---

## 8. Aanbevolen volgorde voor de vier vervolg-PR's

Op basis van de matrix — cellen die op `bindt niet` staan, gerangschikt naar impact:

1. **Rij 1 (validate_body) voor provider + headless** (OI-1017, OI-1048): twee lanes missen rapportcontract-validatie. Impact: stille successen op ongeldige rapporten. Dit is de grootste gap.
2. **Rij 3 (isolation=worktree) voor headless** (OI-1045): headless lane negeert worktree-isolatie. Impact: werk in hoofdcheckout.
3. **Rij 7 (push+PR) voor headless + provider, en scope-verbreding voor tmux** (OI-1011): twee lanes missen PR-enforcement volledig; de tmux-lane dekt alleen "pushed", niet "committed".
4. **Rij 5/6 (reap+teardown) voor provider**: provider lane mist push-check bij reap en teardown-classificatie. Impact: werk kan verloren gaan bij worktree-verwijdering.

Rij 8 (ringbuffer voor tmux) en rij 10 (OI-861 voor tmux) zijn lagere prioriteit: de eerste lekt events van de laatste dispatch in een serie, de tweede heeft een lagere kans op collisie door het branch-naam-mechanisme.
