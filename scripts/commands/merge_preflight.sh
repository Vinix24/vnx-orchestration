#!/usr/bin/env bash
# VNX Command: merge-preflight
# Governance-aware pre-merge check for feature worktrees.
#
# Queries runtime state (open items, PR queue, git cleanliness, processes)
# and returns a clear GO or NO-GO verdict.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, PROJECT_ROOT, VNX_HOME, etc.)
# are available when this runs.

cmd_merge_preflight() {
  local name=""
  local json_output=false

  while [ $# -gt 0 ]; do
    case "$1" in
      --json)
        json_output=true; shift ;;
      -h|--help)
        cat <<HELP
Usage: vnx merge-preflight <name>

Runs governance checks against a feature worktree and returns a GO or NO-GO
verdict based on runtime state.

Arguments:
  <name>         Worktree name (matches directory: \${PROJECT_ROOT}-wt-<name>)

Options:
  --json         Output verdict as JSON (for scripting)
  -h, --help     Show this help

Checks performed:
  1. Git cleanliness (uncommitted changes, unpushed commits)
  2. Open items (blockers and warnings from open_items_manager)
  3. PR queue status (incomplete PRs associated with this worktree)
  4. Active processes (VNX orchestration still running in worktree)
HELP
        return 0
        ;;
      -*)
        err "[merge-preflight] Unknown option: $1"
        return 1
        ;;
      *)
        if [ -z "$name" ]; then
          name="$1"
        else
          err "[merge-preflight] Unexpected argument: $1"
          return 1
        fi
        shift
        ;;
    esac
  done

  if [ -z "$name" ]; then
    err "[merge-preflight] Name is required. Usage: vnx merge-preflight <name>"
    return 1
  fi

  local wt_dir="${PROJECT_ROOT}-wt-${name}"
  if [ ! -d "$wt_dir" ]; then
    err "[merge-preflight] Worktree not found: $wt_dir"
    return 1
  fi

  local wt_data="$wt_dir/.vnx-data"
  local wt_state="$wt_data/state"

  local verdict="GO"
  local blockers=0
  local warnings=0
  local details=""

  # ── Check 1: Git cleanliness ──────────────────────────────────────────
  local git_dirty=false
  local git_unpushed=false
  local git_branch
  git_branch="$(git -C "$wt_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

  if [ -n "$(git -C "$wt_dir" status --porcelain 2>/dev/null)" ]; then
    git_dirty=true
    verdict="NO-GO"
    blockers=$((blockers + 1))
    details="${details}\n  BLOCKER: Uncommitted changes in worktree"
  fi

  local upstream
  upstream="$(git -C "$wt_dir" rev-parse --abbrev-ref '@{upstream}' 2>/dev/null || echo "")"
  if [ -n "$upstream" ]; then
    local ahead
    ahead="$(git -C "$wt_dir" rev-list --count "$upstream..HEAD" 2>/dev/null || echo 0)"
    if [ "$ahead" -gt 0 ]; then
      git_unpushed=true
      warnings=$((warnings + 1))
      details="${details}\n  WARN: $ahead unpushed commit(s) on $git_branch"
    fi
  fi

  # ── Check 2: Open items (blockers + warnings) ─────────────────────────
  local oi_script="$VNX_HOME/scripts/open_items_manager.py"
  if [ -f "$oi_script" ] && [ -f "$wt_state/open_items.json" ]; then
    local open_blockers=0
    local open_warnings=0

    # Parse open items directly from JSON for reliability
    if command -v python3 &>/dev/null; then
      local oi_counts
      oi_counts="$(python3 -c "
import json, sys
try:
    with open('$wt_state/open_items.json') as f:
        data = json.load(f)
    items = [i for i in data.get('items', []) if i.get('status') == 'open']
    blockers = [i for i in items if i.get('severity') == 'blocker']
    warns = [i for i in items if i.get('severity') == 'warn']
    print(f'{len(blockers)}|{len(warns)}')
    for b in blockers:
        print(f'BLOCKER:{b[\"id\"]}:{b[\"title\"]}')
    for w in warns:
        print(f'WARN:{w[\"id\"]}:{w[\"title\"]}')
