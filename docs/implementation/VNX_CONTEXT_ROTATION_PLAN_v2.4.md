# VNX Context Rotation System — Implementatieplan v2.4

## Context

Lange Claude Code sessies lijden aan contextrot. Dit plan bouwt een automatisch context rotation systeem. **v2.4** lost de resterende lock-lifecycle edge cases op uit v2.3 review: early-trap padresolutie en nohup-start failure lek.

**Status**: Implementation-ready conditioneel op probe-succes (PR-B stap a+b). Bij probe-failure is een fallback-pad beschreven.
**Publieke repo rollout**: **opt-in / experimental**. Default gedrag moet backward-compatible blijven voor bestaande gebruikers.

**Branch**: `feature/vnx-context-lifecycle-hooks`

---

## Review Fix Matrix (v2 → v2.4)

| # | Finding | Status |
|---|---------|--------|
| 1 | Root settings scope | ✅ Runtime probe met normaal prompt (niet Ctrl+C) |
| 2 | `--json` CLI flag fout | ✅ `--receipt` |
| 3 | Lock PID mismatch na nohup | ✅ Timestamp-based stale (TTL=300s) |
| 4 | tmux target incorrect | ✅ Direct pane ID |
| 5 | T2/T3 recovery ontbreekt | ✅ `--fallback` chain |
| 6 | stop_hook_active risico | ✅ Geverifieerd |
| 7 | T1 bootstrap na rotation | ✅ Bekende beperking |
| 8 | Terminal settings risico | ✅ Runtime probe (#1) |
| 9 | Lock leak bij crash/error | ✅ v2.3: trap in detector+rotate; v2.4: nohup-failure pad gedicht |
| 10 | Handover injectie te groot | ✅ 8KB cap |
| 11 | Hook payload onbewezen | ✅ v2.3: Concrete payload logger in PR-B |
| 12 | `mkdir -p VNX_LOCKS_DIR` regressie | ✅ v2.3: toegevoegd aan `vnx_acquire_lock()` |
| 13 | Detector geen trap | ✅ v2.3: trap na acquire, disabled bij handoff |
| 14 | Rotate early-trap pad fout | ✅ **v2.4**: gebruikt `PROJECT_ROOT` i.p.v. relatief `../../` |
| 15 | "NIET wijzigen" te assertief | ✅ v2.3: conditioneel op probe |
| 16 | Nohup-start failure = lock leak | ✅ **v2.4**: trap pas disabled NA succesvolle nohup+background |
| 17 | Probe expliciet in testterminal | ✅ **v2.4**: probe stappen specificeren T1 als target |

---

## Architectuur

```
STATUSLINE (~/.claude/statusline-command.sh)
  │ schrijft remaining_pct elke render cycle
  ▼
$VNX_STATE_DIR/context_window.json
  │
  │ leest
  ▼
STOP HOOK (vnx_context_monitor.sh)  ← root settings.json
  │ ≥80% used → decision: "block" + rotation instructie
  │ ≥60% used → additionalContext warning
  ▼
Claude schrijft handover doc
  │ naar $VNX_DATA_DIR/rotation_handovers/
  ▼
POSTTOOLUSE HOOK (vnx_handover_detector.sh) ← matcher: "Write"
  │ 1. Detecteert *-ROTATION-HANDOVER.md
  │ 2. mkdir lock ($VNX_LOCKS_DIR/rotation_T1.lock/)  ← ATOMIC
  │ 3. Schrijft receipt via append_receipt.py --receipt  ← FIX #2
  │ 4. Start vnx_rotate.sh (nohup, async)
  ▼
vnx_rotate.sh (tmux automation)
  │ 1. Resolves pane via get_pane_id()
  │ 2. tmux send-keys -t "$PANE_ID"  ← DIRECT, geen prefix ← FIX #4
  │ 3. Escape → /clear → Enter
  │ 4. Poll signal file (max 15s)
  │ 5. load-buffer + paste-buffer + Enter
  │ 6. rmdir lock
  ▼
SESSIONSTART HOOK (vnx_rotation_recovery.sh)
  │ T1/T2/T3 via root PWD-router  ← FIX #5
  │ source == "clear"|"compact":
  │   1. Zoekt recent handover in rotation_handovers/
  │   2. Injecteert als additionalContext
  │   3. Schrijft signal file
  │   4. Reset context_window.json
  │ source != "clear"/"compact":
  │   5. Chain naar --fallback script  ← FIX #5/#7
  ▼
Terminal hervat met handover context + continuation prompt
```

## Public Repo Compatibility (Nieuw, verplicht)

Omdat de repository publiek is, moet context rotation **veilig niets doen** tenzij expliciet geactiveerd.

### Feature Flag (Opt-in)

- **Env flag**: `VNX_CONTEXT_ROTATION_ENABLED=1`
- Default (`unset` of `0`) = context rotation disabled
- Bij disabled:
  - hooks returnen no-op / bestaand gedrag
  - geen tmux automation
  - geen rotation handovers / receipts
  - geen wijziging aan bestaande workflows

### Compatibiliteitseisen

- Backward-compatible defaults voor bestaande VNX gebruikers
- Graceful no-op zonder `tmux` session `vnx`
- Graceful no-op zonder `.vnx-data/` state files
- Geen writes naar `unified_reports/`
- SessionStart routing mag bestaand gedrag niet breken wanneer feature flag disabled is

### Implementatiepatroon (hook-level guard)

Alle nieuwe hooks/scripts krijgen een vroege guard:

```bash
if [[ "${VNX_CONTEXT_ROTATION_ENABLED:-0}" != "1" ]]; then
  # For SessionStart: chain to fallback/original behavior
  # For Stop/PostToolUse/rotator helpers: exit 0 / echo '{}'
  exit 0
fi
```

---

## PR-A: Statusline Sensor + Shared Utilities (~80 regels)

### Nieuw: `.claude/vnx-system/hooks/lib/_vnx_hook_common.sh`

```bash
#!/usr/bin/env bash
# Shared utilities voor VNX hooks

_VNX_HOOK_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_VNX_HOOK_LIB_DIR/../../scripts/lib/vnx_paths.sh"

vnx_detect_terminal() {
  case "$PWD" in
    */terminals/T0|*/T0) echo "T0" ;;
    */terminals/T1|*/T1) echo "T1" ;;
    */terminals/T2|*/T2) echo "T2" ;;
    */terminals/T3|*/T3) echo "T3" ;;
    */terminals/T-MANAGER|*/T-MANAGER) echo "T-MANAGER" ;;
    *) echo "unknown" ;;
  esac
}

vnx_log() {
  echo "[VNX:hook $(date +%H:%M:%S)] $*" >> "$VNX_LOGS_DIR/hook_events.log" 2>/dev/null
}

vnx_json_context() {
  local context="$1"
  local event="${2:-Stop}"
  if command -v jq &>/dev/null; then
    echo "$context" | jq -Rs "{hookSpecificOutput:{hookEventName:\"$event\",additionalContext:.}}"
  else
    local escaped=$(echo "$context" | sed 's/\\/\\\\/g;s/"/\\"/g' | tr '\n' ' ')
    printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"%s"}}' "$event" "$escaped"
  fi
}

# v2.3 FIX: mkdir-based atomic locks with TIMESTAMP stale detection
# Pattern: process_lifecycle.sh:221 (scripts/lib/)
# Ownership: detector creates, rotator inherits, no PID tracking
vnx_acquire_lock() {
  local name="$1"
  local ttl="${2:-300}"  # seconds before stale (default 5 min)
  mkdir -p "$VNX_LOCKS_DIR"  # FIX #12: ensure parent dir exists (regression from v2.1)
  local lock_dir="$VNX_LOCKS_DIR/${name}.lock"

  if mkdir "$lock_dir" 2>/dev/null; then
    date +%s > "$lock_dir/created_at"
    return 0
  fi

  # Stale detection: timestamp-based (NOT PID-based)
  # Avoids ownership transfer problem between detector → nohup rotator
  local ts_file="$lock_dir/created_at"
  if [[ -f "$ts_file" ]]; then
    local created_at
    created_at=$(cat "$ts_file" 2>/dev/null || echo "0")
    local age=$(( $(date +%s) - created_at ))
    if (( age > ttl )); then
      vnx_log "Stale lock removed: $name (age=${age}s > ttl=${ttl}s)"
      rm -rf "$lock_dir"
      if mkdir "$lock_dir" 2>/dev/null; then
        date +%s > "$lock_dir/created_at"
        return 0
      fi
    fi
  else
    # Lock dir exists but no timestamp — treat as stale
    rm -rf "$lock_dir"
    if mkdir "$lock_dir" 2>/dev/null; then
      date +%s > "$lock_dir/created_at"
      return 0
    fi
  fi
  return 1  # Lock held and not stale
}

vnx_release_lock() {
  local name="$1"
  rm -rf "$VNX_LOCKS_DIR/${name}.lock"
}

vnx_context_rotation_enabled() {
  [[ "${VNX_CONTEXT_ROTATION_ENABLED:-0}" == "1" ]]
}
```

### Wijziging: `~/.claude/statusline-command.sh` (na line 10)

```bash
# VNX: Persist context state for hooks (canonical VNX_STATE_DIR)
if [[ -n "$context_remaining" ]]; then
  _vnx_state_dir="${project_dir:-.}/.vnx-data/state"
  [[ -d "$_vnx_state_dir" ]] && \
    printf '{"remaining_pct":%s,"ts":%s}' "$context_remaining" "$(date +%s)" \
      > "$_vnx_state_dir/context_window.json" 2>/dev/null
fi
```

### Verificatie PR-A
```bash
# Test statusline schrijft state
echo '{"model":{"display_name":"Test"},"workspace":{"current_dir":"/tmp","project_dir":"/Users/vincentvandeth/Development/SEOcrawler_v2"},"context_window":{"remaining_percentage":45}}' | bash ~/.claude/statusline-command.sh
cat .vnx-data/state/context_window.json
# Expected: {"remaining_pct":45,"ts":...}

# Test lock atomicity
source .claude/vnx-system/hooks/lib/_vnx_hook_common.sh
vnx_acquire_lock "test_lock" && echo "acquired" || echo "failed"
ls -la "$VNX_LOCKS_DIR/test_lock.lock/"
vnx_release_lock "test_lock"
```

---

## PR-B: Stop Hook (Context Monitor) (~60 regels)

### Nieuw: `.claude/vnx-system/hooks/vnx_context_monitor.sh`

Zelfde als v2 plan — geen wijzigingen nodig. Script is correct:
- `stop_hook_active == true` → exit 0 (loop preventie, geverifieerd)
- T0/unknown/T-MANAGER → exit 0
- ≥80% used → `decision: "block"` met handover instructie
- ≥60% used → warning via additionalContext
- **Nieuw (public repo safety)**: return no-op tenzij `VNX_CONTEXT_ROTATION_ENABLED=1`

### Nieuw: `.claude/vnx-system/hooks/vnx_hook_payload_logger.sh` (PR-B, FIX #11)

Concrete payload logger om hook input/output formaat te documenteren. Wordt als EERSTE stap van PR-B gedeployd, vuur 1x handmatig, verwijder daarna.

```bash
#!/usr/bin/env bash
# Temporary payload logger — deploy, trigger once, inspect, remove.
# Purpose: verify exact JSON structure Claude sends to Stop hooks.
set -euo pipefail

_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_DIR/lib/_vnx_hook_common.sh"

INPUT=$(cat)
LOGFILE="$VNX_LOGS_DIR/hook_payload_probe.log"
mkdir -p "$VNX_LOGS_DIR"

{
  echo "=== STOP HOOK PAYLOAD $(date -u +%Y-%m-%dT%H:%M:%S) ==="
  echo "PWD: $PWD"
  echo "STDIN:"
  echo "$INPUT"
  echo "=== END ==="
} >> "$LOGFILE"

# Always allow — this is a probe, not a blocker
echo '{"decision":"allow"}'
```

**Deploy stap** (uitvoeren in T1 als testterminal):
1. Maak het script executable
2. Voeg tijdelijk toe aan root settings.json als Stop hook
3. Open een Claude sessie in **T1** (`cd .claude/terminals/T1 && claude`)
4. Stuur 1 simpele prompt (bijv. `echo hello`), wacht tot klaar
5. Inspecteer `$VNX_LOGS_DIR/hook_payload_probe.log` vanuit T-MANAGER
6. Verifieer dat `stop_hook_active` field aanwezig is in de payload
7. Verwijder het payload logger script en de tijdelijke Stop hook entry
8. Documenteer het empirische payload formaat in `VNX_HOOK_INTEGRATION_REPORT.md`

**NB**: Payload logger en precedence probe worden gecombineerd — het logger script dient als probe EN als payload documentatie. Dit is de ENIGE probe (T-P60 in het testplan).

### Wijziging: root `.claude/settings.json`

Voeg Stop hook toe (GEEN matcher nodig voor Stop):
```json
"Stop": [
  {
    "hooks": [
      {
        "type": "command",
        "command": "/Users/vincentvandeth/Development/SEOcrawler_v2/.claude/vnx-system/hooks/vnx_context_monitor.sh",
        "timeout": 3000
      }
    ]
  }
]
```

### Verificatie PR-B

Zie testplan Document B, Fase 2 (T-U10..T-U15) en Fase 6 (T-P60).

---

## PR-C: Auto-Rotation (~150 regels)

### Nieuw: `.claude/vnx-system/hooks/vnx_handover_detector.sh` (PostToolUse)

Wijzigingen t.o.v. v2:
- **FIX #2**: `--receipt` i.p.v. `--json`
- **FIX #3**: timestamp-based lock (geen PID ownership probleem)
- **FIX #9+#16** (v2.4): lock release op ALLE failure paden; trap disabled pas NA succesvolle nohup
- **FIX #13** (v2.3): trap na lock-acquire zodat ELKE exit-path (incl. set -e failures) de lock vrijgeeft
- **Public repo safety**: feature-flag guard in detector + rotator + recovery (opt-in only)

```bash
#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/_vnx_hook_common.sh"

vnx_context_rotation_enabled || exit 0

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // ""')

if [[ "$TOOL_NAME" != "Write" ]] || [[ "$FILE_PATH" != *"ROTATION-HANDOVER"* ]]; then
  exit 0
fi

TERMINAL=$(vnx_detect_terminal)
vnx_log "Handover doc detected: $FILE_PATH (terminal: $TERMINAL)"

# Timestamp-based lock — atomic mkdir, stale after 300s (FIX #3)
if ! vnx_acquire_lock "rotation_${TERMINAL}"; then
  vnx_log "Rotation already in progress for $TERMINAL, skipping"
  exit 0
fi

# FIX #13 (v2.3): trap IMMEDIATELY after lock acquire
# Ensures lock cleanup on ANY failure between here and nohup handoff
# The nohup child (vnx_rotate.sh) has its OWN trap for lock release,
# so we disable this trap just before nohup to avoid double-release.
_detector_cleanup() { vnx_release_lock "rotation_${TERMINAL}"; }
trap _detector_cleanup EXIT

REMAINING=$(jq -r '.remaining_pct // 0' "$VNX_STATE_DIR/context_window.json" 2>/dev/null || echo "0")
USED=$((100 - ${REMAINING%.*}))

RECEIPT_JSON=$(cat <<RECEIPT_EOF
{
  "event_type": "context_rotation",
  "event": "context_rotation",
  "terminal": "$TERMINAL",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)",
  "source": "vnx_rotation",
  "handover_path": "$FILE_PATH",
  "context_used_pct": $USED,
  "action_required": false,
  "auto_generated": true
}
RECEIPT_EOF
)

# FIX #2: --receipt (niet --json)
# Testability: optional override for unit tests (default remains canonical script)
APPEND_RECEIPT_SCRIPT="${VNX_APPEND_RECEIPT_SCRIPT:-$(dirname "$0")/../scripts/append_receipt.py}"
python3 "$APPEND_RECEIPT_SCRIPT" --receipt "$RECEIPT_JSON" 2>/dev/null || \
  vnx_log "WARN: Failed to append rotation receipt"

# FIX #9+#16: release lock als rotate script niet beschikbaar of nohup faalt
ROTATE_SCRIPT="${VNX_ROTATE_SCRIPT:-$(dirname "$0")/vnx_rotate.sh}"
if [[ -x "$ROTATE_SCRIPT" ]]; then
  # FIX #16 (v2.4): Keep trap active THROUGH nohup attempt.
  # Only disable after confirming background process started successfully.
  nohup "$ROTATE_SCRIPT" "$TERMINAL" "$FILE_PATH" \
    > "$VNX_LOGS_DIR/vnx_rotate_${TERMINAL}.log" 2>&1 &
  ROTATE_PID=$!
  # Verify the background process actually started
  if kill -0 "$ROTATE_PID" 2>/dev/null; then
    # Handoff successful — rotate script owns the lock now (it has its own trap)
    trap - EXIT
    vnx_log "Rotation script started for $TERMINAL (PID: $ROTATE_PID)"
  else
    # nohup started but process already died — EXIT trap will release lock
    vnx_log "ERROR: vnx_rotate.sh exited immediately (PID: $ROTATE_PID)"
  fi
else
  # EXIT trap will fire and release lock
  vnx_log "ERROR: vnx_rotate.sh not found or not executable"
fi

exit 0
```

### Nieuw: `.claude/vnx-system/hooks/vnx_rotate.sh` (tmux automation)

Wijzigingen t.o.v. v2:
- **FIX #4**: Gebruik `$PANE_ID` direct, GEEN `vnx:` prefix
- **FIX #9**: `trap` cleanup zodat lock altijd released wordt bij crash/early exit
- **FIX #14** (v2.4): trap VOOR source stappen; `_PROJECT_ROOT` berekend uit `SCRIPT_DIR/../../../` voor correct lockpad

```bash
#!/usr/bin/env bash
set -euo pipefail

TERMINAL="${1:?Usage: vnx_rotate.sh TERMINAL /path/to/handover.md}"
HANDOVER_PATH="${2:?}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# FIX #14 (v2.4): trap BEFORE source steps
# Derive PROJECT_ROOT from SCRIPT_DIR to get correct .vnx-data path.
# SCRIPT_DIR = <repo>/.claude/vnx-system/hooks → PROJECT_ROOT = <repo>
# Uses inline rm -rf instead of vnx_release_lock because _vnx_hook_common.sh
# hasn't loaded yet — if source fails, this trap still works.
_PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
_early_cleanup() {
  rm -rf "$_PROJECT_ROOT/.vnx-data/locks/rotation_${TERMINAL}.lock" 2>/dev/null || true
}
trap _early_cleanup EXIT INT TERM

if [[ "${VNX_CONTEXT_ROTATION_ENABLED:-0}" != "1" ]]; then
  exit 0
fi

source "$SCRIPT_DIR/lib/_vnx_hook_common.sh"
source "$SCRIPT_DIR/../scripts/pane_config.sh"

LOG="$VNX_LOGS_DIR/vnx_rotate_${TERMINAL}.log"
SIGNAL_FILE="$VNX_STATE_DIR/rotation_clear_done_${TERMINAL}"

log() { echo "[$(date +%H:%M:%S)] $*" >> "$LOG"; }

# Upgrade trap now that vnx_release_lock and log() are available
cleanup() {
  vnx_release_lock "rotation_${TERMINAL}"
  log "Lock released (cleanup)"
}
trap cleanup EXIT INT TERM

# FIX #4: direct pane ID (matches dispatcher_v8_minimal.sh:94)
PANE_ID=$(get_pane_id "$TERMINAL")
log "Pane resolved: $TERMINAL → $PANE_ID"

if ! tmux has-session -t vnx 2>/dev/null; then
  log "ERROR: No vnx tmux session found"
  exit 1  # trap releases lock
fi

log "Starting context rotation for $TERMINAL"
rm -f "$SIGNAL_FILE"

sleep 3

log "Sending /clear to pane $PANE_ID"
tmux send-keys -t "$PANE_ID" Escape
sleep 1
tmux send-keys -t "$PANE_ID" "/clear" Enter

log "Waiting for /clear completion..."
WAITED=0
while [[ ! -f "$SIGNAL_FILE" ]] && (( WAITED < 15 )); do
  sleep 1
  WAITED=$((WAITED + 1))
done

if [[ ! -f "$SIGNAL_FILE" ]]; then
  log "WARNING: Signal file not created after 15s, proceeding with fallback"
  sleep 5
fi
rm -f "$SIGNAL_FILE"

PROMPT="Context rotation voltooid. Lees het handover document op:
${HANDOVER_PATH}

Ga verder met het resterende werk zoals beschreven in het handover document. Begin met het lezen van dat bestand."

log "Sending continuation prompt"
echo "$PROMPT" | tmux load-buffer -
tmux paste-buffer -t "$PANE_ID"
sleep 0.5
tmux send-keys -t "$PANE_ID" Enter

# Lock released by EXIT trap
log "Rotation complete for $TERMINAL"
```

### Nieuw: `.claude/hooks/vnx_rotation_recovery.sh` (SessionStart)

**FIX #5/#7**: Accepteert `--fallback` argument voor T2/T3 worker bootstrap chain.

```bash
#!/usr/bin/env bash
set -euo pipefail

FALLBACK_SCRIPT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --fallback) FALLBACK_SCRIPT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$_DIR/../vnx-system/hooks/lib/_vnx_hook_common.sh"

INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"' 2>/dev/null)

# Only act on /clear or compact (rotation trigger)
if ! vnx_context_rotation_enabled; then
  if [[ -n "$FALLBACK_SCRIPT" ]] && [[ -x "$FALLBACK_SCRIPT" ]]; then
    echo "$INPUT" | exec "$FALLBACK_SCRIPT"
  else
    echo '{}'
  fi
  exit 0
fi

# Only act on /clear or compact (rotation trigger)
if [[ "$SOURCE" != "clear" ]] && [[ "$SOURCE" != "compact" ]]; then
  # Non-rotation start: chain to fallback (worker bootstrap) or return {}
  if [[ -n "$FALLBACK_SCRIPT" ]] && [[ -x "$FALLBACK_SCRIPT" ]]; then
    echo "$INPUT" | exec "$FALLBACK_SCRIPT"
  else
    echo '{}'
  fi
  exit 0
fi

TERMINAL=$(vnx_detect_terminal)
HANDOVER_DIR="$VNX_DATA_DIR/rotation_handovers"

if [[ ! -d "$HANDOVER_DIR" ]]; then
  if [[ -n "$FALLBACK_SCRIPT" ]] && [[ -x "$FALLBACK_SCRIPT" ]]; then
    echo "$INPUT" | exec "$FALLBACK_SCRIPT"
  else
    echo '{}'
  fi
  exit 0
fi

HANDOVER=$(ls -t "$HANDOVER_DIR"/*"${TERMINAL}-ROTATION-HANDOVER"*.md 2>/dev/null | head -1)

if [[ -z "$HANDOVER" ]]; then
  if [[ -n "$FALLBACK_SCRIPT" ]] && [[ -x "$FALLBACK_SCRIPT" ]]; then
    echo "$INPUT" | exec "$FALLBACK_SCRIPT"
  else
    echo '{}'
  fi
  exit 0
fi

HANDOVER_AGE=$(( $(date +%s) - $(stat -f %m "$HANDOVER" 2>/dev/null || stat -c %Y "$HANDOVER" 2>/dev/null || echo 0) ))

if (( HANDOVER_AGE >= 300 )); then
  if [[ -n "$FALLBACK_SCRIPT" ]] && [[ -x "$FALLBACK_SCRIPT" ]]; then
    echo "$INPUT" | exec "$FALLBACK_SCRIPT"
  else
    echo '{}'
  fi
  exit 0
fi

# Found recent handover — inject as context
# FIX #10: cap at 8KB (~2000 tokens) to avoid recreating context pressure
HANDOVER_CONTENT=$(head -c 8000 "$HANDOVER")
CONTEXT="CONTEXT ROTATION RECOVERY - Handover document van vorige sessie geladen.

--- HANDOVER START ---
${HANDOVER_CONTENT}
--- HANDOVER END ---

Gebruik bovenstaande context om het werk voort te zetten."

touch "$VNX_STATE_DIR/rotation_clear_done_${TERMINAL}"
rm -f "$VNX_STATE_DIR/context_window.json"
vnx_json_context "$CONTEXT" "SessionStart"
```

### Wijziging: root `.claude/settings.json`

**SessionStart router** (line 45) — FIX #5: T1/T2/T3 allemaal via rotation recovery met fallback chain:

```
Huidige routing:
  T0  → sessionstart_t0_minimal.sh
  T1  → DISABLED (echo '{}')
  T2/T3 → sessionstart_worker.sh
  T-MANAGER → sessionstart_tmanager_minimal.sh

Nieuwe routing:
  T0  → sessionstart_t0_minimal.sh (ongewijzigd)
  T1  → vnx_rotation_recovery.sh (geen --fallback; T1 SessionStart was al disabled)
  T2/T3 → vnx_rotation_recovery.sh --fallback sessionstart_worker.sh
  T-MANAGER → sessionstart_tmanager_minimal.sh (ongewijzigd)
```

**PostToolUse** — voeg Write matcher toe (na bestaande disabled entries):
```json
{
  "matcher": "Write",
  "hooks": [
    {
      "type": "command",
      "command": "/Users/vincentvandeth/Development/SEOcrawler_v2/.claude/vnx-system/hooks/vnx_handover_detector.sh",
      "timeout": 5000
    }
  ]
}
```

### Directory aanmaken
```bash
mkdir -p .vnx-data/rotation_handovers
```

---

## PR-D: Documentatie + T0 Receipt Handling (~50 regels code + rapport)

1. **`.claude/vnx-system/docs/intelligence/VNX_HOOK_INTEGRATION_REPORT.md`** — Research rapport
2. **T0 receipt handling**: `context_rotation` event type documenteren als informationeel
3. **Docs updates**: DOCS_INDEX.md, 00_VNX_ARCHITECTURE.md hooks sectie, PROJECT_STATUS.md

---

## Bestanden Overzicht

### Nieuw
| Bestand | PR | Regels | Opmerking |
|---------|-----|--------|-----------|
| `.claude/vnx-system/hooks/lib/_vnx_hook_common.sh` | A | ~70 | +mkdir -p fix |
| `.claude/vnx-system/hooks/vnx_hook_payload_logger.sh` | B | ~20 | Tijdelijk; verwijderen na probe |
| `.claude/vnx-system/hooks/vnx_context_monitor.sh` | B | ~55 | |
| `.claude/vnx-system/hooks/vnx_handover_detector.sh` | C | ~55 | +trap na lock acquire |
| `.claude/vnx-system/hooks/vnx_rotate.sh` | C | ~65 | +early trap voor source |
| `.claude/hooks/vnx_rotation_recovery.sh` | C | ~70 | |
| `.claude/vnx-system/docs/intelligence/VNX_HOOK_INTEGRATION_REPORT.md` | D | rapport | Incl. payload formaat |

### Te wijzigen
| Bestand | PR | Wijziging |
|---------|-----|-----------|
| `~/.claude/statusline-command.sh:10` | A | +6 regels context state persist |
| `.claude/settings.json:45` | B+C | Stop hook + PostToolUse hook + SessionStart T1/T2/T3 routing |
| `.claude/vnx-system/docs/DOCS_INDEX.md` | D | rapport registratie |
| `.claude/vnx-system/docs/core/00_VNX_ARCHITECTURE.md` | D | hooks subsectie |

### Conditioneel (afhankelijk van probe resultaat — FIX #15)
- **Als probe SLAAGT** (root hooks vuren voor T1): Terminal-specifieke `settings.json` NIET wijzigen
- **Als probe FAALT** (root hooks vuren NIET voor T1): Stop + PostToolUse hooks toevoegen aan `T1/settings.json`, `T2/settings.json`, `T3/settings.json`

### Publieke repo rollout (opt-in)

- Hook registraties mogen aanwezig zijn, maar nieuwe scripts moeten no-op zijn zonder `VNX_CONTEXT_ROTATION_ENABLED=1`
- Documenteer de feature als **experimental / opt-in** in CR-D docs
- Default-on activatie pas overwegen na gebruikersfeedback en field validation

### NIET wijzigen (ongeacht probe)
- `unified_reports/` — handovers gaan naar `.vnx-data/rotation_handovers/`
- `append_receipt.py` — accepteert `context_rotation` al zonder `dispatch_id`

---

## Implementatie Volgorde

1. **PR-A** (statusline + utilities) — geen runtime impact
2. **PR-B** (Stop hook) — in 3 sub-stappen:
   a. Deploy payload logger → trigger 1x → inspecteer → documenteer payload formaat → verwijder logger
   b. Runtime precedence probe → bepaalt deployment-locatie (root vs terminal-specifiek)
   c. Deploy vnx_context_monitor.sh op bewezen locatie
3. **PR-C** (auto-rotation) — volledige /clear + resume flow
4. **PR-D** parallel aan PR-C (docs, incl. empirisch payload formaat)

Elke PR is onafhankelijk deploybaar en rollbackbaar.
PR-B sub-stappen (a, b) zijn gates: c hangt af van de resultaten.
Public repo gate: feature-flag/no-op compatibility tests moeten slagen voor merge naar `main`.

---

## Kritieke Integratiepunten

| Component | Pad | Wat hergebruiken |
|-----------|-----|-----------------|
| Path resolver | `scripts/lib/vnx_paths.sh:46` | `VNX_STATE_DIR`, `VNX_DATA_DIR`, `VNX_LOCKS_DIR` |
| Pane resolver | `scripts/pane_config.sh:12` | `get_pane_id()` → direct pane ID |
| Receipt writer | `scripts/append_receipt.py:850` | `--receipt` flag voor JSON payload |
| Receipt validation | `scripts/append_receipt.py:121` | `timestamp` + `event_type` vereist |
| tmux pattern | `scripts/dispatcher_v8_minimal.sh:94` | `tmux send-keys -t "$pane_id"` (direct) |
| Lock pattern | `scripts/lib/process_lifecycle.sh:221` | `mkdir "$lock_dir"` atomisch + timestamp stale |
| SessionStart JSON | `hooks/sessionstart_t0_minimal.sh:21` | `hookSpecificOutput.additionalContext` |
| Root hook routing | `settings.json:45` | PWD-based inline command |
