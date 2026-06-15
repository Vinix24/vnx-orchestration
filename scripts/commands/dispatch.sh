#!/usr/bin/env bash
# VNX Command: dispatch
# Sourced by bin/vnx — delivers a dispatch .md file to a VNX worker.
#
# DEFAULT lane: subscription-preserving ephemeral tmux-spawn
#   (scripts/lib/tmux_interactive_dispatch.py — interactive claude, never `claude -p`).
# OPT-IN burst lane: paid headless SubprocessAdapter
#   (scripts/lib/subprocess_dispatch.py — `claude -p`), selected via:
#     --adapter subprocess  (CLI flag)  >  Adapter: subprocess  (file header)
#       >  VNX_ADAPTER=subprocess  (env)  >  default: tmux
#
# All variables from bin/vnx (VNX_HOME, VNX_DATA_DIR, VNX_STATE_DIR,
# VNX_DISPATCH_DIR, log, err) are available when this runs.

# ── Helpers ────────────────────────────────────────────────────────────────

_d_parse_header() {
  # Parse [[TARGET:TX]], Role:, Gate:, Feature:, Adapter: from the dispatch file header.
  # Outputs: TERMINAL ROLE GATE FEATURE ADAPTER (tab-separated)
  local file="$1"
  python3 -c "
import re, sys

path = '$file'
target = role = gate = feature = adapter = ''
try:
    with open(path) as f:
        for i, line in enumerate(f):
            if i > 20:
                break
            line = line.rstrip()
            m = re.match(r'\[\[TARGET:(T[0-3])\]\]', line)
            if m:
                target = m.group(1)
            m = re.match(r'Role:\s*(.+)', line)
            if m:
                role = m.group(1).strip()
            m = re.match(r'Gate:\s*(.+)', line)
            if m:
                gate = m.group(1).strip()
            m = re.match(r'Feature:\s*(.+)', line)
            if m:
                feature = m.group(1).strip()
            m = re.match(r'Adapter:\s*(.+)', line)
            if m:
                adapter = m.group(1).strip()
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
print(f'{target}\t{role}\t{gate}\t{feature}\t{adapter}')
"
}

_d_check_terminal_idle() {
  # Returns 0 if terminal is idle (not leased), 1 otherwise.
  local terminal="$1"
  python3 -c "
import sys, os
sys.path.insert(0, '$VNX_HOME/scripts/lib')
try:
    from lease_manager import LeaseManager
    lm = LeaseManager(state_dir='$VNX_STATE_DIR', auto_init=False)
    state = lm.get_state('$terminal')
    if state and state.get('lease_state') == 'leased':
        print(f'Terminal $terminal is leased to dispatch: {state.get(\"dispatch_id\", \"?\")}', file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
except Exception as e:
    # If we can't read lease state, treat as available
    sys.exit(0)
"
}

_d_generate_dispatch_id() {
  # Generate a unique dispatch ID based on timestamp + slug + track.
  local slug="$1"
  local track="${2:-A}"
  python3 -c "
import sys
sys.path.insert(0, '$VNX_HOME/scripts/lib')
try:
    from headless_dispatch_writer import generate_dispatch_id
    print(generate_dispatch_id('$slug', '$track'))
except Exception:
    import datetime, hashlib
    ts = datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    print(f'{ts}-cli-dispatch-{\"$track\"}')
"
}

_d_resolve_track() {
  # Map terminal ID to track letter.
  case "$1" in
    T1) printf 'A' ;;
    T2) printf 'B' ;;
    T3) printf 'C' ;;
    *)  printf 'A' ;;
  esac
}

# ── Single-entry dispatch (VNX_SINGLE_ENTRY_DISPATCH=1) ───────────────────

_d_single_entry_dispatch() {
  # Accepts --spec-file <abs> or <pending-id> (resolved to its bundle's dispatch-spec.json).
  # VNX_DISPATCH_LEGACY=1 is checked by the caller — not re-checked here.
  local spec_file=""
  local dry_run_flag=""
  local pending_id=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --spec-file)
        spec_file="$2"; shift 2 ;;
      --spec-file=*)
        spec_file="${1#*=}"; shift ;;
      --dry-run|-n)
        dry_run_flag="--dry-run"; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx dispatch [--spec-file <abs>] [--dry-run]
       vnx dispatch <pending-id> [--dry-run]

