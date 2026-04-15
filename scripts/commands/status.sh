#!/usr/bin/env bash
# VNX Command: status
# Sourced by bin/vnx — shows terminal health, dispatch queue, and gate results.
#
# All variables from bin/vnx (VNX_HOME, VNX_DATA_DIR, VNX_STATE_DIR,
# VNX_DISPATCH_DIR, log, err) are available when this runs.

# ── Color helpers ──────────────────────────────────────────────────────────
_s_has_color() { [ -t 1 ] && command -v tput >/dev/null 2>&1; }

_s_bold()   { _s_has_color && tput bold 2>/dev/null || true; }
_s_reset()  { _s_has_color && tput sgr0 2>/dev/null || true; }
_s_green()  { _s_has_color && tput setaf 2 2>/dev/null || true; }
_s_yellow() { _s_has_color && tput setaf 3 2>/dev/null || true; }
_s_red()    { _s_has_color && tput setaf 1 2>/dev/null || true; }
_s_cyan()   { _s_has_color && tput setaf 6 2>/dev/null || true; }
_s_dim()    { _s_has_color && tput dim 2>/dev/null || true; }

_s_ok()   { printf '%s%s%s' "$(_s_green)" "●" "$(_s_reset)"; }
_s_warn() { printf '%s%s%s' "$(_s_yellow)" "●" "$(_s_reset)"; }
_s_fail() { printf '%s%s%s' "$(_s_red)" "●" "$(_s_reset)"; }

_s_header() {
  printf '%s%s%s\n' "$(_s_bold)$(_s_cyan)" "$1" "$(_s_reset)"
}

# ── Data helpers ───────────────────────────────────────────────────────────

_s_t0_state() {
  local f="${VNX_STATE_DIR}/t0_state.json"
  if [ -f "$f" ]; then
    python3 -c "
import json, sys
try:
    d = json.load(open('$f'))
    sys.stdout.write(json.dumps(d))
except Exception as e:
    sys.stdout.write('{}')
"
  else
    printf '{}'
  fi
}

_s_count_dispatches() {
  local dir="$1"
  if [ -d "$dir" ]; then
    # Count both .md files and subdirectories
    local n
    n=$(find "$dir" -maxdepth 1 \( -name "*.md" -o -type d ! -path "$dir" \) 2>/dev/null | wc -l | tr -d ' ')
    printf '%s' "$n"
  else
    printf '0'
  fi
}

_s_gate_results() {
  local gate_dir="${VNX_STATE_DIR}/review_gates/results"
  if [ ! -d "$gate_dir" ]; then
    printf '[]'
    return
  fi
  python3 -c "
import json, os, glob, sys

gate_dir = '$gate_dir'
results = []
for f in sorted(glob.glob(os.path.join(gate_dir, '*.json')))[-10:]:
    try:
        d = json.load(open(f))
        results.append({
            'file': os.path.basename(f),
            'gate': d.get('gate', '?'),
            'pr_number': d.get('pr_number', d.get('pr_id', '?')),
            'status': d.get('status', '?'),
            'blocking': len(d.get('blocking_findings', [])),
            'recorded_at': d.get('recorded_at', '?'),
        })
    except Exception:
        pass
print(json.dumps(results))
"
}

# ── Section printers ───────────────────────────────────────────────────────

_s_print_terminals() {
  _s_header "Terminals"
  local state
  state=$(_s_t0_state)

  python3 -c "
import json, sys
state = json.loads('''$state''')
terminals = state.get('terminals', {})
if not terminals:
    print('  (no terminal data — run vnx start first)')
    sys.exit(0)

fmt = '  {:<4} {:<8} {:<10} {:<20} {:<12} {}'
print(fmt.format('ID', 'Track', 'Lease', 'Dispatch', 'Status', 'Last Update'))
print('  ' + '-'*70)
for tid, t in sorted(terminals.items()):
    lease = t.get('lease_state', 'idle')
    status = t.get('status', 'unknown')
    dispatch = (t.get('current_dispatch') or '')[:19]
    last_update = t.get('last_update', 'never')[:19]
    track = t.get('track', '?')
    print(fmt.format(tid, track, lease, dispatch, status, last_update))
"
  printf '\n'
}

