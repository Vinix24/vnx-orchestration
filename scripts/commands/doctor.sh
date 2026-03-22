#\!/usr/bin/env bash
# VNX Command: doctor
# Extracted from bin/vnx — validates VNX installation health.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, VNX_HOME, VNX_CONFIG_DIR, etc.)
# are available when this runs.

check_required_tool() {
  local tool="$1"
  if command -v "$tool" >/dev/null 2>&1; then
    log "[doctor] OK tool: $tool"
    return 0
  fi

  err "[doctor] Missing required tool: $tool"
  return 1
}

check_recommended_tool() {
  local tool="$1"
  if command -v "$tool" >/dev/null 2>&1; then
    log "[doctor] OK optional tool: $tool"
  else
    log "[doctor] WARN optional tool not found: $tool"
  fi
}

run_path_hygiene_check() {
  local check_script="$VNX_HOME/scripts/vnx_doctor.sh"
  if [ -f "$check_script" ]; then
    if bash "$check_script"; then
      return 0
    fi
    return 1
  fi

  log "[doctor] WARN: Path hygiene script missing: $check_script"
  return 0
}

run_package_check() {
  local check_script="$VNX_HOME/scripts/vnx_package_check.sh"
  if [ -f "$check_script" ]; then
    if bash "$check_script"; then
      log "[doctor] OK: Dist root contains no runtime directories."
      return 0
    fi
    return 1
  fi

  log "[doctor] WARN: Package hygiene script missing: $check_script"
  return 0
}

ensure_write_access() {
  local test_file="$VNX_STATE_DIR/.vnx_doctor_write_test"
  if touch "$test_file" 2>/dev/null; then
    rm -f "$test_file"
    log "[doctor] OK write access: $VNX_STATE_DIR"
    return 0
  fi

  err "[doctor] Cannot write to state directory: $VNX_STATE_DIR"
  return 1
}

check_required_path() {
  local path="$1"
  local kind="$2"
  if [ "$kind" = "dir" ]; then
    if [ -d "$path" ]; then
      log "[doctor] OK dir: $path"
      return 0
    fi
    err "[doctor] Missing dir: $path"
    return 1
  fi

  if [ -f "$path" ]; then
    log "[doctor] OK file: $path"
    return 0
  fi

  err "[doctor] Missing file: $path"
  return 1
}