except Exception as e:
    print(f'0|0', file=sys.stderr)
    print(f'ERR:{e}', file=sys.stderr)
" 2>/dev/null)" || true

      if [ -n "$oi_counts" ]; then
        local first_line
        first_line="$(echo "$oi_counts" | head -1)"
        open_blockers="${first_line%%|*}"
        open_warnings="${first_line##*|}"

        if [ "$open_blockers" -gt 0 ] 2>/dev/null; then
          verdict="NO-GO"
          blockers=$((blockers + open_blockers))
          while IFS= read -r line; do
            if [[ "$line" == BLOCKER:* ]]; then
              local oi_id oi_title
              oi_id="$(echo "$line" | cut -d: -f2)"
              oi_title="$(echo "$line" | cut -d: -f3-)"
              details="${details}\n  BLOCKER: [$oi_id] $oi_title"
            fi
          done <<< "$oi_counts"
        fi

        if [ "$open_warnings" -gt 0 ] 2>/dev/null; then
          if [ "$verdict" = "GO" ]; then
            verdict="NO-GO"
          fi
          warnings=$((warnings + open_warnings))
          while IFS= read -r line; do
            if [[ "$line" == WARN:* ]]; then
              local oi_id oi_title
              oi_id="$(echo "$line" | cut -d: -f2)"
              oi_title="$(echo "$line" | cut -d: -f3-)"
              details="${details}\n  WARN: [$oi_id] $oi_title"
            fi
          done <<< "$oi_counts"
        fi
      fi
    fi
  fi

  # ── Check 3: PR queue status ──────────────────────────────────────────
  local pq_script="$VNX_HOME/scripts/pr_queue_manager.py"
  if [ -f "$pq_script" ] && [ -f "$wt_state/pr_queue_state.json" ]; then
    if command -v python3 &>/dev/null; then
      local pq_status
      pq_status="$(python3 -c "
import json, sys
try:
    with open('$wt_state/pr_queue_state.json') as f:
        data = json.load(f)
    prs = data.get('prs', [])
    active = [p for p in prs if p.get('status') == 'in_progress']
    pending = [p for p in prs if p.get('status') == 'pending']
    blocked = [p for p in prs if p.get('status') == 'blocked']
    completed = [p for p in prs if p.get('status') == 'completed']
    print(f'{len(completed)}|{len(active)}|{len(pending)}|{len(blocked)}|{len(prs)}')
    for p in active:
        print(f'ACTIVE:{p[\"id\"]}')
    for p in blocked:
        print(f'BLOCKED:{p[\"id\"]}')
except Exception as e:
    print(f'0|0|0|0|0', file=sys.stderr)
" 2>/dev/null)" || true

      if [ -n "$pq_status" ]; then
        local pq_first
        pq_first="$(echo "$pq_status" | head -1)"
        local pq_completed pq_active pq_pending pq_blocked pq_total
        IFS='|' read -r pq_completed pq_active pq_pending pq_blocked pq_total <<< "$pq_first"

        if [ "$pq_active" -gt 0 ] 2>/dev/null; then
          verdict="NO-GO"
          blockers=$((blockers + 1))
          details="${details}\n  BLOCKER: $pq_active PR(s) still in_progress"
          while IFS= read -r line; do
            [[ "$line" == ACTIVE:* ]] && details="${details} (${line#ACTIVE:})"
          done <<< "$pq_status"
        fi

        if [ "$pq_blocked" -gt 0 ] 2>/dev/null; then
          verdict="NO-GO"
          blockers=$((blockers + 1))
          details="${details}\n  BLOCKER: $pq_blocked PR(s) blocked"
        fi

        details="${details}\n  INFO: PR queue: $pq_completed/$pq_total completed"
      fi
    fi
  fi

  # ── Check 3b: Gate-check results ──────────────────────────────────────
  local gate_results_dir="$wt_data/state/gate_results"
  if [ -d "$gate_results_dir" ] && command -v python3 &>/dev/null; then
    local gate_holds
    gate_holds="$(python3 -c "
