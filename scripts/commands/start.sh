#!/usr/bin/env bash
# VNX Command: start
# Extracted from bin/vnx — launches the VNX T0 tmux session.
#
# This file is sourced by bin/vnx's command loader. All functions and variables
# from the main script (log, err, vnx_kill_all_orchestration, _interactive_startup_menu,
# _update_last_used, _detect_worktree_context, cmd_intelligence_export, etc.)
# are available when this runs.

cmd_start() {
  local session_name="vnx-$(basename "$PROJECT_ROOT")"
  local terms_dir="$PROJECT_ROOT/.claude/terminals"
  local scripts_dir="$VNX_HOME/scripts"
  local runtime_dir="$VNX_DATA_DIR"
  local state_dir="${VNX_STATE_DIR:-$runtime_dir/state}"
  local dispatch_dir="${VNX_DISPATCH_DIR:-$runtime_dir/dispatches}"
  local log_dir="$runtime_dir/logs"

  # ── Cleanup: kill existing VNX session for THIS project/worktree ────
  # Session name is unique per project/worktree, so this only affects us.
  if tmux has-session -t "$session_name" 2>/dev/null; then
    log "Killing existing VNX session: $session_name"
    tmux kill-session -t "$session_name" 2>/dev/null || true
    sleep 1
  fi

  # Kill orphan orchestration processes scoped to THIS project's data dir.
  # Processes are matched by VNX_DATA_DIR in their command line (log redirects).
  local _kill_scope="$runtime_dir"
  local stale_count
  stale_count=$(pgrep -f "$_kill_scope" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$stale_count" -gt 0 ]; then
    log "Cleaning up $stale_count orphan VNX process(es)..."
    pkill -f "vnx_supervisor.*$_kill_scope" 2>/dev/null || true
    pkill -f "$_kill_scope" 2>/dev/null || true
    sleep 1
    # Force kill anything that didn't exit cleanly
    pkill -9 -f "$_kill_scope" 2>/dev/null || true
    log "Cleanup complete."
  fi

  # ── Intelligence: export current state, then import if needed ────────
  local _intel_meta="$VNX_INTELLIGENCE_DIR/db_export/_export_meta.json"
  local _db_file="$state_dir/quality_intelligence.db"
  # Export first: capture intelligence from previous session (since vnx stop may never run)
  if [ -f "$_db_file" ]; then
    log "[start] Exporting intelligence to $VNX_INTELLIGENCE_DIR"
    cmd_intelligence_export || log "[start] WARN: intelligence export failed (non-fatal)"
  fi
  # Then import if export is newer than DB (e.g. from git merge)
  if [ -f "$_intel_meta" ]; then
    local _need_import=false
    if [ ! -f "$_db_file" ]; then
      _need_import=true
    elif [ "$_intel_meta" -nt "$_db_file" ]; then
      _need_import=true
    fi
    if [ "$_need_import" = true ]; then
      log "[start] Intelligence export at $VNX_INTELLIGENCE_DIR is newer than DB — importing..."
      cmd_intelligence_import || log "[start] WARN: intelligence import failed (non-fatal)"
    fi
  fi

  # ── Profile/preset support ──────────────────────────────────────────
  local profile_name=""
  local preset_name=""
  local use_last=false
  while [ $# -gt 0 ]; do
    case "$1" in
      --profile|-p) profile_name="$2"; shift 2 ;;
      --profile=*) profile_name="${1#*=}"; shift ;;
      --preset) preset_name="$2"; shift 2 ;;
      --preset=*) preset_name="${1#*=}"; shift ;;
      --last) use_last=true; shift ;;
      *) shift ;;
    esac
  done

  local presets_dir="$runtime_dir/startup_presets"
  mkdir -p "$presets_dir"

  # Load configuration: --preset > --last > --profile > interactive menu > config.env
  if [ -n "$preset_name" ]; then
    # --preset <name>: load a saved startup preset directly
    local preset_file="$presets_dir/${preset_name}.env"
    if [ -f "$preset_file" ]; then
      # shellcheck source=/dev/null
      source "$preset_file"
      _update_last_used "$presets_dir" "$preset_file"
      log "Loaded preset: $preset_name ($preset_file)"
    else
      err "Preset not found: $preset_file"
      echo "Available presets:"
      ls "$presets_dir/"*.env 2>/dev/null | while read -r f; do
        local bname; bname="$(basename "${f%.env}")"
        [ "$bname" = "last-used" ] && continue
        echo "  $bname"
      done
      exit 1
    fi
  elif [ "$use_last" = true ]; then
    # --last: load last-used preset
    if [ -f "$presets_dir/last-used.env" ]; then
      # shellcheck source=/dev/null
      source "$presets_dir/last-used.env"
      local last_target
      last_target="$(readlink "$presets_dir/last-used.env" 2>/dev/null || echo "unknown")"
      log "Loaded last-used preset: $(basename "${last_target%.env}")"
    else
      err "No last-used preset found. Run 'vnx start' interactively first."
      exit 1
    fi
  elif [ -n "$profile_name" ]; then
    # --profile <name>: legacy profile support (backwards compatible)
    local profile_file="$runtime_dir/profiles/${profile_name}.env"
    if [ -f "$profile_file" ]; then
      # shellcheck source=/dev/null
      source "$profile_file"
      log "Loaded profile: $profile_name ($profile_file)"
    else
      err "Profile not found: $profile_file"
      echo "Available profiles:"
      ls "$runtime_dir/profiles/"*.env 2>/dev/null | while read -r f; do
        echo "  $(basename "${f%.env}")"
      done
      exit 1
    fi
  elif [ -t 0 ]; then
    # Interactive mode: show startup menu when stdin is a terminal
    if ! _interactive_startup_menu "$presets_dir"; then
      # Menu aborted or invalid choice — fall back to config.env or defaults
      if [ -f "$runtime_dir/config.env" ]; then
        source "$runtime_dir/config.env"
        log "Loaded project config: $runtime_dir/config.env"
      fi
    fi
  elif [ -f "$runtime_dir/config.env" ]; then
    # shellcheck source=/dev/null
    source "$runtime_dir/config.env"
    log "Loaded project config: $runtime_dir/config.env"
  fi
  # Resolve the T0 provider once. Workers are spawned on demand outside startup.
  local VNX_PROVIDER="${VNX_T0_PROVIDER:-${VNX_PROVIDER:-claude_code}}"
  local gemini_model="${VNX_GEMINI_MODEL:-gemini-2.5-pro}"
  local codex_model="${VNX_CODEX_MODEL:-gpt-5.1-codex-mini}"
  local t0_flags="${VNX_T0_FLAGS:-}"

  # Skip-permissions flags (from preset/custom config)
  local t0_skip="${VNX_T0_SKIP_PERMISSIONS:-0}"

  # Queue popup toggle (from preset/custom config)
  local queue_popup_enabled="${VNX_QUEUE_POPUP_ENABLED:-1}"
  export VNX_QUEUE_POPUP_ENABLED="$queue_popup_enabled"

  local t0_model="${VNX_T0_MODEL:-default}"
  local t0_cmd=""
  local t0_provider_for_state="claude_code"
  local t0_provider_label="Claude"
  local t0_skip_flag=""
  case "$VNX_PROVIDER" in
    codex_cli|codex)
      [ "$t0_skip" = "1" ] && t0_skip_flag=" --full-auto"
      t0_cmd="codex -m $codex_model$t0_skip_flag"
      t0_provider_for_state="codex"
      t0_provider_label="Codex CLI" ;;
    gemini_cli|gemini)
      t0_cmd="gemini --yolo -m $gemini_model --include-directories '$PROJECT_ROOT'"
      t0_provider_for_state="gemini"
      t0_provider_label="Gemini CLI" ;;
    *)
      [ "$t0_skip" = "1" ] && t0_skip_flag=" --dangerously-skip-permissions"
      t0_cmd="claude --model $t0_model $t0_flags$t0_skip_flag" ;;
  esac

  if ! command -v tmux >/dev/null 2>&1; then
    err "tmux is required for 'vnx start'. Install: brew install tmux"
    exit 1
  fi

  # Ensure runtime directories are available for both fresh starts and
  # stale-session auto-heal paths.
  mkdir -p "$state_dir" \
           "$dispatch_dir"/{pending,active,completed,rejected} \
           "$log_dir" \
           "$terms_dir/T0"

  # If session already exists, attach to it. Auto-heal stale sessions that only
  # contain idle shells (no active Claude/Codex/Gemini process).
  if tmux has-session -t "$session_name" 2>/dev/null; then
    local active_cli_count=0
    active_cli_count="$(tmux list-panes -t "$session_name" -F '#{pane_current_command}' 2>/dev/null \
      | awk '$1=="node" || $1=="claude" || $1=="codex" || $1=="gemini" {c++} END {print c+0}')"

    # If a profile was explicitly selected, kill the existing session and restart
    # fresh so the chosen T0 provider is actually applied.
    if [ -n "$profile_name" ] && [ "${active_cli_count:-0}" -gt 0 ]; then
      log "Profile '$profile_name' selected — restarting session to apply T0 provider ($VNX_PROVIDER)..."
      vnx_kill_all_orchestration "$scripts_dir" "$log_dir" "profile_restart"
      tmux kill-session -t "$session_name" 2>/dev/null || true
      sleep 1
      # Fall through to fresh session creation below.

    elif [ "${active_cli_count:-0}" -eq 0 ]; then
      log "Detected stale $session_name session (shells only). Re-launching T0 CLI..."

      # Kill ALL stale orchestration processes before re-launching CLIs.
      # Without this, re-heal creates duplicate supervisors/receipt_processors.
      vnx_kill_all_orchestration "$scripts_dir" "$log_dir" "session_reheal"

      local T0
      T0="$(tmux list-panes -t "$session_name" -F '#{pane_id} #{pane_current_path}' 2>/dev/null | awk -v p="$terms_dir/T0" '$2==p {print $1; exit}')"

      local node_path=""
      node_path="$(_resolve_node_path 2>/dev/null)" || node_path=""
      local env_clean="unset PROJECT_ROOT VNX_HOME VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_LOGS_DIR VNX_SKILLS_DIR VNX_PIDS_DIR VNX_LOCKS_DIR VNX_REPORTS_DIR VNX_DB_DIR"
      local env_set="export PROJECT_ROOT='$PROJECT_ROOT' VNX_HOME='$VNX_HOME' VNX_DATA_DIR='$VNX_DATA_DIR' VNX_SKILLS_DIR='${VNX_SKILLS_DIR:-}'"
      local path_prefix="$VNX_HOME/bin"
      [ -n "$node_path" ] && path_prefix="$path_prefix:$node_path"

      [ -n "$T0" ] && tmux send-keys -t "$T0" "source ~/.zshrc 2>/dev/null && $env_clean && $env_set && export PATH=$path_prefix:\$PATH && export CLAUDE_ROLE=orchestrator && export CLAUDE_PROJECT_DIR='$PROJECT_ROOT' && cd '$terms_dir/T0' && $t0_cmd" C-m

      if [ -n "$T0" ]; then
        cat > "$state_dir/panes.json" <<PJSON
{
  "session": "$session_name",
  "t0": { "pane_id": "$T0", "role": "orchestrator", "do_not_target": true, "model": "$t0_model", "provider": "$t0_provider_for_state" },
  "T0": { "pane_id": "$T0", "role": "orchestrator", "do_not_target": true, "model": "$t0_model", "provider": "$t0_provider_for_state" },
  "tracks": {}
}
PJSON
        # Startup creates T0 only; workers populate runtime state on demand.
        cat > "$state_dir/terminal_state.json" <<TSJSON
{
  "schema_version": 1,
  "terminals": {}
}
TSJSON
        tmux pipe-pane -o -t "$T0" "cat >> '$state_dir/t0_conversation.log'"

        # Save declarative session profile (PR-3: A-R4, A-R5)
        python3 "$VNX_HOME/scripts/lib/tmux_session_profile.py" save \
          --state-dir "$state_dir" \
          --session "$session_name" \
          --project-root "$PROJECT_ROOT" \
          2>/dev/null || log "[start] WARN: session profile save failed (non-fatal)"
      fi

      # Re-start orchestration processes (they were killed by vnx_kill_all_orchestration above)
      if [ -f "$scripts_dir/vnx_supervisor_simple.sh" ]; then
        cd "$scripts_dir"
        VNX_QUEUE_POPUP_ENABLED="$queue_popup_enabled" nohup bash ./vnx_supervisor_simple.sh start > "$log_dir/supervisor.log" 2>&1 &
        log "Supervisor re-started (PID: $!)"
        cd "$PROJECT_ROOT"
        sleep 2
      else
        # Fallback mode only: without supervisor, start critical services directly.
        if [ -f "$scripts_dir/smart_tap_v7_json_translator.sh" ]; then
          cd "$scripts_dir"
          nohup bash ./smart_tap_v7_json_translator.sh > "$log_dir/tap.log" 2>&1 &
          log "Smart tap V7 re-started (PID: $!)"
          cd "$PROJECT_ROOT"
        fi
        if [ -f "$scripts_dir/dispatcher_v8_minimal.sh" ]; then
          cd "$scripts_dir"
          nohup bash ./dispatcher_v8_minimal.sh > "$log_dir/dispatcher.log" 2>&1 &
          log "Dispatcher V8 re-started (PID: $!)"
          cd "$PROJECT_ROOT"
        fi
        if [ -f "$scripts_dir/receipt_processor_v4.sh" ]; then
          cd "$scripts_dir"
          VNX_MODE=monitor nohup bash ./receipt_processor_v4.sh > "$log_dir/receipt_processor.log" 2>&1 &
          log "Receipt processor V4 re-started (PID: $!)"
          cd "$PROJECT_ROOT"
        fi
        if [ -f "$scripts_dir/generate_valid_dashboard.sh" ]; then
          cd "$scripts_dir"
          nohup bash ./generate_valid_dashboard.sh > "$log_dir/dashboard_gen.log" 2>&1 &
          log "Dashboard generator re-started (PID: $!)"
          cd "$PROJECT_ROOT"
        fi
        if [ -f "$scripts_dir/unified_state_manager_v2.py" ]; then
          cd "$scripts_dir"
          nohup python3 ./unified_state_manager_v2.py > "$log_dir/state_manager.log" 2>&1 &
          log "State manager re-started (PID: $!)"
          cd "$PROJECT_ROOT"
        fi
      fi

      # Re-heal is complete — do NOT fall through to fresh-start path.
      log "Session re-heal complete. Attaching..."
      if [ -z "${TMUX:-}" ]; then
        exec tmux attach-session -t "$session_name"
      else
        tmux switch-client -t "$session_name"
      fi
      return 0
    else
      log "VNX session already running. Attaching..."
      if [ -z "${TMUX:-}" ]; then
        exec tmux attach-session -t "$session_name"
      else
        tmux switch-client -t "$session_name"
      fi
      return 0
    fi

    # Profile restart: session was killed above — fall through to fresh start below.
    # (no return here — let execution continue to the fresh session creation code)
  fi

  # ── Terminal .claude + .vnx-data symlinks ────────────────────────────
  # Symlink T0's .claude → project root .claude so Claude Code discovers
  # skills when CWD is the terminal subdirectory.
  # Symlink .vnx-data so report writes from terminal CWD land in the
  # project's runtime directory (not a relative path that escapes).
  for d in T0; do
    if [ ! -L "$terms_dir/$d/.claude" ]; then
      rm -rf "$terms_dir/$d/.claude"
      ln -s "$PROJECT_ROOT/.claude" "$terms_dir/$d/.claude"
    fi
    if [ ! -L "$terms_dir/$d/.vnx-data" ]; then
      rm -rf "$terms_dir/$d/.vnx-data"
      ln -s "$runtime_dir" "$terms_dir/$d/.vnx-data"
    fi
  done

  # ── Kill ALL stale orchestration processes ──────────────────────────────
  vnx_kill_all_orchestration "$scripts_dir" "$log_dir" "session_restart"

  log "Launching VNX tmux session with T0 only..."

  # ── T0-only layout (single window, one pane) ─────────────────────────
  local T0
  T0=$(tmux new-session -d -s "$session_name" -n main -c "$terms_dir/T0" -P -F '#{pane_id}')
  tmux set-option -t "$session_name" -g allow-rename off
  tmux set-window-option -t "$session_name:main" automatic-rename off
  tmux set-window-option -t "$session_name:main" allow-rename off

  # Pane title and UI.
  tmux select-pane -t "$T0" -T "T0"
  tmux set -t "$session_name" -g pane-border-status top
  tmux set -t "$session_name" -g pane-border-format "#{pane_title}"
  tmux set -t "$session_name" -g mouse on

  # ── State files (panes.json + terminal_state.json) ─────────────────────
  # Startup maps only T0. Workers create their own runtime lanes on demand.
  cat > "$state_dir/panes.json" <<PJSON
{
  "session": "$session_name",
  "t0": { "pane_id": "$T0", "role": "orchestrator", "do_not_target": true, "model": "$t0_model", "provider": "$t0_provider_for_state" },
  "T0": { "pane_id": "$T0", "role": "orchestrator", "do_not_target": true, "model": "$t0_model", "provider": "$t0_provider_for_state" },
  "tracks": {}
}
PJSON
  cat > "$state_dir/terminal_state.json" <<TSJSON
{
  "schema_version": 1,
  "terminals": {}
}
TSJSON
  log "State files written (panes.json + terminal_state.json)"

  # ── Declarative session profile (PR-3: A-R4, A-R5) ──────────────────────
  python3 "$VNX_HOME/scripts/lib/tmux_session_profile.py" save \
    --state-dir "$state_dir" \
    --session "$session_name" \
    --project-root "$PROJECT_ROOT" \
    2>/dev/null || log "[start] WARN: session profile save failed (non-fatal)"

  # ── Worktree initialization (if enabled) ────────────────────────────
  # T0-only startup does not create per-terminal worker worktrees. Use
  # 'vnx new-worktree <name>' for feature worktrees.
  if [ "${VNX_WORKTREES:-false}" = "true" ]; then
    log "Ignoring VNX_WORKTREES=true: T0-only startup no longer creates fixed worker worktrees."
  fi

  # ── Conversation capture (pipe-pane) ───────────────────────────────────
  tmux pipe-pane -o -t "$T0" "cat >> '$state_dir/t0_conversation.log'"
  log "Conversation capture enabled for T0"

  # ── Orchestration components (if available) ────────────────────────────
  if [ -f "$scripts_dir/vnx_supervisor_simple.sh" ]; then
    cd "$scripts_dir"
    VNX_QUEUE_POPUP_ENABLED="$queue_popup_enabled" nohup bash ./vnx_supervisor_simple.sh start > "$log_dir/supervisor.log" 2>&1 &
    log "Supervisor started (PID: $!)"
    cd "$PROJECT_ROOT"
    sleep 2
  else
    # Fallback mode only: without supervisor, start critical services directly.
    if [ -f "$scripts_dir/smart_tap_v7_json_translator.sh" ]; then
      cd "$scripts_dir"
      nohup bash ./smart_tap_v7_json_translator.sh > "$log_dir/tap.log" 2>&1 &
      log "Smart tap V7 started (PID: $!)"
      cd "$PROJECT_ROOT"
    fi
    if [ -f "$scripts_dir/dispatcher_v8_minimal.sh" ]; then
      cd "$scripts_dir"
      nohup bash ./dispatcher_v8_minimal.sh > "$log_dir/dispatcher.log" 2>&1 &
      log "Dispatcher V8 started (PID: $!)"
      cd "$PROJECT_ROOT"
    fi
    if [ -f "$scripts_dir/receipt_processor_v4.sh" ]; then
      cd "$scripts_dir"
      VNX_MODE=monitor nohup bash ./receipt_processor_v4.sh > "$log_dir/receipt_processor.log" 2>&1 &
      log "Receipt processor V4 started (PID: $!)"
      cd "$PROJECT_ROOT"
    fi
    if [ -f "$scripts_dir/generate_valid_dashboard.sh" ]; then
      cd "$scripts_dir"
      nohup bash ./generate_valid_dashboard.sh > "$log_dir/dashboard_gen.log" 2>&1 &
      log "Dashboard generator started (PID: $!)"
      cd "$PROJECT_ROOT"
    fi
    if [ -f "$scripts_dir/unified_state_manager_v2.py" ]; then
      cd "$scripts_dir"
      nohup python3 ./unified_state_manager_v2.py > "$log_dir/state_manager.log" 2>&1 &
      log "State manager started (PID: $!)"
      cd "$PROJECT_ROOT"
    fi
  fi

  # ── Popup queue keybindings ────────────────────────────────────────────
  local popup_script=""
  if [ -f "$scripts_dir/queue_ui_enhanced.sh" ]; then
    popup_script="$scripts_dir/queue_ui_enhanced.sh"
  fi
  if [ -n "$popup_script" ]; then
    # CRITICAL: tmux keybindings are server-wide — the last `vnx start` wins.
    # To support multiple VNX projects on the same tmux server, we store the
    # popup command as a per-session option (@vnx_popup_cmd) and bind a single
    # global key that reads the CURRENT session's option at invocation time.
    local popup_env="unset PROJECT_ROOT VNX_HOME VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_LOGS_DIR VNX_SKILLS_DIR VNX_PIDS_DIR VNX_LOCKS_DIR VNX_REPORTS_DIR VNX_DB_DIR; export PROJECT_ROOT='$PROJECT_ROOT' VNX_HOME='$VNX_HOME' VNX_DATA_DIR='$VNX_DATA_DIR'"
    local popup_full_cmd="$popup_env; bash '$popup_script'"

    # Store per-session so Ctrl+G resolves the correct project at runtime.
    tmux set-option -t "$session_name" @vnx_popup_cmd "$popup_full_cmd" 2>/dev/null

    # Backfill: ensure all other VNX sessions also have @vnx_popup_cmd set.
    # Without this, old sessions (started before the resolver existed) have no
    # popup cmd and Ctrl+G shows "No VNX popup configured" instead of the queue.
    for _other_session in $(tmux list-sessions -F '#{session_name}' 2>/dev/null); do
      [ "$_other_session" = "$session_name" ] && continue
      # Skip sessions that already have a popup cmd
      _existing_cmd=$(tmux show-option -t "$_other_session" -v @vnx_popup_cmd 2>/dev/null || true)
      [ -n "$_existing_cmd" ] && continue
      # Detect project root from pane paths in that session
      _other_root=$(tmux list-panes -t "$_other_session" -F '#{pane_current_path}' 2>/dev/null \
        | head -1 | sed 's|/\.claude/terminals/.*||' || true)
      # Detect VNX layout: prefer .vnx/ primary, fall back to legacy layout
      local _other_vnx_home=""
      if [ -n "$_other_root" ] && [ -d "$_other_root/.vnx" ]; then
        _other_vnx_home="$_other_root/.vnx"
      elif [ -n "$_other_root" ] && [ -d "$_other_root/.claude"/"vnx-system" ]; then
        _other_vnx_home="$_other_root/.claude"/"vnx-system"
      fi
      if [ -n "$_other_vnx_home" ]; then
        _other_cmd="unset PROJECT_ROOT VNX_HOME VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_LOGS_DIR VNX_SKILLS_DIR VNX_PIDS_DIR VNX_LOCKS_DIR VNX_SOCKETS_DIR VNX_REPORTS_DIR VNX_DB_DIR; export PROJECT_ROOT='$_other_root' VNX_HOME='$_other_vnx_home' VNX_DATA_DIR='$_other_root/.vnx-data'; bash '$_other_vnx_home/scripts/queue_ui_enhanced.sh'"
        tmux set-option -t "$_other_session" @vnx_popup_cmd "$_other_cmd" 2>/dev/null || true
        log "Backfilled @vnx_popup_cmd on session: $_other_session (project: $_other_root)"
      fi
    done

    # Create resolver script that reads @vnx_popup_cmd from the current client session.
    # Falls back to scanning all sessions by pane path if the current session has no cmd set.
    # Keep this compatible with tmux variants that do not support `run-shell -F`.
    # Project-scoped path (OI-1067 / W4G): each project owns its own resolver
    # under VNX_DATA_DIR/sockets/, so concurrent VNX projects don't clobber.
    mkdir -p "$VNX_DATA_DIR/sockets"
    local resolver="$VNX_DATA_DIR/sockets/vnx_popup_resolver.sh"
    cat > "$resolver" <<'RESOLVER'
