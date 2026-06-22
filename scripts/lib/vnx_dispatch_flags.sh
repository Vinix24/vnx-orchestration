#!/usr/bin/env bash
# vnx_dispatch_flags.sh — bash binding of the single-entry routing predicate.
#
# Single source of truth lives in scripts/lib/dispatch_flags.py; this function DELEGATES to it
# so the default + the VNX_DISPATCH_LEGACY rollback semantics never drift between bash and
# python. Source this, then branch on the exit code:
#
#   if vnx_single_entry_enabled; then  # route through the door
#   else                               # legacy lane
#   fi
#
# Returns 0 (enabled → door) / 1 (disabled → legacy). Reads VNX_SINGLE_ENTRY_DISPATCH and
# VNX_DISPATCH_LEGACY from the environment (inherited by the python subprocess). Requires
# VNX_HOME (set by bin/vnx); falls back to deriving the lib dir from this script's location.

vnx_single_entry_enabled() {
  local _lib="${VNX_HOME:+${VNX_HOME}/scripts/lib}"
  if [ -z "$_lib" ] || [ ! -f "$_lib/dispatch_flags.py" ]; then
    _lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  # Pass the flags EXPLICITLY (env-assignment prefix) so a NON-exported shell variable is still
  # seen by the python subprocess — preserving the inline-check semantics this replaces. ${VAR-}
  # reads the current value whether the var is exported, set-but-not-exported, or unset (-> "").
  VNX_SINGLE_ENTRY_DISPATCH="${VNX_SINGLE_ENTRY_DISPATCH-}" \
  VNX_DISPATCH_LEGACY="${VNX_DISPATCH_LEGACY-}" \
  PYTHONPATH="${_lib}${PYTHONPATH:+:${PYTHONPATH}}" \
    python3 -c 'import sys, dispatch_flags; sys.exit(0 if dispatch_flags.single_entry_enabled() else 1)'
}