cmd_doctor() {
  local failed=0
  local do_package_check="${VNX_DOCTOR_PACKAGE_CHECK:-0}"

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --package-check|--strict)
        do_package_check=1
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        err "[doctor] Unknown option: $1"
        return 1
        ;;
    esac
    shift
  done

  check_required_tool bash || failed=1
  check_required_tool python3 || failed=1
  check_recommended_tool rg
  check_recommended_tool jq
  check_recommended_tool tmux
  check_recommended_tool codex
  check_recommended_tool gemini

  # Path resolution checks (PR-1: centralized resolvers)
  local resolved_node_path=""
  resolved_node_path="$(_resolve_node_path 2>/dev/null)" || resolved_node_path=""
  if [ -n "$resolved_node_path" ] && [ -x "$resolved_node_path/node" ]; then
    log "[doctor] OK node path: $resolved_node_path"
  else
    log "[doctor] WARN: Node path not resolved. MCP servers may fail in tmux. Set VNX_NODE_PATH or install nvm."
  fi

  local resolved_venv=""
  resolved_venv="$(_resolve_venv_path 2>/dev/null)" || resolved_venv=""
  if [ -n "$resolved_venv" ] && [ -f "$resolved_venv" ]; then
    log "[doctor] OK venv: $resolved_venv"
  else
    log "[doctor] WARN: Python venv not found. Quality services may use system python."
  fi

  local resolved_project_root=""
  resolved_project_root="$(_resolve_project_root 2>/dev/null)" || resolved_project_root=""
  if [ -n "$resolved_project_root" ] && [ -d "$resolved_project_root" ]; then
    if [ "$resolved_project_root" = "$PROJECT_ROOT" ] || [ "$(cd "$resolved_project_root" && pwd)" = "$(cd "$PROJECT_ROOT" && pwd)" ]; then
      log "[doctor] OK project root: $PROJECT_ROOT"
    else
      log "[doctor] WARN: Resolved project root ($resolved_project_root) differs from PROJECT_ROOT ($PROJECT_ROOT)"
    fi
  else
    err "[doctor] Project root resolution failed"
    failed=1
  fi

  check_required_path "$VNX_CONFIG_DIR" dir || failed=1
  check_required_path "$VNX_CONFIG_FILE" file || failed=1

  check_required_path "$VNX_DATA_DIR" dir || failed=1
  check_required_path "$VNX_STATE_DIR" dir || failed=1
  check_required_path "$VNX_LOGS_DIR" dir || failed=1
  check_required_path "$VNX_PIDS_DIR" dir || failed=1
  check_required_path "$VNX_LOCKS_DIR" dir || failed=1
  check_required_path "$VNX_DISPATCH_DIR" dir || failed=1
  check_required_path "$VNX_DISPATCH_DIR/pending" dir || failed=1
  check_required_path "$VNX_DISPATCH_DIR/active" dir || failed=1
  check_required_path "$VNX_DISPATCH_DIR/completed" dir || failed=1
  check_required_path "$VNX_REPORTS_DIR" dir || failed=1

  check_required_path "$TERMINALS_TEMPLATE_DIR/T0.md" file || failed=1
  check_required_path "$TERMINALS_TEMPLATE_DIR/T1.md" file || failed=1
  check_required_path "$TERMINALS_TEMPLATE_DIR/T2.md" file || failed=1
  check_required_path "$TERMINALS_TEMPLATE_DIR/T3.md" file || failed=1
  check_required_path "$VNX_SKILLS_DIR/skills.yaml" file || failed=1

  # Hooks checks
  check_required_path "$PROJECT_ROOT/.claude/hooks/sessionstart.sh" file || failed=1
  if [ -f "$PROJECT_ROOT/.claude/settings.json" ]; then
    if grep -q '"hooks"' "$PROJECT_ROOT/.claude/settings.json" 2>/dev/null; then
      log "[doctor] OK hooks: settings.json has hooks section"
    else
      err "[doctor] Missing hooks section in .claude/settings.json"
      failed=1
    fi
  else
    err "[doctor] Missing .claude/settings.json"
    failed=1
  fi

  # Quality intelligence database check
  local db_path="$VNX_STATE_DIR/quality_intelligence.db"
  if [ -f "$db_path" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
      local table_count
      table_count=$(sqlite3 "$db_path" "SELECT COUNT(*) FROM sqlite_master WHERE type='table'" 2>/dev/null || echo "0")
      if [ "$table_count" -ge 10 ]; then
        log "[doctor] OK quality DB: $table_count tables"
      else
        err "[doctor] Quality DB incomplete: only $table_count tables (expected ≥10). Run: vnx init-db"
        failed=1
      fi
    else
      log "[doctor] WARN: sqlite3 not available, cannot verify quality DB"
    fi
  else
    log "[doctor] WARN: Quality intelligence DB not found. Run: vnx init-db"
  fi

  # Worktree isolation checks
  if _detect_worktree_context 2>/dev/null; then
    log "[doctor] INFO: Running in git worktree: $_WT_ROOT"
    local wt_data="$_WT_ROOT/.vnx-data"
    if [ -L "$wt_data" ]; then
      err "[doctor] WARN: .vnx-data is a SYMLINK (old model). Run 'vnx worktree-start' to migrate to isolated model."
    elif [ -d "$wt_data" ] && [ -f "$wt_data/.snapshot_meta" ]; then
      local snap_date
      snap_date=$(grep '^snapshot_date=' "$wt_data/.snapshot_meta" 2>/dev/null | cut -d= -f2)
      log "[doctor] OK worktree: isolated .vnx-data (snapshot: ${snap_date:-unknown})"
      # Check snapshot freshness (warn if >14 days old)
      if command -v python3 >/dev/null 2>&1 && [ -n "$snap_date" ]; then
        local days_old
        days_old=$(python3 -c "
from datetime import datetime, timezone
try:
    snap = datetime.fromisoformat('$snap_date'.replace('Z','+00:00'))
    print((datetime.now(timezone.utc) - snap).days)
except:
    print(-1)
" 2>/dev/null)
        if [ "${days_old:-0}" -ge 14 ]; then
          log "[doctor] WARN: Intelligence snapshot is ${days_old} days old. Consider: vnx worktree-refresh"
        fi
      fi
      # Check .env_override exists
      if [ ! -f "$wt_data/.env_override" ]; then
        err "[doctor] WARN: Missing .env_override in worktree. Run 'vnx worktree-start' to fix."
      else
        log "[doctor] OK worktree: .env_override present"
      fi
    elif [ -d "$wt_data" ]; then
      err "[doctor] WARN: .vnx-data exists but no snapshot metadata. Run 'vnx worktree-start'."
    else
      err "[doctor] WARN: No .vnx-data in worktree. Run 'vnx worktree-start'."
    fi
  fi

  # Version freshness check
  if [ -f "$VNX_HOME/.vnx-origin" ]; then
    local current_version=""
    if [ -f "$VNX_HOME/version.lock" ]; then
      current_version="$(head -1 "$VNX_HOME/version.lock" | tr -d '[:space:]')"
      log "[doctor] OK version: pinned to $current_version"
    else
      log "[doctor] INFO: No version lock. Run 'vnx update --pin <tag>' to pin."
    fi
  fi

  ensure_write_access || failed=1
  run_path_hygiene_check || failed=1
  if [ "$do_package_check" -eq 1 ]; then
    run_package_check || failed=1
  fi

  if [ "$failed" -ne 0 ]; then
    err "[doctor] FAILED"
    return 1
  fi

  log "[doctor] PASSED"
}