#!/usr/bin/env bash
session=""
session=$(tmux display-message -p '#{client_session}' 2>/dev/null || true)
if [ -z "$session" ]; then
    session=$(tmux list-clients -F '#{client_session}' 2>/dev/null | head -n1 || true)
fi
if [ -z "$session" ]; then
    echo "Could not determine tmux session."
    sleep 2
    exit 1
fi

# Primary: read popup cmd from current session.
cmd=$(tmux show-option -t "$session" -v @vnx_popup_cmd 2>/dev/null || true)

# Fallback: if current session has no cmd (e.g. old session before naming change),
# scan all sessions and pick the one whose PROJECT_ROOT matches the current pane path.
if [ -z "$cmd" ]; then
    current_path=$(tmux display-message -p '#{pane_current_path}' 2>/dev/null || true)
    for s in $(tmux list-sessions -F '#{session_name}' 2>/dev/null); do
        candidate=$(tmux show-option -t "$s" -v @vnx_popup_cmd 2>/dev/null || true)
        if [ -z "$candidate" ]; then continue; fi
        project_root=$(echo "$candidate" | grep -oE "PROJECT_ROOT='[^']+'" | head -1 | cut -d"'" -f2 || true)
        if [ -n "$project_root" ] && [[ "$current_path" == "$project_root"* ]]; then
            cmd="$candidate"
            # Also set it on current session for future Ctrl+G calls.
            tmux set-option -t "$session" @vnx_popup_cmd "$cmd" 2>/dev/null || true
            break
        fi
    done