import json, os, glob, sys
gate_dir = '$gate_results_dir'
holds = []
for f in sorted(glob.glob(os.path.join(gate_dir, '*.json')), reverse=True):
    try:
        with open(f) as fh:
            data = json.load(fh)
        if data.get('verdict') == 'HOLD':
            pr_id = data.get('pr_id', os.path.basename(f).replace('.json',''))
            holds.append(pr_id)
    except Exception:
        pass
print('|'.join(holds))
" 2>/dev/null)" || true

    if [ -n "$gate_holds" ]; then
      IFS='|' read -ra hold_prs <<< "$gate_holds"
      for pr_id in "${hold_prs[@]}"; do
        [ -n "$pr_id" ] || continue
        verdict="NO-GO"
        blockers=$((blockers + 1))
        details="${details}\n  BLOCKER: Gate HOLD for $pr_id"
      done
    fi
  fi

  # ── Check 4: Active processes ─────────────────────────────────────────
  local running_procs=0
  if [ -d "$wt_data/pids" ]; then
    local pid_file
    for pid_file in "$wt_data/pids"/*.pid; do
      [ -f "$pid_file" ] || continue
      local pid
      pid="$(cat "$pid_file" 2>/dev/null)"
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        running_procs=$((running_procs + 1))
      fi
    done
  fi
  if [ "$running_procs" -gt 0 ]; then
    warnings=$((warnings + 1))
    details="${details}\n  WARN: $running_procs VNX process(es) still running in worktree"
  fi

  # ── Check 5: Unmerged reports ─────────────────────────────────────────
  local unmerged_reports=0
  if [ -d "$wt_data/unified_reports" ]; then
    unmerged_reports="$(find "$wt_data/unified_reports" -name '*.md' -type f 2>/dev/null | wc -l | tr -d ' ')"
    if [ "$unmerged_reports" -gt 0 ]; then
      details="${details}\n  INFO: $unmerged_reports report(s) in worktree unified_reports/"
    fi
  fi

  # ── Verdict ───────────────────────────────────────────────────────────
  if [ "$json_output" = true ]; then
    cat <<JSON
{
  "verdict": "$verdict",
  "worktree": "$name",
  "branch": "$git_branch",
  "blockers": $blockers,
  "warnings": $warnings,
  "git_dirty": $git_dirty,
  "git_unpushed": $git_unpushed,
  "running_processes": $running_procs,
  "unmerged_reports": $unmerged_reports
}
JSON
    [ "$verdict" = "GO" ] && return 0 || return 1
  fi

  log ""
  log "════════════════════════════════════════════════════════════════"
  log " Merge Preflight: $name"
  log " Branch: $git_branch"
  log " Worktree: $wt_dir"
  log "────────────────────────────────────────────────────────────────"

  if [ -n "$details" ]; then
    echo -e "$details" | while IFS= read -r line; do
      [ -n "$line" ] && log "$line"
    done
  fi

  log "────────────────────────────────────────────────────────────────"
  if [ "$verdict" = "GO" ]; then
    log " Verdict: GO ($blockers blockers, $warnings warnings)"
    log ""
    log " Safe to run: vnx finish-worktree $name"
  else
    log " Verdict: NO-GO ($blockers blockers, $warnings warnings)"
    log ""
    log " Resolve issues above, or use: vnx finish-worktree $name --force"
  fi
  log "════════════════════════════════════════════════════════════════"

  [ "$verdict" = "GO" ] && return 0 || return 1
}
