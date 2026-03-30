#!/usr/bin/env bash
# VNX command: roadmap

cmd_roadmap() {
  if [ "$#" -eq 0 ]; then
    cat <<'HELP'
Usage: vnx roadmap <init|status|load|reconcile|advance> [args]

Commands:
  init <ROADMAP.yaml>    Initialize roadmap registry state
  status                 Show current roadmap status
  load <feature_id>      Materialize one feature into root FEATURE_PLAN.md + PR_QUEUE.md
  reconcile              Verify closure truth and detect blocking drift
  advance                Auto-load next feature after merged + verified closure
HELP
    return 0
  fi

  python3 "$VNX_HOME/scripts/roadmap_manager.py" "$@"
}
