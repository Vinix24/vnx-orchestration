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

# Single-source routing predicate (vnx_single_entry_enabled). Sourced RELATIVE to this file
# (not VNX_HOME) so it resolves even under a test/stub VNX_HOME; the helper itself falls back
# to its own dir for dispatch_flags.py.
# shellcheck source=/dev/null
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)/vnx_dispatch_flags.sh"

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

# _d_valid_dispatch_id — the ONE id-safety predicate (P0-2 traversal guard).
# Returns 0 iff $1 is a safe dispatch/pending id: no '/', no '..', matches the id regex.
# Centralized so _d_is_staged_form, _d_single_entry_dispatch, and the staged-bundle hint
# never drift on the guard (ADR-025 / door-flip D1).
_d_valid_dispatch_id() {
  local id="${1:-}"
  [[ "$id" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || return 1
  # The regex already forbids '/'; reject any '..' segment explicitly (defense in depth).
  case "$id" in *..*) return 1 ;; esac
  return 0
}

# _d_is_staged_form — decide whether the args address the single-entry DOOR (staged forms)
# vs the LEGACY raw-file path. Consulted ONLY when the door is enabled. Door (return 0):
# --spec-file / --force-release-lock / -h/--help (door operations), no positional (door owns
# the "requires" error), OR a first positional that resolves to an existing pending bundle.
# Legacy (return 1): a path (contains '/' or ends '.md' without a bundle), or a bare slug with
# no staged bundle. Bundle-existence is checked BEFORE the '.md' heuristic so a pending-id that
# legally ends in '.md' still routes to the door. set -u-safe: every $next peek is guarded.
_d_is_staged_form() {
  local a
  # 1. door-only flags anywhere → door.
  for a in "$@"; do
    case "$a" in
      --spec-file|--spec-file=*|--force-release-lock|--force-release-lock=*|-h|--help)
        return 0 ;;
    esac
  done
  # 2. first non-flag token, skipping value-taking flags (the COMPLETE legacy set:
  #    --terminal/-t, --model/-m, --adapter). '--' ends options; the next token is positional.
  local first="" expect_val=0 seen_ddash=0
  for a in "$@"; do
    if [ "$seen_ddash" -eq 1 ]; then first="$a"; break; fi
    if [ "$expect_val" -eq 1 ]; then expect_val=0; continue; fi
    case "$a" in
      --) seen_ddash=1; continue ;;
      --terminal|-t|--model|-m|--adapter) expect_val=1; continue ;;
      --terminal=*|--model=*|--adapter=*|--dry-run|-n) continue ;;
      --*|-*) continue ;;
      *) first="$a"; break ;;
    esac
  done
  # 3. no positional (flags only / value-flag-without-value at end) → door owns the messaging.
  [ -n "$first" ] || return 0
  # 4. classify the token. A path is unambiguously legacy (ids never contain '/').
  case "$first" in */*) return 1 ;; esac
  # Bundle wins over the .md heuristic (a pending-id may legally end in .md).
  if _d_valid_dispatch_id "$first" \
     && [ -f "${VNX_DISPATCH_DIR}/pending/${first}/dispatch-spec.json" ]; then
    return 0
  fi
  return 1
}

# ── Single-entry dispatch (VNX_SINGLE_ENTRY_DISPATCH=1) ───────────────────

_d_single_entry_dispatch() {
  # Accepts --spec-file <abs> or <pending-id> (resolved to its bundle's dispatch-spec.json).
  # VNX_DISPATCH_LEGACY=1 is checked by the caller — not re-checked here.
  local spec_file=""
  local dry_run_flag=""
  local pending_id=""
  local force_release_class=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --spec-file)
        # P1 (PR-4c): guard $2 so a trailing `--spec-file` with no value emits a
        # clean gate error instead of aborting the shell under `set -u`.
        if [ -z "${2:-}" ]; then
          err "[dispatch] single-entry gate: --spec-file requires a path argument"
          return 1
        fi
        spec_file="$2"; shift 2 ;;
      --spec-file=*)
        spec_file="${1#*=}"; shift ;;
      --dry-run|-n)
        dry_run_flag="--dry-run"; shift ;;
      --force-release-lock)
        # Optional positional after the flag: next arg without leading -- is class name.
        if [ -n "${2:-}" ] && [[ "${2:-}" != --* ]]; then
          force_release_class="$2"; shift
        else
          force_release_class="claude-tmux"
        fi
        shift ;;
      --force-release-lock=*)
        force_release_class="${1#*=}"; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx dispatch [--spec-file <abs>] [--dry-run]
       vnx dispatch <pending-id> [--dry-run]
       vnx dispatch --force-release-lock [<class>]

Single-entry gate (the default lane). Staged forms route through the door:
  --spec-file <abs>            Absolute path to dispatch-spec.json
  <pending-id>                 Dispatch ID resolved to dispatches/pending/<id>/dispatch-spec.json
  --dry-run                    Print plan + fingerprint; spawn nothing
  (The raw 'vnx dispatch <file.md>' form still works on the legacy lane but is DEPRECATED —
   removed in 1.x per ADR-025. Stage it to a pending-id.)
  --force-release-lock [CLASS] Release stale serial lock for CLASS (default: claude-tmux).
                               Prints prior holder pid+dispatch_id; removes lock file.
                               Does NOT kill the holder — use the printed pid if needed.
  VNX_DISPATCH_LEGACY=1        Force legacy path even when gate is on

Headless (api_metered) lane: set allow_headless=true + headless_reason in dispatch-spec.json.
  The --adapter subprocess / VNX_ADAPTER / VNX_AUTO_ROUTE flags are LEGACY-ONLY
  (cmd_dispatch path when VNX_SINGLE_ENTRY_DISPATCH is unset) and have no effect here.
HELP
        return 0 ;;
      --terminal|-t|--terminal=*|--model|-m|--model=*|--adapter|--adapter=*)
        # These are legacy raw-file overrides; a staged <pending-id>/--spec-file carries
        # terminal/model/adapter in its spec, so the combination is invalid. Give a clear,
        # actionable error instead of the generic unknown-flag reject.
        err "[dispatch] single-entry gate: '${1%%=*}' is a legacy raw-file override; it is not valid with a staged <pending-id> (the spec already defines terminal/model/adapter). Drop the flag, or use the raw form (vnx dispatch <file.md>)."
        return 1 ;;
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

  # --force-release-lock: operator escape, independent of spec-file.
  if [ -n "$force_release_class" ]; then
    local dispatch_cli_script="${VNX_HOME}/scripts/lib/dispatch_cli.py"
    if [ ! -f "$dispatch_cli_script" ]; then
      err "[dispatch] single-entry gate: dispatch_cli.py not found: $dispatch_cli_script"
      return 1
    fi
    log "[dispatch] force-release-lock: class=$force_release_class"
    PYTHONPATH="${VNX_HOME}/scripts/lib${PYTHONPATH:+:${PYTHONPATH}}" \
      python3 "$dispatch_cli_script" --force-release-lock "$force_release_class"
    return $?
  fi

  if [ -z "$spec_file" ]; then
    if [ -z "$pending_id" ]; then
      err "[dispatch] single-entry gate: requires --spec-file <abs> or <pending-id>"
      return 1
    fi
    # P0-2: validate pending-id format BEFORE interpolation into path (traversal guard).
    # Uses the centralized _d_valid_dispatch_id predicate (shared with _d_is_staged_form + the hint).
    if ! _d_valid_dispatch_id "$pending_id"; then
      err "[dispatch] single-entry gate: invalid pending-id format (must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$, no '..'): $pending_id"
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
  # Routing (flag contract — also documented in --help). The door owns STAGED forms only;
  # raw `vnx dispatch <file.md>` stays on the legacy lane (deprecated, removed in 1.x per ADR-025):
  #   door enabled  + staged form (--spec-file / <pending-id> with a bundle / --force-release-lock)
  #       -> DOOR (_d_single_entry_dispatch)
  #   door enabled  + raw form (a path, *.md, or a bare slug without a bundle)
  #       -> LEGACY lane + a one-time DEPRECATED warning (stderr)
  #   door disabled (VNX_DISPATCH_LEGACY=1, or VNX_SINGLE_ENTRY_DISPATCH=0/unset pre-flip)
  #       -> LEGACY lane, byte-identical, no warning
  # The rollback hatch (VNX_DISPATCH_LEGACY=1) always wins (single-source helper).
  # _door_on is captured ONCE here (not re-evaluated) so the warning fires iff the door is the
  # reason we fell through to legacy (door on + raw), never under explicit legacy/rollback.
  local _door_on=0
  vnx_single_entry_enabled && _door_on=1
  if [ "$_door_on" = 1 ] && _d_is_staged_form "$@"; then
    _d_single_entry_dispatch "$@"
    return $?
  fi
  # Past here = the legacy raw-file lane. When the door is the default yet we got a raw form,
  # warn once (stderr, no ERROR: prefix) — seeds the ADR-025 removal. Fires BEFORE file
  # resolution so it shows regardless of a later not-found / --dry-run outcome.
  if [ "$_door_on" = 1 ]; then
    printf '%s\n' "[dispatch] DEPRECATED: raw-file dispatch (vnx dispatch <file.md>) — stage to a pending-id (vnx dispatch <pending-id>) instead. The raw form is scheduled for removal per ADR-025." >&2
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

Canonical (single-entry door):
  vnx dispatch <pending-id>                         # a promoted dispatch bundle

Raw-file form (DEPRECATED — removed in 1.x per ADR-025; the file must come FIRST):
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

Canonical (single-entry door):
  vnx dispatch <pending-id>                         # a promoted dispatch bundle

Raw-file form (DEPRECATED — removed in 1.x per ADR-025; the file must come FIRST):
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
        # Staged-bundle hint: if the missing arg is a safe id that resolves to a promoted
        # bundle, the caller likely meant the door form but the door is off (rollback/legacy).
        # _d_valid_dispatch_id guards the path-join (no '/', no '..') before the -f check.
        if _d_valid_dispatch_id "$file" \
           && [ -f "${VNX_DISPATCH_DIR}/pending/${file}/dispatch-spec.json" ]; then
          err "[dispatch] note: '${file}' is a staged dispatch bundle; the single-entry door is off (VNX_DISPATCH_LEGACY=1 or VNX_SINGLE_ENTRY_DISPATCH=0). Enable the door, or dispatch the raw .md."
        fi
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
  # LEGACY PATH ONLY — has no effect when VNX_SINGLE_ENTRY_DISPATCH=1.
  # For headless claude via the door, set allow_headless=true in dispatch-spec.json.
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
  # Door-flip A / Option X1 (ADR-024): this legacy lane is reached ONLY for raw-file forms.
  # Staged forms route through the door at cmd_dispatch's intercept and never get here, so this
  # delivery is unconditionally the legacy tmux/subprocess lane — preserving the raw form's full
  # lane precedence (--adapter > Adapter: > VNX_ADAPTER > VNX_AUTO_ROUTE). The old
  # `if vnx_single_entry_enabled -> dispatch_bridge.py` branch was removed: routing raw input
  # through the bridge would silently drop --adapter (bridge defaults claude -> tmux), the
  # regression A2 avoids. The bridge is still the door's delivery for STAGED callers elsewhere.
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
