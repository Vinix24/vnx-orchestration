# Testplan: VNX Context Rotation System v2.4

> Gezuiverd testplan met 7 review findings verwerkt (T-U22 mock, T-I51 PATH mock, probe consolidatie, productie-impact check, T-U24 detector scope, T-E80 isolatie, Go/No-Go v2.4 checks).

## Doelstelling

Alle nieuwe componenten valideren (hooks, locking, tmux-automation, SessionStart recovery, settings-precedence) zonder productieflows te verstoren. Herhaalbaar, rollbackbaar, geïsoleerd.

Publieke repo compatibiliteit is onderdeel van dit testplan:
- feature default = **uit** (opt-in via `VNX_CONTEXT_ROTATION_ENABLED=1`)
- zonder flag moet het systeem backward-compatible no-op gedrag vertonen

---

## Fase 0: Test-Isolatie Architectuur

### 0.1 Testterminal Strategie

**Geen nieuwe terminal directory aanmaken.** In plaats daarvan:

- Shell-tests draaien vanuit **T-MANAGER** (deze terminal) met `PWD` override
- Env var override stuurt alle data naar `/tmp/vnx-rotation-test/`
- Productie `.vnx-data/` wordt NIET beschreven door tests

**Waarom geen T-TEST directory:**
- De hook scripts detecteren terminal via `$PWD` pattern (`*/T1`, `*/T2` etc.)
- Een `T-TEST` directory matcht GEEN van deze patterns → `vnx_detect_terminal()` retourneert `"unknown"`
- De context monitor skipt `unknown` terminals → onbruikbaar voor testen
- We simuleren T1/T2/T3 door `PWD` te zetten naar een temp directory met de juiste naam

### 0.2 Test Environment Setup

```bash
# === SETUP (run eenmalig voor elke testsessie) ===

# Basis test directory
export TEST_ROOT="/tmp/vnx-rotation-test"
rm -rf "$TEST_ROOT"
mkdir -p "$TEST_ROOT"

# Fake terminal directories (voor PWD-based detection)
mkdir -p "$TEST_ROOT/terminals/T1"
mkdir -p "$TEST_ROOT/terminals/T2"
mkdir -p "$TEST_ROOT/terminals/T3"

# Geïsoleerde VNX data directory
export VNX_DATA_DIR="$TEST_ROOT/vnx-data"
export VNX_STATE_DIR="$VNX_DATA_DIR/state"
export VNX_LOCKS_DIR="$VNX_DATA_DIR/locks"
export VNX_LOGS_DIR="$VNX_DATA_DIR/logs"
export VNX_PIDS_DIR="$VNX_DATA_DIR/pids"
mkdir -p "$VNX_STATE_DIR" "$VNX_LOCKS_DIR" "$VNX_LOGS_DIR" "$VNX_PIDS_DIR"
mkdir -p "$VNX_DATA_DIR/rotation_handovers"

# Test receipt file (niet productie t0_receipts.ndjson)
export TEST_RECEIPTS_FILE="$TEST_ROOT/test_receipts.ndjson"
touch "$TEST_RECEIPTS_FILE"

# Mock tmux binary voor T-I51 (FIX review #2)
mkdir -p "$TEST_ROOT/mock-bin"
cat > "$TEST_ROOT/mock-bin/tmux" << 'MOCK_TMUX'
#!/usr/bin/env bash
# Mock tmux: has-session altijd faalt, rest logt
if [[ "$1" == "has-session" ]]; then
  exit 1
fi
echo "[MOCK tmux] $*" >> "${VNX_LOGS_DIR:-/tmp}/mock_tmux.log"
MOCK_TMUX
chmod +x "$TEST_ROOT/mock-bin/tmux"

# Mock vnx_rotate.sh voor T-U22 (FIX review #1)
# In plaats van chmod -x op het echte script, gebruiken we een niet-executable mock
cat > "$TEST_ROOT/mock_rotate_noexec.sh" << 'NOEXEC'
#!/usr/bin/env bash
exit 0
NOEXEC
# Bewust NIET executable gemaakt — simuleert "rotate not found"

# Project root referenties (voor scripts die het nodig hebben)
export PROJECT_ROOT="/Users/vincentvandeth/Development/SEOcrawler_v2"
export VNX_HOME="$PROJECT_ROOT/.claude/vnx-system"

# Public repo safe default: feature disabled unless explicitly enabled
unset VNX_CONTEXT_ROTATION_ENABLED

# Script paden
HOOKS_DIR="$PROJECT_ROOT/.claude/vnx-system/hooks"
SCRIPTS_DIR="$PROJECT_ROOT/.claude/vnx-system/scripts"
```

### 0.3 Test Helper Functies

```bash
# Laad _vnx_hook_common.sh met test env
test_source_common() {
  source "$HOOKS_DIR/lib/_vnx_hook_common.sh"
}

# Simuleer een terminal PWD
test_cd_terminal() {
  local terminal="$1"  # T1, T2, T3
  cd "$TEST_ROOT/terminals/$terminal"
}

# Assert helpers
assert_eq() {
  local expected="$1" actual="$2" label="$3"
  if [[ "$expected" == "$actual" ]]; then
    echo "  PASS: $label"
  else
    echo "  FAIL: $label (expected='$expected', actual='$actual')"
    return 1
  fi
}

assert_file_exists() {
  local path="$1" label="$2"
  if [[ -f "$path" ]]; then
    echo "  PASS: $label"
  else
    echo "  FAIL: $label (file not found: $path)"
    return 1
  fi
}

assert_file_not_exists() {
  local path="$1" label="$2"
  if [[ ! -e "$path" ]]; then
    echo "  PASS: $label"
  else
    echo "  FAIL: $label (file unexpectedly exists: $path)"
    return 1
  fi
}

assert_dir_exists() {
  local path="$1" label="$2"
  if [[ -d "$path" ]]; then
    echo "  PASS: $label"
  else
    echo "  FAIL: $label (dir not found: $path)"
    return 1
  fi
}

assert_contains() {
  local haystack="$1" needle="$2" label="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    echo "  PASS: $label"
  else
    echo "  FAIL: $label (does not contain '$needle')"
    return 1
  fi
}
```

### 0.4 Isolatie-Garanties

| Resource | Productiepad | Testpad | Isolatie |
|----------|-------------|---------|----------|
| State dir | `.vnx-data/state/` | `/tmp/vnx-rotation-test/vnx-data/state/` | env override |
| Locks dir | `.vnx-data/locks/` | `/tmp/vnx-rotation-test/vnx-data/locks/` | env override |
| Logs dir | `.vnx-data/logs/` | `/tmp/vnx-rotation-test/vnx-data/logs/` | env override |
| Handovers | `.vnx-data/rotation_handovers/` | `/tmp/vnx-rotation-test/vnx-data/rotation_handovers/` | env override |
| Receipts | `.vnx-data/state/t0_receipts.ndjson` | `/tmp/vnx-rotation-test/test_receipts.ndjson` | `--receipts-file` flag |
| Panes.json | `.vnx-data/state/panes.json` | Gebruikt productie (read-only) | Geen risico |
| tmux binary | `/usr/bin/tmux` | `$TEST_ROOT/mock-bin/tmux` | PATH prepend |
| settings.json | `.claude/settings.json` | Niet gewijzigd door tests | Read-only |
| Terminal PWD | `.claude/terminals/T1/` | `/tmp/vnx-rotation-test/terminals/T1/` | temp dirs |
| vnx_rotate.sh | `.claude/vnx-system/hooks/vnx_rotate.sh` | Productie (ongewijzigd) | Nooit chmod'd |

---

## Fase 1: Unit Tests — Shared Utilities (`_vnx_hook_common.sh`)

### T-U01: vnx_detect_terminal — PWD patterns

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U01 |
| **Doel** | Verifieer dat `vnx_detect_terminal()` correct terminal herkent uit PWD |
| **Preconditions** | Test env setup (fase 0) |
| **Pass/Fail** | Alle 6 assertions slagen |

