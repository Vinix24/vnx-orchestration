# Bouwplan: T0 daemon-driven lifecycle (context-rotatie) — herziening r2

> Track: `t0-daemon-driven-lifecycle`
> Dispatch: dispatch-20260802-rotation-lifecycle-plan
> Datum: 2026-08-02
> Status: plan — herzien na plan-gate REVISE (0 pass, 2 revise, 2 block)
> Herziening: r2 — adresseert alle 14 bevindingen uit de gate-run

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

Alles in `context_rotation.py` is passief. Geen enkel code-pad vuurt zonder dat iemand `enabled: true` flipt én `checkpoint()` aanroept. De test-suite (77/78 tests groen; de ene falende test `test_project_id_scoped_across_two_projects` is een pre-existing path-resolutie-issue in een non-central-store omgeving) dekt de volledige module.

### 1.2 Wat bestaat maar niet voor dit doel gebouwd is

**De UNIFIED_SUPERVISOR hooks** (`scripts/lib/dispatcher_supervisor_ticks.sh`, 224 regels) zijn een throttled-tick patroon binnen de dispatcher-loop. Ze gaten op `VNX_SUPERVISOR_MODE=unified`:

```
$ grep -n "VNX_SUPERVISOR_MODE\|_maybe_runtime_supervise\|_unified_supervisor_lease_sweep_tick" scripts/lib/dispatcher_supervisor_ticks.sh
26:_maybe_runtime_supervise() {
27:    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
205:_unified_supervisor_lease_sweep_tick() {
206:    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
```

Deze hooks worden aangeroepen in `process_dispatches()` (`scripts/dispatcher_minimal.sh:599-603`):

```
$ grep -n "process_dispatches\|_maybe_runtime_supervise\|_unified_supervisor_lease_sweep_tick" scripts/dispatcher_minimal.sh
599:process_dispatches() {
601:    _maybe_runtime_supervise
603:    _unified_supervisor_lease_sweep_tick
```

**Het throttled-tick patroon is herbruikbaar voor een rotatie-tick.** De bestaande ticks (runtime_supervise elke 60s, lease_sweep elke 30s) zijn exact het mechanisme waar de rotatie-daemon aan moet haken. Er ontbreekt een `_maybe_rotation_tick`.

**De supervisor wrappers** (`scripts/dispatcher_supervisor.sh`, 194 regels; `scripts/receipt_processor_supervisor.sh`, 207 regels) zijn het template voor een rotatie-daemon supervisor: singleton-enforcement, exponential backoff (2s -> 60s), child-monitoring, en PID/lock-file management.

```
$ grep -n "singleton_enforcer\|enforce_singleton" scripts/dispatcher_supervisor.sh
75:# Singleton enforcement
78:source "$VNX_DIR/scripts/singleton_enforcer.sh"
81:enforce_singleton "$SUPERVISOR_NAME" "$LOG_FILE" "$SCRIPT_DIR/dispatcher_supervisor.sh"
```

**De standaard supervisormodus is `legacy`.** De dispatcher gebruikt `${VNX_SUPERVISOR_MODE:-legacy}` — wie geen `VNX_SUPERVISOR_MODE=unified` exporteert, krijgt géén enkele tick. Dit is relevant voor de "default-aan"-claim in §3.3: die claim hangt aan `unified`, maar `unified` is niet de default.

### 1.3 Wat ontbreekt (gap-analyse)

| Gap | Waarom het ontbreekt |
|---|---|
| **Geen daemon die de rotatie bezit** | `checkpoint()` is een passieve functie — iemand moet 'm aanroepen. In het huidige model is dat een draaiende T0 die zelf beslist. Er is geen externe entiteit die de levenscyclus bewaakt. |
| **Geen tick in de dispatcher-loop voor rotatie** | `dispatcher_supervisor_ticks.sh` heeft ticks voor lease_sweep, runtime_supervise, OI-bridge, objective-reconcile, en learning-cycle. Geen rotatie-tick. |
| **Geen crash-loop-halt** | Als een verse T0 sterft voordat hij z'n `.ready`-signaal schrijft, timeout `respawn()` na 60s en keert terug met `success=False`. Maar er is geen mechanisme dat telt hoe vaak dit gebeurt en stopt met respawnen na N pogingen. |
| **Geen handoff-lease** | `checkpoint()` heeft een request-marker TTL (120s, regel 819-826) die duplicate calls binnen hetzelfde proces voorkomt. Maar er is geen distributed lease die concurrente rotaties van twee losse processen blokkeert. |
| **Geen receipt-per-check** | `checkpoint()` emit alleen een receipt bij een bevestigde rotatie (`context_rotation_continuation`, regel 877-880). Een periodieke check die geen rotatie triggert laat geen spoor achter. Een stille daemon-dood wordt niet gedetecteerd. |
| **Rotatie is default-uit** | `configs/context_rotation.yaml:enabled: false`. Bovendien: `VNX_SUPERVISOR_MODE` default is `legacy`, niet `unified`. |

### 1.4 Grens met PR #1149 (`t0-context-rotation-revival`)

PR #1149 leverde het **mechanisme**: `context_rotation.py` met `checkpoint()` -> `write_t0_handoff()` -> `respawn()`. Het is een passief integratiepunt — een draaiende T0 moet het aanroepen.

Deze track (`t0-daemon-driven-lifecycle`) bouwt de **daemon** die:
1. Periodiek checkt of een T0 aan de rotatie-drempel zit
2. De rotatie triggert (handoff schrijven, verse T0 spawnen)
3. De levenscyclus bewaakt (is de verse T0 nog alive?)
4. Zichzelf beschermt tegen crash-loops en concurrente rotaties
5. Een receipt achterlaat bij elke check

Het mechanisme uit #1149 wordt **gebruikt**, niet herbouwd. De daemon is de **eigenaar** van de levenscyclus — dat is wat #1149 expliciet niet levert (zie `CONTEXT_ROTATION.md:339-341`: "No fully hands-off auto-respawn from a daemon with no T0 present — that needs a governed interactive-session-spawn primitive and is explicitly parked as a follow-up track").

---

## 2. Doelarchitectuur

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
│  │  │  Gated op VNX_SUPERVISOR_MODE=unified│  │  │
│  │  │  + VNX_T0_ROTATION_DAEMON=1          │  │  │
│  │  └──────────────────────────────────────┘  │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

De **dispatcher** bezit alle periodieke taken. De rotatie-tick wordt een zusje van de bestaande ticks — zelfde patroon, zelfde gating, zelfde throttling. Geen aparte daemon, geen extra proces. Dit is de dunst mogelijke toevoeging.

