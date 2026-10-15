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

_g_pr_head_branch() {
  # Resolve the PR's head ref from GitHub, not from the local checkout. T0
  # routinely runs `vnx gate` from main; the local branch is then 'main' while
  # the branch under review is the PR's headRefName. Passing the local branch
  # to the gate worktree checks out the wrong tree for the diff being reviewed
  # (OI-904). The diff itself is fetched via `gh pr diff`, but the gate agent's
  # own file reads (sed/rg/cat) resolve against the worktree cwd, which must
  # match the PR branch.
  local pr="$1"
  [ -n "$pr" ] || return 1
  local head_ref
  head_ref="$(gh pr view "$pr" --json headRefName --jq .headRefName 2>/dev/null || true)"
  if [ -n "$head_ref" ]; then
    printf '%s' "$head_ref"
    return 0
  fi
  return 1
}

_g_required_gates() {
  # Read governance_enforcement.yaml and return comma-separated gate names
  # that are level >= 2 (soft_mandatory or hard_mandatory).
  local config="${VNX_HOME}/.vnx/governance_enforcement.yaml"
  # Also try canonical .vnx/ relative to project root
  [ -f "$config" ] || config="${PROJECT_ROOT}/.vnx/governance_enforcement.yaml"
  [ -f "$config" ] || config="${VNX_HOME}/governance_enforcement.yaml"

  # The default review stack: the two standing subscription seats
  # (config_registry VNX_DEFAULT_REVIEW_STACK; test_gate_required_gates pins the
  # two equal). Used when no config is found or it names no required gate.
  local default_stack='codex_gate,kimi_gate'

  if [ ! -f "$config" ]; then
    printf '%s' "$default_stack"
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

# Map enforcement check names to gate names used by review_gate_manager.
# The manager builds its stack with 'ci_gate' (scripts/review_gate_manager.py);
# 'ci' is not a name it knows, so a ci_green_required requirement would
# resolve to an orphan gate that the manager books as blocked and never
# enforces (OI-1265).
gate_map = {
    'codex_gate_required': 'codex_gate',
    'kimi_gate_required': 'kimi_gate',
    'ci_green_required': 'ci_gate',
}
result = []
for g in gates:
    mapped = gate_map.get(g)
    if mapped:
        result.append(mapped)

print(','.join(result) if result else '$default_stack')
"
}

_g_normalize_gate() {
  # Short names to the gate names review_gate_manager knows: codex -> codex_gate,
  # ci -> ci_gate (OI-1265), and the kimi/glm/deepseek reviewers by the same rule.
  case "$1" in
    codex)    printf 'codex_gate' ;;
    ci)       printf 'ci_gate' ;;
    kimi)     printf 'kimi_gate' ;;
    glm)      printf 'glm_gate' ;;
    deepseek) printf 'deepseek_gate' ;;
    *)        printf '%s' "$1" ;;
  esac
}

