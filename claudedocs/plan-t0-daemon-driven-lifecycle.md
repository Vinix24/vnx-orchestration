# Bouwplan: T0-gemedieerde context-rotatie — herziening r3

> Track: `t0-daemon-driven-lifecycle`
> Dispatch: dispatch-20260802-rotation-lifecycle-plan
> Datum: 2026-08-02
> Status: plan — herzien na plan-gate r2 (0 pass / 3 revise / 1 block)
> Herziening: r3 — operatorbesluit: rotatie is T0-gemedieerd, geen daemon-hard-kill

---

## 0. Operatorbesluit: T0-gemedieerd, niet daemon-hard-kill

De plan-gate gaf in ronde 2: `0 pass / 3 revise / 1 block`. De opus-zetel blokkeerde op een
ontwerpsplitsing die kimi zo formuleerde:

> make rotation T0-mediated (rotation_requested marker + checkpoint at next boundary) **or**
> add a real quiescence signal

**De operator kiest T0-gemedieerd.** De daemon **kilt niets, spawnt niets, wacht op niets**.
Hij zet een verzoek-marker (`rotation_requested`). T0 ziet die op zijn eerstvolgende
governance-grens en roteert zichzelf via het bestaande `checkpoint()`-pad. Dit is precies de
weg die `context_rotation.py` al ontwerpt voor T0-geïnitieerde rotatie (docs/operations/
CONTEXT_ROTATION.md:216-217: de oude T0 "is expected to exit shortly after" — de exit is
onderdeel van T0's eigen boundary-logica, niet van dit plan).

De vijf zwaarste r2-bevindingen lossen hiermee in één keer op. Per bevinding, waar het in dit
plan staat:

| r2-bevinding | Oplossing in deze vorm | Sectie |
|---|---|---|
| Destructieve teardown (`tmux kill-session` op de oude T0) | **Vervalt.** Er is geen daemon-kill. De daemon deed dat ook feitelijk fout: hij zocht sessies met `^vnx-t0-`, terwijl `start.sh:30` sessies `vnx-<basename>` noemt en de levende sessies `orch-t0`, `mc-t0`, `seo-t0`, `t0-sales-copilot` heten. Geen match. Wat wél matchte was de successor die `respawn()` zelf aanmaakt (`vnx-t0-rotation-<term>-<id>`, `context_rotation.py:675`) — de daemon kon dus zijn eigen verse T0 killen. | §2.6, Appendix A |
| Twee levende T0's | **Vervalt.** Er wordt maar één T0 gestart, door de bestaande weg: de oude T0 roept `checkpoint()` aan, die `respawn()` uitvoert en op `.ready` wacht, en de oude T0 exit daarna zelf. Geen daemon die een tweede start terwijl de eerste nog draait. | §2.6 |
| `mid_action`-poort dood in de code | **Leeft.** T0 beslist zelf of hij op een grens staat en roept `checkpoint()` aan met de juiste `mid_action`-waarde. De poort in `decide_rotation()` (regel 314-315) blijft onaangetast en is de enige die over rotatie beslist. | §2.4 |
| `respawn()`-wachten blokkeert de dispatcher-loop (tot 60s) | **Vervalt als probleem.** `respawn()` wordt alleen nog aangeroepen vanuit `checkpoint()`, die draait in T0's eigen proces. De dispatcher-loop roept `respawn()` nooit aan; de tick roept alleen de marker-schrijver. | §2.6 |
| `.ready` als hartslag — eenmalig en dus gegarandeerd stale | **Vervalt als probleem.** Zonder daemon-gestuurde liveness-detectie is er geen versheidssignaal nodig. `.ready` blijft alleen de eenmalige handshake binnen `respawn()` (rotation_id-gestempeld). Liveness is de verantwoordelijkheid van de operator (`tmux ls`), de dispatcher-supervisor, en de freshness-timestamp (§4.2). | §2.6, §4.2 |

---

## 1. Huidige toestand, gemeten

### 1.1 Wat bestaat en draait

| Bouwsteen | Bestand | Regels | Status |
|---|---|---|---|
| RotationPolicy + beslisfunctie | `scripts/lib/context_rotation.py:220-335` | 116 | Dormant (enabled=false) |
| Handoff-schrijver | `scripts/lib/context_rotation.py:403-504` | 102 | Dormant |
| Respawn-mechanisme | `scripts/lib/context_rotation.py:642-723` | 82 | Dormant |
| Checkpoint-integratiepunt | `scripts/lib/context_rotation.py:778-898` | 121 | Dormant |
| Stop-hook veiligheidsnet | `scripts/hooks/session_stop_rotation.py` | 73 | Dormant (VNX_T0_ROTATION=1) |
| Handoff-lezer | `scripts/lib/handoff_reader.py` | 104 | Actief |
| `vnx handoff` CLI | `vnx_cli/commands/handoff.py` | ~120 | Actief |
| Rotate skill (handmatig) | `~/.claude/skills/rotate/SKILL.md` | 106 | Actief (handmatig) |

**Verificatie:**

```
$ grep -n "enabled: false\|enabled=False" scripts/lib/context_rotation.py
35:DEFAULT OFF: RotationPolicy.enabled is False unless configs/context_rotation.yaml
238:        returns the dataclass defaults (enabled=False) — the DEFAULT-OFF

$ grep -n "enabled\|respawn" configs/context_rotation.yaml
enabled: false
respawn: off

$ python3 -m pytest tests/test_context_rotation.py -q --tb=no
77 passed, 1 failed
```

Alles in `context_rotation.py` is passief. Geen enkel code-pad vuurt zonder dat iemand
`enabled: true` flipt én `checkpoint()` aanroept. De test-suite (77/78 tests groen; de ene
falende test `test_project_id_scoped_across_two_projects` is een pre-existing path-resolutie-
issue — twee project_ids resolven naar hetzelfde pad in project-local mode, zie §8.3) dekt de
volledige module.

### 1.2 Wat bestaat maar niet voor dit doel gebouwd is

**De UNIFIED_SUPERVISOR hooks** (`scripts/lib/dispatcher_supervisor_ticks.sh`, 224 regels) zijn
een throttled-tick patroon binnen de dispatcher-loop. Ze gaten op `VNX_SUPERVISOR_MODE=unified`:

```
$ grep -n "VNX_SUPERVISOR_MODE\|_maybe_runtime_supervise\|_unified_supervisor_lease_sweep_tick" scripts/lib/dispatcher_supervisor_ticks.sh
26:_maybe_runtime_supervise() {
27:    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
205:_unified_supervisor_lease_sweep_tick() {
206:    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
```

Deze hooks worden aangeroepen in `process_dispatches()` (`scripts/dispatcher_minimal.sh:599-607`):

```
$ grep -n "process_dispatches\|_maybe_runtime_supervise\|_unified_supervisor_lease_sweep_tick" scripts/dispatcher_minimal.sh
599:process_dispatches() {
601:    _maybe_runtime_supervise
603:    _unified_supervisor_lease_sweep_tick
```

**Het throttled-tick patroon is herbruikbaar voor een rotatie-tick.** De bestaande ticks
(runtime_supervise elke 60s, lease_sweep elke 30s) zijn exact het mechanisme waar de
rotatie-daemon aan moet haken. Er ontbreekt een `_maybe_rotation_tick`.

**De supervisor wrappers** (`scripts/dispatcher_supervisor.sh`, 194 regels;
`scripts/receipt_processor_supervisor.sh`, 207 regels) zijn het template voor een rotatie-
daemon supervisor: singleton-enforcement, exponential backoff (2s -> 60s), child-monitoring,
en PID/lock-file management.

**De standaard supervisormodus is `legacy`.** De dispatcher gebruikt
`${VNX_SUPERVISOR_MODE:-legacy}` — wie geen `VNX_SUPERVISOR_MODE=unified` exporteert, krijgt
géén enkele tick. Dit is relevant voor §3.3: de rotatie-tick mag hier NIET aan hangen.

### 1.3 Wat ontbreekt (gap-analyse)

| Gap | Waarom het ontbreekt |
|---|---|
| **Geen verzoek-pad van buiten de T0** | `checkpoint()` is een passieve functie — iemand moet 'm aanroepen. Er is geen externe entiteit die een rotatie *verzoekt* op basis van sessie-duur, zonder zelf te roteren. |
| **Geen tick in de dispatcher-loop voor rotatie** | `dispatcher_supervisor_ticks.sh` heeft ticks voor lease_sweep, runtime_supervise, OI-bridge, objective-reconcile, en learning-cycle. Geen rotatie-tick. |
| **Geen crash-loop-halt** | Als een verse T0 niet `.ready` schrijft, abort `respawn()` na 60s en keert terug met `success=False`. Maar er is geen mechanisme dat telt hoe vaak dit achter elkaar gebeurt en stopt met *verzoeken* na N pogingen. |
| **Geen lease op de marker** | `checkpoint()` heeft een request-marker TTL (120s, regel 819-826) die duplicate calls binnen hetzelfde proces voorkomt. Maar er is geen distributed lease die concurrente marker-writes van twee losse dispatcher-processen blokkeert. |
| **State-paden zijn niet gegarandeerd project_id-gescopet** | `rotation_state_dir`/`rotation_handoff_dir` resolven via `_resolve_state_root`, die in project-local mode (branch 4) twee project_ids op hetzelfde pad laat landen (de enige rode test in de suite). Crash-teller, lease en durable state landen daar. |
| **Rotatie is default-uit** | `configs/context_rotation.yaml:enabled: false`. |

### 1.4 Grens met PR #1149 (`t0-context-rotation-revival`)

PR #1149 leverde het **mechanisme**: `context_rotation.py` met `checkpoint()` ->
`write_t0_handoff()` -> `respawn()`. Het is een passief integratiepunt — een draaiende T0 moet
het aanroepen.

Deze track (`t0-daemon-driven-lifecycle`) bouwt de **verzoeker** die:
1. Periodiek meet of een T0 aan de sessie-duur-drempel zit
2. Een `rotation_requested`-marker plaatst die T0 bij zijn volgende grens consumeert
3. Zichzelf beschermt tegen verzoek-loops (crash-loop-halt) en concurrente writes (flock)
4. Een receipt achterlaat bij elke echte state-transitie
5. De levenscyclus bewaken NIET zelf doet — dat blijft T0's eigen boundary-logica

Het mechanisme uit #1149 wordt **gebruikt, niet herbouwd**. De daemon wordt géén eigenaar van
de levenscyclus — hij is de verzoeker. Dat bewaart de scope-grens uit `CONTEXT_ROTATION.md`:
"No fully hands-off auto-respawn from a daemon with no T0 present" blijft staan, want de daemon
spawnt niet.

---

## 2. Doelarchitectuur (T0-gemedieerd)

### 2.1 Wie bezit de levenscyclus

```
┌──────────────────────────────────────────────────┐
│              dispatcher_supervisor.sh             │
│  (bestaat al — singleton, backoff, child-watch)  │
├──────────────────────────────────────────────────┤
│              dispatcher_minimal.sh                │
│  (bestaat al — process_dispatches loop)          │
│  ┌────────────────────────────────────────────┐  │
│  │  prelude ticks (bestaan al):                │  │
│  │  _maybe_runtime_supervise      (60s)       │  │
│  │  _unified_supervisor_lease_sweep_tick (30s)│  │
│  │  _maybe_oi_bridge_tick         (900s)      │  │
│  │  _maybe_objective_reconcile    (900s)      │  │
│  │  _maybe_learning_cycle         (86400s)    │  │
│  │  ┌──────────────────────────────────────┐  │  │
│  │  │  _maybe_rotation_tick   ← NIEUW      │  │  │
│  │  │  (interval: 120s default)            │  │  │
│  │  │  Gated op VNX_T0_ROTATION_DAEMON=1   │  │  │
│  │  │  ONAFHANKELIJK van supervisor-mode   │  │  │
│  │  └──────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
        │
        │  schrijft alleen de marker:
        │  state/rotation/<project_id>/T0_request.json
        │  { "rotation_id", "status": "rotation_requested", "requested_at" }
        ▼
┌──────────────────────────────────────────────────┐
│  T0 (draaiend, interactief)                      │
│  roept op zijn governance-grens checkpoint()     │
│  aan (bestaat al) — leest de marker, roteert     │
│  zichzelf: handoff → respawn() → wacht .ready →  │
│  oude T0 exit zelf.                              │
└──────────────────────────────────────────────────┘
```

**De dispatcher bezit de periodieke meting.** De rotatie-tick wordt een zusje van de bestaande
ticks — zelfde patroon, zelfde throttling, maar met een **eigen, onafhankelijke schakelaar**
(§3.3). De tick doet maar één ding: meten of `session_duration >= max_session_seconds` en zo
ja een `rotation_requested`-marker schrijven. Hij kilt, spawnt en wacht niet.

**T0 bezit de rotatie-uitvoering.** Alleen T0, via `checkpoint()` op een echte
governance-grens, roteert. De `mid_action`-poort in `decide_rotation()` is daarmee de enige
poort die over een daadwerkelijke rotatie beslist.

### 2.2 State-layout — expliciet project_id-gescopet (bevinding ADR-007)

Alle rotatie-state leeft in het project-resolved data root, onder een **expliciete
project_id-laag**. Dit is de correctie op de r2-claim "alle state is project_id-gescopet":
die claim klopte alleen in central-install mode. In project-local mode (`_resolve_state_root`
branch 4, `vnx_paths.py:420-421`) resolven twee project_ids op dezelfde project_root naar
hetzelfde pad — bewezen door `test_project_id_scoped_across_two_projects`
(`tests/test_context_rotation.py:715`).

**Ontwerp:** de drie path-helpers krijgen de project_id als padcomponent, los van hoe
`_resolve_state_root` resolveert:

```
<data_root>/state/rotation/<project_id>/T0_durable.json     (durable_state_path)
<data_root>/state/rotation/<project_id>/T0_request.json     (request_marker_path)
<data_root>/state/rotation/<project_id>/T0.ready            (ready_signal_path)
<data_root>/state/rotation/<project_id>/T0_rotation.lock    (lease, §3.2)
<data_root>/rotation_handovers/<project_id>/T0/handoff.md   (rotation_handoff_dir)
```

`project_id` voldoet aan `PROJECT_ID_RE = ^[a-z][a-z0-9-]{1,31}$` (`vnx_ids.py:15`) — een
veilige, enkele padcomponent zonder punten of slashes (path-traversal-safe, zelfde redenering
als `_validate_terminal`, `context_rotation.py:93-99`).

Dit is een **deliverable in PR 2** (§5), niet een neveneffect: het maakt crash-teller, lease en
durable state gegarandeerd project_id-disjunct in álle resolutie-modussen en maakt de enige
rode test groen. Omdat de feature dormant is (`enabled: false` overal, geen `T0_durable.json`/
`T0_request.json`/`T0.ready` op schijf) is de pad-verandering compatvrij.

### 2.3 Hoe de tick een verzoek plaatst (bevinding: gedeelde teller)

`_maybe_rotation_tick()` (bash) roept `python3 scripts/lib/rotation_daemon.py tick` aan, die:

1. **Policy leest** (`RotationPolicy.load`). Als `enabled != true` of `respawn !=
   "tmux_new_session"`: schrijf de freshness-timestamp en return. Geen marker.
2. **Durable state leest** (`T0_durable.json`).
3. **Crash-loop-halt checkt**: als `crash_loop_halted_until` in de toekomst ligt, geen verzoek
   (§3.1).
4. **Sessie-duur berekent**: `session_duration = now - last_rotation_at`. Koude start
   (`last_rotation_at = None`): zet `last_rotation_at = now` en return — de klok start, geen
   rotatie op de eerste tick.
5. **Marker-status leest** (`T0_request.json`). Bij een verse `rotation_requested` (al
   gevraagd) of `in_progress` (T0 is al aan het roteren) → return. Alleen bij afwezig of
   afgehandeld (`success`/`aborted`) en `session_duration >= max_session_seconds` → schrijf
   `rotation_requested` met een nieuw `rotation_id` en `based_on_last_rotation_at` (de
   `last_rotation_at` waarop de duur-beslissing is gebaseerd — de generatie-guard, §2.4).
6. **Request-hervalidatie**: ziet de tick een `rotation_requested`-marker terwijl
   `session_duration < max_session_seconds` (de duur die het verzoek rechtvaardigde is niet meer
   bereikt, bijv. na een rotatie die de durable state wél bijwerkte), dan wist hij de marker.
   Zo kan een verzoek nooit langer leven dan de sessie-duur die het rechtvaardigt.
7. **Receipt-emit** bij een daadwerkelijke marker-write (§4).
8. **Freshness-timestamp schrijft** (`.last_rotation_tick_ts`, §4.2).

**De tick raakt `boundaries_since_last_rotation` niet aan.** Die teller is exclusief eigendom
van `checkpoint()` (increment bij elke grens zonder rotatie, `context_rotation.py:836-840`;
reset op bevestigde rotatie, `context_rotation.py:870-873`). In de T0-gemedieerde vorm heeft
de tick zijn eigen cooldown: hij vraagt maximaal één keer per `max_session_seconds` (een verse
`rotation_requested` blokkeert een nieuwe). De r2-problematiek — daemon-tick en `checkpoint()`
schreven allebei in dezelfde teller zonder afgesproken semantiek — vervalt hiermee expliciet:
**de tick schrijft alleen de marker, `checkpoint()` schrijft alleen de teller.**

### 2.4 Hoe T0 het verzoek consumeert (bevinding: marker-status + mid_action-poort)

`checkpoint()` (`context_rotation.py:778`) krijgt één uitbreiding: het leest de marker vóór
`decide_rotation()` en geeft een nieuw `rotation_requested`-signaal door. De guard logica:

```python
# checkpoint(), vóór decide_rotation() — de marker is al gelezen (regel 817-819)
in_flight = _load_json_safe(request_path)
if in_flight and in_flight.get("status") == "in_progress":
    # bestaande duplicate-guard (TTL 120s): al roterend → no-op     [ONGEWIJZIGD]
    ...
# rotation_requested is een geldig verzoek, maar alleen voor de durable-state-
# generatie waarop de tick het schreef (based_on_last_rotation_at). Een verzoek
# dat ouder is dan de laatst bevestigde rotatie is stale en mag niet triggeren.
rotation_requested = bool(
    in_flight
    and in_flight.get("status") == "rotation_requested"
    and in_flight.get("based_on_last_rotation_at") == durable.get("last_rotation_at")
)
```

`decide_rotation()` krijgt één parameter en één tak:

```python
def decide_rotation(
    *,
    policy: RotationPolicy,
    at_governance_boundary: bool,
    boundaries_since_last_rotation: int,
    context_pct: Optional[float] = None,
    mid_action: bool = False,
    rotation_requested: bool = False,                 # NIEUW
) -> RotationDecision:
    if not policy.enabled:                            # poort 1
        return RotationDecision(False, "disabled")
    if mid_action:                                    # poort 2 — LEEFT
        return RotationDecision(False, "mid_action")
    if not at_governance_boundary:                    # poort 3
        return RotationDecision(False, "not_at_boundary")
    if rotation_requested:                            # poort 4 — NIEUW
        # De daemon heeft sessie-duur al gevalideerd; de debounce-teller is
        # niet van toepassing op een daemon-verzoek (de tick-request-cooldown
        # is de debounce). Nog steeds alleen op een echte grens, nooit mid-action.
        return RotationDecision(True, "rotation_requested")
    if policy.trigger != "governance_boundary":       # poort 5 (bestaand)
        return RotationDecision(False, f"unsupported_trigger:{policy.trigger}")
    # ... debounce, pct_ceiling (ONGEWIJZIGD)
```

Toelichting per poort:

- **Poort 1 (`enabled`)** onveranderd. Ook in daemon-verzoek-modus moet `enabled: true` zijn.
- **Poort 2 (`mid_action`)** **leeft.** De daemon roteert niet en heeft dus geen
  mid-action-voorspelling nodig. T0 roept `checkpoint()` aan op een grens waar hij zelf vindt
  dat hij niet mid-action zit. De poort blijft de enige die een daadwerkelijke rotatie tegenhoudt.
- **Poort 3 (`at_governance_boundary`)** blijft voor beide paden gelden. Een daemon-verzoek
  roteert alleen als T0 de checkpoint op een echte grens aanroept. Dit is het kernverschil met
  r2: daar werd `at_governance_boundary=False` geprobeerd (en door regel 316-317 geblokkeerd).
- **Poort 4 (`rotation_requested`)** is de enige nieuwe tak. Hij staat vóór de
  `governance_boundary`-trigger-check en vóór de debounce, met de expliciete aantekening dat de
  request-cooldown van de tick de debounce is. De bestaande boundary-trigger en debounce blijven
  bit-identiek voor T0-geïnitieerde rotatie (handmatig via de rotate-skill).
- **De `boundaries_since_last_rotation`-teller** wordt door het daemon-verzoek **overgeslagen**,
  maar niet geschonden: hij wordt bij een `rotation_requested`-rotatie wél gereset naar 0
  (bestaande code, `context_rotation.py:870-873`). De teller blijft voor T0-geïnitieerde
  rotaties; het daemon-verzoek heeft zijn eigen cooldown. Dit staat expliciet in §2.3.

**Verificatie dat de bestaande poorten echt zo in de code staan** (vereist door de
dispatch-instructie):

```
$ sed -n '312,334p' scripts/lib/context_rotation.py
    if not policy.enabled:
        return RotationDecision(False, "disabled")
    if mid_action:
        return RotationDecision(False, "mid_action")
    if not at_governance_boundary:
        return RotationDecision(False, "not_at_boundary")
    if policy.trigger != "governance_boundary":
        # Only governance_boundary is implemented (verified round 1: no
        # reliable live-% signal for interactive T0).
        return RotationDecision(False, f"unsupported_trigger:{policy.trigger}")

    debounced = boundaries_since_last_rotation < policy.min_boundaries_between_rotations
    pct_backstop = (
        policy.pct_ceiling is not None
        and context_pct is not None
        and context_pct >= policy.pct_ceiling
    )

    if not debounced:
        return RotationDecision(True, "boundary_debounce_cleared")
    if pct_backstop:
        return RotationDecision(True, "pct_ceiling_backstop")
    return RotationDecision(False, "debounced")
```

### 2.5 Marker-status en volledige statusovergangen (bevinding: in-flight-guard)

**Bevinding r2:** `checkpoint()`'s in-flight-guard herkent alleen `status: in_progress`
(`context_rotation.py:820`). In r2 heette de daemon-marker `awaiting_ready`; die kende de guard
niet. In deze vorm heet de marker `rotation_requested` en **kent de guard hem wel** (§2.4).

Volledige statusovergangen:

| Status | Schrijver | Betekenis | Guard/consument |
|---|---|---|---|
| *(geen)* | — | geen verzoek open | tick mag schrijven |
| `rotation_requested` | tick | sessie-duur overschreden; T0 mag bij de volgende grens roteren. Draagt `based_on_last_rotation_at` (de durable `last_rotation_at` op schrijf-moment) | checkpoint leest dit als geldig verzoek (`rotation_requested=True`) **alleen als `based_on_last_rotation_at` nog matcht**; tick schrijft niet over een verse heen en wist 'm als de duur niet meer rechtvaardigt (§2.3.6) |
| `in_progress` | checkpoint | T0 voert de rotatie uit (handoff + respawn) | checkpoint guard: verse `in_progress` → `already_in_progress` no-op (bestaand, TTL 120s); stale `in_progress` → fall-through (bestaand) |
| `success` | checkpoint | rotatie bevestigd; counter gereset; continuation-receipt geëmit | tick mag een nieuw verzoek plaatsen (na `max_session_seconds`) |
| `aborted` | checkpoint | rotatie mislukt (handoff-write-fail of respawn-timeout); counter NIET gereset | tick telt dit voor crash-loop-halt (§3.1); nieuw verzoek pas na cooldown |

```
(geen) ──tick: duration >= max_session_seconds──▶ rotation_requested
rotation_requested ──tick: duration < max────────▶ (geen)  [hervalidatie, §2.3.6]
rotation_requested ──checkpoint: T0 op grens─────▶ in_progress
in_progress ──respawn bevestigd──────────────────▶ success
in_progress ──handoff-fail / respawn-timeout─────▶ aborted
aborted ──tick: na crash-loop-cooldown──────────▶ (geen) / rotation_requested
```

**Idempotentie:** een verse `rotation_requested` (ook als T0 de checkpoint meerdere keren op
dezelfde grens aanroept) leidt tot één rotatie: de eerste `checkpoint()` consumeert 'm
(status → `in_progress`), de tweede ziet `in_progress` en short-circuits. Dit is de bestaande
idempotentie-gedachte uit de docstring (regel 802-804), uitgebreid van `in_progress` naar
`rotation_requested` + `in_progress`.

**TTL-semantiek:** de `request_ttl_seconds` (120s) geldt alleen voor de `in_progress`-
duplicate-guard. Een `rotation_requested`-marker heeft geen TTL: hij blijft staan tot T0 hem
consumeert, de tick hem wist omdat de duur niet meer rechtvaardigt (§2.3.6), of de tick hem
overschrijft. De **generatie-guard** (`based_on_last_rotation_at`) voorkomt dat een verzoek
dóórwerkt ná een bevestigde rotatie: zodra `checkpoint()` `last_rotation_at` bijwerkt, matcht de
`based_on_last_rotation_at` van elk openstaand verzoek niet meer en wordt het bij de volgende
tick gewist. Dit dekt ook het randgeval van een handmatige `/rotate`
(`~/.claude/skills/rotate/SKILL.md`, eigen choreografie die de durable state niet raakt): het
verzoek kan dan nog één grens meeliften, maar wordt bij de eerstvolgende tick gewist zodra de
tick ziet dat het niet meer gerechtvaardigd is — zie §6.13 en het risico-register (R11).

### 2.6 Twee-fasen-model zonder daemon-teardown

De "twee fasen" uit r2 blijven, maar de taakverdeling verandert fundamenteel:

**Fase 1 — verzoek (tick N, in de dispatcher-loop):**
1. `rotation_daemon.tick()` meet `session_duration >= max_session_seconds`.
2. Schrijft de `rotation_requested`-marker met `based_on_last_rotation_at` (onder flock, §3.2).
3. Emit `state_mutation`-receipt (§4).
4. Retourneert onmiddellijk. De dispatcher-loop gaat door. **Geen wachten, geen spawn, geen kill.**

**Fase 2 — uitvoer (T0's eerstvolgende governance-grens):**
1. T0 roept `checkpoint(at_governance_boundary=True, project_id=..., terminal="T0")` aan.
2. `checkpoint()` leest de `rotation_requested`-marker, geeft `rotation_requested=True` door aan
   `decide_rotation()`, die (enabled, niet mid-action, op grens) `should_rotate=True` retourneert.
3. `checkpoint()` schrijft de handoff (`write_t0_handoff`), zet de marker op `in_progress`, en
   roept `respawn()` aan. **Dit is T0's eigen proces** — de dispatcher-loop is hier niet bij
   betrokken en wordt dus nooit geblokkeerd (r2-bevinding "respawn-wachten blokkeert de
   dispatcher" vervalt).
4. `respawn()` spawnt de verse T0 (`tmux new-session -d -s vnx-t0-rotation-<term>-<id>`,
   `context_rotation.py:675`), wacht bounded (default 60s) op de rotation_id-gestempelde
   `.ready`, en reapt bij timeout **alleen de sessie die deze call zelf spawnde**
   (`context_rotation.py:710-718`) — nooit de sessie van de beller (de oude T0).
5. Op `success`: `checkpoint()` reset de teller, zet `last_rotation_at`, schrijft `success`,
   emit continuation-receipt. De oude T0 exit zelf (zijn eigen boundary-logica, zoals
   `CONTEXT_ROTATION.md:216-217` al voorschrijft).
6. Op `aborted`: `checkpoint()` schrijft `aborted`, laat de teller staan, telt voor
   crash-loop-halt (§3.1).

**Waarom er geen twee levende T0's zijn:** er wordt maar één T0 gestart — de successor, door
de bestaande `respawn()`-weg, aangeroepen door de oude T0 zelf. De oude T0 draait door tot de
handshake bevestigd is en exit dan zelf. Er is geen daemon die een tweede start terwijl de
eerste nog draait.

**Waarom er geen destructieve teardown is:** de daemon kilt niets. `tmux kill-session` bestaat
alleen nog als het zelf-reap-pad binnen `respawn()` (`context_rotation.py:716`), dat per
constructie alleen de sessie raakt die diezelfde call heeft aangemaakt — nooit een levende T0.
De r2-fout (daemon zocht `^vnx-t0-` en matchte daarmee alleen zijn eigen verse successor,
`context_rotation.py:675`, terwijl levende T0's `orch-t0`/`mc-t0`/`seo-t0`/`t0-sales-copilot`
heten) is hiermee structureel onmogelijk: er is geen sessie-zoek-logica meer.

**Liveness en `.ready`:** de daemon doet geen liveness-detectie. `.ready` blijft de eenmalige
handshake binnen `respawn()` (rotation_id-gestempeld). Als je tóch daemon-gestuurde liveness
wilt, is een echt signaal nodig (bijv. een periodieke timestamp die de T0 schrijft) — maar die
toevoeging is bewust **niet** in deze track: de dispatcher-supervisor herstart een dode
dispatcher, de operator ziet een dode sessie via `tmux ls`, en de freshness-timestamp (§4.2)
signaleert een stille tick-dood. Een periodiek T0-schrijfsignaal zou een nieuw producer-
protocol en een nieuwe monitoring-check eisen, zonder dat de operator nu iets mist.

---

## 3. De drie hardheidseisen

### 3.1 Crash-loop-halt (herzien voor T0-gemedieerde vorm)

**Wat r2 deed:** de daemon telde gecombineerd falen (`.ready`-timeout óf nieuwe T0 sterft
tijdens teardown) en stopte met respawnen na N pogingen.

**Wat deze vorm doet:** de daemon respawnt niet, dus hij telt ook geen respawn-falen. Maar T0
kan bij elke grens een rotatie blijven proberen die structureel faalt (bijv. tmux-spawn
kapot). Zonder halt blijft T0 per grens falen en blijft de tick verse verzoeken plaatsen.

**Ontwerp:** de faaltelling hoort bij de uitvoering, dus bij `checkpoint()` — die de durable
state toch al schrijft. De tick consumeert de uitkomst.

1. `checkpoint()` op `aborted` (handoff-write-fail of respawn-faal): incrementeer
   `consecutive_spawn_failures` in `T0_durable.json`.
2. Bij `consecutive_spawn_failures >= max_consecutive_failures` (default 3): zet
   `crash_loop_halted_until = now + cooldown_seconds` (default 1800s).
3. `checkpoint()` op `success`: reset `consecutive_spawn_failures = 0` en
   `crash_loop_halted_until = null`.
4. `rotation_daemon.tick()` leest `crash_loop_halted_until`: als die in de toekomst ligt,
   schrijft hij géén `rotation_requested`-marker en logt `crash_loop_halted: true`.
5. Na de cooldown: reset de teller; de volgende tick mag weer verzoeken.

`max_consecutive_failures` en `cooldown_seconds` zijn configureerbaar via
`configs/context_rotation.yaml` met env-override (`VNX_T0_ROTATION_MAX_FAILURES`,
`VNX_T0_ROTATION_COOLDOWN_SECONDS`).

**Waarom dit werkt zonder daemon-spawn:** de halt onderbreekt de *verzoek*-cyclus, niet de
uitvoering. Een T0 die op een grens `rotation_requested` mist, roteert niet — het verzoek komt
pas terug nadat de cooldown verlopen is en de tick opnieuw mag verzoeken.

### 3.2 Handoff-lease: `fcntl.flock` (behouden uit r2)

`fcntl.flock` blijft — kernel-beheerd, automatisch vrijgegeven bij procesdood, geen
PID-hergebruik. In de T0-gemedieerde vorm is het doel iets anders geworden:

- **r2 doel:** twee daemon-instanties mogen niet gelijktijdig een rotatie *starten*.
- **deze vorm:** twee entiteiten schrijven in hetzelfde state-pad — de tick schrijft de
  `rotation_requested`-marker en `checkpoint()` zet `in_progress`/`success`/`aborted`. Zonder
  lease kan een dispatcher-restart (oud proces + nieuw proces) twee markers tegelijk schrijven,
  en kan de tick over een `in_progress`-marker heen schrijven.

**Ontwerp:** `acquire_rotation_lease(lock_path, timeout_seconds=5.0)` op
`<state>/rotation/<project_id>/T0_rotation.lock`. Gehouden rond:
1. Elke marker-write in `rotation_daemon.tick()`.
2. De marker-statusovergang in `checkpoint()` (van `rotation_requested` naar `in_progress`).

```python
import fcntl

def acquire_rotation_lease(lock_path: Path, timeout_seconds: float = 5.0) -> bool:
    """Exclusive fcntl.flock on <lock_path>. Non-blocking with a bounded
    retry loop. True if acquired; False if another process holds it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True  # fd blijft open — lock leeft zolang het proces leeft
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.1)
```

Lock-bestand is leeg; de lock zit op de file descriptor. Zelfde mechanisme als de
singleton-enforcer (`scripts/singleton_enforcer.sh` gebruikt `flock`), maar dan in Python.

### 3.3 Gefaseerde uitrol en eigen schakelaar (bevinding overige #6)

**Wat r2 fout deed:** PR 3 zette `enabled: true` plus respawn in één keer fleet-breed aan, en
leunde voor rollback op `VNX_SUPERVISOR_MODE=legacy` — maar die zet ook lease_sweep,
runtime_supervise, de OI-bridge en reconcile uit. Dat is geen kill-switch voor rotatie.

**Ontwerp — drie onafhankelijke niveaus:**

| Niveau | Schakelaar | Default | Scope |
|---|---|---|---|
| 1. Mechanisme | `configs/context_rotation.yaml:enabled` | `false` | `checkpoint()`/`decide_rotation()` (T0-geïnitieerd) |
| 2. Uitvoerbaar | `configs/context_rotation.yaml:respawn` | `off` | `checkpoint()` spawnt een successor |
| 3. Tick verzoekt | `VNX_T0_ROTATION_DAEMON=1` | `0` (uit) | alleen de rotatie-tick; **onafhankelijk van `VNX_SUPERVISOR_MODE`** |

**De rotatie-tick gated op `VNX_T0_ROTATION_DAEMON=1` + `enabled: true` + `respawn:
tmux_new_session` in de policy** — en **niet** op `VNX_SUPERVISOR_MODE`. Daarmee:
- Rotatie uitzetten (`VNX_T0_ROTATION_DAEMON=0`) raakt geen enkele andere tick.
- Rotatie aanzetten eist geen `unified`-modus (en zet dus geen lease_sweep/
  runtime_supervise/OI-bridge/reconcile aan die de operator niet wilde).
- Zonder `enabled: true` of met `respawn: off` is de tick een no-op (alleen freshness-
  timestamp) — een droge run die nooit een verzoek plaatst, dus nooit het "T0 roteert zonder
  successor"-geval creëert.

**Uitrolpad:**
1. **PR 1-2:** code + tests landen met `VNX_T0_ROTATION_DAEMON` default `0`. Geen
   productie-impact; de tick bestaat maar draait niet.
2. **PR 3:** operator zet per project opt-in aan, één voor één: `VNX_T0_ROTATION_DAEMON=1`,
   `enabled: true`, `respawn: tmux_new_session`. Volgorde: vnx-dev eerst (minimaal 24h
   observatie), dan vnx-orchestration, dan de rest van de fleet. Elke stap is een bewuste
   config-actie, geen fleet-flip.
3. **PR 4:** docs + integratietests landen; de uitrol is dan al op vnx-dev geverifieerd.

---

## 4. Receipt-vloer (herzien: `state_mutation`, geen nieuw `health`-kind)

**Bevinding r2/B3:** 720 receipts per dag per project (elke 120s één) tegen een grootboek van
~23.000, zonder retentie of sampling. Bovendien: `receipt_kind: "health"` zit **niet** in de
gesloten set `RECEIPT_KINDS` (`dispatch_identity.py:67-77`) en `validate_receipt_kind`
(`dispatch_identity.py:79-89`) gooit een `ValueError`.

### 4.1 Keuze: bestaande soort `state_mutation`, geen set-uitbreiding

**De tick emit géén receipt per tick.** Hij emit alleen bij een echte state-transitie:
wanneer hij een `rotation_requested`-marker schrijft. Dat is een state-mutatie (filesystem
state), en `state_mutation` is een bestaande soort in de gesloten set — dezelfde die de
continuation-receipt al gebruikt (`context_rotation.py:752`). Er is dus **geen
`RECEIPT_KINDS`-uitbreiding nodig**.

| Gebeurtenis | Receipt? | `receipt_kind` | `event_type` | `dispatch_id` |
|---|---|---|---|---|
| Tick draait, geen verzoek (duration < max) | Nee (alleen freshness-ts) | — | — | — |
| Tick schrijft `rotation_requested` | Ja | `state_mutation` | `rotation_requested` | `rotation_id` |
| Tick geblokkeerd door crash-loop-halt | Nee (log + freshness-ts) | — | — | — |
| T0 bevestigt rotatie | Ja (bestaand) | `state_mutation` | `context_rotation_continuation` | `rotation_id` |
| Rotatie `aborted` | Nee (marker + log; counter telt) | — | — | — |

**Volume:** bounded door de rotatiefrequentie, niet door de tick-interval. Met
`max_session_seconds` op 4 uur: maximaal ~6 `rotation_requested` + ~6 continuation per dag per
project ≈ 12 receipts. Dat is verwaarloosbaar tegen een grootboek van ~23.000 en heeft **geen
eigen retentie of sampling nodig** — het volume is zelf-limiterend.

**Payload van de request-receipt:**

```json
{
  "event_type": "rotation_requested",
  "receipt_kind": "state_mutation",
  "terminal": "T0",
  "dispatch_id": "<rotation_id>",
  "role": "t0-rotation-daemon",
  "timestamp": "2026-08-02T12:00:00Z",
  "project_id": "vnx-dev",
  "source": "rotation_daemon",
  "fields": {
    "reason": "session_duration_exceeded",
    "seconds_since_last_rotation": 14400,
    "max_session_seconds": 14400,
    "crash_loop_halted": false,
    "consecutive_spawn_failures": 0
  }
}
```

`dispatch_id` = `rotation_id` (UUID hex) — zelfde id als de latere continuation-receipt, zodat
de lees-modellen (`conversation_read_model.py`) het verzoek en de bevestiging aan elkaar
ketenen. `role` is `t0-rotation-daemon`, geen `unknown`.

**Waarom geen `health`-soort en geen 720/dag:** de lees-modellen filteren per `receipt_kind`;
een `health`-soort zou die filters, retentie en aggregaties moeten aanpassen, allemaal voor een
signaal dat de freshness-timestamp (§4.2) al goedkoper en deterministisch levert. De gesloten
set blijft stabiel. De "receipt-vloer"-gedachte van r2 — elke tick een spoor achterlaten om een
stille dood te detecteren — wordt overgenomen door de freshness-check, niet door receipts.

### 4.2 Freshness — eigen timestamp, gecheckt door een bestaande tick

**Het signaal:** de tick schrijft elke keer dat hij draait `<state>/rotation/.last_rotation_tick_ts`
(unix-timestamp). Zelfs als hij géén verzoek plaatst (duration te laag, crash-loop-halt,
policy uit).

**De check:** de bestaande `_maybe_runtime_supervise`-tick (60s interval,
`dispatcher_supervisor_ticks.sh:26-50`) leest deze timestamp. Ouder dan `max_tick_interval * 3`
(default 360s): log op ERROR-niveau "rotation tick stale". Dit is geen nieuwe monitor — een
extra regel in een tick die al draait. Determinisch: timestamp lezen, met `now` vergelijken.

**Fallback:** sterft de dispatcher zelf, dan sterft ook `_maybe_runtime_supervise` en is er
geen proces dat alarmeert — hetzelfde faalpad als nu; de dispatcher-supervisor herstart 'm en
is het uiteindelijke vangnet.

---

## 5. Opsplitsing in deliverables

### task_class per deliverable

| PR | task_class | Rationale |
|---|---|---|
| PR 1 | `foundation` | Tick + marker-plumbing + `decide_rotation`/`checkpoint`-uitbreiding. Geen gedragsverandering, alleen meetbaarheid + verzoek-pad. |
| PR 2 | `safety` | Crash-loop-halt + flock-lease + project_id-state-scoping-fix. Harde mechanismen. |
| PR 3 | `activation` | Per-project opt-in + freshness-alarm + uitrol. Maakt de feature actief onder operator-controle. |
| PR 4 | `validation` | Integratietests + documentatie. Bewijst dat het geheel werkt. |

### model-routing-vloer per deliverable

| PR | Model-calls? | Toelichting |
|---|---|---|
| PR 1 | Nee | Bash-tick + Python (`rotation_daemon.tick`, `decide_rotation`-tak) — timestamps, config, marker-JSON. Volledig deterministisch. |
| PR 2 | Nee | `fcntl.flock()`, teller-logica, pad-herstructurering. Volledig deterministisch. |
| PR 3 | Nee | Env/config-uitlezing, timestamp-vergelijking. Volledig deterministisch. |
| PR 4 | Nee | Integratietests met injectable `tmux_spawn_fn`/`tmux_kill_fn`. Deterministische mocks. |

**Geen enkele deliverable bevat een model-call.** De enige model-afhankelijkheid is indirect:
`respawn()` spawnt een `claude --model opus`-proces (de successor). Maar dat gebeurt in
`checkpoint()` (T0's eigen proces), niet in de daemon, en de daemon neemt geen model-
beslissingen. De routing is: **geen model** — vaste regels, tellers, timestamps en
kernel-primitieven (`flock`, `tmux`).

### Size-gate-verantwoording (bevinding overige: size-gate op PR2)

`scripts/lib/context_rotation.py` is **898** regels en staat **niet** op `FILE_SIZE_ALLOWLIST`
(`quality_advisory.py:36-59`). `FILE_SIZE_BLOCKING_PYTHON` is **1200** (`quality_advisory.py:22`).
De r2-begroting (+120 in PR1 en +200 in PR2) bracht het bestand op 1218 — een blocking
gate-fail bij geboorte, want `file_size_blocking` is `severity="blocking"` en `pre_merge_gate`
blokkeert daarop.

**Deze vorm splitst van meet af aan:**

| Bestand | Nu | PR 1 | PR 2 | Max | Gate |
|---|---|---|---|---|---|
| `scripts/lib/context_rotation.py` | 898 | +50 → 948 | +50 → 998 | **998** | < 1200 ✓ |
| `scripts/lib/rotation_daemon.py` (nieuw) | — | ~180 | +60 → 240 | **240** | < 1200 ✓ |
| `scripts/lib/dispatcher_supervisor_ticks.sh` | 224 | +30 | — | 254 | < 600 ✓ |
| `tests/test_context_rotation.py` | 1127 | +80 | +120 | ~1330 | test (advisory) |
| `tests/test_rotation_daemon.py` (nieuw) | — | ~150 | +80 | ~230 | test (advisory) |

- **`context_rotation.py`** houdt alleen de T0-kant: `decide_rotation()` (+1 param, +1 tak,
  ~15 regels) en `checkpoint()` (marker-lezen + statusovergang + generatie-guard, ~40 regels).
  Blijft op ~998.
- **Nieuwe module `scripts/lib/rotation_daemon.py`** neemt de daemon-kant: `tick()`,
  `_should_request_rotation()`, `_write_request_marker()`, `_emit_request_receipt()`,
  `acquire_rotation_lease()`, en de crash-loop-helpers. Nieuw bestand, ~240 regels, ruim onder
  1200. Geen allowlist-entry nodig.
- **Functie-grootte:** `FUNCTION_SIZE_BLOCKING_PYTHON` is 70 (`quality_advisory.py:89`). De r2
  `rotation_tick()` was op ~80 begroot. In deze vorm is `rotation_daemon.tick()` gesplitst in
  helpers van elk < 70 regels (zie boven). Transparantie: de functie-grootte-gate emitteert als
  `severity="warning"` (`quality_advisory.py:305-314`), dus geen hard block — maar de opsplitsing
  houdt het bestand schoon en onder de soft-max.

**Alternatief dat bewust níét gekozen is:** `context_rotation.py` op de allowlist zetten. Dat
bevriest een groeiend monolith in plaats van hem te splitsen; de dispatch-instructie vraagt
expliciet "splits of verplaats". De splitsing hierboven doet beide: de daemon-kant verhuist naar
een nieuw bestand, de T0-kant blijft klein.

---

### PR 1: Tick + verzoek-marker + `decide_rotation`/`checkpoint`-uitbreiding (foundation)

**Wat:** `_maybe_rotation_tick` in `dispatcher_supervisor_ticks.sh` (bash), `rotation_daemon.tick`
(python), en de T0-kant: `decide_rotation()` krijgt `rotation_requested`, `checkpoint()` leest
de marker. Geen gedragsverandering (alle vlaggen uit).

**Bestanden:**
- `scripts/lib/rotation_daemon.py` (nieuw) — `tick()` (~25), `_should_request_rotation()`
  (~35), `_write_request_marker()` (~30), `_emit_request_receipt()` (~30), freshness-schrijver
  (~10). ~180 regels.
- `scripts/lib/context_rotation.py` — `decide_rotation()`: parameter + tak (~15).
  `checkpoint()`: marker-lezen vóór `decide_rotation()`, `rotation_requested`-signaal met
  generatie-guard (`based_on_last_rotation_at`), en de statusovergang
  `rotation_requested → in_progress` (~40).
- `scripts/lib/dispatcher_supervisor_ticks.sh` — `_maybe_rotation_tick()` (~30), zelfde
  throttle-patroon als `_maybe_runtime_supervise` maar gated op `VNX_T0_ROTATION_DAEMON`.
- `scripts/dispatcher_minimal.sh` — call `_maybe_rotation_tick` in `process_dispatches()`,
  na `_maybe_learning_cycle` (regel 606).
- `configs/context_rotation.yaml` — nieuwe velden: `max_session_seconds: 14400`,
  `max_consecutive_failures: 3`, `cooldown_seconds: 1800`.
- `tests/test_context_rotation.py` (+~80) en `tests/test_rotation_daemon.py` (nieuw, ~150).

**Afhankelijkheden:** geen.

**Verificatie:**
- `decide_rotation(rotation_requested=True, at_governance_boundary=True, mid_action=False,
  enabled=True)` retourneert `should_rotate=True, reason="rotation_requested"`.
- `decide_rotation(rotation_requested=True, mid_action=True)` retourneert `False` (poort 2).
- `decide_rotation(rotation_requested=True, at_governance_boundary=False)` retourneert `False`
  (poort 3).
- Bestaande `governance_boundary`-paden: alle bestaande tests blijven groen.
- `rotation_daemon.tick` schrijft een `rotation_requested`-marker (met
  `based_on_last_rotation_at`) bij `session_duration >= max_session_seconds` en geen verse
  marker.
- `rotation_daemon.tick` overschrijft een verse `rotation_requested`/`in_progress` niet, en wist
  een `rotation_requested` zodra `session_duration < max_session_seconds`.
- Generatie-guard: `checkpoint()` honoreert een `rotation_requested` alleen als
  `based_on_last_rotation_at == durable.last_rotation_at`; een verzoek dat op een oudere
  generatie slaat wordt genegeerd.
- `_maybe_rotation_tick` throttle: tweede call binnen interval is no-op.
- `_maybe_rotation_tick` met `VNX_T0_ROTATION_DAEMON=0` (of unset): no-op.
- `python3 -m pytest tests/test_context_rotation.py tests/test_rotation_daemon.py -q` — groen.

**Grootte:** ~380 regels (180 python + 45 python-uitbreiding + 30 bash + 1 dispatcher + 5 config
+ ~230 tests).

---

### PR 2: Crash-loop-halt + flock-lease + project_id-state-scoping (safety)

**Wat:** crash-loop-halt (§3.1), `fcntl.flock`-lease (§3.2), en de expliciete
project_id-laag in de state-paden (§2.2) die de enige rode test groen maakt.

**Bestanden:**
- `scripts/lib/context_rotation.py` — crash-loop-helpers (`_record_spawn_failure`,
  `_evaluate_crash_loop`, `_reset_crash_loop`, ~40) en pad-helpers: `rotation_state_dir`,
  `rotation_handoff_dir`, `durable_state_path`, `request_marker_path`, `ready_signal_path`
  krijgen de project_id-laag (~25 wijziging).
- `scripts/lib/rotation_daemon.py` — `acquire_rotation_lease()` (+20), crash-loop-uitlezing in
  `_should_request_rotation` (+40).
- `tests/test_context_rotation.py` (+~120) — crash-loop-transities, lease-race,
  project_id-scoping (de bestaande `test_project_id_scoped_across_two_projects` gaat van rood
  naar groen).
- `tests/test_rotation_daemon.py` (+~80) — lease om marker-writes, halt-gedrag in de tick.

**Afhankelijkheden:** PR 1 (de tick en `rotation_requested` moeten bestaan).

**Verificatie:**
- 3 achtereenvolgende `aborted` → `crash_loop_halted_until` in de toekomst; tick plaatst geen
  verzoek; na cooldown wordt gereset.
- `success` reset de faalteller direct.
- Lease: tweede concurrente acquire faalt binnen timeout; kernel geeft vrij bij procesdood.
- `test_project_id_scoped_across_two_projects` is groen: `rotation_handoff_dir("project-a")`
  != `rotation_handoff_dir("project-b")` op dezelfde project_root.
- `python3 -m pytest tests/test_context_rotation.py tests/test_rotation_daemon.py -q` — groen.

**Grootte:** ~310 regels (90 python + 200 tests).

---

### PR 3: Per-project opt-in + freshness-alarm + uitrol (activation)

**Wat:** de tick wordt per project aanzetbaar (§3.3), freshness-alarm in
`_maybe_runtime_supervise` (§4.2), en de uitrol-playbook-documentatie.

**Bestanden:**
- `scripts/lib/runtime_supervise.py` — leest `.last_rotation_tick_ts`, logt ERROR bij
  stale > 360s (~15 regels).
- `scripts/lib/dispatcher_supervisor_ticks.sh` — geen wijziging nodig aan de gate (al in PR 1);
  de interval-var `VNX_ROTATION_TICK_INTERVAL` documenteren.
- `configs/context_rotation.yaml` — comments bij de nieuwe velden; per-project voorbeeld.
- `docs/operations/CONTEXT_ROTATION.md` — sectie "Daemon-verzoek (T0-gemedieerd)" met de
  opt-in-stappen en de uitrol-volgorde.
- `tests/test_runtime_supervise.py` (of bestaand runtime-supervise-testbestand) — stale-alarm.

**Afhankelijkheden:** PR 2.

**Verificatie:**
- `VNX_T0_ROTATION_DAEMON=1` + `enabled: true` + `respawn: tmux_new_session`: tick schrijft
  verzoeken.
- `VNX_T0_ROTATION_DAEMON=0`: tick draait, schrijft freshness-ts, geen verzoeken.
- `enabled: false` met `VNX_T0_ROTATION_DAEMON=1`: tick no-op (geen verzoeken).
- Freshness: `.last_rotation_tick_ts` wordt elke tick bijgewerkt; runtime_supervise alarmeert
  als de ts > 360s oud is.
- `python3 -m pytest ...` — groen.

**Grootte:** ~150 regels (25 python + 30 docs + ~95 tests).

---

### PR 4: End-to-end integratietest + documentatie (validation)

**Wat:** integratietest die de volledige keten aflegt (tick schrijft verzoek → checkpoint
consumeert → respawn → `.ready` → continuation-receipt → oude T0 exit), plus
documentatie-update.

**Bestanden:**
- `tests/test_rotation_daemon_integration.py` (nieuw, ~220) — injectable `tmux_spawn_fn`/
  `tmux_kill_fn`; mock-successor schrijft `.ready`; assert marker-transities
  `rotation_requested → in_progress → success`, continuation-receipt, counter-reset.
- `tests/test_rotation_daemon_integration.py` — crash-loop-integratie: 3 `aborted` → halt →
  cooldown → nieuw verzoek.
- `docs/operations/CONTEXT_ROTATION.md` — nieuwe sectie "Daemon-verzoek (T0-gemedieerd)" met
  architectuur-diagram en statusovergangen.
- `docs/operations/UNIFIED_SUPERVISOR.md` — rotatie-tick toevoegen (met de aantekening dat de
  tick NIET van `VNX_SUPERVISOR_MODE` afhangt).

**Afhankelijkheden:** PR 3.

**Verificatie:**
- Integratietest: mock-tick schrijft verzoek → mock-checkpoint consumeert → mock-ready →
  verify receipt + marker `success` + counter-reset.
- Crash-loop-integratie: 3 failed spawns → halt → cooldown → retry.
- Lease-integratie: twee concurrente marker-writes → één wint.
- Documentatie consistent met code.
- `python3 -m pytest tests/test_rotation_daemon_integration.py tests/test_rotation_daemon.py
  tests/test_context_rotation.py -q` — groen.

**Grootte:** ~350 regels (220 tests + 130 docs).

### Afhankelijkheidsgraaf

```
PR 1 (foundation: tick + marker + decide/checkpoint-uitbreiding)
  └── PR 2 (safety: crash-loop + lease + project_id-scoping)
        └── PR 3 (activation: per-project opt-in + freshness)
              └── PR 4 (validation: integratietests + docs)
```

Elke PR is onafhankelijk deploybaar:
- PR 1: voegt een tick toe die draait maar niets doet zonder vlaggen. Geen gedragsverandering.
- PR 2: voegt veiligheidsmechanismen toe, nog steeds uit.
- PR 3: maakt het verzoek-pad actief, alleen met expliciete per-project opt-in.
- PR 4: tests + docs, geen gedragsverandering.

---

## 6. Wat het NIET doet (scope-grenzen)

1. **Geen daemon-hard-kill.** De daemon kilt nooit een tmux-sessie. `tmux kill-session` bestaat
   alleen nog als het zelf-reap-pad binnen `respawn()` voor de sessie die die call zelf
   aanmaakte.
2. **Geen daemon-spawn.** De daemon start nooit een T0. Spawnen gebeurt uitsluitend via
   `respawn()`, aangeroepen door `checkpoint()` in T0's eigen proces. "Spawn-from-nothing" blijft
   buiten scope (scope-grens uit `CONTEXT_ROTATION.md`).
3. **Geen sessie-naam-afhankelijkheid.** De daemon zoekt geen sessies via `tmux list-sessions`
   of een naming convention. De r2-fout (zoeken op `^vnx-t0-`, dat alleen de eigen
   `vnx-t0-rotation-*`-successor matchte, terwijl levende T0's anders heten) is hiermee
   structureel onmogelijk — er is geen sessie-zoek-logica.
4. **Geen headless T0.** De successor draait interactief in tmux (`claude --model opus`, geen
   `-p`/`--print`). Onveranderd uit r2.
5. **Geen automatische `/goal`-hervatting.** De verse T0 resume via `vnx handoff show`
   (`_build_resume_prompt`). De daemon is geen taakplanner.
6. **Geen cross-project rotatie.** De tick draait per project (één dispatcher per project) en
   schrijft alleen in het eigen project_id-gescopete pad.
7. **Geen vervanging van native compaction.** Compaction blijft de baseline; rotatie is een
   aanvullende laag.
8. **Geen T1/T2/T3-rotatie.** Worker-rotatie (`hooks/vnx_rotate.sh`) is een apart systeem.
9. **Geen self-healing van de dispatcher-supervisor.** Een dode dispatcher wordt herstart door
   `dispatcher_supervisor.sh`; de freshness-check signaleert een stille tick-dood maar herstart
   niet.
10. **Geen model-downgrade voor de verse T0.** `_SUCCESSOR_MODEL = "opus"`
    (`context_rotation.py:82`) blijft hard.
11. **Geen directe writes naar `.vnx-data/`.** Alle state via de bestaande path-helpers en
    `append_receipt_payload()`.
12. **Geen nieuwe liveness-producent.** Zonder daemon-gestuurde liveness-detectie is er geen
    periodiek T0-schrijfsignaal nodig; `.ready` blijft de eenmalige handshake.
13. **Geen integratie met de handmatige `/rotate`-skill.** `~/.claude/skills/rotate/SKILL.md`
    heeft een eigen choreografie (nieuw venster in dezelfde sessie, `/kickoff`) die de durable
    state en de request-marker niet raakt. Deze track laat die skill ongemoeid. Randgeval:
    een stale `rotation_requested` kan na een handmatige `/rotate` nog één grens meeliften;
    de tick wist 'm zodra de duur niet meer rechtvaardigt (§2.3.6, §2.5). De skill laten
    meeschrijven op de marker is een expliciete vervolg-track, niet deze.

---

## 7. Risico-register

| # | Risico | Kans | Impact | Mitigatie | Sectie |
|---|---|---|---|---|---|
| R1 | T0 komt lang niet bij een grens; het verzoek blijft lang staan | Laag | Laag | `rotation_requested` heeft geen TTL; het blijft staan tot de volgende grens. `max_session_seconds` is een zachte drempel — T0 draait door met compaction als vangnet. | §2.5 |
| R2 | `checkpoint()` faalt structureel (tmux-spawn kapot); T0 blijft per grens proberen | Laag | Middel | Crash-loop-halt: na 3 `aborted` stopt de tick met verzoeken tot de cooldown. | §3.1 |
| R3 | Twee dispatcher-instanties (oud + nieuw na restart) schrijven tegelijk een marker | Zeer laag | Middel | `fcntl.flock` met `LOCK_NB` is atomisch op POSIX; de tweede acquire faalt binnen 5s. | §3.2 |
| R4 | `fcntl.flock` niet beschikbaar op niet-POSIX | N.v.t. | N.v.t. | VNX draait alleen op macOS/Linux. | §3.2 |
| R5 | `respawn()` timeout laat de oude T0 zitten zonder successor | Laag | Middel | `respawn()` reapt alleen zijn eigen orphan-sessie; de oude T0 blijft draaien. Crash-teller telt. Geen verloren sessie. | §2.6 |
| R6 | De tick-emit van request-receipts groeit onbeheerst | N.v.t. | Geen | Volume is gebonden aan rotatiefrequentie (~12/dag max), niet aan de tick-interval. Geen retentie nodig. | §4.1 |
| R7 | Twee project_ids onder één project_root collideren in project-local mode | N.v.t. | Hoog (zonder fix) | Verholpen in PR 2: expliciete project_id-laag in de state-paden maakt de state altijd project_id-disjunct. | §2.2, §8.3 |
| R8 | Operator zet `enabled: true` + `respawn: off` en daarna `VNX_T0_ROTATION_DAEMON=1` — T0 roteert zonder successor | Laag | Hoog | De tick schrijft alleen een verzoek als de policy ook `respawn: tmux_new_session` heeft. Met `respawn: off` is de tick een no-op. | §3.3 |
| R9 | `rotation_id` als `dispatch_id` botst met echte dispatch-ids | N.v.t. | Geen | `rotation_id` is UUID-hex; dispatch-ids zijn `YYYYMMDD-HHMMSS-<slug>`. Geen overlap. | §4.1 |
| R10 | De daemon schrijft een verzoek voor een T0 die niet bestaat (niemand draait 'm) | Laag | Laag | De marker blijft staan; zodra een T0 opstart en een grens bereikt, consumeert hij 'm. Geen spawn-from-nothing, geen sessie-zoek-logica. | §2.3, §6.3 |
| R11 | Stale `rotation_requested` na een handmatige `/rotate` leidt tot een dubbele rotatie bij de eerstvolgende grens | Zeer laag | Middel | Generatie-guard (`based_on_last_rotation_at`): een verzoek dat niet meer op de actuele durable-generatie slaat wordt door `checkpoint()` genegeerd en door de tick gewist (§2.3.6). Volledige integratie met de handmatige skill is een vervolg-track (§6.13). | §2.4, §2.5, §6.13 |

---

## 8. Toetsingscriteria

### 8.1 task_class per deliverable

| PR | task_class | Definitie |
|---|---|---|
| PR 1 | `foundation` | Infrastructuur zonder gedragsverandering. Feature is meetbaar maar niet actief. |
| PR 2 | `safety` | Harde veiligheidsmechanismen (halt, lease, scoping). Zonder deze mag de feature niet aan. |
| PR 3 | `activation` | Maakt de feature actief onder expliciete per-project operator-controle. |
| PR 4 | `validation` | Bewijst dat het geheel werkt en documenteert het. |

### 8.2 model-routing-vloer

Alle vier PR's zijn volledig deterministisch — vaste regels, tellers, timestamps en
kernel-primitieven (`flock`, `tmux`). Geen enkele PR bevat een model-call. De enige
model-afhankelijkheid is indirect (de successor wordt als `claude --model opus` gespawnd), en
die zit in T0's eigen `checkpoint()`-proces, niet in de daemon. Per PR gedetailleerd in §5.

### 8.3 ADR-007 — expliciete uitspraak

**Letter (niet van toepassing):** ADR-007 verplicht `project_id`-stamping en composiet-UNIQUE
op alle **centrale-DB-tabellen**. Deze track voegt geen centrale-DB-tabellen toe, geen
migraties, geen UNIQUE-constraints. De bewijs-test (`tests/test_migrate_dry_run.py`) wordt niet
geraakt. Er is dus geen ADR-007-migratie-deliverable.

**Geest (wél toegepast, en de r2-claim gecorrigeerd):** r2 stelde "alle state leeft in het
bestaande project_id-gescopete pad". Dat is **onjuist** in project-local mode: de enige rode
test in de suite (`test_project_id_scoped_across_two_projects`,
`tests/test_context_rotation.py:715`) bewijst dat twee project_ids op dezelfde project_root
naar hetzelfde pad resolven (`_resolve_state_root` branch 4, `vnx_paths.py:420-421`). Daar
landden crash-teller, lease en durable state. PR 2 verhelpt dit structureel: de state-paden
krijgen een expliciete project_id-laag (§2.2), zodat de state in álle resolutie-modussen
project_id-disjunct is. De ADR-007-geest — "project_id is de identiteitslaag" — is daarmee
gerespecteerd, nu ook voor filesystem-state in plaats van alleen DB-tabellen.

---

## Appendix A: Geverifieerde regelnummers

Elk hieronder genoemd regelnummer is geverifieerd met het bijbehorende commando.

```
# Bestandsscan context_rotation.py — 898 regels (r2-appendix zei 899; gecorrigeerd)
$ wc -l scripts/lib/context_rotation.py
898 scripts/lib/context_rotation.py

# Functie-definities
$ grep -n "^def \|^class " scripts/lib/context_rotation.py
93:def _validate_terminal(terminal: str) -> str:
119:def _project_data_root(project_id: str, project_root: Optional[Path] = None) -> Path:
131:def rotation_state_dir(project_id: str, project_root: Optional[Path] = None) -> Path:
135:def rotation_handoff_dir(
142:def durable_state_path(
149:def request_marker_path(
156:def ready_signal_path(
220:class RotationPolicy:
293:class RotationDecision:
298:def decide_rotation(
403:def write_t0_handoff(*, logdir: Path, project_root: Path, project_id: str) -> Path:
512:class RespawnResult:
520:def _build_resume_prompt(
535:class SpawnPartialFailure(RuntimeError):
548:def _default_tmux_spawn(
610:def _default_tmux_kill(session_name: str) -> None:
617:def _check_ready(ready_path: Path, rotation_id: str) -> bool:
624:def write_ready_signal(
642:def respawn(
731:class RotationOutcome:
740:def _emit_continuation_receipt(
769:def _load_durable(path: Path) -> Dict[str, Any]:
778:def checkpoint(

# decide_rotation gates — de poorten waarmee r1/r2 fout ging
$ sed -n '312,334p' scripts/lib/context_rotation.py
    if not policy.enabled:
        return RotationDecision(False, "disabled")
    if mid_action:
        return RotationDecision(False, "mid_action")
    if not at_governance_boundary:
        return RotationDecision(False, "not_at_boundary")
    if policy.trigger != "governance_boundary":
        # Only governance_boundary is implemented (verified round 1: no
        # reliable live-% signal for interactive T0).
        return RotationDecision(False, f"unsupported_trigger:{policy.trigger}")
    ... (debounce, pct_ceiling)

# Opus-pin
$ grep -n "_SUCCESSOR_MODEL\|--model opus" scripts/lib/context_rotation.py
82:_SUCCESSOR_MODEL = "opus"
597:            ["tmux", "send-keys", "-t", session_name, "-l", f"claude --model {_SUCCESSOR_MODEL}"],

# De successor-sessienaam die de r2 "^vnx-t0-" zoekterm wél matchte
$ sed -n '675p' scripts/lib/context_rotation.py
    session_name = f"vnx-t0-rotation-{terminal.lower()}-{rotation_id[:8]}"

# De in-flight-guard die alleen "in_progress" kent (bevinding marker-status)
$ sed -n '818,828p' scripts/lib/context_rotation.py
    now = now_fn()
    in_flight = _load_json_safe(request_path)
    if in_flight and in_flight.get("status") == "in_progress":
        created_at = in_flight.get("created_at")
        if created_at and _seconds_since(created_at, now) < request_ttl_seconds:
            return RotationOutcome(
                rotated=False, reason="already_in_progress", rotation_id=in_flight.get("rotation_id"),
            )
        # Stale in_progress marker (a previous attempt crashed mid-flight
        # without ever writing an outcome) — fall through and retry.

# Debounce-teller: checkpoint is de enige schrijver
$ sed -n '836,845p' scripts/lib/context_rotation.py
    if not decision.should_rotate:
        if at_governance_boundary:
            durable["boundaries_since_last_rotation"] = durable.get("boundaries_since_last_rotation", 0) + 1
            _write_json_atomic(durable_path, durable)
        return RotationOutcome(rotated=False, reason=decision.reason)

    rotation_id = uuid.uuid4().hex[:12]
    _write_json_atomic(request_path, {
        "rotation_id": rotation_id, "status": "in_progress", "created_at": _iso(now),
    })

# Reset op bevestigde rotatie + success-status
$ sed -n '870,880p' scripts/lib/context_rotation.py
    if confirmed:
        durable["boundaries_since_last_rotation"] = 0
        durable["last_rotation_at"] = _iso(now_fn())
        _write_json_atomic(durable_path, durable)
        _write_json_atomic(request_path, {
            "rotation_id": rotation_id, "status": "success", "created_at": _iso(now),
        })
        _emit_continuation_receipt(
            terminal=terminal, dispatch_id=rotation_id, handoff_path=str(handoff_path),
            context_pct=context_pct, project_id=project_id,
        )

# respawn() blokkeert (tot 60s) — alleen in T0's eigen proces in deze vorm
$ sed -n '701,723p' scripts/lib/context_rotation.py
    start = time_fn()
    while True:
        if _check_ready(ready_path, rotation_id):
            return RespawnResult(
                success=True, reason="ready", session_name=session_name,
                rotation_id=rotation_id, waited_seconds=time_fn() - start,
            )
        elapsed = time_fn() - start
        if elapsed >= timeout_seconds:
            ...
            try:
                kill(session_name)
            ...

# Continuation-receipt gebruikt state_mutation (bestaande gesloten-set-soort)
$ sed -n '750,752p' scripts/lib/context_rotation.py
    receipt = {
        "event_type": "context_rotation_continuation",
        "receipt_kind": "state_mutation",

# RECEIPT_KINDS gesloten set — health zit er NIET in
$ sed -n '67,90p' scripts/lib/dispatch_identity.py
RECEIPT_KINDS = frozenset({
    "build",
    "doc",
    "test",
    "review_gate",
    "panel_seat",
    "state_mutation",
    "sub_dispatch",
    "dispatch",
})


def validate_receipt_kind(receipt_kind: Optional[str]) -> str:
    """Emit-time lint (PR-3: warn -> raise). Every emitted receipt MUST carry
    a ``receipt_kind`` from the closed set; a missing or out-of-vocab value
    hard-fails the emit. Returns the validated kind. Raises ValueError.
    """
    if receipt_kind not in RECEIPT_KINDS:
        raise ValueError(
            f"Invalid receipt_kind {receipt_kind!r}. Must be one of "
            f"{sorted(RECEIPT_KINDS)} (receipt-quality §3b closed set; "
            "emit-time lint raises)."
        )
    return receipt_kind

# Size-gates
$ sed -n '21,22p;88,89p' scripts/lib/quality_advisory.py
FILE_SIZE_WARNING_PYTHON = 500
FILE_SIZE_BLOCKING_PYTHON = 1200
FUNCTION_SIZE_WARNING_PYTHON = 40
FUNCTION_SIZE_BLOCKING_PYTHON = 70

# context_rotation.py staat NIET op de size-allowlist
$ grep -n "context_rotation" scripts/lib/quality_advisory.py
(none)

# function_size_blocking is een warning (soft), geen hard block — transparantie
$ sed -n '305,314p' scripts/lib/quality_advisory.py
            if length > FUNCTION_SIZE_BLOCKING_PYTHON:
                checks.append(QualityCheck(
                    check_id="function_size_blocking",
                    severity="warning",
                    file=str(file_path),
                    symbol=node.name,
                    message=f"Function is large: {length} lines (soft max {FUNCTION_SIZE_BLOCKING_PYTHON})",
                    evidence=f"function={node.name},lines={length},max={FUNCTION_SIZE_BLOCKING_PYTHON}",
                    action_required=False,
                ))

# start.sh sessienaam — waarom "^vnx-t0-" nooit matcht op levende T0's
$ sed -n '30p' scripts/commands/start.sh
  local session_name="vnx-$(basename "$PROJECT_ROOT")"

# Levende tmux-sessies (gemeten 2026-08-02) — geen enkele matcht "^vnx-t0-"
$ tmux ls
ConeyBox: 1 windows (created Fri Jul 24 15:28:30 2026) (attached)
legio: 1 windows (created Sun Jul 26 08:42:10 2026)
linkedin-oncue: 1 windows (created Mon Jul 27 13:28:23 2026)
mc-t0: 2 windows (created Fri Jul 24 09:09:23 2026) (attached)
onlineplasticgroup: 1 windows (created Mon Jul 27 11:38:18 2026)
orch-t0: 1 windows (created Fri Jul 24 09:09:23 2026) (attached)
salesminds-bu: 1 windows (created Fri Jul 24 11:49:54 2026)
seo-t0: 1 windows (created Fri Jul 31 11:50:54 2026) (attached)
t0-sales-copilot: 1 windows (created Fri Jul 24 09:09:23 2026)
vnx-D-1ee94ba6: 1 windows (created Fri Jul 24 09:44:13 2026)
vnx-D-4a9fbe0f: 1 windows (created Thu Jul 30 13:15:16 2026)
vnx-D-87cd1e1f: 1 windows (created Wed Jul 29 17:52:07 2026)
vnx-D-ad81c3f1: 1 windows (created Fri Jul 24 11:10:29 2026)
vnx-D-d657bf39: 1 windows (created Sat Aug  1 18:49:47 2026)

De levende T0-sessies zijn orch-t0, mc-t0, seo-t0, t0-sales-copilot (niet "vnx-t0-*").
Wat wél matchte was de vnx-t0-rotation-* successor die respawn() zelf aanmaakt.
Dit ontwerp leunt op géén enkele sessie-naam: de daemon zoekt geen sessies (§6.3).

# Tick-patroon in dispatcher_supervisor_ticks.sh — de sjabloon voor _maybe_rotation_tick
$ sed -n '26,50p' scripts/lib/dispatcher_supervisor_ticks.sh
_maybe_runtime_supervise() {
    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
    local interval="${VNX_RUNTIME_SUPERVISE_INTERVAL:-60}"
    local state_file="$STATE_DIR/.last_runtime_supervise_ts"
    local now last
    now=$(date +%s)
    last=0
    if [[ -f "$state_file" ]]; then
        last=$(cat "$state_file" 2>/dev/null || echo 0)
        [[ "$last" =~ ^[0-9]+$ ]] || last=0
    fi
    if (( now - last < interval )); then
        return 0
    fi
    local log_file="$VNX_LOGS_DIR/runtime_supervise.log"
    mkdir -p "$(dirname "$log_file")"
    python3 "$VNX_DIR/scripts/lib/runtime_supervise.py" >> "$log_file" 2>&1 || true
    printf '%s' "$now" > "$state_file.tmp.$$" && mv -f "$state_file.tmp.$$" "$state_file"
}

# Dispatcher-loop call order
$ sed -n '599,607p' scripts/dispatcher_minimal.sh
process_dispatches() {
    local count=0
    _maybe_runtime_supervise
    _cleanup_stuck_dispatches
    _unified_supervisor_lease_sweep_tick
    _maybe_auto_seed_tracks
    _maybe_oi_bridge_tick
    _maybe_objective_reconcile
    _maybe_learning_cycle

# project_id-regex (path-veilig als padcomponent)
$ grep -n "PROJECT_ID_RE = " scripts/lib/vnx_ids.py
15:PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

# _resolve_state_root branch 4 — project-local collapse (de ADR-007-claim van r2 was onjuist)
$ sed -n '419,421p' scripts/lib/vnx_paths.py
    # 4. Existing dev checkout / pre-migration install — keep project-local dir.
    if local.is_dir():
        return local.resolve()

# De enige rode test in de suite
$ python3 -m pytest tests/test_context_rotation.py -q --tb=short
...F....
FAILED tests/test_context_rotation.py::TestWriteT0Handoff::test_project_id_scoped_across_two_projects
1 failed, 77 passed in 1.73s

# Oude T0 exit zelf (bestaande contract)
$ sed -n '214,217p' docs/operations/CONTEXT_ROTATION.md
   marks the rotation confirmed; the old T0 is expected to exit shortly
   after (outside this module's scope — the actual "exit" instruction is
   part of T0's own boundary logic, not `context_rotation.py`).

# Geen rotatie-state op schijf — de feature is volledig dormant
$ find ~/.vnx-data -name "T0_durable.json" -o -name "T0_request.json" -o -name "T0.ready" 2>/dev/null
(none)
```

---

## Appendix B: Bevindingen-adressering (verplicht — dispatch-instructie)

Per bevinding: in welke sectie hij is geadresseerd. Een lijst, geen proza.

| # | Bevinding | Geadresseerd in |
|---|---|---|
| Op-besluit 1 | Destructieve teardown vervalt (plus: `^vnx-t0-`-zoekterm matcht alleen de eigen `vnx-t0-rotation-*`-successor; levende sessies heten anders) | §0 (tabel), §2.6, §6.3, Appendix A (`tmux ls` + `context_rotation.py:675` + `start.sh:30`) |
| Op-besluit 2 | Twee levende T0's vervalt | §2.6 ("er wordt er maar één gestart, door de bestaande weg"), §6.2 |
| Op-besluit 3 | `mid_action`-poort leeft | §2.4 (poort 2), §1.4 |
| Op-besluit 4 | `respawn()`-wachten blokkeert de dispatcher-loop niet meer | §2.6 (fase 2 is T0's eigen proces), §0 (tabel) |
| Op-besluit 5 | `.ready` als hartslag vervalt als probleem; geen daemon-liveness | §2.6 ("Liveness en `.ready`"), §6.12, §4.2 (freshness als vervanger) |
| Blijft staan | Tick in de dispatcher-loop | §2.1, §2.2 (diagram), PR 1 |
| Blijft staan | Twee-fasen-model | §2.6 (fase 1 = verzoek, fase 2 = uitvoer) |
| Blijft staan | Crash-loop-halt | §3.1 |
| Blijft staan | `fcntl.flock` | §3.2 |
| Blijft staan | Sectie 1 (gemeten huidige toestand) | §1 (behouden uit r2) |
| Open 1 | Gedeelde `boundaries_since_last_rotation`-teller zonder semantiek | §2.3 ("de tick schrijft alleen de marker, `checkpoint()` alleen de teller") + §2.4 (poort 4 bypass) |
| Open 2 | Marker-status / in-flight-guard kent `awaiting_ready` niet | §2.5 (volledige statusovergangen-tabel) + §2.4 (guard leest `rotation_requested` met generatie-guard; naam is niet langer `awaiting_ready`) |
| Open 3 | Receipt-vloer: 720/dag + `health` niet in `RECEIPT_KINDS` + `validate_receipt_kind` gooit | §4.1 (`state_mutation`, bestaande soort; geen set-uitbreiding; volume bounded) + Appendix A (`dispatch_identity.py:67-89`) |
| Open 4 | Size-gate PR2: 898 regels, niet op allowlist, +120/+200 = 1218 | §5 ("Size-gate-verantwoording": splitsing naar `rotation_daemon.py`; context_rotation.py max ~993; functie-opsplitsing) + Appendix A (`quality_advisory.py:21-22,88-89,305-314`) |
| Open 5 | Gefaseerde uitrol; `VNX_SUPERVISOR_MODE=legacy` is geen kill-switch | §3.3 (eigen schakelaar `VNX_T0_ROTATION_DAEMON`, onafhankelijk van supervisor-mode) |
| Open 6 | Drie ontbrekende toetsingscriteria | §5 (task_class + model-routing per PR), §7 (risico-register), §8.3 (ADR-007) |
| Open 7 | ADR-007-onderbouwing leunt op project_id-scoping die de rode test weerspreekt | §2.2 (expliciete project_id-laag in state-paden), §8.3 (letter niet van toepassing, geest wél en gecorrigeerd), PR 2 (deliverable) + Appendix A (`vnx_paths.py:419-421`, `tests/test_context_rotation.py:715`) |

---

## Appendix C: Verificatie-eisen dispatch-instructie

1. **Per bevinding → sectie.** Zie Appendix B.
2. **Per regelnummer → sed/grep-uitvoer.** Zie Appendix A. Elk regelnummer dat in dit plan
   voorkomt is daar terug te vinden met het commando dat het vaststelde.
3. **Sessienamen met uitvoer + ontwerp leunt er niet op.** Zie Appendix A (`tmux ls`):
   levende T0-sessies zijn `orch-t0`, `mc-t0`, `seo-t0`, `t0-sales-copilot`; geen enkele
   matcht `^vnx-t0-`. Wat wél matchte was `vnx-t0-rotation-<term>-<id>`
   (`context_rotation.py:675`), de successor die `respawn()` zelf aanmaakt. Dit ontwerp heeft
   **geen sessie-zoek-logica** (§6.3): de daemon schrijft alleen een marker, zoekt niets, kilt
   niets.
