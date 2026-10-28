# shellcheck shell=bash
# rp_end_line.sh - the one shell read of a dispatch end line (ADR-045 §2)
#
# ADR-045 adds a new end line type next to the legacy
# task_complete/task_failed/task_timeout: `dispatch_finished`. It carries
# `end_state` (completed/failed/timed_out/abandoned) and NOT a status a reader
# may trust raw — the append layer stamps `status` from `end_state`, and a line
# that reached the ledger some other way carries no trustworthy status at all.
# A shell filter written as "is this a task_complete?" stops seeing those
# dispatches the moment the lifecycle setting flips (ADR-045 §9/§10), so every
# shell reader of an end line asks this file.
#
# The end_state -> status projection is NOT reproduced here. It lives once, in
# scripts/lib/event_outcome_semantics.py (END_STATE_TO_STATUS, ADR-045 §4), and
# is reached through ONE `python3 -c` call that imports that module from the
# resolved project root (scripts/lib/vnx_resolve_root.sh) — so shell and Python
# cannot drift into two mapping tables.
#
# Fail-closed: a `dispatch_finished` whose end_state is missing, empty or
# outside the closed set has NO status and NO outcome. Nothing here invents
# one, no reader may fall back to the raw `status` field, and a line without a
# resolvable end_state is never a success.
#
# Requires: nothing (self-contained; sources lib/vnx_resolve_root.sh).

# shellcheck source=../vnx_resolve_root.sh
source "$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd -P)/vnx_resolve_root.sh"

# The module's own lib dir: the fallback for a project root that carries no
# scripts/lib of its own (central install, consumer project).
_RP_END_LINE_MODULE_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd -P)"

# Print the scripts/lib dir of the resolved project root (ADR-045 readers take
# the module from there). Empty when the root cannot be resolved; the python
# caller then falls back to _RP_END_LINE_MODULE_DIR. Cached in VNX_PROJECT_ROOT
# by vnx_resolve_project_root, so only the first end line of a process pays for
# the git probe.
_rp_end_line_project_lib_dir() {
    if [ -z "${VNX_PROJECT_ROOT:-}" ]; then
        vnx_resolve_project_root "${BASH_SOURCE[0]}" >/dev/null 2>&1 || VNX_PROJECT_ROOT=""
    fi
    if [ -n "${VNX_PROJECT_ROOT:-}" ]; then
        printf '%s/scripts/lib' "${VNX_PROJECT_ROOT}"
    fi
    return 0
}

# _rp_end_line_fields <record-json>
#   Prints "<status>\t<outcome>": the status the record's end line carries and
#   classify_event_outcome's verdict on it (success|failure|none). Internal —
#   both public accessors are cut from one call, so a record costs one python
#   invocation whichever of the two a caller needs.
_rp_end_line_fields() {
    local record="${1:-}"
    printf '%s' "$record" | python3 -c '
import json
import sys

for candidate in (sys.argv[1], sys.argv[2]):
    if candidate and candidate not in sys.path:
        sys.path.insert(0, candidate)

from event_outcome_semantics import classify_event_outcome, end_state_status

raw = sys.stdin.read().strip()
status = ""
outcome = "none"
try:
    record = json.loads(raw) if raw else None
except ValueError:
    record = None
if isinstance(record, dict):
    event_type = str(record.get("event_type") or record.get("event") or "").strip().lower()
    if event_type == "dispatch_finished":
        status = end_state_status(record.get("end_state")) or ""
    else:
        status = str(record.get("status") or "")
    outcome = classify_event_outcome(event_type, status) or "none"
sys.stdout.write("%s\t%s\n" % (status, outcome))
' "${_RP_END_LINE_MODULE_DIR:-}" "$(_rp_end_line_project_lib_dir)" 2>/dev/null
}

# rp_is_end_event <event_type>
#   0 for an event_type that ends a dispatch: the three legacy types plus
#   ADR-045's dispatch_finished. `dispatch_started` and `report_ingested` are
#   deliberately absent — ADR-045 §2 makes both never an outcome, so no filter
#   in the processor may accept them as an end.
rp_is_end_event() {
    case "${1:-}" in
        task_complete | task_failed | task_timeout | dispatch_finished) return 0 ;;
    esac
    return 1
}

# rp_is_completion_hook <event_type> <status>
#   0 when the completion-only hooks (contract-claim verification, pattern
#   adoption) run for this line. A legacy task_complete keeps its exact
#   behaviour (any status, unchanged); a dispatch_finished runs them only when
#   its end_state resolves to success — no resolvable end_state is no success.
rp_is_completion_hook() {
    case "${1:-}" in
        task_complete) return 0 ;;
        dispatch_finished) [ "${2:-}" = "success" ] && return 0 ;;
    esac
    return 1
}

# rp_end_status <record-json>
#   The status the record's end line carries: its own `status` for a legacy
#   line (read literally, exactly as before) and the end_state projection for a
#   dispatch_finished. Empty when a dispatch_finished has no resolvable
#   end_state — callers must treat that as "no status", never as a success.
rp_end_status() {
    _rp_end_line_fields "${1:-}" | cut -f1
}

# rp_end_outcome <record-json>
#   classify_event_outcome's verdict for the record: success|failure|none.
#   Use this where a reader needs the governed meaning rather than the literal
#   status (e.g. "is this line a success?"), so the vocabulary stays in Python.
rp_end_outcome() {
    _rp_end_line_fields "${1:-}" | cut -f2
}