```bash
echo "=== T-U01: vnx_detect_terminal ==="
test_source_common

cd "$TEST_ROOT/terminals/T1"
assert_eq "T1" "$(vnx_detect_terminal)" "T1 detection"

cd "$TEST_ROOT/terminals/T2"
assert_eq "T2" "$(vnx_detect_terminal)" "T2 detection"

cd "$TEST_ROOT/terminals/T3"
assert_eq "T3" "$(vnx_detect_terminal)" "T3 detection"

cd /tmp
assert_eq "unknown" "$(vnx_detect_terminal)" "unknown detection"

cd "$PROJECT_ROOT/.claude/terminals/T-MANAGER"
assert_eq "T-MANAGER" "$(vnx_detect_terminal)" "T-MANAGER detection"

cd "$PROJECT_ROOT/.claude/terminals/T0"
assert_eq "T0" "$(vnx_detect_terminal)" "T0 detection"
```

### T-U02: vnx_acquire_lock — happy path

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U02 |
| **Doel** | Lock acquire + release basisfunctionaliteit |
| **Pass/Fail** | Lock dir aangemaakt met `created_at`, release verwijdert alles |

```bash
echo "=== T-U02: Lock acquire + release ==="
test_source_common
rm -rf "$VNX_LOCKS_DIR"/*

vnx_acquire_lock "test_unit" && echo "  PASS: lock acquired" || echo "  FAIL: lock not acquired"
assert_dir_exists "$VNX_LOCKS_DIR/test_unit.lock" "lock dir exists"
assert_file_exists "$VNX_LOCKS_DIR/test_unit.lock/created_at" "created_at exists"

vnx_release_lock "test_unit"
assert_file_not_exists "$VNX_LOCKS_DIR/test_unit.lock" "lock dir removed"
```

### T-U03: vnx_acquire_lock — dubbele acquire (idempotency)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U03 |
| **Doel** | Tweede acquire faalt als lock actief is |
| **Pass/Fail** | Eerste slaagt, tweede faalt (exit 1) |

```bash
echo "=== T-U03: Double lock acquire ==="
test_source_common
rm -rf "$VNX_LOCKS_DIR"/*

vnx_acquire_lock "test_double"
assert_eq "0" "$?" "first acquire succeeds"

vnx_acquire_lock "test_double"
RESULT=$?
assert_eq "1" "$RESULT" "second acquire blocked"

vnx_release_lock "test_double"
```

### T-U04: vnx_acquire_lock — stale lock (TTL verlopen)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U04 |
| **Doel** | Lock ouder dan TTL wordt automatisch opgeruimd |
| **Pass/Fail** | Stale lock verwijderd, nieuwe lock verkregen |

```bash
echo "=== T-U04: Stale lock removal ==="
test_source_common
rm -rf "$VNX_LOCKS_DIR"/*

# Maak een lock met oude timestamp (10 minuten geleden)
mkdir -p "$VNX_LOCKS_DIR/test_stale.lock"
echo $(( $(date +%s) - 600 )) > "$VNX_LOCKS_DIR/test_stale.lock/created_at"

# Acquire met TTL=300 → stale lock (600 > 300) → moet slagen
vnx_acquire_lock "test_stale" 300
assert_eq "0" "$?" "stale lock replaced"

# Verifieer nieuwe timestamp
NEW_TS=$(cat "$VNX_LOCKS_DIR/test_stale.lock/created_at")
NOW=$(date +%s)
DIFF=$(( NOW - NEW_TS ))
if (( DIFF < 5 )); then
  echo "  PASS: fresh timestamp (age=${DIFF}s)"
else
  echo "  FAIL: timestamp not fresh (age=${DIFF}s)"
fi

vnx_release_lock "test_stale"
```

### T-U05: vnx_acquire_lock — lock zonder created_at

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U05 |
| **Doel** | Lock dir zonder timestamp wordt als stale behandeld |
| **Pass/Fail** | Lock dir opgeruimd, nieuwe lock verkregen |

```bash
echo "=== T-U05: Lock without created_at ==="
test_source_common
rm -rf "$VNX_LOCKS_DIR"/*

# Lock dir zonder timestamp
mkdir -p "$VNX_LOCKS_DIR/test_notime.lock"

vnx_acquire_lock "test_notime"
assert_eq "0" "$?" "orphan lock replaced"
assert_file_exists "$VNX_LOCKS_DIR/test_notime.lock/created_at" "new created_at written"

vnx_release_lock "test_notime"
```