fi

if [ -n "$cmd" ]; then
    eval "$cmd"
else
    echo "No VNX popup configured for session: $session"
    echo "Start VNX first: vnx start"
    sleep 2
fi
RESOLVER
    chmod +x "$resolver"

    tmux bind-key -n C-g display-popup -E -w 80% -h 60% "bash $resolver" 2>/dev/null
    tmux bind-key -n 'C-\' display-popup -E -w 80% -h 60% "bash $resolver" 2>/dev/null
    tmux bind-key q display-popup -E -w 80% -h 60% "bash $resolver" 2>/dev/null
    tmux bind-key p display-popup -E -w 80% -h 60% "bash $resolver" 2>/dev/null
    log "Popup queue bound to Ctrl+G, Ctrl+\\, Ctrl+B Q/P (resolver-based)"
  fi

  # ── Clean tmux environment (prevent cross-project contamination) ──────
  # PR-3: VNX_VARS list is canonical in vnx_start_runtime.py. Shell reads it
  # via Python to stay in sync. Falls back to hardcoded list if Python unavailable.
  local _vnx_vars_list
  _vnx_vars_list="$(PYTHONPATH="$VNX_HOME/scripts/lib:${PYTHONPATH:-}" python3 -c "from vnx_start_runtime import VNX_VARS; print(' '.join(VNX_VARS))" 2>/dev/null)" \
    || _vnx_vars_list="PROJECT_ROOT VNX_HOME VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_LOGS_DIR VNX_SKILLS_DIR VNX_PIDS_DIR VNX_LOCKS_DIR VNX_SOCKETS_DIR VNX_REPORTS_DIR VNX_DB_DIR"
  for _vnx_var in $_vnx_vars_list; do
    tmux set-environment -g -u "$_vnx_var" 2>/dev/null || true
  done
  tmux set-environment -t "$session_name" PROJECT_ROOT "$PROJECT_ROOT"
  tmux set-environment -t "$session_name" VNX_HOME "$VNX_HOME"
  tmux set-environment -t "$session_name" VNX_DATA_DIR "$VNX_DATA_DIR"
  tmux set-environment -t "$session_name" VNX_STATE_DIR "$state_dir"
  tmux set-environment -t "$session_name" VNX_DISPATCH_DIR "$dispatch_dir"
  tmux set-environment -t "$session_name" VNX_SKILLS_DIR "${VNX_SKILLS_DIR:-}"

  # ── Launch CLI in the T0 pane ─────────────────────────────────────────
  # MCP FIX: Source shell profile to ensure MCP servers inherit proper
  # environment (node, nvm, PATH). Without this, MCP servers fail in tmux.
  # SKILL FIX: Explicit cd to terminal dir so Claude Code discovers
  # .claude/skills/ via the symlink (tmux -c flag alone is insufficient).
  # ENV FIX: Unset stale VNX vars from tmux global env, then re-export
  # correct values for current project. This prevents cross-project
  # contamination (e.g. SEOcrawler paths leaking into marketing-magic-circle).
  local node_path=""
  node_path="$(_resolve_node_path 2>/dev/null)" || node_path=""
  local env_clean="unset PROJECT_ROOT VNX_HOME VNX_DATA_DIR VNX_STATE_DIR VNX_DISPATCH_DIR VNX_LOGS_DIR VNX_SKILLS_DIR VNX_PIDS_DIR VNX_LOCKS_DIR VNX_REPORTS_DIR VNX_DB_DIR"
  local env_set="export PROJECT_ROOT='$PROJECT_ROOT' VNX_HOME='$VNX_HOME' VNX_DATA_DIR='$VNX_DATA_DIR' VNX_SKILLS_DIR='${VNX_SKILLS_DIR:-}'"
  local path_prefix="$VNX_HOME/bin"
  [ -n "$node_path" ] && path_prefix="$path_prefix:$node_path"

  tmux send-keys -t "$T0" "source ~/.zshrc 2>/dev/null && $env_clean && $env_set && export PATH=$path_prefix:\$PATH && export CLAUDE_ROLE=orchestrator && export CLAUDE_PROJECT_DIR='$PROJECT_ROOT' && cd '$terms_dir/T0' && $t0_cmd" C-m

  # Focus T0.
  tmux select-pane -t "$T0"

  # ── Status ─────────────────────────────────────────────────────────────
  echo ""
  echo "VNX SESSION LAUNCHED"
  echo ""
  [ -n "$profile_name" ] && echo "Profile: $profile_name"
  [ -n "$preset_name" ] && echo "Preset: $preset_name"
  echo "Started T0 in tmux session $session_name. Workers spawn on-demand (subprocess or leaseless tmux lane); attach with \`tmux attach -t vnx-<dispatch_id>\` when needed."
  echo "Layout: T0 only  |  T0: $t0_provider_label ($t0_model)"

  # Show skip-permissions status
  local skip_summary=""
  [ "$t0_skip" = "1" ] && skip_summary="${skip_summary}T0 "
  if [ -n "$skip_summary" ]; then
    echo "Skip-permissions: ${skip_summary}"
  fi
  [ "$queue_popup_enabled" = "0" ] && echo "Queue popup: disabled"

  echo ""
  echo "Controls:"
  echo "  Ctrl+G ......... Open dispatch queue popup"
  echo "  Ctrl+B Q ....... Open popup (tmux prefix style)"
  echo "  Ctrl+B D ....... Detach (keep running)"
  echo ""
  echo "Logs: $log_dir/"
  echo ""

  # Attach.
  if [ -z "${TMUX:-}" ]; then
    exec tmux attach-session -t "$session_name"
  else
    tmux switch-client -t "$session_name"
  fi
}