**Twee onafhankelijke schakelaars** (bevinding overige #6 — gefaseerde uitrol):

| Schakelaar | Default | Effect |
|---|---|---|
| `VNX_SUPERVISOR_MODE=unified` | `legacy` | Activeert ALLE ticks (runtime_supervise, lease_sweep, OI-bridge, reconcile, learning-cycle, rotation) |
| `VNX_T0_ROTATION_DAEMON=1` | `0` (uit) | Activeert specifiek de rotatie-tick. Zonder deze flag schrijft de tick alleen health receipts (dry-run), hij roteert nooit. |

`VNX_SUPERVISOR_MODE=legacy` is de enige kill-switch die de dispatcher nu kent. Die zet alles uit — lease_sweep, runtime_supervise, OI-bridge, reconcile. Een rotatie-specifieke rollback vraagt om een rotatie-specifieke schakelaar. Vandaar `VNX_T0_ROTATION_DAEMON`.

**Uitrolpad** (bevinding overige #6):
1. PR 1-2: tick bestaat, maar `VNX_T0_ROTATION_DAEMON` default `0`. Tick schrijft health receipts, roteert niet. Geen productie-impact.
2. PR 3: operator zet `VNX_T0_ROTATION_DAEMON=1` per project. Eerst in vnx-dev, dan vnx-orchestration, dan fleet. Elke stap minimaal 24h observatie.
3. PR 4: docs + integratietests landen. Uitrol is dan al geverifieerd op vnx-dev.

### 2.2 Waar draait de daemon

De rotatie-logica draait **in-process** in `dispatcher_minimal.sh`, als een throttled tick in `process_dispatches()`. Dit is geen aparte daemon — het is een extra functie in de bestaande dispatcher-loop. De dispatcher is al persistent (draait onder `dispatcher_supervisor.sh` met auto-restart). Een extra proces zou een nieuw singleton-mechanisme, een nieuw PID-bestand, en een nieuw supervisor-wrapper nodig hebben — onnodige complexiteit.

### 2.3 Hoe merkt de daemon dat de drempel bereikt is

**Pre-flight check: bestaat er een T0?** (bevinding overige #3 — geen preconditie)

De rotatie-tick checkt eerst of er een T0-sessie bestaat via `tmux list-sessions -F '#{session_name}'` en een naming convention (`vnx-t0-*` of de session name uit de state). Geen T0-sessie -> de tick schrijft een health receipt met `reason: no_t0_session` en returnt. Geen spawn-from-nothing. Dit bewaart de scope-grens uit `CONTEXT_ROTATION.md:339-341`.

**Primaire trigger: sessie-duur** (bevinding B1 — kernmechanisme)

De tick leest `durable_state.json:last_rotation_at`. Daaruit berekent hij `session_duration_seconds = now - last_rotation_at`. Dit is deterministisch — twee integers aftrekken.

**Koude start** (bevinding B1 — `last_rotation_at=None`): de eerste tick die na een cold start draait, zet `last_rotation_at = now` in de durable state. Dit start de klok. De T0 krijgt `max_session_seconds` vanaf dat moment. Als `last_rotation_at` later `None` blijkt (omdat een eerdere schrijfactie faalde), wordt het opnieuw op `now` gezet — de klok herstart, geen crash.

**Ontwerpbeslissing**: sessie-duur is de primaire trigger. Context-percentage is een optionele backstop (alleen als de T0 een betrouwbaar signaal levert). Dit is een puur deterministische check. Geen model-call.

### 2.4 Hoe triggert de daemon de rotatie — het nieuwe triggerpad (bevinding B1)

**Wat fout was in r1**: het plan schreef `checkpoint(at_governance_boundary=False)`. De code op `context_rotation.py:316-317` retourneert `not_at_boundary` bij `False`, en regel 319 weigert elke trigger behalve `governance_boundary`. De daemon kon via dit pad nooit roteren. Het plan erkende bovendien niet dat `True` hardcoderen een daemon zonder liveness-poort impliceert.

**Wat r2 ontwerpt**: een tweede trigger-mode in `decide_rotation()` die **binnen** de bestaande poorten opereert, niet eromheen.

Het bestaande `decide_rotation()` (regel 298-334):

```python
def decide_rotation(
    *,
    policy: RotationPolicy,
    at_governance_boundary: bool,
    boundaries_since_last_rotation: int,
    context_pct: Optional[float] = None,
    mid_action: bool = False,
) -> RotationDecision:
    if not policy.enabled:                                    # poort 1
        return RotationDecision(False, "disabled")
    if mid_action:                                            # poort 2
        return RotationDecision(False, "mid_action")
    if not at_governance_boundary:                            # poort 3
        return RotationDecision(False, "not_at_boundary")
    if policy.trigger != "governance_boundary":               # poort 4
        return RotationDecision(False, f"unsupported_trigger:{policy.trigger}")
    # ... debounce, pct_ceiling ...
```

De uitbreiding voor `session_duration`-mode:

```python
def decide_rotation(
    *,
    policy: RotationPolicy,
    at_governance_boundary: bool,
    boundaries_since_last_rotation: int,
    context_pct: Optional[float] = None,
    mid_action: bool = False,
    session_duration_seconds: Optional[float] = None,         # NIEUW
) -> RotationDecision:
    if not policy.enabled:
        return RotationDecision(False, "disabled")
    if mid_action:
        return RotationDecision(False, "mid_action")

    # --- NIEUW: session_duration triggerpad ---
    if policy.trigger == "session_duration":
        if session_duration_seconds is None:
            return RotationDecision(False, "no_session_duration_signal")
        if session_duration_seconds < policy.max_session_seconds:
            return RotationDecision(False, "session_duration_below_threshold")
        # In duration-mode: de debounce is een cooldown in seconden,
        # niet een boundary-teller. min_boundaries_between_rotations
        # wordt geherinterpreteerd als min_ticks tussen rotaties, en
        # elke tick = tick_interval_seconds. De effectieve cooldown is
        # min_boundaries_between_rotations * tick_interval_seconds.
        return RotationDecision(True, "session_duration_exceeded")

    # --- Bestaand: governance_boundary triggerpad (ongewijzigd) ---
    if not at_governance_boundary:
        return RotationDecision(False, "not_at_boundary")
    if policy.trigger != "governance_boundary":
        return RotationDecision(False, f"unsupported_trigger:{policy.trigger}")
    # ... debounce, pct_ceiling (ongewijzigd) ...
```

**Toelichting bij de poorten**:

- **Poort 1 (`enabled`)** blijft onveranderd. Ook in daemon-mode moet `enabled: true` zijn.
- **Poort 2 (`mid_action`)** blijft onveranderd. De daemon mag nooit mid-action roteren — de tick heeft geen zicht op wat de T0 op dat moment doet. De tick vertrouwt op het feit dat een T0 die mid-action zit, zijn context-percentage niet onbeheerst laat oplopen (native compaction dekt dat). De `session_duration`-trigger vuurt alleen als de T0 al `max_session_seconds` draait — lang genoeg dat een normaal dispatch-/gate-ritme boundaries heeft gekruist waarin een rotatie past.
- **Poort 3 (`at_governance_boundary`)** wordt **overgeslagen** in `session_duration`-mode. De daemon tick is per definitie niet op een governance boundary — hij checkt op wall-clock tijd.
- **Poort 4 (`trigger != "governance_boundary"`)** wordt vervangen door een `if policy.trigger == "session_duration"` tak die vóór de bestaande `governance_boundary`-tak zit. Het `else`-pad (alles wat niet `session_duration` is) valt terug op de bestaande logica.
- **Debounce** in daemon-mode: de `boundaries_since_last_rotation`-teller wordt **niet** gebruikt voor debouncing. De debounce komt van de tick-interval zelf (120s) plus de `min_boundaries_between_rotations` herinterpretatie als cooldown-ticks. Na een rotatie telt de daemon `min_boundaries_between_rotations` ticks af voordat een nieuwe rotatie mag. Dit is dezelfde teller, maar hij wordt opgehoogd bij elke tick (niet alleen bij governance boundaries — zie bevinding overige #1).

**Verificatie dat dit pad op de echte poorten is gebaseerd** (vereist door de dispatch-instructie):

```
$ sed -n '298,334p' scripts/lib/context_rotation.py
def decide_rotation(
    *,
    policy: RotationPolicy,
    at_governance_boundary: bool,
    boundaries_since_last_rotation: int,
    context_pct: Optional[float] = None,
    mid_action: bool = False,
) -> RotationDecision:
    """Pure decision function — no I/O, no side effects.

    Gates, in order: enabled -> never mid-action -> must be at a governance
    boundary -> durable boundary-count debounce (bypassable only by the
    optional pct_ceiling backstop, which still requires being at a boundary).
    """
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
$
```

De herziening voegt één parameter toe (`session_duration_seconds`) en één `if`-tak vóór regel 316. De bestaande `governance_boundary`-logica (regel 316-334) blijft bit-identiek. De `mid_action`-poort (regel 314-315) blijft van toepassing op beide paden. Er wordt geen tweede beslisfunctie naast `decide_rotation()` gebouwd.

De `rotation_tick()`-functie (nieuw, §5-PR1) roept `decide_rotation()` aan met `at_governance_boundary=False, session_duration_seconds=<berekend>`. De functie `checkpoint()` (de T0-integratie) blijft de bestaande `governance_boundary`-trigger gebruiken en is onveranderd.

### 2.5 Hoe de daemon de rotatie uitvoert — twee-fasen model (bevinding B2 + overige #5)

**Wat fout was in r1**: `checkpoint()` -> `respawn()` is synchroon en blokkeert tot 60s. De daemon-tick mag de dispatcher-loop niet zo lang ophouden. Bovendien: na een succesvolle rotatie leven er twee T0's; het plan schoof de kill door naar "het bestaande mechanisme" dat niet bestaat.

**Wat r2 ontwerpt**: een twee-fasen rotatie waarin de daemon niet blokkeert.

**Fase 1 — start (tick N)**:
1. `rotation_tick()` roept `decide_rotation()` met `session_duration_seconds`. Bij `True`: start de rotatie.
2. Schrijft handoff via `write_t0_handoff()`.
3. Registreert de **oude** T0's tmux session name (via `tmux list-sessions` of een bekend pad in durable state).
4. Roept `respawn()` aan — maar zonder te wachten. Dit vereist een nieuwe parameter `wait_for_ready=False` op `respawn()`. Met deze flag: spawn de nieuwe T0, stuur de resume-prompt, maar **poll niet** op `.ready`. Retourneer direct met `success=None` (pending).
5. Schrijft de request marker met `status: awaiting_ready`, `old_session_name`, `new_session_name`.
6. Retourneert. De dispatcher-loop gaat door.

**Fase 2 — voltooi (tick N+1, N+2, ...)**:
1. `rotation_tick()` ziet een request marker met `status: awaiting_ready`.
2. Pollt `.ready` met rotation_id-match (korte timeout: 2s — alleen even kijken, niet wachten).
3. **Ready**: de nieuwe T0 heeft z'n `.ready` geschreven.
   a. Schrijft `tmux send-keys -t <old_session> "Ik draag over aan een verse T0. Tot ziens!" Enter`
   b. Wacht 5s grace period.
   c. `tmux kill-session -t <old_session>` (harde kill als de oude T0 niet zelf exit).
   d. Verifieert dat de nieuwe T0 nog leeft: `tmux has-session -t <new_session>` (exit code 0).
   e. Leeft de nieuwe T0 niet meer (ondanks `.ready`): dit telt als een failed spawn voor crash-loop-detectie (§3.1).
   f. Leeft de nieuwe T0 wel: update durable state (`last_rotation_at = now`, `boundaries_since_last_rotation = 0`), emit `context_rotation_continuation` receipt, markeer request marker `status: success`.
4. **Nog niet ready, binnen timeout**: blijf `awaiting_ready`. De volgende tick probeert opnieuw.
5. **Timeout verstreken** (default: 120s sinds `created_at`): abort. Reap de nieuwe T0 (`tmux kill-session -t <new_session>`), markeer request marker `status: aborted`, increment crash counter (§3.1).

**Waarom `tmux has-session` als liveness-check** (bevinding B2 — stale `.ready`): `tmux has-session` is kernel-beheerd. Als het T0-proces sterft, geeft de kernel de ptty vrij en verdwijnt de tmux-sessie. Geen stale state, geen PID-hergebruik, geen periodieke schrijfactie nodig van de T0. De `.ready`-file blijft zijn rol houden als **eenmalig rotatie-handshake** — het signaal "ik ben opgestart en heb de handoff gelezen". Liveness daarna is de verantwoordelijkheid van de daemon, via `tmux has-session`.

**T0-sessie-identificatie**: de daemon moet weten welke tmux-sessie de T0 is. Twee benaderingen, in volgorde:
1. Uit `durable_state.json` veld `t0_session_name` — geschreven door de daemon zelf na een succesvolle rotatie.
2. Fallback: `tmux list-sessions -F '#{session_name}' | grep '^vnx-t0-'` — naming convention.

Bij een koude start zonder `t0_session_name` in durable state: de eerste tick detecteert de T0-sessie via de naming convention en schrijft `t0_session_name` naar durable state.

### 2.6 Teardown van de oude T0 — expliciet ontwerp (bevinding B2)

De teardown is **deterministisch** en **niet optioneel**. De volgorde is:

1. Nieuwe T0 schrijft `.ready` (handshake voltooid).
2. Daemon stuurt shutdown-commando naar oude T0: `tmux send-keys -t <old_session> -l "Context-rotatie voltooid. De verse T0 heeft de sessie overgenomen."` + Enter.
3. Daemon wacht 5 seconden grace period (de oude T0 mag zelf `exit` aanroepen).
4. Daemon force-killt de oude sessie: `tmux kill-session -t <old_session>`.
5. Daemon verifieert dat de **nieuwe** sessie nog bestaat: `tmux has-session -t <new_session>`.
6. Pas na deze verificatie wordt de rotatie als "succesvol" gemarkeerd.

**Faalmodus — oude T0 al dood**: `tmux kill-session` op een niet-bestaande sessie exit met non-zero. De daemon behandelt dit als succes (de oude T0 is weg, dat was het doel). Alleen het niet kunnen killen van de oude T0 na 3 pogingen is een fout.

**Faalmodus — nieuwe T0 sterft tijdens grace period**: stap 5 vangt dit. De crash-teller (§3.1) wordt opgehoogd. De rotatie is mislukt, de oude T0 is al gekilld (stap 4). Dit is het worst-case scenario: **beide T0's zijn weg**. De impact: de dispatcher merkt bij de volgende tick dat er geen T0-sessie is, schrijft `reason: no_t0_session`, en wacht op handmatige interventie. Dit scenario is bewust niet zelfherstellend: de daemon heeft geen "spawn-from-nothing"-primitief (§6, scope-grens).

Dit scenario treedt alleen op als de nieuwe T0 `.ready` schrijft (dus succesvol opstart, handoff lezen, `vnx handoff mark-ready` aanroepen) en daarna binnen 5 seconden sterft. De kans hierop is laag, maar niet nul. Het wordt expliciet benoemd in het risico-register (§7).

---

## 3. De drie hardheidseisen

### 3.1 Crash-loop-halt (herzien — bevinding B1 + overige #2)

**Wat fout was in r1**: de teller werd opgehoogd op `respawn().success`, wat alleen betekent dat `.ready` binnen 60s verscheen. Een T0 die `.ready` schrijft en daarna sterft, reset de teller. De halt kan structureel niet vuren op het waarschijnlijkste faalpad.

**Wat r2 ontwerpt**: de crash-teller telt op **gecombineerd falen**: ofwel de nieuwe T0 haalt de ready-handshake niet (timeout), ofwel hij haalt 'm wel maar overleeft de teardown van de oude T0 niet (verificatie-stap 5 in §2.6 faalt).

**Durable state uitbreiding**:

```python
# Bestaand: durable_state.json
{
  "boundaries_since_last_rotation": 0,
  "last_rotation_at": "2026-08-02T12:00:00Z"
}

# Nieuw (toe te voegen):
{
  "boundaries_since_last_rotation": 0,
  "last_rotation_at": "2026-08-02T12:00:00Z",
  "t0_session_name": "vnx-t0-orchestrator",       # NIEUW — voor teardown
  "consecutive_spawn_failures": 0,                 # NIEUW
  "crash_loop_halted_until": null                  # NIEUW
}
```

**Regels**:

1. Elke mislukte rotatiepoging — ofwel timeout op `.ready`, ofwel nieuwe T0 sterft tijdens teardown — incrementeert `consecutive_spawn_failures`.
2. Bij `consecutive_spawn_failures >= max_consecutive_failures` (default: 3): zet `crash_loop_halted_until = now + cooldown_seconds` (default: 1800s = 30 min).
3. Zolang `crash_loop_halted_until` in de toekomst ligt: de tick schrijft health receipts met `crash_loop_halted: true` maar doet geen rotatiepogingen.
4. Na cooldown: reset `consecutive_spawn_failures = 0`, `crash_loop_halted_until = null`. Volgende tick probeert opnieuw.
5. Een **volledig succesvolle** rotatie (nieuwe T0 leeft, oude T0 is gekilld, verificatie OK) reset `consecutive_spawn_failures = 0` direct.
6. `max_consecutive_failures` en `cooldown_seconds` zijn configureerbaar via `configs/context_rotation.yaml`, met env-override (`VNX_T0_ROTATION_MAX_FAILURES`, `VNX_T0_ROTATION_COOLDOWN_SECONDS`).

**Implementatie**: de teller-logica zit in `_evaluate_crash_loop()` en `_record_spawn_failure()` in `context_rotation.py`. `rotation_tick()` roept deze aan. De bestaande test-suite wordt uitgebreid met tests voor het gecombineerde faalpad.

### 3.2 Handoff-lease (herzien — bevinding overige #4)

**Wat fout was in r1**: PID-gebaseerde stale-lock-detectie is fragiel (PID-hergebruik).

**Wat r2 ontwerpt**: `fcntl.flock()` — kernel-beheerd, automatisch vrijgegeven bij procesdood, geen PID-hergebruikprobleem.

```python
import fcntl

def acquire_rotation_lease(lock_path: Path, timeout_seconds: float = 5.0) -> bool:
    """Acquire an exclusive fcntl.flock on <lock_path>. Non-blocking
    with a bounded retry loop (max timeout_seconds). Returns True if
    the lock was acquired, False if another process holds it."""
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

**Waarom `fcntl.flock` en niet PID+locks**:
- Kernel-beheerd: bij procesdood (normaal of crash) geeft de kernel de lock vrij. Geen stale-lock cleanup nodig.
- Geen PID in het lock-bestand: geen race tussen PID-check en lock-acquisitie.
- POSIX-standaard: werkt op macOS en Linux.
- Geen DB-afhankelijkheid: zelfde mechanisme als de singleton-enforcer (`scripts/singleton_enforcer.sh` gebruikt `flock`), maar dan in Python.

**Lease lifetime**: de lock wordt gehouden door het Python-proces dat `rotation_tick()` uitvoert. Omdat dit in-process in de dispatcher-loop draait, leeft de lock zolang de dispatcher leeft. Dit is correct: de dispatcher is de enige die rotaties mag starten. De lock beschermt tegen een race waarbij twee dispatcher-instanties (bv. een oude die nog niet gestopt is en een nieuwe na herstart) tegelijk een rotatie starten.

**Lock-bestand**: `<state>/rotation/T0_rotation.lock`. Naast `T0_request.json`. Het bestand zelf is leeg; de lock is op de file descriptor, niet op de inhoud.

### 3.3 Rotatie default-aan (herzien — bevinding overige #6 + overige #7)

**Wat fout was in r1**:
- Claim "default-aan" hing op `VNX_SUPERVISOR_MODE=unified`, maar de default van die variabele is `legacy` (nergens benoemd).
- PR 3 zette `enabled:true` + `respawn:tmux_new_session` fleet-breed in één keer aan, zonder fasering, per-project opt-in, of eigen kill-switch.
- `VNX_SUPERVISOR_MODE=legacy` als kill-switch zet ook lease_sweep, runtime_supervise, OI-bridge, en reconcile uit — onacceptabel breed.

**Wat r2 ontwerpt**: drie onafhankelijke schakelaars met expliciete defaults.

| Niveau | Schakelaar | Default | Scope |
|---|---|---|---|
| 1. Mechanisme beschikbaar | `configs/context_rotation.yaml:enabled` | `false` | `checkpoint()` werkt (T0-geïnitieerd) |
| 2. Tick actief | `VNX_SUPERVISOR_MODE=unified` | `legacy` | Alle supervisor-ticks |
| 3. Tick roteert | `VNX_T0_ROTATION_DAEMON=1` | `0` (uit) | Alleen rotatie-tick |

**Default-gedrag**:
- Geen enkele variabele gezet: rotatie is volledig uit. `checkpoint()` is een no-op (bestaand gedrag). De tick bestaat niet eens (niveau 2 niet gehaald).
- `VNX_SUPERVISOR_MODE=unified` gezet, verder niets: de rotatie-tick draait en schrijft health receipts (§4), maar roteert niet. Dry-run mode.
- `VNX_SUPERVISOR_MODE=unified` + `VNX_T0_ROTATION_DAEMON=1`: de rotatie-tick roteert op basis van sessie-duur.
- `configs/context_rotation.yaml:enabled: true`: `checkpoint()` werkt voor T0-geïnitieerde rotatie (bestaand mechanisme, niet de daemon).

**Wat default-aan NIET betekent**: het betekent niet dat elke installatie automatisch roteert. De operator moet expliciet twee flags zetten. "Default" slaat op de code: de code paden zijn aanwezig en de tick-structuur is ready; de activatie is een configuratiekeuze.

**Config-flip in PR 3**: `configs/context_rotation.yaml` krijgt nieuwe velden (`max_session_seconds`, `max_consecutive_failures`, `cooldown_seconds`). `enabled` en `respawn` blijven op hun huidige defaults (`false` / `off`). De factory-defaults veranderen niet.

---

## 4. Receipt-per-check-vloer (herzien — bevinding B3)

### 4.1 Volume-dimensie

**Tick-interval**: 120 seconden (configureerbaar via `VNX_ROTATION_TICK_INTERVAL`).

**Receipts per dag per project**: `86400 / 120 = 720`.

**Tegen het grootboek** (23.292 receipts op 2026-08-02): 720 receipts/dag = 3% van het totaal per dag. Over 30 dagen: 21.600 receipts — een verdubbeling van het grootboek.

Dit volume is te hoog om ongedifferentieerd in het hoofdgrootboek te landen. De health-check receipt is een **apart receipt_kind** dat:

1. In een **eigen NDJSON-bestand** wordt geschreven: `<data_dir>/receipts/health_rotation.ndjson` — naast `t0_receipts.ndjson`, niet erin.
2. **Niet meetelt** in governance-aggregaties (First-Pass Yield, rework rate, dispatch-tellingen). De `receipt_kind: health` wordt door de read-model-query's uitgefilterd.
3. Een **eigen retentie** heeft: 7 dagen (5.040 receipts per project). De rotatie-tick verwijdert entries ouder dan 7 dagen uit `health_rotation.ndjson` bij elke tick. Dit is een eenvoudige truncatie — het bestand wordt herschreven zonder de oude entries.

**Payload** — adresseert het `unknown:unknown`-patroon (bevinding B3):

```json
{
  "event_type": "rotation_health_check",
  "receipt_kind": "health",
  "dispatch_id": "health_20260802T120000Z",
  "role": "t0-rotation-daemon",
  "terminal": "T0",
  "timestamp": "2026-08-02T12:00:00Z",
  "project_id": "vnx-dev",
  "source": "rotation_tick",
  "fields": {
    "rotated": false,
    "reason": "session_duration_below_threshold",
    "seconds_since_last_rotation": 3600,
    "max_session_seconds": 14400,
    "crash_loop_halted": false,
    "consecutive_spawn_failures": 0,
    "t0_session_alive": true,
    "tick_interval_seconds": 120
  }
}
```

Velden:
- `dispatch_id`: `health_<ISO-timestamp>` — geen echte dispatch, maar wel herleidbaar naar een tijdstip.
- `role`: `t0-rotation-daemon` — de entiteit die de receipt produceert, niet `unknown`.
- `t0_session_alive`: of `tmux has-session` de T0-sessie vindt — het primaire liveness-signaal.
- `reason`: waarom er niet (of wel) geroteerd is — volledige traceability per tick.

### 4.2 Freshness — eigen mechanisme, niet producer_freshness_monitor (bevinding B3)

**Wat fout was in r1**: het plan leunde op `producer_freshness_monitor`, die sinds 31 juli `status=stale` rapporteert en op `cadence_seconds=86400` draait (dagelijks), niet op de 5 minuten die het plan claimde.

**Wat r2 ontwerpt**: de rotatie-tick schrijft een eigen timestamp-bestand: `<state_dir>/.last_rotation_tick_ts`. De dispatcher's eigen `_maybe_runtime_supervise`-tick (60s interval) checkt dit bestand. Als de timestamp ouder is dan `max_tick_interval * 3` (default: 360s), logt `_maybe_runtime_supervise` een waarschuwing op ERROR-niveau.

Dit is **geen nieuwe monitor** — het is een extra check in een bestaande tick die al elke 60s draait. De check is deterministisch: lees timestamp, vergelijk met `now`. Geen model-call, geen aparte poller.

**Fallback-detectie**: als de dispatcher zelf sterft, stopt ook `_maybe_runtime_supervise`. Dan is er geen proces meer dat de waarschuwing kan loggen. Dit is hetzelfde faalpad als nu: een dode dispatcher wordt gedetecteerd door `dispatcher_supervisor.sh` (die herstart 'm). De supervisor wrappers zijn de uiteindelijke vangnetten.

### 4.3 Wanneer wordt er WEL een receipt geschreven?

| Situatie | Receipt? | Welk bestand? |
|---|---|---|
| Elke rotatie-tick (120s) | Ja, health check | `health_rotation.ndjson` |
| Tick skipped (throttled) | Nee | — |
| Succesvolle rotatie | Ja, health check + `context_rotation_continuation` | `health_rotation.ndjson` + `t0_receipts.ndjson` |
| Mislukte rotatie | Ja, health check met `reason: spawn_failed` | `health_rotation.ndjson` |
| Crash-loop-halt actief | Ja, health check met `crash_loop_halted: true` | `health_rotation.ndjson` |
| Geen T0-sessie | Ja, health check met `reason: no_t0_session` | `health_rotation.ndjson` |

De vloer is: zolang de dispatcher-loop draait en `VNX_SUPERVISOR_MODE=unified`, verschijnen er elke 120s health receipts. Zodra de receipts stoppen, is ofwel de dispatcher dood, ofwel `VNX_SUPERVISOR_MODE` teruggezet naar `legacy`. Beide zijn detecteerbaar.

---

## 5. Opsplitsing in deliverables (herzien — bevinding overige #8)

### task_class per deliverable

| PR | task_class | Rationale |
|---|---|---|
| PR 1 | `foundation` | Bouwt de tick-infrastructuur — geen gedragsverandering, alleen meetbaarheid |
| PR 2 | `safety` | Crash-loop-halt + handoff-lease — harde veiligheidsmechanismen |
| PR 3 | `activation` | Config-flip, sessie-duur trigger, freshness — maakt de feature actief |
| PR 4 | `validation` | Integratietests + documentatie — bewijst dat het geheel werkt |

### model-routing-vloer per deliverable

| PR | Model-calls? | Toelichting |
|---|---|---|
| PR 1 | Nee | Bash tick + Python `decide_rotation()`-uitbreiding + receipt-schrijver — allemaal deterministisch |
| PR 2 | Nee | `fcntl.flock()`, teller-logica, durable state — volledig deterministisch |
| PR 3 | Nee | Config-uitlezing, sessie-duur-berekening, timestamp-vergelijking — deterministisch |
| PR 4 | Nee | Integratietests met injectable `tmux_spawn_fn`/`tmux_kill_fn` — deterministische mocks |

**Geen enkele deliverable bevat een model-call.** Alle vier PR's opereren op vaste regels, tellers, timestamps, en kernel-primitieven (`flock`, `tmux has-session`). De enige model-afhankelijkheid is indirect: de daemon spawnt een `claude --model opus`-proces via `respawn()`. Maar de daemon zelf neemt geen model-beslissingen.

ADR-007 (multitenant project_id-stamping) is **niet van toepassing**: deze track voegt geen nieuwe centrale-DB-tabellen toe. Alle state leeft in het bestaande project_id-gescopete filesystem pad (`~/.vnx-data/<project_id>/state/rotation/`), dat al via `vnx_paths._resolve_state_root` resolveert. De health receipts gaan naar `<data_dir>/receipts/health_rotation.ndjson`, wat binnen hetzelfde project_id-gescopete data_dir valt.

---

### PR 1: Rotatie-tick + decide_rotation-uitbreiding (foundation)

**task_class**: `foundation`

**Wat**: Voeg `_maybe_rotation_tick` toe aan `scripts/lib/dispatcher_supervisor_ticks.sh`. Breid `decide_rotation()` uit met de `session_duration`-trigger (zie §2.4). Voeg `rotation_tick()` toe aan `context_rotation.py` als de periodieke entrypoint. De tick schrijft health receipts maar roteert niet (`VNX_T0_ROTATION_DAEMON` default `0`).

**Bestanden**:
- `scripts/lib/context_rotation.py` — `decide_rotation()`: nieuwe parameter `session_duration_seconds`, `session_duration`-triggerpad (zie code-blok in §2.4). Nieuwe functie `rotation_tick()` (~80 regels). Nieuwe functie `emit_rotation_health_receipt()` (~40 regels).
- `scripts/lib/dispatcher_supervisor_ticks.sh` — nieuwe functie `_maybe_rotation_tick()` (~35 regels, zelfde patroon als `_maybe_runtime_supervise`)
- `scripts/dispatcher_minimal.sh:607` — voeg `_maybe_rotation_tick` toe aan `process_dispatches()`, na `_maybe_learning_cycle`
- `configs/context_rotation.yaml` — nieuwe velden: `max_session_seconds: 14400`, `max_consecutive_failures: 3`, `cooldown_seconds: 1800`
- `tests/test_context_rotation.py` — tests voor `decide_rotation()` met `session_duration`-trigger (~100 regels)

**Afhankelijkheden**: Geen. Dit is de eerste steen.

**Verificatie**:
- `decide_rotation()` met `trigger=session_duration, session_duration_seconds=18000` retourneert `should_rotate=True`
- `decide_rotation()` met `trigger=session_duration, session_duration_seconds=3600` retourneert `should_rotate=False`
- `decide_rotation()` met `trigger=session_duration, mid_action=True` retourneert `should_rotate=False` (poort 2 intact)
- `decide_rotation()` met `trigger=governance_boundary` (bestaand pad) — alle bestaande tests blijven groen
- `_maybe_rotation_tick` gate op `VNX_SUPERVISOR_MODE=unified` + `VNX_T0_ROTATION_DAEMON=1`
- `_maybe_rotation_tick` schrijft health receipt bij elke tick (met `VNX_SUPERVISOR_MODE=unified`)
- `_maybe_rotation_tick` throttling: tweede call binnen interval is no-op
- Bestaande tests blijven groen: `python3 -m pytest tests/test_context_rotation.py -q`

**Grootte**: ~310 regels (120 Python + 35 bash + 5 config + 10 dispatcher + ~140 tests)

---

### PR 2: Crash-loop-halt + handoff-lease + twee-fasen rotatie

**task_class**: `safety`

**Wat**: Implementeer de twee-fasen rotatie (§2.5), crash-loop-halt (§3.1), handoff-lease via `fcntl.flock` (§3.2), en de expliciete teardown van de oude T0 (§2.6).

**Bestanden**:
- `scripts/lib/context_rotation.py` — `rotation_tick()` uitgebreid met fase-1/fase-2 logica. Nieuwe functies: `acquire_rotation_lease()`, `release_rotation_lease()`, `_evaluate_crash_loop()`, `_record_spawn_failure()`. Uitbreiding `respawn()` met `wait_for_ready` parameter. Uitbreiding durable state met `t0_session_name`, `consecutive_spawn_failures`, `crash_loop_halted_until`. (~200 regels)
- `tests/test_context_rotation.py` — tests voor twee-fasen rotatie, crash-loop-halt, lease, teardown (~250 regels)

**Afhankelijkheden**: PR 1 (de tick en `rotation_tick()` moeten bestaan)

**Verificatie**:
- Twee-fasen flow: tick N start rotatie (status `awaiting_ready`), tick N+1 voltooit na `.ready`
- Crash-teller incrementeert op timeout (`.ready` niet binnen deadline)
- Crash-teller incrementeert op "nieuwe T0 sterft tijdens teardown"
- Crash-teller reset op volledig succesvolle rotatie
- Halt na 3 failures, cooldown verloopt na 30 minuten
- Lease via `fcntl.flock`: tweede concurrente poging faalt
- Lease wordt automatisch vrijgegeven bij procesdood (kernel)
- Teardown: oude T0-sessie is weg na succesvolle rotatie (geen twee levende T0's)
- `tmux has-session` bevestigt nieuwe T0 leeft na teardown
- `python3 -m pytest tests/test_context_rotation.py -q` — alle tests groen

**Grootte**: ~450 regels (200 Python + 250 tests)

---

### PR 3: Config-flip + sessie-duur trigger + freshness + retentie

**task_class**: `activation`

**Wat**: Activeer de sessie-duur trigger, implementeer freshness-check in `_maybe_runtime_supervise`, implementeer 7-daagse retentie op health receipts, en voeg `VNX_T0_ROTATION_DAEMON` als onafhankelijke kill-switch.

**Bestanden**:
- `scripts/lib/context_rotation.py` — `rotation_tick()` uitgebreid met sessie-duur berekening en koude-start afhandeling. Health receipt retentie-logica (truncate entries > 7 dagen). (~60 regels)
- `scripts/lib/runtime_supervise.py` — check op `.last_rotation_tick_ts` freshness (~15 regels)
- `scripts/lib/dispatcher_supervisor_ticks.sh` — `_maybe_rotation_tick` leest `VNX_T0_ROTATION_DAEMON` env var (~5 regels)
- `tests/test_context_rotation.py` — tests voor sessie-duur, koude start, retentie, freshness (~120 regels)

**Afhankelijkheden**: PR 2 (crash-loop-halt en lease moeten bestaan voordat rotatie actief is)

**Verificatie**:
- Sessie-duur check: rotatie triggert na `max_session_seconds` (default 14400s)
- Koude start: eerste tick zet `last_rotation_at = now`, rotatie vuurt na `max_session_seconds`
- `VNX_T0_ROTATION_DAEMON=0`: tick schrijft health receipts, roteert niet
- `VNX_T0_ROTATION_DAEMON=1`: tick schrijft health receipts én roteert
- Health receipt retentie: entries ouder dan 7 dagen worden verwijderd
- Freshness: `.last_rotation_tick_ts` wordt elke tick bijgewerkt
- Runtime_supervise alarmeert als timestamp > 360s oud is
- `python3 -m pytest tests/test_context_rotation.py -q` — alle tests groen

**Grootte**: ~200 regels (80 Python + 120 tests)

---

### PR 4: End-to-end integratietest + documentatie-update

**task_class**: `validation`

**Wat**: Integratietest die de volledige keten aflegt, plus update van documentatie.

**Bestanden**:
- `tests/test_rotation_daemon_integration.py` — integratietest met injectable `tmux_spawn_fn`/`tmux_kill_fn` (~200 regels)
- `docs/operations/CONTEXT_ROTATION.md` — nieuwe sectie "Daemon-driven mode" (~100 regels)
- `docs/operations/UNIFIED_SUPERVISOR.md` — rotatie-tick toevoegen aan architectuur-diagram en configuratie (~30 regels)

**Afhankelijkheden**: PR 3 (volledige keten moet werken)

**Verificatie**:
- Integratietest: mock-tmux spawn -> mock-ready signaal -> verify receipt -> verify oude T0 gekilld
- Crash-loop-halt integratietest: 3 failed spawns -> halt -> cooldown -> retry
- Lease integratietest: twee concurrente ticks -> één wint
- Documentatie consistent met code (geen verouderde verwijzingen)
- `python3 -m pytest tests/test_rotation_daemon_integration.py tests/test_context_rotation.py -q` — alle tests groen

**Grootte**: ~330 regels (200 tests + 130 docs)

### Afhankelijkheidsgraaf

```
PR 1 (foundation: tick + decide_rotation-uitbreiding + health receipts)
  └── PR 2 (safety: crash-loop-halt + lease + twee-fasen + teardown)
        └── PR 3 (activation: sessie-duur + freshness + retentie + VNX_T0_ROTATION_DAEMON)
              └── PR 4 (validation: integratietests + docs)
```

Elke PR is onafhankelijk deploybaar:
- PR 1: voegt een tick toe die health receipts schrijft, roteert niet. Geen gedragsverandering.
- PR 2: voegt veiligheidsmechanismen toe, maar zonder activatie (flags staan nog uit).
- PR 3: maakt rotatie actief, maar alleen met expliciete `VNX_T0_ROTATION_DAEMON=1`.
- PR 4: voegt tests en docs toe, geen gedragsverandering.

---

## 6. Wat het NIET doet (scope-grenzen)

1. **Geen headless T0.** De daemon respawnt een interactieve T0 via `tmux new-session`. Het goal specificeert expliciet "IN-PLACE (no new-tmux-window + poll-pane + send-keys + kill-old-window choreography)" — maar dit slaat op het elimineren van de choreografie in de **rotate skill** (stap 4, `~/.claude/skills/rotate/SKILL.md:49-73`), niet op het elimineren van tmux zelf. De daemon vervangt de handmatige stappen door een programmatische flow. De verse T0 draait nog steeds in een tmux-sessie.

2. **Geen spawn-from-nothing.** Als er geen T0-sessie bestaat, roteert de daemon niet — hij schrijft een health receipt met `reason: no_t0_session` en wacht. Het starten van een initiële T0 is een handmatige actie van de operator. Dit bewaart de scope-grens uit `CONTEXT_ROTATION.md:339-341`.

3. **Geen automatische `/goal`-hervatting.** De daemon roteert de T0-sessie, maar hervat geen lopende `/goal`-directive. Dat is de taak van de verse T0 via `/kickoff` (die de handoff leest). De daemon is een lifecycle-manager, geen taakplanner.

4. **Geen cross-project rotatie.** De daemon draait per project (één dispatcher per project). De rotatie-tick checkt alleen de T0 van het eigen project. Fleet-wide rotatie-coördinatie valt buiten scope.

5. **Geen vervanging van native compaction.** Zoals `CONTEXT_ROTATION.md:3-6` stelt: "Native Claude Code compaction stays the baseline." De daemon vervangt geen compaction — rotatie is een aanvullende laag.

6. **Geen T1/T2/T3 rotatie.** De worker-rotatie (`hooks/vnx_rotate.sh`) is een apart systeem dat `/clear` gebruikt. Deze track raakt alleen T0.

7. **Geen self-healing van de dispatcher-supervisor zelf.** Als `dispatcher_supervisor.sh` sterft, is handmatige interventie nodig (zelfde als nu). De freshness-check in `runtime_supervise` signaleert dit, maar herstart niet automatisch.

8. **Geen model-downgrade voor de verse T0.** `_SUCCESSOR_MODEL = "opus"` (regel 82) is hard. De verse T0 start altijd met `--model opus`.

9. **Geen directe writes naar `.vnx-data/`.** Alle state wordt geschreven via `context_rotation.py`'s bestaande path helpers en `append_receipt_payload()`. De daemon schrijft nooit rechtstreeks naar het filesystem buiten deze paden.

10. **Geen verwijdering van de menselijke poort.** De daemon roteert op basis van meetbare criteria (sessie-duur). Hij beslist niet inhoudelijk — dat blijft de T0's domein. De operator kan rotatie altijd stoppen door `VNX_T0_ROTATION_DAEMON=0` te zetten.

---

## 7. Risico-register (bevinding overige #8)

| # | Risico | Kans | Impact | Mitigatie | Sectie |
|---|---|---|---|---|---|
| R1 | Nieuwe T0 schrijft `.ready` en sterft binnen 5s grace period -> beide T0's weg | Laag | Hoog | Daemon detecteert `no_t0_session` bij volgende tick; alert via health receipt. Handmatige interventie nodig (geen auto-spawn-from-nothing). | §2.6, §6.2 |
| R2 | Crash-loop-halt te lang (30 min) — operator merkt het niet en T0 draait door met volle context | Laag | Middel | Health receipts met `crash_loop_halted: true` zijn zichtbaar in de receipt-stream. Runtime_supervise logt ERROR als timestamp te oud is. | §3.1, §4.2 |
| R3 | `fcntl.flock` niet beschikbaar op niet-POSIX systemen (Windows) | N.v.t. | N.v.t. | VNX draait alleen op macOS/Linux. Geen mitigatie nodig. | §3.2 |
| R4 | Twee dispatcher-instanties (oud + nieuw na herstart) claimen beide geen lock door race | Zeer laag | Middel | `flock` met `LOCK_NB` is atomisch op POSIX. De kans dat twee processen simultaan `open()` + `flock()` aanroepen zonder dat de kernel er één blokkeert is nul. | §3.2 |
| R5 | `tmux has-session` faalt door tmux-server crash — daemon denkt dat T0 dood is | Zeer laag | Middel | Als de tmux-server crasht, is de T0 ook echt dood (alle sessies weg). Geen vals-positief. | §2.5 |
| R6 | Health receipt NDJSON groeit onbeheerst door retentie-bug | Laag | Laag | Retentie is een eenvoudige timestamp-vergelijking + bestandsherschrijving. Test in PR 3 dekt dit. Max bestandsgrootte bij 7 dagen = ~1MB (720 entries/dag * 7 * ~200 bytes). | §4.1 |
| R7 | Sessie-duur trigger vuurt tijdens een kritieke operatie van de T0 | Middel | Hoog | De `mid_action`-poort in `decide_rotation()` blijft actief. Maar de daemon kent de T0's interne toestand niet. Mitigatie: `max_session_seconds` is bewust ruim (4h) — de T0 heeft meer dan genoeg tijd om elke operatie af te ronden. | §2.4 |
| R8 | `VNX_SUPERVISOR_MODE=unified` actief op een project zonder dispatcher — rotatie-tick draait nooit | N.v.t. | Geen | Zonder dispatcher draait er geen `process_dispatches()` loop en dus geen tick. Dit is correct: geen dispatcher betekent geen daemon. | §2.2 |
| R9 | Health receipt `dispatch_id` formaat (`health_<timestamp>`) botst met echte dispatch IDs | N.v.t. | Geen | Dispatch IDs zijn `YYYYMMDD-HHMMSS-<slug>`. Het prefix `health_` voorkomt elke overlap. | §4.1 |

---

## 8. Toetsingscriteria (bevinding overige #8)

### 8.1 task_class per deliverable

| PR | task_class | Definitie |
|---|---|---|
| PR 1 | `foundation` | Infrastructuur zonder gedragsverandering. Feature is meetbaar maar niet actief. |
| PR 2 | `safety` | Harde veiligheidsmechanismen. Zonder deze mag de feature niet aan. |
| PR 3 | `activation` | Maakt de feature actief onder expliciete operator-controle. |
| PR 4 | `validation` | Bewijst dat het geheel werkt en documenteert het. |

### 8.2 model-routing-vloer

Alle vier PR's zijn volledig deterministisch. Geen enkele PR bevat een model-call. De routing is daarmee: **geen model** — alles draait in-process in Python en bash, op dezelfde machine als de dispatcher.

### 8.3 ADR-007 toepasbaarheid

ADR-007 (multitenant project_id-stamping op alle centrale-DB-tabellen) is **niet van toepassing** op deze track. De track voegt geen nieuwe database-tabellen toe. Alle state is filesystem-gebaseerd binnen het bestaande project_id-gescopete pad (`~/.vnx-data/<project_id>/state/rotation/`), dat al via `vnx_paths._resolve_state_root` resolveert.

---

## Appendix A: Geverifieerde regelnummers

Elk hieronder genoemd regelnummer is geverifieerd met het bijbehorende commando.

```
# Bestandsscan context_rotation.py
$ wc -l scripts/lib/context_rotation.py
899 scripts/lib/context_rotation.py

# Functie-definities
$ grep -n "^def \|^class " scripts/lib/context_rotation.py
220:class RotationPolicy:
293:class RotationDecision:
298:def decide_rotation(
403:def write_t0_handoff(
512:class RespawnResult:
535:class SpawnPartialFailure(RuntimeError):
624:def write_ready_signal(
642:def respawn(
731:class RotationOutcome:
778:def checkpoint(

# Opus-pin
$ grep -n "_SUCCESSOR_MODEL\|--model opus" scripts/lib/context_rotation.py
82:_SUCCESSOR_MODEL = "opus"
597:            ["tmux", "send-keys", "-t", session_name, "-l", f"claude --model {_SUCCESSOR_MODEL}"],

# Default-uit
$ grep -n "enabled: false\|respawn: off" configs/context_rotation.yaml
enabled: false
respawn: off

# Tick-patronen in dispatcher_supervisor_ticks.sh
$ wc -l scripts/lib/dispatcher_supervisor_ticks.sh
224 scripts/lib/dispatcher_supervisor_ticks.sh

# Dispatcher loop call order
$ grep -n "process_dispatches\|_maybe_runtime_supervise\|_unified_supervisor_lease_sweep" scripts/dispatcher_minimal.sh
599:process_dispatches() {
601:    _maybe_runtime_supervise
603:    _unified_supervisor_lease_sweep_tick

# Supervisor wrappers
$ wc -l scripts/dispatcher_supervisor.sh scripts/receipt_processor_supervisor.sh
194 scripts/dispatcher_supervisor.sh
207 scripts/receipt_processor_supervisor.sh

# Singleton enforcement
$ grep -n "singleton_enforcer\|enforce_singleton" scripts/dispatcher_supervisor.sh
75:# Singleton enforcement
78:source "$VNX_DIR/scripts/singleton_enforcer.sh"
81:enforce_singleton "$SUPERVISOR_NAME" "$LOG_FILE" "$SCRIPT_DIR/dispatcher_supervisor.sh"

# Rotate skill
$ wc -l ~/.claude/skills/rotate/SKILL.md
106 /Users/vincentvandeth/.claude/skills/rotate/SKILL.md

# Test suite
$ python3 -m pytest tests/test_context_rotation.py -q --tb=no
77 passed, 1 failed

# Volledige test suite (alleen context_rotation-relevante)
$ wc -l tests/test_context_rotation.py
1127 tests/test_context_rotation.py

# VNX_SUPERVISOR_MODE default
$ grep -n "VNX_SUPERVISOR_MODE.*legacy" scripts/lib/dispatcher_supervisor_ticks.sh
27:    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0
206:    [[ "${VNX_SUPERVISOR_MODE:-legacy}" == "unified" ]] || return 0

# producer_freshness_monitor status
$ cat ~/.vnx-data/vnx-dev/health/producer_freshness_monitor.json
{
  "component": "producer_freshness_monitor",
  "expected_interval_seconds": 86400,
  "last_run_iso": "2026-07-31T15:38:15Z",
  "status": "stale"
}
```

## Appendix B: Bevindingen-adressering (verplicht — dispatch-instructie)

| # | Bevinding | Sectie in herzien plan |
|---|---|---|
| B1 | Kernmechanisme is gegarandeerde no-op (`at_governance_boundary=False`) | §2.4 — nieuw `session_duration`-triggerpad in `decide_rotation()`, met `sed`-bewijs van de echte poorten |
| B2 | Twee levende T0's na rotatie | §2.5 (twee-fasen model), §2.6 (expliciete teardown) |
| B2 | `.ready` eenmalig -> gegarandeerd stale | §2.5 — `tmux has-session` als primair liveness-signaal, `.ready` alleen voor rotatie-handshake |
| B3 | Receipt-volume niet gedimensioneerd (720/dag) | §4.1 — apart bestand, retentie 7 dagen, `receipt_kind: health`, payload met `dispatch_id` + `role` |
| B3 | `producer_freshness_monitor` is stale + verkeerde cadence | §4.2 — eigen timestamp-bestand, gecheckt door `runtime_supervise` |
| overige #1 | Debounce-teller alleen bij `True` opgehoogd | §2.4 — in daemon-mode telt de tick bij elke iteratie, niet alleen bij boundaries |
| overige #2 | Crash-loop-halt telt op `.ready`-verschijning | §3.1 — telt op gecombineerd falen (timeout OF nieuwe T0 sterft tijdens teardown) |
| overige #3 | Geen preconditie dat T0 bestaat | §2.3 — pre-flight check via `tmux list-sessions`, `reason: no_t0_session` |
| overige #4 | PID-gebaseerde stale-lock-detectie fragiel | §3.2 — `fcntl.flock()` kernel-beheerd, geen PID |
| overige #5 | `respawn()` blokkeert dispatcher tot 60s | §2.5 — twee-fasen rotatie, `wait_for_ready=False`, voltooiing in volgende tick |
| overige #6 | PR 3 fleet-breed `enabled:true` zonder fasering | §2.1 — `VNX_T0_ROTATION_DAEMON=1` als onafhankelijke kill-switch, §3.3 uitrolpad |
| overige #7 | "Default-aan"-claim hangt aan `unified`, default is `legacy` | §1.2 (vaststelling default), §3.3 (drie onafhankelijke schakelaars met expliciete defaults) |
| overige #8 | Ontbrekende toetsingscriteria | §5 (task_class + model-routing per PR), §7 (risico-register), §8 (ADR-007) |
