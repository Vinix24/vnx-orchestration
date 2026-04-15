#!/usr/bin/env bash
# VNX Command: gate
# Sourced by bin/vnx — runs review gates against a PR number.
#
# All variables from bin/vnx (VNX_HOME, VNX_DATA_DIR, VNX_STATE_DIR,
# VNX_DISPATCH_DIR, log, err) are available when this runs.

# ── Helpers ────────────────────────────────────────────────────────────────

_g_current_branch() {
  git -C "${PROJECT_ROOT:-$(pwd)}" rev-parse --abbrev-ref HEAD 2>/dev/null || printf ''
}

_g_required_gates() {
  # Read governance_enforcement.yaml and return comma-separated gate names
  # that are level >= 2 (soft_mandatory or hard_mandatory).
  local config="${VNX_HOME}/.vnx/governance_enforcement.yaml"
  # Also try canonical .vnx/ relative to project root
  [ -f "$config" ] || config="${PROJECT_ROOT}/.vnx/governance_enforcement.yaml"
  [ -f "$config" ] || config="${VNX_HOME}/governance_enforcement.yaml"

  if [ ! -f "$config" ]; then
    # Fallback to default stack when no config found
    printf 'codex_gate,gemini_review'
    return
  fi

  python3 -c "
import sys
config_path = '$config'
gates = []
try:
    # Parse YAML manually (no PyYAML dependency required)
    in_checks = False
    current_check = None
    with open(config_path) as f:
        for line in f:
            stripped = line.rstrip()
            if stripped == 'checks:':
                in_checks = True
                continue
            if in_checks:
                # Top-level key under checks (2-space indent)
                import re
                m = re.match(r'^  (\w+):', stripped)
                if m:
                    current_check = m.group(1)
                    continue
                # Level line (4-space indent)
                m = re.match(r'^    level:\s*(\d+)', stripped)
                if m and current_check:
                    level = int(m.group(1))
                    if level >= 2:
                        gates.append(current_check)
                # Stop at next top-level section
                if stripped and not stripped.startswith(' ') and stripped != 'checks:':
                    in_checks = False
except Exception as e:
    print(f'warn: {e}', file=sys.stderr)

# Map enforcement check names to gate names used by review_gate_manager
gate_map = {
    'codex_gate_required': 'codex_gate',
    'gemini_review_required': 'gemini_review',
    'ci_green_required': 'ci',
}
result = []
for g in gates:
    mapped = gate_map.get(g)
    if mapped:
        result.append(mapped)

print(','.join(result) if result else 'codex_gate,gemini_review')
"
}

_g_show_results() {
  local pr="$1"
  local gate_dir="${VNX_STATE_DIR}/review_gates/results"

  if [ ! -d "$gate_dir" ]; then
    log "[gate] No gate results directory: $gate_dir"
    return 0
  fi

  python3 -c "
import json, os, glob, sys

gate_dir = '$gate_dir'
pr = '$pr'
results = []
for f in glob.glob(os.path.join(gate_dir, '*.json')):
    try:
        d = json.load(open(f))
        pr_num = str(d.get('pr_number', d.get('pr_id', '')))
        if pr and pr not in (pr_num, 'all'):
            continue
        results.append({
            'gate': d.get('gate', '?'),
            'pr': pr_num,
            'status': d.get('status', '?'),
            'blocking': len(d.get('blocking_findings', [])),
            'summary': d.get('summary', '')[:60],
            'recorded_at': d.get('recorded_at', '?')[:19],
        })
    except Exception:
        pass

if not results:
    print(f'  (no gate results found for PR {pr})')
    sys.exit(0)

results.sort(key=lambda r: r['recorded_at'])
fmt = '  {:<20} {:<8} {:<10} {:<8} {}'
print(fmt.format('Gate', 'PR', 'Status', 'Blocking', 'Recorded'))
print('  ' + '-'*68)
for r in results:
    status = r['status']
    marker = 'PASS' if status == 'completed' else 'FAIL' if status == 'failed' else status[:8]
    print(fmt.format(str(r['gate'])[:19], str(r['pr'])[:7], marker, str(r['blocking']), r['recorded_at']))
"
}

# ── Main command ───────────────────────────────────────────────────────────