Single-entry gate (VNX_SINGLE_ENTRY_DISPATCH=1).
  --spec-file <abs>   Absolute path to dispatch-spec.json
  <pending-id>        Dispatch ID resolved to dispatches/pending/<id>/dispatch-spec.json
  --dry-run           Print plan + fingerprint; spawn nothing
  VNX_DISPATCH_LEGACY=1  Force legacy path even when gate is on
HELP
        return 0 ;;
      -*)
        err "[dispatch] single-entry gate: unknown flag: $1"
        return 1 ;;
      *)
        if [ -n "$pending_id" ]; then
          err "[dispatch] single-entry gate: unexpected positional argument: $1"
          return 1
        fi
        pending_id="$1"; shift ;;
    esac
  done

  if [ -z "$spec_file" ]; then
    if [ -z "$pending_id" ]; then
      err "[dispatch] single-entry gate: requires --spec-file <abs> or <pending-id>"
      return 1
    fi
    # P0-2: validate pending-id format BEFORE interpolation into path (traversal guard)
    if [[ ! "$pending_id" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
      err "[dispatch] single-entry gate: invalid pending-id format (must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$): $pending_id"
      return 1
    fi
    local candidate="${VNX_DISPATCH_DIR}/pending/${pending_id}/dispatch-spec.json"
    if [ ! -f "$candidate" ]; then
      err "[dispatch] single-entry gate: dispatch-spec.json not found: $candidate"
      return 1
    fi
    spec_file="$candidate"
  fi

  if [ ! -f "$spec_file" ]; then
    err "[dispatch] single-entry gate: spec file not found: $spec_file"
    return 1
  fi

  local dispatch_cli_script="${VNX_HOME}/scripts/lib/dispatch_cli.py"
  if [ ! -f "$dispatch_cli_script" ]; then
    err "[dispatch] single-entry gate: dispatch_cli.py not found: $dispatch_cli_script"
    return 1
  fi

  log "[dispatch] single-entry gate: spec=$spec_file"

  # P1-#7: no trailing colon (avoids CWD on sys.path when PYTHONPATH is unset)
  PYTHONPATH="${VNX_HOME}/scripts/lib${PYTHONPATH:+:${PYTHONPATH}}" \
    python3 "$dispatch_cli_script" --spec-file "$spec_file" ${dry_run_flag:+--dry-run}
  return $?
}

# ── Main command ───────────────────────────────────────────────────────────

cmd_dispatch() {
  # VNX_SINGLE_ENTRY_DISPATCH=1 delegates to the new single-entry Python gate.
  # VNX_DISPATCH_LEGACY=1 short-circuits back to the legacy path (rollback hatch).
  # When neither flag is set: byte-identical legacy behavior below.
  if [[ "${VNX_SINGLE_ENTRY_DISPATCH:-0}" == "1" ]] && \
     [[ "${VNX_DISPATCH_LEGACY:-0}" != "1" ]]; then
    _d_single_entry_dispatch "$@"
    return $?
  fi

  local file=""
  local terminal_override=""
  local model_override="${VNX_MODEL:-sonnet}"
  local adapter_override=""
  local dry_run=0

  if [ "$#" -eq 0 ]; then
    err "[dispatch] No dispatch file specified. Use: vnx dispatch <file.md>"
    return 1
  fi

  # Pre-scan for --help before consuming the file positional
  for _arg in "$@"; do
    case "$_arg" in
      -h|--help)
        cat <<HELP
Usage: vnx dispatch <file.md> [OPTIONS]

Deliver a dispatch .md file to a VNX worker.

Default lane: subscription-preserving ephemeral tmux-spawn (interactive claude).
Burst lane:   paid headless SubprocessAdapter (claude -p), opt-in via --adapter.

Arguments:
  file.md               Path to the dispatch instruction file

Options:
  --terminal <TX>       Override terminal (default: auto-detect from [[TARGET:TX]])
  --model <model>       Override model (default: sonnet)
  --adapter <lane>      Delivery lane: tmux (default) or subprocess (burst).
                        Precedence: --adapter > 'Adapter:' header > VNX_ADAPTER env > tmux
  --dry-run             Show what would happen without dispatching
  -h, --help            Show this help

File search order:
  1. Exact path as given
  2. .vnx-data/dispatches/pending/<file>
  3. .vnx-data/dispatches/pending/<file>.md

Examples:
  vnx dispatch .vnx-data/dispatches/pending/my-dispatch.md
  vnx dispatch my-dispatch.md --terminal T2
  vnx dispatch my-dispatch.md --model opus --dry-run
  vnx dispatch my-dispatch.md --adapter subprocess   # paid burst lane
HELP
        return 0 ;;
    esac
  done

  # First positional arg is the file
  file="$1"
  shift

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --terminal|-t)
        terminal_override="$2"; shift 2 ;;
      --terminal=*)
        terminal_override="${1#*=}"; shift ;;
      --model|-m)
        model_override="$2"; shift 2 ;;
      --model=*)
        model_override="${1#*=}"; shift ;;
      --adapter)
        adapter_override="$2"; shift 2 ;;
      --adapter=*)
        adapter_override="${1#*=}"; shift ;;
      --dry-run|-n)
        dry_run=1; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx dispatch <file.md> [OPTIONS]