_s_print_dispatches() {
  _s_header "Dispatch Queue"
  local pending active completed
  pending=$(_s_count_dispatches "${VNX_DISPATCH_DIR}/pending")
  active=$(_s_count_dispatches "${VNX_DISPATCH_DIR}/active")
  completed=$(_s_count_dispatches "${VNX_DISPATCH_DIR}/completed")

  printf '  Pending:   %s%s%s\n' "$(_s_yellow)" "$pending" "$(_s_reset)"
  printf '  Active:    %s%s%s\n' "$(_s_green)" "$active" "$(_s_reset)"
  printf '  Completed: %s%s%s\n' "$(_s_dim)" "$completed" "$(_s_reset)"

  # List pending items
  if [ "$pending" -gt 0 ] && [ -d "${VNX_DISPATCH_DIR}/pending" ]; then
    printf '\n  Pending dispatches:\n'
    for f in "${VNX_DISPATCH_DIR}/pending"/*.md "${VNX_DISPATCH_DIR}/pending"/*/dispatch.json; do
      [ -e "$f" ] || continue
      local name
      if [[ "$f" == *.md ]]; then
        name="$(basename "$f")"
        local target role
        target=$(head -3 "$f" 2>/dev/null | grep '^\[\[TARGET:' | sed 's/\[\[TARGET://;s/\]\]//')
        role=$(head -5 "$f" 2>/dev/null | grep '^Role:' | sed 's/Role: *//')
        printf '    %s%-40s%s  %s→%s %s [%s]\n' \
          "$(_s_dim)" "$name" "$(_s_reset)" \
          "$(_s_cyan)" "$(_s_reset)" \
          "${target:-?}" "${role:-?}"
      fi
    done
  fi

  # List active items
  if [ "$active" -gt 0 ] && [ -d "${VNX_DISPATCH_DIR}/active" ]; then
    printf '\n  Active dispatches:\n'
    for f in "${VNX_DISPATCH_DIR}/active"/*.md "${VNX_DISPATCH_DIR}/active"/*/dispatch.json; do
      [ -e "$f" ] || continue
      if [[ "$f" == *.md ]]; then
        local name
        name="$(basename "$f")"
        local target
        target=$(head -3 "$f" 2>/dev/null | grep '^\[\[TARGET:' | sed 's/\[\[TARGET://;s/\]\]//')
        printf '    %s%-40s%s  %s→%s %s\n' \
          "$(_s_bold)" "$name" "$(_s_reset)" \
          "$(_s_green)" "$(_s_reset)" "${target:-?}"
      fi
    done
  fi
  printf '\n'
}

_s_print_gates() {
  _s_header "Gate Results (last 10)"
  local results
  results=$(_s_gate_results)

  python3 -c "
import json, sys
results = json.loads('''$results''')
if not results:
    print('  (no gate results found)')
    sys.exit(0)

fmt = '  {:<25} {:<18} {:<10} {:<8} {}'
print(fmt.format('Gate', 'PR', 'Status', 'Blocking', 'Recorded'))
print('  ' + '-'*72)
for r in results:
    status = r['status']
    marker = '[PASS]' if status == 'completed' else '[FAIL]' if status == 'failed' else f'[{status[:6]}]'
    print(fmt.format(
        str(r['gate'])[:24],
        str(r['pr_number'])[:17],
        marker,
        str(r['blocking']),
        str(r['recorded_at'])[:19],
    ))
"
  printf '\n'
}