cmd_gate() {
  local pr_number=""
  local only_gate=""
  local show_status_only=0
  local mode="final"
  local risk_class="medium"

  if [ "$#" -eq 0 ]; then
    err "[gate] PR number required. Use: vnx gate <pr-number>"
    return 1
  fi

  # First positional arg: PR number (if numeric)
  if [[ "$1" =~ ^[0-9]+$ ]]; then
    pr_number="$1"
    shift
  fi

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --only|-g)
        only_gate="$2"; shift 2 ;;
      --only=*)
        only_gate="${1#*=}"; shift ;;
      --status|-s)
        show_status_only=1; shift ;;
      --mode)
        mode="$2"; shift 2 ;;
      --mode=*)
        mode="${1#*=}"; shift ;;
      --risk-class)
        risk_class="$2"; shift 2 ;;
      --risk-class=*)
        risk_class="${1#*=}"; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx gate <pr-number> [OPTIONS]

Run governance review gates for a pull request.

Arguments:
  pr-number             GitHub PR number to gate

Options:
  --only <gate>         Run a specific gate only (e.g. codex, gemini)
  --status              Show current gate results without running
  --mode <mode>         Gate mode: per_pr or final (default: final)
  --risk-class <class>  Risk class: low, medium, high (default: medium)
  -h, --help            Show this help

Gate names:
  codex_gate            Codex static analysis gate
  gemini_review         Gemini code review gate
  ci                    CI green check

Examples:
  vnx gate 221
  vnx gate 221 --only codex
  vnx gate 221 --only gemini
  vnx gate 221 --status
HELP
        return 0 ;;
      *)
        # Accept PR number as second form: vnx gate --pr 221
        if [ "$1" = "--pr" ] && [ -n "$2" ]; then
          pr_number="$2"; shift 2
        else
          err "[gate] Unknown option: $1"
          return 1
        fi ;;
    esac
  done

  if [ -z "$pr_number" ]; then
    err "[gate] PR number required. Use: vnx gate <pr-number>"
    return 1
  fi

  # Status-only: show existing results
  if [ "$show_status_only" -eq 1 ]; then
    log "[gate] Gate results for PR $pr_number:"
    _g_show_results "$pr_number"
    return 0
  fi

  # Auto-detect branch
  local branch
  branch=$(_g_current_branch)
  if [ -z "$branch" ]; then
    err "[gate] Could not determine current git branch"
    return 1
  fi

  local gate_script="$VNX_HOME/scripts/review_gate_manager.py"
  if [ ! -f "$gate_script" ]; then
    err "[gate] review_gate_manager.py not found: $gate_script"
    return 1
  fi

  local pythonpath="$VNX_HOME/scripts/lib:${VNX_HOME}/scripts:${PYTHONPATH:-}"

  if [ -n "$only_gate" ]; then
    # Run a specific gate
    # Normalize short names: codex -> codex_gate, gemini -> gemini_review
    case "$only_gate" in
      codex)  only_gate="codex_gate" ;;
      gemini) only_gate="gemini_review" ;;
    esac

    log "[gate] Running gate '$only_gate' for PR $pr_number (branch: $branch)"

    PYTHONPATH="$pythonpath" python3 "$gate_script" \
      request-and-execute \
      --pr "$pr_number" \
      --branch "$branch" \
      --review-stack "$only_gate" \
      --risk-class "$risk_class" \
      --mode "$mode"
    local exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
      log "[gate] Gate '$only_gate': PASS"
    else
      err "[gate] Gate '$only_gate': FAIL (exit $exit_code)"
    fi
    return "$exit_code"

  else
    # Run all required gates
    local required_gates
    required_gates=$(_g_required_gates)

    log "[gate] Required gates: $required_gates"
    log "[gate] Running all required gates for PR $pr_number (branch: $branch)"

    PYTHONPATH="$pythonpath" python3 "$gate_script" \
      request-and-execute \
      --pr "$pr_number" \
      --branch "$branch" \
      --review-stack "$required_gates" \
      --risk-class "$risk_class" \
      --mode "$mode"
    local exit_code=$?

    printf '\n'
    log "[gate] Results for PR $pr_number:"
    _g_show_results "$pr_number"

    if [ "$exit_code" -eq 0 ]; then
      log "[gate] All gates PASS for PR $pr_number"
    else
      err "[gate] One or more gates FAILED for PR $pr_number (exit $exit_code)"
    fi
    return "$exit_code"
  fi
}