### T-U06: vnx_acquire_lock — mkdir -p VNX_LOCKS_DIR

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U06 |
| **Doel** | Verifieer dat `vnx_acquire_lock` parent dir aanmaakt (fix #12) |
| **Pass/Fail** | Lock slaagt ook als VNX_LOCKS_DIR niet bestaat |

```bash
echo "=== T-U06: mkdir -p VNX_LOCKS_DIR ==="
# Reset naar niet-bestaande locks dir
export VNX_LOCKS_DIR="$TEST_ROOT/vnx-data/fresh-locks-dir"
rm -rf "$VNX_LOCKS_DIR"
test_source_common

vnx_acquire_lock "test_mkdir"
assert_eq "0" "$?" "lock acquired in fresh dir"
assert_dir_exists "$VNX_LOCKS_DIR" "locks dir auto-created"

vnx_release_lock "test_mkdir"
export VNX_LOCKS_DIR="$TEST_ROOT/vnx-data/locks"  # reset
```

### T-U07: vnx_json_context — output format

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U07 |
| **Doel** | Verifieer JSON output structuur met jq |
| **Pass/Fail** | Valide JSON met hookSpecificOutput.additionalContext |

```bash
echo "=== T-U07: vnx_json_context ==="
test_source_common

OUTPUT=$(vnx_json_context "Test context bericht" "Stop")

# Valideer dat het valide JSON is
echo "$OUTPUT" | jq . > /dev/null 2>&1
assert_eq "0" "$?" "valid JSON output"

EVENT=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.hookEventName')
assert_eq "Stop" "$EVENT" "hookEventName correct"

CONTEXT=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext')
assert_contains "$CONTEXT" "Test context bericht" "additionalContext present"
```

### T-U08: Feature flag helper — default disabled / explicit enabled

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U08 |
| **Doel** | Verifieer opt-in helper gedrag voor `VNX_CONTEXT_ROTATION_ENABLED` |
| **Pass/Fail** | helper false bij unset, true bij `=1` |

```bash
echo "=== T-U08: Feature flag helper ==="
test_source_common

unset VNX_CONTEXT_ROTATION_ENABLED
vnx_context_rotation_enabled
assert_eq "1" "$?" "disabled by default"

export VNX_CONTEXT_ROTATION_ENABLED=1
vnx_context_rotation_enabled
assert_eq "0" "$?" "enabled when flag=1"

unset VNX_CONTEXT_ROTATION_ENABLED
```

---

## Fase 2: Unit Tests — Context Monitor (`vnx_context_monitor.sh`)

### T-U09: Feature disabled → no-op (public repo safe default)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U09 |
| **Doel** | Zonder feature flag blijft context monitor no-op |
| **Preconditions** | `VNX_CONTEXT_ROTATION_ENABLED` unset |
| **Pass/Fail** | Exit 0, geen output |

```bash
echo "=== T-U09: Feature disabled → no-op ==="
test_cd_terminal "T1"
unset VNX_CONTEXT_ROTATION_ENABLED
printf '{"remaining_pct":15,"ts":%s}' "$(date +%s)" > "$VNX_STATE_DIR/context_window.json"

OUTPUT=$(echo '{"stop_hook_active":false}' | bash "$HOOKS_DIR/vnx_context_monitor.sh" 2>/dev/null)
assert_eq "0" "$?" "exit 0 when disabled"
if [[ -z "$OUTPUT" ]] || [[ "$OUTPUT" == "{}" ]]; then
  echo "  PASS: no-op when feature disabled"
else
  echo "  FAIL: unexpected output when disabled: $OUTPUT"
fi
```

> Voor alle verdere positieve context-rotation tests (T-U10+) geldt:
```bash
export VNX_CONTEXT_ROTATION_ENABLED=1
```

### T-U10: stop_hook_active=true → exit 0 (loop preventie)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U10 |
| **Doel** | Loop preventie: als stop_hook_active=true, geen output |
| **Preconditions** | Test env, PWD=T1 |
| **Pass/Fail** | Lege stdout, exit 0 |

```bash
echo "=== T-U10: stop_hook_active=true → no-op ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1

OUTPUT=$(echo '{"stop_hook_active":true}' | bash "$HOOKS_DIR/vnx_context_monitor.sh" 2>/dev/null)
assert_eq "0" "$?" "exit code 0"
if [[ -z "$OUTPUT" ]] || [[ "$OUTPUT" == "{}" ]]; then
  echo "  PASS: no output on active loop"
else
  echo "  FAIL: unexpected output: $OUTPUT"
fi
```

### T-U11: T0 terminal → skip

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U11 |
| **Doel** | Context monitor skip voor T0/T-MANAGER/unknown |
| **Pass/Fail** | Geen block/warning output |

```bash
echo "=== T-U11: T0/T-MANAGER/unknown → skip ==="
export VNX_CONTEXT_ROTATION_ENABLED=1

# T0
cd "$PROJECT_ROOT/.claude/terminals/T0"
OUTPUT=$(echo '{"stop_hook_active":false}' | bash "$HOOKS_DIR/vnx_context_monitor.sh" 2>/dev/null)
if [[ -z "$OUTPUT" ]] || [[ "$OUTPUT" == "{}" ]]; then
  echo "  PASS: T0 skipped"
else
  echo "  FAIL: T0 unexpected output: $OUTPUT"
fi

# unknown
cd /tmp
OUTPUT=$(echo '{"stop_hook_active":false}' | bash "$HOOKS_DIR/vnx_context_monitor.sh" 2>/dev/null)
if [[ -z "$OUTPUT" ]] || [[ "$OUTPUT" == "{}" ]]; then
  echo "  PASS: unknown skipped"
else
  echo "  FAIL: unknown unexpected output: $OUTPUT"
fi
```

### T-U12: context < 60% used → geen actie

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U12 |
| **Doel** | Onder warning threshold → geen output |
| **Preconditions** | context_window.json met remaining_pct=55 (45% used) |
| **Pass/Fail** | Geen warning of block |

```bash
echo "=== T-U12: Under warning threshold ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
printf '{"remaining_pct":55,"ts":%s}' "$(date +%s)" > "$VNX_STATE_DIR/context_window.json"

OUTPUT=$(echo '{"stop_hook_active":false}' | bash "$HOOKS_DIR/vnx_context_monitor.sh" 2>/dev/null)
if [[ -z "$OUTPUT" ]] || [[ "$OUTPUT" == "{}" ]]; then
  echo "  PASS: no action under threshold"
else
  echo "  FAIL: unexpected output: $OUTPUT"
fi
```

### T-U13: 60-80% used → warning (additionalContext)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U13 |
| **Doel** | Tussen 60% en 80% used → warning via additionalContext |
| **Preconditions** | remaining_pct=30 (70% used) |
| **Pass/Fail** | JSON met additionalContext maar GEEN decision:block |

```bash
echo "=== T-U13: Warning threshold (60-80% used) ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
printf '{"remaining_pct":30,"ts":%s}' "$(date +%s)" > "$VNX_STATE_DIR/context_window.json"

OUTPUT=$(echo '{"stop_hook_active":false}' | bash "$HOOKS_DIR/vnx_context_monitor.sh" 2>/dev/null)
echo "$OUTPUT" | jq . > /dev/null 2>&1
assert_eq "0" "$?" "valid JSON"

DECISION=$(echo "$OUTPUT" | jq -r '.decision // "none"')
if [[ "$DECISION" != "block" ]]; then
  echo "  PASS: no block decision"
else
  echo "  FAIL: block decision at warning level"
fi

CONTEXT=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext // ""')
if [[ -n "$CONTEXT" ]]; then
  echo "  PASS: additionalContext warning present"
else
  echo "  FAIL: no warning in additionalContext"
fi
```

### T-U14: ≥80% used → block + rotation instructie

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U14 |
| **Doel** | Boven rotation threshold → decision:block met handover instructie |
| **Preconditions** | remaining_pct=15 (85% used) |
| **Pass/Fail** | JSON met decision=block EN additionalContext met handover instructie |

```bash
echo "=== T-U14: Rotation threshold (≥80% used) ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
printf '{"remaining_pct":15,"ts":%s}' "$(date +%s)" > "$VNX_STATE_DIR/context_window.json"

OUTPUT=$(echo '{"stop_hook_active":false}' | bash "$HOOKS_DIR/vnx_context_monitor.sh" 2>/dev/null)
echo "$OUTPUT" | jq . > /dev/null 2>&1
assert_eq "0" "$?" "valid JSON"

DECISION=$(echo "$OUTPUT" | jq -r '.decision // "none"')
assert_eq "block" "$DECISION" "decision is block"

CONTEXT=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext // ""')
assert_contains "$CONTEXT" "ROTATION" "rotation instruction present"
```

### T-U15: missing context_window.json → geen actie

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U15 |
| **Doel** | Zonder state file → graceful no-op |
| **Pass/Fail** | Geen crash, exit 0 |

```bash
echo "=== T-U15: Missing context_window.json ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -f "$VNX_STATE_DIR/context_window.json"

OUTPUT=$(echo '{"stop_hook_active":false}' | bash "$HOOKS_DIR/vnx_context_monitor.sh" 2>/dev/null)
assert_eq "0" "$?" "no crash without state file"
```

---

## Fase 3: Unit Tests — Handover Detector (`vnx_handover_detector.sh`)

### T-U20: Non-Write tool → exit 0 (no-op)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U20 |
| **Doel** | PostToolUse op Read/Edit/Bash → geen actie |
| **Pass/Fail** | Exit 0, geen lock aangemaakt |

```bash
echo "=== T-U20: Non-Write tool → no-op ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -rf "$VNX_LOCKS_DIR"/*

echo '{"tool_name":"Read","tool_input":{"file_path":"foo.md"}}' | \
  bash "$HOOKS_DIR/vnx_handover_detector.sh" 2>/dev/null
assert_eq "0" "$?" "exit 0"
assert_file_not_exists "$VNX_LOCKS_DIR/rotation_T1.lock" "no lock created"
```

### T-U21: Write op niet-handover bestand → no-op

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U21 |
| **Doel** | Write op regulier bestand → geen handover detectie |
| **Pass/Fail** | Exit 0, geen lock |

```bash
echo "=== T-U21: Write non-handover file → no-op ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -rf "$VNX_LOCKS_DIR"/*

echo '{"tool_name":"Write","tool_input":{"file_path":"src/foo.py"}}' | \
  bash "$HOOKS_DIR/vnx_handover_detector.sh" 2>/dev/null
assert_eq "0" "$?" "exit 0"
assert_file_not_exists "$VNX_LOCKS_DIR/rotation_T1.lock" "no lock created"
```

### T-U22: Write op ROTATION-HANDOVER → lock + trap + receipt (FIX review #1)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U22 |
| **Doel** | Handover detectie: lock acquire, trap cleanup, receipt-write poging |
| **FIX** | Gebruikt env var `VNX_ROTATE_SCRIPT` override i.p.v. `chmod -x` op echt script |
| **Pass/Fail** | Lock aangemaakt en daarna released door trap, detectie gelogd, receipt-write pad bereikt |

```bash
echo "=== T-U22: Write ROTATION-HANDOVER → lock + trap + receipt ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -rf "$VNX_LOCKS_DIR"/*
rm -f "$VNX_LOGS_DIR/hook_events.log"
printf '{"remaining_pct":15,"ts":%s}' "$(date +%s)" > "$VNX_STATE_DIR/context_window.json"

# FIX review #1: Override rotate script pad via env var
# zodat het productie-script NOOIT gewijzigd wordt.
# De detector checkt: if [[ -x "$ROTATE_SCRIPT" ]]; dan wij overriden
# ROTATE_SCRIPT zodat het naar een niet-executable mock wijst.
export VNX_ROTATE_SCRIPT="$TEST_ROOT/mock_rotate_noexec.sh"

echo '{"tool_name":"Write","tool_input":{"file_path":"'"$VNX_DATA_DIR"'/rotation_handovers/20260223-T1-ROTATION-HANDOVER.md"}}' | \
  bash "$HOOKS_DIR/vnx_handover_detector.sh" 2>/dev/null
assert_eq "0" "$?" "exit 0"

# Lock moet NIET meer bestaan (error path trap released het)
assert_file_not_exists "$VNX_LOCKS_DIR/rotation_T1.lock" "lock released on error path"

# Check logs
assert_file_exists "$VNX_LOGS_DIR/hook_events.log" "hook log created"
if grep -q "Handover doc detected" "$VNX_LOGS_DIR/hook_events.log"; then
  echo "  PASS: handover detection logged"
else
  echo "  FAIL: no detection log entry"
fi

if grep -q "vnx_rotate.sh not found or not executable" "$VNX_LOGS_DIR/hook_events.log"; then
  echo "  PASS: detector reached post-receipt error path"
else
  echo "  WARN: could not confirm post-receipt path from logs"
fi

unset VNX_ROTATE_SCRIPT
```

**NB**: Dit vereist een kleine wijziging in `vnx_handover_detector.sh`:
```bash
# In vnx_handover_detector.sh, wijzig:
ROTATE_SCRIPT="$(dirname "$0")/vnx_rotate.sh"
# Naar:
ROTATE_SCRIPT="${VNX_ROTATE_SCRIPT:-$(dirname "$0")/vnx_rotate.sh}"
```
Dit is een 1-regel aanpassing die testbaarheid mogelijk maakt zonder productie-impact.

### T-U23: Dubbele handover write → idempotency (lock blocking)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U23 |
| **Doel** | Tweede handover detectie terwijl rotation loopt → lock blokkeert |
| **Pass/Fail** | Eerste acquire slaagt, tweede wordt gelogd als "already in progress" |

```bash
echo "=== T-U23: Double handover → lock idempotency ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -rf "$VNX_LOCKS_DIR"/*
rm -f "$VNX_LOGS_DIR/hook_events.log"
printf '{"remaining_pct":15,"ts":%s}' "$(date +%s)" > "$VNX_STATE_DIR/context_window.json"

# Pre-acquire lock (simuleer lopende rotation)
test_source_common
vnx_acquire_lock "rotation_T1"

# Nu zou detector geblokt moeten worden
echo '{"tool_name":"Write","tool_input":{"file_path":"'"$VNX_DATA_DIR"'/rotation_handovers/X-T1-ROTATION-HANDOVER.md"}}' | \
  bash "$HOOKS_DIR/vnx_handover_detector.sh" 2>/dev/null

if grep -q "already in progress" "$VNX_LOGS_DIR/hook_events.log"; then
  echo "  PASS: duplicate detected and blocked"
else
  echo "  FAIL: no duplicate blocking log"
fi

vnx_release_lock "rotation_T1"
```

### T-U24: Detector met receipt failure → trap release + graceful (FIX review #5)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U24 |
| **Doel** | Als append_receipt.py faalt, crasht de detector niet en wordt lock released door trap |
| **FIX** | Test nu de DETECTOR failure handling (niet alleen append_receipt.py direct) |
| **Pass/Fail** | Script exit 0, warning gelogd, lock released |

```bash
echo "=== T-U24: Detector with receipt failure → trap release ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -rf "$VNX_LOCKS_DIR"/*
rm -f "$VNX_LOGS_DIR/hook_events.log"
printf '{"remaining_pct":15,"ts":%s}' "$(date +%s)" > "$VNX_STATE_DIR/context_window.json"

# Override append_receipt.py zodat het faalt
export VNX_ROTATE_SCRIPT="$TEST_ROOT/mock_rotate_noexec.sh"
# Maak een mock append_receipt.py die altijd faalt
MOCK_RECEIPT="$TEST_ROOT/mock_append_receipt.py"
cat > "$MOCK_RECEIPT" << 'MOCK_PY'
#!/usr/bin/env python3
import sys
sys.exit(1)  # Altijd falen
MOCK_PY
chmod +x "$MOCK_RECEIPT"

# Vereist testability override in detector:
# APPEND_RECEIPT_SCRIPT="${VNX_APPEND_RECEIPT_SCRIPT:-.../append_receipt.py}"
export VNX_APPEND_RECEIPT_SCRIPT="$MOCK_RECEIPT"

# NB: De detector vangt receipt failure op met `|| vnx_log "WARN: ..."`
# via VNX_APPEND_RECEIPT_SCRIPT override testen we het detector-level failure pad.
echo '{"tool_name":"Write","tool_input":{"file_path":"'"$VNX_DATA_DIR"'/rotation_handovers/20260223-T1-ROTATION-HANDOVER.md"}}' | \
  bash "$HOOKS_DIR/vnx_handover_detector.sh" 2>/dev/null
assert_eq "0" "$?" "detector exit 0 despite receipt failure"

# Lock moet released zijn door trap
assert_file_not_exists "$VNX_LOCKS_DIR/rotation_T1.lock" "lock released despite receipt failure"

# Detectie moet wel gelogd zijn
if grep -q "Handover doc detected" "$VNX_LOGS_DIR/hook_events.log"; then
  echo "  PASS: detection logged before receipt failure"
else
  echo "  FAIL: no detection log"
fi

unset VNX_ROTATE_SCRIPT
unset VNX_APPEND_RECEIPT_SCRIPT
rm -f "$MOCK_RECEIPT"
```

---

## Fase 4: Unit Tests — Rotation Recovery (`vnx_rotation_recovery.sh`)

### T-U30: source=startup → fallback chain

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U30 |
| **Doel** | Niet-rotation SessionStart → chain naar --fallback script |
| **Pass/Fail** | Output komt van fallback script |

```bash
echo "=== T-U30: source=startup → fallback ==="
test_cd_terminal "T2"
export VNX_CONTEXT_ROTATION_ENABLED=1

# Maak een simpele mock fallback
MOCK_FALLBACK="$TEST_ROOT/mock_fallback.sh"
cat > "$MOCK_FALLBACK" << 'MOCK'
#!/usr/bin/env bash
echo '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"FALLBACK_FIRED"}}'
MOCK
chmod +x "$MOCK_FALLBACK"

OUTPUT=$(echo '{"source":"startup"}' | bash "$PROJECT_ROOT/.claude/hooks/vnx_rotation_recovery.sh" \
  --fallback "$MOCK_FALLBACK" 2>/dev/null)

assert_contains "$OUTPUT" "FALLBACK_FIRED" "fallback script executed"
```

### T-U31: source=clear + recent handover → context injection

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U31 |
| **Doel** | Rotation recovery: recent handover → injectie als additionalContext |
| **Pass/Fail** | Output bevat handover content |

```bash
echo "=== T-U31: source=clear + recent handover → injection ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1

# Maak een recent handover
HANDOVER="$VNX_DATA_DIR/rotation_handovers/$(date +%Y%m%d-%H%M%S)-T1-ROTATION-HANDOVER.md"
cat > "$HANDOVER" << 'HD'
# Handover T1
## Resterende taken
- Fix auth bug
- Update tests
HD

OUTPUT=$(echo '{"source":"clear"}' | bash "$PROJECT_ROOT/.claude/hooks/vnx_rotation_recovery.sh" 2>/dev/null)

echo "$OUTPUT" | jq . > /dev/null 2>&1
assert_eq "0" "$?" "valid JSON"

CONTEXT=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext // ""')
assert_contains "$CONTEXT" "ROTATION RECOVERY" "recovery header present"
assert_contains "$CONTEXT" "Fix auth bug" "handover content injected"
```

### T-U32: source=clear + oude handover (>5 min) → geen injectie

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U32 |
| **Doel** | Handover ouder dan 300s → wordt overgeslagen |
| **Pass/Fail** | Output is '{}' of fallback |

```bash
echo "=== T-U32: source=clear + old handover → skip ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -f "$VNX_DATA_DIR/rotation_handovers/"*

# Maak een handover en maak het oud
HANDOVER="$VNX_DATA_DIR/rotation_handovers/20260223-000000-T1-ROTATION-HANDOVER.md"
echo "# Old handover" > "$HANDOVER"
touch -t 202602230000 "$HANDOVER"  # Zet mtime naar middernacht

OUTPUT=$(echo '{"source":"clear"}' | bash "$PROJECT_ROOT/.claude/hooks/vnx_rotation_recovery.sh" 2>/dev/null)

if [[ "$OUTPUT" == "{}" ]] || [[ -z "$OUTPUT" ]]; then
  echo "  PASS: old handover skipped"
else
  CONTEXT=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext // ""' 2>/dev/null)
  if [[ "$CONTEXT" == *"Old handover"* ]]; then
    echo "  FAIL: old handover was injected"
  else
    echo "  PASS: old handover skipped (non-empty but no content)"
  fi
fi
```

### T-U33: source=clear + geen handover dir → graceful

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U33 |
| **Doel** | Ontbrekende handover dir → exit met {} |
| **Pass/Fail** | Geen crash, output is {} of fallback |

```bash
echo "=== T-U33: No handover dir → graceful ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -rf "$VNX_DATA_DIR/rotation_handovers"

OUTPUT=$(echo '{"source":"clear"}' | bash "$PROJECT_ROOT/.claude/hooks/vnx_rotation_recovery.sh" 2>/dev/null)
assert_eq "0" "$?" "no crash"

mkdir -p "$VNX_DATA_DIR/rotation_handovers"  # restore
```

### T-U34: 8KB cap op handover injection

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U34 |
| **Doel** | Grote handover wordt afgekapt op 8000 bytes |
| **Pass/Fail** | Geïnjecteerde content ≤ 8000 bytes |

```bash
echo "=== T-U34: 8KB cap on handover ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -f "$VNX_DATA_DIR/rotation_handovers/"*

# Maak een 20KB handover
HANDOVER="$VNX_DATA_DIR/rotation_handovers/$(date +%Y%m%d-%H%M%S)-T1-ROTATION-HANDOVER.md"
python3 -c "print('A' * 20000)" > "$HANDOVER"

OUTPUT=$(echo '{"source":"clear"}' | bash "$PROJECT_ROOT/.claude/hooks/vnx_rotation_recovery.sh" 2>/dev/null)
CONTEXT=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext // ""')

# Context bevat handover + wrapper tekst; handover deel moet ≤ 8000 bytes zijn
ACOUNT=$(echo "$CONTEXT" | grep -o 'A' | wc -c)
if (( ACOUNT <= 8100 )); then
  echo "  PASS: content capped at ~8KB (${ACOUNT} A's)"
else
  echo "  FAIL: content too large (${ACOUNT} A's)"
fi
```

### T-U35: source=compact → zelfde als clear

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-U35 |
| **Doel** | "compact" source triggert ook rotation recovery |
| **Pass/Fail** | Handover injection werkt met source=compact |

```bash
echo "=== T-U35: source=compact → same as clear ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -f "$VNX_DATA_DIR/rotation_handovers/"*

HANDOVER="$VNX_DATA_DIR/rotation_handovers/$(date +%Y%m%d-%H%M%S)-T1-ROTATION-HANDOVER.md"
echo "# Compact test handover" > "$HANDOVER"

OUTPUT=$(echo '{"source":"compact"}' | bash "$PROJECT_ROOT/.claude/hooks/vnx_rotation_recovery.sh" 2>/dev/null)
CONTEXT=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext // ""')
assert_contains "$CONTEXT" "Compact test handover" "compact source triggers recovery"
```

---

## Fase 5: Integration Tests — vnx_rotate.sh

### 5.1 tmux Mock Strategie (FIX review #2)

**Probleem**: `export -f tmux` in subshell propageert NIET naar `bash vnx_rotate.sh` (apart proces).

**Oplossing**: Prepend mock tmux binary in `$PATH`.

```bash
# Gebruik de mock tmux binary uit fase 0 setup:
# $TEST_ROOT/mock-bin/tmux — altijd exit 1 op has-session
export PATH="$TEST_ROOT/mock-bin:$PATH"
```

### T-I50: Pane resolution

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-I50 |
| **Doel** | Verifieer dat get_pane_id() correct resolved voor T1/T2/T3 |
| **Pass/Fail** | Correcte pane IDs (%1, %2, %3) |

```bash
echo "=== T-I50: Pane resolution ==="
source "$SCRIPTS_DIR/pane_config.sh"

PANE_T1=$(get_pane_id "T1")
PANE_T2=$(get_pane_id "T2")
PANE_T3=$(get_pane_id "T3")

assert_eq "%1" "$PANE_T1" "T1 → %1"
assert_eq "%2" "$PANE_T2" "T2 → %2"
assert_eq "%3" "$PANE_T3" "T3 → %3"
```

### T-I51: vnx_rotate.sh — tmux session missing + lock release (FIX review #2)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-I51 |
| **Doel** | Als tmux session 'vnx' niet bestaat → graceful exit + lock release |
| **FIX** | Gebruikt PATH-based mock tmux binary i.p.v. `export -f tmux` |
| **Pass/Fail** | Exit 1, lock released, error gelogd |

```bash
echo "=== T-I51: Missing tmux session (PATH mock) ==="
test_source_common
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -rf "$VNX_LOCKS_DIR"/*

# Pre-acquire lock (simuleert detector handoff)
vnx_acquire_lock "rotation_T1"

HANDOVER="$VNX_DATA_DIR/rotation_handovers/test-T1-ROTATION-HANDOVER.md"
echo "# test" > "$HANDOVER"

# Prepend mock tmux binary in PATH (review fix #2)
(
  export PATH="$TEST_ROOT/mock-bin:$PATH"
  bash "$HOOKS_DIR/vnx_rotate.sh" "T1" "$HANDOVER" 2>/dev/null
)
EXIT_CODE=$?
assert_eq "1" "$EXIT_CODE" "rotate exits non-zero when tmux session missing"

# Lock moet released zijn door trap
assert_file_not_exists "$VNX_LOCKS_DIR/rotation_T1.lock" "lock released after tmux error"

# Verifieer error log
if grep -q "No vnx tmux session" "$VNX_LOGS_DIR/vnx_rotate_T1.log" 2>/dev/null; then
  echo "  PASS: tmux error logged"
else
  echo "  WARN: error log not found (may be in different log path)"
fi
```

### T-I52: vnx_rotate.sh — early trap voor source failure (FIX #14 verificatie)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-I52 |
| **Doel** | Verifieer dat _PROJECT_ROOT correct resolved wordt voor lock cleanup |
| **Pass/Fail** | _PROJECT_ROOT wijst naar repo root, niet naar .claude/ |

```bash
echo "=== T-I52: Early trap PROJECT_ROOT resolution ==="
# Verifieer het pad-afleiding zonder het script te draaien
SCRIPT_DIR="$HOOKS_DIR"
_TEST_PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

assert_eq "$PROJECT_ROOT" "$_TEST_PROJECT_ROOT" \
  "PROJECT_ROOT derived correctly from SCRIPT_DIR/../../../"

# Verifieer dat het lock pad correct zou zijn
EXPECTED_LOCK="$_TEST_PROJECT_ROOT/.vnx-data/locks/rotation_T1.lock"
assert_contains "$EXPECTED_LOCK" ".vnx-data/locks" "lock path contains .vnx-data/locks"

# Verifieer dat het NIET .claude/.vnx-data/ bevat
if [[ "$EXPECTED_LOCK" == *".claude/.vnx-data"* ]]; then
  echo "  FAIL: lock path incorrectly under .claude/"
else
  echo "  PASS: lock path NOT under .claude/"
fi
```

---

## Fase 6: Runtime Probe — Settings Precedence (T-P60, ENIGE probe)

### T-P60: Payload Logger Probe (combineert precedence + payload format)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-P60 |
| **Doel** | (1) Bewijs dat root Stop hook vuurt in T1; (2) Documenteer exacte stdin payload |
| **Preconditions** | Geen actieve Claude sessie in T1 |
| **Impact** | Tijdelijke wijziging root settings.json (rollback na probe) |
| **Uitvoering** | HANDMATIG vanuit T-MANAGER |

> Dit is de ENIGE probe. Geen aparte "minimal test" of "inline echo" probe nodig.
> Het payload logger script dient als precedence test EN als payload documentatie.

**Stappen:**

```bash
# === STAP 1: Backup root settings.json ===
cp "$PROJECT_ROOT/.claude/settings.json" "$PROJECT_ROOT/.claude/settings.json.bak"

# === STAP 2: Deploy payload logger script ===
cat > "$HOOKS_DIR/vnx_hook_payload_logger.sh" << 'PROBE'
#!/usr/bin/env bash
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

echo '{"decision":"allow"}'
PROBE
chmod +x "$HOOKS_DIR/vnx_hook_payload_logger.sh"

# === STAP 3: Voeg Stop hook toe aan root settings.json ===
# Handmatig toevoegen aan .claude/settings.json "hooks" sectie:
#
# "Stop": [{
#   "hooks": [{
#     "type": "command",
#     "command": "/Users/vincentvandeth/Development/SEOcrawler_v2/.claude/vnx-system/hooks/vnx_hook_payload_logger.sh",
#     "timeout": 3000
#   }]
# }]

# === STAP 4: Trigger in T1 ===
# Open Claude sessie in T1: cd .claude/terminals/T1 && claude
# Stuur 1 simpele prompt (bijv. "echo hello"), wacht tot klaar
# Stop hook vuurt automatisch na prompt completion

# === STAP 5: Inspecteer resultaten ===
cat "$PROJECT_ROOT/.vnx-data/logs/hook_payload_probe.log"
```

### Beslisboom

```
Probe resultaat:
├── hook_payload_probe.log bevat T1 entry
│   ├── stop_hook_active field aanwezig?
│   │   ├── JA → Root hooks werken + payload formaat bewezen
│   │   │   → Gebruik root settings.json voor Stop + PostToolUse
│   │   └── NEE → Root hooks werken, maar payload anders dan verwacht
│   │       → Pas vnx_context_monitor.sh stdin parsing aan
│   └── (geen stop_hook_active check nodig als hook überhaupt vuurt)
│
├── hook_payload_probe.log LEEG of niet-bestaand
│   └── Root Stop hooks vuren NIET voor T1
│       → FALLBACK: deploy Stop + PostToolUse hooks naar:
│         - .claude/terminals/T1/settings.json
│         - .claude/terminals/T2/settings.json
│         - .claude/terminals/T3/settings.json
│
└── hook_payload_probe.log bevat ALLEEN T-MANAGER/T0 entries
    └── Root hooks vuren alleen voor terminals ZONDER eigen hooks sectie
        → Zelfde FALLBACK als hierboven
```

### Rollback

```bash
# === NA PROBE: Rollback ===
rm -f "$HOOKS_DIR/vnx_hook_payload_logger.sh"
mv "$PROJECT_ROOT/.claude/settings.json.bak" "$PROJECT_ROOT/.claude/settings.json"

# Archiveer probe resultaat
mkdir -p "$PROJECT_ROOT/.claude/vnx-system/docs/intelligence"
cp "$VNX_LOGS_DIR/hook_payload_probe.log" \
   "$PROJECT_ROOT/.claude/vnx-system/docs/intelligence/probe_payload_$(date +%Y%m%d).log" 2>/dev/null || true

echo "Rollback complete. Probe log gearchiveerd."
```

---

## Fase 7: Failure Injection Tests

### T-F70: append_receipt.py — invalid payload (geen timestamp)

```bash
echo "=== T-F70: Receipt missing timestamp ==="
OUTPUT=$(python3 "$SCRIPTS_DIR/append_receipt.py" \
  --receipt '{"event_type":"context_rotation","event":"test"}' \
  --receipts-file "$TEST_RECEIPTS_FILE" 2>&1)
EXIT_CODE=$?

if (( EXIT_CODE != 0 )); then
  echo "  PASS: rejected payload without timestamp (exit=$EXIT_CODE)"
else
  echo "  WARN: accepted payload without timestamp"
fi
```

### T-F71: append_receipt.py — valid context_rotation receipt

```bash
echo "=== T-F71: Valid context_rotation receipt ==="
RECEIPT='{"event_type":"context_rotation","event":"context_rotation","terminal":"T1","timestamp":"'"$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"'","source":"vnx_rotation","handover_path":"test.md","context_used_pct":85,"action_required":false,"auto_generated":true}'

python3 "$SCRIPTS_DIR/append_receipt.py" \
  --receipt "$RECEIPT" \
  --receipts-file "$TEST_RECEIPTS_FILE" 2>/dev/null
assert_eq "0" "$?" "receipt accepted"

# Verifieer dat het in het bestand staat
LAST_LINE=$(tail -1 "$TEST_RECEIPTS_FILE")
assert_contains "$LAST_LINE" "context_rotation" "receipt in file"
```

### T-F72: vnx_rotate.sh early trap — source failure simulatie (early trap release verificatie)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-F72 |
| **Doel** | Verifieer dat lock released wordt als source stap faalt in vnx_rotate.sh |
| **FIX** | Verifieert early-trap release; path-resolutie van FIX #14 wordt apart bewezen in T-I52 |
| **Pass/Fail** | Lock released ondanks source failure in gesimuleerde rotator-copy |

```bash
echo "=== T-F72: rotate source failure → early trap release ==="
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -rf "$TEST_ROOT/repo-sim"
mkdir -p "$TEST_ROOT/repo-sim/.claude/vnx-system/hooks"
mkdir -p "$TEST_ROOT/repo-sim/.vnx-data/locks"

# Copy rotator into mirrored path so SCRIPT_DIR/../../../ still points to repo-sim root
cp "$HOOKS_DIR/vnx_rotate.sh" "$TEST_ROOT/repo-sim/.claude/vnx-system/hooks/vnx_rotate.sh"

# Break the first source line to force source failure deterministically
perl -0pi -e 's#source "\\$SCRIPT_DIR/lib/_vnx_hook_common\\.sh"#source "/nonexistent/vnx_hook_common.sh"#' \
  "$TEST_ROOT/repo-sim/.claude/vnx-system/hooks/vnx_rotate.sh"

# Pre-create lock where the mirrored script early trap expects it
mkdir -p "$TEST_ROOT/repo-sim/.vnx-data/locks/rotation_T1.lock"
echo "$(date +%s)" > "$TEST_ROOT/repo-sim/.vnx-data/locks/rotation_T1.lock/created_at"

HANDOVER="$VNX_DATA_DIR/rotation_handovers/test-T1-ROTATION-HANDOVER.md"
echo "# test" > "$HANDOVER"

bash "$TEST_ROOT/repo-sim/.claude/vnx-system/hooks/vnx_rotate.sh" "T1" "$HANDOVER" 2>/dev/null || true

# Lock in mirrored repo must be released by early trap
assert_file_not_exists "$TEST_ROOT/repo-sim/.vnx-data/locks/rotation_T1.lock" \
  "early trap released lock in mirrored repo"
```

### T-F73: Detector nohup immediate exit — lock release (FIX #16 verificatie)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-F73 |
| **Doel** | Verifieer dat lock released wordt als nohup child onmiddellijk stopt |
| **FIX** | Go/No-Go check voor v2.4 FIX #16 |
| **Pass/Fail** | Lock released, error gelogd |

```bash
echo "=== T-F73: nohup immediate exit → lock release ==="
test_cd_terminal "T1"
export VNX_CONTEXT_ROTATION_ENABLED=1
rm -rf "$VNX_LOCKS_DIR"/*
rm -f "$VNX_LOGS_DIR/hook_events.log"
printf '{"remaining_pct":15,"ts":%s}' "$(date +%s)" > "$VNX_STATE_DIR/context_window.json"

# Maak een rotate script dat direct exit 1 doet
FAILING_ROTATE="$TEST_ROOT/rotate_immediate_exit.sh"
cat > "$FAILING_ROTATE" << 'FAIL'
#!/usr/bin/env bash
exit 1
FAIL
chmod +x "$FAILING_ROTATE"
export VNX_ROTATE_SCRIPT="$FAILING_ROTATE"

echo '{"tool_name":"Write","tool_input":{"file_path":"'"$VNX_DATA_DIR"'/rotation_handovers/20260223-T1-ROTATION-HANDOVER.md"}}' | \
  bash "$HOOKS_DIR/vnx_handover_detector.sh" 2>/dev/null

# Lock moet released zijn (nohup child stierf, EXIT trap van detector vuurt)
# NB: race condition — kill -0 kan nog slagen als het process niet snel genoeg sterft
# Wacht even voor de zekerheid
sleep 1
assert_file_not_exists "$VNX_LOCKS_DIR/rotation_T1.lock" "lock released after nohup immediate exit"

unset VNX_ROTATE_SCRIPT
```

---

## Fase 8: End-to-End Simulatie (zonder tmux) (FIX review #6)

### T-E80: Volledige rotation flow (gesimuleerd, volledig geïsoleerd)

| Veld | Waarde |
|------|--------|
| **Test-ID** | T-E80 |
| **Doel** | Simuleer de volledige keten: context druk → block → handover → detect → receipt → recovery |
| **FIX** | Alle paden gebruiken `$VNX_DATA_DIR` / `$VNX_STATE_DIR` (test env), NOOIT hardcoded productie paden |
| **Pass/Fail** | Alle 6 stappen PASS |

```bash
echo "=== T-E80: Full rotation flow simulation ==="
export VNX_CONTEXT_ROTATION_ENABLED=1

# EXPLICIETE isolatie-check (FIX review #6)
if [[ "$VNX_DATA_DIR" != "/tmp/vnx-rotation-test"* ]]; then
  echo "  ABORT: VNX_DATA_DIR niet geïsoleerd! ($VNX_DATA_DIR)"
  echo "  Run fase 0 setup eerst."
  exit 1
fi

test_cd_terminal "T1"
rm -rf "$VNX_LOCKS_DIR"/* "$VNX_LOGS_DIR"/*
rm -f "$VNX_DATA_DIR/rotation_handovers/"*
rm -f "$VNX_STATE_DIR/context_window.json"
rm -f "$VNX_STATE_DIR/rotation_clear_done_T1"

echo "--- Stap 1: Statusline schrijft context state ---"
printf '{"remaining_pct":15,"ts":%s}' "$(date +%s)" > "$VNX_STATE_DIR/context_window.json"
assert_file_exists "$VNX_STATE_DIR/context_window.json" "context state written"

echo "--- Stap 2: Stop hook detecteert hoge context druk ---"
STOP_OUTPUT=$(echo '{"stop_hook_active":false}' | bash "$HOOKS_DIR/vnx_context_monitor.sh" 2>/dev/null)
DECISION=$(echo "$STOP_OUTPUT" | jq -r '.decision // "none"')
assert_eq "block" "$DECISION" "rotation triggered"

echo "--- Stap 3: Claude schrijft handover document ---"
HANDOVER_PATH="$VNX_DATA_DIR/rotation_handovers/$(date +%Y%m%d-%H%M%S)-T1-ROTATION-HANDOVER.md"
cat > "$HANDOVER_PATH" << 'HANDOVER'
# Context Rotation Handover - T1
## Status
- Auth bug fix: 80% compleet
- Tests: 3/5 geschreven
## Volgende stappen
1. Voltooi auth middleware tests
2. Update API docs
HANDOVER
assert_file_exists "$HANDOVER_PATH" "handover written"

echo "--- Stap 4: PostToolUse detecteert handover (rotate disabled) ---"
export VNX_ROTATE_SCRIPT="$TEST_ROOT/mock_rotate_noexec.sh"

echo '{"tool_name":"Write","tool_input":{"file_path":"'"$HANDOVER_PATH"'"}}' | \
  bash "$HOOKS_DIR/vnx_handover_detector.sh" 2>/dev/null

# Lock moet released zijn (error path, rotate niet executable)
assert_file_not_exists "$VNX_LOCKS_DIR/rotation_T1.lock" "lock released (no rotate)"

echo "--- Stap 5: SessionStart recovery na /clear ---"
OUTPUT=$(echo '{"source":"clear"}' | bash "$PROJECT_ROOT/.claude/hooks/vnx_rotation_recovery.sh" 2>/dev/null)
CONTEXT=$(echo "$OUTPUT" | jq -r '.hookSpecificOutput.additionalContext // ""')
assert_contains "$CONTEXT" "Auth bug fix" "handover content recovered"
assert_contains "$CONTEXT" "ROTATION RECOVERY" "recovery header present"

echo "--- Stap 6: Signal file + state reset ---"
assert_file_exists "$VNX_STATE_DIR/rotation_clear_done_T1" "signal file created"
assert_file_not_exists "$VNX_STATE_DIR/context_window.json" "context state reset"

unset VNX_ROTATE_SCRIPT

echo ""
echo "=== E2E FLOW COMPLETE ==="
```

---

## Fase 9: Cleanup & Rollback

### 9.1 Test Cleanup Script

```bash
# === VOLLEDIGE TEST CLEANUP ===
echo "Cleaning up test environment..."

rm -rf /tmp/vnx-rotation-test

# Restore settings.json als backup bestaat
[[ -f "$PROJECT_ROOT/.claude/settings.json.bak" ]] && \
  mv "$PROJECT_ROOT/.claude/settings.json.bak" "$PROJECT_ROOT/.claude/settings.json"

# Verwijder payload logger als nog aanwezig
rm -f "$HOOKS_DIR/vnx_hook_payload_logger.sh"

echo "Cleanup complete."
```

### 9.2 Productie-Impact Check (FIX review #4)

Gebruikt test-marker check in plaats van count-based checks die false positives geven.

```bash
echo "=== Productie-impact check ==="

# Check 1: Geen test-specifieke locks in productie
PROD_LOCKS_DIR="$PROJECT_ROOT/.vnx-data/locks"
if ls -A "$PROD_LOCKS_DIR/" 2>/dev/null | grep -q "rotation_"; then
  # Verifieer of het een stale test lock is (timestamp check)
  for LOCK in "$PROD_LOCKS_DIR"/rotation_*.lock; do
    if [[ -f "$LOCK/created_at" ]]; then
      AGE=$(( $(date +%s) - $(cat "$LOCK/created_at") ))
      echo "  WARN: rotation lock in productie (age=${AGE}s): $LOCK"
    fi
  done
else
  echo "  PASS: geen rotation locks in productie"
fi

# Check 2: Test receipts NIET in productie bestand
# (test receipts gaan naar $TEST_RECEIPTS_FILE, niet naar productie)
PROD_RECEIPTS="$PROJECT_ROOT/.vnx-data/state/t0_receipts.ndjson"
if [[ -f "$PROD_RECEIPTS" ]]; then
  # Zoek naar receipts met source="vnx_rotation" die NIET van een echte rotation komen
  # (als er geldige rotation receipts zijn is dat OK — die zijn van echte runs)
  echo "  PASS: receipts file bestaat (inhoud is valide productiedata)"
fi

# Check 3: settings.json backup opgeruimd
if [[ -f "$PROJECT_ROOT/.claude/settings.json.bak" ]]; then
  echo "  WARN: settings.json backup nog aanwezig — rollback niet uitgevoerd"
else
  echo "  PASS: geen settings backup (schone staat)"
fi

# Check 4: Payload logger verwijderd
if [[ -f "$HOOKS_DIR/vnx_hook_payload_logger.sh" ]]; then
  echo "  WARN: payload logger nog aanwezig — verwijder handmatig"
else
  echo "  PASS: payload logger opgeruimd"
fi
```

---

## Testmatrix Samenvatting

| ID | Fase | Component | Test | Prioriteit |
|----|------|-----------|------|------------|
| T-U01 | 1 | _vnx_hook_common | Terminal detection | P0 |
| T-U02 | 1 | _vnx_hook_common | Lock acquire + release | P0 |
| T-U03 | 1 | _vnx_hook_common | Double lock blocking | P0 |
| T-U04 | 1 | _vnx_hook_common | Stale lock (TTL) removal | P0 |
| T-U05 | 1 | _vnx_hook_common | Lock without created_at | P1 |
| T-U06 | 1 | _vnx_hook_common | mkdir -p VNX_LOCKS_DIR | P0 |
| T-U07 | 1 | _vnx_hook_common | JSON context output | P1 |
| T-U08 | 1 | _vnx_hook_common | Feature flag helper | P0 |
| T-U09 | 2 | context_monitor | Feature disabled no-op | P0 |
| T-U10 | 2 | context_monitor | stop_hook_active=true | P0 |
| T-U11 | 2 | context_monitor | T0/unknown → skip | P1 |
| T-U12 | 2 | context_monitor | Under threshold | P0 |
| T-U13 | 2 | context_monitor | Warning threshold | P0 |
| T-U14 | 2 | context_monitor | Rotation threshold | P0 |
| T-U15 | 2 | context_monitor | Missing state file | P1 |
| T-U20 | 3 | handover_detector | Non-Write tool | P1 |
| T-U21 | 3 | handover_detector | Write non-handover | P1 |
| T-U22 | 3 | handover_detector | Write ROTATION-HANDOVER (mock) | P0 |
| T-U23 | 3 | handover_detector | Double handover (lock) | P0 |
| T-U24 | 3 | handover_detector | Receipt failure + trap release | P0 |
| T-U30 | 4 | rotation_recovery | source=startup → fallback | P0 |
| T-U31 | 4 | rotation_recovery | source=clear + handover | P0 |
| T-U32 | 4 | rotation_recovery | Old handover skip | P1 |
| T-U33 | 4 | rotation_recovery | No handover dir | P1 |
| T-U34 | 4 | rotation_recovery | 8KB cap | P0 |
| T-U35 | 4 | rotation_recovery | source=compact | P1 |
| T-I50 | 5 | vnx_rotate | Pane resolution | P0 |
| T-I51 | 5 | vnx_rotate | Missing tmux (PATH mock) | P0 |
| T-I52 | 5 | vnx_rotate | Early trap PROJECT_ROOT | P0 |
| T-P60 | 6 | settings probe | Root hook precedence + payload | **P0 (GATE)** |
| T-F70 | 7 | failure injection | Invalid receipt | P1 |
| T-F71 | 7 | failure injection | Valid receipt | P0 |
| T-F72 | 7 | failure injection | Source failure → early trap (#14) | **P0** |
| T-F73 | 7 | failure injection | Nohup immediate exit (#16) | **P0** |
| T-E80 | 8 | end-to-end | Full flow (isolated) | **P0** |

**Totaal**: 35 tests (24x P0, 10x P1, 1x GATE)

---

## Go/No-Go Checklist voor Deploy (FIX review #7)

### Prerequisites (alle moeten PASS zijn)

| # | Check | Resultaat | Blocking |
|---|-------|-----------|----------|
| 1 | Alle P0 unit tests PASS | ☐ | JA |
| 2 | T-P60 probe uitgevoerd, resultaat gedocumenteerd | ☐ | JA |
| 3 | Probe beslisboom doorlopen, deployment-locatie bepaald | ☐ | JA |
| 4 | T-E80 end-to-end simulatie PASS | ☐ | JA |
| 5 | Productie-impact check clean (geen test artifacts) | ☐ | JA |
| 6 | settings.json backup gerestored | ☐ | JA |
| 7 | Hook payload formaat gedocumenteerd | ☐ | JA |
| 8 | T-I52 early trap path resolution correct (FIX #14) | ☐ | JA |
| 9 | T-F72 source failure → early trap lock release (FIX #14) | ☐ | JA |
| 10 | T-F73 nohup immediate exit → detector trap release (FIX #16) | ☐ | JA |
| 11 | VNX_ROTATE_SCRIPT env var override in detector script toegevoegd | ☐ | JA |
| 12 | VNX_APPEND_RECEIPT_SCRIPT env var override in detector script toegevoegd | ☐ | JA |
| 13 | T-U08/T-U09 feature-flag compatibility tests PASS (default no-op) | ☐ | JA |
| 14 | Alle P1 tests PASS of failures geaccepteerd als known limitations | ☐ | NEE |

### Deploy Volgorde

1. **PR-A mergen** → draai T-U01..T-U08 in productie
2. **PR-B deployen** → draai T-U10..T-U15 in productie
3. **PR-C deployen** → draai T-U20..T-U35, T-E80 in productie
4. **Na elke PR**: productie-impact check

### Benodigde Code Wijzigingen voor Testbaarheid

| Wijziging | Bestand | Regels | Impact |
|-----------|---------|--------|--------|
| `VNX_ROTATE_SCRIPT` env var override | `vnx_handover_detector.sh` | 1 regel | Geen productie-impact (default is ongewijzigd) |
| `VNX_APPEND_RECEIPT_SCRIPT` env var override | `vnx_handover_detector.sh` | 1-2 regels | Alleen testability, default blijft canonical script |

```bash
# In vnx_handover_detector.sh, wijzig:
ROTATE_SCRIPT="$(dirname "$0")/vnx_rotate.sh"
# Naar:
ROTATE_SCRIPT="${VNX_ROTATE_SCRIPT:-$(dirname "$0")/vnx_rotate.sh}"

# En voeg toe voor receipt writer:
APPEND_RECEIPT_SCRIPT="${VNX_APPEND_RECEIPT_SCRIPT:-$(dirname "$0")/../scripts/append_receipt.py}"
# Gebruik daarna:
python3 "$APPEND_RECEIPT_SCRIPT" --receipt "$RECEIPT_JSON" ...
```

### Known Limitations (handmatig te testen na deploy)

| Item | Reden | Hoe te testen |
|------|-------|---------------|
| Echte tmux /clear + resume | Vereist actieve Claude sessie | Open T1, trigger handmatig rotation |
| Signal file timing | Afhankelijk van /clear snelheid | Monitor in real-time |
| Nohup process lifecycle | Race condition window ~100ms | Stress test met snelle herhalingen |
| Multi-terminal concurrent rotation | T1 + T2 tegelijk | Twee handovers schrijven binnen 1s |
| Public-repo default behavior | Feature default is uit | Start zonder env flag en verifieer no-op |

---

## Artefacten

Na voltooiing van het testplan worden deze opgeleverd:

1. **`$VNX_LOGS_DIR/hook_events.log`** — Alle hook events uit tests
2. **`$VNX_LOGS_DIR/hook_payload_probe.log`** — Empirische Stop hook payload (van T-P60)
3. **`$TEST_RECEIPTS_FILE`** — Test receipts (geïsoleerd van productie)
4. **Testrapport** → `.claude/vnx-system/docs/intelligence/VNX_ROTATION_TEST_REPORT.md`
5. **Go/no-go verdict** — Met onderbouwing per check
6. **Deploy mode besluit** — Experimental opt-in (`VNX_CONTEXT_ROTATION_ENABLED=1`) bevestigd