_s_print_workers() {
  _s_header "Worker Health"
  local health_file="${VNX_DATA_DIR}/events/worker_health.json"
  if [ ! -f "$health_file" ]; then
    printf '  (no worker health data — no active subprocess dispatches)\n\n'
    return
  fi

  python3 -c "
import json, sys

health_file = '$health_file'
try:
    data = json.load(open(health_file))
except Exception as e:
    print(f'  (could not read worker_health.json: {e})')
    sys.exit(0)

if not data:
    print('  (all workers idle)')
    sys.exit(0)

# ANSI color codes (used directly since tput is not available inside python3 -c)
GREEN  = '\033[32m'
YELLOW = '\033[33m'
RED    = '\033[31m'
DIM    = '\033[2m'
RESET  = '\033[0m'
BOLD   = '\033[1m'

STATUS_COLOR = {
    'active':    GREEN,
    'slow':      YELLOW,
    'stuck':     RED,
    'completed': DIM,
    'idle':      DIM,
}

fmt = '  {:<4} {:<12} {:<10} {:<8} {:<20} {}'
print(fmt.format('ID', 'Status', 'Events', 'Elapsed', 'Last Tool', 'Progress'))
print('  ' + '-'*64)

for terminal_id in sorted(data.keys()):
    w = data[terminal_id]
    status = w.get('status', 'idle')
    color = STATUS_COLOR.get(status, DIM)
    status_str = f'{color}{status:<10}{RESET}'
    events = str(w.get('events', 0))
    elapsed = w.get('elapsed', '?')
    last_tool = str(w.get('last_tool', ''))[:19]
    progress = w.get('estimated_progress', 0.0)
    bar_filled = int(progress * 10)
    bar = '[' + '#' * bar_filled + '.' * (10 - bar_filled) + f'] {int(progress*100)}%'
    print(fmt.format(terminal_id, status_str, events, elapsed, last_tool, bar))
print()
"
}

_s_print_summary() {
  _s_header "VNX Status"
  local state
  state=$(_s_t0_state)
  python3 -c "
import json, sys
state = json.loads('''$state''')
gen_at = state.get('generated_at', '?')[:19]
stale = state.get('staleness_seconds', '?')
queues = state.get('queues', {})
print(f'  State generated:  {gen_at}')
print(f'  Staleness:        {stale}s')
print(f'  Pending:          {queues.get(\"pending_count\", \"?\")}')
print(f'  Active:           {queues.get(\"active_count\", \"?\")}')
print(f'  Completed/hr:     {queues.get(\"completed_last_hour\", \"?\")}')
print(f'  Conflicts:        {queues.get(\"conflict_count\", \"?\")}')
"
  printf '\n'
}

# ── Main command ───────────────────────────────────────────────────────────

cmd_status() {
  local mode="all"
  local json_out=0

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --terminals)  mode="terminals" ;;
      --dispatches) mode="dispatches" ;;
      --gates)      mode="gates" ;;
      --workers)    mode="workers" ;;
      --json)       json_out=1 ;;
      -h|--help)
        cat <<HELP
Usage: vnx status [OPTIONS]

Show VNX system status: terminals, dispatch queue, and gate results.

Options:
  --terminals   Show only terminal health
  --dispatches  Show only dispatch queue
  --gates       Show only gate results
  --workers     Show live worker health (subprocess dispatches)
  --json        Output raw t0_state.json as machine-readable JSON
  -h, --help    Show this help

Examples:
  vnx status
  vnx status --terminals
  vnx status --gates
  vnx status --workers
  vnx status --json
HELP
        return 0 ;;
      *)
        err "[status] Unknown option: $1"
        return 1 ;;
    esac
    shift
  done

  if [ "$json_out" -eq 1 ]; then
    # Refresh t0_state and emit JSON
    if command -v python3 >/dev/null 2>&1 && [ -f "$VNX_HOME/scripts/build_t0_state.py" ]; then
      PYTHONPATH="$VNX_HOME/scripts/lib:${PYTHONPATH:-}" \
        python3 "$VNX_HOME/scripts/build_t0_state.py" 2>/dev/null \
        || cat "${VNX_STATE_DIR}/t0_state.json" 2>/dev/null \
        || printf '{}'
    else
      cat "${VNX_STATE_DIR}/t0_state.json" 2>/dev/null || printf '{}'
    fi
    return 0
  fi

  # Refresh t0_state (best-effort, non-blocking)
  if command -v python3 >/dev/null 2>&1 && [ -f "$VNX_HOME/scripts/build_t0_state.py" ]; then
    PYTHONPATH="$VNX_HOME/scripts/lib:${PYTHONPATH:-}" \
      python3 "$VNX_HOME/scripts/build_t0_state.py" >/dev/null 2>&1 || true
  fi

  printf '\n'
  case "$mode" in
    all)
      _s_print_summary
      _s_print_terminals
      _s_print_dispatches
      _s_print_gates
      ;;
    terminals)  _s_print_terminals ;;
    dispatches) _s_print_dispatches ;;
    gates)      _s_print_gates ;;
    workers)    _s_print_workers ;;
  esac
}