_g_gate_known() {
  # An `--only` name must be a gate gate_recorder registers (GATE_PROVIDERS),
  # checked before the provider switch is asked. An unregistered name has no
  # provider, so it is never a switch refusal: it is refused by name with the
  # registered names, exit 2, and writes no GATE-9 line
  # (gate_recorder.gate_switch_refusal answers None for it too). Before this,
  # `providers check` refused a name its family map does not know with exit 4
  # and this command reported a typo as "switched off" with a
  # provider_switched_off line (OI-2070); a name the map does know (`gemini`,
  # `kimi_cli`) passed the switch and reached the gate manager unregistered.
  # A check that cannot run refuses fail-closed with exit 1.
  local gate="$1"
  local pythonpath="$VNX_HOME/scripts/lib:${VNX_HOME}/scripts:${PYTHONPATH:-}"
  local names_rc=0
  local names_out
  names_out=$(PYTHONPATH="$pythonpath" python3 -c '
import sys

from gate_recorder import GATE_PROVIDERS, resolve_gate_provider

if resolve_gate_provider(sys.argv[1]) is not None:
    sys.exit(0)
print(", ".join(sorted(GATE_PROVIDERS)))
sys.exit(3)
' "$gate") || names_rc=$?
  if [ "$names_rc" -eq 0 ]; then
    return 0
  fi
  if [ "$names_rc" -eq 3 ]; then
    err "[gate] unknown gate '$gate': not a registered gate; nothing was requested or launched"
    err "[gate] Registered gates: $names_out (short names: codex, ci, kimi, glm, deepseek)"
    return 2
  fi
  err "[gate] Could not check the gate name '$gate' (exit $names_rc); refusing"
  return 1
}

_g_record_switch_refusal() {
  # A switched-off gate is a gate decision, so it lands in a canonical ledger
  # before `--only` returns (ADR-005): one GATE-9 skip-rationale line in
  # $VNX_STATE_DIR/gate_execution_audit.ndjson, through
  # gate_recorder.write_skip_rationale, the writer every other not_executable
  # gate refusal already uses (the provider_switched_off bookings of the seat
  # walk and the runner included). reason_detail is the decision line
  # `providers check` printed, which names the gate, family, source, scope and
  # consumer. No commit_sha: the refusal comes before the PR is looked up, so
  # nothing here calls gh.
  local gate="$1" pr="$2" check_out="$3"
  local pythonpath="$VNX_HOME/scripts/lib:${VNX_HOME}/scripts:${PYTHONPATH:-}"
  PYTHONPATH="$pythonpath" python3 -c '
import sys
from pathlib import Path

from gate_recorder import PROVIDER_SWITCHED_OFF_REASON, write_skip_rationale

state_dir, gate, pr_id, check_out = sys.argv[1:5]
if not state_dir:
    sys.exit("VNX_STATE_DIR is not set")
lines = [line.strip() for line in check_out.splitlines() if line.strip()]
detail = next((line for line in lines if line.startswith(PROVIDER_SWITCHED_OFF_REASON + ":")), " ".join(lines))
write_skip_rationale(
    Path(state_dir), gate, pr_id=pr_id, reason=PROVIDER_SWITCHED_OFF_REASON, reason_detail=detail,
)
' "${VNX_STATE_DIR:-}" "$gate" "$pr" "$check_out"
}

_g_switch_allows() {
  # The operator's provider switch decides before anything is requested
  # (provider-schakelaar C). Runs the module behind `vnx providers check`:
  # exit 0 allowed, 4 switched off. A refusal is recorded first
  # (_g_record_switch_refusal); a refusal that cannot be recorded returns 1, so
  # an unrecorded decision never reads as an ordinary switch refusal. Any other
  # check exit means the switch could not be consulted, and that refuses too:
  # fail-closed, with no switch decision to record.
  local gate="$1" pr="$2"
  local pythonpath="$VNX_HOME/scripts/lib:${VNX_HOME}/scripts:${PYTHONPATH:-}"
  local check_rc=0
  local check_out
  check_out=$(PYTHONPATH="$pythonpath" python3 "$VNX_HOME/scripts/providers_cli.py" \
    check "$gate" --consumer "vnx gate --only" 2>&1) || check_rc=$?
  if [ "$check_rc" -eq 0 ]; then
    return 0
  fi
  if [ "$check_rc" -eq 4 ]; then
    local record_rc=0
    local record_out
    record_out=$(_g_record_switch_refusal "$gate" "$pr" "$check_out" 2>&1) || record_rc=$?
    err "[gate] '$gate' is switched off by the provider switch; nothing was requested or launched"
    err "[gate] $check_out"
    err "[gate] Re-enable with: vnx providers on <family>"
    if [ "$record_rc" -ne 0 ]; then
      err "[gate] Could not record the refusal in the gate audit ledger (exit $record_rc): $record_out"
      return 1
    fi
    return 4
  fi
  err "[gate] Could not consult the provider switch for '$gate' (exit $check_rc); refusing: $check_out"
  return 1
}

_g_print_seat_lines() {
  # Read "TAG<TAB>line" pairs (gate_seat_line.py's stdout) and print each with
  # log()/err() styling, keyed off the tag rather than re-parsing the line
  # text for "PASS" (OI-1888).
  local tag rest
  while IFS=$'\t' read -r tag rest; do
    [ -n "$tag" ] || continue
    if [ "$tag" = "PASS" ]; then
      log "[gate] $rest"
    else
      err "[gate] $rest"
    fi
  done
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
sys.path.insert(0, '$VNX_HOME/scripts/lib')
from gate_status import has_complete_evidence, is_pass

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
            'detail': d,
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
    d = r['detail']
    status = r['status']
    # OI-1178: a gate that never ran must never read as PASS. Classify the
    # canonical statuses explicitly and fall back to is_pass() + evidence so a
    # hand-authored or drifting record cannot masquerade as a decided pass.
    if status == 'unavailable':
        marker = 'UNAVAIL'
    elif status in ('not_executable', 'not_configured'):
        marker = 'NOEXEC'
    elif status in ('failed', 'fail', 'blocked'):
        marker = 'FAIL'
    else:
        passed, _ = is_pass(d)
        marker = 'PASS' if (passed and has_complete_evidence(d)) else status[:8]
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
  --only <gate>         Run a specific gate only (e.g. codex, kimi, ci)
  --status              Show current gate results without running
  --mode <mode>         Gate mode: per_pr or final (default: final)
  --risk-class <class>  Risk class: low, medium, high (default: medium)
  -h, --help            Show this help

Gate names:
  codex_gate            Codex static analysis gate (subscription)
  kimi_gate             Kimi review gate (subscription)
  glm_gate              GLM review gate (API credit, fallback when codex and kimi are unavailable)
  deepseek_gate         DeepSeek review gate (API credit, fallback when codex and kimi are unavailable)
  ci_gate               CI green check

Exit codes:
  0 gates passed, 1 a gate failed or an error, 2 unknown --only gate name,
  4 the --only gate is switched off by the provider switch

Examples:
  vnx gate 221
  vnx gate 221 --only codex
  vnx gate 221 --only kimi
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

  # --only <gate>: an unknown gate name (exit 2) and a switched-off provider
  # (exit 4) are refused here, in that order, before the PR branch is resolved
  # and before anything is requested or launched.
  if [ -n "$only_gate" ]; then
    only_gate="$(_g_normalize_gate "$only_gate")"
    local name_rc=0
    _g_gate_known "$only_gate" || name_rc=$?
    if [ "$name_rc" -ne 0 ]; then
      return "$name_rc"
    fi
    local switch_rc=0
    _g_switch_allows "$only_gate" "$pr_number" || switch_rc=$?
    if [ "$switch_rc" -ne 0 ]; then
      return "$switch_rc"
    fi
  fi

  # Auto-detect branch. The PR's head ref (gh pr view headRefName) is the only
  # acceptable answer: the gate worktree must match the diff under review, and
  # T0 routinely runs `vnx gate` from main (OI-904). A PR whose head ref cannot
  # be resolved is a hard error (OI-1268) — falling back to the local checkout
  # branch would gate a different tree than the one being reviewed and stamp
  # PASS evidence on it. A gate that does not know what it reviews reviews
  # nothing.
  local branch
  if [ -n "$pr_number" ]; then
    if branch="$(_g_pr_head_branch "$pr_number")"; then
      log "[gate] Resolved PR $pr_number head branch: $branch"
    else
      err "[gate] Could not resolve PR $pr_number head branch via gh pr view; refusing to gate on the local branch"
      return 1
    fi
  else
    branch=$(_g_current_branch)
    if [ -z "$branch" ]; then
      err "[gate] Could not determine current git branch"
      return 1
    fi
  fi

  local gate_script="$VNX_HOME/scripts/review_gate_manager.py"
  if [ ! -f "$gate_script" ]; then
    err "[gate] review_gate_manager.py not found: $gate_script"
    return 1
  fi

  # OI-915: extract dispatch_id from the branch name when it follows the
  # dispatch/<id> convention (the gate knows the branch name — use it as the
  # canonical source, do not invent a second way to derive the same value).
  local dispatch_id=""
  if [[ "$branch" == dispatch/* ]]; then
    dispatch_id="${branch#dispatch/}"
    log "[gate] Resolved dispatch_id from branch: $dispatch_id"
  elif [ -n "${VNX_CURRENT_DISPATCH_ID:-}" ]; then
    dispatch_id="$VNX_CURRENT_DISPATCH_ID"
    log "[gate] Resolved dispatch_id from env: $dispatch_id"
  fi

  local pythonpath="$VNX_HOME/scripts/lib:${VNX_HOME}/scripts:${PYTHONPATH:-}"

  if [ -n "$only_gate" ]; then
    # Run a specific gate (its name was normalized and cleared by the provider
    # switch above, before the branch was resolved).
    log "[gate] Running gate '$only_gate' for PR $pr_number (branch: $branch)"

    local exit_code=0
    local result
    result=$(PYTHONPATH="$pythonpath" python3 "$gate_script" \
      request-and-execute \
      --pr "$pr_number" \
      --branch "$branch" \
      --review-stack "$only_gate" \
      --risk-class "$risk_class" \
      --mode "$mode" \
      --dispatch-id "$dispatch_id" 2>&1) || exit_code=$?

    printf '%s\n' "$result"

    # The requested-and-execute exit code reflects the SUCCESSOR's verdict
    # correctly when a takeover happened, but the seat name it's reported
    # under does not — resolve and print who actually read (OI-1888).
    local seat_exit=0
    local seat_output
    seat_output=$(printf '%s\n' "$result" | PYTHONPATH="$pythonpath" python3 \
      "$VNX_HOME/scripts/lib/gate_seat_line.py" \
      --pr "$pr_number" --state-dir "$VNX_STATE_DIR" --seats "$only_gate") || seat_exit=$?
    printf '%s\n' "$seat_output" | _g_print_seat_lines

    if [ "$exit_code" -ne 0 ] || [ "$seat_exit" -ne 0 ]; then
      exit_code=1
    fi
    return "$exit_code"

  else
    # Run all required gates
    local required_gates
    required_gates=$(_g_required_gates)

    log "[gate] Required gates: $required_gates"
    log "[gate] Running all required gates for PR $pr_number (branch: $branch)"

    local exit_code=0
    local result
    result=$(PYTHONPATH="$pythonpath" python3 "$gate_script" \
      request-and-execute \
      --pr "$pr_number" \
      --branch "$branch" \
      --review-stack "$required_gates" \
      --risk-class "$risk_class" \
      --mode "$mode" \
      --dispatch-id "$dispatch_id" 2>&1) || exit_code=$?

    printf '%s\n' "$result"

    printf '\n'
    log "[gate] Results for PR $pr_number:"
    _g_show_results "$pr_number"

    # One line per requested seat, naming whoever actually read it — a
    # takeover successor's exit code is correct, but $required_gates alone
    # would still print the seat it replaced as if it had read (OI-1888).
    printf '\n'
    local seat_exit=0
    local seat_output
    seat_output=$(printf '%s\n' "$result" | PYTHONPATH="$pythonpath" python3 \
      "$VNX_HOME/scripts/lib/gate_seat_line.py" \
      --pr "$pr_number" --state-dir "$VNX_STATE_DIR" --seats "$required_gates") || seat_exit=$?
    printf '%s\n' "$seat_output" | _g_print_seat_lines

    if [ "$exit_code" -ne 0 ] || [ "$seat_exit" -ne 0 ]; then
      exit_code=1
    fi

    if [ "$exit_code" -eq 0 ]; then
      log "[gate] All gates PASS for PR $pr_number"
    else
      err "[gate] One or more gates did not PASS for PR $pr_number (exit $exit_code)"
    fi
    return "$exit_code"
  fi
}