Deliver a dispatch .md file to a VNX worker.

Default lane: subscription-preserving ephemeral tmux-spawn (interactive claude).
Burst lane:   paid headless SubprocessAdapter (claude -p), opt-in via --adapter.

Arguments:
  file.md               Path to the dispatch instruction file

Options:
  --terminal <TX>       Override terminal (default: auto-detect from [[TARGET:TX]])
  --model <model>       Override model (default: sonnet)
  --adapter <lane>      Delivery lane: tmux (default) or subprocess (burst).
                        Precedence: --adapter > 'Adapter:' header > VNX_ADAPTER env > tmux
  --dry-run             Show what would happen without dispatching
  -h, --help            Show this help

File search order:
  1. Exact path as given
  2. .vnx-data/dispatches/pending/<file>
  3. .vnx-data/dispatches/pending/<file>.md

Examples:
  vnx dispatch .vnx-data/dispatches/pending/my-dispatch.md
  vnx dispatch my-dispatch.md --terminal T2
  vnx dispatch my-dispatch.md --model opus --dry-run
  vnx dispatch my-dispatch.md --adapter subprocess   # paid burst lane
HELP
        return 0 ;;
      *)
        err "[dispatch] Unknown option: $1"
        return 1 ;;
    esac
  done

  # Resolve file path
  if [ ! -f "$file" ]; then
    local candidate
    # Try pending/ directory
    candidate="${VNX_DISPATCH_DIR}/pending/${file}"
    if [ -f "$candidate" ]; then
      file="$candidate"
    else
      candidate="${VNX_DISPATCH_DIR}/pending/${file}.md"
      if [ -f "$candidate" ]; then
        file="$candidate"
      else
        err "[dispatch] File not found: $file"
        err "[dispatch] Also checked: ${VNX_DISPATCH_DIR}/pending/${file}"
        return 1
      fi
    fi
  fi

  local abs_file
  abs_file="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")"

  # Parse header
  local header_out
  if ! header_out=$(_d_parse_header "$abs_file"); then
    err "[dispatch] Failed to parse dispatch file header"
    return 1
  fi

  local terminal role gate feature adapter_header
  terminal=$(printf '%s' "$header_out" | cut -f1)
  role=$(printf '%s' "$header_out" | cut -f2)
  gate=$(printf '%s' "$header_out" | cut -f3)
  feature=$(printf '%s' "$header_out" | cut -f4)
  adapter_header=$(printf '%s' "$header_out" | cut -f5)

  # Apply overrides
  [ -n "$terminal_override" ] && terminal="$terminal_override"

  if [ -z "$terminal" ]; then
    err "[dispatch] No [[TARGET:TX]] found in dispatch file and no --terminal override"
    return 1
  fi

  # Resolve delivery lane.
  # Precedence: --adapter flag > 'Adapter:' header > VNX_ADAPTER env > default 'tmux'.
  local adapter
  adapter="${adapter_override:-${adapter_header:-${VNX_ADAPTER:-tmux}}}"
  # Normalise to lowercase; accept only known lanes.
  adapter=$(printf '%s' "$adapter" | tr '[:upper:]' '[:lower:]')
  case "$adapter" in
    tmux|subprocess) ;;
    "") adapter="tmux" ;;
    *)
      err "[dispatch] Unknown adapter: '$adapter' (expected 'tmux' or 'subprocess')"
      return 1 ;;
  esac

  # VNX_AUTO_ROUTE=1 overrides the bare default tmux lane so smart routing
  # is honoured. Yields to any explicit adapter choice (--adapter flag,
  # Adapter: header, or VNX_ADAPTER env).
  if [[ "${VNX_AUTO_ROUTE:-0}" == "1" ]] && \
     [[ -z "$adapter_override" ]] && \
     [[ -z "$adapter_header" ]] && \
     [[ -z "${VNX_ADAPTER:-}" ]]; then
    adapter="subprocess"
  fi

  local track
  track=$(_d_resolve_track "$terminal")

  # Derive slug from filename
  local slug
  slug=$(basename "$abs_file" .md | cut -c1-30)

  local dispatch_id
  dispatch_id=$(_d_generate_dispatch_id "$slug" "$track")

  log "[dispatch] File:       $(basename "$abs_file")"
  log "[dispatch] Terminal:   $terminal (Track $track)"
  log "[dispatch] Role:       ${role:-<none>}"
  log "[dispatch] Gate:       ${gate:-<none>}"
  log "[dispatch] Feature:    ${feature:-<none>}"
  log "[dispatch] Model:      $model_override"
  log "[dispatch] Adapter:    $adapter$([ "$adapter" = tmux ] && printf ' (default, subscription)' || printf ' (burst, paid)')"
  log "[dispatch] DispatchID: $dispatch_id"

  if [ "$dry_run" -eq 1 ]; then
    log "[dispatch] DRY RUN — no delivery performed"
    return 0
  fi

  # Terminal availability only applies to the leased subprocess lane.
  # The tmux-spawn lane is leaseless (each dispatch gets a fresh ephemeral
  # session + worktree), so there is no fixed terminal to be "busy".
  if [ "$adapter" = "subprocess" ]; then
    if ! _d_check_terminal_idle "$terminal"; then
      err "[dispatch] Terminal $terminal is busy. Use --dry-run to preview, or wait for completion."
      return 1
    fi
  fi

  # Read instruction from file
  local instruction
  instruction=$(cat "$abs_file")

  # Move file to active/
  local active_path="${VNX_DISPATCH_DIR}/active/$(basename "$abs_file")"
  mkdir -p "${VNX_DISPATCH_DIR}/active"
  cp "$abs_file" "$active_path"

  log "[dispatch] Dispatching to $terminal via $adapter lane..."

  # Resolve the delivery script for the selected lane.
  local dispatch_script
  if [ "$adapter" = "tmux" ]; then
    dispatch_script="$VNX_HOME/scripts/lib/tmux_interactive_dispatch.py"
  else
    dispatch_script="$VNX_HOME/scripts/lib/subprocess_dispatch.py"
  fi
  if [ ! -f "$dispatch_script" ]; then
    err "[dispatch] delivery script not found: $dispatch_script"
    rm -f "$active_path"
    return 1
  fi

  local exit_code=0
  if [ "$adapter" = "tmux" ]; then
    # DEFAULT lane: subscription-preserving ephemeral tmux-spawn.
    # Leaseless — pass the resolved terminal as the worker label for audit parity.
    PYTHONPATH="$VNX_HOME/scripts/lib:${PYTHONPATH:-}" \
    python3 "$dispatch_script" \
      --dispatch-id "$dispatch_id" \
      --instruction "$instruction" \
      --model "$model_override" \
      --worker-label "$terminal" \
      ${role:+--role "$role"} \
      || exit_code=$?
  else
    # OPT-IN burst lane: paid headless SubprocessAdapter.
    local _ar_flag=()
    [[ "${VNX_AUTO_ROUTE:-0}" == "1" ]] && _ar_flag=(--auto-route)
    PYTHONPATH="$VNX_HOME/scripts/lib:${PYTHONPATH:-}" \
    python3 "$dispatch_script" \
      --terminal-id "$terminal" \
      --dispatch-id "$dispatch_id" \
      --instruction "$instruction" \
      --model "$model_override" \
      ${role:+--role "$role"} \
      ${_ar_flag[@]+"${_ar_flag[@]}"} \
      || exit_code=$?
  fi

  if [ "$exit_code" -eq 0 ]; then
    # Move to completed/
    local completed_path="${VNX_DISPATCH_DIR}/completed/$(basename "$abs_file")"
    mkdir -p "${VNX_DISPATCH_DIR}/completed"
    mv "$active_path" "$completed_path" 2>/dev/null || true
    log "[dispatch] Done — dispatch $dispatch_id completed (receipt: success)"
    log "[dispatch] Archived to: completed/$(basename "$abs_file")"
  else
    err "[dispatch] Dispatch $dispatch_id failed (exit $exit_code)"
    # Leave in active/ for inspection; copy of original still in pending/
    return "$exit_code"
  fi
}
