#!/usr/bin/env bash
# VNX command: roadmap

cmd_roadmap() {
  if [ "$#" -eq 0 ]; then
    cat <<'HELP'
Usage: vnx roadmap <init|status|load|reconcile|advance|approve|step> [args]

Commands:
  init <ROADMAP.yaml>                         Initialize roadmap registry state
  status                                      Show current roadmap status
  load <feature_id>                           Materialize one feature into root FEATURE_PLAN.md + PR_QUEUE.md
  reconcile                                   Verify closure truth and detect blocking drift
  advance                                     Auto-load next feature after merged + verified closure
  approve <feature_id> --actor <name> --justification <text>
                                              Issue a single-use human approval token (required for
                                              merge_policy=human or risk_class=high features)
  step                                        Dispatch the next dependency-ready PR for the active feature
HELP
    return 0
  fi

  python3 "$VNX_HOME/scripts/roadmap_manager.py" "$@"
}
