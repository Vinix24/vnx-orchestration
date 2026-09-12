#!/usr/bin/env bash
# scripts/lib/launchd_receipt_processor_guard.sh — golf C, C3 (OI-1509/OI-1510).
#
# Is com.vnx.receipt-processor.<project_id> already loaded under launchd for
# THIS project? Sourced by scripts/commands/start.sh and
# scripts/commands/resume.sh — both start receipt_processor.sh (or its
# supervisor) directly as a fallback path when nothing else is already
# running it.
#
# Without this guard a manual (re)start races the launchd-managed instance
# for receipt_processor_supervisor.sh's own flock(2) singleton (see that
# script and scripts/launchd/com.vnx.receipt-processor.plist's comments):
# whichever process wins the lock keeps running, and if launchd's OWN
# attempt loses the race it exits 0 (lock held elsewhere — not a crash).
# KeepAlive.SuccessfulExit=false means launchd then never retries. If the
# manual process that "won" later dies (closed terminal, killed tmux
# session), nothing is left driving the receipt processor for the rest of
# that boot — the exact silent-absence failure mode this dispatch's track
# ("absence-is-loud") exists to close.
#
# Both checks below are overridable for tests, so a test forces the launchd
# state instead of depending on (or polluting) the real host:
#   VNX_LAUNCHD_GUARD_PLATFORM  — default: $(uname -s). Real launchd only
#                                 exists on Darwin.
#   VNX_LAUNCHCTL_LIST_CMD      — default: "launchctl list". Set to a fake
#                                 command/function name to inject a synthetic
#                                 launchctl snapshot.
#
# _vnx_receipt_processor_launchd_loaded [project_root]
#   Returns 0 (loaded — caller should skip its own direct start) when this
#   project's launchd label is present in the (real or injected) launchctl
#   snapshot. Returns 1 on a non-Darwin platform, an unresolvable project
#   id, or a launchctl failure — always fails OPEN to today's direct-start
#   behavior, never silently skips a real start on an unmeasurable state.
#   On a true (0) result, sets VNX_LAUNCHD_GUARD_LABEL to the label found,
#   for callers to log.

# Resolve this project's id: VNX_PROJECT_ID env, else nearest
# .vnx-project-id marker walking up from $1. Mirrors _vnx_state_project_id
# in scripts/lib/vnx_paths.sh (kept as its own small, deliberate duplicate —
# same rationale scripts/launchd/reload_plist.sh already documents for its
# own copy of this exact lookup).
_vnx_launchd_guard_project_id() {
  local start_dir="$1" dir first
  if [ -n "${VNX_PROJECT_ID:-}" ] && printf '%s' "$VNX_PROJECT_ID" | grep -Eq '^[a-z][a-z0-9-]{1,31}$'; then
    printf '%s' "$VNX_PROJECT_ID"
    return 0
  fi
  dir="$start_dir"
  while [ -n "$dir" ]; do
    if [ -f "$dir/.vnx-project-id" ]; then
      first="$(head -1 "$dir/.vnx-project-id" 2>/dev/null | tr -d '[:space:]')"
      if [ -n "$first" ] && printf '%s' "$first" | grep -Eq '^[a-z][a-z0-9-]{1,31}$'; then
        printf '%s' "$first"
      fi
      return 0
    fi
    [ "$dir" = "/" ] && break
    dir="$(dirname "$dir")"
  done
  return 0
}

_vnx_receipt_processor_launchd_loaded() {
  local proot="${1:-${PROJECT_ROOT:-$PWD}}"
  local platform="${VNX_LAUNCHD_GUARD_PLATFORM:-$(uname -s 2>/dev/null)}"
  [ "$platform" = "Darwin" ] || return 1

  local pid
  pid="$(_vnx_launchd_guard_project_id "$proot")"
  [ -n "$pid" ] || return 1

  local label="com.vnx.receipt-processor.${pid}"
  local list_cmd="${VNX_LAUNCHCTL_LIST_CMD:-launchctl list}"
  # Exact field match on the label (last token of each launchctl list line), not a grep substring: a shorter project id would phantom-match a longer one's line (OI-1721).
  # shellcheck disable=SC2086  # list_cmd may be a two-word real command ("launchctl list")
  if $list_cmd 2>/dev/null | awk -v lbl="$label" '$NF == lbl { found=1 } END { exit !found }'; then
    # shellcheck disable=SC2034  # read by callers in start.sh/resume.sh for their log line
    VNX_LAUNCHD_GUARD_LABEL="$label"
    return 0
  fi
  return 1
}
